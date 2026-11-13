"""The payee a payment actually paid, and the flag a re-payment to another one raises.

Decisions §383. A vendor merge moves ``paid`` invoices to the canonical vendor
(their payee is history, §379), and ``POST /api/payments/{id}/void`` sends a
paid invoice back to ``approved``. The re-payment then reads the bank details of
whichever vendor the invoice names NOW, so a paid invoice merged onto another
vendor and then voided was re-paid to the canonical's account with nothing to
compare against: ``Payment`` recorded no payee.

Every payment now records, at the moment it releases money, who it paid:

* ``payee_vendor_id`` — the invoice's vendor at that moment;
* ``payee_bank_fingerprint`` — a keyed HMAC over that vendor's whole
  ``bank_details`` blob, never the numbers themselves (the PII invariant),
  tagged with the id of the key that made it (``v2:<kid>:<hmac>``). The key is
  its own secret, ``FEOH_PAYEE_FINGERPRINT_KEY``, and rotates through a keyring
  (``FEOH_PAYEE_FINGERPRINT_PREVIOUS_KEYS``) so a stamp made under a retired key
  is still compared under that key: rotating it never flags an honest void.
  ``NULL`` on a stamped payment means no key was configured (local dev only; a
  deployed environment refuses to boot without one), and its void flags;
* ``payee_recorded_at`` — when. A payment with no stamp never released money
  after migration 0128, or predates it.

Written by :func:`stamp_payee` at every step that releases money: the rail /
card dispatch (``api/payments._execute_single_payment``), the NACHA export (the
customer's upload is the money moving), and a payment recorded as paid outside
FeohLedger (``services/external_payment._complete``, which serves the user's
record, the run-level record and the ERP-reported payment). An entry of an
exported NACHA file later recorded as paid keeps the stamp its file carried.
The dispatch stamps only once every refusal and hold has passed, immediately
before the processor or card call, so a stamp means money was instructed.

The void asks :func:`flag_repayment_payee_change`: when the invoice's vendor, or
that vendor's bank details, are no longer what the voided payment paid, it opens
the payment- and ERP-push-blocking ``fraud_flag`` with the code
``payee_changed_since_paid``, stamped with the person whose act changed the
payee — one flag per person, so none of them can clear their own
(``exception_lifecycle.segregation_refusal``). A merge records on the retired
vendor where it went and who merged it (``Vendor.merged_into_vendor_id``), so the
void names every steward on the way from the paid vendor to the current one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.models.workflow import AuditLog

logger = logging.getLogger(__name__)

#: Finding codes (``invoice_warning_catalog``, ``exception.`` prefix) of the two
#: re-payment flags. Both are ``fraud_flag`` rows.
PAYEE_CHANGED_SINCE_PAID = "payee_changed_since_paid"
PAYEE_UNRECORDED_AFTER_MERGE = "payee_unrecorded_after_merge"

#: The scheme tag a fingerprint carries, so a later scheme never compares equal
#: to this one by accident: an unknown tag reads as "changed", which flags. `v1`
#: (keyed by a derivation of FEOH_SECRET_KEY) existed only on the unmerged branch
#: that introduced the stamp and is not read (decisions §383).
_FINGERPRINT_VERSION = "v2"

#: The audit action a vendor merge writes (``api/enrichment``), naming the
#: canonical vendor as its entity.
_VENDOR_MERGED = "vendor.merged"


def fingerprint_key_id(key: str) -> str:
    """The id a fingerprint carries for the key that made it.

    Derived from the key, not configured beside it, so a key and its id can
    never be paired wrongly: an operator cannot reuse an id for a new key and
    make old stamps compare under the wrong one. 32 bits of a domain-separated
    hash of a 256-bit random key say nothing about the key.
    """
    return hashlib.sha256(b"feoh-payee-fingerprint-kid\x00" + key.encode()).hexdigest()[:8]


def _current_key() -> str:
    return settings.payee_fingerprint_key.strip()


def _keyring() -> dict[str, str]:
    """Every key a stored fingerprint may have been made under, by id: the
    current key, then each retired one. The current key wins an id collision."""
    ring: dict[str, str] = {}
    previous = settings.payee_fingerprint_previous_keys.split(",")
    for key in [_current_key(), *(k.strip() for k in previous)]:
        if key:
            ring.setdefault(fingerprint_key_id(key), key)
    return ring


def _digest(key: str, bank_details: dict | None, organization_id: uuid.UUID) -> str:
    canonical = json.dumps(bank_details or None, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(
        key.encode(), f"{organization_id}\x00{canonical}".encode(), hashlib.sha256
    ).hexdigest()


def bank_fingerprint(bank_details: dict | None, *, organization_id: uuid.UUID) -> str | None:
    """A keyed, non-reversible fingerprint of a vendor's ``bank_details``.

    The whole blob, canonicalised (sorted keys, no whitespace), because the
    bank-change dual control treats any change to it as a bank change and a key
    allowlist would miss the next key that steers money (``counterparty_id``
    and a cheque's ``mailing_address`` already do). Keyed, because an unkeyed
    hash of a routing number and an account number is small enough to brute
    force. The tenant is in the message, so one account paid by two tenants
    does not fingerprint the same. No bank details at all (``None`` or ``{}``)
    is a value too, and fingerprints as one.

    ``None`` when ``FEOH_PAYEE_FINGERPRINT_KEY`` is empty: there is no fallback
    key, and a fingerprint nobody can recompute is worth nothing.
    """
    key = _current_key()
    if not key:
        return None
    digest = _digest(key, bank_details, organization_id)
    return f"{_FINGERPRINT_VERSION}:{fingerprint_key_id(key)}:{digest}"


def fingerprint_matches(
    stored: str | None, bank_details: dict | None, *, organization_id: uuid.UUID
) -> bool:
    """Does ``stored`` fingerprint exactly these ``bank_details``?

    Recomputed under the key the stamp names, current or retired, so a key
    rotation never reads as a bank change. Everything that cannot be
    recomputed answers ``False``, which flags: no fingerprint (no key when the
    payment was stamped), another scheme, or a key id no longer in the keyring
    (a retired key dropped too early).
    """
    if not stored:
        return False
    version, _, rest = stored.partition(":")
    kid, _, digest = rest.partition(":")
    if version != _FINGERPRINT_VERSION or not kid or not digest:
        return False
    key = _keyring().get(kid)
    if key is None:
        # The id names a key, never the key; nothing here is PII.
        logger.warning(
            "payee fingerprint key %s is not in FEOH_PAYEE_FINGERPRINT_KEY or "
            "FEOH_PAYEE_FINGERPRINT_PREVIOUS_KEYS; the void flags it as a payee change",
            kid,
        )
        return False
    return hmac.compare_digest(_digest(key, bank_details, organization_id), digest)


def stamp_payee(
    payment: Payment,
    *,
    vendor_id: uuid.UUID | None,
    bank_details: dict | None,
    organization_id: uuid.UUID,
    at: datetime,
) -> None:
    """Record on ``payment`` the payee it is releasing money to, as of ``at``."""
    payment.payee_vendor_id = vendor_id
    payment.payee_bank_fingerprint = bank_fingerprint(bank_details, organization_id=organization_id)
    payment.payee_recorded_at = at
    if payment.payee_bank_fingerprint is None:
        logger.warning(
            "FEOH_PAYEE_FINGERPRINT_KEY is empty: payment %s records its payee vendor but "
            "no bank fingerprint, so a void of it raises payee_changed_since_paid",
            payment.id,
        )


async def stamp_payee_from_vendor(
    db: AsyncSession,
    payment: Payment,
    *,
    invoice: Invoice,
    organization_id: uuid.UUID,
    at: datetime,
) -> None:
    """:func:`stamp_payee` with the invoice's vendor's bank details read here."""
    bank = await _bank_details(db, invoice.vendor_id)
    stamp_payee(
        payment,
        vendor_id=invoice.vendor_id,
        bank_details=bank,
        organization_id=organization_id,
        at=at,
    )


async def _bank_details(db: AsyncSession, vendor_id: uuid.UUID | None) -> dict | None:
    if vendor_id is None:
        return None
    return (
        await db.execute(select(Vendor.bank_details).where(Vendor.id == vendor_id))
    ).scalar_one_or_none()


#: A merge chain longer than this is not followed further (each hop is one
#: retired duplicate; a real chain is one or two).
_MAX_MERGE_HOPS = 16


async def _merge_hop(
    db: AsyncSession, vendor_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID | None] | None:
    """Where ``vendor_id`` was merged, and by whom, or ``None`` if it never was.

    The vendor's own record first (``Vendor.merged_into_vendor_id``, written in
    the merge's transaction since migration 0128). A merge from before that has
    only its ``vendor.merged`` audit row: the EARLIEST one naming this vendor a
    duplicate, since a re-run of the same merge writes a row too and must not
    re-attribute it.
    """
    row = (
        await db.execute(
            select(Vendor.merged_into_vendor_id, Vendor.merged_by_user_id).where(
                Vendor.id == vendor_id
            )
        )
    ).one_or_none()
    if row is not None and row.merged_into_vendor_id is not None:
        return row.merged_into_vendor_id, row.merged_by_user_id
    audit = (
        await db.execute(
            select(AuditLog.entity_id, AuditLog.actor_id)
            .where(
                AuditLog.action == _VENDOR_MERGED,
                AuditLog.entity_type == "vendor",
                AuditLog.details.contains({"duplicate_vendor_ids": [str(vendor_id)]}),
            )
            .order_by(AuditLog.created_at.asc())
            .limit(1)
        )
    ).one_or_none()
    if audit is None or audit.entity_id is None:
        return None
    return audit.entity_id, audit.actor_id


async def _merge_actors_between(
    db: AsyncSession, paid: uuid.UUID | None, current: uuid.UUID | None
) -> list[uuid.UUID | None]:
    """Everyone whose merge carried an invoice from ``paid`` to ``current``.

    Walks the merge pointers hop by hop (D → B by one steward, B → C by
    another), so every person who moved the payee is named, not whoever last
    merged something into ``current``.
    """
    actors: list[uuid.UUID | None] = []
    seen: set[uuid.UUID] = set()
    at = paid
    while at is not None and at != current and at not in seen and len(seen) < _MAX_MERGE_HOPS:
        seen.add(at)
        hop = await _merge_hop(db, at)
        if hop is None:
            break
        at, actor = hop
        actors.append(actor)
    return actors


async def _merge_actors_into(
    db: AsyncSession, vendor_id: uuid.UUID | None, *, since: datetime
) -> list[uuid.UUID | None]:
    """Everyone who merged a vendor INTO ``vendor_id`` after ``since``: from the
    retired vendors' own records, and from the audit rows of merges older than
    those records that moved anything (a no-op re-run moved nothing)."""
    if vendor_id is None:
        return []
    recorded = (
        await db.execute(
            select(Vendor.merged_by_user_id).where(
                Vendor.merged_into_vendor_id == vendor_id, Vendor.merged_at > since
            )
        )
    ).scalars()
    audited = (
        await db.execute(
            select(AuditLog.actor_id).where(
                AuditLog.action == _VENDOR_MERGED,
                AuditLog.entity_type == "vendor",
                AuditLog.entity_id == vendor_id,
                AuditLog.created_at > since,
                func.coalesce(AuditLog.details["total_reassigned"].as_integer(), 0) > 0,
            )
        )
    ).scalars()
    return [*recorded, *audited]


async def bank_change_approvers_since(
    db: AsyncSession, vendor_id: uuid.UUID | None, *, since: datetime
) -> list[uuid.UUID | None]:
    """Every approver of a bank-detail change on ``vendor_id`` after ``since``."""
    if vendor_id is None:
        return []
    return list(
        (
            await db.execute(
                select(VendorChangeRequest.reviewed_by_user_id)
                .where(
                    VendorChangeRequest.vendor_id == vendor_id,
                    VendorChangeRequest.change_type == "bank_details",
                    VendorChangeRequest.status == "approved",
                    VendorChangeRequest.reviewed_at > since,
                )
                .order_by(VendorChangeRequest.reviewed_at)
            )
        ).scalars()
    )


def _distinct(actors: list[uuid.UUID | None]) -> list[uuid.UUID | None]:
    return list(dict.fromkeys(actors))


async def payee_changes_since(
    db: AsyncSession,
    *,
    invoice: Invoice,
    payment: Payment,
    current_bank: dict | None,
    organization_id: uuid.UUID,
) -> tuple[str, list[uuid.UUID | None]] | None:
    """Would re-paying ``invoice`` pay someone other than ``payment`` did?

    ``None`` when it would not (or cannot be told, below). Otherwise the finding
    code and the people whose acts changed the payee — one flag is raised per
    person, each refused the clearing of their own (``current_bank`` is the
    invoice's vendor's details, read under the caller's lock):

    * **stamped**, and the invoice's vendor is not the one paid → only a merge
      moves a paid invoice's vendor (an approved one is financially locked), so
      the raisers are the stewards of every merge on the way from the paid
      vendor to the current one;
    * **stamped**, same vendor, and the fingerprint does not match the current
      details under the key it names → the approvers of the bank changes since
      the stamp. A fingerprint that cannot be recomputed (none stored because
      no key was configured, or a key id no longer in the keyring) does not
      match: a stamp says money moved, so the comparison fails closed;
    * **not stamped** (paid before migration 0128): nothing honest to compare,
      and nothing is backfilled (§141) — the current details are exactly what
      cannot stand in for the paid ones. The one fact on record is a merge into
      the invoice's vendor after the payment was booked: then the invoice may
      have moved, so it flags ``payee_unrecorded_after_merge``, raised by the
      stewards of those merges. A bank change on the same vendor is the
      pre-existing ``reflag_voided_invoice_for_bank_change``'s case.

    When no person can be named (bank details cleared by an erasure, a merge
    with no record of its steward) the raiser is ``None``: the flag still
    blocks, and anyone outside the invoice's implicated set may clear it.
    """
    if payment.payee_recorded_at is None:
        actors = await _merge_actors_into(db, invoice.vendor_id, since=payment.created_at)
        if not actors:
            return None
        return PAYEE_UNRECORDED_AFTER_MERGE, _distinct(actors)

    since = payment.payee_recorded_at
    if payment.payee_vendor_id != invoice.vendor_id:
        actors = await _merge_actors_between(db, payment.payee_vendor_id, invoice.vendor_id)
        return PAYEE_CHANGED_SINCE_PAID, _distinct(actors) or [None]

    if fingerprint_matches(
        payment.payee_bank_fingerprint, current_bank, organization_id=organization_id
    ):
        return None
    approvers = await bank_change_approvers_since(db, invoice.vendor_id, since=since)
    return PAYEE_CHANGED_SINCE_PAID, _distinct(approvers) or [None]


async def flag_repayment_payee_change(
    db: AsyncSession,
    *,
    invoice: Invoice,
    payment: Payment,
    organization_id: uuid.UUID,
) -> str | None:
    """Called by the void once it has sent ``invoice`` back to ``approved``.

    Reads the invoice's vendor ``FOR SHARE`` first. A bank-change approval
    updates that row and then flags the vendor's payable invoices, which this
    one is not until the void commits; without the lock each side would read
    the other's state from before it, and neither would flag. With it, whichever
    runs second sees the first: the void reads the new details, or the approval
    sees an `approved` invoice.

    Opens the ``fraud_flag`` :func:`payee_changes_since` finds, once per person
    who changed the payee. For a payment with no stamp, also runs the
    bank-change re-flag that predates the stamp (decisions §286); a stamped
    payment needs it not, since its fingerprint already answers "did the bank
    details change" for every route a change can take. Returns the code raised,
    if any.
    """
    from app.api.vendors import flag_invoice_for_bank_change, reflag_voided_invoice_for_bank_change

    current_bank: dict | None = None
    if invoice.vendor_id is not None:
        current_bank = (
            await db.execute(
                select(Vendor.bank_details)
                .where(Vendor.id == invoice.vendor_id)
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()

    if payment.payee_recorded_at is None:
        await reflag_voided_invoice_for_bank_change(
            db, invoice=invoice, paid_since=payment.created_at
        )
    change = await payee_changes_since(
        db,
        invoice=invoice,
        payment=payment,
        current_bank=current_bank,
        organization_id=organization_id,
    )
    if change is None:
        return None
    code, raisers = change
    for raiser in raisers:
        await flag_invoice_for_bank_change(db, invoice=invoice, actor_id=raiser, code=code)
    return code
