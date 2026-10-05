# Persona audits

A **persona** is a read-only Claude subagent (`.claude/agents/personas/persona-*.md`) that
adopts a specific real-world point of view — an approver, a CFO, a South African
business owner, a card processor — and walks the app the way that person would,
looking for bugs, missing primitives, wrong assumptions, and jurisdiction gaps
that a generic code review never surfaces because it has no domain stake.

Personas complement, not replace, the existing reviewers:

- `code-reviewer` / `repo-security-auditor` ask *"is this code correct and safe?"*
- A persona asks *"does this app actually work for **me**, and would it embarrass
  me in front of an auditor / my bank / SARS / my CFO?"*

## How to run one

These are agents. Run one by asking for it by name, e.g.

> run the `persona-cfo` audit
> run `persona-south-africa-business` and `persona-card-processor`

or run the whole panel with the `/persona` command. Each persona writes its
findings to `reviews/<persona-name>.md` (git-ignored — see `reviews/README.md`).

## The output contract (every persona follows this)

### 1. Reconcile with reality *before* writing anything

This is the rule that keeps the reports trustworthy. On every run, a persona:

1. Captures the current commit: `git rev-parse --short HEAD`.
2. Reads its existing `reviews/<persona>.md` if one exists.
3. **Re-verifies every open finding against the code at HEAD.** For each one:
   - Still reproduces → keep it, refresh the `file:line` (line numbers drift).
   - Fixed since last run → move it to `## Resolved`, stamp the commit/date the
     fix landed (or "fixed by HEAD" if you can't pin it).
   - No longer applicable (feature removed, assumption changed) → delete it with
     a one-line note in `## Resolved` so the next run doesn't re-derive it.
4. Looks for *new* findings.
5. Rewrites the header stamp (commit + UTC date from `date -u`).

A finding that is asserted but not re-verified against current code is a bug in
the report. Stale findings are worse than no findings — they waste a fix cycle
and erode trust in the whole folder. **Never** copy a prior finding forward
without opening the file it cites.

This rule binds *any* session that touches a `reviews/*.md` file, not just the
persona agent — if you open one to act on a finding, confirm it still
reproduces at HEAD first.

### 2. File format

````markdown
---
persona: persona-cfo
last_reviewed_commit: 1a2b3c4
last_reviewed_at: 2026-05-31T14:00:00Z
---

> **Living document — reconcile before you trust.** Findings were verified at
> the commit above. Before acting on or citing any entry, re-verify it against
> the current code; a fix may have landed since. When you edit this file, follow
> the protocol in `.claude/personas/README.md` § "Reconcile with reality".

# persona-cfo — review

_One paragraph: who I am and what I came here to check._

## Open findings

### [High] frontend/src/.../X.svelte:42 — approval threshold is read as a float
- **What I tried:** <concrete steps / curl / click path>
- **What I expected:** <the domain-correct behaviour>
- **What happened:** <the bug>
- **Why it matters to me (the persona):** <business / compliance stake>
- **Invariant / rule:** <root CLAUDE.md § ... if applicable>
- **Fix scope:** <file(s) that would change — I do not patch>

## Resolved
- [Med] ~~vendors.py:88 — ...~~ fixed by `8a1b793` (2026-05-20).

## Out of scope / notes
- <assumptions, things I deliberately didn't test, follow-ups>
````

### 3. Severity rubric (shared with the security auditor)

- **Critical** — wrong money movement, data loss, cross-tenant leak, or a
  compliance breach that is reportable. Fix before next deploy.
- **High** — the persona cannot complete a core job, or the app produces a
  wrong-but-plausible number they'd act on.
- **Medium** — friction, a missing affordance, a defensible-but-wrong default.
- **Low** — cosmetic, wording, nice-to-have.

### 4. House rules

- **Read-only on app code.** A persona reports; it does not patch. The only file
  a persona writes is its own `reviews/<persona>.md`.
- No emojis, no preemptive abstractions in anything you write (root `CLAUDE.md`).
- Don't paste secrets, full PANs, full bank numbers, or tax IDs into a report —
  identify the field by name and location.
- Prefer reproducible findings. If you can't confirm something, file it under
  "needs verification" and say exactly what you'd need to confirm it.
- Distinguish *a real bug* from *a feature the app never claimed to have*. Both
  are worth recording, but label the second as a gap, not a defect.

## Adding a new persona

Copy the closest existing `.claude/agents/personas/persona-*.md`, then:

1. Rewrite the frontmatter `name` (`persona-<slug>`) and `description`.
2. Rewrite the identity paragraph and the "what I care about" list.
3. List the concrete app surfaces this persona exercises, with file starting
   points (so the agent doesn't burn a turn rediscovering the layout).
4. Add a "known bug shapes for this domain" list — the failure modes this
   persona is uniquely positioned to catch.
5. Keep the output contract pointer (this file). Don't restate the whole
   protocol inline — reference it.

Good candidates not yet built: `persona-eu-sepa-business` (EUR/IBAN/SEPA, VAT
MOSS, GDPR DSAR), `persona-erp-integrator` (Merge.dev / NetSuite / D365 sync
direction + status webhooks), `persona-australia-business` (GST/BAS/BSB),
`persona-india-business` (GST/TDS/IFSC), `persona-soc2-auditor` (overlaps
`compliance-auditor` but narrates an evidence request).
