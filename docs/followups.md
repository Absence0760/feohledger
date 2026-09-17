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

**Last reconciled:** 2026-09-17 — a housekeeping pass that pruned completed
work rather than closing open work. **54 checked entries went, 21 of the 41
sections emptied out with them, and two prose-only CLOSED narratives were
pruned**, taking the file from 2570 lines to 1215. Nothing open was removed.

**This file had stopped obeying its own first rule.** "Open items only" is the
line at the top, and 54 `[x]` entries plus their surrounding CLOSED narrative
had accumulated beneath it — more than half the file describing work already in
git history, with its reasoning already in [decisions.md](decisions.md). A
reader looking for what is open was reading mostly what is not. Every pruned
section carried its own `decisions.md` § reference, so nothing was lost by
deleting it; that cross-reference is what makes the pruning safe, and writing
one is what earns a future entry the right to be deleted.

**54 open: 39 (c) · 9 (a) · 6 (b)** — re-derived from the file, never carried
forward. The section heading is authoritative; where an entry also carries a
`(c)`/`(a)`/`(b)` marker, the two now agree.
`grep -c '^- \[ \]' docs/followups.md`.

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

- [ ] **(c) Mobile renders `warning.message`, so the app is English for a German user.** `mobile/lib/
      widgets/invoice_warnings_panel.dart` reads `InvoiceWarning.message` and `mobile/lib/models/
      invoice.dart` does not parse `code` / `params` at all. That is not a regression — it is exactly
      what shipped before round 31, via the fallback that exists for it — but the web app now reads
      the same payload in six languages and mobile reads it in one.
      **Durable fix:** parse `code` + `params` on the model and resolve them through the ARB
      catalogue, which needs the generator to emit a Dart/ARB half beside the TypeScript one so the
      two cannot drift (the same "generated, not hand-written" constraint, a second target). The
      per-kind formatting has a mobile home now — `utils/money.dart` (§160) — so the money params
      land in the row's own currency rather than reintroducing the `$` §157 just removed.
      **Trigger:** the next mobile invoice-detail slice.

#### Opened by the label-map slice

- [ ] **(c) The "Select all N matching" bulk affordance is hardcoded English on five
      routes, on pages `frontend/docs/i18n.md` lists as fully extracted.**
      `/exceptions`, `/invoices`, `/vendors`, `/contracts` and `/expenses` each carry
      `` `Select all ${total} matching` `` and `All matching selected` as literals in
      their `BulkBar` actions snippet. `/payments` is the only one keyed
      (`payments.queue.selectAllMatching` / `.allMatchingSelected`), so the strings
      already exist in all six locales — under a namespace the other five must not
      borrow, since `pagedListFooter.test.ts`' per-namespace pairing is the precedent
      against reaching into a sibling's keys (decisions §155).
      **Durable fix:** decide the owner first, because there are five copies of one
      string and a sixth already keyed: either `common.selectAllMatching` /
      `common.allMatchingSelected` (the `common.all` / `common.loading` precedent —
      the wording is identical on every surface and the only variable is `{total}`),
      or move both strings into `ui/BulkBar.svelte` itself so the affordance carries
      its own copy and no route can forget. Then migrate all six call sites, including
      `/payments` off its private pair, and add a source-scan guard in the
      `listJoinAudit` / `pagedListFooter` shape so a seventh `BulkBar` cannot ship a
      literal. Correct the "bulk-bar" claims in `frontend/docs/i18n.md` for each of
      the five routes in the same change.
      **Trigger:** the next i18n slice, or the next change to `ui/BulkBar.svelte` or
      any of the five routes' bulk paths — whichever comes first. Do it as one commit
      across all six, not per route: five of them drifting from a sixth is how this
      started.

- [ ] **(c) The cookie-consent banner is entirely hardcoded English, on every
      page of the app.** `lib/components/ConsentBanner.svelte` contains **no
      `m()` call at all** — "Your privacy choices", "Strictly necessary (always
      on)", "Analytics (optional)", both button labels, the `aria-label` and the
      "Cookie Notice" link text are literals. It mounts from the root
      `routes/+layout.svelte`, so it is the **first** thing a `de` / `es` / `fr`
      / `ja` / `pt-BR` visitor sees, before any localized surface, and it is the
      one surface they must interact with to dismiss.
      This is not only an i18n defect. The banner is the ePrivacy Art 5(3)
      consent gate, and consent has to be *informed* — a consent dialog a reader
      cannot read is weak evidence that consent was given, which is the same
      standard `/legal/cookies` is written to. It is the same class as the two
      legal link surfaces closed on 2026-09-16, and the reason it was missed is
      that it is a component rather than a route, so a per-route i18n audit never
      reached it.
      **Untracked until 2026-09-17** — noted in issue #321's correction pass and
      filed here, which is the destination guard rail 6 requires.
      **Durable fix:** key every string under a `consent.*` namespace with real
      translations in all six catalogues (`messages_parity.test.ts` enforces the
      parity once the keys exist), and extend whichever source-scan guard the
      `listJoinAudit` / `pagedListFooter` shape establishes to cover
      `lib/components/*.svelte`, not just routes — a component-shaped blind spot
      is what let this sit.
      **Trigger:** the next i18n slice, or any change to the consent banner or
      the cookie notice — whichever comes first.

- [ ] **(c) `/exceptions`' `severity` cell is the last data-driven badge on the row
      still printing its raw wire value.** With the lifecycle status and the type
      label keyed (round 31), `<span class="severity">{exc.severity}</span>` prints
      `error` / `warning` / `info` in lowercase Latin beside cells that are now
      translated in all six locales. There is no label map for severity anywhere in
      the tree, on either surface — the `SEVERITY_COLORS` map in the route is the only
      place the vocabulary is written down, and it is a colour map, so nothing catches
      a fourth severity arriving unlabelled.
      **Durable fix:** `EXCEPTION_SEVERITIES` / `EXCEPTION_SEVERITY_LABEL_KEYS` in
      `types/exception.ts` beside the status pair, with `SEVERITY_COLORS` moved in and
      retyped over the union so a tinted-but-unlabelled severity is a compile error
      (the `EXCEPTION_STATUS_TONES` pairing), a tolerant accessor, three new keys in
      each of the six locales, and a roster drift guard against the backend —
      `models/exception.py` declares the three in a comment on the column only, so the
      guard should pin whatever constant the backend grows, or the backend should grow
      one (`exception_lifecycle.py` is where the type roster and the status maps
      already live).
      **Trigger:** the next /exceptions slice, or the next time a severity is added or
      renamed backend-side.

#### Opened by the mobile-currency slice

- [ ] **(c) Mobile renders a payment run's total bare, including on the dialog
      that authorizes execution — the server now supplies the currency it needs.**
      *Corrected 2026-09-17: the server half of this entry is done.* The entry
      was written when `payment_runs.total_amount` was an unlabelled
      `SUM(Payment.amount)` with no way to say what it was denominated in. Since
      then the run builder refuses a mixed-currency run outright and
      `PaymentRunResponse.currency` derives the code from the legs
      (`api/payments.py::_one_currency`, plus its list-endpoint counterpart), so
      **both** the runs list and the run detail serve a currency — `None` only
      where it genuinely cannot be proven. The web app reads it.
      **What is left is mobile, and it is one field.**
      `mobile/lib/models/payment_queue.dart::PaymentRun` declares no `currency`
      and `fromJson` never parses it, so `_runTotal` passes `currency: null`
      unconditionally and four call sites in `payment_queue_screen.dart` render
      the figure bare — the runs list, the sign-off row, the detail sheet and
      `payRunExecuteBody`, the execute confirmation. The class docstring and
      `_runTotal`'s comment both still explain the absence as a property of the
      data, which was true when written and is now stale.
      **Durable fix:** parse `currency` in `PaymentRun.fromJson`, pass it to
      `_runTotal`, and rewrite both comments to say the code is served and
      `null` means unprovable. `formatMoneyString` already renders bare on
      `null`, so the mixed-run case keeps the behaviour §160 chose.
      **Trigger:** the next change to the mobile payment-queue screen.

- [ ] **(c) Mobile number, date and currency formatting ignores the in-app
      locale picker.** `mobile/docs/i18n.md` claims "every number, date and
      currency renders through the locale-aware helpers". The strings are
      localized in all six locales, but no call site passes a `locale`:
      `utils/money.dart` accepts one and nothing supplies it, every `DateFormat`
      is constructed without one, and `Intl.defaultLocale` is never set — so a
      German user reads German copy with `1,234.50` and `Mar 4, 2026`. The web
      counterpart solves this with `i18n/formatLocale.ts::getActiveFormatLocale`,
      which `formatMoney` reads by default.
      **Durable fix:** the mobile mirror of that — a helper resolving
      `LocaleStore.instance.locale` (falling back to the platform locale) that
      `formatMoney` / `formatMoneyString` / `formatMoneyCompact` and every
      `DateFormat` construction default to, set once so a picker change
      re-formats live. Note it changes the expected string in every widget test
      that asserts a formatted figure or date, so it is its own change rather
      than a rider.
      **Trigger:** the first non-English tenant on mobile, or a bug report that
      the picker changes words but not numbers.

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

- [ ] **(c) `_exception_dict` serves money as a `float` across the API boundary.**
      `api/exceptions.py` sends `float(inv.amount)` for the related invoice's amount,
      where `schemas/money.py::MoneyAmount` exists precisely so a `Decimal` crosses
      the wire as its exact digits. Pre-existing rather than introduced by round 31 —
      which only added the `currency` beside it (§160) — and the reason it was not
      fixed there is that the mobile `Exception` model types the field `double` and
      the web consumers read a number, so the model, both clients and this serializer
      have to move together.
      **Durable fix:** `MoneyAmount` on the serializer, `String`-parsed-to-`Decimal`
      on the clients (the payment-queue and cash-flow payloads already ship money as
      strings for exactly this reason, and `formatMoneyString` on mobile already takes
      the exact-decimal path), then a test that a cent-precise amount survives the
      round trip. Note the amount is display-only on both surfaces — nothing computes
      with it — so this is an invariant repair, not a live money defect.
      **Trigger:** the next change to the exception serializer or either exception
      client, or the next money-precision sweep.

#### Opened by the GL-accounts slice

- [ ] **(c) A GL account can be created but never corrected or retired.** `app/api/gl_accounts.py`
      has `GET ""`, `POST ""` and `POST /sync-erp` and **no PATCH and no DELETE**, which is why
      `/gl-accounts` ships with no row actions. An account created with the wrong name, type or
      parent — or, worse, into the wrong chart, since scope is taken from `X-Entity-ID` at create
      time — is permanent. And nothing under `app/` ever writes `GLAccount.is_active`: the ERP sync
      updates `name` / `account_type` / `erp_account_id` only, so `is_active = false` is reachable
      solely by direct SQL or an imported chart. The page's *Include inactive* filter is still
      correct (the endpoint defaults to hiding those rows, and a migrated chart can carry them, so
      without the toggle they are invisible with no explanation) — but the state it reveals is one
      the product cannot produce.
      **Durable fix:** `PATCH /api/gl-accounts/{id}` over `name` / `account_type` / `parent_code` /
      `is_active`, with a `gl_account.updated` audit row, surfaced as a `RowAction` gated on the
      same `auth.isManager` the two existing writes use. Two fields must stay immutable and the
      reason belongs in the route: `code`, because an invoice records its GL as a **string** and
      renaming the code orphans every line already coded to it; and `entity_id`, because moving an
      account between charts either steals it from every other entity or hands it to all of them,
      and the effective-chart uniqueness guard would have to be re-run against both the old and the
      new scope. A genuine "move between charts" is a create + deactivate, not a PATCH.
      **Trigger:** the first tenant that mistypes a GL name, or retires an account and finds the
      only way to do it is `psql`.

- [ ] **(c) Two invoice modals still fetch the chart of accounts by hand.**
      `lib/components/modals/CreateInvoiceModal.svelte` and `lib/components/modals/InvoiceModal.svelte`
      each declare their own inline `GLAccountOption` (the second one narrower still — `{code, name}`)
      and call `api.get('/api/gl-accounts')` directly, bypassing `lib/api/glAccounts.ts`. Round 31
      made `types/glAccount.ts` the single owner and collapsed the two `api/` copies
      (`catalogs.ts`, `expenses.ts`) onto a `Pick` of it; these two were left declared because four
      sibling agents were live in the same tree that round and both files are heavily e2e-covered,
      so the import churn was not worth the merge surface. It is a two-line edit per file, and while
      it stands the endpoint's shape is described in three places instead of one.
      **Durable fix:** replace both inline interfaces with `import type { GlAccountOption } from
      '$lib/types/glAccount'` and both fetches with `listGlAccounts()`.
      **Trigger:** the next change to either invoice modal, or the next change to the
      `/api/gl-accounts` response shape — whichever comes first, since the second one is when the
      drift starts costing something.

- [ ] **(c) The GL pickers cannot tell two subsidiaries' identical codes apart.** `/gl-accounts`
      now renders a **Scope** column because the consolidated view returns every entity's chart at
      once and two subsidiaries may each legitimately hold their own `6000` — but the four pickers
      reading the same endpoint (invoice line coding, expense coding, requisition lines, catalog
      items) still render only `code — name`, so in the consolidated view they offer two
      indistinguishable options that code to different accounts.
      **Corrected 2026-09-17 — this is worse than the entry said, in two ways.**
      The entry claimed "the picker value is the uuid `id`, so the *write* is
      unambiguous". That holds for three of the four pickers — `ExpenseModal`,
      `RequisitionModal` and `CatalogModal` all bind `value={g.id}` — but **not
      for invoice line coding**, the highest-volume of them:
      `InvoiceModal.svelte` binds `value={acct.code}` at both its line-coding and
      its split sites, so two subsidiaries' `6000` are the *same* value and the
      write is ambiguous, not merely the choice. It also claimed the pickers
      "already receive `entity_id`, so the data is in hand" — the endpoint serves
      it, but `types/glAccount.ts::GlAccountOption` is
      `Pick<GlAccount, 'id' | 'code' | 'name' | 'account_type'>` and drops
      `entity_id` before any picker sees it.
      **Durable fix, in order:** widen `GlAccountOption` to carry `entity_id`;
      move `InvoiceModal` onto the uuid `id` like its three siblings (a write
      change — check what reads `gl_account` as a code first); then, when
      `entityStore.multiEntity`, append the resolved entity name to an
      entity-scoped option's label and leave a shared one bare — the same
      shared-vs-owned distinction the Scope column draws, extracted as one helper
      beside `GlAccountOption` rather than the same conditional in four pickers.
      **Trigger:** the first multi-entity tenant whose subsidiaries define overlapping GL codes.

- [ ] **(c) `/vendors/change-requests` leaves stale rows on screen after a failed
      re-load.** Its `catch` sets `errored` but never clears the row array, so a
      second failed load relabels the *previous* filter's rows as the answer to
      filters it never ran — the identical gap round 31 fixed on `/gl-accounts`
      after the reviewer caught it there, and the shape `/exceptions` already
      handles correctly by clearing. On a bank-detail dual-control queue, showing
      the wrong set of pending change requests is the worst place in the app for
      it. It was left because the file was another agent's likely territory that
      round and nobody ended up owning it.
      **Durable fix:** clear the rows in the `catch` the way `/gl-accounts` and
      `/exceptions` do, and add the route to the parameterized
      `tests-e2e/reactivity/list-load-failure.spec.ts` sweep, which is where the
      first-load half is already covered for every other list.
      **Trigger:** the next change to that route — it is a one-line fix plus a
      sweep entry, so it should ride the next thing that touches the file.

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

## (a) Blocked on external credentials, accounts, or hardware

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
      `feohledger.com` (`frontend/src/lib/legal/operator.ts` → `CONTACT`). None
      of them exists yet. A published `mailto:` that bounces is worse
      than none at all, because a data subject who writes to it reasonably
      believes the request is made and the Art 12(3) one-month clock has started.
      *Corrected 2026-09-17: the mail DNS is no longer a blocker.* The entry said
      it sat on the unmerged `feat/infra-email-dns` branch; that branch merged as
      **#417** (`98fd5ff0`, Migadu mailboxes + SES app mail), so what is left is
      the operator step alone. The stale `origin/feat/infra-email-dns` remote
      branch can go with it.
      **Durable fix:** create the five aliases and send a test to each before the
      pages are linked from anywhere public.
      **Trigger:** before `feohledger.com` serves the app to anyone outside the
      project.
      Ref: [decisions.md](decisions.md) §175.

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
      **Corrected 2026-09-17 — the counsel questions are not where this entry
      says.** It points at `reviews/saas-legal-review-legal-pages.md` and
      `reviews/us-legal-review-legal-pages.md`. `/reviews/*` is gitignored
      (`.gitignore:211`) so neither is in the repo for anyone but their author,
      the second was never written at all, and neither exists on the working
      machine today — the directory holds only its `README.md`. Whoever picks
      this up re-derives the questions from the documents themselves. If those
      questions are worth citing, they belong in a tracked file; a pointer into
      an ignored directory is a pointer to nothing.
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

