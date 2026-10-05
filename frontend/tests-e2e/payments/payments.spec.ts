import { expect, test } from '../fixtures/helpers';

/**
 * /payments — three-tab surface (Queue / History / Runs) plus a
 * summary panel. Seed creates 1 payment run with 3 payments and 4
 * exceptions, so History and Runs both render content.
 */

test.describe('/payments (acme admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/payments');
	});

	test('summary cards render', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Payments' })).toBeVisible();

		// `.scard` blocks are populated from /api/payments/summary. We
		// don't assert on the dollar amounts (seed data may evolve);
		// the contract is "summary cards present and labelled".
		const labels = page.locator('.scard-label');
		await expect(labels.first()).toBeVisible({ timeout: 5_000 });
		expect(await labels.count()).toBeGreaterThanOrEqual(1);
	});

	test('Queue tab is active by default, and says so to assistive tech', async ({ page }) => {
		// The selected state is exposed, not just painted: the bar used to be
		// plain buttons with an `.active` class, announced as four identical
		// buttons (WCAG 4.1.2). It is the shared `<Tabs>` tablist now.
		const tablist = page.getByRole('tablist', { name: 'Payments' });
		const queue = tablist.getByRole('tab', { name: /Queue/ });
		await expect(queue).toHaveAttribute('aria-selected', 'true');
		await expect(tablist.getByRole('tab', { name: 'History' })).toHaveAttribute(
			'aria-selected',
			'false'
		);
		await expect(page.getByRole('tabpanel', { name: /Queue/ })).toBeVisible();

		// Arrow keys move between tabs (roving tabindex), and the panel follows.
		await queue.focus();
		await page.keyboard.press('ArrowRight');
		const history = tablist.getByRole('tab', { name: 'History' });
		await expect(history).toBeFocused();
		await expect(history).toHaveAttribute('aria-selected', 'true');
		await expect(page.getByRole('tabpanel', { name: 'History' })).toBeVisible();
	});

	test('History tab shows seeded payments', async ({ page }) => {
		await page.locator('.tab', { hasText: 'History' }).click();

		// 3 payments in the seed → table renders rows. The History tab
		// is the only one with the search "Search payments..." input.
		await expect(page.getByPlaceholder('Search payments...')).toBeVisible();
		await expect(page.locator('table tbody tr').first()).toBeVisible({
			timeout: 5_000
		});
	});

	test('Runs tab shows the seeded payment run', async ({ page }) => {
		await page.locator('.tab', { hasText: 'Runs' }).click();

		// Seed creates 1 run per tenant. Its status is DERIVED from its
		// payments (decisions.md §41), and the seed's run holds one
		// `completed` and one `pending` payment — so it reports `executing`,
		// not the `completed` its column says.
		const firstRow = page.locator('table tbody tr.clickable').first();
		await expect(firstRow).toBeVisible({ timeout: 5_000 });

		// …which is exactly why this asserts a TONE rather than a colour or a
		// specific status: the row pill is `<Badge>`, and every status the
		// backend can derive must map to one of its tones. `executing` and
		// `partial` had no page-local CSS rule and rendered untinted, so the
		// seeded run's own badge was one of the invisible ones.
		const runPill = firstRow.locator('.badge');
		await expect(runPill).toHaveClass(/\b(accent|success|warning|danger|muted|neutral|erp)\b/);

		await firstRow.click();
		const modal = page.locator('div.modal[role="dialog"][aria-label="Payment run"]');
		await expect(modal).toBeVisible();
		await expect(modal.locator('h2')).toHaveText('Payment Run');
		// The modal's own pill still hand-rolls its status classes
		// (RunDetailModal is not on the shared primitive yet).
		await expect(modal.locator('.status-badge')).toBeVisible();

		// Close it. The modal has two "Close" controls — an X icon
		// (close-btn, aria-label="Close") and a footer Cancel button
		// labelled "Close". Click the X to be unambiguous.
		await modal.locator('.close-btn').click();
		await expect(modal).toBeHidden();
	});
});
