import type { APIRequestContext } from '@playwright/test';

import {
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
	erpReferenceFromAudit,
	sendToErpAndAwaitTerminal,
	setErpSettings,
	syncErpGlAccounts,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * sage_accounting_za adapter e2e — the REAL Sage Business Cloud Accounting
 * (South Africa) adapter (backend/app/services/erp_adapters/sage_accounting_za.py)
 * against the fake ERP's `/sageza` surface (tools/fake-erp, `pnpm erp:up`).
 * The committed backend/.env.development points FEOH_ERP_SAGE_ZA_API_BASE at
 * the fake.
 *
 * The point of this suite is the money rules a South African AP team relies
 * on, end to end:
 *
 *   1. Connection — API key + company id + basic auth reach the company.
 *   2. Full send — a ZAR invoice stating 15% VAT posts VAT-inclusive; the
 *      supplier invoice read back FROM THE FAKE carries the exact gross and
 *      our invoice number.
 *   3. VAT mismatch — VAT stated that is not what the 15% split books is
 *      refused (`tax_mismatch`), never posted with Sage's figure instead.
 *   4. VAT not stated — on a 15% account, an invoice stating no VAT is
 *      refused (`tax_not_stated`): input VAT the supplier never charged is
 *      never claimed.
 *   5. Currency — a USD invoice is refused for a ZAR company
 *      (`currency_not_supported`), never booked as if it were rand.
 */

const SAGE_ZA_ERP_CONFIG = {
	type: 'sage_accounting_za',
	integration_method: 'direct',
	api_key: 'e2e-sage-za-api-key',
	username: 'ap@e2e.test',
	password: 'e2e-sage-za-password',
	company_id: '4711',
	company_currency: 'ZAR'
};

const SUPPLIER = 'Fake Sage ZA Supplier A';
const PROVIDER = 'Sage Accounting (ZA)';

/** A supplier invoice straight from the fake, as Sage holds it. */
async function sageInvoice(request: APIRequestContext, id: string) {
	const resp = await request.get(`${FAKE_ERP_BASE}/sageza/api/2.0.0/SupplierInvoice/Get/${id}`, {
		params: { apikey: 'spec', companyid: '4711', includeDetail: 'true' },
		headers: { Authorization: `Basic ${Buffer.from('spec:spec').toString('base64')}` }
	});
	expect(resp.ok(), await resp.text()).toBe(true);
	return resp.json();
}

test.describe('/erp sage_accounting_za adapter against fake-erp', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await setErpSettings(page, SAGE_ZA_ERP_CONFIG);
	});

	test.afterAll(async ({ browser }) => {
		const context = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await context.newPage();
		await signInAndWait(page);
		await setErpSettings(page, null);
		await context.close();
	});

	test('connection test reaches the Sage company', async ({ page }) => {
		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
	});

	test('full send: a ZAR invoice with 15% VAT posts the exact gross', async ({
		page,
		request
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SAGEZA',
			amount: '1150.00',
			taxAmount: '150.00',
			currency: 'ZAR',
			vendor: SUPPLIER,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
			const posted = await sageInvoice(request, await erpReferenceFromAudit(page, inv.id));
			expect(posted.SupplierId).toBe(101);
			// The fake only saves lines carrying a known tax type and totals
			// them, so the VAT-inclusive gross landing exactly is the proof.
			expect(posted.Total).toBe('1150.00');
			expect([posted.DocumentNumber, posted.Reference]).toContain(inv.invoice_number);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('VAT stated that the 15% split does not book is refused', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SAGEZA-VAT',
			amount: '1150.00',
			taxAmount: '100.00',
			currency: 'ZAR',
			vendor: SUPPLIER,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(`${PROVIDER} post refused: tax_mismatch`);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('no VAT stated on a 15% account is refused, not claimed', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SAGEZA-NOVAT',
			amount: '1150.00',
			currency: 'ZAR',
			vendor: SUPPLIER,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				`${PROVIDER} post refused: tax_not_stated`
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a USD invoice is refused for a ZAR company', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SAGEZA-USD',
			amount: '1150.00',
			taxAmount: '150.00',
			currency: 'USD',
			vendor: SUPPLIER,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				`${PROVIDER} post refused: currency_not_supported`
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});
