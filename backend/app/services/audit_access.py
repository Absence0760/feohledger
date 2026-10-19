"""Access-control auditing + field-level change history (SOX).

Two thin helpers layered on the existing ``dispatch_audit`` pipeline — they do
NOT reimplement it. Both write through ``dispatch_audit`` so view-events and
field diffs land in the same tenant ``audit_log`` → WORM-shipper trail as every
other audit row.

``log_access`` records *who VIEWED what* — the SOX access-control requirement
(log reads of regulated records, not just writes). The detail payload records
the entity id and the **field-names** accessed, never the values: no tax id,
bank number, or PAN ever enters ``audit_log.details`` (PII-out-of-logs
invariant).

``build_field_diff`` produces the structured before/after shape persisted on
mutation rows: ``{"field": {"old": ..., "new": ...}}``. Money values are
serialised as **string-Decimal** (never float) so the "money is exact"
invariant holds inside the JSONB too.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.audit_dispatch import dispatch_audit


def _jsonable(value: Any) -> Any:
    """Coerce a value into something JSONB-safe and invariant-clean.

    Money is kept exact by serialising ``Decimal`` to its string form (never
    ``float``). UUIDs / dates fall back to ``str``. ``None`` and JSON scalars
    pass through untouched.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        # A float here is almost always a mis-typed money value; keep exactness
        # by routing it through Decimal's string form rather than emitting a
        # lossy float into the audit trail.
        return str(Decimal(str(value)))
    if isinstance(value, (list, tuple)):
        # Recurse rather than stringify the whole container: a list field (e.g.
        # the GL codes on a line-item edit) belongs in the JSONB as a real list,
        # not as the opaque repr `"['6100']"` that nothing downstream can read.
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def build_field_diff(
    before: dict[str, Any],
    after: dict[str, Any],
    fields: list[str],
) -> dict[str, dict[str, Any]]:
    """Return ``{field: {"old": x, "new": y}}`` for fields that changed.

    Only fields whose value actually changed are included, so an approval that
    corrected one field doesn't bloat the audit row with eight unchanged ones.
    NULL and a blank string count as the same value.
    Money values are serialised as string-Decimal (never float).
    """
    diff: dict[str, dict[str, Any]] = {}
    for field in fields:
        old = before.get(field)
        new = after.get(field)
        if _is_blank(old) and _is_blank(new):
            # NULL and "" are the same absence of a value. Recording one
            # becoming the other wrote an edit nobody made onto the trail.
            continue
        if old != new:
            diff[field] = {"old": _jsonable(old), "new": _jsonable(new)}
    return diff


async def log_access(
    db: AsyncSession,
    *,
    user: User,
    organization_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    correlation_id: uuid.UUID | None = None,
    fields: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write a ``<entity_type>.viewed`` audit row for a sensitive read.

    ``fields`` records the *names* of the regulated fields surfaced by the read
    (e.g. ``["tax_id", "bank_account"]``) — never their values. ``extra`` may
    carry non-PII scope metadata (e.g. export row count). The caller is
    responsible for committing: a GET has no business transaction, so callers
    either commit the request session or, for control-plane-session endpoints,
    use the self-committing auth-audit pattern instead.
    """
    details: dict[str, Any] = {}
    if fields:
        details["fields"] = fields
    if extra:
        details.update(extra)

    await dispatch_audit(
        db,
        correlation_id=correlation_id or uuid.uuid4(),
        organization_id=organization_id,
        actor_id=user.id,
        action=f"{entity_type}.viewed",
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or None,
    )
