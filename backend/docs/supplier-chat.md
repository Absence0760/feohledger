# Embedded Supplier Chat & Collaboration

A per-invoice conversation shared between the AP team and the supplier — in-app
collaboration that replaces the email chains that normally surround a disputed
or incomplete invoice. The AP team posts from the invoice detail modal; the
supplier posts from the supplier portal. Both see the same thread. File
attachments, @mentions of AP teammates, canned templates, and resolve/reopen
round out the slice, with every post linked to the per-invoice audit trail.

## Data model

Two tenant-scoped tables (`models/supplier_chat.py`), following the
`contract.py` pattern: inline `organization_id` + `EntityMixin` on the parent
thread; the child message carries neither mixin — its scope is inherited
through the parent (mirroring `ContractLineItem` / `InvoiceLineItem`).

### `SupplierChatThread` (`supplier_chat_threads`)

One thread per invoice, **lazy-created on first post** (a GET never creates a
row). The `uq_supplier_chat_thread_invoice` unique index on `invoice_id`
guarantees at most one thread per invoice.

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid PK | |
| `invoice_id` | uuid FK → invoices | Required, indexed; **unique** via `__table_args__`. |
| `status` | enum | `open` (default), `resolved`. `Enum(native_enum=False, length=20)`. |
| `resolved_at` | timestamptz | Set on resolve, cleared on reopen. |
| `resolved_by` | uuid | Control-plane `users.id` — **no FK** (cross-DB). |
| `organization_id` | uuid | Inline, indexed. |
| `entity_id` | uuid FK → entities | From `EntityMixin`; copied from the invoice. |
| `created_at` / `updated_at` | timestamptz | From `TimestampMixin`. |

Relationship: `messages` (cascade `all, delete-orphan`, ordered by
`created_at`).

### `SupplierChatMessage` (`supplier_chat_messages`)

Append-only — messages are never edited or deleted. No `EntityMixin`, no inline
`organization_id`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid PK | |
| `thread_id` | uuid FK → supplier_chat_threads | Required, indexed. |
| `author_role` | enum | `ap_team`, `supplier`, `system`. `Enum(native_enum=False, length=20)`. |
| `author_user_id` | uuid | **No FK** — polymorphic (see below). NULL for `system`. |
| `author_name` | varchar(255) | Display-name snapshot. |
| `body` | text | Plain text. Rendered as text (never `{@html}`). |
| `mentions` | jsonb | List of control-plane `users.id` strings (AP @mentions). NULL for supplier posts. |
| `attachments` | jsonb | List of attachment dicts (shape below). |
| `template_key` | varchar(50) | `missing_po` / `amount_mismatch` / `payment_status` / NULL. |
| `created_at` / `updated_at` | timestamptz | From `TimestampMixin`. |

### Author polymorphism

The author is modeled by `author_role` + `author_user_id` with **no FK** on
`author_user_id`, because it points across databases/tables:

| `author_role` | `author_user_id` | Source |
|---------------|------------------|--------|
| `ap_team` | `user.id` | control-plane `users.id` |
| `supplier` | `vu.id` | tenant `vendor_users.id` |
| `system` | NULL | template/automated posts (reserved — not emitted this slice) |

When an AP user posts *from* a template, the role is still `ap_team`;
`template_key` records which template. `system` is reserved for future
automated posts and is not produced by this slice.

### `attachments` JSONB shape (list of dicts)

```json
[
  {
    "file_key": "ORG_UUID/chat/INVOICE_UUID/MESSAGE_UUID/safe-name.pdf",
    "file_url": "/api/invoices/INVOICE_UUID/chat/file/<file_key>",
    "filename": "safe-name.pdf",
    "content_type": "application/pdf",
    "size": 12345
  }
]
```

The stored `file_url` is **not** what either surface serves. Both message
serializers rebuild it from `file_key` for the surface doing the READING
(`supplier_chat.attachment_url`): `/api/invoices/{id}/chat/file/{key}` on the AP
side, `/api/portal/invoices/{id}/chat/file/{key}` on the portal. Echoing the
stored one handed each surface the poster's route, which the reader's JWT is
refused on (decisions §226). The stored value is the fallback only for a legacy
attachment with no `file_key`.

### `mentions` JSONB shape (list of id strings)

```json
["USER_UUID_1", "USER_UUID_2"]
```

Control-plane `users.id` values as strings. AP side only; always `null`/absent
for supplier posts. The portal response schema omits this field entirely.

Every id is checked against the org's ACTIVE users before it is stored — the
same roster `GET /invoices/chat/mentionable-users` offers, so the picker and the
POST cannot disagree about who exists. An id outside it (another tenant's user,
a deactivated colleague, a UUID that names nobody) is a **400**, exactly like a
malformed one, and nothing is written — the refusal happens before the thread is
lazy-created. Previously only the UUID *syntax* was checked, so any id at all
could be persisted here and read back on every GET of the thread. Nothing
leaked — `notify_event` scopes its recipient load to the organization, so a
foreign id resolved to nobody — but the record then asserted a mention of
someone who was never notified and whose name no reader can resolve. Refusing
beats silently dropping: a stale selection surfaces instead of vanishing from a
message the author believes they sent.

### Who can be mentioned — `GET /invoices/chat/mentionable-users`

The picker's source. Returns the caller's org's ACTIVE users as
`{id, full_name, is_active}` — **no email, no roles, no audit metadata** —
ordered by name.

Gated on `get_current_user`, matching `POST /invoices/{id}/chat` exactly:
reading the candidate list and acting on it are the same privilege, so a
narrower read gate would leave a role able to mention but unable to see who.
Neither existing endpoint fits, which is why this one exists:

* `GET /api/admin/users` is `require_permission(user.manage)` and carries
  emails, roles and last-login. Every non-admin 403s — the identical bug already
  fixed once for the approver picker — and none of that payload belongs in a
  chat composer.
* `GET /api/invoices/assignable-reviewers` is PII-free but gated
  admin/ap_manager and scoped to holders of `invoice.approve`. Mentioning is
  broader in **both** directions: an ap_clerk or CFO can post to this thread yet
  could not read that list, and a clerk is a perfectly ordinary person to
  mention.

The caller is not filtered out — the composer drops itself, and
`notify_ap_mentions` already excludes the poster from the recipients.

Before this endpoint the frontend picker had no source at all: it read a store
only `/admin` and `/workflows/[id]` populate, so on `/invoices` — the one route
the modal is reachable from — it was permanently empty, and arriving via
`/admin` first made it work. Guards: `backend/tests/test_chat_mentionable_users.py`
and `frontend/tests-e2e/invoices/chat-mentions.spec.ts` (which also asserts the
supplier portal never fetches it).

## Attachment key scheme + cross-tenant gate

Attachments are uploaded via `storage.upload_chat_file(org_id, invoice_id,
message_id, file)`, which stamps the key:

```
<org_id>/chat/<invoice_id>/<message_id>/<safe-filename>
```

The filename passes through `storage._safe_filename` (strips `..`, path
separators, leading dots, control chars). Upload validates size (`MAX_FILE_SIZE`)
and content type against `ALLOWED_CONTENT_TYPES` (PDF, PNG, JPEG, TIFF, XML) —
anything else raises and the route returns `400`.

The leading `<org_id>` segment is the **cross-tenant download gate**, but the
two surfaces enforce it at different granularities:

- **AP side** (`/invoices/{invoice_id}/chat/file/{key}` — what every AP
  `file_url` names — and the key-only `/invoices/chat/file/{key}`, any authed
  employee): `api/file_proxy` parses the invoice id out of the key, requires the
  org segment to be the caller's, and resolves that invoice **within the
  selected entity** — with subsidiary B selected, A's attachment is a 404. The
  invoice-bound route also requires the key's invoice to be the path's, so a
  visible invoice id can't launder another invoice's key (decisions §226).
- **Portal side** (`/portal/invoices/{invoice_id}/chat/file/{key}`, vendor):
  the key must start with `"<inv.organization_id>/chat/<inv.id>/"` — i.e. it must
  belong to the **ownership-checked invoice**, not merely share the tenant's
  `<org_id>` segment. An org-prefix-only check is **not** enough on the portal:
  every vendor in a tenant shares the same `<org_id>`, so a vendor could pass
  their OWN `invoice_id` in the path (ownership passes) and a `file_key` pointing
  at another vendor's invoice in the same tenant — a cross-vendor IDOR. The
  invoice-id binding closes it. Pinned by
  `tests/test_vendor_portal_isolation.py::test_chat_file_download_rejects_other_invoice_key_in_same_org`.

A wrong-org / wrong-invoice key and a missing file all return the **same 404**
so the response can't enumerate prefixes — mirroring the invoice and contract
file endpoints.

## Routes

Both surfaces reuse their existing routers — **no new `include_router`** line,
no new router file. Service logic (lazy-create, templates, notification/email
helpers) lives in `services/supplier_chat.py`.

### AP side — `api/invoices.py` (mounted at `/api/invoices`)

| Method | Path | Auth / RBAC | Body | Response |
|--------|------|-------------|------|----------|
| GET | `/invoices/{id}/chat` | any authed | — | `ChatThreadResponse` (200) |
| POST | `/invoices/{id}/chat` | any authed | `ChatMessageCreate` | `ChatMessageResponse` (201) |
| POST | `/invoices/{id}/chat/attachments` | any authed | multipart `file` (+ optional `body`, `mention_user_ids`, `template_key` form fields) | `ChatMessageResponse` (201) |
| POST | `/invoices/{id}/chat/resolve` | `admin` / `ap_manager` / `cfo` | — | `ChatThreadResponse` (200) |
| POST | `/invoices/{id}/chat/reopen` | `admin` / `ap_manager` / `cfo` | — | `ChatThreadResponse` (200) |
| GET | `/invoices/{id}/chat/file/{file_key:path}` | any authed (entity-scoped) | — | bytes (200) / 404 |
| GET | `/invoices/chat/file/{file_key:path}` | any authed (entity-scoped) | — | bytes (200) / 404 |
| GET | `/invoices/chat/templates` | any authed | — | `list[ChatTemplate]` (200) |
| GET | `/invoices/chat/mentionable-users` | any authed | — | `list[{id, full_name, is_active}]` (200) |

- **GET lazy-creates nothing** — with no thread yet it returns
  `{ "id": null, "status": "open", "messages": [] }`.
- **POST lazy-creates the thread** via `supplier_chat.get_or_create_thread`
  inside the same txn.
- **Attachments are a separate multipart endpoint** — you can't bind a JSON
  Pydantic body and an `UploadFile` in one handler. It creates a message row
  with the uploaded file in `attachments` (plus any `body`/`mentions`/
  `template_key` form fields). Both POST paths emit the same audit +
  notifications.
- The templates endpoint returns the static `supplier_chat.CHAT_TEMPLATES` list
  (the source of truth; the frontend may also hardcode them).

### Portal side — `api/portal.py` (mounted at `/api/portal`)

| Method | Path | Auth | Body | Response |
|--------|------|------|------|----------|
| GET | `/portal/invoices/{id}/chat` | `get_current_vendor_user` | — | `PortalChatThreadResponse` (200) |
| POST | `/portal/invoices/{id}/chat` | `get_current_vendor_user` | `PortalChatMessageCreate` | `PortalChatMessageResponse` (201) |
| POST | `/portal/invoices/{id}/chat/attachments` | `get_current_vendor_user` | multipart `file` (+ optional `body`) | `PortalChatMessageResponse` (201) |
| GET | `/portal/invoices/{id}/chat/file/{file_key:path}` | `get_current_vendor_user` | — | bytes (200) / 404 |

- Every query filters on `vu.vendor_id`; a not-found and a not-yours invoice
  return the **same 404** (no 403, no enumeration).
- Supplier posts record `author_role=supplier`, `author_user_id=vu.id`,
  `author_name=vu.full_name` (or the vendor display name). **No** mentions, **no**
  templates, **no** resolve/reopen on the portal.
- `PortalChatMessageResponse` **masks AP author ids** — the supplier sees
  `author_name` only, never an internal `users.id`, and never the `mentions` list.

## Schemas

| Surface | File | Request | Response |
|---------|------|---------|----------|
| AP | `schemas/invoice.py` | `ChatMessageCreate` (`body`, `mention_user_ids`, `template_key`) | `ChatMessageResponse`, `ChatThreadResponse`, `ChatAttachmentOut`, `ChatTemplate` |
| Portal | `schemas/portal.py` | `PortalChatMessageCreate` (`body`) | `PortalChatMessageResponse`, `PortalChatThreadResponse`, `PortalChatAttachmentOut` |

`body` is `min_length=1, max_length=10_000`. The two surfaces deliberately
serialize datetimes differently — AP schemas emit **ISO 8601 strings**
(matching `InvoiceResponse`), portal schemas use **raw `datetime`** (matching
the rest of `portal.py`). This is the existing convention; do not harmonize them.

## Audit trail

Every post/resolve/reopen writes an append-only `audit_log` row via
`dispatch_audit`, `entity_type="invoice"`, `entity_id=invoice.id`,
`correlation_id=invoice.correlation_id`.

| Action | Surface | `actor_id` | `details` |
|--------|---------|-----------|-----------|
| `chat_message_posted` | both | AP post: `user.id` · supplier post: `None` (a VendorUser is not a control-plane user) | `{thread_id, message_id, author_role, has_attachment, template_key}` |
| `chat_thread_resolved` | AP only | `user.id` | `{thread_id}` |
| `chat_thread_reopened` | AP only | `user.id` | `{thread_id}` |

**PII rule:** `details` carries ids, roles, and booleans only — never the
message `body`, vendor email, mention names, or filenames.

## Notifications

A new notification event type `chat_message` (`EVENT_CHAT_MESSAGE` in
`models/notification.py`) drives in-app + email fan-out via
`notification_dispatch.notify_event` (`entity_id=invoice.id`). `notify_event`
only ever reaches **control-plane Users** — a supplier (VendorUser) can never be
a `recipient_user_id`. It is gated internally by `settings.notifications_enabled`
and is called **before** `db.commit()` so the in-app rows ride the same tenant
txn.

| Trigger | Recipients |
|---------|-----------|
| **Supplier posts (portal)** — `notify_supplier_post` | every `ap_manager` in the org (`resolve_role_user_ids(org_id, "ap_manager")`); `actor_id=None` |
| **AP posts with @mentions** — `notify_ap_mentions` | the mentioned `users.id`, **excluding the posting AP user** |
| **AP posts** — `notify_supplier_of_ap_message` | the supplier, via a **direct portal-link email** (see below) |

The render branch (`notification_templates.render`, `EVENT_CHAT_MESSAGE`) emits
a PII-free title/body — `"New message on {ref}"` plus an optional short author
label from `InvoiceContext.note` (`"from supplier"` / `"you were mentioned"`).
**Never** the raw message body. See [notifications.md](notifications.md).

### Supplier email on an AP message (portal link)

When an AP user posts, `notify_supplier_of_ap_message` sends a direct,
best-effort email to `vendor.email`, modeled on
`card_issuance._send_vendor_card_email`:

- The link is built from `settings.tenant_url_template` (`{slug}` → org slug):
  `{base}/portal/invoices/{invoice.id}/chat`.
- Sent via `get_email_adapter().send(EmailMessage(...))` (`console` default
  locally — no network).
- Skipped silently when `vendor.email` is absent or there's no tenant URL
  template.
- Wrapped in try/except, logged without PII (invoice id + error only — never the
  vendor email).
- This direct path is **not** auto-gated, so it checks
  `settings.notifications_enabled` itself.

Subject/body are PII-free: invoice number + vendor/org name only, never the
message text.

## Configuration & feature flag

**No new secret, no new `FEOH_` env var.** The feature reuses
`FEOH_NOTIFICATIONS_ENABLED` (`settings.notifications_enabled`, default `true`) —
it auto-gates `notify_event`, and the supplier-email helper checks it explicitly.

The per-org feature flag is an **off-safe, local-first** read on
`Organization.settings`:

```python
(org.settings or {}).get("supplier_chat", {}).get("enabled", True)
```

Implemented as `supplier_chat.chat_enabled(org)`, following the
`invoice_warnings` / `po_matching.require_inspection` precedent. Default `True`
so the feature works out of the box on a fresh local tenant (local-first
invariant); an org can opt out. When `False`:

- AP/portal chat **POSTs return `403`** ("Supplier chat is disabled").
- **GET returns an empty thread** (`id: null`, `messages: []`).

## Templates

Static, in-code constants in `services/supplier_chat.py::CHAT_TEMPLATES` (not
config, not secrets). Three canned AP asks, surfaced via `GET
/api/invoices/chat/templates` (the source of truth):

| `key` | `label` | Use |
|-------|---------|-----|
| `missing_po` | Missing PO number | invoice can't be matched to a PO |
| `amount_mismatch` | Amount mismatch | invoice total disagrees with records |
| `payment_status` | Payment status | invoice approved & scheduled |

`supplier_chat.is_valid_template_key` validates an inbound `template_key`
against this set (`None` is allowed).

## Migration

- **0038_supplier_chat** — creates `supplier_chat_threads` +
  `supplier_chat_messages`. **Tenant DB only** (gated on the `invoices` table →
  no-ops on the control plane, fans out to every tenant via
  `scripts/migrate_all_tenants.py`). Idempotent (`CREATE TABLE/INDEX IF NOT
  EXISTS`); `downgrade` drops messages first, then threads. DDL mirrors the ORM
  models exactly so a fresh tenant built by
  `tenant_provisioning._create_tenant_tables` (`create_all`) matches a migrated
  one — including the parity pair of indexes on `invoice_id` (the plain
  `ix_..._invoice_id` from `index=True` **and** the `uq_...` unique index from
  `__table_args__`). `revision = "0038_supplier_chat"`,
  `down_revision = "0037_invoice_contract_link"`.

The models are exported from `models/__init__.py` (`SupplierChatThread`,
`SupplierChatMessage`, `ChatThreadStatus`, `ChatAuthorRole`) — load-bearing,
because `tenant_provisioning._create_tenant_tables` iterates
`Base.metadata.tables`, so a fresh tenant only gets these tables if the models
are imported there.

## Out of scope (this slice)

Real-time / websocket push (polling/refetch on open only); read receipts /
per-message unread state; editing or deleting a posted message (append-only);
supplier-side @mentions, templates, or resolve/reopen; inline image preview of
attachments on the **portal** (download-to-save only — the AP side may inline
via `api.fetchBlob`); rich text / markdown.

## Frontend

The shared, surface-agnostic `SupplierChatThread.svelte` component
(`lib/components/chat/`) renders both surfaces, driven by a `surface` prop
(`'ap'` | `'vendor'`). AP calls go through `lib/api/supplierChat.ts` (over
`api`); portal calls through `lib/portalChat.ts` (over `portalApi`). Types in
`lib/types/supplierChat.ts` (full `Chat*` for AP, masked `PortalChat*` for the
portal). Mount points: the invoice detail modal (`InvoiceModal.svelte`, AP) and
the supplier portal invoice list (`routes/portal/invoices/+page.svelte`). See
`frontend/CLAUDE.md`.

## Local-first

No new external dependency and no new `pnpm` script. The feature flag defaults
`True` and the notification path reuses the existing `console` email adapter, so
`pnpm dev` runs the whole chat feature — AP + portal threads, attachments,
mentions, templates, resolve/reopen — with no cloud credential.

## Tests

- `backend/tests/test_supplier_chat.py` — lazy thread creation, audit rows
  (body PII-free), mention notifications, role-gated resolve/reopen, attachment
  org-key + cross-tenant 404, the org flag blocking posts, the templates
  endpoint, portal vendor-scoping + AP-author-id masking, and the supplier
  portal-link email.
- `frontend/tests-e2e/invoices/supplier-chat.spec.ts` — AP modal: post a
  message (right-aligned), Activity row appears, resolve flips the pill.
- `frontend/tests-e2e/portal/supplier-chat.spec.ts` — portal: expand a row,
  post a message, AP-authored messages show name only.
