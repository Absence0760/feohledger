import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	test
} from '../fixtures/helpers';

/**
 * /organization → Branding panel, **Email Link Base URL** field
 * (`settings.brand.tenant_url_template`).
 *
 * The per-tenant base URL stamped into outbound email — approval requests,
 * supplier-portal invites, signup confirmations. Empty means "use the platform
 * default" (`FEOH_TENANT_URL_TEMPLATE`, surfaced to the panel through the public
 * `GET /api/public-config`), which is why the panel renders the resolved default
 * rather than leaving an admin to guess what clearing the field does.
 *
 * It saves with the rest of the Branding section (one "Save Branding" button —
 * the field is not its own panel), so these tests drive that button and read the
 * result back through `GET /api/organization/branding`. Every test restores the
 * whole brand payload in `finally` so a worker's tenant is left as found.
 *
 * `?section=branding` because the route shows one panel at a time now (the slug
 * is part of its URL contract). The `page.reload()` calls below keep the query
 * string, so the panel survives one.
 */

interface BrandPayload {
	product_name: string;
	logo_url: string;
	accent_color: string;
	accent_strong_color: string;
	support_url: string;
	legal_url: string;
	tenant_url_template: string;
}

async function getBranding(page: import('@playwright/test').Page): Promise<BrandPayload> {
	const resp = await page.request.get(`${API_BASE}/api/organization/branding`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	return (await resp.json()) as BrandPayload;
}

async function putBranding(
	page: import('@playwright/test').Page,
	body: BrandPayload
): Promise<number> {
	const resp = await page.request.put(`${API_BASE}/api/organization/branding`, {
		headers: await authedTenantHeaders(page),
		data: body
	});
	return resp.status();
}

function brandingCard(page: import('@playwright/test').Page) {
	return page.locator('section.card#org-branding');
}

test.describe('/organization tenant URL override', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/organization?section=branding');
	});

	test('shows the effective platform default while the override is empty', async ({ page }) => {
		const before = await getBranding(page);
		try {
			// Start from a known-empty override so the default branch renders.
			if (before.tenant_url_template) {
				await putBranding(page, { ...before, tenant_url_template: '' });
				await page.reload();
			}

			const card = brandingCard(page);
			const field = card.getByLabel('Email Link Base URL');
			await expect(field).toBeVisible();
			await expect(field).toHaveValue('');

			// The platform default is `http://{slug}.localhost:7777` in dev, so the
			// resolved default must name THIS worker's tenant host — that's the
			// whole point of rendering it (an admin can see what blank means).
			const cfg = await page.request.get(`${API_BASE}/api/public-config`);
			const template = ((await cfg.json()) as { tenant_url_template: string })
				.tenant_url_template;
			const slug = new URL(page.url()).hostname.split('.')[0];
			const expected = template.replaceAll('{slug}', slug);

			const hint = card.getByTestId('tenant-url-effective');
			await expect(hint).toContainText('platform default');
			await expect(hint).toContainText(expected);
			// The placeholder is the resolved default too, not a made-up example.
			await expect(field).toHaveAttribute('placeholder', expected);
		} finally {
			await putBranding(page, before);
		}
	});

	test('an admin can save an override and it round-trips', async ({ page }) => {
		const before = await getBranding(page);
		const override = 'https://ap.acme-e2e.test';
		try {
			const card = brandingCard(page);
			await card.getByLabel('Email Link Base URL').fill(override);

			// The field saves with the rest of the Branding section.
			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/branding') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await card.getByRole('button', { name: 'Save Branding' }).click();
			await saved;

			// The hint flips from "platform default" to the override's own URL…
			const hint = card.getByTestId('tenant-url-effective');
			await expect(hint).toContainText(override);
			await expect(hint).not.toContainText('platform default');

			// …and it is actually persisted.
			expect((await getBranding(page)).tenant_url_template).toBe(override);

			// A reload re-hydrates the field from settings.brand rather than
			// silently falling back to the platform default.
			await page.reload();
			await expect(brandingCard(page).getByLabel('Email Link Base URL')).toHaveValue(override);
		} finally {
			await putBranding(page, before);
		}
	});

	test('clearing the override falls back to the platform default', async ({ page }) => {
		const before = await getBranding(page);
		try {
			await putBranding(page, {
				...before,
				tenant_url_template: 'https://ap.acme-e2e-clear.test'
			});
			await page.reload();

			const card = brandingCard(page);
			await card.getByLabel('Email Link Base URL').fill('');

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/branding') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await card.getByRole('button', { name: 'Save Branding' }).click();
			await saved;

			await expect(card.getByTestId('tenant-url-effective')).toContainText('platform default');
			expect((await getBranding(page)).tenant_url_template).toBe('');
		} finally {
			await putBranding(page, before);
		}
	});

	test('a non-URL override is refused client-side and never reaches the API', async ({ page }) => {
		const before = await getBranding(page);
		try {
			const card = brandingCard(page);
			await card.getByLabel('Email Link Base URL').fill('acme.example.com');

			let put = false;
			page.on('request', (r) => {
				if (r.url().endsWith('/api/organization/branding') && r.method() === 'PUT') put = true;
			});
			await card.getByRole('button', { name: 'Save Branding' }).click();

			await expect(page.getByText('must be an http(s) URL')).toBeVisible();
			expect(put).toBe(false);
			expect((await getBranding(page)).tenant_url_template).toBe(before.tenant_url_template);
		} finally {
			await putBranding(page, before);
		}
	});
});

test.describe('/organization tenant URL override — non-admin', () => {
	test('a clerk cannot mutate the override', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		// The branding READ is open to any authed user, so the panel renders and
		// the field is populated — the mutate is what's gated.
		const before = await getBranding(page);

		// The endpoint itself refuses the role…
		expect(
			await putBranding(page, { ...before, tenant_url_template: 'https://clerk.example.test' })
		).toBe(403);

		// …and the page no longer lets a clerk reach that refusal at all. It used
		// to render a fully editable form whose Save 403'd, which reads as a
		// broken product rather than a permission boundary; /organization now
		// goes read-only for a non-admin (one disabled <fieldset> around every
		// panel) with a banner saying why. The server gate above is unchanged
		// and remains the authority — this is the UI catching up to it.
		await page.goto('/organization?section=branding');

		await expect(page.getByTestId('org-readonly-banner')).toBeVisible();
		const card = brandingCard(page);
		await expect(card.getByLabel('Email Link Base URL')).toBeDisabled();
		await expect(card.getByRole('button', { name: 'Save Branding' })).toBeDisabled();
		expect((await getBranding(page)).tenant_url_template).toBe(before.tenant_url_template);
	});
});
