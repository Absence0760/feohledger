# deploy/ — minimal single-VM production stack

Operational files for the single-VM deployment described in
[`docs/minimal-deployment.md`](../docs/minimal-deployment.md) (read that
first — it holds the architecture, cost model, and the how-to-add-it-later
paths for everything this footprint leaves out). The database is Amazon RDS
(~$45–55/month all in) or, as the cheaper alternative, a Postgres container on
the VM (~$22/month); one key in the secrets file picks, and `lib.sh` is the
one place every script reads the choice from
([§ Database](../docs/minimal-deployment.md#database)).

The whole flow is four commands on a fresh VM:

```
./bootstrap-vm.sh                     # once: docker, compose, sops, swap, cron, IMDS fix
# copy infra-secrets' feohledger/prod.sops.yaml in as deploy/prod.sops.yaml, log out/in (docker group), then:
./deploy.sh                           # every deploy: build, migrate, roll, verify
./add-tenant.sh acme --name "Acme" --admin-email admin@acme.com
```

| File | Purpose |
|---|---|
| `bootstrap-vm.sh` | One-time, idempotent VM setup (Amazon Linux 2023): docker + compose plugin + sops + cronie (AL2023 ships no cron daemon) + AWS CLI, automatic security updates (dnf-automatic; docker/containerd excluded so the stack never bounces at a random hour), 2 GB swap, nightly backup cron, IMDSv2 hop-limit fix. Other distros get the manual list. |
| `compose.prod.yml` | Redis (AOF) + API + Caddy, plus Postgres (pgvector) under the `localdb` profile for container mode only. No DB host ports; S3 is real AWS. API healthcheck lets deploys verify themselves. Container logs capped (json-file, 10 MB × 5 per service) so they can't fill the 30 GB disk. `FEOH_DATABASE_URL` (set → RDS) and `FEOH_REDIS_URL` are override seams; `PGSSLMODE` is passed to the api (default `prefer`). The one-shot `pgtools` service (same Postgres image, RDS CA bundle mounted, `tools` profile) carries the pg client tools for RDS. Also the one-shot `frontend-build` service (Node image + pnpm-store cache) behind a `build` profile, so `up` never starts it — `deploy.sh` runs it. Every image is `repo:tag@sha256:…`, bumped by Dependabot (`backend/docs/docker.md` § Image pinning). |
| `Caddyfile` | TLS + static SPA + `api.feohledger.com` reverse proxy. Domains via env. |
| `tenants.caddy.example` | Template for the per-VM tenant host list (`tenants.caddy`, gitignored). `add-tenant.sh` maintains it — manual edits rarely needed. |
| `lib.sh` | Sourced by every script below, never run: decides the database mode from `deploy/.env` (`FEOH_DATABASE_URL` set → RDS, else the local container), builds the compose command (`--profile localdb` in container mode), and runs pg client tools in that mode (`feoh_pg`: `exec` into `postgres`, or `pgtools` against RDS with the password passed through the environment, never an argv). |
| `compose.sh` / `psql.sh` | Ad-hoc `docker compose` / `psql` with the database mode applied — use these instead of a bare `docker compose -f compose.prod.yml …`, which in container mode would start the stack without its database. |
| `deploy.sh` | Preflight → pull → decrypt secrets → frontend build (`docker compose run --rm frontend-build`; no Node/pnpm on the VM) → backend build → migrate (control plane + all tenants) **before** rolling → `up -d --wait` → Caddy reload. Flags: `--no-pull`, `--backend-only`, `--frontend-only`. |
| `add-tenant.sh` | Tenant DB + org + admin user (same `provision_tenant` path as signup) + Caddy host block + reload, in one shot. Generates a temp password (first-login change forced) unless `--admin-password` given. |
| `remove-tenant.sh` | The inverse of `add-tenant.sh`, and the deletion `/legal/dpa` § 13 promises within 60 days of termination: removes the Caddy host block and reloads (stop serving first), runs `scripts/delete_tenant.py` in the api container (documents → tenant DB → control-plane rows), then deletes every version of that tenant's nightly dumps from the backup bucket. `--dry-run` prints the inventory and changes nothing; otherwise it makes you type the slug back. Prints the written confirmation to send the customer, including the two residues it does NOT reach. |
| `backup.sh` | Nightly pg dumps (globals + control plane + every `feoh_*` DB) streamed to S3. Cron installed by bootstrap. On RDS these are the long-retention, provider-independent copy beside RDS's automated backups + PITR (globals with `--no-role-passwords`); in container mode they are the whole DR story. Optional `BACKUP_PING_URL` heartbeat (healthchecks.io-style) so silent failures get noticed. |
| `restore.sh` | Streams a night's dumps back from S3 — each DB via `pg_restore --create` (skips existing DBs unless `--force`). Container mode replays globals first; on RDS it skips them and restores with `--no-owner --no-acl` so everything belongs to the master user. Stops the api for the duration, rolls the stack back up after. Test it once against a scratch stack. On RDS, a point-in-time restore is the first choice (`docs/minimal-deployment.md` § Point-in-time restore). |
| `decrypt-env.sh` | Decrypts `prod.sops.yaml` to `.env` and checks it — required keys, JWT key strength, values compose's `env_file` would silently rewrite, and the database mode (RDS: a URL `lib.sh` can parse, `PGSSLMODE` `verify-full`; container: `POSTGRES_PASSWORD`, no TLS mode it cannot serve) — before it replaces the current `.env`. `deploy.sh` runs it; run it on its own to check a new secrets file without deploying. |
| `prod.sops.yaml.example` | Template for the VM's secrets file: flat YAML keyed by env var name, every value quoted. The encrypted original lives in `infra-secrets` (`docs/decisions.md` §171). |

## Before the VM (once per project)

- AWS account + the `infra/` Terraform module applied (S3 buckets incl. the
  lifecycle-expired backups bucket — its `backups_bucket` output feeds
  `BACKUP_S3_BUCKET` — and the KMS key).
- EC2 `t4g.small` (Amazon Linux 2023 arm64 recommended), 30 GB gp3, ports
  80/443 open (TCP, plus UDP 443 — Caddy serves HTTP/3; without the UDP rule
  browsers silently fall back to HTTP/2). Instance profile: `kms:Decrypt` on
  the sops key; `kms:GenerateDataKey` + `kms:Decrypt` on the `infra/` **app**
  key (`app_kms_key_arn` output) — all three buckets encrypt under it, so S3
  refuses every upload and every backup without it; S3 read/write
  (`s3:GetObject/PutObject/DeleteObject/AbortMultipartUpload/ListBucket`) on the
  invoice-files, audit-logs, and backup buckets (plus
  `s3:GetBucketObjectLockConfiguration` on audit-logs once S3 audit shipping is
  on — full list with reasons: `docs/minimal-deployment.md` § 1); `ses:SendEmail` if using
  SES; ideally `ec2:ModifyInstanceMetadataOptions` so bootstrap can fix the
  IMDS hop limit itself.
- Database (RDS path): the RDS for PostgreSQL 16 instance from `infra/` —
  `db.t4g.micro`, `rds.force_ssl = 1`, initial database `feohledger`, 5432
  open to the VM's security group only. Its endpoint and master password go
  into `FEOH_DATABASE_URL`, with `PGSSLMODE: "verify-full"`
  (`docs/minimal-deployment.md` § The RDS instance).
- DNS: three records → this VM: `feohledger.com`, `api.feohledger.com`, and a
  **wildcard** `*.feohledger.com` (the wildcard makes tenant onboarding
  DNS-free; it needs no wildcard certificate — Caddy issues per-host certs).
- Secrets: in the **private** `infra-secrets` repo, create
  `feohledger/prod.sops.yaml` from `prod.sops.yaml.example` —
  `aws sso login --profile feohledger`, then
  `AWS_PROFILE=feohledger sops feohledger/prod.sops.yaml` (paste, fill, save;
  sops writes it encrypted) — and commit it there. Copy it onto the VM as
  `deploy/prod.sops.yaml`; `./decrypt-env.sh` checks it without deploying.
  Generate keys in your own terminal. Never commit either file here — this
  repo is public. Pattern: `~/github/project-mgmt/docs/secrets-management.md`.

## Deploys

`./deploy.sh` — it preflights its own prerequisites and required env keys,
runs migrations before the new API serves traffic, and fails loudly (via the
compose healthcheck) if the API doesn't come up. If the build or migration
step fails, the previously-running containers keep serving.

**A VM first deployed before the frontend build moved into compose**
(`docs/decisions.md` §203) still holds the old pnpm cache volume,
`feoh-prod-pnpm-store`; the build now uses the compose-managed
`feoh-prod_pnpm-store`, so the first deploy after that change builds with a cold
cache and the old volume is never read again. Drop it once:
`docker volume rm feoh-prod-pnpm-store`.

While you're in a deploy window: OS security patches auto-apply nightly
(dnf-automatic, installed by bootstrap), but **docker/containerd are excluded**
— their updates restart the daemon and would bounce the stack at a random
hour, and the exclude also hides them from dnf-automatic's own reporting.
Check them here, where a bounce is fine:
`sudo dnf upgrade --refresh 'docker*' 'containerd*'`

## Tenants

`./add-tenant.sh <slug> --name "Company" --admin-email admin@company.com` —
provisions everything and prints the login URL + temp password. Don't run
`scripts/seed.py` (demo data) in prod.

## Backups

Installed by bootstrap as `/etc/cron.d/feoh-backup` (03:17 UTC nightly,
logging to `/var/log/feoh-backup.log`). Set `BACKUP_PING_URL` in the sops env
to get a heartbeat ping after each successful run.

Restore with `./restore.sh <YYYY-MM-DD> [--force] [db ...]` — globals first,
then each DB via `pg_restore --create`, streamed straight from S3; existing
DBs are skipped unless `--force` (drop + recreate). A restore that fails
partway deliberately leaves the api stopped (don't serve a half-restored
stack) — fix the cause and re-run, or `./compose.sh up -d --wait` to bring it
back as-is. **Test a restore once against a scratch stack before calling
backups done.**

On RDS the dumps are the second line: RDS's automated backups give a
point-in-time restore to any second in the retention window, into a new
instance — the commands are in `docs/minimal-deployment.md` § Point-in-time
restore. The dumps are what survives losing the instance, its snapshots, or
the account, and what restores into a Postgres anywhere.
