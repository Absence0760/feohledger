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

A different or new hit has a different fingerprint and holds again. The sticky
``payments_blocked`` flag a ``match`` set is NOT lifted here: it is
``vendor.block``'s to lift (``POST /api/vendors/{id}/unblock``), so clearing a
``match`` takes two signatures by default.
"""

from __future__ import annotations

import hashlib
import json
import uuid
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
FINGERPRINT_VERSION = 1

# Coded refusals (`frontend/src/lib/api/codedRefusals.ts`).
SANCTIONS_ADJUDICATION_NOT_FLAGGED = "sanctions_adjudication_not_flagged"
SANCTIONS_ADJUDICATION_ALREADY_CLEARED = "sanctions_adjudication_already_cleared"
SANCTIONS_ADJUDICATION_NO_SCREEN = "sanctions_adjudication_no_screen"
SANCTIONS_ADJUDICATION_SEGREGATION = "sanctions_adjudication_segregation"
SANCTIONS_ADJUDICATION_NOT_ACTIVE = "sanctions_adjudication_not_active"

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
        basis = {k: v for k, v in raw.items() if k not in VOLATILE_RAW_KEYS}
    payload = json.dumps(
        {
            "v": FINGERPRINT_VERSION,
            "provider": provider,
            "result": result,
            "matched_list": matched_list,
            "entry": basis,
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


async def covering_adjudication(
    db: AsyncSession,
    *,
    vendor_id: uuid.UUID,
    provider: str,
    result: str,
    fingerprint: str,
) -> SanctionsAdjudication | None:
    """The active adjudication that clears this exact result, or None."""
    if result not in ADJUDICABLE_RESULTS:
        return None
    return (
        await db.execute(
            select(SanctionsAdjudication).where(
                SanctionsAdjudication.vendor_id == vendor_id,
                SanctionsAdjudication.provider == provider,
                SanctionsAdjudication.fingerprint == fingerprint,
                SanctionsAdjudication.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()


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


async def segregation_actors(db: AsyncSession, vendor: Vendor) -> set[uuid.UUID]:
    """Who may not adjudicate this vendor's screen: its onboarder, and the
    people behind its current bank details — the latest APPLIED bank change's
    requester, the AP user who provisioned the portal login that requested it,
    and its approver. Each put the payee or its account in place; clearing the
    sanctions hold on it is a second, independent judgement."""
    actors: set[uuid.UUID] = set()
    if vendor.onboarded_by_user_id is not None:
        actors.add(vendor.onboarded_by_user_id)
    last_change = (
        await db.execute(
            select(VendorChangeRequest)
            .where(
                VendorChangeRequest.vendor_id == vendor.id,
                VendorChangeRequest.change_type == "bank_details",
                VendorChangeRequest.status == "approved",
            )
            .order_by(VendorChangeRequest.reviewed_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_change is not None:
        for uid in (
            last_change.requested_by_user_id,
            last_change.requester_provisioned_by_user_id,
            last_change.reviewed_by_user_id,
        ):
            if uid is not None:
                actors.add(uid)
    return actors


async def adjudicate_refusal(
    db: AsyncSession, vendor: Vendor, actor_id: uuid.UUID
) -> ActionRefusal | None:
    """Why ``POST /api/vendors/{id}/screening/adjudicate`` would refuse a
    ``vendor.sanctions.adjudicate`` holder, or None. The endpoint raises it and
    the review queue ships it as ``adjudicate_refusal`` (issue #575)."""
    if vendor.screening_status not in ADJUDICABLE_SCREENING_STATUSES:
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_NOT_FLAGGED,
            "Only a vendor whose screen is a match or needs review can be adjudicated.",
            status.HTTP_409_CONFLICT,
            params={"screening_status": vendor.screening_status},
        )
    if await active_adjudication(db, vendor) is not None:
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_ALREADY_CLEARED,
            "This screen result is already adjudicated as a false positive.",
            status.HTTP_409_CONFLICT,
        )
    if await latest_stored_screen(db, vendor.id) is None:
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_NO_SCREEN,
            "There is no recorded screen to adjudicate. Re-screen the vendor first.",
            status.HTTP_409_CONFLICT,
        )
    if actor_id in await segregation_actors(db, vendor):
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_SEGREGATION,
            "You onboarded this vendor or put its current bank details in place, "
            "so another reviewer must adjudicate its screen.",
            status.HTTP_403_FORBIDDEN,
        )
    return None


async def revoke_refusal(db: AsyncSession, vendor: Vendor) -> ActionRefusal | None:
    """Why the revoke endpoint would refuse, or None. Revoking only ever
    tightens the gates, so it carries no segregation rule."""
    if await active_adjudication(db, vendor) is None:
        return ActionRefusal(
            SANCTIONS_ADJUDICATION_NOT_ACTIVE,
            "This vendor's screen has no active false-positive adjudication.",
            status.HTTP_409_CONFLICT,
        )
    return None


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
    """Stamp the vendor's stored screen as a false positive. The caller has
    passed :func:`adjudicate_refusal` and commits."""
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
    reason: str,
) -> SanctionsAdjudication:
    """End the vendor's active adjudication. The caller has passed
    :func:`revoke_refusal` and commits. The gates hold again at once: the
    stored screen is uncovered, and the next live screen finds no match."""
    adj = await active_adjudication(db, vendor)
    if adj is None:  # revoke_refusal refuses this first
        raise RuntimeError("no active adjudication to revoke")
    adj.revoked_at = datetime.now(UTC)
    adj.revoked_by_user_id = actor_id
    adj.revoke_reason = reason
    vendor.screening_adjudication_id = None
    corr = uuid.uuid4()
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
    return adj
