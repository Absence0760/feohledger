import type { Page } from '@playwright/test';

import type { CommerceSync } from '#lib/types/commerce.ts';
import { API_BASE, authedTenantHeaders, expect } from '../fixtures/helpers';

/**
 * Shared plumbing for the commerce/ suite (decisions §273) — the specs that
 * push received stock to a commerce platform.
 *
 * Two kinds of spec live here:
 *
 * - **mock** (`mock.spec.ts`) — the in-process `mock` adapter; needs nothing
 *   beyond the normal stack, so it runs in the ordinary e2e shards.
 * - **one spec per real provider** (`<provider>.spec.ts`, added with each
 *   adapter) — the real adapter against tools/fake-commerce (`pnpm commerce:up`,
 *   host port 12113). The committed backend/.env.development sets
 *   FEOH_COMMERCE_API_BASE_OVERRIDE, so the adapter calls
 *   `http://localhost:12113/<provider key>` with no per-run config. Such a spec
 *   calls `skipUnlessReachable(SERVICES.fakeCommerce)` in `beforeEach` and
 *   `resetFakeCommerce()` in `beforeAll`.
 *
 * Every spec leaves the tenant disconnected (`disconnectCommerce` in
 * `afterAll`): other suites record receipts and assume no platform.
 */

export const FAKE_COMMERCE_BASE = 'http://localhost:12113';

/** Wipe fake-commerce's in-memory state. Best-effort: when it is down the
 *  provider specs skip anyway via `skipUnlessReachable`. */
export async function resetFakeCommerce(): Promise<void> {
	try {
		await fetch(`${FAKE_COMMERCE_BASE}/__reset`, {
			method: 'POST',
			signal: AbortSignal.timeout(2_500)
		});
	} catch {
		/* fake-commerce down — the beforeEach skip gate reports it */
	}
}

/** A provider's state snapshot from fake-commerce (`GET /__state/<key>`). */
export async function fakeCommerceState(provider: string): Promise<Record<string, unknown>> {
	const resp = await fetch(`${FAKE_COMMERCE_BASE}/__state/${provider}`);
	expect(resp.ok, `fake-commerce has no state for ${provider}`).toBe(true);
	return (await resp.json()) as Record<string, unknown>;
}

async function jsonHeaders(page: Page) {
	return { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };
}

/**
 * Connect `provider`: its configuration through `PUT /api/commerce/settings`,
 * then its secrets sealed through `PUT /api/organization/credentials/commerce`
 * — the same two writers the panel uses (decisions §266). The settings
 * endpoint refuses a secret, so they are a separate argument rather than mixed
 * into `config`. Configuration first: a new platform or destination drops the
 * previously sealed secrets, and the ones given here then belong to it.
 */
export async function connectCommerce(
	page: Page,
	provider: string,
	config: Record<string, string> = {},
	secrets: Record<string, string> = {}
): Promise<void> {
	const headers = await jsonHeaders(page);
	const resp = await page.request.put(`${API_BASE}/api/commerce/settings`, {
		headers,
		data: { settings: { provider, ...config } }
	});
	expect(resp.ok(), `connect commerce ${provider} (${resp.status()})`).toBe(true);
	if (Object.keys(secrets).length) {
		const creds = await page.request.put(`${API_BASE}/api/organization/credentials/commerce`, {
			headers,
			data: { set: secrets }
		});
		expect(creds.ok(), `seal commerce credentials (${creds.status()})`).toBe(true);
	}
}

/** `PUT /api/commerce/locations`. */
export async function mapCommerceLocation(
	page: Page,
	locationId: string,
	entityLocations: Record<string, string> = {}
): Promise<void> {
	const resp = await page.request.put(`${API_BASE}/api/commerce/locations`, {
		headers: await jsonHeaders(page),
		data: { location_id: locationId, entity_locations: entityLocations }
	});
	expect(resp.ok(), `map commerce location (${resp.status()})`).toBe(true);
}

/** `DELETE /api/commerce/settings` — the afterAll cleanup every spec owes. It
 *  clears the sealed commerce secrets too, so the next spec inherits none. */
export async function disconnectCommerce(page: Page): Promise<void> {
	await page.request.delete(`${API_BASE}/api/commerce/settings`, {
		headers: await authedTenantHeaders(page)
	});
}

/** Record a receipt through the API: one line per `[poLineId, quantity]`. */
export async function recordReceipt(
	page: Page,
	poId: string,
	lines: [string, string][]
): Promise<{ id: string; gr_number: string }> {
	const resp = await page.request.post(`${API_BASE}/api/goods-receipts`, {
		headers: await jsonHeaders(page),
		data: {
			po_id: poId,
			received_date: new Date().toISOString().slice(0, 10),
			lines: lines.map(([id, q]) => ({ po_line_item_id: id, quantity_received: q }))
		}
	});
	expect(resp.status(), `record receipt: ${await resp.text()}`).toBe(201);
	return (await resp.json()) as { id: string; gr_number: string };
}

export async function receiptSyncs(page: Page, receiptId: string): Promise<CommerceSync[]> {
	const resp = await page.request.get(
		`${API_BASE}/api/commerce/syncs?goods_receipt_id=${receiptId}`,
		{ headers: await authedTenantHeaders(page) }
	);
	expect(resp.status()).toBe(200);
	return ((await resp.json()) as { items: CommerceSync[] }).items;
}

/** Poll until no row is still pending / in flight — the push runs after the
 *  receipt commits — and return the settled rows. */
export async function settledSyncs(page: Page, receiptId: string): Promise<CommerceSync[]> {
	await expect
		.poll(
			async () =>
				(await receiptSyncs(page, receiptId)).filter(
					(r) => r.status === 'pending' || r.status === 'in_flight'
				).length,
			{ message: 'the commerce push never settled' }
		)
		.toBe(0);
	return receiptSyncs(page, receiptId);
}
