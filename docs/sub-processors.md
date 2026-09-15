# Sub-processor register

A **sub-processor** is any third party that processes personal or financial
data on our behalf when our customers (the *controllers*) use the platform.
GDPR Art. 28(2)–(4) requires us to keep an accurate, up-to-date list of every
sub-processor, the data they touch, and where they process it, and to flow our
Data Processing Agreement (DPA) obligations down to each of them.

This register is the source of truth for that list. Keep it accurate: it feeds
the sub-processor schedule in the customer DPA
(`docs/founder-runbooks/dpa-template.md`, owned separately), the SOC 2 vendor-management module (`docs/founder-runbooks/soc2-vendor.md`
§ Step 6), and the Records of Processing Activities (`docs/ropa.md`).

Related reading:
- `docs/ropa.md` — Records of Processing Activities (GDPR Art. 30).
- `docs/data-residency.md` — where data is stored and processed by region.
- `docs/founder-runbooks/breach-notification.md` — what to do if a
  sub-processor reports (or causes) a personal-data breach.
- Root `CLAUDE.md` § "Adapter patterns" — the authoritative list of every
  pluggable provider and its default.

---

## The local-first design — read this first

**A default install of this platform shares personal/financial data with NO
external sub-processor.** Every external integration is a *pluggable adapter*
behind a registry, and every adapter category ships a `mock` / `console` /
local in-process default. With the shipped defaults:

- AI extraction runs against a local/`mock` adapter (no invoice image leaves
  the box; the committed dev default for the conversational assistant is a
  **local Ollama** model, falling back to `mock`).
- ERP, virtual-card, payment, FX, sanctions/KYC, PEPPOL, audit-shipping, and
  embedding integrations all default to `mock` (in-process, deterministic, no
  network, no credential).
- Outbound email defaults to the `console` adapter (logs to stdout — nothing
  sent).
- Files live in MinIO and databases in local PostgreSQL via Docker Compose —
  no AWS account required.

A provider only becomes an **active sub-processor** when an operator (or an
individual tenant, via `Organization.settings`) explicitly configures it with a
live credential. The platform refuses to silently activate a real provider:
secrets live only in sops-encrypted files in the private `infra-secrets` repo
(AWS KMS) or the deployed environment, and adapters with no key **fail closed**
to their local default rather than calling out.

The **"Active when configured"** column below makes this explicit. A row marked
"Configured only" is *latent* — present in the codebase, dormant until someone
turns it on. The infra rows (AWS) are the exception: a real deployment runs on
AWS by design, so those are active in any deployed environment.

When you assess our sub-processor exposure for a given customer, the honest
answer is **"which adapters has this tenant / this deployment actually enabled"**
— not the full list below.

---

## Data-category legend

| Code | Category |
|------|----------|
| **INV** | Invoice content — line items, amounts, descriptions, dates, invoice/PO numbers |
| **VEND** | Vendor/supplier master data — legal name, address, contact, vendor code |
| **BANK** | Banking / payment-method data — account & routing numbers, IBAN, card PAN |
| **TAX** | Tax identifiers — EIN/SSN/VAT/TIN, W-9/W-8 forms |
| **USER** | Platform user identity — name, email, auth metadata |
| **AUTH** | Authentication artifacts — credentials, SSO/SAML assertions, MFA secrets |
| **AUDIT** | Audit-log events (actor id, action, entity id; field *names*, never values) |
| **DOC** | Uploaded documents / file bytes (invoice PDFs, receipts, contracts, tax forms) |
| **COMMS** | Communications content — email bodies, supplier-chat messages |

Banking/PAN and raw tax-ID values are deliberately minimized: the platform
stores only redacted forms (e.g. `account_last4`, TIN last-4) in the database;
full values exist only in the encrypted payment file or are passed transiently
to the relevant processor at the moment of use. See root `CLAUDE.md` §
"Project invariants" (PII/banking data stays out of logs).

---

## 1. AI extraction (`services/extraction_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Deterministic test extraction | none leaves process | Local | **Default — always** | n/a (no third party) |
| `ollama` | — (local model server) | Local LLM extraction / assistant | DOC, INV | Local / self-hosted | Default for assistant in dev; otherwise configured | n/a (self-hosted) |
| `claude_vision` | **Anthropic** | Claude Vision invoice extraction | DOC, INV, VEND, TAX (whatever the invoice image contains) | US (Anthropic API) | Configured only (`platform` key or BYOK) | DPA — to be confirmed; zero-retention / no-training terms to be confirmed |
| `openai_vision` | **OpenAI** | GPT Vision invoice extraction | DOC, INV, VEND, TAX | US (OpenAI API) | Configured only (BYOK) | DPA — to be confirmed |
| `aws_textract` | **AWS (Amazon Textract)** | OCR / document extraction | DOC, INV, VEND, TAX | Configured AWS region | Configured only | Covered by AWS DPA / GDPR addendum (see Infra) |
| `einvoice` | — (in-process) | Structured UBL/CII/Factur-X parse | INV, VEND, TAX | Local | Auto-selected for structured files; no network | n/a (no third party) |

> Note: structured e-invoices route to the local `einvoice` parser, not to any
> vision provider — those never leave the process.

## 2. ERP integration (`services/erp_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Test ERP | none leaves process | Local | **Default — always** | n/a |
| `merge_dev` | **Merge.dev** | Unified ERP/accounting API | INV, VEND, TAX, BANK (vendor remit data) | US (Merge.dev) | Configured only | DPA — to be confirmed |
| `dynamics_365_bc` | **Microsoft** (Dynamics 365 Business Central) | Direct ERP posting | INV, VEND, TAX | Customer's tenant region | Configured only (direct) | Covered by customer's own Microsoft agreement; our flow-down to be confirmed |
| `netsuite` | **Oracle** (NetSuite) | Direct ERP posting | INV, VEND, TAX | Customer's account region | Configured only (direct) | Covered by customer's own Oracle agreement; our flow-down to be confirmed |

## 3. Virtual cards (`services/card_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Test card issuance | none leaves process | Local | **Default — always** | n/a |
| `lithic` | **Lithic** | Virtual card issuing | VEND, BANK (PAN), amounts | US | Configured only | DPA — to be confirmed |
| `nium` | **Nium** | Virtual card issuing (intl.) | VEND, BANK (PAN), amounts | Region-dependent | Configured only | DPA — to be confirmed |

> Card PANs are never persisted in our DB; reveal is via a single-use token
> against the issuer. We store the last-4 only.

## 4. Payments / payment rails (`services/payment_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Test payment rail | none leaves process | Local | **Default — always** | n/a |
| `modern_treasury` | **Modern Treasury** | Payment orchestration (ACH/wire) | VEND, BANK | US | Configured only | DPA — to be confirmed |
| `stripe_treasury` | **Stripe** | Treasury / payments | VEND, BANK | US (Stripe global) | Configured only | Covered by Stripe DPA; confirm sub-processing schedule |
| `increase` | **Increase** | Bank-rail payments | VEND, BANK | US | Configured only | DPA — to be confirmed |
| `column` | **Column** | Bank-rail payments | VEND, BANK | US | Configured only | DPA — to be confirmed |
| `dwolla` | **Dwolla** | ACH payments | VEND, BANK | US | Configured only | DPA — to be confirmed |
| `checkeeper` | **Checkeeper** | Check printing / mailing | VEND, BANK, full vendor address | US | Configured only | DPA — to be confirmed |

## 5. FX rates (`services/fx_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Test FX rates | none leaves process | Local | **Default — always** | n/a |
| `openexchangerates` | **Open Exchange Rates** | Currency rate lookup | none (currency pair only — no personal data) | US | Configured only | Low risk — no personal data shared; DPA n/a |

## 6. Sanctions / KYC screening (`services/sanctions_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Test screening | none leaves process | Local | **Default — always** | n/a |
| `complyadvantage` | **ComplyAdvantage** | Sanctions / AML / adverse-media screening | VEND, TAX, beneficial-owner names | UK/EU/US | Configured only | DPA — to be confirmed |
| `dowjones` | **Dow Jones** (Risk & Compliance) | Sanctions / watchlist screening | VEND, TAX, beneficial-owner names | US/EU | Configured only | DPA — to be confirmed |
| `refinitiv` | **LSEG / Refinitiv** (World-Check) | Sanctions / watchlist screening | VEND, TAX, beneficial-owner names | US/EU | Configured only | DPA — to be confirmed |

## 7. Email — outbound (`services/email_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `console` | — (stdout) | Logs email, sends nothing | none | Local | **Default — always** | n/a |
| `smtp` | **(operator's relay)** | SMTP delivery (e.g. Mailpit in dev, any relay in prod) | USER, COMMS | Relay-dependent | Configured only | Depends on chosen relay — to be confirmed |
| `ses` | **AWS (Amazon SES)** | Transactional email | USER, COMMS | Configured AWS region | Configured only | Covered by AWS DPA (see Infra) |

## 8. Email intake — inbound (`services/email_intake_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `ses` | **AWS (Amazon SES)** | Inbound email → invoice | DOC, INV, COMMS | Configured AWS region | Configured only | Covered by AWS DPA |
| `mailgun` | **Mailgun (Sinch)** | Inbound email → invoice | DOC, INV, COMMS | US/EU | Configured only | DPA — to be confirmed |
| `generic` | **(operator's provider)** | Generic inbound webhook | DOC, INV, COMMS | Provider-dependent | Configured only | Depends on chosen provider — to be confirmed |

> Email intake is off unless `FEOH_EMAIL_INTAKE_DOMAIN` is set; with it unset no
> inbound provider is active.

## 9. Audit-log shipping (`services/audit_shipping/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-memory) | Test sink | none leaves process | Local | **Default — always** | n/a |
| `cloudwatch` | **AWS (CloudWatch Logs)** | WORM audit-event sink | AUDIT, USER (actor id) | Configured AWS region | Configured only | Covered by AWS DPA |
| `s3_objectlock` | **AWS (S3 Object Lock)** | WORM audit-event archive | AUDIT, USER (actor id) | Configured AWS region | Configured only | Covered by AWS DPA |

> Audit events carry the field **names** touched, never the field values — no
> bank number, tax id, or PAN ever enters the audit trail.

## 10. Embeddings / RAG (`services/embedding_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Deterministic hash-to-vector | none leaves process | Local | **Default — always** | n/a |
| `openai` | **OpenAI** | Text embeddings (duplicate / RAG search) | INV, VEND (invoice/vendor text) | US (OpenAI API) | Configured only | DPA — to be confirmed |

## 11. E-invoicing — PEPPOL (`services/peppol_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Test PEPPOL transmission | none leaves process | Local | **Default — always** | n/a |
| `as4_gateway` | **(hosted PEPPOL Access Point)** | AS4 send/receive onto the PEPPOL network | INV, VEND, TAX | Access-Point-dependent (EU-centric) | Configured only | DPA with the chosen Access Point — to be confirmed |

> Other national / govt e-invoicing clearance providers (Italy SdI, Mexico
> SAT/PAC, Brazil SEFAZ, Colombia DIAN) are deferred — not currently integrated.
> When integrated, add them here with the controller/processor relationship
> noted (several are statutory clearance, not commercial sub-processing).

## 12. Tax filing / TIN validation (`services/tax_filing_adapters/`, `services/tin_validation_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Offline format/structural checks + deterministic filing | none leaves process | Local | **Default — always** | n/a |
| `tax1099` | **Zenwork / Tax1099** | 1099 e-filing + IRS TIN match | VEND, TAX (TIN), amounts | US | Configured only | DPA — to be confirmed |

> Without a live key both degrade to local format-only validation / deterministic
> filing — no TIN leaves the process.

## 13. Supplier financing (`services/financing_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Deterministic financing quotes | none leaves process | Local | **Default — always** | n/a |
| `c2fo` | **C2FO** | Supply-chain finance marketplace | INV, VEND, amounts | US/global | Configured only (skeleton — fail-closed without key) | DPA — to be confirmed |

## 14. Tax rate (`services/tax_rate_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Test tax rates | none leaves process | Local | **Default — always** | n/a |
| `avalara` | **Avalara** | Tax rate / calculation | INV (amounts, jurisdiction), VEND address | US | Configured only | DPA — to be confirmed |
| `taxjar` | **TaxJar (Stripe)** | Tax rate / calculation | INV (amounts, jurisdiction), VEND address | US | Configured only | DPA — to be confirmed |

## 15. Punch-out catalogs (`services/punchout_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Test punch-out | none leaves process | Local | **Default — always** | n/a |
| `cxml` | **(supplier catalog system)** | cXML / OCI catalog punch-out | USER (buyer id), cart line items | Supplier-dependent | Configured only | Supplier relationship — controller↔supplier; our flow-down to be confirmed |

---

## 16. Platform billing (`services/billing_adapters/`)

Distinct from every other row here: this is **our own** billing of the customer,
not the customer's AP money path. The data subject is the customer's billing
admin, and we are the controller for it rather than a processor.

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Deterministic test billing | none leaves process | Local | **Default — always** | n/a |
| `stripe_billing` | **Stripe** | Subscriptions, plan changes, usage reporting, invoices/receipts, saved payment methods (SetupIntent) | USER (org name + admin contact email), billing amounts. **No** bank/tax/PAN data — card details are collected by Stripe directly via SetupIntent and we store only brand/last4/expiry metadata | US (Stripe global) | Configured only (`FEOH_BILLING_STRIPE_API_KEY`) | Covered by the Stripe DPA; confirm the sub-processing schedule |

---

## 17. Chat notifications — outbound (`services/chat_notification_adapters/`)

Approval-event posts into the customer's own Slack or Teams workspace. The
receiving workspace is the **customer's**, so this is closer to a
customer-directed transfer than to a sub-processor of ours — but the message
transits the vendor's servers, so it belongs in the register.

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Test chat post | none leaves process | Local | **Default — always** | n/a |
| `slack` | **Slack (Salesforce)** | Incoming-webhook post + interactive approval buttons | INV (invoice number), VEND (vendor name), amount + currency, status, deep link. PII-free by design — never bank, tax or contact data | US (customer's workspace region) | Configured only (per-org webhook URL) | Customer's own Slack agreement; our flow-down to be confirmed |
| `teams` | **Microsoft (Teams)** | Incoming-webhook MessageCard + approval actions | same as Slack | Customer's tenant region | Configured only (per-org webhook URL) | Customer's own Microsoft agreement; our flow-down to be confirmed |

> The payload shape is enforced in `chat_notification_adapters/base.py` and is
> the same PII-free set the email/in-app templates use. A change that adds a
> line-item description or a contact field moves this row from PII-free to
> COMMS — update it in the same commit.

---

## 18. Vendor enrichment — firmographics (`services/enrichment_adapters/`)

Advisory only: results are suggestions and never overwrite the `Vendor` row.

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Deterministic synthetic firmographics | none leaves process | Local | **Default — always** | n/a |
| `dun_bradstreet` | **Dun & Bradstreet** | D&B Direct+ match + firmographics (`plus.dnb.com`) | VEND (vendor legal name, country) sent as the match query; DUNS, address, industry, employee count returned | US / global | Configured only (per-org key; fails closed) | DPA — to be confirmed |
| `clearbit` | **Clearbit (HubSpot)** | Company enrichment by domain (`company.clearbit.com`) | VEND (vendor domain) sent; firmographics returned | US | Configured only (per-org key; fails closed) | DPA — to be confirmed |

> Raw `tax_id` is **not** transmitted — it is masked to `***<last4>` via
> `vendor_consolidation.mask_tax_id` before it appears in the enrichment
> result. Only the vendor's name/country (D&B) or domain (Clearbit) leaves.

---

## 19. Quality inspections — QMS (`services/qms_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Deterministic pass/fail/partial fixtures | none leaves process | Local | **Default — always** | n/a |
| `generic` | **(customer's QMS)** | Pull inspection records into `quality_inspections` (4-way match) | Inspection numbers, PO references, pass/fail outcomes. Pull-only — nothing personal is sent outbound beyond the query window | Customer-hosted / vendor-dependent | Configured only (per-org `base_url` + `api_key`; fails closed) | Customer relationship; our flow-down to be confirmed |

---

## 20. Infrastructure — Amazon Web Services (AWS)

AWS is the **infrastructure sub-processor** for any deployed environment. Unlike
the adapter rows above, AWS is active in every real deployment by design (local
dev runs entirely on Docker Compose + MinIO + PostgreSQL with no AWS account).
See `docs/production-deployment.md` for the full architecture and
`docs/data-residency.md` for region placement.

| AWS service | Role in the platform | Data categories | Processing location | DPA / sub-processing status |
|-------------|----------------------|-----------------|---------------------|------------------------------|
| **RDS for PostgreSQL** (control plane + per-tenant DBs) | Primary application datastore | INV, VEND, BANK (redacted), TAX (redacted), USER, AUTH, AUDIT | Configured AWS region | Covered by **AWS GDPR DPA** (standard) |
| **S3** (invoice/receipt/contract files, Positive Pay files, audit archive) | Object storage (MinIO equivalent in prod) | DOC, BANK (Positive Pay file holds full account/routing) | Configured AWS region | AWS GDPR DPA |
| **KMS** | Secrets + at-rest encryption keys (SOPS, RDS, S3) | none (key material only) | Configured AWS region | AWS GDPR DPA |
| **SQS** | Async job queues (extraction / ERP / audit in `lambda` mode) | INV, VEND, DOC (job payloads) | Configured AWS region | AWS GDPR DPA |
| **Lambda** | Async workers (extraction, ERP, audit) | INV, VEND, DOC | Configured AWS region | AWS GDPR DPA |
| **CloudFront** | CDN for the static frontend | none (static assets; no personal data in the bundle) | Global edge | AWS GDPR DPA |
| **ALB** | API load balancer | metadata only (IPs, headers in transit) | Configured AWS region | AWS GDPR DPA |
| **ECS / Fargate** | FastAPI API runtime | all in-transit categories | Configured AWS region | AWS GDPR DPA |
| **ElastiCache (Redis)** | JWT blocklist, rate-limit counters, MFA email-OTP hashes | USER, AUTH (short-lived) | Configured AWS region | AWS GDPR DPA |
| **CloudWatch Logs** | App + audit logs | AUDIT, USER (no PII values by design) | Configured AWS region | AWS GDPR DPA |
| **SES** | Transactional + inbound intake email (when configured) | USER, COMMS, DOC | Configured AWS region | AWS GDPR DPA |
| **Route 53** | DNS | none | Global | AWS GDPR DPA |

> The single AWS DPA / GDPR addendum covers all the AWS services above. Confirm
> it is countersigned and that the configured region(s) match the commitments in
> `docs/data-residency.md`.

---

## Maintenance

- **When to update**: any time an adapter is added/removed, a new external
  provider is configured for an operator-managed deployment, a processing
  region changes, or a DPA status is confirmed. Per the project's docs-as-code
  rule, the same change that wires up a provider updates this register.
- **"To be confirmed"** entries are placeholders for the founder/legal to fill
  as DPAs are countersigned — they are not "no DPA", just "not yet recorded
  here". Drive each to a real status (`docs/founder-runbooks/soc2-vendor.md`
  § Step 6 vendor risk reviews is the natural place to do it).
- **Not every outbound hop is a sub-processor.** Two customer-directed egress
  paths deliberately have no row above, because the customer chooses the
  destination and we are merely delivering: outbound Developer-API webhooks
  (`/api/webhooks` — the tenant nominates the target URL) and scheduled report
  delivery (`/api/analytics/scheduled-reports` — the tenant nominates the
  recipient addresses). Both are worth naming in the DPA as controller-directed
  transfers rather than omitting silently.
- **Customer notice**: GDPR Art. 28(2) requires giving controllers advance
  notice and a chance to object before adding a new sub-processor. When a new
  active sub-processor is introduced to a shared/multi-tenant deployment, notify
  affected customers per the DPA's change procedure.
