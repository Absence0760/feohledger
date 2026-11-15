"""SSRF guard (app/utils/url_safety) + the logo-fetch refusal it backs."""

from __future__ import annotations

import ast
import asyncio
import socket
import ssl
import threading
from pathlib import Path
from unittest.mock import patch

import httpcore
import httpx
import pytest

from app.utils import url_safety
from app.utils.url_safety import (
    UnsafeUrlError,
    assert_public_url,
    assert_public_url_async,
    is_public_url,
    is_public_url_async,
)

# Literal-IP / scheme cases need no DNS — deterministic offline.
UNSAFE = [
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata link-local
    "http://127.0.0.1/",  # loopback
    "http://10.0.0.5/",  # RFC1918
    "http://192.168.1.1/",  # RFC1918
    "http://172.16.0.1/",  # RFC1918
    "http://0.0.0.0/",  # unspecified
    "http://[::1]/",  # IPv6 loopback
    "http://[fe80::1]/",  # IPv6 link-local
    "http://[::ffff:169.254.169.254]/",  # IPv4-mapped metadata
    # IPv6 transition forms embedding the metadata / a private IPv4 address. The
    # stdlib classes the whole prefixes as non-global (is_private), so they are
    # refused whatever they embed; pinned here so that stays true.
    "http://[2002:a9fe:a9fe::]/",  # 6to4 of 169.254.169.254
    "http://[64:ff9b::a9fe:a9fe]/",  # NAT64 well-known prefix (RFC 6052)
    "http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/",  # Teredo
    "ftp://example.com/",  # non-http scheme
    "file:///etc/passwd",  # file scheme
    "https:///nohost",  # no host
]


@pytest.mark.parametrize("url", UNSAFE)
def test_assert_rejects_unsafe(url):
    with pytest.raises(UnsafeUrlError):
        assert_public_url(url)
    assert is_public_url(url) is False


def test_assert_allows_public_ip_literal():
    # A public IP literal needs no DNS and must pass.
    assert_public_url("https://8.8.8.8/")
    assert is_public_url("https://1.1.1.1/health") is True


def test_localhost_hostname_resolves_to_loopback_and_is_rejected():
    # `localhost` resolves to 127.0.0.1 / ::1 — a real, deterministic lookup.
    assert is_public_url("http://localhost:8000/") is False


def test_is_public_url_false_for_empty():
    assert is_public_url(None) is False
    assert is_public_url("") is False


def test_unresolvable_host_is_left_to_fail_naturally():
    # A host that doesn't resolve is NOT rejected here (the connection fails at
    # request time); rejecting on a transient DNS miss would be its own bug.
    assert_public_url("https://this-host-does-not-exist.invalid/")


def test_mixed_resolution_rejected_if_any_address_internal():
    # If a hostname resolves to BOTH a public and an internal address, reject —
    # defeats a record that mixes a decoy public IP with an internal one.
    fake = [
        (None, None, None, None, ("8.8.8.8", 443)),
        (None, None, None, None, ("169.254.169.254", 443)),
    ]
    with patch("app.utils.url_safety.socket.getaddrinfo", return_value=fake):
        with pytest.raises(UnsafeUrlError):
            assert_public_url("https://sneaky.example.com/")


# ---------------------------------------------------------------------------
# The logo fetch refuses an internal URL before making any request.
# ---------------------------------------------------------------------------


def test_fetch_logo_bytes_refuses_internal_url_without_fetching():
    from app.services import branding

    # Patch httpx.Client to explode if constructed — proves the guard returns
    # before any outbound request when the logo URL points at an internal host.
    with patch("httpx.Client", side_effect=AssertionError("SSRF: must not fetch internal URL")):
        assert branding.fetch_logo_bytes("http://169.254.169.254/logo.png") is None
        assert branding.fetch_logo_bytes("http://127.0.0.1:9200/logo.png") is None


# ---------------------------------------------------------------------------
# The awaitable twin: same verdicts, and it must NEVER call blocking
# `socket.getaddrinfo` — that stalls every other request on the worker.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", UNSAFE)
@pytest.mark.asyncio
async def test_async_rejects_the_same_unsafe_urls(url):
    with pytest.raises(UnsafeUrlError):
        await assert_public_url_async(url)
    assert await is_public_url_async(url) is False


@pytest.mark.asyncio
async def test_async_allows_public_ip_literal():
    await assert_public_url_async("https://8.8.8.8/")
    assert await is_public_url_async("https://1.1.1.1/health") is True


@pytest.mark.asyncio
async def test_async_rejects_localhost_hostname():
    assert await is_public_url_async("http://localhost:8000/") is False


@pytest.mark.asyncio
async def test_async_is_public_url_false_for_empty():
    assert await is_public_url_async(None) is False
    assert await is_public_url_async("") is False


@pytest.mark.asyncio
async def test_async_unresolvable_host_is_left_to_fail_naturally():
    await assert_public_url_async("https://this-host-does-not-exist.invalid/")


@pytest.mark.asyncio
async def test_async_mixed_resolution_rejected_if_any_address_internal():
    fake = [
        (None, None, None, None, ("8.8.8.8", 443)),
        (None, None, None, None, ("169.254.169.254", 443)),
    ]

    async def _fake_getaddrinfo(*args, **kwargs):
        return fake

    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", _fake_getaddrinfo):
        with pytest.raises(UnsafeUrlError):
            await assert_public_url_async("https://sneaky.example.com/")


@pytest.mark.asyncio
async def test_async_guard_resolves_off_the_event_loop_thread():
    """The whole point of the async twin: DNS never runs ON the event loop.

    `loop.getaddrinfo` still ends in the stdlib `socket.getaddrinfo`, but in a
    worker thread. Recording the thread the lookup runs on is the exact,
    timing-free statement of the property: the sync `assert_public_url` resolves
    on the loop thread (and stalls every other request for the length of the
    lookup); the async twin must not.
    """
    loop_thread = threading.current_thread().ident
    seen: list[int | None] = []
    real = socket.getaddrinfo

    def _recording(*args, **kwargs):
        seen.append(threading.current_thread().ident)
        return real("127.0.0.1", 443, proto=socket.IPPROTO_TCP)

    with patch("app.utils.url_safety.socket.getaddrinfo", _recording):
        # Sanity: the SYNC form resolves on the caller's (loop) thread.
        assert is_public_url("https://example.com/") is False
        assert seen == [loop_thread]

        seen.clear()
        # The ASYNC form reaches the same verdict from a worker thread.
        assert await is_public_url_async("https://example.com/") is False
        assert seen and all(tid != loop_thread for tid in seen)


@pytest.mark.asyncio
async def test_chat_adapters_resolve_webhook_host_without_blocking():
    """Slack/Teams `send()` run the SSRF guard on the loop — it must not block.

    Both adapters post on every approval event. A blocking `getaddrinfo` here
    stalls every concurrent request for the length of the DNS lookup.
    """
    from app.services.chat_notification_adapters.base import ChatMessage
    from app.services.chat_notification_adapters.slack_adapter import (
        SlackChatNotificationAdapter,
    )
    from app.services.chat_notification_adapters.teams_adapter import (
        TeamsChatNotificationAdapter,
    )

    message = ChatMessage(
        event_type="invoice_approved",
        title="Invoice INV-1 was approved",
        invoice_number="INV-1",
        vendor_name="Acme",
        status="approved",
    )
    loop_thread = threading.current_thread().ident
    seen: list[int | None] = []
    real = socket.getaddrinfo

    def _recording(*args, **kwargs):
        seen.append(threading.current_thread().ident)
        return real("127.0.0.1", 443, proto=socket.IPPROTO_TCP)

    # A hostname (not a literal IP) so the guard has to resolve; it resolves to
    # loopback, so both adapters refuse before any HTTP is attempted.
    cfg = {"webhook_url": "http://hooks.internal.example/hook"}
    with patch("app.utils.url_safety.socket.getaddrinfo", _recording):
        with patch("httpx.AsyncClient", side_effect=AssertionError("must not post internally")):
            await SlackChatNotificationAdapter(cfg).send(message)
            await TeamsChatNotificationAdapter(cfg).send(message)

    assert len(seen) == 2, "both adapters must run the SSRF guard"
    assert all(tid != loop_thread for tid in seen), "DNS ran on the event loop thread"


# ---------------------------------------------------------------------------
# Pinned transports: DNS rebinding cannot pass the guard
# ---------------------------------------------------------------------------
#
# The attack: a record with a short TTL answers the guard's lookup with a public
# address and the connection's own lookup with 127.0.0.1 / 169.254.169.254. The
# pinned client resolves ONCE per connection, vets every address and dials the
# vetted IP literal, so no later lookup exists to rebind. These tests replace
# the socket layer (the inner network backend) with a recorder, so they prove
# which address is dialled without any real network.

PUBLIC_IP = "93.184.216.34"
HOST = "erp.customer.example"
_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


def _gai(ip: str, port: int):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]


class _RebindingDns:
    """`socket.getaddrinfo` stand-in: the first answer, then a different one.

    Every lookup in the process (the guard's, the pinned transport's, anything
    else's) goes through this, so ``calls`` counts them all.
    """

    def __init__(self, first: str, later: str):
        self.first, self.later, self.calls = first, later, 0

    def __call__(self, host, port, *args, **kwargs):
        self.calls += 1
        return _gai(self.first if self.calls == 1 else self.later, port or 443)


class _Log:
    def __init__(self):
        self.dialled: list[tuple[str, int]] = []
        self.written = bytearray()
        self.tls: list[tuple[ssl.SSLContext, str | None]] = []


class _AsyncStream(httpcore.AsyncNetworkStream):
    def __init__(self, log: _Log, response: bytes):
        self._log, self._pending = log, response

    async def read(self, max_bytes, timeout=None):
        out, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return out

    async def write(self, buffer, timeout=None):
        self._log.written += buffer

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._log.tls.append((ssl_context, server_hostname))
        return self

    def get_extra_info(self, info):
        return None


class _AsyncRecorder(httpcore.AsyncNetworkBackend):
    """Stands in for the real socket layer under the pinned backend."""

    def __init__(self, responses: list[bytes] | None = None):
        self.log = _Log()
        self._responses = list(responses or [_OK])

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.log.dialled.append((host, port))
        response = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return _AsyncStream(self.log, response)

    async def connect_unix_socket(self, *a, **k):
        raise AssertionError("never a unix socket")

    async def sleep(self, seconds):
        pass


@pytest.fixture
def recorder(monkeypatch):
    rec = _AsyncRecorder()
    monkeypatch.setattr(url_safety.httpcore, "AnyIOBackend", lambda: rec)
    return rec


@pytest.mark.asyncio
async def test_pinned_client_dials_the_vetted_ip_and_never_resolves_again(recorder):
    """The second answer (127.0.0.1) is never asked for: one lookup, one dial,
    to the address that lookup returned and the policy passed."""
    dns = _RebindingDns(PUBLIC_IP, "127.0.0.1")
    with patch.object(socket, "getaddrinfo", dns):
        async with url_safety.pinned_async_client(timeout=5) as client:
            resp = await client.get(f"https://{HOST}/Rest/Logon")
    assert resp.status_code == 200
    assert recorder.log.dialled == [(PUBLIC_IP, 443)]
    assert dns.calls == 1


@pytest.mark.asyncio
async def test_pinned_client_keeps_the_hostname_for_tls_and_host_header(recorder):
    """Dialling an IP must not weaken TLS: SNI and certificate verification
    still name the hostname, and so does the Host header."""
    with patch.object(socket, "getaddrinfo", _RebindingDns(PUBLIC_IP, PUBLIC_IP)):
        async with url_safety.pinned_async_client(timeout=5) as client:
            await client.get(f"https://{HOST}:20190/x")
    assert recorder.log.dialled == [(PUBLIC_IP, 20190)]
    [(ctx, server_hostname)] = recorder.log.tls
    assert server_hostname == HOST
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert f"Host: {HOST}:20190".encode() in bytes(recorder.log.written)


@pytest.mark.asyncio
async def test_guard_then_rebind_is_refused_at_connect(recorder):
    """The exact rebinding sequence: the up-front guard sees the public answer
    and passes; the connect's own lookup gets loopback and is refused before
    anything is dialled (unpinned, httpx would have connected to it)."""
    url = f"https://{HOST}/"
    with patch.object(socket, "getaddrinfo", _RebindingDns(PUBLIC_IP, "127.0.0.1")):
        await assert_public_url_async(url)  # first answer: public, passes
        async with url_safety.pinned_async_client(timeout=5) as client:
            with pytest.raises(UnsafeUrlError):
                await client.get(url)
    assert recorder.log.dialled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", ["169.254.169.254", "127.0.0.1", "10.0.0.7", "::1"])
async def test_pinned_client_refuses_any_internal_answer(recorder, internal, monkeypatch):
    async def lookup(host, port):
        return [PUBLIC_IP, internal]  # a mixed record is refused, not raced

    monkeypatch.setattr(url_safety, "_lookup_async", lookup)
    async with url_safety.pinned_async_client(timeout=5) as client:
        with pytest.raises(UnsafeUrlError):
            await client.get(f"https://{HOST}/")
    assert recorder.log.dialled == []


@pytest.mark.asyncio
async def test_pinned_client_refuses_an_internal_literal_ip(recorder):
    async with url_safety.pinned_async_client(timeout=5) as client:
        with pytest.raises(UnsafeUrlError):
            await client.get("http://169.254.169.254/latest/meta-data/")
    assert recorder.log.dialled == []


@pytest.mark.asyncio
async def test_redirect_to_another_host_is_vetted_again(monkeypatch):
    """A redirect opens a new connection, so its host goes through the same
    resolve-and-vet; an internal target is refused, not followed."""
    redirect = (
        b"HTTP/1.1 302 Found\r\nLocation: http://internal.example/\r\nContent-Length: 0\r\n\r\n"
    )
    rec = _AsyncRecorder([redirect, _OK])
    monkeypatch.setattr(url_safety.httpcore, "AnyIOBackend", lambda: rec)

    async def lookup(host, port):
        return [PUBLIC_IP] if host == HOST else ["10.0.0.9"]

    monkeypatch.setattr(url_safety, "_lookup_async", lookup)
    async with url_safety.pinned_async_client(timeout=5, follow_redirects=True) as client:
        with pytest.raises(UnsafeUrlError):
            await client.get(f"http://{HOST}/")
    assert rec.log.dialled == [(PUBLIC_IP, 80)]


@pytest.mark.asyncio
async def test_unresolvable_host_fails_as_a_connect_error(recorder, monkeypatch):
    """Same outcome the unpinned guard's fail-open rule always had: the request
    fails at connect, as a transport error the caller already handles."""

    async def lookup(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(url_safety, "_lookup_async", lookup)
    async with url_safety.pinned_async_client(timeout=5) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://{HOST}/")
    assert recorder.log.dialled == []


@pytest.mark.asyncio
async def test_allow_private_still_pins(recorder, monkeypatch):
    """`allow_private` (the local IdP outside a deployed env) skips the address
    judgement, not the pin: the dial is still the address resolved."""
    calls: list[str] = []

    async def lookup(host, port):
        calls.append(host)
        return ["127.0.0.1"]

    monkeypatch.setattr(url_safety, "_lookup_async", lookup)
    async with url_safety.pinned_async_client(allow_private=True, timeout=5) as client:
        assert (await client.get("http://keycloak.localhost:8088/x")).status_code == 200
    assert recorder.log.dialled == [("127.0.0.1", 8088)]
    assert calls == ["keycloak.localhost"]


@pytest.mark.asyncio
async def test_custom_resolver_error_propagates_unchanged(recorder):
    """A call site can raise its own error type from the resolver (commerce,
    SSO and webhooks do); httpx must hand it back untouched."""

    class _Refused(ValueError):
        pass

    async def resolve(host, port):
        raise _Refused

    async with url_safety.pinned_async_client(resolve=resolve, timeout=5) as client:
        with pytest.raises(_Refused):
            await client.get(f"https://{HOST}/")
    assert recorder.log.dialled == []


@pytest.mark.parametrize("kwarg", ["transport", "mounts", "proxy", "trust_env"])
def test_pinned_client_refuses_kwargs_that_bypass_the_pin(kwarg):
    with pytest.raises(TypeError):
        url_safety.pinned_async_client(**{kwarg: None})
    with pytest.raises(TypeError):
        url_safety.pinned_client(**{kwarg: None})


@pytest.mark.asyncio
async def test_pinned_client_ignores_proxy_environment(recorder, monkeypatch):
    """An HTTPS_PROXY in the environment would resolve the host itself."""
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
    with patch.object(socket, "getaddrinfo", _RebindingDns(PUBLIC_IP, PUBLIC_IP)):
        async with url_safety.pinned_async_client(timeout=5) as client:
            await client.get(f"https://{HOST}/")
    assert recorder.log.dialled == [(PUBLIC_IP, 443)]


# -- blocking twin (the PDF logo embed, which runs in a worker thread) -------


class _SyncStream(httpcore.NetworkStream):
    def __init__(self, log: _Log):
        self._log, self._pending = log, _OK

    def read(self, max_bytes, timeout=None):
        out, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return out

    def write(self, buffer, timeout=None):
        self._log.written += buffer

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._log.tls.append((ssl_context, server_hostname))
        return self

    def get_extra_info(self, info):
        return None


class _SyncRecorder(httpcore.NetworkBackend):
    def __init__(self):
        self.log = _Log()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.log.dialled.append((host, port))
        return _SyncStream(self.log)

    def connect_unix_socket(self, *a, **k):
        raise AssertionError("never a unix socket")

    def sleep(self, seconds):
        pass


def test_sync_pinned_client_dials_the_vetted_ip(monkeypatch):
    rec = _SyncRecorder()
    monkeypatch.setattr(url_safety.httpcore, "SyncBackend", lambda: rec)
    dns = _RebindingDns(PUBLIC_IP, "169.254.169.254")
    with patch.object(socket, "getaddrinfo", dns):
        with url_safety.pinned_client(timeout=5) as client:
            assert client.get(f"https://{HOST}/logo.png").status_code == 200
    assert rec.log.dialled == [(PUBLIC_IP, 443)]
    assert dns.calls == 1
    assert rec.log.tls[0][1] == HOST


def test_sync_guard_then_rebind_is_refused_at_connect(monkeypatch):
    rec = _SyncRecorder()
    monkeypatch.setattr(url_safety.httpcore, "SyncBackend", lambda: rec)
    with patch.object(socket, "getaddrinfo", _RebindingDns(PUBLIC_IP, "169.254.169.254")):
        assert is_public_url(f"https://{HOST}/logo.png")
        with url_safety.pinned_client(timeout=5) as client:
            with pytest.raises(UnsafeUrlError):
                client.get(f"https://{HOST}/logo.png")
    assert rec.log.dialled == []


def test_logo_fetch_rebind_falls_back_without_dialling(monkeypatch):
    """End to end through `fetch_logo_bytes`: the check passes, the rebind is
    caught at connect, and the PDF falls back to the text header."""
    from app.services import branding

    rec = _SyncRecorder()
    monkeypatch.setattr(url_safety.httpcore, "SyncBackend", lambda: rec)
    with patch.object(socket, "getaddrinfo", _RebindingDns(PUBLIC_IP, "169.254.169.254")):
        assert branding.fetch_logo_bytes(f"https://{HOST}/logo.png") is None
    assert rec.log.dialled == []


# -- drift guards ------------------------------------------------------------

_APP = Path(__file__).resolve().parents[1] / "app"

#: Modules that SSRF-check an admin- or customer-supplied URL yet still build a
#: plain httpx client, with how many such calls and why. Any other plain client
#: in a guarded module fails, so a new call site there has to adopt
#: ``url_safety.pinned_async_client`` or be argued for here.
_PLAIN_CLIENT_ALLOWED: dict[str, tuple[int, str]] = {
    "services/erp_adapters/dynamics_365_bc.py": (
        8,
        "base_url is allowlisted to api.businesscentral.dynamics.com and the token "
        "host is Microsoft's, so a rebind needs control of Microsoft's DNS",
    ),
    "services/erp_adapters/sage_accounting_za.py": (
        1,
        "base_url is allowlisted to Sage's API host; same reasoning as BC",
    ),
    "services/erp_adapters/syspro.py": (
        1,
        "only the trusted operator-override branch of _client (fake-erp)",
    ),
    "services/commerce_adapters/base.py": (
        2,
        "the test transport override and the operator override (fake-commerce)",
    ),
    "services/peppol_adapters/as4_gateway.py": (
        1,
        "only the trusted operator branch of _client (FEOH_PEPPOL_GATEWAY_URL)",
    ),
    "services/extraction_adapters/ollama.py": (
        1,
        "only the trusted operator branch of _client (FEOH_OLLAMA_BASE_URL)",
    ),
    "services/erp_adapters/netsuite.py": (
        1,
        "only the trusted operator-override branch of _client (fake-erp)",
    ),
}

#: Modules that send to a host a TENANT can configure, with the per-org config
#: key that host is read from. Each must stay in the guarded set above (import
#: url_safety) and route that key's value through ``org_host_client`` — the URL
#: the factory checks must trace back to the key, so a call that vets a constant
#: or the operator setting while the request goes to the per-org host fails.
_ORG_HOST_MODULES = {
    "services/peppol_adapters/as4_gateway.py": "gateway_url",  # settings.peppol
    "services/extraction_adapters/ollama.py": "base_url",  # settings.extraction (BYOK)
    "services/chat_notification_adapters/slack_adapter.py": "webhook_url",  # chat
    "services/chat_notification_adapters/teams_adapter.py": "webhook_url",  # chat
    "services/enrichment_adapters/clearbit.py": "base_url",  # settings.enrichment
    "services/enrichment_adapters/dun_bradstreet.py": "base_url",  # settings.enrichment
}

#: Modules that send to a per-org host through their OWN check + pinned client,
#: because they need something ``org_host_client`` does not offer: a resolver
#: raising the caller's error type (SSO, commerce, NetSuite), the stricter
#: outbound-webhook address rule, a blocking client in a worker thread (the PDF
#: logo), or a check and a client built in different methods (SYSPRO). Each must
#: still build a pinned client, so the guard is the same and only the spelling differs.
_ORG_HOST_BESPOKE = {
    "services/sso.py": "settings.sso discovery / JWKS / token URLs",
    "services/branding.py": "settings.company.logo_url (blocking, worker thread)",
    "services/webhooks/delivery.py": "an outbound webhook subscription's URL",
    "services/commerce_adapters/base.py": "store_url / shop domain / prefix hosts",
    "services/erp_adapters/syspro.py": "settings.erp.base_url (customer-hosted)",
    "services/erp_adapters/netsuite.py": "settings.erp.account_id -> <id>.suitetalk / .restlets",
}

#: Modules that READ a per-org host but send nothing to it yet (skeletons, or
#: a step that records instead of sending). The day one builds an HTTP client
#: it must route that host through ``org_host_client`` and move to
#: ``_ORG_HOST_MODULES`` — this list makes that a failing test, not a review
#: catch.
_ORG_HOST_NOT_YET_SENT = {
    "services/qms_adapters/generic_qms.py": "settings.qms.base_url",
    "services/tax_rate_adapters/avalara.py": "tax config base_url",
    "services/tax_rate_adapters/taxjar.py": "tax config base_url",
    "services/workflow_builder.py": "a workflow webhook step's url (recorded, not sent)",
}


def test_org_configurable_hosts_go_through_org_host_client():
    guarded = dict(_guarded_modules())
    for rel, key in _ORG_HOST_MODULES.items():
        assert rel in guarded, f"{rel} no longer imports url_safety"
        tree = guarded[rel]
        calls = [
            (node, fn, cls)
            for node, fn, cls in _calls_in_context(tree)
            if _url_safety_name(tree, node.func) == "org_host_client"
        ]
        assert calls, f"{rel} does not route its per-org host through org_host_client"
        for call, fn, cls in calls:
            url = call.args[0] if call.args else _kwarg(call, "url")
            assert url is not None, f"{rel}:{call.lineno} passes org_host_client no URL"
            sources = _config_keys(url, fn, cls)
            assert key in sources, (
                f"{rel}:{call.lineno}: the URL org_host_client checks does not trace to "
                f"the per-org {key!r} (traces to {sorted(sources) or 'nothing per-org'})"
            )


def test_config_key_tracer_follows_only_the_per_org_value():
    """The tracer behind the adopter check: it must follow the shapes the
    adopters use, and must not credit a constant or the operator setting."""

    def keys(src: str) -> set[str]:
        tree = ast.parse(src)
        found: set[str] = set()
        for call, fn, cls in _calls_in_context(tree):
            if _url_safety_name(tree, call.func) == "org_host_client":
                found |= _config_keys(call.args[0], fn, cls)
        return found

    imp = "from app.utils.url_safety import org_host_client\n"
    traced = {
        # attribute set in __init__ from the config, with a constant fallback
        "class A:\n def __init__(self, config):\n"
        "  self.u = (config or {}).get('webhook_url') or ''\n"
        " async def f(self):\n  return await org_host_client(self.u, allow_private=False)\n": {
            "webhook_url"
        },
        # local assigned from a helper method returning the stripped config value
        "class A:\n def _org(self):\n  return str(self.config.get('base_url') or '').strip()\n"
        " async def f(self):\n  u = self._org()\n"
        "  return await org_host_client(u, allow_private=False)\n": {"base_url"},
        # the module alias form, with a subscript read
        "from app.utils import url_safety as us\n"
        "async def f(cfg):\n"
        "  return await us.org_host_client(cfg['base_url'], allow_private=False)\n": {"base_url"},
    }
    for src, expected in traced.items():
        assert keys(imp + src) == expected, src
    untraced = (
        "BASE = 'https://api.example.com'\nclass A:\n def __init__(self, config):\n"
        "  self.u = config.get('base_url')\n"
        " async def f(self):\n  return await org_host_client(BASE, allow_private=False)\n",
        "from app.config import settings\nasync def f(config):\n"
        "  return await org_host_client(settings.ollama_base_url, allow_private=False)\n",
        "async def f(response):\n"
        "  return await org_host_client(response.get('base_url'), allow_private=False)\n",
    )
    for src in untraced:
        assert keys(imp + src) == set(), src


_PINNED_FACTORIES = {"org_host_client", "pinned_async_client", "pinned_client"}
_HTTP_CLIENT_NAMES = _PINNED_FACTORIES | {"AsyncClient", "Client", "request", "stream"}


def _called_names(tree) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def test_bespoke_per_org_hosts_still_build_a_pinned_client():
    """A bespoke module's pinned client takes no URL — it vets whatever it is
    asked to dial — so the trace that matters is the other way round: it must
    stay in the guarded set, where the plain-client count fails any unpinned
    client it builds for that host."""
    guarded = dict(_guarded_modules())
    for rel in _ORG_HOST_BESPOKE:
        assert rel in guarded, f"{rel} no longer imports url_safety"
        tree = guarded[rel]
        factories = {
            _url_safety_name(tree, n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)
        }
        assert factories & _PINNED_FACTORIES, f"{rel} sends to a per-org host unpinned"


def test_per_org_hosts_not_yet_sent_build_no_http_client():
    for rel, field in _ORG_HOST_NOT_YET_SENT.items():
        tree = ast.parse((_APP / rel).read_text())
        imported = _imported_client_modules(tree)
        built = _called_names(tree) & _HTTP_CLIENT_NAMES
        assert not imported and not built, (
            f"{rel} now sends to {field}: route it through url_safety.org_host_client "
            "and move the module to _ORG_HOST_MODULES (decisions §339)"
        )


#: Modules that import an HTTP / SMTP client but send only to an OPERATOR host:
#: a vendor constant, a ``FEOH_*`` setting, or a per-org value that merely picks
#: one of the vendor's own constant hosts (``sandbox``, ``environment``). None
#: reads a destination from per-org settings. A module that starts to belongs in
#: ``_ORG_HOST_MODULES`` instead.
_OPERATOR_HOST_MODULES = {
    "services/assistant/claude_adapter.py": "Anthropic constant",
    "services/assistant/ollama_adapter.py": "FEOH_OLLAMA_BASE_URL (orchestrator config)",
    "services/audit_summary.py": "Anthropic constant",
    "services/billing_adapters/stripe_billing.py": "FEOH_BILLING_STRIPE_API_BASE",
    "services/card_adapters/lithic.py": "Lithic constants; `sandbox` picks one",
    "services/card_adapters/nium.py": "Nium constants; `sandbox` picks one",
    "services/commerce_adapters/cin7.py": "Cin7 constant, via the pinned commerce client",
    "services/commerce_adapters/lightspeed.py": "validated <prefix>.retail.lightspeed.app",
    "services/commerce_adapters/shopify.py": "validated <handle>.myshopify.com",
    "services/commerce_adapters/square.py": "Square constants; `environment` picks one",
    "services/commerce_adapters/woocommerce.py": "store_url, sent via commerce base (bespoke)",
    "services/email_adapters/smtp_adapter.py": "FEOH_SMTP_* (email dispatcher config)",
    "services/embedding_adapters/openai_adapter.py": "OpenAI constant",
    "services/erp_adapters/blackbaud_fe_nxt.py": "SKY constant / FEOH_ERP_BLACKBAUD_*",
    "services/erp_adapters/merge_dev.py": "FEOH_ERP_MERGE_API_BASE",
    "services/erp_adapters/oauth_base.py": "the OAuth subclasses' constant / FEOH_* hosts",
    "services/erp_adapters/quickbooks_online.py": "Intuit constants; `environment` picks one",
    "services/erp_adapters/sage_accounting.py": "Sage constant / FEOH_ERP_SAGE_*",
    "services/erp_adapters/sage_intacct.py": "Intacct constant / FEOH_ERP_INTACCT_API_BASE",
    "services/erp_adapters/xero.py": "Xero constant / FEOH_ERP_XERO_API_BASE",
    "services/erp_call_budget.py": "httpx types only; sends nothing",
    "services/erp_oauth.py": "provider-spec constants / FEOH_* token URL overrides",
    "services/erp_status_sync.py": "httpx types only; sends nothing",
    "services/exception_agents/llm_rationale.py": "Anthropic constant",
    "services/extraction_adapters/claude_vision.py": "Anthropic constant",
    "services/extraction_adapters/openai_vision.py": "OpenAI constant",
    "services/fx_adapters/openexchangerates.py": "OXR constant",
    "services/llm_fraud_detection.py": "Anthropic constant",
    "services/payment_adapters/checkeeper.py": "Checkeeper constants; `sandbox` picks one",
    "services/payment_adapters/column.py": "Column constants; `sandbox` picks one",
    "services/payment_adapters/dwolla.py": "Dwolla constants; `sandbox` picks one",
    "services/payment_adapters/increase.py": "Increase constants; `sandbox` picks one",
    "services/payment_adapters/modern_treasury.py": "Modern Treasury constant",
    "services/payment_adapters/stripe_treasury.py": "Stripe constant / FEOH_STRIPE_API_BASE",
    "services/sanctions_adapters/complyadvantage.py": "ComplyAdvantage constant",
    "services/sanctions_adapters/dowjones.py": "Dow Jones constant",
    "services/sanctions_adapters/refinitiv.py": "World-Check constant",
    "services/tax_filing_adapters/tax1099_adapter.py": "Tax1099 constant",
    "services/tin_validation_adapters/tax1099_adapter.py": "Tax1099 constant",
    "utils/hcaptcha.py": "hCaptcha constant",
}

_CLIENT_MODULES = {"httpx", "aiohttp", "smtplib", "urllib.request", "requests"}


def _imported_client_modules(tree) -> set[str]:
    found = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            found |= {a.name for a in n.names if a.name in _CLIENT_MODULES}
        elif isinstance(n, ast.ImportFrom):
            if n.module in _CLIENT_MODULES:
                found.add(n.module)
            elif n.module == "urllib" and any(a.name == "request" for a in n.names):
                found.add("urllib.request")
    return found


def test_every_module_with_an_http_client_classifies_its_host():
    """Discovery, not inventory: a module that imports a client library must say
    where its host comes from, so a new adapter reading ``cfg["base_url"]``
    into a plain client cannot pass silently (the Stripe ``api_base`` shape)."""
    classified = (
        set(_PLAIN_CLIENT_ALLOWED)
        | set(_ORG_HOST_MODULES)
        | set(_ORG_HOST_BESPOKE)
        | set(_OPERATOR_HOST_MODULES)
        | {"utils/url_safety.py"}
    )
    unclassified = sorted(
        rel
        for path in _APP.rglob("*.py")
        if (rel := path.relative_to(_APP).as_posix()) not in classified
        and _imported_client_modules(ast.parse(path.read_text()))
    )
    assert unclassified == [], (
        "these modules build an outbound client but say nothing about its host: "
        "classify each in _OPERATOR_HOST_MODULES, _ORG_HOST_MODULES (org_host_client), "
        "_ORG_HOST_BESPOKE or _PLAIN_CLIENT_ALLOWED"
    )
    stale = sorted(rel for rel in _OPERATOR_HOST_MODULES if not (_APP / rel).exists())
    assert stale == []


#: Per-org hosts with no local emulator: the dev loopback allowance must stay off.
_ORG_HOST_NO_LOOPBACK = (
    "services/chat_notification_adapters/slack_adapter.py",
    "services/chat_notification_adapters/teams_adapter.py",
    "services/enrichment_adapters/clearbit.py",
    "services/enrichment_adapters/dun_bradstreet.py",
)


def test_per_org_hosts_without_an_emulator_never_allow_loopback():
    for rel in _ORG_HOST_NO_LOOPBACK:
        calls = [
            n
            for n in ast.walk(ast.parse((_APP / rel).read_text()))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "org_host_client"
        ]
        assert calls, rel
        for call in calls:
            kw = {k.arg: k.value for k in call.keywords}
            value = kw.get("allow_private")
            assert isinstance(value, ast.Constant) and value.value is False, (
                f"{rel}: allow_private must be the literal False (no local emulator)"
            )


def test_per_org_host_inventories_do_not_overlap():
    lists = [
        set(_ORG_HOST_MODULES),
        set(_ORG_HOST_BESPOKE),
        set(_ORG_HOST_NOT_YET_SENT),
        set(_OPERATOR_HOST_MODULES),
    ]
    for i, a in enumerate(lists):
        for b in lists[i + 1 :]:
            assert not a & b, a & b


def _guarded_modules():
    for path in sorted(_APP.rglob("*.py")):
        rel = path.relative_to(_APP).as_posix()
        if rel == "utils/url_safety.py":
            continue
        src = path.read_text()
        if "url_safety" in src or "url_guard" in src:
            yield rel, ast.parse(src)


def _module_bindings(tree, module: str) -> tuple[set[str], dict[str, str]]:
    """How ``tree`` can name ``module`` and its attributes, through any import:
    the local names bound to the module itself (``import httpx``, ``import httpx
    as h``, ``from app.utils import url_safety as us``) and a map of local name →
    attribute for ``from module import X [as Y]``. Imports inside functions count
    too, module-wide: an over-approximation, which makes the plain-client count
    stricter, never looser."""
    parent, _, leaf = module.rpartition(".")
    modules: set[str] = set()
    names: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == module:
                    # ``import a.b.c`` binds ``a``; only an alias binds the leaf.
                    modules.add(a.asname or a.name)
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            for a in n.names:
                if n.module == module:
                    names[a.asname or a.name] = a.name
                elif parent and n.module == parent and a.name == leaf:
                    modules.add(a.asname or a.name)
    return modules, names


def _resolved_attr(tree, func, module: str) -> str | None:
    """The ``module`` attribute a call's ``func`` names, whatever the import
    spelling, or None when it names something else."""
    modules, names = _module_bindings(tree, module)
    if isinstance(func, ast.Name):
        return names.get(func.id)
    if isinstance(func, ast.Attribute):
        value = func.value
        dotted = []
        while isinstance(value, ast.Attribute):
            dotted.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            path = ".".join([value.id, *reversed(dotted)])
            if path in modules or path == module:
                return func.attr
    return None


def _url_safety_name(tree, func) -> str | None:
    return _resolved_attr(tree, func, "app.utils.url_safety")


#: Every httpx entry point that builds an UNPINNED client: the two constructors,
#: and the module-level request helpers, which each open a plain ``httpx.Client``.
_HTTPX_PLAIN_ENTRY_POINTS = {
    "AsyncClient",
    "Client",
    "request",
    "stream",
    "get",
    "options",
    "head",
    "post",
    "put",
    "patch",
    "delete",
}


def _plain_client_calls(tree) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _resolved_attr(tree, node.func, "httpx") in _HTTPX_PLAIN_ENTRY_POINTS
    )


def test_plain_client_count_resolves_every_import_spelling():
    """Each spelling of a plain httpx client counts, not only the literal
    ``httpx.AsyncClient(...)``, so an alias cannot slip a client past the count
    guard below. Names that merely resemble httpx's do not."""
    counted = (
        "import httpx\nhttpx.AsyncClient()\n",
        "import httpx\nhttpx.Client()\n",
        "import httpx as h\nh.AsyncClient()\n",
        "import httpx as h\nh.Client()\n",
        "from httpx import AsyncClient\nAsyncClient()\n",
        "from httpx import AsyncClient as X\nX()\n",
        "from httpx import Client\nClient()\n",
        "from httpx import Client as C\nC()\n",
        "def f():\n    from httpx import AsyncClient as X\n    return X()\n",
        "import httpx\nhttpx.get('https://example.com')\n",
        "from httpx import post\npost('https://example.com')\n",
        "import httpx as h\nh.stream('GET', 'https://example.com')\n",
    )
    for src in counted:
        assert _plain_client_calls(ast.parse(src)) == 1, src
    not_counted = (
        "from app.utils.url_safety import pinned_async_client\npinned_async_client()\n",
        "import httpx\nhttpx.Timeout(5)\n",
        "import httpx\nclient.get('https://example.com')\n",
        "from somewhere import AsyncClient\nAsyncClient()\n",
        "import httpx\nAsyncClient()\n",
        "import httpx\ndef f(c: httpx.AsyncClient) -> httpx.Client: ...\n",
    )
    for src in not_counted:
        assert _plain_client_calls(ast.parse(src)) == 0, src


def _calls_in_context(tree):
    """Every call in ``tree`` with its innermost enclosing function and class."""

    def visit(node, fn, cls):
        for child in ast.iter_child_nodes(node):
            child_fn, child_cls = fn, cls
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                child_fn = child
            elif isinstance(child, ast.ClassDef):
                child_fn, child_cls = None, child
            if isinstance(child, ast.Call):
                yield child, fn, cls
            yield from visit(child, child_fn, child_cls)

    yield from visit(tree, None, None)


def _kwarg(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_config_root(expr) -> bool:
    """The adapter's per-org config dict: ``config`` / ``cfg``, ``self.config``,
    or either behind an ``or {}`` default."""
    if isinstance(expr, ast.Name):
        return expr.id in ("config", "cfg")
    if isinstance(expr, ast.Attribute):
        return (
            expr.attr == "config" and isinstance(expr.value, ast.Name) and expr.value.id == "self"
        )
    if isinstance(expr, ast.BoolOp):
        return any(_is_config_root(v) for v in expr.values)
    return False


def _assigned_values(scope, target):
    """The values assigned to ``target`` (a local name, or ``self.attr``) in scope."""

    def matches(t):
        if isinstance(target, ast.Name):
            return isinstance(t, ast.Name) and t.id == target.id
        return (
            isinstance(t, ast.Attribute)
            and t.attr == target.attr
            and isinstance(t.value, ast.Name)
            and t.value.id == "self"
        )

    for n in ast.walk(scope):
        if isinstance(n, ast.Assign) and any(matches(t) for t in n.targets):
            yield n.value
        elif isinstance(n, ast.AnnAssign | ast.NamedExpr) and n.value and matches(n.target):
            yield n.value


def _config_keys(expr, fn, cls, depth: int = 0) -> set[str]:
    """The per-org config keys a URL expression is read from: through locals,
    ``self`` attributes, ``self`` helper methods' returns, ``or`` fallbacks,
    f-strings and string methods. A constant or ``settings.*`` contributes none."""
    if depth > 10:
        return set()

    def again(e, f=fn):
        return _config_keys(e, f, cls, depth + 1)

    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Attribute):
            if (
                func.attr == "get"
                and _is_config_root(func.value)
                and expr.args
                and isinstance(expr.args[0], ast.Constant)
                and isinstance(expr.args[0].value, str)
            ):
                return {expr.args[0].value}
            if isinstance(func.value, ast.Name) and func.value.id == "self" and cls is not None:
                keys: set[str] = set()
                for m in cls.body:
                    if not isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef):
                        continue
                    if m.name == func.attr:
                        for r in ast.walk(m):
                            if isinstance(r, ast.Return) and r.value is not None:
                                keys |= again(r.value, m)
                return keys
            return again(func.value)  # "...".strip() / .rstrip("/")
        if isinstance(func, ast.Name) and func.id == "str" and expr.args:
            return again(expr.args[0])
        return set()
    if isinstance(expr, ast.Subscript):
        if (
            _is_config_root(expr.value)
            and isinstance(expr.slice, ast.Constant)
            and isinstance(expr.slice.value, str)
        ):
            return {expr.slice.value}
        return set()
    if isinstance(expr, ast.BoolOp):
        return set().union(*(again(v) for v in expr.values))
    if isinstance(expr, ast.IfExp):
        return again(expr.body) | again(expr.orelse)
    if isinstance(expr, ast.JoinedStr):
        return set().union(
            set(), *(again(v.value) for v in expr.values if isinstance(v, ast.FormattedValue))
        )
    if isinstance(expr, ast.Name) and fn is not None:
        return set().union(set(), *(again(v) for v in _assigned_values(fn, expr)))
    if (
        isinstance(expr, ast.Attribute)
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "self"
        and cls is not None
    ):
        keys = set()
        for m in cls.body:
            if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef):
                keys |= set().union(set(), *(again(v, m) for v in _assigned_values(m, expr)))
        return keys
    return set()


def test_guarded_modules_send_through_the_pinned_client():
    trees = dict(_guarded_modules())
    for rel in _PLAIN_CLIENT_ALLOWED:
        trees.setdefault(rel, ast.parse((_APP / rel).read_text()))
    seen = {rel: n for rel, tree in trees.items() if (n := _plain_client_calls(tree))}
    expected = {rel: count for rel, (count, _why) in _PLAIN_CLIENT_ALLOWED.items()}
    assert seen == expected, (
        "a module that SSRF-checks a URL builds a plain httpx client: use "
        "url_safety.pinned_async_client, or justify it in _PLAIN_CLIENT_ALLOWED"
    )


def test_blocking_pinned_client_is_never_built_in_a_coroutine():
    """`pinned_client` resolves and connects synchronously, so inside an
    `async def` it would stall the event loop (project invariant)."""
    offenders = []
    for path in sorted(_APP.rglob("*.py")):
        rel = path.relative_to(_APP).as_posix()
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and (
                    _url_safety_name(tree, node.func) == "pinned_client"
                    or (
                        rel == "utils/url_safety.py"
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "pinned_client"
                    )
                ):
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


# ---------------------------------------------------------------------------
# Hosts an organisation configures: PEPPOL gateway_url, Ollama base_url
# ---------------------------------------------------------------------------
#
# A per-org URL is untrusted (any tenant admin writes it): in a deployed
# environment it is checked up front and pinned at connect. Outside one,
# loopback is allowed (local Ollama on :11435, a local Access Point emulator)
# but still pinned. The operator's process-level setting is trusted and keeps a
# plain client, as every other operator override does (decisions §334).

_TAGS_BODY = b'{"models": [{"name": "llama3.2-vision:11b"}]}'
_TAGS_OK = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
    + str(len(_TAGS_BODY)).encode()
    + b"\r\n\r\n"
    + _TAGS_BODY
)


@pytest.fixture
def deployed(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "environment", "production")
    assert settings.is_deployed


@pytest.fixture
def not_deployed(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "environment", "development")
    assert not settings.is_deployed


def _transmission():
    from app.services.peppol_adapters.base import ParticipantId, TransmissionRequest

    return TransmissionRequest(
        sender=ParticipantId("9930", "DE111"),
        receiver=ParticipantId("9930", "DE222"),
        doc_type_id="d",
        process_id="p",
        payload=b"<Invoice/>",
        business_message_id="m-1",
    )


@pytest.mark.asyncio
async def test_org_host_client_refuses_an_internal_url_before_any_io(recorder, monkeypatch):
    async def lookup(host, port):  # the pinned transport must not even resolve
        raise AssertionError("resolved after the up-front refusal")

    monkeypatch.setattr(url_safety, "_lookup_async", lookup)
    with pytest.raises(UnsafeUrlError):
        await url_safety.org_host_client("http://169.254.169.254/", allow_private=False)
    assert recorder.log.dialled == []


@pytest.mark.asyncio
async def test_org_host_client_is_pinned_against_a_rebind(recorder):
    url = f"https://{HOST}/"
    with patch.object(socket, "getaddrinfo", _RebindingDns(PUBLIC_IP, "127.0.0.1")):
        client = await url_safety.org_host_client(url, allow_private=False, timeout=5)
        async with client:
            with pytest.raises(UnsafeUrlError):
                await client.get(url)
    assert recorder.log.dialled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", ["http://169.254.169.254", "http://10.0.0.7:8080"])
async def test_peppol_org_gateway_url_refused_when_deployed(recorder, deployed, internal):
    from app.services.peppol_adapters.as4_gateway import (
        GATEWAY_URL_NOT_PUBLIC,
        AS4GatewayAdapter,
    )
    from app.services.peppol_adapters.base import ParticipantId

    adapter = AS4GatewayAdapter({"gateway_url": internal, "api_key": "k"})
    assert adapter.gateway_url_from_org
    result = await adapter.send(_transmission())
    assert (result.success, result.failure_reason) == (False, GATEWAY_URL_NOT_PUBLIC)
    cap = await adapter.resolve_participant(ParticipantId("9930", "DE222"))
    assert cap.unregistered_reason == GATEWAY_URL_NOT_PUBLIC
    assert await adapter.test_connection() is False
    assert recorder.log.dialled == []  # the API key never left the process


@pytest.mark.asyncio
async def test_peppol_org_gateway_url_on_loopback_allowed_outside_deployed(
    recorder, not_deployed, monkeypatch
):
    """A local Access Point emulator works in dev, through the pinned client."""
    from app.services.peppol_adapters.as4_gateway import AS4GatewayAdapter

    async def lookup(host, port):
        return ["127.0.0.1"]

    monkeypatch.setattr(url_safety, "_lookup_async", lookup)
    adapter = AS4GatewayAdapter({"gateway_url": "http://peppol.localhost:9100", "api_key": "k"})
    assert await adapter.test_connection() is True
    assert recorder.log.dialled == [("127.0.0.1", 9100)]


@pytest.mark.asyncio
async def test_peppol_operator_gateway_url_keeps_a_plain_client(deployed, monkeypatch):
    """FEOH_PEPPOL_GATEWAY_URL is the operator's, so it is not judged."""
    from app.config import settings
    from app.services.peppol_adapters.as4_gateway import AS4GatewayAdapter

    monkeypatch.setattr(settings, "peppol_gateway_url", "http://127.0.0.1:9100")
    adapter = AS4GatewayAdapter({"api_key": "k"})
    assert not adapter.gateway_url_from_org
    client = await adapter._client()
    async with client:
        assert not isinstance(client._transport, url_safety.PinnedAsyncTransport)


@pytest.mark.asyncio
async def test_ollama_org_base_url_refused_when_deployed(recorder, deployed):
    from app.services.extraction_adapters.ollama import UNSAFE_BASE_URL_ERROR, OllamaAdapter

    adapter = OllamaAdapter({"base_url": "http://169.254.169.254"})
    result = await adapter.extract(file_bytes=b"img", mime_type="image/png")
    assert (result.success, result.error) == (False, UNSAFE_BASE_URL_ERROR)
    statement = await adapter.extract_statement(file_bytes=b"img", mime_type="image/png")
    assert statement.error == UNSAFE_BASE_URL_ERROR
    assert await adapter.test_connection() is False
    assert recorder.log.dialled == []


@pytest.mark.asyncio
async def test_ollama_org_base_url_on_loopback_allowed_outside_deployed(not_deployed, monkeypatch):
    """The Compose model server on :11435 keeps working in dev, pinned."""
    from app.services.extraction_adapters.ollama import OllamaAdapter

    rec = _AsyncRecorder([_TAGS_OK])
    monkeypatch.setattr(url_safety.httpcore, "AnyIOBackend", lambda: rec)

    async def lookup(host, port):
        return ["127.0.0.1"]

    monkeypatch.setattr(url_safety, "_lookup_async", lookup)
    adapter = OllamaAdapter({"base_url": "http://localhost:11435"})
    assert await adapter.test_connection() is True
    assert rec.log.dialled == [("127.0.0.1", 11435)]


@pytest.mark.asyncio
async def test_ollama_operator_base_url_keeps_a_plain_client(deployed, monkeypatch):
    """Platform mode never carries a per-org base_url; FEOH_OLLAMA_BASE_URL is
    the operator's and is not judged."""
    from app.config import settings
    from app.services.extraction_adapters.ollama import OllamaAdapter

    monkeypatch.setattr(settings, "ollama_base_url", "http://127.0.0.1:11435")
    adapter = OllamaAdapter({})
    assert adapter._base_url() == "http://127.0.0.1:11435"
    # A blank per-org value is "not set", the same as the PEPPOL adapter's.
    assert OllamaAdapter({"base_url": "  "})._base_url() == "http://127.0.0.1:11435"
    client = await adapter._client(5)
    async with client:
        assert not isinstance(client._transport, url_safety.PinnedAsyncTransport)


# ---------------------------------------------------------------------------
# Slack / Teams webhook_url and the Clearbit / D&B base_url: per-org hosts
# through org_host_client (no dev loopback allowance — nothing local to reach)
# ---------------------------------------------------------------------------


def _chat_message():
    from app.services.chat_notification_adapters.base import ChatMessage

    return ChatMessage(
        event_type="invoice_approved",
        title="Invoice INV-1 was approved",
        invoice_number="INV-1",
        vendor_name="Acme",
        status="approved",
    )


def _chat_adapters():
    from app.services.chat_notification_adapters.slack_adapter import (
        SlackChatNotificationAdapter,
    )
    from app.services.chat_notification_adapters.teams_adapter import (
        TeamsChatNotificationAdapter,
    )

    return [SlackChatNotificationAdapter, TeamsChatNotificationAdapter]


def _enrichment_adapters():
    from app.services.enrichment_adapters.clearbit import ClearbitAdapter
    from app.services.enrichment_adapters.dun_bradstreet import DunBradstreetAdapter

    return [ClearbitAdapter, DunBradstreetAdapter]


@pytest.mark.asyncio
@pytest.mark.parametrize("idx", [0, 1])
@pytest.mark.parametrize("env", ["deployed", "not_deployed"])
async def test_chat_webhook_url_internal_is_skipped_without_dialling(recorder, request, idx, env):
    """Loopback is refused even outside a deployed environment."""
    request.getfixturevalue(env)
    cls = _chat_adapters()[idx]
    await cls({"webhook_url": "http://127.0.0.1:9000/hook"}).send(_chat_message())
    assert recorder.log.dialled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("idx", [0, 1])
async def test_chat_webhook_url_rebind_is_refused_at_connect(recorder, idx):
    cls = _chat_adapters()[idx]
    with patch.object(socket, "getaddrinfo", _RebindingDns(PUBLIC_IP, "169.254.169.254")):
        await cls({"webhook_url": f"https://{HOST}/hook"}).send(_chat_message())
    assert recorder.log.dialled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("idx", [0, 1])
@pytest.mark.parametrize("env", ["deployed", "not_deployed"])
async def test_enrichment_base_url_internal_is_refused_without_dialling(
    recorder, request, idx, env
):
    from app.services.enrichment_adapters.base import VendorEnrichmentQuery

    request.getfixturevalue(env)
    cls = _enrichment_adapters()[idx]
    adapter = cls({"api_key": "k", "base_url": "http://169.254.169.254"})
    with pytest.raises(UnsafeUrlError):
        await adapter.enrich_vendor(VendorEnrichmentQuery(vendor_name="Acme", domain="acme.com"))
    assert await adapter.test_connection() is False
    assert recorder.log.dialled == []  # the API key never left the process


@pytest.mark.asyncio
@pytest.mark.parametrize("idx", [0, 1])
async def test_enrichment_base_url_rebind_is_refused_at_connect(recorder, idx):
    from app.services.enrichment_adapters.base import VendorEnrichmentQuery

    cls = _enrichment_adapters()[idx]
    adapter = cls({"api_key": "k", "base_url": f"https://{HOST}"})
    with patch.object(socket, "getaddrinfo", _RebindingDns(PUBLIC_IP, "127.0.0.1")):
        with pytest.raises(UnsafeUrlError):
            await adapter.enrich_vendor(
                VendorEnrichmentQuery(vendor_name="Acme", domain="acme.com")
            )
    assert recorder.log.dialled == []
