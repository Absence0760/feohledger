# Accessibility conformance statement

**Last reviewed:** 2026-09-16
**Conformance target:** [WCAG 2.2](https://www.w3.org/TR/WCAG22/) **Level AA**
**Self-assessment status:** Partial — automated coverage in CI; manual
screen-reader audit in progress (see [Known limitations](#known-limitations)).

We are committed to making the FeohLedger accounts-payable
platform usable by everyone, including people who rely on assistive technology
(screen readers, screen magnifiers, switch access, voice control) or who
navigate without a mouse. This page is our public conformance statement; the
detailed criterion-by-criterion breakdown lives in the companion
[Accessibility Conformance Report / VPAT](./accessibility-vpat.md).

## Scope

This statement covers the three end-user surfaces of the product:

| Surface | Stack | Notes |
| ------- | ----- | ----- |
| Web app (AP staff) | SvelteKit 2 / Svelte 5, static SPA | The full invoice → approve → pay workflow, vendors, payments, exceptions, analytics, admin. |
| Supplier portal | SvelteKit (same app, `/portal/*`) | Vendor self-service: invoice submission, payment history, virtual-card reveal, supplier chat. |
| Mobile app | Flutter 3.41+ (iOS + Android) | Core approval workflow, camera OCR, push notifications. |

Marketing / signup pages (`/`, `/signup`, `/verify`) and the published legal
documents (`/legal/*`, including the statement this file backs) are in scope as
part of the web surface; all six legal routes are axe-scanned at two widths. Third-party embedded surfaces we do not control (an external
IdP's hosted SSO login, a payment processor's hosted card-capture page) are out
of scope for this statement; where we hand off to them we note it.

## How we test

Accessibility is verified at five layers — three automated regression guards in
CI plus two kinds of human review:

1. **Automated (axe-core, every CI run).** The Playwright e2e suite includes an
   accessibility regression guard at `frontend/tests-e2e/a11y/axe.spec.ts`. It
   runs [`axe-core`](https://github.com/dequelabs/axe-core) (via
   `@axe-core/playwright`) against the key authenticated surfaces (dashboard,
   invoices list, vendors, payments, exceptions, the invoice detail modal, the
   whole `/admin` section — Users & Roles, API Keys, Webhooks, Partner Admin —
   plus `/vendor-statements` and its create modal, `/billing`, `/reports`,
   `/experiments` and `/cfo`) and the two unauthenticated
   login surfaces (AP login, supplier portal login),
   asserting **zero violations** at the WCAG 2.0 / 2.1 / 2.2 Level A + AA tag
   set (`wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`, `wcag22aa`). It runs on
   every push as part of the normal e2e job, so a change that reintroduces a
   machine-detectable barrier (missing label, low contrast, ARIA misuse,
   broken landmark/heading structure, undersized target) fails CI. Run it
   locally with `pnpm test:e2e:a11y` (needs the local stack up — see
   `frontend/tests-e2e/README.md`).

   The `/admin` routes were added because that section holds the app's densest
   cluster of a11y-sensitive controls — modal dialogs, armed two-click
   destructive actions, and one-time secret reveals whose focus management is
   the only thing between a user and a credential they can never see again —
   and had no coverage at all. Each route in the list can carry a `ready`
   heading the spec waits on **in addition to** the sidebar: these pages fetch
   before they render, and the sidebar alone would let axe scan a loading frame
   and pass on markup no user ever sees. `/vendor-statements` is scanned as a
   list *and* with its create modal open, since the radio `fieldset`/`legend`
   intake picker, the file input and the persistent `role="alert"` refusal
   region all live in the dialog.
2. **Automated (colour-token contrast scan, every CI run).** A route list will
   always trail the app, and a contrast failure recurs per **surface** rather
   than per route — `--text-muted` on `--surface-2` was 4.34:1 everywhere it
   appeared, so axe caught it on the two `/admin` pages inside the list and
   missed the identical defect on `/billing`, which wasn't. So the palette is
   checked at the source: `frontend/src/lib/a11y/tokenPairing.test.ts` (vitest,
   `pnpm test:unit`, no browser needed) scans **every** stylesheet in `src/` —
   `app.css` plus every component `<style>` block — for a rule that sets both a
   `color` and a `background`, resolves both through the palette, and fails
   below the SC 1.4.3 threshold (3:1 when the rule declares a large-text size).
   A rule that sets only a `color` inherits its background, so a **literal**
   there is instead held against the surfaces body text renders on (`--bg`,
   `--surface`) — which is how the old status red, 4.11:1 in 106 places, was
   found. It also fails a `var(--token, fallback)` whose fallback contradicts
   the declared token, and a `var()` on a token nothing ever assigns — the
   drift that let `--surface-2` render for months as a value nobody declared.
   The palette's own contract (which foreground token is legal on which
   surface) is asserted directly, so one drifting token surfaces as one
   failure rather than dozens.

   A colour that only exists **after compositing** is measured too, in the same
   pass: a rule that fades itself with `opacity`, and a rule that tints its
   background translucently. The latter is the status-badge recipe, and it
   fails in a way nobody spots by eye — the tint lightens the dark surface
   toward text set in the same tone, so 29 badges rendered between 4.15:1 and
   4.48:1, short by a hair rather than obviously wrong. Each tone therefore
   carries a `--<tone>-tint` background paired with the `--<tone>-on-tint` text
   calibrated on it, asserted over both backdrops a badge sits on. See
   [decisions.md](decisions.md) §30.

   The two guards are complements, not alternatives: the scan can't resolve the
   cascade (a rule setting only `color` inherits its background at runtime),
   and axe can't see a surface no listed route renders. Rationale + what was
   rejected: [decisions.md](decisions.md) §28; the token table and the rule for
   authors are in `frontend/docs/ui-patterns.md` § Colour tokens and contrast.

   One hole neither guard can close: white-label theming lets a tenant
   overwrite `--accent` / `--accent-strong` at runtime. Both surfaces that edit
   those colours (`/organization` Branding, the `/admin/partner` child-branding
   modal) show the real white-on-colour ratio as an inline advisory before it
   is saved — advisory, because the brand is the tenant's call.
3. **Automated (opacity de-emphasis scan, every CI run).** The two guards
   above share one blind spot, and it is the app's most-repeated de-emphasis
   idiom. `opacity` is *group* opacity: it composites an element's whole
   subtree onto the surface behind it, so the rule that spends the contrast
   (`tr.inactive td { opacity: 0.6 }`) declares no colour at all — the colours
   it ruins belong to descendants, often in another file. The token scan
   measures a rule against its own declarations and read it as harmless; axe
   only sees it if a listed route happens to render an inactive row, and a
   fresh test tenant has no paused subscription, no revoked key and no
   deactivated user. Measured on `--surface`, the fade inverted its own
   intent — `--text` 5.65:1 at 0.6, but `--text-muted` 2.77:1 and a tinted
   status badge 2.78–2.93:1, so the one cell explaining *why* a row was faded
   became the least readable thing in it. Those rows now take the shared
   `.row-muted` recipe (a muted colour token, 5.38:1), and
   `frontend/src/lib/a11y/opacityAudit.test.ts` (vitest, `pnpm test:unit`)
   scans every stylesheet for the idiom returning. It reports the *pattern*
   rather than a ratio — it cannot know a descendant's colour, and does not
   need to, since the answer for text is always a muted token and never a
   kinder alpha. `opacity` on an inactive control (exempt under 1.4.3), on a
   transient `:hover`, or on text-free decoration is filtered out by rule;
   the remaining judgement calls are two named allowlists, each entry
   carrying its reason. `frontend/tests-e2e/a11y/deemphasised-rows.spec.ts`
   is its runtime complement, stubbing the list responses on
   `/admin/webhooks`, `/admin/api-keys` and `/admin` so a de-emphasised row
   is guaranteed on screen when axe runs.
4. **Automated (target-size scan, every CI run).** WCAG 2.2's SC 2.5.8 accepts
   either a 24×24 CSS-px target or, through its *spacing* exception, an
   undersized one whose 24px-diameter circle reaches no other target.
   `input[type=checkbox]` is painted 16×16, so every row-select and select-all
   checkbox in the app used to conform through the exception — and whether it
   did was decided by whatever padding each page gave its checkbox column, not
   by the design system: `/invoices` left 7px of clear space around the box,
   `/exceptions` (`width: 32px; padding-right: 0`) left 2px, and 4px is the
   threshold. A criterion a page's padding can flip is not one the design
   system is meeting, so the checkbox now satisfies the **size** half outright.
   Its own box is 24×24 — a 4px transparent border plus
   `background-clip: padding-box`, so the painted control is still 16×16 — and
   a matching `margin: -4px` returns those 8px to the layout, leaving the
   margin box (what a line box, a flex row or a table cell measures) at 16×16
   so no row height moves. The floor is spelled `min-width`/`min-height`
   rather than `width`/`height` on purpose: a page-scoped `input { … }` rule
   outranks the global base, and a `min-*` clamp wins regardless of which
   `width` applies. Two guards, because neither sees what the other does.
   `frontend/src/lib/a11y/targetSizeAudit.test.ts` (vitest, `pnpm test:unit`)
   scans the stylesheets so the recipe cannot be weakened for the whole app
   from a file no route list covers. `frontend/tests-e2e/a11y/target-size.spec.ts`
   measures the rendered page — and had to, because the route scan in (1) was
   passing without ever looking: a **disabled** input is not focusable, so
   axe's own matcher skips it, and every `SYSTEM_MANAGED_STATUSES` invoice
   (`pending`, `paid`, `done`, …) renders its checkbox disabled. Playwright
   walks spec folders alphabetically, so `a11y/` saw the tenant before the
   `invoices/` specs added a selectable row to it; the coverage was an artefact
   of file ordering. That spec now forces a selectable row on screen and
   asserts the geometry directly, including that a click 4px outside the
   painted control still toggles it and that the focus ring is drawn on the
   control rather than around the invisible target.
5. **Navigability tests (web).** `frontend/tests-e2e/a11y/screen-reader.spec.ts`
   asserts the structural semantics a screen-reader/keyboard user relies on:
   skip link + named landmarks + a single `<h1>`, no positive tabindex, and
   dialog focus-trap + focus-restore on Esc. `workflow-builder.spec.ts` covers
   the keyboard step-reorder path.
6. **Automated (320px reflow scan, every CI run).**
   `frontend/tests-e2e/a11y/reflow.spec.ts` sets a 320px viewport — the width
   SC 1.4.10 names — and asserts that no page scrolls the document sideways
   (`documentElement.scrollWidth - clientWidth <= 1`). Content that genuinely
   cannot reflow scrolls **inside its own container**, which does not count
   toward that measurement; the page does not.
   **It enumerates `frontend/src/routes` off disk rather than naming paths**,
   so a route is covered the day it lands. That is the whole point of the file:
   the guard previously named five paths by hand (`/`, `/invoices`, `/vendors`,
   `/payments`, `/contracts`) and was green while seven other routes failed the
   same criterion — `/organization` (137px, a diagnosed entry in
   `known-issues.md` that nothing re-tested), `/adaptive` (359px), `/expenses`
   (206px), `/profile` (131px), `/cfo` (81px), `/admin/retention` (9px) and
   `/reports` (7px). All seven are fixed; issue #432 is the write-up. The only
   entries it skips are routes outside the signed-in app shell (the legal
   documents, the pre-auth `AuthShell` pages and the supplier portal), each
   listed with its reason in the spec, and the list is itself asserted to still
   match real routes. **Never add an app route to that list to turn a red run
   green** — fix the layout.
7. **Flutter semantics tests (mobile).** The mobile app uses Flutter's
   `Semantics` tree and `meetsGuideline` widget tests (`mobile/test/a11y/`) to
   assert that interactive widgets expose labels, roles, and state to TalkBack /
   VoiceOver, plus tap-target size and contrast.
8. **Manual screen-reader passes.** Keyboard-only and screen-reader walkthroughs
   of the core flows — **VoiceOver** (macOS Safari + iOS), **NVDA** (Windows
   Firefox/Chrome), **TalkBack** (Android) — run from the repeatable
   [screen-reader checklist](./accessibility-screen-reader-checklist.md) before a
   release touching the core flow, navigation, modals, or forms. Automated
   tooling catches roughly a third to a half of WCAG issues; this pass covers the
   rest — announcement quality, reading order as heard, focus-not-obscured,
   consistent help, and accessible authentication.

### What the automated guard cannot catch

axe-core (like all automated tooling) verifies machine-detectable criteria
only. It does **not** assert the inherently human-judgement criteria — focus
order quality, meaningful link text in context, error-message clarity, the
WCAG 2.2 additions 2.4.11 Focus Not Obscured / 3.2.6 Consistent Help / 3.3.7
Redundant Entry / 3.3.8 Accessible Authentication, or that a screen reader
actually announces a state change usefully. Those depend on the manual passes
above. The VPAT marks any criterion not yet covered by a completed manual pass
as **Partially Supports** and names the remaining work.

## Accessibility features

The current build implements:

- **Skip-to-content link** as the first focusable element on every page.
- **Keyboard operability** across all interactive controls — clickable table
  rows expose a real focusable control (`RowLink`) rather than a click-only
  `<tr>`, so the keyboard path and column-header semantics both survive.
- **Visible focus indicator** (`:focus-visible`) on every interactive element.
- **Focus management in dialogs** — the shared `Modal` traps focus, returns it
  to the trigger on close, and closes on `Esc`; dialogs carry `role="dialog"`
  + an `aria-label`.
- **Focus not obscured by the consent banner** (SC 2.4.11) — the banner is fixed
  to the bottom of the viewport, so while it shows it reserves its own height: a
  spacer lets any page scroll clear of it, and `scroll-padding-bottom` stops
  keyboard focus above it rather than behind it. Without that, at 1280×720 it
  hid the reset-password submit button on a page with nothing to scroll.
  Guarded by `frontend/tests-e2e/consent-banner.spec.ts`.
- **Live-region announcements** for toasts and async status (`aria-live`).
- **Labelled form fields** — every input has an associated `<label>`;
  required fields are marked.
- **Sufficient colour contrast** in the dark theme (text and UI components meet
  the 4.5:1 / 3:1 AA thresholds), verified three ways: the contrast rules in the
  axe guard on what a route renders, the stylesheet-wide colour-token scan on
  every rule in the app whether a guarded route renders it or not, and the
  opacity scan on the one thing neither can see — a fade applied to an
  ancestor, whose cost lands on descendants' colours.
- **Reduced-motion support** — animations respect
  `prefers-reduced-motion: reduce`: durations and delays both collapse, so a
  page is complete on its first frame rather than waiting on staggered
  entrances. Scroll reveals never hide anything under that preference.
  Guarded by `frontend/tests-e2e/a11y/reduced-motion.spec.ts`.
- **Pause, Stop, Hide (SC 2.2.2)** — the marketing page's continuous decorative
  motion (the hero sequence, the background, the adapter rail) has an in-page
  **Pause animation** button in its header, independent of any OS setting,
  which halts every animation on the page. The auth pages carry no continuous
  motion, so they need none. Guarded by
  `frontend/tests-e2e/marketing/landing.spec.ts`.
- **Semantic structure** — landmarks (`header`, `nav`, `main`), a single `h1`
  per page, and ordered headings; native controls (checkbox, radio, select,
  file, range) are restyled with `appearance: none` while preserving their
  native semantics and states.
- **Pointer targets that meet the 24×24 minimum** (SC 2.5.8) rather than
  relying on its spacing exception. The row-select checkbox is the case that
  matters: it is painted 16×16 to fit a dense table row, and its *hit* box is
  grown to 24×24 with a transparent border that the layout gets back as a
  negative margin — so the target clears the floor without any row growing or
  any control moving a pixel.
- **Consistent navigation and help** — the sidebar nav and section tabs appear
  in the same relative order across routes.

## Known limitations

We are honest about where we are not yet fully conformant:

- **Manual screen-reader audit is in progress.** Until a full VoiceOver / NVDA
  / TalkBack pass over each core flow is signed off, criteria that depend on
  it are marked **Partially Supports** in the VPAT. This is the single largest
  piece of outstanding work.
- **A customer's own brand colours.** An org on a plan that allows custom
  branding can set an `accent_strong_color` that does not clear 1.4.3. Both
  editing surfaces show the real white-on-colour ratio inline as it is typed,
  but the backend accepts any valid hex on purpose — the brand is the tenant's
  call, and a hard refusal would make the API the arbiter of a design decision
  ([white-label.md](white-label.md), [decisions.md](decisions.md) §28). The
  published statement discloses this as content we do not control.
- **PDF invoice previews.** Uploaded invoice PDFs are third-party documents
  whose internal tagging we don't control; the extracted invoice data is
  always available as accessible HTML alongside the preview.
- **Third-party hosted pages** (external IdP SSO login, payment-processor card
  capture) are governed by those vendors' own conformance.

Resolved since the first pass, and no longer limitations: the
workflow-builder's drag-to-reorder now carries a per-node keyboard /
single-pointer alternative (Move ↑ / Move ↓ on every step), so **2.5.7 Dragging
Movements** and **2.5.1 Pointer Gestures** both read *Supports* in the VPAT.

None of the above blocks completing the core invoice → approve → pay workflow
with a keyboard and a screen reader.

## Legal and standards context

This statement is written against the following frameworks:

- **WCAG 2.2 Level AA** — the W3C Web Content Accessibility Guidelines, our
  technical conformance target.
- **EU European Accessibility Act (EAA), Directive (EU) 2019/882** — in force
  since **28 June 2025** for products and services placed on the EU market;
  WCAG 2.x AA (via EN 301 549) is the operative technical baseline.
- **Americans with Disabilities Act (ADA), Title III** — U.S. public
  accommodations; courts treat WCAG 2.x AA as the de-facto standard.
- **Section 508 of the Rehabilitation Act** — U.S. federal procurement;
  incorporates WCAG 2.x AA by reference. The companion VPAT/ACR is provided in
  the standard format procurement teams expect.
- **EN 301 549** — the harmonised EU standard that maps WCAG onto the EAA.

## Feedback and contact

If you encounter an accessibility barrier, or need information in an alternative
format, contact us at **support@feohledger.com** with *accessibility* in the
subject line. Please include the page or screen, what you were trying to do, and
the assistive technology and browser/OS you were using. We aim to acknowledge
accessibility reports within five working days.

This was `accessibility@jaredhoward.com` — an address on a different domain,
outside the five aliases the published pages commit to monitoring
(`frontend/src/lib/legal/operator.ts` → `CONTACT`). Two documents naming
different escalation routes is how a report reaches nobody, and the one with a
reader wins.

## See also

- `frontend/src/routes/legal/accessibility/+page.svelte` — the **published**
  accessibility statement at `/legal/accessibility`, which is the customer-facing
  rendering of this file and the VPAT. A conformance claim that changes in one
  changes in all three.
- [Accessibility Conformance Report (VPAT / ACR)](./accessibility-vpat.md) —
  the criterion-by-criterion WCAG 2.2 AA edition table.
- [Screen-reader test checklist](./accessibility-screen-reader-checklist.md) —
  the repeatable manual VoiceOver / NVDA / TalkBack pass.
- `frontend/tests-e2e/a11y/axe.spec.ts` + `screen-reader.spec.ts` — the
  automated regression guards (axe + navigability/reflow/focus-trap).
- `frontend/tests-e2e/a11y/target-size.spec.ts` +
  `frontend/src/lib/a11y/targetSizeAudit.test.ts` — the SC 2.5.8 pair: the
  rendered geometry of a *selectable* row checkbox, and the stylesheet scan on
  the recipe that produces it.
- `frontend/src/lib/a11y/tokenPairing.test.ts` — the stylesheet colour-token /
  contrast scan (`pnpm test:unit`), with its pure scanners in `cssAudit.ts` and
  the WCAG math in `contrast.ts`.
- `frontend/tests-e2e/README.md` — how to run the e2e suite (incl. the a11y
  guard) locally and in CI.
