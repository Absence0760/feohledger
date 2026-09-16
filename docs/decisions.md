# Architecture decisions

Short records of non-obvious choices that the code alone doesn't explain. Reach
for this before proposing something that's already been considered and rejected.

This is not a strict ADR template — each entry is a few paragraphs: what we
decided, why, and what we traded away. **Append new entries to the bottom; don't
rewrite history.** Number them sequentially so other docs, commit messages, and
code comments can cite them as `decisions §N`. Date an entry when you know the
date (a migration's landing date is a good anchor); leave it off rather than
guess.

Entries are deliberately short. Where a full treatment already exists in
`docs/` or `backend/docs/`, this file records **the call and the reasoning** and
links out for the mechanics — it does not duplicate them.

**Scope:** decisions, not status. Feature progress lives in
[roadmap.md](roadmap.md), open deferrals in [followups.md](followups.md),
diagnosed-but-unfixed defects in [known-issues.md](known-issues.md).

---

## 1. Database-per-tenant, with a JWT cross-check as the real boundary

**Decided:** initial architecture · `backend/app/tenant.py`

Each tenant gets its own PostgreSQL database (`feoh_<slug>`); a shared
control-plane DB (`feohledger`) holds organizations, users, and roles. The
alternative — one database with a `tenant_id` column and row-level security —
was rejected because an AP ledger's blast radius on a missed predicate is
somebody else's invoices and bank details, and a forgotten `WHERE tenant_id =`
is a silent, ordinary-looking bug.

The decision that actually carries the security, though, is **not** the physical
split — it's that `get_tenant()` cross-checks the JWT's `org` claim against the
org resolved from the `X-Tenant-Slug` header and 403s on mismatch. Without that,
database-per-tenant buys nothing: an authenticated user from tenant A could read
tenant B simply by changing a header. The same cross-check gates the white-label
custom-domain fallback, so a forged `Host` can't widen access either.

**Trade-off:** every schema change has to fan out across every tenant DB
(`scripts/migrate_all_tenants.py`), connection-pool pressure scales with tenant
count (hence `pool_size=1, max_overflow=0` on the worker engines), and
cross-tenant analytics are not a single query. Accepted.

**Don't re-litigate unless** tenant count reaches a scale where per-tenant
connections, not correctness, is the binding constraint.

See [multi-tenancy.md](multi-tenancy.md).

---

## 2. The invoice header `amount` is never recomputed from its line items

**Decided:** `backend/app/services/invoice_warnings.py`

`PUT /api/invoices/{id}/line-items` does **not** set
`invoice.amount = sum(lines)`. Three reasons, any one sufficient:

- **Line `total` semantics aren't uniform across ingest paths.** The vision
  adapters emit a *tax-inclusive* line total; `e_invoice/mapper.py` maps the same
  column onto UBL `LineExtensionAmount`, which is *tax-exclusive*. An overwrite is
  right under one reading and wrong under the other.
- **Lines are frequently partial** — a reviewer keys only the disputed line.
- **It would move money with no approval behind it.** `amount` is a
  `_FINANCIAL_FIELDS` member; the header `PATCH` refuses to touch it once
  approved, and the approval signature is taken over the exact amount. A side
  effect of a line edit must not do what the header edit itself is forbidden to
  do.

Equally, a mismatch is **not** a 422 — `tax_amount` / `shipping_amount` /
`discount_amount` are separate header columns, so `sum(lines) != amount` is the
ordinary shape of a valid invoice. Instead the divergence raises an `error`-level
`line_total_mismatch` warning plus a de-duplicated exception, which blocks
payment-run inclusion until a human resolves it.

**Trade-off:** a genuinely wrong header stays wrong until someone acts on the
exception. That's the intended shape — a human, not a sum, decides.

See [line-total-reconciliation.md](../backend/docs/line-total-reconciliation.md).

---

## 3. Card-rail payments are excluded from the 1099 reportable total

**Decided:** `backend/app/services/payment_methods.py`

The IRS puts card-settled payments on Form 1099-K, filed by the *settlement
entity*, not on the payer's 1099-NEC/MISC. So a vendor paid $10,000 by ACH and
$5,000 by virtual card gets a **$10,000** box-1 figure. Filing $15,000
over-reports the vendor and double-counts the card leg against the processor's
own 1099-K.

Two sub-decisions worth recording:

- **Unknown / `NULL` rails stay reportable.** Defaulting an unclassified rail
  *out* would silently drop filed money; under-reporting is as wrong as
  over-reporting. The manual/legacy payment path writes NULL, so this is not
  hypothetical.
- **The excluded money is surfaced, not dropped.** `card_paid` /
  `total_card_excluded` appear on the report so the difference reconciles.

The classification lives in exactly one module, and
`tests/test_payment_methods.py` is a **drift guard** — adding a rail to the
`PaymentMethod` enum, to any adapter's `supported_methods`, or to the corridor
fee overrides fails the suite until that rail is classified.

See [tax-1099.md § Card payments are excluded](../backend/docs/tax-1099.md).

---

## 4. A NULL `entity_id` means "unstamped" on vendors but "shared" on GL accounts

**Decided:** multi-entity Phase 1–3 · `backend/app/services/vendor_matching.py`

The same NULL carries two different meanings on purpose, which is the kind of
thing that looks like a bug on first read.

On `gl_accounts`, NULL is a deliberate **shared chart** marker — one chart of
accounts serving every subsidiary. On `vendors`, NULL means the row was never
stamped with a subsidiary: a pre-multi-entity row migration `0029`'s backfill
didn't reach, or one auto-created from an entity-less invoice.

Unstamped vendors stay matchable from **every** entity. Excluding them would not
fail loudly — it would silently mint a duplicate vendor, splitting the supplier's
spend rollup and giving it a second, independently editable bank-detail record.
A supplier is a real-world counterparty, not subsidiary-private data. When the
same supplier exists both unstamped and under the invoice's own entity, the
entity's own row wins.

The scoping matters because `Invoice.vendor_id` is what the fail-closed
credit-memo guard (§9) compares — a cross-entity mislink has a money consequence.

**Trade-off:** the asymmetry needs explaining every time someone reads
`apply_entity_scope(include_shared=True)`. Hence this entry.

See [vendor-management.md § Matching is scoped to the invoice's entity](../backend/docs/vendor-management.md).

---

## 5. Custom roles granted no access at all until a real permission layer existed

**Decided:** superseded 2026-06-20 by migration `0062_role_permissions`

Custom roles were creatable in the UI long before they could grant anything —
`require_roles` matched system role names only, so a custom role was inert by
design. This was repeatedly mistaken for a bug, and the tempting "fix" was to
make `require_roles` match custom role names too.

That was rejected. Role-name matching would have made a custom role an
all-or-nothing clone of a system role, which is the opposite of what an org
splitting duties needs — and it would have let an admin mint a role that silently
carried payment-execution rights.

The durable fix shipped instead: a granular permission catalog
(`backend/app/api/permissions.py`), a static system-role → permissions map
reproducing the prior matrix exactly, and a control-plane `roles.permissions`
JSONB column. Effective permissions are the union over a user's roles. Only the
*splittable* fraud-sensitive duties moved to `require_permission` — payment
execute/void, run approve, vendor bank-change approve, vendor block/manage, user
management. Everything else stays on `require_roles`.

**Trade-off:** two enforcement mechanisms coexist. Deliberate — moving
everything to permissions would have been a large, low-value blast radius.

See [authentication.md § Granular permissions](authentication.md).

---

## 6. Vendor-statement differences are reconciliation lines, not Exceptions

**Decided:** migration `0047` · `backend/app/services/vendor_statement_recon.py`

A `missing_on_our_side` row — "the supplier billed invoice X and we have no
invoice for it" — has, by definition, no invoice on our side. When
`Exception.invoice_id` was still `NOT NULL`, representing it as an Exception
would have meant fabricating a placeholder invoice. Migration `0049`
(2026-06-19) later made that column nullable for Positive Pay's invoice-less
fraud flags, so the constraint is no longer the blocker — but the recon line
remains right regardless:

- It **describes a missing invoice and feeds intake**; the clerk resolves the
  line by creating the real invoice.
- **The run is the unit of work, not the invoice.** Exceptions hang off one
  invoice; a statement reconciliation is a vendor-and-period batch whose lines
  roll up together into counts, totals, and close-readiness.
- **Different lifecycle** — resolve/ignore with a note, and the run auto-flips to
  `resolved` once no actionable line is open.

**Trade-off:** two review queues for a clerk to watch. Accepted; they answer
different questions.

See [vendor-statement-reconciliation.md](../backend/docs/vendor-statement-reconciliation.md).

---

## 7. API keys are unsalted SHA-256 + an indexed prefix, not bcrypt

**Decided:** 2026-06-19 · migration `0055_api_keys`

This deliberately departs from the project invariant that says passwords use the
shared `bcrypt_sha256` context — and the departure is narrow and reasoned, so a
reviewer shouldn't flag it.

API keys are high-entropy random tokens (`secrets.token_urlsafe(32)`), not
user-chosen passwords, and they must be **looked up** by the presented value. A
salted bcrypt hash is deliberately un-indexable: verifying would mean scanning
every row and bcrypt-verifying each. So the platform stores `sha256(full_key)`
plus an indexed `key_prefix`, resolves candidates by prefix, then compares in
constant time via `hmac.compare_digest`. Brute-forcing a 256-bit random token is
infeasible, so salting buys nothing here.

This is the same pattern the SCIM bearer token already used
(`Organization.scim_bearer_hash`). The `bcrypt_sha256` invariant remains
absolute on the **password** path.

**Trade-off:** two hashing idioms in the codebase. Mitigated by code comments at
both sites plus this entry.

See [public-api.md § Why API keys are SHA-256, not bcrypt](../backend/docs/public-api.md).

---

## 8. A candidate MFA secret lives in Redis, never on the account row

**Decided:** `backend/app/api/auth.py` · `FEOH_MFA_ENROLL_PENDING_TTL_SECONDS`

`POST /auth/mfa/enroll` mints a *candidate* TOTP secret and parks it in Redis
(`mfa:pending_enroll:<user_id>`, 15 min default). `User.mfa_secret` /
`mfa_enabled` / `mfa_enrolled_at` are written by `/auth/mfa/enroll/verify` and
nowhere else.

Previously enroll-start wrote the new secret straight onto the row and cleared
`mfa_enabled` — which made merely *starting* an enrollment a silent
second-factor strip. An abandoned enrollment left the account single-factor with
no signal to anyone.

Two related calls in the same area:

- **Changing an existing factor is a step-up**, gated by password, a code from
  the live authenticator, or a WebAuthn assertion — required only once a factor
  exists, so first-time enrollment stays frictionless. Removing a factor is
  gated exactly like adding one.
- **Step-up challenges are bound to purpose *and* operation** via separate
  single-use Redis slots, so a step-up assertion can't be replayed as a login, a
  login assertion can't satisfy a step-up, and a register assertion can't
  authorize a delete.

The supplier portal mirrors the pending-enroll behaviour
(`mfa:vendor_pending_enroll:`) but has **no** passkey step-up —
`WebAuthnCredential` is control-plane and `VendorUser` is tenant-scoped.

See [authentication.md § Per-user enrollment](authentication.md).

---

## 9. Applying a credit memo is fail-closed on the vendor link

**Decided:** `backend/app/api/credit_memos.py`

A credit memo's vendor must **provably** equal `Invoice.vendor_id` before the
memo reduces the invoice's balance. A NULL `Invoice.vendor_id` is refused with a
409 rather than treated as a wildcard match.

Treating NULL as "matches anything" is the natural-looking implementation and it
is exactly the mis-attribution to prevent: an invoice whose vendor can't be
established must not have money moved against it on a guess. Both application
paths (`POST` with an `invoice_id`, and `POST {id}/apply`) row-lock the invoice
and run the same vendor / currency / over-application guards.

The corollary is that resolving a legacy unlinked invoice is a **human** action,
not a backfill migration: re-saving the vendor name on `PATCH /invoices/{id}`
re-runs `vendor_matching.match_and_link_vendor`. A backfill would have had to
guess historical vendors — the precise error the guard exists to stop.

See [api-reference.md § Credit Memos](../backend/docs/api-reference.md).

---

## 10. The frontend is static, and stays static

**Decided:** initial architecture · `frontend/svelte.config.js` (adapter-static)

No SSR, no server routes, no SvelteKit endpoints. Every dynamic value comes from
the backend API. This is what lets the app deploy to GitHub Pages with no
runtime, and it makes the trust boundary trivially auditable: the browser bundle
is public, so anything secret-bearing is unambiguously the backend's job.

**Trade-off:** no server-side rendering for SEO or first paint, and the SPA
needs its own subdomain-to-tenant resolution (`src/lib/tenant.ts`) rather than
reading it server-side. Accepted — the app is behind a login; SEO is irrelevant.

**Don't re-litigate unless** a genuinely public marketing surface needs to live
in this repo. Even then, prefer a separate static site over adding an SSR
adapter.

---

## 11. Local-first: every external dependency ships a mock and a safe default

**Decided:** standing rule · root `CLAUDE.md` guard rail 7

`pnpm dev` must run on a laptop with no cloud account and no API key. Every
external integration ships (a) a local equivalent and (b) a committed default
that points at it — Postgres/Redis/MinIO via Compose, `mock` adapters for
extraction / ERP / cards / payments / FX / sanctions / audit shipping /
billing / QMS / enrichment / PEPPOL, `console` for email, Keycloak for SSO,
LocalStack for AWS, fake-erp for the three real ERP adapters.

The consequence people bump into: `backend/.env.development` and
`frontend/.env.development` are **committed**. That's deliberate — they carry
only loopback URLs, mock adapter names, and the `change-me` JWT key. Real
secrets never appear in any `.env*`.

The skeleton adapters (`c2fo`, `dun_bradstreet`, `clearbit`, `complyadvantage`,
`dowjones`, `refinitiv`, `as4_gateway`, `stripe_billing`) all **fail closed**
without a key rather than falling back to a hardcoded default — a fallback would
turn a missing credential into silently wrong data.

**Trade-off:** every new integration costs a mock adapter up front. That cost is
the feature.

---

## 12. Production secrets live in a separate private repo, not in-repo sops

**Decided:** standing rule · root `CLAUDE.md`

This repo is **public**. `bin/sops-init.sh` exists and `backend/.env.sops` /
`infra/terraform.tfvars.sops` are wired, but **no encrypted payload is
committed** and none should be. Committing ciphertext to public history is
permanent: the KMS key can be revoked, but the blob is archived, forked, and
scraped forever, and it hands an attacker an offline target.

Production secrets go to the private estate repo `Absence0760/infra-secrets`
(per-project subdirectory, its own KMS key, IAM-gated `kms:Decrypt`). This is
the pattern `meryl-green-designs` got wrong by committing `*.sops` into its own
public repo; migrating that is tracked in the estate docs.

See `~/github/project-mgmt/docs/secrets-management.md`.

---

## 13. Workflow config is frozen onto each invoice at creation

**Decided:** `backend/app/services/workflow_engine.py`

`WorkflowInstance.steps_config_snapshot` is a copy of the workflow definition
taken when the invoice enters the workflow. In-flight invoices read the
snapshot; editing a definition never affects them.

Without this, an admin tightening an approval threshold would retroactively
change the rules an invoice was already halfway through — an invoice could sit
approved under rules that no longer exist, or become un-approvable mid-flight.
For a SOX-relevant approval trail, "which rules applied to *this* invoice" has to
be answerable years later from the row itself, not reconstructed from the
definition's edit history.

The A/B testing layer depends on the same invariant: `assign_variant` freezes the
chosen variant's config onto the snapshot, so an experiment's arm can't shift
under an invoice mid-run.

**Trade-off:** a definition fix doesn't rescue already-in-flight invoices; they
need an explicit re-route. Correct — that's a decision, not a side effect.

See [workflow-design.md](../backend/docs/workflow-design.md).

---

## 14. Partner/child tenant linking requires two-sided consent

**Decided:** 2026-06-20 · migration `0065_org_parent`

A partner org can administer child tenants' branding, so "attach a child" is an
access-granting operation. The obvious implementation — the partner posts a child
org id — would let any partner adopt any tenant.

Instead: the **child's own admin** mints a single-use HMAC-signed link code
(`POST /partner/link-code`, ~30 min TTL) for its own org, and handing that code
over **is** the consent. The partner's admin redeems it. A forged, cross-key, or
already-used code is an opaque 400/409; re-parenting an org that already has a
different parent is a 409 takeover (same-partner is an idempotent no-op); a
partner cannot self-adopt. Both organizations' audit trails get a row.

`FEOH_PARTNER_LINK_SIGNING_KEY` is the single on/off knob — no key, no attach,
no fallback.

The sibling path, `POST /partner/children/provision`, takes **no parent id at
all**: it stamps `parent_org_id` from the caller, so a partner can only create a
child under itself.

See [white-label.md § Partner / reseller admin](white-label.md).

---

## 15. The report builder's security boundary is a whitelist, not sanitization

**Decided:** 2026-07-27 · migration `0071_report_definitions`

Self-serve reporting means user-supplied query shape, which is the classic SQL
injection surface. The decision is that the client **never sends a SQL fragment,
column name, or table name** — only catalog *keys*.

`report_builder.REPORT_SOURCES` is a hardcoded map from key → a real,
server-defined SQLAlchemy column, along with the aggregations, filter operators,
and date grains that key may use. `compile_spec` validates every reference
against the catalog **before any SQL is built**; anything unknown raises
`ReportValidationError` → 422 and is never compiled. Filter values bind as
parameters and coerce to the column's Python type.

The point of the ordering — validate, *then* build — is that there is no code
path where an unvalidated identifier reaches the query builder at all. Escaping
or sanitizing an attacker-supplied identifier would be a weaker guarantee for no
gain: the set of legal reports is finite and known.

**Trade-off:** adding a reportable field is a code change, not configuration.
Accepted.

See [report-builder.md § Security model](../backend/docs/report-builder.md).

---

## 16. Invoice file management freezes at `done`, not at `paid`

**Decided:** `backend/app/api/invoices.py`

`PUT`/`DELETE /api/invoices/{id}/file` refuse with 409 once the invoice reaches
`done`. `paid` stays mutable.

The ask was "terminal state", and `paid` is **not** terminal in the state machine
— `payment_scheduled` and `paid` can both void back to `approved`. Freezing the
file at `paid` would have been inconsistent with every other file-adjacent gate
in the router, and would strand a mis-scanned document on an invoice that can
still legitimately re-enter review.

The audit surface was decided at the same time and the supplier chat thread was
**rejected** for it: a file swap is an internal AP action with no reason to be
visible to the vendor. Chat is vendor-facing collaboration. Replace/delete write
`invoice.file_replaced` / `invoice.file_deleted` through the normal
`dispatch_audit` path and render in the Activity timeline like every other
correction.

---

## 17. Concurrent sessions get a git worktree, because the scope guard is path-granular

**Decided:** standing rule · `.claude/settings.json`, `.claude/hooks/git-scope-guard.py`

`git-scope-guard.py` blocks bare `git commit`, `git add -A/.`, and whole-tree
operations, forcing path-scoped commits. In a *shared* checkout that stops a
session committing files it didn't name — but it cannot separate two sessions'
edits to the **same file**, because a path-scoped commit of that file captures
whatever is in the one shared working tree.

So the rule is one worktree per concurrent session (`claude --worktree <name>`),
branched from local `HEAD` (`worktree.baseRef: "head"`, because this repo runs
ahead of an unpushed `origin`). A worktree removes the shared tree entirely,
which is the only real fix. Note that a subagent's `isolation: "worktree"`
isolates *that subagent*, not the top-level session.

The consequence that bites: a worktree commits on its own branch and git won't
let it check out `main`, so that work reaches `main` only via an explicit merge
from the primary checkout — **retiring a worktree does not merge it.** The
`SessionStart` hook `.claude/hooks/unmerged-worktree-check.sh` is the backstop.

A worktree isolates files, **not the database**. Backend `pytest` handles that
itself via per-process realdb slots; sharing the DB with a running dev backend is
still unsafe (see [known-issues.md](known-issues.md)).

---

## 18. The cash-flow copilot narrates deterministic functions; it never computes money

**Decided:** `backend/app/services/cash_flow_plan.py` · `FEOH_CASHFLOW_COPILOT_ENABLED`

The LLM's only job is turning natural language into a typed tool call and
narrating the result. **Every dollar figure comes from an existing deterministic
pure function** — `bucket_outflows`, `compute_cash_position`,
`apply_payment_timing_scenario`, `discount_optimizer.optimize` — serialized as
exact decimal strings, never floats.

That makes answers exact and reproducible under the `mock` adapter, and it means
a model swap can change the prose but not a number. It also draws the audit line
cleanly: there is no path where a generated figure reaches a screen.

`propose_payment_plan` **reuses the discount optimizer's own selection** rather
than re-deriving it, so the plan card and `optimize_discount_capture` can't
disagree. A captured discount that doesn't map cleanly onto a single commitment
row is flagged in `unretimed_offer_ids` rather than silently misrepresenting the
cash curve — surfacing the gap beats a plausible-looking wrong curve.

The hard boundary: **the copilot never moves money.** Its most privileged
intended write (Phase 3, unshipped) is staging a *draft* payment run through the
existing idempotent, CFO-gated path. Funding stays behind human review.

See [cash-flow-copilot.md](cash-flow-copilot.md).

---

## 19. The roadmap is split into open work and a shipped archive, and pruned on landing

**Decided:** 2026-08-06

`roadmap.md` had grown to 1131 lines of which roughly 95 % described work
already finished. Two consequences, both observed rather than hypothetical:

- **Answering "what's left?" required grepping** for non-`Done` statuses and
  unchecked boxes rather than reading the file.
- **Stale statuses survived unnoticed.** Expense Management still read "In
  progress (foundation shipped — WF1)" with every checkbox ticked through WF4;
  the Public Developer API and Supplier Portal entries were similarly out of
  date. Nobody was going to spot that inside a thousand lines of shipped prose.

So: `roadmap.md` keeps only sections with genuinely open work (11 of 51), each
gaining an `**Open:**` line that names what's left and links to
[followups.md](followups.md); `roadmap_shipped.md` takes the other 40
**verbatim**, under their original priority headings.

Two sub-decisions worth recording:

- **The archive is not summarized.** Compressing shipped entries would destroy
  the thing that makes them useful — the checkbox detail is how someone answers
  "does the platform already do X?" before rebuilding it. Size is not the
  problem an archive has.
- **A section moves on its *last* open item landing, not incrementally.** Ticking
  a box mid-section leaves it in the open file. That keeps a section's scope
  readable as one unit and makes the move a single, reviewable diff.

The split criterion is deliberately mechanical: a section stays open **iff** it
has an item in `followups.md`. That makes the two files verify each other — a
section with an `**Open:**` line but no follow-up entry (or the reverse) is a
detectable inconsistency rather than a matter of taste.

**Trade-off:** a priority heading that contains both open and shipped sections
is duplicated across the two files, and moving a section is a manual step the
`doc-hygiene-checker` agent has to remember to flag. Accepted — both are cheaper
than a roadmap nobody trusts.

**Don't re-litigate unless** the open file starts accumulating shipped sections
again, which would mean the prune-on-landing rule isn't being followed — fix the
rule's enforcement, not the split.

---

## 20. A write commits before its response is sent, not from the dependency's teardown

**Decided:** 2026-08-06 · `backend/app/database.py::commit_before_response`

Every session in this app came from a `Depends(yield)` provider that committed
in its post-`yield` teardown. FastAPI unwinds that teardown from an
`AsyncExitStack` it only exits **after** `await response(scope, receive, send)`
— so the client already held its `201` for a write that had not been made
durable. Measured on the real app (instrumenting `AsyncSession.commit` and the
outermost ASGI `send`): both the tenant and control commits landed *after*
`http.response.start`.

For an AP ledger that is a durability defect, not a test annoyance: the API
acknowledged money-relevant writes it had not committed.

**The fix.** FastAPI unwinds a *second*, inner stack (`fastapi_function_astack`)
**before** sending. `commit_before_response` registers the success-path commit
there, so it lands on the correct side of the response. The two session
providers call it; the post-`yield` commit stays as a conditional backstop
(`if session.in_transaction()`), which covers writes made after the response
starts (a streaming body) and any request where the hook could not register.

**Why not the two options the original write-up proposed:**

- *An explicit commit in every mutating handler* touches dozens of files, and
  every future route has to remember. A durability rule enforced by memory is
  not enforced.
- *An ASGI middleware committing before forwarding `http.response.start`* is the
  common workaround, but it needs its own session registry, has to special-case
  requests that never touch the DB, requests using both sessions, and streaming
  responses — and this app already runs a `BaseHTTPMiddleware` whose own task
  semantics interact with send-wrapping. More moving parts for the same result.

A custom `APIRoute` class was also rejected: it only applies to routers that opt
in, so router #66 constructed with a plain `APIRouter` would silently reinstate
the bug. **Silent** reintroduction is worse than the loud alternative below.

**Trade-off:** `fastapi_function_astack` is a FastAPI internal. Mitigated two
ways — the helper degrades to the *old* (correct, merely racy) behaviour if the
key ever disappears, so the failure mode is never a lost write; and
`tests/test_commit_before_response.py` asserts the key still exists, so a
FastAPI upgrade that renames it fails loudly with instructions rather than
silently regressing.

**Testing note worth keeping.** The documented network repro (rapid
create-then-read pairs) did **not** reproduce over loopback even while the
defect was measurably present — server and middleware pacing decide whether a
client can observe it. So the regression tests pin the **ordering** invariant
directly (commit precedes `http.response.start`) rather than racing a live
server. Ordering is the invariant; a lost race is only one symptom of breaking
it, and an in-process ASGI transport can never observe that symptom at all.

The `realdb.client()` harness previously overrode both providers with the old
late-commit bodies, which is exactly why the suite never caught this. The
overrides now mirror the real providers — they exist to swap the *engine*, not
the commit semantics.

---

## 21. CI guards for the three things tests can't fail on

**Decided:** 2026-08-06 · adopted from `project-running`

Three classes of regression are invisible to this repo's test suites, because in
each case the code *works* — it is only wrong later, or wrong for someone else:

- **Bundle weight.** The frontend is adapter-static on GitHub Pages, so every
  kilobyte is paid by every cold visit. No test fails when a dependency doubles
  the payload.
- **Compliance drift.** A migration adds a column holding personal data. The
  DSAR export never learns to include it; erasure never learns to redact it.
  Every test passes. It is wrong on the day someone exercises a data-subject
  right — the worst possible moment to discover it.
- **Env isolation.** This repo is public and deliberately commits
  `*.env.development` so a clone runs with no setup. That is safe exactly as
  long as those files stay boring. gitleaks catches known credential *formats*;
  it does not catch a dev default quietly re-pointed at a production endpoint.

Each now has a workflow. Three choices inside them worth recording:

**Ceilings carry a change log, not just a number.** `web-bundle-budget.yml`
requires a dated entry — measurement plus reason — for every bump. A bare number
tells the next person nothing, so they either raise it reflexively or refuse to
touch it. The log makes "ship the feature, raise the ceiling" a legitimate,
auditable move.

**The compliance detector ships in `warn` mode.** It is heuristic; a heuristic
guard that blocks merges gets switched off, and a switched-off guard protects
nothing. It runs advisory with a documented flip to `fail`. Before landing it I
measured the noise: **0 findings across 726 changed files** of real history,
while still firing correctly on a genuine PII-adding migration. Its own tests
weight the *negative* cases — the ones asserting it stays quiet when the
companion update is present — for the same reason.

**The env guard never prints what it matched.** It reports file, line, and rule
only. A CI log is as public as the repo, so a guard that echoed the credential
it just found would publish the leak it exists to prevent.

**Trade-off:** three more workflows on every PR, and the compliance detector is
pattern-matching that will need its column list extended as the schema grows. A
stale list degrades to silence rather than to false alarms — the failure mode is
"missed a finding", not "blocked a PR", which is the right way round for an
advisory guard.

**Deliberately not adopted:** `project-running`'s `check_production_env` release
guard, which refuses to *build* a release against placeholder endpoints. That
repo ships mobile/watch binaries where a bad endpoint is unrecoverable after
store submission; here the frontend redeploys in minutes and the backend reads
its config at runtime from sops. The guard would cost more than it protects.

## 22. A one-shot money-path sweep owes its failures a row and an exit

**Decided:** 2026-08-15

`services/payment_erp_sync` is the only code path that flips an invoice
`payment_scheduled → paid`. It is dispatched exactly twice, both fire-and-forget
onto a detached thread after a terminal event (run execute, payment webhook),
and **nothing re-invokes it** for a payment that is already `completed` — the
reconciler backstop only re-dispatches payments it moves *out* of
`submitted`/`processing`.

That makes it unlike every other background sweep in this repo. A tick of
`contract_renewal` or `vendor_rescreen` that fails is retried on the next tick;
a leg of this one that fails is permanent. The money has moved, the invoice
never advances, the ERP is never told, and the invoice's aging and 1099 YTD
totals are wrong from then on. Its only trace was a `logger.warning` carrying an
exception class name and a per-run counter that died with the thread.

Three calls behind the fix:

**Reuse `erp_reconciliation`, don't mint a type.** A failed leg means "the ERP
and our ledger disagree and a human must reconcile" — precisely what
`api/erp_webhook` already raises that type for when the ERP reports a void on an
invoice we've advanced past. A new type would have needed a roster entry, a
queue label, a payment-blocking decision, and would have split one situation
across two names in the queue an AP manager works.

**Commit per leg, not per run.** The loop ran every payment in one transaction
with a single commit at the end. A leg failing with a *DB* error poisoned that
transaction, so the final commit raised, the outer handler rolled back, and the
run's successful transitions were discarded too — a strictly worse outcome than
the failure itself, and equally silent. There is no cross-leg invariant to keep
atomic (each payment discharges its own invoice), so per-leg commits cost
nothing and remove the cascade. Each leg re-reads its own rows by id, because
after a rollback the ORM objects the next leg would hold are expired, and
touching one from async SQLAlchemy is a `MissingGreenlet`, not a clean failure.

**The retry endpoint awaits the pass instead of dispatching the thread.** The
two production call sites are fire-and-forget because they run at the tail of a
request that must not block on the ERP. A human clicking "retry" on a strand
wants the *answer*, so `POST /api/payments/runs/{run_id}/sync-erp` awaits
`_sync_payments` and returns its real per-leg counts. Same code path, so the two
can't diverge.

That synchronous retry is also what turned the missing row lock from theoretical
into real, and forced two follow-on details. The pass now takes the invoice
`FOR UPDATE` before the status check, like every other status transition here —
a manual retry can overlap the background thread a webhook just dispatched for
the same run, and two unlocked readers would both see `payment_scheduled` and
both transition, writing a duplicate audit row and a duplicate "invoice paid"
notification (which, unlike the outbound-webhook emit keyed on the invoice id,
has no dedupe). And the result grew a second counter: `synced` counts legs whose
ERP-facing work completed, which stays true for a settled payment whose invoice
is already `paid`, so it can't answer the operator's actual question.
`transitioned` does. Redefining `synced` was rejected because once the real
`adapter.post_payment()` lands, re-pushing an already-`paid` invoice's payment is
still work done.

**Deliberately not done:** auto-resolving the exception when the retry succeeds.
`erp_reconciliation` is shared with the ERP-void path, so closing "the open one"
could silently clear an unrelated reconciliation — the same reasoning
`POST /api/payments/{id}/settlement/accept` records for `fraud_flag`. Also not
done: making `/void` an exit. The money moved; voiding returns the invoice to
`approved`, where it invites a second payment. That the settlement *hold* has
accept-or-void as its two exits and this state has neither is exactly why the
retry endpoint had to exist.

**Trade-off:** a commit per payment instead of one per run — more round-trips on
a background thread, in exchange for never discarding a settled payment's
recorded state. And a repeatedly-failing leg now writes one exception row per
invoice rather than none; the de-dupe on an already-open/escalated row is what
keeps a retry loop from flooding the queue.

---

## 23. A stale list response is discarded, never merged onto a local edit

**Decided:** 2026-08-15

The list surfaces (`/invoices`, `/vendors`, `/payments`) fetch a page and
replace the whole array. Some of them also edit one row *in place* with no
fetch — the invoice modal's save / file attach, and on `/vendors` a
re-screen, a risk recompute, a block/unblock or an enrichment apply. (Not a
bank-detail save: that one stages a dual-control change request and applies
nothing locally.) Both things can be happening at once: a mount fetch, a
debounced search or a filter-chip fetch is still in flight when the user
approves the invoice they have open.
If that fetch resolves afterwards it holds a snapshot the server took
*before* the edit, and putting it into state silently reverts the edit — the
user watches their approval undo itself. `createRequestSequencer` closed the
fetch-vs-fetch half of this, but a local edit issues no request, so its
counter never moved and the in-flight fetch stayed "latest".

`supersedeInFlight()` closes it: every request issued up to the moment of the
edit becomes un-committable. Two calls worth recording:

**The superseded response is dropped, not merged.** The tempting alternative
is to re-apply the local edit on top of the arriving response — "newest data
plus my change". It isn't sound. The response is a whole-row snapshot from
before the edit, so overlaying the edited fields still publishes stale values
for every *other* field on that row, and for a row the server changed for an
unrelated reason in the same window. Dropping it leaves the list showing
pre-fetch data plus the edit — consistent, if briefly behind — and the next
sequenced fetch (a filter change, the modal close, a reload) reconciles. In
practice every call site mutates from an open modal, so the user cannot have
changed a filter concurrently; nothing they asked for is lost.

**"Can I commit this?" and "am I the newest request?" became two questions.**
They used to be one predicate, `isLatest`. A local edit makes them diverge: the
in-flight fetch is still the newest *request* (nothing newer was issued) but
must no longer commit. The `finally` that clears the `loading` flag has to read
the request question — `isCurrentRequest` — or the spinner stays on forever
after a local edit, with no later request coming to clear it. The vendors
load-error toast reads it for the same reason: a superseded request still
failed, and only a newer *fetch* makes its failure someone else's to report.

**Trade-off:** a fetch issued in the same window as the edit is dropped even
though it might have included the edit, costing one redundant round trip's
worth of freshness. The alternative — assuming it is fresh enough — is exactly
the bug. Conservative is correct here: the cost of dropping a good response is
a slightly stale list; the cost of committing a stale one is losing a
financial edit the user believes they made.

**Not adopted:** tracking per-field pending patches (a mutation overlay, à la
an optimistic-update cache). It solves the merge case properly but needs a
patch log, invalidation rules, and a rollback path on a failed mutation — a
lot of machinery for surfaces whose mutations already happen behind a modal.

---

## 24. A background sweep's failure count becomes state, in one shared runner

All fourteen long-lived sweeps started in `main.lifespan` carried a private copy
of the same loop, and every copy discarded the result its `*_once()` returned.
Twelve of those results already carried a `failures: int`. Nothing read it: its
only consumer was a conditional aggregate `logger.info` inside `*_once` itself.
There was no supervision either — `asyncio.create_task` with no
`add_done_callback` — so a sweep whose loop died was gone for the life of the
process with nothing anywhere saying so, and `GET /api/health` returned a static
`ok` that answered a different question. An `audit_shipping` sink misconfigured
for months looked exactly like one running clean.

Four calls are worth recording.

**The mechanism is a shared loop runner, not a shared reporting call.** The
tempting smaller change is to leave fourteen loops in place and add one
`record(...)` line to each. That preserves the thing the follow-up complained
about: fourteen bodies free to drift. `sweep_health.run_sweep_loop` takes the
tick and owns the whole body, so the outcome is recorded *by construction* — a
sweep cannot forget. The loops keep their own logger and their own
`settings.<interval>` read, so per-sweep log filters and the suites that patch
`<module>.settings` are unaffected; only the body is shared, never the identity.

Making them consistent surfaced a defect the deduplication then fixed once.
`payment_reconciler` carried a comment explaining that `exc_info=True` leaks
`str(exc)` — the stdlib appends the whole traceback regardless of what the
format string names — and had removed it *for itself alone*. Six sibling loops
still passed it and two called `logger.exception`, all of them one adapter error
away from putting a vendor name in the log sink. One body, one posture: class
name only.

**A tick that completes reporting failures is a failed run.** Modelling only
"did the tick raise" would have left the motivating case invisible: the
`audit_shipping` adapters raise *per tenant*, `ship_once` catches that, counts
it, and returns normally. So `failures > 0` (and `vendor_rescreen`'s separate
`vendor_failures`) increments the same consecutive-failure streak a raise does.
The streak, not the absolute count, is the signal — a sweep that fails once and
recovers is noise; one that has failed 37 times running is the months-long
misconfiguration.

**The state is per-process and in-memory, and that is the answer, not a
compromise.** A durable per-sweep run table means an Alembic migration fanned
out to every tenant DB to hold telemetry that is platform-level, not
tenant-level; `Organization.settings` is the only JSON marker available and it
is per-tenant, while these sweeps span all tenants. Both were rejected. The
question an operator actually asks — "is *this* replica's sweep alive and
progressing?" — is exactly what a per-process registry answers, and the
cluster-wide view is the log sink, where every replica emits the same PII-free
`NOT MAKING PROGRESS` line on each streak multiple (a multiple, not every tick,
so a 60-second sweep stuck for a day writes 8 lines rather than 1440).

**`GET /api/health` was left alone, and the new endpoint hides tenant counts.**
Folding sweep health into the liveness probe would turn a misconfigured audit
sink into a rolling restart loop — a degraded background sweep is not a reason
to pull a serving process out of rotation. So the probe stays public and static,
and the operator view is a separate admin-gated `GET /api/health/sweeps`. That
endpoint reports state, timestamps, outcome, streak and the exception CLASS, but
**not** the sweeps' raw counters: an ordinary tenant admin holds `ROLE_ADMIN`,
and `tenants_scanned` would tell them how many organizations the platform
sweeps. Only `last_failure_count` crosses the boundary — the number an operator
acts on, and zero on a healthy platform. The full counters stay in the registry
and the logs, whose reader is already trusted with them.

---

## 25. Sequencing is per list, and a bubble is addressed by identity

Applying §23 across the rest of the app — eighteen list surfaces, plus the two
chat pages — forced three calls §23 didn't have to make, because it only had to
serve three surfaces that each owned exactly one list.

**One sequencer per independent list, not one per file.** Several surfaces hold
more than one list: the `admin` store (users and roles), the `notifications`
store (the list and the 60-second unread-count poll), the `expenses` page (four
tabs), the `discounts` page (offers and the KPI dashboard). A single counter
per surface is the obvious economy and it is wrong: `start()` is monotonic, so
a roles refresh, or a badge poll tick, would mark an unrelated in-flight users
fetch un-committable and blank the list it was about to paint. Each list gets
its own counter. The corollary is that a local edit writing state that *several*
of them load — a mark-read moves `unread`, which both the list load and the
badge poll return — must supersede every one of them; `notifications` has a
`supersedeReads()` for exactly that, rather than one call that looks complete
and isn't.

**`syncUrl()` is untracked wholesale, not just on `search`.** Eight pages'
filter `$effect`s transitively depended on `search` through `buildParams()` and
`syncUrl()`, so a keystroke fired an immediate load alongside the debounced one
(issue #168, fixed on three pages in 2026 and never carried to the rest). The
narrow fix is `untrack(() => search)` at each read. It was taken in
`buildParams()`, whose other reads are filters the caller genuinely depends on
and declares. It was **not** taken in `syncUrl()`, which is untracked in full:
that function is a *writer* of URL state called from the effects, never a
source of dependencies, and its `$page.url` read was already untracked for the
adjacent reason (it writes that URL via `replaceState`, so tracking it
self-triggers the effect). Stating the property once means adding a field to
`syncUrl` later can't quietly reintroduce the bug — and it also removed the
accidental cross-dependencies on `/expenses`, where a tab switch re-fired the
expenses list fetch. The cost is that a genuine dependency can no longer be
declared *inside* `syncUrl`; it has to be read in the effect, which is where a
reader looks for it anyway.

**The chat placeholder is addressed by identity, and `busy` still closes the
window.** `/assistant` let a send start while a saved thread was loading, then
wrote the model's answer into `messages[capturedIndex]` of an array the load
had meanwhile replaced — so the answer didn't vanish, it landed on an
unrelated historical message. Either fix alone would stop today's reproduction:
holding `busy` for the load closes the window, and resolving the placeholder by
a stable client-side id makes a replaced array return `null` instead of a
wrong row. Both shipped, because they fail differently — `busy` is a policy
that any new "replace the messages array" path (a delete, a rename, a
server-push) can forget to respect, while identity is a property of the write
itself. `/cash-flow`'s copilot is a copy of the same code with no
thread-opening rail yet; it got the identity half so it isn't the
index-capturing version someone copies from next.

**A write asks a third question, so the primitive grew one.** §23 split the
old single `isLatest` predicate in two because a local edit makes "may I
commit?" and "am I the newest request?" diverge. A *write* — a save that PUTs
the list and then re-reads it — turned out to need a third: only a local edit
invalidates the payload it just sent, and an unrelated newer *read* says
nothing about that. The first attempt at `InvoiceModal.saveLineItems` read
`canCommit`, which is false for either reason, so an extraction poll's own
reload landing mid-save left the dirty flag stuck on and the Save button up
over a table nobody had touched. `wasSupersededByEdit(token)` isolates the
edit half. It is deliberately a fourth method rather than a second sequencer
or a bespoke boolean in the component: the state it reads (`staleThrough`)
already exists and belongs to the primitive, and a component-local copy would
drift from the `supersedeInFlight()` that sets it.

**Not adopted:** wrapping the three-call protocol (`start` / `canCommit` /
`isCurrentRequest`) in a single `sequenced(fn)` helper. It reads better at
twenty call sites, but it has to decide the `finally` semantics for the caller,
and that is the exact distinction §23 records as easy to get backwards — a
`loading` flag cleared on `canCommit` sticks on forever after a local edit. The
three calls stay visible so the choice stays visible.

---

## 26. Platform extraction falls back to the offline reader locally, never in a deployed env

**Decided:** 2026-08-16 · `backend/app/services/extraction.py::resolve_platform_provider`

`extraction._resolve_extraction_config` hardcoded `provider: "claude_vision"`
for `program_type: "platform"` — the default for every org that sets no
`settings.extraction`, which is every seeded tenant — **regardless of whether
`FEOH_ANTHROPIC_API_KEY` was set**. So on a fresh clone every extraction, an
invoice upload and a PDF supplier statement alike, POSTed to
`api.anthropic.com` with an empty key and came back `provider_error`. The
offline `mock` reader existed precisely so `pnpm dev` could exercise the whole
path with no credential, and nothing routed to it. That broke guard rail 7 for
the one adapter family whose local equivalent was already written.

**The rule now** is the pure `resolve_platform_provider`, in precedence order:
an explicit `FEOH_EXTRACTION_PROVIDER`; else a configured platform key →
`claude_vision`; else, in a non-deployed environment, `mock`; else (keyless and
deployed) `claude_vision` anyway. The committed `backend/.env.development` also
sets `FEOH_EXTRACTION_PROVIDER=mock`, so the choice is visible in the file a
contributor reads rather than only implied by an absent key.

**The last rung is the whole point of the entry.** The obvious symmetric fix —
"no key means mock, everywhere" — is wrong here in a way that is easy to miss:
`MockExtractionAdapter.extract` returns a **fixture** ("Extracted Vendor Inc",
1500.00, a fabricated invoice number), not a read of the document. A deployed
environment that lost its key would therefore stop erroring and start booking
invented payables against real vendors, silently, at 0.95 confidence — inside
the confidence band that can auto-approve. A loud `provider_error` that parks
the invoice in `failed` is strictly better. So the fallback is gated on
`settings.is_deployed`, and a keyless deployed env keeps failing exactly as it
did before this change. `extract_statement` is not symmetric with `extract`
here and that is deliberate: mock's statement reader reads the document's real
text layer and gives up loudly when there isn't one, which is why the PDF
statement path is genuinely exercisable offline while the invoice path is only
*runnable* offline.

**Failing visibly was a requirement, not a nicety.** Both fallback rungs log a
PII-free WARNING naming the provider and the reason; the resolved config
carries `platform_provider_reason`; and the provider already travels on the
persisted result (`InvoiceExtractionResult.method`, a statement run's
`meta.extraction.provider`, surfaced in its provenance panel). A `mock` read is
labelled `mock` everywhere a human looks at it.

**Why the boot-time allowlist.** `get_extraction_adapter` falls back to `mock`
for an unrecognised provider name. Introducing an env var that names a provider
therefore introduced a new way to reach the fixture adapter by typo — in
production. `config.py::_validate_extraction_provider` refuses an unregistered
name at boot, and a drift-guard test cross-checks the literal allowlist against
the live registry, because config.py must not import the service layer.

**Not adopted:** making `mock.extract` return an empty/failed result instead of
a fixture. It would make the fallback safe everywhere, but the fixture is what
lets tests and demos produce a populated invoice with no provider — a much
wider blast radius than the resolution rule this entry is about.

---

## 27. A statement's decimal convention is decided per document, not per token

**Decided:** 2026-08-16 · `backend/app/services/vendor_statement_recon.py`

`850,00` is 850.00 in most of Europe and 85000 if the comma groups thousands.
`parse_amount` assumed the latter unconditionally (`s.replace(",", "")`), so a
European supplier statement reconciled at **a hundred times** its real value.
The text reader agreed the token was money — it read that same comma as
thousands-separator evidence — so nothing upstream caught it.

The unit that can answer is the **document**, not the token. A statement is
written in one convention throughout, so one unambiguous `1.234,56` anywhere in
it settles every bare `1.200` beside it. `detect_amount_convention` runs across
the whole amount column (CSV) or all extracted lines (PDF) before any of it is
parsed.

**Only genuinely ambiguous tokens consult the document.** A single separator
with a three-digit tail (`1,234` / `1.234`) is a thousands group under one
convention and a three-decimal value under the other — that shape, and only that
shape, takes the document's answer. Everything else is self-describing and is
read on its own terms: both separators present means the rightmost is the
decimal point; a repeated separator can only be grouping; and a one- or
two-digit tail must be the decimal point, because money carries at most two
decimal places and no grouping run is shorter than three digits. That last rule
is what makes a lone `850,00` correct with no other row present.

The asymmetry matters: a document-level vote must not override a token that says
what it is, or one malformed row would drag an entire statement onto the wrong
reading. Contradictory evidence therefore resolves to "no answer" rather than to
a majority, and the self-describing tokens still parse correctly underneath it.

**Trade-off:** with no evidence at all, the ambiguous three-digit-tail shape
keeps its historical US reading, so a European statement consisting *only* of
`1.200`-shaped amounts still reads them as 1.200. That is unchanged behaviour
rather than a new failure, and any row carrying cents fixes it for the whole
document.

**Not adopted:** per-token locale guessing (no rule distinguishes `1,234`
American from European in isolation — this is the whole difficulty), and
refusing the ambiguous shape outright (safe, but it makes every European
statement unreadable, trading a wrong number for no number on documents that
are perfectly legible once the convention is known).

**A second, quieter blocker rode along.** `_DATE_TOKEN` matched only `-` and
`/`, so `15.01.2026` read as a second *identifier*-shaped token and the
exactly-one-identifier rule refused the whole row. European statements were
therefore skipped wholesale rather than mis-read — safe, but it meant fixing the
amount alone would not have made one reconcile. Dotted dates are now recognised,
and the amount pattern pins grouping runs to exactly three digits so a date can
never be read as money.

See [vendor-statement-reconciliation.md § Decimal conventions](../backend/docs/vendor-statement-reconciliation.md).

---

## 28. Contrast is guarded per surface in the stylesheets, not per route in a browser

**Decision:** the app's colour palette carries a two-token contract — a base
token for text/icons/borders on a dark surface, and a `-strong` companion that
is the fill behind white text — and a vitest guard
(`frontend/src/lib/a11y/tokenPairing.test.ts`) scans every stylesheet in `src/`
for a pair that breaks it. The route-level axe guard
(`tests-e2e/a11y/axe.spec.ts`) stays, and keeps the half the scanner can't do.

**Why.** The axe guard only sees what a listed route happens to render. Twice
running, that shape produced the same outcome: a WCAG 1.4.3 failure was caught
on the pages inside the route list and missed, identically, on the pages
outside it. `--text-muted` on `--surface-2` was 4.34:1 *wherever it appeared*;
axe found it on `/admin/api-keys` and `/admin/webhooks`, and `/billing`'s
proration box — the same defect — was found by a human reading the file. The
bug recurs per **surface**, so the surfaces are what to check.

Running the scan the first time found 99 problems, which is the real argument
(a fourth rule, added once the first three were green, found 106 more — see
below):

- **55 contrast failures**, almost all one root cause. `--accent-strong` had
  been added specifically so white text had somewhere legible to sit, and
  almost nothing used it — 40 buttons, chips and chat bubbles still filled
  with `var(--accent)` at 3.12:1. Green (`#1fa86a`, 3.06:1) and red
  (`#e04040`, 4.22:1) had no companion at all, so pay / approve / execute /
  reject / void — the money buttons — all failed.
- **32 `var(--token, fallback)` declarations whose fallback contradicted the
  token.** Inert while the token exists, and the wrong colour the moment one is
  renamed. This is exactly how `--surface-2` shipped for months rendering a
  value nobody had declared, with two call sites disagreeing about it.
- **12 references to a token nothing ever assigns**, where the fallback is
  always what renders — including two spellings of the monospace stack.

**Scope, deliberately.** The scanner checks pairs decided **inside one rule**.
A rule that sets only `color` inherits its background through the cascade,
which is a runtime question; that stays axe's job. Neither guard subsumes the
other, and saying so here is cheaper than someone deleting one of them later.

**One sound question survives in the cascade case**, though, and asking it
found the round's largest single defect. A rule setting only a `color` will
render on *some* app surface, so a **literal** there has to be legible on the
surfaces body text actually sits on. `#e04040` — the status red — is 4.11:1 on
`--surface` and 4.47:1 on `--bg`: failing, in **106** declarations across 61
files, on error messages, alerts and the danger row-action. Only literals are
asked (a palette token is already asserted against those surfaces), `#fff` and
`#000` are exempt as the deliberate on-a-fill choices, and `--surface-2` is
*not* in the surface list — text on that raised panel declares its background,
so the pair check owns it, and including it would flag every status colour in
the app on the strength of a surface it never renders against. Decorative
fills (a chart bar, a confidence dot, an SVG `fill`) carry no text and are
untouched.

The rule also fires when the rule *does* declare a background that resolves to
nothing usable — a translucent tint, a gradient. Standing down there sounded
conservative and was the opposite: `background: rgba(140,100,240,0.15);
color: #8c64f0` is the standard dark-theme status pill, so the check would have
fallen silent precisely on the pills. A tint over an app surface composites
close to it, so the bare surface is the right approximation; twelve more
failures came out (purple, blue, amber and green pill/banner text, 3.53–4.42:1).

**A hole in the drift check itself** turned up in review and is worth
recording: the `var()` fallback was captured with `[^()]*`, which cannot cross
an inner paren, so `var(--bg, var(--surface))` — shipped on two routes —
matched nothing and was invisible to both the dead-token and the stale-fallback
check. The scan is paren-aware now, and the comparison resolves a fallback
through the palette rather than by spelling, since a token-valued fallback
would otherwise read as stale on sight.

**Fixing a failure means changing the colour.** There is no suppression
mechanism and no allowlist, because the `-strong` companions mean a correct
answer always exists. The one nuance encoded instead of waived is WCAG's own:
a rule that declares a large-text size in the same block is held to 3:1, and an
unresolvable size (`em`, `calc`, inherited) is treated as normal text — the
stricter direction.

**The palette's own contract is asserted directly**, not inferred from the
rules that happen to use it. A token drifting light would otherwise surface as
dozens of scattered failures instead of one.

**Not adopted:** widening the axe route list alone (the ask that prompted this
— it treats the symptom, and the list will trail the app again the next time a
route is added); a linter plugin (a colour comparison needs the resolved token
values, which is the part a generic CSS lint doesn't have); and blocking a
tenant's brand colour. On that last one — `brandThemeVars` writes a tenant's
`accent_strong_color` straight into `--accent-strong`, so a brand colour can
defeat the whole contract at runtime where no static scan can see it. The
backend accepts any valid hex and the brand is the tenant's call, so the
answer is an advisory: `accentStrongContrast` reuses the same WCAG primitive
and both surfaces that edit that colour — the org Branding panel and the
partner child-branding modal — show the real ratio before it is saved.

**The scan also models a rule's own `opacity`, and deliberately stops there.**
Adding `/cfo` to the axe list caught `.kpi-sub` — `--text-muted` under
`opacity: .85` — at 4.24:1, which *both* static checks had structurally missed:
the same-rule pair check compares the colours as declared, and the bare-literal
check exempts a palette token on the reasoning that this contract already
vouches for it. Opacity is exactly what invalidates that reasoning, because it
composites text and its background together down onto the backdrop. So a rule
that fades itself is now measured as it renders, and the eleven instances that
found were all the same shape: an `opacity` line fading text a token had
*already* muted. Every fix was deleting the line — the fade was never carrying
meaning, only cost.

An **ancestor's** opacity stays out of scope, and that is the boundary, not an
omission: resolving it means resolving the cascade, which is the half axe owns.
`.status-pill.revoked` on `/admin/api-keys` is the worked example — a row fade
dragged an already-muted pill to 2.44:1, and only a browser could see it. The
fix spares the status cell from the fade, because the one cell explaining why a
row is faded should not be the least readable thing in it.

Translucent *backgrounds* are the same compositing problem and are measurable
with the same primitives, but 29 badges sit 4.15–4.48:1 — failing by a hair,
needing a design call rather than a mechanical edit. Arming that half before
fixing them would ship a red build, so it is tracked with its measurements in
[followups.md](followups.md) instead.

See `frontend/docs/ui-patterns.md` § Colour tokens and contrast, and
[accessibility.md](accessibility.md).

---

## 29. A mis-typed provider name never resolves to the fixture adapter

**Decided:** 2026-08-16 · `backend/app/services/{payment_adapters,erp_adapters,fx_adapters}/dispatcher.py`

All three dispatchers resolved an unrecognised provider name to their `mock`
adapter. Each had written down why — "prevents a missed config from silently
500-ing the entire payments domain", "so a typo'd config doesn't blow up
sync-erp", "fails closed in prod because the mock returns a fixed rate that
will not match real market" — and each was reasoning about a `mock` that does
not exist. These fixture adapters are not inert stubs; they are the thing that
makes `pnpm dev` work with no cloud account, so they answer **yes** to
everything:

| Family | What `mock` does | What a typo'd `settings.*` produced |
|---|---|---|
| payments | `create_payment` → `success=True, completed` | every payment in every run reported as settled, invoices flipped to `paid`, no money moved |
| payments | `parse_webhook` verifies no signature | the public webhook route reached an unverified parser, under a name the `provider == "mock"` early-return cannot catch |
| payments | `void_payment` → `True` unconditionally | a `voided_upstream` audit row for a rail nobody asked |
| ERP | `post_invoice` → `success=True` + a `MOCK-…` id | the invoice walked `sending_to_erp → sent_to_erp → done` with an ERP reference pointing at nothing |
| ERP | `test_connection` → `True` | `POST /organization/test-erp` answered "Connected to `<typo>` successfully" — the endpoint that exists to catch the misconfiguration confirmed it |
| FX | `get_rate` → a hardcoded table | `prepare_international_payment` **locked** the fabricated rate onto `Payment.fx_rate` / `source_amount`, never re-fetched, driving the real outflow and later `realized_fx_gain_loss_for_settlement` |

**The rule now:** no configured provider still means `mock` — that is the
local-first default (guard rail 7) and an org that has configured nothing is a
normal state. A **named** provider we have no adapter for raises
(`UnknownPaymentProviderError` / `UnknownErpAdapterError` /
`UnknownFxProviderError`).

**This is §26's call, one layer down.** There the same fallback let an
unrecognised `FEOH_EXTRACTION_PROVIDER` reach a fixture adapter, and the fix
was a boot-time allowlist. That doesn't transfer: these names come from
per-org `Organization.settings`, not process env, so there is no boot at which
to check them. The refusal has to live at the dispatcher, and — because a
dispatcher cannot know whether its caller is moving money or drawing a chart —
**each caller decides what the refusal means**:

- **Refuse, before any state changes.** Run execute / resume / retry-failed and
  the compliance release resolve through `_require_payment_adapter` *before*
  claiming the run, so the answer is a 409 with the run still `draft` rather
  than a 500 with it stranded `executing`. The three ERP sync endpoints 400 —
  a config problem, not the 502 they use for a gateway failure.
- **Fail the one payment.** The international leg records
  `failure_reason="fx_provider_unsupported"` instead of booking a rate. The
  reason names the condition and NOT the admin's raw settings value, because
  every AP user reads `failure_reason` while only an admin owns the setting.
- **Degrade.** `fetch_provider_balance` falls back to the manual opening
  balance; the CFO dashboard's unrealized-FX panel reports `available: false`;
  the corridor auction skips just that provider so one bad name in a
  multi-provider list can't take the whole auction down.
- **Record and continue.** `/void` still voids locally — the books should
  reflect intent — but writes `provider_not_supported` rather than a
  fictitious `voided_upstream`.
- **Count it as a failure.** The payment reconciler lets it propagate so the
  tenant registers as a sweep failure and shows `degraded` on
  `GET /api/health/sweeps` (§24).
- **Fail the leg, not the pass.** `payment_erp_sync` resolves the ERP adapter
  *inside* `_sync_one_leg`, where it would be used, so an unsupported type
  travels the same path as any other leg failure and opens the de-duped
  `erp_reconciliation` exception §22 introduced. The first attempt at this put
  the check in `_sync_payments` as a pre-flight, before the tenant session
  exists — which cannot open an exception, aborts every payment in the run at
  once, and returns a count that `_run_in_thread` discards on the primary
  dispatch path. That reintroduced exactly the invisible strand §22 removed:
  money moved, invoices frozen at `payment_scheduled`, nothing in the queue.
  A config error must strand the same way a transport error does.
- **Say which name is wrong.** `POST /organization/test-payments` and
  `/test-erp` echo the bad value and list the registered alternatives. The name
  is bounded to 50 chars (its column width) so an absurd value can't bloat a
  log line, and no credential from the posted config is echoed.

**Why not make the mock adapters inert instead.** §26 rejected the same idea
for extraction and the reason holds here: the fixtures are what let tests and
demos exercise a whole money path with no processor. Neutering them has a far
wider blast radius than the resolution rule this entry is about.

**Not adopted:** validating `settings.payments.provider` on write in
`PATCH /api/organization`. Worth doing, but it is not sufficient on its own —
settings predate any validator, arrive from seeds and migrations, and an
adapter can be *removed* from the registry after a name was already stored.
The dispatcher is the only chokepoint every caller passes through.

See `backend/docs/payments.md` § Provider resolution.

## 30. A translucent tint is composited, not approximated — and each tone names its own text

§28 armed a bare-literal `color:` check against the surfaces body text sits on,
and deliberately let it also fire when a rule *did* declare a background that
resolved to nothing usable — a gradient, or a translucent tint. Its stated
reasoning for the tint half: "a tint over an app surface composites close to
it, so the bare surface is the right approximation."

**That approximation was wrong in the unsafe direction, and this entry
supersedes it.** A tint does not composite *close to* the surface; it moves the
surface *toward the colour of the tint*, which in the status-badge recipe is
the same hue as the text on top of it. Every such badge therefore renders with
less contrast than the bare surface predicts, so the check passed them. 29 did,
between 4.15:1 and 4.48:1 — failing by a hair, which is why none was caught by
eye either. Accent text on its own 15% tint measures 5.55:1 against the bare
surface and renders at 4.48:1.

**Composite it instead.** The primitives were already there and unused on this
path (`parseColorWithAlpha`, `compositeOver` — §28 added them for the `opacity`
case). A translucent background now resolves *with* its alpha, is composited
over each backdrop, and the rule's own opacity applies on top. Both compositing
causes became one calculation rather than two checks that could disagree.

**The fix is a third token role, not 29 new hexes.** The palette already paired
a base token (text on a dark surface) with a `-strong` companion (the fill
behind white text). Badges get `--<tone>-tint` and the `--<tone>-on-tint` text
calibrated on it, for five tones. The values are not new: `StatusBadge` had
solved this for its own six tones by lifting the text rather than darkening the
tint, and those hexes are what the tokens promote out of one component's
private stylesheet.

- **Why not per-hue alphas.** Cheapest and most fragile. Accent passed only at
  alpha ≤ .14 and sat at .15 — one hundredth — so every future nudge silently
  re-breaks it, and nothing states the rule.
- **Why not opaque tint tokens.** It would move these rules under the existing
  same-rule pair check and need no compositing at all. Rejected because an
  opaque tint bakes in ONE backdrop, and badges sit on both `--bg` and
  `--surface`; and because the guard has to composite anyway to catch the next
  hand-rolled `rgba()`, so the simpler check would have bought nothing.
- **`--danger-on-tint` equals `--danger`.** Red is bright enough to clear its
  own tint unaided. The token is declared anyway so the rule stays "tint
  background, matching `-on-tint` text" with no exception to memorise, and so
  red can be recalibrated later without hunting the sites that spelled it the
  other way.

**The guard ships after the sites, not with them.** An armed guard with 29
known failures is a red build, not a guard — so the tokens landed, then the
badges moved onto them, then the compositing was switched on.

**What this did not fix.** ~208 rules still spell a tinted badge as a
hand-rolled `rgba()` plus a literal hex — about 40 spellings of the five tones
now named. All of them pass, so that is design-system debt rather than a
defect, and normalising it shifts tint strength across the app. Tracked in
[followups.md](followups.md).

**A regression the move caused, recorded because the shape recurs.** Folding
two hand-picked oranges onto one warning tone made `/payments`'
`pending_compliance` identical to `pending` — the invisibility that status was
made first-class to end. Sharing the tone is right (both are waiting); the
distinction moved to a ring. When a normalisation collapses two colours, check
what those colours were silently carrying.

See `frontend/docs/ui-patterns.md` § Colour tokens and contrast.

---

## 31. A canonical helper with no caller is a bug that has usually already happened

Eight built, tested, documented capabilities had no production caller
([followups.md](followups.md) § Backend capabilities with no production caller).
The tempting read is that they are spare capacity awaiting a consumer. Closing
all eight in one round showed the opposite: in three cases the reason nothing
called the helper is that somebody had already re-implemented it inline, and the
copies had drifted into live defects.

- `analytics.compute_dpo_trend` was re-derived twice in `api/analytics.py`, and
  the copies disagreed about whether `rejected` invoices belong in the COGS
  proxy — so `/api/analytics/drill/dpo` reported **3.0 days where the chart it
  exists to explain showed 30.0**. Same failure shape as issue #126.
- `workflow_engine.is_known_step_type` was written to be the shared gate and
  never wired, so `POST /api/workflows/import` — the one save path a Pydantic
  `Literal` does not constrain — persisted a typo'd `"aproval"` that the engine
  then silently skipped. An unrecognised step type is not a degraded step; it is
  an *absent* one, and the absent one is usually the approval.
- `international_payments.is_international_payment` was dead while three modules
  hand-rolled its rail set. Unifying them surfaced that
  `compliance._kyc_required_for` compared a raw per-org override against a
  lower-case `Payment.method`, so an admin entry of `"SEPA"` disabled the KYC
  gate for that corridor — and a blank `[""]` produced a truthy set that
  disabled it for **every** corridor. A fail-open on a regulatory control,
  invisible because the code path that would have prevented it was the unused
  one.

The rule this yields: when a helper exists and nothing calls it, look for the
inline copy before assuming the feature was never finished — and treat the
divergence between them as the actual finding.

## 32. A step type we don't recognise is refused by name, never silently ignored

`validate_builder_steps` previously passed over any type outside the five
builder types, reasoning that canonical steps were not its concern. But the
engine reads `steps_config` by type *name*, so an unrecognised type is absent
rather than degraded — a typo reads as "no approval step configured" and the
workflow quietly loses a financial control. Failing the import loudly is
strictly safer than persisting a config whose runtime meaning is "one fewer
gate". Same posture as §29.

Two follow-on calls, both deliberate:

**`create_workflow_step` refuses a builder step type rather than inventing a
number for it.** `WorkflowStep.step_number` is the canonical pipeline's 1-based
index, and `complete_current_step` finds the open step by ordering on it. A
builder step is orchestration config with no place in that ordering, so
`canonical_step_index` raises `NonCanonicalStepTypeError` instead of allocating
a number that would corrupt the queue. Both new errors subclass `ValueError`, so
any handler written against the old bare `.index()` still catches.

**`/drill/dpo` serializes money as exact decimal strings while the rest of
`api/analytics.py` stays float.** That inconsistency is chosen, not overlooked:
the module has ~57 more float money fields feeding shipped frontend and mobile
consumers (`analytics.ts` types them `number`; `CfoMetrics.svelte` calls
`.toFixed()`), so converting them is a separate client-breaking migration,
tracked in followups. `/drill/dpo` has no shipped consumer, so it could be
corrected in isolation and was. `dpo` itself stays a JSON number — it is a day
count, not money. Rejected: leaving `/drill/dpo` on float for consistency with
its neighbours; consistency with a violated invariant is not a reason to keep
violating it.

## 33. A card that cannot be handed a signature signs itself

The Teams approval card's buttons are MessageCard `HttpPOST` actions dispatched
by Microsoft, not by us — which is the whole problem. A Slack interactivity POST
arrives signed by Slack's infrastructure and a Teams *Outgoing Webhook* POST
arrives signed with the shared security token, but an actionable-message action
arrives with whatever we put in its `headers`. There is no handshake to ride on,
which is why the inbound half shipped, was security-reviewed, and then sat
unreachable.

Three options. Point the buttons at the existing email-approval confirm page
(`OpenUri` + an `email`-channel token): safe, but it routes around the shipped
Teams endpoint entirely and breaks the channel binding that keeps each surface's
tokens non-interchangeable. Register a Bot Framework app and use
`Action.Execute`: a different auth model (Bot Framework JWT validation), not an
increment on the shared-secret gate already reviewed. Or sign the action
ourselves.

We sign it ourselves. We control the action's exact `body` string, so the card is
stamped at render time with `HMAC-SHA256(security token, body)` and the endpoint
re-derives it over the raw bytes. What that digest is matters less than what it
is **not**: it is not a key, and it is body-bound, so an approver handed the
Reject action cannot upgrade it to Approve, and publishing one valid
`(body, digest)` pair yields nothing about the token. What it proves is that the
POST replays a body the platform minted — which is exactly the job of keeping
blind probes off a public endpoint. Anyone who can read the digest already holds
the action token sitting in plaintext in the same JSON, and re-firing it is
collapsed to a no-op by the single-use `jti`. So the exposure is unchanged from
the accepted Slack precedent: any channel member can click.

Two consequences that look like oversights and are not:

- **The digest rides two headers.** Teams may replace an actionable message's
  `Authorization` with its own bearer token, stripping our only proof, so the
  card also carries the digest on `X-Feoh-Card-Signature`. The endpoint collects
  every candidate and accepts if any verifies — not weaker (each must reproduce
  the HMAC of the exact body) but robust to a proxy folding duplicate
  `Authorization` values into one string, which would otherwise mask a good card
  signature behind a mangled one.
- **The card emits no timestamp header.** It is stamped at *render* time, so the
  ±5-minute replay window would kill the buttons five minutes after the card was
  posted. Replay stays bounded by the single-use `jti` and the workflow state
  machine — the posture the endpoint already documents for a timestamp-less POST.

Rejected alongside: minting the token on the `email` channel so the card could
reuse the confirm page. That would make a Teams card's credential redeemable at
the email endpoint, dissolving the channel claim that exists precisely to keep
one surface's token off another.

## 34. An attestation nobody made reports "unknown", not the default

Two verdicts landed this round that could have defaulted to the reassuring
answer, and both deliberately do not.

**Data residency.** `check_residency_alignment` compares a tenant's pinned region
against `FEOH_DEPLOYED_REGION`. The obvious shape — default the deployed region
to `DEFAULT_REGION` and return a plain `bool` — was rejected: it makes the
*absence* of a declaration indistinguishable from a verified match, and hands an
EU-pinned tenant `aligned: true` on the strength of nobody having said
otherwise. That is the precise failure a residency control exists to prevent,
and it fails in the reassuring direction, which is the one nobody investigates.
So the verdict is tri-state (`aligned` / `misaligned` / `unknown`) with
`aligned: bool | None`, and `None` only ever pairs with `unknown`; a `reason`
(`deployed_region_unset` / `deployed_region_unrecognised`) makes the unknown
actionable rather than merely honest. An *unrecognised* token reports `unknown`
rather than comparing literally, because one typo (`eu-central-1` for `eu`)
would otherwise mark every tenant misaligned and bury the real ones. Boot-time
validation was also rejected: `config.py` refuses to start on a bad
`FEOH_EXTRACTION_PROVIDER` because that value silently turns a deployed pipeline
into a fixture generator, but this one changes nothing except a report, so
refusing to start over it trades a wrong answer for an outage. Advisory
end-to-end — nothing routes, blocks, or moves data on the verdict.

**Adverse media.** The sanctions taxonomy (`sanctions` / `pep` /
`adverse_media` / `high_risk_country`) shipped on `ScreeningResult.categories`
and was dropped by all three consumers, so negative-news coverage that has not
yet reached a formal list reached nothing that acts. It now rides the row's
existing JSONB under a reserved key — not a new column: the payload is a small
fixed enum list and the only read pattern is "latest row per vendor", so a
column would fan a schema change out to every tenant DB to store it. The merge
never mutates the provider's own payload (a `clear` screen's payload stays
byte-identical, so an auditor replays exactly what was returned) and reads are
tolerant, because a screening-trail row must never 500 the risk endpoint. The
labels are *our* fixed vocabulary rather than provider free text, which is what
makes them safe on an audit row, in an API response and in a UI badge —
`raw_response` itself is still never serialized out (invariant #7). And **a
`clear` verdict carrying adverse media becomes a `hold`**: no shipped adapter
produces that combination today, so it changes no live flow, but auto-allowing
it is exactly the gap the taxonomy was added to close.

**Mileage, by contrast, is advisory — and that is the same principle, not an
exception to it.** `expense_policy.BLOCKING_CODES` holds exactly two codes, both
*missing evidence the submitter can supply*, so the 422 is actionable: attach
the thing and resubmit. A `mileage_amount_mismatch` is a disagreement between
two numbers already supplied, and which one is wrong is a human judgement (the
rate changed mid-period, the line bundles a toll, the claim is deliberately
*under* entitlement). Blocking would strand a legitimate claim with no in-app
override and would refuse a submission for an under-claim that costs nothing.
The approver is the control point, and the violation carries the expected figure
plus its working so the badge says what to pay. Fail *informative*, not falsely
reassuring — and not falsely obstructive either.

## 35. A figure we cannot establish is excluded and counted, never estimated at face value

Rounds 10 and 11 found the same defect five times, in five unrelated modules,
and fixed it the same way each time. Recording it once so the sixth is caught in
review rather than in production.

The shape is always this: `Payment.amount` is denominated in the **invoice's**
currency — `international_payments.prepare_international_payment` sets
`amount=invoice.amount` and puts the home-currency debit on
`source_amount`/`source_currency`. Any aggregate that sums raw `Payment.amount`
across a book containing one foreign invoice is therefore a silent two-currency
mixture, and every one of these labelled that mixture with the org's reporting
currency. The 1099 report summed it onto a **filed tax form** (a EUR 1 000
invoice paid on a USD 1 100 wire reported `ytd_paid: "1000.00", currency:
"USD"`, which can also push a vendor across the $600 threshold); vendor risk
scoring fed it to a ramp whose full-exposure point is a bare `100000`, pinning a
¥10 000 000 payer at the maximum sub-score; the discount optimizer added a EUR
1 000 offer to a USD 1 000 one and checked the sum against a single-currency
cash budget; the cash-position curve produced a **−9 751 000** closing balance;
the reporting-currency lock kept a stale figure and reported it as *converted*.

**The rejected fix in every case was a face-value fallback** — treat 1 000 EUR
as 1 000 USD when no rate is available, optionally flagging it. That is what
`reporting_amount_for_row` does for a spend dashboard, and there it is right: an
approximate total with a caveat beats a blank panel. It is wrong the moment the
number is *filed, gates a control, or is compared against a threshold*, because
the caveat rides a different field from the number and only the number gets
read. One of these had already shipped the caveat — `_commitment_rows` computed
an `unconverted` flag whose docstring promised it made foreign rows "visible
rather than silent", and grep found **zero readers**. A flag nobody reads is not
a mitigation.

**Fetching a rate at read time was rejected too.** It makes a historical total
move under the reader, which §18 already settled for locked-not-recomputed
rates, and it puts an FX outage on the path of a tax report.

So `currency_conversion.payment_reporting_amount_sql` resolves two rungs, most
authoritative first — the rate-locked `source_amount` when its currency IS the
reporting currency, else `amount` when the invoice's own currency is. There is
deliberately no third rung. What neither rung establishes is **excluded from the
total and counted separately** (`unconverted_payment_count`,
`unconverted_payments`, `unconverted_count`, `unconvertible`), the count is
surfaced on the API, the `/cfo` card and the copilot tools, and an affected
vendor is flagged `needs_attention`. A single-currency tenant only ever reaches
rung 2, so its numbers are byte-identical — which is why this was invisible for
so long.

The general rule, beyond currency: **when a value cannot be established, say so
in the result and leave it out of the arithmetic.** Do not substitute a
plausible stand-in and record the doubt somewhere the consumer does not look.
Same instinct as §34 — the reassuring default is the one nobody investigates.

---

## 36. An unresolvable sanctions provider holds the payment; it does not screen against `mock`

**Decided:** 2026-08-19 · `backend/app/services/sanctions_adapters/dispatcher.py`

`get_sanctions_adapter` resolved `_REGISTRY.get(provider) or _REGISTRY["mock"]`,
so a typo'd `Organization.settings.compliance.sanctions.provider` —
`"worldcheck"` for the registry's `refinitiv`, say — screened an entire tenant's
vendor book against the mock's three-entry fixture list and returned `clear`
with risk 0. The dispatcher's own docstring asserted that "the compliance
service surfaces a warning in its result so this misconfiguration is visible to
the AP team". No such code existed: `services/compliance` never inspected
`adapter.provider_name`, and `services/vendor_screening` recorded the mock's
verdict unexamined. The stated mitigation was never built, on the control that
exists to keep money away from a sanctioned party.

A **named** unknown provider now raises `UnknownSanctionsProviderError`. An
absent or empty provider still resolves to `mock` — that is the local-first
default, and a fresh clone must screen vendors with no cloud account. This is
the same call already made for `erp_adapters`, `payment_adapters`, `fx_adapters`
and `financing_adapters` (§29): the mock is never an inert stub, so substituting
it converts a configuration error into a silent, confident wrong answer.

Rejected: raising all the way out to the caller. A 500 on the vendor-create path
would break vendor management over a compliance misconfiguration, and a 500 on
the payment path tells an operator nothing about which control failed. Both
consumers absorb the raise instead, each in the direction that fails closed.
`compliance.check_payment_compliance` returns `hold` with the reason `sanctions
screening could not run: no adapter for configured provider '<name>'`, so the
payment waits in `pending_compliance` and the caller opens the usual
`payment_compliance_hold` exception — the misconfiguration reaches the AP queue
rather than the payment rail. `vendor_screening.screen_vendor_record` writes a
`sanctions_checks` row with `provider="unconfigured"`, `result="review_required"`
(the requested name rides `raw_response`, PII-free) and denormalises
`vendors.screening_status="review"`, so the vendor lands on the review queue
instead of reading `clear`. No payment block is set — a misconfiguration is not
a match.

## 37. A webhook's uniform ack covers decisions, not our own failures

**Decided:** 2026-08-19 · `backend/app/api/email_intake.py`, `backend/app/api/erp_webhook.py`

Every inbound webhook returns the same opaque response on every rejection path,
so the response cannot enumerate tenant slugs or bearer tokens. For
`email_intake` that posture is unusually load-bearing: the HMAC signing secret is
platform-wide (the email provider has no notion of tenants), so anyone who can
sign a request could otherwise grind candidate per-tenant intake tokens and watch
for the response to change. The uniform `200 {"status": "received"}` is what
closes that.

It was applied too widely. `services/email_intake.process_inbound_email` releases
its Redis dedup claim and re-raises on any downstream failure — S3 unreachable,
tenant DB down, Redis flapping — commenting that this "lets the NEXT delivery of
the same message_id actually retry the work". The route then caught that re-raise
and acked `200`, which tells SES / Mailgun the message *was* delivered. There was
no next delivery. The release-on-failure code was preparing for a retry that
could never arrive, and the vendor's invoice was gone behind a log line.
`api/erp_webhook.py` had the identical shape for ERP status transitions;
`api/billing_webhook.py` faced the same choice and had already gone the other way.

The distinction drawn is **decision vs. our own failure**. A decision is a final
answer about this message — unknown or disabled intake token, duplicate delivery,
no usable attachment, unknown tenant / status / invoice, a transition the state
machine forbids, genuine success. Those keep the uniform ack, because varying
them is exactly the oracle. A failure of ours is not an answer at all: the
message is still unprocessed work, and the only correct response is to ask for it
again. Those now return a **bodyless 5xx** (`_retry_please()` → `503`) in both
modules.

The residual exposure is accepted deliberately. A 5xx on our own failure narrows
the token-enumeration oracle to "while the platform is already broken" — an
attacker learns only that something inside us threw, never whose token they
guessed, and only during an outage. Losing every invoice that arrives during a
blip is the larger harm by a wide margin. The response is bodyless, so it still
carries no detail, no stack trace and no tenant.

Rejected: making every path a 5xx (an ERP would then retry forever on an event we
have correctly and permanently refused — the mirror-image failure); and leaving
it alone with a louder log (the log was already there, and it had not made anyone
whole).

## 38. Scheduled reports ship, with a CRUD surface, tenant- but not entity-scoped

**Decided:** 2026-08-19 · `backend/app/api/scheduled_reports.py`

`services/scheduled_reports.py` had a complete, tested runner and no input
surface: nothing under `app/api/` referenced the `ScheduledReport` model, so a
row could only be created by hand-written SQL, `list_due_schedules` returned `[]`
on every tick forever, and the documented 5-strike auto-disable was a one-way
door. The choice was ship the surface or delete the feature. We shipped it —
recurring emailed CFO reports are a table-stakes AP capability and the machinery
was already sound.

Three calls inside that:

*Admin-only to mutate, admin + CFO to read.* A schedule is a standing instruction
to email a CSV of the tenant's AP spend to an arbitrary address on a recurring
basis, with no review of any individual send. That is a data-egress control, not
a reporting preference, so it sits above the gate the rest of `/analytics` uses.
Reads stay open to the CFO, who owns the reports and needs to audit what is going
out.

*Validation against the runner's own registries, never restated copies.*
`report_type` is checked against `report_export.EXPORTERS` and `cadence` against
`scheduled_reports.known_cadences()`. Both matter operationally: a `report_type`
outside the exporter registry raises on every tick and burns through the
auto-disable without ever sending, and an unknown cadence silently falls back to
daily — so a "yearly" row would have emailed 365 times a year. Importing the
registries means adding a report type or a cadence updates the API for free.

*Tenant-scoped, deliberately not entity-scoped.* `ScheduledReport` carries no
`entity_id`, and the runner's `_materialise_rows` applies no entity filter to any
of its six report types — the emailed CSV is whole-tenant by construction.
Stamping an entity on the schedule row would advertise a scope the delivered file
does not honour, which is worse than not offering it at all. Making it real means
entity-filtering the materializer *and* a migration; that is its own slice.
Tenant isolation itself is enforced normally, through `get_tenant_db`.

Audit rows carry the recipient **count**, never the addresses: the trail is
append-only and WORM-shipped, so a corrected distribution list could never be
redacted out of it. And `PATCH {enabled: true}` clears the stale `[retry N]`
marker — `_mark_failure` reads that prefix to count consecutive failures, so
re-enabling without clearing it means the next failure lands at retry 6 and
disables the row again immediately, which an operator cannot distinguish from the
re-enable not having worked.

## 39. `/api/v1/docs` is a self-hosted reference, not Swagger UI

**Decided:** 2026-08-19 · `backend/app/api/v1_openapi.py`

`public_docs` returned FastAPI's `get_swagger_ui_html`, whose only stylesheet,
script and favicon are third-party CDN URLs. `main.SecurityHeadersMiddleware`
stamps `Content-Security-Policy: default-src 'none'` on every response, so the
route returned `200`, fetched nothing, and rendered blank in any browser honouring
the header.

Three exits were available. **Allowlisting the CDN** in a route-scoped CSP is
three lines, but it gives a page the platform itself serves a third-party runtime
dependency, and it still renders blank offline — breaking guard rail 7's
local-first rule. **Dropping the route** and pointing at the spec URL is honest,
but leaves a 404 on a URL our own docs advertise. **Vendoring `swagger-ui-dist`**
keeps the CSP strict and works offline, but commits roughly a megabyte of
third-party JavaScript to a public repo along with a version nobody will remember
to bump — a supply-chain artifact acquired for a reference page.

We took the third exit at its minimum. `render_docs_html` renders the same
published document server-side as self-contained HTML: no script at all, inline
or sourced, and no external asset of any kind. The route sets its own CSP —
`default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri
'none'` — which differs from the global policy by exactly one token, the page's
own inline stylesheet, which can neither execute nor exfiltrate. The **global**
policy is untouched: it is what keeps the API origin unable to load third-party
script at all, and relaxing it for one page relaxes it for every JSON response.
`SecurityHeadersMiddleware` uses `setdefault`, so a route-set header wins with no
middleware change.

The trade-off, stated plainly: this is a reference, not an interactive console —
there is no "Try it out". The machine-readable contract at
`/api/v1/openapi.json` is what integrators actually consume, and it feeds any
client generator or Swagger / Redoc instance they already run. `render_docs_html`
is a pure function of the spec, so the page cannot drift from the routes.

## 40. Semantic duplicate detection stays cross-entity; the warning it writes does not

**Decided:** 2026-08-19 · `backend/app/services/duplicate_detection.py`

`find_semantic_duplicates` queried `invoice_embeddings` with no entity filter
while every sibling reader is scoped — `rag.retrieve_similar` takes an
`entity_id`, the assistant's `find_invoices_by_text` threads it "so 'search'
can't silently widen past the subsidiary the user has selected", and
`vendor_matching._candidate_query` scopes for the same reason. Two consequences
followed: the resulting `duplicate` Exception is payment-blocking, so a
cross-entity false positive holds subsidiary A's payment run; and
`matches_to_warning` wrote the matched invoice's `invoice_number`, `vendor_name`
and `amount` into `invoice.warnings`, which the detail modal renders and whose
`message` is copied verbatim into the Exception description — data from an entity
the viewer is otherwise scoped away from.

Scoping the search would have been the mechanical fix and it is the wrong one.
The same invoice billed to two subsidiaries of one group is a *real* duplicate
and precisely what a group AP team wants caught; silently scoping removes a
genuine control in order to fix a disclosure problem. So the search stays
tenant-wide and `entity_id` **classifies rather than filters**: an outer join to
`invoices` tags each match `cross_entity`, and a cross-entity match is reported as
existence only — "a near-identical invoice exists under another entity", with
`invoice_id`, `invoice_number`, `vendor_name` and `amount` all nulled. The id goes
too: an entity-scoped `GET` would 404 on it anyway, so surfacing it buys nothing
and is one more identifier crossing the boundary.

A NULL `entity_id` on either side means *unstamped* — a pre-multi-entity row, or a
tenant that never used entities — not "some other entity". Treating unknown as
cross-entity would redact the useful detail for every single-entity tenant, the
overwhelming majority, to protect a boundary that does not exist for them. When
both a same-entity and a cross-entity match exist, the headline is built from the
same-entity one (the viewer can act on it and needs the identifiers) with a bare
count of the others appended.

The Exception stays payment-blocking on a cross-entity hit. That is intended: a
suspected intra-group double-bill is exactly the case that wants human sign-off.

## 41. A run's status is derived from its payments, not read from the column

**Decided:** 2026-08-19 · `backend/app/services/payment_runs.py`

`PaymentRun.status` had exactly one writer after execution: the final rollup in
`_dispatch_run_payments`. Neither the processor webhook, the reconciler backstop,
nor `/compliance/{release,dismiss}` touched it. A run that rolled up `submitted`
because one payment was held `pending_compliance`, and then had that payment
dismissed, kept reporting `submitted` while its own payments said `failed` —
`/retry-failed` 409'd on `RETRYABLE_RUN_STATUSES`, and `/execute` and `/resume`
409'd on the claim states. A dead end, and precisely the "button that can't act"
the `retryable_failures` field exists to prevent.

Two mechanisms were on the table. Recompute-and-persist at every site that
changes a payment's status is the obvious one, and it is what a schema-first
instinct reaches for; it was rejected as the *primary* mechanism because it is a
completeness obligation on a set that grows — every future path that moves a
payment must remember to call it, and the failure mode of forgetting is silent
and identical to the bug being fixed. Deriving on read cannot be forgotten:
`derive_run_status(persisted, rollup)` is the single rule, and the runs list, the
run detail and the retry gate all route through it, so what an operator sees and
what an endpoint gates on cannot diverge by construction.

The split is between *claim* states and *outcome* states. `draft`, `executing`
and `cancelled` say something about who holds the run, not about how its payments
turned out, and `/execute` / `/resume` gate on exactly those — re-deriving them
would let a rollup un-claim a run mid-dispatch. They pass through untouched.
`submitted`, `partial`, `failed` and `completed` are claims about outcomes, and
outcomes are what the payment rows already record, so those are recomputed.

`recompute_run_status` was kept as a secondary, belt-and-braces write at the three
endpoints that move a payment outside the dispatch pass. Not because the reads
need it — they don't — but because a stored column that lies is a trap for
anything reading the database directly: an operator at `psql`, a CSV export, a
future consumer written by someone who reasonably assumes the column means what
it says.

The rollup's own default was fixed in the same pass. It returned `completed`
whenever nothing was completed, failed or in flight — a fail-open answer on a
money-run status, which made a run with every payment still `pending` (nothing
attempted) and a run with no payments at all both report success without a cent
moving. All-pending is the resumable state and now reports `executing`; no
payments at all reports `draft`.

That first pass left the *default itself* fail-open — `completed` was still the
fallthrough for any payment status no bucket named. A later pass closed it: a
`voided` payment (a human reversing one after the fact) joined
`RUN_PAYMENT_FAILED_STATUSES` beside `cancelled`, and the final rung now returns
`completed` **only** when every active payment completed — otherwise `partial` /
`failed`. So a run of all-voided payments reports `failed`, not `completed` with
`payments_completed: 0`, and a future adapter status can't slip through as
success either.

## 42. The corridor-quote optimizer got a caller, but not the one that routes money

**Decided:** 2026-08-19 · `backend/app/services/corridor_quotes.py`, `backend/app/api/payments.py`

`compare_quotes` shipped fully built, documented and unit-tested with nothing
calling it. Unreachable code on the money path is worse than absent code: it
passes review, accrues no test pressure from real callers, and hands whoever
wires it up every untested assumption at once. Two very different things were
being called "wiring it up", and conflating them is why it sat unwired for so
long.

The first is letting the auction *decide* which rail moves the money — pick the
cheapest bid at dispatch time and send the payment there. That is a treasury
policy decision, not a defect fix. Which bank moves a customer's money is a
question about banking relationships, settlement risk, reporting and existing
contracts; the fact that one adapter reports a lower fee is an input to it, not
an answer. A bug-hunting pass does not get to make that call, and making it
silently — inside `_execute_single_payment`, where the change would be small —
would be the worst version of making it.

The second is letting a *human* see the comparison. That needs no policy call at
all, and it is what shipped: `POST /api/payments/corridor-quotes`, read-only,
advisory, booking nothing. The response carries `advisory: true` so a client
cannot mistake it for a routing decision, and `payment_corridor.pick_corridor`
remains the sole authority over the rail on a `Payment` row.

This is the general shape for "built but unreachable" capabilities: find the
largest slice that is safe to reach without deciding anything nobody has decided,
wire that, and leave the policy question explicitly open rather than answering it
by omission. The alternative — deleting the module as dead code — would have
thrown away the correct part along with the undecided part.

The same test was applied to `services/financing_adapters` and it failed:
supply-chain financing has **no safe read-only half**, because a financing quote
is only meaningful if it can be accepted, and accepting it moves money to a
supplier from a third-party financier. That one stays unwired, deliberately.

## 43. The commitment set takes one schedule per invoice, and `id` is only a determinism tiebreak

**Decided:** 2026-08-19 · `backend/app/api/analytics.py`

`_commitment_rows`' `outerjoin(PaymentSchedule)` fanned an invoice out to one row
per schedule, double-counting it at full amount across the forecast, the
cash-position curve, the what-if, every copilot tool and the `plan_id` hash
simultaneously. Dedup takes the **latest** schedule via `DISTINCT ON (invoice_id)
… ORDER BY invoice_id, created_at DESC, id DESC`, matching
`discount_auto_trigger._resolve_due_date`'s existing `ORDER BY created_at DESC
LIMIT 1` on the same table so the discount engine and the cash forecast cannot
disagree about an invoice's due date.

`created_at` defaults to `transaction_timestamp()`, which is constant within a
transaction, so two schedules written in one commit carry the *same* timestamp and
"latest" is genuinely undefined between them. The `id` tiebreak exists solely to
make that case **deterministic** — a copilot `plan_id` is a hash of its resolved
inputs and must not flap between reads — not to assert an order the data does not
carry.

Rejected: summing every schedule's terms (a schedule is a restatement, not a
tranche) and adding a real sequence column (a migration for a column no writer
populates today).

## 44. A conformance claim is emitted only when the conformance check passes

**Decided:** 2026-08-19 · `backend/app/services/e_invoice/bis3.py`

`cbc:CustomizationID` / `cbc:ProfileID` are not decoration: together they assert
that a UBL invoice conforms to EN 16931 as profiled by PEPPOL BIS Billing 3.0. An
Access Point routes on them and validates the payload against that profile.
Emitting them on a document that does not meet the profile is worse than emitting
nothing — it converts a document the receiver could have read as plain UBL into
one it is obliged to reject, and it makes our own logs claim a compliance posture
we do not have.

The send path was already declaring BIS 3.0 on the AS4 envelope for documents
with no `cbc:EndpointID` on either party, tax subtotals with no amounts, and lines
with no VAT category. So the document was brought up to the profile *and* the
claim made conditional on it: `generate_ubl` declares the identifiers only when
`bis3_conformance_errors(doc)` is empty, and `peppol_send` refuses to transmit a
document it can itself disprove.

Rejected: adding the two strings unconditionally — the cheap change, and the one
that makes the lie load-bearing; and gating on nothing while leaving the AS4
envelope claiming the profile — the status quo, which only fails at a real Access
Point, in production, on a customer's invoice.

The check is honest about its own limits. It is a mandatory-element pass over the
rules the normalized model can answer, not the official Schematron; a document
that passes may still fail the real validator, but one that **fails** provably
does not conform, and that asymmetry is exactly what makes a conditional
declaration sound.

## 45. CFDI states `ObjetoImp="03"`, not `"01"`, when a taxed line's rate is not establishable

**Decided:** 2026-08-19 · `backend/app/services/e_invoice/country_formats/cfdi.py`

SAT's `c_ObjetoImp` values are claims about the line, and its validation rules key
off them: `"02"` (subject to tax) *requires* the per-line `cfdi:Traslado`; `"01"`
(not subject to tax) and `"03"` (subject, not obliged to break it down) forbid it.
The generator stamped `"02"` on every line and emitted no Traslado at all.

The obvious repair — fall back to `"01"` when the breakdown cannot be built —
trades one false claim for another: a line carrying a tax amount is not *no objeto
de impuesto*. So the mapping is three-way: `"02"` + a complete Traslado when
`Base` and a rate are establishable, `"03"` when the line is taxed but the rate is
not (a multi-rate document, or a tax amount with no rate anywhere), `"01"` only
for a genuinely untaxed line.

Related: a zero rate is emitted as *tasa cero* (`TipoFactor="Tasa"`,
`TasaOCuota="0.000000"`, `Importe="0.00"`) and never as `"Exento"` — the two are
different claims and the normalized model cannot distinguish them.

## 46. Outbound notification legs run after the caller's commit, not inside it

**Decided:** 2026-08-19 · `backend/app/services/post_commit.py`

`transition_invoice` awaited the whole notification fan-out — one email per
recipient, serially, then a Slack / Teams POST with a 10-second `httpx` timeout —
while the caller's transaction was still open. `payment_erp_sync._sync_one_payment`
takes the invoice `SELECT … FOR UPDATE` and only commits after the transition
returns; `review.approve_invoice` holds `FOR UPDATE` on the `WorkflowInstance`. A
hung chat webhook therefore held a row lock on a live invoice for its full
timeout, and N recipients multiplied the email leg linearly.

`services/post_commit` queues the transports on the caller's *session* and fires
them from SQLAlchemy's `after_commit`, which runs after the DB has committed — so
every lock is already released, and no call site had to change. The in-app
`Notification` rows deliberately stay in the caller's transaction: they are DB
writes and should commit atomically with the status change.

Rejected: parallelising the email leg with a bounded `gather` (shrinks N×T to T
but leaves the lock held for T, so it treats the symptom); and threading an
explicit post-commit step through all ~35 `transition_invoice` call sites
(invasive, and one missed call site silently reverts to the old behaviour).

Accepted consequence, and the right one: **a transaction that rolls back now sends
nothing** — we no longer email people about a status change that never happened.
Under a `dispatch_engine_scope` the jobs are awaited inline via `await_only`
rather than spawned, because that worker's loop closes the moment the job returns
and would abandon a task mid-send; inline still costs no lock, only latency on a
background worker.

## 47. One component owns the tinted badge; a caller names a tone, not a colour

**Decided:** 2026-08-19 · `frontend/src/lib/components/ui/Badge.svelte`

202 CSS rules hand-rolled the tinted-badge recipe as an `rgba()` tint plus a
literal hex — 44 distinct spellings of five tones. Every one passed the contrast
guard, so this was design-system debt rather than a defect; but the same tone
written four ways is precisely how the 29 sub-4.5:1 badges accumulated unnoticed
(§30). A shared primitive is the structural fix that stops the next 29.

`Badge.svelte` is now the single owner: a caller names a **tone** and cannot
spell it wrong. `variant` passes the caller's semantic class through as a
**selector hook only** — the e2e suite reads `.badge.approved` — and never as
colour, so styling and test-targeting stop being the same mechanism.

Sizing is fixed rather than exposed as a prop. Call sites varied padding by a
pixel with no discernible intent, and one size is most of the point of a shared
primitive; a pill that genuinely needs different metrics is a different
component, not a prop. `ScreeningBadge` keeps its dense inline metrics but takes
the palette tokens, which is the shape that concession should have.

Two tones stay deliberately non-tinted. `neutral` is a flat `--bg` chip standing
for the *absence* of a signal, and tinting it would give "nothing to report" the
visual weight of a status. `erp` is a measured purple literal that shares no
semantics with the other five.

**Rejected: converting all 125 badge-shaped rules in one pass.** The tokens
standardise on alpha `.15`, so normalising a `.1` or `.12` rule visibly
*strengthens* that badge — a real visual change, not a refactor. Landing it
wholesale would make any subsequent visual complaint unattributable to a
specific tranche. Half moved in the first tranche; the rest follows in
attributable batches, with collapsed distinctions checked each time (two were
verified here: recurring's `paused` / `ended` greys, and three punch-out ambers).

## 48. A counts endpoint takes every list filter except the one it is tallying

**Decided:** 2026-08-19 · `backend/app/api/purchase_orders.py`, `backend/app/api/vendors.py`

`GET /api/vendors/counts` exists because a status chip that counts only the
current page undercounts the moment a tenant has more rows than fit — the
Unverified attention badge was the original case. `GET /api/purchase-orders/counts`
is the same fix for the same reason, and copies it deliberately rather than
inventing a variant.

The non-obvious part is which filters a counts endpoint accepts. It takes
`search` and every *narrowing* filter the list takes — the tallies have to
describe the same population the list would render, or the chips disagree with
the table under them. It deliberately does **not** take `status`, because status
is the dimension being tallied: applying it would return the selected status'
count and zero for every other chip, which is exactly the "chip that lies"
failure the endpoint exists to prevent.

RBAC matches the list endpoint exactly rather than being tightened. A counts call
that 403s where the list 200s produces a page that renders rows above chips that
cannot explain them, which is a worse failure than the count simply being
unavailable — and the frontend already latches an unavailable count back to
pre-counts behaviour.

**Amended 2026-09-04 — the contract is now enforced, and the RBAC clause runs
both ways.** All three parts of the above are asserted by
`backend/tests/test_whole_set_kpi_rollups.py`, which discovers `/counts`
surfaces off the mounted OpenAPI schema (and each handler off its generated
`operationId`) so a new one is covered without editing the file. Stating the
contract was not enough: three violations were found the day the guard was
written — the payments History chips tallying the whole entity while the list
was searched, the vendors chips hand-rolling a predicate set that had already
diverged on `source`, and the purchase-order pair running two copies of the
same predicates that disagreed on a malformed `vendor_id` (a 400 from one, a
500 from the other).

The RBAC clause is checked in **both** directions, which the original entry did
not spell out. It argued the tighter case (a tally a reader of the rows cannot
reach). The looser case is worse and had actually shipped:
`GET /api/vendors/change-requests/counts` admitted `ROLE_CFO` while its queue
does not, so a role deliberately excluded from the dual-control bank-change
review could still read how many redirects were staged. A tally reachable by
more callers than the rows it counts discloses the size of a set they cannot
see — for a fraud-review queue that is the whole point of the exclusion.

One exemption is legitimate and is recorded in the guard rather than assumed: a
list offering **no** narrowing filter has no predicate set for a shared builder
to protect. The change-request queue is the only such surface today, and the
exemption fails if that queue ever gains a real filter.

## 49. A blocked row names the exception type, never the exception's description

**Decided:** 2026-08-19 · `backend/app/api/payments.py`, `backend/app/services/payment_runs.py`

`GET /api/payments/queue` offered rows that `POST /api/payments/runs` then hard
409'd, because run creation refuses any invoice carrying an unresolved exception
in `PAYMENT_BLOCKING_EXCEPTION_TYPES`. The queue now marks each row `blocked` with
a `blocked_reason`.

Two calls in that. First, the predicate is **not restated**: a new
`blocking_exception_types()` is the single source, and `blocked_invoice_ids` was
redefined in terms of it, so the queue and the gate cannot drift and a new
blocking type reaches both for free. That mattered immediately —
`payment_reconciliation` was added to the tuple earlier in this same branch (§41's
aged-out-payment fix) and required no second edit.

Second, `blocked_reason` is the exception **type** from a fixed vocabulary, never
the exception's `description`. A description is free text assembled from invoice
and vendor data; this field crosses a JSON boundary to an operator's browser, so
shipping it would put vendor names and amounts into a payload whose whole purpose
is "this row is not actionable". The type is enough to explain the block, and a
user who needs the detail opens the exception where the normal scoping applies.

Where an invoice carries several blocking exceptions the earliest tuple member is
reported, so the reason is deterministic rather than dependent on row order —
a flapping reason string on a payment surface reads as a bug in the gate.

## 50. The email-shape check has one owner, and it ends in `\Z`

Three copies of `^[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+$` lived in `api/signup.py`,
`api/partner.py` and `app/schemas/scheduled_report.py`. Three copies of a
validation rule drift, and this one gates **who receives a tenant's AP data by
email**. It is now `app/utils/emails.py::looks_like_email`, with a test that
pins the admitted cases so a later tightening has to edit a failing test rather
than silently narrow who can be mailed.

Hoisting it found a live hole. All three ended in `$`, which in Python matches
end-of-string **or just before a trailing newline** — so `"user@example.com\n"`
passed every check and was stored as a login, a child-tenant admin address and a
scheduled-report recipient. A newline reaching an SMTP header is the
header-injection primitive. The shared pattern anchors with `\Z`.

Rejected: pulling in `email-validator`. RFC-strict syntax buys rigour nobody
wants here (quoted local parts, address literals) and still answers nothing about
deliverability, which the confirmation round trip already answers. The
permissiveness is deliberate; the trailing newline was not.

## 51. "Today" is UTC across the backend, and the guard scans four spellings

`date.today()` resolves in the **server's** local zone. Latent on a UTC container
— the deployed shape — and live on any host that is not. Every site under
`backend/app/` now goes through `app/utils/dates.py::utc_today()`.

The sites that mattered were not the stamps. `select_tier_for_date(as_of=…)`
decides whether an early-pay discount is still claimable, and the buyer's view,
the supplier's portal view and the auto-capture sweep read it from two different
clocks — so for part of each day the three could disagree about the same offer.
The recurring-template `generate-now` path derives the period key that **is**
`uq_invoice_recurring_period`, so a manual generate near midnight resolved a
different period and produced a second payable. `Invoice.approval_date` is a
regulated SOX field on all three approval paths.

Rejected: converting only the money-comparison sites and leaving stamps and
filenames local. Each stamp sits beside a `datetime.now(UTC)` in the same
artifact, so a split makes one document disagree with itself —
`export_aging_snapshot` was already labelling a CSV with one date while its
buckets meant another.

Rejected: a `TZ=`-manipulating behavioural test. `date.today()` reads the real
clock, so such an assertion is only true for part of the day. The AST guard is
the right layer — but it had to be fixed first: it matched only `ast.Name`, so
the attribute-shaped `datetime.date.today()` was invisible, and that is exactly
what both Positive Pay modules used. Either could have been certified "converged"
while still reading local time. The scanner now also catches naive
`datetime.now().date()`, which is local and is not spelled `today` at all.

## 52. A caller-specific emphasis is a wrapper, not a sixth badge tone

`pending_compliance` shares the `warning` tone with `pending` on purpose — both
are waiting — and the **ring** is what says "a human must clear this". Converting
those pills to the shared primitive (§47) threatened to flatten a distinction the
code explicitly documents. It survives as a non-inset `box-shadow` on a
caller-owned wrapper, which also takes no layout space and so removes the
misalignment the old `inset` shadow was itself working around.

Rejected: a `ring` / `emphasis` prop on `Badge` — a one-caller prop is exactly
what §47's fixed-sizing argument refuses. Rejected: a `:global(.badge.…)` rule —
this repo has zero `:global` in routes or components.

The same conversion exposed the inverse of §47's warning. The tokens standardise
on alpha `.15`, and that "visibly strengthens" a badge converted from `.1` — but
it also **weakens anything nested inside it**. `--text-muted` sub-labels within a
tinted chip (`.card-meta`, `.discount-pct`) fall to 4.34:1 and 4.11:1 purely
because their parent got the standard tint. Neither guard would catch it:
`cssAudit` deliberately resolves no cascade, and axe only sees whichever tab
renders. They now take `color: inherit`.

## 53. A list search never filters the rows already loaded

`/requisitions` and `/expenses` filtered client-side over the loaded page, with
an honest empty state explaining that a match further in was not visible. The
copy was good; the limitation it described was the bug. The term now goes to the
server, and the filter and its empty state are gone — the table, the footer count,
Load-more and the KPIs finally describe the same set.

Rejected: keeping a client filter alongside the server one, which would leave two
divergent notions of "matching" on one page.

The fix reintroduced issue #168 and had to be caught: `load()` is called
**synchronously** from the status-filter `$effect`, and Svelte tracks reads
transitively through called functions — so reading `search` there made the effect
depend on the term. Every keystroke fired an immediate request, and because the
loader stamps the applied term before the debounce timer fires, the debounce then
short-circuited and the un-debounced request was the only survivor. Typing five
characters cost six requests. That is the second time this has landed on these
pages through a different function (`syncUrl` first, now the loader), so the rule
is recorded as a property of the **call site**, not of one function: anything an
effect calls synchronously is inside its tracking scope. A `fill()`-based e2e is
structurally blind to it — one state write, one term — so the guard has to type.

## 54. Persisting a cash plan does not weaken the stateless `plan_id`; it is keyed by it

§5/§6 gave plans no table deliberately: `plan_id` is a pure hash of resolved
inputs plus the calendar date, and enactment re-derives everything. That still
holds — nothing on the enact path reads `cash_plans`, and `payment_runs.plan_id`
is still the draft-run anchor. What a stored row adds is the one thing
re-derivation cannot supply: **what the projection said on the day it was made**.
Yesterday's plan is not recomputable — the horizon starts elsewhere and the
invoices have moved — so a variance with no baseline is just a restatement of the
present.

Rejected: upsert-on-save. Restating a snapshot against newer data rewrites the
very number the comparison measures against, so a repeat save returns the
existing row untouched (`created: false`, 200), with `uq_cash_plans_org_plan_id`
holding that under a concurrent retry.

Two rules keep the variance honest. Only **fully-elapsed** periods are scored: a
period whose window has not closed has a partial actual, and subtracting a whole
projection from it manufactures an underspend that reverses by week's end. Open
periods are still returned and labelled, never silently zeroed — and pro-rating
was rejected, since it invents a within-period spend distribution the plan never
asserted. And a `completed` payment with no `completed_at` is **counted on
`undated_payment_count`, not dated by a proxy**; its `created_at` bounds whether
it is in scope, never which period it lands in.

## 55. Consolidated scope is discovered from the plan id, not declared by the client

`plan_id` already hashes the `entity_id` it was built under, so exactly two ids
can be legitimate for a caller: their selected entity's, and the consolidated
one. `_resolve_and_verify_plan` tries both, most specific first.

Rejected: a `consolidated: bool` on the replay body. The plan card is rendered
from a tool result that carries no entity, so the flag would be a claim we would
have to trust, and it would need threading through `CopilotChatMessage`.

Discovery widens nothing. Entity scoping is a **view** scope — `get_entity_id`
validates the header against the tenant's own `entities` table and grants no
access by itself — so the consolidated id is equally reachable by omitting
`X-Entity-ID`, and a tampered parameter still matches neither candidate.

One consequence had to be followed through: a consolidated draft run previously
took its entity from `get_write_entity_id`, which would stamp a cross-entity run
with whichever subsidiary happened to be selected. The run entity now derives
from the plan's own scope — the selected entity for a scoped plan, the tenant's
default for a consolidated one — matching what an entity-less
`POST /api/payments/runs` already does.

---

## 56. The fixture-adapter fallback is gone from every remaining registry

**Decided:** 2026-08-21 · `backend/app/services/{card_adapters,positive_pay_adapters,enrichment_adapters}/dispatcher.py`

§29 removed "a named unknown provider resolves to `mock`" from payments, ERP and
FX; §36 from sanctions. Three registries still had it, and in each the fixture is
exactly the confident-wrong-answer §29 describes:

| Family | What the fallback returned | What a typo'd config produced |
|---|---|---|
| cards | `create_card` → `success=True` + `mock_card_…` / `4242`; `get_card_details` → the PAN `4242424242424242`; `cancel_card` → `True` | every issuance "succeeded"; the run's card leg marked payments `completed` and invoices `payment_scheduled`; vendors were emailed reveal links to a fixture PAN |
| positive pay | the `csv` formatter, under the requested `bank_format` | a CSV file the bank cannot parse, stored + audited + idempotency-slotted as the requested layout — so a fraud control the tenant believed was in force was not |
| enrichment | fabricated firmographics (legal name / address / DUNS / employee count) with `matched=True` | an invented identity presented as a D&B lookup, one click from `POST .../apply` writing it onto a real supplier's *screened* `name` |

Same rule, same shape: **no configured provider still means the default**
(local-first, guard rail 7); a **named** one we have no adapter for raises. And,
as §29 established, each caller decides what the refusal means — the card table
is in `backend/docs/virtual-cards.md` § Provider resolution.

One addition beyond §29's pattern: `get_card_adapter` now imports its own
built-in adapters. A refusal is only trustworthy if every adapter has had a
chance to register, and relying on each call site's `# noqa` import preamble made
that a property of the caller rather than of the dispatcher.

---

## 57. The two billing meters are tenant tables; `assistant_usage` is not

**Decided:** 2026-08-21 · `backend/app/services/extraction.py`, `backend/docs/database.md`

`extraction_usage` and `card_rebates` read like control-plane data — they meter
the platform's own billing, which is keyed by org — and three docstrings plus
`docs/database.md` said they were. They are not, and never have been: neither is
in `tenant_provisioning.CONTROL_TABLES`, no Alembic revision creates them, and
`scripts/seed.py::create_control_tables` filters to the control set. They exist
only where `provision_tenant` puts them, per tenant, which is also where
`services/billing`'s `rollup_usage` reads them.

`services/extraction.py` was written against the claim rather than the schema and
committed the meter through a control-plane session. `to_regclass` is NULL there,
so the INSERT raised `UndefinedTableError` *inside* `run_extraction`'s own `try` —
its handler rolled the tenant transaction back, opened an `extraction_failed`
exception and transitioned the invoice to `failed`. A successful extraction was
recorded as a failure, and billing's primary meter stayed permanently zero.

The meter now rides the tenant session and its existing commit, so it lands with
the extraction result or not at all, and the `ctrl_db` parameter that existed
solely for this write is removed rather than left available to a future caller.

Rejected: adding the tables to `CONTROL_TABLES` with a control-plane migration.
That direction additionally requires repointing `api/billing.get_subscription` at
`control_db` and migrating every existing tenant's rows, and it would split the
two meters across two databases for no gain — `rollup_usage` already reads them
together, per tenant.

`assistant_usage` was checked against the same question and is genuinely
control-plane (it *is* in `CONTROL_TABLES`, present in `feohledger`, absent from
tenants). The asymmetry is real, so it is written down here rather than left to
be re-derived — and `docs/conversational-assistant.md` no longer cites
`extraction_usage` as its precedent.

**Guard:** `tests/test_extraction_usage_placement.py` asks Postgres where the
table is, both ways round. The file it replaced asserted the broken placement in
all eight of its tests, using an `AsyncMock` control session — so the INSERT was
never executed against a real database, which is why this survived.

---

## 58. `settings.mfa.required` is accepted even when `FEOH_MFA_ENABLED` is off — the inertness is reported, not refused

**Decided:** 2026-08-22 · `backend/app/api/organization.py`

Saving Org Settings → Security → "Require two-factor authentication for all
users" writes `settings.mfa.required=true`, but MFA itself only runs behind the
platform master switch `FEOH_MFA_ENABLED` (off by default — guard rail 7). Before
this decision, an admin could save the toggle with no signal anywhere that it
was currently inert: no user would ever be prompted.

Two options were on the table: refuse the save with a 409, or accept it and
surface the mismatch. Refusing is wrong here because the config isn't invalid —
an admin pre-configuring `required: true` ahead of a deployed environment
flipping `FEOH_MFA_ENABLED` on is a legitimate, common sequencing (the same
tenant settings travel with the org across environments; nothing about the
*setting* is malformed). This is the same shape of problem §34's residency
`alignment` block and `extraction.py`'s `platform_provider_reason` already
solved: accept the config, and answer "is it actually in effect right now?" as
a separate, computed-on-read signal.

`PATCH /api/organization` now logs a loud warning when accepting
`mfa.required=true` while the switch is off, and `_org_response` (shared by
`GET` and `PATCH`, mirroring `_residency_response`) injects
`settings.mfa.enforcement_active` — `required AND FEOH_MFA_ENABLED` — computed
fresh on every read, never persisted. The frontend toggle isn't wired to the new
field yet (tracked in `docs/followups.md`); the API contract is what this
decision fixes.

---

## 59. Ambiguous-date day-first preference reads a Companies-House number, not the data-residency region

**Decided:** 2026-08-25 · `backend/app/utils/dates.py`

Four call sites — `services/extraction.py`, `services/csv_import.py`,
`services/bank_reconciliation.py`, `services/vendor_statement_recon.py` — each
hand-rolled their own try/except order for a numeric date like `03/04/2026`,
and all four tried `%m/%d/%Y` before `%d/%m/%Y`. A UK invoice dated 3 April
booked silently as March 4th — no error, just a wrong date. `03/04/2026` is
genuinely ambiguous (both readings are structurally valid), so fixing the
order can only trade which locale gets corrupted; the real fix needs a signal
from outside the string.

Two existing org-configured signals could have supplied it:
`settings.residency.region` (`us`/`eu`/`uk`/`ca`/`au`, §34's GDPR/CCPA data
pin) and `settings.company.companies_house_number` (added alongside
`vat_registration_number` for a different UK-persona finding in this same
round). Residency was rejected: it answers *where this tenant's data is
legally required to live*, not *what country this company operates in* — a US
company can legitimately pin EU residency for a subsidiary's data, and reading
that as "this org is day-first" would corrupt a US company's dates on an
unrelated legal configuration. `vat_registration_number` was also rejected: VAT
registration is common across the EU and beyond, so its presence doesn't imply
UK, or even which day-first country. `companies_house_number` is the one
signal that means UK and only UK — Companies House registers UK entities
exclusively — so `resolve_day_first_preference` reads that field alone. No
signal (the default for every org that hasn't set it) resolves to
`day_first=False`, preserving the pre-existing month-first reading for the
common case.

The disambiguation itself (`parse_ambiguous_date`) is a second, orthogonal
piece: given `day_first`, try the preferred order, fall back to the other only
when the preferred order is structurally invalid for that string, and return
`None` — never guess — when neither parses. It is intentionally narrow (only
the `M/D/Y`-vs-`D/M/Y` slash/dash shape); ISO, `YYYY/MM/DD` and dotted
(`DD.MM.YYYY`, conventionally day-first by SEPARATOR, not by org locale) forms
are unambiguous and stay in each caller's own format list.

**Guard:** `tests/test_ambiguous_dates.py` unit-tests both functions and
AST-scans the four call sites (mirroring the UTC-today convergence guard in
`tests/test_utc_today.py`) so none of them can quietly regrow a hardcoded
`"%m/%d/%Y"` / `"%d/%m/%Y"` pair outside the shared helper.

## 60. An IdP-supplied email is refused for a control character, not held to the full shape rule

`services/identity_provisioning.extract_and_check_email` is where both SSO
protocols normalise the address an IdP asserted. It lower-cased, stripped and
domain-allowlisted it, and said nothing about its shape. `.strip()` removes a
*trailing* newline, so the obvious case was incidentally safe; an **interior**
one survived untouched and was stored as `User.email` — a login, and the
destination of every notification the app sends that person. A newline in a
value that reaches an SMTP header is the header-injection primitive, and the
attacker-chosen continuation writes itself: a `Bcc:` on mail carrying that
tenant's AP data.

The obvious fix — run `looks_like_email`, the rule §50 gave one owner — is the
wrong one **here**, and this is the decision. That rule requires a dotted
domain. A corporate IdP can legitimately assert `user@intranet`, and refusing it
would lock a whole tenant out of its own workspace over a cosmetic rule that
has nothing to do with the exposure. Trading a header-injection risk for a
guaranteed lockout is not a trade.

So the guard is narrower than the shape rule and exactly as wide as the danger:
`utils/emails.is_header_safe` refuses C0 controls and DEL, nothing else. No IdP
has a legitimate reason to emit one, so there is no lockout to weigh against it.

Two smaller calls inside that:

- **Refused, not sanitised.** Stripping the character out would provision a
  *different* identity than the one that signed in, silently. `UnsafeEmailAddress`
  propagates and both callbacks turn it into their existing generic refusal
  (SAML `_fail("unsafe_email")`, OIDC a 403 + a PII-free audit reason) — the
  address itself is never echoed into a second log line, which is the whole
  point of the value being unsafe.
- **The check runs before the allowlist**, so an unsafe address whose domain
  happens to be allowed is still refused.

Both callbacks are public routes, so an unhandled raise would be a 500 — a
worse failure than the one being fixed. `tests/test_sso_email_safety.py`
AST-scans each for the `except` rather than trusting the two edits to stay.

Found by the `test_email_shape_call_sites.py` drift guard, which asks which
modules decide "is this an email" without the shared rule. Its other two hits
are exempt with reasons: `api/enrichment.py` extracts a host, and
`api/auth_saml.py` decides whether a NameID *is* an email before falling back to
the attribute statement — where the shape rule would send exactly the internal
domains above down the fallback path.

## 61. An unrecognised sanctions `result` is a hold, not an allow — the verdict is never reached by omission

`ScreeningResult.result` is a three-value contract — `clear` | `match` |
`review_required` — and `check_payment_compliance` branched on two of them:

```python
if screening.result == "match":       # → refuse
if screening.result == "review_required":  # → reason → hold
# anything else falls through
verdict = "hold" if reasons else "allow"
```

Any fourth value therefore matched neither test and fell through to `allow`,
which is the one verdict that must never be reachable by *omission*. A
sanctions gate that clears a name because it could not read the answer is worse
than one that never ran: the payment goes out carrying the audit row of a check
that reported nothing.

The fix is an explicit `elif screening.result != "clear"` — `clear` is the only
value that proceeds silently, and everything else adds a reason.

**Why `hold` and not `refuse`.** Identical reasoning to the unknown-PROVIDER
path (§36) and deliberately the same verdict: the payment waits in
`pending_compliance`, the caller opens the `payment_compliance_hold` exception,
and the misconfiguration reaches the AP queue rather than the payment rail. A
`refuse` would be a dead end — `/compliance/release` re-runs the same gate, so a
refusal on an unreadable value could never be cleared by a human.

**Why fix a latent bug.** No shipped adapter emits a fourth value today, so this
was unreachable in practice. It is fixed anyway because the three live provider
adapters (ComplyAdvantage, Dow Jones, Refinitiv) are fail-closed skeletons
awaiting credentials: the first real provider response shape we have never seen
would arrive precisely here, and it would arrive as a silent allow. The
comparisons are exact, so a provider differing only in case (`Clear`,
`REVIEW_REQUIRED`) is exactly as unreadable as one inventing a new word — the
guard covers those too rather than case-folding, because guessing at an
unreadable answer is the failure being removed.

`vendor_screening.screen_vendor_record` was already fail-closed for the same
input (`_STATUS_MAP.get(result, "review")`), so this aligns the payment gate
with the vendor gate rather than introducing a new posture.

Confirmed one of the eight "unverified leads" recorded in `docs/followups.md`
from the round-14 money-path hunt, which the tracker (#321) carried as a
hypothesis rather than a finding.

## 62. A rebate figure names its currency; the entity scope depends on who is asking

**Decided:** 2026-09-04 · `backend/app/services/currency_conversion.py`, `app/api/dashboard.py`, `app/api/payments.py`, `app/api/cards.py`, `app/api/analytics.py`, `app/services/billing/usage_rollup.py`

`card_rebates` carries **no currency column**. A rebate's denomination is only
knowable through the `virtual_cards` row it accrued on, and six rollups needed
it — five of them shipped as bare cross-currency `SUM(CardRebate.amount)`:

| Rollup | What the mixed figure was |
|---|---|
| `GET /api/dashboard` | the "Rebates Earned" KPI on the main page |
| `GET /api/payments/summary` | `total_rebates`, under the `"currency"` the same response declares two keys below |
| `GET /api/cards/dashboard` | the rebate cards + YTD breakdown (fixed earlier; the join it needed is what made the rest reachable) |
| `GET /api/cards/rebates` | the list's `total`, above per-row amounts that each state their own card's currency |
| `GET /api/analytics/cfo` | the rebate-yield **numerator**, divided by a reporting-currency denominator and then annualised |
| `services/billing/usage_rollup` | `card_rebate_total`, a billing meter a later slice prices |

`currency_conversion.card_currency_sql` is the one owner of the expression. It
is named for the *card*, not the rebate, because card figures (`amount_limit` /
`amount_charged`) read it directly while rebate figures reach it through the
join — one expression, two jobs.

**Filter, don't convert.** `total_paid` / `total_pending` convert through
`payment_reporting_amount_sql`; a rebate cannot, because there is no rate on the
row to convert with. So each single-figure rollup keeps the matching rows and
counts the rest onto an `excluded_rebate_count` — all four of them, including
the analytics rebate-yield leg, where it matters most: a numerator missing most
of the rebates yields a silently understated `yield_pct` that is then
*annualised*. That is the same "be honest about what could not be combined"
rule §18 established for read-time FX.

The disclosure has to reach the wire, not just the handler.
`DashboardResponse` had no `excluded_rebate_count` field, so `response_model`'s
default `extra="ignore"` dropped the key the handler returned and the dashboard
KPI was right-but-silently-partial for every real caller — the state this whole
entry argues is worse than the original wrong-but-complete figure. A test that
calls the handler and reads its dict cannot see that; the guard is an
HTTP-level assertion. Converting on a read would make a historical figure move under the reader.

**Two things that look inconsistent and are not.**

*The entity scope differs by caller.* `GET /api/payments/summary` and the
dashboard KPI are entity-scoped; the billing meter is deliberately org-wide.
Same table, different question: the first two sit beside entity-scoped outflows
an operator reconciles them against, while the platform bills the customer
**org**, so a subsidiary breakdown there would be the wrong unit. A future
reader "fixing" the billing meter to match its siblings would be introducing a
bug.

*The billing meter groups where the endpoints filter.* An endpoint reports one
headline figure, so it picks a currency and discloses the remainder. A meter
cannot: there is no rate that turns a mixed scalar into a charge, and dropping
the non-reporting currencies would silently drop billable activity. So the
currency goes in the meter **name** — `card_rebate_total.USD` — one key per
currency, always, and **no key at all** for an org with no rebates. Zero rebates
in an unstated currency is not a fact, and one shape beats two: a consumer reads
every key prefixed `card_rebate_total.` without having to know whether it is
looking at a single- or multi-currency org. `report_usage` iterates the meter
map generically, so no adapter changed.

**Two error swallows went with it.** The payments-summary and analytics rebate
queries each wrapped themselves in a bare `except Exception` returning `0`.
That was scaffolding for a since-fixed bug where the query ran against the
control plane, where `card_rebates` does not exist (§57 records that cause).
The table is absent from `CONTROL_TABLES`, so the case is unreachable — leaving
a swallow that turned any *other* failure into a confidently wrong money figure
under a response declaring a currency. Zero rebates and "we could not read the
rebates" are different claims, and no sibling figure on either surface swallows.

`tests/test_rebate_currency_denomination.py` guards the class rather than the
six instances: an AST scan fails any statement summing `CardRebate.amount` that
neither filters nor groups by currency, and a source scan fails any module that
re-derives the expression inline instead of calling the shared helper.

---

## 63. Every adapter registry is classified, and the fixture fallback is gone from the ones that could answer wrongly

**Decided:** 2026-09-04 · `backend/app/services/*/dispatcher.py`, `backend/tests/test_adapter_registry_fail_closed.py`

§29 removed `_REGISTRY.get(x) or _REGISTRY["mock"]` from payments / ERP / FX, §36
from sanctions, §56 from cards / positive-pay / enrichment. Six families still had
it, and every one of them was written **after** the rule existed — which is the
finding. The rule was a habit, reapplied by whoever remembered it, not a property
of the codebase.

A `mock` adapter is not an inert stub. It is the thing that makes `pnpm dev` work
with no cloud account, so it answers **yes** to everything:

| Family | What a typo'd provider name produced |
|---|---|
| 1099 e-filing | a `Tax1099Filing` row + a `tax_1099.filed` audit row + a 200 telling the org its 1099s were e-filed when nothing reached the IRS — and the `(org, idempotency_key)` slot burned, so the corrected retry returned `already_filed` and never filed either |
| TIN validation | `Vendor.tin_verified_at` stamped from a regex — the flag B-notice and 24% backup-withholding decisions key off |
| PEPPOL | a legally-significant e-invoice reported `sent` with a synthetic message id to a supplier that never received it, the row occupying `uq_peppol_one_live_per_invoice_direction` so the honest resend came back `already_sent` |
| tax rates | a jurisdiction VAT/GST figure computed off the in-repo fixture table while the response's `provider` field named the provider that was asked for |
| punch-out | a `PunchoutSession` the buyer is navigated to, and a PUBLIC cart-return endpoint accepting a different payload shape than the tenant's configured protocol — whose fixture cart converts into a real `PurchaseRequisition` |
| QMS | three fixture inspections resolved against the tenant's **real** POs and persisted as `completed` `QualityInspection` rows — the 4-way match's quality leg, so a fabricated `pass` clears the gate for whatever invoice references that PO |

The rule is unchanged: absent or empty still resolves the local-first default
(guard rail 7 — an org that has configured nothing is a normal state); a **named**
provider we have no adapter for raises; and each caller decides what the refusal
means. Across eight conversions the callers' answers fell into four kinds, which
is the useful generalisation: **refuse before a claim is recorded** (1099 filing,
above the idempotency-slot insert); **refuse before state moves** (TIN verify,
above the write that would otherwise *clear* a correct verification); **report it**
(the manual QMS route, the tax-rate routes); and **count it and hold what you
have** (the QMS sweep).

Three call-site details generalise past their own surface:

- **A refusal must not advance a cursor.** The QMS sweep's `last_synced_at` sits
  on the success path only. Advancing it on a refusal closes a window that was
  never pulled, so every inspection written during the outage is skipped *forever*
  once the config is corrected — worse than the fabrication the refusal prevents.
  Both the old fallback and a naive unguarded raise would have advanced it.
- **The inbound PEPPOL webhook returns a bodyless 503, not the route's usual
  opaque 204.** An unresolvable provider is *our* failure, not a decision about
  the document — §37's rule, not §29's. Acking would drop a supplier's invoice
  permanently; a 503 leaves it as work the Access Point redelivers.
- **The punch-out cart return drops silently (204) instead.** There is no
  retrying Access Point on that path — a supplier posts once from a browser — so a
  5xx surfaces a stack trace to a prober without recovering the cart.

**The durable part is the classification, not the conversions.**
`test_adapter_registry_fail_closed.py` now requires *every*
`app/services/*/dispatcher.py` to be classified — fail-closed, or fail-open with
what its fallback actually does written down — and AST-scans the fail-closed set
for the `.get(x) or <default>` / `.get(x, <default>)` shape. Sixteen of twenty-one
are fail-closed. The five that remain are a reviewed decision rather than a
backlog (`assistant`, `billing_adapters` — refused at boot by `main.py` for the
one dangerous path, `chat_notification_adapters`, `email_adapters`,
`embedding_adapters`), each re-checked against its current resolver *and* its
consumer. The admission rule is now written into the test: **a fallback belongs on
that list only if it cannot produce a confident wrong answer about money, a
document, or a control.** It must degrade to a no-op, a log line, or a
lower-quality suggestion.

**Not adopted, again: validating the provider name on write.** §29 rejected it for
`PATCH /api/organization` and the reasoning holds — settings predate any
validator, arrive from seeds and migrations, and an adapter can be *removed* from
the registry after a name was stored. The same argument disposes of allowlisting
`bank_format` at the schema: the route already funnels through `_require_formatter`,
which 422s naming the bad value and the registered alternatives, so a field-level
check would be a strictly narrower duplicate of the chokepoint every caller passes
through. The gap there was route-level *coverage*, and that test now exists.

---

## 64. `aws_textract` was the one adapter with no async client

**Decided:** 2026-09-04 · `backend/app/services/extraction_adapters/aws_textract.py`

Every adapter across the 21 registries reaches its provider over
`httpx.AsyncClient` except this one, because AWS ships no async boto3.
`boto3.client("textract", …)` resolves the credential chain (which can reach the
instance-metadata endpoint) and `analyze_expense` is a full HTTPS round trip
against a multi-second OCR service — both ran inline inside `async def`, holding
the event loop for that whole window while every other in-flight request on the
worker queued behind it.

Both call sites are exposed: `extract` is reached from the invoice upload route
**and the public email-intake webhook**, and `test_connection` is awaited directly
on the request path by `POST /api/organization/test-extraction`. The project
invariant grades a blocking call on a public webhook or auth path `Critical`.

Both now go through `await asyncio.to_thread(...)`, matching `services/storage`'s
`_put_object` and the three `*_dispatch` SQS sends. Client construction is factored
into its own `_client()` helper so the credential-chain resolution is offloaded
too, not just the round trip. `tests/test_sqs_dispatch_nonblocking.py` — already
the home of the boto3 loop rule — grew a thread-identity assertion per entry point
plus two AST scans (a blocking helper called inline from a coroutine, and an
inlined `boto3.client(...)` under any name).

---

## 65. A money filter bound is exact *and* snapped onto the column's own grid

**Decided:** 2026-09-04 · `backend/app/api/money_filters.py`

Typing the parameter `Decimal` was necessary but **not sufficient**, and that is
the part worth recording. Two separate roundings were in play:

1. **Python side.** `amount_min: float | None` followed by `Decimal(str(value))`.
   `Decimal(str(f))` recovers the shortest repr, so an ordinary two-decimal bound
   round-trips — which is why this never produced a visible bug — but the value had
   already been rounded to the nearest double before any application code ran.
   Declaring the parameter `Decimal | None` fixes this outright: FastAPI hands
   pydantic the raw query string, which parses exactly.
2. **SQL side, the non-obvious one.** SQLAlchemy types a comparison's bind
   parameter from the *column*, and the asyncpg dialect renders a bind cast:
   `invoices.amount >= $1::NUMERIC(15, 2)`. Postgres therefore rounds an
   over-precise bound **to nearest** at the column's scale — straight back onto the
   boundary row the bound was written to exclude. Retyping alone left the
   behavioural regression tests still failing; that is how this was found.

Rounding a bound to nearest is never right, because the comparison's *direction*
decides which way is safe. A money column is a fixed grid — `Numeric(15, 2)`, every
stored amount a whole number of cents — so the exact answer is to snap the bound
onto that grid in the direction of the comparison: a lower bound rounds **up** (the
smallest representable amount that still satisfies `>=`), an upper bound rounds
**down**. Exact for a bound of any precision. `money_filters.py` derives the scale
from the column itself rather than restating `2` at each call site.

Rejected: binding against a deliberately wide `Numeric(30, 10)` literal (still
truncates a bound with more than ten decimals — trades an exact rule for a wider
threshold), and 422-ing an over-precise bound (a filter refusing to filter).

**Both sides of a shared filter builder move together or not at all** — the list
and its `/counts` rollup filtering differently is precisely the drift the shared
`_*_list_filters` builders exist to prevent, and there is a test that fails when
only one side is converted.

The guard is two independent scans, each with a negative control: an OpenAPI scan
over the mounted app (a `Decimal` parameter renders `anyOf[number, string]` and the
string branch is what parses exactly; a `float` renders `number` alone) **and** an
AST source scan over `app/api/`, because the shared *private* builders appear in no
OpenAPI schema at all — and those are where the bound actually meets the column.
Both assert discovery non-emptiness, per the `_KNOWN_ROLLUP_COUNT` lesson.

The same rounding reaches a JSON **body** differently, and needs a different fix:
pydantic parses a JSON number into `Decimal('100')` from `100.00000000000000001`,
so only the string form round-trips. The shared parse lives on
`backend/app/schemas/money.py` (`ExactMoneyInput` / `OptionalExactMoneyInput`) —
it accepts an exact decimal string or a JSON integer and refuses a float — and
backs `POST /api/discounts/optimize` (with the frontend caller moved in the same
change, a wire-contract change rather than a retype) and the cash-flow copilot's
plan tools and plan bodies.

The copilot case adds one rung the endpoint case does not have. Its budget arrives
as an **LLM-produced tool argument**, and `ToolSpec.anthropic_spec` derives each
tool's `input_schema` from `model_json_schema()` — so a bare `Decimal` advertised
`number`, instructing the model to send the exact shape the validator refuses. The
annotation now carries a `WithJsonSchema` declaring `string`: the refusal is the
backstop, the advertised schema is the fix. It matters beyond tidiness because
`propose_payment_plan` hashes `str(cash_budget)` into `plan_id` and
`POST /plans/{plan_id}/draft-run` stages a real `PaymentRun` from the plan that id
certifies — so a rounded budget is two wrongs: a different selection, and an id
asserting the rounded figure is what was approved. For the same reason the parse
covers `min_balance_threshold` (also in the `plan_id` preimage) and
`opening_balance` (persisted money), not the budget alone — hardening one leg
leaves the hash half-exact. Neither side normalises the value, because the
preimage is `str()` and rescaling `"8.00"` to `"8"` anywhere would fail a plan's
own stale-plan check without its parameters having changed.

---

## 66. A unique index is not a substitute for the two-phase re-check

**Decided:** 2026-09-04 · `backend/app/services/recurring_invoices.py`

`recurring_invoices` was left without step 2 of the sweep locking shape on the
reading that `uq_invoice_recurring_period` already made the sweep idempotent. It
does not. The index forbids a *second* invoice for a period; the unguarded failure
mode is a *first* invoice for a period that is not due yet, on a distinct period key
the index accepts — replica A generates P and advances the cursor to P+1, replica B
locks, reads the fresh P+1 and generates it early, and the cursor jumps to P+2 so
the real P+1 tick raises nothing at all.

The general rule the two facts add up to: **a uniqueness constraint bounds what a
write may contain; only re-reading the predicate under the lock bounds whether the
write should happen.**

Rejected: widening the index (there is no key that expresses "not due yet"), and a
tenant-level advisory lock (it reintroduces exactly the whole-tenant hold
`background-sweeps.md` § Locking exists to remove).

A related correction in the same change: `ORDER BY next_run_on` is a **partial**
order, so templates sharing a due date can be locked in opposite orders by two
replicas and deadlock. "Ordering by id gives every replica the same lock order"
only holds while the sort key is unique.

---

## 67. An over-receipt is flagged beside `MatchResult.status`, never through it

**Decided:** 2026-09-04 · `backend/app/services/po_matching.py`, `backend/app/services/invoice_warnings.py`

The 3-way leg now excludes cancelled goods receipts (a **denylist** — `status` is a
free-form `String(30)`, so an allowlist would have silently stopped counting
`partially_received`) and flags `received > ordered`. That flag is an additive field
plus an `issues` line; `status` keeps its four existing values.

`mismatch` is owned by the AMOUNT control — `invoice_warnings._refresh_po_match`
composes its message purely from `amount_variance` / `amount_variance_pct` — so
routing a quantity over-receipt through it would emit a message about an amount
variance that isn't there, and `partial` means the opposite thing. An over-receipt
with an in-tolerance amount is a receiving discrepancy, not a billing one.

It reaches the exception queue by a different route: `_refresh_po_match` raises it
**independently of `status`**, the way the 4-way inspection block already does. That
independence is the whole point — `status` belongs to the amount control, so an
over-receipt on an otherwise-`matched` invoice was the case that disappeared
entirely. Severity is `warning`, not the `info` a partial receipt gets: a short
delivery is routinely benign, quantities nobody ordered are not.

Rejected: a fifth `status` value (breaks the persisted `invoice.po_match` contract
every downstream reader and the frontend `PoMatch` type read); reusing `mismatch`
(wrong message, and it would block payment on a receiving-side clerical error); and
a new exception TYPE (`EXCEPTION_TYPES` is a fixed vocabulary with a label-coverage
AST guard, and an over-receipt genuinely *is* an invoice-vs-PO discrepancy).

---

## 68. A tally may need a column its list is not selected on — §48 extended

**Decided:** 2026-09-04 · `backend/app/api/vendors.py`

§48 covers a tally that describes a *narrower or wider* population than its list.
The `/vendors/screening` "Payments blocked" KPI was a different shape. It was
counted off the screening review queue, whose predicate is
`screening_status IN ('match','review')`, but `POST /api/vendors/{id}/block` sets
`payments_blocked` and never touches `screening_status`. So the figure was not
merely narrowed — it was **structurally incapable of counting the thing it named**,
at every page size, forever.

The rule §48 states for filters extends to columns: a tally is computed from a query
that asks the tally's own question. Where the tally's axis is orthogonal to the
buckets beside it (`payments_blocked` vs `status`), it rides the **same aggregate**
rather than becoming a second endpoint or a second query — that is what makes "same
filters, same entity scope" true by construction instead of by convention — and it
is reported as its own field, never folded into `total`.

Two consequences worth stating, because both are places the fix could have gone
wrong:

- **The RBAC asymmetry is left visible rather than papered over.**
  `GET /api/vendors/counts` is gated admin / ap_manager / cfo to match its list
  exactly, as §48 requires in both directions; the screening queue also admits
  `ap_clerk`. A clerk therefore gets a 403 and the card renders "Count unavailable"
  rather than falling back to the queue-derived number — that number is the bug, and
  a fallback would reinstate the defect for exactly one role, which is the hardest
  place to notice it.
- **A KPI that cannot honour a filter says so.** The page's search is a client-side
  filter over different columns than the API's `search`, so the card is labelled
  "All vendors, not just this queue" instead of being wired to a search it does not
  describe. Silently ignoring the search was the original sin here; saying which
  population the number covers is the fix.

---

## 69. The WORM trail quarantines a poison row, never an outage

**Decided:** 2026-09-04 · `backend/app/services/audit_log_shipper.py`, `backend/app/services/retention_sweep.py`

Two SOC 2 evidence paths were failing in opposite directions.

**The shipper stopped.** Batches are all-or-nothing and ordered `created_at ASC`, so
one row a sink refused made `adapter.ship` raise on every tick, re-select the
identical oldest-first batch, and block every newer row for that tenant forever. The
sweep-health streak fired correctly; the defect was that the only remedy was manual.
A failed batch is now followed by a **bounded isolation pass**: rows re-ship one at a
time in order, and a row an adapter refuses is re-offered *to that adapter* with its
`details` replaced by a PII-free marker. Row identity is untouched, so the WORM copy
stays an ordered trail, and the complete row remains in the tenant `audit_log`.

The bound is what makes it safe. **If an adapter refuses the marker version too, the
sink is unhealthy rather than the row poisoned** — the pass stops there, everything
from that row on stays unshipped, and the tick fails as before. That caps an outage
at two extra calls per adapter and stops a transient outage stripping the details off
a whole batch. Substitution is per-adapter, since a row CloudWatch refuses may be fine
for the S3 Object Lock copy. A quarantined row is *not* counted as a sweep failure —
the trail moved and nothing was dropped — but the count and one PII-free WARNING per
row (id + exception class, never the payload) are the operator signal.

**The retention sweep would not stop.** Its `retention.archived` manifest was gated on
`archived or overdue_total`, where `overdue_total` counts `audit_log` rows past the
window — and the sweep never deletes an audit row (migration 0022's BEFORE-DELETE
trigger forbids it, and WORM evidence must not be destroyed anyway). So once a
tenant's oldest audit row crossed its window the condition was permanently true, and
a manifest reading `invoices_archived: 0` was appended on every tick forever — each
one itself an `audit_log` row that later ages past the window and inflates the next
tick's count. Unbounded growth in an append-only, undeletable table.

The gate is now `archived or overdue_unshipped`. Rejected: change-detection against
the previously recorded counts, which needs an extra per-tenant read of `audit_log`
every tick to reproduce a property this gate has for free — `overdue_unshipped` is the
actionable half of the same observation, it cannot inflate itself (a manifest written
now is far younger than the window), and it returns to zero once the shipper catches
up, at which point the manifest stops on its own. `audit_rows_overdue` is still
*reported* in every manifest; it just no longer *causes* one.

The pair is the general rule: **an evidence trail must keep moving past one bad row,
and must stop writing when it has nothing to say.**

---

## 70. The login-failure audit row is written off the response path

**Decided:** 2026-09-04 · `backend/app/api/auth.py`, `backend/app/api/portal_auth.py`, `backend/app/services/audit_dispatch.py`

`/api/auth/login` equalised the bcrypt cost with `dummy_verify()` on the
unknown-address path, then reintroduced the same oracle one line later: a
**known** address additionally `await`ed `dispatch_auth_audit`, which resolves
the tenant DB from the control plane, opens a session, and commits an
`auth.login.failure` row. An unknown address has no org, so it skipped all of
it. Two rejections differing by a whole DB round trip:

```
KNOWN  : [check_rate_limit, check_auth_failures, password_hash_cost, record_auth_failure, dispatch_auth_audit]
UNKNOWN: [check_rate_limit, check_auth_failures, password_hash_cost, record_auth_failure]
```

The supplier portal carried the same defect and **worse**: that request already
holds a tenant session from `get_tenant_db`, but the audit helper opens its own
*second* one to commit through. A third timing signature existed there too — a
legacy vendor-user row with no `organization_id` returned from *inside* the
helper, after the `await` had been entered, so it was neither the fast path nor
the slow one.

Both obvious fixes were unavailable: dropping the audit is worse, and padding
the fast path is masking (guard rail 4). So the write moved **off the response
path**, and both rejections were collapsed onto one shared tail — `_reject_login`
/ `_reject_portal_login` — so they are identical *by construction* rather than
by inspection: one awaited `record_auth_failure`, one non-awaited
`queue_auth_audit`, one 401. `organization_id=None` is **passed**, not branched
on, so an unknown address reaches the same call site.

**`post_commit` is the wrong mechanism here, and that is the load-bearing
detail.** It fires from SQLAlchemy's `after_commit`; a failed login raises
`HTTPException`, so `get_control_db` rolls back and `after_rollback` drains and
discards the queue *by design*. A row queued there would silently never be
written. The trigger is `loop.create_task` — the same spawn mode `post_commit`
itself uses — with a strong reference held so a running task cannot be collected
mid-await.

The guarantee is therefore weaker on purpose, and is stated rather than hidden:
"written shortly after the response, **or reported at ERROR**". ERROR, not
WARNING, because an auth audit row is SOX evidence and nothing retries it. A
bounded 5-second drain runs in the lifespan's `finally`, **before**
`dispose_all_engines()` — the queued write commits through the tenant engine
that call disposes, so the reverse order would silently break every in-flight
row. Bounded rather than unbounded because one hung write would otherwise block
shutdown until an orchestrator SIGKILLs the process, losing every *other* queued
row too; on expiry the abandoned **count** is logged PII-free.

The rate-limit ordering is untouched: `check_auth_failures` still runs on the
SHA-256 of the *submitted* identifier **before** the DB lookup, which is what
makes an unknown address throttle identically.

Only *failure* rows moved. `auth.login.success`, `auth.mfa.challenge_issued` and
the SSO-only refusal all sit after a correct password, so none is an existence
oracle; they still await.

---

## 71. Every approval threshold is denominated in the reporting currency, and they share one value

**Decided:** 2026-09-04 · `backend/app/services/approval_chain.py`, `review.py`, `extraction.py`

`auto_approve_below`, `require_cfo_above`, `max_invoice_amount` and the approval
matrix's per-level amount bands were bare JSONB numbers that never declared a
currency, compared against raw `Invoice.amount` in whatever currency the invoice
was billed in.

Measured against the pre-fix code: a **GBP 9,000 invoice — USD 11,403 at the
rate locked on its own row — was approved by an `ap_manager` with no CFO
signature** under a USD 10,000 `require_cfo_above`, and `resolve_applicable_levels`
routed it to the manager tier of a chain whose senior level starts at 10,000.
Separately, three spotless JPY 100,000 vendors (≈USD 650) pushed the
auto-approve recommendation toward the USD 25,000 cap, so a ¥1,000,000 invoice
read as "below 5,000" and auto-approved unattended.

The denomination is the org's **reporting currency** — not a new convention, the
one `payment_controls.cfo_approval_decision` and `expense_approval.cfo_threshold`
already use. Both sides of every comparison convert through
`currency_conversion.reporting_amount_at_locked_rate` at the rate already on the
invoice row, never one fetched at read time (which would make the gate
non-deterministic).

**Converting one side alone is worse than converting neither**, so all five
sites moved in one commit. They now share a *value* — `approval_chain.GateAmount`,
carrying the figure in the gate currency **and** whether it could be established
at all — rather than a shared spelling. A plain `Decimal` still means "already in
the gate currency". The distinction matters because a convention is what the
sixth comparison forgets; an AST drift guard fails any gate site that reverts to
a raw amount.

**Fail-closed is a different direction at each site, all pointing at a human:**
the gates **fire**; the auto-approve floor does **not** fire; and the chain
**skips its amount bands** so every routing-matched level applies. That last one
is the trap — an empty chain result is *no chain requirement at all*, so
filtering on unpriceable figures could drop the senior level, which is the silent
version of skipping the CFO.

The adaptive **anomaly baseline** made the opposite call deliberately: it
**abstains** (emits `amount_comparison_unavailable`, still runs the approver and
timing rules) because it is advisory and writes no warning or Exception row —
there is nothing to fail closed *on*. `detect_invoice_anomaly`'s `amount` is now
a required keyword-only argument with no default, so nothing can forget it.

The UI followed in the same round: an admin typing `10000` into "Require CFO
approval above" had no way to see which currency that was. Before the
denomination was settled the number was merely ambiguous; afterwards it had a
definite meaning the operator could not see, which is worse. Labels and hints
now name the code, resolved at runtime from the org's reporting currency.

---

## 72. GL code uniqueness needs two partial indexes, and a dirty tenant fails loudly

**Decided:** 2026-09-04 · `backend/alembic/versions/0088_gl_account_code_unique.py`

`gl_accounts.entity_id` is multi-entity's deliberate exception: NULL means the
account is **shared** across every entity, not "unstamped legacy row" as it does
on invoices and vendors. NULLs never compare equal in a unique index, so the
obvious `UNIQUE (organization_id, entity_id, code)` would enforce nothing at all
on the shared chart — the exact place a duplicate is most damaging, since a
shared account sits in every entity's effective chart.

Splitting on the NULL-ness states the two rules separately and enforces both:
`(organization_id, code) WHERE entity_id IS NULL` plus
`(organization_id, entity_id, code) WHERE entity_id IS NOT NULL`. Rejected: one
`UNIQUE (organization_id, code)` across the tenant, which would forbid two
subsidiaries each running the standard `6000` — normal practice, and it would
fail against existing data.

The API guard is deliberately **broader** than the index: create refuses a code
already visible in the caller's *effective* chart (shared ∪ selected entity),
because that is the list a clerk codes an invoice from, whereas an index can only
speak per-chart. The ERP sync upsert was matching on `(code, organization_id)`
with no entity filter, so a sync run under entity B updated entity A's row rather
than creating B's — contradicting the route's own docstring.

**A tenant with pre-existing duplicates fails the migration loudly and changes
nothing**, naming up to 20 offending groups. Contrast migration 0081, which
auto-repairs over-claimed bank transactions because clearing a claim back to
"unmatched" is visibly conservative. There is no equivalent here: duplicate GL
rows differ in `name` / `account_type` / `is_active`, invoices already reference
the code as free text, and picking a survivor silently discards chart
configuration or re-labels spend booked under the other row. GL codes are org
configuration, not PII, so naming them in the error is safe.

---

## 73. Touchless rate means "passed review", not "reached a terminal state"

**Decided:** 2026-09-04 · `backend/app/services/analytics.py`, `backend/app/api/dashboard.py`

The straight-through-processing rate counted `done` — and `paid` — as having
cleared review on status alone. Both are reachable *around* review: `new → done`
is a legal `VALID_TRANSITIONS` edge that skips approval outright, and the Day-0
CSV importer (`services/csv_import`) plants historical rows straight at `done`,
its default, or `paid`, with the workflow engine never running. So a tenant that
migrated ten thousand historical invoices on day one reported near-100%
automation — inflating hardest for the tenant with the least automation to show.

The code had already solved this once and not generalised it: the `failed` leg
counted an invoice as cleared only with the durable `Invoice.approval_date`
stamp, and an unstamped one sat in neither leg. `done` and `paid` are the same
shape of problem, so they now sit in a named
`TOUCHLESS_REVIEW_EVIDENCE_STATUSES` beside it.
`approved`/`sending_to_erp`/`sent_to_erp`/`posted_in_erp`/`payment_scheduled`
stay proof-by-status because every edge into them originates at `approved`, and
every writer of `approved` stamps `approval_date`.

The alternative reading — "reached a terminal state without human touch" — was
rejected because the figure is quoted to leadership as evidence the platform is
doing the approving, and an invoice that never entered review is not evidence of
that. An unevidenced invoice leaves the **denominator** too, rather than being
parked in the bounced leg: counting it as "finished review and did not clear"
would deflate the rate exactly as dishonestly as the old rule inflated it.

`compute_touchless_rate`'s optional kwargs collapse into one **required**
`review_cleared_count`, so an un-updated caller raises `TypeError` rather than
quietly re-publishing the old wider number. This moves a previously reported
figure **downward** at deploy time for any tenant using the `new → done`
shortcut or the importer; `backend/docs/analytics.md` records that so a
dashboard delta is not misread as an automation regression.

---

## 74. Two routing numbers on a vendor, and why the checksum left Pydantic

**Decided:** 2026-09-04 · `backend/app/schemas/vendor.py`, `backend/app/api/vendors.py`, `backend/app/api/portal.py`, `backend/app/services/payment_adapters/base.py`

Larger US banks publish a different ABA for incoming Fedwires than for ACH, so
one generic `routing_number` could not express a payable wire at those banks.
Rather than reinterpret stored data, `routing_number` keeps its existing meaning
(ACH) and `wire_routing_number` is added beside it — no backfill, no migration,
`bank_details` is JSONB. `payment_adapters/base.resolve_routing_number` is the
single resolver: wire rails prefer the wire ABA and fall back to the ACH one (a
bank publishing one number uses it for both), while ACH rails **never** borrow
the wire number, because a bank with two ABAs rejects an ACH file addressed to
its Fedwire number — borrowing would convert a missing-data problem into a
returned item at the vendor's expense. The new field travels the existing
dual-control staging path, so it is not a BEC bypass.

The checksum check was deliberately moved **out** of the field validator.
FastAPI renders a Pydantic `ValidationError` as a 422 whose body echoes the
rejected `input`, and that input is the whole `bank_details` dict including the
account number — so "your routing number has a typo" was answering with banking
data, against the invariant that PII/banking data stays out of error bodies. It
now runs at the two chokepoints instead: `_stage_ap_bank_change` (which all
three AP entry points share) and `approve_change_request` (where any staged
change, AP- or portal-submitted, is applied), raising an `HTTPException` naming
only the field.

The supplier portal had the identical validator and kept the identical leak, so
it moved the same way — and sharing one helper closed a second gap in passing:
the portal validator checked only `routing_number`, so a malformed wire ABA
reached staging unvalidated and failed only at approve.

---

## 75. Re-extracting a resubmitted supplier invoice needs two guards, not one

**Decided:** 2026-09-04 · `backend/app/api/portal.py`, `backend/app/services/extraction.py`, `backend/app/services/extraction_dispatch.py`, `backend/app/services/extraction_lambda.py`

`POST /portal/invoices/{id}/resubmit` shipped without re-extraction because a
fresh pass calls `vendor_matching.match_and_link_vendor` and can re-link
`Invoice.vendor_id` to a different supplier — dropping the invoice out of the
`vendor_id ==`-scoped portal list, so the vendor loses sight of their own
resubmission. The scoped fix was a `skip_vendor_match` flag.

The code needed a second one. Re-extraction also re-enters
`decide_auto_approve`, so a tenant with unattended approval configured would let
a supplier launder a human-rejected invoice past the reviewer who rejected it:
submit garbage, get rejected, resubmit a doctored PDF that extracts under the
auto-approve floor. `suppress_auto_approve` is therefore not an extra — it is
what makes re-extraction safe to enable at all.

Both default `False` and travel together in one frozen `ExtractionOptions`, on
the local queue tuple's fourth slot and as flat SQS keys, so the tuple's shape
stops changing, a legacy 3-tuple still drains, and an absent key decodes to
today's behaviour. `lambda` mode reads them too: carrying the flags without
honouring them would have left the hole open in exactly one dispatch mode, which
is worse than not shipping the flags.

`skip_vendor_match` pins `vendor_name` as well as `vendor_id`, because
`PATCH /api/invoices/{id}` re-resolves a stale link from the name — pinning only
the id defers the re-link to the reviewer's next save rather than preventing it.
`rejected → pending` is not in `VALID_TRANSITIONS`, so the route takes the
documented `rejected → new → pending` rework loop rather than widening the state
machine from an API handler; dispatching from `new` was rejected because
`new → failed` is illegal and extraction's own error handler would raise inside
its `except`.

---

## 76. Per-box 1099 allocation follows the GL account, and reconciles by construction

**Decided:** 2026-09-04 · `backend/app/services/tax_1099.py`, `backend/app/services/tax_filing_adapters/base.py`

A vendor's whole reportable YTD total was filed in one box, which is wrong for
any vendor whose spend spans categories. The box for each payment is now
resolved from `Invoice.gl_account` through a per-org mapping on
`Organization.settings.tax.boxes` (exact + `prefix*` GL rules, per-vendor
overrides, a named `fallback_box`) — the coding AP already does, rather than a
new field nobody would fill in. The per-vendor override lives in that settings
blob rather than on the `Vendor` row: there is no general-purpose vendor JSON
column, and borrowing `bank_details` / `risk_factors` to carry a tax setting is
how columns rot.

**No proration.** Each payment lands whole in exactly one box, so the per-box
`Decimal`s sum to the reportable total to the cent with no rounding step and no
residual to lose. Splitting a vendor total by ratio needs largest-remainder
correction, and a correction step is exactly the quiet-gap class this feature
exists to remove. Spend no rule matches is not dropped: it goes to the named
fallback (default `NEC-1`, so an unconfigured tenant behaves exactly as before)
and is separately surfaced as `unmapped_paid`, alongside a published
`box_unallocated` residual and a `box_allocation_reconciled` flag — a
reconciliation guarantee nobody can read is one nobody can check. A configured
box code outside `BOX_CATALOG` is dropped rather than resolved to some box, so
the affected money shows up as unmapped instead of silently filing in the wrong
place.

`FilingFormPayload` carries the split (`box_amounts`) alongside the form total,
because a 1099-MISC with rent *and* medical payments is two boxes on one form
and transmitting only the total files the whole figure in whichever box the
partner defaults to. It is empty when there is no split to send, and a consumer
then files `box_amount` against its own box of record — the pre-allocation
behaviour.

---

## 77. Invoice-number normalization widens a match, so the scope is what keeps it safe

**Decided:** 2026-09-04 · `backend/app/services/invoice_warnings.py`

The always-on duplicate gate compared `lower(trim(invoice_number))`, so one
supplier's `INV-001` and `INV-1` were two distinct payables. The
semantic-similarity pass was the only backstop and it is inert whenever RAG is
off — the common configuration — so the same invoice could be paid twice.

Normalization collapses leading zeros inside each digit run and treats separator
*runs* as interchangeable noise. It collapses them to a single `-` rather than
deleting them, because deleting would make `INV-1-2` and `INV-12` the same
string; and it never strips non-digits, because reducing `INV-1` and `PO-1` to a
bare `1` is guessing rather than normalizing. Only leading zeros go, so `INV-100`
can never collide with `INV-1`.

Because normalization only ever *widens* a match, the candidate query keeps
precisely the scope the exact check already had — same tenant session, same
vendor by stable `vendor_id` or case-folded name, `id != self`. The scope is the
safety, not the string rule. With no migration available, the comparison is a
bounded in-Python narrowing pass rather than a query expression: SQL prefilters
on the ASCII letter skeleton (a superset invariant, so it can cost a match but
never invent one) and caps at 500 rows newest-first, and the pass runs only when
the exact match has already missed — so existing behaviour and the common-path
cost are unchanged. The accepted limit is invoice numbering where a leading zero
is significant within one supplier.

---

## 78. The last two sweeps page rather than cap, and a lead window belongs in SQL

**Decided:** 2026-09-04 · `backend/app/services/discount_auto_trigger.py`, `backend/app/services/contract_renewal.py`

Both sweeps loaded a tenant's whole candidate set in one unbounded `SELECT`. A
`LIMIT` was unavailable to either, for the reason `background-sweeps.md`
§ Locking already states: an offer skipped for a below-threshold ROI stays
`offered`, and a contract outside its lead window stays un-alerted, so neither
removes itself from the candidate set and a capped tick would re-serve the same
lowest-id rows forever — the exact starvation `approval_escalation` was rewritten
to avoid. Both now keyset-paginate until the tenant is exhausted; the per-item
`FOR UPDATE` re-check each already performed is what makes a page boundary safe.

`contract_renewal`'s `end_date <= today + 3650 days` pre-filter is replaced by
the real per-row window in SQL, `end_date - CAST(:today AS DATE) <=
COALESCE(renewal_notice_days, <default>)`. The old predicate matched effectively
every active contract with an end date, so the "keeps the fetched set small" it
claimed was false for any tenant whose contracts are not all decades out — it was
the whole-candidate-set load wearing a `WHERE` clause. The explicit
`CAST(:today AS DATE)` is load-bearing rather than cosmetic: a bare bind
parameter leaves Postgres choosing between `date - integer`, `date - date` and
`date - interval`, three overloads with three different result types. Because a
per-row interval expression is precisely where SQL and Python coercion diverge,
both halves resolve the NULL fallback through one `resolve_notice_days` helper
and a test evaluates the real expression in real Postgres against every boundary
and the NULL case — which also gave `FEOH_CONTRACT_RENEWAL_DEFAULT_NOTICE_DAYS`
its first reader, having been declared and documented but consumed by nothing.

---

## 79. Budget spend legs count what they refuse, and the rollup discloses it

**Decided:** 2026-09-04 · `backend/app/services/budget_service.py`, `backend/app/api/budgets.py`

Every leg of `compute_budget_spend` is scoped to the budget's own currency
because the legs never convert — summing unlike face values would be worse than
excluding the row. That was right, but the excluded rows were dropped
**silently**, so `committed` / `actual` read exactly like complete figures
whether or not a foreign-currency requisition or invoice had been left out.

Each leg now returns its total *and* its excluded count, computed in one query
via a Postgres `FILTER` clause so the count costs no extra round trip. A NULL
currency counts as excluded — `(currency = 'X') IS NOT TRUE`, not `<> 'X'`,
which would swallow it from both sides. The count rides onto both
`GET /budgets/{id}/spend` and the new `GET /budgets/rollup`, and `/cfo` renders
it as a `role="alert"` line naming the figures as a floor, the same treatment
the cash-position card gives its unconverted outflows.

Rejected: converting on read (an FX rate fetched on a read makes the figure
non-deterministic) and a whole-set rollup total across currencies (denominated
in nothing real). The rollup is whole-set by design — a paged rollup presented
as an org-wide total is the exact dishonesty being avoided.

---

## 80. An exact multiply, and why a money preview refuses rather than repairs

**Decided:** 2026-09-04 · `frontend/src/lib/utils/money.ts`

`sumMoney` added exactly, but nothing multiplied exactly, and two previews scale
money by a non-money factor — a requisition line's `quantity * unit_price` and a
discount tier's `base_amount * percent / 100`. Those were the last three
`number`-typed money fields on the frontend ratchet, and they were blocked on
the missing primitive rather than on judgment.

`scaleMoney` parses both operands as plain decimals, multiplies as `BigInt`s at
their combined scale, and rounds HALF_UP to the target scale in **one** step —
never two, which is where a half-cent goes missing (`1.004 * 1.004` rounded
twice gives `1.00`; the exact product is `1.008016`, i.e. `1.01`). `divideBy`
folds a constant divisor into the same step so a percentage needs no lossy
`percent / 100` first.

It returns `null` for input it cannot read, and callers render a dash. This is
the same rule the round applied to seven forms that were `parseFloat`-ing money
to the wire: a preview that silently repairs unreadable input is how a wrong
figure reaches a field the user then trusts, and the sharpest instance was the
approval-gate thresholds — `require_cfo_above` sent as `null` when the text
didn't parse, silently removing the CFO gate.

---

## 81. Imported invoices leave the touchless rate by provenance, not status

**Decided:** 2026-09-05 · `backend/app/services/csv_import.py`, `backend/app/services/analytics.py`, `backend/app/api/dashboard.py`

§73 narrowed the touchless NUMERATOR to require positive evidence of review.
The DENOMINATOR had the mirror of that hole: a CSV-imported `rejected` row sat
in the bounced leg as though a reviewer *here* had sent it back, deflating the
rate exactly as imported `done` rows used to inflate it.

Evidence cannot close it — nothing ever writes an approval stamp on a
rejection, so gating that leg would zero the bounced population outright rather
than exclude the imports. Neither can status: `done`, `paid` and `rejected` are
each reachable both by import and natively, so any status rule guesses. So the
importer records the fact directly: `Invoice.meta["imported"] = {"at",
"source"}`, one reserved key on an existing JSONB column, no migration.
`compute_touchless_rate` subtracts marked rows from every leg via a **required**
`imported_pipeline` kwarg — the §73 precedent, so an un-updated caller raises
`TypeError` instead of quietly publishing a padded denominator.

The NULL-jsonb trap is load-bearing rather than incidental. Postgres' `?`
operator returns NULL, not false, on a SQL-NULL `meta`, so without an
`IS NOT NULL` guard every meta-less legacy row silently left the numerator while
staying in the denominator — 33.3% against a true 50.0% in the regression test.
`native_invoice_clause` is written as the exact complement of
`imported_invoice_clause` for that reason, and both live with the marker's
writer so the reader cannot drift from it.

An UNMARKED row is treated as native, because absence of the marker means "we do
not know" and it is only ever written going forward. There is deliberately **no
backfill**: stamping a historical row on an inference is precisely the guessing
this replaces. Rows imported before the marker shipped therefore stay in the
population, which `backend/docs/analytics.md` states rather than papers over.
The marker also covers the CSV importer only — any future bulk path must stamp
it with its own `source` or its rows read as native, and no guard can enforce
that on code that does not exist yet.

---

## 82. The budget rollup is the per-budget spend query, widened

**Decided:** 2026-09-05 · `backend/app/services/budget_service.py`

`GET /api/budgets/rollup` is whole-set by design, so its cost had to stop
scaling with the budget count: 600 queries / 297 ms at 200 budgets, now 6 /
8.6 ms, with `GET /budgets/{id}/spend` unchanged at 3.

The obvious fix — a second, grouped SQL shape for the rollup — was rejected.
Both endpoints publish an `excluded_row_count` disclosure telling the reader the
money figures are a floor (§79), and **a disclosure the org-wide view and the
per-budget view can disagree about is worse than none.** Instead the grouped
query became the *only* implementation and `compute_budget_spend` is it,
narrowed to one budget. The currency rule is written once against
`Budget.currency` as a **column** rather than a Python literal; that
substitution is the whole trick, because the same predicate then answers for one
budget or a thousand.

Correlating the entity / period / dimension conditions cost the planner its
index and regressed the *single-budget* invoice leg from 0.6 ms to 10.4 ms over
40k invoices — on the path `GET /budgets/check` sits in before every requisition
submit. So each is restated once more as a set-level narrowing predicate,
logically redundant and provably implied by conditions already in the query, and
derived from the same budget set — which restores the identical index scan at
both scopes. That is one query shape, not a fork. The invoice leg batches by
dimension rather than folding four columns into one `CASE`, which would have
bought one query and cost every index.

The standing guard is `test_rollup_agrees_exactly_with_every_per_budget_spend`,
which folds every per-budget response by currency and compares figure for
figure including `excluded_row_count`. It was mutation-tested: forking
`compute_budget_spend` to under-report by one makes it fail, so the guard is not
vacuous.

---

## 83. Two tabs, two search keys

**Decided:** 2026-09-05 · `backend/app/api/bank_reconciliation.py`, `frontend/src/routes/bank-reconciliation/+page.svelte`

`/bank-reconciliation` has two tabs backed by two different endpoints:
Outstanding queries `/outstanding` over vendor names, invoice numbers, payment
methods and bank-line references; Statements queries the paginated statement
list over account identifiers, source formats and ISO period dates. A single
`?search=` would be carried from one to the other on every tab switch, silently
applying an account term to a vendor filter and rendering an empty tab the user
never asked for. The Statements term therefore lives on `?statement_search=`,
with its own debounce, its own applied-term guard and its own entry in the
page's single `syncUrl()` writer. Clearing a shared term on tab switch was
considered and rejected: it discards a filter the user set deliberately and
makes the URL non-restorable for one of the two tabs.

Both legs are server-side, and both are folded into the same filter object the
endpoint's own `COUNT` reads, so a narrowed table can never be headed by a
whole-set count (§48). Period dates match as ISO via `to_char`, **not**
`CAST(... AS text)`, which resolves against the session `DateStyle`: the row
renders a *localised* date, and matching that in SQL would make the result set
depend on the caller's browser language. `currency` is deliberately excluded
from the searched columns — it is never shown on that tab, and a three-letter
code is the highest-noise substring in the set.

---

## 84. Forecast variance renders "not computable", not `0%`

**Decided:** 2026-09-05 · `frontend/src/routes/cfo/forecastVarianceSummary.ts`

`POST /api/analytics/forecast_variance` emits `variance_pct = 0` whenever the
forecast is not positive, since a percentage of zero has no value. On a CFO's
screen `0%` reads as "we landed exactly on plan" — the most reassuring statement
available, over the one row carrying no information at all. That is the same
failure §34 records for the fraud-rate trend and §79 for the budget rollup's
utilization, and both fixed it at the API by emitting `null`.

This surface fixed it client-side instead: `variance_pct` is a shipped field
typed `number`, and this panel is its only consumer, so the honest render costs
nothing while the wire change would need its own migration of every reader. If a
later slice unifies the three, the API is the right place — the client guard is
then redundant, not wrong.

The entry form follows the round's other rule about money a user types: raw
decimal text, validated once at submit, **refused** with a toast. A month typed
without a readable amount refuses the whole submit rather than sending `0`,
which would make the variance equal the entire actual outflow and report a fake
0% — the same reassuring-wrong-answer this entry exists to prevent.

---

## 85. An unused dependency was setting the runtime the project builds on

**Decided:** 2026-09-05 · `frontend/package.json`, `.github/workflows/`, `deploy/deploy.sh`

`isomorphic-dompurify` declared `engines: ^22.22.2 || ^24.15.0 || >=26.0.0`
while every CI job ran Node 20. pnpm does not enforce `engines` without
`engine-strict`, so it installed without complaint — which is precisely why it
drifted unnoticed. The follow-up proposed bumping it to `^4.1.0`, the
maintainer's own "same code under honest semver".

It was **removed** instead. Nothing had ever imported it: the XSS defence in
this tree is that no component uses `{@html}` at all, and every chat / assistant
/ invoice bubble binds plain text and says so in a comment. A dependency with
zero call sites was dictating the runtime the whole project builds on and
dragging `jsdom` into the graph. The floor still exists, but it now belongs to
`jsdom` as **vitest's** test-environment peer — something the project actually
uses. If a case for user-supplied markup ever arrives, the sanitizer comes back
*with* its call site in the same change, never ahead of one.

Node moved to 24 (Active LTS to 2028-04-30; 20 reached end-of-life 2026-04-30)
at **nine** `setup-node` sites across six workflows — not the four the follow-up
named — plus `deploy/deploy.sh`, which builds the *production* frontend in a
`node:*-alpine` container. Both halves move together: a deploy image behind the
CI pin means production builds on a runtime CI never tested, and that
disagreement is invisible until it breaks. Separately, the `# vN.N.N` comment
beside `setup-node`'s SHA pin read `v6.0.0` at eight of nine sites against a SHA
that is really `v7.0.0`; Scorecard reads the SHA, so nothing caught it.

---

## 86. A vanity host is a hostname the SPA must recognise, not a slug it can parse

**Decided:** 2026-09-05 · `frontend/src/lib/hostRouting.ts`, `frontend/src/lib/tenant.ts`, `frontend/src/routes/+layout.svelte`, `frontend/src/routes/portal/+layout.svelte`

White-label custom domains had shipped, were documented, had an admin panel and
a provisioning runbook — and could not work. `backend/app/tenant.py` maps an
inbound `Host` to a tenant, but only when `X-Tenant-Slug` is **absent**, and the
SPA's rule was "the first label of any 3+-label hostname". So `ap.acmecorp.com`
pointed at tenant `acme` sent `X-Tenant-Slug: ap` and every call 404'd
`Unknown tenant: ap`. A bare apex sent no header but still called the
*build-time* `PUBLIC_API_URL` origin, so the backend never saw the vanity `Host`
either. The feature was unreachable in both shipped deployment shapes, and the
`/organization` panel's own placeholder was the broken case.

Three calls worth recording.

**The fix is classification, not validation.** The cheap option on the table was
to reject any custom domain whose first label isn't the tenant slug, forcing
`acme.acmecorp.com`. That makes the panel honest but keeps the product narrower
than what customers buy a vanity domain for. Instead the SPA now classifies a
hostname against an operator-declared `PUBLIC_PLATFORM_DOMAINS`: a platform
subdomain yields a slug, and **anything else deliberately yields none**, so the
`Host` fallback the backend already had is finally reachable. Sending a guessed
slug is strictly worse than sending nothing, because the header is what
suppresses the lookup.

**Unset config replays the old rule exactly.** The tempting default for an empty
list is "then no host is a platform host" — which would have made every existing
static build stop sending `X-Tenant-Slug` on upgrade and fall back to a `Host`
map with no entries. Total breakage, in exchange for a feature nobody had
enabled. The cost of the safe default is that custom domains stay unreachable
until an operator opts in, which both docs state plainly. The var is read
through `$env/dynamic/public`, not `static`, because a static import of an unset
variable is a hard build error and thirteen-plus CI and deploy sites pass only
`PUBLIC_API_URL`.

**Suppressing the header is only half of it.** A vanity host that still calls
the build-time API origin hands the backend the *platform's* `Host`. Only a
same-origin request carries the vanity hostname, so the API base is now resolved
at runtime and collapses to `/api` on a vanity host. That turns "terminate
`/api` on the same origin" into an operator requirement rather than an
implementation detail, which is why it is in the runbook and the panel's own
help text rather than only in code.

The layouts are the part that would have made all of the above still not work:
both gated rendering on `getTenantSlug()`, so a correct `null` on a vanity host
rendered the **marketing landing page** to a paying customer on their own
domain. They now gate on `hasTenantContext()` — "does this host carry a tenant",
which is a different question from "what is its slug", and the only one a render
gate should ask.

---

## 87. A passkey's relying party comes from the account's org, never from the header

**Decided:** 2026-09-05 · `backend/app/services/webauthn_rp.py`, `backend/alembic/versions/0091_webauthn_credential_rp_id.py`

A WebAuthn credential is bound to one registrable domain, and
`FEOH_WEBAUTHN_RP_ID` was a single global — so a tenant on a vanity host had a
strictly reduced second-factor menu. Making the RP ID per-tenant means deriving
it from a hostname, and the hostname arrives in a client-supplied header, so the
whole question is what makes that safe.

The answer is that the `Host` is never *trusted*, only *matched*. The
custom-domain list consulted belongs to the org that owns **the account the
ceremony is for**, resolved through `user.organization_id` — not to a tenant
looked up from the header. A forged host, an unknown host, and another tenant's
genuinely-registered vanity domain are therefore all the same thing here: not on
this account's list, so the platform RP applies. Fail closed, with no branch
where an attacker-supplied string becomes the relying party.

Register and authenticate agreeing is enforced rather than intended: the RP ID
is bound into the single-use Redis challenge and re-checked on finish, so a
ceremony begun on one host and completed on another is refused instead of
persisting a credential bound to a domain the authenticator never signed. A host
already *under* the platform RP ID keeps the platform RP even if separately
registered, so existing passkeys are never stranded.

**The migration story was implemented, not deferred**, because the honest
version of "passkeys now work per-tenant" is that a credential registered on the
platform subdomain genuinely cannot be presented on a vanity host — that is
WebAuthn, not a bug to code around. So it is made legible instead of silent:
`webauthn_credentials.rp_id` records where each credential lives, the list
endpoint reports `usable_here`, and the authenticate and step-up paths name the
host an account's passkeys belong to rather than failing opaquely. That message
is deliberately *not* opaque — the caller has already proved account control and
the hosts named are the tenant's own — while an account with **no** passkey at
all keeps the opaque answer, so the difference can't be used to probe whether a
factor is enrolled.

---

## 88. Two ERP push paths, and the one with the retry was the unreachable one

**Decided:** 2026-09-05 · `backend/app/services/erp.py`

`erp.py` carried `send_to_erp` and `send_to_erp_internal`. Production reached
only the second — `api/workflow.py` transitions the invoice itself and then
dispatches, so `erp_dispatch` and `erp_lambda` both call `_internal`. The two
had diverged on the thing that matters: `send_to_erp` had a 3-attempt
exponential backoff, and the reachable one had **no retry at all**, sending a
transient 503 or timeout straight to `InvoiceStatus.failed`.

The retry semantics were *tested but not shipped*, which is exactly why nobody
noticed: `test_erp_push_flow.py` called it "the load-bearing path",
`test_erp_adapter_idempotency.py` opened by citing its retry loop, and
`retry_erp` reset an `erp_retries` counter nothing in production ever
incremented. Every one of those statements was false. This is the same shape as
the diverged DPO calculation in §31 — two copies of one computation, the
unreachable one carrying the behaviour everyone believed in.

The retry moved to the reachable function and the copy was deleted, rather than
the reverse. Retrying automatically is safe here specifically because `_call_erp`
sends the invoice's `correlation_id` as the adapter's idempotency key, so a
retry after a timeout the ERP actually applied returns the existing document
instead of posting a second vendor bill. The transaction is committed before
each backoff sleep: `erp_dispatch` builds a `pool_size=1` engine per send, and
sleeping with the connection checked out would pin a worker slot for the whole
backoff instead of just the call.

Deleting the twin, rather than keeping it as the "full" entry point, is the
point of the change. A second push path is what allowed the divergence, and
nothing can drift against a function that no longer exists.

---

## 89. Half a validated pair is the worse failure

**Decided:** 2026-09-05 · `backend/app/schemas/vendor.py`

`validate_bank_routing_fields` is the documented chokepoint every bank-detail
write passes through — the AP staging path, the supplier portal, and
`approve_change_request`, where the dual-control BEC sign-off is applied. It
validated `routing_number`, `wire_routing_number` and `sort_code`.
`validate_uk_account_number` existed in `utils/banking.py`, was tested, and was
reached by nothing.

A UK payee is identified by the sort code **and** the account number together.
Checking one half meant a valid sort code alongside a five-digit account number
cleared staging, cleared the second approver, and surfaced days later as a
returned or misdirected payment — precisely the outcome the module's own
docstring says it exists to prevent. The asymmetry was invisible because the
sort-code half *was* checked, so the payload looked validated.

The account number is validated exactly when a sort code is present, and
deliberately not otherwise: `account_number` is the generic key every rail uses,
and a US or IBAN payee's is not eight digits. Validating it unconditionally
would refuse most real payees — the failure mode this gate must never have.

---

## 90. Conformance rules are asserted only where we hold the whole list

**Decided:** 2026-09-05 · `backend/app/services/e_invoice/{en16931_rules,codelists}.py`

`bis3_conformance_errors` gates whether we declare BIS Billing 3.0 at all, so a
document that fails it provably does not conform — that asymmetry is what makes
the conditional declaration sound. It checked mandatory elements only, so a
document that *passed* could still fail the real validator on arithmetic.

Implementing the EN 16931 calculation rules immediately earned its keep: the
generator mapped `Invoice.subtotal` into both BT-106 (sum of line nets) and
BT-109 (total without VAT), but `subtotal` is derived before discount and
shipping apply. **Every invoice carrying a discount or a shipping charge went
out contradicting itself three ways** — BR-CO-13, BR-CO-15 and BR-CO-17 — with
our conformance claim stamped on it. Fixed at the source in `mapper.py`, not by
relaxing the rule.

The judgment call is on code lists. Currency, country, VAT category and document
type are enforced as membership, because those lists can be held completely.
Unit of measure, payment means and the EAS scheme get a **shape check only**: a
curated partial list would 422 a genuinely conforming send on a rare-but-valid
code, and since this gate hard-refuses, refusing a conforming document is a
worse failure than the detection gap it would close. That follows the precedent
`tax_rules.py` already set — an unknown country is skipped, never rejected. For
the same reason the lists lean deliberately inclusive: over-inclusion only
weakens detection, under-inclusion refuses real documents.

The official Schematron is still not vendored, so a pass remains "nothing we can
compute objects", not a conformance guarantee. The failure direction is the one
that carries the weight, and it is unchanged.

---

## 91. One tenant-URL resolver, and the two call sites deliberately left out of it

**Decided:** 2026-09-05 · `backend/app/utils/tenant_urls.py`, `backend/app/api/organization.py`

`FEOH_TENANT_URL_TEMPLATE` was one global with `{slug}` substituted inline at
every call site, so a tenant reachable at its own hostname still got approval
links, portal invites and password resets pointing at
`<slug>.<platform-domain>` — working links that undo the white-label the vanity
domain was bought for. The follow-up named six call sites; there were **ten**.
Two of the extra ones are why the count matters: `services/supplier_chat.py` did
its own substitution, and `services/card_issuance.py` did it on `api/payments`'
behalf through an `org_slug` parameter, so neither read as a template call site
from the outside.

The per-org override is a *complete* base URL and the global is slug-shaped by
construction, so `{slug}` is optional in the resolver: substituted when present,
used verbatim when not. One rule, both sources. Rejected: a separate "vanity
host" setting alongside the template, which is two spellings of one question.
Substitution is `.replace`, not `.format` — a template is operator- and
admin-supplied text, and `str.format` raises on any unrelated brace; the chat
link used `.format` inside a bare `except`, so a stray `{` silently dropped the
link from the email.

**An empty result is a real answer.** Every caller now omits the URL line rather
than fabricating one, and the two callers carrying their own hardcoded
`http://{slug}.localhost:7777` dev fallback lost it. `_password_reset_url`
returning nothing skips the email entirely instead of sending a relative
`/login/reset-password?token=…` — a dead link that also burns a live single-use
credential. The endpoint's generic response is unchanged, so this introduces no
enumeration difference.

**SSO stays on the global template on purpose**, and this is the part that is
not laziness. The OIDC `redirect_uri` and the SAML bridge URL are values
*registered with the customer's IdP*. Re-pointing them at a vanity host silently
breaks every SSO login until the operator re-registers them — an
operator-sequenced migration, not a config read. The "convert everything"
reading of the follow-up was rejected, and both exemptions are asserted in the
drift guard so they read as decisions rather than misses.

**The platform domain is derived, not declared.** It comes from
`FEOH_TENANT_URL_TEMPLATE`'s own host with the `{slug}` label stripped, because
that is where the platform's hostname shape already lives; a new env var would
be a second source that can disagree with the first. It is what lets
`PUT /branding/custom-domains` refuse a host under the platform domain — such a
host is already routed by subdomain, so registering it hands the custom-domain
resolver and the subdomain resolver conflicting claims on one name, and lets one
tenant claim a hostname another tenant's slug owns or takes at the next signup.

**Adding a field to `BrandConfig` widens an unauthenticated endpoint.**
`GET /portal/branding` is public-by-design and returns the whole config, so the
new field would have published a staged, not-yet-cut-over vanity hostname to
anyone who asked. It is blanked there explicitly. The general rule this records:
a new `BrandConfig` field needs a publish / don't-publish call, not a default.

---

## 92. Two URL fields, because one of them is registered somewhere we don't control

**Decided:** 2026-09-05 · `backend/app/services/sso.py`, `backend/app/api/{auth_sso,auth_saml,organization}.py`

§91 left the OIDC `redirect_uri` and the SAML bridge URL on the global template
while the other eight call sites moved per-tenant, and named the reason: they
are registered at the customer's IdP. Closing that gap did **not** mean folding
them into `tenant_url_template`. It meant a second field.

`tenant_url_template` is admin-flippable with no external dependency — worst
case an invite link points somewhere odd. `sso_callback_base_url` is half of a
handshake with a system we do not administer: changing it without adding the new
URI at the IdP first breaks every SSO login for that tenant. One field would mean
an admin fixing invite links silently takes SSO down. So they are separate, the
callback is opt-in, unset is byte-for-byte the old behaviour, and the runbook
carries the ordered migration (add at the IdP → verify a real login → set this →
only then remove the old URI).

Two smaller calls under it.

**The `Host` fallback needs no JWT cross-check here**, unlike `get_tenant`. These
entry points are public and pre-authentication, and a forged `Host` can only
select a tenant that *registered that exact hostname* — the same choice `?slug=`
already gave anyone. The state/nonce (OIDC) and RelayState (SAML) are then minted
and consumed against that tenant. An unresolvable host reuses the **existing**
404 verbatim rather than inventing a response, so the change adds no enumeration
surface.

**The wipe hazard is the part that would have bitten.** Unlike `custom_domains`,
which is protected by not being a `BrandConfig` field at all, this one is — so
`model_dump()` emits it as `""` whenever a caller never mentioned it, and the
`/organization` panel PUTs the whole config. An admin editing the product name
would have silently cleared an IdP-registered callback and taken SSO with it,
with nothing on screen suggesting it. The fix is `model_fields_set`: an omitted
value is carried forward, an explicit empty string still clears it (that is the
documented rollback), and the response echoes what was **stored** rather than
what was sent, so a caller is never told the field is empty when it isn't.

---

## 93. `.get(k, {})` and `.get(k) or {}` were not two styles

**Decided:** 2026-09-05 · `backend/app/services/approval_chain.py`

Eight sites read `state_data["approval_levels"]` by hand in two spellings. The
follow-up filed it as ownership debt with "no behavioural bug found today", and
the instruction was to route them all through the existing, uncalled
`get_chain_progress`. Both halves of that turned out to be wrong.

The spellings differ on a stored `null`: `.get(k, {})` returns `None`, and every
consumer immediately calls `.get("levels", …)` on the result. That is an
`AttributeError` on the approval path, not a routing difference. The two sites
using it survived only because their callers happened to test truthiness before
subscripting — reordering one line would have made it live. `or {}` is correct
and is now the owner's behaviour.

And the uncalled helper fitted only three of the eight sites. Two of the others
read the chain out of a **deep copy** of `state_data` and mutate it in place;
the copy is what makes SQLAlchemy's dirty check see the write, so an
instance-shaped reader would have handed back the un-copied object and broken
the persistence. A function written without callers is not automatically the
right abstraction — ownership was reshaped around what the call sites actually
do (a raw-mapping reader, a thin instance front door, a writer, a clearer).

A *truthy* wrong-shaped value (a list, a string) is deliberately passed through
rather than coerced to `{}`. That is corrupt state, and the escalation sweep's
per-instance `try` is built to count it as a failed instance; coercing would
silently drop a real chain's requirement out of an approval path.

---

## 94. A read should not need a write to answer its own question

**Decided:** 2026-09-05 · `backend/app/api/email_intake.py`

`GET /api/organization/email-intake` returned `address: null` for two unrelated
reasons — the platform has no `FEOH_EMAIL_INTAKE_DOMAIN` (the committed default),
or this org has no token yet — and those call for opposite copy: "email intake
isn't available on this deployment" versus "click to create your address". The
payload could not tell them apart, and `enabled` only disambiguated *after* a
token existed, because provisioning is what sets it.

The UI's honest workaround was to offer a non-destructive "create" in the
ambiguous state and only claim the deployment was unconfigured once the server
had proven it — i.e. establish a read-only fact by performing a write. That is
the wrong shape even when the write is harmless. `domain_configured` is an
operator-config boolean, PII-free, no migration, and it answers on the first
read.

The client reads it as `?? true`. An older backend that doesn't send the field
degrades to assume-available, which is the safe direction: reporting a working
deployment as switched off is worse than the pre-existing ambiguity.

---

## 95. The 422 stopped being a string, and the client's translation table went with it

**Decided:** 2026-09-05 · `backend/app/services/e_invoice/validate.py`

`EInvoiceValidationError.__str__` emitted `field: code`, so `FieldError`'s
PII-free `message` ("FatturaPA requires a Partita IVA…") never left the server
and every client had to maintain its own code→prose map. The frontend had one.
It covered 4 codes and 12 field paths out of dozens, so most rows already fell
through to a bare `"was rejected (BR-CO-09)"`.

The body now uses FastAPI's own `[{loc, type, msg}]` validation-error shape,
chosen because the app's shared `formatApiDetail` **already** flattens exactly
that — so no endpoint-specific client handling was needed and `api.ts` did not
have to change. `__str__` is deliberately untouched: two services log it, and
only the HTTP body moved.

The rule id rides on **two** channels — `type`, and folded into `msg` — because
a client that flattens keeps only `loc` and `msg`, and the rule id is precisely
the half a receiving Access Point's own validator will name.

The trade-off, stated rather than hidden: those refusal sentences are now the
server's English instead of localized strings. It is still a net gain — most
rows previously had no explanation at all — and the wrapper copy stays
localized. Doing it properly means a code→key map generated from the backend's
rule set, which is its own slice.

---

## 96. A capability with a second, divergent path is worse than one with none

**Decided:** 2026-09-05 · `frontend/src/routes/payments/+page.svelte`

This round's premise was that a shipped, tested, documented capability with no
UI is a defect. Two endpoints were nonetheless left unwired on purpose, and the
distinction is worth keeping.

`POST /api/cards/{id}/cancel` exists and works. But every card this app mints
comes from a payment run's `virtual_card` leg and therefore has a `Payment` row,
and `POST /api/payments/{id}/void` — already on the History tab — cancels the
card at the provider *and* reverses the books in one operation. A standalone
Cancel would kill the card while leaving that `Payment` row and its invoice
claiming money is in flight on a now-dead rail: two controls that both close a
card and leave the ledger in different states. `POST /api/cards/generate` books
no `Payment` row at all — it is a card-issuance decision competing with the
payment run, not a rebate control.

So "unreachable" is not the test. The test is whether reaching it leaves the
system in a state the rest of the app can still reason about. The rebate
lifecycle passed that and shipped; these two did not.

The same reasoning shaped the inspections form: `POST /api/inspections` accepts
a body with neither `gr_id` nor `po_id`, but the matcher only ever reads an
inspection through a matched goods receipt or a PO-level row. An unlinked row is
invisible to the very match it was recorded for, so the form requires a receipt
rather than faithfully exposing what the endpoint tolerates.

---

## 97. Rendering "we cannot tell" is a feature, not an omission

**Decided:** 2026-09-05 · `frontend/src/routes/{audit,adaptive}/+page.svelte`

Three surfaces landed this round whose whole value is in *not* stating more than
the data supports, which is the same principle §34 recorded for the residency
alignment verdict and §84 for forecast variance.

**`unsigned` is not a smaller `invalid`.** The approval-signature sweep counts
them separately because a key rollout produces unsigned rows in bulk, and a UI
that merged them into one "problem" figure would report a configuration fact as
suspected tampering. They get separate cards, separate qualifying copy, distinct
badge tones, and only `invalid` is alarm-tinted. An unset signing key renders a
warning-toned notice *before* the counts, not a scary all-unsigned result.

**A threshold recommendation that moved is not an error.** The backend's stale
guard exists for this UI, so the rendered figure is always sent back as
`expected_recommended_threshold` — a guard the client doesn't feed is no guard —
and a 409 gets its own "the recommendation changed, nothing was applied" state
naming both figures, with the current one re-read underneath. "Apply failed"
would misstate both what happened and what to do next.

**Below the minimum sample, the feedback metric renders "not yet measurable".**
`0%` on a two-invoice sample reads as "the automation is never overruled" — the
most reassuring statement available, from the row carrying the least
information.

And the reads that write audit rows are loaded on an explicit act, never polled
or prefetched: a surface that logs the auditor looking at it must not log them
looking at it every thirty seconds.

---

## 98. The auth-coverage gate resolves dependencies by identity, and an allowlist entry must assert something

**Decided:** 2026-09-06 · `backend/tests/test_rbac.py`, `docs/authentication.md`

`test_rbac.py` is the one test that has to fail noisily when someone forgets
RBAC. For a long time it could not. It accepted any dependency whose closure was
*named* `checker` — the house style for all five factories in `deps.py`,
including the two billing entitlement gates. A name is not a security property
in either direction: renaming the closure broke the gate on every route at once,
while an unrelated function called `checker` that authenticated nobody satisfied
it. Alongside it, a single `NO_AUTH_REQUIRED` set asserted *nothing* once an
entry was listed. The two together were demonstrably exploitable — deleting
`Depends(get_scim_tenant)` from all six SCIM `/Groups` handlers, the credential
that creates accounts and grants roles, left the suite green.

The gate now walks `route.dependant` and compares against the actual callables
(`get_current_user`, `get_current_vendor_user`, `get_api_key_principal`);
`require_permission` is identified by its **code object**, one shared `__code__`
across every closure the factory returns. The allowlist splits by obligation, not
category: `PUBLIC_BY_DESIGN` entries are *driven* with a real credential-free
request and must not answer 401/403, so listing a protected route there fails
rather than exempting it; `ALTERNATE_AUTH` entries each name their specific gate.
Three companion tests refuse a stale entry, a route in both lists, and a route
allowlisted despite already having real auth.

Rejected: one list plus a comment convention — a comment is not an assertion,
which is exactly how the SCIM hole survived. **Known limit, accepted:** the
symbol check is static. It catches a gate going away; it cannot catch a gate
called and ignored. The per-route behavioural suites cover that, and the limit is
written down rather than left to be rediscovered.

---

## 99. Tenant engine construction is guarded statically, by discipline rather than by taint

**Decided:** 2026-09-06 · `backend/tests/test_tenant_engine_construction.py`, `.claude/hooks/security-patterns.sh`

`get_tenant`'s JWT org-claim cross-check is only load-bearing if it is reached,
and nothing asserted that it was: a tenant engine built from an interpolated URL
never asks the question. No static rule can prove a `db_name` came from a
resolved `Organization` row rather than from the request, so the guard proves the
weaker, checkable thing — every engine under `app/` is constructed through
`_make_tenant_url(db_name)` or `settings.database_url`, no engine URL is
interpolated, the constructor is never rebound or aliased, and no `feoh_`-prefixed
literal exists outside `config.py`.

Taint analysis was rejected as unmaintainable for the payoff. A blanket ban on
engine construction outside `app/database.py` was rejected because 31 legitimate
sites — background sweeps, dispatchers, webhook handlers — each need a
loop-local engine. The three Lambda handlers used to be exempt from the *helper*
(they cannot import `app.database` on a dotenv-free path — it reaches
`app.config`, which needs dotenv), held only to mirroring its body. **Round 25
deleted that exemption rather than re-documenting it:** the builder moved to a
dependency-free `backend/app/tenant_url.py` that `app/database.py` and all three
handlers import, so the guard now refuses the construction *anywhere* but that
module, and a second test pins the import-cleanliness the sharing depends on —
in a subprocess, because proving it in-process is vacuous once pytest has already
imported the app. Duplication guarded by inspection became duplication that no
longer exists.

The rebound-constructor rule exists because that evasion makes the guard go
*quiet* rather than red — `engine_factory = create_async_engine` removes every
call site from the enumeration. A guard whose failure mode is silence has to
assert against silence.

---

## 100. The audit-coverage guard's unit is the handler, and its route scan has a floor

**Decided:** 2026-09-06 · `backend/tests/test_audit_append_only.py`

Two properties of *how* invariant #3 is enforced are load-bearing and
non-obvious.

**The unit is the handler, not the module.** The sweep required `dispatch_audit`
anywhere in the router module, so one auditing handler vouched for every
unaudited handler beside it — `api/invoices.py` has 21 tenant-mutating routes
behind that single grep, and three unaudited DELETE handlers shipped there and
had to be found by hand in round 23. It now reads `inspect.getsource(endpoint)`
and follows calls to functions defined in the **same module**, so a handler
delegating to a local `_audit(...)` still counts. Exemptions are keyed
`(module, handler)`.

Calls *out of* the handler's module are deliberately not followed. "That other
file audits" is a claim about a chokepoint, and a claim belongs where a reader
can re-check it — not inferred by a source scan that would silently absorb the
day the chokepoint stops auditing. Rejected: a transitive whole-app scan (green
forever once any reachable path mentions `dispatch_audit`), and a runtime
call-every-route harness (needs a live tenant per route, turning the fastest
guard in the suite into the slowest).

**Route discovery asserts a floor.** FastAPI 0.138+ keeps nested routers in
`app.routes`, so the historical `isinstance(r, APIRoute)` filter saw 1 route out
of 564 and every filter built on it yielded `[]`. The swap to
`iter_route_contexts` fixes today; `_MIN_EXPECTED_ROUTES` is what fixes tomorrow.

Six handlers the re-keying exposed sit in `_OPEN_AUDIT_HOLES`, apart from the
real exemption dict and labelled as holes rather than decisions — widening the
exemption dict to reach green would have made a known gap indistinguishable from
a settled call.

---

## 101. The money-`Numeric` guard is opt-out, not opt-in

**Decided:** 2026-09-06 · `backend/tests/test_money_invariants.py`, `backend/docs/database.md`

The sweep sought `Float` only on columns whose *name* matched a token list
(`amount`, `total`, `price`, `subtotal`, `_tax`, `amount_`). That made the money
invariant opt-in: a column was protected only once someone spelled it a way the
tokens happened to cover. The list had drifted — 35 of 88 `Numeric` columns
matched no token, including every FX rate at `NUMERIC(18,8)`, all four
expense-policy thresholds, `contracts.spend_limit`, and `invoice_line_items.tax`
(the token carried an underscore) — so a new `Column(Float)` named `balance`,
`fee` or `*_rate` passed the whole file.

Inverted: **every** `Numeric` must declare precision and scale, and **no** binary
float may exist outside `NON_MONEY_FLOAT_ALLOWLIST` with a written reason. Adding
a money column costs nothing; adding a float costs an argument in writing. The
allowlist shipped **empty** — the schema has zero float columns — and a stale
entry fails its own test, since a leftover key would pre-approve a future column
of that name.

Rejected: a second, broader name-token sweep for a money column typed `String`.
Measured, a token list wide enough to be useful (`balance`, `fee`, `cost`,
`_rate`) flags 9 legitimate non-money columns, and an allowlist for *those* is
the same hand-maintained list this change deleted, one layer down. The `String`
hole is closed instead by an exact-name assertion over nine unambiguous money
names — zero false positives, so no allowlist — documented as a supplement, not
a completeness claim.

Also rejected: walking `Base.registry.mappers` after `import app.models`.
`usage.py` is not re-exported from `__init__.py`, so that walk silently skips it.
The sweep imports every module in the package and reads `Base.metadata.tables`,
with a non-vacuity floor — a guard that iterates nothing passes everything.

---

## 102. The SoD endpoint pin is derived from the app, not hand-listed

**Decided:** 2026-09-06 · `backend/tests/test_sod_endpoint_wiring.py`

The pin hand-listed the routes that must gate on `require_permission`. A
hand-list omits, and what it omitted was the worst possible subset: the eight
routes that actually move money (`runs/{id}/execute`, `{id}/void`,
`settlement/accept`, `compliance/release`, `compliance/dismiss`, `resume`,
`sync-erp`, `retry-failed`) plus the whole `/api/admin` `user.manage` surface —
leaving two of the eight catalogue permissions never imported by the guard at
all. Everything was correctly gated; nothing proved it stayed that way, on the
one class of route where the granular-permission layer existing at all is the
point.

The pin now walks the live app's `route.dependant` and asserts `CASES` ≡ the set
of `require_permission` routes, in both directions, and that every
`ALL_PERMISSIONS` entry gates at least one pinned route so a catalogue addition
wired to nothing is caught as dead config. It stores the **exact** any-of set
rather than "must include this permission", because a widening — adding
`payment.void` to the `/execute` gate — is as much an SoD failure as a narrowing.

Two deliberate exclusions, commented so they read as decisions: `runs/{id}/approve`
(CFO sign-off) stays on `require_roles` so it cannot be granted to whoever holds
the broader create-draft permission, and `/admin/roles` CRUD stays role-gated
because role CRUD *defines* permissions — gating it on one would let a custom
role mint itself everything.

---

## 103. The dashboard's top-vendor tile groups in SQL, and its ties are broken by name

**Decided:** 2026-09-06 · `backend/app/api/dashboard.py`, `backend/docs/analytics.md`

The tile selected five columns of every non-rejected invoice — all time, no
`LIMIT` — and folded them in Python, while the rollup, pipeline, aging and trend
blocks either side of it all `GROUP BY` in SQL and say so in their comments. Two
problems: the fold was a synchronous per-row loop inside an `async def`, the
shape the "blocking work does not run on the event loop" invariant forbids, on
the landing page; and it grew linearly with the invoice table while its four
neighbours stayed one aggregate each. Measured at 190 000 invoices: 505.7 ms, of
which 270.3 ms was the fold, against 28.5 ms for the equivalent `GROUP BY` —
17.7×.

Rejected: keeping the fold and offloading it with `asyncio.to_thread`, which
unblocks the loop but leaves the unbounded transfer and the linear growth; and
pre-aggregating into a stored per-vendor total, the compute-on-read discipline
the rest of analytics deliberately avoids. The rewrite reuses the `_rep_expr`
CASE already in scope rather than restating the conversion rule, so the tile
cannot drift from the figure directly above it — the same one-owner argument as
§82's budget rollup.

It also **tightened** the ordering. The fold sorted on converted total alone and
Python's sort is stable, so equal-spend vendors came back in DB scan order: which
took rank 10, and which fell off the `[:10]`, could differ between two identical
requests. The SQL orders `(total DESC, vendor ASC)`. A non-reproducible top-10 is
not a defensible answer to give a CFO twice.

An index was considered and refused: `EXPLAIN ANALYZE` shows a parallel seq scan
removing only 10% of rows, so a covering index would be near heap-sized and add
write cost on the tenant's hottest table for no plan change.

---

## 104. An index declared only in a migration reaches half the fleet

**Decided:** 2026-09-06 · `backend/alembic/versions/0092_list_and_audit_indexes.py`, `backend/app/models/`

Tenants are provisioned two ways: `alembic upgrade head` for existing ones, and
`Base.metadata.create_all` in `tenant_provisioning._create_tenant_tables` for new
ones. `create_all` builds exactly what the ORM declares, so an index written only
into a migration silently never reaches a freshly-provisioned tenant.

Migration `0010_audit_log_shipping` added `ix_audit_log_shipped_at_null` for the
shipper's `shipped_at IS NULL ORDER BY created_at` sweep and never declared it on
`AuditLog`. For 82 revisions a migrated tenant had it and a provisioned one ran
that sweep — per tenant, every 60 seconds — as a full sequential scan of its
largest table: 39.740 ms / 30 003 buffers at 1.2 M rows, to return nothing.

The rule: **every index is declared in both places, under one name.** The
migration is for tenants that already exist; the model `__table_args__` is for
the ones that don't yet. `test_list_and_audit_indexes.py` compares the two
against `pg_get_indexdef` on a real provisioned tenant, so a name, column order
or partial predicate that disagrees fails.

Rejected: a second index under a new name — it would have worked, and cost every
migrated tenant a duplicate partial-index write on every audit row forever.
Adopting the existing name means this revision must not drop it on downgrade: an
index a revision *ensures exists* is not one it *owns*, and the two lists are
kept apart for that reason.

Also settled: **no `CREATE INDEX CONCURRENTLY` in Alembic revisions.** It cannot
run inside the migration transaction, and paired with `IF NOT EXISTS` a cancelled
build leaves an INVALID index that every later run skips while reporting success.
Measured build cost is ~2.5 s for a 17-index revision at 1.2 M audit rows; an
operator needing less lock pre-builds by hand and verifies `indisvalid`, after
which the migration no-ops.

---

## 105. The SOX audit export streams, and is never capped

**Decided:** 2026-09-06 · `backend/app/api/audit.py`, `backend/docs/audit-log-shipping.md`

`GET /api/audit/export` materialised every row in the range as ORM objects and
built the whole body in memory; an annual range is tens of thousands of rows.

Rejected: a `LIMIT`, which makes the memory problem trivial and the endpoint
worthless — a silently short export is evidence an auditor signs off on, strictly
worse than a slow one. Rejected: deferring the `audit.exported` row until the
stream completes, which would lose the record of an aborted download even though
the access happened.

Chosen: a `yield_per` cursor over plain columns plus a chunked
`StreamingResponse`, with the range pinned to a snapshot from the *database*
clock — comparing against the process clock would drop or admit rows on skew —
so the export cannot contain the record of itself, and `COUNT(*)` +
`DISTINCT actor_id` folded into one aggregate so the range is scanned once. Peak
server allocation went from linear in the range (70.6 MiB at 20 000 rows) to flat
1.5 MiB, output byte-identical.

One consequence accepted here was **later reversed — see §110**: the request held
its tenant connection for the whole response. The other stands: the mechanism
depends on FastAPI keeping
`yield`-dependency teardown after the body drains — internal ordering, guarded by
a raw-ASGI test against a real database rather than assumed. CSV traded ~20% wall
clock, which measurement attributed to the *database* still sorting the range;
`ix_audit_log_created_at` (§104's revision) removes that sort node.

---

## 106. A detail modal is a list of one, and it is the request-identity case that bites hardest

**Decided:** 2026-09-06 · `frontend/src/routes/{vendors/screening,audit,experiments,admin/api-keys}`

The app-wide `createRequestSequencer` sweep landed on lists, so fetches that
*look* like a single read — a modal opened from a row, a drill-down opened from a
finding — were left unguarded. They are not single reads: "open A, close it, open
B" re-issues the same request with a different subject, and the failure is
invisible by construction, because the heading renders from the click and the
body from the response.

Rejected: comparing the response to the current selection inside each handler —
the same guard written four more times, and it gets the loading/error bookkeeping
wrong (`canCommit` vs `isCurrentRequest`). The rule is the one lists follow — one
sequencer per independent request stream — plus a cheap second obligation: the
panel exposes **both** ids in the DOM, the subject it was opened for and the id
the response claims, so a mismatch is assertable rather than inferred from
rendered content.

`/vendors/screening` settled the harder half. A modal that *acts* has a third
identity to bind beyond the heading and the body: the target of its buttons.
Block/Unblock read the live `selected` after its awaits and `rescreen` re-read it
after two, so a reviewer could read one vendor's sanctions timeline while the
control blocked another. The rule: an action captures its subject at click time,
and everything after the first await reads that capture, never the live
selection; its `busy` flag is keyed to that subject for the same reason.

Its history modal also gained a real error state ordered above the empty one —
"we could not look" must outrank "there is nothing", directly above a
Block/Unblock control whose decision rests on that timeline.

---

## 107. A payment states its currency, or states that it cannot

**Decided:** 2026-09-06 · `backend/app/schemas/payment.py`, `backend/app/api/payments.py`, `frontend/src/routes/payments/+page.svelte`

`payments` has no currency column and `payment_runs.total_amount` is one bare
`Numeric`. Both are legitimate — a payment settles in its invoice's currency, and
run creation 422s a run spanning more than one — but neither response carried the
code, though both already joined the invoice row for `vendor_name`. Every
`/payments` reader therefore rendered per-row money under the org's default.

The sharpest instance was the Accept-settlement dialog. It exists because a rail
can settle a different amount, or a different currency, than AP authorized, and
it puts "Authorized" beside "Settled". `settled_currency` was on the wire; the
authorized figure's currency was not. A EUR payment rendered a fabricated
`$1,200.00` directly above a real `€1,150.00` — on the screen built to catch
`currency_mismatch`.

Both responses now carry it, and where it cannot be **proven** the answer is
`None`, never a substituted default (§79/§82). The deciding case is a legacy run
predating the single-currency guard: its `total_amount` is a sum across
currencies, denominated in nothing real, so naming either leg's code would dress
a meaningless figure up as a genuine one. `_one_currency` is one shared rule
applied by the list, the detail and the create response, so the three cannot
publish different codes for the same run. The client renders an unprovable code
as a bare grouped figure.

Rejected: defaulting to the org's reporting currency (the fabrication being
removed), and converting on read (a rate fetched on a read makes a historical
figure move under the reader — §18).

**The guard is the assertion, not the field.** The e2e suite matched digits only,
so `$500.00` and `€500.00` were indistinguishable to it and this shipped unseen.
The payments specs now assert the currency **symbol**, and the settlement fixture
is EUR rather than the tenant's own currency — a same-currency fixture makes the
wrong rendering look right.

---

## 108. OFFSET paging needs a total order, and the guard for it is a source guard

**Decided:** 2026-09-06 · `backend/app/api/`, `backend/tests/test_pagination_total_order.py`

`OFFSET`/`LIMIT` paging is only coherent if the `ORDER BY` is a total order.
`created_at` is not one: it defaults to the transaction timestamp, so every row
written by a single transaction — an ERP sync page, a CSV import, one sweep tick
— shares it *exactly*. Postgres is then free to order the tied rows differently
between the two queries that fetch page 1 and page 2, so a row can be handed to
the caller twice or skipped altogether.

Two endpoints were reported (`api/exceptions.py`, `api/purchase_orders.py`,
found while indexing their tables in §104). Sweeping for the shape found **26**,
including four supplier-portal lists, the notification centre, credit memos,
cards and the SCIM user list. `exceptions.py` was the clearest case: its own
`/ids` select-all resolver three lines below the list already tie-broke on `id`,
so the list disagreed with the resolver it exists to feed. All 26 now append the
primary key.

**The guard is a source guard, deliberately, and that decision was forced by a
failed attempt at the obvious one.** A runtime test — write N rows in one
transaction so their timestamps tie, page through them, assert each appears once
— was written first and **passed without the fix**. At fixture scale Postgres
returns a stable scan order, so the test proved nothing and would have shipped as
a guard that never fails: precisely the vacuity §100 and §101 were about, and it
was discarded rather than tuned until it happened to go red. The manifestation is
planner-dependent; the *property* the correctness argument rests on is not. So
the guard asserts the property — an AST scan for `.offset(...)` chained to an
`.order_by(...)` whose keys are all timestamp-ish — and says in its own docstring
why it is not a runtime test.

It keys on `.offset(...)` rather than on `order_by` alone because a bare
`ORDER BY created_at` is perfectly correct on a `LIMIT 1` lookup, an aggregate,
or a whole-set fetch — nothing splits tied rows across two requests there. The
unnarrowed version flagged 62 sites, most of them fine; a guard that cries wolf
gets its exemption list padded until it means nothing.

---

## 109. A migration-only index is invisible to half the fleet, so parity is a test, not a habit

**Decided:** 2026-09-06 · `backend/alembic/versions/0093_migration_only_indexes.py`, `backend/tests/test_migration_model_index_parity.py`

§104 fixed one instance of this; an audit of all 216 `CREATE INDEX` statements
found **twenty**. A database here is built two ways and only one runs Alembic, so
an index written into a migration and never declared on its model reaches
migrated databases and silently never reaches a freshly-provisioned one. Nothing
notices — the reads still return correct rows, just by sequential scan — and
where the index is UNIQUE, the invariant it enforces is simply absent on half the
fleet. Verified against a tenant with no `alembic_version` row at all, and
against one stamped at head that was nonetheless missing every one: **"at head"
is not evidence the indexes exist.**

**Two were correctness, not performance.** `uq_positive_pay_run_format` is the
*only* concurrency control under the check-issue endpoint's read-then-insert —
without it the handler's `except IntegrityError` branch is dead code, two
concurrent calls both insert, and its own `scalar_one_or_none()` idempotency
lookup then raises `MultipleResultsFound` on **every later call**: a permanent
500 on that run, plus a second MinIO object carrying full account and routing
numbers. `uq_subscription_one_live_per_org` is the one-live-subscription-per-org
billing invariant that `uq_subscription_org_plan` does not bound, since two rows
for two different plans satisfy it.

**Two were not defects and are resolved the other way**, because declaring them
would build a genuine duplicate: `ix_bank_transactions_matched_payment` is the
exact column and predicate of the UNIQUE index 0081 added (which never dropped
it, so migrated tenants carried pure write overhead on a hot table), so 0093
drops it; `ix_vendor_change_requests_org_id` is the model's
`ix_vendor_change_requests_organization_id` under another name, so 0093 converges
on the model's spelling, creating before dropping so the column is never briefly
unindexed. Rejected: declaring both spellings, and `ALTER INDEX … RENAME`, which
is correct only on the one database shape that has the source and not the target.

0093 **ensures, it does not own** — restating an earlier revision's CREATE is a
catch-up for an already-provisioned database, so its downgrade drops none of the
eighteen (§104's `_ADOPTED` semantics). Where a UNIQUE index is involved it
**pre-flights and refuses** with a counts-only, PII-free message rather than
failing mid-revision: choosing which of two Positive Pay files went to the bank
is an operator's judgement, not a silent `DELETE` in a migration — the call §72
made for duplicate GL codes.

**Two test fixtures were themselves relying on the missing index** and only
surfaced once it existed: `test_dunning_sweep_resilience.py` seeded two or three
`past_due` subscriptions under one org, and `tests-e2e/billing/billing.spec.ts`
seeded a second `active` subscription for an org that already holds the `free`
one `tenant_provisioning` gives every org. Neither shape is constructible in
production, and with two live rows the endpoint under test picked one
arbitrarily — so those assertions had been passing by luck. Both now seed the
state the app actually supports.

The durable half is the guard, not the migration. New tenants keep being
provisioned by `create_all`, so a migration alone would leave the *next* tenant
in the state being repaired. The parity test is opt-out — every
`CREATE [UNIQUE] INDEX` in every revision, checked against the models, with
exemptions needing a written reason that is itself re-checked so it cannot rot.

---

## 110. The SOX export renders to a spool, so the connection is released before the last byte

**Decided:** 2026-09-08 · `backend/app/api/audit.py`, `backend/tests/test_audit_export_connection.py`

§105 traded a linear allocation for a flat one by streaming, and accepted that the
request would hold its tenant connection until the last byte was *transmitted*
rather than until the last row was *read*. Over a slow link that is an unbounded
hold on a `pool_size=5, max_overflow=10` pool, so several concurrent annual
exports could exhaust it.

Rendered chunks now drain into a `SpooledTemporaryFile` — in memory below
`_SPOOL_MAX_MEMORY_BYTES`, on disk above it — the session is closed, and the
response streams back from the spool. That threshold is what replaces §105's flat
1.5 MiB as the memory bound; nothing is materialised into a list and nothing
truncates. Cleanup covers all three exits: last block read, client hang-up
mid-download, and a render that raised.

The behaviour change worth stating: a database error part-way through now
surfaces as a clean 500 **before the first byte**, instead of truncating a body
the client had already begun saving. That is the same principle as the
un-terminated CSV chunk and the missing JSON `]` — a partial export must not look
like a complete one.

The test is the load-bearing part. It samples the **engine's own pool
checkout/checkin events** from inside the ASGI `send` callable at each body chunk,
and was verified to fail against the pre-spool route ("1 tenant connection(s) were
still checked out when the first csv body chunk was sent"). Byte identity is
compared against the module's independent materialising renderer over a
deliberately non-ASCII fixture, because the spool slices bytes, not characters.

---

## 111. A second factor being added is as audit-worthy as one being refused

**Decided:** 2026-09-08 · `backend/app/api/auth.py`, `backend/app/api/portal_auth.py`

MFA *step-up failures* audited; MFA enrollment *successes* did not, on either
surface. On an account that can stage a vendor bank change, the more
consequential event is the one that succeeded — a second factor added, replaced,
or removed changes who can authorise, and left no trace.

Four paths were missing (not the three the follow-up predicted — employee disable
was also unaudited): employee TOTP enroll-verify, employee MFA disable, portal
TOTP enroll-verify, portal MFA disable. The two passkey paths already audited and
were **not** duplicated; they joined the same vocabulary instead.

Two details that are the decision rather than the implementation. The row is
written **after** the commit, so the trail records factors that actually took
effect. And `replaced` is read **before** the write — afterwards every account
looks freshly enrolled, so the distinction between a first enrollment and one
that displaced a live authenticator is unrecoverable a moment later.

PII floor: the row carries the factor kind and, for a passkey, the API-visible
credential id — never the TOTP secret, the authenticator handle, the COSE public
key, or a recovery code.

---

## 112. A lapsed discount offer is lapsed by the calendar, not by a sweep having run

**Decided:** 2026-09-08 · `backend/app/services/discount_offers.py`, `backend/app/api/{discounts,portal}.py`

Offer *expiry* was gated behind `FEOH_DISCOUNT_OPTIMIZATION_ENABLED`, the
auto-capture kill switch, which is off by default — so lapsed offers stayed
`offered` forever and `capture_rate_pct` could read `100.00` on one captured of
ten, while the config comment and the docs both claimed only auto-capture was
gated.

The obvious fix — give expiry its own always-on sweep leg — was **rejected on
merit**. It would still leave the truth dependent on a process having run, which
is the exact dependency that caused the bug, and it is wrong in every context
where no sweep runs: tests, a one-shot CLI, `pnpm dev`. Instead `effective_status`
/ `effective_status_sql` compute it on read and every consumer classifies through
them; the sweep's write is demoted to a materialisation. This is §41
(`derive_run_status`) applied to a second subsystem, and it now spans three
surfaces — the AP router, the supplier portal, and the copilot tool.

Two things closing it exposed. The sweep's `expire_if_past` had been committing a
status change with **no audit row** — it now writes `discount_offer.expired`. And
`decline` accepted a lapsed offer and stored `declined`, asserting on an
append-only row a refusal nobody made; the guard went into the shared pure mutator
with a **required** `as_of` rather than a defaulted one, because a default is
precisely the completeness obligation §41 argues against, and because shipping it
on the AP side alone would have been a knowingly asymmetric control.

---

## 113. An importer's audit row belongs on the imported object's own trail

**Decided:** 2026-09-08 · `backend/app/services/{csv_import,vendor_sync}.py`, `backend/tests/test_audit_open_holes.py`

Six tenant-mutating handlers wrote no audit row (invariant #3), exposed by §100's
per-handler guard. Two calls in closing them are worth keeping.

**Per-created-entity, not per-batch.** A batch summary row in the handler would
have satisfied the guard while leaving every imported invoice with an empty
trail — and `import_invoices_from_csv` bulk-inserts `paid`/`done` historicals. The
invoice row is keyed on `Invoice.correlation_id` because that is the join key
`GET /api/audit/invoice/{id}` uses, so the row lands where an auditor looks for
it. This mirrors `gl_recode.bulk_recode_gl`.

**The audit cannot be written inline.** `Invoice.id`, `Invoice.correlation_id` and
`Vendor.id` are Python-side column defaults evaluated at INSERT, not at
construction, so an inline `dispatch_audit` produced rows with a NULL
`entity_id` — present, countable, and useless. Both importers and the ERP sync
accumulate and audit after one batch flush.

`_OPEN_AUDIT_HOLES` is retained but **empty**, with a docstring saying why:
folding a known gap into the exemption dict is how "not done yet" quietly becomes
"this is fine". Closing it also widened the scan to
`("dispatch_audit", "dispatch_auth_audit", "queue_auth_audit")` — four exemptions
had gone stale precisely because their handlers called a writer the grep did not
recognise, so they read as unaudited and their reason strings were never
re-validated. That is the same one-thing-vouches-for-another shape §100 was built
to remove.

---

## 114. The row checkbox meets the 24 px floor rather than resting on its neighbours

**Decided:** 2026-09-08 · `frontend/src/app.css`, `frontend/src/lib/a11y/targetSizeAudit.ts`

WCAG 2.2 SC 2.5.8 (AA) is satisfiable two ways: a 24×24 target, or the spacing
exception — a 24 px-diameter circle on the target touching no other target's
circle. `input[type=checkbox]` was globally 16×16 and passed only via the
exception.

The exception was chosen against, because *its satisfaction was owned by per-page
CSS*. Measured, `/invoices` had 7.0 px of clear space against a 4.0 px
requirement; `/exceptions` had 2.0 px, surviving only on the next cell's 14 px
padding. A conformance claim that flips when someone adjusts a column's padding,
in ten route files a global fix cannot reach, is not a claim worth making. The
24×24 route makes the verdict independent of neighbours.

The mechanism keeps the visual unchanged: a 4 px **transparent border** grows the
border box to 24×24 while the painted box stays 16×16, and a −4 px margin keeps
the *reserved* box at 16×16. Row heights are identical before and after
(46.84 / 44.00 / 38.25 px), which is the property that made a global change safe.

Recorded honestly: the agent that landed this **could not reproduce** an axe
`target-size` violation — its replica reported none before or after. What is
established is the arithmetic above, not a red-to-green transition. Radios stay
16×16 on the exception deliberately: they pass on every scanned route, and their
checked dot is carved with an inset `box-shadow` the transparent-border recipe
would have to re-derive, which is visual risk with no failing criterion behind it.

---

## 115. A deactivated entity stops taking new rows, but keeps giving up its old ones

**Decided:** 2026-09-08 · `backend/app/tenant.py`, `backend/tests/test_entity_scoping.py`

`get_entity_id` validated only that an `X-Entity-ID` **exists**, so a client
holding a stale header — one that had a subsidiary selected when an admin retired
it — kept filing new rows under it. `/entities` deactivates an entity precisely to
stop that.

The check goes on `get_write_entity_id` alone, not on the shared resolver. Reads
must keep resolving a deactivated entity or a retired subsidiary's invoices,
payments and audit trail become unreachable, which would make a deactivation a
soft delete — the opposite of its purpose.

It is a **409, not a 400**: the header is well-formed and names a real entity of
this tenant, so nothing about the request is malformed. What changed is the
entity's state, and the caller's remedy is to re-select, not to correct a field.
The refusal names the state and never the entity's name, so it carries no tenant
data.

The frontend half (the switcher dropping a retired selection back to the
consolidated view) is a convenience, not the control — it was written first, and
was a client-side guard over a server-side hole until this landed.

---

## 116. A challenge that mints a session is auditable; one that mails a code is not

The last two write-nothing entries in `_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT`
were both filed as "challenge issuance", and the call had never been made
either way. It is true of one of them.

`POST /portal/auth/mfa/challenge` does not issue a challenge — it **verifies**
the second factor and mints the session, the portal twin of
`api/auth.verify_mfa`, which has audited since it was built. And because
`/portal/auth/login` returns a challenge instead of a token once a supplier
enrols a factor, it is the **only** place an MFA-enrolled supplier's sign-in
completes: turning MFA on took that account's sign-in off the trail entirely,
in exchange for a stronger credential. The per-account failure budget that
throttles guessing there is a Redis rolling window — a brake, not evidence. A
portal account can stage a vendor bank change, so a campaign against its second
factor is exactly what an auditor looks for. It now writes
`portal.mfa.verify.success` / `.failure`.

`POST /portal/auth/mfa/challenge/email` genuinely is issuance, and stays
unaudited on three grounds now recorded in the dict itself rather than left as
"not yet":

* The auditable event is the code being **redeemed**, which the rows above carry
  as `method`. An issued-but-unused code is already visible as an absence.
* That endpoint answers 204 on every path precisely so it cannot reveal which
  supplier addresses exist **and** are enrolled. A row would be written for
  exactly that set, rebuilding the oracle inside a WORM-shipped trail.
* Its employee twin `api/auth.request_email_otp` is unaudited on the same
  terms. A stricter control on only the supplier surface is an asymmetry, not a
  control.

Two implementation details are really part of the decision. The recorded
`method` is derived from the branch actually taken, never echoed from
`body.method`: the portal schema leaves that field an unconstrained `str` where
the employee one pins `^(totp|email)$`, so restating it would put
caller-controlled text into an append-only trail **and** label the row with a
factor nobody used. And the success row is written **after** the session is
minted — `_mint_portal_session` lets its Redis failures propagate while
`dispatch_auth_audit` swallows its own, so the other order lets a blip leave an
immutable row asserting a sign-in that 500'd. That is §111's "written after the
commit", applied to a session instead of a factor.

---

## 117. The SOX export's control connection is released by a different mechanism than its tenant one

The spool (§110) closed the audit export's connection hold, and its guard
asserted exactly that — on the **tenant** session, which `_spooled_response`
closes explicitly. The **control-plane** session is never closed by that route
at all: it returns to the pool only because `commit_before_response` ends its
read transaction on the exit stack FastAPI unwinds before sending (§20).
Nothing pinned that, and the control pool is shared by every tenant on the
process, so a control read added after the spool would be worse than the tenant
hold §110 had just fixed.

Verified by degrading that hook to its no-op return: the tenant assertion still
passes while control reads one connection, still in transaction, at the first
body chunk. `format=pdf` gets its own case for the third variant of the same
obligation — it cannot stream, so it materialises, but it must close the tenant
session **before** the ReportLab layout rather than hold it across.

---

## 118. The dashboard's discount tile groups by a CASE, and its half-cent ties moved to ROUND_HALF_UP

`api/dashboard`'s discount-capture block was the last unbounded fold on the
landing page: every discount-scheduled invoice (`WHERE discount_percent IS NOT
NULL`, no `LIMIT`, no date bound) streamed into a synchronous Python projection
inside an `async def`. It was left out of the round that fixed the five
vendor-spend folds, on the reasoning that this one needs a per-**row** verdict —
captured / missed / still-capturable — rather than a per-currency sum, so it
could not become a plain `GROUP BY`.

That reasoning was right about a per-currency `GROUP BY` and wrong about the
conclusion. The verdict is **two comparisons**, so it groups by a `CASE` and
three rows come back. What stays in Python is what was never per-row — the
bucket vocabulary, the `captured / (captured + missed)` rate, the
`insufficient_data` state — as `analytics.discount_capture_from_grouped_rows`,
mirroring `currency_conversion.rollup_from_grouped_rows`.

**Rejected: bounding the tile by period.** It was the other option on the table
and it is cheaper, but it answers a different question. "Discounts captured this
quarter" is not "discounts captured", and the tile carries no period control for
a reader to reconcile the two. Narrowing a figure's population to make its query
cheaper is a silent redefinition.

Two details are load-bearing, and one changes a number.

**The rounding tie-break moved.** The figure is rounded to cents per row, where
the fold rounded — quantize-then-sum, not sum-then-quantize. But Postgres
`round(numeric, 2)` breaks an exact half-cent away from zero where
`Decimal.quantize`'s default context broke it to even, so 2.50% of 399.40
(= 9.985) goes from `9.98` to `9.99`. This is an **alignment**, not a drift:
`ROUND_HALF_UP` is the documented convention and it is what
`discount_offers.discount_savings` already returns for this identical quantity —
base × percent ÷ 100. The dashboard was the one place computing it the other
way. Pinned by a boundary test, because a deliberate convention change with no
test on the boundary is indistinguishable from an accident.

**`AT TIME ZONE 'UTC'` before the date cast is not decoration.**
`payments.completed_at` is `timestamptz` and asyncpg handed Python a UTC-aware
datetime, so `.date()` was the UTC date. A bare `::date` reads the Postgres
**session's** TimeZone, so a payment at 23:30 UTC on its deadline would miss its
own discount window on any server configured east of UTC — the class of bug
`utils/dates.utc_today` exists to prevent, reaching SQL for the first time.

---

## 119. `configured_home_currency` exists because `resolve_home_currency` cannot say "unset"

`currency_conversion.resolve_reporting_currency` was the fourth reader of
`settings.payments.home_currency` and the last still reaching into the settings
JSON itself. The recorded fix was two lines: call the hoisted
`international_payments.resolve_home_currency` as its second candidate. That
would have broken it.

`resolve_home_currency`'s entire job is to never answer "unset" — it substitutes
`DEFAULT_HOME_CURRENCY`, because the payment path has to pick something and a
blank code compares equal to nothing. But `resolve_reporting_currency` is a
four-rung chain and this setting is only rung 2. A rung that always answers makes
rungs 3 and 4 unreachable: an org whose only currency signal is
`invoice_defaults.currency` would silently start rolling up in USD — a worse bug
than the duplication being removed, and invisible, since every test asserting
rung 2 would still pass.

So the primitive hoisted underneath both is the one that can abstain:
`configured_home_currency(org_settings) -> str | None` — the setting normalised,
or `None` when the tenant has not usably set one — and `resolve_home_currency`
becomes `configured_home_currency(...) or DEFAULT_HOME_CURRENCY`.
`international_payments.py` is now the only file under `app/` that reads the raw
setting, the source-scan allow-list is down to one entry, and a fall-through test
pins the behaviour the naive de-duplication would have lost.

The general shape, worth naming: **two callers wanting the same value can still
need different answers for "absent".** Sharing the normaliser is right; sharing
the *defaulting* is what breaks a resolution chain. Hoist the layer that returns
`None`.

---

## 120. One refusal predicate set, and a rail-conditional refusal is not a block

`GET /api/payments/queue` derived its `blocked` flag from one of the four
per-invoice refusals `create_payment_run_for_invoices` enforces. The failure
mode is specific and expensive: a run is refused as a **whole**, so one unmarked
row 409s a forty-invoice select-all and the operator has no way to bisect which
row did it. §41's lesson applies — the list of special cases was the defect, not
its length.

`payment_runs.run_refusal_reasons()` is now the single answer to "would a run
refuse this invoice, and why", and the builder consumes it too (its four inline
gates are gone). **Rejected:** leaving the builder's gates in place and having
the queue call a read-only copy. That is exactly the drift this entry was raised
about; the queue can only be trusted if the enforcer reads the same function.

**A live virtual card pins the rail; it does not block the row.**
`virtual_card` *converges* onto the existing card rather than opening a second
outflow, so it is the one rail a card-claimed invoice is still payable on — the
documented mint-then-run flow. Marking the row `blocked` (the simpler answer,
and the one a uniform boolean pushes you toward) would have disabled its
checkbox and removed that flow from the product. So `RunRefusal` carries
`only_method`, derived from `CARD_CONVERGING_METHODS` rather than restated; if a
second converging rail is ever added it resolves to `None` and the refusal reads
as unconditional — the fail-closed direction, since a queue offering "one of
these two rails" it has not learned to render is worse than one that refuses.

A consequence worth stating because it looks like a bug: **`blocked_total` and
`selectable_total` are not complementary.** A pinned row leaves the selectable
set (bulk-select stages the default rail) while its checkbox still works, so
`blocked_total` is counted in its own right. Deriving it as
`total - selectable_total` would report a perfectly payable row to the operator
as something to go and clear.

---

## 121. A page-level text-entry recipe carves out the controls it is not for, rather than being out-specified

`/organization`'s branding swatch was rendering 478 px wide — the entire grid
column — squeezing its sibling hex field to 22 px and failing WCAG 2.2 AA SC
2.5.8. The cause was a **specificity tie**, not a size mistake: §114's checkbox
fix added `:not([type='checkbox']):not([type='radio'])` to that page's `input`
recipe, taking it from 0-0-1 to 0-2-1, which ties
`.color-field input[type='color']` (`width: 40px`, also 0-2-1) and sits later in
the file. The rule that had always lost stopped losing.

**Rejected: `min-width` on the hex field.** It was tried and measured, and it
made things worse — the swatch stays 478 px and the floor pushes the flex line
64 px outside its grid cell, so axe moved from *insufficient size* to *partially
obscured*. A floor on the child cannot fix a sibling that has taken the whole
row. **Rejected: a longer selector on `.color-field`.** It wins today and ties
again the next time anything is added to the base recipe.

Taken: carve `[type='color']` out of the recipe, because it is the same fact as
the checkbox carve-out — a **text-entry** recipe reaching a control that is not a
text entry, declaring a padding and a width meaningless for a native swatch.
Measured after: swatch 40 px, hex 430 px, no overflow, `.color-field`
`scrollWidth == clientWidth`.

The general rule: **when a recipe and a specific override tie, widen the
carve-out, don't lengthen the override.** Ties are created by editing the recipe,
so that is where the repair belongs.

---

## 122. A shared test fixture is not the creating spec's to delete

Four e2e specs were given teardowns for the rows they create, and the first
attempt also deleted the vendors that manual invoice entry provisions. It failed
on a foreign key, and the reason is the useful part:
`vendor_matching.match_and_link_vendor`'s third leg is a normalized-name
Jaccard, so a *different* spec's invoice had been linked to the same vendor row
— `invoices/file-management` types "E2E File Mgmt Vendor" and lands on
`create-manual`'s "E2E File Vendor".

The distinction the teardowns now encode is **bounded vs unbounded**. An invoice
number carrying `Date.now()` is a new row every run and must be cleaned. A
vendor resolved by a fuzzy matcher is one row per distinct name, re-matched
thereafter, and is shared by whoever types a name close enough — a fixture, not
a leak. Only the unbounded rows are a leak, and no single spec owns the others.

The foreign-key refusal is also why the teardowns delete rather than NULL a
link: a `NULL`-the-FK "fix" would have silently rewritten another spec's invoice
instead of failing.

---

## 123. `target-size.spec.ts` bites — established by restoring the pre-fix CSS, not by assertion

§114's target-size work shipped a regression guard that had never been executed,
because ten agents shared one port that round. Running it settled the question:
restoring the pre-fix checkpoint block byte-for-byte fails **five of six**
cases, with real measurements (target 16 against the 24 floor, three times;
painted box 14 against 16; `outline-style: none`).

The sixth did not fail, and that is the finding. "A click outside the painted
box toggles the row" asserted only `insetX > 0`, which a 1 px opaque border also
satisfies — so the click landed on the paint and the test re-proved that
clicking a checkbox toggles it. It now asserts the inset reaches `(24 - 16) / 2`
and that the click point is outside the paint, which is the claim it was named
for.

A guard that has never been run is a guard whose coverage nobody knows. The file
header records the experiment so the next reader does not have to re-derive
whether these assertions bite.

## 124. The editable-install finder is appended, so `PYTHONPATH` wins and the failure is a fall-through

Two source-of-truth notes asserted that `backend/.venv`'s editable install beats
`sys.path`, so a worktree could not override it. Both were wrong, and the
correction changes which commands are dangerous.

`__editable___backend_0_1_0_finder.install()` ends in
`sys.meta_path.append(_EditableFinder)` — **append**, so it lands *after*
`PathFinder`, the finder that reads `sys.path`. `PYTHONPATH` therefore does win.
The real failure is a **fall-through**: when `<worktree>/backend` is on
`sys.path` you get the right tree, and when it is not, `PathFinder` finds nothing
and the appended finder answers with the primary checkout.

That reclassifies every entry point. `python main.py` (script dir on `sys.path`)
and `pytest` (which prepends the rootdir) were always correct. `python
scripts/seed.py` (`sys.path[0]` is `scripts/`), `alembic`, `uvicorn`, and
`pytest --import-mode=importlib` were always wrong. The worst of those is
`alembic revision --autogenerate`, whose output is a plausible-looking migration
diffed against a different checkout's models.

`pytest`'s safety is also **incidental**: it depends on `prepend` import mode
walking up past `tests/__init__.py`. The recommended invocation —
`PYTHONPATH=<primary-venv-site-packages> /usr/bin/python3 -m pytest` — is safe
for a stronger reason: `PYTHONPATH` directories get no `.pth` processing, so the
finder is never installed at all. There is nothing to fall through to, and a bad
`sys.path` becomes a loud `ModuleNotFoundError` instead of a silent wrong tree.

Three mechanisms now cover the class, and they are not redundant:

* **Per-script anchors** — every `backend/scripts/*.py` that imports `app` puts
  its own checkout on `sys.path` first. Correct *by default*, which is what the
  shim structurally cannot be.
* **`alembic.ini`** — `prepend_sys_path = %(here)s`, not `.`. The value is spliced
  verbatim and resolved against the **cwd**, so `.` would have pointed at the
  repo root. `path_separator = os` is not cosmetic either: the legacy fallback
  splits on spaces as well as commas, so a checkout under a path containing a
  space would become two nonexistent directories.
* **`scripts/worktree/sitecustomize.py`** — covers what we cannot edit
  (`uvicorn`, `--import-mode=importlib`), and is the only one that *removes* the
  finder, so a name it cannot fix raises rather than importing quietly.

The guard globs the directory and derives its exempt set as the complement of
"imports `app`", so a new script inherits the rule instead of being forgotten.
It also asserts it found something: every case is parametrized, and a broken glob
would collect zero cases and read as a pass.

## 125. An absent KPI figure is a dash; *pending* is announced, not drawn

`aggMoney(undefined, …)` formatted `0`, so money KPIs flashed a zero before their
response landed — a figure nobody had computed, displayed as a computed one
(§34's class). The fix had to be one convention on `KpiCard`, because fixing one
page would have left its own five-card row inconsistent.

An absent figure renders the em dash. Absence is keyed on **nullish**, never on
falsiness, so a genuine computed `0` still renders `0`.

Whether the figure is absent because it is *still arriving* is carried in the
accessibility layer alone: a `pending` card looks identical but sets
`aria-busy`, hides the dash from the accessibility tree, and puts
screen-reader-only text in its place. Two glyphs would tell a sighted reader
nothing — the difference is only actionable to someone who cannot watch the card
settle. A pending or unavailable card also drops its highlight tint, because the
tint is a *verdict* and there is no figure to have a verdict about.

Rejected: a shimmer skeleton, because the em dash is already this app's glyph for
an unknown and is already inside `formatMoney`'s placeholder. A second vocabulary
for the same fact would read as a *different kind of nothing* beside a sibling
card legitimately showing a dash. `kpiValue.test.ts` pins `KPI_NO_FIGURE ===
formatMoney(null)` so the two owners cannot drift. Also rejected: `aria-live`, as
a KPI row settles every card at once and would fire a burst of announcements over
whatever the reader was on.

`pending` is a prop rather than something the component infers, because only the
page knows whether its fetch is in flight. The `/bank-reconciliation` adoption
validated the split: it reused the page's existing loading flag with no new
state, and because the component consults `pending` only when the value is
actually missing, a filter or search re-fetch keeps the figures on screen instead
of blanking them.

The sharpest case was not the flash. `/bank-reconciliation` derived its red tints
from `(… ?? 0) > 0`, so before its data arrived the page rendered *untinted* and
positively claimed nothing was unmatched and there were no discrepancies — a
clean bill of health on a fraud control, asserted while still asking.

## 126. A supplier sign-in with a second factor is still a sign-in

`portal.login.success` first shipped on the password-only path, with the MFA path
writing only `portal.mfa.verify.success`. That is not a gap, it is a **biased
sample**: an auditor querying successful supplier sign-ins would have silently
missed exactly the accounts carrying a second factor, and nothing in the result
would have said so. It is why the hole was not half-fixed when the first row
landed.

The two rows answer different questions. The factor row says *which* second
factor was used; it does not say a session was minted. Whether a sign-in is
recorded at all must not depend on whether the account happens to be enrolled.
The employee twin already wrote both, so the portal is reaching parity rather
than inventing a convention.

The row is written **after** `_mint_portal_session`, not before. Minting
registers the session in Redis and lets its failures propagate, while the audit
dispatch swallows its own — so auditing first can leave a permanent, immutable
row asserting a completed sign-in for a request that returned no token (§111's
principle, and the order `api/auth.verify_mfa` uses).

A successful challenge now writes two rows, so the tests' single-row unpacks no
longer say what they used to. They select by action through a helper that keeps
the "exactly one" assertion the unpack was worth writing for. The failure-path
unpack is untouched: a refused factor still writes one row and no sign-in row,
which now has its own test.

## 127. Three teardown owners, and the third takes a prefix rather than a predicate

`deleteInvoicesWhere` earned its place because a bare delete only succeeds while
the row happens to have no children. `vendors` and `workflow_definitions` were
the same trap one table over — 18 hand-rolled call sites and ~10 duplicated
purges respectively, each maintaining its own partial child list.

`deleteVendorsWhere(predicate, slug?)` matches the original's shape and delegates
to it rather than restating the invoice graph. Rows that exist only because of
the vendor are deleted; independent records that merely point at one of its rows
have the link cleared.

`deleteWorkflowsWhere(namePrefix, slug?)` deliberately **does not** take a
predicate. A tenant's seeded default must survive every teardown, and so must the
fallback definition the engine mints when an org has none active — so the
`is_default = false` seatbelt belongs to the helper, not to each caller. A
free-form WHERE would let one caller write the teardown that empties a tenant.

Two consequences worth recording. The guard's failure message is now per-table,
because a single template telling the next author to pass a predicate would send
them to write exactly what the guard had just refused. And deriving the vendor
graph turned up a latent bug in the *original* owner: it deleted `virtual_cards`
without first clearing `card_rebates`, so an invoice whose card had earned a
rebate would have failed teardown in precisely the way the guard exists to
prevent.

## 128. An unordered `LIMIT 1` is dangerous only when the candidate set is too wide

`cards/lifecycle.spec.ts` picked a payable invoice with `LIMIT 1`, no `ORDER BY`
and no vendor filter, while `generate_cards` skips any invoice with no screenable
vendor and returns a cheerful `201` with an empty list. On a tenant holding three
vendorless payable rows stranded months earlier, the spec deterministically drew
one and asserted against nothing.

The fix is an ordering that is a *total* order (the primary key), plus the two
decline conditions SQL can state honestly, plus a named non-empty assertion so an
exhausted fixture fails loudly instead of passing vacuously.

The generalisable part is the discriminator, because the idiom appears at roughly
twenty sites and most are fine: **an unordered `LIMIT 1` is only dangerous when
the candidate set is wider than what the code under test accepts.** Picking any
active vendor and then creating your own invoice against it is safe — every
candidate is equally valid. Picking any *payable* invoice when the endpoint
accepts a strictly narrower set is not, and it is worse when the endpoint
declines with a success status, because nothing surfaces as an error.

## 129. The seed entitles one demo tenant, and deliberately leaves the workers unentitled

Every seeded org landed on `free`, whose entitlements are `{}`, so
`require_api_entitlement("public_api")` returned 402 for the whole suite and for
local dev. The public Developer API was unexercisable end to end without
hand-editing the control plane, which guard rail 7 says it must not be.

`acme` now seeds onto a plan carrying `public_api`. `techflow` stays on `free`
**on purpose**: a seed where everyone is entitled makes the 402 as unreachable as
the 200 used to be, and the refusal path deserves local coverage too.

Every `e2eN` worker also stays on `free`. Worker tenants are interchangeable by
design, so entitling one would make *which shard drew which tenant* observable
from a test's behaviour. A `scripts/` toggle was rejected for the opposite
reason: an opt-in command leaves the surface dark by default on a fresh clone,
which is the thing guard rail 7 forbids.

Seeding the plan was only half the fix. `ensure_subscription` no-ops once an org
holds any live subscription and `seed_control_plane` returned early on an
already-seeded control plane, so no number of re-runs could repair a dev box that
was already stranded. The baseline now runs on both branches of that guard and
**repoints** the existing live row rather than inserting a second one that
`uq_subscription_one_live_per_org` forbids.

That repoint exposed a latent collision shared with `change_plan`: a *canceled*
row still occupies the `(organization_id, plan_id)` slot, so re-subscribing to a
previously-cancelled plan raises. The three writers now share one owner whose
primitive is narrower than any of them — *free the slot so it can be occupied* —
because one INSERTs and one repoints, so neither could delegate to the other. The
guard was never missing from `change_plan`; that call site is de-duplication, not
a fix.


## 130. Closing a fail-open dual control without refusing every genuine request

`approve_change_request` enforced the BEC dual control with one comparison —
`req.requested_by_user_id is not None and req.requested_by_user_id == user.id` —
above a comment stating the assumption it rested on: *"Portal-submitted requests
have no AP requester, so this only bites AP-initiated ones."* That assumption is
false, and the code falsifying it is two routes away.
`POST /vendors/{id}/portal-users` is `require_roles(ADMIN, AP_MANAGER)`, took the
supplier's address from the caller, and handed back the plaintext password.
`ROLE_AP_MANAGER` holds `vendor.manage`, `vendor.bank_change.approve` **and**
`payment.execute`. One person could invite a portal user at an address they
controlled, sign in as it, stage a bank redirect (filling
`requested_by_vendor_user_id`, leaving `requested_by_user_id` NULL), approve their
own request through the NULL short-circuit, and pay it.

The interesting part is not the hole; it is that the obvious repairs are all wrong.

**Rejected: fail closed on NULL.** Refusing every request whose AP requester is
unknown closes the hole in one line, and refuses every genuine supplier-submitted
bank change forever — the *normal* case, and the whole point of the supplier
portal. A control that fires on 100% of legitimate traffic is not a control; it is
an outage with a security rationale.

**Rejected: forbid AP from creating portal identities.** Onboarding a supplier is
a real, required AP task. Removing it moves the problem rather than solving it.

**Rejected: rely on the compensating `fraud_flag`.** It is a signal, not a second
control against the same actor: exception resolution has no segregation check
either (recorded in `followups.md`), so the attacker clears their own flag.

**Not defended against, deliberately: two colluding AP actors.** A invites
(stamp = A), B later resets the password (stamp = B) and hands the credential to
A, so the request freezes B and A may approve. That needs a second authenticated
party, which is the boundary every dual-control scheme accepts. The fix closes the
SINGLE-actor path, which is the one that existed.

**Chosen: record who holds the credential, and refuse that person as approver.**
`vendor_users.provisioned_by_user_id` names the control-plane `User` who last
minted a password for a portal identity, stamped by invite AND by
`POST .../reset-password`. Finding the second route mattered: reset is the same
attack against a supplier who already exists, and the follow-up entry had not
named it. Those two are exhaustive — the only writers of
`VendorUser.hashed_password` outside the supplier's own change-password.

That exhaustiveness is what makes NULL safe to keep permissive. NULL does not mean
"we don't know"; it means *no AP actor in this system has ever held this
credential* — a legacy row, or a supplier who has always managed their own
password — and an AP actor cannot come to hold one without passing through a route
that stamps the column. The fix refuses precisely the requests where a second pair
of eyes is provably absent, and touches nothing else.

Three details are load-bearing, each chosen against a simpler alternative:

- **The value is COPIED onto the change request at staging
  (`requester_provisioned_by_user_id`), not joined at approval time.**
  `DELETE /vendors/{id}/portal-users/{id}` carries no FK into
  `vendor_change_requests`, so a join would let the approver delete the portal
  identity between staging and approving and walk their own request through — the
  hole reopened by one extra request. This is the frozen-snapshot discipline
  `WorkflowInstance.steps_config_snapshot` uses, for the same reason: the evidence
  must not be re-derivable from mutable state the actor controls.
- **The supplier changing their own password does NOT clear the stamp.**
  `POST /portal/auth/change-password` requires the *current* password, which the
  provisioner has. Clearing there would be a one-request bypass of the whole
  control. The stamp is durable provenance, not a "who knows the password right
  now" flag. The cost is real and accepted: an AP user who onboarded a supplier is
  permanently not an independent second party to what that identity submits.
  `ROLE_ADMIN` holds `vendor.bank_change.approve` by default, so no tenant is left
  with nobody able to approve.
- **The plaintext temp password left the response, and delivery became
  mandatory.** Returning it for a caller-supplied address made "mint a supplier
  login I control" one frictionless request with no delivery record. It is emailed
  only now, and a send failure rolls the request back (502) rather than leaving a
  portal account whose credential nobody received. This is **defence in depth, not
  the fix** — an AP actor who supplies their own address still receives the email.
  The approval refusal closes the chain; this removes the silent path.

Migration `0095` is additive, nullable, idempotent, tenant-gated, and does **no
backfill** — we cannot know who provisioned a pre-existing credential, and guessing
would either manufacture a refusal or manufacture an absolution.

## 131. A NULL uploader means "no employee made this", and that is enforced elsewhere

`approval_chain.violates_segregation` returns False — no breach — when
`Invoice.uploaded_by_id` is NULL. Read on its own that is fail-open on a fraud
control, which is how `services/csv_import` shipped a real hole: the `import-csv`
route already had the user and already spent it on the `invoice.imported_csv` audit
row, but the `Invoice(...)` constructor never passed it. An AP manager could import
a payable at `new` and approve it on the next click, while the same person doing the
same thing through `POST /api/invoices` got a 403 — from a line whose comment names
this exact failure.

The obvious durable fix is to flip the NULL branch closed. That was rejected. Four
ingestion paths legitimately have **no control-plane user to record**: email intake
and inbound PEPPOL are system ingestion; supplier-portal submit and the portal PO
flip are driven by a tenant-scoped `VendorUser`, and `uploaded_by_id` names a
control-plane `User`; the recurring sweep is a timer. Failing closed makes every
invoice from all four permanently unapprovable — an outage across the platform's
four non-interactive front doors, traded for a control achievable a better way.

The better way is to make the premise true rather than check it at approval time.
NULL is sound precisely when no *approver-capable employee* can create a row without
being recorded, so every employee-facing path stamps the actor: manual create and
file upload already did; CSV import now does; so do `recurring_invoices.generate_one`
(`user.id` on the manual `generate-now`, `None` from the sweep) and
`intercompany.route_intercompany_invoice`, whose mirror payable is now attributed to
whoever routed it. Both already had an `actor_id` in the signature, spent on the
audit row and nowhere else — the follow-up entry claiming they had "no creator column
to fix with" was wrong about two of the three cases it named.

What failed here was not the predicate but that its premise was *assumed*. So the
premise is enforced by `tests/test_invoice_uploader_stamping.py`: it walks the syntax
tree of every `Invoice(...)` construction under `app/`, requires the `uploaded_by_id`
kwarg at each, and requires any site passing a literal `None` to be declared with a
reason. Omission — the failure mode that shipped, and the one that reads as an
oversight rather than a policy — is no longer expressible. A first version of the
guard matched on module path and was blind to the equally normal
`from app.models import Invoice` re-export spelling; it now matches the imported name
and cross-checks at the token level, because a guard with a spelling-shaped hole is
worse than none.

One residual gap is recorded rather than closed: a **sweep-generated** recurring
invoice does have an employee author — whoever created the template — that we have
nowhere to put, because `RecurringInvoiceTemplate` carries no creator column.

## 132. A best-effort leg needs a verdict on the response and a remedy on the operation

Voiding a `virtual_card` payment has two best-effort legs — the payment rail and the
card provider — because a provider outage must not block the accounting void. That
posture is right, and it created two separate holes.

**A best-effort leg that reports only to the audit log is invisible.** The card leg's
`card_outcome` reached `PaymentResponse` in an earlier round, but nothing rendered
it, so a clean 200 and a "Payment voided" toast looked identical whether the card was
dead at the provider or still live and bearer-spendable behind a payment the ledger
calls `voided`. The audit row is evidence for an auditor later; it is not a channel
to the operator now.

**The UI branches on a verdict, not on the outcome vocabulary.** Enumerating tags
client-side fails in one specific direction: a tag added later falls through to
whatever the client's `else` is, and the tempting `else` is "fine". So the
classification lives once, beside the function producing the tags —
`card_cancel_disposition` → `closed` / `no_card` / `not_closed_final` /
`not_closed_retryable` — and classifies an **unknown tag as retryable**.
`card_already_charged` earns its own `not_closed_final` rather than collapsing into a
neighbour: the card was NOT closed (so it must be shown) and never can be (so
offering a retry would be a control that cannot work). `None` stays `None` — "we
never asked" is not "it is shut" (§34).

**The remedy sits on the operation, not beside it.** §96 declined a standalone Cancel
because `POST /api/cards/{id}/cancel` is reachable on a *live* payment, where it kills
the card while that payment and its invoice still claim money is in flight. That
objection is about the *state the control is reachable in*, not about closing cards,
and it evaporates once the payment is already `voided`. So
`POST /payments/{id}/void/retry-card-cancel` 409s on anything but an already-`voided`
`virtual_card` payment: it can only ever **finish** a reversal the books already
recorded. It gates on `payment.void` — the permission of the void it completes — not
the card router's bare roles, which would hand an `ap_manager` in a duty-split org the
half of the reversal the org withheld.

**Idempotency here is a lock and a status check, not a key.**
`build_card_idempotency_key` exists because a *create* that times out after the
provider acted is unrecoverable without one. A cancel is not a create: every adapter
already treats an already-closed card as a confirmed cancel, and
`cancel_card(provider_card_id)` takes no key. What was missing was on our side — the
card row is taken `FOR UPDATE` and an already-`cancelled` row short-circuits with no
provider call and no second `card.cancelled` row. A second retry is a no-op, not an
error.

**Every attempt is audited, not only the successful ones.** The question an operator
asks is "did anyone chase this card", and a trail recording only what changed cannot
answer it. That is the opposite call from `/settlement/accept`, which refuses a
repeat: an *acceptance* is a decision recorded once; an *attempt against a third
party* is worth recording every time.

Also fixed in passing: `_cancel_card_for_void` selected the card with an unordered
`LIMIT 1` over `payment_id`, which is not unique (a cancel-then-reissue leaves the
dead row behind). Postgres could return the cancelled row and the function would
report `card_already_cancelled` while the spendable card stayed open — the precise
failure it exists to prevent. It now orders live-first, then newest. §128's rule
again: an unordered `LIMIT 1` is dangerous exactly when the candidate set is wider
than one.

## 133. `networkidle` is banned as a readiness signal, with one exception

`page.waitForLoadState('networkidle')` waits for 500 ms of no network activity.
Playwright discourages it, and round 27 measured what it costs here: the
`organization/` e2e directory failed 10 runs in 75 (13.3%) on that call alone, every
failure a 30 s timeout whose screenshot showed the panel already fully rendered. It
is not a readiness signal — it never asserts that anything was drawn — and on a page
that polls, streams, or holds an open connection it may never fire at all. Round 28
removed 243 of the 250 call sites in the suite.

Rejected: raising the timeout (masks it, and the rule against inflating a Playwright
timeout exists for precisely this), `waitForTimeout` (a sleep, forbidden by the guard
rails), and `domcontentloaded` (a weaker heuristic, still not a statement about the
thing under test).

The replacement is whatever the test actually depends on, and the auto-waiting
assertion on the next line is almost always already it. That is stronger than it
sounds: `routes/+layout.svelte` renders **nothing** until its `browser`-guarded
`$effect` resolves `hasTenant`, and effects run during neither SSR nor prerender, so
no route's markup exists in the served document. Waiting for any element in the app
shell therefore transitively proves hydration completed, which is more than a quiet
network ever proved.

Three shapes need a real substitute rather than a deletion, and one keeps the wait:

1. A bare `.count()` or `page.evaluate()` on the next line has no auto-wait — gate on
   the element the read depends on.
2. An **absence** assertion (`toHaveCount(0)`) passes vacuously against an empty
   document. Order a positive assertion before it, or wait on the request that
   populates the state being asserted about — a role-gated absence check waits on
   `GET /api/auth/me`, because that is what populates `auth.hasAnyRole` / `auth.can`.
   The wait was hiding a test that could not fail; these replacements are strictly
   stronger than the heuristic they retire.
3. Asserting that **no further request** was issued is the one honest use:
   `waitForResponse` proves at least one fired, never that a duplicate did not, so a
   quiet network is the only signal for "nothing more is coming".
   `credit-memos/load-sequencing.spec.ts` keeps its two, documented inline.

**There is no hydration exception, and the belief that there was one cost two
rounds.** `fixtures/helpers.ts` long carried a comment marking its sign-in waits
load-bearing: Svelte 5 wires `onsubmit` and `bind:value` only at hydration, so a
`fill()` + submit against server-rendered markup would fire the browser's native GET,
attempt no auth POST, and fail indistinguishably from a wrong password. Round 28
initially preserved ten further sites on that basis before checking it. It does not
hold — `/login` and `/portal/login` render a body containing only
`<div style="display: contents">`, the toast region and comment markers, no form and
no inputs, in dev and in the built artifact alike. The markup is created by the client
runtime, which attaches handlers as it creates the nodes, so there is no window in
which a field exists un-bound. A `data-hydrated` affordance was designed and then
dropped for the same reason: it would have added app code to solve a problem the app
does not have. A comment asserting a hazard is not evidence of one; the fix was to
fetch the page and look.

## 134. A fourth teardown owner, because the third is defined by what it refuses

§127 gave `workflow_definitions` an owner that deliberately takes a NAME PREFIX rather
than a predicate, so the `is_default = false` seatbelt belongs to the helper and no
caller can write the teardown that empties a tenant. That makes it structurally unable
to reach the rows this leak is made of: `PATCH /api/workflows/{id}` auto-snapshots the
prior `steps_config` whenever the steps change, and three specs reconfigure the SEEDED
default and put it back — so the orphaned versions hang off precisely the row the
seatbelt protects. Measured: `e2e2` at 14, `e2e5` at 17, against a follow-up entry
that had recorded 9.

Widening `deleteWorkflowsWhere` would delete the property that makes it safe.
`workflows/seededWorkflowVersions.ts` is a fourth owner instead, and the two partition
the table rather than overlap: versions on SPEC-CREATED definitions were already the
third owner's job and stay there.

Its scope comes from §122's bounded-vs-unbounded rule. A blanket "delete this
definition's versions" would take rows a tenant legitimately carries (a manual save, an
earlier restore) — found, not created, and so not the spec's to delete. The mark is a
DB-clock reading taken before the spec touches anything and only later rows are
removed; the strict `>` fails in the safe direction, leaving a row rather than taking
one that predates the run.

Two things fell out of building it. The purge asserts the history is back to the size
the mark recorded, so a version written by a path the timestamp filter misses fails
loudly rather than quietly resuming the leak — a leak produces no failure at all, which
is why it ran for months. And the source guard written to stop the next spec
re-introducing it immediately found a THIRD offender the report had not named
(`admin/delete-safety.spec.ts`), which is the argument for writing the guard rather than
fixing the two files that were pointed at.

## 135. A throwaway plan row is the seeding test's to remove, and one owner removes it

`plans` / `subscriptions` live in the slot's control-plane database, which the per-test
TRUNCATE never reaches and `_rebuild_pytest_schema` clears only once per session. Six
billing files hand-rolled the same two-statement purge at SETUP — which by construction
can only reap the PREVIOUS run's rows and always leaves the last one behind — and one
wrote only the subscription half, so its plans accumulated unchecked for a whole run.

The follow-up entry's mechanism was **stale**: the leaked `meter_test_*` rows in the
shared `feohledger` database are historical, from before the harness moved to per-slot
control databases. The live half is within-session pollution, so the fix is a teardown,
not another setup purge.

**A setup purge is not a teardown, even when it looks like one.** The rows are
observable within the run (`GET /api/billing/plans` is a catalogue listing), so "cleared
at the start of the next test that happens to seed one" is not isolation.
`_reset_control_billing` runs on the fixture's teardown, and its seatbelt is derived from
`DEFAULT_PLAN_CATALOG` rather than hand-listed.

**Prefix, not predicate — §127 one table over.** `RealDB.purge_plans` owns the
plan/subscription child graph the way the vendor and workflow helpers own theirs, and
takes a prefix for the same reason: a free-form WHERE would let one caller write the
purge that empties the plan catalogue.

## 136. `tests-e2e/` is typechecked, and an API stub declares its contract

`pnpm check` extends `.svelte-kit/tsconfig.json`, whose generated `include` lists
`../src/**`, `../test/**` and `../tests/**`. `tests-e2e` is none of those, and
TypeScript does not merge includes from an extended config — so the entire Playwright
tree sat outside every type program, silently. An e2e spec could carry any type error
and CI stayed green.

That is not a hygiene problem; it is what made a class of test quietly stop testing.
Every e2e stub of an API response was a hand-maintained object literal, and the only
thing that could have compared one against the shape the app reads was a typechecker.
The dashboard stub omitted `aging_reporting.unconverted_count`; `undefined > 0` is
false; the partial-conversion disclosure that `unconverted-disclosures.spec.ts` exists
to exercise could only ever render its no-notice branch. Two files, one omission, a
green suite.

`tsconfig.e2e.json` + `pnpm check:e2e` fixes the mechanism; a `satisfies` in an
unchecked tree would have been decoration, so the two land together. A response fixture
now names the `$lib/types` shape it fakes, which makes a new non-optional field a
compile error in the fixture instead of a spec that stops exercising its own branch.

Two consequences worth not re-deriving. **`$lib` imports in `tests-e2e/` must be
`import type`** — `tsc` resolves the alias through the generated `paths`, Playwright's
esbuild transform does not read that file, and a type import is erased before the
runtime sees it. And **`tsconfig.json` gains `"types": []`**: `@types/node` is a real
devDependency now, and without the empty `types` those globals become ambient in `src/`,
where `process.env` in a component would typecheck and fail only in the browser.

Rejected: adding `tests-e2e` to the main `include`, which would make one tree's errors
block the other's check and put Node globals in browser code. Also rejected: a narrow
allow-list of "fixture files only", which would cover the two files in front of us and
nothing a future spec adds.

The check found two real drifts on its first run, which is the argument for it:
`DashboardDiscountCapture`'s money was typed `MoneyString` while
`backend/app/schemas/dashboard.py` serialises those fields as JSON numbers, and the five
`AgingBuckets` bands were typed `number` — so the dashboard was running `sum + b.value`
and `b.value / agingTotal` as raw arithmetic on currency, in a file the money-type
ratchet could not see because the type was declared inline in the route.

## 137. A supplier-portal "phase" is an id, not its label

The portal collapses the 12-value `InvoiceStatus` and the 8-value `PaymentStatus` into a
handful of vendor-facing phases, because a supplier has no use for `sending_to_erp` vs
`posted_in_erp`. The first implementation stored only the English label per status and
*derived* the chip set by grouping statuses whose label string matched.

That made the display text the identity. Translating it is then a data change, not a
presentation one, with two failure modes and no error in either:

- a phase splits — translate "Processing" and its four statuses no longer share a
  string, so one chip becomes four and a supplier filtering "Processing" sees a quarter
  of what they used to;
- two phases merge — any locale where two labels land on the same string folds their
  statuses into one chip.

Both are silent: every type still checks and the API still answers, because the chip
sends whatever status set it ended up with.

**The phase is now a stable snake_case id** with a message key beside it, and the
status→phase assignment is written out rather than inferred. A catalogue edit in any of
the six locales cannot move a status between chips.

Three consequences worth keeping:

1. **The wire did not change.** Each chip still sends the raw internal `status=` values
   behind it. What did move is the URL: `?phase=` carries the id (`?phase=rejected`)
   rather than the label (`?phase=Rejected`), which is what lets a bookmarked filter
   survive the supplier switching language — the label-keyed URL could not. Links using
   the old capitalised form no longer select a chip.
2. **These accessors return a key ALWAYS**, unlike `runStatusLabelKey()` and its
   siblings, which return `null` so the caller can render the raw wire value. Here the
   raw wire value is exactly what must not reach a supplier, so an unclassified status
   resolves to the neutral "Processing" phase. The asymmetry is deliberate: the
   tolerant-null pattern is right for an internal console and wrong for the portal.
3. **The membership is pinned by test, not by type.** Every phase id is assignable to
   every status, so nothing in the type system notices a status moving chips — which is
   a change to what the supplier's filter asks the API for. `portalStatus.test.ts`
   asserts the phase→statuses map byte-for-byte against the status vocabulary enumerated
   from the backend enums, not from the maps under test, which would agree with
   themselves.

## 138. The e-invoice message catalogue is scanned, and the guard is the deliverable

§95 deleted a hand-written code→prose map and named its replacement: a map generated
from the backend's own rule set. The generator is the easy half. The guard is the entry.

**A source scan, not a registry.** The rule ids are literal strings in the second
positional argument of `_err` / `FieldError`. Promoting them to a constant table the
validators index would be a wide refactor of code that reads well, and it would not close
the drift — an author can add a table entry and forget to use it, or use a literal and
forget the table. Scanning is derived by construction, and it is the mechanism
`test_exception_type_labels.py` already uses one domain over.

**The families are OVER-generated on purpose.** `vat_category_rule(cat, suffix)` computes
its id at runtime, so the scan expands each call site over every infix. One family is
only reached for zero-rate categories, a runtime guard a scan cannot see, so a handful of
entries are unreachable. An unreachable row costs one line and covers a future widening
for free; under-generating would leave a real refusal rendering as a bare rule id.

**The generic kinds are not mapped, and that is not an omission.** The 422 folds a code
into `msg` only when it is a rule id (`is_rule_id`, now public because three places must
agree on it), and the app's shared `formatApiDetail` keeps only `loc` + `msg`. A client
therefore cannot identify the generic kinds at all, so a key for one would be dead. They
are emitted as a separate exported list rather than dropped, so a NEW generic kind arrives
as a diff on the generated file instead of as an unmapped code nobody notices.

**Three guards, chained.** Regenerate-and-diff in CI's backend lint job catches a new code;
`satisfies Record<string, MessageKey>` on the generated file catches a key `en.ts` lacks;
the locale-parity test catches the other five locales. Each catches the step after the one
before it, and the chain was walked end to end by adding a fake rule: the check exited 1,
and after regenerating, `pnpm check` rejected the unassignable key.

## 139. The e2e vanity origin is an IP literal, and the server is pinned to match

The vanity half of `tenant/vanity-host.spec.ts` had been unit-only, and the follow-up
entry named two blockers. There were three.

**The env variable had to reach both run modes, and it must fail loud.** Locally the
suite boots `pnpm dev`; CI serves a production `vite build`, which bakes
`$env/dynamic/public` in and never reads `.env.development`. Both now take the value from
one module, so they cannot disagree. What makes this safe rather than merely tidy is that
unset FAILS: with no platform domains the legacy rule reads `127.0.0.1` as a 4-label
platform host with slug `127`, so the assertions fail rather than quietly asserting the
inverse — which was the entry's stated reason for not writing the spec at all.

**The origin is an IP literal because no hostname could work.** Every hostname the harness
can reach is `*.localhost` (Chromium maps those to loopback with no `/etc/hosts` entry),
and `localhost` is exactly what the platform-domain list declares — so no `.localhost`
name can ever classify as vanity. A `.test` name would need a Chromium host-resolver rule
*and* a Vite `allowedHosts` entry (measured: a `.test` Host is 403'd). An IP literal needs
neither.

**Which forced the third piece.** A literal connects only to the address the server BOUND,
and Vite's default binds the `localhost` NAME — which resolved to `::1` on the machine
this was built on, despite `/etc/hosts` listing 127.0.0.1 first (glibc's RFC 3484 address
selection, not file order). So the harness pins `--host 127.0.0.1`. That is *narrower*
than the default — no LAN interface — and everything using the NAME still connects,
because Chromium and Node both fall back across families.

**The `/api` proxy is an app affordance, not test scaffolding.** `docs/white-label.md` has
always stated that a vanity origin must terminate `/api` itself; the dev server did not, so
a custom domain could not be exercised locally at all and the spec would have asserted
against a 404 from the static server. It is proxied in dev and preview with
`changeOrigin: false` — rewriting `Host` to the target would defeat the very lookup the
proxy exists to make reachable.

What the spec still does not assert is that the backend then resolves a tenant from that
`Host`: that needs the origin registered in a tenant's `custom_domains`, and it is the
backend's own coverage. The half that was unreachable — what the SPA sends, and where — is
now reachable.

## 140. A proposal whose base only the server can total previews nothing

`POST /api/discounts/bulk-negotiate` sums a vendor's open invoices into one offer's
`base_amount`. The obvious kindness — showing the buyer that total before they commit to a
percentage of it — requires the client to re-derive it from a second query, and the two can
disagree: a different entity scope, a status that moved between the reads, a page boundary.
A discount is then proposed against a figure the page showed and the server did not book.

So the form previews nothing and states the rule instead, and the created offer is the first
sighting of the number. This is §34 applied to a figure that is *knowable but not yet known*:
the honest options are to show what the server computed or to show nothing, never a second
opinion dressed as the first. The rejected alternative was a server-side preview endpoint
returning the same total — correct, but it doubles the endpoint surface to remove a click.

The entry that sent us here mis-described the endpoint, which is worth recording because the
wrong reading suggested the wrong UI. "Bulk" is the **base**, not the batch: it returns one
`DiscountOfferResponse` and creates one vendor-scoped offer, so there is no per-row
skip-and-report result and the shared bulk-selection toolbar would have been the wrong shape
entirely. Three defects on the endpoint were closed on the way in: a malformed `vendor_id`
was a 500 rather than a 422; a misspelled `valid_until` was silently dropped, creating an
offer the optimizer can never rank while it stands against the vendor's whole open balance
(`extra="forbid"`); and the vendor lookup was not entity-scoped while the invoice sum beside
it was, so an offer could be stamped to one entity while pointing at another's supplier, and
an out-of-entity vendor answered 409 — confirming the id exists — where a missing one
answered 404.

## 141. A sweep-generated recurring invoice names the template's author; pre-existing templates are never backfilled

`approval_chain.violates_segregation` returns False — no breach — when `Invoice.uploaded_by_id`
is NULL, reading NULL as "no employee created this row". Four paths produced NULL and three
earn it: email intake and inbound PEPPOL are system ingestion, and the supplier portal's actor
is a tenant-scoped `VendorUser` who holds no employee JWT and can never reach an approval
endpoint. The recurring sweep was the fourth and did not belong there. Nobody *runs* the sweep,
but an employee *authored* the template, and that is the person segregation has to exclude — so
the author could approve the invoice their own standing instruction raised, exempt exactly as a
legacy pre-`uploaded_by_id` row. Reproduced before the fix: the approve call returns 200,
`status: approved`.

Migration 0096 adds `recurring_invoice_templates.created_by_user_id` (tenant-scoped, nullable,
no FK — `users` is control-plane while the table is tenant-local, the same placement as
`invoices.uploaded_by_id` and `vendor_users.provisioned_by_user_id`). `POST /api/recurring`
stamps it; `generate_one` stamps `actor_id or template.created_by_user_id` — the live actor when
there is one (generate-now), the author when there is not (the sweep). Precedence runs that way
round deliberately: falling back *past* a live actor would exempt the person who clicked.

Three calls inside it:

1. **The audit row keeps `actor_id = None` for the sweep.** The two columns answer different
   questions: *who performed this action* is nobody, *whose instruction created this payable* is
   the author. Only the second is a segregation input; copying the author into the audit trail
   would record a person as having acted when they did not.

2. **Existing rows stay NULL — no backfill.** There is no honest author to recover for a template
   written before the column, and every available proxy is a guess. Stamping the org's admin, or
   the approver of the template's first generated invoice, manufactures either a *refusal* (a real
   approver newly blocked on evidence nobody produced) or an *absolution* (a fabricated name
   standing in the trail as the responsible employee). A fraud control's inputs must be observed,
   not inferred. Those templates keep the legacy NULL reading they have always had — a shrinking
   set that ages out as templates are re-created, not a standing hole. Failing CLOSED on NULL
   instead was re-rejected for the same reason as before: it strands three legitimate ingestion
   channels.

3. **The revision id is shorter than the column it adds.** `alembic_version.version_num` is
   `VARCHAR(32)` and `0096_recurring_template_created_by` is 34 characters, which aborts
   `alembic upgrade head` before applying anything (0086 shipped exactly that). The revision is
   `0096_recurring_template_creator` (31), and the filename matches it so the two cannot drift —
   the trap 0094 set when its filename and revision id diverged.

**Residual:** the column records the author, not the last editor, so someone who PATCHes another's
template can still approve what it generates. Closing that needs segregation to key on a set of
implicated actors rather than one column.

## 142. Three doors onto one effect, and the widest is the only one that matters

`POST /api/cards/{id}/cancel` gated on `require_roles(ADMIN, AP_MANAGER, CFO)` while the two other
routes that close the same virtual card — `POST /api/payments/{id}/void` and its completion
`POST /api/payments/{id}/void/retry-card-cancel` — gate on `payment.void`. The default `ap_manager`
holds `payment.execute`, not `payment.void`: initiating money and reversing it are the two halves
of the split the granular layer exists to make splittable. So an `ap_manager` could not reverse a
card payment but could kill the card behind a live one, and the SoD split was undone by whichever
door was left widest.

§132 stated the rule for the retry route in exactly these words — "not the card router's bare
roles, which would hand an `ap_manager` in a duty-split org the half of the reversal the org
withheld" — and then left the card router's bare roles in place. A rule applied to the new path
but not the old one is not a rule; it is a preference the next reader has to re-derive.

**It breaks the standing requirement, and breaking it is the fix.** Every other route migrated
from `require_roles` to `require_permission` reproduces the prior admin/ap_manager/cfo matrix
exactly for the four system roles — that is what makes the layer additive. This one cannot,
because closing the gap and preserving the matrix are the same sentence read in opposite
directions. `ap_manager` loses the route. The matrix-preserving alternative,
`require_permission(payment.void, payment.execute)`, reproduces the old outcome perfectly and
closes nothing, since the `ap_manager` at issue holds `payment.execute`.

So the narrowing is *stated*, not shipped quietly: on the route's docstring, in `permissions.py`'s
matrix comment (where "reproduces the matrix exactly" would otherwise read as unconditional), in
`require_permission`'s own docstring, in both feature docs, and as an explicit four-role table in
`test_sod_endpoint_wiring.py::test_card_cancel_narrows_ap_manager_by_design`. A future "restore the
old matrix" edit now has to argue with a named expectation rather than silently widen a
money-adjacent control. A second test, `test_every_card_closing_route_gates_on_payment_void`, holds
all three doors to one gate — §96's "a capability with a second, divergent path is worse than one
with none" is usually read as being about *state*, and it is just as true of the gate.

Nothing in the UI regresses. §96 left this route deliberately unwired, because a standalone Cancel
is reachable on a LIVE payment where it would kill the card while that payment and its invoice
still claim money is in flight. The gate was therefore only ever reachable by a direct API call —
precisely the caller a permission layer exists to bound.

## 143. A row the user *asks for* is gated; a row the page *owns* is not

§125 settled that a KPI row renders on every state and announces a missing figure rather than
drawing one. Extending it to the twelve panel-scoped rows surfaced a case §125 did not have: three
of these rows report the result of something a person **runs**, not something the page fetches.
`/audit`'s signature sweep is triggered by a button and its read writes an `audit.viewed` row;
`ForecastVariancePanel` posts a forecast the backend deliberately never persists. Rendering those
unconditionally would put five dashes on screen for a question nobody asked — a card announcing
that a figure is on its way when nothing is coming, which is §34's failure pointed the other way.

So the rule splits on who owns the request. A row whose fetch the page fires renders
unconditionally. A row whose fetch a person fires is gated on `answer || in-flight` — absent before
the click, pending from the click, settled after. Both halves of the convention survive: the row
never collapses *while its own answer is in flight*, which is the whole of §125, and it never
claims an answer is coming when none was requested.

Rejected: gating the page-owned rows the same way, keyed on their loading flag. It reads as the
smaller change but reintroduces exactly what §125 removed — a second mechanism for the same fact,
so a reader learns the convention twice.

The other thing this round settled is that **the tint was doing load-bearing work nobody had
noticed**. Six of these rows carry an unconditional `highlight` — `/admin/health`'s Overall,
`/admin/access-review`'s Dormant, `/audit`'s Invalid, `AgentDashboard`'s Resolution rate,
`BudgetModal`'s Remaining. `KpiCard` already withholds a tint from a missing figure, so the only
thing that had ever stopped those painting a green "all clear" over an unanswered question was the
row not existing yet. Hoisting the row without §125's tint rule would have shipped a SOX access
control, an operational health check and a tamper check each asserting the reassuring answer while
still asking — the `/bank-reconciliation` case §125 calls the sharpest one, now on three controls
at once.

`/billing` needed a structural answer rather than a prop. Its usage meter was two copies of one
row, one per branch of a chain gated on the subscription response, so neither could take
`pending`: which copy owns the row is decided by `hasSubscription`, and while the response is out
there is no answer to that. The row had to move outside the question it was nested inside. A third
copy for the loading state was rejected for the obvious reason.

## 144. A redirect guard reproduces the nav's gate, and says so

`/workflows` and `/workflows/[id]` had no redirect guard, so a non-admin who typed either URL
reached the full editing surface — New Workflow, Import, per-row Delete, the bulk bar, and on the
builder an editable canvas and a Save — every control 403ing on click. The builder is the worse
dead end: a canvas edit is *lost* on the refusal rather than merely refused.

The guard added is the one four sibling routes already use, but it differs from all of them in
what it is parity WITH. Those routes' reads are role-gated, so their guard mirrors the backend.
Here every read is `get_current_user` — list, detail, templates, versions, diff, simulate,
export — and only the mutations are `require_roles(ROLE_ADMIN)`. The guard therefore mirrors
**`nav.ts`**, not the API.

That distinction is deliberately kept visible rather than smoothed over, because the open question
is whether the nav should be widened to match those role-open reads instead. Redirecting reproduces
exactly the access the nav already grants, so it settles nothing and takes nothing away; if the
call goes the other way the fix is to widen `allowed` and add the read-only mode `/organization`
carries, not to delete the guard. The e2e spec asserts the API read still returns **200** while the
mutation 403s — the one sibling spec that cannot assert 403 on both halves — precisely so a later
reader does not take a quiet 403 assertion as evidence the product call had been made.

Guarding only the list was rejected: `GET /api/workflows/{id}` is role-open, so a bookmark walks
straight past a list-only guard into the builder. The three toolbar actions are gated on the same
flag as the redirect rather than left to the redirect alone, matching `/admin/health`'s actions
snippet: unlike the table (whose fetch is gated, so it has no rows) and the builder (hidden behind
`{#if !workflow}`), they depend on no loaded data, so nothing else would stop them painting for the
frame before navigation completes.

## 145. The vendor picker is one server-searched combobox, not six `<select>`s

Six surfaces each rendered a native `<select>` over a client-side vendor list: two asked for
`page_size=100` and rendered page 1, four walked every page on mount. A `<select>` has no search,
so past the cap the remaining vendors were unreachable everywhere, with nothing on screen saying
the list was a subset. Filtering a truncated client page was rejected — it cannot reach what was
never fetched; so was raising `page_size`, which only moves the cliff (the server caps it at
`MAX_PAGE_SIZE`). `ui/VendorPicker` filters through `GET /api/vendors?search=` and states its
coverage: "showing 25 of 137", and a load failure reads as a load failure rather than an empty
tenant — `listVendors()` swallowed its errors, so an `ap_clerk`'s 403 on the
admin/ap_manager/cfo-gated list rendered as "this tenant has no vendors". A failed *next* page is a
third answer again: the page that landed stays valid and reported, and only the footer says what
could not be added. All six landed together on purpose — the entry that raised this named
"half-fixing it in one modal leaves five wrong" as the reason it had gone untaken.

## 146. The picker binds Escape with `onkeydowncapture`, and that is not stylistic

`ui/Modal` traps focus via `actions/focusTrap`, which registers a real `keydown` listener on the
dialog box, while Svelte 5 delegates a plain `onkeydown` to one listener at the app root. Real
bubbling reaches the dialog first, so a bubble-phase handler in a descendant runs only after the
trap has already seen Escape and closed the modal — `stopPropagation()` there is too late to
matter. A capture listener is attached to the element directly and fires in the target phase,
before the bubble phase reaches any ancestor. Any nested control that needs to absorb a key the
trap also handles has the same constraint; `tests-e2e/vendors/vendor-picker.spec.ts` pins the
popup-then-dialog Escape order so the binding cannot be "tidied" back.

## 147. `Catalog` carries `vendor_name`, because an id alone cannot be labelled

`Contract`, `RecurringInvoiceTemplate` and the vendor-statement responses all ship the supplier's
name beside its id; `CatalogResponse` shipped only the id. That is fine while a client holds the
whole vendor list and wrong the moment it does not — the shared picker needs a label for a vendor
that may sit on any page. Resolving it client-side was rejected twice over:
`GET /api/vendors/{id}` writes a SOX access-audit row per modal open, turning the trail into
machine noise; and paging until found is the `fetchAllPages` anti-pattern this change removed. It
is derived, not stored (no migration), and the list resolves a page in one grouped query — the
per-row form is index-only and cheap on the database, which is exactly why it never surfaces as a
slow query, since the cost is a round trip per row.

## 148. List punctuation is locale data, and only prose gets it

`', '` is an English punctuation rule wearing a string literal's clothes, and the tree had
nineteen of them. Japanese enumerates with the ideographic comma `、` and no space; German and
Spanish join the final pair with a word. A translated sentence with an ASCII-comma list spliced in
read as half-localized in three of the six shipped locales — loudest inside a screen-reader
announcement, where the separator is what produces the pause. `utils/list.ts::formatList` is the
one owner, sited beside `money.ts` and `time.ts` and reading the same active-locale holder, so the
picker moves list punctuation at the same moment it moves decimal separators.
`Intl.ListFormat`'s `conjunction` at `narrow` width is the plain shape: character-identical to the
literal in en/fr/pt-BR, ideographic in ja, and each of de/es' own final-pair word. `unit` was
**rejected** — the CLDR unit list is for quantities, and its Japanese form is space-separated with
no comma at all, the opposite of the fix. A missing `Intl.ListFormat` or a malformed locale tag
degrades to `', '` rather than throwing, memoized so a failing environment pays the throw once.

**What was NOT migrated is the load-bearing half.** The line drawn is *prose, not translation*: a
fragment a human reads as a sentence goes through the helper, whether `m()`-produced or spliced
into an `m()` sentence. A value re-split on `,` by its own input, a machine payload, and a bare
identifier cell keep the literal — swapping in a locale-dependent separator there is a correctness
bug, not a translation fix, and a routing-rule set typed in Japanese would collapse to one entry on
save. Banning the literal outright was rejected for that reason. `listJoinAudit.test.ts` records
the classification per file with its reason, so a new `.join(', ')` must be classified rather than
defaulting to English.

## 149. A screening verdict is a third vocabulary, not the vendor status

`/vendors/screening`'s history badge derived its label as `result.replace(/_/g, ' ')` under a
`text-transform: capitalize` — an English-only derivation printing `Review Required` inside an
otherwise-translated modal, sitting directly above the control that blocks a supplier's payments,
and one `capitalize` would have title-cased mid-phrase in German or French. The obvious fix —
reuse `SCREENING_STATUS_LABEL_KEYS` — does not work: `vendor_screening._STATUS_MAP` collapses
`review_required` to `review` before stamping `Vendor.screening_status`, so the vendor-level map
has no member that could label a history row, and reusing it would render a blank badge on the one
verdict a compliance reviewer most needs to read. `SANCTIONS_RESULT_LABEL_KEYS` is therefore its
own total map over `clear | review_required | match`, drift-guarded against the backend's own
literal set, with the tolerant `…LabelKey() → null → de-underscored raw` fallback the category map
established — a widened adapter degrades to raw text, never a dropped verdict.

## 150. Forbidding extras on a surface with callers is a caller audit first, and the audit is the deliverable

Round 28 put `extra="forbid"` on `BulkNegotiationRequest` and deliberately left its sibling
`DiscountOfferCreate` open: `bulk-negotiate` shipped caller-less, while `POST /api/discounts/offers`
has live callers, and tightening a schema in use is a breaking change. The honest way to make one
is to enumerate who breaks *before* deciding, not after.

The enumeration: 37 pytest call sites, 5 Playwright call sites, zero frontend client functions
(`api/discounts.ts` has no `createOffer`), zero mobile callers, and `scripts/seed_extras.py` builds
`DiscountOffer` ORM rows directly rather than going through the schema at all. Union of every key
sent: `{scope, invoice_id, vendor_id, tiers, base_amount, currency, valid_from}` — a strict subset
of the declared fields. Nobody breaks, so the guard lands.

The hazard closed is the bulk one plus a quieter second. A dropped `valid_until` leaves an offer
with no end date, hence no net due date, hence unrankable by the optimizer and standing
indefinitely. A dropped `currency` stamps the offer from the invoice or the org rather than what
the caller asserted — precisely the divergence `discount_capture` refuses to capture against — so
the savings are never realised and nothing says why.

**A grep result rots, so the audit ships as a test.**
`test_create_offer_accepts_every_field_its_live_callers_send` re-asserts that union in the suite.
Without it the audit is a claim in a commit message, and the next field rename discovers the
breakage as a 422 in an e2e spec — or in a deployed caller.

**The nested tiers stay permissive, on both schemas.** `DiscountTier` is also a *response* model:
`DiscountOfferResponse.tiers` and `.accepted_tier` are hydrated from the `discount_offers.tiers`
JSONB. Forbidding there would turn a stored row carrying an unexpected key into a 500 on read — a
worse failure than the silent drop, and on the wrong side of the boundary. `extra="forbid"` is a
rule about what a *request* may assert, not about what a table may hold.

**One thing this deliberately did not decide.** `POST /api/discounts/offers` has no frontend caller
either. Unlike `bulk-negotiate`, that may be *correct*: an early-pay offer normally arrives **from**
the supplier — the portal's own accept/decline surface, or the `financing_adapters` marketplace —
so a manual AP-side create may be a deliberate absence rather than an unwired feature. Whether AP
should raise offers by hand is a product call and stays open; the schema guard presumes neither
answer.

## 151. An abandoned hasher is a dependency you cannot patch, so we own the algorithm instead

`bcrypt` sat at `>=4.0.1,<4.1` in `backend/pyproject.toml` not because 4.0.1 was right but
because passlib 1.7.4 could not import against anything newer: it reads
`bcrypt.__about__.__version__`, deleted in 5.0, and `_finalize_backend_mixin` probes for a
historical wrap bug by hashing a >72-byte secret, which bcrypt raises `ValueError` on from 4.1.
Both fire on import, so PR #392's bump took out every module importing `app.utils.passwords` at
collection — 29 errors across all four shards. passlib's last release was 2020. The pin was
therefore permanent, and what it froze was the hashing library on the login path: no upstream
fixes, ever, for the one dependency whose failure mode is "every credential in the database".

The alternatives were worse in the same direction. Waiting for passlib has no date on it.
Vendoring passlib's `handlers/bcrypt.py` inherits the same probe and the same `__about__` read.
Re-hashing every user onto a different scheme (argon2, or raw bcrypt with our own pre-hash under a
new prefix) cannot be done without the plaintext, so it means a forced password reset for every
account — an enormous user-visible cost to dodge a dependency problem. Leaving the pin and
suppressing the Dependabot PR, which is what round 29 did, buys quiet at the price of a CVE we
would have no answer to.

So `app/utils/passwords.py` implements `bcrypt_sha256` itself. **The algorithm was derived from
the installed passlib source, not from memory or from the format string**, because it is not
`bcrypt(sha256(password))` the way its name suggests: v2 is
`bcrypt(base64(HMAC-SHA256(key=salt_text, msg=password)))` where the HMAC key is the salt's
*encoded* 22-character text rather than the bytes it decodes to, base64 rather than hex because
bcrypt does not mix entropy evenly past byte 55, and the whole thing serialised as
`$bcrypt-sha256$v=2,t=2b,r=<cost>$<salt>$<checksum>` where `r` is unpadded in the wrapper and
zero-padded in the bcrypt config underneath. Any one of those wrong and the code works perfectly
against itself while disagreeing with the column — a total, silent lockout that a round-trip test
cannot see. `tests/test_bcrypt_sha256_compat.py` is the answer: hashes **passlib itself produced**,
pasted in as literals, never regenerated, covering v2 at both costs, v1 with each ident, and the
plain `$2b$` rows this codebase wrote before c6a91396, over empty / unicode / NUL-bearing /
200-character / 72-byte-prefix-colliding secrets.

**`pwd_context` stays, as an object, deliberately.** The obvious shape for a rewrite is two module
functions, and it would have meant touching every call site, the two drift guards that monkeypatch
`pwd_context.verify`, `scripts/seed.py`, the `security-patterns.sh` rule and the root `CLAUDE.md`
invariant — a large diff whose every line is unrelated to the thing being de-risked. Keeping the
`hash` / `verify` / `identify` / `needs_update` surface made the behavioural change auditable in
one file. It also keeps the real reason the object exists: scheme *policy* — which schemes verify,
which one gets written — belongs in exactly one place, and "a second hash context is the defect"
stays literally true.

Three behaviours are decisions rather than transcription. **The legacy `$2b$` arm truncates the
secret to 72 bytes on purpose**, because that is what bcrypt 4.0 did internally and 4.1+ raises
instead; raising would lock a pre-upgrade user out of their own password, which is precisely the
migration this slice exists to avoid. **A NUL byte refuses on that arm**, as passlib refused it:
raw bcrypt reads NUL as end-of-string, so `"ab\0anything"` would otherwise authenticate against a
hash of `"ab"`. And **the legacy hash's shape is matched before `bcrypt.checkpw` sees it**, because
bcrypt's Rust extension does not reject a too-short salt — it panics, and `pyo3_runtime.PanicException`
derives from `BaseException`, so no `except ValueError` in the login handler catches it. That last
one was found by a test asserting "malformed input returns False", which is the only reason it is
not a 500 waiting in the `hashed_password` column.

**What this did not do: wire the re-hash.** The old module docstring claimed passlib's
`deprecated="auto"` policy re-hashed a legacy credential when its owner next logged in. It never
did — nothing in the codebase has ever called `needs_update` or `verify_and_update`, so the claim
was false for two years. `needs_update` is implemented and tested here so the signal is real and
correct, but consulting it means a write on the login path, on both the employee and the supplier
surface, inside a slice whose entire value is that nothing about verification changed. It is an
open follow-up with its own trigger instead.

With passlib gone, `bcrypt` is `>=5.0.0,<6`, the `.github/dependabot.yml` ignore is lifted (the
entry was explicit that the ignore and the pin move together or not at all), and the
`ignore:'crypt' is deprecated` pytest filter retires with the library that needed it.

## 152. Segregation of duties keys on a set of implicated actors, and the set is snapshotted at generation

§141 gave a recurring template its author and had `recurring_invoices.generate_one` stamp that
person onto the generated invoice's `uploaded_by_id`, so the employee whose standing instruction
raised a payable could no longer approve it. Its own closing paragraph named what was left: an
`ap_manager` who PATCHes *someone else's* template — repointing the vendor and the amount — shapes
the payable as completely as whoever wrote it, was recorded nowhere on it, and could still approve
what it generated. Reproduced before the fix: admin creates the template, ap_manager PATCHes the
amount, the sweep raises the invoice, the ap_manager's approve returns 200.

Stamping the editor into `created_by_user_id` instead was the cheap fix and is why §141 declined to
take it opportunistically: **no single column holds both people**, so writing the editor there
merely moves the exemption to the author. `approval_chain.violates_segregation` now refuses the
invoice's `uploaded_by_id` **or** anyone in `Invoice.segregation_actor_ids`, and migration 0097 adds
that column alongside `recurring_invoice_templates.material_editor_ids` — both tenant-scoped,
nullable JSONB arrays of stringified control-plane user ids, no FK, the same placement as 0095 and
0096 for the same reason (`users` is control-plane). `PATCH /api/recurring/{id}` appends the actor
to the template's set; `generate_one` stamps author ∪ editors minus whoever already landed in
`uploaded_by_id` (`implicated_actor_ids`).

**Resolving the set live at approval time, rather than snapshotting it onto the invoice, was
rejected on two counts.** `violates_segregation` is a pure synchronous predicate, shared by the
raising `check_segregation` and by the amount-floor auto-approve path in `api/workflow.py`, and
reused for expense reports, requisitions and expense pre-approvals through a `SimpleNamespace`
attribute shim; making it async and tenant-DB-reaching would put a query on the approval path of
four surfaces, three of which have no template to reach. The stronger reason is that a template is
mutable. A payable's terms are frozen when it is raised — that is why
`WorkflowInstance.steps_config_snapshot` exists — so the people those terms are attributable to must
be frozen with them. Resolving live would let an edit made *after* generation retroactively bar
someone from approving an invoice they had no hand in, and let a template's deletion silently
un-bar someone who did.

**"Material" is one frozenset, and it has a mandatory complement.**
`models/recurring_invoice.MATERIAL_EDIT_FIELDS` is the single definition — vendor, amount, currency,
the four GL-coding dimensions, PO number, payment terms, the cadence/day/start/end schedule, and
the owning entity: a term of the generated payable, meaning who is paid, how much, in what, against
which budget line or PO, on what schedule, and which entity owes it. The router asks the model
(`svc.material_edit_fields`) so the two cannot drift. An allowlist alone fails in the wrong
direction, though: a money-shaped field added later to `RecurringTemplateUpdate` would default to
"not material" and silently widen the exemption, which is precisely the omission-reads-as-oversight
failure §141 and `test_invoice_uploader_stamping` were written against. So `COSMETIC_EDIT_FIELDS`
exists as its declared complement and
`test_every_patchable_template_field_is_classified` fails until a new PATCHable field lands in
exactly one of them. `variance_tolerance_pct` is classified cosmetic deliberately — it is a
detection band for *arrived* vendor invoices that deviate from `amount` and never touches the
invoice this template generates, and an arrived invoice carries its own uploader. The pause / resume
/ end endpoints implicate nobody either: they change whether the standing instruction is live, not
what it instructs, and resuming someone else's template chooses neither its vendor nor its amount.

Four smaller calls inside it:

1. **The set is membership, appended idempotently, and reassigned rather than mutated.** A list that
   grew on every save would make the column's size a function of how often one person edited, and
   `record_material_editor` is keyed off what *actually changed*, so re-saving a form with its
   existing values implicates nobody — otherwise opening a template would cost the opener their
   ability to approve. It assigns a new sorted list because the column is plain `JSONB` with no
   `MutableList`: SQLAlchemy's dirty check compares object identity against the load-time baseline,
   so an in-place `append` would never reach the database. Same trap as the `copy.deepcopy` in
   `approval_chain.advance_approval_chain`.

2. **Empty stores as NULL, not `[]`.** `implicated_actor_ids` returns `None` when nobody is left, so
   "no additional implicated actor" has the same shape every other creation path writes and "empty
   set" never has to be told from "no set" — which keeps the fail-open NULL-uploader reading the
   three genuinely actor-less ingestion channels depend on (email intake, inbound PEPPOL,
   supplier-portal submit / PO flip) exactly as it was. Failing closed there was re-rejected for
   §141's reason: it is an outage across three channels, not a control.

3. **The three shims state the attribute instead of inheriting a default.** The predicate reads it
   with `getattr`, because a future subject that forgets must not raise `AttributeError` on the
   approval path — a 500 there removes the control rather than bypassing it. But a `getattr` default
   is how an exemption gets inherited silently, so `api/expenses.py`, `api/requisitions.py` and
   `api/expense_preapprovals.py` each pass `segregation_actor_ids=None` with the reason (none of
   those tables records who *edited* a row), and a new AST guard in `test_approval_chain.py` fails
   if a shim omits it.

4. **No backfill, with one refinement on §141.** `updated_at` records *that* a template was edited,
   never who, and an older `recurring_template.updated` audit row does not say whether the fields it
   lists were material under a classification that did not exist when it was written. The last
   updater and the org admin are the only proxies and both manufacture either a refusal or an
   absolution, so edits predating 0097 implicate nobody. The refinement: a *material editor* of a
   pre-0096 template does land in the set, because that act is observed even though the authorship
   is not — so the legacy residue is an un-named author, never an un-named editor, and it shrinks
   rather than absorbing new edits.

The inter-company mirror deliberately does **not** inherit the source invoice's set.
`services/intercompany.py` has always made the mirror's segregation subject its own creator, the
routing actor, rather than the source's; propagating the set while still not propagating
`uploaded_by_id` would block a source *editor* from approving the mirror while leaving the source
*uploader* free, which is the inconsistent half of either choice. Whether shaping a payable under
one entity should bar you from signing its mirror under another is an entity-scope question, not a
recurring-template one; the site states the decision in place and `docs/followups.md` carries it.

The revision id is `0097_segregation_actor_set`, 26 characters. `alembic_version.version_num` is
`VARCHAR(32)`, and the filename matches the id — 0086 shipped the overflow and 0094 shipped the
drift, and 0096's docstring had to cite both.

## 153. The read-only `/organization` keeps only what the API told that role, and says so where it was told nothing

`/organization` is admin-only in the nav and read-only in the page — a derived
`readOnly = userLoaded && !auth.isAdmin` wrapping every panel in one disabled `<fieldset>` — for
whoever arrives by typed URL or by a bookmark that outlived their role. §144 left the mode in place
and asked whether it should exist at all. It should: the page's reads are genuinely role-open
(`GET /api/organization`, `…/branding/custom-domains`, `…/data-residency`), so a clerk who reaches
it can legitimately read its company profile, invoice defaults, branding, vanity hostnames,
residency pin and plan. What could not stand is that half of what it showed them was not their
tenant's.

**The "widen the view" branch of the entry's own durable fix is rejected outright, and stays
rejected.** `services/org_settings_view.py::NON_ADMIN_SETTINGS` is an allow-list, and the six blocks
behind the defaulted panels are the ERP client secret and account token, the payment processor's
credential set and webhook secret, the card API key, the extraction provider key, plus `mfa` and
`fraud_rules` — which are not credentials but have no non-admin consumer. Filling those fields for a
clerk means serving them; that is the exact leak the module exists to close, and no amount of UI
tidiness buys it back. The panels are therefore made honest from the client side only: nothing about
what the backend serves changes in this decision.

Two failures, one cause — the page asked questions it already knew the answer to, and then
presented an initializer as an answer.

**The live 403.** `GET /api/organization/chat-notifications` is `require_roles(ROLE_ADMIN)`, and
`loadChat()` ran in the page's main mount effect, so a clerk's Chat Notifications panel rendered
`role="alert"` "Your role does not permit this action." — by its own comment the thing that panel's
design avoids. It moves into its own effect gated on `userLoaded && auth.isAdmin`, the shape the
Email Intake panel beside it already uses, and the panel says `org.chat.adminOnly` instead. The
audit that found it ran over *every* mount read rather than the one the entry named, and turned up a
second: `…/fraud-rules/defaults` is admin-only too. It was fired and its 403 swallowed in a `catch
{}`, so it surfaced nothing — but a request whose only possible answer is a refusal is the same
defect with the alert suppressed, and suppressing the alert is not what makes it acceptable. Gating
it meant splitting the fraud form's two halves by the gate they arrive behind: `fraudOverrides` rides
the role-open org read, `fraudDefaults` comes from the admin-only route in its own effect, and
`composeFraud()` builds (defaults ⊕ overrides) whenever the second of the two lands. A failed
defaults load still substitutes nothing — a client-side default would render switches claiming a
rule set nobody configured, beside a Reset with nothing true to revert to.

**The defaulted panels.** With those blocks absent, every field in them fell back to its component
initializer, and an initializer is a platform default wearing the tenant's clothes: AI Extraction
read "Claude Vision (Anthropic) / Platform" whatever the tenant had bought, Payments read "Mock",
Virtual Cards read disabled/US, Security read MFA-not-required, and Fraud Detection — gated on a
`fraud` that could never be composed — disappeared. **A disabled `<fieldset>` around a wrong value is
still a wrong value**; the existing read-only mode removed the broken *promise* of an editable form
and left the misinformation untouched. Each of the six now replaces its **body** with one shared
`org.readOnly.sectionAdminOnly` hint.

Hiding the six outright was the other treatment on offer and was rejected on three counts. The
heading is true — the setting exists, and for someone who cannot see it the actionable fact is who to
ask. The Getting Started strip links to `#org-payments`, so hiding that section turns a rendered
in-page link into a dead anchor. And the Email Intake panel had already made this call for the same
situation: hiding would have left the page carrying two vocabularies for one fact, a hint in one
panel and silence in five, which is §143's objection to one convention growing a second shape.

One exception inside the six is recorded in the page rather than smoothed over, because the hint is
otherwise read as a stronger claim than it is: the allow-list *does* admit `erp.integration_method`,
so the ERP panel's routing-mode select alone could honestly be shown. It is not. The panel is the
unit — the select beside it (ERP system) and every credential below it are absent — and one live
control among fourteen missing ones is a third treatment for the same fact, on a page that has just
finished getting down to one. The key's declared non-admin consumer is the workflow builder's ERP
hint, which still reads it; nothing is lost.

The criterion for which panels get the hint is worth stating because it is not "is it admin-only".
It is **does this panel state a fact about the tenant that it cannot read**. Data Sync is admin-ish
and its two actions are refused for a clerk, but it asserts nothing — it is two buttons and their
descriptions — so it keeps its body and the fieldset disables it, as before. The panels with real
projected data (Company Profile, Invoice Defaults, Branding, Custom Domains, Data Residency, Plan)
keep their fields and their values for the same reason, inside the same disabled fieldset.

The gate is the **role**, never "is this value empty". An admin's form reads its saved credentials
back into its fields because each section saves whole, so a value-keyed gate would blank a live ERP
or processor config on that admin's next save — the hazard `org_settings_view`'s own docstring
records for the admin projection.

`tests-e2e/organization/settings.spec.ts` carries both directions: a clerk meets no `role="alert"`
anywhere in the settings stack, is told which six panels are admin-only and has **no field** left
behind any of them (asserted as `select, input, textarea` → 0, because "disabled" was never the
claim), still reads her own tenant's name and currency out of the two panels that have them, and —
the negative control for the admin half — an admin sees no `-admin-only` hint at all and every one of
the six keeps its controls.

## 154. The dashboard's KPI row outranks the empty state while the answer is out

`routes/+page.svelte`'s KPI row sat inside `{:else if data}`, so the row on the app's most-visited
page was the last one still collapsing to nothing while its own response was in flight — the
behaviour §125 replaced with the `pending` treatment and §143 extended to twelve panel rows, missed
here because the round-28 note claiming the page-level rows were all done was simply wrong. It now
renders on every state with `pending={loading}` over `null` values: the row keeps its shape,
`aria-busy` plus screen-reader-only text announces the load rather than drawing it, and the
touchless-rate card withholds its green tint until there is a figure to have a verdict about.

The entry named a real collision and it is the whole of this decision. While loading, whether
`total_invoices === 0` is **unknown**, so the zero-invoice onboarding `EmptyState` and the pending row
both want the same screen. Gate the row on `total_invoices > 0` and an empty tenant is spared a
transition at the cost of every other tenant losing the row on every load — which is the collapse the
convention exists to remove, paid for by the one tenant that has nothing to show anyway. So the row
wins: `isEmptyTenant` is `!!data && data.total_invoices === 0`, keyed on the response having **landed**
rather than on the count alone, and an empty tenant sees a pending row hand over to the empty state.
One brief honest transition, in place of the thing §125 was written against.

`KpiCard` was checked rather than assumed, because §143 records the tint rule narrowly: a tint is
withheld from a *missing* figure, not from a zero one (`tinted = figure === 'value'`). That matters
here in the direction nobody would notice — `touchless_rate >= 80 ? 'green' : null` is a verdict
computed from `data`, so before the fix the row's only protection from painting a green "82%
touchless" over an unanswered question was not existing yet; with the row hoisted, the nullish value
is what keeps the card neutral while it waits.

**A card that may not exist at all stays gated on the response.** The four conditional cards —
exceptions, stale approvals, rebates earned, discounts captured — render only when there is something
to report, so the row's count is not fixed. Giving them `pending` would put a dash on screen
promising a figure for a card that then vanishes, which is §143's objection to the user-triggered
rows pointed the other way: a card announcing that an answer is coming when none is. The row's spine
is the five figures every tenant has, and those five are what render pending.

**A failed load renders the row `unavailable` — dashes, no `aria-busy`, no tint — above the error
banner rather than instead of it.** That is /cfo's placement and for /cfo's reason: the dashes are
what the page knows, and the banner beside its Retry is why. `dashboard/error-state.spec.ts` asserts
that state, and its recovery assertion moved from `.kpi` being visible to `data-kpi-state="value"` —
the row is on screen in the error state now, so mere visibility would have passed before the click
and proved nothing.

The two new cases went into `a11y/kpi-pending.spec.ts` rather than a parallel spec, as §143's did.
The hand-off case needs a response that is both **held** and **shaped** — the seeded tenant has
invoices, so the state under test is one its own data can never produce — and a second `page.route`
on one path never runs, so `holdEndpoint` took an optional body instead of pairing it with
`stubEndpoint`. The zero-invoice payload lives in `tests-e2e/dashboard/fixture.ts` as its own export,
not as a `DashboardPatch` knob: `total_invoices` is not a free field, and a payload claiming zero
invoices worth $6,000 is one the backend cannot produce — exactly the class of fixture that module
exists to stop.

## 155. A localized frame is worth shipping around prose we cannot yet localize

The `/invoices` warning icon carried a literal `` `Warnings: ${…join(', ')}` ``, and round 29 left
the join alone on purpose — §148's family 3: migrating a separator into a still-hardcoded English
sentence localizes the punctuation of English. Both halves move here, in one change, which is what
family 3 asks for. What that buys is worth stating precisely, because it is less than it looks:
`services/invoice_warnings.py` composes each finding per row from that row's own data ("PO 4412 not
found", "Round amount: 5000.00", a variance percentage), so the findings reach the browser as
server English. The aria-label is now a **localized sentence around server-English findings**, not
a translated finding — a German screen-reader user gets "Warnungen:" and then English prose, joined
with German list punctuation. That is better than today, because the separator is what produces the
pause between items in an announcement and the frame is what tells the user what the list IS; it is
not the fix. The fix is a warning-code → message-key catalogue, and the repo already has the shape
for one: `pnpm gen:einvoice-messages` generates exactly that from the backend e-invoice rule set.
`InvoiceWarning.type` is the code it would key on, but a code is not enough by itself — four
distinct `po_mismatch` messages and three `quality_hold` ones embed a PO number, an amount or a
variance, so the catalogue needs parameterized messages per code, not a label per code. Too large
to do while keying an aria-label, so it is filed rather than approximated.

`/profile` was the opposite problem: the entry named four strings and the route held eighty, on the
one page where a user changes their password and manages their second factor. Extracting it whole
surfaced the rule that keying is not editing: every migrated string kept its glyphs, straight
apostrophes included (`"Couldn't load your passkeys"`), because three e2e specs address those
sentences and because a copy change hiding inside a mechanical one is a copy change nobody reviewed.
Two literals stay English with the reason written beside them. The passkey **default name**
(`'Passkey'`) is persisted on the credential and read back by every later session, so translating it
would make one account's passkey list depend on which locale each device happened to be in when its
key was registered — that is data, not copy. The roles cell keeps its raw slugs and its literal
`', '` join: identifiers, not prose.

`AgentDashboard` held a third defect, and it is §149 again in a new place. The decision log rendered
its type cell as `exception_type.replace(/_/g, ' ')` — untranslatable by construction, and a
*different wording* from the queue one tab away, which labels the same row `PO Mismatch` from the
server's own map. So the taxonomy got a real map: `types/exception.ts::EXCEPTION_TYPE_LABEL_KEYS`
over the backend's fifteen `EXCEPTION_TYPES`, whose English values are asserted **byte-identical**
to `api/exceptions.py::EXCEPTION_TYPE_LABELS`. That equality is the load-bearing assertion, not a
tidiness one: the queue still renders the server's `type_label`, so the two surfaces would otherwise
be free to drift in English while only one of them was translated. Widening the map to cover the
queue as well was rejected for this slice — it is a different file, and the tolerant accessor means
the server label is already the fallback, so the surfaces agree in English today and the queue can
adopt the keys whenever someone owns that file.

The exception **status** deliberately did NOT get the same treatment, and the decision is the
mirror image of the type one. The queue's badge prints the raw lowercase wire value, this panel's
run dialog prints it too, and keying one without the other produces exactly what the type map was
added to remove: one status wearing two names in one page. It also has an e2e consequence that the
type map did not — three assertions match the raw lowercase `resolved` / `escalated`, and
`toContainText` is case-sensitive, so a translated badge means editing those specs in the same
change. Both halves together are a slice; half of it is a regression dressed as progress.

Two smaller calls worth recording. The three agent-action keys are shared by the decision badge,
the filter chips *and* the two count KPIs rather than minted three times — a chip that names an
action differently from the rows it filters is the drift one map removes. But the decision log got
its **own** `loadMore` key rather than borrowing `exceptions.loadMore`, even though the wording is
identical in all six locales: `pagedListFooter.test.ts` pairs `<list>.showingAll` with
`<list>.loadMore` per namespace, and an unmatched pair reads as "this footer claims to show
everything with no way to load the rest" — the precise defect that guard exists to catch. Reusing
a key there would have meant loosening the guard to accept a parent namespace, which is the wrong
trade: one duplicated string against a weakened invariant.

## 156. Bringing a surface to mobile is deciding which of its controls do NOT travel

The round-22 follow-up asked for mobile screens for `/adaptive` and inspections "if either
capability is marketed on mobile". The scope decision said build both — and the work was mostly
deciding what to leave out, because both surfaces contain controls whose correctness depends on
room to explain themselves.

**Inspections ships whole, including the write, because a receiving dock is where the work happens.**
`POST /api/inspections` needs an inspection number (suggested as `QI-<receipt>`), an outcome, and —
for a partial — an accepted quantity. All three are honestly typed on a handset by the person
holding the goods, which makes this the strongest mobile case in the backlog. The form also requires
a field the **API does not**: `gr_id`. `po_matching` reads an inspection only through the matched
receipt's `gr_id`, else a PO-level row whose `gr_id` IS NULL, so a row naming neither is invisible to
the match it was recorded for. Offering that as a form option would let someone record a failed
inspection, see it listed, and watch the invoice pay anyway — the same call `RecordInspectionModal`
made on the web. The existing unlinked rows (the QMS sync writes them when it can resolve neither
number) are rendered as **Not linked** with a sentence saying no match will read them, because the
row is real and its inertness is the fact worth stating. `POST /inspections/sync` is the one
inspection control left behind: it is an operator action against `settings.qms`, 409s without it, and
belongs beside the configuration it depends on.

**Adaptive ships read-first, and that is a finished state rather than half a feature.** The three
read models plus `POST /suggestions/{id}/dismiss` are on the phone; the two apply paths are not.
`routing-suggestion/apply` reassigns a live approval, and `threshold-recommendation/apply` raises the
org-wide `auto_approve_below` that decides which invoices skip human review entirely — a money-path
control surface. The threshold apply also carries `expected_recommended_threshold`, whose 409 is
*the guard working*: landing it correctly needs a persistent region naming both figures with the
recomputed recommendation underneath (`backend/docs/adaptive-workflows.md` § Frontend). A phone-sized
version of that control is the same control with the explanation cut off, which is worse than not
having it. The Feedback tab is absent for an unrelated reason worth recording separately:
`GET /feedback` writes an `adaptive_feedback.viewed` access-audit row, so a surface that can reach it
needs its own deliberate "only when asked" treatment and must not ride along with two tabs that load
eagerly.

**The outcome filter went to the server, which made this a backend change too.** The inspection list
is paginated, so filtering the loaded page would hide every matching row past the page boundary and
leave `total` describing a set other than the rows under it — the defect family §79/§82 keeps
finding, and the one the approvals tab already shipped once. `GET /api/inspections` gained
`?result=pass|fail|partial` through `_inspection_list_filters`, the one builder the rows and the
count share. It is typed as a `Literal`, so a misspelled value is a 422 rather than an empty page
that reads as "no failures"; the column itself stays free-form, because what constrains a synced row
is `qms_sync.normalize_disposition`, not the DB.

**The Settings hub is gated per entry, not per section.** Round 28 made exactly this correction to
the web nav, and the mobile hub had the same shape: one `isOrgAdmin` wrapper around the whole
Administration block. Adaptive reads are admin/ap_manager/cfo, so that wrapper would have hidden the
surface from two of the three roles the API admits, and the inspection list is `get_current_user`, so
gating it at all would have been a dead end for the clerk working the quality hold a `fail` raised.
Procurement → Quality Inspections is therefore open to everyone with the record affordance gated
inside the screen, and Administration renders whenever any child does with each row carrying its own
check. The three admin rows were localized on the way past: they were still English literals, and
extending a run of hardcoded strings with more hardcoded strings is how that hole stays open.

**Per-vendor amounts render with no currency symbol.** They are denominated in the org's reporting
currency and the `approval-patterns` payload does not name it. A
`NumberFormat.currency(symbol: '$')` — what every other mobile money figure uses — would stamp
dollars on a ZAR org's averages, which is a wrong number rather than a missing one. So the section
says it once, the figures render as the exact strings the backend sent, and the anomaly rows (which
*do* carry `amount_currency`, because `detect_invoice_anomaly` falls back to the billed figure) are
labelled individually. The code **is** reachable — `org_settings_view.NON_ADMIN_SETTINGS` projects
the top-level `reporting_currency` to every role precisely so the web `orgCurrency` store can format
aggregates — but mobile has no equivalent store, and four mobile screens currently assert `$` from a
module-level formatter. Building that store is the fix for all five surfaces at once and is tracked
as a follow-up; quietly adding a fifth screen's worth of guesswork is not.

**The suggestion `title` / `rationale` stay English even though the screen's chrome is localized in
all six locales.** They are sentences `derive_suggestions` composes with the numbers *and the
currency code* inside them, and the web page renders them verbatim for the same reason. Localizing
them is not an ARB entry — it is moving the composition server-side behind a message key, which is
its own change.

**Neither store is offline-cached, and the inspections one is the interesting case.** Adaptive is the
easy call — a privileged analytics read recomputed on every call, like `CashFlowStore`. Inspections
looks like a list a warehouse on bad signal would want, and it is the one list where a stale copy is
a wrong answer about *money*: a `fail` is what puts a quality hold on a payable invoice, so a cached
row that predates a re-inspection tells an approver the opposite of the truth about whether that
invoice can be paid. An honest error beats that, and the write needs the network regardless.

## 157. A warning code identifies a sentence, not a category — and its numbers are not strings

`decisions.md` §155 keyed the `/invoices` warning frame and said plainly what it bought: a localized
sentence around server-English findings, because `services/invoice_warnings.py` composed each
finding per row from that row's own data. Closing it looked like adding a label map to
`InvoiceWarning.type`. It is not, and the reason is the whole design: `po_mismatch` is **five**
sentences (no PO, an amount variance, a partial receipt, an over-receipt, an unquantified
over-receipt) and `quality_hold` is five more. A label keyed on `type` could name exactly one of
them and would silently mislabel the other four. So the code identifies the SENTENCE — 48 of them
across six producing modules — and `type` stays the category that code paths already branch on
(`UPSTREAM_WARNING_TYPES`, the modal's filter). A new code therefore never needs a new type, which
is what keeps the vocabulary cheap to extend.

**The catalogue renders the English too, and that is what makes it honest.** `warning(code,
severity, **params)` fills the declared template to produce `message`, so the fallback a client
renders and the parameters it would have interpolated come from one string. The alternative — a
template on one side and an f-string on the other — is the drift §95 deleted a hand-written map to
escape. `message` stays on the payload permanently rather than being retired once every code is
covered: a warning persisted before this change carries no `code` at all, nothing backfills it, and
`refresh_warnings` only re-derives on the invoice's next write, so the fallback is the normal state
of an untouched historical row.

**A word in the sentence selects the code; it never rides as a parameter.** "over" / "under",
"terminated" / "cancelled", "(not-to-exceed)" — spliced into a German sentence, an English word is
the same defect as an English sentence, just smaller. Hence `price_variance_over` /
`price_variance_under` and `contract_spend_limit_exceeded{,_not_to_exceed}` rather than a
`{direction}` param. The mirror-image call is the **cross-entity tail** on `duplicate_similar`:
that one IS expressible as a parameter, because `{crossEntityCount, plural, =0 {} one {…} other
{…}}` collapses "no siblings" into the same template — so it costs one param instead of doubling
four codes to eight. The test is whether the variation is grammatical or numeric, not whether it is
optional.

**A parameter carries a typed value, not a formatted one.** This is the half a "backend emits
`{code, params}`" instruction does not imply and the half that decides whether the result is worth
shipping. `5000.00` interpolated into a German sentence is still en-US, and `$5000.00` on a ZAR
invoice — which is what several of these sentences actually said — is a *wrong figure*, the same
call §156 made against a `NumberFormat.currency(symbol: '$')` on mobile. So money travels as the
`Decimal`'s own digits beside an ISO code and the client formats it (`5.000,00 €`), and percentages,
counts and dates travel raw for their own formatters. The consequence worth writing down is that the
English fallback and the English message key are **deliberately different strings**: the fallback
spells the currency code and the `%` out because nothing downstream will format them, while the
key's value leaves both to `formatMoney` and `Intl.NumberFormat`. A reader of `en.ts` who does not
know that will "fix" it.

Precision and sign are read off the backend's own digits rather than decided by the client: a
duplicate similarity is `98` (whole percent, unsigned) and a PO variance is `+20.0` (one decimal,
signed), and only the side that measured it knows which. The client counts the decimals in the
string it was handed.

**The resolver fails closed in two directions, not one.** An unknown code falling back to `message`
is obvious. The second is a KNOWN code arriving with an incomplete param set — a client/backend
skew — and it matters because `interpolate.ts` deliberately leaves an unfilled `{placeholder}`
intact rather than blanking it. Rendering that would print braces at a reviewer, so an incomplete
param set degrades to the server's complete sentence too.

**Composition through the catalogue is enforced by a scan, because a registry nobody uses is worse
than no registry.** `e_invoice/rule_catalog.py` rejected a constant table for exactly this reason —
"an author can add a table entry and forget to use it" — and chose an AST scan of the validators
instead. Here the composition sites had to change anyway (they must emit params), so the table
*became* the source and two scans close the loop in both directions: one fails on a dict literal
carrying `type` and `message` in any producing module, the other on a declared code with no call
site. That is also why two sentences stopped being built by cutting substrings out of the matcher's
English `issues` list (`next(i for i in match.issues if i.startswith("Over-receipt"))`) —
`MatchResult` now carries the quantities and the deviation notes as data, and the PO reference
survives the with-notes quality branch, which the substring version silently dropped.

One defect the change created and then fixed at the root is worth recording, because the shape
recurs: a self-correction violation is not only a warning source — `run_extraction` persists the
whole `violations` list into the `priors_metadata` JSONB, which nothing in the old shape made
visible because every value was a finished English string. Handing those checks a `params` dict of
raw `Decimal`s broke the extraction save outright (`Object of type Decimal is not JSON
serializable`, raised from an autoflush and surfacing as an invoice stuck at `new` with a 409 on the
`failed` transition). The fix is not a serializer at the write: each check builds its violation
*through* `warning()`, so JSON-safety is a property of construction rather than of remembering.

## 158. A vocabulary shared by two surfaces gets one key set, and every surface that renders it moves in one change

Round 30 keyed everything around the exception lifecycle `status` and deliberately left the status
itself raw (§155), on the grounds that keying one surface without the other produces one status
wearing two names in one page. That was the right call and it is also why this slice is shaped the
way it is: the unit of work is the vocabulary, not the file.

The unit turned out to be bigger than the deferral recorded. The follow-up named two call sites —
the queue badge and the AI-agents run dialog — and there were three: `AgentDashboard`'s
runnable-queue table prints the same column under a header that round 30 had already keyed, two
tables above the dialog it *did* name. Fixing the two would have reproduced the exact defect inside
the panel being fixed. A deferral that enumerates call sites is a snapshot of a grep, and the grep
was one `<td>` short; the durable habit is to re-derive the site list from the column rather than
from the note.

`EXCEPTION_STATUS_LABEL_KEYS` points at the four `exceptions.filter.*` keys the queue's own status
chips already read, rather than minting an `exceptions.status.*` set. This is the same call as the
three agent-action keys shared by badge, chips and KPIs (§155), and it is worth stating as a rule
because the alternative is seductive: a per-purpose namespace reads tidier, and every one of the
six catalogues would then hold two strings that must stay equal forever with nothing checking that
they do. The first translator to revise one of them produces a chip reading `Gelöst` above rows
badged `resolved` — which is the shipped defect this closed, in a different costume. The test
asserts the key *is* `exceptions.filter.<status>` rather than merely that it resolves, so a future
slice cannot quietly introduce the parallel set.

Two smaller calls. The route's local `STATUS_TONES` moved into the types module beside the label
map, total over the same union, so a status that gains a colour without a label is a compile error
— the `RUN_STATUS_TONES` pairing, and the reason the union is load-bearing rather than decorative.
And the roster is derived from the backend as `ACTIONABLE_STATUSES` ∪ the image of
`RESOLUTION_STATUSES`, with the ORDER pinned against the four `by_status` rows of
`GET /api/exceptions/summary`: those four are literally what the chips count, so pinning against
that block rather than against a sorted set means the guard fails if the chips and the labels ever
stop describing the same four things.

The e2e half of the deferral was also mis-stated — two assertions, not three — and the arithmetic
matters more than the number. Two of the tree's status assertions were case-*insensitive* regexes
(`/open/i`, `/resolved/i`) that pass against both the label and the raw value, which is to say they
were not assertions about this at all. They were tightened to the exact labels in the same change.
A guard that cannot fail is worse than a missing one, because it reads as coverage.

Finally, the same file held a live instance of the defect §155 names. The type-filter chips derived
their text as `exception_type.replace(/_/g, ' ')` and printed `po mismatch` while the rows they
filter carried the server's `PO Mismatch` — §149's defect, in English, on one page, a year after
`EXCEPTION_TYPE_LABEL_KEYS` was created to remove it. §155 had deferred widening that map to this
file "whenever someone owns that file", and that condition was recorded only in this append-only
history and never filed as work. Owning the file is what closed it. The lesson is narrow and
practical: a deferral conditioned on someone touching a file belongs in `docs/followups.md`, where
the person who touches it will look, not here.

## 159. A label map's absence is what made the raw identifier defensible, so the exemption expires with the map

`listJoinAudit.test.ts` exempts a literal `', '` join when the joined text is a bare list of machine
identifiers, and `/profile`'s roles cell held that exemption on the stated grounds that
`admin, ap_manager` are slugs, not prose. The classification was correct. It was also circular: the
roles were identifiers *because nothing had given the four built-in roles a translated name*, and
the same page's neighbour, `/admin`, had been rendering them as `Admin, AP Manager` out of a
hardcoded English `ROLE_LABELS` the whole time. So the two findings — an untranslated label map and
a correctly-exempt join — were one finding seen from two sides, and neither could be fixed alone:
labelling the roles without the separator localizes half a sentence, and localizing the separator
without labelling the roles localizes the punctuation of a list of slugs.

That generalizes into a rule about the audit table itself. An entry whose reason is "these are
identifiers" is only as durable as the claim that the thing has no name, and the table now says so:
role slugs are no longer a family-2 exemplar, and the one entry still citing them —
`/admin/access-review` — was reclassified family 3, because what actually keeps its roles raw is
that the entire route is hardcoded English down to its column headers. Family 2 and family 3 look
identical from the diff and are opposite in lifetime: family 2 is permanent, family 3 is a queue.
Filing something under the permanent one because the code looks the same is how a queue stops being
worked.

The second call is where the tolerance lives. `roleLabelKey()` returns `null` for a custom role and
the caller prints the stored name verbatim, because a custom role's name is text an admin typed
into their own tenant: translating it would rename one org's `Approver` role according to whichever
locale each viewer happens to be in. Every surface therefore resolves through one function — the
system-roles table included, where the map can never miss — so the rule is stated at the call site
rather than encoded in which table happens to call which.

What makes that fallback *sound*, though, is not a convention in the frontend at all. It is
`api/admin.py` refusing a custom role named after a built-in (`400 "reserved system role name"`),
which is the only reason no tenant can own a role this map speaks for. That is a cross-repo
invariant holding up a frontend design decision, and it would break silently: delete the check and
a tenant's `cfo` role starts rendering through `admin.roles.name.cfo`, with nothing failing and
nothing looking wrong. So `types/admin.test.ts` pins the Python check alongside the roster. The
general form is worth keeping: when a tolerant accessor is safe *because* of a guarantee somewhere
else, the guard belongs on the guarantee, not on the accessor — the accessor's own behaviour is the
part you can already see.

## 160. A money figure is labelled by the currency its own payload names, and wears no symbol when none can be proven

Nine mobile screens and widgets each declared `NumberFormat.currency(symbol: '$')` at module
level, so every figure on them read as dollars regardless of what the org — or the row — was
denominated in. The invoice and contract detail screens rendered the contradiction themselves: an
`Amount` line wearing a `$` directly above a `Currency` line reading `EUR`, both off the same
object. §156 recorded the adaptive patterns tab declining to join that and rendering raw server
strings under a section note instead, and filed the fix as one store plus one formatter.

**One store would have been the wrong fix for most of these surfaces.** The follow-up's premise was
that the figures are all in the org's reporting currency and mobile just had no way to learn it.
Four of them are not. An invoice, a contract, a payment and a payment-queue row are each denominated
in that **row's** currency, and a GBP-reporting tenant holds USD invoices — so stamping the org code
there relocates the defect rather than removing it, and does so invisibly, because the two codes
agree for every single-currency tenant. So `utils/money.dart` takes the currency as a **parameter**
and each call site passes the most specific answer it has: the row's own; else the one its payload
names, which for three endpoints already exists (`/payments/summary`'s `currency`,
`cash_position`'s `opening_balance_currency`, the dashboard's `reporting.reporting_currency` —
resolved server-side by the very function that denominated the figures, so there is nothing to
disagree with); else `OrgCurrencyStore`. Sourcing it that way leaves the store with exactly two
consumers — the adaptive per-vendor averages and the cash-flow forecast leg — which is the honest
size of the gap it exists to close, not the five screens the entry assigned it.

**Where nothing can be proven, the figure renders bare — grouped digits, no symbol.** That is the
answer, not a degraded mode, and `PaymentResponse.currency` had already argued it for the payment
case: a `None` code "is NOT a licence to substitute a default … Render the bare figure rather than a
code that cannot be proven" (§79/§82). It generalises cleanly. A missing symbol is a visible gap a
reader can ask about; a wrong one is a wrong number that looks right. It is deliberately **not** the
`—` of §125/§143 either: an em dash means *no figure*, and here there is a figure whose label is
merely unknown. Withhold the verdict, keep the shape. The consequence a reader will notice first is
that **a payment run's total now wears no symbol at all**, including on the dialog that authorizes
its execution. `payment_runs.total_amount` is a plain `SUM(Payment.amount)` over payments
denominated in their own invoices' currencies, so a run spanning a USD and a EUR invoice holds a
quantity in neither; the `$` it wore was the only thing making that look answerable. Rolling that
sum up the way `/payments/queue` already does is filed as a follow-up.

**The rung chain had to be copied, not paraphrased.** The follow-up described the resolution order
as "`reporting_currency` → `invoice_defaults.currency`, the first two rungs". `invoice_defaults.
currency` is the **third**; rung 2 is `payments.home_currency`, which `org_settings_view.
NON_ADMIN_SETTINGS` admits by name — the one key let out of an otherwise all-credentials block —
and which the web `utils/reportingCurrency.ts` reads, its docstring calling `invoice_defaults` "the
LAST candidate". Implementing the entry as written would resolve nothing for an org whose only
signal is the home currency, while every figure the server sent that org was denominated in it: a
silent mislabel produced by a client claiming to mirror a server function it had skipped a rung of.
The store returns `null` when all three miss, and that is §119 exactly — the fourth backend rung is
`settings.reporting_currency_default`, which no client can read, so a client substituting `USD`
there is guessing at an operator-settable value and its guess is indistinguishable from a configured
answer. For the same reason `Invoice`, `Contract` and `PaymentQueueItem` stopped defaulting
`fromJson`'s currency to `'USD'`. `OrgSettings` keeps one defaulted accessor, `defaultCurrency`,
because the org-settings **form field** needs a value — and `resolvedReportingCurrency` sits beside
it as the abstaining one, which is the shape §119 prescribes rather than a duplication.

**Two payloads could not be fixed on the client, and they are different problems.**
`/api/exceptions` served the related invoice's `amount` with no currency at all, so every client had
to invent one; `_exception_dict` now joins `inv.currency` through off the invoice row it already
loads, which is a one-line change in the one serializer the list, detail, assign and bulk handlers
share. The dashboard was worse than unlabelled: `DashboardData` read `total_amount`, `aging` and
`upcoming_total_amount`, which are naive `SUM`s across currencies. No code honestly describes a
mixed-currency sum, so labelling the naive figure — with a `$` or with a correctly-resolved code —
would have been a wrong number wearing a right-looking symbol. It now reads the
reporting-currency counterparts the endpoint already serves beside each of them, which is what makes
`reporting_currency` sayable there at all. Disclosing those payloads' `unconverted_count` on the
mobile surface is a further increment and is filed, not assumed: the figures are correct and
correctly named without it, which they were not before.

**The formatter resolves the minor-unit count, not just the symbol.** `NumberFormat.simpleCurrency`
is used over `.currency` so JPY renders `¥1,235` — a hardcoded two-decimal formatter prints
`¥1,234.50`, a figure that cannot exist, and every one of the nine replaced declarations did. The
exact-decimal entry point is separate from the numeric one on purpose: the payment-queue, cash-flow
and adaptive payloads send money as strings precisely so no float touches it, so
`formatMoneyString` returns the string **verbatim** rather than rounding whenever formatting would
risk changing a digit — no provable currency, not a number, or more than 15 significant digits,
which cannot round-trip through the `double` `NumberFormat` takes. A silently rounded cent is worse
than an unformatted figure.

## 161. A nav row is gated on the read; a control is gated on the write. A duplicate control elsewhere is not a substitute for the page that owns the action

`POST /api/gl-accounts/sync-erp` is `require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)` and its only
caller was the Data Sync panel on `/organization` — a route the nav shows to `admin` alone, inside
the blanket read-only `<fieldset>` §153 left in place. So the one role the backend deliberately
widened that write to was the one role with no way to perform it. `POST /api/gl-accounts` (create)
was worse and went unnoticed: no caller anywhere. Both siblings behind 3-way matching already had
a page with their own Sync-from-ERP button gated on `auth.isManager`; the chart of accounts had no
page at all.

**The row follows the read.** `GET /api/gl-accounts` is `get_current_user` — auth-gated, role-open —
for the same reason `/purchase-orders` and `/goods-receipts` are: a clerk coding an invoice has to
be able to look a GL code up, and the invoice / expense / requisition / catalog pickers already
serve them that identical list. The followup's own durable fix said to gate the nav row on "the same
role gate the action uses", and that is the one instruction in it that had to be refused: gating the
row on `admin | ap_manager` hides a page whose every read succeeds — the dead end `nav.ts` has now
fixed five separate times, each with its own comment — *and* leaves `sync-erp` no more reachable
than before, since the role it was widened to still could not navigate anywhere that calls it. The
two gates answer different questions. Which is also why the write controls are not merely
`disabled` for a clerk: the page renders read-only, the same shape `/purchase-orders`,
`/credit-memos` and `/vendors/screening` already use.

**Procurement, not Settings.** The chart is the coding target of every document in that group
(requisition → PO → invoice line) and of `/budgets` below it, whose `gl_account` dimension keys on
it. Settings was the tempting home — the chart is configuration, synced from an ERP, created by an
admin — and it fails on one concrete consequence: every Settings child today is admin- or CFO-gated,
so a clerk sees no Settings row at all. A clerk-visible child there makes the group appear for them
containing exactly one reference table they cannot change, which reads as a promise the group does
not keep. A row's home is decided by who can see the group it lands in, not only by what the row is.

**Data Sync keeps its place and loses its buttons.** With all three sets reachable, its two buttons
were not merely duplicates but the worse copy of each: admin-only purely by virtue of this route's
nav gate, saying nothing about which entity's chart or PO set they would write into (`sync-erp`
writes into the selected entity's chart), and reporting into a bare `<span>` with no list to
refresh. Deleting the section was the other option and was rejected on three counts: this is where
the ERP connection is configured, so "now where do I pull it?" is asked here; the Getting Started
strip establishes that in-page wayfinding on this route is load-bearing rather than decoration; and
the vendors row had *already* been a link, so the panel was carrying a link and two buttons for one
fact — §143's objection to one convention growing a second shape, which §153 had just finished
removing from the rest of the page. Three links is one vocabulary. §153's criterion is unchanged by
this: the panel still asserts no fact about the tenant, so it still needs no admin-only hint, and
three anchors are honest inside a disabled fieldset in a way two refused buttons were not.

**A page built on an unpaginated read states the count it holds.** `GET /api/gl-accounts` is the
documented exception to the pagination envelope and had to stay one: the pickers need the whole
chart, so a `{items, total, page, page_size}` migration would make a code living past page 1
unselectable in four places at once — a coding defect wearing a paging fix. Building the page on
that read has one visible consequence, and it is a claim about honesty rather than a layout
preference: the footer is a plain count, never "Showing all N" with a Load-more, because the rows on
screen *are* every row matching the active filters. The corollary is a message-key rule, since
`pagedListFooter.test.ts` pairs `<list>.showingAll` with `<list>.loadMore`: such a page must not
mint a `showingAll` key at all, rather than mint one and weaken the guard to excuse it.

## 162. When NULL is a value, the row has to carry it — and the form has to say which value it is about to write

`GLAccount.entity_id` is the one place in this schema where a NULL means something: the account is
**shared across every entity**, not an unstamped legacy row. Every rule built on that is enforced —
two partial unique indexes, an effective-chart 409, a sync that resolves into the chart it was
invoked for — and none of it was *visible*, because the list read did not serialize the column. That
produced two pairs of rows a reader cannot tell apart: in the consolidated view the response is
every subsidiary's chart at once, where two of them legitimately hold their own `6000`; with an
entity selected it is `shared ∪ its own`, where an entity row sharing a shared row's code is an
override of it. A page rendering either pair shows duplicates and can say nothing about them.

So `entity_id` is in the payload. Additive by construction — the picker call sites read
`id`/`code`/`name` — which is what let `GlAccountOption` become a `Pick` of the full row instead of
the fifth hand-written guess at one endpoint's shape.

Two calls inside that worth recording, because each had a plausible alternative.

**The name is resolved client-side, not joined on the backend.** Returning `entity_name` beside the
id meant a join on every list read, for a label the browser already holds: `entityStore` loads the
tenant's entities for the switcher, *including deactivated ones*, precisely so it can name a
selection that has since been retired. That store is therefore the better resolver, not merely the
cheaper one — a backend join would have to reproduce the archived-entity case it already handles,
and an unresolvable id falls back to "Another entity" rather than an empty cell.

**The Scope column renders only on a multi-entity tenant** — `entityStore.multiEntity`, the gate the
switcher itself uses. Always-on was the tidier rule and is the wrong one: with one entity every row
is either shared or the default entity's, the distinction has no consequence anyone can act on, and
a column of one repeated value is noise that makes the table harder to scan. Same reasoning retires
**Status** to the *Include inactive* toggle: while inactive rows are filtered out, a Status column is
a column of "Active".

**The create form states which chart it is about to write to, before it writes.** The backend takes
that from `X-Entity-ID`, not from the body (`get_entity_id`, deliberately not
`get_write_entity_id` — consolidated ⇒ shared), so the sidebar switcher silently decides whether the
new account reaches one subsidiary or all of them. The difference is invisible in the form and
unfixable afterwards: that router has no PATCH. A hint naming the destination is the cheapest
honest answer; the alternatives were an entity picker in the dialog (a second, competing source of
truth for a scope the whole app already scopes by) or saying nothing (the status quo, which is a trap
with no exit).

One thing deliberately *not* asked. The onboarding copy for an empty chart wanted to know whether an
ERP is configured, to choose between "sync it from your ERP" and "add them by hand" — and
`GET /api/organization` projects `settings` **by role** (`org_settings_view.NON_ADMIN_SETTINGS`), so
the answer a non-admin gets for that key is a partial projection rather than the tenant's
configuration. A page that asks a question whose answer depends on who is reading, in order to pick a
sentence, is exactly how `/organization` came to present platform defaults as tenant settings
(§153). One description covers both routes truthfully, and no request is made.

## 163. A legacy password hash is upgraded on the one request that can do it, and the second bcrypt is paid openly

§151 implemented `needs_update` and deliberately left it unwired, and its closing paragraph named
why that was a stopping point rather than a finish: the docstring it replaced had claimed passlib's
`deprecated="auto"` policy re-hashed a legacy credential on login, nothing had ever called
`needs_update` or `verify_and_update`, and so a row written before c6a91396 was a raw `$2b$` bcrypt
hash — 72-byte truncation and all — for as long as its owner never reset. A correct signal nobody
consults is the same outcome as no signal.

**A login is the only request that can fix it**, because it is the only moment the plaintext exists.
A password column cannot be migrated offline; the only alternative is a forced reset for every
account, which is precisely the user-visible cost §151 was written to avoid. So
`services/credential_upgrade.upgrade_password_hash` is called one line after `verify_password` on
both surfaces — `api/auth.py::login` and `api/portal_auth.py::portal_login` — and nowhere else. The
other three consumers of a stored hash need no wiring: `/auth/change-password`, `PATCH /auth/me` and
the admin reset all write a fresh hash by construction.

**Placement is two decisions, not one.** It goes *before* the MFA branch, which returns a challenge
token rather than an access token: the password has already verified at that point, and gating the
upgrade on a completed second factor would skip it for exactly the accounts most worth protecting.
It goes *after* the SSO-only refusal, because a tenant that has closed password login has made that
hash unreachable for signing in and gains nothing from re-encoding it. The residue is recorded
rather than papered over: an SSO-only tenant's legacy hash never upgrades, while remaining a valid
MFA step-up proof, and `docs/followups.md` carries it.

**Acting on `needs_update` alone would have been a defect.** `needs_update` is `identify(h) !=
scheme`, so it is also true for a string `identify` cannot name — correctly, since "replace it" is
the right answer for corruption. But this is the only place in the codebase that *acts* on the
answer, and the action would be to write a **working** credential over a row that held unusable
bytes. In production the case is unreachable: a string `verify` cannot match never gets past the
401. That is exactly why the guard costs nothing and why omitting it would have been a hazard
waiting for the first person to stub a verify. The trigger is both conditions, stated.

**The extra bcrypt is accepted, and the timing signal it creates is not hidden.** An upgraded login
pays two hashes where a current-scheme login pays one. It happens at most once per account ever —
after the upgrade `needs_update` is permanently False — and it runs off the event loop through
`hash_password`, so it costs a thread and CPU rather than the worker's concurrency. Lowering the
cost for the upgrade hash was rejected (it permanently weakens every credential it touches);
deferring the re-hash to a background job was rejected outright, because it requires the plaintext
to outlive the handler, which is a far worse exposure than the hash it would fix. The observable
consequence is that a legacy account's *successful* login is measurably slower, once. The only
observer is someone who has already authenticated as that account and holds a token or a challenge
— somebody who does not need a timing oracle — and the parity the codebase actually defends is
untouched: unknown address, no password hash, deactivated and wrong password all still cost exactly
one bcrypt through the shared `_reject_login` tail, and no rejection path reaches the upgrade.
Equalising would mean paying a second hash on every login forever to conceal a fact the account
owner alone can see, one time.

**No audit row.** A status change writes one; this is not a status change. The credential is
unchanged — same secret, same owner, same validity — only its storage encoding moved, and the
sign-in that triggered it is already on the immutable trail as `auth.login.success` /
`portal.login.success` in the same request. A second row would additionally name, inside a WORM
trail that ships to third parties, which accounts had been on a legacy scheme, for an event with no
actor and no decision in it. An INFO log line carrying the account id and the two scheme names
records the transition instead — which is what answers the operator question the follow-up's own
trigger posed, "do pre-c6a91396 hashes actually exist in a deployed database?"

## 164. A credential-maintenance write is a compare-and-swap, and it can never be the thing that fails the login

The follow-up's durable fix said "re-hash the just-verified plaintext and assign it." Both halves of
that are wrong in ways that only show up under concurrency and under failure, and both were found by
writing the test before trusting the prescription.

**Assignment is a lost update.** The login handler reads the hash, spends ~200 ms verifying and
another ~200 ms re-hashing, and holds no row lock over any of it. A password change committed inside
that window would be overwritten by a re-hash of the *old* plaintext — reviving the credential its
owner had just retired, which is the exact opposite of what somebody resetting a leaked password
asked for. The write is therefore `UPDATE … SET hashed_password = :fresh WHERE id = :id AND
hashed_password = :the_hash_we_verified`, with `synchronize_session=False` and an explicit
`set_committed_value` afterwards (a plain attribute assignment would mark the column dirty and let
the next flush re-emit the write *without* the guard). A zero-row result is a normal outcome, not an
error: two concurrent logins upgrading the same row land there too, and the second correctly does
nothing. Rejected alternatives: `SELECT … FOR UPDATE` before the verify, which holds a row lock
across 400 ms of bcrypt on the most concurrent endpoint in the app, and on every login rather than
the handful that need it; re-reading the row after hashing and comparing in Python, which is the same
race one statement later; a version column, which is a migration for a problem one `WHERE` clause
solves.

**`commit_before_response` is true, and not sufficient.** The entry's reasoning was that both
handlers already commit before the response, so no plumbing was needed. They do — and the first
draft still shipped a 500 on a correct password. Catching a server-rejected UPDATE and calling
`db.rollback()` **expires the loaded ORM instance**; the handler's next attribute read
(`user.mfa_enabled`, or the log line's own `account.id`) is then a *synchronous* lazy SELECT, which
from a coroutine is `MissingGreenlet`. The error handling would have been the outage. Two things fix
it structurally: the write runs inside `db.begin_nested()`, so a rejected statement unwinds a
SAVEPOINT rather than the caller's transaction — nothing is flushed inside it, so no object is
expired and the outer transaction stays usable — and the identity used in every log line is read
into locals **before** any write is attempted. The helper also commits immediately rather than
riding the handler's commit, so the row lock it takes is not held across the audit dispatch that
follows (the rule `services/post_commit` enforces elsewhere) and a later Redis blip in
`register_session` cannot undo a completed upgrade. The cost of that choice is a precondition —
call it before the handler stages any other write — which is stated in the docstring and is why the
portal call sits above its `last_login_at` stamp.

The generalisation worth keeping: **a maintenance write that rides a user-facing request must be
unable to change that request's outcome.** It may fail, and it must then be silent, contained, and
honest in a log line — never a 401, never a 500, and never a rollback that reaches further than the
statement it was cleaning up after. `tests/test_password_hash_upgrade.py` provokes both failures for
real (an over-long digest hitting `hashed_password`'s own `VARCHAR(255)`; a second session changing
the row mid-hash) rather than mocking them, which is the only reason the expired-instance trap was
found before a deployment rather than after one.

## 165. The in-repo sops scaffold is deleted, not left dormant behind a warning

**Decided:** 2026-09-14 · `.sops.yaml` + `bin/sops-init.sh` (removed), `.gitignore`, `.github/workflows/env-isolation.yml`, `backend/CLAUDE.md`, `infra/README.md`

§12 moved production secrets to the private `infra-secrets` repo but left the older template
generation's scaffold standing: a root `.sops.yaml` with placeholder ARNs, `bin/sops-init.sh`,
`.gitignore` negations that re-included `.env.sops` and `*.tfvars.sops`, and a dozen docs telling an
operator to run the script and commit what it produced. The only thing between that scaffold and
ciphertext in public history was one sentence in the root `CLAUDE.md`.

That sentence stopped being enough the day the FeohLedger AWS account was bootstrapped. Until then
the script could not have succeeded — there was no account to create `alias/feohledger-sops` in.
Now the alias exists and resolves, so the script would find it, write its ARN, seed
`backend/.env.sops` and `infra/terraform.tfvars.sops`, and print the `git add` for both.

So the scaffold goes, and the rule gets an enforcement instead of a warning. `.gitignore` ignores
`*.sops` and `*.sops.yaml` rather than re-including them, and `env-isolation.yml` fails on any
tracked sops payload, any `.env.sops`, and an in-repo `.sops.yaml` — the creation rule belongs to the
repo that holds the ciphertext. Every doc now describes the one real path: `sops
feohledger/prod.sops.yaml` from the `infra-secrets` clone.

Rejected: repointing the script at `infra-secrets`, because that repo already ships its own
`bin/sops-init.sh --project <slug>` and `.sops.yaml` rule, and two bootstraps for one key is how an
operator ends up with a second key in the wrong region (the estate script defaults to
`ap-southeast-2`). Rejected too: wiring the `carlpett/sops` provider into `infra/` now. Nothing it
would read exists yet — `prod.sops.yaml` is created with the first real secret — and a `sops_file`
data source on a missing file fails every plan. `infra/README.md` § Secrets carries the snippet for
when it is needed.

## 166. The access-log sink is SSE-S3, the one bucket not under the app key

**Decided:** 2026-09-15 · `infra/s3.tf`, `infra/kms.tf`, `infra/tests/guardrails.tftest.hcl`

Every bucket in `infra/s3.tf` was SSE-KMS under the app key — including the bucket the other three
send their server-access logs to — and the access-logging control read as done. It could never have
delivered a log. AWS's documentation for server-access logging says the destination must use SSE-S3,
and that a destination using SSE-KMS may receive log objects encrypted with a key the owner cannot
access. And nothing granted the delivery service a write in the first place: the sink had no bucket
policy for `logging.s3.amazonaws.com` and no log-delivery ACL, whatever its ownership-controls
comment said. The statement in the app key's policy that let the logging principal use the key
addressed neither problem, and its comment claimed it was the fix.

It was caught reading the module before its first apply, so no real bucket ever went unlogged. The
fix follows AWS's documented setup: the sink is SSE-S3, ACLs are disabled (`BucketOwnerEnforced`),
and a bucket policy grants `s3:PutObject` to the logging principal, pinned to this account and to the
three source buckets so no other bucket can use this one as its log destination. The logging
configurations depend on both, and the key-policy statement is gone. `guardrails.tftest.hcl` pins
the encryption, the ownership setting and the grant, because `terraform validate` cannot see any of
them and a real plan accepts the broken shape without complaint.

The exception is narrow on purpose. The sink holds signal-of-access, not the audit trail — that is
the Compliance-locked audit-logs bucket, still under the app key — and SSE-S3 still encrypts every
log object at rest. It needs no Trivy suppression: the customer-managed-key rule (AWS-0132) passes
on the SSE-S3 sink under both the version CI pins (0.70.0) and the current release.

Rejected: keeping SSE-KMS and widening the key policy further, because the problem is the
destination's encryption, not the key's grants. Rejected too: shipping the logs to CloudWatch Logs
instead, which adds an ingestion bill and a second log store for records whose only job is to exist
when an auditor asks. Dropping access logging was never an option: SOC 2 CC7.2 and AWS-0089 both
expect it.

## 167. The platform lives on feohledger.com — bought by hand, kept by Terraform

**Decided:** 2026-09-15 · `infra/variables.tf`, `infra/domain.tf`, `infra/acm.tf`, `infra/tests/guardrails.tftest.hcl`, `docs/founder-runbooks/production-deployment.md`

The account bootstrap gave FeohLedger a delegated `feohledger.jaredhoward.com` zone, and `infra/`
issued its certificate there. That suits an internal tool and not this one, because the platform
domain is not cosmetic here. Tenants live on `<slug>.<platform domain>`; the platform domain is
derived from `FEOH_TENANT_URL_TEMPLATE` (§91); the passkeys users register are bound to their relying
party's domain (§87); and customers register SSO redirect URIs under it at their own IdPs. Each of
those breaks, or makes every customer redo something, when the domain changes after tenants exist.
Nothing is deployed yet, so the move costs nothing today and more with every signup.

The domain is bought by hand, not through `aws_route53domains_domain`. That resource needs the
registrant's name, address and phone number as configuration — which would have to live in this
public repo, or be threaded in from a private one, for a one-time purchase — and it owns the
registration, so a destroy, or a refactor that renames the resource, can deregister the product's
domain. Terraform owns the part that must not drift once the domain exists:
`aws_route53domains_registered_domain` adopts the registration, keeps it auto-renewing,
transfer-locked and private in WHOIS, and points its name servers at the zone the certificate
validates in. Destroying that resource removes it from state and leaves the registration alone.

`domain_name` now accepts only a name directly under a TLD, because a registration exists only for the
apex; pointing it back at the estate subdomain fails at plan instead of at the Route 53 Domains API. A
nested platform domain (`app.feohledger.com`, the shape `docs/minimal-deployment.md` uses on its
single VM) would need that guard relaxed and the zone lookup split from the registered domain; nothing
on AWS uses one today.

The delegated zone and its NS record in `jaredhoward.com` are retired as an operator step, recorded in
`docs/followups.md`. The delegation has to go before the zone, or the name is left delegated to name
servers someone else can try to claim, and the estate bootstrap cannot express that order: the zone's
`prevent_destroy` fails the plan, and the stage that owns the NS record does not run once
`create_subdomain` is false.

## 168. Tenants live on `<slug>.feohledger.com` on every deployment shape, so every provisioning path checks the slug

**Decided:** 2026-09-15 · `deploy/env.example`, `deploy/README.md`, `deploy/tenants.caddy.example`, `docs/minimal-deployment.md`, `docs/production-deployment.md`, `docs/founder-runbooks/`, `infra/acm.tf`, `backend/app/services/tenant_provisioning.py`, `backend/scripts/create_tenant.py`

The two deployment shapes disagreed about where a tenant lives. The Terraform certificate (§167)
covers `feohledger.com` and `*.feohledger.com`, so the AWS path puts `acme` on `acme.feohledger.com`.
The single-VM path — `deploy/env.example`, its Caddy host list and `docs/minimal-deployment.md` — put
it one label deeper, on `acme.app.feohledger.com`, with the relying party, CORS domain and tenant URL
template all on `app.feohledger.com`. Moving a pilot from the VM to AWS would have changed every
tenant's URL, which is the move §167 says gets more expensive with every signup: passkeys are bound to
their relying party, and customers register SSO callbacks under the host.

Both shapes now use the apex. `FEOH_TENANT_URL_TEMPLATE` is `https://{slug}.feohledger.com`; the
relying party and CORS domain are `feohledger.com`, with `https://*.feohledger.com` as the tenant
origin; the marketing and signup surface is the apex itself (Caddy's `APP_DOMAIN` block, which
`hostRouting.ts` classifies as the platform apex); and the API stays on `api.feohledger.com`. One
certificate and one wildcard DNS record serve either shape, and the VM-to-AWS move changes no tenant
URL.

What it costs is that every first label under `feohledger.com` is now a candidate tenant slug, so a
slug must never be able to claim an infrastructure host. `RESERVED_SLUGS` in `app/utils/slug.py`
already holds `api`, `app`, `www`, `mail`, `docs`, `status` and the rest, and the email-intake host
`ap` is out of reach because a slug needs at least three characters. But a reserved list protects
hosts only if every path that creates a tenant checks it. Signup and partner provisioning called
`validate_slug_format` before `provision_tenant`; the operator CLI, `scripts/create_tenant.py`, did
not, so `create_tenant.py --slug api` would have provisioned a tenant whose URL is the API host.
`provision_tenant` now validates the slug before any database work, so no caller can skip it, and the
CLI turns the rejection into a plain error and exit code 2. `deploy/add-tenant.sh` keeps its own,
looser pre-check; the backend is the authority, and the script stops under `set -euo pipefail` before
it touches Caddy.

Rejected: keeping `app.` for tenants. It leaves the apex free for a separate marketing site, but the
SPA already serves marketing at the platform apex, and it costs every tenant URL an extra label and
every certificate an extra `*.app.` SAN, for a separation nothing uses.

## 169. An exception's segregation subject is the payable it blocks, not whoever raised the flag

`docs/followups.md` carried this as *"exception resolution has no
segregation-of-duties check"* with a prescribed fix: add
`exceptions.raised_by_user_id`, thread it through `create_exception`, refuse the
raiser in `record_decision`, NULL permissive. The premise was checked before the
code was written, by enumerating every raise site, and it does not hold on its
own.

`services/exception_service.create_exception` is the only place an `Exception`
row is constructed, so the enumeration is exhaustive: **eleven call sites, and
at exactly one of them is the signed-in actor the person the flag exists to ask
about.** Five have no user in scope at all (the extraction worker's semantic
duplicate and extraction-failure rows, `payment_erp_sync`'s sync-back failure,
the `payment_reconciler` sweep, the settlement-mismatch check, the inbound ERP
webhook). Three more run from a door that *does* have a user who did not cause
the finding: `invoice_warnings._ensure_exception` raises nine of the fifteen
types and is reached from fifteen callers including two sweeps and two agent
resolvers, so a `duplicate` flag would be attributed to whoever next PATCHed the
invoice rather than to whoever created the duplicate; `api/positive_pay` records
what the *bank* said about a cheque, imported by an operator who did not alter
it; `api/payments` opens a compliance hold from a screening verdict, twice from
unattended retry paths. `services/review._reject` has the actor and is still
wrong to stamp — that row notifies AP of a decision the reviewer already made
and audited, not a second look at their act. Only
`api/vendors._flag_payable_invoices_for_bank_change` qualifies.

So the raiser axis alone would be close to inert, and — worse — the way to make
it *look* busy is to stamp whoever was signed in, which manufactures a refusal
against a bystander and an absolution for whoever really caused the flag. That
is the same error §141 and §152 declined to make by backfilling, committed
forward instead of backward.

**The control's real subject is the payable.** A flag is a detector's finding
about an invoice's contents, so the person with a motive to clear it is whoever
created or shaped that invoice — and `approval_chain.violates_segregation`
already refuses that exact set (`Invoice.uploaded_by_id` ∪
`Invoice.segregation_actor_ids`, §152) the *approval* of the same invoice. The
gap was never "no raiser column"; it was an **asymmetry**: the uploader of a
flagged payable could not approve it, but could clear the `fraud_flag` standing
between it and a payment run. `exception_lifecycle.segregation_refusal` reads
that set through the same predicate, so the queue and the approval path cannot
drift on who is implicated in a payable, and the axis binds on every
invoice-linked exception rather than on the handful with a recorded human.

**The raiser column ships anyway, and it is not decoration.** The
implicated-actor set cannot reach the one case the estate had already written
down as open: the approver of a vendor bank-detail change is not the uploader of
the invoices that change re-points, so `docs/authentication.md` recorded the
compensating `fraud_flag` as *"not a second control against the same actor:
exception resolution has no segregation check either"*. Migration `0098` adds
the column, the bank-change approval is the one site that fills it, and the BEC
chain's last link closes. Dropping the column would have left the highest-value
fraud surface in the app uncovered by either axis.

The column's contract is therefore narrower than "who was signed in": *the
control-plane user whose own act this exception exists to have a second person
look at*. Because that is a judgement rather than a lookup, omission cannot be
allowed to read as an oversight — `tests/test_exception_raiser_stamping.py`
walks the syntax tree of every `create_exception` call, requires the kwarg, and
requires a literal `None` to be declared with its reason, exactly as
`test_invoice_uploader_stamping.py` does one table over. It also pins that the
model constructor still has one caller, since the scan is only exhaustive while
that holds.

Four scoping calls inside it:

1. **Only the verbs that release money, and the scope is borrowed rather than
   re-declared.** `escalate` is not refused: an `escalated` row still blocks a
   payment run (`blocking_exception_types` excludes only `resolved` and
   `dismissed`), so handing the decision on is the behaviour the control wants,
   and refusing it would trap the row with no exit but a role change. Nor are
   non-blocking types: clearing a `po_mismatch` or a `missing_data` releases
   nothing, and a refusal there is friction with no control behind it. Scope is
   `is_payment_blocking`, which reads `api/payments`'
   `PAYMENT_BLOCKING_EXCEPTION_TYPES` — the same tuple the audit row already
   advertises as `payment_blocking` — so adding a type to it extends this
   control for free and there is no second classification to keep in step.

2. **One chokepoint, because there are three doors.** The check is in
   `record_decision`, not in the routes: the single-row resolve, `/bulk/resolve`
   and the agent coordinator all pass through it, and a per-route check would be
   written three times and missing from the fourth door somebody adds. The
   function also **loads the invoice itself** when a caller omitted it, because
   a control that switches off when an optional argument is absent is the
   omission-reads-as-oversight failure again; `org_settings` defaults to
   enforcing for the same reason, so a forgetful caller gets an unwanted refusal
   rather than a silent bypass. `/bulk/resolve` pre-checks with the pure
   predicate because it owes a per-**row** reason — a refusal is another
   `skipped` entry beside `not_found` and `already_resolved`, never a 409 for
   the batch, since the queue is worked a filtered page at a time and one
   refused row must not take down the sweep. It batch-loads the invoices it
   needs through `invoices_for`, which replaced `correlation_ids_for`: the
   correlation was already being fetched per batch, the check needs the same
   rows, and two batch queries over one id set would have been a fork waiting to
   drift.

3. **Default-on with a per-org opt-out, in the settings block the queue already
   reads.** `settings.exceptions.require_segregation`, next to
   `auto_assign_by_type` and `sla_hours_by_type`, mirroring
   `payment_controls.run_segregation_enabled`'s
   `settings.payments.require_run_segregation` so the three identity-level money
   controls are configured alike. Only a literal `false` disables it — the
   string `"false"` (how a boolean arrives from a form) keeps the control, which
   is pinned. Reusing the workflow approval step's own `require_segregation` was
   rejected: an exception need not have an invoice at all (an invoice-less
   Positive Pay `fraud_flag`), reading a per-invoice snapshot would make the
   control's presence depend on a bookkeeping row — the defect
   `review.resolve_approval_config` exists to have fixed — and an org that
   permits self-approval has not thereby decided to permit self-clearing. The
   hatch is the reason this sat as a control-*design* call: a two-person AP team
   can have nobody else to work the queue, and a control that strands a payable
   is an outage.

4. **The refusal is logged, not audited, and the successful row is the better
   evidence.** A refusal changes no state, and the append-only invariant is
   about status changes; the sibling control on the approval path
   (`check_segregation`) audits none either, and a second convention on one
   control is drift. `audit_log` also carries a DB-level append-only trigger
   (0022) and is shipped to a WORM store, so a refusal row is an unprunable
   write at a caller's chosen rate — an implicated `ap_manager` could post 200
   ids and mint 200 immutable rows per request. What CC6.3 actually needs is
   already present and stronger: every **successful** `exception.resolved` /
   `exception.dismissed` row names its `actor_id` and its `invoice_id`, and the
   invoice carries `uploaded_by_id` + `segregation_actor_ids`, so an auditor can
   re-derive the control's *outcome* for every historical decision rather than
   seeing only the attempts that tripped. The refusal gets a PII-free `WARNING`
   with ids only.

Both refusal sentences are about the **caller** — an implicated actor already
learns they are in the set from the identical 403 on the approval path, and a
raiser is being told about their own act — so neither names the other actors,
the uploader, the vendor or the amount. Each names the exit (escalate, or ask
someone else), because a refusal with no next step is where an operator reaches
for the opt-out.

## 170. A machine that acts on a human's authority inherits that human's refusals

`run_agent` resolves exceptions too, and the obvious reading is that an
autonomous agent is not a human actor and should not be caught by a human's
segregation rule. That was rejected, because it mistakes what the agent is.

The agent has **no authority of its own** by deliberate design, established
twice already in that file: `actor_id` is the human who pressed the button; the
fail-closed branch refuses to auto-resolve at all when that human's real roles
are unknown, rather than self-approving on a fabricated set; and
`resolver.apply` approves through `review.approve_invoice` on those roles —
which is precisely why the coordinator already carries an `HTTPException`
handler for *that* path's segregation refusal. Exempting `via="agent"` from the
queue rule would not have let a machine decide. It would have handed an
implicated actor a one-click laundering route to the outcome the HTTP door
refuses them, which is strictly worse than the gap being closed.

So the refusal binds on `actor_id` regardless of `via`, and the interesting half
is *how* it binds. It is checked **before** `resolver.apply` rather than caught
after it, for two reasons: the answer is knowable without mutating anything, and
`record_decision` runs *after* the apply's `SAVEPOINT` closes — so a raise from
there would reach the route as a bare 403 with the exception left `open` and no
`AgentDecision` row, which is the exact regression the `HTTPException` handler
below it was written to fix. Every other way an apply can fail records a
decision and escalates; so does this. The org opt-out is read from the same
setting, so an org that disabled the control does not find the agent still
refusing.

Nothing in the shipped registry can reach the gate: `duplicate` and `fraud_flag`
are escalate-only stubs, `line_total_mismatch` and `payment_reconciliation` have
no resolver at all, so no payment-blocking type has an auto-resolving agent
today. That is the argument for testing it with one registered rather than
deferring the gate until such a resolver exists — a control written after the
resolver lands, lands as a bypass. `test_exception_agent_queue_segregation.py`
registers an auto-resolving `fraud_flag` probe and asserts the escalation, and a
companion test fails the moment a *real* auto-resolving resolver appears for a
blocking type, pointing its author at the gate it now runs behind.

## 171. The VM reads the estate's `prod.sops.yaml`, converted to dotenv — not a dotenv blob

**Decided:** 2026-09-15 · `deploy/decrypt-env.sh`, `deploy/deploy.sh`, `deploy/prod.sops.yaml.example`, `backend/tests/test_deploy_decrypt_env.py`

The VM's deploy decrypted `deploy/.env.sops` and grepped it as dotenv, but nothing said how to make
that file, and the estate could not make it: `infra-secrets/.sops.yaml` matches only
`feohledger/*.sops.yaml`, so encrypting a `feohledger/.env.sops` was refused, and the template beside
that rule was lowercase YAML for a Terraform `sops_file` read. The two ways out were a per-file rule
for a dotenv blob in sops binary format — how `meryl-green-designs` keeps its `.env.sops` — or one
flat-YAML `feohledger/prod.sops.yaml` whose keys are the env var names, turned into dotenv on the VM
by `sops -d --input-type yaml --output-type dotenv`.

YAML won on three counts. It leaves one secrets file for both deployment shapes: the ECS stack's
Terraform can read the same file under the same names (`data.sops_file.secrets.data["FEOH_SECRET_KEY"]`),
where a VM-only blob would have to be copied into a second file, and every secret rotated in two
places, the day that stack lands. sops encrypts a YAML file value by value and leaves the keys
readable, so a diff names the secret that changed and a missing key can be found without decrypting
anything; a binary blob is one opaque value. And the infra-secrets README records meryl's format as
kept verbatim for meryl's existing flow — an exception, not the estate pattern.

The conversion has edges, and the script refuses them rather than trusting the operator to know them.
Checked with a throwaway key and `docker compose config`: sops emits every value unquoted, compose's
`env_file` parser then cuts an unquoted value at ` #` and interpolates `$` (`x$yz` became `x`), and a
multi-line value arrives as a literal `\n`. `decrypt-env.sh` rejects all three, naming the key and
never the value. It also checks the decrypted file *before* moving it over the existing `.env`, where
the old inline block moved first — so a bad edit can no longer replace the file `backup.sh` and
`add-tenant.sh` read between deploys. The checks live in their own script so they can be run against
a new secrets file without deploying, and tested against a stub `sops` with no key at all.

Rejected: keeping the VM file named `deploy/.env.sops` with YAML inside. sops infers the format from
the extension, so the name would contradict the content and every command would need explicit types;
the VM copy now carries the canonical file's own name.

## 172. The platform domain's mail: Migadu mailboxes on the apex, SES app mail on `send.`, records in code

**Decided:** 2026-09-15 · `infra/email.tf`, `infra/variables.tf`, `infra/outputs.tf`, `infra/tests/guardrails.tftest.hcl`

FeohLedger needs two kinds of mail on `feohledger.com`: human mailboxes — the ACME account address,
DMARC reports, anything a vendor replies to — and the app's transactional mail. The estate already
answers both for `threkir.com`: mailboxes on Migadu, in the same account as `jaredhoward.com`, with
records on the apex; app mail through an SES-backed sender on the `send.` subdomain; one `_dmarc`
record for the pair. FeohLedger follows that shape rather than inventing a second one, with SES in its
own account as the sender, because the backend already ships an `ses` email adapter and the VM's
instance profile gives it credentials with no static key.

Two things differ from threkir, both on purpose. SES sends with a custom MAIL FROM domain,
`send.feohledger.com`, so bounces and SPF for app mail live entirely on that subdomain and the apex SPF
authorizes Migadu alone, failing hard. threkir's apex SPF also includes `amazonses.com` because its
relay sends with an apex envelope; SES with a custom MAIL FROM does not, and both senders DKIM-sign as
the apex, so DMARC aligns for each without widening the apex record. And the records are resources, not
a map pasted into tfvars: SES's DKIM tokens only exist once the identity does, so they cannot be pasted
in advance, and Migadu's targets are the same for every domain. Writing them out leaves exactly one
external value, the Migadu ownership token, and lets `terraform test` pin every record's name and target.

DMARC starts at `p=none`, set by a variable, because both senders are new and a wrong record under
`quarantine` silently loses real mail. The report address is validated to be on the platform domain:
receivers ignore a report address on another domain unless that domain publishes an authorization
record, which would leave `none` with nothing to monitor and nothing to say so.
`behavior_on_mx_failure` is `USE_DEFAULT_VALUE`, so if the `send.` MX ever goes missing SES falls back
to its own envelope instead of rejecting app mail, and DKIM alignment keeps DMARC passing meanwhile.

Not done here: SES production access, which is a console request rather than a resource, and an SES
configuration set for bounce and complaint events. SES's account-level suppression list already stops
repeat sends to addresses that bounced or complained, and a configuration set can come with the first
reason to act on those events.

## 173. The brand mark is feoh written as a split tally stick, and every icon is generated from it

Until this change the product shipped placeholders: an "AP" SVG favicon, the
letters "AP" as the sidebar mark, a CSS gradient square on the landing page, and
a stock document-with-a-tick launcher icon left over from the "Better AP" name.
Threkir, the sibling estate product, ships a designed mark: the thorn rune þ.

**The glyph is the name.** Feoh (ᚠ) is Old English for wealth, originally
cattle, and the root of *fee*; it is also the first rune of the Old English
rune-row, which makes it a natural sibling to Threkir's þ. Nine treatments of it
were compared at 132, 64, 32 and 16 px in three palettes, inside the app's real
surfaces, and all nine are kept as geometry in `assets/logo-render/gen_svg.py`
so the choice can be revisited without re-deriving it. The table of what each
one meant and why it lost is in `assets/logo-render/README.md`.

**Tally won on two counts.** It carries the product rather than only the name:
the Exchequer recorded a debt as notches across a hazel stick, split it so each
party held half, and treated the debt as settled when the halves matched, which
is the act this application exists to perform. And it still reads as ᚠ at
16 px, where a mark spends most of its life. The strongest launcher icon, the
runic coin, turned into a plain gold disc in a browser tab; the double-rule
"total" went soft below 32 px; the T-account read as a T to anyone but an
accountant.

**The palette is not the UI accent.** Gold on a navy tile was chosen over the
product's own blue-violet. The tile carries its own ground, so it reads on light
and dark hosts alike, and a mark tied to `--accent` would be tied to the one
token a white-label tenant is allowed to override (§ white-label.md).

**Generated, not drawn.** The geometry and palette live in one Python file,
which writes the SVG masters (`assets/icon.svg` full-bleed, `assets/logo-mark.svg`
rounded, and the three Android adaptive layers). `assets/gen-icons.sh` renders
every platform size from them with Inkscape and ImageMagick, and
`assets/check_icons.py` runs in CI with the standard library alone: masters must
match the generator, every raster must exist at its platform's size, and nothing
iOS or `maskable` may carry alpha, because App Store Connect rejects an icon with
any alpha channel, an indexed palette's tRNS included. The limit is stated in the
checker rather than hidden: it cannot prove a PNG was rendered from the *current*
master. Editing the geometry without re-rendering still fails, because the
committed master goes stale first.

**Android gets an adaptive icon.** The old launcher shipped legacy PNGs only,
which Android 8+ shrinks onto a white plate. The foreground layer shrinks the
glyph into the guaranteed 66 dp circle, and a monochrome layer serves Android
13's themed icons. Push notifications had been initialised with the launcher
icon, which Android draws from its alpha channel alone, so it rendered as a
solid white square; they now use a white silhouette drawable, both for the
foreground notifications the app shows and as Firebase's default for the ones
it posts while the app is in the background. `check_icons.py` fails if that
icon loses its alpha, or if any `@drawable`/`@mipmap` the app names is absent.

**The white-label fallback forks.** The "AP" placeholder was neutral, so a
tenant that renamed the product but configured no logo never displayed platform
branding. The rune is FeohLedger's identity, so swapping it in unconditionally
would have put FeohLedger's mark beside a partner's product name.
`brandTheme.ts::brandMark` shows the tenant logo when one is set, the platform
mark only while the product still carries the platform's name, and otherwise a
monogram of the tenant's own name on its own accent. The static assets
(favicon, touch icon, manifest, launcher icons) stay platform-branded, exactly
as the "AP" favicon already was; a per-tenant favicon would need a runtime
`<link>` swap, which nothing has asked for.

**The iOS build is proven on the pull request, not at release.** CI's
`mobile-ios-build` job runs `mobile-release.yml`'s exact
`flutter build ios --release --no-codesign` on a macOS runner whenever `mobile/`
changes, because the Linux Mobile job never runs Xcode's asset-catalog or
storyboard compilers and the release workflow only runs after a merge. It needs
no signing and no `GoogleService-Info.plist` (nothing in the build reads one), so
a fork's pull request proves the same build. Writing it surfaced a build that
could never have succeeded: the locked `firebase_core` and `firebase_messaging`
require iOS 15.0 while the Xcode project targeted 13.0, and Swift Package Manager
and CocoaPods both refuse that. The deployment target is now 15.0. Pinning
Firebase back was rejected as a workaround that only postpones the same raise;
it drops iOS 13 and 14, both from 2019–2020.

**The native launch screens copy the first Flutter frame rather than design
their own.** The 64dp mark sits 26dp above centre, where the `SplashScreen`
column places it, on `#F8F9FF`, the Material 3 surface of the blue seed theme,
so the hand-off from the OS to Flutter neither moves nor recolours anything.
They stay light in dark mode: the app has no `darkTheme`, so a dark launch screen
would flash before a light first frame. `values-night/styles.xml` is kept but
empty, so `flutter create .` cannot restore the template's black theme. On
Android 12+ only the system splash background is matched and the launcher icon
is left in place, because the platform scales and masks that icon itself and a
64dp mark cannot be pinned to Flutter's size and position there. Because the
colour and the offset are copies, `test/screens/launch_screen_parity_test.dart`
reads the native files and fails when they drift from the theme or the splash
layout.

**The link-preview card is template markup, not `<svelte:head>`.** Unfurl
crawlers run no JavaScript and this static SPA serves one `index.html`
everywhere, so the Open Graph and Twitter tags live in `app.html`, with the image
rendered by the same generator as the icons. The image URL must be absolute, so
its origin comes from `PUBLIC_SITE_URL`, which each deploy derives from a site
origin it already knows (`APP_URL`, `APP_DOMAIN`) and refuses when empty. There
is deliberately no hardcoded `https://feohledger.com` fallback, which would
quietly point a QA or self-hosted build's card at the platform; a bare local build
renders a root-relative path instead.

**Generated PDFs and outbound emails take the same fork on the backend,**
through `BrandContext.mark` in `backend/app/services/branding.py`: the tenant
logo, else the platform mark while the product keeps the platform's name, else
the product name alone (a renamed tenant gets plain text where the web app draws
a monogram). PDFs embed a bundled `backend/app/assets/brand/logo-mark.png`, read
once and never fetched, beside the product name, since the glyph carries no name.
Emails cannot carry a bundled file inline, so the header references
`/email-mark.png` on `FEOH_PUBLIC_URL` as a remote image with empty alt text,
emitted only for an absolute http(s) origin. Email headers previously carried no
image at all, not even a configured tenant logo; they now show the tenant logo
too, so an unbranded tenant's email is never richer than a branded one's.
