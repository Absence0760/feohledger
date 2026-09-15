# infra/

Infrastructure-as-code for this project. Scoped today to the **security substrate** needed as a SOC 2 engineering prerequisite (see `../docs/soc2-readiness.md`). Real AWS workload resources (ECS, ALB, RDS, CloudFront) are not yet defined here; they live on the roadmap under `docs/production-deployment.md`.

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
├── outputs.tf                   # exports for downstream modules
├── backend.config.example       # state-bucket shape for `terraform init` (real one gitignored)
├── terraform.tfvars.example     # committed template
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
| SSE-KMS on every bucket | `s3.tf` — references `aws_kms_key.app` |
| Public access block on every bucket | `s3.tf` — all four flags true |
| Lifecycle cost guards | `s3.tf` — backups bucket expires dumps after `backup_retention_days` (90d default; deliberately NO Object Lock — the lifecycle IS the retention policy), and every lifecycle rule reaps incomplete multipart uploads after 7 days |

### Caveat: Object Lock is immutable

`object_lock_enabled` on `aws_s3_bucket` is **set at creation and cannot be toggled afterwards**. The buckets defined in `s3.tf` are net-new. For any pre-existing bucket (e.g. one that predated this module):

1. Create a new bucket with `object_lock_enabled = true` (add a `-locked` suffix to avoid the name cooldown).
2. `aws s3 sync s3://old s3://new` to copy all objects across.
3. Update the application's `FEOH_S3_BUCKET` (or equivalent) to the new name and deploy.
4. Once retention on the new bucket is verified, schedule deletion of the old bucket.

This migration path is also tracked under "Pending — needs a code change" in `../docs/soc2-readiness.md`.

## Local usage

```bash
cd infra
# Terraform 1.15+ validates the (intentionally empty) partial-backend block,
# so point validate at the local backend first. The override file is
# gitignored — never commit it.
printf 'terraform {\n  backend "local" {}\n}\n' > backend_override.tf
terraform init -backend=false    # skip the S3 backend for local validation
terraform fmt -recursive .       # format
terraform validate               # syntactic + type-check (no AWS creds needed)
rm backend_override.tf           # remove before any real plan/apply
```

## Applying

State lives in `feohledger-tfstate-<account-id>`, the bucket the estate account bootstrap (`~/github/templates/scripts/new-project-account.sh`) created in the FeohLedger AWS account. Locking is S3-native (`use_lockfile`), so there is no DynamoDB table. Key (`envs/prod/terraform.tfstate`), region, locking and encryption are committed in `main.tf`; only the bucket name — which embeds the account ID — is passed at init time, from a gitignored `backend.config`.

Run from `infra/` with Terraform 1.15 (what CI validates against) and the `feohledger` SSO profile (`AdministratorAccess` in the FeohLedger account):

```bash
aws sso login --profile feohledger
printf 'bucket = "feohledger-tfstate-%s"\n' "$(aws sts get-caller-identity --profile feohledger --query Account --output text)" > backend.config
rm -f backend_override.tf   # a leftover validate override would keep state on your laptop
AWS_PROFILE=feohledger terraform init -backend-config=backend.config
AWS_PROFILE=feohledger terraform plan -var-file=../../infra-secrets/feohledger/prod.tfvars -out=tfplan
AWS_PROFILE=feohledger terraform apply tfplan
```

The filled tfvars is operator config rather than a secret, but it is not public either, so its canonical copy lives in the private `infra-secrets` repo as `feohledger/prod.tfvars` (plaintext, beside the encrypted secrets) — the estate convention for non-secret env config. Start it from `terraform.tfvars.example`. The path above assumes `infra-secrets` is cloned beside this repo under `~/github/`.

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
