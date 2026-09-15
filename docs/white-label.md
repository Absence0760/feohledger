# White-Label / Partner Branding

Per-tenant white-labeling so an organization can present the app under its own
product name, logo, and accent colors. Shipped so far:

1. **Brand config + frontend theming** (first slice) — `settings.brand`,
   `GET/PUT /api/organization/branding`, CSS custom-property theming.
2. **Branded outbound surfaces** — the generated PDFs (remittance,
   1099 working copy, SOX audit report) and outbound transactional emails carry
   the tenant's product name + logo + accent, not the platform's.
3. **Custom domains** — a tenant can be served on its own vanity
   hostname (`ap.acmecorp.com`) and have the backend resolve it to the right
   tenant, in addition to the `*.localhost` subdomain / `X-Tenant-Slug`
   mechanism. The SPA half is `PUBLIC_PLATFORM_DOMAINS` (which hosts are the
   platform's) plus a runtime-resolved API origin. See
   [Custom domains](#custom-domains-vanity-hostnames) below.
4. **Partner / reseller admin** (this slice) — a partner (reseller) org can
   administer a set of branded **child** tenants: list them, read each child's
   branding, and push branding to a child. See
   [Partner / reseller admin](#partner--reseller-admin) below.

## What it covers

| Field | Stored as | Effect |
|-------|-----------|--------|
| `product_name` | `settings.brand.product_name` | Sidebar product name + document `<title>`. Fallback: **"FeohLedger"**. |
| `logo_url` | `settings.brand.logo_url` | Sidebar logo `<img>`, and the mark in PDF and email headers. Fallback: the FeohLedger mark while the product keeps the platform name, otherwise a monogram of `product_name` in the app and the product name alone on PDFs and emails (see **The fallback mark** below). |
| `accent_color` | `settings.brand.accent_color` | Overrides the `--accent` CSS token (borders, focus rings, accent text). |
| `accent_strong_color` | `settings.brand.accent_strong_color` | Overrides the `--accent-strong` token (text-bearing accent backgrounds — buttons, active chips). |
| `support_url` | `settings.brand.support_url` | Reserved for support links (exposed via the brand store). |
| `legal_url` | `settings.brand.legal_url` | Reserved for legal/terms links (exposed via the brand store). |

No migration — the brand block lives in `Organization.settings` JSONB, exactly
like `residency` and `chat_notifications`.

## Backend

`GET /api/organization/branding` — returns the tenant's `BrandConfig`. **Read
gated to any authenticated org user** (the whole app themes itself, not just
admins), mirroring `GET /api/organization` / data-residency. An empty field
means "use the platform default".

`PUT /api/organization/branding` — **admin-only** (`require_roles(ROLE_ADMIN)`).
Validated by the Pydantic `BrandConfig` schema (`app/schemas/organization.py`):

- `accent_color` / `accent_strong_color` must be a 3- or 6-digit **hex literal**
  (`#638cff` / `#abc`) — the value is injected verbatim into a CSS custom
  property, so nothing else (no `url(...)`, `expression()`, etc.) is permitted.
- `logo_url` / `support_url` / `legal_url` must be **http(s) URLs** — the logo
  renders as `<img src>` and the links as `<a href>`, so `javascript:`,
  `data:`, and other schemes are rejected.

**A legal hex can still be an illegible one, and the UI says so.**
`accent_strong_color` overrides `--accent-strong`, whose single contract is
that white text sits on it — every primary button, active filter chip and the
skip link read off it. So a tenant picking the yellow from their logo makes
those unreadable across the whole app, and the stylesheet contrast guard
(`frontend/src/lib/a11y/tokenPairing.test.ts`) structurally cannot see it: that
scan runs over the sources, this override happens at runtime. Both surfaces
that edit the colour — the `/organization` Branding panel and the
`/admin/partner` child-branding modal, where the consequence lands on somebody
else's users — show the real white-on-colour ratio inline as the field is
typed (`stores/brandTheme.ts` `accentStrongContrast`, `ui/FieldWarning`).

It is **advisory, not a block.** The backend accepts any valid hex on purpose:
the brand is the tenant's call, and a hard refusal would make the API the
arbiter of a design decision. What was missing was anyone stating the cost
before it is saved. See [accessibility.md](accessibility.md) and
[decisions.md](decisions.md) §28.

The mutate path writes `org.settings["brand"]` via `flag_modified` (nested JSONB
in-place mutation isn't auto-marked dirty) and audits
`organization.branding_updated` into the tenant trail. The audit detail is
**PII-free and value-free** — it records only *which* fields are now set
(booleans), never the configured values.

A persisted-but-now-invalid brand block never breaks the read: `_resolve_brand`
falls back to all-empty (= platform defaults).

## Frontend

**Brand store** — `frontend/src/lib/stores/brand.svelte.ts` (a Svelte 5 rune
store). Lazy-loads the branding once per session (`ensureLoaded()`), exactly
like `orgCurrency`: cache + single in-flight request + resilient fallback to
defaults on any failure. Exposes `productName`, `logoUrl`, `supportUrl`,
`legalUrl`, plus `applyTheme()` / `ensureLoadedAndApply()` / `reset()`.

**Pure theming helpers** — `frontend/src/lib/stores/brandTheme.ts` (runtime-free,
unit-tested under vitest): `isValidHexColor()` and `brandThemeVars(brand)`. The
latter returns only the CSS custom-property overrides for colors the org
configured **with a valid hex** — an unset or malformed color is omitted so the
AA-passing `src/app.css` default token stands. The theme is **never duplicated**;
only `--accent` / `--accent-strong` are overridden.

**Application** — `frontend/src/routes/+layout.svelte` runs an `$effect` (gated
on the user being fully signed into a tenant, since the read needs auth) that
calls `brand.ensureLoadedAndApply()`. `applyTheme()` writes the configured
accent tokens onto `document.documentElement.style` (or removes them when
unset). The document `<title>` and the sidebar logo + product name read the
store reactively. `reset()` clears both the cache and the inline `<html>`
overrides (logout / tenant switch).

**The fallback mark** — `brand.mark` (`brandTheme.ts::brandMark`,
`docs/decisions.md` §172). A configured logo always wins. Without one, the
sidebar shows the FeohLedger mark only while the product still carries the
platform's name; a tenant that renamed the product gets a monogram of its own
name on its own `--accent-strong` instead, because the platform mark beside a
partner's product name is FeohLedger branding they never chose. The old "AP"
placeholder was neutral, which is why this never needed a rule before. The
collapsed rail names the mark or monogram with the product name. The static
assets (favicon, touch icon, web manifest, mobile launcher icons) and the mark
on the pre-auth login pages stay platform-branded, as the "AP" favicon was:
they are served before any tenant branding is known. Generated PDFs and outbound
emails follow the same fork through `BrandContext.mark`
(`backend/app/services/branding.py`): the tenant logo, else the platform mark
beside the product name, else the product name alone. A renamed tenant gets
plain text there, where the web app draws a monogram (see **Branded outbound
surfaces** below).

**Admin UI** — the **Branding** section on `/organization`
(`frontend/src/routes/organization/+page.svelte`): product name, logo URL,
two accent color pickers (native swatch + hex text input), support + legal URLs,
saving via `PUT /api/organization/branding`. Client-side validation mirrors the
backend guards (inline toast on a bad hex / non-http(s) URL). On save it
re-loads the brand store so the sidebar + theme update without a reload.

## Accessibility note

When an org sets a custom accent, that's their choice. The defaults
(`#638cff` / `#3f5fd6`) are the AA-passing tokens documented in `src/app.css`;
the **strong** accent exists specifically as a darker companion so white text on
an accent background clears WCAG 1.4.3 contrast. Orgs are encouraged (UI hint) to
set a darker strong accent for the same reason, but the app does not enforce a
contrast ratio on custom colors in this slice.

## Supplier-portal theming

The supplier portal (`/portal/*`) is a **separate surface** from the employee
app — vendor users (`VendorUser`, JWT `typ=vendor`) authenticate against it with
their own token (`portal_auth_token`), and the **login page is
unauthenticated**. It now carries the tenant's white-label brand (accent colors
+ logo + product name + `<title>`) across the login page AND the authed portal
pages, exactly as the main app does — so a supplier always sees the buyer's
brand, never the platform default.

### The auth-boundary problem — a public brand read

The employee app reads `GET /api/organization/branding`, which is gated to
authenticated org **users** (employees). Vendor users are a *different identity*,
and the portal login is unauthenticated, so neither that endpoint nor a JWT is
available to theme the portal. The portal therefore reads a dedicated,
**public-by-design** endpoint:

`GET /api/portal/branding` (`app/api/portal.py`) — returns the resolved tenant's
`BrandConfig`. Documented public (like the SSO `GET /api/auth/sso/config`
endpoint, and listed in `tests/test_rbac.py::PUBLIC_BY_DESIGN`):

- **Tenant resolution rides the existing chokepoint.** It depends on
  `app.tenant.get_tenant` — the same `X-Tenant-Slug` header / custom-domain
  `Host` resolver every other portal route uses. One tenant's host can never
  return another's brand. Unauthenticated requests are exempt from
  `get_tenant`'s JWT `org`-claim cross-check by design (the cross-check only
  fires for an *employee* token), so the public read works on the login page.
- **Whitelisted fields only — structurally.** The response model **is**
  `BrandConfig`, which carries exactly the six non-sensitive, already-DOM-safe
  white-label fields (`product_name`, `logo_url`, `accent_color`,
  `accent_strong_color`, `support_url`, `legal_url`). No org settings, secrets
  (payment/ERP webhook secrets, extraction/SSO keys), or any other field can
  leak through it — a leakage test asserts the response key-set equals the
  `BrandConfig` whitelist. There is no enumeration surface: the resolver returns
  the one tenant the request already targets, and an unknown tenant is the same
  `404` as everywhere else.
- **Fail-soft.** It reuses `organization._resolve_brand` (the same validated
  parse the admin read/write path uses). The stored values were validated on
  write; a persisted-but-now-invalid block is re-validated here and falls back
  to all-empty (= platform defaults), so the portal always themes and never
  500s. No migration — brand lives in `Organization.settings.brand` JSONB.

### Frontend — the portal brand store

`frontend/src/lib/stores/portalBrand.svelte.ts` is the portal counterpart of the
employee `brand` store. It reads the public `GET /api/portal/branding` over
`portalApi` (rather than the JWT-gated `api`), and shares the **pure** theming
helpers (`brandThemeVars`, `isValidHexColor`) with the employee store via
`brandTheme.ts` — the accent-application + fallback logic is never duplicated.

The portal layout (`frontend/src/routes/portal/+layout.svelte`) runs an
`$effect` that calls `portalBrand.ensureLoadedAndApply()` once a tenant is
resolved (gated only on the tenant, not on auth, so it runs on the login page).
`applyTheme()` writes `--accent` / `--accent-strong` onto
`document.documentElement` only for valid configured colors (an unset/malformed
color leaves the AA-passing `app.css` token standing). The portal header renders
the tenant logo (when set) + product name, falling back to the FeohLedger mark
only while the product keeps the platform's name (`portalBrand.mark`, the same
`brandMark` fork as the employee sidebar); the `<title>` is
`{productName} — Supplier Portal`; and the login card
(`frontend/src/routes/portal/login/+page.svelte`) shows the same logo-or-mark + product-name
heading, on both the password and the MFA step. Fail-soft: any fetch failure degrades to the platform default theme.

The brand is keyed to the **tenant** (subdomain/Host), not the session, so
logout does not reset it — the login page the supplier lands on keeps the
buyer's theme.

### Tests

- Backend: `backend/tests/test_portal_branding.py` (realdb) — the public read is
  anonymous-accessible; returns ONLY the `BrandConfig` whitelist (the leakage
  guard, with sensitive `payments`/`extraction`/`sso` settings present in the
  org row to prove they never surface); fail-soft empty when unset; tolerates a
  malformed persisted brand block (no 500); and is tenant-scoped (tenant A's
  request never returns B's brand).
- Frontend: `frontend/tests-e2e/portal/branding.spec.ts` — sets a known brand
  via the admin `PUT /api/organization/branding` then asserts the portal login
  applies the accent (`--accent`/`--accent-strong` on `<html>`) + the
  product-name heading + `<title>` + the logo `<img>`.

## Branded outbound surfaces (PDFs + emails)

All outbound surfaces resolve brand through **one** helper —
`backend/app/services/branding.py::get_brand_context(org_settings)` — which
returns a frozen `BrandContext` (product name, logo URL, accent color, support /
legal URLs) with platform defaults baked in. It is **pure + total**: tolerates a
`None` settings dict, a missing / non-dict `brand` block, and individually
malformed fields (each falls back to its platform default for text/accent, or to
empty for URLs), and never touches the network. Platform defaults: product name
**"FeohLedger"**, accent **`#638cff`** (kept in sync with the frontend
`app.css` token).

### PDFs

`remittance_pdf.py`, `tax_1099_forms.py`, `audit_report_pdf.py`, and
`analytics_report_pdf.py` each take a resolved `BrandContext` on their render
context (defaulting to the platform brand so an old call site still renders) and
draw a branded header through `build_logo_flowable`, which follows
`BrandContext.mark`:

- **Tenant logo configured** — the logo, fetched and embedded best-effort (see
  below); a failed fetch falls back to the product-name text, as it always has.
- **No logo, product still named "FeohLedger"** — the bundled platform mark
  (`backend/app/assets/brand/logo-mark.png`, 256 px, rendered from
  `assets/logo-mark.svg`) beside the product name in the accent color. The mark
  is a glyph with no name of its own, hence the caption, set in the renderer's
  own header style. The bytes are read from disk once per process and never
  fetched, so this path has no network touch. `backend/.dockerignore` must keep
  `app/assets/`; `test_branding.py` fails if a pattern would drop the file.
- **Renamed without a logo** — the tenant **product name** in the **accent
  color** alone, exactly as before the mark existed.

The remittance footer also appends the tenant's support URL when set.

### Analytics report exports (CSV + PDF)

The analytics export surface — `GET /api/analytics/export/{report}` for
`invoice_register` / `vendor_spend` / `payment_register` / `aging_snapshot` /
`cashflow_forecast` — is white-label branded through the same
`get_brand_context` helper. The endpoint takes a `format` query param
(`csv` default | `pdf`); RBAC + tenant-scoping are unchanged
(admin/ap_manager/cfo, entity-scoped).

- **PDF** (`?format=pdf`) — `services/analytics_report_pdf.render_analytics_report_pdf`
  renders a branded, landscape, tabular PDF: a header with the tenant logo (when
  configured + embeddable, via the shared size/time-bounded `build_logo_flowable`,
  fail-soft to the product-name text in the accent color), the report title /
  org / period / generated-at, then the data table. It re-parses the CSV the
  exporter already produced into header + rows, so the PDF renders **exactly** the
  same cells the CSV dialect emits — never broader, no PII beyond what the CSV
  already carries.
- **CSV** (default) — CSV has no visual chrome, so branding is a leading
  **provenance comment block**: a handful of `# `-prefixed lines carrying the
  tenant **product name**, the org name, the report name, and the generated-at
  timestamp, prepended ahead of the unchanged data grid
  (`report_export.brand_provenance_header`). A `#`-comment header is the standard
  CSV-export provenance convention — the data grid (column header row + rows) is
  byte-for-byte the same as before, so it still parses column-positionally: a
  consumer that doesn't recognise comments skips the leading `#` lines
  (`csv.reader` yields them as single-cell rows; pandas takes `comment="#"`). The
  org name (the only tenant-supplied field in the block) is CR/LF-stripped so it
  can't inject a fake row. PII-free: product name + org + report + timestamp only.
  The pure per-report exporters in `report_export.py` are unchanged — the brand
  block is composed in the route, so passing `brand=None` keeps the legacy
  byte-for-byte output.

**Logo embed is best-effort and bounded** (`fetch_logo_bytes` /
`build_logo_flowable`): the fetch is time-bounded (`LOGO_FETCH_TIMEOUT_SECONDS`,
3s) and size-bounded (`LOGO_MAX_BYTES`, 1 MiB, enforced both via the
`Content-Length` header and a hard cap on the streamed bytes), only `http(s)`
URLs are fetched, and **any** failure (no URL, bad scheme, timeout, oversized,
non-2xx, undecodable image) returns `None` so the renderer falls back to the
product-name text. Only the tenant logo is ever fetched: the platform mark is
bundled, and a build that lost the file degrades to the same text header.
Logo embedding can never break PDF generation, and a dev box with no network
renders fine. Money stays exact and no PII enters the header / footer (brand
chrome only).

### Emails

The `EmailMessage` dataclass carries an optional `brand: BrandContext`. The
email adapters (`console` / `smtp` / `ses`) apply it uniformly via shared
`EmailAdapter` helpers (`_branded_from` / `_branded_html` / `_branded_text`):

- the **From** display name becomes the tenant product name
  (`Acme Pay <no-reply@platform.com>` — the deliverable address is unchanged;
  an address that already has a display name, or is empty, is left alone; the
  name is sanitized of quotes / CR / LF so it can't break the header);
- the **HTML** body is wrapped with a small brand header line (the mark
  `BrandContext.mark` picks, then the product name in the accent color) and a
  support-link footer (only when a support URL is set). The mark is a remote
  `<img>`, not an attachment. A tenant logo loads from its `logo_url`. The
  platform mark loads from `FEOH_PUBLIC_URL` + `/email-mark.png`, a 96 px PNG in
  `frontend/static/` drawn at 32 px, and is emitted only when `FEOH_PUBLIC_URL`
  is an absolute http(s) URL. A deployed env still on the loopback default would
  show recipients a broken image, just as its links already go nowhere. A tenant
  that renamed the product without a logo gets the name alone, byte-for-byte
  the header from before the mark existed. `alt` is empty because the name sits
  beside the image, so a screen reader doesn't announce it twice and a client
  that blocks remote images still shows the name;
- the **plaintext** body gets the same support-link footer.

A message with no `brand` set uses the **platform-default** brand, so every
email still presents consistently. The tenant-aware senders that resolve and
pass the brand: `notification_dispatch.notify_event` (invoice-lifecycle emails),
`vendor_notifications.notify_vendor_of_invoice_event` (supplier paid / rejected),
and `supplier_chat.notify_supplier_of_ap_message` (portal chat link). The
control-plane signup / MFA emails fire before a tenant brand exists, so they use
the platform default, and with it the platform mark. Brand resolution for emails
is best-effort — a load failure degrades to the platform brand, never breaking
the send.

## Tests

- Backend: `backend/tests/test_branding.py` — schema validation (hex/URL
  guards), admin-only mutate, persistence to `settings.brand`, PII-free audit
  row, 422 on bad hex/URL, 401 without auth, GET readable by any role; **plus**
  `get_brand_context` resolution + platform-default + malformed-field fallback,
  the remittance / 1099 / audit PDFs rendering the product name (+ logo-fetch
  failure falling back to text), and the email adapters branding the From /
  HTML header / support footer. **The mark fork**: `BrandContext.mark` for a
  tenant logo, the platform default and a renamed tenant; the bundled
  `logo-mark.png` being a 256 px RGBA PNG that `backend/.dockerignore` keeps;
  each of the four PDF renderers embedding the bundled mark without calling
  `fetch_logo_bytes`, drawing no image for a renamed tenant, and embedding the
  tenant's own image when a logo is set; the email header carrying the platform
  mark `<img>` from `FEOH_PUBLIC_URL`, none for a renamed tenant (byte-for-byte
  the old header), the tenant logo when set, and none when `FEOH_PUBLIC_URL` is
  not http(s). **Analytics exports**:
  `backend/tests/test_report_export.py` (the `brand_provenance_header` block —
  product name + metadata, `None`-brand no-op, the data grid still parsing
  column-positionally below the comment block, org-name newline-injection
  sanitised), `backend/tests/test_analytics_report_pdf.py` (the pure PDF renderer
  — real PDF bytes, configured + default brand, **logo-fetch failure fail-soft**,
  empty rows), and `backend/tests/test_cashflow_forecast_api.py` (the route —
  `format=pdf` content-type + filename, the branded CSV provenance block,
  `format=xlsx` → 422).
- Frontend: `frontend/src/lib/stores/brandTheme.test.ts` — the pure
  color-application / fallback logic (`isValidHexColor`, `brandThemeVars`),
  and the fallback-mark fork (`brandMark`: logo, platform mark, monogram).

## Custom domains (vanity hostnames)

A tenant can be reached on its own hostname — `ap.acmecorp.com` — instead of (or
in addition to) the `*.localhost` / `<slug>.app.com` subdomain. This is the
white-label "served under the partner's own domain" ask. The TLS certificate and
DNS for the vanity host are **infra** concerns (CloudFront/ALB + ACM + a CNAME to
the platform) and out of scope for the app code; the app's job is purely to map
an inbound request on that host back to the right tenant.

### Storage — no migration

The vanity hostnames live on `Organization.settings.brand.custom_domains`, a JSON
array of bare, lowercase hostnames, e.g.:

```json
{ "brand": { "custom_domains": ["ap.acmecorp.com", "pay.acmecorp.com"] } }
```

Like every other white-label field, this is settings JSONB — **no migration**. A
malformed / non-array `custom_domains` value never breaks resolution (see below).

### Resolution — `app/tenant.py`

`get_tenant_slug` resolves the tenant selector for every request, in order:

1. **Primary** — the `X-Tenant-Slug` header (set by the SPA from the subdomain).
   If present, it is used verbatim and the custom-domain lookup is skipped (no DB
   query).
2. **Fallback** — when the header is absent, the request `Host` header is
   normalized (`normalize_custom_domain`: strip `:port`, lowercase, reject empty /
   IPv6-literal / malformed values) and matched against the per-org
   `settings.brand.custom_domains` array via a JSONB `@>` containment query
   (`resolve_tenant_slug_by_custom_domain`). A match yields the owning org's slug.
3. An unknown/unmatched host (or a malformed settings blob — the lookup catches
   and swallows the error) **falls back to the original 400** "Missing
   X-Tenant-Slug header". It never resolves a wrong tenant and never 500s.

The resolved slug then flows through the **unchanged** `get_tenant` →
`get_tenant_db` chain — the tenant engine is still built only through the existing
`get_tenant_engine(tenant.db_name)` chokepoint, never by a hardcoded DB name.

### Managing the list — admin endpoint + UI

The `custom_domains` list is managed through a dedicated admin endpoint pair
(distinct from `GET/PUT /api/organization/branding`, because `custom_domains`
lives under `settings.brand` but is **not** a `BrandConfig` field):

`GET /api/organization/branding/custom-domains` — returns
`{ "custom_domains": [...] }`. **Read-gated to any authenticated org user**
(same posture as `GET .../branding`) — the resolver reads it too.

`PUT /api/organization/branding/custom-domains` — **admin-only**
(`require_roles(ROLE_ADMIN)`). Full-replace semantics. Each host is normalized
through the **same** `normalize_custom_domain` the resolver uses (strip `:port`,
lowercase, reject empty / IPv6-literal / malformed) — so a stored value can never
diverge from what actually resolves. Malformed entries are rejected (`422`); the
normalized list is de-duplicated.

**Cross-org uniqueness (anti-hijack).** A host already registered to a
*different* org is rejected (`409`). A custom domain is only a *candidate*
tenant selector — the JWT `org`-claim cross-check (below) is what actually gates
access — but letting two orgs claim the same host would make resolution
ambiguous, so it is refused at registration time, queried via the **same** JSONB
containment the resolver uses (so the check and the resolution can't disagree).
Re-saving a host the tenant already owns is **not** a self-conflict. The `409`
body is **generic** ("…already registered to another tenant") — it never echoes
the conflicting hostname back, so it can't confirm to the caller that a specific
host belongs to some other org. The check-and-write is serialized by a
transaction-level `pg_advisory_xact_lock`, so two orgs PUT-ing the same host
concurrently can't both race past the guard (the lock is held to commit and
auto-releases on rollback; no DB constraint is needed since the domains live in
a JSONB array, and admin-config writes are rare enough that one global lock is
cheap).

**This endpoint is the only writer.** `PATCH /api/organization` takes a
free-form `settings` dict and shallow-merges it, so a
`{"brand": {"custom_domains": [...]}}` payload replaced the whole `brand` key
and stored vanity hostnames with **none** of the controls above — no
normalization, no advisory lock, no cross-org uniqueness check, no audit row.
Since anyone can self-signup a tenant and become its admin, that let an
attacker claim a hostname already registered to a victim: with two orgs
matching, `resolve_tenant_slug_by_custom_domain` returned `.scalars().first()`
with no ordering, so requests on that host that carry no `X-Tenant-Slug` — which
is exactly what the SPA sends on a two-label vanity apex — could resolve to the
attacker's org. The unauthenticated `GET /api/portal/branding` would then serve
the *attacker's* product name, logo and support/legal URLs on the victim's own
supplier-portal domain (a ready-made phishing surface aimed at the victim's
suppliers), and `POST /api/portal/auth/login` would resolve to the attacker's
tenant DB so the victim's suppliers could no longer sign in. The JWT
`org`-claim cross-check does not help here: these are the documented
public-by-design routes where it is deliberately skipped.

`PATCH /api/organization` now refuses a `brand.custom_domains` key with a 422
naming this endpoint — the same treatment it already gave `chat_notifications`,
and for the same reason (a second, unaudited writer). The resolver additionally
takes a deterministic `ORDER BY`, so a duplicate that predates the fix resolves
consistently instead of flapping.

Every mutation audits `organization.custom_domains_updated` into the tenant
trail, **PII-free**: it records only the host **count** (old → new), never the
hostnames themselves (tenant infra config kept out of the trail). A branding
save (`PUT .../branding`) **preserves** `custom_domains` — it carries the
existing list forward rather than letting `BrandConfig.model_dump()` wipe it.

**Operator responsibility — DNS + TLS.** Registering a host here only tells the
platform which tenant that host maps to. The vanity host's **DNS** (a CNAME to
the platform edge) and **TLS certificate** (CloudFront/ALB + ACM, or Let's
Encrypt via Caddy on the single-VM shape) are infra, provisioned out of band —
see *Custom domains* intro above. The step-by-step operator procedure for both
deployment shapes — DNS records, certificate issuance + renewal, the CORS env
change, end-to-end verification, rollback and troubleshooting — is
[`docs/founder-runbooks/custom-domain-provisioning.md`](founder-runbooks/custom-domain-provisioning.md).

### The SPA half — `PUBLIC_PLATFORM_DOMAINS`

The backend's `Host` fallback only ever fires when `X-Tenant-Slug` is **absent**,
so the vanity host works or doesn't work depending entirely on what the SPA
decides to send. It used to decide wrong. `getTenantSlug()` took the **first
label of any 3+-label hostname**, which cannot tell a platform subdomain from a
customer's own domain: on `ap.acmecorp.com` (the tenant `acme`) it sent
`X-Tenant-Slug: ap` and every call 404'd `Unknown tenant: ap`, with the `Host`
fallback never reached; on a two-label apex (`acmecorp.com`) it sent no header
but still called the **build-time** `PUBLIC_API_URL` origin, so the backend saw
the platform's `Host`, not the vanity one, and answered the 400. Custom domains
were unreachable from the SPA in both shipped deployment shapes.

The SPA is now **platform-domain aware**. `PUBLIC_PLATFORM_DOMAINS` is a
comma-separated list of the registrable domains the *platform* serves, and
`frontend/src/lib/hostRouting.ts` sorts every hostname into one of four kinds:

| Host | Kind | `X-Tenant-Slug` | API origin |
|---|---|---|---|
| `acme.feohledger.com`, `acme.localhost:7777` | platform tenant | `acme` | build-time `PUBLIC_API_URL` |
| `feohledger.com`, bare `localhost` | platform apex (marketing / signup) | none | build-time `PUBLIC_API_URL` |
| `ap.acmecorp.com`, `acmecorp.com` | **vanity** | **none** | **same origin (`''`)** |
| no `window` (prerender) | unknown | none | build-time `PUBLIC_API_URL` |

Both halves matter and neither is sufficient alone. Sending no header is what
lets the backend's `Host` lookup run at all; calling `/api` **same-origin** is
what puts the *vanity* hostname in the `Host` header for it to look up. A vanity
host that called the build-time API origin would hand the backend the platform's
own hostname and get the 400 anyway. `frontend/src/lib/tenant.ts::getApiBase()`
is the single owner of that decision, resolved per request at **runtime** rather
than frozen into a module constant at build time, and `$lib/api.ts` +
`$lib/portalApi.ts` both read it — there is no second spelling.

**Two things the operator must do**, beyond the DNS + TLS below:

1. **Build the frontend with `PUBLIC_PLATFORM_DOMAINS` set** to the platform's
   own domain(s) — e.g. `PUBLIC_PLATFORM_DOMAINS=feohledger.com pnpm build`. See
   `docs/environment.md`. **Unset is legal and means "the old rule"**: an empty
   list replays the pre-change behaviour byte-for-byte, so an existing build that
   passes only `PUBLIC_API_URL` is unchanged — and custom domains stay
   unreachable from the SPA until the var is set. That default is deliberate: if
   an unset list meant "every host is a vanity host", every deployment would stop
   sending `X-Tenant-Slug` on upgrade and fall back to a `Host` map with no
   entries.
2. **Serve `/api` on the vanity origin.** Because a vanity host calls its own
   origin, the edge terminating `ap.acmecorp.com` must proxy `/api/*` through to
   the backend (CloudFront behaviour / ALB rule, or the `handle_path` block in
   `deploy/Caddyfile` on the single-VM shape). Without it every API call hits the
   static site and 404s. There is no CORS change to make for this path — the
   request is same-origin by construction. **Locally, `frontend/vite.config.ts`
   does this for you** — `server.proxy` / `preview.proxy` forward `/api` to
   `PUBLIC_API_URL` with `changeOrigin: false`, so the vanity `Host` survives to
   the backend's lookup. Without that a custom domain could not be exercised on a
   dev laptop at all.

**The hostname is now free-form.** `ap.acmecorp.com` no longer has to be
`<tenant-slug>.<customer-domain>`, because the first label is never read as a
slug on a non-platform host; a two-label apex (`acmecorp.com`) works too. The
one remaining shape rule is the backend's own `normalize_custom_domain` (bare,
lowercase, no port, no IPv6 literal).

**Consequences elsewhere in the SPA.** A vanity host has a tenant but no slug, so
anything that keyed off the slug needs the host instead. `$lib/entity.ts`
partitions its stored multi-entity selection by
`$lib/tenant.ts::getTenantStorageKey()` — the slug on a platform host, the
hostname on a vanity host — rather than by `getTenantSlug()`, which would have
silently disabled entity persistence on every vanity host.

**Admin UI** — the **Custom Domains** section on `/organization`
(`frontend/src/routes/organization/+page.svelte`, below the Branding panel):
lists the current hostnames, adds one (client-side validated to mirror the
backend `normalize_custom_domain`, surfacing a typo inline instead of as a
`422`/`409`), and removes one (armed two-click confirm). Loading / error / empty
states; the operator's DNS+TLS responsibility is called out in the panel hint.

### Trust model — the JWT cross-check still gates everything

This is the load-bearing point. A custom domain resolves only a **candidate**
tenant slug; it does **not** grant access. `get_tenant` still performs the
cross-tenant guard: for an employee JWT (`typ != "vendor"`), the token's `org`
claim **must equal** the resolved Organization's id, or the request is rejected
with `403`. So:

- A forged / leaked `Host: ap.acmecorp.com` header, on its own, can no more widen
  access than a forged `X-Tenant-Slug: acme` header can — both only pick a
  candidate; the JWT org claim is the actual authority. An attacker holding a
  techflow JWT who points a request at acme's vanity host still gets a 403.
- Vendor-portal tokens (`typ="vendor"`) and unauthenticated requests are exempt
  from the cross-check exactly as before (VendorUser rows are tenant-local;
  unauthenticated requests are rejected by the downstream auth dependency). The
  custom-domain fallback changes *how the slug is derived*, not the guard that
  follows it.

In short: custom-domain resolution is a convenience for picking the tenant when
the SPA can't supply the header; it is **not** a new trust boundary, and the
existing tenant-isolation invariant (project invariant #4) is fully preserved.

### Tests

`backend/tests/test_tenant_custom_domain.py` — `normalize_custom_domain`
edge cases (port strip, case-fold, IPv6/malformed reject); the header taking
priority and skipping the DB lookup; a custom domain resolving to the right slug;
unknown domain / no host falling back to 400; a malformed settings blob not
500-ing; and — the security headline — that the JWT `org`-claim cross-check still
rejects a mismatched token on the custom-domain path (and allows a matching one).

`backend/tests/test_custom_domains_admin.py` — the management endpoint: GET
readable by any authed role + 401 without auth; PUT admin-only (403 for a
manager); normalize + de-dupe + persist to `settings.brand.custom_domains`;
PII-free `organization.custom_domains_updated` audit (count only); round-trip
through GET; 422 on a malformed host; clearing the list; **cross-org uniqueness
409** (tenant B claims a host → tenant A is refused, A's list unchanged);
re-registering a host the tenant already owns is fine; and the regression that a
branding save preserves `custom_domains`.

`frontend/tests-e2e/organization/custom-domains.spec.ts` — the panel renders;
adding a host through the UI round-trips through GET; removing one (armed
confirm) drops it; an invalid hostname surfaces an inline error and fires no PUT.

`frontend/src/lib/tenant.test.ts` — the SPA half, over the dependency-free
`$lib/hostRouting.ts`: the four host kinds; a vanity 3-label host and a vanity
apex both yielding **no** slug; the `*.localhost` dev convention and the deployed
platform subdomain still yielding one; longest-platform-domain-first matching;
the API-base resolver (platform host → build-time `PUBLIC_API_URL`, vanity host →
same origin); the per-tenant storage key; and — the upgrade guard — that an unset
`PUBLIC_PLATFORM_DOMAINS` replays the pre-change rule exactly.
`frontend/src/lib/tenantSlugUsage.test.ts` adds two whole-tree scans: nothing
outside `hostRouting.ts` splits a hostname into labels, and nothing outside
`tenant.ts` imports the build-time `PUBLIC_API_URL` (bar a ratcheted two-file
baseline), so a second spelling of either rule can't reappear.

`frontend/tests-e2e/tenant/vanity-host.spec.ts` — **both halves** end-to-end.
The platform half: every `/api` call from `<slug>.localhost:7777` still carries
`X-Tenant-Slug: <slug>` and still targets the build-time API origin rather than
collapsing to same-origin. The vanity half: from a non-platform origin the SPA
sends **no** slug header and calls `/api` on its own origin.

Two things make the vanity half reachable, and both are worth knowing before
touching the harness:

- **`PUBLIC_PLATFORM_DOMAINS` reaches both run modes.** Locally `pnpm dev` gets
  it from `playwright.config.ts`'s `webServer.env`; in CI a production `vite
  build` bakes it into `build/_app/env.js` from the build step's own env
  (`adapter-static` has no server to read `$env/dynamic/public` at runtime, and
  a production build does not read `.env.development`). Both take the value from
  `tests-e2e/fixtures/env.ts::PLATFORM_DOMAINS` so they cannot disagree — which
  matters because unset means "no host is a vanity host", and the same
  navigation would otherwise assert opposite things in the two environments.
  The failure is loud, not silent: with the variable missing the vanity origin
  classifies as a platform host and the spec's own assertions fail.
- **The vanity origin is the loopback IP literal**
  (`fixtures/env.ts::VANITY_ORIGIN`). Every hostname the harness can reach is
  `*.localhost`, and `localhost` is exactly what `PUBLIC_PLATFORM_DOMAINS`
  declares, so no `.localhost` name can classify as vanity — while an IP literal
  is never a platform host unless listed verbatim, Vite serves it with no
  `allowedHosts` entry, and it needs no DNS. The code path is identical to a
  real vanity hostname's.

What the spec deliberately does **not** assert is that the backend then resolves
a tenant from that `Host` — that needs the origin registered in a tenant's
`custom_domains`, a cross-org-unique write, and it is covered on the backend
side by `test_tenant_custom_domain.py`.

## Partner / reseller admin

A **partner** (reseller) org administers a set of branded **child** tenants —
white-labeling a workspace and then operating its sub-tenants' branding under
one parent account. This is the "one partner manages many branded tenants" ask.

### The relationship — a control-plane self-FK

Orgs live in the **control plane**, so the parent/child link is a nullable
self-referential FK on `Organization`: **`parent_org_id`** (migration `0065`,
control-plane-only). When set, that org is a branded **child** administered by
its parent; NULL = a standalone tenant.

A "partner" is **derived, not flagged** — it is simply any org referenced as a
`parent_org_id` by ≥ 1 child. There is no separate `is_partner` column an admin
could flip to claim children it didn't actually parent. The link is now
**created in-app** via the provisioning flow below (no raw DB statement needed) —
see *Provisioning the link* — guarded so a partner can only attach a tenant that
explicitly consented.

The migration is control-plane-only (gated on the `organizations` table
existing, exactly like `0062`/`0055`) — it does **not** fan out to tenant DBs;
`organizations` never exists in a tenant DB. The FK uses `ON DELETE SET NULL`, so
deleting a partner org orphans its children back to standalone rather than
cascading a delete.

### Backend — `/api/partner` (`backend/app/api/partner.py`)

Three admin-only (`require_roles(ROLE_ADMIN)`), JWT-gated endpoints on the
control-plane `organizations` table:

`GET /api/partner` — the caller's partner overview: its identity + the child
tenants it administers (`PartnerOverview` → `is_partner` + `children[]`, each
carrying id / name / slug / plan / the child's resolved white-label
`product_name`). A standalone org gets `is_partner: false` + an empty list (a
state the UI renders, not an error).

`GET /api/partner/children/{child_id}/branding` — read one child's `BrandConfig`
(reuses `organization._resolve_brand`, tolerant of a missing/malformed block →
platform defaults).

`PUT /api/partner/children/{child_id}/branding` — push a child's branding.
Pydantic validates the payload (hex colors, http(s) URLs); the write lands on the
**child** org's `settings.brand` (control plane) and **preserves**
`custom_domains` (the same carry-forward the child's own `PUT
/api/organization/branding` does, so a partner save can't wipe the child's vanity
hostnames). Audited into the **child's** tenant trail as
`organization.branding_updated` with `via: "partner"` + the acting
`partner_org_id` — **PII-free** (which fields are now set, booleans only; never a
raw value).

### Provisioning the link — two-sided consent (attach + detach)

The partner surface now **creates** the parent/child link, not just administers
an existing one. Three more admin-only endpoints
(`require_roles(ROLE_ADMIN)`, JWT-gated via `get_tenant`):

`POST /api/partner/link-code` — mint a **single-use link code** FOR the caller's
**own** org. Handing that code to a prospective partner IS the act of consenting
to be adopted. Returns `{link_code, expires_in_minutes}`. The code is an HMAC-
signed token (`backend/app/services/partner_link_token.py`, pure, modelled on the
email-action token) over the caller's org id only — **no name/slug/PII** — with a
short TTL (`FEOH_PARTNER_LINK_TTL_MINUTES`, default 30). A **503** when no signing
key is configured (feature off). Issuing is audited PII-free
(`partner.link_code_issued`).

`POST /api/partner/children` — the partner's admin **redeems** a child-issued
code to attach that child. The signature is verified first (an invalid / expired
/ wrong-purpose / cross-key code is one opaque **400** — no enumeration), then the
`jti` is claimed **single-use** in Redis (fail-closed — a Redis blip rejects
rather than risk a replay). Guards: a child already linked to a parent is a **409**
(no silent takeover; re-linking to the *same* partner is the idempotent no-op,
201), and an org can't adopt itself (400). On success `child.parent_org_id` is
set and the change is audited on **both** trails (`partner.child_attached` on the
partner's, `partner.parent_linked` on the child's — org ids only).

`DELETE /api/partner/children/{child_id}` — detach a child (back to standalone,
`parent_org_id = NULL`). Scoped at the data layer to the caller's own children
via `_resolve_child` (a non-child / unknown id is the same opaque **404**).
Idempotent (a second detach is a clean 404 — the link is already gone). Audited
on both trails (`partner.child_detached` / `partner.parent_unlinked`).

`POST /api/partner/children/provision` — provision a brand-**NEW** child tenant
already parented to the caller (the new-tenant counterpart of `attach_child`,
which adopts an *existing* consenting org). Admin-only. Body mirrors
`scripts/create_tenant.py`: `{name, slug, admin_email}` (`admin_name`/`plan`
optional) — there is **no `parent_org_id` input**; the new tenant is ALWAYS
parented to `org.id` from the `get_tenant` chokepoint, so a partner can only ever
create a child UNDER ITSELF. Flow: validate the admin-email shape + the slug
(format + reserved-word + availability, same `utils/slug` checks signup uses) →
provision the full tenant via the shared
`services/tenant_provisioning.provision_tenant` primitive (control-plane org +
admin user + the `feoh_<slug>` tenant DB + tables) → stamp `parent_org_id = org.id`
→ audit `partner.child_provisioned` on the partner's trail + `partner.parent_linked`
(`via:provision`) on the new child's, PII-free (org ids + slug only, never the
admin email or password). **Failure path is clean** — `provision_tenant` owns the
orphan-DB rollback (it drops the `feoh_<slug>` DB it created on any partial
failure), so we never half-create; a slug that races past the pre-check trips the
unique constraint inside provisioning → a clean **409** (not a 500). An invalid
slug is **422**, a taken slug **409** (the only enumeration surface is the
partner's *own* choice of slug, which public signup already exposes — not a
cross-tenant leak). Returns the `ChildTenantSummary` plus the new admin's
`admin_email` + a one-time `temp_password` (returned EXACTLY once, like an API-key
mint — the new admin rotates it on first login via `must_change_password`). No
migration (the `parent_org_id` column already exists).

**Why this authorization model is safe (the privilege boundary).** The hard
question attach poses: who may declare org X a child of partner P? Letting any
partner admin unilaterally adopt an arbitrary org would be a cross-tenant
takeover. There is also **no platform-operator / superuser identity** in this
app — every user is org-scoped — so a pure "operator grants the link" path has no
actor to run it. The durable answer is **two-sided consent**: the prospective
child's *own* admin must first mint a link code (proof of consent); the partner's
admin then redeems it. Because the platform holds the signing key (sops + KMS in
deployed envs; a NON-secret value committed in `.env.development`), a partner
**cannot forge a code or aim it at an org that never consented** — an attach with
no/garbage/forged code is rejected and no link is created. The key's presence is
the single on/off knob (`FEOH_PARTNER_LINK_SIGNING_KEY`), fail-closed with no
hardcoded fallback. The link is a control-plane write only; no migration was
needed (the `parent_org_id` column already exists).

### Trust model — isolation at the data layer

This is the load-bearing point and it composes with the existing tenant-isolation
invariant:

- The caller's org is resolved by the standard **`get_tenant`** chokepoint, which
  cross-checks the JWT `org` claim against the resolved tenant — so a partner
  admin authenticates as, and can only act as, *their own* partner org. A
  swapped `X-Tenant-Slug` or forged `Host` can't widen that (same guard as
  everywhere else).
- EVERY child query is then scoped at the data layer:
  `Organization.parent_org_id == <caller org id>` (in
  `partner._resolve_child`'s WHERE clause, not in post-hoc app code). A partner
  can never read or mutate an org it didn't parent.
- A non-child (or unknown) `child_id` is the **same opaque 404** as a missing
  one — no cross-tenant enumeration of org ids.

So a partner holding org A's JWT pointing at org B's id gets a `404`; only orgs
whose `parent_org_id` is A are reachable. Branding pushed to a child is audited in
*that child's* trail, attributable to the partner.

### Frontend — `/admin/partner`

The **Partner Admin** page (`frontend/src/routes/admin/partner/+page.svelte`,
under the Settings nav group, admin-gated — non-admins redirect to `/`, the
backend 403s them regardless). Lists the partner's child tenants (`DataTable`,
clickable rows) and opens a `Modal` to view/edit a child's brand (product name,
logo URL, two accent colors, support + legal URLs) with the same client-side
hex/URL validation as the org's own Branding panel, saving via `PUT
/api/partner/children/{id}/branding`. A standalone org renders the empty
"not a partner" state. API client: `frontend/src/lib/api/partner.ts` (types in
`frontend/src/lib/types/partner.ts`). Built from the shared `ui/` components.

The page also carries the **provisioning** affordances: a **Generate link code**
panel ("Join a partner" — this workspace consenting to *be* a child; copies the
minted code), a **+ Create child tenant** toolbar button opening a `Modal` to
provision a brand-new tenant under this partner (name / slug / admin email →
`POST /api/partner/children/provision`; the result view reveals the one-time temp
password with a copy button, shown only once), an **+ Attach child** toolbar
button opening a `Modal` to paste a child-issued code (`POST /api/partner/children`),
and an armed two-click **Detach** `RowAction` per child row
(`DELETE /api/partner/children/{id}`).

### Tests

- Backend: `backend/tests/test_partner_admin.py` (realdb) — overview lists ONLY
  the caller's own children; a standalone org is `is_partner:false` + empty;
  admin-only (403 for a manager) + 401 unauth; read + push a child's branding;
  push persists to the child's row, audits the child's trail PII-free
  (`via:partner`, raw value never echoed), and preserves the child's
  `custom_domains`; **the isolation headline** — a non-child org id is an opaque
  404 on both read and write, and the non-child's brand is untouched.
- Backend (provisioning): `backend/tests/test_partner_link_token.py` (pure —
  build/verify round-trip, fail-closed on empty key, forgery/tamper rejection,
  purpose binding, expiry) + `backend/tests/test_partner_link_provisioning.py`
  (realdb — mint admin-only/fail-closed; attach with a consenting code links +
  audits both trails PII-free; **the authorization headline**: attach with no /
  garbage / cross-key-forged code is rejected and NO link is created; single-use
  replay → 409; re-parent guard → 409 with same-partner idempotent no-op; detach
  unlinks + audits, non-child opaque 404, admin-only).
- Backend (new-tenant provisioning): `backend/tests/test_partner_provision.py`
  (realdb — provisioning creates the org + tenant DB and stamps
  `parent_org_id = caller`; the child appears in the partner overview; both
  trails audited PII-free (`child_provisioned`/`parent_linked`, never the admin
  email or temp password); admin-only (403) + 401 unauth; slug collision → clean
  409 with the existing tenant undisturbed; invalid slug / email → 422. Each test
  drops the provisioned DB + control rows in a `finally`).
- Frontend: `frontend/tests-e2e/admin/partner.spec.ts` (branding admin surface) +
  `frontend/tests-e2e/admin/partner-provisioning.spec.ts` — admin sees the
  attach + link-code affordances and mints a code; the authorization boundary
  (a garbage code is an opaque 400, no link created; a self-mint can't self-
  attach); a clerk is 403'd on mint + attach.

## Deferred to later slices

- The operational runbook for the TLS-cert + DNS provisioning that pairs with a
  registered custom domain (the app-side `custom_domains` admin UI + endpoint
  pair shipped — see *Managing the list* above; the infra automation that issues
  the ACM cert and wires the CNAME is a separate, infra-owned slice).
- **Self-service "create a *new* child tenant under my partner account"** —
  **shipped.** `POST /api/partner/children/provision` (see *Provisioning the
  link* above) is the thin wrapper over
  `services/tenant_provisioning.provision_tenant` that stamps `parent_org_id` +
  audits both trails, reusing the same admin gate; the `/admin/partner` panel's
  **+ Create child tenant** modal drives it. A partner can now spin up a
  net-new branded tenant already parented to it in one step (the new-tenant
  counterpart of the attach/detach provisioning of *existing* tenants). No
  migration (the `parent_org_id` column already exists). The DNS/TLS automation
  for the new tenant's custom vanity domain is still the separate infra-owned
  slice noted above (the app-side custom-domain registration works today).
