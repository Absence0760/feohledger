---
description: Generate a canonical inventory of the backend HTTP API by reading the FastAPI routers — feeds external integrators (no OpenAPI is published as the contract) and gives /audit/auth a ground-truth route list to work from.
---

Produce a canonical, table-shaped inventory of every backend HTTP endpoint by reading the FastAPI routers — not by trusting any doc.

There is **no codegen** in FeohLedger (request/response types are hand-synced across the backend Pydantic schemas in `backend/app/schemas/`, the frontend client `frontend/src/lib/api.ts`, and the ORM models) and **no maintained OpenAPI / API spec as the contract**. FastAPI does auto-generate `/docs` at runtime, but that's derived state, not the ground truth — the route source itself is the only authority for "what endpoints exist." This command flattens that into one artifact two audiences need:

- **External integrators** wiring against the API (ERP webhook callers, payment-rail webhook senders, card-provider webhooks, SCIM provisioners, email-intake providers, supplier-portal clients) who have no spec to read.
- **`/audit/auth`**, which sweeps every backend route for auth-dependency gating + tenant-context discipline. Hand it a maintained inventory instead of re-deriving the route list every run.

This is a **generator**, not a findings audit. It describes what the API *is*; it does not flag bugs. Read-only on the codebase except for writing the one output file.

## What it produces

`reviews/endpoint-inventory.md` — one row per registered route, in router-mount order (the order the `app.include_router(...)` calls appear in `backend/app/main.py`). Columns:

| Method | Path | Auth | Tenant-scope | Request params | Response shape | Notes |
|--------|------|------|--------------|----------------|----------------|-------|

Column semantics — fill each from the actual code, never inferred:

- **Method** — `GET` / `POST` / `PATCH` / `PUT` / `DELETE`, from the `@router.<verb>(...)` decorator.
- **Path** — full path: the `/api` mount prefix from `main.py` (`app.include_router(invoices.router, prefix="/api")`) joined to the router's own prefix and the in-handler path (`@router.get("/{invoice_id}")`) → `GET /api/invoices/{invoice_id}`. Routers carry their own `prefix=` on the `APIRouter(...)` constructor inside each `backend/app/api/<name>.py` — read it there, don't assume it from the CLAUDE.md table.
- **Auth** — inspect each handler's FastAPI dependencies (`Depends(...)`). Most routes inject `get_current_user` / `require_roles(...)` from `backend/app/api/deps.py` — mark those `auth` (and note the roles for `require_roles`). Mark `public` for the routes that are public-by-design, classifying each by reading its handler, not by router:
  - `GET /api/health`, `GET /api/public-config`
  - `/api/auth/login` (+ logout/refresh and the MFA challenge/verify steps) — but `/api/auth/me`, `/api/auth/change-password`, MFA enroll/disable are per-handler-gated. Mark each `/api/auth/*` row by checking whether that specific handler injects `get_current_user`.
  - `/api/auth/sso/config|authorize|callback` (OIDC — public; the callback JIT-provisions and mints a JWT)
  - `/api/scim/v2/*` (per-tenant **SCIM bearer token**, not a user JWT — its own auth scheme)
  - `/api/portal/auth/*` and `/api/portal/*` (supplier-portal **vendor JWT**, `typ=vendor` — a separate identity from the staff JWT; `portal/auth` login is public, the rest are vendor-gated via `portal_deps.py`)
  - `/api/signup/*` (rate-limited + captcha, no auth)
  - `/api/email-intake` (provider-signed inbound webhook — HMAC, no JWT)
  - `/api/erp/webhook/{erp_type}` inbound ERP webhooks (HMAC, tenant in path)
  - `/api/payments/webhook/{tenant_slug}/{provider}` and `/api/cards/webhook/{provider}` payment/card webhooks (HMAC-verified, tenant in URL path, no JWT)
- **Tenant-scope** — `get_tenant_db` (resolves the tenant DB from the `X-Tenant-Slug` header → `feoh_<slug>`, with a JWT `org`-claim cross-check in `backend/app/tenant.py::get_tenant`) is the norm for tenant-data routes. Flag the exceptions explicitly:
  - **Control-plane-only** routes that hit `feohledger` via `get_control_db` (orgs / users / roles under `/api/admin`, `/api/auth`, `/api/signup`, `/api/organization`) — they touch no tenant DB.
  - Routes that resolve the tenant from a **URL path segment** rather than the header (`/api/payments/webhook/{tenant_slug}/...`, `/api/erp/webhook/{erp_type}`, email-intake's `+<token>@` address) — note where the tenant comes from.
  - A handler that touches tenant data **without** `get_tenant_db`, or that **hardcodes a tenant DB name**, is a finding for `/audit/auth` / `/audit-security` — note it, don't fix it here.
- **Request params** — path params (`{invoice_id}`, `{provider}`, `{tenant_slug}`, `{file_key:path}`), query params (the real casing the handler reads — e.g. `?status=`, `?page=`, `?slug=`), and the body schema name for POST/PATCH/PUT (the Pydantic model the handler takes, from `backend/app/schemas/`). Use the names the handler actually reads, not the doc's names.
- **Response shape** — the Pydantic `response_model=` / returned schema name in `backend/app/schemas/` when one is declared, paired with the matching interface/function in `frontend/src/lib/api.ts` when one exists. For webhook / SCIM / portal-only / integration surfaces with no SPA client, say so plainly — `no client function`, `204 No Content`, `302 → IdP`, `{ "status": "ok" }`.

Then two short cross-reference sections:

- **Server-only routes (no `frontend/src/lib/api.ts` client function).** Routes that exist in `backend/app/api/*.py` but have no matching call in the frontend client — typically the integrator-facing surface consumed by external callers, not the SvelteKit SPA: the payment-rail webhooks (`/api/payments/webhook/...`), card webhooks (`/api/cards/webhook/{provider}`), ERP inbound webhooks (`/api/erp/webhook/{erp_type}`), email-intake (`/api/email-intake`), the SCIM 2.0 surface (`/api/scim/v2/*`), and the supplier-portal endpoints (`/api/portal/*`, vendor-client not staff-SPA). Call these out as the integrator-facing surface.
- **Client functions with no matching route (drift).** `api.ts` functions whose URL no longer resolves to a registered route. Each is a real bug — a dead client call. List them with the `api.ts` line number.

## Procedure

1. **Read the registration order.** `backend/app/main.py` is the source of truth for which routers mount and in what order (every `app.include_router(<name>.router, prefix="/api")`), plus the two app-level routes declared inline (`GET /api/health`, `GET /api/public-config`). Note the special cases: `email_intake` mounts **two** routers (`public_router` for the signed inbound webhook, `admin_router` for the per-tenant intake-address management); `auth_sso`, `scim`, `portal_auth`, and `portal` each carry their own non-`/api`-relative prefix on the `APIRouter(...)`.
2. **Read each router.** For every router imported in `main.py` (`from app.api import admin, analytics, auth, auth_sso, cards, credit_memos, dashboard, email_intake, erp_webhook, exceptions, gl_accounts, goods_receipts, invoices, organization, payments, portal, portal_auth, purchase_orders, scim, signup, tax, vendors, workflow, workflow_definitions`), open `backend/app/api/<name>.py` and enumerate its `@router.<verb>(path, ...)` handlers. Don't sample — read them all. (`deps.py` and `portal_deps.py` are dependency modules, not routers — skip them for the route list, but read them to classify auth.)
3. **Classify auth + tenant-scope per handler** by reading each handler's `Depends(...)` list: which of `get_current_user` / `require_roles(...)` (staff JWT + RBAC), the SCIM bearer dep, the vendor-JWT dep (`portal_deps.py`), or no auth dep at all it injects; and whether it takes `get_tenant_db` (tenant DB) vs `get_control_db` (control plane) vs resolves the tenant from a path segment.
4. **Cross-reference `frontend/src/lib/api.ts`** — match each client function's URL to a registered route to populate the response-shape column and build the two drift sections.
5. **Write `reviews/endpoint-inventory.md`** (overwrite if present). Lead with a one-line note: *generated from the route source on `<date>`; regenerate after adding routes.* This artifact can be **promoted to `docs/`** as a maintained, committed inventory if the team wants it under version control — `reviews/` is gitignored, so by default it's a working snapshot.

**Delegate to** the `Explore` (or `general-purpose`) agent if helpful: pass this file as the prompt. The agent reads `backend/app/main.py` + all of `backend/app/api/*.py` + `backend/app/schemas/` + `frontend/src/lib/api.ts` and writes the single output file. Read-only on the rest of the codebase — no other edits, no git.

## Notes

- This is a generator, not an audit. It does not grade auth-dependency coverage or tenant-isolation discipline — it just records the current state. For findings, pair it with **`/audit/auth`** (route gating + `get_tenant_db` discipline) and **`/audit-security`** (the broader security sweep).
- An **API-contract** check (request/response shapes matching across the backend Pydantic schemas ↔ `api.ts` ↔ ORM models, given there's no codegen) is the natural companion when one exists.
- **Re-run after adding or moving routes** — a new `app.include_router(...)` mount, a new `APIRouter(prefix=...)`, or a new `@router.<verb>(...)` handler makes the inventory stale immediately, and `/audit/auth` starts working from an out-of-date list.

## Guard rails

Read-only / generator — no commits required beyond writing the one artifact. If you do commit it, path-scope the commit (`git commit -m "…" -- reviews/endpoint-inventory.md`).
