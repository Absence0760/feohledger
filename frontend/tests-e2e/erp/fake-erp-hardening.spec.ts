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

	test('QBO auth codes are single-use and refresh tokens rotate', async ({ request }) => {
		const basic = `Basic ${Buffer.from(`${QBO_CLIENT_ID}:fake-qbo-secret`).toString('base64')}`;
		const token = (data: string, authorization = basic) =>
			request.post(`${QBO}/oauth2/token`, {
				headers: {
					Authorization: authorization,
					'Content-Type': 'application/x-www-form-urlencoded'
				},
				data
			});
		const authorize = await request.get(`${QBO}/oauth2/authorize`, {
			params: {
				redirect_uri: 'http://localhost:8000/api/erp/oauth/callback',
				state: 's',
				client_id: QBO_CLIENT_ID
			},
			maxRedirects: 0
		});
		const code = new URL(authorize.headers()['location']).searchParams.get('code') ?? '';

		// A wrong client secret never gets a token, even with a live code.
		const wrong = `Basic ${Buffer.from(`${QBO_CLIENT_ID}:nope`).toString('base64')}`;
		expect((await token(`grant_type=authorization_code&code=${code}`, wrong)).status()).toBe(401);

		const first = await token(`grant_type=authorization_code&code=${code}`);
		expect(first.status()).toBe(200);
		const issued = await first.json();

		// The same code a second time is spent.
		const replay = await token(`grant_type=authorization_code&code=${code}`);
		expect(replay.status()).toBe(400);
		expect(await replay.json()).toEqual({ error: 'invalid_grant' });

		// A refresh rotates: the new refresh token works, the old one is dead.
		const rotated = await token(`grant_type=refresh_token&refresh_token=${issued.refresh_token}`);
		expect(rotated.status()).toBe(200);
		const next = await rotated.json();
		expect(next.refresh_token).not.toBe(issued.refresh_token);
		const stale = await token(`grant_type=refresh_token&refresh_token=${issued.refresh_token}`);
		expect(stale.status()).toBe(400);

		expect((await token('grant_type=password')).status()).toBe(400);
	});

	test('SYSPRO checks the session and business object before parsing', async ({ request }) => {
		const bomb =
			'<?xml version="1.0"?><!DOCTYPE q [<!ENTITY a "aaaa">]><Query><TableName>&a;</TableName></Query>';
		const expired = 'ERROR: The supplied UserID is invalid, or your session has expired';

		// No session: refused on the session check, whatever the document.
		for (const path of ['Query/Query', 'Transaction/Post']) {
			const resp = await request.get(`${SYSPRO}/${path}`, {
				params: { UserId: 'not-a-session', BusinessObject: 'COMFND', XmlIn: bomb }
			});
			expect(await resp.text(), path).toBe(expired);
		}

		const logon = await request.get(`${SYSPRO}/Logon`, {
			params: { Operator: 'op', OperatorPassword: 'pw', CompanyId: 'C', CompanyPassword: 'cpw' }
		});
		const userId = (await logon.text()).trim();

		// Wrong business object for the route: refused before the XML is read.
		const query = await request.get(`${SYSPRO}/Query/Query`, {
			params: { UserId: userId, BusinessObject: 'APSTIN', XmlIn: bomb }
		});
		expect(await query.text()).toBe('ERROR: Business object not supported by fake-erp');

		// A logged-off session is dead.
		await request.get(`${SYSPRO}/Logoff`, { params: { UserId: userId } });
		const after = await request.get(`${SYSPRO}/Query/Query`, {
			params: { UserId: userId, BusinessObject: 'COMFND', XmlIn: '<Query/>' }
		});
		expect(await after.text()).toBe(expired);
	});

	test('SYSPRO refuses unbalanced, unknown and duplicate postings', async ({ request }) => {
		const logon = await request.get(`${SYSPRO}/Logon`, {
			params: { Operator: 'op', OperatorPassword: 'pw', CompanyId: 'C', CompanyPassword: 'cpw' }
		});
		const userId = (await logon.text()).trim();
		const invoice = `REFUSE-${Date.now()}`;
		const doc = (supplier: string, amount: string, ledger: string, value: string) =>
			`<PostApInvoice><Item><Posting><Supplier>${supplier}</Supplier>` +
			`<Invoice>${invoice}</Invoice><InvoiceAmount>${amount}</InvoiceAmount></Posting>` +
			`<Distribution><DistributionLine><LedgerCode>${ledger}</LedgerCode>` +
			`<DistributionValue>${value}</DistributionValue></DistributionLine>` +
			'</Distribution></Item></PostApInvoice>';
		const post = async (xmlIn: string) =>
			(
				await request.get(`${SYSPRO}/Transaction/Post`, {
					params: { UserId: userId, BusinessObject: 'APSTIN', XmlParameters: '<p/>', XmlIn: xmlIn }
				})
			).text();

		expect(await post(doc('9999999', '10.00', '6100', '10.00'))).toBe(
			"ERROR: Supplier '9999999' not on file"
		);
		expect(await post(doc('0000001', '10.00', '0000', '10.00'))).toBe(
			'ERROR: Ledger code not on file'
		);
		// Exact decimals: one cent short does not balance.
		expect(await post(doc('0000001', '10.00', '6100', '9.99'))).toBe(
			'ERROR: Distribution does not balance to the invoice amount'
		);
		expect(await post('<PostApInvoice><Item/></PostApInvoice>')).toBe(
			'ERROR: Posting element missing'
		);

		expect(await post(doc('0000001', '10.00', '6100', '10.00'))).not.toMatch(/^ERROR/);
		expect(await post(doc('0000001', '10.00', '6100', '10.00'))).toBe(
			`ERROR: Invoice '${invoice}' already on file`
		);

		await request.get(`${SYSPRO}/Logoff`, { params: { UserId: userId } });
	});

	test('QBO API refuses a missing token and the wrong realm', async ({ request }) => {
		const company = (realm: string, authorization?: string) =>
			request.get(`${QBO}/v3/company/${realm}/companyinfo/${realm}`, {
				headers: authorization ? { Authorization: authorization } : {}
			});

		const none = await company('fake-realm-1');
		expect(none.status()).toBe(401);
		expect((await none.json()).Fault.Error[0].code).toBe('3200');
		expect((await company('fake-realm-1', 'Bearer made-up')).status()).toBe(401);

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
		const bearer = `Bearer ${(await token.json()).access_token}`;

		expect((await company('fake-realm-1', bearer)).status()).toBe(200);
		// A valid token for one company does not open another.
		const other = await company('someone-elses-realm', bearer);
		expect(other.status()).toBe(403);
		expect((await other.json()).Fault.Error[0].code).toBe('3100');
	});
});
