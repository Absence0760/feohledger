import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from './axe-helper';

/**
 * Accessibility regression guard for **de-emphasised list rows** (WCAG 1.4.3).
 *
 * A paused webhook subscription, a revoked API key and a deactivated user are
 * each drawn quieter than their live siblings. That used to be spelled
 * `opacity: 0.5`–`0.6` on the row's cells, which is GROUP opacity: it
 * composites the row's whole subtree — text, status badge and all — down onto
 * the surface behind it, so it faded hardest exactly the colours that were
 * already quiet. Measured on `--surface`:
 *
 *     --text            @0.6 → 5.65:1   @0.5 → 4.34:1
 *     --text-muted      @0.6 → 2.77:1   @0.5 → 2.33:1
 *     a tinted <Badge>  @0.6 → 2.78–2.93:1
 *
 * i.e. the one cell explaining *why* a row is faded became the least readable
 * thing in it. The rows now take `class:row-muted` (`app.css`), which names
 * `--text-muted` (5.38:1) and leaves a descendant that sets its own colour —
 * the badge's calibrated `-on-tint` pair — at full strength.
 *
 * **Why this spec exists separately from `axe.spec.ts`.** That suite scans
 * these same routes, and it passed throughout: a fresh `e2e<N>` tenant has no
 * paused subscription, no revoked key and no deactivated user, so the faded row
 * never rendered and axe had nothing to measure. A route list is not coverage
 * of a route's *states*. Each test here stubs the list response so the
 * de-emphasised row is guaranteed on screen when axe runs.
 *
 * The complement is the static scan `src/lib/a11y/opacityAudit.test.ts`, which
 * catches the idiom being reintroduced anywhere in the tree without needing a
 * route to render it. Neither subsumes the other: the scan cannot resolve the
 * cascade, and axe cannot see a state no listed route reaches.
 *
 * The `/payments` case at the bottom is the one worth understanding rather than
 * pattern-matching. Its fade was `0.72` — mild enough that the invoice number
 * still rendered at 7.56:1 — but group opacity is uniform, so it hit hardest
 * the one element the row exists to show: the `.blocked-chip` naming WHY the
 * payment run refuses this invoice (a duplicate, a fraud flag, a line-total
 * mismatch) composited to 3.45:1. A financial-integrity signal rendered less
 * legible than the ordinary rows around it is a different kind of defect from a
 * cosmetic contrast miss.
 */

const ISO = '2026-01-15T09:00:00Z';

/** One live row + one de-emphasised row, so the scan covers both treatments. */
const SUBSCRIPTIONS = [
	{
		id: 'sub-active',
		name: 'Ledger sync',
		target_url: 'https://example.test/hooks/ledger',
		event_types: ['invoice.approved'],
		secret_prefix: 'whsec_liv',
		active: true,
		created_at: ISO,
		updated_at: ISO,
		previous_secret_expires_at: null
	},
	{
		id: 'sub-paused',
		name: 'Archive mirror',
		target_url: 'https://example.test/hooks/archive',
		event_types: ['payment.settled', 'exception.raised'],
		secret_prefix: 'whsec_pau',
		active: false,
		created_at: ISO,
		updated_at: ISO,
		previous_secret_expires_at: null
	}
];

const API_KEYS = [
	{
		id: 'key-active',
		name: 'Reporting pipeline',
		key_prefix: 'feoh_live_aaa',
		scopes: ['read'],
		created_at: ISO,
		last_used_at: ISO,
		revoked_at: null
	},
	{
		id: 'key-revoked',
		name: 'Retired integration',
		key_prefix: 'feoh_live_zzz',
		scopes: ['read'],
		created_at: ISO,
		last_used_at: ISO,
		revoked_at: ISO
	}
];

const USERS = {
	items: [
		{
			id: 'user-active',
			email: 'live.user@example.test',
			full_name: 'Live User',
			is_active: true,
			roles: [{ id: 'role-1', name: 'ap_manager' }],
			created_at: ISO,
			last_login: ISO
		},
		{
			id: 'user-inactive',
			email: 'former.user@example.test',
			full_name: 'Former User',
			is_active: false,
			roles: [{ id: 'role-2', name: 'ap_clerk' }],
			created_at: ISO,
			last_login: null
		}
	],
	total: 2,
	page: 1,
	page_size: 20
};

/**
 * Fulfil `pathname` with `body`, letting every other URL through. The
 * `**${pathname}*` glob also matches nested routes (`/api/webhooks/deliveries`),
 * so the handler re-checks the pathname rather than trusting the glob — the
 * same guard `tests-e2e/requisitions/search-scope.spec.ts` documents.
 */
async function stubList(
	page: import('@playwright/test').Page,
	pathname: string,
	body: unknown
): Promise<void> {
	await page.route(`**${pathname}*`, async (route) => {
		if (new URL(route.request().url()).pathname !== pathname) {
			await route.continue();
			return;
		}
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(body)
		});
	});
}

test.describe('accessibility — de-emphasised rows (WCAG 1.4.3)', () => {
	/**
	 * Retire the response-rewriting route handlers before the test does.
	 *
	 * Two tests here call `route.fetch()` to get the real payload back before
	 * editing it. A page that is still settling can leave a handler inside that
	 * `await` when the last assertion resolves, and Playwright then tears the
	 * test down around it — `route.fetch: Test ended.` / `apiResponse.json:
	 * Response has been disposed`, reported as a failure of whichever test owned
	 * the handler. `a11y/target-size.spec.ts` lost two tests and a CI worker to
	 * exactly that; this file has the same exposure and had simply not been
	 * unlucky yet. Same remedy, same reasoning — see the longer note there.
	 */
	test.afterEach(async ({ page }) => {
		await page.unrouteAll({ behavior: 'ignoreErrors' });
	});

	test('a paused webhook subscription row has no axe violations', async ({ page }) => {
		await stubList(page, '/api/webhooks', SUBSCRIPTIONS);
		await page.goto('/admin/webhooks');
		await expect(page.getByRole('heading', { name: 'Webhooks', exact: true })).toBeVisible();

		// The de-emphasised row must actually be on screen — otherwise this
		// passes for the same reason the route-list scan did.
		const paused = page.locator('table tbody tr.row-muted');
		await expect(paused).toHaveCount(1);
		await expect(paused).toContainText('Archive mirror');

		await expectNoA11yViolations(page);
	});

	test('a revoked API-key row has no axe violations', async ({ page }) => {
		await stubList(page, '/api/api-keys', API_KEYS);
		await page.goto('/admin/api-keys');
		await expect(page.getByRole('heading', { name: 'API Keys', exact: true })).toBeVisible();

		const revoked = page.locator('table tbody tr.row-muted');
		await expect(revoked).toHaveCount(1);
		// The status cell is the one the old fade had to carve out with
		// `:not(.status-col)` — it must be inside the de-emphasised row here, so
		// axe measures the badge under whatever the row treatment now is.
		await expect(revoked).toContainText('Revoked');

		await expectNoA11yViolations(page);
	});

	test('an applied credit-memo row has no axe violations', async ({ page }) => {
		// Rewrite the real payload rather than replacing it: the memo list's
		// shape is wide and the point here is the ROW TREATMENT, not a fixture.
		await page.route('**/api/credit-memos**', async (route) => {
			if (route.request().method() !== 'GET') {
				await route.continue();
				return;
			}
			const response = await route.fetch();
			const body = (await response.json()) as { items?: { status?: string }[] };
			if (body.items?.length) body.items[0].status = 'applied';
			await route.fulfill({ response, json: body });
		});

		await page.goto('/credit-memos');
		// A seeded tenant already holds applied and voided memos, so the count
		// is the tenant's, not this test's — the rewrite above only guarantees
		// at least one exists on ANY tenant, including a fresh one.
		const muted = page.locator('table tbody tr.row-muted');
		await expect(muted.first()).toBeVisible();
		// The status badge is the cell that explains the de-emphasis, and the
		// one the fade dimmed hardest (2.59:1 applied / 2.91:1 void).
		// Case-insensitive: the badge prints the TRANSLATED status label
		// ("Applied" / "Void"), not the raw `applied` / `void` column value it
		// used to leak into every locale.
		await expect(muted.first().locator('.badge')).toHaveText(/applied|void/i);

		await expectNoA11yViolations(page);
	});

	test('a blocked payment-queue row has no axe violations', async ({ page }) => {
		// Same rewrite-the-real-payload approach the queue-blocked spec uses:
		// which rows a tenant has blocked is the tenant's business, so flip a
		// known one rather than asserting against whatever it happens to hold.
		await page.route('**/api/payments/queue**', async (route) => {
			if (route.request().method() !== 'GET') {
				await route.continue();
				return;
			}
			const response = await route.fetch();
			const body = (await response.json()) as {
				items?: { blocked?: boolean; blocked_reason?: string }[];
				blocked_total?: number;
				selectable_total?: number;
			};
			const first = body.items?.[0];
			if (first && !first.blocked) {
				first.blocked = true;
				first.blocked_reason = 'duplicate';
				if (typeof body.blocked_total === 'number') body.blocked_total += 1;
				if (typeof body.selectable_total === 'number')
					body.selectable_total = Math.max(0, body.selectable_total - 1);
			}
			await route.fulfill({ response, json: body });
		});

		await page.goto('/payments');
		await page.locator('.tab', { hasText: 'Queue' }).click();
		// The DataTable's loading placeholder is itself a `tbody tr`, so wait on
		// the absence of the empty row rather than on "a row is visible" — the
		// readiness signal the queue-blocked spec documents.
		await expect(page.getByTestId('table-empty')).toHaveCount(0);

		// Same caveat as the memo list: a tenant can already hold blocked rows,
		// so the rewrite is a floor, not the count.
		const blocked = page.locator('table tbody tr.row-muted');
		await expect(blocked.first()).toBeVisible();
		// The reason chip must be INSIDE the de-emphasised row — it is the whole
		// reason this row is scanned, and the element the 0.72 fade cost most.
		await expect(blocked.first().getByTestId('queue-blocked-chip')).toBeVisible();

		await expectNoA11yViolations(page);
	});

	test('a deactivated user row has no axe violations', async ({ page }) => {
		await stubList(page, '/api/admin/users', USERS);
		await page.goto('/admin');
		await expect(page.getByRole('heading', { name: 'Users & Roles', exact: true })).toBeVisible();

		const inactive = page.locator('table tbody tr.row-muted');
		await expect(inactive).toHaveCount(1);
		// The email cell is `--text-muted` at rest — 2.33:1 under the old 0.5
		// fade, and the reason this row is scanned rather than assumed fine.
		await expect(inactive).toContainText('former.user@example.test');

		await expectNoA11yViolations(page);
	});
});
