# Secrets rotation

What we rotate, how often, and the procedure for each. The auditor expects every secret with material blast radius to have a documented owner, cadence, and rotation playbook.

This is a SOC 2 prerequisite (`docs/soc2-readiness.md` § Secrets management).

---

## Inventory + cadence

| Secret | Where it lives | Cadence | Blast radius if leaked |
|---|---|---|---|
| `FEOH_SECRET_KEY` (JWT signing) | sops — `infra-secrets` (`feohledger/`) | **90 days** | Forge any user's JWT; full impersonation |
| AWS KMS key for SOPS | AWS KMS | **365 days (auto)** | Decrypt every secret under `infra-secrets/feohledger/` |
| RDS master password | sops — `infra-secrets` (`feohledger/terraform.sops.yaml`); **not** on the VM | **90 days**, and once right after the IAM cutover | Full DB read/write — but only from inside the VPC (the instance is private), and break-glass only: the app signs in as `feoh_app` with IAM tokens (`docs/decisions.md` §305) |
| `feoh_app` IAM auth tokens | minted per connection, never stored | **15 minutes (automatic)** | One new connection as the app role while it lasts; nothing to rotate — revoke `rds-db:connect` on the VM role to stop all of them |
| `FEOH_ANTHROPIC_API_KEY` (platform Claude Vision) | sops — `infra-secrets` (`feohledger/`) | **180 days** | Anthropic billing fraud, prompt extraction abuse |
| `FEOH_LITHIC_API_KEY` (virtual cards — platform) | sops — `infra-secrets` (`feohledger/`) | **180 days** | Issue cards on our account |
| `FEOH_NIUM_CLIENT_*` (virtual cards — platform) | sops — `infra-secrets` (`feohledger/`) | **180 days** | Issue cards on our account |
| `FEOH_HCAPTCHA_SECRET` (signup) | sops — `infra-secrets` (`feohledger/`) | **365 days** (or on suspected leak) | Bypass signup captcha |
| `POSTGRES_PASSWORD` (single-VM Postgres superuser) | sops — `infra-secrets` (`feohledger/prod.sops.yaml`) | **365 days** (or on suspected leak) | Full read/write of the control plane and every tenant DB — reachable only on the compose network, which publishes no host port |
| `FEOH_APPROVAL_SIGNING_KEY` (invoice approval signatures) | sops — `infra-secrets` (`feohledger/prod.sops.yaml`) | **On suspected leak only** — rotating breaks verification of every signature made under the old key | Forge an approval signature that verifies |
| `FEOH_EMAIL_ACTION_SIGNING_KEY` (approve-by-email links, Slack / Teams buttons) | sops — `infra-secrets` (`feohledger/prod.sops.yaml`) | **180 days** | Mint an approve/reject link for any reviewer (the action still runs that reviewer's segregation, threshold and CFO checks) |
| `FEOH_PARTNER_LINK_SIGNING_KEY` (partner / reseller link codes) | sops — `infra-secrets` (`feohledger/prod.sops.yaml`) | **180 days** | Forge a link code that attaches a tenant to a partner without its admin's consent |
| `FEOH_EMAIL_INTAKE_SIGNING_SECRET` (inbound email-to-invoice webhook) | sops — `infra-secrets` (`feohledger/prod.sops.yaml`) | **365 days**, changed at the email provider in the same step | Post forged inbound mail, creating invoices in any tenant whose intake address is known |
| AWS SES credentials (transactional email) | IAM role (preferred) or sops — `infra-secrets` | **365 days** if static | Send email from our domain |
| GitHub Actions OIDC role | AWS IAM role (no static keys) | n/a — short-lived | n/a |
| Per-tenant SCIM bearer tokens | `Organization.settings.sso.scim_bearer_hash` (sha256) | **On request** by tenant admin via `POST /api/organization/sso/scim-token` | Read/write users on that one tenant |
| Per-tenant OIDC client secret | `Organization.settings.sso.client_secret` (plain JSONB under RDS storage encryption; write-only, never returned) | **On request** by tenant admin via `PUT /api/organization/sso` | Mint OIDC tokens for that one tenant |
| Per-tenant ERP / payment-rail / card-issuer credentials (BYOK keys, client secrets, webhook signing secrets) | `provider_credentials` (control plane), envelope-encrypted under the app KMS key; write-only, never returned | **On request** by tenant admin via `PUT /api/organization/credentials/{block}` | Act as that one tenant against its ERP, processor or card issuer |
| App KMS key (`FEOH_CREDENTIAL_KMS_KEY_ID` — wraps the provider-credential data keys; also RDS/S3/SQS at rest) | AWS KMS (`infra/kms.tf`) | **365 days (auto)** | Unwrap every tenant's sealed provider credentials (with database read access) |
| Per-tenant chat webhook URL (Slack / Teams) | `Organization.settings.chat_notifications.webhook_url` | **On request** by tenant admin via `PUT /api/organization/chat-notifications/webhook` | Post arbitrary content into that tenant's approval channel — a phishing surface aimed at the people who approve payments |

**Triggers for an out-of-band rotation** — do these even if the cadence hasn't fired:
- Suspected leak (commit, log, screenshot, employee departure)
- Vendor breach affecting the third party that holds the secret
- Engineer who handled the secret leaves the team

---

## Procedures

### `FEOH_SECRET_KEY` (JWT signing key)

The riskiest secret in the system — leaks let an attacker forge any user's session.

1. Generate a new key:
   ```bash
   openssl rand -hex 32
   ```
2. Edit the encrypted file, from your `infra-secrets` clone:
   ```bash
   AWS_PROFILE=feohledger sops feohledger/prod.sops.yaml
   ```
   Update `FEOH_SECRET_KEY` (flat key `secret_key`) to the new value, and the same key in the VM's encrypted deploy env.
3. Deploy. **Every existing JWT becomes invalid immediately** — all users are forced to re-login. This is the intended behaviour.
4. Record the rotation in the compliance vendor's evidence locker (date, who rotated, ticket if any).

If you need a graceful rollover, support two keys (current + previous) for one rotation cycle. Not implemented today — accepted because forced re-login is acceptable for the user count.

### AWS KMS key (SOPS encryption)

The KMS key — `alias/feohledger-sops`, created in the FeohLedger AWS account by the estate account bootstrap, not by `infra/` — encrypts every secret under `infra-secrets/feohledger/`.

**Auto-rotation** — preferred. AWS KMS rotates the key material annually with no operator action when the `EnableKeyRotation` flag is set:
```bash
aws kms enable-key-rotation --key-id alias/feohledger-sops
aws kms get-key-rotation-status --key-id alias/feohledger-sops
```

**Manual rotation to a brand-new key** — if the key needs to be replaced (compromised IAM principal, audit finding):
1. Create a new key + alias:
   ```bash
   aws kms create-key --description 'AP SOPS rotation' --key-usage ENCRYPT_DECRYPT
   aws kms create-alias --alias-name alias/feohledger-sops-new --target-key-id <new-key-id>
   ```
2. Point the `feohledger` rule in `infra-secrets/.sops.yaml` at the new key.
3. Re-encrypt every sops file in the slot under the new key, from the `infra-secrets` clone:
   ```bash
   AWS_PROFILE=feohledger sops updatekeys feohledger/prod.sops.yaml
   ```
4. Commit + push in `infra-secrets`.
5. After 30 days with no incidents, schedule deletion of the old key:
   ```bash
   aws kms schedule-key-deletion --key-id <old-key-id> --pending-window-in-days 30
   ```

### RDS master password

See [§ RDS master password (break-glass)](#rds-master-password-break-glass) below — the
procedure is the Terraform one, and since the IAM cutover it no longer touches the VM.

### Third-party API keys (Anthropic, Lithic, Nium, hCaptcha, SES)

Same pattern for each:
1. Mint a new key in the provider's dashboard.
2. Update the corresponding secret in `infra-secrets` (`AWS_PROFILE=feohledger sops feohledger/prod.sops.yaml`).
3. Deploy.
4. Verify a request succeeds against the new key (e.g. trigger an extraction → log shows new key, billing dashboard logs a request).
5. Revoke the old key in the provider's dashboard.

For Anthropic + OpenAI specifically, **never reuse a key across environments** (dev/staging/prod each have their own). If a key is leaked from a single env, the blast radius is contained.

### Per-tenant SCIM bearer

Tenant admin self-serves rotation by re-calling `POST /api/organization/sso/scim-token`. The new token is shown once in the response; the old hash is overwritten in `org.settings.sso.scim_bearer_hash`. Any IdP still using the old token starts getting 401s — this is the desired behaviour because it forces the IdP to be reconfigured with the fresh token.

### Per-tenant OIDC client secret

Tenant admin creates the new secret in their Okta/Entra app, then pastes it into **Organization → Single Sign-On → Client secret** and saves (`PUT /api/organization/sso`). Every other field is round-tripped from the panel, so nothing else changes; leaving the field blank on a later save keeps the stored secret. The save is audited as `organization.sso_updated` with `client_secret` in `changed` — the name only, never the value. That row is written first, straight into the tenant DB in every `FEOH_AUDIT_MODE` (lambda included), and if it cannot be written the save answers `503` and the old secret stays in place — so a rotation is never applied unrecorded; retry once the audit path is healthy. A malformed request body is refused with a value-free `422` naming field locations only, so a botched paste never echoes the secret back. No endpoint ever returns the stored secret (`GET /api/organization` drops it for every role). `PATCH /api/organization` refuses an `sso` key, because its per-key merge used to replace the whole block on a secret-only body and switch SSO off unaudited. SSO handshakes after the change use the new secret. **No grace period** — coordinate with the IdP cutover. If the old secret already expired and the tenant is SSO-only, nobody can sign in to paste the new one: run the break-glass procedure (`docs/founder-runbooks/sso-break-glass.md`) first.

### Per-tenant provider credentials (ERP, payments, cards)

Tenant admin mints the new key at the provider, then pastes it into the matching
secret field on **Organization → ERP Integration / Payments / Virtual cards** and
saves (`PUT /api/organization/credentials/{block}`). The field is always empty
on load; a blank field keeps the stored value and "Remove the stored value"
deletes it. The save is audited as `organization.credentials_updated` with the
block and the changed path NAMES — written first, and a `503` with nothing
saved if it cannot be. The value is re-sealed under a fresh data key on every
write. No endpoint returns it. **No grace period**: revoke the old key at the
provider after the new one is confirmed (a "Test Connection" with the form
unchanged uses the stored key).

**The KMS key itself** rotates automatically every year (`enable_key_rotation`
in `infra/kms.tf`); AWS keeps prior key material for `Decrypt`, so stored rows
keep opening and nothing needs re-sealing. Moving to a *different* key (a new
`FEOH_CREDENTIAL_KMS_KEY_ID`) needs both keys usable during the change: each row
records the key that wrapped it and opens under that one, and is re-sealed under
the new key on its next write. Never schedule the old key for deletion while a
row still names it (`SELECT DISTINCT key_id FROM provider_credentials`).

### Per-subscription outbound-webhook signing secret

Tenant admin self-serves via `POST /api/webhooks/{id}/rotate-secret`. The new secret is shown once; the subscription keeps its id and its **entire delivery history** (unlike delete-and-recreate, which CASCADE-deletes the delivery log — recovering from a leak used to mean destroying the record of what had been delivered).

**Unlike the two above, this one has a grace period.** By default the retiring secret keeps signing a second `X-Webhook-Signature-Previous` header for 60 minutes (max 1440), so a receiver that accepts either header rotates with zero dropped deliveries. Pass `overlap_minutes: 0` for a hard cutover when the secret is known-compromised and must stop verifying immediately.

The receiver-side procedure — and the reason step 1 has to happen *before* you need it — is in [public-api.md](../backend/docs/public-api.md) § Rotating a signing secret. Audited as `webhook_subscription.secret_rotated`, recording the prefix and window, never either secret.

### Per-tenant chat-notification webhook URL (Slack / Teams)

A Slack or Teams **incoming-webhook URL is a credential, not an address**: the token lives in the path, and anyone holding the string can post arbitrary content into the tenant's approval channel forever, unauthenticated. That is a phishing surface pointed directly at the people who approve payments, so it belongs in this inventory even though it never enters a SOPS file (it's per-tenant config, like the SCIM bearer above).

Tenant admin self-serves, admin-only, on `/organization` → **Chat Notifications**:

1. **Delete the compromised webhook at the provider first** (Slack: the app's *Incoming Webhooks* page; Teams: the channel connector). *We cannot do this step* — the URL is issued by them and revoked by them. Until it's revoked there, the old URL still works no matter what we store.
2. Create a replacement webhook at the provider and copy the new URL.
3. `PUT /api/organization/chat-notifications/webhook` with the new URL. The replacement is **atomic**.
4. If you need containment *before* you have a replacement, `DELETE /api/organization/chat-notifications/webhook` instead. The adapters already fail closed with no URL (a no-op plus a PII-free warning), so the fan-out stops immediately and the rest of the org's chat config is left alone. Idempotent.

**Unlike the outbound-webhook signing secret above, there is deliberately no grace period.** That one rotates an HMAC *verifier* held by a counterparty, so an overlap window lets the receiver switch keys without dropping deliveries. This is a *destination*: we POST to exactly one URL, nobody else holds the old value, and keeping it live would mean posting every approval event into the compromised channel as well. An overlap here would extend the leak, not smooth a cutover.

Nothing is "shown once" either — we don't mint this value, the provider does, so the admin already has it. **No endpoint ever returns the stored URL**; reads report only whether one is configured plus its bare hostname (`hooks.slack.com`), which is enough to answer "where does our approval channel post?" during an incident without handing back the token. That holds for the generic `GET /api/organization` too — it serves the settings JSONB and used to return the credential in full to *any* authenticated role, so its response is now projected (`services/org_settings_view`), dropping this key for every role and stripping the other settings-JSON credentials for non-admins. `PATCH /api/organization` refuses a `chat_notifications` key, so the audited endpoint is the only writer.

Audited as `organization.chat_webhook_rotated` on both set/replace and removal (flagged `removed`), recording the previous and new **hostnames** only — never the URL. One action name covers the credential's whole lifecycle so an incident can be reconstructed with a single grep. Mechanics: [notifications.md](../backend/docs/notifications.md) § Rotating the webhook URL.

---

### `POSTGRES_PASSWORD` (single-VM deploy)

The Postgres image reads `POSTGRES_PASSWORD` only when it initialises an empty
data volume. Changing the value in sops alone therefore changes nothing in the
database — and the API's DSN, which `compose.prod.yml` derives from the same
variable, stops matching the role on the next deploy. Change the role first:

This section is the container-mode database (`docs/minimal-deployment.md`
§ Database); the RDS master password is the next section.

1. On the VM, from `deploy/`: `./compose.sh exec postgres psql -U postgres`,
   then `\password postgres` and type the new value at the prompt (it never reaches shell history).
2. In `infra-secrets`: `AWS_PROFILE=feohledger sops feohledger/prod.sops.yaml`, set
   `POSTGRES_PASSWORD`, save and commit; copy the file onto the VM as `deploy/prod.sops.yaml`.
3. `./deploy.sh --no-pull --backend-only` — recreates the api (new DSN) and the postgres
   container (the data volume and its initialised cluster are kept).

`backup.sh` and `restore.sh` connect over the container's local socket and are
unaffected.

### RDS master password (break-glass)

Since the IAM cutover (`deploy/README.md` § Database role, `docs/decisions.md`
§305) the app, its migrations and `backup.sh` / `psql.sh` sign in as `feoh_app`
with IAM tokens, so **the master password is not on the VM** and rotating it
does not interrupt the app. It is the break-glass credential: a logical
restore, a pgvector upgrade, a cutover re-run.

1. In your own terminal, `openssl rand -hex 24` (URL-safe — the break-glass URL
   embeds it), and put it in `infra-secrets/feohledger/terraform.sops.yaml` as
   `db_master_password`.
2. Bump `db_master_password_version` in `prod.tfvars` and apply as in
   `infra/README.md` § Workload stack step 3 (the password is a write-only,
   ephemeral input: it never lands in state or a plan file).
3. Nothing on the VM changes. Confirm the app is still on the app role:
   `./psql.sh -d feohledger -Atc "SELECT session_user"` prints `feoh_app`.

**The one-time rotation after the cutover is not optional.** Until the cutover
the master password was inside `FEOH_DATABASE_URL` in the VM's deploy env, so
treat it as exposed and rotate it as soon as the app is on `feoh_app`. Likewise
after any break-glass session that typed it on the VM.

**Before the cutover** (or if the app is ever put back on the master), the
password is also inside `FEOH_DATABASE_URL` in `prod.sops.yaml`: change it there
in the same sitting, copy the file onto the VM and run
`./deploy.sh --no-pull --backend-only`. Every new connection made with the old
password fails between the apply and that redeploy.

**There is no app-role password to rotate.** `feoh_app` has none (the cutover
sets `PASSWORD NULL`, and RDS refuses password sign-in for an `rds_iam` member
anyway), and `feoh_owner`, which owns the data, cannot sign in at all. To cut off the app role in an incident, remove the `ConnectAsAppRole`
statement from the VM role (`infra/compute.tf`) or `REVOKE rds_iam FROM
feoh_app` as the master. Tokens already minted are only good for opening a
connection in their 15 minutes; connections already open must be terminated
(`pg_terminate_backend`) as the master.

### HMAC signing keys (`FEOH_APPROVAL_SIGNING_KEY`, `FEOH_EMAIL_ACTION_SIGNING_KEY`, `FEOH_PARTNER_LINK_SIGNING_KEY`, `FEOH_EMAIL_INTAKE_SIGNING_SECRET`)

Generate the new value with `openssl rand -hex 32` in your own terminal, set it in
`feohledger/prod.sops.yaml`, copy the file onto the VM, and run
`./deploy.sh --no-pull --backend-only`. None of these keys supports an overlap
window, so know what each rotation breaks:

- **Approval signing** — verification of every signature made under the old key
  (`backend/docs/approval-signatures.md`). Treat it as a key ceremony: rotate
  only on a suspected leak, and record when.
- **Email action** — every approval link and chat button already sent, for up to
  `FEOH_EMAIL_ACTION_TTL_HOURS` (default 7 days). Reviewers approve in the app
  instead.
- **Partner link** — codes not yet redeemed (valid for 30 minutes).
- **Email intake** — every inbound message until the email provider's webhook
  signs with the new secret, so change both together.

## Logging + audit

Every rotation must leave a paper trail:

- **Code rotations** (`FEOH_SECRET_KEY`, third-party keys) — visible in the `infra-secrets` git history because they touch the encrypted file there. The rotator notes the secret name + date in the commit message (don't mention values).
- **AWS rotations** (KMS, RDS) — CloudTrail captures the API call. Compliance vendor pulls it as evidence.
- **Tenant rotations** (SCIM, OIDC) — application audit log writes a `tenant.scim_token_rotated` / `tenant.sso_secret_rotated` row (auth audit logging is on the SOC 2 prereq list — see `docs/soc2-readiness.md` § Logging).
- **Tenant chat-webhook rotations** — `organization.chat_webhook_rotated` into the tenant trail, hostnames only. This one is worth pulling as evidence in its own right: it is the only record that a leaked approval-channel credential was replaced, and before the endpoint existed the change was an untracked hand-edit of the settings JSON.

---

## Things that should never be secrets

If any of these end up in a SOPS file, they're misclassified — move them out:

- Non-sensitive endpoint URLs (already in plain `backend/.env.development`)
- Feature flags (use config, not secrets)
- Tenant slugs, plan names, feature toggles
- AWS region, account ID (treated as low-sensitivity by AWS itself)

---

## What we don't have yet

- **HashiCorp Vault or AWS Secrets Manager dynamic credentials** — DB credentials are still long-lived. Once volume warrants it, switch RDS auth to IAM tokens (15-minute lifetime, no static password to rotate).
- **Automated rotation runners** — Vanta/Drata can ping us to rotate; AWS Secrets Manager can do it for RDS automatically. Pending until SOC 2 is live.
- **SBOM + signed images** — supply-chain hardening is on the roadmap, separate from secrets.
