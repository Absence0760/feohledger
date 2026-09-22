# Environment Variables

## Toolchain versions

Not env vars, but the other half of "what environment does this run in" — and
the place to check before raising either.

| Tool | Pinned to | Where it is declared |
|------|-----------|----------------------|
| Node | **24** (Active LTS, EOL 2028-04-30) | `node-version:` on every `setup-node` step in `.github/workflows/` — `ci.yml` (×4), `sso-e2e`, `web-bundle-budget`, `compliance-drift`, `aws-deploy`, `audit` |
| pnpm | **10.12.4** | `packageManager` in the root **and** `frontend/package.json`; no workflow passes a `version:` input |
| Python | **3.12+** | `backend/pyproject.toml` |
| Dart | **^3.11.4** | `environment.sdk` in `mobile/pubspec.yaml` (the Flutter 3.41+ floor in the root `CLAUDE.md` is not declared in the pubspec) |

There is no `.nvmrc` and no `engines` block in either `package.json`, so the
workflow pins are the whole story for CI — **plus** `deploy/deploy.sh`, which
builds the production frontend inside a `NODE_IMAGE=node:24-alpine` container
(see [minimal-deployment.md](minimal-deployment.md)). Both move together: Node
20 reached end-of-life on 2026-04-30, and a deploy image behind the CI pin means
production builds on a runtime CI never tested.

The floor is not arbitrary: `jsdom` — vitest's test-environment peer — declares
`engines: ^22.22.2 || ^24.15.0 || >=26.0.0`. pnpm does not enforce
`engines` without `engine-strict`, so an under-floor runtime installs silently
rather than failing — which is why this is written down here instead of being
left to a tool to catch. Raise the Node pin in **every** site at once; a floor
raised in one place and not another is worse than not raising it. See
`frontend/CLAUDE.md` § The Node floor.

## Frontend (`frontend/.env.development`)

| Variable         | Default                 | Description                         |
|------------------|-------------------------|-------------------------------------|
| `PUBLIC_API_URL` | `http://localhost:8000` | Backend API URL (embedded at build time) |
| `PUBLIC_PLATFORM_DOMAINS` | `localhost` (dev); unset elsewhere | Comma-separated registrable domains the **platform** serves (e.g. `feohledger.com,localhost`). A host under one of these carries the tenant slug as its first label; a bare one is the marketing/signup host; **any other host is a tenant's white-label vanity domain**, where the SPA sends no `X-Tenant-Slug` and calls `/api` same-origin so the backend resolves the tenant from `Host`. Unset is legal and replays the pre-vanity-domain rule, so an existing build is unchanged — set it (and proxy `/api` on the vanity origin) to enable custom domains. See [white-label.md](white-label.md) § Custom domains. **Also a TEST-time variable**: `frontend/tests-e2e/playwright.config.ts` passes it to the dev server and CI's frontend `pnpm build` steps bake it into the preview bundle, both from `tests-e2e/fixtures/env.ts::PLATFORM_DOMAINS` — the two run modes disagreeing is what kept the vanity-host e2e half uncovered. |
| `PUBLIC_SITE_URL` | `http://localhost:7777` (dev); derived in every deploy | Origin the SPA is served from — scheme + host (+ port), **no trailing slash**. It prefixes the `og:image` / `twitter:image` URLs of the link-preview card in `frontend/src/app.html`, which must be absolute: unfurl crawlers (Slack, LinkedIn, X, iMessage) run no JavaScript, so the card is static template markup substituted at build time via SvelteKit's `%sveltekit.env.PUBLIC_SITE_URL%`, not a `<svelte:head>`. **Never set by hand in a deploy** — `aws-deploy.yml` derives it from the `APP_URL` environment variable (trailing slash stripped; an empty `APP_URL` fails the build step) and `deploy/deploy.sh` from `https://$APP_DOMAIN` (required by its preflight), so it cannot disagree with the site's real origin. **Unset is not a build error**: SvelteKit substitutes an empty string, so a bare `pnpm build` (CI's, say) succeeds and renders a root-relative `/og-image.png` — the right file, but not a URL a crawler is required to resolve. There is deliberately no hardcoded `https://feohledger.com` fallback: it would silently point a QA or self-hosted build's card at the platform's origin. The card is platform-branded on every tenant and vanity host, like the favicon. Pinned by `frontend/src/lib/socialPreviewCard.test.ts`. |
| `BASE_PATH`      | (empty)                 | URL prefix for serving the SPA under a sub-path. **Nothing sets it** — the S3 + CloudFront deploy serves from the bucket root. Kept as an escape hatch for a sub-path host; read only by `frontend/svelte.config.js`. |

`frontend/vite.config.ts` also proxies **`/api` on the same origin** to
`PUBLIC_API_URL` in both `vite dev` and `vite preview` (`changeOrigin: false`, so
the request's `Host` survives). A platform host never uses it — the SPA calls the
build-time API origin cross-origin — but it is the operator requirement a
white-label vanity domain carries, and without it a custom domain cannot be
exercised locally at all.

`frontend/.env.development` is **committed** with the safe local default above
and Vite loads it automatically in dev mode — no setup step. Because `tenant.ts`
imports `PUBLIC_API_URL` from `$env/static/public`, the var must exist for the
dev server to start, and the committed file guarantees it does. For a personal
override, create a gitignored `frontend/.env.local` (it wins in Vite's
precedence over `.env.development`).

Override at build time for different environments:

```bash
PUBLIC_API_URL=https://api-qa.feohledger.com PUBLIC_SITE_URL=https://qa.feohledger.com pnpm build    # QA
PUBLIC_API_URL=https://api.feohledger.com PUBLIC_SITE_URL=https://feohledger.com pnpm build        # Production
```

Note: The frontend also reads the tenant slug from the browser subdomain at runtime (not from env vars). See [multi-tenancy.md](multi-tenancy.md).

### Playwright e2e harness (`frontend/tests-e2e/`)

Test-run configuration only — nothing here reaches the app at build or deploy
time. Every value defaults to what the suite already used, so an existing run is
unchanged.

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_WEB_ORIGIN` | `http://localhost:7777` | Origin the SvelteKit app is served on, **without** a tenant subdomain. Everything else is derived from it — the per-tenant origins (`http://<slug>.localhost:7777`), the post-login landing pattern, and the `--port` the Playwright `webServer` block hands `vite dev` / `vite preview`. |
| `PUBLIC_API_URL` | `http://localhost:8000` | Backend origin. Same variable the app reads (above): one value configures the build under test and the specs that call the API directly. |
| `E2E_TENANT_OFFSET` | `0` | Fixed offset added to every worker's `e2e<N>` tenant index, so two independent runs don't collide on `e2e1`. |
| `E2E_TENANT_COUNT` / `FEOH_E2E_TENANT_COUNT` | `4` | How many `e2e<N>` tenants the seed provisioned; the worker→tenant map wraps modulo this. |
| `PLAYWRIGHT_WORKERS` | `4` (CI: `1`) | Worker count. |
| `PLAYWRIGHT_BASE_URL` | `http://acme.localhost:7777` | Fallback `baseURL` for specs that don't take the per-worker one. |
| `FEOH_E2E_USE_PREVIEW` | unset | `true` serves the built bundle (`vite preview`) instead of the dev server — what CI does after `pnpm build`. |

The first two exist for **git worktrees**: `playwright.config.ts` sets
`reuseExistingServer` locally, so a second session on the default port silently
tests the *primary* checkout's build instead of its own. Worktrees isolate files,
not ports. Resolved in `frontend/tests-e2e/fixtures/env.ts`; the full recipe
(including what stays shared — the databases and the opt-in Docker services) is
in `frontend/tests-e2e/README.md` § Running from a worktree.

## Backend (`backend/.env`)

| Variable              | Default                                                                  | Description                      |
|-----------------------|--------------------------------------------------------------------------|----------------------------------|
| `FEOH_DATABASE_URL`     | `postgresql+asyncpg://postgres:postgres@localhost:5432/feohledger` | Control-plane DB connection      |
| `FEOH_TENANT_DB_PREFIX` | `feoh_`                                                                    | Prefix for tenant database names |
| `FEOH_SECRET_KEY`       | `change-me-in-production`                                                | JWT signing key                  |
| `FEOH_S3_ENDPOINT_URL`  | `http://localhost:9000`                                                  | MinIO/S3 endpoint. Set **empty** in deployed envs → real AWS S3 |
| `FEOH_S3_ACCESS_KEY`    | `minioadmin`                                                             | MinIO/S3 access key. Set **empty** (with the secret key) → boto3 default credential chain (instance/task role) |
| `FEOH_S3_SECRET_KEY`    | `minioadmin`                                                             | MinIO/S3 secret key. Set **empty** with the access key |
| `FEOH_S3_BUCKET`        | `invoices`                                                               | S3 bucket for invoice files      |
| `FEOH_DEBUG`            | `false`                                                                  | Enable debug logging + SQLAlchemy echo. Default `false` so a forgotten deploy doesn't ship Python tracebacks to clients; `backend/.env.development` sets it to `true` for local dev. The boot guard also relaxes the FEOH_SECRET_KEY / FEOH_EMAIL_INTAKE_SIGNING_SECRET defaults when this is `true`. |
| `FEOH_SSO_REDIRECT_PATH` | `/login/sso-callback`                                                   | Path the IdP redirects back to after OIDC auth (per tenant subdomain) |
| `FEOH_SSO_STATE_TTL_SECONDS` | `600`                                                               | TTL on the OIDC state/nonce stored in Redis  |
| `FEOH_SCIM_URL_PATH`    | `/api/scim/v2`                                                           | Mount path for SCIM 2.0 endpoints |
| `FEOH_MFA_ENABLED`      | `false`                                                                  | Master MFA switch. `false` skips all MFA flows (recommended for local dev). Flip to `true` in deployed environments. |
| `FEOH_MFA_ISSUER`       | `FeohLedger`                                                       | Label shown in TOTP authenticator apps |
| `FEOH_MFA_EMAIL_OTP_TTL_SECONDS` | `360`                                                           | Lifetime of email-OTP backup codes |
| `FEOH_MFA_CHALLENGE_TTL_SECONDS` | `300`                                                           | Lifetime of the post-password "still need MFA" challenge token |
| `FEOH_MFA_ENROLL_PENDING_TTL_SECONDS` | `900`                                                      | Lifetime of a pending (started-but-unverified) TOTP enrollment secret. The candidate lives in Redis, never on the account row, so starting an enrollment can't disturb the factor already in force |
| `FEOH_WEBAUTHN_RP_ID`   | `localhost`                                                              | **Platform** passkey Relying Party ID — the registrable domain passkeys are bound to (bare host, no scheme/port). Must be a suffix of every login origin; set to the app apex (e.g. `app.example.com`) in deployed envs. This is now the **fallback**: a request arriving on one of the tenant's own registered `settings.brand.custom_domains` resolves that host as the RP ID instead. Any other host — unknown, forged, or another tenant's vanity domain — falls back here, so a client-supplied `Host` can never become the RP. See [authentication.md](authentication.md) § Passkeys on a custom domain and [decisions.md](decisions.md) §87 |
| `FEOH_WEBAUTHN_ORIGINS` | `http://localhost:7777`                                                  | Comma-separated origins passkey ceremonies are verified against. Exact match, plus wildcard-subdomain entries (`https://*.app.example.com`) that admit every tenant subdomain — same scheme only, never the bare base, ports stay exact-match. Deployed envs MUST set this or passkeys silently fail on every prod origin |
| `FEOH_TRUSTED_PROXY_CIDRS` | (empty)                                                               | CIDRs whose `X-Forwarded-For` is trusted for the real client IP (rate limits + login/signup/SSO audit rows). Empty = never trust XFF (local dev). Behind a proxy (Caddy on the single-VM stack, the ALB on ECS) set it to the proxy's network or every user collapses into one per-IP bucket |
| `FEOH_ACCESS_TOKEN_EXPIRE_MINUTES` | `30`                                                          | JWT lifetime in minutes |
| `FEOH_MAX_CONCURRENT_SESSIONS` | `5`                                                                | Concurrent sessions per user. When a user logs in with this many already active, the oldest JTI is evicted to the blocklist. `0` disables the cap. |
| `FEOH_REDIS_URL`        | `redis://localhost:6379`                                                 | Redis connection — required at runtime (auth blocklist + active-session tracking, MFA, SSO state, rate limit) |
| `FEOH_CORS_ORIGINS`     | `["http://localhost:7777","http://localhost:5173"]`                      | Comma-separated allowed origins (also matched by regex on subdomain) |
| `FEOH_EXTRACTION_MODE`  | `local`                                                                  | `local` (in-process thread) or `lambda` (dispatch to SQS) |
| `FEOH_EXTRACTION_TIMEOUT_SECONDS` | `600`                                                          | How long an invoice may sit in `pending` before the reaper transitions it to `failed`. |
| `FEOH_EXTRACTION_REAPER_INTERVAL_SECONDS` | `60`                                                   | How often the in-process reaper sweeps for stuck `pending` invoices. |
| `FEOH_EXTRACTION_REAPER_ENABLED` | `true`                                                          | Disable to skip the background reaper (useful for one-shot CLI runs / tests). |
| `FEOH_EXTRACTION_AUTO_ROTATE`   | `true`                                                          | Run Tesseract OSD on rendered PDF pages before sending to vision adapters, auto-rotating 90/180/270-off-upright scans. No-op when `pytesseract` / `tesseract` are not installed. |
| `FEOH_SWEEP_FAILURE_ALERT_STREAK` | `3`                                                           | Consecutive failed runs (a tick that raised, or one that completed reporting `failures > 0`) before a background sweep is called **degraded**: the `GET /api/health/sweeps` aggregate flips, and the loop emits the alertable PII-free `NOT MAKING PROGRESS` ERROR on each streak multiple. `0` disables the escalation; the per-tick failure log stays either way. See `backend/docs/background-sweeps.md`. |
| `FEOH_APPROVAL_ESCALATION_ENABLED` | `false`                                                      | Master switch for the approval-escalation sweeper. Disabled in local dev; flip on in deployed envs. |
| `FEOH_APPROVAL_ESCALATION_INTERVAL_SECONDS` | `600`                                                 | How often the escalation sweeper scans every tenant's active workflow instances. |
| `FEOH_APPROVAL_ESCALATION_BATCH_SIZE`       | `200`                                                 | Page size for that sweep's keyset pagination. A page, not a cap — it pages until each tenant is exhausted, locking one instance at a time so it never blocks the approval path. |
| `FEOH_DISCOUNT_OPTIMIZATION_BATCH_SIZE`     | `200`                                                 | Page size for the dynamic-discounting auto-capture sweep's keyset pagination over a tenant's `offered` offers. A page, not a cap — an offer skipped for a below-threshold ROI stays `offered`, so a `LIMIT` would re-serve the same lowest-id offers every tick and never reach the tail. See `backend/docs/background-sweeps.md` § Locking. |
| `FEOH_CONTRACT_RENEWAL_BATCH_SIZE`          | `200`                                                 | Page size for the contract-renewal sweep's keyset pagination, used by BOTH its passes (renewal alert, end-of-term expiry), each with its own cursor. A page, not a cap — a contract outside its lead window stays un-alerted and one not yet over term stays `active`, so neither leaves the candidate set. See `backend/docs/background-sweeps.md` § Locking. |
| `FEOH_PAYMENT_RECONCILE_ENABLED` | `false`                                                        | Master switch for the payment-status reconciler (backstop polling for processors whose webhooks went missing). Pair with Modern Treasury / Stripe Treasury in prod. |
| `FEOH_PAYMENT_RECONCILE_INTERVAL_SECONDS` | `300`                                                 | How often the reconciler sweeps for `submitted`/`processing` payments. |
| `FEOH_PAYMENT_RECONCILE_AFTER_MINUTES` | `10`                                                     | Minimum age before a payment is re-checked against the processor. |
| `FEOH_PAYMENT_RECONCILE_MAX_AGE_HOURS` | `72`                                                     | Maximum age the reconciler tracks; anything older is left to ops. |
| `FEOH_EMAIL_INTAKE_DOMAIN` | (empty)                                                              | Hostname for inbound intake addresses (`invoices+<token>@<domain>`). Empty disables email intake. |
| `FEOH_EMAIL_INTAKE_SIGNING_SECRET` | (empty)                                                      | HMAC-SHA256 signing secret for the email-intake webhook body. Boot refuses if `FEOH_EMAIL_INTAKE_DOMAIN` is set and this is empty (unless `FEOH_DEBUG=true`). |
| `FEOH_ERP_MODE`         | `local`                                                                  | `local` or `lambda` for ERP push dispatch |
| `FEOH_ERP_MERGE_API_BASE` | `https://api.merge.dev/api/accounting/v1`                              | Merge.dev API base. Operator-controlled (process-level, TRUSTED — bypasses the admin-config SSRF guard). `backend/.env.development` points it at the local fake-erp (`http://localhost:12112/merge/api/accounting/v1`). |
| `FEOH_ERP_NETSUITE_API_BASE` | (empty)                                                              | NetSuite SuiteTalk REST base override. Empty → the per-account URL derived from `account_id`; set → used verbatim (operator-trusted). Dev value: `http://localhost:12112/netsuite/services/rest/record/v1`. |
| `FEOH_ERP_D365_API_BASE` | (empty)                                                                  | Dynamics 365 BC OData base override. Empty → the admin-config `base_url` + SSRF guard; set → used verbatim (operator-trusted). Dev value: `http://localhost:12112/d365`. |
| `FEOH_ERP_D365_TOKEN_URL` | (empty)                                                                 | D365 OAuth token endpoint override. Empty → `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`. Dev value: `http://localhost:12112/d365/oauth2/token`. |
| `FEOH_AUDIT_MODE`       | `local`                                                                  | `local` or `lambda` for audit log writes |
| `FEOH_SQS_EXTRACTION_QUEUE_URL` | (empty)                                                          | Required when `FEOH_EXTRACTION_MODE=lambda` |
| `FEOH_SQS_ERP_QUEUE_URL` | (empty)                                                                 | Required when `FEOH_ERP_MODE=lambda` |
| `FEOH_SQS_AUDIT_QUEUE_URL` | (empty)                                                               | Required when `FEOH_AUDIT_MODE=lambda` |
| `FEOH_AUDIT_SHIPPING_ENABLED` | `false`                                                            | Master switch for the centralized audit-log shipper (SOC 2). Disabled in local dev; flip on in deployed envs. |
| `FEOH_AUDIT_SHIPPING_INTERVAL_SECONDS` | `60`                                                      | How often the shipper sweeps every tenant DB for unshipped `audit_log` rows. |
| `FEOH_AUDIT_SHIPPING_BATCH_SIZE` | `500`                                                          | Max rows shipped per tenant per sweep. |
| `FEOH_AUDIT_SHIPPING_PROVIDERS` | `mock`                                                          | Comma-separated adapter names — typical prod value `cloudwatch,s3_objectlock`. |
| `FEOH_AUDIT_SHIPPING_CLOUDWATCH_GROUP` | `/ap/audit`                                              | CloudWatch Logs group for the shipped events. |
| `FEOH_AUDIT_SHIPPING_S3_BUCKET` | (empty)                                                         | Object-Lock-enabled S3 bucket. Required when the `s3_objectlock` provider is enabled. |
| `FEOH_AUDIT_SHIPPING_S3_MIN_RETENTION_DAYS` | `2555`                                            | Floor (in days, ≈ 7 years) the bucket's default Object Lock retention must meet. Boot refuses a bucket under it, in GOVERNANCE mode, or with no default rule at all. |
| `FEOH_ANTHROPIC_API_KEY` | (empty)                                                                 | Platform Claude Vision key (used when org chooses "Platform" extraction) |
| `FEOH_EXTRACTION_MODEL` | `claude-sonnet-4-20250514`                                               | Default extraction model for the platform program |
| `FEOH_EXTRACTION_PROVIDER` | (empty) / `mock` in `.env.development`                                | Operator override for the adapter **platform**-mode extraction runs on. Empty = derive: a set `FEOH_ANTHROPIC_API_KEY` → `claude_vision`; keyless + non-deployed → the offline `mock` reader (so `pnpm dev` never calls out with an empty key); keyless + **deployed** → still `claude_vision`, which fails loudly, because `mock.extract` returns a fixture and fabricating invoice fields is worse than a provider error. A BYOK org's own `settings.extraction` is unaffected. An unregistered name is refused at boot. Precedence table + rationale: `backend/docs/ai-extraction.md` § Platform provider precedence. |
| `FEOH_LITHIC_API_KEY`   | (empty)                                                                  | Platform Lithic key for virtual cards |
| `FEOH_LITHIC_SANDBOX`   | `true`                                                                   | Use Lithic sandbox endpoints |
| `FEOH_NIUM_CLIENT_ID` / `_SECRET` / `_CUSTOMER_HASH_ID` / `_WALLET_HASH_ID` / `_SANDBOX` | (empty / `true`)                | Platform Nium config for virtual cards |
| `FEOH_EMAIL_PROVIDER`   | `console`                                                                | `console` (logs to stdout) or `ses` |
| `FEOH_EMAIL_FROM`       | `no-reply@localhost`                                                     | From-address on outbound transactional email |
| `FEOH_AWS_SES_REGION`   | `us-east-1`                                                              | AWS region when `FEOH_EMAIL_PROVIDER=ses` |
| `FEOH_PUBLIC_URL`       | `http://localhost:7777`                                                  | Where the frontend is served — used in outbound email links, and as the origin of the email header's brand mark (`/email-mark.png`), so a deployed env left on loopback shows recipients a broken image |
| `FEOH_TENANT_URL_TEMPLATE` | `http://{slug}.localhost:7777`                                        | Platform-wide tenant URL shape; `{slug}` is substituted. **Now a fallback** — a tenant that sets `settings.brand.tenant_url_template` (via `PUT /api/organization/branding`) overrides it for every outbound link: invites, password resets, portal and approval deep links. Also the source the **platform domain** is derived from, which `PUT /api/organization/branding/custom-domains` refuses vanity hosts under. Deliberately NOT read per-tenant by `services/sso.py` (the OIDC `redirect_uri` / SAML bridge URL are registered at the customer's IdP) or the public signup-config endpoint. See [../backend/docs/notifications.md](../backend/docs/notifications.md) § Where a tenant's links point and [decisions.md](decisions.md) §91 |
| `FEOH_SIGNUP_ENABLED` | `true`                                                               | Master switch for self-service tenant signup. **Production:** keep `true` (and set both hCaptcha keys) on a deployment that takes self-service customers; set `false` on one whose tenants are provisioned by an operator (`deploy/add-tenant.sh`). Off → every `/api/signup/*` route answers 404, `/api/public-config` reports `signup_enabled: false` so `/signup` renders "signup is closed", and the deployed-env hCaptcha boot check stops applying. Default on so local dev is unchanged. See [self-service-signup.md](self-service-signup.md) § Turning signup off |
| `FEOH_HCAPTCHA_SECRET` / `FEOH_HCAPTCHA_SITEKEY` | (empty)                                                   | hCaptcha keys for self-service signup. The secret is **required** in a deployed env while `FEOH_SIGNUP_ENABLED` is on (boot refuses otherwise), and not needed when it is off |
| `FEOH_SIGNUP_RATE_LIMIT_PER_HOUR` | `5`                                                            | Max `/signup/start` + `/complete` per IP per hour (Redis sliding window) |
| `FEOH_SIGNUP_EMAIL_RATE_LIMIT_PER_HOUR` | `3`                                                      | Max verification emails per target address per hour (anti email-bombing) |
| `FEOH_SLUG_CHECK_RATE_LIMIT_PER_HOUR` | `120`                                                      | Max `/signup/slug-check` per IP per hour (anti-enumeration) |
| `FEOH_ENVIRONMENT`      | `development`                                                            | Deployment discriminator; non-dev values turn on prod guards (captcha required to boot) |
| `FEOH_RAG_ENABLED`      | `true`                                                                   | RAG retrieval of similar invoices as few-shot examples |
| `FEOH_RAG_TOP_K`        | `3`                                                                      | Number of semantic neighbors retrieved per extraction |
| `FEOH_EMBEDDING_PROVIDER` | `mock`                                                                 | `mock` (dev) or `openai` (text-embedding-3-small) |
| `FEOH_EMBEDDING_API_KEY` | (empty)                                                                 | OpenAI API key when `FEOH_EMBEDDING_PROVIDER=openai` |
| `FEOH_EMBEDDING_MODEL`  | `text-embedding-3-small`                                                 | Embedding model name |
| `FEOH_EMBEDDING_DIMENSIONS` | `1536`                                                               | Embedding vector size — must match the column type in pgvector |
| `FEOH_DUPLICATE_SIMILARITY_THRESHOLD` | `0.95`                                                     | Cosine threshold for flagging near-duplicate invoices |
| `FEOH_HSTS_ENABLED`     | `false`                                                                  | Master switch for the `Strict-Transport-Security` response header. Keep `false` in local HTTP dev; set `true` in deployed environments behind HTTPS. |
| `FEOH_HSTS_MAX_AGE`     | `63072000`                                                               | HSTS `max-age` in seconds. Default is two years — the minimum for `hstspreload.org` submission. |
| `FEOH_HSTS_INCLUDE_SUBDOMAINS` | `true`                                                            | Emit the `includeSubDomains` directive (recommended; subdomains inherit the pin). |
| `FEOH_HSTS_PRELOAD`     | `true`                                                                   | Emit the `preload` directive. Only meaningful if you actually submit to the preload list. |
| `FEOH_PEPPOL_PROVIDER`  | `mock`                                                                   | PEPPOL Access Point adapter — `mock` (in-process, local-first default) or `as4_gateway` (real hosted AP). Per-org override via `Organization.settings.peppol.provider`. |
| `FEOH_PEPPOL_GATEWAY_URL` | (empty)                                                                | Base URL of the hosted Access Point. Required when `FEOH_PEPPOL_PROVIDER=as4_gateway`. |
| `FEOH_PEPPOL_GATEWAY_API_KEY` | (empty)                                                            | Gateway API key — no hardcoded fallback; empty disables the gateway (returns `peppol_not_configured`). Store via sops in deployed envs. |
| `FEOH_PEPPOL_INBOUND_ENABLED` | `false`                                                            | Master switch for the inbound PEPPOL AS4 receive webhook (`POST /api/peppol/inbound/{tenant_slug}`). When `false` the route is a silent no-op 204. `backend/.env.development` sets it `true` so the webhook is locally testable. |
| `FEOH_PEPPOL_INBOUND_SIGNING_SECRET` | (empty)                                                     | HMAC-SHA256 key the Access Point signs the inbound POST body with. Boot refuses if `FEOH_PEPPOL_INBOUND_ENABLED` is true and this is empty (unless `FEOH_DEBUG=true`). No hardcoded fallback; real secret via sops. `backend/.env.development` carries a NON-secret dev value (`dev-peppol-inbound-secret`). |
| `FEOH_PEPPOL_INBOUND_MAX_BYTES` | `4194304`                                                          | Hard cap (bytes) on the inbound PEPPOL webhook body — oversized POSTs are rejected with 204 before buffering/parsing (memory-exhaustion guard). PEPPOL UBL documents are tens of KB; the 4 MiB default leaves headroom. |
| `FEOH_DEPLOYED_REGION`  | (empty)                                                                  | Data residency (GDPR/CCPA): the region **this stack** declares it runs in — one of `us`/`eu`/`uk`/`ca`/`au`. Compared against each tenant's pinned `settings.residency.region` and reported as the advisory `alignment` block on `GET`/`PUT /api/organization/data-residency` (never blocks, never routes data). **Empty means "unknown / cannot attest"** — `status: "unknown"`, `aligned: null` — never a reassuring `true` nobody verified. Not validated at boot on purpose: the field is advisory, so an unrecognised value reports `unknown` with a reason rather than refusing to start. `backend/.env.development` sets `us` so the signal is exercisable locally. See `docs/data-residency.md`. |

`backend/.env.development` is **committed** with safe local defaults and is
loaded by `main.py` (the local-dev entrypoint) via `python-dotenv` — no setup
step. It's belt-and-suspenders anyway: the defaults in `app/config.py` already
work with Docker Compose even with no env file present. For personal overrides,
add a gitignored `backend/.env`; it wins over `.env.development`.

All backend variables are prefixed with `FEOH_` and loaded via `pydantic-settings` in `app/config.py`.

### Database URLs

The `FEOH_DATABASE_URL` points to the **control-plane database** (`feohledger`). Tenant database URLs are derived automatically by replacing the database name with `<FEOH_TENANT_DB_PREFIX><slug>` (e.g., `feoh_acme`).

### Alembic

Set `FEOH_MIGRATE_TENANT` to target a specific tenant database for migrations:

```bash
FEOH_MIGRATE_TENANT=feoh_acme alembic upgrade head
```

When unset, Alembic targets the control-plane database.

## Feature flags, adapters and background sweeps (annotated)

The annotated companion to the plain Backend table above — every `FEOH_` switch
that gates a feature, selects an adapter, or paces a background sweep, with the
reasoning for its default. Extracted from the root `CLAUDE.md` so that file stays
small enough to load into every conversation. A few rows (`FEOH_DATABASE_URL`,
`FEOH_SECRET_KEY`, `FEOH_S3_ENDPOINT_URL`, `FEOH_MFA_ENABLED`) also appear in the
plain table above; the fuller description is here. The authoritative list is
always `backend/app/config.py`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_DATABASE_URL` | `postgresql+asyncpg://...localhost:5432/feohledger` | Control plane DB |
| `FEOH_SECRET_KEY` | `change-me-in-production` | JWT signing (HS256) |
| `FEOH_S3_ENDPOINT_URL` | `http://localhost:9000` | MinIO/S3 |
| `FEOH_EXTRACTION_MODE` | `local` | `local` or `lambda` |
| `FEOH_ERP_MODE` | `local` | `local` or `lambda` |
| `FEOH_ERP_MERGE_API_BASE` | `https://api.merge.dev/api/accounting/v1` | Merge.dev API base — operator-controlled (bypasses the admin-config SSRF guard); `.env.development` points it at fake-erp (`http://localhost:12112/merge/api/accounting/v1`) |
| `FEOH_ERP_NETSUITE_API_BASE` | (empty) | NetSuite REST base override — empty derives the per-account URL from `account_id`; set → used verbatim (operator-trusted). Dev value targets fake-erp |
| `FEOH_ERP_D365_API_BASE` | (empty) | Dynamics 365 BC OData base override — empty → admin-config `base_url` + SSRF guard; set → used verbatim (operator-trusted). Dev value targets fake-erp |
| `FEOH_ERP_D365_TOKEN_URL` | (empty) | D365 OAuth token URL override — empty → `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`. Dev value targets fake-erp |
| `FEOH_ANTHROPIC_API_KEY` | (empty) | Claude Vision for platform extraction |
| `FEOH_EXTRACTION_MODEL` | `claude-sonnet-4-20250514` | AI model for extraction |
| `FEOH_ASSISTANT_PROVIDER` | `mock` (code) / `ollama` (`.env.development`) | Conversational assistant adapter — `mock` \| `claude` \| `ollama`. Committed dev default is `ollama` (local model); `claude`/`ollama` fail soft to `mock`. See `backend/docs/conversational-assistant.md`. |
| `FEOH_ASSISTANT_OLLAMA_MODEL` | `qwen2.5:7b` | Local **tool-capable** Ollama text model for the assistant (NOT the vision model used for extraction). Base URL reuses `FEOH_OLLAMA_BASE_URL`. |
| `FEOH_CASHFLOW_COPILOT_ENABLED` | `true` | Master switch for the AI Cash-Flow Copilot (Phases 1–2) — gates the five read-only/proposal cash-planning assistant tools + the `/api/cash-flow/copilot(+/stream)` façade routes. Reuses the assistant's provider/budget/key config — no new secret; local-first via mock/ollama. See `docs/cash-flow-copilot.md`. |
| `FEOH_CASHFLOW_COPILOT_DEFAULT_HORIZON_DAYS` | `90` | Default forecast horizon (days) the copilot uses when the user doesn't specify one. |
| `FEOH_CASHFLOW_SHORTFALL_ALERTS_ENABLED` | `false` | Master switch for the projected-cash-shortfall alert sweep — keep `false` in local dev (it emails finance leaders), flip on in deployed envs. The sweep only READS the cash forecast and notifies; it never creates a Payment/PaymentRun, accepts a discount, or touches an invoice. Per-org opt-in is the persisted `settings.cashflow.min_balance_threshold` — an org without one is skipped. See `docs/cash-flow-copilot.md` § Proactive projected-shortfall alerts. |
| `FEOH_CASHFLOW_SHORTFALL_ALERTS_INTERVAL_SECONDS` | `86400` | Shortfall-alert sweep tick interval (daily — a cash forecast doesn't move hour to hour). |
| `FEOH_CASHFLOW_SHORTFALL_ALERTS_HORIZON_DAYS` | `90` | How far ahead the alerting forecast looks. Independent of the copilot's interactive default so an operator can alert on a shorter, more actionable window. |
| `FEOH_EXTRACTION_AUTO_ROTATE` | `true` | Run Tesseract OSD on rendered PDF pages before sending to vision adapters. No-ops if `pytesseract` / `tesseract` missing. |
| `FEOH_AUDIT_SUMMARY_ENABLED` | `false` | Master switch for the invoice audit-log summary. Left off (the default), `GET /api/invoices/{id}/summary` returns the deterministic template summary with no LLM call. Reuses the extraction key/model — no new secret, which is why it must be opted into rather than defaulted on: with no credential of its own, a `true` default turned an extraction key into an undisclosed second Anthropic data flow. |
| `FEOH_AUDIT_SUMMARY_MODEL` | (empty) | Model for the audit summary; falls back to `FEOH_EXTRACTION_MODEL` when empty. |
| `FEOH_REDIS_URL` | `redis://localhost:6379` | Token blocklist |
| `FEOH_LITHIC_API_KEY` | (empty) | Lithic virtual cards |
| `FEOH_NIUM_CLIENT_*` | (empty) | Nium virtual cards |
| `FEOH_MFA_ENABLED` | `false` | Master MFA switch (gates TOTP, email-OTP, AND WebAuthn/passkeys) — keep `false` in local dev, flip on in deployed envs |
| `FEOH_MFA_ENROLL_PENDING_TTL_SECONDS` | `900` | Lifetime of a *pending* (started-but-unverified) TOTP enrollment secret. The candidate lives in Redis (`mfa:pending_enroll:` / `mfa:vendor_pending_enroll:`), never on the account row, so starting an enrollment can't disturb the second factor already in force; past the TTL the user restarts enrollment. No secret. See `docs/authentication.md` § Per-user enrollment. |
| `FEOH_WEBAUTHN_RP_ID` | `localhost` | WebAuthn/passkey Relying Party ID — the registrable domain a passkey is bound to. A bare host (no scheme/port). `localhost` works across every tenant subdomain in dev; set to your apex (e.g. `app.example.com`) in deployed envs. See `docs/authentication.md` § Passkeys. |
| `FEOH_WEBAUTHN_RP_NAME` | `FeohLedger` | Human-readable Relying Party name the authenticator UI shows. |
| `FEOH_WEBAUTHN_ORIGINS` | `http://localhost:7777` | Comma-separated allowed origins (scheme+host+port) the passkey register/authenticate ceremonies are verified against. Each tenant subdomain is its own origin in dev. Wildcard-subdomain entries (`https://*.app.example.com`) admit every tenant subdomain in deployed envs. No secret. |
| `FEOH_WEBAUTHN_CHALLENGE_TTL_SECONDS` | `300` | Lifetime of the server-minted WebAuthn ceremony challenge stashed in Redis (single-use). |
| `FEOH_API_PUBLIC_URL` | `http://localhost:8000` | Externally-reachable backend base URL. Builds the SAML SP entityId + ACS URL the IdP POSTs to (unlike OIDC's frontend redirect). Set to the real API host in deployed envs |
| `FEOH_SAML_ACS_PATH` | `/login/saml-callback` | Frontend SPA bridge route the SAML ACS 303-redirects to (with a one-time handoff code) |
| `FEOH_SAML_SP_PRIVATE_KEY` / `FEOH_SAML_SP_CERT` | (empty) | Optional SP signing keypair — only when an IdP requires SP-signed AuthnRequests. Real secret → sops; empty by default (local Keycloak runs with SP signing off) |
| `FEOH_HSTS_ENABLED` | `false` | Emit `Strict-Transport-Security` on every response — keep `false` in local HTTP dev, flip on in deployed envs |
| `FEOH_AUDIT_SHIPPING_ENABLED` | `false` | Master switch for the centralized audit-log shipper — keep `false` in local dev, flip on in deployed envs |
| `FEOH_AUDIT_SHIPPING_PROVIDERS` | `mock` | Comma-separated adapter names (e.g. `cloudwatch,s3_objectlock`). All must succeed before rows are marked shipped. |
| `FEOH_AUDIT_SHIPPING_S3_BUCKET` | (empty) | Object-Lock-enabled S3 bucket for the WORM copy; required when the `s3_objectlock` provider is enabled |
| `FEOH_AUDIT_SHIPPING_S3_MIN_RETENTION_DAYS` | `2555` | Minimum default Object Lock retention (days) the bucket must carry; the boot check also requires COMPLIANCE mode (`backend/docs/audit-log-shipping.md`) |
| `FEOH_AUDIT_SHIPPING_CLOUDWATCH_GROUP` | `/ap/audit` | CloudWatch Logs group for shipped audit events |
| `FEOH_AUDIT_MODE` | `local` | `local` or `lambda` — same shape as `FEOH_EXTRACTION_MODE` |
| `FEOH_EMAIL_INTAKE_DOMAIN` | (empty) | Hostname for inbound intake addresses (`invoices+<token>@<domain>`). Empty disables email intake. |
| `FEOH_EMAIL_INTAKE_SIGNING_SECRET` | (empty) | HMAC-SHA256 signing secret for the email-intake webhook body. Required whenever `FEOH_EMAIL_INTAKE_DOMAIN` is set — boot refuses otherwise. |
| `FEOH_MAX_CONCURRENT_SESSIONS` | `5` | Max concurrent sessions per user. Oldest JTI is evicted onto the blocklist when exceeded. `0` disables the cap. |
| `FEOH_NOTIFICATIONS_ENABLED` | `true` | Master switch for email + in-app notifications. When `false`, the `transition_invoice` / `assign_reviewer` hooks skip dispatch. Dispatch is always best-effort regardless (a failure never breaks a transition). See `backend/docs/notifications.md`. |
| `FEOH_CHAT_NOTIFICATION_PROVIDER` | `mock` | Platform-default outbound chat-notification adapter for approval events — `mock` (no network/credential — local-first default) \| `slack` \| `teams`. Per-org override + webhook URL + per-event toggles on `Organization.settings.chat_notifications`. Best-effort, PII-free, fails closed without a webhook URL. See `backend/docs/notifications.md` § Chat notifications. |
| `FEOH_REPORTING_CURRENCY_DEFAULT` | `USD` | Platform last-resort reporting (base) currency for multi-currency rollups when an org sets no `reporting_currency`. Per-org override on `Organization.settings.reporting_currency`. See `backend/docs/multi-currency.md`. |
| `FEOH_PEPPOL_INBOUND_ENABLED` | `false` | Master switch for the inbound PEPPOL AS4 receive webhook (`POST /api/peppol/inbound/{tenant_slug}`). When `false` the route is a silent no-op 204. See `backend/docs/peppol.md` § Inbound. |
| `FEOH_PEPPOL_INBOUND_SIGNING_SECRET` | (empty) | HMAC-SHA256 key the Access Point signs the inbound POST body with. Required when `FEOH_PEPPOL_INBOUND_ENABLED` is true — boot refuses otherwise. No hardcoded fallback; real secret via sops. A NON-secret dev value is committed in `backend/.env.development`. |
| `FEOH_PEPPOL_INBOUND_MAX_BYTES` | `4194304` | Hard cap (bytes) on the inbound PEPPOL webhook body — oversized POSTs are rejected with 204 before buffering/parsing (memory-exhaustion guard). |
| `FEOH_PEPPOL_PROVIDER` | `mock` | PEPPOL Access Point adapter — `mock` (in-process, no network — local-first default) \| `as4_gateway`. Per-org override on `Organization.settings.peppol.provider`. See `backend/docs/peppol.md`. |
| `FEOH_PEPPOL_GATEWAY_URL` | (empty) | Hosted Access Point base URL (deployed only). |
| `FEOH_PEPPOL_GATEWAY_API_KEY` | (empty) | PEPPOL gateway API key — **no hardcoded fallback**; sops in deployed. |
| `FEOH_CONTRACT_RENEWAL_ENABLED` | `false` | Master switch for the contract renewal-alert background sweep — keep `false` in local dev, flip on in deployed envs. See `backend/docs/contracts.md`. |
| `FEOH_CONTRACT_RENEWAL_INTERVAL_SECONDS` | `3600` | Renewal sweep interval. |
| `FEOH_CONTRACT_RENEWAL_DEFAULT_NOTICE_DAYS` | `30` | Platform default renewal lead window; per-contract `renewal_notice_days` overrides it. |
| `FEOH_CONTRACT_RENEWAL_BATCH_SIZE` | `200` | Page size for the contract-renewal sweep's keyset pagination, used by BOTH its passes (renewal alert, end-of-term expiry), each carrying its own cursor. A *page*, not a cap — a contract outside its own lead window stays un-alerted and one not yet over term stays `active`, so neither leaves the candidate set and a capped tick would starve the tail. See `backend/docs/background-sweeps.md` § Locking. |
| `FEOH_VENDOR_SCREENING_ENABLED` | `true` | Synchronous sanctions screening on vendor create/update (mock-safe local-first; best-effort, never blocks the write). See `backend/docs/vendor-risk-screening.md`. |
| `FEOH_VENDOR_RESCREEN_ENABLED` | `false` | Master switch for the periodic vendor re-screening sweep — keep `false` in local dev, flip on in deployed envs. |
| `FEOH_VENDOR_RESCREEN_INTERVAL_SECONDS` | `86400` | Re-screen sweep interval. |
| `FEOH_VENDOR_RESCREEN_AFTER_DAYS` | `7` | Re-screen active vendors whose last screen is older than this (or never screened). |
| `FEOH_DISCOUNT_OPTIMIZATION_ENABLED` | `false` | Master switch for the dynamic-discounting auto-capture background sweep — keep `false` in local dev, flip on in deployed envs. The sweep only flags high-ROI offers as accepted; it never moves money. See `backend/docs/dynamic-discounting.md`. |
| `FEOH_DISCOUNT_OPTIMIZATION_INTERVAL_SECONDS` | `3600` | Auto-capture sweep interval. |
| `FEOH_DISCOUNT_OPTIMIZATION_BATCH_SIZE` | `200` | Page size for the dynamic-discounting auto-capture sweep's keyset pagination over a tenant's `offered` offers. A *page*, not a cap — an offer skipped for a below-threshold ROI stays `offered`, so a `LIMIT` would re-serve the same lowest-id offers every tick and never reach the tail; the sweep pages (`WHERE id > :cursor ORDER BY id`) until each tenant is exhausted, locking one offer at a time. See `backend/docs/background-sweeps.md` § Locking. |
| `FEOH_DISCOUNT_AUTO_CAPTURE_ROI_THRESHOLD` | `12.0` | Annualized return (APR %) an early-pay offer must clear for the sweep to auto-accept it. |
| `FEOH_RECURRING_INVOICES_ENABLED` | `false` | Master switch for the recurring / subscription invoice generation sweep — keep `false` in local dev, flip on in deployed envs. The sweep only generates pre-coded invoices into the approval queue; it never moves money. See `backend/docs/recurring-invoices.md`. |
| `FEOH_RECURRING_INVOICES_INTERVAL_SECONDS` | `3600` | Recurring-invoice generation sweep interval. |
| `FEOH_RECURRING_INVOICES_MAX_PER_SWEEP` | `200` | Per-tick cap on invoices generated per tenant (backlog guard). |
| `FEOH_SCHEDULED_REPORTS_ENABLED` | `false` | Master switch for the scheduled-report runner — the background loop that sweeps every tenant, runs each due `ScheduledReport` (generate CSV → email recipients → bump `next_run_at`), and auto-disables a schedule after 5 consecutive failures. OFF by default so local dev / tests never email; flip on in deployed envs. Only sends reports — never moves money. See `backend/docs/analytics.md` § Scheduled report delivery. |
| `FEOH_SCHEDULED_REPORTS_TICK_SECONDS` | `3600` | Scheduled-report sweep tick interval. |
| `FEOH_STATEMENT_RECON_MATERIALITY_DEFAULT` | `1000.00` | Vendor statement reconciliation: platform-default materiality threshold (run currency, `Decimal`) above which a vendor's leftover unreconciled balance flags it not-close-ready on `GET /api/vendor-statements/close-readiness`. `?materiality=` overrides per call. No background sweep — reconciliation is user-triggered. See `backend/docs/vendor-statement-reconciliation.md`. |
| `FEOH_DISCOUNT_COST_OF_CAPITAL_PCT` | `8.0` | Platform-default annual cost of capital used by the ROI calculator; per-org override `Organization.settings.discounting.cost_of_capital_pct`. |
| `FEOH_QMS_SYNC_ENABLED` | `false` | Master switch for the QMS inspection-sync background sweep — keep `false` in local dev, flip on in deployed envs once a real QMS is configured per-org. Only upserts inspection rows; never moves money. See `backend/docs/po-matching.md` § QMS integration. |
| `FEOH_QMS_SYNC_INTERVAL_SECONDS` | `3600` | QMS sync sweep interval. |
| `FEOH_QMS_PROVIDER` | `mock` | Platform-default QMS adapter — `mock` (deterministic, no network/credential — local-first default) \| `generic` (httpx skeleton, fails closed without a key). Per-org override `Organization.settings.qms.provider`. |
| `FEOH_VENDOR_ENRICHMENT_PROVIDER` | `mock` | Platform-default external vendor-enrichment (firmographics) adapter — `mock` (deterministic synthetic data, no network/credential — local-first default) \| `dun_bradstreet` \| `clearbit` (httpx skeletons, **fail closed** without a per-org `api_key`; no hardcoded fallback). Per-org override `Organization.settings.enrichment.provider`. Powers `POST /api/enrichment/vendors/{id}/enrich` — advisory/suggestion-only, raw `tax_id` masked. See `backend/docs/data-enrichment.md` § External enrichment. |
| `FEOH_ACCESS_REVIEW_DORMANT_DAYS` | `90` | Dormancy window for the periodic SOX access review. A user holding an elevated role (`admin`/`ap_manager`/`cfo`) whose last *mutating* audit action is older than this — or who has never acted — is flagged DORMANT in `GET /api/access-reviews`. Compute-on-read (no column/migration). See `backend/docs/access-reviews.md`. |
| `FEOH_APPROVAL_SIGNING_KEY` | (empty) | HMAC-SHA256 key for digital signatures on invoice approvals (SOX non-repudiation). Signs the canonical approval payload (invoice id + exact amount + actor + decision + timestamp) onto each immutable `invoice.approved` audit row; re-verified at `GET /api/audit/invoice/{id}/verify-signatures`. Empty → signing skipped (no hardcoded fallback). NON-secret dev value committed in `.env.development`; real key via sops. See `backend/docs/approval-signatures.md`. |
| `FEOH_EMAIL_ACTION_SIGNING_KEY` | (empty) | HMAC-SHA256 key for the email-approval link token (approve/reject an assigned invoice from the notification email without logging in). Empty → feature OFF: no links added, every token rejected (fail-closed, no hardcoded fallback). NON-secret dev value committed in `.env.development`; real key via sops. The key's presence is the single on/off knob. See `backend/docs/email-approval.md`. |
| `FEOH_EMAIL_ACTION_TTL_HOURS` | `168` | Validity window (hours) of an email-approval link; past it the reviewer re-authenticates in the app. Also the TTL of the Slack approval-button action token (same primitive). |
| `FEOH_SLACK_SIGNING_SECRET` | (empty) | Slack app **signing secret** verifying the interactive-button POST to `/api/approvals/slack/interactivity` (HMAC over `v0:{X-Slack-Request-Timestamp}:{raw_body}`). Empty → Slack interactive approval is OFF: every inbound POST rejected (fail-closed, no hardcoded fallback). The button's signed action token reuses `FEOH_EMAIL_ACTION_SIGNING_KEY` (bound to a `slack` channel). NON-secret dev value committed in `.env.development`; real secret via sops. The key's presence is the single on/off knob. See `backend/docs/slack-approval.md`. |
| `FEOH_SLACK_REQUEST_MAX_AGE_SECONDS` | `300` | Reject a Slack interactivity POST whose `X-Slack-Request-Timestamp` is more than this far from now (replay-window guard). |
| `FEOH_TEAMS_SECURITY_TOKEN` | (empty) | Microsoft Teams **security token** (base64) — the HMAC key for **both** directions of the approval round-trip: the outbound card stamps each Approve/Reject `HttpPOST` action with the digest of the body that action posts back, and `/api/approvals/teams/interactivity` re-derives it (base64-decoded token as the key, over the raw body). Empty → Teams interactive approval is OFF in both directions: no card actions are rendered and every inbound POST is rejected (fail-closed, no hardcoded fallback). The card's signed action token reuses `FEOH_EMAIL_ACTION_SIGNING_KEY` (bound to a `teams` channel); the card's target is built from `FEOH_API_PUBLIC_URL`, so a loopback value in a deployed env means the buttons go nowhere. NON-secret base64 dev value committed in `.env.development`; real token via sops. See `backend/docs/teams-approval.md`. |
| `FEOH_TEAMS_REQUEST_MAX_AGE_SECONDS` | `300` | Reject a Teams interactivity POST whose `X-Teams-Request-Timestamp` is more than this far from now (replay-window guard). Teams does not always send the header; when absent the single-use action-token jti + the workflow state machine bound replay. |
| `FEOH_PARTNER_LINK_SIGNING_KEY` | (empty) | HMAC-SHA256 key for partner / reseller **link codes** (white-label two-sided-consent attach). A prospective CHILD tenant's admin mints a short-lived signed code (`POST /api/partner/link-code`) — proof of consent — and the PARTNER's admin redeems it to attach the child (`POST /api/partner/children`). The signature is what makes attach safe: a partner can't forge a code or adopt an org that didn't consent. Empty → feature OFF: no code can be minted, every redeem rejected (fail-closed, no hardcoded fallback). The key's presence is the single on/off knob. NON-secret dev value committed in `.env.development`; real key via sops. `partner_link_token.py` is the pure primitive. See `docs/white-label.md` § Partner / reseller admin. |
| `FEOH_PARTNER_LINK_TTL_MINUTES` | `30` | Validity window (minutes) of a partner link code. Short by design — a one-shot handshake between the child's admin and the partner's admin, not a long-lived invite. |
| `FEOH_RETENTION_ENABLED` | `false` | Master switch for the retention-policy enforcement sweep (SOX records management) — keep `false` in local dev, flip on in deployed envs. The sweep soft-archives overdue terminal invoices and verifies audit-log WORM shipment via a privileged, audited path; it NEVER deletes audit rows (composes with the immutability trigger). See `backend/docs/retention.md`. |
| `FEOH_RETENTION_INTERVAL_SECONDS` | `86400` | Retention sweep interval. |
| `FEOH_RETENTION_DEFAULT_MONTHS` | `84` | Platform-default retention window (months) when an org sets no per-class override on `Organization.settings.retention`. |
| `FEOH_RETENTION_BATCH_SIZE` | `500` | Max invoices the retention sweep soft-archives per tenant per tick, oldest first. A *page*, not a total-work cap: already-archived rows are now excluded in SQL, so a capped tick makes strict forward progress and the next tick resumes. Bounds a query that used to load every terminal invoice past the window on every tick, forever. See `backend/docs/retention.md`. |
| `FEOH_APPROVAL_ESCALATION_BATCH_SIZE` | `200` | Page size for the approval-escalation sweep's keyset pagination over candidate workflow instances. A *page*, not a cap — escalation doesn't change `state`, so a capped sweep would re-serve the same lowest-id rows forever and starve the tail; the sweep pages until each tenant is exhausted, locking one row at a time in id order so it never blocks `review.approve_invoice`. See `backend/docs/background-sweeps.md` § Locking. |
| `FEOH_DEPLOYED_REGION` | (empty) | Data residency: the region **this stack** declares it runs in (`us`/`eu`/`uk`/`ca`/`au`). Compared against each tenant's `settings.residency.region` and reported as the advisory `alignment` block on `GET`/`PUT /api/organization/data-residency` — never blocks, never routes data. **Empty = unknown / cannot attest** (`aligned: null`), never a reassuring `true` nobody verified. Deliberately NOT boot-validated: an unrecognised value reports `unknown` with a reason rather than refusing to start, because refusing over an advisory field trades a wrong answer for an outage. `.env.development` sets `us`. See `docs/data-residency.md`. |
| `FEOH_PUBLIC_API_ENABLED` | `true` | Platform kill switch for the public Developer API (`/api/v1`, `X-API-Key` auth). The surface is auth-gated regardless; when `false` every key fails closed with the opaque 401. No secret — API keys are minted per-org and stored hashed. See `backend/docs/public-api.md`. |
| `FEOH_PUBLIC_API_RATE_LIMIT_PER_MINUTE` | `120` | Per-API-key request cap on the `/api/v1` surface (60-second window), enforced by the Redis sliding-window limiter (`services/rate_limit.py`) keyed on `api_key_id` — per-key, not per-IP or per-org. A key over its cap gets a 429 + `Retry-After`. Checked AFTER the key authenticates inside `get_api_key_principal` (a bad key still gets the opaque 401, never a 429 — no enumeration). Fails open on a Redis outage; composes with the global `FEOH_RATE_LIMIT_ENABLED` master switch. No secret. See `backend/docs/public-api.md` § Per-key rate limiting. |
| `FEOH_WEBHOOKS_ENABLED` | `false` | Master switch for **outbound** Developer-API webhooks — gates BOTH the event emit (`services/webhooks/dispatch.emit_event` → silent no-op when off, no outbound HTTP) and the background retry/delivery sweep. OFF in local dev so a fresh clone never makes outbound calls; flip on in deployed envs. No secret — each subscription's HMAC signing secret is generated at create time and stored on the `webhook_subscriptions` row (a symmetric verification key). See `backend/docs/public-api.md` § Outbound webhooks. |
| `FEOH_WEBHOOKS_DELIVERY_INTERVAL_SECONDS` | `60` | Outbound-webhook retry/delivery sweep tick interval. |
| `FEOH_WEBHOOKS_ALLOW_PRIVATE_TARGETS` | `false` | SSRF-guard escape hatch for outbound-webhook target URLs. The SAFE default (`false`) rejects any target whose host resolves to a loopback / RFC1918 private / link-local (incl. the 169.254.169.254 metadata endpoint) / CGNAT / unique-local / unspecified / multicast / reserved address — at subscription create/update AND re-checked immediately before every dispatch (DNS-rebinding TOCTOU). `true` skips only the address checks (scheme/host shape still enforced) so local-first dev can point a webhook at `127.0.0.1`; the committed `backend/.env.development` sets it. Never enable in a deployed env. See `backend/docs/public-api.md` § Outbound webhooks → Target-URL SSRF guard. |
| `FEOH_BILLING_PROVIDER` | `mock` | Platform billing adapter — `mock` (in-process, deterministic, no network/credential — local-first default) \| `stripe_billing` (live create/get-subscription + report-usage over the Stripe REST API, fails closed without a key). Per-org override `Organization.settings.billing.provider`. See `backend/docs/billing.md`. |
| `FEOH_BILLING_STRIPE_API_KEY` | (empty) | Live Stripe Billing key — **no hardcoded fallback**; sops in deployed. The `stripe_billing` adapter fails closed without it. |
| `FEOH_BILLING_STRIPE_WEBHOOK_SECRET` | (empty) | HMAC secret for Stripe billing webhook signature verification — no fallback; sops in deployed. |
| `FEOH_BILLING_STRIPE_WEBHOOK_MAX_AGE_SECONDS` | `300` | Replay window on the `Stripe-Signature` `t=` timestamp — Stripe's verification procedure is digest **and** timestamp-tolerance, and only the digest half was enforced, so a captured correctly-signed event verified forever. Rejects too-old and too-far-future timestamps alike; the same ±5-min window `/approvals/slack` and `/approvals/teams` use. Complements the `event_id` dedupe (which only covers a redelivery of the SAME event inside its 72h TTL) rather than duplicating it. `<= 0` disables the age check, for an operator replaying an archived event. See `backend/docs/billing.md` § Inbound webhook route. |
| `FEOH_BILLING_STRIPE_API_BASE` | `https://api.stripe.com` | Stripe REST API base URL — overridable so a sandbox / test can point the `stripe_billing` adapter elsewhere; still fails closed without an API key. |
| `FEOH_BILLING_WEBHOOK_ENABLED` | `false` | Master switch for the inbound billing webhook route (`POST /api/billing/webhook/{provider}` — public-by-design, HMAC-gated, deduped by `event_id`, drives the `Subscription` lifecycle transition, 204-silent on rejection). OFF in local dev; flip on in deployed envs. Boot refuses to start if this is `true` while `FEOH_BILLING_PROVIDER` is `mock` **or names no registered adapter** — the mock's `parse_webhook` performs no signature verification, and an unregistered name (e.g. `stripe` instead of `stripe_billing`) silently falls back to it, so the public route would accept unauthenticated subscription events. See `backend/docs/billing.md` § Inbound webhook route. |
| `FEOH_BILLING_DUNNING_ENABLED` | `false` | Master switch for the dunning / past-due automation sweep — cancels subscriptions overdue past the grace window (NEVER moves money). OFF by default; flip on in deployed envs. |
| `FEOH_BILLING_DUNNING_INTERVAL_SECONDS` | `3600` | Dunning sweep tick interval. |
| `FEOH_BILLING_DUNNING_GRACE_DAYS` | `14` | Grace window (days from `current_period_end`) a subscription may sit `past_due` before the dunning sweep cancels it. |
