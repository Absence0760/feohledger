# Backup + Disaster Recovery runbook

How we back up customer data, what we'd do if it was destroyed, and how often we test that we actually can.

This is a SOC 2 prerequisite (`docs/soc2-readiness.md` § Backup, recovery, and continuity). The auditor reads this doc, asks the security officer when the last restore test was, and expects to see a recent record.

---

## Targets

| | RTO (Recovery Time Objective) | RPO (Recovery Point Objective) |
|---|---|---|
| Control-plane DB (`feohledger`) | **4 hours** | **15 minutes** |
| Tenant DB (`feoh_<slug>`) | **4 hours** | **15 minutes** |
| S3 (invoice files) | **24 hours** | **0 minutes** (versioned, durable) |
| Redis | n/a — transient | n/a |
| Application code | **15 minutes** (re-deploy from main) | n/a |

**RTO** = max acceptable time from "incident detected" to "service restored." **RPO** = max acceptable data loss measured backward from the incident. The numbers above are what we tell customers; document any gap honestly.

Redis holds the JWT blocklist, MFA OTPs, SSO state, and signup rate-limit counters. All have short TTLs and are recoverable from re-login. **A Redis loss revives revoked tokens** until they expire (≤ 30 min) — accepted risk; documented.

---

## Backup strategy

### PostgreSQL (RDS)

- **Automated snapshots** — daily, 7-day retention. Configured in Terraform (`infra/main.tf` `aws_db_instance.backup_retention_period = 7`).
- **Point-in-time recovery (PITR)** — RDS keeps WAL for the retention period, so we can restore to any second within the last 7 days. Drives the 15-minute RPO.
- **Manual snapshots before risky operations** — major migrations, destructive scripts, etc. Created via `aws rds create-db-snapshot --db-snapshot-identifier feoh-prod-pre-<change>-<date>`.
- **Cross-region copy** — pending. Once we have customers in multiple regions, copy snapshots to a secondary region nightly.

### S3 (invoice files)

- **Object versioning** — enabled on the `invoices` bucket. Deletes create delete markers; previous versions are recoverable.
- **MFA Delete** — pending. Adds friction to permanent deletion of versioned objects.
- **Lifecycle policy** — current versions live forever; non-current versions transition to Glacier after 90 days, expire after 7 years (matches financial-records retention requirements).
- **Replication** — pending. Cross-region replication once customer data warrants it.

### Secrets (SOPS + KMS)

- **Encrypted at rest in the private `infra-secrets` repo** (`feohledger/`), never in this public one. Every value is KMS-encrypted, so loss of that repo = no loss of confidentiality; GitHub plus each operator's clone are the copies.
- **KMS key backup** — AWS KMS keys are durable by definition (eleven nines). Loss requires AWS-side disaster.
- **Recovery** — clone `infra-secrets` + decrypt with `kms:Decrypt` on `alias/feohledger-sops`. See `backend/CLAUDE.md` § Secrets management.

### Application code + infrastructure

- **Code** — GitHub. Restoring is `git clone`.
- **Infra** — Terraform in `infra/`. Restoring is `terraform apply`. **State** is in S3 (gated by DynamoDB lock); back up the state bucket the same way as `invoices`.

---

## Restore procedures

### Scenario A — accidental table drop or bad migration

1. Identify the timestamp the data was last good (audit log, error log, customer report).
2. **Don't roll back the migration.** Instead, restore the database to that point in time:
   ```bash
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier feoh-prod \
     --target-db-instance-identifier feoh-prod-restore-$(date +%s) \
     --restore-time '2026-04-19T14:30:00Z'
   ```
3. Validate the restore (row counts, sample records).
4. **Switch the app over** — update `FEOH_DATABASE_URL` to the restored instance's endpoint and redeploy. Or rename DNS / RDS endpoints if available.
5. Decommission the old instance after a 24-hour soak.

### Scenario B — RDS instance lost entirely

1. List available automated snapshots: `aws rds describe-db-snapshots --db-instance-identifier feoh-prod`.
2. Restore the latest: `aws rds restore-db-instance-from-db-snapshot --db-instance-identifier feoh-prod --db-snapshot-identifier <snap-id>`.
3. Update `FEOH_DATABASE_URL` and redeploy.
4. Run `python scripts/migrate_all_tenants.py` against the restored instance to ensure schema is current.

### Scenario C — accidental S3 object deletion

1. Object versioning is enabled, so the delete just added a delete marker.
2. Recover with the AWS CLI:
   ```bash
   aws s3api list-object-versions --bucket invoices --prefix <org-id>/<invoice-id>/
   aws s3api delete-object --bucket invoices --key <key> --version-id <delete-marker-version-id>
   ```
3. The previous version is now the current version again.

### Scenario D — entire AWS region down

Until cross-region replication is set up: degraded service. Document the timeline, communicate via status page (pending), and resume when the region recovers. With cross-region in place: failover to secondary region's restored snapshot + S3 replica.

---

## Test cadence

The auditor cares more about whether we've **tested** the restore than whether the procedure looks pretty.

| Test | Cadence | Owner | Evidence |
|---|---|---|---|
| RDS snapshot → fresh instance | **Quarterly** | Security officer | Screenshot of restored instance + smoke-test query result, attached in compliance vendor's evidence locker |
| S3 object recovery from version history | **Quarterly** | Security officer | Same |
| Full app rebuild from `main` + Terraform | **Annually** | Security officer | Terraform plan/apply log against a staging account |
| Tabletop exercise (walk through Scenarios A–C without doing them) | **Quarterly** | Security officer + on-call | Meeting notes |

A test that's never run is not a backup.

---

## Monitoring + alerting (pending)

- CloudWatch Alarm on RDS automated-backup failures
- CloudWatch Alarm on S3 replication lag (once replication is live)
- Daily report from the compliance vendor confirming snapshots are happening

---

## What's not in scope here

- **Customer-initiated data export** — separate feature, lives under invoice export endpoints (`/api/invoices/bulk/export`).
- **GDPR right-to-erasure** — separate process; needs a hard-delete path that bypasses S3 versioning. Tracked in privacy roadmap.
- **Long-term archival** — handled by the S3 lifecycle policy (Glacier after 90 days, expire after 7 years). Records-retention policy lives separately.

---

## Change log

Update this section whenever the backup strategy changes; the auditor will compare against the as-built infra.

| Date | Change | Author |
|---|---|---|
| 2026-04-19 | Initial runbook | Founder |
