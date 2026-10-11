import { expect, test } from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import { FAKE_ERP_BASE } from './helpers';

/**
 * The fake ERP's own request handling — what CodeQL flagged in
 * tools/fake-erp/app.py and the fix for each. These hit the container
 * directly (no backend, no tenant): the fake is a dev/CI fixture, but it
 * still listens on a port, so it must not be an open redirect, expand an
 * XML bomb, or backtrack quadratically on a query string.
 */

const SYSPRO = `${FAKE_ERP_BASE}/syspro/SYSPROWCFService/Rest`;
const QBO = `${FAKE_ERP_BASE}/qbo`;
const QBO_CLIENT_ID = 'fake-qbo-client';

test.describe('fake-erp request hardening', () => {
	// No app, no tenant, no sign-in: these talk to the container only.
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async () => {
		await skipUnlessReachable(SERVICES.fakeErp);
	});

	test('QBO authorize redirects only to a registered redirect_uri', async ({ request }) => {
		const registered = 'http://localhost:8000/api/erp/oauth/callback';
		const ok = await request.get(`${QBO}/oauth2/authorize`, {
			params: { redirect_uri: registered, state: 's1', client_id: QBO_CLIENT_ID },
			maxRedirects: 0
		});
		expect(ok.status()).toBe(302);
		expect(ok.headers()['location']).toMatch(
			/^http:\/\/localhost:8000\/api\/erp\/oauth\/callback\?code=fake-qbo-code-\d+&state=s1&realmId=/
		);

		const hostile = await request.get(`${QBO}/oauth2/authorize`, {
			params: { redirect_uri: 'https://evil.example/cb', state: 's2', client_id: QBO_CLIENT_ID },
			maxRedirects: 0
		});
		expect(hostile.status()).toBe(400);
		expect(hostile.headers()['location']).toBeUndefined();
		expect(await hostile.json()).toEqual({
			error: 'invalid_request',
			error_description: 'redirect_uri'
		});
	});

	test('SYSPRO refuses a DTD / entity payload instead of expanding it', async ({ request }) => {
		const logon = await request.get(`${SYSPRO}/Logon`, {
			params: { Operator: 'op', OperatorPassword: 'pw', CompanyId: 'C', CompanyPassword: 'cpw' }
		});
		const userId = (await logon.text()).trim();
		const bomb =
			'<?xml version="1.0"?><!DOCTYPE q [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>' +
			'<Query><TableName>&b;</TableName></Query>';

		const query = await request.get(`${SYSPRO}/Query/Query`, {
			params: { UserId: userId, BusinessObject: 'COMFND', XmlIn: bomb }
		});
		expect(query.status()).toBe(200);
		expect(await query.text()).toBe('ERROR: XmlIn is not a valid XML document');

		const post = await request.get(`${SYSPRO}/Transaction/Post`, {
			params: { UserId: userId, BusinessObject: 'APSTIN', XmlParameters: '<p/>', XmlIn: bomb }
		});
		expect(await post.text()).toBe('ERROR: XmlParameters / XmlIn is not a valid XML document');

		await request.get(`${SYSPRO}/Logoff`, { params: { UserId: userId } });
	});

	test('QBO query parses a multi-line query without regex backtracking', async ({ request }) => {
		const token = await request.post(`${QBO}/oauth2/token`, {
			headers: {
				Authorization: `Basic ${Buffer.from(`${QBO_CLIENT_ID}:fake-qbo-secret`).toString('base64')}`,
				'Content-Type': 'application/x-www-form-urlencoded'
			},
			data: await (async () => {
				const authorize = await request.get(`${QBO}/oauth2/authorize`, {
					params: {
						redirect_uri: 'http://localhost:8000/api/erp/oauth/callback',
						state: 's',
						client_id: QBO_CLIENT_ID
					},
					maxRedirects: 0
				});
				const code = new URL(authorize.headers()['location']).searchParams.get('code') ?? '';
				return `grant_type=authorization_code&code=${code}`;
			})()
		});
		expect(token.ok()).toBe(true);
		const { access_token } = await token.json();

		// The shape CodeQL named: a long run of word characters, then a
		// newline. The old `(\w+)(.*)$` pattern backtracked quadratically on
		// it and then refused the query (400); kept under uvicorn's
		// request-line limit.
		const query = `select * from ${'0'.repeat(8_000)}\nx`;
		const resp = await request.get(`${QBO}/v3/company/fake-realm-1/query`, {
			params: { query },
			headers: { Authorization: `Bearer ${access_token}` }
		});
		expect(resp.status()).toBe(200);
		expect(await resp.json()).toEqual({ QueryResponse: {} });
	});
});
