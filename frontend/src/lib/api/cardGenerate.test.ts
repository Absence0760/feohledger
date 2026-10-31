import { readFileSync } from 'node:fs';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
	CARDABLE_INVOICE_STATUSES,
	cardGenerateSkipRows,
	type CardGenerateResponse
} from '#lib/types/cardGenerate.ts';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import { interpolate } from '#lib/i18n/interpolate.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

// The payments Cards tab's Generate dialog (`components/payments/
// GenerateCardsModal.svelte`): what it asks the server for, and how it states
// the answer. The REAL `api.ts` is driven with its SvelteKit-only seams
// replaced (the `erpConnection.test.ts` arrangement), so these pin the request
// `fetch` actually receives.
vi.mock('#lib/tenant.ts', () => ({
	getApiBase: () => 'http://api.test',
	getTenantSlug: () => 'acme'
}));
vi.mock('#lib/entity.ts', () => ({ getSelectedEntityId: () => null }));
vi.mock('#lib/i18n/store.svelte.ts', () => ({ m: (key: string) => key }));

const fetchMock = vi.fn();

function respond(body: unknown, status = 200) {
	fetchMock.mockResolvedValueOnce(
		new Response(JSON.stringify(body), {
			status,
			headers: { 'Content-Type': 'application/json' }
		})
	);
}

beforeEach(() => {
	fetchMock.mockReset();
	vi.stubGlobal('fetch', fetchMock);
	vi.stubGlobal('window', {});
	vi.stubGlobal('localStorage', {
		getItem: () => 'jwt-1',
		setItem: () => {},
		removeItem: () => {}
	});
});

const enT = (key: MessageKey, params?: Record<string, string | number>) =>
	interpolate((EN_ALL as Record<string, string>)[key], params, 'en');

describe('the invoices the Generate dialog offers', () => {
	it('narrows to exactly the statuses the mint accepts — the backend tuple, read off disk', () => {
		const source = readFileSync(
			new URL('../../../../backend/app/api/payments.py', import.meta.url).pathname,
			'utf8'
		);
		const block = source.match(/^PAYABLE_INVOICE_STATUSES = \(([\s\S]*?)\n\)/m);
		expect(block, 'PAYABLE_INVOICE_STATUSES not found in api/payments.py').not.toBeNull();
		const backend = [...block![1].matchAll(/InvoiceStatus\.(\w+)\.value/g)].map((x) => x[1]);
		expect([...CARDABLE_INVOICE_STATUSES].sort()).toEqual(backend.sort());
	});

	it('pages and searches server-side under that filter', async () => {
		const { listCardableInvoices } = await import('./cards.ts');
		respond({ items: [{ id: 'i1', invoice_number: 'INV-1', amount: '10.00', currency: 'USD' }], total: 41 });
		const page = await listCardableInvoices({ search: '  acme ', page: 2, page_size: 25 });
		expect(page).toEqual({
			items: [{ id: 'i1', invoice_number: 'INV-1', amount: '10.00', currency: 'USD' }],
			total: 41
		});
		const url = new URL(fetchMock.mock.calls[0][0] as string);
		expect(url.pathname).toBe('/api/invoices');
		expect(url.searchParams.get('status')).toBe('approved,posted_in_erp,payment_scheduled');
		expect(url.searchParams.get('search')).toBe('acme');
		expect(url.searchParams.get('page')).toBe('2');
		expect(url.searchParams.get('page_size')).toBe('25');
	});

	it('sends no search term for an empty box', async () => {
		const { listCardableInvoices } = await import('./cards.ts');
		respond({ items: [], total: 0 });
		await listCardableInvoices({ search: '   ', page: 1, page_size: 25 });
		const url = new URL(fetchMock.mock.calls[0][0] as string);
		expect(url.searchParams.has('search')).toBe(false);
	});
});

describe('generateCards', () => {
	it('posts the chosen ids and reads an older backend with no `skipped` as none', async () => {
		const { generateCards } = await import('./cards.ts');
		respond({ items: [], total: 0 }, 201);
		const res = await generateCards(['a', 'b']);
		expect(res.skipped).toEqual([]);
		const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		expect(url).toBe('http://api.test/api/cards/generate');
		expect(init.method).toBe('POST');
		expect(JSON.parse(init.body as string)).toEqual({ invoice_ids: ['a', 'b'] });
	});

	it('states a switched-off card programme in the reader’s language, not the server’s', async () => {
		const { generateCards } = await import('./cards.ts');
		respond(
			{
				detail: {
					code: 'cards_not_enabled',
					message: 'Virtual cards are not enabled. Configure in Organization Settings.',
					params: {}
				}
			},
			400
		);
		await expect(generateCards(['a'])).rejects.toMatchObject({
			code: 'cards_not_enabled',
			// The mocked `m` echoes its key: the registry localized it.
			message: 'refusal.cardsNotEnabled'
		});
	});
});

describe('the result view', () => {
	const response: CardGenerateResponse = {
		items: [],
		total: 0,
		skipped: [
			{
				invoice_id: 'i1',
				code: 'card_skip_not_payable',
				message: 'server English',
				params: { status: 'ready_for_review' }
			},
			{ invoice_id: 'ghost', code: 'card_skip_not_found', message: 'Invoice not found.', params: {} }
		]
	};

	it('labels each skip with the number the reader picked it by, in server order', () => {
		const rows = cardGenerateSkipRows(response, new Map([['i1', 'INV-1']]));
		expect(rows.map((r) => [r.invoiceId, r.invoiceNumber, r.skip.code])).toEqual([
			['i1', 'INV-1', 'card_skip_not_payable'],
			// An id the dialog never sent has no number; the view shows the id.
			['ghost', null, 'card_skip_not_found']
		]);
	});

	it('renders every skip through the shared registry, falling back to the server for an unknown code', async () => {
		const { cardGenerateSkipText } = await import('./cards.ts');
		expect(cardGenerateSkipText(response.skipped![0], enT)).toContain(
			(EN_ALL as Record<string, string>)['invoices.status.readyForReview']
		);
		expect(cardGenerateSkipText(response.skipped![0], enT)).not.toBe('server English');
		expect(
			cardGenerateSkipText(
				{ invoice_id: 'x', code: 'card_skip_from_the_future', message: 'Newer reason.', params: {} },
				enT
			)
		).toBe('Newer reason.');
	});
});
