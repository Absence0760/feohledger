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

Mirrored as GitHub issue [#321](https://github.com/Absence0760/feohledger/issues/321)
for the tracker view. Keep the two reconciled when either moves.

**Last reconciled:** 2026-09-17 (round 26) — five agents, each in its own
worktree. **Eleven entries closed** plus the engineering half of
[#432](https://github.com/Absence0760/feohledger/issues/432), **nine opened**.
Earlier the same day a housekeeping pass pruned 54 checked entries, 21 emptied
sections and two prose-only CLOSED narratives, taking the file from 2570 lines
to 1215; nothing open was removed by it.

**This file had stopped obeying its own first rule.** "Open items only" is the
line at the top, and 54 `[x]` entries plus their surrounding CLOSED narrative
had accumulated beneath it — more than half the file describing work already in
git history, with its reasoning already in [decisions.md](decisions.md). A
reader looking for what is open was reading mostly what is not. Every pruned
section carried its own `decisions.md` § reference, so nothing was lost by
deleting it; that cross-reference is what makes the pruning safe, and writing
one is what earns a future entry the right to be deleted.

**60 open: 45 (c) · 9 (a) · 6 (b)** — re-derived from the file, never carried
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
that the web `resolveCurrency` still substitutes USD, so the two surfaces
currently disagree about the same row (§160).

**A held item is still an open item, and the hold has to be re-checked.** The
header claimed for eight rounds that exception resolution "stays held" as the
file's only segregation-of-duties entry. It had in fact shipped — `#408`,
`services/exception_lifecycle.segregation_refusal`, migration `0098`
([decisions.md](decisions.md) §169–§170) — and the file's only remaining SoD
entry is now the inter-company mirror below. A standing note nobody re-reads
outlives the thing it describes.

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

Two of the eight entries this round opened remain. Both are findings that could
not honestly be folded into the slice that surfaced them.

- [ ] **(c) The inter-company mirror does not inherit the source payable's
      implicated-actor set.** §152 made segregation key on
      `Invoice.uploaded_by_id` ∪ `Invoice.segregation_actor_ids`, and
      `services/intercompany.py` passes `segregation_actor_ids=None` on purpose: the
      mirror's segregation subject has always been its own creator, the routing actor,
      never the source invoice's. So if an `ap_manager` materially edits a recurring
      template and a *different* employee routes the invoice it generates to a
      counterparty entity, that editor can approve the mirror — they are on the
      source's set, and nothing carries it across the boundary.
      Narrow in practice: a recurring template carries no `counterparty_entity_id`
      (the model has none and `generate_one` sets none), so routing needs a separate
      deliberate act by an employee who becomes the mirror's uploader, and the source
      invoice still needs its own clean approval.
      **Durable fix:** decide the entity-scope rule, then apply it to BOTH columns at
      once. Propagating the set while still not propagating `uploaded_by_id` is the
      inconsistent half of either choice — it would block a source *editor* at the
      counterparty while leaving the source *uploader* free. The question is whether
      shaping a payable under one entity should bar you from signing its mirror under
      another, which is a multi-entity approval-scope call (Phase 4 territory), not a
      recurring-template one.
      **Trigger:** the next multi-entity slice, or the first tenant that routes
      recurring spend inter-company.

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

### Surfaced while closing the exception-queue segregation entry (2026-09-14)

- [ ] **(c) A backend refusal sentence reaches a localized page in English.**
      The new segregation refusal on `POST /api/exceptions/{id}/resolve` returns a
      403 whose `detail` the queue renders through `extractError(err)` verbatim —
      so a `de` / `es` / `fr` / `ja` / `pt-BR` operator gets one English sentence
      inside an otherwise-translated page. This is **not** new with that change:
      it is how `approval_chain.check_segregation`'s identical 403 has always
      surfaced on the approval path, and how every other backend `detail` reaches
      every page. The bulk half *is* localized, because `/bulk/resolve` returns a
      machine code per row (`segregation_raiser` / `segregation_implicated`) that
      `exceptions.bulk.segregationSkipped` renders in all six locales — which is
      the shape the fix wants, and the proof it is affordable.
      **Durable fix:** a stable machine `code` alongside `detail` on the refusals
      a user is expected to *act* on (the two SoD paths, the CFO / max-amount
      gates, the named-approver gate), plus a frontend code→`MessageKey` map that
      degrades to the server's sentence for an unknown code — the same tolerant
      pattern `exceptionTypeLabelKey` and `screeningCategoryLabelKey` already
      use, and the same conclusion §149 and §138 reached about rendering a raw
      server string. Deliberately **not** done inside the segregation slice: it
      is a cross-cutting error-contract change touching every refusal on the
      money path, and doing only the one new refusal would have left the page
      inconsistent with the older identical one beside it.
      **Trigger:** the next change to any approval-path refusal message, or the
      first non-English tenant on the approval queue.

### Surfaced by the round-31 batch (2026-09-14)

The largest cluster in the file — eighteen entries opened by round 31 (sixteen
from the slices, two from its own CI run), all still open, grouped by the slice
that surfaced them. The lesson the round recorded is in the header above and
applies to every entry here: **an entry's file list and its counts are the least
reliable part of it, and a durable fix stated in one sentence has usually not
been tried.** Four of these were found to describe the code wrongly in the
2026-09-17 pass and carry an inline correction; assume the rest are no better
and re-derive before implementing.

#### Opened by the invoice-warning catalogue

- [ ] **(c) `Invoice.po_match.issues` is still server English rendered verbatim.** The PO-match
      panel in `InvoiceModal` prints the matcher's own composed sentences ("Partial receipt: 60% of
      ordered quantity received", "Amount mismatch: invoice $150.00 vs PO $100.00 (+50.0%)") one per
      row. Round 31 keyed the `po_mismatch` *warnings* beside them, so the same dialog now shows a
      German finding above an English issue list — a narrower version of the §155 mismatch, one
      panel down. The `$` in the amount-mismatch issue is the same hardcoded-currency defect §157
      removed from the warnings.
      **Durable fix:** `po_matching.py` already carries every figure those sentences embed as a
      structured field (round 31 added `ordered_quantity` / `received_quantity` /
      `inspection_deviation_notes` for exactly this reason), so the issues become
      `{code, params}` entries in `invoice_warning_catalog` — a `po_match.issue.*` key namespace
      reusing the generator and drift guard already wired. `issues` is `list[str]` on the persisted
      JSONB and the frontend `PoMatch` type, so the wire shape changes and the modal's renderer
      moves with it.
      **Trigger:** the next change that touches the PO-match panel or `MatchResult.issues`.

- [ ] **(c) `Exception.description` reaches the exception queue as server English.** `_ensure_exception`
      is handed composed prose at every call site — often the warning's own `message`, sometimes a
      different sentence for the same finding ("Suspicious round amount: $5000.00" against the
      warning's "Round amount: 5000.00 ZAR"), and for `price_variance` a `"; "`-joined summary over
      every flagged line. `/exceptions` renders it raw, so the queue and the invoice modal can
      describe one finding in two languages and two wordings. The hardcoded `$` survives here too.
      **Durable fix:** give the exception description the same `{code, params}` treatment — a
      `description_code` / `description_params` pair on the `Exception` row (a migration that must
      fan out to every tenant DB) with `description` kept as the fallback, and the per-line
      `price_variance` summary decomposed rather than joined server-side. The exception-type LABEL is
      already keyed (`EXCEPTION_TYPE_LABEL_KEYS`, §155); this is the sentence beneath it.
      **Trigger:** the next slice that touches `_ensure_exception` or the `/exceptions` detail panel —
      or sooner, since it is the surface an auditor reads.

#### Opened by the label-map slice

#### Opened by the mobile-currency slice

- [ ] **(c) `frontend/utils/money.ts::resolveCurrency` substitutes
      `DEFAULT_CURRENCY` for a code the backend deliberately declined to
      supply.** `formatMoney` funnels every web figure through
      `resolveCurrency(options.currency)`, which returns `'USD'` for a null /
      malformed code. That directly contradicts `PaymentResponse.currency`'s own
      contract — "`None` is deliberate and is NOT a licence to substitute a
      default … Render the bare figure rather than a code that cannot be
      proven" (decisions §79/§82) — so a payment whose invoice carries no
      currency renders as dollars on the web while round 31 made mobile render
      it bare (§160). Several call sites additionally write
      `p.currency ?? orgCurrency.currency`, labelling a per-row figure with the
      org's reporting currency, which is the mislabel §160 removed.
      **Durable fix:** let `formatMoney` render a bare grouped figure for an
      unprovable code (a `Money` component prop, not a new helper), audit the
      `?? orgCurrency.currency` call sites — *list corrected 2026-09-17:*
      `bank-reconciliation/+page.svelte`, `PositivePayModal.svelte`,
      `routes/positive-pay/+page.svelte`, `routes/payments/+page.svelte` and
      `analytics/ByEntityBreakdown.svelte`. `RunDetailModal.svelte` is **no
      longer** one: it now takes the server's `currency` and renders bare on
      `null`, which is the shape the others should copy. Keep
      `DEFAULT_CURRENCY` only for the picker defaults and form initial values
      that genuinely need a value. Web-only; the two surfaces currently disagree
      about the same row.
      **One site is a backend gap, not a formatter one, and is the next one to
      do:** `routes/purchase-orders/+page.svelte`'s `formatCurrency` labels every
      PO with `orgCurrency.currency` because `GET /api/purchase-orders` serves no
      per-row `currency` at all. **Corrected 2026-09-17 — this is bigger than the
      entry claimed, and the correction matters because the PO work gates the
      formatter work.** The entry said `PurchaseOrder` "carries the column, so
      this is one serializer field plus the type". It does not:
      `models/procurement.py::PurchaseOrder` has `total`, `status`,
      `expected_delivery_date` and no currency anywhere, so this is a tenant
      migration fanned out across every tenant DB, a backfill decision for
      existing rows, and only then the serializer field — not the one-line
      `_exception_dict` shape it was compared to. It still must land before the
      formatter change, or bare-figure rendering would replace a wrong label
      with no label on every PO row; sizing it honestly is what stops the
      formatter change being blocked on a task nobody budgeted for. **Not** a site:
      `bank-reconciliation/StatementDetailModal.svelte`'s Uncleared bucket, which
      falls back deliberately and says so in a comment —
      `UnclearedPaymentResponse` genuinely has no per-row currency where its
      `unmatched_debits` sibling does, so that one is a payload question filed
      with the reconciliation work, not this entry.
      `routes/exceptions/+page.svelte` **was** on this list and is now fixed: the
      round-31 `_exception_dict` change gave that row a currency, and the queue
      reads it (§160).
      **Trigger:** the first legacy invoice with no currency, or the next change
      to `utils/money.ts`.

- [ ] **(c) The mobile dashboard and cash-flow screens hide their
      `unconverted_count`, so a part-converted rollup reads as a single-currency
      figure.** Round 31 pointed both screens at the reporting-currency
      counterparts and labelled them with the code the payload names, but
      `GET /dashboard` also returns `reporting.unconverted_count`,
      `aging_reporting.unconverted_count` and a per-bar count on
      `monthly_trend`, and `cashflow_forecast` / `cash_position` return
      `unconverted_count` — rows folded in at FACE value because no rate lock
      bridged them. Non-zero means the labelled total mixes currencies, and the
      cash-position curve carries the balance forward so one unconvertible row
      poisons the tail. "A fallback nobody reports is just a wrong number"
      (decisions §35), and the adaptive patterns tab and the vendor-spend tile
      already disclose theirs.
      **Durable fix:** parse the counts into `DashboardData` / `CashFlowData`
      and render the same disclosure line the adaptive tab uses
      (`adaptivePatternsUnconverted`'s shape — an ARB plural naming the count
      and what it excludes), on the aging band set, the whole-book KPI and the
      cash-position section. Needs new ARB entries in all six locales.
      **Trigger:** the first tenant booking invoices in more than one currency,
      or any change to the dashboard/cash-flow payload parsing.

#### Opened by the GL-accounts slice

#### Opened by the round-31 CI run

- [ ] **(c) The nav role matrix is pinned twice, by hand, and only one of the two
      pins is visible before CI.** Adding a single child row to a nav group in
      round 31 turned a Playwright shard red: `tests-e2e/auth/rbac.spec.ts`
      hardcodes the expected `sectionTabHrefs` set per role, and
      `src/lib/nav.test.ts` hardcodes the same sets independently — the spec's own
      comment says "`nav.test.ts` pins the same set", which is the tell. The unit
      pin was updated with the change; the e2e twin was not, and could not be
      caught locally: `pnpm check` does not typecheck `tests-e2e/`, `--list` only
      proves a spec parses, and running the suite needs the whole stack up. So the
      first signal was a red shard on an unrelated PR, which is the same failure
      mode as [[e2e-spec-syntax-not-typechecked]] one layer up — a guard that only
      speaks in CI.
      **Durable fix:** derive one of the two from the other instead of restating
      it. `nav.ts` already carries `roles` per entry, so the expected set for a
      role is computable: export the filter `Sidebar`/`SectionTabs` already apply
      (or a thin `navFor(role)` beside it), have `nav.test.ts` assert the *policy*
      (which role may see which href, and that each row's roles match its
      backend gate), and have `rbac.spec.ts` assert that the rendered DOM equals
      `navFor(role)` rather than a literal array. Then a new row is a one-line
      change and the e2e proves the wiring rather than re-typing the answer. Keep
      one literal list somewhere deliberate — a computed expectation that reads
      its answer from the code under test proves nothing — so pin the
      href→roles table itself in `nav.test.ts` and let the e2e compare rendering
      against it.
      **Trigger:** the next nav row added, moved, or re-gated — it will cost a red
      shard again otherwise.

- [ ] **(c) Every container image the local stack and CI pull is a floating tag,
      and one of them silently stopped being pullable.** Round 31's CI run failed
      on two backend shards with `pull access denied for minio/minio, repository
      does not exist or may require 'docker login'` — not a code defect and not
      rate limiting: a manifest GET with a valid anonymous Docker Hub pull token
      returns `401` for `minio/minio` while `library/alpine`, `postgres`, `redis`
      and the other eight images the repo pulls all return `200`. MinIO's Docker
      Hub distribution is gated; `quay.io/minio/minio` serves it. Fixed in that
      round by moving all three references (the compose file and CI's two
      `docker run` invocations) to quay.io.
      **What is still open is the class, not that instance.** `minio/minio:latest`,
      `axllent/mailpit:latest`, `ollama/ollama:latest`, `stripe/stripe-mock:latest`
      and `caddy:2-alpine` are all floating tags, so the stack a contributor gets
      depends on the day they pull, and an upstream retag or a registry change
      lands as a red CI run on an unrelated PR — exactly how this one surfaced.
      The images CI depends on for a *green* run are the ones that matter.
      **Corrected 2026-09-17 — the Dependabot half of this entry was wrong, and
      it names the wrong gap.** The entry says "`docker` is the one ecosystem not
      covered". It **is** covered: `.github/dependabot.yml` has two
      `package-ecosystem: docker` entries (`/backend`, `/tools/fake-erp`), and
      both Dockerfiles are already digest-pinned
      (`python:3.14-slim@sha256:…`). Dependabot's `docker` ecosystem reads
      Dockerfiles, which is why the covered surface is the pinned one and the
      uncovered surface is every compose file — the gap is a
      `package-ecosystem: docker-compose` entry, plus the CI service images,
      which no ecosystem reads.
      The entry's tag list is also short. Floating in `backend/docker-compose.yml`:
      `quay.io/minio/minio:latest`, `ollama/ollama:latest`,
      `stripe/stripe-mock:latest`, `axllent/mailpit:latest`, and the loose-but-dated
      `pgvector/pgvector:pg16`, `redis:7-alpine`, `postgres:16-alpine`,
      `localstack/localstack:3`. **`caddy:2-alpine` is not in that file at all** —
      it is in `deploy/compose.prod.yml`, alongside its own `pgvector:pg16` and
      `redis:7-alpine`, so the *production* stack floats too and the entry never
      said so. CI adds `pgvector/pgvector:pg16` and `redis:7`.
      **Durable fix:** pin each to a digest (`image@sha256:…`) or at minimum a
      dated release tag, and add a `docker-compose` ecosystem entry for both
      compose files so the bumps arrive as reviewable PRs rather than as drift.
      Do it in one change across `backend/docker-compose.yml`,
      `deploy/compose.prod.yml` and the CI call sites, and note that pinning MinIO
      means choosing a `RELEASE.*` tag deliberately rather than inheriting whatever
      `latest` was, which is a behaviour decision and the reason round 31 did not
      fold it in.
      **Trigger:** the next time a floating-tag pull breaks a run, or the next
      Dependabot configuration change — whichever comes first.

#### Opened by the password-hash slice

- [ ] **(c) `needs_update` is blind to the bcrypt cost, so a raised
      `DEFAULT_ROUNDS` would migrate nothing.** Round 31 wired
      `pwd_context.needs_update` into both login handlers
      (`services/credential_upgrade.py`), so a row on a deprecated *scheme* now
      re-hashes on its owner's next sign-in. But `needs_update` is exactly
      `identify(h) != scheme`, and `identify` reports only the scheme — never
      the `r=` cost baked into the hash. passlib's own `needs_update` compared
      rounds as well. Today this is inert: `DEFAULT_ROUNDS = 12` is what passlib
      defaulted to and what every row in the column carries, so scheme and cost
      happen to move together. The moment somebody raises the cost (the normal
      response to faster hardware, and the only knob the scheme exposes), every
      existing credential silently stays at 12 while new ones get the higher
      value, and the upgrade path that exists will not notice — a weaker-by-default
      population with no signal that it exists.
      **Durable fix:** have `needs_update` return True when the parsed `r=` of a
      v2 hash is below `DEFAULT_ROUNDS`, and pin it with a test that a hash
      written at a lower cost is flagged while one at the configured cost is
      not. The existing wiring then migrates costs for free. Note
      `tests/test_bcrypt_sha256_compat.py::test_needs_update_flags_exactly_the_schemes_we_no_longer_write`
      asserts the current narrower contract and would need to widen with it; the
      passlib-generated fixtures are at `r=4` and must NOT be regenerated, so the
      test has to construct its own low-cost hash rather than reuse them.
      **Trigger:** the first time anyone proposes raising `DEFAULT_ROUNDS` — and
      the fix must land in the same change, or the bump quietly applies to new
      passwords only.

- [ ] **(c) An SSO-only tenant's legacy hash never upgrades, yet the password is
      still a valid MFA step-up proof.** The round-31 upgrade is wired into the
      two login handlers, and on the employee surface it deliberately sits after
      the `sso_only` refusal: a tenant that has closed password login has made
      that hash unreachable for signing in, so re-encoding it buys nothing
      ([decisions.md](decisions.md) §163). The gap is that password login is not
      the only consumer of `User.hashed_password`.
      `services/mfa.step_up_verified` accepts the shared-context password as
      proof for every factor-management step-up (enroll-start, passkey
      register/delete, `/mfa/disable`), and `api/auth.py::login`'s own comment is
      explicit that `sso_only` is "closed even for users who still carry a
      password hash". So a pre-c6a91396 hash in an SSO-only tenant keeps
      authenticating a security-sensitive operation, on raw bcrypt, forever — the
      truncation weakness surviving in the one place nobody is looking at it.
      **Durable fix:** either upgrade from the step-up path too, or stop
      accepting a password as step-up proof in an `sso_only` tenant. The second
      is the smaller and probably better change — a tenant that has closed
      password login has said the password is not an authenticator there, and
      passkey assertion plus TOTP already cover the SSO-only account the
      assertion door was built for — but it is a policy change with its own
      lockout question (an SSO-only account with neither a passkey nor TOTP), so
      it is not a drive-by. The first needs the step-up helper, which is pure and
      shared by four endpoints on two surfaces, to reach a session and a write.
      **Trigger:** the next MFA/step-up slice, or any decision to enable
      `sso_only` for a real tenant.

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
      § Arming the AWS pipeline.

### Surfaced by the minimal-deploy readiness review (2026-09-15)

- [ ] **(c) Self-service signup has no off switch.** A deployed env refuses to
      boot without `FEOH_HCAPTCHA_SECRET` (`config.py`
      `_require_captcha_in_deployed_envs`), and nothing else gates `/signup` —
      no setting, no plan flag. An operator who wants an invite-only pilot,
      with tenants provisioned by `deploy/add-tenant.sh`, can only leave
      `FEOH_HCAPTCHA_SITEKEY` empty. That fails closed — no widget loads and
      `POST /api/signup/start` answers 400 "Captcha is required." — but the
      page still renders a complete form that a visitor fills in only to be
      refused, and the operator has to hold a captcha secret for a feature
      they turned off. **Durable fix:** a `FEOH_SIGNUP_ENABLED` setting
      (default `true`, today's behaviour) that the three `/api/signup/*`
      routes refuse on, published through `/api/public-config` so the SPA
      drops the route and its links rather than rendering a dead form; the
      captcha boot check then applies only while signup is on. Documented
      workaround meanwhile: `deploy/prod.sops.yaml.example` § hCaptcha,
      `docs/minimal-deployment.md` § 3. **Trigger:** the first deploy whose
      tenants are all provisioned by hand, or the first report of a refused
      signup.

### Surfaced by fixing the three privacy defects (2026-09-16, issues #423/#424/#425)

- [ ] **(c) The unmasked-DSAR gate does not bite on a stock admin.**
      `POST /api/privacy/dsar` now masks banking by default and gates
      `include_banking` on the `vendor.bank_change.approve` permission plus a
      written justification, with its own `privacy.dsar_export.unmasked` audit
      row (`docs/decisions.md` §182). But `ROLE_ADMIN` resolves to the entire
      permission catalogue, so on the four stock system roles that gate admits
      exactly the callers `require_roles(ROLE_ADMIN)` already admits — it only
      becomes a real refusal once an org defines an admin-equivalent **custom**
      role without that permission. The routine path is fixed (no admin gets a
      full account number by accident any more); what is not yet true is that
      producing one requires proving who you are. **Durable fix:** require a
      **step-up MFA proof** on the request when `include_banking` is set —
      `api/auth._require_mfa_step_up` / `_step_up_satisfied` already implement
      TOTP, email-OTP and WebAuthn proofs for exactly this shape, so the backend
      half is a dependency and a schema field. It is deferred because the other
      half is the SPA collecting that proof before it posts the DSAR, which is a
      frontend change this backend-scoped batch could not make, and a
      half-landed gate that 403s every unmasked export until the UI catches up
      would be worse than the one that ships. **Trigger:** the next change that
      touches `frontend/src/routes/organization` privacy surfaces, or the first
      org that asks to split the privacy-officer duty from vendor bank-change
      approval.

- [ ] **(c) `/legal/privacy` §12 now understates what erasure and export do.**
      That section lists "four gaps we are not going to describe around": the
      export returns counts rather than content and omits passkeys / expense
      reports / contracts / virtual cards; it does not include uploaded
      documents; erasure does not delete stored documents; erasure does not
      revoke passkeys or terminate sessions. **Three and a half of the four are
      no longer true** as of 2026-09-16 — the export returns the content and all
      of those categories plus a document manifest, erasure deletes the
      sole-subject documents, and it deletes passkey rows and revokes sessions.
      What remains true is narrower and worth saying precisely: erasure
      deliberately RETAINS transaction evidence (invoice PDFs, contract
      documents, expense receipts, vendor statements) on the same basis as the
      invoice rows, and each collection in the export is capped at 1000 rows. The
      page is more conservative than the product, so nothing published is
      untrue — but a privacy page that understates the automated path sends data
      subjects to a manual process that no longer needs to exist, and the DPA's
      deletion clause (`docs/founder-runbooks/dpa-template.md` § Annex II, already
      corrected) now disagrees with it. **Durable fix:** rewrite §12 to state the
      retain-vs-delete split (`backend/docs/privacy.md` § Stored documents) and
      the row cap, and re-check the DPA page's deletion clause against it.
      Deferred only because it lives under `frontend/`, which the backend batch
      that made it stale was scoped out of. **Trigger:** the next change that
      touches `frontend/src/routes/legal/`, or a customer DPA review — whichever
      comes first.

### Surfaced by the round-26 parallel batch (2026-09-17)

Five agents, each in its own worktree, closed eleven entries and issue #432's
engineering half. Every one of these was found by doing the work rather than by
reading the entry — and **three entries were materially wrong about their own
code**, twice in the direction that would have caused damage. That is now the
seventh round running where an entry's own account of its scope was the least
reliable part of it.

- [ ] **(c) A consolidated-view GL pick is still not validated against the invoice's own entity.**
      Round 26 made the ambiguity *visible* — entity-scoped options now carry the owning
      entity's name — but the picker still offers, and the server still accepts, subsidiary
      B's `6000` for a subsidiary-A invoice. The stored string then resolves against A's
      chart. Labelling was the half that fit in the frontend; refusing is the half that does
      not.
      **Durable fix:** expose the invoice's own `entity_id` on `InvoiceResponse` (it carries
      only `counterparty_entity_id` today) so the picker can scope to that invoice's
      effective chart, and validate on manual write — `gl_account_invalid` is raised today
      only by `services/extraction.py` and `gl_recode`, never by a PATCH, so a hand-typed or
      cross-entity code is accepted silently.
      **Trigger:** the first multi-entity tenant whose subsidiaries define overlapping codes.

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

- [ ] **(c) The new `PATCH /api/gl-accounts/{id}` has no caller.** The endpoint (correct /
      retire, with a `parent_code` cycle guard and an audit row per changed field) landed in
      round 26; `/gl-accounts` still renders no row actions, so an account is still
      create-only from the UI. The backend agent's boundary stopped at `backend/`.
      **Durable fix:** row actions on `/gl-accounts` for edit + deactivate/reactivate, and
      show retired rows under a filter rather than hiding them — the list endpoint already
      filters on `is_active`.
      **Trigger:** the next change to `/gl-accounts`.

- [ ] **(c) The exception `amount` still crosses the wire as a JSON number.** Round 26 made
      every money serializer exact (51 sites) but deliberately preserved the wire shape.
      Moving `amount` to an exact string is blocked on the clients:
      `mobile/lib/models/exception.dart:131` parses `(json['amount'] as num?)?.toDouble()`
      and would throw at runtime on a string.
      **Durable fix:** a coordinated backend + web + mobile change, one field at a time,
      each client tolerant of both shapes before the server switches.
      **Trigger:** the next deliberate wire-format slice — not a drive-by.

- [ ] **(c) The invoice-warning message catalogue is generated for the web only.**
      `backend/scripts/gen_invoice_warning_messages.py` emits
      `frontend/src/lib/api/invoiceWarningMessages.generated.ts`; round 26 hand-transcribed
      the same 48 codes into mobile's ARB files. A parity test
      (`mobile/test/l10n/invoice_warning_messages_test.dart`) reads the generated TypeScript
      and reddens mobile CI when the codes or parameter kinds diverge, so the duplication is
      guarded rather than silent — but it is still duplication.
      **Durable fix:** teach the generator to emit the Dart/ARB half beside the TypeScript
      one, and drop the parity test to a generated-file drift check like the others.
      `backend/docs/invoice-warnings.md` describes only the web client and understates this.
      **Trigger:** the next new warning code, which is the moment the duplication costs
      something.

- [ ] **(c) `BadgeTone` lives in a `.svelte` module, so `tests-e2e/` cannot import the types
      that reference it.** `$lib/types/vendor.ts` does `import type { BadgeTone } from
      '…/ui/Badge.svelte'`, and plain `tsc` resolves `*.svelte` through an ambient shim with
      no named exports — `TS2614` under `pnpm check:e2e`. So an e2e fixture cannot
      `satisfies` any type that transitively touches it, against the house rule that
      fixtures are type-pinned; round 26 had to skip that on one new fixture and say why
      inline.
      **Durable fix:** move `BadgeTone` into a `.ts` module and re-export it from
      `Badge.svelte`. 28 files import it, so it is its own mechanical change and a poor
      passenger on anything else.
      **Trigger:** the next e2e fixture blocked by it, or any refactor already touching
      `ui/Badge.svelte`.

- [ ] **(c) `DataTable`'s scroll container is not keyboard-pannable.** `.grid-container`
      scrolls horizontally but carries no `tabindex="0"`, so a table whose cells hold
      nothing focusable cannot be panned by keyboard at narrow widths. Round 26 gave its
      two new `/cfo` scrollers the attribute rather than change the shared component while
      four agents were in that tree. Not a 1.4.10 failure — the table scrolls rather than
      overflowing the document — but it is adjacent to 2.1.1.
      **Durable fix:** `tabindex="0"` plus an accessible name on `.grid-container` in
      `ui/DataTable.svelte`, and a check in the a11y suite that a horizontally-scrollable
      region is reachable.
      **Trigger:** the next change to `ui/DataTable.svelte`.

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

- [ ] **(c) `frontend/CLAUDE.md` documents a `t()` that does not exist.** It tells
      contributors to put user-facing strings through `t()`; the runtime function is `m()`,
      which is what every call site uses. A binding instruction file naming the wrong
      function is the kind of error that costs a newcomer an hour.
      **Durable fix:** correct the reference, and grep the per-area `CLAUDE.md` files for
      other stale API names while there.
      **Trigger:** the next edit to `frontend/CLAUDE.md`.

### Surfaced by the /polish-ui pass on /exceptions and /credit-memos (2026-09-17)

Mirrored as GitHub issue [#443](https://github.com/Absence0760/feohledger/issues/443).

Two `ui-polisher` runs against the refreshed agent definition. Both pages landed
their visual and correctness work; everything below is what the polish could not
reach because it needs a router change, a six-locale catalogue tranche, or a
call on test infrastructure. The pattern worth noting: **both pages were missing
the same two primitives for the same reason** — the shared component exists, the
backend parameter does not.

- [ ] **(c) Neither `/exceptions` nor `/credit-memos` can be searched or sorted — the backend has no parameters for it.**
      `GET /api/exceptions` and `GET /api/credit-memos` accept status/severity plus
      pagination and nothing else. That is why both pages ship with zero `SearchBox` and
      zero `SortableHeader` while five sibling list routes have both, and why neither
      polish pass added them: approximating search with a client-side `.filter()` over the
      one loaded page is the anti-pattern `frontend/docs/ui-patterns.md` § Search forbids,
      because it silently searches a page instead of the set.
      **Durable fix:** add `search` plus a sort allowlist to both routers — exceptions over
      invoice number + vendor with sorts on `created_at` / `severity` / due, credit memos
      over `memo_number` + `vendor_name` with sorts on `issued_date` / `amount` /
      `memo_number`. Both pages then take the shared primitives with no new UI patterns.
      **Trigger:** the first tenant whose exception queue or credit-memo list exceeds one
      page of 20 — a triage queue that cannot be sorted by due date is the first thing an
      AP manager asks for.

- [ ] **(c) The exception `severity` filter exists on the backend and nothing in the UI exposes it.**
      `GET /api/exceptions?severity=` has always worked; the queue offers status and type
      chips only. Cheap on its own, but it needs a third chip row and catalogue keys in six
      locales, which is more than a polish pass should mint.
      **Durable fix:** a third `FilterChips` row bound to `?severity=`, with the keys added
      to all six catalogues in the same change.
      **Trigger:** bundle it with the search/sort work above — same file, same tranche.

- [ ] **(c) `/credit-memos` filter chips carry no counts, because there is no per-status count endpoint.**
      `/exceptions` has `GET /api/exceptions/summary` feeding its chip tallies; credit memos
      has no equivalent, so its chips are bare labels and the operator cannot see how many
      open memos exist without clicking through.
      **Durable fix:** a `GET /api/credit-memos/summary` mirroring the exceptions one —
      including taking `?status=`, which is the defect the exceptions summary just had to be
      fixed for (see the same-day commit scoping `by_type` to the viewed status).
      **Trigger:** same tranche as the two above.

- [ ] **(c) The exceptions toasts assemble English grammar from a verb stem, so they cannot be translated.**
      `commitResolve` builds `` `Exception ${action}d` `` and
      `` `${body.updated} ${action}d, ${skipped} skipped` `` — English morphology in a
      template literal, which no catalogue key can express. Alongside them sit hardcoded
      `'Failed to load exceptions'`, `'Resolution note is required'`, `'Action failed'`,
      `` `Selected all N matching exception(s)` `` and three `ariaLabel`s on `Tabs` / `Modal`.
      **Durable fix:** one message key per action outcome rather than stem assembly, added
      to all six catalogues. **Sequencing matters:** `tests-e2e/exceptions/resolve.spec.ts`
      and `load-sequencing.spec.ts` select on the modal's exact English `ariaLabel`, so
      those specs must move to a stable selector *before* the strings are translated, or
      they break on the locale that isn't English.
      **Trigger:** the next i18n tranche that touches the exceptions surface.

- [ ] **(c) `extractError()` on `/exceptions` bypasses `$lib/utils/apiError.ts`, so a 422 renders as `[object Object]`.**
      It hand-rolls `e?.detail ?? e?.message`. FastAPI returns a *list* of validation objects
      for a 422, which stringifies to `[object Object]` — precisely the bug `formatApiDetail`
      was written to fix.
      **Durable fix:** swap `extractError` for `formatApiDetail`. One line, but it changes
      what a refused segregation-of-duties resolve tells the operator, which is a money-path
      message — so it ships with a test that asserts the refusal text, not on its own.
      **Trigger:** the next change to the exception resolution path.

- [ ] **(c) A credit memo cannot be linked to an invoice at creation, and cannot be edited afterwards.**
      `POST /api/credit-memos` accepts `invoice_id`, but the create modal never sends one;
      and there is no `PATCH` on the resource at all. Because apply is currency fail-closed,
      a memo created with the wrong currency is permanently unfixable *and* unappliable —
      the only exit is Void and re-create, which leaves a void row in the audit trail for
      what was a typo.
      **Durable fix:** expose the invoice link in the create modal, and add a `PATCH`
      restricted to memos in `open` (never one already applied — that would rewrite a
      settled money record).
      **Trigger:** the first support request about a mis-keyed credit memo.

- [ ] **(c) Four `/credit-memos` e2e specs fail against a local dev server while CI is green — root cause unknown.**
      `tests-e2e/credit-memos/load-sequencing.spec.ts` (×3) and `void-confirm.spec.ts` fail
      locally with the row absent (`getByRole('table').getByRole('button', {name: 'Void'})`
      not found) even though the spec **mocks** the list response, so tenant data cannot be
      the cause. Established: they fail identically **at HEAD on a clean tree** (stash-tested,
      15 passed / 4 failed both with and without the polish changes), a fresh dev server with
      a rebuilt `.svelte-kit/generated` does not clear it, and the most recent `main` CI run
      passed including all 14 Playwright shards. So it is neither the polish work nor a
      product defect CI can see. Not yet in `known-issues.md` because there is no root cause
      to record there — only a localisation.
      **Durable fix:** determine whether this is a dev-server-versus-preview-build difference
      (CI serves a preview build; these runs used `vite dev`) or a local harness/tenant-slot
      issue, then fix the real cause. If it proves to be dev-only, the specs should say so
      or the local runner should serve a preview build, because four permanently-red specs
      locally is how a genuinely red one gets ignored.
      **Trigger:** the next time anyone runs the credit-memos e2e locally — or sooner, since
      the cost of a standing local red is paid by every contributor.

### Surfaced by widening the backend shard matrix (2026-09-17, issue #444)

- [ ] **(c) The `pytest-split` baseline is stale, and regenerating it is the half
      that is still unbuilt.** `backend/.test_durations` has exactly one commit in
      its history (`aa3d47bd`, 2026-09-04): 8,143 entries against 10,123 collected
      tests, so **20.1% of the suite is partitioned at the *mean* test duration**
      rather than its own, 58 entries name tests that no longer exist, and its
      absolute figures (1,628s) understate the real ~60 min of CI pytest by ~2.2x.
      **The drift now has a voice** — `scripts/check_test_durations.py`
      (`pnpm check:test-durations`) fails past a ratcheted ceiling and runs in CI's
      `Backend lint` job, which is the "cheap guard" half of this entry's original
      durable fix. What is left is the baseline itself.
      **This is balance, not breakage.** Two independent 8-shard runs measured
      7m58s–10m36s, a **1.33x spread** with ~3.8x headroom under the 40-minute cap;
      the 4-shard layout it replaced was **1.12x** (16m16s–18m17s) and failed only
      because ~17 min against a 40-minute cap leaves nothing for a slow runner. The
      one 16m46s outlier on #447's own run was a degraded runner, not slice
      composition — every other shard on that run matches main within ~40s.
      Coverage also overstates the risk on its own: the uncovered tests are
      overwhelmingly cheap parametrized meta-tests (`test_migration_model_index_parity.py`
      alone is 229 of them), for which the ~0.2s mean is about right. The dangerous
      shape is a contiguous block of *slow* tests going uncovered, because
      pytest-split cuts contiguous slices — that is what put the realdb hot zone
      (`test_e*`–`test_i*`) on a single shard.
      **Durable fix:** regenerate with `pytest --store-durations`, ideally on a CI
      runner rather than a laptop (balance is only meaningful against the hardware
      that runs it) — a `workflow_dispatch` job that stores durations and uploads
      the file. Then lower `MAX_MISSING_FRACTION` to match. **Never raise it.**
      **Trigger:** the guard firing, or a shard's median approaching ~15 min (where
      the documented 3x runner headroom runs out under a 40-minute cap).

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
      each jurisdiction, which needs an entity to contract as.
      **Durable fix:** incorporate, engage both, then set `euRepresentative` and
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
      **Why blocked:** a contract needs a party, so this waits on incorporation
      exactly as the representatives above do.
      **Durable fix:** incorporate, execute the Clauses provider by provider,
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
