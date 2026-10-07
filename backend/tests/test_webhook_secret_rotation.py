"""Rotating an outbound-webhook signing secret without losing deliveries or history.

A subscription's HMAC signing secret is the customer's verification key. Anyone
holding it can forge a signed `invoice.approved` / `payment.settled` payload
into their receiver, so it needs a rotation path — and there wasn't one. It was
minted once at create time and shown once, and the only remedy on a leak was
`DELETE /api/webhooks/{id}` + re-create, which changes the subscription id and
CASCADE-deletes the entire delivery log. Recovering from a leak meant destroying
the record of what had been delivered.

The hard part is the instant in between. With ONE signature header you cannot
satisfy a receiver still holding the old secret and one already holding the new
one, so a rotation may open a bounded overlap during which the retiring secret
also signs, in a second `X-Webhook-Signature-Previous` header. The primary
header is ALWAYS the current secret, so an existing receiver's contract never
changes meaning.

What these pin, in the order the properties matter:

  * the expiry rule — an elapsed window is indistinguishable from no window, so
    a row left stale can never keep a retired key signing;
  * the dispatcher emits the second header only while the window is open;
  * the endpoint returns the new secret exactly once, keeps the subscription id
    and its deliveries, and never writes either secret to the audit trail;
  * a hard cutover (`overlap_minutes: 0`) retires the old key immediately.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.webhook import (
    DELIVERY_PENDING,
    EVENT_INVOICE_APPROVED,
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.webhooks import delivery as delivery_mod
from app.services.webhooks.rotation import (
    DEFAULT_OVERLAP_MINUTES,
    MAX_OVERLAP_MINUTES,
    PREVIOUS_SIGNATURE_HEADER,
    previous_secret_if_live,
    rotate_secret,
)
from app.services.webhooks.signing import generate_signing_secret, sign_payload

# Outbound webhooks are plan-gated (decisions §258) — both the subscription CRUD
# and dispatch, which queues nothing for an org without the feature — and the
# realdb harness orgs hold no subscription (= free), so bind both to Scale.
pytestmark = pytest.mark.plan("scale")


@pytest_asyncio.fixture(autouse=True)
async def _reap_subscriptions_this_file_creates(realdb):
    """Delete any subscription this file's tests leave behind.

    The `realdb` harness resets tenant tables but deliberately does NOT delete
    extra CONTROL-plane rows a test creates (see backend/CLAUDE.md § Test
    databases) — and `webhook_subscriptions` is control-plane. The tests below
    create subscriptions through the API, so without this they accumulate in a
    database that outlives the session and inflate any later test that COUNTS
    subscriptions matching an event — which is exactly how
    `test_outbound_webhooks.py::test_emit_enqueues_one_per_matching_active_sub_and_dedupes`
    started failing when the two files ran together.

    Snapshot-and-diff rather than a blanket delete, so this can only ever reap
    rows these tests are responsible for.
    """
    control_mk = realdb.control_sessionmaker()
    async with control_mk() as s:
        before = set((await s.execute(select(WebhookSubscription.id))).scalars().all())
    yield
    async with control_mk() as s:
        after = set((await s.execute(select(WebhookSubscription.id))).scalars().all())
        for sub_id in after - before:
            await s.execute(
                delete(WebhookDelivery).where(WebhookDelivery.subscription_id == sub_id)
            )
            await s.execute(delete(WebhookSubscription).where(WebhookSubscription.id == sub_id))
        await s.commit()


@pytest.fixture(autouse=True)
def _allow_test_targets(monkeypatch):
    """Same escape hatch the sibling `test_outbound_webhooks.py` uses: these
    tests point at a non-resolvable `example.test` target and stub the HTTP
    layer, so flip the SSRF-guard flag (the committed local-dev default) to stop
    the address check rejecting them. The guard itself is covered, with the flag
    OFF, in `test_webhook_url_guard.py`."""
    from app.config import settings

    monkeypatch.setattr(settings, "webhooks_allow_private_targets", True)


# ---------------------------------------------------------------------------
# The expiry rule — pure, and the reason both callers read it from one place
# ---------------------------------------------------------------------------

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def test_previous_secret_is_live_inside_the_window():
    assert (
        previous_secret_if_live(
            previous_secret="whsec_old",
            previous_expires_at=NOW + timedelta(minutes=5),
            now=NOW,
        )
        == "whsec_old"
    )


def test_previous_secret_is_dead_once_the_window_elapses():
    """The property that stops a stale row keeping a retired key alive."""
    assert (
        previous_secret_if_live(
            previous_secret="whsec_old",
            previous_expires_at=NOW - timedelta(seconds=1),
            now=NOW,
        )
        is None
    )


@pytest.mark.parametrize(
    ("secret", "expires"),
    [
        (None, NOW + timedelta(minutes=5)),  # half-written row
        ("whsec_old", None),  # ...the other half
        (None, None),  # the ordinary state
        ("", NOW + timedelta(minutes=5)),  # empty string is not a secret
    ],
)
def test_a_half_written_window_is_no_window(secret, expires):
    assert (
        previous_secret_if_live(previous_secret=secret, previous_expires_at=expires, now=NOW)
        is None
    )


def test_naive_expiry_is_treated_as_utc():
    """The column is TIMESTAMPTZ, but a value can come back from a driver or a
    fixture without tzinfo. Comparing that against an aware `now` raises — and
    failing closed there would silently drop the overlap header mid-rotation."""
    naive = (NOW + timedelta(minutes=5)).replace(tzinfo=None)
    assert (
        previous_secret_if_live(previous_secret="whsec_old", previous_expires_at=naive, now=NOW)
        == "whsec_old"
    )


# ---------------------------------------------------------------------------
# Minting the replacement
# ---------------------------------------------------------------------------


def test_rotation_mints_a_new_secret_and_carries_the_old_one():
    result = rotate_secret(current_secret="whsec_old", now=NOW, overlap_minutes=30)
    assert result.plaintext_secret != "whsec_old"
    assert result.plaintext_secret.startswith("whsec_")
    assert result.secret_prefix == result.plaintext_secret[:12]
    assert result.previous_secret == "whsec_old"
    assert result.previous_expires_at == NOW + timedelta(minutes=30)


def test_hard_cutover_retires_the_old_secret_immediately():
    """`overlap_minutes: 0` is the known-compromised case — the old key must
    stop verifying on the very next delivery, and a few rejections are the
    point rather than a cost."""
    result = rotate_secret(current_secret="whsec_old", now=NOW, overlap_minutes=0)
    assert result.previous_secret is None
    assert result.previous_expires_at is None


@pytest.mark.parametrize("bad", [-1, MAX_OVERLAP_MINUTES + 1])
def test_out_of_range_overlap_raises_rather_than_clamping(bad):
    """Silently shortening a window drops deliveries the caller relied on;
    silently lengthening one keeps a key they wanted dead alive."""
    with pytest.raises(ValueError):
        rotate_secret(current_secret="whsec_old", now=NOW, overlap_minutes=bad)


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


def _make_sub_and_delivery(org_id, **sub_kwargs):
    secret, prefix = generate_signing_secret()
    sub = WebhookSubscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="rotate-test",
        target_url="https://example.test/hook",
        event_types=[EVENT_INVOICE_APPROVED],
        signing_secret=secret,
        secret_prefix=prefix,
        active=True,
        **sub_kwargs,
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


async def _cleanup(control_mk, sub_id):
    async with control_mk() as s:
        await s.execute(delete(WebhookDelivery).where(WebhookDelivery.subscription_id == sub_id))
        await s.execute(delete(WebhookSubscription).where(WebhookSubscription.id == sub_id))
        await s.commit()


async def _capture_delivery(realdb, monkeypatch, **sub_kwargs) -> dict:
    org_id = realdb.info("a").org_id
    control_mk = realdb.control_sessionmaker()
    sub, dlv = _make_sub_and_delivery(org_id, **sub_kwargs)
    await _persist(control_mk, sub, dlv)

    captured: dict = {}

    async def fake_post(target_url, body, signature, delivery, previous_signature=None):
        captured["body"] = body
        captured["sig"] = signature
        captured["prev_sig"] = previous_signature
        return 200

    monkeypatch.setattr(delivery_mod, "_post", fake_post)
    try:
        async with control_mk() as s:
            row = await s.get(WebhookDelivery, dlv.id)
            await delivery_mod.process_delivery(s, row)
        captured["sub"] = sub
        return captured
    finally:
        await _cleanup(control_mk, sub.id)


@pytest.mark.asyncio
async def test_open_window_signs_both_headers(realdb, monkeypatch):
    captured = await _capture_delivery(
        realdb,
        monkeypatch,
        previous_signing_secret="whsec_theoldone",
        previous_secret_expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    sub = captured["sub"]
    # Primary is ALWAYS the current secret — an existing receiver's contract
    # never changes meaning mid-rotation.
    assert captured["sig"] == sign_payload(sub.signing_secret, captured["body"])
    # ...and the retiring secret signs the secondary header over the SAME bytes.
    assert captured["prev_sig"] == sign_payload("whsec_theoldone", captured["body"])
    assert captured["prev_sig"] != captured["sig"]


@pytest.mark.asyncio
async def test_expired_window_sends_no_second_header(realdb, monkeypatch):
    """The end-to-end form of the expiry rule: a row nobody cleaned up must
    not keep the retired key signing."""
    captured = await _capture_delivery(
        realdb,
        monkeypatch,
        previous_signing_secret="whsec_theoldone",
        previous_secret_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert captured["prev_sig"] is None


@pytest.mark.asyncio
async def test_no_rotation_sends_no_second_header(realdb, monkeypatch):
    captured = await _capture_delivery(realdb, monkeypatch)
    assert captured["prev_sig"] is None


def test_the_header_name_is_stable():
    """Receivers hard-code this string; renaming it silently breaks every
    customer mid-rotation."""
    assert PREVIOUS_SIGNATURE_HEADER == "X-Webhook-Signature-Previous"


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


async def _create_subscription(client) -> tuple[str, str]:
    resp = await client.post(
        "/api/webhooks",
        json={
            "name": "rotate-me",
            "target_url": "https://example.test/hook",
            "event_types": [EVENT_INVOICE_APPROVED],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["subscription"]["id"], body["signing_secret"]


@pytest.mark.asyncio
async def test_rotate_returns_a_new_secret_and_keeps_the_subscription(realdb):
    async with realdb.client(key="a", role="admin") as client:
        sub_id, original = await _create_subscription(client)
        resp = await client.post(f"/api/webhooks/{sub_id}/rotate-secret", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Same subscription — its id and therefore its delivery history survive,
    # which delete-and-recreate could never offer.
    assert body["subscription"]["id"] == sub_id
    assert body["signing_secret"] != original
    assert body["subscription"]["secret_prefix"] == body["signing_secret"][:12]
    assert body["previous_secret_expires_at"] is not None


@pytest.mark.asyncio
async def test_rotate_persists_the_overlap(realdb):
    async with realdb.client(key="a", role="admin") as client:
        sub_id, original = await _create_subscription(client)
        resp = await client.post(
            f"/api/webhooks/{sub_id}/rotate-secret", json={"overlap_minutes": 15}
        )
    assert resp.status_code == 200, resp.text

    async with realdb.control_sessionmaker()() as s:
        row = (
            await s.execute(
                select(WebhookSubscription).where(WebhookSubscription.id == uuid.UUID(sub_id))
            )
        ).scalar_one()
        assert row.previous_signing_secret == original
        assert row.signing_secret == resp.json()["signing_secret"]
        assert (
            previous_secret_if_live(
                previous_secret=row.previous_signing_secret,
                previous_expires_at=row.previous_secret_expires_at,
            )
            == original
        )


@pytest.mark.asyncio
async def test_hard_cutover_stores_no_previous_secret(realdb):
    async with realdb.client(key="a", role="admin") as client:
        sub_id, _ = await _create_subscription(client)
        resp = await client.post(
            f"/api/webhooks/{sub_id}/rotate-secret", json={"overlap_minutes": 0}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["previous_secret_expires_at"] is None

    async with realdb.control_sessionmaker()() as s:
        row = (
            await s.execute(
                select(WebhookSubscription).where(WebhookSubscription.id == uuid.UUID(sub_id))
            )
        ).scalar_one()
        assert row.previous_signing_secret is None
        assert row.previous_secret_expires_at is None


@pytest.mark.asyncio
async def test_list_never_echoes_a_rotated_secret(realdb):
    """The create-time contract — shown exactly once — must hold for rotation
    too, or rotating would be a way to read the key back out."""
    async with realdb.client(key="a", role="admin") as client:
        sub_id, _ = await _create_subscription(client)
        rotated = (await client.post(f"/api/webhooks/{sub_id}/rotate-secret", json={})).json()
        listed = await client.get("/api/webhooks")

    assert listed.status_code == 200, listed.text
    blob = listed.text
    assert rotated["signing_secret"] not in blob
    assert "signing_secret" not in listed.json()[0]


@pytest.mark.asyncio
async def test_rotate_refuses_an_out_of_range_overlap(realdb):
    async with realdb.client(key="a", role="admin") as client:
        sub_id, _ = await _create_subscription(client)
        too_long = await client.post(
            f"/api/webhooks/{sub_id}/rotate-secret",
            json={"overlap_minutes": MAX_OVERLAP_MINUTES + 1},
        )
        negative = await client.post(
            f"/api/webhooks/{sub_id}/rotate-secret", json={"overlap_minutes": -1}
        )
    assert too_long.status_code == 422, too_long.text
    assert negative.status_code == 422, negative.text


@pytest.mark.asyncio
async def test_rotate_is_admin_only(realdb):
    async with realdb.client(key="a", role="admin") as client:
        sub_id, _ = await _create_subscription(client)
    async with realdb.client(key="a", role="ap_manager") as client:
        resp = await client.post(f"/api/webhooks/{sub_id}/rotate-secret", json={})
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_rotate_cannot_reach_another_orgs_subscription(realdb):
    """Same opaque 404 as every other by-id route here, so a caller can't
    enumerate another tenant's subscription ids."""
    async with realdb.client(key="a", role="admin") as client:
        sub_id, _ = await _create_subscription(client)
    async with realdb.client(key="b", role="admin") as client:
        resp = await client.post(f"/api/webhooks/{sub_id}/rotate-secret", json={})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_rotation_defaults_to_the_documented_overlap(realdb):
    async with realdb.client(key="a", role="admin") as client:
        sub_id, _ = await _create_subscription(client)
        before = datetime.now(UTC)
        resp = await client.post(f"/api/webhooks/{sub_id}/rotate-secret", json={})

    expires = datetime.fromisoformat(resp.json()["previous_secret_expires_at"])
    delta = expires - before
    assert (
        timedelta(minutes=DEFAULT_OVERLAP_MINUTES - 1)
        <= delta
        <= timedelta(minutes=DEFAULT_OVERLAP_MINUTES + 1)
    )


@pytest.mark.asyncio
async def test_concurrent_rotations_lose_no_secret(realdb):
    """Two admins racing during incident response must not silently break one.

    Rotation derives its new state from the CURRENT secret (it carries it into
    `previous_signing_secret`). Without a row lock both calls read the same
    current secret, mint different replacements, and the second commit
    overwrites the first — leaving the losing admin holding a secret from a
    200 response that was never persisted, so their receiver is configured with
    a key that will never verify and nothing surfaces the error.

    Serialized by `FOR UPDATE`, the loser's secret becomes the PREVIOUS one
    instead: still signing the overlap header, so their receiver keeps working
    through the window. Asserting set-equality is the precise claim — every
    secret handed out is accounted for on the row, in one slot or the other.
    """
    import asyncio

    async with realdb.client(key="a", role="admin") as client:
        sub_id, _ = await _create_subscription(client)
        first, second = await asyncio.gather(
            client.post(f"/api/webhooks/{sub_id}/rotate-secret", json={}),
            client.post(f"/api/webhooks/{sub_id}/rotate-secret", json={}),
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    handed_out = {first.json()["signing_secret"], second.json()["signing_secret"]}
    assert len(handed_out) == 2, "each rotation must mint a distinct secret"

    async with realdb.control_sessionmaker()() as s:
        row = (
            await s.execute(
                select(WebhookSubscription).where(WebhookSubscription.id == uuid.UUID(sub_id))
            )
        ).scalar_one()
    assert {row.signing_secret, row.previous_signing_secret} == handed_out


@pytest.mark.asyncio
async def test_list_surfaces_the_overlap_expiry_so_a_reload_keeps_it(realdb):
    """The list surface carries the expiry — the EXPIRY only, never the secret.

    Without it the admin UI can only remember an in-flight rotation for the life
    of one page view, which is exactly when someone is away pasting the new
    secret into another system. A reload would silently drop the signal that the
    old secret is still being honoured.
    """
    async with realdb.client(key="a", role="admin") as client:
        sub_id, _ = await _create_subscription(client)
        before = (await client.get("/api/webhooks")).json()
        rotated = await client.post(
            f"/api/webhooks/{sub_id}/rotate-secret", json={"overlap_minutes": 30}
        )
        after = (await client.get("/api/webhooks")).json()

    # No rotation in flight -> null, the ordinary state.
    assert next(s for s in before if s["id"] == sub_id)["previous_secret_expires_at"] is None

    row = next(s for s in after if s["id"] == sub_id)
    assert row["previous_secret_expires_at"] == rotated.json()["previous_secret_expires_at"]
    # ...and the retiring secret itself is still nowhere on this surface.
    assert "previous_signing_secret" not in row
    assert "signing_secret" not in row


@pytest.mark.asyncio
async def test_hard_cutover_leaves_no_overlap_on_the_list(realdb):
    async with realdb.client(key="a", role="admin") as client:
        sub_id, _ = await _create_subscription(client)
        await client.post(f"/api/webhooks/{sub_id}/rotate-secret", json={"overlap_minutes": 0})
        listed = (await client.get("/api/webhooks")).json()

    assert next(s for s in listed if s["id"] == sub_id)["previous_secret_expires_at"] is None
