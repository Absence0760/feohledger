# Backup + Disaster Recovery runbook

How we back up customer data, what we'd do if it was destroyed, and how often we test that we actually can.

This is a SOC 2 prerequisite (`docs/soc2-readiness.md` § Backup, recovery, and continuity). The auditor reads this doc, asks the security officer when the last restore test was, and expects to see a recent record.

**What this describes is the deployment in `docs/minimal-deployment.md`:** a single EC2 VM running Postgres, Redis, the API and Caddy under Docker Compose, with S3 for uploaded files and for the database dumps. There is **no RDS, no ElastiCache and no managed backup service** — `infra/` provisions four S3 buckets, one KMS key, ACM, Route 53 and a cost budget, and nothing else (`infra/README.md:3`: "Real AWS workload resources (ECS, ALB, RDS, CloudFront) are not yet defined here"). Every mechanism below is either a script in `deploy/` or an S3 lifecycle rule in `infra/s3.tf`. Production uses real AWS S3 for uploaded files, not MinIO — `deploy/compose.prod.yml:8` and the empty `FEOH_S3_ENDPOINT_URL` in `deploy/prod.sops.yaml.example:57` are what select it.

**This document is the one the published DPA points at.** Annex II's *Availability and restoration* row tells customers "a written backup and disaster-recovery procedure exists and is maintained alongside the code" — this is that procedure — and DPA § 13 rests a customer-facing promise on the mechanism described here: that we delete a tenant's backup objects within 60 days, because each tenant's dump is its own object rather than part of a whole-system image. DPA § 12 then commits us to make available "all information necessary to demonstrate compliance with Article 28", which is the route by which an auditor asks for this file. **So a claim here is a claim a customer can check against the repo, and a wrong one contradicts a published commitment.**

---

## Targets

| | RTO (Recovery Time Objective) | RPO (Recovery Point Objective) |
|---|---|---|
| Control-plane DB (`feohledger`) | **4 hours** | **24 hours** |
| Tenant DB (`feoh_<slug>`) | **4 hours** | **24 hours** |
| S3 (invoice files) | **24 hours** | **0 minutes** (versioned, durable) |
| Redis | n/a — transient | n/a |
| Application code | **15 minutes** (re-deploy from main) | n/a |

**RTO** = max acceptable time from "incident detected" to "service restored." **RPO** = max acceptable data loss measured backward from the incident. The numbers above are what we tell customers; document any gap honestly.

**The 24-hour RPO is the backup schedule, not an aspiration.** `deploy/backup.sh` runs once a night at 03:17 UTC (`/etc/cron.d/feoh-backup`, written by `deploy/bootstrap-vm.sh:92`), so the worst case is a failure at 03:16 that loses a full day of invoices, approvals and payments. There is no continuous archiving and no point-in-time recovery: those come with a managed database, which `docs/minimal-deployment.md` § What's left out records as the trigger to adopt RDS. **Do not publish a sub-24-hour RPO while this is the mechanism.** The 4-hour RTO is provisioning a replacement VM plus `deploy/restore.sh`; it has not been measured against a real incident (see § Test cadence).

Redis holds the JWT blocklist, MFA OTPs, SSO state, and signup rate-limit counters. All have short TTLs and are recoverable from re-login. **A Redis loss revives revoked tokens** until they expire (≤ 30 min) — accepted risk; documented. Redis runs with AOF persistence onto a Docker volume (`deploy/compose.prod.yml:52`), so it survives a container restart — but nothing ships that volume anywhere, so it does not survive the VM.

---

## Backup strategy

### PostgreSQL (nightly logical dumps to S3)

There is no managed database, so there are no automated snapshots and no WAL to replay. The whole mechanism is one script.

- **Nightly dump** — `deploy/backup.sh`, run from cron at 03:17 UTC. It takes `pg_dumpall --globals-only` (roles) plus a `pg_dump -Fc` of the control plane and **every `feoh_*` tenant database**, discovered at run time from `pg_database` so a newly provisioned tenant is included without touching the script. Each stream is piped straight to S3 — nothing is written to the VM's disk.
- **Key layout** — `s3://<backups-bucket>/pg/<YYYY-MM-DD>/`, containing `globals.sql.gz` and one `<database>.dump` per database. **One object per tenant** is the property that makes per-tenant deletion from backups an ordinary S3 delete rather than surgery on a monolithic image.
- **Retention — 90 days.** Enforced by the `expire-old-backups` lifecycle rule on the backups bucket (`infra/s3.tf:483-503`), from `var.backup_retention_days` (`infra/variables.tf:57-61`, default `90`). The script prunes nothing itself; the lifecycle rule *is* the retention policy, which is why that bucket deliberately carries **no** Object Lock (a lock would fight the expiry — `infra/s3.tf:419-431`). Noncurrent versions expire after 30 days and expired delete markers are reaped. **This 90 days is the figure the DPA publishes** (`/legal/dpa` § 13 and Annex II); change one and change the other.
- **Encryption** — the bucket's default SSE-KMS under the application key (`infra/s3.tf:448-458`) and TLS in transit, enforced by a bucket policy that denies plain HTTP (`infra/s3.tf:522-549`). The script does no client-side encryption of its own.
- **Before a risky operation** — major migrations, destructive scripts — run `deploy/backup.sh` by hand. It is idempotent within a day (same date prefix, and the bucket is versioned, so a second run supersedes rather than destroys).
- **Monitoring** — optional dead-man's-switch. Set `BACKUP_PING_URL` in the sops env and the script pings it after a successful run (`deploy/backup.sh:51-61`), so a backup that silently stops running is noticed before a restore needs it. **Unset, nothing reports failure at all** — the cron output goes to `/var/log/feoh-backup.log` on the VM and nowhere else.
- **Not verified** — the script does not checksum, test-restore, or `pg_restore --list` its own output. A dump that is written but unreadable would not be detected until a restore. The quarterly restore test in § Test cadence is the only thing standing in for this.
- **Cross-region copy** — not configured. The buckets are single-region (`us-east-1`).

### S3 (uploaded files, audit archive, backups, access logs)

`infra/s3.tf` provisions four buckets. Names are not fixed in code — they come from variables, and three of the four are exposed as Terraform outputs (`terraform output invoice_files_bucket`, `audit_logs_bucket`, `backups_bucket`). The access-logs bucket has no output; it is referenced only from inside the module, so read its name from `terraform.tfvars` or the console if you ever need it.

| Bucket | Holds | Versioning | Encryption | Object Lock | Lifecycle |
|---|---|---|---|---|---|
| invoice files | uploaded invoices, receipts, contracts, tax forms, Positive Pay files | Enabled | SSE-KMS | **Governance**, 365 days | noncurrent versions expire at 395 days; incomplete multipart uploads reaped at 7 days. Current versions never expire |
| audit logs | the shipped write-once audit archive | Enabled | SSE-KMS | **Compliance**, 2555 days (7 years) | noncurrent versions expire at 2585 days; multipart reaped at 7 days |
| backups | the nightly `pg_dump` stream | Enabled | SSE-KMS | none, deliberately | current versions expire at **90 days**; noncurrent at 30; expired delete markers removed |
| access logs | S3 server-access logs from the other three | Enabled | SSE-S3 (AES256 — log delivery cannot write to an SSE-KMS bucket) | none | objects expire at 365 days; noncurrent at 30 |

- **There is no Glacier and no storage-class transition anywhere.** Every lifecycle rule is expiration-only; `grep -rn 'transition\|glacier\|storage_class' infra/` returns nothing. Long-lived objects sit in S3 Standard under Object Lock, which is what actually makes them undeletable — not an archive tier.
- **MFA Delete** — pending. Adds friction to permanent deletion of versioned objects; it cannot be set through Terraform and needs a root-credential CLI call.
- **Replication** — pending. Cross-region replication once customer data warrants it. Until then a region loss is a region loss; see Scenario D.

### Secrets (SOPS + KMS)

- **Encrypted at rest in the private `infra-secrets` repo** (`feohledger/`), never in this public one. Every value is KMS-encrypted, so loss of that repo = no loss of confidentiality; GitHub plus each operator's clone are the copies.
- **KMS key backup** — AWS KMS keys are durable by definition (eleven nines). Loss requires AWS-side disaster.
- **Recovery** — clone `infra-secrets` + decrypt with `kms:Decrypt` on `alias/feohledger-sops`. See `backend/CLAUDE.md` § Secrets management.
- Note there are **two** keys and they have different owners: `alias/feohledger-sops` encrypts secrets and was created by the estate account bootstrap, outside this repo; `alias/feohledger-app-<env>` is created by `infra/kms.tf` and encrypts the buckets above. A `terraform destroy` in `infra/` never touches the first.

### Application code + infrastructure

- **Code** — GitHub. Restoring is `git clone`.
- **Infra** — the Terraform in `infra/` covers the security substrate only: the four buckets, the application KMS key, the ACM certificate, the Route 53 domain registration and the cost budget. Restoring that is `terraform apply`. **State** lives in S3 (`envs/prod/terraform.tfstate`) with **S3-native conditional-write locking** (`use_lockfile = true`, `infra/main.tf:45`) — there is **no DynamoDB lock table** (`infra/main.tf:26-27`). The state bucket itself is `feohledger-tfstate-<account-id>`, created by the estate account bootstrap rather than by this module, and already versioned and encrypted; it is not one of the four buckets above.
- **The VM is not Terraform-managed.** Terraform for the instance and its profile is explicitly "still optional / not built" (`docs/minimal-deployment.md:305-307`), and the host itself — `t4g.small`, Amazon Linux 2023 arm64, 30 GB gp3, security group open on 80/443 — is a console-clicked build described in prose (`docs/minimal-deployment.md:118`). Rebuilding it is a manual provision plus `deploy/bootstrap-vm.sh` and `deploy/deploy.sh`, not a `terraform apply`. There is also **no EBS snapshot schedule** — no Data Lifecycle Manager policy exists in `infra/` or in any script, and the "weekly EBS snapshot" bullet at `docs/minimal-deployment.md:265` is a recommendation with nothing behind it. So the nightly S3 dumps are the only copy of the databases that survives the instance. (DPA § 13's carve-out for "a deployment that also keeps a volume-level snapshot as a coarse fallback" is written conditionally for exactly this reason: today there is no such snapshot, so that sentence describes nothing.)

### What no backup covers at all

Everything in this section lives only on the VM. None of it is in S3, none of it is in git, and losing the instance loses it — so Scenario B has to rebuild each one by hand.

| Per-VM state | What it is | How it comes back |
|---|---|---|
| `deploy/.env` | the decrypted secrets file, recreated on every `deploy.sh` run (`deploy/.gitignore:1-4`) | `deploy/decrypt-env.sh` against the canonical `infra-secrets/feohledger/prod.sops.yaml` — this one is genuinely recoverable |
| `deploy/tenants.caddy` | one Caddy site block per tenant subdomain; gitignored, seeded empty from `tenants.caddy.example` by `deploy/deploy.sh:55` | rebuilt by hand from the restored control plane's organization slugs — **not** by re-running `add-tenant.sh`, which provisions a new tenant DB and admin user |
| `caddy_data` / `caddy_config` volumes | Let's Encrypt certificates and ACME account state (`deploy/compose.prod.yml:122-125`) | Caddy re-issues per host over HTTP-01 on first request. Watch Let's Encrypt's rate limits on a rebuild with many tenant hosts — the compose file flags this, and it is the one part of Scenario B that can be throttled rather than merely slow |
| `redisdata` volume | JWT blocklist, MFA OTPs, SSO state, rate-limit counters | not restored, by design — see § Targets |

`pgdata` is the fourth of the four named volumes (`deploy/compose.prod.yml:129-133`) and is the one this runbook does cover: the nightly dumps are its off-instance copy.

---

## Restore procedures

Both database scenarios run through the same script: **`deploy/restore.sh <YYYY-MM-DD> [--force] [db …]`**, on the VM, from `deploy/`. It streams each object from S3 (nothing lands on local disk), restores the role globals first, then each database via `pg_restore --create`. It stops the `api` container for the duration — open connections block `DROP`/`CREATE DATABASE` — and brings the stack back up at the end. **A database that already exists is skipped unless you pass `--force`**, which drops and recreates it.

### Scenario A — accidental table drop or bad migration

1. Identify the day the data was last good (audit log, error log, customer report). Granularity is a **whole night's dump**, not a timestamp — there is no PITR.
2. **Don't roll back the migration.** Restore the affected databases from the last good dump. Restore only what you need to, by naming the databases:
   ```bash
   cd deploy && ./restore.sh 2026-04-19 --force feohledger feoh_acme
   ```
   Naming them matters: a bare `--force` restores **every** database under that date prefix, including tenants that were never affected, and rolls each one back a day.
3. Validate the restore (row counts, sample records) before letting traffic back in.
4. Everything written since that dump is gone. Reconcile from the audit trail and tell the affected tenants — this is the point at which the 24-hour RPO stops being an abstraction.

### Scenario B — the VM is lost entirely

1. Provision a replacement instance and run `deploy/bootstrap-vm.sh` (Docker, Compose, the AWS CLI, sops, the backup cron, swap, IMDSv2 hop limit).
2. Restore the sops env and bring the stack up: `deploy/decrypt-env.sh`, then `deploy/deploy.sh`.
3. Restore the databases from the most recent date prefix:
   ```bash
   aws s3 ls s3://<backups-bucket>/pg/          # find the latest date
   cd deploy && ./restore.sh <YYYY-MM-DD>
   ```
4. Bring every restored database up to the current schema head. The migration commands run **inside the api container**, not on the host — there is no venv on the VM. This is the same pair `deploy/deploy.sh:88-89` runs:
   ```bash
   docker compose -f compose.prod.yml run --rm api sh -c "alembic upgrade head && python scripts/migrate_all_tenants.py"
   ```
   Step 2 already ran these once, but it ran them against an empty cluster — the restore in step 3 is what put the tenant databases there, so they need it again.
5. Rebuild `deploy/tenants.caddy`. A fresh VM has the empty seeded copy, so **every tenant subdomain is unserved until its block is back**. List the slugs from the restored control plane and append one block each, then reload Caddy without a restart:
   ```bash
   docker compose -f compose.prod.yml exec -T postgres psql -U postgres -d feohledger -Atc "SELECT slug FROM organizations ORDER BY slug"
   docker compose -f compose.prod.yml exec caddy caddy reload --config /etc/caddy/Caddyfile
   ```
   Each block is the three lines in `deploy/tenants.caddy.example` — `<slug>.<APP_DOMAIN> { import spa }`. **Do not use `deploy/add-tenant.sh` for this**: it provisions a *new* tenant — database, organization and admin user — and appending a Caddy block is only its last step.
6. Re-point DNS at the new instance. With the recommended wildcard record (`*.<APP_DOMAIN>`) there is no per-tenant DNS step; Caddy still issues a per-host certificate over HTTP-01 on first request, so expect the first hit on each tenant host to be slow and watch for Let's Encrypt rate limiting if there are many.

### Scenario C — accidental S3 object deletion

1. Object versioning is enabled on all four buckets, so the delete just added a delete marker.
2. Recover with the AWS CLI — the bucket name comes from `terraform output invoice_files_bucket`:
   ```bash
   aws s3api list-object-versions --bucket <invoice-files-bucket> --prefix <org-id>/<invoice-id>/
   aws s3api delete-object --bucket <invoice-files-bucket> --key <key> --version-id <delete-marker-version-id>
   ```
3. The previous version is now the current version again.
4. On the **audit-logs** bucket this is rarely necessary and often impossible in the other direction: Compliance-mode Object Lock means a current version cannot be deleted before its 7 years are up, by anyone, including the root account.

### Scenario D — entire AWS region down

Everything is in one region: the VM, all four buckets and the KMS key. Until cross-region replication is set up this is degraded service, not failover. Document the timeline, communicate via status page (pending), and resume when the region recovers. With cross-region in place: failover to a new VM in the secondary region restoring from the replicated dumps and S3 replica.

---

## Test cadence

The auditor cares more about whether we've **tested** the restore than whether the procedure looks pretty.

| Test | Cadence | Owner | Evidence |
|---|---|---|---|
| `deploy/restore.sh` against a scratch stack | **Quarterly** | Security officer | The script's output plus a smoke-test query and a successful login, attached in compliance vendor's evidence locker |
| S3 object recovery from version history | **Quarterly** | Security officer | Same |
| Full rebuild: fresh VM → `bootstrap-vm.sh` → `deploy.sh` → `restore.sh`, plus `terraform apply` of `infra/` against a staging account | **Annually** | Security officer | Shell transcript + Terraform plan/apply log |
| Tabletop exercise (walk through Scenarios A–D without doing them) | **Quarterly** | Security officer + on-call | Meeting notes |

A test that's never run is not a backup. **Nothing above has been run yet** — the restore path is scripted but unexercised (`docs/minimal-deployment.md:269-270`), and the first quarterly restore is the outstanding item before this runbook can be shown to an auditor as operating rather than designed.

---

## Monitoring + alerting

What exists today:

- **Backup heartbeat** — optional. `BACKUP_PING_URL` in the sops env; `deploy/backup.sh` pings it after each successful run, so silence is the signal. This is the only alert on backup failure anywhere.
- **Cost** — an AWS Budget (`infra/budgets.tf`) emails the configured addresses at 50% actual, 100% actual and 100% forecast of the monthly limit. Not a backup control, but it is the only other thing in the account that emails anyone.

Still pending:

- A real alarm on backup failure rather than an optional heartbeat. There are **no CloudWatch alarms and no SNS topics** in `infra/` at all.
- Alarm on S3 replication lag (once replication is live).
- Daily report from the compliance vendor confirming backups are happening.

---

## What's not in scope here

- **Customer-initiated data export** — separate feature, lives under invoice export endpoints (`/api/invoices/bulk/export`).
- **GDPR right-to-erasure** — the erasure process itself is separate and tracked in the privacy roadmap. Its interaction with backups belongs here, though, because DPA § 13 makes a customer-facing promise that rests entirely on this mechanism, and it needs three qualifications an auditor will ask for:
  - **A tenant's business data is one object, and deleting it is an ordinary S3 delete.** `pg/<date>/feoh_<slug>.dump` holds that tenant's invoices, vendors, payments and audit trail and nothing else, so removing a tenant from the backup set is not surgery on an encrypted whole-system image. This is the fact DPA § 13 is written on, and it is true.
  - **The control-plane dump is the exception, and the DPA does not carve it out.** `pg/<date>/feohledger.dump` is shared across every tenant: it carries the organization record plus each employee user's name, email and password hash. Those rows are *not* selectively removable from a dump — the whole-system-image problem, scoped to the control plane. They age out with the rest of the window at 90 days under `expire-old-backups`, which is inside the 90-day residue the DPA already discloses, but a reader of § 13 would not expect a second category. **Correct this in the DPA rather than here if the wording is ever revisited.**
  - **Nothing implements the deletion. It is a manual operator step.** No script or backend path touches the backups bucket other than `deploy/backup.sh` and `deploy/restore.sh` — there is no erasure hook, no sweep and no scheduled job, so the 60-day promise is kept by an operator running a delete by hand across every date prefix still in the window:
    ```bash
    for d in $(aws s3 ls s3://<backups-bucket>/pg/ | awk '{print $2}' | tr -d /); do aws s3 rm "s3://<backups-bucket>/pg/$d/feoh_<slug>.dump"; done
    ```
    The bucket is versioned, so each of those is a delete marker rather than an erase; the superseded version then expires 30 days later under `noncurrent_version_expiration`. That is exactly the 30-day residue DPA § 13 discloses, so the two are consistent — but only as long as someone actually runs the loop. **Until a scripted path exists, treat this as a calendar obligation, not a system behaviour.**
- **Long-term archival** — the audit archive is held in S3 Standard under **Compliance-mode Object Lock for 7 years** (`infra/s3.tf:192-201`), and uploaded invoice files under Governance-mode lock for 1 year. Not Glacier — see the note in § S3. Records-retention policy lives separately (`backend/docs/retention.md`).

---

## Change log

Update this section whenever the backup strategy changes; the auditor will compare against the as-built infra.

| Date | Change | Author |
|---|---|---|
| 2026-04-19 | Initial runbook | Founder |
| 2026-09-15 | Corrected to the as-built infrastructure. The runbook described an RDS instance with 7-day automated snapshots and PITR, an S3 lifecycle transitioning to Glacier at 90 days, and a DynamoDB state lock — none of which exist. Replaced with the real mechanism: nightly `pg_dumpall`/`pg_dump` from `deploy/backup.sh` to S3 with a 90-day lifecycle expiry, restore via `deploy/restore.sh`, S3-native state locking. RPO corrected 15 min → 24 h. Added the backups and access-logs buckets, the absence of any EBS snapshot, and the unmanaged VM | Founder |
| 2026-09-16 | Second correction pass, against the claims the DPA now rests on. Added § What no backup covers at all — `tenants.caddy`, the Caddy certificate volumes and `redisdata` are per-VM state no mechanism copies anywhere. Fixed Scenario B: migrations run inside the api container, not on the host, and the tenant Caddy blocks are rebuilt by hand rather than by `add-tenant.sh`, which provisions a new tenant. Recorded three qualifications on erasure-from-backups: the control-plane dump is shared across tenants and is not selectively editable, no code implements the DPA's 60-day backup deletion (it is a manual operator step), and the 30-day delete-marker residue is what the DPA already discloses. Corrected the DPA citation — Annex II names this runbook, § 13 depends on it, § 12 is the access route | Founder |
