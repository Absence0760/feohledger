import { API_BASE, authedTenantHeaders, currentTenantSlug, expect, signInAndWait, TENANT_ROOT_URL, test } from '../fixtures/helpers';

/**
 * /admin/webhooks — Outbound-webhook management + redelivery UI (admin only).
 *
 * Surfaces the existing backend endpoints (`backend/app/api/webhooks.py`):
 *  - POST   /api/webhooks                          → create (signing secret returned ONCE)
 *  - GET    /api/webhooks                          → list (prefix + metadata only)
 *  - PATCH  /api/webhooks/{id}                     → edit
 *  - POST   /api/webhooks/{id}/rotate-secret       → rotate (new secret ONCE, keeps id + history)
 *  - DELETE /api/webhooks/{id}                     → delete (CASCADEs deliveries)
 *  - GET    /api/webhooks/deliveries               → delivery log (status filter, paginated)
 *  - POST   /api/webhooks/deliveries/{id}/redeliver → re-enqueue a failed/dead delivery (409 if delivered)
 *
 * The signing secret is shown once in a copy-able reveal and never echoed after.
 * Login model mirrors the suite: the per-worker storage state signs the admin
 * in (the only role the endpoints allow), so the page loads without a redirect.
 */

async function apiHeaders(page: import('@playwright/test').Page) {
	return {
		...(await authedTenantHeaders(page)),
		'Content-Type': 'application/json'
	};
}

interface SubscriptionResponse {
	id: string;
	name: string;
	target_url: string;
	event_types: string[];
	secret_prefix: string;
	active: boolean;
}

interface DeliveryResponse {
	id: string;
	subscription_id: string;
	event_id: string;
	status: string;
}

/** Seed a `failed` delivery straight into the control DB. Deliveries are
 *  normally produced by the dispatch sweep, which is off in dev. */
async function seedFailedDelivery(
	page: import('@playwright/test').Page,
	subId: string,
	eventId: string
) {
	const headers = await apiHeaders(page);
	const orgId = (
		await (await page.request.get(`${API_BASE}/api/auth/me`, { headers })).json()
	)?.organization_id as string | undefined;

	const { execFileSync } = await import('node:child_process');
	execFileSync(
		'psql',
		[
			'-h',
			'localhost',
			'-U',
			'postgres',
			'-p',
			'5432',
			'-d',
			'feohledger',
			'-c',
			`INSERT INTO webhook_deliveries
			   (id, subscription_id, organization_id, event_id, event_type, payload, status, attempt_count)
			 VALUES
			   (gen_random_uuid(), '${subId}', '${orgId}', '${eventId}', 'invoice.approved',
			    '{}'::jsonb, 'failed', 1);`
		],
		{ env: { ...process.env, PGPASSWORD: 'postgres' }, stdio: ['ignore', 'pipe', 'pipe'] }
	);
}

/** Best-effort cleanup: delete a subscription we created in a test. */
async function deleteSub(page: import('@playwright/test').Page, id: string) {
	const headers = await apiHeaders(page);
	await page.request.delete(`${API_BASE}/api/webhooks/${id}`, { headers });
}

async function createSub(
	page: import('@playwright/test').Page,
	name: string
): Promise<{ subscription: SubscriptionResponse; signing_secret: string }> {
	const headers = await apiHeaders(page);
	return (await (
		await page.request.post(`${API_BASE}/api/webhooks`, {
			headers,
			data: {
				name,
				target_url: 'https://example.com/webhooks/ap',
				event_types: ['invoice.approved']
			}
		})
	).json()) as { subscription: SubscriptionResponse; signing_secret: string };
}

test.describe('/admin/webhooks (admin)', () => {
	// Deterministic explicit sign-in (don't lean on the shared storage cache) so
	// the gated page is reliably authed before each test.
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('create shows the signing secret once + lists the new subscription', async ({ page }) => {
		await page.goto('/admin/webhooks');
		await expect(page.getByRole('heading', { name: 'Webhooks', exact: true })).toBeVisible();

		const name = `e2e-hook-${Date.now()}`;

		// Open create modal + submit.
		await page.getByRole('button', { name: '+ Create webhook' }).click();
		const createModal = page.getByRole('dialog', { name: 'Create webhook' });
		await expect(createModal).toBeVisible();
		await createModal.getByRole('textbox').first().fill(name);
		await createModal.locator('input[type="url"]').fill('https://example.com/webhooks/ap');
		// invoice.approved is checked by default — submit.
		await createModal.getByRole('button', { name: 'Create' }).click();

		// The one-time reveal modal shows the FULL signing secret, warns it's
		// shown once, and offers a Copy button.
		const reveal = page.getByRole('dialog', { name: 'Webhook created' });
		await expect(reveal).toBeVisible({ timeout: 10_000 });
		const minted = reveal.getByTestId('minted-secret');
		await expect(minted).toBeVisible();
		const secret = (await minted.textContent())?.trim() ?? '';
		// The secret is a real, long token — not just the stored prefix.
		expect(secret.length).toBeGreaterThan(16);
		await expect(reveal.getByText(/shown only once/i)).toBeVisible();
		await expect(reveal.getByRole('button', { name: 'Copy' })).toBeVisible();

		// Dismiss the reveal — the secret must be gone (never re-shown).
		await reveal.getByRole('button', { name: 'Done' }).click();
		await expect(reveal).toBeHidden();
		await expect(page.getByTestId('minted-secret')).toHaveCount(0);

		// The new subscription is listed with its name + Active status, but NOT
		// the full secret (only the prefix).
		const row = page.locator('tr', { hasText: name });
		await expect(row).toBeVisible();
		await expect(row.getByText('Active', { exact: true })).toBeVisible();
		await expect(page.getByText(secret)).toHaveCount(0);

		// Cleanup via the API (resolve the id from the list).
		const headers = await apiHeaders(page);
		const list = (await (
			await page.request.get(`${API_BASE}/api/webhooks`, { headers })
		).json()) as SubscriptionResponse[];
		const created = list.find((s) => s.name === name);
		if (created) await deleteSub(page, created.id);
	});

	test('rotate mints a new secret, keeps the subscription id + its delivery history', async ({
		page
	}) => {
		const name = `e2e-rot-${Date.now()}`;
		const created = await createSub(page, name);
		const subId = created.subscription.id;
		const originalSecret = created.signing_secret;
		const originalPrefix = created.subscription.secret_prefix;

		// The whole reason rotation exists: Delete + re-create CASCADEs the
		// delivery log away, so recovering from a leaked secret used to mean
		// destroying the record of what had been delivered. Seed a delivery so
		// the test can prove rotation preserves it.
		const eventId = `e2e-rot-evt-${Date.now()}`;
		await seedFailedDelivery(page, subId, eventId);

		await page.goto('/admin/webhooks');
		const row = page.locator('tr', { hasText: name });
		await expect(row).toBeVisible();

		await row.getByRole('button', { name: `Rotate secret for ${name}` }).click();
		const dialog = page.getByRole('dialog', { name: 'Rotate signing secret' });
		await expect(dialog).toBeVisible();
		// The backend's own default is pre-selected, and the destructive
		// hard-cutover warning is absent until that option is picked.
		await expect(dialog.getByRole('radio', { name: '1 hour (default)' })).toBeChecked();
		await expect(dialog.getByTestId('cutover-warning')).toHaveCount(0);
		await dialog.getByRole('button', { name: 'Rotate secret' }).click();

		// The replacement is revealed exactly once — and it is a genuinely new
		// secret, not the one we already hold.
		const reveal = page.getByRole('dialog', { name: 'Signing secret rotated' });
		await expect(reveal).toBeVisible({ timeout: 10_000 });
		const shown = reveal.getByTestId('rotated-secret');
		await expect(shown).toBeVisible();
		const newSecret = (await shown.textContent())?.trim() ?? '';
		expect(newSecret.length).toBeGreaterThan(16);
		expect(newSecret).not.toBe(originalSecret);
		await expect(reveal.getByText(/shown only once/i)).toBeVisible();
		await expect(reveal.getByRole('button', { name: 'Copy' })).toBeVisible();
		// The overlap window is stated, not left to guess.
		await expect(reveal.getByTestId('rotation-overlap-note')).toContainText(
			'X-Webhook-Signature-Previous'
		);

		// Dismiss — the secret must be gone (never re-shown, never echoed).
		await reveal.getByRole('button', { name: 'Done' }).click();
		await expect(reveal).toBeHidden();
		await expect(page.getByTestId('rotated-secret')).toHaveCount(0);
		await expect(page.getByText(newSecret)).toHaveCount(0);

		// Server-side: SAME subscription id, a new prefix, and the delivery row
		// still there. This is the assertion the feature exists for.
		const headers = await apiHeaders(page);
		const list = (await (
			await page.request.get(`${API_BASE}/api/webhooks`, { headers })
		).json()) as SubscriptionResponse[];
		const after = list.find((s) => s.id === subId);
		expect(after).toBeDefined();
		expect(after?.secret_prefix).not.toBe(originalPrefix);

		const deliveries = (await (
			await page.request.get(`${API_BASE}/api/webhooks/deliveries?subscription_id=${subId}`, {
				headers
			})
		).json()) as DeliveryResponse[];
		expect(deliveries.some((d) => d.event_id === eventId)).toBe(true);

		// The row re-rendered onto the new prefix and shows the rotation is
		// mid-flight rather than leaving the admin to guess.
		const rotatedRow = page.locator('tr', { hasText: name });
		await expect(rotatedRow).toContainText(`${after?.secret_prefix}…`);
		await expect(rotatedRow.getByTestId('overlap-pill')).toBeVisible();

		// ...and it survives a reload, because the expiry comes off the listed
		// row rather than page-local state. This is the case that matters: the
		// admin walks away to paste the new secret into their receiver, comes
		// back, and the window is still visibly open.
		await page.reload();
		await expect(
			page.locator('tr', { hasText: name }).getByTestId('overlap-pill')
		).toBeVisible();

		await deleteSub(page, subId);
	});

	test('a hard cutover warns first, clears the in-flight pill, and each reveal starts un-copied', async ({
		page
	}) => {
		const name = `e2e-cut-${Date.now()}`;
		const created = await createSub(page, name);
		const subId = created.subscription.id;

		// The reveal's Copy button writes to the clipboard; without this the
		// component's failure path toasts instead of acknowledging.
		await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);

		await page.goto('/admin/webhooks');
		const row = page.locator('tr', { hasText: name });
		await expect(row).toBeVisible();
		const dialog = page.getByRole('dialog', { name: 'Rotate signing secret' });
		const reveal = page.getByRole('dialog', { name: 'Signing secret rotated' });

		// ── First rotation: a real overlap window, so a pill is on the row ──
		await row.getByRole('button', { name: `Rotate secret for ${name}` }).click();
		await expect(dialog).toBeVisible();
		// Nothing is in flight yet, so no re-rotation warning.
		await expect(dialog.getByTestId('rerotate-warning')).toHaveCount(0);
		await dialog.getByRole('radio', { name: '15 minutes' }).check();
		await dialog.getByRole('button', { name: 'Rotate secret' }).click();

		await expect(reveal).toBeVisible({ timeout: 10_000 });
		// Copying acknowledges — the behaviour SecretReveal now owns.
		await reveal.getByRole('button', { name: 'Copy' }).click();
		await expect(reveal.getByRole('button', { name: 'Copied' })).toBeVisible();
		await reveal.getByRole('button', { name: 'Done' }).click();
		await expect(reveal).toBeHidden();
		await expect(page.locator('tr', { hasText: name }).getByTestId('overlap-pill')).toBeVisible();

		// ── Second rotation: hard cutover ──
		await page
			.locator('tr', { hasText: name })
			.getByRole('button', { name: `Rotate secret for ${name}` })
			.click();
		await expect(dialog).toBeVisible();
		// Re-rotating during a live window evicts the secret that window was
		// protecting — the backend keeps only one previous-secret slot — so the
		// dialog says so before anything is committed.
		await expect(dialog.getByTestId('rerotate-warning')).toBeVisible();
		await dialog.getByRole('radio', { name: /Compromised/ }).check();
		// Picking the cutover surfaces its own consequence too: deliveries fail
		// until the receiver holds the new secret.
		await expect(dialog.getByTestId('cutover-warning')).toBeVisible();
		await dialog.getByRole('button', { name: 'Rotate secret' }).click();

		await expect(reveal).toBeVisible({ timeout: 10_000 });
		// The acknowledgement resets between reveals — this one has not been
		// copied yet, so it must not open reading "Copied".
		await expect(reveal.getByRole('button', { name: 'Copy' })).toBeVisible();
		await expect(reveal.getByRole('button', { name: 'Copied' })).toHaveCount(0);
		await expect(reveal.getByTestId('rotation-overlap-note')).toContainText(/no overlap window/i);
		await reveal.getByRole('button', { name: 'Done' }).click();
		await expect(reveal).toBeHidden();

		// A cutover ends the window, so the pill the first rotation raised is gone.
		await expect(page.locator('tr', { hasText: name }).getByTestId('overlap-pill')).toHaveCount(0);

		await deleteSub(page, subId);
	});

	test('delete removes the subscription (armed two-click)', async ({ page }) => {
		const name = `e2e-del-${Date.now()}`;
		const created = await createSub(page, name);
		const id = created.subscription.id;

		await page.goto('/admin/webhooks');
		const row = page.locator('tr', { hasText: name });
		await expect(row).toBeVisible();

		// Two-click armed delete: first click arms ("Confirm"), second commits.
		// `exact` so the "Edit <name>" row link (no overlap, but be safe) isn't matched.
		await row.getByRole('button', { name: 'Delete', exact: true }).click();
		await row.getByRole('button', { name: 'Confirm', exact: true }).click();

		// Row is gone from the list.
		await expect(page.locator('tr', { hasText: name })).toHaveCount(0, { timeout: 10_000 });

		// Server-side: the subscription is gone (a repeat DELETE 404s/no-ops).
		const headers = await apiHeaders(page);
		const after = (await (
			await page.request.get(`${API_BASE}/api/webhooks`, { headers })
		).json()) as SubscriptionResponse[];
		expect(after.find((s) => s.id === id)).toBeUndefined();
	});

	test('the deliveries status filter is URL-backed', async ({ page }) => {
		await page.goto('/admin/webhooks');
		await expect(page.getByRole('heading', { name: 'Deliveries', exact: true })).toBeVisible();

		// Click the "Failed" filter chip — the URL gains ?status=failed.
		await page.getByRole('button', { name: 'Failed', exact: true }).click();
		await expect(page).toHaveURL(/[?&]status=failed/);

		// Reload preserves the filter and the chip stays pressed.
		await page.reload();
		await expect(page).toHaveURL(/[?&]status=failed/);
		await expect(page.getByRole('button', { name: 'Failed', exact: true })).toHaveAttribute(
			'aria-pressed',
			'true'
		);

		// Back to "All" clears the param.
		await page.getByRole('button', { name: 'All', exact: true }).click();
		await expect(page).not.toHaveURL(/[?&]status=/);
	});

	test('delivery status cell renders the friendly label, not the raw value', async ({ page }) => {
		const name = `e2e-label-${Date.now()}`;
		const created = await createSub(page, name);
		const subId = created.subscription.id;

		// Seed a FAILED delivery directly in the control DB so a status pill renders.
		const eventId = `e2e-lbl-${Date.now()}`;
		await seedFailedDelivery(page, subId, eventId);

		await page.goto('/admin/webhooks?status=failed');
		const row = page.locator('tr', { hasText: eventId });
		await expect(row).toBeVisible({ timeout: 10_000 });

		// The status pill shows the localized "Failed" label (capitalized), NOT the
		// raw lowercase API value "failed". `exact` is case-sensitive, so it can
		// only match the friendly label routed through deliveryStatusLabel().
		const pill = row.locator('.status-pill');
		await expect(pill).toHaveText('Failed');

		// The pill is the shared `<Badge>`: `.status-pill` survives only as a
		// selector hook (this test's), and the colour comes from the tone.
		// Asserting the tone is what stops a future edit re-introducing a
		// page-local `.status-pill.failed { color: … }` — the drift the
		// primitive exists to prevent.
		await expect(pill).toHaveClass(/\bbadge\b/);
		await expect(pill).toHaveClass(/\bdanger\b/);

		await deleteSub(page, subId);
	});

	test('redeliver re-queues a failed delivery', async ({ page }) => {
		const name = `e2e-redeliver-${Date.now()}`;
		const created = await createSub(page, name);
		const subId = created.subscription.id;

		// Seed a FAILED delivery directly in the control DB so a Redeliver action
		// is rendered.
		const eventId = `e2e-evt-${Date.now()}`;
		await seedFailedDelivery(page, subId, eventId);

		await page.goto('/admin/webhooks?status=failed');
		const row = page.locator('tr', { hasText: eventId });
		await expect(row).toBeVisible({ timeout: 10_000 });
		await expect(row.getByText('failed')).toBeVisible();

		// Redeliver re-enqueues it (the target is example.com, so the inline
		// attempt fails again — but the action returns without crashing and the
		// list refreshes). A success toast confirms the re-queue went through.
		await row.getByRole('button', { name: /Redeliver/ }).click();
		await expect(page.getByText('Delivery re-queued')).toBeVisible({ timeout: 10_000 });

		await deleteSub(page, subId);
	});
});

test.describe('/admin/webhooks (clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and the API 403s them', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/admin/webhooks');
		// admin-only — the page waits for /me then bounces the clerk to root.
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'Webhooks', exact: true })).toHaveCount(0);

		// The API itself 403s a non-admin.
		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const resp = await page.request.get(`${API_BASE}/api/webhooks`, {
			headers: {
				Authorization: `Bearer ${token}`,
				'X-Tenant-Slug': currentTenantSlug()
			}
		});
		expect(resp.status()).toBe(403);
	});
});
