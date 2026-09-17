"""Spend-to-contract rollup.

Aggregates the invoices linked to a contract (``Invoice.contract_id``) into a
spend summary: invoiced total, count, and how that sits against the contract's
``spend_limit``. Money stays exact — the sum runs in the DB over the
``Numeric`` ``amount`` column and stays ``Decimal`` all the way into
``ContractSpendSummary``, whose ``MoneyAmount`` fields do the float hop once, at
JSON-write time.

Rejected invoices are excluded — a rejected bill never became real spend. The
sum is also scoped to the contract's own ``currency`` — same as
``budget_service``'s rollups, the legs never convert, so mixing currencies
would add unlike face values and could misreport ``over_limit``.
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contract import Contract
from app.models.invoice import Invoice, InvoiceStatus
from app.schemas.contract import ContractSpendSummary


async def compute_spend_summary(db: AsyncSession, contract: Contract) -> ContractSpendSummary:
    total, count = (
        await db.execute(
            select(
                func.coalesce(func.sum(Invoice.amount), 0),
                func.count(),
            ).where(
                Invoice.contract_id == contract.id,
                Invoice.status != InvoiceStatus.rejected,
                Invoice.currency == contract.currency,
            )
        )
    ).one()

    invoiced = Decimal(total or 0)
    limit = contract.spend_limit
    remaining = (limit - invoiced) if limit is not None else None
    over_limit = limit is not None and invoiced > limit

    return ContractSpendSummary(
        invoiced_total=invoiced,
        invoice_count=int(count or 0),
        spend_limit=limit,
        remaining=remaining,
        over_limit=over_limit,
    )
