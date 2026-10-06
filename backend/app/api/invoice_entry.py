"""Who may ENTER an invoice, and how far that reach extends.

Standard AP practice puts invoice intake, validation and GL coding with the AP
clerk; segregation of duties sits between that ENTRY work and approval /
payment release. This app enforces the second half at approval —
`approval_chain.violates_segregation` refuses the uploader ∪
`segregation_actor_ids`, and every create path stamps `uploaded_by_id` — so the
clerk can do the first half without weakening it.

`INVOICE_ENTRY_ROLES` is the gate on the entry endpoints (create, upload,
attach / replace / remove the source file, field + line-item edits, extract /
reset extraction, submit for review, resubmit after a rejection, CSV import and
the entry-only bulk status targets). `INVOICE_MANAGE_ROLES` is everything else
an invoice route reaches that is not approval itself (approval is the
`invoice.approve` permission, which `ap_clerk` does not hold).

An ENTRY-ONLY caller — `ap_clerk` and none of the manage roles — is further
held to the pre-approval window: once an invoice has been approved (or has
moved past approval), their entry endpoints refuse with
`INVOICE_ENTRY_WINDOW_CLOSED` instead of letting them touch content someone
else signed off. A caller who also holds a manage role keeps that role's reach.
"""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, ROLE_AP_CLERK, ROLE_AP_MANAGER, ROLE_CFO
from app.api.refusals import coded_refusal
from app.models.invoice import Invoice, InvoiceStatus
from app.models.user import User

INVOICE_MANAGE_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)
INVOICE_ENTRY_ROLES = (*INVOICE_MANAGE_ROLES, ROLE_AP_CLERK)

#: An entry-only caller (a clerk) acted on an invoice outside the pre-approval
#: window, or asked for a transition that is not an entry transition.
INVOICE_ENTRY_WINDOW_CLOSED = "invoice_entry_window_closed"

#: The statuses an invoice can sit in BEFORE anyone signed it off. An explicit
#: allowlist, not "everything but the locked set", so a status added later is
#: outside the clerk's window until someone decides otherwise. `failed` is in
#: it only for an invoice never approved — an approved invoice whose ERP push
#: failed lands at `failed` too, which is why `in_entry_window` also reads
#: `approved_by` (set at final approval and never cleared).
_ENTRY_WINDOW_STATUSES = frozenset(
    {
        InvoiceStatus.new,
        InvoiceStatus.pending,
        InvoiceStatus.failed,
        InvoiceStatus.ready_for_review,
        InvoiceStatus.rejected,
    }
)

#: The `POST /api/invoices/bulk/status` targets an entry-only caller may drive:
#: submit for review (`new → ready_for_review`), resubmit
#: (`rejected → ready_for_review`) and send back to draft (`rejected → new`).
#: Never `approved` / `rejected` (review), `done` (closes an invoice) or
#: `pending` (extraction is `POST /{id}/extract`).
ENTRY_BULK_STATUS_TARGETS = frozenset({InvoiceStatus.new, InvoiceStatus.ready_for_review})

#: The source statuses an entry-only caller's bulk change may move an invoice
#: FROM. `pending` is mid-extraction (the extractor is writing it); every other
#: status is past entry.
ENTRY_BULK_STATUS_SOURCES = frozenset({InvoiceStatus.new, InvoiceStatus.rejected})


def is_entry_only(user: User) -> bool:
    """True for a caller whose invoice reach is entry and nothing more."""
    held = {r.name for r in user.roles}
    return not (held & set(INVOICE_MANAGE_ROLES))


def in_entry_window(invoice: Invoice) -> bool:
    """True while the invoice has not been approved by anyone yet."""
    return invoice.status in _ENTRY_WINDOW_STATUSES and not invoice.approved_by


def _window_closed(invoice: Invoice, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=coded_refusal(INVOICE_ENTRY_WINDOW_CLOSED, message, status=invoice.status.value),
    )


def refuse_entry_only_outside_window(user: User, invoice: Invoice) -> None:
    """403 an entry-only caller acting on an invoice past the entry window.

    A no-op for a caller holding a manage role: their endpoint's own status
    guards apply unchanged.
    """
    if is_entry_only(user) and not in_entry_window(invoice):
        raise _window_closed(
            invoice, "AP clerks can enter and edit an invoice only until it is approved."
        )


async def refuse_entry_only_mid_chain(db: AsyncSession, user: User, invoice: Invoice) -> None:
    """403 an entry-only caller editing an invoice a chain level already signed.

    `approved_by` is set only at FINAL approval, so a multi-level chain that
    has collected a level-1 sign-off still reads as inside the entry window —
    and the edit does not clear that sign-off, so it would carry over to
    content its approver never saw. A clerk's correction at that point goes
    through reject → rework, which clears the chain (`review.reject_invoice`).
    """
    if not is_entry_only(user) or invoice.status != InvoiceStatus.ready_for_review:
        return
    from app.services.approval_chain import chain_state_of
    from app.services.workflow_engine import get_workflow_instance

    instance = await get_workflow_instance(db, invoice.id)
    chain = chain_state_of(instance.state_data if instance else None)
    levels = chain.get("levels") if isinstance(chain, dict) else None
    if isinstance(levels, list) and any(
        isinstance(lv, dict) and lv.get("approvals") for lv in levels
    ):
        raise _window_closed(
            invoice,
            "An approver has already signed part of this invoice's approval chain; "
            "it must be rejected for rework before an AP clerk can change it.",
        )
