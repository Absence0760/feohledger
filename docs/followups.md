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

**Last reconciled:** 2026-10-31 (round 4, test harness) — guarding mock-DB tests against the real engine (known-issues, struck) opened two (c) entries, the deploy-script tests failing on macOS and the rename script hanging across local slots, taking the file from 149 → 151. Before that, 2026-10-31 (round 4) — the follow-ups batch closed four (c) entries (the per-org host sweep, decisions §345; the missing Generate card action, §346; `/adaptive`'s Retry; two stale payment texts), narrowed the restated-predicates entry by the invoice (§347) and expense (§348) controls, and opened four (bulk `done` with no live-claim check, mobile approve, English expense refusals, two guard gaps), keeping the file at 149. Before that, 2026-10-31 (round 3) — the follow-ups batch closed six (c) entries (the resume in-doubt retry, decisions §343; the PEPPOL / Ollama hosts, §339; localhost CORS, §338; zero-total POs, §340; the CSV near-duplicate, §341; card-generate skips, §342), narrowed the restated-predicates entry by six payments controls (§344), and opened four (the exported-run void, no Generate card action, the per-org host sweep, two stale payment-path texts), taking the file from 151 → 149. Before that, 2026-10-31 (round 2) — the follow-ups batch closed six (c) entries (the last three PO listings and the write-back sentences, decisions §332; the ERP push ignoring vendor status, §333; the SSRF re-resolution, §334; the OAuth login-CSRF, §336; the unconfirmed QuickBooks realm, §337) and opened six (zero-total POs on three adapters, sanctions false-positive adjudication, two unguarded configurable hosts, localhost CORS in production, the mobile ERP refusal, `/adaptive`'s missing Retry), keeping the file at 151; the resume/retry in-doubt entry's trigger fired (§335). Before that, 2026-10-31 — the follow-ups batch closed six (c) entries (four PO listings reading a failed read as complete, decisions §329; the unlocked run builder, §330; the hand-rolled load-error blocks; the three copied buttons; the assistant stream's 429; the English ERP reconciliation sentences, §331) and opened two (c) entries (three adapters still reading a failed PO listing as empty, and the uncatalogued QuickBooks write-back sentences), taking the file from 155 → 151. Before that, 2026-10-26 — re-keying old Merge.dev PO rows in the sync (decisions §328, later in PR #670) replaced its (c) entry with a (b) one, running that sync per Merge.dev tenant after deploy, keeping the file at 155. Before that, 2026-10-26 — refreshing a synced PO's lines (decisions §327, later in PR #670) closed its (c) entry, taking the file from 156 → 155. Before that, 2026-10-26 — refusing a cancelled PO in the matcher (decisions §326, later in PR #670) closed its (c) entry, taking the file from 157 → 156. Before that, 2026-10-26 — scoping the PO sync's re-match to the synced entity and skipping paid / done invoices (later in PR #670) closed its (c) entry, taking the file from 158 → 157. Before that, 2026-10-26 — the PR #670 branch-wide money-path review opened five (c) entries (old Merge.dev PO rows, the re-match's scope, no exit for a never-priceable PO, PO lines never refreshed, the matcher ignoring PO status), taking the file from 153 → 158. Before that, 2026-10-26 — the branch review's skipped-PO wiring for NetSuite, Business Central, Sage (ZA) and Intacct (decisions §325, later in PR #670) opened one (c) entry, their PO listings still reading a failed or capped read as complete, taking the file from 152 → 153. Before that, 2026-10-26 — showing the "re-check now matches" note on `/exceptions` and the mobile exception detail (later in PR #670) closed its (c) entry, taking the file from 153 → 152. Before that, 2026-10-26 — letting a failed received-goods read fail `/api/analytics/cfo` instead of reporting a zero GR/IR leg (later in PR #670) closed its (c) entry, taking the file from 154 → 153. Before that, 2026-10-26 — withholding a marked PO's stale total on every surface and leaving it out of budgets and accruals (decisions §323, later in PR #670) closed the (c) entry for it and its review opened one, the received-goods accrual swallowing every error into zeros, keeping the file at 153. Before that, 2026-10-26 — wiring Blackbaud FE NXT into the skipped-PO report (decisions §324) closed its (c) entry, taking the file from 155 → 154. Before that, 2026-10-26 — making the ERP PO sync's re-match hold-only (decisions §323, amended) opened two (c) entries, rendering its "re-check now matches" note and the CISO question on the next-edit auto-close, taking the file from 153 → 155. Before that, 2026-10-26 — reporting the POs an ERP adapter skips (decisions §323) opened two (c) entries, wiring Blackbaud FE NXT into that report and the stale total still shown and summed outside the match, taking the file from 151 → 153. Before that, 2026-10-26 — review of the sub-processor change notice opened one (c) entry, a notice that reaches nobody (no active administrator, or administrators the provider refused) with no procedure behind the counters, taking the file from 149 → 150. Before that, 2026-10-26 — the sub-processor change notice (decisions §321, #427) replaced its (c) entry with a narrower one, the missing privacy/DPO contact, and opened one (b) entry, turning the sweep on in production, taking the file from 148 → 149. Before that, 2026-10-22 — keeping the ERP's reported payment (decisions §319) opened one (c) entry, the ERP reconciliation sentences still English-only, taking the file from 147 → 148. Before that, 2026-10-22 — recording real QuickBooks payment shapes (decisions §318) opened three (c) entries for the walkthrough's user-visible testing gaps (fields in view, the layout-shift budget, a scripted first-customer rehearsal) and one (b) entry, the first recording, taking the file from 143 → 147. Before that, 2026-10-22 — fixing the page-change layout shifts on /invoices, /payments and /exceptions opened two (c) entries, the brand-accent flash on a cold load and the auto-layout tables re-flowing their columns, and fixing the QuickBooks bill-payment reader (decisions §317) opened one (b) entry, clearing up what the wrong reader left in production — together taking the file from 140 → 143. Before that, 2026-10-22 — closing #575, which narrowed its day-one entry to the one gap left (no public-DNS API smoke check in `vm-deploy.yml`) and kept the file at 140. Before that, 2026-10-21 — merging main (#634, #636, #638, #640) into the live-claim ERP refusal branch (§309); both sides kept, taking the file to 140. Before that, 2026-10-21 — merging main (the uptime checks, #634: one (b) and one (c) entry) into the first-client UX branch; both sides kept, taking the file to 139. Before that, 2026-10-21 — merging main (the bundle-metric change, #640, which closed the bundle-total (c) entry) into the first-client UX branch, taking the file to 137. Before that, 2026-10-21 — merging main (the `feoh_app` cutover prune, #638) into the first-client UX branch (issue #635); both sides kept, taking the file to 138. Before that, 2026-10-21 — fixing the payments-spec data leak (issue #635) opened one
(c) entry, leaked draft payment runs and a leaked vendor, taking the file from 138 → 139. Before
that, 2026-10-21 — removing `transition: all` (issue #635) opened one (c)
entry, three pages hand-rolling a shared button, taking the file from 137 → 138. Before that,
2026-10-21 — merging main (§306 workspace closure, #624, #632) into the
first-client UX branch (issue #635); both sides' entries kept, this branch's decisions renumbered
§312–§315, taking the file to 137. Before that, 2026-10-21 — coding the shared rate limiter's 429 (issue
#635) opened one (c) entry, the assistant stream reading every 429 as a budget
error, taking the file from 131 → 132. Before that, 2026-10-21 — the sidebar renames (issue #635 #23,
decisions §314) opened one (c) entry, hiding Partner Admin from non-partners,
taking the file from 130 → 131. Before that, 2026-10-21 — extracting `ui/LoadError` (issue #635) opened
one (c) entry, the pages still hand-rolling a load-error + Retry block, taking
the file from 129 → 130. Before that, 2026-10-21 — merging main (§305, §306) into the #575 round-1 branch, both sides' entries kept, taking the file to 134. Before that, 2026-10-21 — merging main (the UI action-verdict pass, §305) into the workspace-closure branch; both sides' entries kept, taking the file to 131. Before that, 2026-10-21 — the UI action-verdict pass (issue #575,
decisions §305) opened three (c) entries — the modal's assignee clause, bulk
status approving a draft past review, and the pages still restating server
predicates — taking the file from 126 → 129. Before that, 2026-10-21 — storing the ERP document id on the invoice
(decisions §302) opened two (c) entries, the deep link not choosing the ERP
organisation/company and the ERP webhook's id-only path, taking the file from
124 → 126. Before that, 2026-10-21 — auto-sending an approved invoice to the ERP
(decisions §304) opened one (c) entry, an approval whose post-commit push never
ran, and one (b) entry, confirming each tenant's stored `auto_send_on_approval:
true` before deploying it, taking the file from 122 → 124. Before that, 2026-10-21 — the adapters' honest paid read (decisions
§303) split the "ERP status polling and void are unwired" (c) entry into the
non-QuickBooks status sync-back gap and the void/recall gap, taking the file
from 121 → 122. Before that, on this branch, 2026-10-21 — merging main (QuickBooks status sync and
payment write-back, Xero Disconnect, PO vendor link by supplier id) into the
#575 round-1 branch; both sides' entries kept, taking the file to 124. Before
that, 2026-10-20 — the #575 money-path review (decisions §308)
opened one (c) entry, the invoice currency that cannot be unknown, taking the
file from 119 → 120. Before that, 2026-10-20 — giving backend rules a single owner (issue #575 §3)
opened two (c) entries, the payment-schedule readers that join every row and the CSV
importer's byte-exact duplicate skip, taking the file from 117 → 119. Before that,
2026-10-21 — merging main (QuickBooks status sync, Xero
Disconnect) into the PO vendor-link branch; both sides' entries kept, taking
the file to 121. Before that, 2026-10-20 — the live Xero Demo Company test opened two
(c) entries (unapproved ERP POs syncing as open, and the adapters that still
link a PO's vendor by name only), taking the file from 117 → 119. Before that,
2026-10-21 — merging main's Xero Disconnect fix into the QuickBooks status-sync
branch kept both sides' entries. Before that, 2026-10-20 — fixing Xero's Disconnect so it revokes the
grant opened one (c) entry (Sage Accounting and Blackbaud still don't), taking
the file from 117 → 118. Before that, 2026-10-20 — QuickBooks Phases 3–5 (decisions §295)
narrowed the QuickBooks entry to the header-only-tax product call, closed the
"ERP-reported payment is dated the day the webhook arrives" (c) entry, and
opened one (c) (a bill paid in QuickBooks while a run is already executing) and
one (b) (turning status sync on in production), taking the file from 117 → 118;
the dispatch-time ERP check (decisions §296) then closed that (c) and opened
one for a NACHA file's bills being read only at export, 118 → 118.
Before that, 2026-10-20 — the admin persona's user-management fixes
(decisions §293) closed the (c) entry "A SCIM deactivation writes no audit
row", taking the file from 118 → 117. Before that, 2026-10-20 — the workspace-closure review (decisions
§306) opened one (c) entry, `lambda` audit mode refusing a self-service close,
taking the file from 119 → 120. Before that, 2026-10-20 — self-service workspace
closure (decisions §306) opened one (c) entry, abandoned workspaces kept forever
unless someone closes them, taking the file from 118 → 119. Before that, 2026-10-20 — fixing signup's dead ends (decisions §292)
opened one (c) entry, the missing self-service "find my workspace" recovery,
taking the file from 117 → 118. Before that, 2026-10-20 — the cross-workspace identity fixes (decisions
§291) opened two (c) entries, multi-workspace membership and the SAML ACS's
other refusals still landing as raw JSON, taking the file from 115 → 117.
Before that, 2026-10-20 — the light theme (decisions §289) opened one
(c) entry, a white-label accent left unchecked as text on the light surfaces,
taking the file from 114 → 115. Before that, 2026-10-20 — the live QuickBooks re-test (#598) reached
the sync screens' entry, which landed (coded `erp_listing_incomplete` /
`erp_not_connected` answers, parent and too-long values reported), and narrowed
the posted-bill entry to the other adapters' pre-flight refusals, taking the
file from 115 → 114.
Before that, 2026-10-19 — reviewing the QuickBooks live-test fixes
(decisions §284–§287) opened eight (c) entries (listings over the sync bound,
the collapsed row §284 accepts, the sync UI's reporting, over-long names, the
exact-name ambiguity rule, adapters that still drop or mislabel records, vendor
linking gaps, and mock syncs judging pre-0115 links), and extended the
posted-bill entry with `possibly_posted`, taking the file from 107 → 115.
Before that, 2026-10-19 — refusing the ERP push for an open duplicate or
fraud flag (decisions §286) opened three (c) entries (a flag raised after the
bill is already in the ERP, human-cleared findings of the other types
re-opening, and the push not checking the vendor's status), taking the file
from 104 → 107. Before that, 2026-10-19 — giving Free the direct ERP adapters
(decisions §281) built the Scale-only `erp_merge` gate, closing its (c) entry and
taking the file from 106 → 105. Before that, 2026-10-19 — the first production deploy (#566) opened one
(c) entry, the day-one test gap tracked in #575, taking the file from 103 → 104. Before that, 2026-10-12 — evaluating Baidu's Unlimited OCR opened
one (c) entry, a scanned-invoice benchmark before any self-hosted OCR
adapter, taking the file from 102 → 103. Before that, the commerce inventory sync (decisions §273)
landed with its five providers (WooCommerce, Shopify, Square, Lightspeed
X-Series, Cin7 Core): the Priority 14 commerce entry closes, and five (c)
entries open — the OAuth-consent connection, WooCommerce's read → write race,
Lightspeed R-Series, the Cin7 Core push's limits and Cin7 Omni — taking the
file from 98 → 102. Before that, 2026-10-11 — PR #544 opened and closed two (c) entries in the
same change (the OAuth-adapter e2e coverage gap, and only QuickBooks refreshing after
a 401 — now shared as `oauth_base.RefreshOnUnauthorized`), so the file stays at 98. Before that, 2026-10-09 — enabling IAM database authentication on RDS
(#542) opened one (c) entry (the app still signs in with the master password),
taking the file from 97 → 98. Before that, 2026-10-09 — the US + South Africa ERP connections
(decisions §269–§272), merged onto the provider-credential store, opened
eleven (c) entries, one (a) (sandbox verification) and one (b) (registering
the platform OAuth apps) under their own heading, and rewrote two (c)
entries: QuickBooks Online now tracks only phases 3–5, and the Priority 14
adapter entry now tracks the receipt pull and the enterprise ERPs. The
branch's "payment and card secrets still plaintext" entry is not carried,
since §266 sealed them, and the "switching provider keeps the old sealed
secret" entry is narrowed to payments and cards: §271 drops an ERP's sealed
secrets on a new ERP or destination. Review of the merge added two more (c) entries (tenant deletion not revoking
an OAuth grant; QuickBooks' realm id taken from the callback query). That takes
the file from 82 → 97.
Before that, 2026-10-08 — the cards / receiving-SoD /
provider-credentials batch closed **five** (c) entries — a vendor leaving
`active` keeping its live cards (decisions §264), `/cards/generate`'s unlocked
checks (§265), ERP / payment / card credentials in plain JSONB (§266), the
goods-receipt recorder approving the invoice (§267), and a manager's
re-extraction auto-approving a clerk's swapped file (§268) — and opened seven
under their own heading, taking the file from 80 → 82. Before that, 2026-10-07 — a re-validation pass checked all 82 open
entries against the code on `main` (`02f7387b`) rather than against their own
text. One had landed without being pruned — the void reversing a captured
discount by elimination, fixed by `discount_offers.captured_by_payment_id`
(migration 0104) — and is removed, taking the file from 81 → 80. Six described
the code wrongly and are corrected inline (marked *corrected* or *re-measured
2026-10-07*): the USD-only pricing page, the deploy role's target and sops
step, the breakpoint count, the `/organization` size, where pre-approval
cover is computed, and SOC 2's "all engineering prereqs are complete". Every
other entry was confirmed still open. Before that, 2026-10-07 — binding approval to the version the
approver saw (decisions §263) closed **three** (c) entries — approval not tied
to the loaded version (in-app, bulk, and the email / Slack / Teams links),
`bulk/delete` with no entity scope or row lock, and bulk `new →
ready_for_review` skipping `/complete`'s required-field check — taking the
file from 84 → 81. Before that, 2026-10-07 — enforcing the plan feature gates (decisions
§258) opened one (c) entry — the per-tenant audit-log SIEM export the Scale plan
lists but nothing implements. Before that, 2026-10-07 — moving the database onto RDS (docs/minimal-deployment.md
§ Database) opened two (c) entries — the published sub-processor register and
DPA still placing the databases on the VM's disk, and the socket-form
`pg_isready` left in dev and CI — taking the file from
74 → 76. Before that, 2026-10-07 — the public pricing page now renders the plan
catalogue it is generated from (`pnpm gen:pricing` / `check:pricing`, decisions
§253, issue #426) and every remaining public figure has a derivation in
[marketing-substantiation.md](marketing-substantiation.md), closing **two** (c)
entries — the pricing page describing a different product than the billing
code, and the missing substantiation file — taking the file from 76 → 74. Before
that, 2026-10-06 — the no-rail pilot (issue #517, decisions §251)
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

**151 open: 126 (c) · 12 (a) · 13 (b)** — re-derived from the file, never carried
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

- [ ] **The sub-processor notice has no privacy / DPO contact to reach.**
      ([#427](https://github.com/Absence0760/feohledger/issues/427)) The notice
      itself is built (decisions §321): a `notice=True` entry in
      `backend/app/services/subprocessor_changelog.py` is published on
      `/legal/sub-processors` § 10 and emailed to every active administrator of
      every organisation, once, with a ledger row and an audit row. What it
      cannot reach is a customer's DPO or privacy contact who is not an
      administrator — the platform holds no such field, so `/legal/dpa` § 8
      offers to copy the notice to another address *by hand* on request to
      `privacy@`. That manual half is the part still resting on a mailbox.
      **Durable fix:** an optional per-organisation privacy-notice address
      (an audited writer like `PUT /api/organization/sso`, refused by the generic
      `PATCH /api/organization`, a field on the organisation settings page),
      added to the notice's recipients in `services/subprocessor_notices._admins`;
      then drop the by-hand sentence from DPA § 8 and register § 9. Deciding
      whether the contact is *instead of* or *as well as* the admins is the
      contractual call counsel should make first.
      **Trigger:** the first customer who asks for the notice at a non-admin
      address, or before the DPA is countersigned by a customer with a DPO.

- [ ] **A sub-processor notice can reach nobody, and nothing tells a person.**
      (decisions §321) Two cases leave a customer organisation without the
      notice the DPA promises: it has **no active administrator** (the sweep
      releases its claim, counts it as `organizations_without_admin` on
      `GET /api/health/sweeps`, and retries every tick — it stays owed), or
      **some of its administrators' emails were refused** on a notice that
      otherwise went out (`failed_recipients`; never re-sent, by design, so the
      others are not mailed twice). Both are visible — the health counters and
      `python scripts/subprocessor_notices.py list`, which names each partly
      delivered notice — but nobody is paged and no procedure says what to do,
      so the 30 days can run out on a counter nobody reads.
      **Durable fix:** an operator procedure in `docs/founder-runbooks/`
      (resend by hand to the missed admins or the organisation's contract
      contact, then record it), plus an alert on a non-zero
      `organizations_without_admin` / `failed_recipients` once the sweep-health
      alerting can key on a counter rather than `failures` alone; the
      privacy-contact field above would give the no-admin case a second address.
      **Trigger:** the first `notice=True` change-log entry, or the first
      non-zero value of either counter in production.

### Live Stripe billing has no subscription to invoice against

- [ ] **Nothing creates the customer's Stripe subscription, so live AI-read
      overage has nowhere to be invoiced.** AI-read metering ships end to end
      against the `mock` adapter (decisions §253, §255): the gate, the pause, the
      cap, the notices, and overage meter events reported per unit with an
      idempotent ordinal identifier. `provision_org_billing` also resolves the
      `ai_invoice_overage` meter and each paid plan's per-unit metered price
      (`settings.billing.ai_overage_price_ids`). But `StripeBillingAdapter
      .create_subscription` — which adds that metered price as `items[1]` — has
      no caller: plan changes repoint our `Subscription` row and never create or
      amend one at Stripe. Stripe records meter events against the customer
      either way, but invoices them only for a subscription carrying the
      metered price, so today the overage would be reported and never billed.
      **Durable fix:** on a move onto a paid plan, create (or amend) the Stripe
      subscription with the base price + the metered overage price, billing
      cycle anchored on the 1st (UTC), and persist
      `Subscription.external_subscription_id`; a `customer.subscription.*`
      webhook then keeps the status in sync. Until then an operator does this by
      hand per paid customer in the Stripe dashboard.
      **Trigger:** before `FEOH_BILLING_PROVIDER=stripe_billing` is set for any
      org on a paid tier.
      Ref: [billing.md](../backend/docs/billing.md) § What the operator configures in Stripe.

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
      The figures are no longer hardcoded `$`: `lib/marketing/pricing.ts`
      formats every one through `formatMoney` with the generated
      `PLAN_CURRENCY`. But the catalogue carries a single currency
      (`plans.generated.ts` → `PLAN_CURRENCY = 'USD'`), so the page can
      only ever show USD. *Corrected 2026-10-07.*
      **Durable fix:** a product call on whether to localise pricing at all, then
      per-currency prices in `plan_catalog.py` (the page already renders
      whatever currency it is handed).
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

- [ ] **(c) The GitHub deploy role can deploy the VM, but not the ECS pipeline.** The estate
      account bootstrap created `feohledger-deploy` in the FeohLedger account,
      trusted only from this repo's `production` environment, and attached no
      policy — on purpose. *Corrected 2026-10-07:* `infra/` now defines a
      single-VM workload (`compute.tf`'s EC2 instance + `database.tf`'s RDS,
      `docs/minimal-deployment.md`), while `aws-deploy.yml` still targets
      ECS/Fargate — so the first call is which deploy target the pipeline
      serves, and the policy is scoped to that. *Updated 2026-10-18:* the VM half
      is done — `infra/deploy.tf` grants the role SSM Run Command of one
      document on the app VM, and `vm-deploy.yml` deploys on a release
      (`docs/decisions.md` §275). *Updated 2026-10-19:* the SPA half is done
      too — `infra/frontend.tf` grants the S3 publish and CloudFront
      invalidation on the one bucket and distribution, and the frontend job left
      `aws-deploy.yml` (§278). What remains is the ECS half. As written, `aws-deploy.yml` needs ECR push, ECS
      register/update + `run-task`/`describe-tasks`, `iam:PassRole` on the task
      and execution roles and `lambda:UpdateFunctionCode`
      (`docs/production-deployment.md` § Credentials), and every
      one of those targets a resource `infra/` does not define yet. A policy
      written today would either be `*`-wide or name ARNs that do not exist, so
      nothing was written. **Durable fix:** an `aws_iam_role_policy` on the
      bootstrap role — looked up with `data "aws_iam_role"`, never managed from
      here — in the same change that adds the ECR repository, ECS service
      and worker Lambdas, scoped to exactly those
      ARNs. (The RDS master password is already handled — a write-only
      attribute fed at apply time, `infra/README.md` § Secrets; the
      `carlpett/sops` data source this entry used to name was dropped because
      it would put the value in state.)
      `AWS_DEPLOY_ENABLED` stays unset until both land. **Trigger:** the
      workload-stack build-out — step 1 of `docs/production-deployment.md`
      § Arming the AWS pipeline. **See also #449**, which is the same role from
      the other side: its *trust* policy named a subject GitHub no longer issues
      (this repo is on immutable subject claims). That half is fixed; what is
      left there is dispatching `AWS OIDC preflight` to prove the assume, which
      does not need this policy.

### Surfaced by moving the SPA to S3 + CloudFront (2026-10-19, issue #582, decisions §278)

- [ ] **(c) Remove the SPA from the VM once CloudFront has served it for a day
      (#582 cut-over step 4).** After `frontend_on_cloudfront = true`, the VM
      still builds and serves the SPA. `deploy/deploy.sh` runs the
      `frontend-build` service (minutes of CPU credits per deploy) and swaps
      releases under `deploy/www/` (§277), `deploy/Caddyfile` keeps the
      `(spa)` snippet and the apex block, and `deploy/add-tenant.sh` still
      writes a `tenants.caddy` host block per tenant. That is the rollback path
      for the first day, which is why it was not removed in the same change.
      After that it is cost, and a slow failure: once DNS points at CloudFront,
      Caddy cannot renew those Let's Encrypt certificates, so a rollback after
      ~90 days would land on expired ones. **Durable fix:** Caddy keeps only
      `api.<domain>`. Drop `frontend-build`, the pnpm-store volume, the
      `./www` mount and the release swap from `compose.prod.yml` and
      `deploy.sh`, and stop `add-tenant.sh` /
      `remove-tenant.sh` touching `tenants.caddy` for platform hosts. Ship the
      vanity-host block that proxies the SPA from CloudFront with
      `header_up Host <platform domain>` and keeps `/api` on the vanity `Host`
      (`docs/founder-runbooks/custom-domain-provisioning.md` § 3a). Update
      `docs/minimal-deployment.md` § Key decisions 2 and the SSM document's
      timeout comment. Rollback after this means re-deploying the old commit.
      **Trigger:** one day of clean CloudFront traffic after the DNS cutover,
      and no later than ~30 days after it.

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
      *2026-10-19 (§284):* the ERP chart sync now meets renames — it matches by
      `erp_account_id` and keeps our code, reporting the drift (`code_drift`) — and
      repairs a pre-§284 QuickBooks row that merged two accounts by unlinking it, so
      its invoices are refused until they are recoded or a person confirms which
      account the row meant (`POST /api/gl-accounts/{id}/erp-link`). Both are the string column's
      limits showing; neither needed the FK yet.
      **Trigger:** the first request to rename or merge a GL account, or any work on
      cross-entity coding.

- [ ] **(c) Breakpoints are ad hoc — the deferred half of [#432](https://github.com/Absence0760/feohledger/issues/432).**
      Round 26 closed #432's two engineering parts (the `/organization` 320px failure and a
      reflow guard widened from 5 routes to 45) and deliberately did **not** make the
      viewport-floor call, which is a product decision. Measured while there, correcting the
      issue's own premise that there are "zero width breakpoints". *Re-measured 2026-10-07:*
      **37 width-based `@media` queries across 17 distinct values** (`400, 420, 520, 600,
      620, 640, 700, 720, 760, 768, 800, 900, 960, 1100px` and `52rem, 60rem, 72rem`), and
      zero shared tokens. Container queries now exist, on the help centre only
      (`@container help-main` in `routes/help/` and `help/diagrams/Diagram.svelte`).
      **Product call first:** what viewport floor do we support, and what are the two or
      three named steps? Then: declare them once (CSS custom properties cannot be used in
      `@media`, so this is documented literals plus a stylesheet guard rejecting unlisted
      values — the `targetSizeAudit.test.ts` shape — or a preprocessor), migrate 30 call
      sites (37 now), and decide sidebar behaviour below the floor and whether `/exceptions`' 11
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

- [ ] **(c) `/organization` is still one file, and it is growing — 4,186
      lines and 145 `$state` declarations (re-measured 2026-10-07; this entry
      opened at 3,400 / 119).** The section rail fixed the *navigation* problem
      (`docs/decisions.md` §205) without touching the *organisation* one: the
      panels are conditionals around markup that still shares one `<script>`, so guard rail 10 is no better served than
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
      `frontend/tests-e2e/organization/section-nav.spec.ts` asserts the second one, so
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
      unlimited spend.** `_approved_preapproval_amount` (`api/expenses.py`)
      returns the largest approved estimate that matches the line, and the
      policy engine (`services/expense_policy.py`) compares it to
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

### Surfaced by the PR #670 money-path review (2026-10-26, decisions §323)

- [ ] **(c) A PO the ERP can never price has no documented way out.** A SYSPRO
      PO with a line discount, say, stays marked unverifiable for good, and
      `po_mismatch` has no fingerprint suppression, so the next refresh after
      a person resolves the hold (Send to ERP, approve-with-corrections)
      raises it again.
      **Durable fix:** decide the exit (a person-recorded "verified total" on
      the PO, audited, or a per-row suppression a person sets) and document it
      in `backend/docs/po-matching.md`.
      **Trigger:** the first customer report of a hold that keeps coming back,
      or the next change to the unverifiable-PO rules.
- [ ] **(c) ⚠️ CISO REVIEW — an ERP-side PO increase can still lift a hold at
      the invoice's next FeohLedger refresh.** The product owner ruled that the
      sync's own re-match is hold-only (decisions §323, amended, pending CISO
      review). Every other refresh kept its pre-§323 auto-close
      (`invoice_warnings._close_cleared_po_exceptions`). So after the ERP raises
      a total, the next refresh that passes org settings closes the
      `po_mismatch` with no named actor: an invoice save, approve-with-
      corrections (`services/review.py`), **Send to ERP / `/complete`**
      (`api/workflow.py`), re-running extraction, CSV import, a goods receipt or
      QMS sync on the PO, or the missing-PO agent's apply step. Its limits
      (past-approval, segregation, hand-recorded receipts) are about a
      different PO or FeohLedger-side evidence; a same-PO row on an approved
      invoice still closes, and none of them see who edited the PO in the ERP.
      Example: INV-9 is 1,200.00 against PO-9 at 1,000.00, held. PO-9 is raised
      to 1,200.00 in the ERP and synced; a clerk clicks Send to ERP; the hold
      closes and the next run pays 1,200.00.
      **Durable fix (if the review wants it):** one guard in
      `_close_cleared_po_exceptions`: skip a row whose `erp_report.kind` is
      `po_rematch_clears`, so only a person clears it. That covers the case the
      sync already saw; a row whose clearing total changed in the ERP between
      syncs would also need the PO to record when the sync last changed its
      total. Or accept the residual and record that in decisions §323.
      **Trigger:** the CISO / Security Analyst review §323 asks for.

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
      (built by `api/invoices._skip_reason`, plus the two §263 added — a row
      changed since the approver loaded it, which the single approve already
      codes as `invoice_stale_approval`, and "Required fields missing", which
      `/complete` codes as `invoice_required_fields_missing`), the payment-run SoD and CFO sign-off
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
- [ ] **(c) CSV import resolves vendors across entities and defaults a blank
      currency to USD.** A row can link another subsidiary's vendor, and a
      blank currency cell becomes `"USD"` against the rule that a missing
      currency stays missing. Its "records history" row error also has no
      message code, so it reads in English in every locale. **Durable fix:**
      scope vendor matching to the import's entity, refuse a blank currency
      per row, and key the row errors through the message catalogue.
      **Trigger:** the next change to CSV import.
- [ ] **(c) A custom role can't be granted invoice entry.** Entry is a role
      list by design (decisions §248). **Durable fix:** if a customer needs a
      non-clerk preparer role, add a non-sensitive permission tier the
      role-grant guards exempt, and move entry onto it. **Trigger:** the first
      customer asking for a custom preparer role.
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
- [ ] **(c) The NACHA file is always unbalanced.** `services/nacha.py` writes
      credits only, which most ODFIs expect from a corporate originator, but some
      banks require a balanced file (an offsetting debit to the originator's own
      account). **Durable fix:** an optional `settings.payments.nacha` block for
      the offset account (`offset_routing`, `offset_account`, `offset_account_type`)
      that, when present, appends one debit entry for the batch total and switches
      the batch to service class 200, with builder tests on the control totals.
      **Trigger:** a pilot customer's bank rejects or asks for a balanced file.

### Surfaced by scoping the QuickBooks Online adapter (2026-10-07)

- [ ] **(c) QuickBooks Online: bills whose tax is stated only on the header.**
      Phases 0–5 of `backend/docs/quickbooks-online-adapter.md` are built
      (decisions §269–§270, §284–§287, §295). What is left is the product call
      on several tax-exclusive lines with header-only tax, refused today as
      `tax_not_itemised` / `amount_mismatch` (open question 2 of that doc,
      alongside non-US `TxnTaxDetail`). **Durable fix:** decide whether to
      apportion header tax across lines or keep refusing, then build it in the
      shared `bill_lines` rules so every adapter follows. **Trigger:** the first
      pilot customer on QuickBooks whose bills carry header-only tax.
- [ ] **(c) A NACHA file's bills are read once, at export.** The dispatch-time
      QuickBooks check (decisions §296) reads each bill when the file is built,
      but the money moves when a person uploads it to the bank, hours or days
      later; a bill paid in QuickBooks in between is paid twice. The export also
      reads its bills one after another while holding the run and every invoice
      lock (about a second each, so a large file holds them for minutes).
      **Durable fix:** read the file's bills concurrently under a bounded
      semaphore once the locks are taken, and offer a "verify before upload"
      step (re-read the run's bills; void and regenerate the file when any is no
      longer open). **Trigger:** the first record-only tenant on QuickBooks with
      status sync on, or a NACHA run over ~50 payments.

### Surfaced by moving the database onto RDS (2026-10-07, docs/minimal-deployment.md § Database)

- [ ] **(c) The DPA does not name RDS's own backups.** `/legal/sub-processors`
      § 2 and `docs/sub-processors.md` § 20 now carry the RDS row (2026-10-07,
      § 10 change-log row; no customers yet, so no notice was owed). What is
      left is the recovery wording, which undersells rather than misstates:
      `/legal/dpa` Annex II *Availability and restoration* and
      `docs/backup-disaster-recovery.md` § Targets describe nightly dumps and a
      24-hour RPO (still true, and still the published floor), and DPA § 13
      covers RDS's 7-day automated backups only through its conditional
      "volume-level snapshot … coarse fallback" sentence, without naming them
      or their retention. **Durable fix:** a deliberate call on the published
      RPO/RTO for the RDS path (counsel / the #428 review), then DPA Annex II
      and § 13 name RDS automated backups and their retention as a residue that
      ages out, and `remove-tenant.sh`'s confirmation follows if the wording
      moves. **Trigger:** the #428 counsel review, or before the first customer
      signs the DPA, whichever comes first.
- [ ] **(c) `pg_isready` over the socket reports the initdb server as ready, in
      dev and CI.** On a fresh data volume the Postgres image runs a temporary
      socket-only server for its init scripts; a `pg_isready -U postgres`
      healthcheck (no `-h`) answers "ready" against it, the container goes
      healthy, and the first client then meets "the database system is
      shutting down". Reproduced against `deploy/compose.prod.yml`, which now
      checks over TCP (`-h 127.0.0.1`). `backend/docker-compose.yml`'s
      `postgres` (and `authentik-postgres`) and the five `--health-cmd` lines
      in `.github/workflows/ci.yml` / `sso-e2e.yml` keep the socket form, where
      it can surface as a first-boot migration or seed failure that reads as a
      flake. **Durable fix:** the same `-h 127.0.0.1` on each, in one change.
      **Trigger:** the next change to either compose file or CI's service
      containers, or the first unexplained "shutting down" on a fresh volume.

### Surfaced by enforcing the plan feature gates (2026-10-07, decisions §258)

- [ ] **(c) The Scale plan sells an audit-log SIEM export that does not exist
      per tenant.** `plan_catalog.FEATURE_AUDIT_SIEM_EXPORT` is granted by
      `scale` and listed on the pricing page, but there is nothing to gate: the
      only audit shipping is the platform shipper
      (`services/audit_log_shipper.py`), configured by the OPERATOR through
      `FEOH_AUDIT_SHIPPING_PROVIDERS` and shipping every tenant's trail to the
      operator's WORM sinks. A tenant cannot point its own trail at its own SIEM.
      The SOX auditor export (`GET /api/audit/export`) is a different, ungated
      surface and stays that way. **Durable fix:** a per-tenant destination in
      `Organization.settings` (one audited writer, like `PUT
      /api/organization/sso`) — e.g. an HTTPS/HEC or syslog-over-TLS target with
      a write-only token, SSRF-guarded like webhook targets — shipped by a
      per-tenant leg of the shipper that reuses its poison-row isolation, with
      `require_entitlement(FEATURE_AUDIT_SIEM_EXPORT)` on the config writer and
      the leg skipped for an org whose plan lacks it. **Trigger:** the first
      Scale customer who asks for it, or before the pricing page next lists it
      — whichever is first; until then the listing should be qualified or
      dropped (owned by the pricing-page work, issue #426).

### Receiving, ERP depth and commerce sync (2026-10-07, roadmap Priority 14, decisions §260)

Three separate PRs, in this order. Inventory tracking and selling are out of
scope (§260) — do not fold them into any of these.

- [ ] **(c) Mobile has no goods-receipt entry.** `/goods-receipts` → Record
      receipt is web-only; mobile reads receipts only for the inspection
      picker. A warehouse or shop-floor user would naturally record a delivery
      on a phone. **Durable fix:** a receipts screen with the same PO picker,
      per-line outstanding quantities and the endpoints the web form uses (no
      backend change), quantities sent as strings, widget tests and ARB strings
      in every locale. **Trigger:** the first tenant receiving goods without a
      desk, or the next mobile procurement change.
- [ ] **(c) No adapter pulls goods receipts, and the enterprise ERPs still
      ride Merge.dev.** The US + South Africa set is now direct (QuickBooks
      Online, Xero, Sage Accounting and its SA API, Sage Intacct, SYSPRO,
      NetSuite, Business Central, Blackbaud; decisions §269). **Durable fix:**
      add a receipt pull to the `erp_adapters` interface (Business Central and
      NetSuite first), then one direct adapter per PR for SAP S/4HANA, Oracle
      Fusion, Dynamics 365 F&O, Infor and Epicor, each with fake-erp routes and
      an e2e spec. **Trigger:** the first customer on one of those ERPs who
      won't use Merge.dev, or the first customer who receives goods in their
      ERP rather than in FeohLedger.
- [ ] **(c) The Cin7 Core push is read-then-set, its retry check pages the
      whole adjustment list, and it refuses batch / bin stock.** Cin7 Core v2
      has no delta write outside its purchase flow. The adapter reads on-hand
      and posts a completed stock adjustment to `on_hand + delta`, so a sale
      that Cin7 records between the read and the write is overwritten.
      `/stockadjustmentList` cannot search by reference, so every retry
      (`attempt > 1`) pages the list, and refuses past 25,000 adjustments. A
      batch- or serial-tracked product, or stock kept in bins, is refused
      because the receipt names neither (`backend/docs/commerce-integration.md`
      § Cin7 Core). **Durable fix:** move to a delta write and a reference
      search as soon as Cin7 Core's API offers either. Until then, carry the
      batch / bin on the PO line (an optional `commerce_batch` / bin per
      line), add it to the adjustment, and sum availability per batch.
      **Trigger:** a Cin7 tenant reporting a drifted on-hand count, the scan
      cap being hit, or the first Cin7 tenant that receives batch-tracked or
      binned stock.
- [ ] **(c) Cin7 Omni has no adapter.** The `cin7` provider targets Cin7 Core.
      Omni is the older, separate product, with its own API at `api.cin7.com`
      (basic auth, `Adjustments` with a `where` filter). **Durable fix:** a
      separate `cin7_omni` module, following § How to add a provider. Omni
      can filter adjustments by reference, so its retry check would not need
      the full-list scan. **Trigger:** the first customer still on Cin7 Omni.
- [ ] **(c) A WooCommerce stock push can overwrite a concurrent storefront
      sale.** The REST API has no relative stock change and no compare-and-set,
      so the adapter reads `stock_quantity`, adds the receipt's delta and writes
      the absolute result; a sale landing between the read and the write is
      lost from the shelf count (`backend/docs/commerce-integration.md`
      § WooCommerce, the decision note). **Durable fix:** a small companion
      WordPress plugin exposing a REST route that calls
      `wc_update_product_stock($product, $n, 'increase'|'decrease')` — an
      atomic SQL increment — with the `_feoh_inventory_syncs` idempotency check
      beside it; the adapter uses that route when the plugin answers and keeps
      today's read → write path otherwise. **Trigger:** the first WooCommerce
      merchant who reports a shelf count drifting after receipts, or one whose
      order volume makes a sale inside a two-request window routine.
- [ ] **(c) Lightspeed Retail R-Series cannot be connected.** The `lightspeed`
      adapter targets X-Series (the former Vend), Lightspeed's current global
      retail POS. R-Series (the former Lightspeed Retail / "Onsite" line, still
      sold mainly in North America) is a different API on a different host
      (`api.lightspeedapp.com/API/V3/Account/{accountID}`), with integer ids,
      its own inventory model and **OAuth-only** access — there is no personal
      token to paste. **Durable fix:** once commerce OAuth exists (the entry
      below), a separate `lightspeed_r` module whose push writes a received
      purchase order (or an inventory count with a reason) rather than
      overwriting `ItemShop.qoh`, with its own fake-commerce module and contract
      test. **Trigger:** the first customer on R-Series, or commerce OAuth
      landing.
- [ ] **(c) A commerce platform that connects only by OAuth consent cannot be
      connected.** The catalogue's `auth: "oauth"` renders a Connect button
      wired to `GET /api/commerce/oauth/{provider}/authorize`, which answers 501
      `commerce_oauth_not_available`: `services/erp_oauth` (state, callback,
      refresh lock, compare-and-swap) reads and writes `settings.erp` only.
      None of the five planned platforms needs it — each has a key-based
      connection. **Durable fix:** parameterise `erp_oauth` by settings block
      (`erp` / `commerce`) and register commerce `OAuthProviderSpec`s, keeping
      one callback route. **Trigger:** the first commerce provider shipped
      OAuth-only, or a public Shopify / Square app listing (which requires
      OAuth install).

### Surfaced by the US + South Africa ERP connections (2026-10-08, decisions §269–§272)

- [ ] **(c) A VAT-registered Business Central company cannot post at all.** The
      adapter reads back `totalAmountIncludingTax` and, when BC adds VAT on top
      of the approved gross, deletes the draft and refuses
      (`posted_total_mismatch`). That is safe, but such a company is blocked.
      **Durable fix:** send net line amounts plus BC tax codes, so BC's computed
      total equals the approved gross. **Trigger:** the first customer on a
      VAT-registered BC company.
- [ ] **(c) A Business Central user can edit a draft between our total check
      and our post.** The adapter reads the draft's `totalAmountIncludingTax`
      and then calls `Microsoft.NAV.post`; an edit in that window posts a total
      nobody approved, and the success path does not read the posted invoice
      back. **Durable fix:** after the post, read the posted invoice's total and
      treat a mismatch as `posted_total_mismatch` (an accountant must reverse a
      posted invoice in BC; we never delete one). **Trigger:** the first BC
      customer whose staff edit purchase drafts.
- [ ] **(c) Business Central approval workflows are not handled.** A post step
      that fails after the draft is created is now a retryable failure, and a
      retry finishes the draft. But a BC company with purchase-approval
      workflows leaves the invoice "In Review", which the adapter refuses
      (`existing_invoice_not_open`). **Durable fix:** decide whether we submit
      for approval and poll, or post only to companies without workflows, and
      document it. **Trigger:** the first BC customer with approval workflows.
- [ ] **(c) Connecting picks no company when the consent covers several.** Xero
      (several organisations authorised in one consent) and Sage v3.1 (a login
      with several businesses) resolve to none, and the connect is refused
      (`no_external_tenant`). **Durable fix:** a company picker after consent,
      stored with the grant. **Trigger:** the first customer whose consent
      covers more than one company.
- [ ] **(c) A vendor holds one `erp_vendor_id`, whichever ERP set it.** A tenant
      that switches ERP posts the old ERP's ids, and the new ERP rejects them,
      and some syncs stored a name or code where the ERP gave no id. **Durable
      fix:** key the id by ERP (a `vendor_erp_links` table, or `{erp_type: id}`)
      and resolve by the active ERP. **Trigger:** the first tenant that
      switches ERP.
- [ ] **(c) A bill paid in any ERP but QuickBooks never reaches FeohLedger.**
      Status sync-back (§295) covers adapters with `supports_status_sync`,
      which today means QuickBooks only. For every other ERP a push still closes
      at `done`, and `done` is terminal, so the ERP's later payment cannot be
      recorded. The webhook's `Paid` and "record payment made outside" both
      skip a `done` invoice. This is what the live Xero Demo Company test hit:
      FEOH-INV-001 was paid in Xero and still reads `done`. The adapter
      reads are now honest about paid versus credited (§303).
      **Durable fix:** extend §295 per adapter: implement `bill_report` (and
      `changed_bills` or a keyset poll where the ERP has no change feed) plus
      `post_payment`, so `supports_status_sync` can be true and the push lands
      at `posted_in_erp` under §296's dispatch-time read. Xero first. A design
      that instead rests a pushed bill at the non-payable `sent_to_erp` was
      built and is kept on the `feat/erp-status-sync` branch. It contradicts
      §295's "either place may pay" decision and needs a product call before
      reuse. **Trigger:** the first Xero tenant, or the live Xero re-test.
- [ ] **(c) A bill already posted to the ERP can't be voided or recalled from
      FeohLedger.** `void_invoice` has no caller under `app/` beyond
      `posted_total`'s cleanup of a mismatched bill, and no user action voids
      or recalls a pushed bill. **Durable fix:** a `POST /api/invoices/{id}/
      void-erp` (the push's roles) that calls `void_invoice` and refuses a bill
      the ERP shows paid or part-paid. On success it walks the invoice through
      an audited edge back to `approved`, so it can be corrected and re-pushed
      or paid. **Trigger:** the first customer who needs to recall a pushed
      bill, or the live void test now pending.
- [ ] **(c) Business Central's vendor sync stores `paymentTermsId` (a GUID) as
      the payment-terms text, and NetSuite's PO sync pulls no lines.**
      **Durable fix:** resolve BC's `paymentTerms` code; pull NetSuite PO lines
      through SuiteQL. **Trigger:** the first BC customer relying on synced
      terms, or the PO sync starting to store lines.
- [ ] **(c) The OAuth adapters have no Playwright spec.** The ERP e2e suite
      covers Merge.dev, NetSuite and Business Central; the setup panel's spec
      covers the connect UI with a mocked status. **Durable fix:** fake-erp
      OAuth stubs for Xero, Sage and Blackbaud (QuickBooks has one), plus a
      connect → push spec for each under `tests-e2e/erp/`. **Trigger:** the
      next change to `services/erp_oauth`.
- [ ] **(c) Deleting a tenant does not revoke its ERP OAuth grant.**
      `services/tenant_deletion` deletes the sealed `provider_credentials` row,
      but the refresh token stays valid at Intuit / Xero / Sage / Blackbaud
      until it expires (up to five years for QuickBooks). **Durable fix:**
      before the control-plane sweep, open the sealed tokens and call
      `erp_oauth.revoke` best-effort, recording the outcome in the deletion
      report. **Trigger:** the next change to `tenant_deletion`, or before the
      first tenant with a live OAuth ERP is deleted.
### Surfaced by the cards / receiving-SoD / provider-credentials batch (2026-10-08, decisions §264–§268)

- [ ] **(c) Approving first and recording the receipt second still works.**
      §267 refuses a receiver at approval, but the reverse order is open: a
      manager approves the invoice, then records the goods receipt that lifts
      its "billed beyond receipt" hold. `receipts_clear_hold` and the agent
      coordinator's `_receipts_implicated` cannot refuse the approver because
      nothing records who approved by id — only the `approved_by` display name,
      and the audit log, which fails open in lambda mode.
      **Durable fix:** a tenant migration adding `invoices.approved_by_user_id`
      (NULL for history — no honest backfill, §141's reasoning), stamped in
      `review.approve_invoice`; `receipts_clear_hold` and
      `_receipts_implicated` refuse a recorder who approved the invoice, with
      tests on both doors. **Trigger:** the next change to receipt entry or the
      approval chain, or the first tenant running three-way matching with more
      than one approver.
- [ ] **(c) A multi-PO split's `po_ids` vanish on the next warning refresh, and
      the receiver check with them.** `approval_chain.receipt_recorders` reads
      the split's PO ids from `po_match.po_ids`, but `refresh_warnings`
      rewrites `po_match` and the combined `po_number` resolves to no PO, so a
      receiver of one leg of a split can approve after any later edit.
      **Durable fix:** persist the split's PO links outside the recomputed
      `po_match` (a link table or a column the matcher only appends to) and
      read the receiver set from it. **Trigger:** the next PO-matching change
      touching multi-PO splits, or the first customer splitting invoices across
      POs.
- [ ] **(c) Extraction BYOK keys are still plain JSONB.**
      `settings.extraction.api_key` / `aws_secret_access_key` were not in §266's
      scope and are admin-readable. **Durable fix:** add an `extraction` block
      to `services/provider_credentials.SECRET_FIELDS`, move its readers onto
      `provider_config`, and seal existing values in a control-plane
      migration, as 0110 did. **Trigger:** the next extraction-adapter or BYOK
      change, or before the first BYOK extraction customer.
- [ ] **(c) `sso.client_secret` is plain JSONB.** It is write-only
      (`org_settings_view.ALWAYS_REDACTED`) but not sealed;
      `docs/secrets-rotation.md` used to claim it was encrypted and is
      corrected. **Durable fix:** an `sso` block in `provider_credentials`,
      read by `services/sso` through `provider_config`, written only by
      `PUT /api/organization/sso`, with a migration sealing existing values.
      **Trigger:** the next change to `api/organization_sso.py`, or before the
      first SSO customer in production.
- [ ] **(c) Switching payment or card provider keeps the old provider's sealed
      secret.** Changing `payments.provider` (or `cards.provider`) leaves the
      previous provider's stored key in place, and the next call sends it to
      the new provider's host. The change is audited
      (`organization.provider_config_updated`) but nothing clears it. The ERP
      block no longer has this gap: a new ERP or a new destination drops its
      sealed secrets in the same transaction (decisions §271,
      `erp_adapters/catalog.secrets_to_drop`). **Durable fix:** the same shape
      for `payments` / `cards` — a pure "what does this save drop" rule keyed on
      the provider identity (and any base-URL / environment key), applied by
      `PATCH /api/organization` through `provider_credentials.update_secrets`
      with an audited `credentials_updated` row, plus a test. **Trigger:** the
      next change to the payments or cards settings writers, or before the
      first tenant with live payment-rail or card-issuer credentials.
- [ ] **(c) A sealed value that can no longer be opened has no in-app
      recovery.** If the KMS key is disabled or (in dev) `FEOH_SECRET_KEY`
      changes, every save for that block returns 503, because the writer opens
      the existing value to merge into it. **Durable fix:** an admin "clear
      unreadable credentials" action on `/api/organization/credentials/{block}`,
      audited, that replaces the row without opening it. **Trigger:** the first
      key rotation or environment where it happens.

### Surfaced by evaluating Baidu's Unlimited OCR (2026-10-11)

- [ ] **(c) No self-hosted extraction path is benchmarked for scanned invoices.**
      The only local option for a scan with no text layer is a general vision
      model through the `ollama` adapter, and nothing measures how it compares
      with `claude_vision` / `aws_textract`. Baidu's Unlimited OCR (MIT, 3B
      params / ~0.5B active, arXiv 2606.23050, June 2026) is a candidate OCR
      stage: it returns text and layout, not fields, so it would run in front of
      the existing Ollama text-mode prompt rather than replace an adapter. It is
      **not** worth building an adapter on spec: digital PDFs already skip OCR
      (PyMuPDF text mode), invoices are 1–3 pages so its long-document strength
      mostly goes unused, a two-step read has no honest per-field confidence for
      the 0.95 auto-approve gate, it likely needs a GPU plus vLLM / Transformers
      rather than Ollama (a new local service under guard rail 7), and its
      provenance needs a CISO / Security Analyst call before it touches
      customer data. **Durable fix:** a bounded benchmark first — ~30 scanned
      or photographed invoices plus a few multi-page vendor statements
      (`statement_extraction.py`), comparing Unlimited OCR → Ollama text model
      against `ollama` vision, `claude_vision` and `aws_textract` on per-field
      accuracy and latency — and only if it clearly beats the current local
      path on scans, an OCR pre-stage for the `ollama` adapter with a
      confidence story, a Compose profile and `pnpm` script, and
      `docs/sub-processors.md` unchanged (self-hosted). **Trigger:** a customer
      or prospect asks for on-prem / no-third-party extraction of scanned
      invoices, or the next change to the `ollama` adapter's image mode.

### Surfaced by the first production deploy (2026-10-18, #566, decisions §274)

- [ ] **(c) The VM release workflow never smoke-checks the API through public
      DNS.** The day-one gaps the first production deploy found are closed
      ([#575](https://github.com/Absence0760/feohledger/issues/575): the no-seed
      day-one CI job, production-shaped fixtures, single-owner rules with
      shown-equals-allowed parity tests, Compose parity, script exit-code tests).
      One piece is still open. `vm-deploy.yml` runs `deploy/smoke.sh --vm` on the
      box, which goes through the VM's own Caddy, and checks the SPA hosts from the
      runner with `smoke-frontend.sh`. Nothing outside the VM asks
      `https://api.feohledger.com/api/health` or `public-config`. A DNS, Elastic IP
      or security-group fault on `api.` would therefore pass the release and show
      up only as a broken site. **Durable fix:** after the publish step, run
      `deploy/smoke.sh --app … --api … --tenant … --signup …` from the runner,
      the same way `aws-deploy.yml`'s `smoke` job does, with the values taken
      from the workflow's existing repository variables.
      **Trigger:** the next change to `vm-deploy.yml`.

### Surfaced by giving backend rules a single owner (2026-10-20, issue #575 §3)

- [ ] **(c) Three readers join every `PaymentSchedule` row, not the invoice's
      current one.** The payment queue page (`api/payments` — `outerjoin
      (PaymentSchedule, …)` in the queue listing), its early-pay savings
      aggregate, and the dashboard's discount-capture tile all join the table
      directly, so an invoice carrying two schedule rows is listed twice in the
      queue and its discount counted twice. The cash forecast, discount sweep
      and ROI already take the latest row through
      `services/payment_schedules.LATEST_SCHEDULE_ORDER`. Latent: nothing but
      `scripts/seed.py` writes a schedule today, and the dashboard comment
      treats "two schedules on different terms" as two rows on purpose, so
      whether more than one schedule per invoice is meaningful is a product
      call first. **Durable fix:** decide that one schedule per invoice is the
      model (a unique index on `payment_schedules.invoice_id` plus a migration
      that keeps the latest row), or route all three readers through a
      `DISTINCT ON` subquery built from `LATEST_SCHEDULE_ORDER`.
      **Trigger:** the first code path other than the seed that creates a
      `PaymentSchedule`.
- [ ] **(c) An invoice cannot say "currency unknown", so a missing code reads
      as `USD`.** `Invoice.currency` is `Mapped[str]` (NOT NULL, default
      `"USD"`), and `POST /api/workflow/upload` creates the row with
      `currency="USD"` before anything has read the document. So an extraction
      whose model returns no currency field, and no code beside the amounts,
      leaves a non-US invoice labelled USD, which is the default decisions
      §160/§196/§200 forbid. Since §308 the printed code fills the gap when the
      page has one; this entry is the case where it doesn't. Two readers then
      stamp the same default again: `invoice_warnings` scopes the
      statistical-anomaly history with `Invoice.currency == (invoice.currency
      or "USD")`, and `extraction.resolve_gate_aggregate` passes
      `currency=… or "USD"` to the structuring sum. Each `or "USD"` is
      unreachable for a flushed row (the column is NOT NULL) and only mirrors
      the column default, so removing them alone changes nothing. **Durable
      fix:** make `invoices.currency` nullable (tenant migration; no backfill,
      because no honest value exists for the rows already stamped), have the
      upload create the row with `NULL`, and have every reader of an unknown
      currency fail closed. That means the anomaly and structuring lookups
      compare nothing and the approval gates read `GateAmount(expressible=False)`,
      which `reporting_gate_amount` already supports. Schemas, the web and
      mobile types, ERP push and payment-run building must then refuse or
      label a NULL currency, which is why this is sized work rather than a
      one-line fix. **Trigger:** the next change to the upload's invoice
      construction, or the first tenant reporting a non-USD invoice extracted
      as USD.

### Surfaced by replacing signup's temporary password (2026-10-19, decisions §283)

- [ ] **(c) Five other flows still hand out a temporary password.** Signup now
      sends a single-use set-password link; these still generate a password
      with `utils/passwords.generate_temp_password` and email it, return it, or
      both, leaving a working credential in an inbox or on an admin's screen
      until someone signs in: (1) admin create user —
      `POST /api/admin/users` (`api/admin.py::create_user`, emailed and
      returned as `temporary_password`); (2) admin password reset —
      `POST /api/admin/users/{id}/reset-password` (`api/admin.py`, decisions
      §276, emailed and returned once); (3) supplier-portal invite —
      `POST /api/vendors/{id}/portal-users` (`api/vendors.py::invite_vendor_portal_user`, emailed); (4)
      supplier-portal password reset — `POST /api/vendors/{id}/portal-users/{vu}/reset-password`
      (emailed); (5) partner child provisioning — `POST /api/partner/children/
      provision` (`api/partner.py`, returned to the partner admin as
      `temp_password`). **Durable fix:** the same shape as §283 — provision or
      reset to no usable password, mint a set-password link and email that.
      (1), (2) and (5) reuse `services/password_reset` and
      `tenant_urls.password_reset_url(..., set_password=True)` unchanged
      (control-plane `User`s; (5) emails the child's admin rather than
      returning a secret to the partner). (3) and (4) need a portal
      equivalent first: `VendorUser` lives in the tenant DB and the portal has
      no forgot-password flow at all, so the token must carry the tenant slug
      and redeem through a new portal reset page — which also gives suppliers
      the self-service recovery they lack. §276's "never a value the admin
      types" stands; the link replaces the temp, not that rule. **Trigger:**
      the next change to any of those five endpoints or to their emails, and
      before a second customer onboards users or suppliers.

### Surfaced by refusing the ERP push for an open duplicate or fraud flag (2026-10-19, decisions §286)

- [ ] **(c) A duplicate or fraud flag raised after the bill is already in the
      ERP leaves that bill payable there.** §286 refuses the push while an
      `ERP_EXPORT_BLOCKING_EXCEPTION_TYPES` row is open, but a flag can arrive
      later: a Positive Pay return, a fraud rule re-run on an edit, a duplicate
      found when a third copy is entered. Our payment run still refuses the
      invoice; the ERP's own Bill Pay does not know. Nothing tells the operator
      the posted bill should be voided. **Durable fix:** when one of those types
      opens on an invoice holding an ERP reference (`sent_to_erp` onward), raise
      a notification and offer the existing ERP void
      (`ErpAdapter.void_invoice`, `backend/docs/erp-integration.md`) from
      the exception, gated and audited like a payment void; never void
      automatically, since the flag may be false. **Trigger:** the first report
      of a flagged bill paid from the ERP, or the next change to the ERP void
      path.

      The bill can also already be in the ERP when the push was refused: an
      attempt that timed out or got a 5xx may have created it, and the gate's
      refusal of the retry now says `possibly_posted: true` (decisions §286),
      but nothing finds or voids that bill. **Durable fix:** from the exception
      or the failed invoice, run the adapter's read-only idempotency lookup to
      name the orphan, and offer the existing ERP void. (The other half of
      this entry landed 2026-10-20: `ErpPostResult.not_posted` now stops a
      refusal that sent nothing from reading as `possibly_posted` — every
      adapter's pre-network refusals via `base.erp_preflight_refusal`, and
      QuickBooks up to its bill create.) **Trigger:** a bill found in an ERP
      for an invoice we show as failed, or the next change to the ERP void
      path.
- [ ] **(c) A human-cleared finding of any other type is re-opened by the next
      refresh.** §286 stops `refresh_warnings` re-opening a `duplicate` or
      `fraud_flag` a person resolved or dismissed while the invoice is
      unchanged (`exceptions.finding_fingerprint`, migration 0116). The other
      types `_ensure_exception` raises still re-open, including the
      payment-blocking `line_total_mismatch`, so a reviewer who accepts a line
      variance sees the hold come back on the next refresh. **Durable fix:**
      stamp the fingerprint for every type `_ensure_exception` raises, with
      each type's own input fields (line items for the line-total check, the
      PO and receipts for PO matching); `record_decision` already clears it on
      a decision no person made. **Trigger:** the next change to
      `_ensure_exception` or to the line-total reconciliation, or a reviewer
      report of a hold that keeps coming back.
### Surfaced by reviewing the QuickBooks live-test fixes (2026-10-19, decisions §284–§287)

- [ ] **(c) A chart or vendor listing over `SYNC_LISTING_MAX_ROWS` cannot sync
      at all.** Since decisions §287 a listing past the bound (10,000 rows;
      QuickBooks 50,000) raises `ErpListingIncomplete` and the sync endpoints
      answer 502, rather than syncing a partial pull that would unlink every
      link the missing pages held. A tenant that large gets no sync.
      **Durable fix:** a resumable, streamed sync that walks the listing in
      pages, records completeness per run, and applies the absence-based rules
      (dead links, uniqueness counts) only once a run has read the whole
      collection. **Trigger:** the first tenant over the bound.
- [ ] **(c) A collapsed QuickBooks row whose link landed on the right-sounding
      account keeps it.** decisions §284's accepted residual: a pre-§284 row
      that merged several accounts under one bare `Name`, and whose link landed
      on the account whose current code equals that name (a top-level account
      named like the sub-accounts, e.g. "Depreciation" over
      "Truck:Depreciation"), looks exactly like a correct row, so rule 4 keeps
      its link and invoices meant for a sub-account keep posting to the
      top-level one. **Durable fix:** when a row's code is the legacy key of
      more than one pulled account, keep its link but record the other
      accounts as `erp_link_candidates` and badge the row "Needs a decision"
      until a person confirms (`POST /api/gl-accounts/{id}/erp-link`), rather
      than trusting it silently. **Trigger:** the next change to rule 4 in
      `services/gl_account_sync`, or a report of a bill posted to a parent
      account that meant a sub-account.
- [ ] **(c) An ERP `name` over 255 characters still aborts a sync.** decisions
      §287 drops an over-long optional vendor field and an over-long GL
      `parent_code` instead of failing the flush, but `vendors.name` and
      `gl_accounts.name` are `String(255)` and are written unchecked.
      QuickBooks `DisplayName` allows 500; the QuickBooks GL adapter truncates
      the account name to 255 itself, the vendor mapping does not. **Durable
      fix:** a per-record reason code in the shared syncs (refuse the record,
      or store a marked shortened display name, decided once for both), with
      a frontend message, and the adapters' own truncation removed.
      **Trigger:** the first sync that fails on a long name, or the next
      change to `services/vendor_sync` / `gl_account_sync`.
- [ ] **(c) The vendor ambiguous-name rule compares names exactly.**
      `services/vendor_sync` counts names across the pull and the unlinked
      locals with `==`, while the realm evidence (`_same_vendor`) ignores case
      and spacing. So "Acme" and "ACME" in one pull do not trip the
      `ambiguous_name` guard: a manual "Acme" is linked to the record with its
      exact spelling and "ACME" is created as a second vendor, although the
      rule the realm pass applies would call them one supplier. **Durable fix:** count on the same normalised key
      `_identity_keys` builds, in both the pull and the local query.
      **Trigger:** the next change to `vendor_sync`'s name fallback.
- [ ] **(c) Two adapters still drop or mislabel records the syncs read as
      evidence.** (1) Sage Accounting (UK) skips ledgers with
      `included_in_chart: false`, so a row linked to one reads as a dead link.
      (2) Business Central's vendor `blocked` is not mapped to `active=False`,
      so a blocked vendor can be linked or created as a payee. **Durable
      fix:** emit the out-of-chart ledger with `active=False` and map
      `blocked`. **Trigger:** the next change to either adapter's listings.
      (Merge.dev's half — account `status` and the GL parent id — landed
      2026-10-26 with the Contacts vendor fix.)
- [ ] **(c) Vendor linking has three gaps of its own.** (1)
      `vendors.erp_vendor_id` has no unique index, so two rows sharing an id
      make `scalar_one_or_none` in `services/vendor_sync` raise and abort the
      sync. (2) CSV import resolves vendors by `Vendor.code`
      (`services/csv_import`), which an ERP-written code can collide with.
      (3) Sage Intacct, SYSPRO and Blackbaud FE NXT link GL accounts by the
      business account number, not a surrogate key, so a renumbering in the
      ERP follows the number. **Durable fix:** a partial unique index on
      `(erp_link_realm, erp_vendor_id)` with a migration that reports existing
      duplicates first; CSV import resolving by a code namespace that ERP
      codes cannot enter; and each of the three adapters keyed by the ERP's
      own record id where its API exposes one. **Trigger:** the next migration
      touching `vendors`, the next change to `csv_import`'s vendor resolution,
      or the next change to one of those three adapters' chart listings.
- [ ] **(c) A mock sync still treats pre-0115 links as its own.** A no-realm
      pull (`mock`, or an OAuth ERP with no consent) leaves rows tied to a
      named realm alone (decisions §287), but a pre-0115 link is NULL-realm,
      indistinguishable from a link the mock itself made, so a mock sync still
      re-judges it against fixtures. **Durable fix:** stamp the links a no-realm
      sync makes with an explicit sentinel realm (e.g. `"mock"`), so NULL means
      only pre-0115 and a no-realm pull can leave it alone too. **Trigger:**
      the next change to `services/erp_link_realm`, or a tenant that switches
      to `mock` after 0115.

### Surfaced by building the light theme (2026-10-20, decisions §289)

- [ ] **(c) A white-label accent is one colour for both themes, and nothing
      checks it as text.** A tenant's `accent_color` / `accent_strong_color`
      is written inline on `<html>` (`stores/brand.svelte.ts`), so it beats
      both palettes. `--accent-strong` is held to white-on-it by the existing
      `/organization` advisory, which is theme-independent and still right; but
      `--accent` is also used as small TEXT (links, the active tab, armed row
      actions), and a brand blue picked to read on navy — the platform's own
      `#638cff` is 3.1:1 on white — fails 1.4.3 once a reader chooses Light.
      Nothing measured `--accent` as text even in dark. **Durable fix:** let a
      brand carry a per-theme `--accent` (or derive the light one by darkening
      until it clears 4.5:1 on `--surface`), and extend the advisory in
      `stores/brandTheme.ts` to report the accent against both themes'
      surfaces. **Trigger:** the first tenant that sets a brand accent, or any
      white-label sale.

### Surfaced by the cross-workspace identity fixes (2026-10-20, decisions §291)

- [ ] **(c) Multi-workspace membership: one person cannot belong to two
      workspaces.** `users.email` is globally unique and `User.organization_id`
      is one column, so an auditor, a bookkeeper serving several clients or a
      contractor in two customers' IdPs needs a separate address per workspace.
      §291 made every collision a stated, coded refusal (admin create/rename,
      SCIM, SSO/SAML JIT) and budgeted the admin path's existence signal, but
      the signal itself remains for as long as the address is global, and the
      person's only way forward is another address. **Durable fix:** split
      identity from membership — a `memberships(user_id, organization_id,
      roles, is_active)` table, login resolving the account by address and then
      the membership by the page's workspace (which also retires the
      `wrong_workspace` refusal into a workspace picker), per-membership
      sessions/JWT `org`, and admin "invite" adding a membership to an existing
      account (with the invitee's consent) instead of creating one. Large: it
      touches login, every `organization_id`-keyed user query, SCIM, JIT,
      access reviews, DSAR/erasure and the per-user audit trail. **Trigger:** a
      customer or partner asks for one person in two workspaces, or the
      partner/reseller programme onboards an accountant serving several
      tenants. Full plan, security risks and the
      cross-tenant test matrix: #614.
- [ ] **(c) The SAML ACS still answers its other refusals as raw JSON at the
      API origin.** Only the address-in-another-workspace refusal is routed to
      the SPA bridge (`sso.create_saml_refusal_handoff`); `inactive`,
      `domain_blocked`, `unsafe_email`, a replay or a bad assertion still leave
      the browser on `{"detail": "SAML login could not be verified."}` from the
      ACS URL, in English, with no way back — the OIDC callback page states its
      refusals because the SPA posts the callback itself. **Durable fix:** send
      every ACS refusal through the same refusal handoff, keeping the one
      generic sentence (and its PII-free audit reasons) for the verification
      failures so the bridge says no more than the ACS does today, and coded
      `inactive` / `domain_blocked` the OIDC path already states. **Trigger:**
      the first SAML tenant in production, or a support report of a blank JSON
      page after an IdP sign-in.

### Surfaced by fixing signup's dead ends (2026-10-20, decisions §292)

- [ ] **(c) There is no self-service "find my workspace" recovery.** A user
      who forgets which workspace their account is in has no way back without
      support: forgot-password only works on a workspace host (it needs the
      tenant to build the reset link), and the apex has no sign-in page at
      all. Signup now points an address that already has an account at its
      workspace (§292), but only after the visitor has filled the form and
      clicked a verification link — that is not a recovery path, and nobody
      should have to start a signup to find a sign-in page. **Durable fix:**
      an apex "Email me my workspace address" form — `POST
      /api/workspace-lookup {email}`, public, captcha'd and rate-limited per
      IP and per address like `/signup/start`, answering every address with
      the same "if that address has an account, we've emailed it" response,
      and emailing the account's own `tenant_urls.sign_in_url` (address
      prefilled) only when one exists — linked from the apex landing page and
      from the login page's error states. **Trigger:** the first support
      request from someone who cannot find their workspace, or before signup
      is advertised beyond the pilot.

### Surfaced by self-service workspace closure (2026-10-20, decisions §306)

- [ ] **(c) An abandoned workspace is kept forever unless someone closes it.**
      §306 lets an administrator close an unused workspace (which releases its
      addresses), and rejected an unattended sweep, because nothing in the code
      records a trial as ended: signup binds the free plan as `active` with no
      `trial_end`, and "idle" is not a customer's instruction. So a workspace
      its owner simply walks away from — personal data of its users, uploaded
      documents, a tenant database — is retained indefinitely, which a
      storage-limitation review (GDPR Art. 5(1)(e)) will ask about. **Durable
      fix:** an explicit end state, not a guess — a trial (or free-plan
      inactivity) term the Terms state, recorded on the subscription; notice
      emails to the workspace's administrators before it lapses; and only then
      a sweep that runs the same fail-closed guards as
      `services/workspace_closure` and deletes through `tenant_deletion`,
      leaving the same control-plane record. **Trigger:** billing goes live
      with a trial period, the Terms gain a free-plan inactivity clause, or
      the first storage-limitation question from a customer or auditor.
- [ ] **(c) A deployment in `lambda` audit mode cannot close a workspace
      self-service.** In that mode an audit row may still be in SQS, not yet in
      the tenant's `audit_log`, when the closure flushes the trail and drops the
      database, and nothing can see or drain one tenant's messages, so the
      closure refuses (`audit_queue_unverifiable`, decisions §306) and support
      closes it. **Durable fix:** a per-tenant drain the closure can wait on —
      e.g. the Lambda writer stamping a per-tenant high-water mark the closure
      compares with what it enqueued, or a FIFO queue with a tenant message
      group the closure can poll empty. **Trigger:** the first deployment that
      runs `FEOH_AUDIT_MODE=lambda` and wants self-service closure.

### Surfaced by the live Xero Demo Company test (2026-10-20)

Linking a synced purchase order to its vendor by the ERP's supplier id (and
filling a missing link on a re-sync) landed with the test. Two gaps it exposed
stay open.

- [ ] **(c) An unapproved ERP purchase order syncs as `open` and can be
      matched.** `PurchaseOrder.status` has three values (open / closed /
      cancelled), so Xero's `DRAFT` and `SUBMITTED` and NetSuite's Pending
      Supervisor Approval (`A`) all land as `open`. In the Demo Company, a
      Draft PO showed as OPEN. An invoice can then pass 2/3-way match against
      an order nobody in the ERP has approved. **Durable fix:** decide between
      skipping not-yet-issued POs in each `list_pos` (they arrive once
      approved, and the existing upsert refreshes the status) and adding an
      unissued status that `po_matching` refuses. Both change the PO
      vocabulary every adapter maps onto, so record the choice in
      decisions.md. **Trigger:** before the first tenant matches invoices
      against live Xero or NetSuite POs, or the next change to the PO status
      vocabulary.

### Surfaced by Xero's Disconnect leaving the grant live (2026-10-20)

Xero's adapter had no revoke URL, so Disconnect cleared our tokens and audited
`revoked: false` while the org stayed connected at Xero. The live Demo Company
showed it "Already connected" on the next consent. Xero now revokes; two
providers still have the same gap.

- [ ] **(c) Sage Accounting and Blackbaud FE NXT do not revoke on Disconnect.**
      Their `OAuthProviderSpec` has no `revoke_url`, so `erp_oauth.revoke`
      returns False without calling out: the refresh token stays valid at the
      provider until it expires, and the tenant still lists the app as
      connected there. **Durable fix:** set each provider's documented
      revocation endpoint (and a `*_revoke_url` override for fake-erp, with a
      fake revocation route), or record in the spec that the provider has
      none and make the Disconnect message say the grant must be removed at
      the provider. **Trigger:** the next change to either adapter's OAuth
      spec, or before either provider's platform app goes live.

### Surfaced by the Blackbaud FE NXT API audit (#651, 2026-10-26)

- [ ] **(c) A push refused by the SKY call budget is not re-sent when the
      allowance resets.** `erp_call_budget_exhausted` (`tenant_budget` /
      `subscription_quota`) fails the invoice before any call, with the reason
      and `retry_after`. Someone has to retry it after 00:00 UTC, one invoice
      or a bulk retry at a time. The original follow-up asked to *defer*
      rather than fail. **Durable fix:** a sweep that, after the reset,
      re-dispatches invoices whose last `invoice.erp_failed` refusal is
      `erp_call_budget_exhausted` and whose state is unchanged since. It goes
      through `erp_send.start_erp_send` (plan and export gates), in
      `retry_after` order, and stops at the org's share again. **Trigger:**
      the first tenant that hits its share in production, or a support request
      to retry a batch by hand.

### Surfaced by auto-sending approved invoices to the ERP (2026-10-20, decisions §304)

- [ ] **(c) An approval whose auto-send never ran is not noticed.** The push
      after an approval is post-commit work, so a process that dies between the
      approval's commit and the job (a deploy, an OOM) leaves the invoice at
      `approved` with neither `invoice.erp_submitted` nor
      `invoice.erp_auto_send_refused` on its trail. It is still payable and
      sendable by hand, so nothing is lost, but the configured automation was
      skipped silently. **Durable fix:** a sweep that finds `approved` invoices
      whose snapshot has `erp_export.auto_send_on_approval` on, approved more
      than a few minutes ago with no auto-send row of either kind since that
      approval, and runs `erp_send.send_after_approval` for them (it already
      re-locks and acts only on `approved`). **Trigger:** the first tenant that
      relies on auto-send in production, or the first report of an approved
      invoice that did not reach the ERP.

### Surfaced by storing the ERP document id on the invoice (2026-10-20, decisions §302)

- [ ] **(c) The ERP deep link opens in whichever organisation / company the
      user last had open.** `erp_adapters/deep_links.py` builds Xero's
      `go.xero.com/AccountsPayable/View.aspx?InvoiceID=` and QuickBooks'
      `app.qbo.intuit.com/app/bill?txnId=` links from the id alone, so a user
      who works in several Xero organisations or QuickBooks companies lands on
      "not found" (or the wrong company) unless the right one is already
      selected. Xero's own MCP server wraps the bill page in
      `organisationlogin/default.aspx?shortcode=<org>&redirecturl=…`, which
      needs the organisation's `ShortCode`; we never fetch it. **Durable
      fix:** read `ShortCode` from `GET /Organisation` when the Xero connection
      resolves its tenant (`XeroAdapter.resolve_external_tenant_id`), keep it
      on the ERP config, and build the shortcode redirect; find QuickBooks'
      equivalent company-selecting URL (if Intuit has one) keyed by the
      stored `realmId`. The mobile ERP panel shows the id with no link at
      all, because the app carries no `url_launcher`; add it (and parse
      `erp_document_url` into `Invoice`) in the same change. **Trigger:** a
      multi-organisation Xero or multi-company QuickBooks customer, a report
      of the link opening the wrong books, or a mobile user asking to open
      the bill.
- [ ] **(c) The ERP status webhook still ignores an event that carries only
      `erp_document_id`.** `api/erp_webhook.py` resolves the invoice by
      `correlation_id` and returns early without one ("not supported yet"). The
      bill id is now on `invoices.erp_document_id`, so the lookup has a column
      to use. **Durable fix:** with no `correlation_id`, resolve the invoice by
      `erp_document_id` (indexed, scoped to the tenant, acting only on exactly
      one match and taking the same `get_invoice_for_update` row lock), with a
      realdb test that a second invoice carrying the same id is refused rather
      than guessed. **Trigger:** wiring a provider whose webhook does not echo
      our correlation id (QuickBooks / Xero change notifications carry only
      the entity id).

### Surfaced by the UI action-verdict pass (2026-10-21, issue #575 § One rule, one place, decisions §305)

- [ ] **(c) The invoice modal still hides Approve / Reject from a reviewer the
      invoice is not assigned to, and the server accepts them.** The modal's
      `isAssignedElsewhere` clause in `InvoiceModal.svelte` is the one
      restated rule left beside the `review-actions` verdict: `POST
      /api/invoices/{id}/approve|reject` accept any holder of `invoice.approve`
      who passes the approval gates, assignment or not. So the page and the
      server disagree in the safe direction (a working control is hidden, none
      that 403s is shown). **Durable fix:** a product call — either enforce
      assignment server-side (a new gate in `review._identity_gates`, which
      `review_refusals` then reports) or drop the clause and let any qualified
      approver act. **Trigger:** the next change to reviewer assignment, or a
      reviewer reporting they cannot act on an invoice assigned to an absent
      colleague.

- [ ] **(c) Bulk status can still approve a `new` draft past review.** The
      four review-decision doors (in-app approve / reject, email, Slack,
      Teams) now refuse an invoice that is not `ready_for_review`
      (`review.awaiting_review`), but `POST /api/invoices/bulk/status` with
      `status=approved` still moves a `new` invoice straight to `approved`
      (`new → approved` is a legal transition for it), through the same approve
      gates — so segregation and the money limits hold, the review stage does
      not. **Durable fix:** a product call — either the bulk approve branch
      skips non-`ready_for_review` rows with a reported reason (it already
      skips-and-reports), or `new → approved` is documented as the manager's
      deliberate direct approval and kept. **Trigger:** the next change to bulk
      status, or an auditor asking whether every approval passed review.

- [ ] **(c) Other pages still restate server predicates to decide whether to
      offer an action.** Converted so far: payment queue record-outside (#573);
      History Void / Release / Dismiss; run Approve / Execute / Cancel; invoice
      Approve / Reject; exception Resolve / Dismiss / Escalate; credit-memo
      Apply / Edit / Void; vendor Verify / Reject; bank-change Approve; run
      Retry failed payments (§335); History Accept settlement, Runs Retry ERP
      sync, and the run dialog's record-run-as-paid / NACHA download /
      regenerate / bank-rejected (§344); `InvoiceModal`'s Submit / Complete and
      Retry ERP (§347); expense reports' submit / approve / reject / attach and
      card-transaction match / create-expense / unmatch / ignore (§348). Still
      restating (each a status list and/or role / permission / self check):
      intake approve / reject / convert; requisition
      submit / convert / cancel; discount-offer accept / decline; experiment
      start; adaptive-suggestion dismiss. **Mobile** restates the same rules and
      reads none of the verdicts yet: `payment_queue_screen._canApproveRun`
      (`isCfo` + status, no maker-checker), `invoice_detail_screen`'s approve
      (`canApproveInvoice` only), and the vendor verify / reject and exception
      resolve actions (status + role). The list verdicts ride the same list
      reads, and the invoice one is `GET /api/invoices/{id}/review-actions`.
      **Durable fix:** the same shape as the
      converted ones — a gate function the endpoint raises from, a `*_refusal`
      verdict on the row built from it, a parity test per role × row state, and
      the page reading `#lib/api/actionVerdicts.ts`
      (`frontend/docs/ui-patterns.md` § Whether to offer an action). Mobile's
      approve actions first (they touch money).
      **Trigger:** the next change to any of those controls, or a report of a
      button that 403s / 409s.

### Surfaced by the sidebar renames (2026-10-21, issue #635 #23, decisions §314)

- [ ] **(c) Partner Admin shows to every admin, partner or not — needs a product
      call.** #635 asked to hide the row unless the workspace is a partner, but
      nothing says so: `GET /api/partner`'s `is_partner` is derived from having
      at least one child, and `/admin/partner` is also where a standalone org
      provisions or attaches its first child (how it becomes a partner) and
      mints the link code that consents to being adopted. Gating the row on
      `is_partner` would strand both and break "the sidebar gate and the page's
      gate agree" (`frontend/CLAUDE.md`). **Durable fix:** an operator-granted
      partner capability (plan entitlement, or `organizations.is_partner`) on
      `/auth/me`, the `/api/partner` routes gated on it, the link-code minting
      moved to Organization settings (any admin may consent to adoption), and
      the nav row gated on the capability. **Trigger:** the product decision on
      who may act as a partner, or the next partner/reseller onboarding.

### Surfaced by fixing the payments-spec data leak (2026-10-21, issue #635, PR #636)

- [ ] **(c) Some e2e spec leaks draft payment runs, and `queue.spec` leaks a vendor.**
      After `delete-gate.spec` stopped leaking blocked invoices (16d74798), a
      check of the local e2e tenants found 20 draft payment runs on e2e2 dated
      2026-09-05..07 that no spec removes. A draft run holding live payments
      keeps those invoices out of the payment queue, which is the same
      cross-spec interference that broke `execute.spec`.
      `payments/queue.spec.ts`'s `createApprovedInvoice` also leaves its
      auto-created `E2E Pluralization Vendor` behind. It has no invoices, so it
      is inert today. **Durable fix:** find the spec that creates draft runs
      (grep `payment-runs` POSTs without a matching `deletePaymentRun` in an
      `afterEach`) and give it prefix-scoped cleanup. Remove the pluralization
      vendor in `queue.spec`'s teardown, and extend `meta/teardown-guard` to
      catch a spec that creates a payment run or vendor without removing it.
      **Trigger:** the next payments e2e flake, or the next pass over
      `meta/teardown-guard`.

### Surfaced by adding external uptime checks (2026-10-21, decisions §311)

- [ ] **(c) Nothing watches the Let's Encrypt certificates Caddy holds on the
      VM.** `infra/uptime.tf` alarms on the ACM certificate's `DaysToExpiry`,
      but `api.feohledger.com` (and, until the CloudFront cutover, the apex and
      every tenant host) is served on a certificate Caddy obtains and renews
      itself, which no AWS metric sees. The Route 53 health checks do not help:
      they complete the TLS handshake without validating the certificate, so an
      expired one still reads healthy while every browser refuses the API.
      Caddy renews 30 days ahead and retries, so this needs a renewal failing
      for a month unnoticed (an ACME rate limit, DNS moved away, port 80 closed
      by a security-group change). **Durable fix:** a CloudWatch Synthetics
      canary (an API canary, hourly: ~$0.86 a month at $0.0012 a run) that
      fetches `https://api.<domain>/api/health` with certificate validation on
      and fails under 14 days to expiry, alarming to the pager topic; it needs
      an artifacts bucket and an execution role in `infra/`. The alternative,
      a cron on the VM reporting Caddy's certificate expiry as a custom metric,
      is cheaper and is blind exactly when the VM is. **Trigger:** before
      customer #1, with the status-page work, or at once if Caddy's logs show
      a renewal failure.

### Surfaced by measuring the page-change glitch (2026-10-22, `tests-e2e/reactivity/layout-stability.spec.ts`)

- [ ] **(c) A white-labelled tenant's accent flashes the platform default on
      every cold load.** `stores/brand.svelte.ts` reads
      `GET /api/organization/branding` only after `/auth/me` resolves, and
      applies `--accent` / `--accent-strong` when it lands — so after a sign-in
      or a reload the active sidebar row, the primary buttons and the pressed
      chips paint in the default blue for a round trip and then turn the
      tenant's colour (measured: blue → the acme seed's dark red on the first
      page after login). Not per navigation — the store loads once a session —
      but it is the first thing a branded tenant sees. **Durable fix:** cache
      the two applied accent values per workspace (keyed by
      `getTenantStorageKey()`, never the shared theme cookie) and apply them in `app.html`'s pre-paint script beside the theme, letting the
      fetched branding correct a stale cache; clear the entry in
      `brand.reset()`. **Trigger:** the first tenant that sets a brand accent,
      or any white-label sale (the same trigger as the per-theme accent entry
      above).
- [ ] **(c) Tables without `fixed` re-flow their columns when their rows
      land.** `DataTable` uses `table-layout: auto` unless the page passes
      `fixed`, so the header cells are sized against the one "Loading…" row
      and then re-sized against the real rows: every column slides sideways as
      the data arrives (measured on /payments, /purchase-orders,
      /gl-accounts — small layout-shift scores, but visible). /invoices passes
      `fixed` with per-column widths and does not move. **Durable fix:** give
      each list page's `DataTable` `fixed` plus `<th>` widths, as /invoices
      does, page by page as each is next touched; extend
      `tests-e2e/reactivity/layout-stability.spec.ts` with a header-cell
      x-position check per converted page. **Trigger:** the next polish pass on
      any of those pages, or a second report of pages "jumping" once the
      vertical shifts fixed here have shipped.

### Surfaced by the 2026-10-22 production walkthrough (decisions §317–§318)

The walkthrough found defects the suite could not see because each check
covered the one page or shape it was written for. #646 / §318 closed the
QuickBooks half with recorded shapes and a live check; these are the
user-visible halves still open.

- [ ] **(c) Key fields and primary actions are checked to be on screen only on
      /login.** #642 added the check there (`tests-e2e/auth/login.spec.ts`:
      the email and password fields in the viewport of a short window, no
      scrolling) after a global class pushed them below the fold on every
      screen. Every other spec fills fields and clicks buttons by locator,
      and Playwright scrolls to them first, so a page whose search box,
      filters or primary action ends up below the fold still passes.
      **Durable fix:** one table-driven spec over every sidebar page and the
      signup / set-password / supplier-portal entry pages, at 1280×720,
      1366×768 and 390×844, asserting with `toBeInViewport()` (no scroll) the
      page's heading, its primary action and its first input or filter;
      each page's row names those three locators, and a new sidebar page
      without a row fails the spec (the `lib/help/pages.ts` pattern).
      **Trigger:** the next page added to the sidebar, or the next "I could
      not find the button" report.
- [ ] **(c) The layout-shift budget covers three list pages.** #644's
      `tests-e2e/reactivity/layout-stability.spec.ts` holds /invoices,
      /payments and /exceptions' late responses behind a gate and asserts the
      table does not move; /vendors, /purchase-orders, /gl-accounts,
      /credit-memos, /expenses, /contracts, /bank-reconciliation and the
      dashboard have no such check, and the measurement
      showed small shifts on some of them. **Durable fix:** make the spec
      table-driven over every main list page, with each page's late
      responses delayed by a realistic latency (the 2026-10-22 production
      timings, not zero), a cumulative layout-shift budget per page
      (`PerformanceObserver` `layout-shift`, under 0.1, the Core Web Vitals
      "good" line), and a new list page without a row failing the spec.
      **Trigger:** the next list page added, or the next polish pass on any of
      those pages.
- [ ] **(c) No scripted "first customer" release rehearsal.** The 2026-10-22
      walkthrough — sign up, connect QuickBooks, sync the chart and vendors,
      enter an invoice, approve it, push it, pay the bill in QuickBooks, watch
      the status come back — was done by hand and found what no spec did,
      because every spec seeds its own starting state. The day-one suite
      (`tests-e2e/day-one/`) covers signup to first invoice against an empty
      database, and stops there. **Durable fix:** extend the day-one journey
      through ERP connect (fake-erp's QuickBooks sandbox company,
      `fake_company=sandbox`), sync, approve, push, a person paying the bill
      in the fake (`/__qbo/pay-bill`), and the webhook / CDC sweep bringing
      the invoice to paid, asserting what the customer sees at each step; run
      it in CI's day-one job, and once per release by hand against the real
      sandbox with `pnpm test:erp:qbo-live` alongside. **Trigger:** before
      the next release tag, which is when the walkthrough would otherwise be
      repeated by hand.

### Surfaced by the 2026-10-31 follow-ups batch, round 2 (decisions §332–§337)

- [ ] **(c) A vendor whose sanctions screen keeps returning `match` or `review`
      can be neither paid nor booked, and there is no recorded way to clear a
      false positive.** §333 refuses the ERP push on the stored screen, and
      dispatch / `/compliance/release` re-screen live. **Durable fix:** an
      audited "adjudicated false positive" stamp (who, when, which screen
      result) that later screens and both gates respect, under a permission a
      preparer cannot hold. **Trigger:** the first live sanctions provider, or a
      customer report of a stuck vendor.
- [ ] **(c) Mobile does not show the ERP refusal behind a reconciliation row.**
      The web resolve dialog states `erp_report.refusal` through `erpRefusal.*`
      (§332); the mobile exception detail shows only the catalogued sentence.
      **Durable fix:** a mobile ERP refusal-code map with ARB strings, read on
      the exception detail. **Trigger:** the next change to the mobile
      exception detail.
### Surfaced by the 2026-10-31 follow-ups batch, round 3 (decisions §338–§344)

- [ ] **(c) ⚠️ SECURITY ANALYST REVIEW — "Bank rejected the file" can reopen a
      run that was exported and then switched to processor mode.** The NACHA
      void (`run_nacha_void_refusal`) checks only that the run is `exported`. If
      a tenant exports a file, the bank takes it, and the tenant then switches
      `payments.mode` to `processor`, a holder of `payment.execute` can void the
      run back to draft over the API and Execute it, paying the same suppliers
      twice. §251 says the `exported` claim closes the switch-to-processor path;
      the void reopens it. The web panel is hidden off record-only tenants, but
      the API accepts, and §344's parity test pins that verdict. **Durable
      fix:** refuse the void (or the Execute after it) once a file was exported
      on a tenant now in processor mode, with a parity case. **Trigger:** the
      Security Analyst's review, or the first tenant switching modes with an
      exported run.
### Surfaced by the 2026-10-31 follow-ups batch, round 4 (decisions §345–§348)

- [ ] **(c) `POST /bulk/status → done` closes an invoice with no source-status
      or live-claim check.** §347 narrowed `/complete` to `new` / `approved`,
      but the bulk door still moves a `posted_in_erp` invoice to `done` past its
      payment, and neither door checks for a live payment or card before closing
      an `approved` invoice (a staged or exported run leaves its invoice
      `approved`). **Durable fix:** gate bulk `done` on the same
      `complete_status_refusal` and on §309's `live_payment_invoices` /
      `live_card_invoice_ids`, reported per row as bulk already does; add the
      live-claim check to `/complete` too. **Trigger:** now — scheduled for the
      batch PR's next round.
- [ ] **(c) Mobile's invoice approve still restates its rule.**
      `invoice_detail_screen` and the home-screen approve use
      `canApproveInvoice` only, not `GET /api/invoices/{id}/review-actions`.
      Reading the verdict is one GET, but showing a refusal needs a mobile
      refusal-localization path (ARB strings per code). **Durable fix:** that
      path, then both screens on the verdict, with widget tests. **Trigger:** the
      next change to mobile approval.
- [ ] **(c) Expense status and card refusals are English in the HTTP body.**
      §348's verdicts carry codes, but the endpoints' own 409 texts (hit only in
      a race) are plain strings, kept so mobile keeps working. **Durable fix:**
      coded refusals with the English kept as `message`, and the mobile reader
      updated. **Trigger:** the next change to those endpoints.
- [ ] **(c) The deploy-script tests fail on macOS.** `test_deploy_release.py`,
      `test_frontend_publish.py` and `test_deploy_script_failures.py` (30 tests)
      run scripts using GNU `cp` / `date -d` flags that macOS rejects (`cp:
      illegal option`, `date: illegal option -- d`), so a full local suite on a
      Mac is never green; CI is Linux and passes. **Durable fix:** make the
      scripts portable (POSIX flags, or `python3` for the date arithmetic) since
      contributors run them from macOS too, not a platform skip. **Trigger:** the
      next change to `deploy/` or those tests.
- [ ] **(c) `test_rename_databases_script.py` can hang with several local
      pytest slots running.** Seen once with four concurrent groups (passed when
      run alone): likely the script waiting on a lock another slot's databases
      hold. **Durable fix:** find the lock it waits on and scope the script's
      targets to its own slot. **Trigger:** the next time it hangs, or the next
      change to the rename script.
- [ ] **(c) Two gaps in the outbound-host guard.** `_plain_client_calls` counts
      only the literal `httpx.AsyncClient(...)` form in modules that import
      `url_safety` (an alias or `from httpx import AsyncClient` is missed, though
      the discovery guard still forces classification), and the adopter checks
      confirm the factory is called, not that the per-org URL is what it is
      given. **Durable fix:** resolve httpx client constructors through imports
      in the AST walk, and assert the factory's URL argument traces to the
      settings value. **Trigger:** the next change to `test_url_safety.py`.

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
      observation window. This is mostly process work behind a founder
      decision and a vendor contract, but **not all engineering prereqs are
      complete** (*corrected 2026-10-07*): `soc2-readiness.md` still lists
      moving the existing S3 buckets onto Object Lock as pending code work,
      and 5xx/RBAC-denial alerting as pending. (Uptime monitoring is built,
      `infra/uptime.tf`, decisions §311, and waits on the SMS-paging (b)
      entry below.)
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

- [ ] **Verify the SYSPRO, Sage Intacct, Sage SA and Blackbaud adapters
      against a live system.** Each was built from published docs and tested
      against fake-erp only (decisions §269). Unverified:
      **SYSPRO** (issue #650's sandbox list; column names are corroborated by a
      third-party schema dump): the APSTIN element names
      (copy `APSTIN.XSD` / `APSTINDOC.XSD` / `COMFND.XSD` and an e.net
      Diagnostics sample into `tools/fake-erp/fixtures/syspro/`); the exact
      APSTIN and Logon error wording `classify_apstin_error` /
      `classify_logon_error` match on (duplicate, supplier / ledger on hold,
      control account, closed period, session expired, licence busy); whether
      COMFND takes the `GT` condition the PO walk pages with; the
      `ApInvoicePay.TrnType` code for a payment (assumed `P`), the sign
      convention of `TrnValue` (the paid rule compares `abs(sum)` with
      `abs(OrigInvValue)`, so +150 and a −250 refund would read as 100 paid)
      and whether `TrnValue` is in the invoice's currency or local (if local, a
      foreign invoice never reads `paid` — closed, but wrong); whether the
      `ApInvoicePay` read (`return_rows=1000`) can truncate, which it does not
      check today; whether
      `GetLogonProfile` / `LocalCurrencyCode` is served on the REST host as
      the syspro-ruby client reads it; `MForeignPrice`
      being the PO-currency price, the currency of a foreign PO's
      `NMscChargeValue`, and the line-discount and price-UOM arithmetic (POs
      with any of those are skipped today, never synced at a guessed total).
      Still open and higher risk: **tax** — APSTIN sends no tax code and
      distributes the gross, so an exclusive-tax supplier may post at 115 %
      or be refused; **success detection** — a post with no error element is
      read as posted, so a read-back of `ApInvoice` requiring `OrigInvValue ==
      amount` (`posted_total_unconfirmed` when it fails) is the durable fix,
      once **`OrigInvValue`'s currency and tax basis** are known (the
      idempotency compare depends on them too); the `ApInvoice.Invoice` width
      (a `doc_number_too_long` preflight); the GET query-string length for a
      long bill; `posting_period` / `IgnoreWarnings` behaviour for a closed or
      future period; `ApInvoice.InvoiceStatus` for reversed invoices;
      whether `GenMaster.Company` scopes a COMFND read; the e.net licence a
      logon takes (and a `runtime_product_code` for ISV Managed Run Time);
      and the TLS binding a customer has to publish.
      **Intacct:** bill `state` values, the purchasing-document query name,
      `txnTotal`, and whether REST can reverse a paid bill.
      **Sage SA:** the `SupplierInvoice/Save` body, `DocumentNumber` and
      `Reference` limits, OData filters, `CurrencyId` for home-currency
      suppliers, account categories, and delete in a locked period.
      **Blackbaud:** an empty `transaction_code_values`, the description
      length, and job latency; which date ListInvoices' `from_date` /
      `to_date` filter on and whether they are inclusive (the adapter
      queries a day either side, `#651` B5); whether a closed period, an
      inactive vendor or a missing project fails the create (400) or the
      background job (status 7), and whether `PreClosed` years take
      postings (B4, B7); and whether the cohort sandbox can read
      `/generalledger/v1/fiscalyears` (a 403 is treated as "cannot
      check").
      **Why blocked:** each needs a sandbox or developer-programme account.
      **Durable fix:** run each adapter's post / status / sync / void against
      its sandbox, then correct the adapter and fake-erp together.
      **Trigger:** the first customer on each ERP, or sandbox access, whichever
      comes first.

- [ ] **Verify NetSuite's OAuth 2.0 client-credentials sign-in against a real
      account** ([#654](https://github.com/Absence0760/feohledger/issues/654); built under #653).
      The grant is built from Oracle's documentation and proven against
      fake-erp, whose token endpoint verifies the assertion's signature and
      claims (`tools/fake-erp/netsuite_oauth2.py`). Unverified against Oracle:
      that `rest_webservices` alone covers SuiteQL, the exact `aud` NetSuite
      compares (its doc pages differ in the domain they show), the token
      endpoint's real error bodies, and whether `expires_in` arrives as a
      number or a string (both are read).
      **Why blocked:** needs a NetSuite account with an integration record and
      a mapped certificate — a design partner's sandbox or the SuiteCloud
      Developer Network.
      **Durable fix:** map a certificate, run test-connection, the three syncs,
      a push and a void through `auth_method: oauth2_m2m`, then correct the
      adapter and fake-erp together.
      **Trigger:** before the first NetSuite customer, and in any case before a
      customer must onboard on NetSuite 2027.1, where TBA is closed to new
      integrations.

- [ ] **Whether Blackbaud sells a higher-volume SKY product for a
      multi-tenant app, and what the platform key's tier really is.** The
      per-org call budget is in (decisions §322): each tenant on the platform
      key gets `FEOH_ERP_BLACKBAUD_TENANT_DAILY_CALL_BUDGET` (2,500) of
      `FEOH_ERP_BLACKBAUD_DAILY_CALL_QUOTA` (25,000, "Standard APIs") a day,
      and admins are warned at 80%. Both figures are assumptions about a
      subscription we have not bought. Blackbaud's community posts mention
      Partner subscriptions metered per application *per customer* and paid
      tiers of 100K and 250K calls a day. Those would change the right
      defaults, or make the per-tenant share unnecessary. **Durable fix:** ask
      Blackbaud (developer support / Partner Program) which product a
      commercial multi-tenant FE NXT integration is meant to use and its
      limits. Set both settings in `infra-secrets` to match, and if no
      higher tier exists, document that a tenant past its share stores its own
      `subscription_key`. **Trigger:** before a second production tenant
      connects through the platform key, or the first `erp_call_quota` notice
      in production.

## (b) Operator steps on merged code

- [ ] **Run one PO sync on each Merge.dev tenant after deploying PR #670.**
      Built (decisions §328): the sync re-keys POs an earlier build stored under
      their Merge id and marks the old `"UNKNOWN"` row, but only when it runs.
      Until then `/cfo` open-PO accruals can count such a PO twice. Run
      `POST /api/purchase-orders/sync-erp` per Merge.dev tenant and check the
      audit row's `rekeyed` / `retired` counts.
      **Trigger:** the deploy that ships PR #670, if any tenant has synced
      Merge.dev POs.

- [ ] **Turn on the sub-processor notice sweep in production.** Built
      (decisions §321): the published DPA now says every sub-processor change
      that needs the 30-day notice is emailed to each customer's
      administrators, and `FEOH_SUBPROCESSOR_NOTICES_ENABLED` (off by default)
      is what sends it. Set `FEOH_SUBPROCESSOR_NOTICES_ENABLED: "true"` in
      `infra-secrets/feohledger/prod.sops.yaml` (never in this repo), deploy,
      and confirm `subprocessor-notices` is running on `GET /api/health/sweeps`;
      until it is set `deploy/decrypt-env.sh` refuses the file and the API
      refuses to boot (`FEOH_ENVIRONMENT=production`). SES must
      be out of its sandbox for the email to reach a customer.
      **Trigger:** before the deploy that publishes the 2026-10-26 legal text,
      and in any case before customer #1.

- [ ] **Make the first recording of real QuickBooks payment shapes.** Built
      (decisions §318): `backend/tests/test_qbo_recorded_replay.py` replays
      `tools/fake-erp/fixtures/qbo_recorded/`, and until those files exist its
      replay and fake-comparison tests skip with the command that records
      them. Steps: real Intuit development app keys in `backend/.env` with the
      fake-erp `FEOH_ERP_QBO_*` overrides blanked; `pnpm dev`; connect `acme`
      to a sandbox company (Environment = Sandbox); `pnpm gen:qbo-recordings`;
      for the webhook, a tunnel to `/api/erp/webhook/quickbooks` and
      `pnpm gen:qbo-recordings --only webhook --webhook-from-ngrok`; review the
      diff and commit it. Then `pnpm test:erp:qbo-live`. Every step is
      `backend/docs/quickbooks-online-adapter.md` § Recorded payment shapes.
      **Trigger:** once #646 (§317) is on `main`, before the next release.

- [ ] **Turn on SMS paging for the uptime checks.** Built (decisions §311,
      `infra/uptime.tf`): with no phone number set the checks only email. In
      us-east-1, lease a toll-free number in AWS End User Messaging SMS and
      complete its registration (sole proprietor, ~15 days), give SNS the
      resource policy on the number, verify the operator's phone in the SNS
      SMS sandbox, then set `alert_phone_number` and `uptime_tenant_slug` in
      `infra-secrets/feohledger/prod.tfvars` (never in this repo), apply, and
      ring the phone with `aws cloudwatch set-alarm-state`. Every step is
      `infra/README.md` § Uptime checks and paging. **Trigger:** before
      customer #1 (#446 § Support + status).

- [ ] **Register the platform OAuth apps for QuickBooks Online, Xero, Sage
      Accounting and Blackbaud.** Until then those ERPs connect only when a
      tenant brings its own app (decisions §270). Steps: register each app
      with the redirect URI `FEOH_API_PUBLIC_URL/api/erp/oauth/callback`. For
      Intuit, also pass the production app assessment and publish real
      Disconnect / Reconnect pages; for Blackbaud, subscribe the SKY developer
      account to the "Standard APIs" product (free, no approval: 10
      calls/s and 25,000/day per subscription), and check whether the
      Blackbaud Partner Program terms apply to a commercial multi-tenant app.
      Then put `FEOH_ERP_QBO_*`, `FEOH_ERP_XERO_*`,
      `FEOH_ERP_SAGE_ACCOUNTING_*` and `FEOH_ERP_BLACKBAUD_*` (client id,
      secret, subscription key) in `infra-secrets/feohledger/prod.sops.yaml`.
      **Trigger:** before selling ERP sync (on any plan, Free included since
      decisions §281) to customers on those ERPs. The login-CSRF binding it depended on landed (decisions §336); have the Security Analyst review that design before the production apps are configured.

- [ ] **Turn on QuickBooks status sync in production.** Built and off by
      default (decisions §295). In the Intuit developer portal, on the
      production app: Webhooks → endpoint
      `FEOH_API_PUBLIC_URL/api/erp/webhook/quickbooks`, CloudEvents format,
      the Bill and BillPayment entities; copy the app's verifier token into
      `infra-secrets/feohledger/prod.sops.yaml` as
      `FEOH_ERP_QBO_WEBHOOK_VERIFIER_TOKEN` (never into this repo). Then set
      `FEOH_ERP_STATUS_SYNC_ENABLED=true` and
      `FEOH_ERP_CONNECTION_HEALTH_ENABLED=true` in the deployed env, after
      migration 0117 has run (the deploy's own migration step). **Trigger:**
      before the first pilot customer on QuickBooks pays a bill through
      FeohLedger.

- [ ] **Clear up what the wrong QuickBooks bill-payment reader left behind.**
      Until decisions §317 the adapter saw no payments on any real QuickBooks
      bill. On every tenant connected to QuickBooks Online, look for three
      leftovers.
      1. Open `payment_reconciliation` exceptions reading "settled, at least
         in part by a vendor credit" on a bill that was really paid in cash.
         Dismiss each one, then record the invoice paid outside FeohLedger
         with QuickBooks' date and rail. The open exception refuses the
         automatic record, and an unchanged bill is not re-read.
      2. False "paid twice" `erp_reconciliation` and "FeohLedger is paying it
         too" exceptions on bills that only our own write-back paid. Dismiss
         them.
      3. Payment voids audited `payment.erp_void_not_needed` for a payment
         that has an earlier `payment.erp_posted` carrying an
         `erp_document_id`. That BillPayment is still live in QuickBooks, and
         voiding it there by hand is the only way to clear it. The payment is
         already `voided` here, so `POST /api/payments/{id}/void` answers 409,
         and nothing re-queues an ERP void. Until it is voided, the bill reads
         as paid by us, and the invoice's next payment is held at dispatch.
         Query each tenant's `audit_log` for `action =
         'payment.erp_void_not_needed'` and compare `entity_id` against the
         `payment.erp_posted` rows.
      The known case is TEST-0002 on the production sandbox tenant (#598).
      **Trigger:** the first
      deploy carrying §317.

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

- [ ] **Fill the operator facts and get counsel to review the legal set.** ([#428](https://github.com/Absence0760/feohledger/issues/428)) Two
      facts in `frontend/src/lib/legal/operator.ts` are still `null` and render
      as `[… to be confirmed]`: the EU and UK Art 27 representatives (tracked
      separately above). The rest were set 2026-10-07
      ([decisions.md](decisions.md) §252): no separate registered entity, no
      published postal address (location Virginia, contact by email), no DPO,
      Virginia governing law and courts, hosting in AWS `us-east-1`, and no
      lead supervisory authority (no EU establishment). Counsel should confirm
      the email-only contact position. **Before the pages are served
      publicly, the workload stack must actually run in `us-east-1`** — the
      pages now say it does. The documents are complete and operative as written — these are
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
- [ ] **(b) Confirm every stored `auto_send_on_approval: true` before deploying
      decisions §304.** The flag did nothing until §304, and every workflow save
      used to store `true` through the schema default, so a stored `true` may be
      a choice or an accident. Once deployed, a tenant with one and a live ERP
      sends each approved invoice to the ERP on approval, and it leaves the
      payment queue. **Durable fix:** before the deploy, list the default and
      active workflow definitions (`steps_config`) holding
      `erp_export.config.auto_send_on_approval = true` in every tenant with a
      live ERP, confirm each with the tenant's admin, and set the rest to
      `false` through `PATCH /api/workflows/{id}`. Invoices already created
      keep the snapshot they froze, so the same check covers the unapproved
      invoices under such a definition. **Trigger:** the first deploy carrying
      decisions §304 to an environment with a tenant on a live ERP.
