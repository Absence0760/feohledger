"""Real-DB coverage for the org-wide budget-vs-actual rollup.

Covers ``GET /api/budgets/rollup`` (``api/budgets.py::budget_rollup``) and the
``services/budget_service.compute_budget_rollup`` fold it is built on, plus the
currency-mismatch disclosure (``excluded_row_count``) that both the rollup and
the per-budget ``/spend`` response now carry.

The rollup is the CFO's consolidated view: only the standalone ``/budgets`` page
and the per-budget ``/{id}/spend`` existed, so an org-wide allocated vs
committed vs actual meant opening budgets one at a time.

DO NOT run this file standalone in a concurrent build — the ``realdb`` fixture
truncates all tables sequentially. The orchestrator runs the suite at the end.
"""

import uuid
from datetime import date
from decimal import Decimal

from app.models.entity import Entity
from app.models.invoice import Invoice
from app.models.procurement import (
    Budget,
    PurchaseOrder,
    PurchaseRequisition,
    RequisitionStatus,
)


def _u() -> str:
    return uuid.uuid4().hex[:8]


async def _mk_budget(
    realdb,
    key="a",
    *,
    dimension="cost_center",
    dimension_value=None,
    amount="1000.00",
    period="2026",
    period_start=None,
    period_end=None,
    currency="USD",
    entity_id=None,
) -> tuple[uuid.UUID, str]:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    bid = uuid.uuid4()
    value = dimension_value or f"CC-{_u()}"
    async with mk() as s:
        s.add(
            Budget(
                id=bid,
                name=f"Budget {_u()}",
                dimension=dimension,
                dimension_value=value,
                period=period,
                period_start=period_start,
                period_end=period_end,
                amount=Decimal(amount),
                currency=currency,
                entity_id=entity_id,
                organization_id=org_id,
            )
        )
        await s.commit()
    return bid, value


async def _mk_invoice(
    realdb,
    key="a",
    *,
    amount,
    cost_center=None,
    currency="USD",
    entity_id=None,
    dimension="cost_center",
    dimension_value=None,
    invoice_date=None,
    status="paid",
):
    """One realised invoice, attributed on whichever dimension column is asked
    for — the rollup batches by dimension, so all four need exercising."""
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    value = dimension_value if dimension_value is not None else cost_center
    async with mk() as s:
        s.add(
            Invoice(
                id=uuid.uuid4(),
                invoice_number=f"INV-{_u()}",
                vendor_name="Acme Supply",
                amount=Decimal(amount),
                status=status,
                currency=currency,
                entity_id=entity_id,
                invoice_date=invoice_date,
                organization_id=org_id,
                **{dimension: value},
            )
        )
        await s.commit()


async def _mk_converted_requisition(realdb, key="a", *, budget_id, po_total, currency="USD"):
    """A `converted` requisition plus the PO it became — leg 2 of `committed`."""
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    po_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PurchaseOrder(
                id=po_id,
                po_number=f"PO-{_u()}",
                total=Decimal(po_total),
                # Conversion stamps the requisition's code onto the PO.
                currency=currency,
                status="open",
                organization_id=org_id,
            )
        )
        s.add(
            PurchaseRequisition(
                id=uuid.uuid4(),
                requisition_number=f"REQ-{_u()}",
                requester_user_id=uuid.uuid4(),
                budget_id=budget_id,
                total=Decimal(po_total),
                status=RequisitionStatus.converted,
                converted_po_id=po_id,
                currency=currency,
                organization_id=org_id,
            )
        )
        await s.commit()


async def _mk_requisition(realdb, key="a", *, budget_id, total, currency="USD"):
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    async with mk() as s:
        s.add(
            PurchaseRequisition(
                id=uuid.uuid4(),
                requisition_number=f"REQ-{_u()}",
                requester_user_id=uuid.uuid4(),
                budget_id=budget_id,
                total=Decimal(total),
                status=RequisitionStatus.approved,
                currency=currency,
                organization_id=org_id,
            )
        )
        await s.commit()


async def _mk_entity(realdb, key="a") -> uuid.UUID:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    eid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Entity(
                id=eid,
                name=f"Sub {_u()}",
                slug=f"sub-{_u()}",
                is_default=False,
                is_active=True,
                organization_id=org_id,
            )
        )
        await s.commit()
    return eid


def _by_ccy(body) -> dict:
    return {row["currency"]: row for row in body["by_currency"]}


# ---------------------------------------------------------------------------
# The rollup itself
# ---------------------------------------------------------------------------


async def test_rollup_sums_exactly_within_each_currency(realdb):
    """Allocated / committed / actual are exact decimal STRINGS, summed within a
    currency and never across one. Two USD budgets + one EUR budget must produce
    two independent rows, never one blended figure."""
    bid1, cc1 = await _mk_budget(realdb, amount="1000.00")
    bid2, cc2 = await _mk_budget(realdb, amount="2000.10")
    bid3, cc3 = await _mk_budget(realdb, amount="500.00", currency="EUR")

    await _mk_requisition(realdb, budget_id=bid1, total="100.00")
    await _mk_invoice(realdb, amount="250.05", cost_center=cc1)
    await _mk_invoice(realdb, amount="10.00", cost_center=cc2)
    await _mk_requisition(realdb, budget_id=bid3, total="50.00", currency="EUR")
    await _mk_invoice(realdb, amount="25.00", cost_center=cc3, currency="EUR")

    async with realdb.client(key="a", role="cfo") as c:
        res = await c.get("/api/budgets/rollup")
    assert res.status_code == 200
    body = res.json()
    assert body["budget_count"] == 3
    assert body["insufficient_data"] is False

    rows = _by_ccy(body)
    assert set(rows) == {"USD", "EUR"}

    usd = rows["USD"]
    assert usd["budget_count"] == 2
    assert usd["allocated"] == "3000.10"
    assert usd["committed"] == "100.00"
    assert usd["actual"] == "260.05"
    assert usd["remaining"] == "2640.05"
    # (100.00 + 260.05) / 3000.10 * 100
    assert usd["utilization_pct"] == "12.00"
    assert usd["over_budget_count"] == 0

    eur = rows["EUR"]
    assert eur["budget_count"] == 1
    assert eur["allocated"] == "500.00"
    assert eur["committed"] == "50.00"
    assert eur["actual"] == "25.00"
    assert eur["remaining"] == "425.00"

    # `bid2`'s value is only present via the USD row — the two currencies are
    # never added into one number anywhere in the payload.
    assert "3500.10" not in res.text


async def test_rollup_counts_over_budget_currencies(realdb):
    """A budget whose remaining went negative is counted per currency."""
    _bid, cc = await _mk_budget(realdb, amount="100.00")
    await _mk_invoice(realdb, amount="250.00", cost_center=cc)

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/budgets/rollup")).json()
    usd = _by_ccy(body)["USD"]
    assert usd["remaining"] == "-150.00"
    assert usd["over_budget_count"] == 1


async def test_rollup_utilization_is_null_not_zero_when_nothing_allocated(realdb):
    """A currency allocating nothing has NO utilization — `null`, never
    `"0.00"`. "0% used" and "there is nothing to use" are opposite facts and 0%
    reads as the reassuring one (decisions §34)."""
    await _mk_budget(realdb, amount="0.00")

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/budgets/rollup")).json()
    assert _by_ccy(body)["USD"]["utilization_pct"] is None


async def test_rollup_empty_set_reports_insufficient_data(realdb):
    """No budgets at all is a distinct state from a row of confident zeros."""
    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/budgets/rollup")).json()
    assert body["budget_count"] == 0
    assert body["by_currency"] == []
    assert body["insufficient_data"] is True
    assert body["excluded_row_count"] == 0


# ---------------------------------------------------------------------------
# The disclosure
# ---------------------------------------------------------------------------


async def test_rollup_discloses_currency_excluded_rows(realdb):
    """A foreign-currency invoice / requisition matching a budget is REFUSED by
    the spend legs (they never convert) — and counted, so the surface rendering
    the figure can say it is a floor rather than a complete picture."""
    bid, cc = await _mk_budget(realdb, amount="10000.00", currency="USD")
    await _mk_invoice(realdb, amount="300.00", cost_center=cc, currency="USD")
    await _mk_invoice(realdb, amount="999.00", cost_center=cc, currency="EUR")
    await _mk_requisition(realdb, budget_id=bid, total="700.00", currency="EUR")

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/budgets/rollup")).json()
        spend = (await c.get(f"/api/budgets/{bid}/spend")).json()

    usd = _by_ccy(body)["USD"]
    assert usd["actual"] == "300.00"  # the EUR invoice is excluded, not added
    assert usd["committed"] == "0.00"  # the EUR requisition likewise
    assert usd["excluded_row_count"] == 2
    assert body["excluded_row_count"] == 2

    # The per-budget rollup carries the same disclosure.
    assert spend["actual"] == 300.0
    assert spend["excluded_row_count"] == 2


async def test_rollup_excluded_count_is_zero_on_a_clean_set(realdb):
    _bid, cc = await _mk_budget(realdb, amount="1000.00")
    await _mk_invoice(realdb, amount="10.00", cost_center=cc)
    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/budgets/rollup")).json()
    assert body["excluded_row_count"] == 0
    assert _by_ccy(body)["USD"]["excluded_row_count"] == 0


# ---------------------------------------------------------------------------
# Shares the list's filter builder + entity scope
# ---------------------------------------------------------------------------


async def test_rollup_honours_the_list_filters(realdb):
    """The rollup runs the SAME `_budget_list_filters` as `GET /api/budgets`, so
    it can never describe a different set than the table it sits above."""
    await _mk_budget(realdb, dimension="cost_center", amount="1000.00")
    await _mk_budget(realdb, dimension="project", amount="400.00", currency="EUR")

    async with realdb.client(key="a", role="cfo") as c:
        listed = (await c.get("/api/budgets", params={"dimension": "project"})).json()
        body = (await c.get("/api/budgets/rollup", params={"dimension": "project"})).json()

    assert listed["total"] == 1
    assert body["budget_count"] == 1
    assert [r["currency"] for r in body["by_currency"]] == ["EUR"]
    assert _by_ccy(body)["EUR"]["allocated"] == "400.00"


async def test_rollup_honours_the_search_filter(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    marker = f"Zeta{_u()}"
    async with mk() as s:
        s.add(
            Budget(
                id=uuid.uuid4(),
                name=marker,
                dimension="department",
                dimension_value="Engineering",
                period="2026",
                amount=Decimal("777.00"),
                currency="USD",
                organization_id=org_id,
            )
        )
        await s.commit()
    await _mk_budget(realdb, amount="1000.00")

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/budgets/rollup", params={"search": marker})).json()
    assert body["budget_count"] == 1
    assert _by_ccy(body)["USD"]["allocated"] == "777.00"


async def test_rollup_is_entity_scoped(realdb):
    """`X-Entity-ID` narrows the rollup exactly like the list — a subsidiary's
    CFO never sees a sibling entity's allocation folded in."""
    other = await _mk_entity(realdb)
    await _mk_budget(realdb, amount="1000.00", entity_id=other)
    await _mk_budget(realdb, amount="55.00", entity_id=None)

    async with realdb.client(key="a", role="cfo") as c:
        scoped = (await c.get("/api/budgets/rollup", headers={"X-Entity-ID": str(other)})).json()
        unscoped = (await c.get("/api/budgets/rollup")).json()

    assert scoped["budget_count"] == 1
    assert _by_ccy(scoped)["USD"]["allocated"] == "1000.00"
    assert unscoped["budget_count"] == 2


# ---------------------------------------------------------------------------
# RBAC + tenant isolation
# ---------------------------------------------------------------------------


async def test_rollup_read_gate_matches_the_list(realdb):
    """Read is admin / ap_manager / cfo — the existing budgets read gate."""
    for role in ("admin", "ap_manager", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            assert (await c.get("/api/budgets/rollup")).status_code == 200
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.get("/api/budgets/rollup")).status_code == 403


async def test_rollup_is_tenant_isolated(realdb):
    await _mk_budget(realdb, "a", amount="1000.00")
    async with realdb.client(key="b", role="cfo") as c:
        body = (await c.get("/api/budgets/rollup")).json()
    assert body["budget_count"] == 0
    assert body["insufficient_data"] is True


# ---------------------------------------------------------------------------
# Anti-drift: the rollup and the per-budget endpoint are ONE implementation
# ---------------------------------------------------------------------------
#
# `compute_budget_spend` is `compute_budget_spends` narrowed to a single budget,
# so `GET /budgets/rollup` and `GET /budgets/{id}/spend` read the same SQL. The
# tests below are what keeps that true: a second query shape introduced for
# speed would show up here as a figure — or, worse, an `excluded_row_count` —
# that only one of the two endpoints reports. A disclosure the org-wide view and
# the per-budget view can disagree about is worse than no disclosure at all.


async def test_rollup_agrees_exactly_with_every_per_budget_spend(realdb):
    """THE anti-drift guard. Over a tenant with several budgets in several
    currencies AND several dimensions, every per-currency rollup figure must be
    the exact sum of the `GET /{id}/spend` figures for that currency's budgets —
    including `excluded_row_count`, which the whole disclosure rests on."""
    specs = [
        # (dimension, currency, allocation)
        ("cost_center", "USD", "5000.00"),
        ("cost_center", "USD", "1500.50"),
        ("gl_account", "USD", "2000.00"),
        ("department", "EUR", "900.00"),
        ("project", "EUR", "100.25"),
        ("project", "GBP", "750.00"),
    ]
    made: list[tuple[uuid.UUID, str, str]] = []  # (id, currency, dimension_value)
    for i, (dimension, currency, amount) in enumerate(specs):
        bid, value = await _mk_budget(
            realdb,
            dimension=dimension,
            dimension_value=f"{dimension.upper()}-{i}-{_u()}",
            amount=amount,
            currency=currency,
        )
        made.append((bid, currency, value))

        # actual: one matching invoice, plus one in a foreign currency that the
        # legs must REFUSE and COUNT.
        await _mk_invoice(
            realdb, amount="120.00", dimension=dimension, dimension_value=value, currency=currency
        )
        await _mk_invoice(
            realdb, amount="999.00", dimension=dimension, dimension_value=value, currency="JPY"
        )
        # committed leg 1 (open requisition) + leg 2 (converted req -> PO).
        await _mk_requisition(realdb, budget_id=bid, total="30.00", currency=currency)
        await _mk_requisition(realdb, budget_id=bid, total="77.00", currency="JPY")
        await _mk_converted_requisition(realdb, budget_id=bid, po_total="45.00", currency=currency)

    async with realdb.client(key="a", role="cfo") as c:
        rollup = (await c.get("/api/budgets/rollup")).json()
        spends = {bid: (await c.get(f"/api/budgets/{bid}/spend")).json() for bid, _, _ in made}

    # Fold the per-budget responses the way the rollup claims to.
    expected: dict[str, dict] = {}
    for bid, currency, _value in made:
        s = spends[bid]
        assert s["currency"] == currency
        bucket = expected.setdefault(
            currency,
            {
                "budget_count": 0,
                "allocated": Decimal(0),
                "committed": Decimal(0),
                "actual": Decimal(0),
                "excluded_row_count": 0,
                "over_budget_count": 0,
            },
        )
        bucket["budget_count"] += 1
        bucket["allocated"] += Decimal(str(s["allocated"]))
        bucket["committed"] += Decimal(str(s["committed"]))
        bucket["actual"] += Decimal(str(s["actual"]))
        bucket["excluded_row_count"] += s["excluded_row_count"]
        if s["remaining"] < 0:
            bucket["over_budget_count"] += 1

    rows = _by_ccy(rollup)
    assert set(rows) == set(expected)
    for currency, want in expected.items():
        got = rows[currency]
        assert got["budget_count"] == want["budget_count"], currency
        assert Decimal(got["allocated"]) == want["allocated"], currency
        assert Decimal(got["committed"]) == want["committed"], currency
        assert Decimal(got["actual"]) == want["actual"], currency
        assert Decimal(got["remaining"]) == (
            want["allocated"] - want["committed"] - want["actual"]
        ), currency
        # The disclosure is the figure most at risk from a forked query shape.
        assert got["excluded_row_count"] == want["excluded_row_count"], currency
        assert got["over_budget_count"] == want["over_budget_count"], currency

    # Each budget saw one foreign invoice + one foreign requisition.
    assert rollup["excluded_row_count"] == 2 * len(specs)
    assert rollup["budget_count"] == len(specs)


async def test_rollup_agrees_with_spend_on_the_entity_scoped_invoice_leg(realdb):
    """The invoice leg's own entity scope survives batching, and both endpoints
    apply it identically. Batching budgets into one grouped query is exactly
    where a sibling entity's spend could leak in."""
    other = await _mk_entity(realdb)
    shared_value = f"CC-{_u()}"
    mine, _ = await _mk_budget(
        realdb, dimension_value=shared_value, amount="1000.00", entity_id=other
    )
    # Same dimension VALUE, no entity — deliberately overlapping attribution.
    unscoped, _ = await _mk_budget(realdb, dimension_value=shared_value, amount="1000.00")

    await _mk_invoice(realdb, amount="200.00", cost_center=shared_value, entity_id=other)
    await _mk_invoice(realdb, amount="35.00", cost_center=shared_value, entity_id=None)

    async with realdb.client(key="a", role="cfo") as c:
        rollup = (await c.get("/api/budgets/rollup")).json()
        mine_spend = (await c.get(f"/api/budgets/{mine}/spend")).json()
        unscoped_spend = (await c.get(f"/api/budgets/{unscoped}/spend")).json()

    # The entity-bound budget sees only its own entity's invoice...
    assert mine_spend["actual"] == 200.0
    # ...and the entity-less one sees both (unscoped is unscoped).
    assert unscoped_spend["actual"] == 235.0
    # The rollup folds precisely those two, batched into one grouped query.
    assert Decimal(_by_ccy(rollup)["USD"]["actual"]) == Decimal("435.00")


async def test_rollup_agrees_with_spend_across_all_four_dimensions(realdb):
    """`actual` is attributed on a different `Invoice` column per dimension, and
    the rollup issues one query per dimension. All four must land on their own
    budget and nobody else's."""
    made = []
    for dimension, amount in (
        ("cost_center", "11.00"),
        ("gl_account", "22.00"),
        ("department", "33.00"),
        ("project", "44.00"),
    ):
        bid, value = await _mk_budget(realdb, dimension=dimension, amount="1000.00")
        await _mk_invoice(realdb, amount=amount, dimension=dimension, dimension_value=value)
        made.append((bid, Decimal(amount)))

    async with realdb.client(key="a", role="cfo") as c:
        rollup = (await c.get("/api/budgets/rollup")).json()
        spends = {bid: (await c.get(f"/api/budgets/{bid}/spend")).json() for bid, _ in made}

    for bid, amount in made:
        assert Decimal(str(spends[bid]["actual"])) == amount
    assert Decimal(_by_ccy(rollup)["USD"]["actual"]) == Decimal("110.00")


async def test_rollup_agrees_with_spend_on_period_bounded_budgets(realdb):
    """Two budgets on the SAME dimension value in different periods are batched
    into one grouped query — each must still see only its own window's spend."""
    value = f"CC-{_u()}"
    q1, _ = await _mk_budget(
        realdb,
        dimension_value=value,
        amount="1000.00",
        period="2026-Q1",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
    )
    q2, _ = await _mk_budget(
        realdb,
        dimension_value=value,
        amount="1000.00",
        period="2026-Q2",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
    )
    await _mk_invoice(realdb, amount="10.00", cost_center=value, invoice_date=date(2026, 2, 14))
    await _mk_invoice(realdb, amount="20.00", cost_center=value, invoice_date=date(2026, 5, 14))

    async with realdb.client(key="a", role="cfo") as c:
        rollup = (await c.get("/api/budgets/rollup")).json()
        s1 = (await c.get(f"/api/budgets/{q1}/spend")).json()
        s2 = (await c.get(f"/api/budgets/{q2}/spend")).json()

    assert s1["actual"] == 10.0
    assert s2["actual"] == 20.0
    assert Decimal(_by_ccy(rollup)["USD"]["actual"]) == Decimal("30.00")


async def test_rollup_includes_a_budget_with_no_spend_at_all(realdb):
    """A budget nothing has been spent against still appears — as an allocation
    with zero committed / zero actual, not as an absent row. The grouped legs
    are inner joins, so this is exactly the case that would silently vanish."""
    quiet, _ = await _mk_budget(realdb, amount="4000.00")
    busy, cc = await _mk_budget(realdb, amount="1000.00")
    await _mk_invoice(realdb, amount="60.00", cost_center=cc)

    async with realdb.client(key="a", role="cfo") as c:
        rollup = (await c.get("/api/budgets/rollup")).json()
        spend = (await c.get(f"/api/budgets/{quiet}/spend")).json()

    assert spend["allocated"] == 4000.0
    assert spend["committed"] == 0.0
    assert spend["actual"] == 0.0
    assert spend["remaining"] == 4000.0
    assert spend["utilization_pct"] == 0.0
    assert spend["excluded_row_count"] == 0

    usd = _by_ccy(rollup)["USD"]
    assert usd["budget_count"] == 2
    assert usd["allocated"] == "5000.00"
    assert usd["committed"] == "0.00"
    assert usd["actual"] == "60.00"
    assert rollup["budget_count"] == 2
    assert busy is not None


async def test_rollup_utilization_null_only_for_the_currency_that_allocates_nothing(realdb):
    """`utilization_pct` is `null` for a currency allocating nothing — and a
    real figure for a sibling currency in the SAME response. Batching must not
    let one currency's zero-allocation state bleed into another's."""
    _zero, _ = await _mk_budget(realdb, amount="0.00", currency="EUR")
    _live, cc = await _mk_budget(realdb, amount="400.00", currency="USD")
    await _mk_invoice(realdb, amount="100.00", cost_center=cc)

    async with realdb.client(key="a", role="cfo") as c:
        rollup = (await c.get("/api/budgets/rollup")).json()

    rows = _by_ccy(rollup)
    assert rows["EUR"]["utilization_pct"] is None
    assert rows["EUR"]["allocated"] == "0.00"
    assert rows["USD"]["utilization_pct"] == "25.00"


async def test_rollup_does_not_cross_contaminate_budgets_sharing_a_dimension_value(realdb):
    """Two budgets on the same dimension value are grouped in ONE query. Each
    must report the full matching spend for itself — never the other's row, and
    never a share of it."""
    value = f"CC-{_u()}"
    a, _ = await _mk_budget(realdb, dimension_value=value, amount="1000.00")
    b, _ = await _mk_budget(realdb, dimension_value=value, amount="2000.00")
    await _mk_invoice(realdb, amount="90.00", cost_center=value)

    async with realdb.client(key="a", role="cfo") as c:
        sa_ = (await c.get(f"/api/budgets/{a}/spend")).json()
        sb_ = (await c.get(f"/api/budgets/{b}/spend")).json()
        rollup = (await c.get("/api/budgets/rollup")).json()

    assert sa_["actual"] == 90.0
    assert sb_["actual"] == 90.0
    # The rollup is the fold of the two, not a de-duplicated single count.
    assert Decimal(_by_ccy(rollup)["USD"]["actual"]) == Decimal("180.00")


async def test_check_endpoint_reads_the_same_spend_as_the_rollup(realdb):
    """`GET /budgets/check` shares `compute_budget_spend`, so its `remaining` is
    the same number the rollup folded — the requisition flow and the CFO
    dashboard cannot be looking at different headroom."""
    bid, cc = await _mk_budget(realdb, amount="1000.00")
    await _mk_invoice(realdb, amount="250.00", cost_center=cc)
    await _mk_requisition(realdb, budget_id=bid, total="100.00")

    async with realdb.client(key="a", role="cfo") as c:
        check = (
            await c.get("/api/budgets/check", params={"budget_id": str(bid), "amount": "50.00"})
        ).json()
        rollup = (await c.get("/api/budgets/rollup")).json()

    usd = _by_ccy(rollup)["USD"]
    assert Decimal(str(check["remaining"])) == Decimal(usd["remaining"]) == Decimal("650.00")
    assert Decimal(str(check["committed"])) == Decimal(usd["committed"])
    assert Decimal(str(check["actual"])) == Decimal(usd["actual"])
    assert check["remaining_after"] == 600.0
    assert check["would_overspend"] is False
