import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /organization → Data Residency panel (GDPR/CCPA region pin).
 *
 * Manages settings.residency.region plus the backend's advisory
 * configured-vs-deployed `alignment` verdict (JWT org-claim cross-check via
 * `get_tenant` still gates access). The panel is structurally identical to
 * the Custom Domains panel on the same route — region select, a save button
 * that's disabled until the selection differs from what's persisted, and an
 * alignment box that branches three ways on the verdict. The dev backend
 * declares `FEOH_DEPLOYED_REGION=us` (`backend/.env.development`), so pinning
 * `us` reads as aligned and pinning anything else reads as misaligned without
 * needing to reconfigure the backend under test. Each test restores the
 * region via the API in `finally` so runs are independent.
 *
 * `?section=residency` because the route shows one panel at a time now (the
 * slug is part of its URL contract). A `page.reload()` below keeps the query
 * string, so the panel survives one.
 */

interface ResidencyResponse {
	region: string;
	default_region: string;
	supported_regions: string[];
	alignment: { status: string; deployed_region: string | null };
}

async function getResidency(page: import('@playwright/test').Page): Promise<ResidencyResponse> {
	const resp = await page.request.get(`${API_BASE}/api/organization/data-residency`, {
		headers: await authedTenantHeaders(page)
	});
	return (await resp.json()) as ResidencyResponse;
}

async function setRegion(page: import('@playwright/test').Page, region: string): Promise<void> {
	await page.request.put(`${API_BASE}/api/organization/data-residency`, {
		headers: await authedTenantHeaders(page),
		data: { region }
	});
}

test.describe('/organization data residency', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/organization?section=residency');
	});

	function panel(page: import('@playwright/test').Page) {
		return page.locator('section.card', {
			has: page.getByRole('heading', { name: 'Data Residency' })
		});
	}

	test('Data Residency section renders with the current alignment verdict', async ({ page }) => {
		const before = await getResidency(page);
		const card = panel(page);
		await expect(card.getByText('Pin where this tenant')).toBeVisible();
		await expect(card.getByText('Deployment alignment')).toBeVisible();
		await expect(card.getByText('Advisory only — nothing is blocked by this check.')).toBeVisible();
		// Baseline: the dev backend runs 'us', so an unpinned/default tenant
		// (or one already pinned to 'us') reads as aligned.
		if (before.region === before.alignment.deployed_region) {
			await expect(card.locator('.residency-alignment.ok')).toBeVisible();
		}
	});

	test('pinning a region other than the deployed one persists and renders misaligned', async ({
		page
	}) => {
		const before = await getResidency(page);
		try {
			const card = panel(page);
			// Pick a region that isn't the deployed one (us) so the alignment
			// verdict is guaranteed to flip regardless of the starting region.
			const target = before.alignment.deployed_region === 'eu' ? 'ca' : 'eu';
			await card.getByLabel('Region').selectOption(target);

			const saveBtn = card.getByRole('button', { name: 'Save Region' });
			await expect(saveBtn).toBeEnabled();

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/data-residency') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await saveBtn.click();
			await saved;

			await expect(card.locator('.residency-alignment.warn')).toBeVisible();
			await expect(card.getByText(/but this stack runs in/)).toBeVisible();
			await expect(saveBtn).toBeDisabled();

			const after = await getResidency(page);
			expect(after.region).toBe(target);
			expect(after.alignment.status).toBe('misaligned');
		} finally {
			await setRegion(page, before.region);
		}
	});

	test('pinning the deployed region persists and renders aligned', async ({ page }) => {
		const before = await getResidency(page);
		try {
			// Start from a known-misaligned state via the API, then use the UI to
			// pin back to the deployed region and confirm it flips to aligned.
			const deployed = before.alignment.deployed_region ?? 'us';
			const other = deployed === 'eu' ? 'ca' : 'eu';
			await setRegion(page, other);
			await page.reload();

			const card = panel(page);
			await expect(card.locator('.residency-alignment.warn')).toBeVisible();

			await card.getByLabel('Region').selectOption(deployed);
			const saveBtn = card.getByRole('button', { name: 'Save Region' });

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/data-residency') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await saveBtn.click();
			await saved;

			await expect(card.locator('.residency-alignment.ok')).toBeVisible();
			await expect(card.getByText('The commitment is honoured today.')).toBeVisible();

			const after = await getResidency(page);
			expect(after.region).toBe(deployed);
			expect(after.alignment.status).toBe('aligned');
		} finally {
			await setRegion(page, before.region);
		}
	});
});
