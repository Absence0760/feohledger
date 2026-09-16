# Sub-processor register

A **sub-processor** is any third party that processes personal or financial
data on our behalf when our customers (the *controllers*) use the platform.
GDPR Art. 28(2)–(4) requires us to keep an accurate, up-to-date list of every
sub-processor, the data they touch, and where they process it, and to flow our
Data Processing Addendum (DPA) obligations down to each of them.

**The customer-facing register is published in the app at `/legal/sub-processors`**
(`frontend/src/routes/legal/sub-processors/+page.svelte`; `docs/decisions.md`
§175). That page is what a customer's DPO reads, and the published Data
Processing Addendum at `/legal/dpa` incorporates it by reference. **This file is
the internal working copy of the same register** — it carries the adapter-level
detail (registry key, module path, config key, DPA status) the public page
deliberately leaves out.

They are one register in two forms. **A change to a provider, to what it
receives, or to whether it is engaged updates both in the same commit**, and
nothing in this file may assert something the published page contradicts. When
the two disagree, the published page is the one with a reader and a contractual
weight, so fix this file to match it — then check whether the published page was
right.

This register also feeds the offline DPA copy
(`docs/founder-runbooks/dpa-template.md`, now superseded by `/legal/dpa`), the
SOC 2 vendor-management module (`docs/founder-runbooks/soc2-vendor.md` § Step 6),
and the Records of Processing Activities (`docs/ropa.md`).

Related reading:
- `/legal/sub-processors` and `/legal/dpa` — the published, customer-facing
  versions of this register and of the Article 28 terms.
- `docs/ropa.md` — Records of Processing Activities (GDPR Art. 30).
- `docs/data-residency.md` — where data is stored and processed by region.
- `docs/founder-runbooks/breach-notification.md` — what to do if a
  sub-processor reports (or causes) a personal-data breach.
- Root `CLAUDE.md` § "Adapter patterns" — the authoritative list of every
  pluggable provider and its default.

---

## The local-first design — read this first

**A local development install of this platform shares personal/financial data
with NO external sub-processor.** Every external integration is a *pluggable
adapter* behind a registry, and every adapter category ships a `mock` /
`console` / local in-process default. On a laptop, with the shipped defaults:

- ERP, virtual-card, payment, FX, sanctions/KYC, PEPPOL, audit-shipping, and
  embedding integrations all default to `mock` (in-process, deterministic, no
  network, no credential).
- Outbound email defaults to the `console` adapter (logs to stdout — nothing
  sent).
- AI extraction resolves to `mock` — but only because there is no key *and* the
  environment is not deployed. See the exception below; this is not a general
  default.
- The committed dev default for the conversational assistant is a **local
  Ollama** model, falling back to `mock` when Ollama is not running.
- Files live in MinIO and databases in local PostgreSQL via Docker Compose —
  no AWS account required.

**A *deployed* install is a different register, and three sub-processors are
engaged before anyone configures anything:**

1. **AWS** — the hosting substrate (§ 20). A real deployment runs on AWS by
   design.
2. **Anthropic** — invoice extraction (§ 1). This is the one adapter family that
   does **not** fail closed to its mock.
   `services/extraction.py::resolve_platform_provider` returns `claude_vision`
   both when a platform key is present *and* when a deployed environment has no
   key at all, because `MockExtractionAdapter.extract` returns a fabricated
   invoice ("Extracted Vendor Inc", 1500.00) — falling back to it would turn a
   missing credential into invented invoice data on a real tenant's document. A
   loud `provider_error` is the lesser harm. The consequence for this register
   is unambiguous: **Anthropic is a default sub-processor on any deployed
   instance**, and the whole invoice document goes to it.
3. **hCaptcha** — bot protection on the public signup form (§ 21). Mandatory:
   `config.py::_require_captcha_in_deployed_envs` **refuses to boot** a deployed
   environment with `FEOH_HCAPTCHA_SECRET` unset, rather than failing the captcha
   open on a public, tenant-creating endpoint.

For everything else a provider becomes an **active sub-processor** only when an
operator (or an individual tenant, via `Organization.settings`) explicitly
configures it with a live credential. The platform refuses to silently activate a
real provider: secrets live only in sops-encrypted files in the private
`infra-secrets` repo (AWS KMS) or the deployed environment, and adapters with no
key **fail closed** to their local default rather than calling out.

The **"Active when configured"** column below makes this explicit. A row marked
"Configured only" is *latent* — present in the codebase, dormant until someone
turns it on. The rows marked **"Deployed — always"** are the three exceptions
above, and they are the only three.

When you assess our sub-processor exposure for a given customer, the honest
answer is **those three, plus whichever adapters this tenant / this deployment
has actually enabled** — not the full list below.

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
| **AUDIT** | Audit-log events (actor id, action, entity id, and the fields that changed — with before/after *values* for ordinary business fields, last-4 only for bank and tax fields; see § 9) |
| **DOC** | Uploaded documents / file bytes (invoice PDFs, receipts, contracts, tax forms) |
| **COMMS** | Communications content — email bodies, supplier-chat messages |

**Card PANs are never persisted** — a reveal is a single-use call to the issuer
and we keep the last 4 only.

**Bank details and tax IDs are a different story, and this register must not
overstate it.** The full values *are* stored: `Vendor.bank_details` (JSONB) holds
the account and routing numbers / IBAN, and `Vendor.tax_id` is a plain
`String(50)`. What is minimized is everything *around* them — the audit trail
records a bank or tax change as last-4 only (§ 9), enrichment masks `tax_id`
before any outbound call (§ 18), the payment rails are handed a pre-registered
counterparty reference rather than an account number (§ 4), and PII/banking data
is kept out of logs and error bodies by invariant (root `CLAUDE.md` § "Project
invariants"). `docs/decisions.md` §175 records why the published pages state this
plainly rather than reaching for the flattering version.

---

## 1. AI extraction (`services/extraction_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-process) | Deterministic test extraction | none leaves process | Local | Default on a **non-deployed** environment with no key — never the deployed fallback | n/a (no third party) |
| `ollama` | — (local model server) | Local LLM extraction / assistant | DOC, INV | Local / self-hosted | Default for assistant in dev; otherwise configured | n/a (self-hosted) |
| `claude_vision` | **Anthropic** | Claude Vision invoice extraction | DOC, INV, VEND, TAX — the whole document as uploaded, plus the guiding context: the org's chart of accounts and fields from earlier invoices used as worked examples | US (Anthropic API) | **Deployed — always**, unless `FEOH_EXTRACTION_PROVIDER` names another adapter or the org is BYOK. `resolve_platform_provider`: a platform key selects it, and a deployed env with **no** key still selects it (never `mock`) | DPA — to be confirmed; zero-retention / no-training terms to be confirmed |
| `openai_vision` | **OpenAI** | GPT Vision invoice extraction | DOC, INV, VEND, TAX | US (OpenAI API) | Configured only (BYOK) | DPA — to be confirmed |
| `aws_textract` | **AWS (Amazon Textract)** | OCR / document extraction | DOC, INV, VEND, TAX | Configured AWS region | Configured only | Covered by AWS DPA / GDPR addendum (see Infra) |
| `einvoice` | — (in-process) | Structured UBL/CII/Factur-X parse | INV, VEND, TAX | Local | Auto-selected for structured files; no network | n/a (no third party) |

> Note: structured e-invoices route to the local `einvoice` parser, not to any
> vision provider — those never leave the process.

### 1.1 Other features on the same Anthropic connection

Four further features talk to Anthropic. Each is a separate switch, but they all
**reuse the extraction API key**, so none of them adds a sub-processor you did not
already have — each one widens what Anthropic receives. Two of them default OFF
*because* they share a key: defaulted on, an operator who configured Anthropic for
invoice extraction would silently have begun sending it data for a second,
unrelated purpose that no setting disclosed.

| Feature | Module | Switch | What is sent to Anthropic |
|---|---|---|---|
| Conversational AP assistant | `services/assistant/` | `FEOH_ASSISTANT_PROVIDER` — default `mock`; `.env.development` uses local `ollama`; `claude` auto-downgrades to `mock` with no key | The user's question and the structured tool results the answer is composed from — vendor spend, cash-flow forecasts, invoice lists, pending approvals |
| Audit-log summarization | `services/audit_summary.py` | `FEOH_AUDIT_SUMMARY_ENABLED` — **default `False`** | Invoice number, vendor name, amounts and the invoice's audit timeline. Deliberately no remit-to bank details, no PANs, no full addresses |
| LLM anomaly / fraud analysis | `services/llm_fraud_detection.py` | per-org `settings.invoice_warnings.llm_anomaly_enabled` — **default `False`** | The candidate invoice plus the vendor's last 8 approved invoices: amounts, currencies, descriptions, payment methods, PO numbers and the **supplier's remit-to address**. The widest personal-data set of the four |
| Exception-agent decision rationale | `services/exception_agents/llm_rationale.py` | **none of its own** — fires wherever the extraction key resolves | The deterministic draft rationale plus the exception's facts: amounts, variances, PO numbers, GL codes |

> `audit_summary_enabled` flipped from `True` to `False` on 2026-09-15 for exactly
> this reason — see the comment on it in `backend/app/config.py`, which is the
> long-form version of the paragraph above.
>
> **The rationale polish is the one to watch**: it has no feature flag, so
> enabling extraction enables it. The *decision* stays 100% rules-derived (the
> model only rewords the sentence, and the agent behaves identically offline), but
> the facts still leave the process. If that ever needs to be switchable, it needs
> a flag of its own rather than a docstring.

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
| `complyadvantage` | **ComplyAdvantage** | Sanctions / AML / adverse-media screening | VEND — the vendor's name as `search_term`, its ISO country code as a filter. Nothing else | UK/EU/US | Configured only | DPA — to be confirmed |
| `dowjones` | **Dow Jones** (Risk & Compliance) | Sanctions / watchlist screening | VEND — the vendor's name as `search-term`, its country code. Nothing else | US/EU | Configured only | DPA — to be confirmed |
| `refinitiv` | **LSEG / Refinitiv** (World-Check) | Sanctions / watchlist screening | VEND — the vendor's name, its country code as a `nationality` filter. Nothing else | US/EU | Configured only | DPA — to be confirmed |

> **The transmitted set is narrower than the screening function's own inputs, and
> that is worth stating precisely because the previous version of this table got
> it wrong.** `vendor_screening.screen_vendor_record` passes `vendor_tax_id` and
> `beneficial_owners` into `adapter.screen_vendor(...)`, and **all three real
> adapters drop them** — no tax ID and no beneficial-owner name is serialised into
> any outbound body. Only the name and country go on the wire, which is what
> `/legal/sub-processors` § 3.5 publishes.
>
> **That minimisation is now guarded.**
> `backend/tests/test_sanctions_adapter_minimisation.py` pins each adapter's
> outbound body **exactly** — the whole field set, not "no tax ID in it", because
> a new field carrying a beneficial owner's date of birth would pass a substring
> check and still be an undisclosed flow to a sub-processor. Adding a field is
> not forbidden; it is a decision that has to update this table and
> `/legal/sub-processors` § 3.5 in the same change, which is what an exact
> assertion forces someone to notice. The guard was verified red — adding
> `body["tax_id"]` to the ComplyAdvantage adapter fails two of its cases — not
> merely asserted to work.

## 7. Email — outbound (`services/email_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `console` | — (stdout) | Logs email, sends nothing | none | Local | **Default — always** | n/a |
| `smtp` | **(operator's relay)** | SMTP delivery (e.g. Mailpit in dev, any relay in prod) | USER, COMMS | Relay-dependent | Configured only | Depends on chosen relay — to be confirmed |
| `ses` | **AWS (Amazon SES)** | Transactional email | USER, COMMS | Configured AWS region | Configured only | Covered by AWS DPA (see Infra) |

## 8. Email intake — inbound (`services/email_intake_adapters/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `ses` | **AWS (Amazon SES)** | Inbound email → invoice | COMMS (the sender's address, the tenant's intake address, the subject), DOC + INV (**every attachment** — which is to say the invoice documents themselves) | Configured AWS region | Configured only | Covered by AWS DPA |
| `mailgun` | **Mailgun (Sinch)** | Inbound email → invoice | same set | US/EU | Configured only | DPA — to be confirmed |
| `generic` | **(operator's provider)** | Generic inbound webhook | same set | Provider-dependent | Configured only | Depends on chosen provider — to be confirmed |

> Email intake is off unless `FEOH_EMAIL_INTAKE_DOMAIN` is set; with it unset no
> inbound provider is active. It is worth reading this section next to § 7 rather
> than skimming past it: the **inbound** provider is the one that sees an
> unfiltered third party's message — a supplier's own From address, whatever they
> put in the subject, and the raw bytes of everything they attached — before any
> of it reaches a tenant. Outbound (§ 7) only ever carries what we composed.

## 9. Audit-log shipping (`services/audit_shipping/`)

| Adapter | Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|---------|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| `mock` | — (in-memory) | Test sink | none leaves process | Local | **Default — always.** Shipping itself is off by default too (`FEOH_AUDIT_SHIPPING_ENABLED=False`, `FEOH_AUDIT_SHIPPING_PROVIDERS=mock`), so no deployment ships to a WORM store unless an operator turns it on | n/a |
| `cloudwatch` | **AWS (CloudWatch Logs)** | WORM audit-event sink | AUDIT, USER (actor id) | Configured AWS region | Configured only | Covered by AWS DPA |
| `s3_objectlock` | **AWS (S3 Object Lock)** | WORM audit-event archive | AUDIT, USER (actor id) | Configured AWS region | Configured only | Covered by AWS DPA |

> **Careful with this claim — the previous version of it was wrong.** Audit
> events carry the names of the fields that changed **and, for ordinary business
> fields, their before/after values**: `vendor.updated` records `name`, `code`,
> `email`, `phone`, `address`, `payment_terms` and `status` verbatim through
> `audit_access.build_field_diff`, and `invoice.edited` records whatever invoice
> fields moved (`api/invoices.py`). That is deliberate — a trail that cannot say
> what a field changed *to* is not a trail, and the bank-redirect case is exactly
> the one it has to prove.
>
> What is reduced is the **restricted** set. The four keys in
> `api/vendors.py::_BANK_SECRET_KEYS` — `account_number`, `routing_number`,
> `wire_routing_number`, `iban` — plus `tax_id` are recorded as
> `{"old_last4": …, "new_last4": …}`. So **no full bank number, tax id or PAN
> enters the audit trail**, but vendor contact and address values do, and they are
> personal data. `docs/ropa.md` § Security measures carries the same narrowed
> statement; keep the two in step.

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
| `tax1099` | **Zenwork / Tax1099** | 1099 e-filing + IRS TIN match | VEND (recipient legal name), **TAX — the FULL, unmasked taxpayer identification number**, form type + box amounts, our vendor id | US | Configured only | DPA — to be confirmed |

> **This is the only opt-in integration that transmits a raw taxpayer
> identification number, and it does so on both of its paths** — treat it as the
> most sensitive row in this register. Filing posts `recipient_tin` alongside
> `recipient_name`, the form type and the per-box amounts to `/efile/1099/batch`
> (`tax_filing_adapters/tax1099_adapter.py`); TIN matching posts
> `{"tin": <digits>, "name": <legal name>, "tin_type": …}` to `/tinmatch/verify`
> (`tin_validation_adapters/tax1099_adapter.py`). Neither masks.
>
> Without a live key both degrade to a local format/structural check
> (`tin_validation_adapters/format_rules.py`) and a deterministic filing record —
> no TIN leaves the process, and `name_match` reports `None` rather than a
> fabricated pass.

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
the adapter rows above, AWS is engaged in every real deployment by design (local
dev runs entirely on Docker Compose + MinIO + PostgreSQL with no AWS account).

**This section used to describe the reference architecture in
`docs/production-deployment.md` as though it were built. It is not.** RDS, SQS,
Lambda, CloudFront, ALB, ECS/Fargate and ElastiCache appear nowhere in `infra/`,
and listing them told a customer's DPO to record managed services that hold none
of their data. Two things are true instead, and the table below states only
those:

- **What Terraform actually defines** (`infra/*.tf`, applied 2026-09-15) is a
  security substrate, not a workload stack: four S3 buckets, one customer-managed
  KMS key + alias, the Route 53 hosted zone and domain registration, an ACM
  certificate, and a monthly cost budget. Everything is in **`us-east-1`**
  (`var.aws_region`). `infra/README.md` says so in its first paragraph: "Real AWS
  workload resources (ECS, ALB, RDS, CloudFront) are not yet defined here."
- **The near-term deployment shape** is `docs/minimal-deployment.md`: a single
  **EC2** instance (t4g.small) running Caddy, the FastAPI container,
  **PostgreSQL 16 and Redis 7 in Docker Compose**, with S3 for files and backups.
  There is no managed database, no queue and no CDN in that shape — the control
  plane and every tenant DB live on the instance's own encrypted disk, and the
  `local` dispatch mode keeps extraction / ERP / audit work in in-process worker
  threads (no SQS, no Lambda).

| AWS service | Role in the platform | Data categories | Processing location | Status | DPA / sub-processing status |
|-------------|----------------------|-----------------|---------------------|--------|------------------------------|
| **EC2** | The one VM: API runtime, and the PostgreSQL + Redis containers beside it — so the control-plane and per-tenant databases and the token blocklist all sit on its disk | Everything the service holds: INV, VEND, BANK, TAX, USER, AUTH, AUDIT, COMMS | us-east-1 | Deployed (`docs/minimal-deployment.md`) — **not** Terraform-managed today | Covered by **AWS GDPR DPA** (standard) |
| **S3** — `invoice-files`, `audit-logs`, `backups`, `access-logs` | Object storage for uploaded documents, the WORM audit archive, database backups, and the S3 server-access-log sink | DOC, BANK (a Positive Pay file carries full account + routing numbers, because matching on those is what the file is for), AUDIT | us-east-1 | **Defined in `infra/s3.tf`** — SSE-KMS, versioning, public-access block, Object Lock on invoice-files (governance 365d) + audit-logs (compliance 7y) | AWS GDPR DPA |
| **KMS** | One customer-managed key (+ alias) for the buckets and for sops-encrypted deployment secrets; annual rotation on | none (key material + encrypt/decrypt call metadata) | us-east-1 | **Defined in `infra/kms.tf`** | AWS GDPR DPA |
| **Route 53** | Hosted zone for the platform domain and every tenant subdomain, plus the domain registration itself (auto-renew, transfer lock, WHOIS privacy) | none (DNS query metadata; the subdomain label is the tenant slug) | us-east-1 / global edge | **Zone created by the registration; registration adopted in `infra/domain.tf`** | AWS GDPR DPA |
| **ACM** | TLS certificate for the platform domain + `*.<domain>` | none | us-east-1 | **Defined in `infra/acm.tf`** (us-east-1 is mandatory for CloudFront and Route 53 Domains) | AWS GDPR DPA |
| **Budgets** | Monthly cost guardrail + email alerts | none (our own billing contact) | us-east-1 | **Defined in `infra/budgets.tf`** | n/a — our own account data |
| **CloudWatch Logs** | Application logs, and the audit-event sink when `s3_objectlock` is not the selected shipper | AUDIT, USER (actor id); see § 9 for exactly what an audit row carries | Configured AWS region | **Not in `infra/`** — reachable as an audit-shipping target (§ 9), off by default | AWS GDPR DPA |
| **SES** | Transactional (§ 7) and inbound intake (§ 8) email | USER, COMMS, DOC | Configured AWS region | **Not in `infra/`** — configured only | AWS GDPR DPA |

> **Not built, and not to be listed until they are:** RDS, SQS, Lambda,
> CloudFront, ALB, ECS/Fargate, ElastiCache. They are the scale-up target in
> `docs/production-deployment.md`. When one lands, it gets a row here **and** on
> `/legal/sub-processors` in the same commit, with the § Maintenance customer
> notice.
>
> The single AWS DPA / GDPR addendum covers every AWS service above. Confirm it is
> countersigned. On region: everything is in one region and `docs/data-residency.md`
> is an advisory setting nothing routes on — do not read the per-region language
> there as a commitment (`/legal/dpa` § 14 states this to customers directly).

---

## 21. Bot protection — hCaptcha (`utils/hcaptcha.py`)

The only third party a visitor meets **before they are a customer at all**, and
the only row here that is not an adapter. It sits on the public self-service
signup form and nowhere else — it is not present anywhere inside the signed-in
application.

| Processor | Service | Data categories | Processing location | Active when configured | DPA / sub-processing status |
|-----------|---------|-----------------|---------------------|------------------------|------------------------------|
| **hCaptcha (Intuition Machines, Inc.)** | Bot protection on `POST /api/signup`; server-side token verification against `hcaptcha.com/siteverify` | The **requester's IP address** (`remoteip`), the challenge token, and whatever browser signals hCaptcha's own in-page script collects. No invoice, vendor, payment or tax data | US | **Deployed — always** | DPA — to be confirmed |

> **Mandatory, not optional.** `config.py::_require_captcha_in_deployed_envs`
> raises at boot if `FEOH_HCAPTCHA_SECRET` is unset in a deployed environment, so
> the service refuses to start rather than fail the captcha open on a public,
> tenant-creating endpoint. With the secret empty (local dev only)
> `verify_captcha` is a no-op that logs a WARNING and contacts nobody — which is
> why this is "Deployed — always" rather than "Configured only".

---

## 22. Customer-controlled counterparties — NOT our sub-processors

Some connections send personal data outward without engaging a sub-processor of
*ours*, because the destination is a system the customer already controls under
their own agreement with its vendor. We are delivering the customer's data back
to them, not appointing someone to process it on our behalf. These belong in the
**customer's** Article 30 record as their own relationships, and
`/legal/sub-processors` § 4 says so to customers in the same terms.

| Counterparty | Where it connects | What crosses the boundary | Relationship |
|---|---|---|---|
| **The customer's identity provider** — Okta, Microsoft Entra ID, Authentik, Keycloak, or any other OIDC / SAML 2.0 IdP | `/api/auth/sso`, `/api/auth/saml`, `/scim/v2` | Inbound: an authentication assertion plus the user attributes the customer chooses to release (name, email, groups). Outbound SCIM: nothing — SCIM is the IdP pushing to us | **The customer's own controller relationship.** We hold their users' identities; their IdP does not gain ours. Not a sub-processor of ours |
| **Direct ERP connections** — NetSuite (Oracle), Dynamics 365 Business Central (Microsoft) | § 2 | Invoices posted into the customer's own ERP tenant | Customer's own agreement with Oracle / Microsoft. Listed in § 2 for the data description, not as our sub-processor |
| **Supplier catalogue punch-out** | § 15 | Buyer session identity + cart contents, to the supplier's own catalogue system | Customer ↔ supplier |
| **Outbound Developer-API webhooks** | `/api/webhooks` | Event payloads, to a URL the tenant nominates | Customer-directed transfer (see § Maintenance) |
| **Scheduled report delivery** | `/api/analytics/scheduled-reports` | Report contents, to recipient addresses the tenant nominates | Customer-directed transfer |

> Slack and Teams sit on the line — the receiving workspace is the customer's,
> but the message transits the vendor's infrastructure — so they keep their row in
> § 17 rather than moving here.
>
> `docs/ropa.md` § 2 and § 5 point at this section for the SSO/SCIM recipient
> entry; it previously pointed at a section that did not exist.

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
- **Customer notice — and the gap in it.** GDPR Art. 28(2) requires giving
  controllers advance notice and a chance to object before adding a new
  sub-processor, and both `/legal/sub-processors` § 9 and `/legal/dpa` § 8 commit
  us publicly to **30 days'** notice plus a right to object and, failing
  resolution, to terminate the affected part of the service.

  **No notification mechanism exists.** There is no notification list, no
  subscribe endpoint and no email template that reaches a customer's DPO. The
  whole mechanism today is: update this file, update `/legal/sub-processors`,
  change its last-updated date, and add a dated row to its § 10 change log — plus
  a manual mail to anyone who has written in asking to be told. Do not describe
  this as "per the DPA's change procedure" as though a procedure ran it. The
  commitment is contractual, so this is a promise we cannot currently keep on the
  notification half. Tracked in `docs/followups.md` (§ (c), "No mechanism backs
  the 30-day sub-processor notice"); the durable fix is a per-org notification
  fired from a changelog entry, so adding a register row and notifying are one
  action rather than two.

  Until then, **do not add or promote a sub-processor without doing the notice by
  hand first** — promoting a provider from "Configured only" to "Deployed —
  always", or changing what an existing one receives, both count.
