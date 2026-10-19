import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	selectVendorInPicker,
	signInAndWait,
	tenantPsql,
	test, vendorPicker
} from '../fixtures/helpers';

/**
 * /recurring — Recurring / Subscription Invoices.
 *
 * Exercises the template lifecycle end-to-end against the same contract the
 * backend `/api/recurring` router + the `/recurring` frontend route are built
 * to: create a template via the modal, see it in the list, open it, view the
 * upcoming-schedule preview, trigger "generate now", and confirm the generated
 * invoice shows up in the template's history.
 *
 * Login model mirrors the rest of the suite: the default per-worker storage
 * state signs the worker's admin in (an admin is in the mutate set
 * admin / ap_manager), so the page loads directly without a redirect. The
 * "not authorized" describe block opts out and signs in as the clerk.
 *
 * Selectors are by accessible name / aria-label / data-testid — never brittle
 * CSS/nth-child, never `waitForTimeout`. Each test creates a fresh template
 * and hard-deletes it (+ any generated invoice) via psql in `finally`.
 */

interface Vendor {
	id: string;
	name: string;
}

async function getFirstVendor(page: import('@playwright/test').Page): Promise<Vendor> {
	const resp = await page.request.get(`${API_BASE}/api/vendors`, {
		headers: await authedTenantHeaders(page)
	});
	const body = (await resp.json()) as { items: Vendor[] };
	return body.items[0];
}

async function createTemplate(
	page: import('@playwright/test').Page,
	data: Record<string, unknown>
): Promise<{ id: string; status: string; name: string }> {
	const resp = await page.request.post(`${API_BASE}/api/recurring`, {
		headers: await authedTenantHeaders(page),
		data
	});
	expect(resp.status()).toBe(201);
	return (await resp.json()) as { id: string; status: string; name: string };
}

/** Hard-delete a template + any invoices it generated (revertible cleanup). */
function deleteTemplate(id: string): void {
	// A generated invoice carries a WorkflowInstance (+ its WorkflowSteps), so
	// the invoice rows can't be dropped until those children are gone. Unwind
	// the FK chain bottom-up — steps → instances → invoices → template — then
	// remove the template itself.
	const invSubquery = `SELECT id FROM invoices WHERE recurring_template_id='${id}'`;
	tenantPsql(
		`DELETE FROM workflow_steps WHERE instance_id IN ` +
			`(SELECT id FROM workflow_instances WHERE invoice_id IN (${invSubquery}))`
	);
	tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id IN (${invSubquery})`);
	deleteInvoicesWhere(`recurring_template_id='${id}'`);
	tenantPsql(`DELETE FROM recurring_invoice_templates WHERE id='${id}'`);
}

test.describe('/recurring (admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/recurring');
	});

	test('renders the recurring surface — header, KPIs, filters, table', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Recurring' })).toBeVisible();

		// KPI row.
		await expect(page.locator('.kpi').first()).toBeVisible({ timeout: 10_000 });

		// Status filter chips.
		await expect(page.locator('.filter-chip', { hasText: 'All' })).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: 'Active' })).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: 'Paused' })).toBeVisible();

		// The templates data table renders (seeded rows or the centred empty state).
		await expect(page.locator('.grid-container table')).toBeVisible();
	});

	test('switching the status filter re-requests the list', async ({ page }) => {
		const respPromise = page.waitForResponse(
			(r) => r.url().includes('/api/recurring') && r.url().includes('status=paused')
		);
		await page.locator('.filter-chip', { hasText: 'Paused' }).click();
		const resp = await respPromise;
		expect(resp.request().url()).toContain('status=paused');
	});

	test('a created template appears in the list', async ({ page }) => {
		const vendor = await getFirstVendor(page);
		const name = `E2E recurring ${Date.now()}`;
		let id: string | null = null;
		try {
			const created = await createTemplate(page, {
				name,
				vendor_id: vendor.id,
				amount: '2400.00',
				currency: 'USD',
				cadence: 'monthly',
				day_of_period: 1,
				start_date: '2026-01-01'
			});
			id = created.id;
			expect(created.status).toBe('active');

			await page.goto(`/recurring?search=${encodeURIComponent(name)}`);
			await expect(page.getByText(name)).toBeVisible();
		} finally {
			if (id) deleteTemplate(id);
		}
	});

	test('create via the modal: open, fill, save, see it in the table', async ({ page }) => {
		const vendor = await getFirstVendor(page);
		const name = `E2E modal ${Date.now()}`;
		let id: string | null = null;
		try {
			// Open the create modal via the page's primary action.
			await page.getByRole('button', { name: '+ New template' }).click();
			const dialog = page.getByRole('dialog', { name: 'New recurring template' });
			await expect(dialog).toBeVisible();

			await dialog.getByLabel('Name').fill(name);
			// Vendor select carries the vendor name as its option label.
			await selectVendorInPicker(vendorPicker(dialog), vendor.name);
			await dialog.getByLabel('Amount').fill('1500.00');
			await dialog.getByLabel('Cadence').selectOption('monthly');
			await dialog.getByLabel('Start Date').fill('2026-01-01');

			// The create POST fires on submit; capture the new id for cleanup.
			const respPromise = page.waitForResponse(
				(r) => r.url().endsWith('/api/recurring') && r.request().method() === 'POST'
			);
			await dialog.getByRole('button', { name: 'Create' }).click();
			const resp = await respPromise;
			expect(resp.status()).toBe(201);
			id = ((await resp.json()) as { id: string }).id;

			await expect(page.getByText(name)).toBeVisible({ timeout: 10_000 });
		} finally {
			if (id) deleteTemplate(id);
		}
	});

	test('open a template, preview the upcoming schedule, generate now, see history', async ({
		page
	}) => {
		const vendor = await getFirstVendor(page);
		const name = `E2E generate ${Date.now()}`;
		let id: string | null = null;
		try {
			// Active template whose next_run_on has already arrived, so the
			// schedule projects occurrences and generate-now produces an invoice.
			const created = await createTemplate(page, {
				name,
				vendor_id: vendor.id,
				amount: '3300.00',
				currency: 'USD',
				cadence: 'monthly',
				day_of_period: 1,
				start_date: '2026-01-01'
			});
			id = created.id;

			// Upcoming-schedule projection is a read endpoint — confirm it
			// returns at least one occurrence for an active template.
			const schedule = await page.request.get(
				`${API_BASE}/api/recurring/${id}/upcoming-schedule?count=3`,
				{ headers: await authedTenantHeaders(page) }
			);
			expect(schedule.status()).toBe(200);
			const sched = (await schedule.json()) as { occurrences: { period_key: string }[] };
			expect(sched.occurrences.length).toBeGreaterThan(0);

			// Open the detail modal from the list (clickable row / RowLink).
			await page.goto(`/recurring?search=${encodeURIComponent(name)}`);
			await page.getByRole('button', { name: `Open template ${name}` }).click();
			const dialog = page.getByRole('dialog', { name: 'Recurring template detail' });
			await expect(dialog).toBeVisible();

			// The upcoming-schedule preview surfaces at least one projected period.
			await expect(dialog.getByText(sched.occurrences[0].period_key)).toBeVisible({
				timeout: 10_000
			});

			// Generate now → exactly one invoice for this period (the modal's
			// lifecycle action; aria-label is template-specific).
			const genPromise = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/recurring/${id}/generate-now`) &&
					r.request().method() === 'POST'
			);
			await dialog.getByRole('button', { name: `Generate now for ${name}` }).click();
			const genResp = await genPromise;
			expect(genResp.ok()).toBeTruthy();

			// The generated invoice shows up in the template's history.
			const history = await page.request.get(`${API_BASE}/api/recurring/${id}/history`, {
				headers: await authedTenantHeaders(page)
			});
			expect(history.status()).toBe(200);
			const hist = (await history.json()) as { items: { invoice_id: string }[] };
			expect(hist.items.length).toBeGreaterThan(0);
		} finally {
			if (id) deleteTemplate(id);
		}
	});

	test('delete is refused (409) once an invoice has been generated', async ({ page }) => {
		const vendor = await getFirstVendor(page);
		const name = `E2E delete-guard ${Date.now()}`;
		let id: string | null = null;
		try {
			const created = await createTemplate(page, {
				name,
				vendor_id: vendor.id,
				amount: '1000.00',
				currency: 'USD',
				cadence: 'monthly',
				day_of_period: 1,
				start_date: '2026-01-01'
			});
			id = created.id;

			// Generate once so generated_count > 0.
			const gen = await page.request.post(`${API_BASE}/api/recurring/${id}/generate-now`, {
				headers: await authedTenantHeaders(page)
			});
			expect(gen.ok()).toBeTruthy();

			// Now DELETE must 409 — pause/end instead of delete.
			const del = await page.request.delete(`${API_BASE}/api/recurring/${id}`, {
				headers: await authedTenantHeaders(page)
			});
			expect(del.status()).toBe(409);
		} finally {
			if (id) deleteTemplate(id);
		}
	});
});

test.describe('/recurring (clerk — read-only)', () => {
	// Opt out of the default admin storage state so we can sign in as the clerk.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk can read the list but cannot mutate', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/recurring');

		// Read is allowed for all four roles, so the page renders.
		await expect(page.getByRole('heading', { name: 'Recurring' })).toBeVisible();

		// But a mutate call is rejected by the backend (admin / ap_manager only).
		// The `require_roles` dependency runs before the handler body, so the POST
		// 403s regardless of payload — no vendor lookup needed (and `GET /api/vendors`
		// is itself admin/ap_manager/cfo-only, so a clerk can't call it anyway).
		const resp = await page.request.post(`${API_BASE}/api/recurring`, {
			headers: await authedTenantHeaders(page),
			data: {
				name: `E2E clerk ${Date.now()}`,
				amount: '500.00',
				currency: 'USD',
				cadence: 'monthly',
				day_of_period: 1,
				start_date: '2026-01-01'
			}
		});
		expect(resp.status()).toBe(403);
	});
});
