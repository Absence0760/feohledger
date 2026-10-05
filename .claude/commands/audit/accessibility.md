---
description: WCAG 2.2 AA + EU EAA + ADA pass across the two surfaces this project ships — the SvelteKit web app and the Flutter mobile app
---

Audit accessibility across every user-facing surface. The EU EAA, in force since 2025-06-28, makes WCAG 2.2 AA a legal requirement for digital services sold in the EU; ADA Title III and the state privacy/human-rights laws converge on the same bar.

## Goal

Find every place the shipped product misses that bar — and, just as importantly, every place a **claim we have already published** no longer holds. This project ships a conformance statement (`docs/accessibility.md`), a VPAT/ACR (`docs/accessibility-vpat.md`) and a manual screen-reader checklist (`docs/accessibility-screen-reader-checklist.md`). A regression against a published claim outranks a new nice-to-have, because that claim is what a procurement reviewer relies on.

## Surfaces

Exactly two — the SvelteKit web app (`frontend/`) and the Flutter mobile app (`mobile/`). There is no watch, TV, or desktop surface; do not audit for one.

Note there are **two distinct web audiences**: the AP application (the customer's own staff, authenticated) and the **supplier portal** (external vendors, including its unauthenticated login and white-label-themed pages). The portal is the one an EU supplier with no relationship to us is forced to use, so a portal failure carries more legal weight than an internal-tool failure.

## What to check

### Web (SvelteKit 3 / Svelte 5)

1. **Semantic HTML.** `<button>` not `<div onclick>`. Every icon-only control carries an `aria-label` or visually-hidden text. Walk the shared library first — `frontend/src/lib/components/` — because a defect in `RowAction`, `StatusBadge`, `BulkBar`, `SearchBox` or `Modal` ships on every route at once, and so does its fix.
2. **Focus management.** Modals trap focus and restore it on close — the shared `focusTrap` action is the single owner; flag any dialog that hand-rolls its own. `:focus-visible` ring on every focusable. `tabindex="-1"` only on non-interactive targets.
3. **Colour contrast.** ≥ 4.5:1 text, ≥ 3:1 UI components and focus indicators. Resolve the CSS custom properties in `frontend/src/app.css` to real values before computing — and check **both** themes. Note `--accent-strong` exists precisely because the plain accent failed AA on text; a new call site using `--accent` for text is the recurring regression.
4. **White-label theming is a live contrast risk.** A tenant admin sets `accent` / `accent_strong` via `/api/organization/branding`, and the portal serves brand colours **unauthenticated**. The API validates hex *shape*, not contrast — so a tenant can theme itself below AA. Report whether anything warns, clamps, or documents that, and treat the portal (external users, no choice of tool) as the higher-severity case.
5. **Keyboard nav.** Every flow completable without a pointer: the invoice queue and its bulk selection, the approval decision, the workflow builder's step reordering (per-node reorder controls exist for exactly this reason — verify they still do), the entity switcher, every data table's sort and filter.
6. **Form labels.** Every input has a `<label>` or `aria-labelledby`; every validation error is programmatically associated with its field, not merely adjacent to it.
7. **Live regions.** Toasts `role="status"` / `aria-live="polite"`; errors `aria-live="assertive"`. The async surfaces matter most here — extraction finishing, a workflow transition, a payment-run status change: a state change nobody announces is invisible to a screen-reader user.
8. **Skip link** at the top of `+layout.svelte`.
9. **Motion-reduce.** `@media (prefers-reduced-motion: reduce)` honoured by every transition and spinner.
10. **Headings.** One `<h1>` per page, descending without skips.
11. **Reflow + text scale (1.4.4 / 1.4.10).** No loss of content or function at 200% text or 320 CSS px. The dense surfaces — invoice tables, the payment queue, analytics charts — are where this breaks; confirm wide content scrolls in its own container rather than the page scrolling horizontally.
12. **Charts and figures.** The CFO analytics dashboard must not carry meaning by colour alone (1.4.1) and needs a text or table alternative for each figure.

### Mobile (Flutter, Material 3)

13. **Semantics widgets.** Every `IconButton`, `GestureDetector` and custom-painted tappable wrapped in `Semantics(label: …, button: true)`.
14. **Screen-reader labels** on the icon-only actions in the invoice list, approval screen, and the KPI card grid.
15. **Dynamic type.** Text scaling to 200% without overflow (`MediaQuery.textScaler`).
16. **Colour contrast.** Same bar as web; check the Material 3 scheme in both light and dark.
17. **Touch targets.** ≥ 44×44 (Apple HIG) / 48×48 (Material); WCAG 2.5.8 floor is 24×24.
18. **Live regions.** `SemanticsService.announce(...)` on state changes the user is waiting for (extraction complete, approval submitted).
19. **Reduce motion.** `MediaQuery.disableAnimations`.

### The guards

20. Confirm the automated guards still exist and still run: `frontend/tests-e2e/a11y/` (axe-core plus the navigability / reflow / focus-trap specs) and `mobile/test/a11y/` (`meetsGuideline`). A violation those already cover is a **guard** finding — the guard is broken or the spec was loosened — not a new discovery. Never report "loosen the assertion" as a fix.

## Report

- **Critical** — a flow unreachable without sight or without a pointer (especially on the supplier portal, where the user has no alternative tool).
- **High** — a testable WCAG 2.2 AA failure (computed contrast < 4.5:1, missing accessible name, no keyboard path), **or** a published claim in `docs/accessibility-vpat.md` that no longer holds.
- **Medium** — best-practice gap (no live region on an async completion, heading order, no text alternative for a chart).
- **Low** — polish (focus-ring styling, motion-reduce on a non-critical animation).

For each: `file:line`, the success criterion by number and name (e.g. "1.4.3 Contrast (Minimum)"), the **computed** value versus the threshold — never an eyeballed one — and the fix. Say which surfaces share the defect.

End with a **clean** list of the surfaces and criteria you confirmed pass.

## After the report

This command is read-only. `/a11y-hunt` is the fix loop — it re-computes each claim, fixes the root cause at the shared token or component, and lands a guard in the same commit. Hand it the findings rather than patching here.

## Delegate to

Use the `compliance-auditor` agent: `"Audit accessibility across the SvelteKit web app and the Flutter mobile app per WCAG 2.2 AA / EU EAA / ADA, including the supplier portal and the white-label theming path, and check the published VPAT claims still hold."`

`persona-accessibility-user` is the complementary narrative walkthrough — run it when you want the human-impact framing rather than the criterion-by-criterion sweep.

Read-only. Findings only.
