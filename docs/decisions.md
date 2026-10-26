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

---

## 174. Settings split into three sections, and the tab bar overflows into a menu rather than the page

**Decided:** 2026-09-15 · `frontend/src/lib/nav.ts` ·
`frontend/src/lib/components/layout/SectionTabs.svelte`

`SectionTabs` renders a nav group's children as one horizontal row. It was a
flex container of `white-space: nowrap` anchors with neither `flex-wrap` nor
`overflow-x`, so a group wider than the viewport widened the **document**: the
whole page scrolled sideways, clipping the sidebar and the page content, and
failing WCAG 1.4.10 (Reflow). Settings carried 15 tabs and overflowed every
screen; Billing carries 9 and needs ~1380px including the sidebar, so an
ordinary 1280px laptop hit it too.

**Two different problems, so two fixes.** Splitting the group is an
information-architecture call and does nothing for Billing; containing the row
is a rendering fix and does nothing for findability. Doing only one would have
left the other defect in place, and doing only the split would have left the
WCAG failure open on a narrower window.

**Settings became Settings + Governance + Automation.** Fifteen items in one
group was not only too many to render, it was miscategorised: `Workflows`,
`Experiments` and `Adaptive Workflows` are operational surfaces an AP manager
works in, not org configuration, and `Audit Trail`, `Access Review`,
`Retention Policy` and `Privacy & DSAR` are the SOX/GDPR evidence surfaces an
auditor reads. Nobody looking for the audit trail thinks "Settings". Two new
groups rather than four: the sidebar already carries ten top-level rows and
vertical space is finite. A fourth `Developer` group for API Keys + Webhooks
was rejected on that ground; they stay in Settings.

**Overflow goes to a `More` menu, not a scroll strip or a wrapped row.**
Scrolling hides tabs behind a gesture with no affordance that they exist;
wrapping turns the bar into two or three rows that read as broken. The menu
keeps one row, states how many are hidden, and leaves nothing unreachable. The
**active** tab is always pulled back into the visible row — a section whose
current page is only reachable by opening a menu reads as having nothing
selected. `.section-tabs-row` is additionally `overflow: hidden`
unconditionally, so the row cannot widen the page even in the frame before the
first measurement, or if the measurement logic is ever wrong.

**The measurer was the trap.** Widths are read from an off-screen copy of the
row, because once a tab is moved into the menu its width can no longer be
measured from the DOM. The first version positioned that copy `absolute` +
`visibility: hidden` and reintroduced the exact bug being fixed — 372px of
horizontal scroll at a 1000px viewport — because neither property removes an
element from the document's scrollable area. It now sits inside a zero-height
`overflow: hidden` wrapper; clipping affects painting, not layout, so children
still report true widths. It renders `<span>`s rather than `<a>`s: focusable
content inside `aria-hidden` is reachable by keyboard while hidden from
assistive tech.

**Why this went unnoticed.** The 320px reflow guard
(`tests-e2e/a11y/screen-reader.spec.ts`) only visited `/`, `/invoices`,
`/vendors` and `/payments` — every one a top-level link, where `groupForPath`
returns null and the tab bar never mounts. The one check that would have caught
a horizontally-scrolling page had no coverage of the component causing it. It
now visits two grouped routes as well.

**`NavIcon` replaced `icon: string` in the same change.** `Sidebar.svelte`
resolves icons through an if/else chain with no `else`, so an unknown key
rendered a blank 20x20 box — silently, on one row. That is how `Screening`,
`Bank Changes` and `Exceptions` came to share the warning triangle: there was no
icon for the first two, and no gate stopping them borrowing a third's. The union
catches a typo at typecheck; `nav.test.ts` catches a valid key with no branch by
reading the component.

## 175. The legal pages are published in the app, in English, and admit what is not yet decided

**Decided:** 2026-09-15 · `frontend/src/routes/legal/` ·
`frontend/src/lib/legal/`

The app shipped with no published legal documents at all. The only route with
"privacy" in its name was `/admin/privacy`, the DSAR console — an internal tool,
not a notice. `docs/ropa.md`, `docs/sub-processors.md` and
`docs/founder-runbooks/dpa-template.md` held real substance but were repo
markdown nobody outside the project could open, and the consent banner asked for
consent while linking to nothing. A platform that holds other companies'
suppliers' bank details and tax IDs, runs a supplier portal for data subjects
who have no account, and offers a self-service signup that forms a contract, was
asking people to trust it with no published terms on any of it.

Five documents now live under `/legal`: Privacy Policy, Terms of Service, Data
Processing Addendum, sub-processor register, Cookie Notice. The DPA and the
register are in that set deliberately — for a B2B processor they are what
procurement asks for first, and leaving them out would have published the half
of the story that is easiest to write.

**Amended 2026-09-22 — it is six, not five.** The set shipped with an
Accessibility Statement (`/legal/accessibility`) in the same change, which the
paragraph above omits; `frontend/src/lib/legal/pages.ts` has always carried six
`LEGAL_PAGES` entries, and `/legal` itself renders "the six documents". The
miscount propagated to root `CLAUDE.md`'s pointer row and was found by an issue
audit on 2026-09-22 (#428 § 0). Recorded here rather than edited above, because
this file is append-only — but the original sentence is wrong, and a reader
counting documents from it will come up one short.

**They render ahead of both the auth gate and the tenant probe.** Everyone with
the strongest reason to read them arrives without an account: a supplier
chasing their own data, a DPO evaluating the product, a regulator. On a tenant
subdomain the auth effect would have bounced them to a sign-in form; on the
apex, where `hasTenant` is false, the layout would have served the marketing
Landing page instead of the document. Both are silent — the link looks live and
returns the wrong thing — so `/legal/*` is matched as a prefix before either
branch.

**English only, and that is a deliberate exception to the frontend's own rule.**
Every other user-facing string in this app goes through `t()` across six
locales. A translated privacy policy is not a localized UI string; it is a
second binding text, and a mistranslated clause in a DPA is a misrepresentation
rather than a cosmetic bug. Translating them is a legal exercise with counsel
sign-off per locale, not a catalogue backfill. The exception is scoped to
`routes/legal/` and `lib/legal/` and written down in `frontend/CLAUDE.md`.

**An unknown fact renders as a marked gap, never as an invented one.**
`lib/legal/operator.ts` holds the facts only the operator can supply — the
registered entity, a postal address, the governing law, the Art 27
representatives, the hosting region — and every one may be `null`. `null` means
pending, and `Fact.svelte` renders it as `[… to be confirmed]` with a notice at
the top of the page listing what is outstanding. The alternative was to invent a
plausible entity and address, which would make the page false; an inaccurate
privacy policy is an Art 5(1)(a) transparency problem and a deceptive-practice
exposure, not merely an unfinished one. Filling a fact is a one-line edit that
removes the marker everywhere at once. A DPO is modelled as three states rather
than two, because "considered and not appointed" is a real disclosure and must
not read as "still deciding".

**The documents describe what the code does, not what a template says.** Drafting
them against the source turned up several places where the obvious boilerplate
would have been a lie: full bank account, routing and IBAN values *are* stored
in `Vendor.bank_details` (only the audit trail masks to last-4); the retention
sweep is off by default, covers two record classes and soft-archives rather than
deletes; erasure never reaches uploaded documents in object storage, nor
passkeys, nor live sessions; the DSAR export omits those documents too; data
residency is an advisory setting nothing routes on; and no SOC 2 or ISO 27001
certification exists. Each of those is stated plainly rather than papered over,
and the ones that are defects are tracked in `docs/known-issues.md` rather than
being described as features. `tests-e2e/legal/pages.spec.ts` bans the bare
certification-claim shapes outright, so the most dangerous sentence in the set
cannot be reintroduced by a later edit.

The same pass corrected the pricing page, which advertised a "SOC 2 attestation"
and a "99.9% uptime SLA" that do not exist, and a `sales@feohledger.example`
call-to-action on the reserved `.example` TLD that could never have delivered a
message.

## 176. A feature that reuses another feature's credential must carry its own switch

**Decided:** 2026-09-15 · `backend/app/config.py` ·
`backend/tests/test_audit_summary_local_first.py`

`audit_summary_enabled` defaulted to `True`. Audit-log summarization has no API
key of its own — reusing the extraction Anthropic key is the point, "no new
secret" — so on that default, an operator who configured Anthropic for invoice
*extraction* silently also began sending invoice numbers, vendor names, amounts
and audit timelines to Anthropic for a second, unrelated purpose. No per-org
setting named it and no sub-processor disclosure covered it.

Writing the sub-processor register is what surfaced it: the page either had to
disclose a default outbound flow the customer never chose, or the default had to
change. The default changed.

The local-first check could not have caught this. With no key at all the
resolver short-circuits, so `pnpm dev` behaved identically either way — the flag
only misbehaved on precisely the deployments where it mattered. That is the same
shape as the card family's default (§29/§56): harmless on a laptop, wrong in
production. The generalization is the heading — **sharing a credential is exactly
why a feature needs its own gate**, because the credential can no longer serve as
one.

Note what did *not* change. Invoice extraction resolves to `claude_vision` on any
deployed instance even with no key, and that stays: falling back to the mock
adapter would return a fabricated invoice ("Extracted Vendor Inc", 1500.00)
against a real tenant's document, which is worse than a loud provider error. So
Anthropic is a **default** sub-processor for extraction, and the register says so
rather than pretending it is opt-in. The distinction that matters is between a
default that is the feature's own declared purpose and a default that quietly
annexes a credential for a different one.

## 177. The legal set is reachable from every surface, and a footer links the index rather than a chosen subset

The published documents were linked from the marketing footer, the supplier
portal, the signup form and each other. They were not linked from the signed-in
application at all — the surface people spend a working day in, and the one both
`privacy` §19 and `terms` §18 promise to give notice of a change "in the
application" on. A promise of an in-app notice needs an in-app surface for it to
appear on, so the profile popover carries one entry to `/legal`.

The popover rather than a sidebar row: this is a reference people reach for
occasionally, not a destination, and a nav row costs a slot in the collapsed
icon rail where it would have been an unlabelled icon nobody can identify.

**The supplier portal's footer named two documents and now names none.** It
linked the Privacy Policy and the Cookie Notice, which left a Supplier User with
no route to the Terms that bind them — §3.4 binds everyone the Customer gives
access, and §2 makes them a party to the acceptable-use rules — or to the DPA,
the document that actually describes what happens to the bank details they
typed in. A hand-picked subset is also the thing that drifts: the set grew to
six documents and the footer stayed at two. It now carries one link to the
index, which lists whatever the set currently is.

That costs a supplier one click to the Privacy Policy, and the e2e assertion
changed shape to match: it follows the link and asserts the whole set is
reachable, rather than pinning an `href`. The old assertion passed against a
footer that named two of six documents, which is precisely the failure it was
supposed to catch.

**It also resolved the last mixed-language surface around the set.** The
document titles are English by design (§174), so rendering two of them beside
`m('portal.shell.footerSupport')` gave a French-locale supplier "Support ·
Légal · Privacy Policy · Cookie Notice". One translated pointer at an English
document set is honest; a half-translated footer is just untranslated. The
signup consent line went the same way, and needed a mechanism to get there:
a sentence with three links inside it cannot be split into `…Pre`/`…Post`
catalogue entries without fixing the order the links appear in, which German
and Japanese both move. So the message stays one entry carrying `{token}`
markers and `ui/LinkedMessage.svelte` splits it — the translation decides where
each link lands, and `messages_parity.test.ts`'s existing placeholder check is
what stops a locale silently dropping the Terms link out of a consent line.

## 178. A published claim gets a guard that fails, and the guard's subject is not configurable

Three of the published pages' commitments were true when they were written and
true by nothing afterwards. Each now has a check, and the three share a shape
worth stating once.

**The sanctions payload.** `/legal/sub-processors` §3.5 says only a supplier's
name and a country code reach ComplyAdvantage, Dow Jones or Refinitiv. The
interface *accepts* a tax identifier and beneficial owners, both callers
populate them unmasked, and the adapters happened not to serialise them. The
guard asserts the **exact** field set per adapter rather than the absence of a
tax ID, because a new field carrying a beneficial owner's date of birth passes
an absence check and is still an undisclosed flow. Adding a field stays allowed;
it becomes a decision that has to update the register in the same change.

**The registers themselves.** Neither `/legal/sub-processors` nor
`docs/sub-processors.md` is derived from anything, so the next adapter added
makes both wrong silently — which is how the version that shipped with the pages
listed AWS services that do not exist.
`scripts/check_subprocessor_registry.mjs` compares the adapter registries
against both. It **fails** rather than warns, unlike `check_compliance_drift.mjs`
beside it: that one is a heuristic over a diff, where a finding is a prompt to go
and look; this is an exact comparison of two lists, where a finding is a fact.
The distinction is worth keeping — an advisory check that is really exact trains
people to skim exact checks.

**The Object Lock mode**, and the general point. The DPA's Annex II says the
audit archive is Object Lock in *compliance* mode; the boot check read only the
`ObjectLockEnabled` flag, and the adapter's own docstring said *Governance*. The
fix could have added `FEOH_AUDIT_SHIPPING_S3_OBJECT_LOCK_MODE` with COMPLIANCE
as the default, which is the reflex. It does not, and that is the decision:
**a knob whose wrong setting makes a published page false is not a knob, it is a
trap.** An operator who set GOVERNANCE would not have changed a preference, they
would have falsified a customer-facing claim about data they cannot delete. The
retention *period* is configurable precisely because it is not like that — it
has to track `infra/variables.tf`, and the DPA names the figure as configured
rather than as a constant.

The three together generalise the guard rail the repo already applies to money:
a claim with a reader outside the project is an invariant, not a comment, and
invariants get a test. The cost is that a future change has to argue with a
failing check. That is the point of it.

## 179. Deleting a tenant goes documents → database → control plane, and the order is the recoverable one

`deploy/remove-tenant.sh` and `services/tenant_deletion` implement the deletion
`/legal/dpa` § 13 and clause 10 of the Terms promise within 60 days of
termination. Until now that promise had no mechanism: `deploy/` had an
`add-tenant.sh` and no inverse, and `docs/backup-disaster-recovery.md` recorded
the 60-day clause as "a calendar obligation, not a system behaviour".

**The ordering is the decision.** The instinct is to delete the control-plane
organisation row first — it is what makes a tenant reachable, so removing it
looks like the safe opening move. It is the one irreversible mistake available
here. Every object key in the bucket is `{org_id}/…` and the organisation row is
the only place that id is written down, so deleting it and then failing on the
storage sweep leaves documents nobody can find again. Ordered documents →
database → control plane, every step is idempotent and a partial failure is
re-run: the sweep finds nothing, `DROP DATABASE IF EXISTS` is a no-op, and the
control-plane transaction finishes the job.

The trade is a window where the organisation row points at a database that is
gone, so a request on that host errors. The deploy script removes the Caddy host
block and reloads *before* calling the backend, so nothing is serving that host
by then.

**Completeness is enforced against `CONTROL_TABLES`, not remembered.**
`CONTROL_DELETIONS` is held against the same frozenset that decides which tables
a tenant DB must never receive: every control-plane table is either swept or
named in `CONTROL_TABLES_EXEMPT` with a reason (only `plans` is — one catalogue
shared by every org). The check runs at import and in CI, so the next
control-plane table cannot be added without someone deciding which it is. This
is the same drift a sub-processor register has (§178) and the same answer.

Two refusals, both because the database would otherwise accept the operation
quietly. A partner organisation with children is refused, because
`parent_org_id` is `ON DELETE SET NULL` and Postgres would silently promote a
reseller's customers to standalone tenants. And the `roles` sweep is
`organization_id = :org` precisely because `=` never matches NULL, which is what
keeps the four system roles — shared by every tenant on the platform — out of
reach; a Python-side filter or `IS NOT DISTINCT FROM` would put them back in it.

**Two verification notes worth keeping.** The S3 version query was written first
as `[Versions, DeleteMarkers][][?ends_with(Key, …)]`, which parses, runs, and
matches nothing — the filter binds to the flattened projection rather than its
elements. It would have reported "removed 0 version(s)" while leaving every
backup in place, under a confirmation telling the customer they were deleted.
Caught by running it against real S3 semantics rather than reading it. And the
suffix is `/`-anchored, so deleting `acme` cannot match `not-acme` or the shared
`feohledger.dump`.

**The backup confirmation rests on a re-list, not on a loop having run.** A
review of the first version found the listing's `2>/dev/null | … || true` made an
AWS failure — throttling, a wrong region, a missing permission — indistinguishable
from "no backups exist": the delete loop iterated zero times and the confirmation
still said the backups were removed. Now stderr stays visible, a failed listing
stops the script, and after deleting it lists again and refuses to print the
confirmation unless that second listing is empty. The re-list also catches a real
race the first version missed: the nightly backup cron writing a fresh dump
between the inventory and the delete. Both were verified against real S3
semantics, and a re-run finishes the job — which is why the inventory step
tolerates an organisation that is already gone rather than refusing, so the one
leg most likely to need a retry can have one.

**What deletion does not reach is printed, not assumed.** The confirmation the
operator sends names the two residues the DPA already discloses — the shared
control-plane dump, which is not selectively editable and ages out on the
retention cycle, and any audit event already in write-once archival. A
confirmation that overstates is worse than none, because it is the customer's
evidence.

## 180. The public pages move, but only in ways that end, stop, and never hide content

The marketing landing page and the signup page read as a decade out of date:
flat cards on a flat ground, no depth, nothing that moved. They were rebuilt, and
sign-in, MFA, verify, change-password and forgot/reset password came with them —
a new user walks all of them in one sitting (signup → verification link →
first sign-in → forced password change), and a restyle that stopped at signup
would have dropped them back into the old look two clicks later.

**Motion is held to five rules, each written down because it bit.**
(`frontend/docs/ui-patterns.md` § Motion has the working form.)

1. *The last keyframe is the resting state.* `app.css` collapses durations under
   `prefers-reduced-motion`, so the final frame is everything that visitor sees.
   The hero's invoice card is therefore written to END assembled and paid, not
   blank.
2. *A hidden starting state is set by script, never by stylesheet.* `use:reveal`
   adds `.reveal` only after it attaches, and not at all under reduced motion or
   without `IntersectionObserver`. A CSS-first reveal fails as a blank marketing
   site when the bundle fails — the one outcome that page cannot have.
3. *Delays count.* A staggered entrance with `backwards` fill sits on its first
   frame, opacity 0, for its whole delay; collapsing durations does nothing about
   that. The hero's "Approved · Paid" stamp was invisible for 3.6s to exactly the
   visitors who asked for less motion, and the auth pages' error text and links
   popped in late. The reduced-motion rule now zeroes `animation-delay` and
   `transition-delay` too. The chat typing indicator, the only other delayed
   animation, already opts out with `animation: none`.
4. *Continuous motion needs an in-page stop.* WCAG 2.2.2 applies to anything
   moving on its own for over five seconds, and the OS preference does not
   discharge it. The landing page carries a **Pause animation** button;
   `data-motion="paused"` on its root pauses every animation beneath it through
   one `app.css` rule, so a new animation is covered the day it is written. The
   auth pages take the other honest answer — no continuous motion
   (`<Atmosphere still>`) — because a pause button on a sign-in form is clutter.
5. *Fade with colour, not `opacity`, at rest.* The old landing carried three
   `opacityAudit` exemptions for decorative fades; the rebuild spells them as
   `rgba()` colours and the exemptions are gone rather than re-argued.

**The ornament is rendered, and it is the brand's argument.** The mark is feoh
as a split Exchequer tally (§173): a debt notched on a stick, the stick split,
settled when the halves match — which is what matching an invoice against its
record is. `assets/marketing/tally_scene.py` builds that object in Blender from
constants and renders it with Cycles on the GPU; `gen-marketing.sh` trims it to
a 32 KB WebP (the same pixels are 243 KB as PNG). A flat illustration was the
alternative; a lit object with depth says "handled with care" in a way the flat
shape does not, on the one page where that is the claim. It is not run in CI —
there is no GPU there — so, like the icons, the committed file is what ships.
The grain overlay is an SVG `feTurbulence` filter rather than a noise bitmap:
456 bytes against ~50 KB, and no tile seam.

**One auth shell, zero-specificity defaults.** `auth/AuthShell.svelte` owns the
field, button and error styling that six pages had each copied (~80 lines
apiece), exposed under `:global(:where(…))`. `:where()` adds no specificity, so a
page overriding one control — signup's composite slug field, MFA's code input —
wins on its own scoped rule. The first cut used plain `:global()` and signup
needed `.slug-input.slug-input.slug-input` to beat it, which is the smell that
prompted the change. The shell is platform-branded by the same policy as the
favicon (`docs/white-label.md`); the supplier portal's sign-in is deliberately
not on it, because it resolves tenant branding first.

**Defects the rebuild surfaced, fixed where found:**
- signup's "try again" was `<a href="/signup">` on `/signup` — a same-route
  navigation the SPA router resolved without remounting, so the click did
  nothing and a user whose email never came was stuck. It is a button that
  returns to the filled form and focuses the email field. Review of that fix
  caught what it newly made reachable: with hCaptcha configured, the remounted
  widget container was never drawn into (implicit mode renders only what exists
  at script load) and the spent token was kept, so a retry would have failed
  server-side with no captcha in view. The retry clears the token and renders
  the widget explicitly;
- the confirmation replaced the form that held focus, dropping focus to `<body>`;
  focus now moves to its heading;
- the pricing section's "save 17%" chip was white on a lightened blue (3.72:1)
  and its volume-pricing link was distinguishable from its sentence by colour
  alone (1.03:1). Neither had ever been scanned: no public no-tenant page was in
  the axe guard. The landing page and signup now are, under reduced motion so
  contrast is measured at rest rather than mid-fade;
- the new sticky header parked a keyboard-focused control entirely beneath
  itself (2.4.11); `scroll-padding-top` on the document fixes focus and anchor
  jumps together;
- at the 320px reflow width the header CTA ran past the viewport, hidden by the
  page's horizontal clip rather than scrollable; the wordmark collapses to the
  mark there, its text kept as the home link's accessible name.

Each has a guard: `tests-e2e/marketing/landing.spec.ts`,
`tests-e2e/a11y/reduced-motion.spec.ts`, and the new cases in
`tests-e2e/signup/flow.spec.ts` and `tests-e2e/a11y/axe.spec.ts`. The
reduced-motion, focus-obscured and axe guards were run red against the unfixed
code, as was the captcha-retry case (hCaptcha stubbed, since local dev has no
sitekey); the try-again, CTA-colour and 320px guards encode a failure first
reproduced by a throwaway probe against the unfixed page.

## 181. The signed-in app gets one shared visual layer before any page is restyled

Following §180, the signed-in pages were modernised too. Forty-six routes were
restyled by five parallel workers, which made the order the decision: **the
shared layer landed first, alone**, and the page work was written against it.
Most of what a page looks like was never on the page — 43 routes render through
`PageHeader`, every table through `DataTable`'s `.grid-container`, 53 dialogs
through `.backdrop` / `.modal`, 26 lists through `.filter-chip`, 24 through
`.kpi` — so restyling those once in `app.css`, plus the sidebar and section
tabs, moved the whole app before any worker opened a route file. Five workers
each inventing a card, a table header and a hover on their own pages would have
produced five design languages; they instead inherited one and were barred
from editing it (proposals came back as report items, not diffs).

**Four constraints shaped that layer, all learned from what already existed.**
- *The tenant owns the accent.* Only `--accent` and `--accent-strong` are
  white-label overridable, so every accent-coloured effect is derived from them
  with `color-mix()` (`--accent-glow`, `--accent-wash`). The demo tenant's own
  gold-and-red brand was the check.
- *Badges are calibrated on `--surface`.* Tables, dialogs and KPI cards keep it
  as their fill; depth comes from radius and shadow, not from a glass tint that
  would move every badge's contrast.
- *The audits encode past mistakes.* Chips don't tint on hover (`badgeAudit`),
  buttons don't brighten (white-on-strong margin), decoration fades in colour
  (`opacityAudit`), and the nav keeps its label on `--text` because a brand
  accent is not chosen for small-text contrast.
- *The test suite is a user.* Nothing in the shell is sticky — a pinned tab
  strip intercepts the clicks Playwright scrolls into place and hides focused
  controls (2.4.11) — and nothing loops, which would owe a 2.2.2 control the
  shell does not carry. axe now scans every page under emulated reduced motion
  (`axe-helper.ts`), because a dialog's new entrance would otherwise let a scan
  measure a half-faded frame.

**Empty states are illustrated from generated geometry.** Eight line
illustrations replace the emoji. They are emitted as a typed data module by
`assets/illustrations/gen_empty_states.py` and rendered as inline SVG, for the
same two reasons the tally ornament in §180 could not be a flat file here: the
accent has to follow the tenant at runtime (an `<img>` cannot read a custom
property), and the frontend has no `{@html}` by design, so markup cannot arrive
as a string. Inkscape renders a contact sheet for review; CI checks only that
the module matches its generator, with a unittest proving that check fails on
an edited copy — the same shape as the icon guard (§173).

## 182. A routine DSAR masks banking; the unmasked bundle is a second, gated act

`POST /api/privacy/dsar` returned `Vendor.bank_details` and
`beneficial_owner_data` verbatim. Every other surface in the product reduces a
payee's account / routing / IBAN to a last-4 — the audit trail, the dual-control
change queue, the UI, the logs, the error bodies — so this was the one path that
put full banking credentials into a file, and it was reachable by any org admin
for any vendor with no dual control and without holding
`vendor.bank_change.approve` (issue #423).

**Masking it and stopping there was the obvious fix and the wrong one.** A data
subject is entitled to their own personal data; returning a supplier's bank
details *to that supplier* is the Art 15 right the endpoint exists to serve.
Deleting the capability to answer that request would have traded a
privilege-escalation shape for a compliance failure. So the bundle masks by
default and the unmasked variant survives behind `include_banking`, which needs
three things that a routine export does not: the `vendor.bank_change.approve`
permission, a written `banking_justification`, and a `vendor_contact` subject.
The flag is refused — not ignored — on a subject type that has no bank details,
because a silently-masked 200 would leave the operator unable to tell which
bundle they were holding.

**The unmasked disclosure gets its own audit ACTION**, not a field on the routine
row. `privacy.dsar_export.unmasked` is what an incident review greps for; folding
it into `privacy.dsar_export.details.banking_disclosure` would have made "who
pulled a supplier's full account number" a JSONB filter over every export ever
run. The justification rides that row and the `data_subject_requests` row, so
the privacy officer's own history shows it too.

**The gate is honest about its reach, and this is why it is still worth having.**
`ROLE_ADMIN` resolves to the entire permission catalogue, so on the four stock
system roles `require_permission(vendor.bank_change.approve)` admits exactly the
callers `require_roles(ROLE_ADMIN)` already admits. What it adds is
*configurability*: the granular SoD layer exists so an org can split duties that
one system role conflates, and an org that defines an admin-equivalent custom
role can now withhold this without withholding DSARs. The control that would
bite on a stock admin is a step-up MFA proof on the request, and that needs the
SPA to collect the proof — a frontend change, tracked in `docs/followups.md`
rather than half-landed here.

**What a banking secret *is* moved out of the router.** `_BANK_SECRET_KEYS` and
`_bank_details_audit_summary` lived in `api/vendors.py`; a service importing a
router to reuse them is the wrong dependency direction, and reimplementing them
would have given the audit trail and the export two answers to the same
question. They now live in `app/utils/bank_masking.py` and `vendors.py`
re-exports the private names, so every existing call site and test is unchanged.

The two reducers there have **deliberately opposite failure directions**, which
is the part worth remembering. The audit summary is a *denylist*: an audit row's
job is to say exactly what moved, and the keys it sees are the ones an AP user
just sent, so an unlisted key records verbatim. The disclosure masker is an
*allowlist*: only known display keys pass, and the named secrets **plus every key
this module has never heard of** are masked. A future display key therefore
renders as `****` in an export until someone lists it — visible and harmless —
where the denylist default would have quietly published a future secret. That
asymmetry is not an inconsistency; a file that leaves the system and a row that
stays in it do not want the same default.

Beneficial owners are reduced rather than masked: the blob is
`{"owners": [...]}` describing *other* natural persons, who did not ask for this
export. The ownership relationship (`name`, `role`, `ownership_percentage`,
`country`, `is_pep`) is disclosed and every identity-document field is dropped,
because a last-4 of someone else's passport number is still someone else's
passport number. An unrecognised shape is withheld whole — we cannot tell which
of its keys are third-party identity documents.

## 183. Erasure reaches object storage, and the retain/delete line is the money-trail line

`privacy_erasure` and `privacy_export` operated purely on database rows: no
stored document, no `WebAuthnCredential`, no session revocation, and an export
that returned *counts* of the subject's audit and notification activity rather
than the activity (issue #424). The fix needed a decision before it needed code,
because the obvious version — delete every document reachable from the subject —
destroys the invoice PDFs the erasure path deliberately preserves the rows for.

**One traversal, two lenses.** `app/services/privacy_documents.py` walks from a
subject to every object held about them, and both legs read the same list: the
export enumerates it, the erasure deletes the part it is entitled to. Writing
that walk twice would guarantee drift, and the drift would be invisible in the
worst direction — an export that lists a document the erasure leg cannot reach is
exactly the failure being fixed.

**The split follows the money trail, not "is it about the subject".** Erasure
keeps amounts, statuses, dates and the append-only `audit_log` because tax and
SOX record-keeping outrank erasure for transactional rows. A document is deleted
when its sole subject is the erased party *and it is not evidence of a
transaction* — a W-9/W-8 carrying their TIN, a supplier-authored chat attachment.
A document is retained when it is the evidence behind a payable whose row is
retained — the invoice PDF, the contract document, the expense receipt, the
archived vendor statement. Deleting one of those would leave a preserved money
row with its supporting evidence destroyed, which is what the retention argument
exists to prevent.

Two cases resolve against their appearance and are the ones worth stating.
**An AP-authored chat attachment is retained** where a supplier-authored one is
deleted: what an employee attaches to a supplier thread is company
correspondence about an invoice, and the personal data in that row is the
authorship, which `privacy_erasure` already redacts. **An expense receipt is
retained** even though it is unambiguously the employee's own document: it is the
proof behind a reimbursement that was paid, which makes it the same class of
object as an invoice PDF. The `user` erasure leg therefore deletes no documents
at all — what it deletes is the passkey material.

**Passkeys are deleted, not redacted.** A `WebAuthnCredential` is authenticator
material, not a financial record: nothing in it serves the money trail and every
byte is a handle to the erased person's device. Sessions are revoked through the
existing `session_management.revoke_user_sessions` — admin deactivation and
password reset already call it — rather than a second implementation.
Revocation is best-effort, because Redis being unreachable must not fail an
erasure whose database half is the regulated part, and access had already
stopped anyway (`get_current_user` 401s on `not user.is_active`); what this
closes is the un-blocklisted JTI living out its TTL.

**Both new legs run BEFORE the idempotency tombstone check**, which makes the fix
retroactive: a subject erased before this existed still has their documents and
passkeys, and asking again now removes them instead of returning `noop` over the
gap. "Work was done" therefore includes a deleted document or a revoked session,
not only a redacted field.

**Objects go before the commit, and a failure is re-runnable.** The DB row is the
only thing that knows a document's storage key, so committing the null first and
then failing the delete orphans the object beyond any future reach — the same
argument `services/tenant_deletion` makes for the whole-tenant case, resolved the
same way (documents → database). A pointer is nulled only for a key that actually
went, so a partial failure retries cleanly, and `documents_failed` is surfaced on
the response and in the audit row rather than folded into `status`: an erasure
that could not reach an object is not complete and must not read as though it
were.

**The export returns data, not a tally** — Art 15 is a right to the data. Audit
rows the subject authored come back with action, target and timestamp but
**without `details`**: an audit row an admin wrote *about another subject*
carries that subject's identifiers, and Art 15(4) says an access right must not
adversely affect the rights of others. Documents are enumerated as references
rather than inlined as base64, for the same reason in a different key: inlining
would put a W-9's taxpayer identification number into the body of a routine
export, and would build up to 25 MB per document into a synchronous JSON
response. Every collection is capped at 1000 rows with a `truncated` flag and its
true total, because a user with three years of audit rows would otherwise
assemble an unbounded document on the event loop.

## 184. Positive Pay files expire through the retention sweep, not an S3 lifecycle rule

The generated check-issue / ACH-authorization file legitimately holds every
payee's full account and routing number — that is its purpose, and it is why
`PositivePayItem` stores only `account_last4`. Nothing expired it: the retention
sweep reached invoices and the audit log only, and the erasure path never touched
object storage (issue #425).

Both triggers now reach it. `positive_pay` is a retention record class, and the
same shared traversal from §183 reaches the file on an erasure — a rule that
deletes on a timer and a request that deletes on demand are the same walk.

**The file expires; the row does not.** Past the window the object is deleted,
`file_key` is nulled and `meta.file_expired_at` is stamped, while `item_count`,
`total_amount`, `content_hash` and `account_last4` stay — those are the
audit-grade evidence the row exists to carry, and all of them are PII-free. The
default window is **one month** rather than the platform-wide 84, via a per-class
override (`retention_sweep.RECORD_CLASS_DEFAULT_MONTHS`): a bank consumes the
file within days, and a seven-year retention on a file of account numbers is the
opposite of what this class wants. An org whose bank has a long dispute window
can raise it.

**Why not the S3 lifecycle rule the issue asked for first**, which would have been
cheaper and is the right instrument for this shape in general. Two structural
reasons, and the second is the one that matters:

- Every object key in this bucket begins with the owning organisation's id
  (`{org_id}/positive-pay/...`), and an S3 lifecycle prefix filter is a *literal*
  prefix with no wildcard. There is no single prefix naming these objects across
  tenants, so it would take one rule per tenant, created at provisioning time,
  in a bucket Terraform owns — a lifecycle rule set that drifts with the customer
  list.
- The bucket carries a GOVERNANCE-mode Object Lock default retention
  (`infra/s3.tf`, `invoice_retention_days`, 365 by default). Object Lock does not
  stop a lifecycle rule from being *defined* — it defers the deletion of any
  version still inside its lock window. An expiry measured in weeks would sit in
  the Terraform looking like a control and never fire.

A rule that cannot fire is worse than no rule, because it answers the question.
The sweep can express what the bucket cannot: per-org configurability, a window
shorter than the bucket's lock, and a stamped row saying what happened. Tagging
objects at upload and filtering the lifecycle rule by tag would solve the first
problem but not the second, which is why it is not the answer either.

The standing caveat is that the sweep is off by default
(`FEOH_RETENTION_ENABLED`) like every other sweep in this project (guard rail 7),
so removing the exposure is now a deployment step rather than a missing
capability. Deleting the object is the part that needed building.


## 185. A discount offer's last day gets a day of grace, on decline only

`DiscountOffer.valid_until` is a business date — "you have until the 30th" — but
every guard compares it against `utc_today()`. So the final day is silently
shortened by the reader's offset from UTC: a supplier declining at 20:00 Eastern
on the last day was told `cannot decline an offer whose validity window has
closed`. East of UTC the error runs the other way, as a day of grace nobody
asked for. CI runners are UTC, so nothing ever went red over it — it surfaced
only because a laptop in EDT ran the suite after 20:00.

**The obvious fix was rejected because its premise was false.** The tracked
diagnosis proposed resolving `as_of` from the organisation's timezone, "since
the org already carries one for scheduled reports". It does not. There is no
timezone field on `Organization`, none in its settings schema, and
`services/scheduled_reports.py` resolves its own today in UTC like everything
else. That route is a new per-org setting with its UI and a migration — and it
would still only describe the *buyer*, while the party being refused here is the
*supplier*, who has their own offset and whom we store no timezone for at all.
One org timezone cannot answer a question about two counterparties.

**So the slack goes on the decline path and nowhere else.** `DECLINE_GRACE_DAYS`
(one day — real offsets span UTC-12..UTC+14, so a full day covers every
timezone's own last day) is applied by a new `decline_window_closed`, and
`decline_offer` is its only caller.

The asymmetry is the whole design, and it follows the money:

- **Declining moves nothing.** The offer simply is not taken, and `declined` and
  `expired` both land in the dashboard's `missed` bucket, so a decline recorded
  inside the grace changes no figure. It records, accurately, that somebody
  refused — which they did, on their own last day.
- **Accepting captures a discount.** The same slack there would let a buyer
  short-pay a vendor who already considers the offer dead: a payment-dispute
  defect traded for a fairness one, in the direction the money actually moves.
  Accept is gated separately anyway — by tier selection against `valid_until`
  (`best_tier_for_date` / `select_tier_for_date`) — and this change deliberately
  does not touch it.

`has_lapsed` is likewise untouched, because it feeds `effective_status` and
therefore every read surface and the captured/missed denominator. An offer one
day past its window still reads `expired` everywhere it always did; only its
decline is still accepted. That gap is pinned directly, by
`test_the_grace_does_not_reach_the_read_surfaces` and
`test_the_grace_does_not_reach_a_capturable_tier`, so a later reader cannot
"tidy" the two predicates back into one without a test naming what breaks.

What remains open is the wider question this does not answer: every other date
comparison in the product is still UTC, and a genuine business-date semantics
would need a timezone for each counterparty. That is a product decision with a
migration behind it, not a bug fix, and the safe direction — paying face value —
is the one the current behaviour already errs toward.

## 186. An invoice's GL account is a code, so the pickers label rather than re-key

`docs/followups.md` carried an entry asking that `InvoiceModal`'s GL picker be
moved onto the uuid `id` "like its three siblings", on the reasoning that the
write would then be unambiguous when two subsidiaries each hold their own
`6000`.

**Implementing that would have corrupted data.** The three siblings bind `id`
because their columns are real foreign keys — `Expense.gl_account_id`,
`RequisitionLineItem.gl_account_id`, `CatalogItem.gl_account_id`, each
`ForeignKey("gl_accounts.id")`. `Invoice.gl_account` and
`InvoiceLineItem.gl_account` are `String(100)` holding the **code**, and eight
services read them as one: `budget_service` matches `Invoice.gl_account ==
dimension_value`, `matching_rules` keys commodity rules by the code,
`tax_1099` globs box maps over it (`{"6010": …, "64*": …}`), `gl_recode`
compares vendor priors, workflow `RoutingField` routes approvals on it,
`report_builder` filters on it, `vendor_enrichment` autofills it, and the
extraction prompt ships it as the GL catalog. `RecurringInvoiceTemplate`,
`ContractLineItem` and the org-level `settings.invoice_defaults` share the
shape. Binding `id` would have written UUIDs into all of it, and nothing would
have raised — the column accepts any 100 characters.

So the picker **labels** instead: `GlAccountOption` was widened to carry
`entity_id` (it had been dropped by a `Pick<…>`), and one shared helper appends
the owning entity's name to an entity-scoped option while leaving a shared one
bare — the same shared-vs-owned distinction `/gl-accounts` already draws in its
Scope column. Both `{#each}` blocks are keyed on `acct.id`, because a `code` key
collides the moment two subsidiaries define the same one.

This makes the ambiguity visible without pretending it is resolved. **It is
not:** the server still accepts subsidiary B's `6000` on a subsidiary-A
invoice. Refusing needs `InvoiceResponse` to expose the invoice's own
`entity_id` and validation on manual write, and the real repair is an FK
alongside the string. Both are tracked in `docs/followups.md`.

The general rule: **before changing what a picker binds, read what the column
is.** Two pickers that look identical in the markup can be writing into a
foreign key and into a free-text code.

## 187. Local test runs are scoped to the diff; CI owns the full suites

Every long suite here is sharded in CI and runs on every PR: backend pytest is
`--splits 4` (~27 minutes serial, ~7 per shard) and Playwright is 14 shards. A
serial local `pytest tests/` therefore buys a slower copy of an answer that
arrives for free, and it sits on the critical path while it does.

It is also less trustworthy than CI's. Local runs go against a shared Postgres
that is routinely behind `alembic head` — on 2026-09-17 five databases sat at
`0093` against a head of `0098` — and a stale schema surfaces as a 500 from the
first request touching a new column, which reads as an application bug. So the
local failures need triaging before they can be believed, while CI's shards
create and migrate their own databases and cannot have the problem.

What a local run is for is the diff: the test files covering it, plus a
grep-driven sweep of the callers of anything shared it touched. That sweep is
the part a narrow selection misses, and it is what makes the narrow selection
safe — round 26's money-serialisation change collapsed three duplicated helpers,
and the callers of the new shared one were the only meaningful question.

A full local suite is still right when the change has no bounded caller set — a
conftest, a base model, a cross-cutting serialiser — and that should be said
out loud when it happens.

Two things landed with this. Root `CLAUDE.md` § Every change must update docs
and tests carries the rule (and stopped claiming "no tests exist yet", which had
been false for a long time). And `tests-e2e/fixtures/globalSetup.ts` now refuses
to start a run against a database behind head, naming each stale one and
`pnpm migrate:all` — the guard `docs/known-issues.md` had been asking for since
2026-09-08, built after the same trap cost three parallel sessions an afternoon.
The delay is its own lesson: that entry named its own durable fix, sized it
correctly, and still sat for nine days because it was filed as a note rather
than as work.

## 188. The exception queue's chip tallies are faceted, not whole-set

**Decided:** 2026-09-21 · `backend/app/api/exceptions.py`, `backend/app/api/sorting.py`

`GET /api/exceptions/summary` feeds three chip rows beside the queue — status,
type and (new) severity. Until now it was listed in
`tests/test_whole_set_kpi_rollups.py::_DELIBERATELY_WHOLE_SET`: its status
counts took no filter at all, and `by_type` took only `status`. That was
defensible while the queue had no search box. Once it gained one (issue #443),
a whole-set tally is the defect §48 describes: search for one vendor and the
chips go on counting the tenant above a one-row table.

§48's rule is that a counts endpoint takes every filter except the dimension it
tallies. With three chip rows there are three dimensions, so the rule applies
**per row**: the status counts honour type, severity, assignee and search; the
type counts honour status, severity, assignee and search; the severity counts
honour status, type, assignee and search. Every chip then reads what the table
would show if that chip were clicked, given everything else selected — and the
selected chip in each row equals the table's `total`. All three tallies, the
list and the `/ids` resolver go through the one `_exception_list_filters`, so
none of them can mean something different by a filter; the exemption is
retired and the module joins `_SHARED_BUILDER_MODULES`.

The considered alternative was to keep every row whole-set and add search only
to the list. Rejected because the rows would then contradict the table the
moment any chip in another row was on — `Open 12` above the 3 open fraud flags
a type chip had narrowed to — which the old `by_type`-follows-`status` rule had
already conceded for one pair of rows.

Two smaller calls rode along:

- **An omitted `status` means every status on all three endpoints.** The summary
  used to count its types within `open` when no status was sent, so a bare call
  described a different set from a bare list call; and `status=all` on the list
  was passed into the `IN` clause, where it matched nothing. The queue always
  sends the status it is showing, so nothing observed either difference — which
  is why they were worth removing before something did.
- **A sort key whose NULL means "no value" trails in both directions.**
  `resolve_order_by` takes a `nulls_last` set. `due_at` is NULL when no SLA is
  configured for the type, and Postgres ranks NULL above every value, so a
  descending "Due" click would have led with every exception that has no
  deadline. Opt-in per key, because on other columns the default was not wrong,
  just unconsidered. Severity sorts by `exception_lifecycle.EXCEPTION_SEVERITY_RANK`
  rather than by the text column, whose alphabetical order puts `info` between
  `error` and `warning`; an unranked value ranks 0 so it sinks instead of posing
  as urgent.

## 189. An open credit memo can be edited; an applied one never — and edit, apply and void share one row lock

`POST /api/credit-memos` had no counterpart for correcting what it wrote. Both
application paths refuse a currency mismatch, so a memo keyed in the wrong
currency was permanently unappliable, and the only way out was Void and
re-create — a void row in the audit trail for what was a typo (issue #443).

`PATCH /api/credit-memos/{id}` now corrects it, and the boundary it draws is the
money boundary: **only a memo that has never been applied**. That is the whole
of "never rewrite a settled money record" here, because application is
all-or-nothing — status, invoice link, `applied_at` and `applied_by` are written
together in one transaction and nothing reverts them — and because only
`applied` rows are netted off a payable, so an open memo's amount is not yet
anything a payment run reads. The guard checks the link and the timestamp as
well as the status anyway: a future "reopen" path must not make a settled record
editable just by flipping one column.

Considered and rejected:

- **Editing an applied memo, with the invoice balance re-checked.** It would
  restate a credit a payment may already have been booked net of
  (`payment_runs.net_payable_amount`); the executor's stale-amount refusal would
  catch the overpayment, but the ledger would already say something different
  from what was approved. Void (for an open memo) and a new memo remain the
  audit-honest path once money has moved.
- **Letting the PATCH set `invoice_id`.** Linking a memo is applying it, with its
  own guards and its own `credit_memo.applied` audit action. The schema forbids
  the field (`extra="forbid"`) rather than ignoring it, so a caller can never
  believe it applied a credit that it did not.
- **Optimistic concurrency (an `updated_at` token) instead of a lock.** A token
  protects an editor from another editor; the race that matters here is an edit
  against an *apply*, which carries no token. So the edit reads the memo
  `FOR UPDATE` before its checks, and `/apply` and `/void` now lock it too. The
  apply side was a real gap before any PATCH existed: two applies of one open
  memo to two different invoices both read `open`, both passed, and left one
  `credit_memo.applied` row per invoice for a credit that reduced only the
  second. The UI sends only the fields that changed, which keeps an edit from
  re-asserting untouched fields over a concurrent one without needing a token.

Every effective edit writes one `credit_memo.updated` row carrying the old and
new value of each changed field (money as string-Decimal); an edit that changes
nothing writes nothing.

## 190. A `page.route` stub that answers a dev-server module fails the test by name

Four `/credit-memos` e2e specs failed on every laptop and passed in every CI
run. They stubbed the vendor list with `**/api/vendors*` and
`/\/api\/vendors/`, and under `vite dev` the app's own source is served over
HTTP: `$lib/api/vendors.ts`, imported by `VendorPicker`, is the URL
`/src/lib/api/vendors.ts`. The stub answered that module with JSON, the route's
code never loaded, SvelteKit rendered its 500 page, and the specs timed out
looking for a row. CI serves a preview build of hashed `/_app/immutable/`
chunks, which no API pattern can match — so the difference was the server, and
nothing in the failure pointed at it.

The specs now match the exact API pathname, the pattern `adaptive/` and
`experiments/` had already adopted after hitting the same trap. What was missing
was a way for the NEXT such stub to announce itself, so the `page` fixture now
records every module script the page receives a JSON body for and fails the test
at teardown naming the module. Only a stub can produce that — Vite serves every
module, JSON imports included, as JavaScript — so the check has no false
positives, costs one passive listener, and never fires in CI's preview build,
which is the one place the bug cannot occur.

Considered and rejected:

- **Serving a preview build locally.** It would hide the defect rather than fix
  it, and the local loop needs `vite dev`'s on-demand transforms for source
  edits to show up without a rebuild.
- **A static source guard over `page.route` patterns.** Eleven other specs use
  a broad glob and dispatch on the pathname inside the handler, which is
  correct; a pattern scan cannot see that, so it would flag them all and push
  the guard towards being switched off. The runtime check keys on the effect,
  not the spelling.

## 191. A password that cannot sign in cannot authorize a factor change either

**Decided:** 2026-09-21 · `backend/app/api/auth.py` · `backend/tests/test_sso_only.py`

§163 placed the login-time hash upgrade after the SSO-only refusal, on the
reasoning that a tenant which has closed password login has made the hash
unreachable for signing in. It named the residue in the same breath: signing in
is not the only consumer of `User.hashed_password`. Every change to a second
factor — TOTP enroll and disable, passkey register and delete — demands a
step-up, and the account password was one of its three proofs. So in an
SSO-only tenant the hash went on authenticating the one security-sensitive
operation it still could, and for a row written before c6a91396 it did so on raw
bcrypt that no sign-in would ever upgrade.

The follow-up offered two fixes: upgrade from the step-up path too, or stop
accepting the password as a step-up proof there. **The second was taken, and
the first turned out to be unnecessary once it was.** A tenant that turns
`sso_only` on has said the password is not an authenticator in that tenant, and
a step-up *is* an authentication. So `_step_up_satisfied` drops an offered
password before `mfa.step_up_verified` sees it, through one predicate —
`_password_sign_in_closed`, which is `services/sso.is_sso_only`, the same call
the login refusal makes. The two doors therefore cannot disagree about whether
the password authenticates: it proves a step-up exactly when it would prove a
sign-in, and `is_sso_only`'s requirement that `sso.enabled` be true (a broken
IdP config keeps the password open as the escape hatch) reopens both together.
With the step-up closed, the legacy hash authenticates nothing that matters
while the tenant stays SSO-only, and the day the tenant leaves SSO-only the
owner's first password sign-in upgrades it through the path that already
exists. Wiring an upgrade into the step-up would have meant a write from a pure
helper shared by four endpoints on two surfaces, for a credential the tenant has
retired.

Four calls inside it:

1. **The rule lives in `_step_up_satisfied`, not at each caller.** Every route
   that asks "is the step-up satisfied?" goes through it, so a fifth factor
   operation added later inherits the rule rather than having to remember it.
   `mfa.step_up_verified` stays pure — no org, no DB — and is told
   `password=None`; its docstring says the admissibility call is the caller's.

2. **The password is never verified there, so the refusal cannot enumerate.**
   A right password, a wrong one and an account with no password at all get the
   byte-identical `400`. The sentence names the proofs that do work (an
   authenticator code, a registered passkey) instead of the generic "confirm
   your password", which in that tenant would be asking for the one proof it
   refuses. It reveals only that the tenant is SSO-only, which
   `/auth/{sso,saml}/config` already publishes to anyone, to a caller who is
   already signed in to the account. It stays a `400` — the status every other
   step-up refusal uses — and it is throttled and audited as an
   `auth.mfa.step_up.failure` exactly like a wrong password, because a stolen
   session trying the password is the signal that row exists to carry.

3. **The org is loaded only when a password was offered.** Code and assertion
   step-ups pay no extra query for a rule that cannot bind them; the refusal
   path reads the org once more to pick its sentence, which is a failure path
   and cheaper than threading a verdict out of a predicate that returns a bool.

4. **The lockout question has a bounded answer.** A step-up is only demanded of
   an account that has a live factor, and a live factor is itself a proof the
   owner holds — the authenticator for TOTP, the passkey for a passkey — so no
   account is left with nothing to offer. What goes is the password as a
   *fallback*: a member who lost their authenticator, or whose only passkey is
   bound to another host, cannot use it to step up here. That is accepted
   because, in an SSO-only tenant, those factors gate nothing: OIDC and SAML
   sign-in never consult them. A stuck factor there is an inconvenience with a
   route out (the passkey's own host, or the tenant leaving SSO-only), not a
   lockout, and keeping the password as a fallback would have kept exactly the
   door this closes.

The supplier portal is untouched: a `VendorUser` signs in with a password and no
SSO can close it, so there the password remains the step-up proof it always was.

## 192. Segregation of duties is not scoped by entity: a mirror inherits its source's implicated set

**Decided:** 2026-09-21 · `backend/app/services/intercompany.py` ·
`backend/app/services/approval_chain.py` · `backend/tests/test_intercompany.py`

§152 made segregation key on a set — `Invoice.uploaded_by_id` ∪
`Invoice.segregation_actor_ids` — and left the inter-company mirror out of it on
purpose, stamping `segregation_actor_ids=None` with the reasoning in place: the
mirror's segregation subject had always been its own creator, the routing actor,
and propagating the set while still not propagating the source's uploader would
bar a source *editor* from the mirror while leaving the source *uploader* free.
It called the real question an entity-scope one — should shaping a payable
under one entity bar you from signing its mirror under another? — and filed it.
The effect of the deferral was that the employee who uploaded a payable, or a
recurring template's author or material editor, could approve that payable's
mirror under the counterparty entity as long as someone else pressed "route".

**The answer is yes, and it follows from what segregation is about rather than
from anything entity-specific.** The subject of the control is the payable's
terms (§152, §169): whoever caused them to be what they are must not also sign
them off. The mirror has no terms of its own — `route_intercompany_invoice`
copies vendor, amount and currency verbatim and only chooses the counterparty.
So the people who shaped the source shaped the mirror, and an entity is the
wrong place for that to stop being true: entities subdivide one tenant's books
(`docs/multi-entity.md`), the approvers on both sides are the same
control-plane users, and an inter-company charge is one economic transaction
recorded twice. Scoping the rule by entity would turn "route it through a
sibling subsidiary" into a way for a payable's author to approve it.

So **both** of the source's columns travel, which is the consistency §152 asked
for: the mirror's `segregation_actor_ids` is the source's uploader ∪ the
source's own set, minus the routing actor, who is the mirror's uploader. Three
calls inside it:

1. **One definition of "implicated", shared by the gate and the mirror.**
   `approval_chain.implicated_actors(invoice)` is the uploader ∪ the set,
   stringified; `violates_segregation` is now membership in it, and the mirror
   copies it. The predicate had been reading the two columns separately, so a
   third input added to one place would have silently not reached the other —
   now it reaches both. The refactor preserves the predicate's answers: a `None`
   actor still never reads as a breach, and ids compare as strings on both
   columns (the uploader was compared as a `UUID`, which was equivalent for
   every caller, since all of them pass `UUID`s).

2. **Snapshotted at routing, like every implicated set.** §152 rejected
   resolving the set live because a payable's terms are frozen when it is
   raised; the mirror's are frozen when it is routed, so its implicated set is
   too. A later edit to the source (which records no editor today anyway)
   cannot retroactively bar or un-bar anyone on the mirror.

3. **The routing actor is named once.** They land in `uploaded_by_id` and are
   dropped from the set, the same subtraction `recurring_invoices.
   implicated_actor_ids` makes, so the two columns never appear to disagree. An
   empty result is NULL, not `[]`, the shape every creation path writes for
   "nobody beyond the uploader".

What this does **not** do: implicate the source's *approver*. Approving is not
shaping, and the same reviewer signing both halves of an inter-company charge
is ordinary practice; segregation separates the maker from the checker, not one
checker from another. A tenant with nobody left to approve a mirror (a
two-person team where one uploaded and the other routed) has the same
`require_segregation: false` step opt-out every other approval has.

`test_invoice_uploader_stamping.py` now declares the set's writers in
`_SEGREGATION_SET_WRITERS` — the recurring generator and the mirror — pins that
each passes a real value, and fails if a third site starts writing the set
without being declared, so "two writers" cannot quietly go stale the way the
old "`generate_one` is the only writer" comment would have.

## 193. Self-service signup gets its own off switch, and a closed signup is a 404 and a sentence, not a starved captcha

**Decided:** 2026-09-21 · `backend/app/config.py` · `backend/app/api/signup.py` ·
`frontend/src/routes/signup/+page.svelte` · `deploy/decrypt-env.sh`

Until now the only way to close self-service signup was to misconfigure its
abuse control: a deployed env refused to boot without `FEOH_HCAPTCHA_SECRET`, so
an operator running an invite-only pilot set the secret and left the *sitekey*
empty. That failed closed — no widget loaded, and `POST /api/signup/start`
answered 400 "Captcha is required." — but it was a closed door painted to look
open: `/signup` rendered a complete form that a visitor filled in only to be
refused, and the operator held a captcha credential for a feature they had
turned off. §176's rule applies in the other direction: a feature needs its own
gate, and borrowing another control's failure mode as one means the two can no
longer be reasoned about separately.

`FEOH_SIGNUP_ENABLED` (default `true`) is that gate. Four calls inside it:

1. **Default on.** Guard rail 7 and every existing deployment: `pnpm dev`, the
   signup e2e and any box already taking signups behave exactly as before, and
   closing signup is the deliberate act. `deploy/prod.sops.yaml.example` — the
   invite-only minimal deployment's contract, which already told operators to
   keep signup closed — now says `"false"` instead of describing the
   empty-sitekey workaround.

2. **Off is a bare 404 on all three routes, decided before anything else runs.**
   It is a router-level dependency, so it fires before the body is validated,
   before a session is opened, and before a rate limit is spent: a closed
   deployment creates no `EmailVerification` row, consumes no pending token, and
   a probe cannot burn a real visitor's per-IP budget or read slug availability.
   404 with `{"detail": "Not Found"}` — the body an unmounted route returns —
   rather than a 403 or 400 naming the feature, because that is what the
   codebase's two other whole-surface kill switches already answer
   (`cash_flow._require_enabled`, `v1_openapi._ensure_enabled`) and it says
   nothing more than a route that does not exist. Publishing the state through
   `/api/public-config` does not undercut that: the SPA needs it to render
   honestly, and the 404 already told anyone who asked.

3. **The captcha boot check follows the switch.** `_require_captcha_in_deployed_envs`
   exists to stop a public, tenant-creating endpoint shipping without its abuse
   control; with signup off there is no such endpoint, so the secret is required
   only while `signup_enabled` is true. `deploy/decrypt-env.sh` mirrors the rule
   — including every spelling pydantic reads as false, so the script and the app
   cannot disagree about whether a deploy needs the secret — and a near-miss
   spelling keeps the requirement rather than loosening it.

4. **The SPA says "closed"; the marketing page's links still lead there.**
   `/signup` reads `signup_enabled` from the public-config call it already made
   and, on an explicit `false`, renders a heading and one sentence (new
   workspaces are by invitation; an existing customer signs in at their
   workspace's address) — no form, no captcha script, no slug check, and no
   "what happens next" panel describing a flow that is not on offer. Until the
   config arrives, or if it cannot be fetched, the page stays the form it always
   was and the server remains the authority. The landing page's "Create your
   workspace" calls to action are **not** hidden: they land on that notice,
   which is an honest answer, whereas removing or rewording them is marketing
   copy for a deployment shape the landing page was not written for — an apex
   marketing page is a public-SaaS artefact, and an invite-only deployment
   choosing what to say there instead is a product call, not a consequence of
   the switch.

A verification link emailed while signup was open survives the switch
unconsumed, so re-opening signup lets that visitor finish rather than
stranding them.

## 194. An invoice's GL code must resolve in its own entity's chart — refused only when it belongs to another

§186 made the cross-entity ambiguity visible: in the consolidated view the
invoice pickers offered subsidiary B's `6000` for a subsidiary-A invoice, now
labelled as B's. Every manual write still accepted it, and the stored string
then resolved against A's chart — a different account, or none — while budgets,
matching rules, the 1099 box map, approval routing and the report builder all
read it as A's. `services/gl_chart.refuse_foreign_gl_codes` now closes that, on
every path that writes the column: create, `PATCH`, the line-items replace,
approve-with-corrections (and through it the GL-coding exception agent), CSV
import and recurring-template writes.

Four calls shape it.

**The chart is the invoice's entity's, never the sidebar's.** Create checks the
entity the row will be filed under (`get_write_entity_id`: the selection, else
the default); every other path checks `invoice.entity_id` (or the template's).
The picker follows the same rule rather than the `X-Entity-ID` view: the
consolidated view is every subsidiary's chart, and a deep link can open another
entity's invoice while one is selected. So `GET /api/gl-accounts` took a
`chart_entity_id` parameter and `InvoiceResponse` exposes `entity_id`; filtering
the header-scoped list in the browser would have been silently incomplete in
exactly the deep-link case. The parameter widens nothing — the consolidated read
already returns every entity's rows to every role.

**What is refused is "another entity's", not "not in my chart".** A code in no
chart at all — hand-typed, or on a retired account — still writes. Refusing it
too is the stricter rule `gl_recode` and extraction apply to *automated* codes,
and it may well be right for manual ones; but it is a different decision with
costs this one does not have: CSV import is a historical-migration path whose
rows legitimately carry codes whose accounts are long gone, a tenant's chart may
be partial (a few hand-made accounts awaiting the first ERP sync), and every e2e
fixture that codes to a literal would have to create its account first. The refusal
chosen here is never correct for the invoice it is written to; the stricter one
sometimes is. It is filed separately rather than smuggled in.

**Ownership is read over retired rows too.** A code A holds only as a retired
account, and B holds live, still resolves to A's retired account on an A
invoice — that is a retirement question, not a cross-entity one, and calling it
"another entity's" would be both wrong and misleading.

**Only a code NEW to the row is checked.** An invoice coded across entities
before this existed must stay editable: a `PATCH` that echoes the stored code,
or a line carried over through the delete-and-reinsert line-items `PUT`, is not
a coding decision, and refusing it would freeze the invoice the day its account
moved. The same rule `recurring`'s material-edit tracking applies — a re-sent
unchanged field is not an edit.

## 195. A generated Dart catalogue takes its call signature from the ARB, and the parity test it replaces is retired, not kept

Round 26 localized invoice warnings on mobile by transcribing all 48 codes into
`invoice_warning_messages.dart` by hand — a parameter-kind map and a 360-line
`switch` — and backstopped the transcription with a flutter test that parsed the
web's generated TypeScript. §157's generator now writes the Dart half as well,
into `invoice_warning_messages.generated.dart` (a `part` of the hand-written
library, which keeps only the per-kind formatters and the fallback rules). The
generated `switch` is byte-identical to the transcribed one, which is the proof
the transcription was mechanical enough to generate.

**Where the web generator emits a key, this one has to emit a call.** A gen-l10n
class exposes each message as its own typed method, so each code needs an arm
that calls `l.invoiceWarningX(a, b, …)` with its arguments in order. Taking that
order from the catalogue's `params` would have been the obvious choice and a
latent defect: `flutter gen-l10n` builds the signature from the ARB's
placeholder metadata, the two orders agree today only because round 26 typed
them that way, and every warning placeholder but a plural selector is a
`String` — so a swap compiles and renders a PO number where the amount belongs.
The generator therefore reads order and type from `app_en.arb`, refuses outright
when the ARB's placeholders are not the catalogue's (a missing or extra name, an
`int` on anything but a `count`), and treats an ARB-only reorder as drift: the
check names the mobile file stale even though the catalogue did not move. A code
the ARB does not state yet still gets an arm, in catalogue order, which fails
`flutter analyze` — the mobile counterpart of the web map's
`satisfies Record<string, MessageKey>`, and the second link of the same
three-guard chain.

**The cross-surface parity test was deleted rather than kept beside the drift
check.** It existed to catch a hand transcription drifting from the TypeScript;
with both files written by one run from one source, the only divergence left for
it to find is a hand edit to a generated file, which `--check` already refuses
in the same CI job that would have run it. Keeping it would have been a second
guard with no failure of its own to detect — and one that reads another
workspace's source file by relative path, so it breaks when that file moves
without anything having gone wrong. What a generated file cannot prove about
itself is that every arm it emits reaches a real sentence; the mobile test now
asserts that for every code in every locale, plus that dropping any one
parameter falls the finding back to the server's English.

## 196. The web formatter renders an unprovable currency bare by default, and the servers send the code they know

§160 made mobile render a figure with no provable currency bare and noted, in
passing, that the web still disagreed: `utils/money.ts::resolveCurrency`
returned `USD` for any null or malformed code, so the same payment row read as
dollars on the web and bare on a phone. The follow-up proposed an opt-in — a
`<Money>` prop a call site could set to get the honest rendering.

**The default flipped instead.** An opt-in keeps the substitution as what
happens when a caller forgets, and forgetting was the whole history of this
bug: `/payments` grew a second helper (`formatRowMoney`) solely to opt its
per-row cells out of its own fallback, and `RunDetailModal` and `/discounts`
each re-derived the same escape. The audit that decided it was cheap: across
`src/`, exactly one call site omits a currency entirely (the report builder's
`ResultTable`, whose measures can sum across currencies and so have none to
give), and every other call passes a code — so every render the flip changes is
one where the code really was absent. `formatMoney` now renders grouped
figures with two decimals and no symbol for a `null`, empty or malformed code,
and for one `Intl` rejects; `accounting` keeps its parentheses, which `Intl` only
offers beside a symbol. `DEFAULT_CURRENCY` survives for what needs a value — a
picker default, a form's initial value — and `formatAmountWithoutCurrency` is
now just `formatMoney` with no code, one primitive rather than two that could
round differently.

**A bare figure is only honest if the servers stop sending `null` where they
know the answer**, so the three fallback sites the entry named were each traced
to what the payload could have said:

- **Positive Pay** stamped the org's reporting currency on a check-issue file
  whose total sums `Payment.amount`. The model comment said that column was
  "already home-currency"; it is the **invoice's** currency (the home debit is
  `source_amount`). A USD cheque run for a EUR-reporting org was filed as EUR.
  The file now stamps `services/payment_runs.one_currency` over its cheques'
  invoices — the rule the run endpoints already used, moved out of
  `api/payments.py` so a service can call it — and `null` when they disagree.
- **`/analytics/by-entity`** rendered each entity row's naive `total_spend` /
  `outstanding_amount` — sums across whatever currencies that entity's invoices
  are in — under the entity's configured currency, or the org's when it had
  none. The endpoint already served `reporting_outstanding_amount`; it now
  serves `reporting_total_spend` too (the population and rollup of `/cfo`'s
  `reporting_spend`, so the consolidated row equals that tile), and every row
  renders both in the `reporting_currency` the payload names. That also makes
  the consolidated row the cross-check it claimed to be: a column in one
  currency can be summed by eye. The face-value disclosure under it said
  unconverted invoices were *excluded*; the rollup counts them at face value,
  and the sentence now says so.
- **`/payments`**' own `formatCurrency` wrote `currency ?? orgCurrency.currency`.
  It is now `formatMoney` with the payload's code, and `formatRowMoney` is gone.
- **`/bank-reconciliation`**'s statement dialog labelled every match candidate
  with the org's currency, under a comment saying `UnclearedPaymentResponse`
  carried none. It has carried the invoice's currency since the Uncleared
  bucket was fixed; the comment outlived the fact by a release.

**`PurchaseOrder` is the one figure no payload can label**, because the model
has no currency column. By-entity's Open POs column therefore renders bare,
with a note saying why, while `/purchase-orders` and `/cfo`'s accruals card
keep the org label they had: changing those is the tenant migration the entry
sized separately, and bare-rendering the PO list before POs record a currency
would replace a probably-right label with none on every row. The difference
between the two is deliberate and temporary — by-entity sums POs *across
entities that may report in different currencies*, where the org label is not
even probably right.

## 197. A purchase order records the currency its source knew, and an unrecorded one is never guessed — on screen, in the matcher, or by an agent

§196 traced every per-row money figure whose currency the server could name and
made it send one, and ended by naming the exception: `PurchaseOrder` was "the one
figure no payload can label", because the model had `total` and no currency
column. The consequences were spread across four surfaces and one control.
`/purchase-orders` and `/cfo`'s accruals card stamped the org's reporting
currency on every PO figure; `/cfo`'s by-entity table rendered Open POs bare with
a note; `PortalPOListItem.currency` defaulted to `"USD"` and the handler never
set it, so **every supplier saw every order in dollars** and a PO flip booked the
resulting invoice in dollars too. And `po_matching` compared the invoice amount
with the PO total as two bare numbers, so an invoice for **EUR 1,000 against a
USD 1,000 order read `matched` at 0%** — the amount control passing on
quantities in different units.

**The column is nullable with no default, because NULL is a fact.** Migration
0099 adds `purchase_orders.currency` as `varchar(3)`, tenant-fanned and
control-plane-no-op on 0098's pattern. A `DEFAULT 'USD'` would have been a claim
made on behalf of every row that never made one — the same substitution §160 and
§196 removed from the formatters, reinstated one layer down where no client could
see it. NULL means *no source said*, and every surface renders such a figure bare.

**The backfill copies the one answer that exists and invents none.** A PO
converted from a requisition has `purchase_requisitions.converted_po_id` pointing
at it, and `convert_requisition_to_po` copied that requisition's lines and exact
total onto the PO — the two figures ARE the same money, so the requisition's code
is the PO's. That is what `BACKFILL_SQL` copies, upper-cased and trimmed; a
requisition whose code is not three letters (`max_length=3` admitted `""` and
`"us"`) proves nothing, and two requisitions naming one PO with different codes
are two answers rather than one, so both leave NULL. Every other PO stays NULL.
The obvious proxy — the org's reporting currency — was rejected for §141/§152's
reason: a GBP-reporting tenant syncing a USD order from its ERP would have that
order relabelled GBP, and a bare figure is a visible gap a reader can ask about
where a wrong symbol is a wrong number that looks right. Contract-spawned POs
*are* recoverable in principle (the `contract.po_created` audit row carries
`details.po_id`, and the contract carries a currency), and were still left out:
that is an inference across a trail written for another purpose, and the operator's
call was the requisition link only.

**Every path that creates a PO now stamps what its source knows**, normalised
through `models/procurement.po_currency_code`: the requisition's on conversion,
the contract's on a contract-based PO, the ERP payload's on sync (`PoPayload.currency`,
which the Merge adapter maps from the unified record and the mock states), and
`USD` in the seed, matching its own invoices. `tests/test_purchase_order_currency_stamping.py`
fails a `PurchaseOrder(...)` under `app/` or `scripts/` that omits the keyword,
and a literal `None` has to be declared with its reason — the shape
`test_invoice_uploader_stamping.py` established, for the same reason: NULL can
only mean "nobody knew" if every path that *did* know wrote it down. On a
re-sync the ERP's code wins, because it is the other half of the `total` the sync
already overwrites; a payload that states none never erases a recorded one, which
is `expected_delivery_date`'s rule — silence is not a correction.

**The matcher gains a currency leg, and its three answers are not symmetric.**
`compare_currencies` yields `same`, `different` or `unknown`. `different` is a
`mismatch` with **no variance at all** — `amount_variance` / `amount_variance_pct`
are `None`, not `0`, because a zero there reads as a perfect match, and no figure
is the difference between EUR and USD amounts. `unknown` — the PO records no code
— still compares at face value, exactly as the matcher always did. Refusing there
was rejected: a missing code cannot prove a mismatch, and failing closed would
have flagged every pre-0099 PO in every tenant on the invoice's next write,
turning a control into a queue of noise. What changes is that nothing pretends it
was verified: the result says so, the invoice modal renders the PO total bare
with a note beside it, and the amount-variance warning gets its own code whose
PO figure is a `number` rather than `money` (a warning carries ONE `currency`
param — the invoice's — so labelling the PO's figure with it would assert the PO
is in it).

**The agents fail closed where a human may proceed, and that asymmetry is the
point.** A reviewer reading "this PO records no currency, so its total was
compared at face value" can go and look at the order. An agent that snaps the
invoice amount to the PO total (`amount_mismatch_v1`), or links a PO and approves
the invoice (`missing_po_v1`, `multi_po_split_v1`), is making the currency claim
itself — so each of those requires `currency_check == "same"` and escalates
otherwise. A PO with a *different* recorded currency never enters a candidate pool
at all, since it cannot match on amount; one that records *none* stays in, because
dropping it could turn two plausible candidates into a false unique one — the
same reasoning `find_po_subset` already applied to a truncated pool — and the
refusal happens at the decision instead.

**Two aggregates could not be labelled, only regrouped.** The `/cfo` accruals card
summed open POs, received goods and unposted invoices across every currency the
book holds and rendered the result under the org's symbol. Converting is not
available: an invoice carries a rate-locked `reporting_amount`, a PO carries
nothing, and fetching a rate on a read is what `multi-currency.md` forbids. So
both aggregates group instead — `accruals.by_currency` nets each currency's legs
within itself, `by-entity`'s `open_po_by_currency` gives each row one entry per PO
currency — and the flat fields stay as the naive sums, for back-compat, unrendered,
beside `total_spend` which had already earned that treatment. The web renders both
through one new primitive, `ui/MoneyByCurrency.svelte`: a line per currency, each
labelled by its own code, the `null` entry bare and never merged.

**Three defects surfaced while doing it, and all three were the same shape — a
figure read off something that did not own it.** `MatchResult.status` is shared by
the amount, receipt and inspection legs, so `_refresh_po_match` keyed on it raised
"Amount variance +0.0%" and a `po_mismatch` exception for a *failed inspection* on
an in-tolerance invoice, and told a reviewer only part of the order had arrived
when a fully-delivered one was partially *accepted*; each warning now keys on its
own leg. The open-PO accrual summed every PO the tenant had ever raised, though
`AccrualsSnapshot` has always defined it as "non-closed POs" and the card is
labelled "Open POs" — a cancelled order was a live commitment forever; it now
excludes `models/procurement.DEAD_PO_STATUSES`, the roster the budget-commitment
leg already used, moved to the model so the two cannot drift. And mobile's
`PoMatch.fromJson` read a top-level `variance_pct` the backend never sends (it
lives inside `details`), so the variance never rendered at all — with the test
fixtures carrying the same wrong key, which is why nothing noticed.

The budget-commitment PO leg reads the PO's own `currency` now rather than the
requisition's: after conversion and the backfill the two agree, and when they
stop agreeing (an ERP re-sync re-denominated the PO) it is the PO's figure being
summed, so its own label is the true one. No mobile screen shows a PO's money, so
there was nothing to relabel there — only the variance, and it is absent whenever
the currencies differ.

## 198. A mirror routed before §192 is backfilled from the routing's own record, on every status with a decision still ahead of it

**Decided:** 2026-09-22 · `backend/alembic/versions/0100_mirror_implicated_backfill.py` ·
`backend/app/services/intercompany.py` · `backend/tests/test_intercompany.py`

§192 made routing give the mirror its source's implicated set. A mirror routed
before that has no set, so the employee who uploaded or shaped the source could
still approve the mirror. §141, §152 and 0098 all refused to backfill, and the
reason was the same each time: nobody could honestly say who the missing person
was. That reason does not apply here. The source's two columns are on the source
row, so a backfill copies what is recorded instead of guessing, and
`0100_mirror_implicated_backfill` writes exactly what routing writes today. The
follow-up that asked for the backfill described it in some detail. Checked
against the code, two of its premises were wrong, one filter as worded would have
matched nothing, and one input was missing.

**Which row is the mirror comes from the routing's audit row. No column says.**
The entry paired rows on `intercompany_mirror_id` being set on both, the mirror's
`entity_id` matching the origin's `counterparty_entity_id`, and the mirror's
number being `'IC-' ||` the origin's. The FK does pair them, but it is symmetric:
each row points at the other. The entity test is symmetric too, because routing
swaps the two entity columns, so it matches in both directions. The only
condition in the entry that tells mirror from origin is the invoice number, and
`PATCH /api/invoices/{id}` can change it. A renamed pair would be skipped without
a sound, or, in a contrived case, oriented backwards, and that would write the
router onto the origin. What does record the answer is the audit row routing has
written since the feature shipped: `invoice.intercompany_routed` with
`role: "mirror"`, keyed on the mirror, naming `origin_invoice_id`, with the router
in `actor_id`. It is written in the routing transaction, and the 0022 trigger
makes it append-only. The migration orients each pair by that row and still
requires the two rows to point at each other. `created_at` order was rejected as
well: it is an inference that happens to hold, not a record. A pair linked with no
routing row is left alone rather than guessed at.

**Scope is every status with a successor, not "not yet past approval".** The
entry excluded approved mirrors because "an approved one has no decision left to
protect". §169 says otherwise. `exception_lifecycle.segregation_refusal` reads
the same implicated set when someone clears a payment-blocking exception, and
that decision only arises after approval: `PAYABLE_INVOICE_STATUSES` are
`approved`, `posted_in_erp` and `payment_scheduled`. A void can also send `paid`
back to `approved`. The one status with nothing ahead of it is `done`, which has
no successor. So the migration stamps every status except those with an empty
set in `VALID_TRANSITIONS`. It keeps that as a literal, since a migration must not
import app code, and a test re-derives it from the state machine. `failed` is
included because its status alone does not say which side of approval it failed
on.

**"Where the mirror's set is NULL" would have matched nothing that §192 left
behind.** `segregation_actor_ids` is a plain `JSONB` column, and SQLAlchemy stores
Python `None` in it as JSON `null`, not SQL NULL (`should_evaluate_none`). Every
mirror routed between 0097 and §192 was built with `segregation_actor_ids=None`,
so each one holds JSON `null`. Only mirrors older than 0097 hold SQL NULL. The
migration treats SQL NULL, JSON `null` and `[]` as "no set" (§152 already says an
empty set means the same as no set) and never touches a non-empty set. Python
reads JSON `null` back as `None`, which is how this went unseen.

**A mirror routed before §131 gets its router as its uploader.** §131 started
stamping the router onto the mirror, so an older mirror has no uploader either,
and its router could approve it. To match what routing writes today, the
backfill has to name them. The router is in the routing row's `actor_id`, which
is recorded, not inferred. The migration keeps the mirror's own `uploaded_by_id`
when it has one, falls back to that `actor_id` otherwise, and subtracts whichever
it used from the set, as routing does. Leaving this out would have closed the
§192 gap while keeping the §131 one open on the same rows.

**Each changed mirror gets an audit row, which earlier backfills did not
write.** 0081 and 0091 changed reconciliation state and a WebAuthn RP ID.
Neither is an input to a fraud control on a live payable. This migration changes
who may approve or clear one. §169 argues that the invoice's columns, together
with each decision's audit row, let an auditor work out afterwards whether the
control held. That breaks here. A mirror approved before the backfill by someone
the backfill then names would look, from its columns alone, like an approval
that broke segregation. So every mirror the migration changes gets an
`invoice.segregation_backfilled` row: keyed to the mirror's correlation id, with
the revision, the origin's id, and the old and new value of each column that
moved. Its `actor_id` is NULL for §141's reason: nobody acted. The row dates the
new set, so the earlier approval reads as compliant, which it was. The UPDATE and
the INSERT are one statement, so neither can land without the other.

**Routing and the migration share one definition, and a test holds them
together.** Routing's two lines became `intercompany.inherited_actor_ids(source,
uploader_id=…)`. The migration states the same rule as `INHERITED_SET_SQL`, byte
ordering included (`COLLATE "C"`, matching Python's `sorted`). A realdb test runs
both over every shape the column actually holds (SQL NULL, JSON `null`, `[]`,
unsorted arrays) with the excluded uploader absent, inside the set and outside
it. The migration cannot import the helper: an applied revision has to stay what
it was when it ran. So a second test parses `implicated_actors` and fails if it
starts reading any column besides the two this backfill copied. Legacy mirrors
would never carry a third input, and someone should decide whether they need
another backfill before the pin is updated.

`downgrade` is a documented no-op. The schema does not change, and the stamped
values are what the previous revision's code would write for a mirror routed
today. Undoing them would reopen the hole, and the append-only audit rows would
then describe data that no longer existed.

The follow-up prescribed `0100_intercompany_mirror_segregation_backfill` as the
revision id. That is 45 characters, and `alembic_version.version_num` is
`VARCHAR(32)`, the overflow 0086 shipped. The id is
`0100_mirror_implicated_backfill` (31), and the filename matches it.

**Residuals.** `deploy.sh` runs migrations while the previous containers are
still serving. If this revision ships in the same release as §192, pre-§192 code
could route a mirror between the backfill and the container roll, and that
mirror would be created without a set. The window lasts only as long as
`migrate_all_tenants` and the roll, and once the new containers are up nothing
can create another such mirror. That is accepted. The same evidence-based
backfill applies to two other §131 paths: CSV import and recurring
`generate-now` both recorded the acting employee on an audit row keyed to the
invoice before they stamped `uploaded_by_id`. They were left out of this revision
because they are not mirrors, and `docs/followups.md` tracks them.

## 199. A manual GL write must name an active account of the invoice's chart whenever it has one — history, and a code the request did not set, are exempt

**Decided:** 2026-09-22 · `backend/app/services/gl_chart.py` ·
`backend/app/services/csv_import.py` · `backend/app/services/extraction.py` ·
`backend/app/api/enrichment.py` · `backend/tests/test_gl_code_entity_chart.py` ·
`backend/tests/test_entity_coa.py` · `backend/tests/test_extraction_gl_validation.py`

§194 closed the cross-entity half of GL coding and deliberately left the other
half open: a code in **no** chart — mistyped into the mobile edit sheet's free
text, hand-typed through the API, sent in a CSV — and a **retired** account's
code still wrote on every manual path, while extraction and `gl_recode` already
refused both for the codes *they* chose. The web pickers hid the gap (both are
a `<select>` whenever the chart is non-empty); nothing else did, and the code
then travelled to budgets, matching rules, the 1099 box map, approval routing
and the ERP push as if it named an account. The operator made the product call
§194 would not make for them: **yes — a manual write must name an active account
of the invoice's effective chart whenever that chart has one.**

**The rule is one function and two halves.** `gl_chart.refuse_gl_codes_outside_chart`
replaced `refuse_foreign_gl_codes` on every path §194 wired — create, `PATCH`,
the line-items replace, approve-with-corrections (and through it the GL-coding
exception agent, which now escalates a vendor's dominant historical code whose
account has since been retired instead of approving it in), CSV import and
recurring-template create/`PATCH`. It refuses another entity's code always
(§194), and — only when the invoice's effective ACTIVE chart (active shared ∪
its entity's own; the shared chart alone for a NULL-entity invoice) has any
account at all — a code that is not an active account of it. That set is
exactly what `GET /api/gl-accounts?chart_entity_id=` serves the pickers, so the
server now refuses precisely what a `<select>` would not have offered, and the
free-text fallback appears exactly when the server has nothing to hold a code
to. The 422 keeps §194's shape — a string `detail` the web toast and the mobile
snackbar render verbatim — and names each code with its reason (another
entity's, retired, or not in the chart), because the fix differs for each.

**An empty active chart keeps today's behaviour.** Refusing everything there
would make a tenant unusable until its first ERP sync, and a subsidiary created
before anyone builds its chart would be unable to take a single coded invoice.
The same holds for extraction, which §194 had left with the opposite gap: in its
empty-chart branch it treated "nothing to validate against" as "accept
anything", so another entity's `6000` reached an A invoice through the one
path `gl_chart` did not cover. All three of its sites (lines, suggested header
GL, the vendor-prior recheck) now ask the same question through one closure —
membership in the catalog when there is one, `belongs_elsewhere` when there is
not — and each keeps the warning it already raised (`gl_codes_not_in_chart`, or
`gl_code_stale_prior` for the prior site, which already covered a foreign prior
in a synced chart). The follow-up proposed `gl_codes_not_in_chart` at all three;
one condition raising two different warnings depending on whether the chart
happens to be synced would have been the inconsistency. The ownership read is
the tenant's WHOLE chart, lazily and at most once, because the prior overlay
lands a code only after the document's own codes were judged — a bounded read
would have needed a second query. A synced chart pays nothing extra.

**Only a code the request sets or changes is judged**, which is §194's rule
applied to a case it matters far more for. An invoice coded to an account that
is later retired did nothing wrong, and refusing an unrelated edit — a note, a
due date, the web form's full-payload save that echoes the stored code back —
because of a field the user did not touch would freeze every such invoice the
day its account was retired. So `PATCH` and approve judge a code only when it
differs from the stored one, the line-items replace only codes new to the lines,
and a recurring `PATCH` only a changed code. The invoice modal was brought into
line with the same rule: a line carrying a since-retired code now shows it as
its own option (as the header already did) instead of rendering blank while
re-saving it, and a new line inherits the header's code only when the line
picker offers it — otherwise the save was refused naming a code the user could
not see. For the same reason the invoice-coding suggestion
(`GET /api/enrichment/invoices/{id}/suggestions`) no longer offers a dominant
historical GL code the save would refuse: it is dropped, not replaced by the
runner-up, whose dominance over the remaining rows would overstate what the
history says.

**CSV import refuses too, except rows that are history.** The importer's four
statuses were checked against the state machine rather than the comment beside
them, which called `rejected` "terminal-historical": it is not —
`POST /api/invoices/{id}/resubmit` takes it back to `ready_for_review`, then to
approval and the ERP push. `done` is terminal, and an imported `paid` row has no
`Payment` for the void path to walk back to `approved`, so those two — and only
those — are held to §194 alone: the account a payment was booked to years ago
may be long retired, or never have been created in this chart, and refusing
that would make a Day-0 migration impossible. `new` and `rejected` rows take the
full rule. Reading the status before the GL code meant moving the status checks
ahead of vendor resolution, which also stopped an unimportable-status row from
minting an `unverified` vendor stub on its way to the error — the same
"a refused row leaves nothing behind" rule §194's check already followed.

Rejected: **refusing the stored code on any write to its row** (the rule is
about coding decisions, and re-saving is not one); **a per-org opt-out** (an
org that wants free-form codes has an empty chart, which is already the
opt-out); **auto-clearing a retired code on the next save** (a silent data
change under an unrelated edit, and the approval signature and the 1099 box
map would both read the change); and **validating the recurring *generator***
against the chart it materialises into — a template whose account is retired
after it was written keeps stamping that code, which is a real gap but a
different decision (drop the code, warn, or pause the template) with no human
present to ask, so it is filed rather than guessed at.

One thing found on the way is fixed with it: `_resolve_extraction_config`
returned a BYOK org's own `settings["extraction"]` dict, which `run_extraction`
then wrote the invoice's `gl_account_catalog` and RAG `few_shot_prompt` into.
Production loads the org per job, so the damage stayed within one run, but any
caller reusing the settings — which the realdb tests did — handed an invoice
with an empty chart the previous invoice's catalog, another entity's, as its
prompt hint. It returns a copy now.

## 200. The web labels money only with a currency it can prove: an unknown row keeps its own subtotal, the org store answers `null`, and each dashboard disclosure states its own rule

§196 made `formatMoney` render an unprovable code bare, and noted three places
that still substituted one before the formatter ever saw it. All three were the
same move — reach for the org's currency when the figure's own is missing — in
three different disguises.

**`utils/currencyGroups.ts` took the org's code as a fallback parameter.**
`groupAmountsByCurrency(rows, fallback)` bucketed a row with no currency INTO
the fallback's subtotal, so EUR 100 plus a row nobody could put a currency on
rendered as one "€140"; `formatCurrencyTotals(totals, fallback)` labelled a
total the backend had reported under `""` with the org's code, although
`api/bank_reconciliation.py::_currency_totals` emits `""` precisely so such a
total is "not folded into another currency's figure". An unknown row is now a
group of its own, keyed `null`, sorted last and rendered bare. **Rejected:**
keeping the parameter with a `null` default, which is §196's opt-in again — the
parameter existed only to be handed `orgCurrency.currency`, and every caller
handed it that. Dropping the unknown rows was rejected for the reason the helper
exists at all: it understates the selection. The unknown group counts toward
`spansMultipleCurrencies`, because nothing proves it is the currency beside it,
and the warning is the cheaper error than a run refused at submit. What an EMPTY
input reads as stays the caller's call, and the audit found one caller getting
it wrong in a way the parameter had hidden: `/budgets`, `/expenses` and
`/requisitions` rendered "$0.00" for a summary that had not landed or had
failed. A zero wearing the org's code is right for a summary that answered with
nothing; an unanswered summary is no figure, and now reaches `KpiCard` as
`null`.

**The `orgCurrency` store started at, reset to and degraded to `USD`.** Mobile's
`OrgCurrencyStore` answers `null` there, and §119 is the reason: the backend's
fourth rung, `settings.reporting_currency_default`, is operator configuration no
client can read, so a client `USD` is a guess that looks exactly like a
configured answer — and it was also the answer on every page before the store
had loaded at all. The store is now `string | null`, starts and resets to
`null`, and assigns the resolver's result as-is (`if (ccy)` kept the old value
when the resolver abstained, which was the same guess with a longer memory).
Every render reader then renders bare with no further change, since §196. The
non-render readers split three ways, and the type system found every one: `m()`
params are `string | number`, and a `string | null` reaching a
`fallbackCurrency: string` or a form's `$state` fails `pnpm check`.

- A **form** needs *a* value, so it names the platform default itself —
  `orgCurrency.currency ?? DEFAULT_CURRENCY` in `currencyOptions`, the create
  modals, the credit-memo form and the expense-report / pre-approval creates.
  The store stopped being where that default comes from.
- A **label** naming what a bare number input is denominated in —
  "Auto-approve below (EUR)", the approval-matrix bands, the CFO sign-off
  threshold — reads `orgCurrency.label`: the code, or the localized noun
  "reporting currency", which is what the backend compares against whatever it
  resolves. The three hints beneath those fields said "converted to {currency},
  your organisation's reporting currency", which has no honest reading once the
  code IS the noun; they now name the concept and leave the code to the label
  directly above them.
- A figure whose **payload names its currency** reads the payload, not the
  store. The dashboard's KPIs and charts now label from
  `reporting.reporting_currency`; `/cfo`'s forecast and what-if figures from the
  cash position's `opening_balance_currency` (all three endpoints denominate
  through one `resolve_reporting_currency` and land in one `load()`); the CFO
  unrealized-FX columns from `unrealized_fx.reporting_currency`. Before, the
  dashboard could print "no exchange rate into EUR" directly above figures
  wearing `$`, because the notice read the payload and the figures read the
  store. What is left on the store is what has nothing better to read — the
  `/adaptive` thresholds and averages, the zero an empty selection costs, and
  the PO figures until POs record a currency.

Because the store is session-cached, the one page that edits a rung of the chain
(`/organization`'s Invoice Defaults) now re-resolves it after a save; otherwise
the CFO-threshold label on the same page kept naming the pre-save answer until a
reload. **Deferred rather than done:** `GET /api/organization` could name the
code the server itself resolves, fourth rung included, which would let the
store answer where it now abstains. That widens a role-projected settings
response and moves mobile's store with it, so it is filed rather than folded in.

**The dashboard's one partial-conversion banner described the wrong rule for the
figure a reader is most likely to quote.** It ORed three counts into "some
totals above exclude rows … treat them as a floor". Verified against the SQL:
`reporting.unconverted_count` comes from `invoice_reporting_amount_sql`, which
falls back to the FACE amount, so Total Amount is not a floor — it mixes
currencies; `total_paid_unconverted_count` and
`total_pending_unconverted_count` come from `payment_reporting_amount_sql`,
which refuses that fallback and leaves the payment out, so Paid and Pending are
floors. The banner is now two lines, each naming the KPI it qualifies: a
face-value line gated on the invoice count, and an excluded line gated on the
payment counts that names only the KPIs that actually lost a row. The follow-up
also gated the face-value line on `upcoming_unconverted_count`; that was
declined for the web, because the web renders no upcoming TOTAL for a
disclosure to qualify — mobile does, and words it there. What the web does
render is the upcoming list's per-row FACE amounts, which it was labelling with
the page's currency: a EUR invoice read as dollars. `UpcomingPayment` now
carries the invoice's own `currency`, and each row wears it.

## 201. The profile page learns whether the password is a proof from the server's own predicate, not from the login page's

**Decided:** 2026-09-22 · `backend/app/api/auth.py` · `frontend/src/routes/profile/+page.svelte` · `backend/tests/test_sso_only.py` · `frontend/tests-e2e/auth/profile-sso-only-step-up.spec.ts`

§191 stopped the password from proving a step-up in a tenant that has closed password
sign-in. The server side was complete. The page did not know about it. `/profile`'s passkey
card still rendered "Confirm your password" and preferred a typed password over the
passkey ceremony, and the TOTP disable form offered nothing but a password field. A
member who typed one got a 400 toast instead of never being asked. The follow-up
understated one case. A member with TOTP and no passkey had no working proof on the
passkey card at all, because the card had no code field. In an SSO-only tenant that
member could not add a passkey, and could not turn TOTP off either.

The page had to learn one bit, and the follow-up offered two sources. The first was the
public `/auth/{sso,saml}/config`, which the login page already reads. The second was a
field on `/auth/me`. **The second was taken, because the first is a different rule.** The
config endpoints echo `sso_only` only when the IdP config resolves. `_step_up_satisfied`
drops the password whenever `services/sso.is_sso_only` holds, and that needs only
`sso.enabled` and `sso_only`. The two disagree for a tenant with SSO switched on and
required and an IdP block that does not resolve. The echo says "open" there, while sign-in
and step-up both refuse the password. A page built on the echo would have kept offering
the refused field in exactly that tenant. So `/auth/me` carries `password_sign_in_closed`,
and it is not computed next to the rule. It is computed by the rule:
`_org_closes_password_sign_in(org)` is now the one function that login's refusal, the
step-up's password drop (through `_password_sign_in_closed`) and `_user_response` all
call. `test_sso_only.py` walks every shape of `settings.sso`, including the unresolvable
one, and asserts that `/me`, the step-up and login give the same answer for each.

Five calls inside it:

1. **The field names the rule, not the setting.** It is `password_sign_in_closed`, not
   `sso_only`. The setting alone does not close anything (`sso_only` without
   `sso.enabled` is open). Reusing the name the config echo already uses, with a
   different meaning, would invite the near-copy this entry exists to avoid.

2. **Where it is set, no factor change renders a password field.** The disable form asks
   for a current authenticator code instead. The factor being turned off is a live TOTP
   secret, so that code is always on hand, and the passkey button remains when a passkey
   exists. The passkey card asks for the code when TOTP is live, runs the passkey
   ceremony when the field is left blank, and asks for nothing when TOTP is not live. In
   that last case `needsPasskeyStepUp` already guarantees a passkey to run the ceremony
   with. A half-typed code is not treated as an offered proof, because the server
   refuses anything under six digits with a 422. The field is a page-local snippet rather
   than a component, so the cards' scoped field styles still apply to it.

3. **Where it is not set, nothing changes.** The password stays the typed proof, including
   the existing "leave blank for a passkey" route for a passwordless account in a tenant
   that has not closed the password. Offering the code there too would be a reasonable
   addition, but no follow-up asked for it, and the password field is correct in that
   tenant.

4. **An account with no factor is not asked for anything, in any tenant.** This is the
   "neither a passkey nor TOTP" case the follow-up raised. `_require_mfa_step_up` returns
   before any proof is checked when nothing is live, so such a member can enrol either
   factor. The page already knew that: `needsPasskeyStepUp` is false and no field
   renders. The e2e pins it so a later change cannot put a dead prompt there. The
   narrower case, an account whose only passkeys are bound to another host and which has
   no TOTP, still has no proof this host can take (§191, point 4). The Add and Remove
   buttons stay enabled. The server's wrong-host refusal names the host to use, which is
   the only honest thing this page could say. Filtering on `usable_here` to disable the
   buttons would have replaced that sentence with a silent grey button.

5. **Every refused step-up in such a tenant gets the SSO sentence, not only a refused
   password.** The page now sends an authenticator code where it used to send a
   password. A mistyped code answered with the generic "Confirm your password, …" would
   point at a field the page no longer shows. `_refuse_step_up` therefore reads the org
   on every refusal, not only when a password was offered. That is a throttled failure
   path, and the success paths still load the org only for an offered password (§191,
   point 3). The sentence is reworded to "only a current authenticator code or a
   registered passkey can confirm this change", which is correct for a refused password
   and a refused code alike. It still reveals only what the config endpoints already
   publish.

Mobile is unaffected: it deserializes `/auth/me` but manages no factors. Enrolment,
passkeys and disabling all happen on the web. The supplier portal is unaffected for the
reason §191 gives.

The disagreement that decided the source is itself a defect. A tenant with SSO switched on
and required and an unresolvable IdP block gets a login page with a password form that
every submit refuses, and no SSO button. It is recorded in `docs/known-issues.md`, with
the fix: make `is_sso_only` require a resolving config, which `_org_closes_password_sign_in`
now makes a one-site change. When that lands, `/me` follows it automatically. That is the
point of reading the rule rather than copying it.

## 202. The credit-memo invoice pickers ask the server for exactly the set the apply accepts, and the combobox under them is one component

Both `/credit-memos` invoice selects — the Apply dialog, and since #443 the
create dialog's optional "Apply to invoice" link — read one `invoices` array
that the page filled on MOUNT by walking every page of `GET /api/invoices`
(`fetchAllPages`), then filtered by `vendor_id` in the browser. That cost one
request per 100 invoices on every visit, for a list most visits never open, and
it grew with the tenant's whole invoice history. The follow-up that tracked it
proposed a `vendor_id` filter on `GET /api/invoices` feeding a combobox on the
`ui/VendorPicker` pattern.

**The vendor filter was not the whole of what the apply refuses.** Both
application paths run four guards, and vendor is only the first: the memo's
entity against the invoice's, the memo's currency against the invoice's, and
the over-application guard (the invoice must still absorb the credit). A
picker narrowed only by vendor still offered a EUR invoice for a USD memo, a
100.00 invoice for a 250.00 credit, or one already credited down to nothing —
each a 409 the operator met only after choosing. So the pickers read two new
endpoints on the credit-memo router, `GET /credit-memos/{id}/eligible-invoices`
(Apply) and `GET /credit-memos/eligible-invoices?vendor_id=&amount=` (create),
backed by one builder, `_eligible_invoices_query`, that is the SQL form of the
guards clause by clause: the caller's entity scope, `Invoice.vendor_id` equal
to the memo's vendor, the NULL-admitting entity rule, the case- and
space-insensitive currency rule with a blank invoice currency admitted, and
`amount − Σ applied ≥ credit` (or `> 0` before the create form has an amount,
since every credit is strictly positive). The Apply read takes its terms off
the memo row rather than from the client, so the list cannot describe a memo
someone has since edited, and it answers a memo that is no longer `open` with
the apply's own 409. The create read has no currency leg, because a linked
create that names no currency inherits the invoice's — which is what the dialog
sends. The contract is pinned as a set equality in
`tests/test_credit_memos.py`: over a matrix of every refusal and the admission
beside each, what the endpoint lists is exactly what the path accepts.

**Status is a leg only because it became a guard.** A picker that hid
invoices the apply accepts would be as wrong as one offering invoices it
refuses, so the picker never filters on anything the application paths do not
refuse. Building it surfaced that they accepted a credit on an invoice whose
money had already left; that was decided in the same batch (below), and the
status leg is the guard's own set.

The `vendor_id` filter landed on `GET /api/invoices` anyway, in
`_invoice_list_filters`, and so on `/counts` and `/ids` with it (decisions §48;
`test_whole_set_kpi_rollups.py` would have refused a list filter the tally
lacked). It is the exact counterpart the free-text `vendor` leg never was —
`vendor=Acme` is a substring of the invoice's own name and also matches "Acme
Holdings" — and the eligibility builder composes that builder for its vendor
and search legs, so "this vendor's invoices" and "a search term" each mean one
thing on both surfaces rather than two.

**The combobox is one component.** `ui/VendorPicker` carried every lesson the
picker had cost: the Escape capture (§146), focus that selects without opening,
the typed-versus-committed split, a count line that never presents a subset as
the whole or a failure as an empty set. A second copy would have had to be kept
in step by hand, so the control moved into `ui/SearchPicker` — generic over the
option, i18n-agnostic, with its rules in `utils/searchPicker.ts` — and
`VendorPicker` and the new `InvoicePicker` are thin wrappers that own only the
source, the label, the option row and the copy. Two properties were new:

- **The source's identity is the set.** The answer the picker holds is stamped
  with the `load` function that produced it, so a changed scope (another memo,
  another vendor) is simply not this source's answer — its options are never
  drawn, committed by Enter or announced — without an effect racing to clear
  them. The page derives each loader from its scope; the create link's is keyed
  on the vendor alone and reads the amount at fetch time, so typing an amount
  does not throw away the operator's search on every keystroke.
- **`InvoicePicker` preloads.** It fetches the unfiltered first page when its
  dialog opens (never on page mount) and the CLOSED field reports an empty or
  unreachable set in its description — for a dialog whose only purpose is
  picking an invoice, "nothing here can take this credit" is the first thing to
  know. The empty sentence is the caller's (`emptyText`), because only the
  caller knows what its set means. `VendorPicker` still fetches only on open
  and stays silent at rest: its last answer may be for a search the user has
  abandoned.

Considered and rejected:

- **Only `vendor_id` (and perhaps `currency`) on `GET /api/invoices`.** It is
  the follow-up's literal proposal, and it would have left the balance and
  entity refusals to be discovered by the 409 — the defect, narrowed rather
  than fixed. A credit-balance predicate has no business in the invoice list's
  shared builder, which also feeds the list's chips and select-all.
- **The client passing the memo's terms** (`vendor_id`, `currency`, `amount`)
  to one generic endpoint for both dialogs. For Apply the memo exists, so the
  server should read it; a client restating it can only be staler than the row.
- **A copy of `VendorPicker` renamed.** Guard rail 9, and the §146 capture is
  exactly the kind of detail a second copy loses in its first "tidy".
- **Keeping `fetchAllPages`.** It had no caller left, and its docstring invited
  the next `<select>` over a walked list; §145 and this entry are the reasons
  not to write one.

**Paid and done invoices are refused.** A credit memo does nothing by itself:
`services/payment_runs.net_payable_amount` subtracts applied credits when a
payment is built or executed. So a credit applied to an invoice no payment will
read again reduces nothing — and because application is all-or-nothing and
immutable (the memo is `applied`, can be neither voided nor re-applied), the
vendor's credit is simply gone, while their next invoice is paid in full. Both
application paths accepted exactly that for a `paid` or `done` invoice. They now
refuse it with a 409 (`_assert_creditable_status`, checked first because it is
about the invoice alone), and the memo stays open for the vendor's next invoice.

The admitted set is not a list. `CREDITABLE_INVOICE_STATUSES` is derived from
`workflow_engine.VALID_TRANSITIONS` — the rule `api/payments.SCHEDULABLE_INVOICE_STATUSES`
follows — as every status that can still reach `payment_scheduled` (where a
payment is booked) without first being `paid` (where it settles). Those two
anchors are the definition of the question, not a restatement of its answer, and
what falls out is the reasoning:

- **Refused: `paid` and `done`.** `paid` is the settled status itself; its only
  way back is the void (`paid → approved`), after which the invoice is `approved`
  and creditable again — so the refusal lasts exactly as long as the money is out.
  `done` is terminal: whether it closed after a payment or without one (`new`,
  `approved` and `sent_to_erp` can all close straight to `done`), nothing will pay
  it, and "no payment will read this" is the refusal's actual claim — which is
  why its sentence says that, rather than "already paid".
- **Admitted: `payment_scheduled`.** A payment is booked but has not settled; the
  executor's `net_amount_changed` refusal fails the stale booked payment
  retry-safely and the rebuilt run pays net. Refusing here would strand every
  credit that arrives while a run awaits approval.
- **Admitted: `rejected` and `failed`.** Both re-enter the flow (`rejected →
  ready_for_review | new`, `failed → pending | sending_to_erp`). A disputed
  invoice is typically rejected, the supplier issues a credit for the dispute, and
  the invoice is resubmitted — the credit must be able to meet it.
- **Everything upstream of a payment** (`new` through `posted_in_erp`) is
  admitted, as before. A status the graph gains later is classified by its edges,
  and a status with no entry in the graph is refused rather than waved through;
  `test_creditable_statuses_are_derived_from_the_state_machine` pins the
  derivation, not only today's answer.

The picker leg in `_eligible_invoices_query` is the same set, so the parity
tests now assert a `paid` and a `done` invoice are neither offered nor accepted,
and a `payment_scheduled`, `rejected` and `failed` one are both. The refusal
sentence is a plain `detail`, like the four application refusals beside it: none
of them carries a machine code yet, which is the cross-cutting error-contract
change the open "backend refusal sentence reaches a localized page in English"
follow-up already owns — giving this one refusal a code alone would leave the
toast inconsistent with its siblings. With the pickers offering only eligible
invoices, the sentence is reached only by a race or a direct API caller.

Considered and rejected: **refusing `payment_scheduled` too**, which would have
been the simpler "money is committed" line but refuses credits the executor
already knows how to honour; and **a literal `{paid, done}` set**, which is
today's answer but would silently mis-classify the next status the workflow
grows.


## 203. An image ref is written once, in a compose file, and CI and the deploy read it from there

Pinning every image to `repo:tag@sha256:…` gave the compose files a Dependabot
reader (the `docker-compose` entry) and left six other copies without one. CI's
pgvector and Redis `services:` containers (four jobs in `ci.yml`, one in
`sso-e2e.yml`) and both MinIO `docker pull` / `docker run` steps restated
`backend/docker-compose.yml`'s refs by hand, and `deploy/deploy.sh` held the
production frontend-build image in a shell variable. `github-actions` reads only
`uses:`, and nothing reads a shell variable. That was worse than drift. When
`dependabot-auto-merge.yml` merged a green minor/patch compose PR, the green run
had started the *old* images, so the check it trusted had tested something other
than the bump. The follow-up called `NODE_IMAGE` a restated compose ref. It was
not: no compose file declared a Node image at all. It was a ref with no reader,
and so its digest (and the alpine CVEs under it) could only go stale.

**CI reads the dev compose file through a job output.** A `compose-images` job in
each workflow runs `scripts/compose_image_refs.sh backend/docker-compose.yml
postgres redis minio` and exposes the three refs as outputs. `services.<id>.image`
is `${{ needs.compose-images.outputs.<service> }}` (GitHub's context table lists
`needs` for `jobs.<job_id>.services`), and the MinIO step pulls and runs
`$MINIO_IMAGE`, set from the MinIO output. The compose file is the only place a
ref is written, so a compose PR's own CI run starts the images it bumps. Four
details shape it:

- **JSON, not `config --images`.** `docker compose config --images` prints bare
  refs in an order that changes with the arguments, and nothing ties a line to
  its service. `config --format json` plus `jq` reads `.services.<name>.image` by
  name. Both behave the same on the runner's Compose 2.38.2 and on 5.x.
- **The reader fails closed.** If `services.<id>.image` is an empty string, GitHub
  starts no container and still runs the job. So the reader refuses anything but
  `repo:tag@sha256:<64 hex>`, and its refusals (a floating tag, no digest, no
  tag, a build-only service, a service the file does not define) are unit-tested
  at the top of the same job, before the extraction runs.
- **`ci-gate` needs the job directly.** If `compose-images` fails, every job
  behind it is *skipped*, and the gate counts a skip as a pass. The job has no
  path filter, because `backend-test`, which runs on every push, needs it.
  `backend-test` therefore starts about twenty seconds later than it used to,
  and that cost was accepted.
- **Service containers stay.** We did not replace them with `docker compose up`.
  That would read the whole compose service rather than just its ref, but CI's
  containers differ from dev's on purpose (tmpfs `PGDATA` with a 2 GB shm, no
  `init-tenants.sql` mount, start gated on health before the first step). Only
  the ref changes source. Keycloak, Mailpit, LocalStack, stripe-mock and fake-erp
  already start through `docker compose`, so they were never copied.

**The frontend build became a compose service instead of a `docker run`.** It is
`compose.prod.yml`'s one-shot `frontend-build`: the pinned Node image, the repo
bind mount and a pnpm-store volume, behind a `build` profile so `up -d --wait`
never starts it. `deploy.sh` runs it with `docker compose run --rm -T` and passes
`PNPM_SPEC` and the two public origins with `-e`, as it did before. The
`docker-compose` Dependabot entry already covered `/deploy`, so Node minor and
patch bumps now arrive as PRs. Node *majors* are ignored, because the major has
to move with every `setup-node` `node-version:` at once (`frontend/CLAUDE.md`
§ The Node floor). The cache volume became compose-managed
(`feoh-prod_pnpm-store`). Adopting the old `feoh-prod-pnpm-store` by name prints a
"not created by Docker Compose" warning on every deploy, and a warning that
appears on every deploy trains people to ignore warnings. A cache costs one cold
build to replace.

**Enforced by `backend/tests/test_container_supply_chain.py`.** Until now it
checked only Dockerfile `FROM` lines. It now fails in these cases:

- a compose `image:` is unpinned;
- a workflow value or a non-comment `deploy/*.sh` line spells out a digest or a
  compose repository's `repo:tag`;
- a `services:` / `container:` image is anything other than an output of a job
  in that job's `needs` that runs the reader for that service (a `vars.` or `env`
  indirection would be a copy kept somewhere else);
- `compose.prod.yml`'s Postgres or Redis differs from the dev file's, which is
  the one CI starts;
- the `frontend-build` Node major differs from any `setup-node`.

Both compose files sit in one Dependabot entry and one group. If the files are
ever bumped in separate PRs, the equality check keeps both red until they merge
together, which is correct: merging one would leave CI testing a Postgres that
production does not run.

**Rejected:**

- **Keep the copies and add a test that they equal the compose refs.** That
  turns silent drift into a red Dependabot PR that someone has to fix by hand.
  Auto-merge would then stop working for every bump to the three core images,
  and the copies would still exist to be edited. Reading the ref removes the
  copy.
- **A reusable `workflow_call` workflow shared by `ci.yml` and `sso-e2e.yml`.**
  The logic already lives once, in the script. Two fifteen-line jobs are easier
  to follow than an indirection, and a workflow cannot `needs:` another
  workflow's job anyway.
- **For Node, keep the shell variable and guard its major against `setup-node`.**
  That guards the major and leaves the digest with no reader.
- **A compose service that only holds the ref, with `deploy.sh` still running
  `docker run`.** That service would exist only to carry a string. Making it the
  real build container means `compose.prod.yml` describes what the VM runs.

**What this does not cover:** images CI never starts. The Authentik stack and
Ollama, which are local-only profiles, and `compose.prod.yml`'s Caddy and Node
images are still auto-merged on a green run that never started them, and the
next deploy is the first thing to run them. That gap is tracked in
`docs/followups.md`.

## 204. SSO-only closes the password only where an SSO sign-in can start, and a block that cannot start one is refused at save

**Decided:** 2026-09-22 · `backend/app/services/sso.py` · `backend/app/api/auth.py` · `backend/app/api/organization.py` · `backend/tests/test_sso_only.py` · `backend/tests/test_organization_settings_validation.py`

§201 found that two predicates decided whether a tenant was SSO-only, and that
they disagreed. `is_sso_only` looked only at `sso.enabled` and `sso.sso_only`.
The public `/auth/{sso,saml}/config` endpoints reported `sso_only` only when the
IdP block resolved. Login, the step-up and `/auth/me` used the first. The login
page used the second. For a block with both flags set and an IdP config that did
not resolve (no `discovery_url` / `client_id` / `client_secret`, or no
`idp_entity_id` / `idp_sso_url` / `idp_x509_cert`, or a malformed one) the page
showed a password form and no SSO button, and every password it submitted got
the 403. No member could start a session. §191, `docs/authentication.md` and
`is_sso_only`'s own docstring all said a broken IdP config keeps the password
open as the escape hatch. That was true only while `enabled` was false.
`PATCH /api/organization` merged the block with no validation, so an admin could
reach the state in one save.

**Both halves were taken, and the first is the one that closes the lockout.**
`is_sso_only` now requires a third thing: the IdP block of the protocol
`settings.sso` selects must resolve. It gets that from `check_sso_idp_config`,
which runs the same code `resolve_sso_config` and `resolve_saml_config` run. It
is not a copy of their checks. Login's refusal, the step-up's password drop,
`/auth/me`'s `password_sign_in_closed` (all through
`api/auth._org_closes_password_sign_in`) and the config echo all call
`is_sso_only`. So the page hides the password form exactly where the server
refuses the password, and wherever it does, it has the SSO button to show. The
write-time refusal is the second half: a `PATCH` that writes `sso` with
`enabled` and `sso_only` over a block that does not resolve is a `422`. On its
own it would not have been enough. A direct DB edit, or a row saved before it
existed, bypasses it, and the predicate is what covers those.

Five calls inside it:

1. **Unresolvable means open, not closed.** The rejected alternative kept
   `is_sso_only` as it was and made the echo report `sso_only: true` for a
   broken block. That hides the password form and still shows no SSO button,
   which is the same lockout with less explanation. Is failing open a security
   concern? Only an admin writes `settings.sso`, and SCIM and the SCIM-token
   mint only add keys to the block. The actor who can break the block could as
   easily have switched `sso_only` off, so failing open gives nobody a door they
   did not already hold. Each sign-in the escape hatch lets through logs a
   warning naming the org (never the block, which holds the client secret). A
   tenant that believes it enforces SSO and does not is an operator's problem.

2. **"Resolves" is local, never live.** The check is presence, type, URL shape
   and base64 decoding: microseconds, no DNS, no discovery fetch. It runs on
   every password sign-in and every `/auth/me`, so a network call there would
   put an IdP's latency, and its outages, on our login path. The cost is a
   residual this does not cover. A complete block pointing at an IdP that is
   down, or holding a client secret that has expired, still closes the password,
   and recovering from that takes a platform operator. That is the ordinary
   SSO-only trade-off, and it is filed as a follow-up rather than solved by
   probing the IdP from the sign-in path.

3. **The resolvers raise `SSOConfigError` and nothing else.** Before, a
   malformed block (a numeric `client_id`, an unclosed IPv6 bracket in the
   discovery URL, a string where the allowlist or the cert list should be)
   raised a `TypeError` / `AttributeError` on the public config endpoint. That
   was a 500 on a page nobody reads. Now the same code runs inside login, where
   a 500 would be the lockout again. So every value they read is type-checked
   into an `SSOConfigError`, and a test walks the malformed shapes. A malformed
   allowlist is refused rather than ignored, because ignoring it would admit
   every domain.

4. **SAML's validation no longer needs a slug.** `resolve_saml_config` takes the
   tenant slug only to derive the default SP EntityID, which cannot fail. The
   refusable part moved into `_validated_saml_block`, so `is_sso_only` keeps its
   settings-only signature and never needs a slug to answer. The SAML
   `idp_sso_url` is now shape-checked at resolve time, as the OIDC
   `discovery_url` already was. A block that passes can therefore never give
   the login page a SAML button that `saml_login` would refuse.

5. **The write-time refusal names keys and nothing else, and only guards a
   write of `sso`.** `SSOConfigError` carries the offending key names in
   `fields`. The `422` is a plain string `detail` like the endpoint's other
   validation errors, and it lists `sso.<key>` for each one. It never echoes a
   value, because one of them is the client secret. `sso_only` with SSO switched
   off is accepted, since an admin may stage the flag before the IdP is ready,
   and that closes nothing. An incomplete block without `sso_only` is accepted
   too, since it only means there is no SSO button yet. A stored block that is
   already unresolvable does not block a `PATCH` of some other setting. It is
   harmless now that the password stays open over it, and refusing unrelated
   saves would hold the whole settings page hostage to one block.

§201 chose `/auth/me` over the config echo because the two disagreed. They now
agree by construction. `/me` stays the profile page's source because the page
is authenticated and already reads it, and because reading the rule rather than
copying it is what made this a one-site change. A tenant already in the broken
state moves from "nobody can sign in" to "the password works, and the log says
why". The diagnosis's trigger assumed no production tenant had turned
`sso_only` on yet, and nothing here depends on that.

## 205. A page's section navigation follows why its sections share the page

`/organization` had sixteen `<h2>` sections in 3,341 lines, and the only way to
find one was to scroll and read. It had a partial answer already — a
"Getting started" card linking five destinations — which is how the gap stayed
invisible: five sections were addressable, ten were not, and one anchor
(`#org-email-intake`) existed with nothing pointing at it.

A survey of the other sixty-odd routes found that this page is not a pattern.
Nothing else in the app stacks that many unrelated concerns:

```
 lines  h2   page
  3341  16   /organization      settings stack, mixed concerns
  1552  21   /legal/dpa         document
  1054  20   /legal/privacy     document
   871  20   /legal/terms       document
   868  10   /legal/sub-processors
  1176   7   /profile           settings stack, coherent
  3923   3   /payments          list page, merely long
  2107   1   /expenses          list page, merely long
```

So the question is not "how long is this page" — `/payments` is the longest file
in the tree and has no wayfinding problem at all, three sections and a table.
The question is **why its sections share a page**, and there are three answers:

- **They are alternatives** — you arrived to change exactly one. You need a
  menu, not a contents page: one panel at a time, addressed by `?section=`.
- **They are one continuous text** — you arrived to read or cite it. Everything
  stays in the DOM with a table of contents over it.
- **They are stages of one workflow** — leave them stacked; the scroll is the
  narrative.

The threshold is roughly eight unrelated sections for panels, any length at all
for a document, and neither below about seven coherent ones.

### Why panels for the settings pages

`ui/SettingsRail.svelte` is a third navigation primitive, and the three do not
overlap: `layout/SectionTabs.svelte` moves between routes in a nav group,
`ui/Tabs.svelte` is a horizontal `role="tablist"` over local view state, and
this moves between sections of one page with the section in the URL. It renders
anchors with `aria-current`, not `role="tab"` buttons — the destination is an
address, so it is a link, and a link owes no roving-tabindex contract.

Three alternatives were rejected. A **complete anchor TOC** was the cheap option
and buys only scanability: the seven requests still fire on arrival, the file is
still one file, and a scroll offset is still the only sense of place. **Sixteen
new routes** under the Settings nav group was worse than it sounds — that group
already has eight children and `SectionTabs` already overflows them into a
"More" menu. **Accordions** hide their labels behind chevrons and have no
shareable address.

The rail's real payment is not navigational. Seven reads fired on every arrival
at `/organization`; six of them belong to exactly one panel each and now wait
for it, so arriving costs two requests instead of seven, and an admin who came
to change the ACH cut-off no longer pays for a DNS lookup, a residency read and
a chat-webhook probe on the way past. `/profile` went from three to zero — and
that, not its seven sections, is why it got a rail despite sitting below the
threshold. Its sections are coherent enough to scroll; its eager reads were not
defensible.

Two properties are load-bearing and each has a test:

**Field state stays at page level.** Only the markup is conditional, so an
unsaved edit in one panel is still there — with its Save button — after a trip
to another and back. That is what makes a confirm-on-leave dialog unnecessary,
and it is the constraint a later refactor into per-panel components would
quietly break, so `tests-e2e/organization/section-nav.spec.ts` types into a
field, leaves, returns, and asserts the value survived.

**A slug is a URL contract.** It lands in bookmarks, in the Getting-started
links, and in docs. The five `#org-*` anchors the page was previously navigated
by still resolve, and they resolve **by derivation** — `section` reads the query
first and falls back to the anchor map — rather than by rewriting the URL to
`?section=` in an effect.

Rewriting was the first version. **It was observed to work** — a probe
capturing `pageerror` and `console` saw the rewrite land cleanly, before the
lead-in panel ever painted — so this is a simplicity and robustness call, not a
bug fix, and the record should not pretend otherwise.

What made it the wrong shape anyway is the dependency: `replaceState` is
documented to throw when called before the SvelteKit router has initialised, so
the rewrite put a router-timing precondition on the one path whose entire job is
to still work for a link someone saved a year ago. Nothing established that a
first-hydration effect actually lands inside that window — the evidence points
the other way — but the derivation removes the question, along with an import
and an effect. It cannot fail, and the stale `#hash` clears itself on the first
rail click, since the rail's hrefs are query-only. Query beats hash, so an
explicit `?section=` always wins over an anchor merely along for the ride.

### Why not panels for the legal documents

The six published documents carry ninety-one sections between them, every
heading already `id`-anchored, and — until this change — not one table of
contents; the `href="#…"` links in Terms and the DPA are inline
cross-references ("see section 10"), not navigation. They are the page type
where a contents page is the expected convention rather than a nicety: counsel
reviews a DPA clause by clause, a customer cites "DPA §7.2", a regulator skims
for Article 28 flow-down.

They get the opposite treatment for that reason. Everything stays rendered:
panels would break Ctrl-F across the text, printing, and citation by section,
which is most of what these documents are for. The TOC is derived from the
rendered `h2[id]` set inside `lib/legal/LegalPage.svelte` rather than passed
per page, because the alternative was ninety-one duplicated entries that would
drift from the headings inside a release — and because that wrapper is the one
file all six already render through (§174 for why their text is English-only,
#433's layout for why the surround is shared).

## 206. The legal header names the door the reader already has, and reads the one signal that costs no wait

`routes/legal/+layout.svelte` shipped with a hardcoded `Sign in` → `/login`
pill in its top bar (#434). That is correct for exactly one of the three ways a
person reaches these documents, and wrong for the two that happen most.

An employee gets here from `Sidebar.svelte`'s profile menu ("Legal & privacy"),
which means the app offered a signed-in user a sign-in form they had already
filled in. `/login` does not bounce an authenticated visitor onward — the root
layout skips its redirect for any `/login` path — so following the only
affordance in the header was a round trip back to where they started. Worse,
`portal/+layout.svelte` links the same set from the supplier-portal footer, so
a vendor exercising a data right over their own bank details and tax ID was
pointed at the **employee** login: not a dead end but the wrong door, which no
password they hold will ever open.

So the pill is derived — `Back to app` → `/`, `Back to portal` → `/portal`, or
`Sign in` → `/login` — from `auth.loggedIn` and `portalAuth.loggedIn`, employee
first. A browser can hold both tokens (the two surfaces keep separate
localStorage keys on purpose), and when it does the AP app is the likelier
origin and the portal is one click from `/`.

**The static requirement in #434 was about waiting, not about store reads**, and
this keeps the part that matters. Both booleans initialize from a synchronous
`localStorage` read at module load (`$state(hasToken())` /
`$state(hasPortalToken())`), settled before first render. What the standalone
branch in `routes/+layout.svelte` exists to avoid is `hasTenant` — an `$effect`,
hence a tri-state that would blank a published document for a beat and, on the
apex, serve the marketing Landing instead. Nothing here touches it, so the
header still draws on the apex exactly as before, where neither token is likely
to exist and `Sign in` is the honest answer anyway.

It reads token PRESENCE, not proof of a live session, which is the same signal
the rest of the app routes on. An expired token sends the reader to `/`, which
bounces to `/login` on the first 401 — the right destination by a longer road.
Verifying it here would mean a fetch, and a legal document that waits on the
network is precisely what this layout refuses to be.

The reflow cost is real and is now covered: `Back to app` is a wider pill than
`Sign in`, and both legal a11y specs measure 320px anonymously, so the widest
state of this header had no WCAG 1.4.10 coverage at all. The signed-in e2e case
asserts it.

## 207. A bulk re-code decides "the chart is empty" per invoice entity, by the same rule as a manual write

`gl_recode._ActiveChart` validated a vendor-prior GL code against the invoice's
own effective chart (shared ∪ its entity) from the start, but its escape hatch —
"no active accounts, so accept any candidate" — asked the question org-wide. In
a tenant where subsidiary A has no chart of its own and no shared accounts while
B has a chart, every candidate for an A invoice was validated against a chart A
does not have and rejected as `skipped_invalid_code`, although `gl_chart`
(§194, §199) and the extraction catalog both accept any code for A. Nothing
wrong was written; the feature was just unusable for that subsidiary.

`_ActiveChart.is_empty_for(entity_id)` now resolves emptiness over shared ∪ that
entity's own accounts, through `gl_chart.chart_is_empty` — the predicate
`chart_has_active_accounts` states for a single write. The bulk pass keeps its
one org-wide load and answers per invoice from it, so the fix costs no query per
invoice. Tests: `tests/test_entity_coa.py`, both directions of the mixed case.

## 208. A backend test that hangs dies with every thread's stack, at a ceiling no real test approaches

Before this, the only backstop for a wedged test was `timeout-minutes: 40` on the
CI shard, which reaps the runner and leaves `Terminate orphan process` and no
traceback — the reason one slow-runner episode (#444) took two days to rule out
as a hang. `pytest-timeout` now sets `timeout = 600` per test, with
`faulthandler_timeout = 540` so pytest's own stack dump finishes first.

The ceiling is derived, not guessed: the slowest test in `.test_durations` is
14.65s, recorded figures understate CI by ~2.2x, so ~32s is a realistic CI worst
case and 600s is ~18x above it. It is a diagnostic, not a performance gate, and
a ceiling tight enough to fire on a slow-but-honest test would turn into exactly
the retry-and-inflate masking guard rail 4 forbids — so if it ever fires on a
legitimate test, the answer is to find why that test is slow, not to raise it.

`timeout_method = "thread"`, not `signal`: SIGALRM only lands at a bytecode
boundary and cannot reach a C-level wait — a blocked asyncpg read, a wedged
socket — which is what a real hang in this async suite looks like. The thread
method dumps and hard-kills the whole process rather than failing one test and
continuing; the shard was going to die anyway, and now it dies with a stack.

## 209. The password card asks whether the account has a password, and keeps rotation open where the password is not used to sign in

`/profile` rendered "Current password / New password" for every member, including
every account JIT-provisioned by OIDC/SAML or created by SCIM, which has no
`hashed_password` at all. `PATCH /api/auth/me` refused them with the same
sentence a wrong password gets, so the card could only ever produce a 400.
`password_sign_in_closed` (§201) could not answer this: it describes the org's
SSO setting, not whether this account was ever issued a credential. So
`/auth/me` carries `has_password` too. It describes only the caller's own
account, so publishing it enumerates nothing; the schema default is `true`, so
an older response keeps the old form rather than hiding it.

Where it is false, the form is replaced by a sentence — the account signs in
with single sign-on and has nothing to change — rather than a disabled control.
Where it is true but the org has closed password sign-in, the product call was
to **keep rotation available** with a note that the password is not currently
used to sign in: it becomes load-bearing again the day the org leaves SSO-only,
and hiding the card would make that day harder for nothing. Mobile deserializes
none of these flags and ships no change-password screen (§201), so it is
unchanged. Tests: `test_sso_only.py`, `test_api_contracts.py`,
`tests-e2e/auth/profile-no-password.spec.ts`.

## 210. A public SSO entry point reads an IdP block that will not resolve exactly as one that is absent

`sso_authorize`, `sso_callback`, `saml_login`, the ACS and `saml_metadata`
handled a `None` from `resolve_sso_config` / `resolve_saml_config` and nothing
else, so an `{enabled: true}` block missing a required key raised
`SSOConfigError` into a 500 and a logged traceback. Each module now has one
`_resolve_*_or_none` helper that turns `SSOConfigError` into `None`, which is the
posture the two `/config` endpoints and `is_sso_only` already took: the same
status (400, or 404 for metadata), the same generic sentence, and never the
offending keys — naming a tenant's missing IdP fields to an anonymous caller is
exactly what the generic answer exists to avoid.

No warning is logged. The route is only reachable by hand — §204 hides the SSO
button for a block that does not resolve — and the admin who can fix the block
already gets the precise refusal from `/config` and at save (§204). A
per-request log on a public route would be noise an unauthenticated caller can
generate at will. Tests: `test_sso_security.py`, `test_saml_security.py`.

## 211. `GET /api/organization` serves the reporting currency it resolved, and the clients' three-rung resolution becomes the fallback

`currency_conversion.resolve_reporting_currency` has four rungs, and the last is
operator config (`FEOH_REPORTING_CURRENCY_DEFAULT`) no client can read, so the
web `orgCurrency` store and mobile's `OrgCurrencyStore` answered `null` for an
org configured only that way (§119, §160, §200) — honest, but the server had
already denominated every rollup and every approval threshold in a known code.
The response now carries `resolved_reporting_currency` as a top-level field,
computed from the raw settings so a non-admin gets the same answer an admin
does, and both stores read it first. Their three-rung resolution stays, as the
fallback for a response that predates the field.

**Rejected:** adding the operator default to the role-projected `settings`
block. That would publish the operator knob to every role as if it were tenant
configuration; a top-level *result* carries the answer without the input that
produced it. Tests: `test_org_settings_view.py`,
`reportingCurrency.test.ts`, `organization_test.dart`,
`org_currency_store_test.dart`.

## 212. MinIO comes from a community build, `pgsty/minio`, because MinIO's own registries no longer serve anonymous pulls

`quay.io/minio/minio` — adopted when Docker Hub's `minio/minio` stopped serving
anonymous pulls — started answering `401` to anonymous manifest and tag-list
requests too (observed 2026-09-28), failing every CI backend and e2e shard at
the pull step. Upstream ships the community edition as source only, so any
prebuilt image is now a third party's build. The compose file pins
`pgsty/minio:RELEASE.2026-08-04T00-00-00Z@sha256:…`: a rebuild of upstream
source with versioned tags, the same CLI and env vars, and Docker Hub hosting
Dependabot already watches. The server moves off the frozen 2025-09-07 release
because nothing anonymously pullable still serves that digest.

**Rejected:** `cgr.dev/chainguard/minio` — signed and source-built, but the free
tier serves only `latest`, so neither a versioned pin nor a Dependabot bump is
possible and old-digest availability is outside our control. Building MinIO in
CI from source — slower on every shard, for a local stand-in. Swapping to a
different S3 server now — a larger behaviour change than a drop-in rebuild;
it is the next step if this image goes the same way. The trust level is
acceptable because MinIO only ever holds throwaway `minioadmin` data locally and
in CI; production is real S3. Detail: `backend/docs/docker.md` § Where MinIO
comes from.

## 214. An invoice edit may not break a credit memo already applied to it

Applying a credit checks the invoice's vendor, currency and remaining balance
once, and an applied memo can never be voided or re-applied. But the invoice
stays editable until approval, and `PATCH /api/invoices/{id}` and
approve-with-corrections could re-save the vendor (re-linking `vendor_id`), the
currency, or the amount afterwards. `payment_runs.net_payable_amount` then netted
the memo off whatever the invoice had become: vendor A's credit reduced vendor
B's payment, a USD credit came off a EUR payable digit for digit, and an amount
lowered below the credits consumed the excess against nothing (the net went
negative, the payment was refused as fully credited, and the stranded credit was
unrecoverable). Both edit paths now call
`services/applied_credit_integrity.refuse_edit_stranding_applied_credits` after
writing the change and any vendor re-link, and 409 inside the same transaction.
The rule is the apply guards read backwards — NULL vendor fails closed, currency
compared case-insensitively, a blank invoice currency admitted only on a legacy
row that was already blank, never when the edit is what blanked it — and an
amount down to exactly the applied total stays legal (a fully credited invoice).

**Rejected:** re-netting silently (adjusting or detaching the memo when the
invoice changes). An applied memo is an immutable money record, and moving it
without a human is the defect, not the cure. Unapplying on edit was rejected for
the same reason.

**Re-checked at the point of payment, too.** Re-extraction (a manual re-extract,
or the supplier portal's resubmit of a rejected invoice) writes the same fields
in the background, with no request to refuse. So `applied_credit_conflicts`
checks the vendor and currency pairing wherever money is booked or dispatched.
The run builder and the payment queue refuse such an invoice as the new
`applied_credit_mismatch` refusal, ordered before `fully_credited` because that
net is computed from the same credit. `POST /api/payments` returns 409,
`/retry-failed` skips it, and dispatch fails the payment as
`applied_credit_mismatch:<kind>` before the processor call, retry-safe. Refusing
at payment, not inside extraction, keeps one gate for every writer, present or
future. Opening an exception from extraction was rejected as a second spelling
of the same check. The amount needs no payment-time code, because credits above
the amount already make the net zero or negative (`fully_credited`).

Tests: `test_credit_memos.py` § editing an invoice that already carries an
applied credit; `test_payment_run_credit_memo_netting.py` and
`test_payment_queue_blocked.py` for the payment-time check.

## 218. The dashboard's processing-time tile is reduced from grouped day counts, and fails loudly instead of reading zero

`GET /api/dashboard` built the processing-time tile by streaming every
`invoice.approved` audit row, every completed payment, and then every matching
invoice into Python — the last as `Invoice.id IN (...)` with one bind
parameter per invoice. asyncpg refuses more than 32 767, and a bare
`except Exception` around the block turned that refusal into zeros, so every
tenant past roughly 33k approved invoices saw "0 days" with nothing logged.
Each leg is now one grouped query returning `(days rounded to 0.1, count)` —
rows bounded by the span of the history, not its size — reduced by
`analytics.processing_time_from_day_counts`, which an equivalence test pins
to `compute_processing_time_metrics`. At 30k approvals the request went from
544 ms to 170 ms; at 40k it went from a silent zero to the right answer.

Three choices ride along. **The approval leg runs to an invoice's FIRST
approval**: the old dict kept whichever audit row Postgres returned last, so a
re-approved invoice's figure depended on heap order; first approval mirrors the
paid leg's `MIN(completed_at)`. **`_decimal_days` became exact
half-away-from-zero** (integer microseconds, `ROUND_HALF_UP`): `round()` on a
float settled `.x5` ties by which side of them the nearest double fell, which
SQL cannot reproduce and no one chose — it differs from the old value only at
exact ties, in every caller. **The swallowing `try` is gone — from this block
and from the three others on the page** (open exceptions, approval bottleneck,
discount capture): it is what hid the defect, and the case it claimed to guard
(a tenant without the table) cannot occur, since every table they read is part
of the tenant schema every provisioning path builds. A failure is now a 500,
which is a bug report rather than a plausible-looking wrong KPI.

**Rejected:** `percentile_cont` in SQL — it returns `double precision`, so the
p95 would drift from the Decimal reference at quantize boundaries; the
(value, count) form keeps every figure exact. Batching the `IN` list under
32 767 — fixes the failure, keeps the O(invoices) transfer. Tests:
`test_dashboard_processing_time.py` (reference figures, entity scope, and a
request whose statement and bind-parameter counts do not grow with the data),
`test_analytics.py`.

The same perf pass (no separate entry needed, recorded here for the trail):
`payments.payment_run_id` gained the index every per-run read lacked
(migration 0101), and the invoice list stopped loading each extraction's
`raw_result` — the provider's entire response — to read two small columns
(200-350 ms → 60-70 ms per 100-row page at ~50 KB per Textract payload).
Guards: `test_list_endpoint_query_counts.py`, which also holds the payments,
queue, runs, exceptions and credit-memo lists to a statement count that does
not grow with the page.

## 222. Every by-id route on an entity-scoped table resolves within the selected entity, and an out-of-scope id is a missing one

Phase 2 scoped the lists; the by-id routes kept resolving on the primary key, so
the entity selector stopped at the list page. Closing it router by router left a
long tail (procurement, invoices, vendors, contracts, expenses, workflows,
experiments, cards, inspections, suggestions, exception-agent resolve), so the
rule is now stated once: with an entity selected, a row outside it is a 404
byte-identical to an unknown id's, and the consolidated view reaches everything.
Where a router owns a loader, the loader takes `entity_id`; where the row is
loaded inside a service or a helper non-request code shares, the handler calls
`app.tenant.ensure_in_entity_scope` first. Visibility by id matches the list's:
strict for most tables, ∪ NULL for vendors (unstamped, `vendor_matching`'s
reason) and workflow definitions (org-wide).

**Rejected:** a 403 for an out-of-scope row — it confirms the id exists in a
sibling subsidiary, the enumeration the opaque 404 exists to prevent. Threading
`entity_id` into `get_invoice_for_update` and every `review.*` service — the
services are also called by sweeps, the agent coordinator and webhooks that have
no selected entity, and a defaulted parameter is one forgotten argument away
from silently unscoped; one guard at the request boundary is where the header
lives. A FastAPI dependency per model — it would run in signature order, so a
role-refused caller could learn the row's scope before `require_roles` refused
them. Saved cash plans stay unscoped (their deterministic `plan_id` already encodes the scope they were built under), and
the S3 file-key proxies stay org-prefix-gated pending the follow-up that maps a
key back to its row. Detail: `docs/multi-entity.md` § By-id routes resolve
within the selected entity.

## 226. A file-download proxy resolves the row that owns the key, and Positive Pay reads its run in the read scope

The four S3 proxies (invoice document, supplier-chat attachment, contract
document, expense receipt) took the key as the whole authorisation: its first
segment had to be the caller's org, and the tenant DB was never opened. §222
closed every by-id route over the owning rows, so with subsidiary B selected a
caller could no longer open A's invoice — but could still download its PDF by
key. Every key layout already embeds its owner (`<org>/<invoice_id>/…`,
`<org>/chat/<invoice_id>/…`, `<org>/contracts/<id>/…`, `<org>/expenses/<id>/…`),
so `api/file_proxy` parses that id, loads the owner with `apply_entity_scope`,
and — where the row records exactly one file — requires the key to BE the row's
current `file_key` / `receipt_file_key`. A malformed key, a foreign org, the
wrong layout for the route, an out-of-scope owner, a missing owner, a superseded
object and a missing object are all the same `404 "File not found"`. Owner
resolution runs in the consolidated view too (not just under a selected
entity), so an object whose row was deleted, erased or retention-expired stops
being servable even if the bucket still holds it, and an `<org>/positive-pay/…`
key no longer streams through the invoice proxy on the strength of its org
segment.

Two defects on the same objects went with it. The AP chat attachment URL every
message stored, `/api/invoices/{id}/chat/file/{key}`, was not a route at all,
and each surface echoed the URL the *posting* surface built, so an employee was
handed the vendor-only portal route and vice versa. The invoice-bound AP route
now exists (scoped, and the key must sit under that invoice, as the portal's
does), and `supplier_chat.attachment_url` rebuilds the URL from `file_key` for
the surface reading it. And email intake and PEPPOL receive stored the object's
bucket address as `Invoice.file_url`; every writer now uses
`storage.invoice_file_url`, and `InvoiceResponse` rebuilds the URL from
`file_key` so rows written before this still point at the proxy.

Positive Pay's check-issue generation and return processing resolved the run /
file with `get_write_entity_id`, which maps "All entities" to the default
entity — so the consolidated view 404'd on a subsidiary's run, the reverse of
every other by-id route. They now resolve in the read scope (`get_entity_id`),
and what they write follows the row, not the view: the check-issue file is
stamped with the RUN's entity and each return fraud_flag with the FILE's, so
generating from the consolidated view can't misfile a subsidiary's cheque file
under the default entity. A run of a since-deactivated subsidiary can still have
its file generated — the cheques were issued, and the fraud control has to work
for them; deactivation stops new business, which a derived file is not.

**Rejected:** presigned S3 URLs in place of the proxy — the object would then be
fetchable by anyone holding the URL for its lifetime, entity selector or not,
and revocation (a role change, an erasure) would lag by the expiry. Checking
only the owner segment without matching the current key — leaves superseded
objects servable. Removing the key-only chat route — the API is published; it is
scoped the same way instead.

## 228. The report builder splits a money aggregate by currency rather than sum across them, and buckets dates in the zone its filters use

The ad-hoc report builder (`services/report_builder.py`) summed `amount` across
every row in a group whatever its currency. A "spend by vendor" report over a
vendor billing USD 100 and EUR 50 returned one row reading `150.00` — and with no
dimension at all, the whole book collapsed into one such figure. The web
`ResultTable` rendered it bare (§196 kept it from wearing a wrong symbol), but a
bare number that is a sum of two currencies is still a wrong number; §160 and
§200 both say a mixed-currency total must not be presented as one figure.

**Split, not convert.** Every source with a money measure names its currency
dimension, and `compile_spec` appends it to the group-by whenever a money
aggregate is selected and the user did not group by it. Each money column names
that dimension in `currency_key`, and the client labels each cell from its own
row. Converting into the reporting currency was the other durable option and was
rejected for this surface: `invoice_reporting_amount_sql` falls back to the face
amount for an invoice with no usable lock, which reproduces the mixture under a
reporting-currency label; `payment_reporting_amount_sql` has no fallback and
would silently drop rows; and an ad-hoc report has no disclosure slot for an
`unconverted_count`. Splitting needs no rate, cannot be wrong, and is the
same answer a user gets today by adding the currency dimension themselves. A
reporting-currency *measure* (`reporting_amount`) can be added later as its own
catalog entry with its own disclosure; it is not a replacement for this.

`payments` had no currency dimension at all, because `Payment` has no currency
column — its `amount` is in its invoice's currency. The catalog gained a
`currency` dimension over `Invoice.currency` with a server-defined outer join, so
the join, like every column, stays inside the whitelist.

`count` is not split (a count has no currency), and a spec that already groups by
currency is unchanged. `test_every_money_measure_has_a_currency_dimension` fails
when a money measure lands on a source without one.

**Date buckets.** The date filters pinned their day bounds to UTC
(`_day_start`), but the bucket was `date_trunc(grain, col::timestamp)`, and a
`TIMESTAMPTZ → TIMESTAMP` cast converts through the session `TimeZone`. On a
non-UTC session the two disagreed about the same row: 2026-01-31 20:00 UTC passed
a January filter and landed in a February bucket. Timestamp columns are now
bucketed via `timezone('UTC', col)`, so filter and bucket share one explicit zone
and neither depends on the connection's settings. `DATE` columns are unchanged.

## 229. SvelteKit 3: `#lib` with real extensions, and every env var declared `static`

SvelteKit 3 (#492) removed `$lib`, `svelte.config.js`, `$app/stores`,
`$app/environment` and the implicit `$env/*` modules. Kit's own error offers an
escape hatch — `alias: { '$lib': 'src/lib' }` keeps the old specifier — and it
was rejected: it defers the migration rather than doing it, and leaves the tree
on a shape upstream has stopped documenting.

**`#lib/...` names the file.** `#lib` is a package.json `imports` entry, which
Node, TypeScript, Vite and Playwright all resolve natively — so no tool needs an
alias any more (vitest's hand-mapped one is gone). Subpath imports do no
extension probing, so every specifier carries the real file name:
`#lib/api.ts`, `#lib/i18n/store.svelte.ts`. Upstream's examples write `.js` and
rely on each resolver mapping it to `.ts`; the real extension was chosen instead
because it is what is on disk, and needs no mapping in any of the four.

**Every variable in `src/env.ts` is `static: true`.** A non-static public
variable is read "when the app starts", which presumes a server; this build is
files in S3. Inlining at build time is what the old `$env/dynamic/public`
already did under adapter-static, so behaviour is unchanged.
`PUBLIC_API_URL` stays required (a build without it fails, as before);
`PUBLIC_PLATFORM_DOMAINS` and `PUBLIC_SITE_URL` stay optional, each for the
reason its entry gives.

## 230. The consent banner publishes its footprint, and the legal contents rail reads it only while it is there

The legal contents rail (§205) is pinned to the viewport with a viewport-relative
height, so unlike the document it cannot scroll out from under the fixed consent
banner, and tabbing down it could land focus on a link the banner hid (WCAG 2.4.11).
The banner already reserved its own space for the document (spacer plus
`scroll-padding-bottom`). For viewport-pinned content it now also sets
`data-consent-visible` and `--consent-banner-height` (the whole band: height plus
both 16px offsets) on the root element while mounted, from the same resize-tracked
measurement, and removes them on dismissal; the rail subtracts the band only under
that attribute. A fixed bottom gap was rejected because it takes space from every
reader who has already chosen; having the rail measure the banner was rejected
because a page would then reach into the consent component's DOM. The banner owns
its geometry and announces it; consumers only read it. The consent gate itself is
untouched.

Each legal table scroller is a `role="region"` named with `aria-labelledby` on the
heading its table sits under, not a new `aria-label`. That reuses text already in
the reviewed English legal document (§174), so naming the regions adds no legal
wording and no catalogue entries. `tableScrollRegion.test.ts` now covers the legal
tree and fails a dangling `aria-labelledby`.

## 231. Step-up and passkey refusals carry a code, never more specific than the sentence it replaces

A refused factor-change step-up, or a passkey check with no usable credential,
answers 400 with `detail = {code, message, params}`, built by
`api/refusals.coded_refusal` (shared by the employee and supplier-portal routers).
The status stays 400 and the English stays as `message`: every client already
flattens an object `detail` to its `message`, so text readers do not break and a
build older than a code still shows a complete sentence. Params are data — the
wrong-host refusal sends its hosts as a list so the browser joins them with the
reader's punctuation.

The auth rule that keeps this safe: a code is never more specific than the sentence
it replaces. `step_up_failed` covers a wrong password, a wrong authenticator code
and a failed passkey alike, and `passkey_not_registered` is as uninformative as the
old "No passkey registered". The portal gets its own `portal_step_up_failed`
rather than reusing the employee code, because a code identifies one sentence and
the portal's offers no passkey. The web mapping (`api/authRefusals.ts`) is
hand-written rather than generated: it has five entries that change only with a
deliberate auth decision, and a unit test pins each code to the backend's spelling.
Fixing the portal surfaced that `portalApi.ts` threw `new Error(body.detail || …)`,
rendering every portal 422 list as "[object Object]"; it now builds errors through
the same `apiErrorFromBody` as `api.ts`.

## 232. A structured server refusal is localized once, at the transport

The GL-chart refusal (§194, §199) became a 422 `detail` object,
`{code: "gl_codes_outside_chart", on_lines, foreign, retired, unknown, message}`,
rather than a sentence. Codes are grouped by reason so a client builds its own
sentence, `on_lines` replaces a free-text "Line items:" prefix, and `message`
keeps the English for any client or catcher that only reads text. A refused CSV
row carries the same fields beside `row`, so one parser serves both.

On the web it is translated inside `api.ts::apiErrorFromBody`, before
`formatApiDetail`, not at each toast: at least six write paths can receive it, and
fixing call sites would miss the next one while the transport sees every response.
A second structured refusal joins `localizeApiDetail`, never a call site.
Server-side catchers that turned the refusal back into text (the exception agent's
escalation rationale, the bulk-status skip reason, the email-approval page) read it
through `utils/http.detail_text`, because `str(exc.detail)` on an object prints a
dict repr. The recurring-template form's GL field became the same entity-scoped
picker the invoice forms use, which needed `entity_id` on
`RecurringTemplateResponse`; it falls back to free text when the chart is empty.

## 233. A money path's wait for the invoice lock is bounded, scoped to one statement, and refused by name

Dispatch, void, settlement accept and compliance release lock the invoice row
(§214), and the wait was unbounded, so every invoice writer could queue behind a
slow processor. Each caller takes that lock before any processor call, so a
timeout there is always retry-safe — but only if it can never fire later. The bound
(`utils/db_locks`, `FEOH_PAYMENT_INVOICE_LOCK_TIMEOUT_MS`, default 5000) therefore
runs inside a SAVEPOINT and restores the previous `lock_timeout` before release: a
setting left in place would survive to the end of the transaction, onto the writes
after an accepted processor order, where a timeout would roll back the only record
that money moved. The savepoint also keeps the outer transaction usable, so
dispatch records `failed` / `invoice_locked` (retry-safe) on a live session instead
of `unexpected_error` on an aborted one.

The request paths answer 409 rather than recording `failed`: a human is there to
retry, and failing a held payment would push it through the retry-run flow for
nothing. Two waits are deliberately not bounded: the dispatch loop's payment-row
lock (only another dispatcher holds it, so waiting then skipping is correct), and
other invoice writers waiting out a dispatch's processor call (bounded by the
adapters' HTTP timeouts). The remaining abort-after-processor case is tracked in
`docs/followups.md`.

## 234. A money field moves from JSON number to exact string clients-first; the exception queue's `amount` is the first

Round 26 made every money serializer exact but kept the wire shape. The exception
queue's `amount` now crosses as an exact decimal string via a shared
`schemas/money.json_money_string` (no scientific notation, scale kept), so later
fields move by changing one call. Both clients accept either shape first — web
types it `MoneyAmount`, mobile reads it with `exactMoneyFromJson` and formats a
legacy number to the column's two decimals — so deploy order does not matter. A
mobile build from before the change would throw on the string; none has been
distributed (no release has been published, so `mobile-release.yml` has never
run), but once one is, a server switch of the next field must wait for the
tolerant build to be the oldest in use. The `exception.raised` webhook already
sent a string, and `/api/v1` exposes no exceptions, so no published contract
changed.

## 235. Requisition approval refuses material editors, keyed on a set (migration 0102)

`PATCH /api/requisitions/{id}` lets any admin, ap_manager or ap_clerk rewrite
another user's draft — lines, vendor, currency, budget — including one an
ap_manager just converted from an intake, whose requester is the intake's. The
approve check named only `requester_user_id`, so the editor could approve the spend
they had written: the gap §141 / §152 closed for recurring templates, closed the
same way. `purchase_requisitions.material_editor_ids` (nullable JSONB, tenant
fan-out) is filled only by a material change, never holds the requester, and is
passed to the approve check as `segregation_actor_ids`. It is not backfilled, for
§141's reason: nothing records who edited a draft, and any guess either refuses an
innocent approver or absolves the editor.

"Material" is defined once in `models/procurement.py`: lines, vendor, currency,
budget. `department` and `contract_id` are cosmetic because nothing sums or copies
them into spend. Lines are compared by content, not presence, because the edit
modal re-sends every line on every save; counting presence would refuse anyone who
fixed a typo and discredit the control. An intake conversion records nobody on its
own — it copies approved terms and accepts only cosmetic overrides — and a guard
test fails if the conversion body ever gains a material field.

Every route that writes a requisition (PATCH, DELETE, submit, approve, reject,
cancel, reopen, convert-to-PO) locks the row with `SELECT … FOR UPDATE`, because
the editor set is read-modify-write and approve reads what PATCH writes: without
the lock two concurrent material edits silently dropped one editor, and a PATCH
could commit after a concurrent submit + approve. Lock order is requisition row
first, then budget row (§236); no path takes them the other way round.

## 236. Budget guards and requisition links serialise on the budget row

`update_budget` / `delete_budget` counted linked requisitions and then wrote with no
lock in between, so a requisition linking in that window got past the guard: a
delete then failed at the FK with a 500, and a currency change left a requisition
linked in the old currency. Both routes and `api/requisitions._resolve_links`
(whenever it resolves a `budget_id`) now `SELECT … FOR UPDATE` the budget row,
the one row both sides already touch, so the second arrival waits and re-reads.
Serializable isolation was rejected because it turns the race into retry errors
every route would have to handle; locking only the requisition side cannot cover a
link that does not exist yet. The FK stays as the last guard, and a residual
violation on delete maps to the same 409.

## 237. An unmasked DSAR export needs a second factor, never the password

`POST /api/privacy/dsar` with `include_banking` already needed the
`vendor.bank_change.approve` permission and a written justification (§182). But
`ROLE_ADMIN` resolves to the whole permission catalogue, so on the stock roles
that gate admitted exactly the callers the route already admitted, and a stolen
admin session alone could pull a supplier's full account number. The route now
also calls `api/auth.require_sensitive_step_up`, after the permission and
justification checks and before any subject data is read. It accepts a current
authenticator code or a passkey assertion minted for the `dsar_unmasked_export`
operation, and **never the password**: the password is what a stolen session
was obtained with, and refusing it means an SSO-only tenant needs no special
case. An account with no second factor is refused (`sensitive_step_up_no_factor`),
not exempted, because exempting it would make "has no MFA" the cheapest way past
the gate. With the MFA master switch off (local dev, CI) the gate is skipped like
every other MFA challenge, and the audit row records `step_up: mfa_off_local`
so it can never be read as a verified proof.

Shipped with it: the DSAR collections that were cut with a bare `.limit()` now go
through `_capped` with their true totals, since a silent cut hands the subject a
short list claiming to be complete; `/legal/privacy` §12 and the DPA now state the
retain-vs-delete split exactly; and the 1099 `total_reportable` headline and box
panel range only over boxes on forms the vendor must receive, which is what the
filing batch files, while each row keeps its whole `ytd_paid` so the preparer
still sees the unfiled box.

## 238. A refusal a user acts on carries a code; the client states it

The approval-path refusals — segregation, the CFO and max-amount gates, the named
approver, the credit-memo application refusals, the expense CFO gate, the
invoice stale-edit 409 and the required-fields refusal — now return
`detail = {code, message, params}` via `api/refusals.coded_refusal`, and the web
(`api/codedRefusals.ts`) and mobile (`coded_refusal_messages.dart`) clients
state them in the reader's language. Every figure is an exact decimal string
beside its own currency, so the sentence is formatted for the reader, not the
server; a note that does not apply is `null` rather than absent, so a client can
tell "not applicable" from "malformed". A client that cannot state a refusal
completely returns nothing and shows the English `message`, never a
half-localized sentence. An invoice that names no currency sends `currency: null`
and its figures render bare — the §160 / §196 rule — never in the `USD` stand-in
the gate compares with. A malformed or non-finite threshold sends `limit: null`
and the sentence names "the configured limit"; the gate still fails closed.

`InvoiceModal` now branches on `invoice_stale_edit`, not on the English text it
used to substring-match, so rewording the server sentence can no longer break
the reload prompt. A wrong code on employee enroll-verify and on portal MFA
enroll / disable is now a 400 `mfa_code_invalid` instead of a 401, which both
web clients treat as session expiry and answered by signing the user out.
Deliberately not done in this pass (followups): the bulk-status skip reasons,
the payment-run SoD / CFO refusals, and the sign-in MFA challenge.

## 239. `settings.sso` has one audited writer, which writes its row first

`PATCH /api/organization` merged per top-level key with no audit row, so the
`{"sso": {"client_secret": …}}` rotation the runbook once prescribed replaced the
whole block and dropped `enabled`, `sso_only`, the IdP configuration and the
SCIM group state, unrecorded. `GET/PUT /api/organization/sso` (admin) now own the
block and the PATCH refuses the key, the arrangement `chat_notifications` and
custom domains already had. The PUT states the whole configuration
(`enabled` / `sso_only` required, an omission refused rather than read as
false), keeps the stored client secret unless a new one is sent or
`clear_client_secret` is set, and carries the SCIM digest and groups across. The
secret is never returned by any endpoint for any role.

Two properties are what make it the *sanctioned* writer rather than merely a
second one. **The audit row comes first:** a sign-in policy change with no record
is worse than a refused one, so `organization.sso_updated` (key names and
posture, never values) is written before the save, and a row that cannot be
written is a 503 with nothing saved. `record_auth_audit_or_raise` writes it
synchronously to the tenant DB in every audit mode — in `lambda` mode the
ordinary path only enqueues, which would make "first" mean "SQS accepted it".
**Every settings writer takes the org row lock** (`tenant.lock_organization`):
each rewrites the whole JSONB, so a single unlocked writer — a branding save, a
sweep's cursor — silently reverts a concurrent secret rotation. That held for
five writers when the endpoint landed and a review found eleven more, so
`tests/test_settings_writers_lock_the_org_row.py` now fails on any writer
without it. A malformed PUT body is parsed by the endpoint itself and refused
with field locations only, because FastAPI's default 422 echoes a non-object
body whole, client secret included.

`is_sso_only` closes the password whenever the IdP block resolves — a local
completeness check, by design (§204) — so an IdP outage or an expired client
secret locks every member out with the fix behind the sign-in it blocks.
`scripts/sso_break_glass.py --slug` is the operator's way back: it clears
`sso_only` and nothing else, after writing `organization.sso_only_lifted` the
same audit-first way, and exits non-zero having changed nothing if it cannot.

## 240. Exception descriptions and PO-match issues are catalogue findings (migration 0103)

The exception queue and the PO-match panel printed server-composed English
beneath labels that were already localized, so one finding read in two
languages and two wordings in the same dialog. Both now carry the
`invoice_warning_catalog` shape (§157): `exceptions.description_code` /
`description_params` (tenant fan-out, idempotent, nullable) beside the kept
`description`, and `po_match.issues` entries of `{code, params, message}` under
`po_match.issue.*`. A finding that summarizes several lines (price variance,
contract terms) is a frame code with its parts under `params.findings`, rather
than a `"; "`-joined sentence the client could not decompose.

**No backfill.** An old row's English is the only record of what it said, and
re-deriving a code from prose would guess; every reader falls back to the stored
text when the code is absent, and a legacy bare-string issue is still accepted.
`_ensure_exception` raises on a bare string, so a new site cannot regress
silently. The direct `create_exception` callers outside it (extraction failure,
the payment and ERP paths, Positive Pay, the bank-change exception) are not
keyed yet and are tracked in `docs/followups.md`.

## 241. Only a report's owner composes, edits or submits it

Report SoD refused `employee_user_id` and nobody else, while attach,
create-onto-a-report, line edits and submit had no ownership check — so a
manager could write or rewrite lines on a clerk's draft, submit it, and approve
it. Two fixes were open: record every line author on the report (the
`segregation_actor_ids` route the invoice path took in §141 / §152), or make the
owner the only author. The second was chosen because it makes the existing
check complete instead of widening it, needs no migration, and matches what the
docs already claimed ("the owner submits"). Everything else stays open to
reviewers: approve, reject, GL coding (the accountant's classification) and
card reconciliation (evidence, not an edit). The refusal is coded
`expense_report_not_owner`. What it leaves is a line on no report, which has no
owner column at all; that is a followup.

## 242. A database error after the processor call is recorded, not stranded

A deadlock or other database error raised after `adapter.create_payment`
returned aborted the dispatch session: the audit write and commit failed with
it, the request 500'd, the run stayed `executing`, and the processor's payment id
lived only in memory — so nothing could tell that payment had been sent. Each
attempt now runs in a savepoint taken after the payment row lock. A database
error rolls back to it, and the payment is recorded `failed` with the provider
id, reference and order fields restored from a record kept outside the ORM, so
`classify_payment_failure` reads it as **in doubt** and `/retry-failed` never
re-sends it. The run then finishes.

That alone would have traded a stranded run for a double payment. `failed` is a
terminal status, so the row stops holding the invoice's live-payment slot, and
the rollback left the invoice `approved` — back in the queue for the next run,
under a fresh idempotency key the processor treats as a new order. Before this
change the same error left the row `pending`, which at least kept the slot. So
the dispatch opens the payment-blocking `payment_reconciliation` exception the
reconciler already opens when it ages out an in-flight payment, through one
shared helper, and a new run for the invoice is refused until a human has
reconciled the order. The same holds when a non-database exception is raised
after the money-moving call, which had the hole before this batch.
`completed_at` stays NULL on an in-doubt row: it is the settlement timestamp,
and nobody can show this order settled. Compliance
release takes the same guard. If the savepoint itself cannot be rolled back,
the dispatch falls back to a full rollback and re-lock before recording.

## 243. The pytest-split baseline is measured by the shards that use it

`backend/.test_durations` went from its first commit to 25.12% of the suite
unweighted without once being regenerated, and the guard that watches it
(`check_test_durations.py`) tripped on this PR. Regenerating it with
`pytest --store-durations` on a laptop costs an hour and measures the wrong
hardware. Every backend shard now runs with `--store-durations
--clean-durations` — after the split has been read, so it changes nothing about
which tests run — and uploads what it measured; `pnpm gen:test-durations <run-id>`
unions a run's eight artifacts, refusing an incomplete or overlapping set. The
first such baseline covers all but 0.08%, and the ratchet moved from 0.25 to
0.05.

## 244. Help is in the app, written per role, and its prose names the UI by catalogue key

FeohLedger had no user-facing help: everything a new AP clerk or CFO needed to
know lived in engineering docs they will never open. The help centre at `/help`
(`frontend/docs/help-centre.md`) puts it where people already are, and three
choices shape it.

**Per role, not per feature.** The four system roles do different jobs and see
different pages, so the landing page opens on a "your first week" guide for
the reader's own role (their most senior one; `?role=` shows another), with
that role's everyday tasks beside it. A guide's "Open …" button and the page
directory both use `nav.ts::canSee`, the sidebar's own gate. That way help never
sends someone to a page that would refuse them, and a page their role can't open
is still listed and marked, so they know it exists and who to ask.

**Help is reachable from where the question arises.** Every `PageHeader` links
to "How this page works", resolved from the page directory (`PAGE_HELP`) with no
per-route wiring. `content.test.ts` holds `PAGE_HELP` to `NAV`, so a new
sidebar page can't ship without help. AP jargon gets an ⓘ `HelpTip` where it is
used. The tip is a button-driven toggletip, not a hover tooltip, so it works by
keyboard and touch. It shows the term's catalogued one-liner, a glossary link,
and the guides that explain it. Those guides are fetched by dynamic `import()`
on first open, because `PageHeader` and `HelpTip` render on every page and must
not carry the guide prose into every route's bundle.

**What is translated, and how the rest can't drift.** Rule one of
`frontend/docs/i18n.md` is that no user-facing string is a literal. Translating
around twenty-five guides into five languages, and keeping six copies in step
on every edit, is a content programme rather than a catalogue backfill. Doing it
badly (machine output nobody reviews) would be worse than an honest English
original. So the split mirrors §174 (legal) but narrower:

- **Catalogued, all six locales:** the chrome, every glossary term's name and
  one-line definition (the in-app tooltip text), and every diagram label.
- **English prose:** guide bodies, glossary long text and page-directory lines.
  They render with `lang="en"` (WCAG 3.1.2) under a notice, in the reader's
  language, saying the guides are in English for now. Translating them is a
  tracked follow-up.

The prose never types a UI label. `{ui:key}` renders the catalogue label, so a
German reader sees *Freigeben* in the middle of an English sentence about
approving, exactly as it appears on their screen. `content.test.ts` resolves
every `{ui:}`, `[[term]]`, `[[guide:]]` and `[[page:]]` reference, rejects a
key whose value has a placeholder, and enumerates `INVOICE_STATUSES` against the
lifecycle walkthrough. A renamed button, a deleted route or a new workflow
status fails CI instead of leaving a guide that describes an app that no longer
exists. That is the failure mode help systems are known for.

**Typed data, not markdown.** mdsvex is installed and would have made guides
easier to type. It was not used because the references above need to be data a
test can walk: a markdown link to `/payments` is a string nobody checks. A
`{ui:}` label also has to render through `m()` at runtime. Guides are
TypeScript objects with a ten-token inline grammar, rendered element by element
(`RichText.svelte`), so the tree keeps its no-`{@html}` rule.

**Illustrations are code.** Diagrams are inline-SVG Svelte components
coloured from the `app.css` tokens, so they follow a white-label tenant's
accent, and labelled from the catalogue, so they translate. A rendered
PNG would have baked in English and one brand colour.

Writing the guides against the code, not the docs, turned up a set of
places where the engineering docs or in-app copy disagree with what the app
does. The guides follow the code. Pure doc drift and false in-app copy were
corrected in the same change. The rest is filed in `docs/followups.md`
(product calls and sized work) or `docs/known-issues.md` (diagnosed defects).

## 245. Who may see the org's cash position is one answer, read off the routes

The AP assistant is open to every employee, and its per-tool gate was set only
on the five copilot tools. `get_payment_forecast` returns the same due-dated
committed and pending outflow as the copilot's `get_cashflow_forecast`, with no
gate, so a clerk could ask the assistant for a forecast the app refused them.
It now carries `FINANCE_LEADER_ROLES`. The rule that keeps the next tool honest
is a test, not a comment: `test_tool_allowed_roles_match_rest_gate` names each
tool's REST counterpart, reads that route's actual `require_roles` set out of
its dependency tree, and fails on a mismatch or on a tool without a row.

Reading the gates that way showed a second disagreement. The REST forecast,
what-if and cash position were `admin`/`cfo`, while AP managers already had the
same figures from the copilot (§54) and from the `cashflow_forecast` CSV export.
Narrowing the copilot would have taken away a planning surface built for AP
managers; widening the three REST reads takes away nothing anyone relied on and
makes every surface agree. They use `CASH_FORECAST_ROLES`, pinned to
`COPILOT_ROLES` by the same route-reading helper. The rest of the CFO surface
(`/analytics/cfo`, the drill-downs, forecast variance) stays `admin`/`cfo`, and
so does the `/cfo` page in the sidebar: this is consistency of data access, not
a new screen for AP managers.

## 246. A CSV-imported invoice is checked for warnings when it is imported

`import_invoices_csv` created rows at `new` without `refresh_warnings`, which
manual create, upload and every PATCH call. Approval was still safe (submitting
for review refreshes), but a duplicate imported by CSV sat unflagged in the list
until someone submitted it. Rows imported at a live status (`new`, `rejected`)
are now refreshed after the batch flush, in the request's transaction and with
the org's fraud-rule settings, so the warnings and their exceptions commit with
the invoices or not at all.

Historical `done` and `paid` rows are deliberately not refreshed. They never
reach a payment run, and flagging years of settled history on a Day-0 load would
bury the exception queue under findings nobody can act on. They still count as
the other side of the duplicate check, so a live row repeating one of them is
flagged.

## 247. Touchless rate means straight-through processing, defined once over the audit trail

The dashboard's `touchless_rate` counted every invoice that cleared review,
including the ones a person approved, so it measured cleared-vs-rejected under a
name the industry uses for something much stricter. The experiments readout used
a third rule (auto-approved with no changes on the approval row) under the same
label. The definition chosen is the one AP benchmarks use: an invoice is
touchless when it reached approval or later with an `invoice.auto_approved` row
and no person intervened — no human review decision, no field, line-item or GL
correction, and no exception a person resolved, dismissed or escalated. An
exception agent's own decision is not a touch; that is the automation being
measured. The denominator is every invoice that reached a review decision, with
the existing evidence gate on `done`/`paid`/`failed` and CSV imports out of both
legs.

It lives in `services/touchless` as SQL predicates, and the dashboard and the
experiments readout both use them, so the two figures cannot disagree. Renaming
the KPI to what it used to measure was the alternative. It was rejected because
the cleared-vs-rejected rate is close to 100% in any healthy tenant and tells a
buyer nothing, while straight-through processing is the number they ask for.
Because the true figure tops out far lower, the KPI turns green at 49% (Ardent
Partners' 2025 best-in-class) rather than the old 80%. Every fact used was
already recorded, so there is no migration. History older than the actions that
record corrections (2026-06/07) and exception decisions (2026-08-15) can
over-count an auto-approved invoice; `backend/docs/analytics.md` says so.

## 248. AP clerks enter invoices; entry is a role, and nothing a clerk does ends in an approval

Standard AP practice gives intake, validation and GL coding to the clerk and
keeps *approval and payment release* separate from it. Clerks could do none of
the first, so `ap_clerk` joins `INVOICE_ENTRY_ROLES` (create, upload, the file
routes, PATCH, line items, extract/reset, complete, resubmit, bulk status) and
`INVOICE_IMPORT_ROLES` (CSV import of open AP only; historical `done`/`paid`
rows, which assert a payment already happened, stay with admins and AP
managers).

Segregation stays at approval, where it already lived. Every create path stamps
`uploaded_by_id`. An entry-only caller (a clerk with no admin, AP manager or CFO
role) who changes the content of an invoice they did not upload is added to
`segregation_actor_ids` (`invoice_entry.stamp_entry_editor`), so a clerk later
granted approval can never approve figures they keyed. Manager edits are not
stamped: approve-with-corrections is the approver editing what they sign, and
stamping it would refuse a one-approver org's manager the approval of an
invoice they fixed.

The entry-only caller's window closes **at submit**, not at approval. Approval
binds to no version, so an edit landing between the approver's read and click
(re-pointing the payee re-links `vendor_id`) would be approved unseen; rework
goes through reject. The window is also closed on any invoice ever approved
(`approval_date` or `approved_by`), because an approved invoice whose ERP push
failed sits at `failed`. Re-extracting such an invoice is refused for every
role, not only clerks: re-reading the document would rewrite what the approver
signed while `approved_by` still names them, and Retry ERP is that invoice's
path. Nothing an entry-only caller does ends in an approval without a second
person: their `/complete` skips the `auto_approve_below` floor whatever
`require_segregation` says, and their upload and extract run with
`suppress_auto_approve` (the flag §75's portal resubmit introduced).

Entry was deliberately **not** made an `invoice.create` catalog permission. Both
role-grant guards read catalog membership as "sensitive", so a clerk-held
permission stopped a `user.manage`-only role from onboarding or managing
clerks, and entry's control is per invoice at approval, not a role split. The
cost is that a custom role cannot be granted entry; that is a follow-up for the
first customer who needs a non-clerk preparer. Also rejected: stamping every
editor, which would refuse a manager the approval of an invoice they
corrected; and keeping `ready_for_review` editable behind a chain-signoff
check, which left level 0 and single-level approval open. Tying approval to the
version the approver saw closes the same race for managers and is a tracked
follow-up.

## 249. A PO mismatch blocks payment only when it could over-pay, and a refresh may lift it (migration 0105)

Three things the docs and in-app hints already claimed were not true: an
unverified vendor could be paid, and neither a failed inspection nor a PO
mismatch held payment. The product call was to make the claims true. A vendor
that is unverified, inactive or rejected is now refused by run creation,
`POST /api/payments`, compliance release and `/cards/generate`, through the one
`inactive_vendor_statuses` rule. `po_mismatch` and `quality_hold` joined
`PAYMENT_BLOCKING_EXCEPTION_TYPES`, and so the SoD-on-clearing set of §169–§170.

Joining that list changed what may raise a `po_mismatch`. The first cut blocked
every mismatch, and review showed that reversed §67 and held under-billing too:
the matcher compares each invoice with the whole PO total, so the first invoice
on a split or blanket PO reads as a large negative variance. A row is now raised
only where paying could pay more than the PO supports: the cited PO doesn't
exist, the currencies differ, the invoice bills above the PO beyond the match
rule's tolerance, or it bills beyond the received share of the PO
(`po_total × received / ordered`, exact Decimal, at the same tolerance).
Billing under the PO and in-tolerance variances stay warnings. This supersedes
the queue half of §67: an over-receipt is still flagged independently of
status, but as a warning on the invoice, not a queue row, since §67 rejected
blocking on a receiving discrepancy and the queue type now blocks.

A blocking row that nobody can clear once the goods arrive would make receipts
irrelevant, so a warnings refresh that no longer finds the problem closes the
open or escalated row through `record_decision`, with a NULL actor and
`via: po_match_refresh`. §169–§170 treat clearing as a human sign-off, and this
is the deliberate exception, allowed only where nobody with a motive could have
produced the evidence: the org's own settings must be present (never the 5%
default); after approval the same PO must still be cited, and the finding must
be gone even under the strictest rule any GL code could select, since
`gl_account` stays editable; a cleared quality hold must rest on a QMS verdict
or on a manual inspection whose recorder is known and not implicated in the
invoice; and no agent may be mid-decision on the row. Exception agents match
under the org's rules too, and if an agent's own change leaves the finding in
place, the coordinator unwinds it and escalates.

Who recorded an inspection could not be read from the audit log, because it
fails open with `FEOH_AUDIT_MODE=lambda`, where no local row exists. Migration
0105 adds `quality_inspections.source` (`manual`/`qms`) and
`recorded_by_user_id`, written by the inspections API and the QMS sync (which
also overwrites a manual row's PO and receipt links when it takes the row over,
so a typed verdict can't pass as a synced one) and pinned by a stamping guard.
Nothing is backfilled: a pre-0105 inspection has no honest recorder, so it
stays held for a person.

Rejected: a non-blocking queue type for over-receipts (the type roster is
fixed); blocking on finding code rather than type (the per-type de-dupe lets a
non-blocking row hide a later blocking finding); a one-off sweep of open rows
(it goes stale the next day); freezing the match rule on the invoice (a schema
change for what the strictest-rule check already secures). Per-line receipt
valuation and cumulative billing against one PO are a follow-up.

## 250. An accepted early-payment offer changes what is paid (migration 0104)

Accepting an offer is the buyer agreeing to pay the discounted amount by the
deadline, yet a run paid the full amount and the documented workaround was a
credit memo for the discount. `payment_runs.payable_amounts` now nets applied
credits and then deducts the accepted, invoice-scoped offer whose deadline the
pay date meets. The run builder, `POST /api/payments`, dispatch and
`/retry-failed` all book and re-check against that one figure. It is stored on
the payment with the offer (migration 0104, both directions) rather than
re-derived, because settlement verification, capture, void, FX and 1099 totals
must key on what was authorized, and each booking writes a
`discount_offer.applied` audit row.

If the applicable offer differs at dispatch from the one booked — the deadline
passed while the run waited, or an offer was accepted after booking — the
payment fails `discount_changed` before the processor is called. Re-pricing
would either short-pay a supplier who considers the offer dead or move money
nobody approved; a fresh run is the remedy. An offer that can't be honoured
(vendor-scoped, expired, in another currency, priced on an amount the invoice
no longer has) pays in full rather than refusing. An applied memo for exactly
the savings is read as the discount already taken, because the old workaround
and the help text produced exactly such memos and deducting again would
short-pay. That is an amount coincidence, resolved toward paying the supplier
in full; linking memos to offers is a follow-up.

Cards: a card is spendable up to its limit and nothing compares a charge with
`Payment.amount`, so a discounted payment refuses to settle onto a card whose
limit differs (`card_limit_exceeds_payment`), and `/cards/generate` mints net
of credits and takes no discount. Both new foreign keys are `ON DELETE SET
NULL`, not `RESTRICT`, because cancelling a draft run deletes its pending
payments. Rejected: a calendar cutoff for the memo rule (it misfires for a
tenant deployed late) and refusing such a booking as ambiguous (nothing could
resolve it).

## 251. The pilot moves no money: record-only mode, "paid outside", and a NACHA file (issue #517)

For the pilot, customers pay their suppliers from their own bank or ERP and
FeohLedger records it. That takes payment-rail KYB, NACHA Third-Party Sender
registration, the annual ACH Rules Compliance Audit, ACH fraud monitoring and
the money-transmitter opinion off the critical path, because each exists only
if FeohLedger moves customers' money. The code had to change in three places to
make that model honest.

**Execute had to be impossible, not just unused.** With no processor configured
the adapter is `mock`, which reports every payment `completed`; one Execute click
would have marked invoices `paid` with nothing sent.
`services/payment_execution_mode` resolves each tenant to `processor` or
`record_only`, and `api/payments._require_payment_adapter` — which every
dispatcher already called — refuses `record_only` with 409
`payments_record_only` before anything is claimed. It fails closed: an unknown
`mode`, and any **deployed** environment whose provider is absent or `mock`,
resolve record-only even if the admin set `mode: processor`. Local dev keeps the
mock (guard rail 7); the mock is honest where no real money exists. Rejected: a
per-tenant switch alone (a tenant that never set it would still be one click
from fake payments) and refusing `mock` everywhere (breaks local-first).

**`paid` needed an honest writer.** The rail was the only automatic path to
`paid`, and the ERP-led path the issue described as working did not: `posted_in_erp
→ paid` is not an edge, so the ERP's `Paid` was dropped silently (pinned by a
test as intended). `services/external_payment` now records a payment made
outside FeohLedger as a `completed` `Payment` with `provider = "external"`,
dated on the paid-on day at midnight UTC, and walks the invoice through
`payment_scheduled` to `paid`. The user endpoint, the run-level endpoint and the
ERP webhook all use it. A real `Payment` row, rather than a status flag on the
invoice, is the point: bank reconciliation, the 1099 YTD aggregate and aging
already read completed payments, so none of them needed a special case. Rejected:
adding a `posted_in_erp → paid` edge (it would make `paid` mean "something said
so" with no amount behind it, and 1099 would miss the money).

Which controls carry over follows from what each protects. Approval-style
segregation of duties applies (the payable's creator may not record its payment),
with the existing single-operator opt-out. Payment-blocking exceptions refuse,
because closing the item would bury a duplicate or fraud flag. A live card or
another live payment refuses, because each is a double-pay risk. The CFO
threshold and vendor verification do **not** apply: both stop FeohLedger from
*sending* money, and refusing to record money already sent leaves the ledger
wrong without un-sending it. An ERP report that hits a refusal opens an
`erp_reconciliation` exception rather than vanishing. Partial payments are
refused, not approximated; the amount must equal the net payable.

**The bank file stays the customer's origination.** `GET /runs/{id}/nacha`
writes a standard unbalanced CCD/PPD credit file from a draft run, which the
customer uploads to its own bank as originator. It is allowed only on record-only
tenants, because on a processor tenant the file plus an Execute would pay twice.
It keeps the run's maker-checker and CFO sign-off, since the upload is the step
that moves money. It audits a SHA-256 of the file, never its contents. The run is
then closed with `POST /runs/{id}/record-outside`, which refuses the whole run on
one bad invoice rather than leave it half-recorded.

**A money-path review tightened the bank file before it landed.** The first cut
treated the export as a read, and that was wrong in three ways. It checked less
than `/execute`: a `fraud_flag` raised by a bank-detail swap approved after
staging, or a credit applied since, would have reached the file, carrying the new
account or the stale amount. `/execute`'s pre-adapter checks therefore moved into
`payment_runs.dispatch_preflight`, which the dispatcher, the export and the
run-level record now all call. The export also left the run `draft`, so a second
file, a cancel-and-restage, or a later switch to processor mode could each pay
the same suppliers twice. The first export now claims the run (`exported`, a
claim state execute and cancel refuse). A second file needs `regenerate=true` and
carries the next file-ID modifier, counted on the run (migration 0106). Counting
audit rows was rejected because in `lambda` mode they land late. A reasoned
`/nacha/void` is the bank-rejected exit. Finally, the export was gated on
recording when it is the money moving, so it is now gated on `payment.execute`.

Two neighbouring gaps closed in the same pass. An ERP `Paid` for an invoice
FeohLedger still means to pay itself — in a run, with a live payment or card —
now raises the blocking `payment_reconciliation`. The non-blocking
`erp_reconciliation` it raised before would have let the run pay the supplier a
second time. Direct card minting refuses on a record-only tenant, because a card
is a payment by another door.

Recording gets its own permission, `payment.record_external`, rather than reusing
`payment.execute`. Recording moves no money, and an org that sends payments
through a few people may still want others to book what they paid.

The trigger to revisit is a signed customer who wants FeohLedger to send payments.
Then the entity, KYB, Third-Party Sender registration and the money-transmitter
opinion return (`docs/founder-runbooks/payment-rails-onboarding.md`).
## 252. The pilot is signed by a sole proprietor, not a company (2026-10-07)

The operator is trading as a sole proprietor and will sign customer #1 that way.
This settles the open choice in #517 ("form an entity before customer #1, **or**
record an explicit decision to sign the pilot as a sole proprietor"); this
section is the record. `OPERATOR.controllerDescription` in
`frontend/src/lib/legal/operator.ts` already said so.

What it changes. Incorporation was the keystone of the founder critical path
(#446 §1) only because a contract needs a party, and a sole proprietor is one:
Stripe, insurers, the EU/UK Art 27 representatives, the SCCs with each
sub-processor, a SOC 2 vendor and counsel can all be engaged now, in the
operator's own name trading as FeohLedger. The C-corp steps — formation, the
83(b), founder vesting, Delaware franchise tax, a registered agent — leave the
path. Nothing is required to replace them: the operator contracts and bills in their
own name, "Jared Howard", with FeohLedger as the product's name, so no trade-name
(DBA) filing is needed — that is only required to transact *under* a name other
than your own. A DBA, an EIN (to keep the SSN off customer W-9s and Stripe) and a
separate bank account are optional conveniences
(`docs/founder-runbooks/legal-entity.md`).

The legal pages publish no postal address. They state the operator's location
(Virginia, United States) and make email the contact channel for every request
and notice. GDPR Art 13(1)(a) asks for "contact details", which an email
address satisfies; the US state privacy laws that would ask for more (CCPA,
VCDPA) apply only above size thresholds this pilot is far below. Rendering
"Virginia" where the pages said "postal mail reaches us at …" would have been
a claim nobody could act on, so `operator.ts` gained decided states instead:
`legalEntity: false` (no separate company), `postalAddress: false` (email
only, `location` stated), and `dataProtectionOfficer: false`. Counsel should
confirm the email-only position when reviewing the set.

The same day the operator set the remaining domestic facts: the Terms and the
DPA are governed by the law, and disputes go to the courts, of **the
Commonwealth of Virginia** (the SCCs keep Irish law, which Clause 17 requires
to be an EU member state's); customer data is hosted in **AWS `us-east-1`**,
the region `infra/` already deploys to; and `supervisoryAuthority: false`,
because a controller with no EU establishment has no lead authority under the
Art 56 one-stop shop — a data subject complains wherever they live or work,
which the policy already says. That leaves only the EU and UK Art 27
representatives pending, and they are owed only once the Service is offered to
people there.

A foreign customer does not change the governing law: the contract stays under
Virginia law whoever signs it — that is what choosing one is for. What a
foreign customer brings is that country's data-protection law, which applies
by where the people are, not by what the contract says. The EU/UK machinery
(DPA, SCCs, Art 27 representatives) is already written; any other country is
assessed when its first customer appears, as a counsel question, not
pre-built.

What it costs. Liability is personal and unlimited. The Terms' cap binds only
customers, not suppliers or regulators, and preserves gross negligence, wilful
misconduct, fraud and confidentiality breach (§13.4) — and the product holds
supplier bank details. So **E&O and cyber cover become a hard gate before
customer #1**, not a parallel nice-to-have: for a sole proprietor they are the
only protection between a claim and personal assets. Some procurement teams
will not contract with an individual; Terms §1 already permits assignment to a
company formed later, so that is a migration, not a re-papering.

Triggers to form an entity (a single-member LLC is the cheap step; the C-corp
path stays documented for fundraising): a customer whose procurement requires
it, a customer who wants FeohLedger to move money itself (payment-rail KYB needs
a business — #446 §2, decisions §251), raising money, or revenue or exposure
that insurance no longer comfortably covers. Rejected: incorporating
pre-emptively — it costs ~$550+/yr recurring and weeks of calendar time to
solve problems the no-rail pilot does not have.

## 253. Pricing: three tiers metered on AI-read invoices, unlimited users (2026-10-07)

The operator chose the tiers after a competitor and practice review (Bill.com
and Ramp price per user; Dext sells ~250 documents for ~$38; usage plans churn
on bill shock; the "SSO tax" is resented and the top-tier paywall has moved to
SCIM). Issue #426 — the pricing page selling a product the billing code did not
model — closes with this.

| | Free | Growth | Scale | Enterprise |
|---|---|---|---|---|
| Price | $0 | $49/mo | $199/mo | negotiated contract |
| Users | unlimited | unlimited | unlimited | unlimited |
| AI-read invoices included | 100/mo | 500/mo | 3,000/mo | negotiated |
| Past the allowance | AI reading pauses | $0.10 each | $0.07 each | negotiated |
| Features | core AP | + ERP integrations, public API + webhooks, SSO (OIDC/SAML) | + SCIM, "require SSO", multiple entities, audit-log SIEM export | — |

**The metered unit is an AI-read invoice**: a distinct invoice whose extraction
ran on the *platform's* model key and succeeded, counted per calendar month in
UTC. BYOK extractions are the customer's own model bill and never count;
structured e-invoices (PEPPOL, UBL/Factur-X, CSV import) use no model and never
count; re-reading one invoice in the same month counts once. That is the unit
because model calls are nearly the whole marginal cost (~$0.02–0.08 an invoice);
users and storage are noise beside it, which is why users are unlimited — a
direct contrast with per-seat competitors.

**Reaching a limit never blocks accounts payable.** On Free, and on a paid tier
whose customer-set monthly spending cap is reached, *only AI reading pauses*:
the invoice is still created and lands for manual entry, and approval, matching
and payment recording keep working. Hard-stopping a tool a business pays its
suppliers through is the failure the usage-pricing literature is unanimous on.
Paid tiers bill overage instead of stopping, with 80% and 100% notices so the
bill is never a surprise.

**Gating spelling.** Features are `plan_catalog.FEATURE_*` constants, granted by
a truthy key in `Plan.entitlements`; allowances live in `Plan.usage_components`
under `plan_catalog.METER_AI_INVOICES` as `{"included": int,
"overage_unit_price": decimal-string | null}`. `null` overage means "pause at
the limit". Enterprise is not a catalog plan: an operator configures a
negotiated plan per customer, and the pricing page shows it as contact-sales.

Migration `0107_plan_catalog_v2` rewrites `entitlements` and
`usage_components` on the three existing catalog rows from the catalog itself,
once — `ensure_plan_catalog` deliberately never touches an existing row, so
without it a control plane provisioned earlier would keep the v1 grants forever.
Prices and trial days are left alone.

Rejected: per-seat pricing (punishes the AP teams the product is for, and is
not where the cost is); a hard stop at the paid tiers' allowance (bill shock in
reverse — an outage the customer did not choose); SSO as Scale-only (an SSO tax
on a feature that costs little to serve); counting re-reads and failed
extractions (charging for our own retries).

*Update 2026-10-19:* §281 moves `erp_integrations` onto Free, so the Features
row now reads Free "core AP + direct ERP integrations", Growth "+ public API +
webhooks, SSO", and Scale adds "ERPs through Merge.dev" (`erp_merge`). Prices
and allowances are unchanged.

## 254. The pilot runs on one VM with the database on RDS (2026-10-07)

The workload stack is the single VM of `docs/minimal-deployment.md` — Docker
Compose running Caddy, the API and Redis — with **one change: the database is
RDS for PostgreSQL 16, not a container on the VM.** Defined in
`infra/network.tf`, `compute.tf`, `database.tf` and `monitoring.tf`; roughly
$45–55 a month against ~$22 for everything on the VM and $120–200+ for the ECS
build-out in `docs/production-deployment.md`.

Why not the cheaper all-on-the-VM shape: its recovery story is the nightly
`pg_dump`, so a lost VM loses up to a day of approvals, audit rows and payment
records. For an accounts-payable system that is the one failure a pilot customer
does not forgive. RDS gives point-in-time restore to within about five minutes
for ~$15/month, and makes the VM disposable — replacing it costs a rebuild, not
data. The compose stack already treated `FEOH_DATABASE_URL` as the override seam
for exactly this, so the app needed no redesign.

Why not the ECS build-out yet: it buys multi-instance scaling, automatic deploys
and a CDN — none of which a handful of pilot tenants need — at three to four
times the monthly cost and several days of build. Each piece graduates on its
own trigger (`docs/minimal-deployment.md` § What's left out) and the same image,
env contract, database and S3 layout move across unchanged.

Choices inside the stack:

- **Its own VPC, no NAT gateway.** Public subnets for the VM, private subnets
  with no internet route for RDS. A NAT gateway would cost ~$33/month to serve
  subnets that need no outbound traffic.
- **No SSH.** Session Manager only: no port 22, no key pair, every session an
  IAM-authorized CloudTrail event.
- **The RDS master password never enters Terraform state.** It is an ephemeral
  variable feeding the write-only `password_wo`, supplied from `infra-secrets`
  at apply time. The previously documented plan — a `carlpett/sops` data
  source — was rejected because a data source's value is written to state in
  plaintext.
- **TLS forced on the database** (`rds.force_ssl`), deletion protection on, a
  final snapshot on delete, 7 days of point-in-time restore plus the 90-day
  nightly logical dumps.
- **t4g.medium, not t4g.small.** AI extraction and PDF rendering now run
  in-process beside the API, Redis and Caddy; 2 GB leaves no headroom.

Rejected: Multi-AZ RDS (doubles the database cost; a variable flips it on when
a customer needs an uptime commitment); ElastiCache (Redis holds only
ephemeral state on this stack); the account's default VPC (all-public subnets,
and not reproducible from code).
## 255. AI-read metering: one count, an unlocked gate, per-unit overage billed by ordinal (2026-10-07)

§253 set the unit and the prices. This is how the code enforces and bills them
(`backend/docs/billing.md` § AI-read invoice metering).

**One owner of the count.** `services/billing/ai_invoice_meter.count_ai_invoices`
is the only definition: distinct invoices with a successful `platform` usage row
from a *billable* provider in the UTC month. Billable is "costs the platform a
per-document charge" — `claude_vision`, `openai_vision`, `aws_textract`. `mock`
(the keyless dev/e2e reader; counting it would trip Free on every e2e tenant),
`ollama` (self-hosted, no per-call cost) and `einvoice` (structured e-invoices,
which still write a `platform` row) are declared non-billable, and a test fails
until every registered adapter is in one of the two sets. An unlisted provider
is not counted: undercounting costs us cents, overcounting bills a customer for
something nobody decided was billable. No live subscription reads as the Free
allowance; a plan with no component — or a malformed one — reads as unmetered,
because an operator's typo must not pause a paying customer.

**The gate runs before the model and is deliberately unlocked.** A paused read
sends the invoice `pending → new` (a new edge, refused to humans by
`POST /bulk/status`) with a coded `ai_reading_paused` warning — not `failed`,
which leads only back to re-extraction and would pause again. Two reads at the
boundary can both pass. Rejected: a per-org lock held across a 5–30 s model call
(one read at a time per tenant, to protect a few cents) and a reservation row
(a schema change for the same few cents). The overshoot is bounded by concurrent
workers, is our cost, and cannot reach the bill — see the cap below.

**Overage is reported per unit, by ordinal, against a per-unit metered price —
not a graduated "first N free" price.** The task brief suggested a graduated
Stripe price with one event per newly-counted invoice. Rejected because
Stripe's billing period is anchored on the subscription's start day while the
allowance is a UTC calendar month: a graduated price applies the allowance a
second time over a different window, so the two would disagree about which
reads were free whenever the anchor is not the 1st. Instead OUR count applies
the allowance and only overage is reported, one event (value `"1"`) per unit,
identified `ai-overage:<org>:<month>:<n>`. The identifier is the ordinal, not
the invoice id: which invoice is "the 501st" can change while concurrent
extractions commit out of `created_at` order; how many there are cannot, so a
re-run computes the same identifiers and the provider's idempotency
(`Idempotency-Key` = identifier, 24 h) absorbs a resend. Progress is a
max-merged marker on `settings.billing.ai_overage_reported[month]`; the hourly
backstop sweep stays inside the idempotency window.

**The cap is enforced twice.** The gate pauses at it, and the reporter bills
`min(overage, floor(cap / price))` on its own — so the customer's bill never
passes the cap even when the unlocked gate let a read through. A cap of `0.00`
turns a paid tier into "pause at the allowance".

**Follow-ups run after `commit()` returns, not from `after_commit`.** The
`post_commit` hook fires before SQLAlchemy releases the session's connection,
and an extraction worker's tenant pool is a single connection — a job reading
the tenant DB from it would wait on itself. The same single-connection pools
exposed a latent defect fixed here: `extraction_dispatch._run_local` held its
one control connection for the whole extraction, so every control-plane session
opened underneath (notification and audit hooks, now the gate) waited out the
30 s pool timeout.

**Notices are claimed, then sent.** A threshold key is added under the org row
lock before the send and released if nobody was reached; at most once, because
the check runs after every billable read and a duplicated billing email reads
as a billing error. (`cash_flow_alerts` sends first and accepts a duplicate; its
check runs once a day, this one many times a minute.)

## 256. ERP through Merge.dev is a Scale feature; QuickBooks Online and Xero go direct (2026-10-07)

§253 put "ERP integrations" in Growth ($49/mo). Every non-mock ERP config
routes through Merge.dev by default (`erp_adapters/dispatcher`:
`integration_method` defaults to `merge_dev`). Merge's Launch plan
(merge.dev/pricing, read 2026-10-07) is free for the first 3 production linked
accounts, then $650/month for up to 10, then $65 per linked account. One tenant
is one linked account, since an org holds one `settings.erp`. Past three
customers, a Growth tenant on Merge costs at least $65 a month against $49 of
revenue.

**Decision.** Split the entitlement:

- `erp_integrations` (Growth and Scale) covers the **direct** adapters:
  QuickBooks Online and Xero once built (`backend/docs/quickbooks-online-adapter.md`),
  plus the existing NetSuite and Business Central adapters.
- A new `erp_merge` feature (**Scale only**) covers `integration_method:
  merge_dev`, the long tail: Sage Intacct, MYOB, SAP and the rest. A customer
  on one of those systems is usually a Scale-sized company anyway.

**Rejected:**
- **A $50 Merge add-on on Growth.** It is below Merge's $65 marginal cost, so it
  loses money on every connection past the free three, before card fees or the
  support load of a sync we cannot fix ourselves.
- **A ~$99 add-on.** That price would cover the cost, but the billing model has
  no add-ons (plans are a flat price plus `entitlements`, §253). Building one
  means a Stripe price, proration and pricing-page support in money-path code,
  which isn't worth it before anyone asks. Revisit if Growth customers on
  long-tail ERPs ask for Merge.
- **Merge everywhere, absorbing the cost.** Margin goes negative at exactly the
  scale where the business is supposed to work.

The first three Merge connections are free, so pilot customers on long-tail
ERPs cost nothing while demand is tested.

**Not yet enforced.** No route checks `erp_integrations` today (see
`docs/known-issues.md`), and `erp_merge` doesn't exist yet. Both are tracked in
`docs/followups.md`. Until they land, this records the intended plan shape and
the pricing page must not advertise Merge-routed ERPs on Growth.

*Update 2026-10-07:* §258 now enforces `erp_integrations`. `erp_merge` is still
not built, so a Growth tenant can save and push a Merge-routed config; the
follow-up's trigger (the first paid Growth customer or the fourth Merge
connection) is unchanged.

*Update 2026-10-08:* §269 makes Sage Intacct, SYSPRO, Sage Business Cloud
Accounting and Blackbaud direct adapters, so they come under `erp_integrations`
on Growth like QuickBooks Online and Xero. Merge.dev on Scale remains the route
only for ERPs without a direct adapter.

*Update 2026-10-19:* §281 puts `erp_integrations` on Free as well, and builds
`erp_merge` (Scale only) with migration `0112_free_plan_erp`. Merge is now
refused on Free and Growth, which closes the follow-up above.
## 257. The single-VM database is RDS, reached over verify-full TLS set by PGSSLMODE (2026-10-07)

The operator chose the single-VM deployment with Postgres on **RDS for
PostgreSQL 16** (`db.t4g.micro`, PITR, `rds.force_ssl = 1`) from day one, wired
through the override seam the compose file already had (`FEOH_DATABASE_URL` in
the sops env). The container stays as the documented cheaper alternative
(`docs/minimal-deployment.md` § Database). Four calls followed:

- **TLS is set by `PGSSLMODE` / `PGSSLROOTCERT`, not by the URL.** Verified
  against the pinned asyncpg (0.31.0): with no `ssl` argument it reads
  `PGSSLMODE`, and nothing in the app passes one — SQLAlchemy forwards only the
  URL's query, and the raw `asyncpg.connect` calls now parse the URL with the
  same `make_url` (`tenant_provisioning.asyncpg_connect_kwargs`). libpq reads
  the same two variables, so one setting covers the app, Alembic, the Lambda
  handlers and `pg_dump`. A `?ssl=` on the URL would reach asyncpg and not
  libpq, so the deploy contract refuses a query string outright.
  (`make_tenant_url` and Alembic's tenant URL still had to stop dropping a query
  string — a latent bug for any option on the URL, fixed at the builder.)
- **`verify-full`, against a committed CA bundle.** `require` encrypts but
  accepts any certificate; verification needs the RDS CA set in the image.
  It is committed at `backend/certs/rds-global-bundle.pem` and sha256-pinned in
  `tests/test_container_supply_chain.py` rather than downloaded in the
  Dockerfile at a pinned checksum: one file serves both the api image
  (`ENV PGSSLROOTCERT`) and the `pgtools` container (bind mount), the build
  needs no network for it, and a refresh is a reviewed diff. Not added to the
  system trust store — these CAs are for the database, not every HTTPS call.
- **One place decides the mode.** `deploy/lib.sh` reads `FEOH_DATABASE_URL`
  from `deploy/.env` (shell env first, as compose does) and every script
  sources it; the `postgres` service sits under a `localdb` profile with the
  api's dependency `required: false`, so on RDS it never starts. Compose
  interpolates profiled-out services too, so the old `${POSTGRES_PASSWORD:?}`
  guard had to go — `decrypt-env.sh` now requires the password in container
  mode only.
- **Dumps continue on RDS, through a `pgtools` one-shot of the same image.**
  PITR is the primary recovery; the nightly dumps stay as the copy that
  survives losing the instance, its snapshots or the account. The password
  reaches the container as a bare `docker compose run -e PGPASSWORD` from the
  script's environment — never an argv `ps` shows. Restores into RDS skip the
  role globals and use `--no-owner --no-acl`: the app's one role is the master
  user, which is `rds_superuser`, not a superuser, and cannot recreate a
  container dump's `postgres` SUPERUSER.

Rejected: `sslmode=require` (no protection against a man-in-the-middle on the
VPC path, for no saving — the bundle costs nothing); running the dump tools
from the `postgres` service with `run --no-deps` (it mounts the data volume and
carries the container's password contract); a second compose file for RDS (two
files to keep in step, where a profile is one line).
## 258. The plan feature gates, and what a downgrade keeps (2026-10-07)

§253 named the features; this is how each is enforced and — the real design
call — what a tenant that drops to a cheaper plan keeps. Every gate answers
with one coded refusal, `402 plan_feature_required` with `params.feature`
(`api/deps.py::plan_feature_refusal`), so the SPA can name the tier; SCIM
alone answers in the RFC 7644 error shape an IdP parses. The full route table
is `backend/docs/billing.md` § Entitlement gating.

**The rule: a downgrade removes the ability to turn a feature ON, never data,
and never anyone's way in.** Stored configuration is not rewritten, so an
upgrade resumes it untouched. Turning a feature OFF is never refused.

- **SSO.** Sign-in reads the org's settings through its plan
  (`services/sso_plan.plan_scoped_settings`): without `sso` the stored block
  reads as switched off (no IdP button; the handshake answers as for an
  unconfigured tenant, re-checked at the callback / ACS), and without
  `sso_enforcement` `sso_only` reads as off. **Password sign-in reopens**
  rather than the tenant being locked out. Considered and rejected: honouring
  `sso_only` after the plan dropped SSO (nobody can sign in — the outcome §204
  already rules out for an unresolvable IdP block); keeping SSO working on
  Free until an admin turns it off (a paid feature given away indefinitely,
  and an admin who never logs in never turns it off); rewriting the stored
  block on downgrade (destroys configuration the customer paid to set up, and
  `change_plan` would grow a side effect on tenant settings). Accounts that
  have no password — JIT- or SCIM-provisioned — set one through the ordinary
  forgot-password flow, which never required SSO to be off
  (`tests/test_forgot_reset_password.py` pins it on a plan-less tenant).
- **SCIM.** Reads and deprovisioning stay open on every plan; provisioning and
  grants need `scim`. A leaver the IdP cannot deactivate stays active, and on
  a plan without SSO that account's password sign-in is open — so refusing
  the deprovision would turn a billing state into a security hole. A PUT with
  `active: false` applies the deactivation alone.
- **Entities.** Only creating one is gated; every existing entity keeps
  working, because invoices, payments and GL rows are scoped to it.
- **ERP.** Only a LIVE adapter is gated; `mock` stays open on every plan
  (guard rail 7). A downgraded tenant's stored live ERP refuses new pushes
  (send / retry / the completion leg, refused before the transition so the
  invoice stays `approved` and payable) but the ERP webhook and the
  `payment_erp_sync` sync-back of a payment already in flight are left alone —
  a downgrade must never strand money mid-path. An invoice whose push had already
  `failed` is refused a retry too; its way forward is the org's own — upgrade,
  or clear the live ERP config (switching to `mock` is never refused), after
  which the retry runs. Nothing is stuck behind the gate that a setting the
  tenant controls cannot release.
- **Public API.** Key minting, webhook creation and configuration are gated;
  revoking keys, switching a webhook off and rotating its signing secret are
  not — rotation is how a leak is remediated, and a downgraded tenant must
  not have to delete the delivery log to do it. `_emit` queues nothing
  for an unentitled org; deliveries already queued finish.

The `realdb` harness's orgs hold no subscription — which reads exactly like
`free` — and a test of a gated surface arranges its own plan
(`realdb.subscribe`, `@pytest.mark.plan`). Entitling every test org by default
was rejected: it would hide a gate accidentally placed on a core AP route,
which is the failure §253's "never block accounts payable" exists to prevent.
The seed puts `acme` and every e2e worker on `scale` (all workers alike, so
they stay interchangeable) and keeps `techflow` on `free`; e2e covers the free
and growth prompts by stubbing `/api/auth/me`'s `entitlements`, never by moving
a worker's subscription.

`audit_siem_export` gates nothing yet: there is no tenant-configurable SIEM
destination, only the operator's platform shipper. Tracked in
`docs/followups.md`; the SOX auditor export stays ungated regardless.

*Update 2026-10-19:* §281 gives Free `erp_integrations`. The ERP refusal a Free
tenant meets is now `erp_merge` (Merge.dev, Scale). The `erp_integrations`
refusal is reachable only on a negotiated plan that leaves it out. An org with
no live subscription reads as Free (`entitlements.NO_PLAN_ENTITLEMENTS`), so the
harness's subscription-less orgs still read exactly like `free`. The downgrade
rules above apply unchanged to `erp_merge`.

## 259. A month's AI-read overage is priced on the terms it was used under (2026-10-07)

**Context.** `report_ai_overage` (§255) recomputed every period it touched from
the org's plan and spending cap *as they stood at report time*, and the
backstop re-runs the previous month until its last second ages out of the
provider's window. So a month already used could be re-priced after it ended:
2,000 reads on Scale (3,000 included, owed nothing) became 1,500 Growth
overage units after a Nov 2 downgrade, and a cap raised after month end
billed past the cap that was in force when the reads ran. Separately, a resend
after a lost marker was stamped with a fresh timestamp, which Stripe treats as
a *different* request under the same idempotency key — refused for 24 h, then
accepted as a second event.

**Decision.**

- Each period's marker records its pricing **terms** (allowance, unit price,
  plan, cap), refreshed on every pass while the period is open. A **closed**
  period is priced only from its recorded terms. A closed period with usage
  but no recorded terms is refused and logged, never priced on today's plan.
- A plan or cap change **inside** the month applies from the next pass. Units
  already reported are never withdrawn, so an upgrade mid-month stops further
  overage but does not credit what was already billed. Credits are an
  operator action at the provider, not something the reporter infers.
- Every unit's event **timestamp is fixed and stored before the first send**
  (`batches`), so any resend is byte-identical and the provider replays it.
- The backstop sweep is turned on in the deploy template; the 24 h window is
  only a guarantee when something retries inside it.

**Rejected.** *A single timestamp per period* (first-of-month, or the first
report's instant): stateless, but it shrinks the late-report window from ~34
days after month end to as little as a few days, and a first-of-month instant
precedes a subscription that started mid-month. *Pricing a mid-month change
retroactively from its effective date*: it would need per-read plan
attribution the meter does not have, for a few cents either way at pilot scale.


## 260. Inventory and sales stay out of the AP product; receiving and a commerce sync come in

Asked whether a merchandise buyer (a sports retailer ordering ten tennis
rackets) could track ordering, stock and sales here, the answer was that the
buying half exists — requisition, catalog, budget, PO, invoice capture, PO
matching, approval, payment, stored documents — and the stock and selling half
does not: no item quantities, stock movements, valuation, POS, sales orders or
receivables. The call is to keep it that way. Those are a separate product
category (Shopify POS, Lightspeed, Square, Cin7, or an ERP's inventory module),
none of the AP competitors carry them, and doing them properly means costing
methods, multi-location stock, returns and sales tax — a second product, not a
feature.

What comes in instead, as three separate PRs (roadmap Priority 14): goods-receipt
entry, because 3-way matching is advertised but nothing could write a receipt;
direct ERP adapters for the largest ERPs, with a receipt pull; and a commerce
adapter family that pushes received merchandise into the business's own
inventory system. The AP side stays the record of what was ordered, received,
billed and paid; the commerce platform stays the record of what is on the shelf.

Rejected: a lightweight on-hand counter here (it would be a second, drifting
copy of the number the POS owns); waiting for the ERP adapters before receipt
entry (most of the self-serve segment has no ERP that owns receiving).

## 261. The help centre's strings are a second, lazily loaded catalogue per locale (2026-10-08)

Goods-receipt entry (PR #535) took the Japanese catalogue to 101 KB gzipped,
over the 100 KB per-chunk budget in `web-bundle-budget.yml`. The follow-up filed
when the catalogue passed 95 KB had already named the fix: the 334 `help.*`
keys — glossary term names and one-liners, help-route chrome, diagram labels —
were ~9 KB of every locale catalogue, and only `/help` and the ⓘ HelpTips read
them. They now live in `locales/help/<locale>.ts`, one more lazy chunk per
locale, loaded by `ensureHelpCatalogue()` from the /help layout and each
HelpTip. `help.pageLink` and `help.tip.*` stay in the main catalogue because
every page's chrome renders them. Largest catalogue: 92 KB; largest help slice:
10 KB.

English help is statically bundled, as English main is: it is what `m()` falls
back to, so a help key never renders as its raw name while a slice loads. On a
locale switch the help slice drops to English at once — never one language's
glossary under another's chrome — and reloads for the new locale if help copy
was on screen. The budget job counts only the largest help slice, the same
one-per-visitor reading it applies to catalogues; counting all five would have
charged every visitor for languages they never download.

Rejected: raising the per-chunk ceiling (the follow-up existed to avoid exactly
that, and the next feature would hit it again); trimming the new receipt copy
(a worse form to save a kilobyte); per-route catalogues (a reorganisation of
6,000 keys for a problem one prefix solved).

## 262. A goods receipt can be typed in, and the person who typed it cannot release their own invoice with it (migration 0109)

Nothing in the app could write a goods receipt — `scripts/seed.py` was the only
writer and no ERP adapter pulls them — so the 3-way leg and the "billed beyond
receipt" payment hold of §249 ran on demo data or nothing. `POST
/api/goods-receipts` (`services/goods_receipts.py`) and the `/goods-receipts`
form add the entry path; `POST /{id}/cancel` undoes a mistaken one.

A receipt is the evidence that lifts a `po_mismatch` on its own (§249's
auto-close), so the inspection rule of §249 is applied to receipts: each one
records `source = manual` and `recorded_by_user_id`, and while any live manual
receipt on the matched PO has an unknown recorder or one implicated in the
invoice (`approval_chain.violates_segregation`), no `po_mismatch` row on it
closes by itself. It is coarse on purpose — it does not work out which finding a
receipt cleared — because leaving a row for a person is the safe error.
Cancelling only reduces what counts as received, so it can raise a hold but
never lift one and needs no check. Unlike an inspection, a receipt with **no**
source is trusted, not held: before 0109 no app user could type one in, so a
NULL row was written by the seed script or an import, and holding them would
have frozen every existing hold that a real delivery should lift. That asymmetry
is why `tests/test_goods_receipt_source_stamping.py` exists — a new in-app
creation site that forgot the stamp would fail open, not closed.

The clerk may record receipts. Receiving is entry work, and the segregation
check — not the role list — is what stops a clerk releasing an invoice they
keyed. The CFO may not; nothing about signing off spend involves counting boxes.

The re-match runs in the same transaction and is **not** best-effort, unlike the
QMS sync's: a receipt that landed without the hold it should raise is worse than
a refused receipt, while the sync is a batch whose rows are the contract. The PO
row is locked for the whole write, which serialises receipts per PO and makes
the idempotency-key replay and the `GR-<po>-<n>` auto-number race-free. Receipt
lines name the PO line they receive against (`gr_line_items.po_line_item_id`)
so the form can show ordered / received / outstanding; the matcher still sums
the receipt as a whole, and per-line valuation remains §249's follow-up.

Rejected: holding NULL-source receipts like NULL-source inspections (it would
strand every pre-0109 hold); allowing only managers to receive (the
self-service segment's receiving is done by whoever opens the box); a
best-effort rematch (a silent failure leaves an invoice payable that the
receipt says is over-billed); per-line segregation (which receipt cleared which
finding is not recoverable from the match result, and guessing would be a
release on a guess).

**Amended before landing, from the money-path review.** Two holes the first
cut left open:

* *Cancelling a receipt could lift a hold.* The matcher read a PO whose only
  receipt was cancelled as "no receipt evidence" and fell back to a 2-way
  match, so cancelling the manager's six-of-ten receipt turned the clerk's
  full-PO invoice into a clean `matched` and the hold closed itself. A
  cancelled receipt is evidence that the goods did not arrive: a PO whose
  receipts are all cancelled is now a 3-way match with nothing received. This
  reverses the "falls back to 2-way, the honest answer" reading in
  `po-matching.md` § Cancelled receipts.
* *An exception agent could clear what the auto-close refuses.* The agent
  coordinator's gate vets only the human who triggered the run, so a
  colleague's run could resolve a `po_mismatch` resting on the uploader's own
  receipt. After the resolver's change the coordinator re-matches under the
  org's rules and escalates when `receipts_clear_hold` refuses.

An idempotency key now replays only the same request (PO, date, lines);
reused for anything else, including raced onto a second PO, it is a 409.

Still open, and tracked in `docs/followups.md`: whoever records a receipt can
still approve the invoice it supports — receiving is not yet one of the duties
segregated from approval — and mobile has no receipt entry.

## 263. Approval is bound to the version the approver saw (2026-10-07)

**Context.** Every approval door ran `review.approve_invoice` against the row as
it stood when Approve was clicked. An edit that landed between an approver
reading an invoice and clicking Approve — a manager correcting the amount, a
re-pointed vendor — was signed unseen, and the approval signature then
certified figures nobody had looked at. The out-of-app doors were worse: an
email, Slack or Teams message can sit for days, and its Approve token bound the
invoice id and nothing about what the message showed. Clerks were already held
out (their window closes at submit, §248); managers and every out-of-app link
were not.

**Decision.**

- **In-app, the token is `updated_at`.** `POST /invoices/{id}/approve` takes
  `expected_updated_at`; supplied and not current under the row lock → 409
  `invoice_stale_approval`, nothing written. Bulk approve takes an
  `{id: updated_at}` map and skips a stale row, or one missing from a supplied
  map. `GET /invoices/ids` returns each id's version, so a "select all
  matching" set binds the version at selection time. One helper
  (`api/invoice_version.py`) serves this and the PATCH guard.
- **Out of app, the token is a digest of the invoice the message announced**
  — what it displayed (invoice number, vendor name, amount + currency) plus
  what decides the payment it releases (vendor id, payment method, due date,
  entity). One function, `email_action_token.digest_of_invoice`, computes it
  from the row on both sides: the notifying caller passes it as
  `notify_event(action_facts=...)`, the endpoint recomputes it under the lock.
  GL coding and the vendor's bank details are deliberately not bound (the
  first is accounting, not cash; the second is a dual-controlled vendor record
  whose legitimate change would void every pending link). `updated_at` was
  rejected here: any metadata write (a warnings refresh, an assignment) would
  have killed a still-accurate link. Reject is not bound; a token without the
  claim does not verify.
- **Everything that changes the invoice has to move its version, and nothing
  may move it back.** A line-items save changes no header column, so it bumps
  `updated_at` by hand. The audit-summary cache fill, which writes `meta` while
  preserving `updated_at` so opening an invoice is not an edit, became a
  compare-and-swap: it wrote the version it had READ back unconditionally, so
  an edit committed in between kept its new amount and lost its new version —
  the stale-approval e2e caught an approval passing on exactly that.
- **The modal binds what it shows.** Its own file and line-item writes adopt
  the version they return; nothing else does.
- **Alongside, on the same paths:** a refused bulk approval runs in a savepoint
  so it leaves no routed chain behind, as the single door's rollback already
  ensured; an invoice with a live payment (an approved one in an exported run)
  can no longer be deleted out from under the bank, singly or in bulk; and every
  multi-invoice locker takes invoices in id order.
- **The field is optional on the wire**, as PATCH's is, and every first-party
  client (web modal, web bulk bar, mobile detail, swipe and bulk) sends it. The
  threat is an honest approver signing figures they were not shown; a caller
  able to omit it already holds `invoice.approve` and could approve the current
  version directly.

**Rejected.** *Making the token mandatory*: it adds nothing against a caller
who can approve anyway, and would break every API client and ~140 tests for no
gain in the threat this answers. *A content hash for the in-app path*: the
client already holds `updated_at`, and an approver looking at the full modal
saw every field, not a chosen subset.

## 264. A vendor that stops being payable takes its live virtual cards with it (2026-10-08)

**Context.** Payment runs and `POST /api/cards/generate` refuse a vendor whose status is not `active` or which is `payments_blocked`, but both gates only stop the *next* card. A virtual card is bearer-spendable until closed, so a card minted while the vendor was payable stayed live after the vendor was rejected, deactivated, blocked or sanctions-matched. The follow-up named "inactive, rejected or merged"; the code says otherwise on two counts. A merge re-homes every duplicate's cards onto the canonical, so the retired duplicate holds none — the real case is a merge onto a canonical that is itself un-payable. And the most dangerous door, a sanctions match, sets `payments_blocked` from `screen_vendor_record`, reached from roughly ten call sites, none of which touched cards.

**Decision.** One helper, `services/vendor_card_revocation.revoke_vendor_cards`, called in the caller's transaction by every door that makes a vendor un-payable (PATCH `status`, `/reject`, `/bulk/status` reject, `/block`, a sanctions match inside `screen_vendor_record`, the consolidation merge), plus an idempotent retry `POST /api/vendors/{id}/cancel-cards` (`vendor.manage` or `vendor.block`; 409 on a payable vendor). "Un-payable" is the card gates' own predicate: `status != "active"` or `payments_blocked`. Per card, with one audit row each:

- live and **unbooked** (no payment, or a `voided` / `failed` / `cancelled` one) → cancelled provider-first through `card_issuance.cancel_card_at_provider`, `card.cancelled`;
- behind a **live payment** → left live, `card.cancel_deferred_to_void`, reported `requires_payment_void`;
- provider did not confirm → left live, `card.cancel_failed` with the outcome tag, reported `not_closed`;
- spent, expired, already cancelled → untouched.

**Why a card behind a live payment is not cancelled.** A run's card leg books the payment `completed` and the invoice `payment_scheduled` at mint. Killing that card alone leaves the books saying paid while the vendor can never be paid — the half-reversal `POST /api/cards/{id}/cancel` is deliberately unwired against (§96), and the reason that route gates on `payment.void` (§132). The payment void closes the card *and* reopens the invoice, under `payment.void`; a vendor-status edit, gated on `vendor.manage` / `vendor.block`, must not become a second door to that duty. This is the same posture an in-flight ACH payment to a deactivated vendor already has: deactivation stops future money, the void handles booked money. The card is never silently skipped — it is audited and reported, and the UI says "void that payment".

**Why a provider failure does not roll back the status change.** Deactivating, rejecting or blocking a vendor is the defensive action. Refusing it because a card processor is down would also keep the vendor payable by the next run — strictly worse. So the write commits and the failure is made visible instead: per-card on the response (`card_revocation` / `card_revocations`), a `card.cancel_failed` audit row, a warning toast on web and a snackbar on mobile, and the retry endpoint (the vendor modal's **Cancel live cards**). `card_cancel_disposition` classifies any unknown outcome as retryable, so a new failure tag can never read as closed.

**Why in the caller's transaction, not `services/post_commit`.** That queue is best-effort by contract — a failing job is logged by class name and dropped — and its work is lost if the process dies after the commit, which is exactly "vendor inactive, card live, nothing recorded". Running inside the transaction matches the payment void's card leg and the sanctions screen the vendor write already awaits: a crash before commit rolls the status change back and leaves at worst "dead at the provider, live in our DB" (every adapter treats re-cancelling a closed card as success). The cost is provider latency on the vendor row's lock, bounded by the adapters' HTTP timeouts and paid only when the vendor holds live cards. Each card row is taken `FOR UPDATE`, so concurrent callers serialize and the second sees `cancelled`.

**Rejected.** Cancelling every live card including payment-backed ones (the books/SoD problems above); auto-voiding the payment behind the card (a `vendor.manage` holder would void payments an org withheld from them); failing the vendor write on any provider error (keeps the vendor payable); a post-commit job (cannot report its failure).

**Residual.** `/cards/generate` reads the vendor's status without a vendor-row lock, so a mint racing a reject can still land a live card on the just-rejected vendor after the revocation query ran; `cancel-cards` recovers it, and the durable fix is a vendor-row lock in the mint path.

Tests: `backend/tests/test_vendor_card_revocation.py`. Docs: `backend/docs/vendor-management.md` § Leaving `active` cancels the vendor's live cards, `backend/docs/virtual-cards.md`.

## 265. A direct card mint locks the invoice and its vendor, checks, mints and commits one invoice at a time (2026-10-08)

`POST /api/cards/generate` read every gate unlocked (payable status, live card,
live payment, blocking exception, vendor status) and only then called the card
provider. The unique index `uq_virtual_cards_one_live_per_invoice` refuses our
ROW, not the provider's card, so it was no defence: two concurrent requests both
passed the live-card check, the first committed, and the second then counted
that card into `reissue_seq` and sent the provider a FRESH idempotency key. The
provider issued a second real card that the index refused to record, so it was
spendable and nothing in FeohLedger governed it. A payment dispatch was no
barrier either: it holds the invoice `FOR NO KEY UPDATE`, which a card INSERT's
`FOR KEY SHARE` does not conflict with.

- **Lock, then check, then mint, per invoice.** `card_issuance.lock_invoices_for_mint`
  takes the vendor `FOR SHARE` and then the invoice `FOR NO KEY UPDATE`, and
  every gate runs after it as a fresh statement. Under READ COMMITTED a request
  that waited sees the winner's card and skips it (`201`, `total: 0`), so the
  provider is called once. Vendor before invoice is `vendor_merge`'s order, so
  there is no cycle with it. The invoice lock is dispatch's own mode
  (`_lock_payment_invoice`), so a mint and a dispatch serialise.
- **The vendor lock is `FOR SHARE`, not `FOR UPDATE`.** It blocks the vendor
  UPDATE that a reject, deactivate, block, bank-change approval or rescreen
  makes until the card commits, so the revocation that runs in that write's
  transaction (§264) sees the card. Two mints for one vendor still run side by
  side, and FK inserts on the vendor are not blocked. `FOR UPDATE` would
  serialise both for no gain.
- **Bounded per §233.** `FEOH_PAYMENT_INVOICE_LOCK_TIMEOUT_MS`, in a savepoint,
  before any provider call. A timed-out invoice is skipped with nothing minted;
  a request that minted nothing and timed out on at least one invoice answers
  `409 invoice_locked`. Retry-safe either way.
- **Commit per invoice.** The provider call happens while the lock is held, as
  in dispatch, so the lock lasts for one provider call rather than an unbounded
  batch. Holding every lock for the whole request would have stalled every
  invoice writer and vendor edit on the batch for N provider calls. Per-invoice
  commits also make each issued card durable at once; before, a failure late in
  a batch rolled back rows for cards that already existed at the provider. Skips
  were already per invoice, so no atomicity was lost.
- **A live payment is a claim, refused like a live card.** `payment_scheduled`
  is payable, so an invoice whose ACH had settled but was not yet `paid` was
  carded on top of it. Without this the lock would only order the mint after the
  payment it should defer to. A pending `virtual_card` run payment is refused
  too: the run mints at execute.
- **Entity-scoped.** The locked SELECT goes through `apply_entity_scope`. The
  route selected by id alone, so a caller in one subsidiary could card another's
  invoice.
- **Not serialised:** an insert that references the invoice (a new
  payment-blocking exception) takes only `FOR KEY SHARE`. Dispatch has the same
  exposure; the main source of a fraud flag, a bank-change approval, updates the
  vendor row and so waits on the vendor lock.

**Rejected.** *Relying on the unique index plus the provider key*: the key is
derived from a count the winner's commit changes, so the loser's key differs.
*Claiming the slot with a placeholder row before the provider call* (so nothing
is locked during the call): it needs a new card state that every live-card
reader would have to learn, for a lock that now lasts one provider call.
*Locking every invoice up front and committing once*: lock lifetime grows with
the batch and has no upper bound.

**The gate is `payment.execute`, not a role list.** Minting a funded card is
the card rail's equivalent of executing a run, but the route was on
`require_roles(admin, ap_manager, cfo)`, so an org that withheld
`payment.execute` from a custom role could not withhold this door. It now gates
on `require_permission(payment.execute)`; every system role the role list
admitted holds that permission by default, so no system-role user loses access.
Pinned in `tests/test_sod_endpoint_wiring.py`.

## 266. Provider credentials are sealed under the app KMS key and write-only (2026-10-08)

**Context.** The `erp`, `payments` and `cards` blocks of `Organization.settings` held live third-party secrets as plain JSONB: Merge.dev keys and account tokens, OAuth client secrets, NetSuite token secrets, processor and issuer API keys, and the three inbound webhook signing secrets. RDS storage encryption was their only protection, and the admin settings page read them back verbatim. `erp-integration.md` called this "encrypted at rest", which overstated it. The QuickBooks connect flow would have added a five-year refresh token with full access to the customer's books.

**Decision.**
- **Envelope encryption in a control-plane table.** `provider_credentials` holds one row per (organization, block). Each row's secrets are one JSON map encrypted with AES-256-GCM under a fresh data key; the data key is wrapped by the app KMS key (`FEOH_CREDENTIAL_KMS_KEY_ID`). The org id and block are both the KMS encryption context and the GCM associated data, so a row moved to another org does not open, and every unwrap is a CloudTrail event naming the org. The table is in the control plane, not per tenant: the values lived on `organizations`, every reader (public webhooks included) resolves the org there, and migration 0110 can copy and strip in one transaction. A tenant table would have needed a tenant migration reading the control plane plus a later strip pass, with no atomicity between them. Tenant DBs share the app's role, so they would have added no isolation.
- **Local-first without a fallback in production.** With the key id unset, the data key is wrapped by an HKDF derivation of `FEOH_SECRET_KEY`, so a laptop and CI need no AWS account. A deployed environment refuses to boot without the key id (as does `alembic`, before 0110 can seal anything), and a value sealed with the local key is refused rather than opened there.
- **One accessor, one writer, no read-back.** Adapters, connection tests and webhook verifiers read through `provider_credentials.provider_config`; a plaintext secret key left in the JSONB is ignored. `PUT /api/organization/credentials/{block}` is the only writer: it seals, then writes `organization.credentials_updated` with path names and refuses with 503 if that row can't be written, then saves. `GET` returns names only. `PATCH /api/organization` refuses secrets in these blocks and records configuration changes to them the same audit-first way (`organization.provider_config_updated`), because the configuration decides where a stored secret is sent. A connection test adds the stored secrets only when the form's configuration equals the saved one.
- **Fail closed when a secret can't be opened.** Webhooks answer a bodyless 503 (our failure, not a verdict on the event). An ERP send lands `failed`, never the mock adapter. A payment-sync leg fails and opens `erp_reconciliation`, never "no ERP", which would mark invoices paid.

**Rejected.** *A column on `organizations`*: no per-row key metadata, and it keeps the secret on the row every request loads. *Per-tenant tables*: see above. *Secrets Manager per tenant*: one secret per tenant per provider adds a network round trip and a cost line with no isolation gain over a context-bound KMS envelope. *Keeping admin read-back*: "leave blank to keep" removes the only need for it, and read-back is what made a stolen admin session a credential dump.

**Not yet covered.** `extraction.api_key` / `aws_secret_access_key` and `sso.client_secret` are still plain JSONB (tracked in `docs/followups.md`).

## 267. Whoever records a goods receipt cannot approve the invoice billed against it (2026-10-08)

**Context.** §262 stopped an implicated person's receipt from *releasing* a
`po_mismatch` hold, and recorded the rest as open: receiving was not one of the
duties segregated from approval, so a manager could count the delivery in and
then sign off the invoice billed against it — the classic three-way-match
control, unenforced. This closes that open item.

**Decision.**

- **The receiver is refused at approval.** `review.approve_invoice` — the one
  door every approval path uses (in-app single and bulk, email / Slack / Teams
  links, mobile, the exception agents) — reads
  `approval_chain.receipt_recorders` and `check_receiving_segregation` raises
  403 `approval_segregation_receiver`. Its own code, not
  `approval_segregation`: that sentence says the approver helped *create* the
  invoice, which a receiver did not, and a refusal has to name the duty that
  collides.
- **"Live hand-entered receipt on the invoice's PO", precisely:** a
  `goods_receipts` row with `source = manual`, a non-NULL
  `recorded_by_user_id`, a status outside `po_matching.CANCELLED_GR_STATUSES`,
  booked in the invoice's own entity, on any PO the invoice's `po_number` names
  under the matcher's lookup (strict entity, vendor when set — every
  same-numbered candidate, not only the newest the matcher reads) or that its
  stored `po_match` names (`po_id`, or the multi-PO split's `po_ids`). A
  cancelled receipt counts nothing towards the 3-way leg, so its recorder
  vouched for nothing; a NULL-source receipt was typed by no app user; a NULL
  recorder names nobody to refuse.
- **Checked after corrections.** A `po_number` correction can re-point the
  invoice at a PO the approver received; the check reads the corrected row and
  the refusal rolls the correction back with it (every door already rolls back
  a refused approval).
- **One opt-out.** The approval step's `require_segregation: false` lifts it,
  as it lifts the creator rule — an org runs one segregation policy.
- **Bulk approve resolves the batch in two queries** (POs by number, then
  receipts) and hands each row its set; the escalation sweep never adds a
  receiver as an approver (`escalation_ineligible`), for the reason it never
  adds an implicated actor.
- **Receivers are not implicated actors.** `implicated_actors` is who shaped
  the payable's terms, and it is what `receipts_clear_hold` measures a
  receipt's recorder against: folded in, every typed receipt would implicate
  its own recorder and no receipt could ever lift a hold. Nor do they reach the
  inter-company mirror (a receiver shapes no terms the mirror copies) or the
  exception queue (§262 already lets an uninvolved receiver's receipt lift a
  `po_mismatch` by itself; refusing the same person by hand would be a refusal
  the machine does not honour).

**Rejected.** *Adding receivers to `segregation_actor_ids` at receipt time*: a
receipt serves every invoice on its PO, including ones not yet received, and
the stamp could never be withdrawn when the receipt is cancelled. *Matching only
the matcher's newest same-numbered PO*: refusing the receiver of a sibling PO
is the safe error.

**The manual-complete amount floor honours it too.** `POST
/invoices/{id}/complete` auto-approves below `auto_approve_below`, which makes
the caller the effective approver; a receiver completing an invoice billed
against their receipt lands it at review, as an implicated caller already did.

**Still open (docs/followups.md).** The reverse order — approve first, then record the receipt that lifts the hold
— needs the approver's id recorded on the invoice so `receipts_clear_hold` can
refuse it. A multi-PO split's `po_ids` survive only until the next
`refresh_warnings` rewrites `po_match`.

## 268. An unattended approval needs the reader to be the one who supplied the document (2026-10-08)

**Context.** A clerk's own upload and extract ran with `suppress_auto_approve`
(§248), but the read that approves can be someone else's: a manager
re-extracting after a clerk swapped the file in got it auto-approved with no
second look. The follow-up proposed suppressing whenever
`segregation_actor_ids` is non-empty. That misses a clerk replacing the file on
their *own* upload — `stamp_entry_editor` never stamps the uploader — and it
left the decision with the dispatcher.

**Decision.** `run_extraction` decides from the row itself
(`extraction.auto_approve_suppression`), so it holds in both dispatch modes,
whoever dispatched. An extraction that would auto-approve lands at
`ready_for_review` when the dispatcher asked for it, when
`segregation_actor_ids` is non-empty (even if the reader is in the set), or when
`uploaded_by_id` names someone other than the actor asking for the read. An
unattended approval is sound only when no employee supplied the document
(email intake, PEPPOL, the portal) or the reader did, and nobody else has
shaped it since. The first look is unlocked, and the download and provider call
sit before the decision while a `pending` invoice is inside the clerk's entry
window, so the deciding read is repeated under `FOR UPDATE` (the file routes'
lock); a `file_key` changed since the download also suppresses. The reason is
recorded as `details.auto_approve_suppressed` on the completion audit row
(`requested_by_caller` / `segregation_actors` / `uploaded_by_another_user` /
`document_replaced_during_read`), so a person being required where the policy
would have approved is on the trail, not only in a log.

**Consequences.** A recurring invoice that is re-extracted never auto-approves
(a person authored its terms, so a different person looks), nor does an
inter-company mirror's. A manager re-reading another manager's upload lands at
review too. The uploader's role is deliberately not looked up: roles change
after the fact, the worker holds no control-plane session, and the cost is a
human review, not a refusal. `/complete`'s amount floor keeps its own rule
(refuse an implicated *caller*), because there the caller acts on figures in
front of them rather than on a document read after the click.

**Rejected.** Suppressing on the segregation set alone (misses the clerk's
own-upload swap); keying on the uploader being entry-only (role drift, a
control-plane read in the worker); refusing file changes while `pending`
(closes the race too, but changes what clerks can do mid-extraction — the lock
re-read closes the approval hole without that).

## 269. Direct ERP adapters for the top ERPs in the US and South Africa, posting by the ERP's own ids (2026-10-08)

**Context.** The user asked for direct sync with the five most-used ERPs in the
US and in South Africa, plus Blackbaud. Roadmap Priority 14 item 2 had planned
enterprise ERPs first (SAP, Oracle Fusion, …). §256 had put everything except
QuickBooks Online and Xero behind Merge.dev on Scale. Separately, the two
existing direct adapters posted bills by vendor name and GL code, which real
ERPs reject (`known-issues.md`).

**Decision.**

- **The set:** QuickBooks Online, Xero, Sage Business Cloud Accounting, Sage
  Intacct, SYSPRO, NetSuite, Business Central and Blackbaud Financial Edge NXT.
  Together they are the US and South Africa top five, with overlap, plus the
  nonprofit ERP the user named. All are direct, so §256 is **amended**: Sage
  Intacct and SYSPRO now come under Growth's `erp_integrations`, like the other
  direct adapters. Merge.dev stays the route, on Scale, for every ERP without a
  direct adapter. The enterprise ERPs remain open roadmap work.
- **Sage needs two adapters.** Sage's v3.1 API does not serve South African
  businesses (its Swagger lists CA/DE/ES/FR/UK/IE/US only). SA runs on
  `accounting.sageone.co.za/api/2.0.0`, which uses an API key plus a Sage login,
  not OAuth. So `sage_accounting` (v3.1, OAuth) and `sage_accounting_za`
  (credentials) are separate adapters. Sage 200 Evolution and desktop Pastel
  run on the customer's own machines and have no cloud API; they are not
  covered.
- **Sage Intacct uses the REST API, not the XML gateway.** REST has been GA
  since 2025, and its client-credentials grant keeps setup to pasted
  credentials. The XML gateway needs a paid sender ID that every customer
  company has to authorise, and its error XML echoes the submitted fields.
- **A bill is posted against the ERP's own vendor and account ids, never a
  name or code text.** These are `InvoicePayload.vendor_erp_id` /
  `gl_account_erp_id`, resolved from the vendor and chart syncs. A missing id is
  refused before any HTTP call with a stable reason
  (`erp_adapters/base.erp_refusal`), and the refusal is **non-retryable**:
  `send_to_erp_internal` fails the invoice on the first attempt. An OAuth ERP
  with no usable connection is final the same way. A provider outage during a
  token refresh still retries.
- **A coded line is never moved.** Only an uncoded line takes the header's
  account. A line coded to an account with no ERP id is refused, because
  posting it on the header's account books the expense somewhere the approver
  never saw.
- **The booked total must equal the approved amount.** ERPs that compute a
  bill's total themselves (Business Central, Xero, Sage, QuickBooks, NetSuite)
  can add tax on top of the approved figure from default tax codes. The adapter reads
  the booked total back. On a mismatch it deletes the draft where the API
  allows, and returns a non-retryable `posted_total_mismatch`. A missing total
  is treated as unconfirmed, never as success.

**Rejected.**
- *Name lookup as a fallback:* it picks the wrong "Acme" the first time two
  vendors share a name.
- *Routing Intacct and SYSPRO through Merge.dev:* Merge's $65 per linked
  account (§256) is more than the Growth price, and SYSPRO is the South African
  market's own ERP.
- *One Sage adapter with a region switch:* the two APIs differ in auth, paths,
  resources and money format.

## 270. One OAuth connect flow for every consent-based ERP (2026-10-08)

**Context.** QuickBooks Online, Xero, Sage Business Cloud Accounting and
Blackbaud SKY have no client-credentials grant. The customer's admin consents in
the provider's own UI, and we then hold a refresh token that the provider
rotates on every use.

**Decision.**

- **One flow, `services/erp_oauth`, plus `OAuthErpAdapter`.** Adapters declare
  an `OAuthProviderSpec` and call `access_token()`. They never read, refresh or
  store a token themselves.
- **One redirect URI** (`/api/erp/oauth/callback`) for every provider app. The
  tenant travels in an HMAC-signed, single-use, short-TTL `state` (Redis
  GETDEL) that binds org, user and provider. The callback re-checks that the
  user is still an active admin, and the redirect home carries only a provider
  key or a stable error code.
- **Each provider supplies its company id its own way:** a callback parameter
  (QuickBooks `realmId`), a token-response field (Blackbaud `environment_id`),
  or one API call (Xero `GET /connections` filtered by the consent's
  `authEventId`; Sage `GET /businesses`). With several candidates it resolves
  to none, and the connect is refused (`no_external_tenant`). We never pick
  one of a customer's ledgers for them.
- **The stored grant records its provider, org and a random `connection_id`.**
  That metadata lives in `settings.erp.oauth`; the access and refresh tokens
  are sealed in `provider_credentials` beside the block's other secrets (§266,
  §272) and never reach an adapter's config. The refresher re-reads both and
  refuses any config whose provider or connection id differs. So a crafted `/test-erp`
  body cannot borrow another tenant's token, and an ERP-type switch cannot hand
  one ERP's grant to another. The block survives an ERP switch: dropping it
  would leave a live grant unrevoked at the provider, and Disconnect is what
  revokes it.
- **Refresh uses a Redis lock per (org, provider) plus a compare-and-swap
  write, not an org row lock across the call.** A row lock would be held across
  a 15-second provider call and stall every settings writer for the tenant. The
  compare-and-swap — a short org row lock, the one every credential writer takes
  first, around reading the sealed refresh token and writing the rotation
  through `provider_credentials.update_secrets` — keeps a newer rotation that
  landed while the lock expired. A rotation writes no audit row (the system
  keeping a grant alive); consent and disconnect are audited first, and a row
  that cannot be written means nothing is linked or cleared (§239's rule).
- **Platform app or the tenant's own.** A tenant may save its own client id
  and (sealed) secret (the setup page's "use your own app"); otherwise the platform's
  `FEOH_ERP_*_CLIENT_ID/_SECRET` apply. Neither fails closed. The refresher
  uses the same source as the consent.

**Rejected.**
- *Per-provider callback routes and token code:* three copies of the riskiest
  code in the integration.
- *Revoking the grant on an ERP-type switch:* a mis-click in the dropdown
  would disconnect the customer's books.

## 271. ERP secrets: the catalogue names them, the sealed store holds them, a destination change drops them (2026-10-09)

**Context.** The ERP setup form is driven by a per-provider catalogue
(`erp_adapters/catalog.py`, `GET /api/organization/erp/providers`), which adds
secrets §266's store did not know: a Sage (ZA) password, SYSPRO operator and
company passwords, a Blackbaud subscription key, and the OAuth tokens (§270).
§266 also kept a stored secret across any configuration save, and let a
connection test use the stored secrets only when the form equalled the saved
configuration exactly.

**Decision.**

- **Every catalogue field marked `secret` is a §266 sealed secret.**
  `provider_credentials.SECRET_FIELDS["erp"]` lists them, and
  `tests/test_erp_catalog.py` fails when the catalogue names one it does not.
  The OAuth tokens are *service-only* paths (`oauth.access_token`,
  `oauth.refresh_token`, `SERVICE_SECRET_FIELDS`): sealed in the same row,
  stripped from the JSONB, never merged into an adapter's config, refused by
  `PUT /api/organization/credentials/erp`, and written only by
  `services/erp_oauth` through the same `update_secrets` writer. A settings
  read shows the OAuth metadata as `{"connected": bool}`, because its
  `connection_id` is the capability the refresher checks.
- **A save that points the block at a different destination drops the sealed
  secrets.** The destination keys are the base URL, tenant, account, company
  and environment (`catalog.DESTINATION_KEYS`; a test fails on a host-naming
  field left out). Same ERP at a new destination: every secret but the inbound
  webhook key, which is never sent anywhere. A different ERP, or the ERP
  cleared: every secret. `PATCH /api/organization` removes them through
  `update_secrets` in the same transaction, audited first as
  `organization.credentials_updated` with `reason: erp_destination_changed`.
  Without this, a stolen admin session could set SYSPRO's `base_url` to its own
  host and receive the stored operator password on the next push. A block that
  selected no ERP yet binds nothing, so secrets entered before the first
  configuration save stay.
- **A connection test uses the stored secrets while the form names the saved
  ERP at the saved destination** (`catalog.same_connection`) — the same
  condition under which a save keeps them. For `erp` this replaces §266's
  exact-equality rule, which made a test fail whenever the form had an unsaved
  non-destination change (a Xero bill status) or omitted the OAuth metadata it
  never renders.
- The OAuth tokens survive an ERP switch and a destination change: they are
  bound to their provider and `connection_id`, go only to that provider's own
  hosts, and are removed by Disconnect, which revokes them first.

**Rejected.** *Keeping secrets across every save* (§266's rule for the other
blocks): one ERP's `client_secret` would become another's app credentials, and
the stored secret would follow any new host. *Binding each secret to a
destination fingerprint inside the envelope*: the same protection with a second
comparison to keep in step with the catalogue, instead of a drop the audit
trail records.

## 272. Per-field ERP encryption inside the JSONB was built, then dropped for §266's sealed store (2026-10-09)

**Context.** The ERP-connections branch (§269–§271) and §266 were built at the
same time. The branch encrypted each ERP secret and OAuth token in place inside
`settings.erp` (`enc:v1:<key-id>:<b64>`, AES-256-GCM, field name as associated
data) under a keyring from sops, `FEOH_CREDENTIAL_ENCRYPTION_KEYS`, with its own
migration 0110, a re-encrypt script and boot guards. §266 landed first.

**Decision.** That mechanism is gone; ERP secrets and tokens live in §266's
store (§271). Three reasons:

- **One store.** Two encryption schemes for the same kind of value would mean
  two rotation procedures, two failure modes and two places a reader can get
  wrong; payment and card secrets were already in §266's.
- **KMS instead of a keyring.** §266's data keys are wrapped by the app KMS
  key, so every unwrap is a CloudTrail event naming the org, and no long-lived
  symmetric key has to be distributed to every host through sops. Its
  encryption context binds the org as well as the block, which the per-field
  scheme did not.
- **One audited writer.** In the JSONB, every settings save rewrote the
  ciphertexts; §266 has exactly one writer, audit-first, which the OAuth
  callback, the refresher, disconnect and the destination-change drop all call.

**Rejected.** *Keeping both, the keyring for ERP and KMS for the rest*: see
"one store". *Shipping the branch's migration as 0111 after 0110*: it would
have encrypted values §266's migration had already moved out of the JSONB.

## 273. A goods receipt pushes received stock to the business's commerce platform (migration 0111, 2026-10-10)

**Context.** §260 keeps stock, valuation and selling out of the product and
brings in a commerce sync instead: the AP side records what was received, the
business's own platform (Shopify, Lightspeed, Square, Cin7, WooCommerce) records
what is on the shelf. Nothing could say WHAT a receipt line was — lines carried
free text only — and five provider adapters were to be built at once, in
parallel, on top of this foundation.

**Decision.**

- **Item identity is a snapshot on the line.** `po_line_items` gains a nullable
  `catalog_item_id` and a `sku` string; receipt lines copy both from the PO line,
  never from the client. The SKU is a snapshot, not a join: a catalog edit must
  not rewrite what an issued PO named, and ERP and contract POs carry item codes
  with no catalog row behind them.
- **The push is planned in the receipt's transaction and run after it commits.**
  One `commerce_inventory_syncs` row per line and direction (`receive` /
  `reverse`), unique on `(gr_line_item_id, direction)`, commits or rolls back
  with the receipt. The provider call happens post-commit
  (`services/post_commit`) with no transaction open: claim by compare-and-swap,
  resolve the sealed credentials, call, record only if still claimed. A receipt
  never fails because the platform is down; its row says so.
- **Not an error, a skip:** a line with no SKU, nothing received, an SKU the
  platform does not know, or no mapped location is `skipped` with a coded
  reason. The last two are retryable by hand once an admin maps them.
- **Cancel takes back exactly what landed.** A synced (or in-flight) receive
  gets a reverse row at the same item and location; anything that never landed
  becomes `cancelled` so no retry can push it afterwards.
- **Idempotent twice.** Our unique row per line and direction, and the row's
  `feoh:<line>:<direction>` key at the provider — every adapter must apply a
  repeated key at most once (the contract helper proves it).
- **Retry:** manual (`POST /api/commerce/syncs/{id}/retry`, the receipt-entry
  roles — it re-sends what the receipt already authorised under the same key)
  and a sweep, off by default per the env conventions, with backoff and an
  attempt cap; it also reclaims claims a dead process left `in_flight`.
- **Fail closed on a moved target.** A row planned for one provider and pushed
  while another is configured fails `provider_changed`; a fraction on a
  whole-unit platform fails `fractional_quantity` rather than being rounded into
  the shelf count.
- **Commerce secrets live in the sealed store (§266, §271).** `commerce` is a
  fourth `provider_credentials` block: every field a provider spec marks secret
  is listed in `SECRET_FIELDS["commerce"]` (a guard test fails otherwise),
  sealed under the app KMS key, written only through `PUT
  /api/organization/credentials/commerce` and never read back. `settings.commerce`
  is configuration, written by `/api/commerce/*` (admin, audited); a save there
  refuses a secret, and one that names another platform or changes a
  destination field drops the sealed secrets through the store's writer — the
  ERP rule of §271, mirrored in `commerce_adapters/catalog` rather than shared
  code, since the two catalogues differ in identity (ERP type + routing method)
  and OAuth handling. The connection test joins the stored secrets only for the
  saved platform at the saved destination (`config_for_connection_test`).
  `PATCH /api/organization` refuses the key. Location mapping is per org with a
  per-entity override; SKU → item resolves through the platform's own lookup
  with an admin override table.
- **A provider is one module.** Each adapter declares its catalogue entry
  (`CommerceProviderSpec`) beside itself and the registry discovers modules with
  `pkgutil`; fake-commerce mounts `providers/<key>.py` the same way, and one
  operator override (`FEOH_COMMERCE_API_BASE_OVERRIDE`, refused in a deployed
  environment) points every adapter at it. A provider's shared edits are its
  secret names in `SECRET_FIELDS["commerce"]` and its sub-processor register rows.
- **Open on every plan**, like the receipts it hangs off; the `mock` platform is
  the local default.

**Rejected.**

- *Pushing inside the receipt transaction:* a provider's latency would hold the
  PO row lock, the reason `post_commit` exists.
- *Best-effort pushes with no record:* a lost push leaves the shelf count wrong
  with nothing to retry from.
- *Per-field encryption inside `settings.commerce`* (the branch's first cut,
  modelled on the ERP design §272 later dropped): one store, one KMS key and one
  audited writer for every provider secret is §266's whole point.
- *A hand-kept adapter import list and a central catalogue* (the ERP family's
  shape): five concurrent provider branches would all edit the same lines.
- *Per-provider `FEOH_*_API_BASE` overrides:* one override per provider is a
  shared config edit per provider; `<override>/<key>` needs none.
- *Building the OAuth consent flow now:* every planned platform has a key-based
  connection, and `services/erp_oauth` is bound to `settings.erp`. The catalogue
  carries `auth: "oauth"` and the panel a Connect hook; generalising the flow is
  tracked in `followups.md`.

## 274. A fresh control plane is built by `create_all` + stamp, not by Alembic (2026-10-18)

**Context.** The first production deploy (#566) ran `alembic upgrade head`
against the new, empty RDS database and then failed in
`migrate_all_tenants.py` on `relation "organizations" does not exist`. The
Alembic chain starts at `0001_signup`, written after the control-plane schema
had always been built by `create_all` (`scripts/seed.py` locally, the pytest
harness in CI), so no revision creates `organizations`, `users` or `roles`.
Against an empty database every revision's `users`-probe answered "tenant",
the tenant-side branches ran, and head was stamped over nothing. Nothing had
caught it because no environment ever started from an empty control plane and
then ran only Alembic. A second defect hid behind it: `migrate_all_tenants.py`
printed `FAILED` for a tenant and still exited 0, so `deploy.sh` would have
rolled new code over a tenant whose migration had failed.

**Decision.**

- **`scripts/init_control_plane.py` runs before `alembic upgrade head`** in
  `deploy/deploy.sh` (and the dormant ECS pipeline and the DR runbook). On an
  empty database it builds the `CONTROL_TABLES` with `create_all`, seeds the four
  system roles, and writes Alembic's version row at head — all in one
  transaction, so an interruption leaves nothing half-built for the next run
  to mistake for an initialised control plane; on an initialised one it does
  nothing. This is the same shape a new tenant already uses (`database.md`
  § Index parity), and it is sound for the same reason: the ORM is the head
  schema, which `test_migration_model_index_parity.py` enforces.
- **System roles are part of the baseline.** `provision_tenant` grants the
  first admin the `admin` role only if the row exists and skips silently if not,
  so a control plane without them would provision admins who can do nothing.
  The role map moved from `seed.py` to `services/control_plane_bootstrap.py`;
  the seed imports it, so the two cannot drift.
- **A database Alembic stamped while empty is refused, not repaired** (exit 2,
  with the two recovery commands). What the tenant-side branches left in it is
  not a known state, and it cannot hold data — recreating it is cheaper and
  certain.
- **`migrate_all_tenants.py` exits non-zero when any tenant fails**, after still
  attempting every tenant.

**Rejected.** A `0000` "create the control plane" revision: the chain is applied
to every tenant too, and a revision that creates control tables must then probe
which kind of database it is in — the same probe that misfired here. Running
`seed.py` in production: it creates demo organizations and users.

## 275. A published release deploys the VM over SSM, through one scoped document (2026-10-18)

The single-VM stack (§254) deploys on a **published GitHub release**:
`.github/workflows/vm-deploy.yml` assumes the OIDC deploy role in the
`production` environment, and sends SSM Run Command to the app VM, which runs
`deploy/deploy.sh --ref <the release's commit>`, waits for it and fails the run
if the deploy fails. Mechanics: `deploy/README.md` § Deploys and
`infra/README.md` § Release deploys.

- **Release-triggered, behind a human.** A release is already the repo's unit
  of "this ships" (`mobile-release.yml` and the disarmed `aws-deploy.yml` hang
  off the same event), and the `production` environment's required reviewer
  gates the job before any AWS call. Prereleases do not deploy. A
  `workflow_dispatch` covers a redeploy or a rollback.
- **SSM, not SSH.** The VM has no port 22 and no key pair (§254); Run Command
  needs neither, and every command is an IAM-authorized, CloudTrail-logged
  event. Opening SSH to GitHub's runner ranges, or keeping a deploy key in
  Actions secrets, were the alternatives — both add a standing credential to an
  internet-facing machine.
- **The role can run one thing.** `infra/deploy.tf` grants `ssm:SendCommand` on
  the app instance and on a custom `feohledger-vm-deploy` document only. SSM
  authorizes SendCommand against both, so `AWS-RunShellScript` — root shell on
  the VM for whoever holds the role — is out of reach. The document's single
  parameter is the commit, constrained by `allowedPattern` to a 40-hex sha, so
  SSM refuses anything else before a shell exists. `ssm:GetCommandInvocation`
  has no resource type and is granted on `*` (read-only);
  `ListCommandInvocations` is not granted at all. The grant is an inline policy
  attached by role name: the baseline module mints the role with a trust policy
  and no permissions and says the project attaches its own, so the trust policy
  stays the baseline's.
- **The release's commit, not main's tip.** `deploy.sh` used to deploy whatever
  `git pull` brought, and between publishing a release and a reviewer approving
  it `main` can move — the deploy would ship commits nobody released. `--ref`
  checks out exactly the tagged commit, refuses one that is not on
  `origin/main` (a release tagged on a branch fails rather than ships
  unreviewed code), and leaves the checkout on `main` so a rollback does not
  strand the next pull. Refusing every release whose tag is not main's tip was
  the simpler option, rejected because it fails any release where `main` moved
  during the approval wait — the normal case on a busy day.
- **Two fixes the automation surfaced.** `deploy.sh` re-runs the copy of itself
  it just checked out (bash otherwise finishes the deploy with the previous
  release's steps), and takes a `flock`, so a release and a manual run from a
  Session Manager shell cannot build and migrate over each other.
- **The run log is public, so the output is redacted.** The workflow prints the
  tail of the deploy log (SSM returns at most 24,000 characters, so the document
  tails a per-run log file kept on the VM), with tenant database names —
  customer slugs — replaced, and with workflow commands stopped around it.

Rejected: arming `aws-deploy.yml` (it targets the ECS build-out, which does not
exist); a self-hosted runner on the VM (a long-lived GitHub credential on the
production box, and any workflow change runs there); polling `main` from a cron
on the VM (deploys unreviewed commits, and no result reaches anyone). The
workflow exercises the same OIDC handshake as `aws-oidc-preflight.yml`, so the
preflight remains the first thing to run (issue #449).

## 276. An admin resets a password to a forced-change temp, never to a value they type (#566, 2026-10-18)

**Context.** `PATCH /api/admin/users/{id}` took a `password` and stored the
admin-typed value with no `must_change_password`, so the admin went on knowing a
working credential for someone else's account — unlike "Invite User", which
issues a generated temp the owner must replace. The Edit modal exposed it as an
optional password input with no `autocomplete`, and browsers autofilled the
ADMIN's own saved password into it: saving an unrelated rename silently changed
the target's password, which is how it surfaced in production (an approver's
sign-in started failing `bad_password`). The same save logged
`user.updated` with `changed_fields: ["full_name", "email", "password"]` though
name and email were untouched — the handler named every field the body carried,
and the form sends every field. And a user's own password change wrote no row of
its own (only the forced logout's `auth.session.revoked`), so the trail could
not say when the approver set their password.

**Decision.**

- **The reset is its own action**: `POST /api/admin/users/{id}/reset-password`,
  `user.manage`, org-scoped, `_authorize_target_mutation`. It generates the temp
  with `utils/passwords.generate_temp_password` (the invite's generator, so it
  meets `validate_password_complexity`), sets `must_change_password`, revokes
  every target session after the commit, mails it best-effort through the
  invite's own path (`_send_credentials_email`, console adapter locally), returns
  it once in the invite's response shape, and audits `user.password_reset` with
  field names and counts only. A revoke failure after the commit is caught,
  not raised: the response is the only place the temp is shown, so a 500 there
  would lock the user out with no audit row. It is reported instead
  (`sessions_revoked: false` on the response, `null` on the audit row, an error
  toast telling the admin to retry).
- **Not on your own account** (`400 admin_password_reset_self`): it would sign
  you out of the tab you made it from and issue a temp for a password you
  already know. Your own path is change-password.
- **`PATCH` refuses `password` with a coded 422** (`admin_password_field_removed`)
  instead of dropping the key. The field stays on `UpdateUserRequest` hidden from
  the schema (`SkipJsonSchema`) only so the handler can see it; pydantic's
  default would silently ignore an unknown key, and an old client must learn the
  reset did not happen, not believe it did. Nothing else sends it — SCIM and
  `/api/v1` have their own schemas, mobile never had the field.
- **`PATCH` applies, and audits, only fields whose value differs.** Email
  compares normalized. The UI also sends only edited fields, but the server rule
  is what keeps the SOX trail honest for any client.
- **A self-service change writes `auth.password.changed`**
  (`{via, forced}`; `forced` = it replaced an issued temp), from
  `/change-password` and `PATCH /auth/me` alike.
- **UI**: no password input in the Edit modal; a two-click **Reset password**
  (`RowAction` armed, the delete confirm's primitive) shows the temp in the
  invite's credentials dialog.

**Rejected.**

- *Keeping the typed field with `autocomplete="new-password"`*: fixes autofill
  only; the admin still chooses and keeps knowing the value, with no forced
  change.
- *Typed value plus `must_change_password`*: the admin still picks it, and a
  typed field is still an autofill target.
- *Silently ignoring `password` on PATCH*: an existing script would report a
  reset that never happened.
- *Comparing values only for the audit row while still writing every field*:
  a resent email would still run the uniqueness check and the trail would be
  right by accident of the second step, not by rule.

## 277. Caddy serves a stable directory; a release goes live by a symlink rename (#579, 2026-10-19)

**Context.** The first release deploy (v0.1.0) left the whole SPA — apex, tenant
hosts, `/legal/*` — answering 404 with an empty body while the API stayed
healthy, and `deploy.sh` failed at `docker compose exec caddy caddy reload` with
runc's "current working directory is outside of container mount namespace root".
`compose.prod.yml` bind-mounted `../frontend/build` at `/srv`; the SvelteKit build
deletes and recreates that directory; a bind mount pins the directory's inode, so
the container kept the deleted original, empty, and its working directory (the
image's `WORKDIR /srv`) was no longer inside the mount namespace. Earlier manual
deploys happened to recreate Caddy (its compose config changed), which
re-resolved the mount and hid the bug. Reproduced locally against the pinned
Caddy image, error text and empty 404 alike.

**Decision.**

- **Caddy mounts `deploy/www/`** (gitignored, never replaced) and serves
  `/srv/current`, a *relative* symlink to `releases/<UTC timestamp>-<commit>`, so
  it resolves the same inside the container. `frontend/build` is only the build's
  staging output; nothing serves it.
- **A release goes live by one rename.** `lib.sh::feoh_publish_frontend` copies
  the build to `releases/<id>` (via a staging name), makes the new link as
  `current.new` and `mv -T`s it over `current` — rename(2), so a request sees the
  old release or the new one and never a gap. Caddy resolves the root per
  request; no reload is needed for the swap. A build with no non-empty
  `index.html` is refused, and a published id is never overwritten.
- **The previous release is kept** as `previous`; rollback is the same rename
  pointed back, with no rebuild. Every other release is pruned (two copies of the
  SPA on a 30 GB disk).
- **Publish after the roll.** The swap happens after `up -d --wait` passes, so a
  failed migration or an unhealthy API leaves the old SPA in front of the old API.
  The one exception is a VM with no live release (a first deploy, the first on
  this layout, or a `current` whose release was deleted — "live" means
  `current/index.html` resolves, not merely that the link exists): nothing
  useful is served from `www/`, so it publishes first and a container the roll
  recreates starts with a site. That is also the whole one-time migration:
  changing Caddy's volumes makes `up` recreate it.
- **No rollback past the layout.** `deploy.sh --ref` to a commit whose
  `compose.prod.yml` does not mount `./www` (v0.1.0 and earlier) is refused in
  `feoh_sync_source`, before the checkout moves: that commit's own deploy.sh
  would run, mount `frontend/build` again, and recreate the outage on its first
  rebuild. Rolling the SPA back is the `previous` swap; rolling the stack back to
  a pre-layout commit is unsupported.
- **The deploy ends with a smoke check** (`feoh_smoke_check`): `curl` from the VM
  to `https://<APP_DOMAIN>/` and `https://<API_DOMAIN>/api/health` with
  `--resolve <host>:443:127.0.0.1` — through the VM's own Caddy under the real
  hostname, so SNI, certificate and site block are the real ones, without
  depending on public DNS or hairpin NAT. 200 with a non-empty body or the deploy
  fails, retried for about a minute for certificate loading. The API's
  healthcheck alone was green throughout the outage.
- **The same inode trap on Caddy's config files.** `Caddyfile` and `tenants.caddy`
  are single-file mounts: a git checkout (which writes a new file) or
  `remove-tenant.sh`'s `mv` left the container reading the old file, and
  `caddy reload` reloaded the stale config and reported success — reproduced
  too. `lib.sh::feoh_caddy_reload`, used by `deploy.sh`, `add-tenant.sh` and
  `remove-tenant.sh`, compares what the container sees with the host files and
  recreates Caddy when they differ, reloading otherwise; `remove-tenant.sh` now
  rewrites the host list in place.

**Rejected.**

- *Restart or recreate Caddy on every deploy*: a few seconds of downtime per
  deploy, and the build still deletes the directory the live container is
  serving — between the delete and the restart the site is empty.
- *Build straight into the mounted directory*: every build is a window where the
  live site is half-written, and a failed build is what users get.
- *Mount `deploy/` as Caddy's config directory* (a directory mount sees renamed
  files): `deploy/` holds the decrypted `.env` and `prod.sops.yaml`, which the
  internet-facing proxy has no business reading. Moving the two config files into
  their own mounted directory would fix it structurally, at the cost of new paths
  in every runbook that names `deploy/tenants.caddy`; the staleness check covers
  the same failure with no path change.
- *An absolute symlink*: it would name the host path, which does not exist inside
  the container.

## 278. The SPA moves to S3 + CloudFront ahead of the rest of the ECS build-out (#582, 2026-10-19)

The static frontend is served from a private S3 bucket through CloudFront
(`infra/frontend.tf`), and the API stays on the VM (§254). The first release
deploy (#579) took every host down. Caddy bind-mounted a directory the deploy
deletes and recreates, and the orphaned mount served 404s. §277 fixes that on
the VM, but the bug class exists only because a web server serves files that a
deploy rebuilds in place. An upload plus an invalidation has no empty moment and
keeps the previous build's files. One wildcard certificate on the distribution
also covers every tenant with no per-host Let's Encrypt step. The trigger
`docs/minimal-deployment.md` gave for this move was global latency. It turned out
to be deploy safety, at roughly $0–1 a month, so the frontend goes now and the
backend's move to ECS keeps its own trigger.

- **The fallback names the files, not "has an extension".** adapter-static
  emits one document (nothing is prerendered), so every route is
  `/index.html`. A CloudFront Function can't ask S3 whether a key exists, and
  the usual stand-in, "no file extension → index.html", 404s any route whose
  last segment contains a dot (an invoice number, a vendor name, an email).
  `infra/cloudfront/spa-router.js` passes `/_app/*`, `/marketing/*` and a list
  of root files through, and rewrites everything else.
  `frontend/src/lib/deploy/spaRouter.test.ts` runs that source against
  `frontend/static/`, so a new root file fails CI until it is listed. 403/404
  custom error responses were rejected because they also turn a genuinely
  missing asset into a 200 HTML page.
- **A missing asset is a 404.** Behind OAC, S3 answers 403 for a missing key
  unless the reader may `ListBucket`. CloudFront gets both, pinned to the
  distribution's ARN.
- **The publish never deletes what an open page loads.**
  `deploy/publish-frontend.sh` uploads hashed `_app/immutable/*` first (a
  year's immutable cache, `--size-only`), then the other static files, then
  `index.html` and `_app/version.json` last with `no-cache`. It invalidates
  `/*` and waits for the invalidation, and only then prunes hashed files that
  are absent from this build and more than 30 days old. The old disarmed
  `aws-deploy.yml` job's `s3 sync --delete` would have removed chunks an open
  tab still lazy-loads. It was deleted, so there is one path to the bucket. A
  custom cache policy (minimum TTL 0) replaces the managed CachingOptimized
  policy, whose 1 s minimum would override `no-cache`.
- **One approval, backend first.** The SPA builds in an ungated job (no AWS,
  no secrets) while the reviewer decides. It publishes inside the same
  `production` job, after the VM step, so a new UI never calls an API that
  hasn't shipped. A second job in the environment that `needs:` the first would
  pause for a second approval. `deploy/smoke-frontend.sh` then checks the edge
  for this build's `version.json`, the SPA on `/` and on a dotted deep link, a
  404 for a missing asset, and HSTS. It adds the apex and a tenant host to the
  check once the apex answers with CloudFront's `x-amz-cf-id`, so it follows
  the DNS cutover without a second flag.
- **DNS moves by one variable, and back by the same one.**
  `frontend_on_cloudfront` (default `false`) turns the apex and `*.` A records
  into aliases and adds AAAA records. `api.` stays on the Elastic IP, and the
  apex's MX/TXT records are untouched. The stack can be applied and tested on
  the distribution's own hostname first, and rolling back is one apply while
  Caddy still serves the SPA. Removing the SPA from the VM is a separate,
  tracked step a day later (`docs/followups.md`), and within ~30 days, because
  Caddy can't renew certificates for hosts that no longer resolve to it.
- **SSE-S3, logs v2, no WAF.** The build is public, so a CMK would only add a
  key-policy grant. CloudFront's legacy logging needs bucket ACLs, which the
  access-logs sink disables (§166), so access logs use CloudWatch log delivery
  instead. A WAF costs ~$5+/month in front of files that are public anyway.
  Both Trivy findings are suppressed inline with those reasons.
- **Vanity domains stay on Caddy** (#582 option (b)). Each needs its own
  certificate, CloudFront would need one per customer, and none is in use yet.
  After the VM cleanup a vanity block proxies the SPA from CloudFront with
  `Host` rewritten to the platform domain, and keeps `/api` on the vanity
  `Host`.

**Rejected.** *Keeping the VM-only fix (§277) and deferring CloudFront to the
ECS build-out*: that leaves the same class of failure one refactor away, and
the Let's Encrypt per-host limit in place. *A separate frontend workflow*: it
would race the VM deploy or need its own approval. *Staging apex and wildcard
separately*: both serve the same build from the same distribution, so a
staggered cutover tests nothing the `*.cloudfront.net` check and the post-cutover
smoke run don't. Instead it leaves a window in which the marketing host and the
tenant hosts serve different builds.

## 279. The landing page loses its pause button: every animation ends within five seconds (2026-10-19)

**Context.** §180 met WCAG 2.2.2 Pause, Stop, Hide on the marketing page with
an in-page **Pause animation** button (`MotionToggle`), because the hero
sequence, the aurora drift, the headline sheen and the adapter marquee all
looped forever. The operator decided the page should carry no such control.

**Decision.** Keep the motion, end it. 2.2.2 applies only to motion that starts
automatically *and* lasts more than five seconds, so every animation on the
public pages now runs a finite number of times with
`delay + duration × count ≤ 5s` and `forwards` fill, resting on a final frame
that shows everything (§180 rule 1 still holds). The hero lift, sweep, ping,
rail and stage dots each run once and have finished by 4.8s; the aurora blobs
drift once over 4.2 / 4.6 / 5s; the sheen makes one 4s pass. The adapter
marquee — a 46s scroll — cannot fit, so it became a static wrapped row, which
also drops the duplicate `aria-hidden` list the seamless loop needed. The
`data-motion` attribute, its `app.css` pause rule and the component are gone.
Reduced motion is unchanged: durations and delays still collapse to nothing.

**Rejected.**
- *Keep the button and shorten nothing.* Conformant, but it is the control the
  operator asked to remove.
- *Rely on `prefers-reduced-motion`.* An OS setting a visitor may not know
  about does not discharge 2.2.2 — the same reason §180 gave.
- *A marquee that scrolls once in five seconds.* Eighteen names crossing the
  viewport in five seconds is unreadable motion, not a list.

**Guards.** `frontend/src/lib/a11y/marketingMotion.test.ts` reads each
marketing component's stylesheet and fails on `infinite` or a literal timing
past the budget; `frontend/tests-e2e/marketing/landing.spec.ts` reads
`getComputedTiming()` for every animation the page actually runs, which
catches the per-element `calc()` delays the static scan cannot resolve. This
supersedes rule 4 of §180; `frontend/docs/ui-patterns.md` § Motion carries the
working form.

## 280. Every email goes out in one HTML layout, derived from the plaintext when a sender writes none (2026-10-19)

**Context.** Only four of the fourteen send sites wrote HTML, and those four
were bare `<p>` fragments under a one-line brand header; the rest — signup
verification, the welcome email carrying the temporary password, invites,
password resets, sign-in codes — went out as plain text. The first real signup
in production showed it: an email a new customer reads first looked like a
log line. Three of the four HTML bodies also interpolated vendor and org names
straight into markup.

**Decided.** `services/email_adapters/layout.py` owns one email-client-safe
document — tables and inline styles, a 600px card, the brand header above, a
muted support footer below, a hidden preheader, `color-scheme` meta plus a
`prefers-color-scheme: dark` override — and `EmailAdapter._branded_html`
applies it to every message, so it is never `None` any more. A message with no
`body_html` gets one derived from `body_text` by shape (a URL-only line is the
button, indented `Label: value` lines a panel, long blocks monospace, the rest
paragraphs). Hand-written bodies are assembled from the same escaping blocks,
and the signup verification and welcome emails now have them: a confirm button
with the expiry and a paste-this-link fallback, and a workspace button over a
set-off credentials panel with a "keep this email" line (added to both parts).
The plaintext part is never rewritten.

**Only a URL the sender built becomes a link.** No text is ever scanned for
URLs. A name, a company name or a rejection reason is free text, and the
signup greeting goes to any address typed into the form: "your account is
locked, sign in at https://evil.example/login" entered as a name would
otherwise be a live link inside a genuine email from us. A hand-written body
passes its own URLs to `email_button` / `email_fallback_link`; a derived body
links only the URLs the sender lists in `EmailMessage.links`, and only when one
stands alone on its line or as a panel value. Every other URL renders as inert
text, the password cell included. Both blocks raise on anything but an absolute
http(s) URL, so no future caller can put `javascript:` behind a button. An
origin allowlist (public URL plus the tenant template) was rejected: white-label
tenants serve their portal from their own domains, which no static list knows,
and an origin check still admits any path on a trusted host. Some mail clients
autolink a bare URL in text by themselves. The layout cannot stop that, and the
plaintext part has always carried the same text.

**The inbox preview is never derived.** The first sentence is exactly where a
sign-in code or a credential sits, so a preheader is fixed copy the sender opts
into. The sign-in-code, password-reset, invite and portal-reset senders set one
that names no code, link or credential; the rest send none, and the client
previews the body as it always did. Filtering the derived line for digits or
URLs was rejected: a temporary password is alphanumeric and a name can hold
anything, so a denylist cannot know what a credential looks like, while an
opt-in has nothing to miss.

**Why derive instead of hand-writing all fourteen.** Ten senders get the
layout without editing them, and the fifteenth gets it by default; hand-writing
each would leave the next new sender plain again. Their texts already have the
shapes the derivation reads, because the plaintext was written for a human.

**Rejected.**
- *A template engine (Jinja) for email.* A dependency and a second escaping
  model for a handful of shapes Python strings already cover.
- *Converting the text with a Markdown renderer.* Plaintext URLs and indented
  key/value lines are not Markdown, and a renderer passes raw HTML through —
  the injection this change closes.
- *A dark-first card.* Gmail and Outlook force a light canvas whatever the
  document asks for, so the card is light with a dark override where honoured.

**Guards.** `backend/tests/test_email_layout.py` pins the derivation rules,
escaping, that a hostile URL in free text and an undeclared URL never become a
link, the http(s)-only href rule, that no preheader is derived and the
sign-in-code and reset emails preview without their code or link, the document
shell, that SES and SMTP attach the HTML part to a text-only message, and fails
on a `body_html = f"<…"` send site; `backend/tests/test_signup.py` checks both
signup emails as the adapter would send them, including a phishing URL typed as
the admin name. `backend/docs/notifications.md` § The HTML layout carries the
working form.

## 281. Free gets the direct ERP integrations; Merge.dev stays on Scale (migration 0112, 2026-10-19)

§253 sold "ERP integrations" from Growth up, and §256 split Merge.dev out for
Scale. The operator moved the line on 2026-10-19: **`erp_integrations`, the
direct adapters, is now on every catalog plan, Free included.** A workspace
holds one `settings.erp`, so on Free that means one direct ERP that imports
(vendors, GL accounts, purchase orders) and pushes approved invoices. That is
the whole direct-ERP feature, not a trimmed version of it.

**Why.**

- **A direct adapter costs nothing per connection.** QuickBooks Online, Xero,
  NetSuite, Business Central, Sage and the rest (§269) call the ERP's own API
  with the tenant's own grant. No third party bills us per linked account, so
  the gate was protecting no cost.
- **It is the test a small business runs before paying.** A business on
  QuickBooks or Xero wants to see an invoice go from capture to approved to *in
  its books*. With the ERP behind Growth, Free could only show the first half,
  and the half it hid is the part that answers "will this fit my accounting".
- **The upgrade lever becomes volume.** Free pauses AI reading at 100 AI-read
  invoices a month (§253, §255). A business that is using the product past that
  point is the one that should pay, and it pays because it is getting more out
  of the product, not to unlock a connection. That fits §253's usage-based model
  better than a feature lock did.
- **Growth keeps its own reasons to buy it:** 500 AI reads with $0.10 overage
  instead of a pause, the public API and outbound webhooks, and SSO.

**Merge.dev stays Scale-only, and is now enforced.** Every Merge connection past
the first three costs at least $65 a month (§256), and Free pays nothing. §256's
`erp_merge` feature had never been built, so giving Free `erp_integrations`
would also have opened Merge to it. This change builds it: `FEATURE_ERP_MERGE`
in `plan_catalog`, granted by Scale alone, and one more check in
`api/deps.ensure_live_erp_entitled`. After `erp_integrations`, a config that
resolves to Merge (`dispatcher.resolve_adapter_key`, the dispatcher's own rule)
needs `erp_merge` too. That covers save, connection test, the three syncs, send,
retry and the completion leg, with §258's downgrade rules unchanged. A config
that names only a `type` is gated as Merge, because the dispatcher defaults
`integration_method` to `merge_dev` and would route it there. The panel swaps
the Merge form for a Scale `PlanUpgradeNotice`. The direct ERPs in the same
select stay open.

**Growth loses the Merge route it could reach until now.** §256 had already
decided that, and nothing grandfathers it. A Growth tenant that saved a Merge
config before this meets §258's downgrade rules. New pushes, retries, syncs and
re-saves of that config are refused. The background legs are left alone so
money is never stranded mid-path: an invoice already dispatched (the queued or
lambda `send_invoice_to_erp`), the ERP webhook, and the `payment_erp_sync`
sync-back still run. Those legs can keep making Merge calls for a stored config,
the same exemption `erp_integrations` already had. The tenant's way forward is
its own: move to a direct adapter (never refused), or to Scale.

**No live plan now reads as Free.** `get_entitlements` used to return `{}` for
an org without a live subscription. That was the same as Free while Free was
`{}`. Left alone, it would now put a tenant the dunning sweep canceled *below*
Free (no ERP at all), while the AI-read meter already reads "no plan" as Free
(`ai_invoice_meter.allowance_for_plan`). It now returns the catalog's Free
grants (`entitlements.NO_PLAN_ENTITLEMENTS`, taken from the code catalog like
the meter's allowance). That is still fail-closed, since it grants only what
every new workspace is given.

**Migration `0112_free_plan_erp`** reaches control planes provisioned before
this. `ensure_plan_catalog` never touches an existing row. Unlike `0107`, which
rewrote the whole column, it merges one key per row: `erp_integrations` into
`free` and `erp_merge` into `scale`. An operator's other edits to a plan's grants
survive, and a key the operator already set (`false` included) is left as it is.
Growth is untouched. It is control-plane only (gated on `plans`) and
idempotent, and the downgrade removes the two keys.

**Rejected:**

- **A Growth trial at signup.** It shows the ERP for 14 days and then takes it
  away just as the books start to depend on it. A downgrade that stops pushes to
  a ledger the customer already uses is the worst moment to hit a gate. A trial
  would also need trial machinery that nothing grants yet (`trial_days` is
  stored and unused).
- **Import-only ERP on Free** (vendors, GL accounts and POs in, no invoice push).
  It splits one feature at the point that matters to the customer: the push is
  the "in my books" step the free plan exists to demonstrate. It would also mean
  a second gate inside the ERP routes, splitting the sync legs from the push legs,
  to protect a cost that is zero.

*Supersedes:* §253's Features row (Free is no longer "core AP" only), §256's
"`erp_integrations` (Growth and Scale)" and "`erp_merge` doesn't exist yet",
and §258's "the `realdb` harness's orgs hold no subscription — which reads
exactly like `free`", which stays true only because no plan now reads as Free.

## 282. A migrated database converges on the ORM, and the whole schema is compared (#575, migration 0113, 2026-10-19)

**Context.** `test_migration_model_index_parity.py` held the chain and the models
to the same indexes, statically. Nothing compared the rest of the schema, so
#575 added `backend/tests/test_schema_build_parity.py`: build a tenant (and a
control plane) by running the whole chain over the pre-Alembic schema, build
another by `create_all`, and diff the full catalogue. The first run found five
kinds of difference — server defaults no model declares, hand-named FKs,
ON DELETE actions, UNIQUE indexes the models declare as constraints, and
columns / indexes / a table the chain never built — and a sixth hiding behind
the defaults (nullability and a varchar width). A third case, replaying the
chain over a `create_all` tenant, found the one that reaches production: a new
tenant is not stamped, so its first deploy runs every revision, and 0029, 0051
and 0082 guarded their FKs by their own names only. A tenant that has been
deployed over since it was provisioned carries 14 duplicate foreign keys.

**Decision.**

- **The ORM is the head schema; a revision moves the migrated side to it.**
  §274 already builds a fresh control plane on that claim, and every production
  database was built by `create_all`, so this direction changes nothing there
  but the duplicates. `0113_schema_build_parity` drops the 142 defaults (each
  column has a Python `default=` the ORM always sends, so a raw INSERT that
  leaned on one already failed in production), renames the hand-named FKs or
  drops the duplicate where both exist, re-creates four FKs without the ON
  DELETE action the models do not declare, promotes four UNIQUE indexes to
  constraints in place (`USING INDEX`), and adds what the chain never built.
  Its `downgrade()` is a no-op: the two builds differed before it, so there is
  no single prior schema to return to.
- **The three guards are fixed at the source too**, accepting either name, so a
  replay no longer creates the duplicates 0113 then removes.
- **The test is opt-out.** A new difference fails it; an allowed one needs an
  `ALLOWED` entry with its reason, and a stale entry fails. The pre-Alembic
  schema is a committed snapshot (`tests/fixtures/pre_0001_schema/`) with the
  script that re-derives it from `f6d93ab1^`, because CI checkouts are shallow.

- **A tenant provisioned before the head stamp is replayed, not stamped.**
  `services/schema_version.record_head` now records a new tenant at head in
  the transaction that builds it (#575's day-one work), so the deploy after
  provisioning is a no-op. Tenants provisioned earlier carry no version row
  (one in production today), and `migrate_all_tenants.py` names each such
  tenant in its log, then replays the chain over it. Stamping it instead
  would be wrong: `create_all` built it at the head of the release that
  provisioned it, not today's head, so a stamp would skip every revision
  since. With the three guards fixed, the replay changes nothing a revision
  would not have changed and adds no duplicate FK
  (`test_a_tenant_provisioned_before_the_stamp_is_replayed_safely`). Tenants
  whose replay already ran (two in production, at 0111, each with the 14
  duplicates) lose the duplicates to 0113 on their next deploy.

**Rejected.**
- *Leaving the duplicate FKs in place as harmless.* They enforce the same
  reference, but each is checked again on every write to 11 business tables,
  and a later revision that drops or alters one by name would leave its twin.
- *Declaring the 142 defaults on the models instead.* The defensible
  direction for a fresh design, but it changes every production table to match
  development databases, where the other way changes none.
- *Stamping an unversioned existing tenant at today's head in
  `migrate_all_tenants.py`.* See above: its schema is the head of its own
  release, which the parity check cannot vouch for as today's.

## 283. Signup sends a single-use set-password link, never a temporary password (2026-10-19)

**Context.** `POST /api/signup/complete` provisioned the new admin with a
generated 16-character temporary password and `must_change_password`, and the
welcome email printed it in a credentials panel (§280). Two problems. A
working password sat in the inbox indefinitely: it stayed valid until somebody
signed in, and an admin who never did left a live credential in a mailbox for
months. And the user had to copy it out of the email by hand — an email cannot
carry a copy button — then type it into a sign-in form only to be made to
replace it. Supersedes the temporary-password part of the signup design
(`docs/self-service-signup.md` before this change, and the "keep this email"
panel §280 added); the rest of §280 (one layout, only server-built URLs are
links, fixed preheaders) stands and this email follows it.

**Decided.** The signup admin is provisioned with **no password**
(`provision_tenant(admin_password=None)` → `hashed_password` NULL, the
representation JIT- and SCIM-provisioned users already have; `/auth/login`
refuses the account until a password exists) and `must_change_password=false`,
since the password they set is their own. `/complete` then mints a
**set-password link**, which is a forgot-password reset token and nothing
else: `password_reset.issue_reset_token` (same Redis store, same keyed PBKDF2
digest, same single-use `GETDEL` redemption through `POST
/api/auth/reset-password`, same session revocation on use), built by
`utils/tenant_urls.password_reset_url` — the forgot-password email's own
builder, now shared — with `mode=set`, which changes only the reset page's
copy. A second token type, store or redemption endpoint was rejected: it would
be a second implementation of exactly the guarantees the reset token already
has tests for.

**Lifetime: 72 hours, `FEOH_SIGNUP_SET_PASSWORD_TTL_HOURS`.** The reset
token's 30 minutes (`FEOH_PASSWORD_RESET_TTL_MINUTES`) is sized for someone who
just clicked "Forgot password" and is waiting for the email. This link is
minted the moment a workspace exists, for someone who may close the tab and
come back on Monday: 72 hours covers a weekend and is three times the
verification link before it. It is still strictly better than what it
replaces — a temporary password had no expiry at all — and it dies on first
use. Configurable because reset TTL already is; minimum 1.

**The link goes to the browser as well as the inbox.** `/complete` returns
`set_password_url`, and `/verify` shows a **Set your password** button
straight to it. Returning a credential to the browser is acceptable here
because of who is holding the browser: `/verify` is only reached through the
verification link mailed to that same address, so the caller has just proved
they can read the inbox the welcome email is going to. The response discloses
nothing the email does not. It is **one token for both copies**, so whichever
is used first spends the other — two tokens would leave a second live link in
the inbox after the admin had already set their password in the browser.

**A link scanner cannot spend it.** Corporate mail security (Safe Links,
Proofpoint, Mimecast) fetches every link in an inbound email before the user
sees it. The set-password page is a static SPA route whose GET touches no API,
and the page's own load calls `POST /api/auth/reset-password/check`, which
reads the token without consuming it (`peek_reset_token`) — so neither a
scanner's GET nor a reload invalidates anything. Only submitting a new
password does. The check answers the same opaque 400 for every token the
reset would refuse, and otherwise returns the account email — which only the
token's holder learns, for the account their link already resets — so it is
no better an oracle than redemption, has its own per-IP budget, and is a POST
so the token never rides an API query string. Its two uses: the page says
"this link has expired" before anyone types a password, and it fills a hidden
`autocomplete="username"` field (as `/change-password` does) so a password
manager files the new password under the right account.

**When there is no link.** With no tenant base URL configured, `/complete`
answers `503` **before** provisioning and leaves the verification unconsumed:
the admin would have no password, and both the set-password link and
forgot-password need that URL to build a link, so the workspace would exist
with no way into it. A Redis failure while minting yields no link rather than
a 500, because by then the tenant exists and the verification is spent, so an
error would strand a workspace the visitor cannot retry into; the email and
the verify page then name **Forgot password**, which works for the account
like any other — it needs only an active user, not an existing password. That
is also the documented way out of an expired link.

**What the browser copy costs, and what bounds it.** Putting the credential in
the `/complete` response means whoever opens the verification link first holds
the admin's set-password link — before this, a leaked verify link could only
create the workspace, since the credential went to the mailbox. The holders
that matter are a JS-rendering mail scanner that executes `/verify` (it would
also have spent the verification, so the real user would see "invalid or
expired" and sign up again — a scanner does not use what it fetched), and an
access log that keeps the `/verify?token=` query string. Both are bounded: the
verification token is single-use (`FOR UPDATE` + `consumed_at`) and expires in
24 hours, so a logged copy is dead once the owner has clicked it; the response
is sent `Cache-Control: no-store`; and issuing the set-password token writes
the same PII-free `auth.password_reset.requested` audit row a forgot-password
request does (`via: signup`), beside the `auth.password_reset.completed` row
its redemption writes. Dropping the browser copy ("check your email") was the
alternative; the operator chose the one-click path (2026-10-19), and the
welcome email remains the fallback either way.

**The welcome email** has the button, its paste-this fallback and the expiry
("expires in N hours and works once; if it has expired, use Forgot password on
the sign-in page"), the workspace URL and email as plain panel values (no
password row, no monospace), and a sign-in link with the address prefilled
(`tenant_urls.sign_in_url`, read by the login page's new `?email=` prefill —
a plausible single address only, bound as the field's value, no autofocus).
The preheader stays the fixed "Your workspace is live."; neither the token nor
the link is in it or in any application log line (the dev-only `console`
adapter prints whole messages by design, and is how the local flow reads the
link).

**Not changed here.** The other flows that still mail or return a temporary
password — admin create user, admin password reset (§276), supplier-portal
invite and portal reset, partner child provisioning — are listed in
`docs/followups.md` with the same durable fix.

**Guards.** `backend/tests/test_signup.py`: `/complete` provisions with no
password and returns a live token for the provisioned user; the welcome email
has the link as button and fallback, the configured expiry, plain panel values,
the prefilled sign-in link, no password anywhere and a fixed preheader, in
every locale; without a tenant URL `/complete` 503s before provisioning,
minting or consuming anything, and a token-store failure still answers 200;
the response is `no-store` and the issuance audited; the token never reaches a
log line, even when the welcome email fails;
end to end against the real reset endpoints, the account refuses login before
a password exists, repeated checks leave the token usable, one submit sets the
password, and the token is then dead to both check and reset; forgot-password
works for a never-had-a-password admin; an evicted token is refused by both.
`test_forgot_reset_password.py`: the check refuses a since-deactivated account
without spending the token, and has its own rate budget; `test_tenant_url_resolver.py`
pins both URL builders.
`test_tenant_provisioning.py` pins the passwordless admin row;
`frontend/tests-e2e/auth/forgot-password.spec.ts` the reset page's up-front
refusal, that loading or reloading the page never redeems the token, that a
failed check (429, outage) is retryable rather than "expired", set mode, the
hidden username field and the prefilled sign-in link;
`tests-e2e/auth/login.spec.ts` the prefill; `tests-e2e/signup/` and
`tests-e2e/email/signup-email.spec.ts` the whole journey through Mailpit.

## 284. GL accounts and vendors link by the ERP's unique key, within the ERP company that issued it; a push refusal is a code the reader can act on (migrations 0114, 0115, 2026-10-19)

**Found live.** Against the real QuickBooks Online sandbox, a first chart sync
into an empty chart reported 89 accounts as "81 new, 8 updated", and an invoice
coded "Job Materials" — shown under "Job Expenses" in our picker — was posted as
a Bill to "Landscaping Services:Job Materials". Two defects stacked:

1. `quickbooks_online._account_code` keyed an account by `AcctNum or Name`.
   QuickBooks keeps a `Name` unique only among its siblings; the sandbox repeats
   eight sub-account names under two parents. Only `FullyQualifiedName` (the
   colon-joined path) is unique, and `AcctNum` when numbering is on.
2. `POST /api/gl-accounts/sync-erp` upserted by code, with a per-run `pending`
   dict by code, so the second account with a code overwrote the first row's
   `erp_account_id` and counted as "updated". The push resolves a code to that
   id, so the bookkeeper's choice was silently replaced by whichever account the
   sync read last.

**The rule now: a row is linked to an ERP account only by a key the ERP keeps
unique, and nothing ambiguous is linked at all** (`services/gl_account_sync`,
backend/docs/erp-integration.md § GL accounts and vendors are linked by the
ERP's unique key):

- match by `erp_account_id` first (an ERP rename renames our row), then by
  code; a row linked to another live account keeps its link and the newcomer is
  refused (`code_taken`); a dead link (an id the pull no longer contains) is
  re-pointed by code. That alone does NOT make a sandbox → production move
  safe: QuickBooks ids collide across companies, which is the next section;
- a code two pulled accounts resolve to is refused for **both**
  (`duplicate_code`) and an existing row with it loses its link — reported, never
  counted as updated;
- every adapter's `code` was audited against its provider's uniqueness rule
  (the table in the doc). QuickBooks now uses `AcctNum`, else the path; Merge.dev
  falls back to the Merge `id`, never the `name`. The others already used a
  unique key (Xero `Code`, BC No., NetSuite `acctnumber`/`fullname`, Sage nominal
  code / ID, Intacct ACCOUNTNO, SYSPRO `GlCode`, FE NXT account number).
- **Vendors**: the name fallback in `vendor_sync` links a manually created
  vendor only when the name is unique among the pulled records *and* among the
  unlinked locals; otherwise it is reported (`ambiguous_name`) and nothing is
  linked or created. It used `scalar_one_or_none`, so two local vendors with one
  name was a 500 as well as a guess. QuickBooks `Vendor.AcctNum` stopped feeding
  our vendor `code`: it is the company's account number *at* the vendor.

**An ERP id means something only in the company that issued it (migration
0115).** Review of the first cut found that the matching above trusted an id
across companies. QuickBooks numbers accounts and vendors per company with small
integers, so a sandbox and a production company share most ids: our "Office
Supplies", linked to sandbox "7", posted to production "7" (Advertising) after a
reconnect, and the next sync matched the row by that colliding id, renamed it
"Advertising", and recorded no link change. `vendor_sync` did the same and
overwrote the vendor's name. So:

- **every link records its connection realm** — `gl_accounts.erp_link_realm`,
  `vendors.erp_link_realm` (`services/erp_link_realm`), `"<adapter>:<company>"`
  built from NON-secret configuration only: the OAuth consent's company id
  (QuickBooks `realmId`, Xero tenant, Sage business, Blackbaud environment), or a
  direct adapter's company fields (NetSuite account; BC tenant + environment +
  company; Intacct company; SYSPRO / Sage ZA host + company). Merge.dev's ids
  are Merge-assigned UUIDs, unique across linked accounts, so its realm is the
  adapter alone. `mock`, and an OAuth ERP with no consent, have none (`None`):
  nothing can post there, so nothing is filtered. A secret is never fingerprinted
  into a realm: it would land on every row and every audit row, and rotating it
  would read as a company change.
- **the push resolves only links of the current realm** (`services/erp
  ._resolve_erp_refs`, `gl_chart.resolve_erp_account_ids`). A link from another
  company is "not linked", and the bill is refused `vendor_not_linked` /
  `account_not_linked` before any HTTP call.
- **a sync re-judges every link of another realm by evidence, and records the
  outcome** (rule 0 in `gl_account_sync`, the realm pass in `vendor_sync`). A
  link is kept only when the pull shows the SAME id holding the row's own code
  (GL: its current code or the legacy key an older adapter gave it) or, for a
  vendor, the same supplier (same name ignoring case and spacing, or the same
  non-blank tax id). Then the id names what the row meant in the new company too
  — a production company copied from the sandbox keeps its links — and the move
  is recorded (`link_changes` with `from_realm` / `to_realm`; `vendor.synced_from
  _erp` `change: realm_changed`). Anything else is dead: dropped, never matched
  by the colliding id, re-matched by code (GL) or by the unambiguous-name rule
  (vendors), and every change written to `link_changes` / the per-vendor audit
  row (`change: unlinked`). Rejected: re-matching purely by code and ignoring the
  old id — it would churn every pre-§284 sub-account row a single-company tenant
  has, whose code is the legacy key, not the current one.
- **the connect callback audits a change of company**
  (`organization.erp_realm_changed`, beside `organization.erp_connected`, both
  naming the realms). Informational: the per-row realm is what keeps old links
  from posting, so a reconnect after a disconnect — which leaves no previous
  company to compare — needs no flag to be safe.

**Links made before 0115 carry no realm (NULL), and nothing recorded which
company they came from.** Two wrong answers: treat them all as dead (every
existing tenant's pushes refused until someone syncs — an automatic
approve-then-push flow strands every invoice), or adopt them into whatever the
tenant is connected to at the first sync (a tenant that reconnected to another
company first would have its colliding links blessed — the defect itself). The
chosen rule: **the migration records, per organization, the realm its ERP
config names at that moment** (`settings.erp.legacy_link_realm`, control-plane
step of 0115; absent when no company is nameable, **and never written for a
QuickBooks Online org**). A QuickBooks org's NULL links are exactly the ones
the old name-keyed chart sync made, and a sub-account name repeated under two
parents was merged into one row linked to whichever account the sync read last
(eight rows in the sandbox). Trusting them on the snapshot would keep those
rows posting to the wrong account until someone re-ran the sync. So for
QuickBooks the push refuses every pre-0115 link (`account_not_linked` /
`vendor_not_linked`) until the first chart sync and vendor sync adopt it by
evidence and stamp its realm. That holds even on the company the tenant never
left: refusing the push is the cost of not posting a bill to the wrong account.
Merge.dev orgs are still stamped: their links were always keyed by the Merge
record `id` (the name was only a fallback for an id-less record, which Merge
does not send). The push trusts a NULL link only while the org is still
connected to that realm — so a tenant **on any other ERP** that never changed
company keeps posting with no action, and one that connects anywhere else from
then on has its unverified links refused until a sync re-judges them. The syncs
never trust a NULL link on the snapshot alone: they apply the same evidence
rule as for any other realm and stamp the realm (`realm_adopted` counts the
silent confirmations). The snapshot is written only by the migration;
`catalog.merge_erp_update` never takes it from a settings save and drops it with
the block when the ERP changes. Residual, accepted: a tenant that switched
company *before* 0115 and has not synced since is indistinguishable, at the push,
from one that never switched; its first sync after 0115 sorts its links by
evidence. Pre-0115 history is not recoverable from any record. The migration
computes the realm with a frozen copy of `connection_realm`, not by importing
it: a revision must write the same thing on every replay (§282).
`tests/test_migration_0115_erp_link_realm.py` pins the copy to the live
function, and a failure there after 0115 has shipped means the realm format
changed and needs its own revision.

**The repair passes cover the whole organization.** Rules 0, 3 and 4 are about
which ERP account an id or a code names, not about which subsidiary holds the
row, so they run over every chart row whichever entity the sync was run under
(they used to run over shared ∪ the selected entity only, leaving a merged row
in another subsidiary's chart posting until someone synced under it). Matching
and creating stay scoped to the chart being synced into.

**A person settles what the rules will not guess.** The live sandbox showed the
hole in "unlink, never guess": the old row "Depreciation" was correctly unlinked
(the top-level account 40 and "Truck:Depreciation" 39 were both keyed
"Depreciation" before §284), but the top-level account's current code IS
"Depreciation", so rule 2 found that same row, judged it legacy-ambiguous and
refused — on every sync, forever. A bookkeeper could not code a bill to
top-level Depreciation at all. Now a row left unlinked by rule 3 or 4 keeps the
ERP accounts it could mean (`gl_accounts.erp_link_candidates`, with the realm
they came from); `/gl-accounts` shows it as "Needs a decision"; and an admin or
AP manager picks one (`POST /api/gl-accounts/{id}/erp-link`, audited
`gl_account.erp_link_confirmed`). Only an offered id under the same connection
is accepted, and only on an unlinked row. A confirmed link
(`erp_link_confirmed_at` / `_by`) is exempt from rules 3 and 4 — it is the
answer to the question they ask — but still dies with its realm or when its
account leaves the ERP. Nothing auto-picks: the invoices coded to the row are
the evidence, and only a person can read them. If the person picks
"Truck:Depreciation" for the old row, the top-level account cannot also own the
code "Depreciation" in the same chart and stays `code_taken`; that is the
consequence of their answer, and the doc says so. Considered and rejected:
persisting the whole last sync report (it goes stale with every edit; the
per-row candidates are refreshed against each pull and cleared on link).

**Inactive records are read, marked and never created.** `select * from
Account` returns only active QuickBooks accounts, so an account made inactive
vanished from the pull: a row linked to it looked like a dead link and our chart
kept it active (live: Bank Charges, id 8). The adapter now queries `where Active
IN (true, false)` for accounts and vendors (QuickBooks' documented form), and
every adapter emits inactive records with `active=False` (NetSuite `isinactive`,
BC `blocked`) instead of dropping them. Their ids count as live; a matched row
is marked inactive in our chart (reported as `deactivated`), no row is created
for one, and a sync never reactivates — a retirement here may be our own
decision. A vendor linked to an inactive ERP record becomes `inactive` if it was
`active` (live: Squeaky Kleen Car Wash, 57); no payee is created or linked for
one. Whether the ERP accepts a bill against an account it made inactive stays
the ERP's call, as for any retired account (`resolve_erp_account_ids`).

**Smaller holes closed in the same pass.** An account the ERP sends with no id
is refused (`missing_erp_id`) before the dedupe, which used to collapse two
id-less accounts into one and count "linked" for writing a NULL. NetSuite and
BC no longer drop over-long codes themselves: they emit them, the sync refuses
them `code_too_long`, and their ids stay live for a row already linked to one.
Vendors the pre-§284 QuickBooks mapping created still carried `Vendor.AcctNum`
in `code`, which CSV import resolves vendors by: the sync clears `code` where it
equals the record's AcctNum on a vendor it created (`source = erp_sync`; a
manual vendor's code may be a person's choice), audited without the old value.
An `account_not_linked` refusal no longer names the header code when every line
carries its own (the header is posted nowhere then). And the QuickBooks PO sync
stopped reading the whole chart to resolve PO-line accounts: `po_line_items`
has no column for one and nothing reads `PoLinePayload.gl_account`, so the
call bought discarded data. Lines carry no GL (never the `AccountRef` name);
persist it by id when a consumer needs it.

**The code is never rewritten on an id match.** The obvious way to repair a
collapsed row is to re-key it to the account's real code. Rejected: invoices,
lines, recurring templates and contract lines record the GL as a **string**
(`schemas/gl_account.GLAccountUpdate` makes `code` immutable for exactly this
reason, and followups (c) tracks the FK that would lift it), so re-keying
silently detaches every line coded to it. A drifted code is kept, stays
correctly linked by id, and is reported (`code_drift`).

**Repair of collapsed rows is an unlink, not a guess.** A pre-fix row "Job
Materials" linked to Landscaping's id cannot say which account its invoices
meant (our picker showed it under Job Expenses). `GLAccountPayload.legacy_codes`
carries the key an older adapter used (QuickBooks' bare `Name`); a row whose
code is the legacy key of more than one pulled account, and which no longer
matches its linked account's current code, loses its link. Its invoices are then
refused `account_not_linked`, naming the code, before any HTTP call, and each
real account gets its own row. Only legacy keys count as evidence of a merge: a
current code another account holds is a rename (rule 2's `code_taken`), and
unlinking there would have re-pointed the old row's invoices at the newcomer.
Residual, accepted: a collapsed row whose link landed on the account whose new
code equals the old one (a top-level account named like the sub-accounts) looks
exactly like a correct row and keeps its link.

**Code length: widen to 100, the width that records a code.** QuickBooks paths
pass 50 in the sandbox itself ("Landscaping Services:Job Materials:Decks and
Patios" is 51). Considered:

- *a short stable form within 50* (truncate + id): unreadable for ordinary
  sub-accounts, in the one field a line picker shows;
- *skip over-long accounts*, as NetSuite and BC did: a bookkeeper cannot code to
  a real account at all;
- *widen past 100*: every column that records a code (`invoices.gl_account`,
  `invoice_line_items.gl_account`, recurring template and contract lines) is
  `String(100)`, so a longer chart code could not be coded anywhere, and widening
  those means touching the eight services that parse them.

So `GL_CODE_MAX_LENGTH = 100` (migration `0114_gl_code_width`, tenant DBs,
catalog-only `ALTER … TYPE varchar(100)`, downgrade refuses if a value would
truncate), and the rare QuickBooks path past 100 keeps its head and ends
`…#<Id>`, unique because the id is. NetSuite and BC emit an over-long code
as it is (they first skipped it, which hid a live id from the sync). The sync
refuses anything longer (`code_too_long`) rather than let the database raise.

**Push refusals are coded end to end.** The live test also showed the AP user
the raw "QuickBooks Online post refused: vendor_not_linked" in the invoice
modal: English-only, and a reason code is not an instruction. `ErpPostResult`
now carries `refusal_code` / `refusal_provider`; `services/erp` records
`refusal = {code, params}` beside `error` on `invoice.erp_failed` (`provider`,
plus `accounts` for `account_not_linked`); and `api/erpRefusals.ts` states each
of the registry's codes (`base.ERP_REFUSAL_CODES`) in six locales with what to
do. Two guards keep it complete: a backend scan that every reason constant under
`erp_adapters/` is registered, and a vitest that reads the registry out of
`base.py`. The code lives on the audit row rather than a new invoice column
because that row is already where the panel reads the failure from, and it is
append-only — the refusal a user saw is the refusal that was recorded.

## 285. QuickBooks refusals are coded from Intuit's numeric Fault codes, and a deterministic 4xx is never retried (2026-10-19)

**Context.** A live run against the Intuit sandbox (2026-10-19) posted three
bills QuickBooks refused for three different reasons — dated into books closed
through 2026-09-30 (closing set to "warning only" in the UI; the API refused
anyway), coded to an account made inactive, and for a vendor made inactive. All
three reached the AP user as the same "QuickBooks Online post failed: HTTP 400
(invalid_request)", and each was sent three times over six seconds before
failing. The adapter deliberately never read the body (`base.erp_failure_reason`: Intuit's
`Fault` quotes the submitted bill back, and the message lands on an append-only,
shipped audit row).

**Decision.** Read the Fault, but only its numbers. `_fault_errors` keeps each
error's numeric `code` and an `element` that is on a fixed allowlist of Bill
field names — never `Message` / `Detail`. Known codes map to refusals
(`period_closed`, `duplicate_document_number`, `company_subscription_inactive`);
the codes that mean "a reference is bad" (2500, 610, and the catch-all 6000) are
resolved by reading the bill's own vendor and accounts back by id, so the
refusal says WHICH (`vendor_inactive`, or `account_inactive` naming our GL
codes); anything else is `intuit_fault` with the number, so the next unknown is
diagnosable from the row instead of needing another live run. All are
non-retryable. Separately, `base.erp_http_failure` makes every adapter's HTTP
failure retryable only when the status can change on a re-send (5xx, 401, 408,
409, 423, 425, 429).

That includes Fault 6140 (`duplicate_document_number`, another bill already
carries the DocNumber). `_find_existing` looks for our own earlier bill before
every create, so a 6140 is normally someone else's bill; but if QuickBooks'
query lags and the lookup misses our own, the invoice lands at `failed` instead
of finding it on a later attempt. That is the safe direction (nothing is posted
twice), and a manual retry recovers once the query catches up.

**Rejected.** *Reading `Detail` and redacting it* — the redaction would have to
know every field Intuit might echo, and a miss is permanent on a WORM row.
*Guessing account vs vendor from the code alone* — 2500 covers both, and the
read-back is two or three GETs on a path that has already failed. *Retrying
409 / 423 as deterministic too* — Business Central's stale-etag answer is 409
and succeeds on the re-read, so treating every 4xx alike would turn a
concurrency race into a hard failure.

## 286. An open duplicate or fraud exception refuses the ERP push, not only the payment run (migration 0116, 2026-10-19)

**Found live.** In a QuickBooks Online test a clerk entered a second invoice
with the vendor and number of one already posted. The app raised the
`duplicate_invoice_number` warning and an open `duplicate` exception, a member
of `PAYMENT_BLOCKING_EXCEPTION_TYPES`, so our payment runs would not pay it.
Approval gates on none of those types (§169), so an admin approved it, and
"Send to ERP" posted it as a second Bill. A posted bill can be paid from inside
the ERP (QuickBooks "Mark as paid", Bill Pay), so the duplicate liability now
sat where our payment-run control does not reach.

**The rule:** the push refuses while the invoice has an open or escalated
exception of a type that says the liability itself is wrong.
`api/payments.ERP_EXPORT_BLOCKING_EXCEPTION_TYPES`, defined once beside the
payment set and asserted to be a subset of it:

| Type | Refuses the push? | Why |
|---|---|---|
| `duplicate` | yes | the obligation is already booked; a second bill is a second liability for one debt |
| `fraud_flag` | yes | the obligation may not exist, or not be owed to this payee |
| `line_total_mismatch` | no | the approver signed the header amount, which is never recomputed from lines and is the total the bill is posted and read back at (`posted_total`). The hold is on paying until the lines are reconciled |
| `payment_reconciliation` | no | raised on a payment, after the bill exists; the liability is real and the question is whether a rail already holds an order |
| `quality_hold` | no | goods received and awaiting inspection are still owed; ERP practice is to book the bill, hold payment, and credit any return |
| `po_mismatch` | no | billing over the PO or ahead of receipt is the ERP's own 3-way-match hold; the vendor's claim is still recorded |

For the four "no" types a bookkeeper paying from the ERP pays a real debt,
perhaps early; for the two "yes" types they pay money that is not owed. A
`fraud_flag` at `info` severity (a round amount) refuses too, because it is the
same row that blocks the payment run: one row, one meaning, and a person clears
it. So does the `fraud_flag` an approved vendor bank change opens on every
approved, unpaid invoice for that vendor (`api/vendors._BANK_CHANGE_FLAG_STATUSES`,
derived from `VALID_TRANSITIONS`: every status on a forward path from
`approved` to `paid` — `approved`, `sending_to_erp`, `sent_to_erp`, `failed`,
`posted_in_erp`, `payment_scheduled` — with `failed` counted only for an
invoice that was ever approved), although it is about the payment destination
rather than the debt: the change may itself be the BEC, and a bill in the ERP
is paid against the ERP's own vendor record, which the same request may have
changed too. Every approved bill for that vendor waits for a second person, and
the fail-closed cost is that wait. A `paid` invoice is out of reach when the change lands, so voiding its
payment re-raises the flag if a bank change was approved after that payment
was made (`api/vendors.reflag_voided_invoice_for_bank_change`, called from
`POST /api/payments/{id}/void`), stamped with that change's approver; the
re-payment would otherwise go to the new account unreviewed.

**One chokepoint, plus a synchronous answer.** `services/erp._call_erp` asks
`services/erp_export_gate.erp_export_blocking_exception` before it builds an
adapter. Every push reaches it: `POST /complete` (the modal's "Send to ERP"),
`/send-to-erp`, `/retry-erp`, the local dispatcher and the Lambda worker, all
through `send_to_erp_internal`. It re-reads on each attempt, so a flag raised
after the push was queued still stops it, and it raises `ErpPostRefusedError`,
so the invoice lands at `failed` on the first attempt with
`refusal = {code: "exception_blocks_erp_export", params: {exception_type}}` on
`invoice.erp_failed`. The predicate is `payment_runs.blocking_exception_types`
narrowed with `types=`, so "which flags are live" (anything but resolved or
dismissed) stays one query. The three HTTP doors also ask, before their
transition, and answer 409 with the same coded refusal: the push runs in the
background, and a 202 that fails a second later would leave the user reading
the audit trail to find out why. The invoice stays `approved` (or `failed`) and
nothing is queued. The 409 commits first: `/complete` runs `refresh_warnings`,
which may open the very duplicate the refusal names, and the rollback a raised
`HTTPException` causes would otherwise send the user to an empty queue.

**A refusal says when the bill may already be in the ERP.** `_call_erp`
commits `state_data.erp_outcome_unknown = true` before every adapter call and
sets it false only on a success, so a timeout, a 5xx, a worker that died
mid-call, or any refusal leaves it true. Every non-success counts because a
refusal made after the create went through (a QuickBooks read-back that lost
its connection, a Xero read-back raising `ErpNotConnectedError`) cannot be told
from one made before anything was sent. `erp.possibly_posted` also reads an
unconfirmed ERP job, an orphan document, or an `erp_reference`; a row from
before the key is read fail-closed from `last_error` / `erp_retries`, once.
When the gate refuses such a retry, the refusal params, the 409 and the
`invoice.erp_failed` row carry `possibly_posted: true`. The gate does not look
the bill up: it refuses before any adapter is built, and once the flag is
cleared the next push's own idempotency lookup finds the bill instead of
posting another.

**Clearing the flag is the segregated sign-off already in place.** Both types
are payment-blocking, so `exception_lifecycle.record_decision` refuses anyone
implicated in the invoice (§169–§170). The invoice's uploader cannot dismiss
the duplicate and then post it; someone else can, and then the push goes
through. Nothing in the gate writes to an exception.

**A finding a person cleared stays cleared, while the invoice is unchanged.**
`_ensure_exception` deduped only against open and escalated rows, so the next
`refresh_warnings` re-opened a `duplicate` a reviewer had dismissed. With the
gate, that made a legitimate invoice that shares a number (a vendor who reuses
numbers yearly) unpostable, because `/complete` refreshes on every send. For
these two types each detector row now carries `finding_fingerprint` (migration
0116): a SHA-256 of the finding's code and the invoice fields those rules read
(`invoice_warnings.finding_fingerprint`), stamped once when the row is raised.
A row a person resolved or dismissed whose fingerprint equals the invoice's
current one suppresses the re-raise; an edit to any of those fields raises it
again. The one input a rule reads from outside the invoice row, the vendor
email domain of `personal_email_domain`, is hashed from the finding's params.

Matching on the finding's display params was the first version and the review
rejected it both ways. Three findings have no params (`duplicate_invoice_number`,
`remit_to_changed`, `future_invoice_date`), so one dismissal exempted the
invoice from the same finding after an edit re-pointed it at another, already
paid bill, or at an attacker's remit address: the warning showed but nothing
blocked, weakening the payment-run gate as well as this one. Other params drift
with no edit (a new vendor's age in days, a vendor mean, an LLM's wording), so
a dismissal came back the next day. The fingerprint is over what the person
judged, the invoice, not over what the sentence displays; it is hashed so the
column holds no address.

"A person, about this invoice" is enforced, not assumed.
`exception_lifecycle.record_decision`, the one path that closes an exception,
keeps the fingerprint on a resolve or dismiss only when a person decided (an
`actor_id` and no `via`: not a PO-match refresh, not an agent) AND the invoice
still hashes as it did when the finding was raised. The second condition is
what the second review asked for: restamping the open row on each refresh let
an implicated clerk re-point the invoice while a manager had the queue open,
and the manager's dismissal then recorded a state they never saw. With the
stamp fixed at raise time, that dismissal binds nothing, the next refresh
raises the finding again, and a person sees the re-pointed invoice. The same
holds for writers that change these fields without a refresh (vendor merge,
the amount-mismatch and multi-PO agent resolvers). The third review closed the
timed variant, an edit reverted just before the click (S1 to a clean-looking S2
and back): every `refresh_warnings` voids the stamp of an open row the invoice
no longer hashes to, whether or not the rule still fires, so once the invoice
has diverged that row's verdict can never bind (`_void_stale_finding_stamps`).
The cost of all three is one extra review round, never a dead end: the next
refresh raises a fresh row stamped with the current state, and its dismissal
holds. Escalation, including an agent's, leaves the stamp alone. Today no detector or agent closes either type
anyway. A row closed before 0116 has no fingerprint and never suppresses, which
is how it behaved. Other types keep re-opening after a person closes them,
which is a follow-up.

**Not done here** (`docs/followups.md`): a flag raised after the bill is
already in the ERP leaves that bill payable there, and nothing yet offers the
existing ERP void from the exception. Voiding automatically was rejected, since
a flag can be false.

## 287. Absence is evidence only in a whole listing, and only a named company can judge a link (2026-10-19)

**Absence is evidence only in a whole listing, and only a named company can
judge a link.** Review of §284 found three holes in the rules it added.

1. **A partial pull read as a complete one.** Rule 0 and the dead-link
   handling read "this id is not in the pull" as "that record is gone", and
   both syncs count codes and names across the pull to decide uniqueness. But
   most adapters' chart and vendor listings returned `[]` on any error, the
   rows read before a later page failed, or the first 1000 rows. So a
   transient failure, or a large chart, on the first sync after 0115 unlinked
   every NULL-realm and cross-realm link the pull lacked. A listing now
   returns everything or raises `ErpListingIncomplete` (`fetch_failed` /
   `truncated`; QuickBooks raises its own request error on a failed page),
   which the sync endpoints answer 502 without syncing. The bound is
   `base.SYNC_LISTING_MAX_ROWS` (10,000; QuickBooks keeps its 50 × 1000 =
   50,000), and reaching it raises. A truncation marker on a partial result
   was rejected: the syncs also count codes and names across the pull to
   decide uniqueness, which a partial pull gets wrong whatever it is marked.
   An empty pull changes nothing. Locked, inactive and archived records
   (Blackbaud accounts closed to data entry, inactive Sage ZA accounts,
   archived Xero accounts and contacts) are emitted `active=False`, not
   dropped, since a dropped record also reads as a dead link. PO listings keep their bounded best-effort
   reads: nothing judges a link by a PO's absence.
2. **A pull with no realm judged real links.** `mock`, or an OAuth ERP with no
   consent, has no realm, and a sync under one re-judged every link stamped
   with a named realm against fixture data: real links were dropped, matched
   by a fixture's colliding id and renamed, re-linked by code, and pending
   link decisions wiped. Such a pull now leaves every row tied to a named
   realm alone (a link stamped with one, or candidates offered under one), and
   a pulled code such a row holds is `code_taken`. It works only on unlinked
   rows and NULL-realm links.
3. **A legacy code proved nothing between two named realms.** Rule 0 kept a
   link moving from one named realm to another (sandbox to production) when
   the colliding id held the row's legacy code, which for QuickBooks is the
   bare account `Name` two sub-accounts can share. The current code is now
   required, or the row is unlinked and the accounts its legacy key names
   become its `erp_link_candidates` for a person to choose from. A legacy key
   is still evidence for a NULL-realm (pre-0115) link, overwhelmingly into the
   same company. Likewise a vendor link between named realms is kept only on
   a name or tax id that is unique in the pull.

**Smaller holes closed in the same pass.** Business Central and Merge.dev
vendors with no proper id are skipped, as NetSuite's mapping already did, and
never keyed by the vendor No., `remote_id` or name: a name used as an id let
two same-named vendors each relink one row past the ambiguous-name guard, and
the other fallbacks are a different kind of key from the one the push posts. An
ERP value too long for its column is dropped and reported, never truncated, and
never aborts the sync: a vendor's over-long optional field (`code`, email,
phone, address, tax id, payment terms) is not written and the vendor still
syncs (`values_too_long`, field names only, also on its audit row); an
account whose `parent_code` does not fit syncs without one
(`parent_code_dropped`). Before, one NetSuite `entityid` over 50 characters
failed the flush and rolled back the whole vendor sync.

**Not done here** (`docs/followups.md`): a resumable sync for listings over the
bound, the sync UI's reporting of the new keys, over-long names, and the
adapters that still drop or mislabel records.

## 288. Password sign-in is refused on another workspace's page (2026-10-20)

**Found in production.** A user of one workspace signed in on
`sandbox.feohledger.com`. Login looks the user up by email across every
organization and minted a token for the user's own org, whatever page the form
was on. The page then sent `X-Tenant-Slug: sandbox` with that token,
`tenant.get_tenant` refused every tenant-scoped call with `403`, and the user
saw a signed-in screen stuck on "Loading…".

**The rule:** when the login request names a workspace (`X-Tenant-Slug`, or a
registered custom domain in `Host`) that is not the account's own, login answers
`403` coded `wrong_workspace`, issues no token, audits
`auth.login.failure` / `reason=wrong_workspace`, and carries the account's own
sign-in URL in `params.sign_in_url`. It runs after the password verifies, like
the `sso_only` refusal, so the unknown-vs-wrong-password parity is untouched and
the link discloses nothing a correct password does not already prove. A request
naming no workspace (an API client) is unaffected.

**Rejected:** silently issuing the token and redirecting the browser to the
account's own workspace. The user asked to sign in *here*; moving them to a
different tenant without saying so hides which workspace they are in, and a
token for one tenant handed to another tenant's origin is the confusion the
refusal exists to stop. Detail: `docs/authentication.md` § Signing in on another
workspace's page is refused.

**Recovery, for a mismatched token that already exists:** `get_tenant`'s refusal
is coded `token_tenant_mismatch` (still `403`), and the SPA ends the session on
it as it does on a `401` — token cleared, `/login?reason=other_workspace` with a
localized notice — instead of leaving the shell waiting on calls that will never
succeed.

**Why the suites missed it:** backend tests always send the matching
`X-Tenant-Slug`, and the e2e isolation specs signed in on each tenant's own
origin and hopped, where per-origin storage just leaves the other origin signed
out. They asserted no data leaked, which held, not that the user lands somewhere
usable. `tests-e2e/auth/tenant-isolation.spec.ts` now signs in on the wrong
page. Two specs had encoded the defect: `login-security.spec.ts` pinned the old
"header is advisory" login, and `discounts/bulk-negotiate.spec.ts` signed acme's
CFO and clerk in on the worker tenant's page, so its role-refusal checks passed
on the tenant-mismatch 403.

## 289. The web app has a light theme, chosen by the OS or per browser, and the marketing page stays dark (2026-10-20)

**The web app was dark-only, and now follows the reader.** `app.css` set
`color-scheme: dark` on `:root` and carried one palette; nothing ever decided
that the product should be dark-only — §28, §180 and §181 were all written
against the only palette there was ("a base token for text on a dark
surface"). This supersedes that assumption, not those decisions: the two-token
contract (§28), the badge tint pairs (§30) and the shared visual layer (§181)
hold unchanged in both themes. What a theme is: **System** by default (follow
`prefers-color-scheme`; an OS that states no preference gets dark, the palette
the app was designed in), or **Light** / **Dark** chosen on
`/profile?section=appearance` and kept per browser in `localStorage`
(`feoh_theme`), like the display language — a preference about this screen,
not about the account.

**The palette switch is all CSS; script only says which one is wanted.** The
dark palette stays on `:root`, the light one sits on
`:root[data-theme='light']`, and System is the same light block inside
`@media (prefers-color-scheme: light) { :root:not([data-theme='dark']) {…} }`.
The duplication is deliberate: CSS cannot share a declaration block between a
selector and a media-gated selector, and the alternatives were worse.
`light-dark()` would have put both values on every token in one place, but it
changes what `getComputedStyle` returns for a token, needs the audit to model a
per-element colour scheme, and a white-label override of `--accent` would have
had to be written as a `light-dark()` pair too. Having the inline script
resolve System into `light`/`dark` itself would have needed one block, but it
puts the OS preference on a JavaScript listener rather than the cascade, and a
page whose script fails would lose the OS's choice. So the copy exists, and
`tokenPairing.test.ts` holds it: the two light blocks byte-identical, every
dark colour token redeclared in light, `color-scheme` set in each.

**No dark frame for a Light user.** `src/app.html` carries a short
inline script that runs before any stylesheet or module and sets `data-theme`
from storage. It repeats three facts from `src/lib/theme.ts` because it cannot
import it, and `theme.test.ts` runs the script against every stored value ×
OS preference — including storage that throws — and compares the result with
what the module would do. The CloudFront policy sets no CSP (`infra/frontend.tf`
notes SvelteKit's hydration needs inline script anyway), so the script costs no
policy change; a future CSP needs its hash.

**Both palettes are held to the same bars, and the audit runs per theme.**
Every check in `tokenPairing.test.ts` now runs once for dark and once for
light, never with a looser threshold. Running it the first time on the light
palette found **~80 failing rules** and, behind them, ~300 colour literals in
component `<style>` blocks: status red, green and amber typed as hex
(`#f06464`, `#1fa86a`, `#d4940a`) and tinted backgrounds typed as `rgba()` of
those hexes. Each was legible on navy and 2.2–2.9:1 on white. They are tokens
now — a new `--warning` base token, a `--violet` tint pair for the ERP pill
(two badges spelled it as a literal "rather than a token with one caller",
which a second theme ends), chrome tokens for borders, scrollbars and the
modal overlay, and `color-mix(in srgb, var(--tone) N%, transparent)` for every
tone-tinted background. The audit learned to resolve that `color-mix()` form
(and the opaque two-colour one): without it, converting a literal to a
theme-following token would have *removed* its contrast check, which is the
wrong way for a refactor to move a guard. The exceptions queue and the agent
dashboard built badges as a literal hex plus a concatenated `1f` alpha in an
inline `style`; they now name a palette tone and render its tint pair.

**The light base tokens are darker than the bare-surface bar needs.** About
forty rules put a base token as text on a 10–20% tint of its own hue (an armed
delete, an inline error box). A first light palette that cleared 4.5:1 on
white failed every one of them at 4.1–4.5:1; rather than convert forty rules to
the `-on-tint` pair one by one, each light base token was darkened until it
clears its own 20% tint, which is a property of the token and can be asserted
there.

**The marketing landing page stays dark.** It is the one surface designed as
a picture rather than as UI: the Blender-rendered tally (§180), the grain, the
glass panels and the hero card were all composed on navy, and a light version
is a design job — a re-render and new panel treatments — not a token swap.
Its root carries `data-theme-scope="dark"`, which `app.css` includes in the
dark palette's selector list; custom properties inherit, so the subtree renders
dark whatever the page's theme, with no third palette copy. The light audit
skips the four components only it renders, and a test proves they are rendered
nowhere else. **The legal pages follow the theme**: they are documents read at
length, often arriving from outside the app, and a reader who asked for light
text on a light ground is exactly who should get it; their chrome is tokens
already. The auth shell, signup, the supplier portal and the help centre follow
it too.

**Two things this does not settle, tracked rather than half-done.** A
white-label tenant's `--accent` is written inline on `<html>` and so applies in
both themes; nothing measured it as text even in dark, and a blue chosen for
navy is ~3:1 on white (`docs/followups.md`). And testing the storage-throwing
path showed the app shell renders nothing at all when the browser blocks site
storage — on `main`, independent of the theme, whose own storage access is
wrapped (`docs/known-issues.md`).

Guards: `src/lib/a11y/tokenPairing.test.ts` (both palettes, the light blocks'
identity and coverage, the dark-pinned subtree), `src/lib/theme.test.ts` (the
resolution rules, storage throwing, and the pre-paint script executed against
the module), `src/lib/legal/storageInventory.test.ts` (`feoh_theme` is on the
Cookie Notice), and `tests-e2e/a11y/light-theme.spec.ts` (choose → reload →
`data-theme` present at the first animation frame, System following the OS
through CSS alone, the keyboard model of the picker, and axe in light mode on
a cross-section of the signed-in app, the portal, the auth shell, the help
centre and the legal pages).

## 290. The theme choice is a cookie on the platform domain, so it follows the visitor across workspaces (2026-10-20)

**§289 said "per browser", and built per origin.** The choice lived in
`localStorage`, which is scoped to an origin, and every workspace is its own
origin: `acme.feohledger.com`, `globex.feohledger.com`, and the apex
`feohledger.com` where signup lives. So Light picked on one workspace did not
reach another, and — the case a new customer meets first — a theme picked
while signing up was gone the moment the verification link landed them in
their new workspace. The copy ("Choose the colour theme for this browser")
was the intent; the storage was the bug.

**Options weighed.** (a) Keep per-origin and reword everything to say "this
workspace" — honest, but it makes a screen preference behave like workspace
data, and the signup → workspace hop would still drop it. (b) Store it on the
account — §289 already rejected that (a preference about this screen, not the
person), and signup and sign-in come before any account. (c) Hand it over in
the post-signup redirect URL — fixes one hop, not workspace → workspace. (d) A
cookie on the platform's registrable domain, read by the pre-paint script.
Only (d) is per browser in fact, so it is what shipped.

**How.** On a platform host the choice is written to `feoh_theme` with
`Domain=<platform domain>; Path=/; Max-Age=1 year; SameSite=Lax` (+ `Secure`
on https). The domain is never hardcoded. It is never inferred from the
host's shape either: it is only ever one the build positively declares as
ours. `hostRouting.ts::sharedCookieDomainForHost` matches the host against
`PUBLIC_PLATFORM_DOMAINS`, when set, and against the host of
`PUBLIC_SITE_URL`, the platform's apex origin, which every deployed build
already sets (`vm-deploy.yml` from `APP_URL`, `deploy/deploy.sh` from
`APP_DOMAIN`) and which switches no routing on. Production builds leave
`PUBLIC_PLATFORM_DOMAINS` unset, because setting it turns on vanity-domain
routing. So the site URL is what makes the cookie work there.

**The legacy host rule is deliberately not an input.** A first cut used it
when `PUBLIC_PLATFORM_DOMAINS` was unset: a 3+-label host shared at its
parent. That rule reads a customer's `ap.customer.example` as workspace `ap` of
`customer.example`, so it would have put our cookie on the customer's own
parent domain. That is wrong technically, and it would also make the "one
cookie, on our domain" sentence in the Cookie Notice false. A host that is not
equal to, or beneath, a declared domain gets no shared cookie, and a build
that declares nothing shares nothing.

There is no shared domain on localhost (browsers refuse `Domain=localhost`),
on an IP literal, or on a customer's vanity host (a platform cookie does not
belong on the customer's domain, and no other workspace could read it there).
Those keep the §289 `localStorage` behaviour, per origin. On a vanity host
that loses nothing, because it serves one workspace. The pre-paint
script reads the cookie first and `localStorage` second, so a choice made on
another workspace beats an older one remembered here. Two consequences of that
order, both deliberate: System is written to the cookie as `system` rather
than by deleting it — deleting would let a stale per-origin Light resurface —
and a write reads the cookie back before removing this origin's copy, because
a browser blocking cookies drops the write silently; then the choice falls
back to `localStorage` and still survives a reload.

**It is a cookie, and the legal pages said there were none.** The Cookie
Notice and the Privacy Policy both stated the app sets no cookies. They now
say it sets exactly one, `feoh_theme`: the one word, set only when the visitor
picks a theme, identifying no one, classified like the language preference as
functional storage the visitor explicitly asked for (ePrivacy's
user-interface-customisation exemption). It is sent with navigations to the
platform's hosts but not with API calls (the API client never sends
credentials cross-origin). `storageInventory.test.ts` now fails if a second
`document.cookie =` appears anywhere in `src/`, so "exactly one" cannot go
stale silently the way "none" would have.

**A picker on the auth pages.** A cross-workspace theme is most useful before
there is a workspace, and signed-out visitors have no `/profile`. Every
`AuthShell` page (sign-in, MFA, signup, verify, change-password) now carries
the same picker under the form — `ui/ThemePicker`, extracted because it was
about to be wired twice — outside `.form-wrap` so the shell's field styles
never reach it.

**hCaptcha follows the theme after first render.** It was told the palette
once, at render, so changing the theme (or the OS, on System) left a dark
captcha on a light form. hCaptcha has no call to restyle a live widget, so the
page now loads it in explicit-render mode and an effect redraws it (remove +
render) whenever `theme.resolved` changes — except when the visitor has
already solved it: redrawing would discard a token they earned, for a
cosmetic reason. A solved widget is redrawn in the current palette once its
token is gone (expired, or spent on a submit).

Guards: `src/lib/theme.test.ts` (the domain derivation — apex, workspace,
localhost, IP, vanity, the site-URL-only production shape, and a custom
domain under a foreign parent never getting a `Domain=` with only the legacy
rule in play; cookie read/write incl. System, a blocked and a
throwing jar; the pre-paint script against every cookie × stored value),
`src/lib/legal/storageInventory.test.ts` (one cookie writer, and the notice
says so), `tests-e2e/a11y/light-theme.spec.ts` (the cookie beats a stored
choice at the first frame; localhost writes no cookie; the auth-shell picker
persists and passes axe) and `tests-e2e/signup/flow.spec.ts` (the captcha
redraws on a picker change and on an OS change, keeps a solved widget, and
redraws after expiry).

## 291. One address is one account in one workspace, and every collision says so (2026-10-20)

**Found by the admin persona review.** `users.email` is globally unique, so an
address names one account in one workspace and there is no multi-workspace
membership. Three paths had not caught up with that:

- **SSO/SAML JIT** searched only the signing-in workspace, then inserted, and the
  UNIQUE constraint raised at flush: a bare `500` on the callback, no audit row.
  A contractor in two customers' IdPs hit it on first sign-in and nobody could
  see why. SCIM already answered the same collision with a `409`.
- **Admin create/rename** answered a bare "Email already in use" — a dead end
  for the inviter, and a platform-wide existence oracle for anyone holding
  `user.manage`, including the admin of a free self-signup tenant.
- **The `wrong_workspace` row** (§288) recorded neither the workspace whose page
  it was nor that the password had verified.

**The rules:**

1. `identity_provisioning.email_holder_org_id` is the one platform-wide,
   case-insensitive "who holds this address" lookup; JIT, admin and SCIM all ask
   it before writing. JIT raises `EmailInOtherWorkspace` before the insert, and
   the insert runs in a savepoint so a concurrent writer is the same refusal.
2. **SSO refuses with a coded `403` `sso_email_in_other_workspace`, and no link.**
   The password path links the account's own sign-in page because only someone
   who proved the password sees it. Here the proof is an assertion from the
   signing-in workspace's IdP, which that workspace's admin controls and can
   make assert any address; a link would name another tenant's workspace (slug
   or vanity host) for any address typed. The real user knows their own
   workspace; the sentence tells them to sign in there or ask for a different
   address. The row goes in the IdP's workspace (its admin is the one who can
   act) and **not** in the account's: that admin must not be able to write
   rows into another tenant's log by typing addresses. SAML routes the refusal
   through a refusal-only handoff code so the browser lands on the SPA bridge,
   not raw JSON at the ACS.
3. **Admin and SCIM say which case it is** — `email_in_this_workspace` (edit the
   existing user) or `email_in_other_workspace` (use a different address) —
   naming no workspace and no account.
4. **The remaining existence signal is bounded, not hidden.** While the address
   is global, any refusal confirms it is taken. Hiding it would mean answering a
   collision as if it succeeded (inventing a user that does not exist) or
   refusing without a reason (the dead end again), so the admin path spends a
   per-workspace budget of 20 other-workspace refusals an hour, checked before
   the lookup so a throttled sweep learns nothing. Per workspace rather than per
   user, so minting extra user-managers does not widen it. Once spent, EVERY
   create and email rename in that workspace waits out the window — a free
   address included, with its own coded 429 `email_check_throttled` saying so
   — because refusing only the taken addresses would make the 429 the oracle.
   That can pause a real onboarding for up to an hour after twenty
   other-workspace hits, which is the accepted price. SCIM and SSO are **not**
   budgeted, and their signal is accepted: an IdP re-sends a doomed SCIM write
   every reconcile cycle, so a throttle would turn a stated 409 into a stuck
   provisioning job, and a budget on SSO refusals would lock a whole
   workspace's sign-ins; both also need the plan-gated SSO/SCIM feature and a
   configured IdP, each SSO probe is a full browser round trip that leaves a
   row in the prober's own audit log, and the same admin can already ask the
   budgeted admin path.
5. **`wrong_workspace` audits `attempted_workspace` and `password_verified:
   true` in the account's workspace, and nothing in the attempted one.** An
   unknown address leaves no row anywhere, so a row in the attempted workspace
   only when the password was right would make its log an oracle; the event is
   the account's.
6. **The wrong-workspace link prefers a registered custom domain** when the org
   sets no `tenant_url_template` (an explicit template still wins). The runbook
   registers a host only once DNS and TLS are live, and the slug subdomain is a
   different origin: another stored token, and no passkeys bound to the vanity
   host. Other outbound links are unchanged.
7. **A token refused as `token_tenant_mismatch` is also revoked**, best-effort,
   by the SPA calling `POST /api/auth/logout` with it and without the page's
   `X-Tenant-Slug` (logout reads the token's own claims and needs no
   workspace). Clearing it locally left a live session in its own workspace.

**Rejected:** silently linking or migrating the person into the second
workspace (an IdP is not entitled to move an account between tenants), and
true multi-workspace membership now — the right long-term model, but it
reaches login, sessions, every `organization_id`-keyed user query, SCIM, JIT,
access reviews and DSAR; tracked in `docs/followups.md` § Surfaced by the
cross-workspace identity fixes.

Tests: `backend/tests/test_email_identity_case.py` (JIT, the race, the OIDC
callback, admin create/rename, the budget, SCIM, the wrong-workspace audit
row), `backend/tests/test_saml_acs_integration.py` (ACS → bridge → exchange),
`backend/tests/test_tenant_url_resolver.py` (the custom-domain link),
`frontend/src/lib/api/sessionRejection.test.ts` (revocation) and
`frontend/src/lib/api/codedRefusals.test.ts` (the three sentences).

## 292. Signup refuses an address that already has an account at /complete, and a used verification link is re-answered (2026-10-20)

**Two dead ends in self-service signup.**

1. **An address that already had an account.** `users.email` is unique across
   every organization, but `/signup/start` never looked at the address. The
   visitor was told a link was on its way; opening it ran `provision_tenant`,
   which created the database, hit the unique index, dropped the database and
   surfaced as a 500 "Something went wrong… contact support". The verification
   stayed unconsumed, so every reload did all of that again.
2. **Opening the link twice.** `/verify` redeems on page load and the token is
   spent on success, so a reload, a second tab, or a mail scanner that ran the
   page first met the uniform 410 "invalid or expired, please sign up again" —
   and signing up again then failed, because the slug was now taken by the
   workspace that very link had created.

**The rule for (1): refuse at `/complete`, keep `/start` uniform.**
`/signup/complete` checks the address (case-insensitively, as sign-in does)
before the slug and before provisioning. An address with an account answers
`409` coded `signup_email_has_workspace`, with `params.sign_in_url` the existing
account's own sign-in page, built exactly as the wrong-workspace login refusal
builds it (§288). The verification is spent and committed before the refusal is
raised, so it can never reach provisioning again, and re-opening it re-states
the same refusal. The address is checked before the slug so someone signing up
again for their own workspace is pointed at it, not told the name is taken.

`/start` answers an address with an account exactly as it answers any other —
same status, same message, a verification link sent. It is public and captcha'd
but unauthenticated, and a different answer there would let anyone test which
addresses have accounts. At `/complete` the visitor has proven they read the
mailbox, so naming the workspace tells them nothing they could not learn from
their own inbox.

**Rejected: sending a "you already have a workspace" email from `/start`
instead of a verification link.** It saves the visitor a click and also keeps
the answer uniform, but it needs a second email template in six locales, and
`/complete` needs the check regardless: an address can gain an account between
the two steps. One refusal point covers both.

**The race is a definite answer too.** If the account appears after the check,
the real `provision_tenant` fails on the unique index. It already drops the
database it created and removes any control-plane rows it committed (#575), so
nothing half-made is left. `/complete` then re-reads the address and the slug
and, when either explains the failure, answers the coded refusal or the slug
409, never a 500 the link would repeat. A failure neither explains is still the
500 with the link left unconsumed, so a transient fault can be retried.

**The rule for (2): `/complete` is idempotent for a spent link.** A spent,
unexpired verification whose slug now names an organization holding an account
with the link's address is re-answered with that workspace: `200`,
`already_verified: true`, the tenant URL, the address and `sign_in_url`.
Nothing is issued. There is no second set-password link (`set_password_url` is
null), no welcome email, and no audit row. The first answer and the welcome
email carry the only link, and minting a fresh credential every time an email
link is opened is what the single-token design of §283 avoids. The page shows
"Your workspace is ready" with **Go to sign in** and says where the
set-password link is. A spent link that matches no workspace, or one past its
24-hour life, is still the uniform 410: the re-answer never shows a workspace
the link did not create, and a stale link is not kept alive.

**Rejected: redeeming on a button, as the reset-password page spends its token
only on submit.** The property that page has is that opening the link costs the
user nothing (its load is a non-consuming `check`, §283). A button would not
give `/verify` that property: once the workspace exists the token is spent
however it was spent, so a reload after success would still meet the 410. The
re-answer does give it, for every way the link gets opened, the button click
included. It also makes a JavaScript-running mail scanner harmless: if it
redeems first, the person who clicks next sees their workspace, and the
set-password link is in the welcome email the scanner's visit sent.

**Not fixed here.** Nobody who has forgotten which workspace their account is in
can find it without support. Forgot-password works only on a workspace host,
and the apex has no sign-in page. This refusal helps only someone who has
already started a signup. The durable fix, an apex "email me my workspace
address" form with a uniform answer, is in `docs/followups.md`.

Guards: `backend/tests/test_signup.py` (`/start` uniform for an address with an
account; the coded refusal on a case variant, never provisioning and stable
across re-opening; the race against the real `provision_tenant` leaving no
database and no organization; a spent link re-answered twice with nothing
minted, sent or provisioned; a spent link on someone else's slug still 410),
`frontend/src/lib/api/codedRefusals.test.ts` (the refusal localized, the link
carried), and `frontend/tests-e2e/signup/flow.spec.ts` (both states stubbed, plus
through Mailpit: the link opened again in a second tab after success, and an
existing login's signup refused on first open and on reload, with the slug left
free).

## 293. SCIM writes are audited and last-admin guarded, the guard is serialized, and a deleted user's row says who they were (2026-10-20)

An admin persona review found four gaps in how user accounts change, all in the
same family: the record of a change, and the one invariant user management
exists to keep.

**SCIM wrote no audit row and had no last-admin guard.** Create, rename and
every spelling of a deprovision (`PATCH active:false`, `PUT active:false`,
`DELETE`) changed the account with nothing on the trail, never revoked the
deprovisioned user's sessions, and skipped the guard `PATCH /api/admin/users`
has applied since the sole-admin lockout fix. "Who offboarded Jane, and when?"
had no answer for IdP-driven leavers, and an IdP misconfiguration could leave
the org with no admin and no way back short of a database fix. Each SCIM user
write now commits, revokes sessions on a deactivation, and writes the same
`user.created` / `user.updated` row the admin API writes, with `via: "scim"`
and a null `actor_id` (the bearer token is not a user; the group-role rows
already used null). A failed revoke is recorded (`sessions_revoked: null`)
rather than failing the request: the deactivation has committed, and a 5xx
would only make the IdP retry a change that already happened. The SCIM rows are
deliberately best-effort (`dispatch_auth_audit`), not write-first like the admin
delete below: refusing a deprovision because the trail is unavailable would keep
a leaver's account active, which is worse than a deactivation without its row.

*What SCIM gets for the last admin.* `400` with `scimType: "mutability"`.
RFC 7644 §3.12 defines `mutability` as a modification "not compatible with the
target attribute's … current state", which is exactly this, and IdPs surface a
400 to their admin rather than retrying it. A `409` was rejected: the RFC
reserves it for uniqueness and version conflicts, and this codebase already
answers `409 uniqueness` on SCIM, so an IdP reading the two alike would be
reading a different problem. The refusal is audited
(`user.deactivation_refused`), because an IdP that keeps pushing it is itself
something an admin must see. The same guard covers a `/Groups` write that would
revoke an IdP-managed `admin` role from the last admin
(`auth.scim.role_revoke_refused`) — the same lockout reached through a group,
which the finding did not name but the invariant does. It is checked before any
role is touched, so the group write is refused whole rather than half-applied.

**The guard is shared, not copied.** The rule moved out of `api/admin.py` into
`services/admin_lockout`, which raises a domain exception both surfaces map to
their own shape. Duplicating it into `scim.py` would have produced two rules
that drift, and the serialization below would have had to be fixed twice.

**The guard is serialized on the organization row.** It counted the org's other
active admins and then acted, holding nothing between. Two admins demoting (or
deleting) each other at the same moment each counted the other, both passed,
and the org was left with none. The earlier docs called the third-party case "a
no-op — acting on an admin already required the caller to be one", which holds
only one request at a time. The guard now locks the organization row before it
counts, held to the commit, so the second removal waits and then counts what
the first left. Alternatives:

- *Lock the admin `user_roles` rows* (`SELECT … FOR UPDATE` over the org's admin
  set, as the review suggested). It does not close the race: each request locks
  the rows that exist, and a demotion deletes the row the other request would
  need to see. A row lock protects rows; this invariant is about a set.
- *A `pg_advisory_xact_lock` keyed on the org.* Correct, but a second
  serialization mechanism beside the one the codebase already uses for
  per-org writes (`tenant.lock_organization`).
- *`SERIALIZABLE` isolation for these requests.* Correct too, but it turns the
  loser into a serialization failure every caller would need a retry loop for.

The lock is `FOR NO KEY UPDATE`, not `lock_organization`'s `FOR UPDATE`: it
still conflicts with every settings writer, but not with the `FOR KEY SHARE` an
insert of a `users` row takes through its foreign key, so inviting a user is not
made to wait behind a demotion. It is taken only when the write would actually
remove an active admin. The delete paths gained the guard too: a single delete
could already never remove the caller, but under concurrency the caller is
exactly who another request is removing. `tests/test_admin_lockout.py` drives
the race against Postgres (two sessions, the first holding its demotion open,
the second observed blocked in `pg_stat_activity`) and fails without the lock.

**A deleted user's audit row says who they were, and is never lost.**
`user.deleted` carried only the id of a row that no longer exists. It now
carries the `email`, `role_names` and `was_active` the account held — the
address recorded the way every `auth.*` row already records it; `full_name`
stays out, the rule `user.updated` has always followed. The single delete
wrote its row after the commit with the swallow-everything
`dispatch_auth_audit`, and the bulk delete did the same after its one commit,
so a failed write lost the record while the delete stood. The audit row lives
in the tenant database and the user in the control plane, so one transaction
cannot hold both; the nearest equivalent, which `PUT /api/organization/sso` and
the SSO break-glass already use, is to write the row first with
`record_auth_audit_or_raise` and refuse the change if that fails. A commit
failing afterwards leaves a row for a delete that did not land — the safe
direction to be wrong in. The bulk delete does this per row inside a savepoint,
so one unrecordable row fails as `audit_unavailable` and the rest of the batch
still lands.

`user.updated` recorded only the new role list. It now records each changed
role list, active flag and address before and after (`services/user_audit`,
shared with SCIM).

**The vendor reads admit the vendor permissions.** `GET /api/vendors` (and
`/ids`, `/counts`, `/{id}`) were `require_roles(admin, ap_manager, cfo)`, so a
custom role holding only `vendor.manage` or `vendor.block` saw the Vendors page
in no nav and got a 403 on the list its permission acts on. The payment and
admin lists were fixed by moving to `require_permission` because their default
permission holders were exactly the roles they already admitted; here `cfo`
reads vendors without any vendor permission, so the same move would have taken
the page from the CFO. `deps.require_role_or_permission` admits either, so the
system roles keep exactly what they had, and the nav row ORs the same two
permissions.

## 294. The landing page stays dark as brand, not as a gap waiting for a light version (2026-10-20)

**§289 left the marketing page dark for a reason of cost; this makes it the
decision.** §289 kept the landing page on the dark palette because a light
version is a design job (a re-render of the §180 tally on a light ground, new
grain, new glass-panel and hero-card treatments), not a token swap. It did not
say whether that job should ever be done, and without an answer the question
comes back every time someone notices that a light-mode visitor sees a dark
landing page and then a light sign-in page.

**The answer is no, until evidence says otherwise.** The landing page is the
one surface composed as a picture, and dark is its brand: the render, the grain
and the panels were designed together on navy and have passed the accessibility
audit as they stand. A visitor sees it once or twice before signing up, a dark
marketing site in front of a light product is a common and unremarkable
pairing, and a day spent on a second version moves nothing for the first pilot
customer. Everything a reader spends time in already follows the theme (§289:
the app, the auth shell, signup, the legal pages, the help centre, the supplier
portal), so the theme preference is honoured wherever it matters.

**What would reopen it:** measured drop-off of light-mode visitors between the
landing page and sign-in or signup, or repeated feedback that the switch is
jarring. Either is a reason to do the design job properly (a light render of
the tally, panel treatments composed for a light ground, contrast re-measured),
following the visitor's theme through the existing `data-theme-scope`
mechanism rather than a page-local toggle. Not a reason: consistency for its
own sake, or a token swap that leaves the dark-composed render on white.

## 300. Strings only some routes render load with those routes, as lazy catalogue slices in every locale (2026-10-20)

(Numbered 300 because §292–§299 are held by pull requests still in flight.)

The Japanese catalogue measured 100 KB gzipped on `main` — the per-chunk
ceiling in `web-bundle-budget.yml` — and the five keys of PR #611 took it to
101. The help slice of §261 had bought two weeks. Behind it sat a second
problem: English main is a static import, so it shares an entry chunk with
the i18n runtime, and that chunk was at ~97 KB. Moving keys out of the other
five catalogues alone would have left the English chunk to trip the ceiling
next.

The keys moved were chosen by measuring gzipped bytes per top-level namespace
in `ja` and `en` and keeping the namespaces only a bounded set of routes
renders:

| Slice | Namespaces | Keys | Share of `ja` | Loaded by |
|---|---|---|---|---|
| `settings` | `org`, `orgSso`, `admin`, `billing`, `sweepHealth` | 1,023 | ~20% | `/organization`, `/admin/**`, `/billing`, `/help` |
| `portal` | `portal` | 295 | ~5% | `/portal/**` |

`invoices`, `vendors`, `payments` and `workflows` are as large but render
across the app (modals, pickers, status badges), so they stay in the main
catalogue. Measured on `main` + the split: largest main catalogue 77 KB,
largest chunk 77 KB (the English one), settings 21 KB, portal 6 KB, total
1034 KB (was 1050); with #611 on top, 77 / 78 / 1034.

How it works, and what it guarantees:

1. **One mechanism, not a second hand-rolled copy of §261's.**
   `ensureSlices(...names)` replaces `ensureHelpCatalogue()`;
   `SLICE_LOADERS` holds a loader per slice per locale; `m()` reads the main
   catalogue first and only then finds a key's slice by prefix
   (`SLICE_PREFIXES`), so the runtime never bundles a slice's key list.
   `MessageKey` now means any key `m()` resolves.
2. **A route awaits its slice.** Each route's `+layout.ts` is
   `export const load = () => ensureSlices('settings')`. SvelteKit awaits it
   before rendering, so a slice key is never read before its slice is there and
   a client-side navigation paints the reader's language on the first frame —
   which is better than §261's "English until it lands" on a route.
   `tests-e2e/i18n/catalogue-slices.spec.ts` proves this with the slice's
   chunk deliberately delayed; a `load` that does not wait fails it.
3. **The route-scoped slices are lazy in English too.** Nothing reads them
   unloaded, and keeping their English out of the entry chunk is what brought
   the English chunk from ~97 to 77 KB. The help slice keeps its static
   English copy: a HelpTip renders on every page with no route `load` to wait
   on, so it needs something to show while its chunk is in flight.
4. **A key rendered where its slice is not loaded is a test failure.**
   `src/lib/i18n/slices.test.ts` builds the import graph of `src/` and fails on
   any file that names a key of a slice (a literal, a `{ui:}` reference in help
   prose, or a runtime-built prefix) and is reachable from a route whose
   layouts do not load that slice, naming the chain. Keys the backend names —
   the ERP and commerce form fields' `label_key`s — are read out of the backend
   adapter packages and attributed to the panel that renders them. Every slice
   key must be named somewhere, so a key the scan cannot place fails rather
   than slipping through; that found two dead keys (`help.glossary.topic`,
   `org.section.brandingSaved`), removed. In dev, `m()` also logs such a key.
   The thirteen keys under the slice prefixes that shared code renders —
   role names, NACHA field names, commerce environment options, the portal's
   "waiting on" phases — stay in the main catalogue, which the test is what
   decided.
5. **A locale switch swaps everything at once.** `setLocale` loads the main
   catalogue and every wanted slice in parallel and assigns them together, so
   a settings form never shows one language inside another's chrome. A slice
   that fails in the new locale falls back to English. §261's loader retried a
   failed help chunk in a loop for as long as it kept failing; the loader now
   retries only when the locale changed while it was in flight.
6. **Parity and plurals cover every slice.** `messages_parity.test.ts` holds
   each slice in each locale to the main catalogue's rules;
   `plural_messages.test.ts` and `approvalThresholdCurrency.test.ts` read every
   slice through `allMessages.ts::loadAllMessages`, having silently stopped
   seeing the keys that moved (help's plurals included, since §261).
7. **The budget counts one file per catalogue kind**, the kinds read off the
   `locales/` subdirectories, so a further slice is counted correctly without
   editing the workflow.

Rejected:

- **Raising `MAX_LARGEST_CHUNK_KB`.** The workflow's own log says not to for
  catalogues, and the next feature would trip it again.
- **A slice per route.** Six thousand keys reorganised for a problem two
  prefixes solved, and a page's strings would fragment across its modals and
  shared components.
- **Moving `invoices.*` / `vendors.*` / `payments.*`.** Larger, but rendered
  from shared modals and pickers on most routes; the scope test would have
  kept most of them in main, and a slice every route loads saves nothing.
- **Keeping the new slices' English static, as help's is.** Simpler, but it
  leaves the English chunk at ~98 KB, two kilobytes from the ceiling.
- **Resolving the visitor's locale before the first route `load` runs**, so a
  non-English cold load of a settings route does not fetch the English slice
  before its own. It saves ~15 KB on that one cold load and would mean the
  route `load` and `initLocale` racing to decide the locale; the existing
  English-first paint of the main catalogue already has the same shape.

Tests: `frontend/src/lib/i18n/slices.test.ts`,
`frontend/src/lib/i18n/messages_parity.test.ts`,
`frontend/src/lib/i18n/plural_messages.test.ts`,
`frontend/tests-e2e/i18n/catalogue-slices.spec.ts`, and the backend's
`test_erp_catalog.py` / `test_commerce_adapters.py`, which now read the
settings slice for the form labels they check.

## 295. QuickBooks status flows back, our payments flow there, and a dying connection says so (QuickBooks Phases 3–5, migration 0117, 2026-10-20)

**Context.** Phases 0–2 of the QuickBooks Online adapter (§269–§270, §284–§287)
push bills. Three things were open (`backend/docs/quickbooks-online-adapter.md`):
a bill paid or deleted in QuickBooks never reached FeohLedger, a payment made in
FeohLedger never reached QuickBooks, and nothing warned before the refresh token
expired. Reading the money path to scope them found the premise missing: a
confirmed push walked `sent_to_erp → done` (since the `done` status was added),
so a pushed invoice was never payable here at all, and a QuickBooks "paid" had
nothing to move. The double-payment risk the plan worried about could not occur
— and neither could the feature it was meant to protect.

**Decision — where a push lands.** A confirmed push lands at `posted_in_erp`
(payable) instead of `done` **only when the ERP's payment status provably comes
back**: `FEOH_ERP_STATUS_SYNC_ENABLED` on AND an adapter with
`supports_status_sync` (QuickBooks Online). `services/erp_status_sync.
status_sync_active` is the one predicate `send_to_erp_internal` asks. Off (the
default, like every outbound sweep), the push still closes at `done`: leaving a
bill payable here while nothing would ever tell us QuickBooks paid it is the
double payment. The same flag starts the change-data-capture sweep, so the
landing can never be on without its guaranteed channel. The push records
`state_data.erp_realm` beside `erp_reference`: QuickBooks numbers bills per
company (§284), so a bill id only names a bill within its realm.

Rejected: *landing at `posted_in_erp` for every ERP* (no other adapter reports
payments back, so every pushed invoice would sit payable while the ERP paid it);
*gating on the webhook token alone* (Intuit does not guarantee delivery; the CDC
sweep is the guaranteed channel); *a per-tenant "who pays" setting* (a second
switch that can contradict the first; nothing here stops a QuickBooks tenant
paying in either place, and the sync handles both).

**Decision — inbound (Phase 3).**

- `POST /api/erp/webhook/quickbooks`, declared before `/webhook/{erp_type}`.
  The raw body is verified with the Intuit app's ONE verifier token
  (`FEOH_ERP_QBO_WEBHOOK_VERIFIER_TOKEN`, base64 HMAC-SHA256 in
  `intuit-signature`, `webhook_security.verify_hmac_sha256_base64`) before any
  parse; empty token = every delivery refused, no fallback. CloudEvents only
  (`qbo.bill.*`, `qbo.billpayment.*`); the event's `data` is never trusted —
  the bill is read back. Each event is claimed by its id before the 204 goes
  back (a dedup store we cannot reach is a bodyless 503 so Intuit redelivers);
  the reads and the transition run after the response, on the request's loop
  (`payment_erp_sync`'s shape), because Intuit wants an answer within seconds
  and an event costs up to three reads of the company. A failed event releases
  its claim. Every rejection is a silent 204.
- **The realm index is a unique expression index on the OAuth block, not an
  `erp_connections` table** (migration 0117, control plane:
  `uq_organizations_erp_oauth_company` on `settings.erp.oauth.(provider,
  external_tenant_id)`). The block is the one record of the connection, written
  only by the callback, the refresher and disconnect; a table would be a copy
  all three must keep in step. The index serves the webhook's routing and makes
  "one tenant per company" a database rule (the callback maps the
  IntegrityError of a racing second tenant to `already_linked`). The migration
  refuses a control plane already holding two tenants on one company rather
  than pick one. A tenant that switched to another ERP keeps the block (§270) but
  is not synced.
- The CDC sweep (`erp-status-sync`) is stateless: every tick asks each company
  for changes over an overlapping window (`FEOH_ERP_STATUS_SYNC_LOOKBACK_HOURS`,
  72 h). Re-applying a report is a no-op, so no cursor is stored; a full CDC page
  raises rather than apply part (§287's "absence is evidence only in a whole
  listing").
- `apply_bill_report` matches a bill to its invoice by our `PrivateNote` marker
  AND the invoice's `erp_reference` (a deleted bill, which cannot be read, by
  `erp_reference` within the recorded realm only), locks the invoice, and acts
  by the table in `quickbooks-online-adapter.md` § Phases 3–5 as built. Paid by a
  person → the existing ERP-paid path (recorded as paid outside, `source=erp`).
  Paid there while a FeohLedger payment is scheduled, part-paid there, or deleted
  there while payable here → the payment-BLOCKING `payment_reconciliation`,
  because each lets a run pay money not owed; the other disagreements →
  `erp_reconciliation`. **A bill paid only by our own write-back is not news**:
  the adapter tells our BillPayments (marker) from a person's.
- The followup "an ERP-reported payment is dated the day the webhook arrives"
  is the same code path, so it is closed here: the generic webhook takes
  optional `paid_on` / `payment_method`, QuickBooks supplies the paying
  BillPayment's `TxnDate` / `PayType`, both validated like the user's own record
  (a real date not in the future; a recordable rail, never `virtual_card`) and
  falling back to arrival date / NULL. QuickBooks `CreditCard` needed a rail
  that is a card but not ours: **`card`**, recorded only (refused on a payment
  FeohLedger would dispatch), excluded from the 1099 like `virtual_card`.

**Decision — outbound (Phase 4).** `payment_erp_sync` writes a settled payment
back once its invoice is `paid`, after that commit (no lock across Intuit), only
for a bill pushed under the CURRENT realm. Idempotent on the payment id in the
bill's two layers (find our own marker on the bill's BillPayments; `requestid`).
A replay of a payment someone voided in QuickBooks is refused
(`bill_payment_voided`) rather than reported written — re-posting would overrule
them. The pay-from account is the org's choice, as GL codes from our chart
(`bill_payment_bank_account` / `bill_payment_card_account`, resolved under the
realm): QuickBooks needs a bank account for `Check` and a card account for
`CreditCard`, and a guess books money from the wrong account. Never more than the
bill's open balance (`payment_exceeds_balance`). Refusals are coded on the
existing registry (`intuit_code`, never `Message`/`Detail`), audited
`payment.erp_post_failed`, flagged `erp_reconciliation`; they never undo the
transition, since the money moved. Open question 3 (early-pay discounts) is
answered conservatively: post what was paid and flag the balance left open for
the bookkeeper — no vendor credit is created on their behalf. A FeohLedger void
voids our BillPayment (flagged when it cannot).

**Decision — the connection (Phase 5).** The refresher already records the
expiry Intuit returns on every rotation. The `erp-connection-health` sweep (off
by default) **first keeps a quiet connection alive** — a token with fewer than 45
days left is refreshed through the refresher's own lock and compare-and-swap
(`force_refresh`) — and only then warns: admins get an in-app + email
`erp_connection_expiring` notice at each lead (30/7/1 days) still crossed after
the refresh, once per (reported expiry, lead), the marker written after the
send so a crash repeats a warning rather than losing one. A connection the
provider refused gets one "reconnect" notice; the refresher now records why
(`reconnect_reason`: expired past the reported expiry, else revoked), which
`/status` and the ERP panel state, with Reconnect offered from the first lead.
Warning without refreshing was rejected: Intuit rolls the lifetime forward on a
refresh, so most "expiring" connections are only idle, and a notice for those
trains admins to ignore the one about the hard cap.

**Hardened in review, before merge.** The money-path review found four ways
the first cut could still pay a bill twice, each closed here:

- *A vendor credit is not a void.* QuickBooks records a credit applied to a bill
  as a $0 BillPayment; reading every $0 BillPayment as voided made a
  fully-credited bill look "paid only by our write-back" and left it payable.
  "Settled outside FeohLedger" is now judged from the BALANCE (`TotalAmt −
  Balance` beyond what our marked BillPayments applied), and a credit-settled
  bill opens the blocking flag instead of being booked as a (1099-reportable)
  cash payment.
- *A stateless window loses multi-day gaps.* The sweep keeps a per-connection
  high-water mark of its last COMPLETE pass and reads from it; with no mark, a
  mark past CDC's 30 days, or a CDC answer too full to trust, it reads back
  every bill still payable here instead. A reconnect starts with no mark.
- *Voiding an ERP-reported payment* reopened the invoice while QuickBooks still
  showed the bill paid and would never report it again: the void now opens the
  blocking flag in the same transaction.
- *A void's ERP leg must survive a crash*, so the pending ERP void is written in
  the void's transaction and the sweep finishes it; and the write-back re-reads
  the payment's status, posts the settled amount, and runs for an accepted
  short settlement too. A run item may not name the record-only `card` rail.

**The remaining window is closed at dispatch (§296):** between a payment made
in QuickBooks and its webhook (seconds) or the next sweep, a FeohLedger run
could still execute the same bill; every dispatch now re-reads the bill live.
Status sync and connection health are separate flags but meant to run
together: a lost connection is skipped by the sweep and only the health sweep
tells anyone.

## 296. A payment re-reads its QuickBooks bill immediately before the rail, and an unreadable bill is held (2026-10-20)

**Context.** §295 brings a payment made in QuickBooks back to us by webhook
(seconds) or by the change-data-capture sweep (hours), and only then does the
invoice leave our payment queue. A run approved before that — or the second
item of a run whose first item is at the rail when the bookkeeper pays — still
went out: the bill was paid twice, and the blocking flag the ERP-paid path
raises afterwards only stopped what had not yet been dispatched. No local
check can close this, because the fact that changed lives in QuickBooks.

**Decision.** `services/erp_status_sync.erp_bill_dispatch_refusal` runs at
every point where money is about to move, with the invoice row locked:
`api/payments._execute_single_payment` — which every rail dispatch reaches
(`/execute`, `/resume`, `/retry-failed`, a standalone payment,
`/compliance/release`, the virtual-card leg) — after
`payment_runs.dispatch_preflight` has re-confirmed the local facts (still
payable, no open payment-blocking exception, vendor active, amount still owed)
and BEFORE the rail adapter; the NACHA export, since the bank's upload of the
file is the money moving; and `POST /api/cards/generate` before it mints, since
a minted card is spendable the moment it exists.

- It reads only for an ERP whose bills sync back (`status_sync_active`) and an
  invoice pushed there under the CURRENT connection realm. An invoice never
  pushed, or pushed before §295 (closed at `done`, never payable here), is not
  read.
- **Open** — nothing taken off the bill by anyone — pays. **Anything else**
  (paid, part-paid, credited, deleted, a state we cannot classify) refuses the
  item `erp_bill_not_open:<state>` and opens the blocking
  `payment_reconciliation` in the same transaction, so neither a later run nor
  `/retry-failed` pays it until a person clears it. The rest of the run
  proceeds: one bill's state says nothing about another's.
- **Paid only by our own write-back while a FeohLedger void of it is recorded
  as pending** is **held** `erp_bill_void_pending`, retry-safe, with no
  exception: nothing outside us paid it, the pending-void sweep resolves it,
  and a flag a person must clear would be a false alarm. "Paid only by us"
  with NO pending void is unexplained and takes the blocking path.
- **A bill that can never be read under this connection** — the push named no
  document, recorded no company, or lives in a company other than the one
  connected now (a sandbox → production switch) — is held
  `erp_bill_unlinked:<why>` with a non-blocking `erp_reconciliation`: a retry
  cannot clear it, so the way out (record it as paid outside FeohLedger, or
  re-post) needs an owner.
- **When the bill cannot be vouched for right now** — a network error, an
  Intuit 5xx/429/401, a lost connection, or a read past
  `FEOH_ERP_DISPATCH_CHECK_TIMEOUT_SECONDS` (10 s, the whole read: the bill and
  its payments) — the item is **held**: refused `erp_bill_unverified`,
  retry-safe, no exception. A connection-level failure holds that org's further
  checks for a minute without reading, so a degraded QuickBooks costs a long
  run one timeout, not one per item with each invoice locked; an error about
  one bill (a 400) holds that item alone. A token refresh cut short by the
  bound is safe: `erp_oauth.get_access_token` runs the rotation and its
  persistence as one shielded task, so a refresh Intuit has answered is always
  stored. Argued:
  - *Paying blind* re-opens exactly the double payment this exists to close,
    and the failure is most likely precisely when QuickBooks is busy or
    degraded — month-end, when bookkeepers pay bills. A double payment is a
    recovery from the supplier; a held payment is a day's delay that
    `/retry-failed` ends as soon as Intuit answers, with nothing to undo.
  - *Raising the blocking exception* on a failed read was rejected: nothing is
    known to be wrong, and a flag that needs a person (with segregation of
    duties) to clear an Intuit outage would train people to clear flags
    unread. The refusal reason on the payment row, the run's partial status and
    the audit row the dispatch loop writes for every attempt already make the
    hold visible.
  - *Retrying the read inline* was rejected: the invoice row lock is held, and
    the bounded single read is what keeps that lock short.
- **Idempotent and audited.** The check is a read plus a de-duplicated
  exception; the refusal is recorded by the dispatch loop's per-payment
  `payment.*` audit row like every other pre-rail refusal; both reasons are in
  `payment_runs._RETRY_SAFE_FAILURE_PREFIXES` because no order exists at the
  processor.

**Accepted residual.** For a rail dispatch and a card mint, the read and the
money moving are milliseconds apart, not atomic: a bill paid in QuickBooks in
that gap still goes out, and §295's webhook then raises the blocking flag on
anything left. Closing it would need a lock QuickBooks does not offer. **A
NACHA file is different:** the money moves when a person uploads it to the
bank, hours or days after the export's read, and nothing re-reads the bill in
between. The record-only pilot's customer is asked to upload promptly; the
export's own read is what this change can guarantee; a re-check between export
and upload, and reading the file's bills concurrently rather than one after
another under the run and invoice locks, are tracked in `docs/followups.md`.
The old "refuse while the last sync is stale" idea is unnecessary — the live
read is fresher than any sync. The check follows
`FEOH_ERP_STATUS_SYNC_ENABLED`: an operator who turns sync off leaves
`posted_in_erp` invoices payable with no read, by design — the flag is the
statement that QuickBooks is in the loop.

## 301. The bundle budget and the security scans gate merges through `CI gate`, called from ci.yml (2026-10-20)

**What was wrong.** Branch protection on `main` requires one status check,
`CI gate` (the `ci-gate` job in `ci.yml`), which fans in on every job in that
file. Two workflows ran on pull requests from files of their own —
`web-bundle-budget.yml` and `security.yml` (CodeQL Python, TypeScript/JavaScript
and GitHub Actions, plus Trivy on the backend image) — so the gate never saw
their results. A red one was advisory in practice: PR #611 merged with the
budget red and left `main` failing it, though the budget file's own header said
"a PR that pushes either metric over its budget fails".

**Rejected: make them required checks too.** The budget is path-scoped. A
required check whose workflow never triggers never reports, and GitHub holds
the PR as "expected — waiting for status" forever. Every PR that leaves
`frontend/` alone would have hung. That is the same reason `ci.yml` has no
`paths-ignore` and does its scoping inside the run (the `changes` job).

**The rule.** A check that lives in another workflow file gates merges only by
being *called* from `ci.yml` (`on: workflow_call`), from a job `ci-gate` needs.
Both files are now reusable workflows, and `ci.yml` calls them as
`bundle-budget` and `security`.

- **Path scoping moves into `changes`.** The budget's old `paths:` list
  (`frontend/**`, its own workflow file) is the new `bundle` filter, plus
  `ci.yml` like every other filter. An unrelated PR **skips** the caller job;
  `ci-gate` counts `skipped` as a pass and anything else — `failure`,
  `cancelled` — as a fail, unchanged. If `changes` itself fails, the caller is
  skipped, but `ci-gate` needs `changes` directly, so the gate still fails.
- **Security runs on every PR and every push to main, with no filter.** It
  used to skip docs-only PRs (`paths-ignore`). Keeping that would need a
  negated filter, and `dorny/paths-filter`'s default `some` quantifier cannot
  express "anything except docs"; switching the quantifier would change every
  other filter. CodeQL on a docs-only PR costs two minutes. Not worth a
  second mechanism.
- **Trivy comes along.** It is in the same file. It runs with `exit-code: 0`, so
  only an image that fails to build (or a broken scan step) can turn it red, and
  a backend image that does not build should block a merge.
- **The weekly schedule stays in `security.yml`.** No commit starts a CI run on
  Monday morning, and a full CI run to rescan for new CVEs would be waste. So
  `security.yml` keeps `schedule` alongside `workflow_call`. Nothing else
  triggers either file, so no job runs twice.
- **Permissions are granted at the call site.** A called workflow's token is
  capped by the calling job, and `ci.yml` defaults to `contents: read`, so the
  `security` job grants `security-events: write` (SARIF upload) and
  `actions: read` (CodeQL's workflow-run lookup) itself.
- **The budget's steps, comments, ceilings and change log are untouched.**
  Only the trigger changed, so the error messages that tell a contributor to
  edit `web-bundle-budget.yml` still point at the right file.

**CodeQL timeout: 10 → 20 minutes, no retries.** One CodeQL Python run hit
the 10-minute timeout on 2026-10-20. Its log shows it was not hung. Extraction
progressed the whole time, at about ten times the normal per-file cost: 943
files in 8.5 minutes, where a healthy runner did 1431 in 83 seconds. Over the
previous 60 runs the three CodeQL jobs took a median of 0.7–1.5 minutes and a
maximum of 6.6. Ten minutes was about 2× a slow-but-healthy run, too little
for a check that now blocks merges. Twenty absorbs a degraded runner and still
stops a real hang long before GitHub's six-hour default. Retrying was
rejected: a second attempt would hide a genuine hang instead of surfacing it.
Trivy keeps 10 (median 0.9, max 3.8).

**Transition note.** CodeQL keys an analysis by workflow file and job, so the
first runs from `ci.yml` start a new configuration beside the old
`security.yml` ones. The weekly schedule still runs from `security.yml`. Alerts
are matched across configurations, so this changes which configuration an
alert is reported under, not whether it is reported.

## 302. The invoice carries its ERP bill id, and the deep link is built once, at push time, by the backend (2026-10-20)

**Found live against Xero.** FEOH-INV-001 was posted and Xero returned its
InvoiceID, but the id went only to `workflow_instances.state_data
["erp_reference"]` and the `invoice.erp_confirmed` audit row. The invoice modal
read its ERP reference off the *latest* ERP audit row, which on a success is
`invoice.completed` (no reference), and hid the ERP section at `done` anyway. A
user had no way from the invoice to the bill.

**The rule.** `invoices.erp_document_id` (migration 0118) holds the bill of
record, written by `services/erp._call_erp` beside the existing state_data
write — the one place a push is confirmed. It takes the adapter's
`erp_document_id` only: a document *number* is not addressable (no deep link,
no API lookup), and `"UNKNOWN"` names nothing. A payment void does not clear it
(the bill is still in the ERP), and every later confirmed push overwrites the
pair. A push that confirms without reporting an id CLEARS both columns rather
than keeping the earlier pair. Keeping it was the first draft, on the grounds
that the push idempotency resolves a re-push to the same bill, but that only
holds within one ERP. After an org switches ERP, the kept pair would link a
money record to another system's bill. A failed push never writes it: a bill a
failure left behind is the `erp_orphan_document_id` state, not the bill of
record.

**The deep link is stored, not derived on read.** `invoices.erp_document_url`
comes from `erp_adapters/deep_links.py`, the one provider → URL mapping, at
push time. Rejected: a frontend helper over a stored provider name. The link
depends on the adapter config that posted the bill — QuickBooks sandbox and
production companies are on different hosts — which only the backend holds,
and an org may change ERP after posting, so deriving it on read from the
*current* config would point an old bill at the wrong system. Mobile and any
other client also get the link with no copy of the mapping. Only Xero and
QuickBooks Online have one; Merge.dev's id is Merge's own, and the other
adapters' web UIs are not addressable by the id alone, so they show the id with
no link. Both URLs are documented by neither vendor: Xero's
`AccountsPayable/View.aspx?InvoiceID=` matches the page Xero's own MCP server
targets (which wraps it in an organisation-choosing redirect; not done here —
`docs/followups.md`), and QuickBooks' `app/bill?txnId=` is the bill page's own
URL.

**Backfill.** 0118 copies `state_data->>'erp_reference'` where the invoice has
exactly one workflow instance and the value is not `UNKNOWN`. That value is the
adapter's id on every adapter that returns one; only a success with a number
and no id stored the number, which cannot be told apart afterwards and is
accepted. The URL is not backfilled: the tenant database does not record which
adapter posted the bill.

**Where it is shown.** Both columns are on `InvoiceResponse`, which the web
invoice modal and the mobile ERP panel read. Each shows the reference at `done`,
and after a void, as well as at the ERP statuses. Mobile shows the id without a
link (`docs/followups.md`). The supplier portal's invoice schema and the public
`/api/v1` contract deliberately leave both out. The ERP bill is our ledger's
record, not the supplier's, and adding a field to `/v1` is a published-contract
change that needs its own decision.

## 303. An adapter reads a bill as paid only on the ERP's own paid status or a payment it records (2026-10-21)

**The problem.** `get_invoice_status` mapped a zero balance to `paid` in three
adapters (SYSPRO, Sage Accounting ZA, QuickBooks), and Sage Intacct and
Blackbaud FE NXT did the same as a fallback when their own status word still
said open. A credit note, a write-off or a zero-total bill also leaves nothing
owing, so anything recording an ERP-reported payment from that answer would
book money that never moved, as a 1099-reportable cash payment. The open
follow-up named this as the precondition for wiring any status poll.

**The rule.** `paid` is a claim that money left the customer. An adapter returns
it only on the ERP's own paid status, or on a payment the ERP records against
the bill:

| Adapter | `paid` when | Nothing owing otherwise |
|---|---|---|
| QuickBooks Online | `Balance == 0`, `TotalAmt != 0`, and a `BillPaymentCheck` / `BillPaymentCreditCard` in `LinkedTxn` | `settled_no_payment` |
| Xero | `Status == PAID` and `AmountPaid > 0` (credit notes count in `AmountCredited`) | `settled_no_payment` |
| Sage Accounting ZA | `AmountDue == 0`, Sage's own `Paid` flag, and a non-zero `Total` | `settled_no_payment` |
| SYSPRO | never: `ApInvoice` states only a balance | `settled_no_payment` |
| Sage Intacct | `state == paid` | `open` until the state catches up |
| Blackbaud FE NXT | `status == Paid` | `open` until the status catches up |

`settled_no_payment` is a new `ErpInvoiceStatus` member. It means "nothing is
owing, and no payment can be seen", and a caller must flag it for a person, not
record a payment. Intacct and FE NXT return `open` instead because a lagging
status word is a timing issue that the next read resolves, not a different fact.

**Relation to §295.** QuickBooks' status sync-back does not use
`get_invoice_status`. It reads `bill_report`, which judges "settled outside
FeohLedger" from the balance and treats a vendor credit as not cash. This entry
brings the older per-adapter read into line with that, so a later generic poll
for ERPs without `supports_status_sync` (Xero, the gap the live Demo Company
test found, which is tracked in `docs/followups.md`) starts from honest answers.
No caller under `app/` reads `get_invoice_status` today, so nothing changes at
runtime.

*Update 2026-10-26 (issue #650 S4):* the SYSPRO row was wrong about SYSPRO —
it does record payments against an invoice, in `ApInvoicePay`. SYSPRO now
returns `paid` when the balance is zero, the invoice is non-zero, and an
`ApInvoicePay` row by (`Supplier`, `Invoice`) is a payment (`TrnType` `P`,
non-zero `TrnValue`); otherwise `settled_no_payment`, as before. The payment
code is unconfirmed until a sandbox shows it, and an unrecognised code reads as
no payment, so the failure mode stays `settled_no_payment`.

*Update 2026-10-26 (PR #670 money review):* one payment row was not enough. It
proves only that some money left, so an invoice settled 40 by cheque and 60 by
a credit note, or closed by a 0.01 payment and a write-off, read `paid` for the
whole 100. SYSPRO now returns `paid` only when the absolute sum of the payment
rows' `TrnValue` equals `abs(OrigInvValue)`; any shortfall is
`settled_no_payment`. Guard: `test_zero_balance_is_paid_only_with_a_recorded_payment`.

Guards: `tests/test_erp_{quickbooks_online,xero,sage_accounting_za,syspro,sage_intacct,blackbaud}_adapter.py`.

## 304. Approval sends to the ERP when the workflow says so, and a refusal leaves the invoice approved (2026-10-20)

**Found live.** In a Xero test, tenant `acme`'s default workflow had the ERP
export step's `auto_send_on_approval: true`, the builder's "Auto-send on
approval" switch showed it on, and a manager approved FEOH-INV-001. It sat at
`approved` until someone clicked "Send to ERP". The flag was in the schema, the
templates and the builder, and nothing in the backend read it; §286 had even
written "approval never pushes" down as the rule.

**The rule.** An approval that finalises an invoice starts its ERP push when
three things hold: the invoice's frozen snapshot (never the live definition, as
for every other step) has an enabled `erp_export` step whose config holds
`auto_send_on_approval` as the literal `true`; the org has a live ERP; and the
same gates a person's "Send to ERP" meets let it through. Every approval door
counts: `review.approve_invoice`'s final level (in-app single and bulk, email,
Slack, Teams, mobile, the exception agents), `/complete`'s amount-floor
auto-approve, and the extraction worker's auto-approve. A partial chain
approval is not a final one and does not send.

**One sequence for every push.** `services/erp_send.start_erp_send` is the plan
gate (§258/§281), the export gate (§286), the `approved → sending_to_erp`
transition with its audit row, the commit, and then `dispatch_erp`, in that
order. `/send-to-erp`, `/retry-erp`, `/complete` and the auto-send all call it,
so the auto path cannot skip a gate the manual one applies or apply them in
another order.

**After the approval commits, in a transaction of its own.** The approval
doors queue the push as post-commit work (`services/post_commit`); the
extraction worker calls `send_after_approval` itself once its own commit
returns, because there post-commit work runs inline while the committing
session still holds the worker's single connection. Either way the push opens
its own session, re-locks the invoice and acts only on `approved`. Two things
follow:

- **A refusal cannot undo the approval.** The approval has already committed.
  The invoice stays `approved`, which is still payable and still sendable by
  hand, and the refusal is recorded as an `invoice.erp_auto_send_refused` audit
  row carrying `{trigger, status_code, refusal: {code, params}}`, the same coded
  refusal the manual door would have answered with. The approver's response
  says `approved` and is never a 402 or 409 for a push they did not ask for.
  Rejected: running the gates inside the approval transaction and failing the
  approval on a refusal. An open duplicate would then make an invoice
  unapprovable rather than unpostable, and the plan gate would turn a downgrade
  into a block on approving. Also rejected: transitioning to `sending_to_erp`
  in the approval transaction. Every caller that reads the status after
  `approve_invoice` (the email, Slack and Teams doors tell a partial chain
  approval from a final one that way) would have seen a status that was not
  `approved`.
- **It cannot dispatch twice.** Approval leaves `ready_for_review` and happens
  once. A retried job, or a manual send or payment schedule that got there
  first, finds a status other than `approved` under the row lock and does
  nothing. The `approved → sending_to_erp` transition under that lock is the
  single guard, as it already was for the manual doors. A bulk approve queues
  one job per transaction and sends its invoices one after another.

**A missing key is off, and so is the schema default.** The seeded and
provisioned definitions carry no `auto_send_on_approval` key, and the builder
renders a missing key as the switch off with the "users must manually trigger
Send to ERP" warning, so only the literal `true` counts. The schema default for
`ErpExportStepConfig.auto_send_on_approval` was `true`, and every definition
save runs each step through that model and stores the dump: an admin who
changed an approval threshold on a workflow showing the switch off stored
`true` without seeing it. The default is now `false`. A builder-added step and
the templates still set it on explicitly, where the switch shows it.

**Stored `true` values are not rewritten.** Before this change the flag did
nothing, so a `true` saved through the old default cannot be told from one an
admin chose. Rewriting them to `false` would undo real choices (`acme`'s, the
one that found this); leaving them means a tenant whose admin saved a workflow
since the schema existed starts auto-sending on deploy. That is an operator
call per tenant, tracked as a (b) entry in `docs/followups.md`: before
deploying, list the definitions holding `true` and confirm each with its
admin.

**What auto-send hands over.** A successful push ends the invoice at `done`,
live ERP or mock, by hand or automatically (`erp.send_to_erp_internal`), and
`done` is not payable here: the bill is paid from the ERP. With an ERP's status
sync on (QuickBooks, §295) the push lands at `posted_in_erp` instead, still
payable here, and auto-send changes nothing about that. That is the existing
meaning of "Send to ERP"; auto-send makes it happen at approval, so with the
switch on an approved invoice leaves FeohLedger's payment queue moments after
approval, before anyone can schedule it directly. The builder says so under the
switch, and `backend/docs/payments.md` notes it. Tenants that pay through
FeohLedger runs keep the switch off.

**Why the mock ERP is skipped.** For the same reason, applied to tenants with no
real ledger: the mock is the local-first stand-in (guard rail 7), and every dev
and e2e tenant runs on it. Auto-sending there would empty the payment queue
those tenants and the payment specs depend on, with no ERP to pay from. A person
can still send to it by hand.

**No person behind it.** An extraction auto-approval of an emailed invoice has
no actor, so `dispatch_erp` and the Lambda worker now carry a null `actor_id`
instead of the string `"None"`.

**The extraction worker awaits the send.** Its loop (in-process, or the
Lambda handler's) closes when the job returns, so a local-mode send started as
a task there would be abandoned at `sending_to_erp`. It passes
`inline_dispatch=True`, and the send holds that worker until it finishes,
retries included.

**`post_commit` was running at savepoint boundaries.** SQLAlchemy fires
`after_commit` when a SAVEPOINT is released and `after_rollback` when one rolls
back. Bulk approve runs each row in a savepoint, so every queued job (the
approval emails and chat posts, and now the auto-send) ran at the first row's
release, inside the still-open outer transaction, and a skipped row's rollback
then dropped whatever was queued after. A release now leaves the queue alone,
and a savepoint rollback drops only the jobs queued inside that savepoint.

**A hand-off that fails lands at `failed`.** If `dispatch_erp` raises after the
`sending_to_erp` commit (an SQS send in lambda mode), nothing will ever run that
push and `/retry-erp` takes only `failed`. `start_erp_send` now moves it there
with an `invoice.erp_failed` row, for the manual doors too, which used to
answer 500 and leave the invoice stranded at `sending_to_erp`.

**Not done here** (`docs/followups.md`): the auto-send is post-commit work, so
a process that dies between the approval's commit and the job leaves the
invoice `approved` with neither an `erp_submitted` nor an
`erp_auto_send_refused` row. Nothing is lost (it is payable and sendable by
hand), but nothing notices either; a reconciliation sweep is the durable fix.

Guards: `backend/tests/test_erp_auto_send_on_approval.py` (sends with the flag
on; not with it off, absent, the step disabled, no instance, no ERP or the mock
ERP; the snapshot decides in both directions; the floor and extraction
auto-approvals; a partial chain; an open duplicate and the plan each recorded
with the invoice left approved; a rolled-back approval sends nothing; a second
run and a bulk approve each dispatch once), `tests/test_erp_dispatch.py` and
`tests/test_erp_lambda.py` (inline dispatch in a worker scope, the null actor),
and `frontend/tests-e2e/erp/xero.spec.ts` (approving reaches fake Xero as a bill
with no click; with the flag off nothing is posted until the click).

## 305. A page offers an action from the server's verdict, computed by the gate the endpoint raises from (issue #575, 2026-10-21)

**What was wrong.** Pages decided whether to show a money-path control by
restating the endpoint's rule — a status list, a role list, `uploaded_by !==
me` — and every copy drifted in its own direction. The invoice modal checked
status + `invoice.approve`, so the uploader, a non-named approver and an
ap_manager over the CFO limit were offered an Approve that 403'd. The run dialog
offered the run's creator an Execute (and a CFO-creator an Approve as CFO) that
maker-checker refused. The exception queue checked only the status, so the
uploader of a duplicate invoice got a Resolve that 403'd. The bank-change queue
compared only `requested_by_user_id`, missing the provisioned-login SoD axis.
The vendor list hid Reject on an `active` vendor both `/reject` and bulk status
accept. #573 fixed one instance (`record_outside_refusal`); this generalises it.

**The rule.** Each covered action has ONE gate function returning an
`api/refusals.ActionRefusal` (or raising the coded `HTTPException` it already
did). The endpoint raises from it, and the read that lists the row ships
`*_refusal` / `actions` built from the endpoint's permission predicate
(`api/deps.holds_any_role` / `holds_any_permission`, which `require_roles` /
`require_permission` now call too) followed by that same gate. `null` = offer
it; a coded refusal = don't, and say why. The client reads it through
`#lib/api/actionVerdicts.ts`: unknown is a refusal (fail closed),
`role_not_permitted` hides the control, any other refusal disables it beside
its localized reason. The invoice review verdict is its own read (`GET
/api/invoices/{id}/review-actions`, `review.review_refusals`) because its gates
need the workflow instance and the receipts — the approve path was split into
`_identity_gates` / `_figure_gates` / `_chain_level_gate` so the verdict runs
exactly those, the chain on a scratch copy so reading writes nothing.

Every verdict has a parity test that asks both sides for every seeded role and
row state — the verdict and the real POST — and fails unless they agree. A
verdict compared only to itself would prove nothing.

**Two refusals moved onto the server rather than off the page.** Where a page's
restated rule was stricter than its endpoint, and the endpoint's looser
behaviour was the defect, the endpoint took the rule:

- `POST /api/invoices/{id}/approve|reject` refuse an invoice that is not
  `ready_for_review` (409 `invoice_not_in_review`). The single in-app door used
  to approve a `new` draft straight past review while the email, Slack and
  Teams doors already refused; all four now read one predicate,
  `review.awaiting_review`. `POST /api/invoices/bulk/status` with
  `status=approved` still moves a draft (`new → approved` is a legal
  transition, through the same approve gates). That is the documented
  "manager sets a batch's status" capability rather than a review decision, and
  whether it should skip drafts too is a product call this change does not
  make — tracked in `docs/followups.md`.
- `POST /api/payments/{id}/void` refuses a run's `pending` leg and
  `pending_compliance` (`payment_controls.payment_void_refusal`). A pending leg of an exported run
  could be voided back into the queue while the uploaded bank file could still
  pay it — a second payment — and voiding a compliance hold left its
  `payment_compliance_hold` exception open beside a re-queued invoice. Their
  exits are the run's (Cancel run, Bank rejected the file, Record run as paid)
  and Release / Dismiss. Cancel run releases every leg: there is no per-leg
  removal from a draft run, which is a capability this takes away rather than
  one it ever offered safely. A standalone `pending` payment (no run, booked by
  `POST /api/payments`) stays voidable — nothing will send it, and refusing the
  void would leave recording a payment that never happened as its only exit.

**Rejected: put the verdicts on the mutation responses too.** The rows are
re-read after a decision on every covered page except the vendor list's
detail-modal updates, which keep the row's verdict while its status is
unchanged and drop it otherwise (fail closed until the next read). Widening
every mutation response to carry a reader-specific verdict would put a
per-caller field on payloads other clients (mobile, the public API) share.

**Not covered here** (tracked in `docs/followups.md`): the modal's
assigned-elsewhere clause, which the server does not enforce, and the pages
that still restate a predicate.

## 306. An administrator can close an unused workspace, which releases its addresses; anything that must be kept goes to support (migrations 0119, 0120, 2026-10-20)

**The dead end.** Since §291 an address names one account in one workspace.
Someone who signs up with an address, abandons the trial, and is later added to
another workspace with it is refused `email_in_other_workspace`, and deleting a
workspace was operator-only (`scripts/delete_tenant.py`), so the only way out
was support. Reproduced end to end in `backend/tests/test_workspace_closure.py`
(provision a trial, refuse the address in a second workspace, close the trial,
accept the address; and through signup: the same address signs up again, §292's
refusal no longer finding it). Phase 0 of #614; it does not depend on
multi-workspace membership.

**The call: a self-service close for an administrator, fail-closed to the
unused workspace, and nothing else.** `POST /api/organization/close` (admin
role, not a permission: closing is not a splittable duty). It deletes the
workspace through `services/tenant_deletion.delete_tenant`, and deleting the
user rows is what frees the addresses. `GET` is the preflight the page reads
first: the blockers, and — so the irreversible action is an informed one — a
count per class of what WILL be deleted (invoices, vendors, purchase orders,
credit memos, expense reports, contracts, exceptions, deactivated accounts).

**What refuses it**, each by a stable code the page states, and the way forward
for every one is support:

- records with a retention obligation or an effect outside the workspace: an
  approved invoice (status past review, or `approved_by` set, which also
  catches one whose ERP send failed); any payment, payment run or virtual card;
  an applied credit memo; an approved or reimbursed expense report or an
  approved spend pre-approval; imported card transactions or bank statements;
  supplier statement reconciliations (`backend/docs/privacy.md` keeps their
  files as evidence); 1099 filings, VAT / GST / withholding records, Positive
  Pay files, sent or received e-invoices; a purchase order that has been
  received or closed; an approved or converted requisition or intake request;
  goods receipts and stock pushed to a commerce platform; a contract that was
  ever in force; an accepted discount; an approved supplier bank or tax change;
  a data-subject request record;
- other people's accounts: another **active** user (another administrator can
  remove the caller instead, which frees the address the same way), or a
  supplier-portal login;
- a commercial relationship: a live subscription the billing provider knows
  about, or an active / past-due one on a priced plan; a partner parent or a
  partner-managed child;
- operator-side state: a registered custom domain;
- a deployment that cannot keep the promises below: MFA switched off in a
  deployed environment (`require_sensitive_step_up` would refuse every attempt,
  so the page says so up front), no real write-once audit archive configured
  (shipping off, no sink, or `mock` among the sinks), or `lambda` audit mode
  (an audit row can still be in SQS, invisible and undrainable per tenant, when
  the database is dropped; tracked in `docs/followups.md`). Local dev and CI
  are exempt, as from every MFA check (guard rail 7);
- and, fail-closed, any check that cannot be evaluated (`records_unverifiable`).

**Every tenant table is decided, not remembered.** `TENANT_CHECKS` and
`CLOSURE_ALLOWED` (table → reason, the shape of
`tenant_deletion.CONTROL_TABLES_EXEMPT`) together cover every tenant table
exactly once, and a test fails on a table in neither, so the next table cannot
reach a closure without someone deciding whether it is a record that must be
kept. Allowed, with the reasons in the code: children of a guarded parent;
master and reference data (vendors, GL accounts, entities, policies, budgets,
catalogues) — a trial plausibly holds them, and an approved bank change blocks
on its own; configuration (workflows, reports, mappings, templates — every
invoice a template generated is guarded as an invoice); open, draft, rejected
and cancelled work of every kind, including an **untouched purchase order**:
native POs exist (`requisition_service.convert_requisition_to_po`, a
contract's create-PO, intake) and others are pulled from an ERP, but an order
nothing has happened to records an intent to buy, while everything that makes
it a transaction — a receipt, an approved requisition, a contract in force, an
approved invoice, a payment — blocks; exceptions and agent decisions (a cleared
payment-blocking exception matters once a payment exists, which blocks);
sanctions-screening results (the same); notifications and chat history; and
the audit log itself, shipped and sealed first (below).

**A contract's evidence is `activated_at`, not its status.** `/cancel` is
allowed from `active`, so a contract that bound the parties could read as a
draft; `signed_date` is optional input. Migration 0120 adds `activated_at`,
stamped on first activation and never cleared, backfilled from the first
`contract.active` audit row, else from a status that says the contract has
been in force.

**The step-up is a second factor, not the password**: `require_sensitive_step_up`
(operation `workspace_close`), as the unmasked DSAR export uses (§182). An
account with no second factor is refused, not exempted; enrolling one needs no
step-up. The slug is typed back first.

**Nothing checked can change before the delete.** The control-plane checks run
under `FOR UPDATE` on the organisation row, held until the control-plane rows
are deleted in that same transaction (`delete_tenant(control_session=…)`):
every insert referencing the organisation — an invite, a SCIM or SSO provision,
a subscription — takes `FOR KEY SHARE` on that row for its foreign key, and
every `settings` writer takes `FOR UPDATE`, so all of them wait. An UPDATE of a
row that already exists does not touch the organisation row (a billing webhook
moving the free subscription onto a priced plan, a reactivated user), so the
organisation's `subscriptions` and `users` rows are locked too. **The actor is
re-validated under that lock** — still present, active, in the organisation
and an admin — and refused (`403 workspace_close_actor_not_admin`) if not. The
tenant checks run again in a transaction holding `SHARE` locks on every guarded
table, kept open until `DROP DATABASE … WITH (FORCE)` terminates it. Either
lock not coming within 5 s is `records_unverifiable`; an unrelated writer that
collides with the table locks can fail its own transaction, accepted for a
workspace the guards say is unused. Two closures serialize on a
transaction-scoped advisory lock (`409 workspace_close_in_progress`). The
control-plane transaction stays open across the drop and the document sweep; a
server-side `idle_in_transaction_session_timeout` (none is set) killing it would
end as `workspace_close_incomplete`, which the resume path finishes. The route
also checks the account belongs to the workspace, since `get_tenant` skips a
token with no `org` claim.

**No audit row is lost with the database.** An `organization.closing` row is
written first, synchronously in every audit mode, carrying the source IP and
the actor's role names the way auth rows carry an IP. Where shipping is on, the
process's queued audit writes are awaited (`audit_dispatch.settle_auth_audits`),
the tenant's trail is flushed to the archive (`audit_log_shipper.flush_tenant`),
and the table is then **sealed**: `LOCK TABLE audit_log IN EXCLUSIVE MODE` on a
connection held until the drop. If a row committed between the flush and the
seal it is seen under the seal, which is released and the round repeats (three
rounds, then refuse). After the seal a write waits and fails with the database,
which `audit_dispatch` reports at ERROR — a write that never committed, never a
committed row the archive lacks. Any failure here is `503
workspace_close_audit_unavailable` with nothing deleted, and is logged with ids
only rather than audited into the tenant, because the audit path is what just
failed.

**The record that outlives the tenant** is a control-plane `workspace_closures`
row (migration 0119; no foreign keys, no address, no name, no IP — ids, slug,
the requester's role names, proof, counts). It reads `completed` in the same
transaction that deletes the organisation. Refusals and a closure already
running are audited into the tenant's own log (it still exists); a closure that
fails part-way records the exception class on the row (`last_failure`, the
tenant's log may be gone) and an id-only ERROR line. Its downgrade is a
deliberate no-op: the rows are the only evidence of deletions that happened and
the operator's backup queue.

**The deletion order changes for every caller: database, then documents, then
the control plane.** §179 put documents first; the reasoning there was only
that the control-plane row (which names the storage prefix) must go last, and
that still holds. But documents-first meant a drop that failed left a LIVE
workspace whose invoices pointed at deleted files. Database-first, a failed drop
leaves the workspace whole, and a failed sweep leaves a workspace that is
already unusable under a prefix the org row still names, which a re-run (or the
closure's resume path: database gone plus a `started` row) finishes. With the
database gone no upload can commit, and the sweep makes a second pass for an
object an in-flight request PUT during the first listing. `scripts/delete_tenant.py`
and `deploy/remove-tenant.sh` inherit it unchanged otherwise.

**After the commit**, the deleted accounts' Redis state is cleared
(`services/credential_cleanup`): live sessions blocklisted and their set and
metadata deleted, a pending TOTP enrollment, an email OTP, passkey challenges,
and any unspent reset / set-password link (found by scanning the reset prefix,
since those keys are digests of the token). Best-effort: the tokens are already
inert against deleted user rows.

**The slug is held** — by `utils/slug.ensure_slug_not_held`, called from
`provision_tenant` itself so signup, partner provisioning and the operator CLI
behind `deploy/add-tenant.sh` all get it — until an operator has removed the
backups (a new tenant would reuse `feoh_<slug>`, and removing the old dumps
would remove the new) **and** `CLOSED_SLUG_COOLDOWN` (90 days, covering the
backup retention cycle and so beyond the DPA's 60-day window) has passed, so
bookmarks, IdP redirect URIs, invite and approval emails cannot point at a new
owner early. Signup states it with its own code, `signup_slug_held`, not "taken".
`scripts/workspace_closures.py list` is the operator's queue (owed backups, and
closures that never finished); `mark-cleared <organization-id> --operator
<handle>` records who removed them and when (runbook:
`docs/founder-runbooks/workspace-closure.md`).

**A reused slug inherits nothing addressed to its old owner.** PEPPOL inbound
was the one route where it would have: the signing key is platform-wide, so the
path was the only binding, and it named the slug. It now names the organisation
id (`POST /api/peppol/inbound/{organization_id}`), which no later tenant can
hold; a delivery addressed by slug is the silent 204 of an unknown tenant. The
siblings were checked: email / Slack / Teams approval tokens name a user and an
invoice that exist only in the closed tenant (refused, tested); payment, card
and ERP webhooks verify a per-tenant secret deleted with the organisation;
punch-out returns match a session in the tenant database; email intake keys on
a per-tenant random token. None was changed.

**The sign-in page of a closed workspace** renders its notice
(`/login?reason=workspace_closed`) from the URL alone; its only calls, the SSO
and SAML config probes, answer 404 for the deleted tenant, which the page
already treats as "no SSO" (tested against a really-deleted tenant).

**Rejected: a sweep deleting unverified or abandoned trials past N days.**
Unverified signups never become accounts (the workspace is provisioned only at
`/signup/complete`), so they hold no address. A verified-but-idle workspace is a
customer's data under the DPA, deleted on the customer's instruction or after
termination plus notice, and "idle" is not an instruction: nothing records a
trial as ended (the baseline subscription is the free plan, `active`, with no
`trial_end`). It is not needed for the dead end either: whoever is refused
controls the address and can sign in to the abandoned workspace and close it.
Abandoned workspaces kept forever are a storage-limitation question, tracked in
`docs/followups.md`.

**Rejected: a support runbook only** — the status quo, at the cost of an
operator's destructive manual run for the commonest case, an empty trial.

**Rejected: the password as the step-up.** Factor management accepts it (§191)
because what it protects is the factor; here the output is the destruction of
everything in the workspace, which is the DSAR export's reasoning.

**Also changed:** the `email_in_other_workspace` sentence says the other
workspace's administrator can close it to free the address.

**Where its strings live (§300).** The panel's strings are `org.close.*` and
`org.section.close`, in the settings slice that /organization loads. Everything a
route without that slice can render stays in the main catalogue: the coded
refusals (`refusal.workspaceClose*`, `refusal.signupSlugHeld`), and the
`/login?reason=workspace_closed` notice. `api/codedRefusals.ts` reads a refusal's
blocker list through the key-free `types/workspaceClosureRefusal.ts`, never
`types/workspaceClosure.ts`, so the registry every route imports names no slice
key (`i18n/slices.test.ts`).

Numbered §306, after #616's §300: #611 landed as §292, and the open #612 and #613 claim §293 and §294.

Tests: `backend/tests/test_workspace_closure.py` (each record class through a
real row built from the model; what must not block; the table classification;
each control-plane and deployment blocker; the inventory; admin-only and
unauthenticated refusals; the dead end released through admin create and
through signup; the resting slug's own code at slug-check and `/complete`; the
closed workspace's spent link as the uniform 410; the confirmation; the actor
re-validated under the lock; a record written after the preflight refused under
lock with its audit row; the concurrency lock and its audit row; an unshippable
trail, a trail that keeps growing, and a row committed between flush and seal;
a drop that fails leaving the documents, recorded and finished by a retry; the
resume path; the second factor with MFA on; Redis cleared; the closed sign-in
page's probes; the operator queue, `--operator` and the cooldown; the operator
CLI refused a held slug; a reused slug inheriting no approval email; a token
with no workspace claim; a tenant write, an invite and a subscription or user
update each held off until the delete), `backend/tests/test_tenant_deletion.py`
(the order, a failed drop leaving documents, the second pass),
`backend/tests/test_peppol_inbound.py` (a slug addresses nothing),
`backend/tests/test_migration_0119_workspace_closures.py`,
`backend/tests/test_migration_0120_contract_activated_at.py`,
`backend/tests/test_audit_log_shipper.py` (`flush_tenant`),
`frontend/src/lib/types/workspaceClosure.test.ts`,
`frontend/src/lib/api/codedRefusals.test.ts`,
`frontend/src/lib/api/sessionRejection.test.ts` and
`frontend/tests-e2e/organization/close-workspace.spec.ts`.

## 307. PDFs draw non-Latin text through one escaper with font fallback; the seed carries production-shaped rows from one shared file (2026-10-20)

**Found by the production-shaped fixtures (#575 § 2).** The seed and e2e
fixtures now carry a non-ASCII vendor (`Ōsaka Seimitsu Kōgyō 株式会社 — …
Zürich`), and rendering it into the remittance advice printed
`■saka Seimitsu K■gy■ ■■■■`. Every PDF here sets text in ReportLab's built-in
Helvetica, a WinAnsi (cp1252) Type 1 font with no glyph outside Latin-1, and
each renderer had its own `_escape` that only escaped XML. The same render
showed a second defect: the remittance line table put the invoice number and
description in **bare** cell strings, so a long ERP invoice number never wrapped
(it ran through the Description column) and the description printed its
escaped `&amp;` literally.

**Decision.** `services/pdf_text.pdf_markup` is the one escaper: it escapes and
wraps each run Helvetica cannot draw in a `<font>` that can — DejaVu Sans,
bundled (~1.4 MB, Bitstream Vera / DejaVu licence) and subset-embedded, for
Latin Extended / Greek / Cyrillic; ReportLab's built-in Adobe CID fonts for CJK
(`HeiseiKakuGo-W5`) and Hangul (`HYGothic-Medium`). Supplier text in a table
cell becomes a `Paragraph`.

**Rejected: switching every PDF's base font to DejaVu.** It would change the
look of every existing document for the benefit of the few characters
Helvetica lacks; fallback per run leaves an all-Latin-1 document byte-for-byte
in its old typeface.

**Rejected: bundling a CJK TrueType face.** Noto Sans CJK ships as CFF/OTF,
which ReportLab cannot embed, and a TrueType CJK face is several MB in a public
repo. The CID fonts cost nothing and every mainstream viewer (Acrobat, Preview,
pdf.js, pdfium) supplies the glyphs. The trade: CJK glyphs are the viewer's,
not embedded, and Han characters are drawn with the Japanese face (which covers
the common Chinese repertoire). Revisit if a customer reports a viewer without
Adobe-Japan1 / Korea1 support.

**Shared values.** The long / non-ASCII / tax-exclusive values live in
`backend/scripts/fixtures/realistic_rows.json` and nowhere else: the full seed
adds them (`scripts/seed_realistic.py`), the lean seed reshapes its existing
rows with them (so no e2e count moves), and the layout specs and day-one
journey read them through `frontend/tests-e2e/fixtures/realisticValues.ts`. A
value spelled twice is how the layout spec and the demo would drift apart.

Guards: `backend/tests/test_pdf_text.py` (each renderer, with the fixture's
names, asserts no glyph outside cp1252 was drawn in a Helvetica face; the long
number wraps; `&` prints as `&`), `backend/tests/test_seed_realistic.py`.

## 308. "Approved or beyond" is one population for budgets and fraud baselines; a printed currency code is evidence, never a default (#575, 2026-10-20)

(Numbered 308, with §307 above it, because §295, §296, §301 and §302–§304
landed on `main` first and §297–§299, §305 and §306 are held by pull requests
still in flight.)

**Budget actual.** The realised leg of a budget (and the PO-relief leg, which
must admit exactly the same invoices) is `APPROVED_OR_BEYOND_STATUSES`, so it now
includes `sending_to_erp`: an approved invoice no longer drops out of `actual`
while its ERP push is in flight or stuck. It also includes `failed` **with**
`Invoice.approval_date` set — an approved invoice whose push failed is still
spend a human signed off, while an unstamped `failed` is a failed extraction
nobody reviewed (`budget_service.realised_invoice_clause`). That is the same
evidence rule §73 uses for the touchless rate and §286 for the bank-change flag.

**Fraud baselines.** The remit-to-change, statistical-anomaly and LLM-anomaly
baselines in `invoice_warnings` now read `APPROVED_OR_BEYOND_STATUSES` too,
which adds `sending_to_erp`, `sent_to_erp` and `done`. Before that, a vendor
whose history had all finished (`done`) had no baseline at all, so every check
that needs one silently skipped.

**The trade-off we accepted.** `done` is the CSV importer's default status
(`services/csv_import`), so unreviewed imported history now seeds the remit-to
baseline (and the anomaly baselines). `paid` was already in the set and already
importable, so the exposure is not new in kind. The direction it fails is the
safe one: a baseline built from unreviewed history makes the checks fire *more*
often (a remit-to that differs from an imported row raises `remit_to_changed`),
never less. An attacker who could plant a CSV row could already plant a `paid`
one.

**Why this is not §73.** §73 says `done` / `paid` are not proof of *review*,
and it is right for the question it answers: the touchless rate claims the
platform did the approving, and an imported row is evidence neither way. A
baseline asks something else: "what has this vendor historically billed us,
and where did we pay?" Accepted history answers that whether or not this
platform reviewed it, and excluding it leaves a migrated tenant with no
baseline at all, which is worse. Where proof of an approval matters (touchless
rate, budget `failed`, bank-change flag), read `approval_date`. Where the
question is history, read the status set.

**A currency code printed on an amount.** A model transcribing `GBP 9,500.00`
keeps the code. Stripping it so the magnitude parses (#575) then discarded it,
so with no `currency` field the invoice kept the upload's `USD` placeholder and
was gated as USD 9,500; `USD 1,000.00` beside `currency: EUR` was silently
EUR 1,000. Before #575 the token failed to parse, so the amount stayed empty.
That was wrong too, but it erred towards a human. The code is now evidence
(`extraction.CurrencyEvidence`), weighed with the field and never overruled by
a default (§160, §196, §200: a missing currency stays missing):

- the field and the printed codes agree, or only one of them speaks → that is the currency;
- they disagree, or two amounts print different codes → no header money is written and the row's currency is left as it was. The amount stays at `0`, so `missing_amount` fires, and `self_correction_currency_conflict` / `_mixed` tells the reviewer why;
- neither speaks → unchanged (see `docs/followups.md`: the upload still stamps `USD`).

A symbol (`$`, `€`) is not evidence, because `$` names a dozen currencies.

**Rejected: let the field win.** It is the model's summary, and the code is
the page's own text next to the figure. When they disagree there is no basis
for picking one, and either pick can be wrong by the full exchange rate.

**Found alongside it.** "Amount unset" did not actually block auto-approve.
`decide_auto_approve` never read `missing_amount`, so an extraction that left
the 0 placeholder auto-approved under any `auto_approve_below` floor (0 < floor)
or on confidence alone. It now refuses an amount `<= 0` on both triggers, the
same rule as the `missing_amount` warning.

Guards: `tests/test_budget_rollup.py::test_actual_counts_an_invoice_mid_erp_push`,
`tests/test_budgets.py::test_spend_po_relief_counts_an_approved_invoice_whose_erp_push_failed`,
`tests/test_extraction_cleaners.py` (currency evidence),
`tests/test_extraction_golden.py` (the eur_multipage read with its currency
field removed and contradicted), `tests/test_extraction_self_correction.py`,
`tests/test_extraction_confidence_contract.py::test_no_amount_never_auto_approves_on_either_trigger`.

## 309. The ERP push refuses an invoice FeohLedger is already paying (2026-10-21)

**The gap.** The run builder and the card mint each refuse an invoice the
other rail already holds (`uq_payments_one_live_per_invoice`,
`live_payment_invoices`, `live_card_invoice_ids`). The ERP push asked neither.
Staging a run books a live `pending` payment and leaves the invoice `approved`;
an `exported` NACHA run is the same; a minted card leaves it `approved` too.
"Send to ERP" then posted the bill, and a bill in the ERP can be paid there
("Mark as paid", Bill Pay). One invoice, two outflows: our rail and the ERP.
The ERP is a third rail, and it was the only one not checking the other two.

**The rule.** The push refuses while the invoice holds a live payment (any
status outside `api/payments.LIVE_PAYMENT_TERMINAL_STATUSES`) or a live virtual
card (any card not `cancelled`), with the coded refusal
`payment_blocks_erp_export`, `params = {claim: "payment" | "virtual_card"}`,
and `possibly_posted: true` on §286's terms. It is in
`services/erp_export_gate.py` beside §286's exception check, through one
function, `erp_export_refusal`, which every push asks: the three HTTP doors
(409 before their transition) and `services/erp._call_erp` (before any
adapter is built). An open duplicate / fraud exception is named first, so
§286's refusals read as before.

- **"Live" is not restated.** `erp_export_live_claim` calls
  `payment_runs.live_payment_invoices` and `card_issuance.live_card_invoice_ids`,
  the predicates the two rails refuse each other with, which mirror the two
  partial unique indexes. A second spelling is how the gates would come to
  disagree about a `pending_compliance` or `charged` row.
- **A settled card or a completed payment still refuses.** The money has
  already moved on our rail; a bill posted now is payable a second time. The
  remedy the refusal names is the rail's own exit (cancel the run, void the
  payment, cancel the card), the same one the other rail would need.
- **Why `_call_erp` asks too, not only the doors.** `sending_to_erp` and
  `failed` are outside `PAYABLE_INVOICE_STATUSES`, so once the door's
  transition commits no run can be staged on the invoice. But the run builder
  reads its invoices without a lock (the door and the card mint do lock), so
  a run staged concurrently with a send can still read the invoice as
  `approved` and book a payment after the door's gate saw none. Re-reading at
  `_call_erp`, immediately before the adapter, stops a push whose claim landed
  in that window, and it covers pushes that never pass a door: a dispatcher,
  a Lambda redelivery, a row written before this gate (a `failed` invoice
  with a live payment beside it, which `/retry-erp` would otherwise post), or
  a future entry point (the auto-send on approval in flight as PR #625 routes
  through the same gate). §286 put its check at the chokepoint for the same
  reason. The cost is two indexed reads per attempt. It narrows the race but
  does not close it; locking the run builder's invoices is the durable fix,
  tracked in `docs/followups.md`. A claim that slips through is not a double
  payment today: with status sync off the invoice closes at `done` and
  dispatch refuses the payment (`invoice_not_payable`); with it on, the
  invoice lands at `posted_in_erp` and is paid once, with a BillPayment
  written back.
- **Interaction with QuickBooks status sync (§295, §296).** Status sync covers
  the other order. With it on, a confirmed push lands the invoice at
  `posted_in_erp`, still payable here; a payment staged then is written back
  to QuickBooks as a BillPayment, and a bill paid in QuickBooks while
  FeohLedger also holds a payment opens the blocking `payment_reconciliation`
  exception. None of that passes this gate: an invoice at `posted_in_erp` (or
  `payment_scheduled` / `paid`) is never pushed again, so a payment against it
  is never refused here. With sync off, a confirmed push closes the invoice at
  `done`, so nothing is paid here after it either. With an ERP, the supported
  order is therefore *push, then pay*; *pay, then push* is what this refuses.
  That tightens a flow that used to work with sync on: staging a run and then
  pushing ended at `posted_in_erp`, where the write-back and the
  `payment_reconciliation` exception caught a QuickBooks payment after the
  fact. The refusal is still right with sync on. Between the push and our
  run's execution the bill sits open in QuickBooks and anyone can pay it
  there, and the exception only reports that payment once it has been made.
  Do not exempt sync-on orgs from this gate.

**Rejected: transition the invoice off `approved` when a run is staged or a
card minted.** That would change the payment queue, the run builder's own
payable-status check and every screen that reads `approved`, to fix a gap in
one consumer. The claim already exists as a row; the push only had to read it.

**Rejected: refuse silently (a 202 that fails a second later).** Same reason as
§286: the user would have to read the audit trail to learn why.

Tests: `backend/tests/test_erp_export_blocking_exceptions.py` (draft run,
exported run, live card, voided / failed / cancelled payment, cancelled card,
no claim, the real run builder then cancel, `/retry-erp` with
`possibly_posted`, and `_call_erp` stopping a push queued before the claim);
`frontend/src/lib/api/erpRefusals.test.ts` and `codedRefusals.test.ts` (all
six locales).

## 310. The app signs in to RDS as a least-privilege role with an IAM token, and the master password leaves the VM (2026-10-21)

**The problem.** Production connected as the RDS master user `postgres`, whose
password sat in `FEOH_DATABASE_URL` in the VM's deploy env. A leaked env file,
a backup of the VM disk, or a shell on the box was therefore a full,
long-lived login to every tenant database, as a member of `rds_superuser`.
IAM database authentication was already enabled on the instance and used by
nothing. This had to close before the first paying customer's data landed.

**What changed.**

- **Two roles, created by the master once** with `deploy/db-role-cutover.sh`
  (the SQL is in `deploy/sql/db-role-{cluster,database}.sql`):
  - `feoh_owner` is `NOLOGIN`, `CREATEDB`, and a member of nothing. It owns the
    control plane, every `feoh_<slug>` database, and everything in them.
  - `feoh_app` is `LOGIN` with no password. It is a member of `rds_iam` and of
    `feoh_owner`, and nothing else. Its `role` defaults to `feoh_owner`, so
    every session acts as the owner and every object the app creates is owned
    by the owner too.
  - Neither role has `CREATEROLE`, membership in `rds_superuser`, or superuser.
  - The cutover verifies all of this (attributes and an allowlist of
    memberships) on every run, not only when it creates the roles.
- **A token per new connection.** `backend/app/db_engine.py` is now where every
  engine and every raw asyncpg connection is made. `create_async_engine` there
  is a drop-in for SQLAlchemy's; `connect_raw` replaces bare `asyncpg.connect`.
  With `FEOH_DATABASE_IAM_AUTH` on, both pass asyncpg a callable `password`
  that signs a fresh RDS auth token. asyncpg calls it once per connection
  attempt and awaits an awaitable result, so the 15-minute token lifetime
  never matters to a pooled engine (minting once per process would break the
  first reconnect after a quarter of an hour). The same callable works through
  SQLAlchemy 2's asyncpg dialect (`connect_args` reach `asyncpg.connect`
  unchanged), so no `do_connect` event is needed, and a single mechanism covers
  engines, raw connections, Alembic and the Lambda handlers.
- **Off the event loop.** `generate_db_auth_token` signs locally, but it needs
  credentials, and botocore resolves and refreshes instance-profile credentials
  with a synchronous IMDS call. The callable is a coroutine that runs every
  boto3 step in `asyncio.to_thread`. The one RDS client is created lazily, in
  that worker thread, under a lock.
- **Fail closed.** With the flag on, a URL that still carries a password, a
  URL with no user or host, or TLS short of `verify-full` is refused: at boot
  (`Settings`), at every engine construction, and by `decrypt-env.sh` before a
  deploy. A token is a bearer credential for its 15 minutes, so `require`
  (which accepts any certificate) is not enough. The flag defaults off, so a
  laptop and CI keep password auth against the local container (guard rail 7).
- **Deploy tooling.** `deploy/lib.sh` accepts the password-less IAM URL. Each
  `pgtools` run (`psql.sh`, `backup.sh`) mints its own token on the VM with
  `aws rds generate-db-auth-token` and passes it as `PGPASSWORD` through the
  environment, never an argv. The instance role holds `rds-db:connect` on
  exactly `dbuser:<resource id>/feoh_app` (`infra/compute.tf`). `feoh_owner`
  cannot sign in at all. The master
  password lives only in `infra-secrets` (`terraform.sops.yaml`), for
  break-glass.

**pgvector is not a trusted extension on RDS.** The AWS list of extensions a
non-`rds_superuser` holding `CREATE` may install does not include `vector`
(verified against the RDS user guide's "PostgreSQL trusted extensions" list,
2026-10-21; the pgvector image's control file says the same locally). So the
cutover creates it once in `template1`, as the master. Every database the app
creates afterwards is copied from `template1` and already has it, and the
`CREATE EXTENSION IF NOT EXISTS vector` in provisioning and migration 0003 is
a permitted no-op. PostgreSQL checks for an existing extension before it checks
privilege, which was verified against a non-superuser role. The extension (and
its member objects) stays owned by the master. Ownership cannot be transferred
anyway, and updating pgvector is a master operation.

**No separate provisioning role.** The earlier follow-up proposed "a separate,
narrower path for `CREATE DATABASE`" (`feoh_owner` is not that: it exists for
the master's sake, and the app always acts as it). A provisioning role would add
no isolation. Self-service
signup and partner provisioning run `CREATE DATABASE` inside the running API,
so a second role's credential would have to be reachable from the same process
(and the same instance profile). Anyone who compromised the API would hold both
roles. What `CREATEDB` adds on top of owning every tenant database already is
only the ability to create more databases. The real reductions are the ones
made here: no `rds_superuser`, no `CREATEROLE`, and no password to steal.

**Explicit `ALTER … OWNER`, not `REASSIGN OWNED`.** `REASSIGN OWNED BY postgres`
also reassigns shared objects the master owns (the `postgres` maintenance
database, any database outside the app's set) and extension ownership, in one
statement whose reach cannot be read off the page. Here everything moves to
`feoh_owner`. The per-database SQL names
what it moves: schemas, relations (owned sequences follow their table), types,
and routines. It then fails if `pg_shdepend` shows anything else outside
extensions still owned by the master. An object class it does not handle stops
the run, so nothing is quietly left behind.

**Why two roles, and why the master never joins the login role.** RDS signs a
member of `rds_iam` in by token only, and AWS documents that inherited
membership counts. The master has to stay a password login, because it is the
rollback and the break-glass path. It also has to be able to `SET` to the
owner, since a non-superuser may transfer ownership only to such a role, and to
inherit the owner's privileges, which is what makes the rollback work. One role
cannot both carry `rds_iam` and be joined by the master. So ownership sits on
`feoh_owner`, which has no `rds_iam` and which the master joins
`WITH INHERIT TRUE, SET TRUE`. The master is never made a member of `feoh_app`
beyond the ADMIN-only grant PG16 gives a role's creator, the same link AWS's own
"`CREATE USER …; GRANT rds_iam …`" procedure leaves. The cutover refuses to
finish if the master is a member of `feoh_app` with INHERIT or SET. (The first
draft made the master an inheriting member of a single `feoh_app`. The security
review caught that this could make the master IAM-only on RDS, part-way through
the cutover itself.)

One consequence: `DROP DATABASE … WITH (FORCE)` terminates the database's other
sessions. Postgres allows that only to a role holding the privileges of the role
that *signed those sessions in*, and `feoh_owner` does not hold `feoh_app`'s.
`tenant_provisioning._drop_postgres_database` therefore runs `SET ROLE NONE`
first. As the session user, `feoh_app`, it has both its own sessions and the
owner's privileges. `RESET ROLE` would not do this, because it returns to the
role default, `feoh_owner`. Locally the statement is a no-op.

**The cutover's own queries resolve names from the catalog only.** After the
cutover the app owns `public` in every app database. A `public.format(text,
text, text)` planted there would match more exactly than
`pg_catalog.format(text, VARIADIC "any")`, and so it would run with the master's
privileges on the next re-run (the CVE-2018-1058 shape). Both SQL files set
`search_path = pg_catalog, pg_temp`, and the script passes the same setting to
every psql it runs through `PGOPTIONS`. A test plants exactly that function.

**Rollback is trivial by construction.** The master inherits the owner's
privileges. Pointing `FEOH_DATABASE_URL` back at the master, with
`FEOH_DATABASE_IAM_AUTH` off, just works, migrations and tenant provisioning
included. Objects created during that window belong to the master, and
re-running the cutover hands them back.

**What stays with the master (break-glass).** A logical `restore.sh` recreates
each database from `template0`, which has no pgvector, so it refuses in IAM mode
and runs with the master URL, followed by a re-run of the cutover. A
point-in-time restore keeps the roles, and is still the first choice. The
`rds-db:connect` ARN names the instance's resource id, so a PITR into a new
instance needs the grant re-applied. Rotating the master password no longer
touches the VM.

**No migration.** Roles and database ownership are cluster-level. Nothing in any
schema changes, so there is no Alembic revision.

**Verified.** Every engine site moved to the factory, and
`tests/test_db_engine_chokepoint.py` fails on a new direct
`create_async_engine` / `async_engine_from_config` / `asyncpg.connect` under
`app/`, `scripts/` or `alembic/`. `tests/test_db_engine.py` covers the factory:
a token per new connection against the real local Postgres with the generator
stubbed, minted off the loop, and the fail-closed rules.
`tests/test_db_role_cutover.py` runs the cutover SQL as a non-superuser
master-like role on throwaway databases, and covers the script's wiring.
Deploy-side coverage is in `test_deploy_db_mode.py` and
`test_deploy_decrypt_env.py`. End to end against a throwaway pgvector cluster,
with a non-superuser master: the deploy migration and a tenant provisioned as
the master, then the cutover, then a second run (a no-op), then, as `feoh_app`
(acting as `feoh_owner`), the migration step, a new tenant, downgrade and
upgrade of a pre-cutover tenant, DDL and queries, and `delete_tenant.py` with
another app session open on the database (the `WITH (FORCE)` path). For the
rollback, the master downgraded, migrated and provisioned a tenant, and the next
cutover run handed that tenant over. The
operator rollout is `deploy/README.md` § Database role.

## 311. The site is checked from outside by Route 53, and only customer-facing failures text the phone (2026-10-21)

**The gap.** Every alarm in `infra/` watched AWS's view of its own resources:
the VM's status checks, RDS CPU, memory and storage, CloudFront error rates. A
wrong DNS record, a Caddy that stopped proxying, an API container that exited
on a healthy VM, or a distribution serving the wrong thing moved none of them,
and all of them only sent email. Operator to-do #446 asked for uptime
monitoring that reaches the phone before customer #1.

**Route 53 health checks, not a monitoring vendor.** Route 53's checkers make
real HTTPS requests from several AWS regions, live in the same Terraform and
state as the stack they check, and need no account, API key or extra
sub-processor. The public status page is still a vendor call
(`founder-runbooks/support-and-status.md`). A vendor that runs its own monitors
can sit beside these checks. It does not replace them.

**Three hosts, one per way to be down.** The apex (the SPA), one tenant host
(the wildcard record, the wildcard SAN and, before the CloudFront cutover, that
tenant's Caddy block), and `api.<domain>/api/health`. The tenant slug is a
variable with no default, so the public repo names no customer. Unset skips
that check.

**HTTPS everywhere, a body match only on the API.** An HTTP check would follow
nothing and pass on the redirect. HTTPS, string matching and the fast interval
each bill per check. The API's match on `"status":"ok"` earns its cost: the
SPA router answers every path with `index.html`, so if `api.` ever resolved to
CloudFront a status-only check would read healthy. The SPA hosts already fail
on the status code or the handshake in every way a person would notice (an
empty bucket, a missing object, a stopped Caddy, a host with no certificate).
`backend/tests/test_sweep_health.py` reads the string out of `uptime.tf` and
checks it against the bytes `/api/health` returns, so reformatting either side
fails CI instead of paging for an API that is up.

**30-second interval, 3 failures, 3 regions.** About 90 seconds of sustained
failure before a check reads unhealthy, which a deploy's few-second container
restart never reaches. The 10-second interval would cut that to about 30
seconds, at $1–2 more per check, for a pilot with no uptime commitment.
Three regions is Route 53's minimum. They don't lower the price, which is per
check, but every checker is a request against CloudFront's free allowance and
the API, and three regions on two continents still tell a regional network
fault from an outage. All in, about $8–13 a month with the toll-free number.

**us-east-1, by construction.** Route 53 publishes health-check metrics only
in us-east-1, and an alarm can notify only a topic in its own region. The
alarms and the pager topic are pinned to the existing `aws.us_east_1` alias.
`frontend.tf` already refuses any other `aws_region`, which keeps the alerts
topic and the database alarm that pages there too. No new provider, so the lock
file is unchanged.

**A separate pager topic, and only customer-facing alarms on it.** Putting an
SMS subscription on `alerts` would text CPU-credit warnings at 3 a.m. Instead
the uptime alarms and `db-storage-low` (past it writes fail, and no uptime
check notices first) notify both `pager` (SMS) and `alerts` (email). Everything
else stays email-only. CPU, memory and credits are sizing and cost signals. A
failed VM status check, or a CloudFront 5xx surge customers can see, fails an
uptime check anyway, so paging it too would send two or three texts per
incident and teach the operator to ignore the phone.

**Missing data is an outage for the uptime alarms, not for the certificate
alarm.** Route 53 emits `HealthCheckStatus` every minute while a check exists,
so silence means the check or the metric pipeline is broken. An uptime alarm
that goes quiet when it cannot see is the failure this exists to remove. The
cost is one page right after the first apply, which doubles as a delivery
test. ACM's `DaysToExpiry` is daily with drifting timestamps, so an empty
window there is normal and reads `notBreaching`.

**Certificate expiry: ACM covered, Caddy tracked.** Route 53 does not validate
certificates, so an expired one passes. ACM renews the platform certificate
from 60 days out on its own. An alarm at under 30 days catches a renewal that
failed (validation CNAME deleted, a CAA record) with a month left, by email.
The Let's Encrypt certificates Caddy holds on the VM emit no AWS metric. That
gap is a (c) follow-up (a Synthetics canary) rather than half-built here.

**The phone number stays out of the repo, and its absence is safe.**
`alert_phone_number` has no default, is `sensitive` so plans print
`(sensitive value)`, and is validated as E.164. Unset creates no subscription,
leaves the account's SMS settings alone and adds no delivery alarm, so
plan, validate and the offline tests need no number. The `count` reads
`nonsensitive(var.alert_phone_number != null)`, because whether a number is set
reveals nothing and `count` refuses a sensitive value.

**US SMS has prerequisites Terraform cannot do.** A new account is in the SNS
SMS sandbox, which delivers only to numbers verified in the console. That is
fine to stay in, since the operator is the only recipient. US carriers also
refuse SMS without a registered origination identity. A toll-free number is
the cheapest: $2 a month and about 15 days to register, as a sole proprietor
with no EIN. Registrations since 2026-09-15 need privacy and terms URLs, which
`/legal/` already publishes. SNS then needs a resource policy on the number.
These are console steps (`infra/README.md` § Uptime checks and paging) and a
(b) follow-up. Leasing the number through Terraform was rejected: the
registration form carries the operator's address and contact phone, which
belong in neither the public repo nor its state. A text SNS cannot deliver
fails silently (unregistered number, unverified destination, the $1 default
monthly spend limit), so with a number set an alarm on the pager topic's
`NumberOfNotificationsFailed`, which counts an SMS once SNS stops trying to
deliver it, emails the operator, and a second emails at 80% of the spend limit
(`SMSMonthToDateSpentUSD`), since past it SNS stops texting for the rest of the
month. A carrier dropping a text SNS handed over is invisible to both, which is
why the README ends with a test-fire. `aws_sns_topic.pager` also carries its
own `aws_region == "us-east-1"` precondition: `db_storage` sits on the default
provider and publishes to it, which only works in the same region.

**Tests.** `infra/tests/guardrails.tftest.hcl` pins the hosts, paths, body
match, interval, threshold and regions, the tenant check appearing only with a
slug, every uptime alarm paging and emailing on failure and recovery with
missing data breaching, which alarms may page, the SMS subscription, SMS
preferences and both SMS alarms existing only with a number, the E.164 and
slug validations, the region precondition, and the certificate alarm.

## 312. The landing page follows the visitor's theme, with a light render of the tally (2026-10-21)

**This supersedes §289's "the marketing landing page stays dark" and §294's
"not until evidence says otherwise".** §294 named what would reopen the
question — drop-off or repeated feedback — and what doing it properly would
mean: a light render of the tally, panel treatments composed for a light
ground, contrast re-measured, the theme followed rather than toggled per page.
What reopened it is the operator asking for it directly: a visitor who chose
Light (or whose OS did) met a navy landing page between a light sign-in page
and a light app, and on the apex domain that page is the product's first
impression. The job was done the way §294 said it must be, not as a token
swap that leaves a dark-composed render on white.

**What changed.**

- **No pin.** `data-theme-scope="dark"` is gone from `Landing` and from
  `app.css`'s dark selector list; nothing else used the mechanism, so it was
  removed rather than kept idle. The landing subtree (`Landing`, `Pricing`,
  `HeroPipeline`, `AdapterRail`, `Atmosphere`) takes System / Light / Dark
  from the same pre-paint script and store as everything else, so a stored
  Light is on `<html>` at the first frame here too.
- **Every colour is a token.** 85 literals — navy glass `rgba(24, 26, 35, …)`,
  accent glows, the text-gradient stops, the stamp and ping greens — became
  palette tokens or `color-mix(in srgb, var(--token) N%, transparent)`. Three
  tokens are new, in both palettes: `--gilt` (the tally's gold), `--brand-violet`
  (the gradient midpoint) and `--drop-shade` (a cast shadow: black on navy, a
  soft ink on light, where black at the same strength reads as dirt). The dark
  values are the old literals, so the dark page is unchanged to the eye.
- **Light treatments, not an inversion.** Glass panels are translucent
  `--surface` (white, in light) with `--shadow-card`, so they lift off the pale
  ground by shadow rather than by being lighter than it; the hero card is
  `--surface` with `--shadow-float`; the eyebrow pills moved onto the
  calibrated `--accent-tint` / `--accent-on-tint` pair; the grid, grain and
  vignette already followed `--text` / `--bg`. The brand mark is untouched.
- **A second render of the tally.** On the light ground the dark render's
  softbox reflections came out as near-white cream, the page's own value, and
  the stick's near edges dissolved. `tally_scene.py` now takes
  `TALLY_VARIANT=light`: dimmer softbox, ~0.45 stop less exposure, a warm weak
  fill and a lower rim, everything else shared — so the two renders trim to the
  same 1100 × 460 frame and swapping them never moves the layout.
  `gen-marketing.sh` renders both (or one, by argument, so retuning one
  lighting leaves the other's bytes alone) and refuses a pair that disagrees in
  size. The page picks the file from `theme.resolved`
  (`lib/marketing/ornament.ts`) rather than a `<picture>` media query, which
  would follow the OS and ignore a stored choice; `AuthShell`'s panel, which
  already followed the theme and was showing the dark render on white, uses the
  same map. The grain overlay is shared — a grey tile under `overlay` blending
  reads as grain on either ground — so it was not re-rendered.
- **A theme switch in the header.** An anonymous visitor has no `/profile`.
  `ThemePicker` and `SegmentedControl` gained `compact`: the same fieldset of
  native radios drawn as three icons, legend and labels visually hidden but in
  the accessibility tree, each segment titled, 30px targets, the radio clipped
  (not faded) and the segment carrying the focus ring. With the dot hidden the
  raised fill was no cue at all (1.04:1 against the track), so the chosen
  segment takes an `--accent` ring on the accent tint pair — an edge the other
  segments lack, ≥5.7:1 against the track in both palettes (1.4.11, 1.4.1). It sits beside the
  `<nav>`, not in it — a setting, not a destination — and fits beside the mark
  and the short CTA at 320px. No new strings: it reuses `profile.appearance.*`,
  translated in all six locales.

**Rejected.** *A page-local toggle* (a landing-only light/dark switch that
does not touch the global choice) — §294 already ruled it out, and a choice
that reverts on the next page is not a choice. *Keeping the scope mechanism for
later* — an unused pin is an exemption from the light audit waiting to be
reused. *CSS-swapping two `<img>` elements* — needs the light selectors written
twice more (explicit and System) for one image, where the store already knows
the answer.

Guards: `src/lib/a11y/tokenPairing.test.ts` (both palettes over every
marketing stylesheet with no exemption; no `data-theme-scope` anywhere; the
marketing components type no colour literal — hex, any functional notation,
`white` / `black`, in `<style>` or an inline `style=` — but `#fff` in a rule
that also sets a `-strong` background, and a mask's `#000`; `--gilt`, `--brand-violet` and `--accent-on-tint` held to 4.5:1
on `--bg` and `--surface` in each palette, since the scan cannot measure a
`background-clip: text` gradient), `src/lib/marketing/ornament.test.ts` (both
renders committed, same intrinsic size, not the same file), and
`tests-e2e/a11y/light-theme.spec.ts` (the landing under a light OS: axe clean,
light render; dark: axe clean, dark render; a stored Light under a dark OS:
`data-theme` at the first frame, the ornament's first `src` the light file and
the dark one never requested;
the header switch reached by Tab, operated by arrow keys and by pointer,
persisting across a reload, segments ≥ 24px, a drawn focus ring, a checked
edge the others lack; at 320px the header holds mark, CTA and switch with no
overflow; the auth shell's panel follows the theme).

Numbered §312, with this branch's §313–§315 after it: they were drafted as §306–§309, main took §306–§308 and §310 first, and the open #631 and #634 claim §309 and §311.

## 313. Error toasts stay until dismissed; other toasts pause while hovered or focused (issue #635, 2026-10-21)

**This supersedes the fixed five-second timer `frontend/docs/ui-patterns.md`
documented as deliberate** ("auto-dismiss timer kept"). Decided by the
operator's delegate on #635 finding 24.

**What was wrong.** Every toast vanished five seconds after it appeared,
whatever it said and whatever the reader was doing. Several screens used a
toast as their only error signal, some with an instruction in it (#6, #20,
#21 on the issue); a screen-reader user hears it once, a slow reader may not
finish it, and someone moving the pointer onto it to read it watched it go.
That is WCAG 2.2.1 Timing Adjustable: content with a time limit needs a way
to turn it off, adjust it or extend it.

**The rule** (`frontend/src/lib/utils/toastQueue.ts`, rendered by
`ui/Toast.svelte`):

- An **error** toast has no timer. It stays until dismissed with its visible,
  keyboard-reachable dismiss button. An explicit `duration` does not make an
  error transient — no caller passed one, and an error that can be timed away
  is the defect.
- Any **other** toast keeps its timer (5 s, or the caller's explicit
  `duration` — the sync report's 12 s warnings keep theirs), but the clock
  **stops while the pointer is over a toast or focus is inside the region**
  and resumes with the time it had left. A toast that arrives during a pause
  starts its clock on resume.
- **Persistent errors are bounded.** At most three stay on screen; a fourth
  removes the oldest. The same error text arriving again replaces its earlier
  copy instead of stacking — a page retrying a failing call cannot fill the
  corner with identical cards.
- **Live-region behaviour is unchanged where it matters.** Errors still go in
  the assertive container and the rest in the polite one. Pausing changes no
  DOM, so it announces nothing; a repeated error is a fresh insertion, so it is
  announced again rather than silently deduplicated.
- **Focus never falls to `<body>`.** Dismissing from the button moves focus to
  the next toast's dismiss button, or to `<main>` when it was the last.
- The region is named **"Status messages"**, not "Notifications": that is the
  sidebar bell's name, and two landmarks sharing one name are
  indistinguishable in a landmark list.

**A toast is still not a load-error state.** A page whose data did not arrive
says so in place with `ui/LoadError` and a Retry; the vendor list (#635
finding 20) and the notification bell (finding 22) moved off toast-only
failures in the same change. The persistent error toast is for an action's
outcome.

**Rejected: a longer fixed timer** (10 s, 15 s). It moves the line rather than
removing it, and 2.2.1's "extend" option needs the reader to be able to ask
for more time, which a fixed timer cannot give. **Rejected: making every toast
persistent.** Success and info toasts confirm what the reader just did and
the page already shows; requiring a dismissal for each would bury the errors
that do need one.

**Sign-off.** This changes a documented accessibility pattern
(`docs/accessibility.md`, `frontend/docs/ui-patterns.md` § Toast); it should
be confirmed by whoever owns `docs/accessibility.md`.

## 314. The sidebar's groups say what is in them: Treasury, Subscription under Settings, Cash-flow analytics (issue #635, 2026-10-21)

**Decided** by the operator's delegate on #635 finding 23 · `frontend/src/lib/nav.ts`

A new admin met about thirteen sidebar rows and forty-odd destinations on day
one, and three of the names misled:

- **"Billing" held the customer's payables money tools** — Credit Memos,
  Discounts, Positive Pay, Bank Reconciliation, vendor Statements — beside
  *our* subscription page. Read cold, "Billing" is the vendor's bill or the
  platform's invoice to you, not treasury work. The group is now
  **Treasury** (`nav.group.treasury`; the old `nav.group.billing` key is gone,
  so nothing can keep rendering the old name).
- **Subscription moved to Settings, directly after Organization.** It is the
  platform's plan and usage page (`/billing`, control plane), which describes
  the workspace itself — the same kind of thing as Organization — and is
  nothing to do with paying suppliers. It keeps its gate (admin | cfo, the
  backend's `GET /api/billing/subscription`). A CFO, who can see no other
  Settings child, now sees a Settings row that lands on Subscription; per-item
  gating already handles a one-child view (the group row is the page).
- **`/cfo` is "Cash-flow analytics"**, so it reads as distinct from
  "Cash-Flow Copilot" beside it: one is the forecasting dashboard, the other a
  conversation. Only the nav label changed (`nav.cashFlow`); the page keeps its
  own heading key. In French the old label *was* "Trésorerie", which would have
  collided with the new group name, so the guard below checks every locale.

**What this does not change.** §174's row budget is untouched: no row was
added, Treasury drops from nine tabs to eight, Settings grows from eight to
nine, both under `nav.test.ts`'s ten-tab ceiling. The role matrix in
`nav.test.ts` (`NAV_GATES`) moves `/billing` to the Settings block with the
same roles.

**Guard.** `nav.test.ts` pins the new placement, the CFO landing, and a
locale-wide rule: no two sidebar rows share a name, and within a group no two
tabs (nor a tab and its group) read the same, in any of the six catalogues.
`tests-e2e/smoke/section-nav.spec.ts` checks the rendered sidebar.

**The page and every pointer to it say "Subscription".** The rename left the
`/billing` heading (`billing.title`) on "Billing" while the nav said
"Subscription", and the messages that send a user there — the public pricing
page's "starts on Free" line, the plan-feature refusal
(`refusal.planFeatureRequired`), both AI-reading-paused invoice warnings and
the three AI-usage notices (80%, 100%, spending cap) — named a "Billing" page no row is
called. The name a user hunts for is the one in the sidebar, so the heading
now matches the nav label in every locale, and each pointer names the path,
**Settings → Subscription** (translated with each locale's own nav labels), since
the row sits inside the Settings group. The backend's English for the same
warnings (`invoice_warning_catalog.py`, regenerated into the web and mobile
code maps) and the notices (`notification_templates.py`) changed with them, as
did the mobile ARB copies of the two warnings. The route stays `/billing`;
only words changed.

**Partner Admin is NOT hidden yet — it needs a product call.** The finding
asked to hide it unless the workspace is a partner. There is no partner flag:
`GET /api/partner`'s `is_partner` is *derived* — true once the org parents at
least one child — and the page that row opens is also where a standalone org
(a) provisions or attaches its **first** child, which is how it becomes a
partner at all, and (b) mints the link code that consents to being adopted
*by* a partner. Gating the row on `is_partner` would make both unreachable
from the UI for exactly the orgs that need them, and the row's gate would no
longer agree with the page's (admin-only, the backend's
`require_roles(ROLE_ADMIN)` on every `/api/partner` route) — the mismatch
`frontend/CLAUDE.md` calls a defect. Doing it properly means either an
operator-set partner capability (a plan entitlement or an
`organizations.is_partner` column, exposed on `/auth/me`) with the link-code
minting moved to Organization settings, or accepting the row as is. Tracked in
`docs/followups.md`.

## 315. An admin's first run is a setup checklist ticked from facts the app already holds; emptiness is the workspace's (issue #635, 2026-10-21)

Decided by the operator's delegate on #635 findings 9 and 38.

**What was wrong.** A new workspace's dashboard offered one button, "Go to
invoices". The `start-admin` guide says to configure first — company, users,
sign-in security, ERP, vendors, the approval workflow — so a new admin's first
invoice landed in an untuned default workflow with no vendors to match and no
one else to approve it. The `/organization` "Getting started" panel had six
static links with no progress and left out the ERP, vendors, security and email
intake. Separately, the dashboard decided "brand-new workspace" from
`total_invoices`, which is scoped to the selected entity, so an established
customer that added a subsidiary was welcomed with "upload your first invoice"
the moment they switched to it.

**The rule.**

- **An admin on a workspace with no invoices gets a setup checklist** in place
  of the empty state: company details, invite the team, require two-factor
  sign-in, connect the accounting system, add vendors, set up the approval
  workflow, and uploading the first invoice last. Each step links to the panel
  that owns it, and the list links the `start-admin` guide.
- **A step is ticked only from a fact some other feature already owns,
  derived on read** (`backend/app/services/onboarding.py`). There is no
  tracking table and no "mark as done" write, so a step cannot be ticked by
  visiting a page and cannot drift from the setting it names. Each fact is
  narrow and named for what it reads: a Company Profile field beyond the name
  signup sets; more than one active user; `mfa.required` or SSO switched on;
  a non-`mock` ERP selected (the plan gate's own predicate); a vendor row; the
  workflow saved since it was provisioned (a version, an edit, or a second
  definition, because every tenant is provisioned with one); a live intake
  address. "Reviewed the security panel" and "looked at the workflow" are not
  knowable, so they are not claimed — the steps are worded as the change whose
  saved result is the fact.
- **A step with no fact behind it carries no done state at all.** The settings
  panel's Invoice defaults, payment-run sign-off and Branding links are drawn
  with their number only, never as unfinished: a blank defaults form is a valid
  choice. A non-admin, or a progress read that fails, gets the same links with
  no states rather than a row of empty boxes.
- **One server field, one derivation, two readers.** `GET /api/dashboard`
  carries `onboarding` (computed in the same request, only for an admin on an
  empty workspace — everyone else gets `null` and pays for none of its
  queries), and `GET /api/dashboard/onboarding` (admin-only) serves the same
  `compute_onboarding` to `/organization` without computing a dashboard.
  `frontend/src/lib/utils/onboarding.ts` is the one map from step to panel, so
  the two surfaces cannot send a step to two places.
- **The checklist retires itself** when the workspace has its first invoice:
  the server stops sending `onboarding`. No dismiss control — it would need a
  stored preference, and the condition that ends it is one every workspace
  reaches on its first real day.
- **Emptiness is decided org-wide** (`organization_has_invoices`, unscoped by
  `X-Entity-ID`). An entity with nothing in it yet keeps its true zero KPI row
  and says "No invoices for <entity> yet" in place of the charts; the
  new-workspace welcome and the checklist never appear for it.
- **Every other role on an empty workspace** keeps the empty state, fitted to
  what it can do: a role that may add invoices is sent to `/invoices`; one that
  may not (no system role today — a custom role can) is told what will appear,
  with no button into a page whose upload would refuse it.

**Not regressed.** The checklist, like the empty state before it, replaces the
KPI row only once the response has landed (`isEmptyTenant` keys on `data`), so
the loading → empty hand-off of §125 / §154 is unchanged.

**Considered and rejected.** A per-step "mark done" store: it reintroduces the
drift this avoids and makes the tick a claim about intent. Ticking security on
the admin's *own* MFA enrolment: it says nothing about everyone else signing
in. Counting "a workflow definition exists": always true since provisioning
seeds one.

Tests: `backend/tests/test_dashboard_onboarding.py` (each flag follows its
owning data; admin-only; retires with the first invoice; an empty entity on an
established workspace); `frontend/src/lib/utils/onboarding.test.ts`;
`frontend/tests-e2e/dashboard/first-run.spec.ts` (checklist, done states,
non-admin views, retirement and the entity case against the real backend, axe
in both themes); `frontend/tests-e2e/organization/getting-started.spec.ts`.

## 316. A QuickBooks environment change is not a new destination; destination-ness is per provider (2026-10-22)

**What broke.** In v0.1.6 a tenant connected by OAuth to a QuickBooks Online
*sandbox* company, using its own Intuit app (per-tenant `client_id` + sealed
`client_secret`, no platform app), could not switch Environment from
Production to Sandbox. §271 made `environment` a destination key for every
ERP by name, so the form stopped counting the stored `client_secret` as saved,
`missingRequired` blocked Save (the own-app pair is required when no platform
app exists), nothing persisted, and Test Connection went on calling the
production host with the sandbox company's tokens.

**Why `environment` is not a destination for QuickBooks.** The rule §271
protects is that an admin (or a stolen admin session) must not be able to send
a stored secret to a host of their choosing. The QuickBooks adapter maps
`environment == "sandbox"` to `sandbox-quickbooks.api.intuit.com` and every
other value to `quickbooks.api.intuit.com`; the authorize, token and revoke
endpoints (`QBO_OAUTH`) are Intuit constants that no setting reaches. No value
of the field can send the client secret, or the OAuth tokens, anywhere but
Intuit. Dropping the secret on a switch protected nothing and broke the only
way to test against a sandbox company.

**Business Central's `environment` stays a destination.** It is a URL path
segment (`{base}/{environment}/api/v2.0/companies(...)`) — a different value is
different books in the customer's tenant, which is exactly what §271 means by a
new destination. No other ERP catalogue entry declares `environment`.

**The shape.** Destination-ness is now decided per provider from one flag. The
catalogue's `_field(..., destination=...)` defaults to membership of the
by-name `DESTINATION_KEYS`; QuickBooks declares `environment` with
`destination=False`. `catalog.destination_keys(block)` is `DESTINATION_KEYS`
plus the provider's fields marked destination, minus those it marks not, and
`same_connection` / `secrets_to_drop` (so the save, and the test-erp secret
fill) use it. The settings form already read the same per-field flag from
`GET /organization/erp/providers`, so the frontend needed no logic change.

- **Narrowing is opt-in per declared field, never inferred from what a form
  renders.** A key the provider does not declare keeps the by-name default:
  a `base_url` added to a QuickBooks block through the API is still a new
  destination. A block routed through Merge.dev gets no opt-out.
- **A host-naming field can never opt out** (`*_url` / `*host*`), and
  `tests/test_erp_catalog.py` pins the list of opt-outs, so adding one is a
  reviewed change with its own evidence.

**Considered and rejected.**
- *Removing `environment` from `DESTINATION_KEYS` globally.* It would open
  Business Central, and any future provider that builds a URL from the value.
- *Deriving the keys only from each provider's rendered fields.* Fails open
  for a key an adapter reads that the form does not show.
- *Carrying the same change to commerce.* Square's `environment` has the same
  host-pair shape, but a Square access token is issued per environment, so
  asking for it again on a switch is the right prompt there, not a bug. Left as
  is.

Intuit issues separate development and production keys for one app, so a
switch may still need a new secret; the admin can type one. That is a
credential choice, not a destination, and the form no longer forces it.

Tests: `backend/tests/test_erp_catalog.py` (QuickBooks environment keeps the
secret and is the same connection; an undeclared host key on a QuickBooks block
still drops; Business Central environment and `base_url` still drop; the
opt-out list is pinned; the PATCH and test-erp round trips against the real
backend); `frontend/src/lib/types/erpConnection.test.ts` (QuickBooks keeps the
secret counted as saved and Save unblocked; Business Central asks again).

## 317. A bill's payments are read by the LinkedTxn types QuickBooks really uses (2026-10-22)

**What broke.** In production a person paid a QuickBooks Online bill with Pay
Bills, $150 from a bank account: an ordinary cash payment, no vendor credit.
The status sync opened a blocking `payment_reconciliation` saying the bill was
"settled, at least in part by a vendor credit", instead of recording the
invoice paid outside FeohLedger.

**Root cause.** The adapter had two definitions of "a bill-payment link".
`map_bill_status` (§303) matched `LinkedTxn.TxnType` against `BillPaymentCheck`
/ `BillPaymentCreditCard`. `_bill_payments`, which every other reader goes
through, matched the entity name `BillPayment`. A Bill types the link by how it
was paid (the payment's `PayType`), so against a real company `_bill_payments`
found nothing. In `bill_report` the cash list was then empty, so
`settled_by_credit = reduced_outside and (... or not cash)` came out True and
`apply_bill_report` took the vendor-credit branch. The same empty read reached
further than the reported symptom:

- our own write-back was invisible, so a bill FeohLedger paid read as paid
  outside us (`paid_only_by_us` False). That flags `payment_scheduled` as a
  double payment and a FeohLedger-paid invoice as "paid twice";
- `post_payment`'s first idempotency layer (find ours by marker) never matched.
  Intuit's `requestid` replay was the only thing preventing a duplicate;
- `void_payment` answered "no bill payment of ours to void" and reported
  success, while our BillPayment stayed live in QuickBooks.

`tools/fake-erp` and the adapter test doubles both emitted `"TxnType":
"BillPayment"`, so they agreed with the wrong reader and CI's erp-e2e stayed
green.

**Intuit's values.** Intuit's linked-transactions guide says a Bill's link to
its payment is set by QuickBooks through the BillPayment and appears in the
Bill's `LinkedTxn`. The documented `LinkedTxn` type enumeration lists
`BillPaymentCheck` and `BillPaymentCreditCard` and no `BillPayment`. Client
libraries built against the API (quickbooks-ruby's `LinkedTransaction`
defaults to `BillPaymentCheck`) agree, and so does the production bill: its
payment was not found under `BillPayment`.

**The rule.** `_BILL_PAYMENT_TXN_TYPES` is the one definition, and
`_bill_payment_ids(bill)` the one reader. `_has_bill_payment` (status mapping)
and `_bill_payments` (bill report, write-back idempotency, void) both use it.
The other `_linked_ids` calls are unaffected: a BillPayment's own lines link to
a `Bill` or a `VendorCredit` by those names, and the webhook's `billpayment` is
the CloudEvents entity name, not a link type. The fake ERP links a `Check`
payment as `BillPaymentCheck` and a `CreditCard` one as
`BillPaymentCreditCard`, so the erp-e2e exercises the real shape.

**A bare `BillPayment` is deliberately not accepted.** No Intuit source shows
it on a Bill. Accepting it "defensively" is how this hid: the doubles kept a
reader passing that real QuickBooks never matched. It would also either split
the definition again or widen what §303 counts as proof that money left. The
cost of an unrecognised link type is bounded on the inbound side: no payment is
read, so a bill taken down outside FeohLedger still blocks for a person and is
never booked as cash. The void side is not bounded the same way. That is why
`test_qbo_status_sync_adapter.py` now pins the real types in the double, and
why the double and fake-erp derive the link type from the pay type, not a
literal.

**Cash is defined positively.** Fixing the reader made a second gap reachable,
which the money-path review caught before merge. `bill_report` called a
person's payment cash when it had no `VendorCredit` line. That was harmless
while no payment was ever read; once payments are read, a journal-entry credit
cleared through Pay Bills (a $0 BillPayment whose lines link the `Bill` and a
`JournalEntry`) would have been booked as a $150 cheque. Cash is now
`_is_cash_payment`: a positive `TotalAmt` and every line linked to a `Bill` and
nothing else. `settled_by_credit` is True whenever any payment made outside us
is not cash, or the cash read does not cover what left the bill outside us.
Both cases block for a person and book nothing.

**Rows already wrong.** Before this fix every QuickBooks bill read as having no
payments, so production may hold any of these:

- *A bill paid in cash, flagged as a vendor credit* (the reported case). The
  open `payment_reconciliation` stays, and while it is open it also refuses
  the ERP-paid record (`record_invoice_paid_outside` refuses on an open
  payment-blocking exception). A person dismisses it (§169), then records the
  invoice paid outside FeohLedger with QuickBooks' date and rail. Alternatively
  they make any edit to the bill in QuickBooks, so the webhook or CDC re-reads
  it. An unchanged bill is not re-read on its own.
- *A FeohLedger write-back read as a payment made outside us.* This shows up as
  a non-blocking "paid twice" `erp_reconciliation` on a FeohLedger-paid
  invoice, or a blocking "FeohLedger is paying it too" on a `payment_scheduled`
  one. Both were false and are dismissed.
- *A void that left our BillPayment standing.* `void_payment` answered "nothing
  of ours to void", so `payment.erp_void_not_needed` was audited and no pending
  void was kept, and the payment can no longer be voided again here. Each one
  is voided by hand in QuickBooks. They are tracked in `docs/followups.md` (b),
  with the query that finds them.

Tests: `backend/tests/test_qbo_status_sync_adapter.py` (a `BillPaymentCheck`
and a `BillPaymentCreditCard` cash payment read as cash, not credit, with date
and rail; a genuine vendor credit, a journal-entry credit, a part-credit
payment, a $0 payment without a void note and cash short of the reduction all
read as not cash; a bare `BillPayment` link is not read; the write-back, cheque
or card, is found, read as ours and voided through the real link),
`backend/tests/test_erp_status_sync.py` (the adapter's report from a real-shape
bill, carried through `apply_bill_report`, records `paid_recorded` with no
exception).

## 318. QuickBooks payment shapes come from a recording of a real company, not from the author of the reader (2026-10-22)

**What broke.** §317: the status sync read a bill's payments under
`LinkedTxn.TxnType: "BillPayment"`, real QuickBooks says `BillPaymentCheck` /
`BillPaymentCreditCard`, and CI was green because `tools/fake-erp` and the
adapter tests emitted `BillPayment` too. Every double was written by the same
hand, from the same belief, as the code it checked. Fixing the reader fixed
that belief; this entry is about the next one we do not know is wrong.

**The shape.**

- **Recorded fixtures.** `backend/scripts/record_qbo_contract_fixtures.py`
  (`pnpm gen:qbo-recordings`) uses a local tenant's QuickBooks connection to
  build each status-sync scenario in a SANDBOX company — a bill paid by cheque,
  by card, in part, by a vendor credit, by our own write-back marker; a voided
  payment; a deleted bill; change-data-capture over the run; a webhook body —
  and writes Intuit's answers to `tools/fake-erp/fixtures/qbo_recorded/`.
  `backend/tests/test_qbo_recorded_replay.py` replays them through the real
  adapter over a `MockTransport` and asserts what each scenario MEANS (a bill
  paid by cheque is cash, paid outside FeohLedger, by cheque, on the payment's
  date), never what the adapter happened to answer. A bill can also be recorded
  as a person made it (`--bill SCENARIO=ID`), since the production defect came
  from QuickBooks' own Pay Bills screen.
- **Scrubbed by allowlist, because the repository is public.** Strings survive
  only under the keys the sync reads (ids, `SyncToken`, `TxnType`, `PayType`,
  dates, reference values, `status`, Fault `code`); a memo keeps only a leading
  "Voided" and our markers; names and addresses become fake ones; the realm id
  becomes a placeholder; numbers stay. No token passes through the script: the
  access token stays inside `erp_oauth` and the adapter's `_request`. The
  scrubber is pinned by tests, and every committed recording must be a fixed
  point of it.
- **A scenario not recorded yet is a named skip**, with the command that
  records it. That is a visible state in every run, not a masked failure; the
  recording itself is the operator's step (`docs/followups.md`).
- **The fake is compared to the recording, not fed from it.** The fake mints
  bills per test with fresh ids, so it cannot serve recorded records verbatim.
  Instead its Bill / BillPayment / void / CDC builders moved to
  `tools/fake-erp/qbo_records.py` (stdlib only, so the backend suite can import
  it; `app.py` needs FastAPI and defusedxml), and the replay suite fails when a
  field the sync reads is missing from the fake's record, has another JSON
  type, or carries another link type than QuickBooks used.
- **A live check as well** (`pnpm test:erp:qbo-live`,
  `backend/tests/test_qbo_live_contract.py`), mirroring Xero's (§ live Xero
  contract check): opt-in, local, read-only, never in CI. A recording pins one
  run; the live check re-asks the company, so a shape the recording never
  covered still surfaces as a named failure.

**Considered and rejected.**
- *Committing raw responses and trusting review to catch PII.* The repository
  is public and a review of a 500-line JSON blob is not a control.
- *Calling Intuit from CI.* Needs an interactive OAuth consent and a rotating
  refresh token held by a public repository's runners.
- *Generating the fake's records from the recording at runtime.* It would make
  the fake agree with the recording by construction while hiding what the fake
  does with a shape the recording lacks; the comparison keeps the fake's
  behaviour explicit and its drift loud.

Tests: `backend/tests/test_qbo_recorded_replay.py` (scrubber, replay per
scenario, fake against recording); `backend/tests/test_qbo_live_contract.py`
(opt-in).

## 319. The ERP's reported payment is kept as data, and recorded once what blocked it clears (2026-10-22)

**What happened.** §317's production case, after its fix deployed: the sweep
read the QuickBooks bill as paid and called `record_invoice_paid_outside`,
which refused because the stale `payment_reconciliation` from the old bug was
still open. That refusal opened a second exception, `erp_reconciliation`, whose
description told an AP user to "record it via POST /api/payments/record-outside
(erp_document_id=…, event=…)". To clean up, the operator had to work out that
the two rows were linked and in which order to clear them; find that the
payment queue's banner said "clear the exception first" without naming it,
linking to it or saying the uploader may not clear it; find that "Record as
paid" was hidden (not disabled) on the row; and re-key the rail, reference and
date the ERP had already reported, in a form that defaulted to ACH for a
cheque. Dismissing the blocker recorded nothing: the sweep reads only bills
that change, so nothing would ever try again.

**The rule.** A reconciliation exception the ERP paths open keeps what the ERP
reported as **data**, in `exceptions.erp_report` (JSONB, migration 0121), and
its `description` is a sentence for an AP user: what happened, what to do, no
API path, no ids, no refusal code. For `kind: "paid_not_recorded"` the data is
the payment itself (rail, reference, paid-on date and whether the ERP stated
it, the ERP document and event ids) plus the refusal code that stopped it.
The two other ERP-webhook rows carry their ids too (`settled_short`,
`voided_after_post`).

**One live row per invoice, refreshed, never stale.**

- A later report while a `paid_not_recorded` row is open refreshes that row
  instead of being dropped by the per-type dedupe. It refreshes the payment
  conservatively: an undated report never replaces a stated date, an id-less
  report never replaces the reference (an event-derived reference changes per
  delivery, and the reference is the replay key), and `first_reported_at` keeps
  the claim's age.
- There is at most one live `paid_not_recorded` row per invoice, whatever its
  type. A new double-pay refusal over a non-blocking row opens the blocking row
  and closes the other as superseded (audited). A live blocking row stays the
  row, keeping its own reason, because a later report is refused by that very
  row and so says nothing new. Two live rows would each offer to record the
  same payment, and a stale one would be re-attempted against a decision a
  person made on the other.
- **A payment the ERP withdraws is retired, not kept.** When the ERP later
  reports the bill open, part-paid, deleted or voided (`apply_bill_report`;
  the generic webhook on ANY status but `Paid` for an invoice still payable
  here, checked before its transition screen because
  `posted_in_erp → posted_in_erp` is a silent no-op), every live
  `paid_not_recorded` row on the invoice becomes `paid_withdrawn`: it stays
  open for a person, with a sentence saying so, but nothing re-attempts it and
  no one-click offers it. Without this, a cheque voided in QuickBooks after the
  first report would be booked as paid the moment someone cleared an unrelated
  blocker, or cleared the deletion row to say "still owed". The sweep reads
  only bills that change, so nothing would ever have flagged it. A blocking row
  is never closed by a machine here.

**Recording it later goes through the same gates, never around them.**

- *Automatically.* When `exception_lifecycle.record_decision` resolves or
  dismisses a payment-blocking exception, and the invoice holds a live,
  non-blocking `paid_not_recorded` row, the stored facts go back through
  `record_erp_reported_payment` in the same transaction
  (`erp_status_sync.retry_erp_reported_payments`). Every refusal is re-run:
  another open blocker, a live FeohLedger payment, run or card (which now opens
  the blocking `payment_reconciliation`, exactly as a first report would), a
  credit that cannot be netted. A refusal refreshes the row's code and
  sentence. The payment is dated from the first report, never the day the
  blocker cleared. `record_decision` is the hook because it is the one
  chokepoint every clearing door shares (§169); escalating is not a clear and
  records nothing. The record is the ERP's, not the clearer's (`actor_id`
  NULL, `source="erp"`), exactly as the first attempt was, so the clearer needs
  no `payment.record_external` and the recorder's segregation rule does not
  apply to them. Their own act — clearing the blocker — has already passed the
  queue's segregation rule. The payment to record is chosen only under the
  invoice lock every ERP writer takes first, and re-read rather than served
  from the identity map, so a withdrawal the sweep committed while the
  decision waited is seen. The attempt runs in a SAVEPOINT: an unexpected
  failure (a lock timeout, a bug) rolls back only the attempt, is logged with
  its traceback, and stamps the row (`retry_failed_at`) with a sentence
  pointing at the one-click record instead of promising an automatic one. It
  must not undo a person's clearing of a duplicate or fraud flag.
- *In one click.* `POST /api/exceptions/{id}/record-erp-payment`, from the
  exception dialog, pre-filled from `erp_report`. It records the payment as a
  user record (`source="user"`) and resolves the row as the caller, in one
  transaction. **Every refusal comes before anything is written or audited.**
  In `lambda` audit mode an audit row leaves for SQS when it is dispatched, so
  resolving first and rolling back on a refused record would ship an
  `exception.resolved` for a row that stays open. So the record runs first,
  with this one row not counted as a blocker (`answering_exception_ids`, the
  only door that passes one), and `_complete` resolves the row as the caller
  once the payment is booked. The invoice and then the exception row are
  locked (the order every ERP writer takes them in, so a click racing a sweep
  tick waits instead of deadlocking) and re-read before anything is judged, so
  a second click finds the row resolved and 409s. Not `record-outside`: a
  `paid_not_recorded` row opened for a double-pay refusal is itself
  payment-blocking, so `record-outside` refuses on it. Clearing it first in a
  separate request would leave a window with the invoice payable and nothing
  recorded. The caller needs `payment.record_external`; for a blocking row also
  the queue's role and its segregation rule (`segregation_refusal`), and
  always the recorder's (`external_payment.segregation_refusal`). Both are
  reported per row as `record_erp_payment_refusal`, so the button is offered
  exactly when the server would take it (issue #575's rule).
- *Whichever door records the invoice as paid outside FeohLedger* (the
  re-attempt, the one-click, a user's own "Record as paid", a run recorded
  outside), `external_payment._complete` resolves every live
  `paid_not_recorded` row on it through `record_decision`
  (`via: payment_recorded`), because the question is answered. A FeohLedger
  rail cannot pay such an invoice in the meantime: every refusal that leaves a
  non-blocking row open (another blocker, an applied-credit conflict, nothing
  left to pay) also refuses the payment run, and a double-pay refusal opens the
  blocking row.

**Why a blocking `paid_not_recorded` row is not re-attempted automatically.**
It exists because FeohLedger held its own claim on paying the invoice when the
ERP paid it. Which of the two payments stands is a person's call, and the row
refuses the record on itself by design until someone who may clear it makes
that call. The one-click path is that call.

**Why not parse the old rows.** Rows raised before 0121 hold their facts only in
prose. Parsing them back into a payment record would invent precision the row
never had, so they are recorded by hand as before. The production row that
prompted this is one of them.

**The payment queue says what blocks a row.** Each row now carries
`blocking_exception` (the exception's id and type, and this caller's
segregation code for clearing it, or null) and `erp_reported_payment`. The
blocked chip links to `/exceptions?id=…`, which opens that exception's dialog,
and says when someone else must clear it. "Record as paid" is shown disabled
with its reason as an accessible description instead of hidden. It opens
pre-filled from the ERP's report when there is one, with no method chosen when
the ERP stated none, rather than ACH.

**Not done: localized ERP descriptions.** ERP reconciliation sentences are
English, like every non-detector exception description, so the queue shows them
in English in every locale. The dialog's own chrome and the ERP-payment panel
are catalogued in all six locales. Moving these sentences onto
`description_code` is tracked in `docs/followups.md`.

Tests: `backend/tests/test_erp_reported_payment_recovery.py` (facts stored and
refreshed, an undated report keeping the stated date; dismissing the blocker
records with the ERP's rail, reference and date and closes the row with an
audit row; a second blocker and a FeohLedger run still refuse, leaving one live
row; idempotent; a failed re-attempt never undoes the clear; escalate records
nothing; a user's own record closes the row; the one-click records and resolves
a blocking row as the caller, is refused to the uploader on both SoD rules, a
second click 409s, and a money-gate refusal books nothing and ships no audit
row in `lambda` mode; the queue names the blocker and carries the ERP's
payment), `test_erp_status_sync.py` (a payment the ERP withdrew — bill open,
deleted or part-paid — is not recorded when every blocker is cleared),
`test_erp_paid_recorded_outside.py`, `test_erp_webhook_transitions.py`,
`frontend/src/lib/types/erpReport.test.ts`,
`frontend/tests-e2e/payments/erp-reported-payment.spec.ts`.

## 320. NetSuite signs in with OAuth 2.0 client credentials, and a saved block without a method stays TBA (2026-10-26)

**Context.** Oracle closes Token-Based Authentication to new REST integrations
in NetSuite 2027.1 ("Use OAuth 2.0"). The adapter only spoke TBA, so a customer
onboarding on 2027.1 could not create the integration record our setup asked
for (issue #653).

**Decision.** `settings.erp.auth_method` picks `oauth2_m2m` (the client-
credentials grant: a JWT signed with the private key of a certificate mapped to
the integration, exchanged for a 60-minute bearer) or `tba`. The private key is
an `erp` secret sealed in `provider_credentials`, write-only like the others;
the client id and certificate id are configuration.

- **Absent means `tba`, in the adapter and in the form.** Every NetSuite block
  saved before the choice existed is TBA. Making the key mandatory would have
  needed a control-plane data migration for a value we already know; reading
  absent as OAuth 2.0 would break every existing tenant. The form starts a NEW
  connection on OAuth 2.0 (first option) but shows a saved block without the
  key as TBA (`stored_default`), because showing the first option there would
  switch a working tenant on its next save. Any other value is refused, never
  guessed.
- **Each method's fields apply only under it** (`show_when`), so the form never
  requires a TBA token secret of an OAuth 2.0 connection or the reverse. That
  is a generic catalogue attribute rather than a NetSuite branch in the panel.
- **The bearer is cached in process, keyed by a hash of the credentials**, and
  a 401 re-mints once. The grant has no refresh token, so the only alternative
  is a token request per push (three calls each) against NetSuite's
  concurrency limits; persisting the token would add a sealed secret with no
  owner. Hashing the private key into the key means a rotated certificate can
  never reuse the old one's token.
- **The algorithm follows the key**: PS256 for RSA 3072/4096, ES256/384/512 by
  curve — Oracle's certificate conditions, refused up front otherwise rather
  than after a round trip NetSuite would refuse anyway. NetSuite does not take
  RS256.
- **A refused token request is returned as the failed call.** Every caller
  already treats a non-200 as failure, so a push fails non-retryable on a 400
  (`invalid_client`: no certificate mapped) without reaching the record API, and
  the OAuth `error` code is logged — never the assertion or a token. A 200 with
  no token raises instead, since as a response it would read as success.
- **fake-erp verifies the assertion cryptographically**
  (`tools/fake-erp/netsuite_oauth2.py`), unlike its TBA shape check: a header
  shape check would have proved nothing about whether NetSuite accepts what we
  sign. Certificates are mapped through a test hook with the public key only,
  and survive `/__reset` so a parallel worker cannot unmap a running spec.

**Not done: live verification.** Built from Oracle's documentation and proven
against the fake's verifier; a real account is the remaining proof
(`docs/followups.md` (a)).

Tests: `backend/tests/test_erp_netsuite_auth.py`, `test_erp_catalog.py`,
`frontend/src/lib/types/erpConnection.test.ts`,
`frontend/tests-e2e/erp/netsuite.spec.ts`. Reference:
`backend/docs/erp-integration.md` § NetSuite sign-in.

## 321. A sub-processor change is one change-log entry; the notice is emailed from it (issue #427, 2026-10-26)

**What happened.** `/legal/dpa` § 8 and `/legal/sub-processors` § 9 promise
every customer at least 30 days' notice before a new sub-processor processes
their data. Nothing backed it: § 10 was a hand-typed `<tr>`, the DPA defined the
notice as "this page is updated — checking it is how you receive it", and the
30 days were prose in three places and an assertion in none. An Art. 28(2)
general authorisation resting on "check this page" is weak, and a register row
added without anyone told was one forgotten step away.

**The rule.** The change log is data — `app/services/subprocessor_changelog.py`
— and both halves derive from one entry, so adding the row and giving the notice
are the same action:

- `scripts/gen_subprocessor_changelog.py` (`pnpm gen:subprocessor-changelog`,
  `check:` in CI's Backend lint job) writes the module § 10 renders.
- `services/subprocessor_notices.py` (sweep `subprocessor-notices`) emails each
  `notice=True` entry to every active system-`admin` of every organisation that
  existed on its publication date.

The 30 days are enforced, not described: a `notice=True` entry must state
`effective_on` at least `NOTICE_PERIOD_DAYS` after `published_on`, or carry an
`urgent_reason` (the DPA's security/availability carve-out). `validate` refuses
anything else and generation refuses to write past it, so the page cannot
publish a short notice that does not say why. The sent notice is measured too:
each ledger row stores the entry's effective date and `late` — sent with under
30 days to run, which an urgent entry or a sweep left off would cause — and the
tick reports `late_notices` on `/api/health/sweeps`.

**Idempotent and on the record, with no lock held across I/O.** A
control-plane `subprocessor_notices` row, UNIQUE (entry, organisation), moves
through three short transactions: the **claim** is inserted `ON CONFLICT DO
NOTHING` and committed before any email goes out, so a second replica or a
re-run sends nothing; **`sent_at`**, the counts, the recipient user ids and
`late` are committed once at least one admin's email reached the provider (if
none did, the claim is deleted and the next tick retries); then the tenant's
`privacy.subprocessor_notice_sent` audit row is written and **`audited_at`**
stamped. A failed audit write is a tick failure and is retried alone every tick
— it never re-sends the notice. (The first cut held the claim open across the
sends and the tenant write, so a tenant that refused the audit row rolled the
claim back and re-mailed every admin hourly; review caught it before merge.) A
claim with no `sent_at` older than `CLAIM_TIMEOUT` (an hour) is a run that died
mid-send and is re-taken: that is the one at-least-once path, and a duplicate
notice is recoverable where a silent miss is not. The ledger has no foreign key
and is exempt from tenant deletion: it is evidence a contractual notice was
given, and should outlive the customer it was given to.

**No retroactive notices.** `NOTICES_SINCE` (2026-10-26) is the day this
shipped; `validate` refuses a `notice=True` entry published before it, which
would otherwise be mailed to every organisation on the first tick for a change
that already happened without notice. A late notice (an urgent entry, or a
sweep that was off) says "Took effect" rather than claim a future date.

**Why each choice.**

- **Administrators, not a DPO field.** The platform holds no privacy contact
  for an organisation; inventing one meant an endpoint, a settings field and a
  contractual decision (instead of the admins, or as well?) that is counsel's,
  not code's. The active admins are the contact the platform does hold, the
  DPA now says exactly that, and it keeps — rather than drops — the existing
  offer to copy the notice by hand to another address requested at `privacy@`.
  The field is the (c) follow-up.
- **Not a notification event.** `notify_event` is preference-gated; a user who
  muted email would have opted their organisation out of a contractual notice.
  The sweep sends through the email adapter directly, with no in-app row.
- **English, unbranded.** The DPA is English-only by design (§174) and the
  notice is given under it; a white-label child tenant is still notified by the
  processor, which is the platform.
- **Organisations created after publication are not told.** They signed up
  against a register that already listed the change.
- **Append-only, stable ids.** The ledger is keyed by entry id: renaming one
  re-notifies everyone, deleting one leaves a sent notice the page no longer
  shows. The id must start with its publication date; `validate` checks both
  that and order.
- **The sweep is off by default and required in production.** Every sweep that
  emails is off locally (guard rail 7). Because the DPA now says the notice *is*
  emailed, `deploy/prod.sops.yaml.example` sets it and `decrypt-env.sh` warns on
  every deploy while it is off. Warn, not refuse: a deploy should not be blocked
  on it, and the warning plus the (b) follow-up make it impossible to miss.

**Historical rows.** All six existing rows are `notice=False`: the register's
first publication; the move of the databases to RDS (same provider, same
region, same data — no new party); and four systems the customer connects
itself (§ 4), which are not sub-processors. None is ever emailed.

**Rejected.** A subscribe endpoint / mailing list (a second recipient model to
keep consistent with the admin set, and a list a customer has to know to join —
the same weakness as "check the page"); notifying from the git diff of the page
(no stable id to dedupe on, and nothing measures the 30 days).

**Tests.** `backend/tests/test_subprocessor_changelog.py` (the rules, the
generator's drift guard and its refusal), `test_subprocessor_notices.py` (only
active admins, once per entry, roll back when nobody is reached, one failed
address does not stop the rest, `late`, organisations created after
publication, the CLI), `test_migration_0122_subprocessor_notices.py`,
`test_deploy_decrypt_env.py` (the warning),
`frontend/src/lib/legal/subprocessorChangelog.test.ts` (the email's objection
address is `CONTACT.privacy`).

**Amended in review (2026-10-26): refuse, not warn.** "Warn, not refuse" left
a deployment free to run with the contractual mechanism off, the one state the
DPA cannot describe. `config.py` now refuses to boot a deployed environment with
`FEOH_SUBPROCESSOR_NOTICES_ENABLED` off — the same shape as the `console` email
refusal — and `decrypt-env.sh` refuses the file instead of warning, so a deploy
stops before compose restarts the API into that refusal
(`test_config_boot_guards.py`, `test_deploy_decrypt_env.py`). Local dev, tests
and CI keep it off; the day-one rehearsal (production-shaped) turns it on.

**Also amended in review.** An organisation with no active administrator raised
the same error as one whose every email bounced, so it counted as a tick
failure on every tick and held `/api/health/sweeps` red indefinitely; it is now
`organizations_without_admin`, a counter that is not a failure, and the
organisation stays owed (the undelivered case is a (c) follow-up). The audit row
was appended once per attempt, so a control-plane stamp lost after the tenant
commit appended a second row to the append-only trail next tick; it is now
written only when the tenant holds no row with that action and `correlation_id`
= the notice id, under an advisory lock, and synchronously in every audit mode
— in `lambda` mode a row still in SQS is invisible to that check, the reason
`dispatch_durable_auth_audit` writes synchronously too. Per-notice
`failed_recipients` reach the operator as a tick counter and in
`scripts/subprocessor_notices.py list`.

## 322. Blackbaud: `OnHold` vendors are not payable, the shared SKY quota is budgeted per org, and the duplicate lookup searches every attempt's date (#651, 2026-10-26)

Three Blackbaud FE NXT calls from the #651 audit, recorded together because
all three are about what the adapter may assume about FE NXT's state.

**1. An `OnHold` vendor syncs as inactive and is not paid from FeohLedger.**
FE NXT's `VendorStatus` is `Active`, `Inactive` or `OnHold` (read from the
live SKY definitions). `list_vendors` emits both `Inactive` and `OnHold` with
`active=False` (`_UNPAYABLE_VENDOR_STATUSES`). The sync then creates no payee
for one and marks a linked vendor inactive, so no payment run picks it up.
`OnHold` still *takes an invoice* in FE NXT, but FE NXT holds its payments: a
hold is how a nonprofit's finance office says "do not pay this supplier yet"
(a dispute, missing paperwork, an audit). Paying the vendor from FeohLedger
would go around that hold, because our payment never passes through FE NXT's
payables. The vendor is still emitted, not dropped (§284: absence from a pull
unlinks, and the vendor still exists). Lifting the hold in FE NXT does **not**
reactivate the local vendor: re-enabling a payee is a person's call, and a sync
that silently made one payable again would undo whatever the local team
decided in the meantime. Rejected: treating `OnHold` as active because FE NXT
accepts invoices for it. That reads "may be billed" as "may be paid", which
are the two things the hold separates.

**2. The SKY call quota is shared, so it is budgeted per org.**

*What SKY does* (Blackbaud's "Rate limits and quotas" and "Throttling and
retry patterns" pages, checked 2026-10-26): a per-second rate limit answered
with **429**, and a daily call quota answered with **403**, both carrying
`Retry-After`. The quota resets at **00:00 UTC**, and Blackbaud says to wait
`Retry-After` rather than compute a reset. "Standard APIs" is 10 calls/s and
25,000 calls/day **per subscription**. One platform subscription key
(`FEOH_ERP_BLACKBAUD_SUBSCRIPTION_KEY`) serves every tenant without its own,
so one tenant's bulk send or vendor sync could spend the day for all of them.
A quota 403 used to reach the admin only as a failed push.

*What we do* (`services/erp_call_budget.py`):

- Every SKY call goes through an httpx request hook that counts it in Redis,
  per subscription key (a SHA-256 digest, never the key) and per org, per UTC
  day. A response hook records a 429's or quota 403's `Retry-After` as a
  cool-down for the **whole key**, so no tenant keeps calling into a refusal.
- An org on the platform key may spend
  `FEOH_ERP_BLACKBAUD_TENANT_DAILY_CALL_BUDGET` (default 2,500, a tenth of
  Standard) a day, and no org may take a key past
  `FEOH_ERP_BLACKBAUD_DAILY_CALL_QUOTA` (default 25,000; set it to the key's
  real tier). A tenant with its own `subscription_key` is held only to its
  key's quota, since that bucket is its own.
- **A push is never cut off half way.** It asks for its worst case before its
  first call (`push_call_reserve`: the lookup pages, the fiscal years, the
  create, the 400's vendor read and the job watch; more when it reads an
  earlier job or searches extra dates) and then runs unenforced. A call
  refused after the create would leave the job unconfirmed and the invoice
  in doubt, which is worse than overshooting the share by one push. The
  check and the spend are not atomic, so concurrent pushes can overshoot by up
  to their number times the reserve, and a 401 token replay is one call more
  than the reserve counts. SKY's own 403 and the shared cool-down are the
  backstop.
- The refusal is coded, `erp_call_budget_exhausted` with `reason`
  (`tenant_budget`, `subscription_quota`, `rate_limited`) and `retry_after`,
  and is made before any call (`not_posted`). An earlier attempt's job stays on
  the result, so it is still read first next time. Only `rate_limited` is
  retryable by `services/erp`'s seconds-long backoff; the other two fail the
  invoice with the reason, and the operator retries after the reset.
- A sync listing is refused call by call. The hook raises
  `ErpCallBudgetExhausted` (never an `httpx.HTTPError`, which the push reads as
  "outcome unknown"). The listing returns nothing (§284: never a partial pull),
  and `erp_pull_failure` answers a coded **429** with `Retry-After`. A PO
  sync too: `list_pos` otherwise reads any failure as "no POs", and a refusal
  must say why.
- A 403 *with* `Retry-After` is read as the quota, as SKY documents. A
  permission 403 that happened to carry the header would pause the whole key
  until it passes. That is accepted as the documented contract, not guessed
  around from the body (never parsed: it can echo the payload).
- The org's admins get an `erp_call_quota` notification (in-app + email,
  preference-gated), once per UTC day each, at 80% of the share, when the
  share is used, and when the subscription's quota is. It is claimed by a
  Redis SET NX and released if nobody could be told, delivered through
  `erp_connection_health.notify_org_admins`: the ERP connection's existing
  notice path, under its own event type so the "needs reconnecting" label is
  not stretched over a quota.

*Why Redis, when it held mostly auth state.* The counts last a day, must be
shared by every worker, the dispatch task and the Lambda path, and are
incremented on every HTTP call. That is the `services/rate_limit` shape, not a
JSONB write under the org row lock per request. Losing them (a Redis restart)
costs at most a day's count. **It fails open**: a Redis error lets the call go
ahead uncounted. This is a courtesy brake on a quota SKY enforces itself, and
its 403 stays the backstop, so failing closed would only turn a Redis blip into
"no Blackbaud posts". (Auth fails closed on Redis; that asymmetry is
deliberate.)

*Rejected.* Sleeping on `Retry-After` inside the push: a worker held for up to
a day, with the transaction discipline of `services/erp` built around seconds.
An automatic re-send at the reset: worth having, but it is a re-dispatch sweep
over `failed` invoices with its own idempotency story, so it is a follow-up.
A share computed from the number of connected tenants (quota ÷ n): it moves as
tenants connect, and one busy tenant could still never use what quiet ones
leave.

**3. The narrowed duplicate lookup searches every date an attempt was sent
with.** When the unnarrowed `search_text` page (500 rows) is full, the lookup
narrows with `from_date` / `to_date` to a day either side of the invoice date.
It narrowed on the **current** date. If the date was edited between an
attempt whose outcome is unknown and the retry, our own marked bill sat outside
the window and the retry posted a second bill. `services/erp._call_erp` now
records each attempt's invoice date on the workflow instance
(`erp_attempt_invoice_dates`, before the call, cleared by a success). It hands the earlier ones over as
`InvoicePayload.prior_invoice_dates`, and the adapter narrows on each.

Only an attempt that may have reached the ERP keeps its date. One that
states it left nothing (`not_posted`: a budget or pre-flight refusal) gives its
slot back, and the list is never trimmed. A first version kept the five newest,
so a run of date edits, each refused all day by the budget, could push out the
date of the one bill that may exist: the hole this closes, reopened.

*Trade-off kept.* A **hand-keyed** duplicate (no marker of ours) dated outside
every window we search, for a number busy enough to fill the first 500 hits,
is not seen. Nothing ties it to a date we know, and walking every page of a
busy number on every post is the quota problem above. A marked bill of ours is
always found, since every date it can carry is searched. The duplicate keyed
by hand is still caught when the number is not busy, which is the usual case,
and FeohLedger's own duplicate warnings run before approval.

**Tests.** `backend/tests/test_erp_blackbaud_adapter.py`: the cool-down shared
across tenants, a 429 retryable and a quota 403 not, the per-org share and the
key quota, the own-key exemption, the 80% warning once, a notice nobody got
retried, the pending job kept on a refusal, a sync refused page by page and
its coded 429, a status read under a cool-down, Redis down, `Retry-After`
parsing, and the date-edit retry finding the first bill.
`test_erp_push_flow.py` (dates recorded and handed back),
`test_notification_templates.py`, `frontend/src/lib/api/erpRefusals.test.ts`.
fake-erp's `POST /blackbaud/__throttle` reproduces SKY's 429 / 403 locally.

## 323. A PO the ERP adapter cannot price is reported, and an earlier total for it stops being matched against (PR #670 money review, migration 0123, 2026-10-26)

**The problem.** Adapters skip a PO whose total they cannot state exactly:
SYSPRO a PO with a line discount, a price unit of measure, a mixed-currency
order or a currency it cannot place (§303's issue #650 work); Blackbaud FE NXT
one with no `order_total`. Skipping is right for a PO the sync has never seen.
For one it synced earlier it was silent: the row kept the old total, the sync
said nothing, and the 3-way match kept passing invoices against a figure the
ERP no longer stated. Nothing in the sync could tell "the ERP skipped this PO"
from "this PO was never here".

**The rule.** An adapter that skips a PO says so, and the sync stops trusting
the row:

- **Contract.** `ErpAdapter.list_pos_report() -> PoListing(pos, skipped)`, each
  skip a `SkippedPo(po_number, PoSkipReason)` with a coded reason
  (`unpriceable`, `lines_unread`, `no_lines`, `local_currency_unknown`). The
  base default wraps `list_pos` and reports nothing skipped, so every adapter
  that does not override it — and every test double that patches `list_pos` —
  behaves exactly as before. It is additive on purpose: a second abstract method
  would have broken adapters other sessions were editing. SYSPRO overrides it;
  Blackbaud's wiring is a `docs/followups.md` entry.
- **The mark.** The sync stamps `purchase_orders.total_unverifiable_reason` on
  the existing row the upsert would refresh for a skipped number
  (`_sync_target`: this entity's row first, else an unstamped one). Marking
  every row in scope was rejected in review: the upsert clears only that one
  row, so a shared twin would stay marked, and its invoices held, for good. It
  does not change `total`: there is no honest
  figure to write, and erasing one would invent a zero. The next sync that
  prices the PO clears the mark. The response reports `not_priced` and
  `unverifiable`, and the purchase-orders page shows a warning toast.
- **The match.** While the mark is set, `po_matching` compares nothing —
  `mismatch`, no variance, issue `po_total_unverifiable` — and skips the
  receipt-value leg. `refresh_warnings` raises a payment-blocking `po_mismatch`
  at `warning`, the currency guard's treatment and for its reason: with no
  trustworthy figure there is no way to show the invoice is not over-billing.
  The missing-PO and multi-PO-split agents never take such a PO as a candidate.
- **When it lands.** The sync re-matches the invoices citing every PO whose mark
  or total it changed, in its own transaction and **not** best-effort, so the
  hold appears at sync time rather than at the invoice's next edit, and lifts
  the same way. The QMS sync's best-effort precedent does not carry over: a
  receipt changes evidence, while this mark exists to raise a hold, and the
  payment gate reads only open exceptions — a committed mark without its hold
  would leave an approved invoice payable against the stale total. A failed
  re-match fails the sync and rolls the mark back.
- **A failed currency read is not a fact.** SYSPRO skips a currency-labelled PO
  as `local_currency_unknown` only when `GetLogonProfile` answers with a profile
  stating no usable code. A failed read (HTTP error, `ERROR`, unreadable) fails
  the listing (`ErpListingIncomplete`): skipping would mark every labelled PO
  and hold every invoice citing one over a transient outage, and some of those
  holds (past approval, manual receipts) would outlive it.
- **Elsewhere.** The supplier portal refuses to flip a marked PO (409); the PO
  list and detail return the reason. Budget commitments and spend analytics
  still sum the stale total — reporting, not payment; a `docs/followups.md`
  entry.
- **A note for the CISO / Security Analyst.** Re-matching on every ERP total
  change means an ERP-side increase can now close an over-billing `po_mismatch`
  at sync time instead of at the invoice's next edit. The guards are the ones
  every re-match has (org settings, past-approval and segregation limits), but
  nobody with ERP access is treated as an implicated actor the way a manual
  receipt's recorder is (§267). Where one person can edit both the ERP PO and
  the AP invoice, that is a segregation question for the customer's controls.

**Considered and rejected.**

- *Setting the PO to `cancelled` or deleting it.* Both are claims the ERP did
  not make; an invoice against a deleted PO also reads `po_not_found`, which
  sends the missing-PO agent looking for another PO to link.
- *Zeroing `total`.* `NOT NULL` aside, a zero total is a figure, and the
  matcher would compare against it.
- *Holding only invoices still pre-approval.* The point of the mark is that
  nothing pays against an unverified total, which is the payment gate's job.

Guards: `tests/test_purchase_orders.py` (sync → mark → hold → re-price →
clear, the entity scope, mark and clear on the same row beside a shared twin,
a failed re-match rolling the mark back, the base default),
`test_po_matching_algorithm.py`, `test_po_matching_wiring.py`,
`test_exception_agents_missing_po.py`,
`test_portal_self_service.py::test_po_flip_refuses_a_po_whose_total_the_erp_no_longer_vouches_for`,
`test_erp_syspro_adapter.py` (`test_list_pos_report_names_every_po_it_skips_and_why`,
`test_a_failed_logon_profile_read_fails_the_listing_instead_of_skipping`),
`test_migration_0123_po_total_unverifiable.py`.

**Later in PR #670: the stale total leaves every other surface too.** The
"Elsewhere" bullet above deferred budgets and analytics; that deferral is
closed. Display paths send `null` for a marked PO's total and line money
(`models/procurement.po_vouched_money`) — the PO API with the reason, the
supplier portal with only a `total_unverifiable` flag, since the reason code is
the buyer's — and the web shows a named state, not a dash that would read as
"no total". Aggregates follow §35 (exclude and count, the rule `excluded_rebate_count`
(§62) and the budgets' `excluded_row_count` already apply): budget `committed`,
the `/cfo` open-PO and GR/IR accrual legs and by-entity open POs leave the PO
out and report `unverifiable_po_count` / `open_po_unverifiable_count`, rendered
beside the figure. A separate count, not folded into `excluded_row_count`,
because that one means "re-denominate the row" and this one means "the ERP has
to price the PO". *Rejected:* showing the last-synced figure labelled "stale" —
a labelled number still gets copied, summed and compared, which is what the mark
exists to stop. Guards: `po-matching.md` § A PO total the ERP no longer vouches for.
