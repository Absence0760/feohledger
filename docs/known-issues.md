# Known Issues

Tracked, non-trivial defects that are diagnosed but not yet fixed. Each entry
names the root cause, the evidence, blast radius, and a recommended fix
approach — this is a staging area for real problems, not a place to let them
go stale. See root `CLAUDE.md` guard rail 6 (no dangling deferred findings).

**Seven entries are open** — three privacy defects surfaced by publishing the
legal pages (the DSAR-export bank-detail exposure, the Positive Pay file's
unexpiring account numbers, and the erasure/export completeness gap), the
`/organization` 320px reflow defect, and the three local-e2e entries at the
bottom. The header previously said "one" while
those three e2e entries sat beneath it; a known-issues file that under-reports
itself is the failure this note already warned about once. The other
nine are `~~struck-through~~` resolved stubs, kept because the *diagnosis* is
the expensive part and is worth not re-deriving. Add a new entry at the top when
a defect is diagnosed but can't be fixed in the same session.

(This header said "no entries are currently open" while the file carried ten
`##` headings and only nine struck. Root `CLAUDE.md` repeated the claim. Both
are corrected as of 2026-09-06 — a known-issues file that under-reports itself
is worse than one with an entry in it.)

**Scope:** *diagnosed defects* only. A deferral that isn't a defect — blocked on
a credential, an operator step on merged code, or sized-but-unstarted work —
goes to [followups.md](followups.md). Reasoning behind a deliberate design call
goes to [decisions.md](decisions.md).

---

## The DSAR export is the one surface that returns unmasked bank details

**Issue:** [#423](https://github.com/Absence0760/feohledger/issues/423)

**Found:** 2026-09-15, in the pre-counsel legal review of the published pages
(`reviews/saas-legal-review-legal-pages.md`, H12).

`app/services/privacy_export.py:298-299` puts `vendor.bank_details` and
`vendor.beneficial_owner_data` into the export bundle verbatim:

```python
"bank_details": vendor.bank_details,
"beneficial_owner_data": vendor.beneficial_owner_data,
```

`Vendor.bank_details` is JSONB holding the real `account_number`,
`routing_number`, `wire_routing_number` and `iban` (`app/api/vendors.py:150`
names them as `_BANK_SECRET_KEYS`). **Every other surface in the product reduces
them to the last four digits** — the audit trail via
`_bank_details_audit_summary`, the change-request queue via
`maskedProposalSummary`, the UI, the logs, the error bodies. This one does not.

**Why it is not simply correct-by-Art-15.** A data subject is entitled to their
own personal data, so exporting a supplier's own bank details *to that supplier*
is defensible. The problem is who can pull it: `POST /api/privacy/dsar` is
`require_roles(ROLE_ADMIN)` and takes a subject identifier, so **any org admin
can generate, for any vendor contact in the tenant, a downloadable file
containing that supplier's full banking credentials** — with no dual control, no
step-up, and no `vendor.bank_change.approve` permission required. The dual-control
gate on *changing* bank details (`VendorChangeRequest`) exists precisely because
those values are the BEC-fraud target; reading them out in bulk is the same asset
with none of the ceremony.

**Blast radius.** Privilege-escalation-shaped rather than a GDPR failure: the
export is audited (`dispatch_audit` at `app/api/privacy.py:127-140`), so it is
traceable after the fact, but nothing prevents it. It is also the one path that
puts full account numbers into a file that then leaves the system by whatever
route the admin chooses.

**Recommended fix.** Not "mask it and move on" — that would break the Art 15
right the endpoint exists to serve. The shape that keeps both: mask by default in
the bundle, and gate the unmasked variant behind the same dual-control the change
path uses (or a step-up plus an explicit `include_banking` flag that is separately
audited), so producing one is a deliberate, attributable act rather than a side
effect of a routine DSAR. That is a design decision about the DSAR contract, which
is why this is an entry rather than a same-session patch.

---

## The Positive Pay file holds every vendor's full account number, with no expiry

**Issue:** [#425](https://github.com/Absence0760/feohledger/issues/425)

**Found:** 2026-09-15, same review (H10).

The generated ACH-authorization / check-issue file is the one artefact that
legitimately needs full account and routing numbers — `positive_pay_adapters`
assembles them at generation time, which is why `PositivePayItem` stores only
`account_last4` (`app/models/positive_pay.py:125`). The file itself lands in
object storage.

Two things follow that nothing currently handles: the object is **not covered by
the retention sweep** (which reaches invoices and the audit log only, and is off
by default anyway), and it is **not reachable by the erasure path** (which never
touches object storage at all — see the entry below). So a file containing every
vendor's full banking credentials for a given run persists indefinitely, and a
supplier's erasure request cannot reach it.

**Recommended fix.** A lifecycle rule on the Positive Pay prefix is the cheap
half and worth doing on its own — these files have a short operational life, a
bank consumes them within days. The durable half is the same object-storage leg
the erasure entry below needs; do them together, because a retention rule that
deletes on a timer and an erasure path that deletes on request are the same
traversal with different triggers.

## Erasure and the DSAR export never reach object storage, passkeys or live sessions

**Issue:** [#424](https://github.com/Absence0760/feohledger/issues/424)

**Found:** 2026-09-15, while drafting the published Privacy Policy and DPA
(`docs/decisions.md` §175) — the pages could not honestly describe the erasure
and access paths as complete, which is what exposed the gap.

`app/services/privacy_erasure.py` and `app/services/privacy_export.py` both
operate purely on database rows. Neither module references
`app/services/storage.py`, `WebAuthnCredential`, or the Redis session registry —
verified by grep: zero hits for `storage`, `_delete_object`, `webauthn`,
`session` or `revoke` in either file.

Three concrete consequences:

| Request | What the code does | What the subject is owed |
|---|---|---|
| Erasure of a vendor contact | Redacts `email`, `phone`, `address`, `tax_id`, `bank_details`, `beneficial_owner_data`; cascades to portal users and chat bodies | `Vendor.w9_file_key` is left set and the **W-9/W-8 document itself stays in object storage**, as do invoice PDFs, expense receipts, contract documents and chat attachments referencing the subject |
| Erasure of a user | Tombstones email, nulls `full_name`, SSO ids, `hashed_password`, `mfa_secret`; sets `is_active=False` | Their `WebAuthnCredential` rows survive erasure intact — `credential_id`, `public_key`, `rp_id` — and no session is revoked: no `block_token`, no session purge. **Access itself does stop**, because `get_current_user` re-loads the row and 401s on `not user.is_active` (`app/api/deps.py:134`, and the portal at `app/api/portal_deps.py:49`). What survives is the credential material and the Redis session record until its TTL (≤ `access_token_expire_minutes`, 30 by default), with an un-blocklisted JTI |
| DSAR export | Profile fields, related invoice/payment summaries, and **counts** of audit and notification activity | No uploaded documents or even references to them; no `Contract`, `Expense`, `VirtualCard`; for a user subject, no `WebAuthnCredential` or `ApiKey` detail. Art 15 is a right to the data, not to a tally of it |

**Blast radius.** Both are GDPR completeness failures — Art 17 for the erasure
legs, Art 15/20 for the export — and the object-storage leg is the sharp one,
because a W-9 carries a taxpayer identification number and the retention sweep
will never remove it either (it only soft-archives invoices). It is not a
regression: these paths have never covered storage. `backend/docs/privacy.md`'s
"what is redacted vs preserved" table is **silent** on object storage and
auth material rather than stating the limitation, so the gap is currently
invisible to anyone reading the docs.

**Recommended fix.** Erasure needs a storage leg that collects every `*_file_key`
reachable from the subject and deletes each through
`storage._delete_object`, plus a call into the existing session-revocation and
WebAuthn-credential paths; export needs the same collection step to enumerate
(and ideally bundle) those objects. The subtlety is that an invoice PDF is
*shared* evidence — it belongs to the money trail the erasure path deliberately
preserves — so deleting it wholesale is wrong. The likely split is: documents
whose sole subject is the erased party (W-9/W-8, their portal chat attachments)
are deleted; documents that are transaction evidence are retained under the same
justification as the invoice rows, and the policy says so. That judgment is why
this is an entry rather than a same-session fix.

**Interim honesty.** `/legal/privacy` §12 and the DPA's deletion clause both
state these limitations explicitly rather than claiming completeness, so nothing
published is untrue while the gap stands.

**Not closed by tenant deletion, and worth saying so.**
`services/tenant_deletion` (2026-09-16) sweeps a whole tenant's object-storage
prefix via the new `storage.delete_prefix`, which is the traversal this entry's
fix also needs — but it is deliberately NOT the tool for this one. Deleting a
*tenant* is unconditional: the customer is gone, so every object under
`{org_id}/` goes. Erasing a *subject* inside a live tenant is the selective case
this entry describes, where an invoice PDF is shared transaction evidence the
money trail keeps and a W-9 is not. The per-key collection step is still the
work; what exists now is a worked example of talking to the bucket at all.

## Organization settings overflows horizontally at 320px (WCAG 1.4.10)

**Found:** 2026-09-15, while extending the 320px reflow guard to a route that
renders `SectionTabs` (`docs/decisions.md` §174).

`/organization` scrolls the document sideways by **137px** at a 320px viewport,
independently of the section tab bar. Measured with the tab bar's own
measurement row excluded, after the page's content has loaded:

| element | left | right | width |
|---|---|---|---|
| `a.btn-outline` | 191 | 457 | 266 |
| `a.btn-outline` | 191 | 449 | 259 |
| `a.btn-outline` | 184 | 389 | 204 |
| `button.btn-test` | 289 | 431 | 142 |

Same defect class as the tab bar: flex rows whose children are wide and
`white-space: nowrap`, in a container with no `flex-wrap`, so the row cannot
shrink and pushes the page instead. `.erp-test-row` was fixed in that change
(it now wraps); the `.btn-outline` rows were left, because they are a different
set of containers on the same page and fixing them properly is a responsive
pass over the whole settings page rather than a one-line rule.

**Blast radius:** the settings page only, and only below roughly 460px. No data
is wrong and nothing is unreachable — the page scrolls — but it fails WCAG
1.4.10 Reflow, which this project claims conformance to
(`docs/accessibility.md`), so it is a compliance defect rather than a cosmetic
one.

**Fix approach:** give each offending row `flex-wrap: wrap` (or make the
`.btn-outline` group a wrapping grid) and re-add `/organization` to the reflow
loop in `tests-e2e/a11y/screen-reader.spec.ts`, which is deliberately scoped to
`/contracts` today and carries a comment pointing here.


## ~~A named-but-unregistered CARD provider still falls back to `mock`~~ — FIXED 2026-08-21

**Resolved.** `card_adapters/dispatcher.get_card_adapter` now raises
`UnknownCardProviderError` for a NAMED provider it has no adapter for; an unset
provider still resolves through `REGION_DEFAULTS` (the local-first default). The
dispatcher imports the three built-in adapters itself, so the refusal no longer
depends on each call site's `import app.services.card_adapters.lithic` preamble.

*The defect.* `MockCardAdapter` is not an inert stub — `create_card` returns
`success=True` with a `mock_card_…` id and `last_four="4242"`, `get_card_details`
returns the PAN `4242424242424242`, `cancel_card` returns `True` unconditionally.
One typo in `settings.cards.provider` made every issuance "succeed": rows landed
with `card_provider="mock"`, the payment-run card leg marked each payment
`completed` and each invoice `payment_scheduled`, `POST /api/cards/generate`
reported cards minted, and vendors were emailed reveal links resolving to a
fixture PAN.

*The per-caller table §29 requires*, which is why this took its own change:
`issue_card_for_invoice` returns `card_provider_not_configured` (no provider call
was made, so `payment_runs.classify_payment_failure` already reads the
`_not_configured` suffix as RETRY_SAFE); `POST /api/cards/generate` 409s the batch,
because a per-invoice `continue` reported `total: 0` — indistinguishable from
"nothing was eligible", which is how this stayed invisible;
`GET /cards/{id}/details` 409s rather than returning the fixture PAN;
`POST /cards/{id}/cancel` 409s and leaves the row `active`;
`cancel_card_at_provider` records `card_provider_not_configured` rather than a
cancel it never obtained; and the supplier-portal reveal degrades to its PII-free
body. **The card webhook needed no change** — the recommended fix assumed
otherwise, but `POST /api/cards/webhook/{provider}` normalises by the URL segment
and returns for anything but `lithic`/`nium`; it never resolves an adapter.

The same sweep closed the identical shape in two more registries:
`positive_pay_adapters` (an unknown `bank_format` rendered CSV under the requested
name — a fraud control the bank silently could not enforce) and
`enrichment_adapters` (an unknown provider fabricated firmographics with
`matched=True`, one click from being applied onto a real supplier). See
`decisions.md` §56.

Guard: `tests/test_card_provider_resolution.py`.

---

## ~~An invoice pushed to the ERP mid-run records a settled payment as `failed`~~ — FIXED 2026-08-21

**Resolved** in `api/payments._execute_single_payment`, the way the diagnosis
recommended: re-check payability *before* the adapter call rather than let an
invalid transition surface after it.

*The defect.* A run built while the invoice was `approved` could have
`POST /api/invoices/{id}/send-to-erp` walk it to `sent_to_erp` before `/execute`.
`sent_to_erp → payment_scheduled` is not in `VALID_TRANSITIONS`, and the
transition ran **after** `adapter.create_payment` returned and
`provider_payment_id` was assigned — so `validate_transition`'s 409 unwound into
`_dispatch_run_payments`' generic `except`, recording
`failed / unexpected_error:HTTPException` on a payment the processor had already
accepted. Nothing corrected it: `classify_payment_failure` read the populated
handle as `IN_DOUBT` (so `/retry-failed` refused), the webhook won't advance an
already-terminal payment, and the reconciler only polls `submitted`/`processing`.

*The fix.* The payability re-check sits beside the credit-memo
`net_amount_changed` guard — before any order exists at the processor, hence
retry-safe by construction. The payment fails with the named
`invoice_not_payable:<status>` (added to `_RETRY_SAFE_FAILURE_PREFIXES`), and
`/retry-failed`'s own payability gate keeps skipping the row until the ERP push
completes and the invoice reaches the payable `posted_in_erp`.

`sent_to_erp` is also gone from the three dispatch legs' transition branch — but
not by deleting a literal: `api/payments.SCHEDULABLE_INVOICE_STATUSES` is now
**derived** from `workflow_engine.VALID_TRANSITIONS`, so the branch can never
again name a status the state machine refuses.

Coverage: `backend/tests/test_payment_run_invoice_payability.py`. Doc:
`backend/docs/payments.md` § The invoice's payability is re-checked before the
adapter call.

## ~~`extraction_dispatch` / `erp_dispatch` run on a foreign event loop~~ — FIXED 2026-08-17

**Resolved**, both of them, and the class of bug is now closed at a chokepoint
rather than per-dispatcher.

*The defect.* All three `*_dispatch` paths ran their work in a detached thread
on a brand-new event loop. `app.database`'s `control_engine` / `_tenant_engines`
belong to the loop that first drives them, and an asyncpg connection cannot
cross loops — it raises `RuntimeError: got Future attached to a different loop`
**and** can return the half-used connection to the pool the *request* path
draws from, after which unrelated endpoints hang. In `payment_erp_sync` that
produced seven `RuntimeError`s in one CI run and nine failing e2e specs whose
only symptom was `PATCH /api/organization` timing out.

*Why creating your own engines wasn't enough.* Every dispatcher already built
its own. The leak was `transition_invoice`'s hooks — `notification_dispatch`,
`audit_dispatch`, `webhooks.dispatch` — which each open their OWN control-plane
session (and `dispatch_audit` its own tenant engine) by reaching for the module
global. That is code a dispatcher never calls and cannot pass a session to.

*The fix, two shapes.* `erp_dispatch` moved to `asyncio.create_task` on the
caller's loop (its send is `await`-only I/O — httpx plus `asyncio.sleep`
backoff), matching `payment_erp_sync`. `extraction_dispatch` **keeps** its
worker threads, because extraction runs PyMuPDF rendering and Tesseract OSD —
synchronous CPU work that would stall the request loop — and because the pool
is also the concurrency limiter keeping bulk uploads under provider rate
limits. It instead declares its loop-local engines once via the new
`database.dispatch_engine_scope`, and every `control_session_factory()` /
`get_tenant_engine()` beneath it resolves to those. A `ContextVar` carries the
binding: a new thread starts with an empty context, so a worker can never leak
its engines into the request path, and `create_task` copies the context so
nested work inherits them.

`control_session_factory` became a function (all ~70 call sites already spelled
it `control_session_factory()`); `_default_control_session_factory` is the
sessionmaker the pytest harness rebinds with `.configure(bind=...)`.

Also fixed in passing: `erp_dispatch._run_local` disposed its tenant engine in
a trailing statement rather than a `finally`, so an early `return` (invoice not
found) leaked a whole pool per send.

Coverage: `backend/tests/test_dispatch_engine_scope.py` — proven load-bearing
by reverting the indirection and watching four fail, including the two that
assert `notification_dispatch` and `audit_dispatch` pick up the scope *without
being edited*, which is the property that makes this a chokepoint rather than
a patch. Plus loop-identity tests in `test_erp_dispatch.py` and
`test_payment_erp_sync.py`. Rule written up in `backend/CLAUDE.md` § Dispatch
modes → The event-loop rule.

---

## ~~An over-range settlement amount wedges the payment webhook~~ — FIXED 2026-08-17

**Resolved** by giving "reported but unstorable" its own state, which is what
the diagnosis said it would take.

*The defect.* `payments.settled_amount` is `NUMERIC(15, 2)` — 13 integer
digits. A processor reporting more parsed fine (`parse_amount` guards only
against values so large that `quantize` itself raises), verified fine, and then
raised `NumericValueOutOfRangeError` at the flush. That took the whole webhook
transaction with it: the `fraud_flag` the verdict had ALREADY decided on was
rolled back, the completion was never recorded, the handler 5xx'd, and the
processor retried into the identical failure forever.

*Why the obvious fixes were wrong.* Returning `None` (what the three CSV
parsers do) means "the rail reported no amount", which coverage deliberately
fails OPEN on — so a garbage figure would be laundered into a silent pass and
the invoice marked paid. Guarding only the persist and leaving the column NULL
lands in the same place for the same reason. Widening the column moves the
cliff without changing the semantics; no legitimate settlement is 14 integer
digits, so a value that doesn't fit is a corrupt or hostile report, not a big
payment.

*The fix.* `payment_settlement.persistable_settled_amount` is the one splitter
both writers use — the webhook and the reconciler backstop, so they cannot
disagree about what is storable. An over-range figure leaves `settled_amount`
NULL and sets the new `settled_amount_unstorable` (migration `0085`), which
`settlement_coverage` reads as `uncertain`: the invoice holds at
`payment_scheduled` behind the same accept / void exits a shortfall has. The
verdict is untouched, so the payment-blocking `fraud_flag` still lands, and the
figure survives verbatim on the append-only audit row (JSONB, no range limit).
The column carries the decision input; the audit row carries the evidence.

Coverage: the end-to-end webhook case (completion recorded, flag set, exception
raised, raw figure on the audit row — i.e. the transaction no longer rolls
back), a realdb `payment_erp_sync` test proving an unstorable leg holds **while
an amount-free sibling still reaches `paid`** (the two NULL cases must stay
distinct), and a drift guard tying `SETTLED_AMOUNT_NUMERIC` to the model column.

The migration was verified against real Postgres, not just the ORM: apply,
downgrade, re-apply (idempotent), no-op on the control plane, and fan-out to
every tenant. Worth doing — the first revision id was 38 characters and
Alembic's `alembic_version.version_num` is `VARCHAR(32)`, so the DDL applied
and the version bump then failed. Model-based tests would never have seen it.

---

## ~~A non-generatable recurring template silently skips every period~~ — FIXED 2026-08-16

**Resolved**, both halves, in `services/recurring_invoices.py`.

*The silent skip.* The defensive cursor advance was always right — a template
that can't generate must not spin the sweep forever — so what was added is the
missing other half. A skip now stamps a PII-free marker on
`RecurringInvoiceTemplate.meta.generation_skip` (`record_generation_skip` —
reason code, period, consecutive count, timestamp; settings-JSON, **no
migration**), writes a `recurring_template.generation_skipped` audit row
correlated on the template's own id, and rides a `last_skip` field on every
`/api/recurring` response, rendered as a *Not generating* badge on the
`/recurring` list. Past `MAX_CONSECUTIVE_SKIPS` (3) the sweep **pauses** the
template and audits that too (`recurring_template.paused`, `actor_id` NULL,
`source: "sweep"`) — the `services/scheduled_reports` auto-disable shape — so an
unfixable schedule stops claiming to be live. The marker is cleared by **every**
path that establishes the reason no longer holds — `generate_one`, the sweep's
already-generated no-op, `generate-now`'s idempotent branch, and (via
`clear_generation_skip_if_resolved`) `PATCH` and `resume` — which is what makes
`consecutive` mean consecutive; clearing only in the sweep left a manually-fixed
template with a stale count, so its next single miss tripped the three-miss
auto-pause. The "can it generate" verdict itself moved into one pure
`not_generatable_reason`, shared by `generate_one`, the sweep and the router's
`generate-now` 422, so the three can't drift.

The skip count is deliberately **not** a `*_failures` field on `SweepResult`:
`sweep_health.failure_count` sums those, and a template missing a vendor is a
tenant configuration problem, not a broken sweep — counting it would leave the
sweep permanently `degraded` for something no platform operator can fix. The
auto-pause is what bounds it instead.

*The per-tenant commit.* `_sweep_tenant` now selects template **ids**, then
re-reads, guards and commits each template on its own — the `vendor_rescreen`
shape. A template whose generation raises is rolled back, logged by exception
CLASS only, and counted as `template_failures` (which *does* feed
`sweep_health`), while its siblings keep the invoices they already generated.

Regression coverage in `backend/tests/test_recurring_invoices.py`: the persisted
marker + audit row, the auto-pause after N periods (and that a paused template
is left alone), the marker clearing once the template generates, the
sibling-survives-a-poison-template proof, the PII-out-of-logs assertion, and the
API's `last_skip` (present / absent / malformed-`meta` tolerated). Frontend map
guarded by `frontend/src/lib/types/recurring.test.ts`. Documented in
`backend/docs/recurring-invoices.md` § A skipped period is never silent and
§ One template's failure never costs its siblings their work.

---

## ~~A tenant with no ERP configured never gets an invoice to `paid`~~ — FIXED 2026-08-16

**Resolved** by narrowing the gate to what it actually guards.
`services/payment_erp_sync._sync_payments` no longer returns early on an absent
`settings.erp`; it carries `erp_config=None` into the leg loop, and
`_sync_one_leg` resolves the ERP adapter (and would perform the push) only when
a config exists. Every other per-leg guard is unchanged and shared by both
paths — payment `completed`, invoice `payment_scheduled`, settlement covering,
invoice taken `FOR UPDATE` — so an ERP-less tenant's settled invoices now reach
`paid` while an in-flight payment is still skipped and a *named but unsupported*
ERP type still fails its own leg into the de-duped `erp_reconciliation`
exception (the recoverability semantics of [decisions.md §22](decisions.md) are
untouched).

`get_erp_adapter({})` is deliberately not called on the no-ERP path: it fails
closed on an unusable config ([decisions.md §29](decisions.md)), which would
turn "this tenant has no ERP" into a permanent strand plus an exception row for
a situation that is not an error.

Kept as a stub because the *blast radius* is worth not re-deriving: this module
is the only automatic writer of `payment_scheduled → paid` (the other two are
the manual `POST /api/payments/{id}/settlement/accept` and `api/erp_webhook`,
which by definition requires an ERP), so the early return left every settled
invoice of a "direct schedule, no ERP" tenant at `payment_scheduled` forever —
under-counting the aging report, the `/dashboard` pipeline, the vendor's payment
history and the 1099 YTD totals, and never letting `retention_sweep` see the
invoice as archivable. The payment row stayed correct throughout, which is what
made it invisible from the payments page.

Regression coverage:
`backend/tests/test_payment_erp_sync.py::test_no_erp_configured_still_advances_the_settled_invoice`
(asserts the adapter is never resolved AND the invoice advances) plus
`::test_no_erp_configured_does_not_strand_an_unsettled_invoice` (the no-ERP path
reuses the same guards rather than a looser branch). Contract documented in
`backend/docs/payments.md` § ERP Payment Sync → No ERP configured skips the
push, not the transition.

---

## ~~Read-after-write race on every mutating endpoint~~ — FIXED 2026-08-06

**Resolved** by `commit_before_response` (`backend/app/database.py`), applied by
both session providers. The success-path commit now runs on the exit stack
FastAPI unwinds *before* `await response(scope, receive, send)`, so a `201` is
no longer returned for an uncommitted write. The post-`yield` commit remains as
a conditional backstop.

Kept as a stub because the diagnosis is worth not repeating: the root cause,
everything ruled out on the way to it (middleware, pool staleness, engine-cache
races), and why the fix is shaped the way it is, now live in
[decisions.md §20](decisions.md). Regression coverage —
`backend/tests/test_commit_before_response.py` — pins the *ordering* invariant
and drift-guards the FastAPI internal it relies on.

One finding is worth carrying forward: the documented network repro (rapid
create-then-read pairs) did **not** reproduce over loopback even while the defect
was measurably present, because server and middleware pacing decide whether a
client can observe it. Don't treat "the repro passes" as evidence this class of
bug is absent — measure the ordering instead.

---

## ~~Workflow-mutating e2e specs can strand a tenant on a disabled workflow definition~~ — RESOLVED 2026-08-08

**Still live, 2026-09-08 (round 26).** Observed again in a local full-suite run:
`feoh_e2e3` ended with `Default Workflow is_active=f` and a leftover active
`replacement-<ts>`, taking roughly twenty later specs in that worker down with
it. `globalSetup` **passed at start**, so the guard catches a strand inherited
from a previous run, not one created mid-run. Repairing the tenant and re-running
the failures serially turned every one of them green, which is how the cascade
was distinguished from real regressions. The residual risk the try/finally
pattern cannot close is therefore still open, and a per-worker teardown assertion
(not just a setup one) is what would close it.

**Audit:** every spec that mutates a workflow definition's `is_active`, step
`enabled` flags, or `is_default` — `tests-e2e/workflows/*.spec.ts`,
`tests-e2e/workflow-builder.spec.ts`, and the two files that touch the live
default in passing (`admin/delete-safety.spec.ts`,
`invoices/daily-journey.spec.ts`) — already wraps its mutation in a
`try/finally` that restores the exact prior state, or scopes itself to a
throwaway definition it creates and deletes. No spec needed a code fix; the
original diagnosis's "afterAll doesn't restore" theory didn't match what's on
disk today.

**What was actually missing** was the doc's own second recommendation: a cheap
guard for the residual risk the try/finally pattern can't close on its own — a
hard interruption (killed process, machine crash, a timed-out test whose
continuation never gets scheduled) skipping the `finally` block entirely.
Added `frontend/tests-e2e/fixtures/globalSetup.ts`, wired into
`tests-e2e/playwright.config.ts`'s `globalSetup`: before any test/worker
starts, it asserts every tenant (`acme`, `techflow`, `e2e1..N`) has exactly one
`is_default=true` workflow definition, `is_active=true`, with its `approval`
and `erp_export` steps enabled — the shape `backend/scripts/seed.py` creates.
A miss throws one clear, tenant-and-field-named error instead of a confusing
409 three specs later.

**The guard is proven, not just written**: this local dev Postgres had
genuinely accumulated the exact symptom the original diagnosis described —
`feoh_e2e1` and `feoh_e2e3` each carried a shared-scope `is_default=true`
"Invoice Processing" stub (all steps disabled) alongside the entity-scoped
`Default Workflow`. The new guard flagged both, by name, before any test ran.
Cleaned up (no `workflow_instances` referenced either stub, so a plain
`DELETE` restored the fresh-seed shape) and re-ran the guard clean. Then ran
the full `tests-e2e/workflows/` + `workflow-builder.spec.ts` suite (52 tests)
twice back-to-back against `e2e1` — all green both times, and
`workflow_definitions` for that tenant was bit-for-bit identical (one
`Default Workflow`, `is_active=t`, all three steps `enabled:true`) before,
between, and after both runs.

One thing worth carrying forward: the stray "Invoice Processing" stub wasn't
traceable to any spec in the current suite — none of them create a window
where zero workflows are active while also creating a new invoice with no
entity-scoped default in play, which is what the auto-create fallback needs to
fire. It most likely came from manual UI exploration against a long-lived
dev tenant, not test-run drift. The guard doesn't care which — it catches the
*state*, not the cause — but if it ever fires again, check for a new code path
that can leave zero active workflows before assuming it's the old bug back.

---

## ~~A dev backend on the same Postgres mutates the pytest tenant DBs mid-test~~ — FIXED 2026-08-08

**Resolved** by giving the `realdb` pytest harness its own control-plane
database per slot (`feohledger_pytest<N>` — slot 0 included) instead of
registering test orgs in the real, shared `feohledger`
(`backend/tests/conftest.py::control_db_name_for_slot`). A dev backend's
background sweeps (`extraction_reaper.run_reaper_loop` et al. —
`select(Organization.id, Organization.db_name)` with no filter against
whatever DB `settings.database_url` names) can no longer discover the
harness's test tenants at all, because their `Organization` rows no longer
live there. This also removes the cross-process contention the original
write-up flagged on the shared control plane's unique constraints (org slugs,
user emails) — concurrent slots now don't share a control-plane database
either.

Kept as a stub because the diagnosis is worth not repeating: the root cause
(background sweeps enumerating every org with no filter), the confirmation
run that ruled out a genuine test-ordering/leak bug, and why the fix landed
as its own change rather than folded into the original slot-claim work all
still apply as background. The current mechanism — naming, one-time-per-slot
provisioning, and the session-start schema self-heal it shares with the
tenant pair — is documented in `backend/CLAUDE.md` § Test databases and in
`control_db_name_for_slot`'s docstring in `backend/tests/conftest.py`.
Regression coverage is `backend/tests/test_realdb_harness.py` (the "control
plane is per-slot" section) — including a direct proof that a session opened
against `settings.database_url` cannot see an `Organization` row the harness
created for its slot.

One thing worth carrying forward: this is a **one-time-per-slot** cost
(provisioning a new database, or self-healing its schema, once per pytest
process — the same accepted trade-off the tenant pair already makes), not a
per-test one. Measured on a single realdb test reaching a cold slot: about 3
seconds slower than before this fix, which does not survive contact with a
multi-thousand-test suite where the fixture's setup is paid once regardless
of how many realdb tests follow.


---

## `payments/` e2e flakes when run alongside cross-directory specs locally

**Diagnosed 2026-09-06 · not fixed · local-only, CI is green · pre-existing**

Running `payments/` together with specs from other directories against the
shared local dev stack produces an intermittent failure — roughly one run in
three — and **the failing test moves**: `cards-pagination.spec.ts` ("Load more
appends the next page", the `page=2` response never arrives inside 30 s) on two
runs, `daily-journey.spec.ts` ("invoice scheduled leaves queue") on another.

**It is not caused by the specs added in round 24, and not by that round's
changes.** Measured on the round-24 integration branch:

| selection | runs | failures |
|---|---|---|
| `payments/` alone | 2 | 0 (98/98, 34 s) |
| `payments/cards-pagination.spec.ts` alone | 1 | 0 (3.1 s) |
| the three cards specs together | 1 | 0 (16/16) |
| `payments/` + the 3 **new** round-24 identity specs | 5 | 2 |
| `payments/` + 3 **pre-existing** cross-dir specs (control) | 3 | 1 |

The control is the decisive row: swapping the new specs for
`vendors/chip-counts`, `audit/console` and `experiments/load-states` reproduces
the flake at the same rate, so the trigger is *adding cross-directory specs to
the run at all*, not anything those specs contain.

**What is established:** the page state at failure is correct — the DOM snapshot
shows exactly 20 rows and a `Load more (20 of 22)` button, so the seed, the
tenant scoping and the first page are all right. The click is actionable and
lands. What does not happen is the follow-up request completing within the
timeout. The wait is armed before the click, so it is not the listener-attach
race that the same file's comment records fixing earlier.

**Most likely cause:** contention on the single shared local stack — one Vite
dev server, one backend process, one Postgres — once Playwright's worker
sharding changes. CI does not have this shape: it shards 14 ways with its own
stack per shard, and every shard is green.

**Not masked.** No timeout was inflated, no retry added, no assertion loosened —
which is why this is written down instead. The durable fix is the same one
[`followups.md`](followups.md) already carries for e2e tenant isolation: specs
that write must not share a worker tenant with specs that count. Until that
lands, run `payments/` on its own when working locally.

---

## Two `queue-blocked` e2e cases fail on a fully-seeded local tenant

**Diagnosed 2026-09-04 · not fixed · local-only, CI is green**

`frontend/tests-e2e/payments/queue-blocked.spec.ts` — *"an unknown
`blocked_reason` code still blocks, with a generic reason"* and *"a
`payment_reconciliation` block names its own reason, not the generic one"* —
fail on a local tenant seeded with the full `pnpm seed`. They pass in CI, which
runs `seed.py --lean` (10 invoices per tenant against this tenant's ~30
payable).

Round 17 took this file from four red cases to two: the other two were the
page-one assumption fixed by `loadMoreUntilRow` (see the commit *"navigate to
the row instead of assuming it is on page one"*). These two are a different
cause and are **not** that.

**What is established:**

- The invoice the test creates **does** reach the queue API. Probed directly
  against a running backend: created through the same path the spec uses
  (`POST /api/invoices`, then `status='approved'` + a real `vendor_id` by SQL),
  it appears in `GET /api/payments/queue` — `total` rises by one and the
  invoice number is present in `items`.
- It is **not** inter-test pollution: both cases fail when run in isolation
  with `-g`, not only after their file-mates.
- It is **not** the client-side `injectBlockedFlag` interception per se — the
  passing case at `:210` uses the same helper.
- `loadMoreUntilRow` returns without throwing, which means it exited on
  `loadMore.count() === 0` (no Load-more control present when it checked)
  rather than on a poll timeout. With ~30 queue rows against
  `QUEUE_PAGE_SIZE = 20`, a Load-more control is expected to be there.
- Two independent agents confirmed both cases fail identically against
  unmodified `HEAD`, so nothing in round 17 introduced them.

**What is not established:** why the row is absent from the rendered list when
the API response demonstrably contains it. The gap is between the queue
response and the rendered rows, not in the backend.

**Durable fix:** capture a Playwright trace of one failing run and read the
actual queue responses and rendered rows out of it — the cheap diagnostic that
was not run here because the local dev server had to be brought up by hand and
the earlier throwaway probe was invalid (it never authenticated, so it captured
no queue request at all). Do that before theorising further.

**Trigger:** the next change to the payments queue page or to
`queue-blocked.spec.ts` — or the first time either case fails in CI, which
would mean the seed volume assumption above is wrong and this is not local-only
after all.

**Not masked:** no timeout was raised, no retry added, and neither case is
skipped. They fail loudly on a fully-seeded local tenant, which is the correct
behaviour for a test whose premise is not yet understood.

**Round 27 ruled out one candidate cause, and named a discriminator for the
class.** `cards/lifecycle.spec.ts` failed on the same "only on a fully-seeded
tenant" pattern and was root-caused: its fixture query took the first payable
invoice with no `ORDER BY` and no vendor filter, while the endpoint under test
skips a vendorless invoice and returns a cheerful `201` with an empty list. On
`feoh_e2e1` — which holds three vendorless payable rows stranded in June — it
drew one every time, so that failure was deterministic rather than intermittent.
That is fixed ([decisions.md](decisions.md) §128).

It is **not** these two cases. `queue-blocked.spec.ts:61` uses
`SELECT id FROM vendors WHERE status='active' LIMIT 1` and then creates its own
invoice against whatever it gets, so every candidate is equally valid and row
order cannot select something the code under test rejects. Different root cause;
this entry stays open.

The reusable rule: **an unordered `LIMIT 1` is only dangerous when the candidate
set is wider than what the code under test accepts.** Then row order silently
decides whether the test exercises anything. The idiom appears at roughly twenty
sites and the overwhelming majority are the benign kind. When triaging the next
"passes in CI, fails locally" case, apply that test before assuming volume.

---

## Local e2e tenant databases drift behind `alembic head`, and the suite blames the app

**Diagnosed 2026-09-08 (round 26). Local only — CI is unaffected.**

A local full-suite run reported 37 failures. Repairing an unrelated stranded
workflow (above) and re-running serially cleared ten; the rest were not code.
`alembic_version` on `feoh_e2e1` read `0086` against a repo head of `0093`, and
`POST /api/expenses` was returning a **500**:

```
asyncpg.exceptions.UndefinedColumnError: column "payment_method_before_match"
of relation "expenses" does not exist
```

That column is added by migration `0089`. It accounts for the whole `expenses/*`
cluster, and most likely for `tax/box-allocation` and `notifications/chip-counts`
too (their psql setup also errored on a `virtual_cards` INSERT during the
parallel run), though those two were not individually root-caused.

**Nothing surfaces this.** The backend boots fine — SQLAlchemy does not check the
live schema at import. `globalSetup` only validates workflow shape. And the specs
report ordinary assertion failures (`expected 201, received 500`) that read
exactly like application bugs, which is the expensive part: the failure points at
the feature rather than at the database.

**Workaround:** run `pnpm migrate:all` after pulling anything that adds a
migration.

**A pre-run guard would be worth more than this note.** `globalSetup` already
opens the control plane, so comparing each tenant's `alembic_version` against the
newest file in `backend/alembic/versions/` and failing fast with both numbers
would turn a confusing afternoon into one line. Recorded rather than built
because `globalSetup` is shared by every worker and the change wants its own
review.

CI is unaffected: each shard creates a fresh database and migrates it, so the
drift cannot exist there.
