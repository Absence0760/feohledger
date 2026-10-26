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
| 3 | Oracle NetSuite | REST / SuiteTalk SOAP | OAuth 2.0 client credentials (M2M); Token-Based Auth (TBA) for integrations created before 2027.1 |
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

    async def list_pos_report(self) -> PoListing:
        """What the PO sync calls: `PoListing(pos, skipped)`.

        The default wraps `list_pos` and reports nothing skipped, so
        an adapter that never leaves a PO out needs no change. An
        adapter that DOES skip a PO because it cannot state its
        total exactly overrides this, names each one as
        `SkippedPo(po_number, PoSkipReason)` and keeps `list_pos`
        returning `.pos` (decisions §323). SYSPRO does.
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
        Implemented by all three real adapters — `merge_dev` (supplier
        Contacts at `GET /contacts?is_supplier=true`, cursor-paginated;
        Merge has no `/vendors`), `netsuite` (`GET /vendor`), and
        `dynamics_365_bc` (`GET .../vendors`) — plus `mock`.
        Covered by the local fake-erp e2e suite (`pnpm test:erp`); see
        "Local e2e testing" below.
        """
        ...

    async def test_connection(self) -> bool:
        """Verify the ERP connection is working."""
        ...

    # Status sync-back + payment write-back (decisions §295). Only an adapter
    # with `supports_status_sync = True` implements them (QuickBooks Online
    # today); the base raises NotImplementedError and every caller checks the
    # flag first.
    supports_status_sync: bool = False
    async def bill_report(self, bill_id: str) -> ErpBillReport: ...
    async def bills_for_payment(self, payment_id: str) -> list[str]: ...
    async def changed_bills(self, since: datetime) -> list[str]: ...
    async def post_payment(self, payload: PaymentPayload) -> ErpPostResult: ...
    async def void_payment(self, payload: PaymentPayload) -> ErpPostResult: ...
```

See § QuickBooks status sync-back and payment write-back.

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
    ├── success ──> sent_to_erp    (ERP accepted, document ID stored on the invoice)
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
| **Polling** | Background job checks ERP for status every N minutes | Fallback for ERPs without webhook support — **designed, not built** for the generic case (below); QuickBooks Online has its change-data-capture sweep (§295) |

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
payment-blocking exception, a live FeohLedger payment or card — opens a
reconciliation exception instead, whose sentence tells an AP user what stopped
it and what to do, and whose `erp_report` keeps the ERP's payment as data (rail,
reference, date, ids, refusal code). Clearing the blocker records it
automatically; a blocking row is recorded in one click from its exception
(decisions §319). See `backend/docs/payments.md` § The ERP reports an invoice
paid that FeohLedger never paid.

**ERP reconciliation rows say what to do; machine detail is data.** Every
reconciliation exception the ERP webhook and status sync open for a reported
payment carries an AP-user sentence in `description` (no API paths, no ids, no
refusal codes) and the machine facts in `exceptions.erp_report` (migration
0121): `kind` `paid_not_recorded` / `settled_short` / `voided_after_post`, the
ERP type, document id and event id, plus per kind the payment facts, the
settling payment and shortfall, or the ERP status. The ERP's product name in
those sentences comes from `erp_status_sync.erp_product_name` ("Your ERP" when
an adapter has none).

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

**Polling job — not built.** Every adapter implements
`get_invoice_status(erp_document_id)` (and fake-erp's `__set-status` /
`__set-balance` hooks can drive it), but **nothing under `app/` calls it**: there is
no background sweep, so an ERP without a webhook never reports a status change
back, and nothing times a stuck `sent_to_erp` invoice out. The webhook above is the
only inbound path today. The intended design was a sweep every few minutes over
`sent_to_erp` invoices that transitions on a changed status and fails one stuck
past a timeout. The adapters' reads are ready for it: since decisions §303 `paid`
means the ERP's own paid status or a payment it records, never a zero balance
alone, and a bill with nothing owing but no visible payment reads
`settled_no_payment` (for a person, never recorded). Tracked in
`docs/followups.md` ("A bill paid in any ERP but QuickBooks never reaches
FeohLedger"). QuickBooks
Online is the exception: its status comes back through its own webhook and a
change-data-capture sweep (next section).

**A paid report may carry the ERP's own payment date and rail.** The generic
body accepts optional `paid_on` (`YYYY-MM-DD`) and `payment_method` (a
`Payment.method` rail). They are validated like the user's own "paid outside
FeohLedger" record — a real date not in the future, a recordable rail, never
`virtual_card` — and an invalid or absent value falls back to the arrival date
and NULL (1099-reportable) rather than refusing the report (decisions §295).

### QuickBooks status sync-back and payment write-back

Decisions §295; the phase-by-phase account is
`quickbooks-online-adapter.md` § Phases 3–5 as built.

- **Where a push lands.** `services/erp_status_sync.status_sync_active`: with
  `FEOH_ERP_STATUS_SYNC_ENABLED` on and an adapter that `supports_status_sync`,
  a confirmed push goes `sent_to_erp → posted_in_erp` — payable here, so a
  payment made in the ERP can move it to `paid` and out of the queue. Otherwise
  it closes at `done`, as every other adapter does (the ERP owns payment, and
  nothing would tell us it paid). The push records `state_data.erp_realm`.
- **Inbound.** `POST /api/erp/webhook/quickbooks` (Intuit CloudEvents, one app
  verifier token, base64 HMAC over the raw body, dedupe by event id before the
  204, work after it) and the `erp-status-sync` CDC sweep both end in
  `apply_bill_report`, which records a bill paid by a person in QuickBooks
  through the ERP-paid path (with QuickBooks' date and rail), and opens the
  payment-blocking `payment_reconciliation` when FeohLedger would otherwise pay
  a bill QuickBooks paid, part-paid or deleted. The tenant is found through the
  control-plane realm index (`uq_organizations_erp_oauth_company`, migration
  0117), which also makes "one tenant per QuickBooks company" a database rule.
- **How a bill's payments are found.** A Bill names each BillPayment applied
  to it in its `LinkedTxn`, typed by how it was paid: `BillPaymentCheck` (a
  bank/cheque payment, which is what Pay Bills from a bank account records) or
  `BillPaymentCreditCard`, never the entity name `BillPayment`. One definition,
  `quickbooks_online._BILL_PAYMENT_TXN_TYPES` read through `_bill_payment_ids`,
  serves `bill_report` (cash vs vendor credit, and which payments are ours),
  `get_invoice_status`, and the write-back's idempotency lookup and void.
  Reading the wrong type finds no payment at all, so a bill paid in cash reads
  as settled by a vendor credit and our own write-back reads as a payment made
  outside FeohLedger (decisions §317).
- **Outbound.** `payment_erp_sync` writes a settled payment back as a
  BillPayment (`services/erp_payment_writeback`), idempotent on the payment id,
  from the org's mapped pay-from account (`settings.erp.bill_payment_bank_account`
  / `bill_payment_card_account`, our GL codes). Refusals are coded
  (`payment_account_not_mapped`, `bill_not_in_erp`, `payment_exceeds_balance`,
  `bill_payment_voided`, `currency_mismatch`, and the Intuit Fault codes) and
  land on `payment.erp_post_failed` plus an `erp_reconciliation` exception. A
  payment void voids the BillPayment too.
- **Checked again at dispatch.** A bill paid in QuickBooks reaches us within
  seconds by webhook, or by the next sweep — too late for a run already
  executing. So every payment re-reads its bill from QuickBooks immediately
  before its rail is called, under the invoice row lock
  (`erp_status_sync.erp_bill_dispatch_refusal`, decisions §296): not open →
  refused `erp_bill_not_open:<state>` with the blocking
  `payment_reconciliation`; unreadable → held `erp_bill_unverified`, never paid
  blind. The NACHA export runs the same read.

### Live QuickBooks contract check

The QuickBooks counterpart of § Live Xero contract check, and of the recorded
replay (`quickbooks-online-adapter.md` § Recorded payment shapes, decisions
§318). `pnpm test:erp:qbo-live` (`backend/tests/test_qbo_live_contract.py`)
is opt-in (`FEOH_QBO_LIVE=1`; every other run skips it), local only, and
read-only: every request to the company is a GET.

It asserts that QuickBooks still:

- links a bill's payments only with the `LinkedTxn` types the reader matches
  (`_BILL_PAYMENT_TXN_TYPES`; decisions §317);
- uses only the `PayType`s we map to a rail, and applies BillPayments only to
  bills, vendor credits and journal entries;
- serves each linked BillPayment, and its lines point back at the bill
  (`bills_for_payment`);
- reads a bill a person paid wholly in cash as paid outside FeohLedger, not by
  credit, with a rail and a date (`bill_report`);
- answers change-data-capture in the shape the sweep reads, and an unknown bill
  with Fault 610.

**To run it:** a real Intuit development app's keys in `backend/.env` with the
`FEOH_ERP_QBO_*` fake-erp overrides blanked; the local `acme` tenant (or
`FEOH_QBO_LIVE_TENANT`) connected to a sandbox company; a few bills paid there
by hand with Pay Bills, from a bank account and from a credit card; the backend
venv active. Opted in without a connection, the module fails rather than skips.
It never runs in CI, for Xero's reasons. Run it before a release, and whenever
the recorded fixtures or the fake's QuickBooks shapes change.

## Organization ERP Configuration

The block's CONFIGURATION is stored in `Organization.settings` JSONB under
the key `erp`; its SECRETS are not (see § Where the credentials live, below).
An illustrative shape, secrets included as the adapters receive them:

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

### Where the credentials live

The secret fields of the block — every catalogue field marked `secret`
(`erp_adapters/catalog.SECRET_KEYS`: `api_key` and `account_token` for Merge.dev
and the Sage ZA API key, `client_secret` for Business Central, Intacct and any
bring-your-own OAuth app, `consumer_secret` / `token_secret` for NetSuite TBA,
`private_key` (the certificate key that signs the OAuth 2.0 client assertion) for
NetSuite OAuth 2.0,
`password` for Sage ZA, `operator_password` / `company_password` for SYSPRO,
`subscription_key` for Blackbaud) plus `webhook_signing_secret` (inbound ERP
webhooks, older spelling `webhook_secret`) — are **not** in
`Organization.settings`, and neither are the OAuth access and refresh tokens. They are envelope-encrypted in the control-plane
`provider_credentials` table, one sealed row per org and block
(`services/provider_credentials.py`, catalogue `SECRET_FIELDS`;
`services/credential_crypto.py` for the crypto):

- **Sealing.** Each write draws a fresh AES-256 data key, encrypts the block's
  secrets with AES-256-GCM, and stores the data key wrapped by the app KMS key
  (`FEOH_CREDENTIAL_KMS_KEY_ID`, AWS KMS `GenerateDataKey`/`Decrypt`). The
  organization id and block are bound in as the KMS encryption context and the
  GCM associated data, so a row copied to another tenant does not open. With
  the variable unset — local dev and CI only; a deployed env refuses to boot —
  the data key is wrapped by a key derived from `FEOH_SECRET_KEY` instead.
- **Reading.** Every adapter, connection test and webhook verifier gets the
  block through ONE accessor, `provider_credentials.provider_config(org,
  "erp")`, which merges the decrypted secrets into the configuration exactly as
  the adapters always received it. A secret-named key that is somehow still in
  the JSONB is ignored. An unwrapped KMS data key is cached in-process for five
  minutes, so a webhook burst is not a KMS call per event.
- **Writing.** Only `PUT /api/organization/credentials/erp` (admin, audited
  `organization.credentials_updated` with path NAMES, written before the save —
  a 503 if it cannot be). `PATCH /api/organization` refuses a non-blank secret
  in the block and names that endpoint; it drops blank ones, which is what the
  settings form's "leave blank to keep" fields send.
- **Never read back.** `GET /api/organization/credentials` reports which paths
  are stored, by name. No endpoint returns a value, for any role.
- **Connection test.** `POST /api/organization/test-erp` with a form body uses
  the STORED secrets only while the body names the saved ERP at the saved
  destination (`catalog.same_connection`: same `type` + routing, and the same
  values for that ERP's `catalog.destination_keys` — see the next bullet but
  one); otherwise only the secrets typed into the body.
  The body is completed from the saved block first (the OAuth metadata, keys the
  form does not render), exactly as a save would. A test can therefore never
  send a sealed credential to a host that has been typed but not saved. When
  the store cannot be opened the test says so; it never runs without them.
  An OAuth ERP is tested the same way: the panel offers Test connection once
  connected, and the saved `oauth` block plus its sealed tokens reach the
  adapter. The answer is coded — `{success, code, provider | adapter,
  message}`, `code` one of `connected` / `connection_failed` /
  `credentials_unavailable` / `unknown_adapter` — and the panel states it with
  the ERP's display name in the reader's language
  (`types/erpConnection.erpTestResultText`); `message` is the English fallback.
- **A new destination drops them.** A `PATCH` that names a different ERP, clears
  the ERP, or changes a destination key of the same ERP removes the sealed
  secrets that belonged to the old connection (`catalog.secrets_to_drop`; the
  inbound webhook key survives a same-ERP destination change, since it is never
  sent anywhere). It goes through the same writer, in the same transaction,
  audited first as `organization.credentials_updated` with `reason:
  erp_destination_changed`. Without it a stored SYSPRO password would follow
  `base_url` to whatever host a save named. A block that selected no ERP yet
  binds nothing, so secrets entered before the first configuration save stay
  (decisions §271).
- **A switched sign-in method drops its secrets too.** Same ERP, same
  destination, but the save moves a field another field's `show_when` keys on
  (NetSuite's `auth_method`, TBA ↔ OAuth 2.0): the secret fields that applied
  before and no longer apply are removed (`catalog.secrets_switched_off`), audited
  with `reason: erp_sign_in_method_changed`. Only a field the save turned off —
  a secret stored ahead of the save that selects its method stays for it.
- **What counts as a destination is per ERP.** The default is by name,
  `catalog.DESTINATION_KEYS` — `base_url`, `tenant_id`, `account_id`,
  `company_id`, `environment` — and it covers every key a provider does not
  declare, rendered or not. A provider narrows it only by declaring one of its
  own fields `destination=False` in the catalogue, and the backend
  (`catalog.destination_keys`) and the settings form (the field's
  `destination` flag in `GET /organization/erp/providers`) read that one flag.
  The only opt-out is **QuickBooks Online's `environment`**: it picks
  `sandbox-quickbooks.api.intuit.com` or `quickbooks.api.intuit.com` (any other
  value is production) and the OAuth endpoints are Intuit constants, so no value
  sends a credential anywhere but Intuit. Switching production ↔ sandbox
  therefore keeps a bring-your-own-app `client_secret`; before this, the switch
  dropped it and the form refused to save without it, so a sandbox connection
  could not be configured (decisions §316). Business Central's `environment` is
  a URL path segment — other books — and stays a destination. A `*_url` /
  `*host*` field can never opt out, and `tests/test_erp_catalog.py` pins the
  list of opt-outs.
  | Provider | Field | Destination? | Why |
  |---|---|---|---|
  | QuickBooks Online | `environment` | No | Selects one of two fixed Intuit hosts |
  | Dynamics 365 BC | `environment` | Yes | Path segment: `{base}/{environment}/api/v2.0/…` |
  | Dynamics 365 BC | `base_url`, `tenant_id`, `company_id` | Yes | Host / path the token rides on |
  | NetSuite | `account_id` | Yes | Hostname label |
  | SYSPRO, Sage Business Cloud Accounting (SA) | `base_url`, `company_id` | Yes | Host; company logged in to |
  | Sage Intacct | `company_id` | Yes | Company logged in to |
- **OAuth tokens.** Sealed in the same row at the service-only paths
  `oauth.access_token` / `oauth.refresh_token`
  (`provider_credentials.SERVICE_SECRET_FIELDS`). They are never merged into an
  adapter's config — an adapter asks `erp_oauth.get_access_token` — the PUT
  endpoint refuses them, and only the consent callback, the refresher and the
  disconnect write them (through `update_secrets`, under the org row lock).
  `settings.erp.oauth` holds the connection metadata (provider, org,
  `connection_id`, expiry, company id) and reads as `{"connected": bool}`.
- **Fail closed.** A store that cannot be opened (KMS unreachable, a bad
  envelope) is never "no ERP": an ERP send lands `failed`
  (`erp_credentials_unavailable`, retryable), the ERP webhook answers a bodyless
  503 so the ERP retries, the vendor / GL / PO syncs answer 503, an OAuth refresh
  raises `ErpCredentialUnreadableError` (a failed push, never a reconnect mark),
  and a payment-sync leg fails into `erp_reconciliation`.
- **A configuration change is audited too** (`organization.provider_config_updated`,
  key names only), because where a stored credential is sent — a base URL, an
  environment — is decided by the configuration.

Migration `0110_provider_credentials` moved every pre-existing plaintext value
into the table and deleted it from the JSONB. It used to be stated here that the
credentials were "encrypted at rest via PostgreSQL column encryption"; they were
plain JSONB under RDS storage encryption until that migration.

## Provider catalogue

`backend/app/services/erp_adapters/catalog.py` is the single source of truth for
the Organization → ERP form, served at `GET /api/organization/erp/providers`
(admin). Each entry carries `key`, `label`, `regions` (ISO codes; the picker groups `US` and `ZA`, and lists any other region — `GB` for Sage Accounting — under "Other"), `auth`
(`credentials` | `oauth`), `fields` (`name`, `label_key` — a frontend i18n
`MessageKey`, `secret`, `required`, optional `placeholder` / `help_key` /
`options`) and `docs_url`; the endpoint adds `available` (adapter registered in
this build). The response also carries `merge_dev_long_tail` (the ERPs offered
inside the "Other ERP via Merge.dev" choice, Scale plan per `docs/decisions.md`
§256 — the `erp_merge` feature; every direct adapter is on every plan, Free
included, under `erp_integrations`, §281).

| Key | ERP | Regions | Auth |
|---|---|---|---|
| `quickbooks_online` | QuickBooks Online | US | OAuth (optional BYO app; `environment`) |
| `xero` | Xero | US, ZA | OAuth (optional BYO app; `bill_status`, `default_tax_type`) |
| `sage_accounting` | Sage Business Cloud Accounting (v3.1: UK, IE, CA, DE, ES, FR; the US product was retired 2024-12-31) | GB (listed under "Other") | OAuth (optional BYO app) |
| `sage_accounting_za` | Sage Business Cloud Accounting (South Africa) | ZA | API key + Sage login + company |
| `blackbaud_fe_nxt` | Blackbaud Financial Edge NXT | US | OAuth (optional BYO app + SKY API subscription key) |
| `sage_intacct` | Sage Intacct | US, ZA | REST API client credentials + company / user |
| `netsuite` | Oracle NetSuite | US, ZA | `auth_method`: OAuth 2.0 client credentials (`client_id`, `certificate_id`, sealed `private_key`) or TBA (OAuth 1.0a) tokens |
| `dynamics_365_bc` | Business Central | US, ZA | Azure AD client credentials |
| `syspro` | SYSPRO | ZA | Operator + company (https base URL) |
| `merge_dev` | Other ERP via Merge.dev | — | Merge API key + account token |

`catalog.PENDING_ADAPTERS` lists catalogue keys whose adapter has not landed;
`tests/test_erp_catalog.py` fails if a catalogue key is neither registered nor
pending, if a registered direct adapter has no catalogue entry, or if a
`label_key` / `help_key` is missing from the frontend's English catalogue. Those keys
live in the frontend's **settings** catalogue slice (`frontend/src/lib/i18n/locales/settings/`,
`docs/decisions.md` §300), which only the screens that render the form load; the
test reads it with the main catalogue, and the frontend's `slices.test.ts` reads
this package to prove the form's panel loads it.

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
**Auth:** OAuth 2.0 client credentials (M2M), or Token-Based Authentication (TBA) —
`settings.erp.auth_method`, § NetSuite sign-in below.

**Create Vendor Bill:**
```
POST /vendorBill
{
  "entity": { "id": "123" },
  "tranId": "INV-2024-001",
  "tranDate": "2026-04-01",
  "dueDate": "2026-05-01",
  "currency": { "refName": "USD" },
  "externalId": "<correlation_id>",
  "expense": {
    "items": [
      { "account": { "id": "789" }, "amount": 250.00, "memo": "Paper" }
    ]
  }
}
```

What the adapter sends: GL-coded lines on the `expense` sublist (the `item`
sublist needs an item record we never have), amounts as exact JSON number
literals. The create is **synchronous** — no `Prefer: respond-async`, which
would answer 202 with a *job* in `Location` instead of the bill — and NetSuite
answers 204 with the new record's URL in `Location`. The adapter then reads that
record back and checks its `total` (§ NetSuite: SuiteQL syncs and void →
Posted total).

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
  precedence `services/gl_account_sync._preference` applies). Another entity's account never
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
| `netsuite` | `entity: {id}`; refuses `vendor_not_linked` | GL-coded lines go on the **`expense`** sublist (`account: {id}`, `amount`, `memo`), since the `item` sublist needs an item record we never send. The lines are the shared `bill_lines.bill_lines` projection — NetSuite keeps no line rules of its own: `amount` is the line's tax-inclusive gross (its total, else quantity × unit price; a unit price with no quantity refuses `line_amount_missing` rather than being priced as quantity 1), exact Decimal; an uncoded line takes the header account; a coded line with no id, or a bill with no account at all, refuses `account_not_linked`, never moved onto the header account; lines that do not sum to the approved amount refuse `amount_mismatch`, and several tax-exclusive lines with header-only tax refuse `tax_not_itemised`. `memo` is the line's description, else the invoice's, else its number. NetSuite then totals the bill itself, so the booked `total` is read back after the create (§ NetSuite: SuiteQL syncs and void → Posted total). |
| `dynamics_365_bc` | `vendorId`; refuses `vendor_not_linked` | Every line is an `Account` line on **`accountId`** = the account GUID the BC chart sync stored, never `lineObjectNumber` (the No.). The lines are the shared `bill_lines.bill_lines` rule: an uncoded line takes the header account, a coded line with no id or a bill with no account at all refuses `account_not_linked`. See § Business Central: chart, POs and void. |
| `merge_dev` | `contact` (Merge object id); refuses `vendor_not_linked` | A line's `account` is the Merge account id. A coded line with no id refuses `account_not_linked`; an uncoded line sends none (Merge allows it). |

**NetSuite chart sync.** `NetSuiteAdapter.list_gl_accounts` pulls the chart
with one SuiteQL query (`POST …/services/rest/query/v1/suiteql`,
`Prefer: transient`, `SELECT id, acctnumber, fullname, accttype, isinactive FROM
account`), paged by `offset` / `hasMore` up to `SYNC_LISTING_MAX_ROWS`
(10,000); a failed page or a longer chart raises `ErpListingIncomplete` (§ GL
accounts and vendors are linked by the ERP's unique key → Only a whole listing
is evidence). Inactive accounts are emitted with `active=False`, and a code
longer than the code column (`GL_CODE_MAX_LENGTH`, 100 since migration 0114) is
emitted as it is, for the sync to refuse `code_too_long`; either way the id
stays live. Only an account with no id, or with neither a number nor a name, is
skipped. `fullname` is the hierarchical `Parent : Child` path, so it is unique where a
bare name is not. The REST record collection (`GET
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

### A refusal is recorded as a code the reader is told how to fix

The English `error` on `invoice.erp_failed` names a reason code to a person who
cannot act on one ("QuickBooks Online post refused: vendor_not_linked"). So the
same row also carries `refusal = {code, params}`
(`services/erp.erp_refusal_record`): the code (`ErpPostResult.refusal_code`,
set by `erp_refusal` and by the coded post-create outcomes in `posted_total`,
NetSuite, Business Central and Blackbaud), `params.provider` (the adapter's
fixed literal) and, for `account_not_linked`, `params.accounts` — the GL codes
on the invoice that resolved to no ERP id, or `[]` when the bill had a line with
no account at all. An `ErpNotConnectedError` records `not_connected`, the
unopenable-credentials path `erp_credentials_unavailable`; an HTTP failure
(`erp_failure_message`) records none, since its text is already the diagnosis.

The invoice modal's ERP panel states the code in the reader's language with the
fix (`frontend/src/lib/api/erpRefusals.ts`, e.g. *"This vendor isn't in
QuickBooks Online yet. Sync vendors from QuickBooks Online, or add the vendor
there, then retry."*), keeps the Retry button, and falls back to `error` for a
code it does not know or a row written before refusals were coded.

**The registry is `erp_adapters/base.py::ERP_REFUSAL_CODES`.** A new reason
needs an entry there and a message in all six locales:
`tests/test_erp_refusal_codes.py` fails on a lower-snake reason constant under
`erp_adapters/` that is not registered (and on an `erp_refusal` call given an
inline literal), and `frontend/src/lib/api/erpRefusals.test.ts` reads the set
out of `base.py` and fails on a code with no message, a message with no code,
or a locale that leaves a placeholder unfilled.

### An open duplicate or fraud exception refuses the push

A bill in the ERP can be paid there (QuickBooks "Mark as paid", Bill Pay), past
our payment-run gate. So the push refuses while the invoice has an open or
escalated exception of a type in `api/payments.ERP_EXPORT_BLOCKING_EXCEPTION_TYPES`:
`duplicate` and `fraud_flag`, the two payment-blocking types that say the
liability itself is wrong. The other four payment-blocking types are holds on
*paying* a real liability and do not refuse it (decisions §286 has the per-type
reasoning).

- **Chokepoint:** `services/erp._call_erp` asks
  `services/erp_export_gate.erp_export_blocking_exception` before it builds an
  adapter, on every attempt. A refusal is an `ErpPostRefusedError`: the invoice
  goes to `failed` at once, with
  `refusal = {code: "exception_blocks_erp_export", params: {exception_type}}`
  on `invoice.erp_failed`.
- **It says when the bill may already be there.** `_call_erp` commits
  `state_data.erp_outcome_unknown = true` before every adapter call and sets it
  false only on a success, so a timeout, a 5xx, a worker that died mid-call or
  any refusal leaves it true (a refusal made after the create went through
  cannot be told from one made before) — except an attempt the adapter states
  left nothing (`ErpPostResult.not_posted`: every adapter's refusals made
  before its first network call go through `base.erp_preflight_refusal`, and
  QuickBooks also sets it for any refusal before its bill create is sent and
  when Intuit refused the create itself with a 4xx, e.g. closed books), and
  even then an earlier attempt's unknown outcome is kept. A refusal after a
  lookup stays unknown: the lookup may have found our own earlier bill. `erp.possibly_posted` reads that flag,
  an unconfirmed ERP job, an orphan document or an `erp_reference`. When it is
  true, the refusal's params, the 409 and the `invoice.erp_failed` row carry
  `possibly_posted: true`, and the browser says to check the ERP before anyone
  pays the bill. The gate does not look the bill up; once the flag is cleared
  the next push's own idempotency lookup finds it instead of posting another.
- **A bill due before it is dated never posts.** Right after the exception
  gate, `_call_erp` refuses an invoice whose `due_date` is before its
  `invoice_date` (`invoice_due_before_date`, no adapter built, so nothing was
  sent). QuickBooks accepts such a bill over the API and then marks it invalid
  in its own UI (live sandbox, 2026-10-20: moving only the invoice date out of
  a closed period left bill 154 due five days early). `POST /api/invoices` and
  `PATCH /api/invoices/{id}` refuse the pair at entry with the same code (422,
  `api/refusals.refuse_due_before_invoice_date`); the push gate is what covers
  extraction, CSV import and the recurring sweep.
- **The HTTP doors answer first:** `POST /api/invoices/{id}/complete` (the
  modal's "Send to ERP" on an approved invoice), `/send-to-erp` and
  `/retry-erp` return **409** with that coded refusal before they move the
  invoice, so it stays `approved` / `failed` and nothing is queued
  (`erp_export_gate.refuse_blocked_erp_export`).
- **What the user does:** resolve or dismiss the exception in Exceptions, then
  send again. Clearing it goes through `exception_lifecycle.record_decision`,
  so someone implicated in the invoice cannot clear it for themselves.
- **Approval pushes only when the workflow says so** (decisions §304). With
  the snapshot's `erp_export.auto_send_on_approval` on and a live ERP, approval
  starts the push after it commits, through the same gates
  (`services/erp_send.start_erp_send`, shared with the three doors above). A
  refusal there is not a 409 to the approver: the invoice stays `approved` and
  an `invoice.erp_auto_send_refused` audit row records the coded refusal. With
  the flag off (or absent), approving advances the step pointer only. The push
  is post-commit work: a process that dies between the approval and the job
  skips it without a trace (tracked in `docs/followups.md`).
- **A hand-off that raises lands at `failed`.** When `dispatch_erp` raises after
  the `sending_to_erp` commit (an SQS send), `start_erp_send` moves the invoice
  to `failed` with an `invoice.erp_failed` row (`error: "ERP send could not be
  queued"`), so `/retry-erp` can take it.

The browser states the code from either place (`api/erpRefusals.ts`, also used
by `codedRefusals.ts` for the 409). Tests:
`tests/test_erp_export_blocking_exceptions.py`.

### An invoice FeohLedger is already paying refuses the push

Staging a payment run books a live `pending` payment and leaves the invoice
`approved`; an `exported` (NACHA) run is the same, and so is a minted virtual
card. A bill pushed then is payable from the ERP as well, so the bill could be
paid twice. The same gate therefore refuses while the invoice holds a **live
payment** (`payment_runs.live_payment_invoices`: any status outside
`api/payments.LIVE_PAYMENT_TERMINAL_STATUSES`) or a **live virtual card**
(`card_issuance.live_card_invoice_ids`: any card not `cancelled`). Those are
the predicates the run builder and the card mint refuse each other with, so
the three cannot disagree about what is live (decisions §309).

- **One answer, two places.** `erp_export_gate.erp_export_refusal` is asked by
  `_call_erp` (before any adapter, every attempt) and by the three HTTP doors
  and the auto-send on approval, all through `erp_send.start_erp_send` (an
  auto-send refusal is an `invoice.erp_auto_send_refused` row, §304).
  An open duplicate / fraud exception is named first; otherwise the live claim
  refuses with code `payment_blocks_erp_export` and
  `params = {claim: "payment" | "virtual_card"}`, plus `possibly_posted: true`
  on the same terms as above. 409 at the doors, `failed` on the first attempt
  at `_call_erp`.
- **What the user does:** cancel the run (`POST /api/payments/runs/{id}/cancel`;
  an exported run first needs `/nacha/void`) or void the payment, or cancel the
  card, then send again. A voided, failed or cancelled payment and a cancelled
  card no longer hold the invoice.
- **QuickBooks status sync (§295) covers the other order, and does not
  exempt an org from this gate.** With sync on, staging a run and then pushing
  used to work and is now refused: the bill would sit open in QuickBooks,
  payable there, until our run executed. Sync covers *push, then pay*:
  with sync on, a confirmed push lands at `posted_in_erp`, payable here, and a
  payment staged afterwards is written back as a BillPayment; the push is never
  re-run from `posted_in_erp`, so this gate never sees it. A bill paid in
  QuickBooks while FeohLedger also holds a payment is still the status sync's
  `payment_reconciliation` exception. The supported order with an ERP is
  therefore *push, then pay*.

## GL accounts and vendors are linked by the ERP's unique key

A bill posts on `gl_accounts.erp_account_id` (§ above), so which ERP account a
row is linked to *is* where the money is booked. `POST /api/gl-accounts/sync-erp`
links each pulled account through `services/gl_account_sync.sync_chart_from_erp`
(docs/decisions.md §284):

0. **Only within the ERP company the link was made in.** An ERP id is scoped
   to the company that issued it — QuickBooks numbers accounts and vendors per
   company, so sandbox "7" and production "7" are different accounts. Every link
   records its connection realm (`erp_link_realm`, `services/erp_link_realm`:
   the OAuth company id, or a direct adapter's company fields; never a secret).
   A link from another realm — or from before migration 0115 (NULL) — is kept
   only when the pull shows the same id holding the row's own code; otherwise
   it is dropped and the row is re-matched by code. Between two named realms
   that means the **current** code: a legacy code (QuickBooks' bare `Name`,
   which two sub-accounts can share) counts only for a pre-0115 link, and a
   realm-to-realm row whose code is a legacy key is unlinked with the accounts
   that key names offered as `erp_link_candidates`. Every change is in
   `link_changes` (with `from_realm` / `to_realm` when only the company moved).
1. **By ERP id first.** A row already holding the account's `erp_account_id`
   is that account: its name, type and parent follow the ERP, so a rename in the
   ERP renames the row instead of creating a second one. Its **code is kept**
   — invoices record the code as a string — and the drift is reported
   (`code_drift`).
2. **Then by code.** An unlinked row with the code is linked; so is one whose
   link rule 0 dropped, or one linked to an id the pull no longer contains. A
   row with the code linked to *another* account in the pull is a collision
   (`code_taken`), refused.
3. **A code two pulled accounts resolve to is refused for both**
   (`duplicate_code`): neither is created or linked, an existing row with that
   code loses its link, and the response says so. Never "the later one wins".
4. **A row keyed by a legacy code two accounts claim is unlinked**
   (`GLAccountPayload.legacy_codes`). Before §284 QuickBooks sub-accounts were
   keyed by their bare `Name`, which QuickBooks repeats across parents; such a
   row may have merged two accounts and be linked to whichever the sync read
   last. Its link is dropped, so invoices coded to it are refused
   `account_not_linked` (naming the code) and get recoded; the row and the
   invoices stay. Each real account gets its own row under its real code.

Rules 0, 3 and 4 run over the organization's **whole** chart, whichever entity
the sync runs under — they are about which ERP account a code names, not which
subsidiary holds the row. Matching and creating stay scoped to the chart being
synced into.

**Only a whole listing is evidence** (decisions §287). Rule 0 and the dead-link
handling read an id's absence from the pull as "that account is gone", and both
syncs count codes and names across the pull. So every adapter's chart and
vendor listing returns the whole collection or raises: up to
`base.SYNC_LISTING_MAX_ROWS` (10,000 rows; QuickBooks reads 50 pages of 1000,
50,000), and any failed page or a collection past the bound raises
`ErpListingIncomplete` (`fetch_failed` / `truncated`; QuickBooks raises its own
request error on a failed page). `POST /api/gl-accounts/sync-erp` and
`POST /api/vendors/sync-erp` answer **502** and change nothing, with the coded
detail `erp_listing_incomplete` (`params: {provider, gap}`) the browser states
in the reader's language. Every sync (chart, vendors, POs) that finds the OAuth
connection gone — revoked, never consented, expired — answers the coded **409**
`erp_not_connected` ("reconnect it under Organization") rather than a 502
naming the exception class; a token refresh that could not reach the provider
stays a 502, since the connection is still good (`api/refusals.erp_pull_failure`,
the one place all three routes turn an adapter failure into HTTP). An empty pull
changes nothing either. PO listings keep their bounded best-effort reads, since
nothing is unlinked by a PO's absence.

**A sync with no realm never touches another company's rows.** `mock`, and an
OAuth ERP with no consent, have no realm. A sync under one leaves alone every
row linked, or offered candidates, under a named realm: no re-judging,
unlinking, id matching, re-linking by code or candidate refresh, and a pulled
account whose code such a row holds is refused `code_taken`. It works only on
unlinked rows and NULL-realm links. (A pre-0115 link is NULL-realm too, so a
mock sync still treats it as its own; the next sync under a named realm
re-judges it by evidence.)

**Needs a decision.** A row rule 3 or 4 left unlinked keeps the ERP accounts it
could mean (`erp_link_candidates`, refreshed by every pull). `/gl-accounts`
badges it "Needs a decision" and counts such rows above the table; an admin or
AP manager picks one with **Choose ERP account**
(`POST /api/gl-accounts/{id}/erp-link`, audited `gl_account.erp_link_confirmed`).
Only an id the last sync offered, under the same connection, is accepted, and
only on an unlinked row. A confirmed link is exempt from rules 3 and 4 (it still
dies with its realm, or when the account leaves the ERP). The live case: an old
row "Depreciation" when QuickBooks has a top-level "Depreciation" (40) and
"Truck:Depreciation" (39). Picking 39 for the old row means the top-level
account cannot also own the code "Depreciation" in that chart (it stays
`code_taken`): pick the account the invoices coded to the row meant.

**Inactive accounts** arrive with `active=False` (QuickBooks is queried `where
Active IN (true, false)`; NetSuite `isinactive`, BC `blocked`; Merge.dev
`status: INACTIVE` or `remote_was_deleted`). Their ids are
live, a matched row is marked inactive (`deactivated`), none is created, and a
sync never reactivates a row. QuickBooks renames an inactive record —
`Bank Charges (deleted)`, `Automobile:Fuel (deleted)`, a vendor's `DisplayName`
likewise — so its adapter strips that suffix from an inactive record's names
(`quickbooks_online._undeleted`) before keying it; without that the record no
longer answered to the name its row was linked by, and the live sandbox sync
unlinked Bank Charges and Squeaky Kleen Car Wash with a false reason instead of
retiring them (2026-10-20). An active record's name is left as it is. **An
account with no id** is refused (`missing_erp_id`) and never linked or created.

**Parents.** An adapter that knows the parent's id passes it
(`parent_erp_account_id`), and the sync names the parent by **our** code for
that account — the row linked to it — falling back to the ERP's code only for a
parent we hold no row for. The ERP's code is wrong whenever a row kept a code
the ERP has since changed: QuickBooks' numbered "Office Expenses" is `6020`
there, so its Software sub-account read `6020` here. An adapter that sets
`reports_parent` (QuickBooks) states parentage outright, so a top-level account
**clears** a stale parent — the merged "Equipment Rental" row kept "Job
Expenses" after the account turned out to be top-level. Without
`reports_parent`, an absent parent leaves ours alone. Merge.dev's
`parent_account` is the parent's Merge id (a uuid), so it is passed as
`parent_erp_account_id` and resolved to the parent's code within the pull — never
stored as a code. Merge does not set `reports_parent`: a null `parent_account`
can mean "not reported" on the ERP behind it.

A code longer than `GL_CODE_MAX_LENGTH` is refused by the sync
(`code_too_long`), never truncated there. Adapters emit the code they have as it
is (NetSuite and BC included, so the id stays live), with one exception:
QuickBooks shortens an account path over 100 characters itself, to
`path[:100 - len(tail)] + "…#<Id>"` (`quickbooks_online._account_code`), which
stays unique because the id is. An account whose `parent_code` does not fit the
column syncs without its parent and is reported (`parent_code_dropped`). The
response and the `gl_account.synced_from_erp` audit row carry exact counts
(`created`, `updated`, `unchanged`, `linked`, `conflict_count`,
`unlinked_count`, `code_drift_count`, `deactivated_count`, `realm_adopted`,
`parent_code_dropped_count`) and the codes, capped at 50 each in the response
(the audit row is uncapped), plus the `realm` the pull came from; the
`/gl-accounts` page shows refused, unlinked and deactivated codes as warnings
apart from the summary. A collision is never counted as `updated`.

**The code an adapter returns must be unique in that ERP.** Per adapter:

| Adapter | `code` | Why it is unique |
|---|---|---|
| `quickbooks_online` | `AcctNum`, else `FullyQualifiedName`; a path over 100 characters keeps its head and ends `…#<Id>` | QuickBooks refuses a duplicate account number, and keeps a `Name` unique only among siblings, so the colon-joined path is the key. `name` is the path too. |
| `merge_dev` | `account_number`, else the Merge `id` — never `name`; a record with no `id` is dropped | Account numbers are unique in every ERP behind Merge; a name is not (QuickBooks again). |
| `netsuite` | `acctnumber`, else `fullname` | Numbers are unique; `fullname` is the hierarchical path. |
| `dynamics_365_bc` | `number` (the No.) | The account table's primary key. |
| `xero` | `Code` (accounts without one skipped) | Xero refuses a second account with the same code. |
| `sage_accounting` | `nominal_code` | Unique per business ledger. |
| `sage_accounting_za` | `ID` | The record id. |
| `sage_intacct` | `id` (ACCOUNTNO) | The account's key. |
| `syspro` | `GlCode` | The `GenMaster` key. |
| `blackbaud_fe_nxt` | `account_number` | Unique in the FE NXT chart. |

The sync's collision refusal is the backstop for an adapter that gets this
wrong: a repeatable key leaves accounts unlinked, it never merges them.

**Vendors** (`services/vendor_sync`) already matched by `erp_vendor_id` first.
The name fallback — adopting a manually created, unlinked vendor of the same
name — now applies only when exactly one pulled record has that name and
exactly one unlinked local vendor does. Otherwise neither is linked nor created
and the response reports it (`conflicts`, reason `ambiguous_name`): linking the
wrong "Acme" posts its bills to another supplier's ERP record. QuickBooks'
`Vendor.AcctNum` is no longer mapped to our vendor `code`: on a QuickBooks
vendor it is the company's account number *at* the vendor, not an identifier
for the vendor — and the sync clears it from the `code` of vendors the old
mapping created (`source = erp_sync`, `codes_cleared`, audited without the old
value), because CSV import resolves vendors by code.

Vendor links carry a realm too. A vendor linked under another ERP company is
kept only when the record with that id is the same supplier (same name ignoring
case and spacing, or the same non-blank tax id); between two named realms that
name or tax id must also be unique in the pull, while a pre-0115 link keeps the
looser rule. Otherwise its link is dropped (`unlinked`, audited
`change: unlinked`) and the record is matched like any other. A no-realm sync
matches by id only within its own realm and never touches a vendor linked under
a named one. An inactive ERP vendor is never created or linked as a payee, and a
vendor linked to one becomes `inactive` (never the reverse). Business Central
and Merge.dev vendors with no `id` are skipped, never keyed by the vendor No.,
`remote_id` or name.

**Merge.dev suppliers are Contacts.** Merge's Accounting API has no `/vendors`
path and no Vendor model; the adapter lists `GET /contacts?is_supplier=true`
with `expand=addresses,phone_numbers`, following `next` cursors, and any failed
page raises `ErpListingIncomplete` (the sync answers 502 and changes nothing).
It maps `name`, `email_address`, `tax_number`, the first `phone_numbers[].number`
and the first address's `street_1` / `street_2` / `city` / `state` / `zip_code` /
`country`. A Contact carries **no payment term**, so `payment_terms` is always
absent and the sync keeps whatever terms the vendor already has. An `ARCHIVED`
contact, or one with `remote_was_deleted`, is emitted inactive; one that says
`is_supplier: false` is never listed. The Contact `id` is the `erp_vendor_id`:
it is what a Merge PurchaseOrder's `vendor` and an Invoice's `contact` hold, so
the PO sync links by it and the push posts it. Before 2026-10-26 the adapter
read a `/vendors` path Merge never published, so a live Merge vendor sync
failed and every push was refused `vendor_not_linked` (`docs/known-issues.md`).
Sources: [Merge Accounting OpenAPI](https://docs.merge.dev/openapi/accounting.json)
(`Contact`, `GET /accounting/v1/contacts`, `PurchaseOrder.vendor`,
`Invoice.contact`), [Contacts](https://docs.merge.dev/accounting/contacts/),
[Pagination](https://docs.merge.dev/basics/pagination/).

**A vendor value too long for its column is dropped, not truncated.** An
over-long `code`, email, phone, address, tax id or payment terms is not written
(the vendor keeps its old value) and the vendor still syncs; the response lists
each such record in `values_too_long` (`name`, `erp_vendor_id`, `fields`, field
names only), and its audit row names the fields. Before, one NetSuite
`entityid` over `vendors.code`'s 50 characters rolled back the whole sync.

**The push resolves only current-realm links.** `services/erp._resolve_erp_refs`
filters both the vendor and the accounts by the realm of the connection being
posted to, so a link from another company is refused `vendor_not_linked` /
`account_not_linked` rather than posted to a colliding id. A pre-0115 (NULL)
link is trusted only while the org is still connected to the realm migration
0115 recorded for it (`settings.erp.legacy_link_realm`); after any change of
company it is refused until a sync re-judges it. A QuickBooks Online org gets no
such snapshot, so its pre-0115 links are refused until the first syncs (next
section). Reconnecting the OAuth consent
to a different company also writes `organization.erp_realm_changed`.

**Purchase orders link no ERP id the push resolves** (the bill carries
`po_number` as text), so they need no realm. A synced PO is linked to its vendor
by the ERP's supplier id (`PoPayload.vendor_erp_id`, matched against the
`erp_vendor_id` the vendor sync stamps — Xero's `ContactID`, QuickBooks'
`VendorRef`, Business Central's `vendorId`, Sage Accounting SA's `SupplierId`,
Sage Intacct's `vendor.id`, Merge.dev's `vendor`), falling back to the vendor
name; an adapter that does not read the id uses the name alone. Linking by id
is what keeps a PO on the right vendor after a rename on either side, and when
two vendors share a name. Both maps are built over the vendors in the synced
entity's scope (the entity plus shared, unstamped rows — the scope the PO upsert
uses), never the whole org: two subsidiaries connected to different ERP
companies can hold the same supplier id, and an org-wide map linked a PO to
whichever was read last. An ERP id two vendors in scope both hold links
neither — the PO stays unlinked rather than falling back to the name — and a
name two vendors share links neither too.

**A PO the adapter skips is not left trusted.** An adapter that cannot state a
PO's total exactly (SYSPRO: a discount, a mixed-currency order, a currency it
cannot place) leaves it out and names it in `list_pos_report().skipped`. The sync
creates nothing for it, but stamps `purchase_orders.total_unverifiable_reason`
(the `PoSkipReason`) on the existing row the upsert would refresh for that
number (`purchase_orders._sync_target`: this entity first, else an unstamped
row), so the PO match stops comparing invoices against the total an earlier
sync stored (`po-matching.md` § A PO total the ERP no longer vouches for). The
next sync that prices the PO clears the mark on that same row. Invoices citing a
PO whose mark or total changed are re-matched in the same transaction, and
**not** best-effort: the mark exists to raise a hold, so a failed re-match
fails the sync and rolls the mark back rather than commit a mark with no hold.
The response carries `not_priced` (POs the ERP has that were not synced) and
`unverifiable` (existing rows now marked); the PO list and detail return
`total_unverifiable_reason` and withhold the total and line money (`null`), and
the purchase-orders page says so in a warning toast and in place of the figure.
The supplier portal shows no figure for such a PO and refuses to flip it into an
invoice (409); budgets and the CFO accruals leave it out and count it
(`po-matching.md` § A PO total the ERP no longer vouches for).
Adapters that do not override `list_pos_report` report nothing skipped.
**Blackbaud FE NXT still skips a PO with no order total silently**: its wiring
is tracked in `docs/followups.md`.

**Run the vendor sync before the PO sync** on a new
connection. A PO synced first has no vendor, and the next PO sync fills that
missing link once the vendor exists. It never re-points a PO that already has
a vendor. The QuickBooks PO sync also no
longer reads the chart to resolve PO-line accounts: nothing persists or reads a
PO line's GL account, so lines carry none — never the `AccountRef` name. Merge.dev's
lines carry none for the same reason: a Merge PO line's `account` is a Merge
Account id, which stored as a GL code would name no chart row. Merge's PO number
is read from `purchase_order_number`, and its `BILLED` / `DELETED` statuses map to
closed / cancelled, as Xero's do. A Merge PO with neither a number nor a Merge
`id` is skipped (the sync logs a count, nothing else): the sync upserts by
`po_number`, and the `"UNKNOWN"` placeholder it used to get filed every such PO
into one row, each overwriting the last one's vendor and total.

**Repairing a chart synced before §284.** On a QuickBooks Online tenant, every
invoice push is refused after migration 0115, with `account_not_linked` for the
GL accounts and `vendor_not_linked` for the vendor, until the chart has been
synced once (`POST /api/gl-accounts/sync-erp`) and the vendors have been synced
once (`POST /api/vendors/sync-erp`). That is true even if the tenant never
changed company. Those first syncs re-judge each pre-§284 link by evidence: a
link the pull confirms is adopted silently (`realm_adopted`), and a merged one
is unlinked and reported. Only then does the push resolve them. Run both syncs
right after deploying 0115. Rows the sync will not guess about appear as "Needs
a decision".

On any tenant, run the chart sync again. Rule 1
relinks every row by id; rule 4 unlinks each collapsed QuickBooks row and
creates the accounts it had merged under their full paths; the response lists
the unlinked codes. Recode the open invoices coded to those codes (their pushes
are refused, naming the code, until you do), then retire the old rows from
`/gl-accounts` once nothing open uses them. Nothing is deleted or recoded
automatically: which of the merged accounts an invoice meant is a judgement the
data cannot make. One case stays as it was: a collapsed row whose link happened
to land on the account whose new code equals the old one (a top-level account
sharing its name with sub-accounts) is indistinguishable from a correct row and
keeps its link.

Tests: `tests/test_erp_link_realm.py` (colliding ids "7" / "57" across a
sandbox and a production realm, through both syncs, the push and the endpoints;
pre-0115 links and the migration snapshot; whole-org repair; inactive and
id-less accounts; the confirmed link for the live "Depreciation"),
`tests/test_gl_account_sync.py` (the 89 → 89 regression from the live
sandbox chart, rename, duplicate, collision, dead link, legacy repair),
`tests/test_vendor_sync.py`, `tests/test_erp_quickbooks_online_adapter.py`
(`_account_code`, the inactive-inclusive query, no PO chart read, vendor code), and
`tests-e2e/erp/quickbooks-online.spec.ts` (same-named sub-accounts against the
fake; the bill's `AccountRef` is the picked one).

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
| `netsuite` | Pre-create lookup: `GET /vendorBill?q=externalId IS "<correlation_id>"` (NetSuite enforces `externalId` uniqueness per record type). A match short-circuits `post_invoice` to a success referencing the existing bill — once its `total` reads back as the approved amount (the same check as a fresh create); only a miss proceeds to `POST /vendorBill`. |
| `dynamics_365_bc` | Pre-create lookup: `GET purchaseInvoices?$filter=externalDocumentNumber eq '<correlation_id>'`. A hit is re-read whole (`GET purchaseInvoices({id})`, parsed exactly) and short-circuits to success only when it is `Open` or `Paid` **and** its `totalAmountIncludingTax` is exactly the approved amount; a different total refuses `posted_total_mismatch`, none refuses `posted_total_unconfirmed` (both final, the posted invoice never deleted). A `Draft` (an earlier attempt whose post step failed or whose response was lost) is finished: its total re-checked, then `Microsoft.NAV.post` re-run — never a second create. Any other status (`In Review`, `Canceled`, `Corrective`) refuses `existing_invoice_not_open`. |

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

**A deterministic 4xx is never retried** (decisions §285). Every adapter builds
an HTTP failure with `base.erp_http_failure(provider, status)`, whose
`retryable` is `base.erp_status_retryable(status)`: 5xx and network errors
stay retryable, and so do the 4xx answers that can change on a re-send — 401
(the adapter refreshes the token), 408 / 425 / 429 ("later"), 409 / 423 (a
concurrent edit or a lock; Business Central answers a stale etag with 409).
Every other 4xx (400, 403, 404, 422, …) is the ERP's answer to the bill itself,
so the invoice fails on the first attempt with `retries: 1` instead of sending
the same refused bill three times over six seconds. Blackbaud's SKY quota (403
with `Retry-After`) stays retryable, and the next attempt is refused before
any call (`erp_call_budget_exhausted`) while the `Retry-After` lasts. A manual `POST /{id}/retry-erp` after the
fix is unchanged.

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

## The invoice names its bill in the ERP

A confirmed push stores the adapter's `erp_document_id` on
`invoices.erp_document_id` and the provider's deep link on
`invoices.erp_document_url` (migration 0118), alongside the
`state_data["erp_reference"]` write in `services/erp._call_erp`. Both are on
`InvoiceResponse`; the invoice modal's ERP section shows the id as
"ERP Reference", linked when there is a URL, at every status from
`sending_to_erp` on, `done` included.

- **Only the ERP's id is stored**, never the document number or the
  `"UNKNOWN"` placeholder `erp_reference` falls back to: the column is what a
  deep link or an API lookup addresses. A success that reports no id clears
  both columns, because an earlier pair may name another ERP's bill after the
  org switches ERP.
- **A failure never writes it.** A bill a failed push left behind is the
  orphan state above, not the bill of record.
- **A payment void keeps it** (the bill is still in the ERP); a later confirmed
  push overwrites it, and the push idempotency makes that the same bill.
- **Deep links live in one place**, `erp_adapters/deep_links.py`, built at push
  time from the config that posted the bill:

  | Adapter | Link |
  |---|---|
  | `xero` | `https://go.xero.com/AccountsPayable/View.aspx?InvoiceID=<id>` |
  | `quickbooks_online` | `https://app.qbo.intuit.com/app/bill?txnId=<id>` (`app.sandbox.qbo.intuit.com` when `environment` is `sandbox`) |
  | everything else (Merge.dev, mock, the other direct adapters) | none — the id is shown alone |

  Both open in the user's currently selected organisation / company
  (`docs/followups.md`). Invoices posted before 0117 got the id backfilled
  from `state_data` but no link. Reasoning: `docs/decisions.md` §302.

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

- ERP credentials are envelope-encrypted under the app KMS key in `provider_credentials`, write-only (§ Where the credentials live)
- Webhook endpoints validate requests via signature/secret or IP whitelist
- All ERP communication uses HTTPS
- Credentials are never logged or included in audit trail details

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
| NetSuite | SuiteTalk REST records + SuiteQL, OAuth 2.0 token endpoint | `/netsuite/services/rest/record/v1` (token at `/netsuite/services/rest/auth/oauth2/v1/token`) |
| Dynamics 365 BC | OData + OAuth token endpoint | `/d365` (token at `/d365/oauth2/token`) |

`GET /health` reports liveness; `POST /__reset` restores the fixture state.
Fixture data: purchase orders `PO-FAKE-301` (1250.00), `PO-FAKE-302` (980.50),
`PO-FAKE-303` (4400.00) — cursor-paginated — GL accounts `6100 Fake Office
Supplies`, `6200 Fake Software`, `6300 Fake Consulting`, and (Merge.dev only,
issue #256) three cursor-paginated supplier Contacts at `/contacts` —
`Fake Merge Vendor Co`, `Fake Merge Supply Co`, `Fake Merge Services Co`, with
no payment terms, as on Merge — plus a customer contact served only to a request
that does not filter `is_supplier=true`. Like Merge, the fake has no `/vendors`
(404). Full list in `tools/fake-erp/README.md`.

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
full field mapping — name/email/phone/address/tax id, payment terms absent, and
the customer contact never imported; NetSuite
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
presence for NetSuite TBA, client-credentials → the fixed bearer `fake-d365-token`
for D365) but does **not** cryptographically verify those signatures. The one
exception is NetSuite's OAuth 2.0 token endpoint, which verifies the JWT client
assertion's signature and claims as NetSuite documents them
(`tools/fake-erp/netsuite_oauth2.py`, § NetSuite sign-in). It validates
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
    netsuite.py          # Direct NetSuite adapter (OAuth 2.0 M2M or TBA/OAuth1 + REST)

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

Every plan, Free included (`erp_integrations`, `docs/decisions.md` §281). A
workspace holds one `settings.erp`, so Free is one direct ERP that imports
(vendors, GL accounts, purchase orders) and pushes approved invoices.

1. Go to **Organization > ERP Integration**
2. Pick your ERP from the dropdown. It leads with the ERPs popular in the
   United States and in South Africa
3. Enter the fields the form shows for it. A secret field is always empty
   when the page loads and says whether a value is stored; leave it blank to
   keep the stored one, or tick "Remove the stored value" to delete it.
   Changing where the ERP lives (address, account, tenant, company or
   environment) removes the stored secrets, so type them again
4. Save, then **Test connection**
5. For an OAuth ERP (QuickBooks Online, Xero, Sage Business Cloud Accounting),
   optionally enter your own app's client ID and secret, save, then click
   **Connect to <ERP>** and approve access in the ERP. You return to the ERP
   section with a connected status, and can **Disconnect** there
6. QuickBooks Online only: pick the **Bill payments: bank account** (and card
   account, if you pay by card) — accounts from your synced chart — so payments
   made in FeohLedger are recorded against their bills in QuickBooks. Left
   blank, payments are not written back and each one is flagged. When the
   connection nears the end of its authorization the panel says so and offers
   **Reconnect** (admins are also notified); once it has lapsed or been revoked
   it says which, and Reconnect is the fix

### Other ERP via Merge.dev (Scale plan)

Needs the `erp_merge` feature, which only Scale grants (`docs/decisions.md`
§256). On Free and Growth the panel shows a Scale upgrade notice in place of the
Merge form, and the server refuses a Merge config, test, sync or push with
`402 plan_feature_required` naming `erp_merge`. A config with no
`integration_method` routes through Merge (the dispatcher's default) and is
gated as one.

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

$65 a linked account is more than the $49 Growth plan, and Free pays nothing.
That is why the common ERPs are direct adapters (`quickbooks-online-adapter.md`,
decisions §269), open on every plan, with Merge kept for the long tail on
Scale (§256, §281).

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
| `get_invoice_status` | `GET objects/accounts-payable/bill/{key}`, `state` only — never `totalTxnAmountDue = 0` (decisions §303) |
| `void_invoice` | deletes a draft / posted-unpaid bill; a bill that is paid, partially paid, selected for payment or reversed returns `False` (reversal with its payment is an accountant's call) |
| `list_vendors` / `list_gl_accounts` / `list_pos` | `services/core/query` on `accounts-payable/vendor`, `general-ledger/account`, `purchasing/document::<po_document_type>`, 100 rows a page; vendors and accounts up to `SYNC_LISTING_MAX_ROWS` (else `ErpListingIncomplete`), POs 10 pages best-effort; a PO's vendor is linked by `vendor.id` (the vendor ID `list_vendors` stamps) before `vendor.name` |
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
| `get_invoice_status` | `COMFND` on `ApInvoice` — below `OrigInvValue` ⇒ partially paid, else open; `MthInvBal1` 0 ⇒ a second `COMFND` on `ApInvoicePay` by the same key: `paid` only when the payment rows (`TrnType` `P`) sum, in absolute value, to `abs(OrigInvValue)` — a 40 cheque + 60 credit, or a 0.01 payment beside a write-off, is `settled_no_payment` (decisions §303) |
| `void_invoice` | never (`False`) — reversing a posted AP invoice is an adjustment / credit in an open period, an accountant's call, and the adapter deletes nothing |
| `list_vendors` / `list_gl_accounts` | `COMFND` on `ApSupplier` / `GenMaster` (`AccountType` A/L/C/R/E → asset/liability/equity/revenue/expense). A supplier with `OnHold = Y`, and a ledger code with `AccOnHldFlag = Y` or `ControlAccFlag = Y`, is emitted `active=False`: APSTIN refuses all three on a manual AP distribution |
| `list_pos` | `PorMasterHdr`, `PorMasterDetail` and `ApSupplier` in one session, each read whole by a keyset walk (below); `vendor_erp_id` = `PorMasterHdr.Supplier`, the id `list_vendors` reports; `CancelledFlag = Y` or `OrderStatus *` ⇒ cancelled, `9` ⇒ closed |

- **POs are read whole or not at all.** COMFND returns the first `ReturnRows`
  rows in `OrderBy` order and does not page, so the PO listing walks a keyset
  cursor (`PurchaseOrder GT <last>`): headers and suppliers 1,000 a page up
  to `SYNC_LISTING_MAX_ROWS`, lines 5,000 a page. A full page of lines can end
  mid-PO, so that PO's lines are dropped and re-read from the next page; a PO
  with more lines than one page is skipped. A failed page, a walk past the
  bound or a host that ignores the cursor raises `ErpListingIncomplete` (the
  sync answers 502 and syncs nothing) — it used to return the first 1,000
  POs, oldest first, and never the newest.
- **A PO total is in the PO's currency, or the PO is skipped**
  (`syspro.po_total`). Whether a PO is foreign is decided **once per PO**,
  from its header `Currency` against the company's local currency
  (`GetLogonProfile`'s `LocalCurrencyCode`, read in the listing's session;
  element name per wildland/syspro-ruby `lib/syspro/get_logon_profile.rb`) —
  never from the lines, which is how a EUR order with one line priced only in
  local `MPrice` came back as `2 × 10.00 EUR + 1 × 50.00 ZAR = 70.00 EUR`.
  A merchandise line is `MOrderQty × price`: on a foreign PO the price is
  `MForeignPrice`, and a line with no foreign price but a non-zero `MPrice`
  makes the PO unpriceable; on a local PO it is `MPrice`, and a differing
  non-zero `MForeignPrice` contradicts the header and makes it unpriceable. A
  PO with no header `Currency` is local and synced unlabelled. When the local
  currency cannot be read, every PO **with** a currency label is skipped
  rather than guessed — but only when `GetLogonProfile` answers with a profile
  that states no usable code. A failed read (non-200, `ERROR` text, an
  unreadable answer) raises `ErpListingIncomplete` and the sync changes
  nothing: skipping would mark every labelled PO and hold every invoice citing
  one over a transient outage. A freight / misc-charge line (`LineType` 4 / 5) adds
  its `NMscChargeValue`; a comment line (6) adds nothing. A PO with a line
  discount, a price unit of measure other than the order's, a charge on a
  foreign order, or a line with no price is skipped (logged as a count) until a
  sandbox confirms the arithmetic — never synced at a total the PO match
  would trust. Every skipped PO (no lines, lines past one page, unpriceable,
  local currency unknown) is reported by `list_pos_report`, so an existing
  row with that number is marked unverifiable (decisions §323).

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
  status)`.
- **APSTIN refusals are coded** (`classify_apstin_error`), matched on the
  message's wording with every quoted value stripped first, so a supplier,
  ledger code or invoice number cannot steer the match. Because the post asks
  `ApplyIfEntireDocumentValid=Y`, a refused document posted nothing, so a
  recognised refusal is non-retryable **and** `not_posted`:

  | Stem | Code |
  |---|---|
  | invoice … already on file / exists | `duplicate_invoice_number` |
  | supplier … on hold | `vendor_inactive` |
  | ledger code / account … on hold, control account, not on file | `account_inactive` (`params.accounts` = our GL codes for the ledger codes the message quotes) |
  | … period … | `period_closed` |

  An expired session or a full e.net licence count is retryable (and
  `not_posted`). Any other refusal is non-retryable but **not** `not_posted`
  (`SYSPRO post failed: invoice rejected by APSTIN`), and an unreadable answer
  stays retryable — the next attempt's idempotency lookup finds the invoice if
  it did post. SYSPRO does not publish the exact strings; they are on the
  sandbox list in `docs/followups.md`.
- **`post_invoice` never raises past its config checks** (the https / SSRF
  guard on `base_url`). A rejected operator / password / company — or a
  missing credential — is `not_connected` (an administrator reconnects); every
  e.net seat in use, or a company locked for the moment, is retryable and
  `not_posted`; a non-200 logon is the shared HTTP failure; a transport error
  is retryable, and `not_posted` unless it hit the post itself. The status read
  and the syncs still raise `SysproError` for their callers.
- **Schema provenance.** SYSPRO ships its business-object schemas with each
  install (`<SYSPRO>\Base\Schemas\APSTIN*.XSD`, `COMFND.XSD`) rather than
  publishing them. The `COMFND` document matches the published open-source
  client (wildland/syspro-ruby). The column names the adapter reads
  (`ApSupplier.OnHold`, `GenMaster.AccOnHldFlag` / `ControlAccFlag`,
  `ApInvoicePay`, `PorMasterHdr.CancelledFlag`, `PorMasterDetail.LineType` /
  `MForeignPrice` / `NMscChargeValue` / `MDisc*` / `MPriceUom`) are
  corroborated by a third-party column dump of a SYSPRO 8 database (issue
  #650); the APSTIN element names, the error wording and several code values
  have not yet been checked against a live install — see
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
fallback); `FEOH_ERP_XERO_AUTHORIZE_URL` / `_TOKEN_URL` override the identity
endpoints, which dev points at fake-erp so the connect flow runs locally
(`frontend/tests-e2e/erp/xero.spec.ts`). Scopes are Xero's granular set (`accounting.invoices`,
`accounting.contacts.read`, `accounting.settings.read`, `offline_access`),
which apps created on or after 2026-03-02 must use.

**`settings.erp` keys:** `oauth` (written by `erp_oauth` only),
`bill_status` (`AUTHORISED` default, or `DRAFT`), `default_tax_type`
(optional fallback, below).

| Method | Xero call | Notes |
|---|---|---|
| `test_connection` | `GET Organisation` | |
| `list_vendors` | `GET Contacts?where=IsSupplier==true&includeArchived=true`, then `GET PurchaseOrders?Status=` DRAFT / SUBMITTED / AUTHORISED and `GET Contacts?IDs=` for their contacts | paged, 100/page, up to `SYNC_LISTING_MAX_ROWS` (else `ErpListingIncomplete`); archived contacts `active=False`. Xero sets `IsSupplier` only once a contact has a bill, so a supplier you have only sent purchase orders to is read from the unbilled POs; a failed read of those raises too, since the vendor sync treats an absent vendor as gone |
| `list_gl_accounts` | `GET Accounts` | `Class` → account_type; archived accounts `active=False` |
| `list_pos` | `GET PurchaseOrders` | `PurchaseOrderNumber` is the PO number (Xero's free-text `Reference` is not read); vendor linked by `Contact.ContactID`; `BILLED` → closed, `DELETED` → cancelled, everything else (DRAFT / SUBMITTED / AUTHORISED) → open |
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
- **The posted total is checked, on the bill read back.** After the create the
  adapter `GET`s `Invoices/{id}`. Xero computes `Total` from the lines and
  their tax types; a `Total` other than `amount` voids (AUTHORISED) or deletes
  (DRAFT) the bill just created and fails non-retryable
  `posted_total_mismatch`, and a bill that reports no `Total` fails
  non-retryable `posted_total_unconfirmed` — never success
  (`erp_adapters/posted_total.py`). The message says which of three things
  happened to the bill: `the bill was voided`, `the bill could not be voided in
  Xero` (money applied, or the call failed), or `the bill was already deleted
  or voided in Xero`.
- **A replayed create for a removed bill moves to a fresh key.** Once a
  mismatch has voided the bill, a manual retry under the same
  `Idempotency-Key` gets the original "created" response back for a bill that
  is now `VOIDED` / `DELETED`. The read-back sees that status and creates again
  under the next key of a deterministic sequence — `<correlation_id>`, then
  `<correlation_id>#r2`, `#r3`, … (`posted_total.create_attempt_key`). This
  cannot duplicate: the bill the old key named is confirmed not live, and every
  retry walks the same sequence, so Xero's key dedupe still collapses
  concurrent pushes onto one bill. Only a positive `VOIDED` / `DELETED` moves
  on — a 404 or error on the read-back fails the push retryably, creating
  nothing more. After ten keys (`MAX_CREATE_ATTEMPTS`) the push fails
  non-retryable `previous_bill_removed`. Refusing at the first removed bill was
  rejected: the correlation id is the invoice's for life, so that refusal would
  leave the invoice unpostable even after the tax type was fixed.
- **Rate limits** (60/min, 5,000/day per tenant): HTTP 429 →
  `"Xero post failed: HTTP 429 (rate_limited)"`, `Retry-After` kept in the
  in-memory `raw_response`. No sleep loop; `services/erp`'s retry backoff
  owns the timing.
- Failures go through `erp_failure_message`; no response body is persisted.

Tests: `backend/tests/test_erp_xero_adapter.py`. Fake surface:
`tools/fake-erp/README.md` § Xero.

### Live Xero contract check

CI tests the adapter against `tools/fake-erp`, which is only as right as what we
believe Xero does. `pnpm test:erp:xero-live` (`backend/tests/test_xero_live_contract.py`)
checks those beliefs against real Xero. It is opt-in (`FEOH_XERO_LIVE=1`), and
every other run skips it. It is read-only: every request is a GET.

It asserts that Xero still:

- honours the `IsSupplier==true` filter, and the `Status` filter on
  `PurchaseOrders`;
- returns contacts by `Contacts?IDs=`;
- uses only PO, bill and account-class values the adapter maps (an unmapped PO
  status would silently sync as `open`);
- names a `ContactID` on every PO.

**To run it:**

1. Put a real Xero app's credentials in `backend/.env`, with the three
   `FEOH_ERP_XERO_*_URL` / `_API_BASE` overrides blanked so real Xero is used.
2. Connect the local `acme` tenant to a Xero org (a Demo Company is the
   intended target) under Organization → ERP Integration. Set
   `FEOH_XERO_LIVE_TENANT` to use another tenant.
3. With the backend venv active, run the script.

If you opt in without a connection, the module fails rather than skips, so a
green run always means Xero was asked.

It never runs in CI:

- the connection needs an interactive OAuth consent;
- Xero rotates refresh tokens;
- a Demo Company resets every 28 days;
- the repository is public.

Run it before a release, and whenever the fake's Xero data changes. That data
came from what the 2026-10-20 Demo Company test showed: a contact only sent
purchase orders is not `IsSupplier`.

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
unavailable, no fallback); `FEOH_ERP_SAGE_ACCOUNTING_AUTHORIZE_URL` / `_TOKEN_URL`
override the OAuth endpoints, which dev points at fake-erp so the connect flow
runs locally (`frontend/tests-e2e/erp/sage-accounting.spec.ts`).

**Region coverage: not the US, not South Africa.** Every v3.1 operation lists
its availability as CA, DE, ES, FR, GB, IE and US, but **Sage retired the US
product on 2024-12-31** and ended US access on 2025-01-31
([Sage](https://www.sage.com/en-us/sage-business-cloud/sage-accounting/)), so
no US business exists to connect. A US customer on Sage is on Sage Intacct
(`sage_intacct`) or the desktop Sage 50, which has no connector. Test this
adapter against a UK trial business. Sage Business Cloud Accounting
**South Africa** (the Pastel successor) runs on a separate codebase with its
own API (`https://accounting.sageone.co.za/api/2.0.0`, API key + basic auth,
not OAuth), and Sage's developer community confirms v3.1 does not serve it.
So this adapter covers the other v3.1 regions; a ZA business cannot
complete its consent flow. A ZA Sage adapter is separate work.

**`settings.erp` keys:** `oauth` (written by `erp_oauth` only),
`default_tax_rate_id` (optional fallback), `void_reason` (optional; default
`Voided from FeohLedger`).

| Method | Sage call | Notes |
|---|---|---|
| `test_connection` | `GET business_settings` | |
| `list_vendors` | `GET contacts?contact_type_id=VENDOR` | 200/page, up to `SYNC_LISTING_MAX_ROWS` (else `ErpListingIncomplete`); system contacts skipped |
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
| `company_currency` | no (optional) | ISO code of the Sage company's own currency, default `ZAR` — the API reports currencies only as numeric ids and a symbol |

Every call is `<Resource>/<Method>?apikey=…&companyid=…` with the Sage login in
HTTP basic auth. List methods page with OData `$top` / `$skip` (100 rows a
page) and filter with `$filter`. The supplier and account listings read up to
`SYNC_LISTING_MAX_ROWS` and raise `ErpListingIncomplete` on a failed page or
past it; an inactive account is emitted `active=False`. Other lists stop at 10
pages.

| Operation | Calls |
|---|---|
| `post_invoice` | refuse pre-flight without a numeric `vendor_erp_id` (SupplierId) / numeric GL account ids, or in a non-home currency → `SupplierInvoice/Get` idempotency lookup → `Supplier/Get/{id}` (+ `Company/Get/{id}`) currency check → `Account/Get` + `TaxType/Get` → `SupplierInvoice/Save` |
| `get_invoice_status` | `SupplierInvoice/Get/{id}` — `AmountDue` 0 ⇒ paid with Sage's `Paid` flag and a non-zero `Total`, else `settled_no_payment` (decisions §303); below `Total` ⇒ partially paid, else open |
| `void_invoice` | `SupplierInvoice/Delete/{id}`, only when `AmountDue == Total` and the invoice is neither `Locked` nor `Paid`; otherwise `False` (reverse it in Sage with its payment) |
| `list_vendors` / `list_gl_accounts` / `list_pos` | `Supplier/Get` / `Account/Get` (active only; category → asset/liability/equity/revenue/expense, unknown → unclassified) / `PurchaseOrder/Get` (`currency` left NULL — decisions §197; vendor linked by `SupplierId`, the supplier `ID` `list_vendors` stamps, before `SupplierName`) |
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
tenant; the environment is burned into the token. The "Standard APIs" product's
quota (10 calls/s, 25,000 calls/day, reset at 00:00 UTC) is **per subscription**,
so every tenant on the platform key draws on one bucket — see *Call budget* below.

| Setting | Secret | Meaning |
|---|---|---|
| `FEOH_ERP_BLACKBAUD_CLIENT_ID` / `_CLIENT_SECRET` | secret: yes | Platform SKY application (one app, every tenant). Empty → unavailable |
| `FEOH_ERP_BLACKBAUD_SUBSCRIPTION_KEY` | **yes** | Platform subscription key. Empty and no tenant key → every post refuses `subscription_key_missing` before any call |
| `FEOH_ERP_BLACKBAUD_DAILY_CALL_QUOTA` | no | The subscription tier's daily quota (default 25,000, "Standard APIs"). No call is made past it on a key |
| `FEOH_ERP_BLACKBAUD_TENANT_DAILY_CALL_BUDGET` | no | What one tenant on the **platform** key may spend a day (default 2,500; 0 = no per-tenant share). A tenant with its own key has its key's whole quota |
| `settings.erp.ap_account_number` | no | AP liability account (`01-2000-00`) the invoice's Credit distribution posts to — **required** |
| `settings.erp.currency` | no | ISO code of the FE NXT ledger — **required**; an invoice in another currency is refused |
| `subscription_key` | **yes**, sealed (optional) | Tenant key overriding the platform one |
| `settings.erp.client_id` / sealed `client_secret` | secret: yes (optional) | Tenant-owned SKY application (read by `erp_oauth`) |
| `settings.erp.project_id` | no (optional) | FE NXT project (`ui_project_id`) on every distribution split |
| `settings.erp.transaction_code_values` | no (optional) | `[{"id", "value"}, …]` on every split, in FE NXT's code order |
| `settings.erp.approval_status` | no (optional) | `Pending` / `Approved`; omitted otherwise (FE NXT's default applies) |
| `settings.erp.oauth` + sealed tokens | **yes** (tokens) | Written only by `erp_oauth`; `external_tenant_id` = the token response's `environment_id` |

| Operation | Call |
|---|---|
| `post_invoice` | pre-flight refusals → `GET /accountspayable/v1/invoices?search_text=<number>&limit=500` (narrowed by `from_date`/`to_date` only when that page is full) → `GET /generalledger/v1/fiscalyears` (cached 15 min per environment) → `POST /accountspayable/v1/invoices/process` (on a 400, `GET /vendors/{id}`) → `GET …/backgroundProcess/{id}/status` (≤ 5 reads, backing off 1, 2, 4, 8 s) → `…/result` |
| `get_invoice_status` | `GET /accountspayable/v1/invoices/{id}`: `Pending` draft, `Approved` open (a zero balance stays open until FE NXT says `Paid`, decisions §303), `PartiallyPaid`, `Paid`, `Deleted` cancelled |
| `void_invoice` | not automated (`False`): the AP API has no invoice delete, and cancelling a posted payable is an adjustment in an open period |
| `list_vendors` / `list_pos` | `GET /accountspayable/v1/vendors` / `/purchaseorders`, 100 a page; vendors up to `SYNC_LISTING_MAX_ROWS` (else `ErpListingIncomplete`), with `vendor_status` `Inactive` **and `OnHold`** emitted `active=False`; POs 10 pages best-effort, linked to their vendor by `vendor_id` (`PoPayload.vendor_erp_id`); template and deleted POs, and a PO with no `order_total`, skipped; PO `currency` stays NULL |
| `list_gl_accounts` | `GET /generalledger/v1/accounts`, keyed by `account_number` (what AP distributions take), up to `SYNC_LISTING_MAX_ROWS`; `prevent_data_entry` accounts emitted `active=False`; `account_type` unclassified (FE NXT's `class` is a net-asset class) |
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
  would create a second invoice. A job that reports **failed** (status 7) is
  the non-retryable `job_failed`: nothing says the failure is transient, so
  the backoff re-sending it would fail the same way. The job's
  `status_message` is never read (it can echo the payload). A **canceled**
  job (6) stays retryable. The unconfirmed result carries the job's `process_id` as
  `ErpPostResult.pending_job_id`; `services/erp._call_erp` keeps it on
  `WorkflowInstance.state_data["erp_pending_job_id"]` and hands it back as
  `InvoicePayload.pending_job_id` on the next attempt. An operator's retry
  therefore polls that job first: completed → its invoice is the result (and
  the stored job is cleared); still running or unreadable → `job_unconfirmed`
  again, nothing queued; canceled or failed → the normal lookup-then-post
  path (a failed job made nothing, and the retry may follow a config fix).
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
  `duplicate_invoice_number`. `search_text` matches vendor name, description
  and PO number as well as the invoice number, so a common number ("1001")
  can fill the 500-row page. Only then is the search narrowed with
  ListInvoices' `from_date` / `to_date` to a day either side of the invoice
  date (documented as "after" / "before", so the window holds the date
  whether they are inclusive or not) — and to each date an earlier attempt
  of this push was sent with. `services/erp._call_erp` keeps those on
  `WorkflowInstance.state_data["erp_attempt_invoice_dates"]` (recorded before
  the call, given back by an attempt that states it left nothing, cleared by a
  success) and hands the ones that differ
  from the current date to the adapter as `InvoicePayload.prior_invoice_dates`:
  an earlier attempt's bill carries the date it was sent with, and keyed on
  the current date alone a retry after a date edit missed that bill and posted
  a second one (decisions §322). The unnarrowed page is read first because it also catches a
  hand-keyed duplicate dated differently, which the narrowed one cannot. A
  failed lookup is a retryable failure; one still truncated after narrowing
  (more than 2,000 hits on that number within three days) is the
  non-retryable `idempotency_lookup_incomplete`.
- **Closed periods are refused before the create.** The post date (the
  invoice date) is checked against `GET /generalledger/v1/fiscalyears`: a
  year whose `status` is `Closed` or `Purged`, or a period of it marked
  `closed`, refuses `period_closed`, so nothing is queued. The check runs
  after the idempotency lookup, so an invoice an earlier attempt already
  posted is reported rather than refused. The read is cached for 15 minutes
  per environment. A 403 (some sandboxes lack the GL permission) or any
  other failure is "cannot check", never "open" or "closed": the create
  goes ahead and FE NXT judges the date itself. A date no fiscal year covers
  is left to FE NXT too, and `PreClosed` is not treated as closed.
- **Inactive and on-hold vendors.** `list_vendors` emits `Inactive` and
  `OnHold` vendors with `active=False`: the sync creates no payee for one and
  marks a linked vendor inactive. `OnHold` still takes an invoice in FE NXT,
  but FE NXT holds its payments, and paying it from FeohLedger would go
  around that hold — so the vendor is surfaced, not paid. Lifting the hold
  in FE NXT does not reactivate the local vendor; that stays a person's call.
  When the create answers 400, the vendor is read once (`GET
  /vendors/{id}`, never the 400's body) and an `Inactive` one turns the
  uncoded `invalid_request` into `vendor_inactive`.
- **Rate limits.** 429, and 403 with `Retry-After` (SKY's quota signal), map to
  `rate_limited` from status and headers only. Nothing sleeps on them; the
  call budget below records the `Retry-After` as a cool-down every tenant on
  that key honours.
- **Call budget** (`services/erp_call_budget.py`, decisions §322). A clean
  push costs four or five calls: the lookup, the fiscal-year read when it is
  not cached, the create, a status read and the result. Every SKY call is
  counted in Redis per subscription key (a digest, never the key) and per
  org, per UTC day. A tenant on the platform key may spend
  `FEOH_ERP_BLACKBAUD_TENANT_DAILY_CALL_BUDGET` a day, and no tenant on a key
  may take it past `FEOH_ERP_BLACKBAUD_DAILY_CALL_QUOTA`. A 429 or a quota
  403 records its `Retry-After` as a cool-down for the whole key. A push asks
  for its worst case first (`push_call_reserve`: 14 calls, 6 more when it
  reads an earlier job, 4 more per extra date searched) and then runs to the
  end, so its refusal always comes before the first call:
  `erp_call_budget_exhausted` with `params.reason` (`tenant_budget`,
  `subscription_quota`, `rate_limited`) and `params.retry_after`. Only
  `rate_limited` is retryable by the backoff; the others fail the invoice with
  the reason, and the operator retries after the reset (an automatic re-send
  at the reset is an open follow-up). A sync listing is refused call by call
  (`ErpCallBudgetExhausted` → a coded 429 with `Retry-After`, nothing synced);
  a status read is `unknown`; a PO sync is refused the same way. Org admins get an
  `erp_call_quota` notice once a day each at 80% of the share, when the share
  is used, and when the subscription's quota is. Redis down → the budget fails
  open and SKY's own 403 is the backstop. A heavy tenant should still store its
  own `subscription_key`; whether Blackbaud sells a higher-volume product for a
  multi-tenant app is an operator question (`docs/followups.md`).
- **Money** goes out through `dumps_exact_json` and is read back with
  `parse_float=Decimal`.
- **Refusal reasons:** `vendor_not_linked` (also a non-numeric vendor id),
  `account_not_linked`, `subscription_key_missing`,
  `ap_account_not_configured`, `currency_not_configured`, `currency_mismatch`,
  `invoice_date_missing`, `due_date_missing`, `amount_not_positive`,
  `duplicate_invoice_number`, `idempotency_lookup_incomplete`,
  `period_closed`, `vendor_inactive`, `job_failed`, `job_unconfirmed`,
  `erp_call_budget_exhausted`.

Tests: `backend/tests/test_erp_blackbaud_adapter.py`, and end to end through the
real OAuth connect `frontend/tests-e2e/erp/blackbaud-fe-nxt.spec.ts`. fake-erp
surface: `/blackbaud` (`FEOH_ERP_BLACKBAUD_API_BASE`, `FEOH_ERP_BLACKBAUD_TOKEN_URL`,
`FEOH_ERP_BLACKBAUD_AUTHORIZE_URL`). The fake carries an `Inactive` and an
`OnHold` vendor (the create refuses the first with FE NXT's 400), fiscal years
2024–2030 with FY 2024 `Closed` and Jan–Mar 2025 closed (a create posting
there is refused), account `01-5900-00` whose job ends at status 7, a PO with
no `order_total`, ListInvoices' `from_date` / `to_date` (read strictly) and its
full `search_text` match, and `POST /blackbaud/__seed-invoices` for
hand-keyed rows.

Read from the live SKY API definitions (the APIM developer-portal data API,
2026-10-26): `VendorStatus` = `Active, Inactive, OnHold`;
`PurchaseOrderSummary.vendor_id` (int32); `BackgroundProcessStatus` 0–7 with
6 canceled and 7 failed; ListInvoices `limit` ≤ 500 with `from_date` /
`to_date` and the four-field `search_text`; `FiscalYear.status` = `Open,
PreClosed, Closed, Purged, Optimized` and `FiscalPeriod.closed`. Still
unverified without an FE NXT environment: which date `from_date` / `to_date`
filter on and whether they are inclusive, whether FE NXT refuses a closed
period or an inactive vendor on the create or in the job, and whether
`PreClosed` takes postings (`docs/followups.md`).

## Business Central: chart, POs and void

`erp_adapters/dynamics_365_bc.py`, API v2.0
(https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/).

| Operation | Call |
|---|---|
| `post_invoice` | refuse without `vendor_erp_id` / a line account id / a valid `base_url` → `GET purchaseInvoices?$filter=externalDocumentNumber eq '…'` (a non-200 fails the push) → `POST purchaseInvoices` (a draft) → `GET purchaseInvoices({id})` → total check → `POST purchaseInvoices({id})/Microsoft.NAV.post` |
| `list_gl_accounts` | `GET accounts` — `number` → code, `displayName` → name, `id` → `erp_account_id`, `category` Assets/Liabilities/Equity/Income/Cost of Goods Sold/Expense → asset/liability/equity/revenue/expense/expense (blank → unclassified). Heading / total accounts are skipped (BC refuses a line on one); blocked accounts are emitted `active=False`. |
| `list_pos` | `GET purchaseOrders?$expand=purchaseOrderLines` — total `totalAmountIncludingTax`; status always `open`; vendor linked by `vendorId` (the vendor GUID `list_vendors` stamps; BC's empty GUID is no vendor) before `vendorName`; `currencyCode` only when non-blank; `requestedReceiptDate` unless BC's blank `0001-01-01`; comment lines dropped |
| `list_vendors` | `GET vendors` |
| `void_invoice` | `GET purchaseInvoices({id})`; a `Draft` is deleted (`DELETE` with `If-Match` = the etag just read — never `*`; a read with no etag returns `False` unsent); anything else returns `False` |

**Lines post on `accountId`, not the No.** The chart sync is now the writer of
`gl_accounts.erp_account_id` for BC, and `services/erp._resolve_erp_refs`
resolves those ids through the invoice entity's chart
(`gl_chart.resolve_erp_account_ids`). Posting the No. instead would bypass that
resolution: a code typed locally but never synced, or an entity's own account
overriding a shared one, would post against whatever BC account happens to hold
that No. — or fail inside BC instead of refusing up front. The id also survives
a renumbering in BC. Lines follow NetSuite's account rule (uncoded → header
account; coded but unlinked → `account_not_linked`) and its amount rule,
because BC's lines **are** the shared `erp_adapters/bill_lines.bill_lines`
projection rather than a copy of it: every line needs an amount (total, else
quantity × unit price; a unit price with no quantity, or neither, →
`line_amount_missing` — BC's own copy used to read a missing quantity as 1) and
the gross lines must sum to exactly the approved amount (else `amount_mismatch`
/ `tax_not_itemised`) — BC totals the bill from its lines, so lines that
disagree would post a different figure, and collapsing them onto one header
line would move coded expense onto the header's account. Only an invoice with
no line items posts one line for the amount on the header account. Each line
goes as quantity 1 at its gross, an exact `Decimal` at the invoice currency's
scale (`payment_adapters.base.exponent_for`: `100.000000` → `100.00`, JPY →
whole units); a gross finer than the currency refuses `line_amount_precision`
rather than being rounded. A line's description is its own, else the invoice's,
else its number.

**The draft's total is checked before it is posted.** BC computes a
purchaseInvoice's total itself, and a VAT / sales-tax company adds tax on top
of the lines — 1,200 approved would book 1,440, and BC's own payment run would
overpay. So after the create the adapter re-reads the draft and runs
`Microsoft.NAV.post` only when `totalAmountIncludingTax` (parsed exactly, never
via float) equals `payload.amount`. Otherwise — or when BC states no total — the
draft, still unposted, is deleted and the push refused non-retryably:
`Business Central post refused: posted_total_mismatch`. The `DELETE` carries
the draft's real etag (the one just read, else the create / lookup response's)
and is never sent with `If-Match: *`, which would delete the invoice even if a
BC user had posted it in between. Its status is checked: when the draft could
not be deleted (BC refused, or no etag was known) the message says so —
`… posted_total_mismatch (the draft was not deleted in Business Central)` — so
the accountant knows a draft above the approved amount is still sitting in BC.
A failed draft read or post step is a **retryable** `erp_failure_message`
failure. The post step used to be `except Exception: pass`, reporting success —
and moving the invoice to `sent_to_erp` — while BC held only a draft.

**Every success is total-checked, including an invoice already posted.** Success
is reported only for an invoice BC holds as `Open` or `Paid` whose
`totalAmountIncludingTax` is exactly `payload.amount` — whether it was just
posted, found posted on the fresh read, or found by the idempotency lookup. The
case this closes: a draft at 1,150 against an approved 1,000, the `DELETE`
fails, a BC user posts the draft, and the manual retry's lookup finds it
`Open`; that used to report success, and our payment run would pay 1,000
against a 1,150 bill. Now it refuses non-retryably `posted_total_mismatch`
(`posted_total_unconfirmed` when BC states no total), with
`… (the invoice is posted in Business Central and was not deleted)`. A posted
invoice is never deleted — reversing it is a corrective credit memo, the
accountant's call in BC.

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
server page is 20,000 rows) and follows `@odata.nextLink`. Bodies are parsed
with `utils/json_money.loads_exact_json`, so a total is a `Decimal` from the
wire, never a float. The chart and vendor listings read up to
`SYNC_LISTING_MAX_ROWS` and raise on any failure (a token failure as itself; a
non-200, network error, unparseable page or longer collection as
`ErpListingIncomplete`), since the syncs read absence as evidence. The PO
listing stays best-effort: it stops at 10 pages / 1,000 rows, and a failure ends
the pull with what it read. An admin `base_url` stays behind the SSRF guard on
every call, and that refusal is raised, not swallowed.

## NetSuite sign-in: OAuth 2.0 client credentials or TBA

Oracle: "As of 2027.1, no new integrations using TBA can be created for … REST
web services … Use OAuth 2.0." So the adapter signs in either way, chosen by
`settings.erp.auth_method` (issue #653):

| `auth_method` | Config | Secret (sealed) |
|---|---|---|
| `oauth2_m2m` — the form's default for a new connection | `client_id` (the integration record's client ID), `certificate_id` (the Certificate ID NetSuite generates when the certificate is mapped) | `private_key` (that certificate's PEM private key) |
| `tba` — and **absent**, which is every NetSuite block saved before this choice existed | `consumer_key`, `token_id` | `consumer_secret`, `token_secret` |

Any other value is refused (`NetSuite config 'auth_method' is invalid`). The
catalogue marks each method's fields `show_when` its method, so the form shows
and requires only the chosen method's credentials, and `stored_default: "tba"`
makes the form read a saved block without the key as TBA, the way the adapter
does: showing the first option there would switch a working tenant to OAuth
2.0 on its next save.

**The OAuth 2.0 grant**
([token request](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162755359851.html),
[assertion](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162790605110.html)).
`NetSuiteAdapter._client_assertion` builds a JWT with header `{"typ": "JWT",
"alg": …, "kid": certificate_id}` and claims `iss` = `client_id`, `scope` =
`rest_webservices` (records and SuiteQL), `aud` = the token endpoint, `iat`,
and `exp` five minutes later (NetSuite's ceiling is under 60). It is POSTed
form-encoded with `grant_type=client_credentials` and
`client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`
to `https://<account>.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token`
(or, under `FEOH_ERP_NETSUITE_API_BASE`, the same path beside the overridden
REST root, so the fake gets it and real NetSuite never does). The answer is a
60-minute bearer with **no refresh token**, so:

- the token is cached in process (`_TOKEN_CACHE`, keyed on the token URL and a
  hash of account, client id, certificate id and private key — a rotated key
  never reuses the old key's token) until a minute before its `expires_in`,
  then a new assertion is exchanged;
- a 401 from the API drops the cached token and the call is made once more
  with a new one; a second 401 is the answer;
- a refused token request (`invalid_client` for an unmapped certificate,
  `invalid_grant`…) is returned to the caller as the failed call it is — a push
  fails `NetSuite post failed: HTTP 400 (invalid_request)`, non-retryable, and no
  record request is made. The OAuth `error` code is logged; the assertion,
  the token and the key never are.

**The key.** The algorithm follows the key, per Oracle's
[certificate conditions](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_162755332391.html):
RSA of 3072 or 4096 bits signs `PS256` (RSA-PSS; NetSuite does not take
RS256), EC on P-256 / P-384 / P-521 signs `ES256` / `ES384` / `ES512`. Anything
else — another size or curve, a passphrase-protected or unparseable PEM — is
refused before any request with a message that names `'private_key'` and never
its value; so is a missing `client_id` / `certificate_id` / `private_key` (or,
for TBA, a missing `consumer_key` / `consumer_secret` / `token_id` /
`token_secret`). The settings form takes the key in a one-line field, where a
browser strips a pasted PEM's newlines; the adapter re-wraps the base64 body, so
that is harmless. Loading an RSA key validates it (~150 ms of CPU at 4096 bits),
so the parsed key is memoised and the load and the signing both run through
`asyncio.to_thread`, never on the event loop. The key is a write-only `erp` secret like the others
(`provider_credentials.SECRET_FIELDS`). Generate the pair as Oracle shows, e.g.
`openssl req -new -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes
-days 365 -out public.pem -keyout private.pem`, upload `public.pem` on
NetSuite's OAuth 2.0 Client Credentials Setup page for the integration and role,
and paste `private.pem` and the Certificate ID NetSuite shows into the form.
NetSuite caps a certificate at two years and emails before it expires; a
rotation is a new mapping (new Certificate ID) plus a new key here.

**TBA signing** (`_tba_base_string`) follows RFC 5849 §3.4.1: the base string
URI is the URL without its query (scheme and host lower-cased, a default port
dropped), and the query's parameters are signed beside the `oauth_*` ones,
encoded, sorted and encoded again; every value in the header and the signing
key is encoded per §3.6 (`/` and `+` included). The signer used to put the
query inside the URI, so NetSuite computed a different signature for every
request that carried one — SuiteQL paging, the `externalId` lookup, the
connection test — and refused each 401 (audit finding N1). Pinned against the
RFC's own worked example in `tests/test_erp_netsuite_auth.py`.

**The fake.** `tools/fake-erp` answers the token endpoint with the same checks
(alg, kid, signature, iss = the mapped client id, aud = its own URL, scope,
iat/exp), against certificates mapped through
`POST /__netsuite/oauth2/certificates {certificate_id, client_id,
public_key_pem}` — the public half only. Its record and SuiteQL routes take an
issued, unexpired bearer or a TBA header. `POST /__netsuite/oauth2/expire-tokens`
makes every bearer 401 and `GET /__netsuite/oauth2/stats` counts mints.

Tests: `tests/test_erp_netsuite_auth.py` (the adapter against the fake's own
verifier, in process: PS256 and every ES curve, caching, rotation, the 401
retry, a refused assertion, every config refusal, and TBA unchanged),
`tests/test_erp_catalog.py` (the method's fields and defaults),
`frontend/src/lib/types/erpConnection.test.ts` (`show_when` /
`stored_default`), `frontend/tests-e2e/erp/netsuite.spec.ts` (connection test,
the re-mint after a 401, and a full send, against the running fake). Only a real
NetSuite account proves Oracle accepts the assertion (`docs/followups.md`).

## NetSuite: SuiteQL syncs and void

`erp_adapters/netsuite.py`. Every list sync is one SuiteQL query
(`POST …/services/rest/query/v1/suiteql`, `Prefer: transient`, `ORDER BY id`
for stable `offset` paging, 100 rows a page), parsed exactly like BC's. The
chart and vendor queries read up to `SYNC_LISTING_MAX_ROWS` and raise
`ErpListingIncomplete` on a failed page or past it; the PO query stops at 10
pages, best-effort.

| Sync | Query | Notes |
|---|---|---|
| `list_vendors` | `SELECT id, entityid, companyname, email, phone, BUILTIN.DF(terms) AS terms, isinactive FROM vendor` | The REST `/vendor` collection returns ids and links only — no names — so the old record-API sync could not have named a vendor. Name = `companyname`, else `entityid` (an individual); code = `entityid` (an `entityid` longer than `vendors.code` is not written and is reported in `values_too_long`); inactive vendors emitted `active=False`; a row with no `id` skipped. |
| `list_gl_accounts` | `SELECT id, acctnumber, fullname, accttype, isinactive FROM account` | unchanged (§ ERP references) |
| `list_pos` | `transaction` where `type = 'PurchOrd'`, `t.entity AS vendorid`, `BUILTIN.DF(t.entity) AS vendorname`, `foreigntotal`, `currency.symbol`, `TO_CHAR(duedate, 'YYYY-MM-DD')` | Headers only — the PO sync stores no ERP lines. `vendorid` (the vendor's internal id, the same `vendor.id` `list_vendors` reports as `erp_vendor_id`) fills `PoPayload.vendor_erp_id`, so the PO links to its vendor by id and a rename on either side, or two vendors sharing a name, cannot mislink it; the name is the fallback only (issue #652). Status letter C → cancelled, G / H → closed, else open. Dates go through `TO_CHAR` because SuiteQL formats them per the user's preference. |

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

**Posted total.** NetSuite totals a vendorBill itself, and on a tax-enabled
account an expense line's tax code adds tax to what we sent — so the booked
total can differ from the approved `payload.amount` a payment run pays. After
the create, `post_invoice` GETs the record named by `Location` and compares its
`total` exactly (parsed from the raw bytes as a Decimal, never a float):

- equal → success;
- different → non-retryable `posted_total_mismatch`, after deleting the bill
  through `void_invoice`'s rule (only while still Pending Approval). The
  message says which: `NetSuite post failed: posted_total_mismatch (the bill
  was deleted)`, or `(the bill could not be deleted in NetSuite: it is no
  longer pending approval or NetSuite refused)` — an approved bill is left for
  the accountant to reverse;
- no `Location`, an unreadable record or no usable `total` → non-retryable
  `posted_total_unconfirmed`, never success.

The `externalId` re-find applies the same check to the bill it finds, so a
retry never reports an earlier mismatched bill as posted. This is the shape of
`posted_total.check_posted_total`, kept in the adapter only because that
helper's message says "voided" and NetSuite's REST service deletes.

**Status mapping** reads `status.id` (`paidInFull`) or `status.refName` with
its spaces removed (`Paid In Full`). It used to lower-case `refName` only, so a
real paid bill (`"paid in full"`) never matched `paidinfull` and polled as
`unknown`.

fake-erp serves all of the above: BC `accounts` (three posting accounts plus a
heading and a blocked one), `purchaseOrders` (`PO-FAKE-BC-401` in local
currency with a blank date, `PO-FAKE-BC-402` in EUR), `DELETE
purchaseInvoices({id})` (needs `If-Match` equal to the record's current
etag or `*`, else 412; drafts only; posting moves the etag), a 400 for an
`Account` line whose `accountId` is not a posting account, `totalAmount*` on
every purchaseInvoice (company `fake-vat-co` adds 20% VAT on top of the lines),
and a 400 for posting a non-draft; NetSuite SuiteQL
`vendor` and `transaction` rows (`PO-FAKE-NS-501` open USD, `PO-FAKE-NS-502`
closed GBP), `DELETE vendorBill/{id}` (pending approval only), and a `total`
on every vendorBill — account `123` (code `6400`) carries a 10% tax code, so
its lines book more than was sent, and vendor `28` ("Fake NetSuite Vendor
Routed") is under approval routing, so its bills start Pending Approval. Both
are fixed fixtures, not a shared toggle a parallel e2e worker could trip over.

Tests: `test_erp_gl_sync.py`, `test_erp_po_sync.py`,
`test_erp_vendor_sync_adapter.py` (mapping, the listing bound, incomplete listings),
`test_erp_void_invoice.py` (both voids, NetSuite status shapes),
`test_erp_adapter_idempotency.py` (BC lines, failed lookups, the draft total
check, draft resumption, a failed post step; NetSuite lines as the shared
projection, the posted-total read-back on create and on the `externalId`
re-find), `test_erp_adapter_money_exact.py` (the read-back total parsed
exactly),
`test_erp_adapter_error_pii.py` (BC `account_not_linked`),
`test_erp_base_url_overrides.py` (BC `base_url` host allowlist + SSRF guard,
NetSuite `account_id` validation), `frontend/tests-e2e/erp/dynamics.spec.ts`
(the 1,200 / 20% VAT refusal against fake-erp), and
`frontend/tests-e2e/erp/netsuite.spec.ts` (a taxed bill refused, deleted when
pending approval and left when approved).
## Connecting an OAuth ERP (QuickBooks Online, Xero, Sage Accounting, Blackbaud)

These ERPs have no client-credentials grant: the customer's admin consents in
the provider's UI. One flow serves all of them (`services/erp_oauth.py`,
routes in `api/erp_oauth.py`). An adapter subclasses
`erp_adapters/oauth_base.OAuthErpAdapter`, registers an `OAuthProviderSpec`,
and calls `await self.access_token()`.

**Routes**

| Route | Auth | Contract |
|---|---|---|
| `GET /api/organization/erp/oauth/{provider}/authorize` | admin + `erp_integrations` plan | `200 {"authorize_url"}`; the page navigates the browser to it. `404` unknown provider, `409` no app configured, `402` plan, `503` when the tenant saved its own app and the credential store cannot be opened (never a silent switch to the platform app). |
| `GET /api/erp/oauth/callback` | public; signed single-use `state` | Exchanges the code, writes `settings.erp = {type, integration_method: "direct", oauth}` (metadata) and seals the tokens in `provider_credentials`, audit row first, and 302s to `<tenant origin>/organization?section=erp&erp_connected=<provider>` or `&erp_error=<code>`. Codes: `invalid_state` (forged: plain 400, no redirect), `state_expired` (replayed or expired), `access_denied`, `not_authorized` (the admin who started it is no longer an active admin), `plan_required`, `missing_code`, `provider_unavailable`, `token_exchange_failed`, `no_external_tenant`, `already_linked` (the company is connected to another tenant, or another connect for it is in flight: the uniqueness check and the write run under a Redis lock on `(provider, company id)`, `erp_oauth.realm_claim`, because the org row lock serialises one tenant only), `unknown_provider`, `credentials_unavailable` (the credential store could not be opened or written), `audit_unavailable` (the `organization.erp_connected` row could not be written, so nothing was linked). |
| `POST /api/organization/erp/oauth/disconnect` | admin (not plan-gated) | Clears `settings.erp.oauth` and the sealed tokens (audit row first), then revokes best-effort: `{"disconnected", "revoked"}`. `503` when the credential store cannot be opened, with nothing changed. |
| `GET /api/organization/erp/oauth/status` | admin | `{provider, connected, needs_reconnect, external_tenant_id, expires_at, refresh_token_expires_at, connected_at, redirect_uri, providers: [{key, display_name, available, client_source}]}`. Never a token. |

Connect and disconnect write `organization.erp_connected` /
`organization.erp_disconnected` (provider, client source, the sealed paths
changed by name — never a token) BEFORE anything is saved, through
`record_auth_audit_or_raise`; a row that cannot be written means nothing is
linked or cleared. The revoke outcome, known only after the commit, follows as
`organization.erp_token_revoked`.

The callback's query (`code`, `state`, `realmId`) never reaches the uvicorn
access log: `app/utils/access_log.py` strips the query of every path in
`SENSITIVE_QUERY_PATHS` from `uvicorn.access` records, installed by
`app/main.py` where app logging is configured (`tests/test_access_log_redaction.py`).

**Which app.** The tenant's own (`settings.erp.client_id` plus the sealed
`client_secret`, while `settings.erp.type` names the provider) wins over the
platform's `FEOH_` app. With neither, the provider is unavailable. The consent's source is
recorded, and the refresher uses the same one.

**Storage and refresh.** `settings.erp.oauth` holds `provider`, `org_id`, a
random `connection_id`, the expiry times and the company id; the access and
refresh tokens are sealed in `provider_credentials` (§ Where the credentials
live). The block reads as `{"connected": bool}` in the settings response, is
never taken from `PATCH /api/organization`, and is carried across every `erp`
save. The refresher re-reads the stored block and the sealed tokens (an
adapter's config carries none) and requires the config's `provider` and
`connection_id` to match, so an admin-supplied `test-erp` config naming another
org's id reaches nothing. It serialises refreshes per connection with a Redis
lock and persists the rotated tokens by compare-and-swap under the org row lock,
through `provider_credentials.update_secrets` (no audit row: a rotation is the
system keeping the grant alive). A
provider `invalid_grant` marks `needs_reconnect` and writes
`organization.erp_reconnect_required` (best-effort, after the commit). An outage raises
`ErpTokenRefreshError` and leaves the connection alone.

**A 401 refreshes too, not only expiry.** A provider can reject a stored token
before its recorded `expires_at` (the customer re-consented elsewhere, an admin
revoked one session). Every OAuth adapter's API client comes from
`OAuthErpAdapter.oauth_client`, whose `httpx` auth flow
(`oauth_base.RefreshOnUnauthorized`) reads the rejected bearer, calls
`get_access_token(..., rejected_token=...)` — which refreshes only if that token is
still the stored one — and replays the request once; later requests in the same
client carry the refreshed token directly. A failed refresh propagates like it does
from `access_token()`: a revoked consent is the final `ErpNotConnectedError`
(`needs_reconnect` is already set), an outage the retryable `ErpTokenRefreshError`
— never flattened into a 401 the push loop would retry. QuickBooks keeps its
own equivalent loop in `_request`. The only bare clients are the consent-time
tenant lookups (no stored connection to refresh yet), pinned by
`tests/test_erp_oauth.py::test_every_oauth_adapter_api_client_refreshes_on_401`;
end to end in `frontend/tests-e2e/erp/blackbaud-fe-nxt.spec.ts`.

**Per-provider hooks on the spec.** The company id comes from
`external_tenant_id_param` (a callback query parameter: QBO `realmId`), from
`external_tenant_id_token_field` (a token-response field: Blackbaud
`environment_id`), or from an override of
`OAuthErpAdapter.resolve_external_tenant_id` (Xero `GET /connections`, Sage's
business lookup). `extra_token_headers(erp_settings)` adds headers to every
token and revoke call (Blackbaud's subscription key). `token_auth` is `basic`
or `body`. `revoke_json` sends the revoke as a JSON `{"token": …}` (Intuit,
which documents that) instead of RFC 7009's form body; sent a form, Intuit may
refuse it, and Disconnect would then leave the grant live behind a
`revoked: false` the user never sees. **Which providers revoke:** QuickBooks
(JSON) and Xero (RFC 7009 form + Basic, `identity.xero.com/connect/revocation`;
revoking the refresh token also removes the org's connection, so the next
consent offers the org afresh instead of "Already connected"). Sage Accounting
and Blackbaud FE NXT configure no revoke URL yet, so their Disconnect only
forgets our copy of the tokens (`docs/followups.md`). An empty `scopes` sends no `scope`. Operator URL overrides
(`*_url_setting`) are read on every call.

**QuickBooks Online** (`erp_adapters/quickbooks_online.py`). It refuses before
any post, with `QuickBooks Online post refused: <reason>`: `vendor_not_linked`,
`doc_number_too_long` (21 characters), `currency_not_enabled`,
`currency_unknown`, `not_connected`, and the shared line codes from
`bill_lines` — `account_not_linked` (a coded line is never moved onto the
header's account), `line_amount_missing`, `amount_mismatch` /
`tax_not_itemised` (the lines must sum to the header amount, because QuickBooks
re-totals from lines). After the create the bill is read back by id
(`GET bill/{id}`), and QuickBooks' own `TotalAmt` on it must equal the approved
amount: a different one deletes the bill just created and fails non-retryable
`posted_total_mismatch` (the message says whether the bill was deleted, could
not be deleted, or was already deleted); a missing one fails non-retryable
`posted_total_unconfirmed`. The idempotent re-find applies the same check.
Posting is idempotent through `requestid=<correlation id>` plus a pre-check on
DocNumber + vendor + `PrivateNote: "FeohLedger <correlation id>"`. Intuit
replays the original response for a repeated `requestid` even after the bill
was deleted, so a retry after a mismatch would otherwise re-check (or report as
posted) a bill that no longer exists. When the read-back answers Fault `610`
Object Not Found or `status: Deleted`, the push creates again under the next
`requestid` — `<correlation id>#r2`, `#r3`, … (`posted_total.create_attempt_key`,
cut to Intuit's 50 characters). The `PrivateNote` stays the bare correlation id,
so the pre-check finds a bill from any attempt. This cannot duplicate (the old
bill is confirmed deleted, and every retry walks the same sequence); any other
read-back failure fails the push retryably. Ten removed bills
(`MAX_CREATE_ATTEMPTS`) fail non-retryable `previous_bill_removed`; the Xero
section above explains why refusing at the first one was rejected. A 401
refreshes once and retries. `void_invoice` deletes only a bill with no payment
applied. Locally, `pnpm erp:up` starts fake-erp, which serves `/qbo/oauth2/*`
(with rotating refresh tokens) and `/qbo/v3/company/fake-realm-1/*`;
`POST /__qbo/set-balance` marks a bill paid.
