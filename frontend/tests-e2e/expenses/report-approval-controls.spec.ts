import {
	API_BASE,
	currentTenantSlug,
	expect,
	tenantHeaders,
	tenantPsql,
	test
} from '../fixtures/helpers';

// WF3 — expense-report approval CONTROL PATH (the SOX-critical leg).
//
// `expense-approval.spec.ts` already covers the happy path (submit blocked by a
// policy violation, then a different manager approves) and the API self-approve
// 403. This spec goes deeper on the controls that are enforced SERVER-side and
// were previously untested:
//
//   - the CFO-threshold gate boundary (== threshold approvable by a plain
//     manager; > threshold demands cfo/admin — a 403 for an ap_manager),
//     driven through the real UI where the gate is invisible client-side;
//   - reject returns the report to `rejected` and ALL its children back to
//     `draft` (no stranded `submitted` expenses);
//   - every transition writes an append-only audit row (DB-immutable);
//   - pre-approval segregation — the requester cannot decide their own request;
//   - invalid source-state transitions are 422 (never a silent no-op).
//
// Per-role API setup uses tokens minted straight from `POST /api/auth/login`
// (deterministic, no UI), cached so each role logs in at most once. The two UI
// assertions reuse the same cached token by injecting it into localStorage
// (`actAsInUi`) rather than driving the flaky, rate-limited login form — so the
// whole spec performs only a handful of logins and stays well under the
// `auth_login` 10/60s cap even on back-to-back runs.
//
// Reports are built under the CLERK so the owner (`employee_user_id`) is the
// clerk, not the approving manager/admin — otherwise the approver==submitter SoD
// rule fires. The platform default CFO threshold is 5000 (no `expense_approval`
// key on the e2e org's settings), so the boundary is exercised at exactly that
// value without mutating org config.

interface Created {
	id: string;
}

type ReportRow = { id: string; status: string; total_amount: number; report_number: string };

// Cache one JWT per (slug, email) for the whole spec run. The login endpoint is
// rate-limited to 10/60s per IP (`auth_login`). Seven tests each re-logging-in
// (and the two UI tests' form sign-ins) would blow that budget and make the
// spec fragile on back-to-back runs — so each role logs in at most ONCE here,
// via the API, and both the API setup and the UI sign-in reuse that one token.
const _tokenCache = new Map<string, string>();

/** Mint (and cache) a JWT for `creds` via the real login endpoint. No browser
 *  session, fully deterministic; cached so a role logs in at most once. */
async function roleToken(
	page: import('@playwright/test').Page,
	creds: { email: string; password: string }
): Promise<string> {
	const slug = currentTenantSlug();
	const key = `${slug}:${creds.email}`;
	let token = _tokenCache.get(key);
	if (!token) {
		const res = await page.request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': slug },
			data: { email: creds.email, password: creds.password }
		});
		expect(res.status(), 'login should not be rate-limited (cached per role)').toBe(200);
		const body = (await res.json()) as { access_token?: string; token?: string };
		token = body.access_token ?? body.token;
		expect(token, 'login returned a token').toBeTruthy();
		_tokenCache.set(key, token as string);
	}
	return token as string;
}

/** Auth + tenant headers for a role's cached token — for `page.request` API calls. */
async function roleHeaders(
	page: import('@playwright/test').Page,
	creds: { email: string; password: string }
): Promise<Record<string, string>> {
	return tenantHeaders(await roleToken(page, creds), currentTenantSlug());
}

/** Drive the browser as `creds` WITHOUT touching the flaky / rate-limited login
 *  form: inject the role's cached JWT straight into localStorage (the same key
 *  the app + the storage-state fixture use) and reload so the auth store boots
 *  authenticated. Used by the UI assertions; costs zero extra login calls. */
async function actAsInUi(
	page: import('@playwright/test').Page,
	creds: { email: string; password: string }
): Promise<void> {
	const token = await roleToken(page, creds);
	await page.evaluate((t) => localStorage.setItem('auth_token', t), token);
	await page.goto('/expenses?tab=reports');
}

function deleteReport(id: string): void {
	tenantPsql(`UPDATE expenses SET report_id=NULL WHERE report_id='${id}'`);
	tenantPsql(`DELETE FROM expense_reports WHERE id='${id}'`);
}

function deleteExpense(id: string): void {
	tenantPsql(`DELETE FROM expenses WHERE id='${id}'`);
}

function deletePreapproval(id: string): void {
	tenantPsql(`DELETE FROM expense_preapprovals WHERE id='${id}'`);
}

/** Count append-only audit rows for one entity + action. */
function auditCount(action: string, entityId: string): number {
	const out = tenantPsql(
		`SELECT count(*) FROM audit_log WHERE action='${action}' AND entity_id='${entityId}'`
	);
	return parseInt(out.trim(), 10);
}

/** Build a clerk-owned, submitted report with a single expense of `amount`.
 *  No policies → no blocking violation, so the draft→submitted transition
 *  succeeds. Returns the ids for cleanup + assertions. */
async function buildSubmittedReport(
	page: import('@playwright/test').Page,
	clerkH: Record<string, string>,
	amount: string,
	tag: string
): Promise<{ reportId: string; expenseId: string; reportNumber: string }> {
	const reportNumber = `E2E-CTRL-${tag}-${Date.now()}`;
	const expResp = await page.request.post(`${API_BASE}/api/expenses`, {
		headers: clerkH,
		data: {
			merchant: `E2E Ctrl ${tag} ${Date.now()}`,
			amount,
			currency: 'USD',
			expense_date: '2026-03-01',
			category: 'travel'
		}
	});
	expect(expResp.status()).toBe(201);
	const expenseId = ((await expResp.json()) as Created).id;

	const rptResp = await page.request.post(`${API_BASE}/api/expense-reports`, {
		headers: clerkH,
		data: { report_number: reportNumber, title: `WF3 ctrl ${tag}`, currency: 'USD' }
	});
	expect(rptResp.status()).toBe(201);
	const reportId = ((await rptResp.json()) as Created).id;

	const attach = await page.request.post(
		`${API_BASE}/api/expense-reports/${reportId}/expenses`,
		{ headers: clerkH, data: { expense_ids: [expenseId], detach: false } }
	);
	expect(attach.status()).toBe(200);

	const submit = await page.request.post(`${API_BASE}/api/expense-reports/${reportId}/submit`, {
		headers: clerkH,
		data: {}
	});
	expect(submit.status()).toBe(200);
	const submitted = (await submit.json()) as ReportRow;
	expect(submitted.status).toBe('submitted');
	expect(submitted.total_amount).toBeCloseTo(parseFloat(amount), 2);
	return { reportId, expenseId, reportNumber };
}

test.describe('/expenses — WF3 report approval controls', () => {
	test.beforeEach(async ({ page }) => {
		// Land on the authed app shell so localStorage (token) is readable and
		// `page.request` shares the browser context. Storage state = the worker
		// admin; per-role API setup uses freshly-minted tokens via roleHeaders.
		await page.goto('/expenses');
	});

	// --- CFO threshold gate --------------------------------------------------

	test('a plain manager can approve a report AT the CFO threshold (5000)', async ({
		page,
		tenantManager,
		tenantClerk
	}) => {
		let reportId: string | null = null;
		let expenseId: string | null = null;
		try {
			const clerkH = await roleHeaders(page, tenantClerk);
			// Exactly 5000 is NOT over the threshold (gate is strictly `>`), so the
			// plain ap_manager may approve.
			const built = await buildSubmittedReport(page, clerkH, '5000.00', 'AT');
			reportId = built.reportId;
			expenseId = built.expenseId;

			const managerH = await roleHeaders(page, tenantManager);
			const approve = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/approve`,
				{ headers: managerH, data: {} }
			);
			expect(approve.status()).toBe(200);
			expect(((await approve.json()) as ReportRow).status).toBe('approved');
		} finally {
			if (reportId) deleteReport(reportId);
			if (expenseId) deleteExpense(expenseId);
		}
	});

	test('a plain manager is 403d above the CFO threshold; the CFO can approve', async ({
		page,
		tenantManager,
		tenantClerk,
		tenantCfo
	}) => {
		let reportId: string | null = null;
		let expenseId: string | null = null;
		try {
			const clerkH = await roleHeaders(page, tenantClerk);
			// 6000 > 5000 default threshold → only cfo/admin may approve.
			const built = await buildSubmittedReport(page, clerkH, '6000.00', 'OVER');
			reportId = built.reportId;
			expenseId = built.expenseId;

			// ap_manager (not cfo, not admin, not owner) → 403, NOT a 200.
			const managerH = await roleHeaders(page, tenantManager);
			const denied = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/approve`,
				{ headers: managerH, data: {} }
			);
			expect(denied.status()).toBe(403);
			// Report is untouched — still submitted, no approver stamped.
			expect(
				tenantPsql(`SELECT status FROM expense_reports WHERE id='${reportId}'`).trim()
			).toBe('submitted');

			// The CFO clears the gate.
			const cfoH = await roleHeaders(page, tenantCfo);
			const approve = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/approve`,
				{ headers: cfoH, data: {} }
			);
			expect(approve.status()).toBe(200);
			expect(((await approve.json()) as ReportRow).status).toBe('approved');
		} finally {
			if (reportId) deleteReport(reportId);
			if (expenseId) deleteExpense(expenseId);
		}
	});

	test('the UI surfaces the over-threshold 403 and leaves the report Submitted', async ({
		page,
		tenantManager,
		tenantClerk
	}) => {
		// The CFO-threshold gate is invisible client-side — `canDecideReport`
		// only checks manager + not-owner + submitted, so the Approve button is
		// shown to the ap_manager even for an over-threshold report. Clicking it
		// must surface the server 403 as an error toast and NOT flip the badge.
		let reportId: string | null = null;
		let expenseId: string | null = null;
		try {
			// Build the submitted, over-threshold report via the clerk's API token
			// (no UI), then drive the Approve button as the manager with ONE sign-in.
			const clerkH = await roleHeaders(page, tenantClerk);
			const built = await buildSubmittedReport(page, clerkH, '7500.00', 'UI403');
			reportId = built.reportId;
			expenseId = built.expenseId;

			await actAsInUi(page, tenantManager);
			await page.getByRole('button', { name: `Open report ${built.reportNumber}` }).click();
			await expect(page.locator('.report-title-block .badge')).toHaveText('Submitted');
			await page.getByRole('button', { name: 'Approve', exact: true }).click();

			// The error toast renders the server's "CFO approval required" detail.
			await expect(page.locator('.toast.error')).toBeVisible();
			await expect(page.getByText(/CFO approval required/i)).toBeVisible();
			// Badge must NOT have flipped to Approved.
			await expect(page.locator('.report-title-block .badge')).toHaveText('Submitted');
			// And the DB agrees — no approval slipped through.
			expect(
				tenantPsql(`SELECT status FROM expense_reports WHERE id='${reportId}'`).trim()
			).toBe('submitted');
		} finally {
			if (reportId) deleteReport(reportId);
			if (expenseId) deleteExpense(expenseId);
		}
	});

	// --- Only the owner composes or submits ---------------------------------

	test('a manager is not offered Submit / Attach / Detach on a clerk-owned draft', async ({
		page,
		tenantManager,
		tenantClerk
	}) => {
		// Report segregation refuses only the OWNER at approval, so composing or
		// submitting is the owner's alone (403 `expense_report_not_owner`). The
		// page must not offer a manager controls the server will refuse — and the
		// server must refuse them when called anyway.
		const reportNumber = `E2E-OWN-${Date.now()}`;
		let reportId: string | null = null;
		let expenseId: string | null = null;
		try {
			const clerkH = await roleHeaders(page, tenantClerk);
			const expResp = await page.request.post(`${API_BASE}/api/expenses`, {
				headers: clerkH,
				data: { merchant: `E2E Own ${Date.now()}`, amount: '40.00', currency: 'USD', expense_date: '2026-03-01' }
			});
			expect(expResp.status()).toBe(201);
			expenseId = ((await expResp.json()) as Created).id;
			const rptResp = await page.request.post(`${API_BASE}/api/expense-reports`, {
				headers: clerkH,
				data: { report_number: reportNumber, title: 'Owner only', currency: 'USD' }
			});
			expect(rptResp.status()).toBe(201);
			reportId = ((await rptResp.json()) as Created).id;
			const attach = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/expenses`,
				{ headers: clerkH, data: { expense_ids: [expenseId], detach: false } }
			);
			expect(attach.status()).toBe(200);

			const managerH = await roleHeaders(page, tenantManager);
			const submit = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/submit`,
				{ headers: managerH, data: {} }
			);
			expect(submit.status()).toBe(403);
			expect(((await submit.json()) as { detail: { code: string } }).detail.code).toBe(
				'expense_report_not_owner'
			);

			await actAsInUi(page, tenantManager);
			await page.getByRole('button', { name: `Open report ${reportNumber}` }).click();
			await expect(page.locator('.report-title-block .badge')).toHaveText('Draft');
			// The row is rendered (positive anchor) before the absence assertions.
			await expect(page.locator('.report-detail tbody tr')).toHaveCount(1);
			await expect(page.getByRole('button', { name: 'Submit', exact: true })).toHaveCount(0);
			await expect(page.locator('.attach-row')).toHaveCount(0);
			await expect(page.locator('.report-detail tbody .actions button')).toHaveCount(0);
		} finally {
			if (reportId) deleteReport(reportId);
			if (expenseId) deleteExpense(expenseId);
		}
	});

	// --- Reject returns children to draft -----------------------------------

	test('rejecting a submitted report returns its child expenses to draft', async ({
		page,
		tenantManager,
		tenantClerk
	}) => {
		let reportId: string | null = null;
		let expenseId: string | null = null;
		try {
			const clerkH = await roleHeaders(page, tenantClerk);
			const built = await buildSubmittedReport(page, clerkH, '120.00', 'REJ');
			reportId = built.reportId;
			expenseId = built.expenseId;

			// The child expense is `submitted` after the report submit.
			expect(tenantPsql(`SELECT status FROM expenses WHERE id='${expenseId}'`).trim()).toBe(
				'submitted'
			);

			// Reject through the real UI as the manager (not owner). ONE sign-in.
			await actAsInUi(page, tenantManager);
			await page.getByRole('button', { name: `Open report ${built.reportNumber}` }).click();
			await page.getByRole('button', { name: 'Reject', exact: true }).click();
			await page.getByLabel('Rejection reason').fill('Missing itemisation');
			await page.getByRole('button', { name: 'Confirm reject' }).click();

			await expect(page.locator('.report-title-block .badge')).toHaveText('Rejected');

			// The report is rejected AND the child is back to draft (not stranded
			// in `submitted`), so it can be corrected + re-reported.
			expect(
				tenantPsql(`SELECT status FROM expense_reports WHERE id='${reportId}'`).trim()
			).toBe('rejected');
			expect(tenantPsql(`SELECT status FROM expenses WHERE id='${expenseId}'`).trim()).toBe(
				'draft'
			);
		} finally {
			if (reportId) deleteReport(reportId);
			if (expenseId) deleteExpense(expenseId);
		}
	});

	// --- Audit trail (append-only) ------------------------------------------

	test('submit / approve / reject each write an append-only audit row', async ({
		page,
		tenantManager,
		tenantClerk
	}) => {
		// Two reports: one approved, one rejected — so the `.submitted`,
		// `.approved`, and `.rejected` audit actions each land exactly once for
		// their report, and the immutability trigger is exercised.
		let approvedReport: string | null = null;
		let approvedExpense: string | null = null;
		let rejectedReport: string | null = null;
		let rejectedExpense: string | null = null;
		try {
			const clerkH = await roleHeaders(page, tenantClerk);

			const a = await buildSubmittedReport(page, clerkH, '200.00', 'AUDA');
			approvedReport = a.reportId;
			approvedExpense = a.expenseId;
			expect(auditCount('expense_report.submitted', approvedReport)).toBe(1);

			const r = await buildSubmittedReport(page, clerkH, '300.00', 'AUDR');
			rejectedReport = r.reportId;
			rejectedExpense = r.expenseId;
			expect(auditCount('expense_report.submitted', rejectedReport)).toBe(1);

			const managerH = await roleHeaders(page, tenantManager);

			// Approve the first (manager, under threshold).
			const approve = await page.request.post(
				`${API_BASE}/api/expense-reports/${approvedReport}/approve`,
				{ headers: managerH, data: {} }
			);
			expect(approve.status()).toBe(200);
			expect(auditCount('expense_report.approved', approvedReport)).toBe(1);
			expect(auditCount('expense_report.rejected', approvedReport)).toBe(0);

			// Reject the second (admin / ap_manager).
			const reject = await page.request.post(
				`${API_BASE}/api/expense-reports/${rejectedReport}/reject`,
				{ headers: managerH, data: { reason: 'audit-test' } }
			);
			expect(reject.status()).toBe(200);
			expect(auditCount('expense_report.rejected', rejectedReport)).toBe(1);
			expect(auditCount('expense_report.approved', rejectedReport)).toBe(0);

			// Audit rows are immutable — a tamper attempt is rejected by the DB
			// trigger (composes with the SOX append-only invariant).
			let blocked = false;
			try {
				tenantPsql(
					`UPDATE audit_log SET action='tampered' WHERE action='expense_report.approved' AND entity_id='${approvedReport}'`
				);
			} catch {
				blocked = true;
			}
			expect(blocked).toBe(true);
			expect(auditCount('expense_report.approved', approvedReport)).toBe(1);
		} finally {
			if (approvedReport) deleteReport(approvedReport);
			if (approvedExpense) deleteExpense(approvedExpense);
			if (rejectedReport) deleteReport(rejectedReport);
			if (rejectedExpense) deleteExpense(rejectedExpense);
		}
	});

	// --- Invalid state transitions are 422 ----------------------------------

	test('approve/submit/reject reject an invalid source state with 422', async ({
		page,
		tenantManager,
		tenantClerk
	}) => {
		let reportId: string | null = null;
		let expenseId: string | null = null;
		try {
			const clerkH = await roleHeaders(page, tenantClerk);
			const reportNumber = `E2E-CTRL-422-${Date.now()}`;
			// A fresh DRAFT report (not submitted).
			const expResp = await page.request.post(`${API_BASE}/api/expenses`, {
				headers: clerkH,
				data: {
					merchant: `E2E 422 ${Date.now()}`,
					amount: '15.00',
					currency: 'USD',
					expense_date: '2026-03-02',
					category: 'meals'
				}
			});
			expect(expResp.status()).toBe(201);
			expenseId = ((await expResp.json()) as Created).id;
			const rptResp = await page.request.post(`${API_BASE}/api/expense-reports`, {
				headers: clerkH,
				data: { report_number: reportNumber, title: 'WF3 422', currency: 'USD' }
			});
			expect(rptResp.status()).toBe(201);
			reportId = ((await rptResp.json()) as Created).id;

			const managerH = await roleHeaders(page, tenantManager);
			// Approving a DRAFT (not submitted) report → 422.
			const approveDraft = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/approve`,
				{ headers: managerH, data: {} }
			);
			expect(approveDraft.status()).toBe(422);
			// Rejecting a DRAFT (not submitted) report → 422.
			const rejectDraft = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/reject`,
				{ headers: managerH, data: {} }
			);
			expect(rejectDraft.status()).toBe(422);

			// Submit it (clerk owner), then prove a double-submit is 422.
			const submit = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/submit`,
				{ headers: clerkH, data: {} }
			);
			expect(submit.status()).toBe(200);
			const resubmit = await page.request.post(
				`${API_BASE}/api/expense-reports/${reportId}/submit`,
				{ headers: clerkH, data: {} }
			);
			expect(resubmit.status()).toBe(422);
		} finally {
			if (reportId) deleteReport(reportId);
			if (expenseId) deleteExpense(expenseId);
		}
	});

	// --- Pre-approval segregation -------------------------------------------

	test('a requester cannot decide their own pre-approval (self-decide 403)', async ({
		page,
		tenantManager
	}) => {
		// The manager raises a request, then tries to approve / reject it. The
		// requester IS the decider, so segregation of duties must 403 — even
		// though the manager normally holds the decide role.
		let paId: string | null = null;
		try {
			const managerH = await roleHeaders(page, tenantManager);
			const paResp = await page.request.post(`${API_BASE}/api/expense-preapprovals`, {
				headers: managerH,
				data: {
					title: `E2E Self-Decide ${Date.now()}`,
					estimated_amount: '800.00',
					currency: 'USD',
					category: 'travel'
				}
			});
			expect(paResp.status()).toBe(201);
			paId = ((await paResp.json()) as Created).id;

			// Self-approve → 403, and the request stays pending.
			const selfApprove = await page.request.post(
				`${API_BASE}/api/expense-preapprovals/${paId}/approve`,
				{ headers: managerH, data: {} }
			);
			expect(selfApprove.status()).toBe(403);
			expect(
				tenantPsql(`SELECT status FROM expense_preapprovals WHERE id='${paId}'`).trim()
			).toBe('pending');

			// Self-reject is also blocked (the requester can't decide either way).
			const selfReject = await page.request.post(
				`${API_BASE}/api/expense-preapprovals/${paId}/reject`,
				{ headers: managerH, data: {} }
			);
			expect(selfReject.status()).toBe(403);
			expect(
				tenantPsql(`SELECT status FROM expense_preapprovals WHERE id='${paId}'`).trim()
			).toBe('pending');
		} finally {
			if (paId) deletePreapproval(paId);
		}
	});
});
