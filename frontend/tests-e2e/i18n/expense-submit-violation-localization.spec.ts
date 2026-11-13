import { API_BASE, authedTenantHeaders, expect, signInAndWait, tenantPsql, test } from '../fixtures/helpers';

/**
 * A blocked expense-report submit is stated in the reader's language — the
 * toast AND each violation in the panel (decisions §378).
 *
 * `POST /api/expense-reports/{id}/submit`'s blocking-violation 422 used to be
 * `{message, violations}` with server English in both, so a German reader saw
 * an English toast over an English list. The envelope is now the coded
 * `expense_report_policy_blocked` and every violation carries the `{code,
 * params}` the policy engine built, which `api/policyViolations.ts` states.
 *
 * This is the round trip only the real stack proves: the unit tests on either
 * side work off fixtures. The datum is owned — a EUR receipt rule and a EUR
 * line, so the German money format (`5,00 €`) shares nothing with the
 * server's `5.00 EUR`, and a fallback to the English `message` turns this red.
 */

interface Created {
	id: string;
}

test.describe('a blocked expense submit is localized', () => {
	test('a de reader sees the toast and the receipt rule in German', async ({
		page,
		tenantClerk
	}) => {
		const stamp = Date.now();
		const reportNumber = `E2E-VIOL-DE-${stamp}`;
		// A category no seeded policy names (the seed's are capitalised, and a
		// policy matches its category exactly), so exactly one rule fires.
		const category = `e2e-viol-de-${stamp}`;
		let policyId: string | null = null;
		let expenseId: string | null = null;
		let reportId: string | null = null;
		try {
			// The worker's default session is the admin, who may write policies.
			const polResp = await page.request.post(`${API_BASE}/api/expense-policies`, {
				headers: await authedTenantHeaders(page),
				data: {
					name: `E2E Receipt Rule DE ${stamp}`,
					active: true,
					category,
					requires_receipt_above: '5.00',
					threshold_currency: 'EUR'
				}
			});
			expect(polResp.status()).toBe(201);
			policyId = ((await polResp.json()) as Created).id;

			// The clerk owns the report, so the clerk is the one who submits.
			await signInAndWait(page, tenantClerk);
			const clerkH = await authedTenantHeaders(page);
			const expResp = await page.request.post(`${API_BASE}/api/expenses`, {
				headers: clerkH,
				data: {
					merchant: `E2E Viol DE ${stamp}`,
					amount: '50.00',
					currency: 'EUR',
					expense_date: '2026-02-02',
					category
				}
			});
			expect(expResp.status()).toBe(201);
			expenseId = ((await expResp.json()) as Created).id;
			const rptResp = await page.request.post(`${API_BASE}/api/expense-reports`, {
				headers: clerkH,
				data: { report_number: reportNumber, title: 'DE trip', currency: 'EUR' }
			});
			expect(rptResp.status()).toBe(201);
			reportId = ((await rptResp.json()) as Created).id;
			const attach = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/expenses`,
				{ headers: clerkH, data: { expense_ids: [expenseId], detach: false } }
			);
			expect(attach.status()).toBe(200);

			// The API half: coded envelope, list kept, each entry coded.
			const raw = await page.request.post(`${API_BASE}/api/expense-reports/${reportId}/submit`, {
				headers: clerkH,
				data: {}
			});
			expect(raw.status()).toBe(422);
			const detail = (await raw.json()).detail as {
				code: string;
				message: string;
				params: { count: number };
				violations: { code: string; message: string; params: Record<string, unknown> }[];
			};
			expect(detail.code).toBe('expense_report_policy_blocked');
			expect(detail.params).toEqual({ count: 1 });
			expect(detail.message).toBe('Report has blocking policy violations and cannot be submitted.');
			expect(detail.violations).toHaveLength(1);
			expect(detail.violations[0].code).toBe('receipt_required');
			expect(detail.violations[0].message).toBe('A receipt is required for amounts above 5.00 EUR.');
			expect(detail.violations[0].params).toEqual({
				amount: '50.00',
				amount_currency: 'EUR',
				limit: '5.00',
				currency: 'EUR',
				comparison: 'resolved'
			});

			// The page half, in German.
			await page.evaluate(() => localStorage.setItem('feoh_locale', 'de'));
			await page.goto('/expenses?tab=reports');
			await page.getByRole('button', { name: `Bericht ${reportNumber} öffnen` }).click();
			await page.getByRole('button', { name: 'Einreichen', exact: true }).click();

			const panel = page.locator('.violation-panel');
			await expect(panel).toContainText('Einreichen blockiert');
			await expect(panel.locator('li')).toHaveText([
				/^Für Beträge über 5,00\s€ ist ein Beleg erforderlich\.$/
			]);
			await expect(panel).not.toContainText('receipt');
			await expect(
				page.getByText('1 blockierender Richtlinienverstoß verhindert das Einreichen dieser Abrechnung.')
			).toBeVisible();
		} finally {
			if (reportId) {
				tenantPsql(`UPDATE expenses SET report_id=NULL WHERE report_id='${reportId}'`);
				tenantPsql(`DELETE FROM expense_reports WHERE id='${reportId}'`);
			}
			if (expenseId) tenantPsql(`DELETE FROM expenses WHERE id='${expenseId}'`);
			if (policyId) tenantPsql(`DELETE FROM expense_policies WHERE id='${policyId}'`);
		}
	});
});
