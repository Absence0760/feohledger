# Minimal-cost deployment

How to get the whole app on the public internet for **~$45–55/month** with the
database on Amazon RDS — or **~$22/month** with Postgres in a container on the
same VM ([§ Database](#database)) — without building any of the reference AWS
architecture in [`production-deployment.md`](production-deployment.md). That
doc stays the scale-up target; this one is the pilot / first-customers
footprint.

**Total: one small VM running Docker Compose + Caddy, a db.t4g.micro RDS
Postgres 16 for the databases, real S3 for files, everything else in-process.**

```
        *.feohledger.com  ──────────► one VM (EC2 t4g.medium)
                                 ├── Caddy         — TLS, static frontend, /api reverse-proxy
                                 ├── FastAPI       — backend container (uvicorn)
                                 └── Redis 7       — token blocklist, rate limits, MFA state
                                        │  TLS (verify-full)
                                        ▼
                                 RDS Postgres 16   — db.t4g.micro, pgvector (control + tenant DBs),
                                                     automated backups + point-in-time restore
                                        │
                                     AWS S3 — invoice files, nightly logical dumps (no MinIO in prod)
```

The cheaper alternative keeps Postgres (the `pgvector/pgvector` `-pg16` image)
in a container on the VM instead — the same scripts, chosen by one key in the
secrets file ([§ Database](#database)).

## Why this works without the big build-out

The app was designed local-first, and that carries straight into a cheap deploy:

- `FEOH_EXTRACTION_MODE` / `FEOH_ERP_MODE` / `FEOH_AUDIT_MODE` default to `local` —
  in-process worker threads, **no SQS, no Lambda**.
- Every provider integration defaults to its `mock` adapter; real providers
  (a payment rail, Lithic) are per-org config flips later, not infrastructure.
  **Invoice extraction is the exception:** a deployed env never falls back to
  its mock, which would invent fields on a real document, so it needs a
  decision before first boot (§ 3).
- All background sweeps are asyncio tasks inside the API process, each behind
  an `FEOH_*_ENABLED` flag.
- The frontend is a static SPA; its build-time inputs are `PUBLIC_API_URL` and
  `PUBLIC_SITE_URL` (the link-preview card's image origin), both derived by
  `deploy.sh` from domains the env already declares.
- Tenant routing is subdomain → `X-Tenant-Slug`; CORS for that is already
  solved by `FEOH_CORS_PRODUCTION_DOMAIN` (wildcard subdomain regex).

## What this deliberately does NOT give you

Single AZ, single instance, no autoscaling, a single-AZ database instance (no
standby), manual deploys, restore as the failover story. Acceptable for a
pilot; see [What's left out](#whats-left-out--and-how-to-add-it-later) for
when each piece graduates.

## Cost breakdown

| Item | Monthly (us-east-1, on-demand) |
|---|---|
| EC2 `t4g.medium` (2 vCPU ARM, 4 GB) | ~$24.50 |
| EBS 30 GB gp3 | ~$2.40 |
| Public IPv4 address | ~$3.65 |
| Route 53 hosted zone | $0.50 |
| KMS keys ×2 (sops + the `infra/` app key for SSE-KMS) | $2.00 |
| S3 (files + backups, pilot volume) + SES | ~$1 |
| RDS `db.t4g.micro` Postgres 16, single-AZ (2 vCPU burstable, 1 GB) | ~$11.70 |
| RDS storage 20 GB gp3 (backup storage up to the DB's size is free) | ~$2.30 |
| VPC flow logs (rejected traffic only) + CloudWatch alarms | ~$1–2 |
| **Total** | **~$45–55** |

This is the stack `infra/` defines (`docs/decisions.md` §254). Both instances are
burstable in unlimited credit mode, so sustained load adds surplus-CPU charges on
top. **Postgres on the VM instead of RDS** (and a `t4g.small`) drops the two RDS
rows: **~$22**. What
that gives up is the managed recovery — automated backups, point-in-time
restore, a database that outlives the VM ([§ Database](#database)). Check
current prices before quoting any of these: they are list prices at the time
of writing, not a bill.

The domain is extra: `feohledger.com` is registered through Route 53 at
$16/year (`docs/decisions.md` §167).

**Cheaper still:** a Hetzner CAX11 (2 vCPU ARM, 4 GB, ~€3.79) with Postgres
on the box replaces the EC2+EBS+IPv4 and RDS rows → **~$7/month total** (keep
KMS + Route 53 + S3 on AWS).
Trade-off: no instance profile, so the box needs a scoped IAM access key for
S3/KMS instead of role-based credentials, and it sits outside the AWS-org
guardrails. The AWS path is recommended because the estate tooling (org
sub-account, sops KMS key, OIDC role) already automates it.

`t4g.medium` rather than `t4g.small` because AI extraction and PDF rendering
run in-process beside the API, Redis and Caddy, and 2 GB leaves no headroom.
Resizing is a change to `app_instance_type` in the tfvars (a stop → change-type
→ start). Add 2 GB of swap either way.

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
   the VM keeps a copy of `infra-secrets/feohledger/prod.sops.yaml` as
   `deploy/prod.sops.yaml`, and `deploy/decrypt-env.sh` decrypts it host-side
   to the gitignored `deploy/.env` on every deploy — the compose file reads it
   via `env_file` + interpolation. The contract is
   `deploy/prod.sops.yaml.example` (`docs/decisions.md` §171).
5. **Manual deploys.** SSH in: `git pull`, rebuild, migrate, restart (script
   below). `aws-deploy.yml` stays disarmed (`AWS_DEPLOY_ENABLED` unset) until
   the ECS build-out exists.
6. **The database is RDS from day one.** Postgres is the one component whose
   loss is data loss, and RDS turns its recovery from "last night's dump" into
   automated backups with point-in-time restore, on a database that outlives
   the VM and is patched without anyone remembering to — for ~$14/month. It is
   wired through the override seam the compose file always had
   (`FEOH_DATABASE_URL` in the secrets file), so the container mode stays a
   one-key fallback rather than a fork ([§ Database](#database)).

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

- **Defined in Terraform** (`infra/compute.tf`; apply steps in `infra/README.md`
  § Workload stack) — the bullets below describe what it creates, and are the
  checklist if you ever build the VM by hand instead.
- EC2 `t4g.medium`, Amazon Linux 2023 arm64, 30 GB gp3, security group: 80/443
  from anywhere (TCP, plus UDP 443 — Caddy serves HTTP/3; without the UDP
  rule browsers silently fall back to HTTP/2), 22 from your IP (or SSM
  Session Manager and no 22 at all).
- Instance profile — the box holds no static AWS keys, so this role is every
  AWS permission it has:
  - `kms:Decrypt` on the **sops** key (`decrypt-env.sh` decrypts `prod.sops.yaml` with it).
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
  - `ses:SendEmail` on the SES identity (the `ses_identity_arn` output of
    `infra/`) if using SES; ideally `ec2:ModifyInstanceMetadataOptions`
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

### 1b. Database (RDS)

An RDS for PostgreSQL 16 instance — what to create, and how the VM reaches it,
is [§ Database](#database). In short: `db.t4g.micro`, single-AZ, encrypted
gp3 storage, automated backups on (that retention window *is* the
point-in-time-restore window), `rds.force_ssl = 1`, an initial database named
`feohledger`, not publicly accessible, and a security group admitting 5432
from the VM's security group only. The instance profile needs nothing for it —
the app authenticates with the master password, not IAM.

### 2. Production compose stack (`deploy/compose.prod.yml` — built)

Four long-running services, plus a one-shot build (see
[`deploy/README.md`](../deploy/README.md) for operations). Every image is pinned
to a release tag plus its index digest (`repo:tag@sha256:…`), the same refs
`backend/docker-compose.yml` uses — and CI tests — for Postgres and Redis, so a
redeploy can never pick up an upstream retag; bumps arrive as Dependabot
`docker-compose` PRs
([`backend/docs/docker.md` § Image pinning](../backend/docs/docker.md#image-pinning)).

- `postgres` — `pgvector/pgvector` on its `-pg16` line (Postgres 16),
  volume-backed, **no host port** (compose-network only); password from the
  sops env. **Container mode only:** it sits under the `localdb` profile, which
  `deploy/lib.sh` activates when the secrets file leaves `FEOH_DATABASE_URL`
  unset. On the RDS path it never starts, and the api's dependency on it is
  `required: false`.
- `pgtools` — the same pinned image as a one-shot (`tools` profile), carrying
  `pg_dump` / `pg_dumpall` / `psql` / `pg_restore` for `backup.sh` and
  `restore.sh` to run against RDS. The RDS CA bundle is mounted in for
  `verify-full` ([§ Database TLS](#database-tls)).
- `redis` — `redis` 7.x alpine with `--appendonly yes`, no host port.
- `api` — built from `backend/Dockerfile` (works on arm64; the lock resolves
  universally — if an arm64 wheel gap ever bites, fall back to an x86
  `t3a.small`, ~$14). Runs the image CMD, `uvicorn app.main:app` (the
  production entrypoint — not `main.py`). `FEOH_DATABASE_URL` comes from the
  sops env on the RDS path; in container mode it (and `FEOH_REDIS_URL`
  always) is derived in the compose file, from `POSTGRES_PASSWORD`, so the DB
  password lives in exactly one sops entry. `PGSSLMODE` defaults to `prefer`
  (asyncpg's own default — what the app always did); the RDS path sets
  `verify-full`. The compose network is pinned
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
- `frontend-build` — the Node 24 alpine container `deploy.sh` builds the SPA
  in (`docker compose run --rm frontend-build`), with the repo bind-mounted and
  the pnpm store cached in a volume. A `build` profile keeps `up` from ever
  starting it. It lives in the compose file, not as a `docker run` in
  `deploy.sh`, so Dependabot reads its image (`docs/decisions.md` §203).

The frontend is built by the deploy script with
`PUBLIC_API_URL=https://<API_DOMAIN>` and `PUBLIC_SITE_URL=https://<APP_DOMAIN>`
baked in. The second is the origin of the absolute `og:image` URL in the
link-preview card (`frontend/src/app.html`); see
[environment.md](environment.md) § Frontend.

### 3. Backend env (`prod.sops.yaml` — contract: `deploy/prod.sops.yaml.example`)

The whole env lives in one sops file, `infra-secrets/feohledger/prod.sops.yaml`:
flat YAML whose keys are the variable names below, every value double-quoted.
Create it from the template — `aws sso login --profile feohledger`, then
`AWS_PROFILE=feohledger sops feohledger/prod.sops.yaml` inside `infra-secrets`
(paste, fill, save; sops writes it encrypted) — and commit it there. Generate
keys in your own terminal. The same file is what the ECS stack's Terraform will
read later, under the same names.

Beyond the committed defaults, the deployed env sets at minimum:

| Var | Value |
|---|---|
| `FEOH_ENVIRONMENT` | `production` (arms hCaptcha enforcement on signup, while signup is on) |
| `FEOH_SECRET_KEY` | `openssl rand -hex 32` |
| `FEOH_SIGNUP_ENABLED` | `false` to keep self-service signup closed (every `/api/signup/*` route 404s and `/signup` says signup is closed) — the usual choice here, with tenants provisioned by `deploy/add-tenant.sh`. Leave it unset (on) only when you want public signup, and then set both hCaptcha keys |
| `FEOH_HCAPTCHA_SECRET` / `FEOH_HCAPTCHA_SITEKEY` | **Required while signup is on** — the API refuses to boot in production with the secret empty, and `deploy.sh` refuses first. With `FEOH_SIGNUP_ENABLED=false` neither is needed. |
| `FEOH_DATABASE_URL` | **RDS path:** `postgresql+asyncpg://<master user>:<master password>@<RDS endpoint>:5432/feohledger` — the endpoint host name exactly as RDS reports it (verify-full checks the certificate against it), no query string, and a URL-safe password (`openssl rand -hex 24`; anything else percent-encoded). Setting it is what selects the RDS path. **Container path:** leave unset — compose derives it from `POSTGRES_PASSWORD` |
| `PGSSLMODE` | **RDS path:** `verify-full` (required; `require` / `verify-ca` warn, anything weaker is refused). **Container path:** leave unset — the container serves no TLS |
| `POSTGRES_PASSWORD` | **Container path:** `openssl rand -hex 24` (compose derives `FEOH_DATABASE_URL` from it). **RDS path:** leave empty — nothing reads it |
| `FEOH_S3_BUCKET` | invoice-files bucket; set `FEOH_S3_ENDPOINT_URL` / `FEOH_S3_ACCESS_KEY` / `FEOH_S3_SECRET_KEY` **empty** → real S3 via the instance-profile credential chain |
| `FEOH_MFA_ENABLED` / `FEOH_HSTS_ENABLED` | `true` / `true` |
| `FEOH_WEBAUTHN_RP_ID` / `FEOH_WEBAUTHN_ORIGINS` | `feohledger.com` / `https://feohledger.com,https://*.feohledger.com` — with MFA on, the localhost dev defaults reject every prod origin and passkeys silently fail; the wildcard entry covers each tenant subdomain |
| `FEOH_PUBLIC_URL` / `FEOH_API_PUBLIC_URL` | `https://feohledger.com` / `https://api.feohledger.com` |
| `FEOH_TENANT_URL_TEMPLATE` | `https://{slug}.feohledger.com` |
| `FEOH_CORS_PRODUCTION_DOMAIN` | `feohledger.com` |
| `FEOH_DEPLOYED_REGION` | the region this VM runs in (`us`/`eu`/`uk`/`ca`/`au`) — advisory only, but empty makes every tenant's data-residency `alignment` report `unknown` / `aligned: null` ("cannot attest") |
| `FEOH_EMAIL_PROVIDER` / `FEOH_EMAIL_FROM` | `ses` / verified sender |
| `FEOH_ANTHROPIC_API_KEY` **or** `FEOH_EXTRACTION_PROVIDER` | **Decide before first boot.** A real key (Claude Vision; Anthropic becomes a sub-processor of tenant invoices), `mock` (fabricated fields — a demo box only), or neither (invoices keyed in by hand; every upload's extraction fails and `deploy.sh` warns). Extraction is the one adapter that does not fall back to `mock` in a deployed env — `backend/docs/ai-extraction.md` § Platform provider precedence |
| `FEOH_APPROVAL_SIGNING_KEY` + the other HMAC signing keys | real values (each key's presence is its feature's on-switch; leave unset = feature off) |

Everything else keeps its safe default: mock adapters (extraction excepted,
above), `local` modes, sweeps off. Flip individual `FEOH_*_ENABLED` sweeps on once there's a reason
(`FEOH_PAYMENT_RECONCILE_ENABLED` and `FEOH_AUDIT_SHIPPING_ENABLED` are the two
worth enabling first when real payments/compliance start).

SES note: `infra/email.tf` creates the SES identity for the platform domain, its
DKIM and MAIL FROM records, and the Migadu mailbox records beside them — the
bring-up order is `infra/README.md` § Email. A fresh SES account is still
sandboxed (verified recipients only). Either request production access, or keep
self-service signup closed at first (`FEOH_SIGNUP_ENABLED=false`, above) and
provision tenants with `deploy/add-tenant.sh`, leaving email on `console` until
SES clears.

### 4. First boot + deploys (`deploy/deploy.sh` — built)

Copy `prod.sops.yaml` onto the VM as `deploy/prod.sops.yaml` (`deploy/decrypt-env.sh`
checks it without deploying), then run
`deploy/deploy.sh`: it preflights its own prerequisites and the required env
keys (clear errors before any work happens), pulls main, decrypts secrets,
builds the frontend in the `frontend-build` service's `node:24` container
(`PUBLIC_API_URL` baked from `API_DOMAIN`, `PUBLIC_SITE_URL` from `APP_DOMAIN`;
pnpm store cached in a volume) and the backend image, runs
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

### 5. Backups (RDS automated backups + `deploy/backup.sh`)

Two layers on the RDS path, one in container mode:

- **RDS automated backups + point-in-time restore (RDS path — the primary
  recovery).** A daily storage snapshot plus transaction logs shipped about
  every five minutes, kept for the instance's backup retention period. Any
  second inside that window can be restored — to a **new** instance (PITR
  never overwrites the source); see [Point-in-time restore](#point-in-time-restore)
  below for the commands. They live with the instance's account and region,
  which is why the second layer stays.
- **Nightly logical dumps (both paths — on RDS the long-retention,
  provider-independent copy; in container mode the whole DR story).** Cron
  (installed by `bootstrap-vm.sh` as `/etc/cron.d/feoh-backup`) dumps role
  globals + a per-DB `pg_dump -Fc` of `feohledger` and every `feoh_*` tenant
  DB, streamed straight to the backups bucket (nothing persists on disk) —
  restorable into any Postgres 16 anywhere, which is what makes it the copy
  that survives losing the RDS instance, its snapshots, or the account. In
  container mode the tools run inside the `postgres` container; on the RDS
  path `backup.sh` runs them in the one-shot `pgtools` container against the
  RDS endpoint over `verify-full` TLS, with the password handed over in the
  environment (never an argv `ps` shows), and dumps globals with
  `--no-role-passwords` — RDS does not let even the master user read
  `pg_authid`. The bucket is
  provisioned by the `infra/` module (`backups_bucket_name`) with the cost
  guards baked in: 90-day expiry (`backup_retention_days`), noncurrent-version
  cleanup, and incomplete-multipart reaping — no manual lifecycle rule to
  remember. Set `BACKUP_S3_BUCKET` from the `backups_bucket` output; the
  instance profile already has the access.
- Optional heartbeat: set `BACKUP_PING_URL` (healthchecks.io-style) in the
  sops env and `backup.sh` pings it after every successful run — silence
  means backups stopped, noticed before a restore needs them.
- Weekly EBS snapshot (Data Lifecycle Manager, free to configure) as the
  coarse fallback — in container mode the only copy of `pgdata` besides the
  dumps; on the RDS path the VM holds no data worth snapshotting.
- Restore from the dumps is scripted: `deploy/restore.sh <YYYY-MM-DD>
  [--force] [db …]`, streamed straight from S3; existing DBs are skipped
  unless `--force`. Container mode replays the role globals and then each DB
  via `pg_restore --create`. **On the RDS path it does not replay globals and
  restores each DB with `--no-owner --no-acl`:** the app needs exactly one
  role — the master user it connects as, which RDS already has — and the
  dumped roles cannot be recreated as dumped (a container dump's `postgres`
  is a SUPERUSER, which RDS refuses; an RDS dump's `rds*` roles belong to
  AWS). Without `--no-owner`, a container dump restored into RDS would try to
  hand every object to a `postgres` role the app does not connect as. The
  master user is `rds_superuser`, which is enough for `pg_restore --create`
  (`CREATEDB`) and for the `CREATE EXTENSION vector` inside each tenant dump.
  **Test a restore once** before calling this done: scratch stack,
  `restore.sh <yesterday>`, log in.
- RPO / RTO: on the RDS path the database's RPO is about five minutes (the
  transaction-log interval) and a VM loss loses no data at all; RTO is a
  PITR (tens of minutes) or a VM rebuild. In container mode RPO ≈ 24 h and
  RTO ≈ hours (new VM + restore). The *published* targets are
  `docs/backup-disaster-recovery.md` § Targets — tighten them there
  deliberately, not by inference from this paragraph.

#### Point-in-time restore

The operator runs these, from a machine with the FeohLedger profile
(`aws sso login --profile feohledger`) — one line each. PITR creates a **new**
instance from the source's backups; the source keeps running untouched until
you switch.

1. Find the window: `aws rds describe-db-instances --profile feohledger --db-instance-identifier <instance> --query 'DBInstances[0].[EarliestRestorableTime,LatestRestorableTime]' --output text`
2. Restore to a new instance, reusing the source's network and parameter group
   (so `rds.force_ssl` carries over): `aws rds restore-db-instance-to-point-in-time --profile feohledger --source-db-instance-identifier <instance> --target-db-instance-identifier <instance>-pitr-<yyyymmddhhmm> --restore-time <YYYY-MM-DDTHH:MM:SSZ> --db-instance-class db.t4g.micro --db-subnet-group-name <subnet group> --vpc-security-group-ids <db security group> --db-parameter-group-name <parameter group> --no-publicly-accessible`
   (`--use-latest-restorable-time` in place of `--restore-time` for "as late
   as possible").
3. Wait, then read its endpoint: `aws rds wait db-instance-available --profile feohledger --db-instance-identifier <instance>-pitr-<yyyymmddhhmm>` and `aws rds describe-db-instances --profile feohledger --db-instance-identifier <instance>-pitr-<yyyymmddhhmm> --query 'DBInstances[0].Endpoint.Address' --output text`
4. Switch the app to it. Either change the host in `FEOH_DATABASE_URL` in
   `infra-secrets/feohledger/prod.sops.yaml` (same master user and password —
   they are restored with the data), copy the file onto the VM and run
   `./deploy.sh`; or keep the endpoint by renaming — `aws rds modify-db-instance --profile feohledger --db-instance-identifier <instance> --new-db-instance-identifier <instance>-old --apply-immediately`,
   wait, then the same with `--db-instance-identifier <instance>-pitr-<yyyymmddhhmm> --new-db-instance-identifier <instance>`
   — the endpoint host name follows the identifier, so the secrets file does
   not change; restart the api (`./compose.sh restart api`). Either way,
   **reconcile Terraform afterwards** (`terraform import` / state moves for the
   instance that is now live): until you do, a `terraform apply` sees an
   instance it did not create.
5. Run the migrations against it if the restore point predates a deploy
   (`./deploy.sh --no-pull --backend-only` does it), then delete the old
   instance once you are sure — `aws rds delete-db-instance --profile feohledger --db-instance-identifier <instance>-old --final-db-snapshot-identifier <instance>-old-final`.

PITR restores the **whole instance** — every tenant rolls back together. To
recover one tenant's data without rolling back the rest, restore to a new
instance as above, then copy just that database across: from the VM,
`pg_dump -Fc` it out of the restored instance and `pg_restore --clean --create`
it into the live one (the `pgtools` service runs both — the same shape as
`restore.sh`).

## Database

One key in the secrets file chooses where Postgres runs, and `deploy/lib.sh`
is the one place every deploy script reads that choice from:

| | RDS (documented path) | Container on the VM (cheaper alternative) |
|---|---|---|
| Selected by | `FEOH_DATABASE_URL` set in `prod.sops.yaml` | `FEOH_DATABASE_URL` unset |
| Where Postgres runs | RDS for PostgreSQL 16, `db.t4g.micro` | the `postgres` service, `localdb` compose profile |
| TLS | `PGSSLMODE=verify-full` against the RDS CA bundle | none (compose network only); `PGSSLMODE` defaults to `prefer` |
| Primary recovery | automated backups + point-in-time restore | the nightly dumps |
| Nightly dumps | yes — `pgtools` container, long-retention copy | yes — `exec` into `postgres` |
| Cost | ~$45–55/month | ~$22/month |

Ad-hoc compose commands on the VM go through **`deploy/compose.sh`**, which
applies the same choice — `./compose.sh logs api`, `./compose.sh up -d
--wait`. A bare `docker compose -f compose.prod.yml up` in container mode
would start the stack without its database.

### The RDS instance

Created by the Terraform in `infra/` (the operator does not click it out):
RDS for PostgreSQL **16** (the major the `pgtools` client and the container
image share), `db.t4g.micro`, single-AZ, 20 GB gp3, storage encrypted,
automated backups on with a retention period that is the PITR window,
`rds.force_ssl = 1` in its parameter group (the PG 15+ default, set
explicitly so it survives a parameter-group swap), **an initial database named
`feohledger`** (`alembic upgrade head` connects to it and does not create it),
not publicly accessible, and a security group admitting TCP 5432 from the VM's
security group and nothing else. The master password must be URL-safe
(`openssl rand -hex 24`, or Terraform's `random_password` with
`special = false`): it is embedded in `FEOH_DATABASE_URL`, and `decrypt-env.sh`
refuses a URL `lib.sh` cannot hand to libpq.

The app connects as the master user. That user is a member of
`rds_superuser`, **not** a superuser, and nothing in the app needs more:
tenant provisioning's `CREATE DATABASE` / `DROP DATABASE … WITH (FORCE)` need
`CREATEDB` and ownership, which it has; the audit-log immutability triggers
are ordinary `CREATE FUNCTION` / `CREATE TRIGGER`; no code path uses an event
trigger, `ALTER SYSTEM`, or a role change.

### Database TLS

Every connection the app opens reads its TLS mode from the environment, and
the stack sets it in one place:

- **The mechanism.** asyncpg falls back to `PGSSLMODE` / `PGSSLROOTCERT` when
  the caller passes no `ssl` argument — verified against the pinned asyncpg
  (0.31.0, `connect_utils._parse_connect_dsn_and_args`): `ssl` is taken from
  the URL's `sslmode`, else `os.getenv('PGSSLMODE')`, else `prefer`; at
  `require` and above it loads `PGSSLROOTCERT`, and `verify-full` also checks
  the host name. Nothing in the app passes `ssl`: SQLAlchemy's asyncpg
  dialect forwards only the URL's query string, and the raw
  `asyncpg.connect` calls (`tenant_provisioning.asyncpg_connect_kwargs`, used
  by provisioning, deletion, `seed.py` and the rename script) parse the URL
  with the same `make_url` the engines use. So the control-plane engine, every
  tenant engine, Alembic (control and per-tenant), the raw CREATE/DROP
  DATABASE connection and the three Lambda handlers all honour it. libpq —
  `pg_dump`, `psql`, `pg_restore` in `pgtools` — reads the same two variables
  natively, which is why the URL contract forbids a query string: an `?ssl=`
  would reach asyncpg but not libpq.
- **The trust anchor.** `backend/certs/rds-global-bundle.pem` is AWS's RDS
  global CA bundle (every region's RDS root and intermediate CAs), committed
  rather than downloaded at build time so a bump is a reviewed diff;
  `tests/test_container_supply_chain.py` pins its sha256. The backend image
  sets `PGSSLROOTCERT` to it (`backend/Dockerfile`), and `compose.prod.yml`
  mounts the same file into `pgtools`. It is deliberately *not* in the
  system trust store — these CAs are for the database, not every HTTPS call.
  To refresh it (AWS adds a CA for a new region or rotation): download
  `https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem` over
  `backend/certs/rds-global-bundle.pem`, update `RDS_CA_BUNDLE_SHA256`, run
  the supply-chain test, redeploy.
- **The mode.** `compose.prod.yml` passes `PGSSLMODE` to the api and to
  `pgtools`, defaulting to `prefer` — asyncpg's own default, so container
  mode behaves exactly as it always did (the container serves no TLS, so
  `prefer` falls back to plaintext on the compose network). The RDS path sets
  `PGSSLMODE: "verify-full"` in the secrets file, and `decrypt-env.sh` refuses
  it unset or weaker than `require` there, and refuses a verifying mode in
  container mode (it could never connect). `verify-full` checks the
  certificate against the URL's host, so `FEOH_DATABASE_URL` must use the RDS
  endpoint host name exactly as RDS reports it — not an IP, not a CNAME of
  your own.
- **The Lambda workers**, when they exist (§ What's left out), run the same
  image, so `PGSSLROOTCERT` is already set; their function environment needs
  `PGSSLMODE=verify-full` beside `DATABASE_URL`.

### pgvector on RDS

RDS for PostgreSQL 16 ships pgvector as the `vector` extension (every 16.x
minor; the pgvector version follows the minor — 0.8.x on current 16.x). No
`shared_preload_libraries` change is needed, and the master user
(`rds_superuser`) may `CREATE EXTENSION vector`, which is what tenant
provisioning (`CREATE EXTENSION IF NOT EXISTS vector`), migration
`0003_rag_embeddings` and every tenant dump's restore run. The container image
pins pgvector 0.8.7; a dump moves between the two either way, because
`pg_dump` records `CREATE EXTENSION vector` without a version. To confirm on
the live instance, from `deploy/` on the VM:
`./psql.sh -d feohledger -Atc "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'"`
(`deploy/psql.sh` is psql against whichever database the VM is on).

### The cheaper alternative: Postgres on the VM

Leave `FEOH_DATABASE_URL` and `PGSSLMODE` out of the secrets file and set
`POSTGRES_PASSWORD`: `lib.sh` turns on the `localdb` profile, the `postgres`
container runs beside the api exactly as it did before RDS, and `backup.sh` /
`restore.sh` `exec` into it. What it gives up is everything in the RDS column
of the table above — the nightly dump is then the only recovery, RPO 24 hours,
and losing the VM without a dump means losing the data.

### Moving a container-mode VM onto RDS

1. Take a fresh dump: `./backup.sh` (container mode, still).
2. Create the RDS instance ([§ The RDS instance](#the-rds-instance)) and add
   `FEOH_DATABASE_URL` + `PGSSLMODE: "verify-full"` to the secrets file
   (`POSTGRES_PASSWORD` may stay — nothing reads it on the RDS path, and it is
   the fallback's password if you need to go back).
3. Copy the file onto the VM and run `./decrypt-env.sh` — the VM is now in RDS
   mode for every script.
4. `./restore.sh <today> --force` — into RDS, without globals, with
   `--no-owner --no-acl`, so every object belongs to the master user.
   `--force` because `feohledger` already exists on the new instance (created
   empty with it) and would otherwise be skipped; nothing else is there yet to
   overwrite.
5. `./deploy.sh` — migrations run against RDS, the api rolls onto it, and the
   deploy prints a note while the old `postgres` container is still running.
   Verify (log in, check a tenant), then
   `./compose.sh --profile localdb stop postgres`. Keep the `pgdata` volume
   until you are past the point of wanting to go back.

## What's left out — and how to add it later

Every omission has a deliberate seam, so graduating one piece never means
rebuilding the stack:

| Left out | Trigger | How to add it |
|---|---|---|
| Multi-AZ RDS (a standby in a second AZ) | An uptime SLA that a single-AZ instance's failover-by-PITR cannot meet | `multi_az = true` on the instance — roughly doubles the RDS rows of the cost table. No app or deploy change: the endpoint host name stays the same across a failover. |
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
healthcheck, the `localdb` / `pgtools` split, and the RDS/ElastiCache override
seams), `Caddyfile` (+ `tenants.caddy.example`), `lib.sh` (the database-mode
decision every script sources) + `compose.sh` / `psql.sh` (ad-hoc compose and
psql in that mode), `deploy.sh` (preflight → build → migrate → roll →
verify), `add-tenant.sh` (tenant + Caddy + reload in one command,
re-runnable via `--skip-existing`), `backup.sh`, `restore.sh` (streamed
restore of any night's dumps, either database mode), and `decrypt-env.sh` +
`prod.sops.yaml.example` (the secrets file and the contract it is checked
against). Also shipped: the S3 client factory now falls back to real AWS +
the instance-profile credential chain when `FEOH_S3_ENDPOINT_URL` and the
static keys are set empty (previously it always passed the MinIO dev
defaults, so the "omit the endpoint for real S3" story couldn't work).

The VM, its instance profile and the RDS instance are being added to the
Terraform in `infra/` (see `infra/README.md` for what it defines today); the
S3/KMS module there already exists.
