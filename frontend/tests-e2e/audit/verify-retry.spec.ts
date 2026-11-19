import type {
	InvoiceSignatureReport,
	SignatureVerificationReport
} from '#lib/types/auditVerification.ts';
import type { Route } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * /audit — both signature-verification failures carry a working Retry.
 *
 * `verify-error` (the period sweep) and `verify-drill-error` (the per-invoice
 * re-check) are `ui/LoadError` blocks whose Retry re-runs the request. Each
 * must come down while its retry is out — a block that stays up reads as a
 * no-op, and a second failure would not re-announce — and must give way to
 * the result once the retry answers. Responses are answered by a `mode` the
 * test sets and the retry is HELD on a promise, so nothing is timing-dependent.
 */

const INVOICE = '33333333-3333-4333-8333-333333333333';

const PERIOD_REPORT = {
	start: '2020-01-01',
	end: '2030-01-01',
	signing_configured: true,
	approvals_checked: 1,
	invoices_covered: 1,
	valid: 0,
	invalid: 1,
	unsigned: 0,
	findings_truncated: false,
	findings: [
		{
			invoice_id: INVOICE,
			invoice_number: 'RETRY-1',
			actor_id: null,
			actor: 'retry@example.com',
			signed_at: '2024-01-01T00:00:00Z',
			audit_row_id: 'cccccccc-0000-4000-8000-000000000003',
			verdict: 'invalid'
		}
	]
} satisfies SignatureVerificationReport;

const INVOICE_REPORT = {
	invoice_id: INVOICE,
	signing_configured: true,
	approvals: [
		{
			audit_row_id: 'cccccccc-0000-4000-8000-000000000003',
			actor: 'retry@example.com',
			signed_at: '2024-03-03T00:00:00Z',
			signed: true,
			valid: false
		}
	]
} satisfies InvoiceSignatureReport;

const BOOM = { status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'boom' }) };

type Mode = 'fail' | 'hold' | 'ok';

test.describe('/audit verification Retry', () => {
	test('the period sweep: Retry clears the error while out and shows the findings', async ({
		page
	}) => {
		let mode: Mode = 'fail';
		let release: () => void = () => {};
		const held = new Promise<void>((resolve) => {
			release = resolve;
		});
		await page.route('**/api/audit/verify-signatures*', async (route: Route) => {
			if (new URL(route.request().url()).pathname !== '/api/audit/verify-signatures') {
				await route.continue();
				return;
			}
			if (mode === 'fail') {
				await route.fulfill(BOOM);
				return;
			}
			if (mode === 'hold') await held;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(PERIOD_REPORT)
			});
		});

		await page.goto('/audit');
		const runVerify = page.getByTestId('run-verification');
		await expect(runVerify).toBeVisible();
		await runVerify.click();

		const error = page.getByTestId('verify-error');
		await expect(error).toBeVisible();

		mode = 'hold';
		await error.getByRole('button', { name: 'Retry' }).click();
		await expect(error).toHaveCount(0);

		release();
		await expect(page.getByTestId('verify-finding').filter({ hasText: 'RETRY-1' })).toBeVisible();
		await expect(error).toHaveCount(0);
	});

	test('the drill-down: Retry re-checks the same invoice and shows its report', async ({
		page
	}) => {
		let mode: Mode = 'fail';
		let release: () => void = () => {};
		const held = new Promise<void>((resolve) => {
			release = resolve;
		});
		const drillPath = `/api/audit/invoice/${INVOICE}/verify-signatures`;
		await page.route('**/api/audit/**', async (route: Route) => {
			const pathname = new URL(route.request().url()).pathname;
			if (pathname === '/api/audit/verify-signatures') {
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(PERIOD_REPORT)
				});
				return;
			}
			if (pathname !== drillPath) {
				await route.continue();
				return;
			}
			if (mode === 'fail') {
				await route.fulfill(BOOM);
				return;
			}
			if (mode === 'hold') await held;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(INVOICE_REPORT)
			});
		});

		await page.goto('/audit');
		const runVerify = page.getByTestId('run-verification');
		await expect(runVerify).toBeVisible();
		await runVerify.click();

		await page
			.getByTestId('verify-finding')
			.filter({ hasText: 'RETRY-1' })
			.locator('.row-link')
			.click();
		const drill = page.getByTestId('verify-drill');
		await expect(drill).toHaveAttribute('data-invoice-id', INVOICE);
		const error = page.getByTestId('verify-drill-error');
		await expect(error).toBeVisible();

		mode = 'hold';
		const retried = page.waitForRequest((r) => new URL(r.url()).pathname === drillPath);
		await error.getByRole('button', { name: 'Retry' }).click();
		await retried;
		await expect(error).toHaveCount(0);

		release();
		await expect(drill).toHaveAttribute('data-invoice-id', INVOICE);
		await expect(drill).toContainText('retry@example.com');
		await expect(error).toHaveCount(0);
	});
});
