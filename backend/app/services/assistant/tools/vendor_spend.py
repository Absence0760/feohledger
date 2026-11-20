"""``get_vendor_spend`` tool — top-N vendor spend over a period.

Wraps ``services.analytics.compute_supplier_concentration``. The committed-status
set is ``services/open_ap_due.COMMITTED_STATUSES`` (imported, not duplicated). All sums
are ``Numeric``/``Decimal``.

Rolled into the org's reporting currency (not a naive SUM across currencies):
a vendor billing in more than one currency, or a tenant with vendors in
different currencies, used to add e.g. USD + EUR amounts as if they were one
currency and hand the mixed total to the assistant labeled with a single
currency code.

That rollup is done **in SQL**, by the same
`currency_conversion.vendor_currency_rollup_select` +
`vendor_rollup_from_grouped_rows` pair the four AP spend surfaces use. It used
to select five columns of every committed invoice in the period and fold them
in Python — a synchronous per-row loop inside an `async def` whose cost grew
with the invoice table (`docs/decisions.md` §103, and
`backend/docs/analytics.md` § Per-vendor spend is one query). The population
stays this tool's own (`COMMITTED_STATUSES`, not the AP surfaces'
"everything but rejected"); only the grouping and the conversion are shared.

It groups by `vendor_id` as well as name, because this tool RETURNS the id:
two distinct vendor records can carry the same `vendor_name`, and merging them
would attribute one supplier's spend to another.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.services.analytics import compute_supplier_concentration
from app.services.assistant.tools._currency import resolve_org_currency
from app.services.assistant.tools.schemas import (
    VendorSpendParams,
    VendorSpendResult,
    VendorSpendRow,
)
from app.services.currency_conversion import (
    vendor_currency_rollup_select,
    vendor_rollup_from_grouped_rows,
)
from app.services.open_ap_due import COMMITTED_STATUSES
from app.tenant import apply_entity_scope
from app.utils.dates import utc_today

_PERIOD_LABELS = {
    "mtd": "month-to-date",
    "qtd": "quarter-to-date",
    "ytd": "year-to-date",
    "last_30d": "last 30 days",
    "last_90d": "last 90 days",
    "last_12m": "last 12 months",
}


def _period_start(period: str, today: date) -> date:
    if period == "mtd":
        return today.replace(day=1)
    if period == "qtd":
        q_first_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_first_month, day=1)
    if period == "ytd":
        return today.replace(month=1, day=1)
    if period == "last_30d":
        return today - timedelta(days=30)
    if period == "last_90d":
        return today - timedelta(days=90)
    if period == "last_12m":
        return today - timedelta(days=365)
    return today.replace(month=1, day=1)


async def get_vendor_spend(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    params: VendorSpendParams,
    control_db: AsyncSession | None = None,
) -> VendorSpendResult:
    today = utc_today()
    start = _period_start(params.period, today)
    currency = await resolve_org_currency(org_id, control_db)

    stmt = (
        vendor_currency_rollup_select(reporting_currency=currency, include_vendor_id=True)
        .where(Invoice.status.in_(COMMITTED_STATUSES))
        .where(Invoice.invoice_date >= start)
    )
    stmt = apply_entity_scope(stmt, Invoice, entity_id)
    rows = (await db.execute(stmt)).mappings().all()

    # Postgres has already converted and grouped; this reduces the
    # `vendors x currencies` rows into one entry per vendor, already ordered
    # (total DESC, name ASC).
    entries = vendor_rollup_from_grouped_rows([dict(r) for r in rows], reporting_currency=currency)

    # Shape into the dicts compute_supplier_concentration expects. The WHOLE
    # list, never a pre-sliced top-N: it derives its denominator from what it
    # is handed, so slicing first rebases every share (see its docstring).
    vendor_spend = [
        {"vendor": e.vendor or "", "vendor_id": e.vendor_id, "amount": e.amount} for e in entries
    ]

    snapshot = compute_supplier_concentration(vendor_spend)
    total_spend = snapshot.total_spend

    out_rows: list[VendorSpendRow] = []
    for r in vendor_spend[: params.top_n]:
        amount = r["amount"]
        share = (
            (amount / total_spend * Decimal("100")).quantize(Decimal("0.1"))
            if total_spend > 0
            else Decimal("0.0")
        )
        out_rows.append(
            VendorSpendRow(
                vendor_id=r["vendor_id"],
                vendor_name=r["vendor"],
                amount=amount.quantize(Decimal("0.01")),
                share_pct=share,
            )
        )

    return VendorSpendResult(
        period_label=_PERIOD_LABELS.get(params.period, params.period),
        currency=currency,
        total_spend=total_spend,
        vendors=out_rows,
    )
