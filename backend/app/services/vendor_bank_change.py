"""The payee-change ``fraud_flag``: one invoice at a time.

A bank-detail change approved on a vendor (``api/vendors``) and a re-payment
to details the original payment did not go to (``services/payee_stamp``) raise
the same flag through ``flag_invoice_for_bank_change``, de-duplicated on the
finding and the raiser (decisions §286, §383). It lived in the vendors router,
which ``payee_stamp`` then had to import; this module owns it.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice


async def flag_invoice_for_bank_change(
    db: AsyncSession,
    *,
    invoice: Invoice,
    actor_id: uuid.UUID | None,
    code: str = "vendor_bank_changed",
) -> None:
    """Open a payee-change ``fraud_flag`` on ONE invoice, unless an open or
    escalated one with the same finding AND the same raiser is already there.

    ``code`` is the finding: ``vendor_bank_changed`` for a bank-detail change,
    or one of the re-payment findings of ``services/payee_stamp`` (decisions
    §383). ``actor_id`` is the person whose act changed the payee — the bank
    change's approver, or the merge's actor (``raised_by_user_id``, see
    `api/vendors._flag_payable_invoices_for_bank_change`).

    De-duplicated on the finding and the raiser, not on any fraud flag: a person
    clearing an unrelated fraud flag (a Positive Pay return, a detector's
    finding, raised by nobody) must not thereby clear a payee change they never
    saw, which is what the type-wide count allowed. And a second approver's bank
    change (or a second steward's merge) gets its own row: folded into the
    first person's open flag, it could be cleared by the second person, who is
    not that row's raiser (decisions §383). A row raised before §358 has no
    code and is matched on its English.
    """
    from app.models.exception import Exception as APException
    from app.services.exception_service import create_exception
    from app.services.invoice_warning_catalog import exception_finding

    finding = exception_finding(code)
    existing = (
        await db.execute(
            select(func.count()).where(
                APException.invoice_id == invoice.id,
                APException.exception_type == "fraud_flag",
                APException.status.in_(["open", "escalated"]),
                APException.raised_by_user_id.is_not_distinct_from(actor_id),
                or_(
                    APException.description_code == finding["code"],
                    (APException.description_code.is_(None))
                    & (APException.description == finding["message"]),
                ),
            )
        )
    ).scalar() or 0
    if existing:
        return
    await create_exception(
        db,
        exception_type="fraud_flag",
        severity="error",
        description=finding["message"],
        description_code=finding["code"],
        description_params=finding["params"],
        status="open",
        organization_id=invoice.organization_id,
        invoice=invoice,
        raised_by_user_id=actor_id,
    )


async def reflag_voided_invoice_for_bank_change(
    db: AsyncSession, *, invoice: Invoice, paid_since: datetime
) -> None:
    """A voided payment sends its invoice back to ``approved``. If the
    vendor's bank details were changed (approved) after that payment was
    made, the original payment went to the OLD account and the re-payment
    would go to the new one — with no flag, because the change skipped the
    invoice while it was ``paid`` / out of reach (`api/vendors._MONEY_MOVED_STATUSES`).
    Raise the same flag the change would have raised, once per approver of a
    change since, each stamped as its raiser (decisions §286, §383).

    Asked only for a payment with no payee stamp (paid before migration 0128):
    a stamped one carries the fingerprint of the details it paid, which
    `services/payee_stamp.payee_changes_since` compares directly (§383)."""
    from app.services.payee_stamp import bank_change_approvers_since

    approvers = await bank_change_approvers_since(db, invoice.vendor_id, since=paid_since)
    for approver in dict.fromkeys(approvers):
        await flag_invoice_for_bank_change(db, invoice=invoice, actor_id=approver)
