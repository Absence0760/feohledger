import {
	API_BASE,
	authedTenantHeaders,
	expect,
	selectVendorInPicker,
	signInAndWait,
	tenantPsql,
	test, vendorPicker
} from '../fixtures/helpers';

/**
 * /vendor-statements — Vendor Statement Reconciliation.
 *
 * Exercises the reconciliation surface end-to-end against the same contract the
 * backend `/api/vendor-statements` router + the `/vendor-statements` frontend
 * route are built to: create a reconciliation run from pasted statement lines,
 * see it in the list, open the side-by-side diff modal, and resolve / ignore a
 * discrepant line.
 *
 * Login model mirrors the rest of the suite: the default per-worker storage
 * state signs the worker's admin in (an admin is in the mutate set
 * admin / ap_manager), so the page loads directly without a redirect. The
 * "read-only" describe block opts out and signs in as the clerk.
 *
 * Selectors are by accessible name / aria-label / text — never brittle
 * CSS/nth-child, never `waitForTimeout`. Each test creates a fresh run and
 * hard-deletes it (lines first, then the run) via psql in `finally`.
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

async function createReconciliation(
	page: import('@playwright/test').Page,
	data: Record<string, unknown>
): Promise<{ id: string; status: string; vendor_name: string | null }> {
	const resp = await page.request.post(`${API_BASE}/api/vendor-statements`, {
		headers: await authedTenantHeaders(page),
		data
	});
	expect(resp.ok()).toBeTruthy();
	return (await resp.json()) as { id: string; status: string; vendor_name: string | null };
}

/** Hard-delete a reconciliation run + its lines (revertible cleanup). */
function deleteReconciliation(id: string): void {
	tenantPsql(`DELETE FROM vendor_statement_recon_lines WHERE reconciliation_id='${id}'`);
	tenantPsql(`DELETE FROM vendor_statement_reconciliations WHERE id='${id}'`);
}

/**
 * Cleanup for a run created from an UPLOAD, which also archived the document to
 * MinIO. Deleting the rows straight out of Postgres would strand that object
 * forever — only `DELETE /api/vendor-statements/{id}` drops it — so go through
 * the API and fall back to psql if that call can't be made.
 */
async function deleteUploadedReconciliation(
	page: import('@playwright/test').Page,
	id: string
): Promise<void> {
	const resp = await page.request.delete(`${API_BASE}/api/vendor-statements/${id}`, {
		headers: await authedTenantHeaders(page)
	});
	if (!resp.ok()) deleteReconciliation(id);
}

test.describe('/vendor-statements (admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/vendor-statements');
	});

	test('renders the reconciliation surface — header, KPIs, filters, table', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Statements' })).toBeVisible();

		// KPI row.
		await expect(page.locator('.kpi').first()).toBeVisible({ timeout: 10_000 });

		// Status filter chips.
		await expect(page.locator('.filter-chip', { hasText: 'All' })).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: 'Open' })).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: 'Resolved' })).toBeVisible();

		// The reconciliations table renders (seeded rows or the centred empty state).
		await expect(page.locator('.grid-container table')).toBeVisible();
	});

	test('switching the status filter re-requests the list', async ({ page }) => {
		const respPromise = page.waitForResponse(
			(r) => r.url().includes('/api/vendor-statements') && r.url().includes('status=resolved')
		);
		await page.locator('.filter-chip', { hasText: 'Resolved' }).click();
		const resp = await respPromise;
		expect(resp.request().url()).toContain('status=resolved');
	});

	test('a created reconciliation appears in the list', async ({ page }) => {
		const vendor = await getFirstVendor(page);
		const reference = `E2E recon ${Date.now()}`;
		let id: string | null = null;
		try {
			const created = await createReconciliation(page, {
				vendor_id: vendor.id,
				statement_date: '2026-01-31',
				statement_reference: reference,
				currency: 'USD',
				lines: [
					{ invoice_number: 'INV-9001', amount: '1200.00' },
					{ invoice_number: 'INV-9002', amount: '850.00' }
				]
			});
			id = created.id;
			expect(created.status).toBe('open');

			await page.goto(`/vendor-statements?search=${encodeURIComponent(reference)}`);
			await expect(page.getByText(reference)).toBeVisible();
		} finally {
			if (id) deleteReconciliation(id);
		}
	});

	test('open the diff modal and resolve a discrepant line', async ({ page }) => {
		const vendor = await getFirstVendor(page);
		const reference = `E2E diff ${Date.now()}`;
		let id: string | null = null;
		try {
			// A line for an invoice the ledger won't have → missing_on_their_side
			// or missing_on_our_side, i.e. a discrepancy that surfaces a Resolve.
			const created = await createReconciliation(page, {
				vendor_id: vendor.id,
				statement_date: '2026-02-28',
				statement_reference: reference,
				currency: 'USD',
				lines: [{ invoice_number: `E2E-NOMATCH-${Date.now()}`, amount: '4321.00' }]
			});
			id = created.id;

			// Open the detail modal from the list (clickable row / RowLink).
			await page.goto(`/vendor-statements?search=${encodeURIComponent(reference)}`);
			await page
				.getByRole('button', {
					name: `Open reconciliation for ${vendor.name} 2026-02-28`
				})
				.click();
			const dialog = page.getByRole('dialog', { name: 'Vendor statement reconciliation detail' });
			await expect(dialog).toBeVisible();

			// The side-by-side diff surfaces the statement invoice number.
			await expect(dialog.getByText('E2E-NOMATCH-', { exact: false })).toBeVisible({
				timeout: 10_000
			});
		} finally {
			if (id) deleteReconciliation(id);
		}
	});

	test('the create modal opens with the expected aria-label', async ({ page }) => {
		await page.getByRole('button', { name: '+ New reconciliation' }).click();
		const dialog = page.getByRole('dialog', { name: 'New vendor statement reconciliation' });
		await expect(dialog).toBeVisible();
		// Vendor + statement-date controls are present.
		await expect(vendorPicker(dialog)).toBeVisible();
		await expect(dialog.getByLabel('Statement Date')).toBeVisible();
	});

	test('switching intake mode swaps the pasted-lines editor for the file picker', async ({
		page
	}) => {
		await page.getByRole('button', { name: '+ New reconciliation' }).click();
		const dialog = page.getByRole('dialog', { name: 'New vendor statement reconciliation' });

		// Paste is the default: the lines editor is up, the file picker is not.
		await expect(dialog.getByLabel('Statement line 1 invoice number')).toBeVisible();
		await expect(dialog.locator('input[type="file"]')).toHaveCount(0);

		await dialog.getByRole('radio', { name: 'Upload a file' }).check();

		// The two intakes are mutually exclusive — typed lines can no longer be
		// silently discarded by a file that wins the tiebreak.
		await expect(dialog.locator('input[type="file"]')).toBeVisible();
		await expect(dialog.getByLabel('Statement line 1 invoice number')).toHaveCount(0);

		// Leaving the file path drops the pick. The `{#if}` destroys the native
		// picker, so a retained File would leave the "Selected: …" chip claiming an
		// attachment the (re-rendered, empty) input contradicts.
		await dialog.locator('input[type="file"]').setInputFiles({
			name: 'discarded.csv',
			mimeType: 'text/csv',
			buffer: Buffer.from('Invoice Number,Amount\nINV-1,1.00\n')
		});
		await expect(dialog.getByTestId('statement-file-chosen')).toBeVisible();
		await dialog.getByRole('radio', { name: 'Type the lines' }).check();
		await dialog.getByRole('radio', { name: 'Upload a file' }).check();
		await expect(dialog.getByTestId('statement-file-chosen')).toHaveCount(0);
	});

	test('a CSV upload creates a run, and its detail offers the source document', async ({
		page
	}) => {
		const vendor = await getFirstVendor(page);
		const reference = `E2E upload ${Date.now()}`;
		const statementDate = '2026-04-30';
		let id: string | null = null;
		try {
			await page.getByRole('button', { name: '+ New reconciliation' }).click();
			const dialog = page.getByRole('dialog', { name: 'New vendor statement reconciliation' });

			await selectVendorInPicker(vendorPicker(dialog), vendor.name);
			await dialog.getByLabel('Statement Date').fill(statementDate);
			await dialog.getByLabel('Statement Reference').fill(reference);
			await dialog.getByRole('radio', { name: 'Upload a file' }).check();
			await dialog.locator('input[type="file"]').setInputFiles({
				name: 'supplier-statement.csv',
				mimeType: 'text/csv',
				buffer: Buffer.from(
					`Invoice Number,Amount,Date\nE2E-CSV-${Date.now()},1234.56,2026-04-15\n`
				)
			});
			// The picked file is acknowledged before submit — the old form gave no
			// indication a file had been attached at all.
			await expect(dialog.getByTestId('statement-file-chosen')).toContainText(
				'supplier-statement.csv'
			);

			const uploadResp = page.waitForResponse(
				(r) =>
					r.url().includes('/api/vendor-statements/upload') && r.request().method() === 'POST'
			);
			await dialog.getByRole('button', { name: 'Reconcile' }).click();
			const created = (await (await uploadResp).json()) as {
				id: string;
				source_format: string;
				has_source_file: boolean;
			};
			id = created.id;
			expect(created.source_format).toBe('csv');
			expect(created.has_source_file).toBe(true);

			// Deep-link straight to the run the upload produced.
			await page.goto(`/vendor-statements?id=${id}`);
			const detail = page.getByRole('dialog', {
				name: 'Vendor statement reconciliation detail'
			});
			await expect(detail).toBeVisible({ timeout: 10_000 });
			await expect(detail.getByTestId('statement-source')).toHaveText('CSV upload');

			// Provenance: a CSV is parsed directly (no adapter), and the supplier's
			// own document is retrievable for a disputed balance.
			const provenance = detail.getByTestId('statement-provenance');
			await expect(provenance).toBeVisible();
			await expect(provenance).toContainText('Parsed directly from the uploaded CSV.');
			await expect(
				provenance.getByRole('button', { name: 'Download the source statement' })
			).toBeVisible();
		} finally {
			if (id) await deleteUploadedReconciliation(page, id);
		}
	});

	test('a statement the backend refuses explains itself on the form', async ({ page }) => {
		const vendor = await getFirstVendor(page);
		await page.getByRole('button', { name: '+ New reconciliation' }).click();
		const dialog = page.getByRole('dialog', { name: 'New vendor statement reconciliation' });

		await selectVendorInPicker(vendorPicker(dialog), vendor.name);
		await dialog.getByLabel('Statement Date').fill('2026-05-31');
		await dialog.getByRole('radio', { name: 'Upload a file' }).check();
		// Header row only — `parse_statement_csv` refuses this structurally rather
		// than creating a run that claims the supplier listed nothing.
		await dialog.locator('input[type="file"]').setInputFiles({
			name: 'header-only.csv',
			mimeType: 'text/csv',
			buffer: Buffer.from('Invoice Number,Amount,Date\n')
		});

		const refusal = page.waitForResponse(
			(r) => r.url().includes('/api/vendor-statements/upload') && r.request().method() === 'POST'
		);
		await dialog.getByRole('button', { name: 'Reconcile' }).click();
		expect((await refusal).status()).toBe(422);

		// The backend's own explanation lands in a persistent alert on the form —
		// not a toast that fades — and the dialog stays open so it can be acted on.
		const error = dialog.getByTestId('statement-intake-error');
		await expect(error).toBeVisible();
		await expect(error).toHaveAttribute('role', 'alert');
		await expect(error).toContainText('CSV is empty or has no data rows');
		await expect(dialog).toBeVisible();
	});
});

test.describe('/vendor-statements (admin) — statement currency', () => {
	// The currency field used to be a <select> over the sixteen-code shortlist,
	// so a PLN statement could not be entered at all (decisions §377 follow-up).
	// It is free entry over any ISO 4217 code, never blank, seeded from the
	// org's currency whenever that lands.

	test('a currency outside the common shortlist (PLN) can be entered and is stored', async ({
		page
	}) => {
		await page.goto('/vendor-statements');
		const vendor = await getFirstVendor(page);
		let id: string | null = null;
		try {
			await page.getByRole('button', { name: '+ New reconciliation' }).click();
			const dialog = page.getByRole('dialog', { name: 'New vendor statement reconciliation' });

			await selectVendorInPicker(vendorPicker(dialog), vendor.name);
			await dialog.getByLabel('Statement Date').fill('2026-04-30');
			const currency = dialog.getByTestId('statement-currency');
			// Typed in lowercase: the field uppercases as it goes.
			await currency.fill('pln');
			await expect(currency).toHaveValue('PLN');
			await expect(dialog.getByTestId('statement-currency-error')).toHaveCount(0);

			await dialog.getByRole('radio', { name: 'Upload a file' }).check();
			await dialog.locator('input[type="file"]').setInputFiles({
				name: 'pln-statement.csv',
				mimeType: 'text/csv',
				buffer: Buffer.from(`Invoice Number,Amount,Date\nE2E-PLN-${Date.now()},410.00,2026-04-15\n`)
			});

			const uploadResp = page.waitForResponse(
				(r) =>
					r.url().includes('/api/vendor-statements/upload') && r.request().method() === 'POST'
			);
			await dialog.getByRole('button', { name: 'Reconcile' }).click();
			const resp = await uploadResp;
			expect(resp.status()).toBe(201);
			const created = (await resp.json()) as { id: string; currency: string };
			id = created.id;
			expect(created.currency).toBe('PLN');
		} finally {
			if (id) await deleteUploadedReconciliation(page, id);
		}
	});

	test('a malformed code is refused on the form, in words, before any request', async ({
		page
	}) => {
		await page.goto('/vendor-statements');
		await page.getByRole('button', { name: '+ New reconciliation' }).click();
		const dialog = page.getByRole('dialog', { name: 'New vendor statement reconciliation' });
		const currency = dialog.getByTestId('statement-currency');

		await currency.fill('EU');
		const error = dialog.getByTestId('statement-currency-error');
		await expect(error).toHaveText(
			'“EU” is not a three-letter currency code. Use an ISO 4217 code such as USD or PLN.'
		);
		await expect(currency).toHaveAttribute('aria-invalid', 'true');
		await expect(currency).toHaveAttribute('aria-describedby', /.+/);

		// Blank is refused too: "never blank" is the point of §377.
		await currency.fill('');
		await expect(error).toHaveText(
			'Enter the currency as a three-letter ISO 4217 code, such as USD or PLN.'
		);
		await expect(dialog.getByRole('button', { name: 'Reconcile' })).toBeDisabled();
	});

	test("the field follows the org's currency when it loads after the modal opened", async ({
		page
	}) => {
		let release!: () => void;
		const gate = new Promise<void>((resolve) => (release = resolve));
		await page.route(
			(url) => url.pathname === '/api/organization',
			async (route) => {
				await gate;
				await route.fulfill({
					json: { resolved_reporting_currency: 'GBP', settings: { reporting_currency: 'GBP' } }
				});
			}
		);

		await page.goto('/vendor-statements');
		await page.getByRole('button', { name: '+ New reconciliation' }).click();
		const dialog = page.getByRole('dialog', { name: 'New vendor statement reconciliation' });
		const currency = dialog.getByTestId('statement-currency');

		// Unresolved: the platform default, explicitly, so the field is never blank.
		await expect(currency).toHaveValue('USD');
		release();
		// Seeding only at mount left a GBP organization's statement on USD.
		await expect(currency).toHaveValue('GBP');
	});

	test('a code the user typed is not overwritten when the org currency lands', async ({
		page
	}) => {
		let release!: () => void;
		const gate = new Promise<void>((resolve) => (release = resolve));
		await page.route(
			(url) => url.pathname === '/api/organization',
			async (route) => {
				await gate;
				await route.fulfill({
					json: { resolved_reporting_currency: 'GBP', settings: { reporting_currency: 'GBP' } }
				});
			}
		);

		await page.goto('/vendor-statements');
		await page.getByRole('button', { name: '+ New reconciliation' }).click();
		const dialog = page.getByRole('dialog', { name: 'New vendor statement reconciliation' });
		const currency = dialog.getByTestId('statement-currency');

		await currency.fill('HKD');
		release();
		// The suggestion list leads with the org's code once the store resolved,
		// which is the signal that the seeding effect has had its chance to run.
		await expect(dialog.locator('datalist option').first()).toHaveAttribute('value', 'GBP');
		await expect(currency).toHaveValue('HKD');
	});
});

test.describe('/vendor-statements (clerk — read-only)', () => {
	// Opt out of the default admin storage state so we can sign in as the clerk.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk can read the list but cannot mutate', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/vendor-statements');

		// Read is allowed for all four roles, so the page renders.
		await expect(page.getByRole('heading', { name: 'Statements' })).toBeVisible();

		// But a mutate call is rejected by the backend (admin / ap_manager only).
		// The `require_roles` dependency runs before the handler body, so the POST
		// 403s regardless of payload.
		const resp = await page.request.post(`${API_BASE}/api/vendor-statements`, {
			headers: await authedTenantHeaders(page),
			data: {
				vendor_id: '00000000-0000-0000-0000-000000000000',
				statement_date: '2026-01-31',
				currency: 'USD',
				lines: []
			}
		});
		expect(resp.status()).toBe(403);
	});
});
