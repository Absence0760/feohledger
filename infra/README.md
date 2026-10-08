# infra/

Infrastructure-as-code for the FeohLedger AWS account: the **security substrate** needed as a SOC 2 engineering prerequisite (see `../docs/soc2-readiness.md`); the platform domain's registration settings and TLS certificate, its mail DNS (Migadu mailboxes and the SES sending identity) and a monthly cost budget; and the **workload stack** the app runs on — one VM plus an RDS database (`../docs/minimal-deployment.md`, `../docs/decisions.md` §254). The scale-up architecture (ECS, ALB, CloudFront) is `../docs/production-deployment.md` and is not built.

## Layout

```
infra/
├── .gitignore                   # ignores .terraform/, *.tfstate, *.tfplan
├── .terraform.lock.hcl          # committed — pins provider versions
├── main.tf                      # provider + backend + default_tags
├── variables.tf                 # aws_region, project, environment, bucket names, retention
├── kms.tf                       # customer-managed KMS key (rotation ON)
├── s3.tf                        # invoice-files + audit-logs buckets (versioning + Object Lock)
│                                #   + access-logs sink + backups bucket (lifecycle-expired, no lock)
├── acm.tf                       # us-east-1 certificate for the platform domain + wildcard
├── domain.tf                    # registration settings for the platform domain (renewal, lock, WHOIS privacy, name servers)
├── email.tf                     # mail DNS: Migadu mailboxes on the apex, SES identity + DKIM + MAIL FROM on send., DMARC
├── budgets.tf                   # account-wide monthly cost budget + email alerts
├── network.tf                   # workload VPC: 2 public subnets (VM), 2 private (RDS), no NAT, REJECT flow logs
├── compute.tf                   # app VM (EC2 Graviton, Session Manager, IMDSv2) + instance role + Elastic IP + apex/api/* DNS
├── database.tf                  # RDS Postgres 16: private, encrypted, TLS-only, point-in-time restore, write-only password
├── monitoring.tf                # SNS alerts topic + VM recovery/reboot and DB CPU/memory/storage alarms
├── outputs.tf                   # exports for downstream modules
├── backend.config.example       # state-bucket shape for `terraform init` (real one gitignored)
├── terraform.tfvars.example     # committed template
├── tests/                       # plan-only `terraform test` runs against mocked providers
└── README.md                    # this file
```

## Security posture

Every resource in this module follows the SOC 2 baseline:

| Control | Where |
|---|---|
| KMS auto-rotation (annual, customer-managed key) | `kms.tf` — `enable_key_rotation = true` |
| S3 versioning on every bucket | `s3.tf` — `aws_s3_bucket_versioning` = Enabled |
| S3 Object Lock — governance mode, 365d | `s3.tf` — invoice-files bucket |
| S3 Object Lock — compliance mode, 7y | `s3.tf` — audit-logs bucket |
| SSE-KMS on every data bucket; SSE-S3 on the access-logs sink | `s3.tf` — the invoice-files, audit-logs and backups buckets reference `aws_kms_key.app`. AWS does not support SSE-KMS on a server-access-log destination, so the sink uses S3-managed keys (`../docs/decisions.md` §166) |
| Server-access logging on every data bucket | `s3.tf` — delivered to the access-logs sink, whose bucket policy grants `logging.s3.amazonaws.com` `s3:PutObject`, pinned to this account and the three source buckets; ACLs disabled |
| Public access block on every bucket | `s3.tf` — all four flags true |
| Lifecycle cost guards | `s3.tf` — backups bucket expires dumps after `backup_retention_days` (90d default; deliberately NO Object Lock — the lifecycle IS the retention policy), and every lifecycle rule reaps incomplete multipart uploads after 7 days |

### The deploy role is not owned here

`feohledger-deploy` — the role `.github/workflows/aws-deploy.yml` assumes through
OIDC, whose ARN is the `AWS_DEPLOY_ROLE_ARN` environment secret — is **not in
this module and must not be added to it.** It is minted by the estate account
bootstrap (`~/github/templates/infra/modules/project-baseline`, driven by
`~/github/templates/scripts/new-project-account.sh`), its inputs live in
`Absence0760/infra-secrets` → `feohledger/bootstrap.tfvars`, and its state is in
the mgmt account's bucket. Change the trust policy there, never here — the
module is shared by every project account, so fixing it locally would fix this
account and leave the next one broken.

**The gotcha that makes this worth its own section (issue #449):** the trust
policy pins `token.actions.githubusercontent.com:sub` with `StringEquals`, and
this repo is on GitHub's **immutable subject claims** — the subject it presents
carries numeric org and repo IDs, not the slug:

```
repo:Absence0760@21693150/feohledger@1180437106:environment:production
```

Whether a repo has immutable subjects is not derivable from anything Terraform
can see, and across this org some do and some do not. So read it, never build it
from the slug:

```bash
gh api repos/Absence0760/feohledger/actions/oidc/customization/sub --jq .sub_claim_prefix
```

Getting it wrong is silent: the role mints fine, the plan is clean, and the first
deploy fails `AssumeRoleWithWebIdentity` with "Not authorized" — naming neither
the expected nor the received subject, while CloudTrail redacts
`requestParameters` on an AccessDenied so the role ARN is not there either. Run
**`AWS OIDC preflight`** (`.github/workflows/aws-oidc-preflight.yml`,
`workflow_dispatch`) to exercise the handshake on its own; it prints the subject
this repo presents when it fails.

### Caveat: Object Lock is immutable

`object_lock_enabled` on `aws_s3_bucket` is **set at creation and cannot be toggled afterwards**. The buckets defined in `s3.tf` are net-new. For any pre-existing bucket (e.g. one that predated this module):

1. Create a new bucket with `object_lock_enabled = true` (add a `-locked` suffix to avoid the name cooldown).
2. `aws s3 sync s3://old s3://new` to copy all objects across.
3. Update the application's `FEOH_S3_BUCKET` (or equivalent) to the new name and deploy.
4. Once retention on the new bucket is verified, schedule deletion of the old bucket.

This migration path is also tracked under "Pending — needs a code change" in `../docs/soc2-readiness.md`.

## Platform domain + certificate

The platform domain is `feohledger.com` (`var.domain_name`, which accepts only a registered apex). It is **bought by hand** in the Route 53 console while signed in to the FeohLedger account — one year, auto-renew and privacy protection on — and the registration creates the `feohledger.com` public hosted zone in the account. Terraform does not register it: that would need the registrant's name, address and phone number as configuration, and a registration Terraform owns can be deregistered by a destroy (`../docs/decisions.md` §167). What Terraform owns is what must not drift afterwards: `domain.tf` adopts the registration and keeps it auto-renewing, transfer-locked, private in WHOIS and delegated to that zone, and `terraform destroy` only drops it from state.

`acm.tf` issues the TLS certificate the workload stack's CloudFront distribution will use: the domain plus `*.<domain>`, requested in **us-east-1** through the `aws.us_east_1` provider alias because CloudFront accepts no other region (Route 53 Domains is served only from us-east-1 too). It is DNS-validated in the same zone during the apply, and `platform_certificate_arn` only resolves once it is issued.

The wildcard is what makes tenant subdomains work — the SPA takes the tenant slug from the first label under the platform domain (`frontend/src/lib/hostRouting.ts`), so every `<slug>.<domain>` is covered without a certificate change per signup. Tenants live directly under the platform domain on every deployment shape, so no nested SAN is needed; a tenant's own vanity domain would need its own certificate — see the comment at the top of `acm.tf`.

## Email — Migadu mailboxes + SES app mail

`email.tf` puts two independent mail systems in the platform zone (`../docs/decisions.md` §172):

| For | What it creates | Names |
|---|---|---|
| Mailboxes (`ops@`, aliases such as `noreply@`) on Migadu, the estate's mail host | MX, the SPF + ownership TXT, three DKIM CNAMEs, the `autoconfig` CNAME and three SRV client hints | the apex, `key1`–`key3._domainkey`, `autoconfig`, `_imaps._tcp` … |
| App mail through SES in this account (`FEOH_EMAIL_PROVIDER=ses`) | the SES domain identity, its three Easy DKIM CNAMEs, and the `send.<domain>` MAIL FROM domain with its bounce MX and SPF | `<token>._domainkey`, `send` |
| Both | one DMARC record, `p=none` until reports show alignment | `_dmarc` |

The apex SPF authorizes Migadu alone: SES mail's envelope sender is on `send.`, and both systems DKIM-sign as the apex domain, so DMARC aligns for each.

Bring it up in this order:

1. **Add the domain in Migadu** (admin.migadu.com → Domains, in the estate's existing account) and copy the token from its DNS page — the part after `hosted-email-verify=`. Put it, and the DMARC report mailbox, in `infra-secrets/feohledger/prod.tfvars`:
   ```hcl
   migadu_verification_token = "<token>"
   dmarc_report_email        = "ops@feohledger.com"
   ```
   Neither is secret; both end up in public DNS.
2. **Plan and apply** (§ Applying). Everything else can go in before the token is known; the token only adds a second string to the apex TXT record.
3. **Finish in Migadu:** run its DNS check, then create the `ops@` mailbox, plus a `noreply@` alias if replies to app mail should land somewhere.
4. **Wait for SES to verify the identity.** It checks the DKIM CNAMEs itself — up to 72 hours, usually far less. It is ready when this reports `sending: true`, `dkim: SUCCESS` and `mailFrom: SUCCESS`:
   ```bash
   aws sesv2 get-email-identity --email-identity feohledger.com --profile feohledger --query '{sending:VerifiedForSendingStatus,dkim:DkimAttributes.Status,mailFrom:MailFromAttributes.MailFromDomainStatus}'
   ```
5. **Request SES production access** (SES console → Account dashboard). A new account is sandboxed and delivers only to verified addresses; until access is granted, keep `FEOH_EMAIL_PROVIDER=console` in the deploy env.
6. **Tighten DMARC** once a few days of reports show both senders passing: `dmarc_policy = "quarantine"`, later `"reject"`.

The VM sends with its instance profile, so grant it `ses:SendEmail` on the `ses_identity_arn` output (`../docs/minimal-deployment.md` § 1).

## Cost guardrail

`budgets.tf` puts an account-wide monthly budget on the FeohLedger account — `monthly_budget_limit_usd`, default **75 USD**, sized for the single-VM + RDS workload stack's ~$45–55/month — with email alerts at 50 % and 100 % of actual spend and at 100 % of forecast. The forecast alert is the early warning; the actual ones only fire after spend lands on the bill.

`budget_alert_emails` has **no default** and must be set in the operator's tfvars: this repo is public, so the address lives only in the private `infra-secrets` copy (see § Applying). Plan rejects an empty list and the `example.com` placeholder.

If AWS refuses the budget with "ask the payer account to enable budgets", enable *IAM user and role access to billing information* in the account's root settings. Don't remove the budget instead.

## Local usage

```bash
cd infra
# Terraform 1.15+ validates the partial-backend block (its bucket is passed
# at init time), so point validate at the local backend first. The override
# file is gitignored — never commit it.
printf 'terraform {\n  backend "local" {}\n}\n' > backend_override.tf
terraform init -backend=false    # skip the S3 backend for local validation
terraform fmt -recursive .       # format
terraform validate               # syntactic + type-check (no AWS creds needed)
terraform test                   # tests/ — mocked providers, no creds, no state
rm backend_override.tf           # remove before any real plan/apply
```

## Applying

State lives in `feohledger-tfstate-<account-id>`, the bucket the estate account bootstrap (`~/github/templates/scripts/new-project-account.sh`) created in the FeohLedger AWS account. Locking is S3-native (`use_lockfile`), so there is no DynamoDB table. Key (`envs/prod/terraform.tfstate`), region, locking and encryption are committed in `main.tf`; only the bucket name — which embeds the account ID — is passed at init time, from a gitignored `backend.config`.

**Register `feohledger.com` first** (§ Platform domain + certificate). Until the registration has created its hosted zone, `plan` fails on the zone lookup in `acm.tf`, and `domain.tf` has no registration to adopt.

Run from `infra/` with Terraform 1.15 (what CI validates against) and the `feohledger` SSO profile (`AdministratorAccess` in the FeohLedger account):

```bash
aws sso login --profile feohledger
printf 'bucket = "feohledger-tfstate-%s"\n' "$(aws sts get-caller-identity --profile feohledger --query Account --output text)" > backend.config
rm -f backend_override.tf   # a leftover validate override would keep state on your laptop
AWS_PROFILE=feohledger terraform init -backend-config=backend.config
AWS_PROFILE=feohledger terraform plan -var-file=../../infra-secrets/feohledger/prod.tfvars -out=tfplan
AWS_PROFILE=feohledger terraform apply tfplan
```

The filled tfvars is operator config rather than a secret — bucket names, the budget alert address — but it is not public either, so its canonical copy lives in the private `infra-secrets` repo as `feohledger/prod.tfvars` (plaintext, beside the encrypted secrets) — the estate convention for non-secret env config. Start it from `terraform.tfvars.example`. The path above assumes `infra-secrets` is cloned beside this repo under `~/github/`.

## Workload stack

`network.tf`, `compute.tf`, `database.tf` and `monitoring.tf` are the infrastructure the app runs on: one EC2 VM running the Docker Compose stack in `../deploy/` (Caddy, the API, Redis), and the database on RDS. Why that shape rather than the full ECS build-out, and why RDS rather than Postgres on the VM: `../docs/decisions.md` §254. Roughly **$45–55/month**:

| Resource | Monthly (us-east-1) |
|---|---|
| EC2 `t4g.medium` + 30 GB gp3 + public IPv4 | ~$31 |
| RDS `db.t4g.micro` + 20 GB gp3 + backups inside the free allowance | ~$15 |

Both instances are burstable and run in *unlimited* credit mode (the AWS default): under sustained load they are not throttled, they bill surplus CPU on top of these figures. The `*-cpu-credits-low` alarms are the cue to move up a size instead.
| KMS, Route 53, S3, flow logs (REJECT only), alarms | a few dollars |

**What it sets up**

- **No SSH.** Port 22 is closed and there is no key pair; you reach the VM with Session Manager: `aws ssm start-session --target <app_instance_id> --profile feohledger` (needs the Session Manager plugin for the AWS CLI installed locally).
- **The VM holds no AWS keys.** Its instance role can decrypt the sops secrets, read and write the three data buckets under the app key, and send mail as the platform domain — nothing else.
- **The database is private.** Private subnets with no internet route, reachable only from the VM's security group, encrypted with the app key, TLS forced (`rds.force_ssl`), deletion-protected, and restorable to any point in the last `db_backup_retention_days` (default 7). The nightly logical dumps (`../deploy/backup.sh`) keep 90 days beyond that.
- **DNS.** The apex, `api.` and `*.` (every tenant) point at the VM's Elastic IP.
- **Alarms** go to `alert_emails` through SNS — each address must click AWS's confirmation link once. The VM recovers itself onto new hardware on a host failure and reboots on a guest failure.

**The database password never enters Terraform state.** `db_master_password` is an *ephemeral* variable feeding RDS's *write-only* `password_wo`, so it is sent to AWS and stored nowhere else — not in state, not in a plan file. That is why this module does not read it through a `sops_file` data source (§ Secrets): a data source's value is written to state in plaintext. The operator supplies it at apply time from the private `infra-secrets` repo, as an environment variable on the one command.

**First apply — operator steps.** Steps 2–3 touch a secret, so they are yours to run; nothing here prints it.

1. Add `alert_emails = ["…"]` to `prod.tfvars` (it has no default — the repo is public).
2. Create the password once in `infra-secrets` (a 48-hex-character value is URL-safe, which it must be because it is embedded in `FEOH_DATABASE_URL`): `cd ~/github/infra-secrets && AWS_PROFILE=feohledger sops feohledger/terraform.sops.yaml`, and add a `db_master_password:` key set to the output of `openssl rand -hex 24`.
3. Plan and apply with the password passed in from sops (one line each):
   `cd ~/github/feohledger/infra && AWS_PROFILE=feohledger TF_VAR_db_master_password="$(sops -d --extract '["db_master_password"]' ../../infra-secrets/feohledger/terraform.sops.yaml)" terraform plan -var-file=../../infra-secrets/feohledger/prod.tfvars -out=tfplan`
   then `AWS_PROFILE=feohledger TF_VAR_db_master_password="$(sops -d --extract '["db_master_password"]' ../../infra-secrets/feohledger/terraform.sops.yaml)" terraform apply tfplan`.
   An ephemeral variable must be supplied again at apply time even from a saved plan — that is the reason it appears twice.
4. Confirm the SNS subscription email AWS sends to each alert address.
5. Then the VM side: `../deploy/README.md` (bootstrap, the sops env with `FEOH_DATABASE_URL` pointing at the `db_address` output, first deploy).

**Rotating the database password**: put the new value in `terraform.sops.yaml`, bump `db_master_password_version` in `prod.tfvars`, apply as in step 3, then update the password inside `FEOH_DATABASE_URL` in `prod.sops.yaml` and redeploy. Between the apply and the redeploy the app cannot connect, so do both in one sitting.

## Secrets

This repo is **public**, so it holds no secret — not even an encrypted one (`../docs/decisions.md` §12, §165). The project's secrets live sops-encrypted in the private `Absence0760/infra-secrets` repo under `feohledger/`, keyed by `alias/feohledger-sops`. The estate account bootstrap (`~/github/templates/scripts/new-project-account.sh`) created that key in the FeohLedger account, alongside the state bucket and the deploy role; this module neither creates nor manages it.

The one secret this module handles is the RDS master password, and it handles it without storing it: an ephemeral variable feeding a write-only attribute, supplied from `infra-secrets/feohledger/terraform.sops.yaml` at apply time (§ Workload stack). The earlier plan here — a `carlpett/sops` `sops_file` data source — was dropped because a data source's value lands in state in plaintext; the write-only path keeps it out of state and plan files entirely. Every other variable is operator config, not a credential. Access is IAM — an operator needs `kms:Decrypt` on the key plus read access to the private repo; nothing in this repo changes. Rotation: `../docs/secrets-rotation.md`.

## Tearing everything down

The sops key (`alias/feohledger-sops`), the state bucket and the GitHub deploy role belong to the account bootstrap, not this module — a `terraform destroy` here never touches them, and they are not to be deleted from here.

Note: audit-logs bucket uses Object Lock in **Compliance** mode — you cannot delete that bucket until every object has aged past its 7-year retention. Factor that into any teardown plan.

## See also

- `~/github/infra-secrets/feohledger/` (private) — the encrypted secrets and their plaintext template
- `~/github/project-mgmt/docs/secrets-management.md` — the estate secrets pattern
- `../backend/CLAUDE.md` § Secrets management — day-to-day encrypt/decrypt workflow
- `../docs/soc2-readiness.md` — control-by-control status + pending items
