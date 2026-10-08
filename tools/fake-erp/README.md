# fake-erp

A small, deterministic, in-memory fake of the three real ERP providers the
backend integrates with, so the real adapters in
`backend/app/services/erp_adapters/` can be exercised end-to-end with no
cloud account and no credentials. One FastAPI process, three path-prefixed
surfaces:

| Prefix | Emulates | Adapter |
|---|---|---|
| `/merge/api/accounting/v1` | Merge.dev unified accounting API | `merge_dev.py` |
| `/netsuite/services/rest/record/v1` | NetSuite SuiteTalk REST (OAuth 1.0 TBA) | `netsuite.py` |
| `/d365` | Dynamics 365 Business Central OData v4 | `dynamics_365_bc.py` |

## Auth (fails 401 in each provider's error shape)

- **Merge**: any non-empty `Authorization: Bearer …` **and** a non-empty
  `X-Account-Token` header.
- **NetSuite**: an `Authorization: OAuth …` header containing
  `oauth_consumer_key`, `oauth_token` and `oauth_signature` params. The HMAC
  signature is **not** verified — shape only.
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
  `1002`, …) in the `Location` header, status `Open`;
  `GET /vendorBill/{id}` → `{"status": {"refName": "Open"}}`;
  `GET /vendor?limit=1` (test_connection);
  `POST /netsuite/services/rest/query/v1/suiteql` (requires
  `Prefer: transient`; answers only `SELECT … FROM account`, paged by
  `limit`/`offset`/`hasMore`) — the chart sync.
- **D365**: `POST …/companies({id})/purchaseInvoices` → 201 `d365-inv-<n>`
  status `Draft`; `POST …/purchaseInvoices({id})/Microsoft.NAV.post` → 204,
  flips status to `Open`; `GET …/purchaseInvoices({id})`;
  `GET …/vendors?$top=1` (test_connection). OData base is `/d365`, i.e.
  `/d365/{environment}/api/v2.0/companies({company_id})/<resource>`.

## Fixed fixtures (e2e asserts these literals — do not change)

Purchase orders (`GET /merge/api/accounting/v1/purchase-orders`):

1. `PO-FAKE-301` — vendor "Fake ERP Vendor A", total 1250.00 USD
2. `PO-FAKE-302` — vendor "Fake ERP Vendor B", total 980.50 USD
3. `PO-FAKE-303` — vendor "Fake ERP Vendor A", total 4400.00 USD

GL accounts (`GET /merge/api/accounting/v1/accounts`):

1. `6100` "Fake Office Supplies" (expense)
2. `6200` "Fake Software" (expense)
3. `6300` "Fake Consulting" (expense)

Vendors (`GET /merge/api/accounting/v1/vendors`):

1. "Fake Merge Vendor Co" — Net 30, tax id `71-1234567`
2. "Fake Merge Supply Co" — Net 45, tax id `72-2345678`
3. "Fake Merge Services Co" — Net 60 (`payment_term` as a bare string, not an
   object — exercises that branch of `_merge_vendor_to_payload`), tax id
   `73-3456789`

### References are enforced by id, as the real ERPs do

A bill naming its vendor or account by anything but a known id is a **400**, so
the e2e suite proves the adapters post the ids the syncs stored
(`backend/docs/erp-integration.md` § ERP references):

- **Merge** `POST /invoices`: `contact` must be a fixture vendor id
  (`merge-vendor-701` …); a non-null line `account` must be a fixture account id
  (`merge-acct-6100` …).
- **NetSuite** `POST /vendorBill`: `entity.id` must be a vendor id (`25`, `26`);
  the `expense` sublist must be non-empty with each `account.id` an account id;
  an `item` sublist is refused.
- **D365** `POST …/purchaseInvoices`: `vendorId` (or `vendorNumber`) must name a
  fixture vendor.

NetSuite vendors (`GET /vendor`): `25` "Fake NetSuite Vendor A", `26` "Fake
NetSuite Vendor B". NetSuite accounts (SuiteQL): `120` → `6100`, `121` →
`6200`, `122` → `6300`. D365 vendor (`GET …/vendors`): id
`5d115c9c-44e3-ea11-bb43-000d3a2feca1`, number `V0001`, "Fake BC Vendor A".
Each provider's vendor names are distinct, so one e2e tenant syncing all three
never links one provider's vendor id onto another's vendor row.

## Test hooks

- `GET /health` → `{"status": "ok"}`
- `POST /__reset` → clears all in-memory state (counters + stored invoices)
- `POST /__set-status` `{"provider": "merge"|"netsuite"|"d365", "id": "…",
  "status": "…"}` → force a stored invoice into a provider-native status
  (e.g. merge `PAID`, netsuite `paidInFull`, d365 `Paid`) to drive
  `get_invoice_status()` transitions.

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

Standalone (any venv with fastapi + uvicorn):

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
  `0000002`), `GenMaster` (`6100`, `6200` expense, `2000` liability),
  `PorMasterHdr` / `PorMasterDetail` (`PO-SYS-501` 1250.00 open,
  `PO-SYS-502` 980.50 complete) and the posted `ApInvoice` rows; `EQ`
  expressions and `ReturnRows`.
- `Transaction/Post?BusinessObject=APSTIN` — rejects an unknown supplier or
  ledger code, a duplicate (supplier, invoice) and a distribution that does not
  balance to `InvoiceAmount`.
- `POST /syspro/SYSPROWCFService/Rest/__set-balance` — test hook
  `{"supplier", "invoice", "balance"}` (`"0"` = paid).
