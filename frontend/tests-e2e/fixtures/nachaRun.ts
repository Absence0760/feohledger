import type { Locator, Page } from '@playwright/test';

import type { NachaVerifyResult } from '#lib/types/runOutside.ts';
import { API_BASE, authedTenantHeaders, expect, tenantPsql } from './helpers';

/**
 * A payment run whose NACHA bank file has been exported — the state "Verify
 * before upload" (decisions §362) and "Bank rejected the file" act on.
 *
 * Built the way `payments/record-outside.spec.ts` builds its run: the tenant is
 * switched to record-only with a bank-file originator, maker-checker is off
 * (the admin both creates and exports — a legitimate single-operator setup),
 * the payee vendor gets test bank details by direct SQL (a bank change through
 * the API is dual-control by design), and the file is downloaded from the run
 * dialog, which claims the run (`draft → exported`).
 *
 * Hermetic: `PATCH /api/organization` replaces the WHOLE `settings.payments`
 * block, so it is snapshotted first and put back afterwards, with the run,
 * the invoice's status and the vendor's bank details.
 */

export interface ExportedRun {
	runId: string;
	invoiceId: string;
	invoiceNumber: string;
	/** The open run dialog, showing the exported run. */
	modal: Locator;
}

interface QueueItem {
	id: string;
	invoice_number: string;
	blocked?: boolean;
	required_method?: string | null;
	accepted_discount_amount?: string | null;
}

const NACHA_CONFIG = {
	company_name: 'E2E CORP',
	company_id: '1123456789',
	odfi_routing: '021000021'
};

export function runDialog(page: Page): Locator {
	return page.locator('div.modal[role="dialog"][aria-label="Payment run"]');
}

async function paymentsSettings(page: Page): Promise<Record<string, unknown> | null> {
	const resp = await page.request.get(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page)
	});
	const body = (await resp.json()) as { settings: Record<string, unknown> };
	return (body.settings.payments as Record<string, unknown> | undefined) ?? null;
}

async function putPaymentsSettings(page: Page, payments: Record<string, unknown> | null) {
	const resp = await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: { settings: { payments: payments ?? {} } }
	});
	expect(resp.ok(), await resp.text()).toBe(true);
}

/**
 * Switch the tenant's payments to `mode` after the run's file is out, keeping
 * the rest of `settings.payments` (decisions §382: a run exported before a
 * switch to processor mode keeps its bank-file panel). `withExportedRun` puts
 * the original block back afterwards.
 */
export async function switchPaymentsMode(page: Page, mode: 'record_only' | 'processor') {
	const current = await paymentsSettings(page);
	await putPaymentsSettings(page, {
		...(current ?? {}),
		mode,
		...(mode === 'processor' ? { provider: 'mock' } : {})
	});
}

/** A queue row the run path accepts: not blocked, not pinned to a live card,
 *  and no accepted discount (the teardown below does not unwind one). */
async function plainQueueItem(page: Page): Promise<QueueItem> {
	const resp = await page.request.get(`${API_BASE}/api/payments/queue?page=1&page_size=100`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.ok()).toBe(true);
	const target = ((await resp.json()) as { items: QueueItem[] }).items.find(
		(q) => !q.blocked && !q.required_method && !q.accepted_discount_amount
	);
	expect(target, 'the seed leaves at least one plain payable invoice').toBeDefined();
	return target!;
}

/** Export a run's bank file, hand the open dialog to `body`, then put every
 *  row and setting back. */
export async function withExportedRun(
	page: Page,
	body: (run: ExportedRun) => Promise<void>
): Promise<void> {
	const savedPayments = await paymentsSettings(page);
	await putPaymentsSettings(page, {
		...(savedPayments ?? {}),
		require_run_segregation: false,
		mode: 'record_only',
		nacha: NACHA_CONFIG
	});
	const target = await plainQueueItem(page);
	const sourceStatus = tenantPsql(`SELECT status FROM invoices WHERE id='${target.id}'`).trim();
	const vendorId = tenantPsql(`SELECT vendor_id FROM invoices WHERE id='${target.id}'`).trim();
	expect(vendorId, 'the target invoice has a linked vendor').not.toBe('');
	const savedBank = tenantPsql(
		`SELECT COALESCE(bank_details::text, 'NULL') FROM vendors WHERE id='${vendorId}'`
	).trim();
	tenantPsql(
		`UPDATE vendors SET bank_details='{"routing_number":"021000021","account_number":"000123456789","account_type":"checking"}'::jsonb WHERE id='${vendorId}'`
	);

	let runId = '';
	try {
		const created = await page.request.post(`${API_BASE}/api/payments/runs`, {
			headers: await authedTenantHeaders(page),
			data: { items: [{ invoice_id: target.id, method: 'ach' }] }
		});
		expect(created.status()).toBe(201);
		runId = ((await created.json()) as { id: string }).id;

		await page.goto('/payments?tab=runs');
		await page.getByRole('button', { name: `View payment run ${runId.slice(0, 8)}` }).click();
		const modal = runDialog(page);
		await expect(modal.locator('.status-badge')).toHaveText('Draft');
		const download = page.waitForEvent('download');
		await modal.getByRole('button', { name: 'Download bank file (NACHA)' }).click();
		await download;
		await expect(modal.locator('.status-badge')).toHaveText('Exported');
		await expect(modal.getByTestId('nacha-verify')).toBeEnabled();

		await body({ runId, invoiceId: target.id, invoiceNumber: target.invoice_number, modal });
	} finally {
		if (runId) {
			tenantPsql(`DELETE FROM payments WHERE payment_run_id='${runId}'`);
			tenantPsql(`DELETE FROM payment_runs WHERE id='${runId}'`);
		}
		tenantPsql(`UPDATE invoices SET status='${sourceStatus}' WHERE id='${target.id}'`);
		tenantPsql(
			savedBank === 'NULL'
				? `UPDATE vendors SET bank_details=NULL WHERE id='${vendorId}'`
				: `UPDATE vendors SET bank_details='${savedBank.replace(/'/g, "''")}'::jsonb WHERE id='${vendorId}'`
		);
		await putPaymentsSettings(page, savedPayments);
	}
}

/**
 * Answer the dialog's next `POST /runs/{id}/nacha/verify` with `result`.
 *
 * A superseded or held answer needs an ERP that reports a bill paid, or one
 * that cannot be read, at the moment of the check; no local ERP does that on
 * cue. What the dialog does with each answer is the subject here. What the
 * server decides, and the payment it takes out, is pinned by the backend's
 * `test_nacha.py`. `satisfies` keeps the stub honest against the type the
 * dialog reads (`tsconfig.e2e.json`).
 */
export async function routeVerify(
	page: Page,
	runId: string,
	answer: { status: number; json: NachaVerifyResult | { detail: unknown } }
): Promise<void> {
	await page.unroute(`**/api/payments/runs/${runId}/nacha/verify`);
	await page.route(`**/api/payments/runs/${runId}/nacha/verify`, (route) =>
		route.fulfill({ status: answer.status, json: answer.json })
	);
}

/** A verify answer for the run's one payment. */
export function verifyAnswer(
	run: ExportedRun,
	result: NachaVerifyResult['result'],
	{ remaining = 1 }: { remaining?: number } = {}
): NachaVerifyResult {
	const item = {
		payment_id: '00000000-0000-4000-8000-000000000001',
		invoice_id: run.invoiceId,
		invoice_number: run.invoiceNumber,
		reason: result === 'held' ? 'erp_bill_unverified' : 'erp_bill_not_open:paid'
	};
	return {
		id: run.runId,
		status: result === 'superseded' && remaining === 0 ? 'cancelled' : 'exported',
		result,
		checked: 1,
		remaining,
		removed: result === 'superseded' ? [item] : [],
		held: result === 'held' ? [item] : [],
		erp_checked: true
	} satisfies NachaVerifyResult;
}
