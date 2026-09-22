/**
 * The tones `ui/Badge.svelte` renders — the five the palette names, plus the
 * two non-tinted cases every badge row eventually needs.
 *
 * `neutral` is a flat `--bg` chip for a "nothing happening" state
 * (cancelled / not-applicable) — deliberately NOT a tint, because a tint
 * reads as a signal and these states are the absence of one.
 * `erp` is the one measured literal: purple carries no semantic the five
 * tones share (it exists to make "handed to the ERP" scannable
 * mid-pipeline), so it stays a literal in the component rather than becoming
 * a palette token with a single caller. See `StatusBadge.svelte` and
 * decisions.md §30.
 *
 * **Why this is a `.ts` module and not the component's `<script module>`.**
 * Every `$lib/types/*` status→tone map is typed with it, and those modules are
 * also what an e2e fixture reaches for to pin a stub (`satisfies SomeType`).
 * `tests-e2e/` is typechecked by plain `tsc` (`pnpm check:e2e`), which
 * resolves a `.svelte` import through the ambient `*.svelte` shim — a shim
 * with no named exports — so a type declared inside `Badge.svelte` was a
 * TS2614 for any fixture whose type touched it transitively. Declared here,
 * both toolchains read the same file.
 */
export type BadgeTone = 'accent' | 'success' | 'warning' | 'danger' | 'muted' | 'neutral' | 'erp';
