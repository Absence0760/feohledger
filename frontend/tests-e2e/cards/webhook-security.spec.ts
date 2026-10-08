import { createHmac, randomUUID } from 'node:crypto';

import type { APIRequestContext } from '@playwright/test';

import {
	API_BASE,
	expect,
	putProviderCredentials,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * Card-webhook invariant coverage (project invariant #9): every inbound
 * webhook verifies the provider HMAC over the raw body and dedupes by
 * event id, and every rejection path returns 204 silently (a distinct
 * 4xx would let an attacker enumerate card tokens / tenant slugs).
 *
 * The webhook is unauthenticated (it's a provider callback) and resolves
 * the owning tenant by `provider_card_id`, so these tests seed a card row
 * directly with a unique token, set the tenant's
 * `cards.webhook_signing_secret`, and POST raw bodies with hand-computed
 * signatures. No UI is involved — this is the money-state mutation path.
 */

const SECRET = 'wh_secret_for_e2e_card_tests_0001';
// Unique per spec-run so concurrent workers (each in their own tenant DB,
// but the webhook scans *every* tenant) never collide on the token.
const TOKEN = `e2e-wh-${randomUUID()}`;

// The signing secret is sealed in `provider_credentials`, not the settings
// JSONB, so it is set through its one writer (see `putProviderCredentials`).
function setSigningSecret(request: APIRequestContext, secret: string): Promise<void> {
	return putProviderCredentials(request, 'cards', { set: { webhook_signing_secret: secret } });
}

function clearSigningSecret(request: APIRequestContext): Promise<void> {
	return putProviderCredentials(request, 'cards', { clear: ['webhook_signing_secret'] });
}

/** Seed one active card with our unique provider token, against any
 *  seeded invoice in this worker's tenant. Returns the card id. */
function seedCard(): string {
	tenantPsql(
		`INSERT INTO virtual_cards (id, invoice_id, organization_id, card_provider, provider_card_id, amount_limit, currency, status, last_four, created_at, updated_at)
		 SELECT gen_random_uuid(), i.id, i.organization_id, 'lithic', '${TOKEN}', 1000.00, 'USD', 'active', '4242', now(), now()
		 FROM invoices i LIMIT 1`
	);
	return tenantPsql(`SELECT id FROM virtual_cards WHERE provider_card_id = '${TOKEN}'`).trim();
}

function cardStatus(cardId: string): string {
	return tenantPsql(`SELECT status FROM virtual_cards WHERE id = '${cardId}'`).trim();
}

function purge(cardId: string): void {
	// audit_log is append-only (SOX immutability trigger) — the rows the
	// handler wrote stay; they have no FK to virtual_cards, so the card
	// row deletes fine and the stranded rows don't affect later assertions
	// (every test uses a fresh card id and queries by entity_id).
	tenantPsql(`DELETE FROM card_rebates WHERE virtual_card_id = '${cardId}'`);
	tenantPsql(`DELETE FROM virtual_cards WHERE id = '${cardId}'`);
}

/** Lithic-shaped authorization charge event body. */
function chargeBody(eventId: string): string {
	return JSON.stringify({
		card_token: TOKEN,
		type: 'authorization.created',
		event_id: eventId,
		amount: 100000, // cents → $1000.00
		merchant: { descriptor: 'ACME VENDOR' }
	});
}

/** Lithic-shaped DECLINED authorization — must NOT charge the card. */
function declineBody(eventId: string): string {
	return JSON.stringify({
		card_token: TOKEN,
		type: 'authorization.decline',
		event_id: eventId,
		amount: 100000,
		merchant: { descriptor: 'ACME VENDOR' }
	});
}

function sign(rawBody: string, secret = SECRET): string {
	return createHmac('sha256', secret).update(rawBody).digest('hex');
}

test.describe('card webhook HMAC + dedup', () => {
	test.beforeEach(({ request }) => setSigningSecret(request, SECRET));
	test.afterEach(({ request }) => clearSigningSecret(request));

	test('forged signature is rejected (204) and does not move money', async ({ request }) => {
		const cardId = seedCard();
		try {
			const body = chargeBody(randomUUID());
			const resp = await request.post(`${API_BASE}/api/cards/webhook/lithic`, {
				headers: { 'Content-Type': 'application/json', 'Webhook-Signature': 'deadbeef' },
				data: body
			});
			// Silent 204 regardless — never an enumerable 4xx.
			expect(resp.status()).toBe(204);
			// State unchanged: a forged event must NOT charge the card.
			expect(cardStatus(cardId)).toBe('active');
			const rebates = tenantPsql(
				`SELECT count(*) FROM card_rebates WHERE virtual_card_id = '${cardId}'`
			).trim();
			expect(rebates).toBe('0');
		} finally {
			purge(cardId);
		}
	});

	test('missing signature is rejected (204) and does not move money', async ({ request }) => {
		const cardId = seedCard();
		try {
			const body = chargeBody(randomUUID());
			const resp = await request.post(`${API_BASE}/api/cards/webhook/lithic`, {
				headers: { 'Content-Type': 'application/json' },
				data: body
			});
			expect(resp.status()).toBe(204);
			expect(cardStatus(cardId)).toBe('active');
		} finally {
			purge(cardId);
		}
	});

	test('valid signature charges the card and leaves a PII-free audit row', async ({ request }) => {
		const cardId = seedCard();
		try {
			const eventId = randomUUID();
			const body = chargeBody(eventId);
			const resp = await request.post(`${API_BASE}/api/cards/webhook/lithic`, {
				headers: { 'Content-Type': 'application/json', 'Webhook-Signature': sign(body) },
				data: body
			});
			expect(resp.status()).toBe(204);
			expect(cardStatus(cardId)).toBe('charged');

			// amount_charged = 100000 cents / 100 = 1000.00 (exact Decimal).
			const charged = tenantPsql(
				`SELECT amount_charged FROM virtual_cards WHERE id = '${cardId}'`
			).trim();
			expect(charged).toBe('1000.00');

			// Charge wrote an append-only audit row; no PAN in it.
			const audit = tenantPsql(
				`SELECT details FROM audit_log WHERE entity_type = 'virtual_card' AND action = 'card.charged' AND entity_id = '${cardId}'`
			).trim();
			expect(audit.length).toBeGreaterThan(0);
			const details = JSON.parse(audit);
			expect(details.to).toBe('charged');
			// amount_charged is serialised as a string Decimal (never a JSON
			// float) — `1000`, not `1000.0000000001`. Assert the exact value.
			expect(typeof details.amount_charged).toBe('string');
			expect(Number(details.amount_charged)).toBe(1000);
			expect(audit).not.toMatch(/4242424242424242/);
		} finally {
			purge(cardId);
		}
	});

	test('declined authorization is signed+valid but does NOT charge the card', async ({
		request
	}) => {
		// Regression: a naive `"auth" in event_type` substring match treated
		// `authorization.decline` as a real charge, flipping the card to
		// `charged` on money that never moved (and minting a rebate on it).
		const cardId = seedCard();
		try {
			const body = declineBody(randomUUID());
			const resp = await request.post(`${API_BASE}/api/cards/webhook/lithic`, {
				headers: { 'Content-Type': 'application/json', 'Webhook-Signature': sign(body) },
				data: body
			});
			// Valid signature, but a decline is neither a charge nor a settlement.
			expect(resp.status()).toBe(204);
			expect(cardStatus(cardId)).toBe('active');
			// No charge amount, no rebate.
			const charged = tenantPsql(
				`SELECT amount_charged FROM virtual_cards WHERE id = '${cardId}'`
			).trim();
			expect(charged).toBe(''); // NULL
			const rebates = tenantPsql(
				`SELECT count(*) FROM card_rebates WHERE virtual_card_id = '${cardId}'`
			).trim();
			expect(rebates).toBe('0');
		} finally {
			purge(cardId);
		}
	});

	test('Nium charge is recorded in MAJOR units (not divided by 100)', async ({ request }) => {
		// Regression: the handler divided every charge amount by 100, but only
		// Lithic sends minor units (cents). Nium sends major units, so a $50.50
		// charge was being recorded as $0.51.
		const niumToken = `e2e-wh-nium-${randomUUID()}`;
		tenantPsql(
			`INSERT INTO virtual_cards (id, invoice_id, organization_id, card_provider, provider_card_id, amount_limit, currency, status, last_four, created_at, updated_at)
			 SELECT gen_random_uuid(), i.id, i.organization_id, 'nium', '${niumToken}', 1000.00, 'USD', 'active', '4242', now(), now()
			 FROM invoices i LIMIT 1`
		);
		const cardId = tenantPsql(
			`SELECT id FROM virtual_cards WHERE provider_card_id = '${niumToken}'`
		).trim();
		try {
			const body = JSON.stringify({
				cardHashId: niumToken,
				eventType: 'authorization',
				webhookId: randomUUID(),
				amount: 50.5, // MAJOR units → $50.50
				merchantName: 'ACME VENDOR'
			});
			const resp = await request.post(`${API_BASE}/api/cards/webhook/nium`, {
				headers: { 'Content-Type': 'application/json', 'Webhook-Signature': sign(body) },
				data: body
			});
			expect(resp.status()).toBe(204);
			expect(cardStatus(cardId)).toBe('charged');
			const charged = tenantPsql(
				`SELECT amount_charged FROM virtual_cards WHERE id = '${cardId}'`
			).trim();
			// $50.50 stays $50.50 — NOT $0.51 (the /100 bug).
			expect(charged).toBe('50.50');
		} finally {
			tenantPsql(`DELETE FROM card_rebates WHERE virtual_card_id = '${cardId}'`);
			tenantPsql(`DELETE FROM virtual_cards WHERE id = '${cardId}'`);
		}
	});

	test('replayed event id is a no-op (dedup): no double charge', async ({ request }) => {
		const cardId = seedCard();
		try {
			const eventId = randomUUID();
			const body = chargeBody(eventId);
			const sig = sign(body);

			// First delivery — applies the charge.
			const first = await request.post(`${API_BASE}/api/cards/webhook/lithic`, {
				headers: { 'Content-Type': 'application/json', 'Webhook-Signature': sig },
				data: body
			});
			expect(first.status()).toBe(204);
			expect(cardStatus(cardId)).toBe('charged');

			// Reset the card back to active to prove a *replay* of the SAME
			// event id is dropped by dedup (not just blocked by the status
			// guard): if dedup were broken, the card would re-charge to
			// 'charged'. With dedup working it stays 'active'.
			tenantPsql(
				`UPDATE virtual_cards SET status = 'active', amount_charged = NULL, charged_at = NULL WHERE id = '${cardId}'`
			);

			const replay = await request.post(`${API_BASE}/api/cards/webhook/lithic`, {
				headers: { 'Content-Type': 'application/json', 'Webhook-Signature': sig },
				data: body
			});
			expect(replay.status()).toBe(204);
			// Dedup short-circuited the replay → still active.
			expect(cardStatus(cardId)).toBe('active');
		} finally {
			purge(cardId);
		}
	});

	test('unknown card token is a silent 204 (no enumeration)', async ({ request }) => {
		const body = JSON.stringify({
			card_token: `no-such-card-${randomUUID()}`,
			type: 'authorization.created',
			event_id: randomUUID(),
			amount: 5000
		});
		const resp = await request.post(`${API_BASE}/api/cards/webhook/lithic`, {
			headers: { 'Content-Type': 'application/json', 'Webhook-Signature': sign(body) },
			data: body
		});
		expect(resp.status()).toBe(204);
	});
});
