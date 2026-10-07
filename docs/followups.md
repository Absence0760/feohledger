# Open follow-ups

**Open items only.** Completed items are pruned as they land — the code is in
git history and the reasoning belongs in [decisions.md](decisions.md). This file
should shrink as often as it grows.

Every item here is one of:

- **(a)** blocked on an external credential, account, vendor engagement, or
  hardware we don't have;
- **(b)** an operator step on code that is already merged;
- **(c)** a sized-but-unstarted piece of work, or a deferred-with-reason finding
  awaiting a product or architecture call.

This is the destination root `CLAUDE.md` guard rail 6 demands for a deferral —
"deferred / recommended" in a report is a staging area, not an end state. An
item lands here **with its category, the durable fix, and the trigger to do it**,
or it doesn't get deferred.

**What does not belong here:**

| That | Goes here |
|---|---|
| Diagnosed defects with a root cause | [known-issues.md](known-issues.md) |
| Scope + status of work still open | [roadmap.md](roadmap.md) |
| Scope of work already shipped | [roadmap_shipped.md](roadmap_shipped.md) |
| Why something was built the way it was | [decisions.md](decisions.md) |

Each open roadmap section carries an `**Open:**` line naming what's left; the
matching entry here carries the category, durable fix, and trigger. Keep the
pair consistent — if an item leaves this file, its roadmap section either loses
its `**Open:**` line or moves to the archive.

[#321](https://github.com/Absence0760/feohledger/issues/321) is the GitHub-side
entry point, but it is **a pointer to this file, not a mirror of it** — it holds
no item list to keep in sync. It was a transcribed checklist until 2026-09-22,
and an audit that re-derived both found the copy stale at nearly every sync
(`61` against an actual `51`, `46 (c)` against an actual `36`), which is why the
transcription was retired rather than corrected again. Add a follow-up here; add
a GitHub issue only when one warrants its own thread.

**Last reconciled:** 2026-10-06 — the no-rail pilot (issue #517, decisions §251)
opened three (c) entries — mobile record-only parity, the balanced NACHA file and
the ERP-reported payment date — taking the file from 72 → 75. Before that, 2026-10-06 — the round-2 issues batch (PR #514) closed
**twelve** (c) entries and opened five, taking the file from 59 → 52: the unmasked
DSAR export's step-up, the stale `/legal/privacy` §12 and the 1099 headline total
(decisions §237), the English-only money-path refusals in both entries that
tracked them (§238), the unaudited `settings.sso` writer and the missing
break-glass (§239), exception descriptions and PO-match issues in server English
(§240), report SoD naming only the owner (§241), the dispatch session a database
error aborted (§242), and the pytest-split baseline nothing regenerated (§243).
Before that, 2026-10-05 (evening) — a five-agent issues batch closed
**ten** (c) entries and opened three, taking the file from 66 → 59: the legal
table scrollers' names and the consent banner over the contents rail (decisions
§230), step-up refusals on `/profile` plus its hand-rolled shell (§231), the
English-only GL-chart refusal and the recurring form's free-text GL field
(§232), the unbounded invoice-lock wait in payment dispatch (§233), the
exception `amount` on the wire (§234), requisition material editors (§235) and
the budget-delete race (§236). Before that, 2026-10-05 — the bug-hunt, UX, a11y and perf PRs of that
day (#484–#509) landed together and took the file from 53 → 66: sixteen (c)
entries opened across them and three closed (#508 the mobile multi-select entry
#487 opened, #505 the two by-id entity-scope entries #500 opened). Merging #484
and #487 back to back left `main` claiming 54 over a file of 55 until the rest
landed: each PR re-derived the headline against its own base, and the guard
only sees one branch at a time. Before that,
2026-09-23 — a five-agent batch closed **five** (c)
entries and opened none, taking the file from 58 → 53: `gl_recode`'s org-wide
empty-chart read (decisions §207), the missing per-test timeout on the backend
suite (§208), the dead Change-password card for a password-less account (§209),
the public SSO / SAML routes' 500 on an unresolvable IdP block (§210), and the
reporting currency `GET /api/organization` knew but did not serve (§211).
Before that, 2026-09-22 — the follow-up batch, eight agents each in its
own worktree, closing what the #321 / #443 batch had opened earlier the same
day. **Ten entries closed**, one narrowed, **fourteen opened**, so the file went
50 → 54; the new ones are grouped under their own heading below. All eight
entries the #321 / #443 group held are now closed, so that heading is gone, and
with it round 31's mobile-currency and CI-run sub-headings.
Earlier on 2026-09-22 the #321 / #443 batch itself — five agents, each in its own
worktree — closed **nineteen**, narrowed two and opened eight, taking the file
from 61 → 50. Before that, 2026-09-17 (round 26) closed eleven plus the
engineering half of
[#432](https://github.com/Absence0760/feohledger/issues/432) and opened nine, and
a housekeeping pass the same day pruned 54 checked entries, 21 emptied sections
and two prose-only CLOSED narratives, taking the file from 2570 lines to 1215;
nothing open was removed by it.

**This file had stopped obeying its own first rule.** "Open items only" is the
line at the top, and 54 `[x]` entries plus their surrounding CLOSED narrative
had accumulated beneath it — more than half the file describing work already in
git history, with its reasoning already in [decisions.md](decisions.md). A
reader looking for what is open was reading mostly what is not. Every pruned
section carried its own `decisions.md` § reference, so nothing was lost by
deleting it; that cross-reference is what makes the pruning safe, and writing
one is what earns a future entry the right to be deleted.

**76 open: 61 (c) · 9 (a) · 6 (b)** — re-derived from the file, never carried
forward. The section heading is authoritative; where an entry also carries a
`(c)`/`(a)`/`(b)` marker, the two agree.
`grep -c '^- \[ \]' docs/followups.md`.

That line said `52 open: 37 (c)` while the file held 59, because "re-derived,
never carried forward" describes an intention and nothing enforced it: the seven
`/polish-ui` entries were appended without it being touched. Two shapes made the
drift invisible and are worth not repeating — an entry written as prose under a
heading with **no `- [ ]` checkbox** is an open item the count command cannot
see, and an entry whose `(c)`/`(a)`/`(b)` marker disagrees with the section it
physically sits under (the shard-baseline entry was labelled `(c)` beneath
`## (b)`) breaks the "section heading is authoritative" rule that makes one
count possible. Re-derive with the command above, per section, and check the
three sum to the total.

**It drifted again after that was written, the same way.** #455 appended the
dashboard-greeting entry without touching the header, so `main` carried
`50 open: 35 (c)` over a file holding 51 / 36 (c), and the batch after it
inherited the off-by-one (`54` over an actual 55). Neither shape above explains
this one — the entry is a well-formed checkbox under the right heading. The
lesson is simply that a count stored beside the thing it counts has no way to
notice, so re-deriving it is the last step of any change that adds or prunes an
entry, not a periodic clean-up.

**Then a third time, which is what finally bought a guard.** #467's three
settings-panelisation entries left `55 open: 40 (c)` over a file holding
58 / 43. At that point the paragraphs above had documented the failure twice
and stated the lesson once, and it happened anyway — so the discipline was not
the fix. `pnpm check:followups`
([`scripts/check_followups_count.mjs`](../scripts/check_followups_count.mjs))
now re-derives the total and each category and **fails CI** when the header
disagrees, in the same Frontend job as the sub-processor register check. It
also asks the two questions the shapes above describe: whether the three
categories sum to the total (which catches an item stranded outside every
`## (x)` heading), and it counts by section heading only, never by an entry's
own marker. Re-deriving by hand is still the right habit — the guard just means
forgetting is now a red check rather than a number nobody can trust.

Round 26 closed eleven and opened nine, so the file shrank by two. **Three of
the eleven were wrong about their own code**, and twice in the direction that
would have caused damage if implemented as written: the GL-picker entry
prescribed binding the uuid `id` in `InvoiceModal`, which would have written
UUIDs into a `String(100)` column that eight services parse as a code; and the
money entry prescribed a fix contradicting its own helper's contract
(`MoneyAmount` emits a JSON number, not the string the entry asked clients to
parse) while undercounting the defect 51 sites to one. The habit that caught
both was verifying the entry against the code before implementing it.

The previous line said 53 · 40 · 9 · 4. The total moved by one, and both halves
of that are worth stating: one entry **closed** — the Dependabot pip-grouping
hypothesis, whose own confirm condition was met by PR #410 grouping three
backend bumps onto one `backend-minor-patch` branch — and two were **added**,
the untracked consent-banner i18n gap and the lockfile-credential step that had
been sitting in this file as an unboxed paragraph the count could not see. The
category split moved further: three entries marked `(b)` sat under the `(c)`
heading, so the file disagreed with itself about what an operator step was.
They are in the `(b)` section now. A follow-up file that miscounts itself is the
same failure `known-issues.md` fixed in its own header.

The legal-set entries are worth reading as a group rather than as separate
chores: several are the same shape — a document now makes a **published
commitment** (a monitored mailbox, 30 days' sub-processor notice, a minimised
screening payload) that the code does not yet fully back. That is a deliberate
trade, not an oversight: the alternative was to publish nothing, or to publish
something weaker than what we intend to do. It does mean each one is now a
promise with a reader, which is a higher bar than an internal TODO. Two of them
are now backed by a CI guard rather than by intent — see the sanctions and
register entries' replacements in the same commits that closed them.

**The total went up, and the reason is the same one round 30 recorded.** All six
entries turned out to be wrong about their own work rather than merely
incomplete, and in three cases implementing the entry as written would have
shipped a defect. Each slice's corrections are findings in their own right, and
the honest destination for one that cannot be fixed where it was found is a new
entry.

**A docstring asserting a mechanism is not evidence of one, and neither is a
follow-up entry.** Round 30 recorded the first half on the login path. Round 31
found the second: the `needs_update` entry's own durable fix ("re-hash the
just-verified plaintext and assign it") was a lost update and a 500-on-correct-
password waiting to happen, the mobile entry skipped a rung of the
reporting-currency chain it claimed to mirror, and the GL entry instructed a nav
row be gated on a write when gating it on the write is what had hidden the page.
All three were caught by writing the test before trusting the prescription
(§160, §161, §164).

**An entry's file list and its counts are the least reliable part of it, for the
seventh round running.** "four screens" was eleven call sites across ten files.
"both surfaces that render it" was three. "three assertions move with it" was
two — and two of the tree's four status assertions turned out to be
case-insensitive regexes that passed against the raw value either way, i.e. not
assertions about this at all. `po_mismatch` carries five sentences, not four,
and `quality_hold` five, not three. The durable habit is to re-derive the site
list from the column or the vocabulary, never from the note (§157, §158).

**A deferral conditioned on "whenever someone owns that file" belongs in this
file, not in `decisions.md`.** §155 deferred widening the exception type-label
map to `/exceptions` on exactly that condition and recorded it only in
append-only history, where the person who eventually owned the file would never
look. It shipped `po mismatch` beside the server's `PO Mismatch` for a further
round as a result. Owning the file is what closed it (§158).

**A guard that cannot fail reads as coverage.** Two tightened e2e assertions and
one vacuous inset check, on top of round 26's finding of the same shape. Where a
tolerant accessor is safe *because* of a guarantee elsewhere — `api/admin.py`
refusing a custom role named after a built-in, which is the only thing keeping
`ROLE_LABEL_KEYS` from translating tenant data — the guard belongs on the
guarantee, not on the accessor (§159).

**A maintenance write riding a user-facing request must be unable to change that
request's outcome.** The credential upgrade may fail, and it then has to be
silent, contained in a SAVEPOINT, and honest in a log line — never a 401, never
a 500, and never a rollback reaching past the statement it was cleaning up after
(§164).

**Where nothing can be proven, render nothing rather than a default.** A missing
currency symbol is a visible gap a reader can ask about; a substituted `$` is a
wrong number that looks right. The same call §79/§82 made for
`PaymentResponse.currency`, now general on mobile — and it immediately surfaced
that the web still substituted USD. §196 and §200 have since taken that out of
`formatMoney`, the per-currency grouping helpers and the `orgCurrency` store, so
both surfaces now render an unprovable code bare (§160, §196, §200).

**A held item is still an open item, and the hold has to be re-checked.** The
header claimed for eight rounds that exception resolution "stays held" as the
file's only segregation-of-duties entry. It had in fact shipped — `#408`,
`services/exception_lifecycle.segregation_refusal`, migration `0098`
([decisions.md](decisions.md) §169–§170) — and the inter-company mirror entry that
replaced it closed in the 2026-09-22 batch (§192), with the data backfill for
mirrors routed before it following as migration 0100 (§198). A standing note
nobody re-reads outlives the thing it describes.

## (c) Feature work — sized and unstarted

### The published legal set has loose ends

- [ ] **No mechanism backs the 30-day sub-processor notice.** ([#427](https://github.com/Absence0760/feohledger/issues/427)) `/legal/sub-processors`
      and the DPA both commit to 30 days' advance notice of a new sub-processor
      and a right to object. The mechanism today is "this page is updated and a
      dated change-log row is added" — there is no notification list, no
      subscribe endpoint, and no email template that reaches a customer's DPO.
      The commitment is contractual, so the gap is a promise we cannot currently
      keep on the notification half.
      **Durable fix:** a per-org notification (the admin contact already exists,
      and `email_adapters` can send) fired from a changelog entry, so adding a
      register row and notifying are one action rather than two. `~/github/threkir`'s
      `docs/compliance/sub-processor-changelog.md` is the shape to copy for the
      dated-entry half.
      **The other half of #427 has landed:** `pnpm check:subprocessors`
      (`scripts/check_subprocessor_registry.mjs`, CI's Frontend job — the
      Compliance-drift workflow's summary points at it rather than running it
      twice) now fails when a registered adapter is missing from
      `docs/sub-processors.md` or a third-party processor it names never
      reaches the published page. It reads both registration shapes — the
      `@register_*_adapter` decorator and a module-level registry dict — and
      reports any provider family whose registrations it cannot read at all, so
      a family cannot again be silently invisible the way
      `email_intake_adapters/` was. So the registers can no longer drift from
      the code silently — what is still missing is telling customers when they
      change.
      **Trigger:** before adding or changing any sub-processor.

### The pricing page and the billing code describe different products

- [ ] **Marketing prices do not match the plan catalogue.** ([#426](https://github.com/Absence0760/feohledger/issues/426))
      `frontend/src/lib/components/marketing/Pricing.svelte` sells "Pro" at
      $29/seat/month ($24 annual, 5-seat minimum) with a monthly/annual toggle.
      `backend/app/services/billing/plan_catalog.py` has flat monthly plans —
      `free` $0, `growth` $49, `scale` $199 — with no seat pricing and no annual
      interval (`services/billing/period.py` is months-only). The free tier's
      advertised "50 invoices/month, 2 seats" caps are not enforced anywhere
      (`free` carries `entitlements: {}`).
      The claims that were outright false were corrected in the legal-pages change
      — a "SOC 2 attestation" and a "99.9% uptime SLA" that do not exist, a
      `sales@feohledger.example` CTA on the reserved `.example` TLD, a
      "Start 14-day trial" button that routes to the same signup as the free plan
      (`tenant_provisioning._provision_into` binds every new org to `free`, and
      there is no plan selection anywhere in signup), and two fabricated landing
      statistics ("3.2s avg. extraction time", "97% field accuracy" — the latter
      traceable to Basware's published *touchless processing rate* quoted in
      `docs/competitive-analysis.md`, i.e. a competitor's number for a different
      metric).
      **What remains is the pricing model itself, which is a product decision.**
      Two halves: the prices and interval above, and the fact that **no
      plan-differentiation claim on that page is enforced anywhere in the
      backend.** `require_entitlement` gates exactly one thing (`public_api`);
      SSO, SAML and SCIM carry no entitlement check at all, so a Free tenant can
      turn on the feature the page sells as Enterprise-only, and the advertised
      "50 invoices / month" and seat counts are not enforced either (`free`
      carries `entitlements: {}`). Selling a premium feature everyone already has
      is the sharper half — a paying customer has a claim.
      **Durable fix:** decide the real pricing, make one of the two sides match
      (rendering the grid from `plan_catalog` would stop it drifting again), and
      implement the entitlement checks the page implies — or describe only what
      `Plan.entitlements` actually gates.
      **Trigger:** before billing is switched off the `mock` adapter, or before
      any real traffic reaches the pricing page — whichever is first.

- [ ] **No substantiation file backs the remaining marketing numbers.** The two
      invented statistics are gone and the rest are now countable from source
      (7 payment rails, 9 workflow step types, 6 locales, the 1% default rebate
      rate in `api/cards.py`), with a comment in `Landing.svelte` saying so. But
      a specific numeric claim is an objectively verifiable factual claim, and
      the durable habit is a file that records how each was derived, so a
      challenge is answered from a record rather than a re-derivation.
      **Durable fix:** a short substantiation note per public number, refreshed
      whenever the underlying count moves — and a real extraction-accuracy
      benchmark before any accuracy figure is published again.

### One adapter family still ships code no caller reaches

- [ ] **`services/financing_adapters` has no production caller.** The
      supply-chain-finance family (`mock` + the `c2fo` skeleton) is built,
      registered and tested, but nothing in `app/` selects a financing provider
      or requests funding. The Protocol violation that used to sit here is
      **fixed** — `C2FOAdapter.quote` / `.request_funding` return an ineligible
      `FinancingQuote` / unfunded `FinancingFundingResult` with
      `reason="provider_not_implemented"` rather than raising
      `NotImplementedError`, pinned by three tests in
      `tests/test_financing_adapters.py`.
      **Why still deferred:** unlike the corridor auction (now wired as an
      advisory read — [decisions.md](decisions.md) §42), financing has **no safe
      read-only half**. A financing quote is only meaningful if it can be
      accepted, and accepting it moves money to a supplier from a third-party
      financier — so wiring it up *is* the product decision about whether the
      platform offers supply-chain financing at all, not a step toward it.
      **Durable fix:** a product call, then the accept path with its own
      approval + audit story.
      **Trigger:** a decision to offer supplier financing.
      Ref: `backend/docs/dynamic-discounting.md`.

- [ ] **`modern_treasury` publishes no fee table, so it is skipped by the
      corridor auction.** `compare_quotes` now has a production caller —
      `POST /api/payments/corridor-quotes`, advisory and read-only — but an
      adapter with no fee schedule correctly reports `no_quote_endpoint` and
      drops out of the ranking, so a tenant on Modern Treasury sees an auction
      its own rail never enters.
      **Durable fix:** its real pricing, transcribed into the adapter's fee
      table. This is data, not code.
      **Trigger:** obtaining Modern Treasury's contracted pricing.
      Ref: `backend/docs/international-payments.md` § Multi-route quote
      optimization.

### E-invoice conformance is checked by our own code, not the official validators

Both generators were corrected this round to meet their standards
([decisions.md](decisions.md) §44, §45), and both are pinned by structural tests.
Neither is validated against the authority that will actually judge it.

- [ ] **BIS Billing 3.0 conformance is our own re-implementation, not the
      official Schematron.** NARROWED (round 21). The calculation rules
      (BR-CO-*) and code-list membership this entry named are now implemented
      (`services/e_invoice/{en16931_rules,codelists}.py`) and CI-tested, each
      rule with a satisfying and a violating document. Adding them immediately
      earned its keep: the generator mapped `Invoice.subtotal` into **both**
      BT-106 and BT-109, so every invoice carrying a discount or a shipping
      charge went out contradicting itself on BR-CO-13/15/17 with our
      conformance claim on it — fixed at the source in `mapper.py`
      ([decisions.md](decisions.md) §90).
      **What is still open:** (a) rules whose inputs the normalized model has no
      slot for — allowance/charge detail, invoicing periods, VAT point dates
      (tabulated in `en16931_rules`' docstring); (b) membership for UN/ECE Rec 20
      units, UNCL4461 payment means and the CEF EAS scheme list, which get a
      *shape* check only because a partial list would 422 a genuinely conforming
      send; (c) the fact that a pass still means "nothing we can compute
      objects", not conformance. The asymmetry that makes the conditional
      declaration sound is unchanged: a **failure** provably does not conform.
      **Durable fix:** vendor the official EN 16931 + PEPPOL Schematron into
      `backend/tests/fixtures/` and assert generated documents validate in CI.
      Not done this round on purpose — this is a public repo and vendoring
      externally-licensed validation assets is a call to make deliberately.
      **Trigger:** the PEPPOL `as4_gateway` slice.

- [ ] **No FatturaPA XSD in the repo, so the generator is validated by
      inspection.** The root-only namespace-qualification fix cites the v1.2
      schema's `elementFormDefault` default and is pinned by a structural test,
      but nothing validates a generated instance against the real XSD.
      **Durable fix:** vendor the v1.2 XSD into `backend/tests/fixtures/` and
      assert the document validates.
      **Trigger:** the SdI clearance slice.

### Surfaced by the round-18 parallel sweep (2026-09-05)

One item remains of the four the round-18 agents traced to a file and line but
correctly did not fold into their own slice. It is not a defect that can bite
today.

- [ ] **(c) No live payment adapter consumes the wire ABA yet.**
      `resolve_routing_number` picks the right routing number per rail, but every
      shipped adapter identifies the payee by a processor **counterparty token**
      and transmits no raw bank coordinates — so today the resolver's only live
      consumers are the `mock` adapter and Positive Pay's ACH file (which reads
      the ACH number and is correct unchanged). The wire number is stored,
      staged under dual control, and surfaced; it is not yet transmitted.
      **Why it is not a defect:** the field had to exist before a counterparty
      provisioning path could send it, and the resolver is what makes the rail
      distinction unambiguous when one arrives.
      **Durable fix:** counterparty provisioning at the processor, which this
      codebase does not model at all.
      **Trigger:** wiring a payment adapter that hands a bank raw coordinates.
      See [decisions.md](decisions.md) §74.

### Surfaced by the issue #328 checklist reconciliation (2026-08-27)

Reconciling all 56 persona-panel findings and 8 acknowledged gaps against `main`
parked what was genuinely open here, so issue #328 could close — the checklist
itself is not a destination (guard rail 6). **Two of those findings remain**,
plus two product-fit gaps in the section below. Both are category **(c)**.

**Progress — PR #343** (`feat/portal-invoice-search-filter`):

| #328 finding | Status in #343 |
|---|---|
| Portal invoice list — no status/number filter | **done** — repeatable `status=` + `search=`, vendor-facing phase chips |
| Portal payment list — no status/number filter | **done** — same, via shared `PortalListFilters.svelte` |
| Vendor can't see why an invoice was rejected | **done** — `rejection_reason` on the portal API + rendered under the status pill |
| No resubmit path for a rejected portal invoice | **done** — `POST /portal/invoices/{id}/resubmit` + "Revise & resubmit" row control |
| URL filter/search persistence partial on `/invoices` `/payments` `/vendors` | **done** — `search` + status chip + (payments) tab now in the query string |
| No onboarding empty-state / CTA for a zero-data tenant | **done** — shared `ui/EmptyState.svelte`, adopted on the dashboard, `/invoices`, and `/portal/invoices` |
| No UI to create a vendor / invite one to the supplier portal | **done** — `+ New Vendor` header action (`CreateVendorModal`) + `Invite` row action (`InviteVendorPortalUserModal` → `SecretReveal`) |
| `/payments/queue` has no pagination | **done** — `?page=` on `GET /queue`, a `GET /queue/ids` select-all resolver, Load-More + whole-set select-all on the Queue tab |
| GBP→GB domestic payment falls through to `international_wire` | **done** — `bacs`/`faster_payments`/`chaps` rails + a GBP/GB branch in `pick_corridor` (Faster Payments, no SWIFT/FX/IBAN) |
| [Low] Org Settings has no first-time-admin prioritization | **done** — a "Getting started" wayfinding strip at the top of `/organization` |
| Portal lists have no date-range filter | **done** — `date_from`/`date_to` on both portal list endpoints + a From/To pair in `PortalListFilters` |
| Vendor can't see why an invoice is *stuck* | **done** — `waiting_on` bucket (`review`/`processing`/`erp`) + `waiting_on_days` on the portal invoice API, rendered under the status pill |
| _remainders_ | constrained re-extract on resubmit (scoped, deferred — its own slice) — entry below |

**Frontend gaps — built on the backend, unreachable in the product:**

- [ ] **[Low] The marketing pricing page (`Pricing.svelte`) is USD-only.**
      Hardcoded `$` figures; no currency awareness.
      **Durable fix:** a product call on whether to localise pricing at all, then
      per-locale figures if yes.
      **Trigger:** an international pricing decision.

**Supplier portal — the loop-closing steps are missing:**

**Volume surfaces:**

- [ ] **The invoice tax model has no rate category or reverse-charge flag, and
      UK domestic (same-country) VAT reverse charge is structurally
      impossible.** `Invoice`/`InvoiceLineItem` carry one flat
      `tax_amount`/`tax_rate`; `international_tax/vat.py` hardcodes domestic
      reverse charge to `False` and models GB as non-EU, so the UK CIS domestic
      reverse charge can never be expressed, and the `/api/international-tax`
      calculator is never wired to a real invoice.
      **Durable fix:** a tax-treatment design session — per-line rate category +
      a reverse-charge flag on the line, the calculator wired into the invoice
      lifecycle, and a real frontend for it. Explicitly scoped out of the
      parallel bug-fix rounds as architecture, not a fix.
      **Trigger:** a decision to support UK/EU VAT properly (a prerequisite for
      the UK-business go-to-market).

**Investigated, deliberately not changed (recorded so it isn't re-litigated):**

- `approve_payment_run` stays on `require_roles(ROLE_CFO)` rather than
  `require_permission(PERM_PAYMENT_RUN_APPROVE)`. Migrating it was tried and
  reverted during #330 — it let non-CFO admin/ap_manager bypass the
  CFO-approval-threshold control (a real regression caught by CI). The inline
  comment in `backend/app/api/payments.py` is the durable record.

### Persona-panel acknowledged gaps — ⚠️ PRODUCT REVIEW NEEDED (issue #328)

Two capabilities (of an original eight) the personas confirmed absent and classified as *product-fit
gaps* (the app never claimed them), not defects. **None is a bug — each is a
deliberate scope decision waiting to be made.** For each: **keep** it (→ add to
`docs/roadmap.md`, size it) or **drop** it (→ record as a documented non-goal in
`docs/competitive-analysis.md` / the relevant doc so it isn't re-filed every
persona round). The `[ ]` is checked when the keep/drop call is recorded, not
when the feature ships.

A suggested lean is given per gap — **`lean: keep`** / **`lean: drop`** /
**`lean: ?`** (genuine toss-up) — to make the review a yes/no rather than an
open discussion. Owner: a product/founder call; nothing here is Claude's to
decide.

- [ ] **No US sales/use-tax self-assessment** — no self-assessed use tax on
      out-of-state purchases, no nexus tracking, no resale/exemption
      certificates. `Invoice.tax_rate` only records what the vendor charged. US
      AP table stakes above a certain company size. **`lean: keep`** (real US
      mid-market requirement; large, own epic).
- [ ] **No saved views / per-list default view, and no keyboard shortcuts or
      command palette** anywhere in the app. **`lean: ?`** (power-user polish;
      high effort, diffuse payoff — defer unless a design partner asks).

### Surfaced by the round-29 batch (2026-09-10)

One entry remains of what round 29 could not close in the slice that found it.

- [ ] **(c) ⚠️ PRODUCT REVIEW NEEDED (issue #328) — does AP create early-pay
      offers by hand?** `POST /api/discounts/offers` has no frontend caller. Unlike
      `bulk-negotiate`, that may be *correct*: an offer normally arrives **from**
      the supplier — the portal's own accept/decline surface, or the
      `financing_adapters` marketplace — so a manual AP-side create may be a
      deliberate absence rather than an unwired feature. Re-filed on its own
      because §150 closed the schema half beside it and a closed entry is the wrong
      place to keep a live question.
      **Durable fix:** the product call, then either a `/discounts` create surface
      or the endpoint's removal.
      **Trigger:** the #328 product review.

### Surfaced by the round-30 batch (2026-09-11)

One of the eight entries this round opened remains — a finding that could not
honestly be folded into the slice that surfaced it.

- [ ] **(c) The adaptive APPLY paths and the Feedback tab have no mobile
      counterpart, by design — but "by design" is a decision with an expiry.**
      `POST /adaptive/routing-suggestion/apply` and
      `POST /adaptive/threshold-recommendation/apply` were deliberately left off
      the mobile screen (decisions §156): the first reassigns a live approval,
      the second raises the org-wide `auto_approve_below`, and the second's
      `expected_recommended_threshold` 409 needs a persistent surface naming
      both figures plus the recomputed recommendation. `GET /feedback` is out
      separately because it writes an `adaptive_feedback.viewed` access-audit
      row and therefore needs an explicit "only when asked" load rather than
      riding along with two eager tabs.
      **Durable fix:** if an operator asks for either on a phone, design the
      stale-guard state and the audited-read trigger FIRST — a phone-sized copy
      of the web control with the explanation cut off is worse than its absence.
      Routing-apply is the cheaper of the two (no optimistic guard); the
      threshold apply should stay web-only until its refusal has somewhere to
      live. **Trigger:** a request for either control on mobile.

### Surfaced by wiring the FeohLedger AWS account (2026-09-14)

- [ ] **(c) The GitHub deploy role exists but can do nothing.** The estate
      account bootstrap created `feohledger-deploy` in the FeohLedger account,
      trusted only from this repo's `production` environment, and attached no
      policy — on purpose. `aws-deploy.yml` needs ECR push, ECS
      register/update + `run-task`/`describe-tasks`, `iam:PassRole` on the task
      and execution roles, `lambda:UpdateFunctionCode`, S3 sync and CloudFront
      invalidation (`docs/production-deployment.md` § Credentials), and every
      one of those targets a resource `infra/` does not define yet. A policy
      written today would either be `*`-wide or name ARNs that do not exist, so
      nothing was written. **Durable fix:** an `aws_iam_role_policy` on the
      bootstrap role — looked up with `data "aws_iam_role"`, never managed from
      here — in the same change that adds the ECR repository, ECS service,
      frontend bucket + distribution and worker Lambdas, scoped to exactly those
      ARNs. That change also reads its first real secret (the RDS master
      password) through the `carlpett/sops` data source `infra/README.md`
      § Secrets sketches, likewise unwired until something needs it.
      `AWS_DEPLOY_ENABLED` stays unset until both land. **Trigger:** the
      workload-stack build-out — step 1 of `docs/production-deployment.md`
      § Arming the AWS pipeline. **See also #449**, which is the same role from
      the other side: its *trust* policy named a subject GitHub no longer issues
      (this repo is on immutable subject claims). That half is fixed; what is
      left there is dispatching `AWS OIDC preflight` to prove the assume, which
      does not need this policy.

### Surfaced by the round-26 parallel batch (2026-09-17)

Five agents, each in its own worktree, closed eleven entries and issue #432's
engineering half. Every one of these was found by doing the work rather than by
reading the entry — and **three entries were materially wrong about their own
code**, twice in the direction that would have caused damage. That is now the
seventh round running where an entry's own account of its scope was the least
reliable part of it.

- [ ] **(c) `Invoice.gl_account` stores a code, so a renamed or retired account cannot be traced.**
      Round 26 confirmed the shape while auditing the picker: `Invoice.gl_account` and
      `InvoiceLineItem.gl_account` are `String(100)` **codes**, while `Expense`,
      `RequisitionLine` and `CatalogItem` hold real `ForeignKey("gl_accounts.id")`. Eight
      services read the invoice column as a code (`budget_service`, `matching_rules`,
      `tax_1099` glob patterns, `gl_recode`, workflow `RoutingField`, `report_builder`,
      `vendor_enrichment`, the extraction prompt catalog), plus `RecurringInvoiceTemplate`,
      `ContractLineItem` and the org default. `api/gl_accounts._code_in_effective_chart`
      states the consequence itself: *"an invoice records the code as a STRING, so which
      account it was coded to becomes unanswerable."* It is also why the new PATCH refuses
      to make `code` mutable, and why hard-deleting an account orphans posted lines
      **silently** — no constraint fires.
      **Durable fix:** a real FK alongside the string, backfilled by code within each
      entity's chart, with the string kept as the historical record. Large: a tenant
      migration, a backfill with an unresolvable-code policy, and eight read sites.
      **Do NOT** meanwhile "fix" the pickers by binding the uuid — round 26's entry
      prescribed exactly that and it would have written UUIDs into a column eight services
      parse as a code.
      **Trigger:** the first request to rename or merge a GL account, or any work on
      cross-entity coding.

- [ ] **(c) Breakpoints are ad hoc — the deferred half of [#432](https://github.com/Absence0760/feohledger/issues/432).**
      Round 26 closed #432's two engineering parts (the `/organization` 320px failure and a
      reflow guard widened from 5 routes to 45) and deliberately did **not** make the
      viewport-floor call, which is a product decision. Measured while there, correcting the
      issue's own premise that there are "zero width breakpoints": **30 width-based `@media`
      queries across 15 distinct values** (`400, 420, 520, 600, 620, 640, 700, 720, 760,
      768, 800, 900, 960, 1100px` and `52rem`), zero container queries, zero shared tokens.
      **Product call first:** what viewport floor do we support, and what are the two or
      three named steps? Then: declare them once (CSS custom properties cannot be used in
      `@media`, so this is documented literals plus a stylesheet guard rejecting unlisted
      values — the `targetSizeAudit.test.ts` shape — or a preprocessor), migrate 30 call
      sites, and decide sidebar behaviour below the floor and whether `/exceptions`' 11
      columns drop or keep scrolling.
      Worth weighing: five of round 26's seven reflow fixes needed **no breakpoint at all**
      — `flex-wrap`, `min-width: 0`, a flex basis, an intrinsic scroller. Tokens are for
      the cases intrinsic reflow genuinely cannot reach.
      **The 320px reflow guard does not depend on this** and must not be folded into it:
      320px is WCAG's own number. A floor decision adds steps above it.
      **Trigger:** the product call.

### Surfaced by the follow-up batch (2026-09-22)

Eight agents, each in its own worktree, closed ten entries — all eight the
[#321](https://github.com/Absence0760/feohledger/issues/321) /
[#443](https://github.com/Absence0760/feohledger/issues/443) batch had opened earlier the
same day, plus the two round 31 had left narrowed (PO currency, CI service images) — and
narrowed one more (§197–§204). These fourteen are what that work found and could not
honestly fold in: each is a product call, needs a tenant migration or an operator script,
or is a sibling of a fix that needs its own pass.

- [ ] **(c) A PO that records no currency can never be given one, so its invoices stay unverifiable.**
      Migration 0099 backfilled `purchase_orders.currency` from the originating requisition and
      left every other PO NULL by design (§197). A NULL is honest, but it is also permanent for a
      tenant whose POs came from the seed, a contract or an ERP that states no code: there is no
      `PATCH /api/purchase-orders/{id}` at all (the router is list / detail / sync-erp), so the
      only way a legacy PO ever gains a currency is an ERP re-sync that supplies one. The cost is
      not cosmetic: `po_matching` reports `currency_check: "unknown"` forever, the invoice modal
      carries the face-value note on every matched invoice, and `amount_mismatch_v1` /
      `missing_po_v1` / `multi_po_split_v1` escalate instead of resolving — so a tenant sitting on
      pre-0099 POs loses agent autonomy on exactly the invoices that used to auto-resolve.
      **Durable fix:** an operator backfill in the shape of `scripts/backfill_import_provenance.py`
      — the operator ASSERTS the currency for a named set (a tenant, an entity, a `po_number`
      prefix, or every PO created before a cutover), the script stamps only rows still NULL, dry
      runs by default, and writes a PII-free audit manifest. A `PATCH` route is the alternative and
      is worse on its own: it re-prices one PO at a time through a surface that does not exist yet,
      while the actual shape of the problem is "these 4,000 rows were all USD".
      **Trigger:** the first tenant that notices its agents stopped auto-resolving PO mismatches,
      or the first support request to label legacy POs.

- [ ] **(c) A PO flip still books its invoice in USD when the PO records no currency.**
      `POST /api/portal/purchase-orders/{id}/flip` now takes the PO's own currency (§197), but
      `invoices.currency` is `NOT NULL`, so a PO with none falls back to the column's historical
      `"USD"` — a supplier flipping a currency-less EUR order gets a USD invoice carrying the EUR
      figure. The pairing is never mistaken for proof (the matcher reads the PO's NULL, not the
      invoice's placeholder, and reports the leg unverified), and the same `"USD"` is what
      `portal.submit_invoice` writes for an un-extracted upload — but it is still a claim nobody
      made, on the one row the payment path ultimately funds.
      **Durable fix:** the honest version is `invoices.currency` nullable, which is a tenant
      migration plus an audit of every consumer that assumes a code (the reporting-currency
      materialization, `payment_runs.one_currency`, the e-invoice writers, every serializer), and
      is worth doing once rather than per-surface. The cheap intermediate — resolving the org's
      `invoice_defaults.currency` at flip time — is explicitly NOT the fix: it is the org-currency
      proxy §196/§197 refused, applied to a row that then looks established.
      **Trigger:** the first tenant running the supplier portal in more than one currency, or the
      next change to `invoices.currency`.

- [ ] **(c) Invoices an employee created before §131 still carry a NULL uploader, although the audit trail names them.**
      Before §131 (2026-09-09), CSV import (`POST /api/invoices/import-csv`) and
      recurring `generate-now` built the `Invoice` without `uploaded_by_id`. Each still
      wrote an audit row keyed to the invoice with the acting employee in `actor_id`:
      `invoice.imported_csv` for the import, and `invoice.created` with
      `details.source = "recurring_template"` for generate-now (the sweep's copy of
      that row has `actor_id` NULL). `approval_chain.violates_segregation` reads a NULL
      uploader as "no employee created this", so on any such invoice not yet `done`,
      the importer or generator can still approve it or clear a payment-blocking
      exception on it. §198 closed the inter-company mirror's version of this gap from
      the routing audit row. These two paths have the same kind of evidence and
      were left out only because they are not mirrors.
      **Durable fix:** a tenant data migration on the pattern of
      `0100_mirror_implicated_backfill`. Set `uploaded_by_id` from the earliest matching
      audit row's `actor_id`, only where the invoice's is NULL, the row's is not, and
      the status has a successor in `VALID_TRANSITIONS`. Write an
      `invoice.segregation_backfilled` row (actor NULL, old/new) for each change, and
      test it against real audit rows the way `test_intercompany.py`'s 0100 section
      does. Confirm first that no other pre-§131 employee path produced NULL (manual
      create and upload always stamped the uploader).
      **Trigger:** the next round that ships a tenant migration, or the first tenant
      found with a pre-2026-09-09 CSV import or generate-now invoice still in flight.

- [ ] **(c) A recurring template keeps stamping a GL code whose account was retired after it was written.**
      §199 refuses a retired or unknown code on every write that SETS one, including
      a recurring template's `gl_account` on create and on a PATCH that changes it.
      Nothing re-checks the code afterwards: `services/recurring_invoices.generate_one`
      copies `template.gl_account` onto every invoice it raises, so an account retired
      a year after the template was authored keeps being coded onto new payables —
      and those invoices then reach approval and the ERP push carrying a code the
      same tenant's chart no longer offers. The sweep has no human to ask, which is
      why §199 filed this instead of guessing.
      **Durable fix:** a product call between three shapes — (a) generate with
      `gl_account = None` plus a `gl_codes_not_in_chart` warning on the generated
      invoice (the rule extraction applies to an automated code, and the one that
      keeps the invoice payable after a human re-codes it), (b) generate unchanged
      but raise an exception-queue item against the template, or (c) pause the
      template and notify its author. Whichever is chosen, the check belongs in
      `generate_one` through `services/gl_chart.load_invoice_chart` against the
      template's own entity, and `/recurring`'s list should surface the affected
      templates so the operator can re-code them in bulk.
      **Trigger:** the first tenant that retires an account a live template codes to,
      or the next change to recurring generation.

- [ ] **(c) Dependabot auto-merges bumps to production images that no CI job runs.**
      Since §203, a `docker-compose` PR is tested on the images it bumps *when CI starts
      them*: pgvector, Redis and MinIO through `compose-images`, and Keycloak / Mailpit /
      LocalStack / stripe-mock through `docker compose up` in `service-e2e`. No CI job
      starts `deploy/compose.prod.yml`'s `caddy` or `frontend-build` (Node alpine) image.
      `dependabot-auto-merge.yml` still squash-merges a green minor/patch bump to either,
      so the first thing to run the new digest is the next production deploy. A
      Caddyfile directive that a Caddy minor changes, or a musl-only install failure under
      the new Node alpine image, would surface there. `deploy.sh` does fail loudly
      (`caddy reload` rejects a bad config, and the build step exits non-zero before
      anything rolls), but it fails on the VM rather than on the PR. The same is true of
      the local-only Authentik and Ollama images, which matter much less because they
      never reach production.
      **Durable fix:** a small CI job, path-filtered on `deploy/**` and `frontend/**`,
      that runs what the PR bumped against `deploy/compose.prod.yml` with a dummy `.env`:
      `docker compose -f deploy/compose.prod.yml run --rm --no-deps caddy caddy validate
      --config /etc/caddy/Caddyfile` (after seeding `tenants.caddy` from the example the
      way `deploy.sh` does), and the real `frontend-build` service
      (`docker compose run --rm -T -e PNPM_SPEC=… frontend-build`). Both read their refs
      from the compose file, so nothing is restated. A cheaper alternative is to stop
      auto-merging `/deploy` compose PRs (`dependabot/fetch-metadata` reports the
      directory), which trades the test for a human review of every Caddy and Node patch.
      **Trigger:** before the minimal VM serves its first customer, or the first
      auto-merged Dependabot compose PR that touches `deploy/compose.prod.yml`.

### Surfaced by retiring the stale archive PRs (2026-09-18, PRs #453/#454)

- [ ] **(c) The dashboard has no greeting, and the only draft of one never
      compiled.** A session crashed on 2026-09-16 mid-edit on
      `frontend/src/routes/+page.svelte`; PR #454 preserved the 31-line stash so it
      existed somewhere other than one machine's `.git`. That draft is not
      salvageable and was closed rather than merged: it annotated
      `greetingKey` as `MessageKey` against
      `dashboard.greeting.{morning,afternoon,evening}`, and **those keys are defined
      in no locale file** — `MessageKey` is `keyof typeof en`, so the annotation is a
      type error and `pnpm check` fails on it. Below `</script>` none of
      `greetingKey`, `todayLabel`, `userName` or the seven imported
      `~icons/material-symbols/*` glyphs are referenced, so even with the keys added
      it renders nothing and leaves seven unused imports. The idea is recorded here
      because the draft is gone, not because the draft was close.
      **Durable fix:** implement it as a normal change rather than recovering the
      stash — the three `dashboard.greeting.*` keys across **all six** locale
      catalogues (`en`, `de`, `es`, `fr`, `ja`, `pt-BR`; a missing key in one is a
      compile error, which is the point), an actual render site in the
      `<PageHeader>` slot, `$derived.by` re-reading `currentLocale()` so the date
      re-renders on a locale switch, and the glyphs `aria-hidden` with each card's
      `<h2>` still naming it. Greeting text keyed on the reader's own clock is
      presentation only and must not gate or reorder any figure below it.
      **Trigger:** the next `/polish-ui` pass on the dashboard, or any change that
      already touches `frontend/src/routes/+page.svelte`'s header.

### Surfaced by panelising the settings pages (2026-09-22, decisions §205)

- [ ] **(c) `/organization` is still one 3,400-line file, now with fifteen
      `{#if}` blocks in it.** The section rail fixed the *navigation* problem
      (`docs/decisions.md` §205) without touching the *organisation* one: the
      panels are conditionals around markup that still shares one `<script>`
      with 119 `$state` declarations, so guard rail 10 is no better served than
      before. Extracting them was deliberately not bundled into the navigation
      change, because the two have very different risk: the wrapping is
      mechanical and reviewable with `git diff -w`, while the extraction has to
      thread dozens of bindings and would have buried the behaviour change.
      **Durable fix:** one component per panel under
      `frontend/src/lib/components/organization/`, which almost certainly means
      first grouping the loose `$state` vars into per-panel objects (`company`,
      `erp`, `cards`, …) so a panel takes one or two props instead of fifteen.
      Two constraints are load-bearing and must survive it: the single
      `<fieldset disabled={readOnly}>` has to keep wrapping every panel — its
      comment explains that it is the *whole* read-only mode, so a panel outside
      it is silently editable — and field state has to stay above the panel
      boundary, or an unsaved edit stops surviving a panel switch.
      `tests-e2e/organization/section-nav.spec.ts` asserts the second one, so
      the refactor has a guard already waiting for it.
      **Trigger:** the next change that touches more than one panel of
      `frontend/src/routes/organization/+page.svelte`.

### Surfaced by the invoices / exceptions UX hunt (2026-10-05)

- [ ] **(c) An approved or paid invoice's source document can still be
      replaced or deleted, and the original is destroyed.** `PUT` / `DELETE
      /api/invoices/{id}/file` refuse only `done`
      (`backend/app/api/invoices.py`); on `approved`, `sending_to_erp`,
      `sent_to_erp`, `posted_in_erp`, `payment_scheduled` and `paid` they
      succeed, and the replace path deletes the previous object from storage
      after the commit. The audit row names the old filename, but the document
      the approver signed off, and the one the payment was made against, is
      gone. The invoice modal mirrors the server (`canManageFile` hides the
      controls only on `done`), so this is a policy question rather than a
      UI one, which is why the UX hunt reported it instead of changing the
      gate. **Durable fix:** decide whether the source document freezes with
      the financial fields (`_FINANCIALLY_LOCKED_STATUSES`, which starts at
      `approved`). If it does, refuse both routes from that set and gate
      `canManageFile` on the same list. If replacement must stay possible
      (for a corrected scan), keep the superseded object, for example by
      versioning the key instead of deleting it, so the evidence chain
      survives. **Trigger:** the product call, or the next change to either
      file route.

### Surfaced by the vendors table column fix (2026-10-06)

- [ ] **(c) A vendor you've just created can be invisible in the list.**
      `/vendors` is ordered by name (`api/vendors.py`, `default=[Vendor.name.asc(),
      …]`) and pages 25 at a time, and `CreateVendorModal`'s `onsaved` only
      refetches page one. On a tenant with more than a page of vendors, a new
      vendor whose name sorts past page one doesn't appear after you create it,
      with no sign it was saved except the closed modal.
      `tests-e2e/vendors/create-invite.spec.ts` hit this on a well-used e2e
      tenant. It now searches for its vendor, since its subject is the invite,
      not list placement. **Durable fix:** a product call between opening the
      new vendor's detail view after save, pinning it at the top of the list
      until the next reload, or filtering the list to it. Opening the detail is
      the smallest change, and matches how a create usually ends. **Trigger:**
      the next change to the vendors list or `CreateVendorModal`.

### Surfaced by the expense-management bug hunt (2026-10-05, round 2)

- [ ] **(c) An approved pre-approval is never consumed, so one estimate covers
      unlimited spend.** `_approved_preapproval_amount` returns the largest
      approved estimate that matches the line, and the engine compares it to
      each line *individually* (`covered >= amount`). Now that cover is scoped to
      the requester (`backend/docs/expense-management.md` § Pre-approval cover is
      the requester's own) a colleague's request can no longer be borrowed, but
      the same employee's single "500 USD, travel" approval still clears
      `preapproval_required` on any number of 400 USD travel lines, across any
      number of reports, indefinitely — the split-to-evade shape an approval
      limit exists to stop. Not fixed in the same change because the honest fix
      needs a data-model decision, not a query tweak.
      **Durable fix:** link a covered expense to the pre-approval that covers it
      (an `expenses.preapproval_id` FK, or a consumption row) and draw lines down
      against `estimated_amount` in the pre-approval's currency at submit, under
      a row lock on the pre-approval; a line the remaining balance cannot cover
      raises `preapproval_required`. Decide with product whether a pre-approval is
      single-use, a running budget, or time-boxed, and migrate (tenant fan-out).
      **Trigger:** the next change to pre-approval cover or the policy engine's
      `preapproval_required` rule, or a product call on pre-approval semantics.

### Surfaced by the procurement bug hunt (2026-10-05, PR #498)

- [ ] **(c) The budget PO-relief join reads `invoices.po_number`, which has no
      index, and the cost is unmeasured.** `budget_service._invoice_po_attribution`
      (the relief that stops an invoiced PO counting in both `committed` and
      `actual`) joins invoices to POs on `po_number`. Migration 0073 indexed only
      `purchase_orders.po_number`. The join is narrowed to the numbers held by the
      budgets in hand, but the narrowing is an `IN (subquery)` over an unindexed
      column, and this runs on `GET /budgets/check` before every requisition
      submit. The `_invoice_scan_narrowing` docstring records 0.11 ms vs 4.3 ms
      over 40k invoices for the actual leg. Nobody has taken the same measurement
      for relief.
      **Durable fix:** `EXPLAIN ANALYZE` the single-budget spend query over a
      40k-invoice tenant with realistic PO references. If relief dominates, add
      `ix_invoices_po_number` in a migration that fans out to every tenant (and to
      `tenant_provisioning` for fresh ones), with a plan test in the shape of
      `tests/test_invoice_budget_dimension_indexes.py`.
      **Trigger:** the next perf pass on budgets/procurement, or any report of
      `/budgets/check` latency.

### Surfaced by the payment-path bug hunt (2026-10-05, decisions §214)

- [ ] **(c) A resumed dispatch refused before the adapter call is classed retry-safe even if an earlier pass reached the processor.**
      `_dispatch_run_payments` also serves `/resume`, where a `pending` row can be one
      whose earlier pass called `adapter.create_payment` and then crashed before the
      commit. If the resume refuses it for a pre-adapter reason — `invoice_locked`
      (decisions §233), `invoice_not_payable`, `invoice_blocked:` — the row becomes
      `failed` with a RETRY_SAFE reason, and `/retry-failed` mints a fresh
      `correlation_id`, i.e. a second order under a new idempotency key. Pre-existing
      for the older reasons; `invoice_locked` makes it likelier because it is
      transient by design.
      **Durable fix:** stamp a dispatch-attempt marker on the payment row (committed)
      before the adapter call, and have `payment_runs.classify_payment_failure`
      return IN_DOUBT for any marked row regardless of reason, so the reconciler — not
      a retry — resolves it.
      **Trigger:** the next change to `/resume`, `/retry-failed` or
      `classify_payment_failure`, or the first resumed run seen after a mid-dispatch
      crash.

### Surfaced by the auth bug hunt, round 3 (2026-10-05)

- [ ] **(c) `users.email` / `vendor_users.email` are not case-insensitively
      unique in the database, and identity lookups cannot use an index.** Every
      write now stores `utils/emails.normalize_email(...)` and every identity
      lookup matches `lower(email)` (`docs/authentication.md` § An email address
      is matched without regard to case), so the app no longer creates or misses
      a case variant. But the constraint is still the plain, case-sensitive
      `UNIQUE(email)`: rows written before normalization keep their mixed case,
      two of them can differ only in case, and a concurrent pair of creates
      that both pass the app-level 409 check can still land a case variant.
      Login, forgot-password, SSO JIT and SCIM now filter on `lower(email)`,
      which the existing btree cannot serve — a sequential scan of the control
      plane's `users` table per sign-in (small today; the per-tenant
      `vendor_users` scan is smaller still).
      Not done in the same change because it is a data migration with a
      decision in it: an existing pair of case variants has to be merged or one
      of them renamed before a unique index can build, and which account wins
      is an operator call, not something a migration should guess.
      **Durable fix:** a control-plane migration that reports (and refuses on)
      any `lower(email)` collision, lower-cases every `users.email`, and
      replaces `UNIQUE(email)` with a unique index on `lower(email)`; the same
      per tenant for `vendor_users.email`. The lookups can then go back to an
      indexed equality on the normalized value.
      **Trigger:** before the first production tenant's users table holds
      enough rows for the scan to show on login latency, or the first
      case-variant pair reported in any environment — whichever is first.

### Surfaced by the recurring + discounting bug hunt (2026-10-05, PR #503)

- [ ] **(c) A payment void reverses a captured discount only by elimination.**
      `DiscountOffer` records no payment id, so
      `discount_capture.reverse_captures_for_voided_payment` un-captures an
      invoice's `captured` offer on a void only when the voided payment was
      `completed` **and** no other `completed` payment remains on the invoice.
      With two completed payments on one invoice (one matched the discounted
      payoff, one did not) and the capturing one voided, the offer stays
      `captured` and the discounting dashboard keeps reporting the savings —
      deliberately, because guessing which payment realized it could un-realize
      savings that really happened. Single-payment invoices, the normal case,
      are fully covered.
      **Durable fix:** a tenant migration adding a nullable
      `discount_offers.captured_by_payment_id` FK, stamped by `mark_captured`
      from `_capture_discount_offers`, and the void reversing exactly the offers
      whose column names the voided payment (legacy NULL rows keep the
      elimination rule).
      **Trigger:** any path that lets one invoice carry more than one
      `completed` payment at once (partial payments / split settlement), or the
      next schema change to `discount_offers`.
### Surfaced by the report-builder currency pass (2026-10-05, decisions §228)

- [ ] **(c) The cash forecast leaves out every committed invoice that is already past due.**
      `api/analytics._commitment_rows` drops any row whose effective due date is
      `< today` (`if due is None or due < today or due > horizon_end: continue`).
      Probed with real numbers: an approved USD 1,000 invoice due yesterday plus
      an approved USD 200 invoice due in five days gave
      `/cashflow_forecast` totals of `committed_amount: "200.00"`, `count: 1` —
      the overdue thousand is in no period. That one query feeds the forecast,
      `/cash_position`'s balance curve, the what-if, the copilot's planning tools
      and the shortfall-alert sweep, so all of them overstate the cash on hand by
      exactly the AP the business is already late paying — the most certain
      outflow it has. `backend/docs/analytics.md` does describe the
      `[today, today + horizon_days]` bound, but the only reason the code gives
      is "so the query doesn't scan the whole back-catalogue", and the bound is
      applied in Python after the query has already fetched every open invoice,
      so it buys nothing; no decision records excluding overdue AP as intended.
      Left unchanged here because it moves every cash surface and the copilot's
      `plan_id` hash at once, which is a product call rather than a bug fix.
      **Durable fix:** carry past-due committed rows into the first period as an
      explicit `overdue_amount` / `overdue_count` (not silently merged into
      `scheduled_amount`), push the due-date bound into SQL for the future edge
      only, and state the rule in `backend/docs/analytics.md` § Predictive
      cash-flow forecasting and `docs/cash-flow-copilot.md`.
      **Trigger:** the next change to `_commitment_rows` or to the `/cfo`
      cash panels, or a product decision on how overdue AP should appear.

### Surfaced by scoping the email-intake dedup per tenant (2026-10-05, PR #509)

- [ ] **(c) Delete the transitional legacy-claim check in email intake.** The
      intake dedup claim moved from `email_intake:<message_id>` to
      `email_intake:<org_id>:<message_id>` so one email addressed to two
      tenants reaches both. Claims written before that deploy carry no tenant,
      so `services/email_intake._legacy_claim_live` still treats a live
      unscoped claim as "already processed" for every tenant — the only rule
      that can never create a duplicate payable when a provider redelivers a
      pre-deploy message. It is dead weight once every such claim has expired.
      **Durable fix:** delete `_legacy_claim_live` and its call in
      `_process_for_org`, delete `webhook_security.event_claim_exists` if
      nothing else calls it by then, and delete
      `test_a_live_pre_upgrade_unscoped_claim_still_dedupes_the_redelivery`
      (plus `test_event_claim_exists_reads_without_claiming` with the helper).
      **Trigger:** the dedup TTL (`DEFAULT_DEDUP_TTL_SECONDS`, 72h) has elapsed
      since PR #509 was deployed to every environment.

### Surfaced by the round-2 issues batch (2026-10-06, PR #514, decisions §237–§243)

- [ ] **(c) Eight `create_exception` sites still write an unkeyed English description.**
      §240 keyed every `_ensure_exception` finding, but the direct `create_exception`
      callers were outside it: `extraction_failed` in `services/extraction.py` (which
      also embeds `str(exc)`), `api/erp_webhook.py`, `api/positive_pay.py`,
      `api/vendors.py` (the bank-change exception), `api/payments.py`,
      `services/payment_reconciler.py`, `services/payment_settlement_record.py` and
      `services/payment_erp_sync.py`. Their rows have no `description_code`, so the
      queue renders them in server English beside localized ones
      (`backend/docs/invoice-warnings.md` § What is NOT localized lists them).
      **Durable fix:** an `exception.*` catalogue code per site, built through
      `invoice_warning_catalog.warning`, with the generator re-run for both clients.
      Positive Pay de-duplicates on `APException.description == description`, so its
      dedup key has to move to the code + params first. `extraction_failed` should
      carry the exception class name as a param, never `str(exc)`.
      **Trigger:** the next change to any of those sites, or the next i18n pass over
      the exception queue.

- [ ] **(c) Three refusals a user acts on are still English-only.** §238 coded the
      approval, CFO / max-amount, credit-memo, expense and stale-edit refusals. Not
      done: the per-row skip reasons `POST /api/invoices/bulk-status` returns
      (built by `api/invoices._skip_reason`), the payment-run SoD and CFO sign-off
      refusals in `api/payments.py`, and the sign-in MFA challenge's
      `401 "Invalid code"` (`POST /api/auth/mfa/verify`) — the same sentence §238
      coded on enroll-verify, still bare on the step every MFA user meets.
      **Durable fix:** `api/refusals.coded_refusal` on each, registered in
      `frontend/src/lib/api/codedRefusals.ts` and the mobile
      `coded_refusal_messages.dart`; the bulk skip reasons as per-row codes like
      `/exceptions/bulk/resolve` already returns. The sign-in MFA refusal carries
      the `mfa_code_invalid` code; whether its status moves to 400 is decided
      against the login throttle and the client's 401 handling there, which
      §238's enroll-verify change did not have to consider.
      **Trigger:** the next change to any of those messages, or the first
      non-English tenant running payment runs.

- [ ] **(c) The DSAR export returns a vendor's invoices and payments uncapped.**
      §237's truncation fix wrapped every `.limit()` collection in `_capped`, but
      `services/privacy_export` returns a vendor subject's own invoices and payments
      (and its portal users, a user's passkeys and push devices, and the document
      manifest) in full. A supplier with years of volume builds one unbounded JSON
      document in memory, in a synchronous response, on the event loop.
      **Durable fix:** cap invoices and payments through `_capped` like the other
      high-volume collections, and move a large export to an asynchronous job that
      writes the bundle to object storage and hands back a signed URL. Paging a
      DSAR response would not do, because the subject is owed the whole set.
      **Trigger:** the first DSAR for a vendor with more than a few thousand
      invoices, or the next change to `privacy_export`.

- [ ] **(c) `/admin/privacy` is still mostly hardcoded English.** The step-up
      prompt it gained in §237 is localized (`StepUpPrompt`), but the page's own
      headings, form labels, help text and result summaries are literal English
      strings in `frontend/src/routes/admin/privacy/+page.svelte`.
      **Durable fix:** move every string into the six locale files under a
      `privacy.admin.*` namespace, with the result summaries built from the export's
      typed counts rather than server sentences.
      **Trigger:** the next change to that page, or the first non-English privacy
      officer.

- [ ] **(c) An expense that sits on no report has no owner, so anyone can rewrite it.**
      §241 made a report's lines owner-only, but `expenses` has no creator column.
      A loose line stays editable by any `admin` / `ap_manager` / `ap_clerk` until a
      report owner attaches it, and attaching adopts it. A manager can therefore
      edit a clerk's loose line, and the clerk then attaches and submits it unaware.
      The approver ≠ owner check still holds, but the line's real author is not in
      the set it checks.
      **Durable fix:** `expenses.created_by_user_id` (tenant migration, NULL for
      existing rows, which no honest author exists to backfill — §141's reasoning),
      stamped on create; claim-field edits of a loose line restricted to its creator;
      attach refused for a line someone else created.
      **Trigger:** the next change to expense ownership or the expense SoD check,
      or a persona-fraudster pass over expenses.

### Surfaced by writing the help centre (2026-10-06, decisions §244)

Writing the in-app guides against the code turned these up. The guides
describe what the code does today; each item says what changes in help when it
lands. Pure doc drift was corrected in the same PR. The two diagnosed defects
(the assistant's forecast gate, expense Attach) are in
[known-issues.md](known-issues.md).

- [ ] **(c) Translate the help centre's guide prose.** Guide bodies, glossary
      long text and page-directory lines are English, rendered `lang="en"`
      under a translated notice. The chrome, term names, tooltips and diagram
      labels are already in all six locales (`frontend/docs/help-centre.md`
      § Translation). **Durable fix:** a per-locale overlay keyed by guide id
      that carries a hash of the English source it was translated from, so a
      guide edited after translation falls back to English rather than
      showing a stale translation, plus a test that fails on a stale hash.
      Reviewed by a fluent finance reader per locale, not machine output.
      **Trigger:** the first customer contract in a non-English market, or a
      locale's tenants passing a share of active users worth the review cost.
- [ ] **(c) Nothing creates a goods receipt.** There is no create endpoint, ERP
      sync or UI for goods receipts; only seed data makes them. So three-way
      and four-way matching can't be fed in a real tenant. **Durable fix:**
      ERP sync of receipts beside the PO sync (the adapter families already
      sync POs), plus a manual "record receipt" form for tenants without one.
      **Trigger:** the first tenant that turns on three-way matching.
- [ ] **(c) A requisition can't be linked to a budget from the UI.** The
      requisition form has no budget field and `checkBudget()` (the overspend
      pre-check in the frontend API module) is never called, so a budget's
      "Committed" figure can only be filled through the API. **Durable fix:** a
      budget picker on the requisition form that calls `checkBudget()` and
      shows the overspend warning before submit. **Trigger:** the next change
      to requisitions or budgets.
- [ ] **(c) A rejected invoice has no resubmit button on the web.** The web
      invoice modal offers none. Resubmission happens through the bulk
      status change or the supplier portal, which a clerk fixing their own
      rejection won't find. **Durable fix:** a "Resubmit for review" action in
      the modal for a `rejected` invoice, behind the same role gate as the bulk
      action. **Trigger:** the next invoice-modal change.
- [ ] **(c) Settings with a backend and no screen.** Approval delegation,
      per-vendor / per-GL match tolerance, exception-agent autonomy level, the
      SCIM bearer token, an admin revoking another user's sessions, and the
      line-total-mismatch on/off switch (`settings.fraud_rules`) all
      exist in the API with no UI, so help can describe their effect but can't
      tell anyone where to change them. **Durable fix:** a panel each in
      `/organization` (or the user row's actions for sessions), each with its
      own help guide step. **Trigger:** the first customer asking to change
      one, or SCIM onboarding for an enterprise tenant.
- [ ] **(c) Pages still hardcoded in English.** The Report Builder page,
      `/admin/access-review`, `/admin/privacy`, the vendors list's "Merge
      duplicates" action and the budget dimension labels
      (`lib/types/budget.ts`) carry literal English. Help has to name those
      labels in bold rather than through `{ui:}`. **Durable fix:** extract them
      to the catalogues and swap the guides' bold labels for `{ui:}`
      references (`content.test.ts` will check them). **Trigger:** the next
      i18n extraction slice.
- [ ] **(c) Help for the mobile app and the supplier portal.** The help centre
      covers the employee web app. Mobile users and suppliers get none.
      **Durable fix:** a portal-side help section (suppliers are a separate
      audience: submit, get paid, change bank details, card payments) reusing
      the same content shapes and tests, and a Help row in the mobile app's
      settings that opens the web help for the user's tenant. **Trigger:** the
      first supplier onboarding at volume, or the first mobile-first customer.
- [ ] **(c) Approval isn't tied to the version the approver saw.** An edit
      that lands between an approver reading an invoice and clicking Approve
      is approved unseen, for managers as well as clerks (a clerk's own window
      now closes at submit, decisions §248). **Durable fix:** an
      `expected_updated_at` on `ApproveRequest` and per row in bulk approve,
      checked under the row lock, 409 on a mismatch, with the modal reloading
      on 409. **Trigger:** the next change to the approve endpoints, or before
      the first production tenant with more than one approver.
- [ ] **(c) `bulk/delete` has no entity scope or row lock.** It has the shape
      `bulk/status` had before the clerk-entry change fixed it there. Clerks
      can't reach it. **Durable fix:** the same `apply_entity_scope` +
      `FOR UPDATE` + missing-id-as-skip treatment `bulk/status` got, with a
      cross-entity test. **Trigger:** the next change to bulk invoice actions.
- [ ] **(c) Bulk `new → ready_for_review` skips `/complete`'s required-field
      check**, for every role, so an invoice with no vendor or amount can reach
      the approval queue from the bulk bar. **Durable fix:** run the same
      required-field validation per row and report failures as skips.
      **Trigger:** same as above.
- [ ] **(c) CSV import resolves vendors across entities and defaults a blank
      currency to USD.** A row can link another subsidiary's vendor, and a
      blank currency cell becomes `"USD"` against the rule that a missing
      currency stays missing. Its "records history" row error also has no
      message code, so it reads in English in every locale. **Durable fix:**
      scope vendor matching to the import's entity, refuse a blank currency
      per row, and key the row errors through the message catalogue.
      **Trigger:** the next change to CSV import.
- [ ] **(c) A manager's extraction can auto-approve a document a clerk
      swapped in.** A clerk's own upload or extract never auto-approves, but a
      manager who re-extracts after a clerk replaced the file can still get
      it auto-approved with no second look. **Durable fix:** suppress
      auto-approve whenever `segregation_actor_ids` is non-empty, with a test.
      **Trigger:** the next change to extraction dispatch or auto-approve.
- [ ] **(c) A custom role can't be granted invoice entry.** Entry is a role
      list by design (decisions §248). **Durable fix:** if a customer needs a
      non-clerk preparer role, add a non-sensitive permission tier the
      role-grant guards exempt, and move entry onto it. **Trigger:** the first
      customer asking for a custom preparer role.
- [ ] **(c) Deactivating a vendor doesn't cancel its live virtual cards.** A
      card minted before the vendor went inactive, rejected or merged stays
      spendable; payment runs and `/cards/generate` refuse the vendor, but
      nothing reaches cards already issued. **Durable fix:** cancel the
      vendor's live cards when its status leaves `active`, with an audit row
      per card. **Trigger:** the next vendor-lifecycle change.
- [ ] **(c) The PO-match receipt leg is pro rata and not cumulative.** The
      "billed beyond received" check values receipts as `po_total × received /
      ordered`, so receiving nine cheap cables of a 900 server order reads as
      most of the value received; and nothing subtracts what other invoices
      already billed against the same PO, so two 400 invoices both pass
      against 400 received (decisions §249). **Durable fix:** value receipts
      per PO line (cheapest-first as a lower bound) and subtract other live or
      paid invoices on the same PO in both the amount and receipt legs.
      **Trigger:** the next PO-matching change, or the first customer relying
      on split or blanket POs.
- [ ] **(c) Nothing links a credit memo to a discount offer.** An applied memo
      equal to an accepted offer's savings is read as the discount already
      taken (decisions §250), so a return credit that happens to equal the
      savings suppresses the discount and the offer is then recorded as
      captured. **Durable fix:** `credit_memos.discount_offer_id` (or a typed
      reason) and drop the amount match. **Trigger:** the next credit-memo or
      discounting change.
- [ ] **(c) The ERP payment sync posts no amount yet.** `payment_erp_sync`
      only logs. **Durable fix:** when `post_payment` is implemented, send
      `discount_amount` beside `amount` so the ERP books the discount taken.
      **Trigger:** the first real ERP payment-posting adapter.
- [ ] **(c) `/cards/generate` has no row lock between its checks and the
      mint.** Pre-existing: two concurrent calls can both pass the vendor and
      payable checks. **Durable fix:** lock the invoice rows `FOR UPDATE`
      before checking and minting, with a concurrency test. **Trigger:** the
      next card-issuance change.
- [ ] **(c) The web bundle total counts route-split content nobody loads
      together.** `MAX_TOTAL_KB` sums every chunk (one locale catalogue), so
      the help centre's lazy guide prose and diagrams (~81 KB, `/help` only)
      raised it as if every visitor downloaded them; the 2026-09-16 ceiling
      entry's trigger for this fired on PR #516 and the ceiling went to 1075.
      **Durable fix:** measure the app shell plus the heaviest single route
      (from the Vite manifest's import graph) as the gated total, and keep the
      every-chunk sum as a reported, ungated figure. **Trigger:** the next
      time `MAX_TOTAL_KB` binds, or the next change to the budget workflow.
- [ ] **(c) The largest locale catalogue is 5 KB under its chunk ceiling.**
      The ja catalogue is 95 KB of `MAX_LARGEST_CHUNK_KB`'s 100 after the
      help centre's terms and tooltips. **Durable fix:** split the help-only
      keys (`help.*`, diagram labels) into a second lazy catalogue loaded by
      `/help` routes and HelpTip, with the locale key-parity test covering
      both slices. **Trigger:** before the next feature that adds more than a
      couple of hundred keys, or when the catalogue passes 97 KB. **Fired
      2026-10-06:** the no-rail pilot (issue #517) added 87 keys and the largest
      catalogue measured 98 KB of 100 (total 989 of 1075) — still green, but
      the next feature with real copy will bind. Do this split next.

### Surfaced by the no-rail pilot (2026-10-06, issue #517, decisions §251)

- [ ] **(c) The mobile app has no record-only mode.** It still offers Execute on a
      draft run; on a record-only tenant the server refuses with
      `payments_record_only` and the app shows that message, so nothing is paid
      by mistake — but there is no "record as paid outside FeohLedger" and no
      run-level record on mobile, so a mobile-only approver has to switch to the
      web to close an invoice out. **Durable fix:** read
      `GET /api/payments/execution-mode` in the payments store, hide Execute /
      Resume / Retry on `record_only`, and add the single-invoice and run-level
      record flows (the endpoints are the web's, so no backend change), with
      widget tests and the l10n strings in every ARB. **Trigger:** the first pilot
      customer whose approvers use the mobile app, or the next mobile payments
      change, whichever comes first.
- [ ] **(c) An ERP-reported payment is dated the day the webhook arrives.** The
      inbound `Paid` payload carries no payment date, so
      `erp_webhook._record_erp_reported_payment` books `paid_on = utc_today()` and
      `method = NULL` (reportable). A bill paid on 31 December and reported on
      2 January lands in the wrong 1099 year, and a card payment made in the ERP
      would be counted as reportable. **Durable fix:** accept an optional
      `paid_on` and `payment_method` in the ERP webhook body (and map them in each
      ERP adapter's status poll), validate them like the user endpoint does, and
      fall back to the arrival date only when absent. **Trigger:** the first
      ERP-led pilot customer, or the first year-end with ERP-reported payments.
- [ ] **(c) The NACHA file is always unbalanced.** `services/nacha.py` writes
      credits only, which most ODFIs expect from a corporate originator, but some
      banks require a balanced file (an offsetting debit to the originator's own
      account). **Durable fix:** an optional `settings.payments.nacha` block for
      the offset account (`offset_routing`, `offset_account`, `offset_account_type`)
      that, when present, appends one debit entry for the batch total and switches
      the batch to service class 200, with builder tests on the control totals.
      **Trigger:** a pilot customer's bank rejects or asks for a balanced file.

## (a) Blocked on external credentials, accounts, or hardware

Categories (a) and (b) are operator work, not engineering work. Both are
mirrored — alongside the founder critical path — as GitHub issue
[#446](https://github.com/Absence0760/feohledger/issues/446), the running
checklist for everything only the operator can do; the in-repo half of that pair
is [founder-runbooks/status.md](founder-runbooks/status.md). Keep all three
reconciled when an item closes.

- [ ] **Appoint EU and UK Art 27 representatives.** ([#428](https://github.com/Absence0760/feohledger/issues/428)) A controller established
      outside the EU/UK that offers services to people there must appoint a
      representative in each, named and addressable in the privacy notice. The
      product targets both (PEPPOL e-invoicing, EU VAT, UK VAT/HMRC features,
      and six shipped locales), so this is not hypothetical. `operator.ts`
      models both as pending and the Privacy Policy renders all branches, so
      filling them is a one-line edit per representative.
      **Why blocked:** it needs a paid engagement with a representative firm in
      each jurisdiction. No longer waits on incorporation — the operator
      contracts as a sole proprietor ([decisions.md](decisions.md) §252).
      **Durable fix:** engage both, then set `euRepresentative` and
      `ukRepresentative`.
      **Trigger:** before marketing to, or onboarding, an EU or UK customer.
      Ref: [decisions.md](decisions.md) §175.

- [ ] **Execute the transfer safeguards the Privacy Policy and the DPA name.**
      `/legal/privacy` §9 now names the SCCs (Decision 2021/914), the UK
      Addendum and the Swiss amendments, **and** tells a reader to write to the
      privacy address for a copy — which means we have to be able to produce
      one. Naming a safeguard is the drafting half; the contractual half is
      signing the Clauses with each sub-processor that receives personal data
      outside the EEA/UK (`/legal/sub-processors` is the list) and completing
      their annexes, plus the transfer-impact assessment *Schrems II* requires.
      **Why blocked:** operator work with each provider; no longer waits on
      incorporation — the sole proprietor is the party ([decisions.md](decisions.md) §252).
      **Durable fix:** execute the Clauses provider by provider,
      and keep the signed set where the privacy mailbox can answer from it.
      **Trigger:** before the first EEA or UK customer, and before anyone acts
      on the copy offer in §9.
      Ref: [decisions.md](decisions.md) §175.

None of these are startable from the editor. They are listed so they don't read
as oversights.

- [ ] **SOC 2** — vendor selection (Vanta / Drata / Secureframe / Sprinto),
      policy library, onboarding/offboarding checklist with evidence collection,
      incident-response runbook + on-call rotation, Type I audit, then the Type II
      observation window. **All engineering prereqs are complete**; this is
      process work behind a founder decision and a vendor contract.
      Ref: [soc2-readiness.md](soc2-readiness.md).
- [ ] **Live government e-invoice clearance** — SdI (IT), SAT-PAC (MX), SEFAZ
      (BR), DIAN (CO). The generators and national validation ship as pure
      local-first code; only live authorization remains, and each needs its own
      country registration. Ref: [peppol.md](../backend/docs/peppol.md).
- [ ] **Live sanctions-provider wiring** — the ComplyAdvantage / Dow Jones /
      Refinitiv adapters are fail-closed skeletons awaiting keys. `mock` is the
      local-first default and the screening path itself is shipped and tested.
      Ref: [vendor-risk-screening.md](../backend/docs/vendor-risk-screening.md).
- [ ] **Stripe Billing** — a provisioned Stripe account to verify the live
      `stripe_billing` adapter path end-to-end. All the code that needs it is
      shipped, including the plan-change UI (`/billing`, tested against the
      `mock` adapter) — this is purely the credential to validate the real
      Stripe leg.
      Ref: [billing.md](../backend/docs/billing.md).
- [ ] **Mobile push (FCM + APNs)** — a Firebase project,
      `google-services.json` / `GoogleService-Info.plist`, and an APNs auth key.
      Device-token registration + notification-tap deep-linking are shipped
      (`push_service.dart`); what's blocked is the push-*sending* adapter
      itself, which needs these credentials to build against.
- [ ] **Manual screen-reader device pass** — VoiceOver / NVDA / TalkBack. The
      procedure is documented and repeatable; it needs real AT hardware, so it
      cannot run in CI. The automated axe-core + `meetsGuideline` guards ship.
      Ref: [accessibility-screen-reader-checklist.md](accessibility-screen-reader-checklist.md).
- [ ] **Banking-aggregator (Plaid-style) balance feed** — the bring-your-own and
      provider-`get_balance` paths ship; a real aggregator needs an account.

---

## (b) Operator steps on merged code

- [ ] **Create the five published contact aliases.** ([#428](https://github.com/Absence0760/feohledger/issues/428)) `/legal/*` tells readers to
      write to `privacy@`, `security@`, `legal@`, `support@` and `sales@` on
      `feohledger.com` (`frontend/src/lib/legal/operator.ts` → `CONTACT`).
      **All five now exist.** `infra/email.tf` — merged as **#417** (`98fd5ff0`)
      and unapplied until then — was applied 2026-09-17: 17 mail records, SES
      reporting `sending`/`dkim`/`mailFrom` all verified, and the five created in
      Migadu as aliases onto the `ops@` mailbox. (The stale
      `origin/feat/infra-email-dns` remote branch can go; #417 superseded it.)
      **What is still open is proof of delivery**, and no check from the
      workstation can supply it: Migadu refuses SMTP from an IP with no reverse
      DNS, so a probe returns the same refusal for a real alias as for an invented
      one — it reads as a pass to anyone who does not compare the two. A published
      `mailto:` that bounces is worse than none at all, because a data subject who
      writes to it reasonably believes the request is made and the Art 12(3)
      one-month clock has started.
      **Durable fix:** send a real message to each of the five from an outside
      provider and confirm all five land in `ops@`, before the pages are linked
      from anywhere public.
      **Trigger:** before `feohledger.com` serves the app to anyone outside the
      project.
      Ref: [decisions.md](decisions.md) §175. Tracker:
      [#446](https://github.com/Absence0760/feohledger/issues/446) § 3.

- [ ] **Fill the operator facts and get counsel to review the legal set.** ([#428](https://github.com/Absence0760/feohledger/issues/428)) Eight
      facts in `frontend/src/lib/legal/operator.ts` are `null` and render as
      `[… to be confirmed]` on every page: the registered legal entity, a postal
      address, the governing law and venue, the lead supervisory authority, EU
      and UK Art 27 representatives, whether a DPO is appointed, and the hosting
      region. The documents are complete and operative as written — these are
      the facts only the operator can supply.
      **Durable fix:** set each value in that one file (the pending notice and
      every inline marker disappear with no other edit), then have a lawyer read
      the set. The liability cap figure, the arbitration-vs-courts call, and the
      SCC module and governing-law selections in the DPA are the ones that
      genuinely need advice rather than a decision.
      **Corrected 2026-09-17, and the correction was itself half wrong — see
      below.** This entry and #446 §4 cite `reviews/saas-legal-review-legal-pages.md`
      and `reviews/us-legal-review-legal-pages.md`. `/reviews/*` is gitignored
      (`.gitignore:211`), so neither is in the repo for anyone but their author,
      and the **us-legal one was never written at all** — #428's own pointer,
      which names only the saas file, is the accurate one.
      **Re-corrected 2026-09-18.** The first correction went on to claim neither
      file "exists on the working machine today — the directory holds only its
      `README.md`". That is false in the primary checkout, where `reviews/` holds
      43 files including the saas review (72 KB, written 2026-09-15 against HEAD
      `4a8048fb`). It was written from inside a worktree, where `/reviews/*`
      being gitignored and `.worktreeinclude` copying only the `.env` overrides
      means every worktree sees exactly one file there — a worktree artifact
      generalised to the machine. Worth remembering as a shape: **"I looked and
      it wasn't there" is not portable evidence from inside a worktree.**
      The load-bearing point survives both corrections: a pointer into an
      ignored directory is a pointer to nothing for anyone but its author, so
      questions worth citing belong in a tracked file.
      **Trigger:** before the first customer who is not the operator signs up.
      Ref: [decisions.md](decisions.md) §175.

- [ ] **Confirm Teams posts the approval card's action body byte-for-byte.**
      The outbound card stamps each Approve/Reject `HttpPOST` action with the
      HMAC of the exact `body` string it will send, and
      `/api/approvals/teams/interactivity` re-derives it over the raw request
      bytes ([decisions §33](decisions.md)). If Microsoft re-serialised the body
      rather than relaying it verbatim, the digest would not match. The failure
      mode is graceful and already tested — the opaque ack tells the approver to
      sign in to the app, never a 500 or a wrong decision — but only a live
      Teams tenant can confirm the happy path.
      **Durable fix:** post a real card into a Teams channel, click both
      buttons, and confirm the invoice transitions; if the body is re-serialised,
      switch the digest to cover a canonical subset (the action token alone)
      rather than the whole string.
      Ref: [teams-approval.md](../backend/docs/teams-approval.md).

**Two CLOSED narratives stood here** — the custom-domain provisioning runbook
and the restoration of `.github/workflows/dependabot-lockfile.yml` — and were
pruned on 2026-09-17. Both shipped in round 20. The runbook is
[`docs/founder-runbooks/custom-domain-provisioning.md`](founder-runbooks/custom-domain-provisioning.md);
the workflow's design, its three-job privilege split and the pnpm-overrides
check are in the workflow file itself, and the operator procedure for a synced
Dependabot PR — approve the `action_required` runs rather than pushing an empty
commit — is in [backend/CLAUDE.md](../backend/CLAUDE.md) § Dependency lock and
[frontend/CLAUDE.md](../frontend/CLAUDE.md) § The lockfile. What is still open
from that work is the one entry below.

- [ ] **Give the lockfile workflow a credential whose pushes retrigger CI.**
      Removing the approval click needs a token that starts workflow runs
      unattended: a fine-grained PAT with `Contents: Write` in the **Dependabot**
      secret store, or (sturdier — no expiry, not bound to one person) a GitHub
      App token via `actions/create-github-app-token`. Until then every synced
      Dependabot PR costs one approval, and a PR left unapproved reads red for a
      reason that is not a test failure. This carried no checkbox until
      2026-09-17, so the file's own count had been missing it.
      **Durable fix:** provision the App token and swap
      `dependabot-lockfile.yml`'s `GITHUB_TOKEN` for it.
      The manual recipe in [backend/CLAUDE.md](../backend/CLAUDE.md)
      § Dependency lock stays the documented fallback.
      **Trigger:** an operator provisioning either credential.

- [ ] **(b) Deployed databases may already hold rows the three newly-enforced
      UNIQUE indexes forbid.** Migration 0093 pre-flights all three and refuses
      with a counts-only, PII-free message rather than dying mid-`CREATE UNIQUE
      INDEX` ([decisions.md](decisions.md) §109). If it refuses: duplicate
      Positive Pay check-issue files → establish which went to the bank and
      `DELETE /api/positive-pay/{id}` the others; two live subscriptions for one
      org → cancel the superseded row; two orgs sharing a SCIM bearer digest →
      re-mint one. All six local tenants and the local control plane pre-flighted
      clean. **Trigger:** the next `alembic upgrade head` /
      `migrate_all_tenants.py` on a deployed environment.

- [ ] **(b) Enable the retention sweep to expire Positive Pay files in a
      deployed environment.** `positive_pay` is now a retention record class and
      the sweep deletes the stored file past the window (default 1 month,
      per-org configurable), which is what closes issue #425 — but
      `FEOH_RETENTION_ENABLED` is off by default, like every sweep in this
      project (guard rail 7). Until an operator turns it on, a deployed
      environment still accumulates files carrying full account and routing
      numbers; the erasure path reaches them on request, but nothing reaches
      them on a timer. **Durable fix:** set `FEOH_RETENTION_ENABLED=true` in the
      deployed env and confirm the first `retention.archived` manifest reports
      `positive_pay_files_expired`. **Trigger:** the first deployment that
      generates a Positive Pay file, and the SOC 2 records-management evidence
      request either way.
