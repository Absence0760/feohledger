# Vendor Management

## Overview

Vendors enter the system through three channels. Each has a different trust level and verification requirement.

```
ERP Sync ─────────> Active (trusted — verified by ERP)
Manual Create ────> Active (admin-verified on creation)
AI from Invoice ──> Unverified (draft) ──> Review ──> Active or Rejected
```

## Vendor Sources

| Source | How it enters | Initial status | Verification |
|---|---|---|---|
| **ERP Sync** | Pulled from connected ERP via `POST /api/vendors/sync-erp` | `active` | Trusted — ERP is source of truth |
| **Manual** | Created by admin/AP manager via the UI | `active` | Verified on creation (by the creating user) |
| **AI Extracted** | Auto-created when invoice extraction finds no matching vendor | `unverified` | Requires human review before payment |

## Vendor Lifecycle

```
                 ┌──────────┐
 ERP Sync ──────>│  Active   │<──── Verify
                 └────┬─────┘
                      │
 Manual Create ──────>│
                      │
                 ┌────┴─────┐      ┌──────────┐
 AI Extract ───>│Unverified │─────>│ Rejected  │
                 └──────────┘      └──────────┘
                                        │
                      ┌─────────────────┘
                      v
                 ┌──────────┐
                 │ Inactive  │  (deactivated, preserved for history)
                 └──────────┘
```

| Status | Meaning | Can receive payments? |
|---|---|---|
| `active` | Verified, fully operational | Yes |
| `unverified` | AI-created, needs human review | No — blocked from payment runs |
| `inactive` | Deactivated by admin | No |
| `rejected` | Flagged as invalid or duplicate | No |

> **Note (2026-10-06):** the code does not currently enforce this — see docs/followups.md § Surfaced by writing the help centre.

## Vendor Matching

When an invoice is extracted (via AI/OCR), the system attempts to match the extracted vendor name to an existing vendor. This avoids duplicate vendor creation and links invoices to the correct vendor record.

`services/vendor_matching.match_and_link_vendor` is the single matcher, and it
runs on **every** path that can set an invoice's vendor — not just extraction:

| Caller | When |
|---|---|
| `services/extraction` | After an AI/OCR extraction resolves a vendor name |
| `POST /api/invoices` (manual, no-OCR entry) | On create, so a hand-keyed invoice is linked too |
| `PATCH /api/invoices/{id}` | When the vendor name is (re)saved **and** the link is stale (name changed) or missing. Clearing the name to blank clears `vendor_id` instead — the matcher no-ops on an empty name, and a nameless invoice must not keep an uncorroborated link |
| `services/review.approve_invoice(corrections=...)` | Same rule as the PATCH path, applied at approval time: when `corrections` includes a vendor-name change (`corrections["vendor"]`) and the resulting link is stale or missing, `vendor_id` is re-resolved before the invoice is finalized. Without this, an approver correcting a misread vendor NAME left `Invoice.vendor_id` pointing at the pre-correction vendor even though the displayed name changed — the payment run reads the payee's bank details off `vendor_id`, not `vendor_name`, so the money could still route to the wrong supplier despite a visibly correct approval |

That matters beyond tidy data: `Invoice.vendor_id` — not the free-text
`vendor_name` — is what the credit-memo vendor guard compares, and that guard is
fail-closed (an invoice with no resolved vendor cannot be credited at all). A
rename that left the old link in place, or a manual entry that never got one,
would respectively mis-attribute or block a credit. Re-saving an invoice's
vendor name is also the supported way to resolve an invoice that predates
create-time resolution. See `docs/api-reference.md` § Credit Memos.

Vendors created by the manual-entry path are stamped `source = manual` rather
than `ai_extracted` (`match_and_link_vendor(..., source=...)`); matching itself
is identical.

### Matching Logic (priority order)

1. **Tax ID match** — if the extracted invoice has a vendor tax ID, exact-match against `vendor.tax_id`. Confidence: 1.0.
2. **Exact name match** — case-insensitive match on `vendor.name`. Confidence: 0.98.
3. **Fuzzy name match** — normalize both names (strip suffixes like Inc/LLC/Ltd, remove punctuation, collapse whitespace) and compute Jaccard token similarity. Address overlap boosts the score. Threshold: 0.6.

### Matching is scoped to the invoice's entity (subsidiary)

All three lookups run against **the invoice's own `entity_id` ∪ vendors whose
`entity_id` is NULL** — `vendor_matching._candidate_query`, built on the shared
`tenant.apply_entity_scope(..., include_shared=True)`. Without that scope, a
multi-entity tenant could link subsidiary A's invoice to subsidiary B's vendor
row; because `Invoice.vendor_id` is what the fail-closed credit-memo guard
compares, such a mislink has a money consequence (one subsidiary's credit
applying against another's payable).

What a NULL `entity_id` means differs from `gl_accounts`, where NULL is a
deliberate "shared chart" marker. On `vendors` it means the row was never
stamped with a subsidiary — a pre-multi-entity row that migration `0029`'s
backfill didn't reach, or one auto-created from an invoice that itself carried
no entity. Those rows stay matchable from **every** entity: a supplier is a
real-world counterparty, not subsidiary-private data, and excluding them would
not fail loudly — it would silently mint a duplicate vendor, splitting the
supplier's spend rollup and giving it a second, independently editable
bank-detail record. When the same supplier exists both unstamped and under the
invoice's own entity, the entity's own row wins (the candidate query orders it
first).

`match_and_link_vendor` derives the entity from `invoice.entity_id`, so no
caller passes it explicitly — including an inter-company **mirror** payable,
which sits under the counterparty entity and therefore matches against the
counterparty's vendors automatically. The two exception-agent resolvers that
call `match_vendor` directly (`missing_po_v1`, `multi_po_split_v1`) pass
`entity_id=invoice.entity_id` so an agent can't reach a same-named vendor in
another entity and then PO-match across subsidiaries.

`entity_id=None` (an unstamped invoice, or a caller with no entity in hand) is a
passthrough that searches the whole tenant — the pre-multi-entity behaviour.
That is also why **single-entity tenants see no change at all**: with one default
entity every vendor is either under it or NULL, so `entity ∪ NULL` admits the
whole table.

Ordering also makes the pick deterministic: the tax_id and exact-name lookups
take the first ordered row rather than `scalar_one_or_none()`, so the same
supplier legitimately registered under two subsidiaries (duplicate `tax_id`)
resolves to one row instead of raising and turning invoice creation into a 500.

### Match Outcomes

| Confidence | Action |
|---|---|
| >= 0.8 | Auto-link invoice to vendor |
| 0.6 - 0.8 | Link but flag for review |
| < 0.6 | Create new unverified vendor from invoice data |

### What gets extracted to the new vendor

When no match is found, a new vendor is created with:
- `name` from `invoice.vendor_name`
- `address` from `invoice.vendor_address`
- `tax_id` from `invoice.vendor_tax_id`
- `status` = `unverified`
- `source` = `ai_extracted`

## ERP Vendor Sync

The system can pull the vendor master list from the connected ERP. This ensures vendor data (names, codes, payment terms, bank details) stays in sync.

### Sync Behavior

| Scenario | Action |
|---|---|
| ERP vendor not in local DB | Create new vendor with `source=erp_sync`, `status=active` |
| ERP vendor matches by `erp_vendor_id` | Update fields if changed, update `erp_synced_at` |
| ERP vendor matches by name (no `erp_vendor_id` yet) | Link existing vendor to ERP, update fields |
| Unverified vendor matches an ERP vendor by name | Auto-verify → set `status=active`, `source=erp_sync` |

### Sync Endpoint

`POST /api/vendors/sync-erp` — triggers a sync pull. Currently uses mock data; in production, calls the ERP adapter to fetch the vendor list.

Each synced vendor stores:
- `erp_vendor_id` — the vendor's ID in the external ERP (for two-way mapping)
- `erp_synced_at` — timestamp of last sync

### Every changed vendor writes an audit row

`services/vendor_sync.sync_vendors_from_erp` writes an append-only
`vendor.synced_from_erp` row for each vendor it **actually changes** — the same
per-row shape `services/qms_sync` uses for the inspections it upserts, and the
reason `POST /api/vendors/sync-erp` carries no `dispatch_audit` of its own
(`tests/test_audit_append_only.py` records that cross-module chokepoint in
`_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT`).

A vendor is a **payee**: before this, a bulk ERP pull could materialise new
payee rows with nothing in the trail saying where they came from or who ran the
pull — while a single-vendor `POST /api/vendors` has always written
`vendor.created`.

| `details.change` | Means |
|---|---|
| `created` | A brand-new payee row |
| `updated` | Fields refreshed on a vendor already linked by `erp_vendor_id` |
| `linked` | A manually-created vendor adopted by an ERP id (this is also what can promote it out of `unverified`) |

A record the pull leaves byte-identical (`unchanged`) writes **nothing** — an
audit row for a state change that did not happen is unbounded growth describing
nothing.

`details` carries `change`, `erp_vendor_id`, `name` and `code` — the same two
vendor fields `vendor.created` records. The ERP payload also carries `tax_id`,
`email`, `phone` and `address`, and the sync writes all four onto the vendor
row; **none of them reach the trail**, which is append-only, undeletable
(migration 0022) and shipped to a WORM store.

## Bulk and credential paths audit too

Every path that creates or changes a vendor writes an append-only audit row
(project invariant #3), not just the single-row `POST` / `PATCH` endpoints. The
bulk ones were the gap: a per-MODULE audit check let one auditing handler vouch
for its unaudited neighbours, so three vendor-touching paths ran without one.

| Path | Action | `details` |
|---|---|---|
| `POST /api/vendors/sync-erp` | `vendor.synced_from_erp` | `change`, `erp_vendor_id`, `name`, `code` |
| `POST /api/vendors/import-csv` | `vendor.imported_csv` | `name`, `code`, `source` |
| `POST /api/vendors/{id}/portal-users` | `vendor_user.invited` | `vendor_user_id` |

**CSV import** goes through `services/csv_import`, which writes one row per
vendor actually created (a skipped duplicate creates nothing and writes
nothing). Same PII floor as the ERP sync — `name` and `code` only, never the
`tax_id`, `email`, `phone` or `address` columns the CSV may carry. The invoice
importer writes the same row for each vendor stub it mints for an unknown
supplier. See [`csv-import.md`](csv-import.md) § Audit trail.

**Portal invites** write `vendor_user.invited`, keyed on the **vendor** so it
sits on the same trail as the `vendor_user.deleted` row the revoke has always
written. Provisioning a supplier credential is the segregation-of-duties half
that used to leave no trace: an account that can submit invoices and stage
bank-detail changes could be created, used and deleted, and the trail would show
only the deletion. `details` is `{"vendor_user_id": ...}` — never the supplier's
login address, and never the temp password the invite email carries (the
response body no longer carries one at all).

The invite also stamps `vendor_users.provisioned_by_user_id` with the AP actor,
and `POST /api/vendors/{id}/portal-users/{vendor_user_id}/reset-password`
re-stamps it (audit action `vendor_portal_user.password_reset`, `details`
`{"vendor_id": ...}`). That column is what
`POST /api/vendors/change-requests/{id}/approve` reads to refuse a bank-change
approval by the person who minted the submitting identity's password — see
[`supplier-portal.md`](supplier-portal.md) § Credential provenance and the BEC
dual control.

## Vendor Fields

| Field | Type | Description |
|---|---|---|
| name | String | Vendor display name |
| code | String | Short code (e.g., "OSC") |
| email | String | Contact email |
| phone | String | Contact phone |
| address | String | Full address |
| tax_id | String | EIN / VAT number |
| payment_terms | String | Default payment terms |
| bank_details | JSONB | Bank account info (routing, account, SWIFT) — see [Bank details: two routing numbers](#bank-details-two-routing-numbers) |
| accepts_virtual_cards | Boolean | Whether vendor accepts card payments |
| status | String | active / unverified / inactive / rejected |
| source | String | manual / erp_sync / ai_extracted |
| verified_by | String | Who verified the vendor |
| verified_at | DateTime | When verified |
| erp_vendor_id | String | Vendor ID in the external ERP |
| erp_synced_at | DateTime | Last ERP sync timestamp |

## Bank details: two routing numbers

Larger US banks publish a **different ABA for incoming Fedwires than for ACH**.
One generic routing number cannot express a payable wire at those banks, so
`bank_details` carries both:

| Key | Rail family | Notes |
|---|---|---|
| `routing_number` | ACH / domestic (`ach`, `international_ach`, `rtp`, `check`) | The ORIGINAL, generic key. Every row already stored under it means the ACH number, so it keeps that meaning — **no backfill, no reinterpretation of stored data**. |
| `wire_routing_number` | Wire (`wire`, `international_wire`) | Optional. Recorded only when the bank publishes a separate Fedwire ABA. |
| `routing_last4` / `wire_routing_last4` | — | Display-only last-4s, the shape the AP UI records (the full numbers live with the processor / are supplied by the vendor). |

`bank_details` is a JSON column, so this needed **no migration**.

### Which number a rail is instructed with

`services/payment_adapters/base.resolve_routing_number(vendor_bank, method)` is
the single resolver, surfaced on every payload as `PaymentPayload.routing`
(`RoutingSelection(number, source)` — `source` is the PII-free label `wire` /
`ach` / `none`, safe to log; `number` is banking data and goes only into the
outbound processor request).

* A **wire** rail prefers `wire_routing_number` and **falls back** to
  `routing_number`. A bank publishing a single ABA uses it for both, so
  refusing there would break every vendor at a smaller institution.
* An **ACH** rail reads `routing_number` and deliberately does **not** fall back
  to the wire number. The asymmetry is the point: a bank with two ABAs will not
  accept an ACH file addressed to its Fedwire number, so borrowing it converts a
  missing-data problem into a returned item days later at the vendor's expense.
  Missing means missing.

Adapters read `payload.routing`, never `vendor_bank["routing_number"]` — that
key is the ACH number and would misroute a wire. Today every live adapter
identifies the payee by a **processor counterparty token**
(`vendor_bank["counterparty_id"]`) and never transmits raw coordinates, so the
resolver's live consumers are the `mock` adapter (which reports the selected
`routing_source` in its `raw_response` so the choice is observable in local dev
and e2e) and any future adapter that hands a bank raw coordinates. The Positive
Pay **ACH-authorization** file (`services/positive_pay.py`) reads
`routing_number` directly and is correct unchanged — it is an ACH artefact.

### Validation

`schemas/vendor.validate_bank_routing_fields` structurally checks every
routing-shaped key that is actually **present**: both ABA fields against the ABA
checksum (`utils/banking.validate_aba_routing`) and `sort_code` against the UK
6-digit shape. An international payee legitimately carries `iban` / `swift_bic`
and no ABA at all — requiring one would refuse a whole class of real vendors —
but a value that IS supplied and malformed is refused with a 422, because the
alternative is a fat-fingered digit that surfaces days later as a returned or
misdirected payment.

It runs at two points, and the split matters:

* `api/vendors._stage_ap_bank_change` — the single staging chokepoint all three
  AP entry points (`POST /vendors`, `PATCH /vendors/{id}`,
  `POST /vendors/{id}/bank-change`) go through.
* `api/vendors.approve_change_request` — the single point where ANY staged
  change is applied to the vendor row, AP- or supplier-portal-submitted. The
  portal's schema takes a free-form dict, so re-validating at apply time is what
  stops a malformed ABA reaching the row through a route with a looser schema.

It is deliberately **not** a Pydantic `field_validator`. FastAPI renders a
`ValidationError` as a 422 whose body echoes the rejected `input` — and the
input is the whole `bank_details` dict, account number included. Validating
there turned "your routing number has a typo" into a response body carrying
banking data. Raising from the chokepoint lets the 4xx name only the FIELD.

### It travels the dual-control BEC gate like every other banking field

`wire_routing_number` is not a new door. `POST /vendors` strips bank details off
the insert and STAGES them; `PATCH /vendors/{id}` stages rather than applies;
`POST /vendors/{id}/bank-change` is the canonical staging path — and a SECOND
user holding `vendor.bank_change.approve` (who cannot be the proposer) approves.
A field that applied inline would be exactly the one-person bank redirect the
gate exists to prevent.

It is also in `api/vendors._BANK_SECRET_KEYS`, so the audit trail records only
that it changed plus a last-4 — never the number.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/vendors` | List vendors (filterable by status, source, search) |
| `GET` | `/api/vendors/{id}` | Get single vendor |
| `POST` | `/api/vendors` | Create vendor (manual, admin-verified) |
| `PATCH` | `/api/vendors/{id}` | Update vendor fields |
| `DELETE` | `/api/vendors/{id}` | Delete vendor |
| `POST` | `/api/vendors/{id}/verify` | Verify an unverified vendor → active |
| `POST` | `/api/vendors/{id}/reject` | Reject a vendor → rejected |
| `POST` | `/api/vendors/sync-erp` | Pull vendors from connected ERP |

### The paginated lists issue a fixed number of queries

Three list endpoints in `api/vendors.py` each carry one supporting lookup that
must be asked **once per page**, never once per row:

| Endpoint | The per-row lookup | How it is asked once |
|---|---|---|
| `GET /api/vendors` | invoice tally per vendor | one grouped `SELECT vendor_id, count(*) … WHERE vendor_id IN (…) GROUP BY vendor_id` |
| `GET /api/vendors/change-requests` | vendor name per staged request | one batched `IN` via `_vendor_names` |
| `GET /api/vendors/screening/review-queue` | newest `sanctions_checks` row per vendor | one `DISTINCT ON (vendor_id) … ORDER BY vendor_id, checked_at DESC, id DESC` |

All three were fanned out per row. Each individual query was index-only and
cheap **on the database**, which is why none of them ever appeared in a
slow-query log — the cost is one network round trip per row, so the shape only
hurts at page size and only in latency. Measured against a loopback Postgres
(sub-0.1 ms RTT, 120 vendors / 120 change requests / 120 flagged vendors with 3
screening rows each), a `page_size=100` read fell from **105 statements to 6**
on every one of the three, and 73.6 / 69.6 / 86.5 ms to 44.8 / 41.6 / 42.2 ms.
Against a 1 ms-RTT database the same 99 removed round trips are worth roughly
another 99 ms.

Three details are load-bearing and should survive any rewrite:

* The invoice tally is scoped by `vendor_id` **alone**. The per-row query it
  replaced applied no entity filter; adding one here would silently change the
  count every multi-entity tenant already sees.
* A vendor with no invoices produces no group row, so the mapping is read with
  `.get(id, 0)` — otherwise that vendor drops off the page entirely.
* `DISTINCT ON` needs its `ORDER BY` to lead with the same column. The trailing
  `id DESC` is a tie-break, not a change of meaning: a bulk re-screen stamps a
  whole batch with one `checked_at`, and the per-vendor `LIMIT 1` it replaces
  was free to return a different row of that batch on each read.

`tests/test_vendor_list_query_counts.py` is the guard. Its assertion is
shape-agnostic — *the statement count must not grow with the page size* — so it
still holds through a future rewrite of any individual query, and each endpoint
also pins the specific statement that regressed.

## User Interface

### Vendors Page (`/vendors`)

**Top bar:** Search + "Sync from ERP" button

**Filter chips:** All / Unverified (with red count badge) / Active / Rejected

**Table columns:**
| Column | Description |
|---|---|
| Vendor | Name |
| Code | Short code |
| Email | Contact |
| Status | Badge: Active (green), Unverified (yellow), Rejected (red) |
| Source | Badge: Manual, ERP Sync, AI Extracted |
| Invoices | Count of linked invoices |
| ERP | Link icon if `erp_vendor_id` is set |
| Actions | Verify / Reject buttons (for unverified vendors) |

Unverified rows highlighted yellow. Rejected rows dimmed.

## Role Access

| Action | Admin | AP Manager | AP Clerk | CFO |
|---|---|---|---|---|
| View vendors | Yes | Yes | No | Yes |
| Create vendor | Yes | Yes | No | No |
| Edit vendor | Yes | Yes | No | No |
| Verify vendor | Yes | Yes | No | No |
| Reject vendor | Yes | Yes | No | No |
| Sync from ERP | Yes | Yes | No | No |
| Delete vendor | Yes | Yes | No | No |
| Block / unblock payments | Yes | Yes | No | No |
| Approve a bank-detail change | Yes | Yes | No | No |

Create / edit / verify / reject / sync / import / delete are all
`require_permission(vendor.manage)`; block / unblock is `vendor.block`; a
bank-change approval is `vendor.bank_change.approve` and refuses the proposer.
`admin` and `ap_manager` hold all three by default, CFO and clerk none
(`backend/app/api/permissions.py::ROLE_DEFAULT_PERMISSIONS`); a custom role can
be granted any of them.

## Integration with Invoice Processing

1. **Upload** — invoice received, no vendor linked yet
2. **Extraction** — AI extracts vendor name, address, tax ID
3. **Vendor Matching** — system fuzzy-matches against existing vendors
   - Match found → invoice linked to vendor
   - No match → new unverified vendor created, invoice linked
4. **Review** — reviewer sees the vendor (with unverified badge if new)
5. **Payment** — only invoices linked to `active` vendors can be paid
   - Unverified vendors block the invoice from entering the payment queue

> **Note (2026-10-06):** the code does not currently enforce this — see docs/followups.md § Surfaced by writing the help centre.

This ensures no payment goes to a vendor that hasn't been verified, while still allowing the invoice processing pipeline to continue (extraction, review, approval) before verification.

## Implementation Status

| Feature | Status |
|---|---|
| Vendor model with status/source/ERP fields | Done |
| Vendor CRUD API with verify/reject endpoints | Done |
| Vendor matching service (fuzzy + tax ID + exact) | Done |
| AI auto-creation on extraction | Done |
| Vendor matching wired into extraction pipeline | Done |
| ERP vendor sync service | Done |
| Vendor sync endpoint (mock data) | Done |
| Vendors page with status filters and verification UI | Done |
| Seed script: new columns for existing DBs | Done |
| Real ERP vendor list fetch (via adapter) | Planned |
| Two-way sync (push new vendors to ERP) | Planned |
| Vendor deduplication UI | Planned |
| Vendor bank detail change approval workflow (dual control — see [It travels the dual-control BEC gate](#it-travels-the-dual-control-bec-gate-like-every-other-banking-field)) | Done |
