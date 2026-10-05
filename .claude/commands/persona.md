---
description: Run one, several, or all of the bug-hunting persona auditors (.claude/agents/personas/persona-*.md). Each persona adopts a real-world point of view — the AP domain roles (approver, CFO, accountant, supplier, fraudster), the integration counterparties (card/payment processor, integrator), the jurisdictions (USA/UK/ZA business, international user), and the cross-cutting users (new user, power user, admin, data subject, accessibility user) — and writes findings to reviews/<persona>.md. Usage — `/persona cfo`, `/persona uk-business,card-processor`, or `/persona all` (default).
---

Dispatch the persona auditors named in the argument, in parallel, and summarize
what each filed.

## Resolve the argument

`$ARGUMENTS` selects which personas to run:

- empty or `all` → the whole panel (every `.claude/agents/personas/persona-*.md`).
- a comma/space-separated list of slugs → just those. Accept slugs with or
  without the `persona-` prefix (`cfo`, `persona-cfo`, `uk-business` all resolve).
- if a slug doesn't match an existing `persona-*` agent, list the available
  personas and ask which was meant — don't guess.

The current panel, grouped by what each is positioned to catch:

- **AP domain roles** — `approver`, `cfo`, `accountant`, `supplier`, `fraudster`.
- **Integration counterparties** — `card-processor`, `payment-processor`,
  `integrator`.
- **Jurisdiction / locale** — `usa-business`, `uk-business`,
  `south-africa-business`, `international-user`.
- **Cross-cutting users** — `new-user`, `power-user`, `admin`, `data-subject`,
  `accessibility-user`.

(Discover the live list with `ls .claude/agents/personas/persona-*.md` so this stays
correct as personas are added. `all` is 17 agents in parallel — for a scoped
run, name a group's slugs explicitly.)

## Procedure

1. **Spawn one agent per selected persona, in parallel** — all Agent tool calls
   in a single message. Use `subagent_type: "persona-<slug>"` and a prompt that
   says: *"Run your persona audit. Reconcile reviews/<persona>.md with HEAD per
   `.claude/personas/README.md` before writing — re-verify open findings, move
   landed fixes to Resolved, re-stamp the header. Then hunt for new findings.
   Report a one-line-per-finding summary back to me; the full writeup goes in
   your review file."*
2. Each persona is **read-only on app code** and writes only its own
   `reviews/<persona>.md` (git-ignored). They do not patch anything.
3. **Consolidate** the summaries each agent returns into one table — persona,
   counts by severity, and the headline finding. Don't re-paste the full
   reports; point at the files.

## Output shape

```
# Persona panel — <date>  (HEAD <short-sha>)

| Persona | Crit | High | Med | Low | Headline finding |
|---------|------|------|-----|-----|------------------|
| cfo     | 0    | 1    | 2   | 0   | aging bucket filters on created_at not due_date |
| ...     |      |      |     |     |                  |

Full reports: reviews/persona-*.md

## Recommended fix order
1. [Critical] ...
2. ...
```

## Notes

- Read-only. The reports are the deliverable; do not edit app code from findings
  without asking the user first.
- These reports are living documents — the reconcile-before-you-trust rule in
  `.claude/personas/README.md` binds every run and every hand-edit. A finding not
  re-verified against HEAD is a bug in the report.
- For a focused change (just the payment webhook, just the ZA banking form),
  prefer running the single relevant persona over the whole panel.
