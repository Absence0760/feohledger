import { describe, expect, it } from 'vitest';

// "Never hand-roll a modal shell" (frontend/docs/ui-patterns.md § Modals).
// `ui/Modal.svelte` owns the backdrop, the focus trap, Esc / backdrop-click
// close, the page-scroll lock and (through focusTrap's registry) the inert
// background. A private copy drifts: BulkRecodeGLModal carried its own shell,
// shadow and z-index until it moved onto Modal. This is a source-text guard
// (the frontend's vitest setup targets pure modules, per frontend/CLAUDE.md):
// no .svelte file outside Modal writes a `role="dialog"` attribute or a
// `class="backdrop"` element.

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const FILES: [string, string][] = Object.entries(RAW)
	.map(([path, source]) => [path.replace(/^\/src\//, ''), source] as [string, string])
	.sort(([a], [b]) => a.localeCompare(b));

const OWNER = 'lib/components/ui/Modal.svelte';

/**
 * Files still allowed one of the patterns, each with its reason. Shrink-only:
 * the last test fails when a listed file no longer needs its entry, so the
 * list cannot outlive the debt it records.
 */
const ALLOWLIST: Record<string, string> = {
	'lib/components/modals/InvoiceModal.svelte':
		'Pre-dates Modal; the largest e2e-load-bearing dialog. Scheduled to move onto Modal next round.',
	'lib/components/modals/RunDetailModal.svelte':
		'Pre-dates Modal; scheduled to move onto Modal next round with InvoiceModal.',
	'lib/components/layout/NotificationBell.svelte':
		'A non-modal disclosure popover (role="dialog" without aria-modal, no backdrop, the page stays usable) — not a modal shell.',
	'routes/portal/login/+page.svelte':
		'class="backdrop" there is the decorative, aria-hidden brand background behind the sign-in card (issue #422; selected by tests-e2e/portal/login-background.spec.ts), not a dialog backdrop.'
};

/** HTML comments may quote the patterns while explaining them. */
function stripComments(src: string): string {
	return src.replace(/<!--[\s\S]*?-->/g, '');
}

// An attribute, so preceded by whitespace: `'[role="dialog"]'` selectors in
// script and backticked mentions in prose do not count.
const ROLE_DIALOG = /\srole\s*=\s*["']dialog["']/;
// `backdrop` as a whole class token (not `backdrop-x` / `login-backdrop`).
const BACKDROP_CLASS = /\sclass\s*=\s*["'][^"']*(?<![\w-])backdrop(?![\w-])[^"']*["']/;

function offends(src: string): boolean {
	const body = stripComments(src);
	return ROLE_DIALOG.test(body) || BACKDROP_CLASS.test(body);
}

describe('modal shells come from ui/Modal.svelte', () => {
	it('the owner still matches the patterns (the regexes are live)', () => {
		expect(RAW[`/src/${OWNER}`]).toBeDefined();
		expect(offends(RAW[`/src/${OWNER}`])).toBe(true);
	});

	it('no other .svelte file hand-rolls a dialog shell', () => {
		const offenders = FILES.filter(
			([path, src]) => path !== OWNER && !(path in ALLOWLIST) && offends(src)
		).map(([path]) => path);
		expect(offenders).toEqual([]);
	});

	it.each(Object.keys(ALLOWLIST))('allowlisted %s still needs its entry', (path) => {
		const src = RAW[`/src/${path}`];
		expect(src, `${path} no longer exists — drop it from ALLOWLIST`).toBeDefined();
		expect(offends(src), `${path} is clean now — drop it from ALLOWLIST`).toBe(true);
	});
});
