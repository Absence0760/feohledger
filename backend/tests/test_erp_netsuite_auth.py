"""NetSuite sign-in: OAuth 2.0 client credentials (M2M) beside TBA (issue #653).

From NetSuite 2027.1 no new integration can use Token-Based Authentication, so
the adapter also signs in with the OAuth 2.0 client-credentials grant: a JWT
assertion signed with the private key of the certificate mapped to the
integration, exchanged at the account's token endpoint for a 60-minute bearer.

The token endpoint here is the fake ERP's own check
(``tools/fake-erp/netsuite_oauth2.py``, loaded in-process), which verifies the
assertion the way Oracle documents it — alg, kid, the signature itself, iss,
aud, scope, iat/exp — so these tests prove the adapter's assertion would be
accepted, not merely that a header was set. A live NetSuite sandbox is still
the only proof against Oracle itself (docs/followups.md).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.config import settings
from app.services.erp_adapters import netsuite
from app.services.erp_adapters.base import InvoicePayload, LineItemPayload
from app.services.erp_adapters.netsuite import NetSuiteAdapter

_FAKE_ERP = Path(__file__).resolve().parents[2] / "tools" / "fake-erp"


def _load_fake_oauth2():
    spec = importlib.util.spec_from_file_location(
        "fake_netsuite_oauth2", _FAKE_ERP / "netsuite_oauth2.py"
    )
    module = importlib.util.module_from_spec(spec)
    # A dataclass resolves its module through sys.modules while it is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FAKE = _load_fake_oauth2()

API_BASE = "http://fake-erp.test/netsuite/services/rest/record/v1"
TOKEN_URL = "http://fake-erp.test/netsuite/services/rest/auth/oauth2/v1/token"
CLIENT_ID = "ns-client-id-1"
CERT_ID = "cert-id-1"


def _pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _public_pem(key) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(settings, "erp_netsuite_api_base", API_BASE)
    netsuite.clear_oauth2_token_cache()
    netsuite.clear_signing_key_cache()
    yield
    netsuite.clear_oauth2_token_cache()
    netsuite.clear_signing_key_cache()


def _config(private_key_pem: str, **overrides) -> dict:
    cfg = {
        "type": "netsuite",
        "integration_method": "direct",
        "account_id": "1234567_SB1",
        "auth_method": "oauth2_m2m",
        "client_id": CLIENT_ID,
        "certificate_id": CERT_ID,
        "private_key": private_key_pem,
    }
    cfg.update(overrides)
    return cfg


class _FakeNetSuite:
    """The token endpoint (the fake ERP's verifier) and the record routes
    ``post_invoice`` / ``test_connection`` reach, each requiring a bearer the
    endpoint issued."""

    def __init__(self, public_key_pem: str | None):
        self.certificates = (
            {CERT_ID: FAKE.register(CERT_ID, CLIENT_ID, public_key_pem)} if public_key_pem else {}
        )
        self.tokens: set[str] = set()
        self.minted = 0
        self.token_forms: list[dict] = []
        self.api_calls: list[httpx.Request] = []
        #: Answer the next N API calls 401 whatever the token (an early expiry).
        self.reject_next = 0
        #: Answer the next N vendorBill POSTs 401 (creating nothing), whatever
        #: the token: a token revoked between the lookup and the create.
        self.reject_next_create = 0
        #: Status of a vendorBill POST that creates the bill.
        self.create_status = 204
        #: vendorBill POSTs NetSuite answered by creating a bill.
        self.creates = 0
        self.bill_total = Decimal("100.00")

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append(form)
            try:
                FAKE.verify_token_request(
                    form,
                    token_url=TOKEN_URL,
                    certificates=self.certificates,
                    now=int(time.time()),
                )
            except FAKE.TokenError as exc:
                return httpx.Response(400, json=exc.body())
            self.minted += 1
            token = f"bearer-{self.minted}"
            self.tokens.add(token)
            return httpx.Response(
                200, json={"access_token": token, "expires_in": 3600, "token_type": "Bearer"}
            )
        self.api_calls.append(request)
        auth = request.headers.get("authorization", "")
        path = request.url.path.removeprefix("/netsuite/services/rest/record/v1")
        if request.method == "POST" and path == "/vendorBill" and self.reject_next_create > 0:
            self.reject_next_create -= 1
            return httpx.Response(401, json={"title": "Unauthorized"})
        if self.reject_next > 0 or auth.removeprefix("Bearer ") not in self.tokens:
            self.reject_next = max(self.reject_next - 1, 0)
            return httpx.Response(401, json={"title": "Unauthorized"})
        if request.method == "GET" and path == "/vendor":
            return httpx.Response(200, json={"items": [], "hasMore": False})
        if request.method == "GET" and path == "/vendorBill":
            return httpx.Response(200, json={"items": []})
        if request.method == "POST" and path == "/vendorBill":
            self.creates += 1
            return httpx.Response(
                self.create_status, headers={"Location": f"{API_BASE}/vendorBill/1001"}
            )
        if request.method == "GET" and path == "/vendorBill/1001":
            return httpx.Response(200, content=f'{{"total": {self.bill_total}}}'.encode())
        raise AssertionError(f"unexpected {request.method} {request.url}")


_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def install(monkeypatch):
    """Route the adapter's HTTP to a :class:`_FakeNetSuite`."""

    def _install(fake: _FakeNetSuite) -> _FakeNetSuite:
        transport = httpx.MockTransport(lambda request: fake.handle(request))

        def client(*args, **kwargs):
            kwargs.setdefault("transport", transport)
            return _REAL_ASYNC_CLIENT(*args, **kwargs)

        monkeypatch.setattr(netsuite.httpx, "AsyncClient", client)
        return fake

    return _install


@pytest.fixture
def fake_netsuite(rsa_key, install):
    return install(_FakeNetSuite(_public_pem(rsa_key)))


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        correlation_id="corr-ns-oauth2-1",
        invoice_number="INV-NS-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        invoice_date=date(2026, 10, 1),
        vendor_erp_id="25",
        gl_account="6100",
        gl_account_erp_id="120",
        line_items=[
            LineItemPayload(
                line_number=1,
                description="Paper",
                total=Decimal("100.00"),
                gl_account="6100",
                gl_account_erp_id="120",
            )
        ],
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The grant itself
# ---------------------------------------------------------------------------


def test_oauth2_assertion_is_accepted_by_netsuites_check_and_the_bearer_is_used(
    rsa_key, fake_netsuite
):
    assert _run(NetSuiteAdapter(_config(_pem(rsa_key))).test_connection()) is True

    assert fake_netsuite.minted == 1
    form = fake_netsuite.token_forms[0]
    assert form["grant_type"] == "client_credentials"
    assert form["client_assertion_type"] == (
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    )
    (call,) = fake_netsuite.api_calls
    assert call.headers["authorization"] == "Bearer bearer-1"
    assert not call.headers["authorization"].startswith("OAuth")


def test_the_assertion_carries_oracles_header_and_claims(rsa_key):
    import base64
    import json

    adapter = NetSuiteAdapter(_config(_pem(rsa_key)))
    header_b64, claims_b64, _sig = adapter._client_assertion().split(".")

    def decode(part: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

    assert decode(header_b64) == {"alg": "PS256", "typ": "JWT", "kid": CERT_ID}
    claims = decode(claims_b64)
    assert claims["iss"] == CLIENT_ID
    assert claims["scope"] == "rest_webservices"
    assert claims["aud"] == TOKEN_URL
    assert 0 < claims["exp"] - claims["iat"] < 3600


@pytest.mark.parametrize(
    ("curve", "alg"),
    [(ec.SECP256R1(), "ES256"), (ec.SECP384R1(), "ES384"), (ec.SECP521R1(), "ES512")],
)
def test_an_ec_certificate_signs_with_its_curves_es_algorithm(curve, alg, install):
    key = ec.generate_private_key(curve)
    fake = install(_FakeNetSuite(_public_pem(key)))
    adapter = NetSuiteAdapter(_config(_pem(key)))
    assert netsuite._signing_key(_pem(key))[1] == alg
    assert _run(adapter.test_connection()) is True
    assert fake.minted == 1


def test_the_token_is_reused_until_it_nears_expiry(rsa_key, fake_netsuite):
    adapter = NetSuiteAdapter(_config(_pem(rsa_key)))
    assert _run(adapter.test_connection()) is True
    # A second adapter for the same connection (each push builds its own).
    assert _run(NetSuiteAdapter(_config(_pem(rsa_key))).test_connection()) is True
    assert fake_netsuite.minted == 1

    # Past its expiry less the margin, a new assertion is exchanged.
    for key, (token, _expiry) in list(netsuite._TOKEN_CACHE.items()):
        netsuite._TOKEN_CACHE[key] = (token, 0.0)
    assert _run(adapter.test_connection()) is True
    assert fake_netsuite.minted == 2


def test_a_rotated_key_never_reuses_the_old_keys_token(rsa_key, fake_netsuite):
    assert _run(NetSuiteAdapter(_config(_pem(rsa_key))).test_connection()) is True
    other = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    fake_netsuite.certificates["cert-id-2"] = FAKE.register(
        "cert-id-2", CLIENT_ID, _public_pem(other)
    )
    rotated = NetSuiteAdapter(_config(_pem(other), certificate_id="cert-id-2"))
    assert _run(rotated.test_connection()) is True
    assert fake_netsuite.minted == 2


def test_a_401_mints_a_new_token_and_retries_once(rsa_key, fake_netsuite):
    adapter = NetSuiteAdapter(_config(_pem(rsa_key)))
    assert _run(adapter.test_connection()) is True
    fake_netsuite.reject_next = 1  # the cached token was revoked early
    assert _run(adapter.test_connection()) is True
    assert fake_netsuite.minted == 2
    assert fake_netsuite.api_calls[-1].headers["authorization"] == "Bearer bearer-2"

    # A second 401 is the answer: no loop.
    fake_netsuite.reject_next = 2
    before = len(fake_netsuite.api_calls)
    assert _run(adapter.test_connection()) is False
    assert len(fake_netsuite.api_calls) - before == 2


def test_a_401_on_the_bill_create_retries_once_and_creates_exactly_one_bill(rsa_key, fake_netsuite):
    """The money-path case of the 401 retry: the vendorBill POST itself is
    refused 401 (a token revoked between the lookup and the create), the retry
    with a fresh token is answered 201. A 401 means NetSuite created nothing,
    so exactly one bill exists afterwards — never two."""
    fake_netsuite.reject_next_create = 1
    fake_netsuite.create_status = 201

    result = _run(NetSuiteAdapter(_config(_pem(rsa_key))).post_invoice(_payload()))

    assert result.success, result.message
    assert result.erp_document_id == "1001"
    posts = [
        c
        for c in fake_netsuite.api_calls
        if c.method == "POST" and c.url.path.endswith("/vendorBill")
    ]
    assert len(posts) == 2  # the refused attempt and the retry
    assert fake_netsuite.creates == 1
    # The retry carried a freshly minted token, not the one refused.
    assert posts[0].headers["authorization"] == "Bearer bearer-1"
    assert posts[1].headers["authorization"] == "Bearer bearer-2"


def test_key_loading_and_signing_never_run_on_the_event_loop(rsa_key, fake_netsuite, monkeypatch):
    """Loading a 4096-bit RSA key validates it (~150 ms of CPU): on the loop it
    would stall every other request on the worker (the project invariant
    "Blocking work does not run on the event loop")."""
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = netsuite._signing_key

    def spy(pem):
        seen.append(threading.get_ident())
        return real(pem)

    monkeypatch.setattr(netsuite, "_signing_key", spy)
    result = _run(NetSuiteAdapter(_config(_pem(rsa_key))).post_invoice(_payload()))
    assert result.success, result.message
    assert seen and loop_thread not in seen


def test_post_invoice_signs_in_with_oauth2_end_to_end(rsa_key, fake_netsuite):
    result = _run(NetSuiteAdapter(_config(_pem(rsa_key))).post_invoice(_payload()))
    assert result.success, result.message
    assert result.erp_document_id == "1001"
    # One token for the lookup, the create and the total read-back.
    assert fake_netsuite.minted == 1
    assert {c.headers["authorization"] for c in fake_netsuite.api_calls} == {"Bearer bearer-1"}


def test_a_refused_assertion_fails_the_push_without_reaching_the_record_api(
    rsa_key, caplog, install
):
    # No certificate mapped: NetSuite answers the token request invalid_client.
    fake = install(_FakeNetSuite(None))
    with caplog.at_level(logging.WARNING, logger=netsuite.__name__):
        result = _run(NetSuiteAdapter(_config(_pem(rsa_key))).post_invoice(_payload()))

    assert not result.success
    assert result.message == "NetSuite post failed: HTTP 400 (invalid_request)"
    assert result.retryable is False  # a re-send cannot map the certificate
    assert fake.api_calls == []
    assert "invalid_client" in caplog.text
    # Neither the assertion nor any key material reaches the log.
    assert fake.token_forms[0]["client_assertion"] not in caplog.text
    assert "PRIVATE KEY" not in caplog.text


@pytest.mark.parametrize(
    ("body", "logged"),
    [
        ({"error": "invalid_grant"}, "invalid_grant"),
        # Not a standard code: logged as `unknown`, never echoed.
        ({"error": "eyJhbGciOiJQUzI1NiJ9.secret"}, "unknown"),
        ({"error": "made_up_code"}, "unknown"),
        ({"error": 401}, "unknown"),
        (["invalid_client"], "unknown"),
    ],
)
def test_only_a_standard_refusal_reason_reaches_the_log(body, logged):
    assert netsuite._refusal_reason(httpx.Response(400, json=body)) == logged


def test_an_unparseable_token_refusal_logs_as_such():
    assert netsuite._refusal_reason(httpx.Response(400, content=b"<html>")) == "unparseable"


def test_an_assertion_for_another_endpoint_is_refused(rsa_key):
    fake = _FakeNetSuite(_public_pem(rsa_key))
    assertion = NetSuiteAdapter(_config(_pem(rsa_key)))._client_assertion()
    with pytest.raises(FAKE.TokenError) as exc:
        FAKE.verify_token_request(
            {
                "grant_type": "client_credentials",
                "client_assertion_type": FAKE.CLIENT_ASSERTION_TYPE,
                "client_assertion": assertion,
            },
            token_url="https://other.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token",
            certificates=fake.certificates,
            now=int(time.time()),
        )
    assert exc.value.error == "invalid_grant"


# ---------------------------------------------------------------------------
# Configuration: refused before any request, naming the key, never the value
# ---------------------------------------------------------------------------


def test_token_url_is_the_accounts_own_endpoint(monkeypatch, rsa_key):
    monkeypatch.setattr(settings, "erp_netsuite_api_base", "")
    adapter = NetSuiteAdapter(_config(_pem(rsa_key)))
    assert adapter._token_url() == (
        "https://1234567-sb1.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token"
    )


def test_token_url_follows_the_operator_override(rsa_key):
    # The fake ERP gets the token request too, never real NetSuite.
    assert NetSuiteAdapter(_config(_pem(rsa_key)))._token_url() == TOKEN_URL


def test_a_pasted_key_with_its_newlines_stripped_still_signs(rsa_key, fake_netsuite):
    one_line = _pem(rsa_key).replace("\n", "")
    assert _run(NetSuiteAdapter(_config(one_line)).test_connection()) is True


@pytest.mark.parametrize(
    ("overrides", "key"),
    [
        ({"private_key": ""}, "private_key"),
        ({"certificate_id": " "}, "certificate_id"),
        ({"client_id": None}, "client_id"),
        ({"auth_method": "oauth1"}, "auth_method"),
        (
            {"private_key": "-----BEGIN PRIVATE KEY-----bm90IGEga2V5-----END PRIVATE KEY-----"},
            "private_key",
        ),
        ({"private_key": "not a pem at all"}, "private_key"),
    ],
)
def test_a_bad_oauth2_config_is_a_non_retryable_refusal_before_any_request(
    rsa_key, overrides, key, install
):
    fake = install(_FakeNetSuite(_public_pem(rsa_key)))
    cfg = _config(_pem(rsa_key), **overrides)
    result = _run(NetSuiteAdapter(cfg).post_invoice(_payload()))

    assert not result.success and result.retryable is False
    assert f"'{key}'" in result.message
    assert fake.token_forms == [] and fake.api_calls == []
    secret = str(cfg.get("private_key") or "")
    assert not secret or secret not in result.message
    assert _run(NetSuiteAdapter(cfg).test_connection()) is False


def test_a_parsed_key_is_cached_by_digest_never_by_its_plaintext_pem(rsa_key, monkeypatch):
    """The parse is memoised (~150 ms of CPU per RSA load), but the cache must
    not keep a tenant's plaintext private key alive as its lookup key."""
    import hashlib

    pem = _pem(rsa_key)
    loads: list[str] = []
    real = netsuite._load_signing_key

    def counting(value):
        loads.append(value)
        return real(value)

    monkeypatch.setattr(netsuite, "_load_signing_key", counting)
    first = netsuite._signing_key(pem)
    assert netsuite._signing_key(pem) is first
    assert len(loads) == 1  # still memoised
    assert list(netsuite._SIGNING_KEYS) == [hashlib.sha256(pem.encode()).hexdigest()]
    assert all("PRIVATE KEY" not in key for key in netsuite._SIGNING_KEYS)


def test_the_signing_key_cache_is_bounded():
    keys = [
        _pem(ec.generate_private_key(ec.SECP256R1())) for _ in range(netsuite._SIGNING_KEYS_MAX + 2)
    ]
    for pem in keys:
        netsuite._signing_key(pem)
    assert len(netsuite._SIGNING_KEYS) == netsuite._SIGNING_KEYS_MAX


def test_keys_netsuite_would_refuse_are_refused_here():
    small = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(netsuite.NetSuiteConfigError, match="3072 or 4096"):
        netsuite._signing_key(_pem(small))
    k1 = ec.generate_private_key(ec.SECP256K1())
    with pytest.raises(netsuite.NetSuiteConfigError, match="P-256"):
        netsuite._signing_key(_pem(k1))
    encrypted = (
        rsa.generate_private_key(public_exponent=65537, key_size=3072)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"passphrase"),
        )
        .decode()
    )
    with pytest.raises(netsuite.NetSuiteConfigError, match="'private_key'") as exc:
        netsuite._signing_key(encrypted)
    assert "passphrase" not in str(exc.value)


# ---------------------------------------------------------------------------
# TBA: unchanged for existing integrations
# ---------------------------------------------------------------------------


def _tba_config(**overrides) -> dict:
    cfg = {
        "type": "netsuite",
        "integration_method": "direct",
        "account_id": "1234567",
        "consumer_key": "ck",
        "consumer_secret": "cs",
        "token_id": "ti",
        "token_secret": "ts",
    }
    cfg.update(overrides)
    return cfg


@pytest.mark.parametrize("method", [None, "tba"])
def test_a_tba_block_still_signs_oauth1_and_never_asks_for_a_token(method, install):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    fake = _FakeNetSuite(None)
    fake.handle = handle  # type: ignore[method-assign]
    install(fake)
    cfg = _tba_config() if method is None else _tba_config(auth_method=method)
    assert _run(NetSuiteAdapter(cfg).test_connection()) is True
    (request,) = seen
    assert request.headers["authorization"].startswith('OAuth realm="1234567"')
    assert "oauth_signature=" in request.headers["authorization"]
    assert str(request.url) != TOKEN_URL


def test_a_tba_block_missing_a_secret_is_refused_by_name():
    result = _run(NetSuiteAdapter(_tba_config(token_secret="")).post_invoice(_payload()))
    assert not result.success and result.retryable is False
    assert "'token_secret'" in result.message


def test_tba_base_string_matches_rfc_5849s_worked_example():
    """RFC 5849 §3.4.1.1's own request and base string. The query's parameters
    are signed as parameters, never inside the base string URI — signing them
    in the URI (the old signer, audit finding N1) made NetSuite refuse every
    request that carried a query: SuiteQL paging, the externalId lookup and
    the connection test. The RFC's two form-body parameters ride in the query
    here, which normalises identically (§3.4.1.3.1)."""
    url = "http://example.com/request?b5=%3D%253D&a3=a&c%40=&a2=r%20b&c2=&a3=2%20q"
    oauth = {
        "oauth_consumer_key": "9djdj82h48djs9d2",
        "oauth_token": "kkk9d7dh3k39sjv7",
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": "137131201",
        "oauth_nonce": "7d8f3e4a",
    }
    assert netsuite._tba_base_string("post", url, oauth) == (
        "POST&http%3A%2F%2Fexample.com%2Frequest&a2%3Dr%2520b%26a3%3D2%2520q"
        "%26a3%3Da%26b5%3D%253D%25253D%26c%2540%3D%26c2%3D%26oauth_consumer_key"
        "%3D9djdj82h48djs9d2%26oauth_nonce%3D7d8f3e4a%26oauth_signature_method"
        "%3DHMAC-SHA1%26oauth_timestamp%3D137131201%26oauth_token%3Dkkk9d7dh3k39sjv7"
    )


def test_tba_base_string_uri_drops_the_default_port_and_keeps_another():
    oauth = {"oauth_nonce": "n"}
    assert netsuite._tba_base_string(
        "GET", "HTTPS://Acct.SuiteTalk.api.netsuite.com:443/services/rest/record/v1/vendor", oauth
    ).startswith(
        "GET&https%3A%2F%2Facct.suitetalk.api.netsuite.com%2Fservices%2Frest%2Frecord%2Fv1"
        "%2Fvendor&"
    )
    assert "localhost%3A12112" in netsuite._tba_base_string(
        "GET", "http://localhost:12112/netsuite/services/rest/record/v1/vendor", oauth
    )


def test_tba_signature_signs_the_query_as_parameters(monkeypatch):
    """The header's signature is the HMAC-SHA256 of the RFC base string under
    `consumer_secret&token_secret` (each encoded), for a SuiteQL page URL."""
    import base64
    import hashlib
    import hmac
    from urllib.parse import unquote

    monkeypatch.setattr(netsuite.uuid, "uuid4", lambda: type("U", (), {"hex": "nonce1"})())
    monkeypatch.setattr(netsuite.time, "time", lambda: 1700000000)
    adapter = NetSuiteAdapter(_tba_config(consumer_secret="c/s", token_secret="t+s"))
    url = (
        "https://1234567.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
        "?limit=100&offset=0"
    )
    header = adapter._auth_header("POST", url)

    expected_base = (
        "POST&https%3A%2F%2F1234567.suitetalk.api.netsuite.com%2Fservices%2Frest%2Fquery"
        "%2Fv1%2Fsuiteql&limit%3D100%26oauth_consumer_key%3Dck%26oauth_nonce%3Dnonce1"
        "%26oauth_signature_method%3DHMAC-SHA256%26oauth_timestamp%3D1700000000"
        "%26oauth_token%3Dti%26oauth_version%3D1.0%26offset%3D0"
    )
    digest = hmac.new(b"c%2Fs&t%2Bs", expected_base.encode(), hashlib.sha256).digest()
    signature = header.split('oauth_signature="')[1].split('"')[0]
    assert unquote(signature) == base64.b64encode(digest).decode()
    # Encoded per RFC 5849 §3.6: no raw `/` or `+` from the base64 survives.
    assert "/" not in signature and "+" not in signature
