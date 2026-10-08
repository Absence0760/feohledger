"""LLM audit-log summarization for the invoice detail modal.

A reviewer "catching up" on an invoice otherwise has to parse a 15-row
audit timeline. This service distils that timeline (plus the latest
extraction's confidence + priors metadata) into a single
natural-language paragraph, cached on `invoices.meta["audit_summary"]`.

Cache-freshness mechanism (the central design decision)
-------------------------------------------------------
Audit rows are written from ~10 call sites via `dispatch_audit` — there is
no single chokepoint, and in `lambda` audit mode the write happens
out-of-process. So instead of trying to bump a counter on every write, we
**derive freshness from the audit log itself**. The cache stores a
`source_fingerprint = (count of audit rows for this correlation_id,
max(created_at))`. On modal open the endpoint cheaply recomputes that
fingerprint (one `SELECT count(*), max(created_at)`); if it matches the
cached value we serve the cached text, otherwise we regenerate. This needs
**zero changes to any audit write path** and works identically in `local`
and `lambda` audit modes — every status transition, correction, exception
resolution, and ERP-sync event already writes an audit row, so the
fingerprint moves and the summary regenerates naturally.

Fail-soft contract
-------------------
Modeled on `services.llm_fraud_detection`: a pure `build_prompt` /
`parse_response` pair, an async `summarize(...)` with an injectable
`http_post` for tests. Any error (no API key, mock provider, empty events,
network, malformed response) falls back to a deterministic *template*
summary built from the same events without an LLM call — so local dev shows
a real summary with no external dependency, and a flaky LLM never blocks the
modal.

PII discipline (project invariant)
-----------------------------------
The prompt and the cached text exclude banking / PII: no `vendor_tax_id`, no
remit-to bank details, no card PANs, no full addresses. Only invoice number,
vendor name, amount, currency, status events, and extraction confidence ride
along. The service logs only counts / ids — never invoice content.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exceptions import EXCEPTION_TYPE_LABELS
from app.config import settings
from app.models.invoice import Invoice, InvoiceExtractionResult
from app.models.user import User
from app.models.workflow import AuditLog

logger = logging.getLogger(__name__)


@dataclass
class AuditSummaryResult:
    text: str
    confidence_context: str | None = None


@dataclass
class AuditEvent:
    """Compact, PII-free view of one audit-log row — what the LLM sees."""

    action: str
    actor_name: str | None
    created_at: str | None
    # Only whitelisted, non-PII detail keys are carried (see _SAFE_DETAIL_KEYS).
    details: dict


# Whitelist of `audit_log.details` keys that are safe to send to the LLM /
# render. Everything else (addresses, tax ids, bank fields, raw payloads) is
# dropped. Keep this conservative — additive only after a PII review.
_SAFE_DETAIL_KEYS = frozenset(
    {
        "from_status",
        "to_status",
        "status",
        "reason",
        "fields_corrected",
        "corrected_fields",
        "exception_type",
        "resolution",
        "erp_reference",
        "erp_document_id",
        "method",
        "confidence",
    }
)


def _scrub_details(details: dict | None) -> dict:
    """Keep only whitelisted, non-PII detail keys.

    ``audit_log.details`` is ``JSONB`` with no object-shape constraint, so a
    non-object value can be present on a hand-written / corrupted row. Treat it
    as carrying nothing rather than indexing into it — a bad row must not 500
    the invoice's summary.
    """
    if not isinstance(details, dict):
        return {}
    scrubbed: dict = {}
    for key in _SAFE_DETAIL_KEYS:
        if key in details:
            scrubbed[key] = details[key]
    return scrubbed


_PROMPT = """You are writing a one-paragraph plain-English summary for an \
accounts-payable reviewer catching up on an invoice.

Invoice (non-sensitive fields only):
{invoice_json}

Extraction context:
{extraction_json}

Audit timeline (oldest → newest, JSON):
{events_json}

Write a SINGLE concise paragraph (no lists, no headings, no preamble) that \
covers, where present: how the invoice was created/extracted, status \
transitions, field corrections, exception resolutions, and ERP sync events. \
End with one short confidence-context clause when extraction context is \
present (e.g. "auto-extracted at 95% confidence with RAG priors applied").

Respond with a single JSON object, no surrounding prose:

{{
  "text": "<the one-paragraph summary>",
  "confidence_context": "<short confidence clause, or null if no extraction context>"
}}
"""


def _invoice_payload(invoice) -> dict:
    """Non-PII invoice fields for the prompt. Amount is stringified only into
    this throwaway prompt — never stored as float-for-currency."""
    return {
        "invoice_number": invoice.invoice_number or "",
        "vendor_name": invoice.vendor_name or "",
        "amount": str(invoice.amount) if invoice.amount is not None else None,
        "currency": invoice.currency or "USD",
        "status": str(invoice.status.value if hasattr(invoice.status, "value") else invoice.status),
    }


def _extraction_payload(extraction_meta: dict | None) -> dict:
    meta = extraction_meta or {}
    return {
        "confidence": meta.get("confidence"),
        "method": meta.get("method"),
        "vendor_cache_applied": meta.get("vendor_cache_applied", []),
        "rag_neighbor_count": meta.get("rag_neighbor_count", 0),
    }


def build_prompt(
    invoice,
    events: list[AuditEvent],
    extraction_meta: dict | None,
) -> str:
    """Render the summarization prompt. Exposed for tests so the contract
    with the LLM stays asserted as we evolve it."""
    events_json = json.dumps(
        [
            {
                "action": e.action,
                "actor": e.actor_name,
                "at": e.created_at,
                "details": e.details,
            }
            for e in events
        ],
        indent=2,
    )
    return _PROMPT.format(
        invoice_json=json.dumps(_invoice_payload(invoice), indent=2),
        extraction_json=json.dumps(_extraction_payload(extraction_meta), indent=2),
        events_json=events_json,
    )


def parse_response(text: str) -> AuditSummaryResult | None:
    """Pull the JSON object out of the model's response. Tolerates code-fenced
    output and surrounding prose. Returns None when no usable text can be
    extracted (caller falls back to the template)."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, dict):
        return None
    summary_text = data.get("text")
    if not summary_text or not isinstance(summary_text, str):
        return None
    cc = data.get("confidence_context")
    return AuditSummaryResult(
        text=summary_text.strip(),
        confidence_context=cc.strip() if isinstance(cc, str) and cc.strip() else None,
    )


# ---------- Deterministic template (the "mock adapter" path) ---------------

_ACTION_PHRASES = {
    "invoice.uploaded": "uploaded",
    "invoice.created": "created",
    "invoice.extraction_completed": "AI-extracted",
    "invoice.extraction_dispatched": "queued for extraction",
    "invoice.extraction_failed": "extraction failed",
    "invoice.submitted_for_review": "submitted for review",
    "invoice.approved": "approved",
    "invoice.rejected": "rejected",
    "invoice.resubmitted": "resubmitted for review",
    "invoice.erp_submitted": "sent to ERP",
    "invoice.completed": "marked complete",
    # Exception-queue events. These now reach the invoice's trail (they are
    # correlation-keyed to it — see services/exception_lifecycle), and a reviewer
    # catching up needs them: a duplicate / fraud flag is why the invoice
    # stalled, and clearing one is what let it move.
    "exception.raised": "flagged",
    "exception.resolved": "had its exception cleared",
    "exception.escalated": "had its exception escalated",
    "exception.dismissed": "had its exception dismissed",
}


def _build_confidence_context(extraction_meta: dict | None) -> str | None:
    meta = extraction_meta or {}
    confidence = meta.get("confidence")
    if confidence is None:
        return None
    try:
        pct = round(float(confidence) * 100)
    except (TypeError, ValueError):
        return None
    clause = f"auto-extracted at {pct}% confidence"
    extras = []
    if meta.get("vendor_cache_applied"):
        extras.append("vendor-cache priors")
    if meta.get("rag_neighbor_count"):
        extras.append("RAG priors")
    if extras:
        clause += " with " + " and ".join(extras) + " applied"
    return clause


def build_template_summary(
    invoice,
    events: list[AuditEvent],
    extraction_meta: dict | None,
) -> AuditSummaryResult:
    """Deterministic, no-LLM summary. Used as the local-dev default and as the
    fail-soft fallback whenever the LLM path is unavailable or errors."""
    inv_no = invoice.invoice_number or "this invoice"
    vendor = invoice.vendor_name or "an unknown vendor"
    amount = str(invoice.amount) if invoice.amount is not None else None
    currency = invoice.currency or "USD"

    head = f"Invoice {inv_no} from {vendor}"
    if amount is not None:
        head += f" for {amount} {currency}"

    phrases: list[str] = []
    for e in events:
        phrase = _ACTION_PHRASES.get(e.action)
        if not phrase:
            continue
        if e.action.startswith("exception."):
            # Name the exception type — "flagged (duplicate)" reads as the
            # reason the invoice stalled; a bare "flagged" doesn't.
            exception_type = e.details.get("exception_type")
            if exception_type:
                label = EXCEPTION_TYPE_LABELS.get(exception_type, exception_type)
                phrase = f"{phrase} ({label})"
            if e.action != "exception.raised" and e.actor_name:
                phrase = f"{phrase} by {e.actor_name}"
            phrases.append(phrase)
            continue

        to_status = e.details.get("to_status") or e.details.get("status")
        if e.action in ("invoice.approved", "invoice.rejected") and e.actor_name:
            phrase = f"{phrase} by {e.actor_name}"
        elif to_status and phrase not in ("approved", "rejected"):
            phrase = f"{phrase} ({to_status})"
        phrases.append(phrase)

    if phrases:
        # De-dupe consecutive repeats while preserving order.
        deduped: list[str] = []
        for p in phrases:
            if not deduped or deduped[-1] != p:
                deduped.append(p)
        body = "was " + ", then ".join(deduped) + "."
    else:
        current = str(invoice.status.value if hasattr(invoice.status, "value") else invoice.status)
        body = f"is currently {current} with no recorded timeline activity yet."

    text = f"{head} {body}"
    confidence_context = _build_confidence_context(extraction_meta)
    if confidence_context:
        text += f" It was {confidence_context}."
    return AuditSummaryResult(text=text, confidence_context=confidence_context)


# ---------- Config resolution (mirrors extraction) -------------------------


def _resolve_summary_config(org_settings: dict | None) -> dict:
    """Resolve the LLM config for summarization. Platform mode uses the
    app-level Anthropic key + model; BYOK uses the org's extraction settings.
    Returns `{"api_key", "model"}` — empty `api_key` selects the template path.
    """
    if not settings.audit_summary_enabled:
        return {"api_key": "", "model": ""}

    extraction = (org_settings or {}).get("extraction", {})
    program_type = extraction.get("program_type", "platform")
    model = settings.audit_summary_model or settings.extraction_model

    if program_type == "byok":
        # Only a `claude_vision` org's key is an Anthropic key. Any other BYOK
        # provider's secret must never be posted to api.anthropic.com, and the
        # platform key is no fallback (it would engage Anthropic for an org that
        # chose another provider) — the template path, as in
        # `llm_fraud_detection.resolve_anomaly_llm_config`.
        if extraction.get("provider") != "claude_vision":
            return {"api_key": "", "model": ""}
        return {
            "api_key": extraction.get("api_key", ""),
            "model": extraction.get("model") or model,
        }
    return {"api_key": settings.anthropic_api_key, "model": model}


async def summarize(
    invoice,
    events: list[AuditEvent],
    extraction_meta: dict | None,
    *,
    config: dict,
    http_post=None,
) -> AuditSummaryResult:
    """Build the summary, fail soft to the deterministic template.

    `http_post` is an injection point for tests — defaults to
    `httpx.AsyncClient.post`. Production code never passes it.

    Falls back to the template when:
      - api_key is missing (mock / local-dev / disabled)
      - there are no events
      - the API call fails or the response can't be parsed
    """
    api_key = (config or {}).get("api_key") or ""
    model = (config or {}).get("model") or settings.extraction_model

    if not api_key or not events:
        return build_template_summary(invoice, events, extraction_meta)

    prompt = build_prompt(invoice, events, extraction_meta)
    body = {
        "model": model,
        # Sized for adaptive thinking as well as the short summary: current
        # models think by default and thinking counts toward this cap, so 600
        # could end the turn before any text and silently drop to the template.
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        if http_post is not None:
            resp = await http_post(json=body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=body,
                    headers=headers,
                )
    except Exception:
        logger.exception("Audit summary: LLM call failed; using template")
        return build_template_summary(invoice, events, extraction_meta)

    if resp.status_code != 200:
        logger.warning("Audit summary: LLM returned %s; using template", resp.status_code)
        return build_template_summary(invoice, events, extraction_meta)

    data = resp.json()
    blocks = data.get("content") or []
    text = next((b.get("text", "") for b in blocks if b.get("type") == "text"), "")
    parsed = parse_response(text) if text else None
    if parsed is None:
        return build_template_summary(invoice, events, extraction_meta)
    return parsed


# ---------- Fingerprint + orchestration ------------------------------------


async def compute_fingerprint(db: AsyncSession, correlation_id) -> dict:
    """Cheap freshness probe: (count, max(created_at)) of audit rows for this
    correlation_id. Uses the existing index on `audit_log.correlation_id`."""
    result = await db.execute(
        select(func.count(AuditLog.id), func.max(AuditLog.created_at)).where(
            AuditLog.correlation_id == correlation_id
        )
    )
    count, last_at = result.one()
    return {"count": int(count or 0), "last_at": last_at.isoformat() if last_at else None}


async def _load_events(
    db: AsyncSession, control_db: AsyncSession, correlation_id
) -> list[AuditEvent]:
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.correlation_id == correlation_id)
        .order_by(AuditLog.created_at)
    )
    rows = result.scalars().all()

    actor_ids = {r.actor_id for r in rows if r.actor_id}
    actor_names: dict[str, str] = {}
    if actor_ids:
        res = await control_db.execute(select(User).where(User.id.in_(actor_ids)))
        for u in res.scalars().all():
            actor_names[str(u.id)] = u.full_name

    return [
        AuditEvent(
            action=r.action,
            actor_name=actor_names.get(str(r.actor_id)) if r.actor_id else None,
            created_at=r.created_at.isoformat() if r.created_at else None,
            details=_scrub_details(r.details),
        )
        for r in rows
    ]


async def _load_extraction_meta(db: AsyncSession, invoice) -> dict | None:
    """Latest extraction confidence + priors metadata for the confidence
    clause. Reads from the already-loaded `extraction_results` relationship
    when available to avoid a lazy load; otherwise queries directly."""
    rows = list(getattr(invoice, "extraction_results", []) or [])
    latest: InvoiceExtractionResult | None
    if rows:
        latest = max(rows, key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC))
    else:
        res = await db.execute(
            select(InvoiceExtractionResult)
            .where(InvoiceExtractionResult.invoice_id == invoice.id)
            .order_by(InvoiceExtractionResult.created_at.desc())
            .limit(1)
        )
        latest = res.scalar_one_or_none()

    if latest is None:
        return None
    priors = latest.priors_metadata or {}
    return {
        "confidence": float(latest.confidence) if latest.confidence is not None else None,
        "method": latest.method,
        "vendor_cache_applied": priors.get("vendor_cache_applied", []),
        "rag_neighbor_count": len(priors.get("rag_neighbors", []) or []),
    }


async def get_or_build_summary(
    db: AsyncSession,
    control_db: AsyncSession,
    invoice: Invoice,
    *,
    org_settings: dict | None = None,
    force: bool = False,
    http_post=None,
) -> dict:
    """Return the audit summary for an invoice, regenerating only when the
    audit-log fingerprint has changed since the cached generation (or `force`).

    Response shape:
        {"text", "confidence_context", "generated_at", "stale": bool}
    """
    current_fp = await compute_fingerprint(db, invoice.correlation_id)
    cached = (invoice.meta or {}).get("audit_summary") if invoice.meta else None

    if (
        not force
        and cached
        and cached.get("source_fingerprint") == current_fp
        and cached.get("text")
    ):
        return {
            "text": cached["text"],
            "confidence_context": cached.get("confidence_context"),
            "generated_at": cached.get("generated_at"),
            "stale": False,
        }

    events = await _load_events(db, control_db, invoice.correlation_id)
    extraction_meta = await _load_extraction_meta(db, invoice)
    config = _resolve_summary_config(org_settings)
    result = await summarize(invoice, events, extraction_meta, config=config, http_post=http_post)

    generated_at = datetime.now(UTC).isoformat()
    payload = {
        "text": result.text,
        "confidence_context": result.confidence_context,
        "source_fingerprint": current_fp,
        "generated_at": generated_at,
        "model": config.get("model") or "",
    }
    new_meta = dict(invoice.meta or {})
    new_meta["audit_summary"] = payload
    # A Core UPDATE, not `invoice.meta = ...` + ORM flush: this write is
    # read-shaped — a fingerprint-idempotent cache fill triggered by opening
    # the invoice, not a user edit — but `updated_at`'s `onupdate=func.now()`
    # fires on ANY ORM-flushed update to the row unless the column is given
    # an explicit value IN THE SAME STATEMENT (self-assigning
    # `invoice.updated_at = invoice.updated_at` does not count — an
    # unchanged value never dirties the attribute, so onupdate still wins).
    # A Core `update().values(...)` naming the column explicitly is the
    # reliable way to override it. Left alone, opening the invoice modal
    # (which calls this) silently bumps `updated_at` behind the
    # optimistic-concurrency lock's back: the frontend's
    # `expected_updated_at` (captured moments earlier from the same GET)
    # goes stale before the user touches anything, and the very next
    # PATCH/approve/complete 409s as a phantom conflict.
    #
    # The write is a compare-and-swap on the version it read. Writing
    # `updated_at` back unconditionally ROLLED BACK any edit that committed
    # between this read and this write: the row kept the new amount but got
    # the old version, so an approver still holding that version passed the
    # stale-approval check and signed figures they never saw (decisions §260).
    # `meta` built from the stale read was a lost update for the same reason.
    # If the row moved, the cache fill is simply skipped — the summary below
    # is still returned, and the next open regenerates against the new row.
    await db.execute(
        update(Invoice)
        .where(Invoice.id == invoice.id, Invoice.updated_at == invoice.updated_at)
        .values(meta=new_meta, updated_at=invoice.updated_at)
    )
    await db.commit()

    logger.info(
        "Audit summary regenerated for invoice %s (events=%d)",
        invoice.id,
        len(events),
    )
    return {
        "text": result.text,
        "confidence_context": result.confidence_context,
        "generated_at": generated_at,
        "stale": False,
    }
