"""Adjudicated sanctions false positives (decisions §477).

A screen result a human has found is NOT this vendor is recorded once, with
who, when, why and WHICH result (``SanctionsAdjudication``). Every later screen
is compared to it by :func:`screen_fingerprint`:

* the stored screen (``vendor_screening.screen_vendor_record``) records the
  covering adjudication on ``vendors.screening_adjudication_id`` and, on a
  covered ``match``, does not re-set the payment block or revoke cards;
* the live pre-payment screen (``compliance.check_payment_compliance``, run by
  dispatch and ``/compliance/release``) treats a covered result as clear;
* the ERP push and the run gate read ``payment_runs.VendorPayability``, which
  lifts the screening hold only while the stored screen is covered.

A different or new hit has a different fingerprint and holds again, and so does
the same hit on different screened inputs: every screen records a hash of what
it sent the provider (:func:`screened_inputs_hash`) and the fingerprint
includes it, so a name, tax-id, bank-country or owner edit, or any applied bank
change, owes a fresh judgement. The sticky ``payments_blocked`` flag a
``match`` set is NOT lifted here: it is ``vendor.block``'s to lift
(``POST /api/vendors/{id}/unblock``), which refuses the adjudicator
(:func:`unblock_refusal`), so clearing a ``match`` takes two people.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.refusals import ActionRefusal
from app.models.sanctions_adjudication import SanctionsAdjudication
from app.models.sanctions_check import SanctionsCheck
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.services.audit_dispatch import dispatch_audit

#: Results an adjudication can clear. `clear` needs none; anything else is a
#: provider answer we do not understand and stays a hold (compliance.py).
ADJUDICABLE_RESULTS: frozenset[str] = frozenset({"match", "review_required"})

#: Stored screening statuses (`vendors.screening_status`) an adjudication covers.
ADJUDICABLE_SCREENING_STATUSES: frozenset[str] = frozenset({"match", "review"})

#: Top-level `raw_response` keys that change between two screens of the same
#: hit (request ids, timestamps) and so are left out of the fingerprint. A
#: provider adapter that can name the matched entry and list version itself
#: should put them under `match_fingerprint` instead, which is used verbatim.
VOLATILE_RAW_KEYS: frozenset[str] = frozenset(
    {
        "request_id",
        "requestId",
        "search_id",
        "searchId",
        "timestamp",
        "searched_at",
        "checked_at",
        "correlation_id",
    }
)

#: The fingerprint scheme's version; a change to the basis bumps it, which
#: (deliberately) makes every existing adjudication stop covering.
FINGERPRINT_VERSION = 2

#: The `raw_response` key each screen stores :func:`screened_inputs_hash` under.
#: It is ours, not the provider's, so it is folded into the fingerprint as its
#: own term rather than as part of the provider's entry.
SCREENED_INPUTS_KEY = "screened_inputs"

# Coded refusals (`frontend/src/lib/api/codedRefusals.ts`).
SANCTIONS_ADJUDICATION_NOT_FLAGGED = "sanctions_adjudication_not_flagged"
SANCTIONS_ADJUDICATION_ALREADY_CLEARED = "sanctions_adjudication_already_cleared"
SANCTIONS_ADJUDICATION_NO_SCREEN = "sanctions_adjudication_no_screen"
SANCTIONS_ADJUDICATION_SEGREGATION = "sanctions_adjudication_segregation"
SANCTIONS_ADJUDICATION_NOT_ACTIVE = "sanctions_adjudication_not_active"
SANCTIONS_ADJUDICATION_STALE_SCREEN = "sanctions_adjudication_stale_screen"
VENDOR_UNBLOCK_ADJUDICATOR = "vendor_unblock_adjudicator"

ADJUDICATED_ACTION = "vendor.sanctions_adjudicated"
REVOKED_ACTION = "vendor.sanctions_adjudication_revoked"
CLEARANCE_CHANGED_ACTION = "vendor.screening_clearance_changed"

#: The clearance state names on the status-change audit row.
CLEARANCE_ADJUDICATED = "adjudicated_false_positive"


def screen_fingerprint(
    *, provider: str, result: str, matched_list: str | None, raw_response: dict | None
) -> str:
    """The identity of one screen result: sha256 hex over the provider, the
    verdict, the list name and the matched entry.

    The entry is the adapter's own ``raw_response["match_fingerprint"]`` when it
    sets one (entry id + list version), else the raw response minus
    :data:`VOLATILE_RAW_KEYS`. Only the hash is stored: the raw response can
    carry match PII (invariant #7) and never leaves ``sanctions_checks``.
    """
    raw = raw_response if isinstance(raw_response, dict) else {}
    declared = raw.get("match_fingerprint")
    if isinstance(declared, str) and declared:
        basis: object = declared
    else:
        basis = {
            k: v for k, v in raw.items() if k not in VOLATILE_RAW_KEYS and k != SCREENED_INPUTS_KEY
        }
    payload = json.dumps(
        {
            "v": FINGERPRINT_VERSION,
            "provider": provider,
            "result": result,
            "matched_list": matched_list,
            "entry": basis,
            # What was screened (decisions §477): the same hit on another name,
            # tax id, bank country, owner list or bank account is a new result.
            "inputs": raw.get(SCREENED_INPUTS_KEY),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def check_fingerprint(check: SanctionsCheck) -> str:
    return screen_fingerprint(
        provider=check.provider,
        result=check.result,
        matched_list=check.matched_list,
        raw_response=check.raw_response,
    )


async def _latest_applied_bank_change(
    db: AsyncSession, vendor_id: uuid.UUID
) -> VendorChangeRequest | None:
    return (
        await db.execute(
            select(VendorChangeRequest)
            .where(
                VendorChangeRequest.vendor_id == vendor_id,
                VendorChangeRequest.change_type == "bank_details",
                VendorChangeRequest.status == "approved",
            )
            .order_by(
                VendorChangeRequest.reviewed_at.desc().nulls_last(),
                VendorChangeRequest.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def screened_inputs_hash(db: AsyncSession, vendor: Vendor) -> str:
    """sha256 hex of what a screen of ``vendor`` sends the provider (name, tax
    id, bank country, beneficial owners) plus the id of the bank change that put
    its current account in place. Stored under :data:`SCREENED_INPUTS_KEY` on
    every screen and folded into the fingerprint, so an adjudication covers the
    result only for the inputs the reviewer saw screened. The bank change is
    named by its id, never its account number (invariant #7)."""
    blob = vendor.beneficial_owner_data or {}
    owners = blob.get("owners") if isinstance(blob, dict) else None
    country = (vendor.bank_details or {}).get("country") if vendor.bank_details else None
    bank_change = await _latest_applied_bank_change(db, vendor.id)
    payload = json.dumps(
        {
            "name": vendor.name,
            "tax_id": vendor.tax_id,
            "country": country,
            "owners": owners if isinstance(owners, list) else None,
            "bank_change": str(bank_change.id) if bank_change is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def covering_adjudication(
    db: AsyncSession,
    *,
    vendor_id: uuid.UUID,
    provider: str,
    result: str,
    fingerprint: str,
    pointer_only: bool = False,
    pointer: uuid.UUID | None = None,
) -> SanctionsAdjudication | None:
    """The active adjudication that clears this exact result, or None.

    ``pointer_only`` restricts the match to ``pointer`` (the vendor's
    ``screening_adjudication_id``): the live pre-payment screen honours only
    the adjudication the stored screen is shown under, never an older active
    row the queue does not lead with."""
    if result not in ADJUDICABLE_RESULTS:
        return None
    if pointer_only and pointer is None:
        return None
    query = select(SanctionsAdjudication).where(
        SanctionsAdjudication.vendor_id == vendor_id,
        SanctionsAdjudication.provider == provider,
        SanctionsAdjudication.fingerprint == fingerprint,
        SanctionsAdjudication.revoked_at.is_(None),
    )
    if pointer_only:
        query = query.where(SanctionsAdjudication.id == pointer)
    return (await db.execute(query)).scalar_one_or_none()


async def latest_stored_screen(db: AsyncSession, vendor_id: uuid.UUID) -> SanctionsCheck | None:
    """The screen `vendors.screening_status` reflects: the newest trail row
    written by `screen_vendor_record` (every check type but `pre_payment`,
    which the dispatch gate writes without updating the vendor)."""
    return (
        await db.execute(
            select(SanctionsCheck)
            .where(
                SanctionsCheck.vendor_id == vendor_id,
                SanctionsCheck.check_type != "pre_payment",
            )
            .order_by(SanctionsCheck.checked_at.desc(), SanctionsCheck.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def active_adjudication(db: AsyncSession, vendor: Vendor) -> SanctionsAdjudication | None:
    if vendor.screening_adjudication_id is None:
        return None
    adj = await db.get(SanctionsAdjudication, vendor.screening_adjudication_id)
    if adj is None or adj.revoked_at is not None:
        return None
    return adj


async def active_adjudications(
    db: AsyncSession, vendor_id: uuid.UUID
) -> list[SanctionsAdjudication]:
    """Every unrevoked adjudication of the vendor, newest first. More than one
    can stand (one per adjudicated result); the queue lists them all and revoke
    ends them all, so none can keep clearing a later screen unseen."""
    return list(
        (
            await db.execute(
                select(SanctionsAdjudication)
                .where(
                    SanctionsAdjudication.vendor_id == vendor_id,
                    SanctionsAdjudication.revoked_at.is_(None),
                )
                .order_by(
                    SanctionsAdjudication.adjudicated_at.desc(), SanctionsAdjudication.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )


async def segregation_actors(db: AsyncSession, vendor: Vendor) -> set[uuid.UUID]:
    """Who may not adjudicate this vendor's screen: its onboarder, and the
    people behind its current bank details — the latest APPLIED bank change's
    requester, the AP user who provisioned the portal login that requested it,
    and its approver. Each put the payee or its account in place; clearing the
    sanctions hold on it is a second, independent judgement."""
    return _segregation_actors(vendor, await _latest_applied_bank_change(db, vendor.id))


def _segregation_actors(vendor: Vendor, last_change: VendorChangeRequest | None) -> set[uuid.UUID]:
    actors: set[uuid.UUID] = set()
    if vendor.onboarded_by_user_id is not None:
        actors.add(vendor.onboarded_by_user_id)
    if last_change is not None:
        for uid in (
            last_change.requested_by_user_id,
            last_change.requester_provisioned_by_user_id,
            last_change.reviewed_by_user_id,
        ):
            if uid is not None:
                actors.add(uid)
    return actors


@dataclass
class AdjudicationContext:
    """Everything the adjudicate / revoke gates read about one vendor, loaded
    once. The endpoints load it per vendor (:func:`load_context`); the review
    queue loads a whole page in four queries (:func:`load_contexts`) instead of
    four per row. Both run the SAME gate functions over it, so the queue's
    verdicts are the endpoints' answers (issue #575)."""

    #: The adjudication covering the stored screen (the vendor's pointer).
    pointer: SanctionsAdjudication | None = None
    #: Every unrevoked adjudication, newest first.
    active: list[SanctionsAdjudication] = field(default_factory=list)
    #: :func:`latest_stored_screen`.
    latest: SanctionsCheck | None = None
    #: The latest applied bank change (segregation).
    last_bank_change: VendorChangeRequest | None = None


def _newest_first(rows: list[SanctionsAdjudication]) -> list[SanctionsAdjudication]:
    return sorted(rows, key=lambda a: (a.adjudicated_at, a.id), reverse=True)


async def load_contexts(
    db: AsyncSession, vendors: Sequence[Vendor]
) -> dict[uuid.UUID, AdjudicationContext]:
    """:class:`AdjudicationContext` for every vendor, one query per input."""
    ids = [v.id for v in vendors]
    out = {vid: AdjudicationContext() for vid in ids}
    if not ids:
        return out
    active_rows = (
        (
            await db.execute(
                select(SanctionsAdjudication).where(
                    SanctionsAdjudication.vendor_id.in_(ids),
                    SanctionsAdjudication.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for adj in active_rows:
        out[adj.vendor_id].active.append(adj)
    for v in vendors:
        ctx = out[v.id]
        ctx.active = _newest_first(ctx.active)
        ctx.pointer = next((a for a in ctx.active if a.id == v.screening_adjudication_id), None)
    latest_rows = (
        (
            await db.execute(
                select(SanctionsCheck)
                .where(
                    SanctionsCheck.vendor_id.in_(ids),
                    SanctionsCheck.check_type != "pre_payment",
                )
                .distinct(SanctionsCheck.vendor_id)
                .order_by(
                    SanctionsCheck.vendor_id,
                    SanctionsCheck.checked_at.desc(),
                    SanctionsCheck.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    for check in latest_rows:
        out[check.vendor_id].latest = check
    change_rows = (
        (
            await db.execute(
                select(VendorChangeRequest)
                .where(
                    VendorChangeRequest.vendor_id.in_(ids),
                    VendorChangeRequest.change_type == "bank_details",
                    VendorChangeRequest.status == "approved",
                )
                .distinct(VendorChangeRequest.vendor_id)
                .order_by(
                    VendorChangeRequest.vendor_id,
                    VendorChangeRequest.reviewed_at.desc().nulls_last(),
                    VendorChangeRequest.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    for change in change_rows:
        out[change.vendor_id].last_bank_change = change
    return out


async def load_context(db: AsyncSession, vendor: Vendor) -> AdjudicationContext:
    return (await load_contexts(db, [vendor]))[vendor.id]


async def adjudicate_refusal(
    db: AsyncSession,
    vendor: Vendor,
    actor_id: uuid.UUID,
    sanctions_check_id: uuid.UUID | None = None,
    ctx: AdjudicationContext | None = None,
) -> ActionRefusal | None:
    """Why ``POST /api/vendors/{id}/screening/adjudicate`` would refuse a
    ``vendor.sanctions.adjudicate`` holder, or None. The endpoint raises it and
    the review queue ships it as ``adjudicate_refusal`` (issue #575).

    ``sanctions_check_id`` is the screen the reviewer saw (the queue row's
    ``latest_check_id``). The endpoint passes it under the vendor row lock and
    refuses when it is no longer :func:`latest_stored_screen`, so the stamp
    never covers a result nobody looked at. The queue omits it: it reads the
    verdict for the row it is about to ship, from its page-wide ``ctx``."""
    if ctx is None:
        ctx = await load_context(db, vendor)
    if vendor.screening_status not in ADJUDICABLE_SCREENING_STATUSES:
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_NOT_FLAGGED,
            "Only a vendor whose screen is a match or needs review can be adjudicated.",
            status.HTTP_409_CONFLICT,
            params={"screening_status": vendor.screening_status},
        )
    if ctx.pointer is not None:
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_ALREADY_CLEARED,
            "This screen result is already adjudicated as a false positive.",
            status.HTTP_409_CONFLICT,
        )
    latest = ctx.latest
    if latest is None:
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_NO_SCREEN,
            "There is no recorded screen to adjudicate. Re-screen the vendor first.",
            status.HTTP_409_CONFLICT,
        )
    if sanctions_check_id is not None and latest.id != sanctions_check_id:
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_STALE_SCREEN,
            "The vendor was screened again after you opened it. Reload and review "
            "the latest result before adjudicating.",
            status.HTTP_409_CONFLICT,
        )
    if actor_id in _segregation_actors(vendor, ctx.last_bank_change):
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_SEGREGATION,
            "You onboarded this vendor or put its current bank details in place, "
            "so another reviewer must adjudicate its screen.",
            status.HTTP_403_FORBIDDEN,
        )
    return None


async def revoke_refusal(
    db: AsyncSession, vendor: Vendor, ctx: AdjudicationContext | None = None
) -> ActionRefusal | None:
    """Why the revoke endpoint would refuse, or None. Revoking only ever
    tightens the gates, so it carries no segregation rule. Any active row
    counts, not only the one the stored screen is shown under."""
    if ctx is None:
        ctx = await load_context(db, vendor)
    if not ctx.active:
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_NOT_ACTIVE,
            "This vendor's screen has no active false-positive adjudication.",
            status.HTTP_409_CONFLICT,
        )
    return None


async def unblock_refusal(
    db: AsyncSession, vendor: Vendor, actor_id: uuid.UUID
) -> ActionRefusal | None:
    """Why ``POST /api/vendors/{id}/unblock`` refuses this caller, or None.

    While the stored screen is a ``match`` (so the sticky block came from a
    screen) and an adjudication covers it, its adjudicator may not also lift the
    block: clearing a match takes two people, and ``admin`` holds both
    permissions (decisions §477)."""
    if vendor.screening_status != "match":
        return None
    adj = await active_adjudication(db, vendor)
    if adj is None or adj.adjudicated_by_user_id != actor_id:
        return None
    return ActionRefusal(
        VENDOR_UNBLOCK_ADJUDICATOR,
        "You adjudicated this vendor's sanctions match, so another person must "
        "lift its payment block.",
        status.HTTP_403_FORBIDDEN,
    )


def _audit_details(adj: SanctionsAdjudication) -> dict:
    # PII-free: the list NAME, provider, verdict and hash; never match details.
    return {
        "adjudication_id": str(adj.id),
        "sanctions_check_id": str(adj.sanctions_check_id),
        "provider": adj.provider,
        "result": adj.result,
        "matched_list": adj.matched_list,
        "fingerprint": adj.fingerprint,
    }


async def adjudicate(
    db: AsyncSession,
    *,
    vendor: Vendor,
    actor_id: uuid.UUID,
    organization_id: uuid.UUID,
    reason: str,
) -> SanctionsAdjudication:
    """Stamp the vendor's stored screen as a false positive. The caller holds
    the vendor row lock, has passed :func:`adjudicate_refusal` with the
    reviewer's ``sanctions_check_id`` (so the screen stamped here is the one
    they saw) and commits."""
    check = await latest_stored_screen(db, vendor.id)
    if check is None:  # adjudicate_refusal refuses this first
        raise RuntimeError("no stored screen to adjudicate")
    adj = SanctionsAdjudication(
        organization_id=organization_id,
        vendor_id=vendor.id,
        sanctions_check_id=check.id,
        provider=check.provider,
        result=check.result,
        matched_list=check.matched_list,
        fingerprint=check_fingerprint(check),
        reason=reason,
        adjudicated_by_user_id=actor_id,
        adjudicated_at=datetime.now(UTC),
    )
    db.add(adj)
    await db.flush()
    vendor.screening_adjudication_id = adj.id
    corr = uuid.uuid4()
    await dispatch_audit(
        db,
        correlation_id=corr,
        organization_id=organization_id,
        actor_id=actor_id,
        action=ADJUDICATED_ACTION,
        entity_type="vendor",
        entity_id=vendor.id,
        details={**_audit_details(adj), "reason": reason},
    )
    await dispatch_audit(
        db,
        correlation_id=corr,
        organization_id=organization_id,
        actor_id=actor_id,
        action=CLEARANCE_CHANGED_ACTION,
        entity_type="vendor",
        entity_id=vendor.id,
        details={"from": vendor.screening_status, "to": CLEARANCE_ADJUDICATED},
    )
    return adj


async def revoke(
    db: AsyncSession,
    *,
    vendor: Vendor,
    actor_id: uuid.UUID,
    organization_id: uuid.UUID,
    org_settings: dict | None,
    reason: str,
) -> list[SanctionsAdjudication]:
    """End EVERY active adjudication of the vendor. The caller holds the vendor
    row lock, has passed :func:`revoke_refusal` and commits.

    The gates hold again at once. A revoked ``match`` covering the stored
    screen gets back what an uncovered match does, in this transaction: the
    sticky block and the closing of the vendor's live cards (a card issued
    while the stamp stood would otherwise outlive it)."""
    from app.services.vendor_screening import apply_sanctions_match

    rows = await active_adjudications(db, vendor.id)
    if not rows:  # revoke_refusal refuses this first
        raise RuntimeError("no active adjudication to revoke")
    pointer = vendor.screening_adjudication_id
    covered_stored_screen = any(adj.id == pointer for adj in rows)
    now = datetime.now(UTC)
    corr = uuid.uuid4()
    for adj in rows:
        adj.revoked_at = now
        adj.revoked_by_user_id = actor_id
        adj.revoke_reason = reason
        await dispatch_audit(
            db,
            correlation_id=corr,
            organization_id=organization_id,
            actor_id=actor_id,
            action=REVOKED_ACTION,
            entity_type="vendor",
            entity_id=vendor.id,
            details={**_audit_details(adj), "reason": reason},
        )
    vendor.screening_adjudication_id = None
    if covered_stored_screen:
        await dispatch_audit(
            db,
            correlation_id=corr,
            organization_id=organization_id,
            actor_id=actor_id,
            action=CLEARANCE_CHANGED_ACTION,
            entity_type="vendor",
            entity_id=vendor.id,
            details={"from": CLEARANCE_ADJUDICATED, "to": vendor.screening_status},
        )
        stored = await latest_stored_screen(db, vendor.id)
        if vendor.screening_status == "match" and stored is not None and stored.result == "match":
            await apply_sanctions_match(
                db,
                vendor=vendor,
                organization_id=organization_id,
                org_settings=org_settings,
                actor_id=actor_id,
                matched_list=stored.matched_list,
                provider=stored.provider,
                trigger="vendor.sanctions_adjudication_revoked",
            )
    await db.flush()
    return rows
