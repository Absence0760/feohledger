"""ERP dispatch — routes ERP sends to local in-process call or SQS/Lambda."""

import asyncio
import json
import uuid

import boto3

from app.config import settings

#: Strong references to in-flight local sends. `asyncio` keeps only a WEAK
#: reference to a running task, so a fire-and-forget task with no other
#: referent can be collected mid-await and disappear silently.
_dispatch_tasks: set[asyncio.Task] = set()


async def dispatch_erp(
    invoice_id: uuid.UUID,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Trigger ERP send via the configured mode (local or lambda)."""
    if settings.erp_mode == "lambda":
        # Off the event loop: boto3 is synchronous, so the client build (which
        # resolves the credential chain, possibly via IMDS) plus the SQS round
        # trip would occupy the loop for their full duration on a request path.
        # See `extraction_dispatch.dispatch_extraction` for the same call.
        await asyncio.to_thread(_send_to_sqs, invoice_id, org_id, actor_id)
    else:
        # Scheduled on the CALLER's loop, deliberately not a detached thread
        # running a second one. The send reaches `transition_invoice`, whose
        # notification / audit / webhook hooks resolve through the app-loop
        # engines in `app.database`; driving those from a foreign loop raises
        # and can poison the pool the request path shares (see
        # `database.dispatch_engine_scope`).
        #
        # Safe to run here because the whole send is `await`-only I/O: httpx to
        # the ERP and asyncpg to the DB, with `asyncio.sleep` between retries.
        # `extraction_dispatch` keeps its worker threads precisely because that
        # is NOT true of extraction.
        task = asyncio.create_task(
            _run_local(invoice_id, org_id, actor_id), name=f"erp-send-{invoice_id}"
        )
        _dispatch_tasks.add(task)
        task.add_done_callback(_dispatch_tasks.discard)


def _send_to_sqs(
    invoice_id: uuid.UUID,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Put ERP job on SQS for Lambda to pick up.

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
        QueueUrl=settings.sqs_erp_queue_url,
        MessageBody=json.dumps(
            {
                "invoice_id": str(invoice_id),
                "org_id": str(org_id),
                "actor_id": str(actor_id),
            }
        ),
        MessageGroupId=str(invoice_id),
    )


async def _run_local(
    invoice_id: uuid.UUID,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Run ERP send in-process with its own DB session and engine."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import _make_tenant_url
    from app.models.invoice import Invoice
    from app.models.organization import Organization
    from app.services.credential_crypto import CredentialCryptoError
    from app.services.erp import fail_erp_send_without_credentials, send_to_erp_internal

    ctrl_engine = create_async_engine(settings.database_url)
    ctrl_factory = async_sessionmaker(ctrl_engine, expire_on_commit=False)

    try:
        async with ctrl_factory() as ctrl_db:
            result = await ctrl_db.execute(select(Organization).where(Organization.id == org_id))
            org = result.scalar_one_or_none()
            if not org:
                return
            from app.services.provider_credentials import provider_config

            credentials_ok = True
            try:
                erp_config = await provider_config(org, "erp", db=ctrl_db)
            except CredentialCryptoError:
                erp_config, credentials_ok = None, False

        tenant_url = _make_tenant_url(org.db_name)
        tenant_engine = create_async_engine(tenant_url)
        tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)

        try:
            async with tenant_factory() as db:
                try:
                    result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
                    invoice = result.scalar_one_or_none()
                    if not invoice:
                        return
                    if not credentials_ok:
                        await fail_erp_send_without_credentials(db, invoice, actor_id=actor_id)
                        return

                    await send_to_erp_internal(
                        db, invoice, actor_id=actor_id, erp_config=erp_config
                    )
                except Exception:
                    await db.rollback()
        finally:
            # `finally`, not a trailing statement: the early `return` above and
            # any raise from the session's own exit would otherwise skip it and
            # leak a whole connection pool per send.
            await tenant_engine.dispose()
    finally:
        await ctrl_engine.dispose()
