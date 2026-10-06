import type { Page } from '@playwright/test';

import {
	API_BASE,
	acceptConsent,
	authedTenantHeaders,
	currentTenantSlug,
	deleteInvoicesWhere,
	expect,
	signInAndWait,
	tenantHeaders,
	test
} from '../fixtures/helpers';

/**
 * Structured e-invoicing, from the invoice detail modal.
 *
 * The backend has shipped `GET /api/invoices/{id}/einvoice?format=…` (six
 * dialects, all four roles) and `POST /api/invoices/{id}/peppol-send`
 * (admin / ap_manager / cfo) for a long time, and neither had a single caller
 * in the frontend — the capability was documented, scored as a "Have", and
 * unreachable from the product. These specs lock the UI half:
 *
 * 1. every dialect is offered and a permitted role can download one;
 * 2. a role without the PEPPOL gate sees the download menu but no send control;
 * 3. the **422** — an invoice the dialect refuses — renders the backend's own
 *    per-field reasons as a readable, persistent explanation, not a generic
 *    "export failed" toast. That path is the interesting one: the export
 *    deliberately refuses to emit a non-compliant document, so a clerk meets
 *    it routinely and must be told which fields to fix. The body is the
 *    backend's STRUCTURED error list, so the sentence on screen is the
 *    server's own — there is no code→prose map on the client to drift from it;
 * 4. PEPPOL is confirm-then-act, and a repeat send reports the EXISTING
 *    transmission (`already_sent`) rather than claiming a second document went
 *    onto the network;
 * 5. the PEPPOL block states the real transmission state **on open**, read from
 *    `GET /api/invoices/{id}/peppol-transmissions` — before that endpoint the
 *    UI could only describe a send made in the same session, so it had to stay
 *    silent, and silence reads as "not sent".
 */

const MARKER = 'E2E-EINV';

type Inv = { id: string; invoice_number: string; status: string };

/** An invoice that maps to a VALID e-invoice document: number, issue date,
 *  currency, a seller + (the org's own) buyer name, a grand total and — added
 *  separately below — at least one line. Tax is deliberately left unset: with
 *  no `tax_amount` / `tax_rate` the monetary-identity and rate-plausibility
 *  checks have nothing to disagree about, so the only thing under test is the
 *  UI. */
async function createValidInvoice(page: Page): Promise<Inv> {
	const unique = `${MARKER}-OK-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			invoice_number: unique,
			vendor: 'E2E E-Invoice Vendor',
			amount: '250.00',
			currency: 'USD',
			invoice_date: '2026-01-15'
		}
	});
	expect(resp.status(), `create failed: ${await resp.text()}`).toBe(201);
	const inv = (await resp.json()) as Inv;

	// One line whose total equals the header amount — a divergence would raise
	// a `line_total_mismatch` exception, which is a different subject.
	const lines = await page.request.put(`${API_BASE}/api/invoices/${inv.id}/line-items`, {
		headers: await authedTenantHeaders(page),
		data: [
			{
				line_number: 1,
				description: 'Consulting',
				quantity: '1.0000',
				unit_price: '250.00',
				total: '250.00'
			}
		]
	});
	expect(lines.ok(), `line items failed: ${await lines.text()}`).toBe(true);
	return inv;
}

/** An invoice that CANNOT be issued: no invoice date and no line items, so the
 *  structural guard reports `issue_date: missing; lines: missing`. */
async function createInvalidInvoice(page: Page): Promise<Inv> {
	const unique = `${MARKER}-BAD-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			invoice_number: unique,
			vendor: 'E2E E-Invoice Vendor',
			amount: '99.00',
			currency: 'USD'
		}
	});
	expect(resp.status(), `create failed: ${await resp.text()}`).toBe(201);
	return (await resp.json()) as Inv;
}

/** An invoice that satisfies the PEPPOL BIS Billing 3.0 conformance pass, not
 *  just the export's structural + tax guard. The extra requirements are
 *  BIS3's, and each is here for a named rule: a seller VAT id whose prefix also
 *  supplies the seller COUNTRY (BR-CO-26 + BR-08), the document tax breakdown
 *  and its per-line category/rate (BR-CO-14 / BR-CO-18 / BR-CO-04), the net and
 *  gross totals (BR-CO-10 / BR-CO-13), and a due date (BR-CO-25). 19% is
 *  Germany's standard rate, so the rate-plausibility check passes too. */
async function createBis3Invoice(page: Page): Promise<Inv> {
	const unique = `${MARKER}-P2P-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			invoice_number: unique,
			vendor: 'E2E E-Invoice Vendor',
			vendor_tax_id: 'DE123456789',
			amount: '297.50',
			subtotal: '250.00',
			tax_amount: '47.50',
			tax_rate: '19.00',
			currency: 'EUR',
			invoice_date: '2026-01-15',
			due_date: '2026-02-14'
		}
	});
	expect(resp.status(), `create failed: ${await resp.text()}`).toBe(201);
	const inv = (await resp.json()) as Inv;

	const lines = await page.request.put(`${API_BASE}/api/invoices/${inv.id}/line-items`, {
		headers: await authedTenantHeaders(page),
		data: [
			{
				line_number: 1,
				description: 'Consulting',
				quantity: '1.0000',
				unit_price: '250.00',
				tax: '47.50',
				total: '250.00'
			}
		]
	});
	expect(lines.ok(), `line items failed: ${await lines.text()}`).toBe(true);
	return inv;
}

function modalFor(page: Page) {
	return page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
}

async function openModal(page: Page, id: string) {
	await page.goto(`/invoices?id=${id}`);
	const modal = modalFor(page);
	await expect(modal).toBeVisible({ timeout: 15_000 });
	return modal;
}

/** Sign the tenant's seeded `manager` account in for the approve step —
 *  segregation of duties refuses the creator from also approving. */
async function managerCreds(page: Page): Promise<Record<string, string>> {
	const slug = currentTenantSlug();
	const resp = await page.request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': slug, 'Content-Type': 'application/json' },
		data: { email: `demo+manager@${slug}.localhost`, password: 'demo' }
	});
	expect(resp.ok(), `manager login failed (${resp.status()})`).toBe(true);
	const { access_token } = (await resp.json()) as { access_token: string };
	return tenantHeaders(access_token, slug);
}

async function getStatus(page: Page, id: string): Promise<string> {
	const r = await page.request.get(`${API_BASE}/api/invoices/${id}`, {
		headers: await authedTenantHeaders(page)
	});
	return ((await r.json()) as Inv).status;
}

test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${MARKER}-%'`));

test.describe('/invoices — e-invoice export', () => {
	test('offers every dialect and downloads the UBL document', async ({ page }) => {
		const inv = await createValidInvoice(page);
		const modal = await openModal(page, inv.id);

		await modal.getByTestId('einvoice-menu-toggle').click();
		const menu = modal.getByTestId('einvoice-menu');
		await expect(menu).toBeVisible();
		// All six `?format=` tokens the export route accepts.
		for (const format of ['ubl', 'cii', 'fatturapa', 'cfdi', 'nfe', 'dian']) {
			await expect(menu.getByTestId(`einvoice-format-${format}`)).toBeVisible();
		}

		const download = page.waitForEvent('download');
		await menu.getByTestId('einvoice-format-ubl').click();
		const file = await download;
		expect(file.suggestedFilename()).toBe(`einvoice-${inv.invoice_number}.xml`);

		// A successful download leaves no error region behind.
		await expect(modal.getByTestId('einvoice-error')).toHaveCount(0);
	});

	test('a national dialect downloads under its own format-tagged filename', async ({ page }) => {
		const inv = await createValidInvoice(page);
		const modal = await openModal(page, inv.id);

		await modal.getByTestId('einvoice-menu-toggle').click();
		const download = page.waitForEvent('download');
		await modal.getByTestId('einvoice-format-cii').click();
		const file = await download;
		expect(file.suggestedFilename()).toBe(`einvoice-${inv.invoice_number}-cii.xml`);
	});

	test('a refused dialect names the fields that made it non-compliant', async ({ page }) => {
		const inv = await createInvalidInvoice(page);
		const modal = await openModal(page, inv.id);

		await modal.getByTestId('einvoice-menu-toggle').click();
		await modal.getByTestId('einvoice-format-ubl').click();

		// A persistent alert region, not a toast: the reasons are the actionable
		// half of the refusal and must survive long enough to be acted on.
		const err = modal.getByTestId('einvoice-error');
		await expect(err).toBeVisible({ timeout: 10_000 });
		await expect(err).toContainText('cannot be issued as UBL 2.1');
		// The backend's own PII-free sentences, one row per field — the client
		// keeps no code→prose map, so what is on screen is what the server said.
		await expect(err).toContainText('Issue date is required');
		await expect(err).toContainText('At least one invoice line is required');
		// The field path stays visible beside each reason (it is what says WHICH
		// line or tax row is at fault) — but as a bare path, never the raw
		// `field: message` join, which is what an unparseable detail falls back to.
		await expect(err).toContainText('issue_date');
		await expect(err).not.toContainText('issue_date:');

		// No download was triggered, and the control is usable again.
		await expect(modal.getByTestId('einvoice-menu-toggle')).toBeEnabled();
	});

	test('PEPPOL send is confirm-then-act, and a repeat reports the existing transmission', async ({
		page
	}) => {
		const adminHeaders = await authedTenantHeaders(page);
		// PEPPOL runs the full EN 16931 / BIS Billing 3.0 conformance pass on top
		// of the export's own guard, so this invoice carries what the network
		// requires and the export path does not: both parties' countries, the
		// seller's VAT id, the tax breakdown and a due date.
		const inv = await createBis3Invoice(page);
		const before = (await (
			await page.request.get(`${API_BASE}/api/organization`, { headers: adminHeaders })
		).json()) as { settings?: { company?: unknown; peppol?: unknown } };
		try {
			// The send route gates on AP approval (mirrors the ERP-send gate), so
			// walk the invoice through the normal path first.
			expect(
				(
					await page.request.post(`${API_BASE}/api/invoices/${inv.id}/complete`, {
						headers: adminHeaders,
						data: {}
					})
				).status()
			).toBe(200);
			await expect
				.poll(() => getStatus(page, inv.id), { timeout: 10_000 })
				.toBe('ready_for_review');
			expect(
				(
					await page.request.post(`${API_BASE}/api/invoices/${inv.id}/approve`, {
						headers: await managerCreds(page),
						data: {}
					})
				).status()
			).toBe(200);
			await expect.poll(() => getStatus(page, inv.id), { timeout: 10_000 }).toBe('approved');

			// The sender participant id and the BUYER's country both live on the
			// org (settings are merged per top-level key, so this touches nothing
			// else). Without the sender the route 400s; without the buyer country
			// BIS3 refuses the document.
			const company = {
				...((before.settings?.company as Record<string, unknown>) ?? {}),
				country_code: 'DE'
			};
			await page.request.patch(`${API_BASE}/api/organization`, {
				headers: adminHeaders,
				data: {
					settings: {
						company,
						peppol: { sender_scheme: '9930', sender_value: 'E2E-SENDER' }
					}
				}
			});

			const modal = await openModal(page, inv.id);
			// The state is read on OPEN, from the invoice's own transmission log —
			// an approved invoice nobody has transmitted says so, rather than the
			// block staying silent about it.
			await expect(modal.getByTestId('peppol-state')).toContainText(
				'Not yet transmitted over PEPPOL.',
				{ timeout: 10_000 }
			);
			// Confirm-then-act: the first click only opens the form.
			await modal.getByTestId('peppol-send').click();
			const form = modal.getByTestId('peppol-form');
			await expect(form).toBeVisible();
			await expect(modal.getByTestId('peppol-result')).toHaveCount(0);

			await form.getByTestId('peppol-receiver-scheme').fill('9930');
			await form.getByTestId('peppol-receiver-value').fill('E2E-RECEIVER');
			await form.getByTestId('peppol-confirm').click();

			const result = modal.getByTestId('peppol-result');
			await expect(result).toBeVisible({ timeout: 15_000 });
			await expect(result).toContainText('Transmitted over PEPPOL.');

			// Re-open and send again. Transmission is idempotent at the data
			// layer, and the UI must SAY so — a second "Transmitted" would claim
			// a second document reached the network.
			await modal.getByRole('button', { name: 'Close' }).click();
			const reopened = await openModal(page, inv.id);
			// A fresh modal holds no send response, so this line is the ONLY thing
			// that can report the transmission — and it must, or a second send is
			// the user's only way to find out whether the first one happened.
			await expect(reopened.getByTestId('peppol-state')).toContainText(
				'Transmitted over PEPPOL on',
				{ timeout: 10_000 }
			);
			await reopened.getByTestId('peppol-send').click();
			await reopened.getByTestId('peppol-receiver-scheme').fill('9930');
			await reopened.getByTestId('peppol-receiver-value').fill('E2E-RECEIVER');
			await reopened.getByTestId('peppol-confirm').click();

			const second = reopened.getByTestId('peppol-result');
			await expect(second).toBeVisible({ timeout: 15_000 });
			await expect(second).toContainText('Already transmitted');
		} finally {
			// Put the org's settings back exactly as they were — this tenant is
			// shared with every other spec in the shard.
			await page.request
				.patch(`${API_BASE}/api/organization`, {
					headers: adminHeaders,
					data: {
						settings: {
							company: before.settings?.company ?? null,
							peppol: before.settings?.peppol ?? null
						}
					}
				})
				.catch(() => {});
		}
	});
});

test.describe('/invoices — e-invoice export (ap_clerk)', () => {
	// The clerk signs in through the UI, so opt out of the admin storage state.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('a clerk can download an e-invoice but is offered no PEPPOL send', async ({
		page,
		tenantClerk
	}) => {
		await acceptConsent(page);
		await signInAndWait(page, tenantClerk);

		// The clerk creates the invoice through the same authed API surface the
		// UI uses (POST /api/invoices allows admin/ap_manager/cfo — so seed it as
		// the admin first, then read it back as the clerk).
		const inv = await createValidInvoiceAsAdmin(page);

		const modal = await openModal(page, inv.id);
		await modal.getByTestId('einvoice-menu-toggle').click();
		await expect(modal.getByTestId('einvoice-format-ubl')).toBeVisible();

		// `POST /peppol-send` is admin / ap_manager / cfo — a clerk gets neither
		// the send button nor the "available once approved" note, because the
		// whole block is behind the same gate the backend enforces.
		await expect(modal.getByTestId('peppol-send')).toHaveCount(0);
		await expect(modal.getByTestId('peppol-not-sendable')).toHaveCount(0);

		const download = page.waitForEvent('download');
		await modal.getByTestId('einvoice-format-ubl').click();
		const file = await download;
		expect(file.suggestedFilename()).toBe(`einvoice-${inv.invoice_number}.xml`);
	});
});

/** Seed a valid invoice with the tenant ADMIN's credentials while the browser
 *  is signed in as another role, so the fixture does not depend on which role
 *  is under test (a clerk could create it too since entry opened to ap_clerk). */
async function createValidInvoiceAsAdmin(page: Page): Promise<Inv> {
	const slug = currentTenantSlug();
	const login = await page.request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': slug, 'Content-Type': 'application/json' },
		data: { email: `demo+admin@${slug}.localhost`, password: 'demo' }
	});
	expect(login.ok(), `admin login failed (${login.status()})`).toBe(true);
	const { access_token } = (await login.json()) as { access_token: string };
	const headers = tenantHeaders(access_token, slug);

	const unique = `${MARKER}-OK-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers,
		data: {
			invoice_number: unique,
			vendor: 'E2E E-Invoice Vendor',
			amount: '250.00',
			currency: 'USD',
			invoice_date: '2026-01-15'
		}
	});
	expect(resp.status(), `create failed: ${await resp.text()}`).toBe(201);
	const inv = (await resp.json()) as Inv;

	const lines = await page.request.put(`${API_BASE}/api/invoices/${inv.id}/line-items`, {
		headers,
		data: [
			{
				line_number: 1,
				description: 'Consulting',
				quantity: '1.0000',
				unit_price: '250.00',
				total: '250.00'
			}
		]
	});
	expect(lines.ok(), `line items failed: ${await lines.text()}`).toBe(true);
	return inv;
}
