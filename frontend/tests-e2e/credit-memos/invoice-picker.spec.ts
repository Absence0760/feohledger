import type { Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	deleteVendorsWhere,
	expect,
	invoicePicker,
	selectInvoiceInPicker,
	selectVendorInPicker,
	tenantPsql,
	test,
	vendorPicker
} from '../fixtures/helpers';

/**
 * `/credit-memos` — the invoice pickers ask the server for exactly the set the
 * application will accept, when a dialog opens (docs/decisions.md §202).
 *
 * What this replaced: both dialogs read one `invoices` array the page filled on
 * MOUNT by walking every page of `GET /api/invoices`, then filtered by vendor in
 * the browser. That cost one request per 100 invoices on every visit, and it
 * still offered invoices the apply refused — another currency, too little
 * balance left — each a 409 the operator only met after choosing.
 *
 * Driven against the real backend: the point is that what the picker lists and
 * what `POST /apply` accepts are the same set, and a stub would only prove the
 * picker renders whatever it is handed. Every fixture hangs off one vendor
 * named for this run, so `deleteVendorsWhere` takes the invoices and memos with
 * it.
 */

const RUN = `CMPICK${Date.now().toString(36).toUpperCase()}`;

async function makeVendor(page: Page, name: string): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: await authedTenantHeaders(page),
		data: { name }
	});
	expect(resp.status(), await resp.text()).toBe(201);
	return ((await resp.json()) as { id: string }).id;
}

/** An approved invoice firmly bound to `vendorId` — the link the credit guards
 *  compare. POST ignores a client-supplied status, so SQL sets both. */
async function makeInvoice(
	page: Page,
	vendor: { id: string; name: string },
	number: string,
	amount: string,
	currency = 'USD'
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: vendor.name, invoice_number: number, amount, currency }
	});
	expect(resp.status(), await resp.text()).toBe(201);
	const { id } = (await resp.json()) as { id: string };
	tenantPsql(`UPDATE invoices SET status='approved', vendor_id='${vendor.id}' WHERE id='${id}'`);
	return id;
}

async function makeMemo(page: Page, vendorId: string, number: string, amount: string) {
	const resp = await page.request.post(`${API_BASE}/api/credit-memos`, {
		headers: await authedTenantHeaders(page),
		data: { memo_number: number, vendor_id: vendorId, amount, currency: 'USD' }
	});
	expect(resp.status(), await resp.text()).toBe(201);
	return ((await resp.json()) as { id: string }).id;
}

/** Every invoice-LIST request the page makes, by exact pathname — never the
 *  dev server's `src/lib/api/*.ts` modules (tests-e2e/README.md § Stubbing). */
function recordRequests(page: Page) {
	const invoiceList: string[] = [];
	const eligible: URL[] = [];
	page.on('request', (request) => {
		const url = new URL(request.url());
		if (url.pathname === '/api/invoices') invoiceList.push(url.search);
		if (/^\/api\/credit-memos\/(.+\/)?eligible-invoices$/.test(url.pathname)) eligible.push(url);
	});
	return { invoiceList, eligible };
}

function applyDialog(page: Page) {
	return page.locator('div.modal[role="dialog"][aria-label="Apply credit memo"]');
}

test.describe('/credit-memos — the invoice pickers', () => {
	test('Apply offers exactly the invoices the apply accepts, searched on the server, and applies', async ({
		page
	}) => {
		const vendorName = `${RUN}-apply`;
		try {
			const vendorId = await makeVendor(page, vendorName);
			const vendor = { id: vendorId, name: vendorName };
			const okA = `${RUN}-A`;
			const okB = `${RUN}-B`;
			const okBId = await makeInvoice(page, vendor, okB, '300.00');
			await makeInvoice(page, vendor, okA, '500.00');
			// Refused by `/apply`, so never offered: another currency, and less
			// balance than the 75.00 credit.
			await makeInvoice(page, vendor, `${RUN}-EUR`, '500.00', 'EUR');
			await makeInvoice(page, vendor, `${RUN}-SMALL`, '10.00');
			const memoNumber = `${RUN}-MEMO`;
			const memoId = await makeMemo(page, vendorId, memoNumber, '75.00');

			const seen = recordRequests(page);
			await page.goto(`/credit-memos?search=${memoNumber}`);
			const row = page.locator('table tbody tr', { hasText: memoNumber });
			await expect(row).toBeVisible();
			// Mount issued its requests synchronously in the same effect that
			// fetched this row, so by the time the row is on screen any invoice
			// walk would already have been ISSUED — no quiet-network wait needed
			// for this absence.
			expect(seen.invoiceList, 'the page fetched invoice pages on mount').toEqual([]);
			expect(seen.eligible, 'an invoice picker loaded before any dialog opened').toEqual([]);

			const preload = page.waitForResponse(
				(r) => new URL(r.url()).pathname === `/api/credit-memos/${memoId}/eligible-invoices`
			);
			await row.getByRole('button', { name: 'Apply', exact: true }).click();
			const dialog = applyDialog(page);
			await expect(dialog).toBeVisible();
			// Loaded because the dialog opened — before the field is even touched.
			expect((await preload).status()).toBe(200);
			const applyButton = dialog.getByRole('button', { name: 'Apply', exact: true });
			await expect(applyButton).toBeDisabled();

			const field = invoicePicker(dialog, 'Invoice');
			await field.click();
			// Scoped to the picker's listbox: the page's own `<select>`s hold
			// `option`s too.
			const listbox = page.getByRole('listbox', { name: 'Invoice' });
			await expect(listbox.getByRole('option')).toHaveCount(2);
			await expect(listbox.getByRole('option', { name: new RegExp(`^${okA}\\s`) })).toBeVisible();
			await expect(listbox.getByRole('option', { name: new RegExp(`^${okB}\\s`) })).toBeVisible();
			await expect(dialog.getByText('All matches shown (2)')).toBeVisible();

			// Server-side search: the term travels to the eligible endpoint and the
			// list narrows to what the server answered.
			const searched = page.waitForRequest(
				(r) =>
					new URL(r.url()).pathname === `/api/credit-memos/${memoId}/eligible-invoices` &&
					new URL(r.url()).searchParams.get('search') === okB
			);
			await selectInvoiceInPicker(field, okB);
			await searched;
			await expect(applyButton).toBeEnabled();

			const posted = page.waitForRequest(
				(r) => r.method() === 'POST' && r.url().endsWith(`/api/credit-memos/${memoId}/apply`)
			);
			const applied = page.waitForResponse(
				(r) =>
					r.request().method() === 'POST' && r.url().endsWith(`/api/credit-memos/${memoId}/apply`)
			);
			await applyButton.click();
			expect((await posted).postDataJSON()).toEqual({ invoice_id: okBId });
			const resp = await applied;
			expect(resp.status()).toBe(200);
			expect(((await resp.json()) as { status: string }).status).toBe('applied');
			await expect(dialog).toBeHidden();
			await expect(row).toContainText(okB);
			expect(seen.invoiceList, 'the page fell back to walking invoice pages').toEqual([]);
		} finally {
			deleteVendorsWhere(`name = '${vendorName}'`);
		}
	});

	test('an Apply with nothing to go on says so before the field is opened, and stays disabled', async ({
		page
	}) => {
		const vendorName = `${RUN}-none`;
		try {
			const vendorId = await makeVendor(page, vendorName);
			// Its only invoice is in another currency than the memo.
			await makeInvoice(page, { id: vendorId, name: vendorName }, `${RUN}-ONLY-EUR`, '90.00', 'EUR');
			const memoNumber = `${RUN}-NONE`;
			await makeMemo(page, vendorId, memoNumber, '20.00');

			await page.goto(`/credit-memos?search=${memoNumber}`);
			const row = page.locator('table tbody tr', { hasText: memoNumber });
			await row.getByRole('button', { name: 'Apply', exact: true }).click();
			const dialog = applyDialog(page);

			// The closed field already carries the answer — in its description,
			// so a screen reader hears it on focus, not only a sighted user.
			const field = invoicePicker(dialog, 'Invoice');
			await expect(field).toHaveAccessibleDescription(/No invoice can take this credit/);
			await expect(dialog.getByText(/No invoice can take this credit/)).toBeVisible();
			await expect(page.getByRole('listbox')).toHaveCount(0);
			await expect(dialog.getByRole('button', { name: 'Apply', exact: true })).toBeDisabled();
		} finally {
			deleteVendorsWhere(`name = '${vendorName}'`);
		}
	});

	test('a failed load reads as a failure, never as "nothing to credit"', async ({ page }) => {
		const vendorName = `${RUN}-fail`;
		try {
			const vendorId = await makeVendor(page, vendorName);
			const memoNumber = `${RUN}-FAIL`;
			const memoId = await makeMemo(page, vendorId, memoNumber, '20.00');
			// Exact pathname — a glob would also answer `src/lib/api/creditMemos.ts`
			// under `vite dev` (docs/decisions.md §190).
			await page.route(
				(url) => url.pathname === `/api/credit-memos/${memoId}/eligible-invoices`,
				(route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
			);

			await page.goto(`/credit-memos?search=${memoNumber}`);
			await page
				.locator('table tbody tr', { hasText: memoNumber })
				.getByRole('button', { name: 'Apply', exact: true })
				.click();
			const dialog = applyDialog(page);
			await expect(dialog.getByText(/Invoices couldn’t be loaded/)).toBeVisible();
			await expect(dialog.getByText(/No invoice can take this credit/)).toHaveCount(0);
		} finally {
			deleteVendorsWhere(`name = '${vendorName}'`);
		}
	});

	test('the create dialog links only what a linked create accepts, narrowed by the amount', async ({
		page
	}) => {
		const vendorName = `${RUN}-link`;
		try {
			const vendorId = await makeVendor(page, vendorName);
			const vendor = { id: vendorId, name: vendorName };
			const big = `${RUN}-BIG`;
			const bigId = await makeInvoice(page, vendor, big, '400.00', 'EUR');
			await makeInvoice(page, vendor, `${RUN}-TINY`, '5.00');

			const seen = recordRequests(page);
			await page.goto('/credit-memos');
			await page.getByRole('button', { name: '+ New Credit Memo' }).click();
			const modal = page.locator('div.modal[role="dialog"][aria-label="New credit memo"]');
			await expect(modal).toBeVisible();

			await modal.getByLabel('Memo Number').fill(`${RUN}-NEW`);
			await selectVendorInPicker(vendorPicker(modal), vendorName);
			await modal.locator('input[type="number"]').fill('50.00');

			const field = invoicePicker(modal, 'Apply to invoice');
			await field.click();
			// The currency is not a leg here (a linked memo inherits the
			// invoice's), but the amount is: only BIG can absorb 50.00. Scoped to
			// the picker's listbox — the currency `<select>` holds options too.
			const listbox = page.getByRole('listbox', { name: 'Apply to invoice' });
			await expect(listbox.getByRole('option')).toHaveCount(1);
			await expect(listbox.getByRole('option', { name: new RegExp(`^${big}\\s`) })).toBeVisible();
			const lastFetch = seen.eligible.at(-1);
			expect(lastFetch?.pathname).toBe('/api/credit-memos/eligible-invoices');
			expect(lastFetch?.searchParams.get('vendor_id')).toBe(vendorId);
			// The number field hands over a number, so `50.00` travels as `50`.
			expect(Number(lastFetch?.searchParams.get('amount'))).toBe(50);

			await selectInvoiceInPicker(field, big);
			// Linked: the currency is the invoice's, shown rather than offered.
			await expect(modal.getByLabel('Currency')).toBeDisabled();
			await expect(modal.getByLabel('Currency')).toHaveValue('EUR');

			const created = page.waitForResponse(
				(r) => r.request().method() === 'POST' && r.url().endsWith('/api/credit-memos')
			);
			await modal.getByRole('button', { name: /^Create$/ }).click();
			const resp = await created;
			expect(resp.status()).toBe(201);
			const body = (await resp.json()) as { invoice_id: string; currency: string };
			expect(body.invoice_id).toBe(bigId);
			expect(body.currency).toBe('EUR');
			expect(seen.invoiceList, 'the create dialog walked invoice pages').toEqual([]);
		} finally {
			// The memo goes with the vendor it credits.
			deleteVendorsWhere(`name = '${vendorName}'`);
		}
	});
});
