import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	tenantBase,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * A warning FINDING is localized, not just the frame around it.
 *
 * `decisions.md` §155 keyed the `/invoices` warning icon's aria-label and said
 * plainly what that bought: a localized sentence AROUND server-English
 * findings, because `services/invoice_warnings.py` composed each one per row
 * from that row's own data. §157 closed it — every warning now carries a stable
 * `code` plus typed `params`, and `api/invoiceWarnings.ts` resolves them
 * through a GENERATED catalogue.
 *
 * This spec pins the two halves that only the real stack can prove:
 *
 *   1. The API actually emits `{code, params}` beside `message` — the unit
 *      tests on either side both work off fixtures, so nothing else asserts
 *      that the round trip carries them.
 *   2. A German reader sees a German finding with a German-formatted amount.
 *      That is the whole point, and it cannot regress silently: if the code
 *      stops resolving, the fallback renders the backend's English sentence
 *      and this spec goes red.
 *
 * The datum is owned rather than borrowed (same reasoning as
 * `locale-formats-on-load.spec.ts`): a round amount of 5000.00 EUR reliably
 * fires the `round_amount` fraud rule (>= 1000 and an even multiple of 100),
 * and EUR makes the German number format unmistakable — `5.000,00 €` shares no
 * substring with en-US's `€5,000.00`.
 */

interface InvoiceWarning {
	type: string;
	severity: string;
	message: string;
	code?: string | null;
	params?: Record<string, string | number> | null;
}

interface InvoiceResp {
	id: string;
	warnings: InvoiceWarning[] | null;
}

/** Round, four-figure, and an even multiple of 100 — fires `round_amount`. */
const ROUND_AMOUNT = '5000.00';

test.describe('invoice warning findings are localized', () => {
	let invoiceId: string | null = null;
	let slug: string;

	test.afterEach(() => {
		if (!invoiceId) return;
		try {
			tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${invoiceId}'`, slug);
			tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${invoiceId}'`, slug);
			deleteInvoicesWhere(`id='${invoiceId}'`, slug);
		} catch {
			/* best-effort */
		}
		invoiceId = null;
	});

	test('a de reader gets a German finding with a German-formatted amount', async ({
		page,
		tenantSlug
	}) => {
		slug = tenantSlug;
		const headers = await authedTenantHeaders(page, tenantSlug);
		const invoiceNumber = `E2E-WARN-I18N-${Date.now()}`;

		const created = await page.request.post(`${API_BASE}/api/invoices`, {
			headers,
			data: {
				vendor: 'E2E Warning i18n Vendor',
				invoice_number: invoiceNumber,
				amount: ROUND_AMOUNT,
				currency: 'EUR',
				status: 'new'
			}
		});
		expect(created.status(), 'fixture invoice must be created').toBe(201);
		invoiceId = ((await created.json()) as InvoiceResp).id;

		// `refresh_warnings` runs on every MUTATION, not on bare create — the same
		// contract `invoices/duplicate-fraud-detection.spec.ts` documents.
		const patched = await page.request.patch(`${API_BASE}/api/invoices/${invoiceId}`, {
			headers,
			data: { notes: `touched ${Date.now()}` }
		});
		expect(patched.status(), 'patch recomputes warnings').toBe(200);
		const warnings = ((await patched.json()) as InvoiceResp).warnings ?? [];

		const round = warnings.find((w) => w.code === 'round_amount');
		expect(
			round,
			`no round_amount warning in ${JSON.stringify(warnings.map((w) => w.code ?? w.type))}`
		).toBeTruthy();
		// The wire contract: exact decimal digits for money (never a float), the
		// ISO code beside it, and the English fallback still on the payload.
		expect(round!.params?.amount).toBe(ROUND_AMOUNT);
		expect(round!.params?.currency).toBe('EUR');
		expect(round!.message).toContain('Round amount');

		// Seed the picker's own storage key before any app code runs, so this is
		// the path a returning German user takes.
		await page.addInitScript(() => {
			localStorage.setItem('feoh_locale', 'de');
		});
		await page.goto(`${tenantBase(tenantSlug)}/invoices`);

		const listed = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes(`search=${encodeURIComponent(invoiceNumber)}`) &&
				r.request().method() === 'GET'
		);
		// By contract class, not placeholder text — under `feoh_locale=de` the
		// placeholder is German, which is what this spec is about.
		await page.locator('.search-box input').first().fill(invoiceNumber);
		await listed;

		const row = page.locator('table tbody tr', { hasText: invoiceNumber }).first();
		await expect(row).toBeVisible();

		const icon = row.locator('.warning-icon').first();
		await expect(icon).toBeVisible();
		const title = (await icon.getAttribute('title')) ?? '';

		// The finding itself, in German, with the amount in German number format.
		expect(title).toContain('Runder Betrag');
		expect(title).toContain('5.000,00');
		// And NOT the server's English sentence — the fallback rendering, which is
		// exactly what shipped before §157 and what a broken code lookup restores.
		expect(title).not.toContain('Round amount');
		expect(title).not.toContain('5,000.00');
	});
});
