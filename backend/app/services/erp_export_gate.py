"""The ERP push refuses an invoice whose liability is itself in question.

Approval gates on no payment-blocking exception, so an approved invoice can
still carry an open ``duplicate`` or ``fraud_flag``. Our payment runs refuse
it, but the ERP push used to post it anyway: the bill then lived in the ERP's
books, where a bookkeeper can pay it directly and our run gate never sees it.
The push now refuses while one of ``api/payments.ERP_EXPORT_BLOCKING_EXCEPTION_TYPES``
is open or escalated (decisions §286).

Two places ask, with one predicate (``payment_runs.blocking_exception_types``):

* ``services/erp._call_erp``, directly before the adapter: every push passes
  through it (manual send, retry, the workflow's own advance, the local
  dispatcher and the Lambda worker), and it re-reads on every attempt, so a
  flag raised between retries stops the next one. It raises
  ``ErpPostRefusedError`` with :func:`blocked_export_refusal`, which lands the
  invoice at ``failed`` without spending the retry budget.
* the three HTTP doors in ``api/workflow.py``, before they move the invoice to
  ``sending_to_erp``, so the user gets a 409 saying what to do instead of a
  queued push that fails a moment later, and the invoice stays where it was.

Clearing the flag goes through ``exception_lifecycle.record_decision``, whose
segregation check refuses anyone implicated in the invoice. Nothing here
bypasses that: the gate only reads the exceptions.

Every string here is PII-free: an exception type is a fixed literal.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

#: The refusal code, registered in ``erp_adapters/base.ERP_REFUSAL_CODES`` so
#: the invoice modal states it (``frontend/src/lib/api/erpRefusals.ts``) and in
#: the coded-refusal registry the 409 is localized by (``codedRefusals.ts``).
EXCEPTION_BLOCKS_ERP_EXPORT = "exception_blocks_erp_export"


async def erp_export_blocking_exception(db: AsyncSession, invoice_id: uuid.UUID) -> str | None:
    """The type of the open or escalated exception that refuses this push, or None.

    When an invoice carries both, the one earliest in
    ``ERP_EXPORT_BLOCKING_EXCEPTION_TYPES`` is named.
    """
    from app.api.payments import ERP_EXPORT_BLOCKING_EXCEPTION_TYPES
    from app.services.payment_runs import blocking_exception_types

    blocked = await blocking_exception_types(
        db, [invoice_id], types=ERP_EXPORT_BLOCKING_EXCEPTION_TYPES
    )
    return blocked.get(invoice_id)


def blocked_export_message(exception_type: str) -> str:
    """The English ``error`` on the ``invoice.erp_failed`` row."""
    return f"ERP post refused: {EXCEPTION_BLOCKS_ERP_EXPORT} ({exception_type})"


def blocked_export_refusal(exception_type: str) -> dict:
    """The coded ``refusal`` beside it, in ``erp.erp_refusal_record``'s shape."""
    return {"code": EXCEPTION_BLOCKS_ERP_EXPORT, "params": {"exception_type": exception_type}}


async def refuse_blocked_erp_export(db: AsyncSession, invoice_id: uuid.UUID) -> None:
    """409 before an HTTP door queues a push the chokepoint would refuse.

    Commits before raising. A raised ``HTTPException`` rolls the request's
    session back (``tenant.get_tenant_db``), and ``POST /complete`` runs
    ``refresh_warnings`` first: the duplicate it may have opened in this very
    request is the exception the refusal tells the user to resolve, so rolling
    it back would send them to an Exceptions queue with nothing in it. The
    invoice itself is unchanged, since every door calls this before its
    transition.
    """
    from app.api.refusals import coded_refusal

    exception_type = await erp_export_blocking_exception(db, invoice_id)
    if exception_type is None:
        return
    await db.commit()
    raise HTTPException(
        status_code=409,
        detail=coded_refusal(
            EXCEPTION_BLOCKS_ERP_EXPORT,
            f"This invoice has an open {exception_type.replace('_', ' ')} exception. "
            "Resolve or dismiss it in Exceptions, then send it to the ERP.",
            exception_type=exception_type,
        ),
    )
