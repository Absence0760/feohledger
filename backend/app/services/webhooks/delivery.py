"""Deliver a webhook: sign, POST, classify, retry with backoff, dead-letter.

A delivery is attempted up to ``MAX_ATTEMPTS`` times. Each failed attempt
schedules the next via exponential backoff (``BACKOFF_BASE_SECONDS * 2**(n-1)``);
once attempts are exhausted the row moves to the ``dead`` state (dead-letter) and
is never retried automatically — only an explicit redelivery (the management API)
re-queues it.

Local-first: delivery is a plain ``httpx`` POST in-process. No cloud queue is
required. The immediate attempt fires from ``dispatch.emit_event`` on the running
loop; ``run_webhook_delivery_loop`` is the durable retry backstop, gated behind
``FEOH_WEBHOOKS_ENABLED`` (OFF by default in local dev).

Because those two paths overlap in time (emit commits the row due-now, THEN
spawns the immediate attempt), a delivery is **claimed** before it is sent:
every load that is about to POST takes the row ``FOR UPDATE SKIP LOCKED``, so a
row already in flight elsewhere is skipped rather than POSTed twice, and
``attempt_count`` is a serialized read-modify-write. Without the claim the retry
budget was ``MAX_ATTEMPTS`` per *worker*, not per delivery.

PII discipline: the payloads themselves carry only invoice metadata (no bank /
tax / PAN fields — built in ``dispatch.py``), and the logs here record the
delivery id + status code + event type only, never the target URL's query string
or the response body.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.webhook import (
    DELIVERY_DEAD,
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.webhooks.rotation import (
    PREVIOUS_SIGNATURE_HEADER,
    previous_secret_if_live,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 10.0


def _next_backoff(attempt_count: int) -> timedelta:
    """Exponential backoff after ``attempt_count`` failed attempts."""
    return timedelta(seconds=BACKOFF_BASE_SECONDS * (2 ** max(0, attempt_count - 1)))


async def _post(
    target_url: str,
    body: bytes,
    signature: str,
    delivery: WebhookDelivery,
    previous_signature: str | None = None,
) -> int:
    """POST the signed body. Returns the HTTP status code; raises on transport
    error (timeout / connection refused) so the caller classifies it as a no-code
    failure."""
    headers = {
        "Content-Type": "application/json",
        # Always the CURRENT secret's signature — a receiver's existing contract
        # never changes meaning, even mid-rotation.
        "X-Webhook-Signature": signature,
        "X-Webhook-Event-Id": delivery.event_id,
        "X-Webhook-Event-Type": delivery.event_type,
    }
    if previous_signature is not None:
        # Only while a rotation overlap window is open. A receiver that accepts
        # either header rotates with zero dropped deliveries; one that reads
        # only the primary header simply ignores this.
        headers[PREVIOUS_SIGNATURE_HEADER] = previous_signature
    # Pinned: the socket opens to an address `url_guard` vetted in this same
    # connect, so a DNS record that flips after the pre-send check cannot
    # steer the signed payload to an internal host. A refusal raises
    # `WebhookTargetNotAllowed`, which `process_delivery` already handles.
    from app.services.webhooks.url_guard import resolve_pinned_addresses
    from app.utils.url_safety import pinned_async_client

    async with pinned_async_client(
        resolve=resolve_pinned_addresses, timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        resp = await client.post(target_url, content=body, headers=headers)
        return resp.status_code


async def process_delivery(db: AsyncSession, delivery: WebhookDelivery) -> WebhookDelivery:
    """Attempt one delivery and persist the outcome. Commits the row.

    Terminal rows (delivered / dead) are returned untouched — safe to call on
    any row, so the sweep and the immediate path share one code path.
    """
    if delivery.status in (DELIVERY_DELIVERED, DELIVERY_DEAD):
        return delivery

    sub = (
        await db.execute(
            select(WebhookSubscription).where(WebhookSubscription.id == delivery.subscription_id)
        )
    ).scalar_one_or_none()
    if sub is None or not sub.active:
        # Subscription gone or deactivated since enqueue — dead-letter it; we
        # have no secret/target to deliver with.
        delivery.status = DELIVERY_DEAD
        delivery.next_attempt_at = None
        await db.commit()
        return delivery

    # Sign byte-identical payload bytes (frozen at emit time → stable signature).
    from app.services.webhooks.signing import sign_payload

    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = sign_payload(sub.signing_secret, body)

    # Mid-rotation, the retiring secret signs a second header so a receiver that
    # hasn't switched yet still verifies. `previous_secret_if_live` owns the
    # expiry rule (an elapsed window reads as no previous secret at all), so a
    # row left stale can never keep a retired key signing.
    retiring = previous_secret_if_live(
        previous_secret=sub.previous_signing_secret,
        previous_expires_at=sub.previous_secret_expires_at,
    )
    previous_signature = sign_payload(retiring, body) if retiring else None

    from app.services.webhooks.url_guard import (
        WebhookTargetNotAllowed,
        ensure_public_webhook_target,
    )

    delivery.attempt_count += 1
    delivery.last_attempt_at = datetime.now(UTC)
    code: int | None = None
    ok = False
    try:
        # SSRF guard, re-checked at send time: the stored host is RE-resolved
        # so a DNS record that flipped to a private/loopback/metadata address
        # after create (TOCTOU / DNS rebinding) is refused, not POSTed to.
        await ensure_public_webhook_target(sub.target_url)
        code = await _post(sub.target_url, body, signature, delivery, previous_signature)
        ok = 200 <= code < 300
    except WebhookTargetNotAllowed:
        # PII-free by design: delivery id + event type only — never the URL,
        # host, or the address it resolved to.
        logger.warning(
            "webhook delivery refused: target not publicly routable: delivery=%s event=%s",
            delivery.id,
            delivery.event_type,
        )
    except Exception as exc:  # noqa: BLE001 — any transport error is a failed attempt
        logger.warning(
            "webhook delivery transport error: delivery=%s event=%s err=%s",
            delivery.id,
            delivery.event_type,
            type(exc).__name__,
        )

    delivery.response_code = code
    if ok:
        delivery.status = DELIVERY_DELIVERED
        delivery.next_attempt_at = None
    elif delivery.attempt_count >= MAX_ATTEMPTS:
        delivery.status = DELIVERY_DEAD
        delivery.next_attempt_at = None
        logger.warning(
            "webhook delivery dead-lettered: delivery=%s event=%s attempts=%s code=%s",
            delivery.id,
            delivery.event_type,
            delivery.attempt_count,
            code,
        )
    else:
        delivery.status = DELIVERY_FAILED
        delivery.next_attempt_at = datetime.now(UTC) + _next_backoff(delivery.attempt_count)

    await db.commit()
    return delivery


async def process_delivery_by_id(delivery_id: uuid.UUID) -> None:
    """Claim a delivery by id (own control-plane session) and process it once.

    Used by the fire-and-forget immediate attempt. The row is taken
    ``FOR UPDATE SKIP LOCKED``: ``emit_event`` commits the row with
    ``next_attempt_at = now()`` and only THEN spawns this attempt, so a sweep
    tick landing in that window sees a due row and would POST the same delivery
    a second time — a duplicate the customer's endpoint has to absorb, with two
    commits racing on ``attempt_count``/``next_attempt_at`` so the 5-attempt
    budget silently shrinks. Skipping a row another worker already holds means
    the other worker is sending it; there is nothing to do here.

    Best-effort: any failure is logged, never raised (the outcome lives on the
    row).
    """
    from app.database import control_session_factory

    try:
        async with control_session_factory() as db:
            delivery = (
                await db.execute(
                    select(WebhookDelivery)
                    .where(WebhookDelivery.id == delivery_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if delivery is None:
                # Already claimed by the sweep (or gone) — not our attempt.
                await db.rollback()
                return
            await process_delivery(db, delivery)
    except Exception:  # noqa: BLE001
        logger.exception("webhook immediate delivery failed: delivery=%s", delivery_id)


async def deliver_due(db: AsyncSession, *, limit: int = 100) -> int:
    """Process every delivery whose retry is due. Returns the count attempted.

    Two-phase and one row claimed at a time. The due ids are read UNLOCKED, then
    each is re-selected ``FOR UPDATE SKIP LOCKED`` with the due predicate
    re-applied, processed, and committed — which releases the claim before the
    next row is touched. Selecting the whole page ``FOR UPDATE`` up front would
    not hold: ``process_delivery`` commits per row, and that commit ends the
    transaction and drops the locks on every row still queued behind it.

    The claim is what makes ``MAX_ATTEMPTS`` mean 5 attempts rather than 5 per
    replica: ``attempt_count`` is now read-modify-written under the row lock,
    so two workers can't both increment from the same value, and a delivery
    already in flight (the immediate attempt from ``emit_event``, or another
    replica's tick) is skipped instead of POSTed twice.
    """
    now = datetime.now(UTC)
    due_ids = (
        (
            await db.execute(
                select(WebhookDelivery.id)
                .where(
                    WebhookDelivery.status.in_((DELIVERY_PENDING, DELIVERY_FAILED)),
                    WebhookDelivery.next_attempt_at.isnot(None),
                    WebhookDelivery.next_attempt_at <= now,
                )
                .order_by(WebhookDelivery.next_attempt_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    attempted = 0
    for delivery_id in due_ids:
        # The predicate is repeated under the lock deliberately: between the id
        # read and the claim another worker may have delivered or dead-lettered
        # the row. Postgres re-evaluates the qualification against the latest
        # tuple after locking, so a row that moved on is simply not returned.
        delivery = (
            await db.execute(
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.id == delivery_id,
                    WebhookDelivery.status.in_((DELIVERY_PENDING, DELIVERY_FAILED)),
                    WebhookDelivery.next_attempt_at.isnot(None),
                    WebhookDelivery.next_attempt_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if delivery is None:
            # Locked by another worker, or no longer due — end the transaction
            # so we don't hold a snapshot open across the rest of the page.
            await db.rollback()
            continue
        await process_delivery(db, delivery)
        attempted += 1
    return attempted


async def run_webhook_delivery_loop() -> None:
    """Background retry sweep — re-attempts due failed/pending deliveries.

    Gated behind ``FEOH_WEBHOOKS_ENABLED`` at the call site (main.lifespan), like
    every other background sweep. The immediate emit attempt handles the happy
    path; this loop is the durable backstop for retries + anything queued while
    no event loop was available.

    Body is the shared ``sweep_health.run_sweep_loop``. ``deliver_due`` returns
    the number of deliveries attempted, which the runner records as a count; a
    delivery's own failure is already durable per-row (``status`` +
    ``attempt_count``, queryable via ``GET /api/webhooks/deliveries``), so this
    sweep contributes no ``failures`` counter — only "did the tick itself
    raise". The old ``logger.exception`` is replaced for the same reason as in
    the dunning sweep: it attached ``str(exc)`` to the record.
    """
    from app.database import control_session_factory
    from app.services.sweep_health import SWEEP_WEBHOOK_DELIVERY, run_sweep_loop

    async def tick() -> int:
        async with control_session_factory() as db:
            return await deliver_due(db)

    await run_sweep_loop(
        SWEEP_WEBHOOK_DELIVERY,
        tick,
        interval_seconds=settings.webhooks_delivery_interval_seconds,
        log=logger,
        log_prefix="[webhooks]",
    )
