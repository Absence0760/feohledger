# Notifications

Email + in-app notifications for invoice lifecycle events. One centralized
hook, preference-gated per recipient, fully best-effort (a notification failure
never breaks a status transition or its audit row).

## Hook point

Every invoice status change funnels through
`services/workflow_engine.py::transition_invoice()`, which writes the audit row
(`dispatch_audit`) and then — keyed off the **resulting status** — calls
`notification_dispatch.notify_event()`. Because all paths that converge on a
status (payment webhook, ERP webhook, and ERP-sync all reach `paid`) go through
this one function, notifications fire once, centrally, instead of at every call
site.

The "assigned" event is the exception: assignment (`review.assign_reviewer`)
sets `assigned_to_id` without always changing status, so it calls
`notify_event(... event_type="invoice_assigned", recipient_user_ids=[reviewer])`
explicitly after its audit write.

Both the in-transition hook and the assign hook are wrapped so any failure is
logged (PII-free) and swallowed — the transition/assignment always completes.

## The outbound legs run POST-COMMIT

**Everything that talks to a third party — every email (employee and supplier
alike) and the single Slack/Teams post — runs AFTER the caller's transaction
commits**, not inside it. `notify_event` and
`vendor_notifications.notify_vendor_of_invoice_event` do their DB work
(recipient load, preference gating, the in-app `Notification` rows) inline and
then hand a closure to `services/post_commit.enqueue_post_commit`, which fires
it from SQLAlchemy's `after_commit` event.

Why it had to move: `transition_invoice` awaited the whole fan-out — one email
per recipient, serially, then a chat POST with a 10-second `httpx` timeout —
while the caller's transaction was still open. Two call sites make that
concrete: `payment_erp_sync._sync_one_payment` takes the invoice
`SELECT … FOR UPDATE` and only commits *after* the transition returns, and
`review.approve_invoice` holds `FOR UPDATE` on the `WorkflowInstance`. So a hung
Slack webhook held a row lock on a live invoice for up to ten seconds, and N
recipients multiplied the email leg linearly. Parallelising the email leg would
have shrunk N×T to T but left the lock held, so it was not the fix.

What did **not** change:

- **In-app `Notification` rows still ride the caller's commit.** They are DB
  writes and belong in the caller's transaction; only the outbound legs moved.
- **No call site changed.** All ~35 `transition_invoice` callers keep committing
  exactly as they did.
- **Still best-effort.** A job that raises is logged by exception CLASS NAME
  only (never `logger.exception` — the traceback of an `HTTPStatusError` carries
  the webhook URL, which IS the credential, and `SMTPRecipientsRefused` carries
  the addresses).

What *did* change, deliberately: **a transaction that rolls back sends
nothing.** We no longer email people about a status change that never happened.

Execution mode depends on where the commit happens. On the app's event loop the
jobs are spawned as tasks (strong-referenced until done, mirroring
`services/webhooks/dispatch`) so the request returns immediately. Under a
`database.dispatch_engine_scope` — a dispatcher worker's own short-lived loop,
which closes the moment the job returns — a spawned task would be abandoned
mid-send, so the jobs are awaited inline via SQLAlchemy's `await_only`; the
COMMIT has already happened either way, so no lock is held.

Tests asserting on an outbound send must `await drain_post_commit()` after the
commit — it is the real signal, not a sleep. Guarded by
`tests/test_post_commit_dispatch.py`, which also AST-scans both dispatch
functions so neither can regain a direct transport `await` inside the
transaction.

## Event → recipient matrix

| Event type | Trigger (resulting status / action) | Recipients |
|---|---|---|
| `invoice_assigned` | `review.assign_reviewer` | the (possibly delegated) reviewer |
| `invoice_approved` | `transition_invoice → approved` | invoice uploader (`uploaded_by_id`) |
| `invoice_rejected` | `transition_invoice → rejected` | invoice uploader |
| `invoice_paid` | `transition_invoice → paid` (payment webhook / ERP webhook / ERP-sync) | invoice uploader + every `ap_manager` in the org |
| `chat_message` | supplier posts on the portal → every `ap_manager`; AP posts with @mentions → the mentioned AP users (poster excluded) | see below |
| `ai_invoice_usage` | the org crosses 80% / 100% of its plan's monthly AI-read-invoice allowance, or its AI spending cap (`services/billing/ai_usage_notices.py`, checked after every billable read and by the `billing-ai-overage` sweep) | every org `admin` — once per threshold per UTC month, claimed under the org row lock (`billing.md` § Notices). `entity_type="billing"`, `entity_id` NULL |

`ap_manager` recipients are resolved against the control plane via
`notification_dispatch.resolve_role_user_ids(org_id, "ap_manager")`.

For `invoice_paid` and `invoice_rejected`, the same chokepoint **also** emails
the invoice's **vendor portal users** — see § Vendor recipients below.

### Vendor recipients (`invoice_paid` / `invoice_rejected`)

The supplier portal lets a vendor get **emailed** when one of *their own*
invoices is paid or rejected (supplier-portal Phase 2). This fans out from the
same `transition_invoice` chokepoint, but through a **separate** path —
`services/vendor_notifications.notify_vendor_of_invoice_event` — because the
recipients are `VendorUser`s (tenant DB), not control-plane `User`s, and
`notify_event` only ever reaches control-plane Users:

- It loads the **active** portal users of the invoice's `vendor_id` and emails
  each one whose per-user preference allows it.
- It is **independent** of the control-plane recipient resolution: a
  portal-submitted invoice usually has no `uploaded_by_id` (the actor was a
  VendorUser), so it has zero control-plane recipients yet still must reach the
  supplier. The vendor fan-out runs even when the control-plane recipient list
  is empty.
- It runs under its own best-effort guard (in addition to `notify_event`'s) so a
  failure never breaks the invoice transition or its audit row.
- Preferences are stored on `vendor_users.notification_prefs` (JSONB, migration
  0052), keyed by the **same** `invoice_paid` / `invoice_rejected` event strings
  and read with the **same** `resolve_prefs` helper (opt-out, defaults on).
  Vendors have no in-app center, so only the `email` channel is consulted.
- Emails reuse the shared **PII-free** templates (`notification_templates.render`)
  — invoice number, vendor name, amount, currency, optional rejection reason.
  Failures log the event type + the exception **class name** only, never the
  recipient address — and specifically **never `logger.exception`**, for the same
  reason the control-plane leg documents above: the traceback carries the
  exception's own text whatever the format string names, and
  `smtplib.SMTPRecipientsRefused` stringifies as
  `{'supplier@customer.com': (550, b'…')}`. The vendor send guard used
  `logger.exception` and so wrote a supplier's address into the log sink on the
  first refusal; `tests/test_vendor_notification_prefs.py::test_vendor_email_failure_never_logs_the_recipient_address`
  pins it, mirroring the control-plane guard in
  `tests/test_chat_notification_adapters.py`.

The vendor-friendly API shape (`email_on_payment` / `email_on_rejection`) and
the GET/PATCH portal endpoints are documented in
[supplier-portal.md](supplier-portal.md) § Notification preferences.

### Email approval links (`invoice_assigned` only)

When `FEOH_EMAIL_ACTION_SIGNING_KEY` is set, the **`invoice_assigned`** email gains
per-recipient **Approve / Reject** links so the reviewer can decide straight from
the email without logging in. `notify_event` resolves the tenant slug once and
calls `email_action_token.build_email_action_links` per recipient (the token
binds to that reviewer + invoice + action). The links land on the public
`GET /api/invoices/email-action/{token}` confirm page → `POST .../confirm`, which
runs the normal `services/review` approve/reject path. Empty key → no links
added. Full design + security properties: [email-approval.md](email-approval.md).

### `chat_message` (supplier chat)

The embedded supplier-chat feature fans out via `services/supplier_chat.py`
(not the `transition_invoice` chokepoint — chat posts don't change invoice
status):

- **Supplier posts (portal)** → `notify_supplier_post` notifies every
  `ap_manager` so the AP team learns of the reply (`actor_id=None`).
- **AP posts with @mentions** → `notify_ap_mentions` notifies the mentioned
  control-plane `users.id`, **excluding the posting AP user**.
- **AP posts (any)** → the *supplier* gets a **direct portal-link email**
  (`notify_supplier_of_ap_message`), not an in-app/`notify_event` notification —
  `notify_event` only ever reaches control-plane Users, never a VendorUser. The
  email carries `{base}/portal/invoices/{id}/chat`, is best-effort, PII-free
  (invoice number + vendor/org name only), and is gated on
  `notifications_enabled` explicitly.

The `chat_message` render branch emits only `"New message on {ref}"` plus an
optional short author label (`InvoiceContext.note` — `"from supplier"` /
`"you were mentioned"`). **Never** the raw message body. See
[supplier-chat.md](supplier-chat.md).

## Chat notifications (Slack / Teams)

In addition to per-recipient email + in-app, an org can fan **approval-lifecycle
events** out to a team chat channel (Slack or Microsoft Teams) via that
provider's **incoming webhook**. This is a single channel post per event (not
per-recipient) and is **entirely best-effort** — a chat-send failure never
breaks the invoice transition.

### Adapter family

`services/chat_notification_adapters/` mirrors `email_adapters/` exactly — a
decorator registry + a per-org-config-aware factory + a local-first `mock`
default:

| Provider | Body shape | Notes |
|---|---|---|
| `mock` | — | **Default.** No network, no credential. Records sends on `mock_adapter.SENT` + logs a PII-free line, so `pnpm dev` exercises the full path with no real Slack/Teams. |
| `slack` | `{"text": ..., "blocks": [section]}` | Posts to a Slack incoming-webhook URL via httpx. Interactive: Block Kit Approve/Reject buttons on `invoice_assigned`. |
| `teams` | legacy `MessageCard` (`@type`/`sections.facts`/`potentialAction`) | Posts to a Teams incoming-webhook URL via httpx. Interactive: `HttpPOST` Approve/Reject actions on `invoice_assigned`. |

`get_chat_notification_adapter(org_config)` resolves the provider from
`org_config["provider"]` → `FEOH_CHAT_NOTIFICATION_PROVIDER` (default `mock`); an
unknown key falls back to `mock` and never raises.
`render_chat_message(event_type, ...)` builds the PII-free `ChatMessage`
(returns `None` for non-approval events like `chat_message` /
`contract_renewal_due`, so they're skipped).

### Per-org configuration

`Organization.settings.chat_notifications` (JSONB — no migration, mirroring how
`notifications` / `residency` carry config in settings-JSON):

```json
{
  "enabled": true,
  "provider": "slack",
  "webhook_url": "https://hooks.slack.com/services/...",
  "events": { "invoice_assigned": true, "invoice_approved": true,
              "invoice_rejected": true, "invoice_paid": false }
}
```

- `enabled` is the per-org master gate and defaults **off** — chat is opt-in per
  org (unlike email/in-app, which default on). When off, no adapter is built.
- `events` is opt-out *within* an enabled org: a missing event key defaults on.
- `webhook_url` is the per-org credential, carried in settings (it's a
  channel-scoped URL, not a platform secret). The `slack` / `teams` adapters
  **fail closed** when it's absent: a no-op + a PII-free warning, never an
  exception. There is **no hardcoded fallback** webhook URL.

The shape rules for this block — it is write-only, a config save preserves it,
and nothing derived from it may be logged beyond its hostname — live in the pure
`services/chat_notifications_config`, which is its single owner. Don't reach
into the raw dict from a new caller; go through `safe_status` / `apply_config` /
`apply_webhook_url`.

### Managing it — `/api/organization/chat-notifications`

Admin-only, JWT-gated, every mutation audited (`api/chat_notifications.py`,
mounted under `/api/organization/...` the same way `api/email_intake.py` hangs
its admin surface there):

| Endpoint | What it does |
|---|---|
| `GET /api/organization/chat-notifications` | `enabled` / `provider` / `events`, plus `webhook_configured` + `webhook_host` and the registry-derived `supported_providers` / `supported_events`. **Never the URL.** |
| `PUT /api/organization/chat-notifications` | Update the non-credential settings. Validates the provider against the live adapter registry and every event key against `CHAT_EVENT_TYPES` (422 on an unknown one — a typo would otherwise persist as a toggle that reads as configured and does nothing). **Preserves `webhook_url`.** Audits `organization.chat_notifications_updated`. |
| `PUT /api/organization/chat-notifications/webhook` | Set or replace the credential. Audits `organization.chat_webhook_rotated`. |
| `DELETE /api/organization/chat-notifications/webhook` | Revoke it. Idempotent. Same audit action, flagged `removed: true`. |

The read is admin-only too — unlike branding / data-residency, nothing in the
app renders from this, and the response carries the webhook's hostname, which is
closer to the credential than any non-admin role needs.

### Rotating the webhook URL

The URL is the credential for both real providers: whoever holds the string can
post arbitrary content into the customer's approval channel forever, with no
authentication — a phishing surface aimed squarely at the people who approve
payments. Three properties make that recoverable, and each is deliberate:

- **Write-only, system-wide.** No endpoint returns the stored URL — not the
  ones above, and not `GET /api/organization`, which serves the raw settings
  JSONB and used to hand the credential to every authenticated role. That
  endpoint's projection (`services/org_settings_view`) drops
  `chat_notifications.webhook_url` for **every** role, admin included, and
  `PATCH /api/organization` refuses a `chat_notifications` key outright so the
  audited endpoint is the only writer. Reads report whether one
  is set plus its bare **hostname** — the token lives in the path (Slack
  `/services/T…/B…/<token>`, Teams `/webhookb2/<guid>@<guid>/…`), so the
  hostname answers "where does our approval channel post?" during an incident
  without handing back the capability. That is also the only thing that reaches
  the audit trail, which is shipped to CloudWatch and an S3 Object Lock WORM
  bucket — a credential written there would be replicated *and* undeletable.
- **No overlap window**, unlike `POST /api/webhooks/{id}/rotate-secret`. That
  rotates an HMAC *signing secret* — a verifier held by a counterparty, so an
  overlap lets the receiver switch keys with no dropped deliveries. This is a
  *destination*: we POST to exactly one URL, nobody else holds the old value,
  and keeping it live would mean posting every approval event into the
  compromised channel too. The overlap would extend the leak, not smooth a
  cutover. The replacement is atomic, and there is no `previous_*` slot to
  leave a retired URL receiving.
- **Nothing is "shown once."** We don't mint this value; the customer creates
  the incoming webhook at Slack / Teams and pastes it, so they already hold it.
  There is no one-time reveal to design around.
- **Revoking the old URL at the provider is the customer's step** and we can't
  do it for them. Until they delete it there, the old URL still works no matter
  what we store. The operator procedure is in
  [../../docs/secrets-rotation.md](../../docs/secrets-rotation.md)
  § Per-tenant chat-notification webhook URL.

The write path runs the **same** `is_public_url` SSRF rule the adapters apply at
send time, so a URL that saves is a URL that will actually post — a write-path
guard with different rules would let an admin store a target that quietly never
fires. Every refusal answers with one generic, value-free 422: FastAPI's default
validation body echoes the offending `input`, so the URL field carries no
Pydantic constraints and all checks run in the handler instead.

### Wiring

`notification_dispatch.notify_event` dispatches chat **after** the per-recipient
email/in-app loop, once per event, for the four approval events email already
handles (`invoice_assigned` / `invoice_approved` / `invoice_rejected` /
`invoice_paid`). `_send_chat_best_effort` loads the org's
`settings.chat_notifications`, checks the enable + per-event gate, renders the
message, builds the adapter, and sends — the whole thing wrapped in its own
try/except so any failure (config load, adapter build, transport) is swallowed
and logged PII-free. The deep link is `tenant_base_url(slug, org_settings)` +
invoice id (no secrets) — see § Where a tenant's links point below.

### Interactive approval actions

On `invoice_assigned` only, the channel post can carry **Approve / Reject**
actions that decide the invoice without an app login.
`_build_chat_action_tokens` dispatches on the org's chat provider and mints the
pair of signed, single-use action tokens on that provider's **own** channel
(`slack` / `teams`), bound to the single intended approver — so a Slack token can
never be redeemed at the Teams endpoint or vice versa, and a provider with no
interactive surface (`mock`, or an unknown key) mints nothing. Tokens are only
minted when `FEOH_EMAIL_ACTION_SIGNING_KEY` is set and the event resolves to
exactly one approver; the adapter then decides whether it can render a *usable*
action (Teams additionally needs `FEOH_TEAMS_SECURITY_TOKEN` +
`FEOH_API_PUBLIC_URL` to sign and target the callback). Every rung falls back to
a plain read-only post rather than a button that can't work.

The decision runs the normal `services/review` path — segregation of duties, the
CFO gate, thresholds, the immutable audit row, the approval signature. Full
mechanics: `slack-approval.md` and `teams-approval.md`.

### PII

The chat message carries **only** invoice number, vendor name, amount + currency
(rendered exactly from the `Decimal`), a human status word, and an optional deep
link — **never** bank details, tax IDs, full addresses, or payment-method
numbers. Failure logs record the event type only, never the webhook URL or the
amount.

## Two effects per recipient, each preference-gated

### `notify_event` returns what it actioned

`notify_event` returns the **number of recipients something was actioned for** —
an in-app row added, or an email queued. Most callers ignore it: for a status
transition, "nobody had this event turned on" is a normal outcome, and the
dispatcher must never raise into a transition either way.

It exists for the two callers that write a **suppress-forever marker** right
afterwards — `cash_flow_alerts`'s alerted-period marker and
`contract_renewal`'s `renewal_alert_sent_at` (only
`POST /api/contracts/{id}/renew` ever clears it). Both already skipped their
marker when they resolved zero recipients, which covers one of the ways this
reaches nobody. Every other silent exit — the master
`FEOH_NOTIFICATIONS_ENABLED` switch off, an unknown event type, a template
render that raised, the recipient load failing, every resolved recipient
inactive or opted out — returned `None` indistinguishably from success, so the
marker went down and the finance leaders were never warned about that projected
shortfall period, or that contract's renewal, for the rest of its life. Nothing
counted as a failure, so `GET /api/health/sweeps` stayed green too.

A caller that suppresses future attempts must test the count. Guards:
`tests/test_notify_event_outcome.py`,
`tests/test_cash_flow_alerts.py::test_sweep_leaves_the_marker_unwritten_when_notifications_are_disabled`,
`tests/test_contracts.py::test_renewal_alert_not_stamped_when_dispatch_reached_nobody`.

For each recipient, `notify_event`:

1. **in-app** — inserts a `Notification` row into the **tenant** DB on the
   caller's session (commits atomically with the status change), *if* the
   recipient's `in_app` preference is on for that event.
2. **email** — builds an `EmailMessage` and hands it to the configured email
   adapter (`get_email_adapter()` — `console` default), *if* the recipient's
   `email` preference is on.

Recipients are de-duplicated, so a user who is both uploader and AP manager
gets one notification.

**Recipient resolution is org-scoped at the data layer.** `users` is a
control-plane table, so `_load_recipients` takes the dispatching
`organization_id` and filters on it — an id belonging to another tenant simply
does not resolve, and the recipient loop's documented "wrong org — skip
silently" branch is therefore true rather than aspirational. This is the
backstop for a caller that fails to scope its own lookup (the invoice-assign
route did, and stamped a foreign tenant's user onto `Invoice.assigned_to_id`);
without it, the `invoice_assigned` email — invoice number, vendor name and
amount — would have been addressed outside the tenant it belongs to.

## Preferences

User-global, stored on the control-plane `users.notification_prefs` JSONB:

```json
{
  "invoice_assigned":         { "email": true, "in_app": true },
  "invoice_approved":         { "email": true, "in_app": true },
  "invoice_rejected":         { "email": true, "in_app": true },
  "invoice_paid":             { "email": true, "in_app": true },
  "contract_renewal_due":     { "email": true, "in_app": true },
  "chat_message":             { "email": true, "in_app": true },
  "cash_shortfall_projected": { "email": true, "in_app": true },
  "ai_invoice_usage":         { "email": true, "in_app": true }
}
```

Opt-out, not opt-in: a missing event or missing channel key defaults to **on**
(`resolve_prefs`). The `/api/notifications/preferences` endpoints read/patch
this blob; the `/profile` page renders the per-event toggle grid.

### The prefs schema must cover the WHOLE roster

`schemas/notification.py` enumerated only the four `invoice_*` events while
`models/notification.NOTIFICATION_EVENT_TYPES` declared seven. Because
`resolve_prefs` defaults an unknown key to **on**, the three missing events
weren't merely undocumented — they were **unmutable**: every supplier-chat
message emailed the AP team with no opt-out, and the same held for
`contract_renewal_due` and `cash_shortfall_projected`.

Both `NotificationPrefs` and `NotificationPrefsUpdate` now enumerate all seven
(a key present in the read schema but absent from the update schema would be
readable and unchangeable). `tests/test_notification_prefs_roster.py` derives
the expected population from the model's roster, so adding an event type fails
that file until it joins both schemas — there is no second list to remember.

## Templates & PII

`services/notification_templates.py::render(event_type, InvoiceContext, *, locale=None)`
returns `(title, body_text, body_html)`. Templates reference **only** invoice
number, vendor name, amount (rendered exactly from the `Decimal`), currency, and
an optional rejection reason — never bank details, tax IDs, full addresses, or
payment-method numbers. Email-send failures log the event type + nothing else
(never the recipient address). The copy strings live in the per-locale **email
catalogue** (`render` pulls them keyed by `notif.<event>.{title,body}` — see §
Localized email); `locale=None` is English, which the **in-app** notification
center always uses (the locale pref drives email only).

## Localized email

Outbound transactional email renders server-side, so the recipient's language is
carried by a DB-synced, account-level `locale` preference — "what language to
email this person in". It is **separate** from the frontend's per-device UI
locale picker (`frontend/src/lib/i18n/`): the DB pref is written from the UI and
read by the **email-render path only**, and is **never** returned to drive in-app
UI.

**Where the pref lives** (nullable; NULL → English fallback):

- `User.locale` — control plane (AP employees). Set via `PATCH /api/auth/me`
  (`{"locale": "<loc>"}` — supported value sets it, `""` clears it → English, an
  unknown value 422s). Surfaced on `GET/PATCH /api/auth/me` (`UserResponse.locale`).
- `VendorUser.locale` — tenant-scoped (supplier-portal users). Set via
  `PATCH /api/portal/auth/me`, same validation. Surfaced on the portal me-route.

Both added by migration **`0059_email_locale_pref`** — a single, existence-guarded
revision that runs on BOTH the control DB (adds `users.locale`) and every tenant
DB (adds `vendor_users.locale`); each `ADD COLUMN IF NOT EXISTS` is gated on its
table existing, so the same revision is safe on both. **Fan-out**: control DB via
`alembic upgrade head`; every existing tenant via
`python scripts/migrate_all_tenants.py` (or `FEOH_MIGRATE_TENANT=feoh_<slug> alembic
upgrade head`); fresh tenants get the column from `create_all` in
`tenant_provisioning` (the model field). Nullable + reversible.

**The catalogue** — `app/services/email_adapters/email_catalogue.py`:

- `SUPPORTED_EMAIL_LOCALES = (en, de, fr, es, pt-BR, ja)` — the same six as
  web/mobile. `DEFAULT_LOCALE = "en"`.
- `normalize_locale(locale)` — coerces any value (case / `_`-vs-`-` / base
  language like `pt` → `pt-BR`, `de-AT` → `de`) to a supported locale; unknown /
  `None` → `"en"`. Never raises. Used at the **render** path.
- `is_supported_locale(locale)` — STRICT exact-match (no coercion). Used at the
  **write** path so a stored preference is always a canonical locale.
- `translate(key, locale, /, **params)` — per-key resolution: requested locale →
  English → raw key (so a missing key is visible, never empty / a crash).
  `{placeholder}` tokens are filled from `params`; a missing param leaves the
  literal token rather than raising. **English is the always-present fallback** —
  a non-English catalogue may translate a subset; any untranslated key resolves
  to the English string.

**Only copy is localized.** Deep links, brand chrome (the adapter's
`apply_brand` header/footer), money amounts, invoice numbers, and vendor names
are interpolated as `{placeholder}` tokens — a translation can reorder them but
can't drop or distort them (the parity test asserts the placeholder set matches
English exactly per key).

**Surfaces covered:**

| Surface | Locale source |
|---|---|
| Invoice notifications → employees (`notification_dispatch.notify_event`) | per-recipient `User.locale`; re-rendered per email recipient (the in-app row stays English) |
| Invoice notifications → suppliers (`vendor_notifications.notify_vendor_of_invoice_event`) | per-recipient `VendorUser.locale`; rendered per portal user |
| Signup verification + welcome emails (`api/signup.py`) | optional `locale` on `POST /api/signup/start`, normalized + stashed in `EmailVerification.meta`, reused by the welcome email at `/complete` |
| Supplier-chat portal-link email (`supplier_chat.notify_supplier_of_ap_message`) | catalogue-routed, English default — the recipient is the `Vendor` contact address, not an identified `VendorUser`, so there's no per-user locale to read |

**Deferred (frontend track):** the language-picker → backend write. The backend
endpoints + persistence are in place; the frontend profile/portal picker calling
`PATCH .../me` with the chosen locale is owned by the frontend i18n track.

## In-app data model

Tenant table `notifications` (`models/notification.py`):

- `id`, `correlation_id` (carries the invoice's, for audit-shipping
  correlation), `organization_id`
- `recipient_user_id` (control-plane `users.id`, no FK — cross-DB), indexed
- `event_type`, `entity_type` (default `invoice`), `entity_id`
- `title`, `body`, `read_at` (NULL = unread)
- indexes: `(recipient_user_id)` and `(recipient_user_id, read_at)` for the
  unread-count query

## API

All endpoints require auth and are tenant-scoped to `recipient_user_id ==
current_user.id` (preferences via the control plane). Mark-read on another
user's row returns the same 404 as a missing row — no enumeration.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/notifications?unread_only=&page=&page_size=` | paginated list; envelope includes `unread` |
| GET | `/api/notifications/unread-count` | `{unread}` for the badge |
| POST | `/api/notifications/{id}/read` | mark one read (naturally idempotent) |
| POST | `/api/notifications/read-all` | `{updated}` |
| GET | `/api/notifications/preferences` | current prefs (defaults if unset) |
| PATCH | `/api/notifications/preferences` | partial update; unspecified events unchanged |
| POST | `/api/notifications/device-token` | register/re-register this device's push token — see § Push device tokens |

## Push device tokens

`POST /api/notifications/device-token` (`{token, platform: "ios"|"android"}`)
lets the mobile app register its current Firebase Cloud Messaging token.
**Registration only** — there is no server-side push-SENDING adapter in this
codebase yet (no Firebase Admin SDK integration), so this just persists the
token for whenever that's built.

Stored on the control-plane `User.device_tokens` JSONB column (migration
`0078_user_device_tokens`) — deliberately a **separate column** from
`notification_prefs`: a device token is per-device registration state that
rotates and gets replaced, not a per-event delivery preference, and the two
have unrelated shapes. Device tokens are per-employee-user, not tenant
business data, so — like `notification_prefs` — they live on the
control-plane `users` table, never fanned out to tenant DBs. Shape:
`{"ios": {"token": "...", "updated_at": "<iso8601>"}, "android": {...}}`.

**Data-subject rights**: a push token is an identifier of the employee's own
phone, so a `user` DSAR export lists each registration (`push_devices`:
platform + registration time, token withheld) and erasure empties the column
(`backend/docs/privacy.md`).

**Upsert semantics**: at most one token per platform per account. A fresh
registration for a platform replaces whatever was stored — FCM tokens rotate
(reinstall, OS-level refresh), and a stale, unregistered token should be
overwritten rather than accumulated.

Mobile calls this from `PushService` (`mobile/lib/services/push_service.dart`)
both on initial token acquisition and on every `onTokenRefresh` fire; the call
is best-effort (a failure — e.g. not yet authenticated at cold-start, since
push init runs before session restore — is logged and swallowed, never blocks
push setup). See `mobile/CLAUDE.md`.

## Where a tenant's links point

Every outbound link this backend builds for a tenant — the Slack/Teams approval
deep link above, the email-approval link, the signup welcome email, the admin
"you've been invited" email, the supplier-portal invite, the password-reset
link, the virtual-card reveal link, the supplier-chat portal link — resolves its
base URL through **one** function:

```python
from app.utils.tenant_urls import tenant_base_url

base = tenant_base_url(org.slug, org.settings)   # "" when nothing is configured
link = f"{base}/invoices/{invoice.id}"           # base never has a trailing slash
```

`tenant_base_url` reads two sources, in order:

1. **the per-org override** `Organization.settings.brand.tenant_url_template`,
   managed by `GET`/`PUT /api/organization/branding` (field
   `tenant_url_template`, nullable string; GET readable by any authed user,
   admin-only mutate, audited on the existing PII-free
   `organization.branding_updated` row as the boolean `tenant_url_template_set`
   — the URL itself is never written to the trail); then
2. **the global** `FEOH_TENANT_URL_TEMPLATE`.

`{slug}` is **optional in the per-org value** and structural in the global one:
a vanity host is a complete base URL (`https://ap.acmecorp.com`) with no slug
label in it, while the platform template is slug-shaped
(`https://{slug}.app.example.com`). Both go through the same rule — substitute
the placeholder if it is present, use the value verbatim if it is not — and the
result is returned with any trailing slash stripped, because every call site
appends a path.

Why it exists: before this, each of those call sites substituted `{slug}` into
the global template itself, so a tenant reachable at its own vanity hostname
still got every invite, reset and approval link pointing back at
`<slug>.<platform-domain>`. That works, and it undoes the white-label the custom
domain was bought for.

`""` is a real answer, not an error: an operator may deliberately blank
`FEOH_TENANT_URL_TEMPLATE`, and every caller omits the URL line rather than
fabricating a `localhost` link into a customer's inbox.

The persisted override is re-validated on **read** as well as on write (same
http(s) shape rule the sibling brand URLs use, in
`app/schemas/organization.looks_like_http_url`): a row edited straight in the
database has never been through the API, and this value ends up in an outbound
email body.

**Two deliberate non-callers**, both asserted by
`tests/test_tenant_url_resolver.py`:

- `app/services/sso.py` — the OIDC `redirect_uri` and the SAML bridge URL are
  values *registered with the customer's IdP*. Silently re-pointing them at a
  vanity host breaks every SSO login until the operator re-registers the app at
  the IdP, so that move is an operator-sequenced migration, not a config read.
- `app/main.py` — the public signup-config endpoint hands the raw global
  template to the unauthenticated signup page, which is showing the shape of a
  hostname for a tenant that does not exist yet. There is no org to override
  from.

### The platform domain, and why a custom domain can't sit under it

`app/utils/tenant_urls.platform_domain()` derives the platform's own registrable
host from `FEOH_TENANT_URL_TEMPLATE` — `https://{slug}.app.example.com` →
`app.example.com`. That is the one place the platform's hostname shape is
already declared, so no new env var was added for it.

`PUT /api/organization/branding/custom-domains` refuses (422) any host that IS
the platform domain or sits under it. Such a host is already routed by the
`<slug>.<platform-domain>` subdomain path, so registering it as a custom domain
adds no reachability — it hands the custom-domain resolver and the subdomain
resolver a conflicting claim on one hostname, and lets one tenant claim a name
another tenant's slug owns (or takes at the next signup). The refusal echoes no
hostname, matching the endpoint's existing PII-free posture.

## Configuration & local-first

- `FEOH_NOTIFICATIONS_ENABLED` (default `true`) — master kill switch. When off,
  both hooks skip dispatch entirely.
- No new external dependency: outbound email reuses the existing adapter stack
  (`FEOH_EMAIL_PROVIDER`, `console` default — no network, no secrets). Mailpit
  (`pnpm mail:up`) previews SMTP locally. The in-app center is pure Postgres.
- `FEOH_TENANT_URL_TEMPLATE` (default `http://{slug}.localhost:7777`) — the
  platform-wide tenant URL shape, and the source the platform domain is derived
  from. It is now a **fallback**: a tenant that sets
  `settings.brand.tenant_url_template` (via `PUT /api/organization/branding`)
  overrides it for every outbound link. See § Where a tenant's links point.
- `FEOH_CHAT_NOTIFICATION_PROVIDER` (default `mock`) — platform-default chat
  adapter. Per-org override + webhook URL + per-event toggles live on
  `Organization.settings.chat_notifications` (see § Chat notifications above).
  The `mock` default needs no Slack/Teams credential, so `pnpm dev` runs
  unchanged.

## Tests

- `tests/test_tenant_url_resolver.py` — the one tenant-base-URL resolver:
  per-org override wins, global fallback, `{slug}` optional in the override
  (substituted when present, verbatim when not), trailing-slash normalisation,
  unusable/malformed persisted values falling back rather than being emitted,
  the platform-domain derivation + subdomain guard, and an AST **drift guard**
  failing if any module under `app/` reads `settings.tenant_url_template` or
  substitutes `{slug}` inline instead of calling the resolver (two documented
  exemptions: `main.py`, `services/sso.py`). Pure — no DB.
- `tests/test_branding_tenant_url_template.py` — the management surface: GET
  exposes the field and defaults empty, PUT is admin-only, persists + audits
  PII-free + drives the resolver, `null` clears it, invalid URLs 422,
  `custom_domains` survives a branding PUT, and the custom-domains
  platform-domain guard rejects while a legitimate vanity host passes (real DB).
- `tests/test_notification_templates.py` — content + PII safety (pure).
- `tests/test_email_catalogue.py` — email-catalogue parity (every locale resolves
  every key, no empty strings, placeholder-faithful vs English), English fallback
  on a missing key / unknown locale, `normalize_locale` / `is_supported_locale`,
  and `render(locale=…)` producing non-English copy while keeping the
  number/vendor/exact-money identical + PII-free (all pure).
- `tests/test_locale_pref_endpoints.py` — set-locale endpoints (employee
  `PATCH /api/auth/me` + supplier `PATCH /api/portal/auth/me`): persists a valid
  locale, rejects unknown (422), clears on `""`, omitting leaves it untouched,
  auth enforced; per-recipient persistence to the right DB (real DB).
- `tests/test_notification_dispatch.py` — recipient mapping per event,
  preference gating (in-app/email suppression), email-failure isolation (the
  transition + audit row survive), kill switch (real DB).
- `tests/test_notifications.py` — router: per-user scoping, cross-user/tenant
  isolation, mark-read 404, read-all, pagination shape, prefs round-trip, and
  the device-token register/upsert-per-platform round-trip + platform
  validation (real DB).
- `tests/test_chat_notification_adapters.py` — chat fan-out: mock default +
  unknown-key fallback, per-org provider override, Slack vs Teams body shaping
  (httpx mocked, no network), fail-closed when no webhook URL, PII absent from
  the rendered message, and `_send_chat_best_effort` swallowing a send failure /
  honouring the enable + per-event gate (all pure / mocked — no DB).
- `tests/test_chat_notifications_config.py` — the pure settings-block owner:
  `safe_status` structurally cannot emit the URL, `webhook_host` returns the
  hostname and never the token-bearing path/query, `apply_config` preserves the
  credential, `apply_webhook_url` replaces atomically with no overlap slot and
  removes the key rather than storing `""` (no DB, no network).
- `tests/test_chat_notifications_admin.py` — the admin surface: admin-only on
  every verb *including* the read, the URL absent from every response body and
  from every refusal body, config-save preserves the credential, rotate replaces
  atomically, delete revokes idempotently without disturbing the rest of the
  config, both webhook verbs audit hostnames only, the SSRF gate matches the
  sender's, and tenant isolation (real DB).
- `tests/test_vendor_notification_prefs.py` — vendor prefs: pure mapping
  (`prefs_to_response` / `apply_pref_update`), the GET/PATCH portal endpoints
  (vendor-scoped, auth enforced, audited, caller-only), and the dispatch
  substance — paid emails the vendor when on, rejected suppressed when off,
  inactive/other-vendor users skipped, failing adapter never breaks the
  transition (real DB).
- `frontend/tests-e2e/notifications/` — center badge/list, mark read, unread
  filter + empty state, preference persistence.
