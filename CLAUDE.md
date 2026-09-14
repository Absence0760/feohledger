# CLAUDE.md

Guidance for Claude Code working in this repository. Keep this file short — it loads into every conversation.

## How the guidance files are split

**A `CLAUDE.md` holds rules; a sibling `docs/` holds reference material.** The
four `CLAUDE.md` files load automatically — the root one into *every*
conversation, the per-area ones whenever you touch that tree — so every byte in
them is paid for on every turn whether or not it gets used. Catalogues (every
router, every component, every env var, every adapter, every screen) are read on
demand instead, from `docs/`, `backend/docs/`, `frontend/docs/` and
`mobile/docs/`.

So when you add something here:

- A **rule, invariant, or gotcha** that changes how code gets written — it goes
  in the `CLAUDE.md`, stated once, as briefly as it can be stated.
- An **enumeration, table, or per-item detail** — it goes in the matching
  `docs/` file, and the `CLAUDE.md` gets at most a pointer.
- If a section here grows past roughly a screenful, that is the signal it has
  turned into reference material. Move it and leave the pointer.

Guard rail 12 (docs-as-code) applies to both halves: a behaviour change updates
whichever of them describes it, in the same turn.

## Project

Full-stack accounts payable management app. SvelteKit frontend + FastAPI backend with multi-tenant (database-per-tenant) architecture. Features: invoice extraction (AI/OCR), workflow engine, ERP integration, payment runs, virtual cards, exception tracking.

## Stack

- **frontend/** — SvelteKit 2, Svelte 5 (runes), adapter-static, TypeScript, pnpm. Dev port `7777`.
- **backend/** — FastAPI, Python 3.12+, SQLAlchemy 2 async, Alembic, PostgreSQL 16, Redis 7, MinIO (S3). Dev port `8000`.
- **mobile/** — Flutter 3.41+, Dart 3.11+, iOS + Android. Material 3, ChangeNotifier stores.
- **infra/** — Terraform skeleton for future AWS resources; SOPS-encrypted tfvars. See `infra/README.md`.
- **Local infra** — Docker Compose for Postgres/Redis/MinIO. GitHub Pages for frontend deploy.
- **Secrets** — `backend/.env.sops` + `infra/terraform.tfvars.sops`, both AWS KMS-encrypted via SOPS. **This repo is PUBLIC and no encrypted payload is committed yet.** Do NOT bootstrap the in-repo sops (`./bin/sops-init.sh`) and commit `*.sops` here — that would put ciphertext in public history (the mistake meryl-green-designs made). Instead adopt the private estate secrets repo: `Absence0760/infra-secrets` (per-project subdir + KMS). Pattern + onboarding: `~/github/project-mgmt/docs/secrets-management.md`. See `backend/CLAUDE.md` → Secrets management for the local-dev flow.

## Commands

The repo root has a `package.json` with `pnpm` dispatch scripts that wrap each workspace's native toolchain — `pnpm run` lists them. The native commands still work, and CI calls them directly.

```bash
# Common tasks via root pnpm scripts (any working directory)
pnpm install:all              # bootstrap all three workspaces
pnpm db:up                    # core services: Postgres + Redis + MinIO (docker compose up -d)
pnpm idp:up                   # local IdPs (opt-in `idp` profile): Keycloak (OIDC SSO, :8088) + Authentik (SCIM, :9002)
pnpm idp:seed                 # point the acme tenant's settings.sso at local Keycloak (OIDC SSO)
pnpm saml:seed                # point acme's settings.sso at local Keycloak via SAML (protocol=saml; replaces OIDC block)
pnpm test:saml                # SAML SSO e2e (tests-e2e/saml/) — real Keycloak handshake
pnpm scim:seed                # set acme's SCIM bearer token to match the Authentik blueprint (SCIM)
pnpm test:scim                # SCIM provisioning e2e (tests-e2e/scim/)
pnpm aws:up                   # local AWS emulator (LocalStack :4566, opt-in `aws` profile): SQS/SES/CloudWatch/S3-ObjectLock
pnpm ollama:up                # local AI model server (Ollama :11435, opt-in `ai` profile) for the ollama extraction adapter
pnpm stripe:up                # Stripe API mock (stripe-mock :12111, opt-in `payments` profile) for the stripe_treasury adapter
pnpm mail:up                  # Mailpit SMTP sink + web inbox (:1025/:8025, opt-in `mail` profile) for the smtp email adapter
pnpm erp:up                   # fake ERP server (fake-erp :12112, opt-in `erp` profile) for merge_dev/netsuite/dynamics_365_bc adapter e2e (erp:down / erp:logs too)
pnpm test:erp                 # ERP adapter e2e (tests-e2e/erp/) — real HTTP against fake-erp; skips if it's down
pnpm services:up              # everything at once: core + IdPs + LocalStack + Ollama + stripe-mock + Mailpit + fake-erp (services:down / services:logs / services:reset too)
pnpm seed                     # python scripts/seed.py
pnpm dev                      # backend (:8000) + frontend (:7777) together, one Ctrl-C stops both
pnpm dev:all                  # db:up (core only), then pnpm dev (whole web stack from cold)
pnpm dev:full                 # services:up (core + every opt-in profile), then pnpm dev — the entire stack from cold
pnpm dev:backend              # python main.py (loads backend/.env.development, then .env override)
pnpm dev:frontend             # vite dev on :7777
pnpm dev:mobile               # flutter run (needs a device/emulator — not part of `pnpm dev`)
pnpm lint                     # ruff + svelte-check + tsc over tests-e2e/ + flutter analyze
pnpm test                     # pytest + Playwright + flutter test
pnpm gen:einvoice-messages    # regenerate the e-invoice rule-code → message-key catalogue from the backend rule set
pnpm check:einvoice-messages  # its drift guard (CI's Backend lint job runs this)
pnpm gen:warning-messages     # regenerate the invoice-warning code → message-key catalogue from the backend catalogue
pnpm check:warning-messages   # its drift guard (same CI job; see backend/docs/invoice-warnings.md)
pnpm migrate:all              # alembic upgrade head + migrate_all_tenants.py

# Frontend (from frontend/)
pnpm i                       # install
pnpm dev                     # dev server on :7777
pnpm build                   # production build
pnpm check                   # typecheck (src/ only)
pnpm check:e2e               # typecheck tests-e2e/ (svelte-check skips it — frontend/tsconfig.e2e.json)

# Backend (from backend/)
docker compose up -d          # start Postgres, Redis, MinIO
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"       # install with dev deps
python scripts/seed.py        # seed demo data
python main.py                # dev server on :8000 (auto-reload)

# Backend testing & linting
pytest                        # run tests
ruff check .                  # lint
ruff format .                 # format

# Mobile (from mobile/)
flutter pub get               # install
flutter run                   # run on iOS simulator
flutter analyze               # lint
flutter test                  # run tests

# Database migrations (from backend/)
alembic revision --autogenerate -m "description"   # create migration
alembic upgrade head                                # apply to control plane
FEOH_MIGRATE_TENANT=feoh_acme alembic upgrade head      # apply to one tenant
python scripts/migrate_all_tenants.py               # apply to all tenants
```

The backend dispatch scripts (`lint:backend`, `test:backend`, `format:backend`, `dev:backend`, `seed`, `migrate*`) assume the backend venv is activated — `source backend/.venv/bin/activate` before invoking them, or call the native commands from inside an already-activated shell.

## Root package.json scripts — estate format

Root `scripts` follow the estate-wide format canonized in the templates repo's `base` CLAUDE.md; the exemplar is `project-running/package.json` — read it before restructuring this repo's scripts:

- `"//-- <group> --": "<one-line description>"` comment-key dividers above each cluster; the description carries load-bearing facts (ports, prerequisites, doc pointers), not filler.
- Verb-first, colon-namespaced names: `setup[:*]`, `dev:*` (orchestrators, then `dev:db:*`, `dev:run:<app>`, per-service groups), `build:<surface>`, `check:<surface>`, `test:<surface>[:unit|:e2e]`, `gen:<what>`. Long-running services reuse the lifecycle verbs `up`/`down`/`status`/`logs`.
- JSON holds one-liners only — anything longer delegates to a script under `bin/` or `scripts/`; workspace delegation goes through `pnpm -C <workspace> <script>`.
- New scripts join an existing group (or add a new `//--` divider in the right place); never append ungrouped entries at the bottom.
- Keep a `test:scripts` guard validating the root script targets (project-running's `scripts/check_root_scripts.mjs` is the reference shape; write it against this repo's layout).

If the current scripts block predates this format, migrate it the next time a change touches it — as its own commit, and renaming a script must update every caller (CI workflows, docs, `bin/`) in the same change.

## First-time setup

1. `cd backend && docker compose up -d`
2. `python3 -m venv .venv && source .venv/bin/activate`
3. `pip install -e ".[dev]"`
4. `python scripts/seed.py` — creates 2 demo tenants with sample data
5. `cd ../frontend && pnpm i`
6. Open http://acme.localhost:7777 — login: `demo@acme.com` / `demo`

No `.env` setup at all: `backend/.env.development` and
`frontend/.env.development` are **committed** with safe, no-risk local defaults
(loopback URLs, mock adapters, the `change-me` JWT key, MinIO's
minioadmin/minioadmin), so a fresh clone runs immediately. The backend loads
them via `main.py` (local-dev entrypoint only); the frontend loads
`.env.development` natively in Vite dev mode. Personal overrides go in a
gitignored `backend/.env` / `frontend/.env.local` and win over the committed
defaults. Deployed secrets stay in the `*.sops` files — never in any `.env*`.

## Multi-tenancy

- **Control plane DB** (`feohledger`): organizations, users, roles
- **Tenant DBs** (`feoh_<slug>`): invoices, vendors, payments, workflows, etc.
- Frontend extracts subdomain → sends `X-Tenant-Slug` header → backend resolves tenant DB
- Provision: `python scripts/create_tenant.py --name "Corp" --slug corp --admin-email admin@corp.com --admin-password changeme`

## Architecture overview

### Backend routers (all under `/api`)

One line each. **The full catalogue — per-route RBAC, invariants, and the
reasoning behind each design call — is `backend/docs/api-surface.md`.** Read that
before changing any router; the summaries below are only an index.

| Prefix | Purpose |
|--------|---------|
| `/auth` | Login, logout, profile, MFA (TOTP + passkey), step-up, per-account failure budgets |
| `/auth/sso`, `/auth/saml` | OIDC + SAML 2.0 SSO — config, authorize, callback/ACS, JIT provision |
| `/scim/v2` | SCIM 2.0 user provisioning (Okta/Entra/Authentik), per-tenant bearer auth |
| `/portal`, `/portal/auth` | Supplier portal — VendorUser auth, invoice submit/resubmit, payments, self-service, MFA |
| `/admin` | User CRUD, role assignment |
| `/organization` | Org settings, connection tests, SCIM token, branding, custom domains, data residency |
| `/entities` | Multi-entity admin (legal entities / subsidiaries — the `entity_id` scope target) |
| `/partner` | Partner / reseller multi-tenant admin — child tenants, link codes, provision |
| `/invoices` | Invoice CRUD, upload, extraction, approve/reject, line items, e-invoice, email approval |
| `/vendors` | Vendor CRUD, sanctions screening, risk, bulk ops; bank changes are dual-control |
| `/vendor-statements` | Vendor statement reconciliation (CSV/PDF) against our AP ledger |
| `/bank-reconciliation` | Bank statement import + auto-match against our `Payment` rows |
| `/positive-pay` | Positive Pay / payment-fraud files (check-issue, ACH authorization) |
| `/payments` | Payment listing, runs, settlement verification, compliance holds, ERP sync-back |
| `/discounts` | Dynamic discounting — offers, ROI, optimizer, bulk vendor negotiation |
| `/recurring` | Recurring / subscription invoice templates + generation sweep |
| `/cards` | Virtual card issuance (Lithic/Nium), webhooks, rebates |
| `/contracts` | Contract lifecycle (CLM) — CRUD, documents, lifecycle, contract-based PO |
| `/expenses`, `/expense-reports` | Expense CRUD + receipts; report submit/approve/reject, locked-FX multi-currency |
| `/expense-policies`, `/expense-preapprovals` | Reimbursement policy CRUD (currency-denominated thresholds); spend pre-approvals |
| `/corporate-card-transactions` | Corporate-card import + reconciliation, match suggestions, create-expense |
| `/intake`, `/requisitions` | Procurement — non-PO spend intake forms; purchase requisitions → PO |
| `/catalogs`, `/budgets` | Procurement — supplier catalogs + punch-out; budget tracking (spend computed on read) |
| `/purchase-orders`, `/goods-receipts` | PO + goods-receipt list/detail (3-way match feeders). Read is role-open |
| `/gl-accounts` | GL account CRUD, ERP sync |
| `/credit-memos` | Credit-memo list / create / apply / void. Fail-closed on vendor + currency |
| `/tax`, `/international-tax` | 1099 tracking (card rails excluded); VAT / GST / withholding + period report |
| `/analytics`, `/reports` | CFO aggregates, exports, cash-flow forecast, scheduled reports; ad-hoc report builder |
| `/assistant`, `/cash-flow` | Conversational AP assistant; AI Cash-Flow Copilot (plans, draft runs, variance) |
| `/workflows`, `/adaptive`, `/experiments` | Workflow definition CRUD + no-code builder; pattern learning; A/B testing of rules |
| `/audit` | SOX auditor export + approval-signature verification (per-invoice and per-period) |
| `/exceptions` | Exception queue, resolution, autonomous AI agents + decision log |
| `/notifications` | In-app notification center, preferences, mobile push device tokens |
| `/privacy`, `/retention-policy` | GDPR/CCPA DSAR + erasure; SOX per-record-class retention config |
| `/dashboard` | KPI aggregates (pipeline, aging, spend, trends) |
| `/billing` | Platform billing & metering — the AP platform's OWN customer billing (control-plane) |
| `/erp`, `/email-intake`, `/peppol` | Inbound webhooks — ERP status, email-to-invoice, PEPPOL AS4 receive |
| `/approvals/slack`, `/approvals/teams` | Interactive approval from a chat message — public, HMAC + single-use action token |
| `/organization/email-intake`, `/signup` | Per-tenant intake address; self-service tenant signup |
| `/v1`, `/api-keys`, `/webhooks` | Public Developer API (`X-API-Key`) + published OpenAPI; API-key mgmt; outbound webhooks |
| `/health` | Health check (public, static) + `GET /health/sweeps` (admin-gated) |

### Key services (`backend/app/services/`)

Per-module responsibilities are in **`backend/docs/services-map.md`**. The ones
worth knowing before you touch anything money-adjacent:

- `workflow_engine.py` — the invoice state machine; `VALID_TRANSITIONS` is the authoritative graph.
- `review.py` — approve/reject with field corrections; segregation of duties and the CFO gate.
- `payment_runs.py` / `payment_erp_sync.py` / `payment_settlement.py` — run creation, the ERP sync-back that flips to `paid`, and settlement-amount verification.
- `payment_methods.py` — the single source of truth for what a payment rail means (tax-reportable? international?). Adding a rail means editing one frozenset.
- `invoice_warnings.py` — duplicates, fraud flags, line-total reconciliation. The header `amount` is never recomputed from line items. Every finding is built by `invoice_warning_catalog.warning(code, severity, **params)`, never a dict literal, so it carries the `{code, params}` the browser localizes on (`backend/docs/invoice-warnings.md`).
- `po_matching.py` + `matching_rules.py` — 2/3/4-way matching and the per-vendor/per-commodity rule resolver.
- `vendor_matching.py` — fuzzy vendor resolution, scoped to the invoice's own entity.
- `post_commit.py` — best-effort side effects run **after** the caller's transaction commits, so no third party's latency is charged to an open transaction holding row locks.

### Adapter patterns (pluggable providers)

Every external integration is a registry-based adapter with a `mock` default, so
the whole app runs with no cloud account (guard rail 7). Families:

`extraction_adapters` · `erp_adapters` · `card_adapters` · `payment_adapters` ·
`positive_pay_adapters` · `audit_shipping` · `fx_adapters` ·
`financing_adapters` · `qms_adapters` · `sanctions_adapters` ·
`email_adapters` · `chat_notification_adapters` · `email_intake_adapters` ·
`embedding_adapters` · `peppol_adapters` · `billing_adapters` ·
`enrichment_adapters`

**Read `backend/CLAUDE.md` § Adapter patterns before adding or changing one** —
it lists every provider per family, which are real vs fail-closed skeletons, and
the per-org settings key that selects them. To add one: copy `mock_adapter.py`,
implement the interface, register with the decorator.

### Dispatch modes

Extraction, ERP, and audit operations support two execution modes via config:
- `local` (default) — jobs queued in-process; pool of 3 worker threads drains the queue (engines use `pool_size=1, max_overflow=0` to stay under PostgreSQL's connection limit)
- `lambda` — sends to SQS, processed by Lambda worker

Controlled by: `FEOH_EXTRACTION_MODE`, `FEOH_ERP_MODE`, `FEOH_AUDIT_MODE`

### Invoice workflow state machine

```
new → pending → ready_for_review → approved → sending_to_erp → sent_to_erp → posted_in_erp → payment_scheduled → paid → done
                      ↕ rejected ↔ new                                   ↘                                  ↑
                                    failed → pending | sending_to_erp     approved ----------------- (direct schedule, no ERP)
                                                                          ↑ (void) ←──────────────────────────────┘
```

Terminal state: `done`. Step types: `extraction`, `approval`, `erp_export`, `done`.
Workflow definitions are snapshotted per-invoice — editing a definition does not affect in-flight invoices.
The void-payment path (`POST /api/payments/{id}/void`) takes `payment_scheduled` or `paid` back to `approved` so the invoice re-enters the queue. Authoritative graph: `backend/app/services/workflow_engine.py::VALID_TRANSITIONS`.

### Data models

**Control plane**: Organization, User, Role, UserRole, AssistantUsage, ApiKey, ApiKeyUsage, Plan, Subscription, WebhookSubscription, WebhookDelivery, WebAuthnCredential
**Tenant-scoped**: Entity, Invoice, InvoiceLineItem, InvoiceExtractionResult, Vendor, VendorChangeRequest, PurchaseOrder, POLineItem, GoodsReceipt, GRLineItem, QualityInspection, GLAccount, PaymentRun, PaymentSchedule, Payment, VirtualCard, CardRebate, ExtractionUsage, WorkflowDefinition, WorkflowInstance, WorkflowStep, AuditLog, Exception, AgentDecision, Notification, Contract, ContractLineItem, SupplierChatThread, SupplierChatMessage, ExpenseReport, Expense, ExpensePolicy, CorporateCardTransaction, ExpensePreapproval, DiscountOffer, RecurringInvoiceTemplate, VendorStatementReconciliation, VendorStatementReconLine, WorkflowExperiment, CashPlan

**Multi-entity**: business tables (Invoice, Vendor, PurchaseOrder, GoodsReceipt, Payment, PaymentRun, CreditMemo, Exception, GLAccount, WorkflowDefinition, VirtualCard) carry a nullable `entity_id` FK (`EntityMixin`) to the tenant-local `Entity` (subsidiary). Every tenant has one `is_default` Entity; rows backfill to it (GLAccount stays NULL = shared chart). Phase 2 + 2b scope reads/writes (incl. the dashboard + CFO analytics) by the `X-Entity-ID` header (`app/tenant.py` → `get_entity_id` / `get_write_entity_id` / `apply_entity_scope`) with a sidebar entity switcher. Phase 3 wires the entity-level chart of accounts (shared NULL ∪ entity) into the AI extraction GL catalog + bulk-recode validation and selects the entity's own `WorkflowDefinition` (shared fallback; one default per `(org, entity)` via `uq_workflow_definitions_one_default`, migration 0050). Phase 4 adds inter-company invoice routing (`counterparty_entity_id` / `intercompany_mirror_id`, migration 0051) + cross-entity consolidated reporting (`GET /analytics/by-entity`). Multi-entity is **complete** (Phases 1–4). See `docs/multi-entity.md`.

### RBAC roles

`admin`, `ap_manager`, `ap_clerk`, `cfo` — checked in both backend (deps.py) and frontend (auth store).

**Granular permissions (SoD).** On top of roles, fraud-sensitive *splittable* duties are gated by a granular permission layer (`backend/app/api/permissions.py`): a small catalog (`invoice.approve`, `payment_run.approve`, `payment.execute`, `payment.void`, `vendor.bank_change.approve`, `vendor.block`, `vendor.manage`, `user.manage`), a static system-role→default-permissions map reproducing the prior matrix exactly, and a control-plane `roles.permissions` JSONB column (migration `0062`, control-plane-only) so **custom roles can grant access** and an org can split duties. Effective permissions = union over the user's roles (system via the map, custom via the stored list), computed in `get_current_user`, exposed on `GET /api/auth/me`'s `permissions`, enforced by `require_permission(*perms)` and `auth.can(perm)`. Only the splittable sensitive endpoints (payment execute/void, run approve, vendor bank-change approve, vendor block/unblock + manage, user management) moved to `require_permission`; everything else stays on `require_roles`. See `docs/authentication.md` § Granular permissions / segregation of duties.

**Approver ≠ creator keys on a SET: `Invoice.uploaded_by_id` ∪ `Invoice.segregation_actor_ids`.** `approval_chain.violates_segregation` refuses the uploader **or** anyone in that set, and returns False — no breach — only when the column is NULL *and* the set is empty, which it reads as "no employee created this row". That is only sound because **every path under `app/` that creates an invoice for a signed-in employee stamps the uploader**: manual create, file upload, CSV import (`POST /api/invoices/import-csv`), recurring `generate-now`, and the inter-company mirror (stamped with the routing actor). **The recurring *sweep* stamps too** — nobody runs it, but an employee authored the template, so `recurring_invoice_templates.created_by_user_id` (migration 0096) records that author and `generate_one` falls back to it. **One column names one person, and a template can be shaped by several**: whoever materially PATCHes it (vendor, amount, currency, GL coding, schedule — `models/recurring_invoice.MATERIAL_EDIT_FIELDS`, the single classification) joins `material_editor_ids`, and `generate_one` stamps author ∪ editors onto `segregation_actor_ids` (migration 0097) — stamping the editor *instead* would only have moved the exemption to the author (`docs/decisions.md` §141, §152). Nothing written before either migration is backfilled: no honest author or editor exists to recover and every proxy manufactures either a refusal or an absolution. The remaining NULLs are the paths with no control-plane user at all — email intake and inbound PEPPOL (system), and supplier-portal submit / PO flip (a tenant-scoped `VendorUser`, who holds no employee JWT and can never reach an approval endpoint). Failing CLOSED on NULL was rejected: it would make those three ingestion channels permanently unapprovable. `backend/tests/test_invoice_uploader_stamping.py` is the enforcement — a new `Invoice(...)` site must pass `uploaded_by_id` explicitly and a literal `None` must be declared with its reason; `segregation_actor_ids` is pinned only at the one site that has a template behind it, since nowhere else has a second actor to name.

## Key environment variables (`FEOH_` prefix)

Backend config is `pydantic-settings` in `backend/app/config.py` — **that file is
authoritative**. The annotated table of every feature flag, adapter selector and
sweep interval, with the reasoning behind each default, is
`docs/environment.md` § Feature flags, adapters and background sweeps.

The handful that shape local dev:

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_DATABASE_URL` | `postgresql+asyncpg://...localhost:5432/feohledger` | Control-plane DB |
| `FEOH_SECRET_KEY` | `change-me-in-production` | JWT signing (HS256) |
| `FEOH_REDIS_URL` | `redis://localhost:6379` | Token blocklist |
| `FEOH_S3_ENDPOINT_URL` | `http://localhost:9000` | MinIO/S3 |
| `FEOH_EXTRACTION_MODE`, `FEOH_ERP_MODE`, `FEOH_AUDIT_MODE` | `local` | `local` (in-process workers) or `lambda` (SQS) |

Two conventions hold across the whole set, and a change that breaks either is a
defect:

- **Every external integration defaults to its `mock` adapter** and every sweep
  that emails, charges, or calls out defaults to *off* — that is what makes
  guard rail 7 (local-first) true. A new flag defaults to the safe local value.
- **A secret has no hardcoded fallback.** Empty means the feature is off and
  fails closed, never a default that silently works (see `## Project
  invariants`). Non-secret dev values may be committed in `.env.development`.

## Where to look

Prefer reading docs over guessing. Update them when behavior changes.

| Topic | Read this |
|-------|-----------|
| Frontend details | `frontend/CLAUDE.md` + `frontend/docs/` |
| Backend API surface (per-router detail) | `backend/docs/api-surface.md` |
| Backend services map | `backend/docs/services-map.md` |
| Backend details | `backend/CLAUDE.md` + `backend/docs/` |
| Mobile app | `mobile/CLAUDE.md` + `mobile/docs/` |
| AI extraction | `backend/docs/ai-extraction.md` |
| Structured e-invoicing (in + outbound) | `backend/docs/e-invoicing.md` |
| Conversational assistant | `backend/docs/conversational-assistant.md` |
| ERP integration | `backend/docs/erp-integration.md` |
| Workflow design | `backend/docs/workflow-design.md` |
| Payments | `backend/docs/payments.md` |
| Virtual cards | `backend/docs/virtual-cards.md` |
| Dynamic discounting | `backend/docs/dynamic-discounting.md` |
| Recurring / subscription invoices | `backend/docs/recurring-invoices.md` |
| Vendor statement reconciliation | `backend/docs/vendor-statement-reconciliation.md` |
| Positive Pay / payment-fraud file | `backend/docs/positive-pay.md` |
| PO matching | `backend/docs/po-matching.md` |
| Line-total reconciliation | `backend/docs/line-total-reconciliation.md` |
| Invoice warnings (payload, code catalogue) | `backend/docs/invoice-warnings.md` |
| Vendor mgmt | `backend/docs/vendor-management.md` |
| Local AI testing | `backend/docs/local-ai-testing.md` |
| API reference | `backend/docs/api-reference.md` |
| DB / Redis / MinIO | `backend/docs/{database,redis,minio,docker}.md` |
| Auth & RBAC | `docs/authentication.md` + `docs/user-management.md` |
| Local SSO + SCIM testing | `docs/local-sso-keycloak.md` |
| Local SAML SSO testing | `docs/local-sso-saml.md` |
| Local AWS testing | `docs/local-aws-localstack.md` |
| Local email preview | `docs/local-email-mailpit.md` |
| Multi-tenancy | `docs/multi-tenancy.md` |
| Multi-entity | `docs/multi-entity.md` |
| Architecture | `docs/architecture.md` |
| **Why it's built this way** | `docs/decisions.md` — **Read before proposing something that may already have been considered**; cite as `decisions §N`. Append-only |
| Environment vars | `docs/environment.md` |
| Deployment | `docs/production-deployment.md` |
| Minimal-cost deployment | `docs/minimal-deployment.md` |
| FeohLedger rename migration | `docs/feohledger-rename-migration.md` |
| SOC 2 readiness | `docs/soc2-readiness.md` |
| Data privacy (GDPR/CCPA) | `backend/docs/privacy.md` + `docs/data-residency.md` + `docs/ropa.md` |
| Platform billing & metering (plans / subscriptions / entitlements) | `backend/docs/billing.md` |
| White-label / partner branding | `docs/white-label.md` |
| Founder runbooks (non-code) |  |
| Accessibility (WCAG 2.2 AA) | `docs/accessibility.md` + `docs/accessibility-vpat.md` + `frontend/docs/ui-patterns.md` |
| CSV data import | `backend/docs/csv-import.md` |
| Email-to-invoice intake | `backend/docs/email-intake.md` |
| Contract Management (CLM) | `backend/docs/contracts.md` |
| Expense Management | `backend/docs/expense-management.md` |
| Automated E-Invoicing (PEPPOL send + receive) | `backend/docs/peppol.md` |
| 1099 tracking | `backend/docs/tax-1099.md` |
| Audit-log shipping | `backend/docs/audit-log-shipping.md` |
| Notifications | `backend/docs/notifications.md` |
| Exception agents | `backend/docs/exception-agents.md` |
| Adaptive AI workflows | `backend/docs/adaptive-workflows.md` |
| Data enrichment | `backend/docs/data-enrichment.md` |
| Backup + DR | `docs/backup-disaster-recovery.md` |
| Secrets rotation | `docs/secrets-rotation.md` |
| Getting started | `docs/getting-started.md` |
| Troubleshooting | `docs/troubleshooting.md` |
| Known issues (diagnosed, unfixed) | `docs/known-issues.md` — diagnosed defects, incl. struck-through resolved ones kept for the diagnosis |
| Open follow-ups (deferred work) | `docs/followups.md` — the destination guard rail 6 requires. Open items only — prune on landing |
| Self-service signup | `docs/self-service-signup.md` |
| Supplier portal | `backend/docs/supplier-portal.md` |
| Supplier chat & collaboration | `backend/docs/supplier-chat.md` |
| Roadmap (open work) | `docs/roadmap.md` — **prune on landing** — a finished section moves to the archive in the same commit |
| Roadmap (shipped archive) | `docs/roadmap_shipped.md` — **check here for prior art before building** — most capabilities already exist |
| Competition | `docs/competitive-analysis.md` |

## Guard rails

Standing rules for how to work in this repo. They are not optional; when in
doubt, follow the rule and say so. (Several are detailed in their own sections
below or in `## Project invariants` — this is the index.)

1. **Commit each piece of work; never push.** One logical unit = one
   path-scoped commit, as you finish it. See [Git workflow](#git-workflow).
2. **Add test coverage with the change.** A behavior change ships with tests in
   the same session (pytest / Playwright / flutter). If something is genuinely
   untestable, say *why* rather than skipping silently. See [Every change must
   update docs and tests](#every-change-must-update-docs-and-tests).
3. **Code-review important code.** For non-trivial or load-bearing changes
   (auth, tenant isolation, migrations, the money/payment path, webhook
   handlers, PII), run a review pass before committing — `/check`, `/safe-edit`,
   or the `code-reviewer` agent. Don't gate trivial edits (typos, comments, dep
   bumps) on it.
4. **Never code around an issue — fix the root cause.** No masking: no inflated
   timeouts, sleeps, retries, skipped/loosened assertions, or swallowed errors.
   See [Fix bugs at the source](#fix-bugs-at-the-source--never-adjust-the-test-to-hide-them).
5. **Always recommend the long-term solution.** When a quick patch and a durable
   fix diverge, lead with the durable one and name the trade-off — don't let an
   expedient workaround pass as the answer silently.
6. **No dangling "deferred" / "out-of-scope" findings.** A real issue you
   surface — in a review, an audit, a `/bug-hunt` or `/audit-and-fix` report, a
   code comment, or your own analysis — must be driven to a concrete resolution,
   not left as a passing mention. Default: fix it the same session when it's
   bounded and you've already diagnosed it. Defer only when a fix is genuinely
   too large or risky to land now — and a deferral needs a tracked follow-up
   naming what's broken, the durable fix, and the trigger to do it. **The
   destination is `docs/followups.md`** (categorized: blocked on a credential /
   operator step on merged code / sized-but-unstarted), plus a GitHub issue when
   it warrants one — confirm before creating. A *diagnosed defect* goes to
   `docs/known-issues.md` instead. "Deferred / recommended" in a report is a
   staging area, not a destination. Extends rails 4–5: surfacing an issue is the
   start of the obligation, not the end of it.
7. **Local-first.** Every part of the app must run on a dev laptop with no cloud
   account. Each external dependency ships with a local equivalent *and* a safe
   local default that points at it: Postgres/Redis/MinIO via Compose; provider
   integrations default to their `mock` adapter (extraction, ERP, cards,
   payments, FX, sanctions, audit shipping); email defaults to `console`; SSO is
   off by default with **Keycloak** (`pnpm idp:up`) as the local IdP. When you
   add a dependency on an external service, add its local equivalent and a safe
   local default in the *same* change — never make `pnpm dev` require a real
   SaaS credential.
8. **A pnpm script per service.** Every service or long-running process a
   contributor starts in dev gets a root `package.json` script — don't make
   anyone memorize raw `docker compose --profile …` invocations (`pnpm db:up`,
   `pnpm idp:up`, `pnpm services:up`). Add the script in the same change you add
   the service.
9. **Reusable components.** Build UI from the shared component libraries —
   `frontend/src/lib/components/` (Svelte 5 runes only: `$state` / `$derived` /
   `$effect` / `$props`) and the mobile widget library — instead of copy-pasting
   markup across routes/screens; extract a component the second time you'd
   duplicate it. See [frontend/CLAUDE.md](frontend/CLAUDE.md) and
   [mobile/CLAUDE.md](mobile/CLAUDE.md).
10. **Organize files by responsibility.** Put code in the file / dir its
    siblings already establish (backend: `app/api/`, `app/services/` + the
    adapter subdirs, `app/models/`, `app/schemas/`; frontend:
    `src/lib/{stores,utils,components}` + `api.ts`). Don't dump unrelated logic
    into a file just because it's open — extend the file that owns that
    responsibility. Read the per-area `CLAUDE.md` before editing.
11. **Honour the project invariants.** Money is `Decimal`/`Numeric`; writes that
    move money are idempotent; status changes write audit rows; tenant isolation
    is enforced at the data layer; auth before everything; secrets via sops+KMS;
    PII/banking data stays out of logs; migrations fan out to every tenant;
    webhooks verify signatures + dedupe. Full enumeration with severities in
    [Project invariants](#project-invariants).
12. **Docs-as-code.** A behaviour, command, env var, port, or convention change
    updates its docs in the same turn — deferred docs are drift. See [Every
    change must update docs and tests](#every-change-must-update-docs-and-tests).
13. **One worktree per concurrent session; merge it back to `main`.** When more
    than one Claude (or person) works this repo at once, each session runs in its
    own git worktree (`claude --worktree <name>`) — never two sessions in the
    shared checkout. The scope-guard is path-granular, not hunk-granular, so two
    sessions editing the *same file* in one checkout silently capture each
    other's edits (a path-scoped commit grabs whatever is in the one shared tree);
    a worktree is the only real fix. Then **consolidate**: a worktree commits on
    its own branch and only reaches `main` via an explicit `git merge` from the
    primary checkout — retiring a worktree does NOT merge it. Before ending such
    work, run `git branch --no-merged main` and merge anything still off `main`.
    The `SessionStart` hook `.claude/hooks/unmerged-worktree-check.sh` is the
    backstop: it warns at every session start about branches holding commits not
    on `main` — when it fires, surface it and offer to consolidate. See [Running
    concurrent sessions — use a worktree](#running-concurrent-sessions--use-a-worktree).

## Every change must update docs and tests

1. **Update tests** — add or adjust coverage for behavior you touched. No tests exist yet — create them when adding new features.
2. **Update docs** — if the change affects architecture, commands, env vars, deployment, or features, update the relevant doc (and this CLAUDE.md if setup or workflows changed).

## Git workflow

- **Commit each piece of work; never push.** Land every logical unit of work as its own path-scoped commit (`git commit -m "…" -- path/to/file …`) as you finish it — don't leave the tree dirty across tasks or batch unrelated changes into one commit. **Never `git push`** in this repo; publishing is the operator's call.
- Path-scoped commits are also required by the `.claude/hooks/git-scope-guard.py` PreToolUse hook — bare `git commit`, `git add -A/.`, `git commit -a`, and whole-tree ops are blocked. If a git command is denied, follow the scoped alternative in its message.
- No `Co-Authored-By` / "Generated with" trailer in commits or PRs — write them as a human would.
- **PR titles must be conventional-commit formatted** — `type(scope): subject`, type one of `feat|fix|chore|docs|refactor|test|perf|ci|build|revert` (scope free-form). CI enforces this via `.github/workflows/pr-title-lint.yml` (`lint title` check) and a bare descriptive title fails it, so pass a compliant `--title` to `gh pr create` / `gh pr edit`.

### Running concurrent sessions — use a worktree

When more than one Claude (or person) works this repo at once, **start each
session in its own git worktree** so they never share a working tree:

```bash
claude --worktree <name>      # e.g. claude --worktree clickable-rows
```

Each lands in `.claude/worktrees/<name>/` — a full checkout on its own branch,
gitignored, branched from local `HEAD` (`worktree.baseRef: "head"` in
`.claude/settings.json`, because this repo runs ahead of an unpushed `origin`).

Why it matters: the scope-guard is **path-granular, not hunk-granular**. In a
*shared* checkout it stops a session from committing files it didn't name, but
it can't separate two sessions' edits to the **same file** — a path-scoped
commit of that file captures whatever is in the one shared working tree. A
worktree removes the shared tree entirely, which is the only real fix. (`isolation: "worktree"` on a subagent isolates that subagent, not the top-level session.)

Worktree notes:
- The committed `*.env.development` defaults travel with the worktree, so the
  stack runs immediately. Gitignored personal env overrides are copied via
  `.worktreeinclude`.
- `backend/.venv` and `node_modules` do **not** carry over (venv paths are
  absolute; node_modules is heavy) — run `pnpm install` / recreate the venv in
  the worktree before building or testing there.
- A worktree isolates **files, not the database** — every session still shares
  the one local Postgres. Backend `pytest` handles that itself: the `realdb`
  harness claims a per-process slot and gets its own tenant databases, so
  concurrent runs no longer truncate and disconnect each other (backend
  `CLAUDE.md` § Test databases). Sharing the DB with a *running dev backend* is
  still unsafe — see `docs/known-issues.md`.
- A worktree isolates **files, not ports**, either — and that one is quieter,
  because `playwright.config.ts` sets `reuseExistingServer`: a second session
  keeping the default `:7777` finds the *primary checkout's* dev server already
  listening and tests **that** build, green, against code it never changed. Give
  the second session its own stack rather than borrowing the first's:
  ```bash
  E2E_WEB_ORIGIN=http://localhost:7801 PUBLIC_API_URL=http://localhost:8001 E2E_TENANT_OFFSET=1 pnpm test:e2e
  ```
  `frontend/tests-e2e/fixtures/env.ts` resolves both and derives every other
  origin from them; nothing else may read a port. Full detail, including what
  those variables do **not** reach, is `frontend/tests-e2e/README.md` § Ports.
- **All work must end up on `main`.** A worktree commits on its own branch, and
  git won't let a worktree check out `main`, so that work only reaches `main`
  via an explicit merge from the **primary checkout**. Before retiring a
  worktree, consolidate it:
  ```bash
  git branch --no-merged main          # audit: anything still off main?
  git merge <worktree-branch>          # from the primary checkout (on main)
  ```
  Use `git merge --ff-only` when main hasn't moved (linear); otherwise rebase
  the worktree branch onto `main` first to keep history linear. Claude prompts
  to keep or remove the worktree on exit (auto-removes it if you left no
  changes) — but removal does **not** merge; consolidate first.
- This is backed by a safety net: the `SessionStart` hook
  `.claude/hooks/unmerged-worktree-check.sh` warns at the start of every session
  if any branch holds commits not on `main`, so stranded worktree work surfaces
  and gets merged instead of forgotten.

## Fix bugs at the source — never adjust the test to hide them

When a test fails, the only acceptable resolution paths are:

1. **The test itself is broken** (wrong fixture, missing required field, typo, race in test setup, unique-constraint collision with seed data). Fix the test.
2. **The app has a real bug or missing primitive.** Fix the app code. If the app needs a new affordance for the test to wait deterministically (a `data-ready` attribute backed by a real readiness signal, an exposed status, a broadcast handshake), add it in the app code — it's a real API, not test scaffolding.

There is no third option. These are forbidden because they ship the bug behind a green check:

- Inflating a Playwright `expect` / `toBeVisible` timeout to absorb a flake (`5_000` → `15_000` → `30_000`). Fix whatever makes the page slow.
- `await page.waitForTimeout(N)` between two actions. Wait on a real signal (DOM node, state attribute, network response).
- Bumping `--retries` (or relying on Playwright's `retries: 1`) to mask a real race.
- `test.skip(…)` / `test.fixme(…)` / `test.fail(…)` (or pytest's `@pytest.mark.skip` / `xfail`) against a real bug without an open follow-up that names what's broken + when it'll be fixed.
- Loosening strict assertions (`assert x == 'foo'` → `assert 'foo' in x.lower() or 'bar' in x.lower()`) to "absorb variance" — the variance IS the bug.
- Replacing a real wait with a sleep "because the real signal is unreliable" — the real signal needs fixing.

If you spot a candidate fix that fits one of those patterns: stop, surface the underlying app issue, and either fix it in the same session or flag it explicitly. Don't half-mask it via the test.

## Conventions and gotchas

- **Static frontend** — no SSR. All dynamic data goes through the backend API.
- **Svelte 5 runes** — `$state`, `$derived`, `$effect`, `$props` — not the legacy options API.
- **API client** — all frontend fetches go through `frontend/src/lib/api.ts` (auto-adds JWT + tenant header).
- **Python style** — ruff for lint/format. Line length 100. Python 3.12+ features allowed.
- **Migrations** — Alembic for all schema changes. Must run on every tenant DB, not just control plane.
- **Secrets** — never commit a secret-bearing `.env`. The only committed env files are `*.env.development` (safe local-dev defaults only) and the KMS-encrypted `*.sops` files.
- **Two backend entry points** — `main.py` for local dev (auto-reload), `app/main.py:app` for production (uvicorn).
- **Async everywhere** — all DB operations use SQLAlchemy 2 async. Don't mix sync/async.
- **Workflow snapshots** — `WorkflowInstance.steps_config_snapshot` is frozen at invoice creation. Read the snapshot, not the live definition, for in-flight invoices.
- **Redis** — used for JWT token blocklist (logout), not general caching.

## What not to do

- Don't add a test framework other than pytest (backend) or vitest (frontend).
- Don't replace pnpm with npm/yarn.
- Don't add SSR adapters to the frontend; it must stay static for GitHub Pages.
- Don't call secret-bearing services from the frontend — go through the backend.
- Don't modify tenant DBs outside of Alembic migrations.
- Don't add `dotenv` imports to modules reachable from Lambda entry points.
- Don't hardcode tenant DB names — always use `feoh_<slug>` via config.

## Project invariants

These are the rules the `.claude/agents/code-reviewer.md` agent cites. A diff that violates one is `Critical` unless the project explicitly opts out in writing. Stack-specific enforcement notes are in parentheses.

- **Money is exact.** Amounts use `Decimal` (never `float`), and SQLAlchemy columns for currency use `Numeric(precision, scale)` (never `Float` / `Real`). A new column or in-memory total typed as `float` for currency is `Critical`.
- **Idempotency on writes that move money.** Anything that initiates a payment, reverses a payment, or confirms an invoice as paid must be idempotent at the API boundary. The mechanism is whichever the backend already uses (idempotency-key header, request-id table, or a DB-level unique constraint on the operation tuple). A new "send payment" / "post payment" / "confirm payable" handler with no idempotency story is `Critical`.
- **Audit trail is append-only.** Status transitions on invoices, payments, approvals, and vendors write a log row through the audit-shipping infrastructure (`services/audit_shipping/` — see `## Architecture overview`), not just mutate state. A status change that overwrites without producing an audit row is `Improvement` at minimum, `Critical` if the field is regulated (`paid_at`, `approved_at`, `void_at`).
- **Tenant isolation is enforced at the data layer, not just by application code.** Every read / write resolves the tenant DB via the `X-Tenant-Slug` header → `feoh_<slug>` mapping (see `## Multi-tenancy`). `backend/app/tenant.py::get_tenant` is the chokepoint and cross-checks the JWT's `org` claim against the resolved tenant — so a leaked / spoofed header alone can't widen access. A new query that runs against the control-plane DB while reading tenant data, hardcodes a tenant DB name, or constructs a tenant engine outside `get_tenant_db` is `Critical`.
- **Auth before everything.** Every route under `/api` is behind the auth middleware unless it is documented public-by-design. A new route mounted before the auth dependency, or one that references the user's identity without the auth dependency injected, is `Critical`. Approval / payment endpoints also check role / RBAC, not just authentication.
- **Secrets via sops + AWS KMS, no hardcoded fallback.** Long-lived secrets live only in `*.sops` files, decrypted via the project's KMS key. A new `os.environ["X"]` with a fallback like `or "some-default"` for a secret is `Critical`. The only committed env files are `*.env.development` (safe local-dev defaults only — loopback URLs, mock adapters, the `change-me` JWT key) and the encrypted `*.sops` files; a committed `.env` / `.env.local` / `.env.production` carrying a real secret is `Critical`.
- **PII / banking data stays out of logs and error responses.** Bank account numbers, tax IDs, full vendor addresses, and full payment-method numbers must not appear in `logger` output, in HTTP error bodies, or in URL query strings. A `print` / `logger.info(...)` containing one of those fields is `Critical`.
- **Migrations are idempotent and run on every tenant DB.** New Alembic revisions use safe DDL (`IF NOT EXISTS` / `IF EXISTS` where applicable). A schema change that lands as control-plane-only when the change should fan out to every tenant is `Critical` — see `Don't modify tenant DBs outside of Alembic migrations` in `## What not to do`.
- **Webhook handlers verify signatures and dedupe by event id.** A new handler that doesn't verify the provider's HMAC, or doesn't dedupe by `event.id`, is `Critical` — webhook providers retry on any non-2xx and dedup is the only thing keeping a one-time effect one-time. The shared helpers live in `backend/app/services/webhook_security.py` (`verify_hmac_sha256`, `is_event_already_processed`, `extract_signature_header`); every webhook also returns 204 silently on every rejection path so the response doesn't enumerate.
- **Passwords use the shared `bcrypt_sha256` context, through its awaitable wrappers.** `backend/app/utils/passwords.py::pwd_context` is the single hash context across the codebase, and since `docs/decisions.md` §151 it *implements* `bcrypt_sha256` (HMAC-SHA256 pre-hash → bcrypt) directly rather than via passlib, which could not import against bcrypt 4.1+ and so froze the hashing library on the login path at 4.0.1. That makes `app/utils/passwords.py` the ONLY module allowed to `import bcrypt` — a second import, or any `bcrypt.hashpw` / `bcrypt.checkpw` call outside it, is `Critical`, as is a fresh `CryptContext(schemes=["bcrypt"], ...)` should passlib ever return; both shapes are caught by `.claude/hooks/security-patterns.sh` rule `bcrypt-truncation` and by `tests/test_password_hashing_offloaded.py`. Application code calls `verify_password` / `hash_password` / `dummy_verify` (coroutines that run the hash via `asyncio.to_thread`), never `pwd_context.verify` / `.hash` inline: bcrypt is ~200 ms of CPU by design, so an inline call from a coroutine stalls the whole worker for that window on the most concurrent endpoint in the app. A direct call under `app/` is `Improvement` at minimum and fails the same test. Digest compatibility with every hash already in the column — v2, legacy v1, and pre-upgrade plain `$2b$` — is pinned against passlib-generated literals in `tests/test_bcrypt_sha256_compat.py`; do not regenerate those fixtures. **A row still on a deprecated scheme is re-hashed on its owner's next successful login, by `services/credential_upgrade.py` and nowhere else** — one call per login surface, compare-and-swap, and it never fails a sign-in (`docs/authentication.md` § A legacy hash is upgraded on its owner's next login; `docs/decisions.md` §163–§164).
- **Blocking work does not run on the event loop.** The backend is async throughout, so a synchronous call that waits — a boto3 S3 round trip, `socket.getaddrinfo`, a sync `httpx.Client`, bcrypt, a ReportLab layout — occupies the loop for its full duration and every other in-flight request on that worker waits behind it. Each such case has a single owner that offloads it: `services/storage`'s `_put_object` / `_get_object` / `_delete_object` for object storage, `utils/url_safety`'s `*_async` pair for SSRF DNS, `utils/passwords`' wrappers for hashing, `await asyncio.to_thread(render_x, ctx)` at every PDF export route, and `await asyncio.to_thread(_send_to_sqs, ...)` in the three `*_dispatch` modules when the mode is `lambda` (boto3 SQS is a synchronous round trip, and `dispatch_auth_audit` is on the login path). A new blocking call reached from an `async def` is `Improvement`, or `Critical` on a public webhook / auth path. Drift guards: `tests/test_storage_nonblocking.py`, `tests/test_pdf_render_offloaded.py`, `tests/test_password_hashing_offloaded.py`, `tests/test_url_safety.py`, `tests/test_sqs_dispatch_nonblocking.py`.
