"""DSAR export — assemble everything held about a data subject (GDPR Art. 15 /
CCPA right-to-know) into a portable JSON bundle.

Three subject types, each living in a different place:

  * ``user``           — a control-plane :class:`User` (AP-team member). PII +
                         roles come from the control DB; the tenant DB
                         contributes the CONTENT of their activity (the audit
                         actions they authored, their in-app notifications,
                         their expense reports), plus passkey metadata. Art. 15
                         is a right to the data, not to a tally of it — the
                         legacy ``activity`` counts are still returned beside
                         it so nothing that read them breaks.
  * ``vendor_user``    — a tenant :class:`VendorUser` (supplier-portal login).
  * ``vendor_contact`` — the contact PII on a tenant :class:`Vendor` (the
                         supplier company's own contact details), plus a
                         summary of the business records tied to that vendor
                         (invoices, payments, portal users, chat).

The bundle is the subject's data only. Every query is filtered by the resolved
subject id AND the caller's ``organization_id`` so a DSAR for one subject can
never surface another subject's — or another tenant's — data. Money fields in
the related-records summary are serialised as **string-Decimal** (never float),
and only field *values that belong to this subject* are included.

**Banking data is masked by default and unmasking is a separate, gated act.**
``vendor.bank_details`` and ``vendor.beneficial_owner_data`` were returned
verbatim here, which made a routine admin-only DSAR the one surface in the
product that emits a full account / routing number / IBAN into a downloadable
file — the same asset the dual-control change queue exists to protect, with none
of the ceremony. Every bundle now reduces them through
``utils/bank_masking``; ``include_banking=True`` returns them whole and is
gated on the ``vendor.bank_change.approve`` permission and separately audited by
the router. See ``docs/decisions.md`` § 182.

**Stored documents are enumerated, not inlined.** The manifest comes from the
shared traversal in ``services/privacy_documents`` — the same walk the erasure
leg deletes through — so the two can never disagree about what is held.

**Every list is capped** at :data:`MAX_EXPORT_ROWS` with a ``truncated`` flag and
the true total beside it. An export is a synchronous HTTP response; an AP user
with three years of audit rows would otherwise build an unbounded JSON document
in memory on the event loop.

Pure-ish: the gather functions take the sessions and return plain dicts; the API
layer owns the session lifecycle, the audit write, and the request-row insert.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.contract import Contract
from app.models.data_subject_request import (
    SUBJECT_USER,
    SUBJECT_VENDOR_CONTACT,
    SUBJECT_VENDOR_USER,
)
from app.models.expense import Expense, ExpenseReport
from app.models.invoice import Invoice
from app.models.notification import Notification
from app.models.payment import Payment
from app.models.supplier_chat import SupplierChatMessage, SupplierChatThread
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.models.virtual_card import VirtualCard
from app.models.webauthn_credential import WebAuthnCredential
from app.models.workflow import AuditLog
from app.services.privacy_documents import collect_subject_documents, documents_manifest
from app.utils.bank_masking import mask_bank_details, mask_beneficial_owner_data


class SubjectNotFound(Exception):
    """The requested subject could not be resolved within this tenant/org."""


class BankingDisclosureNotPermitted(Exception):
    """``include_banking`` was asked for where it cannot apply."""


#: Per-collection row cap. Large enough that a normal subject's export is
#: complete, small enough that a pathological one cannot build an unbounded
#: response. A truncated collection says so and reports its true total.
MAX_EXPORT_ROWS = 1000


def _capped(rows: list, total: int) -> dict:
    """Wrap a capped collection with its true total, so a tally is never silent."""
    return {
        "total": total,
        "returned": len(rows),
        "truncated": total > len(rows),
        "items": rows,
    }


def _jsonable(value: Any) -> Any:
    """Coerce a column value to something JSON-serialisable.

    Decimals → string (money-exact, never float); datetimes/dates → ISO; UUIDs →
    str. Everything else passes through.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def resolve_subject_id(
    *,
    subject_type: str,
    identifier: str,
    organization_id: uuid.UUID,
    control_db: AsyncSession,
    tenant_db: AsyncSession,
) -> uuid.UUID:
    """Resolve the subject identifier to a UUID, scoped to this org/tenant.

    Raises :class:`SubjectNotFound` if no matching subject exists *in this
    tenant* — which is also the cross-tenant guard: a User from another org, or
    a Vendor/VendorUser in another tenant's DB, never resolves here.
    """
    if subject_type == SUBJECT_USER:
        row = (
            await control_db.execute(
                select(User.id).where(
                    func.lower(User.email) == identifier.strip().lower(),
                    User.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise SubjectNotFound("No user with that email in this organization")
        return row

    if subject_type == SUBJECT_VENDOR_USER:
        row = (
            await tenant_db.execute(
                select(VendorUser.id).where(
                    func.lower(VendorUser.email) == identifier.strip().lower()
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise SubjectNotFound("No vendor user with that email in this tenant")
        return row

    if subject_type == SUBJECT_VENDOR_CONTACT:
        # Identifier is a Vendor UUID. (Vendor "contact" PII has no unique
        # natural key — many vendors share a blank/duplicate email — so the
        # subject is addressed by the vendor's own id.)
        try:
            vid = uuid.UUID(identifier.strip())
        except ValueError as exc:
            raise SubjectNotFound("vendor_contact identifier must be a vendor UUID") from exc
        row = (
            await tenant_db.execute(
                select(Vendor.id).where(Vendor.id == vid, Vendor.organization_id == organization_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise SubjectNotFound("No vendor with that id in this tenant")
        return row

    raise SubjectNotFound(f"Unknown subject_type: {subject_type}")


async def build_user_bundle(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    control_db: AsyncSession,
    tenant_db: AsyncSession,
) -> dict:
    """PII + roles for a control-plane User, plus the CONTENT of their activity."""
    user = (
        await control_db.execute(
            select(User)
            .where(User.id == subject_id, User.organization_id == organization_id)
            .options(selectinload(User.roles))
        )
    ).scalar_one_or_none()
    if user is None:
        raise SubjectNotFound("user not found in this organization")

    audit_filter = (
        AuditLog.actor_id == subject_id,
        AuditLog.organization_id == organization_id,
    )
    audit_total = (
        await tenant_db.execute(select(func.count()).select_from(AuditLog).where(*audit_filter))
    ).scalar_one()
    audit_rows = (
        await tenant_db.execute(
            select(
                AuditLog.id,
                AuditLog.action,
                AuditLog.entity_type,
                AuditLog.entity_id,
                AuditLog.created_at,
            )
            .where(*audit_filter)
            .order_by(AuditLog.created_at.desc())
            .limit(MAX_EXPORT_ROWS)
        )
    ).all()

    notif_filter = (
        Notification.recipient_user_id == subject_id,
        Notification.organization_id == organization_id,
    )
    notif_total = (
        await tenant_db.execute(select(func.count()).select_from(Notification).where(*notif_filter))
    ).scalar_one()
    notif_rows = (
        (
            await tenant_db.execute(
                select(Notification)
                .where(*notif_filter)
                .order_by(Notification.created_at.desc())
                .limit(MAX_EXPORT_ROWS)
            )
        )
        .scalars()
        .all()
    )

    report_rows = (
        (
            await tenant_db.execute(
                select(ExpenseReport)
                .where(
                    ExpenseReport.employee_user_id == subject_id,
                    ExpenseReport.organization_id == organization_id,
                )
                .order_by(ExpenseReport.created_at.desc())
                .limit(MAX_EXPORT_ROWS)
            )
        )
        .scalars()
        .all()
    )
    report_total = (
        await tenant_db.execute(
            select(func.count())
            .select_from(ExpenseReport)
            .where(
                ExpenseReport.employee_user_id == subject_id,
                ExpenseReport.organization_id == organization_id,
            )
        )
    ).scalar_one()
    report_ids = [r.id for r in report_rows]
    expense_rows: list = []
    if report_ids:
        expense_rows = (
            await tenant_db.execute(
                select(
                    Expense.id,
                    Expense.report_id,
                    Expense.expense_date,
                    Expense.merchant,
                    Expense.category,
                    Expense.description,
                    Expense.amount,
                    Expense.currency,
                    Expense.status,
                )
                .where(Expense.report_id.in_(report_ids))
                .order_by(Expense.expense_date)
                .limit(MAX_EXPORT_ROWS)
            )
        ).all()

    passkeys = (
        (
            await control_db.execute(
                select(WebAuthnCredential).where(WebAuthnCredential.user_id == subject_id)
            )
        )
        .scalars()
        .all()
    )

    documents = await collect_subject_documents(
        subject_type=SUBJECT_USER,
        subject_id=subject_id,
        organization_id=organization_id,
        tenant_db=tenant_db,
    )

    return {
        "profile": {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "sso_provider": user.sso_provider,
            "sso_provider_id": user.sso_provider_id,
            "is_active": user.is_active,
            "mfa_enabled": user.mfa_enabled,
            "mfa_enrolled_at": _jsonable(user.mfa_enrolled_at),
            "notification_prefs": user.notification_prefs,
            "created_at": _jsonable(user.created_at),
            "organization_id": str(user.organization_id),
        },
        "roles": [r.name for r in user.roles],
        # The counts predate the content below and are kept: they are cheap, and
        # something out there reads them.
        "activity": {
            "audit_actions_authored": audit_total,
            "in_app_notifications": notif_total,
        },
        # The CONTENT of the audit rows this user authored. `details` is
        # deliberately omitted: an audit row an admin authored ABOUT another
        # subject carries that subject's identifiers, and Art. 15(4) says an
        # access right does not adversely affect the rights of others. What the
        # user did — action, target, when — is theirs; what it recorded about
        # someone else is not.
        "audit_events": _capped(
            [
                {
                    "id": str(r.id),
                    "action": r.action,
                    "entity_type": r.entity_type,
                    "entity_id": str(r.entity_id) if r.entity_id else None,
                    "created_at": _jsonable(r.created_at),
                }
                for r in audit_rows
            ],
            audit_total,
        ),
        "notifications": _capped(
            [
                {
                    "id": str(n.id),
                    "event_type": n.event_type,
                    "entity_type": n.entity_type,
                    "entity_id": str(n.entity_id) if n.entity_id else None,
                    "title": n.title,
                    "body": n.body,
                    "read_at": _jsonable(n.read_at),
                    "created_at": _jsonable(n.created_at),
                }
                for n in notif_rows
            ],
            notif_total,
        ),
        "expense_reports": _capped(
            [
                {
                    "id": str(r.id),
                    "report_number": r.report_number,
                    "title": r.title,
                    "status": str(r.status),
                    "total_amount": _jsonable(r.total_amount),
                    "currency": r.currency,
                    "submitted_at": _jsonable(r.submitted_at),
                    "approved_at": _jsonable(r.approved_at),
                    "created_at": _jsonable(r.created_at),
                }
                for r in report_rows
            ],
            report_total,
        ),
        "expenses": [
            {
                "id": str(e.id),
                "report_id": str(e.report_id) if e.report_id else None,
                "expense_date": _jsonable(e.expense_date),
                "merchant": e.merchant,
                "category": e.category,
                "description": e.description,
                "amount": _jsonable(e.amount),
                "currency": e.currency,
                "status": str(e.status),
            }
            for e in expense_rows
        ],
        # Passkey METADATA. `credential_id` and `public_key` are withheld: they
        # are the authenticator's handle and verification key, they identify the
        # subject's physical device, and neither is information the subject
        # cannot already see in the security UI. What is returned is what makes
        # the entry recognisable — its label, the host it is bound to, when it
        # was registered and last used.
        "passkeys": [
            {
                "id": str(c.id),
                "name": c.name,
                "rp_id": c.rp_id,
                "transports": c.transports,
                "sign_count": c.sign_count,
                "last_used_at": _jsonable(c.last_used_at),
                "created_at": _jsonable(c.created_at),
                "_note": "credential_id / public_key withheld (authenticator material)",
            }
            for c in passkeys
        ],
        "documents": documents_manifest(documents),
    }


async def build_vendor_user_bundle(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
) -> dict:
    """PII for a tenant VendorUser, the parent vendor link, and what they wrote."""
    vu = (
        await tenant_db.execute(select(VendorUser).where(VendorUser.id == subject_id))
    ).scalar_one_or_none()
    if vu is None:
        raise SubjectNotFound("vendor_user not found")

    msg_total = (
        await tenant_db.execute(
            select(func.count())
            .select_from(SupplierChatMessage)
            .where(SupplierChatMessage.author_user_id == subject_id)
        )
    ).scalar_one()
    messages = (
        (
            await tenant_db.execute(
                select(SupplierChatMessage)
                .where(SupplierChatMessage.author_user_id == subject_id)
                .order_by(SupplierChatMessage.created_at)
                .limit(MAX_EXPORT_ROWS)
            )
        )
        .scalars()
        .all()
    )

    documents = await collect_subject_documents(
        subject_type=SUBJECT_VENDOR_USER,
        subject_id=subject_id,
        organization_id=organization_id,
        tenant_db=tenant_db,
    )

    return {
        "profile": {
            "id": str(vu.id),
            "vendor_id": str(vu.vendor_id),
            "email": vu.email,
            "full_name": vu.full_name,
            "is_active": vu.is_active,
            "last_login_at": _jsonable(vu.last_login_at),
            "mfa_enabled": vu.mfa_enabled,
            "mfa_enrolled_at": _jsonable(vu.mfa_enrolled_at),
            "notification_prefs": vu.notification_prefs,
            "created_at": _jsonable(vu.created_at),
        },
        "chat_messages": _capped(
            [_chat_message_entry(m) for m in messages],
            msg_total,
        ),
        "documents": documents_manifest(documents),
    }


def _chat_message_entry(msg: SupplierChatMessage) -> dict:
    """One chat message the subject authored. Attachment BYTES stay in storage —
    the manifest is where the objects are enumerated."""
    return {
        "id": str(msg.id),
        "thread_id": str(msg.thread_id),
        "author_role": str(msg.author_role),
        "author_name": msg.author_name,
        "body": msg.body,
        "attachment_count": len(msg.attachments or []),
        "created_at": _jsonable(msg.created_at),
    }


async def build_vendor_contact_bundle(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
    include_banking: bool = False,
) -> dict:
    """Vendor contact PII + a summary of the related business records.

    The summary is deliberately a list of identifiers + money totals, not a full
    copy of every invoice — the subject is the *vendor contact*, and the related
    invoices/payments are surfaced so the subject can see what's tied to them.

    ``include_banking`` returns ``bank_details`` and ``beneficial_owner_data``
    whole instead of masked. The caller (``api/privacy``) is responsible for the
    permission gate and the separate audit row; this function only honours the
    flag, so the decision lives at one place and cannot be reached by a service
    that skipped it.
    """
    vendor = (
        await tenant_db.execute(
            select(Vendor).where(Vendor.id == subject_id, Vendor.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise SubjectNotFound("vendor not found in this tenant")

    invoices = (
        await tenant_db.execute(
            select(
                Invoice.id,
                Invoice.invoice_number,
                Invoice.amount,
                Invoice.currency,
                Invoice.status,
                Invoice.created_at,
            )
            .where(Invoice.vendor_id == subject_id, Invoice.organization_id == organization_id)
            .order_by(Invoice.created_at)
        )
    ).all()
    invoice_ids = [r.id for r in invoices]

    payments_rows: list = []
    if invoice_ids:
        payments_rows = (
            await tenant_db.execute(
                select(
                    Payment.id,
                    Payment.invoice_id,
                    Payment.amount,
                    Payment.method,
                    Payment.status,
                    Payment.created_at,
                )
                .where(Payment.invoice_id.in_(invoice_ids))
                .order_by(Payment.created_at)
            )
        ).all()

    portal_users = (
        await tenant_db.execute(
            select(VendorUser.id, VendorUser.email, VendorUser.full_name).where(
                VendorUser.vendor_id == subject_id
            )
        )
    ).all()

    chat_messages = 0
    chat_rows: list = []
    if invoice_ids:
        chat_messages = (
            await tenant_db.execute(
                select(func.count())
                .select_from(SupplierChatMessage)
                .join(SupplierChatThread, SupplierChatMessage.thread_id == SupplierChatThread.id)
                .where(SupplierChatThread.invoice_id.in_(invoice_ids))
            )
        ).scalar_one()
        chat_rows = (
            (
                await tenant_db.execute(
                    select(SupplierChatMessage)
                    .join(
                        SupplierChatThread, SupplierChatMessage.thread_id == SupplierChatThread.id
                    )
                    .where(SupplierChatThread.invoice_id.in_(invoice_ids))
                    .order_by(SupplierChatMessage.created_at)
                    .limit(MAX_EXPORT_ROWS)
                )
            )
            .scalars()
            .all()
        )

    contracts = (
        (
            await tenant_db.execute(
                select(Contract)
                .where(
                    Contract.vendor_id == subject_id,
                    Contract.organization_id == organization_id,
                )
                .order_by(Contract.created_at)
                .limit(MAX_EXPORT_ROWS)
            )
        )
        .scalars()
        .all()
    )

    # Virtual cards issued to this vendor. The row stores `last_four` only — no
    # PAN has ever been persisted (`docs/virtual-cards.md`), so there is nothing
    # here to mask that is not already masked at rest.
    cards = (
        (
            await tenant_db.execute(
                select(VirtualCard)
                .where(
                    VirtualCard.vendor_id == subject_id,
                    VirtualCard.organization_id == organization_id,
                )
                .order_by(VirtualCard.created_at)
                .limit(MAX_EXPORT_ROWS)
            )
        )
        .scalars()
        .all()
    )

    documents = await collect_subject_documents(
        subject_type=SUBJECT_VENDOR_CONTACT,
        subject_id=subject_id,
        organization_id=organization_id,
        tenant_db=tenant_db,
    )

    return {
        "vendor": {
            "id": str(vendor.id),
            "name": vendor.name,
            "code": vendor.code,
            "email": vendor.email,
            "phone": vendor.phone,
            "address": vendor.address,
            "tax_id": vendor.tax_id,
            "bank_details": (
                vendor.bank_details if include_banking else mask_bank_details(vendor.bank_details)
            ),
            "beneficial_owner_data": (
                vendor.beneficial_owner_data
                if include_banking
                else mask_beneficial_owner_data(vendor.beneficial_owner_data)
            ),
            "banking_disclosure": "unmasked" if include_banking else "masked",
            "status": vendor.status,
            "created_at": _jsonable(vendor.created_at),
        },
        "related_invoices": [
            {
                "id": str(r.id),
                "invoice_number": r.invoice_number,
                "amount": _jsonable(r.amount),
                "currency": r.currency,
                "status": str(r.status),
                "created_at": _jsonable(r.created_at),
            }
            for r in invoices
        ],
        "related_payments": [
            {
                "id": str(r.id),
                "invoice_id": str(r.invoice_id),
                "amount": _jsonable(r.amount),
                "method": r.method,
                "status": r.status,
                "created_at": _jsonable(r.created_at),
            }
            for r in payments_rows
        ],
        "portal_users": [
            {"id": str(r.id), "email": r.email, "full_name": r.full_name} for r in portal_users
        ],
        "contracts": [
            {
                "id": str(c.id),
                "contract_number": c.contract_number,
                "title": c.title,
                "contract_type": str(c.contract_type),
                "status": str(c.status),
                "currency": c.currency,
                "total_value": _jsonable(c.total_value),
                "start_date": _jsonable(c.start_date),
                "end_date": _jsonable(c.end_date),
                "signed_date": _jsonable(c.signed_date),
                "created_at": _jsonable(c.created_at),
            }
            for c in contracts
        ],
        "virtual_cards": [
            {
                "id": str(c.id),
                "invoice_id": str(c.invoice_id),
                "card_provider": c.card_provider,
                "last_four": c.last_four,
                "amount_limit": _jsonable(c.amount_limit),
                "amount_charged": _jsonable(c.amount_charged),
                "currency": c.currency,
                "status": c.status,
                "expires_at": _jsonable(c.expires_at),
                "created_at": _jsonable(c.created_at),
            }
            for c in cards
        ],
        "chat_messages": _capped([_chat_message_entry(m) for m in chat_rows], int(chat_messages)),
        "documents": documents_manifest(documents),
        "counts": {
            "invoices": len(invoices),
            "payments": len(payments_rows),
            "portal_users": len(portal_users),
            "chat_messages": chat_messages,
            "contracts": len(contracts),
            "virtual_cards": len(cards),
            "documents": len(documents),
        },
    }


async def build_dsar_bundle(
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    control_db: AsyncSession,
    tenant_db: AsyncSession,
    include_banking: bool = False,
) -> dict:
    """Dispatch to the per-subject-type bundle builder.

    ``include_banking`` only means anything for ``vendor_contact`` — it is the
    only subject type that has bank details. Asking for it anywhere else raises
    rather than silently doing nothing: an operator who requested an unmasked
    bundle and got a masked one with a 200 would have no way to tell.
    """
    if subject_type == SUBJECT_USER:
        if include_banking:
            raise BankingDisclosureNotPermitted(
                "include_banking applies to vendor_contact subjects only"
            )
        return await build_user_bundle(
            subject_id=subject_id,
            organization_id=organization_id,
            control_db=control_db,
            tenant_db=tenant_db,
        )
    if subject_type == SUBJECT_VENDOR_USER:
        if include_banking:
            raise BankingDisclosureNotPermitted(
                "include_banking applies to vendor_contact subjects only"
            )
        return await build_vendor_user_bundle(
            subject_id=subject_id,
            organization_id=organization_id,
            tenant_db=tenant_db,
        )
    if subject_type == SUBJECT_VENDOR_CONTACT:
        return await build_vendor_contact_bundle(
            subject_id=subject_id,
            organization_id=organization_id,
            tenant_db=tenant_db,
            include_banking=include_banking,
        )
    raise SubjectNotFound(f"Unknown subject_type: {subject_type}")
