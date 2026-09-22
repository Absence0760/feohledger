import { randomUUID } from 'node:crypto';

import type { Page } from '@playwright/test';

import { API_BASE, authedTenantHeaders, expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /bank-reconciliation — did every payment we think we made actually clear the
 * bank, and is every debit the bank shows one of ours?
 *
 * Covers the path the page exists for: land on the outstanding worksheet →
 * open an imported statement → act on a line (confirm / clear / match), plus
 * the RBAC posture (read = all four roles, mutate = admin | ap_manager, the
 * same treasury split as Positive Pay).
 *
 * Login model mirrors the rest of the suite: the default per-worker storage
 * state signs the worker's admin in, so the page loads without a redirect. The
 * clerk block opts out and signs in as the clerk.
 *
 * Statements are imported through the real API (a CSV import moves no money),
 * and torn down afterwards. The two places a REAL payment would be needed —
 * the match picker's candidate list, and the resolve round-trip — are stubbed
 * instead, for the same reason the Positive Pay spec stubs its run list:
 * creating and dispatching a payment run is money-moving, and the assertion
 * here is about what the UI sends and renders, not about the matcher (which
 * `backend/tests/test_bank_reconciliation*.py` already pins end-to-end).
 */

/** An account identifier unique to this run, so the import-idempotency slot
 *  `(org, account_identifier, sha256(body))` never collides across runs.
 *  `randomUUID` rather than `Math.random`: the shard matrix starts workers in
 *  the same millisecond, which leaves `Date.now()` contributing nothing and a
 *  ~20-bit tail carrying the whole collision budget. */
function uniqueAccount(): string {
	return `E2E-ACCT-${randomUUID()}`;
}

/**
 * One debit that cannot match anything: a long-past date well outside the
 * matcher's ±5-day window and an amount no seeded payment carries, so the line
 * reliably lands `unmatched` and the manual-resolve affordance is the one
 * under test.
 */
function unmatchableCsv(reference: string): string {
	return [
		'Date,Amount,Description,Reference,Counterparty',
		`2019-03-07,-13579.24,E2E bank recon debit,${reference},E2E Recon Counterparty`
	].join('\n');
}

interface ImportedStatement {
	id: string;
	account_identifier: string;
	transaction_count: number;
	transactions: { id: string; direction: string; matched_payment_id: string | null }[] | null;
}

async function importStatement(page: Page, account: string, csv: string) {
	const headers = await authedTenantHeaders(page);
	return page.request.post(`${API_BASE}/api/bank-reconciliation/upload`, {
		headers, // Authorization + X-Tenant-Slug; Playwright sets the multipart Content-Type
		multipart: {
			file: { name: 'statement.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) },
			account_identifier: account,
			period_start: '2019-03-01',
			period_end: '2019-03-31',
			currency: 'USD'
		}
	});
}

async function deleteStatement(page: Page, id: string) {
	await page.request.delete(`${API_BASE}/api/bank-reconciliation/${id}`, {
		headers: await authedTenantHeaders(page)
	});
}

test.describe('/bank-reconciliation (admin)', () => {
	test('renders the outstanding worksheet — heading, KPIs, tabs, filters', async ({ page }) => {
		await page.goto('/bank-reconciliation');

		await expect(page.getByRole('heading', { name: 'Bank Reconciliation', level: 1 })).toBeVisible();

		// Whole-set KPI row (counts come from /outstanding, never a row reduce).
		await expect(page.locator('.kpi')).toHaveCount(3);

		// Both tabs are present and Outstanding is the default panel.
		await expect(page.getByRole('tab', { name: 'Outstanding' })).toHaveAttribute(
			'aria-selected',
			'true'
		);
		await expect(page.getByRole('tab', { name: /Statements/ })).toBeVisible();

		// The three buckets a bank-rec worksheet closes a period on.
		await expect(page.getByRole('heading', { name: 'Uncleared payments' })).toBeVisible();
		await expect(page.getByRole('heading', { name: 'Unmatched bank debits' })).toBeVisible();
		await expect(page.getByRole('heading', { name: 'Discrepancies' })).toBeVisible();

		// Age chips + the filter box.
		await expect(page.locator('.filter-chip', { hasText: 'Any age' })).toBeVisible();
		await expect(page.getByLabel('Filter outstanding reconciliation items')).toBeVisible();
	});

	test('a bucket total with no established currency renders bare, beside a real one — never inside it', async ({
		page
	}) => {
		// `_currency_totals` reports a payment whose currency could not be
		// established under `""` precisely so it is "not folded into another
		// currency's figure". The page used to fold it straight back in on
		// screen, labelling it with the org's code (decisions §200). The org is
		// pinned to USD here so a total borrowing that code is unmistakable.
		await page.route(
			(url) => url.pathname === '/api/organization',
			(route) => route.fulfill({ json: { settings: { reporting_currency: 'USD' } } })
		);
		await page.route(
			(url) => url.pathname === '/api/bank-reconciliation/outstanding',
			(route) =>
				route.fulfill({
					json: {
						as_of: '2026-03-01',
						older_than_days: 0,
						uncleared_payments: [],
						uncleared_count: 2,
						uncleared_totals: [
							{ currency: '', total: '12.50' },
							{ currency: 'EUR', total: '100.00' }
						],
						unmatched_debits: [],
						unmatched_debit_count: 0,
						unmatched_debit_totals: [],
						discrepancies: [],
						discrepancy_count: 0,
						amount_mismatch_net_variances: []
					}
				})
		);

		await page.goto('/bank-reconciliation');

		const buckets = page.locator('section.bucket');
		const uncleared = buckets.nth(0).locator('.bucket-total');
		// Two figures side by side: the unknown one bare, the EUR one in euros.
		await expect(uncleared).toHaveText('12.50 · €100.00');
		await expect(uncleared).not.toContainText('$');

		// An EMPTY bucket is the one place the org's code is the right label —
		// nothing outstanding costs zero in the currency the org reports in.
		await expect(buckets.nth(1).locator('.bucket-total')).toHaveText('$0.00');
	});

	test('the age filter is a SERVER filter and is URL-backed', async ({ page }) => {
		await page.goto('/bank-reconciliation');

		const respPromise = page.waitForResponse(
			(r) =>
				r.url().includes('/api/bank-reconciliation/outstanding') &&
				r.url().includes('older_than_days=30')
		);
		await page.locator('.filter-chip', { hasText: 'Over 30 days' }).click();
		await respPromise;

		await expect(page).toHaveURL(/older_than_days=30/);

		// And a reload restores the same filter from the URL.
		await page.reload();
		await expect(page.locator('.filter-chip.active', { hasText: 'Over 30 days' })).toBeVisible();
	});

	test('an imported statement lists, opens, and shows its unmatched line', async ({ page }) => {
		const account = uniqueAccount();
		let id: string | null = null;
		try {
			await page.goto('/bank-reconciliation');

			const resp = await importStatement(page, account, unmatchableCsv(`E2E-${account}`));
			expect(resp.status()).toBe(201);
			const statement = (await resp.json()) as ImportedStatement;
			id = statement.id;
			expect(statement.transaction_count).toBe(1);

			// A second import of the SAME bytes for the same account is idempotent:
			// it returns the first statement with 200, never a second row that would
			// match nothing and read as "this file didn't reconcile".
			const again = await importStatement(page, account, unmatchableCsv(`E2E-${account}`));
			expect(again.status()).toBe(200);
			expect(((await again.json()) as ImportedStatement).id).toBe(id);

			await page.goto('/bank-reconciliation?tab=statements');

			const openRow = page.getByRole('button', { name: new RegExp(`Open bank statement ${account}`) });
			await expect(openRow).toBeVisible({ timeout: 10_000 });
			await openRow.click();

			const dialog = page.getByRole('dialog', { name: `Bank statement ${account}` });
			await expect(dialog).toBeVisible();
			await expect(dialog.getByText('0 of 1 lines reconciled')).toBeVisible();

			// The line is a debit nothing claims — it must read as unmatched, and
			// the manual-match affordance must be the obvious next step.
			await expect(dialog.locator('.badge.unmatched')).toBeVisible();
			await expect(
				dialog.getByRole('button', { name: /Match a payment to E2E Recon Counterparty/ })
			).toBeVisible();
		} finally {
			if (id) await deleteStatement(page, id);
		}
	});

	test('matching an unmatched line sends the picked payment id and re-renders', async ({
		page
	}) => {
		const account = uniqueAccount();
		const PAYMENT_ID = '33333333-3333-4333-8333-333333333333';
		let id: string | null = null;
		try {
			await page.goto('/bank-reconciliation');

			const resp = await importStatement(page, account, unmatchableCsv(`E2E-${account}`));
			expect(resp.status()).toBe(201);
			const statement = (await resp.json()) as ImportedStatement;
			id = statement.id;
			const txId = statement.transactions?.[0]?.id;
			expect(txId).toBeTruthy();

			// Candidate list = /outstanding's uncleared_payments bucket. Stubbed to
			// a fixed row so the picker is exact without dispatching real money —
			// in EUR, deliberately not the seeded org default, because the picker
			// used to label every candidate with the org's currency on the belief
			// that the row carried none.
			await page.route(
				(url) => url.pathname === '/api/bank-reconciliation/outstanding',
				(route) =>
					route.fulfill({
						json: {
							as_of: '2026-03-01',
							older_than_days: 0,
							uncleared_payments: [
								{
									payment_id: PAYMENT_ID,
									invoice_id: '44444444-4444-4444-8444-444444444444',
									invoice_number: 'E2E-INV-9001',
									vendor_name: 'E2E Stub Vendor',
									amount: '13579.24',
									currency: 'EUR',
									method: 'ach',
									status: 'submitted',
									sent_on: '2019-03-05',
									days_outstanding: 2000
								}
							],
							uncleared_count: 1,
							uncleared_totals: [{ currency: 'EUR', total: '13579.24' }],
							unmatched_debits: [],
							unmatched_debit_count: 0,
							unmatched_debit_totals: [],
							discrepancies: [],
							discrepancy_count: 0,
							amount_mismatch_net_variances: []
						}
					})
			);

			// Intercept the resolve so the assertion is about what the UI SENDS —
			// pointing a real bank line at a fabricated payment id would 404, and
			// creating a real payment would move money.
			let resolveBody: Record<string, unknown> | null = null;
			await page.route(
				(url) => url.pathname.endsWith(`/transactions/${txId}/resolve`),
				async (route) => {
					resolveBody = route.request().postDataJSON();
					await route.fulfill({
						json: {
							...statement,
							matched_count: 1,
							amount_mismatch_count: 0,
							discrepancy_count: 0,
							transactions: [
								{
									...(statement.transactions ?? [])[0],
									matched_payment_id: PAYMENT_ID,
									matched_invoice_number: 'E2E-INV-9001',
									match_method: 'manual',
									match_confidence: 100,
									matched_at: '2026-03-01T00:00:00Z',
									matched_payment_amount: '13579.24',
									matched_payment_currency: 'USD',
									matched_payment_status: 'submitted',
									variance_amount: '0.00',
									is_reconciled: true
								}
							]
						}
					});
				}
			);

			await page.goto(`/bank-reconciliation?id=${id}`);
			const dialog = page.getByRole('dialog', { name: `Bank statement ${account}` });
			await expect(dialog).toBeVisible({ timeout: 10_000 });

			await dialog
				.getByRole('button', { name: /Match a payment to E2E Recon Counterparty/ })
				.click();
			const pick = dialog.getByRole('button', {
				name: 'Match this line to payment for E2E Stub Vendor'
			});
			await expect(pick).toBeVisible();
			// The candidate wears ITS OWN currency, not the org's.
			const candidate = dialog.locator('.picker-list li', { hasText: 'E2E Stub Vendor' });
			await expect(candidate).toContainText(/€|EUR/);
			await expect(candidate).not.toContainText('$');
			await pick.click();

			// The row now reads as a human-confirmed match, and the id the UI sent
			// is the one that was picked.
			await expect(dialog.locator('.badge.confirmed')).toBeVisible();
			await expect(dialog.getByText('1 of 1 lines reconciled')).toBeVisible();
			expect(resolveBody).toEqual({ matched_payment_id: PAYMENT_ID });
		} finally {
			if (id) {
				await page.unrouteAll({ behavior: 'ignoreErrors' });
				await deleteStatement(page, id);
			}
		}
	});

	test('a fuzzy 60%-confidence match is presented as a suggestion, never a fact', async ({
		page
	}) => {
		// The judgment this whole surface exists to protect: a vendor-name
		// similarity hit is a suggestion a human still owes a decision on. It must
		// not carry the confirmed treatment, and it must offer the confirm/clear
		// affordance.
		const account = uniqueAccount();
		let id: string | null = null;
		try {
			await page.goto('/bank-reconciliation');

			const resp = await importStatement(page, account, unmatchableCsv(`E2E-${account}`));
			expect(resp.status()).toBe(201);
			const statement = (await resp.json()) as ImportedStatement;
			id = statement.id;

			await page.route(
				(url) => url.pathname === `/api/bank-reconciliation/${id}`,
				async (route) => {
					const original = await route.fetch();
					const body = (await original.json()) as Record<string, unknown> & {
						transactions: Record<string, unknown>[];
					};
					body.transactions = body.transactions.map((t) => ({
						...t,
						matched_payment_id: '55555555-5555-4555-8555-555555555555',
						matched_invoice_number: 'E2E-INV-9002',
						match_method: 'fuzzy_vendor',
						match_confidence: 60,
						matched_payment_amount: '13579.24',
						matched_payment_currency: 'USD',
						matched_payment_status: 'submitted',
						is_reconciled: true
					}));
					await route.fulfill({ response: original, json: body });
				}
			);

			await page.goto(`/bank-reconciliation?id=${id}`);
			const dialog = page.getByRole('dialog', { name: `Bank statement ${account}` });
			await expect(dialog).toBeVisible({ timeout: 10_000 });

			await expect(dialog.locator('.badge.suggested')).toBeVisible();
			await expect(dialog.locator('.badge.confirmed')).toHaveCount(0);
			await expect(dialog.getByText('Vendor name')).toBeVisible();
			await expect(dialog.getByText('60% confidence')).toBeVisible();
			await expect(dialog.getByText(/a suggestion, not a fact/)).toBeVisible();
			await expect(
				dialog.getByRole('button', { name: /Confirm the suggested match on/ })
			).toBeVisible();
			await expect(dialog.getByRole('button', { name: /Clear the match on/ })).toBeVisible();
		} finally {
			if (id) {
				await page.unrouteAll({ behavior: 'ignoreErrors' });
				await deleteStatement(page, id);
			}
		}
	});
});

test.describe('/bank-reconciliation (clerk — reads, cannot mutate)', () => {
	// Opt out of the default admin storage state so we can sign in as the clerk.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk sees the surface but no mutate control, and the write API 403s', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);

		// Read is all four roles, so the clerk DOES get the nav row — hiding it
		// would be a dead end, not a gate. The row is a CHILD of the Billing
		// group, so it surfaces as a section tab on that group's pages, not as a
		// top-level sidebar link (see tests-e2e/smoke/section-nav.spec.ts).
		await page.goto('/bank-reconciliation');
		await expect(
			page.locator('.section-tabs a.section-tab[href="/bank-reconciliation"]')
		).toBeVisible();

		await expect(page.getByRole('heading', { name: 'Bank Reconciliation', level: 1 })).toBeVisible();
		await expect(page.locator('.kpi')).toHaveCount(3);

		// …and no import control anywhere on the page.
		await expect(page.getByRole('button', { name: '+ Import statement' })).toHaveCount(0);

		// Nor on the empty state's call to action. Anchor on the statements
		// panel first — the absence check below proves nothing until the tab
		// this claim is about has actually rendered.
		await page.goto('/bank-reconciliation?tab=statements');
		await expect(page.locator('#bank-recon-panel-statements')).toBeVisible();
		await expect(page.getByRole('button', { name: '+ Import statement' })).toHaveCount(0);

		// And the backend refuses the mutate regardless — `require_roles` runs
		// before the handler body, so the POST 403s whatever the payload.
		const resp = await importStatement(page, uniqueAccount(), unmatchableCsv('E2E-CLERK'));
		expect(resp.status()).toBe(403);
	});
});
