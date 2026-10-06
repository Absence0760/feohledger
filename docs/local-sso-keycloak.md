# Local IdP testing — Keycloak (SSO) + Authentik (SCIM)

Two identity flows, two local containers, both under the Docker Compose `idp`
profile (`pnpm idp:up`). This is the "local-first" guard rail applied to
identity: each external dependency (a cloud IdP) ships a local equivalent and a
safe default (both are **off** until you opt in — password login always works).

| Flow | Direction | Local IdP | Stand-in for |
|---|---|---|---|
| **OIDC SSO** | app *pulls* from the IdP | **Keycloak** (:8088) | Okta / Entra login |
| **SCIM provisioning** | IdP *pushes* into the app | **Authentik** (:9002) | Okta / Entra SCIM |

- SSO: a single generic OIDC flow (`backend/app/api/auth_sso.py`,
  `backend/app/services/sso.py`) — every provider-specific detail comes from the
  discovery doc, so Keycloak locally behaves like Okta/Entra in prod.
- SCIM: the app is the SCIM *Service Provider* (`backend/app/api/scim.py`,
  `/api/scim/v2/Users`); the IdP is the SCIM *client* that pushes users in with a
  per-tenant bearer token. Authentik drives that path locally.

The Keycloak setup is below; jump to [Authentik — local SCIM
provisioning](#authentik--local-scim-provisioning) for SCIM.

## Keycloak — local SSO

## TL;DR

```bash
pnpm idp:up        # start Keycloak (Docker, opt-in `idp` profile) on :8088
pnpm idp:seed      # point the acme tenant's settings.sso at local Keycloak
pnpm dev           # backend :8000 + frontend :7777
# open http://acme.localhost:7777 → "Sign in with SSO" → demo@acme.com / demo
pnpm idp:down      # stop Keycloak when done
```

`pnpm idp:seed --slug techflow` configures a different tenant; `python
backend/scripts/enable_keycloak_sso.py --disable` (or `pnpm idp:seed` with
`--disable` appended) removes the SSO block and reverts to password-only login.

## What's provisioned

The `keycloak` service in `backend/docker-compose.yml` runs in dev mode and
re-imports `backend/keycloak/realm-export.json` on every boot, so the IdP is
always a clean, reproducible state (no persisted volume).

| Thing | Value |
|---|---|
| Keycloak admin console | http://localhost:8088 (`admin` / `admin`) |
| Realm | `feohledger` |
| Discovery URL | `http://localhost:8088/realms/feohledger/.well-known/openid-configuration` |
| Client ID | `feohledger-app` |
| Client secret | `local-dev-keycloak-secret` |
| Test user (links to seeded admin) | `demo@acme.com` / `demo` |
| Test user (JIT-provisions new) | `newhire@acme.com` / `demo` |

`pnpm idp:seed` writes exactly these values into `Organization.settings.sso`
for the chosen tenant (the same shape Okta/Entra would use), merging into the
existing settings rather than overwriting `cards` / `payments` / etc.

The seed is a **dev shortcut that writes the row directly**: it bypasses the
product's one writer of `settings.sso`, `PUT /api/organization/sso` (the
`/organization` → **Single Sign-On** panel), so it is unaudited and replaces the
whole `sso` block, SCIM keys included — run `pnpm scim:seed` after it, not
before. To exercise the real path, configure the same values in the panel
instead (`docs/authentication.md` § SSO configuration). `PATCH
/api/organization` refuses an `sso` key.

## Why port 8088 (not 8080)

Keycloak's own default is 8080, but that port is commonly taken by other local
dev tooling, so this repo maps the host port to **8088**. Keycloak in dev mode
derives its issuer and endpoint URLs from the request `Host` header, so hitting
it on `localhost:8088` yields an issuer of
`http://localhost:8088/realms/feohledger` with no extra hostname config —
which is what keeps the backend's discovery host check (`auth_sso.py`,
authorize-host-must-match-discovery-host) happy.

## How the two test users differ

Both demonstrate a JIT-provisioning path in `_jit_provision`:

- **`demo@acme.com`** already exists as the seeded acme admin (a password user).
  The first SSO login matches by email and *links* SSO to that existing row
  (`sso_provider` / `sso_provider_id` get set) — the user keeps their admin role.
- **`newhire@acme.com`** has no app-side row. The first SSO login *creates* one
  and assigns the least-privilege `ap_clerk` role (admins elevate later in the
  admin UI).

## Exercising the email-domain allowlist

The realm returns the standard `email`, `email_verified`, `sub`, `name`, and
`preferred_username` claims. To test the optional JIT allowlist, edit
`KEYCLOAK_SSO_CONFIG["allowed_email_domains"]` in
`backend/scripts/enable_keycloak_sso.py` to `["acme.com"]`, re-run `pnpm
idp:seed`, then try a user whose email is outside the domain — the callback
returns 403 and writes an `auth.sso.login.failure` audit row with
`reason: domain_blocked`.

## Troubleshooting

- **"SSO is not configured correctly."** on authorize — the discovery host and
  the authorize-endpoint host disagree. Usually means Keycloak was reached on a
  different host/port than what's in `settings.sso.discovery_url`. Keep both on
  `localhost:8088`.
- **Login session expired** on callback — the OIDC `state` lives in Redis with a
  10-minute TTL (`FEOH_SSO_STATE_TTL_SECONDS`). Make sure `pnpm db:up` (Redis) is
  running and you completed the flow within the window.
- **Keycloak slow to accept connections** — first boot pulls the image and
  imports the realm; give it ~10-15s. `pnpm idp:logs` shows progress; it's ready
  when the discovery URL returns JSON.

## Authentik — local SCIM provisioning

Authentik is the SCIM *client*: it pushes users into the app's SCIM Service
Provider (`backend/app/api/scim.py`). It's the local-first stand-in for Okta /
Entra SCIM, so outbound provisioning can be exercised with no cloud account.

### TL;DR

```bash
pnpm idp:up        # starts Keycloak + the Authentik stack (Docker)
pnpm scim:seed     # set the matching SCIM bearer token on the acme tenant
pnpm dev           # the app must be running — Authentik POSTs to :8000
# Authentik admin: http://localhost:9002  (akadmin / admin)
#   Applications → Providers → "FeohLedger SCIM" → Run sync
# Provisioned users land in acme; see them at http://acme.localhost:7777/admin
pnpm idp:down      # stop the IdP stack when done
```

`pnpm scim:seed --slug techflow` targets another tenant; append `--disable`
(`python backend/scripts/enable_authentik_scim.py --disable`) to clear the token.

### What's provisioned

The Authentik stack is **self-contained** (its own Postgres + Redis, not the
app's) and applies `backend/authentik/blueprints/feohledger-scim.yaml` on
boot, so it's reproducible. The blueprint creates:

| Thing | Value |
|---|---|
| Authentik admin | http://localhost:9002 (`akadmin` / `admin`) |
| API token (for scripting) | `local-dev-authentik-api-token` |
| SCIM provider | "FeohLedger SCIM" → `http://host.docker.internal:8000/api/scim/v2` |
| SCIM bearer token | `local-dev-scim-token-acme` (matches `pnpm scim:seed`) |
| Demo user to sync | `scim.demo@acme.com` |

The app resolves the tenant from the token's sha256 (`Organization.scim_bearer_hash`),
so `pnpm scim:seed` must run before a sync or every push 401s.

### How sync works

Authentik runs a full sync on a schedule and a direct sync when an assigned user
changes. On sync it `POST`s new users to `/Users` (201), `PUT`s the full
resource on updates (200), and `PATCH`es `active=false` / `DELETE`s on
deprovision. By default it syncs **all** non-service-account Authentik users
(so `akadmin` comes along with `scim.demo`); scope it to a group via the
provider's *filter group* if you want just one.

### Users only, no groups

The app implements SCIM `/Users`, not `/Groups` (by design — see the
`scim.py` docstring). The blueprint therefore sets `property_mappings_group: []`
so Authentik doesn't `POST /Groups` and get a 404. If you re-enable group
mappings, expect group-sync errors in the Authentik worker log.

### Automated coverage

The deterministic Playwright e2e `frontend/tests-e2e/scim/provisioning.spec.ts`
(`pnpm test:scim`) drives this exact contract — create → filter → PUT → PATCH
deactivate → DELETE — and verifies the effect in `/admin`, with the bearer token
minted through the real admin endpoint. It runs in CI without the Authentik
container (which CI can't host), so SCIM has coverage in the normal pipeline;
Authentik is the hands-on local complement. That spec is also the regression
guard for two bugs the live Authentik run surfaced: SCIM `PATCH` 500'ing on the
`updated_at` onupdate-expiry, and `PUT` (Authentik's update verb) being
unimplemented (405).

### Troubleshooting (Authentik)

- **Pushes 401** — token not seeded (or seeded on the wrong tenant). Run
  `pnpm scim:seed`; the bearer must equal the blueprint's `token:`.
- **First boot is slow** — Authentik runs DB migrations on first start (~1 min).
  `pnpm idp:logs` shows progress; ready when `http://localhost:9002/-/health/ready/`
  returns 200.
- **Group sync errors in the worker log** — expected only if group mappings are
  on; the app has no `/Groups`. Keep `property_mappings_group: []`.
- **Authentik can't reach the app** — the SCIM URL uses `host.docker.internal`
  (Docker host-gateway); the app must be running on the host at `:8000`.

See also: `docs/authentication.md` § SSO + § SCIM, `backend/docs/docker.md`.
