"""Extraction dispatch — routes extraction to local background task or SQS/Lambda.

Local mode uses a single worker thread with a job queue so that extractions
run one at a time.  This prevents concurrent uploads from triggering parallel
AI API calls that get rate-limited (429) and fail.
"""

import asyncio
import json
import logging
import queue
import threading
import uuid
from dataclasses import asdict, dataclass

import boto3

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractionOptions:
    """Per-job extraction modifiers, carried across BOTH dispatch modes.

    Every field defaults to ``False`` — the unchanged ingest behaviour — so an
    older in-flight job (a local queue tuple that predates this field, an SQS
    message written by an older deploy) decodes to exactly what it used to do.
    New options belong here rather than as extra positional tuple slots, so the
    queue tuple's shape stops changing.

    ``skip_vendor_match`` / ``suppress_auto_approve`` are documented on
    ``services.extraction.run_extraction``; both exist for RE-extraction of an
    invoice that was already triaged once (the supplier-portal resubmit).
    """

    skip_vendor_match: bool = False
    suppress_auto_approve: bool = False

    @classmethod
    def from_payload(cls, payload: dict | None) -> "ExtractionOptions":
        """Decode the SQS message body. An absent key is ``False``."""
        data = payload or {}
        return cls(
            skip_vendor_match=bool(data.get("skip_vendor_match", False)),
            suppress_auto_approve=bool(data.get("suppress_auto_approve", False)),
        )

    def as_payload(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Local extraction queue — a small pool of worker threads processes jobs
# concurrently (default 3).  Keeps throughput high for bulk uploads while
# staying under typical AI-provider rate limits.
# ---------------------------------------------------------------------------

_WORKER_COUNT = 3
# `(invoice_id, org_id, actor_id, options)`. The 4th slot is optional on read —
# see `_extraction_worker` — so a job already sitting in the queue when this
# module is reloaded (dev auto-reload) still drains instead of raising.
_job_queue: queue.Queue[tuple] = queue.Queue()
_worker_threads: list[threading.Thread] = []
_worker_lock = threading.Lock()


def _ensure_workers() -> None:
    """Start worker threads, replacing any that have exited."""
    with _worker_lock:
        # Remove dead threads
        _worker_threads[:] = [t for t in _worker_threads if t.is_alive()]
        # Top up to _WORKER_COUNT
        while len(_worker_threads) < _WORKER_COUNT:
            t = threading.Thread(target=_extraction_worker, daemon=True)
            t.start()
            _worker_threads.append(t)


def _extraction_worker() -> None:
    """Drain the job queue, processing one extraction at a time.

    Exits after 120 s of idle time so we don't keep a thread alive when
    there's no upload activity.  The next ``dispatch_extraction`` call
    will restart it.
    """
    logger.info("[extraction] Worker thread started")
    while True:
        try:
            job = _job_queue.get(timeout=120)
        except queue.Empty:
            logger.info("[extraction] Worker idle — exiting")
            break  # idle timeout — exit

        # Tolerant unpack: a job enqueued before the options slot existed is a
        # 3-tuple, and must not crash the worker (which would strand the
        # invoice in `pending` with no `failed` transition).
        invoice_id, org_id, actor_id = job[0], job[1], job[2]
        options = (
            job[3]
            if len(job) > 3 and isinstance(job[3], ExtractionOptions)
            else (ExtractionOptions())
        )

        logger.info("[extraction] Worker picked up job for invoice %s", invoice_id)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_local(invoice_id, org_id, actor_id, options))
        except Exception as exc:
            # Class name only — a pipeline exception can carry extracted PII.
            logger.error(
                "[extraction] Unexpected error for invoice %s: %s",
                invoice_id,
                exc.__class__.__name__,
            )
            try:
                loop.run_until_complete(
                    _mark_failed(invoice_id, org_id, "Extraction crashed unexpectedly")
                )
            except Exception as mark_exc:
                logger.error(
                    "[extraction] Failed to mark invoice %s failed after crash: %s",
                    invoice_id,
                    mark_exc.__class__.__name__,
                )
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def dispatch_extraction(
    invoice_id: uuid.UUID,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    skip_vendor_match: bool = False,
    suppress_auto_approve: bool = False,
) -> None:
    """Trigger extraction via the configured mode (local or lambda).

    The two keyword flags are carried unchanged to ``run_extraction`` in BOTH
    modes — through the local queue tuple and through the SQS message body —
    and both default to today's behaviour. See ``ExtractionOptions``.
    """
    options = ExtractionOptions(
        skip_vendor_match=skip_vendor_match,
        suppress_auto_approve=suppress_auto_approve,
    )
    if settings.extraction_mode == "lambda":
        # boto3 is synchronous: building the client resolves the credential
        # chain (which can reach IMDS) and `send_message` is a full HTTPS round
        # trip to SQS. This coroutine is awaited straight from the invoice
        # upload route and from the public email-intake webhook, so running it
        # inline occupies the event loop for that whole window and every other
        # in-flight request on the worker waits behind it. Same offload
        # `services/storage` and the audit-shipping adapters already use.
        await asyncio.to_thread(_send_to_sqs, invoice_id, org_id, actor_id, options)
    else:
        _job_queue.put((invoice_id, org_id, actor_id, options))
        _ensure_workers()


# ---------------------------------------------------------------------------
# SQS path (Lambda mode)
# ---------------------------------------------------------------------------


def _send_to_sqs(
    invoice_id: uuid.UUID,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    options: ExtractionOptions | None = None,
) -> None:
    """Put extraction job on SQS for Lambda to pick up.

    **Blocking — never call this from a coroutine directly.** boto3 is
    synchronous: constructing the client resolves the credential chain (which
    can reach the instance-metadata endpoint) and ``send_message`` is a full
    HTTPS round trip. The caller above hands it to ``asyncio.to_thread`` so it
    never occupies the event loop, matching ``services/storage``'s
    ``_put_object``. Guarded by ``tests/test_sqs_dispatch_nonblocking.py``.
    """
    client = boto3.client(
        "sqs",
        endpoint_url=settings.aws_endpoint_url or settings.s3_endpoint_url,  # LocalStack when set
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
    )
    client.send_message(
        QueueUrl=settings.sqs_extraction_queue_url,
        MessageBody=json.dumps(
            {
                "invoice_id": str(invoice_id),
                "org_id": str(org_id),
                "actor_id": str(actor_id),
                # Additive + flat, never nested: an older Lambda consumer
                # ignores the keys it doesn't know, and a newer one reads them
                # with `.get(..., False)` so a message from an older producer
                # decodes to the unchanged behaviour.
                **(options or ExtractionOptions()).as_payload(),
            }
        ),
        MessageGroupId=str(invoice_id),
    )


# ---------------------------------------------------------------------------
# Local execution
# ---------------------------------------------------------------------------


async def _run_local(
    invoice_id: uuid.UUID,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    options: ExtractionOptions | None = None,
) -> None:
    """Run extraction in-process with its own DB session and engine.

    This runs in a worker thread with its own event loop, so it must create
    fresh engines — the module-level ones in `app.database` belong to the app's
    loop, and an asyncpg connection cannot cross loops.

    Creating them here was never enough on its own, though. The extraction
    reaches `transition_invoice`, whose notification / audit / webhook hooks
    each open their OWN control-plane session (and `dispatch_audit` its own
    tenant engine) by reaching for those module-level globals — code this
    function never calls directly and cannot pass a session to. Those calls ran
    on the app's engines from this foreign loop, which raises and can poison
    the connection pool the request path shares.

    `dispatch_engine_scope` is what closes that: it binds these loop-local
    engines for everything running underneath, so the hooks resolve to them
    without knowing they exist. See `database.dispatch_engine_scope`.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import _make_tenant_url, dispatch_engine_scope
    from app.models.invoice import Invoice
    from app.models.organization import Organization
    from app.services.extraction import run_extraction

    # Create a fresh control-plane engine for this thread.
    # Minimal pool (1 conn) — each worker only runs one job at a time and
    # we don't want 3 workers × 2 engines × default pool (15) to exhaust
    # PostgreSQL's max_connections (100).
    ctrl_engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    ctrl_factory = async_sessionmaker(ctrl_engine, expire_on_commit=False)

    try:
        # ctrl_db resolves the Organization (a control-plane row) so the
        # tenant slug and org settings are known before the tenant engine is
        # built. The ExtractionUsage meter is NOT written here — it is a tenant
        # table and run_extraction writes it through the tenant session.
        async with ctrl_factory() as ctrl_db:
            result = await ctrl_db.execute(select(Organization).where(Organization.id == org_id))
            org = result.scalar_one_or_none()
            if not org:
                logger.warning("[extraction] Organization %s not found", org_id)
                return
            # Hand the connection back NOW. `ctrl_engine` is a ONE-connection
            # pool and this session would otherwise hold it (its autobegun
            # read transaction) for the whole extraction — so every control-plane
            # session opened underneath (`control_session_factory()` in the
            # notification / audit hooks, and the AI-read allowance gate that
            # must run before the model call) waited out the 30 s pool timeout
            # and failed. `org`'s loaded attributes stay readable once detached.
            await ctrl_db.close()

            # Create a fresh tenant engine for this thread
            tenant_url = _make_tenant_url(org.db_name)
            tenant_engine = create_async_engine(tenant_url, pool_size=1, max_overflow=0)
            tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)

            try:
                # Bind these loop-local engines for the whole extraction, so the
                # transition hooks (notifications / audit / webhooks) resolve to
                # them instead of the app-loop globals. `tenant_engines` seeds
                # the scope with the engine we already opened, so `dispatch_audit`
                # writing this tenant's audit row reuses it rather than standing
                # up a second pool for the same database.
                async with (
                    dispatch_engine_scope(
                        control_sessionmaker=ctrl_factory,
                        tenant_engines={org.db_name: tenant_engine},
                    ),
                    tenant_factory() as db,
                ):
                    try:
                        result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
                        invoice = result.scalar_one_or_none()
                        if not invoice:
                            logger.warning("[extraction] Invoice %s not found", invoice_id)
                            return

                        logger.info(
                            "[extraction] Starting extraction for invoice %s, file_key=%s",
                            invoice_id,
                            invoice.file_key,
                        )
                        opts = options or ExtractionOptions()
                        await run_extraction(
                            db,
                            invoice,
                            actor_id=actor_id,
                            org_settings=org.settings,
                            skip_vendor_match=opts.skip_vendor_match,
                            suppress_auto_approve=opts.suppress_auto_approve,
                        )
                        logger.info(
                            "[extraction] Completed extraction for invoice %s, status=%s",
                            invoice_id,
                            invoice.status,
                        )
                    except Exception as exc:
                        # Class name only — never the raw message. An extraction
                        # exception (vision/OCR SDK, GL validation, dup-detect)
                        # can carry invoice PII (vendor tax id / bank / address),
                        # and no log-redaction filter is installed here.
                        logger.error(
                            "[extraction] ERROR for invoice %s: %s",
                            invoice_id,
                            exc.__class__.__name__,
                        )
                        await db.rollback()
                        # Ensure the invoice doesn't stay stuck in 'pending' —
                        # run_extraction's own error handler transitions to 'failed',
                        # but if *that* handler itself blew up we need a fallback.
                        await _fail_invoice_safely(db, invoice_id, actor_id, str(exc))
            finally:
                await tenant_engine.dispose()
    finally:
        await ctrl_engine.dispose()


async def _fail_invoice_safely(
    db, invoice_id: uuid.UUID, actor_id: uuid.UUID | None, error: str
) -> None:
    """Best-effort transition to 'failed' if the invoice is still in 'pending'.

    Called as a fallback when ``run_extraction``'s own error handler raised.
    Swallows all exceptions so it never disrupts the caller's cleanup path.
    """
    try:
        from sqlalchemy import select

        from app.models.invoice import Invoice, InvoiceStatus
        from app.services.workflow_engine import transition_invoice

        result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
        inv = result.scalar_one_or_none()
        if inv and inv.status == InvoiceStatus.pending:
            await transition_invoice(
                db,
                inv,
                InvoiceStatus.failed,
                actor_id=actor_id,
                action_name="invoice.extraction_failed_fallback",
                details={"error": error},
            )
            await db.commit()
            logger.info("[extraction] Fallback: marked invoice %s as failed", invoice_id)
    except Exception as exc:
        logger.error(
            "[extraction] Fallback fail-invoice path errored for invoice %s: %s",
            invoice_id,
            exc.__class__.__name__,
        )


async def _mark_failed(invoice_id: uuid.UUID, org_id: uuid.UUID, reason: str) -> None:
    """Transition an invoice to 'failed' using a fresh DB session.

    Used by the worker when a job times out or crashes outside ``_run_local``.
    Runs under its own `dispatch_engine_scope` for the same reason `_run_local`
    does — this is still the worker's foreign loop, and `transition_invoice`
    fires the same hooks that reach for the app-loop engines.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import _make_tenant_url, dispatch_engine_scope
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.organization import Organization

    ctrl_engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    ctrl_factory = async_sessionmaker(ctrl_engine, expire_on_commit=False)
    try:
        async with ctrl_factory() as ctrl_db:
            result = await ctrl_db.execute(select(Organization).where(Organization.id == org_id))
            org = result.scalar_one_or_none()
            if not org:
                return

        tenant_url = _make_tenant_url(org.db_name)
        tenant_engine = create_async_engine(tenant_url, pool_size=1, max_overflow=0)
        try:
            tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)
            async with (
                dispatch_engine_scope(
                    control_sessionmaker=ctrl_factory,
                    tenant_engines={org.db_name: tenant_engine},
                ),
                tenant_factory() as db,
            ):
                from app.services.workflow_engine import transition_invoice

                result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
                invoice = result.scalar_one_or_none()
                if invoice and invoice.status == InvoiceStatus.pending:
                    await transition_invoice(
                        db,
                        invoice,
                        InvoiceStatus.failed,
                        actor_id=None,
                        action_name="invoice.extraction_timed_out",
                        details={"reason": reason},
                    )
                    await db.commit()
                    logger.info("[extraction] Marked invoice %s as failed: %s", invoice_id, reason)
        finally:
            await tenant_engine.dispose()
    except Exception as exc:
        logger.error(
            "[extraction] _mark_failed errored for invoice %s: %s",
            invoice_id,
            exc.__class__.__name__,
        )
    finally:
        await ctrl_engine.dispose()
