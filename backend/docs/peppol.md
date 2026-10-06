# PEPPOL AS4 — e-invoice transmission (send + receive)

Transmits an approved invoice onto the PEPPOL network as a UBL 2.1 / BIS
Billing 3.0 document. This doc covers both halves of automated e-invoicing:
the **send** half first, then the inbound (receive) half, which has shipped and
reuses everything here — see [Inbound (AS4 receive)](#inbound-as4-receive).

## Four-corner model

PEPPOL is a four-corner network:

```
 C1 (us, sender)  →  C2 (our Access Point)  →  C3 (receiver AP)  →  C4 (receiver)
```

We integrate with a **hosted Access Point** (C2) over its HTTP API; the gateway
does the raw AS4/ebMS3 SOAP handshake to C3. We do **not** implement AS4 SOAP
ourselves in this slice. The `mock` adapter simulates the Access Point entirely
in-process — no DNS, no SMP/SML lookup, no network — so `pnpm dev` transmits
end-to-end with no PEPPOL credential.

## Adapter family — `app/services/peppol_adapters/`

Same registry shape as `payment_adapters` / `card_adapters`:

```python
@register_peppol_adapter("my_ap")
class MyAdapter(PeppolAdapter):
    async def resolve_participant(self, pid: ParticipantId) -> ParticipantCapability: ...
    async def send(self, request: TransmissionRequest) -> TransmissionResult: ...
    async def test_connection(self) -> bool: ...
    def parse_inbound(self, headers, body): ...   # inbound: verify + parse an AS4 delivery
```

Registered:

| Provider | What it is |
|----------|-----------|
| `mock` | **Local-first default** — in-process, no network. Canned SMP capability for any known scheme; `receiver_not_registered` for a value containing `UNREGISTERED`. Deterministic `MessageId`. |
| `as4_gateway` | Real adapter — `httpx` to a hosted Access Point's REST API. Base URL + key from config; **no hardcoded key fallback** (returns `peppol_not_configured` when unkeyed). |

Selection: `Organization.settings.peppol.provider` → falls back to
`FEOH_PEPPOL_PROVIDER` (default `mock`).

**A NAMED provider we have no adapter for is refused, never `mock`.**
`get_peppol_adapter` resolves an absent/empty provider to `mock` (the local-first
default — a fresh clone runs the whole four-corner flow with no PEPPOL
credential) but raises `UnknownPeppolProviderError` for an unregistered name. It
used to fall back to `mock` there too, and `mock` is not an inert stub: its
`send` returns `success=True` with a synthetic message id and no network
involved. `peppol_send` wrote that outcome onto the `PeppolTransmission` row as
`status="sent"` with a `message_id`, emitted an `invoice.peppol_sent` audit row
and answered 200 — so one typo in an admin-entered provider name reported a
legally-significant e-invoice as transmitted to a supplier that never received
it, and the row occupied `uq_peppol_one_live_per_invoice_direction`, so the
honest resend came back `already_sent`. Same call `decisions.md` §29 made for the
payment / ERP / FX dispatchers and §36 for sanctions.

Each caller decides what the refusal means:

| Call site | On refusal |
|---|---|
| `POST /api/invoices/{id}/peppol-send` | The adapter is resolved in step 5-6, **above** the step-7 slot claim, so a `PeppolSendError("peppol_provider_not_configured")` → **422** with no `PeppolTransmission` row at all and the live slot still free. The body carries the PII-free code, never the admin's raw settings value. |
| `POST /api/peppol/inbound/{tenant_slug}` | **Bodyless 503**, not the route's usual 204. This is OUR failure, not a decision about the document (`decisions.md` §37): the message is still unprocessed work and a redelivery after the setting is fixed would succeed, so acking it drops a supplier's invoice permanently behind a log line. Bodyless keeps the detail and the tenant out of the response, and it narrows the enumeration surface to "while this tenant is already misconfigured" — the trade §37 accepted for email intake. |
| `peppol_receive.receive_peppol_message` | Defence in depth (the route resolved the same adapter to parse): a soft `ReceiveResult(reason="peppol_provider_not_configured")` before the tenant engine opens, so no invoice, no transmission row, no S3 object — and never a 500 on a public webhook. |

Guard: `tests/test_adapter_registry_fail_closed.py`.

## ParticipantId value object

A frozen `(scheme, value)` dataclass. Wire form
`iso6523-actorid-upis::<scheme>:<value>`, e.g.
`iso6523-actorid-upis::9930:DE123456789`. `scheme` is the EAS code (0088=GLN,
9930=DE org, 0192=NO org). `ParticipantId.parse()` accepts the full wire form
**or** the bare `9930:DE123456789`; on a malformed id it raises a **PII-free**
`ValueError` that names the field but never echoes the value.

## SMP/SML resolution

`adapter.resolve_participant(receiver_id)` answers "is this receiver registered
on PEPPOL, and on which Access Point?" → `ParticipantCapability(registered,
access_point_url, supported_doc_types, unregistered_reason)`. The mock returns a
canned capability (no DNS). The send service refuses an unregistered receiver
(`PeppolSendError("receiver_not_registered")`) before persisting any row.

## BIS Billing 3.0 identifiers (`constants.py`)

```python
PEPPOL_BIS_BILLING_DOCTYPE = (
    "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2::Invoice"
    "##urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0::2.1"
)
PEPPOL_BIS_BILLING_PROCESSID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
```

## Payload — reuse the `e_invoice` package

The transmission carries the **same** UBL the export route emits. The send
service reuses the shipped package wholesale (no UBL duplication):

```
invoice_to_einvoice_document(invoice, line_items, BuyerIdentity)
  → assert_valid(doc)              # check_tax=True → hard reject (422)
  → stamp EndpointIDs from the participant ids
  → assert_bis3_conformant(doc)    # profile claim → hard reject (422)
  → generate_ubl(doc) -> bytes
```

**The parties' electronic addresses ARE the AS4 participant ids.** BT-34 /
BT-49 (`cac:Party/cbc:EndpointID` + `@schemeID`) are mandatory in BIS Billing
3.0 but live only on the transport arguments, so the send service stamps them
onto the document before serializing: the receiver is the counterparty this
invoice names as **seller**, the sender is us — the document's **buyer**.

**The send path refuses a non-conformant document.** The transmission declares
`PEPPOL_BIS_BILLING_DOCTYPE`, which *asserts* EN 16931 / BIS Billing 3.0
conformance, so `assert_bis3_conformant` runs before `generate_ubl`: a document
that provably does not meet the profile (no endpoint id, no VAT breakdown, a
line with no VAT category, a seller with no country, **or arithmetic that does
not add up**) never leaves the building. That last one is not a rounding
nicety: the check now evaluates the EN 16931 calculation rules, and adding them
is what caught the generator mapping `subtotal` into both BT-106 and BT-109 —
so every invoice carrying a discount or a shipping charge used to go out
contradicting itself while we stamped the conformance claim on it.
PII-free STRUCTURED body — one item per failing field carrying `loc` (the
field path), `type` (the rule id a receiving Access Point's own validator would
name, `BR-CO-15`) and `msg` (the human sentence, with that rule id folded in) —
mapped to 422 at the route. See `backend/docs/e-invoicing.md` § The 422 body is
structured, not a flattened string + § PEPPOL BIS Billing 3.0 conformance.

The buyer identity is built by the existing
`invoices._buyer_identity_from_org` helper. **SBDH** (Standard Business
Document Header) wrapping happens in the **adapter** layer (the gateway builds
it from the participant/doc-type/process metadata + the UBL), never in the
`e_invoice` generator — which only emits clean UBL.

## Transmission model + DB idempotency guard

`PeppolTransmission` (`peppol_transmissions`, tenant-scoped — `EntityMixin` +
`TimestampMixin` + explicit `organization_id`). Money (`amount`) is
`Numeric(15,2)` — never float. PII (`participant_value` = supplier tax/org id)
lives on the row legitimately but never in a log or error body.

Idempotency is enforced at the **data layer**, not by application code:

```sql
CREATE UNIQUE INDEX uq_peppol_one_live_per_invoice_direction
  ON peppol_transmissions (invoice_id, direction) WHERE status <> 'failed';
```

At most one **non-failed** (`sending`/`sent`/`delivered`) transmission per
`(invoice_id, direction)`. The send service inserts the row in `sending` state
and **flushes before the networked transmit**, so two concurrent POSTs cannot
both reach the gateway — the loser hits `IntegrityError`, rolls back, and gets
the committed live row returned (`already_sent=True`). A prior **failed** send
is excluded from the index, so a legitimate retry is allowed and creates a
fresh row.

A second partial unique index dedupes the AP-assigned `message_id`
(`WHERE message_id IS NOT NULL`) so the inbound receive path dedupes
redeliveries the same way payment webhooks dedupe by `event_id`. A **failed**
send never persists a `message_id` (nulled in both the adapter and the send
service): the supported failed→retry reuses the same `business_message_id`, so
a real AP that echoes the same MessageId on the retry would otherwise collide
on this index.

`direction` and `status` carry DB `CHECK` constraints (`ck_peppol_direction` ∈
`{outbound,inbound}`, `ck_peppol_status` ∈ `{sending,sent,delivered,failed}`)
so a typo can't slip past the `WHERE status <> 'failed'` index predicate and
strand a live row. Migration `0034` creates them inline on fresh tenants;
`0035` is the idempotent catch-up `ADD CONSTRAINT` for tenants already at `0034`.

The model's `__table_args__` (partial-index predicates + the two CHECK
constraints) match the migrations verbatim, so a fresh tenant built by
`tenant_provisioning` (`create_all`) is schema-identical to a migrated one.

## Send route

```
POST /api/invoices/{invoice_id}/peppol-send
  body: { receiver_scheme, receiver_value, sender_scheme?, sender_value? }
  → 200 PeppolSendResponse { transmission_id, status, message_id, direction, already_sent }
```

Role-gated `require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)` (same gate as
the e-invoice export). `sender_*` falls back to
`Organization.settings.peppol.{sender_scheme,sender_value}`.

The invoice must have cleared **AP approval** — only `approved` and the
post-approval states (`sending_to_erp` … `done`) are transmittable, mirroring
the ERP-send / payment-run gate. A `new` / `pending` / `rejected` / `failed`
invoice is refused (`422 invoice_not_approved`) before any row or audit is
written. The SMP step also refuses a receiver that doesn't advertise the BIS
Billing 3.0 doc type (`422 receiver_doctype_unsupported`) when the AP returns a
non-empty `supported_doc_types`.

| Outcome | HTTP |
|---------|------|
| transmitted (or idempotent re-send) | 200 |
| invoice not found | 404 |
| malformed participant id / no sender configured | 400 |
| invoice not approved / receiver not registered / doc-type unsupported | 422 (PII-free bare code, e.g. `invoice_not_approved`) |
| tax-invalid / not BIS 3.0 conformant | 422 (PII-free structured `[{loc, type, msg}]` list — see `e-invoicing.md` § The 422 body) |
| role not permitted | 403 |

A send writes a `invoice.peppol_sent` (or `invoice.peppol_send_failed`) audit
row through `dispatch_audit` (`details` records provider, `receiver_scheme`,
`message_id`, `doc_type` — **never** the receiver value / tax id). The
idempotent short-circuit deliberately writes **no** duplicate audit row.

## Reading an invoice's transmission state

`GET /api/invoices/{id}/peppol-transmissions` returns every transmission
recorded for the invoice, newest first:

```json
{"transmissions": [{
  "id": "…", "direction": "outbound", "status": "sent", "provider": "mock",
  "participant_scheme": "9930", "message_id": "…", "failure_reason": null,
  "transmitted_at": "…", "created_at": "…"
}]}
```

- **A sub-resource, not a field on the invoice detail.** `GET
  /api/invoices/{id}` is the hottest read in the app (list rows, the modal,
  every poll) and PEPPOL concerns a minority of tenants, so a second query on
  every invoice read would be paid by everyone to serve a few. It also matches
  the shape its siblings already use (`/line-items`, `/summary`, `/einvoice`,
  `/chat`).
- **Read-gated `ALL_ROLES`**, matching `GET /{id}/einvoice` — the same subject,
  read-only, PII-free. The network-touching `POST /peppol-send` stays narrower
  (admin / ap_manager / cfo). Tenant isolation is `get_tenant_db`; the invoice
  lookup is entity-scoped (`X-Entity-ID`), and an invoice outside the selected
  entity — or another tenant's — is the same opaque 404.
- **PII-free by omission.** `participant_value` / `sender_value` are the
  counterparty's and our own registered org/tax ids; they live on the row and
  inside the UBL and are deliberately not in this response. The schemes are bare
  EAS registry codes, which identify nobody. `failure_reason` is a reason code.

It exists because the send response's `already_sent` describes only a send the
caller just made. Without this the UI could report an outcome but could not
state the state when an invoice is opened — and staying silent reads as "not
sent", the one claim it could not support. The frontend's PEPPOL block renders
four distinct states off it: still loading, unreadable (the read failed —
never degraded into "not sent"), transmitted, and *only failed attempts*, which
the partial unique index treats as re-sendable and so must not read as done.

## Inbound (AS4 receive)

We are the receiver corner **C4**. The receiver's Access Point (**C3**) delivers
an inbound BIS Billing 3.0 document to us by POSTing the UBL/CII payload plus
metadata (sender participant id, AS4 `MessageId`, doc type, process id) with a
provider HMAC signature.

**Route (public-by-design, signature-gated, tenant in path):**

```
POST /api/peppol/inbound/{tenant_slug}      → always 204 No Content
```

The route mirrors the payment webhook (`/api/payments/webhook/{tenant_slug}/…`):
no JWT, no `X-Tenant-Slug` header — the tenant is the URL path segment, so each
tenant configures its own inbound URL with the Access Point and a leaked URL
only affects that one tenant. **Every** path — success and every rejection —
returns `204` silently with a PII-free `logger.warning` (reason code only); a
distinct 4xx would enumerate which slugs / signing secrets / payload shapes are
accepted, and the supplier's participant value / tax id / payload never enters a
log line or the body.

**Receive flow** (`api/peppol_inbound.peppol_inbound_webhook` →
`services/peppol_receive.receive_peppol_message`, mirroring
`email_intake.process_inbound_email`):

1. Master switch `FEOH_PEPPOL_INBOUND_ENABLED` off → 204 (closed by default).
2. **Bound the body** before buffering it — reject (204, no parse) when the
   declared `Content-Length`, or the actual read, exceeds
   `FEOH_PEPPOL_INBOUND_MAX_BYTES` (default 4 MiB; PEPPOL UBL is tens of KB). This
   stops a signed-but-oversized POST from exhausting memory on a public route.
   Then read the raw bytes + headers.
3. **Verify HMAC** over the raw body via `verify_inbound_signature`, which
   delegates the constant-time digest to the shared chokepoint
   `webhook_security.verify_hmac_sha256` (its try/except fails closed, so a
   pathological body returns `False` rather than 500-ing the public route). The
   one PEPPOL carve-out is local: an empty secret returns `bool(FEOH_DEBUG)`. The
   header lookup reuses `webhook_security.extract_signature_header` for
   `X-Peppol-Signature` / `X-Signature` / `X-Webhook-Signature`. Bad/missing → 204.
4. Resolve the org from the path slug (control DB). Unknown → 204.
5. The tenant's configured adapter `parse_inbound(headers, body)` →
   `InboundPeppolMessage` (message id, sender scheme/value, doc type, process
   id, payload). `None` / missing message id → 204 (can't dedupe → refuse).
6. In a short-lived tenant session: dedupe pre-check → parse/validate the
   payload with the **existing** `e_invoice.parse_e_invoice` (malformed →
   204, no invoice) → create `Invoice(status=new)` (vendor/amount/currency from
   the UBL; **amount is `Decimal`**) → freeze the invoice's `WorkflowInstance`
   snapshot (`create_workflow_instance`, same as every other ingress — without
   it a later workflow-definition edit retroactively governs an already-ingested
   invoice and the invoice is invisible to every step-based read; see
   `backend/docs/workflow-snapshots.md`) → claim the dedupe slot
   (`PeppolTransmission(direction="inbound", status="delivered", message_id=…)`,
   flushed **before** the S3 upload) → upload the raw payload to S3 →
   `invoice.peppol_received` audit row (PII-free details) → commit.
7. **Outside** the tenant transaction, `dispatch_extraction` — `run_extraction`
   auto-routes the stored UBL/CII to the `einvoice` adapter (confidence 1.0,
   auto-approve); **no** config change, no second parse in the adapter path.

**Dedupe — DB-enforced, the AS4 `MessageId` is the key.** Two layers, the DB
authoritative:

- *Fast path (advisory):* a `SELECT` on `message_id` short-circuits the common
  sequential redelivery without creating an invoice.
- *Authoritative guarantee (the concurrent-redelivery race):* the partial unique
  index `uq_peppol_message_id` (`WHERE message_id IS NOT NULL`, created by
  migration 0034) lets only one transmission INSERT commit. The loser's
  `IntegrityError` on `db.flush()` rolls back its **entire** tenant transaction —
  the Invoice it created included — so no second invoice survives; both
  redeliveries return 204. The transmission row is flushed (claiming the slot)
  **before** the S3 upload, so the loser never writes an S3 object. This mirrors
  `peppol_send`'s claim-the-slot-then-transmit ordering.

**Deliberate divergence from the payment webhook:** Redis
`is_event_already_processed` is **not** used here. For a create-an-invoice
one-time effect the durable DB unique index is the correct guarantee; a 24h
Redis TTL would let a later redelivery slip through and is redundant given the
index.

**No new migration / model column.** The `peppol_transmissions` table already
carries `direction` (CHECK allows `inbound`), the partial-unique `message_id`,
and `status` (CHECK allows `delivered`) — inbound writes zero DDL.

`parse_inbound` is implemented on both adapters: the `mock` adapter parses a
dev-shaped JSON envelope (`message_id` / `sender_*` / `doc_type_id` /
`process_id` / `payload_base64`) **or** raw-UBL-body + `X-Peppol-*` metadata
headers, so the webhook is exercisable locally; the `as4_gateway` adapter maps
the hosted AP's JSON inbound-delivery envelope. The route verifies the HMAC
before either is called.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_PEPPOL_PROVIDER` | `mock` | `mock` (in-process default) \| `as4_gateway` |
| `FEOH_PEPPOL_GATEWAY_URL` | (empty) | Hosted Access Point base URL (deployed only) |
| `FEOH_PEPPOL_GATEWAY_API_KEY` | (empty) | Gateway API key — **no hardcoded fallback**; sops in deployed |
| `FEOH_PEPPOL_INBOUND_ENABLED` | `false` | Master switch for the inbound receive webhook — no-op 204 until on |
| `FEOH_PEPPOL_INBOUND_SIGNING_SECRET` | (empty) | HMAC-SHA256 key the Access Point signs the inbound POST body with — **no hardcoded fallback**; boot refuses if inbound is enabled without it; a NON-secret dev value is set in `.env.development` |
| `FEOH_PEPPOL_INBOUND_MAX_BYTES` | `4194304` | Hard cap (bytes) on the inbound webhook body — oversized POSTs are rejected (204) before buffering/parsing. PEPPOL UBL is tens of KB; 4 MiB headroom |

Per-org overrides live on `Organization.settings.peppol` and win over the
process-level defaults.

## Local-first

The mock adapter is in-process, so there is **no** new long-running service and
**no** new `pnpm` script — `pnpm dev` transmits **and** receives e-invoices
without any PEPPOL credential. Inbound ships a NON-secret dev signing secret
(`FEOH_PEPPOL_INBOUND_SIGNING_SECRET=dev-peppol-inbound-secret`) in
`.env.development` so the webhook is locally testable with a known key. Set
`FEOH_PEPPOL_PROVIDER=as4_gateway` with a real gateway URL + key (sops) and the
real inbound signing secret (sops) to receive/transmit for real.

## Tests

- `tests/test_peppol_adapters.py` — ParticipantId parse/format + PII-free
  errors, mock resolve/send, registry fallback, gateway HTTP (stubbed,
  no-network) incl. the no-key-fallback path, constants.
- `tests/test_peppol_send.py` — happy path (row + one audit), idempotent
  re-send (one adapter call), tax-invalid (no row), unknown receiver, failed-
  then-retry, PII-not-in-logs.
- `tests/test_peppol_route.py` — 200 / already_sent / 404 / 400 / 422 / 403,
  the structured 422 body, and `GET /peppol-transmissions` (empty / populated
  / PII-free / clerk-readable / 404 / tenant- + entity-scoped).
- `tests/test_e_invoice_error_payload.py` — the 422 payload shape, its
  PII-freeness across both validation passes, and that rule ids survive.
- `tests/test_peppol_inbound.py` — inbound receive: signed happy path (one
  Invoice + one inbound transmission), sequential + concurrent-race dedupe (one
  invoice), bad/missing signature, unknown tenant, malformed payload, master
  switch off, no-PII-in-logs/body, boot guard, mock `parse_inbound` unit.
- `tests/test_tenant_provisioning.py` — `peppol_transmissions` fans out + the
  two partial unique indexes are built by `create_all`.
