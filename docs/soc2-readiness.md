# SOC 2 readiness

The plan to get to **SOC 2 Type II** — the security attestation enterprise finance buyers ask for in every procurement review.

This is the kickoff plan. Most of SOC 2 is process work (policies, evidence collection, audit observation), but a handful of engineering controls have to land in code first. This doc is the source of truth for what's done, what's pending, and what the founder still has to do as a human (sign contracts, write policies, etc.).

---

## What SOC 2 actually is

A point-in-time (Type I) or longitudinal (Type II) attestation by an independent CPA firm that the company's controls around **Security, Availability, Confidentiality, Processing Integrity, and Privacy** (the five Trust Services Criteria) are designed and operating effectively.

- **Type I** — design only, point in time. Auditor checks "do you have these controls written down and built?". 4–8 weeks to close after prereqs are in place.
- **Type II** — design + operating effectiveness over a 6+ month observation window. The one buyers actually want. Renewable annually.

Most "SOC 2-compliant" SaaS companies hold Type II.

## Timeline

```
T+0       Sign with compliance vendor (Vanta/Drata/Secureframe)
T+0       Connect cloud + IdP + repo integrations to vendor dashboard
T+0–4w    Engineering prereqs in code (this document's checklist)
T+0–8w    Policy library drafted + adopted (HR + leadership sign-off)
T+8w      Type I audit kickoff (4–8 weeks)
T+12–16w  Type I report issued
T+12w     Type II observation window starts (6+ months)
T+9mo     Type II audit + report issued
```

So **9 months** from kickoff to a Type II report you can hand to a buyer. Start the vendor + prereqs now even if you don't audit yet — the observation clock only starts once everything is in place.

---

## Vendor comparison

Pick one. They all do roughly the same thing: continuous-control monitoring (CCM) dashboards, policy templates, evidence collection, employee onboarding flows, and a paper trail the auditor pulls from.

| Vendor | Approx. price (startup tier) | Strengths | Notes |
|---|---|---|---|
| **Vanta** | $8–15K/yr | Largest integration catalog, polished UX, strong AWS coverage | Default choice for YC/seed-stage SaaS |
| **Drata** | $7–14K/yr | Best continuous-monitoring dashboards, tight AWS/GitHub flows | Slightly more engineering-friendly |
| **Secureframe** | $7–12K/yr | Strong policy templates, good support | Smaller integration catalog than Vanta/Drata |
| **Sprinto** | $4–8K/yr | Cheapest, audit-firm relationships, decent UX | Startup-focused, less mature than Vanta |
| **Audit firm** | $10–25K (Type II) | Independent — issues the report. NOT the same as the vendor above. | Examples: Prescient, A-LIGN, Insight Assurance, Johanson Group |

**Recommendation**: Vanta or Drata for the dashboard, then a small audit firm (Prescient or Johanson) for the actual attestation. Total: **~$20–30K for the first Type II report**.

The vendor signs you up with their preferred audit-firm partners — you don't need to source the auditor independently.

---

## Engineering prerequisites — checklist

Things an auditor expects to see *in code or config*, not just in a policy doc. Status reflects the current state of this repo.

### Identity, access, and authentication

| Control | Status | Where it lives |
|---|---|---|
| Password complexity + storage (bcrypt_sha256) | Done | `backend/app/utils/passwords.py` — single shared `pwd_context` implements `bcrypt_sha256` (HMAC-SHA256 pre-hash → bcrypt) to side-step bcrypt's 72-byte truncation; no passlib (`docs/decisions.md` §151). Legacy `$2b$` and v1 hashes still verify. Digest compatibility: `backend/tests/test_bcrypt_sha256_compat.py`. |
| Cross-tenant guard (JWT-org vs X-Tenant-Slug) | Done | `backend/app/tenant.py::get_tenant` — refuses to resolve the tenant when the employee JWT's `org` claim doesn't match the header. Tests: `backend/tests/test_tenant_isolation.py` + `frontend/tests-e2e/auth/tenant-isolation.spec.ts`. |
| MFA support (TOTP + email backup) | Done | `backend/app/services/mfa.py` |
| MFA enforcement (org-level toggle) | Done | `Organization.settings.mfa.required` |
| SSO (OIDC — Okta, Entra) | Done | `backend/app/api/auth_sso.py` |
| SCIM 2.0 provisioning | Done (`/Users` only) | `backend/app/api/scim.py` |
| Forced password change on first login | Done | `User.must_change_password` |
| RBAC enforcement at the API layer | Done | `backend/app/api/deps.py` `require_roles()` + every router |
| Segregation of duties (approver ≠ uploader) | Done | `backend/app/services/approval_chain.py::check_segregation` — default-on; opt-out per workflow via `require_segregation: false`. Keys on `Invoice.uploaded_by_id`, so the control only binds where the creator is recorded: `backend/tests/test_invoice_uploader_stamping.py` is the enforcement that every employee-facing creation path stamps it (see `docs/authentication.md` § Segregation of duties → NULL uploader) |
| Token revocation on logout (Redis blocklist) | Done | `backend/app/redis.py` |
| Session timeout (JWT lifetime ≤ 30 min) | Done | `FEOH_ACCESS_TOKEN_EXPIRE_MINUTES` |
| Quarterly access reviews (auditor-friendly export) | Done | `backend/scripts/access_review.py` |
| Forced logout on role change | Done | `app/api/admin.py` → `update_user` / `delete_user` call `services.session_management.revoke_user_sessions` on role change or deactivation |
| Concurrent session limit | Done | Redis sorted-set per user (`active_jtis:<user_id>`) populated on login / MFA verify / SSO callback; oldest evicted via `FEOH_MAX_CONCURRENT_SESSIONS` (default 5) |
| SSO-only mode (disable password login when SSO is configured) | Done | `settings.sso.sso_only`, decided by `backend/app/services/sso.py::is_sso_only`: closes `/auth/login` and the password step-up once the IdP config resolves, and `PATCH /api/organization` refuses to save it over one that does not (`docs/authentication.md` § SSO-only mode) |

### Encryption

| Control | Status | Where it lives |
|---|---|---|
| TLS in transit (frontend + API) | Done | CloudFront/ALB (`infra/`) + HSTS middleware (`backend/app/main.py` `SecurityHeadersMiddleware`). Post-deploy smoke test: `backend/scripts/verify_tls.py` |
| HSTS + security response headers | Done | `backend/app/main.py` `SecurityHeadersMiddleware` — HSTS gated on `FEOH_HSTS_ENABLED`; `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` always set. Tests: `backend/tests/test_security_headers.py` |
| Encryption at rest — RDS | Done in prod | RDS storage encryption flag (Terraform) |
| Encryption at rest — S3 | Done | `infra/s3.tf` — SSE-KMS via the customer-managed key from `infra/kms.tf` on every data bucket; the server-access-log sink is SSE-S3, because AWS does not support SSE-KMS on a log destination (`docs/decisions.md` §166) |
| Encryption at rest — secrets | Done | SOPS + AWS KMS, in the private `infra-secrets` repo (`feohledger/`) — never this public one |
| KMS key rotation procedure | Done | `docs/secrets-rotation.md` |
| Plaintext secrets in CI | None | All secrets via GitHub Actions `secrets:` context |

### Logging + monitoring

| Control | Status | Where it lives |
|---|---|---|
| Application audit log (writes, approvals, transitions) | Done | `backend/app/services/audit.py` → `AuditLog` table per tenant |
| RBAC denial logging | Done | `app/api/deps.py` `require_roles()` writes WARNING with actor + path |
| Auth event logging (login, logout, MFA events) | Done | `app/services/audit_dispatch.py::dispatch_auth_audit` resolves the tenant DB from the org id and writes an `auth.*` row for every login / logout / MFA / SSO event |
| Webhook HMAC verification + event dedup | Done | `backend/app/services/webhook_security.py` (`verify_hmac_sha256` constant-time + `is_event_already_processed` Redis SET NX EX with 24h TTL). Applied to `/api/payments/webhook/...`, `/api/cards/webhook/{provider}`, `/api/erp/webhook/{erp_type}`. All four rejection paths return 204 silently so the response doesn't enumerate. |
| File-upload sanitisation + cross-tenant download check | Done | `backend/app/services/storage.py::_safe_filename` strips path separators + control chars before interpolating into the S3 key; `GET /api/invoices/file/{file_key}` verifies the key's first segment equals the requesting user's `organization_id`. |
| **Centralized + WORM-compliant audit log shipping** | Done | `backend/app/services/audit_log_shipper.py` + `services/audit_shipping/` adapters — background loop ships tenant `audit_log` rows to CloudWatch Logs + S3 Object Lock. See `backend/docs/audit-log-shipping.md`. |
| Centralized application logs (stderr) | Done in prod | ECS → CloudWatch Logs |
| Alerting on 5xx + RBAC-denial spikes | Pending | CloudWatch Alarms or Datadog |
| Uptime monitoring | Pending | UptimeRobot, Pingdom, or Better Stack |

### Vulnerability + dependency management

| Control | Status | Where it lives |
|---|---|---|
| Dependabot for Python, npm, Docker, GitHub Actions | Done | `.github/dependabot.yml` |
| SAST in CI | Done | `.github/workflows/security.yml` (CodeQL — Python + JS) |
| Container image scanning | Done | `.github/workflows/security.yml` (Trivy on backend Dockerfile) |
| Secret scanning in repo | Done | GitHub native (free for public + paid tiers) |
| Penetration test (annual) | Pending | One-time engagement, ~$8–15K |

### Backup, recovery, and continuity

| Control | Status | Where it lives |
|---|---|---|
| Automated RDS backups | Done in prod | RDS automated snapshots, 7-day retention |
| S3 object versioning | Done | `infra/s3.tf` — `aws_s3_bucket_versioning` = Enabled on every bucket |
| S3 Object Lock (WORM) | Done | `infra/s3.tf` — invoice-files = GOVERNANCE 365d, audit-logs = COMPLIANCE 2555d |
| Documented backup + restore runbook | Done | `docs/backup-disaster-recovery.md` |
| Quarterly restore test | Pending | Schedule + record; runbook describes the procedure |
| Documented RTO/RPO | Done | `docs/backup-disaster-recovery.md` |

### Secrets management

| Control | Status | Where it lives |
|---|---|---|
| Secrets encrypted at rest (SOPS + KMS) | Done | `infra-secrets` (private) `feohledger/`, key `alias/feohledger-sops`; `.gitignore` + `.github/workflows/env-isolation.yml` keep sops files out of this public repo |
| Documented rotation cadence + procedure | Done | `docs/secrets-rotation.md` |
| 90-day rotation for app secrets | Pending | Document accepted; track rotations in vendor dashboard |
| KMS key rotation | Done | `infra/kms.tf` — `enable_key_rotation = true` on the app KMS key. SOPS bootstrap key is rotated separately via the procedure in `docs/secrets-rotation.md`. |

### Change management

| Control | Status | Where it lives |
|---|---|---|
| All changes via PR + review | Done | GitHub branch protection on `main`: PR required and enforced on admins (no direct pushes, incl. the owner), linear history, no force-push/deletions, conversation resolution required. 0 required approvals — a green CI is the merge gate, not a human sign-off. |
| CI gate (lint + tests) before merge | Done | `.github/workflows/ci.yml` — the "CI gate" aggregator job (`needs:` every CI job) is the **single required status check** on `main`, so a red CI blocks the merge. |
| Production deploy from `main` only | **Not yet evidenced** | Corrected 2026-09-06: the cited `.github/workflows/deploy.yml` **does not exist**. The real workflow is `.github/workflows/aws-deploy.yml`, which triggers on `release: [published]` — not `push: main` — and every job is gated on the repo variable `AWS_DEPLOY_ENABLED`, unset today, so **nothing deploys at all**. Branch protection (row 1) does enforce that `main` is the only source a release tag can come from, which is the control's substance; but there is no running deploy pipeline to evidence, so this cannot be claimed as Done until the AWS estate exists and the variable is set. Re-evidence at that point against `aws-deploy.yml` and its release trigger. |
| Documented incident-response runbook | Pending | Vendor template + customise; needs on-call rotation |

---

## Process work (what the founder has to do)

These are not coding tasks. The compliance vendor's dashboard walks you through them.

1. **Sign with a compliance vendor** (Vanta / Drata / Secureframe / Sprinto). Two weeks of trial usually included.
2. **Connect integrations** — AWS, GitHub, Google Workspace (or Slack for IdP), the IdP if you use one.
3. **Adopt the policy library** — info security, acceptable use, access control, change management, incident response, vendor management, business continuity, data classification, encryption, password. Vendor provides templates; founder reviews + signs.
4. **Set up employee onboarding/offboarding checklist** — even with one employee, the checklist needs to exist (it'll be a one-line form). On hire: account creation, MFA enrollment, policy attestation. On termination: revoke access, capture exit checklist.
5. **Designate the security officer** — you, for now. The role just needs a name attached.
6. **Vendor risk reviews** — a one-page review for each material vendor (AWS, Anthropic, Lithic/Nium, Merge.dev, the email provider). Vendor dashboard tracks them.
7. **Run the Type I audit** — 4–8 weeks once the prereqs are in place.
8. **Open the Type II window** — minimum 6 months of "we did the things we said we'd do" with evidence captured by the vendor dashboard.

## When to actually start

- **Now**, if any prospect ≥ $50K ARR has asked about SOC 2.
- **In 3 months**, if not. The Type II clock is the binding constraint — every month delayed is a month before you can answer "yes" to the security questionnaire.

## Cost summary (first year)

| Item | Cost |
|---|---|
| Compliance vendor (Vanta/Drata) | $8–15K |
| Audit firm (Type I + Type II) | $15–25K |
| Penetration test (annual) | $8–15K |
| Engineering time for prereqs | 1–3 weeks (one engineer) |
| Founder time for policies + onboarding flows | 2–3 weeks across 3 months |
| **Total first year** | **~$30–55K + 4–6 person-weeks** |

Renewals (Type II + pen test annually) are around **$25–40K/yr** thereafter.

---

## What's in this repo today vs. pending

**Shipped engineering controls:**
- `docs/soc2-readiness.md` — this document
- `docs/backup-disaster-recovery.md` — backup + DR runbook with RTO/RPO
- `docs/secrets-rotation.md` — secrets rotation procedure
- `backend/scripts/access_review.py` — quarterly access-review CSV export
- `.github/workflows/security.yml` — CodeQL (SAST) + Trivy (container scan), weekly + on push
- Concurrent session limit (Redis sorted set per user, configurable cap)
- Forced logout on role change + account deactivation
- Auth event audit logging (login/logout/MFA/SSO → tenant `audit_log`)
- `backend/app/services/audit_log_shipper.py` + `audit_shipping/` adapters — centralized, WORM-compliant audit-log shipping (CloudWatch Logs + S3 Object Lock)
- HSTS + security-header middleware (`backend/app/main.py`), TLS smoke script (`backend/scripts/verify_tls.py`)
- KMS auto-rotation + S3 versioning + Object Lock in Terraform (`infra/kms.tf`, `infra/s3.tf`)

**Pending — needs a code change** (next work):
- Migrate existing S3 buckets onto Object Lock. Object Lock can only be
  enabled at bucket creation — the invoice-files and audit-logs buckets in
  `infra/s3.tf` are net-new. For any pre-existing bucket: create a new
  bucket with `object_lock_enabled = true`, `aws s3 sync` the contents,
  switch `FEOH_S3_BUCKET` to the new name, delete the old bucket once
  retention on the new one is verified.

**Pending — process work** (founder, not engineer):
- Pick + sign with a compliance vendor
- Adopt the policy library
- Onboard/offboard checklist
- Vendor risk reviews
- Schedule the Type I audit + open the Type II window
