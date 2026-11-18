"""One rule for "which payment schedule is this invoice's" (issue #575).

The cash forecast took the newest row by `created_at` then `id`; the discount
auto-capture sweep and the per-invoice ROI took it by `created_at` alone. Two
rows written in one transaction share `created_at`, so those two picked either
row while the forecast picked the higher id — the discount engine and the cash
forecast could disagree on an invoice's due date. All three read
`services/payment_schedules` now; this pins them to one answer on that exact
tie.

The readers that JOIN schedules onto invoices — the payment queue's page, its
early-pay savings rollup and the dashboard's discount-capture tile — join
`latest_schedules()`, the same rule as a `DISTINCT ON` subquery. Joined to the
table, an invoice with two schedule rows was listed twice in the queue (and
pushed the next invoice off its page) and its discount was counted twice in
both aggregates; the second half of this file pins each of them to one row.

DO NOT run this file standalone in a concurrent build — the ``realdb`` fixture
truncates all tables sequentially.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.api.analytics import _commitment_rows
from app.api.pagination import PaginationParams
from app.models.discount import DiscountOffer
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentSchedule
from app.services.discount_auto_trigger import _resolve_due_date
from app.services.payment_schedules import latest_schedule, latest_schedules
from app.utils.dates import utc_today

APP = Path(__file__).resolve().parents[1] / "app"


async def _invoice_with_tied_schedules(realdb):
    """An approved invoice with two schedules sharing one `created_at`. The
    ids are chosen so the higher id is the LATER due date, and the lower id is
    inserted first — so an ORDER BY with no id tiebreak, reading heap order,
    returns the wrong row (which is what the two drifted copies did)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    today = utc_today()
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    low = uuid.UUID(int=1)
    high = uuid.UUID(int=2)
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            vendor_name="Acme Supply",
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            amount=Decimal("100.00"),
            currency="USD",
            status=InvoiceStatus.approved,
            due_date=today + timedelta(days=60),
        )
        s.add(inv)
        await s.flush()
        s.add(
            PaymentSchedule(
                id=low,
                invoice_id=inv.id,
                due_date=today + timedelta(days=10),
                created_at=stamp,
                updated_at=stamp,
            )
        )
        await s.flush()
        s.add(
            PaymentSchedule(
                id=high,
                invoice_id=inv.id,
                due_date=today + timedelta(days=20),
                created_at=stamp,
                updated_at=stamp,
            )
        )
        await s.commit()
        return inv.id, today + timedelta(days=20)


async def test_every_reader_picks_the_same_schedule_on_a_created_at_tie(realdb):
    invoice_id, expected_due = await _invoice_with_tied_schedules(realdb)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        chosen = await latest_schedule(s, invoice_id)
        assert chosen is not None and chosen.due_date == expected_due

        offer = DiscountOffer(invoice_id=invoice_id)
        assert await _resolve_due_date(s, offer) == expected_due

        rows = await _commitment_rows(s, today=utc_today(), horizon_days=90, include_pending=False)
        (row,) = [r for r in rows if r["invoice_id"] == str(invoice_id)]
        assert row["due_date"] == expected_due


async def test_falls_back_to_the_invoice_due_date_without_a_schedule(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    due = date(2030, 1, 31)
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            vendor_name="Acme Supply",
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            amount=Decimal("100.00"),
            currency="USD",
            status=InvoiceStatus.approved,
            due_date=due,
        )
        s.add(inv)
        await s.commit()
        assert await latest_schedule(s, inv.id) is None
        assert await _resolve_due_date(s, DiscountOffer(invoice_id=inv.id)) == due


def test_no_module_orders_payment_schedules_itself():
    """A new reader must use `LATEST_SCHEDULE_ORDER` / `latest_schedule`,
    not its own `PaymentSchedule.created_at.desc()`."""
    owner = APP / "services" / "payment_schedules.py"
    hits = []
    for path in sorted(APP.rglob("*.py")):
        if path == owner:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "created_at"
                and isinstance(node.value, ast.Name)
                and node.value.id == "PaymentSchedule"
            ):
                hits.append(f"{path.relative_to(APP.parent)}:{node.lineno}")
    assert hits == []


# ---------------------------------------------------------------------------
# Readers that JOIN schedules: one invoice, one row, one discount.
# ---------------------------------------------------------------------------

#: The superseded schedule's terms (older `created_at`) and the current one's.
#: Different percentages, so a reader that picks the wrong row, or both, shows
#: a different figure rather than an accidentally-equal one.
_OLD_PERCENT = Decimal("10.00")
_CURRENT_PERCENT = Decimal("2.00")


async def _invoice_with_two_schedules(
    s,
    org_id,
    ent,
    *,
    number: str,
    amount: Decimal,
    currency: str,
    discount_date: date,
    due_date: date | None = None,
) -> uuid.UUID:
    """An approved invoice carrying a superseded schedule and a current one,
    both with a discount window ending `discount_date`. The superseded row has
    the HIGHER id, so a reader ordering by id alone would pick it."""
    inv = Invoice(
        organization_id=org_id,
        entity_id=ent,
        vendor_name="Two Schedules Co",
        invoice_number=number,
        amount=amount,
        currency=currency,
        status=InvoiceStatus.approved,
        due_date=due_date,
        correlation_id=uuid.uuid4(),
    )
    s.add(inv)
    await s.flush()
    old_stamp = datetime(2026, 1, 1, tzinfo=UTC)
    new_stamp = datetime(2026, 2, 1, tzinfo=UTC)
    for stamp, percent, sid in (
        (old_stamp, _OLD_PERCENT, uuid.UUID("ffffffff" + uuid.uuid4().hex[8:])),
        (new_stamp, _CURRENT_PERCENT, uuid.UUID("00000000" + uuid.uuid4().hex[8:])),
    ):
        s.add(
            PaymentSchedule(
                id=sid,
                invoice_id=inv.id,
                due_date=discount_date + timedelta(days=20),
                discount_date=discount_date,
                discount_percent=percent,
                created_at=stamp,
                updated_at=stamp,
            )
        )
    await s.flush()
    return inv.id


def _queue_org(org_id):
    from types import SimpleNamespace

    return SimpleNamespace(id=org_id, name="PyTest", slug="pytesta", settings={})


def _queue_user(uid):
    from types import SimpleNamespace

    return SimpleNamespace(id=uid, full_name="Queue Tester", roles=["admin"])


async def test_queue_lists_an_invoice_with_two_schedules_once_on_its_current_terms(realdb):
    """Two invoices, page size one. Joined to every schedule row, invoice A
    (earlier due date) filled page 1 AND page 2, and B never appeared; the
    early-pay banner summed A's discount on both schedules."""
    from app.api.payments import payment_queue

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    today = utc_today()
    window = today + timedelta(days=10)
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        a = await _invoice_with_two_schedules(
            s,
            org_id,
            ent,
            number="TWO-SCHED-A",
            amount=Decimal("1000.00"),
            currency="CHF",
            discount_date=window,
            due_date=today + timedelta(days=30),
        )
        b = Invoice(
            organization_id=org_id,
            entity_id=ent,
            vendor_name="One Schedule Co",
            invoice_number="TWO-SCHED-B",
            amount=Decimal("500.00"),
            currency="CHF",
            status=InvoiceStatus.approved,
            due_date=today + timedelta(days=40),
            correlation_id=uuid.uuid4(),
        )
        s.add(b)
        await s.commit()
        b_id = b.id

    pages = []
    async with mk() as db:
        for page in (1, 2):
            pages.append(
                await payment_queue(
                    pagination=PaginationParams(page=page, page_size=1),
                    db=db,
                    org=_queue_org(org_id),
                    user=_queue_user(realdb.info("a").users["admin"]),
                    entity_id=None,
                )
            )

    assert [p["total"] for p in pages] == [2, 2]
    listed = [item["id"] for p in pages for item in p["items"]]
    assert listed == [str(a), str(b_id)]

    row_a = pages[0]["items"][0]
    # The CURRENT schedule's terms: 2% of 1000.00, not 10%.
    assert row_a["discount_percent"] == float(_CURRENT_PERCENT)
    assert row_a["discount_amount"] == "20.00"
    # A's discount counted once in the whole-set rollup (B has no schedule).
    bucket = next(e for e in pages[0]["by_currency"] if e["currency"] == "CHF")
    assert bucket["total_savings"] == "20.00"
    assert bucket["count"] == 2


async def test_dashboard_discount_capture_counts_an_invoice_with_two_schedules_once(realdb):
    """Joined to every schedule row, the tile counted one missed discount as
    two eligible invoices and summed both schedules' figures."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    today = utc_today()
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        # Elapsed window, unpaid: a miss.
        await _invoice_with_two_schedules(
            s,
            org_id,
            ent,
            number="TWO-SCHED-MISS",
            amount=Decimal("1000.00"),
            currency="USD",
            discount_date=today - timedelta(days=5),
        )
        # Paid inside the window: a capture, still one invoice despite two
        # schedules AND a completed payment on the outer join.
        paid = await _invoice_with_two_schedules(
            s,
            org_id,
            ent,
            number="TWO-SCHED-CAPTURED",
            amount=Decimal("500.00"),
            currency="USD",
            discount_date=today - timedelta(days=5),
        )
        s.add(
            Payment(
                entity_id=ent,
                invoice_id=paid,
                amount=Decimal("490.00"),
                method="ach",
                provider="mock",
                status="completed",
                completed_at=datetime.combine(
                    today - timedelta(days=6), datetime.min.time(), tzinfo=UTC
                ),
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        d = (await c.get("/api/dashboard")).json()["discount_capture"]

    assert d["eligible_count"] == 2
    assert d["missed_count"] == 1
    assert d["captured_count"] == 1
    # 2% of each, on the current schedule — not 10%, and not 2% + 10%.
    assert Decimal(str(d["missed_amount"])) == Decimal("20.00")
    assert Decimal(str(d["captured_amount"])) == Decimal("10.00")


async def test_latest_schedules_join_agrees_with_latest_schedule(realdb):
    """The join form and the per-invoice lookup are one rule: on the
    `created_at` tie from the first half of this file, both pick the same row."""
    invoice_id, expected_due = await _invoice_with_tied_schedules(realdb)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        current = latest_schedules()
        rows = (
            await s.execute(
                select(current.due_date)
                .select_from(Invoice)
                .join(current, current.invoice_id == Invoice.id)
                .where(Invoice.id == invoice_id)
            )
        ).all()
        assert [r.due_date for r in rows] == [expected_due]
        assert (await latest_schedule(s, invoice_id)).due_date == expected_due


def test_no_module_joins_the_payment_schedules_table_directly():
    """A reader joining schedules onto invoices joins `latest_schedules()`;
    `.join(PaymentSchedule, …)` / `.outerjoin(PaymentSchedule, …)` fans an
    invoice out to one row per schedule."""
    hits = []
    for path in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"join", "outerjoin", "join_from", "outerjoin_from"}
                and any(
                    isinstance(arg, ast.Name) and arg.id == "PaymentSchedule" for arg in node.args
                )
            ):
                hits.append(f"{path.relative_to(APP.parent)}:{node.lineno}")
    assert hits == []
