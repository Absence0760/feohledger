import { API_BASE, authToken, chartGlCode, expect, tenantBase, tenantHeaders, test } from '../fixtures/helpers';
import {
	cleanup,
	createGr,
	createMatchedInvoice,
	createPo,
	exceptionsFor,
	recompute
} from './setup';

/**
 * 4-way matching — the Quality Inspection gate.
 *
 * After the 3-way GR leg, the matcher folds in the most-recent
 * `QualityInspection` (preferring one tied to the matched GR, else the PO):
 *   - `pass`    → no status change (clean gate)
 *   - `fail`    → status mismatch + quality_hold ERROR exception (blocks)
 *   - `partial` → status partial + quality_hold INFO (pay-only-accepted)
 * These specs create the inspection through the real POST /api/inspections
 * route, then re-run the matcher and pin the gate outcome + exception routing.
 *
 * They stay on the API deliberately: what they pin is the MATCHER's gate, and
 * routing each one through the form would make a matcher regression fail as a
 * UI failure (and re-test the same three form paths four times over).
 * `inspection-ui.spec.ts` is the other half — it records a pass, a fail and a
 * partial acceptance through `/goods-receipts` and then asserts these same
 * matcher outcomes, so the entry path is covered without weakening what is
 * asserted here.
 */

async function createInspection(
	page: import('@playwright/test').Page,
	body: {
		inspection_number: string;
		gr_id?: string;
		po_id?: string;
		result: 'pass' | 'fail' | 'partial';
		accepted_quantity?: number;
		deviation_notes?: string;
	}
): Promise<{ status: number; id?: string }> {
	const headers = { ...tenantHeaders(await authToken(page)), 'Content-Type': 'application/json' };
	const resp = await page.request.post(`${API_BASE}/api/inspections`, { headers, data: body });
	if (!resp.ok()) return { status: resp.status() };
	return { status: resp.status(), id: ((await resp.json()) as { id: string }).id };
}

test.describe('4-way Quality Inspection gate', () => {
	const created: { invoiceIds: string[]; grIds: string[]; poIds: string[] } = {
		invoiceIds: [],
		grIds: [],
		poIds: []
	};
	test.afterAll(() => cleanup(created));

	test('passing inspection → 4-way, status stays matched, no quality_hold', async ({ page }) => {
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

		const ins = await createInspection(page, {
			inspection_number: `QI-PASS-${grNumber}`,
			gr_id: grId,
			result: 'pass'
		});
		expect(ins.status).toBe(201);

		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.match_type).toBe('4-way');
		expect(poMatch!.inspection_result).toBe('pass');
		expect(poMatch!.status).toBe('matched');
		expect(exceptionsFor(invoiceId)).not.toContain('quality_hold:error');
	});

	test('failing inspection → mismatch + quality_hold ERROR (blocks)', async ({ page }) => {
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

		const ins = await createInspection(page, {
			inspection_number: `QI-FAIL-${grNumber}`,
			gr_id: grId,
			result: 'fail',
			deviation_notes: 'cracked units'
		});
		expect(ins.status).toBe(201);

		// Amount is within tolerance and receipt is full — only the failed
		// inspection should knock the match down to mismatch.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.match_type).toBe('4-way');
		expect(poMatch!.inspection_result).toBe('fail');
		expect(poMatch!.status).toBe('mismatch');
		expect(poMatch!.issues.join(' ')).toMatch(/Failed quality inspection: cracked units/i);
		expect(exceptionsFor(invoiceId)).toContain('quality_hold:error');
	});

	test('partial inspection → partial + quality_hold INFO with accepted qty', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 1000,
			lines: [{ description: 'part', quantity: 10, unitPrice: 100, total: 1000 }]
		});
		created.poIds.push(poId);
		// Full receipt so the only downgrade comes from the partial acceptance.
		const { grId, grNumber } = createGr({
			poId,
			lines: [{ description: 'part', quantityReceived: 10 }]
		});
		created.grIds.push(grId);

		const ins = await createInspection(page, {
			inspection_number: `QI-PART-${grNumber}`,
			gr_id: grId,
			result: 'partial',
			accepted_quantity: 7
		});
		expect(ins.status).toBe(201);

		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.match_type).toBe('4-way');
		expect(poMatch!.inspection_result).toBe('partial');
		expect(poMatch!.status).toBe('partial');
		expect(poMatch!.inspection_accepted_quantity).toBeCloseTo(7, 4);
		expect(poMatch!.issues.join(' ')).toMatch(/Partial acceptance: 7 of ordered quantity/i);
		expect(exceptionsFor(invoiceId)).toContain('quality_hold:info');
	});

	test('a failed inspection added after a clean match re-gates on recompute', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 1000,
			lines: [{ description: 'part', quantity: 5, unitPrice: 200, total: 1000 }]
		});
		created.poIds.push(poId);
		const { grId, grNumber } = createGr({
			poId,
			lines: [{ description: 'part', quantityReceived: 5 }]
		});
		created.grIds.push(grId);

		// First: no inspection yet → clean 3-way matched.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);
		expect(poMatch!.status).toBe('matched');
		expect(poMatch!.match_type).toBe('3-way');

		// Then a failing inspection lands and the invoice is touched again.
		const ins = await createInspection(page, {
			inspection_number: `QI-LATE-${grNumber}`,
			gr_id: grId,
			result: 'fail',
			deviation_notes: 'late reject'
		});
		expect(ins.status).toBe(201);

		const after = await recompute(page, invoiceId);
		expect(after!.match_type).toBe('4-way');
		expect(after!.status).toBe('mismatch');
		expect(exceptionsFor(invoiceId)).toContain('quality_hold:error');
	});
});

test.describe('require_inspection rule (4-way gate when no inspection exists)', () => {
	const created: { invoiceIds: string[]; poIds: string[] } = { invoiceIds: [], poIds: [] };
	let originalSettings: Record<string, unknown> | null = null;

	test.beforeAll(async ({ browser, tenantSlug }) => {
		// Capture the org settings so afterAll can restore them — the matching
		// config is org-wide and must not leak into sibling specs.
		const ctx = await browser.newContext({ baseURL: tenantBase(tenantSlug) });
		const page = await ctx.newPage();
		const { signInAndWait } = await import('../fixtures/helpers');
		await signInAndWait(page);
		const headers = tenantHeaders(await authToken(page));
		const resp = await page.request.get(`${API_BASE}/api/organization`, { headers });
		originalSettings = ((await resp.json()) as { settings: Record<string, unknown> }).settings ?? {};
		await ctx.close();
	});

	test.afterAll(async ({ browser, tenantSlug }) => {
		cleanup(created);
		// Restore the org's matching settings to whatever they were.
		const ctx = await browser.newContext({ baseURL: tenantBase(tenantSlug) });
		const page = await ctx.newPage();
		const { signInAndWait } = await import('../fixtures/helpers');
		await signInAndWait(page);
		const headers = { ...tenantHeaders(await authToken(page)), 'Content-Type': 'application/json' };
		const restore = { ...(originalSettings ?? {}) };
		delete (restore as Record<string, unknown>).matching;
		await page.request.patch(`${API_BASE}/api/organization`, {
			headers,
			data: { settings: { matching: null } }
		});
		await ctx.close();
	});

	async function setMatching(
		page: import('@playwright/test').Page,
		matching: Record<string, unknown> | null
	) {
		const headers = { ...tenantHeaders(await authToken(page)), 'Content-Type': 'application/json' };
		const resp = await page.request.patch(`${API_BASE}/api/organization`, {
			headers,
			data: { settings: { matching } }
		});
		expect(resp.ok()).toBe(true);
	}

	test('org-wide require_inspection on + no inspection → inspection_required + quality_hold warning', async ({
		page
	}) => {
		await setMatching(page, { require_inspection: true, tolerance_pct: 5.0 });

		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);
		// Matched amount, no GR, no inspection — require_inspection forces the gate.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.inspection_required).toBe(true);
		expect(poMatch!.inspection_result).toBeNull();
		expect(poMatch!.issues.join(' ')).toMatch(/Quality inspection required but missing/i);
		expect(exceptionsFor(invoiceId)).toContain('quality_hold:warning');

		await setMatching(page, null);
	});

	test('require_inspection off (default) + no inspection → no quality_hold', async ({ page }) => {
		await setMatching(page, null);

		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.inspection_required).toBe(false);
		expect(poMatch!.status).toBe('matched');
		expect(exceptionsFor(invoiceId)).not.toContain('quality_hold:warning');
	});

	test('commodity (GL-account) rule lowers tolerance for a specific GL', async ({ page }) => {
		// A tight 1% tolerance for one GL only; org default stays 5%. The GL is a
		// real account of the tenant's chart — an invoice write refuses any other
		// (docs/decisions.md §199).
		const gl = chartGlCode();
		await setMatching(page, {
			tolerance_pct: 5.0,
			commodity_rules: { [gl]: { tolerance_pct: 1.0 } }
		});

		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);
		// +3% — fine under org default (5%) but BREACHES that GL's rule (1%).
		const { invoiceId, poMatch } = await createMatchedInvoice(page, {
			poNumber,
			amount: 1030,
			glAccount: gl
		});
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.amount_variance_pct).toBeCloseTo(3, 2);
		// The commodity rule's 1% tolerance is what was applied → mismatch.
		expect(poMatch!.within_tolerance).toBe(false);
		expect(poMatch!.status).toBe('mismatch');
		expect(poMatch!.details.tolerance_pct).toBeCloseTo(1.0, 4);

		await setMatching(page, null);
	});
});
