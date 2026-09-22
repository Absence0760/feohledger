import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	escapeRegExp,
	expect,
	selectVendorInPicker,
	tenantPsql,
	test, vendorPicker
} from '../fixtures/helpers';

interface Vendor {
	id: string;
	name: string;
}

// Prefix used by the enrichment spec's fixture vendors. Filtering these out
// makes all helpers immune to leftover test-vendor pollution from prior runs
// where the cleanup was interrupted (e.g. by a test timeout).
const _FIXTURE_VENDOR_PREFIX = 'ENRICH-TEST-';

/**
 * Return a seed vendor (not an ENRICH-TEST fixture vendor) that has at
 * least one invoice, together with the id of that invoice.
 *
 * Resolution is by the invoice's own `vendor_id` — the link the backend's
 * credit-memo guard compares and the apply picker filters on. (It used to
 * re-derive the vendor from the free-text `vendor` name because the API
 * didn't expose `vendor_id`; a name that had drifted, e.g. the enrichment
 * spec's "(MOCK)" suffix, then produced a vendor whose id didn't actually
 * own the invoice.) An invoice with a null `vendor_id` is skipped: it can't
 * be credited at all.
 */
async function getVendorWithInvoice(
	page: import('@playwright/test').Page
): Promise<Vendor & { invoiceId: string }> {
	const headers = await authedTenantHeaders(page);

	const invResp = await page.request.get(`${API_BASE}/api/invoices`, { headers });
	const invBody = (await invResp.json()) as {
		items: Array<{ id: string; vendor: string; vendor_id: string | null }>;
	};
	if (!invBody.items.length) throw new Error('No invoices found in the tenant');

	const vResp = await page.request.get(`${API_BASE}/api/vendors?page_size=100`, { headers });
	const vendorsById = new Map(
		((await vResp.json()) as { items: Vendor[] }).items.map((v) => [v.id, v])
	);

	for (const inv of invBody.items) {
		if (!inv.vendor_id) continue;
		const vendor = vendorsById.get(inv.vendor_id);
		if (!vendor || vendor.name.startsWith(_FIXTURE_VENDOR_PREFIX)) continue;
		return { ...vendor, invoiceId: inv.id };
	}
	throw new Error('Could not find a non-fixture vendor with a linked invoice in the tenant');
}

async function createMemo(
	page: import('@playwright/test').Page,
	data: Record<string, unknown>
): Promise<{ id: string; status: string }> {
	const resp = await page.request.post(`${API_BASE}/api/credit-memos`, {
		headers: await authedTenantHeaders(page),
		data
	});
	expect(resp.status()).toBe(201);
	return (await resp.json()) as { id: string; status: string };
}

/**
 * Hard-delete a credit memo via psql. The product API doesn't expose a
 * delete endpoint by design (memos are kept for audit even after
 * voiding), so direct SQL is the only revertible path for tests.
 */
function deleteMemo(id: string): void {
	tenantPsql(`DELETE FROM credit_memos WHERE id='${id}'`);
}

/**
 * /credit-memos — list, create, apply, void. Each test creates fresh
 * test memos and removes them via psql in finally.
 */

test.describe('/credit-memos', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/credit-memos');
	});

	test('renders the empty-state placeholder when no memos exist', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Credit Memos' })).toBeVisible();
		// A fresh tenant has no seeded memos and gets the zero-data onboarding
		// block INSTEAD of the table; a run where earlier tests left memos
		// behind gets the table. Either is acceptable — what must not happen is
		// neither, which is what a load failure or a stuck spinner looks like.
		await expect(
			page.locator('table, [data-testid="credit-memos-empty-state"]').first()
		).toBeVisible();
	});

	test('Create modal validates and creates a memo via the API', async ({ page }) => {
		const vendor = await getVendorWithInvoice(page);
		const memoNumber = `CM-E2E-${Date.now()}`;
		let createdId: string | null = null;

		try {
			await page.getByRole('button', { name: '+ New Credit Memo' }).click();
			const modal = page.locator('div.modal[role="dialog"][aria-label="New credit memo"]');
			await expect(modal).toBeVisible();

			// By label, not by tag: the vendor picker is an `input[type="text"]`
			// too (a searchable combobox, not a `<select>`), so a bare tag
			// locator is a strict-mode violation on both fields.
			await modal.getByLabel('Memo Number').fill(memoNumber);
			await selectVendorInPicker(vendorPicker(modal), vendor.name);
			await modal.locator('input[type="number"]').fill('250.50');
			await modal.locator('textarea').fill('e2e: returned defective monitors');

			const created = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/credit-memos') &&
					r.request().method() === 'POST' &&
					r.status() === 201
			);
			await modal.getByRole('button', { name: /^Create$/ }).click();
			const resp = await created;
			const body = (await resp.json()) as { id: string; status: string };
			createdId = body.id;
			expect(body.status).toBe('open');

			// Modal closed; new row visible.
			await expect(modal).toBeHidden();
			await expect(
				page.locator('table tbody tr', { hasText: memoNumber })
			).toBeVisible();
		} finally {
			if (createdId) deleteMemo(createdId);
		}
	});

	test('Status filter chips narrow the list', async ({ page }) => {
		const vendor = await getVendorWithInvoice(page);
		const created: string[] = [];

		try {
			// Make one memo per status path.
			const open = await createMemo(page, {
				memo_number: `CM-OPEN-${Date.now()}`,
				vendor_id: vendor.id,
				amount: 100
			});
			created.push(open.id);

			await page.reload();

			// "Open" chip should reveal it.
			const openFiltered = page.waitForResponse(
				(r) =>
					r.url().includes('/api/credit-memos') && r.url().includes('status=open')
			);
			// `\b`, not `$`: the chip carries its whole-set count once
			// `GET /api/credit-memos/counts` lands ("Open 3").
			await page.locator('.filter-chip', { hasText: /^Open\b/ }).click();
			await openFiltered;
			await expect(
				page.locator('table tbody tr.applied').first()
			).toHaveCount(0);

			// "Applied" chip should not include it.
			const appliedFiltered = page.waitForResponse(
				(r) =>
					r.url().includes('/api/credit-memos') && r.url().includes('status=applied')
			);
			await page.locator('.filter-chip', { hasText: /^Applied\b/ }).click();
			await appliedFiltered;
			await expect(
				page.locator('table tbody tr', { hasText: open.id.slice(0, 8) })
			).toHaveCount(0);
		} finally {
			for (const id of created) deleteMemo(id);
		}
	});

	test('the chips carry the server tallies for exactly the searched population', async ({
		page
	}) => {
		const vendor = await getVendorWithInvoice(page);
		const prefix = `CM-CNT-${Date.now()}`;
		const created: string[] = [];

		try {
			for (const suffix of ['A', 'B', 'C']) {
				const memo = await createMemo(page, {
					memo_number: `${prefix}-${suffix}`,
					vendor_id: vendor.id,
					amount: 5
				});
				created.push(memo.id);
			}
			const voidResp = await page.request.post(
				`${API_BASE}/api/credit-memos/${created[2]}/void`,
				{ headers: await authedTenantHeaders(page) }
			);
			expect(voidResp.status()).toBe(200);

			// Real backend, real `GET /api/credit-memos/counts`: the search term
			// narrows the tallies to exactly this test's three rows, whatever else
			// the worker's tenant holds.
			await page.goto(`/credit-memos?search=${prefix}`);
			await expect(page.getByRole('button', { name: 'All 3', exact: true })).toBeVisible();
			await expect(page.getByRole('button', { name: 'Open 2', exact: true })).toBeVisible();
			await expect(page.getByRole('button', { name: 'Applied 0', exact: true })).toBeVisible();
			await expect(page.getByRole('button', { name: 'Void 1', exact: true })).toBeVisible();
			await expect(page.locator('table tbody tr')).toHaveCount(3);
		} finally {
			for (const id of created) deleteMemo(id);
		}
	});

	test('Apply flips an open memo to applied via the UI', async ({ page }) => {
		// getVendorWithInvoice returns a vendor that already has an invoice,
		// so we can skip the separate invoice-search step.
		const vendor = await getVendorWithInvoice(page);

		const memo = await createMemo(page, {
			memo_number: `CM-APPLY-${Date.now()}`,
			vendor_id: vendor.id,
			amount: 75
		});

		try {
			await page.reload();

			const row = page.locator('table tbody tr', { hasText: `CM-APPLY-` });
			await row.getByRole('button', { name: 'Apply' }).click();

			const applyModal = page.locator(
				'div.modal[role="dialog"][aria-label="Apply credit memo"]'
			);
			await expect(applyModal).toBeVisible();
			await applyModal.locator('select').selectOption(vendor.invoiceId);

			const applied = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/credit-memos/${memo.id}/apply`) &&
					r.request().method() === 'POST' &&
					r.status() === 200
			);
			await applyModal.getByRole('button', { name: /^Apply$/ }).click();
			const resp = await applied;
			const body = (await resp.json()) as { status: string; invoice_id: string };
			expect(body.status).toBe('applied');
			expect(body.invoice_id).toBe(vendor.invoiceId);
		} finally {
			deleteMemo(memo.id);
		}
	});

	test('API: cannot apply a credit memo to an invoice from a different vendor', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);
		const vendorsResp = await page.request.get(`${API_BASE}/api/vendors`, { headers });
		const vendors = (await vendorsResp.json()) as { items: Vendor[] };
		expect(vendors.items.length).toBeGreaterThanOrEqual(2);
		const [vendorA, vendorB] = vendors.items;

		// Create the memo for vendorA, plus a fresh invoice for vendorB.
		// Don't lean on the seed having a vendorB invoice — bind the
		// invoice to vendorB.id directly via psql since the public
		// /api/invoices POST infers vendor_id from vendor_name and
		// vendorB.name might not exist as a fuzzy match.
		const memo = await createMemo(page, {
			memo_number: `CM-MISMATCH-${Date.now()}`,
			vendor_id: vendorA.id,
			amount: 50
		});

		const invoiceResp = await page.request.post(`${API_BASE}/api/invoices`, {
			headers,
			data: {
				vendor: vendorB.name,
				invoice_number: `MISMATCH-${Date.now()}`,
				amount: 100,
				status: 'new'
			}
		});
		const invoiceB = (await invoiceResp.json()) as { id: string };
		// Force vendor_id=vendorB regardless of whatever vendor matching
		// the create endpoint did — we want a deterministic mismatch.
		tenantPsql(`UPDATE invoices SET vendor_id='${vendorB.id}' WHERE id='${invoiceB.id}'`);

		try {
			const resp = await page.request.post(
				`${API_BASE}/api/credit-memos/${memo.id}/apply`,
				{
					headers,
					data: { invoice_id: invoiceB.id }
				}
			);
			expect(resp.status()).toBe(409);
		} finally {
			deleteMemo(memo.id);
			tenantPsql(
				`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id='${invoiceB.id}')`
			);
			tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${invoiceB.id}'`);
			// vendorB.name may not match an existing vendor exactly, minting a
			// fresh `unverified` one — refresh_warnings (now run at manual-entry
			// creation time) raises an `unverified_vendor` exception against it,
			// which FKs to this invoice and must clear before the delete below.
			tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${invoiceB.id}'`);
			// audit_log is append-only (DB trigger, migration 0022 + seed) — never DELETE;
			// orphan rows for the removed invoice are harmless (no FK back to invoices).
			deleteInvoicesWhere(`id='${invoiceB.id}'`);
		}
	});

	test('API: cannot void an already-applied memo (audit immutability)', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		// Use getVendorWithInvoice to guarantee we have a vendor+invoice pair
		// regardless of leftover test-vendor pollution in the tenant.
		const vendor = await getVendorWithInvoice(page);

		const memo = await createMemo(page, {
			memo_number: `CM-VOID-${Date.now()}`,
			vendor_id: vendor.id,
			amount: 25,
			invoice_id: vendor.invoiceId
		});
		expect(memo.status).toBe('applied');

		try {
			const voidResp = await page.request.post(
				`${API_BASE}/api/credit-memos/${memo.id}/void`,
				{ headers }
			);
			expect(voidResp.status()).toBe(409);
		} finally {
			deleteMemo(memo.id);
		}
	});

	test('Void flips an open memo to void status', async ({ page }) => {
		const vendor = await getVendorWithInvoice(page);
		const memo = await createMemo(page, {
			memo_number: `CM-VOID-OK-${Date.now()}`,
			vendor_id: vendor.id,
			amount: 30
		});

		try {
			await page.reload();
			const row = page.locator('table tbody tr', { hasText: `CM-VOID-OK-` });

			// Void is a two-click armed-confirm action (irreversible-action guard).
			// First click arms the button (Void → Confirm); the API call fires on
			// the second click.  Set up the response listener BETWEEN the two clicks
			// so it's in place before the confirming click but not racing the arm.
			await row.getByRole('button', { name: 'Void' }).click();
			const confirmBtn = row.getByRole('button', { name: 'Confirm' });
			await expect(confirmBtn).toBeVisible();

			const voided = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/credit-memos/${memo.id}/void`) &&
					r.request().method() === 'POST' &&
					r.status() === 200
			);
			await confirmBtn.click();
			const resp = await voided;
			expect(((await resp.json()) as { status: string }).status).toBe('void');
		} finally {
			deleteMemo(memo.id);
		}
	});
});

/**
 * Correcting a mis-keyed memo, and linking one to its invoice at creation
 * (issue #443). Before either existed, a memo keyed in the wrong currency was
 * permanently unappliable AND uncorrectable — the only exit was Void and
 * re-create, leaving a void row in the audit trail for what was a typo.
 *
 * Every test narrows the list to its own rows through the URL-backed
 * `?search=`, so whatever else the worker's tenant holds is off screen.
 */
test.describe('/credit-memos — edit and link-at-create', () => {
	function modalNamed(page: import('@playwright/test').Page, name: string) {
		return page.locator(`div.modal[role="dialog"][aria-label="${name}"]`);
	}

	function editAuditRows(id: string): number {
		return Number(
			tenantPsql(
				`SELECT count(*) FROM audit_log WHERE entity_id='${id}' AND entity_type='credit_memo' AND action='credit_memo.updated'`
			).trim()
		);
	}

	test('Edit corrects an open memo, sending only the fields that changed', async ({ page }) => {
		const vendor = await getVendorWithInvoice(page);
		const memoNumber = `CM-EDIT-${Date.now()}`;
		const memo = await createMemo(page, {
			memo_number: memoNumber,
			vendor_id: vendor.id,
			amount: '100.00',
			currency: 'USD'
		});

		try {
			await page.goto(`/credit-memos?search=${memoNumber}`);
			const row = page.locator('table tbody tr', { hasText: memoNumber });
			await row.getByRole('button', { name: 'Edit', exact: true }).click();

			const modal = modalNamed(page, 'Edit credit memo');
			await expect(modal).toBeVisible();
			// Prefilled from the row, including the vendor the picker cannot look
			// up from its own first page.
			await expect(modal.getByLabel('Memo Number')).toHaveValue(memoNumber);
			await expect(vendorPicker(modal)).toHaveValue(new RegExp(escapeRegExp(vendor.name)));

			await modal.locator('input[type="number"]').fill('88.50');
			await modal.getByLabel('Currency').selectOption('EUR');

			const patched = page.waitForRequest(
				(r) => r.method() === 'PATCH' && r.url().endsWith(`/api/credit-memos/${memo.id}`)
			);
			const saved = page.waitForResponse(
				(r) => r.request().method() === 'PATCH' && r.url().endsWith(`/api/credit-memos/${memo.id}`)
			);
			await modal.getByRole('button', { name: 'Save', exact: true }).click();
			// Only what changed — never a re-assertion of the untouched fields
			// over whatever a concurrent editor just saved.
			expect((await patched).postDataJSON()).toEqual({ amount: '88.5', currency: 'EUR' });
			const resp = await saved;
			expect(resp.status()).toBe(200);
			const body = (await resp.json()) as { currency: string; status: string };
			expect(body.currency).toBe('EUR');
			expect(body.status).toBe('open');

			await expect(modal).toBeHidden();
			await expect(row).toContainText('88.50');
			expect(editAuditRows(memo.id)).toBe(1);
		} finally {
			deleteMemo(memo.id);
		}
	});

	test('Edit is offered only on an open memo that has never been applied', async ({ page }) => {
		const vendor = await getVendorWithInvoice(page);
		const prefix = `CM-EDITABLE-${Date.now()}`;
		const created: string[] = [];

		try {
			const open = await createMemo(page, {
				memo_number: `${prefix}-OPEN`,
				vendor_id: vendor.id,
				amount: 5
			});
			created.push(open.id);
			const applied = await createMemo(page, {
				memo_number: `${prefix}-APPLIED`,
				vendor_id: vendor.id,
				amount: 1,
				invoice_id: vendor.invoiceId
			});
			created.push(applied.id);
			expect(applied.status).toBe('applied');
			const voided = await createMemo(page, {
				memo_number: `${prefix}-VOID`,
				vendor_id: vendor.id,
				amount: 5
			});
			created.push(voided.id);
			const voidResp = await page.request.post(`${API_BASE}/api/credit-memos/${voided.id}/void`, {
				headers: await authedTenantHeaders(page)
			});
			expect(voidResp.status()).toBe(200);

			await page.goto(`/credit-memos?search=${prefix}`);
			const rowFor = (suffix: string) =>
				page.locator('table tbody tr', { hasText: `${prefix}-${suffix}` });
			await expect(rowFor('OPEN').getByRole('button', { name: 'Edit', exact: true })).toBeVisible();
			// Positive assertion above first, so these absences are not passing
			// vacuously against a table that has not rendered yet.
			await expect(rowFor('APPLIED')).toBeVisible();
			await expect(rowFor('APPLIED').getByRole('button', { name: 'Edit' })).toHaveCount(0);
			await expect(rowFor('VOID')).toBeVisible();
			await expect(rowFor('VOID').getByRole('button', { name: 'Edit' })).toHaveCount(0);
		} finally {
			for (const id of created) deleteMemo(id);
		}
	});

	test('an edit that lost the race to an apply is refused with the reason, and the row catches up', async ({
		page
	}) => {
		const vendor = await getVendorWithInvoice(page);
		const memoNumber = `CM-EDIT-RACE-${Date.now()}`;
		const memo = await createMemo(page, {
			memo_number: memoNumber,
			vendor_id: vendor.id,
			amount: 1
		});

		try {
			await page.goto(`/credit-memos?search=${memoNumber}`);
			const row = page.locator('table tbody tr', { hasText: memoNumber });
			await row.getByRole('button', { name: 'Edit', exact: true }).click();
			const modal = modalNamed(page, 'Edit credit memo');
			await expect(modal).toBeVisible();

			// Someone else applies the memo while this dialog is open.
			const applyResp = await page.request.post(
				`${API_BASE}/api/credit-memos/${memo.id}/apply`,
				{ headers: await authedTenantHeaders(page), data: { invoice_id: vendor.invoiceId } }
			);
			expect(applyResp.status()).toBe(200);

			await modal.locator('textarea').fill('late correction');
			const refused = page.waitForResponse(
				(r) => r.request().method() === 'PATCH' && r.url().endsWith(`/api/credit-memos/${memo.id}`)
			);
			await modal.getByRole('button', { name: 'Save', exact: true }).click();
			expect((await refused).status()).toBe(409);

			// The server's own reason reaches the operator, and the list reloads
			// to show why: the memo is applied, so it no longer offers Edit.
			await expect(page.getByText(/never been applied can be edited/)).toBeVisible();
			await expect(row.locator('.badge')).toHaveText('Applied');
			await expect(row.getByRole('button', { name: 'Edit' })).toHaveCount(0);
			expect(editAuditRows(memo.id)).toBe(0);
		} finally {
			deleteMemo(memo.id);
		}
	});

	test('Create can link the memo to an invoice, which applies it in the invoice currency', async ({
		page
	}) => {
		const vendor = await getVendorWithInvoice(page);
		const headers = await authedTenantHeaders(page);
		const invoice = (await (
			await page.request.get(`${API_BASE}/api/invoices/${vendor.invoiceId}`, { headers })
		).json()) as { invoice_number: string; currency: string };
		const memoNumber = `CM-LINK-${Date.now()}`;
		let createdId: string | null = null;

		try {
			await page.goto('/credit-memos');
			await page.getByRole('button', { name: '+ New Credit Memo' }).click();
			const modal = modalNamed(page, 'New credit memo');
			await expect(modal).toBeVisible();

			const invoiceSelect = modal.getByLabel('Apply to invoice');
			// Nothing to link until a vendor names whose invoices are eligible.
			await expect(invoiceSelect).toBeDisabled();

			await modal.getByLabel('Memo Number').fill(memoNumber);
			await selectVendorInPicker(vendorPicker(modal), vendor.name);
			await modal.locator('input[type="number"]').fill('1.00');
			await invoiceSelect.selectOption(vendor.invoiceId);

			// Linked: the currency is the invoice's, shown rather than offered —
			// asserting any other would only be refused.
			const currency = modal.getByLabel('Currency');
			await expect(currency).toBeDisabled();
			await expect(currency).toHaveValue(invoice.currency.toUpperCase());

			const posted = page.waitForRequest(
				(r) => r.method() === 'POST' && r.url().endsWith('/api/credit-memos')
			);
			const created = page.waitForResponse(
				(r) => r.request().method() === 'POST' && r.url().endsWith('/api/credit-memos')
			);
			await modal.getByRole('button', { name: /^Create$/ }).click();
			const sent = (await posted).postDataJSON() as Record<string, unknown>;
			expect(sent.invoice_id).toBe(vendor.invoiceId);
			// The memo INHERITS the invoice's currency server-side.
			expect(sent).not.toHaveProperty('currency');

			const resp = await created;
			expect(resp.status()).toBe(201);
			const body = (await resp.json()) as {
				id: string;
				status: string;
				invoice_id: string;
				currency: string;
			};
			createdId = body.id;
			expect(body.status).toBe('applied');
			expect(body.invoice_id).toBe(vendor.invoiceId);
			expect(body.currency).toBe(invoice.currency.toUpperCase());

			await expect(modal).toBeHidden();
			await expect(
				page.locator('table tbody tr', { hasText: memoNumber })
			).toContainText(invoice.invoice_number);
		} finally {
			if (createdId) deleteMemo(createdId);
		}
	});
});
