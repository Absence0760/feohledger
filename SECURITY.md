# Security Policy

Thank you for helping keep this project and its users safe.

## Reporting a Vulnerability

**Do not open a public GitHub issue for a suspected vulnerability.**
Public issues are indexed and disclosed the moment they land. We use
GitHub's private vulnerability reporting workflow instead.

### How to report

1. Open the project's Security tab:
   https://github.com/Absence0760/feohledger/security
2. Click **Report a vulnerability** (or follow the direct link:
   https://github.com/Absence0760/feohledger/security/advisories/new).
3. Fill in the form. Steps-to-reproduce and a minimal proof-of-concept
   make triage faster; speculation about impact is welcome but not
   required.

If GitHub private reporting is unavailable for any reason, email the
maintainer at the address listed on the repo owner's public profile:
https://github.com/Absence0760

### What to expect

| Step | SLA |
|------|-----|
| Acknowledgement | 3 business days |
| Severity triage + initial response | 7 business days |
| Fix or coordinated-disclosure plan (High / Critical) | 30 days |
| Fix (Medium / Low) | next release |

We aim to credit reporters in the published GitHub Security Advisory
unless you ask to remain anonymous.

## Scope

In scope:

- Backend (`backend/`): authentication, multi-tenant isolation,
  money-moving paths, secret handling, audit-log integrity, webhook
  signature verification.
- Frontend (`frontend/`): cross-tenant data exposure, XSS in the
  rendered UI, JWT handling, CSP / cookie configuration.
- Mobile (`mobile/`): credential storage, deep-link handling,
  background-sync data exposure.
- Infrastructure (`infra/`): Terraform IaC misconfigurations (public
  buckets, over-broad IAM, etc.).
- Anything that could compromise SOC 2 controls listed in
  `docs/soc2-readiness.md`.

Out of scope:

- Findings that require physical access to a user's device.
- Findings that require an already-privileged role (admin, CFO) inside
  a tenant — those are scoped to "trusted operator", not "external
  attacker".
- Reports on demo data or seed credentials (`backend/scripts/seed.py`).
  These are intentionally weak; no production data is reachable
  through them.
- Bug reports unrelated to security (use issues / discussions for
  those).

## Supported Versions

This project ships rolling releases off `main`. We do not maintain
parallel security branches. Security fixes land on `main`; downstream
consumers should track `main`.

| Branch | Supported |
|--------|-----------|
| `main` | Yes |
| anything else | No |

## Safe Harbor

Good-faith security research conducted under this policy is
authorized. We will not pursue legal action against researchers who:

- Stay within the scope above.
- Avoid privacy violations, destruction of data, and interruption
  of service.
- Give us reasonable time to investigate and remediate before any
  public disclosure (the SLAs above are the default).
- Use only their own test accounts; do not interact with other
  tenants' data.

## References

- GitHub's private vulnerability reporting docs:
  https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities
- OpenSSF Scorecard Security-Policy check:
  https://github.com/ossf/scorecard/blob/main/docs/checks.md#security-policy
