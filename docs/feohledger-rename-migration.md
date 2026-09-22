# FeohLedger rename — upgrade runbook

One-time migration for environments provisioned **before** the rename. A fresh
clone needs none of this — the defaults are already correct, and `pnpm dev:all`
comes up clean.

The rename changed five things that live outside the code: environment variable
names, database names, the SOPS KMS alias, the local IdP realm, and the
deployment resource names. Sections 1–5 need action; 6–7 are informational.
Work top to bottom.

> **Not renamed, on purpose.** The RBAC roles `ap_manager` / `ap_clerk`, the
> chat author type `ap_team`, and the audit actor `ap_user` keep their names:
> "accounts payable manager" is a job title, not the product. Renaming them
> would mean a data migration on every tenant's `roles` table plus a JWT claim
> change, for no benefit.

---

## 1. Environment variables: `AP_*` → `FEOH_*`

Every backend setting is read through `pydantic-settings` with an `env_prefix`,
which is now `FEOH_`. **An unrenamed variable is not an error — it is silently
ignored and the default is used instead.** That is the dangerous failure mode
here: `AP_SECRET_KEY` left as-is doesn't crash, it quietly falls back to the
`change-me-in-production` default. Rename all of them before restarting.

```bash
# Preview the rename against a decrypted env file.
sed -E 's/\bAP_([A-Z0-9_]+)=/FEOH_\1=/' backend/.env | diff backend/.env - | head
```

For SOPS-managed environments, edit the encrypted env in place (it lives in the
private `infra-secrets` repo, never in this one) and re-encrypt:

```bash
sops <encrypted env file>  # rename every AP_* key to FEOH_* in $EDITOR, save
```

Do the same for any CI secrets, container env, and `deploy/.env` on the VM.

**Verify** nothing was missed — this should print nothing:

```bash
grep -rE '\bAP_[A-Z0-9_]+' backend/.env deploy/.env 2>/dev/null
```

## 2. Databases: `account_payables` → `feohledger`, `ap_<slug>` → `feoh_<slug>`

`organizations.db_name` stores each tenant's physical database name and
`get_tenant_engine` resolves the tenant connection from it, so the databases
and that column have to move together. Renaming the databases alone leaves
every tenant request pointing at a name that no longer exists.

`backend/scripts/rename_databases_to_feohledger.py` does both in one pass. It
is **dry-run by default** and idempotent, so a re-run after a partial failure
finishes the job rather than erroring.

```bash
cd backend && source .venv/bin/activate

# 1. Inspect the plan — changes nothing.
python scripts/rename_databases_to_feohledger.py

# 2. Stop the app (ALTER DATABASE ... RENAME fails while sessions are open),
#    then apply. --force terminates any leftover backends.
python scripts/rename_databases_to_feohledger.py --apply --force
```

Then point `FEOH_DATABASE_URL` at the renamed control-plane database and start
the app back up.

**Take a backup first** (`deploy/backup.sh`, or `pg_dumpall`). The rename is
reversible with the same script — `--old-control-db feohledger --new-control-db
account_payables --old-tenant-prefix feoh_ --new-tenant-prefix ap_` — but a
backup is the safer undo.

## 3. SOPS KMS alias

The alias moves from `alias/account-payables-sops` to `alias/feohledger-sops`.
The key material is unchanged, so nothing needs re-encrypting. `update-alias`
is the wrong verb here — it repoints an alias that already *exists*, and
`alias/feohledger-sops` doesn't yet, so it fails with `NotFoundException`.
Create the new alias on the same key, then refresh the ARN each encrypted
file has recorded:

```bash
aws kms create-alias --alias-name alias/feohledger-sops \
  --target-key-id "$(aws kms describe-key --key-id alias/account-payables-sops \
                       --query KeyMetadata.KeyId --output text)"
sops updatekeys <each encrypted file>        # rewrite the embedded alias ARN
aws kms delete-alias --alias-name alias/account-payables-sops   # optional, LAST
```

Order matters: each `.sops` file records the alias ARN it was encrypted
under and decryption resolves that recorded ARN, so deleting the old alias
before `updatekeys` has rewritten it leaves the files undecryptable until
the alias is restored.

If no encrypted payload exists yet, skip this: the FeohLedger AWS account was
bootstrapped with `alias/feohledger-sops` directly (the estate
`new-project-account.sh`), so there is no old alias to move.

## 4. Local Keycloak / Authentik

The Keycloak realm and client are renamed (`account-payables[-app]` →
`feohledger[-app]`), and the Authentik SCIM blueprint file is now
`feohledger-scim.yaml`. Realm import happens at container start, so recreate
the IdP containers:

```bash
pnpm idp:down && pnpm idp:up && pnpm idp:seed   # add saml:seed / scim:seed as needed
```

## 5. Deployment resource names (one-VM / compose deployments)

Infrastructure resource names lost their `ap-` prefix. These are not read from
the database, so they only matter where the old name is already materialised.

| Resource | Old | New |
|---|---|---|
| Compose project | `ap-prod` | `feoh-prod` |
| pnpm store volume | `ap-prod-pnpm-store` | `feoh-prod-pnpm-store`, since replaced by the compose-managed `feoh-prod_pnpm-store` (a cache — `deploy/README.md` § Deploys) |
| Backup cron / log | `/etc/cron.d/ap-backup`, `/var/log/ap-backup.log` | `feoh-backup` |
| SQS queues (lambda dispatch modes) | `ap-extraction`, `ap-erp`, `ap-audit` | `feoh-*` |
| Audit WORM bucket | `ap-audit-worm` | `feoh-audit-worm` |

**The compose project rename is the one with teeth.** Docker Compose scopes
containers, networks and volumes by project name, so after the rename the stack
no longer *sees* the running `ap-prod` resources — `docker compose up` starts a
second, empty set rather than upgrading the old one. Stop the old project by its
old name first, and move the Postgres volume across (or restore from backup):

```bash
cd deploy
docker compose -p ap-prod -f compose.prod.yml down     # stop under the OLD name
sudo rm -f /etc/cron.d/ap-backup                        # bootstrap-vm.sh reinstalls it
./bootstrap-vm.sh                                       # or just re-run deploy.sh
```

If `FEOH_EXTRACTION_MODE` / `FEOH_ERP_MODE` / `FEOH_AUDIT_MODE` are still
`local` (the default), the SQS queues are unused and nothing needs recreating —
just update `FEOH_SQS_*_QUEUE_URL` if you later switch to `lambda`.

> **Fixed in this change:** `deploy/backup.sh` selected tenant databases with
> `LIKE 'ap\_%'`. Left as-is it would have matched nothing after the rename and
> backed up only the control plane — silently, with a success message. It now
> matches `feoh\_%`. If you run a pre-rename backup script against renamed
> databases, check its output count before trusting it.

## 6. Client-side state (informational — no action)

These reset themselves once, per device:

| What | Effect |
|------|--------|
| `ap_locale` → `feoh_locale` (browser + mobile) | Saved display-language choice resets to "follow system" |
| `ap_consent_choice` → `feoh_consent_choice` | Cookie-consent banner shows once more |
| `ap_cache.db` → `feohledger_cache.db` (mobile) | Offline cache starts empty and refills on next sync |
| Mobile application id → `com.feohledger.mobile` | An installed build is replaced, not upgraded |

## 7. API keys and webhooks (informational — no action)

Newly minted API keys carry the `feoh_live_` brand instead of `ap_live_`.
**Existing keys keep working**: lookup is by the stored prefix plus a SHA-256
digest, never by the brand literal. Webhook signing secrets are untouched —
their `whsec` prefix is the Stripe convention, not our product name.

---

## Verification

```bash
pnpm dev:all                    # stack comes up against the renamed databases
cd backend && pytest -q         # full suite
```

Then sign in and confirm the sidebar reads **FeohLedger**, and that an existing
tenant's invoices still load (proves `organizations.db_name` matches the
renamed databases).
