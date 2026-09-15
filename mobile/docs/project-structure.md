# Project structure

The `mobile/lib/` tree — screens, stores, widgets, models and services.
Extracted from `mobile/CLAUDE.md` to keep that file cheap to load.


```
mobile/
├── l10n.yaml                    # gen-l10n config (arb-dir, template, committed output dir)
├── lib/
│   ├── main.dart                # App entry, splash, biometric check, push init; MaterialApp.locale ← LocaleStore (i18n); MaterialApp.navigatorKey ← PushService.navigatorKey (notification-tap deep links)
│   ├── config.dart              # API URL, tenant slug
│   ├── l10n/                    # i18n — ARB catalogues + committed gen-l10n output
│   │   ├── app_en.arb           # source-of-truth catalogue (+ app_{de,fr,es,pt,pt_BR,ja}.arb)
│   │   └── gen/                 # committed AppLocalizations (flutter gen-l10n; non-synthetic)
│   ├── api/
│   │   ├── api_client.dart      # HTTP client (JWT + X-Tenant-Slug header, timeout, debug logs)
│   │   └── endpoints.dart       # Typed API methods (auth, invoices, dashboard, payments)
│   ├── models/
│   │   ├── user.dart            # User model with role helpers
│   │   ├── adaptive.dart        # ApproverPattern / VendorPattern / ApprovalPatterns, AnomalyFlag / InvoiceAnomaly / AnomalyBatch, WorkflowSuggestion + SuggestionStatus (every statistic a string — the backend stringifies its Decimals)
│   │   ├── goods_receipt.dart   # GoodsReceipt (read only to populate the record-inspection picker; `label` = "GR-1 → PO-9")
│   │   ├── inspection.dart      # Inspection + InspectionResult enum (pass/fail/partial + unknown for a free-form/QMS value) + InspectionDraft (quantities sent as raw strings) + quantityToDisplay
│   │   ├── audit_entry.dart     # AuditEntry + AuditFieldChange (invoice activity timeline; details.changes diff)
│   │   ├── contract.dart        # Contract + ContractStatus enum (draft/active/expired/terminated/cancelled; isActionable gate on the lifecycle buttons)
│   │   ├── invoice.dart         # Invoice, InvoiceStatus enum (12 states); isEditable mirrors backend IMMUTABLE_STATUSES, isFinanciallyLocked mirrors the narrower _FINANCIALLY_LOCKED_STATUSES ({approved} ∪ immutable) + kFinancialInvoiceFields / stripFinancialFields
│   │   ├── mfa_challenge.dart    # MFAChallenge (login MFA-challenge response) — challengeToken + offered methods (totp/email) + mustEnroll
│   │   ├── exception.dart       # ApException, ApExceptionStatus + ApExceptionSeverity enums
│   │   ├── notification.dart    # AppNotification (in-app notification center row); eventLabel + linksToInvoice helper; copyMarkedRead for optimistic mark-read
│   │   ├── payment.dart         # Payment (+ its invoice's `currency`), PaymentMethod, DashboardData (reads the `reporting` block's rollups + `reporting_currency` — the face-value keys are cross-currency sums), aging, trends
│   │   ├── payment_queue.dart   # PaymentQueueItem (own invoice currency), PaymentSummary (+ the reporting `currency` it names), PaymentRun (total is a cross-currency SUM — no currency exists for it), PaymentRunSelection (money as display strings — no client float math)
│   │   ├── vendor.dart          # Vendor, VendorStatus enum (active/unverified/inactive/rejected)
│   │   └── workflow.dart        # WorkflowDefinition + WorkflowStepConfig (read-only; parses steps_config.steps; typeLabel helper)
│   ├── services/
│   │   ├── biometric_service.dart  # Face ID / fingerprint via local_auth
│   │   ├── camera_capture.dart     # Image picker (camera/gallery) + file picker (PDF/PNG/JPG/TIFF) + invoice upload
│   │   ├── file_share.dart         # Swappable share_plus wrapper — writes bytes to a temp file → platform share sheet (bulk export); FileShare.debugOverride for tests
│   │   ├── offline_store.dart      # SQLite cache for offline viewing — every key namespaced by (tenant, user); inert with no session scope
│   │   ├── session.dart            # Session lifetime chokepoint — beginSession (scope + purge on change) / endSession (clear cache + reset every store)
│   │   └── push_service.dart       # Firebase Cloud Messaging + local notifications; registers the device token with the backend (POST /api/notifications/device-token) on acquisition + refresh; deep-links a notification tap to InvoiceDetailScreen via PushService.navigatorKey (wired into MaterialApp in main.dart)
│   ├── stores/
│   │   ├── adaptive_store.dart  # Adaptive read models — three independent section states + request sequences; dismiss patches the row from the response; NOT offline-cached (privileged analytics read)
│   │   ├── inspection_store.dart # Quality inspections — list + server-side result filter, record (refetches), getById, goods-receipt options; NOT offline-cached (a stale pass/fail is a wrong answer about payability)
│   │   ├── auth_store.dart      # Auth state — login, logout, role checks (incl. canBulkEditInvoices + isOrgAdmin gates); binds the session scope on login/restore
│   │   ├── admin_user_store.dart # Admin user management — users + roles, set-roles / activate-deactivate (admin-only, not offline-cached)
│   │   ├── org_settings_store.dart # Organization settings — load + save the safe subset (company + invoice defaults; admin-only, not offline-cached)
│   │   ├── org_currency_store.dart # The org's REPORTING currency for aggregates no payload names (adaptive per-vendor averages, the cash-flow forecast leg) — three settings rungs, `null` when none is usable; NOT for per-row amounts, which carry their own
│   │   ├── invoice_store.dart   # Invoice list, filter, approve/reject + multi-select bulk delete/status (offline cached)
│   │   ├── exception_store.dart # Exception list, filter, resolve/escalate/dismiss + getById (detail) + assign (in-place row patch) + multi-select state + bulkResolve (offline cached)
│   │   ├── notification_store.dart # In-app notification center — list (All/Unread filter), unread badge count, optimistic mark-read + read-all (offline cached)
│   │   ├── dashboard_store.dart # Dashboard KPI data (offline cached)
│   │   ├── contract_store.dart  # Contract list, status filter + search, activate/terminate/cancel (offline cached)
│   │   ├── cash_flow_store.dart # Predictive cash-flow forecast + cash position (CFO/admin); 30/60/90-day horizon; not offline-cached (privileged fast-moving read)
│   │   ├── locale_store.dart    # Per-device display-language choice (i18n) → MaterialApp.locale; persisted via secure storage, never account-roamed
│   │   ├── vendor_store.dart    # Vendor list, filter/search, verify/reject, ERP sync (offline cached)
│   │   ├── workflow_store.dart  # Workflow-definition list (read-only) — load + loading/error; NOT offline-cached (privileged admin read, no mutators)
│   │   └── payment_queue_store.dart # Payment queue + summary + runs; per-row method selection; create/execute/cancel runs
│   ├── utils/
│   │   ├── money.dart           # THE money formatter — formatMoney (num) / formatMoneyString (exact decimal) / formatMoneyCompact + normalizeCurrencyCode; a figure is formatted with the currency its own payload names, and renders BARE when none can be proven (never a substituted `$`)
│   │   ├── a11y.dart            # A11y.announce — screen-reader live-region announcements (SemanticsService, Directionality-aware)
│   │   ├── debouncer.dart       # Trailing-edge debounce for search-as-you-type
│   │   └── sequenced_fetch.dart # SequencedFetch mixin — per-store request sequence so a slow response can't overwrite a newer one
│   ├── screens/
│   │   ├── login_screen.dart    # Tenant + email/password login (routes to MfaScreen on an MFA challenge)
│   │   ├── mfa_screen.dart      # MFA second-factor code entry (TOTP + email-OTP backup); POST /auth/mfa/verify → JWT
│   │   ├── home_screen.dart     # Bottom nav host (role-aware tabs)
│   │   ├── dashboard_screen.dart # KPIs, aging, top vendors (app-bar: CashFlowButton + NotificationBell)
│   │   ├── cash_flow_screen.dart # Predictive cash-flow forecast (CFO/admin) — KPI summary (opening/projected-end balance, committed/pending outflow), per-period forecast + running cash-position list, low-balance alert, 30/60/90-day horizon chips, pull-to-refresh
│   │   ├── invoices_screen.dart  # Invoice list — search + status filters + advanced-search (tune) + camera + multi-select bulk delete/status (admin/ap_manager/cfo)
│   │   ├── contracts_screen.dart # Contract list — search + status filter chips; tap a row → detail
│   │   ├── contract_detail_screen.dart # Contract detail — terms/dates/value fields, spend-to-contract summary, line items, activate / terminate lifecycle actions (admin/ap_manager)
│   │   ├── admin_users_screen.dart # Admin — user management: list/search users, edit roles (system roles), activate/deactivate (admin-only)
│   │   ├── org_settings_screen.dart # Admin — organization settings: company profile + invoice defaults form (admin-only; ERP/payment/SSO secrets NOT surfaced)
│   │   ├── invoice_detail_screen.dart # Detail view with approve/reject + edit affordance + warnings/fraud + PO match + ERP status + activity timeline + file preview (image thumbnail / PDF card) → full viewer
│   │   ├── approvals_screen.dart # Pending approvals with swipe-to-approve
│   │   ├── exceptions_screen.dart # Exception queue — filter + swipe resolve/dismiss; tap a row → detail; long-press / checklist app-bar action → multi-select + BulkActionBar bulk-resolve (admin/ap_manager)
│   │   ├── exception_detail_screen.dart # Single-exception detail (GET /api/exceptions/{id}) — full fields + linked invoice + SLA/due/overdue + assignee, resolve/escalate/dismiss + an admin-gated assignee picker; loading/error/empty states
│   │   ├── notifications_screen.dart # In-app notification center — All/Unread filter, tap → mark read (+ deep-link to invoice detail when the row is an invoice), mark-all-read; empty/loading/error states
│   │   ├── capture_screen.dart   # Camera/gallery capture + file picker (PDF/PNG/JPG/TIFF) → upload → extract
│   │   ├── payments_screen.dart  # Payment history
│   │   ├── vendors_screen.dart   # Vendor management — list + search/status filters, swipe/sheet verify+reject (unverified only), ERP-sync app-bar action (all admin/ap_manager-gated)
│   │   ├── payment_queue_screen.dart # Pay — Queue tab (select approved invoices + per-row method → Create Run) + Runs tab (execute/cancel drafts), KPI summary bar
│   │   ├── workflows_screen.dart # Admin — read-only workflow list (name, active/default status, step count) → tap-through; reached from Settings → Administration
│   │   ├── workflow_detail_screen.dart # Read-only workflow detail — steps (number, type, name, enabled) + per-step config summary; fetches GET /api/workflows/{id} on open
│   │   ├── inspections_screen.dart # Quality inspections — list + server-side outcome chips, pull-to-refresh, record-inspection FAB (admin/ap_manager); tap a row → detail
│   │   ├── inspection_detail_screen.dart # Read-only inspection detail — fields + what the outcome does to the 4-way match + an unlinked-row warning; loading / not-found / error states
│   │   ├── adaptive_screen.dart  # Adaptive AI workflows — Suggestions (+ dismiss) / Approval patterns / Anomalies tabs, per-tab states; read-first (no apply paths)
│   │   └── settings_screen.dart  # User profile, biometric toggle, logout; Procurement section (all roles): Quality Inspections; Administration section (gated PER ENTRY — User Management / Organization Settings / Workflows for admins, Adaptive Workflows for admin/ap_manager/cfo)
│   └── widgets/
│       ├── brand_mark.dart      # The FeohLedger mark (login, splash); decorative, rendered by assets/gen-icons.sh
│       ├── activity_timeline.dart # Invoice audit-log timeline (action label, actor, time, per-field before→after diff); empty state; one merged Semantics label per entry
│       ├── bulk_action_bar.dart  # Bottom bar shown in invoice multi-select mode — selected count + bulk export / status-change / delete actions (each action omitted when its callback is null; reusable shape)
│       ├── advanced_search_sheet.dart # Modal bottom-sheet advanced search (vendor, PO, amount range, due-date range); seeded from live filters; min≤max + decimal validation; returns InvoiceSearchFilters (Apply) / empty (Clear) / null (dismiss)
│       ├── invoice_warnings_panel.dart # Detail-screen warnings/fraud flags (severity-coloured) + PO-match panel (match type, status, variance %, issues); one merged Semantics label per warning
│       ├── erp_status_panel.dart # Detail-screen ERP status — ErpInfo.fromAuditLog derives ERP reference / document id / send error from the audit log; shown for ERP-bound + ERP-failed statuses
│       ├── invoice_file_viewer.dart # Full-screen uploaded-file viewer — images via Image.network (auth headers), PDFs fetched as bytes via ApiClient.getBytes + rendered with pdfx; isPdf/absoluteUrl helpers; loading/error/Retry states
│       ├── invoice_edit_sheet.dart # Modal bottom-sheet edit form (vendor, invoice #, amount, PO, GL, description, due date); returns the partial diff; amount sent as string-Decimal; vendor + amount render read-only (with a lock notice) and are stripped from the diff once the invoice is financially locked
│       ├── inspection_list_tile.dart  # Inspection row — number + outcome badge, goods receipt (or "Not linked"), inspected date; one merged Semantics label
│       ├── inspection_result_badge.dart # Colored inspection-outcome chip (pass/fail/partial/unknown) + the shared localized `inspectionResultLabel`
│       ├── record_inspection_sheet.dart # Modal bottom-sheet record form — receipt picker (required), suggested number, outcome segmented control + consequence, string quantities, date / inspector / notes; returns an InspectionDraft
│       ├── contract_list_tile.dart    # Contract row with number/title, vendor, value, status
│       ├── contract_status_badge.dart # Colored contract status chip (draft/active/expired/terminated/cancelled)
│       ├── status_badge.dart    # Colored invoice status chip
│       ├── exception_status_badge.dart # Colored exception status chip (open/escalated/resolved/dismissed)
│       ├── exception_list_tile.dart    # Exception row with type, invoice, severity, status
│       ├── notification_list_tile.dart # Notification row — unread dot, title, body, event label + relative time; one merged Semantics label
│       ├── notification_bell.dart      # App-bar bell action with a live unread Badge → opens NotificationsScreen (in the Dashboard app bar; visible to all roles)
│       ├── kpi_card.dart        # Dashboard metric card
│       ├── cash_flow_button.dart # Dashboard app-bar action → CashFlowScreen; gated to CFO/admin (renders nothing otherwise; mirrors backend _CFO_ROLES)
│       ├── invoice_list_tile.dart # Invoice row with vendor, amount, status
│       ├── vendor_status_badge.dart # Colored vendor status chip (active/unverified/inactive/rejected)
│       └── vendor_list_tile.dart # Vendor row with name, code/email, status, invoice count
├── test/                        # Unit and widget tests
├── ios/                         # Xcode project (auto-managed by Flutter)
├── android/                     # Gradle project (auto-managed by Flutter)
├── pubspec.yaml                 # Dependencies
└── analysis_options.yaml        # Lint rules
```

