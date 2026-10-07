import type { Locator, Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * The no-rail pilot (issue #517): a customer pays its suppliers from its own
 * bank or ERP and RECORDS the payment in FeohLedger, which never sends money.
 *
 *  - From the payment queue, one invoice is recorded as paid outside
 *    FeohLedger: it leaves the queue, the invoice is `paid`, and History labels
 *    the payment "Paid outside FeohLedger" rather than the raw provider tag.
 *  - On a record-only tenant the run dialog offers no Execute at all, and a
 *    draft run is recorded as paid by the customer's bank instead. The NACHA
 *    bank-file refusal for an unconfigured tenant is stated in the dialog.
 *  - The NACHA bank-file lifecycle: the first export CLAIMS the run
 *    (`draft → exported`, no Execute, no Cancel), a second file must be asked
 *    for explicitly (armed Regenerate; the bare re-export 409s), "Bank
 *    rejected the file" releases it to `draft`, and recording closes it.
 *
 * SoD note: like `execute.spec.ts`, the admin both creates and records here,
 * so maker-checker (`require_run_segregation`) is switched off for the test —
 * a legitimate single-operator configuration. The run-level refusals are
 * covered by the backend's `test_external_payment.py`.
 *
 * Hermetic: `PATCH /api/organization` replaces the WHOLE `settings.payments`
 * block, so each test snapshots the block first and restores it afterwards.
 */

interface QueueItem {
	id: string;
	invoice_number: string;
	blocked?: boolean;
	required_method?: string | null;
	accepted_discount_amount?: string | null;
}

async function headers(page: Page) {
	return authedTenantHeaders(page);
}

async function getQueue(page: Page): Promise<QueueItem[]> {
	const resp = await page.request.get(`${API_BASE}/api/payments/queue?page=1&page_size=100`, {
		headers: await headers(page)
	});
	expect(resp.ok()).toBe(true);
	return ((await resp.json()) as { items: QueueItem[] }).items;
}

/** A queue row the record path accepts: not blocked, not pinned to a live
 *  card, and no accepted discount (recording would capture the offer, which
 *  the teardown below does not unwind). */
async function recordableQueueItem(page: Page): Promise<QueueItem> {
	const target = (await getQueue(page)).find(
		(q) => !q.blocked && !q.required_method && !q.accepted_discount_amount
	);
	expect(target, 'the seed leaves at least one plain payable invoice').toBeDefined();
	return target!;
}

async function getInvoiceStatus(page: Page, id: string): Promise<string> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${id}`, {
		headers: await headers(page)
	});
	return ((await resp.json()) as { status: string }).status;
}

async function getPaymentsSettings(page: Page): Promise<Record<string, unknown> | null> {
	const resp = await page.request.get(`${API_BASE}/api/organization`, {
		headers: await headers(page)
	});
	const body = (await resp.json()) as { settings: Record<string, unknown> };
	return (body.settings.payments as Record<string, unknown> | undefined) ?? null;
}

async function putPaymentsSettings(page: Page, payments: Record<string, unknown> | null) {
	const resp = await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await headers(page),
		data: { settings: { payments: payments ?? {} } }
	});
	expect(resp.ok(), await resp.text()).toBe(true);
}

/** Put the invoice back where the test found it. A recorded payment moves the
 *  invoice into an immutable status, so direct SQL is the only way back (the
 *  same reasoning as `execute.spec.ts`). */
function resetInvoice(invoiceId: string, status: string): void {
	tenantPsql(
		`DELETE FROM payments WHERE invoice_id='${invoiceId}' AND provider='external' AND payment_run_id IS NULL`
	);
	tenantPsql(`UPDATE invoices SET status='${status}' WHERE id='${invoiceId}'`);
}

function deletePaymentRun(runId: string): void {
	tenantPsql(`DELETE FROM payments WHERE payment_run_id='${runId}'`);
	tenantPsql(`DELETE FROM payment_runs WHERE id='${runId}'`);
}

function runDialog(page: Page): Locator {
	return page.locator('div.modal[role="dialog"][aria-label="Payment run"]');
}

test.describe('/payments — paying outside FeohLedger', () => {
	let savedPayments: Record<string, unknown> | null = null;

	test.beforeEach(async ({ page }) => {
		savedPayments = await getPaymentsSettings(page);
	});

	test.afterEach(async ({ page }) => {
		await putPaymentsSettings(page, savedPayments);
	});

	test('records one queued invoice as paid outside FeohLedger', async ({ page }) => {
		await putPaymentsSettings(page, { ...(savedPayments ?? {}), require_run_segregation: false });
		const target = await recordableQueueItem(page);
		const sourceStatus = await getInvoiceStatus(page, target.id);

		try {
			await page.goto('/payments');
			const row = page.locator('table tbody tr', { hasText: target.invoice_number }).first();
			await expect(row).toBeVisible();
			await row
				.getByRole('button', { name: `Record as paid — invoice ${target.invoice_number}` })
				.click();

			const dialog = page.getByRole('dialog', {
				name: 'Record a payment made outside FeohLedger'
			});
			await expect(dialog).toBeVisible();
			// The amount owed is stated before anything is recorded.
			await expect(dialog.getByTestId('record-amount-owed')).not.toHaveText('');
			// Nothing can be submitted without the reference bank rec matches on.
			await expect(dialog.getByTestId('record-confirm')).toBeDisabled();

			await dialog.getByTestId('record-method').selectOption('check');
			await dialog.getByTestId('record-reference').fill(`CHK-${target.invoice_number}`);

			const recorded = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === '/api/payments/record-outside' &&
					r.request().method() === 'POST'
			);
			await dialog.getByTestId('record-confirm').click();
			const resp = await recorded;
			expect(resp.status()).toBe(201);
			const payment = (await resp.json()) as {
				status: string;
				provider: string;
				method: string;
				reference: string;
			};
			expect(payment).toMatchObject({
				status: 'completed',
				provider: 'external',
				method: 'check',
				reference: `CHK-${target.invoice_number}`
			});

			// The dialog closes and the invoice leaves the queue.
			await expect(dialog).toBeHidden();
			await expect(
				page.locator('table tbody tr', { hasText: target.invoice_number })
			).toHaveCount(0);
			expect(await getInvoiceStatus(page, target.id)).toBe('paid');

			// History names it for what it is — never the raw `external` tag.
			await page.getByRole('tab', { name: 'History' }).click();
			const historyRow = page.locator('table tbody tr', { hasText: target.invoice_number });
			await expect(historyRow.getByTestId('payment-external')).toHaveText(
				'Paid outside FeohLedger'
			);
		} finally {
			resetInvoice(target.id, sourceStatus);
		}
	});

	test('a record-only tenant has no Execute, and a draft run is recorded as paid', async ({
		page
	}) => {
		await putPaymentsSettings(page, {
			...(savedPayments ?? {}),
			require_run_segregation: false,
			mode: 'record_only',
			nacha: null
		});
		const target = await recordableQueueItem(page);
		const sourceStatus = await getInvoiceStatus(page, target.id);
		const h = await headers(page);

		const createResp = await page.request.post(`${API_BASE}/api/payments/runs`, {
			headers: h,
			data: { items: [{ invoice_id: target.id, method: 'ach' }] }
		});
		expect(createResp.status()).toBe(201);
		const runId = ((await createResp.json()) as { id: string }).id;

		try {
			// The server refuses the send path outright — the UI only mirrors it.
			const execute = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers: h }
			);
			expect(execute.status()).toBe(409);
			expect(((await execute.json()) as { detail: { code: string } }).detail.code).toBe(
				'payments_record_only'
			);

			await page.goto('/payments?tab=runs');
			await expect(page.getByTestId('record-only-banner')).toBeVisible();
			await page
				.getByRole('button', { name: `View payment run ${runId.slice(0, 8)}` })
				.click();

			const modal = runDialog(page);
			await expect(modal.locator('.status-badge')).toHaveText('Draft');
			// Positive signals first (the record and bank-file panels rendered),
			// then the absence that is the point of the test.
			await expect(modal.getByTestId('run-record-panel')).toBeVisible();
			await expect(modal.getByTestId('run-nacha-panel')).toBeVisible();
			await expect(modal.getByTestId('run-record-only-note')).toBeVisible();
			await expect(modal.getByRole('button', { name: /^Execute/ })).toHaveCount(0);

			// No bank-file settings on this tenant: the refusal is stated in the
			// dialog, naming what an admin has to fill in.
			const nacha = page.waitForResponse(
				(r) => new URL(r.url()).pathname === `/api/payments/runs/${runId}/nacha`
			);
			await modal.getByRole('button', { name: 'Download bank file (NACHA)' }).click();
			expect((await nacha).status()).toBe(409);
			await expect(modal.getByTestId('nacha-error')).toContainText('Company name');

			// Record the run as paid by the customer's bank.
			await modal.getByRole('button', { name: 'Record run as paid…' }).click();
			await modal.getByTestId('run-record-reference').fill(`BATCH-${runId.slice(0, 8)}`);
			const recorded = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === `/api/payments/runs/${runId}/record-outside` &&
					r.request().method() === 'POST'
			);
			await modal.getByTestId('run-record-confirm').click();
			const resp = await recorded;
			expect(resp.status()).toBe(200);
			expect(await resp.json()).toMatchObject({ status: 'completed', payment_count: 1 });

			await expect(modal.locator('.status-badge')).toHaveText('Completed');
			await expect(modal.getByTestId('run-payment-external')).toHaveText(
				'Paid outside FeohLedger'
			);
			// A closed-out run offers nothing more to record.
			await expect(modal.getByTestId('run-record-panel')).toHaveCount(0);
			expect(await getInvoiceStatus(page, target.id)).toBe('paid');
		} finally {
			deletePaymentRun(runId);
			resetInvoice(target.id, sourceStatus);
		}
	});

	test('a NACHA export claims the run; the bank\'s rejection releases it; recording closes it', async ({
		page
	}) => {
		await putPaymentsSettings(page, {
			...(savedPayments ?? {}),
			require_run_segregation: false,
			mode: 'record_only',
			nacha: { company_name: 'E2E CORP', company_id: '1123456789', odfi_routing: '021000021' }
		});
		const target = await recordableQueueItem(page);
		const sourceStatus = await getInvoiceStatus(page, target.id);
		const h = await headers(page);

		// The file needs the payee's bank details. The seed carries none, so the
		// vendor gets a test routing + account for the length of the test and
		// its previous value back afterwards. Direct SQL, not the API: a bank
		// change through the API is dual-control by design (a second approver),
		// which is not what this test is about.
		const vendorId = tenantPsql(
			`SELECT vendor_id FROM invoices WHERE id='${target.id}'`
		).trim();
		expect(vendorId, 'the target invoice has a linked vendor').not.toBe('');
		const savedBank = tenantPsql(
			`SELECT COALESCE(bank_details::text, 'NULL') FROM vendors WHERE id='${vendorId}'`
		).trim();
		tenantPsql(
			`UPDATE vendors SET bank_details='{"routing_number":"021000021","account_number":"000123456789","account_type":"checking"}'::jsonb WHERE id='${vendorId}'`
		);

		const createResp = await page.request.post(`${API_BASE}/api/payments/runs`, {
			headers: h,
			data: { items: [{ invoice_id: target.id, method: 'ach' }] }
		});
		expect(createResp.status()).toBe(201);
		const runId = ((await createResp.json()) as { id: string }).id;

		try {
			await page.goto('/payments?tab=runs');
			await page
				.getByRole('button', { name: `View payment run ${runId.slice(0, 8)}` })
				.click();
			const modal = runDialog(page);
			await expect(modal.locator('.status-badge')).toHaveText('Draft');

			// 1. Export: the browser receives a NACHA file and the run is claimed.
			const download = page.waitForEvent('download');
			await modal.getByRole('button', { name: 'Download bank file (NACHA)' }).click();
			const file = await download;
			expect(file.suggestedFilename()).toMatch(
				new RegExp(`^payment-run-${runId.slice(0, 8)}-\\d{4}-\\d{2}-\\d{2}\\.ach$`)
			);
			await expect(modal.locator('.status-badge')).toHaveText('Exported');
			await expect(modal.getByTestId('run-exported-note')).toBeVisible();
			// Claimed: neither send path nor cancel is offered.
			await expect(modal.getByRole('button', { name: /^Execute/ })).toHaveCount(0);
			await expect(modal.getByRole('button', { name: 'Cancel run' })).toHaveCount(0);

			// A second file without asking for one explicitly is refused.
			const again = await page.request.get(`${API_BASE}/api/payments/runs/${runId}/nacha`, {
				headers: h
			});
			expect(again.status()).toBe(409);
			expect(((await again.json()) as { detail: { code: string } }).detail.code).toBe(
				'nacha_already_exported'
			);

			// 2. Regenerate is armed: the first click only warns.
			await modal.getByRole('button', { name: 'Regenerate file' }).click();
			await expect(modal.getByTestId('nacha-regenerate-warning')).toBeVisible();
			await expect(modal.getByRole('button', { name: 'Confirm regenerate' })).toBeVisible();

			// 3. The bank rejected the file: release the run back to draft.
			await modal.getByRole('button', { name: 'Bank rejected the file' }).click();
			await expect(modal.getByTestId('nacha-reject-confirm')).toBeDisabled();
			await modal.getByTestId('nacha-reject-reason').fill('Bank returned the file: wrong company ID');
			const voided = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === `/api/payments/runs/${runId}/nacha/void` &&
					r.request().method() === 'POST'
			);
			await modal.getByTestId('nacha-reject-confirm').click();
			expect((await voided).status()).toBe(200);
			await expect(modal.locator('.status-badge')).toHaveText('Draft');

			// 4. Export again, then record the run as paid with the bank's reference.
			const secondDownload = page.waitForEvent('download');
			await modal.getByRole('button', { name: 'Download bank file (NACHA)' }).click();
			await secondDownload;
			await expect(modal.locator('.status-badge')).toHaveText('Exported');

			await modal.getByRole('button', { name: 'Record run as paid…' }).click();
			await modal.getByTestId('run-record-reference').fill(`ACH-${runId.slice(0, 8)}`);
			const recorded = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === `/api/payments/runs/${runId}/record-outside` &&
					r.request().method() === 'POST'
			);
			await modal.getByTestId('run-record-confirm').click();
			expect((await recorded).status()).toBe(200);
			await expect(modal.locator('.status-badge')).toHaveText('Completed');
			expect(await getInvoiceStatus(page, target.id)).toBe('paid');
		} finally {
			deletePaymentRun(runId);
			resetInvoice(target.id, sourceStatus);
			tenantPsql(
				savedBank === 'NULL'
					? `UPDATE vendors SET bank_details=NULL WHERE id='${vendorId}'`
					: `UPDATE vendors SET bank_details='${savedBank.replace(/'/g, "''")}'::jsonb WHERE id='${vendorId}'`
			);
		}
	});
});
