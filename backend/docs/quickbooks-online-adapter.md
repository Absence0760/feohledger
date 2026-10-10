# QuickBooks Online direct adapter — scope

**Status: scoped, not started (2026-10-07).** Tracked in `docs/followups.md`
(c). This is the plan; `erp-integration.md` stays the description of what is
built until code lands.

## Why direct, not through Merge.dev

Merge's Launch plan is **$650/month for up to 10 production linked accounts,
then $65 per linked account** (merge.dev/pricing, read 2026-10-07; first three
free; Professional and Enterprise are contract-only). A linked account is one
customer connected to one integration. The Growth plan is $49/month and
includes `erp_integrations`, so every Growth customer who connects an ERP
through Merge costs more than they pay.

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
  - `POST …/disconnect` revokes the token at Intuit and clears local state.
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
  - Add `refresh_token` and `access_token` to the `erp` block's secret-field
    list in `services/provider_credentials.py`, so they are sealed under the
    app KMS key in `provider_credentials` like every other ERP secret
    (`docs/decisions.md` §266) and never read back. The only writers are the
    callback and the refresher, both through that service.
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
| `list_vendors` | `query: select * from Vendor` | `STARTPOSITION`/`MAXRESULTS 1000` paging; `Id` → `erp_vendor_id` |
| `list_gl_accounts` | `query: select * from Account` | Map `Classification` to the asset/liability/equity/revenue/expense vocabulary |
| `list_pos` | `query: select * from PurchaseOrder` | Plus/Advanced plans only; an empty list on Simple Start is correct |
| `post_invoice` | `POST bill?requestid=<correlation_id>` | See below |
| `get_invoice_status` | `GET bill/{id}` | `Balance == 0` → `paid`; `0 < Balance < TotalAmt` → `partially_paid`; else `open` |
| `void_invoice` | `POST bill?operation=delete` | QuickBooks has no bill void, only delete. See open question 1 |

**`post_invoice`:**
- **Idempotency, two layers.** `requestid=<correlation_id>` makes Intuit
  return the original response to a retry, but Intuit doesn't document how long
  it remembers a request id. So, as Business Central and NetSuite already do,
  pre-check with `query: select * from Bill where DocNumber = '…'` filtered to
  the vendor, and accept a hit only if its `PrivateNote` carries the
  correlation id. Every bill is written with
  `PrivateNote: "FeohLedger <correlation_id>"`.
- `VendorRef` ← `vendor_erp_id` (Phase 0). Lines are
  `AccountBasedExpenseLineDetail` with `AccountRef` ←
  `gl_account_erp_id`. Money goes through `dumps_exact_json` and never
  `float`.
- **Don't let QuickBooks re-total the bill.** QuickBooks derives `TotalAmt`
  from the lines, and our invariant is that the header `amount` is never
  recomputed from lines. If the lines (plus tax, as posted) don't sum to
  `amount`, refuse with `amount_mismatch` rather than post a different total
  into the customer's books.
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
- A manual sandbox checklist per release: one bill end to end, a duplicate
  retry, a payment write-back, a token refresh.

## Docs this touches when it lands

`erp-integration.md` (adapter table, setup, the QuickBooks section),
`docs/environment.md` (the new `FEOH_` settings), `docs/decisions.md` (direct
for QuickBooks/Xero, Merge for the long tail, and the Merge pricing behind it),
`api-surface.md` (the connect/callback/webhook routes), and the sub-processor
register via `/audit:third-party-data-flows`. Whether Intuit is a
sub-processor, when data flows to the customer's own books at their direction,
is a question for that audit.

## Open questions (product calls, not engineering)

Three remain open.

1. **"Void" on a QuickBooks bill means delete.** Allow it only while no
   payment is applied (`Balance == TotalAmt`) and refuse otherwise? Or never
   delete, and tell the operator to handle it in QuickBooks?
2. **Tax.** Phase 2 as scoped handles US companies with tax carried in the
   lines. UK/CA/AU VAT/GST needs `TxnTaxDetail` and tax-code mapping. Ship US
   first, or block on non-US?
3. **Early-pay discounts.** BillPayment has no discount field. Book the
   discount as a vendor credit applied in the same payment, or post the net
   amount and leave the difference open? It has to be decided before Phase 4.
4. ~~**Merge for Growth customers.**~~ Decided (`docs/decisions.md` §256):
   Merge-routed ERPs become a Scale-only `erp_merge` feature, and Growth's
   `erp_integrations` covers the direct adapters.

**Total: about three weeks of engineering** (Phases 0–4), plus Intuit review
time. Phase 0 is worth doing first on its own: it fixes the two adapters that
already exist.
