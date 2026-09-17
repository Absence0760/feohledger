"""Right-to-erasure / anonymization (GDPR Art. 17 / CCPA right-to-delete).

Irreversibly redact a data subject's PII **while preserving the immutable
financial + audit record**. Legally-required retention (tax, SOX, AP records)
wins over erasure for transactional rows: we redact the PII *text* fields and
keep the money trail (amounts, statuses, dates) and the append-only
``audit_log`` completely intact.

What is redacted vs. preserved, per subject type:

  * ``user`` (control plane) — redact ``email`` / ``full_name`` / ``sso_*``,
    null the MFA secret + password, deactivate, **delete every
    ``WebAuthnCredential`` row** and **revoke every live session**. **Preserve**
    the row id, ``organization_id``, role assignments, and every ``audit_log``
    row the user authored (the actor_id link stays — non-repudiation).
  * ``vendor_user`` (tenant) — redact ``email`` / ``full_name``, null
    ``hashed_password`` + MFA secret, deactivate.
  * ``vendor_contact`` (tenant Vendor) — redact contact PII (``email`` /
    ``phone`` / ``address`` / ``tax_id`` / ``bank_details`` /
    ``beneficial_owner_data``). **Preserve** ``vendor.name`` (the legal payee on
    every invoice's ``vendor_name`` money field) and every related Invoice /
    Payment amount + status. Supplier-chat message *bodies* the supplier wrote
    are also redacted (free-text PII) but the thread + audit timeline stay.

Three things the DB-row redaction above does not by itself reach, and how each
is handled:

  * **Stored documents.** Objects in storage are walked by
    ``services/privacy_documents``, which decides per document whether erasure
    deletes it (sole subject, not transaction evidence — a W-9/W-8, a
    supplier-authored chat attachment, a Positive Pay file) or the money trail
    retains it (an invoice PDF, a contract document, an expense receipt, an
    archived vendor statement). That module's docstring carries the reasoning;
    ``backend/docs/privacy.md`` carries the published policy.
  * **Passkey material.** ``WebAuthnCredential`` rows are DELETED, not redacted.
    A credential row is authenticator material, not a financial record — there
    is nothing in it the money trail needs and every byte of it is a handle to
    the erased person's device.
  * **Live sessions.** Access already stops on the next request (``is_active``
    is re-read by ``get_current_user``), but the issued JTI stays valid in Redis
    until its TTL. Erasure calls the EXISTING revocation path,
    ``services/session_management.revoke_user_sessions`` — the same one admin
    deactivation and password reset use — rather than growing a second.

**Object deletion happens BEFORE the caller commits, deliberately.** The DB row
is the only thing that knows a document's storage key, so committing the null
first and then failing the delete would orphan the object beyond any future
reach — the same ordering argument ``services/tenant_deletion`` makes for the
whole-tenant case. A pointer is nulled only for a key that actually went, so a
partial failure is simply re-run.

Hard guarantees (project invariants):
  * **No money field is ever touched** — only PII text columns are nulled /
    tombstoned. Amounts, statuses, currencies, dates are untouched.
  * **``audit_log`` is append-only** — erasure NEVER updates or deletes an audit
    row; it writes a NEW one (done by the caller via ``dispatch_audit``).
  * **Idempotent** — re-running erasure on an already-erased subject is a safe
    no-op (detected via the ``erased_at`` tombstone marker).

Pure-ish: these mutate the passed ORM objects / session but never commit — the
API layer owns the transaction, the audit write, and the request-row insert.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.data_subject_request import (
    SUBJECT_USER,
    SUBJECT_VENDOR_CONTACT,
    SUBJECT_VENDOR_USER,
)
from app.models.positive_pay import PositivePayFile
from app.models.supplier_chat import ChatAuthorRole, SupplierChatMessage, SupplierChatThread
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.models.webauthn_credential import WebAuthnCredential
from app.services.privacy_documents import (
    DELETE,
    KIND_CHAT_ATTACHMENT,
    KIND_POSITIVE_PAY,
    KIND_TAX_FORM,
    collect_subject_documents,
    delete_subject_documents,
)
from app.services.session_management import revoke_user_sessions

# Redaction tombstones. Distinct, recognisable, and PII-free. The id suffix
# keeps formerly-unique columns unique after redaction (email has a UNIQUE
# constraint) and lets an operator correlate the row to its erasure request
# without revealing the original value.
REDACTED = "[redacted]"

logger = logging.getLogger(__name__)


def _redacted_email(subject_id: uuid.UUID) -> str:
    # Stays unique (email is UNIQUE on both User + VendorUser) and obviously
    # non-deliverable so it can never be used to re-contact the subject.
    return f"erased+{subject_id}@redacted.invalid"


class ErasureResult:
    """The non-PII outcome of an erasure run."""

    def __init__(self) -> None:
        self.already_erased: bool = False
        self.fields_redacted: int = 0
        self.record_counts: dict[str, int] = {}
        # Storage leg. `documents_failed` is surfaced rather than swallowed:
        # an erasure that could not reach an object is not complete, and the
        # operator needs to know to re-run rather than to read "completed".
        self.documents_deleted: int = 0
        self.documents_retained: int = 0
        self.documents_failed: int = 0
        # Auth material (the `user` leg only).
        self.passkeys_deleted: int = 0
        self.sessions_revoked: int = 0


async def _erase_documents(
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
    result: ErasureResult,
    now: datetime,
) -> int:
    """Run the storage leg for one subject. Returns how many objects went.

    Deletes every :data:`DELETE`-dispositioned object the shared traversal finds,
    then nulls the DB pointer for each key that actually went — and only for
    those, so a failed delete stays discoverable and the next run retries it.

    This runs on EVERY erasure, including one whose DB fields are already
    tombstoned. That is what makes the fix retroactive: a subject erased before
    this leg existed still has their documents reachable, and asking again now
    removes them rather than returning ``noop`` over the gap.
    """
    docs = await collect_subject_documents(
        subject_type=subject_type,
        subject_id=subject_id,
        organization_id=organization_id,
        tenant_db=tenant_db,
    )
    if not docs:
        return 0

    outcome = await delete_subject_documents(docs)
    result.documents_deleted += outcome.deleted
    result.documents_retained += outcome.retained
    result.documents_failed += outcome.failed

    gone = {
        d.file_key
        for d in docs
        if d.disposition == DELETE and d.file_key not in set(outcome.failed_keys)
    }
    if not gone:
        return 0

    # --- null the pointers, per record kind --------------------------------
    vendor_ids = {d.record_id for d in docs if d.kind == KIND_TAX_FORM and d.file_key in gone}
    for vid in vendor_ids:
        vendor = (
            await tenant_db.execute(select(Vendor).where(Vendor.id == uuid.UUID(vid)))
        ).scalar_one_or_none()
        if vendor is not None:
            vendor.w9_file_key = None

    pp_ids = {d.record_id for d in docs if d.kind == KIND_POSITIVE_PAY and d.file_key in gone}
    for pid in pp_ids:
        row = (
            await tenant_db.execute(
                select(PositivePayFile).where(PositivePayFile.id == uuid.UUID(pid))
            )
        ).scalar_one_or_none()
        if row is not None:
            row.file_key = None
            meta = dict(row.meta or {})
            # PII-free marker: the file is gone and WHY, so an auditor reading
            # the row later is not left inferring a lost upload.
            meta["file_erased_at"] = now.isoformat()
            meta["file_erased_reason"] = "data_subject_erasure"
            row.meta = meta

    msg_ids = {d.record_id for d in docs if d.kind == KIND_CHAT_ATTACHMENT and d.file_key in gone}
    for mid in msg_ids:
        msg = (
            await tenant_db.execute(
                select(SupplierChatMessage).where(SupplierChatMessage.id == uuid.UUID(mid))
            )
        ).scalar_one_or_none()
        if msg is None:
            continue
        remaining = [
            a
            for a in (msg.attachments or [])
            if not (isinstance(a, dict) and a.get("file_key") in gone)
        ]
        msg.attachments = remaining or None
        flag_modified(msg, "attachments")

    return len(gone)


async def _erase_auth_material(
    *,
    subject_id: uuid.UUID,
    control_db: AsyncSession,
    result: ErasureResult,
) -> int:
    """Delete the subject's passkeys and revoke their live sessions.

    Like the storage leg, this runs unconditionally so it reaches a subject
    erased before it existed. Session revocation goes through the existing
    ``revoke_user_sessions`` — the same path admin deactivation and password
    reset use — and is best-effort: Redis being unreachable must not fail an
    erasure whose DB half is the regulated part. The account is already
    deactivated, so the sessions 401 on their next request regardless.
    """
    deleted = (
        await control_db.execute(
            sa_delete(WebAuthnCredential).where(WebAuthnCredential.user_id == subject_id)
        )
    ).rowcount or 0
    result.passkeys_deleted += int(deleted)

    try:
        revoked = await revoke_user_sessions(subject_id)
        result.sessions_revoked += len(revoked)
    except Exception as exc:  # noqa: BLE001 — Redis must not fail the erasure
        logger.warning(
            "[privacy] erasure could not revoke sessions for a subject: %s",
            exc.__class__.__name__,
        )
    return int(deleted)


async def erase_user(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    control_db: AsyncSession,
    tenant_db: AsyncSession | None = None,
    now: datetime | None = None,
) -> ErasureResult:
    now = now or datetime.now(UTC)
    result = ErasureResult()
    user = (
        await control_db.execute(
            select(User).where(User.id == subject_id, User.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if user is None:
        # Already gone — treat as a completed no-op (idempotent).
        result.already_erased = True
        return result

    # The storage + auth legs run before the tombstone check, so they reach a
    # subject erased before those legs existed (see `_erase_documents`).
    if tenant_db is not None:
        await _erase_documents(
            subject_type=SUBJECT_USER,
            subject_id=subject_id,
            organization_id=organization_id,
            tenant_db=tenant_db,
            result=result,
            now=now,
        )
    await _erase_auth_material(subject_id=subject_id, control_db=control_db, result=result)

    # Idempotency: the User model has no `meta` column, so we detect a prior
    # erasure by the tombstone email we wrote last time. Deleting a leftover
    # passkey or revoking a live session is real work, so a re-run that found
    # either is NOT a no-op.
    already_tombstoned = user.email.startswith("erased+") and user.email.endswith(
        "@redacted.invalid"
    )
    if already_tombstoned:
        result.already_erased = not (
            result.passkeys_deleted or result.sessions_revoked or result.documents_deleted
        )
        if not result.already_erased:
            result.record_counts = _user_record_counts(result, users=0)
        return result

    user.email = _redacted_email(subject_id)
    user.full_name = REDACTED
    user.sso_provider = None
    user.sso_provider_id = None
    user.hashed_password = None
    user.mfa_secret = None
    user.mfa_enabled = False
    user.is_active = False
    result.fields_redacted = 6
    result.record_counts = _user_record_counts(result, users=1)
    return result


def _user_record_counts(result: ErasureResult, *, users: int) -> dict[str, int]:
    """PII-free breakdown for the `user` leg — counts only, never identifiers."""
    counts = {"users": users}
    if result.passkeys_deleted:
        counts["passkeys_deleted"] = result.passkeys_deleted
    if result.sessions_revoked:
        counts["sessions_revoked"] = result.sessions_revoked
    return _with_document_counts(counts, result)


def _with_document_counts(counts: dict[str, int], result: ErasureResult) -> dict[str, int]:
    """Append the storage-leg counters when the leg found anything."""
    if result.documents_deleted:
        counts["documents_deleted"] = result.documents_deleted
    if result.documents_retained:
        counts["documents_retained"] = result.documents_retained
    if result.documents_failed:
        counts["documents_failed"] = result.documents_failed
    return counts


async def erase_vendor_user(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
    now: datetime | None = None,
) -> ErasureResult:
    now = now or datetime.now(UTC)
    result = ErasureResult()
    vu = (
        await tenant_db.execute(select(VendorUser).where(VendorUser.id == subject_id))
    ).scalar_one_or_none()
    if vu is None:
        result.already_erased = True
        return result

    await _erase_documents(
        subject_type=SUBJECT_VENDOR_USER,
        subject_id=subject_id,
        organization_id=organization_id,
        tenant_db=tenant_db,
        result=result,
        now=now,
    )

    if vu.email.startswith("erased+") and vu.email.endswith("@redacted.invalid"):
        result.already_erased = not result.documents_deleted
        if not result.already_erased:
            result.record_counts = _with_document_counts({"vendor_users": 0}, result)
        return result

    vu.email = _redacted_email(subject_id)
    vu.full_name = REDACTED
    vu.hashed_password = None
    vu.mfa_secret = None
    vu.mfa_enabled = False
    vu.is_active = False
    result.fields_redacted = 5
    result.record_counts = _with_document_counts({"vendor_users": 1}, result)
    return result


async def erase_vendor_contact(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
    now: datetime | None = None,
) -> ErasureResult:
    """Redact a vendor's contact PII; preserve the legal payee + money trail.

    ``vendor.name`` is preserved — it's denormalised onto every Invoice's
    ``vendor_name`` (a record we must legally retain), and nulling it would
    orphan the financial trail. Only the *contact* fields a data subject can
    demand erased are redacted. We also redact the supplier-authored chat
    message bodies (free-text PII) but keep the thread + the AP side.

    The Vendor model has no generic ``meta`` JSONB, so a prior run is detected
    by whether the contact fields are already all NULL (and no portal user / chat
    body still needs redacting).
    """
    now = now or datetime.now(UTC)
    result = ErasureResult()
    vendor = (
        await tenant_db.execute(
            select(Vendor).where(Vendor.id == subject_id, Vendor.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if vendor is None:
        result.already_erased = True
        return result

    # Storage leg first — see `_erase_documents` for why it precedes the
    # idempotency check and why the objects go before the caller commits.
    await _erase_documents(
        subject_type=SUBJECT_VENDOR_CONTACT,
        subject_id=subject_id,
        organization_id=organization_id,
        tenant_db=tenant_db,
        result=result,
        now=now,
    )

    # Idempotency: if every contact PII field is already cleared, this is a
    # re-run — no-op.
    contact_fields_cleared = (
        vendor.email is None
        and vendor.phone is None
        and vendor.address is None
        and vendor.tax_id is None
        and vendor.bank_details is None
        and vendor.beneficial_owner_data is None
    )

    fields_redacted = 0
    if not contact_fields_cleared:
        if vendor.email is not None:
            vendor.email = None
            fields_redacted += 1
        if vendor.phone is not None:
            vendor.phone = None
            fields_redacted += 1
        if vendor.address is not None:
            vendor.address = None
            fields_redacted += 1
        if vendor.tax_id is not None:
            vendor.tax_id = None
            fields_redacted += 1
        if vendor.bank_details is not None:
            vendor.bank_details = None
            fields_redacted += 1
        if vendor.beneficial_owner_data is not None:
            vendor.beneficial_owner_data = None
            fields_redacted += 1

    # Redact any portal users for this vendor too (their email/name is the same
    # natural person's PII).
    portal_users = (
        (await tenant_db.execute(select(VendorUser).where(VendorUser.vendor_id == subject_id)))
        .scalars()
        .all()
    )
    portal_redacted = 0
    for vu in portal_users:
        if vu.email.startswith("erased+") and vu.email.endswith("@redacted.invalid"):
            continue
        vu.email = _redacted_email(vu.id)
        vu.full_name = REDACTED
        vu.hashed_password = None
        vu.mfa_secret = None
        vu.mfa_enabled = False
        vu.is_active = False
        portal_redacted += 1

    # Redact supplier-authored chat message bodies (free-text PII the supplier
    # wrote). Keep the row + author_role so the AP timeline stays coherent.
    # Narrow to this vendor's invoices: thread.invoice -> Invoice.vendor_id.
    from app.models.invoice import Invoice

    vendor_invoice_ids = (
        (
            await tenant_db.execute(
                select(Invoice.id).where(
                    Invoice.vendor_id == subject_id, Invoice.organization_id == organization_id
                )
            )
        )
        .scalars()
        .all()
    )
    chat_redacted = 0
    if vendor_invoice_ids:
        msgs = (
            (
                await tenant_db.execute(
                    select(SupplierChatMessage)
                    .join(
                        SupplierChatThread, SupplierChatMessage.thread_id == SupplierChatThread.id
                    )
                    .where(
                        SupplierChatThread.invoice_id.in_(vendor_invoice_ids),
                        SupplierChatMessage.author_role == ChatAuthorRole.supplier,
                    )
                )
            )
            .scalars()
            .all()
        )
        for m in msgs:
            if m.body != REDACTED:
                m.body = REDACTED
                m.author_name = None
                chat_redacted += 1

    if (
        contact_fields_cleared
        and portal_redacted == 0
        and chat_redacted == 0
        and result.documents_deleted == 0
    ):
        result.already_erased = True
        return result

    result.fields_redacted = fields_redacted
    result.record_counts = _with_document_counts(
        {
            "vendors": 1,
            "vendor_contact_fields": fields_redacted,
            "portal_users_redacted": portal_redacted,
            "chat_messages_redacted": chat_redacted,
        },
        result,
    )
    return result


async def erase_subject(
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    control_db: AsyncSession,
    tenant_db: AsyncSession,
    now: datetime | None = None,
) -> ErasureResult:
    """Dispatch erasure to the per-subject-type redactor.

    Never commits and never touches a money field or an ``audit_log`` row. It
    DOES delete storage objects (irreversibly, before the caller commits) and,
    for a ``user`` subject, passkey rows + live sessions — see the module
    docstring for why each is ordered where it is.
    """
    if subject_type == SUBJECT_USER:
        return await erase_user(
            subject_id=subject_id,
            organization_id=organization_id,
            control_db=control_db,
            tenant_db=tenant_db,
            now=now,
        )
    if subject_type == SUBJECT_VENDOR_USER:
        return await erase_vendor_user(
            subject_id=subject_id,
            organization_id=organization_id,
            tenant_db=tenant_db,
            now=now,
        )
    if subject_type == SUBJECT_VENDOR_CONTACT:
        return await erase_vendor_contact(
            subject_id=subject_id,
            organization_id=organization_id,
            tenant_db=tenant_db,
            now=now,
        )
    raise ValueError(f"Unknown subject_type: {subject_type}")
