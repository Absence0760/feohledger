# fake-erp

A small, deterministic, in-memory fake of the three real ERP providers the
backend integrates with, so the real adapters in
`backend/app/services/erp_adapters/` can be exercised end-to-end with no
cloud account and no credentials. One FastAPI process, three path-prefixed
surfaces:

| Prefix | Emulates | Adapter |
|---|---|---|
| `/merge/api/accounting/v1` | Merge.dev unified accounting API | `merge_dev.py` |
| `/netsuite/services/rest/record/v1` | NetSuite SuiteTalk REST (OAuth 1.0 TBA, or an OAuth 2.0 bearer) | `netsuite.py` |
| `/netsuite/services/rest/auth/oauth2/v1/token` | NetSuite OAuth 2.0 client credentials (M2M) | `netsuite.py` |
| `/d365` | Dynamics 365 Business Central OData v4 | `dynamics_365_bc.py` |

## Auth (fails 401 in each provider's error shape)

- **Merge**: any non-empty `Authorization: Bearer …` **and** a non-empty
  `X-Account-Token` header.
- **NetSuite**: either sign-in NetSuite accepts.
  - TBA: an `Authorization: OAuth …` header containing `oauth_consumer_key`,
    `oauth_token` and `oauth_signature` params. The HMAC signature is **not**
    verified — shape only.
  - OAuth 2.0 client credentials (M2M): `Authorization: Bearer <token>` from
    `POST /netsuite/services/rest/auth/oauth2/v1/token`, unexpired. That
    endpoint **does** verify the JWT client assertion as NetSuite documents it
    (`netsuite_oauth2.py`: `typ`, `alg` PS256/384/512 or ES256/384/512 — not
    RS256 — `kid`, the signature, `iss` = the mapped client id, `aud` = its own
    URL, `scope`, `iat`/`exp` under 60 minutes) and answers `{access_token,
    expires_in: 3600, token_type: "Bearer"}`, or a 400 `{error,
    error_description}`. Map a certificate first, as an administrator does:
    `POST /__netsuite/oauth2/certificates {"certificate_id", "client_id",
    "public_key_pem"}` (the PUBLIC key or X.509 PEM only; RSA 3072/4096 or EC
    P-256/P-384/P-521). Mappings survive `/__reset`; issued tokens do not.
    `POST /__netsuite/oauth2/expire-tokens` makes every bearer 401;
    `GET /__netsuite/oauth2/stats` returns `{minted}`.
- **D365**: `POST /d365/oauth2/token` (also `/d365/oauth2/v2.0/token` and
  `/d365/{tenant}/oauth2/v2.0/token`) accepts any `client_credentials` form
  POST with non-empty `client_id`/`client_secret` and returns
  `access_token: fake-d365-token`; every OData endpoint then requires
  `Authorization: Bearer fake-d365-token`.

## Surfaces

- **Merge**: `POST /invoices` (creates `merge-inv-<n>`, status `OPEN`; response
  nests the record under `model`), `GET /invoices/{id}` (top-level `status`),
  `GET /purchase-orders` + `GET /accounts` + `GET /vendors` (all
  cursor-paginated 2 + 1 via `next` / `?cursor=`), `GET /account-details`
  (test_connection).
- **NetSuite**: `POST /vendorBill` → **204** with the new numeric id (`1001`,
  `1002`, …) in the `Location` header, status `Open` (`Pending Approval` for
  vendor `28`, which is under approval routing);
  `GET /vendorBill/{id}` → the record with its `status` and `total` — the
  lines plus any tax code's tax (account `123` / `6400` adds 10%, rounded to
  the cent), written as an exact JSON number;
  `GET /vendor?limit=1` (test_connection);
  `POST /netsuite/services/rest/query/v1/suiteql` (requires
  `Prefer: transient`; answers only `SELECT … FROM account`, paged by
  `limit`/`offset`/`hasMore`) — the chart sync.
- **D365**: `POST …/companies({id})/purchaseInvoices` → 201 `d365-inv-<n>`
  status `Draft`, with `totalAmountExcludingTax` / `totalTaxAmount` /
  `totalAmountIncludingTax` computed from the lines (company `fake-vat-co`
  adds 20% VAT on top, like a real BC VAT company; every other company is
  untaxed); `POST …/purchaseInvoices({id})/Microsoft.NAV.post` → 204,
  flips a `Draft` to `Open` and moves its etag (400 for anything already posted);
  `GET …/purchaseInvoices({id})`; `DELETE …/purchaseInvoices({id})` (`If-Match`
  must be the current etag or `*`, else 412; drafts only);
  `GET …/vendors?$top=1` (test_connection). OData base is `/d365`, i.e.
  `/d365/{environment}/api/v2.0/companies({company_id})/<resource>`.

## Fixed fixtures (e2e asserts these literals — do not change)

Purchase orders (`GET /merge/api/accounting/v1/purchase-orders`):

1. `PO-FAKE-301` — vendor `merge-vendor-701` "Fake Merge Vendor Co", total 1250.00 USD
2. `PO-FAKE-302` — vendor `merge-vendor-702` "Fake Merge Supply Co", total 980.50 USD
3. `PO-FAKE-303` — vendor `merge-vendor-701` "Fake Merge Vendor Co", total 4400.00 USD

As in Merge, a PO's `vendor` is the vendor's Merge id — the one the vendor
sync stores, so the PO sync links by it — and a line's `account` is a Merge
Account id (`merge-acct-6100` …), not an account number.

GL accounts (`GET /merge/api/accounting/v1/accounts`):

1. `6100` "Fake Office Supplies" (expense)
2. `6200` "Fake Software" (expense)
3. `6300` "Fake Consulting" (expense)

Suppliers (`GET /merge/api/accounting/v1/contacts?is_supplier=true`). Merge has
no `/vendors` path and no Vendor model — a supplier is a `Contact` with
`is_supplier` true, and a Contact has **no payment term** — so the fake serves
none either and `/vendors` is a 404, as on Merge
([OpenAPI](https://docs.merge.dev/openapi/accounting.json),
[Contacts](https://docs.merge.dev/accounting/contacts/)). `addresses` and
`phone_numbers` come back as objects only when named in `expand`, else as bare
ids:

1. `merge-vendor-701` "Fake Merge Vendor Co" — tax id `71-1234567`
2. `merge-vendor-702` "Fake Merge Supply Co" — tax id `72-2345678`
3. `merge-vendor-703` "Fake Merge Services Co" — tax id `73-3456789`

plus `merge-customer-801` "Fake Merge Customer Co", a customer
(`is_supplier: false`) served only to a request that does not filter on
`is_supplier=true`, so an adapter that drops the filter fails the e2e count.

### References are enforced by id, as the real ERPs do

A bill naming its vendor or account by anything but a known id is a **400**, so
the e2e suite proves the adapters post the ids the syncs stored
(`backend/docs/erp-integration.md` § ERP references):

- **Merge** `POST /invoices`: `contact` must be a fixture contact id
  (`merge-vendor-701` …); a non-null line `account` must be a fixture account id
  (`merge-acct-6100` …).
- **NetSuite** `POST /vendorBill`: `entity.id` must be a vendor id (`25`, `26`,
  `28`);
  the `expense` sublist must be non-empty with each `account.id` an account id;
  an `item` sublist is refused.
- **D365** `POST …/purchaseInvoices`: `vendorId` (or `vendorNumber`) must name a
  fixture vendor.

NetSuite vendors (`GET /vendor`): `25` "Fake NetSuite Vendor A", `26` "Fake
NetSuite Vendor B", `28` "Fake NetSuite Vendor Routed" (approval routing).
NetSuite accounts (SuiteQL): `120` → `6100`, `121` → `6200`, `122` → `6300`,
`123` → `6400` (10% tax code). Both behaviours are fixed fixtures, not a toggle,
so parallel e2e workers cannot trip over each other. D365 vendor (`GET …/vendors`): id
`5d115c9c-44e3-ea11-bb43-000d3a2feca1`, number `V0001`, "Fake BC Vendor A".
Each provider's vendor names are distinct, so one e2e tenant syncing all three
never links one provider's vendor id onto another's vendor row.

## Test hooks

- `GET /health` → `{"status": "ok"}`
- `POST /__reset` → clears all in-memory state (counters + stored invoices), except the QuickBooks transaction-Id sequences (see QuickBooks Online below)
- `POST /__set-status` `{"provider": "merge"|"netsuite"|"d365", "id": "…",
  "status": "…"}` → force a stored invoice into a provider-native status
  (e.g. merge `PAID`, netsuite `paidInFull`, d365 `Paid`) to drive
  `get_invoice_status()` transitions.
- OAuth token lifecycle, for any `FakeOAuthApp` (`xero`, `sage`, `blackbaud`):
  `POST /__oauth/{key}/lifetime {"seconds"}` sets the `expires_in` of every token
  issued from then on (under the backend's 120 s refresh margin, each use must
  refresh first); `GET /__oauth/{key}/stats` → `{"refreshes"}` counts successful
  refresh grants; `POST /__oauth/{key}/revoke-access` rejects every access token
issued so far while the refresh token stays good (a 401 must refresh);
`POST /__oauth/{key}/revoke` revokes the consent — every issued
  token and the refresh token stop working, so the next refresh is
  `invalid_grant`. Only `erp/blackbaud-fe-nxt.spec.ts` uses them, on the one
  provider no other spec shares, so parallel workers cannot interfere.

## Dependencies

Direct deps live in `requirements.in`; the image installs from the generated
`requirements.txt` with `pip install --require-hashes`, so every package —
transitive ones included — is pinned by hash. Same posture as the backend
image, and the base image is digest-pinned too.

Dependabot maintains both files: it recognises a pip-compile lockfile only
when the name ends in `.txt` and matches the `.in` basename, which is why
this directory uses `requirements.txt` rather than the backend's
`requirements.lock`. To regenerate by hand after editing `requirements.in`:

```bash
uv pip compile requirements.in --universal --python-version 3.14 --generate-hashes -o requirements.txt
```

## Running

Standalone (any venv with `pip install -r requirements.txt`):

```bash
uvicorn app:app --port 12112   # from tools/fake-erp/
```

Via compose (opt-in `erp` profile, host port **12112** → container 8080):

```bash
docker compose -f backend/docker-compose.yml --profile erp up -d fake-erp
```

Point the backend at it with the `FEOH_ERP_*_API_BASE` overrides in
`backend/.env.development`:

```
FEOH_ERP_MERGE_API_BASE=http://localhost:12112/merge/api/accounting/v1
FEOH_ERP_NETSUITE_API_BASE=http://localhost:12112/netsuite/services/rest/record/v1
FEOH_ERP_D365_API_BASE=http://localhost:12112/d365
FEOH_ERP_D365_TOKEN_URL=http://localhost:12112/d365/oauth2/token
```

## Sage Intacct (`/intacct/ia/api/v1`)

Backs `erp_adapters/sage_intacct.py` (`FEOH_ERP_INTACCT_API_BASE`).

- `POST /oauth2/token` — `client_credentials` form with non-empty
  `client_id` / `client_secret` and a `username` containing `@` → bearer
  `fake-intacct-token`, required on every other call.
- `POST /services/core/query` — `accounts-payable/vendor` (`V-ACME`,
  `V-BETA`), `general-ledger/account` (`6100`, `6200`, `2000`),
  `purchasing/document::Purchase Order` (`PO-INTACCT-401` 1250.00 pending,
  `PO-INTACCT-402` 980.50 closed) and the created bills; `$eq` filters,
  `start` / `size` paging with `ia::meta.next`.
- `POST /objects/accounts-payable/bill` → 201 key `5001`, `5002`, …, state
  `posted`; rejects an unknown vendor / GL account or a non-string `txnAmount`.
  `GET` / `DELETE /objects/accounts-payable/bill/{key}` (a paid bill refuses
  deletion).
- `POST /intacct/ia/api/v1/__set-state` — test hook
  `{"key", "state", "totalTxnAmountDue"}`.

## SYSPRO 8 e.net REST (`/syspro/SYSPROWCFService/Rest`)

Backs `erp_adapters/syspro.py` (`FEOH_ERP_SYSPRO_API_BASE=http://localhost:12112/syspro`).
Every call is `GET` with query-string parameters; errors are HTTP 200 with a
body starting `ERROR`, as SYSPRO's are.

- `Logon?Operator=&OperatorPassword=&CompanyId=&CompanyPassword=` → a session
  id (non-empty operator, password and company required); `Logoff?UserId=`.
  `GET /syspro/SYSPROWCFService/Rest/__sessions` reports how many are still
  open — the adapter should always leave it at 0.
- `Query/Query?BusinessObject=COMFND` — tables `ApSupplier` (`0000001`,
  `0000002`, and `0000003` with `OnHold=Y`), `GenMaster` (`6100`, `6200`
  expense, `6300` expense with `AccOnHldFlag=Y`, `2000` creditors control with
  `ControlAccFlag=Y`), `PorMasterHdr` / `PorMasterDetail` (`PO-SYS-501`
  1250.00 open, `PO-SYS-502` 980.50 complete, `PO-SYS-503` a USD order whose
  lines carry `MForeignPrice` 100.00 beside the local `MPrice` → 400.00 USD,
  `PO-SYS-504` 3 × 20.00 plus a 45.50 misc-charge line and a comment line →
  105.50, `PO-SYS-505` a EUR order with one line priced only in local
  `MPrice` — no EUR total, so the adapter skips it), the posted `ApInvoice`
  rows and their `ApInvoicePay` rows.
- `GetLogonProfile?UserId=` — the session's company profile; its
  `LocalCurrencyCode` (`ZAR`) is what the adapter compares a PO header's
  `Currency` with to decide whether the PO is foreign. Element names follow
  the open-source client wildland/syspro-ruby
  (`lib/syspro/get_logon_profile.rb`).
  `EQ` / `NE` / `GT` / `GE` / `LT` / `LE` expressions; rows come back sorted
  by the `OrderBy` column and cut at `ReturnRows`, as COMFND's are — so a
  caller has to page with a `GT` cursor.
- `Transaction/Post?BusinessObject=APSTIN` — rejects an unknown supplier, a
  supplier on hold (`Supplier '…' is on hold`), an unknown ledger code, one on
  hold (`Ledger code '…' is on hold`), a control account (`… is a control
  account and cannot be used`), a duplicate (supplier, invoice) and a
  distribution that does not balance to `InvoiceAmount`. The wording is ours;
  SYSPRO's is to be confirmed in a sandbox (docs/followups.md).
- The `XmlIn` / `XmlParameters` documents are parsed with `defusedxml`, so a
  DTD or entity declaration is refused rather than expanded.
- `POST /syspro/SYSPROWCFService/Rest/__set-balance` — test hook
  `{"supplier", "invoice", "balance"}`. `"0"` reads as settled with no
  payment: a zero balance with no `ApInvoicePay` payment row is never paid
  (docs/decisions.md §303).
- `POST /syspro/SYSPROWCFService/Rest/__pay` — test hook
  `{"supplier", "invoice", "amount"?, "trn_type"?}`: records an `ApInvoicePay`
  row (`TrnType` default `P`, a payment) and takes the amount (default: the
  whole balance) off `MthInvBal1`. With `"trn_type": "A"` it is an adjustment
  that zeroes the balance with no payment.
## Xero (`/xero/api.xro/2.0`)

Backs `erp_adapters/xero.py`. Auth is shape-only: any non-empty
`Authorization: Bearer …` plus a non-empty `Xero-Tenant-Id` (401 / 403
otherwise). Point the backend at it with
`FEOH_ERP_XERO_API_BASE=http://localhost:12112/xero/api.xro/2.0`.

- `GET /Organisation` — `Fake Xero Org` (ZAR, ZA).
- `GET /Contacts?where=IsSupplier==true` — `xero-contact-1` *Fake Xero
  Supplier Co* (the customer `xero-contact-2` is filtered out).
- `GET /Accounts` — `xero-acc-6100` / `6200` (default tax type `INPUT`) and
  `xero-acc-6300` (no default tax type: a taxed bill is refused unless the org
  sets `default_tax_type`).
- `GET /PurchaseOrders` — `PO-XERO-401` (1250.00, AUTHORISED → open),
  `PO-XERO-402` (980.50, BILLED → closed).
- `GET /Invoices` with `InvoiceNumbers` / `ContactIDs` / `Statuses` filters
  (the pre-create idempotency lookup); `PUT /Invoices` creates `xero-inv-<n>`
  and replays the original response for a repeated `Idempotency-Key`;
  `GET /Invoices/{id}`; `POST /Invoices/{id}` changes status (DELETED only
  from DRAFT/SUBMITTED, VOIDED only from unpaid AUTHORISED).
- OAuth (the backend's real connect flow): `GET /xero/identity/connect/authorize`
  302s to a **registered** `redirect_uri` with a `code` (client `fake-xero-client`,
  `response_type=code`; anything else is a 400); `POST /xero/identity/connect/token`
  takes `fake-xero-client:fake-xero-secret` by HTTP Basic, makes each code single-use
  and bound to its `redirect_uri`, and rotates refresh tokens; `GET /xero/connections`
  (bearer must be one it issued) lists the one organisation, tenant
  `fake-xero-tenant-1`. Wired up by the committed `FEOH_ERP_XERO_*` dev values.
- Test hook `POST /xero/api.xro/2.0/__set-status {"id", "status",
  "amount_paid"?}`. The backend reads `PAID` as paid only with `amount_paid`
  above zero; `PAID` with nothing paid is a bill settled by credit note
  (docs/decisions.md §303). `POST /__reset` clears this state too.

## Sage Business Cloud Accounting (`/sage/v3.1`)

Backs `erp_adapters/sage_accounting.py`. Auth is shape-only: any non-empty
bearer plus a non-empty `X-Business`. Collections use Sage's
`{"$items", "$next", ...}` envelope. Point the backend at it with
`FEOH_ERP_SAGE_ACCOUNTING_API_BASE=http://localhost:12112/sage/v3.1`.

- `GET /business_settings` (the connection test).
- `GET /contacts?contact_type_id=VENDOR` — `sage-contact-1` *Fake Sage
  Supplier Ltd* (the customer `sage-contact-2` is filtered out).
- `GET /ledger_accounts`, `GET /ledger_accounts/{id}` — `sage-ledger-5000`,
  `sage-ledger-7500` (default tax rate `GB_STANDARD`), `sage-ledger-7600` (no
  default rate: a taxed invoice is refused unless the org sets
  `default_tax_rate_id`).
- `GET /purchase_invoices` with `contact_id` / `from_date` / `to_date` (the
  pre-create idempotency lookup); `POST /purchase_invoices` creates
  `sage-pi-<n>` as `UNPAID`, and 422s on an unknown contact or ledger, a taxed
  line with no `tax_rate_id`, or net + tax that does not equal the total;
  `GET /purchase_invoices/{id}`; `DELETE /purchase_invoices/{id}` deletes a
  draft and voids an unpaid invoice (`void_reason` required).
- OAuth (the backend's real connect flow): `GET /sage/oauth2/auth/central` 302s to a
  **registered** `redirect_uri` with a `code` (client `fake-sage-client`);
  `POST /sage/oauth2/token` takes `client_id` / `client_secret` as **form fields**
  (Sage's shape — HTTP Basic is refused), makes each code single-use and bound to
  its `redirect_uri`, and rotates refresh tokens; `GET /sage/v3.1/businesses` (bearer
  must be one it issued) lists the one business, `fake-sage-business`. Same
  `FakeOAuthApp` as Xero's.
- Test hook `POST /sage/v3.1/__set-status {"id", "status",
  "outstanding_amount"?}`. `POST /__reset` clears this state too.

## Blackbaud Financial Edge NXT — SKY API (`/blackbaud`)

Backs `erp_adapters/blackbaud_fe_nxt.py`
(`FEOH_ERP_BLACKBAUD_API_BASE=http://localhost:12112/blackbaud`,
`FEOH_ERP_BLACKBAUD_TOKEN_URL=http://localhost:12112/blackbaud/oauth2/token`).
Every API route requires **both** a bearer the token endpoint issued and a
non-empty `Bb-Api-Subscription-Key`, as the real gateway does.

- OAuth (the backend's real connect flow, same `FakeOAuthApp` as Xero / Sage):
  `GET /blackbaud/oauth2/authorization` 302s to a **registered** `redirect_uri`
  with a `code` (client `fake-blackbaud-client`). `POST /oauth2/token` takes
  `fake-blackbaud-client:fake-blackbaud-secret` by HTTP Basic, makes each code
  single-use and bound to its `redirect_uri`, rotates refresh tokens, and answers
  in the SKY shape: the bearer plus `environment_id` `p-fake-env-1`,
  `legal_entity_id` and `refresh_token_expires_in` (the backend takes the
  environment from the token response — there is no lookup call).
- `GET /accountspayable/v1/vendors` (`136`, `137` Active, `138` `Inactive`,
  `139` `OnHold` — `vendor_status`), `GET /accountspayable/v1/vendors/{id}`
  (the `VendorDetail`), `/purchaseorders` (`1001` 1250.00 open, `1002` 980.50
  closed, `1003` with no `order_total`; each carries `vendor_id`),
  `GET /generalledger/v1/accounts` (`01-5000-00`, `01-5100-00`, `01-2000-00`,
  and `01-5900-00`, whose job fails) — `{"count", "value"}` with `limit` /
  `offset` (limit capped at 500).
- `GET /generalledger/v1/fiscalyears` — calendar years 2024–2030: FY 2024
  `Closed`, Jan–Mar 2025 periods `closed`, everything else open.
- `GET /accountspayable/v1/invoices?search_text=` — matches invoice number,
  description, PO number or vendor name, as SKY documents; `from_date` /
  `to_date` keep invoices dated strictly after / before (the narrower reading
  of "after" / "before"). `POST /accountspayable/v1/invoices/process` →
  `{"process_id"}`; rejects (400, `{"Error", "Details"}`) an unknown or
  `Inactive` vendor, a `post_date` in a closed period or year, an unknown
  account, a split set not totalling 100%, and distributions whose Debits and
  Credits don't both equal `amount`. The job is complete at once:
  `backgroundProcess/{id}/status` → 5, `backgroundProcess/{id}/result` →
  `{"record_id"}` — except a create touching `01-5900-00`, whose job ends at 7
  (failed) with no record.
  `GET /accountspayable/v1/invoices/{id}` returns `status` / `amount` / `balance`.
- `POST /blackbaud/__set-status` — test hook `{"invoice_id", "status", "balance"}`.
- `POST /blackbaud/__throttle` — test hook `{"status": 429|403, "retry_after": <s>,
  "count": <n>}`: the next `n` SKY calls answer SKY's rate-limit 429 or
  quota 403, with `Retry-After`. Drives the adapter's call budget locally
  (`services/erp_call_budget.py`, decisions §322): after one quota 403 every
  tenant on the platform key is refused before its first call until
  `Retry-After`, and the org's admins get one `erp_call_quota` notice.
  `count: 0` clears it; so does `POST /__reset`.
- `POST /blackbaud/__seed-invoices` — test hook `{"rows": [{"vendor_id",
  "invoice_number", "invoice_date", "description"?, "status"?}]}`: invoices
  keyed by hand in FE NXT (e.g. 600 rows a common `search_text` matches).


## Business Central + NetSuite syncs and void

- **D365** `GET …/accounts` — `6100` / `6200` / `6300` Posting accounts (ids
  `a6100000-…`), plus heading `6000` and blocked `6900` (the adapter skips
  both). `GET …/purchaseOrders` (lines with `$expand=purchaseOrderLines`) —
  `PO-FAKE-BC-401` 1500.25, blank `currencyCode`, blank date `0001-01-01`;
  `PO-FAKE-BC-402` 820.00 EUR. Every collection honours
  `Prefer: odata.maxpagesize` and returns `@odata.nextLink` (`$skiptoken`).
  `DELETE …/purchaseInvoices({id})` needs `If-Match` and deletes only a `Draft`.
  A purchaseInvoice `Account` line must carry a posting account's `accountId`.
- **NetSuite** SuiteQL also answers `SELECT … FROM vendor` (`25`, `26`, `28`,
  and inactive `27`) and `SELECT … FROM transaction … type = 'PurchOrd'`
  (`PO-FAKE-NS-501` 2100.50 USD open, vendor `25`; `PO-FAKE-NS-502` 640.00 GBP
  closed, vendor `26` — each row's `vendorid` is the `t.entity` id).
  `DELETE /vendorBill/{id}` → 204 for a `pendingApproval` bill only
  (`/__set-status` with `"pendingApproval"`).

## QuickBooks Online (`/qbo`)

**Validation Faults** (off until a spec turns them on): `POST
/__qbo/validation` with any of `{"closed_through": "YYYY-MM-DD",
"inactive_vendors": ["57"], "inactive_accounts": ["63"],
"duplicate_doc_check": true}` (keys left out keep their value; `/__reset`
turns all off). A bill create is then refused with HTTP 400 and the Fault code
Intuit documents: `2500` Invalid Reference Id for an inactive vendor or account,
`6210` Account Period Closed for a `TxnDate` on or before `closed_through`,
`6140` Duplicate Doc Num for a DocNumber another bill of the same vendor holds.
Each Fault's `Detail` quotes the bill back, as Intuit's does, so a spec can
prove the adapter never reads it. `GET /qbo/v3/company/{realm}/vendor/{id}` and
`…/account/{id}` return the fixture with its `Active` flag (610 for an unknown
id). State: `STATE["qbo_validation"]`.

`GET /qbo/oauth2/authorize` 302s straight back to `redirect_uri` with a `code`
and `realmId` — but only when `redirect_uri` exactly matches a registered URI,
as Intuit requires; anything else is a 400 `invalid_request`, so the fake is
never an open redirect. The registered set defaults to
`http://{localhost,127.0.0.1}:{8000-8009}/api/erp/oauth/callback` (the
backend's ERP OAuth callback on the dev API port and the worktree offsets);
set `FAKE_ERP_OAUTH_REDIRECT_URIS` (comma-separated) to replace it. The same
registered set guards the Xero authorize route.

**Status sync-back and BillPayment write-back** (decisions §295): bills carry
`LinkedTxn` and `MetaData.LastUpdatedTime` like QuickBooks'. A payment is
linked on its bill with the `TxnType` real QuickBooks uses — `BillPaymentCheck`
for a `Check` (bank) payment, `BillPaymentCreditCard` for a `CreditCard` one —
never `BillPayment` (decisions §317).
`POST /qbo/v3/company/{realm}/billpayment?requestid=…` creates a BillPayment
against the bill its `Line[0].LinkedTxn` names (a repeated `requestid` replays
the first answer, as Intuit does; more than the bill's open balance is a 400
Fault `6000`), and `…?operation=update&include=void` voids one (sparse,
`SyncToken` checked; the bill's balance comes back). `GET …/billpayment/{id}`
reads one (610 when unknown). `GET …/cdc?entities=Bill,BillPayment&changedSince=…`
answers the bills and payments changed since that time, deleted bills as
`{"Id", "status": "Deleted"}`. Test hooks that play a person in QuickBooks:
`POST /__qbo/pay-bill {"id", "amount"?, "pay_type"?: "Check"|"CreditCard",
"txn_date"?}` (no FeohLedger marker on it), `POST /__qbo/delete-bill {"id"}`,
and `POST /__qbo/token-lifetime {"seconds"}` (the `x_refresh_token_expires_in`
later token responses carry, to stand a connection near its expiry).
The Bill, BillPayment, void and CDC records are built in `qbo_records.py`
(stdlib only), which `backend/tests/test_qbo_recorded_replay.py` compares
against answers recorded from a real QuickBooks sandbox company
(`fixtures/qbo_recorded/`, written by
`backend/scripts/record_qbo_contract_fixtures.py`; decisions §318): a field the
status sync reads that QuickBooks sends and the fake does not, or sends with
another type or link type, fails the backend suite. Change those shapes there,
never inline in `app.py`.
State: `STATE["qbo"]` (`payments`, `deleted_bills`, `refresh_lifetime`).
Bill and BillPayment Ids come from one sequence that `POST /__reset` does NOT
restart and that starts from the clock, so a restarted container does not
restart it either: QuickBooks never reuses a transaction Id within a company,
and an e2e tenant keeps the invoices earlier runs posted, so a restarted
sequence would hand a new bill an Id an old invoice already names.

## QuickBooks Online sandbox company (`/qbo`, `fake_company=sandbox`)

The Intuit sample company FeohLedger was tested against live on 2026-10-19
(#598), replayed: 91 accounts and 29 vendors recorded in
`fixtures/qbo_sandbox_company.json`, served by `qbo_sandbox.py`. The same JSON
feeds the backend's in-process replay (`backend/tests/test_qbo_sandbox_replay.py`)
and `frontend/tests-e2e/erp/quickbooks-online-sandbox.spec.ts`. Never edit its
`live_sync` block to make a test pass: it is what the live syncs answered.

- **Selecting it**: append `fake_company=sandbox` to the authorize URL (the
  company picker on Intuit's consent screen). The 302 carries realm
  `fake-sandbox-realm`, and the tokens issued for it work on that realm only —
  the default company (`fake-realm-1`) is untouched, so its specs and this one
  never see each other's data. Any other `fake_company` value is a 400.
- **Query semantics**: `select * from Account|Vendor` returns ACTIVE records
  only, as QuickBooks does — `where Active IN (true,false)` or
  `where Active = false` reaches the inactive ones (Bank Charges, Id 8; Squeaky
  Kleen Car Wash, Id 57). `STARTPOSITION` / `MAXRESULTS` page (default 100, max
  1000); an unknown clause is `QueryParserError`, never ignored. `PurchaseOrder`
  is empty (the live POs were not recorded).
- **Bills**: create (with `requestid` replay), read, `operation=delete`. The
  vendor and every line account must be an existing, ACTIVE record of the
  company, else 400 Fault `2500` "Invalid Reference Id".
- **Tokens**: access tokens expire after their `expires_in` and are then
  refused 401 Fault `3200`; refresh tokens rotate (a spent one is
  `invalid_grant`); `POST /qbo/oauth2/revoke` (JSON or form `token`) ends the
  grant and is recorded.

Its state is its own: the global `POST /__reset` does not clear it. Test hooks,
all under `/qbo/_admin/sandbox/`:

- `POST reset` — clear connections, bills and counters.
- `POST tokens {"access_ttl": seconds}` — the `expires_in` of every access
  token issued from now on (under the backend's 120 s margin, each use refreshes
  first).
- `POST expire-access` — every access token issued so far is expired; the next
  call is a 401 the backend must answer with a refresh. Refresh tokens stay good.
- `POST revoke-consent` — the user removed the app on Intuit's side; the next
  refresh is `invalid_grant`.
- `GET stats` → `{"realm", "refreshes", "rejected", "refresh_tokens", "revocations"}`
  (fake tokens only).
- `GET bills/{id}` — a bill as the company holds it, with no bearer.
