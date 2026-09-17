# Privacy — DSAR export + right-to-erasure (GDPR / CCPA)

Two coupled data-subject rights, implemented together under the admin-only
`/api/privacy` router:

- **DSAR export** (GDPR Art. 15 / CCPA right-to-know) — assemble everything held
  about a data subject into a portable JSON bundle.
- **Right-to-erasure / anonymization** (GDPR Art. 17 / CCPA right-to-delete) —
  irreversibly redact a subject's PII **while preserving the immutable financial
  + audit record**.

Both are synchronous, admin-gated, and audited. Source:
`app/api/privacy.py`, `app/services/privacy_export.py`,
`app/services/privacy_erasure.py`, `app/services/privacy_documents.py`,
`app/utils/bank_masking.py`, `app/schemas/privacy.py`,
`app/models/data_subject_request.py`, migration `0054_data_subject_requests`.

Both reach beyond the database: the erasure path deletes stored documents,
passkey rows and live sessions, and the export enumerates the same documents.
The rules for which documents go and which stay are in
[Stored documents](#stored-documents--what-erasure-deletes-and-what-it-keeps)
below, and they are the interesting part of this page.

## Subject types

A data subject can live in three places, so the API is parameterised by
`subject_type`:

| `subject_type` | Where the PII lives | `identifier` |
|---|---|---|
| `user` | control-plane `User` (an AP-team member) | the user's email |
| `vendor_user` | tenant `VendorUser` (a supplier-portal login) | the vendor user's email |
| `vendor_contact` | tenant `Vendor` contact fields (the supplier company's own contact details) | the **Vendor UUID** |

`vendor_contact` is addressed by the vendor's id rather than an email because
vendor contact PII has no unique natural key (many vendors share a blank or
duplicate email).

## Endpoints

All three routes are `require_roles(ROLE_ADMIN)` — the privacy-officer
privilege.

### `POST /api/privacy/dsar`

Body: `{ "subject_type": "...", "identifier": "...", "include_banking": false,
"banking_justification": null }`

Resolves the subject (org/tenant-scoped), assembles the bundle, records a
`DataSubjectRequest` row, writes a `privacy.dsar_export` audit row, and returns:

```jsonc
{
  "request_id": "...",
  "subject_type": "vendor_contact",
  "subject_id": "...",
  "generated_at": "2026-06-19T...",
  "data": { /* the portable bundle — see below */ }
}
```

The `data` bundle is intentionally loosely typed (it aggregates heterogeneous
records). Per subject type:

- **user** — `profile` (email, full_name, sso ids, MFA metadata,
  notification_prefs), `roles`, `activity` counts (kept for compatibility), and
  then the actual content: `audit_events` (the rows this user authored),
  `notifications`, `expense_reports` + `expenses`, `passkeys` (metadata only)
  and `documents`.
- **vendor_user** — `profile` + the parent `vendor_id`, the `chat_messages` they
  authored, and `documents`.
- **vendor_contact** — `vendor` (name, code, email, phone, address, tax_id,
  **masked** bank_details + beneficial_owner_data, `banking_disclosure`), a
  summary of `related_invoices` / `related_payments` (id + **string-Decimal**
  amount + status + date — never float), `portal_users`, `contracts`,
  `virtual_cards` (the row stores `last_four` only; no PAN is ever persisted),
  `chat_messages`, `documents`, and `counts`.

Every list-shaped collection is wrapped as
`{ total, returned, truncated, items }` and capped at
`privacy_export.MAX_EXPORT_ROWS` (1000). An export is a synchronous HTTP
response; a user with three years of audit rows would otherwise build an
unbounded JSON document in memory on the event loop. A truncated collection says
so and reports its true total rather than silently shortening.

#### Banking data in a DSAR bundle

`bank_details` and `beneficial_owner_data` are **masked by default**. This was
not always true, and the fix is issue #423: every other surface in the product
(the audit trail, the dual-control change queue, the UI, the logs) reduces a
payee's account / routing / IBAN to a last-4, and the DSAR bundle was the one
path that emitted them whole into a downloadable file — available to any org
admin, for any vendor, with none of the ceremony the *change* path demands.

Masking alone would have been wrong too: a data subject is entitled to their own
personal data, so returning a supplier's bank details *to that supplier* is the
right the endpoint exists to serve. So both exist, and the unmasked one is a
deliberate act:

| | masked (default) | unmasked (`include_banking: true`) |
|---|---|---|
| Who | any admin | an admin **holding `vendor.bank_change.approve`** |
| Subject | any | `vendor_contact` only (nothing else has bank details) |
| Extra input | — | a non-blank `banking_justification` (PII-free) |
| Audit | `privacy.dsar_export` | that, **plus** `privacy.dsar_export.unmasked` carrying the justification |
| Request row | `note: null` | `note: "unmasked banking disclosure: ..."` |
| Response | `banking_disclosure: "masked"` | `banking_disclosure: "unmasked"` |

The unmasked disclosure is a **separate audit action**, not a field on the
routine row, so "who pulled a supplier's full account number" is something an
incident review can grep for.

**What masking does.** `app/utils/bank_masking.py` is the single definition of
what a banking secret is — `api/vendors.py` re-exports it rather than owning it,
so the audit trail and the export cannot disagree. The disclosure masker is an
**allowlist**: known display keys (`bank_name`, `country`, `swift_bic`,
`counterparty_id`, `*_last4`, …) pass through, and everything else — the named
secrets *and any key this module has never heard of* — becomes `"****1234"`. A
newly-added display key therefore renders as `****` in an export until someone
lists it, which is visible and harmless; the reverse default is what shipped the
bug. Beneficial owners are reduced to the ownership relationship (`name`,
`role`, `ownership_percentage`, `country`, `is_pep`) and every identity-document
field is dropped rather than masked — a vendor's ultimate beneficial owner is a
*different* natural person who did not ask for this export, and a last-4 of
someone else's passport number is still someone else's passport number.

**One honest limit.** `ROLE_ADMIN` resolves to every permission in the catalogue,
so on the four stock system roles this gate admits exactly the callers the route
already admits. What it adds is configurability: an org that splits duties with a
custom admin-equivalent role can now deny an unmasked disclosure without denying
DSARs. A step-up MFA proof on the request is the stronger gate; it needs the SPA
to collect that proof and is tracked in `docs/followups.md`.

### `POST /api/privacy/erasure`

Body: `{ "subject_type": "...", "identifier": "...", "confirm": true, "note": "GDPR #42" }`

`confirm` must be `true` (erasure is destructive). Redacts PII in place and
returns:

```jsonc
{
  "request_id": "...",
  "subject_type": "vendor_contact",
  "subject_id": "...",
  "status": "completed",   // or "noop" on a re-run
  "already_erased": false,
  "fields_redacted": 6,
  "record_counts": { "vendors": 1, "vendor_contact_fields": 6, ... },
  "completed_at": "2026-06-19T...",
  "documents_deleted": 2,
  "documents_retained": 3,
  "documents_failed": 0,
  "passkeys_deleted": 0,
  "sessions_revoked": 0
}
```

`note` is an optional, PII-free operator note (legal basis / ticket reference) —
never auto-populated from subject data.

**`documents_failed > 0` means the erasure is incomplete.** A storage object that
could not be deleted leaves its DB pointer intact, so re-running the request
retries it; the counter is on the response and in the audit row rather than
folded into `status`, so an operator sees it without reading the trail.

### `GET /api/privacy/requests`

The privacy officer's request history for the tenant — PII-free (subject UUID +
type + status + counts only).

## What is redacted vs. preserved

The governing rule: **legally-required retention wins over erasure for
transactional rows.** We redact PII *text* fields and keep the money trail.

| Subject type | Redacted | Preserved |
|---|---|---|
| `user` | email → tombstone, full_name, sso_provider/id, hashed_password, mfa_secret; deactivated; **every `WebAuthnCredential` row deleted**; **every live session revoked** | row id, organization_id, role assignments, **every `audit_log` row authored** (the `actor_id` link stays — non-repudiation) |
| `vendor_user` | email → tombstone, full_name, hashed_password, mfa_secret; deactivated; their supplier-authored chat attachments | row id, vendor link, the parent vendor's own documents (the company is a separate subject) |
| `vendor_contact` | vendor email, phone, address, tax_id, bank_details, beneficial_owner_data; the vendor's portal users (as above); supplier-authored **chat message bodies** (free-text PII); the documents listed below | **`vendor.name`** (the legal payee, denormalised onto every Invoice's `vendor_name` money field), **every related Invoice / Payment amount + status + date**, the chat threads + AP-side messages, the `audit_log` |

### Passkeys and sessions

Passkey rows are **deleted, not redacted**: a `WebAuthnCredential` is
authenticator material, not a financial record — there is nothing in it the
money trail needs and every byte of it is a handle to the erased person's
device. Sessions are revoked through the existing
`services/session_management.revoke_user_sessions`, the same path admin
deactivation and password reset use, rather than a second implementation.

Access already stopped before this existed — `get_current_user` re-reads the row
and 401s on `not user.is_active` — so what this closes is the credential material
and the un-blocklisted JTI sitting in Redis until its TTL. Revocation is
best-effort: Redis being unreachable must not fail an erasure whose database half
is the regulated part.

Both legs run **before** the idempotency tombstone check, so a subject erased
before they existed is reached by asking again rather than answered with `noop`.

## Stored documents — what erasure deletes and what it keeps

`app/services/privacy_documents.py` is the one walk from a subject to the objects
held about them in storage, and both legs read it: the export enumerates, the
erasure deletes. It is deliberately one traversal — an export that lists a
document the erasure leg cannot reach is the exact failure the split invites.

**The rule is the same one the database rows follow.** Erasure preserves the
money trail because tax and SOX record-keeping outrank erasure for transactional
rows. Documents split along that line, *not* along "is it about the subject":

| Document | Disposition | Why |
|---|---|---|
| `Vendor.w9_file_key` (W-9 / W-8) | **delete** | The supplier's own signed tax form carrying their TIN. Not the record of a payable. |
| Supplier-authored chat attachment | **delete** | A document the supplier chose to send. Their own. |
| Positive Pay file | **delete** | See below — it is an *instruction*, not a record. |
| `Invoice.file_key` (invoice PDF) | retain | It **is** the invoice — the evidence behind a payable the erasure keeps. |
| `Contract.file_key` | retain | The commitment that authorised the spend. |
| `Expense.receipt_file_key` | retain | The proof behind a reimbursement that was paid. |
| `VendorStatementReconciliation.file_key` | retain | The counterparty document a reconciliation was read from — the only answer to "did we read their statement correctly?". |
| AP-authored chat attachment | retain | Company correspondence about an invoice, not the employee's own data. The personal data in that row is the authorship, which *is* redacted. |

Deleting a retained document would leave a preserved money row with its
supporting evidence destroyed, which is precisely what the retention argument
exists to prevent.

### Why the Positive Pay file is deleted

It looks like transaction evidence and is not. It is an instruction handed to a
bank — the list of cheques we issued, or the accounts authorised to debit us —
and the record of what actually happened lives in `payments` and in the
`positive_pay_files` row, which keeps `item_count`, `total_amount`,
`content_hash` and `account_last4`. Nothing evidential is lost by removing the
rendered file, and the rendered file is the single artefact in this system
carrying every payee's full account and routing number in the clear.

It is multi-subject (one file covers a whole run), so an erasure removes other
payees' coordinates too. That is a reduction in their standing exposure, not a
loss. The same object is also reached on a timer by the `positive_pay` retention
class (see `backend/docs/positive-pay.md` and `docs/decisions.md` §184) — a
retention rule that deletes on a schedule and an erasure that deletes on request
are the same traversal with different triggers.

### Ordering, and what a failure means

Objects are deleted **before** the caller commits. The DB row is the only thing
that knows a document's storage key, so committing the null first and then
failing the delete would orphan the object beyond any future reach — the same
argument `services/tenant_deletion` makes for the whole-tenant case, in the same
direction (documents → database).

A pointer is nulled only for a key that actually went. A failed delete therefore
leaves the row pointing at a real object, the next run retries it, and
`documents_failed` on the response says the erasure is not finished.

### In the export

The same traversal produces the bundle's `documents` manifest: for each object,
its `kind`, `file_key`, owning record, and the `erasure_disposition` an Art. 17
request would apply. It is **references, not bytes** — inlining would base64 up
to 25 MB per document into a JSON response and would put a W-9's taxpayer
identification number into the body of a routine export. Each object stays
retrievable through the download endpoint that already gates it by tenant and
owner.

### Tombstones

Redacted unique fields (email is `UNIQUE` on both `User` and `VendorUser`) are
replaced with `erased+<subject_id>@redacted.invalid` — which stays unique, is
obviously non-deliverable, and lets an operator correlate a row to its erasure
without revealing the original value. Free-text names become `[redacted]`.

### Money trail is never touched

No amount, status, currency, or date is mutated by erasure — only PII text
columns are nulled / tombstoned. This is a project invariant (money is exact);
the erasure service touches no `Numeric`/`Decimal` column.

## Audit trail

Both operations write a **new** append-only audit row through `dispatch_audit`
(`privacy.dsar_export` / `privacy.erasure`). Erasure **never** updates or deletes
an existing `audit_log` row — it respects the migration-0022 immutability
trigger (which rejects every DELETE and every UPDATE touching a column other than
`shipped_at`). The financial/audit history of an erased subject's transactions
therefore survives erasure intact.

The audit `details` and the persisted `DataSubjectRequest` row are **PII-free**:
they carry only the resolved subject UUID + type + non-identifying counts —
never the email / tax-id / bank details that the DSAR *bundle* itself contains.
The bundle is returned in the HTTP response and is never logged or stored.

## Tenant isolation

Subjects span the control plane and the tenant DB. Isolation is enforced at the
data layer:

- The injected `get_tenant` / `get_tenant_db` chokepoint cross-checks the JWT
  `org` claim against the requested tenant (a spoofed `X-Tenant-Slug` alone can't
  widen access).
- `resolve_subject_id` filters by `organization_id` (control-plane `User`,
  tenant `Vendor`) or runs against the tenant DB (`VendorUser`), so a subject in
  tenant A is invisible — neither exportable nor erasable — when acting as
  tenant B. An unresolved subject is a flat `404 Subject not found` (the same
  shape whether it's truly absent or in another tenant, so the response can't be
  used to probe cross-tenant existence).

## Idempotency

Re-running erasure on an already-erased subject is a safe no-op (`status:
"noop"`, `already_erased: true`, no further mutation). The prior run is detected
by the tombstone email (`user` / `vendor_user`) or by all contact fields already
being NULL with no portal user / chat body left to redact (`vendor_contact`).

## Data model + migration

`DataSubjectRequest` (tenant-scoped, `data_subject_requests`, migration
`0054_data_subject_requests`) is the queryable request index — strictly PII-free
(request type/status, resolved subject UUID + type, requesting admin, counts,
timestamps). The migration is gated on the `invoices` table so it no-ops on the
control plane and fans out to every tenant via
`scripts/migrate_all_tenants.py`; it mirrors the model so fresh tenants built via
`tenant_provisioning._create_tenant_tables` (`create_all`) match a migrated one.

## Tests

`backend/tests/test_privacy.py` (real-Postgres `realdb` fixture): DSAR bundle
assembly per subject type; banking masked by default and the four branches of
the `include_banking` gate; the unmasked bundle's separate audit row; the export
returning activity *content* rather than counts; passkey metadata without the
credential material; erasure redacts every PII field; erasure leaves
Invoice/Payment money fields **and** the append-only `audit_log` untouched;
erasure deletes passkey rows and calls the existing session revocation; erasure
idempotency; non-admin 403; and tenant isolation for both export and erasure.

`backend/tests/test_privacy_documents.py`: the masking reducers (including that
an unknown key fails **closed**), the retain-vs-delete split asserted key by key,
that a retained object is never handed to storage at all, that a deleted object's
pointer is nulled and a failed one's is not, and that one key is deleted once
however many rows reference it.

`backend/tests/test_retention.py`: the `positive_pay` class — its short default
window, that the sweep expires the file while keeping the PII-free row, that it
is idempotent, and that a failed object delete leaves the key for the next tick.
