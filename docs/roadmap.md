# Roadmap — open work

Feature backlog for the AP automation platform, ordered by impact. **This file
carries only the areas that still have unshipped work.** A section lives here
because something in it is genuinely open; the `**Open:**` line under each
status names exactly what, and every one of those items is tracked with its
category, durable fix, and trigger in [followups.md](followups.md).

Fully-shipped areas were moved to [roadmap_shipped.md](roadmap_shipped.md) —
verbatim, nothing summarized away. 46 of the 54 sections live there. Look for
prior art in the archive before assuming a capability doesn't exist.

**Related:** diagnosed-but-unfixed defects in
[known-issues.md](known-issues.md); the reasoning behind non-obvious design
calls in [decisions.md](decisions.md). This file carries **scope and status**
— when an entry needs to explain *why* something was built a given way, put the
paragraph in `decisions.md` and link it.

## Legend

- **Done** — implemented and working
- **Partial** — backend or models exist, needs completion
- **Planned** — not started

**Prune on landing.** When a section's last open item ships, move the whole
section to `roadmap_shipped.md` in the same commit. That is the discipline that
keeps this file honest — a stale "In progress" status survived unnoticed here
for weeks precisely because it was buried in 1131 lines of shipped prose.

---

## Priority 7: Authentication & Enterprise Security
**Competitive gap: SSO is an enterprise deal-blocker**

### SOC 2 Readiness
**Status:** Engineering prereqs complete — **all code controls landed; process work pending founder sign-off**
**Open:** All six process items below — vendor selection, policy library, onboarding/offboarding evidence, IR runbook + on-call, Type I audit, Type II window. **Every engineering prereq is complete.** *(a) blocked on a founder decision + a vendor contract.* Tracked in [followups.md](followups.md).

SOC 2 Type I (design) → Type II (operating over time) is the table-stakes security attestation for selling to finance teams. Full plan in [`docs/soc2-readiness.md`](soc2-readiness.md) — vendor comparison, control mapping, timeline, and what the founder still needs to do as a human.

**Engineering prerequisites:**
- [x] Access reviews — `backend/scripts/access_review.py` exports every user × role × org as CSV (quarterly)
- [x] Backup + DR runbook — `docs/backup-disaster-recovery.md` with RTO/RPO + restore procedures
- [x] Secrets rotation runbook — `docs/secrets-rotation.md` (cadence + procedure for every secret)
- [x] Vulnerability scanning in CI — Dependabot (shipped) + CodeQL SAST (Python + JS) + Trivy on the backend container, weekly + on push (`.github/workflows/security.yml`)
- [x] RBAC enforcement at API layer (separate roadmap item — already done)
- [x] MFA support + org-level enforcement (separate roadmap item — already done)
- [x] Session management — per-user concurrent session cap (Redis sorted set, `FEOH_MAX_CONCURRENT_SESSIONS`), forced logout on role change / deactivation (`services.session_management.revoke_user_sessions`), standalone admin force-logout (`POST /api/admin/users/{id}/revoke-sessions`), and self-service session visibility + revocation (`GET /api/auth/sessions`, `DELETE .../{jti}`, `POST .../revoke-others` + the `/profile` Signed-in devices card)
- [x] Centralized audit log shipping — background shipper loop + adapters (CloudWatch Logs + S3 Object Lock) at `backend/app/services/audit_log_shipper.py` + `services/audit_shipping/`. See `backend/docs/audit-log-shipping.md`.
- [x] Auth event audit log — login/logout/MFA/SSO events written via `app/services/audit_dispatch.py::dispatch_auth_audit` into the tenant `audit_log` table
- [x] HSTS header + security-header middleware (`backend/app/main.py` `SecurityHeadersMiddleware`, gated on `FEOH_HSTS_ENABLED`); TLS smoke script at `backend/scripts/verify_tls.py`
- [x] KMS key auto-rotation flag in Terraform — `infra/kms.tf` `enable_key_rotation = true`
- [x] S3 versioning + Object Lock in Terraform — `infra/s3.tf` (versioning Enabled; invoice-files GOVERNANCE 365d, audit-logs COMPLIANCE 2555d)

**Process / attestation work** (founder, not engineer):
- [ ] Vendor selection — Vanta, Drata, Secureframe, or Sprinto. See `docs/soc2-readiness.md` § Vendor comparison.
- [ ] Policy library — info security, incident response, change management, access control, vendor mgmt (vendor templates)
- [ ] Employee onboarding / offboarding checklist with evidence collection
- [ ] Incident response runbook + on-call rotation
- [ ] SOC 2 Type I audit (point-in-time) — typical 4–8 weeks after prereqs
- [ ] Begin Type II observation window (6+ months) for annual renewal

**Competitors:** Every serious competitor has SOC 2 Type II. Without it, enterprise deals stall at security review.

---

## Priority 8: Mobile & Notifications
**Competitive gap: most competitors have mobile apps**

### Flutter Mobile App
**Status:** Done — full iOS + Android app; web-parity + admin screens all shipped
**Open:** Push-token registration to the backend and notification deep-linking (`push_service.dart:49` / `:99`). Every roadmap checkbox below ships; FCM/APNs is wired but no-ops until configured. *(a) blocked on a Firebase project + APNs auth key.* Tracked in [followups.md](followups.md).

Flutter app at `mobile/` with login, dashboard, invoice list, approve/reject, payments, settings. Same backend API as web.

**Done:**
- [x] Login with tenant selection
- [x] Dashboard (KPIs, aging buckets, top vendors)
- [x] Invoice list with search + status filter chips
- [x] Invoice detail with approve/reject
- [x] Approvals tab with swipe-to-approve
- [x] Payment history list
- [x] Role-based bottom navigation
- [x] Settings (profile, tenant info, logout)
- [x] JWT in secure storage (iOS Keychain / Android Keystore)
- [x] API contract tests in backend to prevent client breakage

- [x] Camera OCR — snap photo or pick from gallery → upload → trigger AI extraction
- [x] Push notifications — Firebase Cloud Messaging + local notifications (no-op until Firebase configured)
- [x] Offline mode — SQLite cache for dashboard and invoice list, serves cached data on network failure
- [x] Biometric login — Face ID / fingerprint / device PIN toggle in settings, checked on app launch

**Medium priority — parity with web (see `mobile/CLAUDE.md` for full gap list):**
- [x] Invoice upload via file picker (PDF/PNG/JPG/TIFF support) — `CameraCapture.pickDocument` via `file_picker`, Choose-file button on the capture screen → same `/api/invoices/upload` extraction pipeline as the camera path; PDFs preview as a document card, images inline
- [x] Invoice editing (change fields in detail screen) — edit sheet → `PATCH /api/invoices/{id}` (vendor/number/amount/PO/GL/description/due date; money + dates as string-Decimal), gated to admin/ap_manager/cfo on editable statuses
- [x] Activity timeline in invoice detail (audit log) — `GET /api/invoices/{id}/audit-log` rendered as a timeline widget (actor, action, time, per-field before→after), with empty/loading/error states
- [x] PDF/image viewer for uploaded invoice files — `InvoiceFileViewer` on the invoice detail screen: images via `Image.network` (auth headers), PDFs fetched as bytes (`ApiClient.getBytes`) + rendered with `pdfx`; inline image-thumbnail / PDF-card preview opens the full viewer; loading/error/Retry states
- [x] Exception queue (list, detail, resolve, escalate, dismiss, assign, bulk-resolve) — `ExceptionsScreen` + `ExceptionDetailScreen` + `ExceptionStore` over `GET /api/exceptions`, `GET /api/exceptions/{id}`, `POST /api/exceptions/{id}/resolve`, `POST /api/exceptions/{id}/assign`, `POST /api/exceptions/bulk/resolve`, admin/ap_manager. Detail view shows full fields + linked invoice + SLA/due/overdue + assignee with the three actions; tapping a row opens it. Assign uses an org-user picker (admin-gated — it needs the admin-only `/admin/users` list; ap_manager can still act). Multi-select (long-press / checklist app-bar action) + the shared `BulkActionBar` → `bulkResolve` with an updated/skipped snackbar
- [x] Vendor management (list, verify/reject, ERP sync) — `VendorsScreen` + `VendorStore` over `GET /api/vendors`, status filter chips + search, swipe verify/reject + ERP-sync action, RBAC-gated
- [x] Payment queue (select invoices, choose method) — `PaymentQueueScreen` Queue tab over `GET /api/payments/queue`, per-row method dropdown
- [x] Payment runs (create/execute batches) — Runs tab: create draft from selection, execute/cancel with CFO-approval-gate surfacing
- [x] Payment summary cards (total paid, pending, rebates) — KPI summary bar over `GET /api/payments/summary`
- [x] Advanced search modal (vendor, PO, amount range, date range) — `AdvancedSearchSheet` (app-bar `tune` action with an active-filter badge) → `InvoiceStore.setFilters` → `GET /api/invoices` with `vendor` / `po_number` / `amount_min` / `amount_max` / `due_date_from` / `due_date_to`; seeded from the live filters, validated (min ≤ max, plain decimals)
- [x] Invoice warnings/fraud flags display — `InvoiceWarningsPanel` on the detail screen renders `Invoice.warnings` (severity-coloured) + the `po_match` panel (match type, status, variance %, issues), parity with the web modal
- [x] ERP status display on invoice detail — `ErpStatusPanel` derives ERP reference / document id / send error from the loaded audit log (`invoice.erp_*` / `invoice.completed` entries); shown for ERP-bound statuses + ERP-failed invoices

**Low priority — admin features (less needed on mobile):**
- [x] Bulk operations (select multiple, delete, status change) — invoice
  multi-select (long-press or the checklist app-bar action) + bulk delete /
  bulk status-change over `POST /api/invoices/bulk/{delete,status}`, gated to
  admin/ap_manager/cfo; backend skips immutable-status rows; result snackbar
  reports deleted/updated + skipped counts. `BulkActionBar` widget +
  `InvoiceStore` selection state. See `mobile/CLAUDE.md`.
- [x] Admin user management — `AdminUsersScreen` + `AdminUserStore` over
  `/api/admin/*`: list/search users, **create a user** (validated form sheet →
  `POST /api/admin/users`; the server-generated one-time temporary password is
  surfaced for the admin to hand over, then the list refreshes), edit roles
  (system roles only), activate / deactivate, and **delete a user** (armed /
  confirmed destructive action → `DELETE /api/admin/users/{id}`; self-delete
  disabled client-side, the backend's 409 self / still-referenced reason
  surfaces in the failure snackbar). Admin-only, reached from Settings →
  Administration.
- [x] Organization settings — `OrgSettingsScreen` + `OrgSettingsStore` read +
  edit the safe subset the web app exposes (company profile + invoice defaults)
  via `GET/PATCH /api/organization`; ERP/payment/SSO secrets deliberately not
  surfaced; web-set `logo_url` carried through unedited. Admin-only.
- [x] Export (CSV/XML) — an **Export** action on the invoice `BulkActionBar`
  (role-gated as the other bulk ops) POSTs the selected ids to
  `POST /api/invoices/bulk/export` (CSV or XML format sheet) and hands the
  rendered bytes to the platform share sheet (`share_plus` + `ApiClient.postBytes`).
- [x] Workflow management — **read-only** list + step viewer: `WorkflowsScreen` +
  `WorkflowStore` over `GET /api/workflows` (name / active+default badges / step
  count) → `WorkflowDetailScreen` over `GET /api/workflows/{id}` (per-step
  type/name/enabled + PII-free config summary), reached from Settings →
  Administration, admin-gated. Create/edit stays a desktop surface (the no-code
  builder).

**Files:** `mobile/` — see `mobile/CLAUDE.md` for full structure

---

## Priority 9: AI-Powered Automation (strong differentiators)

### Predictive Cash Flow Forecasting
**Status:** Done — GET /api/analytics/cashflow_forecast (+ /cashflow_whatif, /cash_position) over Invoice/PaymentSchedule/Payment; what-if early/on-time/late with discount capture; cash-position with BYO opening balance + threshold alerts; CSV export via the analytics/export registry; CFO web dashboard at /cfo. Bank-balance auto-sync (optional PaymentAdapter.get_balance capability — mock returns a deterministic balance; best-effort, falls back to the manual opening balance) + persisted thresholds (Organization.settings.cashflow, GET/PUT /api/analytics/cash-position-settings, no migration) now shipped. Mobile shipped: CashFlowScreen + CashFlowStore (CFO/admin) over cashflow_forecast + cash_position — KPI summary (opening/projected-end balance, committed/pending outflow), per-period forecast + running cash-position list, low-balance alert, 30/60/90-day horizon chips; reached from a CFO/admin-gated Dashboard app-bar action; money rendered from server display strings (no device float math).
**Open:** A real banking-aggregator (Plaid-style) balance feed. Bring-your-own opening balance and the provider `get_balance` path both ship. *(a) blocked on an aggregator account.* Tracked in [followups.md](followups.md).

Use AP data to forecast cash outflows and optimize payment timing.

- [x] Forecast daily/weekly/monthly cash outflows from pending invoices
- [x] Factor in payment terms, early-pay discounts, and approval pipeline
- [x] "What-if" scenarios — impact of paying early vs. on-time vs. late
- [x] Cash position dashboard with AP commitments overlay
- [x] Alert when projected outflows exceed thresholds
- [x] Integration with bank balance data for complete cash picture — bring-your-own opening balance (query param or `Organization.settings.cashflow.opening_balance`) PLUS optional auto-sync from the org's configured payment/banking provider via the new `PaymentAdapter.get_balance` capability (`services/cashflow.fetch_provider_balance`; best-effort, falls back to BYO on unsupported/failure; mock returns a deterministic balance for local-first dev). A real banking-aggregator feed (Plaid-style) is the only deferred piece.
- [x] Persisted alert thresholds — `Organization.settings.cashflow.min_balance_threshold` via `GET/PUT /api/analytics/cash-position-settings` (no migration), read by `cash_position` when the request passes no override.
- [x] Export forecasts for CFO reporting (CSV via `/api/analytics/export/cashflow_forecast`)

### Sanctions & Vendor Risk Screening
**Status:** Shipped — screening on vendor create/update, periodic re-screen sweep, payment-block gate, adverse-media support, composite risk scoring, Dow Jones/Refinitiv/ComplyAdvantage adapter skeletons, the append-only screening trail, and the dedicated review-queue page (`/vendors/screening`) all landed. Real-provider wiring (live keys) is the remaining deployment work.
**Open:** Live-provider wiring. The ComplyAdvantage / Dow Jones / Refinitiv adapters are fail-closed skeletons; `mock` is the local-first default and the screening path itself ships and is tested. *(a) blocked on provider keys.* Tracked in [followups.md](followups.md).

Tipalti, Coupa, Medius, and Basware all screen vendors against sanctions lists. Required for financial services, government contractors, and regulated industries. See `backend/docs/vendor-risk-screening.md`.

- [x] OFAC/SDN sanctions screening on vendor creation and update — `services/vendor_screening.screen_vendor_record` runs on `POST`/`PATCH /api/vendors` (best-effort, savepoint-isolated so a provider failure never blocks the vendor write) + manual `POST /api/vendors/{id}/screen`. Gated by `FEOH_VENDOR_SCREENING_ENABLED` (default on; mock-safe local-first).
- [x] Ongoing monitoring — re-screen vendors periodically (daily/weekly) — `services/vendor_rescreen.py` background sweep (mirrors `contract_renewal`): re-screens active vendors whose `last_screened_at` is stale per `FEOH_VENDOR_RESCREEN_AFTER_DAYS`. Disabled by default (`FEOH_VENDOR_RESCREEN_ENABLED`).
- [x] Flag and block payments to sanctioned entities — a `match` sets `vendors.payments_blocked`; `check_payment_compliance` refuses a blocked vendor up front (before FX lock / any adapter call). Manual `POST /api/vendors/{id}/block` \| `/unblock`.
- [x] Adverse media screening — `ScreeningResult.categories` (`("adverse_media",)`) on all four adapters, folded through `services/sanctions_categories` into the compliance verdict reasons, the persisted `sanctions_checks` row, the `vendor.screened` audit row, the vendor risk sub-score (floored above a bare `review_required`), the screening-trail + review-queue API, and a "Negative news" pill on `/vendors/screening`. A `clear` verdict carrying negative news becomes a `hold` (fail-closed) — see [decisions.md](decisions.md) §34.
- [x] Vendor risk scoring (sanctions + fraud signals + payment history) — `services/vendor_risk_scoring.py` blends latest sanctions check + open `fraud_flag` exceptions + trailing-12m payment history into a 0–100 composite + bucket (PII-free factors). `GET /api/vendors/{id}/risk`, `POST .../risk/recompute`, `GET /api/vendors/risk/summary`.
- [x] Integration with screening providers (Dow Jones, Refinitiv, ComplyAdvantage) — `sanctions_adapters/`: `mock` (default), `complyadvantage`, `dowjones`, `refinitiv` (skeletons — live key required, fail-closed without one). Selected per-org via `Organization.settings.compliance.sanctions.provider`.
- [x] Screening audit trail — log all checks and results — append-only `sanctions_checks` (every screen: initial/periodic/manual/pre_payment) + PII-free `vendor.screened` audit rows; `GET /api/vendors/{id}/screening-history` + `GET /api/vendors/screening/review-queue`.

**Competitors:** Tipalti (OFAC/SDN built-in), Coupa (community risk), Basware (sanctions + fraud module), Medius (fraud intelligence)

---

### Automated E-Invoicing
**Status:** Inbound + outbound UBL 2.1 shipped (parse + generate, auto-detect, schema validation, country VAT/GST/IVA tax validation); PEPPOL AS4 **outbound send AND inbound receive** shipped (hosted Access Point adapter, mock default, idempotent transmission log; inbound webhook dedupes redeliveries by AS4 MessageId and routes to the einvoice extractor). Country-specific outbound formats — **FatturaPA (IT), CFDI 4.0 (MX), NF-e (BR), DIAN (CO)** — shipped as pure local-first generators + national validation, wired into the export route via `?format=` and registered behind `e_invoice/country_formats/`; **live government clearance** (SdI / SAT-PAC / SEFAZ / DIAN authorization) is the one remaining deferral, tracked per-format below.
**Open:** Live government clearance — SdI (IT), SAT-PAC (MX), SEFAZ (BR), DIAN (CO). Generators and national validation ship as pure local-first code. *(a) blocked on per-country registration.* Tracked in [followups.md](followups.md).

Support structured electronic invoice formats required in the EU, Australia, and other regions. Inbound parsing is pure/local-first (no network, no SaaS key) and on by default; see `backend/docs/e-invoicing.md`.

- [x] Factur-X / ZUGFeRD — hybrid PDF/XML format (EU standard): embedded CII XML extracted from PDF/A-3 and parsed
- [x] UBL (Universal Business Language) 2.1 — **parse + generate** (PEPPOL BIS Billing 3.0 payload). `generate_ubl(doc) -> bytes` is the exact inverse of the parser; round-trip property `parse_ubl(generate_ubl(doc)) == doc` holds on core fields
- [x] Auto-detect format on upload — structured data parsed instead of OCR (`extraction.run_extraction` choke point routes to the `einvoice` adapter at confidence 1.0)
- [x] Validate against schema — malformed e-invoices rejected with clear field-level errors (EN 16931 structural subset)
- [x] UBL 2.1 **generate** (outbound) — reuses `EInvoiceDocument` via `mapper.invoice_to_einvoice_document`; `GET /api/invoices/{id}/einvoice` (role-gated AP export, 422 on tax-invalid) + `GET /portal/invoices/{id}/einvoice` (vendor-scoped supplier download).
- [x] CII (UN/CEFACT Cross-Industry Invoice, D16B) **generate** (outbound) — `e_invoice/generate_cii.py::generate_cii(doc) -> bytes`, the exact inverse of the CII parser (`cii.py`); emits `rsm:CrossIndustryInvoice` with the `ram:`/`udt:` namespaces (ExchangedDocument + SupplyChainTradeTransaction). Round-trip `parse_cii(generate_cii(doc)) == doc` on core fields. Wired into the AP export route as `?format=cii` (built-in path alongside `ubl`, shares the same `assert_valid` 422-on-tax-invalid guard, format-tagged filename). Pure / local-first, no model change. This is the Factur-X / ZUGFeRD payload dialect — embedding the CII into a PDF/A-3 is the one remaining deferral (own slice; trigger: a corridor that mandates the hybrid PDF, not bare CII XML).
- [x] Peppol BIS Billing 3.0 — **receive and send** via Peppol network shipped. **Send:** `POST /api/invoices/{id}/peppol-send` (reuses the UBL generator; `PEPPOL_BIS_BILLING_DOCTYPE`/`PROCESSID` constants). **Receive:** `POST /api/peppol/inbound/{tenant_slug}` (public-by-design, HMAC-gated webhook; dedupes redeliveries by AS4 MessageId via the `uq_peppol_message_id` index; parses the inbound UBL with the existing `e_invoice` parser, creates the Invoice, and hands to `dispatch_extraction` → the `einvoice` adapter). Reuses the `PeppolTransmission.direction`/`message_id` columns, `ParticipantId`, and `webhook_security`
- [x] FatturaPA — Italian e-invoicing format. `e_invoice/country_formats/fatturapa.py` generates the `FatturaElettronica` v1.2 (`FPR12`) document (DatiTrasmissione + Cedente/Cessionario header, DatiGeneraliDocumento/DatiBeniServizi/DatiRiepilogo body) + national validation (seller **and** buyer Partita IVA required). *Deferred: SdI transmission + the `.p7m` (CAdES) digital signature — own slice; trigger: first IT customer going live.*
- [x] CFDI 4.0 — Mexican e-invoicing. `country_formats/cfdi.py` generates `cfdi:Comprobante` v4.0 (Emisor/Receptor RFC, Conceptos, Impuestos) + national validation (emisor **and** receptor RFC required). *Deferred: SAT-PAC stamping → `Sello`/`Certificado`/`tfd:TimbreFiscalDigital` UUID (folio fiscal) — own slice; trigger: first MX customer going live.*
- [x] NF-e / NFS-e — Brazilian electronic invoicing. `country_formats/nfe.py` generates `NFe/infNFe` v4.00 (ide/emit/dest/det·prod/total·ICMSTot) + national validation (emit CNPJ required). *Deferred: SEFAZ authorization → 44-digit chave de acesso + protocolo + digital signature (a deterministic placeholder `Id` is emitted meanwhile); municipal NFS-e schema — own slice; trigger: first BR customer going live.*
- [x] DIAN — Colombian e-invoicing. `country_formats/dian.py` generates DIAN-profiled UBL 2.1 (`CustomizationID=10`, DIAN `ProfileID`, `UBLExtensions` placeholder) + national validation (supplier NIT required). *Deferred: CUFE + XAdES signature + `dian:DianExtensions` injected at clearance — own slice; trigger: first CO customer going live.*
- [x] Access point / PEPPOL AS4 gateway integration — **send and receive** shipped (`services/peppol_adapters/`: mock in-process default + `as4_gateway` real adapter talking to a hosted AP's HTTP API; SMP/SML resolution behind `resolve_participant`; SBDH wrapping in the adapter, not the generator). Inbound delivery: both adapters implement `parse_inbound`; the AP's inbound POST is verified (`FEOH_PEPPOL_INBOUND_SIGNING_SECRET`) and deduped at the receive webhook. See `backend/docs/peppol.md`
- [x] Country-specific tax validation (VAT, GST, IVA) — `e_invoice/tax_rules.py`: per-country tax-ID format (EU/GB VAT, AU ABN, NZ/IN/CA GST, MX/ES/IT IVA), rate plausibility per regime, zero-rate/reverse-charge handling. Pure, PII-free `FieldError`s; wired into inbound `validate_document` + the outbound export guard

---

### Accessibility (WCAG 2.2 AA / EU EAA / ADA)
**Status:** Shipped — WCAG 2.2 AA adopted as the conformance target across web, supplier portal, and Flutter mobile; baseline fixes landed across the shared component library + every route, automated guards (`axe-core` + a navigability/reflow/focus-trap spec on web, `meetsGuideline` widget tests on mobile) lock against regressions, and a conformance statement + VPAT/ACR are published. The structural follow-ups are all closed (shared `focusTrap` action on every dialog, keyboard step-reorder in the workflow builder, `autocomplete` tokens, 320px reflow). The one remaining item is the **manual screen-reader device pass** (VoiceOver / NVDA / TalkBack), now a documented repeatable procedure (`docs/accessibility-screen-reader-checklist.md`) — it needs real AT hardware so it can't run in CI. See `docs/accessibility.md` + `docs/accessibility-vpat.md`.
**Open:** The manual screen-reader device pass (VoiceOver / NVDA / TalkBack). The procedure is documented and repeatable; automated axe-core + `meetsGuideline` guards ship. *(a) blocked on real AT hardware — cannot run in CI.* Tracked in [followups.md](followups.md).

Legally required, not optional: the **EU Accessibility Act** is in force (June 2025), and US ADA Title III + Section 508 apply to enterprise buyers.

- [x] Adopt **WCAG 2.2 AA** as the conformance target across web (SvelteKit), mobile (Flutter), and the supplier portal; publish a VPAT/ACR — `docs/accessibility.md` (conformance statement) + `docs/accessibility-vpat.md` (VPAT 2.5 criterion table)
- [x] Web baseline — skip link + named landmarks, global `:focus-visible` ring, `Modal` focus trap/restore (no keyboard traps), form-label + error association (`aria-describedby`/`aria-invalid`), `aria-live` on async + toast surfaces, `prefers-reduced-motion` blanket, AA contrast on `StatusBadge`/`ScreeningBadge`/charts. Shared `lib/components/` carry the baseline so route pages inherit it
- [x] Audit-and-fix pass route by route (shared `lib/components/` first so fixes propagate); findings driven to closure. The two items first deferred are now **done**: (a) keyboard step-reorder in the workflow-builder canvas — per-node Move ↑/↓ buttons over `onreorder` (WCAG 2.5.7), covered by `workflow-builder.spec.ts`; (b) the four hand-rolled modal shells (`InvoiceModal`, `RunDetailModal`, `BulkRecodeGLModal`, portal `discount-offers`) now get shared focus trap/restore via the reusable `#lib/actions/focusTrap` action. Plus `autocomplete` tokens (1.3.5) and 320px reflow (1.4.10) closed + guarded
- [x] Automated regression guard — `axe-core` assertions wired into the Playwright e2e suite (`tests-e2e/a11y/`, auto-run in the standard glob so a regression fails CI) + Flutter `meetsGuideline` semantics/contrast/tap-target widget tests (`mobile/test/a11y/`)
- [x] **Stylesheet-wide colour-token/contrast guard** — `frontend/src/lib/a11y/tokenPairing.test.ts` (vitest, no browser) scans every `.css`/`<style>` in `src/` for a same-rule pair below SC 1.4.3, a `var(--token, fallback)` contradicting its token, and a `var()` on a token nothing assigns. Added because a contrast failure recurs per *surface*, not per route, so the axe route list kept catching one instance and missing its twin. Closing it introduced `--success{,-strong}` / `--danger{,-strong}` beside `--accent{,-strong}` and fixed 99 findings; `/billing`, `/reports`, `/experiments`, `/cfo` joined the axe list in the same pass. Runtime brand overrides get an inline contrast advisory on `/organization` + `/admin/partner`. A follow-up pass extended it to a rule's own `opacity` — which composites text toward the backdrop, invisibly to both earlier checks — after `/cfo` joining the axe list caught `.kpi-sub` at 4.24:1; all 11 instances were a redundant fade over already-muted text. Translucent-tint badges are the adjacent class, measured but deferred (`docs/followups.md`). See `docs/decisions.md` §28
- [x] `prefers-reduced-motion` respected (global app.css rule + component-scoped guards; mobile uses default Material transitions which honor the platform setting). Manual **screen-reader pass** (VoiceOver / NVDA / TalkBack) on the invoice → approve → pay flow + supplier portal is the tracked outstanding VPAT item (the supporting semantics — labels, roles, live regions — are in place)

**Competitors:** enterprise suites (Coupa, SAP Ariba, Basware) ship VPATs; a clean ACR is increasingly a procurement gate, especially for public-sector + EU buyers

---

## Priority 14: Receiving, ERP depth & commerce sync
**Competitive gap: 3-way matching needs receipts, and a merchandise buyer's stock lives in a commerce platform**

Scope boundary: FeohLedger records what was **ordered, received, billed and
paid**. Stock on hand, valuation, and selling to customers (POS, sales orders,
receivables) are a different product and are **not** being built here — a
business like a sports retailer runs its POS / inventory system *beside* the AP
tool, and item 3 below is how the two meet. Reasoning: [decisions.md](decisions.md) §260.

Each item ships as **its own PR**. Item 1 (goods-receipt entry) has shipped — see
[roadmap_shipped.md](roadmap_shipped.md) § Priority 14.

### 2. Direct ERP adapters
**Status:** Partial. Direct adapters cover the top ERPs in the US and South Africa: QuickBooks Online, Xero, Sage Business Cloud Accounting (global v3.1 and the separate South Africa API), Sage Intacct, SYSPRO, NetSuite, Business Central and Blackbaud Financial Edge NXT. They are set up from a catalogue-driven panel (decisions §264–§266; shipped in [roadmap_shipped.md](roadmap_shipped.md) § Priority 14). Merge.dev stays the route for everything else.
**Open:** a receipt pull on the adapter interface, and direct adapters for the large enterprise ERPs. *(c)* Tracked in [followups.md](followups.md).

- [ ] Extend the `erp_adapters` interface with a goods-receipt pull, next to the PO sync, so an ERP that owns receiving feeds the 3-way match (Business Central + NetSuite first)
- [ ] SAP S/4HANA (OData) · Oracle Fusion Cloud ERP · Microsoft Dynamics 365 Finance & Operations · Infor CloudSuite · Epicor Kinetic — each with a fake-erp route set (`pnpm erp:up`) and a `tests-e2e/erp/` spec, mock-by-default (guard rail 7). Until one lands, these ride Merge.dev on Scale (decisions §256)

### 3. Commerce / inventory platform adapter (Shopify first)
**Status:** Planned
**Open:** A new adapter family that pushes received merchandise into the business's commerce / inventory system. Builds on item 1 (receipts, shipped) and needs PO / receipt lines to carry an item identity (SKU). *(c)* Tracked in [followups.md](followups.md).

- [ ] Item identity on PO and receipt lines — link to `catalog_items` (SKU) so a received quantity names *what* arrived, not just free text
- [ ] `commerce_adapters` family (`mock` default, registry + decorator like every other family): on a goods receipt, adjust the mapped SKU's available quantity at the mapped location; idempotent per receipt line
- [ ] Shopify (Admin GraphQL inventory adjust) first; then Lightspeed, Square, Cin7, WooCommerce
- [ ] Per-org SKU ↔ product-variant and location mapping; a local fake for e2e; sub-processor registers updated (`pnpm check:subprocessors`)

---
