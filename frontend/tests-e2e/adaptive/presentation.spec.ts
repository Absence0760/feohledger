import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';
import type { Page, Request, Route } from '@playwright/test';

/**
 * `/adaptive` presentation contracts — four things the page must not misreport.
 *
 * Every response is stubbed, because three of the four states cannot be
 * produced on demand against a seeded tenant: an invoice with no usable FX rate
 * lock, an `info`-severity flag, and a vendor whose approvals were partly
 * excluded from its own average.
 *
 * 1. An anomaly row's invoice id was an 8-character stub — not openable, not
 *    selectable, not enough to paste anywhere.
 * 2. `amount` is only in the reporting currency when the invoice could be
 *    converted into it; otherwise the backend falls back to the BILLED figure
 *    "for DISPLAY only". The page stamped the org's reporting currency on it
 *    either way — a wrong number, not a missing one.
 * 3. `severity === 'error' ? 'danger' : 'warning'` painted every `info` flag as
 *    a warning, including the one that reports the ABSENCE of a verdict.
 * 4. The vendor average excludes approvals that `sample_size` still counts, so
 *    the exclusion has to be disclosed rather than left to be inferred
 *    (decisions §79/§82).
 *
 * Route globs dispatch on the exact PATHNAME: `**\/api/adaptive*` also matches
 * Vite's dev-server module URL for `/src/lib/api/adaptive.ts`, and fulfilling
 * that with JSON blanks the whole page.
 */

const PATH = {
	suggestions: '/api/adaptive/suggestions',
	threshold: '/api/adaptive/threshold-recommendation',
	patterns: '/api/adaptive/approval-patterns',
	anomalies: '/api/adaptive/anomalies'
} as const;

const CONVERTED_ID = '44444444-4444-4444-4444-444444444444';
const BILLED_ONLY_ID = '55555555-5555-5555-5555-555555555555';

function pathOf(request: Request): string {
	return new URL(request.url()).pathname;
}

function json(route: Route, body: unknown, status = 200) {
	return route.fulfill({
		status,
		contentType: 'application/json',
		body: JSON.stringify(body)
	});
}

const ANOMALIES = {
	total_scanned: 2,
	flagged: [
		{
			// Convertible: the figure IS in the reporting currency, and the flag
			// that fired is a real amount verdict.
			invoice_id: CONVERTED_ID,
			vendor_id: null,
			vendor_name: 'Steady Vendor',
			amount: '48000.00',
			amount_currency: 'EUR',
			insufficient_history: false,
			baseline: null,
			flags: [
				{
					code: 'amount_high',
					severity: 'warning',
					message: 'Amount 48000.00 is 4.2σ above this vendor’s mean of 310.00',
					observed: '48000.00',
					expected: '900.00'
				}
			]
		},
		{
			// Not convertible: the amount is the BILLED figure in the invoice's own
			// currency, and the only flag is the info one saying no comparison was
			// possible.
			invoice_id: BILLED_ONLY_ID,
			vendor_id: null,
			vendor_name: 'Tokyo Parts KK',
			amount: '48000.00',
			amount_currency: 'JPY',
			insufficient_history: false,
			baseline: null,
			flags: [
				{
					code: 'amount_comparison_unavailable',
					severity: 'info',
					message:
						'Amount could not be expressed in EUR (no locked FX rate on this invoice), so it was not compared against this vendor’s baseline',
					observed: '48000.00',
					expected: '310.00'
				}
			]
		}
	]
};

function vendorPattern(overrides: Record<string, unknown> = {}) {
	return {
		vendor_id: null,
		vendor_name: 'Steady Vendor',
		approved_count: 10,
		rejected_count: 2,
		approval_rate_pct: '83.3',
		unmodified_count: 9,
		consistency_pct: '90.0',
		avg_approved_amount: '310.00',
		median_approved_amount: '300.00',
		min_approved_amount: '100.00',
		max_approved_amount: '900.00',
		sample_size: 12,
		unconverted_count: 0,
		...overrides
	};
}

function patterns(vendors: Array<Record<string, unknown>>) {
	return {
		generated_at: '2026-09-01T10:00:00+00:00',
		lookback_days: 180,
		entity_id: null,
		approvers: [],
		vendors
	};
}

/** Stub the two calls the page makes on mount so nothing depends on real history. */
async function stubPageLoad(page: Page) {
	await page.route('**/api/adaptive/**', async (route) => {
		const p = pathOf(route.request());
		if (p === PATH.suggestions) return json(route, { suggestions: [] });
		if (p === PATH.threshold) {
			return json(route, {
				should_raise: false,
				current_threshold: '1000.00',
				recommended_threshold: '1000.00',
				cap_threshold: '5000.00',
				qualifying_vendor_count: 0,
				total_clean_invoices: 0,
				reason_code: 'no_increase',
				rationale: 'Nothing to raise.',
				evidence: [],
				workflow_id: null,
				lookback_days: 365
			});
		}
		return route.continue();
	});
}

async function openAnomalies(page: Page) {
	await stubPageLoad(page);
	await page.route('**/api/adaptive/anomalies**', async (route) => {
		if (pathOf(route.request()) !== PATH.anomalies) return route.continue();
		return json(route, ANOMALIES);
	});
	await page.goto('/adaptive');
	await page.getByRole('tab', { name: 'Anomalies' }).click();
	// Count, not `.first()`: the DataTable renders a single loading/empty row
	// before the response lands, and that row would satisfy a visibility wait.
	await expect(page.locator('#adaptive-panel-anomalies table tbody tr')).toHaveCount(
		ANOMALIES.flagged.length,
		{ timeout: 15_000 }
	);
}

async function openPatterns(page: Page, vendors: Array<Record<string, unknown>>) {
	await stubPageLoad(page);
	await page.route('**/api/adaptive/approval-patterns**', async (route) => {
		if (pathOf(route.request()) !== PATH.patterns) return route.continue();
		return json(route, patterns(vendors));
	});
	await page.goto('/adaptive');
	await page.getByRole('tab', { name: 'Approval patterns' }).click();
	// The vendors table is the second of the panel's two. Waiting on its ROW
	// COUNT (callers always pass two vendors, the empty state is one row) is
	// what makes the negative assertion below meaningful: waiting on the table
	// element alone would be satisfied by the pre-response empty state, and
	// "no disclosure rendered" would pass before the data arrived.
	await expect(
		page.locator('#adaptive-panel-patterns table').nth(1).locator('tbody tr')
	).toHaveCount(vendors.length, { timeout: 15_000 });
}

test.describe('/adaptive — anomaly rows', () => {
	test('the invoice id opens the invoice and carries the full uuid', async ({ page }) => {
		await openAnomalies(page);

		const link = page.getByRole('link', { name: `Invoice ${CONVERTED_ID}` });
		await expect(link).toBeVisible();
		// The deep link the rest of the app already uses — /invoices?id= opens
		// that invoice's detail modal.
		await expect(link).toHaveAttribute('href', `/invoices?id=${CONVERTED_ID}`);
		// The visible text is still the readable stub, but the full id is on the
		// cell (hover + copy) and in the accessible name, so it can be pasted.
		await expect(link).toHaveText(CONVERTED_ID.slice(0, 8));
		await expect(page.locator(`td[title="${CONVERTED_ID}"]`)).toBeVisible();

		await link.click();
		await expect(page).toHaveURL(new RegExp(`/invoices\\?id=${CONVERTED_ID}`));
	});

	test('an unconvertible amount is labelled with the currency it is actually in', async ({
		page
	}) => {
		await openAnomalies(page);

		const rows = page.locator('#adaptive-panel-anomalies table tbody tr');
		const convertible = rows.filter({ hasText: 'Steady Vendor' });
		const billedOnly = rows.filter({ hasText: 'Tokyo Parts KK' });

		// Converted → the currency the baseline is in.
		await expect(convertible.locator('.money')).toContainText('€');
		// Not converted → the BILLED currency. Before the fix this rendered with
		// the org's reporting currency symbol, which the figure is not in.
		await expect(billedOnly.locator('.money')).toContainText('¥');
		await expect(billedOnly.locator('.money')).not.toContainText('€');
	});

	test('an info flag is not painted as a warning', async ({ page }) => {
		await openAnomalies(page);

		const infoBadge = page.locator('.badge.amount_comparison_unavailable');
		await expect(infoBadge).toBeVisible();
		await expect(infoBadge).toHaveClass(/\baccent\b/);
		await expect(infoBadge).not.toHaveClass(/\bwarning\b/);

		// …and a genuine warning still is one, so the map isn't just flattening
		// every severity to the quiet tone.
		const warnBadge = page.locator('.badge.amount_high');
		await expect(warnBadge).toHaveClass(/\bwarning\b/);
	});

	test('the anomalies panel has no axe violations', async ({ page }) => {
		await openAnomalies(page);
		await expectNoA11yViolations(page);
	});
});

test.describe('/adaptive — vendor averages', () => {
	test('excluded approvals are disclosed beside the average', async ({ page }) => {
		await openPatterns(page, [
			vendorPattern({ unconverted_count: 3 }),
			vendorPattern({ vendor_name: 'Local Co', unconverted_count: 0 })
		]);

		// The average is over 10 - 3 = 7 approvals while "Sample" reads 12. The
		// page must say so rather than present the two as agreeing.
		const disclosure = page.getByTestId('adaptive-vendor-unconverted');
		await expect(disclosure).toBeVisible();
		await expect(disclosure).toHaveAttribute('role', 'alert');

		// The denominator itself is untouched — the sample count still reports
		// every decision.
		const row = page.locator('#adaptive-panel-patterns tbody tr', { hasText: 'Steady Vendor' });
		await expect(row).toHaveAttribute('data-unconverted', '3');
		await expect(row.locator('td').last()).toHaveText('12');
	});

	test('a single-currency tenant sees no disclosure', async ({ page }) => {
		await openPatterns(page, [vendorPattern(), vendorPattern({ vendor_name: 'Local Co' })]);
		await expect(page.getByTestId('adaptive-vendor-unconverted')).toHaveCount(0);
	});
});

/**
 * The threshold figures and the vendor averages carry no currency of their own
 * on the wire, so they are labelled from the `orgCurrency` store — and when the
 * org's settings resolve nothing a client can read, the store answers `null`
 * and they render BARE. It used to answer `USD`, which put a `$` on every one
 * of them for any org that had not set a currency, and on every one of them
 * before the store had loaded. The backend's last rung
 * (`settings.reporting_currency_default`) is operator config no client can
 * see, so a `USD` here was a guess wearing the look of a configured answer
 * (`docs/decisions.md` §119, §200).
 */
async function stubOrgSettings(page: Page, settings: Record<string, unknown>) {
	await page.route(
		(url) => url.pathname === '/api/organization',
		(route) => route.fulfill({ json: { settings } })
	);
}

async function openThreshold(page: Page) {
	await stubPageLoad(page);
	await page.goto('/adaptive');
	await page.getByRole('tab', { name: 'Auto-approve threshold' }).click();
	const card = page.getByTestId('adaptive-threshold-card');
	await expect(card.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value', {
		timeout: 15_000
	});
	return card;
}

test.describe('/adaptive — figures labelled from the org store', () => {
	test('an org whose settings resolve no currency sees the thresholds BARE, never in dollars', async ({
		page
	}) => {
		await stubOrgSettings(page, {});
		const card = await openThreshold(page);

		const current = card.locator('.kpi', { hasText: 'Current threshold' }).locator('.kpi-value');
		await expect(current).toHaveText('1,000.00');
		await expect(card).not.toContainText('$');
		await expect(card.locator('.kpi', { hasText: 'Cap' }).locator('.kpi-value')).toHaveText(
			'5,000.00'
		);
	});

	test('the same figures wear the org code once it resolves', async ({ page }) => {
		// The contrast case, so the bare rendering above is proven to be about
		// the unresolved store and not about the figure itself.
		await stubOrgSettings(page, { reporting_currency: 'EUR' });
		const card = await openThreshold(page);

		const current = card.locator('.kpi', { hasText: 'Current threshold' }).locator('.kpi-value');
		await expect(current).toHaveText('€1,000.00');
	});
});
