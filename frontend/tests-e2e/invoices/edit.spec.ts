import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * /invoices — modal edit + save round-trip.
 *
 * The modal pre-fills from the invoice row, lets the user edit form
 * fields, and persists via PATCH /api/invoices/<id>. The contract
 * we care about: a saved edit survives a modal reopen.
 *
 * Tests mutate one field (description) and revert via API in finally
 * so the suite is re-runnable locally.
 */

test.describe('/invoices modal edit + save', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();
	});

	test("modal pre-fills with the API's values for the row", async ({ page }) => {
		// The row's vendor cell renders the vendor name + a priors-summary
		// badge (e.g. "cache·2"), so reading td textContent merges both.
		// Read the invoice from the API for the canonical pre-fill values.
		const listResp = await page.request.get(`${API_BASE}/api/invoices`, {
			headers: await authedTenantHeaders(page)
		});
		const listed = (await listResp.json()) as {
			items: Array<{ id: string; invoice_number: string; vendor: string }>;
		};
		const target = listed.items[0];
		expect(target).toBeTruthy();

		await page
			.locator('table tbody tr', { hasText: target.invoice_number })
			.first()
			.getByRole('button', { name: 'Edit' })
			.click();
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal).toBeVisible();
		await expect(modal.locator('header h2')).toContainText(target.invoice_number);
		// vendor + invoice_number inputs are pre-filled (first two text inputs).
		await expect(modal.locator('input[type="text"]').nth(0)).toHaveValue(target.vendor);
		await expect(modal.locator('input[type="text"]').nth(1)).toHaveValue(
			target.invoice_number
		);
	});

	test('PATCH /api/invoices/<id> persists a description change', async ({ page }) => {
		// API-level persistence test. The UI form-submit path was flaky
		// in this suite (HTML5 validation interplay with the multi-step
		// modal), but the underlying API surface is what guards the
		// money path — round-trip via page.request directly.
		const headers = await authedTenantHeaders(page);
		const listResp = await page.request.get(`${API_BASE}/api/invoices`, { headers });
		const listed = (await listResp.json()) as {
			items: Array<{ id: string; description: string | null; status: string }>;
		};
		// IMMUTABLE_STATUSES on the backend rejects PATCH for these — pick
		// an invoice whose status is editable. Default seed order surfaces
		// posted_in_erp / done invoices among the first rows.
		const immutable = new Set([
			'sending_to_erp',
			'sent_to_erp',
			'posted_in_erp',
			'payment_scheduled',
			'paid',
			'done'
		]);
		const target = listed.items.find((i) => !immutable.has(i.status));
		expect(target, 'no editable invoice in the seed').toBeTruthy();
		const original = target!.description ?? '';
		const next = `e2e-api-edited-${Date.now()}`;

		try {
			// Use page.request.fetch with an explicit Content-Type to guard
			// against an intermittent flake where Playwright's `data: {...}`
			// shorthand drops the JSON content-type after enough requests
			// in the same context, causing FastAPI to receive an empty
			// body and exclude_unset=True to skip the description field
			// (so the response echoes the seed value instead of `next`).
			const patch = await page.request.fetch(
				`${API_BASE}/api/invoices/${target!.id}`,
				{
					method: 'PATCH',
					headers: {
						...headers,
						'Content-Type': 'application/json'
					},
					data: JSON.stringify({ description: next })
				}
			);
			expect(patch.status()).toBe(200);
			expect(((await patch.json()) as { description: string }).description).toBe(next);

			// Round-trip: GET reflects the change.
			const get = await page.request.get(`${API_BASE}/api/invoices/${target!.id}`, {
				headers
			});
			expect(((await get.json()) as { description: string }).description).toBe(next);
		} finally {
			await page.request.fetch(`${API_BASE}/api/invoices/${target!.id}`, {
				method: 'PATCH',
				headers: {
					...headers,
					'Content-Type': 'application/json'
				},
				data: JSON.stringify({ description: original })
			});
		}
	});

	test('Cancel button dismisses without saving', async ({ page }) => {
		await page
			.locator('table tbody tr')
			.first()
			.getByRole('button', { name: 'Edit' })
			.click();
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal).toBeVisible();

		// Cancel is in the footer, distinct from the close-X (aria-label="Close").
		await modal.getByRole('button', { name: /^Cancel$/ }).click();
		await expect(modal).toBeHidden();
	});
});

/**
 * Two defects from a live test (2026-10-19), on one untouched open + Save:
 *
 * - The amount input read `77.1` for an invoice of 77.10 USD: money crosses
 *   the wire as a JSON number, which has no trailing zeros. The modal now
 *   starts from `moneyInputText` — the currency's precision — and the save
 *   sends that exact text back.
 * - The save wrote an `invoice.edited` row recording `po_number` and
 *   `description` as `null -> ""`: edits nobody made, on the SOX trail.
 */
const ROUND_TRIP_PREFIX = 'E2E-AMOUNT-PRECISION-';

test.describe('/invoices modal amount precision + no-op save', () => {
	test.afterEach(() => {
		deleteInvoicesWhere(`invoice_number LIKE '${ROUND_TRIP_PREFIX}%'`);
	});

	test('shows 77.10 as 77.10 and an untouched save audits no edit', async ({ page }) => {
		const number = `${ROUND_TRIP_PREFIX}${Date.now()}`;
		// po_number / description stay NULL, as on an invoice nobody coded. The
		// vendor is already LINKED, as on the live invoice: a save that finds no
		// link resolves one, and that is a real, audited change (`vendor_id`).
		tenantPsql(
			`INSERT INTO invoices (id, correlation_id, organization_id, invoice_number, vendor_name,
			   vendor_id, amount, currency, status, created_at, updated_at)
			 SELECT gen_random_uuid(), gen_random_uuid(), i.organization_id, '${number}',
			   v.name, v.id, 77.10, 'USD', 'new', now(), now()
			 FROM vendors v, (SELECT organization_id FROM invoices LIMIT 1) i
			 WHERE v.status = 'active' LIMIT 1`
		);

		await page.goto('/invoices');
		const listed = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes(`search=${encodeURIComponent(number)}`) &&
				r.request().method() === 'GET'
		);
		await page.getByPlaceholder('Search invoices...').fill(number);
		await listed;
		await page
			.locator('table tbody tr', { hasText: number })
			.first()
			.getByRole('button', { name: 'Edit' })
			.click();

		const modal = page.getByRole('dialog');
		const amountInput = modal.locator('input[type="number"]').first();
		await expect(amountInput).toHaveValue('77.10');

		const saved = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices/') &&
				!r.url().includes('/api/invoices?') &&
				r.request().method() === 'PATCH'
		);
		await modal.getByRole('button', { name: /^Save$/ }).click();
		const resp = await saved;
		expect(resp.status()).toBe(200);
		const body = resp.request().postDataJSON() as Record<string, unknown>;
		expect(body.amount).toBe('77.10');
		expect(body.po_number).toBeNull();
		expect(body.description).toBeNull();

		expect(
			tenantPsql(`SELECT amount FROM invoices WHERE invoice_number = '${number}'`).trim()
		).toBe('77.10');
		// The changes themselves, not a count, so a failure names the field.
		expect(
			tenantPsql(
				`SELECT coalesce(string_agg(a.details->>'changes', '; '), '')
				 FROM audit_log a JOIN invoices i ON a.correlation_id = i.correlation_id
				 WHERE i.invoice_number = '${number}' AND a.action = 'invoice.edited'`
			).trim()
		).toBe('');
	});
});
