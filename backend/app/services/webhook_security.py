"""Shared webhook-security primitives.

Project invariant #9 says every inbound webhook must
  - verify the provider's HMAC over the raw bytes, and
  - dedupe by event id so a re-delivery doesn't replay a one-time
    effect (a settled payment, a card rebate, an ERP status flip).

The payment adapter implements its own HMAC verification inline. This
module exposes the same primitives in a form the card- and ERP-webhook
handlers (which don't go through the adapter abstraction) can reuse.

Dedup uses Redis with a TTL so retries within the provider's
re-delivery window are caught, but the key set doesn't grow without
bound. The 72-hour default covers the widest processor retry window we
integrate with (Lithic retries a failed delivery on an exponential
backoff over ~3 days); a shorter TTL would let a late redelivery slip
past the dedup and replay the one-time effect.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from app.redis import get_redis

logger = logging.getLogger(__name__)

DEDUP_PREFIX = "webhook:event:"
# 3 days — must cover the LONGEST provider retry window (Lithic ~72h), or a
# redelivery after the key expires reprocesses the event (double rebate).
DEFAULT_DEDUP_TTL_SECONDS = 72 * 60 * 60


def verify_hmac_sha256(secret: str, raw_body: bytes, provided_hex: str | None) -> bool:
    """Constant-time HMAC-SHA256 check.

    Returns False (not raises) so the caller can decide whether to
    log the rejection silently or return an error. Empty / missing
    secret or missing signature always fail — there's no
    "no secret = anyone can post" mode.
    """
    if not secret or not provided_hex:
        return False
    try:
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    except Exception:  # noqa: BLE001 — defensive: malformed input must fail closed
        return False
    return hmac.compare_digest(expected, provided_hex)


async def is_event_already_processed(
    provider: str, event_id: str, *, ttl_seconds: int = DEFAULT_DEDUP_TTL_SECONDS
) -> bool:
    """Record an event id (provider + event) and return True if it
    was already recorded within the TTL window.

    The set-if-not-exists semantics mean callers can do::

        if await is_event_already_processed("lithic", evt.id):
            return  # silent ack, same response as first delivery

    A `False` return means the caller is the *first* to claim this
    event id — they must drive the side effect now.
    """
    if not event_id:
        # Empty / missing event id can't be deduped — treat as "always
        # first delivery" but log so an operator can spot a provider
        # that's stopped including event ids.
        logger.info("[webhook-dedup] missing event id for provider=%s", provider)
        return False
    r = await get_redis()
    key = f"{DEDUP_PREFIX}{provider}:{event_id}"
    # SET key value NX EX <ttl> — returns OK if set, nil if key already
    # existed. redis-py's `set` returns True / None for that.
    was_set = await r.set(key, "1", nx=True, ex=ttl_seconds)
    return not was_set


async def release_event_claim(provider: str, event_id: str) -> None:
    """Release a dedup claim made by `is_event_already_processed`.

    A Redis `SET NX` claim is durable the instant it's written — but the side
    effect it guards (a rebate mint, a charge transition) isn't durable until
    the DB transaction commits. If that commit rolls back AFTER the claim, the
    event id would stay claimed for the full TTL, so the provider's retry is
    silently deduped and the one-time effect is lost forever. Callers must
    release the claim on any post-claim failure so the retry can reprocess.

    Best-effort: an empty event id is a no-op (nothing was claimed), and a Redis
    hiccup here just falls back to TTL expiry rather than raising.
    """
    if not event_id:
        return
    try:
        r = await get_redis()
        await r.delete(f"{DEDUP_PREFIX}{provider}:{event_id}")
    except Exception:  # noqa: BLE001 — release is best-effort; never raise
        logger.warning("[webhook-dedup] failed to release claim for provider=%s", provider)


async def event_claim_exists(provider: str, event_id: str) -> bool:
    """Read-only: is a claim made by `is_event_already_processed` still live?

    Never claims. For a caller migrating its claim-key format, which must still
    honour claims written under the old format until their TTL has elapsed.
    An empty event id was never claimed, so it reads as absent.
    """
    if not event_id:
        return False
    r = await get_redis()
    return bool(await r.exists(f"{DEDUP_PREFIX}{provider}:{event_id}"))


def extract_signature_header(headers: dict, *candidates: str) -> str | None:
    """Pull the first present signature header out of a request.

    Different providers use different header names (Lithic:
    `Webhook-Signature`, Stripe-style: `Stripe-Signature`,
    generic: `X-Signature`). Headers are case-insensitive in HTTP,
    so we walk the dict comparing lower-cased names.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None
