"""Periodic vendor re-screening sweep — background loop ("ongoing monitoring").

Sanctions lists move. A vendor that screened ``clear`` at onboarding can land
on a watchlist months later, so a one-time screen is not enough — the roadmap
"ongoing monitoring" item re-screens active vendors on a cadence and re-blocks
any that newly hit. Every tick:

  1. Enumerate tenant DBs from the control plane (one query, also pulls each
     org's ``settings`` so we have the adapter config per tenant).
  2. For each tenant, open a fresh per-tenant engine and select ``active``
     vendors whose ``last_screened_at`` is NULL or older than the staleness
     window (``settings.vendor_rescreen_after_days``).
  3. Re-read each candidate ``FOR UPDATE``, re-check that it is *still* due
     (see :func:`_still_due`), and re-screen it via
     ``vendor_screening.screen_vendor_record``
     (``check_type="periodic"``) — the trail row, denormalised state, and the
     payment block are all handled there.
  4. Track how many vendors NEWLY flip to ``match`` / ``review`` (a vendor that
     was previously neither) and log an INFO line per new flag so the AP team
     can monitor. (No notification event-type exists for vendor screening and
     inventing one would need a migration / event-registry change; the re-screen
     itself — including the payment block on a ``match`` — is the load-bearing
     behaviour, so we deliberately fall back to logging here.)
  5. Commit **per vendor**.

Mirrors ``services/contract_renewal.py``: long-lived asyncio task started in
``main.lifespan``, fresh per-tenant engine, one tenant's failure logged but
never halting the sweep. Disabled by default (``FEOH_VENDOR_RESCREEN_ENABLED``).

**Why per-vendor and not once per tenant.** The tenant loop had no per-vendor
guard and committed once at the end, and ``screen_vendor_record`` has no
internal ``except`` — so a single vendor whose screen raised (adapter transport
error, a bad credential, one malformed row) aborted the tenant's whole sweep
*before* that commit. Not even the vendors already screened on that tick got
their ``last_screened_at`` advanced, so every one of them stayed due, the same
poison vendor was re-selected on the next tick, and the sweep made **zero
progress forever** — a sanctions-compliance control silently not running, whose
only trace was a per-tenant ``failures`` counter the loop discards. Committing
each vendor on its own turns that into: the poison vendor is skipped and
counted, everyone else advances. Each vendor is re-read by id inside its own
leg, because a rollback expires the ORM objects a pre-loaded list would still be
holding — and touching one of those from async SQLAlchemy is a
``MissingGreenlet``, not a clean failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.db_engine import create_async_engine
from app.models.organization import Organization
from app.models.vendor import Vendor
from app.services.sweep_health import SWEEP_VENDOR_RESCREEN, run_sweep_loop
from app.services.vendor_screening import screen_vendor_record

logger = logging.getLogger(__name__)

# Denormalised statuses that count as "flagged" — a vendor flipping INTO one of
# these (from anything else) is a new flag worth surfacing.
_FLAGGED_STATUSES = frozenset({"match", "review"})


def _still_due(vendor: Vendor, cutoff: datetime) -> bool:
    """Re-test, under the row lock, the predicate the id query selected on.

    Step 2 of the two-phase shape in ``../docs/background-sweeps.md`` § Locking,
    and it is **correctness, not an optimisation**. The id query is unlocked, so
    between reading the id and locking the row another replica's sweep — or a
    manual ``POST /api/vendors/{id}/screen`` — can have screened the same vendor
    and committed. Acting on the stale snapshot bills the sanctions provider a
    second time for one screening event and appends a second
    ``SanctionsCheck`` row plus a second ``vendor.screened`` audit row for it,
    and both trails are append-only, so the duplicate cannot be tidied away
    afterwards. It also races the vendor's denormalised
    ``screening_status`` / ``last_screened_at``, whose last writer wins.

    Deliberately the same three clauses as the id query (``status == "active"``
    and a NULL-or-stale ``last_screened_at``), so a vendor deactivated mid-tick
    is skipped too — re-screening a vendor an admin just retired is work nobody
    asked for and a payment block nobody expects.
    """
    if vendor.status != "active":
        return False
    return vendor.last_screened_at is None or vendor.last_screened_at < cutoff


@dataclass
class RescreenResult:
    """Per-sweep outcome for logging + tests."""

    tenants_scanned: int = 0
    vendors_screened: int = 0
    new_flags: int = 0
    #: Tenants whose sweep aborted outright (engine/connect/query failure).
    failures: int = 0
    #: Individual vendors whose screen raised. Counted apart from ``failures``
    #: because a vendor failure no longer takes its tenant down with it — the
    #: rest of that tenant's vendors still advance.
    vendor_failures: int = 0


async def rescreen_vendors_once(*, now: datetime | None = None) -> RescreenResult:
    """One sweep across every tenant. Safe to call directly (CLI / tests).

    ``now`` is injectable so the staleness cutoff is deterministic in tests;
    production leaves it None and it defaults to ``datetime.now(UTC)``.
    """
    result = RescreenResult()
    ref_now = now or datetime.now(UTC)
    cutoff = ref_now - timedelta(days=settings.vendor_rescreen_after_days)

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(
            select(Organization.id, Organization.db_name, Organization.settings)
        )
        tenants = list(rows.all())

    for _org_id, db_name, org_settings in tenants:
        result.tenants_scanned += 1
        try:
            screened, flags, vendor_failures = await _sweep_tenant(db_name, org_settings, cutoff)
            result.vendors_screened += screened
            result.new_flags += flags
            result.vendor_failures += vendor_failures
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            # Log the exception CLASS only — a DB/asyncpg or sanctions-adapter
            # error message can echo a vendor name / partial banking value
            # (PII-out-of-logs).
            logger.warning(
                "[vendor-rescreen] failed sweeping %s: %s", db_name, exc.__class__.__name__
            )
            result.failures += 1

    if result.vendors_screened or result.new_flags or result.failures or result.vendor_failures:
        logger.info(
            "[vendor-rescreen] swept %d tenant(s); screened=%d new_flags=%d "
            "failed_sweeps=%d failed_vendors=%d",
            result.tenants_scanned,
            result.vendors_screened,
            result.new_flags,
            result.failures,
            result.vendor_failures,
        )
    return result


async def _sweep_tenant(
    db_name: str, org_settings: dict | None, cutoff: datetime
) -> tuple[int, int, int]:
    """Re-screen due vendors for one tenant.

    Returns ``(vendors_screened, new_flags, vendor_failures)``. Commits per
    vendor — see the module docstring for why one commit per tenant made a
    single failing vendor block the tenant's sweep permanently.

    **Two-phase, one row locked at a time**, the shape every mutating sweep uses
    (``../docs/background-sweeps.md`` § Locking): candidate ids are read
    UNLOCKED ``ORDER BY id`` (the same lock order on every replica, so two
    sweeps queue instead of deadlocking), then each is re-read ``FOR UPDATE``,
    re-checked against the predicate the id query used (:func:`_still_due`),
    screened, and committed on its own — which releases the lock before the
    next vendor is touched.

    The re-check is the half this sweep was missing. See :func:`_still_due` for
    what the gap cost: a duplicate paid sanctions call and a duplicate
    append-only ``SanctionsCheck`` row for a single screening event.
    """
    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            # Ids only, UNLOCKED: each vendor is re-read `FOR UPDATE` inside its
            # own leg, so a leg that rolls back can't leave the NEXT one holding
            # an expired ORM object, and no lock outlives the vendor that needed
            # it. `ORDER BY id` gives every replica the same lock order.
            due_ids = (
                (
                    await db.execute(
                        select(Vendor.id)
                        .where(
                            Vendor.status == "active",
                            or_(
                                Vendor.last_screened_at.is_(None),
                                Vendor.last_screened_at < cutoff,
                            ),
                        )
                        .order_by(Vendor.id)
                    )
                )
                .scalars()
                .all()
            )

            screened = 0
            new_flags = 0
            vendor_failures = 0
            for vendor_id in due_ids:
                try:
                    # `with_for_update` bypasses the identity map, so this is a
                    # real `SELECT ... FOR UPDATE` on exactly one row.
                    vendor = await db.get(Vendor, vendor_id, with_for_update=True)
                    if vendor is None or not _still_due(vendor, cutoff):
                        # Deleted, deactivated, or screened by another replica /
                        # a manual re-screen between the id read and the lock.
                        # End the transaction so the lock is released now rather
                        # than at the end of the tick.
                        await db.rollback()
                        continue

                    # Capture the pre-screen status so we only count a vendor
                    # that was NOT already flagged flipping into match / review.
                    was_flagged = vendor.screening_status in _FLAGGED_STATUSES

                    outcome = await screen_vendor_record(
                        db,
                        vendor=vendor,
                        organization_id=vendor.organization_id,
                        org_settings=org_settings,
                        check_type="periodic",
                    )
                    await db.commit()
                    screened += 1
                except Exception as exc:  # noqa: BLE001 — one vendor must not halt the tenant
                    # Class only — a sanctions-adapter or asyncpg error message
                    # can echo a vendor name / partial banking value
                    # (PII-out-of-logs).
                    logger.warning(
                        "[vendor-rescreen] vendor=%s screen failed in %s: %s",
                        vendor_id,
                        db_name,
                        exc.__class__.__name__,
                    )
                    await db.rollback()
                    vendor_failures += 1
                    continue

                if not was_flagged and outcome.screening_status in _FLAGGED_STATUSES:
                    new_flags += 1
                    # No notification event-type exists for vendor screening
                    # (see module docstring); log so the AP team can monitor.
                    # PII-free: vendor id + verdict + list NAME only.
                    logger.info(
                        "[vendor-rescreen] vendor=%s newly flagged status=%s "
                        "matched_list=%s tenant=%s",
                        vendor.id,
                        outcome.screening_status,
                        outcome.matched_list,
                        db_name,
                    )

            return screened, new_flags, vendor_failures
    finally:
        await engine.dispose()


async def run_vendor_rescreen_loop() -> None:
    """Long-lived loop started in `main.lifespan`; cancelled on shutdown.

    Body is the shared `sweep_health.run_sweep_loop`. Both counters this sweep
    reports feed the health streak — `failures` (a whole tenant's sweep
    aborted) and `vendor_failures` (individual screens that raised) — so a
    tenant that keeps completing while every vendor screen fails is no longer
    indistinguishable from a clean run.
    """
    await run_sweep_loop(
        SWEEP_VENDOR_RESCREEN,
        lambda: rescreen_vendors_once(),
        interval_seconds=settings.vendor_rescreen_interval_seconds,
        log=logger,
        log_prefix="[vendor-rescreen]",
    )
