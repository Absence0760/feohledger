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

The ERP type determines which adapter is used. Auth credentials are encrypted at rest (future: use a secrets manager).

## Per-ERP Integration Details

### 1. Microsoft Dynamics 365 Business Central

**API:** OData v4 REST API
**Base URL:** `https://api.businesscentral.dynamics.com/v2.0/{tenant}/{environment}/api/v2.0`
**Auth:** OAuth 2.0 client credentials

**Create Purchase Invoice:**
```
POST /purchaseInvoices
{
  "vendorNumber": "V10000",
  "invoiceDate": "2026-04-01",
  "dueDate": "2026-05-01",
  "vendorInvoiceNumber": "INV-2024-001",
  "purchaseInvoiceLines": [
    {
      "lineType": "Item",
      "lineObjectNumber": "1000",
      "quantity": 10,
      "unitCost": 25.00
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
| `dynamics_365_bc` | Pre-create lookup: `GET purchaseInvoices?$filter=externalDocumentNumber eq '<correlation_id>'`. Same short-circuit shape as NetSuite. |

The local `fake-erp` mock implements the matching server-side behavior (a
merge-idempotency-key cache; `q=`/`$filter=` collection queries filtered by
`externalId`/`externalDocumentNumber`) so this is exercised end-to-end by
`pnpm test:erp` without a live ERP account. Unit-level coverage (mocked HTTP,
no fake-erp container needed) lives in
`backend/tests/test_erp_adapter_idempotency.py`.

## Retry Logic

```
Attempt 1: immediate
Attempt 2: wait 30 seconds
Attempt 3: wait 2 minutes
Attempt 4: wait 10 minutes
After 4 failures: mark as failed, require manual retry
```

Manual retry available via `POST /api/invoices/{id}/retry-erp` (resets the attempt counter).

## Security

- ERP credentials stored in `Organization.settings` JSONB (encrypted at rest via PostgreSQL column encryption — future)
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
| `FEOH_ERP_D365_API_BASE` | (empty) | Empty → the admin-config `base_url` + SSRF guard; set → used verbatim. Dev value: `http://localhost:12112/d365`. |
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
(`list_gl_accounts`), vendor sync (`list_vendors` — Merge.dev only for now,
asserting the full field mapping — name/email/phone/address/tax id/payment
terms — not just that the sync call didn't error), and a full send-to-ERP
round trip to a terminal invoice status.

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
4. Import the module in `_call_erp()` in `erp.py` to trigger registration
5. Add the ERP type to the frontend `ERP_TYPES` array in the organization page
6. Add conditional credential fields in the ERP config UI

## Setup Guide

### Using Merge.dev (Recommended)

1. Create a [Merge.dev](https://merge.dev) account
2. Get your API key from the Merge dashboard
3. Have your customer connect their ERP via Merge Link — this creates an account token
4. In your app: go to **Organization > ERP Integration**
5. Select your ERP system, keep "Merge.dev (Unified API)" as the method
6. Enter the API key and account token
7. Save

### Using Direct Adapters

1. Go to **Organization > ERP Integration**
2. Select the ERP system (e.g., Business Central)
3. Change method to "Direct API Connection"
4. Enter the ERP-specific credentials (shown dynamically based on ERP type)
5. Save

### Merge.dev Pricing

Merge.dev is **not free**. Pricing is per linked account (per customer ERP connection):
- **Launch**: Free for first 5 linked accounts (good for development)
- **Professional**: Starts at $650/month
- **Enterprise**: Custom pricing

For development and testing, the free tier is sufficient. See [merge.dev/pricing](https://merge.dev/pricing).

## Implementation Status

| Phase | Status |
|---|---|
| Adapter interface + dispatcher | Done |
| Mock adapter | Done |
| Merge.dev adapter | Done |
| Business Central direct adapter | Done |
| NetSuite direct adapter | Done |
| Webhook endpoint | Done |
| ERP config UI in org settings | Done |
| Post-ERP statuses (posted_in_erp, payment_scheduled, paid) | Done |
| Polling job for status sync | Planned |
| Remaining direct adapters (SAP, Epicor, etc.) | Use Merge.dev |
| Test connection button in UI | Planned |
| ERP status display in invoice modal | Planned |
