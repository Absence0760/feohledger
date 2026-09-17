"""The one walk from a data subject to the objects held about them in storage.

Both halves of the privacy surface need the same traversal and reach opposite
conclusions with it: the DSAR export ENUMERATES what is held (Art. 15), the
erasure path DELETES what it is entitled to (Art. 17). Writing that walk twice
would guarantee they drift, and the drift would be invisible — an export that
lists a document the erasure leg cannot reach is exactly the failure this module
exists to prevent. So the walk lives here once and each caller reads the same
:class:`SubjectDocument` list through a different lens.

WHAT GETS DELETED AND WHAT DOES NOT — this is the whole design decision.

Erasure in this product deliberately preserves the money trail: amounts,
statuses, dates and the append-only ``audit_log`` survive, because tax and SOX
record-keeping outranks erasure for transactional rows (see
``services/privacy_erasure``). Stored documents split along the SAME line, not
along "is it about the subject":

* ``DELETE`` — the document's sole subject is the erased party and it is not
  evidence of a transaction. A W-9/W-8 is the supplier's own signed tax form
  carrying their TIN; a supplier-authored chat attachment is a document the
  supplier chose to send. Neither is the record of a payable. They go.
* ``RETAIN`` — the document is the evidence behind a booked payable, and the row
  it supports is itself retained. An invoice PDF is the invoice; a contract
  document is the commitment that authorised the spend; an expense receipt is
  the proof behind a reimbursement; an archived vendor statement is the
  counterparty document a reconciliation was read from. Deleting any of them
  would leave a retained money row with its supporting evidence destroyed, which
  is the thing the retention argument exists to stop.

The Positive Pay file is the one case that resolves against its shape rather
than for it, and is worth stating because it looks like transaction evidence.
It is DELETED. It is an *instruction* to a bank, not a record of what happened:
the ``positive_pay_files`` row keeps the audit-grade metadata (item count, total,
``content_hash``, ``account_last4``) and ``payments`` keeps the money, so nothing
evidential is lost — while the rendered file is the single artefact in the system
holding every payee's full account and routing number in the clear. A file whose
operational life is days and whose contents are the BEC-fraud target does not
earn indefinite retention against an erasure request. It is multi-subject (one
file covers a whole run), and deleting it therefore removes other payees'
coordinates too; that is a reduction in standing exposure for them, not a loss.

AP-authored chat attachments are retained: what an AP employee attaches to a
supplier thread is company correspondence about an invoice, not the employee's
own personal data. The personal data in that row is the authorship, which
``privacy_erasure`` already redacts.

Nothing here touches the event loop directly: object deletion goes through
``services.storage._delete_object``, which offloads the boto3 round trip to a
worker thread (project invariant — see ``tests/test_storage_nonblocking.py``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contract import Contract
from app.models.data_subject_request import (
    SUBJECT_USER,
    SUBJECT_VENDOR_CONTACT,
    SUBJECT_VENDOR_USER,
)
from app.models.expense import Expense, ExpenseReport
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.positive_pay import PositivePayFile
from app.models.supplier_chat import ChatAuthorRole, SupplierChatMessage, SupplierChatThread
from app.models.vendor import Vendor
from app.models.vendor_statement_recon import VendorStatementReconciliation
from app.services import storage

logger = logging.getLogger(__name__)

#: The document's sole subject is the erased party; erasure removes the object.
DELETE = "delete"
#: The document is transaction evidence the money trail keeps; erasure leaves it.
RETAIN = "retain"

# Document kinds, used as the manifest's grouping key and in the retain reason.
KIND_TAX_FORM = "tax_form"
KIND_CHAT_ATTACHMENT = "chat_attachment"
KIND_POSITIVE_PAY = "positive_pay_file"
KIND_INVOICE = "invoice_document"
KIND_CONTRACT = "contract_document"
KIND_EXPENSE_RECEIPT = "expense_receipt"
KIND_VENDOR_STATEMENT = "vendor_statement"

_RETAIN_REASON_EVIDENCE = (
    "transaction evidence — retained on the same legal basis as the invoice / "
    "payment rows it supports"
)
_RETAIN_REASON_COMPANY_RECORD = (
    "company correspondence authored by an AP user, not the subject's own document"
)


@dataclass(frozen=True)
class SubjectDocument:
    """One stored object reachable from a data subject.

    ``file_key`` is the storage key; ``disposition`` is :data:`DELETE` or
    :data:`RETAIN` and is decided HERE, once, for both callers.
    ``kind`` + ``record_id`` are what the erasure leg uses to null the owning
    row's pointer after the object is gone, so an erased subject is never left
    with a row referencing a key that no longer exists.
    """

    file_key: str
    kind: str
    disposition: str
    record_type: str
    record_id: str | None = None
    filename: str | None = None
    content_type: str | None = None
    reason: str = ""

    def manifest_entry(self) -> dict:
        """The export's view: what is held, and whether erasure would remove it."""
        entry = {
            "kind": self.kind,
            "file_key": self.file_key,
            "record_type": self.record_type,
            "record_id": self.record_id,
            "erasure_disposition": self.disposition,
        }
        if self.filename:
            entry["filename"] = self.filename
        if self.content_type:
            entry["content_type"] = self.content_type
        if self.reason:
            entry["retention_reason"] = self.reason
        return entry


@dataclass
class DocumentErasureResult:
    """Non-PII outcome of the storage leg of an erasure."""

    deleted: int = 0
    failed: int = 0
    retained: int = 0
    #: Keys whose delete raised. Kept so the caller can decide not to null the
    #: pointer — the next run retries. Never logged (a key embeds ids).
    failed_keys: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-subject collection
# ---------------------------------------------------------------------------


async def _vendor_invoice_ids(
    tenant_db: AsyncSession, *, vendor_id: uuid.UUID, organization_id: uuid.UUID
) -> list[uuid.UUID]:
    return list(
        (
            await tenant_db.execute(
                select(Invoice.id).where(
                    Invoice.vendor_id == vendor_id,
                    Invoice.organization_id == organization_id,
                )
            )
        )
        .scalars()
        .all()
    )


def _chat_attachment_documents(
    message: SupplierChatMessage,
) -> list[SubjectDocument]:
    """Attachments on one chat message, dispositioned by who wrote it."""
    out: list[SubjectDocument] = []
    supplier_authored = message.author_role == ChatAuthorRole.supplier
    for att in message.attachments or []:
        if not isinstance(att, dict):
            continue
        key = att.get("file_key")
        if not key:
            continue
        out.append(
            SubjectDocument(
                file_key=key,
                kind=KIND_CHAT_ATTACHMENT,
                disposition=DELETE if supplier_authored else RETAIN,
                record_type="supplier_chat_message",
                record_id=str(message.id),
                filename=att.get("filename"),
                content_type=att.get("content_type"),
                reason="" if supplier_authored else _RETAIN_REASON_COMPANY_RECORD,
            )
        )
    return out


async def _collect_vendor_contact(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
) -> list[SubjectDocument]:
    docs: list[SubjectDocument] = []

    vendor = (
        await tenant_db.execute(
            select(Vendor).where(Vendor.id == subject_id, Vendor.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if vendor is None:
        return docs

    # The supplier's own signed W-9 / W-8 — their TIN, nobody else's, and not
    # the record of a payable.
    if vendor.w9_file_key:
        docs.append(
            SubjectDocument(
                file_key=vendor.w9_file_key,
                kind=KIND_TAX_FORM,
                disposition=DELETE,
                record_type="vendor",
                record_id=str(vendor.id),
            )
        )

    invoice_ids = await _vendor_invoice_ids(
        tenant_db, vendor_id=subject_id, organization_id=organization_id
    )

    if invoice_ids:
        invoice_rows = (
            await tenant_db.execute(
                select(Invoice.id, Invoice.file_key).where(
                    Invoice.id.in_(invoice_ids), Invoice.file_key.is_not(None)
                )
            )
        ).all()
        for row in invoice_rows:
            docs.append(
                SubjectDocument(
                    file_key=row.file_key,
                    kind=KIND_INVOICE,
                    disposition=RETAIN,
                    record_type="invoice",
                    record_id=str(row.id),
                    reason=_RETAIN_REASON_EVIDENCE,
                )
            )

        messages = (
            (
                await tenant_db.execute(
                    select(SupplierChatMessage)
                    .join(
                        SupplierChatThread,
                        SupplierChatMessage.thread_id == SupplierChatThread.id,
                    )
                    .where(SupplierChatThread.invoice_id.in_(invoice_ids))
                )
            )
            .scalars()
            .all()
        )
        for msg in messages:
            docs.extend(_chat_attachment_documents(msg))

        # Positive Pay files for any run that paid one of this vendor's
        # invoices. Reached through `payments.payment_run_id` because the file
        # is keyed to the RUN, not to a vendor.
        run_ids = list(
            (
                await tenant_db.execute(
                    select(Payment.payment_run_id)
                    .where(
                        Payment.invoice_id.in_(invoice_ids),
                        Payment.payment_run_id.is_not(None),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        if run_ids:
            pp_rows = (
                await tenant_db.execute(
                    select(PositivePayFile.id, PositivePayFile.file_key).where(
                        PositivePayFile.organization_id == organization_id,
                        PositivePayFile.payment_run_id.in_(run_ids),
                        PositivePayFile.file_key.is_not(None),
                    )
                )
            ).all()
            for row in pp_rows:
                docs.append(
                    SubjectDocument(
                        file_key=row.file_key,
                        kind=KIND_POSITIVE_PAY,
                        disposition=DELETE,
                        record_type="positive_pay_file",
                        record_id=str(row.id),
                    )
                )

    contract_rows = (
        await tenant_db.execute(
            select(Contract.id, Contract.file_key).where(
                Contract.vendor_id == subject_id,
                Contract.organization_id == organization_id,
                Contract.file_key.is_not(None),
            )
        )
    ).all()
    for row in contract_rows:
        docs.append(
            SubjectDocument(
                file_key=row.file_key,
                kind=KIND_CONTRACT,
                disposition=RETAIN,
                record_type="contract",
                record_id=str(row.id),
                reason=_RETAIN_REASON_EVIDENCE,
            )
        )

    statement_rows = (
        await tenant_db.execute(
            select(
                VendorStatementReconciliation.id,
                VendorStatementReconciliation.file_key,
            ).where(
                VendorStatementReconciliation.vendor_id == subject_id,
                VendorStatementReconciliation.organization_id == organization_id,
                VendorStatementReconciliation.file_key.is_not(None),
            )
        )
    ).all()
    for row in statement_rows:
        docs.append(
            SubjectDocument(
                file_key=row.file_key,
                kind=KIND_VENDOR_STATEMENT,
                disposition=RETAIN,
                record_type="vendor_statement_reconciliation",
                record_id=str(row.id),
                reason=_RETAIN_REASON_EVIDENCE,
            )
        )

    return docs


async def _collect_vendor_user(
    *,
    subject_id: uuid.UUID,
    tenant_db: AsyncSession,
) -> list[SubjectDocument]:
    """A portal login's own documents.

    Deliberately NOT the parent vendor's W-9: the vendor company is a separate
    data subject with its own erasure request, and one of its logins asking to
    be forgotten does not erase the company's tax form.
    """
    messages = (
        (
            await tenant_db.execute(
                select(SupplierChatMessage).where(
                    SupplierChatMessage.author_user_id == subject_id,
                    SupplierChatMessage.author_role == ChatAuthorRole.supplier,
                )
            )
        )
        .scalars()
        .all()
    )
    docs: list[SubjectDocument] = []
    for msg in messages:
        docs.extend(_chat_attachment_documents(msg))
    return docs


async def _collect_user(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
) -> list[SubjectDocument]:
    """An AP-team member's documents — all of them transaction evidence.

    An expense receipt is the proof behind a reimbursement that was paid, so it
    is retained for the same reason the reimbursement row is. An attachment the
    user posted to a supplier thread is company correspondence. The user leg
    therefore deletes no objects; what it does delete is the passkey material
    and the live sessions, and that is handled in ``privacy_erasure``.
    """
    docs: list[SubjectDocument] = []

    receipt_rows = (
        await tenant_db.execute(
            select(Expense.id, Expense.receipt_file_key)
            .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
            .where(
                ExpenseReport.employee_user_id == subject_id,
                ExpenseReport.organization_id == organization_id,
                Expense.receipt_file_key.is_not(None),
            )
        )
    ).all()
    for row in receipt_rows:
        docs.append(
            SubjectDocument(
                file_key=row.receipt_file_key,
                kind=KIND_EXPENSE_RECEIPT,
                disposition=RETAIN,
                record_type="expense",
                record_id=str(row.id),
                reason=_RETAIN_REASON_EVIDENCE,
            )
        )

    messages = (
        (
            await tenant_db.execute(
                select(SupplierChatMessage).where(
                    SupplierChatMessage.author_user_id == subject_id,
                    SupplierChatMessage.author_role == ChatAuthorRole.ap_team,
                )
            )
        )
        .scalars()
        .all()
    )
    for msg in messages:
        docs.extend(_chat_attachment_documents(msg))

    return docs


async def collect_subject_documents(
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
) -> list[SubjectDocument]:
    """Every stored object reachable from this subject, dispositioned.

    Tenant-scoped by construction: ``tenant_db`` is the session the request's
    ``get_tenant_db`` chokepoint resolved, and every query additionally filters
    on ``organization_id`` where the table carries it. Returns an empty list for
    an unknown subject type rather than raising — the caller has already
    validated it, and a traversal is not the place to re-litigate that.
    """
    if subject_type == SUBJECT_VENDOR_CONTACT:
        return await _collect_vendor_contact(
            subject_id=subject_id,
            organization_id=organization_id,
            tenant_db=tenant_db,
        )
    if subject_type == SUBJECT_VENDOR_USER:
        return await _collect_vendor_user(subject_id=subject_id, tenant_db=tenant_db)
    if subject_type == SUBJECT_USER:
        return await _collect_user(
            subject_id=subject_id,
            organization_id=organization_id,
            tenant_db=tenant_db,
        )
    return []


# ---------------------------------------------------------------------------
# The two lenses
# ---------------------------------------------------------------------------


def documents_manifest(docs: list[SubjectDocument]) -> dict:
    """The export's view of the traversal.

    References, not bytes. Inlining the objects would base64 up to 25 MB per
    document into a JSON response — and would put a W-9's taxpayer
    identification number into the same response body that a routine export
    returns. The manifest names every object held, its kind, the record it hangs
    off and whether an erasure would remove it; each remains retrievable through
    the surface that already gates it by tenant and owner.
    """
    return {
        "total": len(docs),
        "deleted_on_erasure": sum(1 for d in docs if d.disposition == DELETE),
        "retained_on_erasure": sum(1 for d in docs if d.disposition == RETAIN),
        "documents": [d.manifest_entry() for d in docs],
        "_note": (
            "References only — object bytes are retrieved through the "
            "per-document download endpoints, which enforce tenant + owner "
            "scoping. `erasure_disposition` says what an Art. 17 request would "
            "do with each; see backend/docs/privacy.md."
        ),
    }


async def delete_subject_documents(docs: list[SubjectDocument]) -> DocumentErasureResult:
    """Delete every :data:`DELETE`-dispositioned object. Retained ones are counted.

    Each key is deleted at most once even when two rows reference it. A failure
    is recorded rather than raised: the caller nulls the DB pointer ONLY for
    keys that actually went, so a re-run retries the rest and the erasure stays
    idempotent. Nothing about the key is logged — a storage key embeds the org
    and record ids.
    """
    result = DocumentErasureResult()
    seen: set[str] = set()
    for doc in docs:
        if doc.disposition != DELETE:
            result.retained += 1
            continue
        if doc.file_key in seen:
            continue
        seen.add(doc.file_key)
        try:
            await storage._delete_object(doc.file_key)
            result.deleted += 1
        except Exception as exc:  # noqa: BLE001 — one object must not abort the erasure
            result.failed += 1
            result.failed_keys.append(doc.file_key)
            logger.warning(
                "[privacy] erasure could not delete a %s object: %s",
                doc.kind,
                exc.__class__.__name__,
            )
    return result
