"""Per-vendor spend is grouped in SQL, and says what it could not convert.

Two related guarantees about the four surfaces that answer "what did we spend,
by vendor" — the CFO supplier-concentration tile (`GET /api/analytics/cfo`),
its drill-through (`/api/analytics/drill/spend_concentration`), the
`vendor_spend` CSV export (`/api/analytics/export/vendor_spend`) and the
emailed scheduled report (`services/scheduled_reports`):

1. **They group in SQL.** Each used to `SELECT` five columns of every invoice
   in the period and fold them in Python through
   `currency_conversion.vendor_rollup_to_reporting_currency` — a synchronous
   per-row loop inside an `async def`, the shape `docs/decisions.md` §103
   removed from the dashboard's own top-vendor tile. They now share ONE query
   builder, `vendor_spend_grouped_select`, whose `GROUP BY (vendor, currency)`
   applies the conversion rule inside Postgres.

   They cannot simply take a `LIMIT`: `compute_supplier_concentration` derives
   its denominator from the whole vendor set it is handed, so the builder
   returns every vendor and each caller slices for display afterwards.

2. **They disclose what they could not convert.** A foreign invoice with no
   usable rate lock is added at FACE value (`reporting_amount_for_row`'s
   flagged fallback). That fallback is only defensible if it is reported, so
   `VendorSpendEntry` carries an `unconverted_count`, and both JSON surfaces
   publish it — the tile as a whole-population count beside the shares it
   distorts, the drill-through both per row and for the whole period. (The
   CSV export does not yet carry a column for it; `report_export` owns that
   header.)

The equivalence tests compare the SQL against the row-at-a-time helper kept in
`currency_conversion` for exactly that purpose. Books are randomised with
INDEPENDENT dimensions (vendor, amount, status, date, currency, lock state) —
correlated generators are how an aggregation bug hides (`docs/decisions.md`
§82): if every EUR invoice also happened to be the largest, a mis-keyed
`GROUP BY` would still look right.

DO NOT run this file standalone in a concurrent build — the `realdb` fixture
truncates all tables sequentially. The orchestrator runs the suite at the end.
"""

from __future__ import annotations

import ast
import operator
import pathlib
import random
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.services.currency_conversion import (
    invoice_currency_rollup_select,
    rollup_from_grouped_rows,
    vendor_rollup_from_grouped_rows,
    vendor_rollup_to_reporting_currency,
    vendor_spend_grouped_select,
)

TENANT = "a"

_STATUSES = [
    InvoiceStatus.new,
    InvoiceStatus.pending,
    InvoiceStatus.ready_for_review,
    InvoiceStatus.approved,
    InvoiceStatus.sent_to_erp,
    InvoiceStatus.posted_in_erp,
    InvoiceStatus.payment_scheduled,
    InvoiceStatus.paid,
    InvoiceStatus.done,
    InvoiceStatus.rejected,
]


async def _default_entity_id(s):
    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


# ---------------------------------------------------------------------------
# Reference: the fold the four call sites used to run, over the rows their old
# query selected. Kept independent of the endpoints so the comparison means
# something.
# ---------------------------------------------------------------------------


async def _fold_reference(realdb, *, period_start: date, reporting_currency="USD"):
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        rows = (
            await s.execute(
                select(
                    Invoice.vendor_name,
                    Invoice.amount,
                    Invoice.currency,
                    Invoice.reporting_amount,
                    Invoice.reporting_currency,
                ).where(
                    Invoice.invoice_date >= period_start,
                    Invoice.vendor_name.isnot(None),
                    Invoice.vendor_name != "",
                    Invoice.status != "rejected",
                )
            )
        ).all()
    return vendor_rollup_to_reporting_currency(
        [
            {
                "vendor": vendor,
                "amount": amount,
                "currency": currency,
                "reporting_amount": rep_amt,
                "reporting_currency": rep_cur,
            }
            for vendor, amount, currency, rep_amt, rep_cur in rows
        ],
        reporting_currency=reporting_currency,
    )


async def _grouped(realdb, *, period_start: date, reporting_currency="USD"):
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    vendor_spend_grouped_select(
                        reporting_currency=reporting_currency, period_start=period_start
                    )
                )
            )
            .mappings()
            .all()
        )
    return vendor_rollup_from_grouped_rows(
        [dict(r) for r in rows], reporting_currency=reporting_currency
    )


async def _seed_randomised_book(realdb, *, seed: int, count: int = 140, vendors: int = 14):
    rng = random.Random(seed)
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = date.today()
    names = [f"RSQ-{i:02d}" for i in range(vendors)]

    async with mk() as s:
        ent = await _default_entity_id(s)
        for i in range(count):
            amount = Decimal(rng.randrange(1_00, 90_000_00)) / Decimal(100)
            # The lock is drawn independently of the currency, so all four
            # per-row cases the CASE must distinguish occur: locked at the
            # target, locked at a THIRD currency (must be ignored), no lock in
            # the target currency (1:1), no lock in a foreign one (face value,
            # "unconverted").
            lock = rng.choice(["target", "other", "none", "none"])
            if lock == "target":
                rep_cur = "USD"
                rep_amt = (amount * Decimal(rng.randrange(80, 130)) / Decimal(100)).quantize(
                    Decimal("0.01")
                )
            elif lock == "other":
                rep_cur, rep_amt = "CHF", amount * Decimal("2")
            else:
                rep_cur, rep_amt = None, None
            s.add(
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number=f"RSQ-{seed}-{i:04d}",
                    vendor_name=rng.choice(names),
                    amount=amount,
                    currency=rng.choice(["USD", "EUR", "GBP", "JPY"]),
                    reporting_currency=rep_cur,
                    reporting_amount=rep_amt,
                    status=rng.choice(_STATUSES),
                    invoice_date=today - timedelta(days=rng.randrange(0, 300)),
                )
            )
        # Must be excluded regardless of everything above.
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number=f"RSQ-{seed}-BLANK",
                vendor_name="",
                amount=Decimal("999999.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=today,
            )
        )
        await s.commit()


# ---------------------------------------------------------------------------
# 1. Equivalence — the GROUP BY returns what the Python fold returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [7, 1234, 65535])
async def test_grouped_sql_matches_the_python_fold(realdb, seed):
    await _seed_randomised_book(realdb, seed=seed)
    period_start = date.today() - timedelta(days=365)

    by_name = operator.attrgetter("vendor")
    expected = sorted(await _fold_reference(realdb, period_start=period_start), key=by_name)
    actual = sorted(await _grouped(realdb, period_start=period_start), key=by_name)

    assert len(actual) > 1, "fixture must produce several vendors"
    assert [e.vendor for e in actual] == [e.vendor for e in expected]
    assert [e.amount for e in actual] == [e.amount for e in expected]
    assert [e.invoice_count for e in actual] == [e.invoice_count for e in expected]
    assert [e.currencies for e in actual] == [e.currencies for e in expected]
    assert [e.unconverted_count for e in actual] == [e.unconverted_count for e in expected]
    assert all(e.vendor != "" for e in actual)


@pytest.mark.asyncio
async def test_grouped_sql_breaks_ties_by_vendor_name(realdb):
    """Equal-spend vendors come back in a reproducible order.

    Sorting on the total alone leaves ties in accumulation order, so which of
    two took the last slot of a `[:limit]` cut could differ between identical
    requests — the defect `docs/decisions.md` §103 fixed for the dashboard tile.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = date.today()
    async with mk() as s:
        ent = await _default_entity_id(s)
        # Inserted in reverse-alphabetical order so scan order contradicts the
        # required output order.
        for name in ["TIE-D", "TIE-C", "TIE-B", "TIE-A"]:
            s.add(
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number=f"TIE-{name}",
                    vendor_name=name,
                    amount=Decimal("500.00"),
                    currency="USD",
                    status=InvoiceStatus.approved,
                    invoice_date=today,
                )
            )
        await s.commit()

    entries = await _grouped(realdb, period_start=today - timedelta(days=30))
    assert [e.vendor for e in entries] == ["TIE-A", "TIE-B", "TIE-C", "TIE-D"]


# ---------------------------------------------------------------------------
# 2. Disclosure — unconvertible rows are counted, not silently folded
# ---------------------------------------------------------------------------


async def _seed_currency_cases(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = date.today()

    def inv(s, ent, num, vendor, amount, currency, rep_cur=None, rep_amt=None):
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number=num,
                vendor_name=vendor,
                amount=amount,
                currency=currency,
                reporting_currency=rep_cur,
                reporting_amount=rep_amt,
                status=InvoiceStatus.approved,
                invoice_date=today,
            )
        )

    async with mk() as s:
        ent = await _default_entity_id(s)
        # Convertible: a USD row plus a EUR row carrying a USD rate lock.
        inv(s, ent, "UCV-1", "UCV Locked Co", Decimal("1000.00"), "USD")
        inv(s, ent, "UCV-2", "UCV Locked Co", Decimal("1000.00"), "EUR", "USD", Decimal("1086.96"))
        # Unconvertible: a foreign row with NO usable lock — folded at face.
        inv(s, ent, "UCV-3", "UCV Unlocked Co", Decimal("500.00"), "USD")
        inv(s, ent, "UCV-4", "UCV Unlocked Co", Decimal("400.00"), "GBP")
        # Lock present but in a THIRD currency — ignored, and still foreign.
        inv(s, ent, "UCV-5", "UCV Wrong Lock Co", Decimal("700.00"), "EUR", "CHF", Decimal("9999"))
        await s.commit()


@pytest.mark.asyncio
async def test_vendor_entries_count_the_rows_they_folded_at_face_value(realdb):
    await _seed_currency_cases(realdb)
    entries = {
        e.vendor: e for e in await _grouped(realdb, period_start=date.today() - timedelta(days=7))
    }

    # Rate-locked EUR row converts; nothing unconverted.
    assert entries["UCV Locked Co"].amount == Decimal("2086.96")
    assert entries["UCV Locked Co"].unconverted_count == 0

    # GBP with no lock: added at FACE value, and said so.
    assert entries["UCV Unlocked Co"].amount == Decimal("900.00")
    assert entries["UCV Unlocked Co"].unconverted_count == 1

    # A lock in a third currency is not a lock.
    assert entries["UCV Wrong Lock Co"].amount == Decimal("700.00")
    assert entries["UCV Wrong Lock Co"].unconverted_count == 1


@pytest.mark.asyncio
async def test_a_lock_amount_without_a_lock_currency_is_not_a_lock(realdb):
    """A `reporting_amount` with no `reporting_currency` must count as
    unconverted, exactly as `reporting_amount_for_row` treats it.

    This is the case SQL three-valued logic gets wrong if the lock test is left
    as `UPPER(reporting_currency) = tgt`: for a NULL currency that comparison
    is NULL, `NOT NULL` is NULL, and the `unconverted` CASE falls to its `ELSE`
    — silently reporting the row as converted while its face value is what was
    added. `invoice_reporting_amount_sql` states `IS NOT NULL` explicitly; this
    is the test that the extra leg does something.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number="HALFLOCK-1",
                vendor_name="Half Lock Co",
                amount=Decimal("250.00"),
                currency="EUR",
                reporting_currency=None,
                reporting_amount=Decimal("275.00"),
                status=InvoiceStatus.approved,
                invoice_date=date.today(),
            )
        )
        await s.commit()

    entry = next(
        e
        for e in await _grouped(realdb, period_start=date.today() - timedelta(days=7))
        if e.vendor == "Half Lock Co"
    )
    # The half-written lock is ignored: the FACE amount is what was added...
    assert entry.amount == Decimal("250.00")
    # ...and the row says so.
    assert entry.unconverted_count == 1


@pytest.mark.asyncio
async def test_an_unknown_currency_row_is_unconverted_beside_a_usd_row(realdb):
    """NULL and blank invoice currencies are unknown (decisions §473): their
    own group, counted as unconverted, never read as the reporting currency —
    even when the row carries a 1:1 lock written before §473."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        for num, currency, rep_cur, rep_amt in (
            ("NOCUR-1", "USD", None, None),
            ("NOCUR-2", None, "USD", Decimal("40.00")),
        ):
            s.add(
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number=num,
                    vendor_name="No Currency Co",
                    amount=Decimal("100.00") if currency else Decimal("40.00"),
                    currency=currency,
                    reporting_currency=rep_cur,
                    reporting_amount=rep_amt,
                    status=InvoiceStatus.approved,
                    invoice_date=date.today(),
                )
            )
        await s.commit()

        rows = (
            (
                await s.execute(
                    invoice_currency_rollup_select(reporting_currency="USD").where(
                        Invoice.vendor_name == "No Currency Co"
                    )
                )
            )
            .mappings()
            .all()
        )
    rollup = rollup_from_grouped_rows([dict(r) for r in rows], reporting_currency="USD")
    assert rollup.unconverted_count == 1
    groups = {e.currency: e for e in rollup.by_currency}
    assert set(groups) == {"USD", None}
    assert groups[None].unconverted_count == 1
    assert groups["USD"].unconverted_count == 0

    entry = next(
        e
        for e in await _grouped(realdb, period_start=date.today() - timedelta(days=7))
        if e.vendor == "No Currency Co"
    )
    assert entry.unconverted_count == 1
    assert entry.currencies == ["USD"]


@pytest.mark.asyncio
async def test_cfo_concentration_reports_its_unconverted_count(realdb):
    await _seed_currency_cases(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        conc = (await c.get("/api/analytics/cfo")).json()["supplier_concentration"]

    # UCV-4 (GBP, no lock) and UCV-5 (EUR, CHF lock) could not be converted.
    assert conc["unconverted_count"] == 2
    # A count, never money — it must not be an exact-decimal string.
    assert isinstance(conc["unconverted_count"], int)


@pytest.mark.asyncio
async def test_cfo_concentration_unconverted_count_is_zero_on_a_clean_book(realdb):
    """The disclosure is honest in both directions — a fully-convertible book
    reports nothing outstanding, so a non-zero value always means something."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number="CLEAN-1",
                vendor_name="Clean Co",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=date.today(),
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="cfo") as c:
        conc = (await c.get("/api/analytics/cfo")).json()["supplier_concentration"]
    assert conc["unconverted_count"] == 0


@pytest.mark.asyncio
async def test_drill_through_reports_unconverted_per_row_and_for_the_period(realdb):
    await _seed_currency_cases(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/drill/spend_concentration?limit=1")).json()

    # Whole-period, taken BEFORE `limit` — it describes the same population as
    # `total_spend`, which is also pre-limit.
    assert body["unconverted_count"] == 2
    assert len(body["rows"]) == 1
    by_vendor = {r["vendor"]: r for r in body["rows"]}
    assert set(by_vendor) == {"UCV Locked Co"}
    assert by_vendor["UCV Locked Co"]["unconverted_count"] == 0

    full = None
    async with realdb.client(key=TENANT, role="cfo") as c:
        full = (await c.get("/api/analytics/drill/spend_concentration")).json()
    per_vendor = {r["vendor"]: r["unconverted_count"] for r in full["rows"]}
    assert per_vendor["UCV Unlocked Co"] == 1
    assert per_vendor["UCV Wrong Lock Co"] == 1
    # The whole-period figure does not move with `?limit=`, unlike a per-page
    # tally would.
    assert full["unconverted_count"] == body["unconverted_count"] == 2


# ---------------------------------------------------------------------------
# 3. The assistant's `get_vendor_spend` — the fifth consumer of the same rollup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assistant_vendor_spend_keeps_same_named_vendors_apart(realdb):
    """The assistant tool groups by `vendor_id`, not just by name.

    It RETURNS the id, so merging two vendor records that happen to share a
    `vendor_name` — an unmatched extraction beside a verified master record —
    would attribute one supplier's spend to the other and hand back an id that
    does not own the number beside it. `include_vendor_id=True` is what keeps
    them apart; the four AP surfaces group by name alone, deliberately, because
    they only label.
    """
    from app.models.vendor import Vendor
    from app.services.assistant.tools.schemas import VendorSpendParams
    from app.services.assistant.tools.vendor_spend import get_vendor_spend

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = date.today()
    ids = []
    async with mk() as s:
        ent = await _default_entity_id(s)
        for i, amount in enumerate((Decimal("400.00"), Decimal("100.00"))):
            v = Vendor(
                organization_id=org_id,
                entity_id=ent,
                name="Twinned Supplies",
                code=f"TWIN-{i}",
                status="verified" if i == 0 else "unverified",
            )
            s.add(v)
            await s.flush()
            ids.append(str(v.id))
            s.add(
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    vendor_id=v.id,
                    invoice_number=f"TWIN-{i}",
                    vendor_name="Twinned Supplies",
                    amount=amount,
                    currency="USD",
                    status=InvoiceStatus.approved,
                    invoice_date=today,
                )
            )
        await s.commit()

    async with mk() as s:
        result = await get_vendor_spend(
            s,
            org_id=org_id,
            entity_id=None,
            current_user_id=uuid.uuid4(),
            params=VendorSpendParams(period="ytd", top_n=10),
        )

    twins = [r for r in result.vendors if r.vendor_name == "Twinned Supplies"]
    assert len(twins) == 2, "same-named vendors must not be merged into one row"
    assert {r.vendor_id for r in twins} == set(ids)
    assert sorted(r.amount for r in twins) == [Decimal("100.00"), Decimal("400.00")]
    # `total_spend` is still the WHOLE set's total, so the shares add up.
    assert result.total_spend >= Decimal("500.00")


@pytest.mark.asyncio
async def test_assistant_vendor_spend_converts_before_summing(realdb):
    """A rate-locked foreign invoice converts; an unlockable one falls back to
    face value — the same rule the AP surfaces apply, because it is the same
    expression."""
    from app.services.assistant.tools.schemas import VendorSpendParams
    from app.services.assistant.tools.vendor_spend import get_vendor_spend

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = date.today()
    async with mk() as s:
        ent = await _default_entity_id(s)
        for num, amount, cur, rep_cur, rep_amt in (
            ("ASST-1", Decimal("1000.00"), "USD", None, None),
            ("ASST-2", Decimal("1000.00"), "EUR", "USD", Decimal("1086.96")),
            ("ASST-3", Decimal("400.00"), "GBP", None, None),
        ):
            s.add(
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number=num,
                    vendor_name="Assistant Co",
                    amount=amount,
                    currency=cur,
                    reporting_currency=rep_cur,
                    reporting_amount=rep_amt,
                    status=InvoiceStatus.approved,
                    invoice_date=today,
                )
            )
        await s.commit()

    async with mk() as s:
        result = await get_vendor_spend(
            s,
            org_id=org_id,
            entity_id=None,
            current_user_id=uuid.uuid4(),
            params=VendorSpendParams(period="ytd", top_n=10),
        )

    row = next(r for r in result.vendors if r.vendor_name == "Assistant Co")
    # 1000 + 1086.96 (locked) + 400 (face) — not the naive 2400.
    assert row.amount == Decimal("2486.96")


# ---------------------------------------------------------------------------
# 4. Drift guard — no call site may reintroduce the Python fold
# ---------------------------------------------------------------------------


def _app_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "app"


#: The row-at-a-time reducers. Both COLLAPSE a list of per-invoice dicts into
#: totals, which is the whole shape being guarded against: to call either from
#: a request or sweep path, the period's invoices must first be transferred and
#: reduced in Python, on the event loop, growing with the invoice table.
#: `invoice_currency_rollup_select` / `vendor_currency_rollup_select` do the
#: same reduction in SQL. They are kept as the readable statement of the rule
#: and as this file's reference implementations, so the guard is on their
#: CALLERS, not on their existence.
_COLLAPSING_REDUCERS = {
    "rollup_to_reporting_currency",
    "vendor_rollup_to_reporting_currency",
    # The dashboard's discount-capture tile. It was deliberately absent when
    # this guard was written, on the reasoning that the fold needs a per-ROW
    # verdict (captured / missed / still-capturable) rather than a per-currency
    # sum, so it could not become a plain `GROUP BY`. True of a per-currency
    # GROUP BY, but the verdict is two comparisons and expresses fine as a
    # `CASE` — see `api/dashboard`, which now groups by that CASE and reduces
    # via `discount_capture_from_grouped_rows`. Streaming every
    # discount-scheduled invoice into Python (no LIMIT, no date bound) is the
    # same unbounded shape as the five vendor-spend folds, so it is guarded the
    # same way. `compute_discount_capture` itself stays as the readable rule +
    # the equivalence test's reference implementation.
    "compute_discount_capture",
}

#: The row-at-a-time CONVERTER. Unlike the reducers above it has legitimate
#: uses — projecting one output row per input row (`commitment_rows`' cash
#: commitments, the dashboard's ten upcoming payments). What is never
#: legitimate is using it to build a per-KEY TOTAL, because that is an
#: aggregate a `GROUP BY` should have produced.
_ROW_CONVERTER = "reporting_amount_for_row"


def _called_names(node) -> set[str]:
    return {
        f.id if isinstance(f, ast.Name) else f.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        for f in [n.func]
        if isinstance(f, ast.Name | ast.Attribute)
    }


def _has_keyed_accumulation(node) -> bool:
    """Does this loop body accumulate into a SUBSCRIPT — i.e. per key?

    Catches both spellings of the fold:

        totals[key] += converted                     # AugAssign
        totals[key] = totals.get(key, 0) + converted  # Assign of a BinOp

    A scalar `total += x` is deliberately NOT matched: summing an
    already-bounded, already-materialised set into one figure is a different
    question from grouping, and the two remaining cases in `app/` are bounded
    by their own `LIMIT` / window.
    """
    for n in ast.walk(node):
        if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Subscript):
            return True
        if (
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Subscript) for t in n.targets)
            and isinstance(n.value, ast.BinOp)
            and isinstance(n.value.op, ast.Add)
        ):
            return True
    return False


def test_no_module_collapses_per_invoice_rows_in_python():
    """Nothing under `app/` may build a reporting-currency TOTAL by folding
    per-invoice rows in Python.

    Two shapes, because the previous version of this guard scanned for one
    function NAME and that is exactly why it missed the fifth instance: the
    assistant's `get_vendor_spend` never called the vendor rollup helper — it
    hand-rolled the same fold out of `reporting_amount_for_row` and a dict.

      1. any call to a collapsing reducer (`_COLLAPSING_REDUCERS`);
      2. a call to `reporting_amount_for_row` inside a loop or comprehension
         whose body accumulates into a per-key subscript — the hand-rolled
         `GROUP BY`.

    Fix either by grouping in SQL: `invoice_currency_rollup_select` for a
    whole-population rollup, `vendor_currency_rollup_select` for a per-vendor
    one, then `rollup_from_grouped_rows` / `vendor_rollup_from_grouped_rows`.
    """
    app_dir = _app_dir()
    owner = app_dir / "services" / "currency_conversion.py"

    offenders: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        if path == owner:
            continue
        rel = path.relative_to(app_dir.parent)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (
                    fn.id
                    if isinstance(fn, ast.Name)
                    else fn.attr
                    if isinstance(fn, ast.Attribute)
                    else None
                )
                if name in _COLLAPSING_REDUCERS:
                    offenders.append(f"{rel}:{node.lineno} calls {name}()")
                continue
            is_loop = isinstance(
                node, ast.For | ast.AsyncFor | ast.ListComp | ast.DictComp | ast.SetComp
            )
            if not is_loop:
                continue
            if _ROW_CONVERTER in _called_names(node) and _has_keyed_accumulation(node):
                offenders.append(
                    f"{rel}:{node.lineno} folds {_ROW_CONVERTER}() into a per-key total"
                )

    assert not offenders, (
        "per-invoice reporting-currency fold at: "
        f"{offenders}. Group in SQL instead — see "
        "app/services/currency_conversion.py's *_rollup_select builders."
    )


def test_the_drift_guard_actually_detects_both_shapes():
    """The guard above passes on a clean tree, so prove it can fail.

    Without this, deleting the detector's body would leave a green suite. Both
    shapes are exercised on synthetic source, including the hand-rolled dict
    fold the name-only version of this guard could not see.
    """
    reducer_call = ast.parse("entries = vendor_rollup_to_reporting_currency(rows, x=1)")
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in _COLLAPSING_REDUCERS
        for n in ast.walk(reducer_call)
    )

    aug = ast.parse(
        "for r in rows:\n"
        "    converted, _ = reporting_amount_for_row(amount=r.amount)\n"
        "    totals[r.vendor] += converted\n"
    ).body[0]
    assert _ROW_CONVERTER in _called_names(aug)
    assert _has_keyed_accumulation(aug)

    get_default = ast.parse(
        "for r in rows:\n"
        "    converted, _ = reporting_amount_for_row(amount=r.amount)\n"
        "    totals[r.vendor] = totals.get(r.vendor, 0) + converted\n"
    ).body[0]
    assert _has_keyed_accumulation(get_default)

    # A projection — one output row per input row — must NOT match, or the
    # guard would force three legitimate call sites into SQL they can't be
    # expressed in.
    projection = ast.parse(
        "for r in rows:\n"
        "    converted, unconverted = reporting_amount_for_row(amount=r.amount)\n"
        "    out.append({'amount': converted, 'unconverted': unconverted})\n"
    ).body[0]
    assert _ROW_CONVERTER in _called_names(projection)
    assert not _has_keyed_accumulation(projection)

    # ...and neither must a bounded scalar total.
    scalar = ast.parse(
        "for r in rows:\n"
        "    converted, _ = reporting_amount_for_row(amount=r.amount)\n"
        "    total += converted\n"
    ).body[0]
    assert not _has_keyed_accumulation(scalar)


def test_grouped_vendor_select_is_never_limited_in_sql():
    """The builder must return every vendor.

    `compute_supplier_concentration` derives `total_spend` and every
    `*_share_pct` from the list it is handed, so a `LIMIT` in this query would
    rebase every share onto the slice — `top_50_share_pct` becomes 100.0 by
    construction. Slicing is the caller's job, after the rollup.
    """
    compiled = str(
        vendor_spend_grouped_select(reporting_currency="USD", period_start=date(2024, 1, 1))
    ).upper()
    assert "GROUP BY" in compiled
    assert "LIMIT" not in compiled


def test_grouped_vendor_select_applies_the_conversion_rule_in_sql():
    """Non-tautological shape check: the reporting sum and the unconverted
    count are both CASE expressions inside aggregates, not columns streamed
    out for Python to reduce."""
    compiled = str(
        vendor_spend_grouped_select(reporting_currency="USD", period_start=date(2024, 1, 1))
    )
    assert compiled.count("sum(CASE WHEN") == 2, compiled
    assert "GROUP BY invoices.vendor_name" in compiled
    # Exactly the five columns `vendor_rollup_from_grouped_rows` consumes —
    # nothing per-invoice comes back over the wire.
    assert " AS reporting_amount" in compiled
    assert " AS unconverted_count" in compiled
    assert "invoices.invoice_date" not in compiled.split("FROM")[0]
