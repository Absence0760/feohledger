# Data Processing Addendum (DPA) — OFFLINE TEMPLATE, SUPERSEDED

> **⚠️ THE AUTHORITATIVE DPA IS THE PUBLISHED ONE AT `/legal/dpa`.**
> `frontend/src/routes/legal/dpa/+page.svelte` is the Data Processing Addendum
> that is actually in force: it is incorporated by reference into the Terms of
> Service, takes effect without a signature (Art. 28(9) is satisfied by a written
> electronic form), and is written against what the code actually does. It is
> longer, more specific, and materially more accurate than this file. **Quote it,
> link to it, and keep it current — not this one.**
>
> **This file is retained for exactly one case**: a customer whose procurement
> process needs an executed, countersigned counterpart on paper. `/legal/dpa` § 3
> promises such a copy on request to the legal contact, and this is the skeleton
> for producing it — signature block, bracketed party fields, annexes in a form
> that can be printed and initialled. **A countersigned copy must reproduce the
> substance of `/legal/dpa`, not this summary**; read that page first and carry
> across anything it says that this file does not.
>
> **NOT LEGAL ADVICE, and not counsel-reviewed.** Do not send it to a customer,
> sign it, or rely on it until a qualified data-protection lawyer has reviewed and
> adapted it. Bracketed `[…]` fields must be completed. Where this file and a
> customer's negotiated terms conflict, the executed agreement governs; where this
> file and `/legal/dpa` conflict, **`/legal/dpa` governs**.
>
> **Corrections already applied here, because they would otherwise be copied into
> a signed contract.** Each was a place where the boilerplate said something the
> system does not do: no SOC 2 report exists (§ 9); there is no running encrypted
> production database and no WORM shipping in force (Annex II); residency is an
> advisory setting nothing routes on (§ 8, Annex I); the "[48–72] hours" breach
> window was invented (§ 7); deletion does not reach uploaded documents and there
> is no tenant-deprovisioning routine at all (§ 11); the retention engine archives
> and never deletes (§ 11); the sub-processor objection had no remedy (§ 5); and
> the CCPA section was missing two of its required terms (§ 10). See
> `docs/decisions.md` §175.

---

## Data Processing Addendum

This Data Processing Addendum ("**DPA**") forms part of the agreement for
services (the "**Agreement**") between **[Customer legal name]** ("**Controller**")
and **[Your company legal name]** ("**Processor**") and governs the Processor's
processing of Personal Data on the Controller's behalf in connection with the
accounts-payable platform (the "**Service**"). It is the offline counterpart of
the Addendum published at `/legal/dpa`, which governs in the event of any
difference.

Where the Controller is itself a processor for a third party, this DPA applies
on a back-to-back basis and the Processor acts as a sub-processor.

### 1. Definitions

"**Personal Data**", "**processing**", "**controller**", "**processor**",
"**data subject**", "**personal data breach**", and "**supervisory authority**"
have the meanings given in the **GDPR** (Regulation (EU) 2016/679) and, where
applicable, the **UK GDPR** and **Data Protection Act 2018**. "**Data Protection
Laws**" means all privacy and data-protection laws applicable to the processing,
including the GDPR, UK GDPR, and the **CCPA/CPRA** where the Controller's data
includes California consumers' personal information. Other capitalized terms have
the meaning given in the Agreement.

### 2. Subject-matter, duration, nature and purpose

- **Subject-matter & nature**: hosting and processing of accounts-payable data
  — vendor records, invoices, expenses, contracts, payment instructions, and the
  authentication of the Controller's users — to provide the Service.
- **Purpose**: solely to provide, maintain, secure, and support the Service in
  accordance with the Controller's documented instructions (the Agreement, this
  DPA, and the Controller's use of the Service's configuration controls).
- **Duration**: for the term of the Agreement and until deletion/return under
  Section 11.
- **Types of Personal Data and categories of data subjects**: as described in
  **Annex I** below and in the Processor's Record of Processing Activities,
  [`docs/ropa.md`](../ropa.md).

### 3. Processor obligations

The Processor shall:

1. process Personal Data **only on the Controller's documented instructions**,
   including with regard to international transfers, unless required by law (in
   which case it will notify the Controller unless legally prohibited);
2. ensure persons authorized to process Personal Data are bound by
   **confidentiality**;
3. implement appropriate **technical and organizational measures** (Article 32),
   as summarized in **Annex II**;
4. respect the conditions in Section 5 for engaging **sub-processors**;
5. **assist** the Controller (Section 6) in responding to data-subject requests;
6. assist the Controller with security, breach notification, DPIAs, and prior
   consultations (Articles 32–36);
7. at the Controller's election, **delete or return** Personal Data at the end of
   the engagement (Section 11);
8. make available information necessary to demonstrate compliance and allow for
   and contribute to **audits** (Section 9);
9. immediately inform the Controller if, in its opinion, an instruction infringes
   Data Protection Laws.

### 4. Controller obligations

The Controller warrants that it has a lawful basis for the processing, that its
instructions are lawful, and that it has provided any notices and obtained any
consents required for the Processor to process the Personal Data.

### 5. Sub-processors

1. The Controller provides **general authorization** for the Processor to engage
   sub-processors (Article 28(2); SCC Clause 9(a) Option 2). The current list is
   **published at `/legal/sub-processors`**, which identifies each sub-processor,
   what it does, the categories of data it can receive, where it processes, and —
   the part that matters — which are engaged for every customer versus which stay
   dormant until the Controller or an operator switches them on.
   [`docs/sub-processors.md`](../sub-processors.md) is the internal working copy
   of the same register.
2. The Processor will give the Controller **at least 30 days' prior notice**
   of any intended addition or replacement of a sub-processor, by updating the
   published register at `/legal/sub-processors` (with a dated change-log entry)
   and notifying the Controller's nominated contact. The Controller may
   **object** on reasonable data-protection grounds, in writing, within those 30
   days. *(The number is fixed at 30 to match `/legal/dpa` § 8 and
   `/legal/sub-processors` § 9 — it is a published commitment, not a negotiable
   bracket.)*
3. **If the objection cannot be resolved, the Controller has a remedy.** The
   Processor will work with the Controller in good faith — by proposing a
   commercially reasonable alternative, by configuring the Service so the
   sub-processor is not used for the Controller's tenant where the architecture
   allows it, or by explaining why it believes the concern is met. If the
   objection is not resolved within a reasonable period, the Controller may
   **terminate the affected part of the Service without penalty**, on written
   notice, with a pro-rata refund of fees paid for the unused remainder of the
   term. A good-faith objection is not a breach by the Controller. *(Article
   28(2) gives the Controller the right to object; an objection with no
   consequence is not a right, which is why this limb exists.)*
4. Where a change is needed urgently to protect the security or availability of
   the Service, the Processor may make it sooner than 30 days and will tell the
   Controller as soon as reasonably possible, with the reason. The right to
   object survives.
5. The Processor will impose **data-protection obligations no less protective**
   than this DPA on each sub-processor by written contract and remains **liable**
   for its sub-processors' performance.

> **Operational note, not contract text.** The notification half of limb 2 is
> currently manual: there is no notification list, no subscribe endpoint and no
> email template that reaches a customer's DPO. Updating the register and mailing
> the nominated contact by hand is the whole mechanism. Tracked in
> `docs/followups.md`; see `docs/sub-processors.md` § Maintenance. Do not sign a
> counterpart of this document without knowing that.

### 6. Assistance with data-subject rights

Taking into account the nature of the processing, the Processor shall assist the
Controller by appropriate technical and organizational measures, insofar as
possible, to respond to data-subject requests to exercise rights of access,
rectification, erasure, restriction, portability, and objection. Where a data
subject contacts the Processor directly, the Processor will, where permitted,
forward the request to the Controller and not respond except on the Controller's
instruction.

### 7. Personal data breach

The Processor shall notify the Controller **without undue delay** after becoming
aware of a personal data breach affecting the Controller's Personal Data, and
shall provide the information the Controller reasonably needs to meet its own
notification obligations (Articles 33–34) — in phases as more becomes known,
rather than waiting for the investigation to close. The Processor's internal
breach-handling procedure is in
[`docs/founder-runbooks/breach-notification.md`](breach-notification.md).

**No hour count is given, and none should be added.** Article 33(2) sets no
numeric deadline for the processor→controller leg — the standard is "without
undue delay", full stop. The 72 hours in Article 33(1) is the *Controller's*
obligation to its supervisory authority, running from the Controller's own
awareness, and it is the Controller who decides whether the breach is notifiable
and whether Article 34 communication to data subjects is required. The
breach-notification runbook says the same thing in its own words ("no 72h grace
for the processor→controller notice"), and `/legal/dpa` § 11 says it to
customers. An invented "[48–72] hours" would be a contractual commitment
narrower than the law requires in some cases and looser in others, made up to
sound reassuring.

### 8. International transfers

1. The Processor shall not transfer Personal Data outside the EEA/UK except in
   accordance with the Controller's instructions and a valid transfer mechanism.
2. Where the Processor or a sub-processor processes Personal Data in a country
   without an **adequacy decision**, the parties incorporate the **EU Standard
   Contractual Clauses (SCCs)** (Module appropriate to the relationship) and,
   for UK data, the **UK International Data Transfer Addendum (IDTA)** / UK
   Addendum to the SCCs, completed as set out in **Annex III**.
3. **There is no per-region hosting today, and this clause must not imply one.**
   All customer data sits in a **single region** (AWS `us-east-1`). The Service
   exposes a data-residency setting and an administrator can pin a tenant to a
   region, but **nothing routes on it**: the selection is recorded, audited, and
   reported back as "misaligned" where it differs from the region the stack
   actually runs in, and that is the whole of its effect. It is an advisory
   preference, not a residency guarantee.
   [`docs/data-residency.md`](../data-residency.md) documents the intended model
   and says so in its own "Current reality" section; `/legal/dpa` § 14 states it
   to customers directly. A Controller with a hard residency requirement must not
   be told this setting satisfies it.
4. Consequently, for a Controller in the EEA, the UK or Switzerland, using the
   Service **involves a transfer of personal data to the United States**, and the
   SCCs (with the UK Addendum where UK data is involved) are the baseline
   mechanism rather than a contingency. Enabling a sub-processor in § 5 adds that
   provider's own location to the picture, which is the Controller's decision to
   record in its transfer impact assessment.

### 9. Audit rights

The Processor shall make available to the Controller information necessary to
demonstrate compliance with Article 28 and allow for and contribute to audits,
including inspections, conducted by the Controller or an auditor it mandates,
**[once per 12-month period, on at least 30 days' written notice, during business
hours, subject to confidentiality]**, scoped to the systems and records used to
process the Controller's personal data. Those limits fall away where an
inspection is required by a supervisory authority with jurisdiction over the
Controller, or follows a personal data breach affecting the Controller's data.

**The Processor holds no SOC 2 or ISO 27001 report and must not offer one.**
[`docs/soc2-readiness.md`](../soc2-readiness.md) is a *readiness plan* — the
route to a Type II examination and a list of what is still outstanding — not an
attestation, and there is no report in existence to produce. Offering "our
current SOC 2 report" in a signed contract would be a promise that cannot be
performed on the day it is called. In the first instance the Processor discharges
this section by providing: this DPA and its annexes; the published sub-processor
register; its record of processing activities so far as it relates to the
Controller's data; the description of measures in **Annex II** (including the
*Measures not claimed* list); written answers to a reasonable security or privacy
questionnaire; and, on request, the Controller's own tenant audit trail — which
the Controller can also export itself at any time.

### 10. CCPA/CPRA terms

To the extent the Controller's Personal Data includes the personal information of
California residents, the Controller is the **Business**, the Processor acts as a
**service provider**, and this DPA is the written contract the CCPA/CPRA
requires. The Processor shall not:

(a) **sell** the personal information, or **share** it for cross-context
    behavioural advertising, for any purpose and for no consideration of any
    kind;
(b) retain, use, or disclose it for any purpose other than the business purposes
    specified in the Agreement — including for any commercial purpose other than
    providing the Service — or as otherwise permitted by the CCPA/CPRA;
(c) **retain, use, or disclose it outside the direct business relationship**
    between the Controller and the Processor; or
(d) combine it with personal information received from or on behalf of anyone
    else, or collected from the Processor's own interaction with a consumer,
    except as the CCPA and its regulations permit for a service provider.

The Processor certifies that it understands the restrictions in this section and
will comply with them, and will provide the same level of privacy protection the
CCPA requires. It will assist the Controller, by the mechanisms in Section 6, in
responding to consumer requests to know, delete, correct, limit the use of
sensitive personal information, and opt out, and imposes these same restrictions
on every sub-processor by written contract.

**The Processor will notify the Controller if it determines that it can no longer
meet its obligations under the CCPA.** The Controller may take reasonable and
appropriate steps to stop and remediate any unauthorised use of personal
information, and may, on notice, audit the Processor's compliance on the terms in
Section 9. *(Limbs (c) and the notice-of-inability sentence are required terms
under § 1798.100(d) and the CPPA regulations; both were missing from earlier
versions of this template.)*

### 11. Deletion or return on termination

On expiry or termination of the Agreement, and at the Controller's choice, the
Processor shall **delete or return** the Controller's Personal Data, unless
retention is required by law. The Controller tells the Processor which it wants
within **30 days** of termination; during that window the data remains available
for export and no deletion action is taken. Deletion means destroying the tenant
database (`feoh_<slug>`), the control-plane organisation and user records, and
the object-storage key prefix holding uploaded invoices, receipts, contracts and
tax forms, and is completed within **60 days**, confirmed in writing.

**This clause is qualified deliberately. Do not restore the unqualified
"and delete existing copies".** Four things deletion does not reach, and a signed
promise that it does would be unperformable:

1. **Uploaded documents are not reached by the in-product erasure function.**
   `services/privacy_erasure.py` redacts the databases and touches object storage
   not at all — no stored invoice, receipt, contract or tax form is deleted by it.
   Deleting those is a manual operator action on request. (`/legal/dpa` § 9 states
   this to customers; it is also in `docs/known-issues.md`.)
2. **There is no tenant-deprovisioning routine.** The only `DROP DATABASE` in the
   codebase is `tenant_provisioning._drop_postgres_database`, which rolls back a
   *failed provisioning attempt* so a partial failure does not leak an orphan
   database. End-of-contract deletion is therefore a hand-run operator procedure,
   not a feature — write it down before promising a 60-day window to a customer
   who will hold you to it.
3. **Backups are whole-system snapshots and are not selectively edited.** The
   Controller's data persists in them until they age out on the ordinary backup
   lifecycle, currently **90 days** (`infra/s3.tf`, `backup_retention_days`).
   Until then it remains subject to this DPA, is not restored or accessed except
   to recover the Service as a whole, and is deleted when the backup expires.
4. **The append-only audit log is never deleted.** Migration 0022 installs a
   `BEFORE DELETE` trigger that rejects every delete and every update except the
   shipper's dispatch stamp, so neither the application nor an administrator nor
   the Processor can rewrite it. Audit rows carry the actor, action, record and
   what changed, with bank and tax fields reduced to last-4, and are retained as
   evidence of the integrity of the financial record.

**On retention generally:** the per-record-class windows the Controller configures
(default **84 months** for financial records) are enforced by a sweep that
**archives and never deletes** — it sets an `archived_at` marker, writes a
manifest, and never touches an audit row. [`backend/docs/retention.md`](../../backend/docs/retention.md)
is the reference for it, but cite it for *archival* windows, not for "deletion
windows": that engine performs no deletion of any kind.

### 12. Liability and precedence

Liability under this DPA is subject to the limitations in the Agreement. In the
event of conflict between this DPA and the Agreement on the subject of personal
data, **this DPA prevails**; the SCCs prevail over both on the subject of
restricted transfers.

---

## Annex I — Details of processing

| Item | Detail |
|---|---|
| **Categories of data subjects** | Controller's vendors/suppliers and their contacts; **sole traders and other individual suppliers**, whose business contact and banking data is personal data about them; Controller's employees/users (AP staff, approvers, administrators, expense claimants); supplier-portal users; **beneficial owners and other individuals identified for sanctions, KYC or tax screening**; contract counterparties; and anyone else named in an uploaded document or a free-text field. |
| **Categories of personal data** | Names, business contact details, postal addresses, role/login identifiers; tax IDs (EIN/SSN/VAT/TIN) and uploaded W-9/W-8 forms; bank account / routing / IBAN / sort-code and account-holder name; virtual-card metadata (last four digits only — a full number is never stored); beneficial-ownership and screening results; invoice, purchase-order, goods-receipt, credit-memo, payment and approval records; expense claims, corporate-card transactions and uploaded receipts; contract records and documents; supplier-chat messages and attachments; authentication data including password hashes, MFA enrolment state, WebAuthn passkey credentials and session records carrying sign-in IP and a coarse device label; and audit records. See [`docs/ropa.md`](../ropa.md) for the full mapping. |
| **Audit-record content** | The actor, action, record, timestamp and the fields that changed — **with before/after values for ordinary business fields** (vendor name, code, email, phone, address, status; whichever invoice fields moved). Bank fields (`account_number`, `routing_number`, `wire_routing_number`, `iban`) and `tax_id` are recorded as last-4 only, so no full account number, tax identifier or card number enters the trail. |
| **Special categories** | None intended. The Controller shall not load Article 9 special-category data or Article 10 criminal-offence data into the Service except where expressly agreed in writing. Bank details and tax identifiers are not special categories, but both parties treat them as requiring restricted handling. |
| **Frequency** | Continuous, for the term. |
| **Nature & purpose** | As in Section 2. |
| **Retention** | Within the term, per-record-class windows the Controller configures, defaulting to **84 months** for financial records. Records past their window are **archived by an audited sweep, never hard-deleted**; audit records are append-only and are not deleted at all. See [`backend/docs/retention.md`](../../backend/docs/retention.md), and Section 11 for end-of-term deletion and its four stated limits. |
| **Hosting and transfers** | **One region — AWS `us-east-1`, United States.** There is no per-region deployment and the data-residency setting does not route anything (Section 8). For a Controller in the EEA, the UK or Switzerland this is a transfer to the United States on the SCCs, with the UK Addendum where UK data is involved. |

## Annex II — Technical and organizational measures

Summarized; [`docs/ropa.md`](../ropa.md) § Security measures has the detail, and
**`/legal/dpa` Annex II is the authoritative, per-measure version** — reproduce
that table in a countersigned copy rather than this paragraph.

**In force today:** tenant isolation at the data layer, through a single
chokepoint that cross-checks the organisation claim in the caller's signed token
against the tenant being requested; RBAC over four system roles plus a granular
permission layer over the fraud-sensitive duties, enforced server-side;
segregation of duties on approval and on clearing a payment-blocking exception;
passwords hashed with bcrypt over an HMAC-SHA256 pre-hash, TOTP MFA and WebAuthn
passkeys, step-up re-authentication, per-account failure budgets, SSO over OIDC
or SAML with SCIM provisioning; an **append-only audit log enforced by the
database itself** (a trigger rejects every delete and every update but the
shipper's stamp); dual control on supplier bank-detail changes; exact-decimal
money and idempotent money-moving writes; secrets sops-encrypted under AWS KMS in
a private repository with no hardcoded fallbacks; webhook HMAC verification and
event-id de-duplication; PII and banking data kept out of logs and error
responses by invariant, with automated tests guarding it; every S3 bucket the
infrastructure code defines carrying SSE-KMS, versioning, public-access blocking,
server-access logging and (invoice + audit archives) Object Lock; non-essential
storage gated behind a consent banner.

**Do not assert these flatly — both were overstated in earlier versions:**

- **"Encryption in transit and at rest."** True of what exists, and the intent is
  firm, but **the AWS workload stack — database, API runtime, CDN/load balancer —
  is not yet deployed** (`infra/README.md`: "Real AWS workload resources (ECS,
  ALB, RDS, CloudFront) are not yet defined here"). The correct form is the one
  `/legal/dpa` Annex II uses: this describes how the Service is built and how it
  will be operated, together with a commitment not to process production personal
  data other than over an encrypted transport and on encrypted-at-rest storage.
- **"Audit log shipped to a WORM store."** The shipper exists and can write to S3
  Object Lock (compliance mode) or CloudWatch Logs, but
  `FEOH_AUDIT_SHIPPING_ENABLED` defaults to `False` and the default provider is
  `mock`. It is **available and configurable, not running**. The append-only
  guarantee in the primary database is the one that holds unconditionally.

**Measures not claimed** — reproduce this list in any countersigned copy, because
a measures annex is only useful if its omissions are visible: no SOC 2 and no ISO
27001 certification, and no report of either to produce under Section 9; no
third-party penetration test; no 24/7 security operations centre and no
continuous security monitoring; no data-residency guarantee (Section 8);
in-product erasure does not reach uploaded documents (Section 11); write-once
audit archival is off by default.

## Annex III — Sub-processors and transfer mechanisms

The current sub-processor list and each processor's location / transfer basis is
**published at `/legal/sub-processors`**, which is incorporated into this DPA and
into Annex III of the SCCs by reference; [`docs/sub-processors.md`](../sub-processors.md)
is the internal working copy of the same register. That page also marks which
entries are engaged for every customer and which are merely available until an
operator or a tenant enables them.

Three sub-processors are engaged in **every deployed environment**, without
anyone configuring anything: the infrastructure provider (AWS), **Anthropic** —
which reads uploaded invoices by default, because the extraction adapter
deliberately does *not* fall back to a mock that would fabricate invoice fields —
and **hCaptcha** on the public signup form, which the Service refuses to boot
without. Every other entry becomes active only when a credential is configured.

The SCC modules and clause selections are set out in Section 8 and, in their
authoritative form, in `/legal/dpa` § 14 (Module Two controller-to-processor, or
Module Three where the Controller is itself a processor and for onward transfers;
Clause 7 docking applies; Clause 9 Option 2; Clause 17 Option 1, law of Ireland).
Any UK IDTA completion is recorded in the executed transfer documentation.

---

### Signatures

| | Controller | Processor |
|---|---|---|
| Name | `[…]` | `[…]` |
| Title | `[…]` | `[…]` |
| Date | `[…]` | `[…]` |
| Signature | `[…]` | `[…]` |

> **Reminder:** counsel must review before use, and **`/legal/dpa` is the
> authoritative text** — a countersigned copy must reproduce its substance, not
> this summary. See the warning at the top.
