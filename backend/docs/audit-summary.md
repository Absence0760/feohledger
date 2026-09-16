# Audit Log Summarization

A one-paragraph, plain-English summary of an invoice's audit timeline,
shown at the top of the invoice detail modal so a reviewer can catch up
without parsing a 15-row timeline. Built from the audit log plus the
latest extraction's confidence + priors metadata.

- Service: `app/services/audit_summary.py`
- Endpoints: `GET /api/invoices/{id}/summary`, `POST /api/invoices/{id}/summary/regenerate`
- Cache: `invoices.meta["audit_summary"]` (JSONB)
- Config: `FEOH_AUDIT_SUMMARY_ENABLED` (default `false`), `FEOH_AUDIT_SUMMARY_MODEL` (defaults to `FEOH_EXTRACTION_MODEL`)

**The LLM path is opt-in.** Left off, the endpoints return the deterministic
template summary and no request leaves the process. It defaults off because it
reuses the extraction Anthropic key rather than holding one of its own: on the
old `true` default, configuring Anthropic for *extraction* silently started a
second, unrelated flow of invoice numbers, vendor names, amounts and audit
timelines to Anthropic that no per-org setting disclosed. Turn it on
deliberately, and disclose it — it is a sub-processor engagement
(`/legal/sub-processors`).

## Cache-freshness mechanism

Audit rows are written from ~10 call sites via `dispatch_audit` — there is
no single chokepoint, and in `lambda` audit mode the write happens
out-of-process. Rather than try to bump a counter on every write, freshness
is **derived from the audit log itself**.

The cache stores a `source_fingerprint`:

```
source_fingerprint = { "count": <count of audit rows for this correlation_id>,
                       "last_at": <max(created_at) ISO string> }
```

On modal open, `GET /summary` cheaply recomputes that fingerprint
(`SELECT count(*), max(created_at) FROM audit_log WHERE correlation_id = :cid`,
using the existing index on `correlation_id`) and compares it to the cached
value:

- **match** → return the cached text (no LLM call)
- **mismatch** (or `force=True` via `/regenerate`) → rebuild the summary,
  write the new text + fingerprint back to `meta`, return it

This needs **zero changes to any audit write path** and works identically in
`local` and `lambda` audit modes — every status transition, correction,
exception resolution, and ERP-sync event already writes an audit row, so the
fingerprint moves and the summary regenerates naturally.

## Cached shape

`invoices.meta["audit_summary"]`:

```json
{
  "text": "Invoice INV-42 from Acme Hosting for 4200.00 USD was AI-extracted, then submitted for review, then approved by Manny Manager, then sent to ERP. It was auto-extracted at 95% confidence with RAG priors applied.",
  "confidence_context": "auto-extracted at 95% confidence with RAG priors applied",
  "source_fingerprint": { "count": 6, "last_at": "2026-05-01T10:09:00+00:00" },
  "generated_at": "2026-06-11T12:00:00+00:00",
  "model": "claude-sonnet-4-20250514"
}
```

## LLM plumbing + local-first

Modeled on `services/llm_fraud_detection.py`: a pure `build_prompt` /
`parse_response` pair and an async `summarize(...)` with an injectable
`http_post` for tests. Config is resolved the same way extraction is — platform
(`anthropic_api_key` + `extraction_model`) or BYOK (`org_settings.extraction`).

**Fail-soft / mock path.** Any unavailable-LLM condition (no API key, mock /
disabled, empty events, network error, non-200, unparseable response) falls
back to a deterministic *template* summary built from the same events without
an LLM call. With the committed `.env.development` default (empty
`anthropic_api_key`), a fresh clone shows a real template summary with no
network dependency — this is the "mock adapter" equivalent. No new infra
profile is needed; the existing `pnpm dev` path exercises it. A real local LLM
(`pnpm ollama:up`) honors the same provider config if desired.

## PII / banking discipline

The prompt and cached text exclude banking / PII — no `vendor_tax_id`, no
remit-to bank details, no card PANs, no full addresses. Audit-row `details`
are scrubbed to a conservative whitelist (`_SAFE_DETAIL_KEYS`) before they
reach the prompt. Only invoice number, vendor name, amount (stringified — never
a float for storage), currency, status events, and extraction confidence ride
along. The service logs only counts / ids.

## Auth & invariants

- Both routes carry an auth dependency (`require_roles`) — `GET /summary` is
  open to any AP role; `POST /summary/regenerate` is admin/manager only.
  Covered by `tests/test_rbac.py`.
- Tenant isolation via `get_tenant` / `get_tenant_db`; a cross-tenant request
  404s.
- Read-shaped: the endpoint may lazily write the cache, but the write is
  fingerprint-idempotent and moves no money, so no idempotency key is needed.
- The audit log is only *read*; no append-only audit row is consumed or
  mutated.

## Tests

- `backend/tests/test_audit_summary.py` — prompt contract (all event types +
  confidence + PII exclusion), `parse_response` tolerance, deterministic
  template, `summarize` fail-soft + happy path.
- `backend/tests/test_invoices_summary_api.py` — endpoint shape, 404, tenant
  isolation, fingerprint freshness (cached until a new audit row moves the
  fingerprint), `meta` persistence, regenerate + RBAC.
- `frontend/tests-e2e/invoices/summary.spec.ts` — summary paragraph renders at
  the top of the modal; admin regenerate.
