import { expect, test } from '../fixtures/helpers';
import type { Page, Route } from '@playwright/test';

/**
 * `/requisitions` — every lifecycle control reads the row's server verdict
 * (`actions`, backend `services/procurement_controls.requisition_*_refusal`),
 * never a restated status or role rule (frontend/docs/ui-patterns.md § Whether
 * to offer an action).
 *
 * One refusal is explained rather than hidden: segregation of duties on
 * Approve. The reader can act on it (a colleague must approve), so Approve
 * stays on screen, disabled, with the reason above it in a `RefusalNote`
 * (rendered by `RowAction reason`) that the button names in
 * `aria-describedby`. A status refusal or a role refusal hides the control,
 * and a row whose verdict has not arrived offers nothing (fail closed).
 *
 * Stubbed, exact-pathname guarded (`**\/api/requisitions*` also matches Vite's
 * module URL for `/src/lib/api/requisitions.ts`).
 */

const LIST = '/api/requisitions';
const SUMMARY = '/api/requisitions/summary';

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

const STATUS_REFUSED = refused('requisition_status_refused', { status: 'cancelled' });
const REFUSE_ALL = Object.fromEntries(Object.keys(ALLOW_ALL).map((k) => [k, STATUS_REFUSED]));

function requisition(id: string, number: string, overrides: Record<string, unknown> = {}) {
	return {
		id,
		requisition_number: number,
		title: `Verdict fixture ${number}`,
		requester_user_id: '11111111-1111-4111-8111-111111111111',
		department: 'Ops',
		status: 'pending_approval',
		needed_by: null,
		justification: null,
		vendor_id: null,
		contract_id: null,
		budget_id: null,
		total: 250,
		currency: 'USD',
		notes: null,
		submitted_at: null,
		approved_at: null,
		approved_by: null,
		rejection_reason: null,
		converted_po_id: null,
		line_items: [],
		created_at: '2026-11-01T10:00:00+00:00',
		updated_at: '2026-11-01T10:00:00+00:00',
		...overrides
	};
}

function json(route: Route, body: unknown) {
	return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

async function stubRequisitions(page: Page, items: unknown[]) {
	await page.route('**/api/requisitions**', async (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname;
		if (req.method() === 'GET' && path === LIST) {
			return json(route, { items, total: items.length, page: 1, page_size: 100 });
		}
		if (req.method() === 'GET' && path === SUMMARY) {
			return json(route, {
				total: items.length,
				by_status: { pending_approval: items.length },
				by_currency: [{ currency: 'USD', total: '250.00', count: items.length }]
			});
		}
		return route.continue();
	});
}

function row(page: Page, number: string) {
	return page.locator('tbody tr', { hasText: number });
}

const CONTROLS = [/^Submit$/, /^Approve$/, /^Reject$/, /^Convert to PO$/, /^Reopen/, /^Cancel$/] as const;

test.describe('/requisitions — controls follow the server verdict', () => {
	test('an allowed verdict offers each control, even on a status the old rule refused', async ({
		page
	}) => {
		await stubRequisitions(page, [
			requisition('a0000000-0000-4000-8000-000000000001', 'REQ-V-0001', {
				status: 'cancelled',
				actions: ALLOW_ALL
			})
		]);
		await page.goto('/requisitions');

		const r = row(page, 'REQ-V-0001');
		await expect(r).toBeVisible({ timeout: 15_000 });
		for (const name of CONTROLS) {
			await expect(r.getByRole('button', { name })).toBeVisible();
		}
		await expect(r.getByRole('button', { name: /^Approve$/ })).toBeEnabled();
		await expect(r.locator('.refusal-note')).toHaveCount(0);
	});

	test('a segregation refusal shows Approve disabled, with the reason above it as its description', async ({
		page
	}) => {
		const id = 'a0000000-0000-4000-8000-000000000002';
		await stubRequisitions(page, [
			requisition(id, 'REQ-V-0002', {
				actions: { ...ALLOW_ALL, approve: refused('requisition_approval_segregation') }
			})
		]);
		await page.goto('/requisitions');

		const r = row(page, 'REQ-V-0002');
		const approve = r.getByRole('button', { name: /^Approve$/ });
		await expect(approve).toBeVisible({ timeout: 15_000 });
		await expect(approve).toBeDisabled();

		// The localized sentence for the code, not the server's English stub.
		const note = r.locator(`#approve-refusal-${id}`);
		await expect(note).toBeVisible();
		await expect(note).toContainText('Segregation of duties');
		// The requisition sentence, not the invoice one.
		await expect(note).toContainText('requisition');
		await expect(note).not.toContainText('invoice');
		await expect(note).not.toContainText('Refused:');
		await expect(approve).toHaveAttribute('aria-describedby', `approve-refusal-${id}`);
		await expect(approve).toHaveAccessibleDescription(/Segregation of duties/);

		// Above the buttons, not beside them.
		const noteBox = await note.boundingBox();
		const btnBox = await approve.boundingBox();
		expect(noteBox && btnBox && noteBox.y + noteBox.height <= btnBox.y + 1).toBe(true);

		// The rest of the row is unaffected.
		await expect(r.getByRole('button', { name: /^Reject$/ })).toBeEnabled();
	});

	test('a status or role refusal hides the control rather than explaining it', async ({ page }) => {
		await stubRequisitions(page, [
			requisition('a0000000-0000-4000-8000-000000000003', 'REQ-V-0003', {
				actions: { ...REFUSE_ALL, reject: refused('role_not_permitted') }
			})
		]);
		await page.goto('/requisitions');

		const r = row(page, 'REQ-V-0003');
		await expect(r).toBeVisible({ timeout: 15_000 });
		for (const name of CONTROLS) {
			await expect(r.getByRole('button', { name })).toHaveCount(0);
		}
		await expect(r.locator('.refusal-note')).toHaveCount(0);
	});

	test('a row whose verdict has not arrived offers nothing (fail closed)', async ({ page }) => {
		await stubRequisitions(page, [
			requisition('a0000000-0000-4000-8000-000000000004', 'REQ-V-0004', { status: 'draft' })
		]);
		await page.goto('/requisitions');

		const r = row(page, 'REQ-V-0004');
		await expect(r).toBeVisible({ timeout: 15_000 });
		for (const name of CONTROLS) {
			await expect(r.getByRole('button', { name })).toHaveCount(0);
		}
	});

	test('a converted row with its PO does not offer Convert again', async ({ page }) => {
		await stubRequisitions(page, [
			requisition('a0000000-0000-4000-8000-000000000005', 'REQ-V-0005', {
				status: 'converted',
				converted_po_id: '33333333-3333-4333-8333-333333333333',
				actions: { ...REFUSE_ALL, convert: null }
			})
		]);
		await page.goto('/requisitions');

		const r = row(page, 'REQ-V-0005');
		await expect(r).toBeVisible({ timeout: 15_000 });
		await expect(r.getByRole('button', { name: /^Convert to PO$/ })).toHaveCount(0);
	});
});
