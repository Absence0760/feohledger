---
description: Sweep every backend route for auth-middleware gating + tenant-context wrapper discipline
---

Audit auth gating and tenant-context enforcement across the FastAPI
backend.

## Goal

A single route registered without `Depends(get_current_user)` exposes
whatever it does to anonymous callers. A single route that reads
tenant-scoped data without going through `get_tenant_db` reads against
the wrong database (or no database at all), bypassing the DB-per-tenant
isolation this project relies on. Find both classes of bug in one pass.

## What to check

1. **Router registration.** `backend/app/main.py` is the single mount
   point — every domain router is included with
   `app.include_router(<router>, prefix="/api")`. There is no global
   "auth middleware applied at the mount" — auth is per-route via
   FastAPI dependency injection. The gate is therefore:

   - Every route inside an included router declares
     `user: User = Depends(get_current_user)` (or `Depends(require_roles(...))`,
     which transitively depends on `get_current_user`).
   - Public-by-design routes live in `PUBLIC_BY_DESIGN` (credential-free, driven
     by the test) or `ALTERNATE_AUTH` (gated by a non-JWT mechanism) inside
     `backend/tests/test_rbac.py`. That set is the explicit allowlist;
     any new route mounted without an auth dep that is *not* in
     either allowlist fails the test gate.

   The legitimate public routes today: `/api/health`, `/api/public-config`,
   `/api/auth/login`, `/api/auth/sso/*`, `/api/auth/mfa/challenge/*`,
   `/api/auth/mfa/verify`, `/api/signup/*`, every `/api/*/webhook/*`,
   every `/api/portal/auth/*`, every SCIM bearer-token endpoint
   (`/api/scim/v2/*`).

   Anything else mounted without a user dep is a finding.

2. **`request.state.user` / `current_user.id` access.** Inside any
   handler, code like `current_user.id`, `current_user.organization_id`,
   or `user.roles` is only safe when `get_current_user` actually ran
   upstream. If the access exists in a route whose signature does NOT
   declare a `Depends(get_current_user)` (or `Depends(require_roles(...))`,
   or vendor-portal equivalent `Depends(get_current_vendor_user)`), the
   handler will 500 on the first anonymous call. Grep route handlers
   for these accesses and confirm each lives in a gated signature.

3. **Tenant-DB session discipline.** Routes that read or write
   tenant-scoped data must take `db: AsyncSession = Depends(get_tenant_db)`
   (from `app.tenant`), never `Depends(get_control_db)`. The
   chokepoint `get_tenant` (transitively pulled in by `get_tenant_db`)
   cross-checks the JWT's `org` claim against the resolved
   `X-Tenant-Slug` so a leaked header alone can't widen access.

   **Control-plane queries are legitimate** (don't flag) when they are
   explicitly cross-tenant by design:

   - The auth / signup / SSO routers — they read `Organization` and
     `User` rows from the control plane before any tenant context
     exists.
   - SCIM provisioning endpoints — they target the control-plane User
     table by definition.
   - Admin endpoints that manage users in the control plane.
   - Background sweeps that iterate every tenant (the shipper, the
     reaper, the reconciler — they use the engine pool directly, not
     a FastAPI dep).

   Any **new** tenant-data route that opens a control-plane session
   (`Depends(get_control_db)`) is a finding unless the file has a
   comment explaining why it's cross-tenant by design.

   Anywhere code constructs a tenant engine **without** going through
   `get_tenant_db` — e.g. calling `get_tenant_engine(...)` directly
   from a request handler, or hardcoding an `feoh_<slug>` DB name — is
   a finding. That path bypasses the JWT/header cross-check.

4. **Resource-level authorization on path-param handlers.** Endpoints
   that take a resource id in the URL (`/invoices/{id}`,
   `/payments/{id}`, `/workflows/{id}`) must verify the resource
   belongs to the caller's tenant **before** doing the work. With
   DB-per-tenant this is almost always automatic — the row lookup
   runs against the tenant DB the JWT cross-check already locked
   down. The remaining footguns:

   - **File-key handlers** (e.g. `GET /api/workflow/file/{file_key:path}`)
     where the key is interpreted across tenants. The fix lives in
     `app/api/workflow.py`: the handler verifies the key's first
     segment equals the requesting user's `organization_id` and
     returns the same 404 for wrong-org vs missing-file so the
     response doesn't enumerate prefixes.
   - **Webhook handlers** where the tenant comes from the URL path
     (`/api/payments/webhook/{tenant_slug}/{provider}`). The HMAC has
     to cover the body AND be scoped to that tenant's secret — see
     `/audit-webhooks`.

5. **JWT decode discipline.** `app/utils/jwt_hs256.decode` is the
   single chokepoint that enforces `algorithms=["HS256"]` (joserfc) and
   raises one `InvalidTokenError`, which `app/api/deps.decode_token` turns
   into a 401 and the MFA decoders into a `ValueError`. `services/sso.py`
   (IdP ID tokens, asymmetric algorithms only) is the one other allowed
   `jwt.decode`. Any other call site that does `jwt.decode(...)`
   inline (especially with a permissive `algorithms=` argument) is a
   finding. `get_tenant` is allowed to call `decode_token` for the
   cross-tenant guard; nothing else should reimplement it.

6. **Role gating on privileged actions.** Authentication alone is not
   enough on money-moving / config-changing endpoints. Approve, reject,
   create payment run, execute payment run, void payment, mint card,
   change RBAC, change org settings — all require
   `Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO))` (or
   the appropriate subset). A new privileged endpoint with only
   `Depends(get_current_user)` and no `require_roles` is a finding.

7. **Vendor-portal vs employee tokens.** Two JWT shapes coexist:
   `typ` defaults to employee, `typ=vendor` is the supplier-portal
   token. The two `get_current_*` helpers must not be interchangeable.
   A new portal route gated by the employee `get_current_user` (or
   vice versa) lets one auth flow into the other's surface. The
   cross-tenant guard in `get_tenant` is keyed on `typ != "vendor"`
   precisely so vendor tokens can resolve against the per-tenant
   `VendorUser` table without colliding with the employee `org` claim.

## Report

Group findings by severity:

- **Critical** — a route exposes tenant data to anonymous callers; a
  route lets one tenant read/write another tenant's rows because the
  JWT/header cross-check was bypassed; JWT decoded with a permissive
  algorithm whitelist; vendor JWT accepted on an employee endpoint
  (or vice versa).
- **High** — `current_user.*` access reachable in a path that isn't
  gated by `get_current_user`; a privileged action behind plain
  `get_current_user` instead of `require_roles(...)`; a file-key /
  opaque-id handler that doesn't verify owner-scope.
- **Medium** — new control-plane query inside a tenant-data route
  without a comment explaining why; a per-route auth check that
  duplicates (and could drift from) the central deps.
- **Low** — public route in either allowlist missing a reason
  explaining why it's public.

For each: file:line, the concrete fix, the worst-case blast radius.

## Useful starting points

- `backend/app/main.py` — every `app.include_router(...)` line plus
  the two inline public routes (`/api/health`, `/api/public-config`).
- `backend/app/api/deps.py` — `decode_token`, `get_current_user`,
  `get_org_id`, `require_roles`, `ROLE_*` constants.
- `backend/app/tenant.py` — `get_tenant`, `get_tenant_db`, the
  cross-tenant guard the latter relies on.
- `backend/tests/test_rbac.py` — the `PUBLIC_BY_DESIGN` / `ALTERNATE_AUTH` allowlists and
  the gate that fails on any unlisted unauthenticated route.
- The four webhook handlers — see `/audit-webhooks` for the deeper
  signature / dedup audit.

## Delegate to

Use the `repo-security-auditor` agent: `"Audit auth gating and
tenant-context enforcement across the FastAPI backend."`

Read-only. Findings only.
