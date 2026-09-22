# Supplier Portal

Self-service surface for vendors to submit invoices and track payment status.
Runs on the tenant's subdomain (`acme.localhost:7777/portal`) but uses a
separate authentication tree so vendor credentials can't cross into the AP
application.

## Why it's separate from employee auth

- `User` lives in the control plane and carries RBAC roles (`admin`,
  `ap_manager`, `ap_clerk`, `cfo`). A vendor login must never acquire one of
  these roles.
- `VendorUser` lives in the tenant DB alongside `Vendor`. Each row is a
  supplier-portal credential belonging to exactly one `vendor_id`.
- JWTs carry a `typ` claim (`user` vs `vendor`). `get_current_user` rejects
  `typ=vendor`; `get_current_vendor_user` rejects anything else. A bug in
  one dependency tree cannot leak into the other.

## Data model

`vendor_users` (tenant DB, migration 0009):

| Column                 | Type          | Notes                                                  |
|------------------------|---------------|--------------------------------------------------------|
| `id`                   | UUID          | PK                                                     |
| `vendor_id`            | UUID          | FK → `vendors.id` ON DELETE CASCADE, indexed           |
| `email`                | VARCHAR(320)  | UNIQUE — login identifier                              |
| `full_name`            | VARCHAR(255)  |                                                        |
| `hashed_password`      | VARCHAR(255)  | `bcrypt_sha256`, via the shared `pwd_context`           |
| `is_active`            | BOOLEAN       | Soft-disable without deleting                          |
| `must_change_password` | BOOLEAN       | Set on invite; cleared on first successful change      |
| `last_login_at`        | TIMESTAMPTZ   | Updated on each successful login                       |
| `mfa_secret`           | VARCHAR(64)   | base32 TOTP seed (migration 0053); held pending until verified |
| `mfa_enabled`          | BOOLEAN       | True once the vendor verifies a code; gates the login challenge |
| `mfa_enrolled_at`      | TIMESTAMPTZ   | Stamped on successful enrollment                       |
| `created_at` / `updated_at` | TIMESTAMPTZ | Standard TimestampMixin                              |

The three `mfa_*` columns (migration `0053_vendor_mfa`) mirror the `User` MFA
columns exactly. See **MFA (two-factor)** below.

## Endpoints

All under `/api/portal/*`. Tenant resolved via the usual `X-Tenant-Slug`
header. RBAC coverage gate in `tests/test_rbac.py` asserts every endpoint
uses `get_current_vendor_user` (except `/portal/auth/login`,
`/portal/auth/logout`, and `/portal/auth/mfa/challenge`, which are listed in
`ALTERNATE_AUTH`).

### Auth (`portal_auth.py`)

| Method | Path                             | Notes                                                                         |
|--------|----------------------------------|-------------------------------------------------------------------------------|
| POST   | `/portal/auth/login`             | email + password → `{access_token, …}` OR (MFA-enrolled) an MFA challenge     |
| POST   | `/portal/auth/logout`            | Adds `jti` to the shared Redis blocklist                                      |
| GET    | `/portal/auth/me`                | Returns the vendor-user + vendor summary (incl. `mfa_enabled`)               |
| POST   | `/portal/auth/change-password`   | Used by the forced first-login rotation and voluntary rotations               |
| POST   | `/portal/auth/mfa/challenge`     | **Public** — trade the login-issued challenge token + a code (`method` totp\|email) for an access token |
| POST   | `/portal/auth/mfa/challenge/email` | **Public** — email the on-demand OTP backup code to the enrolled vendor; 204-silent (no enumeration) |
| POST   | `/portal/auth/mfa/enroll`        | Mint a CANDIDATE TOTP secret + QR (parked in Redis until verified). Optional `{password?, code?}` step-up — required once a factor is already live |
| POST   | `/portal/auth/mfa/verify`        | Verify a code to promote the candidate + activate MFA (`mfa_enabled=true`)     |
| POST   | `/portal/auth/mfa/disable`       | Turn MFA off — re-verifies a current code first                              |

### MFA (two-factor) — `portal_auth.py`

TOTP MFA for supplier-portal vendor users (roadmap Priority 6), mirroring the
employee flow (`docs/authentication.md` § MFA) but scoped to `VendorUser` + the
`typ=vendor` JWT. Reuses the shared TOTP primitives in `services/mfa.py` —
secret generation, provisioning URI, QR, and `verify_totp` (±1 step skew).

- **Master switch.** `FEOH_MFA_ENABLED` (default `false` for local dev) gates the
  whole feature, exactly like employee MFA. With it off, an enrolled vendor
  still logs in with just a password (no challenge). MFA is **opt-in per vendor
  user**; there is no org-wide enforcement for vendors yet.
- **Enrollment.** `POST /mfa/enroll` mints a *candidate* secret + QR data URL and
  returns the secret in plaintext (manual entry). The candidate is parked in
  Redis (`mfa:vendor_pending_enroll:<vendor_user_id>`,
  `FEOH_MFA_ENROLL_PENDING_TTL_SECONDS`) — **nothing is written to
  `vendor_users`** until `POST /mfa/verify` confirms a valid code, which is the
  only place `mfa_secret`/`mfa_enabled`/`mfa_enrolled_at` are set. That way an
  abandoned enrollment can never leave the supplier without the factor they
  already had. The secret is never echoed back after activation.
  `POST /mfa/disable` re-verifies a current code before clearing the columns.
- **Re-enrollment is a step-up.** Once a factor is live, `POST /mfa/enroll`
  requires an optional-body credential — `{password}` (the portal password, via
  the shared `pwd_context`) or `{code}` (a code from the CURRENTLY enrolled
  authenticator) — mirroring `api/auth._require_mfa_step_up` on the employee
  surface via the shared `services/mfa.step_up_verified`. Without it a stolen
  vendor session could silently strip or swap the supplier's second factor. A
  **first** enrollment needs no step-up, so onboarding stays frictionless.
  Missing / wrong credential ⇒ 400 with a generic, account-agnostic message.
  The check is throttled 5/min keyed on the vendor USER (not the client IP —
  an attacker holding a stolen portal token can rotate IPs freely) and a
  failure writes a PII-free `portal.mfa.step_up.failure` audit row carrying
  only the operation name. `POST /mfa/disable` rides the same throttle +
  audit. Without both, the credential check is a silent, unlimited oracle.
- **Login challenge.** When `FEOH_MFA_ENABLED` is on and the vendor is enrolled,
  `POST /login` returns `PortalMFAChallengeResponse` (`{mfa_required, mfa_challenge_token,
  methods: ["totp", "email"]}`) instead of the access token. The browser submits the code
  to `POST /mfa/challenge` with `method` (`totp` default | `email`), which verifies and
  mints the real vendor access token.
- **Email-OTP backup factor.** Mirrors the employee email-OTP backup
  (`docs/authentication.md` § MFA → Email OTP) for the vendor when they've lost
  their authenticator. `POST /mfa/challenge/email` (public, gated by the same
  `vendor_mfa_challenge` token) issues a 6-digit code via
  `services/mfa.issue_vendor_email_otp` and emails it through the configured
  outbound email adapter (`console` in local dev — local-first, no cloud). The
  code's SHA-256 lives in Redis under a **distinct** keyspace
  (`mfa:vendor_email_otp:<vendor_user_id>`, separate from the employee
  `mfa:email_otp:` prefix) with the `FEOH_MFA_EMAIL_OTP_TTL_SECONDS` TTL,
  single-use. The vendor then submits it to `POST /mfa/challenge` with
  `method="email"`. The backup is gated on the vendor having actually enrolled
  TOTP (`mfa_enabled` + `mfa_secret`) — it's a fallback to the authenticator, not
  an independent enrollment path. The request endpoint is 204-silent for an
  unenrolled / unknown / inactive account (no enumeration); the OTP and the
  vendor email never appear in logs. No migration — Redis-only, exactly like the
  employee backup.
- **Token-type isolation (cross-auth-leak guard).** The challenge token carries
  `typ=vendor_mfa_challenge` — distinct from both the employee challenge
  (`mfa_challenge`) and the vendor access token (`vendor`). So a challenge token
  can never resolve as an access token through `get_current_vendor_user`, an
  employee challenge can never hit `/portal/auth/mfa/challenge[/email]`, and a
  vendor access token can never satisfy the challenge endpoint.
  `services/mfa.create_vendor_challenge_token` / `decode_vendor_challenge_token`
  enforce the `typ` symmetrically. The email-OTP keyspace is likewise isolated
  from the employee one, so the same UUID value can't collide across surfaces.

### Invoices + payments (`portal.py`)

| Method | Path                             | Notes                                                                         |
|--------|----------------------------------|-------------------------------------------------------------------------------|
| GET    | `/portal/invoices`               | Vendor-scoped list. Optional filters: repeatable `status=` (raw `InvoiceStatus` values — the frontend's vendor-facing phase chips, `PORTAL_INVOICE_PHASES`, expand to the set behind each phase; an unrecognised value is dropped, never 422'd, so a stale portal build can't break the list) and `search=` (case-insensitive substring on the invoice number, LIKE metacharacters escaped). Both compose ON TOP of the `vendor_id ==` scope — a filter can only narrow. Each item carries `rejection_reason` (the latest `review_rejected` exception's description) **only while `status == "rejected"`** — so a supplier knows what to fix before resubmitting; never the rejecting employee's name. Optional `date_from`/`date_to` (`YYYY-MM-DD`, inclusive; `date_to` runs through end-of-day; inverted range → empty not 422) filter on `Invoice.created_at`. And each processing-phase item carries `waiting_on` (a PII-free bucket: `review` / `processing` / `erp`) + `waiting_on_days` — the vendor-facing "what is this waiting on" signal, NULL for `new`/`approved`/`paid`/`rejected`/`done`. |
| GET    | `/portal/invoices/{id}`          | 404 for "doesn't exist" AND "belongs to another vendor" (no ID enumeration). Same `rejection_reason` field as the list. |
| POST   | `/portal/invoices`               | Multipart PDF upload — routes into the same extraction pipeline as AP uploads |
| POST   | `/portal/invoices/{id}/resubmit` | **Revise & resubmit** a `rejected` invoice (multipart; 409 for any other status). Swaps the file on the SAME row — so it never trips the duplicate check against its own prior version and stays this vendor's — resolves the open `review_rejected` exception, and **re-extracts the corrected document** so the reviewer isn't reconciling a new PDF against the fields from the version they rejected. See *Resubmission re-extracts, under two constraints* below. |
| GET    | `/portal/payments`               | Payments joined to Invoice to filter on `vendor_id`. Same optional filters as the invoice list: repeatable `status=` (allow-listed against `_PORTAL_PAYMENT_STATUSES` — an unknown value is dropped, not 422'd) + `search=` on the paid invoice's number + `date_from`/`date_to` on `Payment.created_at`. Both compose on top of the `vendor_id ==` scope |
| GET    | `/portal/payments/{id}/remittance` | Vendor-scoped remittance PDF; ownership via `Payment→Invoice.vendor_id`; 404 on a foreign payment |
| GET    | `/portal/summary`                | **Portal home.** Whole-set, vendor-scoped counts + rollups: `invoices_total`, `invoices_action_required` (`rejected` — the only bucket the SUPPLIER can move), `invoices_in_progress` (with the customer), `invoices_paid`, `invoices_completed` (`done`, which is reachable from `approved` without a payment so it never inflates "paid"), `outstanding_by_currency` (in-progress value, per currency, exact decimal strings), `open_discount_offers` and the staged `pending_change`. Reuses the list's own `_portal_invoice_filters` + `_vendor_offer_filter` + `_pending_change`, so a figure can never describe a different set from the page it links to. PII-free: counts, currency codes and money only — never an AP actor's name, never a raw `InvoiceStatus` |

#### Resubmission re-extracts, under two constraints

A resubmit swaps the source document on an invoice that has already been
triaged once. Re-reading it is the point — otherwise the reviewer compares a
corrected PDF against the amount, number and dates extracted from the version
they rejected. Two things must not happen on that pass, and both are carried by
`dispatch_extraction` → `run_extraction` as keyword flags that default to
today's ingest behaviour:

- `skip_vendor_match=True` — `vendor_matching.match_and_link_vendor` is a fuzzy
  matcher. Re-run over a re-typed vendor name it can land `Invoice.vendor_id` on
  a **different** supplier, which (a) drops the invoice out of this
  `vendor_id ==`-scoped portal, so the vendor loses sight of their own
  resubmission, and (b) re-points the payee whose `Vendor.bank_details` the
  payment run reads. The existing link **and** `vendor_name` are pinned —
  `vendor_name` too, because that is what `PATCH /api/invoices/{id}`
  re-resolves a stale link from, so leaving it rewritten would only move the
  re-link to the reviewer's next save.
- `suppress_auto_approve=True` — a human REJECTED this document. Letting the
  unattended confidence / small-amount gates approve its replacement would let a
  supplier launder a rejected invoice past the reviewer who rejected it.

`rejected → pending` is not a legal transition, so the route takes the
documented rework loop `rejected → new → pending` (two audit rows:
`invoice.resubmitted_by_vendor`, then `invoice.extraction_dispatched`) and
`run_extraction` performs the final `pending → ready_for_review` plus its usual
`refresh_warnings` / PO-match / exception pass. Extraction is dispatched **after
the commit**, since the worker opens its own session. When the invoice's frozen
workflow snapshot has the extraction step disabled, the route goes straight to
`ready_for_review` exactly as before.

An extraction that fails leaves the invoice at `failed` with an
`extraction_failed` exception in the AP queue — the same exposure any upload
has; the vendor's own resubmit window (`rejected` only) has closed, so recovery
is AP's retry, not another vendor upload.

### Purchase orders + PO flip (`portal.py`)

| Method | Path                                  | Notes                                                                        |
|--------|---------------------------------------|------------------------------------------------------------------------------|
| GET    | `/portal/purchase-orders`             | Vendor-scoped PO list (with line-item counts)                                |
| GET    | `/portal/purchase-orders/{id}`        | PO detail + line items; 404 for a foreign PO                                 |
| POST   | `/portal/purchase-orders/{id}/flip`   | **PO flip** — create an invoice pre-populated from a vendor-owned PO         |

Both reads serve each PO's own `currency` — `null` when the PO records none,
and the portal renders that total bare. The schema used to default `currency`
to `"USD"` and the handler never set it, so every PO a supplier saw read as
dollars. The flip books the invoice in the PO's currency; a PO with none falls
back to the column's historical `"USD"`, because `invoices.currency` is NOT NULL
and cannot say "unknown" — PO matching reads the PO's NULL, not that
placeholder, so the pair is reported as unverified rather than proven
(`docs/decisions.md` §197).

### Company self-service (`portal.py`)

| Method | Path                                  | Notes                                                                        |
|--------|---------------------------------------|------------------------------------------------------------------------------|
| GET    | `/portal/company`                     | Current company info; bank/tax **masked**; surfaces any pending change       |
| PATCH  | `/portal/company`                     | Update phone/address/email — **applies live**                               |
| POST   | `/portal/company/bank-change`         | **Stages** a `bank_details` change (NOT applied); 202; deduped on pending    |
| POST   | `/portal/company/tax-id-change`       | **Stages** a `tax_id` change (NOT applied); 202                             |
| GET    | `/portal/company/change-requests`     | This vendor's own change requests + statuses                                |
| GET    | `/portal/company/tax-form`            | Whether a W-9/W-8 is on file (PII-free: on-file flag + form type + received date) |
| POST   | `/portal/company/tax-form`            | Multipart upload of the vendor's own signed W-9 (US) / W-8 (foreign) — **applies live** |
| GET    | `/portal/company/tax-form/file`       | Vendor-scoped download proxy of their own uploaded form; 404 on foreign/missing |

#### W-9 / W-8 tax-form upload (self-service)

US suppliers file a **W-9**, foreign suppliers a **W-8** (BEN / BEN-E). The
vendor uploads their own signed form from the portal; AP uses it for 1099 /
withholding compliance. Design notes:

- **Reuses the existing vendor columns.** The form writes
  `Vendor.w9_file_key` + `Vendor.w9_received_date` on the caller's **own**
  vendor row — the same columns the AP-side `POST /api/tax/vendors/{id}/w9`
  upload writes. **No new vendor column, no migration.**
- **Form type without a column.** W-9 vs W-8 is encoded in the S3 key segment
  (`<org>/tax-forms/<vendor>/<form_type>/<file>`, via
  `storage.upload_tax_form_file`) and recovered on read by
  `portal._tax_form_type_from_key`. A W-9 stored via the AP-side path (key
  prefix `<org>/w9/<vendor>/…`, no form-type segment) reads back as `w9`.
- **Applies live, unlike bank/tax-ID changes.** A tax form is a document, not
  a money-routing target, so there's no AP-approval gate. (AP still verifies
  the TIN separately via `POST /api/tax/vendors/{id}/tin-verify`.)
- **Vendor-scoped + cross-tenant gated.** Every handler resolves the vendor by
  `vu.vendor_id` only (cross-vendor → 404, not 403). The download proxy reads
  the key from the vendor row (never the request) **and** cross-checks the
  key's leading org segment against the vendor's org — wrong-org / missing both
  404, no enumeration. Content-type is gated by `ALLOWED_CONTENT_TYPES` (PDF /
  PNG / JPEG / TIFF / XML) at the storage boundary.
- **PII-out-of-logs.** The `vendor.tax_form_uploaded_by_vendor` audit row
  carries only the form type + filename + `vendor_user_id` — never the tax ID.
  The `GET` response carries an on-file boolean + form type + received date,
  never the tax ID or the document bytes.

### Early-payment discount offers (`portal.py`)

Vendor-facing view of the dynamic-discounting offers the AP team extends to the
supplier (see `dynamic-discounting.md`). Every offer the vendor sees is scoped
to their own `vendor_id` **or** to one of their own invoices — a vendor can
never see another vendor's offers (cross-vendor / unknown id → 404, never 403).

| Method | Path                                       | Notes                                                                                  |
|--------|--------------------------------------------|----------------------------------------------------------------------------------------|
| GET    | `/portal/discount-offers`                  | Vendor-scoped offer list; per-tier savings + best capturable tier today; `?status=` filter. Both the reported `status` and the filter use the **effective** status (below) |
| POST   | `/portal/discount-offers/{id}/accept`      | Accept the discount (`tier_days` or best tier today). **Never moves money** — flips `offered → accepted` only (reuses `discount_offers.accept_offer`); CFO-gated payment run still funds it. Idempotent: re-accepting a non-`offered` offer is a `409`; foreign/unknown id `404`. A lapsed offer is refused (`409`, or `422` when a rung is named) — no tier is capturable past `valid_until` |
| POST   | `/portal/discount-offers/{id}/decline`     | Decline the discount (reuses `discount_offers.decline_offer`); `409` if no longer `offered`, **including a lapsed offer** |

#### The supplier and AP see the same status

An offer's `expired` state is **derived** from `valid_until`, not read from the
column — the column's only writer is the auto-capture sweep, which is off by
default (see `dynamic-discounting.md` § Expiry is derived, not swept). The
portal used to render the raw column while `/api/discounts` derived it, so the
same offer read `expired` to the AP team and `offered` to the supplier, who
still saw a live Decline button on it.

The portal now goes through the same one rule: `effective_status` on the
response, `effective_status_sql` on the `?status=` filter and on the
`open_discount_offers` KPI in `/portal/summary`. Nothing is written on a read —
the stored column is left for the sweep to materialize.

Neither side can act on a dead offer either. Accept already refused one (no tier
is capturable past `valid_until`); decline did not, so a lapsed offer could be
flipped to `declined` — asserting a refusal that never happened, on an
append-only audit row. `decline_offer` now takes a required `as_of` and refuses,
for the AP router and the portal alike.

Both money math (savings) and the lifecycle mutators are the **same pure
`services/discount_offers.py` primitives** the AP `/api/discounts` router uses —
the vendor side never duplicates the Decimal math. Audit rows
(`discount_offer.accepted_by_vendor` / `.declined_by_vendor`) are PII-free
(`actor_id=None`, no values, only the chosen tier). No migration — the
`DiscountOffer` table already exists (migration 0043).

### Notification preferences (`portal.py`)

| Method | Path                                  | Notes                                                                        |
|--------|---------------------------------------|------------------------------------------------------------------------------|
| GET    | `/portal/notification-preferences`    | Calling vendor user's effective email prefs (`email_on_payment`, `email_on_rejection`); defaults on |
| PATCH  | `/portal/notification-preferences`    | Partial update of the **caller's own** `VendorUser` prefs; audited; unspecified fields unchanged |

A vendor portal user controls, per user, whether they get **emailed** when one
of *their own* invoices is **paid** or **rejected**. Stored on
`vendor_users.notification_prefs` (JSONB, migration 0052), keyed by the same
`invoice_paid` / `invoice_rejected` event strings the rest of the system uses
(`{event_type: {"email": bool}}`), mapped to the vendor-friendly
`email_on_payment` / `email_on_rejection` shape by
`services/vendor_notifications.py`. Opt-out, not opt-in: an unset event defaults
to **on**. Vendors have no in-app notification center, so only the `email`
channel exists. The PATCH is scoped to the caller's own row
(`get_current_vendor_user`) — a vendor user can never touch another's prefs —
and writes a PII-free `vendor_user.notification_prefs_updated` audit row (field
names only). Actual email dispatch is wired into the `transition_invoice`
chokepoint; see [notifications.md](notifications.md) § Vendor recipients.

### Single-use virtual-card reveal (`portal.py`)

| Method | Path                    | Notes                                                            |
|--------|-------------------------|------------------------------------------------------------------|
| GET    | `/portal/cards/{token}` | **Public-by-design** — the emailed token IS the credential. Returns the live PAN/CVV/expiry exactly once |

When a virtual card is issued, `services/card_reveal.mint_reveal_token` persists
a `card_reveal_tokens` row holding only the **sha256** of a 32-byte URL-safe
token; the plaintext goes into the vendor's email link and is never stored. The
row expires after 7 days regardless of use.

**Claim semantics — atomic, then committed before the provider call.**
`consume_reveal_token` claims the token with a single statement:

```sql
UPDATE card_reveal_tokens SET used_at = now()
 WHERE token_hash = :h AND used_at IS NULL AND expires_at > now()
   AND organization_id = :org
   AND EXISTS (SELECT 1 FROM virtual_cards
                WHERE id = card_reveal_tokens.card_id AND organization_id = :org)
RETURNING card_id
```

Postgres evaluates that predicate under the row lock, so of N simultaneous
requests carrying the same token **exactly one** updates a row and receives the
card; the rest match zero rows and are classified (`used` / `expired` /
`invalid`) by a follow-up read. The previous read-then-write pair (plain
`SELECT`, `used_at is None` checked in Python) let every concurrent request
through — all of them read before any of them wrote, and all of them got the
live PAN/CVV.

`api/portal.py::reveal_card` then **commits the claim, and its audit row, before
calling the card provider** — the row lock is never held across network I/O, and
once committed nothing downstream can revive the link: not a provider outage,
not a commit that fails after the PAN is already on the wire, not a crash.

This is deliberately **fail-closed**: a degraded reveal (org disabled cards after
issuance, or a provider outage) still spends the link, and the response is the
PII-free fallback body (`last_four` + limit, `pan`/`cvv` = `null`, a `warning`)
with no retry. A link that survives a failed reveal is observationally
indistinguishable from a link that can be revealed twice, and this is live card
data — so the vendor asks AP to re-issue instead.

Tenant binding is belt-and-braces: the tenant is resolved through the usual
`get_tenant` chokepoint, and both the token row *and* the card it points at must
carry that tenant's org id. A mismatch is refused as the same opaque `invalid`
an unknown token gets — and because the card check lives inside the claim's own
`WHERE`, a rejected reveal never stamps `used_at` (nothing burns).

Errors: `404 invalid` · `410 expired` · `410 used`. Every consumption writes a
PII-free `card.revealed_via_token` audit row (`last_four` only — never the PAN
or CVV). Tests: `tests/test_card_reveal.py` (service + real-Postgres
concurrency) and `tests/test_card_reveal_endpoint.py` (handler ordering).

### Admin invite + change-request approval (`vendors.py`)

| Method | Path                                                 | Notes                                 |
|--------|------------------------------------------------------|---------------------------------------|
| GET    | `/vendors/{id}/portal-users`                         | List portal users for a vendor        |
| POST   | `/vendors/{id}/portal-users`                         | Invite — the temp password is **emailed only**, never returned. Stamps `provisioned_by_user_id` |
| POST   | `/vendors/{id}/portal-users/{vendor_user_id}/reset-password` | Mint + email a replacement password on the SAME row. Re-stamps `provisioned_by_user_id` |
| DELETE | `/vendors/{id}/portal-users/{vendor_user_id}`        | Remove a portal user                  |
| GET    | `/vendors/change-requests/counts`                    | Whole-set tallies for the queue — `{total, pending, by_status}` (admin, ap_manager — **exactly** the queue list's gate, decisions §48: it previously admitted `cfo`, who cannot read the queue, so the size of the staged-bank-change review set was visible to a role excluded from it); counts only, PII-free. A nav badge driven off a page of results undercounts once the queue paginates — the same reason `/vendors/counts` exists |
| GET    | `/vendors/change-requests`                           | Pending change-request queue (admin, ap_manager); value masked |
| GET    | `/vendors/{id}/change-requests`                      | One vendor's requests (admin, ap_manager, cfo); value revealed |
| POST   | `/vendors/change-requests/{id}/approve`              | Apply the staged change to the vendor (admin, ap_manager); `FOR UPDATE` locked, exactly-once |
| POST   | `/vendors/change-requests/{id}/reject`               | Mark rejected; never touches the vendor (admin, ap_manager) |

## Self-service change-request gate (fraud prevention)

Bank-detail and tax-ID changes from the portal do **not** apply live — they
stage a `vendor_change_requests` row (`status=pending`) and leave the `Vendor`
row untouched. AP approval is what applies the change:

- **bank_details** → on approve, merged into `Vendor.bank_details` via
  `_merge_bank_details` (the same merge the AP PATCH uses).
- **tax_id** → on approve, sets `Vendor.tax_id` and clears `tin_verified_at`
  (a re-keyed TIN must be re-verified).

The approve handler locks the request row `FOR UPDATE` and 409s if it's already
resolved, so the apply is exactly-once. Every stage / approve / reject writes an
append-only audit row; the audit `details` (and the staging table) carry only
`{change_type, request_id, last4}` — never the full account number or tax ID.
A vendor can hold at most one pending request per `change_type` (dedupe → 409).

This is the fraud control: a redirected bank account has **zero effect on where
money goes** until an AP admin explicitly approves it.

## Credential provenance and the BEC dual control

"Dual control" on a bank change means two humans. Until this landed, one
`ap_manager` could be both of them.

The chain, every link verified:

1. `POST /vendors/{id}/portal-users` is `require_roles(ADMIN, AP_MANAGER)` and
   took the address from the caller.
2. Signing in as that identity and staging a bank change fills
   `requested_by_vendor_user_id` and leaves `requested_by_user_id` **NULL**.
3. `approve_change_request`'s segregation check compared only
   `requested_by_user_id`, so a NULL passed it unconditionally. The code comment
   stated the assumption — *"portal-submitted requests have no AP requester, so
   this only bites AP-initiated ones"* — which holds only if AP cannot mint
   portal identities. It can.
4. `ROLE_AP_MANAGER`'s default permissions hold `vendor.manage`,
   `vendor.bank_change.approve` **and** `payment.execute`.

Two nullable columns close it (migration `0095`, tenant-scoped):

| Column | Meaning |
|---|---|
| `vendor_users.provisioned_by_user_id` | The control-plane `User` who last minted a password for this portal identity — stamped by invite AND by `reset-password`, the only two routes that hand an AP actor a working supplier credential. |
| `vendor_change_requests.requester_provisioned_by_user_id` | That value, **frozen onto the request at staging time**. |

`approve_change_request` refuses (403) when either requester column equals the
approver.

Three properties are deliberate and easy to get wrong:

- **NULL is permissive, and that is correct.** A legacy credential, or one a
  supplier has always managed themselves, has no provisioner — and an AP actor
  cannot sign in as it without going through invite or reset, both of which
  stamp the column. Fail-closing on NULL would refuse every genuine
  supplier-initiated bank change forever and buy nothing.
- **The value is copied, not joined.** `DELETE /portal-users/{id}` carries no FK
  into `vendor_change_requests`, so resolving the provisioner at approval time
  would let an approver delete the portal identity between staging and approving
  and walk their own request through. A frozen column cannot be un-stamped by a
  later delete.
- **The supplier changing their own password does NOT clear the stamp.**
  `POST /portal/auth/change-password` requires the *current* password, which the
  provisioner has — so clearing there would be a one-request bypass of the whole
  control. The stamp is durable provenance, not a "who knows the password right
  now" flag. The operational cost is real and accepted: an AP user who onboarded
  a supplier is never an independent second party to what that identity submits,
  so someone else approves. Admin holds `vendor.bank_change.approve` by default,
  so no tenant can be left with nobody able to approve.

Regression coverage is two files, because the fix has two failure modes:

- `tests/test_vendor_bank_change_provisioner_sod.py` drives the whole chain
  (provision → sign in with the emailed credential → stage → refused), the
  negative case (a different approver succeeds), the untouched case (a
  self-managed supplier still approves normally), the delete-the-evidence
  attempt, and the reset-password takeover of a legacy identity.
- `tests/test_vendor_credential_provenance.py` guards the **exhaustiveness
  claim** the NULL-permissive reading rests on. Invite and reset being the only
  two ways an AP actor learns a supplier's password is a property of the whole
  `app/` tree, so a third route — "resend credentials", "impersonate supplier" —
  would reopen the hole silently with every other test green. It AST-scans for a
  `VendorUser(...)` construction or a `.hashed_password =` write that neither
  stamps the column nor carries a reasoned exemption.

## Security invariants

- **Vendor scoping:** every portal handler filters on
  `Invoice.vendor_id == vu.vendor_id`. `test_supplier_portal.py` asserts
  this at the source level so a regression can't silently broaden a query.
- **404, not 403, on cross-vendor probe:** distinguishes nothing from a
  missing invoice — so the portal can't be used to enumerate invoice IDs
  across tenants' vendors.
- **Token blocklist:** shared with employee auth (Redis `jti`), so a
  compromised portal token can be revoked through the same mechanism.
- **`typ` claim enforced symmetrically:** employee JWT → 401 on portal,
  vendor JWT → 401 on AP — verified in `test_supplier_portal.py`.
- **Brute-force is throttled on two axes:** the per-IP request cap
  (10/min) *and* a per-account failure budget — 10 failures / 15 min on
  `/portal/auth/login`, 5 on `/portal/auth/mfa/challenge`. The per-IP cap
  alone is blind to a spray distributed across rotating addresses, which
  matters most on the second factor (six digits, and an attacker who gets
  there already has the password). The bucket key is **tenant-scoped**
  (`slug + identity`): a vendor address is unique only within a tenant DB,
  so keying on the address alone would let one tenant's traffic throttle
  another tenant's supplier. Full contract + the accepted DoS trade-off:
  [`docs/authentication.md`](../../docs/authentication.md) §
  Brute-force protection.
- **An accepted sign-in is on the record too, once MFA is on:** with a second
  factor enrolled `/login` returns a challenge instead of a token, so the
  sign-in completes at `/mfa/challenge` — which writes
  `portal.mfa.verify.success` / `.failure` (`{method, ip}`, PII-free). Without
  those, enabling MFA would have removed the account's sign-in from the trail.
  Issuing an email backup code (`/mfa/challenge/email`) has no row of its own by
  design — see `docs/authentication.md` § The supplier portal's second-factor
  stage is on the trail.
- **A rejected sign-in is on the record:** `portal.login.failure` lands in
  the tenant's `audit_log`, identifying the account by `entity_id` and
  carrying `{ip, reason}` only — a supplier contact's address is
  third-party PII and isn't restated on every guess. `reason` is
  `bad_password` / `no_password` / `inactive`, so hammering a *deactivated*
  supplier login is visible too. An address with no account writes nothing:
  there is no org to resolve a tenant trail from, and a row would itself be
  the enumeration signal the identical 401 exists to avoid.

## Invoice submission flow

1. Vendor uploads a PDF.
2. An `Invoice` row is created with `vendor_id` + `vendor_name` pre-filled
   from the JWT's `ven` claim (no free-form vendor input — the portal user
   only represents their own vendor).
3. File is uploaded to S3 under `{org_id}/{invoice_id}/{filename}`.
4. A `WorkflowInstance` is created using the active workflow definition.
5. If the extraction step is enabled, the invoice transitions to `pending`
   and extraction is dispatched via the normal `dispatch_extraction` path.
6. An audit log entry is written with `action="invoice.submitted_by_vendor"`
   and `details.source="supplier_portal"` so AP teams can see provenance.

The `actor_id` on the audit log is `NULL` for portal uploads — the actor is
a `VendorUser`, not a `User`, and the two namespaces are deliberately
separate. The `vendor_user_id` is carried in `details` instead.

## Frontend

Separate auth + HTTP surface:

- `$lib/portalApi.ts` — parallel to `$lib/api.ts`, uses `portal_auth_token`
  in localStorage so the AP app and portal can coexist in the same browser.
- `$lib/stores/portalAuth.svelte.ts` — parallel to `auth.svelte.ts`.
- `/portal/+layout.svelte` — portal shell (header, nav, logout). Root
  `+layout.svelte` bypasses all AP-auth logic when the path starts with
  `/portal`.

Routes:

| Route                         | Purpose                                                |
|-------------------------------|--------------------------------------------------------|
| `/portal`                     | **Home / overview** — whole-set KPI cards (needs your attention / with your customer / paid / completed), per-currency outstanding value, and the decisions waiting on the supplier elsewhere (open early-payment offers, a staged bank/tax change). Reads `GET /portal/summary`; `EmptyState` for a supplier with no invoices yet |
| `/portal/login`               | Sign-in form + MFA second-factor step (TOTP, with a "use email code instead" backup) |
| `/portal/change-password`     | Forced first-login rotation                            |
| `/portal/invoices`            | List (invoice-number search + vendor-facing phase chips) + upload + per-row "Revise & resubmit" on a rejected invoice |
| `/portal/purchase-orders`     | PO list + per-row "Create invoice" (flip)              |
| `/portal/payments`            | Payment history + per-row "Download remittance"        |
| `/portal/discount-offers`     | Early-payment discount offers — accept / decline       |
| `/portal/company`             | Contact (live) + bank/tax change requests (staged) + W-9/W-8 tax-form upload/download (live) + Security (MFA enroll/disable) |
| `/portal/notifications`       | Email preferences (paid / rejected) — vendor-controlled |

The portal company form makes the approval-gating visible: bank/tax changes
show a "pending AP approval" banner (read from `GET /portal/company`'s
`pending_change`) so the vendor isn't surprised the change didn't take effect.

## Phase 2 (shipped)

- [x] PO flip — create invoice pre-populated from a PO (idempotent per `(vendor, po)`)
- [x] Remittance download (reuses `services/remittance_pdf.py`)
- [x] Company info self-update (contact live; bank/tax staged)
- [x] Bank-detail changes with AP admin approval workflow (fraud mitigation)
- [x] W-9 / W-8 upload + storage (self-service; reuses `Vendor.w9_file_key` / `w9_received_date`, no migration — see *W-9 / W-8 tax-form upload* above)
- [x] Dynamic-discount offers — vendor accepts/declines early-payment discounts
  (ties into the dynamic-discounting engine; accept never moves money)
- [x] In-app per-invoice chat between vendor and AP team
- [x] Notification preferences (email-on-paid, email-on-rejected) — per-portal-user, vendor-controlled; wired into the `transition_invoice` dispatch chokepoint
- [x] Virtual card viewing (secure, single-use reveal token) — `GET /portal/cards/{token}` consumes a one-time `CardRevealToken` atomically (`UPDATE … WHERE used_at IS NULL … RETURNING`), committed before the provider call; see *Single-use virtual-card reveal* above
- [x] MFA (TOTP) for portal users (migration 0053; opt-in per vendor user, gated by `FEOH_MFA_ENABLED`)
- [x] MFA email-OTP backup factor for portal users (Redis-only, no migration; on-demand via `POST /portal/auth/mfa/challenge/email`, sent through the email adapter, gated by `FEOH_MFA_ENABLED`)
- [x] Portal home / overview (`GET /portal/summary` + `/portal`) — whole-set,
  vendor-scoped counts of what needs the supplier vs what is with the customer,
  per-currency outstanding value, and the open decisions (early-payment offers,
  a staged bank/tax change)
- [x] Resubmission re-extracts the corrected document, pinned to the existing
  vendor link and with unattended auto-approve suppressed (see *Resubmission
  re-extracts, under two constraints* above)

## Phase 3 (deferred)

Add these when there's demand from the first paying customer:

- (nothing currently parked here)

## Operational notes

- Portal users have no relationship to the control-plane `User` table. Adding
  one via the admin UI (or SCIM) does not create a portal login.
- Deleting a `Vendor` cascades to `vendor_users` (`ON DELETE CASCADE`). An
  orphaned portal user row is therefore impossible.
- **The temp password is emailed and nothing else.** It is not in the invite
  (or reset) response body, and delivery is not best-effort: a send failure
  rolls the whole request back (502) rather than leaving a portal account whose
  credential nobody received. Local dev is unaffected — the default `console`
  adapter renders the message, exactly as the tenant-signup welcome email
  already relies on. Recovery for a lost credential is `reset-password`, which
  mints and emails a new one on the same row.

  This changed for a security reason, not a tidiness one. AP chooses the
  address, so returning the plaintext made "mint a supplier login I control" a
  single frictionless request with no delivery record — the first link of the
  BEC chain in [Credential provenance](#credential-provenance-and-the-bec-dual-control)
  below. Removing it does not close that chain on its own (an AP actor who
  supplies their own address can still read the email); the approval refusal
  does. It removes the *silent* path and forces the credential through a channel
  that leaves a record.
