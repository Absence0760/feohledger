"""NetSuite's OAuth 2.0 client-credentials (M2M) token check, for the fake ERP.

Real NetSuite (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162755359851.html)
takes a form POST of ``grant_type=client_credentials``, ``client_assertion_type
=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`` and ``client_assertion``:
a JWT whose header is ``{"typ": "JWT", "alg": PS256|PS384|PS512|ES256|ES384|ES512,
"kid": <certificate id>}`` and whose claims are ``iss`` (the integration's client
id), ``scope`` (``restlets`` / ``rest_webservices`` / ``suite_analytics``,
comma-separated), ``aud`` (the token endpoint), ``iat`` and ``exp`` (under 60
minutes after ``iat``) — signed with the private key of the certificate an
administrator mapped to the integration
(https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162790605110.html).

This module verifies exactly that, signature included, against certificates
registered the way an administrator uploads one: the fake's
``POST /__netsuite/oauth2/certificates`` hook (``register``). Pure — no FastAPI,
no clock of its own — so the backend's tests can drive the same check
in-process (``backend/tests/test_erp_netsuite_auth.py``).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
TOKEN_LIFETIME_SECONDS = 3600
#: NetSuite: ``exp`` must be less than 60 minutes after ``iat``.
MAX_ASSERTION_LIFETIME_SECONDS = 3600
#: Clock skew tolerated on ``iat``.
_SKEW_SECONDS = 60
_SCOPES = frozenset({"restlets", "rest_webservices", "suite_analytics"})

_HASHES: dict[str, type[hashes.HashAlgorithm]] = {
    "256": hashes.SHA256,
    "384": hashes.SHA384,
    "512": hashes.SHA512,
}
_EC_CURVES: dict[str, str] = {"ES256": "secp256r1", "ES384": "secp384r1", "ES512": "secp521r1"}


class TokenError(Exception):
    """An OAuth error response: ``error`` is the RFC 6749 code."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description

    def body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


@dataclass(frozen=True)
class Certificate:
    """One mapping, as an administrator creates it in NetSuite: the
    certificate's public key, bound to an integration's client id."""

    certificate_id: str
    client_id: str
    public_key: Any


def register(certificate_id: str, client_id: str, public_key_pem: str) -> Certificate:
    """Parse an uploaded public key (or certificate). Raises ValueError for one
    NetSuite would refuse: not RSA 3072/4096 or EC P-256/P-384/P-521."""
    data = public_key_pem.encode()
    try:
        if b"CERTIFICATE" in data:
            from cryptography import x509

            key = x509.load_pem_x509_certificate(data).public_key()
        else:
            key = serialization.load_pem_public_key(data)
    except ValueError as exc:
        raise ValueError("not a PEM public key or certificate") from exc
    if isinstance(key, rsa.RSAPublicKey):
        if key.key_size not in (3072, 4096):
            raise ValueError("RSA key must be 3072 or 4096 bits")
    elif isinstance(key, ec.EllipticCurvePublicKey):
        if key.curve.name not in _EC_CURVES.values():
            raise ValueError("EC key must be on P-256, P-384 or P-521")
    else:
        raise ValueError("key must be RSA or EC")
    if not certificate_id or not client_id:
        raise ValueError("certificate_id and client_id are required")
    return Certificate(certificate_id, client_id, key)


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _verify_signature(cert: Certificate, alg: str, signing_input: bytes, sig: bytes) -> None:
    family, bits = alg[:2], alg[2:]
    hash_cls = _HASHES.get(bits)
    if hash_cls is None:
        raise TokenError("invalid_client", "unsupported alg")
    key = cert.public_key
    try:
        if family == "PS" and isinstance(key, rsa.RSAPublicKey):
            # RFC 7518 §3.5: MGF1 with the same hash, salt as long as the hash.
            key.verify(
                sig,
                signing_input,
                padding.PSS(mgf=padding.MGF1(hash_cls()), salt_length=hash_cls.digest_size),
                hash_cls(),
            )
            return
        if family == "ES" and isinstance(key, ec.EllipticCurvePublicKey):
            if key.curve.name != _EC_CURVES[alg]:
                raise TokenError("invalid_client", "alg does not match the certificate's curve")
            # RFC 7518 §3.4: the JWS signature is r || s, fixed width.
            width = (key.curve.key_size + 7) // 8
            if len(sig) != 2 * width:
                raise InvalidSignature
            r = int.from_bytes(sig[:width], "big")
            s = int.from_bytes(sig[width:], "big")
            key.verify(encode_dss_signature(r, s), signing_input, ec.ECDSA(hash_cls()))
            return
    except InvalidSignature:
        raise TokenError("invalid_client", "signature does not verify") from None
    raise TokenError("invalid_client", "alg does not match the certificate's key type")


def verify_token_request(
    form: dict[str, str],
    *,
    token_url: str,
    certificates: dict[str, Certificate],
    now: int,
) -> dict[str, Any]:
    """The verified assertion's claims, or :class:`TokenError`.

    ``token_url`` is the endpoint the request reached: the assertion's ``aud``
    must name it exactly, as NetSuite's must name the account's own endpoint.
    """
    if form.get("grant_type") != "client_credentials":
        raise TokenError("unsupported_grant_type", "grant_type must be client_credentials")
    if form.get("client_assertion_type") != CLIENT_ASSERTION_TYPE:
        raise TokenError("invalid_request", "client_assertion_type must be jwt-bearer")
    assertion = form.get("client_assertion") or ""
    parts = assertion.split(".")
    if len(parts) != 3:
        raise TokenError("invalid_request", "client_assertion is not a JWS")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except (ValueError, UnicodeDecodeError):
        raise TokenError("invalid_request", "client_assertion is not a JWS") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise TokenError("invalid_request", "client_assertion is not a JWS")
    if header.get("typ") != "JWT":
        raise TokenError("invalid_request", "typ must be JWT")
    alg = header.get("alg")
    if alg not in {"PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}:
        # RS256 included: NetSuite accepts only RSA-PSS and ECDSA.
        raise TokenError("invalid_client", "unsupported alg")
    cert = certificates.get(str(header.get("kid") or ""))
    if cert is None:
        raise TokenError("invalid_client", "unknown kid")
    _verify_signature(cert, alg, f"{parts[0]}.{parts[1]}".encode(), signature)
    if claims.get("iss") != cert.client_id:
        raise TokenError("invalid_client", "iss is not the client the certificate is mapped to")
    if claims.get("aud") != token_url:
        raise TokenError("invalid_grant", "aud is not this token endpoint")
    scopes = {s.strip() for s in str(claims.get("scope") or "").split(",") if s.strip()}
    if not scopes or not scopes <= _SCOPES:
        raise TokenError(
            "invalid_scope", "scope must be restlets / rest_webservices / suite_analytics"
        )
    iat, exp = claims.get("iat"), claims.get("exp")
    if not isinstance(iat, int) or not isinstance(exp, int):
        raise TokenError("invalid_grant", "iat and exp are required")
    if iat > now + _SKEW_SECONDS or exp <= now:
        raise TokenError("invalid_grant", "assertion is not currently valid")
    if exp - iat >= MAX_ASSERTION_LIFETIME_SECONDS:
        raise TokenError("invalid_grant", "exp must be less than 60 minutes after iat")
    return claims
