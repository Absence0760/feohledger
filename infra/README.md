# infra/

Infrastructure-as-code for the FeohLedger AWS account: the **security substrate** needed as a SOC 2 engineering prerequisite (see `../docs/soc2-readiness.md`); the platform domain's registration settings and TLS certificate, its mail DNS (Migadu mailboxes and the SES sending identity) and a monthly cost budget; and the **workload stack** the app runs on — one VM plus an RDS database (`../docs/minimal-deployment.md`, `../docs/decisions.md` §254), with the static SPA on S3 + CloudFront (§ Frontend on CloudFront, §278). The rest of the scale-up architecture (ECS, ALB) is `../docs/production-deployment.md` and is not built.

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
├── monitoring.tf                # SNS alerts (email) + pager (SMS) topics + VM recovery/reboot and DB CPU/memory/storage alarms
├── uptime.tf                    # Route 53 HTTPS checks on the apex, a tenant host and api./api/health, their paging alarms, ACM expiry alarm
├── deploy.tf                    # release deploys: the SSM command document that runs deploy.sh --ref, + the deploy role's grant to run it
├── frontend.tf                  # the SPA: private S3 bucket + CloudFront (OAC, SPA router, headers, logs, alarms) + the deploy role's publish grant
├── cloudfront/spa-router.js     # the CloudFront Function: built files pass through, routes become /index.html
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
| Server-access logging on every data bucket | `s3.tf` — delivered to the access-logs sink, whose bucket policy grants `logging.s3.amazonaws.com` `s3:PutObject`, pinned to this account and the four source buckets (the SPA bucket included); CloudFront access logs arrive through `delivery.logs.amazonaws.com`, under `cloudfront/` only; ACLs disabled |
| Public access block on every bucket | `s3.tf`, `frontend.tf` — all four flags true; the SPA bucket is read only by its CloudFront distribution (OAC) |
| Lifecycle cost guards | `s3.tf` — backups bucket expires dumps after `backup_retention_days` (90d default; deliberately NO Object Lock — the lifecycle IS the retention policy), and every lifecycle rule reaps incomplete multipart uploads after 7 days |

### The deploy role is not owned here

`feohledger-deploy` — the role `.github/workflows/vm-deploy.yml` (and the
disarmed `aws-deploy.yml`) assume through OIDC, whose ARN is the
`AWS_DEPLOY_ROLE_ARN` environment secret — is **not in this module and must not
be added to it.** It is minted by the estate account
bootstrap (`~/github/templates/infra/modules/project-baseline`, driven by
`~/github/templates/scripts/new-project-account.sh`), its inputs live in
`Absence0760/infra-secrets` → `feohledger/bootstrap.tfvars`, and its state is in
the mgmt account's bucket. Change the trust policy there, never here — the
module is shared by every project account, so fixing it locally would fix this
account and leave the next one broken.

What the role may **do** is the project's, by the module's own design: it mints
the role with a trust policy and no permissions ("Project repo attaches its own
policies"). This module attaches one inline policy to it, by name —
`vm-deploy`, in `deploy.tf` (§ Release deploys). It references the role through
a `data "aws_iam_role"` lookup, so the role, its trust policy and its lifetime
stay the baseline's. If the baseline ever starts managing the role's inline
policies exclusively (`aws_iam_role_policies_exclusive`), it would strip this
one on its next apply — move the grant there at that point rather than letting
the two fight.

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
- **The database is private.** Private subnets with no internet route, reachable only from the VM's security group, encrypted with the app key, TLS forced (`rds.force_ssl`), deletion-protected, IAM database authentication enabled — the app signs in as the `feoh_app` role with a per-connection token, and the VM role's `rds-db:connect` names that one user on this one instance (`compute.tf`, `../docs/decisions.md` §310); the master keeps password login for break-glass only — and restorable to any point in the last `db_backup_retention_days` (default 7). The nightly logical dumps (`../deploy/backup.sh`) keep 90 days beyond that.
- **DNS.** `api.` points at the VM's Elastic IP. The apex and `*.` (every tenant) do too until `frontend_on_cloudfront` moves them to the SPA's CloudFront distribution (§ Frontend on CloudFront).
- **Alarms** go to `alert_emails` through SNS — each address must click AWS's confirmation link once. The VM recovers itself onto new hardware on a host failure and reboots on a guest failure. Database storage running out also texts `alert_phone_number`, as do the external uptime checks (§ Uptime checks and paging).

**The database password never enters Terraform state.** `db_master_password` is an *ephemeral* variable feeding RDS's *write-only* `password_wo`, so it is sent to AWS and stored nowhere else — not in state, not in a plan file. That is why this module does not read it through a `sops_file` data source (§ Secrets): a data source's value is written to state in plaintext. The operator supplies it at apply time from the private `infra-secrets` repo, as an environment variable on the one command.

**First apply — operator steps.** Steps 2–3 touch a secret, so they are yours to run; nothing here prints it.

1. Add `alert_emails = ["…"]` to `prod.tfvars` (it has no default — the repo is public).
2. Create the master password once in `infra-secrets` (a 48-hex-character value is URL-safe, which it must be because the break-glass `FEOH_DATABASE_URL` embeds it): `cd ~/github/infra-secrets && AWS_PROFILE=feohledger sops feohledger/terraform.sops.yaml`, and add a `db_master_password:` key set to the output of `openssl rand -hex 24`.
3. Plan and apply with the password passed in from sops (one line each):
   `cd ~/github/feohledger/infra && AWS_PROFILE=feohledger TF_VAR_db_master_password="$(AWS_PROFILE=feohledger sops -d --extract '["db_master_password"]' ../../infra-secrets/feohledger/terraform.sops.yaml)" terraform plan -var-file=../../infra-secrets/feohledger/prod.tfvars -out=tfplan`
   then `AWS_PROFILE=feohledger TF_VAR_db_master_password="$(AWS_PROFILE=feohledger sops -d --extract '["db_master_password"]' ../../infra-secrets/feohledger/terraform.sops.yaml)" terraform apply tfplan`.
   An ephemeral variable must be supplied again at apply time even from a saved plan — that is the reason it appears twice. `AWS_PROFILE` is set inside the `$( … )` too: a prefix assignment reaches only the command it precedes, so without it the substitution decrypts with no profile.
   If `plan` reports the cached provider "does not match any of the checksums recorded in the dependency lock file", re-run `terraform init -backend-config=backend.config` and keep the `h1:` line it adds until the apply is done — restoring the lock first brings the error straight back. Then `git restore .terraform.lock.hcl`, so one workstation's hash never lands in a commit.
4. Confirm the SNS subscription email AWS sends to each alert address.
5. Then the VM side: `../deploy/README.md` (bootstrap, the sops env with `FEOH_DATABASE_URL` pointing at the `db_address` output, first deploy, then § Database role to move the app onto `feoh_app`).

**Rotating the master password**: put the new value in `terraform.sops.yaml`, bump `db_master_password_version` in `prod.tfvars`, and apply as in step 3. Once the app is on `feoh_app` (`../deploy/README.md` § Database role) nothing on the VM holds the master password, so the app keeps running through it. Before that cutover the password was inside `FEOH_DATABASE_URL` in `prod.sops.yaml` too, and had to be changed there and redeployed in the same sitting. `../docs/secrets-rotation.md` § RDS master password.

## Release deploys

Publishing a GitHub release deploys it: `.github/workflows/vm-deploy.yml`
assumes `feohledger-deploy`, waits for the `production` environment's required
reviewer, and sends SSM Run Command to the VM, which runs
`deploy/deploy.sh --ref <the release's commit>` (`../deploy/README.md`
§ Deploys, `../docs/decisions.md` §275). `deploy.tf` holds the two AWS pieces:

- **`aws_ssm_document.deploy`** (`feohledger-vm-deploy`, output
  `vm_deploy_document_name`) — runs exactly
  `sudo -iu ec2-user /home/ec2-user/feohledger/deploy/deploy.sh --ref '<Ref>'`,
  logs each run to its own file under `/var/log/feoh-deploy/` (kept 90 days)
  and prints that log's tail. Its one
  parameter, `Ref`, has `allowedPattern` `^[0-9a-f]{40}$`, so SSM refuses
  anything but a full commit sha before a shell exists.
- **`aws_iam_role_policy.deploy_vm`** on the deploy role — `ssm:SendCommand` on
  the app instance's ARN **and** that document's ARN (SendCommand is authorized
  against both, so `AWS-RunShellScript` and every other document stay out of
  reach, as does every other instance), plus `ssm:GetCommandInvocation` on
  `*` to poll the result: SSM defines no resource type for it, it is read-only,
  and the only command output in this account is deploy logs.
  `ssm:ListCommandInvocations` is deliberately not granted — the workflow knows
  its own command id.

**One-time operator steps**, in order:

1. **Plan and apply** as in § Workload stack step 3 (the database password has
   to be passed again — it is an ephemeral variable). The plan should add
   exactly `aws_ssm_document.deploy` and `aws_iam_role_policy.deploy_vm`.
2. **Update the VM's own `deploy.sh` once by hand**, so it understands `--ref`:
   `aws ssm start-session --target <app_instance_id> --profile feohledger`, then
   `sudo -iu ec2-user /home/ec2-user/feohledger/deploy/deploy.sh`. Until this
   runs, the VM's checkout predates `--ref` and an automated deploy fails at
   argument parsing (harmlessly — nothing is touched).
3. **Give the workflow the instance id**, as a `production` environment secret
   (secret, not variable: this repo's workflow logs are public, and secrets are
   masked in them):
   `gh secret set VM_INSTANCE_ID --env production --repo Absence0760/feohledger --body "$(AWS_PROFILE=feohledger terraform -chdir=infra output -raw app_instance_id)"`
4. **Prove the OIDC handshake** (issue #449): run **AWS OIDC preflight**
   (`gh workflow run aws-oidc-preflight.yml --repo Absence0760/feohledger`) and
   approve it. If it fails, the trust policy's subject is wrong — fix it in the
   baseline module (§ The deploy role is not owned here), not here.
5. **Arm the workflow**:
   `gh variable set VM_DEPLOY_ENABLED --repo Absence0760/feohledger --body true`.
   Until then a release skips the deploy job rather than failing it.
6. **Publish a release** (or run `Deploy to the VM` by hand from `main`) and
   approve it; the run's log ends with the tail of `deploy.sh`'s output.

## Frontend on CloudFront

`frontend.tf` serves the SPA from a private S3 bucket through CloudFront
(issue #582, `../docs/decisions.md` §278); the API stays on the VM. What it
creates:

- **The bucket** (`frontend_bucket` output; name derived from the account id,
  so no tfvars entry): versioned, SSE-S3, every public-access flag on, server
  access logs to the sink. The only reader is CloudFront, through Origin Access
  Control and pinned to this distribution's ARN. It holds `s3:ListBucket` as
  well as `s3:GetObject`, so a missing key is a real 404 instead of a 403.
  Replaced versions expire after 90 days.
- **The distribution** (`frontend_distribution_id`, `frontend_distribution_domain`):
  `feohledger.com` + `*.feohledger.com` on the existing ACM certificate, TLS
  1.2+, HTTP/2 + HTTP/3, IPv6, `PriceClass_100`. A cache policy honours each
  object's own `Cache-Control` (a minimum TTL of 0, which the managed
  CachingOptimized policy's 1 s minimum would override). A response-headers
  policy sets the same four headers Caddy sets, and still no CSP.
- **The SPA router**, a CloudFront Function on viewer-request
  (`cloudfront/spa-router.js`): built files pass through and everything else
  becomes `/index.html`. It names the files rather than testing for an
  extension, because a route can contain a dot (`/invoices/INV-2026.001`).
  `frontend/src/lib/deploy/spaRouter.test.ts` runs it against `frontend/static/`.
- **Access logs** through CloudWatch log delivery into the access-logs sink under
  `cloudfront/`. Not the legacy `logging_config`, which needs bucket ACLs the
  sink disables.
- **Alarms**: 5xx above 5% for 10 minutes and 404s above 10% for 15 minutes, to
  the alerts topic. The router means a route never 404s, so a 404 surge means a
  publish left referenced files out. It watches 404, not 4xx, because the
  distribution allows only GET/HEAD and answers any other method with 403: a
  vulnerability scanner POSTing login and WordPress paths once tripped the old
  4xx alarm with no deploy and no failed real request. `404ErrorRate` is a
  CloudFront additional metric, so `aws_cloudfront_monitoring_subscription.frontend`
  turns those on for this distribution (up to 8 metrics at the CloudWatch
  custom-metric rate, $0.30 each, so at most ~$2.40/month).
- **The deploy role's grant** (`aws_iam_role_policy.deploy_frontend`): list,
  put, delete and abort-multipart on that bucket, and create/get invalidation plus
  get-distribution on that distribution. Nothing else, and no `GetObject`.
- **DNS**: nothing moves until `frontend_on_cloudfront = true`. The apex and
  `*.` A records then become aliases of the distribution, AAAA aliases appear,
  and `api.` stays on the Elastic IP (`compute.tf` § DNS). The apex's Migadu
  MX/TXT records are separate and untouched.

The whole stack refuses an `aws_region` other than `us-east-1`. The certificate,
the CloudFront metrics and the log-delivery source live there, and the alarms
must publish to an SNS topic in the same region.

**Cost**: CloudFront's always-free allowance (1 TB out and 10M requests a month)
covers pilot traffic. S3 storage for a few builds and the alarms are cents, so
the $75 budget is unaffected.

**Operator steps** (cut-over plan in #582). The one-line commands assume you
are in the repo root with `AWS_PROFILE=feohledger`.

1. **Apply with DNS unchanged.** Plan and apply as in § Workload stack step 3.
   The plan should add the `frontend.tf` resources and change only the
   access-logs bucket policy (the SPA bucket as a log source, and CloudFront log delivery). It should not touch a Route 53
   record.
2. **Give the workflow its targets** as `production` environment secrets. The
   bucket name embeds the account id and this repo's run logs are public:
   `gh secret set FRONTEND_BUCKET --env production --repo Absence0760/feohledger --body "$(terraform -chdir=infra output -raw frontend_bucket)"`
   and
   `gh secret set CLOUDFRONT_DISTRIBUTION_ID --env production --repo Absence0760/feohledger --body "$(terraform -chdir=infra output -raw frontend_distribution_id)"`.
3. **Set the two build variables** as repository variables (the SPA builds in
   an ungated job, which can't read environment variables):
   `gh variable set APP_URL --repo Absence0760/feohledger --body https://feohledger.com`
   and `gh variable set PUBLIC_API_URL --repo Absence0760/feohledger --body https://api.feohledger.com`.
4. **Arm the publish**:
   `gh variable set FRONTEND_PUBLISH_ENABLED --repo Absence0760/feohledger --body true`.
5. **Publish a release** and approve it. After the VM step the job uploads the
   build, waits for the invalidation, and smoke-checks the distribution's
   `*.cloudfront.net` name: this build's `version.json`, the SPA on `/` and on a
   dotted deep link, a 404 for a missing asset, and HSTS.
   `deploy/smoke-frontend.sh` runs the same checks by hand.
6. **Try a tenant flow on the CloudFront hostname** before moving DNS. The
   `*.cloudfront.net` name carries no tenant, so the app shows the marketing
   page there. Sign-in is checked properly after step 7, on `sandbox.`.
7. **Cut over**: `frontend_on_cloudfront = true` in `prod.tfvars`, then plan and
   apply. The plan should change `aws_route53_record.apex` and `.tenants` from
   the IP to an alias, and add two `spa_ipv6` records. Nothing else. The next
   release's smoke check then covers the apex and a tenant host too: it notices
   CloudFront's `x-amz-cf-id` header on the apex.
8. **Watch for a day**, then remove the SPA from Caddy, `deploy.sh` and
   `add-tenant.sh` (the step-4 follow-up in `../docs/followups.md`). Do it within
   about 30 days: Caddy can no longer renew the apex and tenant certificates
   once DNS points elsewhere.

**Rollback**: set `frontend_on_cloudfront = false` and apply. The records swap
back to the VM, whose Caddy still serves the SPA until step 8. For a bad
*build*, rather than bad infrastructure, see `../deploy/README.md` § Frontend
rollback.

## Uptime checks and paging

`uptime.tf` checks the product from outside, and the pager topic in
`monitoring.tf` texts the operator's phone when it fails (`../docs/decisions.md`
§311). The alarms above all watch AWS's view from inside the account. None of
them notices a wrong DNS record, a Caddy that stopped proxying, or an API
container that exited on a healthy VM.

**What it creates**

- **Route 53 HTTPS health checks** (with SNI) from three regions (`us-east-1`,
  `us-west-2`, `eu-west-1`) every 30 seconds. An endpoint reads unhealthy after 3
  failures in a row, about 90 seconds:
  - `https://<domain>/`, the apex SPA;
  - `https://api.<domain>/api/health`, matched on the body `"status":"ok"`, so a
    200 from anything else (the SPA router answering that path with
    `index.html`, a parked domain) fails. `backend/tests/test_sweep_health.py`
    pins the string against what the endpoint really sends;
  - `https://<uptime_tenant_slug>.<domain>/`, one tenant host: the wildcard
    record, the wildcard SAN and, before the CloudFront cutover, that tenant's
    Caddy block. Skipped while `uptime_tenant_slug` is unset.
- **One alarm per check** (`<project>-<env>-uptime-<apex|api|tenant>`), in
  us-east-1 because Route 53 publishes health-check metrics only there. It goes
  to the pager topic (text) **and** the alerts topic (email), on failure and on
  recovery. Missing data counts as a failure, so expect one page right after the
  first apply, before Route 53's first datapoint.
- **The pager topic** (`<project>-<env>-pager`, us-east-1) and, when
  `alert_phone_number` is set, its SMS subscription, the account's SMS type set
  to Transactional, and two email alarms: a text SNS failed to deliver
  (`pager-sms-failed`), and this month's SMS spend reaching 80% of the
  account's SMS spend limit (`pager-sms-spend`), past which SNS stops texting.
- **Which alarms page:** the uptime checks and `db-storage-low` (past it writes
  fail, and no uptime check notices first). Everything else stays email-only:
  CPU, memory and CPU credits are sizing and cost signals, and a failed VM status
  check or a CloudFront error surge that customers can see already fails an
  uptime check, so paging those too would send two or three texts per incident.
- **Certificate expiry** (`<project>-<env>-certificate-expiry`, email): the ACM
  certificate's `DaysToExpiry` under 30. Route 53 checks complete the TLS
  handshake but don't validate the certificate, so an expired one would pass
  them. ACM renews from 60 days out on its own, so this only fires when that
  renewal has failed. The VM's own Let's Encrypt certificates (Caddy, on `api.`)
  are not covered yet: `../docs/followups.md`.

**Cost**: about **$8–13 a month**. Each HTTPS check is $0.50–0.75 a month plus
$1–2 for HTTPS and, on the API, $1–2 for the string match. The lower figures are
AWS's rates for an AWS endpoint, and these checks may bill at the non-AWS rate
since they target hostnames. That comes to $5.50–10.25 for three checks. The
toll-free number is $2 a month, each text about a cent (base fee plus carrier
fee, `aws.amazon.com/end-user-messaging/pricing`), and up to six new alarms
about $0.60. The 10-second fast interval and latency graphs are off: each adds $1–2
per check. Fewer regions don't lower the price, which is per check, but they
cut the requests the checkers make against CloudFront and the API. The $75
budget still covers the stack plus this.

**Phone delivery, before you set `alert_phone_number`** (US numbers; these are
console steps, not Terraform). New accounts are in the SNS **SMS sandbox**,
which texts only verified numbers (up to 10). The operator is the only
recipient, so the sandbox is fine to stay in. US carriers also refuse SMS
without a registered **origination identity**. A toll-free number is the
cheapest: $2 a month, registration in about 15 days.

1. **Request a toll-free number** in the AWS End User Messaging SMS console, in
   **us-east-1** (a number belongs to one region, and the pager topic is
   there): SMS capability, message type Transactional. Requesting it opens its
   registration form.
2. **Complete the registration.** Business type *Sole proprietor* (no EIN
   needed), website `https://feohledger.com`, privacy and terms URLs
   `https://feohledger.com/legal/privacy` and `https://feohledger.com/legal/terms`
   (required on registrations since 2026-09-15), use case *Account
   notifications*, with details along the lines of "infrastructure alerts sent
   only to the business owner's own phone". Message sample:
   `ALARM: "feohledger-prod-uptime-api" in US East (N. Virginia)`. The form also
   asks for a street address and contact phone. Those go to AWS and the
   carriers, not to any published page. The number sends nothing until the
   registration reads **Complete**.
3. **Let SNS use the number.** AWS's registration docs require a resource
   policy on it for SNS: on the number's *Resource policy* tab, allow
   `sns.amazonaws.com` `sms-voice:SendTextMessage` on that number, conditioned
   on `aws:SourceAccount` = this account
   (`docs.aws.amazon.com/sms-voice/latest/userguide/shared-resources.html`).
4. **Verify the destination number** in the SNS console, us-east-1 → Text
   messaging (SMS) → Sandbox destination phone numbers → Add phone number, then
   enter the code it texts.
5. The SMS spend limit for a new account is $1 a month, about a hundred texts.
   That covers paging; `pager-sms-spend` emails at 80% of it, in time to raise
   it in the SNS console's text messaging preferences.

**Operator steps**

1. **Apply the checks** (they need no phone): add `uptime_tenant_slug` (a
   tenant you control, such as the test tenant) to `prod.tfvars`, then plan and
   apply as in § Workload stack step 3. The plan should add the health checks
   (two, or three with the slug), one `uptime` alarm per check,
   `platform_certificate_expiry` and `aws_sns_topic.pager`. It should change
   only `db_storage` (its actions gain the pager topic) and destroy nothing.
   Each alarm emails ALARM, then OK, within a few minutes of the apply.
2. **Add the phone** once the toll-free registration is Complete and the
   number is verified: `alert_phone_number = "+1…"` in `prod.tfvars`, then plan
   and apply again. The plan should add `pager_sms`, the `pager` SMS
   preferences, `pager_delivery_failed` and `pager_sms_spend`, and the number shows as
   `(sensitive value)`.
3. **Ring the phone**:
   `aws cloudwatch set-alarm-state --alarm-name feohledger-prod-uptime-api --state-value ALARM --state-reason "Paging test" --region us-east-1 --profile feohledger`.
   A text and an email arrive within a minute. The next evaluation sets the
   alarm back to OK, which sends a second text. If no text comes, check
   `feohledger-prod-pager-sms-failed` and the registration status.

## Secrets

This repo is **public**, so it holds no secret — not even an encrypted one (`../docs/decisions.md` §12, §165). The project's secrets live sops-encrypted in the private `Absence0760/infra-secrets` repo under `feohledger/`, keyed by `alias/feohledger-sops`. The estate account bootstrap (`~/github/templates/scripts/new-project-account.sh`) created that key in the FeohLedger account, alongside the state bucket and the deploy role; this module neither creates nor manages it.

The one secret this module handles is the RDS master password (break-glass only — the app signs in with IAM tokens, `../docs/decisions.md` §310), and it handles it without storing it: an ephemeral variable feeding a write-only attribute, supplied from `infra-secrets/feohledger/terraform.sops.yaml` at apply time (§ Workload stack). The earlier plan here — a `carlpett/sops` `sops_file` data source — was dropped because a data source's value lands in state in plaintext; the write-only path keeps it out of state and plan files entirely. Every other variable is operator config, not a credential. Access is IAM — an operator needs `kms:Decrypt` on the key plus read access to the private repo; nothing in this repo changes. Rotation: `../docs/secrets-rotation.md`.

## Tearing everything down

The sops key (`alias/feohledger-sops`), the state bucket and the GitHub deploy role belong to the account bootstrap, not this module — a `terraform destroy` here never touches them, and they are not to be deleted from here.

Note: audit-logs bucket uses Object Lock in **Compliance** mode — you cannot delete that bucket until every object has aged past its 7-year retention. Factor that into any teardown plan.

## See also

- `~/github/infra-secrets/feohledger/` (private) — the encrypted secrets and their plaintext template
- `~/github/project-mgmt/docs/secrets-management.md` — the estate secrets pattern
- `../backend/CLAUDE.md` § Secrets management — day-to-day encrypt/decrypt workflow
- `../docs/soc2-readiness.md` — control-by-control status + pending items
