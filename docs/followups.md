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

Mirrored as GitHub issue [#321](https://github.com/Absence0760/project-account-payables/issues/321)
for the tracker view. Keep the two reconciled when either moves.

**Last reconciled:** 2026-09-11 (round 30) — five agents, each in its own git
worktree, plus integrator verification of the merged branch. **Six** entries
closed, **eight** opened. **27 → 29** — by category, **19 (c)** · **7 (a)** ·
**3 (b)**.

**The total went up, and that is the file working rather than failing.** Three of
the five slices returned a defect their entry had not named, and the honest
destination for one that cannot be fixed in the slice that found it is a new
entry, not a closed one. Six of the eight new entries are of that kind; the other
two are deliberate scope calls recorded so they don't read as oversights.

**A docstring asserting a mechanism is not evidence of one** — the same lesson
the `networkidle` comment taught round 29, now on the login path. The password
module claimed passlib's `deprecated="auto"` policy re-hashed a legacy credential
when its owner next logged in. Nothing has ever called `needs_update` or
`verify_and_update`, so the claim was false for the two years it stood, and a
pre-`bcrypt_sha256` row is still verifying against a raw `$2b$` hash with no
pre-hash. The rewrite implemented the signal and deliberately left it unwired
(§151); wiring it is now its own entry, with the DB-write-on-login cost stated.

**An allowlist fails in the wrong direction, so it ships with its complement.**
`MATERIAL_EDIT_FIELDS` decides who is implicated in a payable raised from a
recurring template, and a money-shaped field added to the PATCH schema later
would have defaulted to "not material" and silently widened the exemption. It
ships with a declared `COSMETIC_EDIT_FIELDS` and a guard that fails until every
new field lands in exactly one of them — the omission-reads-as-oversight shape
§141 and the uploader-stamping guard already exist to prevent (§152).

**Auditing the whole surface beat auditing the named defect, twice.** The
`/organization` entry named one admin-only read whose 403 reached a clerk as a
live alert. Checking every mount read found a second, `…/fraud-rules/defaults`,
firing unconditionally with its refusal swallowed in a bare `catch {}` — the
identical defect with the symptom suppressed, which is why it had never been
reported. The same pass found `docs/authentication.md`'s permission matrix
drifted on the exact point the entry was about: it called the organization reads
admin-only when four of them are open to any authenticated role (§153).

**An entry's count is still the least reliable part of it, for the sixth round
running.** "`/profile` ('Full name', 'Email', 'Roles', 'Saving...')" held eighty
strings. The same entry's claim that exactly two `discounts.bulk.*` keys had gone
dead was right — established by grepping all thirty, not by trusting it (§155).

**Bringing a surface to mobile is deciding which of its controls do not travel.**
Inspections shipped whole, write included, because a receiving dock is the
strongest case in the backlog for recording an outcome on a handset. Adaptive
shipped read-first as a finished state: both apply paths reassign a live approval
or raise the org-wide auto-approve threshold, and a phone-sized copy of a control
with its explanation cut off is worse than its absence (§156). Opting out of the
mobile money formatter to avoid stamping dollars on a ZAR org's averages is what
exposed four existing screens doing exactly that.

**Exception resolution stays held.** It remains the only segregation-of-duties
item in the file and was deliberately untouched for the seventh round running,
pending the standing "loop in the CISO / Security Analyst" gate on that section.

## (c) Feature work — sized and unstarted

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
      [decisions.md](decisions.md) §165 (the subject inversion) and §166 (why the
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

- [ ] **(c) A legacy password hash is never upgraded, and the module claimed it
      was.** `app/utils/passwords.py` carried a docstring asserting that passlib's
      `deprecated="auto"` policy would re-hash a credential onto the current scheme
      when its owner next logged in. It never happened: nothing in the codebase has
      ever called `pwd_context.needs_update` or `verify_and_update`, so the claim was
      false for the two years it stood. A user who has not changed their password
      since commit c6a91396 (2026-05-10) is therefore still authenticating against a
      raw `$2b$` bcrypt hash with no SHA-256 pre-hash — which, for a password longer
      than 72 bytes, is the exact truncation weakness `bcrypt_sha256` was adopted to
      close, and which stays open for as long as that user never resets. Round 30
      corrected the docstring and implemented `needs_update` (total over
      `bcrypt_sha256` / `bcrypt_sha256_v1` / `bcrypt`, tested in
      `tests/test_bcrypt_sha256_compat.py`), but deliberately did not wire it:
      consulting it means a DB write on the login path, on BOTH the employee
      (`api/auth.py::login`) and the supplier-portal (`api/portal_auth.py`) surfaces,
      inside a slice whose whole value was that nothing about verification changed.
      **Durable fix:** after a successful `verify_password` in each login handler, when
      `pwd_context.needs_update(user.hashed_password)` is true, re-hash the
      just-verified plaintext and assign it. Both handlers already run under
      `commit_before_response`, so no new commit plumbing is needed; the MFA branch
      returns a challenge before the token is minted but AFTER the password verified,
      so the upgrade belongs before that branch. Needs a test per surface proving a
      `$2b$` row becomes `$bcrypt-sha256$` on one successful login and that a wrong
      password leaves it alone.
      **Trigger:** the next auth slice, or any evidence that pre-c6a91396 hashes
      actually exist in a deployed database (no deployment has happened yet, so
      today this is latent rather than live).

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

- [ ] **(c) An `ap_manager` cannot reach the chart-of-accounts ERP sync from
      anywhere.** `POST /api/gl-accounts/sync-erp` is
      `require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)`, and its ONLY caller in the app is
      the Data Sync panel on `/organization` — a route the nav shows to `admin`
      alone, inside the blanket read-only `<fieldset>` that disables every control
      for a non-admin. Its two siblings are reachable (`/purchase-orders` and
      `/vendors` each carry their own Sync-from-ERP button gated on
      `auth.isManager`, matching the same backend gate); GL accounts have no list
      page at all, only picker options behind `GET /api/gl-accounts`. So the one
      role the backend deliberately widened this write to is the one role with no
      way to perform it. Surfaced while making the read-only mode honest (§153) and
      left alone there on purpose: §153 is about a non-admin being told the truth,
      and carving a control OUT of that fieldset is the opposite change — it cannot
      be done by nesting either (a disabled `<fieldset>` cannot be re-enabled from
      within), so the panel would have to move out of the stack.
      **Durable fix:** give GL accounts the surface its two 3-way-match siblings
      have — a `/gl-accounts` list page with the sync action gated on
      `auth.isManager` — and then decide whether the `/organization` Data Sync panel
      is still earning its place or is a duplicate of three per-entity buttons.
      **Trigger:** the next procurement/GL slice, or the first tenant whose AP
      manager is asked to refresh the chart of accounts.

- [ ] **(c) Invoice warnings reach the browser as server English, so a localized
      label frames un-localized prose.** Round 30 keyed the `/invoices` warning-icon
      frame and moved its join to `formatList`, but each finding itself is composed by
      `services/invoice_warnings.py` from the row's data — `PO 4412 not found`,
      `Round amount: 5000.00`, a variance percentage — and is rendered verbatim both
      there and in `InvoiceModal`'s warnings list. A non-English user gets a
      translated frame and English findings.
      **Durable fix:** a parameterized warning-code → message-key catalogue, the shape
      `pnpm gen:einvoice-messages` already generates from the backend e-invoice rule
      set (`frontend` reads the generated map, a drift guard fails on a new code).
      `InvoiceWarning.type` is the code to key on, but a label per code is NOT enough:
      `po_mismatch` carries four distinct messages and `quality_hold` three, each
      embedding a PO number, an amount or a variance, so the backend must emit
      `{code, params}` beside `message` and keep `message` as the fallback for an
      un-keyed code.
      **Trigger:** the next i18n slice, or the next change to `invoice_warnings.py`'s
      message set — whichever comes first.

- [ ] **(c) The exception lifecycle `status` is still the raw wire value on both
      surfaces that render it.** `/exceptions`' queue badge prints `exc.status`
      (`open`, `escalated`, `resolved`, `dismissed`) and the AI-agents run dialog
      prints `runOutcome.exception.status`; the round-30 slice keyed everything around
      them but left both raw, because keying one without the other is how one status
      comes to wear two names in one page.
      **Durable fix:** `EXCEPTION_STATUS_LABEL_KEYS` pointed at the `exceptions.filter.*`
      keys the queue's own chips already use (NOT a second set of keys), a tolerant
      accessor beside `exceptionTypeLabelKey`, and both call sites in the same change.
      It was deliberately not added as an unused export this round.
      **Trigger:** the next /exceptions slice. Note it is not a one-line swap —
      `tests-e2e/exceptions/agent-resolve.spec.ts` asserts
      `toContainText('resolved')` / `('escalated')` on `agent-run-status`, and
      `toContainText` is case-sensitive, so three assertions move with it.

- [ ] **(c) `/profile` shows raw role slugs where `/admin` shows labels.** The Account
      card renders `auth.user.roles.join(', ')` — `admin, ap_manager` — while
      `UsersPanel` renders the same roles through `types/admin.ts::ROLE_LABELS` as
      `Admin, AP Manager`. The join is correctly classified family 2 in
      `listJoinAudit.test.ts` *because* they are slugs, so the two defects are one.
      **Durable fix:** `ROLE_LABEL_KEYS` over the four system roles with a tolerant
      accessor (custom roles keep falling back to their stored name, which is tenant
      data and must not be translated), then use it on both surfaces and migrate
      /profile's join to `formatList` — at which point the route leaves `ALLOWED`
      entirely. `ROLE_LABELS` is currently documented as staying data-driven English,
      so that note in `frontend/docs/i18n.md` moves with it.
      **Trigger:** the next i18n slice, or the next /profile or user-management pass.

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

- [ ] **(c) Mobile renders money with a hardcoded `$`, and the new adaptive
      screen had to opt out of it.** `NumberFormat.currency(symbol: '\$')` is
      declared at the top of `payments_screen.dart`, `cash_flow_screen.dart`,
      `contract_detail_screen.dart` and `exception_detail_screen.dart`, so every
      figure on those screens reads as dollars regardless of the org's currency.
      The adaptive patterns tab could not join that: its per-vendor amounts are
      in the org's **reporting** currency, which the `approval-patterns` payload
      does not name, so it renders the exact server strings under a section note
      instead (decisions §156). That is correct for that screen and makes the
      inconsistency visible: four screens assert a currency they were never told.
      **Durable fix:** the data is already there — `GET /api/organization` is
      `get_current_user` and `services/org_settings_view.NON_ADMIN_SETTINGS`
      projects the top-level `reporting_currency` (plus
      `invoice_defaults.currency`) to every role *for exactly this reason*; the
      web `orgCurrency` store consumes it. So: a small mobile `OrgCurrencyStore`
      mirroring it (resolution order `reporting_currency` →
      `invoice_defaults.currency`, the first two rungs of
      `currency_conversion.resolve_reporting_currency`), plus one shared
      `formatMoney(amount, currency)` helper that the four `$` screens and the
      adaptive patterns tab call. Note the mobile `OrgSettings` model does not
      parse `reporting_currency` yet. **Trigger:** the first non-USD tenant on
      mobile, or any new mobile screen that renders money.

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



## (a) Blocked on external credentials, accounts, or hardware

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
`infra/` is KMS + S3 only, so no distribution or certificate exists yet, and no
ARN or resource name was invented.

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

