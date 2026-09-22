import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /organization → Custom Domains panel (white-label vanity hostnames).
 *
 * Manages settings.brand.custom_domains — the list the backend resolver matches
 * an inbound Host against (JWT org-claim cross-check still gates access). The
 * panel lists current domains, adds one (validated), and removes one (armed
 * confirm). Each test cleans the list back to empty via the API in finally so
 * runs are independent and don't leak a host that other specs would collide on.
 *
 * `?section=custom-domains` because the route shows one panel at a time now
 * (the slug is part of its URL contract). A `page.reload()` below keeps the
 * query string, so the panel survives one.
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

test.describe('/organization custom domains', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/organization?section=custom-domains');
	});

	test('Custom Domains section renders', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Custom Domains' })).toBeVisible();
	});

	test('add a custom domain through the UI and it round-trips', async ({ page }) => {
		const before = await getDomains(page);
		const host = `e2e-${Date.now() % 1000000}.acme.test`;
		try {
			const card = page.locator('section.card', {
				has: page.getByRole('heading', { name: 'Custom Domains' })
			});
			await card.getByLabel('New custom domain').fill(host);

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/branding/custom-domains') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await card.getByRole('button', { name: 'Add domain' }).click();
			await saved;

			// The new host appears in the rendered list…
			await expect(card.getByText(host, { exact: true })).toBeVisible();
			// …and is actually persisted.
			expect(await getDomains(page)).toContain(host);
		} finally {
			await setDomains(page, before);
		}
	});

	test('remove a custom domain (armed confirm) drops it', async ({ page }) => {
		const before = await getDomains(page);
		const host = `e2e-rm-${Date.now() % 1000000}.acme.test`;
		try {
			// Seed via API so the test starts from a known one-domain state.
			await setDomains(page, [...before, host]);
			await page.reload();

			const card = page.locator('section.card', {
				has: page.getByRole('heading', { name: 'Custom Domains' })
			});
			const removeBtn = card.getByRole('button', { name: `Remove custom domain ${host}` });
			// First click arms (label flips to "Confirm remove"); second commits.
			await removeBtn.click();
			await expect(removeBtn).toHaveText('Confirm remove');

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/branding/custom-domains') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await removeBtn.click();
			await saved;

			await expect(card.getByText(host, { exact: true })).toHaveCount(0);
			expect(await getDomains(page)).not.toContain(host);
		} finally {
			await setDomains(page, before);
		}
	});

	test('invalid hostname surfaces an inline error and does not save', async ({ page }) => {
		const before = await getDomains(page);
		try {
			const card = page.locator('section.card', {
				has: page.getByRole('heading', { name: 'Custom Domains' })
			});
			await card.getByLabel('New custom domain').fill('not a hostname');
			await card.getByRole('button', { name: 'Add domain' }).click();

			// Client-side validation toast; no PUT fired, list unchanged.
			await expect(page.getByText(/valid hostname/i)).toBeVisible();
			expect(await getDomains(page)).toEqual(before);
		} finally {
			await setDomains(page, before);
		}
	});
});
