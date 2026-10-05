# reviews/

Output folder for the persona auditors (`.claude/agents/personas/persona-*.md`). Each
persona writes its findings to `reviews/<persona-name>.md`.

**Everything in here except this README is git-ignored.** The reports are
per-clone working notes, not a committed artifact — they go stale the moment
code lands, so they're regenerated, not version-controlled.

## If you open a report to act on it

These are **living documents**. A finding is only as good as the commit it was
verified against (see the header stamp in each file). Before you act on,
cite, or hand off any finding:

1. Open the `file:line` it points at and confirm it still reproduces at HEAD.
2. If it's already fixed, move it to `## Resolved` with the fixing commit.
3. If you changed code that a report covers, re-run that persona so the report
   reconciles instead of drifting.

The full protocol — file format, severity rubric, the reconcile-before-you-trust
rule — lives in `.claude/personas/README.md`. Read it before editing a report by
hand.

## Running the personas

Ask for one by name (`run persona-cfo`), several at once, or the whole panel via
the `/persona` command. The current panel:

| Persona | Point of view |
|---|---|
| `persona-approver` | AP manager working the approval queue — limits, segregation of duties, escalation |
| `persona-cfo` | CFO — sign-off thresholds, dashboard/analytics math, spend authorization |
| `persona-accountant` | AP clerk / accountant — coding, 2/3-way match, credit memos, month-end, 1099 |
| `persona-usa-business` | US customer — USD, EIN/W-9, 1099, ACH routing, checks, sales/use tax |
| `persona-uk-business` | UK customer — GBP, VAT, HMRC/MTD, sort codes, BACS/CHAPS, reverse charge |
| `persona-south-africa-business` | ZA customer — ZAR, 15% VAT, SARS, 6-digit branch codes, EFT |
| `persona-card-processor` | Lithic/Nium integration — card lifecycle, webhooks, rebate math, PAN reveal |
| `persona-payment-processor` | Bank / payment rail — ACH/wire/check/SEPA, webhook idempotency, settlement, FX lock |
| `persona-supplier` | Vendor using the supplier portal — submission, payment visibility, isolation |
| `persona-fraudster` | Adversary — duplicate/split invoices, BEC bank-detail swap, replay, cross-tenant probing |
