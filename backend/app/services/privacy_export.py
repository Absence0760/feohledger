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

**Every high-volume collection is capped in the response, and the complete set
is one download away.** The collections that can grow without bound for a
single subject — audit events, notifications, expense reports and their
expenses, chat messages, contracts, virtual cards, a vendor's invoices and
payments, and the document manifest — are cut at :data:`MAX_EXPORT_ROWS` in the
``POST /privacy/dsar`` response and wrapped by :func:`_capped` with a
``truncated`` flag and the true total beside them. That response is built on the
event loop and held in memory whole, so it has to be bounded. A cap is not a
completeness answer on its own, though: GDPR Art. 15 owes the subject the whole
set, not a tally of what was left out. So every capped section is declared once
as a :class:`_Section` (a column select plus its serialiser), and the same
declaration drives both readers — :func:`build_dsar_bundle` reads the first
:data:`MAX_EXPORT_ROWS` of it, and :func:`stream_full_dsar_bundle` streams all of
it off a server-side cursor, uncapped, for ``GET /privacy/dsar/{id}/bundle``.
One declaration means the preview and the complete bundle cannot disagree about
what a section holds (``docs/decisions.md`` §369). The few small,
naturally-bounded lists — a user's roles, passkeys and push devices, a vendor's
portal users — are returned whole in both.

Pure-ish: the gather functions take the sessions and return plain dicts; the API
layer owns the session lifecycle, the audit write, and the request-row insert.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
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


#: Per-collection row cap of the INLINE response (``POST /privacy/dsar``). Large
#: enough that a normal subject's export is complete inline, small enough that a
#: pathological one cannot build an unbounded response on the event loop. A
#: truncated collection says so, reports its true total, and the response points
#: at the complete bundle (:func:`stream_full_dsar_bundle`), which has no cap.
MAX_EXPORT_ROWS = 1000

#: Rows fetched per server-side cursor round trip, and rows rendered per emitted
#: chunk, when the complete bundle is streamed.
STREAM_CHUNK_ROWS = 500


def _capped(rows: list, total: int) -> dict:
    """Wrap a capped collection with its true total, so a tally is never silent."""
    return {
        "total": total,
        "returned": len(rows),
        "truncated": total > len(rows),
        "items": rows,
    }


#: The bundle sections counted into the response's typed ``record_counts``.
#: The admin page builds its "what this bundle holds" summary from these, in
#: the reader's language, rather than from anything the server wrote in prose
#: (docs/decisions.md §360). A new list-shaped section must be added here —
#: ``tests/test_privacy_record_counts.py`` fails on one that is not — and the
#: frontend label map (``frontend/src/lib/types/privacy.ts``) reads this tuple.
DSAR_COUNTED_SECTIONS: tuple[str, ...] = (
    "roles",
    "push_devices",
    "passkeys",
    "audit_events",
    "notifications",
    "expense_reports",
    "expenses",
    "related_invoices",
    "related_payments",
    "portal_users",
    "contracts",
    "virtual_cards",
    "chat_messages",
    "documents",
)


def bundle_record_counts(bundle: dict) -> dict[str, int]:
    """How many records each counted section of a DSAR bundle holds.

    A list counts its length; a capped collection or the document manifest
    counts its true ``total`` (not the returned page, so a truncated section
    still reports what is held). Sections a subject type does not have are
    absent rather than zero.
    """
    counts: dict[str, int] = {}
    for section in DSAR_COUNTED_SECTIONS:
        value = bundle.get(section)
        if isinstance(value, list):
            counts[section] = len(value)
        elif isinstance(value, dict) and isinstance(value.get("total"), int):
            counts[section] = value["total"]
    return counts


def truncated_sections(bundle: dict) -> list[str]:
    """The counted sections of a bundle that the inline response cut short.

    Empty means the inline bundle is the whole of what is held, and the
    response says ``complete: true``. Anything listed is held in full in the
    streamed bundle (``GET /privacy/dsar/{id}/bundle``).
    """
    return [
        section
        for section in DSAR_COUNTED_SECTIONS
        if isinstance(bundle.get(section), dict) and bundle[section].get("truncated") is True
    ]


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


def _json_default(value: Any) -> Any:
    """``json.dumps`` fallback for the streamed bundle: the same coercions as
    :func:`_jsonable`, and a ``TypeError`` for anything else rather than a
    silent ``str()`` of an object nobody meant to export."""
    coerced = _jsonable(value)
    if coerced is value:
        raise TypeError(f"not JSON-serialisable in a DSAR bundle: {type(value).__name__}")
    return coerced


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Section declarations — one per high-volume collection, shared by the capped
# inline reader and the uncapped streamed one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Section:
    """One high-volume bundle section.

    ``query`` selects plain COLUMNS (never an ORM entity), filtered to the
    subject and the org, ordered, and unlimited. Columns because the streamed
    reader walks it off a server-side cursor, and a streamed entity would pile
    up in the session's identity map — the same reason the audit export selects
    columns (``api/audit._stream_entries``). ``serialize`` turns one row into
    the bundle entry; both readers call it, so the inline preview and the
    complete bundle render a row identically.
    """

    name: str
    query: Select
    serialize: Callable[[Any], dict]


async def _section_total(db: AsyncSession, section: _Section) -> int:
    counted = select(func.count()).select_from(section.query.order_by(None).subquery())
    return int((await db.execute(counted)).scalar_one())


async def _read_capped(db: AsyncSession, section: _Section) -> dict:
    """The inline reader: the first :data:`MAX_EXPORT_ROWS` rows + the true total."""
    total = await _section_total(db, section)
    rows = (await db.execute(section.query.limit(MAX_EXPORT_ROWS))).all()
    return _capped([section.serialize(r) for r in rows], total)


@dataclass
class _BundlePlan:
    """Everything one subject's bundle is made of.

    ``head`` holds the small, naturally-bounded parts, already read (profile,
    roles, passkeys, the vendor record, portal users). ``documents`` is the
    stored-object traversal, kept apart because the inline response caps its
    listing and the streamed bundle does not. ``sections`` are the high-volume
    collections, not yet read. ``tail`` derives any trailing tallies from the
    sections' true totals (the vendor bundle's legacy ``counts``).
    """

    head: dict
    documents: list
    sections: list[_Section]
    tail: Callable[[dict[str, int]], dict] = field(default=lambda totals: {})


def _documents_view(docs: list, *, cap: int | None) -> dict:
    """The document manifest, its listing cut at ``cap`` when one is given.

    ``total`` is always the whole traversal; ``returned`` / ``truncated`` say
    whether the listing below it is all of it.
    """
    manifest = documents_manifest(docs)
    listing = manifest["documents"]
    if cap is not None and len(listing) > cap:
        listing = listing[:cap]
    manifest["documents"] = listing
    manifest["returned"] = len(listing)
    manifest["truncated"] = manifest["total"] > len(listing)
    return manifest


# ---------------------------------------------------------------------------
# Subject resolution
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# user — a control-plane User
# ---------------------------------------------------------------------------


def _user_sections(subject_id: uuid.UUID, organization_id: uuid.UUID) -> list[_Section]:
    """The CONTENT of a user's activity.

    Audit rows omit ``details`` deliberately: an audit row an admin authored
    ABOUT another subject carries that subject's identifiers, and Art. 15(4)
    says an access right does not adversely affect the rights of others. What
    the user did — action, target, when — is theirs; what it recorded about
    someone else is not.
    """
    report_filter = (
        ExpenseReport.employee_user_id == subject_id,
        ExpenseReport.organization_id == organization_id,
    )
    return [
        _Section(
            name="audit_events",
            query=select(
                AuditLog.id,
                AuditLog.action,
                AuditLog.entity_type,
                AuditLog.entity_id,
                AuditLog.created_at,
            )
            .where(AuditLog.actor_id == subject_id, AuditLog.organization_id == organization_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id),
            serialize=lambda r: {
                "id": str(r.id),
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": str(r.entity_id) if r.entity_id else None,
                "created_at": _jsonable(r.created_at),
            },
        ),
        _Section(
            name="notifications",
            query=select(
                Notification.id,
                Notification.event_type,
                Notification.entity_type,
                Notification.entity_id,
                Notification.title,
                Notification.body,
                Notification.read_at,
                Notification.created_at,
            )
            .where(
                Notification.recipient_user_id == subject_id,
                Notification.organization_id == organization_id,
            )
            .order_by(Notification.created_at.desc(), Notification.id),
            serialize=lambda n: {
                "id": str(n.id),
                "event_type": n.event_type,
                "entity_type": n.entity_type,
                "entity_id": str(n.entity_id) if n.entity_id else None,
                "title": n.title,
                "body": n.body,
                "read_at": _jsonable(n.read_at),
                "created_at": _jsonable(n.created_at),
            },
        ),
        _Section(
            name="expense_reports",
            query=select(
                ExpenseReport.id,
                ExpenseReport.report_number,
                ExpenseReport.title,
                ExpenseReport.status,
                ExpenseReport.total_amount,
                ExpenseReport.currency,
                ExpenseReport.submitted_at,
                ExpenseReport.approved_at,
                ExpenseReport.created_at,
            )
            .where(*report_filter)
            .order_by(ExpenseReport.created_at.desc(), ExpenseReport.id),
            serialize=lambda r: {
                "id": str(r.id),
                "report_number": r.report_number,
                "title": r.title,
                "status": str(r.status),
                "total_amount": _jsonable(r.total_amount),
                "currency": r.currency,
                "submitted_at": _jsonable(r.submitted_at),
                "approved_at": _jsonable(r.approved_at),
                "created_at": _jsonable(r.created_at),
            },
        ),
        # Every line on every one of the subject's reports — joined, not
        # restricted to the reports the inline response happened to return, so
        # the section and its total are the same set.
        _Section(
            name="expenses",
            query=select(
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
            .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
            .where(*report_filter)
            .order_by(Expense.expense_date, Expense.id),
            serialize=lambda e: {
                "id": str(e.id),
                "report_id": str(e.report_id) if e.report_id else None,
                "expense_date": _jsonable(e.expense_date),
                "merchant": e.merchant,
                "category": e.category,
                "description": e.description,
                "amount": _jsonable(e.amount),
                "currency": e.currency,
                "status": str(e.status),
            },
        ),
    ]


async def _plan_user(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    control_db: AsyncSession,
    tenant_db: AsyncSession,
) -> _BundlePlan:
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

    head = {
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
            "locale": user.locale,
            "created_at": _jsonable(user.created_at),
            "organization_id": str(user.organization_id),
        },
        # Mobile push registrations, one per platform. The token itself is
        # withheld for the same reason a passkey's credential id is: it is a
        # handle to the subject's device, and what makes the entry recognisable
        # is the platform and when it was registered.
        "push_devices": [
            {
                "platform": platform,
                "registered_at": (entry or {}).get("updated_at")
                if isinstance(entry, dict)
                else None,
                "_note": "push token withheld (device handle)",
            }
            for platform, entry in sorted((user.device_tokens or {}).items())
        ],
        "roles": [r.name for r in user.roles],
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
    }

    def tail(totals: dict[str, int]) -> dict:
        # The counts predate the content and are kept, from the sections' true
        # totals: they are cheap, and something out there reads them.
        return {
            "activity": {
                "audit_actions_authored": totals["audit_events"],
                "in_app_notifications": totals["notifications"],
            }
        }

    return _BundlePlan(
        head=head,
        documents=documents,
        sections=_user_sections(subject_id, organization_id),
        tail=tail,
    )


# ---------------------------------------------------------------------------
# vendor_user — a supplier-portal login
# ---------------------------------------------------------------------------


def _chat_message_entry(msg: Any) -> dict:
    """One chat message. Attachment BYTES stay in storage — the manifest is
    where the objects are enumerated. ``msg`` is a column row (or an entity):
    both answer to the same attribute names."""
    return {
        "id": str(msg.id),
        "thread_id": str(msg.thread_id),
        "author_role": str(msg.author_role),
        "author_name": msg.author_name,
        "body": msg.body,
        "attachment_count": len(msg.attachments or []),
        "created_at": _jsonable(msg.created_at),
    }


_CHAT_COLUMNS = (
    SupplierChatMessage.id,
    SupplierChatMessage.thread_id,
    SupplierChatMessage.author_role,
    SupplierChatMessage.author_name,
    SupplierChatMessage.body,
    SupplierChatMessage.attachments,
    SupplierChatMessage.created_at,
)


async def _plan_vendor_user(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
) -> _BundlePlan:
    """PII for a tenant VendorUser, the parent vendor link, and what they wrote."""
    vu = (
        await tenant_db.execute(select(VendorUser).where(VendorUser.id == subject_id))
    ).scalar_one_or_none()
    if vu is None:
        raise SubjectNotFound("vendor_user not found")

    documents = await collect_subject_documents(
        subject_type=SUBJECT_VENDOR_USER,
        subject_id=subject_id,
        organization_id=organization_id,
        tenant_db=tenant_db,
    )

    head = {
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
    }
    sections = [
        _Section(
            name="chat_messages",
            query=select(*_CHAT_COLUMNS)
            .where(SupplierChatMessage.author_user_id == subject_id)
            .order_by(SupplierChatMessage.created_at, SupplierChatMessage.id),
            serialize=_chat_message_entry,
        )
    ]
    return _BundlePlan(head=head, documents=documents, sections=sections)


# ---------------------------------------------------------------------------
# vendor_contact — a Vendor's contact details and the records tied to it
# ---------------------------------------------------------------------------


def _vendor_contact_sections(subject_id: uuid.UUID, organization_id: uuid.UUID) -> list[_Section]:
    """A vendor's invoices, payments, chat, contracts and cards.

    Payments and chat reach the vendor through its invoices by SUBQUERY, not
    through a list of the invoice ids already read — so they stay complete when
    the invoice section itself is capped.
    """
    invoice_filter = (
        Invoice.vendor_id == subject_id,
        Invoice.organization_id == organization_id,
    )
    vendor_invoice_ids = select(Invoice.id).where(*invoice_filter)
    return [
        _Section(
            name="related_invoices",
            query=select(
                Invoice.id,
                Invoice.invoice_number,
                Invoice.amount,
                Invoice.currency,
                Invoice.status,
                Invoice.created_at,
            )
            .where(*invoice_filter)
            .order_by(Invoice.created_at, Invoice.id),
            serialize=lambda r: {
                "id": str(r.id),
                "invoice_number": r.invoice_number,
                "amount": _jsonable(r.amount),
                "currency": r.currency,
                "status": str(r.status),
                "created_at": _jsonable(r.created_at),
            },
        ),
        _Section(
            name="related_payments",
            query=select(
                Payment.id,
                Payment.invoice_id,
                Payment.amount,
                Payment.method,
                Payment.status,
                Payment.created_at,
            )
            .where(Payment.invoice_id.in_(vendor_invoice_ids))
            .order_by(Payment.created_at, Payment.id),
            serialize=lambda r: {
                "id": str(r.id),
                "invoice_id": str(r.invoice_id),
                "amount": _jsonable(r.amount),
                "method": r.method,
                "status": r.status,
                "created_at": _jsonable(r.created_at),
            },
        ),
        _Section(
            name="contracts",
            query=select(
                Contract.id,
                Contract.contract_number,
                Contract.title,
                Contract.contract_type,
                Contract.status,
                Contract.currency,
                Contract.total_value,
                Contract.start_date,
                Contract.end_date,
                Contract.signed_date,
                Contract.created_at,
            )
            .where(Contract.vendor_id == subject_id, Contract.organization_id == organization_id)
            .order_by(Contract.created_at, Contract.id),
            serialize=lambda c: {
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
            },
        ),
        # Virtual cards issued to this vendor. The row stores `last_four` only —
        # no PAN has ever been persisted (`docs/virtual-cards.md`), so there is
        # nothing here to mask that is not already masked at rest.
        _Section(
            name="virtual_cards",
            query=select(
                VirtualCard.id,
                VirtualCard.invoice_id,
                VirtualCard.card_provider,
                VirtualCard.last_four,
                VirtualCard.amount_limit,
                VirtualCard.amount_charged,
                VirtualCard.currency,
                VirtualCard.status,
                VirtualCard.expires_at,
                VirtualCard.created_at,
            )
            .where(
                VirtualCard.vendor_id == subject_id,
                VirtualCard.organization_id == organization_id,
            )
            .order_by(VirtualCard.created_at, VirtualCard.id),
            serialize=lambda c: {
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
            },
        ),
        _Section(
            name="chat_messages",
            query=select(*_CHAT_COLUMNS)
            .join(SupplierChatThread, SupplierChatMessage.thread_id == SupplierChatThread.id)
            .where(SupplierChatThread.invoice_id.in_(vendor_invoice_ids))
            .order_by(SupplierChatMessage.created_at, SupplierChatMessage.id),
            serialize=_chat_message_entry,
        ),
    ]


async def _plan_vendor_contact(
    *,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    tenant_db: AsyncSession,
    include_banking: bool,
) -> _BundlePlan:
    """Vendor contact PII + the related business records.

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

    portal_users = (
        await tenant_db.execute(
            select(VendorUser.id, VendorUser.email, VendorUser.full_name).where(
                VendorUser.vendor_id == subject_id
            )
        )
    ).all()

    documents = await collect_subject_documents(
        subject_type=SUBJECT_VENDOR_CONTACT,
        subject_id=subject_id,
        organization_id=organization_id,
        tenant_db=tenant_db,
    )

    head = {
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
        "portal_users": [
            {"id": str(r.id), "email": r.email, "full_name": r.full_name} for r in portal_users
        ],
    }
    n_portal = len(portal_users)
    n_documents = len(documents)

    def tail(totals: dict[str, int]) -> dict:
        # The legacy tallies, from the sections' TRUE totals — never the length
        # of a capped page.
        return {
            "counts": {
                "invoices": totals["related_invoices"],
                "payments": totals["related_payments"],
                "portal_users": n_portal,
                "chat_messages": totals["chat_messages"],
                "contracts": totals["contracts"],
                "virtual_cards": totals["virtual_cards"],
                "documents": n_documents,
            }
        }

    return _BundlePlan(
        head=head,
        documents=documents,
        sections=_vendor_contact_sections(subject_id, organization_id),
        tail=tail,
    )


# ---------------------------------------------------------------------------
# The two readers
# ---------------------------------------------------------------------------


async def _plan_bundle(
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    control_db: AsyncSession,
    tenant_db: AsyncSession,
    include_banking: bool,
) -> _BundlePlan:
    """Dispatch to the per-subject-type plan.

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
        return await _plan_user(
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
        return await _plan_vendor_user(
            subject_id=subject_id,
            organization_id=organization_id,
            tenant_db=tenant_db,
        )
    if subject_type == SUBJECT_VENDOR_CONTACT:
        return await _plan_vendor_contact(
            subject_id=subject_id,
            organization_id=organization_id,
            tenant_db=tenant_db,
            include_banking=include_banking,
        )
    raise SubjectNotFound(f"Unknown subject_type: {subject_type}")


async def build_dsar_bundle(
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    control_db: AsyncSession,
    tenant_db: AsyncSession,
    include_banking: bool = False,
) -> dict:
    """The INLINE bundle: every high-volume section capped at
    :data:`MAX_EXPORT_ROWS`, each reporting its true total.

    :func:`truncated_sections` over the result says whether it is complete; when
    it is not, the complete set is :func:`stream_full_dsar_bundle`'s.
    """
    plan = await _plan_bundle(
        subject_type=subject_type,
        subject_id=subject_id,
        organization_id=organization_id,
        control_db=control_db,
        tenant_db=tenant_db,
        include_banking=include_banking,
    )
    bundle = dict(plan.head)
    totals: dict[str, int] = {}
    for section in plan.sections:
        capped = await _read_capped(tenant_db, section)
        bundle[section.name] = capped
        totals[section.name] = capped["total"]
    bundle["documents"] = _documents_view(plan.documents, cap=MAX_EXPORT_ROWS)
    bundle.update(plan.tail(totals))
    return bundle


async def stream_full_dsar_bundle(
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    control_db: AsyncSession,
    tenant_db: AsyncSession,
    envelope: dict,
    summary: dict | None = None,
) -> AsyncIterator[str]:
    """The COMPLETE bundle as JSON text, in bounded chunks. Nothing is capped.

    The document has the inline response's shape — ``envelope`` (request id,
    subject, ``generated_at``, …), ``complete: true``, ``data`` and
    ``record_counts`` — and every section carries ``total == returned`` and
    ``truncated: false``, so a consumer that reads one reads the other. Each
    high-volume section is read off a server-side cursor
    (``yield_per=STREAM_CHUNK_ROWS``) and rendered :data:`STREAM_CHUNK_ROWS`
    rows at a time, so neither the rows nor the rendered body are ever whole in
    memory; the caller spools the chunks (``utils/spooled_export``).

    Banking is ALWAYS masked here. The only section carrying it — ``vendor`` —
    is a single record, never capped, so the inline response (where an
    unmasked disclosure answers to its own step-up and audit row) already holds
    it whole; this download adds nothing banking-bearing to unlock.

    A section's ``total`` is the number of rows actually streamed, emitted after
    them, so it is exact for the snapshot the cursor read. The closing braces
    come last: a run that dies part-way can never parse as a complete bundle.

    ``summary``, when given, receives the bundle's ``record_counts`` once the
    last chunk is produced — PII-free tallies the caller records in its audit
    row.
    """
    plan = await _plan_bundle(
        subject_type=subject_type,
        subject_id=subject_id,
        organization_id=organization_id,
        control_db=control_db,
        tenant_db=tenant_db,
        include_banking=False,
    )

    opening = {**envelope, "banking_disclosure": "masked", "complete": True}
    yield _dumps(opening)[:-1] + ',"data":{'

    first = True

    def key(name: str) -> str:
        nonlocal first
        prefix = "" if first else ","
        first = False
        return f"{prefix}{_dumps(name)}:"

    counted: dict[str, Any] = {}
    for name, value in plan.head.items():
        counted[name] = value
        yield key(name) + _dumps(value)

    totals: dict[str, int] = {}
    for section in plan.sections:
        parts: list[str] = [key(section.name) + '{"items":[']
        n = 0
        result = await tenant_db.stream(
            section.query.execution_options(yield_per=STREAM_CHUNK_ROWS)
        )
        async for row in result:
            parts.append(("," if n else "") + _dumps(section.serialize(row)))
            n += 1
            if len(parts) >= STREAM_CHUNK_ROWS:
                yield "".join(parts)
                parts = []
        parts.append(f'],"total":{n},"returned":{n},"truncated":false}}')
        yield "".join(parts)
        totals[section.name] = n
        counted[section.name] = {"total": n}

    documents = _documents_view(plan.documents, cap=None)
    counted["documents"] = documents
    yield key("documents") + _dumps(documents)

    for name, value in plan.tail(totals).items():
        yield key(name) + _dumps(value)

    record_counts = bundle_record_counts(counted)
    if summary is not None:
        summary["record_counts"] = record_counts
    yield '},"record_counts":' + _dumps(record_counts) + "}"
