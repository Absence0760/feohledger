# Accessibility Conformance Report (VPAT / ACR)

**Product:** FeohLedger — accounts-payable platform (web app,
supplier portal, Flutter mobile app)
**Report date:** 2026-09-16
**Edition basis:** [VPAT® 2.5](https://www.itic.org/policy/accessibility/vpat)
(WCAG edition)
**Standards evaluated:** [WCAG 2.2](https://www.w3.org/TR/WCAG22/) Level A and
Level AA
**Evaluation methods:** Automated testing with `axe-core` in the CI Playwright
e2e suite (`frontend/tests-e2e/a11y/axe.spec.ts`), Flutter semantics tests
(mobile), and in-progress manual keyboard + screen-reader review (VoiceOver,
NVDA, TalkBack).
**Companion:** [Accessibility conformance statement](./accessibility.md) (internal),
published as [`/legal/accessibility`](../frontend/src/routes/legal/accessibility/+page.svelte)
in the app. **A conformance claim that changes here changes in all three.**

This Accessibility Conformance Report (ACR) follows the VPAT format used in
procurement. It is a **self-assessment**. The conformance column reflects the
accessibility work landed in the current initiative (skip link, focus trap and
focus return in dialogs, `:focus-visible` indicators, `aria-live` toasts,
labelled form fields, AA colour contrast, reduced-motion support, semantic
landmark/heading structure, and the automated axe regression guard).

## Conformance terms

| Term | Meaning |
| ---- | ------- |
| **Supports** | The functionality meets the criterion without known defects. |
| **Partially Supports** | Some functionality meets the criterion; exceptions or unverified areas remain (typically: not yet covered by a completed manual screen-reader pass). |
| **Does Not Support** | The majority of the functionality does not meet the criterion. |
| **Not Applicable** | The criterion is not relevant to the product (the underlying feature/content type does not exist). |

A note on honesty: criteria whose verification depends on a human screen-reader
pass that has not yet been signed off are marked **Partially Supports** even
where we have no evidence of a defect — automated tooling cannot assert them,
and the manual audit is the tracked outstanding work.

## WCAG 2.2 Level A

| Criterion | Level | Conformance | Remarks |
| --------- | ----- | ----------- | ------- |
| 1.1.1 Non-text Content | A | Supports | Icons paired with text or `aria-label`; the axe guard flags unlabelled images/buttons. |
| 1.2.1 Audio-only and Video-only (Prerecorded) | A | Not Applicable | No pre-recorded audio/video content. |
| 1.2.2 Captions (Prerecorded) | A | Not Applicable | No pre-recorded multimedia. |
| 1.2.3 Audio Description or Media Alternative (Prerecorded) | A | Not Applicable | No pre-recorded multimedia. |
| 1.3.1 Info and Relationships | A | Supports | Semantic landmarks, single `h1` + ordered headings, real `<table>`/`<th>` for data grids, `<label>`-associated inputs; verified by axe. |
| 1.3.2 Meaningful Sequence | A | Supports | DOM order matches visual reading order; no CSS-reordered content that changes meaning. |
| 1.3.3 Sensory Characteristics | A | Supports | Instructions don't rely on shape/position/colour alone (status badges carry text labels). |
| 1.4.1 Use of Color | A | Supports | Status and validation are conveyed by text/icon in addition to colour (e.g. `StatusBadge`, error text). |
| 1.4.2 Audio Control | A | Not Applicable | No auto-playing audio. |
| 2.1.1 Keyboard | A | Supports | All controls keyboard-operable; clickable rows expose a focusable `RowLink` rather than a click-only `<tr>`; a data grid that scrolls inside its container is a focusable, named region the arrow keys pan (`a11y/reflow.spec.ts` runs axe's `scrollable-region-focusable` on every route at 320px). |
| 2.1.2 No Keyboard Trap | A | Supports | The shared `Modal` traps focus only while open and releases it on `Esc`/close; no other traps. |
| 2.1.4 Character Key Shortcuts | A | Not Applicable | No single-character key shortcuts are implemented. |
| 2.2.1 Timing Adjustable | A | Supports | No time limits on user interaction; session expiry re-prompts for login without data loss of in-progress work beyond auth. |
| 2.2.2 Pause, Stop, Hide | A | Supports | In the app: no auto-updating/blinking content beyond brief toasts; loading indicators are not blinking content. On the public marketing page, the looping decorative animation (hero sequence, background, adapter rail) is stopped by an in-page Pause animation control in the header; the sign-in and signup pages have no continuous motion. |
| 2.3.1 Three Flashes or Below Threshold | A | Supports | No flashing content. |
| 2.4.1 Bypass Blocks | A | Supports | Skip-to-content link is the first focusable element; consistent landmark structure provides additional bypass. |
| 2.4.2 Page Titled | A | Supports | Each route sets a descriptive document title. |
| 2.4.3 Focus Order | A | Partially Supports | DOM order is logical; focus order through the full set of complex widgets (workflow builder, multi-step modals) is pending a manual keyboard pass. |
| 2.4.4 Link Purpose (In Context) | A | Partially Supports | Row links carry row-specific `aria-label`s; a full audit of link text in context is part of the manual pass. |
| 2.5.1 Pointer Gestures | A | Supports | Interactions are single taps/clicks. The one drag surface — the workflow-builder canvas — carries a per-node single-pointer alternative (Move ↑ / Move ↓ on every step), so no path-based or multipoint gesture is required anywhere. |
| 2.5.2 Pointer Cancellation | A | Supports | Actions fire on the up-event; no down-event commits. |
| 2.5.3 Label in Name | A | Supports | Visible labels are contained in each control's accessible name; verified by axe. |
| 2.5.4 Motion Actuation | A | Not Applicable | No motion/device-orientation-actuated functions (mobile camera OCR is an explicit button, not motion-triggered). |
| 3.1.1 Language of Page | A | Supports | `<html lang="en-US">` is set. |
| 3.2.1 On Focus | A | Supports | Focusing a control does not trigger a context change. |
| 3.2.2 On Input | A | Supports | Changing a field value does not auto-submit or navigate without an explicit action. |
| 3.3.1 Error Identification | A | Supports | Form validation errors are identified in text adjacent to the field. |
| 3.3.2 Labels or Instructions | A | Supports | Every input has a visible label; required fields are marked. |
| 4.1.1 Parsing | A | Supports | (Obsolete in WCAG 2.2, retained for cross-reference.) Markup is well-formed; no duplicate IDs flagged by axe. |
| 4.1.2 Name, Role, Value | A | Supports | Native controls keep native semantics; custom widgets (modal, tabs, badges) expose role/name/state via ARIA; verified by axe. |

## WCAG 2.2 Level AA

| Criterion | Level | Conformance | Remarks |
| --------- | ----- | ----------- | ------- |
| 1.2.4 Captions (Live) | AA | Not Applicable | No live multimedia. |
| 1.2.5 Audio Description (Prerecorded) | AA | Not Applicable | No pre-recorded multimedia. |
| 1.3.4 Orientation | AA | Supports | Layout works in both portrait and landscape; no orientation lock (mobile + responsive web). |
| 1.3.5 Identify Input Purpose | AA | Supports | Auth + onboarding fields carry `autocomplete` tokens (`email`, `current-password`, `new-password`, `organization`, `name`, `one-time-code`); inputs use the matching `type`. |
| 1.4.3 Contrast (Minimum) | AA | Supports | Dark-theme text and UI components meet 4.5:1 / 3:1. Enforced two ways on every CI run: the axe `color-contrast` rule on what the listed routes render, and a scan of every stylesheet in the app that resolves each colour pair through the palette — including pairs that only exist after compositing (a rule's own `opacity`, a translucent badge tint). The stylesheet scan is what covers a surface no listed route happens to render. |
| 1.4.4 Resize Text | AA | Supports | Layout uses relative units and reflows; text scales to 200% without loss of content. |
| 1.4.5 Images of Text | AA | Supports | Text is real text, not images of text. |
| 1.4.10 Reflow | AA | Supports | Content reflows to a single column at 320px with no page-level horizontal scroll (sidebar auto-collapses to its icon rail; tab bars, settings rows and chart grids wrap); data grids scroll within their own `overflow-x` container (the permitted exception). Guarded by `a11y/reflow.spec.ts`, which measures **every** route under `frontend/src/routes` rather than a hand-kept sample — the previous five-route guard reported Supports while seven other routes did not reflow (issue #432). |
| 1.4.11 Non-text Contrast | AA | Supports | UI component boundaries, focus indicators, and graphical controls meet 3:1; verified by axe. |
| 1.4.12 Text Spacing | AA | Supports | No loss of content when user text-spacing overrides are applied (no fixed-height text containers clipping). |
| 1.4.13 Content on Hover or Focus | AA | Partially Supports | Popovers/tooltips are dismissible and hoverable; full verification of every hover surface is part of the manual pass. |
| 2.4.5 Multiple Ways | AA | Supports | Navigation via the sidebar, section tabs, in-page search, and deep links. |
| 2.4.6 Headings and Labels | AA | Supports | Descriptive headings and labels throughout; single `h1` per page enforced. |
| 2.4.7 Focus Visible | AA | Supports | `:focus-visible` indicator on every interactive element. |
| 2.4.11 Focus Not Obscured (Minimum) | AA | Partially Supports | **New in 2.2.** The shared `Modal` and sticky headers are designed not to fully hide the focused element; this is inherently a manual check and is part of the outstanding screen-reader/keyboard pass. |
| 2.4.12 Focus Not Obscured (Enhanced) | AAA | Not Applicable | AAA criterion — outside the Level AA target (listed for completeness re: the 2.2 additions). |
| 2.5.7 Dragging Movements | AA | Supports | **New in 2.2.** Most interactions need no dragging. The workflow-builder canvas uses native HTML5 drag-and-drop **and** a per-node keyboard / single-pointer alternative — Move ↑ / Move ↓ on every step and in the step config panel (`components/workflow-builder/StepNode.svelte`), covered by `tests-e2e/workflow-builder.spec.ts`. |
| 2.5.8 Target Size (Minimum) | AA | Supports | **New in 2.2.** Interactive targets meet the 24×24 CSS-px minimum (buttons, chips, row controls). The row-select checkbox is the case worth stating: it is *painted* 16×16 to fit a dense table row, so it used to conform only through the criterion's spacing exception — which each page's checkbox-column padding decided rather than the design system. Its own box is now 24×24 (a transparent border, returned to the layout as a negative margin, so nothing moves and no row grows), meeting the size minimum outright. Guarded by the axe `target-size` rule, by `tests-e2e/a11y/target-size.spec.ts` on a *selectable* row, and by the stylesheet scan `src/lib/a11y/targetSizeAudit.test.ts`. |
| 3.1.2 Language of Parts | AA | Not Applicable | Content is single-language (en-US); no inline foreign-language passages requiring `lang`. |
| 3.2.3 Consistent Navigation | AA | Supports | Sidebar nav and section tabs appear in the same relative order across routes (driven by the single `$lib/nav.ts` source). |
| 3.2.4 Consistent Identification | AA | Supports | Components with the same function (e.g. status badges, row actions, money formatting) are identified consistently via the shared `ui/` component library. |
| 3.2.6 Consistent Help | AA | Partially Supports | **New in 2.2.** The feedback/help contact is consistent; a full audit that any help affordance appears in a consistent relative order across all surfaces is part of the manual pass. |
| 3.3.3 Error Suggestion | AA | Supports | Validation errors describe how to fix the input where the fix is known (format hints, required markers). |
| 3.3.4 Error Prevention (Legal, Financial, Data) | AA | Supports | Money-moving actions (payment-run execute, void, bulk delete) use explicit armed-confirm; partial-success results are surfaced per-row. |
| 3.3.7 Redundant Entry | AA | Partially Supports | **New in 2.2.** Multi-step flows (signup → verify, payment-run build) avoid re-asking for known data; a complete pass over every multi-step flow is part of the manual audit. |
| 3.3.8 Accessible Authentication (Minimum) | AA | Partially Supports | **New in 2.2.** Login is email + password (paste-enabled, no cognitive-function test); password managers work. MFA via TOTP/email is supported. A confirmation that no step imposes a cognitive-function test without an alternative is part of the manual pass. |
| 4.1.3 Status Messages | AA | Supports | Async status and toasts use `aria-live` regions so they're announced without moving focus. |

## WCAG 2.2 — new success criteria summary

The criteria introduced in WCAG 2.2 (over 2.1), called out because they're the
likeliest gaps in an older codebase:

| Criterion | Level | Conformance | Where it bites in this product |
| --------- | ----- | ----------- | ------------------------------ |
| 2.4.11 Focus Not Obscured (Minimum) | AA | Partially Supports | Sticky headers / modals must not hide the focused control — manual check pending. |
| 2.4.12 Focus Not Obscured (Enhanced) | AAA | Not Applicable | Above the AA target. |
| 2.4.13 Focus Appearance | AAA | Not Applicable | Above the AA target. |
| 2.5.7 Dragging Movements | AA | Supports | The workflow-builder canvas drag-to-reorder has a per-node single-pointer + keyboard alternative — Move ↑ / Move ↓ buttons on every step (and in the step config panel). Covered by `workflow-builder.spec.ts`. |
| 2.5.8 Target Size (Minimum) | AA | Supports | 24×24px minimum, met by size rather than by the spacing exception — the 16×16 row-select checkbox carries a 24×24 hit box that costs no layout. Guarded by axe `target-size`, `tests-e2e/a11y/target-size.spec.ts` and `src/lib/a11y/targetSizeAudit.test.ts`. |
| 3.2.6 Consistent Help | AA | Partially Supports | Consistent placement of help/feedback affordance — manual check pending. |
| 3.3.7 Redundant Entry | AA | Partially Supports | Don't re-ask for known info in multi-step flows — manual check pending. |
| 3.3.8 Accessible Authentication (Minimum) | AA | Partially Supports | No cognitive-function test without alternative; paste-enabled password fields — manual confirmation pending. |
| 3.3.9 Accessible Authentication (Enhanced) | AAA | Not Applicable | Above the AA target. |

## Outstanding work (tracked)

The structural follow-ups from the initial pass are now **resolved**:

- ~~Workflow-builder drag alternative (2.5.7)~~ — **Done.** Per-node Move ↑/↓
  buttons (keyboard + single-pointer) on every step; covered by
  `workflow-builder.spec.ts`.
- ~~Hand-rolled-modal focus management (2.1.2 / 2.4.3)~~ — **Done.** The shared
  `$lib/actions/focusTrap` action gives `InvoiceModal`, `RunDetailModal`,
  `BulkRecodeGLModal`, and the portal discount-accept dialog the same focus
  trap + restore as `ui/Modal`; covered by `screen-reader.spec.ts`.
- ~~`autocomplete` tokens (1.3.5)~~ and ~~320px reflow (1.4.10)~~ — **Done** and
  guarded by `screen-reader.spec.ts`.

Remaining:

1. **Execute the manual screen-reader pass on real devices** (VoiceOver / NVDA /
   TalkBack) using
   [docs/accessibility-screen-reader-checklist.md](./accessibility-screen-reader-checklist.md).
   The programmatic semantics it depends on (names, roles, landmarks, headings,
   focus trap/restore, reflow, no positive tabindex) are locked by the automated
   guards; this pass is the human-judgement layer (announcement quality, reading
   order as heard) and is what converts the few remaining **Partially Supports**
   rows (2.4.11 Focus Not Obscured, 3.2.6 Consistent Help, 3.3.7 Redundant
   Entry, 3.3.8 Accessible Authentication) to a verified status. It needs real
   AT hardware, so it can't run in CI.

Per the project's no-dangling-findings rule, the device pass is a documented,
repeatable procedure (the checklist), not an open-ended TODO.

## Evaluation evidence

- **Automated (axe):** `frontend/tests-e2e/a11y/axe.spec.ts` runs `axe-core` at
  the `wcag2a,wcag2aa,wcag21a,wcag21aa,wcag22aa` tag set against the dashboard,
  invoices list, vendors, payments, exceptions, the invoice detail modal, and
  the two login surfaces, asserting zero violations on every CI push. Run
  locally: `pnpm test:e2e:a11y`.
- **Automated (navigability):** `frontend/tests-e2e/a11y/screen-reader.spec.ts`
  asserts the skip link + named landmarks + single `<h1>`, no positive tabindex,
  320px reflow with no horizontal scroll, and dialog focus-trap + Esc focus
  restore — the programmatic semantics the manual SR pass relies on.
  `workflow-builder.spec.ts` covers the keyboard step-reorder path.
- **Mobile:** Flutter `meetsGuideline` widget tests (`mobile/test/a11y/`) assert
  label/role/state exposure, tap-target size, and contrast.
- **Manual:** keyboard-only + screen-reader walkthroughs per
  [docs/accessibility-screen-reader-checklist.md](./accessibility-screen-reader-checklist.md)
  (device pass — run before a release touching the core flow/nav/modals/forms).
