"""Dynamic-discounting auto-capture sweep — auto-accept worthwhile open offers.

Every tick:

  1. Enumerate tenant DBs from the control plane.
  2. For each tenant, find ``offered`` :class:`~app.models.discount.DiscountOffer`
     rows that are still valid (not past ``valid_until``).
  3. For each, pick the best tier (highest discount percent) and compute its
     annualized ROI via :func:`app.services.discount_roi.compute_roi`, using the
     org's cost of capital
     (``Organization.settings.discounting.cost_of_capital_pct`` →
     ``settings.discount_cost_of_capital_pct`` fallback).
  4. If the annualized return clears ``settings.discount_auto_capture_roi_threshold``
     **and** the ROI is ``worthwhile``, AUTO-ACCEPT the offer: stamp
     ``accepted_tier`` / ``accepted_at`` and move ``status`` → ``accepted``, then
     write an append-only ``discount_offer.auto_accepted`` audit row.

Expiry is materialized here, but it does NOT live here
------------------------------------------------------
The sweep also flips a lapsed ``offered`` row to ``expired``. That is a
tidy-up, not the definition: this whole loop is behind
``FEOH_DISCOUNT_OPTIMIZATION_ENABLED``, which is **off by default**, and an
offer past its ``valid_until`` is off the table whether or not any background
process ran. Auto-capture is a money-adjacent *policy* decision (an ROI, a
threshold, a cost of capital) and belongs behind a switch; expiry is a
*correctness* fact about the calendar and must not.

They were sharing one switch, so with the sweep off a lapsed offer stayed
``offered`` forever, the dashboard's ``missed`` bucket stayed empty, and
``capture_rate_pct`` — captured / (captured + missed) — read ``100.00`` on one
capture out of ten offers.

The fix is to derive rather than to add a second always-on loop:
``discount_offers.effective_status`` (and its SQL mirror) is what every read
surface classifies by, so expiry is correct in local dev, in tests, and in any
deployment that never starts a sweep. The write below adds the durable row
state and the append-only ``discount_offer.expired`` audit entry on top of it.
Same shape as ``payment_runs.derive_run_status`` (``docs/decisions.md`` §41).

Money-path boundary (important)
-------------------------------
This sweep ONLY accepts an offer — it flags it for capture by transitioning
``offered`` → ``accepted``. It deliberately does **not** execute a payment or
create any ``Payment`` / ``PaymentRun`` row. Actually moving the money still
flows through the normal CFO-gated payment-run path, exactly like a manually
accepted offer. Keeping the auto-trigger payment-free is what prevents it from
silently moving money: the worst it can do is mark a high-ROI discount as
"accepted", which a human still has to fund.

Idempotency
-----------
Only ``offered`` offers are touched, and accepting one moves it out of
``offered``. So a re-run never double-accepts — the status guard is the dedupe
(the same role ``renewal_alert_sent_at`` plays in ``contract_renewal``).

Candidate offers are keyset-paginated per tenant
(``FEOH_DISCOUNT_OPTIMIZATION_BATCH_SIZE`` per page) rather than read in one
unbounded ``SELECT``; a *cap* is not available, because an offer skipped for a
below-threshold ROI stays ``offered``. See ``backend/docs/background-sweeps.md``
§ Locking.

Mirrors the ``contract_renewal`` pattern: long-lived asyncio task started in
``main.lifespan``, fresh per-tenant engine, one tenant's failure logged but
never halts the sweep. Within a tenant it mirrors ``vendor_rescreen``: each
offer is re-read by id, guarded, and committed **on its own**, so one offer that
raises can't abort the tick and discard the acceptances already made on it — see
:func:`_sweep_tenant` and ``backend/docs/background-sweeps.md`` § Locking.
Disabled by default (``FEOH_DISCOUNT_OPTIMIZATION_ENABLED``).
See ``backend/docs/dynamic-discounting.md``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models.discount import (
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_OFFERED,
    DiscountOffer,
)
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import PaymentSchedule
from app.services.audit_dispatch import dispatch_audit
from app.services.discount_offers import (
    best_tier_for_date,
    expire_if_past,
    has_lapsed,
    invoice_awaits_payment,
    offer_reference_date,
)
from app.services.discount_roi import compute_roi, days_between
from app.services.sweep_health import SWEEP_DISCOUNT_AUTO_TRIGGER, run_sweep_loop
from app.utils.dates import utc_today

logger = logging.getLogger(__name__)

# resolver: organization_id -> annual cost of capital (%) as Decimal.
OrgSettingsResolver = Callable[[uuid.UUID], Awaitable[Decimal]]


@dataclass
class AutoTriggerResult:
    """Per-sweep outcome for logging + tests."""

    tenants_scanned: int = 0
    offers_captured: int = 0
    #: Lapsed offers materialized to `expired` this sweep. Not a capture — see
    #: `TenantSweepOutcome.expired`.
    offers_expired: int = 0
    #: Tenants whose sweep aborted outright (engine/connect/candidate-query
    #: failure).
    failures: int = 0
    #: Individual offers whose decision or write raised. Counted apart from
    #: ``failures`` because one bad offer no longer takes its tenant's
    #: remaining offers down with it — mirrors ``vendor_rescreen``'s
    #: ``vendor_failures``. The ``*_failures`` suffix is load-bearing:
    #: ``sweep_health.failure_count`` sums exactly ``failures`` and any
    #: ``*_failures`` field, so a tick that keeps completing while every offer
    #: inside it raises reports ``partial`` (and past the streak, ``degraded``)
    #: instead of ``ok``.
    offer_failures: int = 0


@dataclass
class TenantSweepOutcome:
    """One tenant's outcome — accepted + expired counts and per-offer failures."""

    captured: int = 0
    #: Lapsed `offered` rows this tick materialized to `expired`. Counted apart
    #: from `captured` because expiry is not a capture decision: it takes no
    #: ROI, no threshold, and it is a tidy-up of a fact the read surfaces
    #: already derive.
    expired: int = 0
    offer_failures: int = 0


def _tier_deadline(offer: DiscountOffer, tier: dict, ref_today: date) -> date:
    """Latest date we can pay and still earn ``tier`` — the discount deadline.

    Measured from the offer's own reference date
    (``discount_offers.offer_reference_date`` — ``valid_from``, else the date
    the offer was created) plus ``tier.days``, capped at the offer's
    ``valid_until`` when set (you can never capture after the offer expires).
    Using ``ref_today`` (today) as the reference regardless of when the offer
    opened makes every tier's deadline a ROLLING "N days from now" — a tier
    that should have expired 15 days ago instead looks achievable forever.

    The earlier fix only reached ``valid_from``, and ``build_bulk_offer``
    doesn't set one, so every bulk negotiation kept the rolling deadline. The
    ``ref_today`` fallback now applies only to an offer carrying neither date —
    an unpersisted one being previewed, where "from today" is correct.
    """
    reference = offer_reference_date(offer) or ref_today
    deadline = reference + timedelta(days=int(tier["days"]))
    if offer.valid_until is not None and deadline > offer.valid_until:
        return offer.valid_until
    return deadline


async def _resolve_due_date(db: AsyncSession, offer: DiscountOffer) -> date | None:
    """Net due date for an invoice-scoped offer — ``PaymentSchedule.due_date``
    (the authoritative payment due date) falling back to ``Invoice.due_date``.

    Returns ``None`` for vendor-scoped (bulk) offers or invoice-less offers; the
    caller then falls back to ``valid_until`` as the horizon (documented
    approximation — a bulk offer spans many invoices with no single due date),
    and if there is no ``valid_until`` either the horizon is genuinely
    **unknown**. Callers must propagate that as ``None`` rather than
    substituting ``pay_by``: that reads as zero days accelerated, which is a
    measurement, not an absence.
    """
    if offer.invoice_id is None:
        return None
    due = (
        await db.execute(
            select(PaymentSchedule.due_date)
            .where(PaymentSchedule.invoice_id == offer.invoice_id)
            .order_by(PaymentSchedule.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if due is not None:
        return due
    return (
        await db.execute(select(Invoice.due_date).where(Invoice.id == offer.invoice_id))
    ).scalar_one_or_none()


async def settled_invoice_status(db: AsyncSession, offer: DiscountOffer) -> str | None:
    """The offer's invoice status when that invoice can no longer be paid,
    else ``None``.

    The one gate every accept path asks — ``POST /api/discounts/offers/{id}/
    accept``, the supplier portal's accept, the copilot's capture and this
    sweep — so an acceptance can never be recorded against an invoice already
    ``paid`` / ``done`` (``discount_offers.invoice_awaits_payment``). A
    vendor-scoped bulk offer has no single invoice and is never refused here.
    """
    if offer.invoice_id is None:
        return None
    status = (
        await db.execute(select(Invoice.status).where(Invoice.id == offer.invoice_id))
    ).scalar_one_or_none()
    if status is None or invoice_awaits_payment(status):
        return None
    return status.value if hasattr(status, "value") else str(status)


async def _resolve_cost_of_capital(organization_id: uuid.UUID) -> Decimal:
    """Org's annual cost of capital (%): per-org override else platform default.

    Reads ``Organization.settings.discounting.cost_of_capital_pct`` from the
    control plane, falling back to ``settings.discount_cost_of_capital_pct``.
    """
    default = Decimal(str(settings.discount_cost_of_capital_pct))
    async with control_session_factory() as ctrl:
        raw = (
            await ctrl.execute(
                select(Organization.settings).where(Organization.id == organization_id)
            )
        ).scalar_one_or_none()
    if isinstance(raw, dict):
        discounting = raw.get("discounting")
        if isinstance(discounting, dict) and discounting.get("cost_of_capital_pct") is not None:
            try:
                return Decimal(str(discounting["cost_of_capital_pct"]))
            except (TypeError, ValueError, ArithmeticError):
                pass
    return default


async def run_auto_trigger_once(*, today: date | None = None) -> AutoTriggerResult:
    """One auto-capture sweep across every tenant. Safe to call directly."""
    result = AutoTriggerResult()
    ref_today = today or utc_today()

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(select(Organization.id, Organization.db_name))
        tenants = list(rows.all())

    for _org_id, db_name in tenants:
        result.tenants_scanned += 1
        try:
            outcome = await _sweep_tenant(db_name, ref_today, _resolve_cost_of_capital)
            result.offers_captured += outcome.captured
            result.offers_expired += outcome.expired
            result.offer_failures += outcome.offer_failures
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            logger.warning(
                "[discount-auto-trigger] failed sweeping %s: %s", db_name, exc.__class__.__name__
            )
            result.failures += 1

    if result.offers_captured or result.offers_expired or result.failures or result.offer_failures:
        logger.info(
            "[discount-auto-trigger] swept %d tenant(s); accepted=%d expired=%d "
            "failed_sweeps=%d failed_offers=%d",
            result.tenants_scanned,
            result.offers_captured,
            result.offers_expired,
            result.failures,
            result.offer_failures,
        )
    return result


async def _claim_if_still_offered(db, offer_id) -> DiscountOffer | None:
    """Re-read ONE offer under a row lock, returning it only if it is still
    ``offered``; ``None`` means someone else has since moved it.

    The candidate scan above is deliberately unlocked and its rows are only a
    snapshot: the sweep then does per-row async work (cost-of-capital resolution,
    due-date lookup, ROI) before deciding, and a supplier or an AP user can
    decline or accept an offer in that window. The sweep used to mutate the
    stale ORM object and issue an unconditional ``UPDATE ... SET status`` at the
    single end-of-loop commit, so a *committed* human decision was silently
    overwritten — an offer the supplier had declined came back as
    ``auto_accepted``, with an audit row asserting the sweep found it open.

    The lock is taken here, at the point of mutation, rather than on the
    candidate scan: holding ``FOR UPDATE`` across the whole loop would keep a
    growing lock set open across unrelated awaits, which is the pattern
    ``payment_reconciler`` is already flagged for. ``populate_existing`` forces
    the identity-mapped object to refresh from the locked row — without it the
    second SELECT returns the stale in-memory copy and re-checks nothing.
    """
    return (
        await db.execute(
            select(DiscountOffer)
            .where(
                DiscountOffer.id == offer_id,
                DiscountOffer.status == OFFER_STATUS_OFFERED,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _sweep_tenant(
    db_name: str,
    ref_today: date,
    org_settings_resolver: OrgSettingsResolver,
) -> TenantSweepOutcome:
    """Auto-accept worthwhile open offers for one tenant.

    Does NOT move money — only transitions ``offered`` → ``accepted`` (see the
    module docstring's money-path boundary).

    **Per offer, not per tenant, and keyset-paginated.** Candidate ids are
    selected unlocked, ordered by id and read a page at a time
    (``discount_optimization_batch_size``, ``WHERE id > :cursor``) until the
    tenant is exhausted; each id is then re-read inside its own guarded leg,
    claimed under a row lock at the point of mutation, and committed on its own.

    **Paginating rather than capping is forced by the predicate.** An offer the
    sweep skips for a below-threshold ROI stays ``offered``, so it never leaves
    the candidate set — a per-tick ``LIMIT`` would re-serve the same lowest-id
    offers on every tick and never reach the tail, the starvation
    ``approval_escalation`` was rewritten to avoid. Paging is safe across a page
    boundary precisely because the leg re-claims the row under ``FOR UPDATE``
    and re-checks ``status == offered`` there: the unlocked id read is only a
    snapshot, and a page taken after several commits is no more stale than the
    single unbounded read it replaces. The unbounded read was a memory/latency
    ceiling — a tenant with a large open-offer book loaded every id at once. The
    loop used to mutate a pre-loaded snapshot and commit once at the end, so a
    single offer that raised — a malformed ``tiers`` blob, the control-plane
    cost-of-capital lookup failing, an audit write that will not land — aborted
    the tenant's whole tick AND discarded every acceptance already made on it.
    Deterministic causes (a malformed row does not heal itself) meant that
    tenant then made zero progress on every subsequent tick, forever, while the
    discarded ``failures`` counter reported nothing. Same shape as
    ``vendor_rescreen`` / ``recurring_invoices`` / ``approval_escalation``; see
    ``backend/docs/background-sweeps.md`` § Locking.

    Re-reading by id inside the leg is not cosmetic: a rollback expires the ORM
    objects a pre-loaded list would still hold, and touching one of those from
    async SQLAlchemy is a ``MissingGreenlet``, not a clean failure. A leg with
    nothing to write ends in ``rollback()`` so the read transaction (and, on
    the claim paths, its row lock) is released immediately rather than at the
    end of the tick.
    """
    threshold = Decimal(str(settings.discount_auto_capture_roi_threshold))
    page_size = int(settings.discount_optimization_batch_size)
    outcome = TenantSweepOutcome()
    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            # Resolve cost of capital once per distinct org (a tenant DB is one
            # org today, but the resolver contract is per-org). Outside the page
            # loop so it survives paging.
            coc_cache: dict[uuid.UUID, Decimal] = {}
            after: uuid.UUID | None = None
            while True:
                query = (
                    select(DiscountOffer.id)
                    .where(DiscountOffer.status == OFFER_STATUS_OFFERED)
                    .order_by(DiscountOffer.id.asc())
                    .limit(page_size)
                )
                if after is not None:
                    query = query.where(DiscountOffer.id > after)
                offer_ids = (await db.execute(query)).scalars().all()
                # NOT pre-filtered to valid_until >= ref_today: an `offered` row
                # past its own valid_until must still surface here so the
                # expire_if_past check below can actually flip it - previously
                # excluded entirely, which is exactly why an offer never
                # auto-expired anywhere (issue #124).
                if not offer_ids:
                    break
                after = offer_ids[-1]
                for offer_id in offer_ids:
                    try:
                        offer = await db.get(DiscountOffer, offer_id)
                        if offer is None or offer.status != OFFER_STATUS_OFFERED:
                            # Deleted or decided between the id read and this leg.
                            await db.rollback()
                            continue

                        # Materialize expiry. This leg is NOT the auto-capture
                        # decision and shares none of its policy: it takes no ROI,
                        # no threshold and no cost of capital, and it moves no
                        # money — it writes down a fact the calendar already
                        # settled. It also is NOT what makes expiry correct: this
                        # whole sweep is behind FEOH_DISCOUNT_OPTIMIZATION_ENABLED,
                        # which is off by default, so every read surface derives
                        # the effective status instead
                        # (`discount_offers.effective_status`). What running here
                        # adds is the durable row state plus the append-only audit
                        # entry. Expiry is a status write, so it takes the same
                        # claim.
                        if has_lapsed(offer, as_of=ref_today):
                            claimed = await _claim_if_still_offered(db, offer_id)
                            if claimed is None:
                                await db.rollback()
                                continue
                            if expire_if_past(claimed, as_of=ref_today):
                                # A status change writes an audit row (project
                                # invariant); this one used to commit silently, so
                                # the only evidence an offer had lapsed was the
                                # column itself.
                                await dispatch_audit(
                                    db,
                                    correlation_id=uuid.uuid4(),
                                    organization_id=claimed.organization_id,
                                    actor_id=None,  # system actor
                                    action="discount_offer.expired",
                                    entity_type="discount_offer",
                                    entity_id=claimed.id,
                                    details={
                                        "valid_until": claimed.valid_until.isoformat(),
                                        "as_of": ref_today.isoformat(),
                                    },
                                )
                                outcome.expired += 1
                            await db.commit()
                            continue

                        # Date-window-enforced pick — the SAME rule the acceptance
                        # endpoints use, measured from when the offer was actually
                        # extended (`offer_reference_date`: `valid_from`, else the
                        # offer's creation date), not from today. Without this, every
                        # tier's deadline looked like "N days from now" and the sweep
                        # always auto-accepted the highest-percent tier regardless of
                        # how long the offer had been open.
                        tier = best_tier_for_date(
                            offer.tiers or [],
                            ref_today,
                            offer.valid_until,
                            reference_date=offer_reference_date(offer),
                        )
                        if tier is None:
                            await db.rollback()
                            continue

                        # An invoice already paid / done has no payment left to
                        # make early, so the discount can never be realized —
                        # whatever its ROI. Left `offered` (no decision made).
                        if await settled_invoice_status(db, offer) is not None:
                            await db.rollback()
                            continue

                        if offer.organization_id not in coc_cache:
                            coc_cache[offer.organization_id] = await org_settings_resolver(
                                offer.organization_id
                            )
                        cost_of_capital = coc_cache[offer.organization_id]

                        # Acceleration is the textbook horizon: pay on the discount
                        # deadline instead of at the invoice's net due date
                        # (days_between(pay_by, due_date)), NOT the discount period.
                        pay_by = _tier_deadline(offer, tier, ref_today)
                        due_date = await _resolve_due_date(db, offer) or offer.valid_until
                        if due_date is None:
                            # No net due date to accelerate against, so there is
                            # no APR and nothing can be SHOWN to clear the
                            # threshold. `pay_by` used to be the last fallback,
                            # which reports zero days accelerated — an offer
                            # skipped for a fabricated 0.00 % APR rather than for
                            # the honest reason. The outcome is the same (never
                            # auto-accepted); the reason is now true. Surfacing
                            # the gap beats a plausible-looking wrong figure
                            # (`docs/decisions.md` §18).
                            await db.rollback()
                            continue
                        roi = compute_roi(
                            base_amount=offer.base_amount,
                            discount_percent=Decimal(str(tier["percent"])),
                            days_accelerated=days_between(pay_by, due_date),
                            cost_of_capital_pct=cost_of_capital,
                        )

                        if not (roi.worthwhile and roi.annualized_return_pct >= threshold):
                            await db.rollback()
                            continue

                        # Auto-accept: flag for capture. Money still moves via the
                        # CFO-gated payment run — never here.
                        #
                        # Re-claim under a row lock: everything above ran against the
                        # unlocked candidate read, and a decline committed in that
                        # window must win over a sweep that never saw it.
                        claimed = await _claim_if_still_offered(db, offer_id)
                        if claimed is None:
                            await db.rollback()
                            continue
                        claimed.accepted_tier = tier
                        claimed.accepted_at = datetime.now(UTC)
                        claimed.status = OFFER_STATUS_ACCEPTED

                        await dispatch_audit(
                            db,
                            correlation_id=uuid.uuid4(),
                            organization_id=claimed.organization_id,
                            actor_id=None,  # system actor
                            action="discount_offer.auto_accepted",
                            entity_type="discount_offer",
                            entity_id=claimed.id,
                            details={
                                "tier": tier,
                                "threshold_pct": str(threshold),
                                "roi": roi.as_dict(),  # Decimal-strings, no PII
                            },
                        )
                        await db.commit()
                        outcome.captured += 1
                    except Exception as exc:  # noqa: BLE001 — one offer must not halt the tenant
                        # Class only — a DB/asyncpg error message can echo a vendor
                        # name or a denormalised amount (PII-out-of-logs).
                        logger.warning(
                            "[discount-auto-trigger] offer=%s auto-capture failed in %s: %s",
                            offer_id,
                            db_name,
                            exc.__class__.__name__,
                        )
                        await db.rollback()
                        outcome.offer_failures += 1
                        continue

                if len(offer_ids) < page_size:
                    break

            return outcome
    finally:
        await engine.dispose()


async def run_discount_optimization_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; cancelled on shutdown.
    Body is the shared ``sweep_health.run_sweep_loop``."""
    await run_sweep_loop(
        SWEEP_DISCOUNT_AUTO_TRIGGER,
        lambda: run_auto_trigger_once(),
        interval_seconds=settings.discount_optimization_interval_seconds,
        log=logger,
        log_prefix="[discount-auto-trigger]",
    )
