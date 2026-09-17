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

**Last reconciled:** 2026-09-14 (round 31) — five agents, each in its own git
worktree, plus integrator verification of the merged branch. **Six** entries
closed, **eighteen** opened. **29 → 41** — by category, **31 (c)** · **7 (a)** ·
**3 (b)**.

**Since:** two (c) entries added outside a round — the GitHub deploy role
(2026-09-14) and the missing self-service-signup switch (2026-09-15) — then
**twelve** from publishing the legal document set and reviewing it
(2026-09-15/16, [decisions.md](decisions.md) §175): nine (c), two (b) and one
(a). Six of those twelve closed on 2026-09-16 (the accessibility statement, the
i18n link surfaces, the sanctions-minimisation guard, the register drift guard,
the transfer-safeguards sentence — whose contractual half moved to (a) — and the
60-day backup deletion, now `deploy/remove-tenant.sh`).

**50 open: 38 (c) · 9 (a) · 3 (b)** — counted from the file rather than carried
forward. The previous line claimed 55 · 42 · 8 · 5, and the (b) count had been
wrong since before the legal set: three entries, described as five. A follow-up
file that miscounts itself is the same failure `known-issues.md` fixed in its
own header, so the count here is now something to re-derive rather than
increment:
`grep -c '^- \[ \]' docs/followups.md`.

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

**Exception resolution stays held.** It remains the only segregation-of-duties
item in the file and was deliberately untouched for the eighth round running,
pending the standing "loop in the CISO / Security Analyst" gate on that section.

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

### Backend capabilities with no production caller — CLOSED

All eight entries that stood here were landed in one five-agent round (see
`git log --oneline` for `round7/*`). Each was a built, tested, documented
capability that nothing in `app/`, `scripts/` or `alembic/` reached; three of
them turned out to be masking a live defect rather than merely being unwired:

- `analytics.compute_dpo_trend` — the two inline copies had already **diverged**
  (one excluded `rejected` invoices from the COGS proxy, the other didn't), so
  `/api/analytics/drill/dpo` reported 3.0 days where the chart it explains
  showed 30.0.
- `workflow_engine.is_known_step_type` — `POST /api/workflows/import` is the one
  save path a Pydantic `Literal` doesn't constrain, so a typo'd `"aproval"`
  persisted and was silently skipped at runtime, which the engine reads as *no
  approval step configured*. A spelling mistake could drop a financial control.
- `international_payments.is_international_payment` — unifying the three
  hand-rolled rail sets exposed that a per-org `high_risk_corridor_methods`
  entry of `"SEPA"` (or a blank `[""]`) made `_kyc_required_for` fail **open**,
  disabling the KYC gate for that corridor — or, for a blank entry, for every
  corridor.

The remaining five (`expense_policy.mileage_reimbursement`, Teams outbound
approval actions, sanctions `ScreeningResult.categories`,
`data_residency.check_residency_alignment`, the `avalara`/`taxjar` skeleton
probes) were wiring gaps as described, and are now wired, tested and documented.

Rationale for the non-obvious calls made while closing them:
[decisions.md](decisions.md) §31–§34.

### Consistency debt the round-12 sweep surfaced rather than introduced

The email-regex entry that stood here is **closed** — hoisted to
`app/utils/emails.py::looks_like_email` with a drift-guard test. Closing it found
a live hole: all three copies ended in `$`, which in Python matches end-of-string
*or just before a trailing newline*, so `"user@example.com\n"` passed every check
and was stored as a login, a child-tenant admin address and a scheduled-report
recipient — a newline reaching an SMTP header is the header-injection primitive.
The shared pattern anchors with `\Z` ([decisions.md](decisions.md) §50).

The `date.today()` sweep is likewise done: **no `.today()` call remains anywhere
under `backend/app/`**, and `tests/test_utc_today.py` now guards 31 modules with a
scanner that catches `date.today()`, `datetime.today()`, `datetime.date.today()`
and naive `datetime.now().date()`. Widening it exposed a hole in the guard itself
— it matched only `ast.Name`, so the attribute-shaped `datetime.date.today()` was
invisible, which is exactly what both Positive Pay modules used; either could have
sat on the "converged" allowlist while still reading local time
([decisions.md](decisions.md) §51). What is left is cosmetic:

The last of it is **closed** too. The six modules that inlined
`datetime.now(UTC).date()` — `api/api_keys`, `api/bank_reconciliation`, the
recurring / contract-renewal / discount-auto-capture sweeps and the mock
financing adapter, plus `api/cash_flow` and the four copilot tools that
predated the helper — now import `utc_today`, and the guard stopped being an
opt-in allowlist: it AST-scans **the whole of `app/`** for a local-timezone
"today", and separately fails on an inlined `datetime.now(UTC).date()` outside
`utils/dates.py`. The allowlist was the right shape while the tree was mixed
and the wrong one once it wasn't — a list cannot see a module nobody added to
it, and a new module is where the next `date.today()` arrives.

### Tinted badges — the primitive owns the recipe; what's left is deliberate keeps

The contrast half of this entry is long closed (the 29 badges below 4.5:1, fixed
via tint-paired text tokens — [decisions.md](decisions.md) §30). Round 12 closed
the **ownership** half: `frontend/src/lib/components/ui/Badge.svelte` is now the
single owner of the tinted-badge recipe. A caller names a *tone* and cannot spell
it wrong; `variant` passes the caller's semantic class through as a **selector
hook only** (the e2e suite reads `.badge.approved`), never as colour. Rationale,
including why sizing is fixed rather than a prop and why `neutral` / `erp` stay
non-tinted: [decisions.md](decisions.md) §47.

**The tranches are done — CLOSED (round 20).** `/discounts` (4),
`/tax` (3), `/vendors` (3) and `/vendor-statements` (1) went to zero, each its
own attributable tranche with the baseline edited down in the same commit. The
two shared-status pages hoisted a `STATUS_TONES` map beside the existing
`STATUS_LABELS` (`types/discounts.ts`, `types/vendor.ts`) so a list page and its
detail modal cannot disagree — the convention rounds 18–19 established.

`/invoices`' `.priors-badge` was the one conversion **refused**, and it moved
above the divider as a deliberate keep. It is not a status: it is an extraction
provenance annotation (`RAG·2·cache·3`, `cursor: help`) rendered *inside the
vendor cell beside the vendor name*, in a fixed-width ellipsised column. The
primitive's metrics would crowd out the name it annotates — the `UsersPanel`
`.you-badge` case verbatim, already an accepted keep for that reason — and it
already takes the `--accent-tint` / `--accent-on-tint` pair, so its colour
cannot drift.

Two things follow, and both are now enforced rather than remembered. The
`--- Still to convert ---` divider is **gone**: every remaining baseline entry
is a keep carrying its own reason, so a new non-zero entry is a keep that must
argue for itself, not a tranche waiting to land. And the audit's own self-check
(`it('detects the recipe it is meant to detect')`) pointed at `/vendors`, which
this round took to zero — it would have begun passing vacuously, so it now
names `ScreeningBadge`, a *permanent* keep that cannot be invalidated by the
next conversion.

### Surfaced by the round-20 parallel sweep — CLOSED (rounds 21-22)

All three entries here said a white-label vanity domain did not really work.
Two are now closed and the third is narrowed to one remaining piece.

**The SPA resolves a vanity host — CLOSED.** The diagnosis was right and the
proposed *cheap half* (validate the first label against the tenant slug, so the
panel refuses the broken shape) was **rejected in favour of the real fix**: it
would have made the panel honest while permanently narrowing the product to
`<slug>.<customer-domain>`, which is not what a customer buys a vanity domain
for. Instead the SPA classifies the hostname against an operator-declared
`PUBLIC_PLATFORM_DOMAINS` (`frontend/src/lib/hostRouting.ts`) and sends **no**
`X-Tenant-Slug` on anything that is not a platform host, which is what finally
reaches the backend `Host` fallback that has existed all along. The API origin
resolves at runtime and collapses to same-origin `/api` on a vanity host,
because a request to the build-time API origin carries the *platform's* `Host`
and defeats the lookup either way. Both layouts now gate on `hasTenantContext()`
instead of the slug — without that they rendered the marketing landing page to a
customer on their own domain, so the rest would have been invisible.
Unset config replays the old rule byte-for-byte, so no existing build changes on
upgrade ([decisions.md](decisions.md) §86).

**Per-tenant outbound links — CLOSED.** `settings.brand.tenant_url_template`
overrides the global, resolved by one `app/utils/tenant_urls.py` that all
**ten** call sites read — not the six this entry claimed; `services/supplier_chat`
and `services/card_issuance` both did their own substitution and did not look
like template call sites from the outside. An unresolvable base is now a real
answer: callers omit the link rather than fabricate a `localhost` URL into a
customer's inbox ([decisions.md](decisions.md) §91).

**Per-tenant passkeys — CLOSED.** RP ID and origins resolve from the tenant's
own registered custom domains, from the org that owns *the account*, never from
a `Host`-driven lookup — so a forged host, an unknown host and another tenant's
vanity domain all fail closed to the platform RP. The migration story this entry
said "needs designing, not just a config field" was designed and shipped:
`webauthn_credentials.rp_id` (migration 0091), `usable_here` on the list
endpoint, and a named cross-host message so a credential registered elsewhere
reports itself instead of failing opaquely ([decisions.md](decisions.md) §87).

**SSO on a vanity host — CLOSED (round 22).** The last piece. `slug` is now
optional on the four SSO/SAML entry points, falling back to the existing
`resolve_tenant_slug_by_custom_domain`; an unresolvable `Host` reuses the
**existing** 404 verbatim, so no enumeration surface was added (a test asserts
the two exceptions are equal, not merely both 404s). The callback base URL is a
**separate** opt-in field from `tenant_url_template`, because it is registered
at the customer's IdP and folding the two would mean fixing invite links
silently breaks SSO ([decisions.md](decisions.md) §92); the runbook carries the
ordered re-registration. Closing it surfaced a defect the entry had not
predicted: a routine branding save would have silently wiped that callback,
since it IS a `BrandConfig` field and `model_dump()` emits `""` for an omitted
one. `BUILD_TIME_API_URL_BASELINE` is now empty — the ratchet shrank to zero as
designed, and roadmap Priority 13 moved to the archive.

- [x] **DONE (round 28).** The entry named two blockers; there were three.
      `PUBLIC_PLATFORM_DOMAINS` now reaches both run modes from one module, so the
      local dev server and the CI production build cannot disagree — and unset
      **fails loudly** (the legacy rule reads `127.0.0.1` as a 4-label platform
      host with slug `127`) rather than asserting the inverse, which was the
      entry's stated reason for not writing the spec.
      The third blocker: the vanity origin has to be an **IP literal**, because
      every hostname the harness can reach is `*.localhost` and `localhost` is
      itself the declared platform domain, so no `.localhost` name can ever
      classify as vanity. A literal only connects to the address the server
      *bound*, and Vite's default binds the `localhost` name — which resolves to
      `::1` here despite `/etc/hosts` order — so the harness pins
      `--host 127.0.0.1`. `vite.config.ts` also proxies `/api` same-origin with
      `changeOrigin: false`, the operator requirement `white-label.md` already
      stated. Verified by the integrator: both tests in
      `tests-e2e/tenant/vanity-host.spec.ts` pass. See
      [decisions.md](decisions.md) §139.

### Surfaced by the round-19 parallel sweep (2026-09-05)

**Indexed — CLOSED (round 20).** Migration `0090_invoice_budget_dim_idx`,
gated on the `invoices` table existing (the shape 0044 and 0088 use) so it
no-ops on the control plane and fans out via `migrate_all_tenants.py`; the model
declares `index=True` so `create_all`-provisioned tenants match, and the names
follow SQLAlchemy's default so the two provisioning paths cannot diverge.

Measured before landing, as this entry demanded. Median of 7 warm runs against
the SQL `budget_service._actual_invoice_legs` actually emits, on a scratch
tenant with **independently randomised** dimension and status — a first
generator keyed both off `i % N`, correlating them into a misleading zero-row
case, and was discarded. `department` is the control and must not move:

| Invoices | `cost_center` | `gl_account` | `department` (control) |
|---|---|---|---|
| 40 000 | 7.7 ms → **1.8 ms** | 6.5 ms → **1.5 ms** | 2.7 ms → 2.7 ms |
| 200 000 | 15.9 ms → **7.4 ms** | 14.9 ms → **6.7 ms** | 9.6 ms → 9.6 ms |

Seq Scan → Bitmap Index Scan; buffers 1003 → 578 at 40k, which is the durable
number — seq-scan cost grows with the *table*, index-scan cost with the
*matching subset*.

**What it does not fix, recorded so nobody re-measures it hoping:** the
whole-tenant rollup over ~half the distinct cost centers is 10.3 ms → 9.1 ms —
inside noise, and slightly *more* buffers. At that selectivity a seq scan is the
right plan. This is a fix for the *selective* path — `GET /budgets/{id}/spend`
and `GET /budgets/check`, the latter running before every requisition submit.

The guard test walks every `BudgetDimension` through `_DIMENSION_MATCH_COLUMN`
to an indexed column, so a fifth dimension added on an unindexed one fails
rather than silently reintroducing the asymmetry.

**The operator can now assert the cutover — CLOSED (round 20).**
`backend/scripts/backfill_import_provenance.py` is exactly the tool this entry
specified and nothing more: `--cutover` is required, has no default, and is the
only thing that sets the boundary. Nothing is inferred from the data — that was
the whole point ([decisions.md](decisions.md) §81).

Safety properties, each with a test: dry run is the **default** (`--apply` is
the only mutating switch), one **named** tenant (there is no all-tenants mode),
the bound is strictly `created_at < cutover`, a **future** cutover is refused (a
migration that has not run produced no history, and that date would stamp live
invoices), and candidates come through `csv_import.native_invoice_clause()`
rather than a restated predicate, so an already-marked row is excluded in SQL
and a re-run can neither double-stamp nor overwrite a real `csv_import` marker.
A stamping run appends one PII-free `invoice.import_provenance_backfilled` audit
row; a dry run writes nothing. The marker records that it was *asserted* rather
than observed (`source=operator_backfill`, `asserted=true`), so a later reader
can tell a declared provenance from a recorded one.

**The smoke run changed the design, and it is worth knowing why.** A date bound
alone over-captures: on the dev tenant a cutover of today proposed 90 rows, 58
of them in statuses `csv_import` provably cannot land. Stamping those would have
*deleted genuinely native invoices from the metric* — the exact failure the
no-backfill rule existed to prevent, arriving through the front door. So the
tool restricts to importable statuses (imported from `csv_import`,
drift-guarded) and reports the rest as skipped. That is not the rejected
identify-by-status inference: it only ever refuses to mark, and a refusal leaves
the row reading exactly as it does today.

The caveat is in `backend/docs/analytics.md` in full: a wrong date mis-stamps in
either direction, neither direction is detectable from the data, and the tool
does not reverse it.

### Surfaced by the round-18 parallel sweep (2026-09-05)

Four items the round-18 agents traced to a file and line but correctly did not
fold into their own slice. None is a defect that can bite today.

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

### Surfaced by the persona-panel round-2 parallel fix batch (issue #328)

- **`pnpm i` dropping the frontend's security-pin overrides — SUPERSEDED.**
  This entry described pnpm 11 no longer reading `pnpm.overrides` from
  `frontend/package.json` (where `cookie@<0.7.0` and `undici@<7.28.0` are
  pinned), so a plain `pnpm i` regenerated the lockfile with the CVE pins
  silently gone. Its proposed fix was a `frontend/pnpm-workspace.yaml`.
  **A different fix landed** in #353: both `package.json` files now pin
  `packageManager: pnpm@10.12.4` and every `pnpm/action-setup` site reads it
  instead of passing `version:`, so one pnpm — a 10.x that does read that
  location — writes the lockfile everywhere. The pin is the thing to preserve;
  moving the overrides is only needed if the project later moves to pnpm 11+.
  What remains open is the *verification*, tracked below as
  § Surfaced while clearing the open-PR backlog → "Confirm the `packageManager`
  pin stopped Dependabot dropping the pnpm overrides", which is where the recipe
  and the recurrence instructions live. Kept as a pointer rather than deleted:
  the diagnosis (which pnpm versions read which location) is the expensive half.

### Surfaced by the issue #328 checklist reconciliation (2026-08-27)

Going through all 56 persona-panel findings + 8 acknowledged gaps against `main`
after PRs #329, #330 and #341 landed left **13 findings genuinely open** plus
**8 product-fit gaps awaiting a keep-or-drop call**. They are parked here so
issue #328 can close — the checklist itself is not a destination (guard rail 6).
Every one is category **(c)**: a sized-but-unstarted piece of work, or a
deferred-with-reason finding awaiting a product/architecture call.

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

- [x] **UI to create a vendor + invite one to the supplier portal — DONE
      (PR #343).** `+ New Vendor` header action (`vendor.manage`-gated) opens
      `CreateVendorModal` (`POST /api/vendors`; no bank field — the backend
      dual-control-stages that on create); an `Invite` row action
      (`auth.isManager`) opens `InviteVendorPortalUserModal`
      (`POST /api/vendors/{id}/portal-users`) whose one-time temp password is
      shown via the shared `SecretReveal`. Guard:
      `tests-e2e/vendors/create-invite.spec.ts`.

- [x] **No onboarding empty-state / CTA for a zero-data tenant — DONE
      (PR #343).** `ui/EmptyState.svelte` (icon + heading + description +
      optional button/link action, i18n-agnostic) is adopted on the dashboard
      (zero invoices → `/invoices`), `/invoices` (zero rows + no filter → the
      upload action, role-gated), `/portal/invoices` (vendor submitted nothing
      → the submit action), and `/vendors` (zero vendors + no filter → the
      `+ New Vendor` action, `vendor.manage`-gated — gated on a first-fetch
      `loaded` flag so it doesn't flash during load, and on `!loadErrored`).
      Each page keeps its `DataTable` + loading/errored/filtered-empty copy
      for every other state. Guards:
      `tests-e2e/reactivity/empty-state.spec.ts` (`/invoices` + `/vendors`).

- [x] **[Low] Organization/Settings first-time-admin prioritization — DONE
      (PR #343).** A "Getting started" wayfinding strip at the top of
      `/organization` links a new admin to the five sections they configure
      first (Company Profile, Invoice Defaults, Users & Roles, approval
      thresholds, Branding). Sections are neither reordered nor hidden — it's a
      shortcut strip with anchor `id=`s. Guard:
      `tests-e2e/organization/getting-started.spec.ts`.

- [ ] **[Low] The marketing pricing page (`Pricing.svelte`) is USD-only.**
      Hardcoded `$` figures; no currency awareness.
      **Durable fix:** a product call on whether to localise pricing at all, then
      per-locale figures if yes.
      **Trigger:** an international pricing decision.

**Supplier portal — the loop-closing steps are missing:**

- [x] **A vendor can see why an invoice is *stuck* — DONE (PR #343).** The
      *rejected* half (`rejection_reason`) shipped earlier in the PR;
      `GET /portal/invoices[/{id}]` now also carries `waiting_on` — a PII-free
      bucket (`review` / `processing` / `erp`) plus `waiting_on_days`, set
      **only** while the invoice is in a processing phase, NULL for
      `new`/`approved`/`paid`/`rejected`/`done`. Never an internal status
      string or a user name. Rendered as a localized line under the status
      pill ("Awaiting your customer's review · 5 days"). Guard:
      `tests/test_portal_waiting_on.py`. (A finer step-level detail off the
      workflow instance was scoped down to the phase-bucket + age — enough to
      add information beyond the chip without touching workflow internals.)

**Volume surfaces:**

- [x] **`GET /api/payments/queue` pagination — DONE (PR #343).** `?page=` /
      `?page_size=` on `GET /queue` (order `due_date ASC NULLS LAST, id ASC` —
      the `id` tie-breaker the invoice list has), plus a `GET /queue/ids`
      resolver for the whole selectable set (capped, currency-bucketed). The
      Queue tab renders Load-More; "select all N matching" resolves via
      `/queue/ids` (not the loaded rows) and the pay-bar count / per-currency
      subtotals / mixed-currency guard derive from the whole-set rollup.
      Guards: `tests/test_payment_queue_pagination.py`,
      `tests-e2e/payments/queue-pagination.spec.ts`.

- [x] **URL filter/search persistence on `/invoices`, `/payments`, `/vendors`
      — DONE (PR #343).** Each page now initialises `search` + the status chip
      from the query string and folds them into its `syncUrl()` writer
      (untracked, called from the filter effect + the debounce timer);
      `/payments` also persists the active tab. Guard:
      `tests-e2e/reactivity/filter-url-persistence.spec.ts`; the debounce it
      sits next to stays covered by `search-debounce-race.spec.ts`.

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

- [x] **GBP→GB domestic payment rails — DONE (PR #343).** `bacs`,
      `faster_payments`, `chaps` added to the `PaymentMethod` enum, classified
      on both `payment_methods.py` axes (IRS-reportable + `DOMESTIC`), with fee
      anchors as `Decimal`. `pick_corridor` gets a GBP/GB branch:
      `not requires_fx and target_currency == "GBP" and country in (None, "GB")`
      → Faster Payments (no SWIFT, no FX lock, no IBAN — UK domestic uses sort
      code + account number even though `is_sepa_country("GB")` is true).
      Cross-currency into GBP still routes `international_wire`. No migration
      (`Payment.method` is a `String`); only the `mock` adapter gained the
      rails. Guards: `tests/test_payment_corridor_uk_domestic.py`, extended
      `test_payment_methods.py`.
      **Still open (separate finding, above):** the per-line VAT tax model —
      that's the architecture item, unrelated to the payment rail.

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
### Surfaced while clearing the open-PR backlog (2026-09-02)

- [ ] **(b) The Dependabot pip-grouping `patterns` fix is REFUTED; a second
      candidate is now under test (round 28).** The entry's own refute condition
      was met. `patterns: ["*"]` went on the `backend-minor-patch` group on
      2026-09-05; the next scheduled run, Monday 2026-09-07, still delivered
      #379 (`ruff`) and #381 (`boto3`) on separate
      `dependabot/pip/backend/<dep>-gte-…` branches. The entry had already
      suspected this — the `npm` group has no `patterns` and groups anyway.
      **What the refutation exposed:** the operative difference is
      `update-types` on an ecosystem where Dependabot can see no lockfile. Our
      locks are `requirements.lock` / `requirements-dev.lock`, and pip-compile
      support only recognises a lockfile whose name ends in `.txt` and matches
      an `.in` basename — which is exactly why the `fake-erp` group, on
      `requirements.in` + `requirements.txt`, does group. For `/backend`
      Dependabot therefore reads only `pyproject.toml`'s ranges, resolves no
      concrete version, computes no semver update type, and every member falls
      out of a group filtered by one. The branch names corroborate it:
      `boto3-gte-1.43.88-and-lt-2` is a requirement-range edit, not a version
      bump.
      **Applied this round:** `update-types` dropped from `backend-minor-patch`,
      leaving `patterns: ["*"]` alone — structurally identical to the two
      groups that demonstrably work. The accepted cost is that a major can now
      ride in the same PR as patches, which is the lesser evil while every bump
      arrives alone and each one costs a hand recompile of the locks.
      **Confirmed if** the next Monday run delivers the pip bumps on one
      `dependabot/pip/backend/backend-minor-patch-…` branch; **refuted if** they
      again arrive separately — in which case the remaining lead is renaming the
      locks to the `.in`/`.txt` pair pip-compile support recognises.
      **Do NOT** copy this to `terraform-minor-patch`: it has a real
      `.terraform.lock.hcl`, its `update-types` resolves, and it groups today
      (#332, #345, #380).
      **Trigger:** next Monday's Dependabot run.

- [x] **(b) CONFIRMED (round 28) — the `packageManager` pin held.** PR #378
      (`vitest` 4.1.11 → 5.0.0, opened Monday 2026-09-07, the first Dependabot
      npm PR after the pin) rewrote `frontend/pnpm-lock.yaml` and left the
      `overrides:` block untouched — zero `overrides` lines in its diff — and
      merged green. The block is present in both `frontend/package.json` and
      `frontend/pnpm-lock.yaml` on `main` today. The recurrence recipe below
      stays recorded because it is the remedy if it ever returns.
      Two npm PRs in one day (#344, #351) had arrived with the whole
      `overrides:` block deleted from `frontend/pnpm-lock.yaml` while
      `package.json` still declared `pnpm.overrides` for `cookie@<0.7.0` and
      `undici@<7.28.0`, red on every job that installs. Root cause was that
      nothing declared which pnpm writes that lockfile, so four wrote it (CI on
      9, `audit.yml` on 10, contributors on whatever, Dependabot on its own
      default). Both package.json files now pin `pnpm@10.12.4` and every
      `pnpm/action-setup` site reads it instead of passing `version:`.
      The divergence is fixed and verified. Whether it also fixes Dependabot is
      a **hypothesis** — it cannot be tested without waiting for the next npm
      PR.
      **If it recurs:** regenerate by hand (`pnpm install --lockfile-only`, then
      confirm the block survived), exactly as § (b) describes for the pip locks.
      Do NOT reach for `--no-frozen-lockfile`: those are conditional floor
      guards against a future transitive downgrade, inert today
      (`cookie@0.7.2`, `undici@8.10.0` both already clear them), which is
      precisely what makes losing one easy to miss.
      **Trigger:** the next Dependabot npm PR. Recipe in
      [frontend/CLAUDE.md](../frontend/CLAUDE.md) § The lockfile.

### Surfaced by the round-21 parallel sweeps — mostly CLOSED (round 22)

The round-21 sweeps found more verified work than that round's agent budget
could land, and it was recorded here rather than dropped. Round 22 spent ten
agents on it. **Closed:** the email-intake panel + token rotation, `/admin/entities`,
the `/api/adaptive` router's UI, the `/api/inspections` UI, the
approval-signature verification panel, `/health/sweeps`, the card-rebate
lifecycle, mobile CFO run approval, the `approval_chain` ownership + drift guard,
the PEPPOL transmission read path, and the e-invoice structured error contract.

Three of those turned up something the entry had not predicted, recorded in
[decisions.md](decisions.md): the two `approval_levels` spellings were not a
style difference but a latent `AttributeError` on the approval path (§93); a
routine branding save would have silently wiped an IdP-registered SSO callback
(§92); and `GET /organization/email-intake` had to perform a write to establish
a read-only fact (§94).

What remains from those sweeps:

- [x] **DONE (round 28) — and this empties the list.** The caller is the
      `/discounts` **Propose vendor offer** modal, gated `admin`/`ap_manager` to
      match `_WRITE_ROLES`, which is *narrower* than the page's accept/decline
      gate, so a CFO reads the page and sees no trigger.
      **The entry mis-described the endpoint**, and the wrong reading suggested
      the wrong UI: "bulk" is the **base**, not the batch. It returns one offer
      whose base is a single vendor's summed open balance, so there was no
      skip-and-report result to render and the shared bulk-selection toolbar would
      have been the wrong shape entirely. The base is server-computed and
      deliberately not previewed ([decisions.md](decisions.md) §140).
      Three defects on the endpoint were closed in the same change: a malformed
      `vendor_id` was a 500 rather than a 422; a misspelled `valid_until` was
      silently dropped, creating an offer the optimizer can never rank while it
      stands against the vendor's whole open balance; and the vendor lookup was
      not entity-scoped while the invoice sum beside it was.

### Surfaced by the round-22 parallel round (2026-09-05)

Found while closing the above. None is a defect that can bite today.

**Closed in round 23:** `RebateResponse` now resolves each row's `currency`
from its joined card, and `GET /api/cards/rebates` / `GET /api/inspections`
are both on the canonical `page` / `page_size` contract — `/inspections` also
returns `gr_number` and takes a `?gr_id=` filter, so the UI no longer fetches
a 100-row page of receipts purely to label a column.

- [x] **DONE (round 28).** The entry was half stale — `PaymentResponse` had
      carried `void_card_outcome` since an earlier round. What was true is that
      **nothing rendered it** (the void handler discarded the response) and no
      remedy existed. Both are closed: the response now also carries the verdict
      `void_card_disposition`, and `POST /payments/{id}/void/retry-card-cancel`
      re-attempts only the card leg, gated on `payment.void` (the permission of
      the void it completes, not the card router's bare roles), 409ing on anything
      but an already-`voided` card payment so it can only ever *finish* a
      reversal. §96's objection was about the state a control is reachable in, and
      it evaporates once the payment is voided.
      **A live defect surfaced on the way:** `_cancel_card_for_void` selected the
      card with an unordered `LIMIT 1` over `payment_id`, which is not unique
      (cancel-then-reissue leaves the dead row), so Postgres could return the
      cancelled row and report success while the live, bearer-spendable card
      stayed open — the precise failure the function exists to prevent. Now
      ordered live-first and taken `FOR UPDATE`. See
      [decisions.md](decisions.md) §132, and §128 for the `LIMIT 1` rule.

- [x] **DONE (round 28).** The nav is now aligned to the backend **per entry**
      rather than per group. The entry understated it: the group already gated per
      child, and `/purchase-orders` excluded the clerk too — both widened, each
      citing its own route gate; `/budgets` correctly keeps excluding them.
      **A worse bug turned up in the other direction: the nav was too WIDE.**
      `nav.ts` ORs `payment.execute`/`payment.void` into the Payments row on a
      comment claiming every call the page makes would succeed. It would not —
      `/payments/summary`, `/payments/queue` and `/queue/ids` still gated on
      `require_roles`, so exactly the split-duty role the clause exists to serve
      got three 403s on first paint. All three moved to `require_permission`,
      reproducing the four-system-role matrix exactly. Guarded by
      `frontend/src/lib/nav.test.ts` and `backend/tests/test_sod_endpoint_wiring.py`.

- [x] **DONE (round 28).** The load-bearing word in the durable fix was
      *generated*, and a hand-written map is exactly what §95 threw away — so the
      deliverable is the **guard**, not the catalogue. `rule_catalog.py` scans the
      validators' own source for every emittable code (a scan, not a registry: a
      registry can be added to and not used), `gen_einvoice_rule_messages.py`
      writes the frontend map, and `--check` runs in CI's backend-lint job.
      Three guards chain — regenerate-and-diff catches a new code, `satisfies`
      catches a key `en.ts` lacks, and locale parity catches the other five — and
      the chain was walked end to end by adding a fake rule and watching each
      link fire. Refusals render through a shared `EInvoiceIssueList`, with an
      unknown code degrading to the raw server sentence rather than a blank row.
      See [decisions.md](decisions.md) §138.

- [x] **DONE (round 30).** Both surfaces now exist on mobile, and the work was
      mostly deciding which of their controls do NOT travel. **Inspections ships
      whole**, write included — list with a **server-side** outcome filter
      (`?result=`, added to `GET /api/inspections`; filtering the loaded page
      would hide every matching row past the page boundary and leave `total`
      describing a different set), a detail view that states what the outcome
      does to the 4-way match, and a record form. The form requires a goods
      receipt even though the API does not: `po_matching` reads an inspection
      only through a receipt or a PO-level row, so an unlinked one would let
      someone record a failure, see it listed, and watch the invoice pay anyway.
      QMS sync stays on the web — it is an operator action against
      `settings.qms` that 409s without it. **Adaptive ships read-first**, which
      is a finished state: suggestions / patterns / anomalies plus the one safe
      write (dismiss), with **both apply paths deliberately excluded** — they
      reassign a live approval and raise the org-wide auto-approve threshold,
      and the latter's stale-value 409 needs a surface that names both figures
      to land safely. The Feedback tab is out for a different reason
      (`GET /feedback` writes an access-audit row, so it needs its own
      "only when asked" treatment).
      **The doc imprecision the entry was filed for is fixed at the source:**
      `backend/docs/adaptive-workflows.md`'s "No mobile (Flutter) surface yet"
      is now a Mobile section that also records the exclusions, and
      `backend/docs/po-matching.md` carries the mobile inspection surface.
      `frontend/CLAUDE.md`'s parity list — which still claimed vendors,
      exceptions, workflows, org settings, admin, the payment queue, invoice
      editing, bulk ops and the audit timeline were web-only — was replaced by a
      pointer to `mobile/docs/feature-status.md` plus the parity *direction*.
      The Settings hub is now gated **per entry** (round 28's correction to the
      web nav, applied here): Procurement → Quality Inspections is open to every
      role because the reads are `get_current_user`, and Administration renders
      whenever any child does. See [decisions.md](decisions.md) §156.

### Surfaced by the round-23 hunt (2026-09-06)

Round 23 ran six read-only hunters alongside four fix agents. The tenant-isolation
and authz audit came back **clean** across every surface rounds 20-22 added
(including the `Host`-resolved SSO fallback — a forged `Host` can only select a
tenant that registered it, and the state/nonce is minted against the
server-resolved slug). The other five found substantially more than the round
could land. Everything below was verified at the code level; nothing here is a
suspicion.

**Fixed in round 23** and not repeated below: audit rows on the three
destructive deletes that had none (invoice hard-delete + its cascade, portal
credential revocation, workflow-definition delete taking its version history);
the `/admin/partner` cross-tenant branding race; the `/audit` CSV export that
exported the live form rather than the query on screen; and nine documentation
statements that contradicted the code.

#### ⚠️ Segregation-of-duties — HELD FOR SECURITY REVIEW

Both are SOC 2 CC6.3 controls. Changing who may approve what is a control-design
decision, not a bug fix, so an item here is recorded rather than patched. **Loop
in the CISO / Security Analyst before acting.**

**Both original entries were closed in round 28** under the repo owner's explicit
authorisation, recorded on each. The one item opened in their place — exception
resolution — is now closed on the same terms, so **nothing in this section is
outstanding**; a new entry here is still held for review rather than patched.
One *non*-control gap surfaced while closing it and is tracked below, outside
this section's gate, because it is a localisation defect rather than a
control-design question.

- [x] **DONE, under the repo owner's explicit authorisation** — which is what
      satisfies this section's standing "loop in the CISO" gate, and it covers
      this entry only. **The entry's premise was checked first and does not hold
      on its own, so the prescribed fix was not the fix.** `create_exception` is
      the only constructor of an `Exception` row, so the call sites enumerate
      exhaustively: **eleven, and at exactly one is the signed-in actor the
      person the flag exists to ask about.** Five have no user at all (the
      extraction worker ×2, the ERP sync-back, the reconciler sweep, the
      settlement-mismatch check, the inbound ERP webhook); three more run from a
      door that *does* have a user who did not cause the finding —
      `invoice_warnings._ensure_exception` raises nine types and is reached from
      fifteen callers including two sweeps and two agent resolvers, so a
      `duplicate` would have been pinned on whoever next PATCHed the invoice
      rather than on whoever created the duplicate; Positive Pay records what the
      *bank* said, imported by an operator who did not alter the cheque; the
      compliance hold comes from a screening verdict, twice from unattended retry
      paths. `review._reject` has the actor and is still wrong to stamp. So a
      raiser-only rule would have been near-inert, and the way to make it look
      busy — stamp whoever was signed in — manufactures a refusal against a
      bystander and an absolution for whoever really caused the flag: §141/§152's
      error committed forward instead of backward.
      **The gap was an asymmetry, not a missing column.** A flag is a detector's
      finding about an invoice's *contents*, so the motive to clear it belongs to
      whoever created or shaped that invoice — and `violates_segregation` already
      refused that same set (`uploaded_by_id` ∪ `segregation_actor_ids`, §152)
      the **approval** of the same invoice, while leaving them the `fraud_flag`
      standing between it and a payment run.
      `exception_lifecycle.segregation_refusal` now reads that set through the
      same predicate, so the queue and the approval path cannot drift, **plus**
      `exceptions.raised_by_user_id` (migration `0098`, tenant DBs, no backfill)
      — which ships because the invoice-side axis cannot reach the one case
      [authentication.md](authentication.md) had already written down as open:
      the bank-change approver is not the uploader of the invoices their approval
      re-points, so the compensating `fraud_flag` was "not a second control
      against the same actor". That link is now closed.
      Scoped to `is_payment_blocking` (the payment-run gate's own tuple, so a new
      blocking type is covered for free) and to `resolve`/`dismiss` — `escalate`
      stays open because an escalated row still blocks the run and is the refused
      actor's exit. Enforced in `record_decision`, the one chokepoint all three
      doors share; `/bulk/resolve` pre-checks so a refusal is a per-**row**
      `skipped` reason rather than a 409 for the batch. **Agents inherit rather
      than bypass**: the coordinator escalates with a recorded `AgentDecision`,
      because `actor_id` is the triggering human and an exemption would be a
      laundering route to what the HTTP door refuses. Default-ON with
      `settings.exceptions.require_segregation: false` as the explicit per-org
      opt-out — the entry's own "small AP team" objection, answered the way
      `require_run_segregation` answers it. A refusal is logged, not audited
      (it changes no state, the sibling control audits none, and `audit_log` is
      WORM + append-only); the successful row plus the invoice's columns let an
      auditor re-derive the outcome of every historical decision, which is
      stronger evidence than a refusal row.
      `tests/test_exception_raiser_stamping.py` is the enforcement that keeps the
      column honest — every `create_exception` site must pass the kwarg, and a
      literal `None` must be declared with its reason. See
      [decisions.md](decisions.md) §169 (the subject inversion) and §170 (why the
      agent inherits).

- [x] **DONE (round 28), under the repo owner's explicit authorisation** — which
      is what satisfies this section's standing "loop in the CISO" gate, and it
      covers this entry only. Every link of the chain was re-verified and held.
      **The entry named only the invite route**, and that omission mattered:
      `POST .../portal-users/{id}/reset-password` is the same hole against a
      supplier who already exists — same role gate, also returned the plaintext
      password, and sent no email at all. Both are closed, and closing both is
      what makes the design sound, because invite and reset are *exhaustively*
      the only writers of `VendorUser.hashed_password` outside the supplier's own
      change-password — which is why a NULL provisioner can safely stay
      permissive.
      `vendor_users.provisioned_by_user_id` records who minted a credential;
      `vendor_change_requests.requester_provisioned_by_user_id` **freezes** it at
      staging rather than joining at approval, because deleting the portal user
      carries no FK into the change-request table and a join would let the
      approver delete the identity and walk their own request through. The stamp
      deliberately survives the supplier's own password change, since that route
      requires the current password the provisioner holds. `temp_password` left
      both responses and is emailed only, with a send failure unwinding the
      transaction — defence in depth, not the fix. Migration `0095`, additive and
      un-backfilled. `tests/test_vendor_credential_provenance.py` guards the
      exhaustiveness claim the design rests on, so a future "resend credentials"
      route fails until it stamps or earns an exemption. See
      [decisions.md](decisions.md) §130.

- [x] **DONE (round 28), under the repo owner's explicit authorisation.** The
      fix was **one keyword argument**, not the thread-through the entry
      described: `import-csv` already had the user and already spent it on the
      `invoice.imported_csv` audit row; `csv_import`'s `Invoice(...)` constructor
      simply never passed it. Proven empirically rather than by reading — with the
      stamp removed the importer's own approve returned 200/`approved`; with it,
      403.
      **The entry's parenthetical was wrong.** It said `recurring_invoices` and
      `intercompany` had "no creator column to fix with"; both already carried an
      `actor_id` in their signatures, spent on audit rows and nowhere else, and
      both now stamp. The two portal paths stamp `None` **explicitly with a
      reason**, which turns an omission into a declared exemption.
      `violates_segregation` deliberately stays fail-open on NULL — failing closed
      would make every email-intake, PEPPOL, portal-submitted and sweep-generated
      invoice permanently unapprovable, an outage across four non-interactive
      front doors. Instead the branch's **premise** is enforced:
      `tests/test_invoice_uploader_stamping.py` walks the syntax tree of every
      `Invoice(...)` under `app/` and requires the kwarg, or a literal `None`
      declared with a reason. See [decisions.md](decisions.md) §131.

#### Money path — verified, unfixed

- [x] **DONE (PR #374).** `/payments/runs/{id}/execute` and `/resume` skipped
      two of the four gates run creation applies. Creation checks payable
      status, financial integrity (`blocking_exception_types`), credit-memo
      netting and the live card claim; the dispatch leg (`_execute_single_payment`)
      re-checked payable status + credit-memo netting but NOT the blocking
      exception or the card claim. So a `fraud_flag` raised **after** a draft run
      exists — exactly what an approved vendor bank change raises ("Vendor bank
      details changed; verify before payment"), while a draft run sits for days
      awaiting CFO sign-off and `_execute_single_payment` re-reads
      `Vendor.bank_details` — did not stop execution; a virtual card minted after
      the run was built was the second variant. `_execute_single_payment` now
      runs `blocking_exception_types` + `card_claimed_invoice_ids` (the SAME
      shared predicates the run builder and `/retry-failed` use) right after the
      payable-status re-check, refusing BEFORE the adapter call with named
      retry-safe reasons `invoice_blocked:<type>` / `invoice_has_live_card`. Also
      covers `/compliance/release`, which flows through the same chokepoint.
      Dispatch-time tests added to `test_payment_run_blocking_exceptions.py` +
      `test_payment_run_live_card_claim.py`.

- [x] **DONE (PR #375).** The ERP webhook was a writer of
      `payment_scheduled → paid` that bypassed the settlement-coverage hold:
      `api/erp_webhook.py` transitioned on `VALID_TRANSITIONS` alone and never
      read `settled_amount` / `settlement_coverage`, so a validly-signed
      `{"status":"Paid"}` from the tenant's own ERP flipped a short-settled
      invoice to `paid` (firing `payment.settled`, emailing the supplier,
      counting the full amount in aging / 1099 YTD) while `/settlement/accept`
      then 409'd — the documented exit gone. The webhook's
      `payment_scheduled → paid` branch now runs the SAME `settlement_coverage`
      check off the SAME persisted `Payment.settled_amount` and, on a
      non-covering verdict, opens an `erp_reconciliation` exception (dedup'd) and
      leaves the invoice at `payment_scheduled` — a silent 204, same two exits
      (`/settlement/accept` or `/void`). `backend/docs/payments.md` corrected
      ("primary code path", not "only").

- [x] **DONE (PR #373).** `derive_run_status` failed open — a run of voided
      payments reported `completed`. `voided` matched none of the four bucket
      tuples so it bumped `total` only and every branch fell through to
      `return "completed"` (with `payments_completed: 0` beside the full
      `total_amount`). Fixed both halves: `voided` joins
      `RUN_PAYMENT_FAILED_STATUSES` (it is a non-success terminal, grouped with
      `cancelled` in `LIVE_PAYMENT_TERMINAL_STATUSES`), and the final rung is now
      fail-closed — `"completed"` only when `completed == total`, otherwise
      `partial` / `failed` — so any future adapter status a bucket doesn't
      recognise can't report success either. Parametrized cases for both in
      `test_payment_run_status_derivation.py`.

- [x] **DONE (PR #377).** The payment queue offered — and counted as "ready to
      pay" — rows the run builder hard-409s. The three queue queries excluded
      only `Payment.status == "completed"`, so an invoice with a `submitted` /
      `processing` / `pending` / `pending_compliance` payment (every real rail —
      ACH settles in 1-3 days) was a selectable queue row that
      `create_payment_run_for_invoices` then 409'd on
      `uq_payments_one_live_per_invoice`, taking the whole select-all batch down
      with no way to bisect. The queue's `_queue_base_where()` now excludes any
      invoice with a **LIVE** payment via `_live_payment_invoice_ids()` — the
      SAME `Payment.status NOT IN LIVE_PAYMENT_TERMINAL_STATUSES` definition the
      run builder's `_live_payment_invoice_numbers` guard uses — applied to
      `/queue`, `/queue/ids`, the rollup KPIs and `/summary`'s `queue_count`. A
      terminal (`failed` / `voided`) payment still leaves the invoice offered
      (re-pay after a failure). New non-tautological drift guard compares the
      offered set against BOTH run-builder refusal predicates.

      *Round-26 correction:* this closed the live-payment case, but by
      special-casing one status rather than sourcing the verdict from the
      builder's predicate set — so two of the four refusals
      `create_payment_run_for_invoices` enforces (`fully_credited` and
      `live_virtual_card`) were still offered and still 409'd the whole batch.
      `payment_runs.run_refusal_reasons()` is now the one predicate set both the
      builder and the queue read, and a card claim **pins** the rail rather than
      blocking the row — see [decisions.md](decisions.md) §120, including why
      `blocked_total` and `selectable_total` are deliberately not complementary.

#### Performance — measured, unfixed

Numbers from a 200k-invoice / 1M-audit-row scratch database, medians of 5-7 warm
`EXPLAIN (ANALYZE, BUFFERS)` runs, dimensions randomised independently (the
round-20 lesson about correlated generators was applied).

#### Frontend — verified defects

- [x] **DONE (round 26).** **Four** specs, not three, and all four leaked
      **unboundedly** — every identifier carries `Date.now()`, so each run added
      rows. Measured on `e2e1` beforehand: 41 invoices of which 5 were stranded,
      32 vendors of which 4 were, and 2 orphan `line_total_mismatch` exceptions;
      `e2e3` also held 2 stranded entities. `create-manual` (2 invoices a run)
      and `line-total-reconciliation` (3 invoices plus a **payment-blocking**
      exception each) now go through `deleteInvoicesWhere`, which owns the child
      graph and is what removes the exception; `entities/switcher` (2 entities a
      run, and `/api/entities` has no DELETE, so nothing could ever remove them
      through the app) and `vendors/consolidation-merge` (2 vendors a run — the
      merge soft-retires the duplicate and keeps the canonical one *by design*,
      so even a clean run left both) go through `tenantPsql`. A fifth,
      `admin/api-keys`, was found while adding the usage e2e: 20 permanently
      revoked control-plane keys had accumulated. Every predicate is the spec's
      own marker **prefix**, not the ids that run created, so a run also clears
      what earlier runs stranded. After: stranded invoices 5 → 0, orphan
      exceptions 2 → 0, entities 2 → 0, consolidation vendors 4 → 0,
      control-plane keys 23 → 7 (the remaining 7 belong to the other three e2e
      orgs, each of which clears its own on its next run).
      **The vendors that manual invoice entry provisions are deliberately NOT
      deleted** — see [decisions.md](decisions.md) §122. They are a shared
      fixture, not a leak, and deleting them failed a foreign key, which is how
      that was found.
      *The `EntitySwitcher` half of this entry landed in round 25* — the switcher
      lists only active entities, and `get_write_entity_id` now refuses a write
      filed under a deactivated one ([decisions.md](decisions.md) §115).

### Surfaced by the round-24 batch (2026-09-06)

Eleven agents closed thirteen entries above and opened eleven. **Round 25 closed
all but two of those** — the six unaudited handlers, the unaudited MFA
enrollment, the three dashboard currency figures, the four folds on the event
loop, `RunDetailModal`'s USD, the spooled audit export, the Lambda URL builder,
the name-resolved SoD gate, the api-keys e2e, and the hardcoded e2e port. What
survives is below.

#### Consequences of round-24 changes

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

#### Guard and tooling remainders

- [x] **DONE (round 26).** The read-pattern review this was waiting on was done,
      measured on a 55 000-row scratch copy rather than reasoned: the composite
      serves both real call sites **better** than the narrow index did (both
      columns land in the `Index Cond` instead of filtering `bank_format`), serves
      a bare `payment_run_id = $1` (`=` is strict, so the partial predicate
      holds), and serves the FK's own `FOR KEY SHARE` probe. The one read it
      cannot serve is `payment_run_id IS NULL`, and no such query exists —
      run-less `ach_authorization` files are reached through `file_type`.
      Migration `0094` drops it and `PositivePayFile.payment_run_id` loses
      `index=True` in the same commit ([decisions.md](decisions.md) §104/§109);
      the parity guard's `EXEMPT` entry carries the reason plus a structural
      assertion that the composite still **leads** on that column, since the whole
      argument collapses if it stops. **Trigger if it ever returns:** adding a
      "list the run-less files" query.

### Surfaced by the round-25 batch (2026-09-08)

Ten agents closed fifteen entries. Every one of them returned something the entry
had not predicted, and where that finding was itself a defect it was fixed in the
same round rather than recorded — the entries below are only what genuinely could
not be closed. Two of them exist *because* of a round-25 change and say so.

Three predictions the entries got wrong, kept because the correction is the
useful part:

* `/invoices`' two URL writers were recorded as a race observed once. They were
  **deterministic**: SvelteKit's `replaceState` never updates `page.url`, so both
  writers rebuilt the query string from a URL frozen at the last real navigation
  and the second always dropped the first's params.
* The un-stripped `payments.home_currency` was recorded as mis-routing a domestic
  payment to `international_wire`. It made the payment **fail outright** — the
  wire corridor demands a SWIFT/BIC the domestic vendor has none of, so
  `prepare_international_payment` raised, while the KYC gate read the same
  setting through its own stripping helper and saw an ordinary domestic payment.
* The realized-FX gap on reconciler-recovered payments was recorded as missing
  rows. It was **permanent**: the webhook refuses an already-terminal payment, so
  a late webhook could never supply them.

- [x] **DONE (round 28).** All three closed, each on its own terms, and two were
      worse than "hardcoded English". `portalStatus.ts` took the redesign: a phase
      is now a stable snake_case id with a message key beside it and the
      status→phase assignment **written out** rather than inferred from matching
      label strings — under the old shape, translating a label was a *data*
      change that silently split one chip into four or merged two phases into one
      ([decisions.md](decisions.md) §137). Membership is pinned byte-for-byte
      against the backend's own status vocabulary, because no type can catch a
      status changing chips. `vendor.ts` became `SCREENING_CATEGORY_LABEL_KEYS`
      over a total `ScreeningCategory` union with a tolerant accessor.
      `positivePay.ts` took the whole `PositivePayModal` extraction (47 keys)
      rather than leaving a dialog with two localized strings and fifteen
      hardcoded ones — and keying it fixed **two surfaces the entry never named**,
      the list's Format column and the modal's detail pill, both of which printed
      the raw `fixed_width` one click from a picker reading "Fixed width".
      66 keys across all six locales, actually translated.
      **One user-visible change:** the portal deep link is now `?phase=rejected`
      rather than `?phase=Rejected`, so an old bookmarked link no longer selects
      that chip. A label-keyed URL could not survive a locale switch.

- [x] **DONE (round 26).** The payment-run status badge no longer renders the raw
      enum. A `RUN_STATUS_LABEL_KEYS` map sits beside the payment one, with
      `PaymentRunStatus` as a total union so a status with a tone but no label is
      a compile error, and the tolerant `runStatusLabelKey()` accessor degrades an
      unknown wire value to its own raw text. **Both** readers were fixed, not
      just the one the entry named: `/payments`' Runs table renders the same enum
      through the same map, one click from the modal, and `daily-journey.spec.ts`
      asserted the raw text on both. Fixing only the modal would have left a
      translated pill in the dialog and an untranslated one in the row it was
      opened from.

- [x] **DONE (round 26).** `DiscountDashboard.capture_rate_pct` is nullable with
      an `insufficient_data` marker, matching its `analytics` sibling exactly, and
      the `/discounts` card renders an em dash rather than `0%` for both the
      nothing-decided and the still-loading states — `0%` there is precisely the
      misreading [decisions.md](decisions.md) §34 exists to remove. Writing the
      test also exposed a pre-existing local-timezone flake in
      `test_discounts_api.py`, which anchored fixtures on `date.today()` while the
      API compares against `utc_today()`; fixed at the test clock and the module
      joined `UTC_TODAY_TEST_MODULES`. CI runs UTC and would never have shown it.

- [x] **DONE (round 26).** `currency_conversion.resolve_reporting_currency` no
      longer reads `settings.payments.home_currency` itself, and
      `international_payments.py` is now the only file under `app/` that does.
      **The recorded two-line fix would have been a regression** — see
      [decisions.md](decisions.md) §119. `resolve_home_currency` can never answer
      "unset", so dropping it in as rung 2 of a four-rung chain would have made
      rungs 3 and 4 unreachable and silently switched an org whose only currency
      signal is `invoice_defaults.currency` to USD. The fix hoists a primitive
      that *can* abstain, `configured_home_currency(...) -> str | None`.

- [x] **DONE (round 26).** `api/dashboard.py`'s discount-capture block groups in
      SQL. The entry judged the per-row classification inexpressible as a
      `GROUP BY`; it is two comparisons, so it groups by a `CASE` and three rows
      come back, with the bucket vocabulary and the rate staying in Python. The
      period bound was rejected as a silent redefinition of the figure. One number
      changed deliberately: the half-cent tie-break moved from half-even to
      away-from-zero, which aligns the tile with
      `discount_offers.discount_savings` for the identical quantity. See
      [decisions.md](decisions.md) §118, including why
      `AT TIME ZONE 'UTC'` before the date cast is load-bearing.

- [x] **DONE (round 26).** The call on the two portal auth handlers has been made
      in both directions — see [decisions.md](decisions.md) §116.
      `portal_mfa_challenge` **verifies** a factor and mints the session (it is
      the only place an MFA-enrolled supplier's sign-in completes), so it now
      writes `portal.mfa.verify.success` / `.failure`.
      `portal_request_email_otp` genuinely is issuance and stays unaudited, with
      the reason recorded in `_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT` rather than
      as "not yet": a row there would be written for exactly the set of supplier
      addresses that exist **and** are enrolled, rebuilding inside a WORM-shipped
      trail the oracle its 204-on-every-path exists to prevent.

- [x] **DONE (round 26).** `a11y/target-size.spec.ts` was run, and it **does**
      bite: restoring the pre-fix `app.css` block byte-for-byte fails five of its
      six cases with real measurements. The sixth did not, and that was the
      finding — "a click outside the painted box toggles the row" asserted only
      `insetX > 0`, which a 1 px opaque border satisfies, so the click landed on
      the paint and it re-proved that clicking a checkbox toggles it. It now
      asserts the inset reaches `(24 - 16) / 2`. The run also surfaced a **real,
      shipping** 2.5.8 failure on `/organization` caused by round 25's own
      checkbox fix — see [decisions.md](decisions.md) §121 and §123.

### Surfaced by the round-27 batch (2026-09-09)

Ten agents, each in its own worktree, closed **all ten** round-26 entries. Two
integration agents then closed work that spanned two of those slices and could
not have been done inside either. Nothing from round 26 remains open.

The round's most useful result was not a fix. **The worktree entry below was
wrong about its own mechanism, and so was the durable note it came from.** The
editable-install finder is *appended* to `sys.meta_path`, so it sits after
`PathFinder` and `PYTHONPATH` does win. The failure is a **fall-through**, not a
precedence fight: you get the right checkout whenever `sys.path` finds one, and
the primary checkout when it does not. That reclassified which commands were
actually dangerous — `python main.py` and `pytest` were always fine, while
`python scripts/seed.py` and `alembic revision --autogenerate` were silently
wrong, the second producing a plausible migration diffed against the wrong
models. Three entries this round were disproved on measurement rather than
merely completed, which is the pattern rounds 25 and 26 also hit: an entry is a
lead, and a round that only implements its entries ships at least one wrong fix.

Two entries were wrong on their numbers. The vendor teardown said "17
non-cascading FKs, 17 call sites, 16 files"; the graph has 15 non-cascading FKs
and 18 call sites across 17 files. The workflow leak called itself harmless
because the rows were inactive — but one leaked definition still had a live
instance pointing at it, so the API refuses to delete it and only a SQL sweep
can. And the dashboard-disclosure entry prescribed a fix that would have been
**vacuous** for two of its three cases, because an absence assertion over an
empty data series passes whatever the guard says.

Routing again beat filing. Nine findings moved between agents mid-round and were
fixed rather than written down: a stale comment went from the schema agent to the
route agent, the same zero-while-loading defect on two further pages went back to
the agent that had just built the convention, and the card-lifecycle spec's
unordered fixture select went to the agent that diagnosed it. Two more spanned
branches and fell to the integrator — the supplier sign-in audit row, which
needed one agent's route and another's test file at once, and the workflow
teardown consolidation, which needed both halves of a class two separate agents
had closed in parallel.

- [x] **DONE (round 28) — 250 sites to 7, and the exception was disproved.**
      Three agents swept the tree by directory. The entry's count was wrong for
      the third round running: it said 242, which missed a spec that
      double-quotes the argument. The real figure was **250**, verified
      independently before the sweep; 243 are gone.
      All seven survivors are deliberate and documented in place: two in
      `credit-memos/load-sequencing.spec.ts`, whose assertion is that **exactly
      one** request fired — `waitForResponse` proves at least one did, never that
      a duplicate did not, so a quiet network is the only honest signal there —
      two documented `beforeEach` guards, and three that are comments explaining
      why no wait is present.
      **The `fixtures/helpers.ts` "load-bearing for Svelte 5 form hydration"
      caveat is RETRACTED.** The app server-renders no form at all: the root
      layout gates its slot behind a `browser`-guarded `$effect`, effects run
      during neither SSR nor prerender, and `/login` and `/portal/login` return a
      body holding only `<div style="display: contents">`, the toast region and
      comment markers — measured in dev **and** in the built artifact. Ten sites
      were initially kept on that belief and then removed; all ten were followed
      by an auto-waiting `.fill()`, so they were redundant on their own terms
      regardless. A proposed `data-hydrated` app affordance was dropped for the
      same reason. Nine further sites were **replaced** rather than deleted,
      and three of those were tests that could not fail — role-gated absence
      assertions that ran before `GET /api/auth/me` populated the permission
      store, so `toHaveCount(0)` passed on an unrendered toolbar. See
      [decisions.md](decisions.md) §133.

- [x] **DONE (round 28).** `tests-e2e/workflows/seededWorkflowVersions.ts` is a
      **fourth** teardown owner: the third takes a name prefix specifically so its
      `is_default = false` seatbelt cannot be dropped, which makes it structurally
      unable to reach child rows of a *seeded* definition, and widening it would
      delete the property that makes it safe ([decisions.md](decisions.md) §134).
      The mark is a DB-clock reading taken before the spec touches anything; only
      later rows are purged, and the purge then **asserts** the history is back to
      the mark rather than trusting the filter. The entry's numbers were low —
      `e2e5` measured 17, not 9 — and the source guard written to stop a
      recurrence immediately found a **third** offender the entry never named
      (`admin/delete-safety.spec.ts`), which is the argument for the guard over
      fixing the two files that were pointed at.

- [x] **DONE (round 28).** `DashboardData` / `ReportingAgingBuckets` /
      `AgingBuckets` moved to `$lib/types/analytics.ts` and the dashboard
      fixtures now `satisfies DashboardData`. The entry's mechanism was right,
      which is unusual for this file: `frontend/tsconfig.json` extends
      `.svelte-kit/tsconfig.json`, whose generated `include` covers `../src`,
      `../test` and `../tests`, and TypeScript does not merge includes from an
      extended config. A `satisfies` in an unchecked tree would have been
      decoration, so `tsconfig.e2e.json` + `pnpm check:e2e` (root
      `lint:frontend:e2e`, wired into the Frontend CI job) land with it — proven
      to fire by adding a required field and watching the check go red.
      **It found two real drifts on its first run**, which is the argument for
      it: `DashboardDiscountCapture`'s six money fields were typed `MoneyString`
      while the backend serialises them as JSON numbers, and the five
      `AgingBuckets` bands were typed `number`, so the route was running
      `sum + b.value` and `b.value / agingTotal` as raw arithmetic on currency —
      invisible to the money-type ratchet precisely because the type was declared
      inline in the route. See [decisions.md](decisions.md) §136.

- [x] **DONE (round 28).** `/cfo` renders its KPI row on every state with the
      `pending` affordance instead of collapsing behind `{:else if forecast}`,
      matching `/discounts` and `/bank-reconciliation`; the spec swapped to
      `expectRowPending`. The consistency sweep caught two more: `/tax` had the
      same collapse, and `/adaptive` hand-wrote `value="—"`, which renders
      `data-kpi-state="value"` — the card claiming a figure exists — and is now
      `value={null}`. The zeroed-forecast case gained a real readiness gate,
      since the row's presence is no longer a signal that loading finished.
      Seven panel-scoped rows still collapse; they are a separate entry below.

- [x] **DONE (round 29).** The one-off cleanup ran on this box: 42 `meter_test_*`
      plan rows and their 2 child subscriptions deleted from the shared
      `feohledger` control plane, children first, leaving the three real plans
      (`free`/`growth`/`scale`) untouched. The durable half —
      `_reset_control_billing` on the `realdb` teardown, with `RealDB.purge_plans`
      owning that child graph ([decisions.md](decisions.md) §135) — landed in round
      28 and is what stops them coming back. Any *other* long-lived developer box
      still needs the same two statements; CI builds its control plane fresh.

### Surfaced by the round-28 batch (2026-09-09)

Ten agents closed thirteen entries. Every one of them returned something its
entry had not predicted; where that was itself a defect it was fixed in the same
round rather than recorded. What follows is only what genuinely could not be
closed in the slice that found it.

- [x] **DONE (round 29).** Migration `0096` adds
      `recurring_invoice_templates.created_by_user_id` (tenant-scoped, nullable, no
      FK — `users` is control-plane while the table is tenant-local);
      `POST /api/recurring` stamps it and `generate_one` stamps
      `actor_id or template.created_by_user_id`, so the live actor wins for
      generate-now and the author is used for the sweep. Existing rows stay NULL —
      no honest author exists to backfill, and every proxy manufactures either a
      refusal or an absolution. The tests were proven non-vacuous by a negative
      control: with the fallback reverted, the author approving their own template's
      invoice returns `200 {"status": "approved"}`. See
      [decisions.md](decisions.md) §141.

- [x] **DONE (round 29).** Moved onto `require_permission(payment.void)`, matching
      the two sibling routes that close the same card. It is the one migration that
      does **not** reproduce the prior four-system-role matrix, and cannot:
      `ap_manager` holds `payment.execute`, not `payment.void`, so closing the gap
      and preserving the matrix are the same sentence read in opposite directions.
      The narrowing is stated in five places rather than shipped quietly, including
      an explicit four-role table in
      `test_sod_endpoint_wiring.py::test_card_cancel_narrows_ap_manager_by_design`,
      and a second test holds all three card-closing doors to one gate. See
      [decisions.md](decisions.md) §142.

- [x] **DONE (round 29).** One shared `ui/VendorPicker` — a WAI-ARIA 1.2 combobox
      over a new `searchVendorOptions()`, so filtering is server-side and a vendor on
      page 40 is reachable, with a count line stating how much of the matching set is
      on screen. The entry named five consumers; there were **six** (`/credit-memos`
      walked every page on mount — the same defect class, not on the list). No local
      `VendorOption` re-declarations remain. Three further defects surfaced on the
      way and were fixed with coverage: Escape closed the enclosing modal instead of
      the popup, a click on an already-focused picker never re-opened it (making the
      control one-shot), and a failed *next* page reported the whole list as failed.
      `CatalogResponse` gained `vendor_name` so an existing selection can be labelled
      without a per-open SOX access-audit row. See [decisions.md](decisions.md)
      §145-§147.

- [x] **DONE (round 29).** The entry said seven; its own list enumerated nine, and
      three more surfaces had the identical defect with a loading flag already to
      hand (`CfoMetrics`, `AgentDashboard`, `BudgetModal`). **Twelve** fixed,
      rendering as eleven rows — `/billing`'s two copies of the usage section
      collapsed into one, since neither branch of a chain gated on the subscription
      response could own the row. It was not only consistency: six of these rows
      carried an unconditional `highlight`, so the only thing that had ever stopped a
      SOX access review, an operational health check and a tamper check each painting
      a green "all clear" over an unanswered question was the row not existing yet.
      Two rows keep a gate on purpose — `/audit` and `ForecastVariancePanel` report a
      sweep the *user* runs. `tests-e2e/a11y/kpi-pending.spec.ts` extended with six
      cases rather than a parallel spec. See [decisions.md](decisions.md) §143.

- [x] **DONE (round 30).** The product call went to **keep** the read-only mode and
      make it honest; the entry's other branch — widening
      `services/org_settings_view.py`'s projection to fill the panels — is rejected
      outright and recorded as rejected, because those six blocks are the tenant's
      ERP client secret, processor credentials, card API key and extraction key, and
      filling the fields means serving them. Nothing about what the backend serves
      changed. The 403 half: `loadChat()` moved into its own effect gated on
      `userLoaded && auth.isAdmin` — the shape Email Intake beside it already uses —
      and the panel renders `org.chat.adminOnly`. The audit ran over *every* mount
      read rather than the one the entry named and found a second admin-only one,
      `…/fraud-rules/defaults`, fired and its 403 swallowed in a bare `catch {}`;
      gating it split the fraud form into `fraudOverrides` (the role-open org read)
      ⊕ `fraudDefaults` (its own effect), composed by whichever lands second. The
      defaults half: all six panels — AI Extraction, ERP, Payments, Virtual Cards,
      Security, Fraud Detection — replace their **body** with one shared
      `org.readOnly.sectionAdminOnly` hint, because a disabled `<fieldset>` around a
      platform default is still a platform default. Hiding them was rejected: the
      heading is true, Getting Started links to `#org-payments`, and the page would
      carry two vocabularies for one fact. Data Sync deliberately keeps its body —
      the criterion is "does this panel state a fact about the tenant it cannot
      read", and two buttons state nothing. Four e2e cases in
      `tests-e2e/organization/settings.spec.ts`, including the admin negative
      control. See [decisions.md](decisions.md) §153.

- [x] **DONE (round 29).** `switcher.spec.ts` annotated exactly as
      `cfo/by-entity.spec.ts` does, the exclusion deleted, and `exclude: []` kept in
      place so the standing rule — never add an entry; the fix for a type error in
      that tree is the annotation — still has somewhere to live. `pnpm check:e2e` is
      green whole-tree with the spec's assertions unchanged.

- [x] **DONE (round 29).** `utils/list.ts::formatList` is the one owner, sited
      beside `money.ts`/`time.ts` and reading the same active-locale holder;
      `Intl.ListFormat` at `conjunction`/`narrow`, memoized, degrading to `', '` when
      unavailable rather than throwing. Six prose sites migrated; the other thirteen
      deliberately keep the literal and `listJoinAudit.test.ts` records why each does
      — a value re-split on `,` by its own input, or a bare list of identifiers,
      becomes a correctness bug under a locale-dependent separator, so banning the
      literal outright was rejected. vitest pins Japanese (`A、B、C`) differing from
      English. See [decisions.md](decisions.md) §148.

- [x] **DONE (round 29).** Every literal on the route keyed, with real translations
      in all six locales — 51 keys each, not English placeholders. Two things the
      entry did not name went with it: the history verdict badge derived its label as
      `result.replace(/_/g, ' ')` under a `capitalize` that would have title-cased a
      translated phrase mid-word, and the risk level and score were concatenated as
      bare text. The obvious reuse of `SCREENING_STATUS_LABEL_KEYS` does not work —
      the backend collapses `review_required` to `review` before stamping the vendor
      — so `SANCTIONS_RESULT_LABEL_KEYS` is its own drift-guarded map. See
      [decisions.md](decisions.md) §149.

- [x] **DONE (round 29) on the engineering half.** The caller audit ran first, as
      the entry required: 42 call sites across pytest, Playwright and the seed
      script, zero frontend client functions and zero mobile callers, and a union of
      keys that is a strict subset of the declared fields — so nobody breaks and
      `extra="forbid"` landed. The audit ships as an executable assertion rather than
      a claim in a commit message, because a grep result rots. The nested
      `DiscountTier` stays permissive by design: it is also a response model hydrated
      from JSONB, where forbidding would turn an unexpected stored key into a 500 on
      read. The entry's separate product question is re-filed below on its own. See
      [decisions.md](decisions.md) §150.

### Surfaced by the round-29 batch (2026-09-10)

Five agents closed nine entries and half of a tenth. Each returned something its
entry had not predicted — two entries undercounted their own scope, and one
named a durable fix that turned out to break the standing rule it was written
under. What follows is only what genuinely could not be closed in the slice that
found it.

- [x] **DONE (round 30).** Segregation now keys on a **set** —
      `Invoice.uploaded_by_id` ∪ `Invoice.segregation_actor_ids` — and the entry was
      right that the predicate and every path feeding it had to change together.
      `PATCH /api/recurring/{id}` appends the actor to
      `recurring_invoice_templates.material_editor_ids` when a **material** field
      actually changes value; `generate_one` stamps author ∪ editors minus whoever
      already landed in `uploaded_by_id`; `violates_segregation` refuses anyone in
      the set. Migration 0097, unapplied locally (hand-written — the worktree cannot
      safely run alembic against four other agents' shared databases).
      Two things the entry did not predict. **The set had to be snapshotted onto the
      invoice rather than resolved live** from the template: the predicate is a pure
      sync function reused by the auto-approve path and by three non-invoice subjects
      through an attribute shim, and — the stronger reason — a template is mutable, so
      resolving live would let an edit made *after* generation retroactively bar
      someone from approving an invoice they had no hand in. A payable's terms are
      frozen when it is raised (`steps_config_snapshot`); the people those terms are
      attributable to are frozen with them. **And an allowlist of material fields
      fails in the wrong direction**, so `MATERIAL_EDIT_FIELDS` ships with a declared
      complement `COSMETIC_EDIT_FIELDS` and a guard that fails until every new
      PATCHable field is classified — otherwise a money-shaped field added later would
      default to "not material" and silently widen the exemption.
      No backfill, for §141's reason, with one refinement: a *material editor* of a
      pre-0096 template does land in the set, because that act is observed even though
      the authorship is not. The legacy residue is an un-named author, never an
      un-named editor. See [decisions.md](decisions.md) §152.

- [x] **DONE (round 30).** The row renders on every state with `pending={loading}`
      over `null` values, so the app's most-visited KPI row is off its own response
      at last. The collision the entry flagged is resolved **in favour of the row**:
      `isEmptyTenant` is `!!data && total_invoices === 0`, keyed on the response
      having landed rather than on the count, so an empty tenant sees a pending row
      hand over to the `EmptyState` — one brief honest transition, against the
      alternative of collapsing the row for every tenant on every load to spare the
      one tenant with nothing to show. The four conditional cards (exceptions, stale
      approvals, rebates, captured discounts) stay gated on `data` rather than taking
      `pending`: they render only when there is something to report, so a dash there
      would promise a figure that may never arrive — §143's objection pointed the
      other way. `KpiCard`'s tint rule was checked, not assumed: it withholds a tint
      from a *missing* figure only, which is what keeps the `touchless_rate >= 80`
      verdict off a card still waiting. A failed load leaves the row `unavailable`
      above the error banner and its Retry (/cfo's placement), and
      `dashboard/error-state.spec.ts` re-keys its recovery assertion on
      `data-kpi-state` because the restructure would have made "`.kpi` is visible"
      vacuous. Two cases in `a11y/kpi-pending.spec.ts`; the hand-off one needed a
      held *and* shaped response, so `holdEndpoint` took an optional body and the
      zero-invoice payload became its own export in `tests-e2e/dashboard/fixture.ts`
      (not a `DashboardPatch` knob — zero invoices worth $6,000 is a payload the
      backend cannot produce). See [decisions.md](decisions.md) §154.

- [x] **DONE (round 30).** All three surfaces are keyed, with a real translation in
      all six locales (§149's precedent, not English placeholders), and the two dead
      keys are pruned — a grep of the whole `discounts.bulk.*` namespace confirmed
      those two and only those two had lost their last caller. `/invoices` did both
      halves together as the entry required: the `Warnings: …` frame became
      `invoices.warningsAria` and the `.join(', ')` inside it moved to `formatList`,
      so the route left `listJoinAudit`'s `ALLOWED` table for its `MIGRATED` list.
      What that buys is less than it looks and is stated at the call site: the
      findings are composed per row by `services/invoice_warnings.py` from that row's
      own data, so the label is now a localized sentence AROUND server-English
      findings. `InvoiceWarning.type` IS a code, but a label per code would lose the
      PO number / amount / variance that four distinct `po_mismatch` messages embed —
      so the real fix is a parameterized warning-code → message-key catalogue of the
      kind `pnpm gen:einvoice-messages` already builds, re-filed below on its own.
      `/profile` held eighty strings, not the four the entry named, and was extracted
      whole; `AgentDashboard` took `RUN_COLUMNS`, `ACTION_CHIPS`, the inline states
      and the run dialog alongside `COLUMNS`, plus three label maps — the third
      replacing a `replace(/_/g, ' ')` that printed `po mismatch` where the queue one
      tab away says `PO Mismatch` (§149's defect again), now drift-guarded
      byte-for-byte against the backend's own label map. The exception lifecycle
      `status` is the one thing left raw, deliberately, and is filed below.
      See [decisions.md](decisions.md) §155.

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

### Surfaced while clearing the open-PR backlog (2026-09-10, PR #392)

- [x] **DONE (round 30).** **(c) The password hasher is pinned to an abandoned
      passlib, which pins bcrypt to 4.0.** `backend/app/utils/passwords.py` now
      implements the `bcrypt_sha256` scheme itself against the `bcrypt` package —
      HMAC-SHA256 keyed off the encoded salt text, base64 before bcrypt, passlib's
      own `$bcrypt-sha256$v=2,t=2b,r=N$salt$checksum` serialisation — derived from
      the installed passlib source rather than from memory. Every stored hash still
      verifies: v2, the legacy v1 wrapper (`2a` and `2b`), and the plain `$2b$` rows
      written before c6a91396, with the legacy arm truncating at 72 bytes on purpose
      because bcrypt 4.1+ raises where 4.0 ignored the tail. Nobody was asked to
      reset a password. `backend/tests/test_bcrypt_sha256_compat.py` is the proof —
      hashes passlib itself produced, hard-coded as literals, 45 assertions green
      under bcrypt 4.0.1 AND 5.0.0. `pyproject.toml` drops `passlib[bcrypt]` and
      lifts bcrypt to `>=5.0.0,<6`, both hash-locks are recompiled, the
      `.github/dependabot.yml` bcrypt ignore is removed, and the
      `ignore:'crypt' is deprecated` pytest filter retires with it. `docs/decisions.md`
      §151.

### Surfaced by the round-30 batch (2026-09-11)

Five agents closed six entries and opened these eight. Six of them are findings
that could not honestly be folded into the slice that surfaced them; the last two
are deliberate scope calls, recorded so an absence does not read as an oversight.

- [x] **DONE (round 31).** **(c) A legacy password hash is never upgraded, and
      the module claimed it was.** `services/credential_upgrade.py` is the one
      owner: both login handlers call it a line after `verify_password`, and a row
      on a deprecated scheme is re-hashed onto the current one before either
      handler reaches its MFA branch (which mints a challenge, not a token — the
      password is already proven, and gating on the second factor would skip
      exactly the accounts that have one). **Three things the durable fix did not
      say.** (1) Acting on `needs_update` alone is wrong: it is also true for a
      string `identify` cannot name, and this is the only place that would act on
      that — by writing a *working* credential over a row that had none. The
      trigger is `identify(...) is not None` AND `needs_update(...)`. (2) "Assign
      it" is a lost update. The handler holds no row lock across its ~400 ms of
      bcrypt, so a password change committed inside that window would be
      overwritten by a re-hash of the OLD plaintext — the leaked credential its
      owner had just retired, revived. The write is a compare-and-swap on the hash
      that verified, and a zero-row result is a normal outcome, not an error.
      (3) `commit_before_response` is true but not sufficient: catching a rejected
      UPDATE and rolling the caller's transaction back EXPIRES the loaded `User`,
      after which the handler's next attribute read is a synchronous lazy SELECT —
      `MissingGreenlet`, a 500 on a correct password, produced by the error
      handling itself. The write runs in a SAVEPOINT and the log lines read their
      identity into locals first; a test that provokes a real Postgres rejection is
      what found it. The second bcrypt and the timing signal it creates are paid
      openly rather than equalised away, and no audit row is written — the encoding
      moved, the credential did not, and the sign-in is already on the trail in the
      same request. See [decisions.md](decisions.md) §163, §164.

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

- [x] **DONE (round 31).** **(c) An `ap_manager` cannot reach the chart-of-accounts
      ERP sync from anywhere.** GL accounts got the surface, and the entry's second
      question is answered against the panel. `/gl-accounts` is a list page shaped
      after `/purchase-orders` + `/budgets` rather than invented — PageHeader shell,
      `SearchBox` + `FilterChips`, `DataTable`, URL-backed `search`/`type`/`inactive`
      state, one request sequencer, four distinct empty states — and it carries
      **both** of that router's writes behind `auth.isManager`: the sync the entry
      named, and `POST /api/gl-accounts` (create), which turned out to have no caller
      in the app either — the same root cause, one endpoint over.
      **The nav row is gated on the READ, not the write**, which is where the entry's
      own durable fix would have gone wrong if followed literally. `GET
      /api/gl-accounts` is `get_current_user`, so `admin | ap_manager` on the row
      would have hidden a page whose every read succeeds — the dead end `nav.ts` has
      now fixed five times — while leaving `sync-erp` exactly as unreachable. It sits
      in Procurement beside the other two 3-way-match feeders, and `nav.test.ts`
      asserts the exact per-role set so the clerk row cannot be quietly narrowed
      later.
      **Data Sync is not earning its buttons, and does keep its place.** Both were
      the worse copy of an action that now exists on the page that owns it: admin-only
      purely by virtue of `/organization`'s nav gate (against a backend gate of
      admin | ap_manager), silent about which entity's chart or PO set they would
      write into, and reporting into a bare `<span>` with no list to refresh. The
      vendors row had already been a link for that reason, leaving one panel carrying
      two vocabularies for one fact. It is three links now; deleting the section was
      rejected because this is where the ERP connection is configured, so "now where
      do I pull it?" is asked here — and §153's criterion still holds, the panel
      asserts no tenant fact and so needs no admin-only hint.
      Two things the entry had wrong rather than merely incomplete. **"Only picker
      options" understates the read** — it already filtered server-side and was
      already entity-scoped; what it did *not* do was serialize `entity_id`, and on
      this one table NULL means SHARED across every entity rather than unstamped. So
      the consolidated view was handing back every subsidiary's chart at once with two
      legitimate `6000` rows indistinguishable, and an entity-scoped read could not
      separate a shared account from an entity's override of it. The field is in the
      payload now (additive — the picker call sites ignore it) and the page renders it
      as a **Scope** column, only on a multi-entity tenant. **And the read is
      unpaginated on purpose**, which `test_pagination.py` already pinned: both
      consumers need every row, and a paginated picker could not offer a code past
      page 1 — a coding defect, not a paging nicety. The footer therefore states a
      plain count; a `showingAll` + `loadMore` pair would have been the page claiming
      that rows it never fetched do not exist. That exception's recorded rationale
      ("its only consumer is the invoice GL dropdown") is updated rather than left to
      rot. See [decisions.md](decisions.md) §161, §162.

- [x] **DONE (round 31).** **(c) Invoice warnings reach the browser as server
      English.** A parameterized warning-code catalogue, the shape the entry named:
      `backend/app/services/invoice_warning_catalog.py` declares **48 codes**, one
      per distinct SENTENCE, each with its `type` bucket, its English template and
      every placeholder's kind; `warning(code, severity, **params)` is now the only
      way a warning is built and renders `message` from the same template, so the
      fallback and the params cannot disagree. `pnpm gen:warning-messages` derives
      the frontend map, `pnpm check:warning-messages` guards it in CI's Backend lint
      job beside `check:einvoice-messages`, and `api/invoiceWarnings.ts` resolves
      `{code, params}` at BOTH render sites — one resolver, because keying one
      without the other is how a finding comes to read as German in the list and
      English in the tooltip. 48 keys × six locales, real translations.
      **The entry's counts were the unreliable part, as flagged: `po_mismatch` is
      five sentences and `quality_hold` five, not four and three** — and
      `InvoiceWarning.type` is NOT the code, it stays the category several codes
      share. Three things the entry could not see cost more than the keying. Two
      sentences were assembled by cutting a substring out of the matcher's English
      `issues` list, so `MatchResult` gained `ordered_quantity` /
      `received_quantity` / `inspection_deviation_notes` and they parameterize from
      the record. Several money-bearing sentences hardcoded `$` — a wrong figure on
      a ZAR invoice, §156's call again — so the templates name the invoice's own
      currency and the client formats the exact digits with it. And `run_extraction`
      persists the self-correction violations into the `priors_metadata` JSONB, so a
      `params` dict of raw `Decimal`s broke the save outright (`Object of type
      Decimal is not JSON serializable`, surfacing as an invoice stuck at `new`);
      each check now builds its violation through the catalogue, which makes
      JSON-safety a property of construction. The guard a registry needs is an AST
      scan that fails on a hand-rolled warning dict in any producing module, plus
      its mirror — a declared code with no call site. `po_match.issues`, the
      `Exception.description` the queue renders and the mobile panel stay English
      via the fallback, each re-filed below. See [decisions.md](decisions.md) §157.

- [x] **DONE (round 31).** **(c) The exception lifecycle `status` is still the raw
      wire value on both surfaces that render it.** There were **three** call sites,
      not two: besides the queue badge and the run dialog, `AgentDashboard`'s
      runnable-queue table prints the same column under a header that was already
      keyed — so fixing the two the entry named would have reproduced the split
      inside the panel being fixed. All three read
      `types/exception.ts::EXCEPTION_STATUS_LABEL_KEYS`, which points at the four
      `exceptions.filter.*` keys the queue's own chips already use, so no new
      catalogue entries were needed in any of the six locales and a chip cannot name
      a status differently from the rows it filters. The accessor is tolerant like
      its siblings (`status` is a plain `String(30)` with no DB enum, so a row from a
      later build prints raw rather than blank), the roster is drift-guarded against
      `ACTIONABLE_STATUSES` ∪ the image of `RESOLUTION_STATUSES` with its order
      pinned to the four counts `/summary` returns, and the route's local
      `STATUS_TONES` moved into the same module beside the label map — total over the
      union, so a status that gains a colour without a label is now a compile error.
      The e2e count was wrong too: **two** assertions, not three, both on
      `agent-run-status`; while in there, `exceptions/filter.spec.ts`' two row-badge
      assertions were tightened from `/open/i` and `/resolved/i` to the exact labels,
      because a case-insensitive regex would have gone on passing against the raw
      value this replaced. The same pass closed a live instance of §155's own
      deferral in that file: the type-filter chips derived `po mismatch` from
      `exception_type.replace(/_/g, ' ')` while the rows they filter carried the
      server's `PO Mismatch`, so both now read `EXCEPTION_TYPE_LABEL_KEYS`.
      See [decisions.md](decisions.md) §158.

- [x] **DONE (round 31).** **(c) `/profile` shows raw role slugs where `/admin`
      shows labels.** The premise was half true — `/admin`'s own `RolesPanel`
      system-roles table printed `ap_manager` one tab from the user rows that printed
      `AP Manager`, so it was three surfaces and two vocabularies, and all three read
      `types/admin.ts::ROLE_LABEL_KEYS` now. Real translations in all six locales
      over the four `api/deps.py::ALL_ROLES` built-ins (`CFO` stays verbatim, the
      convention the catalogue already applies to it); a **custom** role is exactly
      the value the tolerant accessor has no key for, and falls back to its stored
      name because that name is text an admin typed into their own tenant.
      `/profile`'s join moved to `formatList` in the same change, so the route left
      `listJoinAudit`'s `ALLOWED` table for its `MIGRATED` list — the family-2
      exemption was downstream of there being no label at all, and expired with the
      map. Role slugs stop being a family-2 exemplar there: `/admin/access-review`,
      the one entry still citing them, is reclassified family 3, since that whole
      route is hardcoded English down to its column headers. `types/admin.test.ts`
      pins the roster against the Python and also pins `api/admin.py`'s refusal of a
      custom role named after a built-in — that check is the only thing making the
      fall-back-to-tenant-data sound, and losing it would break nothing visibly. The
      `ROLE_LABELS`-stays-English note in `frontend/docs/i18n.md` moved with it.
      See [decisions.md](decisions.md) §159.

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

- [x] **DONE (round 31).** **(c) Mobile renders money with a hardcoded `$`, and the
      new adaptive screen had to opt out of it.** The entry named four screens; there
      were **eleven call sites across ten files** — add `dashboard_screen.dart`
      (`compactCurrency`), `payment_queue_screen.dart`, `invoice_detail_screen.dart`
      and all three list tiles (`invoice_list_tile`, `contract_list_tile`,
      `exception_list_tile`). It also had the resolution order wrong in a way that
      would have shipped a bug: `invoice_defaults.currency` is
      `resolve_reporting_currency`'s **third** rung, not its second — rung 2 is
      `payments.home_currency`, which `NON_ADMIN_SETTINGS` admits *by name* and the
      web `reportingCurrency.ts` mirror reads — so an org whose only signal is that
      key would have resolved to nothing on mobile while the server denominated its
      figures in it. `OrgCurrencyStore` resolves all three and returns `null` when
      none is usable (§119: the fourth rung is a server-side config no client can
      read).
      The larger correction is that **a store alone was the wrong fix for most of
      these surfaces**. An invoice, contract, payment and payment-queue row carry
      their OWN `currency`, so labelling them with the org's reporting currency is a
      different wrong answer, not a fix — the two detail screens already printed the
      real code as a `Currency` row directly under a `$` amount. `utils/money.dart`
      therefore takes the currency as an argument (`formatMoney` for a `num`,
      `formatMoneyString` for an exact decimal string, `formatMoneyCompact` for a KPI
      tile), and each call site passes the most specific code it has: the row's own;
      else the payload's (`/payments/summary`'s `currency`, `cash_position`'s
      `opening_balance_currency`, the dashboard's `reporting.reporting_currency`);
      else `OrgCurrencyStore`, whose only consumers turn out to be the adaptive
      patterns tab and the cash-flow forecast fallback; else **nothing**, and the
      figure renders bare — the contract `PaymentResponse.currency` already stated
      (§79/§82), now §160.
      Three things the entry could not have known: `PaymentResponse` already ships
      `currency` and mobile simply wasn't parsing it; `/api/exceptions` shipped no
      currency at all, so `_exception_dict` now joins `inv.currency` through; and
      `DashboardData` was reading naive cross-currency sums, so it moved to the
      `reporting.*` / `aging_reporting` / `upcoming_total_amount_reporting`
      counterparts — which is what makes naming a currency possible there at all. The
      adaptive section note now renders only when no code resolves. Guards:
      `mobile/test/utils/money_test.dart` (symbol, placement, JPY's zero minor units,
      the >15-digit pass-through), `mobile/test/stores/org_currency_store_test.dart`
      (every rung incl. all-unset), widget coverage on the invoice tile / payments /
      exception detail / contract detail / dashboard / payment queue (three
      denominations on one screen) / adaptive, and
      `backend/tests/test_exception_assignment.py`. See `mobile/CLAUDE.md`
      § Money formatting and [decisions.md](decisions.md) §160.

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

Five agents, each in its own git worktree, closed six entries and opened these
eighteen (sixteen from the slices, two from the round's own CI run). **Every one of the six entries was wrong about its own work** — not
merely incomplete — and in three cases implementing the entry as written would
have shipped a defect: a rung skipped in the reporting-currency chain, a nav row
gated on a write instead of its read, and a credential write with no
compare-and-swap. That is the pattern worth carrying forward, six rounds running:
an entry's file list and its counts are the least reliable part of it, and a
durable fix stated in one sentence has usually not been tried.

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

- [ ] **(c) A payment run's total is a cross-currency `SUM` with no currency, so
      it now renders bare — including on the dialog that authorizes execution.**
      `payment_runs.total_amount` is `SUM(Payment.amount)` and each payment is
      denominated in its own invoice's currency, so a run spanning a USD and a
      EUR invoice holds a quantity in neither. Round 31 stopped stamping a `$`
      on it (decisions §160) and the figure is now honest but unlabelled, on
      `/payments/runs/` rows, the CFO sign-off dialog and the execute
      confirmation — the three places an operator authorizes money.
      **Durable fix:** roll the run total up server-side the way
      `GET /api/payments/queue` already does — `payment_reporting_amount_sql`
      per run, returning `total_amount` in the reporting currency plus
      `currency` and `unconverted_count`, which is the same triple
      `/payments/summary` returns — then have the mobile `PaymentRun` model and
      the web run views read it. A `by_currency` breakdown per run would let a
      mixed run say so explicitly instead of reporting one number.
      **Trigger:** the first multi-currency payment run, or any change to the
      runs list/detail payload.

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
      `?? orgCurrency.currency` call sites — `bank-reconciliation/+page.svelte`,
      `PositivePayModal.svelte`, `RunDetailModal.svelte` — and keep
      `DEFAULT_CURRENCY` only for the picker defaults and form initial values
      that genuinely need a value. Web-only; the two surfaces currently disagree
      about the same row.
      **One site is a backend gap, not a formatter one, and is the next one to
      do:** `routes/purchase-orders/+page.svelte`'s `formatCurrency` labels every
      PO with `orgCurrency.currency` because `GET /api/purchase-orders` serves no
      per-row `currency` at all — `PurchaseOrder` carries the column, so this is
      one serializer field plus the type, the same one-line shape
      `_exception_dict` took this round, and it must land before the formatter
      change or the bare-figure rendering would replace a wrong label with no
      label on every PO row. **Not** a site:
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
      indistinguishable options that code to different accounts. The picker value is the uuid `id`,
      so the *write* is unambiguous; what the user cannot do is choose deliberately between them.
      They already receive `entity_id` (round 31 added it to the payload), so the data is in hand.
      **Durable fix:** when `entityStore.multiEntity`, append the resolved entity name to an
      entity-scoped option's label and leave a shared one bare — the same shared-vs-owned
      distinction the Scope column draws, expressed in an option label; extract it as one helper
      beside `GlAccountOption` rather than writing the same conditional in four pickers.
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
      **Durable fix:** pin each to a digest (`image@sha256:…`) or at minimum a
      dated release tag, with Dependabot's `docker` ecosystem enabled so the bumps
      arrive as reviewable PRs rather than as drift (the repo already groups
      `pip`/`npm`/`github-actions`; `docker` is the one ecosystem not covered —
      `.github/dependabot.yml`). Do it in one change across `backend/docker-compose.yml`
      and both CI call sites, and note that pinning MinIO means choosing a
      `RELEASE.*` tag deliberately rather than inheriting whatever `latest` was,
      which is a behaviour decision and the reason round 31 did not fold it in.
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
      of them exists yet; the mail DNS itself is on the unmerged
      `feat/infra-email-dns` branch. A published `mailto:` that bounces is worse
      than none at all, because a data subject who writes to it reasonably
      believes the request is made and the Art 12(3) one-month clock has started.
      **Durable fix:** land the mail DNS branch, create the five aliases, and
      send a test to each before the pages are linked from anywhere public.
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
      the set. The per-document counsel questions are recorded in
      `reviews/saas-legal-review-legal-pages.md` and
      `reviews/us-legal-review-legal-pages.md`; the liability cap figure, the
      arbitration-vs-courts call, and the SCC module and governing-law selections
      in the DPA are the ones that genuinely need advice rather than a decision.
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

**Written — CLOSED (round 20).**
[`docs/founder-runbooks/custom-domain-provisioning.md`](founder-runbooks/custom-domain-provisioning.md)
covers both deployment shapes, because the certificate story differs between
them: Caddy + Let's Encrypt HTTP-01 on the single VM (per-host site block,
automatic renewal, no separate validation record) and ACM in `us-east-1` behind
CloudFront alternate domain names (a permanent `_token` CNAME the customer owns;
certs are immutable, so onboarding re-issues). Plus the DNS records, the CORS
env change, end-to-end verification through the public branding endpoint,
rollback, and the failure modes this code path actually has.

The whole AWS branch and every quota figure are marked confirm-on-first-run:
`infra/` defines no CloudFront distribution yet (the platform certificate has
existed since 2026-09-15, but nothing serves with it), and no ARN or resource
name was invented.

**Restored, without the credential that made it inert — CLOSED (round 20).**
`.github/workflows/dependabot-lockfile.yml` is back. The reason it was removed
in #325 is designed out rather than re-gated: it no longer needs a
`DEPENDABOT_LOCKFILE_PAT`, so there is no missing-secret gate left to
skip-and-succeed. It runs on `pull_request_target` (base-branch context, so
`GITHUB_TOKEN` can carry `contents: write`), filtered to the four manifests and
gated on `github.actor == 'dependabot[bot]'` plus a same-repo head — strictly
less privileged than `dependabot-auto-merge.yml`, which already merges these
PRs on the same trigger.

Three jobs, and only one holds `contents: write`. The two resolver jobs check
out PR head **by immutable SHA** with `persist-credentials: false`, reference no
secret, and run the resolvers — sized on the assumption that `uv pip compile`
can build an sdist and execute third-party `setup.py`. The push job downloads
their artifacts and runs only `git` and `cp`, takes every trusted input from the
event payload, uses no `--force`, and refuses if the branch tip moved off the
SHA the resolvers ran against.

**The pnpm overrides check is the point of the workflow, not a detail.** Between
`--lockfile-only` and the upload it asserts every `pnpm.overrides` key is
present in the lockfile at the same value; a miss exits 1 *before* anything is
uploaded, so the push job has nothing to commit. There is no
`--no-frozen-lockfile` anywhere in the file — the automation must never become
the thing that quietly launders away the guard that caught #344/#351. That check
was extracted and run against the real files: passing intact, failing on a
deleted block, a dropped key and a weakened value.

**Both open verifications closed on 2026-09-07** by PRs #379 (ruff) and #381
(boto3), the first real Dependabot manifest bumps to reach this workflow. The
trigger fired on both, the resolver jobs produced correct locks, and the push
job's `GITHUB_TOKEN` commit onto the Dependabot branch was accepted
(`ruff==0.16.6`, `boto3==1.43.89` — the two pins
`tests/test_dependency_lock_sync.py` had just failed the PRs on).

**What the run corrected in this entry's own description**, and it changes the
operator instruction: a `GITHUB_TOKEN` push does not start a workflow run
*unattended*, but it does not leave the head uncovered either. The
`synchronize` event created the CI / Security / gitleaks runs against the synced
commit and parked all six in `action_required`, awaiting a maintainer's
approval — which is why both PRs still read red after a successful sync. One
approval per run (`gh api -X POST
repos/<owner>/<repo>/actions/runs/<run_id>/approve`, or the checks tab's
"Approve and run") verifies the new head; the empty-commit / "Update branch"
advice this entry used to give is heavier than approving runs already queued
against the right SHA. The four places that restated the old claim
(`.github/workflows/dependabot-lockfile.yml` header + push-job step summary,
`backend/CLAUDE.md` § Dependency lock, `frontend/CLAUDE.md` § The lockfile) were
corrected in the same change.

**Still open — category (b), an operator step on merged code.** Removing the
approval click needs a credential whose pushes retrigger workflows unattended: a
fine-grained PAT with `Contents: Write` in the **Dependabot** secret store, or
(sturdier — no expiry, not bound to one person) a GitHub App token via
`actions/create-github-app-token`. Until then every synced Dependabot PR costs
one approval, and a PR left unapproved reads red for a reason that is not a test
failure. The manual recipe in [backend/CLAUDE.md](../backend/CLAUDE.md)
§ Dependency lock stays the documented fallback.
**Trigger:** an operator provisioning either credential.

