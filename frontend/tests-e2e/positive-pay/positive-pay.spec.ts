import type { Route } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	test
} from '../fixtures/helpers';

/**
 * /positive-pay — Positive Pay / Payment Fraud File.
 *
 * Exercises the Positive Pay surface against the same contract the backend
 * `/api/positive-pay` router + the `/positive-pay` frontend route are built to:
 * render the list page (header, KPI row, filter chips, table), open the
 * generate modal, and confirm the RBAC posture (read = admin/ap_manager/cfo,
 * write = admin/ap_manager; clerks excluded entirely — a treasury control).
 *
 * Login model mirrors the rest of the suite: the default per-worker storage
 * state signs the worker's admin in (an admin is in both the read and the
 * mutate set), so the page loads directly without a redirect. The clerk
 * describe block opts out and signs in as the clerk.
 *
 * Robust without a pre-seeded executed check run: every assertion targets the
 * rendered page, the modal, or a direct API RBAC check — no seeded Positive Pay
 * file is required.
 */

test.describe('/positive-pay (admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/positive-pay');
	});

	test('renders the Positive Pay surface — header, KPIs, filters, table', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Positive Pay' })).toBeVisible();

		// KPI row.
		await expect(page.locator('.kpi').first()).toBeVisible({ timeout: 10_000 });

		// File-type filter chips.
		await expect(page.locator('.filter-chip', { hasText: 'All' })).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: 'Check issue' })).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: 'ACH auth' })).toBeVisible();

		// The files table renders (seeded rows or the centred empty state).
		await expect(page.locator('.grid-container table')).toBeVisible();
	});

	test('switching the file-type filter re-requests the list', async ({ page }) => {
		const respPromise = page.waitForResponse(
			(r) => r.url().includes('/api/positive-pay') && r.url().includes('file_type=ach_authorization')
		);
		await page.locator('.filter-chip', { hasText: 'ACH auth' }).click();
		const resp = await respPromise;
		expect(resp.request().url()).toContain('file_type=ach_authorization');
	});

	test('the generate modal opens with the expected aria-label', async ({ page }) => {
		await page.getByRole('button', { name: '+ Generate file' }).click();
		const dialog = page.getByRole('dialog', { name: 'Generate positive pay file' });
		await expect(dialog).toBeVisible();
		// File-type + bank-format controls are present.
		await expect(dialog.getByLabel('File type')).toBeVisible();
		await expect(dialog.getByLabel('Bank format')).toBeVisible();
	});

	/**
	 * The run picker must never offer a run that has not executed.
	 *
	 * A check-issue file generated from a draft persists an EMPTY issued map,
	 * and generation is idempotent per (run, bank_format) — so it can never be
	 * regenerated after the run executes. Every cheque the bank later presents
	 * then classifies `not_on_file` and raises a false `fraud_flag` exception
	 * against a real payment. The picker is the only door to that state.
	 *
	 * The runs list is stubbed rather than seeded: the assertion is about which
	 * runs the UI offers, and a fixed draft/executed pair makes it exact without
	 * creating or executing a real payment run (money-moving) in the tenant.
	 */
	const DRAFT_RUN_ID = '11111111-1111-4111-8111-111111111111';
	const EXECUTED_RUN_ID = '22222222-2222-4222-8222-222222222222';

	async function stubRuns(
		page: import('@playwright/test').Page,
		items: Array<Record<string, unknown>>
	) {
		await page.route(
			(url) => url.pathname === '/api/payments/runs/',
			(route) => route.fulfill({ json: { items, total: items.length } })
		);
	}

	test('the run picker offers executed runs only — never a draft', async ({ page }) => {
		await stubRuns(page, [
			{ id: DRAFT_RUN_ID, status: 'draft', executed_at: null, total_amount: 1000 },
			{
				id: EXECUTED_RUN_ID,
				status: 'completed',
				executed_at: '2026-01-15T10:00:00Z',
				total_amount: 2500
			}
		]);

		await page.getByRole('button', { name: '+ Generate file' }).click();
		const dialog = page.getByRole('dialog', { name: 'Generate positive pay file' });
		await expect(dialog).toBeVisible();

		// File type defaults to check_issue, so the run control is already shown.
		// `exact` matters: the File-type select's accessible name folds in its
		// option text ("Check issue (per payment run)"), which substring-matches.
		const select = dialog.getByLabel('Payment run', { exact: true });
		await expect(select).toBeVisible();

		// The executed run is selectable; the draft is not present at all.
		await expect(select.locator(`option[value="${EXECUTED_RUN_ID}"]`)).toHaveCount(1);
		await expect(select.locator(`option[value="${DRAFT_RUN_ID}"]`)).toHaveCount(0);
		// Placeholder + the one executed run, and nothing else.
		await expect(select.locator('option')).toHaveCount(2);
	});

	test('with only draft runs the picker explains itself instead of accepting one', async ({
		page
	}) => {
		// The pre-fix fallback was a free-text run-id box whenever the list came
		// back empty — which is exactly the state a draft-only tenant is in, so a
		// draft id could still be typed straight back in. Loaded-but-empty must
		// therefore be a dead end, not a text input.
		await stubRuns(page, [
			{ id: DRAFT_RUN_ID, status: 'draft', executed_at: null, total_amount: 1000 }
		]);

		await page.getByRole('button', { name: '+ Generate file' }).click();
		const dialog = page.getByRole('dialog', { name: 'Generate positive pay file' });
		await expect(dialog).toBeVisible();

		await expect(dialog.getByTestId('no-executed-runs')).toBeVisible();
		await expect(dialog.getByLabel('Payment run', { exact: true })).toHaveCount(0);
		await expect(dialog.getByLabel('Payment run id', { exact: true })).toHaveCount(0);
		// And nothing can be submitted — Generate stays disabled.
		await expect(dialog.getByRole('button', { name: 'Generate' })).toBeDisabled();
	});

	test('the file total renders in the stored per-file currency, not a hardcoded USD', async ({
		page
	}) => {
		// Each file now carries its OWN currency (the org reporting currency
		// stamped at generation). Patch the list response to a non-USD currency
		// and assert the total cell follows it — a hardcoded USD fallback would
		// show "$". Fresh page per test → no leak into the USD-default tests.
		await page.route('**/api/positive-pay?**', async (route) => {
			if (route.request().method() !== 'GET') return route.continue();
			const resp = await route.fetch();
			const body = await resp.json();
			body.items = (body.items ?? []).map((f: Record<string, unknown>) => ({
				...f,
				currency: 'EUR'
			}));
			await route.fulfill({ response: resp, json: body });
		});

		let id: string | null = null;
		try {
			// page.request bypasses page.route, so this create hits the real API.
			const resp = await page.request.post(`${API_BASE}/api/positive-pay/ach-authorization`, {
				headers: await authedTenantHeaders(page),
				data: { bank_format: 'csv' }
			});
			expect(resp.ok()).toBeTruthy();
			id = ((await resp.json()) as { id: string }).id;

			await page.goto('/positive-pay?file_type=ach_authorization');

			const row = page.locator('tr', { hasText: id!.slice(0, 8) });
			await expect(row).toBeVisible({ timeout: 10_000 });
			// Total cell (file, type, format, items, TOTAL) — 5th column, index 4.
			const totalCell = row.locator('td').nth(4);
			await expect(totalCell).toContainText('€');
			await expect(totalCell).not.toContainText('$');
		} finally {
			if (id) {
				await page.request.delete(`${API_BASE}/api/positive-pay/${id}`, {
					headers: await authedTenantHeaders(page)
				});
			}
		}
	});

	test('a file with no stored currency renders its total bare, not in the org default', async ({
		page
	}) => {
		// `currency: null` is a legacy row, or a cheque run whose invoices
		// disagree — nobody established what the total is in. The list cell and
		// the detail dialog used to write `file.currency ?? orgCurrency.currency`
		// and so rendered "$0.00" here; the figure now renders bare
		// (`docs/decisions.md` §196).
		const nullCurrency = async (route: Route) => {
			if (route.request().method() !== 'GET') return route.continue();
			const resp = await route.fetch();
			const body = await resp.json();
			const strip = (f: Record<string, unknown>) => ({ ...f, currency: null });
			await route.fulfill({
				response: resp,
				json: Array.isArray(body.items) ? { ...body, items: body.items.map(strip) } : strip(body)
			});
		};
		await page.route('**/api/positive-pay?**', nullCurrency);

		let id: string | null = null;
		try {
			const resp = await page.request.post(`${API_BASE}/api/positive-pay/ach-authorization`, {
				headers: await authedTenantHeaders(page),
				data: { bank_format: 'csv' }
			});
			expect(resp.ok()).toBeTruthy();
			id = ((await resp.json()) as { id: string }).id;
			await page.route(`**/api/positive-pay/${id}`, nullCurrency);

			await page.goto('/positive-pay?file_type=ach_authorization');
			const row = page.locator('tr', { hasText: id!.slice(0, 8) });
			await expect(row).toBeVisible({ timeout: 10_000 });
			const totalCell = row.locator('td').nth(4);
			// An ACH authorization file's total is always zero: the figure, bare.
			await expect(totalCell).toHaveText(/^\s*0[.,]00\s*$/);

			await page.goto(`/positive-pay?id=${id}`);
			const total = page
				.getByRole('dialog')
				.locator('.total-box', { hasText: 'Total amount' })
				.locator('.total-value');
			await expect(total).toHaveText(/^\s*0[.,]00\s*$/);
		} finally {
			if (id) {
				await page.request.delete(`${API_BASE}/api/positive-pay/${id}`, {
					headers: await authedTenantHeaders(page)
				});
			}
		}
	});

	test('a generated ACH file stamps + returns the org reporting currency (USD default)', async ({
		page
	}) => {
		// No reporting currency configured → the file falls back to the platform
		// default (USD) and returns it, proving the column is populated at
		// creation (not left null for fresh files).
		let id: string | null = null;
		try {
			const resp = await page.request.post(`${API_BASE}/api/positive-pay/ach-authorization`, {
				headers: await authedTenantHeaders(page),
				data: { bank_format: 'csv' }
			});
			expect(resp.ok()).toBeTruthy();
			const file = (await resp.json()) as { id: string; currency: string };
			id = file.id;
			expect(file.currency).toBe('USD');
		} finally {
			if (id) {
				await page.request.delete(`${API_BASE}/api/positive-pay/${id}`, {
					headers: await authedTenantHeaders(page)
				});
			}
		}
	});

	test('generating an ACH authorization file lands it in the list', async ({ page }) => {
		// ACH authorization needs no payment run, so it generates from any tenant
		// state. Drive it via the API (the modal path is covered above) then assert
		// the row renders, and clean up.
		let id: string | null = null;
		try {
			const resp = await page.request.post(`${API_BASE}/api/positive-pay/ach-authorization`, {
				headers: await authedTenantHeaders(page),
				data: { bank_format: 'csv' }
			});
			expect(resp.ok()).toBeTruthy();
			const file = (await resp.json()) as { id: string; file_type: string };
			id = file.id;
			expect(file.file_type).toBe('ach_authorization');

			await page.goto('/positive-pay?file_type=ach_authorization');
			await expect(
				page.getByRole('button', { name: new RegExp(`Open Positive Pay file.*${id!.slice(0, 8)}`) })
			).toBeVisible({ timeout: 10_000 });
		} finally {
			if (id) {
				await page.request.delete(`${API_BASE}/api/positive-pay/${id}`, {
					headers: await authedTenantHeaders(page)
				});
			}
		}
	});
});

test.describe('/positive-pay (clerk — no access)', () => {
	// Opt out of the default admin storage state so we can sign in as the clerk.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is excluded from Positive Pay (no nav, write API 403s)', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);

		// Positive Pay is gated to admin/ap_manager/cfo — the clerk never sees the
		// nav entry.
		await page.goto('/');
		// The sidebar has to be rendered before an absence check means anything:
		// the pre-hydration document is empty (the root layout renders nothing
		// until its browser-only `$effect` resolves the tenant), so a bare
		// `toHaveCount(0)` would pass vacuously against it.
		await expect(page.locator('aside.sidebar').first()).toBeVisible();
		await expect(page.getByRole('link', { name: 'Positive Pay' })).toHaveCount(0);

		// And a mutate call is rejected by the backend (admin / ap_manager only).
		// `require_roles` runs before the handler body, so the POST 403s regardless
		// of payload.
		const resp = await page.request.post(`${API_BASE}/api/positive-pay/ach-authorization`, {
			headers: await authedTenantHeaders(page),
			data: { bank_format: 'csv' }
		});
		expect(resp.status()).toBe(403);
	});
});
