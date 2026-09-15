# Self-service tenant signup

End-to-end flow for anonymous visitors to provision their own workspace. Landing page → signup form → verification email → workspace created → welcome email → first login → forced password change.

## User journey

1. Visitor lands on the apex domain (no subdomain) and clicks **Create your workspace**.
2. `/signup` form: company name, desired slug, admin name, admin email, hCaptcha.
3. Form submits `POST /api/signup/start`. Backend validates, records an `EmailVerification` row, and emails a verification link (`PUBLIC_URL/verify?token=...`).
4. User clicks the link. `/verify?token=...` calls `POST /api/signup/complete`. Backend re-checks availability, provisions the tenant (DB + org + admin user), generates a 16-char temp password, sends the welcome email with tenant URL + credentials.
5. User receives the welcome email, clicks the tenant URL, signs in with the temp password.
6. Login response sets `must_change_password=true` on the `/auth/me` user object. The frontend layout redirects to `/change-password`.
7. User sets a new password (min 12 chars, upper/lower/digit). Backend clears the flag. User is taken to the dashboard.

## Backend components

| Layer | File |
|-------|------|
| Router | `app/api/signup.py` |
| Router (change-password) | `app/api/auth.py` → `change_password()` |
| Provisioning service | `app/services/tenant_provisioning.py` |
| Email dispatcher | `app/services/email_adapters/` (`console`, `ses`) |
| Rate limiter (Redis) | `app/services/rate_limit.py` |
| Slug validator | `app/utils/slug.py` |
| Captcha verifier | `app/utils/hcaptcha.py` |
| Password helpers | `app/utils/passwords.py` |
| Models | `app/models/signup.py` (`EmailVerification`) + `User.must_change_password` |
| Migration | `alembic/versions/0001_self_service_signup.py` |

## Frontend components

| Route | File |
|-------|------|
| Landing (no tenant) | `src/routes/+layout.svelte` (inline) |
| Signup form | `src/routes/signup/+page.svelte` |
| Verify handler | `src/routes/verify/+page.svelte` |
| First-login redirect | `src/routes/+layout.svelte` (effect) |
| Change password | `src/routes/change-password/+page.svelte` |

## Abuse mitigations

- **Captcha**: hCaptcha on `POST /signup/start`. Skipped locally when `FEOH_HCAPTCHA_SECRET` is empty — but a **deployed** env (`FEOH_ENVIRONMENT` not in `development`/`test`/`ci`/…) **refuses to boot** with the secret empty, so the gate can't silently fail open in production. The empty-secret skip logs at WARNING.
- **Rate limit (per IP)**: Redis sliding window keyed by IP+endpoint. `FEOH_SIGNUP_RATE_LIMIT_PER_HOUR` (default 5) caps `/signup/start` and `/signup/complete`; `/signup/slug-check` has its own higher cap (`FEOH_SLUG_CHECK_RATE_LIMIT_PER_HOUR`, default 120) so it can't be scripted for namespace enumeration / control-plane DB amplification.
- **Rate limit (per email)**: a second limiter keyed on the target address (`FEOH_SIGNUP_EMAIL_RATE_LIMIT_PER_HOUR`, default 3) caps verification-email volume to one victim — the per-IP limit alone can't stop an attacker rotating IPs to email-bomb an address. A "resend" also **replaces** any prior un-consumed verification for that email, so the table can't grow unbounded per address.
- **Email verification**: no resources are provisioned until the user proves inbox access by clicking the link. Stolen email addresses don't result in tenants.
- **Single-use, race-safe tokens**: `/signup/complete` selects the verification row `FOR UPDATE`, so two concurrent completes for one token can't both pass the consumed-check and double-provision. Every non-actionable token state (missing / consumed / expired) returns the **same** 410 + message, so the response can't be used to tell a token that never existed from one that did.
- **Slug squatting**: slugs are only locked when `/complete` runs. Abandoned `/start` submissions do not reserve the namespace.
- **Reserved subdomains**: `utils/slug.py` → `RESERVED_SLUGS` blocks names that would collide with marketing/infra subdomains (`www`, `api`, `admin`, etc.). The check is enforced inside `provision_tenant` too, before any database work, so the operator CLI (`scripts/create_tenant.py`, and `deploy/add-tenant.sh` behind it) cannot provision a reserved or malformed slug; the CLI exits 2 with the reason.
- **No orphan databases**: if provisioning fails after the tenant DB is created (control-plane insert or table creation errors), the DB this attempt created is dropped, so partial failures don't leak databases or squat the namespace.
- **Log hygiene**: the submitter's email (PII) and raw captcha-network exception text are kept out of logs.
- **Partial-failure visibility**: if the welcome email fails after provisioning, the tenant is usable but the admin needs a password reset. Logs warn loudly; a support runbook should resend manually.

## Email

Pluggable via `FEOH_EMAIL_PROVIDER`:

- `console` (default for local dev) — logs the full message to stdout.
- `ses` — AWS SES via boto3. Requires the sending domain to be verified and the runtime IAM role to have `ses:SendEmail`.

Adding a provider: copy `app/services/email_adapters/console_adapter.py`, implement `send()` + `test_connection()`, decorate with `@register_email_adapter("<name>")`, and import it in `__init__.py`.

## Config (env vars)

| Var | Default | Notes |
|-----|---------|-------|
| `FEOH_EMAIL_PROVIDER` | `console` | `console` or `ses` |
| `FEOH_EMAIL_FROM` | `no-reply@localhost` | Verified SES sender in prod |
| `FEOH_AWS_SES_REGION` | `us-east-1` | Only used by the SES adapter |
| `FEOH_PUBLIC_URL` | `http://localhost:7777` | Frontend URL — used to build verification links |
| `FEOH_TENANT_URL_TEMPLATE` | `http://{slug}.localhost:7777` | `{slug}` is substituted in the welcome email |
| `FEOH_ENVIRONMENT` | `development` | Deployment discriminator. Any value outside `development`/`dev`/`local`/`test`/`ci` is "deployed" and turns on the production safety guards (e.g. captcha must be configured). |
| `FEOH_HCAPTCHA_SECRET` | *(empty)* | Empty skips verification — OK for local dev; a deployed env refuses to boot when empty. |
| `FEOH_HCAPTCHA_SITEKEY` | *(empty)* | Exposed to the frontend via `/api/public-config` |
| `FEOH_SIGNUP_RATE_LIMIT_PER_HOUR` | `5` | Per-IP cap on `/signup/start` (and `/signup/complete`) |
| `FEOH_SIGNUP_EMAIL_RATE_LIMIT_PER_HOUR` | `3` | Per-email cap on verification sends (anti email-bombing) |
| `FEOH_SLUG_CHECK_RATE_LIMIT_PER_HOUR` | `120` | Per-IP cap on `/signup/slug-check` (anti-enumeration) |

## Migration

The feature adds a `must_change_password` boolean to `users` and a new `email_verifications` table (control plane only). The migration uses `IF NOT EXISTS` everywhere so running it on a fresh dev DB bootstrapped via `create_all()` is a no-op on the pre-existing bits.

```bash
cd backend
alembic upgrade head   # applies 0001_self_service_signup
```

Tenant DBs are unaffected — both changes live in the control plane.

## Testing the flow locally

1. `cd backend && docker compose up -d && python main.py`
2. `cd frontend && pnpm dev` (runs on port 7777)
3. Visit http://localhost:7777 — land on the CTA.
4. Click **Create your workspace** → fill in the form (leave captcha empty, it's disabled locally).
5. Watch the backend logs — the verification email is printed to stdout. Copy the verify link and paste it into your browser.
6. The verify page shows progress → success. Watch logs again for the welcome email with the temp password.
7. Visit `http://<slug>.localhost:7777`, sign in with the temp password, and you'll be redirected to `/change-password`.
