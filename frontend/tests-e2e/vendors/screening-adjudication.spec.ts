import { expect, test } from '../fixtures/helpers';
import type { Page, Route } from '@playwright/test';

/**
 * /vendors/screening — false-positive adjudication (decisions §477), offered
 * from the server's verdicts only (`adjudicate_refusal`, issue #575).
 *
 * The queue and the vendor's history are mocked so each case pins one verdict
 * shape without seeding a provider hit:
 *
 *   1. allowed — the control is enabled once the reason reaches the minimum
 *      length, and the POST carries the screen the row shows
 *      (`sanctions_check_id` = `latest_check_id`);
 *   2. refused — the control is disabled and its reason is shown, tied to the
 *      button with `aria-describedby`;
 *   3. hidden — `role_not_permitted` offers nothing;
 *   4. stale — a rescreen landed: the POST answers 409, the page says so and
 *      reads the queue again;
 *
 * plus the row's "Adjudicated" badge for a vendor already cleared.
 */

const VENDOR_ID = '00000000-0000-4000-8000-00000000a477';
const CHECK_ID = '00000000-0000-4000-8000-0000000c4477';
const NEW_CHECK_ID = '00000000-0000-4000-8000-0000000c5477';
const NAME = 'Adjudication Fixture Co';
const REASON = 'Different entity: registration number does not match.';

type Verdict = { code: string; message: string; params?: Record<string, unknown> } | null;

function item(over: Record<string, unknown> = {}) {
	return {
		vendor_id: VENDOR_ID,
		vendor_name: NAME,
		screening_status: 'review',
		last_screened_at: '2026-11-20T10:00:00Z',
		payments_blocked: false,
		risk_level: 'medium',
		risk_score: null,
		latest_matched_list: 'ADVERSE_MEDIA',
		latest_provider: 'mock',
		latest_check_id: CHECK_ID,
		latest_categories: ['adverse_media'],
		adverse_media: true,
		adjudication: null,
		active_adjudications: [],
		adjudicate_refusal: null as Verdict,
		revoke_adjudication_refusal: {
			code: 'sanctions_adjudication_not_active',
			message: "This vendor's screen has no active false-positive adjudication."
		} as Verdict,
		...over
	};
}

const ADJUDICATION = {
	id: '00000000-0000-4000-8000-0000000ad477',
	vendor_id: VENDOR_ID,
	sanctions_check_id: CHECK_ID,
	provider: 'mock',
	result: 'review_required',
	matched_list: 'ADVERSE_MEDIA',
	fingerprint: 'f'.repeat(64),
	reason: REASON,
	adjudicated_by_user_id: '00000000-0000-4000-8000-000000000001',
	adjudicated_at: '2026-11-20T11:00:00Z',
	revoked_by_user_id: null,
	revoked_at: null,
	revoke_reason: null
};

/** Serve the queue from `rows()` (read on every request) and an empty history. */
async function mockQueue(page: Page, rows: () => Record<string, unknown>[]) {
	await page.route('**/api/vendors/screening/review-queue**', (route: Route) =>
		route.fulfill({
			json: { items: rows(), total: rows().length, page: 1, page_size: 50 }
		})
	);
	await page.route(`**/api/vendors/${VENDOR_ID}/screening-history`, (route: Route) =>
		route.fulfill({ json: [] })
	);
}

async function openRow(page: Page) {
	await page.goto('/vendors/screening');
	const row = page.locator('table tbody tr', { hasText: NAME });
	await row.locator('.row-link').click();
	const modal = page.getByRole('dialog', { name: 'Vendor screening review' });
	await expect(modal).toBeVisible();
	return modal;
}

test.describe('/vendors/screening false-positive adjudication', () => {
	test('allowed: enabled at the minimum length, posting the screen the row shows', async ({
		page
	}) => {
		let rows = [item()];
		await mockQueue(page, () => rows);
		let posted: Record<string, unknown> | null = null;
		await page.route(`**/api/vendors/${VENDOR_ID}/screening/adjudicate`, async (route) => {
			posted = route.request().postDataJSON();
			rows = [
				item({
					adjudication: ADJUDICATION,
					active_adjudications: [ADJUDICATION],
					adjudicate_refusal: {
						code: 'sanctions_adjudication_already_cleared',
						message: 'This screen result is already adjudicated as a false positive.'
					},
					revoke_adjudication_refusal: null
				})
			];
			await route.fulfill({ status: 201, json: ADJUDICATION });
		});

		const modal = await openRow(page);
		const section = modal.getByTestId('screening-adjudication');
		const reason = section.getByTestId('screening-adjudication-reason');
		// The minimum is said, and tied to the field.
		await expect(reason).toHaveAttribute('aria-describedby', 'adjudication-reason-hint');
		await expect(section.locator('#adjudication-reason-hint')).toHaveText(
			'At least 3 characters.'
		);
		const submit = section.getByTestId('screening-adjudication-submit');
		await reason.fill('no');
		await expect(submit).toBeDisabled();
		await reason.fill(REASON);
		await expect(submit).toBeEnabled();
		await submit.click();

		await expect(section.getByTestId('screening-adjudication-cleared')).toBeVisible();
		expect(posted).toEqual({ reason: REASON, sanctions_check_id: CHECK_ID });
		// The queue row now carries the text badge.
		await page.keyboard.press('Escape');
		const row = page.locator('table tbody tr', { hasText: NAME });
		await expect(row.getByTestId('screening-row-adjudicated')).toHaveText('Adjudicated');
	});

	test('refused: disabled, with the reason tied to the button', async ({ page }) => {
		await mockQueue(page, () => [
			item({
				adjudicate_refusal: {
					code: 'sanctions_adjudication_segregation',
					message:
						'You onboarded this vendor or put its current bank details in place, so another reviewer must adjudicate its screen.'
				}
			})
		]);
		const modal = await openRow(page);
		const section = modal.getByTestId('screening-adjudication');
		const submit = section.getByTestId('screening-adjudication-submit');
		await expect(submit).toBeDisabled();
		await expect(submit).toHaveAttribute('aria-describedby', 'adjudication-refusal');
		await expect(section.locator('#adjudication-refusal')).toContainText('another reviewer');
	});

	test('hidden: role_not_permitted offers no adjudication', async ({ page }) => {
		const notPermitted = { code: 'role_not_permitted', message: 'Your role does not permit this action.' };
		await mockQueue(page, () => [
			item({ adjudicate_refusal: notPermitted, revoke_adjudication_refusal: notPermitted })
		]);
		const modal = await openRow(page);
		await expect(modal.getByRole('heading', { name: 'Screening history' })).toBeVisible();
		await expect(modal.getByTestId('screening-adjudication')).toHaveCount(0);
	});

	test('stale: a rescreen since the read answers 409 and the queue is read again', async ({
		page
	}) => {
		let rows = [item()];
		let queueReads = 0;
		await page.route('**/api/vendors/screening/review-queue**', (route: Route) => {
			queueReads += 1;
			return route.fulfill({ json: { items: rows, total: rows.length, page: 1, page_size: 50 } });
		});
		await page.route(`**/api/vendors/${VENDOR_ID}/screening-history`, (route: Route) =>
			route.fulfill({ json: [] })
		);
		await page.route(`**/api/vendors/${VENDOR_ID}/screening/adjudicate`, async (route) => {
			// The rescreen landed: the next queue read shows the new screen.
			rows = [item({ latest_check_id: NEW_CHECK_ID })];
			await route.fulfill({
				status: 409,
				json: {
					detail:
						'The vendor was screened again after you opened it. Reload and review the latest result before adjudicating.'
				}
			});
		});

		const modal = await openRow(page);
		const readsBefore = queueReads;
		const section = modal.getByTestId('screening-adjudication');
		await section.getByTestId('screening-adjudication-reason').fill(REASON);
		await section.getByTestId('screening-adjudication-submit').click();

		await expect(page.getByText('screened again', { exact: false })).toBeVisible();
		await expect.poll(() => queueReads).toBeGreaterThan(readsBefore);
		await expect(section.getByTestId('screening-adjudication-cleared')).toHaveCount(0);
	});
});
