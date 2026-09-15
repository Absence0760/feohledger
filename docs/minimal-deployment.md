# Minimal-cost deployment

How to get the whole app on the public internet for **~$20/month** (or ~$7/month
off-AWS), without building any of the reference AWS architecture in
[`production-deployment.md`](production-deployment.md). That doc stays the
scale-up target; this one is the pilot / first-customers footprint.

**Total: one small VM running Docker Compose + Caddy, real S3 for files,
everything else in-process.**

```
        *.feohledger.com  ──────────► one VM (EC2 t4g.small)
                                 ├── Caddy         — TLS, static frontend, /api reverse-proxy
                                 ├── FastAPI       — backend container (uvicorn)
                                 ├── Postgres 16   — pgvector/pgvector:pg16 (control + tenant DBs)
                                 └── Redis 7       — token blocklist, rate limits, MFA state
                                        │
                                     AWS S3 — invoice files, backups (no MinIO in prod)
```

## Why this works without the big build-out

The app was designed local-first, and that carries straight into a cheap deploy:

- `FEOH_EXTRACTION_MODE` / `FEOH_ERP_MODE` / `FEOH_AUDIT_MODE` default to `local` —
  in-process worker threads, **no SQS, no Lambda**.
- Every provider integration defaults to its `mock` adapter; real providers
  (Claude Vision, a payment rail, Lithic) are per-org config flips later, not
  infrastructure.
- All background sweeps are asyncio tasks inside the API process, each behind
  an `FEOH_*_ENABLED` flag.
- The frontend is a static SPA; the only build-time input is `PUBLIC_API_URL`.
- Tenant routing is subdomain → `X-Tenant-Slug`; CORS for that is already
  solved by `FEOH_CORS_PRODUCTION_DOMAIN` (wildcard subdomain regex).

## What this deliberately does NOT give you

Single AZ, single instance, no autoscaling, no managed database, manual
deploys, restore-from-backup as the failover story. Acceptable for a pilot;
see [Upgrade triggers](#upgrade-triggers) for when each piece graduates.

## Cost breakdown

| Item | Monthly (us-east-1, on-demand) |
|---|---|
| EC2 `t4g.small` (2 vCPU ARM, 2 GB) | ~$12.30 |
| EBS 30 GB gp3 | ~$2.40 |
| Public IPv4 address | ~$3.65 |
| Route 53 hosted zone | $0.50 |
| KMS keys ×2 (sops + the `infra/` app key for SSE-KMS) | $2.00 |
| S3 (files + backups, pilot volume) + SES | ~$1 |
| **Total** | **~$22** |

The domain is extra: `feohledger.com` is registered through Route 53 at
$16/year (`docs/decisions.md` §167).

**Cheaper still:** a Hetzner CAX11 (2 vCPU ARM, 4 GB, ~€3.79) replaces the
EC2+EBS+IPv4 rows → **~$7/month total** (keep KMS + Route 53 + S3 on AWS).
Trade-off: no instance profile, so the box needs a scoped IAM access key for
S3/KMS instead of role-based credentials, and it sits outside the AWS-org
guardrails. The AWS path is recommended because the estate tooling (org
sub-account, sops KMS key, OIDC role) already automates it.

If 2 GB gets tight (OCR/extraction spikes), `t4g.medium` (4 GB) is ~$24.50 —
resize is a stop → change-type → start. Add 2 GB of swap either way.

## Key decisions

1. **Real S3 instead of MinIO in prod.** The `infra/` Terraform module already
   defines the invoice-files and audit-logs buckets (versioning, Object Lock,
   SSE-KMS) plus the backups bucket (lifecycle-expired, no lock — see
   § Backups). Set `FEOH_S3_BUCKET`, omit `FEOH_S3_ENDPOINT_URL`, and drop the
   MinIO container — less RAM, real durability, pennies at pilot volume.
2. **Caddy on the VM serves the frontend.** GitHub Pages can't serve wildcard
   tenant subdomains and CloudFront+ACM is more moving parts. Caddy serves the
   static `frontend/build`, reverse-proxies `api.feohledger.com` to the backend, and
   auto-provisions TLS. Start with an **explicit hostname list** (one line per
   tenant subdomain — plain HTTP-01, no DNS plugin, no extra IAM); move to a
   wildcard cert via DNS-01 + the Route 53 plugin only when tenant churn makes
   the list annoying.
3. **Everything stays in `local` dispatch mode.** The Lambda/SQS split exists
   for burst isolation, which a pilot doesn't have.
4. **Secrets follow the estate pattern.** This repo is public — `*.sops` files
   go in the private `Absence0760/infra-secrets` repo (per-project subdir +
   per-project KMS key), never committed here. The EC2 instance profile gets
   KMS access to both the sops key and the app key, plus scoped S3 access
   (§ 1 below), so no static AWS keys live on the box;
   `deploy/deploy.sh` decrypts the VM's copy (`deploy/.env.sops`) host-side to
   the gitignored `deploy/.env` on every deploy — the compose file reads it
   via `env_file` + interpolation. The contract is `deploy/env.example`.
5. **Manual deploys.** SSH in: `git pull`, rebuild, migrate, restart (script
   below). `aws-deploy.yml` stays disarmed (`AWS_DEPLOY_ENABLED` unset) until
   the ECS build-out exists.

## Step-by-step

### 0. Account, DNS, secrets substrate

- Create/choose the AWS account (estate: `new-project-account.sh <slug>` gives
  the sub-account, tfstate bucket, sops KMS key, and a delegated
  `<slug>.jaredhoward.com` zone; set `create_subdomain = false` and buy an apex
  instead if this is customer-facing).
- Bootstrap the project's subdir in the private `infra-secrets` repo
  (`bin/sops-init.sh --project <slug> --region <r>` there — see
  `~/github/project-mgmt/docs/secrets-management.md`). This repo has no sops
  config of its own (`docs/decisions.md` §165).
- `terraform apply` the existing `infra/` module for the S3 buckets + app KMS
  key.

### 1. VM

- EC2 `t4g.small`, Amazon Linux 2023 arm64, 30 GB gp3, security group: 80/443
  from anywhere (TCP, plus UDP 443 — Caddy serves HTTP/3; without the UDP
  rule browsers silently fall back to HTTP/2), 22 from your IP (or SSM
  Session Manager and no 22 at all).
- Instance profile — the box holds no static AWS keys, so this role is every
  AWS permission it has:
  - `kms:Decrypt` on the **sops** key (`deploy.sh` decrypts `.env.sops` with it).
  - `kms:GenerateDataKey` + `kms:Decrypt` on the **app** key — the
    `app_kms_key_arn` output of `infra/`. The invoice-files, audit-logs and
    backups buckets default to SSE-KMS under that key, and S3 checks the
    *caller's* access to it on every encrypted write and read: without these
    two, every invoice upload and every nightly `backup.sh` run is refused even
    with the S3 actions below granted. The key policy delegates to IAM
    (`infra/kms.tf`), so the role policy is all it takes.
  - `s3:GetObject/PutObject/DeleteObject/AbortMultipartUpload/ListBucket` on the
    invoice-files, audit-logs, and backups buckets (Delete because replacing or
    removing a stored document deletes its object — under Object Lock that
    writes a delete marker and the locked version survives; Abort because
    `backup.sh` streams multipart — a failed upload must be abortable, and the
    lifecycle reaper handles stragglers).
  - `s3:GetBucketObjectLockConfiguration` on the audit-logs bucket, once S3
    audit shipping is turned on: its adapter reads the lock at boot and refuses
    to start without it.
  - `ses:SendEmail` if using SES; ideally `ec2:ModifyInstanceMetadataOptions`
    so bootstrap can fix the IMDSv2 hop limit itself (containers can't reach
    instance-profile credentials through Docker's NAT at the default limit
    of 1).
- Run **`deploy/bootstrap-vm.sh`** — one idempotent script: docker + compose
  plugin + sops + cronie (AL2023 ships **no cron daemon** — without it the
  backup cron is a file nothing reads) + AWS CLI, automatic security updates
  (dnf-automatic, security-only; docker/containerd excluded so a package
  update never bounces the stack at a random hour — update those around a
  deploy window), 2 GB swap, the nightly backup cron, and the IMDS hop-limit
  fix. Node/pnpm are *not* needed on the VM — the frontend builds inside a
  `node:24` container.
- DNS: three records → the instance IP: `feohledger.com`, `api.feohledger.com`, and a
  **wildcard `*.feohledger.com`** so tenant onboarding never touches DNS again.
  (A DNS wildcard needs no wildcard *certificate* — Caddy still issues
  ordinary per-host HTTP-01 certs.)

### 2. Production compose stack (`deploy/compose.prod.yml` — built)

Four services (see [`deploy/README.md`](../deploy/README.md) for operations):

- `postgres` — `pgvector/pgvector:pg16`, volume-backed, **no host port**
  (compose-network only); password from the sops env.
- `redis` — `redis:7-alpine` with `--appendonly yes`, no host port.
- `api` — built from `backend/Dockerfile` (works on arm64; the lock resolves
  universally — if an arm64 wheel gap ever bites, fall back to an x86
  `t3a.small`, ~$14). Runs the image CMD, `uvicorn app.main:app` (the
  production entrypoint — not `main.py`). `FEOH_DATABASE_URL` / `FEOH_REDIS_URL`
  are derived in the compose file from `POSTGRES_PASSWORD`, so the DB
  password lives in exactly one sops entry. The compose network is pinned
  (`172.28.0.0/16`) and `FEOH_TRUSTED_PROXY_CIDRS` defaults to it, so per-IP
  rate limits and the login/signup audit rows key on the real client from
  Caddy's `X-Forwarded-For` — untrusted, every user would collapse into the
  proxy's container IP and the signup/login caps would throttle everyone
  collectively.
- `caddy` — ports 80/443, mounts the built `frontend/build` as the site root
  plus `deploy/Caddyfile` (domains via env) and the per-VM, gitignored
  `deploy/tenants.caddy` host list (one block per tenant subdomain —
  per-host HTTP-01 certs, no DNS plugin; maintained by `add-tenant.sh`, not
  by hand):
  - `feohledger.com` + each tenant host → SPA (`try_files {path} /index.html`)
  - `api.feohledger.com` → `reverse_proxy api:8000`

The frontend is built by the deploy script with
`PUBLIC_API_URL=https://<API_DOMAIN>` baked in.

### 3. Backend env (the sops-managed env — contract: `deploy/env.example`)

Beyond the committed defaults, the deployed env sets at minimum:

| Var | Value |
|---|---|
| `FEOH_ENVIRONMENT` | `production` (arms hCaptcha enforcement on signup) |
| `FEOH_SECRET_KEY` | `openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` (compose derives `FEOH_DATABASE_URL` / `FEOH_REDIS_URL` from it — don't set those) |
| `FEOH_S3_BUCKET` | invoice-files bucket; set `FEOH_S3_ENDPOINT_URL` / `FEOH_S3_ACCESS_KEY` / `FEOH_S3_SECRET_KEY` **empty** → real S3 via the instance-profile credential chain |
| `FEOH_MFA_ENABLED` / `FEOH_HSTS_ENABLED` | `true` / `true` |
| `FEOH_WEBAUTHN_RP_ID` / `FEOH_WEBAUTHN_ORIGINS` | `feohledger.com` / `https://feohledger.com,https://*.feohledger.com` — with MFA on, the localhost dev defaults reject every prod origin and passkeys silently fail; the wildcard entry covers each tenant subdomain |
| `FEOH_PUBLIC_URL` / `FEOH_API_PUBLIC_URL` | `https://feohledger.com` / `https://api.feohledger.com` |
| `FEOH_TENANT_URL_TEMPLATE` | `https://{slug}.feohledger.com` |
| `FEOH_CORS_PRODUCTION_DOMAIN` | `feohledger.com` |
| `FEOH_DEPLOYED_REGION` | the region this VM runs in (`us`/`eu`/`uk`/`ca`/`au`) — advisory only, but empty makes every tenant's data-residency `alignment` report `unknown` / `aligned: null` ("cannot attest") |
| `FEOH_EMAIL_PROVIDER` / `FEOH_EMAIL_FROM` | `ses` / verified sender |
| `FEOH_APPROVAL_SIGNING_KEY` + the other HMAC signing keys | real values (each key's presence is its feature's on-switch; leave unset = feature off) |

Everything else keeps its safe default: mock adapters, `local` modes, sweeps
off. Flip individual `FEOH_*_ENABLED` sweeps on once there's a reason
(`FEOH_PAYMENT_RECONCILE_ENABLED` and `FEOH_AUDIT_SHIPPING_ENABLED` are the two
worth enabling first when real payments/compliance start).

SES note: a fresh SES account is sandboxed (verified recipients only). Either
request production access, or skip self-service signup at first and provision
tenants by CLI (`python scripts/create_tenant.py …`), leaving email on
`console` until SES clears.

### 4. First boot + deploys (`deploy/deploy.sh` — built)

Copy the sops env onto the VM as `deploy/.env.sops`, then run
`deploy/deploy.sh`: it preflights its own prerequisites and the required env
keys (clear errors before any work happens), pulls main, decrypts secrets,
builds the frontend in a `node:24` container (`PUBLIC_API_URL` baked from
`API_DOMAIN`; pnpm store cached in a volume) and the backend image, runs
`alembic upgrade head && python scripts/migrate_all_tenants.py` **before**
the new API serves traffic (same ordering contract as the future ECS
pipeline), then rolls the containers with `up -d --wait` — the deploy fails
loudly if the API healthcheck never passes, and a failed build or migration
leaves the previous containers serving. Flags: `--no-pull`, `--backend-only`,
`--frontend-only`.

Tenants are one command each: `deploy/add-tenant.sh <slug> --name "Company"
--admin-email admin@company.com` provisions the tenant (the same
`provision_tenant` path self-service signup uses), appends the Caddy host
block, and reloads — no DNS step thanks to the wildcard record. Do **not**
run `scripts/seed.py` (demo data) in prod.

### 5. Backups (`deploy/backup.sh` — built; this is the whole DR story)

- Nightly cron (installed by `bootstrap-vm.sh` as `/etc/cron.d/feoh-backup`):
  dumps role globals + per-DB
  `pg_dump -Fc` of `feohledger` and every `feoh_*` tenant DB, streamed
  straight to the backups bucket (nothing persists on disk). The bucket is
  provisioned by the `infra/` module (`backups_bucket_name`) with the cost
  guards baked in: 90-day expiry (`backup_retention_days`), noncurrent-version
  cleanup, and incomplete-multipart reaping — no manual lifecycle rule to
  remember. Set `BACKUP_S3_BUCKET` from the `backups_bucket` output; the
  instance profile already has the access.
- Optional heartbeat: set `BACKUP_PING_URL` (healthchecks.io-style) in the
  sops env and `backup.sh` pings it after every successful run — silence
  means backups stopped, noticed before a restore needs them.
- Weekly EBS snapshot (Data Lifecycle Manager, free to configure) as the
  coarse fallback.
- Restore is scripted: `deploy/restore.sh <YYYY-MM-DD> [--force] [db …]` —
  globals first, then each DB via `pg_restore --create`, streamed straight
  from S3; existing DBs are skipped unless `--force`. **Test a restore once**
  before calling this done: scratch stack, `restore.sh <yesterday>`, log in.
- RPO ≈ 24h, RTO ≈ hours (new VM + restore). If a customer needs better, that
  is the RDS trigger below.

## What's left out — and how to add it later

Every omission has a deliberate seam, so graduating one piece never means
rebuilding the stack:

| Left out | Trigger | How to add it |
|---|---|---|
| Managed Postgres (RDS) | Uptime SLA / RPO < 24h asks | Create RDS PG16 (pgvector supported), restore the latest `backup.sh` dumps, set `FEOH_DATABASE_URL` in the sops env — the compose default is an **override seam**, no compose edit — redeploy, then `docker compose stop postgres`. ~$15–30/mo. |
| Managed Redis (ElastiCache) | Same HA push | Same seam: set `FEOH_REDIS_URL` in the sops env, redeploy. Redis holds only ephemeral state (blocklist / MFA / rate limits) — no data migration. |
| SQS + Lambda async workers | Extraction/OCR saturates the VM | Already implemented and bundled in the same image (`awslambdaric`). Provision queues + functions (production-deployment.md § Lambda workers), flip `FEOH_EXTRACTION_MODE=lambda` + `FEOH_SQS_*_QUEUE_URL` in the sops env, redeploy. Same pattern for the ERP and audit modes. |
| CloudFront + S3 frontend | Global latency / offloading the VM | The build artifact is identical. Arm the committed `aws-deploy.yml` pipeline (its § Arming checklist), then drop the SPA hosts from Caddy. |
| Wildcard TLS certificate | Tenant count makes per-host certs noisy (Let's Encrypt ~50 certs/week limit) | DNS already wildcards; swap the Caddy image for an xcaddy build with the Route 53 DNS plugin and replace `tenants.caddy` with one `*.feohledger.com` site block. |
| Real provider adapters (payments, cards, AI extraction, ERP, sanctions…) | Going live with real money / real data | Per-org `Organization.settings.*` flips + sops keys — zero infrastructure. |
| Background sweeps (payment reconciler, audit shipping, renewals, dunning…) | First real payments / compliance needs | `FEOH_*_ENABLED=true` in the sops env, redeploy. |
| SES production access | Emailing unverified recipients (self-service signup) | AWS console request; until it clears, `FEOH_EMAIL_PROVIDER=console` + CLI-provisioned tenants. |
| Multi-instance / ECS / ALB | >1 instance needed | The full `production-deployment.md` build-out; the compose file retires. Nothing here changes shape — the same image, env contract, DB schema, and S3 layout move onto ECS. |

## Implementation status

The deploy files are **built** and live under [`deploy/`](../deploy/):
`bootstrap-vm.sh` (one-shot VM setup), `compose.prod.yml` (with API
healthcheck + the RDS/ElastiCache override seams), `Caddyfile`
(+ `tenants.caddy.example`), `deploy.sh` (preflight → build → migrate → roll
→ verify), `add-tenant.sh` (tenant + Caddy + reload in one command,
re-runnable via `--skip-existing`), `backup.sh`, `restore.sh` (streamed
restore of any night's dumps), and `env.example` (the sops env contract,
validated by deploy.sh). Also shipped: the S3 client factory now falls back to real AWS +
the instance-profile credential chain when `FEOH_S3_ENDPOINT_URL` and the
static keys are set empty (previously it always passed the MinIO dev
defaults, so the "omit the endpoint for real S3" story couldn't work).

Still optional / not built: Terraform for the VM + instance profile —
clicking it out in the console is defensible at this scale; the S3/KMS module
in `infra/` already exists.
