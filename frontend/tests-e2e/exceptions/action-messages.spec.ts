import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';
import { bulkResolveDialog, resolveDialog } from './dialogs';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` — what the queue SAYS after an action (GitHub #443).
 *
 * Two defects shared this surface:
 *
 *  - The toasts conjugated the verb in a template literal — `Exception
 *    ${action}d`, `${n} ${action}d, ${k} skipped` — English morphology no
 *    catalogue key can carry, which also misspelled "dismissd". They are now
 *    one catalogue sentence per outcome, and a dismissal with no note stores no
 *    note rather than the invented `dismissd by user`.
 *  - Errors went through a hand-rolled `extractError` reading an `e.detail`
 *    no `ApiError` carries, with an English fallback. The refusal a
 *    segregation-of-duties 403 carries is a money-path instruction to the
 *    operator, and a 422's `detail` is a LIST; both must reach the toast as
 *    readable text. They do through `api.ts`'s `formatApiDetail`, which these
 *    specs pin end to end.
 *
 * Everything is stubbed so each response shape is exact.
 */

const ROW_ID = '00000000-0000-4000-9400-000000000001';

function exceptionRow(n: number) {
	return {
		id: `00000000-0000-4000-9400-${String(n).padStart(12, '0')}`,
		invoice_id: null,
		invoice_number: `E2E-MSG-${n}`,
		vendor_name: 'Message Vendor',
		amount: 100,
		currency: 'USD',
		exception_type: 'fraud_flag',
		type_label: 'Fraud Flag',
		severity: 'error',
		description: 'stubbed',
		status: 'open',
		resolution: null,
		resolved_by: null,
		resolved_at: null,
		assigned_to: null,
		assigned_to_user_id: null,
		due_at: null,
		is_overdue: false,
		time_to_resolution_hours: null,
		created_at: '2026-01-01T00:00:00Z'
	};
}

async function stubQueue(page: Page) {
	await page.route('**/api/exceptions/summary*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(
				exceptionSummary({ open: 3, by_type: { fraud_flag: 3 }, by_severity: { error: 3 } })
			)
		})
	);
	await page.route('**/api/exceptions?*', async (route) => {
		const url = new URL(route.request().url());
		if (url.pathname !== '/api/exceptions') {
			await route.fallback();
			return;
		}
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				items: [exceptionRow(1), exceptionRow(2), exceptionRow(3)],
				total: 3,
				page: 1,
				page_size: 20
			})
		});
	});
}

/** Answer every single-row resolve with `status` + `body`, recording what was posted. */
async function stubResolve(page: Page, status: number, body: unknown) {
	const posted: { action: string; resolution: string }[] = [];
	await page.route('**/api/exceptions/*/resolve', async (route) => {
		posted.push(route.request().postDataJSON());
		await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
	});
	return posted;
}

async function openResolve(page: Page) {
	await page.goto('/exceptions');
	const row = page.getByRole('row').filter({ hasText: 'E2E-MSG-1' });
	await row.getByRole('button', { name: 'Resolve' }).click();
	const modal = resolveDialog(page);
	await expect(modal).toBeVisible();
	return modal;
}

test.describe('/exceptions — action toasts and error text', () => {
	test.beforeEach(async ({ page }) => {
		await stubQueue(page);
	});

	test('a list-shaped 422 renders as field: message, never [object Object]', async ({ page }) => {
		await stubResolve(page, 422, {
			detail: [
				{ loc: ['body', 'resolution'], msg: 'Field required', type: 'missing' },
				{ loc: ['body', 'action'], msg: 'Input should be a valid string', type: 'string_type' }
			]
		});
		const modal = await openResolve(page);
		await modal.locator('input[type="text"]').fill('e2e: 422 shape');
		await modal.getByRole('button', { name: 'Resolve', exact: true }).click();

		const toast = page.locator('.toast-text');
		await expect(toast).toHaveText(
			'resolution: Field required; action: Input should be a valid string'
		);
		await expect(toast).not.toContainText('[object Object]');
		// A refused action leaves the dialog open with the operator's note intact.
		await expect(modal).toBeVisible();
	});

	test('a segregation-of-duties refusal reaches the operator verbatim', async ({ page }) => {
		// The sentence `exception_lifecycle.REFUSAL_MESSAGES` returns for an
		// implicated actor — it names the way out (escalate, or another user),
		// which is the whole point of showing it rather than "Action failed".
		const refusal =
			'Segregation of duties: a user involved in creating this invoice cannot also clear ' +
			'an exception that blocks its payment. Escalate it, or ask a different user to decide.';
		await stubResolve(page, 403, { detail: refusal });
		const modal = await openResolve(page);
		await modal.locator('input[type="text"]').fill('e2e: my own invoice');
		await modal.getByRole('button', { name: 'Resolve', exact: true }).click();

		await expect(page.locator('.toast-text')).toHaveText(refusal);
	});

	test('each action gets its own sentence, and a note-less dismissal stores no note', async ({
		page
	}) => {
		const posted = await stubResolve(page, 200, { id: ROW_ID, status: 'dismissed' });
		const modal = await openResolve(page);

		const done = page.waitForResponse((r) => r.url().includes('/resolve'));
		await modal.getByRole('button', { name: 'Dismiss' }).click();
		await done;

		await expect(page.locator('.toast-text')).toHaveText('Exception dismissed');
		// The old page invented `dismissd by user` and stored it as the note.
		expect(posted).toEqual([{ resolution: '', action: 'dismiss' }]);
	});

	test('escalating says so, in a sentence of its own', async ({ page }) => {
		await stubResolve(page, 200, { id: ROW_ID, status: 'escalated' });
		const modal = await openResolve(page);
		await modal.locator('input[type="text"]').fill('e2e: needs the CFO');
		await modal.getByRole('button', { name: 'Escalate' }).click();

		await expect(page.locator('.toast-text')).toHaveText('Exception escalated');
	});

	test('a bulk outcome is one pluralised sentence, and a segregation refusal is named', async ({
		page
	}) => {
		let posted: { resolution: string } | null = null;
		await page.route('**/api/exceptions/bulk/resolve', async (route) => {
			posted = route.request().postDataJSON();
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					updated: 1,
					skipped: [
						{ id: exceptionRow(2).id, reason: 'already_resolved' },
						{ id: exceptionRow(3).id, reason: 'segregation_implicated' }
					]
				})
			});
		});

		await page.goto('/exceptions');
		await expect(page.getByText('E2E-MSG-1')).toBeVisible();
		await page.getByLabel('Select all selectable exceptions').check();
		await page.getByRole('button', { name: 'Resolve 3' }).click();

		const modal = bulkResolveDialog(page);
		await expect(modal).toBeVisible();
		await expect(modal).toHaveAccessibleName(/\S/);
		await modal.getByRole('button', { name: 'Dismiss' }).click();

		await expect(page.locator('.toast-text')).toHaveText(
			'1 exception dismissed, 2 skipped. 1 row was left open: segregation of duties — ' +
				'you are recorded as involved in that payable, or in raising its flag. ' +
				'Escalate it, or ask a different user to decide.'
		);
		// No invented `bulk dismiss` note either.
		expect(posted).toMatchObject({ resolution: '' });
	});
});
