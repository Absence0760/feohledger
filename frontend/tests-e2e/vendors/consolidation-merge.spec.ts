import {
	API_BASE,
	authedTenantHeaders,
	deleteVendorsWhere,
	expect,
	signInAndWait,
	test
} from '../fixtures/helpers';

/** Every vendor this file creates is named `<NAME_PREFIX><timestamp> …`, which
 *  is what `afterEach` cleans up. */
const NAME_PREFIX = 'Qznx';

/**
 * /vendors — "Merge into canonical" consolidation UI.
 *
 * Drives the full flow against the real backend:
 *   1. Seed two near-duplicate ACTIVE vendors sharing a tax id (the
 *      consolidation clusterer groups on exact tax id, so the pair is a
 *      guaranteed deterministic cluster — independent of seed data).
 *   2. Open the "Merge duplicates" modal from the vendors header action.
 *   3. Confirm the cluster renders a canonical-vs-duplicate diff.
 *   4. Merge → the duplicate is soft-retired (status=inactive) and the
 *      vendor list refreshes.
 *
 * The merge endpoint is gated on the granular `vendor.manage` permission. The
 * admin holds it (header action + per-cluster Merge present); a clerk does not
 * (the action is absent).
 */

// A unique tax id per run so the cluster is exactly our two vendors (no
// collision with other workers / prior runs in the shared tenant).
function uniqueTaxId(): string {
	// 9 digits, formatted like an EIN. `Date.now()` keeps it unique per run.
	const n = (Date.now() % 1_000_000_000).toString().padStart(9, '0');
	return `${n.slice(0, 2)}-${n.slice(2)}`;
}

async function createVendor(
	page: import('@playwright/test').Page,
	body: Record<string, unknown>
): Promise<{ id: string; name: string }> {
	const headers = await authedTenantHeaders(page);
	const res = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: { ...headers, 'Content-Type': 'application/json' },
		data: body
	});
	expect(res.ok(), `create vendor ${body.name}: ${res.status()}`).toBeTruthy();
	const v = await res.json();
	return { id: v.id, name: v.name };
}

test.describe('/vendors consolidation merge (admin)', () => {
	// Pre-record the cookie-consent choice so the GDPR banner (which overlays the
	// bottom of the viewport) never intercepts the modal's Close button. The
	// banner is orthogonal to this feature.
	test.beforeEach(async ({ page }) => {
		await page.addInitScript(() => {
			try {
				localStorage.setItem('feoh_consent_choice', 'accepted');
			} catch {
				/* about:blank — ignore */
			}
		});
	});

	/**
	 * The merge soft-RETIRES the duplicate (`status=inactive`) rather than
	 * deleting it, and the canonical survives by design — so a passing run left
	 * two vendors behind, every run, in a tenant shared with every other spec.
	 * The name carries a timestamp so nothing ever collided and nothing ever
	 * stopped accumulating: `/vendors` counts them in its status chips, pages
	 * one row further, and the consolidation clusterer itself has more rows to
	 * consider on the next run.
	 *
	 * The predicate is the shared prefix rather than this run's token, so a run
	 * also clears what earlier runs stranded. `deleteVendorsWhere` owns the
	 * child graph — screening runs on vendor create, and that FK does not
	 * cascade.
	 */
	test.afterEach(() => {
		deleteVendorsWhere(`name LIKE '${NAME_PREFIX}%'`);
	});

	test('seed a duplicate pair, merge into canonical, list refreshes', async ({ page }) => {
		// A run-unique name token so this run's cluster is exactly our two
		// vendors — robust against vendors any prior run left behind (the tenant
		// is shared across runs; the unique token keeps clusters from bleeding
		// together by name similarity).
		const token = `${NAME_PREFIX}${Date.now().toString(36)}`;
		const taxId = uniqueTaxId();
		const canonName = `${token} Holdings Co`;
		const dupeName = `${token} Holdings Company`;

		// Two active vendors sharing a tax id → a deterministic cluster of just
		// these two (the names also pass the fuzzy threshold).
		const a = await createVendor(page, {
			name: canonName,
			code: `${token}-C`,
			tax_id: taxId
		});
		const b = await createVendor(page, {
			name: dupeName,
			tax_id: taxId
		});

		await page.goto('/vendors');

		// Open the consolidation modal from the header action.
		await page.getByRole('button', { name: 'Merge duplicates' }).click();
		const modal = page.getByRole('dialog', { name: 'Vendor consolidation' });
		await expect(modal).toBeVisible();

		// Our pair's cluster renders both names + a Canonical / Duplicate split.
		const cluster = modal.locator('li.cluster', { hasText: token });
		await expect(cluster).toBeVisible();
		await expect(cluster.getByText(canonName, { exact: true })).toBeVisible();
		await expect(cluster.getByText(dupeName, { exact: true })).toBeVisible();
		await expect(cluster.getByText('Canonical', { exact: true })).toBeVisible();
		await expect(cluster.getByText('Duplicate', { exact: true })).toBeVisible();
		// The backend's English evidence ("same tax id") is keyed, not echoed (#635).
		await expect(cluster.locator('.reason-pill', { hasText: 'Same tax ID' })).toBeVisible();

		// At 320px the six-column member table pans inside its own named region
		// instead of pushing the dialog sideways (WCAG 1.4.10).
		const desktop = page.viewportSize()!;
		await page.setViewportSize({ width: 320, height: 568 });
		await expect(cluster.getByRole('region', { name: /^Duplicate cluster for / })).toBeVisible();
		expect(await modal.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
		await page.setViewportSize(desktop);

		// Read which member the backend picked as canonical (most invoices, tie →
		// oldest) so the assertion doesn't assume which of our two it is.
		const canonRow = cluster.locator('tr.canonical-row');
		const canonRowName = (await canonRow.locator('td.m-name').textContent())?.trim();
		const ids: Record<string, string> = { [canonName]: a.id, [dupeName]: b.id };
		const canonId = ids[canonRowName ?? ''];
		const dupeId = canonId === a.id ? b.id : a.id;

		// Arm + confirm the merge (two-step, irreversible-ish). The Merge button
		// carries a per-cluster aria-label (`Merge cluster into <canonical>`).
		await cluster.getByRole('button', { name: `Merge cluster into ${canonRowName}` }).click();
		const confirmBtn = cluster.getByRole('button', { name: 'Confirm merge' });
		await expect(confirmBtn).toBeVisible();
		await confirmBtn.click();

		// One duplicate, so the singular — this was a hand-built `s` suffix.
		await expect(
			page
				.getByRole('region', { name: 'Status messages' })
				.getByText(/^Merged 1 duplicate \(\d+ records? re-homed\)$/)
		).toBeVisible();

		// The merged cluster drops out of the modal list.
		await expect(modal.locator('li.cluster', { hasText: token })).toHaveCount(0);

		await modal.getByRole('button', { name: 'Close' }).click();
		await expect(modal).toBeHidden();

		// Backend truth: the duplicate is soft-retired, the canonical stays active.
		const headers = await authedTenantHeaders(page);
		const check = await page.request.get(`${API_BASE}/api/vendors/${dupeId}`, { headers });
		expect(check.ok()).toBeTruthy();
		expect((await check.json()).status).toBe('inactive');

		const checkCanon = await page.request.get(`${API_BASE}/api/vendors/${canonId}`, { headers });
		expect((await checkCanon.json()).status).toBe('active');
	});
});

test.describe('/vendors consolidation suggestions fail to load', () => {
	test('the dialog states the failure in place, announced', async ({ page }) => {
		await page.addInitScript(() => {
			try {
				localStorage.setItem('feoh_consent_choice', 'accepted');
			} catch {
				/* about:blank — ignore */
			}
		});
		await page.route(
			(url) => url.pathname === '/api/enrichment/vendors/consolidation-suggestions',
			(route) => route.fulfill({ status: 500, json: { detail: 'boom' } })
		);
		await page.goto('/vendors');
		await page.getByRole('button', { name: 'Merge duplicates' }).click();
		const modal = page.getByRole('dialog', { name: 'Vendor consolidation' });
		// The only signal now (its old toast duplicated it, and an error toast
		// no longer times out): an alert inside the dialog.
		await expect(modal.getByRole('alert')).toHaveText(
			"Couldn't load suggestions. Close and try again."
		);
		await expect(page.getByTestId('toast')).toHaveCount(0);
	});
});

test.describe('/vendors consolidation merge refused, in German', () => {
	test('the coded refusal is stated in the reader’s language, not the server’s English', async ({
		page
	}) => {
		await page.addInitScript(() => {
			try {
				localStorage.setItem('feoh_consent_choice', 'accepted');
				localStorage.setItem('feoh_locale', 'de');
			} catch {
				/* about:blank — ignore */
			}
		});
		const canonId = '00000000-0000-0000-0000-00000000c001';
		const dupeId = '00000000-0000-0000-0000-00000000d001';
		const member = (vendor_id: string, name: string, is_canonical: boolean) => ({
			vendor_id,
			name,
			code: null,
			tax_id_masked: '***6789',
			status: 'active',
			invoice_count: 0,
			is_canonical
		});
		await page.route(
			(url) => url.pathname === '/api/enrichment/vendors/consolidation-suggestions',
			(route) =>
				route.fulfill({
					json: {
						clusters: [
							{
								cluster_id: 1,
								members: [
									member(canonId, 'Stub Holdings Co', true),
									member(dupeId, 'Stub Holdings Company', false)
								],
								canonical_vendor_id: canonId,
								score: '1',
								reasons: ['same_tax_id']
							}
						],
						vendor_count: 2,
						cluster_count: 1,
						truncated: false,
						generated_at: '2026-10-21T00:00:00Z'
					}
				})
		);
		// Exactly what `services/vendor_merge.py` answers for a payments-blocked
		// duplicate folded into an unblocked canonical.
		const english =
			'Cannot merge a payments-blocked vendor into an unblocked canonical — resolve the ' +
			"block first (see the vendor's screening review) or exclude it from this merge.";
		await page.route(
			(url) => url.pathname === '/api/enrichment/vendors/consolidation/merge',
			(route) =>
				route.fulfill({
					status: 422,
					json: {
						detail: { code: 'vendor_merge_blocked_duplicate', message: english, params: {} }
					}
				})
		);

		await page.goto('/vendors');
		await page.getByRole('button', { name: 'Duplikate zusammenführen' }).click();
		const modal = page.getByRole('dialog', { name: 'Lieferantenkonsolidierung' });
		const cluster = modal.locator('li.cluster', { hasText: 'Stub Holdings Co' });
		await cluster
			.getByRole('button', { name: 'Gruppe in Stub Holdings Co zusammenführen' })
			.click();
		await cluster.getByRole('button', { name: 'Zusammenführen bestätigen' }).click();

		const toasts = page.locator('.toast-text');
		await expect(
			toasts.filter({
				hasText:
					'Für ein Duplikat sind Zahlungen gesperrt, für den maßgeblichen Lieferanten nicht.'
			})
		).toBeVisible();
		await expect(toasts.filter({ hasText: 'payments-blocked' })).toHaveCount(0);
		// Refused, so the cluster stays to be acted on.
		await expect(cluster).toBeVisible();
	});
});

test.describe('/vendors consolidation (clerk has no access)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk does not see the Merge duplicates action', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		// The action gates on `auth.can('vendor.manage')`, which is only
		// populated once GET /api/auth/me lands — asserting the absence before
		// that would pass for the wrong reason (nobody holds any permission
		// yet). Wait on the identity fetch itself, not on a quiet network.
		const me = page.waitForResponse(
			(r) => r.url().includes('/api/auth/me') && r.request().method() === 'GET'
		);
		await page.goto('/vendors');
		await me;

		await expect(page.getByRole('button', { name: 'Merge duplicates' })).toHaveCount(0);
	});
});
