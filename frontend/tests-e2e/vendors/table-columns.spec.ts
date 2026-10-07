import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	deleteVendorsWhere,
	expect,
	test
} from '../fixtures/helpers';

/**
 * /vendors keeps every value inside its own column.
 *
 * The table is `table-layout: fixed` and declared no widths, so each column got
 * an equal share and nothing clipped: an email ran under the Status badge, and
 * an unverified row's four action buttons ran off the table's right edge. The
 * actions cell was also `display: flex`, which is not a table cell at all.
 *
 * Driven with a deliberately extreme address so the edge case is what's tested.
 */
const PREFIX = 'E2E Col Width ';

test.describe('/vendors table columns', () => {
	test.afterEach(() => deleteVendorsWhere(`name LIKE '${PREFIX}%'`, currentTenantSlug()));

	test('a very long email is clipped to its column, never under the next one', async ({
		page
	}) => {
		await page.setViewportSize({ width: 2000, height: 1100 });
		await page.goto('/vendors');
		const name = `${PREFIX}${Date.now()}`;
		const email = `accounts-payable.remittance-notifications+${Date.now()}@a-very-long-subsidiary-name.example-holdings.co.uk`;
		const created = await page.request.post(`${API_BASE}/api/vendors`, {
			headers: await authedTenantHeaders(page),
			data: { name, email }
		});
		expect(created.status()).toBe(201);

		await page.goto(`/vendors?search=${encodeURIComponent(name)}`);
		const row = page.locator('tbody tr', { hasText: name });
		await expect(row).toHaveCount(1);

		const cells = row.locator('td');
		const emailCell = row.locator('td', { hasText: '@' });
		await expect(emailCell).toHaveAttribute('title', email);

		const geometry = await row.evaluate((tr) => {
			const tds = Array.from(tr.querySelectorAll('td'));
			const emailTd = tds.find((td) => td.textContent?.includes('@'))!;
			const next = emailTd.nextElementSibling as HTMLElement;
			return {
				emailRight: emailTd.getBoundingClientRect().right,
				nextLeft: next.getBoundingClientRect().left,
				truncated: emailTd.scrollWidth > emailTd.clientWidth,
				overflow: getComputedStyle(emailTd).overflow,
				ellipsis: getComputedStyle(emailTd).textOverflow
			};
		});
		expect(geometry.emailRight).toBeLessThanOrEqual(geometry.nextLeft + 0.5);
		expect(geometry.truncated, 'the address should be visibly truncated').toBe(true);
		expect(geometry.overflow).toBe('hidden');
		expect(geometry.ellipsis).toBe('ellipsis');
		await expect(cells).toHaveCount(10);
	});

	test('every row action sits inside the table, and the actions cell is a real cell', async ({
		page
	}) => {
		await page.setViewportSize({ width: 2000, height: 1100 });
		await page.goto('/vendors?status=unverified');
		const row = page.locator('tbody tr.unverified').first();
		await expect(row).toBeVisible();

		const out = await row.evaluate((tr) => {
			const actions = tr.querySelector('td.actions') as HTMLElement;
			const region = tr.closest('.grid-container') as HTMLElement;
			const regionRight = region.getBoundingClientRect().right;
			const buttons = Array.from(actions.querySelectorAll('button'));
			return {
				display: getComputedStyle(actions).display,
				buttons: buttons.length,
				overflowing: buttons.filter((b) => b.getBoundingClientRect().right > regionRight + 0.5)
					.length,
				// A real cell shares its row's box exactly.
				cellBottom: actions.getBoundingClientRect().bottom,
				rowBottom: tr.getBoundingClientRect().bottom
			};
		});
		expect(out.display).toBe('table-cell');
		expect(out.buttons).toBeGreaterThanOrEqual(3);
		expect(out.overflowing).toBe(0);
		expect(Math.abs(out.cellBottom - out.rowBottom)).toBeLessThanOrEqual(1);
	});
});
