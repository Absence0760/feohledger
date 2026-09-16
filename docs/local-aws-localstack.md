# Local AWS testing with LocalStack

The app talks to several AWS services that, until now, had only a `mock` (or
in-process) local story: **SQS** (the `lambda` dispatch modes), **SES**
(outbound email), and **CloudWatch Logs + S3 Object Lock** (the SOC 2 audit-log
shipper). [LocalStack](https://localstack.cloud) emulates all of these in one
container, so those paths run on a dev laptop with no cloud account — the
local-first guard rail applied to AWS.

It's opt-in under the Compose `aws` profile and safe by default: with
`FEOH_AWS_ENDPOINT_URL` unset, every AWS client talks to real AWS exactly as
before (production is unaffected). Set the var and they target LocalStack.

## TL;DR

```bash
pnpm aws:up        # LocalStack on :4566 (Docker); init script creates resources
# then point the backend at it (backend/.env):
#   FEOH_AWS_ENDPOINT_URL=http://localhost:4566
#   AWS_ACCESS_KEY_ID=test
#   AWS_SECRET_ACCESS_KEY=test
#   AWS_DEFAULT_REGION=us-east-1
pnpm dev:backend   # restart so the new env is picked up
pnpm aws:down      # stop it when done
```

## What's provisioned

`backend/localstack/init/ready.d/01-resources.sh` runs on boot and creates:

| Resource | Value | Used by |
|---|---|---|
| SQS queue | `feoh-extraction` | `FEOH_EXTRACTION_MODE=lambda` |
| SQS queue | `feoh-erp` | `FEOH_ERP_MODE=lambda` |
| SQS queue | `feoh-audit` | `FEOH_AUDIT_MODE=lambda` |
| SES identity | `no-reply@localhost` | `ses` email adapter |
| CloudWatch log group | `/ap/audit` | `cloudwatch` audit sink |
| S3 bucket (Object Lock) | `feoh-audit-worm` | `s3_objectlock` audit sink — versioned, **COMPLIANCE mode, 2555-day default retention**, matching `infra/s3.tf`'s audit-logs bucket. The adapter's boot check requires all three, so a bucket created with Object Lock merely *enabled* is refused (`backend/docs/audit-log-shipping.md`). |

The single knob is **`FEOH_AWS_ENDPOINT_URL`** (`app/config.py`). When set, the SQS
dispatch clients, the `ses` email adapter, and the `cloudwatch` / `s3_objectlock`
audit sinks all build their boto3 client against it. LocalStack accepts any
credentials, but boto3 still needs *some* present — hence the dummy
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

## Exercising each path

### Audit-log shipping (CloudWatch + S3 Object Lock)

The headline SOC 2 path — previously only the `mock` sink ran locally.

```bash
# backend/.env (plus the four AWS vars above)
FEOH_AUDIT_SHIPPING_ENABLED=true
FEOH_AUDIT_SHIPPING_PROVIDERS=cloudwatch,s3_objectlock
FEOH_AUDIT_SHIPPING_S3_BUCKET=feoh-audit-worm
FEOH_AUDIT_SHIPPING_CLOUDWATCH_GROUP=/ap/audit
```

Restart the backend, generate an auditable event (approve an invoice, etc.), and
after the shipper tick:

```bash
docker compose -f backend/docker-compose.yml exec localstack \
  awslocal s3 ls s3://feoh-audit-worm --recursive
docker compose -f backend/docker-compose.yml exec localstack \
  awslocal logs describe-log-streams --log-group-name /ap/audit
```

### Lambda dispatch modes (SQS)

```bash
# backend/.env
FEOH_EXTRACTION_MODE=lambda
FEOH_SQS_EXTRACTION_QUEUE_URL=http://localhost:4566/000000000000/feoh-extraction
# (and FEOH_ERP_MODE / FEOH_SQS_ERP_QUEUE_URL, FEOH_AUDIT_MODE / FEOH_SQS_AUDIT_QUEUE_URL)
```

Trigger an extraction; the job lands on the queue instead of the in-process pool:

```bash
docker compose -f backend/docker-compose.yml exec localstack \
  awslocal sqs receive-message --queue-url http://localhost:4566/000000000000/feoh-extraction
```

(There's no local Lambda consumer — this verifies the *dispatch* half. The
in-process `local` mode remains the default for actually processing jobs.)

### SES outbound email

```bash
# backend/.env
FEOH_EMAIL_PROVIDER=ses
FEOH_EMAIL_FROM=no-reply@localhost
```

Sent mail is captured by LocalStack (not delivered):

```bash
curl -s http://localhost:4566/_aws/ses | python3 -m json.tool
```

## Coverage

The seam itself — every AWS client honoring `FEOH_AWS_ENDPOINT_URL` (and passing
`endpoint_url=None` when unset, so prod hits real AWS) — is locked by
`backend/tests/test_aws_endpoint_override.py`, which runs in CI without the
container. LocalStack is the hands-on complement for actually exercising the
round trip.

## Notes

- **Textract** also routes through `FEOH_AWS_ENDPOINT_URL`, but Textract is a
  LocalStack **Pro**-only service — extraction stays on `mock` / `ollama` /
  `claude_vision` locally.
- **MinIO stays the S3 file store** for invoice uploads (`FEOH_S3_ENDPOINT_URL`).
  LocalStack only fronts the *other* AWS services here; the two coexist.
- Resources live in the `localstack-data` volume; `pnpm services:reset` (or
  `docker compose --profile aws down -v`) wipes them, and the init script
  recreates them on the next boot.
