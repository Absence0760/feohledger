"""AWS Lambda handler for audit logging.

Triggered by SQS messages when audit_mode = "lambda".
Each message contains { correlation_id, organization_id, actor_id, action,
                        entity_type, entity_id, details }.

The producer (``services/audit_dispatch``) also puts a ``tenant_db_name`` on the
body. **This handler ignores it.** The database a tenant's audit row lands in is
re-derived here from ``organization_id`` against the control plane, the same way
``extraction_lambda`` and ``erp_lambda`` do — so the one input that chooses a
database is a column on a resolved ``Organization`` row, never a string off the
queue. That is the project's tenant-isolation invariant stated at the point of
use rather than at the point of publish, and it also means a hostile or stale
``tenant_db_name`` cannot steer the connection at all. The field stays on the
wire for compatibility with in-flight messages during a deploy; nothing reads it.

Deploy this module as the Lambda handler: app.services.audit_lambda.handler
"""

import asyncio
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db_engine import create_async_engine

# `app.tenant_url` imports nothing (bar `os`), so it is safe on this
# dotenv-free path where `app.database` — which reaches `app.config` — is not.
from app.tenant_url import control_url_from_env, make_tenant_url


def handler(event, context):
    """AWS Lambda entry point — processes SQS batch."""
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        asyncio.get_event_loop().run_until_complete(_process_message(body))
    return {"statusCode": 200}


async def _process_message(body: dict) -> None:
    organization_id = uuid.UUID(body["organization_id"])
    db_url = control_url_from_env()

    # Look up the org to find the tenant DB name — the same first step
    # `extraction_lambda` / `erp_lambda` take. An organization that does not
    # exist RAISES rather than returning: unlike theirs, this message is not a
    # request to re-do work that can be re-requested, it IS the only copy of an
    # audit event, and the trail is append-only. Raising sends it back to SQS and
    # ultimately to the dead-letter queue, where it can be replayed; swallowing
    # it would lose the row.
    control_engine = create_async_engine(db_url)
    control_factory = async_sessionmaker(control_engine, expire_on_commit=False)
    try:
        async with control_factory() as ctrl_db:
            from app.models.organization import Organization

            result = await ctrl_db.execute(
                select(Organization.db_name).where(Organization.id == organization_id)
            )
            db_name = result.scalar_one_or_none()
    finally:
        await control_engine.dispose()

    if not db_name:
        # The id is a UUID, not PII — and naming it is what makes a dead-lettered
        # message diagnosable.
        raise ValueError(f"Organization {organization_id} not found; cannot route audit row")

    # Connect directly to the tenant DB
    tenant_url = make_tenant_url(db_url, db_name)
    tenant_engine = create_async_engine(tenant_url)
    tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)

    async with tenant_factory() as db:
        try:
            from app.services.audit import log_action

            await log_action(
                db,
                correlation_id=uuid.UUID(body["correlation_id"]),
                organization_id=organization_id,
                actor_id=uuid.UUID(body["actor_id"]) if body.get("actor_id") else None,
                action=body["action"],
                entity_type=body["entity_type"],
                entity_id=uuid.UUID(body["entity_id"]),
                details=body.get("details"),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await tenant_engine.dispose()
