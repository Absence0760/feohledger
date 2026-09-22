import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /organization → Custom Domains panel, **refusal rendering**.
 *
 * `PUT /api/organization/branding/custom-domains` refuses two things an admin
 * can plausibly type, each with a specific, actionable message:
 *
 *   • 422 — a host under the platform's OWN domain (already routed by the
 *     `<slug>.<platform-domain>` subdomain path, so a second claim on it would
 *     make the two resolvers disagree).
 *   • 409 — a host already registered to a different tenant.
 *
 * Both used to land in a toast that faded, on top of a generic "Failed to save
 * custom domains" fallback. The message IS the value of the response, so the
 * panel renders it in a persistent `role="alert"` region beside the field.
 *
 * The 422 is exercised for real against the dev backend, whose platform domain
 * derives from `FEOH_TENANT_URL_TEMPLATE=http://{slug}.localhost:7777` → any
 * `*.localhost` host is refused. The 409 needs a SECOND tenant to have claimed
 * the host, which no single-tenant spec can arrange without reaching into
 * another worker's tenant, so its status/detail pair is replayed through
 * `page.route` — the assertion is that the panel renders whatever the backend
 * said, which is the contract this spec exists to hold.
 *
 * `?section=custom-domains` because the route shows one panel at a time now
 * (the slug is part of its URL contract).
 */

async function getDomains(page: import('@playwright/test').Page): Promise<string[]> {
	const resp = await page.request.get(`${API_BASE}/api/organization/branding/custom-domains`, {
		headers: await authedTenantHeaders(page)
	});
	return ((await resp.json()) as { custom_domains: string[] }).custom_domains;
}

async function setDomains(
	page: import('@playwright/test').Page,
	domains: string[]
): Promise<void> {
	await page.request.put(`${API_BASE}/api/organization/branding/custom-domains`, {
		headers: await authedTenantHeaders(page),
		data: { custom_domains: domains }
	});
}

function panel(page: import('@playwright/test').Page) {
	return page.locator('section.card', {
		has: page.getByRole('heading', { name: 'Custom Domains' })
	});
}

test.describe('/organization custom-domain refusals', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/organization?section=custom-domains');
	});

	test('the panel points at the provisioning runbook', async ({ page }) => {
		const card = panel(page);
		await expect(card.getByText('serve /api on that same origin')).toBeVisible();
		await expect(
			card.getByText('docs/founder-runbooks/custom-domain-provisioning.md')
		).toBeVisible();
	});

	test('a host under the platform domain is refused with the backend reason', async ({
		page
	}) => {
		const before = await getDomains(page);
		try {
			const card = panel(page);
			await card.getByLabel('New custom domain').fill(`e2e-reject.localhost`);

			const refused = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/branding/custom-domains') &&
					r.request().method() === 'PUT' &&
					r.status() === 422
			);
			await card.getByRole('button', { name: 'Add domain' }).click();
			await refused;

			// The backend's own sentence, inline and persistent — not a generic
			// "Failed to save".
			const err = card.getByTestId('custom-domain-error');
			await expect(err).toBeVisible();
			await expect(err).toContainText('already');
			await expect(err).toContainText('routed by tenant subdomain');
			await expect(err).not.toContainText('Failed to save');

			// Nothing was registered.
			expect(await getDomains(page)).toEqual(before);
		} finally {
			await setDomains(page, before);
		}
	});

	test('a host claimed by another tenant renders the 409 reason inline', async ({ page }) => {
		const before = await getDomains(page);
		try {
			await page.route('**/api/organization/branding/custom-domains', async (route) => {
				if (route.request().method() !== 'PUT') return route.fallback();
				await route.fulfill({
					status: 409,
					contentType: 'application/json',
					body: JSON.stringify({
						detail:
							'One or more requested custom domains is already registered to another tenant.'
					})
				});
			});

			const card = panel(page);
			await card.getByLabel('New custom domain').fill('ap.taken-by-someone-else.test');
			await card.getByRole('button', { name: 'Add domain' }).click();

			const err = card.getByTestId('custom-domain-error');
			await expect(err).toBeVisible();
			await expect(err).toContainText('already registered to another tenant');

			await page.unroute('**/api/organization/branding/custom-domains');
			expect(await getDomains(page)).toEqual(before);
		} finally {
			await setDomains(page, before);
		}
	});
});
