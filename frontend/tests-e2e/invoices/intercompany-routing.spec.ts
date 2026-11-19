import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	deleteVendorsWhere,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * Invoice modal — inter-company routing (multi-entity).
 *
 * `POST /api/invoices/{id}/route-intercompany` generates the mirror PAYABLE
 * under a counterparty entity of the SAME tenant. It creates a live liability
 * under books the operator may not be looking at, so the control is
 * confirm-then-act and the confirmation names the counterparty entity.
 *
 * Three gates are asserted here because each one hides a different failure:
 *   - the panel only exists once the tenant actually has a second entity (the
 *     same `entityStore.multiEntity` signal the sidebar switcher renders on);
 *     without a counterparty the endpoint could only ever 400;
 *   - once routed, the ROUTED STATE replaces the action. The backend stamps a
 *     counterparty only while unrouted and returns the same mirror on a repeat
 *     call, so re-offering the button would be a control that cannot change
 *     anything;
 *   - the write is admin / ap_manager — an ap_clerk is refused.
 *
 * The second entity is created through the API, exactly as
 * `entities/switcher.spec.ts` does (the seed tenants ship single-entity).
 */

const MARKER = 'IC';

async function createEntity(
	page: import('@playwright/test').Page,
	name: string,
	slug: string
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/entities`, {
		headers: await authedTenantHeaders(page),
		data: { name, slug }
	});
	expect(resp.status()).toBe(201);
	return ((await resp.json()) as { id: string }).id;
}

async function createInvoice(
	page: import('@playwright/test').Page,
	number: string,
	vendorName: string
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: vendorName, invoice_number: number, amount: '500.00', currency: 'USD' }
	});
	expect(resp.status()).toBe(201);
	return ((await resp.json()) as { id: string }).id;
}

/** Open an invoice's modal via search → Edit. */
async function openInvoice(page: import('@playwright/test').Page, number: string) {
	await page.goto('/invoices');
	const search = page.getByPlaceholder('Search invoices...');
	await search.fill(number);
	await page.waitForResponse(
		(r) => r.url().includes('/api/invoices') && r.url().includes('search=')
	);
	// Exact name, not a substring: once routed, the mirror's number is
	// `IC-<number>`, so a `hasText` row filter matches BOTH rows.
	const link = page.getByRole('button', { name: `Edit invoice ${number}`, exact: true });
	await expect(link).toBeVisible({ timeout: 10_000 });
	await link.click();
	const modal = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
	await expect(modal).toBeVisible();
	return modal;
}

/** Remove the origin AND its `IC-`-prefixed mirror (one LIKE covers both), then
 *  the throwaway entity. The self-FK `intercompany_mirror_id` is nulled first —
 *  it points between the two rows, so deleting either one first would trip it. */
function cleanup(stamp: string, entityId: string, vendorName: string): void {
	const like = `invoice_number LIKE '%${MARKER}-ORIGIN-${stamp}%'`;
	// Each statement stands alone: one failure must not abandon the rest and
	// leave the throwaway entity behind for every later spec in this tenant.
	for (const stmt of [
		`UPDATE invoices SET intercompany_mirror_id=NULL WHERE ${like}`,
	]) {
		try {
			tenantPsql(stmt);
		} catch {
			/* best-effort */
		}
	}
	try {
		deleteInvoicesWhere(like);
	} catch {
		/* best-effort */
	}
	// `POST /api/invoices` auto-creates an unverified vendor from the name;
	// `deleteVendorsWhere` owns whatever the app hung off it. The entity
	// goes last — `vendors.entity_id` FK-references it.
	try {
		deleteVendorsWhere(`name='${vendorName}'`);
	} catch {
		/* best-effort */
	}
	try {
		tenantPsql(`DELETE FROM entities WHERE id='${entityId}'`);
	} catch {
		/* best-effort */
	}
}

test.describe('/invoices inter-company routing', () => {
	test('routes to a named counterparty, then shows the routed state instead of the action', async ({
		page
	}) => {
		const stamp = Date.now().toString(36);
		const entityName = `IC Sub ${stamp}`;
		const number = `${MARKER}-ORIGIN-${stamp}`;

		await page.goto('/invoices');
		const vendorName = `${MARKER}-VENDOR-${stamp}`;
		const entityId = await createEntity(page, entityName, `ic-sub-${stamp}`);
		const invoiceId = await createInvoice(page, number, vendorName);

		try {
			// Reload so the entity store sees >1 entity (the panel's gate).
			const modal = await openInvoice(page, number);

			const panel = modal.locator('[data-testid="intercompany"]');
			await expect(panel).toBeVisible();
			// Advisory-free framing: it says what it will create.
			await expect(panel).toContainText('creates a matching payable');

			await panel
				.locator('[data-testid="intercompany-counterparty"]')
				.selectOption({ label: entityName });

			// Confirm-then-act: the first click arms, and the armed label NAMES
			// the entity the payable will land under.
			const routeBtn = panel.getByRole('button', { name: 'Route inter-company' });
			await routeBtn.click();
			const confirmBtn = panel.getByRole('button', {
				name: `Create a payable under ${entityName}? Confirm`
			});
			await expect(confirmBtn).toBeVisible();

			const routed = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/invoices/${invoiceId}/route-intercompany`) &&
					r.request().method() === 'POST'
			);
			await confirmBtn.click();
			expect((await routed).status()).toBe(200);

			// The routed state replaces the picker + action.
			await expect(panel.locator('[data-testid="intercompany-routed"]')).toContainText(
				entityName
			);
			await expect(panel.locator('[data-testid="intercompany-mirror-id"]')).toBeVisible();
			await expect(panel.locator('[data-testid="intercompany-counterparty"]')).toBeHidden();

			// The mirror payable really exists, under the counterparty entity, at
			// `new` — the workflow entry point, not slipped past the state machine.
			const mirror = tenantPsql(
				`SELECT invoice_number || '|' || status FROM invoices WHERE intercompany_mirror_id='${invoiceId}'`
			).trim();
			expect(mirror).toBe(`IC-${number}|new`);

			// Re-opening the invoice shows the same settled state — the action is
			// never re-offered, so a second attempt can't be made from the UI.
			// `openInvoice` navigates, which closes the current modal.
			const reopened = await openInvoice(page, number);
			const reopenedPanel = reopened.locator('[data-testid="intercompany"]');
			await expect(reopenedPanel.locator('[data-testid="intercompany-routed"]')).toBeVisible();
			await expect(
				reopenedPanel.getByRole('button', { name: 'Route inter-company' })
			).toHaveCount(0);

			// And the endpoint itself is idempotent behind that UI: a direct
			// repeat returns the SAME mirror rather than a second payable.
			const again = await page.request.post(
				`${API_BASE}/api/invoices/${invoiceId}/route-intercompany`,
				{
					headers: await authedTenantHeaders(page),
					data: {
						counterparty_entity_id: tenantPsql(
							`SELECT counterparty_entity_id FROM invoices WHERE id='${invoiceId}'`
						).trim()
					}
				}
			);
			expect(again.status()).toBe(200);
			const mirrorCount = tenantPsql(
				`SELECT count(*) FROM invoices WHERE intercompany_mirror_id='${invoiceId}'`
			).trim();
			expect(mirrorCount).toBe('1');
		} finally {
			cleanup(stamp, entityId, vendorName);
		}
	});
});

test.describe('/invoices inter-company routing (clerk is refused)', () => {
	// `require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)` on the router — creating a
	// payable under another entity is not a clerk's duty.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk cannot route inter-company (403)', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const invoiceId = tenantPsql(`SELECT id FROM invoices LIMIT 1`).trim();
		expect(invoiceId).toMatch(/[0-9a-f-]{36}/);
		const entityId = tenantPsql(`SELECT id FROM entities LIMIT 1`).trim();
		expect(entityId).toMatch(/[0-9a-f-]{36}/);

		const resp = await page.request.post(
			`${API_BASE}/api/invoices/${invoiceId}/route-intercompany`,
			{
				headers: await authedTenantHeaders(page),
				data: { counterparty_entity_id: entityId }
			}
		);
		expect(resp.status()).toBe(403);
	});
});
