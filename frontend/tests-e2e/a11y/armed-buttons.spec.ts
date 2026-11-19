import type { Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	deletePaymentRunsWhere,
	expect,
	firstPayableQueueInvoice,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';
import { expectNoA11yViolations } from './axe-helper';

/**
 * axe, in both themes, on the two places the shared button classes meet
 * their hardest pairing: the payment-run footer with a commit ARMED (white
 * on the `--danger-strong` fill, the red ring, beside the outlined
 * `.btn-danger` and `.btn-cancel`) and the expense-report detail header
 * (`.btn-secondary` + `.btn-primary` + `.btn-danger` in one row).
 *
 * `tokenPairing.test.ts` measures each token pair per theme on paper; this
 * measures what the cascade composes, which is where an `opacity` or a
 * scoped override would show up. The OS is emulated as the OTHER theme, so a
 * pass cannot come from System happening to resolve to the one asked for.
 */

const THEMES = ['dark', 'light'] as const;
type Theme = (typeof THEMES)[number];

async function useTheme(page: Page, theme: Theme): Promise<void> {
	await page.emulateMedia({ colorScheme: theme === 'dark' ? 'light' : 'dark' });
	await page.addInitScript((value) => {
		try {
			localStorage.setItem('feoh_theme', value);
		} catch {
			// about:blank — ignore
		}
	}, theme);
}

async function patchOrg(page: Page, partial: object): Promise<void> {
	const resp = await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: { settings: partial }
	});
	expect(resp.status(), `PATCH /api/organization: ${await resp.text()}`).toBe(200);
}

test.describe('armed run footer — axe (WCAG 2.2 AA)', () => {
	test.beforeEach(async ({ page }) => {
		// One admin both creates and executes the run, as in
		// payments/execute.spec.ts; SoD itself is covered by run-cfo-signoff.
		await page.goto('/payments');
		await patchOrg(page, { payments: { require_run_segregation: false } });
	});

	test.afterEach(async ({ page, tenantAdmin }) => {
		await signInAndWait(page, tenantAdmin);
		await patchOrg(page, { payments: { require_run_segregation: true } });
	});

	for (const theme of THEMES) {
		test(`armed Execute, then armed Cancel run, in ${theme}`, async ({ page }) => {
			await useTheme(page, theme);
			const target = await firstPayableQueueInvoice(page);
			const resp = await page.request.post(`${API_BASE}/api/payments/runs`, {
				headers: await authedTenantHeaders(page),
				data: { items: [{ invoice_id: target.id, method: 'ach' }] }
			});
			expect(resp.status(), `POST /api/payments/runs: ${await resp.text()}`).toBe(201);
			const runId = ((await resp.json()) as { id: string }).id;

			try {
				await page.goto('/payments');
				await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
				await page.locator('.tab', { hasText: 'Runs' }).click();
				await page
					.getByRole('button', { name: `View payment run ${runId.slice(0, 8)}` })
					.click();
				const modal = page.locator('div.modal[role="dialog"][aria-label="Payment run"]');
				await expect(modal).toBeVisible();

				await modal.getByRole('button', { name: /^Execute/ }).click();
				const confirmExecute = modal.getByRole('button', { name: /^Confirm execute/ });
				await expect(confirmExecute).toBeVisible();
				await expect(confirmExecute).toHaveClass(/\barmed\b/);
				await expectNoA11yViolations(page);

				// The armed state moves to Cancel run: the other armed fill.
				await modal.getByRole('button', { name: 'Cancel run' }).click();
				const confirmCancel = modal.getByRole('button', { name: 'Confirm cancel' });
				await expect(confirmCancel).toHaveClass(/\barmed\b/);
				await expectNoA11yViolations(page);
			} finally {
				deletePaymentRunsWhere(`id='${runId}'`);
				tenantPsql(`UPDATE invoices SET status='${target.status}' WHERE id='${target.id}'`);
			}
		});
	}
});

test.describe('expense-report header — axe (WCAG 2.2 AA)', () => {
	for (const theme of THEMES) {
		test(`the report detail header in ${theme}`, async ({ page }) => {
			await useTheme(page, theme);
			await page.goto('/expenses');
			const reportNumber = `E2E-A11Y-RPT-${theme}-${Date.now()}`;
			const resp = await page.request.post(`${API_BASE}/api/expense-reports`, {
				headers: await authedTenantHeaders(page),
				data: { report_number: reportNumber, title: 'E2E a11y header', currency: 'USD' }
			});
			expect(resp.status(), `POST /api/expense-reports: ${await resp.text()}`).toBe(201);
			const reportId = ((await resp.json()) as { id: string }).id;

			try {
				await page.goto('/expenses?tab=reports');
				await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
				await page.getByRole('button', { name: `Open report ${reportNumber}` }).click();
				const actions = page.locator('.report-detail-actions');
				await expect(actions.getByRole('button', { name: 'Export CSV' })).toBeVisible();
				await expectNoA11yViolations(page);
			} finally {
				tenantPsql(`DELETE FROM expense_reports WHERE id='${reportId}'`);
			}
		});
	}
});
