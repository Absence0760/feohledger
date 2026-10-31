# QuickBooks Online direct adapter — scope

**Status (2026-10-20): Phases 0–5 built** (Phases 3–5 in decisions §295 —
see § Phases 3–5 as built, below). The OAuth
connect flow (`services/erp_oauth.py`, `api/erp_oauth.py`) and the adapter
(`erp_adapters/quickbooks_online.py`) are described as built in
`erp-integration.md` § Connecting an OAuth ERP. Where the build departs from
this plan:

- Tokens are sealed under the app KMS key in the control-plane
  `provider_credentials` row for the `erp` block (decisions §266, §271);
  `settings.erp.oauth` keeps only the connection metadata.
- There is no `erp_connections` realm index table. The realm index is a
  control-plane UNIQUE expression index over the OAuth block itself,
  `uq_organizations_erp_oauth_company` on `settings.erp.oauth.(provider,
  external_tenant_id)` (migration 0117, §295): the block is the one record of
  the connection, so a second table would be a copy every writer of it must
  keep in step. The connect callback still refuses a company linked elsewhere
  (JSONB query under a Redis claim), and the index now refuses it in the
  database too (the callback answers `already_linked` on the IntegrityError).
- Access tokens are not cached in Redis: each call reads the stored block (one
  primary-key select). Refreshes are serialised by a Redis lock plus a
  compare-and-swap write.
- Routes are `GET /api/organization/erp/oauth/{provider}/authorize` (JSON
  `authorize_url`) and one shared `GET /api/erp/oauth/callback`, so every
  OAuth ERP registers one redirect URI. The callback is bound to the browser
  that called authorize (a nonce cookie), and the `realmId` it carries is
  linked only after `companyinfo/{realmId}` answers under the new token
  (`erp-integration.md` § Connecting an OAuth ERP).
- Open question 1 is answered conservatively: `void_invoice` deletes only a
  bill with no payment applied (`Balance == TotalAmt`).
- Open question 2 is answered by the shared line rules: the lines come from
  `erp_adapters/bill_lines.py` (erp-integration.md § Bill lines: one rule set,
  two views), so each line posts its tax-inclusive gross on its own account and
  no tax code is sent. Tax-exclusive lines are grossed up by their stated tax
  (per line, or all of it on a single line); several tax-exclusive lines with
  header-only tax are refused `tax_not_itemised`; lines that sum to neither are
  refused `amount_mismatch`. A coded line whose account has no QuickBooks id is
  refused `account_not_linked`, never posted on the header's account.
- QuickBooks still computes `TotalAmt` itself, and a company's default tax code
  can add tax on top of those lines. So after the create, a `TotalAmt` other
  than `amount` deletes the bill just created and fails non-retryable
  `posted_total_mismatch`; a create with no `TotalAmt` fails non-retryable
  `posted_total_unconfirmed` (`erp_adapters/posted_total.py`). The idempotent
  re-find applies the same check before reporting an earlier bill as posted.
- **A create QuickBooks refuses is a coded, non-retryable refusal**, not the
  bare "HTTP 400 (invalid_request)" every business rule used to collapse into
  (live sandbox, 2026-10-19: books closed, an inactive account and an inactive
  vendor all read the same). `_fault_errors` reads ONLY each Fault error's
  numeric `code` and an `element` that is a known Bill field name — never
  `Message` / `Detail`, which quote the submitted bill back. Mapping (codes per
  Intuit's error-code reference,
  developer.intuit.com/app/developer/qbo/docs/develop/troubleshooting/error-codes):

  | Intuit code | Refusal |
  |---|---|
  | 6200 Account Period Closed, 6210 … Required Password | `period_closed` |
  | 6140 Duplicate Doc Num | `duplicate_document_number` |
  | 6190 Invalid Company Status (subscription ended / billing) | `company_subscription_inactive` |
  | 2500 Invalid Reference Id, 610 Object Not Found, 6000 Business Validation | the bill's vendor, then each account it sent, is read back by id; `Active: false` or a 610 → `vendor_inactive` / `account_inactive` (with our GL `accounts`) |
  | 2500 / 610 with nothing inactive | `reference_invalid` |
  | anything else (incl. a 6000 with nothing inactive) | `intuit_fault` |

  Every one carries `intuit_code` in its params (and in the English message,
  "… (Intuit fault 6210)"), so an unmapped code is diagnosable from the audit
  row. A 5xx / 429 / 408 answer stays the retryable HTTP failure.
  The closed-books case was seen with the closing set to "warning only" in the
  QuickBooks UI: the API refuses anyway.
- The panel's **Test connection** is offered on a connected QuickBooks
  connection too; `POST /api/organization/test-erp` tests the saved connection
  with its stored tokens and answers a coded result the panel names the ERP in.
- The `x_refresh_token_expires_in` expiry is recorded on every token response
  (each refresh rotates it) and shown on `/status`; Phase 5 below acts on it.

## Phases 3–5 as built (decisions §295)

**The lifecycle change that makes them mean anything.** Until §295 a
confirmed push closed the invoice at `done` (`sent_to_erp → done`): the ERP
owned payment, FeohLedger never paid a pushed bill, and a QuickBooks `Paid` had
nothing to move. With `FEOH_ERP_STATUS_SYNC_ENABLED` on, a confirmed push to an
adapter that reports back (`ErpAdapter.supports_status_sync` — QuickBooks
Online) lands at `posted_in_erp` instead: still payable here, and paid status
flows back. With it off (the default) the push still closes at `done`, because
nothing would then guarantee a payment made in QuickBooks ever reached us.
`services/erp_status_sync.status_sync_active` is the one predicate. The push
also records `state_data.erp_realm`, the connection realm the bill lives in.

**Phase 3 — status sync-back** (`services/erp_status_sync.py`):

- `POST /api/erp/webhook/quickbooks` (before the generic `/webhook/{erp_type}`):
  the raw body is verified against `FEOH_ERP_QBO_WEBHOOK_VERIFIER_TOKEN`
  (`intuit-signature`, base64 HMAC-SHA256 — `webhook_security.
  verify_hmac_sha256_base64`) before it is parsed; empty token = refused.
  CloudEvents only (`qbo.bill.*` / `qbo.billpayment.*`; `intuitaccountid` is
  the company, `intuitentityid` the record). Each event is claimed by its id
  (`is_event_already_processed`) before the 204 goes back; a dedup store we
  cannot reach is a bodyless 503 so Intuit redelivers. The work — routing the
  company to its tenant through the realm index, reading the bill back, applying
  it — runs after the response (Intuit wants an answer within seconds); a
  failure releases the claim, and the sweep below is what guarantees delivery.
  Every rejection is a silent 204.
- The change-data-capture sweep (`erp-status-sync`, off by default) asks each
  connected company for `GET cdc?entities=Bill,BillPayment&changedSince=…` from
  its last COMPLETE pass (`settings.erp.oauth.status_sync_through`) or the
  lookback window (`FEOH_ERP_STATUS_SYNC_LOOKBACK_HOURS`, 72 h), whichever is
  earlier, and applies every bill the same way. A pass that cannot vouch for
  the gap — no mark (a first pass, or a new connection after a reconnect), a
  mark older than CDC's 30-day reach, or a CDC answer too full to trust (1,000
  objects) — reads back every bill still payable here that was posted to the
  company instead, so a sweep that was off, failing or skipped for days loses
  nothing. The mark moves only when the whole pass went through. The same pass
  finishes any ERP void a payment void recorded but did not complete.
- `bill_report(bill_id)` reads the bill (and its BillPayments through
  `LinkedTxn`, where QuickBooks types each one `BillPaymentCheck` or
  `BillPaymentCreditCard`, never `BillPayment` — decisions §317): `paid` / `partially_paid` / `open` / `deleted` (Fault 610 or
  `status: Deleted` only — an unreadable bill raises), the correlation id from
  our `PrivateNote` marker, and the latest cash payment a PERSON made: its date
  and rail (`PayType` `Check` → `check`, `CreditCard` → `card`). Whether the
  bill was taken down outside FeohLedger is judged from the BALANCE
  (`reduced_outside`: `TotalAmt − Balance` beyond what our own marked
  BillPayments applied), so a vendor credit — QuickBooks records one as a $0
  BillPayment linked to the `VendorCredit`, which is NOT a void — or a journal
  entry counts too; `settled_by_credit` says when anything but cash took part,
  or the cash read does not cover what left the bill outside us. Cash is
  defined positively (`_is_cash_payment`): a positive `TotalAmt` and every line
  linked to a `Bill` and nothing else, so a journal-entry credit cleared in Pay
  Bills, or a $0 payment not noted as voided, is never booked as a cheque
  (decisions §317).
- `apply_bill_report` matches a bill to its invoice by the marker's correlation
  id AND the invoice's `erp_reference` (a deleted bill, which cannot be read, by
  `erp_reference` within the recorded realm only), row-locks it, and:

  | Bill in QuickBooks | Invoice here | Result |
  |---|---|---|
  | settled outside us, by a vendor credit (in part or whole) | payable here | blocking `payment_reconciliation`; no cash payment is booked |
  | paid outside us, in cash | approved / sent / posted | recorded as paid outside FeohLedger (the ERP-paid path, `record_erp_reported_payment`), with QuickBooks' date and rail → `paid`, out of the queue |
  | paid outside us | payment_scheduled | blocking `payment_reconciliation`: FeohLedger is paying it too |
  | paid outside us | paid by a FeohLedger rail | `erp_reconciliation`: paid twice (a payment recorded as made outside is taken to be that same payment) |
  | paid only by our write-back | any | nothing |
  | partly paid or credited outside us | payable here | blocking `payment_reconciliation`: a full payment would overpay |
  | open again | paid by an ERP report | `erp_reconciliation`: the payment was deleted/voided there |
  | deleted / voided | payable here | blocking `payment_reconciliation` |
  | deleted / voided | sent / paid / done / failed | `erp_reconciliation` |

  Every exception is de-duplicated per (invoice, type) while open, and PII-free.
- The generic ERP webhook now also accepts optional `paid_on` / `payment_method`
  on a paid report (validated: a real date not in the future, a recordable rail
  — never `virtual_card`), falling back to the arrival date / NULL.

**Phase 4 — `post_payment` → BillPayment** (`services/erp_payment_writeback.py`,
`QuickBooksOnlineAdapter.post_payment`):

- `payment_erp_sync` writes a settled payment back once its invoice is `paid`,
  after committing the transition (no lock held across Intuit). Only for a bill
  the push recorded under the CURRENT realm; an invoice never pushed has no bill
  and nothing is written.
- `POST billpayment?requestid=<payment id>`, `LinkedTxn` → the bill,
  `PrivateNote: "FeohLedger payment <id>"`. Idempotent in two layers like the
  bill: the bill's own BillPayments are searched for the marker first, and the
  request id makes Intuit replay a retry. A replay of a payment someone voided
  in QuickBooks is refused `bill_payment_voided`, never reported as written.
- `PayType`: `Check` with `BankAccountRef` for bank rails, `CreditCard` with
  `CCAccountRef` for card rails. The accounts are the org's choice —
  `settings.erp.bill_payment_bank_account` / `bill_payment_card_account`, GL
  codes from our chart (the ERP setup form's two optional fields), resolved to
  QuickBooks ids under the current realm. Unmapped or unlinked →
  `payment_account_not_mapped`.
- Refused before any write: `bill_not_in_erp`, `currency_mismatch`,
  `payment_exceeds_balance` (more than the bill still has open). A QuickBooks
  refusal is coded from the numeric Fault code exactly like a bill
  (`period_closed`, `reference_invalid`, `intuit_fault` + `intuit_code`), never
  from `Message` / `Detail`.
- Outcomes are audited on the payment (`payment.erp_posted` /
  `payment.erp_post_failed` with the coded refusal); a failure opens a de-duped
  `erp_reconciliation` (`exception.erp_payment_writeback_failed`: the bill still
  reads unpaid in QuickBooks; fix the cause, then **Retry ERP sync** on the run,
  which is `POST /api/payments/runs/{id}/sync-erp`). The coded refusal, payment,
  run and bill are on its `erp_report`, not in the sentence. It never undoes the
  transition: the money moved.
- **Open question 3 (early-pay discounts) is answered conservatively:** the
  BillPayment is for what was actually paid; when the bill keeps a balance in
  QuickBooks afterwards (a discount or credit taken in FeohLedger), an
  `erp_reconciliation` exception (`exception.erp_payment_writeback_balance_open`)
  states the balance for the bookkeeper. No
  vendor credit is created on their behalf.
- Voiding a settled payment (`POST /api/payments/{id}/void`) records the
  pending ERP void on the invoice's workflow state IN the void's transaction
  (`erp_payment_voids_pending`), then voids our BillPayment after the commit
  (sparse update, `include=void`, current `SyncToken`). A process that dies
  before that task, or an ERP that refuses, leaves the marker for the next
  status-sync pass; a refusal also opens `erp_reconciliation`
  (`exception.erp_payment_void_writeback_failed`). "Nothing of ours
  there" is audited as `payment.erp_void_not_needed`, not as a void.
- **Voiding a payment the ERP REPORTED** (`provider = external`, referencing the
  bill) sends the invoice back to `approved` while QuickBooks still shows the
  bill paid, and nothing about the bill changes again — so the void opens the
  blocking `payment_reconciliation` in the same transaction.
- **Dispatch re-reads the bill** (decisions §296). Immediately before each
  FeohLedger payment for a QuickBooks-posted invoice is handed to its rail —
  after `dispatch_preflight` re-confirmed the local facts under the invoice row
  lock — `erp_bill_dispatch_refusal` reads the bill live (bounded by
  `FEOH_ERP_DISPATCH_CHECK_TIMEOUT_SECONDS`). Open → pay. Anything else →
  refused `erp_bill_not_open:<state>` and the blocking `payment_reconciliation`
  opened, the rest of the run carrying on. Paid only by our own write-back
  while a void of it is pending → held `erp_bill_void_pending`. A failed or
  timed-out read → held `erp_bill_unverified` (a connection-level failure
  holds that org's checks for a minute without reading). Both are retry-safe
  and open no exception. A push with no document id or company, or a bill in a
  company other than the one connected now → held `erp_bill_unlinked:<why>`
  with a non-blocking `erp_reconciliation`, since no retry clears it. The NACHA
  export and `POST /api/cards/generate` do the same read.
- The write-back re-reads the payment's status first (a void can commit
  between the sync's transition and the ERP call), posts what reached the
  supplier (the settled amount when it is in the bill's currency), and runs
  for every FeohLedger path to `paid`: the sync pass and an accepted short
  settlement (`/settlement/accept`, `dispatch_settled_writeback`).
- Not written back, by design: a payment recorded as made outside FeohLedger
  (a record-only tenant's, §251 — the customer enters it in QuickBooks too,
  and status sync then reads it as that same payment), and a bill pushed
  before §295 (no recorded realm, so nothing proves which company it is in).

**Phase 5 — the refresh-token expiry notice** (`services/erp_connection_health.py`,
sweep `erp-connection-health`, off by default): for every OAuth ERP connection,
a refresh token with fewer than `FEOH_ERP_CONNECTION_KEEPALIVE_DAYS` (45) left is
refreshed (`get_access_token(force_refresh=True)`, through the refresher's lock
and compare-and-swap) so a quiet tenant does not lose its connection; one still
inside a lead of `FEOH_ERP_CONNECTION_EXPIRY_NOTICE_DAYS` (`[30, 7, 1]`) — the
hard cap — gets an in-app + email notice to the org's admins
(`erp_connection_expiring`, once per (reported expiry, lead), marker
`settings.erp.oauth.expiry_notice`); a connection the provider already refused
gets one "reconnect" notice. The refresher now records why it was refused
(`reconnect_reason`: `refresh_token_expired` when past the expiry Intuit last
reported, else `refresh_token_revoked`); `/status` returns it and
`expires_soon`, and the ERP panel states the reason and offers Reconnect while
the connection is still live.

The rest of this file is the original plan.

## Why direct, not through Merge.dev

Merge's Launch plan is **$650/month for up to 10 production linked accounts,
then $65 per linked account** (merge.dev/pricing, read 2026-10-07; first three
free; Professional and Enterprise are contract-only). A linked account is one
customer connected to one integration. The Growth plan is $49/month and Free
is $0, and both include `erp_integrations` (decisions §281), so every such
customer who connects an ERP through Merge would cost more than they pay. Merge
is Scale-only (`erp_merge`, §256).

QuickBooks Online is the ERP most of the SMB/mid-market segment runs on.
Intuit's API costs nothing at the volumes in question: writes are "Core" calls
and are not metered, and reads ("CorePlus") are free up to 500,000 a month on
the App Partner Program's Builder tier. Over the cap, calls are **blocked**,
not billed. One status poll per open bill per day stays far below that.

Merge stays for the long tail (Sage Intacct, MYOB, the rest) as a Scale-only
feature (`docs/decisions.md` §256).

## Phase 0 — prerequisite, shared by every direct adapter

**The payload carries no ERP references.** `services/erp._build_payload`
sends `vendor_name` and the GL **code**, but every real ERP posts a bill
against the vendor's and the account's *internal ids*. The vendor and GL syncs
already store both (`vendors.erp_vendor_id`, `gl_accounts.erp_account_id`);
nothing passes them on. This is diagnosed in `docs/known-issues.md`, because it
already breaks the NetSuite and Business Central adapters (the fake ERP
accepts what the real ones would not).

- Add `vendor_erp_id: str | None` to `InvoicePayload` and
  `gl_account_erp_id: str | None` to it and to `LineItemPayload`.
- Resolve them in `_build_payload`: the vendor from `invoice.vendor_id`, and
  the account from the entity-scoped chart (shared NULL ∪ the invoice's
  entity — the same resolution the extraction GL catalogue uses).
- **Fail closed.** A direct adapter refuses a payload with no
  `vendor_erp_id`, using a stable reason code (`vendor_not_linked`), and never
  falls back to a name lookup. A name match picks the wrong "Acme" the first
  time two vendors share one.
- Fix NetSuite (`entity: {id}`, `account: {id}`) and Business Central
  (`vendorId`) in the same change, with tests.

Size: 1–2 days.

## Phase 1 — connecting a QuickBooks company (new primitive)

No ERP here uses an OAuth **authorization-code** flow today. Business Central
uses client credentials, NetSuite uses TBA, and Merge uses a pasted account
token. QuickBooks needs the customer's admin to consent in Intuit's UI.
Xero needs the same flow, so build it as a reusable piece rather than inside
the adapter.

- **Platform config, not tenant config.** One Intuit app serves every tenant.
  Its client id/secret and webhook verifier token are `FEOH_` settings, kept in
  sops in `infra-secrets`. Empty means QuickBooks is unavailable and the option
  is hidden (fail closed). Add `FEOH_ERP_QBO_API_BASE` /
  `FEOH_ERP_QBO_TOKEN_URL` operator overrides, the same shape as
  `erp_d365_*`, so local dev and e2e point at fake-erp.
- **Routes** (admin-gated except the callback):
  - `POST /api/organization/erp/quickbooks/connect` returns Intuit's authorize
    URL with a signed, expiring `state` (org, user, nonce) — the same shape as
    the SSO state.
  - `GET /api/organization/erp/quickbooks/callback` is public by design. It
    verifies `state`, exchanges the code, and stores the `realmId` + tokens.
  - `POST …/disconnect` revokes the token at Intuit (JSON `{"token": …}`, as
    Intuit documents — `revoke_json`) and clears local state.
  - Intuit **requires** a Disconnect URL and a Reconnect URL on the app
    listing, and both have to point at real pages.
  - Every connect/disconnect writes an audit row.
- **Token storage.** The refresh token grants full read/write on the
  customer's books for up to **five years** (Intuit's lifetime cap; the first
  expiries for accounting scopes arrive October 2028). Its value rotates
  roughly every 24 hours, so the backend must persist the newest one on every
  refresh.
  - Serialize refreshes per realm (row lock on the token row, or a Redis
    lock). Two workers refreshing at once must not each persist a different
    token.
  - Cache access tokens (60 minutes) per realm in Redis.
  - Sealed under the app KMS key in `provider_credentials`, beside the `erp`
    block's other secrets, at the service-only paths `oauth.access_token` /
    `oauth.refresh_token` (`provider_credentials.SERVICE_SECRET_FIELDS`;
    `docs/decisions.md` §266, §271). Never read back: `settings.erp.oauth`
    keeps only the connection metadata and reads as `{"connected": bool}`
    (`erp_adapters/catalog.public_erp_config`), a settings save never takes it
    from the request, and `PUT /api/organization/credentials/erp` refuses the
    token paths. The only writers are the callback, the refresher and
    disconnect, all through `provider_credentials.update_secrets`.
- **Expiry visibility.** Record `x_refresh_token_expires_in`. Show
  "reconnect required" on the org ERP card and send an admin notification
  30 days before expiry. A dead token must surface as a notification, never
  as a silently failing sync.
- **Realm → tenant index (control plane).** Intuit's webhooks identify the
  company by `realmId` alone (Phase 3), so the control plane needs a
  `erp_connections(realm_id UNIQUE, org_id)` table. A realm connected to two
  tenants is refused at connect time. This is a control-plane-only migration.

Size: 4–5 days.

## Phase 2 — the adapter (`erp_adapters/quickbooks_online.py`)

`@register_adapter("quickbooks_online")`, `integration_method: "direct"`. Every
call is sent to `/v3/company/{realmId}/…?minorversion=75`. Minor versions 1–74
were retired in August 2025, so earlier versions are served as 75 anyway.

| Method | QuickBooks call | Notes |
|---|---|---|
| `test_connection` | `GET companyinfo/{realmId}` | Also records the company's country and home currency for the checks below |
| `list_vendors` | `query: select * from Vendor where Active IN (true, false)` | Inactive vendors too (a plain select returns active ones only), emitted `active=False`, `DisplayName` without QuickBooks' ` (deleted)` suffix. `STARTPOSITION`/`MAXRESULTS 1000` paging; `Id` → `erp_vendor_id`. `code` is left unset: `Vendor.AcctNum` is the company's account number *at* the vendor, not an id for it — it rides as `legacy_code` so the sync clears it from vendors the pre-§284 mapping created (§284) |
| `list_gl_accounts` | `query: select * from Account where Active IN (true, false)` | Inactive accounts too, emitted `active=False` (the sync retires a linked row and creates none), with the ` (deleted)` QuickBooks appends to an inactive record's `Name` / `FullyQualifiedName` stripped (`_undeleted`). `parent_erp_account_id` = `ParentRef`, and `reports_parent` is set, so the sync names the parent by our code and clears it for a top-level account. **`code` = `AcctNum`, else `FullyQualifiedName`** — never the bare `Name`, which QuickBooks repeats across parents (the sandbox has "Job Materials" under both "Job Expenses" and "Landscaping Services"). `name` is the path; a path over 100 characters ends `…#<Id>`. `legacy_codes` carries the old `Name` key so the sync can unlink a row a pre-§284 sync collapsed. Map `Classification` to the asset/liability/equity/revenue/expense vocabulary. See erp-integration.md § GL accounts and vendors are linked by the ERP's unique key |
| `list_pos` | `query: select * from PurchaseOrder` | Plus/Advanced plans only; an empty list on Simple Start is correct. Read whole like the chart and vendor listings: a failed page (non-200, transport error, unreadable body) or more than 50 pages of 1000 raises `ErpListingIncomplete` (the PO sync answers the coded 502 and syncs nothing), never "no POs" or the first pages (§329). PO lines carry no GL account: nothing persists or reads one, so there is no chart read, and the `AccountRef` name is never taken as one (§284) |
| `post_invoice` | `POST bill?requestid=<correlation_id>` | See below |
| `get_invoice_status` | `GET bill/{id}` | `Balance == 0` with a `BillPaymentCheck` / `BillPaymentCreditCard` `LinkedTxn` and a non-zero `TotalAmt` → `paid`, without one → `settled_no_payment` (decisions §303); `0 < Balance < TotalAmt` → `partially_paid`; else `open`. (Status sync-back reads `bill_report`, not this.) |
| `void_invoice` | `POST bill?operation=delete` | QuickBooks has no bill void, only delete. See open question 1 |

**`post_invoice`:**
- **Idempotency, two layers.** `requestid=<correlation_id>` makes Intuit
  return the original response to a retry, but Intuit doesn't document how long
  it remembers a request id. So, as Business Central and NetSuite already do,
  pre-check with `query: select * from Bill where DocNumber = '…'` filtered to
  the vendor, and accept a hit only if its `PrivateNote` carries the
  correlation id. Every bill is written with
  `PrivateNote: "FeohLedger <correlation_id>"`. A replay can name a bill an
  earlier `posted_total_mismatch` deleted, so the created bill is read back;
  Fault 610 / `status: Deleted` moves to the next `requestid`
  (`<correlation_id>#r2`, …) — `erp-integration.md` § Connecting an OAuth ERP.
- `VendorRef` ← `vendor_erp_id` (Phase 0). Lines are
  `AccountBasedExpenseLineDetail` with `AccountRef` ←
  `gl_account_erp_id`. Money goes through `dumps_exact_json` and never
  `float`.
- **Don't let QuickBooks re-total the bill.** QuickBooks derives `TotalAmt`
  from the lines, and our invariant is that the header `amount` is never
  recomputed from lines. If the lines (plus tax, as posted) don't sum to
  `amount`, refuse with `amount_mismatch` rather than post a different total
  into the customer's books. (Built: the shared `bill_lines` helper, plus a
  check of the `TotalAmt` QuickBooks reports back — see the status notes at
  the top.)
- **`DocNumber`: 21 characters** is the limit integrators report; verify it
  against Intuit's entity reference. Refuse when it is exceeded
  (`doc_number_too_long`), and never truncate. QuickBooks' own duplicate
  warning keys on the number, and a truncated one collides.
- **Currency, fail closed.** A bill in a currency other than the company's
  home currency needs multicurrency enabled in QuickBooks *and* a vendor in
  that currency. Refuse when either is missing; never post it in the home
  currency.
- Errors go through `erp_failure_message`. Intuit's `Fault` body echoes
  fields back and is never persisted. Throttling is HTTP 429 (code 003001),
  and Intuit sometimes signals it as a 401, so a 401 retries once after a
  token refresh before it is reported as `unauthorized`.
- Limits: 500 requests per minute per realm, 10 concurrent.

Size: 3–4 days, including fake-erp routes.

## Phase 3 — status sync: webhooks + change-data-capture

- **New route `POST /api/erp/webhook/quickbooks`.** The existing
  `/webhook/{erp_type}` reads `tenant_slug` from the body and verifies against a
  per-tenant secret; Intuit sends neither.
  - Intuit signs the **raw body** with one app-wide verifier token, sent in the
    `intuit-signature` header (HMAC-SHA256, base64). Verify with
    `webhook_security.verify_hmac_sha256` before parsing.
  - Resolve each event's `realmId` through the Phase 1 index.
  - Dedupe by event id with `is_event_already_processed`.
  - Return 204 on every rejection path.
- **CloudEvents format only.** Intuit made the CloudEvents payload mandatory
  on 2026-05-15; the legacy `eventNotifications` envelope is not worth
  supporting. Events carry only entity ids, so the handler fetches the
  `Bill`/`BillPayment` and then runs the existing status-transition path.
- **Webhooks are not guaranteed delivery.** Add a daily reconciliation sweep
  over `GET cdc?entities=Bill,BillPayment&changedSince=…`; Intuit's
  change-data-capture covers 30 days. This also delivers the "polling job for
  status sync" `erp-integration.md` lists as planned, for QuickBooks at least.
  The sweep defaults to off, like every other sweep (guard rail 7).

Size: 2–3 days.

## Phase 4 — paying the bill in QuickBooks (`post_payment`)

No adapter writes payments back today. `payment_erp_sync` logs the payment and
transitions the invoice (`docs/followups.md`, "The ERP payment sync posts no
amount yet"). For an SMB customer, "the bill shows paid in QuickBooks" is the
feature, so this phase carries most of the value.

- Add `post_payment(PaymentPayload) -> ErpPostResult` to `ErpAdapter`. The base
  raises `NotImplementedError`, and `payment_erp_sync` keeps today's behaviour
  for adapters without it.
- QuickBooks: `POST billpayment?requestid=<payment id>`, `LinkedTxn` → the
  bill. `PayType` is `Check` (needs `BankAccountRef`) or `CreditCard` (needs
  `CCAccountRef`), so the org must map each payment rail to a QuickBooks
  account in settings. Refuse when a rail has no mapping.
- A void payment here maps to a BillPayment void (sparse update with
  `include=void` and the current `SyncToken`).
- The discount taken: see open question 3.

Size: 3 days.

## Phase 5 — Intuit production access (calendar time, not code)

Production keys require Intuit's app assessment (security questionnaire, app
details, the Disconnect/Reconnect pages) and the official "Connect to
QuickBooks" button assets. Start it as soon as Phase 1 runs against the
sandbox. Build and test against free Intuit sandbox companies throughout.

## Tests

- Adapter: request shape, the `requestid` and pre-check idempotency paths,
  every fail-closed refusal, money exactness, status mapping. Add the adapter
  to the existing cross-adapter guards: `test_erp_adapter_error_pii.py`,
  `test_erp_adapter_idempotency.py`, `test_erp_adapter_money_exact.py`,
  `test_erp_base_url_overrides.py`.
- OAuth: state forgery/expiry/replay, the refresh race (two concurrent
  refreshes persist one token), redaction for admins, and a realm already
  linked to another tenant.
- Webhook: bad signature → 204 + no effect; a duplicate event → one
  transition; an unknown realm → 204; re-serialized JSON must fail the
  signature (proves hashing uses the raw body).
- fake-erp: `/qbo/oauth2/token` and `/qbo/v3/company/{realm}/…` routes, plus a
  `quickbooks` spec in `frontend/tests-e2e/erp/` (connect → sync → post →
  webhook → paid).
- The live sandbox, replayed (#598): the Intuit sample company tested live on
  2026-10-19 is recorded in `tools/fake-erp/fixtures/qbo_sandbox_company.json`
  with our pre-§284 chart and the live syncs' answers. The fake serves it as a
  second company (`fake_company=sandbox` at authorize; `tools/fake-erp/README.md`)
  to `frontend/tests-e2e/erp/quickbooks-online-sandbox.spec.ts` (repair replay,
  pushes, vendors, token expiry / rotation / revocation, disconnect revoke), and
  `backend/tests/test_qbo_sandbox_replay.py` replays the same file through the
  adapter and both syncs in-process. A defect found against the real sandbox
  goes into that fixture, so CI keeps it found.
- A manual sandbox checklist per release: one bill end to end, a duplicate
  retry, a payment write-back, a token refresh.
- Recorded payment shapes and the live check: see § Recorded payment shapes,
  below.

## Recorded payment shapes (decisions §318)

The status sync's reads (`bill_report`, `map_bill_status`, `bills_for_payment`,
`changed_bills`, `parse_qbo_events`) are replayed in CI over answers recorded
from a real sandbox company, because hand-written doubles once agreed with a
reader real QuickBooks never matched (§317).

| Scenario file (`tools/fake-erp/fixtures/qbo_recorded/`) | What the replay asserts |
|---|---|
| `bill_paid_by_check.json`, `bill_paid_by_credit_card.json` | `paid`, reduced outside, NOT by credit, rail `check` / `card`, the payment's date, its id external |
| `bill_part_paid.json` | `partially_paid`, reduced outside, cash by cheque |
| `bill_vendor_credit.json` | `paid`, reduced outside, `settled_by_credit`, no date or rail booked |
| `bill_paid_by_feohledger.json` | `paid_only_by_us`, not reduced outside, nothing external |
| `bill_payment_voided.json` | `open`, the voided payment not external |
| `bill_deleted.json` | `deleted` |
| `cdc.json` | `changed_bills` names every bill the recording run changed |
| `webhook.json` | `parse_qbo_events` reads every bill / bill-payment event, company = the placeholder realm |

Each bill scenario also checks `bills_for_payment` on its payments and, for the
cheque, card, void and CDC files, that `tools/fake-erp/qbo_records.py` builds
records carrying every field the sync reads with QuickBooks' types and link
types. A file not recorded yet is a skip naming the command below.

**Making the recording** (operator, not CI — it writes to the sandbox):

1. In `backend/.env`, a real Intuit development app's keys
   (`FEOH_ERP_QBO_CLIENT_ID`, `FEOH_ERP_QBO_CLIENT_SECRET`) and the four
   fake-erp overrides blanked (`FEOH_ERP_QBO_API_BASE=`,
   `FEOH_ERP_QBO_AUTHORIZE_URL=`, `FEOH_ERP_QBO_TOKEN_URL=`,
   `FEOH_ERP_QBO_REVOKE_URL=`); the app's redirect URI must include
   `http://localhost:8000/api/erp/oauth/callback`.
2. `pnpm dev`, then connect the `acme` tenant (Organization → ERP Integration)
   to a sandbox company with Environment = Sandbox.
3. `pnpm gen:qbo-recordings` — creates and records every scenario except the
   webhook. `--only <scenario>` repeats one; `--bill bill_paid_by_check=<id>`
   records a bill you paid by hand with Pay Bills instead of creating one.
4. The webhook body: point the Intuit app's webhook URL at a tunnel to
   `/api/erp/webhook/quickbooks` (`ngrok http 8000`), pay a bill in the sandbox,
   then `pnpm gen:qbo-recordings --only webhook --webhook-from-ngrok` (or
   `--webhook-body <saved.json>`).
5. Review the diff (every string not read by the sync reads `[scrubbed]`), run
   `pytest tests/test_qbo_recorded_replay.py`, and commit the files.

Re-record when QuickBooks' minor version (`MINOR_VERSION`) moves, and whenever a
live check fails.

**The live check.** `pnpm test:erp:qbo-live` (`backend/tests/test_qbo_live_contract.py`)
re-asks a connected company, read-only — see `erp-integration.md` § Live
QuickBooks contract check.

## Docs this touches when it lands

`erp-integration.md` (adapter table, setup, the QuickBooks section),
`docs/environment.md` (the new `FEOH_` settings), `docs/decisions.md` (direct
for QuickBooks/Xero, Merge for the long tail, and the Merge pricing behind it),
`api-surface.md` (the connect/callback/webhook routes), and the sub-processor
register via `/audit:third-party-data-flows`. Whether Intuit is a
sub-processor, when data flows to the customer's own books at their direction,
is a question for that audit.

## Open questions (product calls, not engineering)

Two remain open (1 and 2 are answered conservatively in the build; 3 is decided).

1. **"Void" on a QuickBooks bill means delete.** Allow it only while no
   payment is applied (`Balance == TotalAmt`) and refuse otherwise? Or never
   delete, and tell the operator to handle it in QuickBooks?
2. **Tax.** Phase 2 as scoped handles US companies with tax carried in the
   lines. UK/CA/AU VAT/GST needs `TxnTaxDetail` and tax-code mapping. Ship US
   first, or block on non-US?
3. ~~**Early-pay discounts.**~~ Decided for Phase 4 (`docs/decisions.md` §295):
   post the amount actually paid and leave the difference open in QuickBooks,
   flagged as an `erp_reconciliation` exception naming the balance. Booking it
   as a vendor credit stays a possible later refinement.
4. ~~**Merge for Growth customers.**~~ Decided (`docs/decisions.md` §256):
   Merge-routed ERPs become a Scale-only `erp_merge` feature, and Growth's
   `erp_integrations` covers the direct adapters. Built with §281, which also
   puts `erp_integrations` on Free.

**Total: about three weeks of engineering** (Phases 0–4), plus Intuit review
time. Phase 0 is worth doing first on its own: it fixes the two adapters that
already exist.
