import type { Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	deletePaymentRunsWhere,
	deleteVendorsWhere,
	expect,
	firstPayableQueueInvoice,
	flagVendorForScreeningReview,
	selectVendorInPicker,
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

/**
 * Arming follows the precondition (ui-patterns § Buttons, `.armed`): a commit
 * shows three looks — muted while it cannot be pressed (no `.armed`), the red
 * fill once it can, and the busy tint (still `.armed`, disabled) while the
 * request is in flight. The accessible name changes with it, so the armed
 * state is announced, not only painted.
 */
test.describe('armed commits follow their precondition', () => {
	const PREFIX = 'E2E-ARMED-REJECT';
	test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`));

	async function openInReview(page: Page, number: string): Promise<string> {
		const res = await page.request.post(`${API_BASE}/api/invoices`, {
			headers: await authedTenantHeaders(page),
			data: { vendor: 'E2E Armed Vendor', invoice_number: number, amount: '42.00', currency: 'USD' }
		});
		expect(res.status(), await res.text()).toBe(201);
		const id = ((await res.json()) as { id: string }).id;
		return id;
	}

	test('InvoiceModal: one lead per footer, and Confirm reject arms on a reason', async ({ page }) => {
		const number = `${PREFIX}-${Date.now()}`;
		const id = await openInReview(page, number);
		const modal = page.locator('div.modal[role="dialog"]');
		const footer = modal.getByTestId('invoice-modal-actions');

		// `new`: Submit leads, Save is secondary.
		await page.goto(`/invoices?id=${id}`);
		await expect(modal.locator('header h2')).toContainText(number);
		await expect(footer.locator('.btn-primary')).toHaveCount(1);
		await expect(footer.locator('.btn-primary')).toHaveClass(/\bbtn-submit\b/);

		// `ready_for_review`, approvable: Approve leads, Save steps back.
		tenantPsql(`UPDATE invoices SET status='ready_for_review', uploaded_by_id=NULL WHERE id='${id}'`);
		const verdict = page.waitForResponse(
			(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
		);
		await page.goto(`/invoices?id=${id}`);
		await verdict;
		const approve = footer.getByRole('button', { name: 'Approve', exact: true });
		await expect(approve).toBeEnabled();
		await expect(footer.locator('.btn-primary')).toHaveCount(1);
		await expect(footer.getByRole('button', { name: 'Save', exact: true })).toHaveClass(
			/\bbtn-secondary\b/
		);

		await footer.getByRole('button', { name: 'Reject', exact: true }).click();
		const confirm = footer.getByRole('button', { name: 'Confirm Reject' });
		// No reason yet: disabled and NOT armed (muted, not red).
		await expect(confirm).toBeDisabled();
		await expect(confirm).not.toHaveClass(/\barmed\b/);

		await footer.getByRole('textbox', { name: 'Reason for rejection' }).fill('Wrong vendor');
		await expect(confirm).toBeEnabled();
		await expect(confirm).toHaveClass(/\barmed\b/);
		await expectNoA11yViolations(page);

		// Busy: held in flight, it stays armed and disabled under its busy name.
		let release!: () => void;
		const held = new Promise<void>((r) => (release = r));
		await page.route(`**/api/invoices/${id}/reject`, async (route) => {
			await held;
			await route.fallback();
		});
		await confirm.click();
		const busy = footer.getByRole('button', { name: 'Rejecting...' });
		await expect(busy).toBeDisabled();
		await expect(busy).toHaveClass(/\barmed\b/);
		release();
	});

	test('Close workspace: muted until the slug is typed, red once it is, busy stays armed', async ({
		page
	}) => {
		const SLUG = 'armed-ws';
		let release!: () => void;
		const held = new Promise<void>((r) => (release = r));
		await page.route(
			(url) => url.pathname === '/api/organization/close',
			async (route) => {
				if (route.request().method() === 'GET') {
					return route.fulfill({
						status: 200,
						contentType: 'application/json',
						body: JSON.stringify({ closable: true, blockers: [], slug: SLUG, will_delete: {} })
					});
				}
				await held;
				return route.fulfill({
					status: 409,
					contentType: 'application/json',
					body: JSON.stringify({ detail: { code: 'workspace_not_closable', message: 'x', params: {} } })
				});
			}
		);
		await page.goto('/organization?section=close');
		const submit = page.getByTestId('close-workspace-submit');
		await expect(submit).toBeDisabled();
		await expect(submit).not.toHaveClass(/\barmed\b/);

		await page.getByLabel(`Type ${SLUG} to confirm`).fill(SLUG);
		await expect(submit).toBeEnabled();
		await expect(submit).toHaveClass(/\barmed\b/);
		await expect(submit).toHaveAccessibleName('Close workspace permanently');

		await submit.click();
		await expect(submit).toBeDisabled();
		await expect(submit).toHaveAccessibleName('Closing…');
		await expect(submit).toHaveClass(/\barmed\b/);
		release();
	});
});

/** A uniquely named vendor through the real API; callers tear it down with
 *  `deleteVendorsWhere`, which cascades its screening history and offers. */
async function makeVendor(page: Page, name: string): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: await authedTenantHeaders(page),
		data: { name }
	});
	expect(resp.status(), `POST /api/vendors: ${await resp.text()}`).toBe(201);
	return ((await resp.json()) as { id: string }).id;
}

/** Hold every request matching `url` until the returned release is called,
 *  then let it through to the real backend. */
async function holdRoute(page: Page, url: string): Promise<() => void> {
	let release!: () => void;
	const held = new Promise<void>((r) => (release = r));
	await page.route(url, async (route) => {
		await held;
		await route.fallback();
	});
	return release;
}

/**
 * The same three looks, plus axe in both themes, on the two armed commits the
 * siblings above do not reach: `/vendors/screening`'s Confirm unblock (it
 * releases payments a screen stopped) and `BulkNegotiationModal`'s Confirm —
 * propose (it covers every open invoice the vendor has). Real backend; the
 * held routes only pause a request so its busy state can be read.
 */
test.describe('armed commits follow their precondition — screening and bulk discounts', () => {
	for (const theme of THEMES) {
		test(`/vendors/screening Confirm unblock in ${theme}`, async ({ page }) => {
			await useTheme(page, theme);
			await page.goto('/vendors');
			const name = `E2E Armed Unblock ${theme} ${Date.now()}`;
			const vendorId = await makeVendor(page, name);
			try {
				flagVendorForScreeningReview(vendorId, undefined, { blocked: true });
				await page.goto('/vendors/screening');
				await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
				await page.locator('table tbody tr', { hasText: name }).locator('.row-link').click();
				const modal = page.getByRole('dialog', { name: 'Vendor screening review' });
				const actions = modal.getByTestId('screening-actions');

				// Unarmed: the plain secondary trigger.
				const unblock = actions.getByRole('button', { name: 'Unblock payments' });
				await expect(unblock).toBeEnabled();
				await expect(unblock).not.toHaveClass(/\barmed\b/);

				// Ready: armed fill under the armed name.
				await unblock.click();
				const confirm = actions.getByRole('button', { name: 'Confirm unblock' });
				await expect(confirm).toBeEnabled();
				await expect(confirm).toHaveClass(/\barmed\b/);
				await expectNoA11yViolations(page);

				// Disabled by a re-screen in flight: the commit cannot be pressed, so
				// it is disarmed — muted, under the unarmed name — not a red fill.
				const releaseScreen = await holdRoute(page, `**/api/vendors/${vendorId}/screen`);
				const screened = page.waitForResponse(
					(r) => r.request().method() === 'POST' && r.url().endsWith(`/api/vendors/${vendorId}/screen`)
				);
				await actions.getByRole('button', { name: 'Re-screen now' }).click();
				await expect(actions.getByRole('button', { name: 'Re-screening…' })).toBeDisabled();
				await expect(unblock).toBeDisabled();
				await expect(unblock).not.toHaveClass(/\barmed\b/);
				releaseScreen();
				expect((await screened).status()).toBe(200);
				await expect(unblock).toBeEnabled();

				// Busy: re-armed and committed, it stays armed while the unblock is held.
				await unblock.click();
				await expect(confirm).toHaveClass(/\barmed\b/);
				const releaseUnblock = await holdRoute(page, `**/api/vendors/${vendorId}/unblock`);
				const unblocked = page.waitForResponse(
					(r) => r.request().method() === 'POST' && r.url().endsWith(`/api/vendors/${vendorId}/unblock`)
				);
				await confirm.click();
				const busy = actions.getByRole('button', { name: 'Working…' });
				await expect(busy).toBeDisabled();
				await expect(busy).toHaveClass(/\barmed\b/);
				await expectNoA11yViolations(page);
				releaseUnblock();
				expect((await unblocked).status()).toBe(200);
				await expect(actions.getByRole('button', { name: 'Block payments' })).toBeVisible();
			} finally {
				deleteVendorsWhere(`id='${vendorId}'`);
			}
		});

		test(`BulkNegotiationModal Confirm — propose in ${theme}`, async ({ page }) => {
			await useTheme(page, theme);
			await page.goto('/discounts');
			// No open invoices: the real endpoint answers 409, so the held commit
			// creates nothing and its refusal is the end state.
			const name = `E2E Armed BulkNeg ${theme} ${Date.now()}`;
			const vendorId = await makeVendor(page, name);
			try {
				await page.goto('/discounts');
				await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
				await page.getByRole('button', { name: 'Propose vendor offer' }).click();
				const submit = page.getByTestId('bulk-negotiate-submit');

				// Disabled, precondition unmet (no vendor, no tier): not armed.
				await expect(submit).toBeDisabled();
				await expect(submit).toHaveAccessibleName('Propose offer');
				await expect(submit).not.toHaveClass(/\barmed\b/);

				await selectVendorInPicker(page.getByTestId('bulk-negotiate-vendor'), name);
				await page.getByLabel('Tier 1 — pay within days').fill('10');
				await page.getByLabel('Tier 1 — discount percent').fill('2.00');
				await expect(submit).toBeEnabled();
				await expect(submit).not.toHaveClass(/\barmed\b/);

				// Ready: the first click arms, under the name that says what commits.
				await submit.click();
				await expect(submit).toHaveAccessibleName(`Confirm — propose to ${name}`);
				await expect(submit).toHaveClass(/\barmed\b/);
				await expectNoA11yViolations(page);

				// Busy: held in flight, still armed, disabled under its busy name.
				const release = await holdRoute(page, '**/api/discounts/bulk-negotiate');
				const proposed = page.waitForResponse(
					(r) =>
						r.request().method() === 'POST' && r.url().includes('/api/discounts/bulk-negotiate')
				);
				await submit.click();
				await expect(submit).toBeDisabled();
				await expect(submit).toHaveAccessibleName('Proposing…');
				await expect(submit).toHaveClass(/\barmed\b/);
				await expectNoA11yViolations(page);
				release();
				expect((await proposed).status()).toBe(409);
				await expect(page.getByTestId('bulk-negotiate-error')).toBeVisible();
			} finally {
				deleteVendorsWhere(`id='${vendorId}'`);
			}
		});
	}
});
