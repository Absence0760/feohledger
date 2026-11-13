import { beforeEach, describe, expect, it, vi } from 'vitest';

// The ERP OAuth authorize call must be CREDENTIALED: its response sets the
// HttpOnly nonce cookie on the API host that the callback compares against the
// signed `state` (the login-CSRF binding, backend/app/api/erp_oauth.py). A
// plain fetch drops that cookie and every connect ends `browser_mismatch`.
//
// `#lib/api.ts` reaches `$app/env/public` through `#lib/tenant.ts`, which only
// SvelteKit resolves; the test drives the REAL `api.ts` with those seams
// replaced, so it pins what `fetch` actually receives, not just what the
// helper asked for.
vi.mock('#lib/tenant.ts', () => ({
	getApiBase: () => 'http://api.test',
	getTenantSlug: () => 'acme'
}));
vi.mock('#lib/entity.ts', () => ({ getSelectedEntityId: () => null }));
vi.mock('#lib/i18n/store.svelte.ts', () => ({ m: (key: string) => key }));

const fetchMock = vi.fn();

beforeEach(() => {
	fetchMock.mockReset();
	fetchMock.mockResolvedValue(
		new Response(JSON.stringify({ authorize_url: 'https://idp.test/authorize?state=s' }), {
			status: 200,
			headers: { 'Content-Type': 'application/json' }
		})
	);
	vi.stubGlobal('fetch', fetchMock);
	vi.stubGlobal('window', {});
	vi.stubGlobal('localStorage', {
		getItem: () => 'jwt-1',
		setItem: () => {},
		removeItem: () => {}
	});
});

describe('startErpOAuth', () => {
	it('calls authorize with credentials included, on the API host, with the bearer', async () => {
		const { startErpOAuth } = await import('./erpConnection.ts');
		const body = await startErpOAuth('quickbooks_online');
		expect(body.authorize_url).toBe('https://idp.test/authorize?state=s');
		expect(fetchMock).toHaveBeenCalledTimes(1);
		const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		expect(url).toBe('http://api.test/api/organization/erp/oauth/quickbooks_online/authorize');
		expect(init.credentials).toBe('include');
		const headers = init.headers as Record<string, string>;
		expect(headers['Authorization']).toBe('Bearer jwt-1');
		expect(headers['X-Tenant-Slug']).toBe('acme');
	});

	it('surfaces the custom-domain refusal by code, with the platform address', async () => {
		const url = 'https://acme.feohledger.com/organization?section=erp';
		fetchMock.mockResolvedValueOnce(
			new Response(
				JSON.stringify({
					detail: {
						code: 'erp_connect_platform_host',
						message: 'Connecting an ERP has to start from your FeohLedger address.',
						params: { url }
					}
				}),
				{ status: 409, headers: { 'Content-Type': 'application/json' } }
			)
		);
		const { startErpOAuth } = await import('./erpConnection.ts');
		const { ApiError } = await import('#lib/api.ts');
		const err = await startErpOAuth('xero').catch((e: unknown) => e);
		expect(err).toBeInstanceOf(ApiError);
		const apiErr = err as InstanceType<typeof ApiError>;
		expect(apiErr.status).toBe(409);
		expect(apiErr.code).toBe('erp_connect_platform_host');
		expect(apiErr.params).toEqual({ url });
	});

	it('leaves every other read uncredentialed', async () => {
		const { getErpOAuthStatus } = await import('./erpConnection.ts');
		await getErpOAuthStatus();
		const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		expect(init.credentials).toBeUndefined();
	});
});

describe('the company picker calls (decisions §355)', () => {
	// The pending connect is keyed on the same HttpOnly nonce cookie, so every
	// picker call must carry it; a plain fetch would read as `erp_pick_expired`.
	it('reads, picks and discards with credentials included', async () => {
		fetchMock.mockImplementation(
			async () =>
				new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
		);
		const { getErpPendingConnect, selectErpCompany, discardErpPendingConnect } = await import(
			'./erpConnection.ts'
		);
		await getErpPendingConnect();
		await selectErpCompany('fake-xero-tenant-2');
		await discardErpPendingConnect();
		const calls = fetchMock.mock.calls as [string, RequestInit][];
		expect(calls.map(([url, init]) => [url, init.method ?? 'GET', init.credentials])).toEqual([
			['http://api.test/api/organization/erp/oauth/pending', 'GET', 'include'],
			['http://api.test/api/organization/erp/oauth/pending/select', 'POST', 'include'],
			['http://api.test/api/organization/erp/oauth/pending', 'DELETE', 'include']
		]);
		expect(JSON.parse(String(calls[1][1].body))).toEqual({ company_id: 'fake-xero-tenant-2' });
		for (const [, init] of calls) {
			expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer jwt-1');
		}
	});
});
