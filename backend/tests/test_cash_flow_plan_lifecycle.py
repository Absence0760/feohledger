"""AI Cash-Flow Copilot — the saved-plan lifecycle, end to end and in depth.

Companion to `tests/test_cash_flow_saved_plans.py` (which pins the surface:
routes, RBAC, freeze/thaw, the happy-path variance) and
`tests/test_cash_flow_copilot.py` (which pins the tools and the Phase 3 enact
boundary). This file goes after the four load-bearing calls in
[decisions](../../docs/decisions.md) §54/§55 that are easy to break in a way
every existing test still reads green for:

1. **A saved plan is a frozen baseline.** The headline test does not merely
   assert `created is False` on a repeat save — it moves the world underneath
   the plan (a new in-horizon invoice, a new discount offer, a different
   opening balance), *proves a fresh proposal for the same `plan_id` would now
   say something different*, and only then asserts the second save returns the
   FIRST snapshot unchanged — response payload, stored JSONB and `updated_at`
   alike. Without the non-vacuity step, an upsert-on-save regression passes:
   restating identical data is indistinguishable from not restating it.

2. **`plan_id` is a pure hash of its resolved inputs.** Every defining input
   moves the id, `entity_id` genuinely among them (which is what makes §55's
   scope discovery safe), and an omitted value cannot collide with a present
   one — `Decimal("0")` is falsy, so a `str(x or "-")` regression would hash a
   zero threshold exactly like no threshold at all.

3. **Only fully-elapsed periods are scored, and an undated payment is counted
   rather than proxy-dated.** Both are asserted where they bite: an
   in-progress period carrying real settled cash contributes nothing to any
   total, and a `completed` payment with no `completed_at` whose `created_at`
   sits inside an ELAPSED window still leaves that period's actual at zero.

4. **Consolidated scope is discovered, never declared.** An entity-scoped plan
   enacted with that entity selected stages only that entity's commitments; a
   `consolidated: true` smuggled into the replay body cannot rescue an id that
   hashes to neither candidate.

Plus the shared money-path gates the draft-run route inherits from
`services.payment_runs.create_payment_run_for_invoices` (payable status,
financial-integrity exception, credit-memo netting, single currency) — proven
THROUGH this route, because inheriting a gate is only worth anything if the
caller actually reaches it.

Every money figure crossing the API is asserted to be an exact decimal string;
`_assert_no_float` walks each response for a `float` that would mean a JSON
number slipped in.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text, update

from app.utils.dates import utc_today

# ===========================================================================
# Helpers
# ===========================================================================

#: The plan-defining parameter set every enact/save route replays. Kept as one
#: dict so a test that tampers with a single field is obviously doing that.
_BOGUS_REPLAY = {
    "granularity": "week",
    "horizon_days": 90,
    "min_balance_threshold": None,
    "cash_budget": None,
    "cost_of_capital_pct": "8.0",
}


async def _default_entity_id(session, org_id):
    row = (
        await session.execute(
            text("SELECT id FROM entities WHERE organization_id = :o AND is_default"),
            {"o": org_id},
        )
    ).first()
    return row[0]


async def _seed_entity(session, org_id, *, slug):
    from app.models.entity import Entity

    ent = Entity(
        id=uuid.uuid4(),
        organization_id=org_id,
        name=f"Entity {slug}",
        slug=slug,
        is_default=False,
        is_active=True,
    )
    session.add(ent)
    return ent


async def _seed_invoice(
    session,
    org_id,
    entity_id,
    *,
    number,
    amount,
    status="approved",
    due_in_days=10,
    currency="USD",
    vendor_name="PlanCo",
):
    from app.models.invoice import Invoice

    inv = Invoice(
        id=uuid.uuid4(),
        organization_id=org_id,
        entity_id=entity_id,
        invoice_number=number,
        vendor_name=vendor_name,
        amount=Decimal(str(amount)),
        currency=currency,
        status=status,
        invoice_date=utc_today(),
        due_date=utc_today() + timedelta(days=due_in_days),
    )
    session.add(inv)
    return inv


async def _seed_payment(
    session,
    entity_id,
    invoice_id,
    *,
    amount,
    completed_at,
    status="completed",
    created_at=None,
):
    """A `Payment` carries no `organization_id` — it is scoped through its
    invoice and the tenant database, which is why the variance query filters on
    entity + status only. `created_at` is settable so the undated-payment path
    can be exercised inside a chosen window."""
    from app.models.payment import Payment

    pay = Payment(
        id=uuid.uuid4(),
        entity_id=entity_id,
        invoice_id=invoice_id,
        amount=Decimal(str(amount)),
        status=status,
        completed_at=completed_at,
    )
    if created_at is not None:
        pay.created_at = created_at
    session.add(pay)
    return pay


def _at(day, hour=9):
    return datetime.combine(day, time.min, tzinfo=UTC) + timedelta(hours=hour)


def _assert_money_str(value, path):
    assert isinstance(value, str), f"{path} should be an exact string, got {type(value)}: {value!r}"
    Decimal(value)


def _assert_no_float(obj, *, path="result"):
    if isinstance(obj, float):
        raise AssertionError(f"float found at {path}: {obj!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _assert_no_float(v, path=f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_no_float(v, path=f"{path}[{i}]")


def _replay_body(plan, **extra) -> dict:
    """The replay body a well-behaved frontend sends back: the plan's own
    RESOLVED defining fields, verbatim."""
    body = {
        "granularity": plan.granularity,
        "horizon_days": plan.horizon_days,
        "min_balance_threshold": (
            str(plan.min_balance_threshold) if plan.min_balance_threshold is not None else None
        ),
        "cash_budget": str(plan.cash_budget) if plan.cash_budget is not None else None,
        "cost_of_capital_pct": str(plan.cost_of_capital_pct),
    }
    body.update(extra)
    return body


async def _propose_plan(realdb, key="a", *, entity_id=None, **param_overrides):
    from app.services.assistant.tools.cashflow import propose_payment_plan
    from app.services.assistant.tools.schemas import ProposePaymentPlanParams

    info = realdb.info(key)
    mk = realdb.sessionmaker(key)
    ctrl_mk = realdb.control_sessionmaker()
    async with mk() as sa, ctrl_mk() as ctrl:
        return await propose_payment_plan(
            sa,
            org_id=info.org_id,
            entity_id=entity_id,
            current_user_id=info.users["admin"],
            control_db=ctrl,
            params=ProposePaymentPlanParams(granularity="week", **param_overrides),
        )


async def _set_org_settings(realdb, key, settings_dict):
    from app.models.organization import Organization

    info = realdb.info(key)
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as ctrl:
        await ctrl.execute(
            update(Organization)
            .where(Organization.id == info.org_id)
            .values(settings=settings_dict)
        )
        await ctrl.commit()


async def _create_offer(realdb, invoice_id, *, days=5, percent="3.00", role="ap_manager"):
    async with realdb.client(key="a", role=role) as c:
        resp = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": str(invoice_id),
                "tiers": [{"days": days, "percent": percent}],
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _raw_snapshot(realdb, plan_id):
    """The stored row as Postgres holds it: the JSONB curve as TEXT (so a
    re-serialisation is visible) plus the timestamps."""
    mk = realdb.sessionmaker("a")
    async with mk() as sa:
        row = (
            (
                await sa.execute(
                    text(
                        "SELECT periods::text AS periods, selected_offer_ids::text AS offers, "
                        "opening_balance, total_savings_selected, created_at, updated_at "
                        "FROM cash_plans WHERE plan_id = :p"
                    ),
                    {"p": plan_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


# ===========================================================================
# 1. `plan_id` determinism — the pure hash every other guarantee rests on
#
# The id is the replay key, the draft-run idempotency anchor
# (`payment_runs.plan_id`), the saved-snapshot key (`uq_cash_plans_org_plan_id`)
# AND the carrier of the entity scope §55 discovers. A field dropping out of
# the preimage would silently make two different plans one plan.
# ===========================================================================

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ENTITY_A = uuid.UUID("33333333-3333-3333-3333-333333333333")
_ENTITY_B = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _plan_kwargs(**overrides):
    base = {
        "org_id": _ORG,
        "entity_id": None,
        "granularity": "week",
        "horizon_days": 90,
        "min_balance_threshold": Decimal("1000.00"),
        "cash_budget": Decimal("5000.00"),
        "cost_of_capital_pct": Decimal("8.0"),
        "today": utc_today(),
    }
    base.update(overrides)
    return base


def test_plan_id_is_a_pure_function_of_its_resolved_inputs():
    """Same inputs, same id — every time, in any order, with no random state.

    `compute_plan_id` is UUID5, not `uuid4`, precisely so a plan proposed in
    one request can be re-derived in the enact request that follows."""
    from app.services.cash_flow_plan import compute_plan_id

    kwargs = _plan_kwargs()
    first = compute_plan_id(**kwargs)
    assert first == compute_plan_id(**kwargs)
    assert first == compute_plan_id(**_plan_kwargs())
    # A UUID string, so it fits `payment_runs.plan_id` / `cash_plans.plan_id`
    # (both String(64)).
    assert str(uuid.UUID(first)) == first


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("org_id", _OTHER_ORG),
        ("entity_id", _ENTITY_A),
        ("granularity", "month"),
        ("horizon_days", 91),
        ("min_balance_threshold", Decimal("1000.01")),
        ("cash_budget", Decimal("5000.01")),
        ("cost_of_capital_pct", Decimal("8.1")),
        ("today", utc_today() - timedelta(days=1)),
    ],
)
def test_every_defining_input_moves_the_plan_id(field, value):
    """Each of the eight preimage components must change the id.

    A component silently dropped from the join (or a resolution step that
    stops feeding it in) makes two genuinely different plans share one id —
    which would let a draft run staged for yesterday's horizon satisfy today's
    replay, and let one saved snapshot answer for both."""
    from app.services.cash_flow_plan import compute_plan_id

    assert compute_plan_id(**_plan_kwargs()) != compute_plan_id(**_plan_kwargs(**{field: value}))


def test_plan_id_hashes_the_entity_scope_so_two_entities_are_two_plans():
    """§55 rests entirely on this: scope discovery is safe because exactly two
    ids can be legitimate for a caller (their entity's and the consolidated
    one), and an id built under a DIFFERENT entity matches neither."""
    from app.services.cash_flow_plan import compute_plan_id

    consolidated = compute_plan_id(**_plan_kwargs(entity_id=None))
    a = compute_plan_id(**_plan_kwargs(entity_id=_ENTITY_A))
    b = compute_plan_id(**_plan_kwargs(entity_id=_ENTITY_B))
    assert len({consolidated, a, b}) == 3


@pytest.mark.parametrize("field", ["min_balance_threshold", "cash_budget"])
def test_an_omitted_optional_cannot_hash_like_a_zero_one(field):
    """`Decimal("0")` is FALSY. A `str(value or "-")` refactor of the preimage
    would hash "no threshold configured" and "a threshold of zero" identically
    — two materially different plans (one has no floor at all, the other must
    never go below zero) collapsing onto one id, one draft run and one saved
    baseline."""
    from app.services.cash_flow_plan import compute_plan_id

    omitted = compute_plan_id(**_plan_kwargs(**{field: None}))
    zero = compute_plan_id(**_plan_kwargs(**{field: Decimal("0")}))
    assert omitted != zero


def test_the_decimal_scale_is_part_of_the_preimage():
    """`Decimal("8")` and `Decimal("8.00")` are equal numbers with different
    string forms, and the preimage is built from `str()`.

    So a replay must send the plan's own figures back VERBATIM (which is what
    `PaymentPlanResult` carries and the plan card renders). Asserted here
    rather than left to be discovered as a mystery 409: the safe half is that
    a mismatch is refused, never silently acted on — see
    `test_a_rescaled_replay_value_is_refused_not_silently_accepted`.
    """
    from app.services.cash_flow_plan import compute_plan_id

    assert compute_plan_id(**_plan_kwargs(min_balance_threshold=Decimal("8"))) != compute_plan_id(
        **_plan_kwargs(min_balance_threshold=Decimal("8.00"))
    )


def test_no_two_distinct_parameter_tuples_share_an_id():
    """A field-boundary collision check over the whole preimage.

    The components are joined with `|`, so a value carrying the delimiter (or
    a sentinel colliding with a real value) is the shape of bug that makes two
    plans one. 2 x 2 x 3 x 2 x 3 x 2 x 2 distinct tuples must produce that
    many distinct ids."""
    from app.services.cash_flow_plan import compute_plan_id

    today = utc_today()
    grid = list(
        itertools.product(
            [_ORG, _OTHER_ORG],
            [None, _ENTITY_A],
            ["day", "week", "month"],
            [7, 90],
            [None, Decimal("0"), Decimal("1000.00")],
            [None, Decimal("5000.00")],
            [Decimal("8.0"), Decimal("12.5")],
        )
    )
    ids = {
        compute_plan_id(
            org_id=org,
            entity_id=ent,
            granularity=gran,
            horizon_days=horizon,
            min_balance_threshold=threshold,
            cash_budget=budget,
            cost_of_capital_pct=cost,
            today=today,
        )
        for org, ent, gran, horizon, threshold, budget, cost in grid
    }
    assert len(ids) == len(grid), "two distinct plans collided onto one plan_id"


# ===========================================================================
# 2. The frozen baseline (§54) — the highest-value assertion in this file
# ===========================================================================


async def test_a_repeat_save_returns_the_first_snapshot_even_after_the_world_moves(realdb):
    """A saved plan is the baseline a variance is measured against, so a second
    save must return the FIRST snapshot untouched — not restate it.

    The test is deliberately in three acts, because the middle one is what
    stops it passing vacuously:

    1. save a plan over one 500.00 commitment;
    2. move the world underneath it — a second, much larger in-horizon
       invoice, a discount offer on it, and a persisted opening balance — and
       PROVE a fresh proposal for the *same* `plan_id` now projects a
       different curve. `plan_id` hashes only the resolved parameters and the
       date, none of which moved, so the id is identical while the projection
       is not: exactly the situation in which an upsert would destroy the
       baseline;
    3. save again, and assert the response payload, the stored JSONB and even
       `updated_at` are unchanged.

    Without act 2 an upsert-on-save regression reads green (restating
    identical data looks like not restating it at all).
    """
    from app.models.cash_plan import CashPlan

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent, number="FROZE-BASE", amount="500.00", due_in_days=5)
        await sa.commit()

    plan = await _propose_plan(realdb)
    body = _replay_body(plan, label="baseline")

    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=body)
    assert created.status_code == 201, created.text
    first = created.json()["plan"]
    raw_before = await _raw_snapshot(realdb, plan.plan_id)

    # --- act 2: move the world, and prove the projection really would move ---
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa, a.org_id, ent, number="FROZE-LATER", amount="9999.00", due_in_days=5
        )
        discountable = await _seed_invoice(
            sa, a.org_id, ent, number="FROZE-OFFER", amount="2000.00", due_in_days=30
        )
        await sa.commit()
        discountable_id = discountable.id
    await _create_offer(realdb, discountable_id)
    await _set_org_settings(realdb, "a", {"cashflow": {"opening_balance": "77777.00"}})

    fresh = await _propose_plan(realdb)
    assert fresh.plan_id == plan.plan_id, (
        "the parameters and the date are unchanged, so the id must be too — "
        "that is precisely why a repeat save must not restate the snapshot"
    )
    frozen_outflows = [Decimal(p["outflow"]) for p in first["periods"]]
    fresh_outflows = [p.outflow for p in fresh.periods]
    assert frozen_outflows != fresh_outflows, (
        "non-vacuity check failed: the world did not actually move, so this "
        "test could not tell a frozen baseline from a restated one"
    )
    assert fresh.opening_balance == Decimal("77777.00")
    assert Decimal(first["opening_balance"]) != fresh.opening_balance
    # The discount side of the snapshot is frozen too: the offer raised after
    # the save is selected by a fresh pass and must never appear on the
    # baseline (a variance reports follow-through against the offers the plan
    # ACTUALLY recommended).
    assert first["selected_offer_ids"] == []
    assert [r.offer_id for r in fresh.discount_recommendations if r.selected], (
        "expected the post-save offer to be worthwhile, so the snapshot's "
        "empty selected_offer_ids is a real difference"
    )

    # --- act 3: the second save returns the first snapshot, byte for byte ---
    async with realdb.client(key="a", role="admin") as c:
        again = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=body)
        detail = await c.get(f"/api/cash-flow/plans/{plan.plan_id}")
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert again.json()["plan"] == first
    assert detail.status_code == 200, detail.text
    assert detail.json() == first

    raw_after = await _raw_snapshot(realdb, plan.plan_id)
    assert raw_after == raw_before, (
        "the stored snapshot changed — a repeat save must not touch the row "
        "(periods JSONB, money columns and updated_at included)"
    )

    async with mk_a() as sa:
        rows = (
            (await sa.execute(select(CashPlan).where(CashPlan.plan_id == plan.plan_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1, "one snapshot per plan_id — uq_cash_plans_org_plan_id is the anchor"


async def test_a_repeat_save_writes_no_second_audit_row(realdb):
    """The audit trail says a baseline was taken. A repeat save takes no new
    baseline, so it must not claim to have taken one."""
    from app.models.workflow import AuditLog

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent, number="AUDIT-1", amount="400.00", due_in_days=6)
        await sa.commit()

    plan = await _propose_plan(realdb)
    body = _replay_body(plan)
    async with realdb.client(key="a", role="admin") as c:
        assert (
            await c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=body)
        ).status_code == 201
        assert (
            await c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=body)
        ).status_code == 200

    async with mk_a() as sa:
        rows = (
            (await sa.execute(select(AuditLog).where(AuditLog.action == "cash_plan.saved")))
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_a_concurrent_double_save_stores_exactly_one_snapshot(realdb):
    """Two in-flight saves of the same plan (a double-clicked button, a client
    retry racing the original) must converge on one row.

    The short-circuit read cannot cover this — both requests can pass it — so
    `uq_cash_plans_org_plan_id` + the `IntegrityError` branch is what holds,
    and the loser returns the WINNER's snapshot rather than an error.
    """
    from app.models.cash_plan import CashPlan

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent, number="RACE-1", amount="800.00", due_in_days=8)
        await sa.commit()

    plan = await _propose_plan(realdb)
    body = _replay_body(plan)
    async with realdb.client(key="a", role="admin") as c:
        first, second = await asyncio.gather(
            c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=body),
            c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=body),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 201], (
        f"{first.status_code} / {second.status_code}: {first.text} {second.text}"
    )
    assert first.json()["plan"] == second.json()["plan"], (
        "the loser must return the winner's baseline, not its own re-derivation"
    )

    async with mk_a() as sa:
        rows = (
            (await sa.execute(select(CashPlan).where(CashPlan.plan_id == plan.plan_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_the_stored_curve_holds_money_as_exact_strings_not_json_numbers(realdb):
    """The frozen curve lives in JSONB, and every JSON codec on that path
    round-trips a bare number through `float`.

    `freeze_periods` is unit-tested; this asserts what actually LANDED in
    Postgres, which is what a later "just store numbers, it's simpler" change
    would break.
    """
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent, number="JSONB-1", amount="1234.56", due_in_days=4)
        await sa.commit()

    plan = await _propose_plan(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=_replay_body(plan))
    assert resp.status_code == 201, resp.text
    _assert_no_float(resp.json(), path="save")

    async with mk_a() as sa:
        stored = (
            await sa.execute(
                text("SELECT periods FROM cash_plans WHERE plan_id = :p"), {"p": plan.plan_id}
            )
        ).scalar_one()

    assert stored, "the curve must be persisted, not an empty list"
    for i, period in enumerate(stored):
        for key in ("opening", "outflow", "closing"):
            value = period[key]
            assert isinstance(value, str), (
                f"periods[{i}].{key} stored as {type(value)} ({value!r}) — money in JSONB must "
                "be an exact decimal string; a JSON number round-trips through float"
            )
            Decimal(value)
        assert isinstance(period["below_threshold"], bool)
        assert isinstance(period["unconverted_count"], int)


# ===========================================================================
# 3. The stale-plan guard — on every route that takes a replay body
# ===========================================================================

_PLAN_BODY_ROUTES = ("save", "draft-run", "capture-discounts")

_TAMPERS = [
    ("granularity", "month"),
    ("horizon_days", 30),
    ("min_balance_threshold", "500.00"),
    ("cash_budget", "250.00"),
    ("cost_of_capital_pct", "9.50"),
]


async def _seed_payable_invoice_with_offer(realdb):
    """One approved, in-horizon invoice carrying an open discount offer — so a
    refused call has something it COULD have staged and something it COULD
    have accepted, making "nothing happened" a real assertion."""
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        inv = await _seed_invoice(
            sa, a.org_id, ent, number="STALE-1", amount="1000.00", due_in_days=30
        )
        await sa.commit()
        inv_id = inv.id
    offer_id = await _create_offer(realdb, inv_id)
    return inv_id, offer_id


async def _assert_nothing_enacted(realdb, *, offer_id):
    from app.models.cash_plan import CashPlan
    from app.models.discount import DiscountOffer
    from app.models.payment import Payment, PaymentRun

    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        assert (await sa.execute(select(CashPlan))).scalars().all() == []
        assert (await sa.execute(select(PaymentRun))).scalars().all() == []
        assert (await sa.execute(select(Payment))).scalars().all() == []
        offer = (
            await sa.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(offer_id)))
        ).scalar_one()
        assert offer.status == "offered"
        assert offer.accepted_at is None


async def test_an_exact_budget_survives_propose_then_replay(realdb):
    """The plan-id round trip holds for a budget no ``float`` could carry.

    This is the guard on the fix, not just on the parse. `compute_plan_id`
    hashes ``str(cash_budget)``, so propose and replay MUST resolve the same
    wire value to the same `Decimal` — including its scale. If either side
    normalised, rounded, or re-scaled it, a plan whose parameters had not
    changed would start failing its own stale-plan check with a 409 nobody
    could explain.
    """
    _inv_id, offer_id = await _seed_payable_invoice_with_offer(realdb)
    exact = "9799.999999999999999"
    assert Decimal(str(float(exact))) != Decimal(exact), (
        "fixture assumption: this value does not survive a float round-trip"
    )

    plan = await _propose_plan(realdb, cash_budget=exact)
    # Echoed back verbatim, digit for digit — that is what makes the replay
    # reproducible at all.
    assert str(plan.cash_budget) == exact

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["plan_id"] == plan.plan_id


async def test_a_float_budget_would_have_certified_a_different_plan(realdb):
    """Why the budget's exactness reaches money, one surface past the parse.

    Rounding the budget changes the id, and the id is what
    ``POST /plans/{plan_id}/draft-run`` treats as certifying the figure the
    plan was built from. So the rounded and the exact budget are two DIFFERENT
    plans — and a client replaying the exact value against the rounded plan's
    id is refused, which is precisely the confusion an accepted float would
    have created silently.
    """
    _inv_id, offer_id = await _seed_payable_invoice_with_offer(realdb)
    exact = "9799.999999999999999"
    rounded = str(float(exact))  # "9800.0" — what the old parse produced

    exact_plan = await _propose_plan(realdb, cash_budget=exact)
    rounded_plan = await _propose_plan(realdb, cash_budget=rounded)
    assert exact_plan.plan_id != rounded_plan.plan_id

    # The exact parameters do not hash to the rounded plan's id, so enacting
    # under it is a clean 409 rather than a run staged against a budget the
    # user never chose.
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{rounded_plan.plan_id}/draft-run",
            json=_replay_body(exact_plan),
        )
    assert resp.status_code == 409, resp.text
    await _assert_nothing_enacted(realdb, offer_id=offer_id)


async def test_a_json_number_budget_is_refused_by_every_plan_body_route(realdb):
    """The wire shape is part of the contract on the enact side too.

    A shipped client already sends these as strings
    (``frontend/src/lib/api/cashFlow.ts`` ``PlanReplayParams``), so this closes
    a door nothing walks through — but the two sides have to agree, or the
    stale-plan guard starts refusing plans that are actually identical.
    """
    _inv_id, offer_id = await _seed_payable_invoice_with_offer(realdb)
    plan = await _propose_plan(realdb, cash_budget="5000.00")
    body = _replay_body(plan)
    body["cash_budget"] = 5000.00  # a JSON number, not a decimal string

    async with realdb.client(key="a", role="admin") as c:
        for route in _PLAN_BODY_ROUTES:
            resp = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/{route}", json=body)
            assert resp.status_code == 422, f"{route}: {resp.text}"
            assert "string" in resp.text.lower()
    await _assert_nothing_enacted(realdb, offer_id=offer_id)


@pytest.mark.parametrize("route", _PLAN_BODY_ROUTES)
@pytest.mark.parametrize(("field", "value"), _TAMPERS, ids=[t[0] for t in _TAMPERS])
async def test_a_tampered_replay_parameter_is_refused_on_every_plan_route(
    realdb, route, field, value
):
    """Each defining parameter, tampered one at a time, on each route that
    takes a replay body.

    The guard is what stops a client acting on a plan its own body no longer
    describes — the enact routes re-derive everything from these parameters, so
    a mismatch means the server would stage a DIFFERENT set of invoices (or
    accept a different set of offers) than the plan the user approved on
    screen. Refusal must also be side-effect-free.
    """
    _inv_id, offer_id = await _seed_payable_invoice_with_offer(realdb)
    plan = await _propose_plan(realdb)
    body = _replay_body(plan)
    body[field] = value

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/{route}", json=body)
    assert resp.status_code == 409, resp.text
    await _assert_nothing_enacted(realdb, offer_id=offer_id)


@pytest.mark.parametrize("route", _PLAN_BODY_ROUTES)
async def test_yesterdays_plan_id_is_refused_today(realdb, route):
    """The calendar date is part of the preimage on purpose: it decides which
    commitments are in-horizon, so yesterday's plan is a different plan.

    A client holding a plan card open overnight must be told to ask for a
    fresh plan, not have last night's horizon staged against this morning's
    ledger."""
    from app.services.cash_flow_plan import compute_plan_id

    a = realdb.info("a")
    _inv_id, offer_id = await _seed_payable_invoice_with_offer(realdb)
    plan = await _propose_plan(realdb)
    yesterdays_id = compute_plan_id(
        org_id=a.org_id,
        entity_id=None,
        granularity=plan.granularity,
        horizon_days=plan.horizon_days,
        min_balance_threshold=plan.min_balance_threshold,
        cash_budget=plan.cash_budget,
        cost_of_capital_pct=plan.cost_of_capital_pct,
        today=utc_today() - timedelta(days=1),
    )
    assert yesterdays_id != plan.plan_id

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{yesterdays_id}/{route}", json=_replay_body(plan)
        )
    assert resp.status_code == 409, resp.text
    await _assert_nothing_enacted(realdb, offer_id=offer_id)


async def test_a_rescaled_replay_value_is_refused_not_silently_accepted(realdb):
    """The safe half of `test_the_decimal_scale_is_part_of_the_preimage`.

    A client that "normalises" `8.0` to `8` before replaying no longer hashes
    to the id it was given. That must be a clean 409 — the server never acts
    on a plan it cannot reproduce — rather than a 500 or, far worse, a silent
    accept against re-derived parameters the user never saw."""
    _inv_id, offer_id = await _seed_payable_invoice_with_offer(realdb)
    plan = await _propose_plan(realdb)
    assert str(plan.cost_of_capital_pct) == "8.0", (
        "fixture assumption: the platform default cost of capital stringifies "
        "with one decimal place"
    )
    body = _replay_body(plan, cost_of_capital_pct="8")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=body)
    assert resp.status_code == 409, resp.text
    await _assert_nothing_enacted(realdb, offer_id=offer_id)


@pytest.mark.parametrize("route", _PLAN_BODY_ROUTES)
async def test_a_bogus_plan_id_is_refused_on_every_plan_route(realdb, route):
    """An id that hashes from nothing at all is the same clean 409 — never a
    404 that would confirm which ids exist, and never a 500 on an
    unparseable value."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/cash-flow/plans/not-a-real-plan-id/{route}", json=_BOGUS_REPLAY)
    assert resp.status_code == 409, resp.text


# ===========================================================================
# 4. Plan-vs-actual — only fully-elapsed periods are scored (§54)
# ===========================================================================


def _plan_period(*, start, end, outflow):
    from app.services.cash_flow_plan import PlanPeriod

    return PlanPeriod(
        period=start.isoformat(),
        period_start=start,
        period_end=end,
        opening=Decimal("0"),
        outflow=Decimal(outflow),
        closing=Decimal("0"),
        below_threshold=False,
    )


def _week_period(anchor, outflow):
    from app.services.analytics import _period_bounds
    from app.services.cash_flow_plan import PlanPeriod

    label, start, end = _period_bounds(anchor, "week")
    return PlanPeriod(
        period=label,
        period_start=start,
        period_end=end,
        opening=Decimal("0"),
        outflow=Decimal(outflow),
        closing=Decimal("0"),
        below_threshold=False,
    )


@pytest.mark.parametrize(
    ("start_offset", "end_offset", "expected"),
    [
        (-14, -8, "elapsed"),  # closed last week
        (-7, -1, "elapsed"),  # closed yesterday — the last day that counts
        (-6, 0, "in_progress"),  # ends TODAY: the window has not closed yet
        (0, 6, "in_progress"),  # starts today
        (1, 7, "future"),
    ],
)
def test_a_periods_status_is_decided_by_whether_its_window_has_closed(
    start_offset, end_offset, expected
):
    """The boundary that the whole rule turns on: a period is `elapsed` only
    once its END is strictly in the past.

    A period ending TODAY is still in progress — more cash can leave before
    midnight — so scoring it would compare a whole projection against a
    partial actual. That off-by-one is the manufactured-underspend bug."""
    from app.services.cash_flow_plan import compare_plan_to_actual

    as_of = utc_today()
    period = _plan_period(
        start=as_of + timedelta(days=start_offset),
        end=as_of + timedelta(days=end_offset),
        outflow="100.00",
    )
    result = compare_plan_to_actual([period], {}, as_of=as_of)
    assert result.periods[0].status == expected


def test_an_in_progress_period_with_real_cash_is_reported_but_never_scored():
    """The manufactured underspend, in isolation.

    Half-way through the week 200 of a projected 1000 has left. The period ROW
    must still say so — a reader wants the shape of what is coming — while
    every TOTAL excludes it, because "we are 800 under plan" is a reading that
    reverses by Friday.
    """
    from app.services.cash_flow_plan import compare_plan_to_actual

    as_of = utc_today()
    period = _plan_period(
        start=as_of - timedelta(days=2), end=as_of + timedelta(days=4), outflow="1000.00"
    )
    result = compare_plan_to_actual([period], {period.period: Decimal("200.00")}, as_of=as_of)

    row = result.periods[0]
    assert row.status == "in_progress"
    assert row.actual_outflow == Decimal("200.00"), "the partial actual is still reported"
    assert row.variance == Decimal("-800.00"), "the row's own arithmetic is unchanged"
    # …but nothing about it reaches the score.
    assert result.planned_total == Decimal("0")
    assert result.actual_total == Decimal("0")
    assert result.variance_total == Decimal("0")
    assert result.elapsed_period_count == 0
    assert result.open_period_count == 1


def test_only_elapsed_periods_contribute_to_the_totals():
    from app.services.cash_flow_plan import compare_plan_to_actual

    as_of = utc_today()
    closed_a = _plan_period(
        start=as_of - timedelta(days=21), end=as_of - timedelta(days=15), outflow="100.00"
    )
    closed_b = _plan_period(
        start=as_of - timedelta(days=14), end=as_of - timedelta(days=8), outflow="200.00"
    )
    running = _plan_period(
        start=as_of - timedelta(days=3), end=as_of + timedelta(days=3), outflow="400.00"
    )
    coming = _plan_period(
        start=as_of + timedelta(days=7), end=as_of + timedelta(days=13), outflow="800.00"
    )
    actuals = {
        closed_a.period: Decimal("150.00"),
        closed_b.period: Decimal("200.00"),
        running.period: Decimal("50.00"),
        coming.period: Decimal("0"),
    }

    result = compare_plan_to_actual([closed_a, closed_b, running, coming], actuals, as_of=as_of)
    assert result.elapsed_period_count == 2
    assert result.open_period_count == 2
    assert result.planned_total == Decimal("300.00")
    assert result.actual_total == Decimal("350.00")
    assert result.variance_total == Decimal("50.00")
    assert [p.status for p in result.periods] == [
        "elapsed",
        "elapsed",
        "in_progress",
        "future",
    ]


def test_cash_in_a_period_the_plan_never_projected_is_surfaced_not_summed():
    """Real settled cash that belongs to no planned period cannot be added to
    `actual_total` (it has no `planned` counterpart to be a variance against)
    and must not be dropped either — a total that quietly omits money that
    really left is the worst of the three options."""
    from app.services.cash_flow_plan import compare_plan_to_actual

    as_of = utc_today()
    planned = _plan_period(
        start=as_of - timedelta(days=14), end=as_of - timedelta(days=8), outflow="100.00"
    )
    result = compare_plan_to_actual(
        [planned],
        {planned.period: Decimal("100.00"), "1999-01-04": Decimal("42.00")},
        as_of=as_of,
    )
    assert result.actual_total == Decimal("100.00")
    assert result.variance_total == Decimal("0")
    assert result.unmatched_actual_periods == ["1999-01-04"]
    assert result.unmatched_actual_total == Decimal("42.00")


# --- the same rules through the HTTP surface -------------------------------


async def _insert_snapshot(
    realdb,
    *,
    entity_id,
    periods,
    plan_date,
    granularity="week",
    currency="USD",
    selected_offer_ids=None,
):
    """Insert a `CashPlan` snapshot directly and return its `plan_id`.

    The variance endpoint's contract is "given a saved snapshot, score it",
    and a plan proposed today can only ever hold in-progress/future periods —
    so exercising the ELAPSED path needs a historical row, which is exactly
    what this table stores."""
    from app.models.cash_plan import CashPlan
    from app.services.cash_flow_plan import compute_plan_id, freeze_periods

    a = realdb.info("a")
    plan_id = compute_plan_id(
        org_id=a.org_id,
        entity_id=entity_id,
        granularity=granularity,
        horizon_days=90,
        min_balance_threshold=None,
        cash_budget=None,
        cost_of_capital_pct=Decimal("8.0"),
        today=plan_date,
    )
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        sa.add(
            CashPlan(
                id=uuid.uuid4(),
                organization_id=a.org_id,
                entity_id=entity_id,
                plan_id=plan_id,
                plan_date=plan_date,
                label="historical",
                granularity=granularity,
                horizon_days=90,
                min_balance_threshold=None,
                cash_budget=None,
                cost_of_capital_pct=Decimal("8.0"),
                currency=currency,
                opening_balance=Decimal("0"),
                first_shortfall_period=None,
                total_savings_selected=Decimal("0"),
                total_outlay_selected=Decimal("0"),
                unconverted_count=0,
                periods=freeze_periods(periods, granularity),
                selected_offer_ids=list(selected_offer_ids or []),
                unretimed_offer_ids=[],
            )
        )
        await sa.commit()
    return plan_id


async def test_variance_never_dates_an_undated_payment_by_a_proxy(realdb):
    """A `completed` payment with no `completed_at` is COUNTED, not placed.

    `created_at` bounds whether such a payment is in scope for this plan's
    window; using it as the settlement date would be inventing evidence. The
    test puts the undated payment's `created_at` squarely inside a FULLY
    ELAPSED period alongside a genuinely dated one, so a proxy-dating
    regression is visible as that period's actual jumping from 250 to 1000.
    """
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    today = utc_today()
    anchor = today - timedelta(days=14)

    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        dated = await _seed_invoice(sa, a.org_id, ent, number="UND-DATED", amount="250.00")
        undated = await _seed_invoice(sa, a.org_id, ent, number="UND-NONE", amount="750.00")
        await sa.flush()
        await _seed_payment(sa, ent, dated.id, amount="250.00", completed_at=_at(anchor))
        await _seed_payment(
            sa,
            ent,
            undated.id,
            amount="750.00",
            completed_at=None,
            created_at=_at(anchor, hour=11),
        )
        await sa.commit()

    plan_id = await _insert_snapshot(
        realdb,
        entity_id=None,
        periods=[_week_period(anchor, "1000.00")],
        plan_date=anchor - timedelta(days=7),
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/cash-flow/plans/{plan_id}/variance")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    _assert_no_float(data, path="variance")

    row = next(p for p in data["periods"] if p["status"] == "elapsed")
    _assert_money_str(row["actual_outflow"], "periods[].actual_outflow")
    assert Decimal(row["actual_outflow"]) == Decimal("250.00"), (
        "the undated payment must not be dated into this period by its created_at"
    )
    assert Decimal(data["actual_total"]) == Decimal("250.00")
    assert Decimal(data["variance_total"]) == Decimal("-750.00")
    assert data["undated_payment_count"] == 1


async def test_variance_counts_only_settled_cash(realdb):
    """A payment still in flight is not cash that left. Scoring it would
    report a variance against money the rail has not moved."""
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    today = utc_today()
    anchor = today - timedelta(days=14)

    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        inflight = await _seed_invoice(sa, a.org_id, ent, number="INFLIGHT-1", amount="600.00")
        pending = await _seed_invoice(sa, a.org_id, ent, number="PENDING-1", amount="300.00")
        await sa.flush()
        # `completed_at` deliberately SET on a non-completed row: the filter is
        # on status, so a rail that stamps a timestamp early can't leak in.
        await _seed_payment(
            sa, ent, inflight.id, amount="600.00", completed_at=_at(anchor), status="submitted"
        )
        await _seed_payment(
            sa, ent, pending.id, amount="300.00", completed_at=None, status="pending"
        )
        await sa.commit()

    plan_id = await _insert_snapshot(
        realdb,
        entity_id=None,
        periods=[_week_period(anchor, "900.00")],
        plan_date=anchor - timedelta(days=7),
    )

    async with realdb.client(key="a", role="admin") as c:
        data = (await c.get(f"/api/cash-flow/plans/{plan_id}/variance")).json()

    row = next(p for p in data["periods"] if p["status"] == "elapsed")
    assert Decimal(row["actual_outflow"]) == Decimal("0")
    assert Decimal(data["actual_total"]) == Decimal("0")
    assert Decimal(data["variance_total"]) == Decimal("-900.00")
    assert data["undated_payment_count"] == 0, (
        "an undated payment that never completed is not an unplaceable settlement"
    )


async def test_variance_totals_ignore_the_running_periods_settled_cash(realdb):
    """End to end: an elapsed week scores, the running week does not — even
    though real money has already left in it."""
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    today = utc_today()
    anchor = today - timedelta(days=14)

    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        closed = await _seed_invoice(sa, a.org_id, ent, number="CLOSED-1", amount="100.00")
        running = await _seed_invoice(sa, a.org_id, ent, number="RUNNING-1", amount="400.00")
        await sa.flush()
        await _seed_payment(sa, ent, closed.id, amount="100.00", completed_at=_at(anchor))
        await _seed_payment(sa, ent, running.id, amount="400.00", completed_at=_at(today, hour=1))
        await sa.commit()

    plan_id = await _insert_snapshot(
        realdb,
        entity_id=None,
        periods=[_week_period(anchor, "100.00"), _week_period(today, "500.00")],
        plan_date=anchor - timedelta(days=7),
    )

    async with realdb.client(key="a", role="admin") as c:
        data = (await c.get(f"/api/cash-flow/plans/{plan_id}/variance")).json()

    by_status = {p["status"]: p for p in data["periods"]}
    assert Decimal(by_status["elapsed"]["actual_outflow"]) == Decimal("100.00")
    assert Decimal(by_status["in_progress"]["actual_outflow"]) == Decimal("400.00"), (
        "the running period's real cash is still reported"
    )
    assert Decimal(data["planned_total"]) == Decimal("100.00")
    assert Decimal(data["actual_total"]) == Decimal("100.00")
    assert Decimal(data["variance_total"]) == Decimal("0"), (
        "scoring the running week would have manufactured a -100 underspend"
    )
    assert data["elapsed_period_count"] == 1
    assert data["open_period_count"] == 1


async def test_variance_with_no_elapsed_period_says_not_scored_rather_than_on_plan(realdb):
    """A plan saved today has nothing to score yet.

    Its totals are zero — but so are the totals of a plan that came in exactly
    on budget, so the response must let a reader tell those apart:
    `elapsed_period_count == 0` with the period rows carrying their real
    actuals and an honest `in_progress` label."""
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    today = utc_today()

    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        inv = await _seed_invoice(sa, a.org_id, ent, number="NOWSCORE-1", amount="400.00")
        await sa.flush()
        await _seed_payment(sa, ent, inv.id, amount="400.00", completed_at=_at(today, hour=2))
        await sa.commit()

    plan_id = await _insert_snapshot(
        realdb,
        entity_id=None,
        periods=[_week_period(today, "500.00")],
        plan_date=today,
    )

    async with realdb.client(key="a", role="admin") as c:
        data = (await c.get(f"/api/cash-flow/plans/{plan_id}/variance")).json()

    assert data["elapsed_period_count"] == 0
    assert data["open_period_count"] == 1
    assert Decimal(data["planned_total"]) == Decimal("0")
    assert Decimal(data["actual_total"]) == Decimal("0")
    assert [p["status"] for p in data["periods"]] == ["in_progress"]
    assert Decimal(data["periods"][0]["actual_outflow"]) == Decimal("400.00")
    assert Decimal(data["periods"][0]["planned_outflow"]) == Decimal("500.00")


# ===========================================================================
# 5. Consolidated scope is DISCOVERED from the plan id, never declared (§55)
# ===========================================================================


async def _two_entities_with_payables(realdb):
    """The default entity and one subsidiary, each holding one payable
    in-horizon invoice (300 and 700)."""
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        default_ent = await _default_entity_id(sa, a.org_id)
        other = await _seed_entity(sa, a.org_id, slug=f"sub{uuid.uuid4().hex[:6]}")
        await sa.flush()
        await _seed_invoice(sa, a.org_id, default_ent, number="SCOPE-DEF", amount="300.00")
        await _seed_invoice(sa, a.org_id, other.id, number="SCOPE-SUB", amount="700.00")
        await sa.commit()
        return default_ent, other.id


async def test_an_entity_plan_resolves_to_that_entity_not_the_whole_group(realdb):
    """Scope discovery tries the caller's selected entity FIRST.

    An entity-scoped plan must resolve to the entity — so the staged set is
    the set the plan reasoned about (300, not 1000) and the run row lands on
    that entity. Resolving it to the consolidated scope instead would stage a
    sibling subsidiary's invoices off a plan that never mentioned them."""
    from app.models.payment import Payment, PaymentRun

    default_ent, _other_id = await _two_entities_with_payables(realdb)

    plan = await _propose_plan(realdb, entity_id=default_ent)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run",
            json=_replay_body(plan),
            headers={"X-Entity-ID": str(default_ent)},
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["payment_count"] == 1
    assert Decimal(data["total_amount"]) == Decimal("300.00")

    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        run = (
            await sa.execute(select(PaymentRun).where(PaymentRun.plan_id == plan.plan_id))
        ).scalar_one()
        assert run.entity_id == default_ent
        assert run.status == "draft"
        payments = (await sa.execute(select(Payment))).scalars().all()
        assert len(payments) == 1


async def test_a_consolidated_plan_resolves_with_no_entity_header_at_all(realdb):
    """The single-candidate case: with no `X-Entity-ID` the only legitimate id
    is the consolidated one, and it stages every entity's commitments onto a
    run owned by the tenant's DEFAULT entity (the documented home for
    un-scoped rows)."""
    from app.models.payment import PaymentRun

    default_ent, _other_id = await _two_entities_with_payables(realdb)

    plan = await _propose_plan(realdb, entity_id=None)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["payment_count"] == 2
    assert Decimal(data["total_amount"]) == Decimal("1000.00")

    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        run = (
            await sa.execute(select(PaymentRun).where(PaymentRun.plan_id == plan.plan_id))
        ).scalar_one()
    assert run.entity_id == default_ent


@pytest.mark.parametrize("route", _PLAN_BODY_ROUTES)
async def test_a_client_cannot_declare_consolidated_to_rescue_a_foreign_plan_id(realdb, route):
    """§55 rejected a `consolidated: bool` on the replay body precisely because
    it would be a claim the server has to trust.

    Sending one anyway must change nothing: an id built under entity A, replayed
    with entity B selected, hashes to neither candidate and is still refused."""
    from app.models.cash_plan import CashPlan
    from app.models.payment import PaymentRun

    default_ent, other_id = await _two_entities_with_payables(realdb)
    plan = await _propose_plan(realdb, entity_id=default_ent)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/{route}",
            json=_replay_body(plan, consolidated=True),
            headers={"X-Entity-ID": str(other_id)},
        )
    assert resp.status_code == 409, resp.text

    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        assert (await sa.execute(select(PaymentRun))).scalars().all() == []
        assert (await sa.execute(select(CashPlan))).scalars().all() == []


async def test_a_consolidated_snapshot_is_readable_and_scorable_from_a_subsidiary_view(realdb):
    """A consolidated snapshot carries `entity_id IS NULL`, so an entity filter
    on the read path would hide it from the very view that created it.

    Detail and variance are therefore keyed by `plan_id` within the tenant,
    and the variance runs under the SAVED plan's scope rather than the
    caller's header."""
    default_ent, other_id = await _two_entities_with_payables(realdb)

    plan = await _propose_plan(realdb, entity_id=None)
    async with realdb.client(key="a", role="admin") as c:
        saved = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/save",
            json=_replay_body(plan),
            headers={"X-Entity-ID": str(other_id)},
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["plan"]["consolidated"] is True

        detail = await c.get(
            f"/api/cash-flow/plans/{plan.plan_id}", headers={"X-Entity-ID": str(other_id)}
        )
        variance = await c.get(
            f"/api/cash-flow/plans/{plan.plan_id}/variance",
            headers={"X-Entity-ID": str(default_ent)},
        )
    assert detail.status_code == 200, detail.text
    assert detail.json()["entity_id"] is None
    assert variance.status_code == 200, variance.text
    assert variance.json()["consolidated"] is True


# ===========================================================================
# 6. `/draft-run` — the shared money-path gates, reached through this route
#
# The route hands its narrowed invoice set to the SAME
# `services.payment_runs.create_payment_run_for_invoices` the manual
# `POST /api/payments/runs` uses. Inheriting a gate is worth nothing unless
# the caller reaches it, so each is exercised here rather than assumed.
# ===========================================================================


async def test_only_payable_invoices_are_staged_from_a_plans_horizon(realdb):
    """A plan's horizon deliberately includes the pre-approval pipeline (so the
    cash curve shows what is coming), and `sent_to_erp` sits between the two.

    Only `PAYABLE_INVOICE_STATUSES` may be staged: staging a
    `ready_for_review` invoice would book a payment against something nobody
    has approved."""
    from app.models.payment import Payment

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        approved = await _seed_invoice(
            sa, a.org_id, ent, number="PAY-OK", amount="300.00", status="approved"
        )
        await _seed_invoice(
            sa, a.org_id, ent, number="PAY-REVIEW", amount="5000.00", status="ready_for_review"
        )
        await _seed_invoice(
            sa, a.org_id, ent, number="PAY-ERP", amount="7000.00", status="sent_to_erp"
        )
        await sa.commit()
        approved_id = approved.id

    plan = await _propose_plan(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    _assert_no_float(data, path="draft_run")
    _assert_money_str(data["total_amount"], "total_amount")
    assert data["payment_count"] == 1
    assert Decimal(data["total_amount"]) == Decimal("300.00")
    assert data["status"] == "draft"

    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        payments = (await sa.execute(select(Payment))).scalars().all()
    assert [p.invoice_id for p in payments] == [approved_id]
    assert payments[0].status == "pending", "a draft run moves no money"


async def test_a_plan_whose_commitments_are_all_unapproved_is_refused(realdb):
    from app.models.payment import Payment, PaymentRun

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa, a.org_id, ent, number="NONE-PAYABLE", amount="900.00", status="ready_for_review"
        )
        await sa.commit()

    plan = await _propose_plan(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code == 409, resp.text
    assert "approved for payment" in resp.json()["detail"]

    async with mk_a() as sa:
        assert (await sa.execute(select(PaymentRun))).scalars().all() == []
        assert (await sa.execute(select(Payment))).scalars().all() == []


async def test_a_plan_with_an_empty_horizon_is_refused(realdb):
    """No commitments at all is its own message — an operator needs to know
    whether nothing is DUE or nothing is APPROVED."""
    plan = await _propose_plan(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code == 409, resp.text
    assert "no open commitments" in resp.json()["detail"]


async def test_a_payment_blocking_exception_refuses_the_whole_plan_run(realdb):
    """The financial-integrity gate: approval does not clear a `duplicate` /
    `fraud_flag` / `line_total_mismatch` / `payment_reconciliation` exception,
    so a human has to resolve it before any money path stages that invoice —
    including this one."""
    from app.models.exception import Exception as InvoiceException
    from app.models.payment import Payment, PaymentRun

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        inv = await _seed_invoice(sa, a.org_id, ent, number="BLOCKED-1", amount="640.00")
        await sa.flush()
        sa.add(
            InvoiceException(
                id=uuid.uuid4(),
                organization_id=a.org_id,
                entity_id=ent,
                invoice_id=inv.id,
                exception_type="duplicate",
                severity="error",
                status="open",
            )
        )
        await sa.commit()

    plan = await _propose_plan(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "duplicate" in detail, "the refusal names the exception TYPE that blocked it"

    async with mk_a() as sa:
        assert (await sa.execute(select(PaymentRun))).scalars().all() == []
        assert (await sa.execute(select(Payment))).scalars().all() == []


async def _seed_applied_credit_memo(session, org_id, entity_id, invoice_id, *, amount, number):
    from app.models.credit_memo import CreditMemo
    from app.models.invoice import Invoice
    from app.models.vendor import Vendor

    vendor = Vendor(
        id=uuid.uuid4(),
        organization_id=org_id,
        entity_id=entity_id,
        name=f"CreditVendor {number}",
    )
    session.add(vendor)
    await session.flush()
    # Linked to the memo's vendor, as the apply guard requires and the payment
    # paths re-check (`applied_credit_mismatch`, decisions §214).
    await session.execute(
        update(Invoice).where(Invoice.id == invoice_id).values(vendor_id=vendor.id)
    )
    session.add(
        CreditMemo(
            id=uuid.uuid4(),
            organization_id=org_id,
            entity_id=entity_id,
            memo_number=number,
            vendor_id=vendor.id,
            invoice_id=invoice_id,
            amount=Decimal(str(amount)),
            currency="USD",
            status="applied",
        )
    )


async def test_a_credited_invoice_is_staged_at_its_net_amount(realdb):
    """Credit-memo netting is what an applied credit is FOR, and this route
    inherits it: the run must pay 600 of a 1,000 invoice with a 400 credit
    applied, exactly like the manual payment-run path."""
    from app.models.payment import Payment

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        inv = await _seed_invoice(sa, a.org_id, ent, number="NET-1", amount="1000.00")
        await sa.flush()
        await _seed_applied_credit_memo(
            sa, a.org_id, ent, inv.id, amount="400.00", number="CM-NET-1"
        )
        await sa.commit()

    plan = await _propose_plan(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code == 201, resp.text
    assert Decimal(resp.json()["total_amount"]) == Decimal("600.00")

    async with mk_a() as sa:
        payments = (await sa.execute(select(Payment))).scalars().all()
    assert len(payments) == 1
    assert payments[0].amount == Decimal("600.00")


async def test_a_fully_credited_invoice_refuses_the_plan_run(realdb):
    """Nothing to move is not a zero-dollar payment: a real rail rejects one,
    which would strand the invoice in the payable queue."""
    from app.models.payment import Payment, PaymentRun

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        inv = await _seed_invoice(sa, a.org_id, ent, number="NET-FULL", amount="500.00")
        await sa.flush()
        await _seed_applied_credit_memo(
            sa, a.org_id, ent, inv.id, amount="500.00", number="CM-NET-FULL"
        )
        await sa.commit()

    plan = await _propose_plan(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code == 409, resp.text
    assert "credit memo" in resp.json()["detail"]

    async with mk_a() as sa:
        assert (await sa.execute(select(PaymentRun))).scalars().all() == []
        assert (await sa.execute(select(Payment))).scalars().all() == []


async def test_a_draft_run_retry_returns_the_same_run_even_after_new_payables_appear(realdb):
    """The money-write idempotency invariant, at its sharpest.

    A retry is keyed on `plan_id` (`payment_runs.plan_id`, partial-unique), NOT
    on the CONTENT of the horizon. So a retry after a new payable invoice has
    landed must return the ORIGINAL run untouched — never a second run, and
    never a silently-widened one — because the user approved the first plan,
    not the ledger's current state."""
    from app.models.payment import Payment, PaymentRun

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent, number="IDEM-1", amount="200.00")
        await sa.commit()

    plan = await _propose_plan(realdb)
    body = _replay_body(plan)
    async with realdb.client(key="a", role="admin") as c:
        first = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=body)
        assert first.status_code == 201, first.text
        assert first.json()["created"] is True

        async with mk_a() as sa:
            ent = await _default_entity_id(sa, a.org_id)
            await _seed_invoice(sa, a.org_id, ent, number="IDEM-LATER", amount="4444.00")
            await sa.commit()

        again = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=body)

    assert again.status_code == 200, again.text
    data = again.json()
    assert data["created"] is False
    assert data["run_id"] == first.json()["run_id"]
    assert data["payment_count"] == first.json()["payment_count"] == 1
    assert Decimal(data["total_amount"]) == Decimal("200.00")

    async with mk_a() as sa:
        runs = (await sa.execute(select(PaymentRun))).scalars().all()
        payments = (await sa.execute(select(Payment))).scalars().all()
    assert len(runs) == 1
    assert len(payments) == 1


async def test_a_foreign_currency_commitment_is_left_behind_and_counted(realdb):
    """A payment run is single-currency (`PaymentRun.total_amount` is a bare
    Numeric the CFO threshold is compared against), so the route stages the
    org's REPORTING-currency slice — the currency the plan's own curve, budget
    and threshold are already in — and reports what it left rather than
    dropping it silently."""
    from app.models.payment import Payment

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        usd = await _seed_invoice(sa, a.org_id, ent, number="FX-USD", amount="300.00")
        await _seed_invoice(sa, a.org_id, ent, number="FX-EUR", amount="900.00", currency="EUR")
        await sa.commit()
        usd_id = usd.id

    plan = await _propose_plan(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["run_currency"] == "USD"
    assert data["payment_count"] == 1
    assert data["excluded_currency_count"] == 1
    assert Decimal(data["total_amount"]) == Decimal("300.00")

    async with mk_a() as sa:
        payments = (await sa.execute(select(Payment))).scalars().all()
    assert [p.invoice_id for p in payments] == [usd_id]


async def test_a_plan_with_nothing_in_the_reporting_currency_says_what_it_found(realdb):
    """The actionable version of the old 422: name the currencies that ARE
    there, from a plan the user cannot edit."""
    from app.models.payment import PaymentRun

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa, a.org_id, ent, number="FX-ONLY-EUR", amount="900.00", currency="EUR"
        )
        await sa.commit()

    plan = await _propose_plan(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=_replay_body(plan)
        )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "USD" in detail and "EUR" in detail

    async with mk_a() as sa:
        assert (await sa.execute(select(PaymentRun))).scalars().all() == []


# ===========================================================================
# 7. `/capture-discounts` — status only, never money
# ===========================================================================


async def test_only_still_offered_offers_are_accepted(realdb):
    """An offer already handled — declined here, but equally an accept from the
    `/discounts` dashboard or a previous call — must be left exactly as it is.

    The candidate query is `status == offered`, so a decided offer is not even
    a candidate: the guarantee is that a plan enact can never revive it."""
    from app.models.discount import DiscountOffer

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        live = await _seed_invoice(
            sa, a.org_id, ent, number="CAP-LIVE", amount="1000.00", due_in_days=30
        )
        decided = await _seed_invoice(
            sa, a.org_id, ent, number="CAP-DECIDED", amount="2000.00", due_in_days=30
        )
        await sa.commit()
        live_id, decided_id = live.id, decided.id

    live_offer = await _create_offer(realdb, live_id)
    decided_offer = await _create_offer(realdb, decided_id)
    async with mk_a() as sa:
        await sa.execute(
            update(DiscountOffer)
            .where(DiscountOffer.id == uuid.UUID(decided_offer))
            .values(status="declined")
        )
        await sa.commit()

    plan = await _propose_plan(realdb, opening_balance=Decimal("50000.00"))
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/capture-discounts", json=_replay_body(plan)
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["accepted_offer_ids"] == [live_offer]
    assert data["accepted_count"] == 1
    assert data["skipped_count"] == 0
    _assert_money_str(data["total_savings_selected"], "total_savings_selected")

    async with mk_a() as sa:
        rows = {str(o.id): o for o in (await sa.execute(select(DiscountOffer))).scalars().all()}
    assert rows[live_offer].status == "accepted"
    assert rows[decided_offer].status == "declined", "a decided offer must never be revived"
    assert rows[decided_offer].accepted_at is None
    assert rows[decided_offer].accepted_tier is None


async def test_an_offer_the_optimizer_did_not_select_is_left_offered(realdb):
    """This route accepts the SAME selection the plan proposed — never every
    open offer.

    A zero cash budget is the cleanest way to say "select nothing": the offer
    is still worthwhile on APR, but no outlay fits, so the plan did not
    recommend capturing it and neither may the enact call."""
    from app.models.discount import DiscountOffer

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        inv = await _seed_invoice(
            sa, a.org_id, ent, number="CAP-BUDGET", amount="1000.00", due_in_days=30
        )
        await sa.commit()
        inv_id = inv.id
    offer_id = await _create_offer(realdb, inv_id)

    plan = await _propose_plan(realdb, cash_budget=Decimal("0"))
    assert not any(r.selected for r in plan.discount_recommendations), (
        "fixture assumption: a zero cash budget selects nothing"
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/cash-flow/plans/{plan.plan_id}/capture-discounts", json=_replay_body(plan)
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted_count"] == 0
    assert resp.json()["accepted_offer_ids"] == []

    async with mk_a() as sa:
        offer = (
            await sa.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(offer_id)))
        ).scalar_one()
    assert offer.status == "offered"


async def test_capturing_a_discount_moves_no_money_and_audits_once(realdb):
    """The whole safety model of this route in one test: the offer's STATUS
    flips (with the actor and the channel recorded), and nothing else in the
    money path moves — no `Payment`, no `PaymentRun`, no invoice transition.
    A repeat call adds no second accept and no second audit row."""
    from app.models.discount import DiscountOffer
    from app.models.invoice import Invoice
    from app.models.payment import Payment, PaymentRun
    from app.models.workflow import AuditLog

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        inv = await _seed_invoice(
            sa, a.org_id, ent, number="CAP-AUDIT", amount="1000.00", due_in_days=30
        )
        await sa.commit()
        inv_id = inv.id
    offer_id = await _create_offer(realdb, inv_id)

    plan = await _propose_plan(realdb, opening_balance=Decimal("50000.00"))
    body = _replay_body(plan)
    async with realdb.client(key="a", role="admin") as c:
        first = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/capture-discounts", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["accepted_count"] == 1
        again = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/capture-discounts", json=body)
        assert again.status_code == 200, again.text
        assert again.json()["accepted_count"] == 0

    async with mk_a() as sa:
        offer = (
            await sa.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(offer_id)))
        ).scalar_one()
        assert offer.status == "accepted"
        assert offer.accepted_tier is not None
        assert offer.accepted_by == a.users["admin"], "the acting user is recorded on the offer"
        assert offer.captured_at is None, "capture is a later, money-moving step"

        invoice = (await sa.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert invoice.status == "approved", "accepting an offer never advances the invoice"
        assert (await sa.execute(select(Payment))).scalars().all() == []
        assert (await sa.execute(select(PaymentRun))).scalars().all() == []

        audit = (
            (await sa.execute(select(AuditLog).where(AuditLog.action == "discount_offer.accepted")))
            .scalars()
            .all()
        )
    assert len(audit) == 1, "one accept, one audit row — a no-op repeat adds nothing"
    assert audit[0].entity_id == uuid.UUID(offer_id)
    assert (audit[0].details or {}).get("via") == "cashflow_copilot"


# ===========================================================================
# 8. RBAC + the kill switch, across the whole saved-plan surface
# ===========================================================================

#: Every route in `app/api/cash_flow.py`'s plan surface, as (method, path,
#: body). One list so a new route joins the RBAC and kill-switch matrices at
#: once rather than shipping ungated.
_PLAN_ROUTES = [
    ("post", "/api/cash-flow/plans/whatever/save", _BOGUS_REPLAY),
    ("post", "/api/cash-flow/plans/whatever/draft-run", _BOGUS_REPLAY),
    ("post", "/api/cash-flow/plans/whatever/capture-discounts", _BOGUS_REPLAY),
    ("get", "/api/cash-flow/plans", None),
    ("get", "/api/cash-flow/plans/whatever", None),
    ("get", "/api/cash-flow/plans/whatever/variance", None),
    ("delete", "/api/cash-flow/plans/whatever", None),
]
_PLAN_ROUTE_IDS = [f"{m}-{p.rsplit('/', 1)[-1] or 'plans'}" for m, p, _ in _PLAN_ROUTES]


@pytest.mark.parametrize(("method", "path", "body"), _PLAN_ROUTES, ids=_PLAN_ROUTE_IDS)
async def test_every_plan_route_refuses_an_ap_clerk(realdb, method, path, body):
    """A clerk may not see the org's cash position, let alone stage a run off
    it — `COPILOT_ROLES` is admin/ap_manager/cfo.

    Asserted against a BOGUS plan id on purpose: the role gate must run before
    anything looks the plan up, so a refused clerk learns nothing about which
    ids exist and never trips a 500 on an unparseable one."""
    kwargs = {"json": body} if body is not None else {}
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await getattr(c, method)(path, **kwargs)
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize(("method", "path", "body"), _PLAN_ROUTES, ids=_PLAN_ROUTE_IDS)
async def test_every_plan_route_requires_authentication(realdb, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    async with realdb.client(key="a", role=None) as c:
        resp = await getattr(c, method)(path, **kwargs)
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("role", ["admin", "ap_manager", "cfo"])
async def test_every_finance_leader_role_can_save_enact_and_score_a_plan(realdb, role):
    """The three roles that own the cash position all reach the full lifecycle
    — a CFO who may commit cash must not be locked out of the baseline, and an
    ap_manager who runs the payment cycle must be able to stage the draft."""
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent, number=f"ROLE-{role}", amount="450.00")
        await sa.commit()

    plan = await _propose_plan(realdb)
    body = _replay_body(plan)
    async with realdb.client(key="a", role=role) as c:
        assert (
            await c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=body)
        ).status_code == 201
        assert (await c.get(f"/api/cash-flow/plans/{plan.plan_id}/variance")).status_code == 200
        assert (
            await c.post(f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=body)
        ).status_code == 201
        assert (
            await c.post(f"/api/cash-flow/plans/{plan.plan_id}/capture-discounts", json=body)
        ).status_code == 200


async def test_the_kill_switch_closes_the_write_half_of_the_surface_too(realdb, monkeypatch):
    """`FEOH_CASHFLOW_COPILOT_ENABLED=false` must make the whole surface 404 —
    a disabled copilot is indistinguishable from an unmounted route.

    The existing coverage pins the read routes; the write ones matter more
    (one of them stages a payment run), so each is asserted to 404 AND to have
    changed nothing."""
    from app.config import settings
    from app.models.cash_plan import CashPlan
    from app.models.payment import PaymentRun

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent, number="KILL-1", amount="500.00")
        await sa.commit()

    plan = await _propose_plan(realdb)
    body = _replay_body(plan)
    monkeypatch.setattr(settings, "cashflow_copilot_enabled", False)

    async with realdb.client(key="a", role="admin") as c:
        for route in _PLAN_BODY_ROUTES:
            resp = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/{route}", json=body)
            assert resp.status_code == 404, f"{route}: {resp.status_code} {resp.text}"
        assert (await c.delete(f"/api/cash-flow/plans/{plan.plan_id}")).status_code == 404

    async with mk_a() as sa:
        assert (await sa.execute(select(CashPlan))).scalars().all() == []
        assert (await sa.execute(select(PaymentRun))).scalars().all() == []


async def test_the_snapshot_reports_its_draft_run_and_outlives_nothing_when_deleted(realdb):
    """`plan_id` is the one key shared by `cash_plans` and `payment_runs`, so a
    saved snapshot can tell a reader whether it was ever enacted.

    And deleting the baseline must remove ONLY the baseline: the draft run
    staged from the same id is real money-path state that a discarded
    projection has no business taking with it."""
    from app.models.cash_plan import CashPlan
    from app.models.payment import PaymentRun

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent, number="LINK-1", amount="700.00")
        await sa.commit()

    plan = await _propose_plan(realdb)
    body = _replay_body(plan)
    async with realdb.client(key="a", role="admin") as c:
        saved = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=body)
        assert saved.status_code == 201, saved.text
        assert saved.json()["plan"]["has_draft_run"] is False

        staged = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/draft-run", json=body)
        assert staged.status_code == 201, staged.text
        run_id = staged.json()["run_id"]

        detail = await c.get(f"/api/cash-flow/plans/{plan.plan_id}")
        assert detail.json()["has_draft_run"] is True
        listed = await c.get("/api/cash-flow/plans?consolidated=true")
        assert [r["plan_id"] for r in listed.json()] == [plan.plan_id]
        _assert_no_float(listed.json(), path="list")

        # A repeat save still returns the frozen snapshot — now correctly
        # reporting the run that appeared after it was taken, because
        # `has_draft_run` is derived on read rather than frozen into the row.
        again = await c.post(f"/api/cash-flow/plans/{plan.plan_id}/save", json=body)
        assert again.status_code == 200
        assert again.json()["plan"]["has_draft_run"] is True

        assert (await c.delete(f"/api/cash-flow/plans/{plan.plan_id}")).status_code == 204

    async with mk_a() as sa:
        assert (await sa.execute(select(CashPlan))).scalars().all() == []
        run = (
            await sa.execute(select(PaymentRun).where(PaymentRun.plan_id == plan.plan_id))
        ).scalar_one()
    assert str(run.id) == run_id, "deleting a baseline must not delete the draft run"
    assert run.status == "draft"
