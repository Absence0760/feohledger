# ERP Integration

## Overview

The AP system integrates with external ERP systems to post approved invoices into the ERP's accounts payable ledger. Once the ERP confirms the invoice is posted, the invoice becomes payable.

### Two Integration Methods

| Method | How it works | When to use |
|---|---|---|
| **Merge.dev (default)** | Unified API that normalizes across all ERPs. One integration, all 10 ERPs. | Default for all customers. Fastest to set up. |
| **Direct adapter** | Custom adapter per ERP with direct API calls. | When you need deep control, custom field mapping, or can't use Merge.dev. |

Both methods use the same adapter interface. Per-organization config in **Organization > ERP Integration** determines which method is used. Merge.dev is the default; direct adapters exist for Business Central and NetSuite, with mock adapter for development.

### Implementation Status

| Adapter | Status | File |
|---|---|---|
| Mock (dev/testing) | Implemented | `erp_adapters/mock_adapter.py` |
| Merge.dev (all ERPs) | Implemented | `erp_adapters/merge_dev.py` |
| Business Central | Implemented | `erp_adapters/dynamics_365_bc.py` |
| NetSuite | Implemented | `erp_adapters/netsuite.py` |
| SAP, Epicor, others | Use Merge.dev | — |

## Supported ERPs

| # | ERP | API Style | Auth Method |
|---|---|---|---|
| 1 | Microsoft Dynamics 365 Business Central | REST (OData v4) | OAuth 2.0 |
| 2 | SAP S/4HANA | REST (OData) / RFC | OAuth 2.0 / X.509 |
| 3 | Oracle NetSuite | REST / SuiteTalk SOAP | Token-Based Auth (TBA) |
| 4 | Epicor Kinetic | REST | API Key / OAuth 2.0 |
| 5 | Acumatica Cloud ERP | REST | OAuth 2.0 |
| 6 | Sage X3 | REST | Basic Auth / API Key |
| 7 | Infor CloudSuite Industrial | REST (ION API) | OAuth 2.0 (Infor ION) |
| 8 | QAD Adaptive | REST | OAuth 2.0 |
| 9 | Cetec ERP | REST | API Key |
| 10 | DELMIAWorks | REST / SOAP | API Key / Basic Auth |

## Architecture

### Adapter Pattern

Each ERP has a dedicated adapter that implements a common interface. The workflow engine calls the adapter without knowing which ERP it's talking to.

```
Workflow Engine
    |
    v
ERP Dispatcher (reads org config → picks adapter)
    |
    ├── Business Central Adapter
    ├── SAP S/4HANA Adapter
    ├── NetSuite Adapter
    ├── Epicor Adapter
    ├── Acumatica Adapter
    ├── Sage X3 Adapter
    ├── Infor Adapter
    ├── QAD Adapter
    ├── Cetec Adapter
    └── DELMIAWorks Adapter
```

### Adapter Interface

Every adapter implements:

```python
class ErpAdapter:
    """Base interface for ERP integrations."""

    async def post_invoice(self, invoice: InvoicePayload) -> ErpPostResult:
        """Send an invoice to the ERP. Returns the ERP document ID."""
        ...

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        """Poll the ERP for the current status of a posted invoice."""
        ...

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Request cancellation of a posted invoice. Returns success."""
        ...

    async def list_pos(self) -> list[PoPayload]:
        """Pull purchase orders from the ERP for the PO management UI.

        Default returns []. Adapters that don't implement PO sync
        cause `POST /api/purchase-orders/sync-erp` to no-op rather
        than 500 — currently overridden by `mock` and `merge_dev`.

        `PoPayload.currency` is the ISO code the ERP record states
        (Merge's unified PurchaseOrder carries `currency`; the mock
        states `USD`). None when the record has none — never a
        default. The sync stores it on `PurchaseOrder.currency`
        through `po_currency_code`; on a re-sync a stated code wins
        (it is the other half of `total`) and an absent one never
        erases a recorded code (decisions §197).
        """
        ...

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Pull the chart of accounts for `POST /api/gl-accounts/sync-erp`.

        Same default-empty pattern as `list_pos`. Currently overridden
        by `mock` (canonical 20-row demo chart) and `merge_dev` (best-
        effort against `/accounts`, paginated, classification-mapped).
        Drives the Auto GL Coding pipeline — the chart-of-accounts
        prompt injection only constrains AI suggestions when the org
        has actually synced rows.
        """
        ...

    async def list_vendors(self) -> list[VendorPayload]:
        """Pull vendors from the ERP for `POST /api/vendors/sync-erp`.

        Same default-empty pattern as `list_pos` / `list_gl_accounts`.
        Implemented by all three real adapters — `merge_dev` (best-effort
        against the unified `/vendors`, paginated), `netsuite` (`GET
        /vendor`), and `dynamics_365_bc` (`GET .../vendors`) — plus `mock`.
        Covered by the local fake-erp e2e suite (`pnpm test:erp`); see
        "Local e2e testing" below.
        """
        ...

    async def test_connection(self) -> bool:
        """Verify the ERP connection is working."""
        ...
```

### Data Types

```python
@dataclass
class InvoicePayload:
    """Normalized invoice data sent to every ERP adapter."""
    correlation_id: str          # Idempotency key
    invoice_number: str
    vendor_name: str
    vendor_tax_id: str | None
    amount: Decimal
    currency: str
    invoice_date: date | None
    due_date: date | None
    po_number: str | None
    description: str | None
    subtotal: Decimal | None
    tax_amount: Decimal | None
    tax_rate: Decimal | None
    discount_amount: Decimal | None
    shipping_amount: Decimal | None
    gl_account: str | None
    cost_center: str | None
    payment_terms: str | None
    payment_method: str | None
    bill_to_address: str | None
    remit_to_address: str | None
    line_items: list[LineItemPayload]

@dataclass
class LineItemPayload:
    line_number: int
    item_code: str | None
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    gl_account: str | None

@dataclass
class ErpPostResult:
    success: bool
    erp_document_id: str | None    # ERP's internal ID for the posted invoice
    erp_document_number: str | None # Human-readable document number
    message: str | None
    raw_response: dict | None

@dataclass
class VendorPayload:
    """Normalized vendor record returned by `ErpAdapter.list_vendors`.

    `erp_vendor_id` and `name` are the only fields a real ERP is guaranteed
    to supply; the rest are optional, and `services.vendor_sync.sync_vendors_from_erp`
    never nulls out an existing local value for a field a given pull omits.
    """
    erp_vendor_id: str
    name: str
    code: str | None
    email: str | None
    phone: str | None
    address: str | None
    tax_id: str | None
    payment_terms: str | None

class ErpInvoiceStatus(str, Enum):
    draft = "draft"           # Created but not posted
    open = "open"             # Posted to AP ledger — payable
    partially_paid = "partially_paid"
    paid = "paid"
    cancelled = "cancelled"
    unknown = "unknown"
```

## Invoice Status Flow with ERP

```
approved
    |
    v
sending_to_erp          (adapter.post_invoice() called)
    |
    ├── success ──> sent_to_erp    (ERP accepted, document ID stored)
    |                   |
    |                   v
    |               posted_in_erp  (ERP confirmed posting — webhook or poll)
    |                   |
    |                   v
    |               payment_scheduled  (payment date set)
    |                   |
    |                   v
    |               paid            (payment executed and confirmed)
    |                   |
    |                   v
    |               done            (fully complete)
    |
    └── failure ──> failed          (retry available)
```

### Status Sync: Webhook vs Polling

After sending an invoice to the ERP, the system needs to know when the ERP has posted it.

| Method | How it works | When to use |
|---|---|---|
| **Webhook** | ERP calls our endpoint when status changes | Preferred — real-time, no polling cost |
| **Polling** | Background job checks ERP for status every N minutes | Fallback for ERPs without webhook support |

**Webhook endpoint:** `POST /api/erp/webhook/{erp_type}`

The webhook handler:
1. Validates the request (signature, API key, or IP whitelist)
2. Dedupes by `event_id` (`services/webhook_security.is_event_already_processed`)
3. Looks up the invoice by `correlation_id` or `erp_document_id`
4. Maps the ERP status to our internal status
5. Transitions the invoice (e.g., `sent_to_erp` → `posted_in_erp`)
6. Writes an audit log entry

**The ERP paying an invoice FeohLedger never paid is recorded, not dropped.**
`posted_in_erp → paid` is not a state-machine edge (a `paid` invoice needs a
payment behind it), so a `Paid` report for an `approved` / `sent_to_erp` /
`posted_in_erp` invoice used to be a silent no-op — which left the ERP-led path
of the no-rail pilot (issue #517) with no way to reach `paid`. The handler now
records the ERP's payment through `services/external_payment` (a `completed`
`provider="external"` payment, reference = the ERP document id), moving a
`sent_to_erp` invoice to `posted_in_erp` first. A refusal — an open
payment-blocking exception, a live FeohLedger payment or card — opens an
`erp_reconciliation` exception naming the refusal code instead. See
`backend/docs/payments.md` § The ERP reports an invoice paid that FeohLedger never paid.

**Dedup key is `event_id` only — no fallback to `erp_document_id` /
`correlation_id`.** Both of those are constant for an invoice's entire
lifecycle, so falling back to either would let the FIRST status delivery's
dedup claim silently swallow every LATER, genuinely distinct status event
for the same invoice (e.g. `posted_in_erp` claims the key, and the next
day's real `paid` event never reaches `transition_invoice`). A direct
integration that omits a per-delivery `event_id` instead hits
`is_event_already_processed`'s own "missing event id → always process"
path, which is the correct trade-off: no dedup at all beats false dedup on a
different event.

**Polling job:** Runs every 5 minutes for invoices in `sent_to_erp` status older than 1 minute:
1. Calls `adapter.get_invoice_status(erp_document_id)`
2. If status changed, transitions the invoice
3. If stuck for > 30 minutes, marks as `failed` with timeout reason

## Organization ERP Configuration

Stored in `Organization.settings` JSONB under the key `erp`:

```json
{
  "erp": {
    "type": "dynamics_365_bc",
    "environment": "production",
    "company_id": "cronus-us",
    "base_url": "https://api.businesscentral.dynamics.com/v2.0",
    "auth": {
      "method": "oauth2",
      "tenant_id": "...",
      "client_id": "...",
      "client_secret": "...",
      "scope": "https://api.businesscentral.dynamics.com/.default"
    },
    "field_mapping": {
      "vendor_id_field": "vendorNumber",
      "gl_account_field": "accountId"
    },
    "webhook_secret": "..."
  }
}
```

The ERP type determines which adapter is used.

### Credentials at rest

Every credential in `settings.erp` is stored encrypted, field by field, inside
the JSONB: each catalogue field marked `secret` (`catalog.SECRET_KEYS`, which
includes the inbound webhook key) and the OAuth block's `access_token` /
`refresh_token`. A stored value reads `enc:v1:<key_id>:<base64>` — AES-256-GCM
with a fresh nonce, and the field name (`erp.client_secret`,
`erp.oauth.refresh_token`) bound as associated data, so a ciphertext moved to
another field, or altered by one byte, fails to decrypt instead of yielding
garbage (`app/utils/credential_crypto.py`). Ids, base URLs and the OAuth
block's `connection_id` / `org_id` stay readable, so routing, masking, the plan
gate and the realm lookup need no key.

| Direction | The one place | Module |
|---|---|---|
| Settings save (`PATCH /organization`) | after `catalog.merge_erp_update` | `api/organization._encrypt_erp_or_refuse` |
| OAuth connect | `erp_oauth.new_connection_block` | `services/erp_oauth` |
| OAuth refresh | the compare-and-swap write | `services/erp_oauth._compare_and_swap` |
| To an adapter | `get_erp_adapter` decrypts the top-level secrets | `erp_adapters/dispatcher` |
| OAuth tokens / tenant app secret | `_stored_block`, `revoke`, `resolve_client_credentials`, `token_headers` | `services/erp_oauth` |
| Inbound ERP webhook | the HMAC key | `api/erp_webhook` |

The masked read (`catalog.mask_erp_config`) never decrypts. An adapter's config
carries the OAuth block still encrypted: adapters never read tokens, they ask
`erp_oauth.get_access_token`, which reads the stored row.

**Keys.** `FEOH_CREDENTIAL_ENCRYPTION_KEYS` is a keyring (first entry encrypts,
all decrypt), a sops secret in deployed envs, with a non-secret dev key
committed in `.env.development` (`docs/environment.md`). **No keyring → fail
closed everywhere:** a save carrying a secret answers 503 and stores nothing; a
stored ciphertext cannot be read (`/test-erp` says so, a push fails with
`ErpCredentialUnreadableError`, the webhook drops the event). A stored value
that does not decrypt is never marked `needs_reconnect` — the grant may be fine;
the keyring is not. Rotation: `docs/secrets-rotation.md` § ERP credential
encryption keyring.

**Legacy plaintext.** A value without the `enc:v1:` prefix is read as-is, and
migration `0110_erp_credentials_encrypted` encrypts every one on the control
plane (it refuses to run while plaintext exists and no keyring is set). A save
or a refresh also re-writes the values it touches encrypted.

### Secrets are write-only

No response carries an ERP secret, admin included:

- **Read.** `GET /api/organization` returns every key in
  `erp_adapters/catalog.SECRET_KEYS` (each catalogue field marked `secret`, plus
  `webhook_signing_secret` / `webhook_secret`) as `********` when set and `""`
  when not, and replaces `erp.oauth` (the token block `services/erp_oauth`
  writes) with `{"connected": bool}`. Non-admins still see only
  `integration_method`.
- **Write.** `PATCH /api/organization` runs `catalog.merge_erp_update`: a secret
  sent blank, as `********`, or omitted keeps the stored value **while the ERP
  selection (`type` + routing) and its destination are unchanged**. Switching
  ERP never carries one ERP's secret into another's field of the same name, and
  changing any of `catalog.DESTINATION_KEYS` (`base_url`, `tenant_id`,
  `account_id`, `company_id`, `environment` — what an adapter builds its host or
  target books from) is a new connection: every outbound secret must be typed
  again, so a stored password can never be pointed at a host the save just
  named. Only the inbound webhook HMAC key (`webhook_signing_secret`), which is
  never sent anywhere, survives a destination change. An explicit `null` clears
  a secret. `erp.oauth` is never taken from the body, so the OAuth callback stays
  its only writer, and even `{"erp": null}` keeps it (only the OAuth disconnect
  removes it). Every change writes `organization.erp_updated` (changed key
  names, `type`, `integration_method`, never a value) before the save commits;
  if that row can't be written the save is a `503` and nothing changes.
- **Test.** `POST /api/organization/test-erp` with an unsaved form config fills
  each masked or blank secret from the stored config the same way — and under
  the same destination rule — so "Test connection" works on a form that shows
  only masks but cannot send a stored secret to a new `base_url`.

## Provider catalogue

`backend/app/services/erp_adapters/catalog.py` is the single source of truth for
the Organization → ERP form, served at `GET /api/organization/erp/providers`
(admin). Each entry carries `key`, `label`, `regions` (`US`, `ZA`), `auth`
(`credentials` | `oauth`), `fields` (`name`, `label_key` — a frontend i18n
`MessageKey`, `secret`, `required`, optional `placeholder` / `help_key` /
`options`) and `docs_url`; the endpoint adds `available` (adapter registered in
this build). The response also carries `merge_dev_long_tail` (the ERPs offered
inside the "Other ERP via Merge.dev" choice, Scale plan per `docs/decisions.md`
§256) and `secret_mask`.

| Key | ERP | Regions | Auth |
|---|---|---|---|
| `quickbooks_online` | QuickBooks Online | US | OAuth (optional BYO app; `environment`) |
| `xero` | Xero | US, ZA | OAuth (optional BYO app; `bill_status`, `default_tax_type`) |
| `sage_accounting` | Sage Business Cloud Accounting (v3.1: US, UK, IE, CA) | US | OAuth (optional BYO app) |
| `sage_accounting_za` | Sage Business Cloud Accounting (South Africa) | ZA | API key + Sage login + company |
| `blackbaud_fe_nxt` | Blackbaud Financial Edge NXT | US | OAuth (optional BYO app + SKY API subscription key) |
| `sage_intacct` | Sage Intacct | US, ZA | REST API client credentials + company / user |
| `netsuite` | Oracle NetSuite | US, ZA | TBA (OAuth 1.0a) tokens |
| `dynamics_365_bc` | Business Central | US, ZA | Azure AD client credentials |
| `syspro` | SYSPRO | ZA | Operator + company (https base URL) |
| `merge_dev` | Other ERP via Merge.dev | — | Merge API key + account token |

`catalog.PENDING_ADAPTERS` lists catalogue keys whose adapter has not landed;
`tests/test_erp_catalog.py` fails if a catalogue key is neither registered nor
pending, if a registered direct adapter has no catalogue entry, or if a
`label_key` / `help_key` is missing from the frontend's English catalogue.

## Per-ERP Integration Details

### 1. Microsoft Dynamics 365 Business Central

**API:** OData v4 REST API
**Base URL:** `https://api.businesscentral.dynamics.com/v2.0/{tenant}/{environment}/api/v2.0`
— an admin `base_url` (optional; blank = `https://api.businesscentral.dynamics.com/v2.0`)
must be **https on `api.businesscentral.dynamics.com`**, then passes the SSRF guard too. Any
other scheme, host, userinfo, port or query raises `BusinessCentralConfigError` naming
`'base_url'` only — the bearer token for the customer's whole BC tenant rides on every
request, so a public look-alike host the SSRF guard would pass is a credential leak.
`post_invoice` refuses it non-retryably before the token exchange. The operator override
`FEOH_ERP_D365_API_BASE` is exempt.
**Auth:** OAuth 2.0 client credentials

**Create Purchase Invoice:**
```
POST /purchaseInvoices
{
  "vendorId": "5d115c9c-44e3-ea11-bb43-000d3a2feca1",
  "invoiceDate": "2026-04-01",
  "dueDate": "2026-05-01",
  "vendorInvoiceNumber": "INV-2024-001",
  "externalDocumentNumber": "<correlation_id>",
  "purchaseInvoiceLines": [
    {
      "lineType": "Account",
      "accountId": "a6100000-0000-0000-0000-000000006100",
      "quantity": 1,
      "unitCost": 250.00
    }
  ]
}
```

**Post (finalize):** `POST /purchaseInvoices({id})/Microsoft.NAV.post`

**Status field:** `status` — `Draft`, `Open` (posted), `Paid`, `Canceled`

**Webhook support:** Business Central supports webhooks via subscriptions API:
```
POST /subscriptions
{
  "resource": "purchaseInvoices",
  "notificationUrl": "https://your-app.com/api/erp/webhook/dynamics_365_bc",
  "changeType": "updated"
}
```

**Key considerations:**
- Must create vendor in BC first or map to existing vendor number
- `purchaseInvoiceLines` require item numbers or GL accounts
- Posting the invoice is a separate step from creating it
- The `correlation_id` should be stored in `externalDocumentNumber`

---

### 2. SAP S/4HANA

**API:** OData REST or BAPI/RFC via SAP Gateway
**Base URL:** `https://{host}/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV`
**Auth:** OAuth 2.0 or X.509 certificate

**Create Supplier Invoice:**
```
POST /A_SupplierInvoice
{
  "CompanyCode": "1000",
  "FiscalYear": "2026",
  "SupplierInvoiceIDByInvcgParty": "INV-2024-001",
  "InvoicingParty": "VENDOR001",
  "DocumentDate": "2026-04-01",
  "InvoiceGrossAmount": "1500.00",
  "DocumentCurrency": "USD",
  "to_SupplierInvoiceItemGLAcct": [...]
}
```

**Status:** Tracked via `SupplierInvoiceStatus` — `1` (open), `2` (cleared/paid), `3` (blocked)

**Webhook support:** SAP Event Mesh or custom ABAP webhook. More commonly polled.

**Key considerations:**
- Requires company code and fiscal year
- GL account assignments via sub-entity `to_SupplierInvoiceItemGLAcct`
- Tax handling via tax codes, not raw amounts
- Blocking reasons must be handled (payment block, verification block)
- Heavy field validation — SAP is strict about data formats

---

### 3. Oracle NetSuite

**API:** REST API or SuiteTalk SOAP
**Base URL:** `https://{account_id}.suitetalk.api.netsuite.com/services/rest/record/v1`
— `account_id` must match `^[A-Za-z0-9_-]+$` (production `1234567`, sandbox `1234567_SB1`)
before it reaches the hostname or the OAuth `realm`. Before this, `evil.tld/x?` sent every
signed request to evil.tld. A bad id raises `NetSuiteConfigError` naming `'account_id'` only;
`post_invoice` turns it into a non-retryable failure before any request.
**Auth:** Token-Based Authentication (TBA) — OAuth 1.0 style

**Create Vendor Bill:**
```
POST /vendorBill
{
  "entity": { "id": "123" },
  "tranId": "INV-2024-001",
  "tranDate": "2026-04-01",
  "dueDate": "2026-05-01",
  "currency": { "refName": "USD" },
  "item": {
    "items": [
      {
        "item": { "id": "456" },
        "quantity": 10,
        "rate": 25.00,
        "account": { "id": "789" }
      }
    ]
  }
}
```

**Status:** `approvalStatus` — `1` (pending), `2` (approved). `status` — `Open`, `Paid In Full`, `Voided`

**Webhook support:** SuiteScript User Event Scripts or RESTlets for callbacks. SuiteTalk also supports Saved Search polling.

**Key considerations:**
- Uses internal IDs for everything (vendors, items, accounts) — need a mapping layer
- Multi-subsidiary support requires subsidiary field
- Custom fields via `customFieldList`
- Rate limiting: 10 concurrent requests per account

---

### 4. Epicor Kinetic

**API:** REST (Epicor Functions / BAQs / BO Methods)
**Base URL:** `https://{server}/api/v2/odata/{company}`
**Auth:** API Key or OAuth 2.0

**Create AP Invoice:**
```
POST /Erp.BO.APInvoiceSvc/APInvoices
{
  "Company": "EPIC01",
  "VendorNum": 1234,
  "InvoiceNum": "INV-2024-001",
  "InvoiceDate": "2026-04-01",
  "InvoiceAmt": 1500.00,
  "APInvDtl": [
    {
      "VendorNum": 1234,
      "InvoiceLine": 1,
      "GLAccount": "6100",
      "ExtCost": 1500.00
    }
  ]
}
```

**Status:** `OpenPayable` (true/false), `InvoiceStatus` — varies by customization

**Webhook support:** Epicor Functions can trigger outbound HTTP calls on business events.

**Key considerations:**
- Uses Business Object (BO) pattern — CRUD via service methods
- Tax groups and tax regions for tax handling
- Group ID required for AP invoice grouping
- Multi-company support via `Company` field

---

### 5. Acumatica Cloud ERP

**API:** REST / Contract-Based SOAP
**Base URL:** `https://{instance}.acumatica.com/entity/{endpoint}/{version}`
**Auth:** OAuth 2.0

**Create AP Bill:**
```
PUT /entity/Default/24.200.001/Bill
{
  "Type": { "value": "Bill" },
  "Vendor": { "value": "V000001" },
  "Date": { "value": "2026-04-01" },
  "DueDate": { "value": "2026-05-01" },
  "VendorRef": { "value": "INV-2024-001" },
  "Details": [
    {
      "Account": { "value": "6100" },
      "Amount": { "value": 1500.00 },
      "Description": { "value": "Office supplies" }
    }
  ]
}
```

**Status:** `Status` — `Balanced`, `On Hold`, `Open`, `Closed`, `Voided`

**Webhook support:** Push notifications via Generic Inquiries and webhooks.

**Key considerations:**
- Uses PUT for create (upsert pattern)
- Supports custom fields natively
- Batch operations supported via `$batch` endpoint
- Screen-based API allows interaction with any Acumatica form

---

### 6. Sage X3

**API:** REST via Sage Web Services
**Base URL:** `https://{server}:{port}/api1/x3/erp/{folder}`
**Auth:** Basic Auth or API Key

**Create Purchase Invoice:**
```
POST /purchaseInvoice
{
  "BPSNUM": "VENDOR001",
  "BPCINV": "INV-2024-001",
  "INVDAT": "20260401",
  "INVDATVAL": "20260501",
  "LINE": [
    {
      "ITMREF": "ITEM001",
      "QTY": 10,
      "NETPRI": 25.00,
      "ACCCOD": "6100"
    }
  ]
}
```

**Status:** Status field via `INVSTA` — `1` (draft), `2` (validated), `3` (posted)

**Webhook support:** Limited — typically polling-based. Sage X3 supports workflow rules that can trigger external calls.

**Key considerations:**
- Field names are abbreviated (e.g., `BPSNUM` = vendor, `BPCINV` = vendor invoice number)
- Multi-site/multi-company via folder and site codes
- Date format `YYYYMMDD` strings
- Limited API documentation — some endpoints require inspection

---

### 7. Infor CloudSuite Industrial (SyteLine)

**API:** REST via Infor ION API Gateway
**Base URL:** `https://{tenant}.mingle-ionapi.inforcloudsuite.com/{tenant}/IONSERVICES`
**Auth:** OAuth 2.0 via Infor ION

**Integration pattern:** Infor uses **BODs (Business Object Documents)** via ION:
- Send `SyncPayableTransaction` or `ProcessAPVoucher` BOD
- Receive `AcknowledgePayableTransaction` BOD as confirmation

```xml
<SyncPayableTransaction>
  <DataArea>
    <PayableTransaction>
      <PayableTransactionHeader>
        <SupplierParty>VENDOR001</SupplierParty>
        <DocumentReference>INV-2024-001</DocumentReference>
        <TotalAmount currencyID="USD">1500.00</TotalAmount>
        <DueDate>2026-05-01</DueDate>
      </PayableTransactionHeader>
    </PayableTransaction>
  </DataArea>
</SyncPayableTransaction>
```

**Webhook support:** ION Connect workflows trigger events. Use ION API file-based or API-based integration.

**Key considerations:**
- BOD-based integration is the standard Infor pattern
- ION API Gateway handles auth and routing
- Requires Infor OS portal for configuration
- Mapping tables needed for Infor-specific codes

---

### 8. QAD Adaptive

**API:** REST
**Base URL:** `https://{instance}.qad.com/api/v1`
**Auth:** OAuth 2.0

**Create AP Voucher:**
```
POST /ap/vouchers
{
  "supplier": "VENDOR001",
  "voucherNumber": "INV-2024-001",
  "invoiceDate": "2026-04-01",
  "dueDate": "2026-05-01",
  "totalAmount": 1500.00,
  "currency": "USD",
  "lines": [
    {
      "account": "6100",
      "amount": 1500.00,
      "description": "Office supplies"
    }
  ]
}
```

**Status:** `status` — `draft`, `approved`, `posted`, `paid`

**Webhook support:** QAD supports event-driven architecture with webhooks for entity changes.

**Key considerations:**
- Clean modern REST API
- Multi-entity (domain) support
- Supports batch posting of vouchers
- GL account validation enforced server-side

---

### 9. Cetec ERP

**API:** REST
**Base URL:** `https://{company}.cetecerp.com/api`
**Auth:** API Key (via header `X-API-Key`)

**Create AP Invoice:**
```
POST /ap/invoices
{
  "vendor_id": 123,
  "invoice_number": "INV-2024-001",
  "invoice_date": "2026-04-01",
  "due_date": "2026-05-01",
  "total": 1500.00,
  "lines": [
    {
      "gl_account": "6100",
      "amount": 1500.00,
      "description": "Office supplies"
    }
  ]
}
```

**Status:** `status` — `open`, `approved`, `paid`, `void`

**Webhook support:** Limited — primarily polling-based.

**Key considerations:**
- Simple API key auth
- Straightforward REST endpoints
- Smaller ERP — fewer edge cases but less documentation
- Vendor must exist in Cetec before creating AP invoice

---

### 10. DELMIAWorks (formerly IQMS)

**API:** REST / SOAP (legacy)
**Base URL:** `https://{server}/api/v1` (REST) or WSDL-based (SOAP)
**Auth:** API Key or Basic Auth

**Create AP Invoice (REST):**
```
POST /accounts-payable/invoices
{
  "vendorId": "VENDOR001",
  "invoiceNumber": "INV-2024-001",
  "invoiceDate": "2026-04-01",
  "dueDate": "2026-05-01",
  "amount": 1500.00,
  "glEntries": [
    {
      "account": "6100",
      "debit": 1500.00
    }
  ]
}
```

**Status:** `status` — `pending`, `posted`, `paid`, `voided`

**Webhook support:** Limited — custom triggers may be available depending on version.

**Key considerations:**
- Manufacturing-focused ERP — AP is a secondary module
- Legacy SOAP API still in use for some endpoints
- GL entries use debit/credit pattern
- On-premise deployments may require VPN/tunnel for API access

## Field Mapping

Each ERP has different field names for the same concepts. The adapter handles mapping, but organizations can customize via `field_mapping` in org settings.

### Common Mapping Table

| AP System Field | BC | SAP | NetSuite | Epicor | Acumatica |
|---|---|---|---|---|---|
| vendor_name | vendorNumber | InvoicingParty | entity.id | VendorNum | Vendor.value |
| invoice_number | vendorInvoiceNumber | SupplierInvoiceIDByInvcgParty | tranId | InvoiceNum | VendorRef.value |
| amount | totalAmountIncludingTax | InvoiceGrossAmount | total | InvoiceAmt | Amount.value |
| invoice_date | invoiceDate | DocumentDate | tranDate | InvoiceDate | Date.value |
| due_date | dueDate | PaymentBaselineDate | dueDate | DueDate | DueDate.value |
| po_number | purchaseOrderNumber | PurchaseOrder | purchaseOrderNumber | PONum | PONumber.value |
| gl_account | accountId | GLAccount | account.id | GLAccount | Account.value |
| currency | currencyCode | DocumentCurrency | currency.refName | CurrencyCode | CurrencyID.value |
| correlation_id | externalDocumentNumber | ReferenceDocument | externalId | UserDefinedField | Note |

## Error Handling

| Scenario | Handling |
|---|---|
| Auth failure (401/403) | Log error, mark as `failed`, surface in UI for admin to fix credentials |
| Validation error (400/422) | Store the status code + reason code in the audit-log details, mark as `failed` |
| Timeout | Retry with exponential backoff (max 3 attempts), then mark as `failed` |
| Duplicate (409 / DUP_ENTITY) | Already handled BEFORE the create call — see § Idempotency below |
| Rate limit (429) | Retry after `Retry-After` header delay |
| Server error (500) | Retry with backoff, then mark as `failed` |
| Network error | Retry with backoff, then mark as `failed` |

### The failure message never carries the provider's response body

`ErpPostResult.message` is **operator-facing and persisted**: `services/erp.py`
raises `RuntimeError(result.message)` on a failed post and writes `str(exc)`
into `details={"error": …}` on the `invoice.erp_failed` audit row and onto
`WorkflowInstance.state_data["last_error"]`. That audit row is **append-only**
(migration `0022_sox_audit_immutable` installs BEFORE-UPDATE/DELETE triggers)
and `audit_log_shipper` ships it to CloudWatch Logs / S3 Object Lock. Nothing
downstream can redact any of the three.

An ERP's validation error routinely echoes the submitted fields back, and
`InvoicePayload` carries `vendor_tax_id`, `vendor_address`, `remit_to_address`
and `bill_to_address` — so the old `message=f"… {resp.status_code}: {resp.text}"`
put vendor PII into an immutable, WORM-shipped row, violating the
PII-out-of-logs-and-error-responses invariant.

Every adapter therefore builds its failure message through the shared
`erp_adapters/base.py::erp_failure_message(provider, status_code)`, which pairs
the status with a stable, provider-independent reason code from
`erp_failure_reason` (`invalid_request` / `unauthorized` / `forbidden` /
`not_found` / `conflict` / `validation_failed` / `rate_limited` /
`client_error` / `provider_error` / `unexpected_status`):

```
NetSuite post failed: HTTP 422 (validation_failed)
```

That is enough to route the operator — *we sent something the ERP rejected* vs
*our credentials are stale* vs *the ERP is down* — while the diagnosis stays in
the ERP's own error console, which is where the submitted values legitimately
live. Same shape `billing_adapters/stripe_billing.py::_json_or_raise` already
uses. `ErpPostResult.raw_response` may still hold the parsed body, but it is
in-memory only — no caller persists or logs it, and none should start.

Guard: `tests/test_erp_adapter_error_pii.py` drives each real adapter against a
response body echoing tax id / addresses / IBAN and asserts none of it reaches
`message`, plus an AST scan of `erp_adapters/` that fails if any adapter
interpolates `.text` / `.content` / `.json()` into a `message=` f-string again.

## ERP references: a bill is posted by id, never by name

Every real ERP posts a bill against the vendor's and the account's **internal
ids**. The payload carries them (`InvoicePayload.vendor_erp_id`,
`InvoicePayload.gl_account_erp_id`, `LineItemPayload.gl_account_erp_id`), and
`services/erp._resolve_erp_refs` fills them before every push:

- **Vendor** — `vendors.erp_vendor_id` of the invoice's resolved `vendor_id`
  link. An invoice whose vendor never matched has none; there is no lookup by
  `vendor_name`.
- **Accounts** — `gl_accounts.erp_account_id` for each GL code on the header and
  the lines, resolved by `gl_chart.resolve_erp_account_ids` against the
  invoice's own chart: shared (`entity_id IS NULL`) ∪ the invoice entity's own,
  with the entity's row winning when both define the code (the override
  precedence `_sync_match_query` applies). Another entity's account never
  resolves.
- **Bounded** — two queries per push whatever the line count (one vendor read,
  one chart read for every distinct code). Only the vendor and chart syncs write
  these ids, so a tenant has to run them before its first push.

**Fail closed, before any HTTP call.** An adapter that needs an id the payload
lacks returns `erp_refusal(provider, reason)`: an `ErpPostResult` with
`success=False`, `retryable=False` and the message
`erp_refusal_message(provider, reason)`, e.g. `NetSuite post refused:
vendor_not_linked`. `services/erp` raises `ErpPostRefusedError` for a
non-retryable result and fails the invoice on the first attempt, with no
backoff, because the same payload would be refused again. The reason codes are
stable constants in `erp_adapters/base.py`: `VENDOR_NOT_LINKED` and
`ACCOUNT_NOT_LINKED`. The message is PII-free for the same reason as
`erp_failure_message` (it lands on the append-only `invoice.erp_failed` row). A
name or code fallback was rejected: a name picks the wrong "Acme" the first time
two vendors share one, and Business Central's `vendorNumber` holds a vendor
*number*, so the name we used to send there matched nothing, or matched another
vendor whose number happened to equal it.

| Adapter | Vendor | Accounts |
|---|---|---|
| `netsuite` | `entity: {id}`; refuses `vendor_not_linked` | GL-coded lines go on the **`expense`** sublist (`account: {id}`, `amount` = the line total, or quantity × unit price when it has none), since the `item` sublist needs an item record we never send. An uncoded line takes the header account. A coded line with no id, or a bill with no account at all, refuses `account_not_linked`. A coded line is never moved onto the header account. A line with no amount at all refuses `line_amount_missing` (it used to post as 0). NetSuite totals the bill from its lines, so lines that do not sum to exactly the approved amount (tax-exclusive lines, header-only shipping or discount) refuse `amount_mismatch` — never collapsed onto the header account, which would move coded expense there. Only an invoice with no line items posts one line for the amount on the header account. |
| `dynamics_365_bc` | `vendorId`; refuses `vendor_not_linked` | Every line is an `Account` line on **`accountId`** = the account GUID the BC chart sync stored, never `lineObjectNumber` (the No.). Same rules as NetSuite: an uncoded line takes the header account, a coded line with no id or a bill with no account at all refuses `account_not_linked`. See § Business Central: chart, POs and void. |
| `merge_dev` | `contact` (Merge object id); refuses `vendor_not_linked` | A line's `account` is the Merge account id. A coded line with no id refuses `account_not_linked`; an uncoded line sends none (Merge allows it). |

**NetSuite chart sync.** `NetSuiteAdapter.list_gl_accounts` pulls the chart
with one SuiteQL query (`POST …/services/rest/query/v1/suiteql`,
`Prefer: transient`, `SELECT id, acctnumber, fullname, accttype, isinactive FROM
account`), paged by `offset` / `hasMore` up to 1,000 rows. It skips inactive
accounts, and skips an account whose name would have to stand in for a missing
number but is longer than the 50-character code column, because truncating
could merge two accounts onto one code. The REST record collection (`GET
/account`) returns only ids and links, so it would cost one request per
account.

The fake ERP enforces the same rules: NetSuite returns a 400 for an unknown `entity` or
expense-line `account` id, or for an `item` sublist. Business Central returns
a 400 for a `vendorId` / `vendorNumber` that names no vendor, and Merge does
the same for an unknown `contact` / line `account`. So the `tests-e2e/erp/`
specs prove the ids reach the wire: each one syncs first, then sends.
`netsuite.spec.ts` also proves the refusal end to end.

Tests: `tests/test_erp_push_flow.py` (resolution, entity override vs shared
fallback against a real tenant, the two-query bound, refusal and NetSuite body
through `_call_erp`), `tests/test_erp_adapter_error_pii.py` (every refusal, for
every adapter, before any HTTP call), `tests/test_erp_adapter_idempotency.py`
(NetSuite / BC body shape), `tests/test_erp_adapter_money_exact.py`,
`tests/test_erp_gl_sync.py` (SuiteQL mapping + paging).

## Idempotency (retry-safe pushes)

`_call_erp`'s 3-attempt retry loop (`services/erp.py`) means a client-side
timeout AFTER the ERP already accepted the create can otherwise retry into a
**second** vendor bill for the same invoice. Every real adapter's
`post_invoice` is idempotent on `payload.correlation_id` (the stable per-invoice
key already threaded through `_build_payload`), via whichever mechanism the
target ERP actually supports:

| Adapter | Mechanism |
|---|---|
| `merge_dev` | `X-Idempotency-Key: <correlation_id>` header on `POST /invoices` — Merge's unified API returns the ORIGINAL response for a repeated key instead of creating a second invoice. |
| `netsuite` | Pre-create lookup: `GET /vendorBill?q=externalId IS "<correlation_id>"` (NetSuite enforces `externalId` uniqueness per record type). A match short-circuits `post_invoice` to a success referencing the existing bill; only a miss proceeds to `POST /vendorBill`. |
| `dynamics_365_bc` | Pre-create lookup: `GET purchaseInvoices?$filter=externalDocumentNumber eq '<correlation_id>'`. A hit short-circuits only when it is `Open` or `Paid`. A `Draft` (an earlier attempt whose post step failed or whose response was lost) is finished: its total re-checked, then `Microsoft.NAV.post` re-run — never a second create. Any other status (`In Review`, `Canceled`, `Corrective`) refuses `existing_invoice_not_open`. |

The local `fake-erp` mock implements the matching server-side behavior (a
merge-idempotency-key cache; `q=`/`$filter=` collection queries filtered by
`externalId`/`externalDocumentNumber`) so this is exercised end-to-end by
`pnpm test:erp` without a live ERP account. Unit-level coverage (mocked HTTP,
no fake-erp container needed) lives in
`backend/tests/test_erp_adapter_idempotency.py`.

**A failed lookup is a failure, not a miss** — on every direct adapter. A
non-200 from the NetSuite `externalId` or the BC `externalDocumentNumber`
lookup returns a retryable `erp_failure_message(...)` result and creates
nothing; the retry looks again. Reading it as "not posted yet" re-created the
duplicate the lookup exists to prevent, at exactly the moment the ERP was
struggling (Sage Intacct and SYSPRO already behaved this way).

## Retry Logic

`services/erp.send_to_erp_internal` is the only push path. It makes up to
`MAX_RETRIES` (3) attempts with exponential backoff (2 s, then 4 s), and keeps
the attempt count on `WorkflowInstance.state_data["erp_retries"]`, so a
re-entered push resumes rather than restarts. A **non-retryable** result
(`ErpPostResult.retryable` False: a pre-flight refusal, `posted_total_*`,
`job_unconfirmed`) or an OAuth ERP with no usable connection fails the invoice
on the first attempt. After the last attempt the invoice goes to `failed`, with
`last_error` on the instance and an `invoice.erp_failed` audit row.

**A failure that left a bill in the ERP names it.** Examples are a
`posted_total_mismatch` whose void failed ("could not be voided") and a
`posted_total_unconfirmed` bill. A result like that carries
`erp_document_id` / `erp_document_number`. `_call_erp` raises
`ErpPostRefusedError` (or `ErpPostFailedError` on the retryable path) with
those ids. It keeps them on `state_data["erp_orphan_document_id"]` /
`["erp_orphan_document_number"]`, the same way it keeps the pending job id. The
`invoice.erp_failed` row records them as `erp_document_id` /
`erp_document_number`. Without this, the invoice sat at `failed` while a bill
with a total nobody approved stayed live in the ERP, and nobody could find it.
These are the ERP's own ids, not PII. The provider's response body is never
written. A failure that reports no document leaves earlier ids in place,
because that bill is still in the ERP. A successful push clears them, because
its `erp_reference` then names the bill of record.

**Manual retry:** `POST /api/invoices/{id}/retry-erp` resets `erp_retries` to 0
and moves the invoice to `sending_to_erp`. The route then dispatches the push.
It assigns a **new** `state_data` dict. The column is plain JSONB with no
`MutableDict`, so an in-place edit is never saved. That was a real bug: the
reset was lost, and the retry failed at once on the exhausted counter. Every
other key is kept. That includes `erp_pending_job_id`: an ERP whose create is a
background job (Blackbaud) must poll the job an earlier attempt queued before
it queues another, or the retry posts a second bill. The orphan ids are kept
too, until a successful push replaces them.

## Security

- ERP credentials are encrypted at rest, per field, inside `Organization.settings` JSONB (§ Credentials at rest)
- Webhook endpoints validate requests via signature/secret or IP whitelist
- All ERP communication uses HTTPS
- Credentials are never logged or included in audit trail details
- Future: integrate with AWS Secrets Manager or HashiCorp Vault

## Testing

Each adapter includes a `test_connection()` method that:
1. Authenticates with the ERP
2. Makes a lightweight read request (e.g., list vendors)
3. Returns success/failure

Available in the UI via the Organization settings page under ERP configuration.

## Local e2e testing (fake ERP server)

The three real adapters (`merge_dev`, `netsuite`, `dynamics_365_bc`) can be
exercised end-to-end against a local **fake ERP server** — a small FastAPI app
(`tools/fake-erp/`, in-memory, deterministic) run as the `fake-erp` service in
`backend/docker-compose.yml` (opt-in `erp` profile, host port **12112**,
container 8080). Why: NetSuite has no free sandbox, and the local-first rule
requires every provider path to have a deterministic local equivalent — the
fake gives all three direct-HTTP code paths a target with no cloud account or
credential.

One server fakes three provider surfaces by path prefix:

| Provider | Fake surface | Path prefix |
|---|---|---|
| Merge.dev | Unified accounting API | `/merge/api/accounting/v1` |
| NetSuite | SuiteTalk REST records | `/netsuite/services/rest/record/v1` |
| Dynamics 365 BC | OData + OAuth token endpoint | `/d365` (token at `/d365/oauth2/token`) |

`GET /health` reports liveness; `POST /__reset` restores the fixture state.
Fixture data: purchase orders `PO-FAKE-301` (1250.00), `PO-FAKE-302` (980.50),
`PO-FAKE-303` (4400.00) — cursor-paginated — GL accounts `6100 Fake Office
Supplies`, `6200 Fake Software`, `6300 Fake Consulting`, and (Merge.dev only,
issue #256) three cursor-paginated vendors — `Fake Merge Vendor Co` (Net 30),
`Fake Merge Supply Co` (Net 45), `Fake Merge Services Co` (Net 60, and the one
fixture whose `payment_term` is a bare string rather than an object) — full
list in `tools/fake-erp/README.md`.

### Env-overridable base URLs

Four operator-controlled env vars point the adapters at the fake. All four
carry committed, non-secret local-dev values in `backend/.env.development`:

| Variable | Default | Purpose |
|---|---|---|
| `FEOH_ERP_MERGE_API_BASE` | `https://api.merge.dev/api/accounting/v1` | Merge.dev API base. Dev value: `http://localhost:12112/merge/api/accounting/v1`. |
| `FEOH_ERP_NETSUITE_API_BASE` | (empty) | Empty → the per-account URL derived from `account_id`; set → used verbatim. Dev value: `http://localhost:12112/netsuite/services/rest/record/v1`. |
| `FEOH_ERP_D365_API_BASE` | (empty) | Empty → the admin-config `base_url` (https on `api.businesscentral.dynamics.com` only) + SSRF guard; set → used verbatim. Dev value: `http://localhost:12112/d365`. |
| `FEOH_ERP_D365_TOKEN_URL` | (empty) | Empty → `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`. Dev value: `http://localhost:12112/d365/oauth2/token`. |

**Trust model:** these vars are process-level and operator-controlled
(TRUSTED), so they bypass the SSRF guard that screens *tenant-admin-supplied*
config. An admin-entered `base_url` in Organization settings is still guarded
— only the operator who owns the process env can point an adapter at an
arbitrary host.

### Running the suite

```bash
pnpm erp:up      # start fake-erp (compose profile erp, :12112); erp:down / erp:logs too
pnpm dev         # backend picks up the .env.development fake-erp base URLs
pnpm test:erp    # Playwright suite frontend/tests-e2e/erp/ (merge-dev / netsuite / dynamics specs)
```

Coverage per spec: `test_connection`, PO sync (`list_pos`), GL-account sync
(`list_gl_accounts`), vendor sync (`list_vendors` — Merge.dev asserts the
full field mapping — name/email/phone/address/tax id/payment terms; NetSuite
and Business Central prove it by posting against the synced vendor id), and a
full send-to-ERP round trip to a terminal invoice status. The BC and NetSuite
specs sync PO fixtures numbered `PO-FAKE-BC-4xx` / `PO-FAKE-NS-5xx`, so they
never collide with Merge's `PO-FAKE-30x`.

The specs skip gracefully when fake-erp isn't reachable, so the normal suite
stays green without it. CI runs them in the dedicated `erp-e2e` job in
`ci.yml` (modeled on `service-e2e`, required by `ci-gate`). `pnpm services:up`
includes the `erp` profile.

**Caveat — what this does and doesn't test:** the fake *shape-checks* provider
auth (non-empty bearer + `X-Account-Token` for Merge, OAuth 1.0 header
presence for NetSuite, client-credentials → the fixed bearer `fake-d365-token`
for D365) but does **not** cryptographically verify signatures. It validates
OUR client code — request shape, auth header construction, pagination, status
mapping — not that a real provider would accept our credentials.

## Code Structure

```
backend/app/services/erp_adapters/
    __init__.py          # Package exports
    base.py              # ErpAdapter base class, InvoicePayload, ErpPostResult types
    dispatcher.py        # get_erp_adapter() — reads config, picks adapter
    mock_adapter.py      # Development/testing adapter
    merge_dev.py         # Merge.dev unified API adapter (covers all 10 ERPs)
    dynamics_365_bc.py   # Direct Business Central adapter (OAuth2 + OData)
    netsuite.py          # Direct NetSuite adapter (TBA/OAuth1 + REST)

backend/app/services/erp.py          # send_to_erp(), retry logic, calls adapters
backend/app/services/erp_dispatch.py  # Routes to local or Lambda, loads org ERP config
backend/app/api/erp_webhook.py        # POST /api/erp/webhook/{erp_type}
```

### Adding a New Direct Adapter

1. Create `backend/app/services/erp_adapters/your_erp.py`
2. Subclass `ErpAdapter` and implement `post_invoice`, `get_invoice_status`, `void_invoice`, `test_connection`. Optionally override `list_pos` if the ERP supports listing purchase orders (otherwise the default `[]` is used and `/api/purchase-orders/sync-erp` reports zero new POs)
3. Decorate the class with `@register_adapter("your_erp_type")`
4. Add the module to `BUILTIN_ADAPTER_MODULES` in `erp_adapters/dispatcher.py` — the one list every caller loads the registry from (`tests/test_erp_adapter_registry.py` fails if it is missing)
5. Add (or un-pend) its entry in `erp_adapters/catalog.py`: `key` = the registry key, its `regions`, `auth`, and one `_field(...)` per config key the adapter reads, with `secret=True` on every credential. That is the whole UI change: the Organization → ERP form renders from the catalogue. Remove the key from `PENDING_ADAPTERS` if it was there (`tests/test_erp_catalog.py` enforces both)
6. Add any new `org.erp.field.<name>` / `help_key` label to all six frontend locale catalogues (`frontend/src/lib/i18n/locales/*.ts`)

## Setup Guide

### Direct ERP (QuickBooks Online, Xero, Sage, NetSuite, Business Central, SYSPRO)

1. Go to **Organization > ERP Integration**
2. Pick your ERP from the dropdown. It leads with the ERPs popular in the
   United States and in South Africa
3. Enter the fields the form shows for it. Saved secrets show as "saved, leave
   blank to keep"; type a new value only to replace one
4. Save, then **Test connection**
5. For an OAuth ERP (QuickBooks Online, Xero, Sage Business Cloud Accounting),
   optionally enter your own app's client ID and secret, save, then click
   **Connect to <ERP>** and approve access in the ERP. You return to the ERP
   section with a connected status, and can **Disconnect** there

### Other ERP via Merge.dev (Scale plan)

1. Create a [Merge.dev](https://merge.dev) account and get your API key
2. Have your customer connect their ERP via Merge Link, which creates an
   account token
3. In **Organization > ERP Integration**, choose **Other ERP via Merge.dev**,
   pick the ERP from the second dropdown, enter the API key and account token,
   then save and test

### Merge.dev Pricing

Merge.dev is **not free**. Pricing is per linked account: one customer
connected to one integration (merge.dev/pricing, read 2026-10-07):
- **Launch**: the first 3 production linked accounts free, then $650/month for
  up to 10, then $65 per additional linked account per month. Up to 3 test
  linked accounts.
- **Professional / Enterprise**: contract-based.

$65 a linked account is more than the $49 Growth plan, which includes ERP
integrations. That is why QuickBooks Online is scoped as a direct adapter
(`quickbooks-online-adapter.md`) with Merge kept for the long tail.

## Implementation Status

| Phase | Status |
|---|---|
| Adapter interface + dispatcher | Done |
| Mock adapter | Done |
| Merge.dev adapter | Done |
| Business Central direct adapter | Done |
| NetSuite direct adapter | Done |
| Webhook endpoint | Done |
| ERP config UI in org settings | Done (catalogue-driven, write-only secrets) |
| Post-ERP statuses (posted_in_erp, payment_scheduled, paid) | Done |
| Polling job for status sync | Planned |
| OAuth authorization-code connect flow (`services/erp_oauth`) | Done — § Connecting an OAuth ERP |
| QuickBooks Online direct adapter | Done (Phases 1–2) — `quickbooks-online-adapter.md`; webhooks and BillPayment write-back still scoped |
| Remaining direct adapters (SAP, Epicor, etc.) | Use Merge.dev |
| Test connection button in UI | Done |
| ERP status display in invoice modal | Planned |

## Bill lines: one rule set, two views

Every direct adapter that posts line detail takes its lines from
`erp_adapters/bill_allocation.allocate_bill_lines`, which is the single
statement of the rules; each module's docstring opens with this "which helper
when" note:

| View | Shape | Used by | Why |
|---|---|---|---|
| `bill_allocation.allocate_bill_lines` | net, tax and gross per line | Xero, Sage Business Cloud Accounting v3.1 | the ERP derives the bill total from lines **plus tax** |
| `bill_lines.bill_lines` | `(account, gross, memo)` | Sage Intacct, SYSPRO, Sage Accounting ZA, Blackbaud FE NXT, QuickBooks Online | the ERP takes tax separately (or not at all; Sage ZA splits VAT out of the gross by the account's rate) |

`bill_lines` is a projection of the allocation, so the two cannot disagree:

- **Amount** — a line's `total`, else `quantity × unit_price`, else
  `line_amount_missing`. An invoice with no lines is one header line.
- **Account** — a coded line posts on its own account's ERP id; coded but
  unlinked → `account_not_linked`, never moved onto the header's account. Only
  an uncoded line takes the header's (none → `account_not_linked`).
- **Total** — the gross lines sum to exactly `payload.amount`: tax-inclusive
  lines as given, tax-exclusive lines plus their stated tax (each line's own
  `tax` when they add up to `tax_amount`, or all of it on a single line).
  Anything else → `amount_mismatch`; several tax-exclusive lines with
  header-only tax → `tax_not_itemised` (never pro-rated). There is no fallback
  to one line on the header account.

The shared reason codes (`account_not_linked`, `amount_mismatch`,
`line_amount_missing`, `not_connected`, `posted_total_mismatch`,
`posted_total_unconfirmed`) are constants in `erp_adapters/base.py`.

## Sage Intacct direct adapter (`sage_intacct`)

`erp_adapters/sage_intacct.py`, selected by `settings.erp = {"type":
"sage_intacct", "integration_method": "direct", ...}`. Top-5 for US mid-market
AP; set up by pasting credentials — no OAuth consent screen.

**API choice: the REST API, not the XML gateway.** Intacct has two APIs. The
XML Web Services gateway (`https://api.intacct.com/ia/xml/xmlgw.phtml`) needs a
paid Web Services *sender ID* that each customer company must authorize, a
session per request batch, and XML whose error blocks echo the submitted
fields. The REST API (`https://api.intacct.com/ia/api/v1`) went generally
available in 2025, is where Sage points new integrations, and covers every
object used here. Its OAuth 2.0 **client-credentials** grant authenticates a
Web Services user directly, so the setup page stays a paste-the-credentials
form. JSON throughout — no XML parser on this path.

| `settings.erp` key | Secret | Meaning |
|---|---|---|
| `client_id` | no | OAuth client id of the registered Sage app |
| `client_secret` | **yes** | OAuth client secret |
| `company_id` | no | Intacct company id |
| `user_id` | no | Web Services user authorized for the app (token `username` is `user_id@company_id`) |
| `location_id` | no (optional) | Top-level entity of a multi-entity company; sent as `X-IA-API-Param-Entity` on every call |
| `po_document_type` | no (optional) | Purchasing transaction definition used for POs (default `Purchase Order`) |

| Operation | Call |
|---|---|
| token | `POST <base>/oauth2/token` (form: `grant_type=client_credentials`, `client_id`, `client_secret`, `username`) — one per adapter operation |
| `post_invoice` | refuse without `vendor_erp_id` (`vendor_not_linked`) or any GL account ERP id (`account_not_linked`) → `POST services/core/query` on `accounts-payable/bill` filtered `referenceNumber = correlation_id` → `POST objects/accounts-payable/bill` |
| `get_invoice_status` | `GET objects/accounts-payable/bill/{key}`, `state` (+ `totalTxnAmountDue = 0` ⇒ paid) |
| `void_invoice` | deletes a draft / posted-unpaid bill; a bill that is paid, partially paid, selected for payment or reversed returns `False` (reversal with its payment is an accountant's call) |
| `list_vendors` / `list_gl_accounts` / `list_pos` | `services/core/query` on `accounts-payable/vendor`, `general-ledger/account`, `purchasing/document::<po_document_type>`, 100 rows × 10 pages |
| `test_connection` | token + a one-row vendor query |

Request rules worth knowing:

- **Lines.** `bill_lines.bill_lines` posts one Intacct line per invoice line
  (one header line when the invoice has none) at its tax-inclusive gross. It is
  a projection of `bill_allocation.allocate_bill_lines`, so the rules are the
  ones every direct adapter shares (see § Bill lines: one rule set, two views):
  a line's amount is its total, else quantity × unit price, else
  `line_amount_missing`; a coded line posts on its own account's ERP id and an
  unlinked one refuses `account_not_linked` (only an uncoded line takes the
  header's); the lines must sum to exactly `payload.amount` (tax-inclusive, or
  tax-exclusive plus their stated tax), else `amount_mismatch` /
  `tax_not_itemised`. There is no one-header-line fallback: the header amount is
  never recomputed from lines, and coded expense is never moved to the header
  account to make the lines fit.
- **Money** goes as fixed-point decimal strings (`format(d, "f")`): no float,
  no exponent form, scale preserved.
- **Idempotency.** The pre-create lookup by `referenceNumber`; a *failed*
  lookup is a failure, never read as "not posted yet".
- **Failure messages** come from `erp_failure_message("Sage Intacct", status)`;
  a token failure raises with the status only.
- **GL account types.** Intacct classifies only balance sheet vs income
  statement, so balance-sheet credit accounts (liability or equity) stay
  unclassified rather than guessed.

Tests: `backend/tests/test_erp_sage_intacct_adapter.py`. fake-erp surface:
`/intacct/ia/api/v1` (`FEOH_ERP_INTACCT_API_BASE`).

## SYSPRO direct adapter (`syspro`)

`erp_adapters/syspro.py`, selected by `settings.erp = {"type": "syspro",
"integration_method": "direct", ...}`. SYSPRO is the South African-built ERP for
manufacturing and distribution, and it is **customer-hosted**: SYSPRO 8's e.net
Communications Service exposes the e.net business objects as a REST endpoint on
the customer's own server.

| `settings.erp` key | Secret | Meaning |
|---|---|---|
| `base_url` | no | https URL of the e.net REST endpoint, e.g. `https://syspro.example.co.za:20190` (`/SYSPROWCFService/Rest` is appended when absent) |
| `operator` | no | SYSPRO operator code |
| `operator_password` | **yes** | Operator password |
| `company_id` | no | SYSPRO company id |
| `company_password` | **yes** (optional) | Company password, when the company has one |
| `posting_period` | no (optional) | APSTIN `PostingPeriod` (default `C`, current) |

Every operation is `Logon` → work → `Logoff`, with the logoff in a `finally`:
an orphaned session holds a SYSPRO licence seat. All calls are `GET` with
query-string parameters, which is how the WCF REST host takes them:

| Operation | Business object |
|---|---|
| `post_invoice` | refuse without `vendor_erp_id` / a GL account ERP id → `COMFND` on `ApInvoice` by (`Supplier`, `Invoice`) → `Transaction/Post` `APSTIN` |
| `get_invoice_status` | `COMFND` on `ApInvoice` — `MthInvBal1` 0 ⇒ paid, below `OrigInvValue` ⇒ partially paid, else open |
| `void_invoice` | not automated (`False`) — reversing a posted AP invoice is an adjustment / credit in an open period, an accountant's call |
| `list_vendors` / `list_gl_accounts` | `COMFND` on `ApSupplier` / `GenMaster` (`AccountType` A/L/C/R/E → asset/liability/equity/revenue/expense) |
| `list_pos` | `COMFND` on `PorMasterHdr` + `PorMasterDetail` (total = Σ qty × price) + `ApSupplier` for names, one session; a PO with no lines returned is skipped, never synced at 0 |

- **`erp_document_id` is `<supplier>|<invoice>`** — SYSPRO keys an AP invoice
  by that pair, not by a surrogate id.
- **Idempotency** rests on that same pair, which SYSPRO holds unique: an
  existing row with the same `OrigInvValue` is the earlier attempt (success,
  no second post); a different amount is another document holding the number
  and is refused as `duplicate_invoice_number`. A failed lookup is a failure,
  never read as "not posted yet".
- **XML** is built with lxml (text is escaped; characters XML cannot carry are
  dropped) and parsed with the hardened `e_invoice/_xml.parse_secure` parser
  (no DTD, no entity resolution, no network) — the same posture as the
  e-invoice and punch-out paths, so no second XML-hardening dependency.
- **Credentials in the URL.** SYSPRO takes the operator password, the session
  id and the business-object XML in the query string, and the app's root logger
  runs at INFO, where httpx logs every request URL. The module installs a filter
  on the `httpx` logger that replaces the query of any `SYSPROWCFService` URL
  with `?[redacted]`, and transport errors are re-raised as
  `SysproError("SYSPRO <step> failed: <ExceptionClass>")` from `None`, because
  httpx error strings can carry the URL. The APSTIN document carries no vendor
  tax id or address.
- **https only, SSRF-guarded.** An admin `base_url` must be `https` (the
  password would otherwise travel in clear text) and passes
  `assert_public_url_async` before any request; `FEOH_ERP_SYSPRO_API_BASE` is
  the operator override for fake-erp and skips both. A SYSPRO server on a
  private network therefore has to be published (reverse proxy with TLS) before
  it can be connected.
- **Failure messages** never echo the response: SYSPRO's error text quotes the
  submitted fields back. HTTP failures use `erp_failure_message("SYSPRO",
  status)`; a business-object rejection is `SYSPRO post failed: invoice
  rejected by APSTIN`.
- **Schema provenance.** SYSPRO ships its business-object schemas with each
  install (`<SYSPRO>\Base\Schemas\APSTIN*.XSD`, `COMFND.XSD`) rather than
  publishing them. The `COMFND` document matches the published open-source
  client (wildland/syspro-ruby); the APSTIN element names and the
  `ApInvoice` / `PorMasterHdr` / `PorMasterDetail` column names follow SYSPRO's
  field names but have not yet been checked against a live install — see
  `docs/followups.md`.

Tests: `backend/tests/test_erp_syspro_adapter.py`. fake-erp surface:
`/syspro/SYSPROWCFService/Rest` (`FEOH_ERP_SYSPRO_API_BASE`).
## Xero (direct, OAuth 2.0)

`erp_adapters/xero.py`, `@register_adapter("xero")`, `integration_method:
"direct"`. Subclasses `OAuthErpAdapter`: the bearer token comes only from
`self.access_token()` (`services/erp_oauth` owns consent, refresh and
storage), and every call sends `Xero-Tenant-Id: <external_tenant_id>`, the
organisation picked at consent. Base `https://api.xero.com/api.xro/2.0`,
overridable with `FEOH_ERP_XERO_API_BASE` (fake-erp in dev). Platform app
credentials: `FEOH_ERP_XERO_CLIENT_ID` / `_SECRET` (empty → unavailable, no
fallback). Scopes are Xero's granular set (`accounting.invoices`,
`accounting.contacts.read`, `accounting.settings.read`, `offline_access`),
which apps created on or after 2026-03-02 must use.

**`settings.erp` keys:** `oauth` (written by `erp_oauth` only),
`bill_status` (`AUTHORISED` default, or `DRAFT`), `default_tax_type`
(optional fallback, below).

| Method | Xero call | Notes |
|---|---|---|
| `test_connection` | `GET Organisation` | |
| `list_vendors` | `GET Contacts?where=IsSupplier==true` | paged, 100/page, 10-page cap |
| `list_gl_accounts` | `GET Accounts` | `Class` → account_type; archived skipped |
| `list_pos` | `GET PurchaseOrders` | `BILLED` → closed, `DELETED` → cancelled |
| `post_invoice` | `PUT Invoices` (`Type: ACCPAY`) | below |
| `get_invoice_status` | `GET Invoices/{id}` | DRAFT/SUBMITTED → draft; AUTHORISED → open, or partially_paid when `AmountPaid > 0`; PAID → paid; VOIDED/DELETED → cancelled |
| `void_invoice` | `POST Invoices/{id}` | DRAFT/SUBMITTED → `DELETED`; unpaid, uncredited AUTHORISED → `VOIDED`; anything with money applied → `False` |

**`post_invoice`:**

- **Refusals, before any HTTP call** (`"Xero post refused: <reason>"`):
  `vendor_not_linked` (no `vendor_erp_id`; never a name lookup),
  `missing_dates`, `account_not_linked` (a coded line whose account has no
  `gl_account_erp_id` — never moved onto the header's — or an uncoded line /
  header-only bill with no linked header account), `amount_mismatch` /
  `tax_not_itemised` / `line_amount_missing` (from
  `erp_adapters/bill_allocation.py`, shared with every direct adapter).
- **The total is never re-derived.** `bill_allocation.allocate_bill_lines`
  classifies lines as tax-inclusive (they sum to `amount`) or tax-exclusive
  (they plus `tax_amount` sum to `amount`) and refuses anything else. Per-line
  tax comes only from the invoice: each line's own `tax` when they sum to
  `tax_amount`, or all of it on a single line. Several tax-exclusive lines with
  header-only tax are refused, not pro-rated.
- **Tax.** No tax → `LineAmountTypes: NoTax`. Itemised tax →
  `Exclusive` with an explicit `TaxAmount` per line (Xero honours the
  override, so the total is exactly `amount`). Tax-inclusive lines with
  header-only tax → `Inclusive` with no `TaxAmount`: Xero splits net/tax by the
  rate, and the gross total is still exact. The `TaxType` is the line
  account's own default from `GET Accounts` (the customer's chart, e.g. a ZA
  org's 15% input VAT type); `settings.erp.default_tax_type` is used only for
  an account with none; neither → `tax_rate_unresolved`. Rates are never
  hardcoded.
- **Idempotency, two layers.** A pre-create
  `GET Invoices?InvoiceNumbers=…&ContactIDs=<vendor>&Statuses=DRAFT,SUBMITTED,AUTHORISED,PAID`:
  a live bill with the same total is adopted (idempotent success); a different
  total is refused `duplicate_document_number`. A failed lookup fails closed.
  `Reference` is ACCREC-only in Xero, so no bill field can carry our
  correlation id; the create also sends `Idempotency-Key: <correlation_id>`
  (Xero replays the original response).
- **The posted total is checked.** Xero computes `Total` from the lines and
  their tax types; a `Total` other than `amount` voids (AUTHORISED) or deletes
  (DRAFT) the bill just created and fails non-retryable
  `posted_total_mismatch`, and a create that reports no `Total` fails
  non-retryable `posted_total_unconfirmed` — never success
  (`erp_adapters/posted_total.py`).
- **Rate limits** (60/min, 5,000/day per tenant): HTTP 429 →
  `"Xero post failed: HTTP 429 (rate_limited)"`, `Retry-After` kept in the
  in-memory `raw_response`. No sleep loop; `services/erp`'s retry backoff
  owns the timing.
- Failures go through `erp_failure_message`; no response body is persisted.

Tests: `backend/tests/test_erp_xero_adapter.py`. Fake surface:
`tools/fake-erp/README.md` § Xero.

## Sage Business Cloud Accounting (direct, OAuth 2.0)

`erp_adapters/sage_accounting.py`, `@register_adapter("sage_accounting")`,
`integration_method: "direct"`. Subclasses `OAuthErpAdapter` like Xero: the
token comes only from `self.access_token()`, and every call sends
`X-Business: <external_tenant_id>`. API v3.1 at
`https://api.accounting.sage.com/v3.1` (override
`FEOH_ERP_SAGE_ACCOUNTING_API_BASE`). OAuth: authorize at
`https://www.sageone.com/oauth2/auth/central` with `filter=apiv3.1`, token at
`https://oauth.accounting.sage.com/token`, scope `full_access`. Platform app
credentials: `FEOH_ERP_SAGE_ACCOUNTING_CLIENT_ID` / `_SECRET` (empty →
unavailable, no fallback).

**Region coverage: not South Africa.** Every v3.1 operation lists its
availability as CA, DE, ES, FR, GB, IE and US. Sage Business Cloud Accounting
**South Africa** (the Pastel successor) runs on a separate codebase with its
own API (`https://accounting.sageone.co.za/api/2.0.0`, API key + basic auth,
not OAuth), and Sage's developer community confirms v3.1 does not serve it.
So this adapter covers the US and the other v3.1 regions; a ZA business cannot
complete its consent flow. A ZA Sage adapter is separate work.

**`settings.erp` keys:** `oauth` (written by `erp_oauth` only),
`default_tax_rate_id` (optional fallback), `void_reason` (optional; default
`Voided from FeohLedger`).

| Method | Sage call | Notes |
|---|---|---|
| `test_connection` | `GET business_settings` | |
| `list_vendors` | `GET contacts?contact_type_id=VENDOR` | 200/page, 10-page cap; system contacts skipped |
| `list_gl_accounts` | `GET ledger_accounts` | `nominal_code` → code; `ledger_account_type` → account_type; out-of-chart skipped |
| `list_pos` | — | base default `[]` (no v3.1 PO collection on every plan) |
| `post_invoice` | `POST purchase_invoices` | below |
| `get_invoice_status` | `GET purchase_invoices/{id}` | DRAFT → draft; UNPAID/DISPUTED → open; PART_PAID → partially_paid; PAID → paid; VOID → cancelled |
| `void_invoice` | `DELETE purchase_invoices/{id}` | DRAFT deleted; unpaid UNPAID/DISPUTED voided with `void_reason`; anything with a payment allocated → `False` |

**`post_invoice`:**

- Same refusals and the same exact line split as Xero
  (`erp_adapters/bill_allocation.py`), plus one: tax-inclusive lines with
  header-only tax are refused `tax_not_itemised`, because v3.1 never
  calculates tax and needs every line's `tax_amount`.
- **Explicit amounts on every line**: `net_amount`, `tax_amount`,
  `total_amount`, `unit_price_includes_tax: false`, and `quantity` ×
  `unit_price` only when it reproduces the net (else 1 × net). The header
  carries `net_amount` / `tax_amount` / `total_amount = amount`. The
  `tax_rate_id` is the line ledger's own default (`GET
  ledger_accounts/{id}?attributes=tax_rate`), `default_tax_rate_id` only when
  it has none, else `tax_rate_unresolved`. An untaxed invoice sends no tax
  rate.
- **Idempotency.** Sage has no idempotency key, so every invoice is written
  with `notes: "FeohLedger <correlation_id>"`, and a pre-create lookup
  (`contact_id` + `from_date`/`to_date` = the invoice date) looks for a live
  invoice with the same `vendor_reference`: carrying our marker → adopted;
  without it → `duplicate_document_number`. A failed lookup fails closed, and a
  lookup that runs past the page cap is refused
  `idempotency_lookup_incomplete`. An adopted invoice must carry the approved
  `total_amount` too (the check below).
- **The posted total is checked.** Sage recalculates `total_amount`; one other
  than `amount` voids/deletes the invoice just created and fails non-retryable
  `posted_total_mismatch`, and a create that reports none fails non-retryable
  `posted_total_unconfirmed` (`erp_adapters/posted_total.py`).
- 429 → `rate_limited` (no sleep loop); failures through
  `erp_failure_message`.

Tests: `backend/tests/test_erp_sage_accounting_adapter.py`. Fake surface:
`tools/fake-erp/README.md` § Sage Business Cloud Accounting.

## Sage Business Cloud Accounting — South Africa (`sage_accounting_za`)

`erp_adapters/sage_accounting_za.py`, selected by `settings.erp = {"type":
"sage_accounting_za", "integration_method": "direct", ...}`. Sage Business Cloud
Accounting is the cloud successor to Sage Pastel and the leading SA-native SME
ledger. **South African companies are not served by Sage's global v3.1
Accounting API** (its Swagger lists CA/DE/ES/FR/UK/IE/US only); they run on a
separate product with its own API, `https://accounting.sageone.co.za/api/2.0.0`
(specification: `https://accounting.sageone.co.za/api/2.0.0/Help`).

| `settings.erp` key | Secret | Meaning |
|---|---|---|
| `api_key` | **yes** | Integrator API key from Sage's developer programme |
| `username` | no | The Sage login email of the user the app acts as |
| `password` | **yes** | That user's Sage password |
| `company_id` | no | Numeric Sage company id (`Company/Get` lists them) |
| `base_url` | no (optional) | API base; default `https://accounting.sageone.co.za/api/2.0.0`. Admin-supplied and handed the API key and the Sage password, so https-only, on `accounting.sageone.co.za` only (any other host or port → `SageZaConfigError`), and SSRF-guarded. The operator override `FEOH_ERP_SAGE_ZA_API_BASE` (fake-erp) skips all three |
| `home_currency` | no (optional) | ISO code of the company's home currency, default `ZAR` — the API reports currencies only as numeric ids and a symbol |

Every call is `<Resource>/<Method>?apikey=…&companyid=…` with the Sage login in
HTTP basic auth. List methods page with OData `$top` / `$skip` (100 rows, 10
pages) and filter with `$filter`.

| Operation | Calls |
|---|---|
| `post_invoice` | refuse pre-flight without a numeric `vendor_erp_id` (SupplierId) / numeric GL account ids, or in a non-home currency → `SupplierInvoice/Get` idempotency lookup → `Supplier/Get/{id}` (+ `Company/Get/{id}`) currency check → `Account/Get` + `TaxType/Get` → `SupplierInvoice/Save` |
| `get_invoice_status` | `SupplierInvoice/Get/{id}` — `AmountDue` 0 ⇒ paid, below `Total` ⇒ partially paid, else open |
| `void_invoice` | `SupplierInvoice/Delete/{id}`, only when `AmountDue == Total` and the invoice is neither `Locked` nor `Paid`; otherwise `False` (reverse it in Sage with its payment) |
| `list_vendors` / `list_gl_accounts` / `list_pos` | `Supplier/Get` / `Account/Get` (active only; category → asset/liability/equity/revenue/expense, unknown → unclassified) / `PurchaseOrder/Get` (`currency` left NULL — decisions §197) |
| `test_connection` | `Company/Get` answers *and* lists the configured `company_id` |

- **Lines** are `LineType` 1 (account) with `SelectionId` = the account id, one
  per `bill_lines` line, posted **VAT-inclusive** (`Inclusive: true`, quantity 1,
  the gross as `UnitPriceInclusive`, exclusive rounded half-up with the tax the
  remainder, so each line adds back exactly). Decimals go on the wire as exact
  JSON number literals, never through `float`.
- **VAT** uses the account's `DefaultTaxTypeId`, else the company's default
  `TaxType`; the percentage is always Sage's. No resolvable (non-manual) tax
  type → `tax_type_not_resolved`. When the payload carries `tax_amount` and
  Sage's VAT would differ by more than a cent per line → `tax_mismatch` (a
  zero-rated invoice coded to a standard-rated account would otherwise claim
  input VAT never charged). A payload with **no** `tax_amount` states no VAT,
  which is not "any VAT": it posts only when every line's resolved rate is 0%,
  else `tax_not_stated`.
- **Idempotency**: `Reference` carries our `correlation_id`. The lookup filters
  `SupplierId eq N and (Reference eq '<corr>' or DocumentNumber eq '<inv>')`
  (OData quotes doubled). Our reference with the same `Total` is adopted; with
  another total → `correlation_total_mismatch`; the same invoice number under
  another reference → `duplicate_invoice_number`. A failed lookup is a
  retryable failure, never read as "not posted". If Sage saves a different
  `Total` than approved, the invoice just saved is deleted (when nothing is
  allocated to it) and the push fails non-retryable `posted_total_mismatch`; a
  save that reports no `Total` fails non-retryable `posted_total_unconfirmed`
  (never success — `erp_adapters/posted_total.py`, shared with QuickBooks, Xero
  and Sage v3.1).
- **Refusal reasons** (all non-retryable): `vendor_not_linked`,
  `account_not_linked` (also an unknown or inactive account),
  `currency_not_supported`, `foreign_currency_supplier`,
  `tax_type_not_resolved`, `tax_mismatch`, `tax_not_stated`,
  `correlation_total_mismatch`, `duplicate_invoice_number`, plus the shared
  line codes `line_amount_missing` / `amount_mismatch` / `tax_not_itemised`
  from `bill_lines`.
- **Credentials.** The API key rides in the query string, so the module
  registers `apikey=` with the shared `erp_adapters/log_redaction.py` filter
  (the same filter SYSPRO uses), which sits on the `httpx` logger and on each
  `httpcore` logger by name (`REDACTED_LOGGERS` — a logger's filters never see
  its children's records), installed by `app/main.py` at import: any logged URL
  containing it has its query replaced with `?[redacted]`. Transport errors are re-raised as
  `SageZaError("Sage Accounting (ZA) <step> failed: <ExceptionClass>")` from
  `None`. The password only travels in the basic-auth header. Failure messages
  use `erp_failure_message` and never echo a response body.

Tests: `backend/tests/test_erp_sage_accounting_za_adapter.py`. fake-erp surface:
`/sageza/api/2.0.0` (`FEOH_ERP_SAGE_ZA_API_BASE`).

## Blackbaud Financial Edge NXT direct adapter (`blackbaud_fe_nxt`)

`erp_adapters/blackbaud_fe_nxt.py`, selected by `settings.erp = {"type":
"blackbaud_fe_nxt", "integration_method": "direct", ...}`. Financial Edge NXT
is Blackbaud's fund-accounting system for nonprofits, reached through the **SKY
API** (`https://api.sky.blackbaud.com`). It is the first adapter on the shared
OAuth 2.0 authorization-code flow: it subclasses `OAuthErpAdapter`, registers
its `OAuthProviderSpec` with `services/erp_oauth` at import, and gets every
bearer token from `await self.access_token()` — it never reads, refreshes or
stores a token.

Every call carries **two** credentials: the bearer token, and the SKY developer
subscription key as `Bb-Api-Subscription-Key`. The key belongs to the developer
account, not to a customer environment, so one platform key serves every
tenant; the environment is burned into the token.

| Setting | Secret | Meaning |
|---|---|---|
| `FEOH_ERP_BLACKBAUD_CLIENT_ID` / `_CLIENT_SECRET` | secret: yes | Platform SKY application (one app, every tenant). Empty → unavailable |
| `FEOH_ERP_BLACKBAUD_SUBSCRIPTION_KEY` | **yes** | Platform subscription key. Empty and no tenant key → every post refuses `subscription_key_missing` before any call |
| `settings.erp.ap_account_number` | no | AP liability account (`01-2000-00`) the invoice's Credit distribution posts to — **required** |
| `settings.erp.currency` | no | ISO code of the FE NXT ledger — **required**; an invoice in another currency is refused |
| `settings.erp.subscription_key` | **yes** (optional) | Tenant key overriding the platform one |
| `settings.erp.client_id` / `client_secret` | secret: yes (optional) | Tenant-owned SKY application (read by `erp_oauth`) |
| `settings.erp.project_id` | no (optional) | FE NXT project (`ui_project_id`) on every distribution split |
| `settings.erp.transaction_code_values` | no (optional) | `[{"id", "value"}, …]` on every split, in FE NXT's code order |
| `settings.erp.approval_status` | no (optional) | `Pending` / `Approved`; omitted otherwise (FE NXT's default applies) |
| `settings.erp.oauth` | **yes** | Written only by `erp_oauth`; `external_tenant_id` = the token response's `environment_id` |

| Operation | Call |
|---|---|
| `post_invoice` | pre-flight refusals → `GET /accountspayable/v1/invoices?search_text=<number>` → `POST /accountspayable/v1/invoices/process` → `GET …/backgroundProcess/{id}/status` (≤ 10 reads, 1 s apart) → `…/result` |
| `get_invoice_status` | `GET /accountspayable/v1/invoices/{id}`: `Pending` draft, `Approved` open (balance 0 ⇒ paid), `PartiallyPaid`, `Paid`, `Deleted` cancelled |
| `void_invoice` | not automated (`False`): the AP API has no invoice delete, and cancelling a posted payable is an adjustment in an open period |
| `list_vendors` / `list_pos` | `GET /accountspayable/v1/vendors` / `/purchaseorders`, 100 × 10 pages; template and deleted POs skipped; PO `currency` stays NULL |
| `list_gl_accounts` | `GET /generalledger/v1/accounts`, keyed by `account_number` (what AP distributions take); `prevent_data_entry` accounts skipped; `account_type` unclassified (FE NXT's `class` is a net-asset class) |
| `test_connection` | an `environment_id` was captured at consent + `GET /accountspayable/v1/vendors?limit=1` |

Request rules worth knowing:

- **Balanced distributions (fund accounting).** One `Debit` per line from
  `bill_lines.bill_lines` (a negative line becomes the same amount on the
  `Credit` side) plus one `Credit` of `payload.amount` to `ap_account_number`.
  Each distribution has one split at `percent: 100`. Project and transaction
  codes are sent only as configured — never derived from `cost_center` or
  guessed; when FE NXT's account setup requires one we lack, its 400
  (`invalid_request`) surfaces and the fix is tenant config.
- **Dates.** `invoice_date` and `due_date` are required by FE NXT and refused
  when missing; `post_date` is the invoice date. `payment_details` is sent
  empty, so the vendor's payment defaults apply.
- **Asynchronous create.** The synchronous `POST /invoices` is deprecated in
  favour of the background-process job. Once FE NXT has accepted a job, any
  outcome we cannot confirm — still running after the bounded poll, or a
  429 / 403-quota / error while asking — is **non-retryable** `job_unconfirmed`:
  `services/erp` backs off for seconds and a re-submit while the first job runs
  would create a second invoice. A job that reports canceled/failed is
  retryable. The unconfirmed result carries the job's `process_id` as
  `ErpPostResult.pending_job_id`; `services/erp._call_erp` keeps it on
  `WorkflowInstance.state_data["erp_pending_job_id"]` and hands it back as
  `InvoicePayload.pending_job_id` on the next attempt. An operator's retry
  therefore polls that job first: completed → its invoice is the result (and
  the stored job is cleared); still running or unreadable → `job_unconfirmed`
  again, nothing queued; canceled/failed → the normal lookup-then-post path.
- **Transport errors are "unconfirmed", never retryable.** Any
  `httpx.HTTPError` (timeout, connect, read) once the create has been sent is
  `job_unconfirmed`, not an exception for `services/erp` to back off and
  re-send. On a status or result read the `process_id` is known and is kept
  as `pending_job_id`. On `POST /invoices/process` itself nothing names the
  job, yet a timeout can land after FE NXT queued it — so the result carries
  no job and the operator's retry runs the correlation-marker lookup before
  posting, which returns the invoice once that job has finished. A 2xx create
  whose body is not readable is treated the same way. Residual: a manual retry
  issued while such an unnamed job is *still running* finds nothing and posts;
  the message says the retry searches first, so wait for the job before
  retrying.
- **Idempotency.** `correlation_id` rides in the description as
  `[feoh:<id>]` (description bounded to 60 characters so the marker survives).
  A row with the same vendor and invoice number, not `Deleted`, carrying our
  marker is the earlier attempt; one without it is refused as
  `duplicate_invoice_number`. A failed or truncated lookup is a failure.
- **Rate limits.** 429, and 403 with `Retry-After` (SKY's quota signal), map to
  `rate_limited` from status and headers only. Nothing sleeps on them.
- **Money** goes out through `dumps_exact_json` and is read back with
  `parse_float=Decimal`.
- **Refusal reasons:** `vendor_not_linked` (also a non-numeric vendor id),
  `account_not_linked`, `subscription_key_missing`,
  `ap_account_not_configured`, `currency_not_configured`, `currency_mismatch`,
  `invoice_date_missing`, `due_date_missing`, `amount_not_positive`,
  `duplicate_invoice_number`.

Tests: `backend/tests/test_erp_blackbaud_adapter.py`. fake-erp surface:
`/blackbaud` (`FEOH_ERP_BLACKBAUD_API_BASE`, `FEOH_ERP_BLACKBAUD_TOKEN_URL`).

## Business Central: chart, POs and void

`erp_adapters/dynamics_365_bc.py`, API v2.0
(https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/).

| Operation | Call |
|---|---|
| `post_invoice` | refuse without `vendor_erp_id` / a line account id / a valid `base_url` → `GET purchaseInvoices?$filter=externalDocumentNumber eq '…'` (a non-200 fails the push) → `POST purchaseInvoices` (a draft) → `GET purchaseInvoices({id})` → total check → `POST purchaseInvoices({id})/Microsoft.NAV.post` |
| `list_gl_accounts` | `GET accounts` — `number` → code, `displayName` → name, `id` → `erp_account_id`, `category` Assets/Liabilities/Equity/Income/Cost of Goods Sold/Expense → asset/liability/equity/revenue/expense/expense (blank → unclassified). Heading / total accounts and blocked accounts are skipped. |
| `list_pos` | `GET purchaseOrders?$expand=purchaseOrderLines` — total `totalAmountIncludingTax`; status always `open`; `currencyCode` only when non-blank; `requestedReceiptDate` unless BC's blank `0001-01-01`; comment lines dropped |
| `list_vendors` | `GET vendors` |
| `void_invoice` | `GET purchaseInvoices({id})`; a `Draft` is deleted (`DELETE` with `If-Match` = the etag just read); anything else returns `False` |

**Lines post on `accountId`, not the No.** The chart sync is now the writer of
`gl_accounts.erp_account_id` for BC, and `services/erp._resolve_erp_refs`
resolves those ids through the invoice entity's chart
(`gl_chart.resolve_erp_account_ids`). Posting the No. instead would bypass that
resolution: a code typed locally but never synced, or an entity's own account
overriding a shared one, would post against whatever BC account happens to hold
that No. — or fail inside BC instead of refusing up front. The id also survives
a renumbering in BC. Lines follow NetSuite's account rule (uncoded → header
account; coded but unlinked → `account_not_linked`) and its amount rule: every
line needs an amount (total, else quantity × unit price; none →
`line_amount_missing`) and the lines must sum to exactly the approved amount
(else `amount_mismatch`) — BC totals the bill from its lines, so lines that
disagree would post a different figure, and collapsing them onto one header
line would move coded expense onto the header's account. Only an invoice with
no line items posts one line for the amount on the header account. A line keeps its
quantity and unit cost only when they multiply to its amount exactly; otherwise
it goes as 1 × amount.

**The draft's total is checked before it is posted.** BC computes a
purchaseInvoice's total itself, and a VAT / sales-tax company adds tax on top
of the lines — 1,200 approved would book 1,440, and BC's own payment run would
overpay. So after the create the adapter re-reads the draft and runs
`Microsoft.NAV.post` only when `totalAmountIncludingTax` (parsed exactly, never
via float) equals `payload.amount`. Otherwise — or when BC states no total — the
draft, still unposted, is deleted (`If-Match` = the etag just read) and the push
refused non-retryably: `Business Central post refused: posted_total_mismatch`. A
failed delete leaves a draft, never a ledger entry, and the refusal stands; a
later manual retry finds it and checks again. A failed draft read or post step
is a **retryable** `erp_failure_message` failure. The post step used to be
`except Exception: pass`, reporting success — and moving the invoice to
`sent_to_erp` — while BC held only a draft. Success is reported only for an
invoice BC holds as `Open` or `Paid`.

**POs are always `open`.** The API's statuses are `Draft`, `In Review` and
`Open`; BC deletes a purchase order once it is fully received and invoiced, so
a closed or cancelled one never appears in the collection. A blank
`currencyCode` is BC's local currency and stays NULL rather than a guessed code
(decisions §197). A PO with no stated total is skipped, never synced at 0.

**Void deletes drafts only.** A purchaseInvoice has `DELETE` and one bound
action, `Microsoft.NAV.post`
([resource](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/dynamics_purchaseinvoice),
[delete](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/api/dynamics_purchaseinvoice_delete)).
There is no cancel or corrective-credit-memo action on the purchase side, so a
posted invoice is reversed in BC by an accountant (corrective credit memo) and
`void_invoice` returns `False` for it.

**Paging.** Every list sync sends `Prefer: odata.maxpagesize=100` (BC's own
server page is 20,000 rows), follows `@odata.nextLink`, and stops at 10 pages /
1,000 rows. Bodies are parsed with `utils/json_money.loads_exact_json`, so a
total is a `Decimal` from the wire, never a float. Best-effort like the other
adapters: a token failure, non-200 or network error ends the pull with what it
read. An admin `base_url` stays behind the SSRF guard on every call, and that
refusal is raised, not swallowed.

## NetSuite: SuiteQL syncs and void

`erp_adapters/netsuite.py`. Every list sync is one SuiteQL query
(`POST …/services/rest/query/v1/suiteql`, `Prefer: transient`, `ORDER BY id`
for stable `offset` paging, 10 pages × 100 rows), parsed exactly like BC's.

| Sync | Query | Notes |
|---|---|---|
| `list_vendors` | `SELECT id, entityid, companyname, email, phone, BUILTIN.DF(terms) AS terms, isinactive FROM vendor` | The REST `/vendor` collection returns ids and links only — no names — so the old record-API sync could not have named a vendor. Name = `companyname`, else `entityid` (an individual); code = `entityid`; inactive vendors skipped. |
| `list_gl_accounts` | `SELECT id, acctnumber, fullname, accttype, isinactive FROM account` | unchanged (§ ERP references) |
| `list_pos` | `transaction` where `type = 'PurchOrd'`, `foreigntotal`, `currency.symbol`, `TO_CHAR(duedate, 'YYYY-MM-DD')` | Headers only — the PO sync stores no ERP lines. Status letter C → cancelled, G / H → closed, else open. Dates go through `TO_CHAR` because SuiteQL formats them per the user's preference. |

**Void.** The REST record service has no void: its record actions name neither
`vendorBill` nor a void action
([supported record actions](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1516982564.html));
`transaction.void` is SuiteScript only
([N/transaction](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4413162576.html)),
reachable through a customer-deployed RESTlet. REST does offer
[`DELETE /record/v1/vendorBill/{id}`](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1545142287.html).
`void_invoice` deletes a bill still `Pending Approval` (nothing posted to the
GL) and returns `False` for any approved bill, whose reversal — void, reversing
journal, or vendor credit — is the accountant's call.

**Status mapping** reads `status.id` (`paidInFull`) or `status.refName` with
its spaces removed (`Paid In Full`). It used to lower-case `refName` only, so a
real paid bill (`"paid in full"`) never matched `paidinfull` and polled as
`unknown`.

fake-erp serves all of the above: BC `accounts` (three posting accounts plus a
heading and a blocked one), `purchaseOrders` (`PO-FAKE-BC-401` in local
currency with a blank date, `PO-FAKE-BC-402` in EUR), `DELETE
purchaseInvoices({id})` (needs `If-Match`, drafts only), a 400 for an
`Account` line whose `accountId` is not a posting account, `totalAmount*` on
every purchaseInvoice (company `fake-vat-co` adds 20% VAT on top of the lines),
and a 400 for posting a non-draft; NetSuite SuiteQL
`vendor` and `transaction` rows (`PO-FAKE-NS-501` open USD, `PO-FAKE-NS-502`
closed GBP) and `DELETE vendorBill/{id}` (pending approval only).

Tests: `test_erp_gl_sync.py`, `test_erp_po_sync.py`,
`test_erp_vendor_sync_adapter.py` (mapping, 1000-row bound, degradation),
`test_erp_void_invoice.py` (both voids, NetSuite status shapes),
`test_erp_adapter_idempotency.py` (BC lines, failed lookups, the draft total
check, draft resumption, a failed post step; NetSuite line sums),
`test_erp_adapter_error_pii.py` (BC `account_not_linked`),
`test_erp_base_url_overrides.py` (BC `base_url` host allowlist + SSRF guard,
NetSuite `account_id` validation), and `frontend/tests-e2e/erp/dynamics.spec.ts`
(the 1,200 / 20% VAT refusal against fake-erp).
## Connecting an OAuth ERP (QuickBooks Online, Xero, Sage Accounting, Blackbaud)

These ERPs have no client-credentials grant: the customer's admin consents in
the provider's UI. One flow serves all of them (`services/erp_oauth.py`,
routes in `api/erp_oauth.py`). An adapter subclasses
`erp_adapters/oauth_base.OAuthErpAdapter`, registers an `OAuthProviderSpec`,
and calls `await self.access_token()`.

**Routes**

| Route | Auth | Contract |
|---|---|---|
| `GET /api/organization/erp/oauth/{provider}/authorize` | admin + `erp_integrations` plan | `200 {"authorize_url"}`; the page navigates the browser to it. `404` unknown provider, `409` no app configured, `402` plan. |
| `GET /api/erp/oauth/callback` | public; signed single-use `state` | Exchanges the code, writes `settings.erp = {type, integration_method: "direct", oauth}`, and 302s to `<tenant origin>/organization?section=erp&erp_connected=<provider>` or `&erp_error=<code>`. Codes: `invalid_state` (forged: plain 400, no redirect), `state_expired` (replayed or expired), `access_denied`, `not_authorized` (the admin who started it is no longer an active admin), `plan_required`, `missing_code`, `provider_unavailable`, `token_exchange_failed`, `no_external_tenant`, `already_linked` (the company is connected to another tenant, or another connect for it is in flight: the uniqueness check and the write run under a Redis lock on `(provider, company id)`, `erp_oauth.realm_claim`, because the org row lock serialises one tenant only), `unknown_provider`. |
| `POST /api/organization/erp/oauth/disconnect` | admin (not plan-gated) | Clears `settings.erp.oauth`, then revokes best-effort: `{"disconnected", "revoked"}`. |
| `GET /api/organization/erp/oauth/status` | admin | `{provider, connected, needs_reconnect, external_tenant_id, expires_at, refresh_token_expires_at, connected_at, redirect_uri, providers: [{key, display_name, available, client_source}]}`. Never a token. |

Connect and disconnect audit `organization.erp_connected` /
`organization.erp_disconnected` (provider and client source, no tokens).

The callback's query (`code`, `state`, `realmId`) never reaches the uvicorn
access log: `app/utils/access_log.py` strips the query of every path in
`SENSITIVE_QUERY_PATHS` from `uvicorn.access` records, installed by
`app/main.py` where app logging is configured (`tests/test_access_log_redaction.py`).

**Which app.** The tenant's own (`settings.erp.client_id` / `client_secret`
while `settings.erp.type` names the provider) wins over the platform's `FEOH_`
app. With neither, the provider is unavailable. The consent's source is
recorded, and the refresher uses the same one.

**Storage and refresh.** `settings.erp.oauth` holds the tokens plus `provider`,
`org_id` and a random `connection_id`. It is `ALWAYS_REDACTED` from the
settings response, refused on `PATCH /api/organization`, and carried across an
`erp` save that keeps the same type. The refresher re-reads the stored block
(never the config's copy) and requires the config's `provider` and
`connection_id` to match it, so an admin-supplied `test-erp` config naming
another org's id reaches nothing. It serialises refreshes per connection with
a Redis lock and persists the rotated refresh token by compare-and-swap. A
provider `invalid_grant` marks `needs_reconnect`. An outage raises
`ErpTokenRefreshError` and leaves the connection alone.

**Per-provider hooks on the spec.** The company id comes from
`external_tenant_id_param` (a callback query parameter: QBO `realmId`), from
`external_tenant_id_token_field` (a token-response field: Blackbaud
`environment_id`), or from an override of
`OAuthErpAdapter.resolve_external_tenant_id` (Xero `GET /connections`, Sage's
business lookup). `extra_token_headers(erp_settings)` adds headers to every
token and revoke call (Blackbaud's subscription key). `token_auth` is `basic`
or `body`. An empty `scopes` sends no `scope`. Operator URL overrides
(`*_url_setting`) are read on every call.

**QuickBooks Online** (`erp_adapters/quickbooks_online.py`). It refuses before
any post, with `QuickBooks Online post refused: <reason>`: `vendor_not_linked`,
`doc_number_too_long` (21 characters), `currency_not_enabled`,
`currency_unknown`, `not_connected`, and the shared line codes from
`bill_lines` — `account_not_linked` (a coded line is never moved onto the
header's account), `line_amount_missing`, `amount_mismatch` /
`tax_not_itemised` (the lines must sum to the header amount, because QuickBooks
re-totals from lines). After the create, QuickBooks' own `TotalAmt` must equal
the approved amount: a different one deletes the bill just created and fails
non-retryable `posted_total_mismatch`; a missing one fails non-retryable
`posted_total_unconfirmed`. The idempotent re-find applies the same check.
Posting is idempotent through `requestid=<correlation id>` plus a pre-check on
DocNumber + vendor + `PrivateNote: "FeohLedger <correlation id>"`. A 401
refreshes once and retries. `void_invoice` deletes only a bill with no payment
applied. Locally, `pnpm erp:up` starts fake-erp, which serves `/qbo/oauth2/*`
(with rotating refresh tokens) and `/qbo/v3/company/fake-realm-1/*`;
`POST /__qbo/set-balance` marks a bill paid.
