import { API_BASE, authedTenantHeaders, expect, tenantPsql, test } from '../fixtures/helpers';

const CSV_HEADER = 'external_txn_id,date,posted_date,merchant,amount,currency,card_last_four,card_ref';

/** Unlink any expense created from these txns, then delete the txns. */
function deleteCardTxnByExternalId(ext: string): void {
	tenantPsql(
		`UPDATE expenses SET card_transaction_id=NULL WHERE card_transaction_id IN (SELECT id FROM corporate_card_transactions WHERE external_txn_id='${ext}')`
	);
	tenantPsql(`DELETE FROM corporate_card_transactions WHERE external_txn_id='${ext}'`);
}

async function importCsv(page: import('@playwright/test').Page, csv: string) {
	const headers = await authedTenantHeaders(page);
	return page.request.post(`${API_BASE}/api/corporate-card-transactions/import-csv`, {
		headers, // Authorization + X-Tenant-Slug; Playwright sets the multipart Content-Type
		multipart: {
			file: { name: 'cards.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) }
		}
	});
}

test.describe('/expenses — Cards tab (WF4)', () => {
	test('imports a CSV, re-import dedupes, rows render on the Cards tab', async ({ page }) => {
		const ext = `e2e-card-${Date.now()}`;
		const csv = `${CSV_HEADER}\n${ext},2026-02-01,2026-02-02,E2E Card Merchant,55.00,USD,4242,visa-corp`;
		try {
			const first = await importCsv(page, csv);
			expect(first.status()).toBe(200);
			const r1 = (await first.json()) as { imported: number; skipped: number };
			expect(r1.imported).toBe(1);

			// Re-import the same external_txn_id → deduped (partial-unique index).
			const second = await importCsv(page, csv);
			expect(second.status()).toBe(200);
			const r2 = (await second.json()) as { imported: number; skipped: number };
			expect(r2.imported).toBe(0);
			expect(r2.skipped).toBe(1);

			await page.goto('/expenses?tab=cards');
			await expect(page.getByText('E2E Card Merchant')).toBeVisible();
		} finally {
			deleteCardTxnByExternalId(ext);
		}
	});

	// A blank or malformed currency is refused per row, never read as USD
	// (decisions §377), and the Cards tab lists each refused row with its
	// reason in the reader's language, not just a "skipped" count.
	test('lists each refused row with its localized reason (de)', async ({ page }) => {
		const stamp = Date.now();
		const ok = `e2e-ccy-ok-${stamp}`;
		const blank = `e2e-ccy-blank-${stamp}`;
		const bad = `e2e-ccy-bad-${stamp}`;
		const csv = [
			CSV_HEADER,
			`${ok},2026-02-07,,E2E Ccy Pret,12.40,gbp,4242,corp`,
			`${blank},2026-02-07,,E2E Ccy Uber,8.00,,4242,corp`,
			`${bad},2026-02-07,,E2E Ccy Lyft,9.00,EURO,4242,corp`
		].join('\n');
		try {
			await page.addInitScript(() => {
				localStorage.setItem('feoh_locale', 'de');
			});
			await page.goto('/expenses?tab=cards');
			const imported = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === '/api/corporate-card-transactions/import-csv' &&
					r.request().method() === 'POST'
			);
			await page.locator('input[type="file"][accept=".csv"]').setInputFiles({
				name: 'cards.csv',
				mimeType: 'text/csv',
				buffer: Buffer.from(csv)
			});
			const body = (await (await imported).json()) as {
				imported: number;
				skipped: number;
				errors: { row: number; code?: string; params?: Record<string, unknown> }[];
			};
			// The wire contract: each refused row's code + params beside it.
			expect(body.imported).toBe(1);
			expect(body.errors.map((e) => [e.row, e.code])).toEqual([
				[3, 'csv_currency_required'],
				[4, 'csv_currency_invalid']
			]);
			expect(body.errors[1].params?.value).toBe('EURO');

			const panel = page.getByRole('region', { name: 'Nicht importierte Zeilen' });
			const rows = panel.locator('li');
			await expect(rows).toHaveCount(2);
			await expect(rows.nth(0)).toContainText('3');
			await expect(rows.nth(0)).toContainText('Die Spalte currency ist leer');
			await expect(rows.nth(1)).toContainText('EURO ist kein dreistelliger Währungscode');
			await expect(panel).not.toContainText('currency is required');

			// The one good row landed, normalized; the refused ones left nothing.
			expect(
				tenantPsql(
					`SELECT currency FROM corporate_card_transactions WHERE external_txn_id='${ok}'`
				).trim()
			).toBe('GBP');
			expect(
				tenantPsql(
					`SELECT count(*) FROM corporate_card_transactions WHERE external_txn_id IN ('${blank}','${bad}')`
				).trim()
			).toBe('0');
		} finally {
			deleteCardTxnByExternalId(ok);
		}
	});

	test('syncs charged virtual cards into card transactions (idempotent)', async ({ page }) => {
		const resp = await page.request.post(
			`${API_BASE}/api/corporate-card-transactions/sync-virtual-cards`,
			{ headers: await authedTenantHeaders(page), data: {} }
		);
		expect(resp.status()).toBe(200);
		const body = (await resp.json()) as { created: number; skipped: number };
		expect(typeof body.created).toBe('number');

		// A second sync creates nothing new (dedupe by external_txn_id "vc:…").
		const again = await page.request.post(
			`${API_BASE}/api/corporate-card-transactions/sync-virtual-cards`,
			{ headers: await authedTenantHeaders(page), data: {} }
		);
		const body2 = (await again.json()) as { created: number };
		expect(body2.created).toBe(0);
	});

	test('matches a card txn to an expense (both sides linked + payment_method)', async ({
		page
	}) => {
		const ext = `e2e-match-${Date.now()}`;
		const csv = `${CSV_HEADER}\n${ext},2026-02-03,2026-02-04,E2E Match Co,77.00,USD,1111,corp`;
		let expenseId: string | null = null;
		try {
			// An expense with the same amount + a near date, so it's a suggestion.
			const expResp = await page.request.post(`${API_BASE}/api/expenses`, {
				headers: await authedTenantHeaders(page),
				data: {
					merchant: 'E2E Match Co',
					amount: '77.00',
					currency: 'USD',
					expense_date: '2026-02-03',
					category: 'meals'
				}
			});
			expect(expResp.status()).toBe(201);
			expenseId = ((await expResp.json()) as { id: string }).id;

			await importCsv(page, csv);
			const txnId = tenantPsql(
				`SELECT id FROM corporate_card_transactions WHERE external_txn_id='${ext}'`
			).trim();

			const match = await page.request.post(
				`${API_BASE}/api/corporate-card-transactions/${txnId}/match`,
				{ headers: await authedTenantHeaders(page), data: { expense_id: expenseId } }
			);
			expect(match.status()).toBe(200);

			// Both sides linked + payment_method set to corporate_card.
			expect(
				tenantPsql(
					`SELECT matched_expense_id FROM corporate_card_transactions WHERE id='${txnId}'`
				).trim()
			).toBe(expenseId);
			expect(
				tenantPsql(`SELECT card_transaction_id FROM expenses WHERE id='${expenseId}'`).trim()
			).toBe(txnId);
			expect(
				tenantPsql(`SELECT payment_method FROM expenses WHERE id='${expenseId}'`).trim()
			).toBe('corporate_card');
		} finally {
			deleteCardTxnByExternalId(ext);
			if (expenseId) tenantPsql(`DELETE FROM expenses WHERE id='${expenseId}'`);
		}
	});

	test('ignores a card transaction', async ({ page }) => {
		const ext = `e2e-ignore-${Date.now()}`;
		const csv = `${CSV_HEADER}\n${ext},2026-02-05,2026-02-06,E2E Ignore Co,9.00,USD,2222,corp`;
		try {
			await importCsv(page, csv);
			const txnId = tenantPsql(
				`SELECT id FROM corporate_card_transactions WHERE external_txn_id='${ext}'`
			).trim();
			const resp = await page.request.post(
				`${API_BASE}/api/corporate-card-transactions/${txnId}/ignore`,
				{ headers: await authedTenantHeaders(page), data: {} }
			);
			expect(resp.status()).toBe(200);
			expect(
				tenantPsql(
					`SELECT reconciliation_status FROM corporate_card_transactions WHERE id='${txnId}'`
				).trim()
			).toBe('ignored');
		} finally {
			deleteCardTxnByExternalId(ext);
		}
	});
});
