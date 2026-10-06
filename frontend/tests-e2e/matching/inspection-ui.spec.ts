import { API_BASE, authToken, expect, signInAndWait, tenantBase, tenantHeaders, tenantPsql, test } from '../fixtures/helpers';
import {
	cleanup,
	createGr,
	createInspectionRow,
	createMatchedInvoice,
	createPo,
	exceptionsFor,
	issueText,
	uniq
} from './setup';
import { expectNoA11yViolations } from '../a11y/axe-helper';
import type { Page } from '@playwright/test';

/**
 * Recording a quality inspection THROUGH THE APP — `/goods-receipts`.
 *
 * The sibling `four-way-inspection.spec.ts` pins the matcher's gate by POSTing
 * `/api/inspections` directly, and `inspections-api.spec.ts` pins that route's
 * own contract. Neither could pin the thing that was actually missing: the app
 * rendered `inspection_result`, `inspection_accepted_quantity` and the
 * `quality_hold` exception on an invoice while offering no way to enter an
 * inspection at all. These specs drive the UI and then assert the SAME matcher
 * outcomes off the API, so a regression in either half fails here:
 *
 *   - a `pass` recorded from a receipt's detail modal → 4-way, still matched;
 *   - a `fail` recorded from the Inspections tab → mismatch + a blocking
 *     `quality_hold`, with the notes typed into the form quoted in the issue;
 *   - a `partial` with an accepted quantity → partial + the quantity the form
 *     sent, which is the case the 4-way leg exists for;
 *   - the role gate (a clerk sees the inspections, and no controls);
 *   - the Sync-from-QMS action reporting what it actually did, including its
 *     409 refusal when the org has no QMS configured.
 */

const RECEIPTS_URL = '/goods-receipts';
const INSPECTIONS_URL = '/goods-receipts?tab=inspections';

/** Delete inspections a UI spec created, by number prefix. They hang off a GR
 *  that `cleanup()` also removes, but the QMS-sync rows resolve to no receipt
 *  at all — nothing else would ever collect those. */
function deleteInspectionsLike(prefix: string): void {
	tenantPsql(`delete from quality_inspections where inspection_number like '${prefix}%';`);
}

async function patchOrgSettings(page: Page, settings: Record<string, unknown>): Promise<void> {
	const headers = { ...tenantHeaders(await authToken(page)), 'Content-Type': 'application/json' };
	const resp = await page.request.patch(`${API_BASE}/api/organization`, {
		headers,
		data: { settings }
	});
	expect(resp.ok()).toBe(true);
}

/** Fill the record-inspection dialog and submit it. The dialog is already open
 *  and its receipt either fixed or selected by the caller. */
async function fillAndSubmit(
	page: Page,
	opts: {
		number: string;
		result: 'pass' | 'fail' | 'partial';
		acceptedQuantity?: string;
		rejectedQuantity?: string;
		notes?: string;
		inspector?: string;
	}
): Promise<void> {
	await page.getByTestId('inspection-number').fill(opts.number);
	await page.getByTestId(`inspection-result-${opts.result}`).check();
	if (opts.acceptedQuantity !== undefined) {
		await page.getByTestId('inspection-accepted-quantity').fill(opts.acceptedQuantity);
	}
	if (opts.rejectedQuantity !== undefined) {
		await page.getByTestId('inspection-rejected-quantity').fill(opts.rejectedQuantity);
	}
	if (opts.inspector !== undefined) await page.getByTestId('inspection-inspector').fill(opts.inspector);
	if (opts.notes !== undefined) await page.getByTestId('inspection-notes').fill(opts.notes);
	await page.getByRole('button', { name: 'Record Inspection', exact: true }).click();
	// The dialog closes only on a successful create; a refusal renders inline.
	await expect(page.getByTestId('record-inspection-form')).toHaveCount(0);
}

test.describe('recording an inspection through /goods-receipts', () => {
	const created: { invoiceIds: string[]; grIds: string[]; poIds: string[] } = {
		invoiceIds: [],
		grIds: [],
		poIds: []
	};
	test.afterAll(() => cleanup(created));

	test('a pass recorded from the receipt detail → 4-way, still matched', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 1000,
			lines: [{ description: 'part', quantity: 10, unitPrice: 100, total: 1000 }]
		});
		created.poIds.push(poId);
		const { grId, grNumber } = createGr({
			poId,
			lines: [{ description: 'part', quantityReceived: 10 }]
		});
		created.grIds.push(grId);

		await page.goto(RECEIPTS_URL);
		// Open the receipt whose delivery this inspection covers. The in-cell
		// RowLink is the keyboard/AT affordance, so the spec uses it too.
		await page.getByRole('button', { name: `View goods receipt ${grNumber}` }).click();
		await expect(page.getByRole('dialog', { name: 'Goods receipt' })).toBeVisible();
		await expect(page.getByText('No inspections recorded for this receipt.')).toBeVisible();

		await page.getByTestId('record-inspection-for-receipt').click();
		// Opened from a receipt, the subject is fixed — no picker to get wrong.
		await expect(page.getByTestId('inspection-receipt-fixed')).toHaveValue(
			`${grNumber} → ${poNumber}`
		);
		const number = uniq('QI-UI-PASS');
		await fillAndSubmit(page, { number, result: 'pass', inspector: 'UI Inspector' });

		// The receipt's own panel now lists it, and so does the tab.
		await expect(page.getByTestId('receipt-inspection-row')).toHaveCount(1);
		await expect(page.getByTestId('receipt-inspection-row')).toContainText(number);
		await page.goto(INSPECTIONS_URL);
		await expect(page.locator(`[data-inspection-number="${number}"]`)).toContainText('Pass');

		// And the matcher reads it: 4-way, the gate clean.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);
		expect(poMatch!.match_type).toBe('4-way');
		expect(poMatch!.inspection_result).toBe('pass');
		expect(poMatch!.status).toBe('matched');
		expect(exceptionsFor(invoiceId)).not.toContain('quality_hold:error');
	});

	test('a fail recorded from the Inspections tab → mismatch + blocking quality_hold', async ({
		page
	}) => {
		const { poId, poNumber } = createPo({
			total: 1000,
			lines: [{ description: 'part', quantity: 10, unitPrice: 100, total: 1000 }]
		});
		created.poIds.push(poId);
		const { grId, grNumber } = createGr({
			poId,
			lines: [{ description: 'part', quantityReceived: 10 }]
		});
		created.grIds.push(grId);

		await page.goto(INSPECTIONS_URL);
		await page.getByTestId('record-inspection').click();
		// No receipt context here, so the form has to be told which delivery.
		await page.getByTestId('inspection-receipt').selectOption(grId);
		const number = uniq('QI-UI-FAIL');
		await fillAndSubmit(page, {
			number,
			result: 'fail',
			rejectedQuantity: '10',
			notes: 'cracked units'
		});

		const row = page.locator(`[data-inspection-number="${number}"]`);
		await expect(row).toContainText('Fail');
		await expect(row).toContainText(grNumber);
		await expect(row).toContainText('cracked units');

		// Amount is within tolerance and the receipt is full — only the failed
		// inspection can knock this down.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);
		expect(poMatch!.match_type).toBe('4-way');
		expect(poMatch!.inspection_result).toBe('fail');
		expect(poMatch!.status).toBe('mismatch');
		// The notes typed into the form are what the reviewer reads on the issue.
		expect(issueText(poMatch!)).toMatch(/Failed quality inspection: cracked units/i);
		expect(exceptionsFor(invoiceId)).toContain('quality_hold:error');
	});

	test('a partial acceptance carries its accepted quantity into the 4-way leg', async ({
		page
	}) => {
		const { poId, poNumber } = createPo({
			total: 1000,
			lines: [{ description: 'part', quantity: 10, unitPrice: 100, total: 1000 }]
		});
		created.poIds.push(poId);
		const { grId } = createGr({ poId, lines: [{ description: 'part', quantityReceived: 10 }] });
		created.grIds.push(grId);

		await page.goto(INSPECTIONS_URL);
		await page.getByTestId('record-inspection').click();
		await page.getByTestId('inspection-receipt').selectOption(grId);
		const number = uniq('QI-UI-PART');
		await page.getByTestId('inspection-number').fill(number);
		await page.getByTestId('inspection-result-partial').check();

		// A partial with no accepted quantity renders as "part of ordered
		// quantity accepted" — true and useless — so the form refuses to send it.
		const submit = page.getByRole('button', { name: 'Record Inspection', exact: true });
		await expect(submit).toBeDisabled();
		await page.getByTestId('inspection-accepted-quantity').fill('7');
		await page.getByTestId('inspection-rejected-quantity').fill('3');
		await expect(submit).toBeEnabled();
		await submit.click();
		await expect(page.getByTestId('record-inspection-form')).toHaveCount(0);

		const row = page.locator(`[data-inspection-number="${number}"]`);
		await expect(row).toContainText('Partial acceptance');
		await expect(row).toContainText('7 / 3');

		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);
		expect(poMatch!.match_type).toBe('4-way');
		expect(poMatch!.inspection_result).toBe('partial');
		expect(poMatch!.status).toBe('partial');
		expect(poMatch!.inspection_accepted_quantity).toBeCloseTo(7, 4);
		expect(issueText(poMatch!)).toMatch(/Partial acceptance: 7 of ordered quantity/i);
		expect(exceptionsFor(invoiceId)).toContain('quality_hold:info');
	});
});

test.describe('inspection controls are admin / ap_manager only', () => {
	const created: { grIds: string[]; poIds: string[] } = { grIds: [], poIds: [] };
	const READER_PREFIX = 'QI-UI-LABEL';
	let grNumber: string;
	let grId: string;
	let readerInspection: string;

	test.beforeAll(() => {
		const { poId } = createPo({ total: 500 });
		created.poIds.push(poId);
		const gr = createGr({ poId, lines: [{ description: 'part', quantityReceived: 5 }] });
		created.grIds.push(gr.grId);
		grNumber = gr.grNumber;
		grId = gr.grId;
		// Seeded in SQL rather than through the API: the spec below signs in as a
		// clerk, who cannot create one — and the point is what a READER sees.
		readerInspection = uniq(READER_PREFIX);
		createInspectionRow({ grId, number: readerInspection });
	});
	test.afterAll(() => {
		deleteInspectionsLike(READER_PREFIX);
		cleanup(created);
	});

	test('a clerk reads the inspections and gets no way to write one', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);

		await page.goto(INSPECTIONS_URL);
		// Read is open to every role — the hint (and the table) render.
		await expect(page.getByTestId('inspections-hint')).toBeVisible();
		// …and neither mutate control does. `require_roles` refuses both
		// regardless; this asserts the page doesn't offer a button that can only
		// 403.
		await expect(page.getByTestId('record-inspection')).toHaveCount(0);
		await expect(page.getByTestId('sync-inspections')).toHaveCount(0);

		await page.goto(RECEIPTS_URL);
		await page.getByRole('button', { name: `View goods receipt ${grNumber}` }).click();
		await expect(page.getByRole('dialog', { name: 'Goods receipt' })).toBeVisible();
		await expect(page.getByTestId('record-inspection-for-receipt')).toHaveCount(0);
	});

	test('a reader gets no receipts-lookup fetch, and the receipt column is labelled anyway', async ({
		page,
		tenantClerk
	}) => {
		// The tab used to drag a `page_size=100` goods-receipts fetch along with
		// EVERY inspection load, purely to turn `gr_id` into a number — and a
		// receipt outside that window still rendered unlabelled, which growing
		// the page size could only move. The row now carries `gr_number` from the
		// server's own join, so the only receipts fetch left backs the record
		// form's PICKER — which a clerk cannot open, so none is issued at all.
		await signInAndWait(page, tenantClerk);
		const lookups: string[] = [];
		page.on('request', (req) => {
			const url = new URL(req.url());
			if (url.pathname === '/api/goods-receipts' && url.searchParams.get('page_size') === '100') {
				lookups.push(req.url());
			}
		});

		await page.goto(INSPECTIONS_URL);
		// Labelled from the row itself — the receipt's real number, not the
		// generic "View receipt" fallback an unresolvable id renders.
		await expect(page.locator(`[data-inspection-number="${readerInspection}"]`)).toContainText(
			grNumber
		);
		expect(lookups).toEqual([]);
	});
});

test.describe('Sync from QMS reports what it did', () => {
	const SYNC_PREFIX = 'QI-UI-SYNC';
	let originalQms: unknown = null;

	test.beforeAll(async ({ browser, tenantSlug }) => {
		// Capture whatever `settings.qms` holds so afterAll restores it — the QMS
		// config is org-wide and must not leak into sibling specs.
		const ctx = await browser.newContext({ baseURL: tenantBase(tenantSlug) });
		const p = await ctx.newPage();
		await signInAndWait(p);
		const resp = await p.request.get(`${API_BASE}/api/organization`, {
			headers: tenantHeaders(await authToken(p))
		});
		originalQms = ((await resp.json()) as { settings?: Record<string, unknown> }).settings?.qms ?? null;
		await ctx.close();
	});

	test.afterAll(async ({ browser, tenantSlug }) => {
		deleteInspectionsLike(SYNC_PREFIX);
		const ctx = await browser.newContext({ baseURL: tenantBase(tenantSlug) });
		const p = await ctx.newPage();
		await signInAndWait(p);
		await patchOrgSettings(p, { qms: originalQms });
		await ctx.close();
	});

	test('refuses with the backend explanation when no QMS is configured', async ({ page }) => {
		// `authToken` reads localStorage, so the page has to be on the tenant
		// origin before any API call — navigate first, then configure.
		await page.goto(INSPECTIONS_URL);
		await patchOrgSettings(page, { qms: null });
		await page.getByTestId('sync-inspections').click();
		// The 409's own text IS the outcome — an operator asked for this pull
		// directly, so a clean all-zero summary would hide why nothing happened.
		await expect(
			page.locator('.toast.error', { hasText: /No QMS is configured/i })
		).toBeVisible();
	});

	test('reports the counts, then reports an idempotent re-run as up to date', async ({ page }) => {
		await page.goto(INSPECTIONS_URL);
		// Opt the org in with the local-first `mock` adapter, overriding its
		// fixtures so this spec owns every row it creates (the default set is
		// shared, and its numbers would collide across reruns).
		await patchOrgSettings(page, {
			qms: {
				provider: 'mock',
				mock_records: [
					{
						inspection_number: `${SYNC_PREFIX}-001`,
						result: 'pass',
						inspected_date: '2024-01-15',
						inspector: 'QMS Auto',
						accepted_quantity: 100
					},
					{
						inspection_number: `${SYNC_PREFIX}-002`,
						result: 'fail',
						inspected_date: '2024-01-16',
						rejected_quantity: 50,
						deviation_notes: 'surface finish out of spec'
					}
				]
			}
		});
		deleteInspectionsLike(SYNC_PREFIX);

		await page.getByTestId('sync-inspections').click();
		await expect(
			page.locator('.toast', { hasText: /2 added.*2 fetched/ })
		).toBeVisible();

		// Both records reference PO / GR numbers this tenant does not hold, so
		// they resolve to neither — the state that exists nowhere else in the UI,
		// and the reason the flat tab is here at all.
		const synced = page.locator(`[data-inspection-number="${SYNC_PREFIX}-001"]`);
		await expect(synced).toContainText('Pass');
		await expect(synced).toContainText('Not linked');
		await expect(page.locator(`[data-inspection-number="${SYNC_PREFIX}-002"]`)).toContainText(
			'Fail'
		);

		// The upsert is keyed on (org, inspection_number), so a second pull
		// changes nothing — and has to SAY so, rather than reporting a clean run
		// indistinguishable from one that found new work.
		await page.getByTestId('sync-inspections').click();
		await expect(
			page.locator('.toast', { hasText: /already up to date/i })
		).toBeVisible();
	});
});

test.describe('the inspection surface is WCAG 2.2 AA clean', () => {
	const created: { grIds: string[]; poIds: string[] } = { grIds: [], poIds: [] };

	test.beforeAll(() => {
		const { poId } = createPo({ total: 750 });
		created.poIds.push(poId);
		created.grIds.push(createGr({ poId, lines: [{ description: 'part', quantityReceived: 5 }] }).grId);
	});
	test.afterAll(() => cleanup(created));

	// Scanned here rather than added to `a11y/axe.spec.ts`'s route table because
	// the controls this change introduces are inside a DIALOG — a radio
	// fieldset/legend, two decimal text inputs, a persistent role="alert"
	// refusal region — and a list-only scan would report the page clean while
	// none of them were checked. The tab bar + table are covered on the way in.
	test('the Inspections tab and the record dialog have no axe violations', async ({ page }) => {
		await page.goto(INSPECTIONS_URL);
		await expect(page.locator('aside.sidebar').first()).toBeVisible();
		await expect(page.getByTestId('inspections-hint')).toBeVisible();
		await expectNoA11yViolations(page);

		await page.getByTestId('record-inspection').click();
		await expect(page.getByTestId('record-inspection-form')).toBeVisible();
		// `partial` renders the two quantity fields, so scan the widest form.
		await page.getByTestId('inspection-result-partial').check();
		await expect(page.getByTestId('inspection-accepted-quantity')).toBeVisible();
		await expectNoA11yViolations(page);
	});
});
