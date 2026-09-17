# Screen-reader test checklist

A repeatable manual pass for the criteria a screen reader exercises that
automation can't fully assert (announcement quality, reading order as heard,
focus feedback). Run it before a release that touches the core flow, navigation,
modals, or forms. The **programmatic** semantics this pass depends on (accessible
names, landmarks, heading levels, focus trap/restore, no positive tabindex,
reflow) are locked by `frontend/tests-e2e/a11y/axe.spec.ts`,
`screen-reader.spec.ts`, `reflow.spec.ts` and `mobile/test/a11y/` — this checklist covers the
human-judgement layer on top.

## Tooling

| Platform | Screen reader | Browser/host |
| -------- | ------------- | ------------ |
| macOS | VoiceOver (`⌘`+`F5`) | Safari |
| Windows | NVDA (free, nvaccess.org) | Firefox or Chrome |
| iOS | VoiceOver (Settings → Accessibility) | Flutter app |
| Android | TalkBack (Settings → Accessibility) | Flutter app |

Basics: `Tab`/`Shift+Tab` moves between controls; the SR reads the focused
control's **name, role, state**. VoiceOver rotor / NVDA element list jumps by
headings, landmarks, links, form fields.

## Web — core invoice → approve → pay flow

1. **Login** (`/login`) — email + password fields announce their label and
   type; the show/hide control (if any) announces state; an invalid login
   announces the error (it's an `aria-live` alert).
2. **Skip link** — first `Tab` on any authenticated page announces "Skip to main
   content, link"; activating it moves focus to the main region.
3. **Landmarks** — the rotor/element-list shows: navigation "Primary", "main",
   the page `<h1>`. Nav items announce their name + current state.
4. **Invoices list** (`/invoices`) — the status filter chips announce
   pressed/unpressed; the table announces column headers when navigating cells;
   each row's open control announces "Edit invoice <number>".
5. **Invoice detail modal** — on open, focus moves into the dialog and it's
   announced as a dialog with its name; `Tab` cycles **within** the dialog and
   does not reach the page behind it; every field announces its label; `Esc`
   closes and focus returns to the row control it opened from.
6. **Approve / reject** — the action buttons announce their name; after the
   action, the status change / toast is announced (the toast container is an
   `aria-live` region — `assertive` for errors, `polite` otherwise).
7. **Payments** (`/payments`) — tabs announce role "tab" + selected state and
   move with arrow keys; a payment run executes and the result is announced.
8. **Workflow builder** (`/workflows/[id]`) — each step node is reachable and
   announces its name; the per-node "Move <step> up/down" buttons announce and
   reorder via `Enter`/`Space` (drag is not required).

## Web — supplier portal

9. **Portal login** (`/portal/login`) — labelled fields; error announced.
10. **Portal invoices / discount-offers** — list rows announce; the
    discount-accept dialog traps focus, announces as a dialog, and `Esc`
    restores focus.

## Cross-cutting

11. **Reduced motion** — with the OS "reduce motion" setting on, transitions /
    toast slide-ins are suppressed (no functionality gated on an animation
    finishing).
12. **Reading order** — the heard order matches the visual order on each page
    (no `tabindex` jump-arounds — guarded, but confirm by ear).
13. **No keyboard trap** — you can always `Tab` out of every widget (except an
    open modal, which intentionally traps until closed).

## Mobile (Flutter — VoiceOver / TalkBack)

14. Invoice list tiles announce one composed label (vendor, amount, status), not
    six fragments.
15. Icon-only actions (capture, app-bar, password show/hide) announce a name.
16. Approve/reject and capture-success announce via `SemanticsService`.
17. Tap targets are reachable and ≥48dp; text scales to 200% without clipping.

## Recording results

Log per-item pass/fail + the SR/OS/browser used. File any failure as a bug and
fix at the source (never by loosening the automated guard). Update
`docs/accessibility-vpat.md` "Partially Supports" rows to "Supports" once the
corresponding items pass on at least one desktop SR + one mobile SR.
