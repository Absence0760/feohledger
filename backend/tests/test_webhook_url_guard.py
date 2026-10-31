"""SSRF guard on outbound-webhook target URLs (issue #171).

Covers:
  * the pure guard — literal loopback / RFC1918 / link-local (incl. the
    169.254.169.254 metadata endpoint) / ULA / IPv4-mapped IPv6 / unspecified /
    CGNAT / multicast targets are rejected; public IPv4 + IPv6 pass; a hostname
    is judged by EVERY address it resolves to (mixed public+private rejects);
    DNS failure fails closed; the FEOH_WEBHOOKS_ALLOW_PRIVATE_TARGETS escape
    hatch skips only the address checks (scheme/host shape still enforced);
  * the management API — create/update with a private target 422s with the one
    generic non-enumerating message;
  * the delivery path — a subscription whose host resolves private at SEND time
    (TOCTOU / DNS rebinding) is refused before any POST fires and the delivery
    is marked failed;
  * the pinned send — `_post` dials only an address `resolve_pinned_addresses`
    vetted in the same connect, so a record that flips between the check and
    the connect cannot reach an internal host.

Hostname resolution is monkeypatched (`url_guard._resolve_host`) for
determinism — no real DNS. The escape hatch is forced OFF in every blocking
test so results don't depend on ambient env.
"""

from __future__ import annotations

import ipaddress
import socket
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpcore
import pytest

from app.config import settings
from app.models.webhook import (
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    EVENT_INVOICE_APPROVED,
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.webhooks import delivery as delivery_mod
from app.services.webhooks import url_guard
from app.services.webhooks.signing import generate_signing_secret

# A public (globally routable) IPv4 + IPv6 — used as literal targets and as
# stubbed resolution results.
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


@pytest.fixture(autouse=True)
def _block_by_default(monkeypatch):
    """Force the SAFE default (blocking) regardless of ambient env."""
    monkeypatch.setattr(settings, "webhooks_allow_private_targets", False)


def _stub_resolve(monkeypatch, addresses=None, error=None):
    async def fake_resolve(host):
        if error is not None:
            raise error
        return [ipaddress.ip_address(a) for a in addresses]

    monkeypatch.setattr(url_guard, "_resolve_host", fake_resolve)


# ---------------------------------------------------------------------------
# Pure guard — literal-IP hosts.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/hook",  # loopback
        "http://127.8.9.10:8080/hook",  # loopback, non-canonical + port
        "http://10.0.0.1/hook",  # RFC1918 10/8
        "http://172.16.5.5/hook",  # RFC1918 172.16/12
        "http://192.168.1.10/hook",  # RFC1918 192.168/16
        "http://169.254.169.254/latest/meta-data/",  # link-local / AWS IMDS
        "http://100.64.0.1/hook",  # CGNAT 100.64/10
        "http://0.0.0.0/hook",  # unspecified
        "http://224.0.0.1/hook",  # multicast
        "http://[ff02::1]/hook",  # IPv6 multicast
        "http://[::1]/hook",  # IPv6 loopback
        "http://[fc00::1]/hook",  # unique-local fc00::/7
        "http://[fd12:3456::1]/hook",  # unique-local fd00::/8
        "http://[fe80::1]/hook",  # IPv6 link-local
        "http://[::ffff:10.0.0.1]/hook",  # IPv4-mapped IPv6 wrapping RFC1918
        "http://[::ffff:127.0.0.1]/hook",  # IPv4-mapped IPv6 wrapping loopback
        "https://[::]/hook",  # IPv6 unspecified
    ],
)
@pytest.mark.asyncio
async def test_guard_rejects_non_public_literal_targets(target):
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await url_guard.ensure_public_webhook_target(target)


@pytest.mark.parametrize(
    "target",
    [
        f"https://{PUBLIC_V4}/hook",
        f"http://{PUBLIC_V4}:8443/hook",
        f"https://[{PUBLIC_V6}]/hook",
    ],
)
@pytest.mark.asyncio
async def test_guard_allows_public_literal_targets(target):
    await url_guard.ensure_public_webhook_target(target)  # does not raise


@pytest.mark.parametrize(
    "target",
    [
        "ftp://example.com/hook",  # non-http(s) scheme
        "https:///hook",  # no host
        "not a url",  # no scheme/host at all
    ],
)
@pytest.mark.asyncio
async def test_guard_rejects_malformed_or_non_http_urls(target):
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await url_guard.ensure_public_webhook_target(target)


# ---------------------------------------------------------------------------
# Pure guard — hostname resolution (stubbed).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resolved",
    [
        ["10.0.0.7"],  # A record → private
        ["::1"],  # AAAA → loopback
        ["fc00::2"],  # AAAA → ULA
        ["::ffff:192.168.0.9"],  # AAAA → IPv4-mapped private
        [PUBLIC_V4, "169.254.169.254"],  # mixed: ONE private address poisons it
        [PUBLIC_V6, "127.0.0.1"],  # mixed across families
    ],
)
@pytest.mark.asyncio
async def test_guard_rejects_hostname_resolving_to_private(monkeypatch, resolved):
    _stub_resolve(monkeypatch, addresses=resolved)
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await url_guard.ensure_public_webhook_target("https://hooks.example.com/x")


@pytest.mark.asyncio
async def test_guard_allows_hostname_resolving_public_only(monkeypatch):
    _stub_resolve(monkeypatch, addresses=[PUBLIC_V4, PUBLIC_V6])
    await url_guard.ensure_public_webhook_target("https://hooks.example.com/x")


@pytest.mark.asyncio
async def test_guard_fails_closed_on_dns_failure(monkeypatch):
    _stub_resolve(monkeypatch, error=socket.gaierror(-2, "Name or service not known"))
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await url_guard.ensure_public_webhook_target("https://does-not-resolve.example/x")


@pytest.mark.asyncio
async def test_guard_fails_closed_on_empty_resolution(monkeypatch):
    _stub_resolve(monkeypatch, addresses=[])
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await url_guard.ensure_public_webhook_target("https://empty.example/x")


@pytest.mark.asyncio
async def test_guard_rejection_message_is_generic(monkeypatch):
    """Every rejection carries the SAME message — no range/host enumeration."""
    _stub_resolve(monkeypatch, error=socket.gaierror(-2, "nope"))
    targets = [
        "http://127.0.0.1/hook",
        "http://169.254.169.254/latest/meta-data/",
        "ftp://example.com/hook",
        "https://does-not-resolve.example/x",
    ]
    for target in targets:
        with pytest.raises(url_guard.WebhookTargetNotAllowed) as exc_info:
            await url_guard.ensure_public_webhook_target(target)
        assert str(exc_info.value) == url_guard.REJECT_DETAIL


# ---------------------------------------------------------------------------
# Escape hatch (FEOH_WEBHOOKS_ALLOW_PRIVATE_TARGETS=true).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escape_hatch_allows_private_but_keeps_shape_checks(monkeypatch):
    monkeypatch.setattr(settings, "webhooks_allow_private_targets", True)
    # Address checks skipped: loopback OK, and no DNS lookup happens at all.
    _stub_resolve(monkeypatch, error=AssertionError("must not resolve"))
    await url_guard.ensure_public_webhook_target("http://127.0.0.1:8025/hook")
    await url_guard.ensure_public_webhook_target("https://mailpit.localdomain/hook")
    # Scheme / host shape is still enforced.
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await url_guard.ensure_public_webhook_target("ftp://127.0.0.1/hook")
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await url_guard.ensure_public_webhook_target("https:///hook")


# ---------------------------------------------------------------------------
# Boot-time guard — a deployed env (FEOH_DEBUG=false) may not ship with the
# escape hatch on. Mirrors the billing-webhook / peppol boot guards.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_refuses_escape_hatch_when_not_debug(monkeypatch):
    from app.main import lifespan

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "a-real-non-default-secret-value")
    monkeypatch.setattr(settings, "webhooks_allow_private_targets", True)

    with pytest.raises(RuntimeError, match="FEOH_WEBHOOKS_ALLOW_PRIVATE_TARGETS"):
        async with lifespan(object()):  # pragma: no cover - never enters body
            pass


@pytest.mark.asyncio
async def test_boot_allows_safe_default_when_not_debug(monkeypatch):
    """The blocking default must never trip the guard in a deployed env."""
    from app.main import lifespan

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "a-real-non-default-secret-value")
    monkeypatch.setattr(settings, "webhooks_allow_private_targets", False)
    monkeypatch.setattr(settings, "extraction_reaper_enabled", False)

    async with lifespan(object()):
        pass


# ---------------------------------------------------------------------------
# Management API — create/update reject private targets with a clean 422.
# ---------------------------------------------------------------------------


async def _cleanup(control_mk, *sub_ids):
    from sqlalchemy import delete

    async with control_mk() as s:
        for sid in sub_ids:
            await s.execute(delete(WebhookDelivery).where(WebhookDelivery.subscription_id == sid))
            await s.execute(delete(WebhookSubscription).where(WebhookSubscription.id == sid))
        await s.commit()


async def _create_sub(c, target_url):
    return await c.post(
        "/api/webhooks",
        json={
            "name": "ssrf-probe",
            "target_url": target_url,
            "event_types": [EVENT_INVOICE_APPROVED],
        },
    )


# The subscription CRUD is plan-gated (decisions §258); the harness org reads as free.
@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_create_rejects_private_targets(realdb):
    async with realdb.client(key="a", role="admin") as c:
        for target in (
            "http://127.0.0.1:8000/api/internal",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.20.30.40/hook",
            "http://[::1]/hook",
            "http://[::ffff:192.168.1.1]/hook",
        ):
            resp = await _create_sub(c, target)
            assert resp.status_code == 422, resp.text
            assert resp.json()["detail"] == url_guard.REJECT_DETAIL


# The subscription CRUD is plan-gated (decisions §258); the harness org reads as free.
@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_create_rejects_hostname_resolving_private(realdb, monkeypatch):
    _stub_resolve(monkeypatch, addresses=["192.168.7.7"])
    async with realdb.client(key="a", role="admin") as c:
        resp = await _create_sub(c, "https://internal.corp.example/hook")
        assert resp.status_code == 422
        assert resp.json()["detail"] == url_guard.REJECT_DETAIL


# The subscription CRUD is plan-gated (decisions §258); the harness org reads as free.
@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_create_rejects_unresolvable_hostname(realdb, monkeypatch):
    _stub_resolve(monkeypatch, error=socket.gaierror(-2, "nope"))
    async with realdb.client(key="a", role="admin") as c:
        resp = await _create_sub(c, "https://does-not-resolve.example/hook")
        assert resp.status_code == 422
        assert resp.json()["detail"] == url_guard.REJECT_DETAIL


# The subscription CRUD is plan-gated (decisions §258); the harness org reads as free.
@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_create_allows_public_target_and_update_rejects_private(realdb):
    control_mk = realdb.control_sessionmaker()
    sub_id = None
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await _create_sub(c, f"https://{PUBLIC_V4}/hook")
            assert resp.status_code == 201, resp.text
            sub_id = uuid.UUID(resp.json()["subscription"]["id"])

            # Re-pointing the subscription at a private target is refused …
            upd = await c.patch(
                f"/api/webhooks/{sub_id}",
                json={"target_url": "http://192.168.0.5/exfil"},
            )
            assert upd.status_code == 422
            assert upd.json()["detail"] == url_guard.REJECT_DETAIL

            # … and the stored target is unchanged.
            listed = (await c.get("/api/webhooks")).json()
            row = next(r for r in listed if r["id"] == str(sub_id))
            assert row["target_url"] == f"https://{PUBLIC_V4}/hook"
    finally:
        if sub_id:
            await _cleanup(control_mk, sub_id)


# ---------------------------------------------------------------------------
# Delivery path — the guard re-runs at send time (TOCTOU / DNS rebinding).
# ---------------------------------------------------------------------------


def _make_sub_and_delivery(org_id, *, target):
    secret, prefix = generate_signing_secret()
    sub = WebhookSubscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="rebind",
        target_url=target,
        event_types=[EVENT_INVOICE_APPROVED],
        signing_secret=secret,
        secret_prefix=prefix,
        active=True,
    )
    dlv = WebhookDelivery(
        id=uuid.uuid4(),
        subscription_id=sub.id,
        organization_id=org_id,
        event_id=f"{EVENT_INVOICE_APPROVED}:{uuid.uuid4()}",
        event_type=EVENT_INVOICE_APPROVED,
        payload={"id": "x", "type": EVENT_INVOICE_APPROVED, "data": {}},
        status=DELIVERY_PENDING,
        attempt_count=0,
        next_attempt_at=datetime.now(UTC),
    )
    return sub, dlv


async def _persist(control_mk, sub, dlv):
    async with control_mk() as s:
        s.add(sub)
        await s.flush()
        s.add(dlv)
        await s.commit()


@pytest.mark.parametrize(
    ("target", "resolved"),
    [
        # Host flipped to a private address AFTER create (DNS rebinding).
        ("https://rebind.example/hook", ["10.9.8.7"]),
        # Literal metadata target that somehow reached the table (e.g. written
        # before this guard existed) — refused at send regardless.
        ("http://169.254.169.254/latest/meta-data/", None),
    ],
)
@pytest.mark.asyncio
async def test_delivery_refuses_non_public_target_at_send(realdb, monkeypatch, target, resolved):
    org_id = realdb.info("a").org_id
    control_mk = realdb.control_sessionmaker()
    sub, dlv = _make_sub_and_delivery(org_id, target=target)
    await _persist(control_mk, sub, dlv)

    if resolved is not None:
        _stub_resolve(monkeypatch, addresses=resolved)

    async def forbidden_post(*a, **k):
        raise AssertionError("POST must never fire for a non-public target")

    monkeypatch.setattr(delivery_mod, "_post", forbidden_post)

    try:
        async with control_mk() as s:
            row = await s.get(WebhookDelivery, dlv.id)
            await delivery_mod.process_delivery(s, row)
            # Refused before any POST: a failed attempt with no response code,
            # so the normal retry/backoff → dead-letter path applies.
            assert row.status == DELIVERY_FAILED
            assert row.response_code is None
            assert row.attempt_count == 1
            assert row.next_attempt_at is not None
    finally:
        await _cleanup(control_mk, sub.id)


@pytest.mark.asyncio
async def test_delivery_proceeds_when_target_resolves_public(realdb, monkeypatch):
    org_id = realdb.info("a").org_id
    control_mk = realdb.control_sessionmaker()
    sub, dlv = _make_sub_and_delivery(org_id, target="https://hooks.example.com/x")
    await _persist(control_mk, sub, dlv)

    _stub_resolve(monkeypatch, addresses=[PUBLIC_V4])

    async def fake_post(*a, **k):
        return 200

    monkeypatch.setattr(delivery_mod, "_post", fake_post)

    try:
        async with control_mk() as s:
            row = await s.get(WebhookDelivery, dlv.id)
            await delivery_mod.process_delivery(s, row)
            assert row.response_code == 200
            assert row.attempt_count == 1
    finally:
        await _cleanup(control_mk, sub.id)


# ---------------------------------------------------------------------------
# The send is PINNED: the socket opens to an address url_guard vetted.
# ---------------------------------------------------------------------------
#
# `ensure_public_webhook_target` runs just before `_post`, but httpx would make
# its own lookup when it connects. `_post` uses url_safety.pinned_async_client
# with `url_guard.resolve_pinned_addresses`, so the address dialled is one that
# this module's rule passed in the same connect. The socket layer is replaced by
# a recorder so the dialled address is observable without a network.


class _Stream(httpcore.AsyncNetworkStream):
    def __init__(self):
        self._pending = b"HTTP/1.1 204 No Content\r\n\r\n"

    async def read(self, max_bytes, timeout=None):
        out, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return out

    async def write(self, buffer, timeout=None):
        pass

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, info):
        return None


class _Recorder(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.dialled: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.dialled.append((host, port))
        return _Stream()

    async def connect_unix_socket(self, *a, **k):
        raise AssertionError("never a unix socket")

    async def sleep(self, seconds):
        pass


@pytest.fixture
def recorder(monkeypatch):
    from app.utils import url_safety

    rec = _Recorder()
    monkeypatch.setattr(url_safety.httpcore, "AnyIOBackend", lambda: rec)
    return rec


def _delivery_stub():
    return SimpleNamespace(event_id="evt_1", event_type=EVENT_INVOICE_APPROVED)


class _Rebinding:
    """`_resolve_host` stand-in: a public answer once, then an internal one."""

    def __init__(self, later: str):
        self.later, self.calls = later, 0

    async def __call__(self, host):
        self.calls += 1
        return [ipaddress.ip_address(PUBLIC_V4 if self.calls == 1 else self.later)]


@pytest.mark.asyncio
async def test_post_dials_the_vetted_address_not_a_rebound_one(monkeypatch, recorder):
    dns = _Rebinding("169.254.169.254")
    monkeypatch.setattr(url_guard, "_resolve_host", dns)
    code = await delivery_mod._post("https://hooks.example.com/x", b"{}", "sig", _delivery_stub())
    assert code == 204
    assert recorder.dialled == [(PUBLIC_V4, 443)]
    assert dns.calls == 1  # no second lookup exists for the record to flip


@pytest.mark.asyncio
async def test_check_then_rebind_is_refused_at_connect(monkeypatch, recorder):
    """The TOCTOU the send-time re-check alone left open: the check sees the
    public answer, the connect sees loopback, and nothing is dialled."""
    monkeypatch.setattr(url_guard, "_resolve_host", _Rebinding("127.0.0.1"))
    target = "https://hooks.example.com/x"
    await url_guard.ensure_public_webhook_target(target)  # passes on answer one
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await delivery_mod._post(target, b"{}", "sig", _delivery_stub())
    assert recorder.dialled == []


@pytest.mark.asyncio
async def test_pinned_resolver_applies_the_webhook_rule_not_the_generic_one(monkeypatch):
    """CGNAT 100.64/10 is not global: the webhook rule blocks it (the generic
    url_safety policy does not), and the pinned resolver must use THIS rule."""
    _stub_resolve(monkeypatch, addresses=["100.64.0.1"])
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await url_guard.resolve_pinned_addresses("hooks.example.com", 443)
    _stub_resolve(monkeypatch, error=socket.gaierror("nope"))
    with pytest.raises(url_guard.WebhookTargetNotAllowed):
        await url_guard.resolve_pinned_addresses("hooks.example.com", 443)
    _stub_resolve(monkeypatch, addresses=[PUBLIC_V4, PUBLIC_V6])
    assert await url_guard.resolve_pinned_addresses("hooks.example.com", 443) == [
        PUBLIC_V4,
        PUBLIC_V6,
    ]


@pytest.mark.asyncio
async def test_escape_hatch_still_pins_but_allows_loopback(monkeypatch, recorder):
    """FEOH_WEBHOOKS_ALLOW_PRIVATE_TARGETS keeps local delivery to Mailpit /
    127.0.0.1 working; the connection is still pinned to what resolved."""
    monkeypatch.setattr(settings, "webhooks_allow_private_targets", True)
    _stub_resolve(monkeypatch, addresses=["127.0.0.1"])
    assert await url_guard.resolve_pinned_addresses("mailpit.localdomain", 8025) == ["127.0.0.1"]
    code = await delivery_mod._post("http://127.0.0.1:8025/hook", b"{}", "sig", _delivery_stub())
    assert code == 204
    assert recorder.dialled == [("127.0.0.1", 8025)]
