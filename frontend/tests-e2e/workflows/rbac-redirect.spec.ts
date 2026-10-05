import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	TENANT_ROOT_URL,
	test
} from '../fixtures/helpers';

/**
 * `/workflows` and `/workflows/[id]` — the non-admin redirect guard.
 *
 * **What this closes.** `#lib/nav.ts` gates the Workflows row to
 * `roles: ['admin']`, but every READ under `/api/workflows` is
 * `get_current_user` — list, detail, templates, versions, diff, simulate,
 * export — and only the mutations are `require_roles(ROLE_ADMIN)`. So a
 * non-admin never saw the nav row but could type either URL, and neither page
 * shipped a read-only mode: what they reached was the full editing surface
 * (New Workflow, From Template, Import, per-row Delete, the bulk bar; on the
 * builder, an editable canvas and a Save), every control 403ing on click. A
 * dead end that looks like a working page.
 *
 * **Why the API leg of these tests asserts 200, not 403.** Every sibling
 * "ap_clerk is redirected away and the API 403s them" spec
 * (`admin/sweep-health`, `admin/api-keys`, `admin/webhooks`, `admin/partner`,
 * `admin/retention`, `admin/privacy`, `admin/access-review`) can pin BOTH
 * halves because those endpoints are role-gated. This one cannot, and the
 * difference is the point: the guard here reproduces the **nav's** gate, not
 * the backend's. Asserting the read still succeeds keeps that asymmetry in
 * front of whoever revisits it — the product question of whether the nav
 * should instead be WIDENED to match these role-open reads is open in
 * `docs/followups.md`, and a spec that quietly asserted 403 would read as
 * evidence it had been settled the other way.
 *
 * If that call ever goes to "widen", the fix is to widen the page's `allowed`
 * and add the read-only mode `/organization` carries — these two redirect
 * cases then flip to asserting a populated, non-editable page. Deleting the
 * guard and going back to a page of buttons that cannot work is not an option
 * either way, which is what the "no editable control ever rendered" assertions
 * below are really pinning.
 */

test.describe('/workflows RBAC (ap_clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('a clerk typing /workflows is redirected, never stranded on dead controls', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);

		// The nav row is admin-only, so the URL bar is the only way in.
		await expect(page.locator('aside.sidebar a[href="/workflows"]')).toHaveCount(0);

		await page.goto('/workflows');
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });

		// Nothing of the page survives the bounce. The heading first, because a
		// `toHaveCount(0)` alone passes vacuously against a document that never
		// rendered — `waitForURL` above is what makes these non-vacuous, and the
		// dashboard's own landing content is what is on screen instead.
		await expect(page.getByRole('heading', { level: 1 })).not.toHaveText('Workflows');
		await expect(page.getByRole('button', { name: '+ New Workflow' })).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'New from template' })).toHaveCount(0);

		// NOTE on what this does NOT cover. These assert the state after the
		// redirect settles. The frame BEFORE it — where the three toolbar
		// buttons would otherwise paint, since they depend on no loaded data —
		// is closed by the `{#if userLoaded && allowed}` gate on the actions
		// snippet, not by an assertion here: catching a single pre-navigation
		// frame deterministically needs either a sleep or a race, both of which
		// this repo forbids outright (root CLAUDE.md § Fix bugs at the source).
		// The table needs no such gate (its fetch is gated, so it has no rows)
		// and neither does the builder (hidden behind `{#if !workflow}`).
	});

	test('a clerk typing a builder URL is redirected too', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		// Read a real definition id AS THE CLERK. That the call succeeds is half
		// the finding: the guard is not backed by a 403, so guarding only the
		// list would have left this URL reachable by bookmark — and the builder
		// is the worse dead end, since a canvas edit is lost on the refusal
		// rather than merely refused.
		const headers = await authedTenantHeaders(page);
		const listed = await page.request.get(`${API_BASE}/api/workflows`, { headers });
		expect(listed.status()).toBe(200);
		const body = (await listed.json()) as { items: { id: string }[] };
		expect(body.items.length).toBeGreaterThan(0);
		const workflowId = body.items[0].id;

		await page.goto(`/workflows/${workflowId}`);
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });

		// The builder's own furniture is gone — not merely the list's.
		// `.canvas .node` is the selector every builder spec uses for a step node
		// (`workflows/detail.spec.ts` documents it).
		await expect(page.locator('.canvas .node')).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Save' })).toHaveCount(0);
	});

	test('the reads stay open and the mutations stay closed — the asymmetry the guard covers', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		const headers = await authedTenantHeaders(page);

		// Role-open read (`get_current_user`) — this is why the guard matches the
		// nav rather than the backend.
		const read = await page.request.get(`${API_BASE}/api/workflows`, { headers });
		expect(read.status()).toBe(200);

		// Admin-only mutation (`require_roles(ROLE_ADMIN)`) — every control the
		// unguarded page rendered ended here. The body is a VALID
		// `WorkflowDefinitionCreate` on purpose: a malformed one could return 422
		// from body validation and the test would pass without the role gate ever
		// being the reason. Nothing is created, so there is nothing to tear down.
		const write = await page.request.post(`${API_BASE}/api/workflows`, {
			headers,
			data: {
				name: 'clerk-should-not-create',
				description: '',
				steps: [{ number: 1, type: 'extraction', name: 'Extraction', enabled: true, config: {} }]
			}
		});
		expect(write.status()).toBe(403);
	});
});

test.describe('/workflows RBAC (admin — still reaches the page)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('an admin still lands on the list with its create action', async ({ page }) => {
		// The load moved behind the same `allowed` flag the redirect reads, so
		// this pins that the happy path did not become a page that never fetches.
		await signInAndWait(page);

		await page.goto('/workflows');
		await expect(page.getByRole('heading', { name: 'Workflows' })).toBeVisible();
		await expect(page.getByRole('button', { name: 'New Workflow' })).toBeVisible();
		// A seeded tenant always has the default definition, so the table having
		// rows is what proves the gated fetch actually ran.
		await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 15_000 });
	});
});
