# API Reference

FastAPI backend running on http://localhost:8000. Interactive docs available at:

- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc

## Required Headers

### Authentication

All endpoints except the explicit allowlist below require a Bearer token:

```
Authorization: Bearer <jwt_token>
```

**No-JWT endpoints** (use other mechanisms or are public):
- `/api/auth/login`, `/api/auth/mfa/challenge/email`, `/api/auth/mfa/verify` — pre-login flow, gated by password + challenge token
- `/api/auth/sso/*` — OIDC handshake
- `/api/scim/v2/*` — uses per-tenant SCIM bearer (validated inside the handler)
- `/api/signup/*`, `/api/public-config`, `/api/health` — public
- `/api/cards/webhook/{provider}`, `/api/erp/webhook/{erp_type}` — provider-signed webhooks

### Tenant Routing

All business endpoints (invoices, vendors, dashboard, payments, etc.) require:

```
X-Tenant-Slug: acme
```

The frontend sends this automatically based on the subdomain. When testing via curl, include both headers.

### RBAC

Every protected endpoint declares the roles that can call it via `Depends(require_roles(...))`. The four roles are `admin`, `ap_manager`, `ap_clerk`, `cfo`. Failed checks return `403 Forbidden`. The full permission matrix lives in [`docs/authentication.md`](../../docs/authentication.md) § RBAC; the **Roles** column on each table below summarises it for that endpoint.

Notation: `*` = any authenticated role, `admin/manager` = admin or ap_manager, `admin/manager/cfo` = the three non-clerk roles.

## Money and quantity fields — digit bounds

Every `Decimal` field on a request body is bounded with Pydantic `max_digits` /
`decimal_places` matching the `Numeric(precision, scale)` of the column it lands
in. Most money is `Numeric(15, 2)`, i.e. **13 integer digits and 2 decimal
places**; quantities are `Numeric(12, 4)`, and a few totals are `Numeric(18, 2)`.

- A value wider than its column returns **422** with a `decimal_max_digits`
  error. It used to pass validation and raise `NumericValueOutOfRangeError` at
  the DB flush — a **500** for input the caller got wrong.
- A value with **more decimal places than the column's scale** also returns 422
  (`decimal_max_places`) rather than being silently rounded by Postgres. Send
  money already rounded to the currency's minor unit; quietly changing a
  submitted amount is a data-integrity problem, not a convenience.
- Bounds are **exactly** the column's — never tighter — so any value the
  database can store is accepted. Where a field additionally carries a semantic
  range (`tax_rate` is `ge=0, le=100`), that narrower rule also applies.

`backend/tests/test_schema_decimal_bounds.py` derives this from the live route
tree and fails when a new `Decimal` request field lands without a bound.

## Pagination

List endpoints share one contract, defined once in `app/api/pagination.py`
(the `pagination_params` dependency + `PageMeta` mixin / `paginated()` helper):

- Query params: **`page`** (1-based, default `1`) and **`page_size`** (default
  **20**, max **100** — values above 100 return `422`).
- Response envelope always carries **`items`**, **`total`** (full unpaged
  count), **`page`**, and **`page_size`**:

  ```json
  { "items": [ ... ], "total": 137, "page": 1, "page_size": 20 }
  ```

The frontend renders the first page then a "Load more" control that requests
`page=N+1` and appends. Paginated lists: `/invoices`, `/vendors`, `/payments`,
`/payments/runs/`, `/purchase-orders`, `/goods-receipts`, `/credit-memos`,
`/exceptions`, `/notifications`, `/cards`, `/cards/rebates`, `/inspections`,
`/workflows`, `/admin/users`, and the supplier-portal `/portal/invoices` +
`/portal/payments`.

`/cards/rebates` carries a money figure BESIDE the envelope: `total` is the row
count like everywhere else, and the summed rebate amount over the whole filtered
set is `total_amount`. It was the one list whose `total` meant the money, which
is exactly how a `total` stops meaning the same thing twice.

**Intentionally not paginated** (bounded reference collections returned in
full): `/gl-accounts` — it feeds the invoice GL dropdown, which needs every row.

## Auth

| Method  | Path                         | Roles | Description                                                        |
|---------|------------------------------|-------|--------------------------------------------------------------------|
| `POST`  | `/api/auth/login`            | (public) | Login with email/password. Returns either `TokenResponse` (`{access_token, must_change_password}`) or `MFAChallengeResponse` (`{mfa_required: true, mfa_challenge_token, methods, must_enroll}`) when MFA is in play. |
| `POST`  | `/api/auth/logout`           | * | Revoke current token via Redis blocklist                           |
| `GET`   | `/api/auth/me`               | * | Get current user (roles, `must_change_password`, `mfa_enabled`, `mfa_required_by_org`) |
| `PATCH` | `/api/auth/me`               | * | Update own name or password                                        |
| `POST`  | `/api/auth/change-password`  | * | Set a new password (clears `must_change_password`)                 |
| `GET`   | `/api/auth/sessions`         | * | The caller's own live sessions, newest first — `{id (jti), created_at, expires_at, ip, device, method, current}`. Expired-but-tracked entries are pruned, not listed. |
| `DELETE`| `/api/auth/sessions/{jti}`   | * | End one of the caller's own sessions (blocklists the JTI). Opaque 404 for a JTI that isn't the caller's — same answer as one already gone. Returns `{revoked: 1}`. |
| `POST`  | `/api/auth/sessions/revoke-others` | * | Sign out everywhere except the current session. Idempotent (`{revoked: 0}` when there's nothing else). |

Auth endpoints do **not** require the `X-Tenant-Slug` header.

The three session endpoints are scoped to the caller's own account — membership in their session set *is* the authorization check. No step-up gate: they only ever remove access. Both revokes write a PII-free `auth.session.revoked` audit row. See [`docs/authentication.md`](../../docs/authentication.md) § Session management.

## MFA

TOTP-based two-factor with email-OTP backup. Master switch `FEOH_MFA_ENABLED` (default `false` for local dev). See [`docs/authentication.md`](../../docs/authentication.md) § MFA for the full flow.

| Method | Path                              | Roles | Description |
|--------|-----------------------------------|-------|-------------|
| `POST` | `/api/auth/mfa/challenge/email`   | (challenge token) | Body `{challenge_token}`. Generates + emails a 6-digit OTP. Returns 204. |
| `POST` | `/api/auth/mfa/verify`            | (challenge token) | Body `{challenge_token, code, method}` (`method` ∈ `totp`/`email`). Returns `TokenResponse`. |
| `POST` | `/api/auth/mfa/enroll`            | * | Optional body `{password?, code?, assertion?}`. Mints a CANDIDATE TOTP secret + QR (parked in Redis, not on the account). Returns `{secret, provisioning_uri, qr_code_data_url}`. 400 without a valid step-up when the account already has a live factor. |
| `POST` | `/api/auth/mfa/enroll/verify`     | * | Body `{code}`. Promotes the pending candidate onto the account and flips `mfa_enabled` true — the only writer of `mfa_secret`. |
| `POST` | `/api/auth/mfa/disable`           | * | Optional body `{password?, code?, assertion?}` — the same three-proof step-up as every other factor change (an SSO-only account has no password, so its passkey assertion is the proof). Turns MFA off. Blocked when org enforces MFA. In a tenant with `sso_only` on, a `password` is never a proof on any step-up (it cannot sign in there either) — 400 naming the code / passkey proofs instead. |
| `POST` | `/api/auth/mfa/passkey/register`  | * | Optional body `{password?, code?, assertion?}`. Mints WebAuthn registration options. 400 without a valid step-up when a factor is already live (TOTP or an existing passkey). |
| `DELETE` | `/api/auth/mfa/passkey/{id}`    | * | Body `{password?, code?, assertion?}` — step-up ALWAYS required (the passkey is itself a live factor). Opaque 404 for an id that isn't the caller's. Blocked when it's the last factor under org enforcement. |
| `POST` | `/api/auth/mfa/step-up/passkey`   | * | Body `{operation}` (`totp_enroll`\|`totp_disable`\|`passkey_register`\|`passkey_delete`). Mints WebAuthn assertion options for a factor-management step-up; the signed response goes back as `assertion` on the matching call. Challenge is single-use and bound to (user, step-up, operation), so it can't be replayed as a login or against a different operation. 400 when the account has no registered passkey. |

The challenge endpoints don't take a JWT — they're authenticated by the short-lived challenge token returned from `/api/auth/login`.

## SSO (OIDC — Okta + Microsoft Entra)

| Method  | Path                          | Roles | Description |
|---------|-------------------------------|-------|-------------|
| `GET`   | `/api/auth/sso/config`        | (public) | `?slug=<s>` — returns `{enabled, provider}` for the login button. Never leaks secrets. |
| `GET`   | `/api/auth/sso/authorize`     | (public) | `?slug=<s>` — 302 to the IdP's authorization endpoint. Mints state+nonce in Redis. |
| `POST`  | `/api/auth/sso/callback`      | (public) | `{code, state}` → `{access_token, must_change_password, tenant_slug}`. JIT-provisions the user. |

See [`docs/authentication.md`](../../docs/authentication.md) § SSO for the full handshake. Note: SSO sign-in does not trigger our MFA challenge — IdPs handle their own MFA.

## SSO (SAML 2.0 — Okta, Azure AD, OneLogin, ADFS)

| Method  | Path                          | Roles | Description |
|---------|-------------------------------|-------|-------------|
| `GET`   | `/api/auth/saml/config`       | (public) | `?slug=<s>` — `{enabled, provider}` for the login button. Never leaks secrets. |
| `GET`   | `/api/auth/saml/login`        | (public) | `?slug=<s>` — 302 AuthnRequest to the IdP. Binds a single-use RelayState to `{tenant, request_id}`. |
| `POST`  | `/api/auth/saml/acs`          | (public) | IdP POSTs `SAMLResponse`+`RelayState`. Verifies signature/conditions, JIT-provisions, 303s to the SPA bridge with a one-time code. |
| `POST`  | `/api/auth/saml/exchange`     | (public) | `{code}` → `{access_token, must_change_password, tenant_slug}`. Swaps the one-time handoff code for the JWT (body, never a URL). |
| `GET`   | `/api/auth/saml/metadata`     | (public) | `?slug=<s>` — SP EntityDescriptor XML to register at the IdP. No secrets. |

Same per-tenant `settings.sso` block as OIDC, discriminated by `protocol="saml"`. Verification is pinned to the tenant's `idp_x509_cert` (hardened: signed-assertion-required, SHA-256-only, issuer/audience/destination/InResponseTo enforced, per-tenant replay dedup). MFA is skipped (IdP-owned). See [`docs/authentication.md`](../../docs/authentication.md) § SAML SSO + [`docs/local-sso-saml.md`](../../docs/local-sso-saml.md).

## SCIM 2.0 (user provisioning from the IdP)

All `/api/scim/v2/*` endpoints authenticate via the **per-tenant SCIM bearer** (not a user JWT). Tenant is resolved by sha256-matching the bearer against `Organization.settings.sso.scim_bearer_hash`.

| Method   | Path                                | Description |
|----------|-------------------------------------|-------------|
| `GET`    | `/api/scim/v2/ServiceProviderConfig` | Discovery doc — Okta/Entra probe this first. |
| `GET`    | `/api/scim/v2/Schemas/{id}`         | Minimal User schema — Entra probes this before syncing. |
| `GET`    | `/api/scim/v2/Users`                | List + paginate. Filters: `userName eq`, `emails eq`, `externalId eq`, `active eq`. |
| `GET`    | `/api/scim/v2/Users/{id}`           | Fetch one user. |
| `POST`   | `/api/scim/v2/Users`                | Create. 409 `uniqueness` on duplicate userName. |
| `PATCH`  | `/api/scim/v2/Users/{id}`           | Partial update — supports the ops Okta + Entra send. |
| `DELETE` | `/api/scim/v2/Users/{id}`           | **Soft delete** — sets `is_active=false`. Preserves audit trail. |
| `GET`    | `/api/scim/v2/Groups`               | List + paginate. Filter: `displayName eq`. |
| `GET`    | `/api/scim/v2/Groups/{id}`          | Fetch one group. |
| `POST`   | `/api/scim/v2/Groups`               | Create. 409 `uniqueness` on duplicate displayName. |
| `PUT`    | `/api/scim/v2/Groups/{id}`          | Full replace (displayName + members). |
| `PATCH`  | `/api/scim/v2/Groups/{id}`          | Add/remove/replace members + rename. |
| `DELETE` | `/api/scim/v2/Groups/{id}`          | Remove the group (revokes its mapped role from former members). |

Groups map to RBAC roles via `settings.sso.scim_group_role_map` (`{displayName: role}`); membership changes reconcile `user_roles` idempotently (only mapped roles are touched). See [`docs/authentication.md`](../../docs/authentication.md) § Groups → role mapping.

## Signup (anonymous, pre-tenant)

| Method  | Path                      | Description |
|---------|---------------------------|-------------|
| `GET`   | `/api/signup/slug-check`  | `?slug=<s>` — availability check for the signup form's inline UX |
| `POST`  | `/api/signup/start`       | Rate-limited + captcha-verified. Creates `email_verifications` row and sends verification email. |
| `POST`  | `/api/signup/complete`    | Consumes token, provisions tenant (DB + org + admin user), sends welcome email. |

See [`docs/self-service-signup.md`](../../docs/self-service-signup.md) for the full flow.

## Public config

| Method | Path                  | Description |
|--------|-----------------------|-------------|
| `GET`  | `/api/public-config`  | Non-secret config (hcaptcha sitekey, tenant URL template) for the signup form |

## Organization

| Method | Path                              | Roles  | Description |
|--------|-----------------------------------|--------|-------------|
| `GET`  | `/api/organization`               | *      | Get the current tenant's org settings (company, invoice defaults, ERP, extraction, cards, mfa, sso) |
| `PATCH` | `/api/organization`              | admin  | Patch settings. Body `{name?, settings?}` — `settings` is merged into existing JSONB. |
| `POST` | `/api/organization/test-erp`      | admin  | Test ERP connection (uses request body if provided, otherwise saved config) |
| `POST` | `/api/organization/test-extraction` | admin  | Test AI extraction provider connection |
| `POST` | `/api/organization/sso/scim-token` | admin  | Mint (or rotate) the per-tenant SCIM bearer. Returns `{token, bearer_hash_prefix}` ONCE. |

## Invoices

| Method   | Path                                | Roles | Description |
|----------|-------------------------------------|-------|-------------|
| `GET`    | `/api/invoices`                     | *     | List invoices (paginated, filterable). Returns `priors_summary` and `po_match` per row when applicable. |
| `GET`    | `/api/invoices/counts`              | *     | Per-status tallies for the list-page filter chips — `{counts: {status: n}, total}` via a server-side GROUP BY. Honours the list's population filters (`search`, `vendor`, `invoice_number`, `po_number`, `description`, `amount_min/max`, `due_date_from/to`, `assigned_to_id`) through the SAME `_invoice_list_filters` builder as `GET /api/invoices`, so the chips describe exactly the rows the list would return — **not** `status` (the dimension being tallied). Entity-scoped. Mirrors `/api/vendors/counts` and `/api/purchase-orders/counts`. |
| `GET`    | `/api/invoices/assignable-reviewers` | admin/manager | Candidate approvers for `POST /api/invoices/{id}/assign` — a bare list of `{id, full_name, is_active}` for the org's ACTIVE users holding a role that confers `invoice.approve` (resolved via `effective_permissions`, so a custom role granting it is offered). Deliberately narrower than `GET /api/admin/users`: **no email, no roles, no audit metadata** — that projection is what makes the admin directory admin-only, and none of it is needed to pick an approver. RBAC mirrors `/assign` exactly (admin + ap_manager); the picker used to source the admin route and 403'd for every other role, so an invoice on a `approver_strategy: "manual"` workflow could not be submitted at all. |
| `GET`    | `/api/invoices/chat/mentionable-users` | any authed | Colleagues the supplier-chat @mention picker may offer — the same `{id, full_name, is_active}` shape as `assignable-reviewers`, for the org's ACTIVE users. Gated on `get_current_user`, matching `POST /api/invoices/{id}/chat` exactly: reading the candidate list and acting on it are the same privilege. Deliberately NOT `assignable-reviewers` (admin/manager-only and scoped to `invoice.approve` holders — an ap_clerk or CFO can post a mention yet could not read that list) and deliberately NOT `GET /api/admin/users` (admin-only, and carries the email/roles a chat composer must not hold). The POST validates `mention_user_ids` against this same roster — an id outside it is a 400, not a silently stored mention of somebody who is never notified. See `docs/supplier-chat.md`. |
| `GET`    | `/api/invoices/{id}`                | *     | Get single invoice — includes the latest `po_match` JSONB result and any `warnings`. A warning is `{type, severity, message, code, params}`: `code` + `params` are what a client localizes on, `message` the backend's English fallback (and the only field on a row written before the code catalogue shipped). See `backend/docs/invoice-warnings.md` |
| `GET`    | `/api/invoices/{id}/priors`         | *     | Priors metadata from latest extraction (vendor cache + RAG neighbors). See [`ai-extraction.md`](ai-extraction.md). |
| `GET`    | `/api/invoices/{id}/line-items`     | *     | Get invoice line items |
| `PUT`    | `/api/invoices/{id}/line-items`     | admin/manager/cfo | Replace all line items. 409 once the invoice is approved (financial freeze). Writes an `invoice.line_items_edited` audit row, re-runs `refresh_warnings`, and reconciles the summed lines against the header money fields — the header `amount` is never recomputed from the lines; a divergence raises an `error` `line_total_mismatch` warning + exception. Returns `{saved, line_items_total, header_amount, reconciles_with_header}` (money as exact decimal strings). See `line-total-reconciliation.md` |
| `POST`   | `/api/invoices`                     | admin/manager/cfo | Create invoice |
| `POST`   | `/api/invoices/{id}/file`           | admin/manager/cfo | Attach a source file to a manually-entered invoice that has none yet. 409 if it already has one. |
| `PUT`    | `/api/invoices/{id}/file`           | admin/manager/cfo | Replace an invoice's existing file. 404 if none to replace, 409 if the invoice is done. |
| `DELETE` | `/api/invoices/{id}/file`           | admin/manager/cfo | Delete an invoice's file. 404 if none to delete, 409 if the invoice is done. |
| `PATCH`  | `/api/invoices/{id}`                | admin/manager/cfo | Update invoice. Optional optimistic-concurrency guard: pass `expected_updated_at` (the `updated_at` ISO timestamp read alongside the invoice) and a row that has since moved (checked under a row lock — same pattern as `get_invoice_for_update`) is refused 409 instead of silently overwritten; omit it for the pre-existing behavior. |
| `DELETE` | `/api/invoices/{id}`                | admin/manager/cfo | Delete invoice |
| `POST`   | `/api/invoices/bulk/delete`         | admin/manager/cfo | Bulk delete |
| `POST`   | `/api/invoices/bulk/status`         | admin/manager/cfo | Bulk status change. Partial-success: returns `{updated, skipped}`; a member the state machine refuses (or that fails a control on the `approved`/`rejected` paths) is listed in `skipped` and never aborts the batch. Only `new`/`pending`/`ready_for_review`/`approved`/`rejected`/`done` are settable — the rest 422 (they are workflow-engine driven). |
| `POST`   | `/api/invoices/bulk/export`         | *     | Bulk export (CSV/JSON/XML) |
| `POST`   | `/api/invoices/bulk-recode-gl`      | admin | Bulk GL re-code via vendor priors (+ optional AI fallback). Defaults to dry-run. See [`ai-extraction.md`](ai-extraction.md) § Bulk re-coding. |

**Query parameters for `GET /api/invoices`:** `page`, `page_size`, `status` (comma-sep), `vendor`, `invoice_number`, `po_number`, `description`, `amount_min`, `amount_max`, `due_date_from`, `due_date_to`, `search`, `assigned_to_id` (exact match on `Invoice.assigned_to_id`; the frontend's "Assigned to" filter and "My Approvals" quick view both drive off it — the latter passing the caller's own id). `GET /api/invoices/ids` (the select-all-matching resolver) accepts the same filter set, `assigned_to_id` included, so a select-all under either filter can't silently widen past what's on screen. Every invoice row already carries `assigned_to_id` + `assigned_to` (the display name, denormalized at assign time) in `InvoiceResponse` — no separate lookup needed to render an assignee column.

## Workflow Actions (per invoice)

| Method | Path                              | Roles | Description |
|--------|-----------------------------------|-------|-------------|
| `POST` | `/api/invoices/upload`            | admin/manager/cfo | Multipart file upload. Creates invoice + optionally triggers extraction. |
| `POST` | `/api/invoices/{id}/extract`      | admin/manager/cfo | Re-trigger extraction on `new` or `failed` invoices |
| `POST` | `/api/invoices/{id}/reset-extraction` | admin/manager/cfo | Reset stuck `pending` extraction back to `new` |
| `POST` | `/api/invoices/{id}/assign`       | admin/manager | Assign reviewer (body `{user_id}`). The reviewer is resolved **inside the caller's own org and must be active** — `users` is control-plane, so an unscoped by-id lookup reached every tenant's accounts: a foreign user could be stamped onto `Invoice.assigned_to_id` and then emailed this tenant's invoice number, vendor and amount, while the invoice sat owned by an account that can never act on it. Wrong-org, deactivated and unknown are the same opaque 404 (no enumeration); a malformed `user_id` is a 422. Same guard as `POST /api/exceptions/{id}/assign` and `POST /api/auth/delegation`, and it accepts exactly what `GET /api/invoices/assignable-reviewers` offers. |
| `POST` | `/api/invoices/{id}/approve`      | admin/manager/cfo | Approve. Body may include field corrections. |
| `POST` | `/api/invoices/{id}/reject`       | admin/manager/cfo | Reject (body `{reason}`) |
| `POST` | `/api/invoices/{id}/resubmit`     | admin/manager/cfo | Resubmit after rejection |
| `POST` | `/api/invoices/{id}/send-to-erp`  | admin/manager/cfo | Transition to `sending_to_erp` and dispatch (local thread or SQS) |
| `POST` | `/api/invoices/{id}/retry-erp`    | admin/manager/cfo | Retry a failed ERP push |
| `POST` | `/api/invoices/{id}/complete`     | admin/manager/cfo | Advance to the next workflow step based on the snapshot |
| `GET`  | `/api/invoices/{id}/audit-log`    | * | Full audit trail (with actor names) |
| `GET`  | `/api/invoices/{id}/workflow`     | * | Workflow instance + steps |
| `GET`  | `/api/invoices/{id}/extraction`   | * | AI extraction results |
| `GET`  | `/api/invoices/{id}/export`       | * | Export single invoice |
| `GET`  | `/api/invoices/{id}/einvoice`     | admin/manager/cfo/ap_clerk | Export invoice as UBL 2.1 XML (`?format=ubl`). 400 bad format, 404 unknown, 422 (PII-free `field:code` list) on tax-invalid; returns `application/xml` attachment. See [`e-invoicing.md`](e-invoicing.md). |
| `POST` | `/api/invoices/{id}/peppol-send`  | admin/manager/cfo | Transmit invoice as PEPPOL BIS Billing 3.0 via the configured Access Point. Body: `{receiver_scheme, receiver_value, sender_scheme?, sender_value?}`. 200 `PeppolSendResponse` `{transmission_id, status, message_id, direction, already_sent}`; 400 malformed participant / no sender; 403 role; 404 invoice; 422 invoice not approved, tax-invalid, or receiver not registered / doc-type unsupported (PII-free). Idempotent — a re-send of a live transmission returns `already_sent=true` with no second adapter call. See [`peppol.md`](peppol.md). |
| `GET`  | `/api/invoices/file/{file_key}`   | * | Proxy invoice file from S3 |

## Audit Trail (auditor export — SOX)

GET-only. The auditor-export surface, separate from the per-invoice operational
`/api/invoices/{id}/audit-log` above. Every call is itself audited. The
response carries only sanitised `details` (field-name lists + before/after
diffs written at audit time) — no banking/tax-id value can reach the HTTP
surface.

| Method | Path                              | Roles | Description |
|--------|-----------------------------------|-------|-------------|
| `GET`  | `/api/audit/export`               | admin/cfo | Auditor export. Query: `invoice_id` **or** (`start`,`end`) — mutually exclusive; optional `entity_type`; `format=json\|csv\|pdf` (default `json`). Ordered by `created_at`. Writes an `audit.exported` row (`details.format` records the chosen format). Invalid range / missing args → generic `400`; unknown invoice → `404`. `format=pdf` returns a formatted SOX audit-trail report (`application/pdf` attachment) — cover (org, scope, generated-at/-by), event-count summary grouped by action, chronological trail table; renders exactly the same already-sanitised entries as JSON/CSV (PII kept out of `details` at audit-write time — no broader exposure). See [`access-reviews.md`](access-reviews.md) for the related access-review surface. |
| `GET`  | `/api/audit/invoice/{id}`         | admin/manager/cfo | Per-invoice trail (auditor-facing alias of the operational endpoint), ordered by `created_at`. |
| `GET`  | `/api/audit/invoice/{id}/verify-signatures` | admin/cfo | Cryptographic non-repudiation check on the invoice's approval signatures. Re-derives the HMAC-SHA256 on each `invoice.approved` audit row (against the invoice's current amount, the row's actor, and the signed timestamp) and returns per-approval `{audit_row_id, signed_at, actor, signed, valid}`. A post-approval amount/actor/timestamp tamper → `valid: false`. Writes an `audit.viewed` access row. See `approval-signatures.md`. |
| `GET`  | `/api/audit/verify-signatures`    | admin/cfo | **Population-level** non-repudiation test over a period — the same check applied to EVERY `invoice.approved` row in `start`..`end` (at least one required; `end` is whole-day inclusive; optional `limit`, default 100, caps the findings list only). Returns `{start, end, signing_configured, invoices_covered, approvals_checked, valid, invalid, unsigned, findings[], findings_truncated}`; a finding is `{invoice_id, invoice_number, audit_row_id, actor_id, actor, signed_at, verdict}` where `verdict` is `invalid` (digest no longer re-derives — tamper) or `unsigned` (row predates signing — nothing to verify). Counts cover the whole population even when `findings` is truncated. Writes an `audit.viewed` access row carrying counts only. See `approval-signatures.md`. |

## Retention Policy (SOX records management)

Per-record-class retention windows on `Organization.settings.retention`
(configurable, not hardcoded). The enforcement sweep is `services/retention_sweep.py`
(disabled by default behind `FEOH_RETENTION_ENABLED`). See `retention.md`.

| Method | Path                       | Roles | Description |
|--------|----------------------------|-------|-------------|
| `GET`  | `/api/retention-policy`    | admin | Effective policy per record class (`invoices`, `audit_log`) + platform default + whether the sweep is enabled. |
| `PUT`  | `/api/retention-policy`    | admin | Update one or more `<class>` windows (months, `> 0`). Unknown class / non-positive → `422`. Writes a `retention_policy.updated` audit row. |

## Access Reviews (periodic — SOX)

Periodic review of users holding **elevated** roles (`admin` / `ap_manager` /
`cfo`). Compute-on-read: a user's "last privileged action" is derived live from
`MAX(audit_log.created_at)` over their **mutating** rows (read verbs `*.viewed` /
`*.exported` are excluded). No `last_elevated_use` column, no migration. The
review-list GET is itself a sensitive read (writes `access_review.viewed`).
See [`access-reviews.md`](access-reviews.md).

| Method | Path                              | Roles | Description |
|--------|-----------------------------------|-------|-------------|
| `GET`  | `/api/access-reviews`             | admin/cfo | Computed review list. Each user: `user_id`, `full_name`, `email`, `roles`, `last_privileged_action_at`, `dormant`, `days_since`. DORMANT when last mutating action is older than `FEOH_ACCESS_REVIEW_DORMANT_DAYS` (default 90) or never acted. Sorted dormant-first. Inactive users excluded. Writes an `access_review.viewed` row. |
| `POST` | `/api/access-reviews/acknowledge` | admin/cfo | Records review completion for the period: writes an `access_review.completed` audit row + stamps `Organization.settings.access_review.{last_completed_at,last_completed_by}` (control plane). Idempotent-friendly (re-stamps). |

## Workflow Definitions

| Method   | Path                              | Roles | Description |
|----------|-----------------------------------|-------|-------------|
| `GET`    | `/api/workflows/active/steps`     | * | Active steps + approval config (used in invoice modal) |
| `GET`    | `/api/workflows`                  | * | List workflow definitions |
| `GET`    | `/api/workflows/{id}`             | * | Get one |
| `POST`   | `/api/workflows`                  | admin | Create |
| `PATCH`  | `/api/workflows/{id}`             | admin | Update (activating one auto-deactivates the others) |
| `DELETE` | `/api/workflows/{id}`             | admin | Delete (not the default) |

## Vendors

| Method   | Path                            | Roles | Description |
|----------|---------------------------------|-------|-------------|
| `GET`    | `/api/vendors`                  | admin/manager/cfo | List (paginated, filterable by `status`, `source`, `search`) |
| `GET`    | `/api/vendors/{id}`             | admin/manager/cfo | Get one |
| `POST`   | `/api/vendors`                  | admin/manager | Create — auto-marked `active` and `verified_by` set |
| `PATCH`  | `/api/vendors/{id}`             | admin/manager | Update |
| `DELETE` | `/api/vendors/{id}`             | admin/manager | Delete |
| `POST`   | `/api/vendors/{id}/verify`      | admin/manager | Promote `unverified` → `active` |
| `POST`   | `/api/vendors/{id}/reject`      | admin/manager | Mark `rejected` |
| `POST`   | `/api/vendors/sync-erp`         | admin/manager | Pull vendors from connected ERP |
| `GET`    | `/api/vendors/counts`           | admin/manager/cfo | Per-status tallies for the list filter chips — `{total, by_status}` via a server-side GROUP BY, so the red "Unverified" attention badge can't undercount past page 1. Honours the list's population filters (`search`, `source`) through the SAME `_vendor_list_filters` builder as `GET /api/vendors` and `/ids` — **not** `status` (the dimension being tallied). Entity-scoped. Mirrors `/api/invoices/counts`. |
| `GET`    | `/api/vendors/change-requests/counts`  | admin/manager | Whole-set tallies for the dual-control queue (`{total, pending, by_status}`); counts only, PII-free — drives the nav badge. Gated **exactly** like the queue list (decisions §48): it previously admitted `cfo`, who cannot read the queue, so the size of the staged-bank-change review set was visible to a role excluded from it |
| `GET`    | `/api/vendors/change-requests`  | admin/manager | Pending supplier change-request queue (`?status=`); proposed value masked |
| `GET`    | `/api/vendors/{id}/change-requests` | admin/manager/cfo | One vendor's change requests; value revealed |
| `POST`   | `/api/vendors/change-requests/{id}/approve` | admin/manager | Apply staged bank/tax change to the vendor (exactly-once) |
| `POST`   | `/api/vendors/change-requests/{id}/reject`  | admin/manager | Reject; vendor untouched |

## Purchase Orders

| Method | Path                           | Roles | Description |
|--------|--------------------------------|-------|-------------|
| `GET`  | `/api/purchase-orders`         | * | List POs (filterable by `status`, `vendor_id`, `search`) |
| `GET`  | `/api/purchase-orders/counts`  | * | Whole-set status tallies for the filter chips — `{total, by_status}`, entity-scoped, honours `search` + `vendor_id` through the SAME `_purchase_order_list_filters` builder as the list (but not `status`, the dimension being tallied). Mirrors `GET /api/vendors/counts`; the list's `total` counts only the ACTIVE filter's result set, so it can't label the All chip. A malformed `vendor_id` is a 422 from the boundary on both endpoints — as is an explicitly EMPTY `?vendor_id=`, which previously read as falsy and silently dropped the filter. |
| `POST` | `/api/purchase-orders/sync-erp` | admin/manager | Pull POs from connected ERP |

## GL Accounts

| Method | Path                          | Roles | Description |
|--------|-------------------------------|-------|-------------|
| `GET`  | `/api/gl-accounts`            | * | List Chart of Accounts (filterable, `active_only=true` default) |
| `POST` | `/api/gl-accounts`            | admin/manager | Create a GL account |
| `PATCH`| `/api/gl-accounts/{id}`       | admin/manager | Correct (`name` / `account_type` / `parent_code`) or retire (`is_active`) an account. `code` + `entity_id` are immutable; there is no DELETE — see `api-surface.md` |
| `POST` | `/api/gl-accounts/sync-erp`   | admin/manager | Pull Chart of Accounts from ERP |

## Payments

All payment endpoints require `admin/manager/cfo`.

| Method | Path                          | Description |
|--------|-------------------------------|-------------|
| `GET`  | `/api/payments`               | List payments (filterable by `status`, `method`, `invoice_id`, `search`, amount range) |
| `GET`  | `/api/payments/{id}`          | Get one payment |
| `POST` | `/api/payments`               | Create payment for an invoice |
| `GET`  | `/api/payments/queue`         | Approved invoices sorted by due date. Each row carries `blocked` / `blocked_reason` / `required_method` — the verdict of `payment_runs.run_refusal_reasons`, the SAME predicate set `POST /api/payments/runs` enforces, so the queue never offers a row a run then 409s. `blocked` = refused on every rail (an unresolved `PAYMENT_BLOCKING_EXCEPTION_TYPES` exception, or `fully_credited`); `required_method` = payable, but only on that rail (a live virtual card already claims the invoice, and `virtual_card` converges onto it). Reason is a fixed vocabulary code, never the exception's description. `blocked_total` and `selectable_total` are deliberately not complementary — see `docs/payments.md`. |
| `GET`  | `/api/payments/counts`        | Per-status tallies for the History-tab filter chips — `{total, by_status}` via a server-side GROUP BY, so the chips span every page. Honours the list's population filters (`method`, `invoice_id`, `search`, `amount_min/max`) through the SAME `_payment_list_filters` builder as `GET /api/payments` — **not** `status` (the dimension being tallied). Entity-scoped; same permission gate as the list (`payment.execute` OR `payment.void`). Mirrors `/api/invoices/counts`. |
| `GET`  | `/api/payments/counts`        | Per-status tallies for the History-tab filter chips — `{total, by_status}` via a server-side GROUP BY, so the chips span every page. Honours the list's population filters (`method`, `invoice_id`, `search`, `amount_min/max`) through the SAME `_payment_list_filters` builder as `GET /api/payments` — **not** `status` (the dimension being tallied). Entity-scoped; same permission gate as the list (`payment.execute` OR `payment.void`). Mirrors `/api/invoices/counts`. |
| `GET`  | `/api/payments/summary`       | Totals: paid, pending, queue count, rebates earned |
| `GET`  | `/api/payments/runs/`         | List payment runs |
| `POST` | `/api/payments/runs`          | Create a payment run |
| `GET`  | `/api/payments/runs/{id}`     | Get payment run details |
| `POST` | `/api/payments/runs/{id}/execute` | Execute a draft payment run via the configured processor; response includes `payments_completed` / `payments_in_flight` / `payments_failed` rollup |
| `POST` | `/api/payments/webhook/{tenant_slug}/{provider}` | (provider-signed) — payment-status webhook from Modern Treasury / similar. Tenant in URL path, signature verified per adapter. |

## Virtual Cards

All card endpoints require `admin/manager/cfo` (except the webhook).

| Method | Path                          | Description |
|--------|-------------------------------|-------------|
| `GET`  | `/api/cards`                  | List virtual cards |
| `GET`  | `/api/cards/dashboard`        | Card spend + rebate KPIs |
| `POST` | `/api/cards/generate`         | Generate one or more cards (returns the new list) |
| `GET`  | `/api/cards/{id}/details`     | Card details (includes PAN — Lithic/Nium fetched on demand) |
| `POST` | `/api/cards/{id}/cancel`      | Cancel/void a card |
| `GET`  | `/api/cards/rebates`          | List rebate rows, paginated (`page`/`page_size`, optional `period`). Each row states the `currency` of the card that earned it; `total` is the row count and `total_amount` the whole-set money sum in the org's reporting currency |
| `POST` | `/api/cards/webhook/{provider}` | (provider-signed) — Lithic/Nium event webhook |

## Exceptions

All exception endpoints require `admin/manager`.

| Method | Path                              | Description |
|--------|-----------------------------------|-------------|
| `GET`  | `/api/exceptions`                 | List flagged invoices (filter by `status`, `type`, `severity`) |
| `GET`  | `/api/exceptions/summary`         | Counts by status + open-by-type breakdown |
| `POST` | `/api/exceptions/{id}/resolve`    | Body `{resolution, action}` (`resolve`/`escalate`/`dismiss`) |

## Notifications

Per-user, any authenticated role. List + mark endpoints are tenant-scoped to
`recipient_user_id == current user`; a notification belonging to another user
returns the same `404` as a missing one (no enumeration). Preferences are
user-global (control plane). See `notifications.md`.

| Method  | Path                                  | Description |
|---------|---------------------------------------|-------------|
| `GET`   | `/api/notifications`                  | List current user's notifications (`?unread_only=&page=&page_size=`); envelope adds `unread` |
| `GET`   | `/api/notifications/unread-count`     | `{unread}` for the sidebar badge |
| `POST`  | `/api/notifications/{id}/read`        | Mark one read (naturally idempotent); `404` if not owned |
| `POST`  | `/api/notifications/read-all`         | Mark all current-user unread → read; returns `{updated}` |
| `GET`   | `/api/notifications/preferences`      | Per-event `{email, in_app}` map (defaults if unset) |
| `PATCH` | `/api/notifications/preferences`      | Partial update; unspecified events unchanged |

## Dashboard

| Method | Path              | Roles | Description |
|--------|-------------------|-------|-------------|
| `GET`  | `/api/dashboard`  | *     | Aggregated KPIs (status counts, aging buckets, top vendors, etc.) |

## Admin (User Management)

All require `admin`.

| Method   | Path                     | Description |
|----------|--------------------------|-------------|
| `GET`    | `/api/admin/users`       | List all users in the organization |
| `GET`    | `/api/admin/roles`       | List all available roles |
| `POST`   | `/api/admin/users`       | Create user (returns temp password) |
| `PATCH`  | `/api/admin/users/{id}`  | Update user (name, email, roles, password, active) |
| `POST`   | `/api/admin/users/{id}/revoke-sessions` | Force-log-out a user without changing their account. Org-scoped (foreign user → 404), idempotent, audited `user.sessions_revoked` |
| `DELETE` | `/api/admin/users/{id}`  | Permanently delete a user |

## ERP Inbound Webhook

| Method | Path                              | Description |
|--------|-----------------------------------|-------------|
| `POST` | `/api/erp/webhook/{erp_type}`     | (provider-signed) — inbound ERP status updates |

## Goods Receipts

Used by 3-way matching. `admin` / `ap_manager` / `ap_clerk`.

| Method | Path                                    | Description |
|--------|-----------------------------------------|-------------|
| `GET`  | `/api/goods-receipts`                   | List goods receipts (paginated, filters by `vendor_id`, `po_id`, `status`) |
| `GET`  | `/api/goods-receipts/{id}`              | Single GR with line items |

## Credit Memos

| Method | Path                              | Roles | Description |
|--------|-----------------------------------|-------|-------------|
| `GET`  | `/api/credit-memos`                | admin, ap_manager, ap_clerk, cfo | List credit memos (paginated, entity-scoped, `?status=`) |
| `POST` | `/api/credit-memos`                | admin, ap_manager | Create a credit memo. With no `invoice_id` it lands `open`; with one it is applied on the spot and runs the same guards as `/apply` |
| `POST` | `/api/credit-memos/{id}/apply`     | admin, ap_manager | Apply an `open` credit memo against a payable |
| `POST` | `/api/credit-memos/{id}/void`      | admin, ap_manager | Void an `open` memo (409 once `applied` — applied memos are immutable for audit) |

### `currency` is resolved, never defaulted to USD

`CreditMemoCreate.currency` is **optional**. The create endpoint resolves it in
three rungs:

1. what the caller asserted (normalised to upper case);
2. the named invoice's own `currency`, when `invoice_id` is supplied — the memo
   *inherits* rather than asserting;
3. the org's reporting currency (`services/currency_conversion.resolve_reporting_currency`,
   which itself falls back to `FEOH_REPORTING_CURRENCY_DEFAULT`).

The schema used to default to a hardcoded `"USD"`, which dead-ended every
non-USD tenant: the memo was stamped USD, guard 2 below then 409'd it against
the EUR invoice on the very same request, and — because there is **no PATCH on
credit memos** — the row could never be applied or corrected. An explicitly
asserted currency is still checked against the invoice, so inheriting is not a
way to launder a real mismatch.

### Applying a credit — the guards

Both application paths (`POST /api/credit-memos` with an `invoice_id`, and
`POST /api/credit-memos/{id}/apply`) row-lock the target invoice and then
enforce, in order, three 409s:

1. **Vendor must match, and must be PROVEN to match** — the memo's `vendor_id`
   has to equal the invoice's `vendor_id`. A NULL `Invoice.vendor_id` is
   **refused**, not waved through: an unlinked invoice is one whose vendor
   cannot be established, and crediting it would reduce a balance nobody can
   attribute. (This is fail-closed by design. The guard used to skip entirely
   on NULL, which let one vendor's memo be applied to another vendor's invoice
   for any invoice created without extraction — see
   `_assert_vendor_matches` in `app/api/credit_memos.py`.)
2. **Currency must match** — the remaining-balance math subtracts the amounts
   directly, so a EUR memo on a USD invoice would corrupt it. This fires only
   on a currency the caller actually asserted (see below).
3. **No over-application** — the sum of `applied` memos on an invoice may never
   exceed the invoice amount (a credit past the balance would mint a negative
   payable).

**Where the vendor link comes from.** `Invoice.vendor_id` is resolved by
`services/vendor_matching.match_and_link_vendor` — on the AI-extraction path, on
manual entry (`POST /api/invoices`), and again on `PATCH /api/invoices/{id}`
whenever the vendor name is (re)saved and the link is stale or missing.
Re-saving an invoice's vendor is therefore the supported way to resolve an
invoice that predates create-time resolution and so still carries a NULL link;
there is deliberately **no** backfill migration, because guessing a historical
invoice's vendor is exactly the mis-attribution the guard exists to prevent.
Clearing the vendor name on a `PATCH` clears the link too — a nameless invoice
must not keep pointing at a vendor nothing visible corroborates.
`GET /api/invoices` and `GET /api/invoices/{id}` expose the resolved
`vendor_id` so the UI can offer only eligible targets.

## Tax / 1099

| Method | Path                                    | Roles | Description |
|--------|-----------------------------------------|-------|-------------|
| `GET`  | `/api/tax/vendors/{vendor_id}`           | admin, ap_manager, cfo | Vendor's 1099 status (W-9 received, classification, YTD totals) |
| `POST` | `/api/tax/vendors/{vendor_id}/w9`        | admin, ap_manager | Upload signed W-9 PDF + mark vendor 1099-eligible |
| `GET`  | `/api/tax/1099/{year}`                   | admin, ap_manager, cfo | YTD 1099 summary across all eligible vendors |

## Supplier Portal (`typ=vendor` JWT, vendor-scoped)

All require a vendor JWT (`get_current_vendor_user`); every query filters on the
caller's own `vendor_id`; cross-vendor IDs return 404. See
[`supplier-portal.md`](supplier-portal.md).

| Method | Path                                    | Description |
|--------|-----------------------------------------|-------------|
| `GET`  | `/api/portal/invoices`                  | Vendor-scoped invoice list |
| `GET`  | `/api/portal/invoices/{id}`             | Get one (404 cross-vendor) |
| `GET`  | `/api/portal/invoices/{id}/einvoice`    | UBL 2.1 XML download (vendor-scoped; 404 cross-vendor; soft tax warnings logged only, never 422'd to the supplier). |
| `POST` | `/api/portal/invoices`                  | Multipart PDF upload → extraction pipeline |
| `GET`  | `/api/portal/payments`                  | Payment history |
| `GET`  | `/api/portal/payments/{id}/remittance`  | Remittance-advice PDF (404 on a foreign payment) |
| `GET`  | `/api/portal/purchase-orders`           | Vendor-scoped PO list |
| `GET`  | `/api/portal/purchase-orders/{id}`      | PO detail + line items |
| `POST` | `/api/portal/purchase-orders/{id}/flip` | PO flip → new invoice (idempotent per vendor+PO) |
| `GET`  | `/api/portal/company`                   | Company info (bank/tax masked) + pending change |
| `PATCH`| `/api/portal/company`                   | Update phone/address/email (applies live) |
| `POST` | `/api/portal/company/bank-change`       | Stage a bank-details change (202; AP approval required) |
| `POST` | `/api/portal/company/tax-id-change`     | Stage a tax-ID change (202; AP approval required) |
| `GET`  | `/api/portal/company/change-requests`   | This vendor's change requests + statuses |

## Analytics

CFO-grade aggregates beyond the basic dashboard, plus CSV/PDF export and recurring report scheduling.

| Method | Path                                       | Roles | Description |
|--------|--------------------------------------------|-------|-------------|
| `GET`  | `/api/analytics/spend`                      | cfo, admin, ap_manager | Aggregated spend by GL / vendor / cost center / time bucket |
| `GET`  | `/api/analytics/dpo`                        | cfo, admin | Days-payable-outstanding rolling history |
| `GET`  | `/api/analytics/cashflow_forecast`          | cfo, admin | Projected AP outflows bucketed day/week/month (committed vs pending) |
| `GET`  | `/api/analytics/cashflow_whatif`            | cfo, admin | Payment-timing what-if: early vs on-time vs late, with discount capture |
| `GET`  | `/api/analytics/cash_position`              | cfo, admin | Running cash position from a BYO opening balance + threshold-breach alerts |
| `GET`  | `/api/analytics/export/{report}`            | cfo, admin, ap_manager | CSV export: invoice_register, vendor_spend, payment_register, aging_snapshot, cashflow_forecast |
| `GET`  | `/api/analytics/scheduled-reports`          | cfo, admin | List scheduled-report definitions |
| `POST` | `/api/analytics/scheduled-reports`          | cfo, admin | Create a recurring report (cron + recipients + format) |
| `PATCH`| `/api/analytics/scheduled-reports/{id}`     | cfo, admin | Update a scheduled report |
| `DELETE`|`/api/analytics/scheduled-reports/{id}`     | cfo, admin | Delete a scheduled report |

## Email Intake

Inbound email turns provider attachments into invoices. The public webhook is provider-signed (HMAC over the body); the admin endpoints manage the per-tenant intake address.

| Method | Path                                         | Roles | Description |
|--------|----------------------------------------------|-------|-------------|
| `POST` | `/api/email-intake/inbound/{provider}`       | (provider-signed) | Webhook entrypoint. Returns 204 silently on every rejection (bad signature / unknown provider / unparsable payload) to avoid enumeration. |
| `GET`  | `/api/organization/email-intake`             | admin | Show the current intake address + enabled flag |
| `POST` | `/api/organization/email-intake/rotate-token`| admin | Generate a new token; the old address stops accepting email immediately |

## Health

| Method | Path           | Description |
|--------|----------------|-------------|
| `GET`  | `/api/health`  | Health check |

## Example: full curl flow (MFA off)

```bash
# Login (no tenant header needed). When FEOH_MFA_ENABLED=true and the user is
# enrolled, the response is an MFAChallengeResponse instead — see
# docs/authentication.md § MFA for the verify flow.
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@acme.com","password":"demo"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# List invoices (requires both auth + tenant headers)
curl http://localhost:8000/api/invoices \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Tenant-Slug: acme"

# Get dashboard KPIs
curl http://localhost:8000/api/dashboard \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Tenant-Slug: acme"
```

See [`docs/authentication.md`](../../docs/authentication.md) for full auth + RBAC details and [`docs/multi-tenancy.md`](../../docs/multi-tenancy.md) for tenant routing.
