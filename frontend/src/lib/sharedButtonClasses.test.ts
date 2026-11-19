import { describe, expect, it } from 'vitest';
import appCss from '../app.css?raw';
import { extractStyleBlocks, parseRules } from './a11y/cssAudit';

/**
 * Buttons come from the shared classes in `app.css` (`frontend/docs/ui-patterns.md`
 * § Buttons), never from a per-file copy of their recipe.
 *
 * Every copy this guard covers had drifted from the shared one before it was
 * folded in: a secondary button at `--text-muted` in one route and `--text` in
 * the next, disabled shown with `opacity` (2.33:1 for muted text, which
 * `a11y/opacityAudit.test.ts` exists to stop), radii of 4, 6 and 8px in one
 * toolbar, and an "armed" confirm filled with `--danger` in one place and
 * `--danger-strong` in another. A copy is only ever correct on the day it is
 * made.
 *
 * In a covered file:
 *   1. No scoped rule names a local button class — `btn-*` other than the
 *      shared ones, or `*-btn`. An icon-only control is `.btn-icon`, so
 *      there is no variant left a local copy could stand in for.
 *   2. A scoped rule that names a shared class may place it (margin,
 *      `align-self`, padding to fill a slot) but never paint it: no colour,
 *      background, border, shadow, opacity, filter, font or cursor.
 *   3. Every shared class the file's markup uses exists in `app.css`.
 *
 * Coverage is the files migrated so far. A file joins `COVERED` in the same
 * change that moves it onto the shared classes; the ones left out still carry
 * copies.
 */

const SHARED = [
	'btn-primary',
	'btn-cancel',
	'btn-secondary',
	'btn-danger',
	'btn-ghost',
	'btn-icon'
] as const;

const COVERED = [
	'lib/components/layout/AccountLoadState.svelte',
	'lib/components/modals/AdvancedSearchModal.svelte',
	'lib/components/modals/BulkNegotiationModal.svelte',
	'lib/components/modals/BulkRecodeGLModal.svelte',
	'lib/components/modals/ContractModal.svelte',
	'lib/components/modals/InvoiceModal.svelte',
	'lib/components/modals/RunDetailModal.svelte',
	'lib/components/organization/CloseWorkspacePanel.svelte',
	'lib/components/reports/ResultTable.svelte',
	'lib/components/ui/BulkBar.svelte',
	'lib/components/ui/BulkDeleteButton.svelte',
	'lib/components/workflow-mgmt/ImportExportControls.svelte',
	'lib/components/workflow-mgmt/SimulationModal.svelte',
	'lib/components/workflow-mgmt/TemplateLibraryModal.svelte',
	'lib/components/workflow-mgmt/VersionHistoryModal.svelte',
	'routes/admin/privacy/+page.svelte',
	'routes/assistant/+page.svelte',
	'routes/cash-flow/+page.svelte',
	'routes/contracts/+page.svelte',
	'routes/exceptions/+page.svelte',
	'routes/expenses/+page.svelte',
	'routes/gl-accounts/+page.svelte',
	'routes/invoices/+page.svelte',
	'routes/payments/+page.svelte',
	'routes/portal/company/+page.svelte',
	'routes/portal/notifications/+page.svelte',
	'routes/recurring/+page.svelte',
	'routes/reports/+page.svelte',
	'routes/tax/+page.svelte',
	'routes/vendors/+page.svelte',
	'routes/vendors/change-requests/+page.svelte',
	'routes/vendors/screening/+page.svelte'
];

/** Properties that paint a button; a scoped rule on a shared class sets none. */
const PAINT =
	/^(color|background(-.*)?|border(-.*)?|outline(-.*)?|box-shadow|opacity|filter|font(-.*)?|text-decoration(-.*)?|cursor)$/;

/**
 * Class names a migrated button keeps with no CSS behind them, because an e2e
 * spec or the page's own script selects by them. `<file> <class>`.
 */
const E2E_HOOKS = new Set([
	'lib/components/modals/AdvancedSearchModal.svelte btn-apply',
	'lib/components/modals/ContractModal.svelte btn-save',
	'lib/components/modals/InvoiceModal.svelte btn-save',
	'lib/components/modals/InvoiceModal.svelte btn-submit',
	'routes/invoices/+page.svelte btn-upload',
	'routes/payments/+page.svelte btn-clear',
	'routes/payments/+page.svelte btn-execute',
	// Read by the outside-click un-arm (`closest('.row-action, .btn-decide')`).
	'routes/vendors/change-requests/+page.svelte btn-decide'
]);

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const SOURCES = new Map(
	Object.entries(RAW).map(([path, source]) => [path.replace(/^\/src\//, ''), source])
);

function classesIn(selector: string): string[] {
	return [...selector.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((m) => m[1]);
}

function scopedRules(path: string) {
	return extractStyleBlocks(path, SOURCES.get(path) ?? '').flatMap((b) => parseRules(b.css));
}

/** Every class `app.css` styles (`.btn-load-more` and the like count too). */
const APP_CSS_CLASSES = new Set(parseRules(appCss).flatMap((r) => classesIn(r.selector)));

/** A `btn-*` class name in markup — whole, so `bulk-btn-wrap` is not `btn-wrap`. */
const BTN_CLASS = /(?<![\w-])btn-[a-z]+(?:-[a-z]+)*(?![\w-])/g;

function markup(source: string): string {
	return source.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '');
}

describe('shared button classes', () => {
	it('every covered file exists', () => {
		const missing = COVERED.filter((p) => !SOURCES.has(p));
		expect(missing).toEqual([]);
	});

	it('app.css declares every shared class, and the armed state', () => {
		for (const cls of SHARED) expect(appCss).toMatch(new RegExp(`^\\.${cls}\\b`, 'm'));
		// The red fill holds through .btn-primary's own :hover (which would
		// otherwise swap the ring for its lift shadow) …
		expect(appCss).toMatch(
			/^\.btn-primary\.armed,\n\.btn-danger\.armed,\n\.btn-primary\.armed:hover:not\(:disabled\) \{/m
		);
		// … and busy stays red without `opacity` (.btn-primary:disabled's 0.5).
		const busy = appCss.match(/^\.btn-primary\.armed:disabled,\n\.btn-danger\.armed:disabled \{([^}]*)\}/m);
		expect(busy, 'the armed busy state is declared').not.toBeNull();
		expect(busy![1]).toMatch(/opacity: 1;/);
		expect(busy![1]).toMatch(/background: var\(--danger-tint\);/);
		expect(busy![1]).toMatch(/color: var\(--danger-on-tint\);/);
	});

	it('app.css gives .btn-primary and .btn-cancel the box .btn-secondary has', () => {
		const rules = parseRules(appCss);
		const box = (cls: string) => {
			const decl = new Map<string, string>();
			for (const r of rules) {
				const parts = r.selector.split(',').map((p) => p.trim());
				if (parts.includes(`.${cls}`)) for (const [p, v] of r.declarations) decl.set(p, v);
			}
			return decl;
		};
		const secondary = box('btn-secondary');
		for (const cls of ['btn-primary', 'btn-cancel']) {
			const decl = box(cls);
			for (const prop of ['display', 'align-items', 'justify-content', 'gap', 'line-height', 'padding']) {
				expect(decl.get(prop), `.${cls} ${prop}`).toBe(secondary.get(prop));
			}
			// A 1px border on all four, so the heights match to the pixel.
			expect(decl.get('border'), `.${cls} border`).toMatch(/^1px solid /);
		}
	});

	it('app.css declares the icon button: a 24px target, a pressed state, no opacity', () => {
		const rules = parseRules(appCss);
		const base = rules.find((r) => r.selector === '.btn-icon');
		expect(base, '.btn-icon is declared').toBeDefined();
		const decl = new Map(base!.declarations);
		expect(decl.get('min-width')).toBe('24px');
		expect(decl.get('min-height')).toBe('24px');
		const pressed = rules.find((r) => r.selector === ".btn-icon:where([aria-pressed='true'])");
		expect(pressed, 'the toggle-on state is declared').toBeDefined();
		expect(new Map(pressed!.declarations).get('color')).toBe('var(--accent-on-tint)');
		const iconRules = rules.filter((r) => classesIn(r.selector).includes('btn-icon'));
		expect(iconRules.flatMap((r) => r.declarations).filter(([p]) => p === 'opacity')).toEqual([]);
	});

	it.each(COVERED)('%s carries no local button class', (path) => {
		const local = new Set<string>();
		for (const rule of scopedRules(path)) {
			for (const cls of classesIn(rule.selector)) {
				const isButtonish = cls.startsWith('btn-') || cls.endsWith('-btn');
				if (!isButtonish || (SHARED as readonly string[]).includes(cls)) continue;
				local.add(cls);
			}
		}
		expect([...local]).toEqual([]);
	});

	it.each(COVERED)('%s only places shared buttons, never paints them', (path) => {
		const painted: string[] = [];
		for (const rule of scopedRules(path)) {
			if (!classesIn(rule.selector).some((c) => (SHARED as readonly string[]).includes(c))) {
				continue;
			}
			for (const [prop] of rule.declarations) {
				if (PAINT.test(prop)) painted.push(`${rule.selector} { ${prop} }`);
			}
		}
		expect(painted).toEqual([]);
	});

	it.each(COVERED)('%s uses no button class app.css does not define', (path) => {
		const used = new Set(
			[...markup(SOURCES.get(path) ?? '').matchAll(BTN_CLASS)].map((m) => m[0])
		);
		const undefinedHere = [...used].filter(
			(cls) =>
				!APP_CSS_CLASSES.has(cls) &&
				!scopedRules(path).some((r) => classesIn(r.selector).includes(cls))
		);
		// A class neither shared nor styled locally is either an e2e hook kept on
		// a migrated button (allowed below) or a typo that renders unstyled.
		expect(undefinedHere.filter((c) => !E2E_HOOKS.has(`${path} ${c}`))).toEqual([]);
	});

	it('reads a whole class name, not a btn- fragment of a longer one', () => {
		const found = [...markup('<span class="bulk-btn-wrap"><b class="btn-x">').matchAll(BTN_CLASS)].map((m) => m[0]);
		expect(found).toEqual(['btn-x']);
	});

	it('the guard catches a copy and a paint override', () => {
		const rules = parseRules('.btn-save { color: red; } .composer .btn-primary { background: red; }');
		expect(classesIn(rules[0].selector)).toEqual(['btn-save']);
		expect(rules[1].declarations.some(([p]) => PAINT.test(p))).toBe(true);
		expect(PAINT.test('align-self')).toBe(false);
		expect(PAINT.test('padding')).toBe(false);
	});
});

