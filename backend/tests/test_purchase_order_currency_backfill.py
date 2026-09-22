"""Migration 0099: ``purchase_orders.currency`` and its requisition backfill.

The migration's own SQL constants are run against a real tenant database. The
backfill's contract (``docs/decisions.md`` §197, the operator's call of
2026-09-22):

* a PO converted from a requisition takes that requisition's currency —
  normalised, since the requisition schema admitted lower case;
* every other PO stays NULL — no org-currency proxy;
* a requisition code that is not three letters proves nothing;
* two linking requisitions that disagree prove nothing either;
* a PO that already records a currency is never overwritten, so a re-run is a
  no-op.

Real-Postgres harness (``realdb``).
"""

from __future__ import annotations

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text

from app.models.procurement import PurchaseOrder, PurchaseRequisition, RequisitionStatus

TENANT = "a"
_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0099_purchase_order_currency.py"
)


def _migration_module():
    """Import the migration for its SQL constants (it never runs ``op`` here)."""
    spec = importlib.util.spec_from_file_location("_mig_0099", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    mig = _migration_module()
    assert mig.revision == "0099_purchase_order_currency"
    assert mig.down_revision == "0098_exception_raiser"


async def _columns(session) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'purchase_orders'"
        )
    )
    return set(rows.scalars().all())


async def test_column_statements_apply_and_are_idempotent(realdb):
    mig = _migration_module()
    async with realdb.sessionmaker(TENANT)() as s:
        try:
            await s.execute(text(mig.DROP_COLUMN_SQL))
            await s.commit()
            assert "currency" not in await _columns(s)
        finally:
            for _ in range(2):  # idempotent: ADD COLUMN IF NOT EXISTS
                await s.execute(text(mig.ADD_COLUMN_SQL))
                await s.commit()
        assert "currency" in await _columns(s)


async def _po(s, org_id, number: str, currency: str | None = None) -> uuid.UUID:
    po = PurchaseOrder(
        po_number=number,
        total=Decimal("100.00"),
        currency=currency,
        status="open",
        organization_id=org_id,
    )
    s.add(po)
    await s.flush()
    return po.id


def _req(org_id, po_id: uuid.UUID | None, currency: str) -> PurchaseRequisition:
    return PurchaseRequisition(
        requisition_number=f"REQ-{uuid.uuid4().hex[:8]}",
        requester_user_id=uuid.uuid4(),
        status=RequisitionStatus.converted if po_id else RequisitionStatus.approved,
        total=Decimal("100.00"),
        currency=currency,
        converted_po_id=po_id,
        organization_id=org_id,
    )


async def test_backfill_copies_only_a_provable_requisition_currency(realdb):
    mig = _migration_module()
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with mk() as s:
        from_eur = await _po(s, org_id, "PO-BF-EUR")
        from_lower = await _po(s, org_id, "PO-BF-LOWER")
        unlinked = await _po(s, org_id, "PO-BF-NONE")
        already = await _po(s, org_id, "PO-BF-KEEP", currency="USD")
        malformed = await _po(s, org_id, "PO-BF-BAD")
        disputed = await _po(s, org_id, "PO-BF-TWO")
        agreed = await _po(s, org_id, "PO-BF-AGREE")
        s.add_all(
            [
                _req(org_id, from_eur, "EUR"),
                _req(org_id, from_lower, "gbp"),
                _req(org_id, already, "EUR"),
                _req(org_id, malformed, "US"),
                _req(org_id, disputed, "EUR"),
                _req(org_id, disputed, "USD"),
                _req(org_id, agreed, "JPY"),
                _req(org_id, agreed, "jpy"),
                # A requisition that was never converted links no PO at all.
                _req(org_id, None, "CHF"),
            ]
        )
        await s.commit()

        for _ in range(2):  # the second run must change nothing
            await s.execute(text(mig.BACKFILL_SQL))
            await s.commit()

        got = dict(
            (await s.execute(select(PurchaseOrder.id, PurchaseOrder.currency))).tuples().all()
        )

    assert got[from_eur] == "EUR"
    assert got[from_lower] == "GBP"  # normalised on the way across
    assert got[unlinked] is None  # no requisition → no claim, not the org's currency
    assert got[already] == "USD"  # a recorded code is never overwritten
    assert got[malformed] is None  # "US" proves nothing
    assert got[disputed] is None  # two answers is not one
    assert got[agreed] == "JPY"  # the same code twice is one answer
