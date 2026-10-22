import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * The invoice modal offers Approve / Reject from the server's verdict
 * (`GET /api/invoices/{id}/review-actions`), never a restated rule
 * (issue #575 § One rule, one place).
 *
 * The drift this pins: the modal checked only the status and the
 * `invoice.approve` permission, so the admin who keyed an invoice was shown an
 * Approve that could only 403 `approval_segregation` (`daily-journey.spec.ts`
 * documents the same trap from the other side — it filters such rows out of
 * its candidates). Now that reader sees Approve DISABLED with the server's own
 * reason, keeps Reject (which has no segregation gate), and a row nobody
 * uploaded is approvable as before. Live data, no stubs: the verdict and the
 * click both go to the real backend.
 *
 * The backend parity — verdict `null` exactly when the POST succeeds, for every
 * role and row state — is `backend/tests/test_review_actions_parity.py`.
 */

const PREFIX = 'E2E-REVIEW-VERDICT';

async function createInReview(
	page: import('@playwright/test').Page,
	number: string
): Promise<string> {
	// Created through the API as THIS admin, so the admin is its uploader.
	const res = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: 'E2E Review Verdict Vendor', invoice_number: number, amount: '42.00', currency: 'USD' }
	});
	expect(res.status()).toBe(201);
	const id = ((await res.json()) as { id: string }).id;
	// Into review directly: PATCH ignores `status`, and the stage is the
	// fixture here, not the submit path.
	tenantPsql(`UPDATE invoices SET status='ready_for_review' WHERE id='${id}'`);
	return id;
}

test.describe('invoice modal — review decisions follow the server verdict', () => {
	test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`));

	test('the uploader sees Approve disabled with the reason, and Reject open', async ({ page }) => {
		const number = `${PREFIX}-OWN-${Date.now()}`;
		const id = await createInReview(page, number);

		const verdict = page.waitForResponse(
			(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
		);
		await page.goto(`/invoices?id=${id}`);
		await verdict;
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal.locator('header h2')).toContainText(number);

		const approve = modal.getByRole('button', { name: 'Approve', exact: true });
		await expect(approve).toBeVisible();
		await expect(approve).toBeDisabled();
		await expect(modal.getByTestId('approve-refusal')).toHaveText(
			'Segregation of duties: a user involved in creating this invoice cannot also approve it.'
		);
		await expect(modal.getByRole('button', { name: 'Reject', exact: true })).toBeEnabled();
	});

	test('an invoice nobody uploaded is approvable, with no refusal shown', async ({ page }) => {
		const number = `${PREFIX}-SYS-${Date.now()}`;
		const id = await createInReview(page, number);
		// No employee created it — the shape of an emailed-in invoice.
		tenantPsql(`UPDATE invoices SET uploaded_by_id = NULL WHERE id='${id}'`);

		const verdict = page.waitForResponse(
			(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
		);
		await page.goto(`/invoices?id=${id}`);
		await verdict;
		const modal = page.locator('div.modal[role="dialog"]');
		const approve = modal.getByRole('button', { name: 'Approve', exact: true });
		await expect(approve).toBeEnabled();
		await expect(modal.getByTestId('approve-refusal')).toHaveCount(0);

		const approved = page.waitForResponse(
			(r) => r.url().endsWith(`/api/invoices/${id}/approve`) && r.request().method() === 'POST'
		);
		await approve.click();
		expect((await approved).status()).toBe(200);
	});

	test('Approve, Reject and Save are on screen when the dialog opens on a short viewport', async ({
		page
	}) => {
		// Production report: Approve / Reject sat at the foot of the right
		// panel's scroll, below the summary, fields, line items, activity, chat
		// and e-invoice sections — an approver had to scroll past all of it to
		// decide. The actions now live in a footer UNDER the scroll region (not
		// pinned over it — decisions §181), so they are in view on open.
		await page.setViewportSize({ width: 1280, height: 720 });
		const number = `${PREFIX}-VIEW-${Date.now()}`;
		const id = await createInReview(page, number);
		tenantPsql(`UPDATE invoices SET uploaded_by_id = NULL WHERE id='${id}'`);

		const verdict = page.waitForResponse(
			(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
		);
		await page.goto(`/invoices?id=${id}`);
		await verdict;
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal.locator('header h2')).toContainText(number);

		const approve = modal.getByRole('button', { name: 'Approve', exact: true });
		const reject = modal.getByRole('button', { name: 'Reject', exact: true });
		const save = modal.getByRole('button', { name: 'Save', exact: true });
		await expect(approve).toBeEnabled();

		// The premise: the content really is taller than the pane, so "in view"
		// is a statement about placement, not about a short invoice.
		const scroll = modal.getByTestId('invoice-form-scroll');
		await expect
			.poll(() => scroll.evaluate((el) => el.scrollHeight > el.clientHeight))
			.toBe(true);
		expect(await scroll.evaluate((el) => el.scrollTop)).toBe(0);

		await expect(approve).toBeInViewport({ ratio: 1 });
		await expect(reject).toBeInViewport({ ratio: 1 });
		await expect(save).toBeInViewport({ ratio: 1 });
		// …and they are not overlaying the content: the footer sits wholly
		// below the scroll region.
		const footer = modal.getByTestId('invoice-modal-actions');
		const [scrollBox, footerBox] = await Promise.all([scroll.boundingBox(), footer.boundingBox()]);
		expect(scrollBox && footerBox).toBeTruthy();
		expect(footerBox!.y).toBeGreaterThanOrEqual(scrollBox!.y + scrollBox!.height - 1);

		// The reject-reason flow opens in the footer too, still in view.
		await reject.click();
		const reason = modal.getByRole('textbox', { name: 'Reason for rejection' });
		await expect(reason).toBeInViewport();
		await expect(modal.getByRole('button', { name: 'Confirm Reject' })).toBeInViewport();
	});

	test('at 320px the action footer neither scrolls sideways nor crowds out the fields', async ({
		page
	}) => {
		// 1.4.10 at the width it names. The footer is outside the scroll region,
		// so it must wrap rather than overflow, and must leave the fields room —
		// it is capped at half the pane and scrolls on its own past that.
		await page.setViewportSize({ width: 320, height: 720 });
		const number = `${PREFIX}-320-${Date.now()}`;
		const id = await createInReview(page, number);
		tenantPsql(`UPDATE invoices SET uploaded_by_id = NULL WHERE id='${id}'`);

		const verdict = page.waitForResponse(
			(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
		);
		await page.goto(`/invoices?id=${id}`);
		await verdict;
		const modal = page.locator('div.modal[role="dialog"]');
		const approve = modal.getByRole('button', { name: 'Approve', exact: true });
		await expect(approve).toBeEnabled();
		await expect(approve).toBeInViewport({ ratio: 1 });

		const footer = modal.getByTestId('invoice-modal-actions');
		const scroll = modal.getByTestId('invoice-form-scroll');
		for (const region of [footer, scroll]) {
			expect(await region.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
		}
		// Approve + Reject wrap inside their card rather than spilling past it
		// (which the footer's own scrollWidth cannot see while the spill stays
		// inside its padding).
		const spill = await footer.evaluate((el) => {
			const card = el.querySelector('.review-section')!.getBoundingClientRect();
			return [...el.querySelectorAll('.review-section button')]
				.map((b) => b.getBoundingClientRect())
				.filter((r) => r.left < card.left - 0.5 || r.right > card.right + 0.5).length;
		});
		expect(spill).toBe(0);
		await expect(modal.getByRole('button', { name: 'Reject', exact: true })).toBeInViewport({
			ratio: 1
		});
		const [footerH, scrollH] = await Promise.all([
			footer.evaluate((el) => el.getBoundingClientRect().height),
			scroll.evaluate((el) => el.getBoundingClientRect().height)
		]);
		// The footer's cap is max-height: 50% of the pane, so at worst the two
		// split it evenly; the 1px absorbs sub-pixel layout rounding only.
		expect(scrollH).toBeGreaterThanOrEqual(footerH - 1);
		expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
	});
});
