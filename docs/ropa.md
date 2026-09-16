# Record of Processing Activities (RoPA)

**Article 30 GDPR — record of processing activities.** This document is the
controller/processor record of the personal-data processing carried out by the
accounts-payable platform. It is a living document: update it whenever a
processing purpose, data category, recipient/sub-processor, retention window, or
transfer mechanism changes.

> **Roles.** For our own operational data (our employees/users, our auth records)
> we act as a **controller**. For the AP data our customers load into their
> tenants (their vendors, their invoices, their banking details, the expense
> data of *their* employees) we act as a **processor** on the customer's
> behalf — the customer is the controller. The Article 28 terms that govern that
> processor relationship are **published at `/legal/dpa`**
> (`frontend/src/routes/legal/dpa/+page.svelte`); the offline copy in
> [`docs/founder-runbooks/dpa-template.md`](founder-runbooks/dpa-template.md) is
> superseded by it and retained only for a countersigned paper copy.

## Cross-references

| Topic | Document |
|---|---|
| Recipients / sub-processors (who else touches the data) | [`docs/sub-processors.md`](sub-processors.md) — internal working copy; the published register is `/legal/sub-processors` |
| Retention periods (how the windows are enforced) | [`backend/docs/retention.md`](../backend/docs/retention.md) |
| International transfers / data residency | [`docs/data-residency.md`](data-residency.md) |
| Breach notification procedure | [`docs/founder-runbooks/breach-notification.md`](founder-runbooks/breach-notification.md) |
| Cookie / non-essential consent | `frontend/src/lib/components/ConsentBanner.svelte` (banner) |

Retention windows below are summarized from
[`backend/docs/retention.md`](../backend/docs/retention.md); that document is the
authoritative source and the enforcement mechanism (the retention sweep,
`FEOH_RETENTION_*` env vars, the audit-log WORM store). Where a number here and
there diverge, the retention doc wins.

International-transfer notes below are summarized from
[`docs/data-residency.md`](data-residency.md); consult it for the per-region
hosting posture and the Standard Contractual Clauses (SCC) / UK IDTA / adequacy
basis that actually applies to a given customer.

---

## Processing activities

Each row is one processing activity (Art. 30(1)). "Legal basis" cites GDPR
Art. 6 where we are controller; where we are processor the customer (controller)
holds the legal basis and we process under the
[DPA](founder-runbooks/dpa-template.md) (Art. 28) on documented instructions.

### 1. Vendor / supplier PII

| Field | Detail |
|---|---|
| **Purpose** | Maintain the customer's vendor master; match invoices to vendors; pay suppliers; sanctions/KYC screening; 1099/tax reporting; supplier-portal access. |
| **Our role** | Processor (customer is controller). |
| **Legal basis** | Customer's: performance of a contract (Art. 6(1)(b)) with its supplier; legal obligation for tax/sanctions (Art. 6(1)(c)); legitimate interests for fraud prevention (Art. 6(1)(f)). We process on the customer's instructions per the DPA. |
| **Categories of data subjects** | Suppliers/vendors of the customer; vendor contacts; supplier-portal users (`VendorUser`); **beneficial owners and other individuals identified for sanctions / KYC screening**; and — the category that makes much of this personal data at all — **sole traders and other individual suppliers**, whose "business" contact and banking data is personal data *about them*, not about a company. |
| **Categories of personal data** | Vendor/contact name, business email, phone, address; tax ID (EIN/SSN/VAT no.); bank account + routing/IBAN/sort-code (special-handling banking data); W-9/W-8 tax forms; **beneficial-owner / KYC details** (`Vendor.beneficial_owner_data`); sanctions-screening results and the derived risk score. Full bank details and the full `tax_id` are stored, not only last-4 — see the note under § Security measures. |
| **Recipients / sub-processors** | Sanctions/KYC providers (name + country only), ERP integrations, payment rails, e-invoicing/PEPPOL access point, vendor-enrichment lookups — see [`docs/sub-processors.md`](sub-processors.md). |
| **Retention** | Tied to the customer's invoice/financial retention class (default 84 months); supplier-portal accounts purged on customer offboarding. See [`backend/docs/retention.md`](../backend/docs/retention.md). |
| **International transfers** | Per the customer's data-residency tier; SCCs/IDTA where applicable. See [`docs/data-residency.md`](data-residency.md). |

### 2. Employee / User PII (customer's AP staff and our staff)

| Field | Detail |
|---|---|
| **Purpose** | Provision and authenticate platform users; role/permission assignment; approval routing and segregation-of-duties; expense reimbursement; audit/non-repudiation of approvals. |
| **Our role** | Processor for the customer's users; controller for our own staff accounts. |
| **Legal basis** | Customer's: contract (Art. 6(1)(b)) / legitimate interests (Art. 6(1)(f)) for workforce administration. Ours: contract / legitimate interests for our own staff. |
| **Categories of data subjects** | Customer's AP clerks, managers, CFOs, admins; expense claimants (customer employees); our own personnel. |
| **Categories of personal data** | Name, work email, role, login identifiers, MFA enrolment metadata, IP/last-active for access reviews; expense-report claimant identity + receipts; approval actor identity on audit rows. |
| **Recipients / sub-processors** | Email delivery, and — where enabled — audit-log shipping; see [`docs/sub-processors.md`](sub-processors.md). The customer's **SSO/SCIM identity provider** (Okta / Entra ID / Authentik / Keycloak) is *not* a sub-processor of ours: it is the customer's own controller relationship, and we are the party receiving the assertion rather than the party sending data out. See [`docs/sub-processors.md` § 22](sub-processors.md) (Customer-controlled counterparties). |
| **Retention** | User records retained for the life of the customer account; audit rows are append-only WORM and retained per the audit-retention class. See [`backend/docs/retention.md`](../backend/docs/retention.md). |
| **International transfers** | Per the customer's data-residency tier; SCCs/IDTA where applicable. See [`docs/data-residency.md`](data-residency.md). |

### 3. Banking / payment-instrument data

| Field | Detail |
|---|---|
| **Purpose** | Execute payment runs (ACH/wire/check); issue and reconcile virtual cards; positive-pay fraud files; FX-locked international payments; sanctions screening before each payment. |
| **Our role** | Processor (customer is controller). |
| **Legal basis** | Customer's: contract (Art. 6(1)(b)); legal obligation (Art. 6(1)(c)); legitimate interests in fraud prevention (Art. 6(1)(f)). |
| **Categories of data subjects** | Suppliers being paid; virtual-card holders; bank-account holders. |
| **Categories of personal data** | Bank account + routing/IBAN/sort-code, account holder name; virtual-card PAN (single-use reveal only) + last-4; positive-pay account numbers. **Full account and routing numbers are stored**, in `Vendor.bank_details` (JSONB) and in the Positive Pay file in object storage; a card PAN is the only value never persisted. What is guaranteed is narrower and still load-bearing: **no full account number, tax ID or PAN appears in logs, in HTTP error bodies, or in the audit trail** (project invariant). |
| **Recipients / sub-processors** | Payment rails (Modern Treasury, Stripe Treasury, Increase, Column, Dwolla), check printing, card issuers (Lithic/Nium) — see [`docs/sub-processors.md`](sub-processors.md). |
| **Retention** | Payment records tied to the financial-retention class (default 84 months for the tax/audit trail); card PAN never persisted server-side beyond the single-use reveal. See [`backend/docs/retention.md`](../backend/docs/retention.md). |
| **International transfers** | Cross-border where the payment rail or card issuer is non-EEA; SCCs/IDTA per provider. See [`docs/data-residency.md`](data-residency.md). |

### 4. Invoice / financial data

| Field | Detail |
|---|---|
| **Purpose** | Ingest, extract (AI/OCR), code, match (2/3/4-way), approve, and post invoices; expense management; contracts/CLM; analytics and CFO reporting; e-invoicing (PEPPOL); audit trail. |
| **Our role** | Processor (customer is controller). |
| **Legal basis** | Customer's: contract (Art. 6(1)(b)); legal obligation for tax/accounting records (Art. 6(1)(c)). |
| **Categories of data subjects** | Suppliers, supplier contacts, expense claimants, contract counterparties named on documents, and supplier-portal users who write in the chat thread on an invoice. |
| **Categories of personal data** | Names, contact details, and any personal data incidentally present on invoice/contract/receipt documents and line items; extraction results. **Expense receipts** uploaded by the customer's employees, and the corporate-card transactions they are reconciled against. **Supplier-chat message bodies and attachments** (`SupplierChatThread` / `SupplierChatMessage`), authored by both the customer's AP staff and the supplier's own portal users — free text, so it can contain anything either party chose to write. |
| **Recipients / sub-processors** | AI extraction providers (when a customer enables a non-mock adapter), ERP integrations, e-invoicing/PEPPOL access point, object storage — see [`docs/sub-processors.md`](sub-processors.md). |
| **Retention** | Financial-records retention class, default 84 months; terminal invoices soft-archived by the retention sweep. See [`backend/docs/retention.md`](../backend/docs/retention.md). |
| **International transfers** | Per the customer's data-residency tier and any enabled AI/ERP provider; SCCs/IDTA where applicable. See [`docs/data-residency.md`](data-residency.md). |

### 5. Authentication / security data

| Field | Detail |
|---|---|
| **Purpose** | Authenticate users (JWT); MFA/TOTP; SSO (OIDC/SAML) and SCIM provisioning; session/token-blocklist management; sanctions/fraud controls; access reviews and SOX non-repudiation. |
| **Our role** | Processor for the customer's users; controller for our own security telemetry. |
| **Legal basis** | Customer's: contract (Art. 6(1)(b)); legitimate interests in account security (Art. 6(1)(f)). Ours: legal obligation/legitimate interests for security and SOX/SOC 2 controls. |
| **Categories of data subjects** | All platform users (customer staff, our staff) and supplier-portal users. |
| **Categories of personal data** | Login identifiers/email, hashed passwords (`bcrypt_sha256` — never plaintext), TOTP MFA secrets + enrolment state, **WebAuthn passkey credentials** (`WebAuthnCredential`: credential id, public key, sign counter, the label the user gave it — no biometric material ever reaches us, it stays on the authenticator), JWT/session identifiers, IP and timestamps in audit rows, per-session sign-in IP + a coarse device label (e.g. "Chrome on macOS" — the raw `User-Agent` is never stored) shown back to the account holder on `/profile`, approval signatures. |
| **Recipients / sub-processors** | Redis (token blocklist + session records — self-hosted, never a third party), and the centralized audit-log WORM sink (CloudWatch / S3 Object Lock) **where an operator has enabled shipping — it is off by default** — see [`docs/sub-processors.md`](sub-processors.md). The customer's SSO/SCIM identity provider is a counterparty, not a sub-processor: [`docs/sub-processors.md` § 22](sub-processors.md). |
| **Retention** | Session/blocklist entries expire with the token; the per-session IP + device record is torn down with the session it describes (revoke, logout, eviction, expiry) and never outlives the access-token lifetime; audit/security rows are append-only and retained per the audit-retention class. See [`backend/docs/retention.md`](../backend/docs/retention.md). |
| **International transfers** | Per the customer's data-residency tier and any SSO provider's region; SCCs/IDTA where applicable. See [`docs/data-residency.md`](data-residency.md). |

---

## Security measures (Art. 30(1)(g))

A general description — see `docs/soc2-readiness.md` and the project invariants
in the root `CLAUDE.md` for specifics:

- **Tenant isolation** at the data layer (database-per-tenant; header→`feoh_<slug>`
  resolution cross-checked against the JWT `org` claim).
- **Encryption**: every S3 bucket `infra/` defines carries SSE-KMS, versioning,
  public-access blocking and (for the invoice and audit archives) Object Lock,
  and the platform TLS certificate is issued in `infra/acm.tf`. Secrets are sops
  + AWS KMS with no hardcoded fallbacks. **State it that way rather than as a
  flat "encryption in transit and at rest":** the AWS workload stack — managed
  database, API runtime, CDN — is not yet deployed (`infra/README.md`), so this
  describes how the service is built and how it will be operated, not a running
  production estate.
- **Access control**: RBAC (`admin`/`ap_manager`/`ap_clerk`/`cfo`) plus the
  granular permission layer over the fraud-sensitive duties, MFA (TOTP +
  passkeys), SSO, periodic SOX access reviews flagging dormant elevated roles.
- **Auditability**: the audit log is **append-only enforced in the database
  itself** — migration 0022 installs a `BEFORE DELETE` trigger that rejects every
  delete and every update except the shipper's dispatch stamp — with approval
  signatures for non-repudiation. Shipping it onward to a WORM store (S3 Object
  Lock or CloudWatch) is **available and configurable, not running**:
  `FEOH_AUDIT_SHIPPING_ENABLED` defaults to `False` and the default provider is
  `mock`. Do not describe a deployment as shipping to WORM storage unless it has
  been turned on.
- **What an audit row actually contains.** It carries the actor, the action, the
  record, the timestamp, and the fields that changed — **with before/after values
  for ordinary business fields**: `vendor.updated` records `name`, `code`,
  `email`, `phone`, `address`, `payment_terms` and `status` verbatim through
  `audit_access.build_field_diff`, and `invoice.edited` records whatever invoice
  fields moved. That is deliberate; a trail that cannot say what a field changed
  *to* is not a trail. What is reduced is the **restricted** set — the four keys
  in `api/vendors.py::_BANK_SECRET_KEYS` (`account_number`, `routing_number`,
  `wire_routing_number`, `iban`) plus `tax_id` are written as
  `{"old_last4": …, "new_last4": …}`. So no full bank number, tax ID or PAN
  enters the audit trail, but vendor contact and address values do, and they are
  personal data. The same narrowed statement is in
  [`docs/sub-processors.md` § 9](sub-processors.md); keep the two in step.
- **PII/banking minimization in telemetry**: account/PAN/tax-ID data is kept out
  of logs and error responses by invariant.
- **Consent**: non-essential storage gated behind the consent banner
  (`frontend/src/lib/components/ConsentBanner.svelte`); essential JWT auth is
  exempt and disclosed as such.

## Maintenance

Review this record at least annually and on any material change to processing.
When you add an integration/provider, update this RoPA,
[`docs/sub-processors.md`](sub-processors.md) **and the published register at
`/legal/sub-processors`** in the same change — the published page is the one a
customer's DPO reads, and a change to it also triggers the 30-day sub-processor
notice in `/legal/dpa` § 8 (whose notification half is still manual — see
`docs/sub-processors.md` § Maintenance).
