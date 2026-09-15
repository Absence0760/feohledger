# Production deployment — first deploy to AWS

**Why this matters**: The product is currently demo-able on `localhost`.
You cannot sell a localhost. The first paying customer needs a real
URL, a real database, real uptime, and someone to wake up when it's
on fire.

> This runbook is the **founder-facing checklist** — what buttons to
> push, in what order. For the engineering rationale behind the
> architecture (why CloudFront → ALB → ECS, why Lambdas for the
> async paths, service-by-service deployment targets), see
> [`docs/production-deployment.md`](../production-deployment.md).

## Current state

`infra/` holds the security substrate (KMS key + S3 buckets); the VPC,
ECS, RDS and CloudFront stack is not written yet, and **nothing is
deployed**. No deployed secret has been authored yet — they will live
sops-encrypted in the private `infra-secrets` repo (`feohledger/`),
never in this public repo.

## What you need

- AWS account (preferably new, dedicated to the production workload)
- A domain you control (e.g., `feohledger.com`)
- A few hours of focused DevOps work

## Step 1 — AWS account setup

**The account exists (2026-09-14).** The estate bootstrap
(`new-project-account.sh feohledger`) created **FeohLedger** inside the estate
AWS Organization, with the Terraform state bucket, the sops KMS key, a GitHub
OIDC deploy role and the delegated `feohledger.jaredhoward.com` zone. Operators
sign in through IAM Identity Center (`aws sso login --profile feohledger`), not
IAM users or root keys. That covers items 1, 2 and 5 below; 3 and 4 are still
open.

1. Create a dedicated AWS account for production. Don't mix with
   personal/sandbox.
2. Enable **AWS Organizations** and add the account under it (makes
   billing + IAM easier to audit later for SOC 2).
3. Enable **CloudTrail** org-wide — SOC 2 auditor will ask for it.
4. Enable **GuardDuty** + **Security Hub** — free tier covers the
   basics.
5. Set up an **IAM role for Terraform** (instead of root keys). The
   `infra/README.md` has the policy JSON.

## Step 2 — Domain + ACM certificate

**Handled by Terraform (`infra/acm.tf`).** The platform domain today is
`feohledger.jaredhoward.com`, a Route 53 zone the account bootstrap
delegated to the FeohLedger account. The first `terraform apply` issues
the `us-east-1` certificate (CloudFront requires that region) for the
apex plus `*.feohledger.jaredhoward.com` — the wildcard is what serves
tenant subdomains — and DNS-validates it in that zone.

To move to a product apex (e.g. `feohledger.com`): register it, host its
zone in the FeohLedger account, and set `domain_name`.

## Step 3 — Populate SOPS secrets

Secrets live encrypted in the private `infra-secrets` repo, never here
(this repo is public). From that clone, authenticated to the FeohLedger
account:

```bash
cd ~/github/infra-secrets
AWS_PROFILE=feohledger sops feohledger/prod.sops.yaml
```

Required values for prod:
- `FEOH_SECRET_KEY` — generate with `openssl rand -hex 32`
- `FEOH_DATABASE_URL` — RDS endpoint from Terraform outputs
- `FEOH_REDIS_URL` — ElastiCache endpoint
- `FEOH_S3_BUCKET` — Terraform-provisioned invoice bucket
- `FEOH_ANTHROPIC_API_KEY` — your Claude Vision key
- `FEOH_MFA_ENABLED=true`
- `FEOH_HSTS_ENABLED=true`
- `FEOH_AUDIT_SHIPPING_ENABLED=true`
- `FEOH_AUDIT_SHIPPING_PROVIDERS=cloudwatch,s3_objectlock`
- `FEOH_AUDIT_SHIPPING_S3_BUCKET` — from Terraform
- `FEOH_EMAIL_PROVIDER=ses`
- `FEOH_EMAIL_INTAKE_DOMAIN=ap.feohledger.com` (see
  `backend/docs/email-intake.md`)
- `FEOH_EMAIL_INTAKE_SIGNING_SECRET` — generate with `openssl rand -hex 32`

## Step 4 — `terraform apply`

```bash
cd infra
# backend.config names the state bucket — see infra/README.md § Applying
AWS_PROFILE=feohledger terraform init -backend-config=backend.config
AWS_PROFILE=feohledger terraform plan -var-file=../../infra-secrets/feohledger/prod.tfvars -out=tfplan   # read this carefully
AWS_PROFILE=feohledger terraform apply tfplan
```

Expect errors on the first run. Common ones:
- **ACM cert not validated** — DNS records propagating. Wait 5 min.
- **RDS subnet group missing** — Terraform ordering issue. Apply
  twice.
- **S3 bucket name conflict** — S3 bucket names are globally unique.
  Prefix with your company slug.

## Step 5 — Deploy the backend container

The backend runs on ECS Fargate. CI builds a Docker image on every
push to `main` and pushes to ECR. The deploy action updates the ECS
service.

Before the first deploy:
1. Create a GitHub environment called `production` in repo settings.
2. Add GitHub Actions secrets:
   - `AWS_ROLE_ARN` (role with ECS + ECR permissions, via OIDC)
   - `AWS_REGION`
3. Push `main`. The `deploy.yml` workflow builds + pushes + updates.

First deploy will fail until:
- RDS has run migrations → `alembic upgrade head` against the control
  plane (do this from a temporary bastion EC2 or via a one-off ECS
  task).
- At least one tenant has been provisioned (run
  `python scripts/create_tenant.py` pointed at the production DB).

## Step 6 — Frontend deploy

The frontend is static (SvelteKit adapter-static) and goes to S3 +
CloudFront. Two options:

- **GitHub Pages** (cheapest, what the repo is set up for today).
  Point your custom subdomain (e.g. `app.feohledger.com`) at Pages.
  Fine for pre-revenue.
- **S3 + CloudFront via Terraform** (production-appropriate). The
  Terraform already provisions the bucket + distribution; update the
  deploy workflow to `aws s3 sync ./build s3://<bucket>` instead of
  pushing to `gh-pages`.

Migrate from Pages → CloudFront the week before your first customer
deploy. Easier to switch DNS before a customer is actively using it.

## Step 7 — First production smoke test

From your laptop, against the production URL:
1. Hit `GET /api/health` — should return `{"status": "ok"}`
2. Create a test tenant via `scripts/create_tenant.py`
3. Log in to the test tenant at
   `https://<tenant-slug>.app.feohledger.com`
4. Upload a test invoice → watch extraction complete in the UI
5. Check CloudWatch Logs for the backend service — errors should be
   zero
6. Run `python scripts/access_review.py > /tmp/access.csv` to prove
   the SOC 2 control still works in prod

## Ongoing

- **Backups verified** — restore a snapshot to a sandbox DB quarterly.
  See `docs/backup-disaster-recovery.md`.
- **Secrets rotation** — 90-day cadence. See
  `docs/secrets-rotation.md`.
- **CloudWatch alarms** — at minimum, alarm on 5xx rate + RDS CPU.

## Checklist

- [ ] AWS prod account created
- [ ] Domain in Route53, ACM cert validated
- [ ] SOPS secrets populated
- [ ] `terraform apply` clean
- [ ] GitHub Actions deploy workflow green
- [ ] First tenant provisioned
- [ ] Smoke test passes
- [ ] CloudWatch alarms wired to PagerDuty / email

Time: ~1 week of focused work. Cost: ~$150–300/mo at low traffic
(RDS t4g.small, ECS Fargate 0.5 vCPU, CloudFront, Route53).
