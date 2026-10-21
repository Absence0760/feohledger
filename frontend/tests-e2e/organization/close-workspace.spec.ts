import type { Page, Route } from '@playwright/test';
import type { ClosurePreflight } from '#lib/types/workspaceClosure.ts';
import { expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /organization → Close workspace (`docs/decisions.md` §306).
 *
 * The closure itself is destructive and irreversible, and the e2e tenants are
 * shared by every spec on a worker, so the two routes are STUBBED here and the
 * real deletion is proven end to end in `backend/tests/test_workspace_closure.py`
 * (a throwaway tenant provisioned per test). What this spec holds is the page's
 * side of the contract: it shows the blockers instead of a control that would
 * be refused, it will not send until the slug is typed back exactly, it asks
 * for a second factor when the server says so and resends with it, and on
 * success it lands on this host's sign-in page saying what happened.
 *
 * The stubs replay the real payload shapes (`ClosurePreflight`, the coded
 * `{code, message, params}` refusal). The non-admin case is real end to end —
 * it is the page's own gate.
 */

const SLUG = 'trial-ws';
const CLOSE_PATH = '/api/organization/close';

function panel(page: Page) {
	return page.locator('section.card', {
		has: page.getByRole('heading', { name: 'Close this workspace' })
	});
}

async function stubPreflight(page: Page, body: ClosurePreflight) {
	await page.route(
		(url) => url.pathname === CLOSE_PATH,
		async (route: Route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(body)
			});
		}
	);
}

test.describe('/organization close workspace', () => {
	test('a workspace with records to keep says why and offers no close control', async ({
		page
	}) => {
		await stubPreflight(page, {
			closable: false,
			blockers: ['payments', 'other_users'],
			slug: SLUG,
			will_delete: {}
		});
		await page.goto('/organization?section=close');

		const card = panel(page);
		const blocked = card.getByTestId('close-workspace-blocked');
		await expect(blocked).toBeVisible();
		await expect(blocked.locator('[data-blocker="payments"]')).toHaveText('It holds payments.');
		await expect(blocked.locator('[data-blocker="other_users"]')).toContainText(
			'Other people have active accounts in this workspace.'
		);
		await expect(blocked).toContainText('Contact support to close it.');
		await expect(card.getByTestId('close-workspace-submit')).toHaveCount(0);
	});

	test('the slug must be typed back, a second factor is asked for, and success signs out', async ({
		page
	}) => {
		await stubPreflight(page, {
			closable: true,
			blockers: [],
			slug: SLUG,
			will_delete: { invoices: 2, vendors: 1 }
		});
		const posted: Record<string, unknown>[] = [];
		await page.route(
			(url) => url.pathname === CLOSE_PATH,
			async (route: Route) => {
				if (route.request().method() !== 'POST') return route.fallback();
				const body = route.request().postDataJSON() as Record<string, unknown>;
				posted.push(body);
				if (!body.step_up) {
					return route.fulfill({
						status: 403,
						contentType: 'application/json',
						body: JSON.stringify({
							detail: {
								code: 'sensitive_step_up_required',
								message: 'Confirm a current authenticator code or a registered passkey to continue.',
								params: {}
							}
						})
					});
				}
				return route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ closed: true })
				});
			}
		);
		await page.goto('/organization?section=close');

		const card = panel(page);
		// What will go is stated before anything is asked.
		const willDelete = card.getByTestId('close-workspace-will-delete');
		await expect(willDelete).toContainText('2 invoices');
		await expect(willDelete).toContainText('1 vendor');
		const confirm = card.getByLabel(`Type ${SLUG} to confirm`);
		const submit = card.getByTestId('close-workspace-submit');
		await expect(confirm).toBeVisible();
		await expect(submit).toBeDisabled();

		await confirm.fill('trial-w');
		await expect(submit).toBeDisabled();
		await confirm.fill(SLUG);
		await expect(submit).toBeEnabled();

		await submit.click();
		const prompt = page.getByRole('dialog', { name: 'Confirm it’s you' });
		await expect(prompt).toBeVisible();
		await prompt.getByTestId('step-up-code').fill('123456');
		await prompt.getByRole('button', { name: 'Confirm' }).click();

		await expect(page).toHaveURL(/\/login\?reason=workspace_closed$/);
		await expect(
			page.getByText('This workspace has been closed and its accounts deleted.')
		).toBeVisible();
		expect(posted).toEqual([
			{ confirm_slug: SLUG },
			{ confirm_slug: SLUG, step_up: { code: '123456' } }
		]);
	});

	test('a refusal at submit replaces the form with the blockers it names', async ({ page }) => {
		await stubPreflight(page, { closable: true, blockers: [], slug: SLUG, will_delete: {} });
		await page.route(
			(url) => url.pathname === CLOSE_PATH,
			async (route: Route) => {
				if (route.request().method() !== 'POST') return route.fallback();
				return route.fulfill({
					status: 409,
					contentType: 'application/json',
					body: JSON.stringify({
						detail: {
							code: 'workspace_close_refused',
							message: 'server English',
							params: { blockers: ['approved_invoices'] }
						}
					})
				});
			}
		);
		await page.goto('/organization?section=close');

		const card = panel(page);
		await card.getByLabel(`Type ${SLUG} to confirm`).fill(SLUG);
		await card.getByTestId('close-workspace-submit').click();

		const blocked = card.getByTestId('close-workspace-blocked');
		await expect(blocked).toBeVisible();
		await expect(blocked.locator('[data-blocker="approved_invoices"]')).toHaveText(
			'It holds approved invoices.'
		);
		await expect(card.getByTestId('close-workspace-submit')).toHaveCount(0);
	});

	test('a non-admin sees the notice and the page asks the server nothing', async ({
		page,
		tenantClerk
	}) => {
		let asked = 0;
		page.on('request', (r) => {
			if (new URL(r.url()).pathname === CLOSE_PATH) asked += 1;
		});
		await signInAndWait(page, tenantClerk);
		await page.goto('/organization?section=close');

		const card = panel(page);
		await expect(card.getByTestId('close-workspace-admin-only')).toBeVisible();
		await expect(card.getByTestId('close-workspace-submit')).toHaveCount(0);
		expect(asked).toBe(0);
	});
});
