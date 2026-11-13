"""Who may ENTER an invoice, and how far an entry-only caller's reach extends.

Entry — create, upload, attach / replace / remove the source file, field and
line-item edits, extract / reset extraction, submit for review, resubmit after
a rejection, CSV import of open AP, and the entry-only bulk status targets — is
open to `INVOICE_ENTRY_ROLES`, `ap_clerk` included. Segregation of duties sits
at approval: `approval_chain.violates_segregation` refuses the uploader ∪
`segregation_actor_ids`, every create path stamps `uploaded_by_id`, and
:func:`stamp_entry_editor` adds an entry-only caller who edits someone else's
invoice to that set.

Entry is a role list, not a catalog permission (`app/api/permissions.py`). The
catalog holds the fraud-sensitive duties, and both role-grant guards in
`api/admin.py` read catalog membership as "sensitive": were entry in it, the
baseline `ap_clerk` role would carry a catalog permission and a user-manager
without it could no longer onboard or manage clerks. Entry's SoD control is
per-invoice, at approval, and does not need a role split.

An ENTRY-ONLY caller holds `ap_clerk` and none of `INVOICE_MANAGE_ROLES`. They
are held to the entry window (:func:`in_entry_window`) and the entry
transitions, and nothing they do can end in an approval without a second
person: their `/complete` skips the amount floor and their extraction runs with
`suppress_auto_approve`. Nor can anyone else's extraction approve a document
they chose: `services/extraction.auto_approve_suppression` refuses the
unattended approval whenever the uploader is not the reader or the segregation
set is non-empty.
"""

from fastapi import HTTPException, status

from app.api.deps import ROLE_ADMIN, ROLE_AP_CLERK, ROLE_AP_MANAGER, ROLE_CFO, holds_any_role
from app.api.refusals import coded_refusal, role_not_permitted
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_status import financially_locked, was_ever_approved
from app.models.user import User

#: The roles whose invoice reach goes past entry (delete, ERP, the `approved`
#: metadata window).
INVOICE_MANAGE_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)
INVOICE_ENTRY_ROLES = (*INVOICE_MANAGE_ROLES, ROLE_AP_CLERK)

#: `POST /api/invoices/import-csv`. Not `cfo`: CSV import was admin / AP
#: manager before clerks could enter, and opening it to clerks is the change.
INVOICE_IMPORT_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)

#: Who may CSV-import a historical `done` / `paid` row. Such a row asserts a
#: payment already happened and never meets approval, so it is not entry: it
#: stays with the roles that held CSV import before clerks could enter.
HISTORICAL_IMPORT_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER)

INVOICE_ENTRY_WINDOW_CLOSED = "invoice_entry_window_closed"

#: The source-document routes (`POST` / `PUT` / `DELETE /invoices/{id}/file`)
#: on an approved invoice. ``params.status``. See :func:`document_locked`.
INVOICE_DOCUMENT_LOCKED = "invoice_document_locked"

#: `PATCH /invoices/{id}` carrying a financial field, or
#: `PUT /invoices/{id}/line-items`, on a financially locked invoice
#: (`models/invoice_status.financially_locked`). ``params.status``.
INVOICE_FINANCIALLY_LOCKED = "invoice_financially_locked"

# An explicit allowlist, so a status added later is outside the window until
# someone decides otherwise. `ready_for_review` is NOT in it: once submitted,
# the content is what the approver is looking at, and a correction after
# submit goes through reject → rework. (A manager may still edit at this stage;
# approval refuses any version the approver did not load, so such an edit is
# re-reviewed rather than approved unseen — `api/invoice_version.py`.)
# `failed` is in it only for an invoice never approved (see `in_entry_window`).
_ENTRY_WINDOW_STATUSES = frozenset(
    {
        InvoiceStatus.new,
        InvoiceStatus.pending,
        InvoiceStatus.failed,
        InvoiceStatus.rejected,
    }
)

#: Bulk-status targets an entry-only caller may set: submit / resubmit
#: (`→ ready_for_review`) and send a rejected invoice back to draft (`→ new`).
ENTRY_BULK_STATUS_TARGETS = frozenset({InvoiceStatus.new, InvoiceStatus.ready_for_review})

#: Statuses an entry-only caller's bulk change may move an invoice FROM.
#: `pending` is mid-extraction; everything else is past entry.
ENTRY_BULK_STATUS_SOURCES = frozenset({InvoiceStatus.new, InvoiceStatus.rejected})


def is_entry_only(user: User) -> bool:
    """True for a caller whose invoice reach is entry and nothing more."""
    held = {r.name for r in user.roles}
    return not (held & set(INVOICE_MANAGE_ROLES))


def may_import_history(user: User) -> bool:
    held = {r.name for r in user.roles}
    return bool(held & set(HISTORICAL_IMPORT_ROLES))


def in_entry_window(invoice: Invoice) -> bool:
    """True while an entry-only caller may still change the invoice.

    An approved invoice whose ERP push failed sits at `failed` too, and one
    approved then rejected sits at `rejected` — hence the approval read.
    """
    return invoice.status in _ENTRY_WINDOW_STATUSES and not was_ever_approved(invoice)


def missing_required_fields(invoice: Invoice) -> list[str]:
    """The fields an invoice needs before it may leave entry, by wire name.

    `POST /invoices/{id}/complete` refuses on a non-empty list, and the bulk
    `new → ready_for_review` submit reports each such invoice as a skip — one
    definition, so the bulk bar cannot put a vendorless or amountless invoice
    in the approval queue that the single-invoice submit would have refused.
    """
    missing: list[str] = []
    if not invoice.vendor_name or not invoice.vendor_name.strip():
        missing.append("vendor")
    if not invoice.invoice_number or not invoice.invoice_number.strip():
        missing.append("invoice_number")
    if invoice.amount is None or invoice.amount <= 0:
        missing.append("amount")
    return missing


def entry_window_refusal(user: User, invoice: Invoice) -> dict | None:
    """The coded refusal for an entry-only caller past the entry window, or
    None — the verdict form of :func:`refuse_entry_only_outside_window`."""
    if is_entry_only(user) and not in_entry_window(invoice):
        return coded_refusal(
            INVOICE_ENTRY_WINDOW_CLOSED,
            "AP clerks can enter and edit an invoice only until it is submitted for "
            "review; after that it needs an AP manager, or a rejection for rework.",
            status=invoice.status.value,
        )
    return None


def refuse_entry_only_outside_window(user: User, invoice: Invoice) -> None:
    """403 an entry-only caller acting on an invoice past the entry window.

    A no-op for a caller holding a manage role: their endpoint's own status
    guards apply unchanged.
    """
    refused = entry_window_refusal(user, invoice)
    if refused is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refused)


def document_locked(invoice: Invoice) -> bool:
    """Whether the invoice's source document is frozen (decisions §363).

    The document is what the approver signed off and what the payment is made
    against, so it freezes exactly when the amount and payee do: the shared
    `models/invoice_status.financially_locked` (from `approved` on, and at
    `failed` after an approval, which `/retry-erp` pushes again with no new
    approval). It is neither replaced, removed nor added to then.
    """
    return financially_locked(invoice)


def document_lock_refusal(invoice: Invoice) -> dict | None:
    """The coded 409 the file routes answer on a frozen document, or None."""
    if not document_locked(invoice):
        return None
    return coded_refusal(
        INVOICE_DOCUMENT_LOCKED,
        "The source document cannot be changed once the invoice is approved.",
        status=invoice.status.value,
    )


def financial_lock_refusal(invoice: Invoice) -> dict | None:
    """The coded 409 `PATCH` (on a financial field) and `PUT /line-items`
    answer on a financially locked invoice, or None (decisions §370)."""
    if not financially_locked(invoice):
        return None
    return coded_refusal(
        INVOICE_FINANCIALLY_LOCKED,
        "The amount, payee and line items cannot be changed once the invoice is "
        "approved: they are what the approver signed off and what will be paid.",
        status=invoice.status.value,
    )


def edit_financials_refusal(user: User, invoice: Invoice) -> dict | None:
    """What `PATCH` with a financial field, and `PUT /line-items`, would answer
    ``user`` for ``invoice`` right now: the entry roles, the entry window, then
    the financial lock — the doors' own order. ``GET /review-actions`` serves
    it as ``edit_financials``, so the modal decides which fields it may edit
    and send from the server's answer, not a status list of its own. (`PATCH`
    refuses an immutable status before the lock; every immutable status is
    financially locked, so the verdict refuses there too.)"""
    if not holds_any_role(user, INVOICE_ENTRY_ROLES):
        return role_not_permitted()
    return entry_window_refusal(user, invoice) or financial_lock_refusal(invoice)


def manage_file_refusal(user: User, invoice: Invoice) -> dict | None:
    """What the file routes would answer ``user`` for ``invoice`` right now:
    the route's role list, the entry window, then the document lock — the
    doors' own order. ``GET /review-actions`` serves it as ``manage_file``, so
    the modal offers Upload / Replace / Delete from it rather than a status
    list of its own. Whether there is a file to replace is the modal's choice
    of button, not a rule."""
    if not holds_any_role(user, INVOICE_ENTRY_ROLES):
        return role_not_permitted()
    return entry_window_refusal(user, invoice) or document_lock_refusal(invoice)


def stamp_entry_editor(user: User, invoice: Invoice) -> None:
    """Add an entry-only caller who changed this invoice's content to its
    segregation set (`Invoice.segregation_actor_ids`).

    A clerk cannot approve today, but a clerk granted `invoice.approve` through
    a custom role, or later promoted, could — and on an invoice nobody uploaded
    (email intake, PEPPOL) or someone else did, nothing else names them. The
    preparer of a payable's figures never approves them; that is the rule the
    uploader stamp already applies to whoever created the row.

    Manage-role editors are deliberately NOT stamped: approve-with-corrections
    (`review.approve_invoice`) is the approver editing what they sign, and
    stamping a manager's pre-review fix would refuse that same manager the
    approval in a one-approver org.
    """
    if not is_entry_only(user) or invoice.uploaded_by_id == user.id:
        return
    current = [str(x) for x in (invoice.segregation_actor_ids or [])]
    if str(user.id) in current:
        return
    # A new list, not an in-place append: the JSONB column does not track
    # mutation, so an append would never be flushed.
    invoice.segregation_actor_ids = [*current, str(user.id)]
