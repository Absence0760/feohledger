# Production Deployment

## Overview

This document describes the recommended AWS deployment architecture for production. The core principle is to run the FastAPI API as a long-lived container and offload async, bursty workloads to Lambda functions.

> This is the **engineering reference** — architecture, deployment targets, and the rationale behind each choice. For the step-by-step founder checklist (AWS account, domain, SOPS, terraform apply, smoke test), see [`docs/founder-runbooks/production-deployment.md`](founder-runbooks/production-deployment.md).

```
                         CloudFront
                            │
                  ┌─────────┴──────────┐
                  │                    │
            S3 (Frontend)      ALB (API)
                               │
                          ECS / Fargate
                          (FastAPI API)
                               │
              ┌────────────────┼────────────────┐
              │                │                │
         RDS Postgres     ElastiCache       S3 (Files)
         (multi-tenant)    (Redis 7)
                               │
                          SQS Queues
                    ┌──────┬───┴───┬──────┐
                    │      │       │      │
                 Extract  ERP   Audit   (future)
                 Lambda  Lambda Lambda
```

## Service Deployment Targets

| Service | Deploy As | Trigger | Why |
|---|---|---|---|
| FastAPI API | ECS/Fargate | ALB | Connection pooling, low latency, multi-tenant DB routing |
| Frontend | S3 + CloudFront | HTTP | Static SPA, global CDN caching |
| Invoice Extraction | Lambda | SQS | Bursty, CPU-heavy, scales to zero |
| ERP Integration | Lambda | SQS | Async retries, external API isolation, DLQ support |
| Audit Logging | Lambda | EventBridge/SNS | Fire-and-forget, decoupled from request path |

## Container: FastAPI API (ECS/Fargate)

The main API should run as a container behind an Application Load Balancer.

**Why not Lambda:**
- Maintains persistent connection pools to multiple tenant databases
- Multi-tenant session routing (`X-Tenant-Slug` header) benefits from warm, long-lived processes
- Synchronous CRUD endpoints need predictable low latency (no cold starts)
- Simpler to manage CORS, middleware, and dependency injection in a long-running process

**Recommended configuration:**
- ECS service with Fargate launch type
- Minimum 2 tasks across 2 AZs for availability
- ALB health check on `GET /api/health`
- Auto-scaling based on CPU/request count
- Uses the existing `Dockerfile` (`python:3.14-slim`, pinned by digest, + `uvicorn`)

## Lambda: Invoice Extraction

Already implemented in `backend/app/services/extraction_lambda.py`.

**Handler:** `app.services.extraction_lambda.handler`

**How it works:**
1. API receives invoice upload and writes the file to S3
2. When `extraction_mode = "lambda"`, `extraction_dispatch.py` sends a message to SQS
3. Lambda picks up the message, runs extraction, and writes results back to the tenant DB
4. Configurable via `extraction_mode` and `sqs_extraction_queue_url` in `app/config.py`

**Recommended configuration:**
- Runtime: Python 3.12
- Memory: 512 MB+ (extraction is CPU-bound)
- Timeout: 120s
- Concurrency: reserve based on expected upload volume
- DLQ for failed extractions
- SQS trigger with batch size of 1

## Lambda: ERP Integration

Implemented in `backend/app/services/erp_lambda.py`.

**Handler:** `app.services.erp_lambda.handler`

**Why Lambda:**
- ERP calls are async and already fire-and-forget from the user's perspective
- External API calls can be slow/unreliable — Lambda isolates this from the API
- SQS + DLQ gives automatic retry with exponential backoff
- Failed sends land in a DLQ for inspection instead of silently dropping

**How it works:** SQS-triggered (`erp_mode = "lambda"`), each message carries
`{ invoice_id, org_id, actor_id }`; the handler reuses the ERP send logic and
writes status back to the tenant DB. Deployed by the `lambdas` job in
`aws-deploy.yml` — see *CI/CD: gated production deploy*.

## Lambda: Audit Logging

Implemented in `backend/app/services/audit_lambda.py`.

**Handler:** `app.services.audit_lambda.handler`

**Why Lambda:**
- Audit writes should never block or slow the request path
- Scales independently from the API

**How it works:** SQS-triggered (`audit_mode = "lambda"`), each message carries
the audit event (`correlation_id`, `organization_id`, `actor_id`, `action`,
`entity_*`, `details`, `tenant_db_name`); the handler writes the row to the
named tenant DB. Deployed by the `lambdas` job in `aws-deploy.yml`.

## Managed Services Mapping

| Dev (Docker Compose) | Production (AWS) |
|---|---|
| PostgreSQL 16 container | RDS PostgreSQL (Multi-AZ) |
| Redis 7 container | ElastiCache Redis |
| MinIO container | S3 |
| localhost | Route 53 + CloudFront + ALB |

## Environment Configuration

The FastAPI app uses `pydantic-settings` (`backend/app/config.py`) with the `FEOH_` prefix. **Every variable below MUST be prefixed with `FEOH_`** — pydantic-settings ignores unprefixed names and silently falls back to defaults.

```env
# Database
FEOH_DATABASE_URL=postgresql+asyncpg://user:pass@rds-host:5432/feohledger
FEOH_TENANT_DB_PREFIX=feoh_

# Auth — REQUIRED in production
FEOH_SECRET_KEY=<generate via `openssl rand -hex 32`>
FEOH_MFA_ENABLED=true

# S3 (use real S3, not MinIO)
FEOH_S3_BUCKET=your-invoice-bucket
# Set the endpoint + static keys EMPTY to use real AWS S3 via the ambient
# credential chain (task/instance role). The committed defaults point at
# local MinIO, so merely omitting the vars keeps the localhost endpoint.
FEOH_S3_ENDPOINT_URL=
FEOH_S3_ACCESS_KEY=
FEOH_S3_SECRET_KEY=

# Dispatch — async via SQS in production
FEOH_EXTRACTION_MODE=lambda
FEOH_SQS_EXTRACTION_QUEUE_URL=https://sqs.region.amazonaws.com/account/extraction-queue
FEOH_ERP_MODE=lambda
FEOH_SQS_ERP_QUEUE_URL=https://sqs.region.amazonaws.com/account/erp-queue
FEOH_AUDIT_MODE=lambda
FEOH_SQS_AUDIT_QUEUE_URL=https://sqs.region.amazonaws.com/account/audit-queue

# Redis — required for auth blocklist, MFA, SSO state
FEOH_REDIS_URL=redis://elasticache-host:6379

# Email + signup
FEOH_EMAIL_PROVIDER=ses
FEOH_EMAIL_FROM=no-reply@feohledger.com
FEOH_PUBLIC_URL=https://feohledger.com
FEOH_TENANT_URL_TEMPLATE=https://{slug}.feohledger.com
# Self-service signup: keep it on (the default) for a public SaaS, and set
# both hCaptcha keys — the API refuses to boot without the secret while signup
# is on. For an invite-only deployment whose tenants are provisioned by an
# operator, set FEOH_SIGNUP_ENABLED=false instead and omit both keys.
FEOH_SIGNUP_ENABLED=true
FEOH_HCAPTCHA_SECRET=<from hCaptcha dashboard>
FEOH_HCAPTCHA_SITEKEY=<from hCaptcha dashboard>
```

See `docs/environment.md` for the full var list including extraction, ERP, and card platform keys.

## Network Architecture

- **VPC** with public and private subnets across 2+ AZs
- **ALB** in public subnets, ECS tasks in private subnets
- **RDS and ElastiCache** in private subnets (no public access)
- **Lambda functions** in private subnets with NAT Gateway for outbound access
- **S3** accessed via VPC endpoint (no NAT needed)
- **SQS/EventBridge** accessed via VPC endpoints

## CI/CD: gated production deploy

Two workflows fire on `release: published`, and **nothing else** — production
deploys happen only through a published release, never an ad-hoc manual run.
Every job in both is gated by the `production` GitHub Environment:

`.github/workflows/aws-deploy.yml` deploys the web stack to AWS:

| Job | Target | Steps |
|---|---|---|
| `frontend` | S3 + CloudFront | `pnpm build` → `aws s3 sync --delete` → CloudFront invalidation |
| `backend` | ECS / Fargate | `docker build backend` → push to ECR (commit-SHA tag) → register a new task-definition revision with that image → **run DB migrations** (one-off Fargate task) → `update-service` → wait for stable |
| `lambdas` | Lambda (extraction / ERP / audit) | `needs: backend` → point every function in `LAMBDA_FUNCTIONS` at the **same** image the backend job just pushed (`update-function-code --image-uri`) → wait for update |

`.github/workflows/mobile-release.yml` builds the mobile artifacts:

| Job | Target | Steps |
|---|---|---|
| `mobile-android` | APK artifact | `flutter build apk --release` → upload artifact |
| `mobile-ios` | iOS build | `flutter build ios --release --no-codesign` |

> The web frontend's old **GitHub Pages** deploy has been **retired** — AWS
> S3 + CloudFront is now the single production frontend. (Pages could never use
> this `production` environment anyway: a Pages deploy is hard-wired to the
> `github-pages` environment.)

### Production gate

Every job declares `environment: production`, so the protection rules on that
GitHub Environment (Settings → Environments → `production`) apply: **GitHub
pauses the job for the required reviewer's approval before any deploy step
runs**. Add your reviewer / wait-timer / branch rules there.

Note on granularity: when one release triggers several jobs that share the
`production` environment, a single reviewer approval releases all of them in
that run. If you need to approve services **independently** (e.g. ship the
backend but hold the frontend), give them separate environments
(`production-backend`, `production-frontend`, …), each with its own rules.

### Database migrations

The `backend` job runs `alembic upgrade head && python scripts/migrate_all_tenants.py`
as a one-off Fargate task on the freshly-registered task definition **before**
rolling the service, so new code never serves against an un-migrated schema and
the revision fans out to **every tenant DB** (not just the control plane). A
non-zero exit fails the job and the service is never rolled. This needs the
task's network placement — `ECS_SUBNETS` + `ECS_SECURITY_GROUPS` below.

### Lambda workers (extraction / ERP / audit)

The three async workers — `app.services.extraction_lambda.handler`,
`erp_lambda.handler`, `audit_lambda.handler` — run the **same container image**
as the API. The `lambdas` job `needs: backend`, so it reuses the exact image
the backend job already built and pushed (one build, many runtimes) and runs
only after migrations succeed. Each function is provisioned in Terraform with
its own `ImageConfig.Command` set to its handler; deploying is just
`update-function-code --image-uri <image>` for every name in
`LAMBDA_FUNCTIONS`. Add the next worker to that list and it ships gated too.

> **Container-Lambda packaging:** a container image needs the Lambda Runtime
> Interface Client (`awslambdaric`) to run on Lambda. It is **bundled** in the
> backend image (a runtime dependency in `pyproject.toml`, hash-pinned in
> `requirements.lock`, cp314 manylinux wheel — no compiler added). The ECS
> entrypoint stays `uvicorn`; each Lambda function's `ImageConfig` overrides
> the entrypoint to `python -m awslambdaric` + its handler. So the only
> remaining infra step is provisioning the functions with that `ImageConfig`
> (Terraform) — no second image, no zip. (Zip packaging was rejected: native-dep
> cross-compilation + the 250 MB unzipped limit.)

### Credentials — OIDC, no static keys

The jobs assume an AWS role via GitHub OIDC (`id-token: write`); there are no
long-lived access keys in the repo. Create a deploy role whose trust policy is
scoped to this repository **and** the `production` environment
(`token.actions.githubusercontent.com:sub` like
`repo:<owner>/<repo>:environment:production`), with least-privilege permissions
for ECR push, ECS register/update + `run-task`/`describe-tasks` (the migration
task), `iam:PassRole` for the task + execution roles, `lambda:UpdateFunctionCode`
on the worker functions, S3 sync, and CloudFront invalidation. Store its ARN as
the `AWS_DEPLOY_ROLE_ARN` **environment secret** on `production`.

**Status:** the role exists. The estate account bootstrap created
`feohledger-deploy` in the FeohLedger AWS account with exactly that trust policy
(this repo + the `production` environment) and **no permissions**. Its
least-privilege policy lands with the workload stack it has to name — tracked in
`docs/followups.md`.

### Required configuration

| Name | Kind | Scope | Purpose |
|---|---|---|---|
| `AWS_DEPLOY_ENABLED` | variable | **repository** | Kill switch — must equal `true` to arm either job. Repository-scoped (not environment) so it is readable in the job-level `if`, which is evaluated before the environment gate. |
| `AWS_DEPLOY_ROLE_ARN` | secret | environment | OIDC role the jobs assume. |
| `AWS_REGION` | variable | environment | Target region. |
| `ECR_REPOSITORY` | variable | environment | ECR repo name for the API image. |
| `ECS_CLUSTER` / `ECS_SERVICE` | variable | environment | ECS cluster + service to roll. |
| `ECS_TASK_FAMILY` | variable | environment | Task-definition family to re-register. |
| `ECS_CONTAINER_NAME` | variable | environment | Container name within the task def to swap the image on + target for the migration command override. |
| `ECS_SUBNETS` | variable | environment | Comma-separated private subnet IDs for the one-off migration task (e.g. `subnet-a,subnet-b`). |
| `ECS_SECURITY_GROUPS` | variable | environment | Comma-separated security-group IDs for the migration task (needs DB egress). |
| `FRONTEND_BUCKET` | variable | environment | S3 bucket serving the SPA. |
| `CLOUDFRONT_DISTRIBUTION_ID` | variable | environment | Distribution to invalidate. |
| `LAMBDA_FUNCTIONS` | variable | environment | Comma/space-separated Lambda function names for the async workers (extraction / ERP / audit). Each is updated to the new image. |
| `PUBLIC_API_URL` | variable | environment | API base URL — baked into the frontend build; also the backend job's deploy URL. |
| `APP_URL` | variable | environment | Public site origin (e.g. `https://feohledger.com`) — the frontend job's deploy URL, and baked into the frontend build as `PUBLIC_SITE_URL` (trailing slash stripped), the origin of the link-preview card's absolute `og:image` URL. **Required**: the Build step fails when it is empty, because SvelteKit would otherwise ship a root-relative image URL silently. See [environment.md](environment.md) § Frontend. |

### Arming the AWS pipeline

`aws-deploy.yml` is committed as a **scaffold** and stays inert until armed, so
a release won't turn it red before the infrastructure exists. To go live:

1. Build the AWS infra in `infra/` (ECR, ECS cluster/service/task family,
   frontend S3 bucket, CloudFront distribution, the three worker Lambda
   functions + their SQS triggers) — the stack today is the KMS key, the S3
   buckets and the us-east-1 ACM certificate for the platform domain
   (`infra/acm.tf`, output `platform_certificate_arn`), which the distribution
   attaches.
2. Provision each worker function with its `ImageConfig` — entrypoint
   `python -m awslambdaric`, command its handler (see *Lambda workers* above).
   The RIC is already bundled in the image, so nothing else is needed here.
3. Attach the deploy role's least-privilege policy (the role itself already
   exists — see *Credentials*) and store `AWS_DEPLOY_ROLE_ARN`.
4. Set the environment variables above on the `production` environment and the
   protection rules (reviewer / wait timer).
5. Set the repository variable `AWS_DEPLOY_ENABLED=true`.

Until step 5, the AWS jobs (`backend`, `lambdas`, `frontend`) skip via the
kill switch — `backend`/`frontend` on their `if:` guard, `lambdas` because it
`needs: backend`. (The `mobile-release.yml` jobs have no such guard — they
build on every release and are gated only by the `production` reviewer.)
