# FeohLedger

Full-stack accounts payable management application built with SvelteKit, FastAPI, and PostgreSQL.

## Quick Start

```bash
# 1. Start infrastructure (Postgres, Redis, MinIO)
pnpm db:up

# 2. Bootstrap the backend (in one terminal)
cd backend && python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pnpm seed
pnpm dev:backend       # uvicorn on :8000

# 3. Start the frontend (in another terminal)
pnpm install:frontend  # first time only
pnpm dev:frontend      # vite on :7777
```

Open http://acme.localhost:7777 — login with `demo@acme.com` / `demo`

**Local-first, zero secret setup.** You don't copy, stamp, or edit any `.env`
files by hand. `backend/.env.development` and `frontend/.env.development` are
**committed** with safe, no-risk local defaults (loopback URLs, `mock`
adapters, MinIO's `minioadmin/minioadmin`, a `change-me` JWT key), so a fresh
clone runs immediately. The backend loads them via `main.py` (its local-dev
entrypoint); the frontend loads `.env.development` natively in Vite dev mode;
the mobile app hardcodes its localhost API URL. Personal overrides go in a
gitignored `backend/.env` / `frontend/.env.local` and win over the committed
defaults. Real deployed secrets never live in any `.env*` or anywhere in this
public repo; they're sops-encrypted in the private `infra-secrets` repo (see
[`backend/CLAUDE.md` § Secrets management](backend/CLAUDE.md)).

The `pnpm` commands above are thin wrappers that `cd` into the right workspace and call its native toolchain (pip / pnpm / flutter). See [Root scripts](#root-scripts) for the full list, or run `pnpm run` to print them.

## Root scripts

Common cross-workspace tasks are exposed via `pnpm run` at the repo root. Each script is a one-line dispatch to the per-workspace native tool — there are no JS dependencies at root.

| Script | Dispatches to |
|---|---|
| `pnpm install:{backend,frontend,mobile,all}` | `pip install -e '.[dev]'` / `pnpm install` / `flutter pub get` |
| `pnpm dev:{backend,frontend,mobile}` | `python main.py` / `vite dev` / `flutter run` |
| `pnpm dev` / `pnpm dev:all` / `pnpm dev:full` | backend + frontend together (one Ctrl-C stops both) / `db:up` (core) then `dev` / `services:up` (core + **every** opt-in profile) then `dev` — the whole stack from cold |
| `pnpm build:frontend` | `vite build` |
| `pnpm lint:{backend,frontend,mobile}` + `pnpm lint` | `ruff check .` / `pnpm check` / `flutter analyze` |
| `pnpm format[:backend][:check]` | `ruff format [--check] .` |
| `pnpm test:{backend,frontend,mobile}` + `pnpm test` | `pytest` / `pnpm test:e2e` / `flutter test` |
| `pnpm db:{up,down,logs,reset}` | core services (Postgres + Redis + MinIO) `docker compose ...` in `backend/` |
| `pnpm idp:{up,down,logs}` | local IdPs (opt-in `idp` profile): Keycloak (OIDC SSO, :8088) + Authentik (SCIM, :9002) |
| `pnpm idp:seed` | point the acme tenant's `settings.sso` at local Keycloak |
| `pnpm scim:seed` | set the acme tenant's SCIM bearer token to match the Authentik blueprint |
| `pnpm aws:{up,down,logs}` | local AWS emulator (LocalStack, opt-in `aws` profile): SQS, SES, CloudWatch, S3 Object Lock |
| `pnpm ollama:{up,down,logs}` + `pnpm ollama:pull <model>` | local AI model server (opt-in `ai` profile) for the `ollama` extraction adapter |
| `pnpm stripe:{up,down,logs}` | Stripe API mock (opt-in `payments` profile) for the `stripe_treasury` adapter |
| `pnpm mail:{up,down,logs}` | Mailpit SMTP sink + web inbox (opt-in `mail` profile) for the `smtp` email adapter (inbox at :8025) |
| `pnpm services:{up,down,logs,reset}` | bring up / tear down / tail **all** local services (core + IdPs + LocalStack + Ollama + stripe-mock + Mailpit) at once |
| `pnpm seed` | `python scripts/seed.py` |
| `pnpm test:scim` | run the SCIM provisioning e2e (`tests-e2e/scim/`) |
| `pnpm migrate[:tenants|:all]` | `alembic upgrade head` / `scripts/migrate_all_tenants.py` |

Local identity testing, no cloud account:

- **SSO** — `pnpm idp:up && pnpm idp:seed`, then sign in via SSO at
  `http://acme.localhost:7777` as `demo@acme.com` / `demo`.
- **SCIM** — `pnpm idp:up && pnpm scim:seed`, then in Authentik
  (`http://localhost:9002`, `akadmin` / `admin`) run the "FeohLedger SCIM"
  provider sync; provisioned users appear in `/admin`.

See [`docs/local-sso-keycloak.md`](docs/local-sso-keycloak.md).

The backend scripts assume your backend venv is activated (`source backend/.venv/bin/activate`). Per-workspace docs in `backend/CLAUDE.md`, `frontend/CLAUDE.md`, `mobile/CLAUDE.md` cover everything the dispatch scripts don't.

## Documentation

Cross-cutting docs live in [`/docs`](docs/). Backend-specific docs live in [`/backend/docs`](backend/docs/). Frontend and mobile details live in their subproject `CLAUDE.md` files.

**Cross-cutting (`/docs`)**

| Document | Description |
|----------|-------------|
| [Getting Started](docs/getting-started.md) | Prerequisites, setup, and first run |
| [Architecture](docs/architecture.md) | System overview, tech stack, project structure |
| [Authentication](docs/authentication.md) | JWT auth flow, RBAC, frontend/backend integration |
| [User Management](docs/user-management.md) | Role matrix, user admin |
| [Multi-Tenancy](docs/multi-tenancy.md) | Subdomain routing, DB-per-tenant, provisioning |
| [Self-Service Signup](docs/self-service-signup.md) | Public signup flow, abuse mitigations |
| [Environment Variables](docs/environment.md) | Frontend and backend configuration |
| [Production Deployment](docs/production-deployment.md) | AWS, CloudFront, ALB, ECS |
| [Backup & Disaster Recovery](docs/backup-disaster-recovery.md) | RTO/RPO, restore procedures |
| [Secrets Rotation](docs/secrets-rotation.md) | What to rotate, when, and how |
| [FeohLedger Rename Migration](docs/feohledger-rename-migration.md) | One-time upgrade for environments provisioned before the rename (`AP_*` → `FEOH_*`, database names, KMS alias) |
| [SOC 2 Readiness](docs/soc2-readiness.md) | Control mapping, vendor selection, kickoff plan |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and fixes |
| [Founder Runbooks](docs/founder-runbooks/) | Non-code playbooks — legal, prod deploy, Stripe, payment rails, SOC 2, support |
| [Roadmap](docs/roadmap.md) | Open feature backlog — only what's still unshipped |
| [Roadmap — shipped](docs/roadmap_shipped.md) | Archive of completed roadmap sections |
| [Architecture decisions](docs/decisions.md) | Why non-obvious choices were made, and what was rejected |
| [Open follow-ups](docs/followups.md) | Deferred work, categorized, with its durable fix and trigger |
| [Competitive Analysis](docs/competitive-analysis.md) | Market landscape |

**Backend (`/backend/docs`)**

| Document | Description |
|----------|-------------|
| [API Reference](backend/docs/api-reference.md) | All REST endpoints with parameters |
| [Database](backend/docs/database.md) | PostgreSQL schema, models, Alembic migrations |
| [Docker](backend/docs/docker.md) | Docker Compose services, commands, health checks |
| [Redis](backend/docs/redis.md) | Token blocklist and cache |
| [MinIO](backend/docs/minio.md) | S3-compatible object storage setup |
| [AI Extraction](backend/docs/ai-extraction.md) | Platform vs BYOK, provider configs |
| [Inbound E-Invoicing](backend/docs/e-invoicing.md) | UBL 2.1 / Factur-X / ZUGFeRD parsing, auto-detect on upload and email intake |
| [ERP Integration](backend/docs/erp-integration.md) | Adapter pattern, Merge.dev, direct APIs |
| [Workflow Design](backend/docs/workflow-design.md) | State machine, step types |
| [Workflow Snapshots](backend/docs/workflow-snapshots.md) | Frozen definition semantics |
| [Payments](backend/docs/payments.md) | Payment runs, schedules, ERP sync |
| [Virtual Cards](backend/docs/virtual-cards.md) | Lithic/Nium, rebates, webhooks |
| [International Payments](backend/docs/international-payments.md) | Cross-border rails, FX, sanctions screening |
| [Bank Reconciliation](backend/docs/bank-reconciliation.md) | Statement import, match-to-payment |
| [PO Matching](backend/docs/po-matching.md) | 2-way/3-way matching logic |
| [Vendor Management](backend/docs/vendor-management.md) | Sources, sync, matching |
| [Supplier Portal](backend/docs/supplier-portal.md) | VendorUser auth, invoice submission |
| [Email Intake](backend/docs/email-intake.md) | Inbound email-to-invoice, SES + Mailgun |
| [CSV Import](backend/docs/csv-import.md) | Day-0 vendor + invoice migration |
| [Analytics](backend/docs/analytics.md) | CFO dashboard metrics, exports, scheduled reports |
| [Audit Log Shipping](backend/docs/audit-log-shipping.md) | Centralized WORM sink, S3 Object Lock |
| [1099 Tracking](backend/docs/tax-1099.md) | W-9 collection, YTD reporting |
| [Local AI Testing](backend/docs/local-ai-testing.md) | Ollama setup |

**Subproject guides**

| File | Purpose |
|------|---------|
| [`backend/CLAUDE.md`](backend/CLAUDE.md) | Backend structure, conventions, test harness |
| [`backend/docs/`](backend/docs/) | Per-router API surface, services map, adapter families |
| [`frontend/CLAUDE.md`](frontend/CLAUDE.md) | Frontend rules, stack, conventions |
| [`frontend/docs/`](frontend/docs/) | UI patterns, component library, routes → API map, i18n |
| [`mobile/CLAUDE.md`](mobile/CLAUDE.md) | Mobile rules, architecture patterns, role-based UI |
| [`mobile/docs/`](mobile/docs/) | Feature status, project structure, i18n |

Each `CLAUDE.md` holds the **rules**; the sibling `docs/` directory holds the
**reference material**, so the always-loaded files stay small.
