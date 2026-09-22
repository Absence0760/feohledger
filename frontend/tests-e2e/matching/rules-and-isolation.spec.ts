import { ACME_ADMIN, ACME_BASE, API_BASE, authToken, chartGlCode, expect, signInAndWait, tenantBase, tenantHeaders, tenantPsql, test } from '../fixtures/helpers';
import { cleanup, createMatchedInvoice, createPo, recompute, tenantScope } from './setup';

/**
 * matching_rules per-vendor / per-commodity precedence + tenant isolation.
 *
 * `services/matching_rules.resolve_match_rule` resolves tolerance_pct (and
 * require_inspection) per-field with precedence vendor → commodity → org →
 * default. These specs drive a single invoice through the real PATCH→matcher
 * path while flipping org settings, asserting the *applied* tolerance via
 * `po_match.details.tolerance_pct`. The isolation block proves the matcher
 * only ever sees its own tenant's POs.
 */

test.describe('matching_rules tolerance precedence (vendor > commodity > org)', () => {
	const created: { invoiceIds: string[]; poIds: string[] } = { invoiceIds: [], poIds: [] };
	let originalSettings: Record<string, unknown> = {};
	let vendorId: string;

	test.beforeAll(async ({ browser, tenantSlug }) => {
		const ctx = await browser.newContext({ baseURL: tenantBase(tenantSlug) });
		const page = await ctx.newPage();
		await signInAndWait(page);
		const headers = tenantHeaders(await authToken(page));
		const resp = await page.request.get(`${API_BASE}/api/organization`, { headers });
		originalSettings = ((await resp.json()) as { settings: Record<string, unknown> }).settings ?? {};
		await ctx.close();
		// Pick a real vendor row to key the vendor rule on.
		vendorId = tenantPsql('select id from vendors limit 1;').trim();
		expect(vendorId).toBeTruthy();
	});

	test.afterAll(async ({ browser, tenantSlug }) => {
		cleanup(created);
		const ctx = await browser.newContext({ baseURL: tenantBase(tenantSlug) });
		const page = await ctx.newPage();
		await signInAndWait(page);
		const headers = { ...tenantHeaders(await authToken(page)), 'Content-Type': 'application/json' };
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

	test('vendor rule wins over commodity + org for the same invoice', async ({ page }) => {
		// Org=5%, commodity GL=3%, vendor=1%. The invoice carries BOTH the
		// vendor and the GL, so the vendor rule (1%) must be the applied one.
		const gl = chartGlCode();
		await setMatching(page, {
			tolerance_pct: 5.0,
			commodity_rules: { [gl]: { tolerance_pct: 3.0 } },
			vendor_rules: { [vendorId]: { tolerance_pct: 1.0 } }
		});

		const { poId, poNumber } = createPo({ total: 1000, vendorId });
		created.poIds.push(poId);
		// +2% — passes org (5%) and commodity (3%) but BREACHES vendor (1%).
		const { invoiceId, poMatch } = await createMatchedInvoice(page, {
			poNumber,
			amount: 1020,
			vendorId,
			glAccount: gl
		});
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.details.tolerance_pct).toBeCloseTo(1.0, 4);
		expect(poMatch!.within_tolerance).toBe(false);
		expect(poMatch!.status).toBe('mismatch');
	});

	test('commodity rule applies when no vendor rule matches', async ({ page }) => {
		// Org=5%, commodity GL=3%, NO vendor rule for this vendor.
		const gl = chartGlCode();
		await setMatching(page, {
			tolerance_pct: 5.0,
			commodity_rules: { [gl]: { tolerance_pct: 3.0 } }
		});

		const { poId, poNumber } = createPo({ total: 1000, vendorId });
		created.poIds.push(poId);
		// +4% — passes org (5%) but BREACHES commodity (3%).
		const { invoiceId, poMatch } = await createMatchedInvoice(page, {
			poNumber,
			amount: 1040,
			vendorId,
			glAccount: gl
		});
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.details.tolerance_pct).toBeCloseTo(3.0, 4);
		expect(poMatch!.status).toBe('mismatch');
	});

	test('falls back to org default when neither vendor nor commodity matches', async ({ page }) => {
		const gl = chartGlCode();
		await setMatching(page, {
			tolerance_pct: 5.0,
			commodity_rules: { '9999': { tolerance_pct: 1.0 } }
		});

		const { poId, poNumber } = createPo({ total: 1000, vendorId });
		created.poIds.push(poId);
		// +4% with the invoice's GL (no rule) → org 5% applies → still matched.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, {
			poNumber,
			amount: 1040,
			vendorId,
			glAccount: gl
		});
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.details.tolerance_pct).toBeCloseTo(5.0, 4);
		expect(poMatch!.within_tolerance).toBe(true);
		expect(poMatch!.status).toBe('matched');
	});

	test('malformed vendor rule is ignored, tolerance falls through (never raises)', async ({
		page
	}) => {
		// A non-numeric tolerance on the vendor rule must be ignored per the
		// resolver's fail-soft contract; the org default (5%) takes over.
		await setMatching(page, {
			tolerance_pct: 5.0,
			vendor_rules: { [vendorId]: { tolerance_pct: 'not-a-number' } }
		});

		const { poId, poNumber } = createPo({ total: 1000, vendorId });
		created.poIds.push(poId);
		const { invoiceId, poMatch } = await createMatchedInvoice(page, {
			poNumber,
			amount: 1040,
			vendorId
		});
		created.invoiceIds.push(invoiceId);

		// Resolver didn't blow up; org tolerance applied.
		expect(poMatch!.details.tolerance_pct).toBeCloseTo(5.0, 4);
		expect(poMatch!.status).toBe('matched');
	});
});

test.describe('matching tenant isolation', () => {
	const created: { invoiceIds: string[]; poIds: string[] } = { invoiceIds: [], poIds: [] };
	test.afterAll(() => cleanup(created));

	test('a PO seeded in our tenant is invisible to a different tenant', async ({
		page,
		tenantSlug
	}) => {
		// Seed a uniquely-numbered PO in OUR tenant. (We only ever write to our
		// own worker's DB — never another worker's — so concurrent workers can't
		// collide.) The matcher only ever queries `get_tenant_db`, so this is
		// the chokepoint that keeps one tenant's POs out of another's matches.
		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);

		// Our own tenant lists it.
		const ownHeaders = tenantHeaders(await authToken(page));
		const own = await page.request.get(
			`${API_BASE}/api/purchase-orders?search=${poNumber}&page_size=100`,
			{ headers: ownHeaders }
		);
		expect(own.ok()).toBe(true);
		const ownNumbers = ((await own.json()) as { items: Array<{ po_number: string }> }).items.map(
			(p) => p.po_number
		);
		expect(ownNumbers).toContain(poNumber);

		// A DIFFERENT tenant, using its own JWT + slug, must not see it. We sign
		// into the other tenant in a throwaway context (read-only — no writes to
		// its DB) and confirm the PO number is absent.
		// Use the always-seeded `acme` tenant as the "other" side. The dynamic
		// `e2e<N>` neighbours are only present when FEOH_E2E_TENANT_COUNT > 1,
		// which is NOT the case on CI shards (TENANT_COUNT=1 → only e2e1).
		// `acme` + `techflow` are always seeded regardless of TENANT_COUNT.
		const otherSlug = 'acme';
		const ctx = await page.context().browser()!.newContext({
			baseURL: ACME_BASE
		});
		try {
			const otherPage = await ctx.newPage();
			await signInAndWait(otherPage, ACME_ADMIN);
			const otherHeaders = tenantHeaders(await authToken(otherPage), otherSlug);
			const other = await otherPage.request.get(
				`${API_BASE}/api/purchase-orders?search=${poNumber}&page_size=100`,
				{ headers: otherHeaders }
			);
			expect(other.ok()).toBe(true);
			const otherNumbers = (
				(await other.json()) as { items: Array<{ po_number: string }> }
			).items.map((p) => p.po_number);
			expect(otherNumbers).not.toContain(poNumber);
		} finally {
			await ctx.close();
		}
	});

	test('the matcher matches a local PO (control) and recompute is stable', async ({ page }) => {
		tenantScope(); // assert our scope resolves
		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.status).toBe('matched');
		expect(poMatch!.po_id).toBe(poId);

		const again = await recompute(page, invoiceId);
		expect(again!.status).toBe('matched');
	});
});
