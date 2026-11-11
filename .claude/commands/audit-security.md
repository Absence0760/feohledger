---
description: Run the repo-security-auditor against the working diff (or a specified area). Read-only — reports findings, doesn't patch. Heavier than `/check`; use when a security-sensitive change is in flight or when investigating a suspected leak.
---

Invoke `repo-security-auditor` against a focused area. Output a structured finding list; do NOT apply fixes — that's the user's call.

## When to use this command

**Right fit:**

- Before merging a PR that touches `app/api/*.py` (HTTP surface), `app/services/webhook_security.py`, `app/tenant.py`, `app/api/deps.py`, or any router that adds a new endpoint.
- After a customer reports anything that smells like cross-tenant or auth leakage.
- Periodically (monthly) as a sweep across the trust boundaries.
- After dependency bumps that touch crypto (bcrypt, joserfc, cryptography, xmlsec).

**Wrong fit — refuse:**

- The diff is trivial (typo, comment, dep-version bump with no source change).
- `/check` has already run cleanly on the same diff in the last hour and nothing security-sensitive changed — the hook + `/check`'s code-reviewer already cover the day-to-day case.

## What this command does NOT do

- It does NOT apply fixes. The agent is read-only by design.
- It does NOT replace `.claude/hooks/security-patterns.sh` — that hook runs on every edit and catches the cheap regressions. This command spins up a slower, more nuanced sweep.

## Procedure

### 1. Pick a focus area

If the user supplied `<task>` to the command, that's the focus. Otherwise default to "the working diff against main".

Common focus areas (use the wording in the agent prompt):

- "tenant isolation across HTTP routes and DB session resolution"
- "money path — every endpoint that initiates / reverses / confirms payment"
- "webhook security (HMAC verification + event dedup) across `/api/payments/webhook`, `/api/cards/webhook`, `/api/erp/webhook`, `/api/email-intake/inbound`"
- "secrets handling: where new code reads credentials, whether `pwd_context` from `utils.passwords` is the only hash context, whether jwt.decode is centralised"
- "audit trail coverage: every status-mutating handler dispatches an audit row"
- "PII surface: bank details / tax_id / PAN never reach logs or response bodies"

### 2. Spawn the agent

Send a single Agent call to `repo-security-auditor`:

```
Audit <focus area>.

Specifically:
  - The bug classes you should hunt are the eight project invariants
    in the root `CLAUDE.md` plus the tactical patterns the
    security-patterns hook flags (`.claude/hooks/security-patterns.sh`).
  - Examples of bugs we have shipped before: cross-tenant data leak
    via `get_tenant_db` (no JWT-org cross-check), cross-tenant file
    read via `/api/workflow/file/{file_key}`, raw filename in S3
    keys, bcrypt 72-byte truncation, exception interpolation in log
    messages (PAN leak risk), missing HMAC on card / ERP webhooks.
  - Output the strict format from your spec. Group findings by
    severity (Critical / Improvement / Nit). For each, give file:line,
    the bug-class label, and the suggested fix.
```

### 3. Render the report

The agent's output is the report. Pass it back verbatim under a heading; do NOT summarize away severities or file:line references.

```
## /audit-security report

<agent output here>

---
Next steps:
  - Critical findings: fix before merging. Re-run `/audit-security` after.
  - Improvement findings: address in this PR or open a follow-up
    issue with a label.
  - Nit: address in this PR if it's quick; otherwise let it slide.
```

### 4. If the agent finds nothing

Say so plainly: "No findings. The audit scoped to <area> against <ref> turned up clean." Don't pad.

## How this complements the rest of the stack

| Layer | What it catches | Cost |
|---|---|---|
| `.claude/hooks/security-patterns.sh` | Stable textual shapes (bcrypt scheme, naive datetime, raw filename, exception-in-log, etc.) | ~50ms per Edit |
| `/check` (code-reviewer + test-gap-checker + doc-hygiene-checker) | Diff-level review against documented conventions | ~30s |
| `/audit-security` | Trust-boundary sweep across the area named | ~1–2min |
| `/safe-edit` | Coder ↔ reviewer loop for high-blast-radius changes | 2-3x normal edit |

Reach for the heaviest one warranted by the change. The hook + `/check` is the daily floor; this command is the per-PR gate; `/safe-edit` is for the money-path / migration / auth changes.
