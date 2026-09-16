# Mobile — CLAUDE.md

Mobile-specific guidance. See root `CLAUDE.md` for project-wide context.

## Where to look (mobile docs)

This file holds the rules. The reference material lives in `mobile/docs/`:

| Topic | File |
|-------|------|
| What mobile ships vs web-only vs out of scope | `docs/feature-status.md` |
| The `lib/` tree — screens, stores, widgets, models, services; the native launch screens | `docs/project-structure.md` |
| i18n — ARB catalogues, delegate, locale-aware formatting | `docs/i18n.md` |

## Stack

- **Flutter 3.41+**, Dart 3.11+
- **iOS + Android** (single Dart codebase, dual platform targets)
- **State management**: StatefulWidget + setState + ChangeNotifier singletons (no Bloc/Provider/Riverpod)
- **HTTP**: `http` package + `flutter_secure_storage` for JWT
- **Styling**: Material 3 with blue seed color

## Commands (from `mobile/`)

```bash
flutter pub get              # install dependencies
flutter run                  # run on connected device/simulator
flutter build ios            # production iOS build
flutter build apk            # production Android APK
flutter build appbundle      # production Android App Bundle (Play Store)
flutter analyze              # lint/analyze
flutter test                 # run tests
flutter gen-l10n             # regenerate AppLocalizations from lib/l10n/*.arb
```

`flutter build ios` needs macOS and Xcode. On Linux, rely on CI's **Mobile iOS
build** job (`ci.yml` → `mobile-ios-build`), which runs the release workflow's
exact `flutter build ios --release --no-codesign` on every PR touching
`mobile/`, with no signing and no Firebase config.

## Project structure

**The full `lib/` tree is `mobile/docs/project-structure.md`.**

The shape: `screens/` (one per route) · `stores/` (ChangeNotifier state) ·
`widgets/` (the shared widget library) · `models/` · `services/` (API client).
Guard rail 10 — put code in the directory its siblings already establish, and
build screens from the widget library rather than duplicating markup.
## Architecture patterns

- **ChangeNotifier singletons** for stores — `AuthStore.instance`, `InvoiceStore.instance`, etc.
- **ListenableBuilder** in widgets to react to store changes
- **No DI framework** — stores are static singletons
- **API client singleton** — `ApiClient()` auto-adds JWT and `X-Tenant-Slug` header
- **Secure storage** — JWT token persisted in iOS Keychain via `flutter_secure_storage`

### Session lifetime + offline-cache scoping

The store singletons and the SQLite offline cache outlive a session — a phone
gets handed over, an account gets switched, a token expires — so both are bound
to the session that produced them. `services/session.dart` is the chokepoint;
**never clear or scope this state from a screen**.

- **Namespaced keys.** `OfflineStore.put/get` prepend a
  `<tenant>|<user>|` prefix derived from the installed scope, so a call site
  never passes one and can't forget one. Different `(tenant, user)` ⇒ different
  keys ⇒ a session physically cannot read another's rows. With **no** scope
  installed (signed out) the store is inert: writes are dropped, reads return
  null — it fails closed to "no cache", never to another session's data.
- **`SessionManager.beginSession(tenantSlug, userId)`** — called by
  `AuthStore` on login, on post-MFA verify, and on session restore, BEFORE the
  user is published. Installs the scope; if it differs from the one the cache
  was last written under (device reused, crash before logout, or an install
  upgrading from the pre-scoping schema) it purges every cached row and resets
  the stores first.
- **`SessionManager.endSession()`** — called from `ApiClient.clearSession()`,
  the single place a session ends: explicit logout, a 401 on any request, and a
  failed session restore all funnel through it. Drops the scope, clears the
  cache, and resets **every** account-scoped store singleton.
- **Session restore is failure-aware.** `AuthStore.init()` tears the session
  down only when the stored token is actually **rejected** (`ApiException` 401).
  A transport failure (offline, DNS, timeout) says nothing about the token, and
  tearing down on one would wipe the cache at exactly the moment offline mode
  exists to serve it — so credentials and cache are left intact and the same
  scope is re-installed over the same rows on the next successful load.
- **Adding a store?** Add it to `SessionManager.resetStores()`. A store that
  isn't reset keeps one account's data in memory for the next one;
  `test/services/session_test.dart` fails if a new file under `lib/stores/`
  isn't listed. The only exemptions are `LocaleStore` (display language is a
  device preference, not account data) and `sequenced_fetch.dart` (the
  `SequencedFetch` mixin — a per-store request-sequence helper, not an
  account-scoped store singleton).
- **Cache DB upgrade path.** `feohledger_cache.db` is at schema v2; the v1→v2 upgrade
  deletes every pre-existing row, because rows written before scoping have
  global keys (`dashboard`, `invoices_all_`, …) with no owner to attribute them
  to. An install carrying an old cache therefore starts empty rather than
  serving un-namespaced rows to whoever signs in next.
- **Tests.** `test/services/session_test.dart` covers the scoping + teardown
  behaviour through the in-memory seam;
  `test/services/offline_store_sqlite_test.dart` runs the same production code
  against a **real** SQLite database via the `sqflite_common_ffi` dev dependency
  (`sqfliteFfiInit()` + `databaseFactory = databaseFactoryFfi`), which is what
  proves the key prefix round-trips through a TEXT column and that the v1 → v2
  upgrade really deletes a legacy device's rows.

## API integration

The mobile app talks to the same FastAPI backend as the web frontend:

- API base URL: `http://localhost:8000` (configurable in `config.dart`)
- Auth: `POST /api/auth/login` → JWT stored in secure storage
- **MFA**: `POST /api/auth/login` may return an **MFA challenge**
  (`{mfa_required: true, mfa_challenge_token, methods, must_enroll}`) instead of
  a `TokenResponse` when `FEOH_MFA_ENABLED` is on and the user is enrolled /
  org-enforced. `AuthStore.login` returns a `LoginResult`
  (`success`/`mfaRequired`/`failure`); on `mfaRequired` the login screen pushes
  `MfaScreen`, which submits the code to `POST /api/auth/mfa/verify` (`totp` or
  `email` method) → real JWT stored exactly like the no-MFA path. Email-OTP
  backup is requested via `POST /api/auth/mfa/challenge/email`. Passkey
  (WebAuthn) is web-only — never offered on mobile; MFA *enrollment* is also
  web-only (an org-enforced un-enrolled user can still verify by email, with a
  banner pointing them to the web app). Mirrors the web `/login/mfa` flow.
- Tenant: entered on login screen → sent as `X-Tenant-Slug` header
- 401 responses auto-clear session and return to login — on **every** verb,
  but **only when a credential was actually presented** (`_token != null`).
  `/auth/login`, `/auth/mfa/verify` and `/auth/mfa/challenge/email` return 401
  for a wrong password or mistyped code while no token exists yet; tearing the
  session down there deleted the `tenant_slug` the half-finished login still
  needs, so the user would retype the code correctly, authenticate against the
  control-plane-only auth routes, reach the home screen, and have every
  tenant-scoped request go out with no `X-Tenant-Slug` — unrecoverable without
  a manual sign-out. `clearSession()` also deletes the token and tenant keys
  **explicitly** rather than `deleteAll()`: locale and biometric-enabled are
  device preferences, not session state, and a single mistyped password used to
  reset both.
- The **return to login** half is a reactive gate, not an imperative push:
  `main.dart` wraps `home:` in a `ListenableBuilder` on `AuthStore`, so a
  forced logout re-routes the tree. It previously only *cleared* the session,
  stranding the user on `HomeScreen` with the nav collapsed to clerk-level
  tabs and every tab erroring, with no explanation and no way out but guessing
  to open Settings → Sign Out.
  (`get`/`getList`/`post`/`patch`/`delete`/`getBytes`/`postBytes`), and the
  teardown is **awaited before the error is thrown**, so an offline-fallback
  `catch` can't read the cache of the session being torn down

## Screens → API mappings

| Screen | API calls |
|--------|-----------|
| Login | `POST /api/auth/login`, `GET /api/auth/me` |
| MFA (second factor) | `POST /api/auth/mfa/verify` (totp/email → JWT), `POST /api/auth/mfa/challenge/email` (request email OTP) |
| Dashboard | `GET /api/dashboard` |
| Cash Flow | `GET /api/analytics/cashflow_forecast` + `GET /api/analytics/cash_position` (both `horizon_days` + `granularity`; CFO/admin) |
| Invoices | `GET /api/invoices` (advanced search adds `vendor` / `po_number` / `amount_min` / `amount_max` / `due_date_from` / `due_date_to`); bulk ops `POST /api/invoices/bulk/delete` + `POST /api/invoices/bulk/status` + `POST /api/invoices/bulk/export` (CSV/XML → share sheet; admin/ap_manager/cfo) |
| Admin — User Management | `GET /api/admin/users` (`search`/paginated), `GET /api/admin/roles`, `POST /api/admin/users` (`email` / `full_name` / `role_names` → returns a one-time `temporary_password`), `PATCH /api/admin/users/{id}` (`role_names` / `is_active`), `DELETE /api/admin/users/{id}` — admin only |
| Admin — Organization Settings | `GET /api/organization`, `PATCH /api/organization` (`{name, settings:{company, invoice_defaults}}` — shallow-merged; admin only) |
| Admin — Workflows (read-only) | `GET /api/workflows` (list), `GET /api/workflows/{id}` (detail) — reads open to any authed role; the mobile entry point is admin-only (mirrors web nav `roles: ['admin']`). No create/edit on mobile |
| Invoice Detail | `GET /api/invoices/{id}` (carries `warnings` + `po_match`), `POST /api/invoices/{id}/approve`, `POST /api/invoices/{id}/reject`, `PATCH /api/invoices/{id}` (edit fields — admin/ap_manager/cfo, hidden in immutable statuses; financial fields omitted once approved), `GET /api/invoices/{id}/audit-log` (activity timeline + ERP-status derivation, any authenticated role) |
| Approvals | `GET /api/invoices?status=ready_for_review` — a **server-side** filter into `InvoiceStore._pending`, with its own `SequencedFetch` token. It used to slice the Invoices tab's already-fetched page client-side, so tapping the *Paid* chip on Invoices made Approvals read "All caught up" while invoices sat awaiting review — and because both live in one `IndexedStack`, `initState` never re-fired and pull-to-refresh re-applied the same filter, so it could not self-correct. |
| Exceptions | `GET /api/exceptions` (status filter), `POST /api/exceptions/{id}/resolve` (action=resolve\|escalate\|dismiss), `POST /api/exceptions/bulk/resolve` (`{ids, action, resolution}` → `{updated, skipped:[{id,reason}]}`) |
| Exception Detail | `GET /api/exceptions/{id}` (full row + invoice), `POST /api/exceptions/{id}/assign` (`{user_id}`, null = unassign), plus the resolve/bulk routes above. The assignee picker reuses `GET /api/admin/users` (admin-only) |
| Contracts | `GET /api/contracts` (`status` / `contract_type` / `search` / paginated) — read open to admin/ap_manager/ap_clerk/cfo |
| Contract Detail | `GET /api/contracts/{id}` (fields + spend summary + line items), `POST /api/contracts/{id}/activate`, `POST /api/contracts/{id}/terminate`, `POST /api/contracts/{id}/cancel` (mutations admin/ap_manager). Document upload / repository / renew / create-PO stay web-only |
| Notifications | `GET /api/notifications` (`unread_only` filter — envelope carries `items` + total `unread`), `GET /api/notifications/unread-count` (badge), `POST /api/notifications/{id}/read`, `POST /api/notifications/read-all` |
| Payments | `GET /api/payments` |
| Vendors | `GET /api/vendors` (status/search filters), `POST /api/vendors/{id}/verify`, `POST /api/vendors/{id}/reject`, `POST /api/vendors/sync-erp` (mutations admin/ap_manager) |
| Pay (queue) | `GET /api/payments/queue`, `GET /api/payments/summary`, `GET /api/payments/runs/`, `POST /api/payments/runs` (create draft), `POST /api/payments/runs/{id}/execute`, `POST /api/payments/runs/{id}/cancel` (admin/ap_manager/cfo), `POST /api/payments/runs/{id}/approve` (CFO sign-off — **cfo role only**, mirroring the backend `require_roles(ROLE_CFO)`) |
| Quality Inspections | `GET /api/inspections` (`result` / `gr_id` filters, paginated — reads are role-open), `GET /api/inspections/{id}`, `POST /api/inspections` (record — admin/ap_manager), plus `GET /api/goods-receipts` for the record form's receipt picker. `POST /inspections/sync` (QMS pull) is deliberately web-only |
| Adaptive Workflows | `GET /api/adaptive/approval-patterns`, `GET /api/adaptive/anomalies`, `GET /api/adaptive/suggestions`, `POST /api/adaptive/suggestions/{id}/dismiss` (admin/ap_manager). The two `/apply` paths and `GET /feedback` are deliberately web-only |
| Settings | Uses cached auth state |

## Role-based UI

Bottom navigation adapts based on user roles (same as web frontend):

| Tab | Visible to |
|-----|-----------|
| Dashboard | All roles |
| Invoices | All roles |
| Contracts | All roles (activate / terminate: Admin, AP Manager only) |
| Approvals | Admin, AP Manager |
| Exceptions | Admin, AP Manager |
| Vendors | Admin, AP Manager, CFO (verify/reject + ERP sync: Admin, AP Manager only) |
| Pay | Admin, AP Manager, CFO |
| Payments | Admin, AP Manager, CFO |
| Settings | All roles |

The **Settings list is the hub for everything that is not a bottom-nav tab**, and
its sections are gated **per entry, against each surface's own backend gate** —
never once for the whole group. **Settings → Procurement → Quality Inspections**
is open to every role, because `GET /api/inspections` and its detail are
`get_current_user` and a clerk chasing a quality hold is exactly who needs to
read a failed inspection (the *record* affordance inside the screen is the thing
gated to admin/ap_manager). **Settings → Administration** renders whenever any of
its children does: the three admin surfaces on `isOrgAdmin`, **Adaptive
Workflows** on `canViewAdaptive` (admin/ap_manager/cfo — the backend's
`_READ_ROLES`). A group-level admin gate could express only one of those and hid
adaptive from two roles the API admits.

The **admin surfaces** (User Management + Organization Settings + the read-only
Workflows viewer) are not bottom-nav tabs — they live under that **Settings →
Administration** section and each renders only for admins (`AuthStore.isOrgAdmin`),
mirroring the backend `require_roles(ROLE_ADMIN)` on `/api/admin/*` + `PATCH
/api/organization` and the web nav `roles: ['admin']` on `/workflows` (the
`/api/workflows` reads themselves are open to any authed role, so the Workflows
entry is a UI gate matching desktop, not a security boundary). The
**invoice bulk-ops** affordance (multi-select toggle + long-press) shows only
for `canBulkEditInvoices` (admin/ap_manager/cfo), matching the bulk endpoints'
gate; clerks never see it.

The **notification center** is not a bottom-nav tab — it's reached from the
`NotificationBell` app-bar action (with a live unread `Badge`) in the Dashboard
app bar, so it's available to **all roles** (notifications are per-user, not
role-gated; the backend scopes the list to the caller via `require_roles(*ALL_ROLES)`).

The **cash-flow forecast** is likewise not a bottom-nav tab — it's reached from
the `CashFlowButton` app-bar action (chart icon) in the Dashboard app bar,
visible only to **CFO + admin** (`AuthStore.canViewCashFlow`, mirroring the
backend `_CFO_ROLES = (admin, cfo)` gate on `/api/analytics/cashflow_forecast`
+ `/cash_position`). `ap_manager` is deliberately excluded — it's a privileged
CFO surface, not the operational dashboard. The button renders nothing for
everyone else.

## Feature status

**The per-surface table — what mobile ships, what is web-only, and what is
deliberately out of scope — is `mobile/docs/feature-status.md`.** Parity
direction is set in `frontend/CLAUDE.md` § Web vs Mobile feature parity.

Check it before building a screen: mobile is intentionally a **subset**, focused
on the approve-on-the-go path rather than mirroring every web page.
## Accessibility (WCAG 2.2 AA equivalent)

The app targets WCAG 2.2 AA / EU EAA / ADA via Flutter's accessibility APIs.
Follow these conventions on every new screen/widget:

- **Label every icon-only / custom tappable.** A bare tooltip is *not* reliably
  exposed as a screen-reader label on all platforms (verified — `IconButton`'s
  `tooltip` shows up via `find.byTooltip` but not `find.bySemanticsLabel`). Wrap
  icon-only buttons in `Semantics(label: ..., button: true, child: IconButton(...))`
  and keep the `tooltip` for sighted hover (e.g. the login password show/hide
  toggle, the invoices Capture action). Swipe-action affordances carry a visible
  text label beside the icon (see `approvals_screen._swipeBackground`).
- **Compose one announcement per row/card.** List tiles, KPI cards and status
  badges wrap their inner spans in `Semantics(label: '...', excludeSemantics: true)`
  so assistive tech reads one sensible phrase ("Acme Supplies, $1,500, invoice
  INV-001, Ready for Review") instead of 5 disjoint fragments. Status badges
  expose `'Status: <label>'`.
- **Live-region announcements** for state changes that aren't seamlessly spoken
  (toasts, a swiped row vanishing, inline form errors). Funnel them through
  `A11y.announce(context, message)` in `lib/utils/a11y.dart` — it uses the
  non-deprecated `SemanticsService.sendAnnouncement` and resolves
  `TextDirection` from the active `Directionality` (avoids the `intl`
  `TextDirection` name clash). Wired into the `_showSnack` helpers
  (invoice/contract detail), capture upload result, login error, and the
  approvals swipe-approve.
- **Colour contrast ≥4.5:1.** Status/payment badges render the text in a
  *darkened* variant (`.shade700`/`.shade800`/`.shade900`) of the accent over
  the 0.15-alpha tint — the full-saturation hue fails AA. Muted greys use
  `grey.shade700` (not `shade500`/`shade600`, which fail at 11-14px).
- **Decorative icons** (the `BrandMark` widget, placeholder camera glyph, aging dots) are
  wrapped in `ExcludeSemantics` so they aren't announced.
- **Tap targets ≥48dp** — use `IconButton` defaults; don't shrink hit areas.
- **Don't disable text scaling / reduce-motion.** The app uses default Material
  transitions only, which already honour the platform settings; no custom
  animation caps scaling or ignores `MediaQuery.disableAnimations`.

**Regression guard** (mirrors the web axe pass) — `test/a11y/accessibility_test.dart`.
In a `testWidgets`, call `tester.ensureSemantics()`, pump the widget/screen, then:

```dart
await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
await expectLater(tester, meetsGuideline(textContrastGuideline));
```

plus `find.bySemanticsLabel(...)` to confirm icon buttons expose labels. Covers
the invoice list tile, KPI card, status badge, login screen, the capture action,
the approvals approve/reject affordances, the exception list tile + exception
status badge + exceptions screen (queue swipe actions) + the exception list tile
in **selection mode** (exposes a `selected`/checked state, keeps its tap target,
still announces one merged row label) + the `ExceptionDetailScreen` (the loaded
detail meets tap-target/label/contrast), the invoice
activity timeline + invoice edit-sheet (icon-only close/clear controls labelled,
one merged announcement per timeline entry; the financially-locked variant keeps
its labels + AA contrast on the lock notice and per-field "Frozen after
approval" helper text), the vendor list tile + vendor
status badge (one merged row announcement; every status colour clears contrast),
the invoice warnings panel (one merged "Severity: message" announcement per
warning; every severity tint clears contrast), the ERP status panel (error-row
contrast), the advanced-search sheet (labelled icon-only close/date-clear
controls, tap-target + contrast), and the notification center (the
`NotificationListTile` — one merged "Unread, <event>, <title>, …" announcement
that also clears contrast on a read row; the `NotificationBell` — accessible
label carrying the live unread count, e.g. "Notifications, 3 unread"; the
`NotificationsScreen` — labelled mark-all-read action + tap-target/contrast),
and the cash-flow forecast (the `CashFlowScreen` with a breached period — the
low-balance alert exposes one merged "Low balance alert …" announcement, and the
red projected-end / breached-closing money + alert copy all clear contrast at
AA via `.shade900`), the invoice list tile in **selection mode** (exposes a
`checked` state + keeps its tap target) and the `BulkActionBar` (labelled count +
the export / status / delete actions, contrast), the read-only `WorkflowsScreen`
(loaded list meets tap-target + label + contrast; the inactive row merges into
one announcement carrying "Inactive" so the status badge isn't an unlabelled
colour cue), the quality-inspection surface (every `InspectionResultBadge` tint — pass /
fail / partial / unknown — plus its `Result: <outcome>` announcement; the
`InspectionsScreen` loaded list, whose rows merge into one announcement carrying
the outcome and, for an unlinked row, "Not linked"; the `InspectionDetailScreen`
loaded detail, where the consequence note and the amber no-match-will-read-this
callout both render `brown.shade800` over a pale tint because a true orange
fails), the `AdaptiveScreen` (the suggestions tab — status badge + muted
rationale/confidence, one merged announcement per card carrying the status; the
patterns tab, whose unconverted-approvals disclosure uses the same amber-reading
brown; the anomalies tab, covering both severity tints), and the two admin
screens — `AdminUsersScreen` (a
deactivated row merges into one announcement carrying "inactive" so the Inactive
badge isn't an unlabelled colour cue; the in-app-bar Material `SearchBar` is a
24px framework field exempt from the whole-screen tap-target sweep, same as the
invoices/vendors screens) and `OrgSettingsScreen` (form fields meet tap-target +
label + contrast).
`textContrastGuideline` is strict
(it caught the 4.38:1 and 2.55:1 muted-grey defects during this pass), so add a
contrast check when introducing new coloured text.

## Money formatting — the currency comes from the payload, never from the screen

**Every money figure goes through `lib/utils/money.dart`, and the currency is an
argument.** `formatMoney` for a `num`, `formatMoneyString` for an exact decimal
string off the wire, `formatMoneyCompact` for a KPI tile. Nine screens and
widgets each declared their own `NumberFormat.currency(symbol: '\$')` at module
level, so every figure on them read as dollars whatever the row or the org said
— on the two detail screens, directly above a `Currency` row printing the real
code.

Where a call site gets the currency, in order — **take the most specific one
that exists and never reach past it**:

1. **The row's own `currency`** for a per-row figure (invoice, contract,
   payment, payment-queue row). A multi-currency tenant holds rows in several,
   so the org's reporting currency is a *different* wrong answer here, not a
   fallback.
2. **The payload's own code** for an aggregate that names one —
   `/payments/summary`'s `currency`, `cash_position`'s
   `opening_balance_currency`, the dashboard's `reporting.reporting_currency`.
   The server resolved it to denominate those figures, so there is nothing to
   disagree with.
3. **`OrgCurrencyStore.instance.currency`** for an aggregate the server
   denominates in the reporting currency *without* naming it — today the
   adaptive per-vendor averages and the cash-flow forecast leg. It mirrors the
   web `orgCurrency` store and resolves the same three settings rungs
   (`reporting_currency` → `payments.home_currency` →
   `invoice_defaults.currency`); call `ensureLoaded()` in `initState` and put the
   store in the screen's `ListenableBuilder` (`Listenable.merge`) so a figure
   picks up its symbol when the code lands.
4. **Nothing — pass `null` and let the figure render bare.** Not a degraded
   mode, the answer: a mixed-currency sum (`payment_runs.total_amount`) is in no
   currency, and a `null` per-row code "is NOT a licence to substitute a
   default" (`docs/decisions.md` §79/§82, §160). A missing symbol is a visible
   gap; a wrong one is a wrong number that looks right.

A model's `fromJson` must **not** default a currency to `'USD'` — that is the
rung-that-always-answers trap `docs/decisions.md` §119 records, and it makes
rung 4 unreachable. The one sanctioned default is
`OrgSettings.defaultCurrency`, which exists because the org-settings *form*
field needs a value; `resolvedReportingCurrency` is the abstaining accessor
beside it.

## Internationalization (i18n)

**Full reference: `mobile/docs/i18n.md`** (ARB catalogues, the generated
delegate, locale negotiation, and the locale-aware formatting helpers). The web
counterpart is `frontend/docs/i18n.md`.

The rule is the same on both surfaces: **no user-facing string is a hardcoded
literal**, and every number, date and currency renders through the locale-aware
helpers. A new string ships with its ARB entry in the same change.
## Conventions

- **StatefulWidget + setState** for local state, **ChangeNotifier** for shared state
- **No code generation** — manual `fromJson` factories for models (the only
  generated code is the gen-l10n `AppLocalizations` under `lib/l10n/gen/`)
- **Material 3** with `useMaterial3: true`
- **iOS + Android** — no web/desktop targets
- **Lint rules** (`analysis_options.yaml`): `prefer_single_quotes`, `require_trailing_commas`, `sort_pub_dependencies`, `always_use_package_imports`
- **Native launch screens copy `SplashScreen`** — its background colour (`#F8F9FF`, the theme's scaffold background) and the mark's 26px lift above centre are duplicated in the Android res and the iOS storyboard, so a change to the seed colour or the splash column changes them too. Their images come from `assets/gen-icons.sh`. Details: `docs/project-structure.md` § Launch screens.
