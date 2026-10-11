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

	test('QBO redirect_uri must match a registered URI exactly', async ({ request }) => {
		const authorize = (redirect_uri: string, client_id = QBO_CLIENT_ID) =>
			request.get(`${QBO}/oauth2/authorize`, {
				params: { redirect_uri, state: 'x', client_id },
				maxRedirects: 0
			});

		// The loopback IP and a worktree-offset port are both registered.
		const ip = await authorize('http://127.0.0.1:8003/api/erp/oauth/callback');
		expect(ip.status()).toBe(302);
		expect(ip.headers()['location']).toMatch(/^http:\/\/127\.0\.0\.1:8003\/api\/erp\/oauth\/callback\?/);

		// Near misses are not prefix-matched: another port, another path, a
		// suffix smuggled after the registered URI, another scheme.
		for (const uri of [
			'http://localhost:8010/api/erp/oauth/callback',
			'http://localhost:8000/api/erp/oauth/callback/../../evil',
			'http://localhost:8000/api/erp/oauth/callback@evil.example',
			'https://localhost:8000/api/erp/oauth/callback'
		]) {
			const resp = await authorize(uri);
			expect(resp.status(), uri).toBe(400);
			expect(resp.headers()['location'], uri).toBeUndefined();
		}

		// The client check runs first: an unknown client never redirects.
		const client = await authorize('http://localhost:8000/api/erp/oauth/callback', 'other');
		expect(client.status()).toBe(400);
		expect(await client.json()).toEqual({ error: 'invalid_client' });
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

	test('SYSPRO refuses an external-entity (XXE) document', async ({ request }) => {
		const logon = await request.get(`${SYSPRO}/Logon`, {
			params: { Operator: 'op', OperatorPassword: 'pw', CompanyId: 'C', CompanyPassword: 'cpw' }
		});
		const userId = (await logon.text()).trim();
		const xxe =
			'<?xml version="1.0"?><!DOCTYPE q [<!ENTITY f SYSTEM "file:///etc/passwd">]>' +
			'<Query><TableName>&f;</TableName></Query>';

		const resp = await request.get(`${SYSPRO}/Query/Query`, {
			params: { UserId: userId, BusinessObject: 'COMFND', XmlIn: xxe }
		});
		const body = await resp.text();
		expect(body).toBe('ERROR: XmlIn is not a valid XML document');
		expect(body).not.toContain('root:');

		await request.get(`${SYSPRO}/Logoff`, { params: { UserId: userId } });
	});

	test('SYSPRO still posts and queries well-formed documents', async ({ request }) => {
		const logon = await request.get(`${SYSPRO}/Logon`, {
			params: { Operator: 'op', OperatorPassword: 'pw', CompanyId: 'C', CompanyPassword: 'cpw' }
		});
		const userId = (await logon.text()).trim();
		const invoice = `HARD-${Date.now()}`;
		const xmlIn =
			'<PostApInvoice><Item><Posting><Supplier>0000001</Supplier>' +
			`<Invoice>${invoice}</Invoice><InvoiceAmount>100.25</InvoiceAmount></Posting>` +
			'<Distribution><DistributionLine><LedgerCode>6100</LedgerCode>' +
			'<DistributionValue>100.25</DistributionValue></DistributionLine>' +
			'</Distribution></Item></PostApInvoice>';

		const post = await request.get(`${SYSPRO}/Transaction/Post`, {
			params: { UserId: userId, BusinessObject: 'APSTIN', XmlParameters: '<p/>', XmlIn: xmlIn }
		});
		const posted = await post.text();
		expect(posted).not.toMatch(/^ERROR/);
		expect(posted).toContain(`<Invoice>${invoice}</Invoice>`);

		const query = await request.get(`${SYSPRO}/Query/Query`, {
			params: {
				UserId: userId,
				BusinessObject: 'COMFND',
				XmlIn:
					'<Query><TableName>ApInvoice</TableName><Columns><Column>Invoice</Column>' +
					'<Column>OrigInvValue</Column></Columns><Where><Expression>' +
					`<Column>Invoice</Column><Value>${invoice}</Value></Expression></Where></Query>`
			}
		});
		const rows = await query.text();
		expect(rows).toContain(`<Invoice>${invoice}</Invoice><OrigInvValue>100.25</OrigInvValue>`);
		expect(rows).toContain('<RowsReturned>1</RowsReturned>');

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

	test('QBO query keeps its normal shapes after the regex change', async ({ request }) => {
		const authorize = await request.get(`${QBO}/oauth2/authorize`, {
			params: {
				redirect_uri: 'http://localhost:8000/api/erp/oauth/callback',
				state: 's',
				client_id: QBO_CLIENT_ID
			},
			maxRedirects: 0
		});
		const code = new URL(authorize.headers()['location']).searchParams.get('code') ?? '';
		const token = await request.post(`${QBO}/oauth2/token`, {
			headers: {
				Authorization: `Basic ${Buffer.from(`${QBO_CLIENT_ID}:fake-qbo-secret`).toString('base64')}`,
				'Content-Type': 'application/x-www-form-urlencoded'
			},
			data: `grant_type=authorization_code&code=${code}`
		});
		const { access_token } = await token.json();
		const run = (query: string) =>
			request.get(`${QBO}/v3/company/fake-realm-1/query`, {
				params: { query },
				headers: { Authorization: `Bearer ${access_token}` }
			});

		// The entity name still ends at the first non-word character.
		const vendors = await (await run('select * from Vendor')).json();
		expect(Object.keys(vendors.QueryResponse)).toEqual(['Vendor']);
		expect(vendors.QueryResponse.Vendor.length).toBeGreaterThan(0);

		// A page past the single fixture page is empty, not an error.
		const page2 = await run('select * from Vendor STARTPOSITION 1000 MAXRESULTS 10');
		expect(await page2.json()).toEqual({ QueryResponse: {} });

		// A DocNumber filter with an escaped quote matches no stored bill.
		const bill = await run("select * from Bill where DocNumber = 'O\\'Brien-1'");
		expect(bill.status()).toBe(200);
		expect(await bill.json()).toEqual({ QueryResponse: {} });

		// Not a select at all is still a parser error.
		const bad = await run('delete from Vendor');
		expect(bad.status()).toBe(400);
	});
});
