"""Audit dispatch — routes audit log writes to local in-process or SQS/Lambda."""

import asyncio
import json
import logging
import uuid

import boto3
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


async def dispatch_audit(
    db: AsyncSession,
    *,
    correlation_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    details: dict | None = None,
):
    """Write audit log locally or dispatch to SQS for Lambda processing."""
    if settings.audit_mode == "lambda":
        tenant_db_name = await _resolve_tenant_db_name(organization_id)
        # Off the event loop — see `_send_to_sqs`. Every audited mutation in the
        # app reaches this line in lambda mode.
        await asyncio.to_thread(
            _send_to_sqs,
            tenant_db_name=tenant_db_name,
            correlation_id=correlation_id,
            organization_id=organization_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
        )
    else:
        from app.services.audit import log_action

        await log_action(
            db,
            correlation_id=correlation_id,
            organization_id=organization_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
        )


async def _resolve_tenant_db_name(organization_id: uuid.UUID) -> str:
    """Look up the tenant DB name from the control plane."""
    from app.database import control_session_factory
    from app.models.organization import Organization

    async with control_session_factory() as ctrl_db:
        result = await ctrl_db.execute(
            select(Organization.db_name).where(Organization.id == organization_id)
        )
        db_name = result.scalar_one_or_none()
        if not db_name:
            raise ValueError(f"Organization {organization_id} not found")
        return db_name


async def _write_auth_audit(
    *,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    entity_id: uuid.UUID | None = None,
    entity_type: str = "auth",
    details: dict | None = None,
) -> None:
    """The actual control-plane-originated audit write. **Raises on failure.**

    Split out from :func:`dispatch_auth_audit` so a caller that runs this OFF
    the response path (:func:`queue_auth_audit`) can see — and escalate — a
    failure that the fire-and-forget wrapper would otherwise swallow. Nothing
    calls this directly except the wrappers in this module.
    """
    correlation_id = uuid.uuid4()
    # AuditLog.entity_id is nullable, but most writers pass one. Fall back to
    # the correlation_id so dashboards that GROUP BY entity_id still work.
    _entity_id = entity_id or correlation_id
    if settings.audit_mode == "lambda":
        tenant_db_name = await _resolve_tenant_db_name(organization_id)
        # Off the event loop — see `_send_to_sqs`. This is the LOGIN path
        # (`api/auth.py` writes an auth audit row on every attempt), the
        # most concurrent surface in the app.
        await asyncio.to_thread(
            _send_to_sqs,
            tenant_db_name=tenant_db_name,
            correlation_id=correlation_id,
            organization_id=organization_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=_entity_id,
            details=details,
        )
        return

    # Local mode: open a tenant session and write directly.
    from app.database import get_tenant_engine
    from app.services.audit import log_action

    tenant_db_name = await _resolve_tenant_db_name(organization_id)
    engine = get_tenant_engine(tenant_db_name)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as tenant_db:
        try:
            await log_action(
                tenant_db,
                correlation_id=correlation_id,
                organization_id=organization_id,
                actor_id=actor_id,
                action=action,
                entity_type=entity_type,
                entity_id=_entity_id,
                details=details,
            )
            await tenant_db.commit()
        except Exception:
            await tenant_db.rollback()
            raise


async def dispatch_auth_audit(
    *,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    entity_id: uuid.UUID | None = None,
    entity_type: str = "auth",
    details: dict | None = None,
) -> None:
    """Write a control-plane-originated audit entry into the tenant audit_log.

    Some endpoints run on the control-plane session but the AuditLog table
    lives on the tenant DB. This helper resolves the tenant DB from the
    organization_id and opens its own short-lived session to write the row.

    ``entity_type`` defaults to ``"auth"`` (its original, auth-event use), but
    callers writing a domain event from a control-plane session can pass the
    matching type — e.g. ``"organization"`` for a branding mutation — so the
    trail is queryable consistently regardless of which write path produced the
    row (a SOX auditor filtering on ``entity_type`` sees every
    ``organization.*`` change, not only the ones written via ``dispatch_audit``).

    Any exception is caught + logged at WARNING so the calling endpoint never
    fails because of an audit-infrastructure blip (Redis blocklist degrades the
    same way). SOC 2 wants writes hardened *and* observable — but available
    first.

    **This awaits a tenant-DB round trip on the caller's path.** On a branch
    whose response time must not depend on whether the account exists, use
    :func:`queue_auth_audit` instead.
    """
    try:
        await _write_auth_audit(
            organization_id=organization_id,
            actor_id=actor_id,
            action=action,
            entity_id=entity_id,
            entity_type=entity_type,
            details=details,
        )
    except Exception as exc:  # pragma: no cover — defensive
        # Class name only. `details` routinely carries the submitted email
        # address, and a driver error can echo the parameters it choked on —
        # so the exception is never rendered into the log (same rule as
        # `services/post_commit`).
        logger.warning(
            "auth audit dispatch failed: action=%s org=%s actor=%s err=%s",
            action,
            organization_id,
            actor_id,
            exc.__class__.__name__,
        )


async def record_auth_audit_or_raise(
    *,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    entity_id: uuid.UUID | None = None,
    entity_type: str = "auth",
    details: dict | None = None,
) -> None:
    """Write a control-plane-originated audit row and **raise** if it fails.

    For an operator tool, not a request path: there an unrecorded change is
    worse than a refused one, so the caller writes this row first and makes its
    change only once the row exists (`services/sso_break_glass`). Request paths
    keep :func:`dispatch_auth_audit`, which must never fail the request.
    """
    await _write_auth_audit(
        organization_id=organization_id,
        actor_id=actor_id,
        action=action,
        entity_id=entity_id,
        entity_type=entity_type,
        details=details,
    )


# Strong references to in-flight audit writes. Without this the only reference
# is the local in `queue_auth_audit` and the loop's own registry is weak, so a
# row could be lost to garbage collection mid-write. Mirrors
# `services/post_commit`'s `_tasks` and `services/webhooks/dispatch`.
_pending_auth_audits: set[asyncio.Task] = set()


def _finish_auth_audit(task: asyncio.Task) -> None:
    _pending_auth_audits.discard(task)
    if not task.cancelled():
        task.exception()


async def _guarded_auth_audit(**kwargs) -> None:
    try:
        await _write_auth_audit(**kwargs)
    except Exception as exc:  # noqa: BLE001 — nothing above us to propagate to
        # ERROR, not WARNING: this is SOX evidence for an authentication event
        # and nothing downstream retries it, so a silent loss is exactly what
        # must not happen. PII-free — action + org id (a UUID) + the exception
        # CLASS; never `details`, which carries the submitted address.
        logger.error(
            "auth audit write failed off the response path: action=%s org=%s err=%s",
            kwargs.get("action"),
            kwargs.get("organization_id"),
            exc.__class__.__name__,
        )


def queue_auth_audit(
    *,
    organization_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    action: str,
    entity_id: uuid.UUID | None = None,
    entity_type: str = "auth",
    details: dict | None = None,
) -> None:
    """Schedule an auth audit row to be written AFTER the caller responds.

    **Why this exists.** ``dispatch_auth_audit`` resolves the tenant DB from the
    control plane and commits a row inline — a whole DB round trip on the
    caller's path. On ``/auth/login`` that round trip only happens when the
    submitted address *has* an organization, so a known address (wrong
    password, no password, deactivated) was measurably slower than an unknown
    one: an account-existence oracle around the very bcrypt cost
    ``dummy_verify`` exists to equalize. Dropping the row is worse than the
    leak, and padding the fast path is masking; moving the write off the
    response path is the fix that costs neither branch anything.

    **Why not** :mod:`app.services.post_commit`. That queue fires from
    SQLAlchemy's ``after_commit``. A failed login raises ``HTTPException``, so
    ``get_control_db`` rolls its session back — ``after_rollback`` drains and
    DISCARDS the queue by design ("do not tell anyone about a change that never
    happened"). A row queued there would never be written. The trigger here is
    therefore the event loop itself: the write is spawned as a task and runs
    while the response is already on its way out.

    ``organization_id`` may be ``None`` — the tenant DB is resolved from it, so
    there is nowhere to write and the row is dropped (visibly, at INFO). Taking
    ``None`` rather than making the caller branch is the point: both login
    failure branches call this unconditionally, so they stay structurally
    identical.

    Never raises, never awaits.
    """
    if organization_id is None:
        # PII-free: the action only. Not the submitted address — that is the
        # thing an unknown-account log line must not record.
        logger.info("auth audit skipped, no organization to route to: action=%s", action)
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        # No loop to outlive us. Every real caller is an async request handler,
        # so this is a programming error rather than an operational one — but
        # a dropped auth row is loud either way.
        logger.error(
            "auth audit could not be scheduled, no running event loop: action=%s org=%s",
            action,
            organization_id,
        )
        return

    task = loop.create_task(
        _guarded_auth_audit(
            organization_id=organization_id,
            actor_id=actor_id,
            action=action,
            entity_id=entity_id,
            entity_type=entity_type,
            details=details,
        ),
        name=f"auth-audit-{action}",
    )
    _pending_auth_audits.add(task)
    task.add_done_callback(_finish_auth_audit)


# How long a graceful shutdown waits for queued auth audit rows to land. The
# write is a control-plane lookup plus a tenant INSERT and COMMIT — generous at
# five seconds, and BOUNDED on purpose: a hung write must cost the shutdown a
# known amount, not block it indefinitely. An orchestrator that gives up waiting
# and SIGKILLs loses every remaining row instead of just the stuck one.
AUTH_AUDIT_DRAIN_TIMEOUT_SECONDS = 5.0


async def drain_auth_audits(*, timeout: float | None = None) -> int:
    """Await every queued auth audit write currently in flight.

    For TESTS that assert on the row right after the response, and for the
    lifespan shutdown, which is what makes the documented guarantee ("written
    shortly after the response, or reported at ERROR") actually hold when the
    process is stopped cleanly. Production request handlers never call it — not
    waiting is the whole point.

    ``timeout`` bounds the wait. Past it the remaining writes are cancelled and
    the count is logged at WARNING and returned, so an abandoned row is as
    visible as a failed one. ``None`` (the test default) waits indefinitely.
    """

    async def _drain() -> None:
        while _pending_auth_audits:
            await asyncio.gather(*list(_pending_auth_audits), return_exceptions=True)

    if timeout is None:
        await _drain()
        return 0

    # `asyncio.wait` rather than `wait_for`: on expiry it returns instead of
    # cancelling, so the writes are still countable. `wait_for` cancels the
    # gather, whose done callbacks then empty `_pending_auth_audits` before the
    # count can be read — reporting `abandoned=0` for a drain that abandoned
    # work is worse than not reporting at all.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _pending_auth_audits:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.wait(list(_pending_auth_audits), timeout=remaining)

    abandoned = len(_pending_auth_audits)
    if abandoned:
        for task in list(_pending_auth_audits):
            task.cancel()
        # PII-free: a count and a bound, nothing about who was signing in.
        logger.warning(
            "auth audit drain timed out after %ss; abandoned=%d row(s)", timeout, abandoned
        )
    return abandoned


def _send_to_sqs(
    *,
    tenant_db_name: str,
    correlation_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    details: dict | None,
) -> None:
    """Put audit event on SQS for Lambda to pick up.

    **Blocking — never call this from a coroutine directly.** boto3 is
    synchronous: constructing the client resolves the credential chain (which
    can reach the instance-metadata endpoint) and ``send_message`` is a full
    HTTPS round trip. Both callers above hand it to ``asyncio.to_thread`` so it
    never occupies the event loop, matching ``services/storage``'s
    ``_put_object`` and the audit-shipping adapters. Guarded by
    ``tests/test_sqs_dispatch_nonblocking.py``.
    """
    client = boto3.client(
        "sqs",
        endpoint_url=settings.aws_endpoint_url or settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
    )
    client.send_message(
        QueueUrl=settings.sqs_audit_queue_url,
        MessageBody=json.dumps(
            {
                "tenant_db_name": tenant_db_name,
                "correlation_id": str(correlation_id),
                "organization_id": str(organization_id),
                "actor_id": str(actor_id) if actor_id else None,
                "action": action,
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "details": details,
            }
        ),
        MessageGroupId=str(correlation_id),
    )
