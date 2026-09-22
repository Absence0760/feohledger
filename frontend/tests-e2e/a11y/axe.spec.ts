import type { EligibleInvoice } from '$lib/types/creditMemo';
import { expect, invoicePicker, NO_TENANT_BASE, test, vendorPicker } from '../fixtures/helpers';
import { expectNoA11yViolations } from './axe-helper';

/**
 * Automated accessibility regression guard (WCAG 2.2 Level AA).
 *
 * Runs axe-core against the key surfaces of the app at the WCAG 2.0/2.1/2.2
 * A + AA tag set and asserts zero violations. This is the automated leg of the
 * conformance posture documented in `docs/accessibility.md` /
 * `docs/accessibility-vpat.md` — it catches the machine-detectable regressions
 * (missing labels, contrast, ARIA misuse, landmark/heading structure, target
 * size) so a future change can't silently undo the a11y fixes this initiative
 * landed. Manual screen-reader passes (VoiceOver / NVDA / TalkBack) cover the
 * criteria axe can't assert; see the docs.
 *
 * Pattern: reuses the per-worker `e2e<N>` tenant + storage-state fixtures from
 * `fixtures/helpers.ts` exactly like every other spec — the authenticated specs
 * inherit the default admin storage state; the two unauthenticated surfaces
 * (the AP login page and the supplier portal login) opt out of it.
 *
 * It is auto-discovered by the normal `pnpm test:e2e` run (the config's
 * `testDir: '.'` walks recursively), so CI catches a11y regressions on every
 * push. `pnpm test:e2e:a11y` (frontend/package.json) targets just this folder
 * for a fast focused run.
 */

/**
 * Authenticated surfaces — the core invoice → approve → pay flow plus a
 * representative sample of the rest of the app shell. Each navigates as the
 * worker's admin (default storage state) and asserts axe-clean.
 *
 * `ready` is the page's own `<h1>`, waited on **in addition to** the sidebar.
 * The sidebar renders before a route's own fetch resolves, so on a page that
 * loads its data (every `/admin` route, `/vendor-statements`) the sidebar alone
 * would let axe scan a loading frame — passing on markup no user ever sees.
 * Omit it for the routes that render their shell synchronously.
 */
const AUTHED_ROUTES: { path: string; name: string; ready?: string }[] = [
	{ path: '/', name: 'dashboard' },
	{ path: '/invoices', name: 'invoices list' },
	{ path: '/vendors', name: 'vendors list' },
	{ path: '/payments', name: 'payments' },
	{ path: '/exceptions', name: 'exceptions queue' },
	// The `/admin` section carries the app's densest cluster of a11y-sensitive
	// controls — modal dialogs, armed two-click destructive actions, and
	// one-time secret reveals whose focus management is the only thing standing
	// between a user and a credential they can never see again. It had no axe
	// coverage at all, which is exactly where a labelling or focus regression is
	// most costly and least likely to be noticed.
	{ path: '/admin', name: 'admin users', ready: 'Users & Roles' },
	{ path: '/admin?tab=roles', name: 'admin roles', ready: 'Custom roles' },
	{ path: '/admin/api-keys', name: 'admin API keys', ready: 'API Keys' },
	{ path: '/admin/webhooks', name: 'admin webhooks', ready: 'Webhooks' },
	// A standalone (non-partner) tenant renders the "not a partner" empty state
	// here; the heading is present either way, so the scan is meaningful for
	// both shapes.
	{ path: '/admin/partner', name: 'admin partner', ready: 'Partner Admin' },
	// Its create modal has since gained a radio fieldset/legend intake picker, a
	// file input and a persistent role="alert" refusal region — new interactive
	// controls that had never seen an axe pass. (The list page is scanned here;
	// the modal itself is covered by the dedicated test below.)
	{ path: '/vendor-statements', name: 'vendor statements', ready: 'Statements' },
	// The four routes the guard had been trailing. Widening it to `/admin` +
	// `/vendor-statements` last round caught a real `serious` contrast failure
	// (--text-muted on --surface-2, 4.34:1) — and fixing that turned up the
	// SAME defect on /billing's proration box, found by reading rather than by
	// the guard, because /billing wasn't in this list. These are the pages with
	// saved-card metadata, money readouts and a plan-change dialog, which is
	// exactly where a labelling or contrast regression is worth catching.
	//
	// The headings are the ones the existing specs for these routes already
	// select on (`tests-e2e/{billing,reports,cfo}/`), so a title change breaks
	// them together rather than silently skipping the scan here.
	{ path: '/billing', name: 'billing', ready: 'Billing' },
	{ path: '/reports', name: 'report builder', ready: 'Report Builder' },
	{ path: '/experiments', name: 'workflow experiments', ready: 'Workflow Experiments' },
	{ path: '/cfo', name: 'cash flow', ready: 'Cash Flow' },
	// Round 22's surfaces. Each shipped with an axe scan inside its own spec,
	// which proves the page was clean the day it landed but does not keep it
	// clean: this list is what re-scans every route on every run. `/organization`
	// in particular had never been scanned at all despite carrying ~10 panels of
	// forms, armed destructive actions and a one-time secret reveal.
	{ path: '/organization', name: 'organization settings', ready: 'Organization' },
	{ path: '/adaptive', name: 'adaptive workflows', ready: 'Adaptive Workflows' },
	{ path: '/goods-receipts', name: 'goods receipts', ready: 'Goods Receipts' },
	{ path: '/admin/entities', name: 'admin entities', ready: 'Entities' },
	{ path: '/admin/health', name: 'admin sweep health', ready: 'Sweep Health' },
	// `/gl-accounts` — a new route in this round, scanned from the day it
	// landed rather than "when someone widens the list". Its own controls are
	// the ones this suite exists for: a checkbox filter whose only label is the
	// text beside it, a column set that changes shape with that checkbox, and
	// two `<Badge>` tint pairs in the Scope / Status cells.
	{ path: '/gl-accounts', name: 'chart of accounts', ready: 'Chart of Accounts' }
];

test.describe('accessibility — authenticated app (WCAG 2.2 AA)', () => {
	for (const { path, name, ready } of AUTHED_ROUTES) {
		test(`${name} (${path}) has no axe violations`, async ({ page }) => {
			await page.goto(path);
			// The sidebar means we're inside the authenticated app shell;
			// waiting on it ensures axe scans the rendered page, not a
			// mid-redirect / pre-hydration frame.
			await expect(page.locator('aside.sidebar').first()).toBeVisible();
			await expect(page).not.toHaveURL(/\/login/);
			if (ready) {
				// `exact` because several of these headings are substrings of
				// another on the same page ("Webhooks" vs "Webhook deliveries").
				await expect(page.getByRole('heading', { name: ready, exact: true })).toBeVisible();
			}
			await expectNoA11yViolations(page);
		});
	}

	test('vendor-statement create modal (open state) has no axe violations', async ({ page }) => {
		// The modal, not the list, is where this surface's interactive controls
		// live: a radio fieldset/legend intake picker, a file input, and a
		// persistent role="alert" region the backend's refusal message lands in.
		// Scanning only the list would report the page clean while every one of
		// those went unchecked.
		await page.goto('/vendor-statements');
		await expect(page.getByRole('heading', { name: 'Statements', exact: true })).toBeVisible();
		await page.getByRole('button', { name: '+ New reconciliation' }).click();

		// Same dialog + control selectors the dedicated recon spec uses, so the
		// two can't drift apart on a label change.
		const modal = page.getByRole('dialog', { name: 'New vendor statement reconciliation' });
		await expect(modal).toBeVisible();
		// The vendor picker is populated from a fetch — wait for it so the scan
		// covers the fully rendered form rather than a half-built one.
		await expect(vendorPicker(modal)).toBeVisible();

		await expectNoA11yViolations(page);
	});

	test('credit-memo Apply dialog with the invoice picker open has no axe violations', async ({
		page
	}) => {
		// The picker is the dialog's whole interactive surface: a combobox, its
		// listbox of options (two-line when a prior credit has eaten into the
		// invoice), and a live count line wired in as the field's description.
		// Both reads are stubbed on their EXACT pathnames (docs/decisions.md
		// §190), so the scan covers a populated popup on any tenant.
		const memoId = '00000000-0000-4000-b000-0000000000c1';
		await page.route(
			(url) => url.pathname === '/api/credit-memos',
			(route) =>
				route.fulfill({
					json: {
						items: [
							{
								id: memoId,
								memo_number: 'CM-AXE-1',
								vendor_id: '00000000-0000-4000-b001-0000000000c1',
								vendor_name: 'Axe Supplies',
								invoice_id: null,
								invoice_number: null,
								amount: 75,
								currency: 'USD',
								issued_date: '2026-01-01',
								reason: null,
								status: 'open',
								applied_at: null,
								applied_by: null,
								created_at: '2026-01-01T00:00:00Z'
							}
						],
						total: 1,
						page: 1,
						page_size: 20
					}
				})
		);
		const eligible = [
			{
				id: '00000000-0000-4000-b002-0000000000c1',
				invoice_number: 'AXE-INV-1',
				vendor_name: 'Axe Supplies',
				status: 'approved',
				due_date: '2026-02-01',
				amount: 500,
				currency: 'USD',
				creditable_balance: 500
			},
			{
				id: '00000000-0000-4000-b002-0000000000c2',
				invoice_number: 'AXE-INV-2',
				vendor_name: 'Axe Supplies',
				status: 'approved',
				due_date: null,
				amount: 300,
				currency: 'USD',
				creditable_balance: 120
			}
		] satisfies EligibleInvoice[];
		await page.route(
			(url) => url.pathname === `/api/credit-memos/${memoId}/eligible-invoices`,
			(route) => route.fulfill({ json: { items: eligible, total: 2, page: 1, page_size: 25 } })
		);

		await page.goto('/credit-memos');
		await page
			.locator('table tbody tr', { hasText: 'CM-AXE-1' })
			.getByRole('button', { name: 'Apply', exact: true })
			.click();
		const dialog = page.getByRole('dialog', { name: 'Apply credit memo' });
		await expect(dialog).toBeVisible();
		await invoicePicker(dialog, 'Invoice').click();
		await expect(page.getByRole('listbox', { name: 'Invoice' }).getByRole('option')).toHaveCount(2);
		await expect(dialog.getByText('All matches shown (2)')).toBeVisible();

		await expectNoA11yViolations(page);
	});

	test('invoice detail modal (open state) has no axe violations', async ({ page }) => {
		// The invoice detail modal is the most interactive authenticated
		// surface (dialog semantics, focus trap, labelled form fields, the
		// Activity timeline). Open it the same way the detail spec does —
		// click the first row's "Edit invoice …" RowLink — then scan.
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		await page.locator('table tbody tr').first().getByRole('button', { name: 'Edit' }).click();

		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal).toBeVisible();
		// Wait for the Activity timeline to load so the scan covers the fully
		// populated modal, not a loading frame.
		await expect(modal.locator('.line-items-title')).toHaveText('Line Items');

		await expectNoA11yViolations(page);
	});
});

test.describe('accessibility — public surfaces on the no-tenant origin (WCAG 2.2 AA)', () => {
	test.use({ storageState: { cookies: [], origins: [] }, baseURL: NO_TENANT_BASE });

	// Scanned under `prefers-reduced-motion: reduce`, deliberately. Both pages
	// animate text in — the landing's scroll reveals and hero sequence, the
	// auth shell's entrance stagger — and axe's colour-contrast rule measures
	// whatever alpha a node has at the instant it runs, so a scan mid-fade
	// reports a failure no reader ever sees at rest, or passes on a frame
	// before the text is drawn. Reduced motion puts every element in its final
	// state before the scan (the reveal action never hides anything; app.css
	// collapses every animation to its last frame), which is the state the
	// contrast criterion is about. The moving states themselves are covered
	// by tests-e2e/marketing/landing.spec.ts.
	test.beforeEach(async ({ page }) => {
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await page.addInitScript(() => {
			try {
				localStorage.setItem('feoh_consent_choice', 'accepted');
			} catch {
				// about:blank — ignore
			}
		});
	});

	test('marketing landing (/) has no axe violations', async ({ page }) => {
		await page.goto('/');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		await expect(page.locator('.stat-value').first()).toHaveText('7');
		await expectNoA11yViolations(page);
	});

	test('self-service signup (/signup) has no axe violations', async ({ page }) => {
		await page.goto('/signup');
		await expect(page.getByRole('heading', { name: 'Create your workspace' })).toBeVisible();
		await expectNoA11yViolations(page);
	});
});

test.describe('accessibility — unauthenticated surfaces (WCAG 2.2 AA)', () => {
	// These surfaces have no signed-in user: drop the default admin storage
	// state so the page renders its real unauthenticated UI.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('AP login page (/login) has no axe violations', async ({ page }) => {
		// Reduced motion for the same reason as the no-tenant block above: the
		// auth shell staggers its fields in, and a scan mid-fade measures a
		// transient alpha rather than the resting contrast.
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await page.goto('/login');
		await expect(page.locator('input[type="email"]')).toBeVisible();
		await expectNoA11yViolations(page);
	});

	test('forgot password (/login/forgot-password) has no axe violations', async ({ page }) => {
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await page.goto('/login/forgot-password');
		await expect(page.locator('input[type="email"]')).toBeVisible();
		await expectNoA11yViolations(page);
	});

	test('supplier portal login (/portal/login) has no axe violations', async ({ page }) => {
		await page.goto('/portal/login');
		await expect(page.locator('input[type="email"]')).toBeVisible();
		await expectNoA11yViolations(page);
	});

	// The published legal documents. Unauthenticated by design and long-form,
	// so they carry the two things this sweep catches most often in prose:
	// heading order (every section is an h2 under one h1) and link/text
	// contrast against the page background. They are also the surfaces a
	// screen-reader user is most likely to arrive at cold, from an external
	// link, with no app chrome to orient them.
	for (const path of [
		'/legal',
		'/legal/privacy',
		'/legal/terms',
		'/legal/dpa',
		'/legal/sub-processors',
		'/legal/accessibility',
		'/legal/cookies'
	]) {
		test(`${path} has no axe violations`, async ({ page }) => {
			await page.goto(path);
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
			await expectNoA11yViolations(page);
		});
	}
});
