"""Vendor sanctions screening — the shared screening primitive.

`screen_vendor_record` is the single choke point that screens one vendor
against the configured sanctions / PEP provider and records the result. It
is called from three places:

  * vendor create / update (`api/vendors.py`) — `check_type="initial"`,
  * the periodic re-screen sweep (`services/vendor_rescreen.py`) —
    `check_type="periodic"`,
  * a manual re-screen (`POST /api/vendors/{id}/screen`) —
    `check_type="manual"`.

(The orchestrator's own pre-payment screen stays in
`services/compliance.check_payment_compliance` with `check_type="pre_payment"`
— it has a different verdict contract, so it is deliberately not routed
through here.)

Every call:

  1. runs the adapter,
  2. appends a `sanctions_checks` row (the immutable screening trail —
     roadmap item "log all checks and results"),
  3. denormalises the outcome onto the `vendors` row
     (`screening_status` / `last_screened_at`), and
  4. on a `match`, sets the hard payment block (`payments_blocked`) so
     `check_payment_compliance` refuses any payment to the vendor before
     it reaches a payment adapter (roadmap item "flag and block payments
     to sanctioned entities"),
  5. writes a PII-free `vendor.screened` audit row.

It mutates the session (adds the trail row, updates the vendor, writes the
audit row) but does NOT commit — the caller owns the transaction boundary,
mirroring `check_payment_compliance`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sanctions_check import SanctionsCheck
from app.models.vendor import Vendor
from app.services.audit_dispatch import dispatch_audit
from app.services.sanctions_adapters import (
    SanctionsAdapter,
    ScreeningResult,
    UnknownSanctionsProviderError,
    get_sanctions_adapter,
)
from app.services.sanctions_categories import (
    has_adverse_media,
    merge_categories_into_raw_response,
)
from app.services.vendor_card_revocation import VendorCardRevocation, revoke_vendor_cards

logger = logging.getLogger(__name__)

# Adapter `result` → denormalised `vendors.screening_status`.
_STATUS_MAP = {"clear": "clear", "review_required": "review", "match": "match"}


@dataclass
class ScreenOutcome:
    """What a single screen produced. `blocked` is True iff this screen
    *newly* set (or kept) the vendor's payment block."""

    result: str  # 'clear' | 'review_required' | 'match'
    screening_status: str  # 'clear' | 'review' | 'match'
    risk_score: Decimal | None
    matched_list: str | None
    provider: str
    blocked: bool
    sanctions_check: SanctionsCheck
    # PII-free taxonomy of WHAT was hit (sanctions / pep / adverse_media /
    # high_risk_country). Defaulted so existing constructions keep working.
    categories: tuple[str, ...] = ()
    # What a `match` did to the vendor's live virtual cards
    # (`vendor_card_revocation.revoke_vendor_cards`). `None` when the screen
    # did not block the vendor.
    card_revocation: VendorCardRevocation | None = None

    @property
    def adverse_media(self) -> bool:
        """True when this screen included a negative-news hit."""
        return has_adverse_media(self.categories)


def _adapter_for(org_settings: dict | None) -> SanctionsAdapter:
    cfg = ((org_settings or {}).get("compliance") or {}).get("sanctions") or {}
    return get_sanctions_adapter(cfg)


def _beneficial_owners(vendor: Vendor) -> list[dict] | None:
    blob = vendor.beneficial_owner_data or {}
    owners = blob.get("owners") if isinstance(blob, dict) else None
    return owners if isinstance(owners, list) else None


async def screen_vendor_record(
    db: AsyncSession,
    *,
    vendor: Vendor,
    organization_id: uuid.UUID,
    org_settings: dict | None,
    check_type: str,
    actor_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID | None = None,
    sanctions_adapter: SanctionsAdapter | None = None,
) -> ScreenOutcome:
    """Screen `vendor`, append a trail row, denormalise + (un)block.

    `sanctions_adapter` is injectable so tests don't monkey-patch the
    dispatcher; production callers leave it None and it is resolved from
    `org_settings.compliance.sanctions`.
    """
    corr = correlation_id or uuid.uuid4()

    adapter: SanctionsAdapter | None = sanctions_adapter
    unknown_provider: str | None = None
    if adapter is None:
        try:
            adapter = _adapter_for(org_settings)
        except UnknownSanctionsProviderError as exc:
            # The org named a provider this deployment has no adapter for. The
            # dispatcher used to substitute `mock`, which clears every name
            # outside its fixture list — so a typo'd provider recorded a
            # `clear` screen for the whole vendor book. Record the screen as
            # `review_required` instead: the vendor lands on the screening
            # review queue (`GET /api/vendors/screening/review-queue`) and its
            # denormalised `screening_status` reads `review`, never `clear`.
            adapter = None
            unknown_provider = exc.provider

    if adapter is None:
        screening = ScreeningResult(
            # PII-free: the configured provider name is org config, and the
            # error type already length-bounds it. `SanctionsCheck.provider` is
            # String(50), so the sentinel carries the name in `raw_response`
            # rather than in the column.
            provider="unconfigured",
            result="review_required",
            matched_list="provider_not_configured",
            risk_score=None,
            raw_response={"error": "unknown_sanctions_provider", "provider": unknown_provider},
        )
    else:
        country = (vendor.bank_details or {}).get("country") if vendor.bank_details else None
        screening = await adapter.screen_vendor(
            vendor_name=vendor.name,
            vendor_country=country,
            vendor_tax_id=vendor.tax_id,
            beneficial_owners=_beneficial_owners(vendor),
        )

    row = SanctionsCheck(
        vendor_id=vendor.id,
        organization_id=organization_id,
        provider=screening.provider,
        check_type=check_type,
        result=screening.result,
        risk_score=screening.risk_score,
        matched_list=screening.matched_list,
        # The PII-free category taxonomy rides the row: `vendor_risk_scoring`
        # is compute-on-read off the latest `sanctions_checks` row and never
        # calls an adapter, so this is the only way an adverse-media hit can
        # reach the vendor's `risk_factors`.
        raw_response=merge_categories_into_raw_response(
            screening.raw_response, screening.categories
        ),
        correlation_id=corr,
    )
    db.add(row)
    await db.flush()

    now = datetime.now(UTC)
    vendor.screening_status = _STATUS_MAP.get(screening.result, "review")
    vendor.last_screened_at = now

    newly_blocked = False
    if screening.result == "match":
        if not vendor.payments_blocked:
            newly_blocked = True
        vendor.payments_blocked = True
        # No PII: the list NAME only, never match details.
        vendor.payments_blocked_reason = (
            f"sanctions match ({screening.matched_list or 'unspecified list'}) "
            f"via {screening.provider}"
        )
        vendor.payments_blocked_at = now

    # PII-free audit row: result + list NAME + provider only. Raw match
    # details live solely in the sanctions_checks JSONB (invariant #7).
    await dispatch_audit(
        db,
        correlation_id=corr,
        organization_id=organization_id,
        actor_id=actor_id,
        action="vendor.screened",
        entity_type="vendor",
        entity_id=vendor.id,
        details={
            "result": screening.result,
            "provider": screening.provider,
            "matched_list": screening.matched_list,
            # Fixed-vocabulary labels only (sanctions / pep / adverse_media /
            # high_risk_country) — no provider free text, so the trail can say
            # WHAT was hit without carrying a name or a date of birth.
            "categories": list(screening.categories),
            "check_type": check_type,
            "newly_blocked": newly_blocked,
        },
    )

    # A sanctioned vendor must not keep spending on a card minted before the
    # match: the payment block only stops the NEXT card. Runs on every match,
    # not only a new block, so a re-screen of an already-blocked vendor also
    # retries any card a previous attempt could not close.
    card_revocation: VendorCardRevocation | None = None
    if screening.result == "match":
        card_revocation = await revoke_vendor_cards(
            db,
            vendor=vendor,
            organization_id=organization_id,
            org_settings=org_settings,
            actor_id=actor_id,
            trigger="vendor.sanctions_match",
        )

    if screening.result in ("match", "review_required"):
        logger.info(
            "[vendor-screening] vendor=%s result=%s provider=%s check_type=%s",
            vendor.id,
            screening.result,
            screening.provider,
            check_type,
        )

    return ScreenOutcome(
        result=screening.result,
        screening_status=vendor.screening_status,
        risk_score=screening.risk_score,
        matched_list=screening.matched_list,
        provider=screening.provider,
        blocked=vendor.payments_blocked,
        sanctions_check=row,
        categories=screening.categories,
        card_revocation=card_revocation,
    )


async def screen_best_effort(
    db: AsyncSession,
    *,
    vendor: Vendor,
    org_settings: dict | None,
    org_id: uuid.UUID,
    check_type: str,
    actor_id: uuid.UUID | None,
) -> None:
    """Run a sanctions screen for ``vendor`` without ever jeopardising the
    surrounding vendor write.

    Screening is a best-effort side effect: if the configured provider is down
    or raises, the vendor create/update must still succeed. The screen runs
    inside a SAVEPOINT (``begin_nested``) so a mid-screen failure rolls back
    only the screen's partial mutations, leaving the vendor row intact, and the
    exception is logged + swallowed.

    Lives here rather than in ``api/vendors.py`` because it has two callers:
    the vendor router's create/update/bank-change paths, and the enrichment
    apply path (``POST /api/enrichment/vendors/{id}/apply``), which writes
    ``Vendor.name`` — an identity field — and so owes the same re-screen. A
    private copy in one router is how the enrichment path came to skip it.
    """
    from app.config import settings

    if not settings.vendor_screening_enabled:
        return
    try:
        async with db.begin_nested():
            await screen_vendor_record(
                db,
                vendor=vendor,
                organization_id=org_id,
                org_settings=org_settings,
                check_type=check_type,
                actor_id=actor_id,
            )
    except Exception as exc:  # noqa: BLE001
        # Log the exception type, never the message/traceback. A sanctions
        # adapter's error string could embed a vendor identifier; interpolating
        # `exc` (or exc_info=True) would push that into the log sink
        # (invariant #7).
        logger.warning(
            "Sanctions screen failed for vendor=%s (check_type=%s) — vendor write preserved: %s",
            vendor.id,
            check_type,
            exc.__class__.__name__,
        )
