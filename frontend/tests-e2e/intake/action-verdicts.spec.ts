import { expect, test } from '../fixtures/helpers';
import type { Page, Route } from '@playwright/test';

/**
 * `/intake` — every lifecycle control reads the row's server verdict
 * (`actions`, backend `api/intake.intake_action_refusals`), never a restated
 * status or role rule (frontend/docs/ui-patterns.md § Whether to offer an
 * action).
 *
 * Every refusal those gates give is about the row's status or the reader's
 * role, so the page explains none of them: a refused control is HIDDEN, and a
 * row whose verdict has not arrived offers nothing (fail closed). The status
 * here is deliberately one that the old restated rule would have answered the
 * other way, so a page that went back to reading `status` fails.
 *
 * Stubbed: the point is a verdict the seeded tenant cannot be made to produce
 * on demand. Exact-pathname guarded — `**\/api/intake*` also matches Vite's
 * module URL for `/src/lib/api/intake.ts`.
 */

const LIST = '/api/intake';
const SUMMARY = '/api/intake/summary';

const ALLOW_ALL = {
	submit: null,
	approve: null,
	reject: null,
	convert: null,
	cancel: null,
	reopen: null
};

function refused(code: string, params: Record<string, unknown> = {}) {
	return { code, message: `Refused: ${code}`, params };
}

const REFUSE_ALL = Object.fromEntries(
	Object.keys(ALLOW_ALL).map((k) => [k, refused('intake_status_refused', { status: 'cancelled' })])
);

function intake(number: string, overrides: Record<string, unknown> = {}) {
	return {
		id: `00000000-0000-4000-8000-${number.replace(/\D/g, '').padStart(12, '0')}`,
		request_number: number,
		title: `Verdict fixture ${number}`,
		request_type: 'software',
		requester_user_id: '11111111-1111-4111-8111-111111111111',
		description: null,
		estimated_amount: 120,
		currency: 'USD',
		vendor_name: null,
		vendor_id: null,
		status: 'open',
		form_data: null,
		needed_by: null,
		justification: null,
		converted_requisition_id: null,
		converted_po_id: null,
		created_at: '2026-11-01T10:00:00+00:00',
		updated_at: '2026-11-01T10:00:00+00:00',
		...overrides
	};
}

function json(route: Route, body: unknown) {
	return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

async function stubIntake(page: Page, items: unknown[]) {
	await page.route('**/api/intake**', async (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname;
		if (req.method() === 'GET' && path === LIST) {
			return json(route, { items, total: items.length, page: 1, page_size: 50 });
		}
		if (req.method() === 'GET' && path === SUMMARY) {
			return json(route, { total: items.length, by_status: { open: items.length } });
		}
		return route.continue();
	});
}

function row(page: Page, number: string) {
	return page.locator('tbody tr', { hasText: number });
}

const CONTROLS = [
	/^Submit$/,
	/^Approve$/,
	/^Reject$/,
	/^Convert to requisition$/,
	/^Reopen/,
	/^Cancel$/
] as const;

test.describe('/intake — controls follow the server verdict', () => {
	test('an allowed verdict offers each control, even on a status the old rule refused', async ({
		page
	}) => {
		// `cancelled` offered nothing under the restated rule; the verdict says
		// every door is open, and the page must believe it.
		await stubIntake(page, [intake('IN-V-0001', { status: 'cancelled', actions: ALLOW_ALL })]);
		await page.goto('/intake');

		const r = row(page, 'IN-V-0001');
		await expect(r).toBeVisible({ timeout: 15_000 });
		for (const name of CONTROLS) {
			await expect(r.getByRole('button', { name })).toBeVisible();
		}
	});

	test('a refused verdict hides each control, even on a status the old rule allowed', async ({
		page
	}) => {
		await stubIntake(page, [intake('IN-V-0002', { status: 'open', actions: REFUSE_ALL })]);
		await page.goto('/intake');

		const r = row(page, 'IN-V-0002');
		await expect(r).toBeVisible({ timeout: 15_000 });
		for (const name of CONTROLS) {
			await expect(r.getByRole('button', { name })).toHaveCount(0);
		}
	});

	test('a role refusal hides the control rather than explaining it', async ({ page }) => {
		await stubIntake(page, [
			intake('IN-V-0003', {
				status: 'in_review',
				actions: { ...ALLOW_ALL, approve: refused('role_not_permitted') }
			})
		]);
		await page.goto('/intake');

		const r = row(page, 'IN-V-0003');
		await expect(r.getByRole('button', { name: /^Reject$/ })).toBeVisible({ timeout: 15_000 });
		await expect(r.getByRole('button', { name: /^Approve$/ })).toHaveCount(0);
		await expect(r).not.toContainText('Refused:');
	});

	test('a row whose verdict has not arrived offers nothing (fail closed)', async ({ page }) => {
		await stubIntake(page, [intake('IN-V-0004', { status: 'open' })]);
		await page.goto('/intake');

		const r = row(page, 'IN-V-0004');
		await expect(r).toBeVisible({ timeout: 15_000 });
		for (const name of CONTROLS) {
			await expect(r.getByRole('button', { name })).toHaveCount(0);
		}
	});

	test('a row that already names its requisition links to it instead of offering Convert', async ({
		page
	}) => {
		await stubIntake(page, [
			intake('IN-V-0005', {
				status: 'converted',
				converted_requisition_id: '22222222-2222-4222-8222-222222222222',
				actions: { ...REFUSE_ALL, convert: null }
			})
		]);
		await page.goto('/intake');

		const r = row(page, 'IN-V-0005');
		await expect(r.getByRole('link', { name: 'View requisition' })).toBeVisible({ timeout: 15_000 });
		await expect(r.getByRole('button', { name: /^Convert to requisition$/ })).toHaveCount(0);
	});
});
