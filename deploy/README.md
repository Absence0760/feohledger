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
./deploy.sh                           # first deploy: build, migrate, roll, verify (later ones: publish a release, § Deploys)
./add-tenant.sh acme --name "Acme" --admin-email admin@acme.com
```

| File | Purpose |
|---|---|
| `bootstrap-vm.sh` | One-time, idempotent VM setup (Amazon Linux 2023): docker + compose plugin + sops + cronie (AL2023 ships no cron daemon) + AWS CLI, automatic security updates (dnf-automatic; docker/containerd excluded so the stack never bounces at a random hour), 2 GB swap, nightly backup cron, IMDSv2 hop-limit fix. Other distros get the manual list. |
| `compose.prod.yml` | Redis (AOF) + API + Caddy, plus Postgres (pgvector) under the `localdb` profile for container mode only. No DB host ports; S3 is real AWS. API healthcheck lets deploys verify themselves. Container logs capped (json-file, 10 MB × 5 per service) so they can't fill the 30 GB disk. `FEOH_DATABASE_URL` (set → RDS) and `FEOH_REDIS_URL` are override seams; `PGSSLMODE` is passed to the api (default `prefer`). The one-shot `pgtools` service (same Postgres image, RDS CA bundle mounted, `tools` profile) carries the pg client tools for RDS. Also the one-shot `frontend-build` service (Node image + pnpm-store cache) behind a `build` profile, so `up` never starts it — `deploy.sh` runs it. Every image is `repo:tag@sha256:…`, bumped by Dependabot (`backend/docs/docker.md` § Image pinning). |
| `Caddyfile` | TLS + static SPA (served from `/srv/current`, the live release under the gitignored `www/` — § How the SPA is served) + `api.feohledger.com` reverse proxy. Domains via env. |
| `tenants.caddy.example` | Template for the per-VM tenant host list (`tenants.caddy`, gitignored). `add-tenant.sh` maintains it — manual edits rarely needed. |
| `lib.sh` | Sourced by every script below, never run: decides the database mode from `deploy/.env` (`FEOH_DATABASE_URL` set → RDS, else the local container), builds the compose command (`--profile localdb` in container mode), and runs pg client tools in that mode (`feoh_pg`: `exec` into `postgres`, or `pgtools` against RDS with the password passed through the environment, never an argv). Also `feoh_sync_source`, `deploy.sh`'s checkout step: main's tip, or with `--ref` exactly one commit that must be on `origin/main`, always leaving the checkout on `main`. And the serving half: `feoh_publish_frontend` (a build becomes the live release by an atomic symlink swap), `feoh_caddy_reload` (reload, or recreate when Caddy's single-file mounts are stale) and `feoh_smoke_check` (the post-deploy fetch through the VM's own Caddy). |
| `compose.sh` / `psql.sh` | Ad-hoc `docker compose` / `psql` with the database mode applied — use these instead of a bare `docker compose -f compose.prod.yml …`, which in container mode would start the stack without its database. |
| `deploy.sh` | Preflight → lock (one deploy at a time, `flock` on the gitignored `.deploy.lock`) → pull main, or check out `--ref <sha>` → re-run the `deploy.sh` just checked out → decrypt secrets → frontend build (`docker compose run --rm frontend-build`; no Node/pnpm on the VM) → backend build → migrate (control plane + all tenants) **before** rolling → `up -d --wait` → publish the SPA as a release under `www/` (atomic symlink swap, § How the SPA is served) → Caddy reload (or recreate, if its mounted config is stale) → smoke check of the site and the API through the VM's own Caddy. Flags: `--no-pull`, `--ref <sha>` (what a release runs over SSM), `--backend-only`, `--frontend-only`. |
| `add-tenant.sh` | Tenant DB + org + admin user (same `provision_tenant` path as signup) + Caddy host block + reload, in one shot. Generates a temp password (first-login change forced) unless `--admin-password` given. |
| `remove-tenant.sh` | The inverse of `add-tenant.sh`, and the deletion `/legal/dpa` § 13 promises within 60 days of termination: removes the Caddy host block and reloads (stop serving first), runs `scripts/delete_tenant.py` in the api container (documents → tenant DB → control-plane rows), then deletes every version of that tenant's nightly dumps from the backup bucket. `--dry-run` prints the inventory and changes nothing; otherwise it makes you type the slug back. Only `delete_tenant.py`'s exit 4 (no such organisation) lets it carry on to the Caddy and backup legs of a tenant already deleted; any other inventory failure (api down, database unreachable, a partner organisation refused) stops it before it changes anything. Prints the written confirmation to send the customer, including the two residues it does NOT reach. |
| `backup.sh` | Nightly pg dumps (globals + control plane + every `feoh_*` DB) streamed to S3. Cron installed by bootstrap. On RDS these are the long-retention, provider-independent copy beside RDS's automated backups + PITR (globals with `--no-role-passwords`); in container mode they are the whole DR story. Optional `BACKUP_PING_URL` heartbeat (healthchecks.io-style) so silent failures get noticed. Fails, without pinging, if the server's database list does not include the control plane `feohledger` — an empty list used to report success. |
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

The deploy ends with a **smoke check**: from the VM, `curl` fetches
`https://<APP_DOMAIN>/` and `https://<API_DOMAIN>/api/health` through this VM's
own Caddy (`--resolve <host>:443:127.0.0.1` — no public DNS or hairpin NAT
involved, but the real hostname, certificate and site block) and fails the
deploy unless each answers 200 with a non-empty body, retrying for up to a
minute while a just-recreated Caddy loads its certificates. The API's own
healthcheck stays green through a Caddy that serves nothing, which is how
v0.1.0 shipped every page as an empty 404 (#579).

### How the SPA is served

Caddy does **not** mount `frontend/build`. The build deletes and recreates that
directory, a bind mount keeps the inode it started with, and the container is
left on an orphaned empty directory: every page 404s with an empty body and
`docker exec` into Caddy is refused (#579, `docs/decisions.md` §277). Instead
Caddy mounts the gitignored `deploy/www/`, which is never replaced, and serves
`/srv/current`:

```
deploy/www/
├── current  -> releases/<UTC timestamp>-<commit>   (live)
├── previous -> releases/<…>                        (the one before)
└── releases/<id>/                                  (exactly these two are kept)
```

`frontend/build` is only the build's staging copy. After the roll succeeds,
`deploy.sh` copies it to `releases/<id>` and points `current` at it with a single
rename (`feoh_publish_frontend` in `lib.sh`), so a request sees the old release
or the new one, never a gap — and a failed migration or an API that never comes
healthy leaves the old SPA in front of the old API. A `--backend-only` deploy
publishes nothing and needs a release to exist already.

**Rolling the frontend back** to the previous release is one rename, no
rebuild and no reload (Caddy resolves the symlink per request); from `deploy/`:
`ln -sfn "$(readlink www/previous)" www/current.new && mv -T www/current.new www/current`

**A `--ref` older than this layout is refused** (v0.1.0 and everything before
it): that commit's own `deploy.sh` and compose file would mount
`frontend/build` into Caddy again and bring back the empty-404 outage on its
first rebuild. Roll the SPA back with the swap above; rolling the whole stack
back to a pre-layout commit is not supported.

**Moving a VM to this layout needs nothing by hand.** The first deploy of a
commit with this change finds no `www/current`, publishes the new build there
*before* rolling, and `up -d --wait` recreates Caddy because its volumes
changed, so the new container starts with the site already in place. The old
`frontend/build` stays where it is as the staging copy.

Caddy's two config files are still single-file mounts, which pin an inode the
same way: a Caddyfile changed by a git checkout, or a `tenants.caddy` saved by
an editor that writes a new file and renames it, is invisible to the running
container, and `caddy reload` would reload the old copy and report success.
`deploy.sh`, `add-tenant.sh` and `remove-tenant.sh` all reload through
`feoh_caddy_reload`, which compares what the container sees with the files on
disk and recreates Caddy (a second or two of downtime) instead of reloading
when they differ.

`--ref` refuses a commit that is not on `origin/main` (a release tagged on a
branch fails rather than ships unreviewed code) and a checkout with modified
tracked files (the deploy would no longer be the commit it names). It resets
the VM's local `main` to that commit — a commit made on the VM and never pushed
is dropped from the branch, so never commit on the box — and leaves the checkout
on `main`, so after a rollback the next plain
`./deploy.sh` fast-forwards as usual. Migrations are forward-only: rolling the
code back past one leaves the newer schema in place.

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

Until the DNS cutover (`frontend_on_cloudfront`), the apex and tenant hosts are
still this VM's Caddy, serving the build `deploy.sh` makes here. The smoke check
notices which one answers and checks the apex only once it is CloudFront.

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
- **CloudFront itself misbehaving**: set `frontend_on_cloudfront = false` and
  apply (`../infra/README.md` § Frontend on CloudFront). The apex and tenants
  go back to this VM's Caddy, which keeps serving the SPA until the step-4
  cleanup removes it.

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
