"""Push received quantities into the business's commerce platform (decisions §273).

The shape, end to end:

1. **Plan — inside the receipt's own transaction.** ``goods_receipts`` calls
   :func:`plan_receipt` after writing a receipt and :func:`plan_reversal` after
   cancelling one. Each writes one ``commerce_inventory_syncs`` row per line
   (unique on ``(gr_line_item_id, direction)``), so the record of what must be
   pushed commits — or rolls back — with the receipt itself. A line with no SKU
   or no quantity is written ``skipped`` there and then.
2. **Push — after the commit** (``services/post_commit``), never inside the
   open transaction: a provider's latency must not be charged to a transaction
   holding the PO row lock. :func:`push_syncs` claims each row (a
   compare-and-swap to ``in_flight``, committed), calls the provider with no
   transaction open, and records the outcome with a second compare-and-swap.
3. **Retry.** A retryable failure (timeout, 5xx, 429) is retried by the sweep
   (:func:`run_commerce_sync_loop`, ``FEOH_COMMERCE_SYNC_ENABLED``, off by
   default) with backoff, up to ``FEOH_COMMERCE_SYNC_MAX_ATTEMPTS``; any failed
   row, and a skip an admin can fix (an unmapped SKU, an unmapped location), can
   be retried by hand (:func:`request_retry`). A claim left ``in_flight`` by a
   process that died is reclaimed once stale.

Idempotency is two-sided. Ours: the unique row per line and direction — a
receipt replayed through its own idempotency key plans nothing new. The
provider's: every push carries the row's ``idempotency_key``
(``feoh:<line id>:<direction>``), so a push retried after a lost response
changes the shelf count at most once.

Never best-effort about the RECORD, always best-effort about the PUSH: the
receipt never fails because the platform is down; the row says it did.

Nothing here logs a SKU, a credential or a provider message — class names and
ids only.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.refusals import coded_refusal
from app.config import settings
from app.models.commerce import (
    DIRECTION_RECEIVE,
    DIRECTION_REVERSE,
    SYNC_CANCELLED,
    SYNC_FAILED,
    SYNC_IN_FLIGHT,
    SYNC_PENDING,
    SYNC_SKIPPED,
    SYNC_SYNCED,
    CommerceInventorySync,
    CommerceSkuMapping,
)
from app.models.procurement import GoodsReceipt
from app.services.audit_dispatch import dispatch_audit
from app.services.commerce_adapters import (
    CommerceAdapter,
    CommerceError,
    CommerceUnmappedSkuError,
    InventoryAdjustment,
    UnknownCommerceAdapterError,
    get_commerce_adapter,
)
from app.services.commerce_adapters.base import FRACTIONAL_QUANTITY
from app.services.commerce_adapters.catalog import location_for
from app.services.credential_crypto import CredentialCryptoError
from app.services.post_commit import enqueue_post_commit

logger = logging.getLogger(__name__)

SETTINGS_KEY = "commerce"

# Coded reasons stored on a row (`reason`). The web client localizes them
# (`org.commerce.reason.<code>` / `commerceSync.reason.<code>`); adapter error
# codes (`auth_failed`, `transient`, `rejected`, …) arrive through the same column.
REASON_NO_SKU = "no_sku"
REASON_ZERO_QUANTITY = "zero_quantity"
REASON_UNMAPPED_SKU = "unmapped_sku"
REASON_NO_LOCATION = "no_location"
REASON_NOTHING_TO_REVERSE = "nothing_to_reverse"
REASON_RECEIPT_CANCELLED = "receipt_cancelled"
REASON_NOT_CONFIGURED = "not_configured"
REASON_PROVIDER_CHANGED = "provider_changed"
REASON_CREDENTIALS_UNREADABLE = "credentials_unreadable"
REASON_UNEXPECTED = "unexpected"

#: Skips an admin can fix and then retry: map the SKU, or the location.
RETRYABLE_SKIPS = frozenset({REASON_UNMAPPED_SKU, REASON_NO_LOCATION})

#: A claim this old is a push that died mid-call; the sweep may take it again.
#: The provider-side idempotency key makes the re-send safe.
STALE_CLAIM = timedelta(minutes=15)

#: Sweep page size (a page, not a cap — the sweep keyset-pages each tenant).
SWEEP_PAGE = 100

# Refusal codes — the web client localizes on these.
SYNC_NOT_RETRYABLE = "commerce_sync_not_retryable"
SYNC_NOT_FOUND = "commerce_sync_not_found"


def commerce_config(org_settings: Any) -> dict | None:
    """The org's ``settings.commerce`` block when it has opted in, else ``None``.

    Opting in is choosing a provider (``mock`` included). With no block, a
    receipt plans nothing: a tenant that never connected a platform must not
    accumulate a sync row per receipt line.
    """
    block = (org_settings or {}).get(SETTINGS_KEY) if isinstance(org_settings, dict) else None
    if isinstance(block, dict) and isinstance(block.get("provider"), str) and block["provider"]:
        return block
    return None


def idempotency_key(gr_line_item_id: uuid.UUID, direction: str) -> str:
    return f"feoh:{gr_line_item_id}:{direction}"


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Planning (inside the receipt's transaction)
# --------------------------------------------------------------------------- #


async def plan_receipt(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    org_settings: Any,
    receipt: GoodsReceipt,
    tenant_db_name: str | None,
) -> list[uuid.UUID]:
    """Write the ``receive`` rows for a just-recorded receipt; queue their push.

    Returns the ids of the rows left ``pending``. No-op when the org has no
    commerce platform configured. ``receipt.line_items`` must be loaded.
    """
    config = commerce_config(org_settings)
    if config is None:
        return []
    rows: list[CommerceInventorySync] = []
    for line in receipt.line_items:
        qty = Decimal(line.quantity_received or 0)
        sku = (line.sku or "").strip() or None
        status, reason = SYNC_PENDING, None
        if qty <= 0:
            status, reason = SYNC_SKIPPED, REASON_ZERO_QUANTITY
        elif sku is None:
            status, reason = SYNC_SKIPPED, REASON_NO_SKU
        rows.append(
            CommerceInventorySync(
                organization_id=org_id,
                entity_id=receipt.entity_id,
                goods_receipt_id=receipt.id,
                gr_line_item_id=line.id,
                direction=DIRECTION_RECEIVE,
                provider=config["provider"],
                sku=sku,
                quantity_delta=qty,
                status=status,
                reason=reason,
                retryable=False,
                idempotency_key=idempotency_key(line.id, DIRECTION_RECEIVE),
                attempts=0,
            )
        )
    db.add_all(rows)
    await db.flush()
    pending = [r.id for r in rows if r.status == SYNC_PENDING]
    for row in rows:
        if row.status == SYNC_SKIPPED:
            await _audit(db, row, actor_id=None)
    _enqueue_push(db, org_id=org_id, tenant_db_name=tenant_db_name, config=config, ids=pending)
    return pending


async def plan_reversal(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    org_settings: Any,
    receipt: GoodsReceipt,
    tenant_db_name: str | None,
) -> list[uuid.UUID]:
    """Write the ``reverse`` rows for a just-cancelled receipt; queue their push.

    Per line, by what its ``receive`` row reached:

    * ``synced`` / ``in_flight`` — the quantity is (or may be) on the shelf, so a
      ``reverse`` row takes it back off (an in-flight receive is waited for);
    * ``pending`` / ``failed`` / ``skipped`` — nothing landed. The receive row
      becomes ``cancelled`` so no retry ever pushes it, and the reverse row is a
      skip (``nothing_to_reverse``);
    * no receive row — the platform was not connected when the goods arrived;
      nothing to take back, no row.

    Runs whether or not commerce is still configured: a quantity already pushed
    has to come back off even if the admin since disconnected (the push then
    fails ``not_configured`` and waits for a retry).
    """
    receives = (
        (
            await db.execute(
                select(CommerceInventorySync)
                .where(
                    CommerceInventorySync.goods_receipt_id == receipt.id,
                    CommerceInventorySync.direction == DIRECTION_RECEIVE,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    rows: list[CommerceInventorySync] = []
    for rec in receives:
        if rec.status == SYNC_CANCELLED:
            continue
        pushed = rec.status in (SYNC_SYNCED, SYNC_IN_FLIGHT)
        if not pushed:
            rec.status = SYNC_CANCELLED
            rec.reason = REASON_RECEIPT_CANCELLED
            rec.retryable = False
        rows.append(
            CommerceInventorySync(
                organization_id=org_id,
                entity_id=rec.entity_id,
                goods_receipt_id=receipt.id,
                gr_line_item_id=rec.gr_line_item_id,
                direction=DIRECTION_REVERSE,
                provider=rec.provider,
                sku=rec.sku,
                quantity_delta=-Decimal(rec.quantity_delta),
                status=SYNC_PENDING if pushed else SYNC_SKIPPED,
                reason=None if pushed else REASON_NOTHING_TO_REVERSE,
                retryable=False,
                idempotency_key=idempotency_key(rec.gr_line_item_id, DIRECTION_REVERSE),
                attempts=0,
            )
        )
    if not rows:
        return []
    db.add_all(rows)
    await db.flush()
    pending = [r.id for r in rows if r.status == SYNC_PENDING]
    config = commerce_config(org_settings)
    if config is not None:
        _enqueue_push(db, org_id=org_id, tenant_db_name=tenant_db_name, config=config, ids=pending)
    return pending


def _enqueue_push(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    tenant_db_name: str | None,
    config: dict,
    ids: list[uuid.UUID],
    actor_id: uuid.UUID | None = None,
) -> None:
    """Queue the push to run after ``db`` commits (dropped on rollback).

    Without a tenant DB name (a caller outside a request) nothing is queued:
    the rows stay ``pending`` for the sweep or a manual retry.
    """
    if not ids or not tenant_db_name:
        return
    snapshot = dict(config)
    ids = list(ids)

    async def _job() -> None:
        await push_syncs(
            org_id=org_id,
            tenant_db_name=tenant_db_name,
            config=snapshot,
            sync_ids=ids,
            actor_id=actor_id,
        )

    enqueue_post_commit(db, _job, name="commerce-inventory-push")


# --------------------------------------------------------------------------- #
# Pushing (after the commit)
# --------------------------------------------------------------------------- #


def tenant_sessionmaker(tenant_db_name: str) -> async_sessionmaker:
    """A session maker on the tenant's engine, valid on the caller's loop.

    A module-level seam: tests point it at the harness's per-loop engines.
    """
    from app.database import get_tenant_engine

    return async_sessionmaker(get_tenant_engine(tenant_db_name), expire_on_commit=False)


@dataclass
class PushSummary:
    claimed: int = 0
    synced: int = 0
    skipped: int = 0
    #: Rows the provider refused or could not be reached for. Not named
    #: ``*failures``: a platform outage is the row's state, not a broken sweep.
    failed: int = 0
    deferred: int = 0


async def push_syncs(
    *,
    org_id: uuid.UUID,
    tenant_db_name: str,
    config: dict | None,
    sync_ids: Iterable[uuid.UUID],
    actor_id: uuid.UUID | None = None,
) -> PushSummary:
    """Push each row in ``sync_ids`` that can be claimed. Never raises per row.

    ``config`` is the PUBLIC ``settings.commerce`` block (configuration only);
    the sealed secrets are merged in here, after the receipt's commit and off
    its transaction (``provider_credentials.resolve_block``, decisions §266).
    """
    summary = PushSummary()
    adapter, build_failure = await _build_adapter(org_id, config)
    async with tenant_sessionmaker(tenant_db_name)() as db:
        for sync_id in sync_ids:
            outcome = await _push_one(
                db,
                sync_id=sync_id,
                config=config,
                adapter=adapter,
                build_failure=build_failure,
                actor_id=actor_id,
            )
            if outcome is None:
                continue
            summary.claimed += 1
            if outcome == SYNC_SYNCED:
                summary.synced += 1
            elif outcome == SYNC_SKIPPED:
                summary.skipped += 1
            elif outcome == SYNC_FAILED:
                summary.failed += 1
            else:
                summary.deferred += 1
    return summary


async def _build_adapter(
    org_id: uuid.UUID, config: dict | None
) -> tuple[CommerceAdapter | None, tuple[str, str] | None]:
    if config is None:
        return None, (REASON_NOT_CONFIGURED, "No commerce platform is connected.")
    from app.services.provider_credentials import resolve_block

    try:
        resolved = await resolve_block(org_id, {SETTINGS_KEY: config}, SETTINGS_KEY)
        return get_commerce_adapter(resolved or {}), None
    except UnknownCommerceAdapterError as exc:
        return None, (
            REASON_NOT_CONFIGURED,
            f"'{exc.provider}' is not a supported commerce platform.",
        )
    except CredentialCryptoError:
        return None, (
            REASON_CREDENTIALS_UNREADABLE,
            "The saved commerce credentials could not be opened on this server "
            "(the credential store is unavailable).",
        )


def _claimable(now: datetime):
    return or_(
        CommerceInventorySync.status == SYNC_PENDING,
        and_(
            CommerceInventorySync.status == SYNC_FAILED,
            CommerceInventorySync.retryable.is_(True),
        ),
        and_(
            CommerceInventorySync.status == SYNC_IN_FLIGHT,
            CommerceInventorySync.last_attempt_at < now - STALE_CLAIM,
        ),
    )


async def _claim(db: AsyncSession, sync_id: uuid.UUID) -> CommerceInventorySync | None:
    """Move the row to ``in_flight`` if nobody else holds it; commit the claim."""
    now = _now()
    claimed = (
        await db.execute(
            update(CommerceInventorySync)
            .where(CommerceInventorySync.id == sync_id, _claimable(now))
            .values(
                status=SYNC_IN_FLIGHT,
                attempts=CommerceInventorySync.attempts + 1,
                last_attempt_at=now,
            )
            .returning(CommerceInventorySync.id)
        )
    ).scalar_one_or_none()
    await db.commit()
    if claimed is None:
        return None
    row = await db.get(CommerceInventorySync, sync_id, populate_existing=True)
    # End the read's transaction: the provider call that follows must run with
    # nothing open (the session keeps the loaded row — expire_on_commit=False).
    await db.commit()
    return row


async def _push_one(
    db: AsyncSession,
    *,
    sync_id: uuid.UUID,
    config: dict | None,
    adapter: CommerceAdapter | None,
    build_failure: tuple[str, str] | None,
    actor_id: uuid.UUID | None,
) -> str | None:
    """One row: claim, call the provider (no transaction open), record. Returns its status."""
    try:
        row = await _claim(db, sync_id)
    except Exception as exc:  # noqa: BLE001 — one row's DB error must not stop the batch
        await db.rollback()
        logger.warning("[commerce-sync] claim failed sync=%s: %s", sync_id, type(exc).__name__)
        return None
    if row is None:
        return None

    outcome: dict[str, Any]
    try:
        outcome = await _attempt(db, row, config=config, adapter=adapter, failure=build_failure)
    except CommerceError as exc:
        outcome = _failed(exc.code, str(exc), retryable=exc.retryable)
    except Exception as exc:  # noqa: BLE001 — recorded on the row; class name only
        logger.warning("[commerce-sync] push raised sync=%s: %s", sync_id, type(exc).__name__)
        outcome = _failed(REASON_UNEXPECTED, f"Unexpected error ({type(exc).__name__}).", True)
    return await _record(db, row, outcome, actor_id=actor_id)


def _failed(reason: str, message: str, retryable: bool) -> dict[str, Any]:
    return {
        "status": SYNC_FAILED,
        "reason": reason,
        "error_message": message[:300] or None,
        "retryable": retryable,
    }


def _skipped(reason: str) -> dict[str, Any]:
    return {"status": SYNC_SKIPPED, "reason": reason, "error_message": None, "retryable": False}


async def _attempt(
    db: AsyncSession,
    row: CommerceInventorySync,
    *,
    config: dict | None,
    adapter: CommerceAdapter | None,
    failure: tuple[str, str] | None,
) -> dict[str, Any]:
    if failure is not None or adapter is None or config is None:
        reason, message = failure or (REASON_NOT_CONFIGURED, "No commerce platform is connected.")
        return _failed(reason, message, retryable=False)
    if row.provider != adapter.key:
        return _failed(
            REASON_PROVIDER_CHANGED,
            "The commerce platform changed after this line was received; it was not pushed "
            "to the new one.",
            retryable=False,
        )

    if row.direction == DIRECTION_REVERSE:
        receive = (
            await db.execute(
                select(CommerceInventorySync).where(
                    CommerceInventorySync.gr_line_item_id == row.gr_line_item_id,
                    CommerceInventorySync.direction == DIRECTION_RECEIVE,
                )
            )
        ).scalar_one_or_none()
        await db.commit()
        if receive is not None and receive.status == SYNC_IN_FLIGHT:
            return {"status": SYNC_PENDING, "deferred": True}
        if receive is None or receive.status != SYNC_SYNCED:
            return _skipped(REASON_NOTHING_TO_REVERSE)
        item_id = receive.provider_item_id
        location_id = receive.provider_location_id
    else:
        location_id = location_for(config, row.entity_id)
        if location_id is None:
            return _skipped(REASON_NO_LOCATION)
        item_id = await _mapped_item(db, row)
        if item_id is None:
            try:
                item_id = (await adapter.resolve_sku(row.sku or "")).item_id
            except CommerceUnmappedSkuError:
                return _skipped(REASON_UNMAPPED_SKU)

    delta = Decimal(row.quantity_delta)
    if not adapter.spec.supports_fractional_quantities and delta != delta.to_integral_value():
        return _failed(
            FRACTIONAL_QUANTITY,
            "This platform counts whole units only; the received quantity has a fraction.",
            retryable=False,
        )
    result = await adapter.adjust_available(
        InventoryAdjustment(
            item_id=str(item_id),
            location_id=str(location_id),
            delta=delta,
            idempotency_key=row.idempotency_key,
            sku=row.sku,
            reason="received" if row.direction == DIRECTION_RECEIVE else "receipt_cancelled",
            # The claim already counted this push.
            attempt=max(1, row.attempts or 1),
        )
    )
    return {
        "status": SYNC_SYNCED,
        "reason": None,
        "error_message": None,
        "retryable": False,
        "provider_item_id": str(item_id)[:255],
        "provider_location_id": str(location_id)[:255],
        "provider_reference": (result.provider_reference or "")[:255] or None,
        "synced_at": _now(),
    }


async def _mapped_item(db: AsyncSession, row: CommerceInventorySync) -> str | None:
    """The admin's override for this SKU on this provider, if any."""
    if not row.sku:
        return None
    mapped = (
        await db.execute(
            select(CommerceSkuMapping.provider_item_id).where(
                CommerceSkuMapping.organization_id == row.organization_id,
                CommerceSkuMapping.provider == row.provider,
                CommerceSkuMapping.sku == row.sku,
            )
        )
    ).scalar_one_or_none()
    await db.commit()
    return mapped


async def _record(
    db: AsyncSession,
    row: CommerceInventorySync,
    outcome: dict[str, Any],
    *,
    actor_id: uuid.UUID | None,
) -> str | None:
    """Write the outcome only if the row is still ours (``in_flight``)."""
    deferred = outcome.pop("deferred", False)
    values = dict(outcome)
    if deferred:
        # Waiting on the receive push: give the attempt back.
        values["attempts"] = max(0, (row.attempts or 1) - 1)
    try:
        written = (
            await db.execute(
                update(CommerceInventorySync)
                .where(
                    CommerceInventorySync.id == row.id,
                    CommerceInventorySync.status == SYNC_IN_FLIGHT,
                )
                .values(**values)
                .returning(CommerceInventorySync.id)
            )
        ).scalar_one_or_none()
        if written is None:
            await db.rollback()
            return None
        await db.refresh(row)
        if not deferred and _audit_worthy(row):
            await _audit(db, row, actor_id=actor_id)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — the stale claim will be reclaimed
        await db.rollback()
        logger.warning("[commerce-sync] record failed sync=%s: %s", row.id, type(exc).__name__)
        return None
    return SYNC_PENDING if deferred else row.status


def _audit_worthy(row: CommerceInventorySync) -> bool:
    """Every terminal outcome is audited; a retryable failure only once it stops
    being retried — so an hour-long outage is one row, not one per tick."""
    if row.status != SYNC_FAILED or not row.retryable:
        return True
    return row.attempts >= settings.commerce_sync_max_attempts


async def _audit(
    db: AsyncSession, row: CommerceInventorySync, *, actor_id: uuid.UUID | None
) -> None:
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=row.organization_id,
        actor_id=actor_id,
        action=f"commerce_inventory.{row.status}",
        entity_type="goods_receipt",
        entity_id=row.goods_receipt_id,
        details=_audit_details(row),
    )


def _audit_details(row: CommerceInventorySync) -> dict[str, Any]:
    return {
        "sync_id": str(row.id),
        "gr_line_item_id": str(row.gr_line_item_id),
        "direction": row.direction,
        "provider": row.provider,
        "sku": row.sku,
        "quantity_delta": format(Decimal(row.quantity_delta).normalize(), "f"),
        "status": row.status,
        "reason": row.reason,
        "provider_reference": row.provider_reference,
        "attempts": row.attempts,
    }


# --------------------------------------------------------------------------- #
# Manual retry
# --------------------------------------------------------------------------- #


async def request_retry(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    sync_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    actor_id: uuid.UUID,
    org_settings: Any,
    tenant_db_name: str | None,
) -> CommerceInventorySync:
    """Reset a failed row, or a fixable skip, to ``pending`` and queue its push.

    Re-sends what the receipt already authorised, under the same idempotency
    key — a retry can never push a line twice. Audited with the actor.
    """
    from app.tenant import apply_entity_scope

    row = (
        await db.execute(
            apply_entity_scope(
                select(CommerceInventorySync).where(CommerceInventorySync.id == sync_id),
                CommerceInventorySync,
                entity_id,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=coded_refusal(SYNC_NOT_FOUND, "Inventory sync not found."),
        )
    fixable_skip = row.status == SYNC_SKIPPED and row.reason in RETRYABLE_SKIPS
    if row.status != SYNC_FAILED and not fixable_skip:
        raise HTTPException(
            status_code=409,
            detail=coded_refusal(
                SYNC_NOT_RETRYABLE,
                "Only a failed push, or one skipped for an unmapped SKU or location, can be "
                "retried.",
                status=row.status,
            ),
        )
    previous = {"status": row.status, "reason": row.reason}
    row.status = SYNC_PENDING
    row.reason = None
    row.error_message = None
    row.retryable = False
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=actor_id,
        action="commerce_inventory.retry_requested",
        entity_type="goods_receipt",
        entity_id=row.goods_receipt_id,
        details={**_audit_details(row), "previous": previous},
    )
    config = commerce_config(org_settings)
    if config is not None:
        _enqueue_push(
            db,
            org_id=org_id,
            tenant_db_name=tenant_db_name,
            config=config,
            ids=[row.id],
            actor_id=actor_id,
        )
    return row


# --------------------------------------------------------------------------- #
# The retry sweep
# --------------------------------------------------------------------------- #


def backoff(attempts: int) -> timedelta:
    """Wait before the next automatic attempt: 1, 2, 4 … minutes, capped at 6 hours."""
    return min(timedelta(minutes=2 ** max(0, attempts - 1)), timedelta(hours=6))


@dataclass
class CommerceSweepResult:
    tenants_scanned: int = 0
    claimed: int = 0
    synced: int = 0
    skipped: int = 0
    #: Row-level provider failures (see ``PushSummary.failed``).
    failed: int = 0
    #: Tenants whose sweep raised — the sweep's own health signal.
    failures: int = 0


async def due_sync_ids(db: AsyncSession, *, after: uuid.UUID | None, now: datetime) -> list:
    """One keyset page of rows the sweep should try now, and the page's last id."""
    query = (
        select(
            CommerceInventorySync.id,
            CommerceInventorySync.status,
            CommerceInventorySync.attempts,
            CommerceInventorySync.last_attempt_at,
        )
        .where(_claimable(now))
        .order_by(CommerceInventorySync.id)
        .limit(SWEEP_PAGE)
    )
    if after is not None:
        query = query.where(CommerceInventorySync.id > after)
    return list((await db.execute(query)).all())


def _is_due(status: str, attempts: int, last_attempt_at: datetime | None, now: datetime) -> bool:
    if status != SYNC_FAILED:
        return True
    if attempts >= settings.commerce_sync_max_attempts:
        return False
    return last_attempt_at is None or now - last_attempt_at >= backoff(attempts)


async def sweep_tenant(org_id: uuid.UUID, tenant_db_name: str, config: dict) -> PushSummary:
    total = PushSummary()
    after: uuid.UUID | None = None
    while True:
        now = _now()
        async with tenant_sessionmaker(tenant_db_name)() as db:
            page = await due_sync_ids(db, after=after, now=now)
        if not page:
            return total
        after = page[-1][0]
        due = [r[0] for r in page if _is_due(r[1], r[2], r[3], now)]
        if due:
            done = await push_syncs(
                org_id=org_id, tenant_db_name=tenant_db_name, config=config, sync_ids=due
            )
            for name in ("claimed", "synced", "skipped", "failed", "deferred"):
                setattr(total, name, getattr(total, name) + getattr(done, name))
        if len(page) < SWEEP_PAGE:
            return total


async def run_commerce_sync_once() -> CommerceSweepResult:
    """One pass over every tenant with a commerce platform configured."""
    from app.database import control_session_factory
    from app.models.organization import Organization

    result = CommerceSweepResult()
    async with control_session_factory() as ctrl:
        tenants = list(
            (
                await ctrl.execute(
                    select(Organization.id, Organization.db_name, Organization.settings)
                )
            ).all()
        )
    for org_id, db_name, settings_blob in tenants:
        config = commerce_config(settings_blob)
        if config is None:
            continue
        result.tenants_scanned += 1
        try:
            summary = await sweep_tenant(org_id, db_name, config)
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            logger.warning("[commerce-sync] tenant %s failed: %s", db_name, type(exc).__name__)
            result.failures += 1
            continue
        result.claimed += summary.claimed
        result.synced += summary.synced
        result.skipped += summary.skipped
        result.failed += summary.failed
    return result


async def run_commerce_sync_loop() -> None:
    """Long-lived loop started in ``main.lifespan`` when FEOH_COMMERCE_SYNC_ENABLED."""
    from app.services.sweep_health import SWEEP_COMMERCE_SYNC, run_sweep_loop

    await run_sweep_loop(
        SWEEP_COMMERCE_SYNC,
        lambda: run_commerce_sync_once(),
        interval_seconds=settings.commerce_sync_interval_seconds,
        log=logger,
        log_prefix="[commerce-sync]",
    )
