"""The 1099 calendar-year cutoff must be pinned to UTC, not the Postgres
session's `timezone` GUC.

`build_1099_report` joins completed payments to their vendor with
`extract("year", Payment.completed_at) == year`. `Payment.completed_at` is
`TIMESTAMPTZ`, and Postgres's `EXTRACT(YEAR FROM …)` on a `timestamptz`
implicitly converts the value into the SESSION's `timezone` setting before
pulling the year out of it — unlike a plain `timestamp`, which carries no
zone conversion at all. A payment completed right after midnight UTC on
January 1st is still December 31st in every zone west of UTC, so on a
non-UTC server session it silently slid into the wrong tax year: excluded
from the (correct) new year's report and counted in the (wrong) prior year's.

This is the same class of bug `bank_reconciliation.py`'s `sent_on_expr`
already guards against for its own `completed_at`-derived date filter — this
file pins the fix for the 1099 aggregation path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.services.tax_1099 import build_1099_report

# Payments right on the UTC year boundary — the exact case a session-timezone
# read would misclassify.
_NEW_YEAR_PAYMENT_UTC = datetime(2027, 1, 1, 2, 0, tzinfo=UTC)  # 2027 in UTC…
# …but 2026-12-31 18:00 in America/Los_Angeles (UTC-8) — a session in that
# zone would EXTRACT(YEAR) this as 2026 under the old bare `extract` call.
_NON_UTC_SESSION_TZ = "America/Los_Angeles"


async def _vendor(mk, org_id, *, name):
    async with mk() as s:
        v = Vendor(organization_id=org_id, name=name, tax_id="12-3456789", is_1099_eligible=True)
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return v.id


async def _paid_invoice(mk, org_id, vendor_id, amount, completed_at):
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"INV-{vendor_id}-{uuid.uuid4().hex[:8]}",
            vendor_name="x",
            amount=Decimal(amount),
            currency="USD",
            status=InvoiceStatus.paid,
            vendor_id=vendor_id,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        p = Payment(
            invoice_id=inv.id,
            amount=Decimal(amount),
            status="completed",
            completed_at=completed_at,
        )
        s.add(p)
        await s.commit()


async def test_year_boundary_payment_lands_in_its_utc_year_on_a_non_utc_session(realdb):
    """A payment completed at 2027-01-01 02:00 UTC must count toward the 2027
    report — not 2026 — even when the querying session's own `timezone` GUC is
    set to a zone west of UTC that would otherwise reclassify it as
    2026-12-31.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    vendor_id = await _vendor(mk, org_id, name="Year Boundary Co")
    await _paid_invoice(mk, org_id, vendor_id, "750.00", _NEW_YEAR_PAYMENT_UTC)

    async with mk() as s:
        # Simulate a server whose Postgres session defaults to a non-UTC
        # zone — the exact condition the bug needs to manifest. Executed on
        # THIS session/connection, before the report query runs on it.
        await s.execute(text(f"SET TIME ZONE '{_NON_UTC_SESSION_TZ}'"))

        report_2027 = await build_1099_report(s, org_id, 2027)
        report_2026 = await build_1099_report(s, org_id, 2026)

    row_2027 = next((r for r in report_2027.rows if r.vendor_id == vendor_id), None)
    row_2026 = next((r for r in report_2026.rows if r.vendor_id == vendor_id), None)

    assert row_2027 is not None
    # Correctly counted in the UTC year the payment actually completed in.
    assert row_2027.ytd_paid == Decimal("750.00")
    assert row_2027.payment_count == 1

    # And NOT double-counted (or mis-counted) into the prior UTC year, which
    # is what a session-timezone-dependent EXTRACT would have done.
    assert row_2026 is None or row_2026.ytd_paid == Decimal("0")


async def test_year_boundary_payment_still_correct_on_a_utc_session(realdb):
    """Sanity check: the same payment, queried from a UTC session (the
    deployed default), lands in 2027 too — proving the fix doesn't merely
    shift the bug onto UTC sessions."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    vendor_id = await _vendor(mk, org_id, name="Year Boundary Co UTC")
    await _paid_invoice(mk, org_id, vendor_id, "300.00", _NEW_YEAR_PAYMENT_UTC)

    async with mk() as s:
        await s.execute(text("SET TIME ZONE 'UTC'"))
        report_2027 = await build_1099_report(s, org_id, 2027)

    row = next((r for r in report_2027.rows if r.vendor_id == vendor_id), None)
    assert row is not None
    assert row.ytd_paid == Decimal("300.00")
