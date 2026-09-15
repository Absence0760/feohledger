---
description: Audit the Terraform under infra/ and the deploy pipeline — KMS, S3 Object Lock/WORM, OIDC least-privilege, state backend, and drift between the code and the deployment docs
---

Audit `infra/` and the AWS-facing CI against `infra/README.md`, `docs/production-deployment.md`, `docs/minimal-deployment.md` and `docs/soc2-readiness.md`.

## Goal

Two distinct risks, and it matters which one a finding belongs to:

1. **What is actually built.** `infra/` is deliberately scoped to the *security substrate* — a customer-managed KMS key and the S3 buckets (invoice files, the Object-Lock audit-log WORM sink, an access-log sink, backups). Compute (ECS/Fargate, ALB, RDS, CloudFront) is **not defined here yet**; `docs/production-deployment.md` describes the target, not the current state. Do not report an absent ECS module as a misconfiguration — report it as scope, and only flag it if a doc or workflow claims it exists.
2. **What the pipeline can reach.** `.github/workflows/aws-deploy.yml` is a scaffold gated off by the `AWS_DEPLOY_ENABLED` repository variable, assuming a GitHub OIDC role. A permissive trust policy on that role is the one finding that makes everything else moot: it turns a fork's PR into account write access.

The audit's job is to keep those two honest against each other — code, docs, and workflow all describing the same reality.

## Files in scope

- `infra/main.tf` — provider, backend, `default_tags`
- `infra/variables.tf` — inputs + validation
- `infra/kms.tf` — the customer-managed key (rotation)
- `infra/s3.tf` — invoice-files + audit-logs (versioning + Object Lock) + access-logs + backups
- `infra/outputs.tf` — exports consumed downstream
- `infra/terraform.tfvars.example` (committed template) and the **absence** of a committed `terraform.tfvars.sops` — see §7
- `infra/.terraform.lock.hcl` — provider pinning
- `.github/workflows/{aws-deploy,terraform}.yml`
- `docs/production-deployment.md`, `docs/minimal-deployment.md`, `docs/backup-disaster-recovery.md`, `docs/soc2-readiness.md`

## What to check

1. **State backend.** `main.tf` declares an S3 backend with `encrypt = true` and locking (a DynamoDB lock table, or S3-native `use_lockfile = true` on Terraform ≥ 1.10 — recommend the latter as a Low/Note, not a finding). Confirm the state bucket has versioning + a Public Access Block; those pre-exist Terraform so verify via the bootstrap path, not the plan.

2. **KMS (`kms.tf`).** `enable_key_rotation = true` on the customer-managed key. The key policy grants only the principals that need it — no `Principal: "*"`, no account-root-plus-wildcard-action shape. Its alias (`alias/<project>-app-<env>`) must stay distinct from `alias/feohledger-sops`: the sops key is created by the estate account bootstrap, used only by the private `Absence0760/infra-secrets` repo's `.sops.yaml`, and never managed from this module (see the root `CLAUDE.md` § secrets — this repo's production secrets belong in that private repo, **not** in this public repo's history).

3. **S3 (`s3.tf`).** For every bucket: `aws_s3_bucket_public_access_block` with all four flags `true`; versioning enabled; SSE configured against the KMS key (not `AES256`, if the SOC 2 claim is a customer-managed key); no legacy `aws_s3_bucket_acl`; a bucket policy with a concrete `Principal` and a `SourceArn`/`SourceAccount` condition, never `"*"`; a lifecycle rule expiring non-current versions so version sprawl is not an unbounded bill.

4. **Object Lock is the SOC 2 claim — verify it end to end.** The audit-log bucket carries Object Lock so shipped `audit_log` rows are immutable. Confirm: Object Lock is enabled **at bucket creation** (it cannot be added later), the retention mode and period match what `backend/docs/audit-log-shipping.md` and `docs/soc2-readiness.md` claim, and the retention window is at least as long as the retention policy in `backend/docs/retention.md` promises. Also confirm the *governance* vs *compliance* mode choice is deliberate and documented — governance can be bypassed with a privileged permission, which is a meaningful caveat in an auditor's eyes. A mismatch between the doc's claim and the resource is **High**: it is a control we assert to customers.

5. **OIDC least-privilege.** Wherever the deploy role is defined (in this repo or in the estate bootstrap it inherits), the trust policy must pin **both** `:aud = sts.amazonaws.com` and a specific `:sub` — repo **and** `environment:production`. A `StringLike` wildcard on `:sub`, or the condition removed, is **Critical**. The attached policies must be per-resource ARNs — no `iam:*`, no `sts:AssumeRole`, no `secretsmanager:*`, no `kms:*` on the whole account. Cross-check against `docs/production-deployment.md` § CI/CD.

6. **The deploy workflow's gates.** `aws-deploy.yml` must keep all three: the `AWS_DEPLOY_ENABLED` kill switch in a job-level `if` (repository variable, not environment-scoped — an environment var does not resolve in `if`), `environment: production` on **every** job that touches AWS, and a `concurrency` group with `cancel-in-progress: false`. An environment with no required reviewer defeats gate three, so say so if you cannot confirm the reviewer is configured. Every action pinned by SHA.

7. **Secrets discipline.** This repo is **public**. Confirm no decrypted `terraform.tfvars`, no `*.tfstate`, and no real credential is tracked; that `infra/.gitignore` covers `.terraform/`, `*.tfstate*`, `*.tfplan`; and that the tracked `terraform.tfvars.example` holds only placeholders. Per the root `CLAUDE.md`, committing an encrypted `*.sops` payload here is itself the mistake to avoid — flag a newly-tracked `*.sops` in this repo as **High** with the pointer to the private estate secrets repo.

8. **Cost + DR guardrails.** Budgets and alarms are deliberately **not** in the estate baseline — each project's own infra owns them. So: is there a budget/alarm resource here at all, and if not does a doc own that gap? Cross-check `docs/backup-disaster-recovery.md`'s stated RTO/RPO against what the buckets actually provide (versioning + lifecycle ≠ a tested restore), and flag any restore procedure the code cannot support.

9. **Drift between code and docs.** Read `infra/README.md`'s layout table against `ls infra/`. A file listed but absent (or present but undocumented) is a **Low** that predicts a bigger one — this is exactly how a stale claim about a control survives an audit.

## Report

- **Critical** — an OIDC trust policy a fork can satisfy; a bucket reachable publicly; a deploy role with account-wide IAM/KMS/secrets actions; a real credential or decrypted tfvars tracked in this public repo.
- **High** — a SOC 2 control we assert (Object Lock, KMS rotation, encryption at rest) that the code does not actually implement, or implements more weakly than the doc claims; a deploy job missing `environment: production`.
- **Medium** — missing lifecycle/versioning guardrail, unpinned action or provider, no budget/alarm ownership anywhere.
- **Low** — README drift, a recommended-but-optional modernisation (S3-native state locking), undocumented deliberate choice.

For each finding: `file:line`, the resource, the exact attribute to change, and — where the finding is a doc/code mismatch — which side you judge to be right and why.

## Delegate to

Use the `repo-security-auditor` agent: `"Audit the Terraform under infra/ plus the AWS deploy workflow — KMS, S3 Object Lock/WORM against the SOC 2 claim, OIDC trust-policy least-privilege, state backend, secrets discipline in a public repo, and drift between infra/README.md and the actual files."`

Read-only. Report findings; don't `terraform apply` anything.
