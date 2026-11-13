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
./deploy.sh                           # first deploy: build the API, migrate, roll, verify (later ones: publish a release, § Deploys)
./add-tenant.sh acme --name "Acme" --admin-email admin@acme.com
```

| File | Purpose |
|---|---|
| `bootstrap-vm.sh` | One-time, idempotent VM setup (Amazon Linux 2023): docker + compose plugin at exactly `COMPOSE_VERSION` (the one place that version is written — CI installs it from this line, and a re-run replaces any other version) + sops + cronie (AL2023 ships no cron daemon) + AWS CLI, automatic security updates (dnf-automatic; docker/containerd excluded so the stack never bounces at a random hour), 2 GB swap, nightly backup cron, IMDSv2 hop-limit fix. Other distros get the manual list. |
| `compose.prod.yml` | Redis (AOF) + API + Caddy, plus Postgres (pgvector) under the `localdb` profile for container mode only. No DB host ports; S3 is real AWS. API healthcheck lets deploys verify themselves. Container logs capped (json-file, 10 MB × 5 per service) so they can't fill the 30 GB disk. `FEOH_DATABASE_URL` (set → RDS) and `FEOH_REDIS_URL` are override seams; `PGSSLMODE` is passed to the api (default `prefer`). The one-shot `pgtools` service (same Postgres image, RDS CA bundle mounted, `tools` profile) carries the pg client tools for RDS. No SPA: nothing builds or serves the frontend here (§ The SPA on S3 + CloudFront). Every image is `repo:tag@sha256:…`, bumped by Dependabot (`backend/docs/docker.md` § Image pinning). |
| `Caddyfile` | TLS + the `api.feohledger.com` reverse proxy, and the `vanity` snippet for customer domains (`/api` to the backend under the vanity `Host`, the page proxied from CloudFront). Domains via env. No apex or tenant block: those hosts are CloudFront's (`docs/decisions.md` §364). CI validates it with the pinned Caddy image (`scripts/check_deploy_caddyfile.sh`). |
| `tenants.caddy.example` | Template for the per-VM vanity host list (`tenants.caddy`, gitignored), one `import vanity` block per customer domain, added by hand (`docs/founder-runbooks/custom-domain-provisioning.md` § 3a). `deploy.sh` drops any platform-host block an older `add-tenant.sh` wrote. |
| `lib.sh` | Sourced by every script below, never run: decides the database mode from `deploy/.env` (`FEOH_DATABASE_URL` set → RDS, else the local container), builds the compose command (`--profile localdb` in container mode), and runs pg client tools in that mode (`feoh_pg`: `exec` into `postgres`, or `pgtools` against RDS with the password — or, with `FEOH_DATABASE_IAM_AUTH` on, a fresh IAM token minted per run — passed through the environment, never an argv). Also `feoh_sync_source`, `deploy.sh`'s checkout step: main's tip, or with `--ref` exactly one commit that must be on `origin/main`, always leaving the checkout on `main`. And the serving half: `feoh_prune_platform_hosts` (takes the apex and `*.<domain>` blocks out of `tenants.caddy`, in place), `feoh_caddy_reload` (reload, or recreate when Caddy's single-file mounts are stale) and `feoh_smoke_check` (one post-deploy fetch, through the VM's own Caddy or public DNS). Small readers shared so two scripts cannot disagree: `feoh_signup_enabled` (decrypt-env.sh and smoke.sh), `feoh_compose_pin` (deploy.sh and CI's installer). |
| `smoke.sh` | The post-deploy smoke check: `api/health`, a tenant host's TLS, and `/api/public-config` matching the env, plus the apex outside `--vm` (§ Deploys). `--vm` reads `.env` and goes through this VM's Caddy (deploy.sh's last step), so it checks only what the VM serves: the API and the first vanity host; with explicit flags it goes through public DNS (`aws-deploy.yml`'s last job). Exit 1 on any failed check, 2 on bad usage. |
| `compose.sh` / `psql.sh` | Ad-hoc `docker compose` / `psql` with the database mode applied — use these instead of a bare `docker compose -f compose.prod.yml …`, which in container mode would start the stack without its database. |
| `deploy.sh` | Preflight → lock (one deploy at a time, `flock` on the gitignored `.deploy.lock`) → pull main, or check out `--ref <sha>` → re-run the `deploy.sh` just checked out → decrypt secrets → prune platform hosts from `tenants.caddy` → backend build → migrate (control plane + all tenants) **before** rolling → `up -d --wait` → Caddy reload (or recreate, if its mounted config is stale) → `smoke.sh --vm`, last: the API, a vanity host's TLS and `public-config` against the env, through the VM's own Caddy. No SPA build (`docs/decisions.md` §364). Flags: `--no-pull`, `--ref <sha>` (what a release runs over SSM), and `--backend-only`, accepted for compatibility; `--frontend-only` is refused. |
| `add-tenant.sh` | Tenant DB + org + admin user (same `provision_tenant` path as signup), in one shot. Nothing on the web side: the tenant host is CloudFront's wildcard. Generates a temp password (first-login change forced) unless `--admin-password` given. |
| `remove-tenant.sh` | The inverse of `add-tenant.sh`, and the deletion `/legal/dpa` § 13 promises within 60 days of termination: runs `scripts/delete_tenant.py` in the api container (documents → tenant DB → control-plane rows), then deletes every version of that tenant's nightly dumps from the backup bucket. `--dry-run` prints the inventory and changes nothing; otherwise it makes you type the slug back. Only `delete_tenant.py`'s exit 4 (no such organisation) lets it carry on to the backup leg of a tenant already deleted; any other inventory failure (api down, database unreachable, a partner organisation refused) stops it before it changes anything. Prints the written confirmation to send the customer, including the two residues it does NOT reach, and a reminder that a vanity domain's `tenants.caddy` block is removed by hand. |
| `db-role-cutover.sh` | One-shot, idempotent move of the app off the RDS master onto the `feoh_app` role (IAM auth, `CREATEDB`, owner of every app database — § Database role, `docs/decisions.md` §310). Runs as the master; `--check` reports, `--verify-login` proves the app role signs in with a token. SQL in `sql/`. |
| `backup.sh` | Nightly pg dumps (globals + control plane + every `feoh_*` DB) streamed to S3. Cron installed by bootstrap. On RDS these are the long-retention, provider-independent copy beside RDS's automated backups + PITR (globals with `--no-role-passwords`); in container mode they are the whole DR story. Optional `BACKUP_PING_URL` heartbeat (healthchecks.io-style) so silent failures get noticed. Fails, without pinging, if the server's database list does not include the control plane `feohledger` — an empty list used to report success. |
| `restore.sh` | Streams a night's dumps back from S3 — each DB via `pg_restore --create` (skips existing DBs unless `--force`). Container mode replays globals first; on RDS it skips them and restores with `--no-owner --no-acl` so everything belongs to the master user. Stops the api for the duration, rolls the stack back up after. Test it once against a scratch stack. On RDS, a point-in-time restore is the first choice (`docs/minimal-deployment.md` § Point-in-time restore). A logical restore on RDS runs as the master (break-glass, § Database role) and refuses the IAM app role, which cannot recreate pgvector. |
| `decrypt-env.sh` | Decrypts `prod.sops.yaml` to `.env` and checks it — required keys, JWT key strength, the payee fingerprint key (its own, at least 32 chars), values compose's `env_file` would silently rewrite, and the database mode (RDS: a URL `lib.sh` can parse, `PGSSLMODE` `verify-full`, and either a password or `FEOH_DATABASE_IAM_AUTH` true — never both; container: `POSTGRES_PASSWORD`, no TLS mode it cannot serve) — before it replaces the current `.env`. `deploy.sh` runs it; run it on its own to check a new secrets file without deploying. |
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
  open to the VM's security group only. The app signs in as the `feoh_app`
  role with an IAM token: `FEOH_DATABASE_URL` is the endpoint with that user
  and **no password**, `FEOH_DATABASE_IAM_AUTH: "true"`, `PGSSLMODE:
  "verify-full"` (§ Database role). The master password stays in
  `infra-secrets` for break-glass and is not in the VM's env.
- DNS: three records → this VM: `feohledger.com`, `api.feohledger.com`, and a
  **wildcard** `*.feohledger.com` (the wildcard makes tenant onboarding
  DNS-free; it needs no wildcard certificate — Caddy issues per-host certs).
- Secrets: in the **private** `infra-secrets` repo, create
  `feohledger/prod.sops.yaml` from `prod.sops.yaml.example` —
  `aws sso login --profile feohledger`, then
  `AWS_PROFILE=feohledger sops feohledger/prod.sops.yaml` (paste, fill, save;
  sops writes it encrypted) — and commit it there. Copy it onto the VM as
  `deploy/prod.sops.yaml`; `./decrypt-env.sh` checks it without deploying.
  Session Manager has no file copy, so carry the (still encrypted) file through
  the backups bucket, which the instance role can already read: `aws s3 cp` it
  to `s3://<backups bucket>/transfer/prod.sops.yaml` from your laptop, `aws s3
  cp` it down on the VM, then delete every version of that key (the bucket is
  versioned, so a plain delete leaves a copy behind).
  Generate keys in your own terminal. Never commit either file here — this
  repo is public. Pattern: `~/github/project-mgmt/docs/secrets-management.md`.

## Deploys

**Publish a GitHub release.** That is the whole deploy: tag a commit on `main`,
publish the release, and approve the `Deploy to the VM` run
(`.github/workflows/vm-deploy.yml`) when the `production` environment asks for
a reviewer. The workflow sends SSM Run Command to this VM, which runs
`./deploy.sh --ref <the release's commit>` as `ec2-user`, waits for it (up to
45 minutes), prints the tail of its output, and fails the run if the deploy
failed. That output is redacted of tenant database names, because the run log
of a public repo is public. Each automated deploy's full log is a file under
`/var/log/feoh-deploy/` on the VM (kept 90 days). No SSH is involved: the deploy role can
run only the `feohledger-vm-deploy` command document, on only this instance,
and the document accepts nothing but a commit sha (`infra/deploy.tf`,
`docs/decisions.md` §275). Prereleases do not deploy. Redeploy or roll back by
running the workflow by hand from `main` or from an older release tag (one that
already contains `vm-deploy.yml`; for anything older, `./deploy.sh --ref <sha>`
by hand).
One-time setup (apply, the instance-id secret, the OIDC preflight, arming the
workflow): `../infra/README.md` § Release deploys.

`./deploy.sh` by hand, from a Session Manager shell, still works: it deploys
main's tip, and `--ref <sha>` deploys one commit. Either way it preflights its
own prerequisites and required env keys, runs migrations before the new API
serves traffic, and fails loudly (via the compose healthcheck) if the API
doesn't come up. If the build or migration step fails, the previously-running
containers keep serving. Only one deploy runs at a time — a second one, manual
or released, refuses to start while the first holds `.deploy.lock`.

The deploy ends with a **smoke check**, `./smoke.sh --vm` — its last step, and
the same script the ECS release workflow ends with (#575). From the VM, `curl`
fetches through this VM's own Caddy (`--resolve <host>:443:127.0.0.1` — no
public DNS or hairpin NAT involved, but the real hostname, certificate and site
block), retrying for up to a minute per check while a just-recreated Caddy
loads its certificates:

- `https://<API_DOMAIN>/api/health` — a 200 with a non-empty body. The API's
  own healthcheck stays green through a Caddy that serves nothing (#579).
- the first vanity host in `tenants.caddy` — a page over a certificate curl
  accepts, i.e. the one TLS path besides `api.` this VM issues for, and the
  `vanity` proxy to CloudFront. With no vanity host the script prints a NOTE
  saying so rather than passing silently.
- Not the apex or a tenant subdomain: they are CloudFront's, and the release
  workflow checks them after the publish (`smoke-frontend.sh`).
- `https://<API_DOMAIN>/api/public-config` — `signup_enabled` must match
  `FEOH_SIGNUP_ENABLED` in `.env` (unset means on, as in the app), and while
  signup is on `hcaptcha_sitekey` must be `FEOH_HCAPTCHA_SITEKEY`.

It reports every failed check, not just the first, and exits 1; the deploy then
fails. Run it by hand any time: `./smoke.sh --vm`. From elsewhere, through
public DNS: `./smoke.sh --app <host> --api <host> --tenant <host> --signup true|false [--sitekey <key>]`.

`deploy.sh` also compares the running `docker compose` with `COMPOSE_VERSION`
in `bootstrap-vm.sh` and warns when they differ: CI renders `compose.prod.yml`
with exactly that version, in both database modes, and fails on any warning
(`deploy-compose` job; #569 was a warning only the VM's compose printed).
Re-running `bootstrap-vm.sh` converges the plugin on the pin. To bump compose,
change that one line — CI reads it from there.

### The VM serves no SPA

Since `docs/decisions.md` §364 nothing on this VM builds or serves the
frontend: there is no Node build container, no `deploy/www/`, and no apex or
tenant block in the Caddyfile. The SPA is S3 + CloudFront (§ The SPA on S3 +
CloudFront), built in CI. Before that the VM built it and served published
releases from `deploy/www/` (§277), after v0.1.0's Caddy mounted the rebuilt
`frontend/build` and served every page as an empty 404 (#579).

**The first deploy of this layout needs nothing by hand.** `deploy.sh` drops
every apex and `<slug>.<APP_DOMAIN>` block an older `add-tenant.sh` wrote to
`tenants.caddy` (it lists them), moves any vanity block still on `import spa`
to `import vanity`, and `up -d --wait` recreates Caddy because its volumes
changed. It then prints a NOTE for each leftover it leaves to you, once the
deploy is verified:

- `rm -rf deploy/www` — the old releases (`sudo` if Docker created it as root);
- `docker volume rm feoh-prod_pnpm-store` — the build's pnpm cache (and
  `feoh-prod-pnpm-store`, the pre-§203 one, if it is still there).

The apex and tenant certificates in `caddy_data` stop being renewed, because
Caddy no longer serves those names; they expire on their own and need no
cleanup.

**A `--ref` older than §277 is refused** (v0.1.0 and everything before it):
that commit's compose file mounts `frontend/build` into Caddy, which serves
every page as an empty 404 after its first rebuild. A commit between §277 and
§364 still deploys, and that is the rollback past §364: its `deploy.sh`
builds and serves the SPA here again. `tenants.caddy` then needs its
`<slug>.<APP_DOMAIN> { import spa }` blocks back (the first §364 deploy's log
lists the ones it dropped), any vanity block back on `import spa`, and
`frontend_on_cloudfront = false` to point DNS at the VM
(`../infra/README.md` § Frontend on CloudFront, Rollback).

Caddy's two config files are single-file mounts, which pin an inode: a
Caddyfile changed by a git checkout, or a `tenants.caddy` saved by an editor
that writes a new file and renames it, is invisible to the running container,
and `caddy reload` would reload the old copy and report success. `deploy.sh`
reloads through `feoh_caddy_reload`, which compares what the container sees
with the files on disk and recreates Caddy (a second or two of downtime)
instead of reloading when they differ.

`--ref` refuses a commit that is not on `origin/main` (a release tagged on a
branch fails rather than ships unreviewed code) and a checkout with modified
tracked files (the deploy would no longer be the commit it names). It resets
the VM's local `main` to that commit — a commit made on the VM and never pushed
is dropped from the branch, so never commit on the box — and leaves the checkout
on `main`, so after a rollback the next plain
`./deploy.sh` fast-forwards as usual. Migrations are forward-only: rolling the
code back past one leaves the newer schema in place.

While you're in a deploy window: OS security patches auto-apply nightly
(dnf-automatic, installed by bootstrap), but **docker/containerd are excluded**
— their updates restart the daemon and would bounce the stack at a random
hour, and the exclude also hides them from dnf-automatic's own reporting.
Check them here, where a bounce is fine:
`sudo dnf upgrade --refresh 'docker*' 'containerd*'`

## The SPA on S3 + CloudFront

The SPA also ships from the release. Once `FRONTEND_PUBLISH_ENABLED` is armed
(`../infra/README.md` § Frontend on CloudFront), `vm-deploy.yml` builds it in an
ungated job while the reviewer decides. After the VM step succeeds (backend
first, so a new UI never calls an API that hasn't shipped), the same approved
job publishes it to the bucket with `publish-frontend.sh` and checks the edge
with `smoke-frontend.sh` (`docs/decisions.md` §278). Neither script runs on
this VM.

`publish-frontend.sh` uploads in an order that never breaks a page someone
already has open:

1. The hashed `_app/immutable/*` files go first, a year's cache, and existing
   keys are skipped.
2. The other static files go next.
3. `index.html`, then `_app/version.json`, go last, both `no-cache`.
4. It invalidates `/*` and waits for the invalidation to finish.
5. Only then does it prune hashed files that are both unused by this build and
   older than `FRONTEND_PRUNE_DAYS` (30).

Nothing is deleted on the way in, because an open page lazy-loads chunks from
the build it started on.

The apex and tenant hosts must answer from CloudFront: the smoke step fails if
the apex lacks CloudFront's `x-amz-cf-id` header, because since
`docs/decisions.md` §364 this VM serves no SPA to fall back on.

### Frontend rollback

- **A bad build, with infrastructure fine**: publish an older build. Run `Deploy
  to the VM` by hand from an older release tag that contains this publish step.
  That rolls the VM back to the same commit, so the frontend and backend stay
  matched. For the frontend alone, build the older tag locally and run the same
  script with your own credentials, from the repo root:
  `git worktree add ../feoh-rollback <tag> && cd ../feoh-rollback/frontend && pnpm install --frozen-lockfile && PUBLIC_API_URL=https://api.feohledger.com PUBLIC_SITE_URL=https://feohledger.com pnpm build && cd .. && AWS_PROFILE=feohledger FRONTEND_BUCKET=$(terraform -chdir=../feohledger/infra output -raw frontend_bucket) CLOUDFRONT_DISTRIBUTION_ID=$(terraform -chdir=../feohledger/infra output -raw frontend_distribution_id) ../feohledger/deploy/publish-frontend.sh frontend/build`
  It runs the *current* checkout's `publish-frontend.sh`, so this works for a
  tag that predates the script too. An older build's hashed files are
  re-uploaded if they were already pruned. Remove the worktree afterwards with
  `git worktree remove ../feoh-rollback`.
- **Tags from before this publish step**: restore the previous versions of
  `index.html` and `_app/version.json` in the versioned bucket (S3 console →
  object → Versions), then invalidate `/*`. Their hashed files are still there
  for 30 days, and as noncurrent versions for 90.
- **CloudFront itself misbehaving**: there is no one-flag fallback any more.
  Setting `frontend_on_cloudfront = false` alone is an outage, because this
  VM serves no SPA (`docs/decisions.md` §364). Re-deploy a commit from before
  §364 first (§ The VM serves no SPA), then flip the flag.

## Rehearsed on every PR: the day-one job

The first production deploy (#566) found nine defects in the most basic paths
because nothing had ever run these steps in sequence from zero. CI's
`day-one-e2e` job (`.github/workflows/ci.yml`, required through `CI gate`) now
does, on every PR touching `backend/`, `frontend/`, `deploy/` or
`scripts/day-one/` (#575):

1. an **empty** Postgres (no `seed.py`, no pytest harness, no `init-tenants.sql`);
2. the deploy's migration step — `backend/scripts/deploy_migrate.sh`, the very
   file `deploy.sh` runs in the api container, so the two cannot drift;
3. the production image booted on its own entrypoint with production-shaped
   settings (`FEOH_ENVIRONMENT=production`, MFA on, signup off, `smtp` email,
   the credential KMS key on LocalStack);
4. the first tenant, provisioned through `lib.sh::feoh_tenant_argv` — the
   command `add-tenant.sh` execs — with a generated temporary password;
5. the migration step **again**, failing on any change to any database's
   schema or Alembic version (a deploy must be idempotent);
6. the Playwright journey in `frontend/tests-e2e/day-one/`.

So a change to a deploy step changes what the job runs. Keep it that way:
`deploy.sh` and `add-tenant.sh` call the shared file / function rather than
spelling the commands, and `backend/tests/test_control_plane_bootstrap.py` +
`test_deploy_add_tenant.py` fail on a restated copy. It all lives in
`scripts/day-one/stack.sh`; `pnpm day-one:up && pnpm test:day-one` runs it on a
laptop, on its own ports beside the dev stack.

## Tenants

`./add-tenant.sh <slug> --name "Company" --admin-email admin@company.com` —
provisions the tenant and prints the login URL + temp password. The tenant's
`<slug>.<APP_DOMAIN>` is already served by CloudFront's wildcard; nothing on
this VM or in DNS changes. A customer vanity domain is the one host added here
by hand (`docs/founder-runbooks/custom-domain-provisioning.md` § 3a). Don't run
`scripts/seed.py` (demo data) in prod.

## Database role

The app does not sign in as the RDS master. It signs in as `feoh_app` (`LOGIN`,
`rds_iam`, no password), with a 15-minute IAM token minted per connection from
the instance profile (`backend/app/db_engine.py`). Every session acts as
`feoh_owner` (`NOLOGIN`, `CREATEDB`), which owns the control plane, every
tenant database, and everything in them. Neither role is in `rds_superuser` or
can create roles. The master joins `feoh_owner`, so it can still do everything
(that is the rollback), but never joins `feoh_app`, so RDS keeps accepting its
password. The deploy scripts' pg tools mint their own tokens on the VM the same
way. The reasoning and the rejected alternatives are in `docs/decisions.md`
§310.

**The cutover** (once per instance; idempotent). Steps that touch a secret are
the operator's.

1. `terraform apply` the `rds-db:connect` grant (`../infra/README.md` § Workload
   stack). Harmless before anything uses it.
2. Deploy a release carrying this code. With `FEOH_DATABASE_IAM_AUTH` unset
   nothing changes yet.
3. On the VM, from `deploy/`, while `.env` still holds the master URL:
   `./db-role-cutover.sh --check`, then `./db-role-cutover.sh`, then
   `./db-role-cutover.sh --check` again. Expect every database owned by
   `feoh_owner`, `0` master-owned objects in each, and `template1 has
   pgvector: t`. `feoh_app` should show `rds_iam t`, `master_can_set f` and
   `defaults role=feoh_owner`. `feoh_owner` should show `login f`,
   `createdb t` and `master_can_set t`. That `--check` itself signed in with the
   master's password, which proves the master is not IAM-only.
4. `./db-role-cutover.sh --verify-login` — signs in as `feoh_app` with a token
   minted here. If it fails, nothing has changed for the app yet.
5. In `infra-secrets/feohledger/prod.sops.yaml`: `FEOH_DATABASE_URL` becomes
   `postgresql+asyncpg://feoh_app@<db_address>:5432/feohledger` (no password)
   and `FEOH_DATABASE_IAM_AUTH: "true"` is added. Copy it onto the VM,
   `./decrypt-env.sh`, then redeploy (`./deploy.sh --ref <the live sha>` or the
   release workflow) — migrations run as `feoh_app` first.
6. Verify: the smoke check passes;
   `./psql.sh -d feohledger -Atc "SELECT session_user, current_user"` prints
   `feoh_app|feoh_owner`; `./backup.sh` completes; sign in to a tenant.
7. Rotate the master password (`../docs/secrets-rotation.md` § RDS master
   password): it sat in the VM's env until step 5.

**Rollback** at any point: put the master URL back in `prod.sops.yaml` with
`FEOH_DATABASE_IAM_AUTH` false (or removed) and redeploy. The master inherits
`feoh_owner`'s privileges, so it can still do everything, migrations and tenant
provisioning included. Run `./db-role-cutover.sh` again when you come back: it
hands over whatever the master created in the meantime. Before step 5, the app
has not changed at all, so there is nothing to roll back.

**Break-glass as the master** (a logical `restore.sh`, a pgvector upgrade, a
cutover re-run after a rollback): for one command, in that shell only. Read
the URL without echo, so it lands in neither the terminal nor the history —
`read -rs MASTER_URL`, paste, then
`FEOH_DATABASE_URL="$MASTER_URL" FEOH_DATABASE_IAM_AUTH=false ./restore.sh <date>`
(the shell wins over `.env` for both, in `lib.sh` and compose alike). Run that
way, `restore.sh` leaves the api **stopped**, because rolling it up from this
shell would hand it the master's URL. Re-run `./db-role-cutover.sh` the same way
next, then `unset MASTER_URL` (or close the shell), and bring the api up from a
clean shell with `./compose.sh up -d --wait`. Then rotate the master password,
since it was on the VM (`../docs/secrets-rotation.md`). The cutover's own SQL
resolves names from the catalog only. In any interactive master session in an
app database, run `SET search_path = pg_catalog, pg_temp;` first, because the
app owns `public` there.

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
