# Multi-Tenancy

The application uses **subdomain-based routing** with **database-per-tenant isolation**. Each tenant gets a unique subdomain and their own PostgreSQL database.

## Architecture

```
acme.localhost:7777 ──┐                        ┌── feoh_acme DB (invoices, vendors, ...)
                      ├── Backend API :8000 ────┼── feoh_techflow DB
techflow.localhost:7777┘   (shared)             └── feohledger DB (control plane)
```

- **Control-plane DB** (`feohledger`): organizations, users, roles
- **Tenant DBs** (`feoh_<slug>`): invoices, vendors, payments, workflows, exceptions

## How It Works

### 1. Frontend extracts subdomain

`src/lib/tenant.ts` reads the subdomain from the browser URL:
- `acme.localhost:7777` → slug `acme`
- `techflow.app.com` → slug `techflow`
- `localhost:7777` (no subdomain) → shows "no tenant" page

#### Signing in from the apex (`/find-workspace`)

The apex (`feohledger.com`, `localhost:7777`) has no sign-in form: a user
belongs to one workspace and signs in at its host. The landing page's **Sign
in** links, and an apex `/login`, go to `/find-workspace`, which asks for the
workspace address, checks its format (the client half of `utils/slug.py`, in
`frontend/src/lib/utils/workspaceAddress.ts`) and sends the browser to that
workspace's `/login`, built from `tenant_url_template` in `GET
/api/public-config`. A full domain that is not the platform's — a workspace on its own custom
domain — is offered back as a link to that host's `/login` rather than refused
as a malformed name. If the config cannot be loaded it says so and offers Retry;
it never guesses a host from the current URL.

**There is deliberately no "enter your email, we'll find your workspace"
lookup.** An unauthenticated endpoint that maps an email address to a workspace
tells anyone who asks whether that person is a customer, and at which company —
an account-enumeration oracle, the same disclosure the forgot-password flow is
written to avoid. The address is in the welcome email and every notification a
workspace sends, so the visitor already has it; a slug that names no workspace
simply lands on a sign-in page that fails like any other. Adding such a lookup
would need the same answer-for-everyone design as forgot-password (email the
links, say the same thing whether or not anything matched) and a security
review first.

### 2. Frontend sends tenant header

Every API request includes an `X-Tenant-Slug` header (added automatically by `src/lib/api.ts`).

### 3. Backend resolves tenant

The dependency chain in `app/tenant.py`:
```
X-Tenant-Slug header + Authorization Bearer <JWT>
  → get_tenant_slug()        # extract slug from header
  → get_tenant()             # look up org in control-plane DB by slug
                             # CROSS-CHECK: payload["org"] must equal org.id
                             #              for employee tokens (typ != "vendor")
                             #              → 403 on mismatch
  → get_tenant_db()          # yield a session on the tenant's DB
```

The cross-check in `get_tenant` is the load-bearing security control. Without it, an authenticated user from tenant A could read or mutate tenant B's data by sending `X-Tenant-Slug: <other-tenant>`. Vendor-portal tokens (`typ="vendor"`) are exempt — they're scoped naturally because `VendorUser` rows live in the per-tenant DB. See `docs/authentication.md § Cross-tenant guard`.

### Engine construction is the other half of the guard

`get_tenant` being *correct* is only useful if it is *used*. A tenant session is
never more isolated than the URL its engine was built from, and a URL is a
string — a module that writes

```python
create_async_engine(f"{base}/feoh_{slug}")     # slug straight off the request
```

reaches a tenant database with no `Organization` row behind it, so every check
in `get_tenant` sits upstream of a call that never happens. `tests/test_tenant_isolation.py`
would still be green.

So the construction discipline is asserted too, by
**`backend/tests/test_tenant_engine_construction.py`** — a pure-AST scan over
`backend/app/`, no database, sub-second. It enforces five things:

1. **One builder, one body.** `app/tenant_url.py` owns the construction:
   `make_tenant_url(base_url, db_name)` is the single place a tenant DB name
   becomes a URL. `app/database.py` binds it to `settings.database_url` as
   `_make_tenant_url(db_name)`, which is what the ~70 in-app call sites spell.
   Every engine built elsewhere must be handed one of those two calls (tenant) or
   the control-plane URL — `settings.database_url` in the app,
   `control_url_from_env()` on a Lambda path (control). The scan follows renamed
   imports (`create_async_engine as make_engine`), covers the synchronous
   `create_engine` twin, and treats a `**kwargs`-hidden or later-mutated URL as
   unresolved, so the easy ways around it fail closed.
2. **The constructor is never renamed.** A constructor bound to another name
   (`engine_factory = create_async_engine`) or passed as a value takes every call
   through it *out of the enumeration* rather than into the offender list, so a
   non-call reference is refused outright.
3. **No interpolated URLs.** No f-string, `%`, or `.format()` anywhere in an
   engine's URL expression — the Lambda handlers included.
4. **Nobody re-implements the builder.** The builder's body
   (`<base>.rsplit("/", 1)[0] + "/" + <name>`) may appear in `app/tenant_url.py`
   and nowhere else, and that module must stay import-free (`os` aside). Those two
   rules are a pair: the three AWS Lambda handlers (`extraction_lambda`,
   `erp_lambda`, `audit_lambda`) cannot import `app.database` — it reaches
   `app.config`, and backend/CLAUDE.md forbids dotenv-reaching imports on a Lambda
   path — so they used to carry their own copies of that line, and the guard held
   all three to *mirroring* the original. A dependency-free
   `app/tenant_url.py` lets them import the real thing, which turns a guard over a
   permanent duplication into a guard against any duplication.
5. **No hardcoded tenant DB names.** A `feoh_`-prefixed database-name literal
   appears nowhere outside `app/config.py`, which owns `tenant_db_prefix`.

The `tenant-url-interpolation` and `hardcoded-tenant-db-name` rules in
`.claude/hooks/security-patterns.sh` catch the same two shapes earlier, at edit
time. They are line-based greps, so they see a URL interpolated *at* the call —
positionally or as `url=` — but not one assembled on an earlier line and passed
by name, and not a renamed constructor. The pytest guard resolves both; the hook
is the fast first pass, never the backstop.

What no static rule can prove is that a given `db_name` came from a *resolved
row* rather than from the request. What the guard does prove is that every call
site goes through the one helper whose only caller-visible input is a `db_name`,
which turns reviewing a new engine site into a one-line question instead of an
audit. `make_tenant_url` additionally refuses a name that would *re-shape* the
URL — one carrying `/`, `?`, `#`, `@`, `:`, `%` or whitespace would append a
path, start a query, or move the authority section — so the weaker property that
IS checkable is checked (`backend/tests/test_tenant_url.py`).

**The SQS/Lambda workers re-derive too.** `audit_lambda` used to take
`tenant_db_name` straight off the message body: the producer had resolved it from
an `Organization` row at *publish* time, but the consumer's routing input was
still a string on a queue. It now looks the org up from `organization_id` exactly
as `extraction_lambda` and `erp_lambda` do, and ignores the field (which stays on
the wire for in-flight messages). An unresolvable org **raises** rather than
returning silently — unlike the other two handlers, whose message asks for work
that can be re-requested, an audit message is the only copy of an append-only
event, so it goes back to SQS and ultimately to the dead-letter queue.

### Custom-domain fallback (white-label vanity hostnames)

When the `X-Tenant-Slug` header is **absent**, `get_tenant_slug` falls back to matching the request `Host` against the per-org `settings.brand.custom_domains` JSON array, so a tenant served on its own vanity hostname (`ap.acmecorp.com`) resolves to its slug without the SPA supplying the header. An unknown / unmatched host (or a malformed settings blob) falls back to the original `400` — never a wrong tenant. **The fallback only picks a *candidate* slug; the `get_tenant` JWT `org`-claim cross-check above still gates it**, so a forged `Host` header can no more widen access than a forged `X-Tenant-Slug` header can. The list is managed by tenant admins via `GET/PUT /api/organization/branding/custom-domains` (admin-only mutate; each host normalized through the same `normalize_custom_domain` the resolver uses, **cross-org-unique** — a host registered to another tenant is `409` — and audited count-only) + the Organization → Custom Domains panel. See `docs/white-label.md § Custom domains` for the full trust model.

### 4. Routes use the correct database

- **Auth routes** (`/api/auth/*`) use the control-plane DB — users and orgs live there
- **Business routes** (`/api/invoices`, `/api/vendors`, `/api/dashboard`) use the tenant DB — data is fully isolated per tenant. Every route flows through `get_tenant` so the cross-check fires uniformly.

## Database Layout

### Control-plane DB (`feohledger`)

| Table                  | Purpose                                                          |
|------------------------|------------------------------------------------------------------|
| `organizations`        | Tenant registry (slug, db_name, plan, settings JSONB)            |
| `users`                | All users across all tenants — incl. SSO + MFA fields            |
| `roles`                | Role definitions (admin, ap_manager, ap_clerk, cfo)              |
| `user_roles`           | User-role assignments                                            |
| `email_verifications`  | Pending self-service signups (token, slug, expires_at)           |
| `extraction_usage`     | Per-invoice billing rows for platform extraction                 |
| `card_rebates`         | Per-virtual-card rebate billing rows                             |

### Tenant DB (`feoh_<slug>`)

| Table                        | Purpose                    |
|------------------------------|----------------------------|
| `invoices`                   | Invoice records            |
| `invoice_line_items`         | Line-level detail          |
| `invoice_extraction_results` | AI extraction output       |
| `vendors`                    | Vendor master data         |
| `purchase_orders`            | PO headers                 |
| `po_line_items`              | PO line detail             |
| `goods_receipts`             | GR headers                 |
| `gr_line_items`              | GR line detail             |
| `workflow_definitions`       | Approval workflows         |
| `workflow_instances`         | Active workflow runs        |
| `workflow_steps`             | Individual step records     |
| `audit_log`                  | Immutable event log        |
| `payment_runs`               | Batch payment runs         |
| `payment_schedules`          | Payment timing             |
| `payments`                   | Individual payments        |
| `exceptions`                 | Flagged issues             |
| `credit_memos`               | Vendor credit memos        |
| `bank_statements`            | Uploaded statement files (for reconciliation) |
| `bank_transactions`          | Parsed transactions matched against payments |
| `sanctions_checks`           | Append-only KYC / sanctions screening trail |
| `scheduled_reports`          | Recurring CFO-report definitions |
| `invoice_embeddings`         | Vector embeddings for RAG + duplicate detection |
| `vendor_extraction_priors`   | Cached vendor field priors fed to next extraction |
| `vendor_users`               | Supplier-portal credentials (scoped to a Vendor) |
| `card_reveal_tokens`         | Single-use PAN-reveal tokens for vendors |

Tenant tables still have an `organization_id` column (as a plain UUID, not a foreign key) for backward compatibility.

## Provisioning a New Tenant

```bash
cd backend
source .venv/bin/activate
python scripts/create_tenant.py \
  --name "New Corp" \
  --slug newcorp \
  --admin-email admin@newcorp.com \
  --admin-password changeme
```

This:
1. Creates `feoh_newcorp` database on the Postgres server
2. Inserts the org row into the control-plane DB
3. Creates the admin user in the control-plane DB
4. Creates all tenant tables in `feoh_newcorp`

The new tenant is immediately accessible at `newcorp.localhost:7777`.

## Migrations

### Control-plane DB

```bash
cd backend
alembic upgrade head
```

### Single tenant DB

```bash
FEOH_MIGRATE_TENANT=feoh_acme alembic upgrade head
```

### All tenant DBs

```bash
python scripts/migrate_all_tenants.py
```

## CORS

The backend uses a regex-based CORS policy to allow any subdomain:

```python
allow_origin_regex=r"https?://([\w-]+\.)?(localhost(:\d+)?|app\.com)"
```

This accepts `acme.localhost:7777`, `techflow.localhost:7777`, `acme.app.com`, etc.

## Local Development

### DNS

`*.localhost` resolves to `127.0.0.1` natively in Chrome, Firefox, and Edge. No `/etc/hosts` changes needed.

For Safari, add entries manually:

```
# /etc/hosts
127.0.0.1 acme.localhost techflow.localhost
```

### Dev URLs

| Tenant    | URL                           | Login                          |
|-----------|-------------------------------|--------------------------------|
| Acme      | http://acme.localhost:7777    | `demo@acme.com` / `demo`      |
| TechFlow  | http://techflow.localhost:7777| `admin@techflow.com` / `demo`  |

### Docker

The `docker-compose.yml` mounts `init-tenants.sql` which auto-creates `feoh_acme` and `feoh_techflow` databases on first run. For a fresh start:

```bash
docker compose down -v
docker compose up -d
python scripts/seed.py
```

## Engine Pool Management

Each tenant gets a lazily-created `AsyncEngine` with `pool_size=5, max_overflow=10`. Engines are cached in `app/database._tenant_engines` and disposed on shutdown. For production with many tenants, consider adding a connection pooler like PgBouncer.
