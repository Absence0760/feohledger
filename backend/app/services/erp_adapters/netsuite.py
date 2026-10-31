"""Oracle NetSuite adapter — direct REST API integration.

Two sign-in methods, chosen by ``settings.erp.auth_method``:

* ``oauth2_m2m`` — OAuth 2.0 client credentials (machine to machine): a JWT
  assertion signed with the private key of the certificate the customer mapped
  to the integration record, exchanged for a 60-minute bearer token. The only
  method Oracle lets a NEW integration use from 2027.1.
* ``tba`` — Token-Based Authentication (OAuth 1.0a), for integrations created
  before that. A block with no ``auth_method`` is one of those (every NetSuite
  configuration saved before OAuth 2.0 existed), so absent means ``tba``.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from joserfc import jwt
from joserfc.jwk import ECKey, RSAKey

from app.config import settings
from app.services.erp_adapters.base import (
    POSTED_TOTAL_MISMATCH,
    POSTED_TOTAL_UNCONFIRMED,
    VENDOR_NOT_LINKED,
    ErpAdapter,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    ListingGap,
    PoListing,
    PoPayload,
    PoSkipReason,
    SkippedPo,
    VendorPayload,
    erp_http_failure,
    erp_preflight_refusal,
    sync_listing_pages,
)
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter
from app.utils.json_money import dumps_exact_json, loads_exact_json

logger = logging.getLogger(__name__)

#: SuiteQL pages at 100 rows. Every listing (chart, vendors, POs) reads up to
#: ``SYNC_LISTING_MAX_ROWS`` and raises past it.
_PAGE_SIZE = 100

#: Vendor sync. The REST record collection (``GET /vendor``) returns only ids
#: and links — no names — so it would cost one more request per vendor.
#: ``BUILTIN.DF(terms)`` is the payment term's display name, not its id.
_VENDOR_QUERY = (
    "SELECT id, entityid, companyname, email, phone, BUILTIN.DF(terms) AS terms, isinactive "
    "FROM vendor ORDER BY id"
)

#: Chart sync (see ``list_gl_accounts``). ``ORDER BY`` keeps offset paging stable.
_ACCOUNT_QUERY = "SELECT id, acctnumber, fullname, accttype, isinactive FROM account ORDER BY id"

#: Purchase-order sync. ``t.entity`` is the vendor's internal id — the same
#: ``vendor.id`` that ``list_vendors`` reports as ``erp_vendor_id`` — so the PO
#: links to its vendor by id; ``BUILTIN.DF(t.entity)`` is the vendor's display
#: name, the sync's fallback only. ``foreigntotal`` is the total in the order's
#: own currency, and ``currency.symbol`` is that currency's ISO code. The date
#: goes through ``TO_CHAR`` because SuiteQL otherwise renders dates in the
#: user's date-format preference.
_PO_QUERY = (
    "SELECT t.id, t.tranid, t.status, t.entity AS vendorid, "
    "BUILTIN.DF(t.entity) AS vendorname, t.foreigntotal, "
    "c.symbol AS currency, TO_CHAR(t.duedate, 'YYYY-MM-DD') AS duedate "
    "FROM transaction t LEFT JOIN currency c ON c.id = t.currency "
    "WHERE t.type = 'PurchOrd' ORDER BY t.id"
)

#: A NetSuite account id: digits for production, ``1234567_SB1`` for a
#: sandbox, letters for some legacy accounts. It is spliced into the API
#: HOSTNAME (and the OAuth ``realm``), so anything outside this alphabet —
#: ``evil.tld/x?``, ``@``, a quote — is refused rather than escaped.
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


#: ``settings.erp.auth_method`` values. Absent means TBA (see the module doc).
AUTH_TBA = "tba"
AUTH_OAUTH2_M2M = "oauth2_m2m"
AUTH_METHODS: tuple[str, ...] = (AUTH_OAUTH2_M2M, AUTH_TBA)

#: The config keys each method needs. The secrets among them
#: (``consumer_secret``, ``token_secret``, ``private_key``) are sealed in
#: ``provider_credentials`` and merged in by the dispatcher.
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    AUTH_TBA: ("consumer_key", "consumer_secret", "token_id", "token_secret"),
    AUTH_OAUTH2_M2M: ("client_id", "certificate_id", "private_key"),
}

#: The OAuth 2.0 client-credentials grant
#: (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162755359851.html):
#: a form POST of these to the account's token endpoint. The assertion is a JWT
#: (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162790605110.html)
#: whose ``kid`` is the certificate id the mapping generated, ``iss`` the
#: integration's client id, ``aud`` the token endpoint, and ``exp`` under 60
#: minutes after ``iat``. REST records and SuiteQL are both ``rest_webservices``.
_CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
_OAUTH2_SCOPE = "rest_webservices"
_ASSERTION_LIFETIME_SECONDS = 300
#: A token is used until this many seconds before NetSuite says it expires.
_TOKEN_EXPIRY_MARGIN_SECONDS = 60
#: NetSuite documents ``expires_in`` as always 3600; used when it is missing.
_DEFAULT_TOKEN_LIFETIME_SECONDS = 3600

#: The keys NetSuite accepts for the certificate
#: (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_162755332391.html):
#: RSA of 3072 or 4096 bits, signed RSA-PSS (``PS256``), or EC on P-256 /
#: P-384 / P-521 (``ES256`` / ``ES384`` / ``ES512``). RS256 is not accepted.
_RSA_KEY_SIZES = frozenset({3072, 4096})
_EC_CURVE_ALGORITHMS: dict[str, str] = {
    "secp256r1": "ES256",
    "secp384r1": "ES384",
    "secp521r1": "ES512",
}

_PEM_BLOCK_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL)

#: Bearer tokens by connection (:meth:`NetSuiteAdapter._token_cache_key`):
#: ``(access_token, time.monotonic() expiry)``. In-process only, never
#: persisted or logged. The key hashes the private key in, so a rotated
#: certificate never reuses a token minted with the old one.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def clear_oauth2_token_cache() -> None:
    """Forget every cached bearer token."""
    _TOKEN_CACHE.clear()


class NetSuiteConfigError(ValueError):
    """``settings.erp`` has a bad NetSuite field. Names the key, never the value."""


def _normalise_pem(raw: str) -> bytes | None:
    """The first PEM block in ``raw``, re-wrapped at 64 columns.

    The settings form takes the key in a one-line input, and a browser strips
    the newlines out of a pasted value, so a PEM can arrive as
    ``-----BEGIN PRIVATE KEY-----MIIE...-----END PRIVATE KEY-----``. The
    base64 body's line breaks carry no meaning, so they are rebuilt.
    """
    match = _PEM_BLOCK_RE.search(raw)
    if match is None:
        return None
    label = match.group(1).strip()
    body = re.sub(r"\s+", "", match.group(2))
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n".encode()


#: Parsed signing keys by ``sha256(PEM)``, least recently used first. Keyed on
#: the digest, not the PEM: an ``lru_cache`` on the PEM argument would keep every
#: tenant's plaintext private key alive as a dict key for the life of the
#: process, beside the parsed key it was cached for.
_SIGNING_KEYS: OrderedDict[str, tuple[RSAKey | ECKey, str]] = OrderedDict()
_SIGNING_KEYS_MAX = 32
_SIGNING_KEYS_LOCK = threading.Lock()  # callers run in worker threads


def clear_signing_key_cache() -> None:
    """Forget every parsed signing key."""
    with _SIGNING_KEYS_LOCK:
        _SIGNING_KEYS.clear()


def _signing_key(private_key_pem: str) -> tuple[RSAKey | ECKey, str]:
    """The JWK and JWS algorithm for the certificate's private key.

    Loading an RSA key validates it, ~150 ms of CPU for 4096 bits, so a parsed
    key is memoised under the PEM's SHA-256 (a refusal is not) and every caller
    on the event loop reaches this through ``asyncio.to_thread``.
    """
    digest = hashlib.sha256(private_key_pem.encode()).hexdigest()
    with _SIGNING_KEYS_LOCK:
        hit = _SIGNING_KEYS.get(digest)
        if hit is not None:
            _SIGNING_KEYS.move_to_end(digest)
            return hit
    loaded = _load_signing_key(private_key_pem)
    with _SIGNING_KEYS_LOCK:
        _SIGNING_KEYS[digest] = loaded
        _SIGNING_KEYS.move_to_end(digest)
        while len(_SIGNING_KEYS) > _SIGNING_KEYS_MAX:
            _SIGNING_KEYS.popitem(last=False)
    return loaded


def _load_signing_key(private_key_pem: str) -> tuple[RSAKey | ECKey, str]:
    """Parse and check the key. Refuses, naming the key only, what NetSuite
    would refuse anyway: an unparseable or passphrase-protected PEM, an RSA key
    of the wrong size, a curve NetSuite does not accept, any other key type."""
    unusable = NetSuiteConfigError("NetSuite config 'private_key' is not a usable private key")
    pem = _normalise_pem(private_key_pem)
    if pem is None:
        raise unusable
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        # TypeError: the PEM is encrypted. Neither message is re-raised: the
        # error must never carry key material.
        raise unusable from None
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size not in _RSA_KEY_SIZES:
            raise NetSuiteConfigError(
                "NetSuite config 'private_key' must be an RSA key of 3072 or 4096 bits"
            )
        return RSAKey.import_key(key), "PS256"
    if isinstance(key, ec.EllipticCurvePrivateKey):
        alg = _EC_CURVE_ALGORITHMS.get(key.curve.name)
        if alg is None:
            raise NetSuiteConfigError(
                "NetSuite config 'private_key' must be an EC key on P-256, P-384 or P-521"
            )
        return ECKey.import_key(key), alg
    raise unusable


@register_adapter("netsuite")
class NetSuiteAdapter(ErpAdapter):
    """Direct integration with Oracle NetSuite REST API.

    Config:
        account_id: NetSuite account ID (e.g. "1234567", "1234567_SB1")
        auth_method: "oauth2_m2m" or "tba"; absent means "tba"

        OAuth 2.0 client credentials (``oauth2_m2m``):
            client_id: the integration record's client ID
            certificate_id: the Certificate ID NetSuite generated when the
                certificate was mapped to the integration (the JWT ``kid``)
            private_key: that certificate's PEM private key (secret)

        Token-Based Authentication (``tba``):
            consumer_key / consumer_secret: the integration record's
            token_id / token_secret: the access token's
    """

    erp_type = "netsuite"

    def _auth_method(self) -> str:
        """The configured sign-in method. Absent is TBA; anything else that is
        not a known method is refused rather than guessed at."""
        method = self.config.get("auth_method") or AUTH_TBA
        if method not in AUTH_METHODS:
            raise NetSuiteConfigError("NetSuite config 'auth_method' is invalid")
        return method

    async def _check_config(self) -> None:
        """Raise :class:`NetSuiteConfigError` (naming the key) for a config no
        request can succeed with: a bad ``account_id`` or ``auth_method``, a
        missing credential, or a private key NetSuite would not accept. Makes
        no request."""
        self._account_id()
        method = self._auth_method()
        for key in _REQUIRED_KEYS[method]:
            if not str(self.config.get(key) or "").strip():
                raise NetSuiteConfigError(f"NetSuite config '{key}' is missing")
        if method == AUTH_OAUTH2_M2M:
            await asyncio.to_thread(_signing_key, str(self.config["private_key"]))

    def _rest_root(self) -> str:
        """``.../services/rest``: the record API, SuiteQL and the OAuth 2.0
        token endpoint all sit under it."""
        base = self._base_url()
        if base.endswith("/record/v1"):
            base = base[: -len("/record/v1")]
        return base

    def _token_url(self) -> str:
        """The account's OAuth 2.0 token endpoint — also the assertion's
        ``aud``. Follows ``FEOH_ERP_NETSUITE_API_BASE`` when the operator set
        it, so the fake ERP gets the token request too, never real NetSuite."""
        return f"{self._rest_root()}/auth/oauth2/v1/token"

    def _token_cache_key(self) -> str:
        material = "\x00".join(
            str(self.config.get(k) or "")
            for k in ("account_id", "client_id", "certificate_id", "private_key")
        )
        return f"{self._token_url()}\x00{hashlib.sha256(material.encode()).hexdigest()}"

    def _client_assertion(self) -> str:
        """The signed JWT the token request carries (Oracle's "request token
        structure"): ``kid`` = certificate id, ``iss`` = client id, ``aud`` =
        the token endpoint, five minutes of validity."""
        key, alg = _signing_key(str(self.config["private_key"]))
        now = int(time.time())
        return jwt.encode(
            {"alg": alg, "typ": "JWT", "kid": str(self.config["certificate_id"])},
            {
                "iss": str(self.config["client_id"]),
                "scope": _OAUTH2_SCOPE,
                "aud": self._token_url(),
                "iat": now,
                "exp": now + _ASSERTION_LIFETIME_SECONDS,
            },
            key,
            algorithms=[alg],
        )

    async def _oauth2_token(self, client: httpx.AsyncClient) -> str | httpx.Response:
        """A bearer token for this connection: the cached one while it has more
        than a minute left, else a new one from the token endpoint.

        The grant issues no refresh token, so a new assertion is the only way
        to a new token. A refusal comes back as the token endpoint's own
        response, which every caller already treats as a failed call; its
        ``error`` code is logged, never the assertion or a token.
        """
        cache_key = self._token_cache_key()
        cached = _TOKEN_CACHE.get(cache_key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        resp = await client.post(
            self._token_url(),
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": _CLIENT_ASSERTION_TYPE,
                # Parsing (first use) and signing are CPU work: off the loop.
                "client_assertion": await asyncio.to_thread(self._client_assertion),
            },
        )
        if resp.status_code != 200:
            logger.warning(
                "NetSuite OAuth 2.0 token request refused: HTTP %s (%s)",
                resp.status_code,
                _refusal_reason(resp),
            )
            return resp
        try:
            body = resp.json()
        except ValueError:
            body = None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            # A 200 without a token is not a refusal we can hand back: as a
            # response it would read as the API call succeeding.
            raise httpx.DecodingError("NetSuite token response carried no access_token")
        try:
            lifetime = int(body.get("expires_in") or _DEFAULT_TOKEN_LIFETIME_SECONDS)
        except (TypeError, ValueError):
            lifetime = _DEFAULT_TOKEN_LIFETIME_SECONDS
        _TOKEN_CACHE[cache_key] = (
            token,
            time.monotonic() + max(lifetime - _TOKEN_EXPIRY_MARGIN_SECONDS, 0),
        )
        return token

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        content: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One authenticated NetSuite call, signed by the configured method.

        Under OAuth 2.0 a 401 means the bearer expired or was revoked early:
        the cached token is dropped and the call made once more with a new
        one. A second 401 is the answer.
        """
        method = method.upper()
        out = dict(headers or {})
        if self._auth_method() == AUTH_TBA:
            out["Authorization"] = self._auth_header(method, url)
            return await _dispatch(client, method, url, content, out)
        resp = await self._send_bearer(client, method, url, content, out)
        if resp.status_code != 401:
            return resp
        _TOKEN_CACHE.pop(self._token_cache_key(), None)
        return await self._send_bearer(client, method, url, content, out)

    async def _send_bearer(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        content: str | bytes | None,
        headers: dict[str, str],
    ) -> httpx.Response:
        token = await self._oauth2_token(client)
        if isinstance(token, httpx.Response):
            return token
        return await _dispatch(
            client, method, url, content, {**headers, "Authorization": f"Bearer {token}"}
        )

    def _account_id(self) -> str:
        """The configured ``account_id``, validated before it reaches a URL or
        a header. Admin-supplied, so it is checked, never trusted."""
        account = str(self.config.get("account_id") or "")
        if not _ACCOUNT_ID_RE.fullmatch(account):
            raise NetSuiteConfigError("NetSuite config 'account_id' is invalid")
        return account

    def _base_url(self) -> str:
        # OPERATOR-controlled override (env/process level, not tenant-admin
        # config) so local dev + e2e can point the adapter at the fake ERP
        # container (backend/docker-compose.yml `fake-erp`, host port 12112).
        # Trusted, so no SSRF guard. Empty (the default) = derive the real
        # per-account NetSuite URL from account_id. OAuth 1.0 signing below
        # always signs the URL actually used, so requests to the override
        # carry a signature computed over the override URL; the OAuth 2.0
        # token endpoint (`_token_url`) follows the override as well.
        if settings.erp_netsuite_api_base:
            return settings.erp_netsuite_api_base.rstrip("/")
        account = self._account_id().replace("_", "-").lower()
        return f"https://{account}.suitetalk.api.netsuite.com/services/rest/record/v1"

    def _auth_header(self, method: str, url: str) -> str:
        """Generate OAuth 1.0 authorization header for NetSuite TBA."""
        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time()))

        params = {
            "oauth_consumer_key": self.config["consumer_key"],
            "oauth_token": self.config["token_id"],
            "oauth_nonce": nonce,
            "oauth_timestamp": timestamp,
            "oauth_signature_method": "HMAC-SHA256",
            "oauth_version": "1.0",
        }

        base_string = _tba_base_string(method, url, params)
        signing_key = (
            f"{_oauth1_encode(self.config['consumer_secret'])}"
            f"&{_oauth1_encode(self.config['token_secret'])}"
        )
        signature = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha256).digest()
        sig_b64 = base64.b64encode(signature).decode()

        parts = [
            f'OAuth realm="{self._account_id()}"',
            f'oauth_consumer_key="{_oauth1_encode(params["oauth_consumer_key"])}"',
            f'oauth_token="{_oauth1_encode(params["oauth_token"])}"',
            f'oauth_nonce="{nonce}"',
            f'oauth_timestamp="{timestamp}"',
            'oauth_signature_method="HMAC-SHA256"',
            'oauth_version="1.0"',
            f'oauth_signature="{_oauth1_encode(sig_b64)}"',
        ]
        return ", ".join(parts)

    async def _find_by_external_id(self, external_id: str) -> tuple[int, str | None]:
        """Look up an existing vendorBill by externalId.

        NetSuite enforces externalId uniqueness per record type, so this is
        the pre-create idempotency check (issue #143): a retried push after a
        client-side timeout on the FIRST attempt's response (which may have
        already succeeded server-side) finds the already-created bill here
        instead of blindly POSTing a second one.

        Returns ``(status_code, id)``. A non-200 comes back as-is so the caller
        fails the push: a lookup that could not be made is not a miss.
        """
        q = f'externalId IS "{external_id}"'
        url = f"{self._base_url()}/vendorBill?q={quote(q, safe='')}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await self._send(client, "GET", url)
        if resp.status_code != 200:
            return resp.status_code, None
        items = resp.json().get("items", [])
        if not items:
            return 200, None
        return 200, items[0].get("id")

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        # Refuse before any HTTP call. A vendorBill is posted against the
        # vendor's and each account's internal id; a name or a code's text is
        # never a stand-in (a name picks the wrong "Acme" the first time two
        # vendors share one).
        if not payload.vendor_erp_id:
            return erp_preflight_refusal("NetSuite", VENDOR_NOT_LINKED)
        expense_lines = _netsuite_expense_lines(payload)
        if isinstance(expense_lines, str):
            return erp_preflight_refusal("NetSuite", expense_lines)
        try:
            await self._check_config()
        except NetSuiteConfigError as exc:
            # Retrying cannot fix the config, and the message names the key only.
            return ErpPostResult(success=False, message=str(exc), retryable=False)

        lookup_status, existing_id = await self._find_by_external_id(payload.correlation_id)
        if lookup_status != 200:
            return erp_http_failure("NetSuite", lookup_status)
        if existing_id:
            # Our earlier attempt; it counts only if NetSuite booked the
            # approved total (an unconfirmed first attempt lands here too).
            existing_id = str(existing_id)
            problem = await self._confirm_posted_total(payload, existing_id)
            if problem:
                return problem
            return ErpPostResult(
                success=True,
                erp_document_id=existing_id,
                erp_document_number=payload.invoice_number,
                message="Already posted to NetSuite (idempotent — found by externalId)",
            )

        url = f"{self._base_url()}/vendorBill"

        body = {
            "entity": {"id": payload.vendor_erp_id},
            "tranId": payload.invoice_number,
            "tranDate": payload.invoice_date.isoformat() if payload.invoice_date else None,
            "dueDate": payload.due_date.isoformat() if payload.due_date else None,
            "currency": {"refName": payload.currency},
            "memo": payload.description,
            "externalId": payload.correlation_id,
            # GL-coded lines go on the EXPENSE sublist (account + amount). The
            # `item` sublist books against an inventory/service ITEM record,
            # which we don't have, so it cannot carry these lines.
            "expense": {"items": expense_lines},
        }

        # Synchronous on purpose: `Prefer: respond-async` turns the create into
        # a job (202, Location = the job), and the total check below needs the
        # record's own Location.
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await self._send(
                client,
                "POST",
                url,
                content=dumps_exact_json(body),
                headers={"Content-Type": "application/json"},
            )

        if resp.status_code in (200, 201, 204):
            # NetSuite returns the record ID in the Location header
            location = resp.headers.get("Location", "")
            doc_id = location.rsplit("/", 1)[-1] if location else None
            # NetSuite totals the bill itself, and on a tax-enabled account an
            # expense line's tax code adds tax to what we sent. Only the
            # approved amount counts as posted.
            problem = await self._confirm_posted_total(payload, doc_id or None)
            if problem:
                return problem
            return ErpPostResult(
                success=True,
                erp_document_id=doc_id,
                erp_document_number=payload.invoice_number,
                message="Posted to NetSuite",
                raw_response=resp.json() if resp.content else None,
            )
        else:
            return erp_http_failure(
                "NetSuite",
                resp.status_code,
                raw_response=resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else None,
            )

    async def _read_bill_total(self, doc_id: str) -> Decimal | None:
        """The ``total`` NetSuite booked on a vendorBill, or None when the
        record could not be read or carries none. Parsed exactly — never via a
        float."""
        url = f"{self._base_url()}/vendorBill/{quote(doc_id, safe='')}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await self._send(client, "GET", url)
        if resp.status_code != 200:
            return None
        try:
            record = loads_exact_json(resp.content)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        return _netsuite_decimal(record.get("total"))

    async def _confirm_posted_total(
        self, payload: InvoicePayload, doc_id: str | None
    ) -> ErpPostResult | None:
        """None when the bill's NetSuite ``total`` is exactly ``payload.amount``.

        The shape of ``posted_total.check_posted_total``, which this does not
        call only because its message says "voided": NetSuite's REST service
        cannot void, and ``void_invoice`` DELETES the bill, and only while it
        is still pending approval (an approved bill has posted to the GL and is
        the accountant's to reverse). So:

        * no record id, an unreadable record, or no ``total`` → non-retryable
          ``posted_total_unconfirmed``, never success. A manual retry re-finds
          the bill by ``externalId`` and checks it again;
        * a different total → non-retryable ``posted_total_mismatch``, after
          deleting the bill where ``void_invoice`` allows it; the message says
          whether it did.
        """
        total = await self._read_bill_total(doc_id) if doc_id else None
        if total is not None and total == payload.amount:
            return None
        if total is None:
            return ErpPostResult(
                success=False,
                erp_document_id=doc_id,
                erp_document_number=payload.invoice_number,
                message=f"NetSuite post unconfirmed: {POSTED_TOTAL_UNCONFIRMED} "
                "(the bill was created but NetSuite reported no total)",
                retryable=False,
                refusal_code=POSTED_TOTAL_UNCONFIRMED,
                refusal_provider="NetSuite",
            )
        try:
            deleted = await self.void_invoice(doc_id)
        except httpx.HTTPError:
            # Not swallowed: the message below says the bill was not deleted.
            deleted = False
        outcome = (
            "the bill was deleted"
            if deleted
            else "the bill could not be deleted in NetSuite: it is no longer pending approval "
            "or NetSuite refused"
        )
        return ErpPostResult(
            success=False,
            erp_document_id=doc_id,
            erp_document_number=payload.invoice_number,
            message=f"NetSuite post failed: {POSTED_TOTAL_MISMATCH} ({outcome})",
            retryable=False,
            refusal_code=POSTED_TOTAL_MISMATCH,
            refusal_provider="NetSuite",
        )

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        url = f"{self._base_url()}/vendorBill/{erp_document_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await self._send(client, "GET", url)

        if resp.status_code != 200:
            return ErpInvoiceStatus.unknown

        status_map = {
            "open": ErpInvoiceStatus.open,
            "pendingapproval": ErpInvoiceStatus.draft,
            "paidinfull": ErpInvoiceStatus.paid,
            "cancelled": ErpInvoiceStatus.cancelled,
            "voided": ErpInvoiceStatus.cancelled,
        }
        return status_map.get(_netsuite_bill_status(resp.json()), ErpInvoiceStatus.unknown)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Delete the vendorBill while it is pending approval; otherwise False.

        The REST record service has no void. Its record actions are a fixed
        list that names neither ``vendorBill`` nor a void action
        (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1516982564.html);
        voiding is ``transaction.void`` in SuiteScript's N/transaction module
        (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4413162576.html),
        reachable only through a RESTlet the customer deploys. What REST does
        offer is ``DELETE /record/v1/vendorBill/{id}``
        (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1545142287.html).

        A bill pending approval has posted nothing to the GL, so deleting it is
        the whole cancellation. An approved bill has: deleting it would erase
        posted history rather than reverse it, and the right reversal (a void,
        a reversing journal, a vendor credit) is the accountant's call in
        NetSuite. So every other status returns False, as does a bill NetSuite
        no longer has.
        """
        url = f"{self._base_url()}/vendorBill/{quote(erp_document_id, safe='')}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await self._send(client, "GET", url)
            if resp.status_code != 200:
                return False
            if _netsuite_bill_status(resp.json()) != "pendingapproval":
                return False
            resp = await self._send(client, "DELETE", url)
        return resp.status_code == 204

    def _suiteql_url(self) -> str:
        """SuiteQL lives beside the record API: ``.../services/rest/query/v1``."""
        return f"{self._rest_root()}/query/v1/suiteql"

    async def _suiteql(self, query: str, *, collection: str) -> list[dict]:
        """Every row of one SuiteQL query, paged by ``offset`` + ``hasMore``.

        Read whole or not at all: a non-200, a network error, an unparseable
        page, or more rows than ``SYNC_LISTING_MAX_ROWS`` raises
        :class:`ErpListingIncomplete` naming ``collection``. The rows read so
        far are never returned. The chart and vendor syncs read absence as
        evidence (§284), and the PO sync reads the listing's ``skipped`` report,
        which a partial read would silently shorten (§323). Bodies are parsed
        with ``loads_exact_json`` so a money column is never a float.
        """
        rows: list[dict] = []
        offset = 0
        body_text = dumps_exact_json({"q": query})
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(sync_listing_pages(_PAGE_SIZE)):
                url = f"{self._suiteql_url()}?limit={_PAGE_SIZE}&offset={offset}"
                headers = {"Content-Type": "application/json", "Prefer": "transient"}
                try:
                    resp = await self._send(client, "POST", url, content=body_text, headers=headers)
                except httpx.HTTPError:
                    raise ErpListingIncomplete(
                        "NetSuite", collection, ListingGap.fetch_failed
                    ) from None
                if resp.status_code != 200:
                    raise ErpListingIncomplete("NetSuite", collection, ListingGap.fetch_failed)
                try:
                    body = loads_exact_json(resp.content)
                except ValueError:
                    raise ErpListingIncomplete(
                        "NetSuite", collection, ListingGap.fetch_failed
                    ) from None
                rows.extend(r for r in body.get("items") or [] if isinstance(r, dict))
                if not body.get("hasMore"):
                    return rows
                offset += _PAGE_SIZE
        raise ErpListingIncomplete("NetSuite", collection, ListingGap.truncated)

    async def list_vendors(self) -> list[VendorPayload]:
        """Pull vendors through SuiteQL (``_VENDOR_QUERY``).

        The ``id`` is what ``post_invoice`` posts a bill's ``entity`` against.
        Inactive vendors are emitted ``active=False``: NetSuite will not take a
        bill for one, so the sync never creates a payee for it, but its id
        still exists.
        """
        out: list[VendorPayload] = []
        for raw in await self._suiteql(_VENDOR_QUERY, collection="vendors"):
            vendor = _netsuite_vendor_to_payload(raw)
            if vendor is not None:
                out.append(vendor)
        return out

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Pull the chart of accounts through SuiteQL (``_ACCOUNT_QUERY``).

        This sync is what fills ``gl_accounts.erp_account_id`` with NetSuite's
        internal ids, which ``post_invoice`` posts every expense line against.
        The REST record collection (``GET /account``) returns only ids and
        links, one fetch per account after that; one SuiteQL query returns the
        columns we need.
        """
        out: list[GLAccountPayload] = []
        for raw in await self._suiteql(_ACCOUNT_QUERY, collection="accounts"):
            acct = _netsuite_account_to_payload(raw)
            if acct is not None:
                out.append(acct)
        return out

    async def list_pos(self) -> list[PoPayload]:
        return (await self.list_pos_report()).pos

    async def list_pos_report(self) -> PoListing:
        """Pull purchase orders through SuiteQL (``_PO_QUERY``), headers only.

        The PO sync stores the header (number, vendor, total, status, currency,
        expected date); it does not persist ERP lines, so no line query is
        made. A PO with no number is dropped. One with a number but no usable
        total is skipped, never synced at 0, and named in ``skipped`` as
        ``unpriceable``, so a total an earlier sync stored for it stops being
        matched against (decisions §323).

        Read whole or not at all (:meth:`_suiteql`): a failed or truncated
        read raises :class:`ErpListingIncomplete`, never the POs read so far,
        which would hide every skip in the part it did not read.
        """
        pos: list[PoPayload] = []
        skipped: list[SkippedPo] = []
        for raw in await self._suiteql(_PO_QUERY, collection="purchase orders"):
            po = _netsuite_po_to_payload(raw)
            if po is not None:
                pos.append(po)
            elif raw.get("tranid"):
                skipped.append(SkippedPo(str(raw["tranid"]), PoSkipReason.unpriceable))
        return PoListing(pos=pos, skipped=skipped)

    async def test_connection(self) -> bool:
        try:
            # Checked first so a missing credential or an unusable key is a
            # plain False, before any request (for OAuth 2.0 this call also
            # proves the token exchange: the certificate mapping, client id
            # and role).
            await self._check_config()
            url = f"{self._base_url()}/vendor?limit=1"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await self._send(client, "GET", url)
            return resp.status_code == 200
        except Exception:
            return False


def _oauth1_encode(value: str) -> str:
    """RFC 5849 §3.6 percent-encoding: everything but the unreserved set
    (ALPHA, DIGIT, ``-._~``) is encoded — ``/`` included, which ``quote``'s
    default leaves alone (a base64 signature can carry one)."""
    return quote(value, safe="~")


def _tba_base_string(method: str, url: str, oauth_params: dict[str, str]) -> str:
    """The OAuth 1.0a signature base string (RFC 5849 §3.4.1).

    The base string URI is the URL WITHOUT its query (§3.4.1.2: scheme and host
    lower-cased, a default port dropped); the query's parameters are signed as
    request parameters instead, beside the ``oauth_*`` ones, each name and
    value encoded, the pairs sorted, then encoded once more as a whole
    (§3.4.1.3). Signing the query inside the URI — what this did before — makes
    NetSuite compute a different signature for every request that carries one
    (SuiteQL paging, the ``externalId`` lookup, the connection test), so each
    of them was refused 401.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    netloc = host if parts.port in (None, default_port) else f"{host}:{parts.port}"
    base_uri = f"{scheme}://{netloc}{parts.path or '/'}"
    pairs = [*parse_qsl(parts.query, keep_blank_values=True), *oauth_params.items()]
    normalised = "&".join(
        f"{k}={v}" for k, v in sorted((_oauth1_encode(k), _oauth1_encode(v)) for k, v in pairs)
    )
    return "&".join((method.upper(), _oauth1_encode(base_uri), _oauth1_encode(normalised)))


async def _dispatch(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    content: str | bytes | None,
    headers: dict[str, str],
) -> httpx.Response:
    if method == "POST":
        return await client.post(url, content=content, headers=headers)
    if method == "DELETE":
        return await client.delete(url, headers=headers)
    return await client.get(url, headers=headers)


def _refusal_reason(resp: httpx.Response) -> str:
    """The OAuth ``error`` code of a refused token request (``invalid_client``,
    ``invalid_grant``), for the log. Only one of RFC 6749 §5.2's token-endpoint
    codes is returned (RFC 7523's JWT-assertion grant reuses them), and as our
    own literal: nothing from the provider's body reaches the log."""
    try:
        body = resp.json()
    except ValueError:
        return "unparseable"
    match body.get("error") if isinstance(body, dict) else None:
        case "invalid_request":
            return "invalid_request"
        case "invalid_client":
            return "invalid_client"
        case "invalid_grant":
            return "invalid_grant"
        case "unauthorized_client":
            return "unauthorized_client"
        case "unsupported_grant_type":
            return "unsupported_grant_type"
        case "invalid_scope":
            return "invalid_scope"
        case "server_error":
            return "server_error"
        case "temporarily_unavailable":
            return "temporarily_unavailable"
        case _:
            return "unknown"


def _netsuite_bill_status(record: dict) -> str:
    """A vendorBill's status, normalised to compare: ``id`` lower-cased
    (``paidInFull`` -> ``paidinfull``), else ``refName`` with its spaces removed
    (``Paid In Full`` -> ``paidinfull``)."""
    status = record.get("status") or {}
    if not isinstance(status, dict):
        return ""
    raw = status.get("id") or status.get("refName") or ""
    return str(raw).replace(" ", "").lower()


def _netsuite_vendor_to_payload(raw: dict) -> VendorPayload | None:
    """Map a SuiteQL ``vendor`` row (lower-case columns) to a VendorPayload.

    The name is ``companyname``, falling back to ``entityid`` (an individual
    vendor has no company name); ``entityid`` is also the vendor's code. An
    inactive vendor (``isinactive`` "T") is emitted with ``active=False`` (the
    sync never creates a payee for one, and its id still exists); a row with no
    id is skipped. Anything absent maps to None — ``sync_vendors_from_erp``
    never nulls out an existing local value for a missing field.
    """
    vendor_id = raw.get("id")
    if vendor_id is None:
        return None
    entity_id = raw.get("entityid") or None
    name = raw.get("companyname") or entity_id or str(vendor_id)
    return VendorPayload(
        erp_vendor_id=str(vendor_id),
        name=str(name),
        code=str(entity_id) if entity_id else None,
        email=raw.get("email") or None,
        phone=raw.get("phone") or None,
        payment_terms=raw.get("terms") or None,
        active=str(raw.get("isinactive", "F")).upper() != "T",
    )


#: Purchase-order status letters (``transaction.status`` for ``PurchOrd``) ->
#: our PO vocabulary. A Pending Supervisor Approval (A), Pending Receipt (B),
#: Partially Received (D), Pending Billing/Partially Received (E) or Pending
#: Bill (F) order is still open.
_NETSUITE_PO_STATUSES: dict[str, str] = {
    "C": "cancelled",  # Rejected by Supervisor
    "G": "closed",  # Fully Billed
    "H": "closed",  # Closed
}


def _netsuite_po_to_payload(raw: dict) -> PoPayload | None:
    """Map a SuiteQL purchase-order row to a PoPayload.

    ``currency`` is the currency's ISO symbol only when NetSuite states one —
    never defaulted (decisions §197); ``expected_delivery_date`` is the
    "Receive By" date (``duedate``) only when set.
    """
    number = raw.get("tranid")
    total_raw = raw.get("foreigntotal")
    if not number or total_raw is None or total_raw == "":
        return None
    try:
        total = total_raw if isinstance(total_raw, Decimal) else Decimal(str(total_raw))
    except (InvalidOperation, ValueError):
        return None
    expected: date | None = None
    if raw.get("duedate"):
        try:
            expected = date.fromisoformat(str(raw["duedate"])[:10])
        except ValueError:
            expected = None
    currency = str(raw.get("currency") or "").strip().upper()
    vendor_id = raw.get("vendorid")
    return PoPayload(
        po_number=str(number),
        vendor_name=raw.get("vendorname") or None,
        # The vendor's internal id, the same string `_netsuite_vendor_to_payload`
        # reports as `erp_vendor_id`, so the PO links by id, not by name.
        vendor_erp_id=str(vendor_id) if vendor_id not in (None, "") else None,
        total=total,
        status=_NETSUITE_PO_STATUSES.get(str(raw.get("status") or "").strip().upper(), "open"),
        expected_delivery_date=expected,
        currency=currency or None,
    )


def _netsuite_decimal(raw: object) -> Decimal | None:
    """A NetSuite number as an exact Decimal, or None when absent/unparseable."""
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _netsuite_expense_lines(payload: InvoicePayload) -> list[dict] | str:
    """The vendorBill ``expense`` sublist, or the stable reason it is refused.

    The lines are the shared ``bill_lines.bill_lines`` projection — the one
    statement of which lines, on which account, for what gross, and every
    refusal (``line_amount_missing``, ``account_not_linked``,
    ``amount_mismatch``, ``tax_not_itemised``). Their grosses sum to exactly
    ``payload.amount``; the header amount is never recomputed from them.

    NetSuite-specific is only the shape: each line posts on its account's
    internal id and its amount stays a Decimal all the way to the encoder
    (``utils/json_money``). What NetSuite then books — its own tax codes can
    add to the lines — is checked after the create
    (``NetSuiteAdapter._confirm_posted_total``).
    """
    lines = bill_lines(payload)
    if isinstance(lines, str):
        return lines
    return [
        {"account": {"id": account_id}, "amount": gross, "memo": memo}
        for account_id, gross, memo in lines
    ]


#: NetSuite ``accttype`` -> the vocabulary ``GLAccountPayload.account_type``
#: uses. Anything unlisted maps to None (the sync accepts an unclassified row).
_NETSUITE_ACCOUNT_TYPES: dict[str, str] = {
    "Bank": "asset",
    "AcctRec": "asset",
    "OthCurrAsset": "asset",
    "FixedAsset": "asset",
    "OthAsset": "asset",
    "DeferExpense": "asset",
    "UnbilledRec": "asset",
    "AcctPay": "liability",
    "CredCard": "liability",
    "OthCurrLiab": "liability",
    "LongTermLiab": "liability",
    "DeferRevenue": "liability",
    "Equity": "equity",
    "Income": "revenue",
    "OthIncome": "revenue",
    "COGS": "expense",
    "Expense": "expense",
    "OthExpense": "expense",
}


def _netsuite_account_to_payload(raw: dict) -> GLAccountPayload | None:
    """Map a SuiteQL ``account`` row to a GLAccountPayload.

    SuiteQL returns lower-case column names. An account with no number (the
    "Use Account Numbers" preference off) is keyed by its name, since the code
    is what an AP clerk picks; its internal id is what the bill is posted
    against either way.

    Emitted, not dropped: an inactive account (``isinactive`` "T") as
    ``active=False``, and a code longer than ``gl_accounts.code`` as it is.
    The sync then marks the first inactive in our chart and refuses the second
    ``code_too_long`` — never truncated, which would let two long names share
    a code — and either way still knows the account's id exists, so a row
    linked to it is not mistaken for a link to a deleted account.
    """
    account_id = raw.get("id")
    if account_id is None:
        return None
    name = raw.get("fullname") or ""
    code = str(raw.get("acctnumber") or name)
    if not code:
        return None
    return GLAccountPayload(
        code=code,
        name=str(name or code)[:255],
        account_type=_NETSUITE_ACCOUNT_TYPES.get(str(raw.get("accttype") or "")),
        erp_account_id=str(account_id),
        active=str(raw.get("isinactive", "F")).upper() != "T",
    )
