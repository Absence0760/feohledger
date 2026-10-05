"""Auditor-facing audit-trail endpoints (`/api/audit`).

SOX evidence surface for external auditors: per-invoice and date-range export
of the tenant ``audit_log``, in JSON or CSV. Distinct from the per-invoice
``GET /api/invoices/{id}/audit-log`` (operational UI) — this router carries its
own RBAC (export is admin/CFO only, the auditor-equivalent privilege) and is
itself audited (every export writes an ``audit.exported`` row).

All routes are reads — no money moves — so the idempotency invariant does not
bind here. Every audit query resolves the tenant DB via ``get_tenant_db``;
actor names/emails are resolved against the control DB exactly as the existing
endpoint does. No PUT/PATCH/DELETE is defined anywhere in this router, keeping
the audit trail GET-only.
"""

import asyncio
import io
import tempfile
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    require_roles,
)
from app.config import settings
from app.database import get_control_db
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.user import User
from app.models.workflow import AuditLog
from app.schemas.audit import AuditExportEntry
from app.services.approval_signature import (
    VERDICT_INVALID,
    VERDICT_UNSIGNED,
    VERDICT_VALID,
    check_approval_row,
)
from app.services.audit_access import log_access
from app.services.audit_dispatch import dispatch_audit
from app.services.audit_report_pdf import AuditReportContext, render_audit_report_pdf
from app.services.branding import get_brand_context
from app.services.report_export import safe_csv_writer
from app.tenant import ensure_in_entity_scope, get_entity_id, get_tenant_db
from app.utils.dates import utc_today

router = APIRouter(prefix="/audit", tags=["audit"])


async def _actor_names(
    control_db: AsyncSession, actor_ids: set[uuid.UUID]
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({actor_id: name}, {actor_id: email}) for a set of actor ids."""
    names: dict[str, str] = {}
    emails: dict[str, str] = {}
    if actor_ids:
        result = await control_db.execute(select(User).where(User.id.in_(actor_ids)))
        for u in result.scalars().all():
            names[str(u.id)] = u.full_name
            emails[str(u.id)] = u.email
    return names, emails


async def _resolve_actors(
    control_db: AsyncSession, entries: list[AuditLog]
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({actor_id: name}, {actor_id: email}) from the control DB."""
    return await _actor_names(control_db, {e.actor_id for e in entries if e.actor_id})


_CSV_HEADER = [
    "created_at",
    "action",
    "entity_type",
    "entity_id",
    "actor_name",
    "actor_email",
    "correlation_id",
    "details",
]

# Rows per database fetch AND per emitted response chunk. The streaming export
# reuses one number for both so the two never drift: every `yield` hands control
# back to the event loop, so this is also the bound on how long the CSV/JSON
# serialisation of a 40k-row SOX export can occupy the loop uninterrupted.
_STREAM_CHUNK_ROWS = 500


def _csv_row(e: AuditExportEntry) -> list[str]:
    """One CSV row from an export entry.

    Cells come only from the already-sanitised export entry (the ``details``
    column was scrubbed of regulated values at audit-write time), so no
    banking/tax-id value can reach the CSV surface. Sanitising against
    spreadsheet formula injection is `safe_csv_writer`'s job at the writerow
    site — both the streaming export and `_entries_to_csv` go through it.
    """
    return [
        e.created_at,
        e.action,
        e.entity_type,
        e.entity_id or "",
        e.actor_name or "",
        e.actor_email or "",
        e.correlation_id,
        "" if e.details is None else str(e.details),
    ]


def _entries_to_csv(entries: list[AuditExportEntry]) -> str:
    """Render a materialised list of export entries to a single CSV string.

    The export route streams instead (see ``_csv_body``); this shares
    ``_CSV_HEADER`` / ``_csv_row`` with it so the two renderings cannot drift,
    and remains the surface the CSV formula-injection guard exercises.
    """
    buf = io.StringIO()
    writer = safe_csv_writer(buf)
    writer.writerow(_CSV_HEADER)
    for e in entries:
        writer.writerow(_csv_row(e))
    return buf.getvalue()


async def _stream_entries(
    db: AsyncSession,
    query,
    names: dict[str, str],
    emails: dict[str, str],
) -> AsyncIterator[AuditExportEntry]:
    """Yield export entries straight off a server-side cursor.

    ``yield_per`` is what keeps a whole year of ``audit_log`` out of memory —
    the same mechanism ``GET /audit/verify-signatures`` already uses. ``query``
    selects plain columns rather than the ``AuditLog`` entity, so no row is
    added to the session's identity map either; a streamed ORM entity would
    accumulate there and defeat the point.
    """
    result = await db.stream(query.execution_options(yield_per=_STREAM_CHUNK_ROWS))
    async for row in result:
        yield AuditExportEntry.from_db(row, names, emails)


async def _csv_body(
    db: AsyncSession,
    query,
    names: dict[str, str],
    emails: dict[str, str],
) -> AsyncIterator[str]:
    """CSV response body, emitted in bounded chunks."""
    buf = io.StringIO()
    writer = safe_csv_writer(buf)
    writer.writerow(_CSV_HEADER)
    pending = 0
    async for entry in _stream_entries(db, query, names, emails):
        writer.writerow(_csv_row(entry))
        pending += 1
        if pending >= _STREAM_CHUNK_ROWS:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            pending = 0
    if buf.tell():
        yield buf.getvalue()


async def _json_body(
    db: AsyncSession,
    query,
    names: dict[str, str],
    emails: dict[str, str],
) -> AsyncIterator[str]:
    """JSON response body — the same bare array the route used to return.

    The closing ``]`` is emitted only after the cursor is exhausted, so a run
    that dies mid-export produces syntactically invalid JSON that no parser
    accepts. That is deliberate: see the route docstring on why a SOX export
    must never be able to look complete when it isn't.
    """
    parts: list[str] = ["["]
    pending = 0
    first = True
    async for entry in _stream_entries(db, query, names, emails):
        payload = entry.model_dump_json()
        parts.append(payload if first else "," + payload)
        first = False
        pending += 1
        if pending >= _STREAM_CHUNK_ROWS:
            yield "".join(parts)
            parts = []
            pending = 0
    parts.append("]")
    yield "".join(parts)


# The spool keeps at most this much of a rendered export in memory; past it
# `SpooledTemporaryFile` rolls the rest onto disk, so peak RSS is bounded by
# this number no matter how large the range is. A per-invoice trail or a short
# date range never touches the filesystem at all.
_SPOOL_MAX_MEMORY_BYTES = 1024 * 1024

# Block size the finished export is handed back to the transport in. Small
# enough that the response is genuinely chunked (an aborted body has no
# terminating chunk — see the route docstring), large enough that a 40k-row
# export is a few hundred reads rather than a few hundred thousand.
_SPOOL_READ_BYTES = 64 * 1024


async def _spool_body(chunks: AsyncIterator[str]) -> tempfile.SpooledTemporaryFile:
    """Render an export body into a spool file, positioned at its start.

    This is what releases the tenant DB connection before transmission begins.
    Streaming the cursor straight into the response bounded the *memory* but
    extended the *connection hold* from "while the rows are read" to "until the
    last byte reaches the client" — and the per-tenant pool is
    ``pool_size=5, max_overflow=10``, so a handful of concurrent annual exports
    over slow links could exhaust it. Draining into a spool first restores the
    short hold without giving back the memory bound: the file is
    ``_SPOOL_MAX_MEMORY_BYTES`` of RAM and then disk.

    A database error part-way through therefore surfaces as a clean 500 *before*
    any byte is sent, instead of truncating a body the client had already begun
    to save — which is the same principle the un-terminated chunk and the
    missing JSON ``]`` serve: an incomplete SOX export must never look complete.

    Writes go through ``asyncio.to_thread`` because a spool that has rolled over
    is a real file and a synchronous ``write`` would sit on the event loop for
    its duration (project invariant: no blocking work on the loop).
    """
    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_MEMORY_BYTES, mode="w+b")
    try:
        async for chunk in chunks:
            await asyncio.to_thread(spool.write, chunk.encode("utf-8"))
        await asyncio.to_thread(spool.seek, 0)
    except BaseException:
        # Includes cancellation: nothing downstream will ever be handed this
        # file, so it is this frame's job to unlink it.
        spool.close()
        raise
    return spool


async def _spooled_response(
    db: AsyncSession, chunks: AsyncIterator[str], **response_kwargs
) -> StreamingResponse:
    """Render, release the tenant connection, then hand back the streamer.

    The three steps belong together — a spool that is created but never reaches
    a ``StreamingResponse`` has nobody left to close it — so both dialects go
    through here rather than repeating the sequence.
    """
    spool = await _spool_body(chunks)
    try:
        await db.close()
    except BaseException:
        spool.close()
        raise
    return StreamingResponse(_drain_spool(spool), **response_kwargs)


async def _drain_spool(spool: tempfile.SpooledTemporaryFile) -> AsyncIterator[bytes]:
    """Emit a spooled body in bounded blocks, deleting the file when done.

    The ``finally`` covers both exits that matter: the last block being read,
    and the client disconnecting mid-download — Starlette closes the body
    generator, which raises ``GeneratorExit`` here, so the temp file is unlinked
    on the abandoned path too rather than waiting for garbage collection.
    """
    try:
        while True:
            block = await asyncio.to_thread(spool.read, _SPOOL_READ_BYTES)
            if not block:
                return
            yield block
    finally:
        spool.close()


@router.get("/export")
async def export_audit_trail(
    invoice_id: uuid.UUID | None = Query(None),
    start: date | None = Query(None),
    end: date | None = Query(None),
    entity_type: str | None = Query(None),
    export_format: str = Query("json", alias="format", pattern="^(json|csv|pdf)$"),
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
):
    """Auditor export — per-invoice OR date-range, JSON / CSV / PDF.

    Provide either ``invoice_id`` (the invoice's whole correlation trail) or a
    ``start``/``end`` date range. The two are mutually exclusive. Validation
    errors are generic ("invalid range") and never echo entity values; a
    missing invoice returns 404 with no leaked data. The export itself is
    audited (``audit.exported``).

    ``format=pdf`` returns a formatted SOX audit-trail report (cover + summary +
    chronological table) for external auditors. It renders exactly the same
    already-sanitised export entries the JSON/CSV dialects do (PII is kept out of
    ``details`` at audit-write time) — no broader, no regulated value added.

    **The export streams, and is never capped.** The real use case is an annual
    SOX range — tens of thousands of rows — and this route used to materialise
    every one of them as an ORM object before writing a byte, then build the
    whole response body in memory on top of that. It is now read through a
    server-side cursor (``yield_per``, the mechanism the sibling
    ``/audit/verify-signatures`` already used), and JSON/CSV are rendered in
    bounded chunks as the rows arrive.

    Those chunks land in a ``SpooledTemporaryFile`` (``_spool_body``) rather
    than going straight out on the wire, and the tenant session is CLOSED before
    the response is returned. Streaming the cursor directly into the response
    bounded the memory but extended the connection hold from "while the rows are
    read" to "until the last byte is transmitted" — and the per-tenant pool is
    ``pool_size=5, max_overflow=10``, so a handful of concurrent annual exports
    over slow links could exhaust it while the database itself sat idle. The
    spool keeps both properties: peak memory is bounded by
    ``_SPOOL_MAX_MEMORY_BYTES`` (the rest rolls to disk) and the connection is
    back in the pool before transmission starts. It also moves a mid-export
    database error to *before* the first byte, where it is a clean 500 instead
    of a body the client had already begun to save.

    A ``LIMIT`` was the obvious alternative and is the wrong one: a silently
    short export is evidence an auditor would sign off on, which is worse than
    a slow one. Nothing here truncates, and a run that fails part-way cannot
    pass for a complete one either — the response is chunked (no
    ``Content-Length``), so an aborted body has no terminating chunk and every
    conforming client raises a transport error, and the JSON dialect
    additionally never emits its closing ``]``.

    The number of queries is fixed regardless of range size: the snapshot
    ``now()``, one aggregate carrying both the row ``COUNT`` and the range's
    ``DISTINCT actor_id`` set, one batched actor lookup on the control DB, and
    the streamed cursor. Resolving actor names used to require a second pass
    over the materialised rows, which is exactly what streaming cannot afford.
    """
    if invoice_id is not None and (start is not None or end is not None):
        raise HTTPException(status_code=400, detail="Provide invoice_id or a date range, not both")
    if invoice_id is None and start is None and end is None:
        raise HTTPException(status_code=400, detail="Provide invoice_id or a start/end date range")

    # Collected as a criteria list rather than layered onto one query object,
    # so the row stream, the COUNT and the DISTINCT actor lookup are provably
    # the same predicate — three readers of one filter set, not three
    # independently-maintained queries that can drift apart.
    filters: list = []

    # Pin the export to a snapshot taken before anything is written.
    #
    # The materialising version read its rows BEFORE writing the `audit.exported`
    # row, so an export could never contain the record of itself. Streaming
    # inverts that order — the audit row is committed first, then the cursor is
    # opened — so without an upper bound the body would carry a row the `count`
    # on the same trail does not, and any row a concurrent request committed in
    # between besides.
    #
    # The bound comes from the DATABASE clock, not `datetime.now(UTC)`:
    # `AuditLog.created_at` defaults to Postgres' `now()`, and comparing it
    # against this process's clock would drop or admit rows on any skew. Postgres
    # `now()` is the transaction timestamp, so the `audit.exported` row this
    # request goes on to insert shares it exactly and is excluded by the strict
    # `<`. For a historical range this is a no-op; for an open-ended one it is
    # what makes the export reproducible.
    snapshot_at = (await db.execute(select(func.now()))).scalar_one()
    filters.append(AuditLog.created_at < snapshot_at)

    if invoice_id is not None:
        row = (
            await db.execute(
                select(Invoice.correlation_id, Invoice.invoice_number).where(
                    Invoice.id == invoice_id
                )
            )
        ).first()
        if not row or not row[0]:
            raise HTTPException(status_code=404, detail="Invoice not found")
        correlation_id, invoice_number = row
        filters.append(AuditLog.correlation_id == correlation_id)
        scope = "invoice"
        scope_label = f"Invoice {invoice_number}" if invoice_number else "Invoice"
    else:
        if start is not None and end is not None and start > end:
            raise HTTPException(status_code=400, detail="Invalid range")
        if start is not None:
            filters.append(
                AuditLog.created_at >= datetime.combine(start, datetime.min.time(), tzinfo=UTC)
            )
        if end is not None:
            # Inclusive of the whole `end` day: strictly-less-than the start of
            # the next day, so a row at end-of-day's last microsecond is included.
            next_day_start = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
            filters.append(AuditLog.created_at < next_day_start)
        scope = "range"
        scope_label = (
            f"{start.isoformat() if start else 'beginning'} to {end.isoformat() if end else 'now'}"
        )

    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)

    # Columns, not the `AuditLog` entity: `AuditExportEntry.from_db` reads these
    # by attribute and a `Row` answers to the same names, so streaming costs the
    # session no identity-map growth.
    rows_query = (
        select(
            AuditLog.id,
            AuditLog.correlation_id,
            AuditLog.actor_id,
            AuditLog.action,
            AuditLog.entity_type,
            AuditLog.entity_id,
            AuditLog.details,
            AuditLog.created_at,
        )
        .where(*filters)
        .order_by(AuditLog.created_at)
    )

    # The population size and its distinct actors in ONE pass over the range.
    #
    # Both are needed before the first byte, and asking for them separately is
    # two scans of the same range for one row of results. Folded together they
    # are one. (Measured at 20 000 rows the saving was inside the noise — the
    # streaming export's remaining cost is the cursor, not these — but two full
    # scans where one does is worth avoiding on an annual range regardless.)
    #
    # `count` is what the `audit.exported` row records. It is COUNTED rather
    # than measured while streaming because the audit row is written BEFORE a
    # byte leaves — the access has already happened by then, so an export the
    # auditor aborts half way must still appear on the trail. Deferring the row
    # until the stream finished would lose exactly that record.
    #
    # The distinct actor set replaces the second pass the materialising version
    # took over its loaded rows; a stream has no such pass. It is bounded by the
    # org's user count, not by the number of audit rows, so the batched control-DB
    # lookup behind it stays one small query.
    total, actor_id_list = (
        await db.execute(
            select(
                func.count(),
                func.array_agg(distinct(AuditLog.actor_id)).filter(AuditLog.actor_id.is_not(None)),
            ).where(*filters)
        )
    ).one()
    total = int(total or 0)
    names, emails = await _actor_names(control_db, set(actor_id_list or ()))

    # The export is itself an auditable access event. Use the export-specific
    # verb ("audit.exported", not "audit.viewed"), so dispatch directly. The
    # details carry only non-PII scope metadata (scope + row count).
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=user.organization_id,
        actor_id=user.id,
        action="audit.exported",
        entity_type="audit",
        entity_id=invoice_id or user.organization_id,
        details={"scope": scope, "count": total, "format": export_format},
    )
    await db.commit()

    if export_format == "csv":
        filename = f"audit_export_{utc_today().isoformat()}.csv"
        return await _spooled_response(
            db,
            _csv_body(db, rows_query, names, emails),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    if export_format == "pdf":
        # The one dialect that genuinely cannot stream: a PDF is laid out as a
        # whole document, so every entry must be in hand before rendering
        # starts. It is still read through the cursor (no ORM entities, no
        # identity map) and the layout itself already runs off the event loop.
        export = [entry async for entry in _stream_entries(db, rows_query, names, emails)]
        # Nothing below reads the tenant DB, and the layout runs off the event
        # loop — releasing here keeps the render out of the connection's hold.
        await db.close()
        org = await control_db.get(Organization, user.organization_id)
        ctx = AuditReportContext(
            org_name=(org.name if org else "Organization"),
            scope=scope,
            scope_label=scope_label,
            generated_at=datetime.now(UTC),
            generated_by_name=user.full_name,
            generated_by_email=user.email,
            entries=export,
            brand=get_brand_context(org.settings if org else None),
        )
        pdf_bytes = await asyncio.to_thread(render_audit_report_pdf, ctx)
        filename = f"audit_report_{utc_today().isoformat()}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return await _spooled_response(
        db, _json_body(db, rows_query, names, emails), media_type="application/json"
    )


@router.get("/verify-signatures")
async def verify_signatures_for_period(
    start: date | None = Query(None),
    end: date | None = Query(None),
    limit: int = Query(100, ge=1, le=1000, description="Cap on the findings list"),
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
):
    """Population-level non-repudiation test over a date range.

    The per-invoice check (``/audit/invoice/{id}/verify-signatures``) can only
    answer "is THIS approval still intact" — which presumes you already know
    which invoice to suspect. Testing the control the way an auditor actually
    tests it (over a period's whole population of approvals) had no surface at
    all, so a tampered ``invoice.approved`` row was in practice undetectable.

    This sweeps every ``invoice.approved`` audit row in ``start``..``end``,
    re-derives each signature against its invoice's **current** exact amount via
    the same ``check_approval_row`` primitive, and returns population counts plus
    a bounded list of the rows that did not verify. A clean run — ``invalid`` and
    ``unsigned`` both zero — is the evidence; a non-zero count names exactly the
    rows to investigate.

    Rows are streamed (``yield_per``), so the counts cover the whole population
    without materialising it. ``findings`` is capped by ``limit`` and flags
    ``findings_truncated`` when there were more; the counts are never truncated.

    ``unsigned`` is reported separately from ``invalid``: an approval written
    before ``FEOH_APPROVAL_SIGNING_KEY`` was configured has nothing to verify and
    is not evidence of tampering — but once signing IS configured, an unsigned
    approval in the period is itself a finding, which is why it is listed rather
    than merely counted.

    Admin/CFO only (the auditor privilege), and itself audited (``audit.viewed``
    carrying counts only — no invoice values enter the trail).
    """
    if start is None and end is None:
        raise HTTPException(status_code=400, detail="Provide a start and/or end date")
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=400, detail="Invalid range")

    # Join on `correlation_id`: it is UNIQUE on `invoices`, so one audit row
    # maps to exactly one invoice — and it is the column the trail is filed
    # under, so this is the same linkage the per-invoice read uses.
    query = (
        select(
            AuditLog.id,
            AuditLog.actor_id,
            AuditLog.created_at,
            AuditLog.details,
            Invoice.id,
            Invoice.invoice_number,
            Invoice.amount,
        )
        .join(Invoice, Invoice.correlation_id == AuditLog.correlation_id)
        .where(AuditLog.action == "invoice.approved")
        .order_by(AuditLog.created_at)
    )
    if start is not None:
        query = query.where(AuditLog.created_at >= datetime.combine(start, time.min, tzinfo=UTC))
    if end is not None:
        # Inclusive of the whole `end` day (mirrors /audit/export).
        query = query.where(
            AuditLog.created_at < datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
        )

    # Keyed off the verdict constants themselves so a renamed/added verdict is a
    # KeyError at test time, never a silently-dropped row.
    counts = dict.fromkeys((VERDICT_VALID, VERDICT_INVALID, VERDICT_UNSIGNED), 0)
    invoice_ids: set[uuid.UUID] = set()
    findings: list[dict] = []
    findings_truncated = False

    signing_key = settings.approval_signing_key
    result = await db.stream(query.execution_options(yield_per=500))
    async for row_id, actor_id, _created_at, details, inv_id, inv_number, inv_amount in result:
        invoice_ids.add(inv_id)
        check = check_approval_row(
            details=details,
            invoice_id=inv_id,
            # Money stays exact — the digest was computed over a Decimal.
            amount=Decimal(str(inv_amount or 0)),
            actor_id=actor_id,
            signing_key=signing_key,
        )
        counts[check.verdict] += 1
        if check.verdict == VERDICT_VALID:
            continue
        if len(findings) >= limit:
            findings_truncated = True
            continue
        findings.append(
            {
                "invoice_id": str(inv_id),
                "invoice_number": inv_number,
                "audit_row_id": str(row_id),
                "actor_id": str(actor_id) if actor_id else None,
                "signed_at": check.signed_at,
                "verdict": check.verdict,
            }
        )

    names, _emails = await _actor_names(
        control_db, {uuid.UUID(f["actor_id"]) for f in findings if f["actor_id"]}
    )
    for finding in findings:
        finding["actor"] = names.get(finding["actor_id"]) if finding["actor_id"] else None

    checked = sum(counts.values())
    await log_access(
        db,
        user=user,
        organization_id=user.organization_id,
        entity_type="audit",
        entity_id=user.organization_id,
        extra={
            "scope": "range",
            "verify_signatures": checked,
            "invalid": counts[VERDICT_INVALID],
            "unsigned": counts[VERDICT_UNSIGNED],
        },
    )
    await db.commit()

    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "signing_configured": bool(signing_key),
        "invoices_covered": len(invoice_ids),
        "approvals_checked": checked,
        "valid": counts[VERDICT_VALID],
        "invalid": counts[VERDICT_INVALID],
        "unsigned": counts[VERDICT_UNSIGNED],
        "findings": findings,
        "findings_truncated": findings_truncated,
    }


@router.get("/invoice/{invoice_id}")
async def get_invoice_audit_trail(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Auditor-facing per-invoice trail (alias of the operational endpoint).

    Same correlation-scoped query as ``GET /api/invoices/{id}/audit-log`` but
    in its own ``/api/audit`` namespace with the auditor RBAC set. Ordered by
    ``created_at`` so the timeline is chronological. The view is itself audited.
    """
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    correlation_id = (
        await db.execute(select(Invoice.correlation_id).where(Invoice.id == invoice_id))
    ).scalar_one_or_none()
    if not correlation_id:
        raise HTTPException(status_code=404, detail="Invoice not found")

    entries = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.correlation_id == correlation_id)
                .order_by(AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    names, emails = await _resolve_actors(control_db, entries)
    export = [AuditExportEntry.from_db(e, names, emails) for e in entries]

    await log_access(
        db,
        user=user,
        organization_id=user.organization_id,
        entity_type="audit",
        entity_id=invoice_id,
        correlation_id=correlation_id,
    )
    await db.commit()

    return export


@router.get("/invoice/{invoice_id}/verify-signatures")
async def verify_invoice_signatures(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Cryptographic non-repudiation check on an invoice's approval signatures.

    Loads every ``invoice.approved`` audit row carrying a ``details.signature``
    block and re-derives the HMAC-SHA256 over the approval facts — invoice id +
    the invoice's CURRENT exact ``amount`` + the row's ``actor_id`` + decision +
    the signed timestamp — comparing it (constant-time) to the stored digest. A
    tampered amount, a swapped actor, or an altered timestamp flips ``valid`` to
    ``False``, proving the approval record wasn't silently changed after the
    fact.

    Admin/CFO only (the auditor privilege). This is a sensitive read, so it
    writes its own ``audit.viewed`` access row.
    """
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    ).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.correlation_id == invoice.correlation_id,
                    AuditLog.action == "invoice.approved",
                )
                .order_by(AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )

    # Recompute against the invoice's CURRENT amount — the whole point is that a
    # post-approval tamper of the amount breaks verification. Money stays
    # Decimal (never float).
    current_amount = Decimal(str(invoice.amount or 0))

    names, _emails = await _resolve_actors(control_db, rows)

    results: list[dict] = []
    for row in rows:
        # One shared definition of "does this row still verify" — the population
        # sweep below calls the same primitive, so the two can't drift on what
        # counts as unsigned vs tampered.
        check = check_approval_row(
            details=row.details,
            invoice_id=invoice.id,
            amount=current_amount,
            actor_id=row.actor_id,
            signing_key=settings.approval_signing_key,
        )
        results.append(
            {
                "audit_row_id": str(row.id),
                "signed_at": check.signed_at,
                "actor": names.get(str(row.actor_id)) if row.actor_id else None,
                "signed": check.signed,
                "valid": check.valid,
            }
        )

    # Sensitive read → access audit. No PII in the details (counts only).
    await log_access(
        db,
        user=user,
        organization_id=user.organization_id,
        entity_type="audit",
        entity_id=invoice_id,
        correlation_id=invoice.correlation_id,
        extra={"verify_signatures": len(results)},
    )
    await db.commit()

    return {
        "invoice_id": str(invoice_id),
        "signing_configured": bool(settings.approval_signing_key),
        "approvals": results,
    }
