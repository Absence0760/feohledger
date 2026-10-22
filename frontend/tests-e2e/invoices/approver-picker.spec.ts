import type { Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * `/invoices` — a non-admin must never be dead-ended by the approver picker.
 *
 * Regression: the picker sourced its options from `GET /api/admin/users`, which
 * is `require_roles(ROLE_ADMIN)`. For every non-admin that call 403'd, nothing
 * caught the rejection, `adminStore.users` stayed `[]`, the `<select>` rendered
 * only its placeholder — and Submit was `disabled={… || (needsApproverSelect &&
 * !selectedApproverId)}`, i.e. disabled forever with nothing selectable.
 *
 * The seeded workflow's approval step is `approver_strategy: "manual"`
 * (`backend/scripts/seed.py`), so `needsApproverSelect` is true for every `new`
 * invoice — and `POST /api/invoices/{id}/complete` allows admin, ap_manager AND
 * cfo. An ap_manager could therefore submit via the API but not via the UI.
 *
 * The durable fix is a non-admin-readable reviewer list
 * (`GET /api/invoices/assignable-reviewers`); the fix that makes the dead end
 * impossible *by construction* is that Submit only waits on the picker when the
 * picker actually has someone to pick.
 *
 * That endpoint is now the ONLY source — the transitional admin-only fallback
 * onto `GET /api/admin/users` is deleted, so these specs also assert the modal
 * never calls it. The endpoint gates on what `POST /invoices/{id}/assign` gates
 * on, which means a CFO gets a 403 from it too; the submit-unassigned path is
 * therefore load-bearing for a whole role, not just a failure cushion.
 *
 * What "submit unassigned" means is checked against the backend, not assumed:
 * `complete_invoice` transitions to `ready_for_review` with no assignee, and an
 * unassigned invoice is reviewable by any approver (`canReview` in the modal
 * gates on `!invoice.assigned_to_id`).
 */

const SUBMIT = 'Submit for Review';

async function createNewInvoice(page: Page, invoiceNumber: string): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			vendor: 'E2E Approver Picker Vendor',
			invoice_number: invoiceNumber,
			amount: '42.50',
			currency: 'USD'
		}
	});
	expect(res.ok()).toBeTruthy();
	return ((await res.json()) as { id: string }).id;
}

/**
 * Which approver affordance the modal is going to render for THIS tenant.
 *
 * `InvoiceModal.svelte` decides with two deriveds:
 *   needsApproverSelect = status 'new' && activeSteps.approval &&
 *                         approval_config.approver_strategy === 'manual'
 *   reviewerOptions     = assignable reviewers, minus the signed-in user
 *
 * so there are three outcomes, not two — and the third ("this workflow picks
 * its own approver") renders neither a picker nor a note. Re-derive them here
 * from the same endpoints, BEFORE the modal opens, so the assertions below are
 * a statement about what must happen rather than a guess about what did.
 */
async function expectedApproverWorld(page: Page): Promise<'picker' | 'note' | 'not-required'> {
	const headers = await authedTenantHeaders(page);
	const get = async <T>(path: string): Promise<T> => {
		const res = await page.request.get(`${API_BASE}${path}`, { headers });
		expect(res.ok(), `${path} must be readable by an ap_manager`).toBeTruthy();
		return (await res.json()) as T;
	};

	const steps = await get<{
		approval?: boolean;
		approval_config?: { approver_strategy?: string } | null;
	}>('/api/workflows/active/steps');
	if (!steps.approval || steps.approval_config?.approver_strategy !== 'manual') {
		return 'not-required';
	}

	const me = await get<{ id: string }>('/api/auth/me');
	const reviewers = await get<{ id: string; is_active: boolean }[]>(
		'/api/invoices/assignable-reviewers'
	);
	return reviewers.some((r) => r.is_active && r.id !== me.id) ? 'picker' : 'note';
}

/**
 * Record every `GET /api/admin/users` the page makes.
 *
 * The picker's admin-only fallback is deleted, and "deleted" is only provable
 * negatively: the modal must reach the admin directory on NO path, including
 * the one where its own reviewer endpoint just failed. Attach before
 * navigating.
 */
function watchAdminUsersCalls(page: Page): string[] {
	const calls: string[] = [];
	page.on('request', (req) => {
		if (new URL(req.url()).pathname === '/api/admin/users') calls.push(req.url());
	});
	return calls;
}

/** Search the list down to one invoice number and open its detail modal. */
async function openInvoice(page: Page, invoiceNumber: string) {
	const listed = page.waitForResponse(
		(r) =>
			r.url().includes('/api/invoices?') &&
			r.url().includes(`search=${encodeURIComponent(invoiceNumber)}`) &&
			r.request().method() === 'GET'
	);
	await page.getByPlaceholder('Search invoices...').fill(invoiceNumber);
	await listed;

	const row = page.locator('table tbody tr', { hasText: invoiceNumber }).first();
	await expect(row).toBeVisible();
	await row.click();
	await expect(page.getByRole('button', { name: SUBMIT })).toBeVisible();
}

function cleanUp(id: string | null) {
	if (!id) return;
	tenantPsql(
		`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id='${id}')`
	);
	tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${id}'`);
	tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${id}'`);
	deleteInvoicesWhere(`id='${id}'`);
}

test.describe('/invoices approver picker — non-admin', () => {
	let invoiceId: string | null = null;
	test.afterEach(() => {
		cleanUp(invoiceId);
		invoiceId = null;
	});

	test('an ap_manager can always reach a submittable state', async ({ page, tenantManager }) => {
		await signInAndWait(page, tenantManager);
		const number = `E2E-APPROVER-${Date.now()}`;
		invoiceId = await createNewInvoice(page, number);

		// Decide WHICH world this tenant is in before opening the modal, from the
		// same two endpoints the modal reads. Probing the rendered picker instead
		// (`picker.isVisible()`) can't work: that check does not retry, the picker
		// only exists once `GET /assignable-reviewers` has resolved, and the modal
		// paints its Submit button long before that — so a tenant that HAS
		// reviewers reads as "no picker" and the spec then waits forever for a
		// note the app is right not to render.
		const world = await expectedApproverWorld(page);

		await page.goto('/invoices');
		await openInvoice(page, number);

		const submit = page.getByRole('button', { name: SUBMIT });
		const picker = page.getByLabel('Assign approver');
		const note = page.locator('.approver-note');

		// Three legitimate worlds, one invariant: the manager is never stuck.
		if (world === 'picker') {
			// Reviewers exist: pick one. Assert the picker FIRST — Submit is only
			// disabled once the list has landed, and `approverRequired` is derived
			// from the very same state that renders the `<select>`.
			await expect(picker).toBeVisible();
			await expect(submit).toBeDisabled();
			// The placeholder plus at least one real reviewer.
			expect(await picker.locator('option').count()).toBeGreaterThan(1);
			await picker.selectOption({ index: 1 });
			await expect(submit).toBeEnabled();
		} else if (world === 'note') {
			// The approval step wants an approver but nobody can be offered.
			// Submit must stand on its own with the consequence spelled out.
			await expect(note).toBeVisible();
			await expect(picker).toHaveCount(0);
			await expect(submit).toBeEnabled();
		} else {
			// This tenant's approval step assigns its own approver (`specific` /
			// `auto`), so there is nothing to pick and nothing to explain. The
			// modal renders neither control — which means the ONLY way to show the
			// manager isn't stuck is to actually submit and watch it move.
			await expect(picker).toHaveCount(0);
			await expect(note).toHaveCount(0);
			await expect(submit).toBeEnabled();
			await submit.click();
			await expect
				.poll(async () => {
					const res = await page.request.get(`${API_BASE}/api/invoices/${invoiceId}`, {
						headers: await authedTenantHeaders(page)
					});
					if (!res.ok()) return null;
					return ((await res.json()) as { status: string }).status;
				})
				.not.toBe('new');
		}
	});

	test('a failed reviewer lookup explains itself and leaves Submit usable', async ({
		page,
		tenantManager
	}) => {
		await signInAndWait(page, tenantManager);
		const number = `E2E-APPROVER-FAIL-${Date.now()}`;
		invoiceId = await createNewInvoice(page, number);

		// Force the failure deterministically on the one source there is.
		const adminUsersCalls = watchAdminUsersCalls(page);
		await page.route(
			(url) => url.pathname === '/api/invoices/assignable-reviewers',
			(route) =>
				route.fulfill({
					status: 403,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'Insufficient permissions' })
				})
		);

		await page.goto('/invoices');
		await openInvoice(page, number);

		// No picker, an explanation, and a Submit that works — the whole point.
		await expect(page.getByLabel('Assign approver')).toHaveCount(0);
		await expect(page.locator('.approver-note')).toBeVisible();
		await expect(page.getByRole('button', { name: SUBMIT })).toBeEnabled();
		// …and it got there without the admin-only endpoint, which would have
		// 403'd for this manager anyway.
		expect(adminUsersCalls).toEqual([]);
	});

	test('submitting without an approver lands the invoice in the review queue', async ({
		page,
		tenantManager
	}) => {
		await signInAndWait(page, tenantManager);
		const number = `E2E-APPROVER-SUBMIT-${Date.now()}`;
		invoiceId = await createNewInvoice(page, number);

		await page.route(
			(url) => url.pathname === '/api/invoices/assignable-reviewers',
			(route) =>
				route.fulfill({
					status: 403,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'Insufficient permissions' })
				})
		);

		await page.goto('/invoices');
		await openInvoice(page, number);
		await page.getByRole('button', { name: SUBMIT }).click();

		// The note promises the review queue with no assignee — verify the claim
		// against the server rather than the toast.
		await expect
			.poll(
				async () => {
					const res = await page.request.get(`${API_BASE}/api/invoices/${invoiceId}`, {
						headers: await authedTenantHeaders(page)
					});
					if (!res.ok()) return null;
					const inv = (await res.json()) as {
						status: string;
						assigned_to_id: string | null;
					};
					return inv.status;
				},
				{ timeout: 15_000 }
			)
			.toBe('ready_for_review');

		const res = await page.request.get(`${API_BASE}/api/invoices/${invoiceId}`, {
			headers: await authedTenantHeaders(page)
		});
		const inv = (await res.json()) as { assigned_to_id: string | null };
		expect(inv.assigned_to_id).toBeFalsy();
	});
});

test.describe('/invoices approver picker — admin', () => {
	let invoiceId: string | null = null;
	test.afterEach(() => {
		cleanUp(invoiceId);
		invoiceId = null;
	});

	test('a disabled Submit says it is waiting for an approver, in view on open', async ({
		page
	}) => {
		// Production report: an admin could not see WHY Submit for Review was
		// disabled — and, on a short screen, could not see Submit at all
		// without scrolling past the whole form. The footer now sits under the
		// scroll region and names the one fixable reason beside the button.
		await page.setViewportSize({ width: 1280, height: 720 });
		const number = `E2E-APPROVER-HINT-${Date.now()}`;
		invoiceId = await createNewInvoice(page, number);

		// The hint belongs to the picker world: a manual approval step with
		// someone to pick. The step is the seeded default — assert it rather than
		// assume it — and pin the reviewer list so the world cannot drift with
		// whichever users the tenant happens to hold.
		const steps = await page.request.get(`${API_BASE}/api/workflows/active/steps`, {
			headers: await authedTenantHeaders(page)
		});
		expect(steps.ok()).toBeTruthy();
		const stepsBody = (await steps.json()) as {
			approval?: boolean;
			approval_config?: { approver_strategy?: string } | null;
		};
		expect(
			stepsBody.approval && stepsBody.approval_config?.approver_strategy,
			'the seeded workflow approval step is manual'
		).toBe('manual');
		await page.route(
			(url) => url.pathname === '/api/invoices/assignable-reviewers',
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify([
						{
							id: '00000000-0000-4000-8000-00000000e2e1',
							full_name: 'E2E Hint Reviewer',
							is_active: true
						}
					])
				})
		);

		await page.goto('/invoices');
		await openInvoice(page, number);

		const modal = page.locator('div.modal[role="dialog"]');
		const submit = modal.getByRole('button', { name: SUBMIT });
		const picker = modal.getByLabel('Assign approver');
		const hint = modal.getByTestId('approver-required-hint');

		await expect(picker).toBeVisible();
		await expect(submit).toBeDisabled();
		await expect(hint).toHaveText('Pick an approver to submit');
		await expect(submit).toHaveAttribute('aria-describedby', 'approver-required-hint');
		await expect(submit).toHaveAccessibleDescription('Pick an approver to submit');

		// All of it on screen without scrolling.
		await expect(submit).toBeInViewport({ ratio: 1 });
		await expect(picker).toBeInViewport({ ratio: 1 });
		await expect(hint).toBeInViewport();
		await expect(modal.getByRole('button', { name: 'Save', exact: true })).toBeInViewport({
			ratio: 1
		});
		expect(await modal.getByTestId('invoice-form-scroll').evaluate((el) => el.scrollTop)).toBe(0);

		// Picking someone answers the hint: it goes, and so does the description.
		await picker.selectOption({ label: 'E2E Hint Reviewer' });
		await expect(submit).toBeEnabled();
		await expect(hint).toHaveCount(0);
		await expect(submit).not.toHaveAttribute('aria-describedby');
	});

	test('an admin degrades to submit-unassigned too — no admin-users fallback', async ({
		page
	}) => {
		const number = `E2E-APPROVER-ADMIN-${Date.now()}`;
		invoiceId = await createNewInvoice(page, number);

		// An admin is the one role that CAN read `GET /api/admin/users`, so it is
		// the only role for which a surviving fallback would still be invisible.
		// With the reviewer endpoint down the admin must land where a manager
		// does: no picker, the note, a usable Submit — and no call to the admin
		// directory.
		const adminUsersCalls = watchAdminUsersCalls(page);
		await page.route(
			(url) => url.pathname === '/api/invoices/assignable-reviewers',
			(route) =>
				route.fulfill({
					status: 404,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'Not Found' })
				})
		);

		await page.goto('/invoices');
		await openInvoice(page, number);

		await expect(page.locator('.approver-note')).toBeVisible();
		await expect(page.getByLabel('Assign approver')).toHaveCount(0);
		await expect(page.getByRole('button', { name: SUBMIT })).toBeEnabled();
		expect(adminUsersCalls).toEqual([]);
	});
});
