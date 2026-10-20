# Self-service tenant signup

End-to-end flow for anonymous visitors to provision their own workspace. Landing page → signup form → verification email → workspace created → **set your password** (one single-use link, shown on the verify page and in the welcome email) → sign in. No password is ever generated or emailed (`docs/decisions.md` §283).

## Turning signup off — `FEOH_SIGNUP_ENABLED`

Self-service signup is **on by default** — `pnpm dev`, the signup e2e and every
existing deployment keep working unchanged. Set `FEOH_SIGNUP_ENABLED=false` on a
deployment whose tenants are all provisioned by an operator
(`deploy/add-tenant.sh`, `scripts/create_tenant.py`), such as an invite-only
pilot. With it off:

- **All three `/api/signup/*` routes answer a bare `404 {"detail": "Not Found"}`**
  — the body an unmounted route returns. The check is a router-level dependency
  (`api/signup.py::_require_signup_enabled`), so it runs before the request body
  is validated, before a database session is opened and before any rate limit is
  spent: a closed deployment creates no `EmailVerification` row, consumes no
  verification token, and a probe cannot burn a real visitor's per-IP budget.
  404 rather than a "disabled" 403 follows the cash-flow copilot's and the
  public API's kill switches — the response does not enumerate a feature that is
  off.
- **`/api/public-config` reports `signup_enabled: false`**, and `/signup` renders
  a "Signup is closed" notice (no form, no captcha widget, no slug check, no
  "what happens next" panel) instead of a form every submit of which would fail.
  The landing page's calls to action still lead there, which is deliberate —
  see [decisions.md](decisions.md) §193.
- **The hCaptcha secret stops being required.** The deployed-env boot check
  (`config.py::_require_captcha_in_deployed_envs`) and `deploy/decrypt-env.sh`
  demand `FEOH_HCAPTCHA_SECRET` only while signup is on: with nothing public
  creating tenants there is nothing for a captcha to protect.
- A verification link already emailed while signup was open stays unconsumed;
  re-opening signup lets that visitor finish.

**What production should set:** a deployment that takes self-service customers
keeps the default (`true`) and **must** set `FEOH_HCAPTCHA_SECRET` and
`FEOH_HCAPTCHA_SITEKEY` — it refuses to boot without the secret. A deployment
whose tenants are provisioned by hand sets `FEOH_SIGNUP_ENABLED=false` and may
leave both hCaptcha values empty.

The two hCaptcha values must come from the **same hCaptcha account**: the
sitekey from the site's entry under *Sites*, the secret from *Settings →
Secrets → Account-level Secret* (not the API key, which `/siteverify`
rejects). A pair from two accounts fails every signup with
`sitekey-secret-mismatch` in the API log, while the widget itself renders and
solves normally. The site's hostname is the apex (`feohledger.com`), where
`/signup` lives; hCaptcha rejects a wildcard. Quote both values in the sops
file: a secret starting `0x` is otherwise read by YAML as a number.

## User journey

1. Visitor lands on the apex domain (no subdomain) and clicks **Create your workspace**.
2. `/signup` form: company name, desired slug, admin name, admin email, hCaptcha (explicit render, in whichever theme the page is showing — `theme.resolved` — and redrawn when that changes unless the visitor has already solved it, decisions §289, §290). The page's theme picker (every `AuthShell` page carries one) writes the shared `feoh_theme` cookie on the platform domain, so a theme chosen here is the one the new workspace opens in.
3. Form submits `POST /api/signup/start`. Backend validates (including refusing an admin name that holds a web or email address: the name greets the reader of an email sent to whatever address the visitor typed, and mail clients auto-link a bare URL even in inert text), records an `EmailVerification` row, and emails a verification link (`PUBLIC_URL/verify?token=...`).
4. User clicks the link. `/verify?token=...` calls `POST /api/signup/complete`. Backend re-checks availability and provisions the tenant (DB + org + an admin user with **no password** — `hashed_password` NULL, the representation SSO-only users already have, so `/auth/login` refuses the account until a password exists; `must_change_password=false`). It then mints a **set-password link**: a forgot-password reset token (`services/password_reset.issue_reset_token`, same Redis store, keyed digest and single-use `GETDEL`) with a longer life, `FEOH_SIGNUP_SET_PASSWORD_TTL_HOURS` (default 72), built by the same `utils/tenant_urls.password_reset_url` as the reset email plus `&mode=set`. The link is returned to the browser (`set_password_url`) **and** emailed in the welcome message; it is one token, so whichever copy is used first spends the other.
5. The verify page shows **Set your password**, a button straight to `<tenant>/login/reset-password?token=…&mode=set`. That page (the existing reset page) reads the token with `POST /api/auth/reset-password/check` — a read that never consumes it, so a mail scanner opening the link, or a reload, leaves it usable — fills a hidden `autocomplete="username"` field with the account email so a password manager saves the new password under it, and redeems the token with `POST /api/auth/reset-password` on submit. Only that submit spends the token.
6. The confirmation links to `/login?email=<address>`; the login page prefills its email field from `?email=` (`frontend/src/lib/utils/prefillEmail.ts`, a plausible single address only). The user signs in with the password they chose — no forced change.
7. An expired or used link explains itself on the reset page and offers **Forgot password**, which works for this account like any other (it needs only an active user, not an existing password).

### Opening the verification link again

`/verify` redeems the token on page load, so a reload, a second tab, or a mail scanner that ran the page first all reach `/complete` with a token that is already spent. A spent, unexpired token whose slug now names an organization holding an account with the link's address is **re-answered, not re-run**: `200` with `already_verified: true`, the workspace's URL and `sign_in_url`, and `set_password_url: null`. Nothing is issued: no second set-password link, no welcome email, no audit row. The page then shows **Your workspace is ready** with a **Go to sign in** button and says the set-password link is in the welcome email (or Forgot password works). A spent token that created no workspace, or one past its 24-hour life, is still the uniform `410`. Why a re-answer rather than a redeem button: [decisions.md](decisions.md) §292.

### An address that already has an account

`users.email` is unique across every organization, so an address that already has an account can never be provisioned. `/signup/start` answers it **exactly like any other address** and sends the verification link, because a public endpoint must not tell anyone which addresses have accounts. `/signup/complete`, reached only by someone who can read that mailbox, checks the address (case-insensitively) before the slug and before provisioning, and answers `409`:

```json
{"detail": {"code": "signup_email_has_workspace",
            "message": "This email address already has a FeohLedger workspace. Sign in at https://<their-workspace>/login?email=….",
            "params": {"sign_in_url": "https://<their-workspace>/login?email=…"}}}
```

`sign_in_url` is the **existing** account's sign-in page, built like the wrong-workspace login refusal's (§288). The verification is spent when the refusal is given, so the link never reaches provisioning again, and re-opening it repeats the same refusal. The `/verify` page shows **You already have a workspace**, the localized sentence and a **Go to your workspace's sign-in page** button. If the account appears between the check and provisioning, `provision_tenant` fails on the unique index, drops the database it created and removes its control-plane rows, and `/complete` answers this same refusal instead of a 500.

There is no self-service way yet to find your workspace without starting a signup: see `docs/followups.md` (an apex "email me my workspace address" form).

The welcome email carries the **Set your password** button (with the paste-this fallback and the expiry), the workspace URL and the email as plain panel values, and a sign-in link with the address prefilled — no password anywhere. Its preheader is fixed copy ("Your workspace is live."), never the link. With no tenant base URL configured, `/complete` refuses with `503` **before** provisioning and leaves the verification unconsumed — a passwordless admin with no link to set a password (forgot-password needs that URL too) would have no way in. A Redis failure while minting yields no link (the email and the verify page point at Forgot password) rather than failing a request whose tenant already exists. The response is sent `Cache-Control: no-store`, and the issuance is audited as `auth.password_reset.requested` with `via: signup`.

## Closing a workspace the signer-up no longer wants

The address used at signup becomes that workspace's one account, and one address
can belong to only one workspace (`docs/decisions.md` §291). A trial that is
abandoned therefore holds its admin's address: if that person is later added to
another workspace — their employer's, typically — the add is refused
`email_in_other_workspace`. The way out is self-service, not support: they sign
in to the abandoned workspace (**Forgot password** works if they never set one)
and close it from **Organization → Close workspace**. Closing deletes the
workspace and every account in it, through the operator's own deletion code, so
the address is free at once.

It is a narrow door on purpose: a workspace that holds anything that must be
kept — approved invoices, payments, other active users, a paid subscription and
the rest of the list in [`user-management.md`](user-management.md) § Freeing an
address held by an abandoned workspace — is refused with its reasons, and
support closes it instead. The closure needs the slug typed back and, where MFA
is on, a code or a passkey. What it leaves behind: a control-plane
`workspace_closures` row (ids, slug, proof, counts — no address), the
`organization.closing` audit event already shipped to the write-once archive
where audit shipping is on, and the per-tenant backups, which an operator
removes within the DPA's 60 days
([`founder-runbooks/workspace-closure.md`](founder-runbooks/workspace-closure.md)).
Until the operator has, the closed workspace's slug stays taken: `/signup`
answers it as unavailable, because a new workspace under it would share its
`feoh_<slug>` backup name.
Nothing deletes an abandoned trial on its own; why, and what would have to
change first: `docs/decisions.md` §294 and `docs/followups.md` § Surfaced by
self-service workspace closure.

## Backend components

| Layer | File |
|-------|------|
| Router | `app/api/signup.py` |
| Set-password redemption | `app/api/auth.py` → `check_reset_token()` + `reset_password()`; token store `app/services/password_reset.py`; link builder `app/utils/tenant_urls.py` → `password_reset_url()` / `sign_in_url()` |
| Provisioning service | `app/services/tenant_provisioning.py` |
| Email dispatcher | `app/services/email_adapters/` (`console`, `ses`) |
| Rate limiter (Redis) | `app/services/rate_limit.py` |
| Slug validator | `app/utils/slug.py` |
| Captcha verifier | `app/utils/hcaptcha.py` |
| Password helpers | `app/utils/passwords.py` |
| Models | `app/models/signup.py` (`EmailVerification`); the admin `User` starts with `hashed_password` NULL |
| Migration | `alembic/versions/0001_self_service_signup.py` |

## Frontend components

| Route | File |
|-------|------|
| Landing (no tenant) | `src/routes/+layout.svelte` (inline) |
| Signup form | `src/routes/signup/+page.svelte` |
| Verify handler | `src/routes/verify/+page.svelte` |
| Set password (the reset page in `mode=set`) | `src/routes/login/reset-password/+page.svelte` |
| Sign in (prefills `?email=`) | `src/routes/login/+page.svelte` + `src/lib/utils/prefillEmail.ts` |

## Abuse mitigations

- **Captcha**: hCaptcha on `POST /signup/start`. Skipped locally when `FEOH_HCAPTCHA_SECRET` is empty — but a **deployed** env (`FEOH_ENVIRONMENT` not in `development`/`test`/`ci`/…) with signup on **refuses to boot** with the secret empty, so the gate can't silently fail open in production. The empty-secret skip logs at WARNING. To close signup, don't starve the captcha — set `FEOH_SIGNUP_ENABLED=false` (above), which also lifts the secret requirement.
- **Rate limit (per IP)**: Redis sliding window keyed by IP+endpoint. `FEOH_SIGNUP_RATE_LIMIT_PER_HOUR` (default 5) caps `/signup/start` and `/signup/complete`; `/signup/slug-check` has its own higher cap (`FEOH_SLUG_CHECK_RATE_LIMIT_PER_HOUR`, default 120) so it can't be scripted for namespace enumeration / control-plane DB amplification.
- **Rate limit (per email)**: a second limiter keyed on the target address (`FEOH_SIGNUP_EMAIL_RATE_LIMIT_PER_HOUR`, default 3) caps verification-email volume to one victim — the per-IP limit alone can't stop an attacker rotating IPs to email-bomb an address. A "resend" also **replaces** any prior un-consumed verification for that email, so the table can't grow unbounded per address.
- **Email verification**: no resources are provisioned until the user proves inbox access by clicking the link. Stolen email addresses don't result in tenants.
- **Single-use, race-safe tokens**: `/signup/complete` selects the verification row `FOR UPDATE`, so two concurrent completes for one token can't both pass the consumed-check and double-provision; the loser waits for the winner's commit and gets the re-answer (above). Every token that cannot be acted on (missing, expired, or spent without having created a workspace for its own address) returns the **same** 410 + message, so the response can't be used to tell a token that never existed from one that did.
- **No account enumeration at `/start`**: an address that already has an account gets the same response and the same verification email as any other; only `/complete`, after the mailbox is proven, says so.
- **Slug squatting**: slugs are only locked when `/complete` runs. Abandoned `/start` submissions do not reserve the namespace.
- **Reserved subdomains**: `utils/slug.py` → `RESERVED_SLUGS` blocks names that would collide with marketing/infra subdomains (`www`, `api`, `admin`, etc.). The check is enforced inside `provision_tenant` too, before any database work, so the operator CLI (`scripts/create_tenant.py`, and `deploy/add-tenant.sh` behind it) cannot provision a reserved or malformed slug; the CLI exits 2 with the reason.
- **No orphan databases**: if provisioning fails after the tenant DB is created (control-plane insert or table creation errors), the DB this attempt created is dropped, so partial failures don't leak databases or squat the namespace. A failure the address or the slug explains (a race lost since the checks) is answered as the coded refusal or the slug `409`; only an unexplained one is a `500`, with the token left unconsumed so a transient fault can be retried.
- **Log hygiene**: the submitter's email (PII) and raw captcha-network exception text are kept out of logs.
- **Partial-failure visibility**: if the welcome email fails after provisioning, the verify page still carries the set-password link, and Forgot password works for the account. Logs warn loudly, naming the slug only.
- **No credential at rest in the inbox**: the welcome email holds a single-use link that dies on first use or after `FEOH_SIGNUP_SET_PASSWORD_TTL_HOURS`, where it used to hold a temporary password that stayed valid until someone signed in. Neither the token nor the link is written to an application log line or the preheader (the `console` adapter, dev-only, prints whole messages by design).

## Email

Pluggable via `FEOH_EMAIL_PROVIDER`:

- `console` (default for local dev) — logs the full message to stdout.
- `ses` — AWS SES via boto3. Requires the sending domain to be verified and the runtime IAM role to have `ses:SendEmail`.

Adding a provider: copy `app/services/email_adapters/console_adapter.py`, implement `send()` + `test_connection()`, decorate with `@register_email_adapter("<name>")`, and import it in `__init__.py`.

## Config (env vars)

| Var | Default | Notes |
|-----|---------|-------|
| `FEOH_EMAIL_PROVIDER` | `console` | `console`, `ses` or `smtp`. A deployed env refuses to boot on `console`, which logs each email's full text (sign-in codes and set-password links included) instead of sending it. |
| `FEOH_EMAIL_FROM` | `no-reply@localhost` | Verified SES sender in prod |
| `FEOH_AWS_SES_REGION` | `us-east-1` | Only used by the SES adapter |
| `FEOH_PUBLIC_URL` | `http://localhost:7777` | Frontend URL — used to build verification links |
| `FEOH_TENANT_URL_TEMPLATE` | `http://{slug}.localhost:7777` | `{slug}` is substituted in the welcome email and the set-password link; blank → `/signup/complete` answers 503 before provisioning |
| `FEOH_SIGNUP_SET_PASSWORD_TTL_HOURS` | `72` | Lifetime of the signup admin's single-use set-password link (minimum 1). Longer than the 30-minute forgot-password link because nobody is waiting at the keyboard for it — see `docs/decisions.md` §283 |
| `FEOH_ENVIRONMENT` | `development` | Deployment discriminator. Any value outside `development`/`dev`/`local`/`test`/`ci` is "deployed" and turns on the production safety guards (e.g. captcha must be configured). |
| `FEOH_SIGNUP_ENABLED` | `true` | Master switch for `/api/signup/*`. `false` → every signup route 404s, `/signup` says signup is closed, and the hCaptcha secret is no longer required. See § Turning signup off. |
| `FEOH_HCAPTCHA_SECRET` | *(empty)* | Empty skips verification — OK for local dev; a deployed env with signup on refuses to boot when empty. |
| `FEOH_HCAPTCHA_SITEKEY` | *(empty)* | Exposed to the frontend via `/api/public-config` (alongside `signup_enabled`) |
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
6. The verify page shows progress → success with a **Set your password** button. Click it — or take the same link from the welcome email in the logs (the line ending `/login/reset-password?token=…&mode=set`); it is one link, usable once.
7. Choose a password on `http://<slug>.localhost:7777/login/reset-password`, follow **Go to sign in** (your email is already filled in), and sign in.
