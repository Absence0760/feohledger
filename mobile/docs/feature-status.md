# Feature status (mobile vs web)

Which surfaces the Flutter app ships, which are web-only, and what is
deliberately out of scope. Extracted from `mobile/CLAUDE.md` to keep that
file cheap to load.

Parity direction is set in `frontend/CLAUDE.md` § Web vs Mobile feature parity.


**Done:**
- Login with tenant selection
- Dashboard (KPIs, aging buckets, top vendors). The upcoming-payments total
  (`DashboardData.upcoming.totalAmount`) reads the backend's server-computed
  `upcoming_total_amount` field directly — it is never folded from the
  `upcoming_payments` list on-device, mirroring the payment-queue and
  cash-flow "server-supplied total, never client float math" invariant.
  **Every reporting-currency rollup it renders discloses its
  `unconverted_count`** — invoices with no rate into the reporting currency,
  added at FACE value rather than dropped — beside the figure it touches, via
  the shared `PartialConversionNote`: one line per money KPI
  (`reporting.unconverted_count` for Total Invoices,
  `upcoming_unconverted_count` for Upcoming), one for the aging band set
  (`aging_reporting.unconverted_count`, a single count by the API's design),
  and one for the top-five vendor ranking (the sum over the rows it renders)
  with each affected row's subtitle naming its own count. Nothing renders at
  zero. The monthly trend's per-bar count is not read, because mobile draws no
  trend
- Invoice list with search + status filter chips
- Advanced search — `AdvancedSearchSheet` (app-bar `tune` action; a dot badge marks an active advanced filter) filters the list by vendor, PO number, amount range and due-date range via `InvoiceStore.setFilters` → `GET /api/invoices` (`vendor` / `po_number` / `amount_min` / `amount_max` / `due_date_from` / `due_date_to`). Seeded from the live filters; validates min ≤ max + plain-decimal amounts; Apply / Clear / dismiss. The advanced filters compose with the quick status chips + search box (all carried into the same request + offline cache key)
- Invoice detail with approve/reject
- Invoice warnings / fraud flags + PO match — `InvoiceWarningsPanel` on the detail screen renders `Invoice.warnings` (`{type, severity, message}`, severity-coloured to WCAG AA) and the `po_match` panel (match type, status, variance %, issues). Parity with the web invoice modal; nothing renders when there are no warnings and no PO
- ERP status — `ErpStatusPanel` shows the invoice's ERP integration status (ERP reference / document id / send error + last action). `ErpInfo.fromAuditLog` derives it from the already-loaded audit log (latest `invoice.erp_*` / `invoice.completed` entry), so no extra request. Shown for ERP-bound statuses (`sending_to_erp` / `sent_to_erp` / `posted_in_erp`) and ERP-failed invoices
- Invoice editing — edit-sheet on the detail screen (vendor, invoice #, amount, PO, GL account, description, due date) via `PATCH /api/invoices/{id}`; amount sent as string-Decimal (never a lossy float); input validation; RBAC-gated (admin/ap_manager/cfo, hidden for clerks) and hidden in immutable statuses (the backend would 409); save success/failure announced via `A11y.announce`, and a refused save shows the **server's own sentence** (`InvoiceStore.update` records `describeApiError`) rather than a generic "try again" — the GL field is free text, so a code outside the invoice's chart (`docs/decisions.md` §194/§199) is refused with a sentence naming the code and the reason, and retrying the same save cannot succeed. **The financial freeze is mirrored client-side**: once an invoice is `approved` or later (`InvoiceStatus.isFinanciallyLocked` ⇄ the backend `_FINANCIALLY_LOCKED_STATUSES` = `{approved}` ∪ `IMMUTABLE_STATUSES`), the money + payee fields render **read-only** under a short notice saying how to change them (reject → correct → re-approve), and `stripFinancialFields` drops every key in `kFinancialInvoiceFields` (the mirror of the backend `_FINANCIAL_FIELDS`: amount, currency, subtotal, tax_amount, discount_amount, shipping_amount, tax_rate, vendor, vendor_name, remit_to_address) from the PATCH diff. The backend 409s the WHOLE request if one slips through, so a combined description + amount edit used to lose the description too — same guard the web modal applies in `invoiceFieldPayload()`. Locked by `test/widgets/invoice_edit_sheet_test.dart` + the `financial freeze` group in `test/models/invoice_test.dart`
- Activity timeline — invoice audit log on the detail screen (`GET /api/invoices/{id}/audit-log`): action label, actor, timestamp, per-field before→after diff from `details.changes`; loading / empty / error states; one merged Semantics announcement per entry
- Approvals tab with swipe-to-approve
- Exception queue (list + status filter + resolve / escalate / dismiss via swipe; admin / AP manager only). **Detail / assign / bulk-resolve** now shipped: tapping a row opens `ExceptionDetailScreen` (`GET /api/exceptions/{id}`) — full fields + linked invoice + SLA/due/overdue + current assignee, with resolve/escalate/dismiss reachable there and loading/error/empty states. An admin-gated assignee picker (`POST /api/exceptions/{id}/assign`, null = unassign) reuses the admin-only `/admin/users` list — `ap_manager` can act but doesn't get the picker (no org-user-list access); reassignment patches the row in place. Multi-select (long-press or the checklist app-bar action) drives the shared `BulkActionBar` (Status → resolve, Delete → dismiss) → `POST /api/exceptions/bulk/resolve`, whose `{updated, skipped:[{id,reason}]}` partial-success result is surfaced in a snackbar. The bottom-sheet picker is height-capped (60% of the viewport) so a long user list scrolls inside the sheet
- In-app notification center — `NotificationsScreen` + `NotificationStore` over `GET /api/notifications` (+ `unread-count` / `{id}/read` / `read-all`). Reached from the `NotificationBell` app-bar action (live unread `Badge`) in the Dashboard app bar (all roles). All / Unread filter chips; tapping a row marks it read (optimistic — flips the row + decrements the badge instantly, reconciles via refetch on failure) and deep-links to the invoice detail when the row is an `invoice` with an `entity_id` (other entity types e.g. `contract` just mark read — no mobile detail yet); mark-all-read app-bar action shown only while something is unread; offline-cached list + empty / loading / error (Retry) states. The email/in-app backend (Priority 8) serves mobile with no new endpoints
- Contract management (CLM) — `ContractsScreen` + `ContractStore` over
  `GET /api/contracts` with status filter chips + debounced search; tapping a
  row opens `ContractDetailScreen` (`GET /api/contracts/{id}`) with the terms /
  dates / value fields, the spend-to-contract summary (invoiced vs
  not-to-exceed, over-limit + remaining) and the line items. **Activate** and
  **terminate** are confirm-then-act lifecycle actions gated to
  admin/ap_manager (`AuthStore.canApprove`, mirroring the backend mutate gate)
  and hidden once the contract is no longer actionable; success / failure is
  toasted and live-region announced. Offline-cached list; loading / empty /
  error states. The web-only remainder is document upload + the file
  repository, renewal, and contract-based PO creation
- Payment history list
- Predictive cash-flow forecast (CFO/admin) — `CashFlowScreen` + `CashFlowStore` combine `GET /api/analytics/cashflow_forecast` + `GET /api/analytics/cash_position` (same `horizon_days` + `granularity` so the two legs line up). A KPI summary (opening balance + its source, projected end balance — red when a breach is projected, total committed vs pending outflow over the horizon), a low-balance alert banner when the cash position breaches the org's persisted threshold (names the worst period + shortfall), a per-period forecast list (scheduled / committed / pending + invoice count) and a running cash-position list (period closing balance, breached rows flagged red). 30 / 60 / 90-day horizon chips (`CashFlowStore.setHorizon`), pull-to-refresh, loading / error (Retry) / empty states. Reached from the `CashFlowButton` Dashboard app-bar action (CFO/admin only). **Money is rendered from server-supplied display strings — the device never does float arithmetic on currency** (every total, opening/closing balance and shortfall is server-computed; mirrors the payment-queue invariant). Both legs' `unconverted_count` — commitments projected at FACE value for want of a rate into the reporting currency — is disclosed in its own section: `totals.unconverted_count` above the forecast rows (naming the committed / pending KPI totals it also touches), `cash_position.unconverted_count` above the running balance (naming the projected-end KPI, which is its last period); nothing renders at zero. Not offline-cached (privileged, fast-moving CFO read)
- Vendor management — `VendorsScreen` + `VendorStore` over `GET /api/vendors` with status filters + search; verify / reject an unverified vendor via swipe (verify ⟶ / reject ⟵) or the action sheet, and an ERP-sync app-bar action (`POST /api/vendors/sync-erp`). Read is admin/ap_manager/cfo; the mutating actions are gated to admin/ap_manager (mirrors `require_roles`) and simply hidden for CFO. Offline-cached list
- Payment queue + runs — `PaymentQueueScreen` + `PaymentQueueStore`. Queue tab lists approved invoices (`GET /api/payments/queue`), each row a checkbox + per-row method picker; the selection creates a draft run (`POST /api/payments/runs`). A row the backend marks `blocked` (an unresolved payment-blocking exception, or fully covered by applied credit memos) is rendered but **unselectable** — its checkbox is disabled, and the localized reason is both shown on the row and carried in its merged screen-reader announcement — because one such invoice 409s the whole draft run, taking every other invoice in it down with no way to bisect. A row carrying `required_method` (a live virtual card already claims it, and `virtual_card` converges onto that card rather than opening a second outflow) is NOT blocked: it stays selectable but is **pinned** to that rail — no method picker, and the pin wins over any stored operator pick at send time. `PaymentQueueItem.isSelectable` is the single predicate; the guard is enforced in `PaymentQueueStore.toggleSelection` / `setMethod` and re-applied by `_reconcileSelection` on every fetch (live and offline-cache), not just on the checkbox. `blocked_reason` is a stable PII-free CODE, never the exception's description, so it renders only through the screen's own label map. A pin naming a rail this build cannot resolve makes the row unselectable rather than defaulting to ACH — the same fail-closed direction the backend takes when it can no longer name one converging rail. Runs tab lists runs (`GET /api/payments/runs/`) and executes / cancels drafts. A KPI summary bar (total paid / pending / queue / card rebates) sits above both (`GET /api/payments/summary`). CFO-approval-required runs surface the gate before an execute attempt — **and a CFO can now clear it from the phone**: the run popup carries an "Approve as CFO" item
  (`POST /api/payments/runs/{id}/approve`) whenever the run is a draft that
  still needs sign-off. It is gated on the **strict `cfo` role**
  (`AuthStore.canApprovePaymentRun`), not `canManagePayments` and not "admin
  counts as CFO" — the backend gate is `require_roles(ROLE_CFO)`, which does
  not special-case admin, and a sign-off an admin can grant themselves is not a
  sign-off (same reasoning as the web `auth.hasRole('cfo')` on that button).
  Authorizing a run is a money-path decision, so it confirms first and the
  dialog names the run (created date + payment count) and its total; the
  server's own refusal sentence (409 wrong state / 403 maker-checker) surfaces
  verbatim. Approving moves no money — execution stays a separate action.
  Money is rendered as server-supplied display strings — the device never does float arithmetic on money (totals are server-computed)
- Quality inspections (4-way matching) — `InspectionsScreen` + `InspectionStore`
  over `GET /api/inspections` with **server-side** outcome chips (`?result=`;
  filtering the loaded page would hide every matching row past the page
  boundary). Tapping a row opens `InspectionDetailScreen`
  (`GET /api/inspections/{id}`), which states what the outcome does to the
  4-way match — a `fail` is what puts a quality hold on a payable invoice, and
  that is not guessable from the word "Fail" — and flags a row linked to neither
  receipt nor PO, which no match will ever read. **Recording** an inspection
  (`POST /api/inspections`) is a form sheet: a goods-receipt picker (required,
  because `po_matching` only reads an inspection through a receipt or a PO-level
  row), a suggested `QI-<receipt>` number, the three-outcome segmented control
  with the consequence of the selected outcome spelled out, quantities shown once
  something was refused, and an inspected date / inspector / deviation notes.
  Quantities are sent as **strings**, never parsed through a `double` — the API
  hands them straight to `Numeric(12, 4)`. The list + detail are open to every
  role (the backend reads are `get_current_user`); the record affordance is
  admin / ap_manager only. Reached from **Settings → Procurement**. QMS sync is
  deliberately web-only — it is an operator action against org-level config that
  409s unless `settings.qms` is set. Not offline-cached: a stale pass/fail is a
  wrong answer about whether an invoice can be paid
- Adaptive AI workflows (read-first) — `AdaptiveScreen` + `AdaptiveStore` over
  `GET /api/adaptive/{suggestions,approval-patterns,anomalies}`, three tabs each
  with its own loading / error / empty state and its own request sequence (a
  failing anomaly scan cannot blank the patterns a reader is looking at). The one
  write is **dismiss a suggestion** (`POST /suggestions/{id}/dismiss`, behind a
  confirm dialog, gated on admin / ap_manager = the backend `_WRITE_ROLES`; a CFO
  reads and is not offered it). **The two apply paths are deliberately absent** —
  `routing-suggestion/apply` reassigns a live approval and
  `threshold-recommendation/apply` raises the org-wide auto-approve threshold,
  which is a money-path control surface, and the latter's stale-value 409 needs a
  real "the recommendation changed, nothing was applied" surface to land safely.
  The Feedback tab is absent too: `GET /feedback` writes an access-audit row, so
  it needs its own "only when asked" treatment rather than loading with the
  others. Per-vendor amounts render **without a currency symbol** under a section
  note (the payload does not name the reporting currency they are in); anomaly
  rows carry their own `amount_currency` and are labelled with it; a non-zero
  `unconverted_count` is disclosed, because those approvals are still counted in
  the sample. Reached from **Settings → Administration**, gated on
  admin / ap_manager / cfo (`_READ_ROLES`). Not offline-cached — a privileged
  analytics read recomputed server-side on every call
- Role-based bottom navigation
- Settings (profile, tenant info, logout)
- JWT in secure storage (iOS Keychain / Android Keystore)
- MFA challenge login — when `POST /api/auth/login` returns an MFA challenge
  (instead of a `TokenResponse`), `AuthStore.login` reports `mfaRequired` and the
  login screen routes to `MfaScreen`. The user enters their TOTP code (or
  switches to the email-OTP backup, auto-/re-requested via
  `POST /api/auth/mfa/challenge/email`), which is verified at
  `POST /api/auth/mfa/verify`; the returned JWT is stored exactly like the
  no-MFA path. Wrong/expired codes surface a friendly live-region-announced
  error and keep the user on the screen to retry. An org-enforced un-enrolled
  user (`must_enroll`) gets an email-only flow plus a banner to finish
  authenticator setup in the web app (enrollment + passkeys are web-only)
- Camera OCR — snap photo or pick from gallery → upload → trigger AI extraction
- File upload via file picker — pick a PDF / PNG / JPG / TIFF document on the device (`CameraCapture.pickDocument` → `file_picker`) and upload it through the same `/api/invoices/upload` extraction pipeline as the camera path. The capture screen offers Camera / Gallery / Choose file; PDFs preview as a document card (no inline bitmap), images preview inline
- File viewer — the invoice detail screen previews the uploaded file (image thumbnail or a PDF card) and opens it full-screen via `InvoiceFileViewer`: images via `Image.network` (auth headers), PDFs fetched as bytes (`ApiClient.getBytes`, so the JWT + tenant headers are attached) and rendered with `pdfx`; loading / error / Retry states
- Push notifications — Firebase Cloud Messaging (foreground + background), no-op if Firebase not configured. The device token is registered with the backend (`POST /api/notifications/device-token`, best-effort — a pre-login/offline failure is logged and swallowed, and the token is re-sent on the next `onTokenRefresh`) on both initial acquisition and every refresh — see `backend/docs/notifications.md` § Push device tokens; registration only, no server-side push-SENDING adapter exists yet. Tapping a background/terminated notification deep-links to `InvoiceDetailScreen` for `message.data['invoice_id']` via `PushService.navigatorKey` (a `GlobalKey<NavigatorState>` wired into `MaterialApp.navigatorKey` in `main.dart`, since the FCM tap callback runs outside the widget tree with no `BuildContext` of its own); a missing id or an unmounted navigator no-ops rather than crashing
- Offline mode — SQLite cache for dashboard and invoice list, serves cached data on network failure; **scoped to the signed-in `(tenant, user)`** and torn down on logout (see Session lifetime + offline-cache scoping)
- Biometric login — Face ID / fingerprint / device PIN, toggle in settings, checked on app launch

**Mobile-only features (not on web):**
- Camera OCR capture (snap photo → upload)
- Push notifications (FCM)
- Offline mode (SQLite cache)
- Biometric login (Face ID / fingerprint)
- Swipe-to-approve gesture

**Web features not yet on mobile (see `docs/roadmap.md` Priority 8):**
- **MFA enrollment** — the *challenge / verify* flow is shipped (see Done →
  "MFA challenge login"), but **enrolling** a TOTP authenticator (and managing
  passkeys) is still web-only (`/profile`). An org-enforced un-enrolled user can
  log in by email OTP on mobile and is pointed to the web app to finish setup.
- **Passkey (WebAuthn) MFA** — web-only; never offered as a mobile factor.
- **Org Security settings** — the web `/organization` page exposes the `mfa.required` toggle; mobile has no equivalent.
- **OIDC SSO** — `Sign in with Okta/Microsoft` button is web-only.
- **Workflow management (create / edit / no-code builder)** — the read-only
  list + step viewer is now on mobile (see Done → "Workflow management
  (read-only)"); creating, editing, version history, simulation and import/export
  stay desktop-only (lower value on a phone).

**Admin parity (now shipped on mobile):**
- **Bulk operations** — invoice multi-select (long-press or the checklist
  app-bar action) + bulk delete / bulk status-change / **bulk export** over
  `POST /api/invoices/bulk/{delete,status,export}`; gated to
  admin/ap_manager/cfo; the backend skips immutable-status rows and the result
  snackbar reports deleted/updated + skipped counts. **Export** offers CSV / XML
  from a format sheet, POSTs the selected ids to `bulk/export` (raw bytes via
  `ApiClient.postBytes`, which parses the `Content-Disposition` filename), writes
  the bytes to a temp file and hands them to the platform share sheet
  (`share_plus` via the swappable `services/file_share.dart`). Export is a
  non-mutating read, so it leaves the selection intact; loading + error +
  share-cancel states are announced via `A11y.announce`.
- **Workflow management (read-only)** — `WorkflowsScreen` + `WorkflowStore` over
  `GET /api/workflows` list a tenant's workflow definitions (name, active/default
  status badges, step count); tapping a row opens `WorkflowDetailScreen`
  (`GET /api/workflows/{id}`) with the configured steps (number, type, name,
  enabled flag, a short PII-free per-step config summary). Reached from Settings →
  Administration, admin-gated (`AuthStore.canViewWorkflows`, mirroring the web
  nav `roles: ['admin']`). The no-code builder — create / edit / versions /
  simulate / import-export — stays on the web; mobile is a viewer. Not
  offline-cached (privileged admin read).
- **Admin user management** — `AdminUsersScreen` over `/api/admin/*`: list/search
  users, **create a user** (a FAB opens a validated form sheet — full name +
  email + system-role pick → `POST /api/admin/users`; the server-generated
  one-time temporary password is surfaced in a dialog for the admin to hand
  over, then the list refreshes), edit a user's roles (system roles only —
  the mobile role editor offers only the four SYSTEM roles — custom roles are created and granted on the web (`/admin?tab=roles`). Custom roles DO confer access: effective permissions are the union over a user's roles, custom ones via the `roles.permissions` JSONB column (migration 0062) — see `backend/app/api/permissions.py`), activate/deactivate, and **delete a
  user** (an armed/confirmed destructive action in the per-user sheet →
  `DELETE /api/admin/users/{id}`; self-delete is disabled client-side and the
  backend's 409 — self / still-referenced-by-in-flight-work — surfaces in the
  failure snackbar). Admin-only; reached from Settings → Administration.
- **Organization settings** — `OrgSettingsScreen` reads + edits the safe subset
  the web app exposes (company profile + invoice defaults) via `GET/PATCH
  /api/organization`. ERP credentials, payment/webhook secrets, extraction keys
  and SSO are deliberately NOT surfaced. Admin-only; the company `logo_url` set
  on web is carried through unedited so a save doesn't drop it.

