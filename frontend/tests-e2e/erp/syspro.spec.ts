import type { APIRequestContext } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	signInAndWait,
	tenantBase,
	test
} from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import {
	FAKE_ERP_BASE,
	createApprovedInvoice,
	deleteInvoice,
	erpFailureFromAudit,
	sendToErpAndAwaitTerminal,
	setErpSettings,
	syncErpGlAccounts,
	syncErpPurchaseOrders,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * syspro adapter e2e — the REAL SYSPRO 8 e.net REST adapter
 * (backend/app/services/erp_adapters/syspro.py) against the fake ERP's
 * `/syspro` surface (tools/fake-erp, `pnpm erp:up`). The committed
 * backend/.env.development points FEOH_ERP_SYSPRO_API_BASE at the fake.
 *
 * SYSPRO is the one adapter that speaks XML both ways (COMFND queries, the
 * APSTIN posting document), so this is also the end-to-end proof that the
 * fake's hardened parser accepts exactly what the real adapter sends.
 *
 * Coverage:
 *   1. Connection — Logon / Query / Logoff, and no session is left open.
 *   2. Full send — after the supplier + ledger syncs an approved invoice
 *      posts through APSTIN; the invoice read back FROM THE FAKE carries the
 *      exact approved amount, and the adapter logged off afterwards.
 *   3. Idempotent retry — the invoice is already on file in SYSPRO with the
 *      same amount (a post whose reply was lost): the send completes as
 *      already-posted instead of double-posting or failing.
 *   4. Number collision — the same supplier + invoice number with a
 *      different amount is refused `duplicate_invoice_number`, never
 *      overwritten or accepted.
 *   5. PO sync — COMFND over PorMasterHdr / PorMasterDetail; totals are the
 *      exact sum of qty × price (the foreign price on a foreign order, plus
 *      miscellaneous charges), statuses map, currency is kept.
 */

const SYSPRO_ERP_CONFIG = {
	type: 'syspro',
	integration_method: 'direct',
	base_url: 'https://syspro.example.com/SYSPROWCFService/Rest',
	operator: 'e2e-operator',
	operator_password: 'e2e-operator-password',
	company_id: 'E2E',
	company_password: 'e2e-company-password'
};

const REST = `${FAKE_ERP_BASE}/syspro/SYSPROWCFService/Rest`;
const SUPPLIER_A = { name: 'Fake SYSPRO Supplier A', code: '0000001' };

async function openSessions(request: APIRequestContext): Promise<number> {
	return (await (await request.get(`${REST}/__sessions`)).json()).open;
}

/** Post an invoice straight into the fake, as if an earlier send had landed. */
async function postDirect(
	request: APIRequestContext,
	invoice: string,
	amount: string
): Promise<void> {
	const userId = (
		await (
			await request.get(`${REST}/Logon`, {
				params: { Operator: 'o', OperatorPassword: 'p', CompanyId: 'E2E', CompanyPassword: 'c' }
			})
		).text()
	).trim();
	const xmlIn =
		`<PostApInvoice><Item><Posting><Supplier>${SUPPLIER_A.code}</Supplier>` +
		`<Invoice>${invoice}</Invoice><InvoiceAmount>${amount}</InvoiceAmount></Posting>` +
		'<Distribution><DistributionLine><LedgerCode>6100</LedgerCode>' +
		`<DistributionValue>${amount}</DistributionValue></DistributionLine>` +
		'</Distribution></Item></PostApInvoice>';
	const resp = await request.get(`${REST}/Transaction/Post`, {
		params: { UserId: userId, BusinessObject: 'APSTIN', XmlParameters: '<p/>', XmlIn: xmlIn }
	});
	expect(await resp.text()).not.toMatch(/^ERROR/);
	await request.get(`${REST}/Logoff`, { params: { UserId: userId } });
}

/** The invoice's original value as SYSPRO holds it, or null if not on file. */
async function postedValue(request: APIRequestContext, invoice: string): Promise<string | null> {
	const userId = (
		await (
			await request.get(`${REST}/Logon`, {
				params: { Operator: 'o', OperatorPassword: 'p', CompanyId: 'E2E', CompanyPassword: 'c' }
			})
		).text()
	).trim();
	const resp = await request.get(`${REST}/Query/Query`, {
		params: {
			UserId: userId,
			BusinessObject: 'COMFND',
			XmlIn:
				'<Query><TableName>ApInvoice</TableName><Columns><Column>OrigInvValue</Column>' +
				'</Columns><Where><Expression><Column>Invoice</Column>' +
				`<Value>${invoice}</Value></Expression></Where></Query>`
		}
	});
	const xml = await resp.text();
	await request.get(`${REST}/Logoff`, { params: { UserId: userId } });
	return /<OrigInvValue>([^<]*)<\/OrigInvValue>/.exec(xml)?.[1] ?? null;
}

test.describe('/erp syspro adapter against fake-erp', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await setErpSettings(page, SYSPRO_ERP_CONFIG);
	});

	test.afterAll(async ({ browser }) => {
		const context = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await context.newPage();
		await signInAndWait(page);
		await setErpSettings(page, null);
		await context.close();
	});

	test('connection test logs on, queries and logs off again', async ({ page, request }) => {
		const before = await openSessions(request);
		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
		expect(await openSessions(request)).toBe(before);
	});

	test('full send: SYSPRO holds the exact approved amount', async ({ page, request }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const invoiceNumber = `SYS-${Date.now()}`;
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SYS',
			invoiceNumber,
			amount: '875.40',
			vendor: SUPPLIER_A.name,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
			expect(await postedValue(request, invoiceNumber)).toBe('875.40');
			expect(await openSessions(request)).toBe(0);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a retry after a lost reply completes without double-posting', async ({
		page,
		request
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const invoiceNumber = `SYS-RETRY-${Date.now()}`;
		await postDirect(request, invoiceNumber, '310.05');
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SYS-RETRY',
			invoiceNumber,
			amount: '310.05',
			vendor: SUPPLIER_A.name,
			glAccount: '6100'
		});
		try {
			// A second APSTIN post would be refused "already on file" by SYSPRO;
			// `done` means the adapter found the first one and stopped there.
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
			expect(await postedValue(request, invoiceNumber)).toBe('310.05');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('the same number with a different amount is refused, not overwritten', async ({
		page,
		request
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const invoiceNumber = `SYS-DUP-${Date.now()}`;
		await postDirect(request, invoiceNumber, '100.00');
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SYS-DUP',
			invoiceNumber,
			amount: '100.01',
			vendor: SUPPLIER_A.name,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'SYSPRO post refused: duplicate_invoice_number'
			);
			expect(await postedValue(request, invoiceNumber)).toBe('100.00');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('PO sync imports purchase orders with exact totals', async ({ page }) => {
		const { adapter, pos } = await syncErpPurchaseOrders(page);
		expect(adapter).toBe('syspro');
		const open = pos.find((p) => p.po_number === 'PO-SYS-501');
		const complete = pos.find((p) => p.po_number === 'PO-SYS-502');
		expect(open, 'PO-SYS-501 synced').toBeTruthy();
		expect(complete, 'PO-SYS-502 synced').toBeTruthy();
		// 10 × 100.00 + 1 × 250.00, and 2 × 490.25 — exact, not float-rounded.
		expect(String(open!.total)).toMatch(/^1250(\.0+)?$/);
		expect(String(complete!.total)).toMatch(/^980\.50*$/);
		expect(open!.currency).toBe('ZAR');
		expect(open!.status).toBe('open');
		expect(complete!.status).toBe('closed');

		// A USD order is totalled from MForeignPrice (4 × 100.00), never from
		// the local MPrice labelled USD.
		const foreign = pos.find((p) => p.po_number === 'PO-SYS-503');
		expect(foreign, 'PO-SYS-503 synced').toBeTruthy();
		expect(String(foreign!.total)).toMatch(/^400(\.0+)?$/);
		expect(foreign!.currency).toBe('USD');
		// 3 × 20.00 plus a 45.50 miscellaneous charge; the comment line adds nothing.
		const charged = pos.find((p) => p.po_number === 'PO-SYS-504');
		expect(charged, 'PO-SYS-504 synced').toBeTruthy();
		expect(String(charged!.total)).toMatch(/^105\.50*$/);
		// A EUR order (the company is ZAR, per GetLogonProfile) with one line
		// priced only in local MPrice has no EUR total: skipped, never summed
		// to a mixed-currency 70.00 EUR.
		expect(pos.find((p) => p.po_number === 'PO-SYS-505')).toBeUndefined();
	});
});
