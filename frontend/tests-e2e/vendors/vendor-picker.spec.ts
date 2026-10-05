import {
	API_BASE,
	authedTenantHeaders,
	deleteVendorsWhere,
	expect,
	selectVendorInPicker,
	test
} from '../fixtures/helpers';

/**
 * The shared vendor picker (`#lib/components/ui/VendorPicker.svelte`) reaches
 * PAST its first page.
 *
 * What this replaced: five surfaces each rendered a native `<select>` over a
 * client-side vendor list — two capped at `page_size=100` page 1, three walking
 * every page on mount. A `<select>` has no search, so on the capped pair a
 * tenant past 100 vendors could not select the rest ANYWHERE, and nothing on
 * screen said the list was a subset. The picker is a server-searched combobox,
 * so the fix has to be demonstrated at the only thing that matters: a vendor
 * the popup is not showing is still selectable, and the form submits ITS id.
 *
 * The fixture makes the truncation deterministic instead of assuming the
 * tenant's vendor count. `PAGE` vendors are created under a `zz` prefix so they
 * sort last (the list is `name ASC`), which puts the highest-numbered one off
 * page 1 no matter what else the tenant holds — including a tenant holding
 * nothing else at all.
 *
 * Driven through `/discounts`' `BulkNegotiationModal` because its picker
 * carries a `testid`; the component under test is shared, so one consumer is
 * the proof for all five.
 */

/** `VENDOR_PICKER_PAGE_SIZE` in `#lib/utils/vendorPicker.ts`. Duplicated rather
 *  than imported: a VALUE import from `#lib` typechecks and then fails to
 *  resolve when Playwright loads the spec (frontend `CLAUDE.md` § check:e2e). */
const PAGE = 25;

const RUN = Date.now().toString(36);
const PREFIX = `zzpick${RUN}`;
/** Sorts after every other fixture vendor, so it is never on page 1. */
const OFF_PAGE = `${PREFIX}-${String(PAGE).padStart(2, '0')}`;

async function makeVendor(page: import('@playwright/test').Page, name: string): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: await authedTenantHeaders(page),
		data: { name }
	});
	expect(resp.status(), await resp.text()).toBe(201);
	return ((await resp.json()) as { id: string }).id;
}

test.describe('shared vendor picker — reaching past the first page', () => {
	test('a vendor off page 1 is disclosed as missing, then found by search and submitted', async ({
		page
	}) => {
		try {
			// PAGE + 1 vendors: page 1 of the popup can hold at most PAGE of them,
			// so the last one is off it whatever else this tenant has.
			let offPageId = '';
			for (let i = 0; i <= PAGE; i++) {
				const name = `${PREFIX}-${String(i).padStart(2, '0')}`;
				const id = await makeVendor(page, name);
				if (name === OFF_PAGE) offPageId = id;
			}
			expect(offPageId, 'the off-page fixture vendor was not created').not.toBe('');

			await page.goto('/discounts');
			await expect(page.getByRole('heading', { name: 'Discounts' })).toBeVisible();
			await page.getByRole('button', { name: 'Propose vendor offer' }).click();

			const picker = page.getByTestId('bulk-negotiate-vendor');
			await picker.click();

			// (1) The subset is DISCLOSED. This is the half a `<select>` could
			// never do: it rendered its truncated options with nothing saying so.
			const options = page.getByRole('option');
			await expect(options).toHaveCount(PAGE);
			await expect(page.getByText(new RegExp(`Showing ${PAGE} of \\d+ matches`))).toBeVisible();
			await expect(page.getByTestId('bulk-negotiate-vendor-more')).toBeVisible();

			// (2) And the vendor really is absent from what is on screen — so the
			// search below is reaching something the popup was not offering.
			await expect(page.getByRole('option', { name: OFF_PAGE })).toHaveCount(0);

			// (3) Server-side search reaches it. Driven inline rather than through
			// `selectVendorInPicker`, because committing CLOSES the popup — the
			// narrowed count has to be read while it is still open.
			await picker.fill(OFF_PAGE);
			const match = page.getByRole('option', { name: OFF_PAGE });
			await expect(match).toHaveCount(1);
			// The count line no longer claims a remainder.
			await expect(page.getByText('All matches shown (1)')).toBeVisible();
			await match.click();
			await expect(picker).toHaveValue(new RegExp(OFF_PAGE));

			// (4) The picker committed the vendor's ID, not just its label. Read
			// it off the wire: a combobox that displays the right name while
			// submitting a stale id is the failure a label assertion misses.
			await page.getByLabel('Tier 1 — pay within days').fill('10');
			await page.getByLabel('Tier 1 — discount percent').fill('2.00');

			const submit = page.getByTestId('bulk-negotiate-submit');
			await submit.click(); // arms; posts nothing
			const posted = page.waitForRequest(
				(r) => r.url().includes('/api/discounts/bulk-negotiate') && r.method() === 'POST'
			);
			await submit.click(); // commits
			const body = (await posted).postDataJSON() as { vendor_id?: string };
			expect(body.vendor_id).toBe(offPageId);
		} finally {
			deleteVendorsWhere(`name LIKE '${PREFIX}-%'`);
		}
	});

	test('Escape closes the popup, not the dialog around it', async ({ page }) => {
		// `ui/Modal` traps focus with `actions/focusTrap`, which registers a REAL
		// `keydown` listener on the dialog box, while Svelte 5 delegates a plain
		// `onkeydown` to one listener at the app root — so a bubble-phase handler
		// in the picker runs only after the trap has already shut the modal. The
		// picker binds `onkeydowncapture` for exactly this, and this case is what
		// stops the binding being "tidied" back to `onkeydown` later: the popup
		// must absorb the FIRST Escape, and the dialog take the second.
		const prefix = `${PREFIX}e`;
		try {
			await makeVendor(page, `${prefix}-a`);

			await page.goto('/discounts');
			await page.getByRole('button', { name: 'Propose vendor offer' }).click();

			const dialog = page.locator(
				'div.modal[role="dialog"][aria-label="Propose a vendor-wide early-payment discount"]'
			);
			await expect(dialog).toBeVisible();

			const picker = page.getByTestId('bulk-negotiate-vendor');
			await picker.click();
			await expect(page.getByRole('listbox')).toBeVisible();

			await page.keyboard.press('Escape');
			await expect(page.getByRole('listbox')).toHaveCount(0);
			await expect(dialog).toBeVisible();

			// And the dialog is still reachable by the same key once the popup has
			// given it up — closing the popup must not strand the user in it.
			await page.keyboard.press('Escape');
			await expect(dialog).toHaveCount(0);
		} finally {
			deleteVendorsWhere(`name LIKE '${prefix}-%'`);
		}
	});

	test('re-opening on a committed vendor shows the unfiltered list, not zero matches', async ({
		page
	}) => {
		// The search term and the box's display TEXT are separate states. At rest
		// the box shows the chosen vendor's name, which is not a search term:
		// issuing it as one returns no matches and reads as "this tenant has no
		// vendors" — the exact confusion the picker exists to remove.
		//
		// Asserted against the count line rather than a named option, so the
		// tenant's own vendor population can't decide the outcome: whatever the
		// unfiltered count was before choosing, it must be that again after.
		const prefix = `${PREFIX}r`;
		try {
			await makeVendor(page, `${prefix}-a`);

			await page.goto('/discounts');
			await page.getByRole('button', { name: 'Propose vendor offer' }).click();

			const picker = page.getByTestId('bulk-negotiate-vendor');
			const countLine = page.locator('.vendor-picker p[aria-live="polite"]');

			await picker.click();
			// The line passes through "Loading vendors…" on its way to the count,
			// and `not.toBeEmpty()` is satisfied by that transient text — so the
			// baseline captured was a loading message the re-opened popup could
			// never match. Wait for it to settle before reading it.
			await expect(countLine).not.toHaveText('Loading vendors…');
			const unfiltered = await countLine.textContent();

			// Committing closes the popup, which empties the count line — so the
			// comparison below is between two OPEN states, not across a close.
			await selectVendorInPicker(picker, `${prefix}-a`);

			await picker.click();
			await expect(countLine).toHaveText(unfiltered ?? '');
		} finally {
			deleteVendorsWhere(`name LIKE '${prefix}-%'`);
		}
	});
});
