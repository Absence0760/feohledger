"""Discount-offer, experiment and adaptive-suggestion rows carry the caller's
verdict on each row action, and it says exactly what the endpoint answers
(issue #575 § One rule, one place).

* discount offers — `actions.{accept,decline}` (+ `capturable_tier_days`): the
  page offered Accept / Decline on `status === 'offered'` and a manager-or-CFO
  role check, so an offer whose tier windows had all closed, or whose invoice
  was already paid, showed an Accept the endpoint refuses;
* experiments — `actions.start`: `isAdmin && status === 'draft'`, a restated
  copy of the start gate;
* adaptive suggestions — `actions.dismiss`: the page offered Dismiss only on
  `open`, while the endpoint also accepted `stale` and overwrote an `applied`
  row with `dismissed` (now refused, `workflow_suggestion_not_dismissable`).

The parity property, per role and row state: verdict `None` exactly when the
mutation succeeds, otherwise the code (coded detail) or the sentence (plain
detail) of the refusal it answers with. Both sides are asked. The two
idempotent repeats — starting a `running` experiment, dismissing a
`dismissed` suggestion — answer 200 with the row unchanged, and their verdict
refuses (nothing to offer); that pairing is asserted as such.

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.adaptive_workflows import WORKFLOW_SUGGESTION_NOT_DISMISSABLE, dismiss_refusal
from app.api.discounts import (
    DISCOUNT_OFFER_DECLINE_WINDOW_CLOSED,
    DISCOUNT_OFFER_INVOICE_SETTLED,
    DISCOUNT_OFFER_NO_CAPTURABLE_TIER,
    DISCOUNT_OFFER_NOT_OPEN,
)
from app.api.workflow_experiments import EXPERIMENT_NOT_STARTABLE
from app.models.adaptive_suggestion import WorkflowSuggestion
from app.models.discount import DiscountOffer
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import WorkflowDefinition
from app.models.workflow_experiment import WorkflowExperiment
from app.utils.dates import utc_today

TENANT = "a"
ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")
pytestmark = pytest.mark.asyncio


def _assert_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code in (200, 201), (label, resp.text)
        return
    assert resp.status_code in (403, 409, 422), (label, verdict, resp.text)
    detail = resp.json()["detail"]
    if isinstance(detail, dict):
        assert detail["code"] == verdict["code"], (label, verdict, detail)
    else:
        assert detail == verdict["message"], (label, verdict, detail)


async def _default_entity_id(s):
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


# ── Discount offers ────────────────────────────────────────────────────────

#: Each state an offer row can be in when the page reads it.
#:   open          — offered, tiers measured from today: both decisions accepted
#:   tiers_closed  — offered, every tier window past: decline only
#:   lapsed        — offered, valid_until past the decline grace: neither
#:   settled       — offered, its invoice already paid: decline only
#:   accepted / declined — decided: neither
OFFER_STATES = ("open", "tiers_closed", "lapsed", "settled", "accepted", "declined")


async def _offer(realdb, state: str) -> uuid.UUID:
    today = utc_today()
    org_id = realdb.info(TENANT).org_id
    valid_from = today - timedelta(days=60) if state == "tiers_closed" else today
    valid_until = today - timedelta(days=5) if state == "lapsed" else today + timedelta(days=40)
    status = {"accepted": "accepted", "declined": "declined"}.get(state, "offered")
    async with realdb.sessionmaker(TENANT)() as s:
        entity_id = await _default_entity_id(s)
        invoice_id = None
        if state == "settled":
            inv = Invoice(
                organization_id=org_id,
                entity_id=entity_id,
                invoice_number=f"VERDICT-{uuid.uuid4().hex[:8]}",
                vendor_name="Verdict Vendor",
                amount=Decimal("1000.00"),
                currency="USD",
                due_date=today + timedelta(days=30),
                status=InvoiceStatus.paid,
            )
            s.add(inv)
            await s.flush()
            invoice_id = inv.id
        offer = DiscountOffer(
            organization_id=org_id,
            entity_id=entity_id,
            scope="invoice" if invoice_id else "vendor",
            invoice_id=invoice_id,
            source="supplier",
            status=status,
            tiers=[{"days": 5, "percent": "3.00"}, {"days": 10, "percent": "2.00"}],
            base_amount=Decimal("1000.00"),
            currency="USD",
            valid_from=valid_from,
            valid_until=valid_until,
            accepted_tier={"days": 10, "percent": "2.00"} if status == "accepted" else None,
        )
        s.add(offer)
        await s.commit()
        return offer.id


async def _offer_row(realdb, role, offer_id):
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.get("/api/discounts/offers", params={"page_size": 100})
    assert resp.status_code == 200, resp.text
    rows = {item["id"]: item for item in resp.json()["items"]}
    return rows[str(offer_id)]


@pytest.mark.parametrize("role", ROLES)
async def test_the_offer_verdicts_match_accept_and_decline(realdb, role):
    for state in OFFER_STATES:
        for action in ("accept", "decline"):
            offer_id = await _offer(realdb, state)
            row = await _offer_row(realdb, role, offer_id)
            async with realdb.client(key=TENANT, role=role) as c:
                resp = await c.post(f"/api/discounts/offers/{offer_id}/{action}", json={})
            verdict = row["actions"][action]
            _assert_parity(verdict, resp, (role, state, action))


async def test_the_offer_verdicts_name_the_reason(realdb):
    """The codes the page localizes, one per state — so a regression that
    folds them (or reorders the gate) is caught by name, not just parity."""
    expected = {
        "open": (None, None),
        "tiers_closed": (DISCOUNT_OFFER_NO_CAPTURABLE_TIER, None),
        "lapsed": (DISCOUNT_OFFER_NO_CAPTURABLE_TIER, DISCOUNT_OFFER_DECLINE_WINDOW_CLOSED),
        "settled": (DISCOUNT_OFFER_INVOICE_SETTLED, None),
        "accepted": (DISCOUNT_OFFER_NOT_OPEN, DISCOUNT_OFFER_NOT_OPEN),
        "declined": (DISCOUNT_OFFER_NOT_OPEN, DISCOUNT_OFFER_NOT_OPEN),
    }
    for state, (accept_code, decline_code) in expected.items():
        offer_id = await _offer(realdb, state)
        row = await _offer_row(realdb, "ap_manager", offer_id)
        accept, decline = row["actions"]["accept"], row["actions"]["decline"]
        assert (accept or {}).get("code") == accept_code, (state, accept)
        assert (decline or {}).get("code") == decline_code, (state, decline)
        if state == "settled":
            assert accept["params"] == {"status": "paid"}


async def test_capturable_tier_days_are_the_tiers_accept_takes_by_name(realdb):
    """The accept dialog offers `capturable_tier_days` and nothing else; each
    listed tier is accepted by name, and a tier not listed is refused."""
    offer_id = await _offer(realdb, "open")
    row = await _offer_row(realdb, "ap_manager", offer_id)
    assert sorted(row["capturable_tier_days"]) == [5, 10]
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        missing = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={"tier_days": 7})
        assert missing.status_code == 422, missing.text
        taken = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={"tier_days": 10})
        assert taken.status_code == 200, taken.text
    # The decided offer's own response carries the refreshed verdicts.
    assert taken.json()["actions"]["accept"]["code"] == DISCOUNT_OFFER_NOT_OPEN
    assert taken.json()["capturable_tier_days"] == []

    closed = await _offer(realdb, "tiers_closed")
    assert (await _offer_row(realdb, "ap_manager", closed))["capturable_tier_days"] == []


# ── Experiments ────────────────────────────────────────────────────────────

EXPERIMENT_STATES = ("draft", "running", "concluded")


async def _experiment(realdb, status: str) -> uuid.UUID:
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        entity_id = await _default_entity_id(s)
        defn = WorkflowDefinition(
            organization_id=org_id,
            entity_id=entity_id,
            name=f"Verdict WF {uuid.uuid4().hex[:6]}",
            steps_config={"steps": [{"type": "approval", "config": {}}]},
            is_active=True,
            is_default=False,
        )
        s.add(defn)
        await s.flush()
        exp = WorkflowExperiment(
            organization_id=org_id,
            entity_id=entity_id,
            name=f"Verdict experiment {uuid.uuid4().hex[:6]}",
            workflow_definition_id=defn.id,
            config_a={"steps": []},
            config_b={"steps": []},
            split_a_pct=50,
            primary_metric="time_to_approval_days",
            min_sample_per_variant=5,
            status=status,
            started_at=datetime.now(UTC) if status != "draft" else None,
            ended_at=datetime.now(UTC) if status == "concluded" else None,
        )
        s.add(exp)
        await s.commit()
        return exp.id


@pytest.mark.parametrize("role", ROLES)
async def test_the_experiment_verdict_matches_start(realdb, role):
    for state in EXPERIMENT_STATES:
        exp_id = await _experiment(realdb, state)
        async with realdb.client(key=TENANT, role=role) as c:
            listed = await c.get("/api/experiments")
            resp = await c.post(f"/api/experiments/{exp_id}/start")
        if listed.status_code == 403:
            # Not a reader (clerk): no row to judge, and start is a write.
            assert resp.status_code == 403
            continue
        rows = {e["id"]: e for e in listed.json()["experiments"]}
        verdict = rows[str(exp_id)]["actions"]["start"]
        if state == "running" and verdict["code"] == EXPERIMENT_NOT_STARTABLE:
            # The idempotent repeat: 200, unchanged — not an action to offer.
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "running"
            continue
        _assert_parity(verdict, resp, (role, state))
        if verdict is not None and verdict["code"] == EXPERIMENT_NOT_STARTABLE:
            assert verdict["params"] == {"status": state}


# ── Adaptive suggestions ───────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["open", "stale", "dismissed", "applied"])
def test_dismiss_refusal_per_status(status):
    """`open` and `stale` dismiss; `dismissed` is the idempotent repeat (not
    offered) and `applied` is refused — it records a change that was made."""
    row = WorkflowSuggestion(status=status)
    refusal = dismiss_refusal(row)
    if status in ("open", "stale"):
        assert refusal is None
    else:
        assert refusal is not None
        assert refusal.code == WORKFLOW_SUGGESTION_NOT_DISMISSABLE
        assert refusal.http_status == 409
        assert refusal.params == {"status": status}


async def _suggestion(realdb, status: str) -> uuid.UUID:
    sid = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            WorkflowSuggestion(
                id=sid,
                organization_id=realdb.info(TENANT).org_id,
                kind="auto_approve_threshold",
                dedupe_key=f"auto_approve_threshold:VERDICT-{sid.hex}",
                vendor_name="Verdict Vendor",
                title="Verdict suggestion",
                payload={"vendor_id": None},
                confidence_pct=Decimal("90"),
                status=status,
                dismissed_at=datetime.now(UTC) if status == "dismissed" else None,
            )
        )
        await s.commit()
    return sid


@pytest.mark.parametrize("role", ROLES)
async def test_the_suggestion_verdict_matches_dismiss(realdb, role):
    # Seeded `open`, the list read itself turns a row no derivation produces
    # into `stale` — so `stale` is what the read judges; `open` is pinned by
    # the gate's unit test above.
    applied_sid = None
    for state in ("open", "dismissed", "applied"):
        sid = await _suggestion(realdb, state)
        if state == "applied":
            applied_sid = sid
        async with realdb.client(key=TENANT, role=role) as c:
            listed = await c.get("/api/adaptive/suggestions", params={"status": "all"})
            resp = await c.post(f"/api/adaptive/suggestions/{sid}/dismiss")
        if listed.status_code == 403:
            assert resp.status_code == 403
            continue
        row = {str(r["id"]): r for r in listed.json()["suggestions"]}[str(sid)]
        verdict = row["actions"]["dismiss"]
        if row["status"] == "dismissed" and verdict and verdict["code"] != "role_not_permitted":
            # The idempotent repeat: 200, unchanged — not an action to offer.
            assert verdict["code"] == WORKFLOW_SUGGESTION_NOT_DISMISSABLE
            assert resp.status_code == 200, resp.text
            assert resp.json()["suggestions"][0]["status"] == "dismissed"
            continue
        _assert_parity(verdict, resp, (role, state, row["status"]))
    async with realdb.sessionmaker(TENANT)() as s:
        row = await s.get(WorkflowSuggestion, applied_sid)
    assert row.status == "applied", "a dismiss must never overwrite an applied suggestion"
