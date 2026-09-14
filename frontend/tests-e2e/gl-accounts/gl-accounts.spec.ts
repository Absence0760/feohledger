import { expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * `/gl-accounts` — the chart-of-accounts list page.
 *
 * What this file exists to pin is the reachability defect the page closes: the
 * READ is role-open (`GET /api/gl-accounts` is `get_current_user`) while both
 * writes are admin | ap_manager, so the row belongs in the nav for all four
 * roles and the two buttons belong to `auth.isManager` only. Before the page
 * existed, `POST /api/gl-accounts/sync-erp` — deliberately widened to
 * `ap_manager` on the backend — was callable from nowhere but the
 * `/organization` Data Sync panel, which the nav shows to `admin` alone inside
 * a blanket read-only `<fieldset>`. See `docs/decisions.md` §161.
 *
 * **This spec creates no rows.** The lean seed gives each worker tenant exactly
 * one GL account (`6000` / "Operating Expenses" / expense), and there is no
 * DELETE on this router, so a created account could not be cleaned up — an
 * unbounded leak across runs. Create is therefore exercised as far as the
 * dialog (opened, fields present, cancelled) and no further; the write itself
 * is covered by `backend/tests/test_gl_accounts.py`, whose realdb harness
 * truncates. Sibling specs (`erp/merge-dev`, `organization/gl-accounts-sync`)
 * legitimately add rows to this same tenant via a mock-ERP sync, so nothing
 * here asserts an exact row count — only that the seeded account is present
 * and that the filters do what they claim.
 */

/** The one account the lean seed guarantees in every worker tenant. */
const SEEDED_CODE = '6000';

test.describe('/gl-accounts', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/gl-accounts');
	});

	test('renders the chart of accounts with the seeded account', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Chart of Accounts' })).toBeVisible();

		const rows = page.locator('table tbody tr');
		await expect(rows.first()).toBeVisible();
		await expect(page.locator('table tbody td.mono', { hasText: SEEDED_CODE }).first()).toBeVisible();

		// The footer states the count it actually holds — the read is
		// unpaginated, so there is no "Showing all N" claim and no Load-more.
		const shown = await rows.count();
		await expect(page.locator('.count-line')).toHaveText(
			shown === 1 ? '1 account' : `${shown} accounts`
		);
		await expect(page.getByRole('button', { name: /Load more/ })).toHaveCount(0);
	});

	test('search filters server-side and round-trips through the URL', async ({ page }) => {
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const filtered = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
				r.url().includes(`search=${SEEDED_CODE}`)
		);
		await page.getByPlaceholder('Search code or name...').fill(SEEDED_CODE);
		await filtered;

		await expect(page.locator('table tbody td.mono', { hasText: SEEDED_CODE }).first()).toBeVisible();
		await expect(page).toHaveURL(new RegExp(`search=${SEEDED_CODE}`));
	});

	test('a search that matches nothing says so, and does not claim an empty chart', async ({
		page
	}) => {
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const miss = 'zzz-no-such-account-zzz';
		const filtered = page.waitForResponse(
			(r) => new URL(r.url()).pathname.endsWith('/api/gl-accounts') && r.url().includes('search=zzz')
		);
		await page.getByPlaceholder('Search code or name...').fill(miss);
		await filtered;

		// The FILTERED copy, not the genuinely-empty one and not the onboarding
		// block — a narrow filter must never read as "this tenant has no chart".
		await expect(page.getByTestId('table-empty')).toHaveText('No accounts match your filters.');
		await expect(page.getByTestId('gl-accounts-empty-state')).toHaveCount(0);
	});

	test('the type chip filters on the server and lights up from a bookmarked URL', async ({
		page
	}) => {
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const filtered = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
				r.url().includes('account_type=expense')
		);
		await page.getByRole('button', { name: 'Expense', exact: true }).click();
		await filtered;
		await expect(page).toHaveURL(/type=expense/);

		// Every row on screen really is an expense account.
		const typeCells = page.locator('table tbody tr td:nth-child(3)');
		const count = await typeCells.count();
		expect(count).toBeGreaterThan(0);
		for (let i = 0; i < count; i++) {
			await expect(typeCells.nth(i)).toHaveText('Expense');
		}

		// A pasted link reproduces the view rather than resetting it.
		const reloaded = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
				r.url().includes('account_type=expense')
		);
		await page.goto('/gl-accounts?type=expense');
		await reloaded;
		await expect(page.getByRole('button', { name: 'Expense', exact: true })).toHaveAttribute(
			'aria-pressed',
			'true'
		);
	});

	test('a chip click issues exactly one list request, not two', async ({ page }) => {
		// The page has TWO effects that call `load()` synchronously — the type
		// chip's and the inactive toggle's — and Svelte tracks reads
		// transitively through the functions an effect calls. A tracked filter
		// read inside `buildParams()` therefore lands in both dependency sets,
		// so one chip click re-runs both and fires two identical requests. The
		// request sequencer keeps the later from clobbering the earlier, which
		// is precisely what makes the duplicate invisible without a count — so
		// this counts. (`fill()` cannot catch the search half of the same
		// family; `reactivity/search-debounce-race.spec.ts` types for that.)
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		let listCalls = 0;
		page.on('request', (r) => {
			if (new URL(r.url()).pathname.endsWith('/api/gl-accounts') && r.method() === 'GET') {
				listCalls++;
			}
		});

		const filtered = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
				r.url().includes('account_type=expense')
		);
		await page.getByRole('button', { name: 'Expense', exact: true }).click();
		await filtered;
		// Settle: a duplicate would be issued in the same microtask flush as the
		// first, so it is already counted by the time the response lands — but
		// assert on the request the click produced rather than on a quiet
		// network, and give a second effect nothing to hide behind.
		await expect(page).toHaveURL(/type=expense/);
		expect(listCalls).toBe(1);
	});

	test('the inactive toggle is URL-backed and adds the Status column', async ({ page }) => {
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		// Status earns a column only once an inactive row can appear at all.
		await expect(page.getByRole('columnheader', { name: 'Status' })).toHaveCount(0);

		const withInactive = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
				r.url().includes('active_only=false')
		);
		await page.getByLabel('Include inactive').check();
		await withInactive;

		await expect(page).toHaveURL(/inactive=1/);
		await expect(page.getByRole('columnheader', { name: 'Status' })).toBeVisible();

		// And back off again — the param is removed, not left behind.
		const activeOnly = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
				r.url().includes('active_only=true')
		);
		await page.getByLabel('Include inactive').uncheck();
		await activeOnly;
		await expect(page).not.toHaveURL(/inactive=1/);
	});

	test('a failed RE-load drops the stale rows instead of relabelling them', async ({ page }) => {
		// `errored` only reaches the reader through the composed `empty`
		// message, and `DataTable` renders that on `isEmpty` alone. So a second
		// failed load — a chip click or a search after one good fetch — used to
		// leave the PREVIOUS filter's rows on screen with nothing but a toast
		// that fades, while the count footer kept reporting that stale number
		// as the answer to filters it never ran. The first-load case is in the
		// parameterized `reactivity/list-load-failure.spec.ts`; only the
		// second-load one can go stale, which is why it is pinned here.
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		await expect(page.locator('.count-line')).toBeVisible();

		await page.route('**/api/gl-accounts*', async (route) => {
			if (new URL(route.request().url()).pathname !== '/api/gl-accounts') {
				await route.continue();
				return;
			}
			await route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'boom' })
			});
		});

		await page.getByRole('button', { name: 'Asset', exact: true }).click();

		await expect(page.getByTestId('table-empty')).toHaveText(
			/Couldn.t load the chart of accounts/
		);
		await expect(page.locator('table tbody td.mono')).toHaveCount(0);
		// …and the footer stops asserting a count it no longer has.
		await expect(page.locator('.count-line')).toHaveCount(0);
	});

	test('an admin gets both writes; the create dialog opens and cancels clean', async ({ page }) => {
		await expect(page.getByRole('button', { name: '+ New Account' })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Sync from ERP' })).toBeVisible();

		await page.getByRole('button', { name: '+ New Account' }).click();
		const modal = page.locator('div.modal[role="dialog"][aria-label="New GL account"]');
		await expect(modal).toBeVisible();
		await expect(modal.getByText('Parent code')).toBeVisible();
		await expect(modal.locator('select')).toBeVisible();
		// Submit stays disabled until both required fields are filled, so a
		// cancelled dialog cannot have written anything.
		await expect(modal.getByRole('button', { name: 'Create account' })).toBeDisabled();
		await modal.getByRole('button', { name: 'Cancel' }).click();
		await expect(modal).toBeHidden();
	});
});

test.describe('/gl-accounts — RBAC', () => {
	test('an ap_manager sees the sync action (the role the backend widened it to)', async ({
		page,
		tenantManager
	}) => {
		await signInAndWait(page, tenantManager);
		await page.goto('/gl-accounts');
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		await expect(page.getByRole('button', { name: 'Sync from ERP' })).toBeVisible();
		await expect(page.getByRole('button', { name: '+ New Account' })).toBeVisible();
	});

	test('an ap_clerk reads the chart and gets neither write control', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		await page.goto('/gl-accounts');

		// The read is role-open, which is the whole reason the nav row admits a
		// clerk: hiding it would have been a dead end, not a gate.
		await expect(page.getByRole('heading', { name: 'Chart of Accounts' })).toBeVisible();
		await expect(page.locator('table tbody td.mono', { hasText: SEEDED_CODE }).first()).toBeVisible();

		await expect(page.getByRole('button', { name: 'Sync from ERP' })).toHaveCount(0);
		await expect(page.getByRole('button', { name: '+ New Account' })).toHaveCount(0);

		// …and the row is genuinely reachable from the Procurement section bar.
		await expect(page.locator('nav a[href="/gl-accounts"]')).toBeVisible();
	});
});
