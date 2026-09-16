import AxeBuilder from '@axe-core/playwright';
import type { Page } from '@playwright/test';
import type { Result } from 'axe-core';

import { expect } from '../fixtures/helpers';

/**
 * Shared axe-core driver for the accessibility regression guard.
 *
 * One canonical tag set across every spec: WCAG 2.0/2.1/2.2 Levels A + AA.
 * These are the conformance target stated in `docs/accessibility.md` and the
 * VPAT (`docs/accessibility-vpat.md`). Keep this list and those docs in sync —
 * if you widen the target (e.g. add AAA), update both.
 *
 * `wcag22aa` covers the success criteria new in WCAG 2.2 that axe can detect
 * automatically (e.g. 2.5.8 Target Size). The 2.2 criteria that are inherently
 * manual (2.4.11 Focus Not Obscured, 3.2.6 Consistent Help, 3.3.7 Redundant
 * Entry, 3.3.8 Accessible Authentication) are tracked as "Partially Supports"
 * in the VPAT pending a manual screen-reader pass — automated tooling can't
 * assert them.
 */
export const WCAG_AA_TAGS = [
	'wcag2a',
	'wcag2aa',
	'wcag21a',
	'wcag21aa',
	'wcag22aa'
] as const;

/**
 * Render a violations array into a readable, CI-actionable summary so a failing
 * run names exactly what broke (rule id, impact, help URL, and the offending
 * DOM nodes) instead of an opaque `toEqual([])` diff.
 */
export function formatViolations(violations: Result[]): string {
	if (violations.length === 0) return 'no accessibility violations';
	return violations
		.map((v) => {
			const nodes = v.nodes
				.map((n) => `      - ${n.target.join(' ')}\n        ${n.failureSummary ?? ''}`)
				.join('\n');
			return (
				`  [${v.impact ?? 'unknown'}] ${v.id}: ${v.help}\n` +
				`    ${v.helpUrl}\n` +
				`    affected nodes (${v.nodes.length}):\n${nodes}`
			);
		})
		.join('\n\n');
}

/**
 * Run axe against the current page state at the WCAG 2.2 AA tag set and assert
 * zero violations. On failure the error message carries the full
 * `formatViolations` summary so the CI log is actionable.
 *
 * `exclude` is an escape hatch for a known-failing complex widget owned by
 * another worker — pass a CSS selector AND name what/why in the call site with
 * a tracked follow-up. Prefer not excluding: zero violations is the target.
 */
export async function expectNoA11yViolations(
	page: Page,
	opts: { exclude?: string[] } = {}
): Promise<void> {
	// Measure the page AT REST. Modals rise in, the landing page reveals on
	// scroll, the auth shell staggers its fields — and axe's colour-contrast
	// rule reads whatever alpha a node has at the instant it runs, so a scan
	// that lands mid-entrance reports a contrast failure no reader ever sees
	// (or passes on a frame before the text is drawn). Emulating reduced motion
	// here collapses every animation, delay included, to its final frame
	// (app.css § Reduced motion), which is the state 1.4.3 is about.
	//
	// Set centrally so a spec cannot forget it, and after the caller's own
	// setup so it does not change what they asserted before scanning. It
	// persists for the rest of the test; nothing after a scan depends on
	// motion. The moving states have their own guards
	// (tests-e2e/a11y/reduced-motion.spec.ts, tests-e2e/marketing/).
	await page.emulateMedia({ reducedMotion: 'reduce' });
	let builder = new AxeBuilder({ page }).withTags([...WCAG_AA_TAGS]);
	for (const selector of opts.exclude ?? []) {
		builder = builder.exclude(selector);
	}
	const results = await builder.analyze();
	expect(
		results.violations,
		`Accessibility violations found:\n${formatViolations(results.violations)}`
	).toEqual([]);
}
