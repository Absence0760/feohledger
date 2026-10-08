# Roadmap — shipped

Archive of completed roadmap sections, moved verbatim out of
[roadmap.md](roadmap.md) so the open file shows only what's left. Nothing here
is summarized — the full original entry, its checkbox detail, and its competitor
notes are preserved, because that detail is what makes the archive useful when
someone asks "does the platform already do X?".

**Read this before building.** 46 of 54 roadmap sections are here. Prior art for
most capabilities — matching, payments, e-invoicing, procurement, RBAC — lives in
this file and in `backend/docs/`.

**Adding to it:** when a section's last open item ships, move the whole section
here in the same commit that closes it, keeping it under its original priority
heading. Don't rewrite entries on the way in.

Section order and priority groupings match the original file. Where a priority
still has open sections, its heading appears in both files.

---

## Priority 1: Core Automation (highest impact)

### Real AI Extraction
**Status:** Done — adapter pattern with Claude Vision (platform), OpenAI GPT-4V, AWS Textract (BYOK). Platform/BYOK dual model. See [ai-extraction.md](../backend/docs/ai-extraction.md).

- [x] Extraction adapter pattern with dispatcher
- [x] Claude Vision adapter (platform default) with structured JSON prompt
- [x] OpenAI GPT-4V adapter (BYOK)
- [x] AWS Textract adapter (BYOK)
- [x] Mock adapter for development
- [x] Per-field confidence scoring (0-1)
- [x] Extract line items (not just header fields)
- [x] Platform/BYOK dual model — platform keys in env vars, customer keys in org settings
- [x] Extraction config in organization settings UI
- [x] Usage tracking (ExtractionUsage model) for billing
- [x] Support multi-page PDFs — `_pdf_to_images()` converts all pages (up to 20) to PNG; vision adapters (Ollama, OpenAI) send all page images in one API call. Claude Vision handles multi-page natively via document mode. Text-mode already extracted all pages.
- [x] Handle rotated scans — Tesseract OSD auto-rotation in `app/services/image_preprocess.py`, called from `OllamaAdapter._pdf_to_images`. Gated on `FEOH_EXTRACTION_AUTO_ROTATE` (default on); soft-depends on `pytesseract` + the `tesseract` binary — missing deps silently no-op. Small-angle deskew (1–5° tilt) and low-quality enhancement still open if real data demands them.
- [x] Auto-approve extraction above configurable threshold — `auto_approve_enabled` + `auto_approve_threshold` on extraction step config; also checks `auto_approve_below` from approval step. Invoices skip review and go directly to `approved` with `approved_by="system (auto-approve)"`
- [x] Custom chart of accounts in extraction prompt — org's active GLAccount rows queried and injected into extraction prompt via `config["gl_account_catalog"]`. Falls back to hardcoded default list
- [x] Extraction self-correction pass — `services/extraction_self_correction.py` verifies arithmetic (subtotal+tax≈amount), date ordering, line-item math. Violations lower confidence (-0.2) and add warnings. Controlled by `org_settings.extraction.self_correction_enabled`
- [x] Learning from corrections — per-vendor correction cache (see below)
- [x] RAG-based extraction priors — pgvector + few-shot retrieval (see below)
- [x] Semantic duplicate detection — near-duplicate catch via cosine similarity on the same `invoice_embeddings` store. Threshold `FEOH_DUPLICATE_SIMILARITY_THRESHOLD` (default 0.95, tighter than RAG retrieval). See `backend/docs/ai-extraction.md` § Duplicate detection.
- [x] Stuck-extraction reaper — `services/extraction_reaper.py` sweeps every 60s (configurable) and transitions invoices in `pending` longer than `FEOH_EXTRACTION_TIMEOUT_SECONDS` to `failed`. Started in `main.lifespan`; one-shot CLI at `scripts/reap_stuck_extractions.py`.

**Files:** `backend/app/services/extraction_adapters/`, `backend/app/services/extraction.py`, `backend/app/services/vendor_priors.py`

#### Learning from corrections — per-vendor cache (shipped)

When a reviewer corrects extracted fields during approval, the corrected values are stored keyed by `(vendor_id, field_name)` in the `vendor_extraction_priors` tenant table. On the next extraction for the same vendor, low-confidence values for cached fields are overlaid with the stored values. Only "vendor-consistent" fields are cached (currency, tax_rate, payment_terms, payment_method, vendor_address, vendor_tax_id, remit_to_address, gl_account, cost_center) — never per-invoice fields like amount or invoice_number.

This is deterministic and requires no ML infrastructure. It handles the 80% case ("same vendor's invoices follow the same pattern") with zero cold-start cost.

#### RAG with pgvector (shipped)

Semantic-similarity learning complementing the per-vendor cache. At correction time, the invoice's PyMuPDF-extracted text is embedded with `text-embedding-3-small` (mock adapter available for local dev) and stored in `invoice_embeddings` (pgvector) alongside the final corrected fields. At extraction time, the incoming invoice's text is embedded, top-3 semantic neighbors are retrieved via cosine distance, and the matched `(invoice_text, corrected_fields)` pairs are injected into the Claude Vision prompt as few-shot examples.

Tenant-scoped by default (no cross-tenant leakage). HNSW index on the embedding column for approximate nearest-neighbor search at scale. Metadata about which neighbors were used is persisted on `InvoiceExtractionResult.priors_metadata` and surfaced in the invoice detail UI via `GET /api/invoices/{id}/priors`.

Conflict resolution: the per-vendor cache (see above) runs AFTER the AI output and overrides low-confidence fields, so when cache and RAG disagree on a field, the cache wins — per-vendor explicit corrections are more authoritative than semantic retrieval.

---

### 2/3-Way PO Matching & Auto-Validation
**Status:** Done — matching runs after every extraction and on every invoice mutation. Mismatches and missing POs route into the exception queue. Modal renders a PO Match panel with status, variance, and issues. PO + GR management UIs and adapter-driven ERP sync are live.

- [x] 2-way match: invoice vs. PO (amount, vendor)
- [x] 3-way match: invoice vs. PO vs. goods receipt (quantity received)
- [x] Configurable tolerance thresholds (default 5%)
- [x] Vendor-aware matching (PO lookup by vendor_id)
- [x] Wired into extraction/review pipeline — `services.invoice_warnings.refresh_warnings` runs `match_invoice_to_po` whenever an invoice changes; result is persisted on `invoice.po_match` (JSONB column added in migration 0006)
- [x] Match result display in invoice modal — color-coded panel (matched / mismatch / partial / no PO) with PO #, variance, issues
- [x] Routes mismatches to exception queue — `po_mismatch` exceptions auto-created, severity scaled (error for missing PO, warning for amount variance, info for partial 3-way receipt)
- [x] PO management UI — list page with status chips + search + Sync from ERP toolbar action; click-through detail modal showing line items and linked invoices (matches by `po_number`)
- [x] Goods receipt UI — `/goods-receipts` list page with received-date / status / line-count columns; detail modal shows linked PO + line items received; backend GET endpoints support `?po_id=` and `?status=` filters
- [x] PO sync from ERP — `POST /api/purchase-orders/sync-erp` now dispatches via `get_erp_adapter().list_pos()`. Mock adapter ships a deterministic three-PO catalogue; Merge.dev adapter walks paginated `/purchase-orders` and maps to the unified `PoPayload`; NetSuite + Business Central inherit the base's `[]` default until those endpoints are wired (sync no-ops rather than 500s)

**Files:** `backend/app/services/po_matching.py`, `backend/app/models/procurement.py`

---

### AI Auto GL Coding
**Status:** Done. Claude Vision suggests GL code + cost center, constrained to the org's active chart of accounts; cached vendor priors and RAG-retrieved approved invoices both feed the prompt; suggestions are validated post-extraction; admins can backfill via bulk re-code.

- [x] AI suggests GL code + cost center during extraction
- [x] Confidence score per suggestion
- [x] Auto-apply above 0.7 threshold
- [x] Learn from corrections — reviewer corrections to `gl_account` / `cost_center` feed the per-vendor correction cache (see AI extraction section). Future extractions for the same vendor overlay the cached code on low-confidence suggestions.
- [x] Custom chart of accounts per org in the prompt — `services.extraction.run_extraction` queries the org's active `GLAccount` rows and injects them via `config["gl_account_catalog"]`; the Claude Vision adapter swaps the `{{GL_ACCOUNT_CATALOG}}` placeholder. Falls back to a static default list when the org hasn't synced a chart yet.
- [x] RAG-driven GL coding — `services.rag.retrieve_similar` fetches nearest-neighbor approved invoices via `invoice_embeddings`; `SNAPSHOT_FIELDS` includes `gl_account` so the few-shot prompt prepended to extraction surfaces the historical code. New vendors whose layout resembles a known one inherit GL signal from the neighbor.
- [x] Bulk re-code capability — `POST /api/invoices/bulk-recode-gl` (admin-only). Date / vendor scoped; priors-first then optional AI fallback. Defaults to `dry_run=true` and returns a `{matched, would_change, by_source, skipped, changes}` report. Admin UI: "Bulk Re-code GL" button on `/invoices` opens a preview-then-apply modal. Audit-logs each persisted change as `invoice.gl_recoded`.
- [x] GL code validation against chart of accounts — post-extraction guard in `run_extraction` rejects any AI-suggested code (or cached vendor prior that's gone stale) that isn't in the org's active chart, drops it from the invoice header and line items, and emits a structured `gl_account_invalid` warning. No-ops when the org hasn't synced a chart yet.

---

### Recurring / Subscription Invoices
**Status:** Shipped. A `RecurringInvoiceTemplate` (tenant-scoped, migration 0046) captures vendor + amount + GL coding + entity + cadence; the `recurring_invoices` background sweep (mirrors `contract_renewal` / `discount_auto_trigger`, off by default via `FEOH_RECURRING_INVOICES_ENABLED`) generates the next pre-coded `Invoice` into the approval queue, idempotent on `(template, period_key)` via the partial unique index `uq_invoice_recurring_period`. `/api/recurring` CRUD + pause/resume/end/generate-now + upcoming-schedule + history; the `/recurring` frontend route under the Billing nav group. See `backend/docs/recurring-invoices.md`.

Predictable, fixed-cadence spend (rent, SaaS seats, utilities, insurance) shouldn't need a fresh upload + extraction every period. A recurring template auto-generates the next invoice on schedule, pre-coded and pre-matched, so it lands straight in the approval queue. Common in Bill.com, Tipalti, and Stampli; absent here today.

- [x] `RecurringInvoiceTemplate` tenant-scoped model — vendor, amount, GL coding, entity, cadence (monthly / quarterly / annual + `day_of_period`), start/end, `next_run_on`; Alembic migration 0046 fans out to every tenant
- [x] Background generation sweep (`services/recurring_invoices.py`) — mirrors the existing `contract_renewal` / `discount_auto_trigger` loop pattern (`FEOH_RECURRING_INVOICES_ENABLED` master switch, off in local dev); generates the next `Invoice` into the queue and advances `next_run_on`. **Idempotent** on `(template_id, period_key)` (partial unique index `uq_invoice_recurring_period`) so a double-fire never double-creates. Never moves money
- [x] Variance handling — flags when an arrived invoice for a recurring vendor deviates from the template amount beyond `variance_tolerance_pct` (reuses the price-variance signal from data enrichment) rather than blindly trusting the schedule
- [x] Generated invoices link back to their template (`invoices.recurring_template_id`) + pause / resume / end / generate-now controls on the template; every generation + lifecycle change audited (`recurring_template.*` actions)
- [x] Frontend `/recurring` route — template CRUD, status filter chips, KPI row, upcoming-schedule preview + generated-invoice history in the detail modal

**Competitors:** Bill.com (recurring bills), Tipalti (subscription spend), Stampli, Airbase (SaaS spend management)

---

### Invoice PDF Management (Upload / Replace / Delete from Invoice Detail)
**Status:** Done. Today the invoice's source file (`Invoice.file_key`) can be replaced or removed directly from the invoice detail page — a mis-scanned or wrong-file upload is no longer stuck for the life of the invoice.

Lets a reviewer fix a bad attachment (wrong file, illegible scan, missing pages) directly from the invoice detail page instead of deleting and re-creating the whole invoice.

- [x] `PUT /api/invoices/{id}/file` (replace) + `DELETE /api/invoices/{id}/file` (remove) — reuse the existing `storage.upload_invoice_file` / filename-sanitizer path, cross-tenant-checked like the other file endpoints; `PUT` 404s when there's no file to replace (points the caller at the attach endpoint instead), `DELETE` 404s when there's no file to delete
- [x] Role gate — same admin/ap_manager/cfo gate as the existing manual-entry create/attach endpoints (`require_roles`); ap_clerk is excluded (403), matching the frontend's `!isClerkOnly` check — no new permission was introduced
- [x] Status gate — both endpoints refuse (409) once the invoice has reached the terminal `done` state. Resolved the open question on `paid`: it stays mutable — the literal ask was "terminal state," and `paid` is not `done` in the state machine (`payment_scheduled`/`paid` can still void back to `approved`), so freezing the file there would be inconsistent with every other file-adjacent gate in this router
- [x] Audit trail — resolved in favor of the existing append-only `audit_log` + the invoice modal's Activity timeline: every replace/delete writes `invoice.file_replaced` / `invoice.file_deleted` via `dispatch_audit`, rendered as an Activity entry through the same path as every other invoice status-change/correction. The supplier chat thread (`supplier_chat.py`) was rejected as the surface — it's vendor-facing collaboration, and a file swap is an internal AP action with no reason to be externally visible
- [x] Frontend — a file-management toolbar in `InvoiceModal` (file viewer section): "Upload File" (aria-label "Upload invoice file") when the invoice has no file; "Replace" + a two-click armed-confirm "Delete File" → "Confirm Delete" when it does. Hidden entirely for ap_clerk or once the invoice is `done` — the same role + status checks the backend enforces, never client-only. Every action leaves the modal open and refreshes its file pane + Activity timeline live
- [x] e2e coverage (`tests-e2e/invoices/file-management.spec.ts`: upload → replace → delete lifecycle + Activity-timeline entries, ap_clerk role-gate on any invoice, a direct-API 409 probe against a `done` invoice) + backend tests (`backend/tests/test_invoice_file_management.py`)

---

### Manual Invoice Entry (No-OCR Creation)
**Status:** Done. `POST /api/invoices` (`create_invoice`) already keyed a full field set at `new` status; this shipped the missing frontend path plus optional-file attach. AP clerks can now key an invoice in directly instead of being forced through OCR.

- [x] `POST /api/invoices/{id}/file` — a new attach-only companion endpoint (not a single multipart POST on create, to keep `create_invoice`'s existing JSON contract untouched): refuses with 409 once the invoice already has a file, so it can never double as a "replace" path (that's the separate `PUT {id}/file` in the Invoice PDF Management item below). Same admin/ap_manager/cfo gate as create; audits `invoice.file_attached`
- [x] Frontend `CreateInvoiceModal` — a fresh, focused create form (vendor/invoice#/amount/currency/dates/PO/payment terms/GL account (dropdown when the org catalog is loaded)/cost center) + an optional file picker; deliberately NOT built into the existing edit-focused `InvoiceModal` (which carries audit-timeline/chat/PO-match/extraction-confidence machinery that doesn't apply to a blank form). Reachable via "+ Create Invoice" on the `/invoices` toolbar, gated to admin/ap_manager/cfo (`auth.hasAnyRole`); the backend enforces regardless. A rejected file (bad type/oversized) surfaces its own toast without rolling back the already-created invoice
- [x] Submits into `new` — no different treatment than an extracted invoice from that point on; goes through the same review/approval/PO-match pipeline
- [x] `InvoiceExtractionResult` is simply absent for a manually-entered invoice — the invoice modal's summary/activity view degrades cleanly (verified manually: no extraction panel, no broken state)
- [x] i18n across all 6 locales + e2e coverage (`tests-e2e/invoices/create-manual.spec.ts`: required-field gating, create with/without a file, ap_clerk role-gate) + backend tests (`backend/tests/test_invoice_manual_entry.py`)

---

## Priority 2: Workflow, Approvals & Exceptions

### Exception Queue
**Status:** Done

Dedicated page for handling flagged invoices — mismatches, rejections, anomalies.

- [x] Exception list page with filters (type, status, severity, date)
- [x] Exception types: PO mismatch, duplicate, fraud flag, rejection, extraction failure, unverified vendor, missing data
- [x] Resolution actions: resolve, escalate, dismiss (with resolution note)
- [x] Summary: counts by status, breakdown by type
- [x] Link back to invoice from exception
- [x] Assignment — `POST /api/exceptions/{id}/assign` (org-scoped user lookup); auto-assignment from `Organization.settings.exceptions.auto_assign_by_type` at creation; `?assigned_to_user_id` filter on list
- [x] SLA tracking — `due_at` (sla_hours_by_type / default_sla_hours) + `is_overdue` flag in list payload; `time_to_resolution_hours` populated only on terminal transitions (resolve / dismiss)
- [x] Bulk resolution — `POST /api/exceptions/bulk/resolve` with partial-success contract `{updated, skipped:[{id, reason}]}` matching the invoice bulk endpoints

**Files:** `backend/app/api/exceptions.py`, `backend/app/models/exception.py`, `backend/alembic/versions/0013_exception_assignment_sla.py`, `backend/tests/test_exception_assignment.py`, `frontend/tests-e2e/exceptions/assign-bulk.spec.ts`

---

### Advanced Approval Routing
**Status:** Done — all routing strategies, escalation, email/Slack/Teams approval, and the approval-matrix UI shipped

Current state: manual, specific, auto, and chain approval strategies. Amount-based auto-approve, CFO gate, max-amount rejection, multi-level chains, segregation of duties, and delegation are implemented. No escalation, email/Slack approval, or visual matrix builder yet.

- [x] Amount-based auto-approve (auto_approve_below threshold)
- [x] CFO role gate (require_cfo_above threshold)
- [x] Max invoice amount rejection (max_invoice_amount)
- [x] Multi-level approval chains (strategy="chain", ApprovalLevelConfig)
- [x] Segregation of duties (require_segregation, uploaded_by_id)
- [x] Delegation / out-of-office (delegate_to_id, delegate_until, /api/auth/delegation)
- [x] Department/GL-based routing — `ApprovalLevelConfig.routing_rules` filters by gl_account / cost_center / department / vendor_id; AND-composes with min/max amount; unknown fields fail open so a stale UI config can't lock the chain
- [x] Parallel approvals — `parallel_mode: "any" | "all"`. `any` = `required_approvals` distinct users (default, legacy behaviour). `all` = every listed approver_id must approve.
- [x] Escalation rules — `escalation_hours` + `escalation_to_user_ids` per level. Background sweeper (`services/approval_escalation.py`) appends the targets onto the level's `approver_ids` once the level's `entered_at` is older than `escalation_hours`. Idempotent. Toggleable via `FEOH_APPROVAL_ESCALATION_ENABLED`.
- [x] Email approval — approve/reject directly from the assignment email without logging in. Per-recipient HMAC-signed, expiring, single-use token in the link (`services/email_action_token.py`); public GET confirm page → POST performs the action through the normal `services/review` path (segregation + CFO gate + thresholds + immutable audit row + approval signature all apply). Two-step click is prefetch-safe (GET never mutates). Single knob `FEOH_EMAIL_ACTION_SIGNING_KEY` (empty → off, fail-closed); local-first via console/Mailpit email. See `backend/docs/email-approval.md`.
- [x] Slack approval — approve/reject from Slack Block Kit buttons, no login. `POST /api/approvals/slack/interactivity` verifies Slack's `v0=` request signature + a ±5min timestamp window, then the per-action HMAC token (reuses the email-approval `email_action_token` with a `channel="slack"` claim, single-use via the Redis `jti`), and performs the decision through `services/review` (segregation + CFO gate + thresholds + immutable audit + signature all apply). `FEOH_SLACK_SIGNING_SECRET` empty → fail-closed. See `backend/docs/slack-approval.md`
- [x] Teams approval — approve/reject from a Microsoft Teams approval card, no login. `POST /api/approvals/teams/interactivity` verifies the Teams Outgoing-Webhook HMAC (`Authorization: HMAC <base64-sha256>` over the raw body with `FEOH_TEAMS_SECURITY_TOKEN`, optional `X-Teams-Request-Timestamp` replay window) **and** the per-action HMAC token (reuses the email-approval `email_action_token` with a `channel="teams"` claim, single-use via the Redis `jti`), then runs the decision through `services/review` (segregation + CFO gate + thresholds + immutable audit + signature all apply). Opaque 200 ack on every path. `FEOH_TEAMS_SECURITY_TOKEN` empty → fail-closed. See `backend/docs/teams-approval.md`
- [x] Approval matrix UI — `frontend/src/lib/components/ApprovalMatrixEditor.svelte` plugged into `/workflows/[id]` when `approver_strategy=chain`. Edits levels (name, amount range, parallel mode, approvers, routing rules, escalation hours + targets); persists through the existing `PATCH /api/workflows/{id}` path.

**Competitors:** Coupa (matrix approval), Tipalti (parallel + Slack), Stampli (email/Slack), Airbase (Slack-native), Basware (conditional chains)

---

### Backend RBAC Enforcement
**Status:** Done — `require_roles(*roles)` dependency, full permission matrix applied across every router. Coverage gate in `tests/test_rbac.py` blocks regressions.

- [x] `require_roles(*roles)` dependency in `app/api/deps.py` — any-of semantics, 403 on miss, WARN-level log on denial
- [x] Endpoint-level permission mapping for all 4 roles (admin / ap_manager / ap_clerk / cfo) — see `docs/authentication.md` § RBAC
- [x] Return 403 Forbidden (not just hide UI elements)
- [x] Unit tests for `require_roles` semantics + coverage gate that fails CI if a new endpoint ships without an auth dependency
- [x] Log unauthorized access attempts at WARN level (sufficient for monitoring; persistent audit-log entries deferred to SOC 2 prep)
- [x] Segregation of duties enforcement (approver ≠ creator) — default-on baseline; `check_segregation` runs on every `approve_invoice` call. Opt-out per workflow via `require_segregation: false` for single-operator orgs
- [x] Per-org custom-role *CRUD* — `Role.organization_id` nullable (NULL = system, non-NULL = org-scoped). Admin CRUD at `/api/admin/roles` (POST / PATCH / DELETE) refuses to touch system rows and rejects creation under reserved names. Frontend surface at `/admin/roles` with a system / custom split.
- [x] Per-org custom roles **with teeth** — *Done.* Custom roles now grant access via the granular permission layer (`roles.permissions` + `require_permission`), so an org can split fraud-sensitive duties. See *Granular permissions / segregation of duties* below.

**Files:** `backend/app/api/deps.py`, every `backend/app/api/*.py` router, `backend/tests/test_rbac.py`, `frontend/src/routes/admin/roles/+page.svelte`

---

### Granular permissions / segregation of duties
**Status:** Done — the additive permission layer below shipped. Custom roles now
grant access; the fraud-sensitive splittable endpoints are gated by
`require_permission`. The four system roles are unchanged (their defaults
reproduce the prior `require_roles` matrix exactly). See
`docs/authentication.md` § Granular permissions / segregation of duties.

**Why this and not "custom roles inherit system roles":** users can already
hold multiple system roles (the assignment UI checkboxes them; `require_roles`
is any-of), so *bundling whole roles* adds nothing you can't do today. The real
gap is that fraud-sensitive duties are conflated **inside** `ap_manager` — a
single `ap_manager` can both approve a vendor **bank-detail change**
(`POST /api/vendors/change-requests/{id}/approve`) and **execute a payment run**
(`POST /api/payments/runs/{id}/execute`). That's a textbook SoD violation (the
person who can redirect where money goes can also send it), and no amount of
role-bundling can *split* it. Only a permission layer can. This composes with
the existing instance-level SoD (`check_segregation`, approver ≠ creator).

**Design (additive, backward-compatible — existing behavior unchanged until a
custom role is deliberately given a permission). All shipped:**

- [x] **Permission catalog** — named constants in `app/api/permissions.py`
  (`invoice.approve`, `payment_run.approve`, `payment.execute`, `payment.void`,
  `vendor.bank_change.approve`, `vendor.block`, `vendor.manage`, `user.manage`)
  + labels + `GET /api/admin/permissions`. The *sensitive, splittable* set.
- [x] **System-role → default permissions map** — `ROLE_DEFAULT_PERMISSIONS`
  reproduces today's matrix exactly, so the four system roles behave
  identically with zero migration of their semantics.
- [x] **Custom-role permissions** — JSONB column `roles.permissions` (migration
  `0062_role_permissions`, control-plane-only since `roles` is control-plane).
  System roles leave it NULL (resolve via the default map); custom roles store
  an explicit, sanitized list.
- [x] **Effective permissions** — union over all the user's roles (system via
  the default map, custom via their stored list). Computed once in
  `get_current_user`, cached on `User.effective_permissions`.
- [x] **Enforcement** — `require_permission(*perms)` alongside `require_roles`.
  Migrated **only the splittable sensitive endpoints** (payment-run
  approve/execute, payment void, vendor bank-change approve, vendor
  block/unblock, vendor manage, user management); everything else stays on
  `require_roles`. `test_rbac.py`'s coverage gate stays green.
- [x] **Frontend** — `GET /api/auth/me` returns the effective `permissions`
  array; `auth.can(perm)` helper added; the Execute / Void / vendor
  Block-Unblock controls converted. `isManager`/`isCfo` keep working for
  everything not yet split.
- [x] **Custom-role UI** — permission checkboxes in the `/admin/roles`
  create/edit modal; the "custom roles confer no permissions" copy reverted.

**Tests:** permission-resolution unit tests (`tests/test_permissions.py` —
system default map + custom union + sanitize + `require_permission` semantics);
`test_rbac.py` extended (`test_split_endpoints_are_permission_gated`) so the
split endpoints assert permission gating; the SoD guarantee — a custom role
granted only `invoice.approve` is 403'd on payment execution — is asserted at
the dependency level (`test_require_permission_rejects_non_holder`).

**Files:** `backend/app/api/permissions.py` (new), `app/api/deps.py`,
`app/api/{payments,vendors,admin,auth}.py`, `app/models/user.py`,
`app/schemas/{admin,auth}.py`, migration `0062_role_permissions`,
`frontend/src/lib/stores/auth.svelte.ts`,
`frontend/src/lib/types/admin.ts`, `frontend/src/lib/stores/admin.svelte.ts`,
`frontend/src/lib/components/admin/RolesPanel.svelte`, the converted controls
(`modals/RunDetailModal.svelte`, `modals/VendorModal.svelte`,
`routes/payments/+page.svelte`), `docs/authentication.md`.

---

### Enhanced Fraud Detection
**Status:** Done

Eight fraud rules implemented in `services/invoice_warnings.py`, each
gated by a per-org tunable. Rules raise both an inline warning on the
invoice and an `Exception` row when the signal is actionable. Defaults
in `DEFAULT_FRAUD_RULES`; org admins override via the Fraud Detection
section on `/organization` (UI maps onto `settings.fraud_rules`).

- [x] Semantic duplicate detection — cosine similarity on `invoice_embeddings` catches near-duplicates the exact-match rule misses. See `backend/docs/ai-extraction.md` § Duplicate detection.
- [x] Vendor bank account / remit-to change — flags when an invoice's `remit_to_address` differs from prior approved invoices for the same vendor.
- [x] LLM-based anomaly detection — feeds the invoice + last N approved invoices to the configured extraction provider with a "in-pattern for this vendor?" prompt; opt-in (`llm_anomaly_enabled=False` by default; one LLM call per incoming invoice). Module: `services/llm_fraud_detection.py`.
- [x] Statistical amount anomaly — fires when `amount > vendor_mean + N·σ` over the vendor's prior approved invoices. N + min-history are tunable.
- [x] Rush payment pattern — `due_date - invoice_date <= rush_payment_max_days`.
- [x] New vendor + large amount — vendor age < `new_vendor_max_age_days` AND amount ≥ `new_vendor_large_amount`.
- [x] Personal email domain — flags vendors whose contact email matches a configurable allowlist of free-mail providers.
- [x] Configurable fraud rules per org — `Organization.settings.fraud_rules` takes a partial override; unknown keys are dropped silently so we can ship new rules without a settings migration. Frontend editor at `/organization` Fraud Detection card.

**Files:** `backend/app/services/invoice_warnings.py`, `backend/app/services/llm_fraud_detection.py`, `backend/app/api/organization.py:get_fraud_rule_defaults`, `frontend/src/routes/organization/+page.svelte` (Fraud Detection card), `backend/tests/test_fraud_rules.py` (23 tests), `backend/tests/test_llm_fraud_detection.py` (19 tests), `frontend/tests-e2e/organization/fraud-rules.spec.ts` (7 specs).

---

## Priority 3: Payments

### Payment Run UI
**Status:** Done (core flow) — queue → select + per-row method → create draft → review in modal → execute. Drilldown from Runs tab works.

- [x] Payment queue page — approved invoices sorted by due date, overdue highlighting
- [x] Payment history — all methods in one table (ACH, wire, check, card badges)
- [x] Payment runs list — batch history with status, total, count
- [x] Summary bar — total paid, pending, queue count, payments, rebates earned
- [x] Payment queue backend — `GET /api/payments/queue` and `GET /api/payments/summary`
- [x] Create payment run — select invoices in the queue, choose method per row, totals shown
- [x] Run detail modal — status, total, payments table, references; opens after creating a draft and from any row in the Runs tab
- [x] Execute payment run — separate from create, so a draft can be reviewed before money moves
- [x] Early-pay discount highlighting with savings calculation — queue surfaces discount banner + chip column when `PaymentSchedule.discount_date` is in window
- [x] Void/cancel payment capability — `POST /api/payments/{id}/void` (RBAC: admin/manager), adapter-level void with `voided` status + audit row
- [x] Cancel a draft run before executing — `POST /api/payments/runs/{id}/cancel` releases the invoices back to `ready_for_review`
- [x] Payment remittance generation (PDF/email to vendor) — `GET /api/payments/{id}/remittance` returns reportlab-rendered PDF
- [x] Approval workflow on a draft run (CFO sign-off before execute) — `requires_cfo_approval` gate + `POST /api/payments/runs/{id}/approve` (CFO-only)

**Files:** `backend/app/api/payments.py`, `backend/app/models/payment.py`, `backend/app/services/remittance_pdf.py`, `frontend/src/lib/components/RunDetailModal.svelte`

**See also:** [payments.md](../backend/docs/payments.md)

---

### Virtual Card Program
**Status:** Done — adapter pattern (Lithic + Nium), models, API endpoints, org config UI, webhook handler, frontend Cards tab (list + dashboard + rebates), and payment-run integration (`virtual_card` method + `execute_payment_run` card issuance) all shipped.

Generate single-use virtual cards per invoice payment. Earn 1-2% rebates on every card payment. Primary monetization channel. See [virtual-cards.md](../backend/docs/virtual-cards.md) for full design.

- [x] VirtualCard and CardRebate data models
- [x] Card adapter pattern with dispatcher (Lithic for US/UK/EU, Nium for global)
- [x] Lithic adapter — card creation, detail retrieval, cancellation, status
- [x] Nium adapter — same interface for 40+ countries
- [x] Mock adapter for development/testing
- [x] Card API endpoints — generate, list, details, cancel, webhook, rebates, dashboard
- [x] Card detail security — role-restricted (admin/manager), audit-logged
- [x] Webhook handler — process charge/settlement events, auto-create rebates
- [x] Platform/BYOK dual model — platform keys in env vars, customer keys in org settings
- [x] Card config in organization settings UI — region auto-selects provider
- [x] Vendor `accepts_virtual_cards` field
- [x] Card list in payments page — dedicated Cards tab + card_last_four/card_provider join on history rows
- [x] Card generation in payment run — `execute_payment_run` calls `card_issuance.issue_card_for_invoice` when `method == "virtual_card"`
- [x] Vendor email notification — pluggable email adapter sends single-use reveal URL on issuance
- [x] Rebate dashboard — monthly earnings + YTD totals block on payments page
- [x] Supplier portal integration — `GET /portal/cards/{token}` returns full card detail once (sha256-hashed token, 7-day expiry)

---

### International Payments
**Status:** Complete — see `backend/docs/international-payments.md`. Migrations 0017 + 0018. Sanctions provider integration today ships as a `complyadvantage` skeleton with the wire shape correct; the live API key needs to be set in `Organization.settings.compliance.sanctions.api_key`. Wise / Tipalti payment-rail adapters slot in via `@register_payment_adapter` — Modern Treasury covers most demand for now.

- [x] Multi-currency payment execution — pay in vendor's local currency (`services/international_payments.py::prepare_international_payment` builds the Payment row with `source_currency`, `source_amount`, `fx_rate`, `fx_locked_at`, `corridor`, `target_country`)
- [x] FX rate management — real-time rates, rate lock at payment creation. Pluggable adapter (`services/fx_adapters/`) — mock + Open Exchange Rates today; Wise / Tipalti slot in via `@register_fx_adapter`
- [x] Cross-border ACH (NACHA Global ACH / IAT) — `method=international_ach` for USD→CA / MX / GB / BR / select LATAM corridors; cheaper than SWIFT for low-value recurring payments
- [x] International wire transfers (SWIFT) — `method=international_wire`; SWIFT/BIC validation in `utils/banking.py`
- [x] SEPA payments (EU) — `method=sepa`; IBAN mod-97 + SEPA zone membership in `utils/banking.py`; corridor picker auto-routes EUR→SEPA-country to SEPA
- [x] Payment corridor optimization — `services/corridor_quotes.compare_quotes` ranks N processor quotes (cheapest by default; `fastest` mode for urgent runs). Org enables via `payments.providers=[...]`; legacy single-provider config still works
- [x] Regulatory compliance per corridor (KYC/AML) — sanctions / PEP screening (`services/sanctions_adapters/` — mock + ComplyAdvantage skeleton), KYC gating per high-risk corridor, AML trailing-12m-spend signal, append-only `sanctions_checks` audit log
- [x] FX gain/loss tracking — `compute_fx_gain_loss` (booked vs realized); columns persisted on `payments` for reporting

**Competitors:** Tipalti (196 countries, 120 currencies), Coupa Pay, Basware Pay, Airbase

---

### Bank / Payment Processor Integration
**Status:** Done (Modern Treasury + mock) — adapter pattern lives in `backend/app/services/payment_adapters/`. Real ACH/wire/RTP flow works end-to-end (create payment → idempotent processor call → webhook-driven status updates → ERP sync on settle).

- [x] Adapter scaffold (`base.py`, `dispatcher.py`, `mock_adapter.py`)
- [x] Modern Treasury adapter — full payment-order create + status lookup + webhook parsing with HMAC-SHA256
- [x] Per-org config (`Organization.settings.payments`) — provider, credentials, originating account, webhook secret, sandbox flag
- [x] Frontend org-settings UI for selecting + configuring the processor
- [x] Webhook handler (`POST /api/payments/webhook/{tenant_slug}/{provider}`) — drives `submitted → completed/failed` transitions
- [x] `payments.provider`, `provider_payment_id`, `failure_reason`, `submitted_at`, `completed_at` columns (alembic 0007)
- [x] `execute_payment_run` dispatches via adapter; run status reflects rollup (`completed` / `partial` / `submitted` / `failed`)
- [x] Vendor counterparty management UI — `Bank` action on the vendors grid opens a counterparty modal
- [x] Reconciliation job — `services/payment_reconciler.py` sweeps every tenant on a timer, re-polls non-terminal payments, force-fails past `FEOH_PAYMENT_RECONCILE_MAX_AGE_HOURS`. Disabled by default in local dev.
- [x] Stripe Treasury / Increase / Column adapters — ACH + wire (Stripe Treasury, Increase, Column) via the same `@register_payment_adapter` pattern. Idempotency, HMAC webhooks with replay protection on Stripe + Increase (timestamped signatures), plain HMAC on Column.
- [x] ACH integration — Dwolla adapter (OAuth client-credentials + token caching) for ACH-specialist orgs; ACH also available via Stripe Treasury / Increase / Column for orgs using their other rails too. Plaid bank-link is a separate concern (vendor onboarding, not payment origination) and remains pending.
- [x] Wire transfer integration — Modern Treasury, Stripe Treasury, Increase, Column all support `method=wire`. Domestic and international (SWIFT) wires both flow through the same path (`international_wire` via the corridor picker).
- [x] Check printing service — Checkeeper adapter (`method=check`): prints + mails physical checks. Mailing-address validation refuses checks without a valid US address before submitting.
- [x] Payment status webhooks from processor — every adapter implements `parse_webhook` with HMAC signature verification + Redis-based event dedup (`services/webhook_security.py`). Stripe + Increase use timestamped signatures with 5-min replay protection.
- [x] Bank reconciliation — import statements, auto-match. CSV importer (`services/bank_reconciliation.py::parse_csv_statement`) handles the common bank export formats; the matcher runs three strategies (provider_id → amount+date → fuzzy vendor) with confidence scores 100 / 80 / 50–70. Unmatched debits surface as exceptions. See `backend/docs/bank-reconciliation.md`.

---

### Positive Pay / Payment Fraud File
**Status:** Done

Bank-side fraud control: export an issued-items file so the bank only honors checks/ACH debits we actually originated. A natural extension of the existing `checkeeper` check-printing + payment-rail adapters, and a frequent enterprise-AP procurement requirement.

Shipped: a `PositivePayFile` model + migration 0048 (tenant-gated + fans out; idempotent `uq_positive_pay_run_format` partial unique index for one check-issue file per run+format), pluggable per-bank formatter adapters (`positive_pay_adapters/`: `csv` + `fixed_width`), a pure return classifier (`matched_ok` / `amount_mismatch` / `not_on_file`) + the async file-item builders, the `/api/positive-pay` router (generate check-issue + ACH-authorization, list/detail, MinIO download with a cross-tenant gate, process-return, delete), and the `/positive-pay` frontend route. PII handled per the invariant (full account/routing numbers only in the MinIO file; DB stores `account_last4`; audit/logs/errors PII-free). See `backend/docs/positive-pay.md`.

- [x] Positive Pay file export (check issue file) — per-bank format (BAI2-ish / fixed-width / CSV) of `{check_number, payee, amount, issue_date, account}` for every check in an executed payment run; pluggable per-bank formatter like the existing payment adapters
- [x] ACH Positive Pay / debit-block authorization list — export approved originators for ACH debit filtering
- [x] Exception return handling — ingest the bank's "items presented not on file" report and surface mismatches as fraud exceptions *(Both fraud signals raise a deduped `fraud_flag` Exception: altered cheques map to their invoice; never-issued `not_on_file` cheques become standalone invoice-less exceptions — migration 0049 made `Exception.invoice_id` nullable for exactly this. See the doc.)*
- [x] Generation is idempotent per run + audited; account/routing numbers stay out of logs and error bodies (PII invariant)

**Competitors:** Coupa Pay, Tipalti, AvidXchange (positive pay as a treasury-controls feature)

---

### Vendor Statement Reconciliation
**Status:** Done — pure engine in `backend/app/services/vendor_statement_recon.py`, `/api/vendor-statements` router, `/vendor-statements` frontend route, migration 0047. See `backend/docs/vendor-statement-reconciliation.md`.

Distinct from bank reconciliation (cleared payments ↔ bank lines): this reconciles a **supplier's statement of open items** against our AP ledger to catch missing invoices, double-posted bills, mis-applied credits, and stale balances before month-end close. A core AP-clerk task that's entirely manual today.

- [x] Statement intake — CSV upload (forgiving header sniff, mirrors the bank-rec CSV parser) + manual pasted-lines path, parsed into a normalized list of `{invoice_number, invoice_date, amount, status}` line items, vendor-scoped
- [x] PDF intake via the org's own extraction adapter (`ExtractionAdapter.extract_statement`, an optional capability on `mock` / `claude_vision` / `ollama`) + raw-file storage — the uploaded document is archived to S3 and served back by `GET /api/vendor-statements/{id}/file`. The offline `mock` reader **skips rather than guesses** an ambiguous row; every failure is a fail-closed 422 with a PII-free reason, never a partial run
- [x] Reconciliation engine (`services/vendor_statement_recon.py`, pure) — matches statement lines to our `Invoice` rows by normalized invoice number → amount+date-window fallback; classifies each as *matched* / *amount mismatch* (within/over a tolerance) / *missing on our side* (supplier billed, we never received) / *missing on their side* (we have an open invoice they omitted)
- [x] Persist a `VendorStatementReconciliation` run + `VendorStatementReconLine` results (migration 0047, tenant-gated + fans out); the actionable rows (missing-on-our-side + amount-mismatch) surface as the per-run review queue feeding invoice intake. *(Design note: they're recon **lines**, not `Exception` rows — a deliberate choice for their per-line resolve/ignore lifecycle and side-by-side diff. Migration 0049 has since made `Exception.invoice_id` nullable for the Positive Pay feature, so the "we have no invoice" constraint no longer applies, but recon lines remain the right model here. See the doc.)*
- [x] Frontend reconciliation view (`/vendor-statements`) — upload / manual create, side-by-side statement-vs-ledger diff, per-line resolve/ignore; every mutation RBAC-gated + audited (`vendor_statement_recon.created` / `.line_resolved` / `.deleted`)
- [x] Statement upload UI — an explicit intake-mode choice (type the lines / upload a CSV-or-PDF file), the backend's 422 refusal reason rendered as a persistent inline alert on the form, and on the run detail a source pill, the `extraction` provenance block (adapter / confidence / open items read, plus what the skip-rather-than-guess rule means for the diff below it) and a download of the archived supplier document
- [x] Period close tie-in — `GET /api/vendor-statements/close-readiness` flags vendors whose most-recent open run carries a material (over `FEOH_STATEMENT_RECON_MATERIALITY_DEFAULT`, `?materiality=` override) unreconciled balance

**Competitors:** Tipalti, Basware, Medius (statement reconciliation in close workflows); most SMB tools lack it — a differentiator down-market

---

## Priority 4: Analytics & Reporting

### Dashboard Enhancements
**Status:** Done — operational metrics live on `GET /api/dashboard`. See `backend/docs/analytics.md`.

- [x] Spend by vendor chart — `vendor_spend` (top 10 by amount, served sorted)
- [x] Invoice aging chart — `aging` (current / days_30 / days_60 / days_90_plus)
- [x] Processing time metrics — `processing_time` (avg + median + p95 days from upload→approval and upload→paid; min-sample threshold collapses to 0 below 5 rows)
- [x] Approval bottleneck detection — `approval_bottleneck` (per-approver pending count, oldest age, average age; unassigned rolls under a synthetic key)
- [x] Monthly trend lines — `monthly_trend`
- [x] Discount capture rate — `discount_capture` (eligible / captured / missed counts + amounts + capture_rate_pct)
- [x] Touchless rate tracking — `touchless_rate`
- [x] Export reports as CSV — `GET /api/analytics/export/{report}` for invoice_register / vendor_spend / payment_register / aging_snapshot. PDF deferred (separate reportlab/weasyprint piece).
- [x] Scheduled report delivery via email — migration 0020 + `scheduled_reports` table + `services/scheduled_reports.execute_schedule`. Daily / weekly / monthly cadence; PII-safe failure messages; auto-disable after 5 consecutive failures.

---

### CFO / Finance-Leader Analytics
**Status:** Done — see `GET /api/analytics/cfo` and `backend/docs/analytics.md`.

Dashboard Enhancements above is *operational* (for AP clerks/managers). CFOs and controllers buy on different metrics — the ones that show up in board decks and drive working-capital decisions. Separate surface because the audience and filter defaults differ (entity, period, currency, accrual vs cash).

- [x] Days Payable Outstanding (DPO) trend — `dpo_current` + `dpo_trend` (last 6 months). Computed AP/COGS×period_days; benchmark overlay deferred until we ship industry-benchmark data.
- [x] Cash conversion cycle — `cash_conversion_cycle`, returns NULL when DSO/DIO unavailable (we're AP-only) so the UI shows "needs receivables data" rather than a misleading 0.
- [x] Accruals view — `accruals.{open_po_amount, received_amount, unposted_invoice_amount, total_accrual}`. `received_amount` values goods received but not yet invoiced (the GR/IR leg): the 3-way match is fanned out per PO, each receipted PO contributing `po_total × min(1, gr_qty/po_qty)` — the same received-fraction the PO matcher computes. Pure math in `analytics.value_received_goods`, SQL fan-out in `api/analytics._received_amount` (entity-scoped, fails soft to 0 on tenants without procurement tables).
- [x] Working capital impact — `working_capital_impact_5_days` (avg_daily_outflow × 5; configurable via days-extended param when called via drill-through).
- [x] Supplier concentration — `supplier_concentration` (top-10 / top-50 share, largest vendor, `flagged` when largest exceeds 25%).
- [x] Fraud rate trend — `fraud_rate_trend` (exceptions per invoice per month, last 6 months).
- [x] Early-pay discount ROI — `discount_capture` on the dashboard surfaces $ captured + $ missed; the CFO endpoint quotes the same numbers.
- [x] Rebate yield — `rebate_yield.{yield_pct, annualised_rebates}` (virtual-card rebates / spend × 100 + 12/months annualisation).
- [x] Forecast variance — `POST /api/analytics/forecast_variance` accepts a CFO-supplied forecast and returns actual vs forecast vs variance vs variance_pct per month. Forecasts are NOT persisted — the CFO pastes from their FP&A tool.
- [x] Drill-through — `/api/analytics/drill/spend_concentration`, `/api/analytics/drill/dpo`. Per-metric drill is the design pattern; new metrics get a new drill endpoint as they ship.

**Competitors:** Coupa (Spend Intelligence), Tipalti (CFO Insights), SAP Ariba (Spend Visibility), AppZen (audit analytics). This is where enterprise AP tools differentiate from SMB tools.

---

### Custom Report Builder (ad-hoc / self-serve)
**Status:** Shipped. Closes the last Tier-2 competitive gap — Dashboard / CFO analytics + scheduled delivery + CSV/PDF export were all fixed-shape; this adds self-serve report building. A user picks a **data source** (`invoices` / `payments` / `vendors` / `expenses`), **group-by dimensions** (with optional `day|month|quarter|year` date-grain bucketing), **aggregate measures** (`sum`/`avg`/`count`/`min`/`max`), and **whitelisted filters** (`eq/ne/gt/gte/lt/lte/in/contains/between`), then runs it ad-hoc, saves it, or exports it branded (CSV / PDF).

The security boundary is `app/services/report_builder.py`: a hardcoded `REPORT_SOURCES` catalog maps catalog KEYS → real SQLAlchemy columns. The client only ever sends catalog keys — never a raw column/table name — and any key/agg/op/grain outside the catalog is a 422 that never reaches SQL (`compile_spec`). Runs go through the `get_tenant` chokepoint (tenant isolation) + honour `X-Entity-ID`; money measures serialize as exact decimal **strings**.

- [x] Catalog — `GET /api/reports/catalog` (four sources with their dimensions / measures / filters + enum values).
- [x] Saved definitions — `GET/POST/PATCH/DELETE /api/reports` (tenant + entity-scoped `ReportDefinition`, migration 0071, spec as JSONB). Reads = all four roles; mutate = admin/ap_manager/cfo; every mutation writes a PII-free `report.created/updated/deleted` audit row.
- [x] Run — `POST /api/reports/run` (ad-hoc, paginated) + `POST /api/reports/{id}/run` (saved).
- [x] Export — `GET /api/reports/{id}/export?format=csv|pdf` (reuses the shared brand provenance-header + branded analytics-report PDF).

See `backend/docs/report-builder.md`.

**Competitors:** Coupa, Tipalti, Stampli, Airbase, Medius, Basware.

---

## Priority 5: Multi-Currency & Tax

### Multi-Currency Support
**Status:** Done — reporting-currency rollups + locale-aware display, built on the existing FX adapters (`services/fx_adapters/`) and international-payments rate locking. See `backend/docs/multi-currency.md`.

- [x] Real-time exchange rate lookup — reuses the existing `fx_adapters` (`mock` + Open Exchange Rates) `get_rate`; no new provider
- [x] Auto-convert to reporting currency — `services/currency_conversion.py` + per-org `Organization.settings.reporting_currency` (falls back to `payments.home_currency` → `invoice_defaults.currency` → `FEOH_REPORTING_CURRENCY_DEFAULT`); `/analytics/cfo` (`reporting_spend`) and `/dashboard` (`reporting` block) roll multi-currency invoices into one reporting currency. The rate is locked + materialized on the invoice (`reporting_amount` / `reporting_fx_rate` / `reporting_fx_locked_at`, migration 0025) — no silent recompute at today's rate
- [x] Realized/unrealized gain/loss tracking — payment-level realized (`compute_fx_gain_loss`, pre-existing) + open-position `compute_unrealized_fx_gain_loss` surfaced as `unrealized_fx` on `/analytics/cfo`
- [x] Currency displayed correctly per locale — frontend `<Money>` component + `formatMoney()` (`Intl.NumberFormat`, ISO-4217-code-driven) applied across invoices / payments / dashboard / analytics / portal; each amount renders with its own currency code, never a hardcoded `$`

---

### Multi-Entity
**Status:** Done (subsidiaries within one tenant DB — Phases 1–4). See `docs/multi-entity.md`.

- [x] Multiple entities (subsidiaries) within one organization — `Entity` model + nullable `entity_id` on business tables (`EntityMixin`), `X-Entity-ID` request scoping + sidebar switcher, per-tenant Default entity (Phases 1/2/2b)
- [x] Entity-level chart of accounts, GL codes, cost centers — `GLAccount.entity_id` NULL = shared ∪ entity-specific; wired into the AI extraction GL catalog + bulk-recode validation (per-invoice-entity), not just the list endpoint
- [x] Inter-company invoice routing — `POST /api/invoices/{id}/route-intercompany` generates the mirror payable under the counterparty entity (`counterparty_entity_id` / `intercompany_mirror_id`, migration 0051); idempotent, audited on both rows. See `backend/docs/inter-company.md`
- [x] Consolidated reporting across entities — `GET /api/analytics/by-entity` per-entity rollup + consolidated cross-check; "By entity" breakdown on the `/cfo` dashboard
- Also shipped: per-entity workflow selection — the engine picks the entity's active/default `WorkflowDefinition` (shared NULL fallback), one default per `(org, entity)` enforced by `uq_workflow_definitions_one_default` (migration 0050)

---

### Tax Compliance
**Status:** Done (US 1099 + international VAT/GST/withholding) — e-invoicing (Peppol/ZUGFeRD etc.) tracked separately under Priority 10. See `backend/docs/tax-1099.md` + `backend/docs/international-tax.md`.

1099 compliance is required for US AP operations. Bill.com, Tipalti, AvidXchange, Stampli, MineralTree all have it. VAT/e-invoicing is required for EU expansion (Medius and Basware lead).

**US Tax (Priority):**
- [x] W-9 collection — request, store, validate vendor W-9 forms (`POST /api/tax/vendors/{id}/w9`, vendor tax fields)
- [x] TIN validation — pluggable `tin_validation_adapters/` (offline `mock` default + Tax1099 TIN-match skeleton, local-first); `POST /api/tax/vendors/{id}/tin-verify` stamps `tin_verified_at`; format + checksum validation, raw TIN never logged
- [x] 1099 tracking — `build_1099_report` / `build_1099_dashboard` flag vendors over the $600 annual threshold
- [x] 1099-NEC and 1099-MISC generation — `services/tax_1099_forms.py` renders reportlab PDFs from payment data (`GET /api/tax/vendors/{id}/1099`); TIN masked in the PDF text layer
- [x] 1099 e-filing — pluggable `tax_filing_adapters/` (offline `mock` default + Tax1099 partner skeleton); `POST /api/tax/1099/file`, idempotent at two layers (DB unique constraint on `(org, idempotency_key)` in migration 0026 + deterministic adapter)
- [x] 1099 vendor dashboard — `GET /api/tax/1099-dashboard` (eligible vendors, YTD totals, W-9-on-file + TIN-verified status, threshold flags) + frontend `/tax` route

**International Tax:**
- [x] Tax rate lookup by jurisdiction — pluggable `tax_rate_adapters/` (offline `mock` default + Avalara / TaxJar skeletons, local-first), per-org override via `Organization.settings.tax.rate_provider`
- [x] VAT handling for international invoices — incl. EU reverse-charge (`services/international_tax/vat.py`)
- [x] Withholding tax calculation — by jurisdiction / vendor (`services/international_tax/withholding.py`)
- [x] GST handling (Australia, India, Canada) — `services/international_tax/gst.py`
- [x] Tax report generation — per-period collected-vs-owed report (`services/international_tax/report.py`, `/api/international-tax` router); figures persisted on `intl_tax_records` (migration 0027) as the audit fact
- [x] Country-specific tax rules engine — data-driven `services/international_tax/country_rules.py`; new countries are config, not code

**Competitors:** Tipalti (1099 + W-8BEN + VAT), Bill.com (1099 e-filing), Basware (global VAT, 60+ countries), Medius (EU e-invoicing mandates)

---

### Multi-Language UI (Internationalization / i18n)
**Status:** Done — **web runtime + full starter locale set shipped**: `frontend/src/lib/i18n/` (locale negotiation, typed `en` catalogue + the full `de/fr/es/pt-BR/ja` set as lazy chunks, lazy loader registry, reactive `m()`/`setLocale()`/`initLocale()`, ICU plurals, `<html lang/dir>`, locale picker with endonyms, `messages_parity` vitest) with the shell/nav + **dashboard** + **invoices list** + **payments** + **vendors** + **exceptions** + **notifications** + **contracts** + **recurring** + **organization settings** + **cfo/analytics** + **expenses** (incl. the **`ExpenseModal`** + **`PolicyModal`** dialogs) + the **procurement routes** (**requisitions** + **intake** + **catalogs** + **budgets** + **purchase-orders** + **goods-receipts**) + the **procurement create/edit modals** (`RequisitionModal` + `IntakeModal` + `CatalogModal`/`PunchoutModal` + `BudgetModal` + `ContractModal` + `RecurringModal`) + **positive-pay** + the **supplier portal** (every `routes/portal/**` page) + the **authentication & onboarding routes** (`login` + `login/mfa` + `login/{sso,saml}-callback` + `signup` + `change-password` + `verify`, `auth.*` namespace) + the **admin section** (the **Users & Roles** page + its `UsersPanel`/`RolesPanel`, **API Keys**, **Webhooks**, and **Partner / reseller admin**, `admin.*` namespace) extracted; `formatMoney` **and per-row dates** follow the active locale (the shared `utils/time.ts::formatDate` is locale-aware and drives every route/component's dates — **the date-localization slice is closed**: the 16 `.svelte` files that used to call `toLocaleDateString` inline now all route through `formatDate`). The mobile ARB track is complete at the screen level (every `mobile/lib/screens/*` uses `AppLocalizations`). **All web routes + their feature modals are now extracted** — including the workflows, audit, tax, discounts, credit-memos, vendor-statements (incl. the `VendorStatementReconModal` create/diff dialog), assistant, billing, and experiments routes; server-side email localization is **shipped**. See `frontend/CLAUDE.md` → i18n.

The data layer is already internationalized (multi-currency rollups, locale-aware `Intl` money/date formatting, country tax rules, e-invoicing) alongside the presentation layer, closing the loop for genuine international reach (EU mandates, LATAM, APAC, MENA). Basware/Medius ship 20+ UI languages; Tipalti and Bill.com localize the supplier-facing surfaces. Starter set: `en, de, fr, es, pt-BR, ja` (the six [`../project-running`](../../project-running) already ships), with the RTL switch-point in place for a later `ar`/`he`.

**Web (SvelteKit, `frontend/`):**
- [x] i18n runtime under `frontend/src/lib/i18n/` — client-side locale detection on first mount (stored choice → `navigator.languages` → English), reactive `m(key, params)` lookup, `<html lang/dir>` applied. **No `Accept-Language` SSR hook** — the frontend is adapter-static (GitHub Pages), so detection must be client-side
- [x] English statically bundled (fallback dict + prerender default); every other locale a dynamic `import()` chunk via a typed loader registry, so a single-locale visitor downloads only their strings — i18n adds ~nothing to the initial payload (`catalogues.ts`: `en` static, `de/fr/es/pt-BR/ja` lazy `import()`)
- [x] Compile-time + runtime parity: `Messages = typeof en` + `satisfies Messages` per locale (missing/extra key = type error); a `messages_parity` vitest validating every locale is loadable, complete, non-empty, and placeholder-faithful (covers all six locales via `SUPPORTED_LOCALES`)
- [x] ICU inline plurals (`{n, plural, one {…} other {…}}`) resolved via `Intl.PluralRules` for the active locale — not `fooOne`/`fooOther` key pairs (keeps web and mobile plural shapes identical) — e.g. the invoices `selected` count + showing-all string
- [x] Locale picker in settings/shell (endonyms — each language in its own script: English / Deutsch / Français / Español / Português (Brasil) / 日本語), choice persisted to `localStorage`
- [x] Active locale drives the existing `Intl.NumberFormat`/`Intl.DateTimeFormat` formatters (`<Money>` / `formatMoney()`) so numbers, currency, and dates all localize together
- [x] RTL switch-point (`dirForLocale`) wired to `<html dir>`; audit CSS for logical properties so an `ar`/`he` catalogue drops in with no further layout plumbing (switch-point present + unit-tested; no RTL catalogue ships yet)
- [x] Incremental string extraction — shell/nav first, then route-by-route; every route and feature modal is now extracted
- [x] Date-localization sweep — every remaining inline `toLocaleDateString` call site in the procurement / portal / admin lists now routes through the locale-aware `utils/time.ts::formatDate`

**Mobile (Flutter, `mobile/`):**
- [x] Standard Flutter `gen-l10n` + `intl` + `.arb` catalogues (idiomatic path — plural/placeholder/ICU + `AppLocalizations.of(context)`), committed (non-synthetic) output under `mobile/lib/l10n/gen/`, same six locales (en, de, fr, es, pt-BR, ja; a base `pt` fallback accompanies `pt_BR` as gen-l10n requires). Coverage: nav + dashboard + invoices list + settings + notifications + vendors + exceptions + payments history + approvals + capture + advanced-search + invoice detail/edit (+ sub-widgets) + payment queue/runs + login + MFA + admin users + org settings + workflows (the full screen set is now extracted). See `mobile/CLAUDE.md` → i18n
- [x] Per-device locale via `LocaleStore` (`stores/locale_store.dart`, secure-storage-persisted, never account-roamed) → `MaterialApp.locale` (a `ListenableBuilder` in `main.dart` re-localizes live); endonym picker + "System default" in the Settings screen
- [x] ARB key-parity test (`test/l10n/arb_parity_test.dart`) mirroring the web `messages_parity` — key-complete, non-empty, placeholder-faithful (set-based, so a 1-arm `ja` plural matches a 2-arm `en` one); plus a live locale-switch widget test (`test/l10n/locale_switch_test.dart`)

**Server-side (FastAPI, `backend/`):**
- [x] Localized outbound email — DB-synced `locale` preference on `User` (control plane) + `VendorUser` (tenant-scoped), migration `0059` (existence-guarded, runs on both control + every tenant DB; nullable → English fallback). Consumed by a per-locale email catalogue (`app/services/email_adapters/email_catalogue.py`, same six locales as web/mobile) with English fallback per key. Covers the `email_adapters` surfaces: signup/welcome (locale captured at `/signup/start`, stashed in the `EmailVerification.meta`, reused by the welcome email), invoice notifications (employee via `notification_dispatch` + supplier via `vendor_notifications`, rendered per recipient in their locale), supplier-chat portal-link email (catalogue-routed, English default — no per-user locale at that surface). Set via `PATCH /api/auth/me` (employee) + `PATCH /api/portal/auth/me` (vendor), each validating against the supported set (422 on unknown). **Deferred:** the frontend language-picker → backend write (owned by the frontend track) — the backend endpoint + persistence are in place. See `backend/docs/notifications.md` § Localized email
- [x] Email catalogue parity test (`tests/test_email_catalogue.py` — every locale resolves every key, no empty strings, placeholder-faithful vs English); deep links + brand chrome stay locale-independent — only copy changes (placeholders carry the data unchanged across locales)
- [x] DB `locale` pref kept **separate** from the per-device UI locale — it means "what language to email this person in" (account-level), written from the UI and read by the email-render path ONLY; never returned to drive in-app UI (documented in `backend/docs/notifications.md` § Localized email)

**Pointers from `../project-running`** (it shipped exactly this — three translation surfaces kept in lockstep by parity tests, no shared source because TS/Dart/Python can't import one catalogue):
- Web runtime to model on: `apps/web/src/lib/i18n/` — `locale.ts` (pure negotiation: `SUPPORTED_LOCALES`, `negotiateLocale`, `dirForLocale`, `parseAcceptLanguage`), `messages.ts` (`Messages = typeof en`), `catalogues.ts` (typed lazy-loader registry), `store.svelte.ts` (reactive `m()` + `setLocale` + `initLocale`), `interpolate.ts` (ICU plural + `{placeholder}` substitution), `messages_parity.test.ts`, and `locales/*.ts`
- Decision records spelling out the *why* and the traps to avoid: `docs/architecture/decisions.md` §108 (web client-side + lazy catalogue), §113 (mobile gen-l10n/ARB + per-device locale), §120 (server-side email localization from a DB-synced pref — the one place locale leaves the device)
- Reuse the design wholesale; the only AP-specific delta is that **two** identities email-localize (internal `User` and supplier-portal `VendorUser`) and the email catalogue lives in Python (`backend/app/services/email_adapters/`), not Go

**Competitors:** Basware / Medius (20+ UI languages, EU-mandate-driven), Tipalti & Bill.com (localized supplier portals), SAP Ariba / Coupa (full enterprise localization)

---

## Priority 6: Supplier Portal
**Competitive gap: all competitors have a supplier portal**

### Vendor Self-Service
**Status:** Complete — Phase 3 shipped (W-9/W-8 upload, vendor notification preferences, virtual-card reveal, early-payment discount offers, in-app supplier chat, portal MFA) on top of Phase 2 self-service (PO flip, remittance download, approval-gated company/bank/tax self-update) and the Phase 1 MVP (separate auth, invoice submission, status/payment tracking). The MFA email-OTP backup factor — the last deferral — shipped too (`POST /api/portal/auth/mfa/challenge/email`, Redis-only, no migration). See [`backend/docs/supplier-portal.md`](../backend/docs/supplier-portal.md).

Separate portal for vendors to interact with the AP system. Biggest workflow gap — forces email/manual invoice intake without this. Every competitor (Coupa CSP, Tipalti Supplier Hub, Basware Network, Stampli) offers this.

- [x] Vendor login (separate auth, linked to vendor record) — `VendorUser` tenant-scoped, JWT `typ=vendor` prevents cross-contamination with AP-app tokens
- [x] Submit invoices directly (upload PDF) — routes into the existing extraction pipeline with `vendor_id` pre-filled and a `source=supplier_portal` audit breadcrumb
- [x] Check invoice status and payment status
- [x] View payment history — joins `payments` ↔ `invoices` on `vendor_id`
- [x] Admin invite flow — `POST /api/vendors/{id}/portal-users` mints a temp password + welcome email
- [x] PO flip — create invoice from PO (pre-populate fields) — `POST /api/portal/purchase-orders/{id}/flip` seeds an invoice from a vendor-owned PO into the existing extraction/workflow path; idempotent per `(vendor, po)`
- [x] Download remittances (PDF generation) — `GET /api/portal/payments/{id}/remittance` reuses `services/remittance_pdf.py`, ownership-joined on `Invoice.vendor_id`
- [x] Update company info, bank details, tax ID — `GET/PATCH /api/portal/company` (contact fields apply live, masked bank/tax) + `POST /api/portal/company/{bank-change,tax-id-change}` staging
- [x] Bank detail change requires AP admin approval (fraud prevention) — bank/tax changes stage a `VendorChangeRequest`; the vendor row is untouched until an admin approves via `POST /api/vendors/change-requests/{id}/approve`
- [x] W-9/W-8 form upload and management — `GET/POST /api/portal/company/tax-form` (+ `/file`); vendor uploads their own signed form live onto `Vendor.w9_file_key`/`w9_received_date` (no migration), vendor-scoped + cross-tenant-gated, PII-free audit
- [x] Notification preferences (email on payment, on rejection) — `GET/PATCH /api/portal/notification-preferences`; per-portal-user, vendor-controlled, wired into the `transition_invoice` dispatch chokepoint (migration 0052)
- [x] Virtual card detail viewing (secure, one-time access) — `GET /api/portal/cards/{token}` consumes a single-use `CardRevealToken`
- [x] Early payment discount offers (tie into dynamic discounting) — `GET /api/portal/discount-offers`, `POST .../{id}/accept`|`/decline`; reuses the dynamic-discounting engine, accept flips status only (never moves money), idempotent
- [x] In-app per-invoice chat between vendor and AP team — `GET/POST /api/portal/invoices/{id}/chat` (+ attachments, file proxy); vendor-scoped, AP author ids masked
- [x] MFA for portal users — TOTP via `POST /api/portal/auth/mfa/{enroll,verify,disable,challenge}`; opt-in per vendor user, `FEOH_MFA_ENABLED`-gated, distinct `typ=vendor_mfa_challenge` (migration 0053). **Email-OTP backup factor now shipped** — `POST /api/portal/auth/mfa/challenge/email` issues a 6-digit code via the pluggable email adapter (console in dev), verified through `POST .../challenge` with `method=email`; reuses the employee Redis-OTP primitives under a distinct `mfa:vendor_email_otp:` keyspace (no migration); "use email code instead" affordance on the portal login MFA step

**Files:** `backend/app/api/portal.py`, `backend/app/api/portal_auth.py`, `backend/app/models/vendor_user.py`, `frontend/src/routes/portal/`

**Competitors:** Coupa (CSP, free for suppliers), Tipalti (full supplier hub), Basware (Basware Network), Stampli (invoice submission + status)

---

## Priority 7: Authentication & Enterprise Security
**Competitive gap: SSO is an enterprise deal-blocker**

### SSO / Enterprise Authentication
**Status:** Done — OIDC + SAML 2.0 + SCIM (/Users + /Groups) all shipped

No SSO = no enterprise sale. OIDC (Okta + Entra), SAML 2.0, and SCIM 2.0 user provisioning are live. See [`docs/authentication.md`](authentication.md) § SSO and § SCIM for the full design, and [`docs/local-sso-saml.md`](local-sso-saml.md) for local SAML testing via Keycloak.

- [x] OIDC (OpenID Connect) support — single flow covers Okta + Entra via per-tenant discovery URL
- [x] JIT (Just-In-Time) user provisioning from SSO — match by `(provider, sub)` then `(org, email)`, otherwise create
- [x] SCIM 2.0 `/Users` provisioning (create / list / get / PATCH / soft-delete) with per-tenant bearer token
- [x] Force password change on first login (non-SSO users) — `User.must_change_password` flag, cleared on `/api/auth/change-password`
- [x] SAML 2.0 SSO (Okta, Azure AD, OneLogin, ADFS) — SP-initiated, separate code path (`api/auth_saml.py`) reusing the OIDC JIT + session-mint tail. python3-saml verification pinned to the per-tenant IdP cert; hardened (wantAssertionsSigned, SHA-256-only, issuer/audience/destination/InResponseTo enforced, per-tenant replay dedup, XXE-hardened parsing). Local IdP via Keycloak (`pnpm saml:seed`).
- [x] SCIM `/Groups` — IdP groups → RBAC roles. Group state JSONB on `settings.sso.scim_groups`; `scim_group_role_map` (`{displayName: role}`) drives idempotent role reconciliation (only mapped roles are added/removed; manual/JIT assignments untouched). Full list/get/create/PUT/PATCH/delete. `services/scim_groups.py`
- [x] SSO-only mode — `settings.sso.sso_only` (covers OIDC + SAML) closes password login org-wide: `/api/auth/login` 403s with an `sso_only` audit reason, and the login page hides the password form. `sso_only` is echoed on the public `/config` endpoints only when the IdP config resolves, so a broken config can't lock everyone out. `services.sso.is_sso_only`
- [x] MFA — TOTP enrollment + email-OTP backup, opt-in per user with org-level enforcement toggle (`FEOH_MFA_ENABLED` master switch; default off in dev)
- [x] MFA — WebAuthn / passkeys (separate code path from TOTP; `services/webauthn.py` + `py_webauthn`, control-plane `WebAuthnCredential` table migration 0063, register/list/delete + authenticate endpoints under `/api/auth/mfa/passkey/*`, login challenge offers `passkey`, profile + MFA-login UI, RP ID/origin configurable for localhost dev). See `docs/authentication.md` § Passkeys.
- [x] MFA — mobile app support — Flutter login now detects an MFA **challenge** response (vs a direct `TokenResponse`) and routes to a code-entry `MfaScreen` (TOTP + email-OTP backup factor switcher, org-enforcement banner), submitting to `/api/auth/mfa/verify` → JWT → secure storage. No backend change (reuses the web flow). TOTP enrollment + passkeys stay web-only. `mobile/lib/screens/mfa_screen.dart` + `auth_store.dart`
- [x] Session management — per-user concurrent session cap + forced logout on role change / deactivation (see SOC 2 Readiness below)

**Competitors:** All competitors support SSO. Coupa, SAP Concur, and Basware also support SCIM.

---

## Priority 8: Mobile & Notifications
**Competitive gap: most competitors have mobile apps**

### Email & Notification System
**Status:** Done

- [x] Email notifications on key events (invoice assigned, approved, rejected, paid) — centralized `transition_invoice` hook + explicit `assign_reviewer` hook → `notification_dispatch.notify_event`, sent via the existing pluggable email adapter (`console`/`smtp`/`ses`). See `backend/docs/notifications.md`.
- [x] Configurable notification preferences per user — `users.notification_prefs` JSONB, per-event in-app/email toggles in `/profile`, gating both channels.
- [x] In-app notification center — tenant `notifications` table, `/api/notifications*`, `/notifications` route + sidebar unread badge.
- [x] Email-to-invoice — forward invoices to a dedicated email address for auto-import (Bill.com, Tipalti, Stampli, Medius have this) — `/api/email-intake` inbound webhook turns attachments at the per-tenant `invoices+<token>@<domain>` address into invoices; provider-signed (SES/Mailgun/generic adapters, HMAC-verified) → extraction pipeline. See `backend/docs/email-intake.md`.
- [x] Slack/Teams integration for approval notifications (Stampli, Airbase differentiate here) — pluggable `chat_notification_adapters/` (mock default + slack + teams, per-org config) wired best-effort into `notify_event` on the approval events; fails closed without a webhook URL, PII-free, no migration. Redelivery UI / dead-letter deferred to the outbound-webhook track. See backend/docs/notifications.md
- [x] Mobile parity — `NotificationsScreen` + `NotificationStore` (`mobile/`) over the existing `GET /api/notifications` (+ `unread-count` / `{id}/read` / `read-all`); reached from a `NotificationBell` app-bar action (live unread `Badge`) in the Dashboard app bar (all roles). All/Unread filter, optimistic tap-to-mark-read with deep-link to invoice detail, mark-all-read, offline-cached list + empty/loading/error states. No backend change (endpoints already existed). See `mobile/CLAUDE.md`.

---

## Priority 9: AI-Powered Automation (strong differentiators)

### AI Agents for Autonomous Exception Handling
**Status:** Done — resolvers + dashboard shipped (amount-mismatch, missing-PO, GL-coding; agent dashboard UI). Additional resolver types are new scope, not unfinished work.

AI agents that autonomously resolve common exceptions without human intervention — mismatched amounts, missing PO references, GL coding errors. See `backend/docs/exception-agents.md`.

- [x] Agent framework — registry + coordinator + autonomy thresholds (`services/exception_agents/`)
- [x] Auto-resolve: small amount mismatches within tolerance (`amount_mismatch_v1`)
- [x] Auto-resolve: missing PO — match by vendor + amount + date range — `missing_po_v1` resolver: finds the real PO by vendor (id/fuzzy ≥0.8) + amount (per-vendor/commodity tolerance) + date window, links by `po_number`, approves via `review` (never adjusts the amount); a registered `po_mismatch` **dispatcher** routes to `amount_mismatch_v1` (status `matched`) vs `missing_po_v1` (status `no_po`). Confidence-gated on autonomy; ambiguous/none → escalate; idempotent. **Multi-PO split matching shipped** — `multi_po_split_v1` (third dispatcher delegate): when no single PO matches but a **unique** set of the vendor's open POs sums (within the resolved tolerance) to the invoice total, links the whole set (combined `po_number` ref + multi-PO `po_match` snapshot) and approves via `review` — **never adjusts the amount**. Bounded combinatorial search (≤12 candidate POs, set size ≤4; an over-cap pool escalates, no silent truncation); defers to `missing_po_v1` when a single PO matches; ambiguous (>1 set) / none → escalate; confidence one band below single-PO (0.90 dated / 0.80 undated); idempotent. Line-level (per-line) split matching still deferred
- [x] Auto-resolve: GL coding errors — correct based on historical patterns — `gl_coding_v1` resolver under a `missing_data` **dispatcher**: derives the vendor's dominant GL (and an empty cost center) from approved history via the pure `vendor_enrichment.suggest_fields` primitive (no stats reimplemented), fills or corrects the GL through the audited `review.approve_invoice(corrections=…)` path (never moves money), confidence-banded (0.92 strong / 0.80 majority) and gated on the org autonomy threshold; ambiguous / other-missing-field / already-correct → escalate; CFO-gate honoured; idempotent (re-derives under the row lock). See `backend/docs/exception-agents.md`
- [x] Escalation rules — sub-threshold confidence routes to human (`escalated`)
- [x] Agent decision log — `AgentDecision` table + `/api/exceptions/agent-decisions`
- [x] Dashboard: agent resolution rate, accuracy, escalation rate — `/exceptions` → **AI Agents** tab (`AgentDashboard.svelte`) over `/agent-stats` + `/agent-decisions`: KPI row (decisions / resolution rate / escalation rate / auto-resolved / escalated) + recent-decision log with an action filter. Accuracy is shown as an explicit "Not yet measured" placeholder (a human-overturn signal is needed before a real figure — never fabricated)
- [x] Configurable autonomy level per org (conservative → aggressive)

---

### Adaptive AI Workflows
**Status:** Done — read model + baseline anomaly read + advisory suggestions shipped, plus the feedback loop (realised auto-approval overturn rate folded back into the threshold recommendation) and overturn-weighted smart routing.

Workflows that learn from team behavior and adapt over time — routing, approval timing, exception handling. The first slice shipped the **read** surfaces (learning, on-demand anomaly, advisory suggestions); the **act** surfaces are now shipped too — smart routing apply, auto-adjust thresholds, and A/B testing of workflow rules — leaving only the model-retraining feedback loop as a follow-up. All learning + metrics is deterministic statistics over existing tenant data — no LLM, runs with no cloud key.

- [x] Adaptive approval-pattern learning (read model) — per-approver + per-vendor approval stats (counts, approval/consistency rates, time-to-approve). `services/adaptive_workflows.py`, `GET /api/adaptive/approval-patterns`.
- [x] Baseline anomaly detection (on-demand, explainable) — `GET /api/adaptive/anomalies`; flags amount / approver / timing deviation and **returns the per-vendor baseline it compared against**. Read-only — distinct from (and does not duplicate) the per-invoice `fraud_stat_anomaly` warning, which writes warnings + Exceptions.
- [x] Advisory workflow-change suggestions — "consider auto-approve under $X" auto-approve-threshold suggestions persisted in `workflow_suggestions` (migration 0031) with `open/dismissed/applied/stale`; advisory only — nothing is auto-applied.
- [x] Smart routing — **recommend** the fastest/most-appropriate approver for an invoice — advisory, read-only `GET /api/adaptive/routing-suggestion?invoice_id=` ranks the org's eligible approvers (admin/ap_manager/cfo) by a deterministic score (speed + consistency + vendor familiarity + experience) from their approval history; `recommend_approvers` in `services/adaptive_workflows.py`. The advisory GET assigns nobody; the **apply** path is now shipped — `POST /api/adaptive/routing-suggestion/apply` (admin/ap_manager) assigns the top recommendation through the audited `review.assign_reviewer` (audit row + notification + OOO delegation; never a raw `assigned_to_id` write), 409 if not `ready_for_review`, 422 if no eligible approver, idempotent no-op when already assigned.
- [x] Auto-adjust thresholds — raise auto-approve limit as accuracy improves. `GET /api/adaptive/threshold-recommendation` (admin/ap_manager/cfo) recommends a **conservative** raise to the org-wide `auto_approve_below` from clean-history vendor evidence (`recommend_auto_approve_threshold` in `services/adaptive_workflows.py`: ≥3 qualifying vendors with zero rejections/corrections, never lowers, capped at 2×/$25k per step). `POST /api/adaptive/threshold-recommendation/apply` (**admin only** — matches who can edit workflow definitions) writes it **through the audited workflow-definition PATCH path** (reuses `_snapshot_version` → a `WorkflowVersion` snapshot + `workflow.version_snapshot` + `workflow.auto_approve_threshold_raised` audit rows; never a raw `steps_config` mutation), idempotent no-op when the evidence doesn't raise. Affects only NEW invoices (frozen workflow snapshots). See `backend/docs/adaptive-workflows.md` § Auto-approve threshold.
- [x] A/B testing for workflow rules — run a controlled experiment comparing two workflow-rule configs (an **A** control vs a **B** variant) on the same workflow definition and measure which performs better on objective, deterministic metrics (median time-to-approval, touchless rate, exception rate, rejection rate). `WorkflowExperiment` model + migration 0064 (tenant-scoped, fans out). Deterministic assignment at invoice creation (stable hash of invoice id + experiment id, honouring `split_a_pct`) freezes the chosen variant's config onto the invoice's workflow-instance snapshot (respects the per-invoice frozen-snapshot invariant) and records the assignment durably + a PII-free `invoice.experiment_assigned` audit row — hooked into `workflow_engine.create_workflow_instance` (best-effort; never breaks invoice creation). `GET /api/experiments/{id}/results` computes per-variant metrics over the recorded assignments with a clear "not enough data yet" state and a winner call only past `min_sample_per_variant` completed invoices (no statistical-significance claim — deterministic direction check on the configured primary metric, no LLM). API `/api/experiments` — CRUD + start/stop/conclude/results; read managers/CFO, mutate admin-only, every mutation audited. Frontend `/experiments` surface (list + status chips + create modal + live results readout). `services/workflow_experiments.py` (pure) + `services/workflow_experiments_runtime.py` (assignment hook) + `api/workflow_experiments.py`. See `backend/docs/adaptive-workflows.md` § A/B testing.
- [x] Feedback loop — human OUTCOMES feed back into the deterministic recommendations so they self-correct (there is no trainable model — the feature is pure stats). Reads `audit_log` (auto-approvals later voided / rejected / corrected) as the overturn signal: `outcome_adjusted_threshold` pulls the auto-approve-threshold recommendation BACK in bands (passthrough <5% → no-raise 5–15% → freeze ≥15%; never lowers, never reacts on thin evidence). `compute_effectiveness` replaces the "Not yet measured" placeholder with `auto_approval_overturn_rate` + `recommendation_acceptance_rate`, each with an explicit `insufficient_data` state (no fabricated figures). `GET /api/adaptive/feedback` (admin/ap_manager/cfo, access-audited) returns the tallies + both base and outcome-adjusted recommendation. Compute-on-read, no migration, no LLM. **Routing down-weighting now shipped too**: `recommend_approvers` subtracts a bounded penalty (≤30 pts, banded, floored at the 5-decision min-sample) for an approver's own overturn rate (their `invoice.approved` decisions later voided / rejected / corrected *by a different actor*), surfaced on `GET /api/adaptive/routing-suggestion` as `base_score`/`outcome_penalty`/`overturn_rate_pct` + a `reasons` entry (explainable); the shared `is_overturned` helper is the single overturn classifier for both the threshold and routing sides. *A genuinely trainable model is separately scoped.*

**Files:** `backend/app/services/adaptive_workflows.py`, `backend/app/api/adaptive_workflows.py`, `backend/app/schemas/adaptive_workflows.py`, `backend/app/models/adaptive_suggestion.py`, `backend/alembic/versions/0031_workflow_suggestions.py`, `backend/docs/adaptive-workflows.md`, `backend/tests/test_adaptive_workflows.py`. A/B testing adds `backend/app/services/workflow_experiments.py`, `backend/app/services/workflow_experiments_runtime.py`, `backend/app/api/workflow_experiments.py`, `backend/app/schemas/workflow_experiments.py`, `backend/app/models/workflow_experiment.py`, `backend/alembic/versions/0064_workflow_experiments.py`, `backend/tests/test_workflow_experiments.py`, `frontend/src/routes/experiments/+page.svelte` + `frontend/src/lib/api/experiments.ts` + `frontend/src/lib/types/experiments.ts`.

---

### Intelligent Data Enrichment from Supplier History
**Status:** Done

Auto-populate and validate invoice fields using historical data from the same supplier.

- [x] Auto-fill GL account, cost center, payment terms from vendor history — GL/cost-center/terms suggested from the vendor's approved-invoice history (dominant value + confidence + evidence); suggestion-only, never overwrites. `GET /api/enrichment/invoices/{id}/suggestions`. See backend/docs/data-enrichment.md.
- [x] Flag deviations — "This vendor usually invoices ~$5K, this one is $50K" — already shipped via `adaptive_workflows.detect_invoice_anomaly` (`GET /api/adaptive/anomalies`); not duplicated here.
- [x] Vendor performance scoring — on-time delivery, invoice accuracy, dispute rate — all three sub-scores shipped (`GET /api/enrichment/vendors/{id}/score`). On-time delivery now real via `PurchaseOrder.expected_delivery_date` (migration 0060, tenant fan-out): `received_date <= expected_delivery_date`, folded into the composite at weight 0.3, N/A when no PO has both an expected date and a receipt. The expected date now **auto-populates from ERP PO sync** (`POST /api/purchase-orders/sync-erp`): the unified `PoPayload` carries `expected_delivery_date`, the mock catalogue emits deterministic dates (one PO deliberately without, to exercise the leave-None branch), the `merge_dev` adapter maps it from the upstream `delivery_date`/`expected_delivery_date`/`requested_delivery_date` field, and the sync mapper sets it on create + back-fills it onto an existing PO **without clobbering** a human-set value (a None payload never erases one). POs are ERP/manual-only (the AI extraction pipeline creates invoices, not POs), so there's no extraction leg to wire.
- [x] Vendor consolidation — identify duplicate/similar vendors (suggest) AND merge them (execute). `GET /api/enrichment/vendors/consolidation-suggestions` clusters by tax_id / code / fuzzy name (union-find, blocking-bounded), deterministic canonical pick, tax_id masked (advisory). `POST /api/enrichment/vendors/consolidation/merge` (`vendor.manage`) executes the merge — reassigns every `vendor_id` FK across all tenant child tables to the canonical vendor, soft-retires the duplicates (`status=inactive`, never hard-deleted), row-locked, idempotent, PII-free `vendor.merged` audit row; refuses self-merge / cross-entity / unknown vendor. The **"Merge into canonical" UI now ships** on `/vendors` — a `vendor.manage`-gated **Merge duplicates** modal (`VendorConsolidationModal`) renders each cluster's canonical-vs-duplicate diff, two-step-confirms the irreversible-ish fold, surfaces the backend's 4xx refusals, and refreshes the list (e2e: `vendors/consolidation-merge.spec.ts`). See backend/docs/data-enrichment.md
- [x] Enrich vendor data from external sources (D&B, Clearbit) — pluggable `enrichment_adapters/` family (`mock` local-first default + fail-closed `dun_bradstreet`/`clearbit` skeletons); `POST /api/enrichment/vendors/{id}/enrich` returns firmographics + an advisory per-field suggestion diff (PII-safe — `tax_id` is an input match-key only, never echoed). The **apply** path is now shipped too: `POST /api/enrichment/vendors/{id}/apply` writes a steward-selected set (`name`/`address`/`website`) onto the Vendor through an audited (`vendor.updated`), idempotent, non-destructive path; `Vendor.website` added in migration 0061 (tenant fan-out); `tax_id` excluded (goes through the bank/tax change-request gate). The **Apply action now ships in the vendor-detail UI** — the `VendorModal` has an "Enrich from external source" action (admin/ap_manager/cfo) that renders the per-field suggestion diff (current → suggested) with per-field checkboxes; "Apply selected" calls `POST .../apply` with the chosen subset and refreshes the row (`tax_id` is never applyable here). Remaining: live D&B/Clearbit keys. See backend/docs/data-enrichment.md § External enrichment.
- [x] Price variance detection — same item, different price across invoices — per-vendor line-item median baseline + tolerance; returned inline on the suggestions endpoint with baseline+delta. **Now also persisted** at the `invoice_warnings.refresh_warnings` write chokepoint: a deviating line writes an `Invoice.warnings` entry + a de-duped `price_variance` `Exception` (gated by `settings.fraud_rules.price_variance_enabled`, default on; reuses the pure `detect_price_variance`, no math duplication; idempotent via `_ensure_exception`).

---

### Conversational AP Assistant
**Status:** Done — fixed read-only toolset, SSE streaming, conversation history, token-usage meter; mock/claude/ollama adapters. **Differentiator for CFO / AP Manager persona**

Chat over the tenant's data. Replaces ad-hoc SQL and spreadsheet exports for common operational questions. Backend `/api/assistant/*`; see `backend/docs/conversational-assistant.md`.

- [x] Tool-calling assistant with a fixed toolset: `list_invoices(filters)`, `get_vendor_spend(period)`, `list_pending_approvals(assignee)`, `get_payment_forecast(horizon)`, `find_invoices_by_text(query)` — no raw SQL exposure, each tool is a typed endpoint over the current tenant. Local-first: deterministic `mock` adapter default, `claude` adapter (Anthropic tool-use) when keyed.
- [x] Tenant-scoped context — conversation history per `(tenant, user)`, org-level cap on tokens/cost.
- [x] Streaming responses via server-sent events — `POST /api/assistant/chat/stream` emits `tool`/`delta`/`done`/`error` SSE events (budget refusal stays a real HTTP 429 before the stream; rows + token debit commit together inside the generator). **Per-token claude SSE passthrough shipped**: the `claude` adapter's `respond_streaming` runs the tool-use loop with `stream: true` and forwards the Anthropic `text_delta`s as `delta` frames token-by-token (token accounting reads `message_start`/`message_delta` usage, summed per hop; fails soft mid-stream with no double-charge); `mock`/`ollama` keep deterministic word-chunking via the base `respond_streaming` (and `claude` still downgrades to `mock` with no key). See `backend/docs/conversational-assistant.md` § Streaming. Charts rendered from structured tool output: the `/assistant` page (`api.ts::streamAssistantChat` → `fetch` + body-reader, not `EventSource`) renders each `tool` frame's `result` as a bar chart (`SpendBarChart`) or table as it arrives, falling back to non-streaming `POST /api/assistant/chat` when the stream endpoint is unavailable.
- [x] Example prompts built into the empty state: *"which approvals have I been sitting on > 5 days?"*, *"which vendors are we paying the most this quarter?"*, *"show me invoices with PO mismatches over $10k"*. Shipped on the `/assistant` empty state (`ExamplePrompts`) — clicking one fills + sends it.
- [x] Cost controls — token budget per org per month with a usage meter (`/api/assistant/usage`); graceful 429 refusal on exceed. UI surfaced: the `/assistant` page shows a `UsageMeter` (used/budget tokens, amber/red as it nears/exceeds budget; budget `0` = unlimited), refreshed after each turn, and renders the friendly "monthly AI budget reached" notice on a 429.
- [x] Audit trail — every tool call logs a PII-safe `assistant.tool_invoked` row via the append-only audit infra.

Highest-leverage "sticky feature" work once the product has real usage. Cold-start is fine because it's retrieval over existing data, not learned patterns.

---

### Audit Log Summarization
**Status:** Done

One-paragraph natural-language summary at the top of the invoice modal, generated from the audit log + extraction metadata. Dramatically improves the "catching up on an invoice" UX — reviewers don't have to parse a 15-row timeline.

- [x] Cached summary field on `invoices.meta` (regenerate when the audit-log fingerprint changes — derived from `audit_log`, so it works in both `local` and `lambda` audit modes with no audit-write-path changes)
- [x] LLM call invoked lazily on first open after audit log changes (`services/audit_summary.py`, `GET /api/invoices/{id}/summary`)
- [x] Handles all status transitions, corrections, exception resolutions, ERP sync events
- [x] Shows confidence context: *"auto-extracted at 95% confidence with RAG priors applied"*
- [x] Small feature but high-visibility — pairs well with the invoice-list priors chips

Local-first: with no Anthropic key (committed `.env.development` default) the service returns a deterministic template summary — no network call. See `backend/docs/audit-summary.md`. Mobile is excluded (the audit timeline it builds on is not yet on mobile — Priority 8 parity item).

---

## Priority 10: Compliance & E-Invoicing

### SOX-Compliant Audit Trails
**Status:** Done — immutable log + access auditing + field history + auditor export (JSON/CSV/**PDF**) + periodic access reviews + retention policies + digital signatures on approvals all shipped. Live government-clearance-style integrations (e.g. external WORM SLAs) compose with the existing audit-shipping infra; nothing in this slice is deferred.

Enhance the existing audit trail to meet SOX (Sarbanes-Oxley) compliance requirements.

- [x] Immutable audit log — DB-level `BEFORE` triggers on `audit_log` reject every DELETE and every UPDATE that touches a column other than `shipped_at` (the shipper's carve-out). Survives a rogue ORM call or direct `psql`. See `app/services/audit_immutability.py` + migration `0022_sox_audit_immutable`; installed on every tenant DB (migration fan-out + `tenant_provisioning`).
- [x] Segregation of duties enforcement — default-on in the approval step; see `app/services/approval_chain.py::check_segregation`
- [x] Access control audit — log who viewed what, not just who changed what. Sensitive reads (vendor detail, payment detail, card PAN, audit-trail view, every export) write a `<entity>.viewed` row via `app/services/audit_access.py::log_access`, recording field-NAMES not values (PII-out-of-logs).
- [x] Periodic access reviews — flag users with unused elevated permissions. Compute-on-read (no migration): `services/access_review.py` derives each elevated-role user's (`admin`/`ap_manager`/`cfo`) last *mutating* audit action; flagged DORMANT past `FEOH_ACCESS_REVIEW_DORMANT_DAYS` (default 90) or if they never acted. `GET /api/access-reviews` (audited read) + `POST /api/access-reviews/acknowledge` (review-workflow closure: `access_review.completed` audit row + `Organization.settings.access_review` stamp), admin/CFO. See `backend/docs/access-reviews.md`.
- [x] Retention policies — configurable retention periods, archival. Per-class windows on `Organization.settings.retention` (`GET`/`PUT /api/retention-policy`, admin, audited). `services/retention_sweep.py` is a master-switched (`FEOH_RETENTION_ENABLED`, default off) per-tenant sweep that soft-archives overdue terminal invoices and verifies audit-log WORM shipment via a privileged, **audited** path (`retention.archived`) — it never raw-DELETEs and composes with the immutability trigger (audit rows are never deleted). See `backend/docs/retention.md`.
- [x] Audit report generation — formatted for external auditors. `GET /api/audit/export?format=pdf` returns a SOX audit-trail PDF (cover + event-count summary + chronological table) via the pure `services/audit_report_pdf.py` (reportlab), reusing the existing entry-load + `audit.exported` audit row. Renders only the field-NAME-sanitised entries (no PII). See `backend/docs/api-reference.md` § Audit Trail.
- [x] Digital signatures on approvals (timestamp + user hash) — HMAC-SHA256 over the canonical approval payload (invoice id + exact `Decimal` amount + actor + decision + timestamp), keyed by `FEOH_APPROVAL_SIGNING_KEY` (sops; no hardcoded fallback; no-op when unset). The digest lands in the immutable `invoice.approved` audit row's `details.signature`; re-verifiable at `GET /api/audit/invoice/{id}/verify-signatures` (admin/CFO) which recomputes each approval's digest and reports valid/invalid (tamper-evident non-repudiation). See `backend/docs/approval-signatures.md`.
- [x] Change history on every field — before/after values. Invoice edits + approve-with-corrections write `details.changes = {field: {old, new}}` (money serialised as string-Decimal, never float) via `audit_access.build_field_diff`; rendered in the invoice-modal Activity timeline.
- [x] Export audit trail per invoice or date range for auditor review — `GET /api/audit/export` (JSON/CSV, admin/CFO) + the `/audit` auditor console. Every export is itself audited (`audit.exported`).

---

### Data Privacy & Residency (GDPR / CCPA)
**Status:** Complete

Selling internationally means handling vendor + employee PII and banking data under GDPR (EU/UK), CCPA/CPRA (California), and similar regimes. The app stores this across tenant DBs today; this track adds the data-subject-request path, retention policy, residency story, and the consent + processing-record paperwork that EU/enterprise procurement reviews demand. Pairs with the [Multi-Language UI](#multi-language-ui-internationalization--i18n) work as the "go international" track.

- [x] DSAR export — assemble everything held about a data subject (a `VendorUser`, vendor contact, or `User`) into a portable JSON bundle. New `/api/privacy` router (`POST /privacy/dsar`), admin-only, the request audited PII-free (`privacy.dsar_export`) + recorded in `data_subject_requests` (migration 0054). Subject resolution spans the control plane (`User`) + tenant DB (`VendorUser`, `Vendor`); cross-tenant identifiers 404. See `backend/docs/privacy.md`
- [x] Right-to-erasure / anonymization — `POST /privacy/erasure` irreversibly redacts a subject's PII (email/name/tax_id/bank details/contact + supplier-authored chat bodies) while PRESERVING the **immutable financial + audit record** — no money field is touched, and the append-only `audit_log` is never mutated (a new `privacy.erasure` row is written instead, respecting the 0022 immutability trigger). Idempotent. Cross-DB commit ordered tenant-audit-first so a failure is recoverable. See `backend/docs/privacy.md`
- [x] Configurable data-retention policies — per-record-class windows on `Organization.settings.retention` (`GET/PUT /api/retention-policy`, admin) + the `retention_sweep` background loop that soft-archives overdue terminal invoices and, for the WORM `audit_log` class, verifies shipment instead of deleting (never deletes audit rows — composes with the immutability trigger). Disabled by default (`FEOH_RETENTION_ENABLED`). See `backend/docs/retention.md`
- [x] Data residency — per-tenant region pin (`us`/`eu`/`uk`/`ca`/`au`) on `Organization.settings.residency.region` via `GET/PUT /api/organization/data-residency` (admin mutate, audited `organization.residency_updated`); `services/data_residency.py` documents the intended per-region DB + object-storage placement (the database-per-tenant architecture makes per-region pinning tractable) and ships an advisory deploy-region alignment check. Settings-JSON, no migration; documents the model ahead of multi-region infra. See `docs/data-residency.md`
- [x] Consent + processing records — reusable `ConsentBanner.svelte` (Svelte 5 runes, accessible, localStorage-persisted, governs non-essential storage only) mounted in the root layout so it covers the app + supplier portal + signup/marketing surfaces; a Record of Processing Activities doc (`docs/ropa.md`, GDPR Art. 30); and a DPA template (`docs/founder-runbooks/dpa-template.md`, Art. 28, counsel-review-flagged)
- [x] Sub-processor register (`docs/sub-processors.md` — every adapter-backed processor with data shared + "active when configured", leading with the local-first default that activates none) + breach-notification runbook (`docs/founder-runbooks/breach-notification.md`, the 72-hour GDPR clock, Art. 33/34)

**Competitors:** every EU-serving competitor (Basware, Medius, SAP Ariba, Coupa) has GDPR DSAR + residency; it's table stakes for enterprise procurement reviews

---

## Priority 11: Dynamic Payments & Matching

### Dynamic Discounting & Early Payment Optimization
**Status:** Shipped (first slice)

Go beyond static early-pay discounts — dynamically negotiate and optimize payment timing.
`DiscountOffer` model + migration 0043, `/api/discounts` router, the
`discount_roi` / `discount_offers` / `discount_optimizer` / `discount_auto_trigger`
services, the `financing_adapters` package, and the `/discounts` web dashboard.
See `backend/docs/dynamic-discounting.md`.

- [x] Supplier-offered dynamic discounts — "Pay in 5 days for 3% off" (sliding scale) — `DiscountOffer.tiers` JSONB sliding scale, `source=supplier`
- [x] AI-optimized payment timing — maximize discount capture vs. cash preservation — `discount_optimizer.optimize` (greedy APR-ranked, cash-budget constrained)
- [x] ROI calculator per invoice — annualized return of paying early — `discount_roi` (cost-of-forgoing-discount APR) + `GET /api/discounts/invoices/{id}/roi`
- [x] Bulk discount negotiations — "Pay all 10 invoices from Vendor X early for 2%" — `POST /api/discounts/bulk-negotiate` (vendor-scoped offer over open invoices)
- [x] Supplier financing marketplace — connect to supply chain finance platforms — `services/financing_adapters/` (mock default + c2fo skeleton)
- [x] Dashboard: total discounts captured, missed, and projected savings — `GET /api/discounts/dashboard` + `/discounts` web page
- [x] Auto-trigger early payment when ROI exceeds configurable threshold — `discount_auto_trigger` sweep (`FEOH_DISCOUNT_AUTO_CAPTURE_ROI_THRESHOLD`); accepts only — never moves money (CFO-gated payment run still funds)

---

### 4-Way Matching (with Quality Inspection)
**Status:** Shipped

Extend PO matching to include quality inspection data — critical for manufacturing.

- [x] 4-way match: invoice vs. PO vs. goods receipt vs. quality inspection
- [x] Quality inspection model — pass/fail, partial acceptance, deviation notes
- [x] Reject invoices for goods that failed inspection (`quality_hold` error)
- [x] Partial payment — pay only for accepted quantity (`accepted_quantity`)
- [x] Configurable `require_inspection` per org (`Organization.settings.matching.require_inspection`)
- [x] Exception routing when quality data is missing or mismatched (`quality_hold`)
- [x] Configurable match rules per vendor or commodity type — `services/matching_rules.resolve_match_rule` resolves `require_inspection` + amount `tolerance_pct` per-field from `settings.matching.vendor_rules[<vendor_id>]` → `commodity_rules[<gl_account>]` → org default
- [x] Integration with QMS (Quality Management Systems) — `services/qms_adapters/` (mock default + generic skeleton) + `qms_sync` background sweep + `POST /api/inspections/sync`; pulls inspection records into `quality_inspections`, idempotent on `(org, inspection_number)`

---

## Priority 12: Collaboration & Self-Service

### Embedded Supplier Chat & Collaboration
**Status:** Shipped

In-app communication between AP team and suppliers — no more email chains.

- [x] Per-invoice chat thread — AP team and supplier see the same conversation
- [x] Attach files to messages (corrected invoices, supporting docs)
- [x] @mention team members to loop them in
- [x] Supplier gets email notification with link to portal chat
- [x] Chat history persisted and linked to audit trail
- [x] Templates for common messages (missing PO, amount mismatch, payment status)
- [x] Resolution tracking — mark thread as resolved

---

### No-Code Workflow Builder
**Status:** Shipped

Visual drag-and-drop workflow builder for non-technical users.

- [x] Canvas UI — drag steps onto a flowchart
- [x] Conditional branching — "if amount > $10K, route to CFO"
- [x] Parallel paths — multiple approvers in parallel
- [x] Custom step types — webhook, email notification, delay/wait
- [x] Template library — pre-built workflows for common scenarios
- [x] Version history — compare and rollback workflow changes
- [x] Simulation mode — test a workflow with sample invoices before activating
- [x] Import/export workflow definitions as JSON

---

## Priority 13: Platform Expansion (adjacent markets)

These features expand beyond core AP automation into broader spend management. Airbase and Coupa win mid-market deals by offering all-in-one spend platforms. Consider these only after core AP gaps are closed.

### Expense Management
**Status:** Done — WF1–WF4 all shipped (foundation, reporting + GL coding, policy engine + approval flow, corporate-card import + reconciliation). *(This read "In progress (foundation shipped — WF1)" until 2026-08-06 — every checkbox below had been ticked through WF4 for some time. The stale line is what motivated splitting this archive out of the open roadmap.)*

Corporate expense tracking and reimbursement. Airbase, Coupa, SAP Concur, and Bill.com (Divvy) all offer this. Increasingly expected as part of a "spend management" platform.

WF1 (foundation) shipped the data model — five tenant-scoped tables
(`expenses`, `expense_reports`, `expense_policies`,
`corporate_card_transactions`, `expense_preapprovals`; migration
`0039_expense_management`) — plus the `/expenses` + `/expense-reports` API
(CRUD, receipt upload + cross-tenant-checked download, report attach/detach
with total recompute, RBAC, audit). See `backend/docs/expense-management.md`.
Policy enforcement, card import/reconciliation, pre-approval gating, and the
frontend UX land in WF2-4.

- [x] Out-of-pocket expense submission with receipt capture *(backend foundation done — WF1; UX lands in WF2)*
- [x] Corporate card transaction import and reconciliation *(WF4 — `/api/corporate-card-transactions` CSV import (idempotent on `external_txn_id`) + amount/date+merchant match-suggestions + match/unmatch/ignore/create-expense both-sides linkage; `/expenses` Cards tab)*
- [x] Expense policies — per diem, mileage rates, category limits *(WF3 — `services/expense_policy.py` engine + `/api/expense-policies` CRUD; violations on `Expense.policy_violations`)*
- [x] Pre-approval workflows for high-value expenses *(WF3 — `/api/expense-preapprovals` request + approve/reject with segregation; pre-approval-required policy rule)*
- [x] Integration with existing virtual card program *(WF4 — `POST /api/corporate-card-transactions/sync-virtual-cards` pulls charged `VirtualCard` spend into the reconciliation feed via the synthetic `vc:<provider_card_id>` external id; matched expenses get `payment_method=virtual_card`)*
- [x] Expense reporting with GL coding *(WF2 — report summary + CSV export (`/api/expenses/export`) + per-expense and bulk GL coding)*
- [x] Manager approval flow (reuse AP approval infrastructure) *(WF3 — report submit/approve/reject reusing `approval_chain.check_segregation` + a CFO-threshold gate)*

**Competitors:** Airbase (core offering), Coupa (full module), SAP Concur (industry leader), Bill.com/Divvy (corporate cards + expenses)

---

### Procurement / Requisitions
**Status:** Done — full procure-to-pay: requisitions + approval, requisition→PO conversion, catalog management + guided buying, budget tracking, and non-PO intake forms. Six tenant-scoped tables (migration `0041_procurement`), four routers (`/api/requisitions`, `/api/catalogs`, `/api/budgets`, `/api/intake`), and frontend routes (`/requisitions`, `/catalogs`, `/budgets`, `/intake`). See [procurement.md](../backend/docs/procurement.md) + the four vertical docs.

Procure-to-pay: requisitioning, PO creation, catalog management. Coupa and Basware are leaders here. Airbase offers "intake-to-procure" for software purchases.

- [x] Purchase requisition creation and approval *(`/api/requisitions` — create/submit/approve/reject/cancel state machine, RBAC + segregation-of-duties on approval, every transition audited; `services/requisition_service.py`)*
- [x] Requisition-to-PO conversion *(`POST /api/requisitions/{id}/convert-to-po` — approved-only, idempotent + `SELECT … FOR UPDATE` row-locked so a replay returns the existing PO, audited)*
- [x] Catalog management (supplier catalogs, punch-out) *(`/api/catalogs` — Catalog/CatalogItem CRUD, vendor-linked; live cXML/OCI punch-out round-trip via the `punchout_adapters` family (mock default + real cxml), `PunchoutSession` (migration 0045), secret-gated supplier cart-return endpoint, convert-to-requisition — see [procurement-catalogs.md](../backend/docs/procurement-catalogs.md))*
- [x] Guided buying — direct users to preferred vendors/contracts *(`GET /api/catalogs/guided-buying` — deterministic, no LLM: preferred catalogs → preferred vendors, active contracts → in-contract vendors, active catalog items by category/vendor/text)*
- [x] Budget tracking — spend against department/project budgets *(`/api/budgets` — dimension/period budgets with compute-on-read committed (open reqs + converted POs, no double-count) + actual (matched invoices, now matching all four dimensions incl. department/project via `Invoice.department`/`project`, migration 0044) — see [procurement-budgets.md](../backend/docs/procurement-budgets.md))*
- [x] Intake forms for non-PO spend (software, services) *(`/api/intake` — free-form `form_data` requests, open→in_review→approved/rejected lifecycle, idempotent + row-locked intake→requisition conversion; PO created via the existing req→PO flow)*

**Competitors:** Coupa (full source-to-pay), Basware (procurement suite), Airbase (intake-to-procure), Medius (basic procurement)

---

### Contract Management
**Status:** Done — full CLM: contract repository + document upload, spend-to-contract tracking, renewal alerts (background sweep), compliance monitoring (`contract_noncompliant` exception), and contract-based PO creation. `/api/contracts` + invoice link/unlink. See [contracts.md](../backend/docs/contracts.md).

Contract lifecycle management. Only enterprise tools (Coupa, Basware) have this natively. Most mid-market competitors don't.

- [x] Contract repository — upload and store contracts
- [x] Spend-to-contract tracking — link invoices to contracts
- [x] Renewal alerts — notify before contract expiry
- [x] Contract compliance monitoring — flag spend outside contract terms
- [x] Contract-based PO creation — auto-populate PO from contract terms

**Competitors:** Coupa (full CLM), Basware (moderate), Airbase (basic repository)

---

### Public Developer API & Webhooks
**Status:** Done — all three slices shipped: API-key auth + the `/api/v1` read surface + key-management UI; outbound webhooks (dispatch, retries, dead-letter, redelivery UI); and the published, versioned OpenAPI spec + Swagger docs page + deprecation policy. Per-key rate limiting and usage metering shipped alongside.

The backend is a rich REST surface, but it's framed as an internal contract — CLAUDE.md notes "no OpenAPI published as the contract," and the `endpoint-inventory` skill exists precisely because integrators have no published spec. A first-class public API turns the platform into something customers and partners build on (ERP middleware, custom dashboards, RPA bots).

- [x] API-key auth for programmatic access — per-tenant, scoped, revocable keys (control-plane `ApiKey`, migration 0055; sha256 + indexed prefix; `X-API-Key` resolves org→tenant via the existing chokepoint), admin-gated mint/list/revoke, audited. First slice also ships a stable `GET /api/v1/invoices(+/{id})` read surface behind `require_api_scope('read')`. **Per-key rate-limiting now shipped** — the Redis sliding-window `rate_limit` primitive is keyed on `api_key_id` (per-key, not per-IP/org) inside `get_api_key_principal`, AFTER auth (a bad key still gets the opaque 401, never a 429); over-cap → 429 + `Retry-After`; cap `FEOH_PUBLIC_API_RATE_LIMIT_PER_MINUTE` (default 120/min, 60s window); fails open on a Redis outage. See backend/docs/public-api.md § Per-key rate limiting
- [x] Published, versioned OpenAPI spec + a stable `/api/v1` contract surface with deprecation policy — `GET /api/v1/openapi.json` (machine-readable) + `GET /api/v1/docs` (Swagger UI), generated from the live routes by `app/api/v1_openapi.py` but **scoped to `/api/v1` only** (internal SPA routes + orphan component schemas pruned out), overlaid with the `X-API-Key` security scheme, a `servers` entry, and `info.version: v1`; both public-to-read but 404 when `FEOH_PUBLIC_API_ENABLED` is off. Additive/path-based versioning + additive-only `v1` guarantee + ≥6-month sunset window documented in backend/docs/public-api.md § Versioning &amp; deprecation policy
- [x] **Outbound** webhooks (backend) — control-plane `WebhookSubscription` + `WebhookDelivery` (migration 0057, both in `CONTROL_TABLES`); per-subscription HMAC-SHA256 signing secret (returned once, reuses the `webhook_security.py` primitive), `X-Webhook-Signature`/`-Event-Id` headers; in-process dispatch (`services/webhooks/`) with bounded retries + exponential backoff → dead-letter; dedupe on `(subscription, event_id)`. Admin-gated `/api/webhooks` CRUD + delivery log + **redelivery** endpoint (audited, PII-free). Emits `invoice.approved` + `payment.settled` from the `transition_invoice` chokepoint, and `exception.raised` from the shared exception-create chokepoint `services/exception_service.create_exception` (all five former Exception-construction sites — invoice_warnings / extraction / review / positive_pay — now route through it; full coverage, best-effort, PII-free, money as exact string, dedupe on the exception id). `FEOH_WEBHOOKS_ENABLED` kill switch (OFF in local dev). **Frontend management + redelivery UI now shipped** — `/admin/webhooks` (Settings nav group, admin-only, redirects non-admins): a Subscriptions section (list + Create modal with a one-time signing-secret reveal, edit, armed two-click delete) and a Deliveries section (paginated delivery log with a URL-backed status filter + a Redeliver action that surfaces the backend 409 on an already-`delivered` delivery), over `GET/POST/PATCH/DELETE /api/webhooks` + `GET /api/webhooks/deliveries` + `POST .../redeliver` via `$lib/api/webhooks.ts`. e2e: `frontend/tests-e2e/admin/webhooks.spec.ts`. See backend/docs/public-api.md § Outbound webhooks
- [x] Developer docs + sandbox keys against the local-first stack; key-management UI in org settings — developer docs ship (backend/docs/public-api.md, incl. the published OpenAPI/Swagger surface); keys are minted per-org and work against the local-first stack today (mock billing adapter grants `public_api`). **Frontend key-management UI shipped** — `/admin/api-keys` (Settings nav group, admin-only, redirects non-admins): list (prefix + scopes + created/last-used + Active/Revoked status), a Create-key modal with a one-time copy-able plaintext reveal ("shown only once"), armed two-click Revoke (idempotent), and a per-key usage view (totals + trailing-window + per-day breakdown) over `GET /api/api-keys` / `POST` / `DELETE {id}` / `GET {id}/usage` via `$lib/api/apiKeys.ts`. e2e: `frontend/tests-e2e/admin/api-keys.spec.ts`
- [x] Per-key usage metering (feeds the billing track below) — control-plane `ApiKeyUsage` aggregate (table `api_key_usage`, migration 0058, in `CONTROL_TABLES`): one row per `(api_key_id, usage_date)` with an exact `request_count`. Incremented on the `get_api_key_principal` auth path via a single `INSERT … ON CONFLICT … DO UPDATE` upsert, **best-effort** (a meter failure is swallowed PII-free and never breaks the authenticated request) alongside the `last_used_at` stamp. Admin-gated `GET /api/api-keys/{id}/usage` returns all-time + trailing-window totals + a per-day breakdown (counts only — never the hash/plaintext). Control-plane-only migration (no tenant fan-out). See backend/docs/public-api.md § Per-key usage metering

**Competitors:** Bill.com (public API + dev portal), Tipalti (API + webhooks), Coupa (open API platform)

---

### Platform Billing & Metering
**Status:** Done — plan/subscription model, usage rollup, pluggable billing adapters (mock + live Stripe), entitlement gating, the customer-facing read surface, live Stripe API calls + inbound webhook + dunning, per-org Stripe provisioning + mid-period proration, the payment-method + invoices/receipts UI, and the live plan-change UI (wired to the shipped `POST /api/billing/change-plan`, tested against the `mock` adapter). A provisioned Stripe account to verify the live-Stripe path end-to-end is a deployment prerequisite, not an unshipped feature — tracked as the generic credential-blocked "Stripe Billing" item in [followups.md](followups.md).

The product meters extraction usage (`ExtractionUsage`, `CardRebate`) but had no way to **bill** for the SaaS itself — plans, subscription state, usage rollups, invoices to customers. Needed before commercial launch beyond hand-managed contracts.

- [x] Plan / subscription model (control-plane) — `Plan` (tier, monthly price `Numeric`, per-seat + usage components JSON, feature entitlements JSON, trial_days) + `Subscription` (org FK, plan FK, status `trialing|active|past_due|canceled`, period + trial window, nullable `external_subscription_id`). Migration 0056 (control-plane, idempotent); both in `CONTROL_TABLES`. See `backend/docs/billing.md`
- [x] Usage rollup — `services/billing/usage_rollup.py` aggregates `ExtractionUsage` (+ `CardRebate` total) into Decimal-exact billable meters per org/period (pure read, no mutation)
- [x] Billing adapter family (`services/billing_adapters/`) — `mock` default (local-first, deterministic) + `stripe_billing` (live Stripe REST via `httpx`, fail-closed without a key). Registry decorator + `get_billing_adapter()`; `FEOH_BILLING_PROVIDER` + per-org override
- [x] Entitlement gating — `require_entitlement` (JWT) / `require_api_entitlement` (API key) in `deps.py`, 402 on a plan miss, composes with `require_roles` / `require_api_scope`; wired onto the public `/api/v1` surface (`public_api` feature). Reads `services/billing/entitlements.py`
- [x] Customer-facing read endpoint — `GET /api/billing/subscription` (admin/cfo): current plan + status + usage-to-date
- [x] Live Stripe Billing API calls (create/get subscription, report usage) + the inbound webhook route (HMAC-verified, deduped) + dunning / past-due automation — `stripe_billing` adapter's create/get-subscription + report-usage hit the Stripe REST API via `httpx` (idempotency-key header on create, exact decimal-string usage values, fail-closed without a key); `POST /api/billing/webhook/{provider}` verifies the HMAC + dedupes by `event_id` + drives the idempotent `Subscription` lifecycle transition (`trialing→active→past_due→canceled`) with an append-only audit row, 204-silent on every rejection; the `billing_dunning` sweep cancels subscriptions overdue past the grace window (never moves money). `FEOH_BILLING_WEBHOOK_ENABLED` / `FEOH_BILLING_DUNNING_ENABLED` kill switches. See `backend/docs/billing.md`
- [x] Per-org Stripe customer/price provisioning + mid-period proration + plan change — `ensure_customer` / `ensure_price` (idempotent Stripe creates; minor-units via exact Decimal) + `services/billing/provisioning.provision_org_billing` resolves-and-persists the per-org `stripe_customer_id` + per-plan `plan_price_ids` on `settings.billing` (no migration). `services/billing/proration.compute_proration` is a pure Decimal-exact mid-period proration (`ROUND_HALF_UP`, 2 dp); `POST /api/billing/change-plan` (admin/cfo) repoints the live subscription, records the proration, and writes an append-only `billing.plan_changed` audit row — idempotent (same-plan retry is a no-op, never double-charges) and never moves money directly. `GET /api/billing/plans` lists the active plan catalog (cheapest first) for the change-plan picker. See `backend/docs/billing.md`
- [x] Payment-method endpoint — adapter `create_setup_intent(customer_id)` → `ProviderSetupIntent` (single-use `client_secret`; no charge, no PAN) + `list_payment_methods(customer_id)` → `ProviderPaymentMethod` **PII-safe metadata only** (brand/last4/exp — **never a PAN**). `POST /api/billing/payment-method/setup-intent` + `GET /api/billing/payment-methods` — both admin/cfo, degrade gracefully (no customer / unconfigured → `configured=false` + null secret / empty list, never a 500). See `backend/docs/billing.md`
- [x] Billing invoices / receipts list — adapter `list_invoices(customer_id, limit)` returning the org's past billing invoices/receipts as `ProviderInvoice` DTOs (money as an exact decimal string). `GET /api/billing/invoices` (admin/cfo) degrades gracefully — no customer / unconfigured / provider error → empty list, never a 500. See `backend/docs/billing.md`
- [x] Customer-facing billing surface (UI) — `/billing` (Subscription sub-tab of the Billing nav group, admin/cfo-gated): current plan + price (`<Money>`), a `SubscriptionBadge` status pill, the period/trial window, granted entitlements, usage-to-date meters, the invoices/receipts list, the payment-method section (saved cards + Add/replace-card SetupIntent flow with a deployed-only Stripe Elements seam), and the **live plan-change flow**: a `Modal` plan picker (`GET /api/billing/plans`, cheapest first, current plan marked + non-selectable) → a plain-language notice that the change applies immediately and prorates the current period (there is no preview-only mode on the backend) → `POST /api/billing/change-plan` on confirm → the result view renders the REAL returned proration via `<Money>` (or a clean no-op message when the org was already on the target plan, `changed: false`). `backend/tests/test_billing.py`. See `backend/docs/billing.md` § Customer-facing UI

**Competitors:** standard SaaS monetization; the metering primitives (`ExtractionUsage`) already exist — this productizes them

---

## Legacy “Done” list (pre-dating the per-section statuses)

- [x] Multi-tenant architecture (database per tenant, subdomain routing)
- [x] JWT authentication with Redis token revocation
- [x] User management (invite, roles, self-service profile)
- [x] Role-based UI restrictions (Admin, AP Manager, AP Clerk, CFO)
- [x] Invoice CRUD with all standard fields
- [x] Invoice upload with PDF viewer
- [x] Mock AI extraction with confidence scoring
- [x] Configurable workflow engine (extraction, approval, ERP export steps)
- [x] Multi-approver support with search/pick UI
- [x] Approval thresholds (auto-approve below, require CFO above, max amount)
- [x] Approve/reject buttons with audit trail
- [x] Activity timeline in invoice modal (audit log with actor names)
- [x] Invoice warnings (duplicates, fraud flags, missing fields)
- [x] Bulk operations (delete, status change, export)
- [x] Status transition rules with valid transitions per status
- [x] ERP adapter pattern (Merge.dev + direct adapters for BC and NetSuite)
- [x] ERP webhook endpoint for status callbacks
- [x] ERP test connection button in org settings
- [x] ERP retry button in invoice modal for failed sends
- [x] ERP status display in invoice modal (document ID, error details)
- [x] Post-ERP statuses (posted_in_erp, payment_scheduled, paid)
- [x] Organization settings with per-section save (company, defaults, ERP, cards)
- [x] Payments page with tabs (Queue, History, Runs) and summary bar
- [x] Virtual card adapter pattern (Lithic + Nium) with platform/BYOK dual model
- [x] Card detail security (role-restricted, audit-logged, never cached)
- [x] Card API endpoints (generate, list, cancel, details, webhook, rebates, dashboard)
- [x] Advanced search and filtering
- [x] Export (CSV, JSON, XML — single and bulk)
- [x] Vendor management (status, verification, ERP sync, fuzzy matching, AI auto-creation)
- [x] Vendors page with verify/reject actions, ERP sync button, status filters
- [x] Vendor matching wired into invoice extraction pipeline
- [x] Vendor accepts_virtual_cards field for card payment eligibility
- [x] Seed data: 10 vendors (mixed sources/statuses), 10 invoices (linked to vendors, varied statuses)
- [x] Sidebar navigation with role-based visibility
- [x] Sidebar icons for all nav items (Admin, Organization, Workflows, etc.)
- [x] Delete user with cascade (role assignments cleaned up)
- [x] Invoice delete with full cascade (all related tables cleaned up)
- [x] Workflow ERP export step config (auto-send, format, payload options)
- [x] Workflow approval step: approver search/pick UI with chips
- [x] Workflow approval thresholds (auto-approve below, CFO above, max amount)
- [x] Self-service profile editing (name, password) in sidebar popover
- [x] AI extraction adapter pattern (Claude Vision, OpenAI GPT-4V, AWS Textract)
- [x] Platform/BYOK dual model for extraction (per-invoice billing for platform)
- [x] Per-field confidence scoring on extraction
- [x] AI GL coding (suggest GL account + cost center in extraction prompt)
- [x] Line item extraction
- [x] Extraction usage tracking for billing (ExtractionUsage model)
- [x] PO matching service (2-way and 3-way with configurable tolerance)
- [x] Exception queue with filters, resolution actions, and summary
- [x] Fraud detection (duplicate invoices, round amounts, future dates, unverified vendors)
- [x] Dashboard KPIs (pipeline, aging, vendor spend, monthly trends, upcoming payments)
- [x] Payment run creation and execution with ERP sync
- [x] Contract management (repository + upload, spend-to-contract tracking, renewal alerts, compliance monitoring, contract-based PO creation)

---

### AI Cash-Flow Copilot
**Status:** COMPLETE — Phases 1–3 shipped (read-only cash Q&A + `/cash-flow` copilot; proposed payment plans via `propose_payment_plan` + the plan card; draft-only enactment — stage a draft payment run / accept the plan's discounts) — see [cash-flow-copilot.md](cash-flow-copilot.md).
**Shipped in full** (round 13, 2026-08-19): saved plans / plan-vs-actual (`CashPlan`, migration 0087) and consolidated cross-entity mode closed the last deferred bucket. See [decisions.md](decisions.md) §54-§55.

A **beyond-parity** differentiator, not a competitive gap: a natural-language,
forward-looking copilot that answers "when do I run low on cash?" and "what
should I pay early / on time / defer, and what does it cost me?", and can
*propose* a cash-constrained payment plan. It builds directly on the shipped
primitives above — pairing the conversational assistant (tenant isolation,
audit, budgeting, local-first mock/ollama/claude adapters) with the cash-flow
forecast, the payment-timing what-if, and the ROI-ranked discount optimizer.
No mid-market competitor pairs a conversational interface with a cash-constrained
early-pay optimizer.

**Hard boundary: the copilot never moves money.** The LLM only turns NL into a
typed tool call and narrates the result — every dollar figure comes from an
existing deterministic pure function (`bucket_outflows`, `compute_cash_position`,
`apply_payment_timing_scenario`, `discount_optimizer.optimize`), so answers are
exact and reproducible under the `mock` adapter. Its most privileged write is
staging a **draft** payment run (existing idempotent, CFO-gated path); funding
stays behind the unchanged human review + CFO gate + segregation.

- [x] Phase 1 — read-only cash Q&A: four new entity-scoped, finance-leader-gated (`admin`/`ap_manager`/`cfo`, not `ap_clerk`) planning tools (`get_cashflow_forecast`, `get_cash_position`, `run_payment_whatif`, `optimize_discount_capture`) registered alongside the existing assistant tools; `/api/cash-flow/copilot(+/stream)` façade; `/cash-flow` chat + cash-position chart. Money as exact strings (must NOT inherit the analytics endpoints' `float()` coercion). Gated by `FEOH_CASHFLOW_COPILOT_ENABLED` (default on); `FEOH_CASHFLOW_COPILOT_DEFAULT_HORIZON_DAYS` (default 90).
- [x] Phase 2 — proposed plans: `propose_payment_plan` tool assembles a plan artifact (period-by-period schedule + discounts to capture + resulting cash curve) from the pure functions (`bucket_outflows`/`compute_cash_position`/the discount optimizer's own selection, reused not re-derived — same source of truth as `optimize_discount_capture`); a selected discount re-times onto its `pay_by` period at its discounted outlay when it matches a single commitment row, otherwise it's flagged `unretimed_offer_ids` rather than misrepresenting the curve. Plan-card UI (`PlanCard.svelte`). See `services/cash_flow_plan.py`.
- [x] Phase 3 — draft-only enactment: `POST /api/cash-flow/plans/{plan_id}/draft-run` (stages a **draft** `PaymentRun` over the plan's currently-payable commitments via the shared `services/payment_runs.py`, idempotent on the deterministic `plan_id` — `payment_runs.plan_id` partial-unique index, migration 0079 — execute stays CFO-gated and unchanged) + `.../capture-discounts` (status-only accept of the SAME optimizer selection, never moves money). Both re-verify `plan_id` against the client's replayed plan parameters and refuse (409) on a mismatch. Plan-card "Create draft run" / "Capture N discounts" buttons, human-confirmed + audited.
- [x] Deferred: saved plans / plan-vs-actual (`CashPlan` model + migration), opening-balance provenance surfacing, consolidated cross-entity mode, proactive shortfall-alert sweep.

**Competitors:** none pair NL + cash-constrained early-pay optimization; Coupa/Basware have cash analytics without a conversational copilot.

---

## Priority 10: Compliance & E-Invoicing

---

## Priority 13: Platform Expansion (adjacent markets)

These features expand beyond core AP automation into broader spend management. Airbase and Coupa win mid-market deals by offering all-in-one spend platforms. Consider these only after core AP gaps are closed.

### White-Label / Partner Branding
**Status:** ✅ Complete — per-tenant brand config + CSS-var theming, branded outbound PDFs / emails / analytics exports, supplier-portal theming, custom-domain tenant resolution, the custom-domains admin panel, partner/reseller admin with two-sided-consent link provisioning and new-child provisioning, the TLS/DNS runbook, and — closing it — a vanity domain that works end to end: the SPA classifies hostnames and calls `/api` same-origin (`hostRouting.ts`), outbound links resolve per-tenant (`utils/tenant_urls.py`), passkeys resolve their relying party from the tenant's own registered domains (`services/webauthn_rp.py`, migration 0091), and SSO accepts a `Host`-resolved tenant with an opt-in per-org callback override. See `docs/decisions.md` §86, §87, §91, §92.

Per-tenant theming so resellers, banks, and ERP partners can offer the platform under their own brand — a common mid-market distribution channel and an enterprise procurement ask.

- [x] Per-tenant brand config — logo, accent/theme tokens, product name, support + legal links on `Organization.settings.brand` (no migration), `GET/PUT /api/organization/branding` (admin mutate, audited, hex/URL-validated). Frontend `brand` rune store applies `--accent`/`--accent-strong` CSS custom properties on mount (org colors override the AA defaults only when set), logo + product name in the sidebar + `<title>`, edited from the Organization → Branding panel. See `docs/white-label.md`
- [x] Custom domain / subdomain support beyond `*.localhost` tenant routing — backend resolution layer shipped: a tenant can register vanity hostnames on `settings.brand.custom_domains` (JSON, no migration); `app/tenant.py::get_tenant_slug` falls back to matching the request `Host` against that list when no `X-Tenant-Slug` header is present (`resolve_tenant_slug_by_custom_domain` + `normalize_custom_domain`, JSONB `@>` lookup). The resolved slug is only a **candidate** — `get_tenant`'s JWT `org`-claim cross-check still gates it, so a forged `Host` can't widen access (invariant #4 preserved); unknown/malformed host falls back to the original 400. **Admin UI + endpoint pair now shipped**: `GET/PUT /api/organization/branding/custom-domains` (read open to any authed user, admin-only mutate; each host normalized through the resolver's own `normalize_custom_domain`, de-duped, **cross-org-unique** — a host claimed by another tenant is 409 — and audited `organization.custom_domains_updated` PII-free count-only; a branding save preserves the list) + the Organization → Custom Domains panel (list / add / armed-remove). TLS + DNS provisioning stays infra (out of scope for app code; runbook deferred). See `docs/white-label.md § Custom domains` + `docs/multi-tenancy.md`
- [x] Branded outbound surfaces (PDFs + emails) — remittance / 1099 / SOX-audit PDFs and outbound transactional emails carry the tenant product name + logo + accent (resolved through one `services/branding.get_brand_context` helper; PDF logo embed is size/time-bounded + fail-soft to product-name text; email From display name + HTML header + support footer applied in the shared email-adapter base). **Analytics report exports now branded too**: `GET /api/analytics/export/{report}` (invoice_register / vendor_spend / payment_register / aging_snapshot / cashflow_forecast) takes `format=csv|pdf` — the PDF (`services/analytics_report_pdf.py`) draws the same branded header (logo / product-name / accent) via the shared `build_logo_flowable` (size/time-bounded + fail-soft to text), and the CSV prepends a `#`-comment brand-provenance block (product name + org + report + generated-at) ahead of the unchanged, still-column-positional data grid (`report_export.brand_provenance_header`). See `docs/white-label.md`. **Supplier-portal theming now ships too**: the supplier portal (`/portal/*`, including the unauthenticated login page) carries the tenant brand (accent + logo + product name + `<title>`) via the public-by-design, PII-free `GET /api/portal/branding` (tenant resolved by the existing `get_tenant` chokepoint; returns only the whitelisted `BrandConfig` fields) + the `portalBrand` store. See `docs/white-label.md` § Supplier-portal theming. (The localized email catalogue remains.)
- [x] Partner/reseller admin — a parent org administers a set of branded **child** tenants. Control-plane self-FK `Organization.parent_org_id` (migration `0065_org_parent`, control-plane-only); "partner" is **derived** (referenced by ≥1 child — can't be self-claimed). `/api/partner` (admin-gated) lists children + reads/writes each child's `settings.brand`, scoped at the SQL layer to `parent_org_id == caller org` (the `get_tenant` org-claim cross-check still gates; a non-child id is an opaque 404 — no enumeration); child-branding writes preserve `custom_domains` + audit into the **child's** trail PII-free. Frontend `/admin/partner` panel. **Link provisioning shipped (round 2):** attaching an *existing* tenant uses **two-sided consent** — the prospective child's own admin mints a short-lived HMAC-signed single-use link code (`POST /api/partner/link-code`), the partner's admin redeems it (`POST /api/partner/children`); a partner can't forge a code or adopt a non-consenting org, no re-parent without detach, single-use via Redis `jti`, `DELETE /api/partner/children/{id}` detaches (scoped to own children), both mutations audited PII-free on both org trails, gated by `FEOH_PARTNER_LINK_SIGNING_KEY` (fail-closed, no fallback; non-secret dev value committed). No migration (stateless token + existing column). **New-tenant provisioning shipped (this slice):** `POST /api/partner/children/provision` (admin-only) is the thin wrapper over `services/tenant_provisioning.provision_tenant` that creates a brand-**new** tenant already parented to the caller — no `parent_org_id` input (always the caller's org, so a partner can only create a child under itself), validates slug (format/reserved/availability) + admin-email shape like signup, reuses `provision_tenant`'s orphan-DB rollback (clean 409 on a slug race, never a half-create), audits `partner.child_provisioned`/`partner.parent_linked` PII-free on both trails, and returns a one-time temp password for the new admin. `/admin/partner` "Create child tenant" modal drives it; no migration. The TLS/DNS automation for the new tenant's vanity domain stays an infra-owned follow-up. See `docs/white-label.md § Partner / reseller admin`

**Competitors:** AvidXchange + several bank-channel AP products ship white-label; a distribution lever more than a feature

---

## Priority 14: Receiving, ERP depth & commerce sync

### 1. Goods-receipt entry (receive against a PO)
**Status:** Done — `POST /api/goods-receipts` + cancel, `/goods-receipts` → Record receipt, migration 0109, decisions §261. See [po-matching.md](../backend/docs/po-matching.md) § Recording a goods receipt.

- [x] `POST /api/goods-receipts` — receive against a PO, line by line (ordered vs already-received vs this delivery), partial and zero-quantity lines, entity taken from the PO
- [x] Record who entered a receipt (`source` + `recorded_by_user_id`), and refuse the auto-close of a `po_mismatch` hold when the receipt that cleared it was typed in by someone implicated in the invoice — the inspection rule of §249, applied to receipts
- [x] Cancel a hand-entered receipt (a cancelled receipt already stops counting in the matcher)
- [x] Re-run matching on every invoice citing the PO, so a hold lifts (or is raised) when the receipt lands
- [x] Web: "Record receipt" on `/goods-receipts`
- [x] A PO whose receipts are all cancelled reads as a 3-way match with nothing received, so cancelling a receipt can never release a hold; exception agents escalate rather than clear a `po_mismatch` resting on an implicated receipt (both from the pre-merge money-path review)

Still open from this work, tracked in [followups.md](followups.md): the person who records a receipt can still approve the invoice it supports, and mobile has no receipt entry.

---
