# infra/

Infrastructure-as-code for the FeohLedger AWS account. Scoped today to the **security substrate** needed as a SOC 2 engineering prerequisite (see `../docs/soc2-readiness.md`), plus two account-level pieces the workload stack will lean on: the platform TLS certificate and a monthly cost budget. Real AWS workload resources (ECS, ALB, RDS, CloudFront) are not yet defined here; they live on the roadmap under `docs/production-deployment.md`.

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
├── budgets.tf                   # account-wide monthly cost budget + email alerts
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

### Caveat: Object Lock is immutable

`object_lock_enabled` on `aws_s3_bucket` is **set at creation and cannot be toggled afterwards**. The buckets defined in `s3.tf` are net-new. For any pre-existing bucket (e.g. one that predated this module):

1. Create a new bucket with `object_lock_enabled = true` (add a `-locked` suffix to avoid the name cooldown).
2. `aws s3 sync s3://old s3://new` to copy all objects across.
3. Update the application's `FEOH_S3_BUCKET` (or equivalent) to the new name and deploy.
4. Once retention on the new bucket is verified, schedule deletion of the old bucket.

This migration path is also tracked under "Pending — needs a code change" in `../docs/soc2-readiness.md`.

## Platform domain + certificate

The platform domain is `feohledger.com` (`var.domain_name`, which accepts only a registered apex). It is **bought by hand** in the Route 53 console while signed in to the FeohLedger account — one year, auto-renew and privacy protection on — and the registration creates the `feohledger.com` public hosted zone in the account. Terraform does not register it: that would need the registrant's name, address and phone number as configuration, and a registration Terraform owns can be deregistered by a destroy (`../docs/decisions.md` §167). What Terraform owns is what must not drift afterwards: `domain.tf` adopts the registration and keeps it auto-renewing, transfer-locked, private in WHOIS and delegated to that zone, and `terraform destroy` only drops it from state. The `feohledger.jaredhoward.com` zone the account bootstrap delegated here is unused; retiring it is an operator step in `../docs/followups.md`.

`acm.tf` issues the TLS certificate the workload stack's CloudFront distribution will use: the domain plus `*.<domain>`, requested in **us-east-1** through the `aws.us_east_1` provider alias because CloudFront accepts no other region (Route 53 Domains is served only from us-east-1 too). It is DNS-validated in the same zone during the apply, and `platform_certificate_arn` only resolves once it is issued.

The wildcard is what makes tenant subdomains work — the SPA takes the tenant slug from the first label under the platform domain (`frontend/src/lib/hostRouting.ts`), so every `<slug>.<domain>` is covered without a certificate change per signup. A nested platform domain (`<slug>.app.<domain>`) or a tenant's own vanity domain would each need another certificate; see the comment at the top of `acm.tf`.

## Cost guardrail

`budgets.tf` puts an account-wide monthly budget on the FeohLedger account — `monthly_budget_limit_usd`, default **25 USD**, sized for a pre-launch account whose real spend is a few dollars — with email alerts at 50 % and 100 % of actual spend and at 100 % of forecast. The forecast alert is the early warning; the actual ones only fire after spend lands on the bill.

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

## Secrets

This repo is **public**, so it holds no secret — not even an encrypted one (`../docs/decisions.md` §12, §165). The project's secrets live sops-encrypted in the private `Absence0760/infra-secrets` repo under `feohledger/`, keyed by `alias/feohledger-sops`. The estate account bootstrap (`~/github/templates/scripts/new-project-account.sh`) created that key in the FeohLedger account, alongside the state bucket and the deploy role; this module neither creates nor manages it.

Nothing this module reads is secret today — every variable is operator config, not a credential. When the first real secret lands (the RDS master password, with the workload stack), read it in place with the `carlpett/sops` provider, never through a committed or decrypted tfvars:

```hcl
data "sops_file" "secrets" {
  source_file = "${path.module}/../../infra-secrets/feohledger/prod.sops.yaml"
}
# ... = data.sops_file.secrets.data["rds_master_password"]
```

The path assumes `infra-secrets` is cloned beside this repo under `~/github/`. It is deliberately not wired yet: `prod.sops.yaml` is created with the first real secret, and a `sops_file` data source on a missing file fails every plan. Access is IAM — an operator needs `kms:Decrypt` on the key plus read access to the private repo; nothing in this repo changes. Rotation: `../docs/secrets-rotation.md`.

## Tearing everything down

The sops key (`alias/feohledger-sops`), the state bucket and the GitHub deploy role belong to the account bootstrap, not this module — a `terraform destroy` here never touches them, and they are not to be deleted from here.

Note: audit-logs bucket uses Object Lock in **Compliance** mode — you cannot delete that bucket until every object has aged past its 7-year retention. Factor that into any teardown plan.

## See also

- `~/github/infra-secrets/feohledger/` (private) — the encrypted secrets and their plaintext template
- `~/github/project-mgmt/docs/secrets-management.md` — the estate secrets pattern
- `../backend/CLAUDE.md` § Secrets management — day-to-day encrypt/decrypt workflow
- `../docs/soc2-readiness.md` — control-by-control status + pending items
