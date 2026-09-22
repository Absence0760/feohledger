import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';

async function apiHeaders(page: import('@playwright/test').Page) {
	return await authedTenantHeaders(page);
}

interface ExceptionItem {
	id: string;
	status: string;
	exception_type: string;
	is_overdue: boolean;
	due_at: string | null;
	assigned_to_user_id: string | null;
	assigned_to: string | null;
	time_to_resolution_hours: number | null;
}

async function fetchExceptions(
	page: import('@playwright/test').Page,
	params = ''
): Promise<ExceptionItem[]> {
	const headers = await apiHeaders(page);
	const resp = await page.request.get(`${API_BASE}/api/exceptions${params}`, { headers });
	const body = (await resp.json()) as { items: ExceptionItem[] };
	return body.items;
}

async function fetchAdminUserId(
	page: import('@playwright/test').Page,
	adminEmail: string
): Promise<{ id: string; full_name: string }> {
	const headers = await apiHeaders(page);
	const resp = await page.request.get(`${API_BASE}/api/admin/users`, { headers });
	const body = (await resp.json()) as {
		items: Array<{ id: string; email: string; full_name: string }>;
	};
	const admin = body.items.find((u) => u.email === adminEmail);
	if (!admin) throw new Error(`${adminEmail} not found`);
	return { id: admin.id, full_name: admin.full_name };
}

/**
 * Insert `n` fresh `open` exceptions straight into the worker's tenant DB —
 * each attached to a real seeded invoice (FK + organization_id copied from
 * it) — and return their ids.
 *
 * Self-seeding so these specs no longer depend on how many open exceptions
 * the seed script happens to leave around. It leaves exactly ONE, which made
 * the old `if (open.length < 2) test.skip()` guard on the bulk-resolve test a
 * silent always-skip, and the assign tests one mutation away from skipping
 * too. The `created_at` / `updated_at` columns fill from their server
 * defaults, so a minimal column list is enough.
 */
// `missing_data`, deliberately not a payment-blocking type (`duplicate`,
// `fraud_flag`, …): clearing one of those is refused to anyone implicated in
// the linked invoice (decisions §169–§170), and `invoices LIMIT 1` is whatever
// row the heap yields first — once an earlier spec in the same tenant leaves an
// invoice uploaded by this worker's admin in front, every dismiss here is a
// segregation refusal and `updated` reads 0. These specs exercise assignment
// and bulk mechanics, not segregation, so the seed must not depend on which
// invoice it lands on.
function seedOpenExceptions(n: number): string[] {
	const out = tenantPsql(
		`INSERT INTO exceptions (id, invoice_id, organization_id, exception_type, severity, description, status)
		 SELECT gen_random_uuid(), i.id, i.organization_id, 'missing_data', 'warning', 'e2e self-seeded open exception', 'open'
		 FROM (SELECT id, organization_id FROM invoices LIMIT 1) i, generate_series(1, ${n})
		 RETURNING id`
	);
	// psql appends a command-tag line ("INSERT 0 2") after the RETURNING
	// rows; keep only UUID-shaped lines so it never leaks into the id list.
	const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
	return out
		.split('\n')
		.map((s) => s.trim())
		.filter((s) => UUID.test(s));
}

/** Delete rows seeded by `seedOpenExceptions` so repeated local runs don't
 *  accumulate exceptions in the tenant DB. Called from each test's `finally`. */
function deleteExceptions(ids: string[]): void {
	if (ids.length === 0) return;
	const list = ids.map((id) => `'${id}'`).join(',');
	tenantPsql(`DELETE FROM exceptions WHERE id IN (${list})`);
}

/**
 * Exception queue improvements (Workflow / Approvals & Exceptions
 * roadmap section): assignment, SLA / overdue display, and bulk
 * resolve. Backend wiring lands here; UI bits land alongside the
 * existing /exceptions page in a follow-up.
 */

test.describe('/api/exceptions — assignment + bulk resolve', () => {
	test('list response carries the new SLA + assignee fields', async ({ page }) => {
		const items = await fetchExceptions(page);
		// We don't assert that ANY exception is overdue (depends on SLA
		// config) — only that the field exists on every row.
		expect(items.length).toBeGreaterThan(0);
		for (const it of items) {
			expect(it).toHaveProperty('is_overdue');
			expect(it).toHaveProperty('due_at');
			expect(it).toHaveProperty('assigned_to_user_id');
			expect(it).toHaveProperty('time_to_resolution_hours');
		}
	});

	test('assign: PATCH-style endpoint sets the user, unassign clears it', async ({
		page,
		tenantAdmin
	}) => {
		const headers = await apiHeaders(page);

		const [exceptionId] = seedOpenExceptions(1);
		try {
			const { id: userId, full_name: adminName } = await fetchAdminUserId(
				page,
				tenantAdmin.email
			);

			// Assign.
			const r1 = await page.request.post(
				`${API_BASE}/api/exceptions/${exceptionId}/assign`,
				{
					headers: { ...headers, 'Content-Type': 'application/json' },
					data: { user_id: userId }
				}
			);
			expect(r1.status()).toBe(200);
			const after = (await r1.json()) as ExceptionItem;
			expect(after.assigned_to_user_id).toBe(userId);
			expect(after.assigned_to).toBe(adminName);

			// Unassign.
			const r2 = await page.request.post(
				`${API_BASE}/api/exceptions/${exceptionId}/assign`,
				{
					headers: { ...headers, 'Content-Type': 'application/json' },
					data: { user_id: null }
				}
			);
			expect(r2.status()).toBe(200);
			const cleared = (await r2.json()) as ExceptionItem;
			expect(cleared.assigned_to_user_id).toBe(null);
			expect(cleared.assigned_to).toBe(null);
		} finally {
			deleteExceptions([exceptionId]);
		}
	});

	test('assign: cross-org user is rejected as 404', async ({ page }) => {
		const headers = await apiHeaders(page);
		// A real open exception must exist — the handler 404s on a missing
		// exception before it ever validates the user_id, so this would pass
		// for the wrong reason against an empty queue.
		const [exceptionId] = seedOpenExceptions(1);
		try {
			const fakeUuid = '00000000-0000-0000-0000-000000000000';
			const resp = await page.request.post(
				`${API_BASE}/api/exceptions/${exceptionId}/assign`,
				{
					headers: { ...headers, 'Content-Type': 'application/json' },
					data: { user_id: fakeUuid }
				}
			);
			expect(resp.status()).toBe(404);
		} finally {
			deleteExceptions([exceptionId]);
		}
	});

	test('assign: 400 on malformed user_id', async ({ page }) => {
		const headers = await apiHeaders(page);
		// Same reason as the cross-org case: the exception lookup happens
		// before user_id parsing, so the 400 path needs a real exception.
		const [exceptionId] = seedOpenExceptions(1);
		try {
			const resp = await page.request.post(
				`${API_BASE}/api/exceptions/${exceptionId}/assign`,
				{
					headers: { ...headers, 'Content-Type': 'application/json' },
					data: { user_id: 'not-a-uuid' }
				}
			);
			expect(resp.status()).toBe(400);
		} finally {
			deleteExceptions([exceptionId]);
		}
	});

	test('assign: clerk role gets 403', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const headers = await apiHeaders(page);
		const fakeId = '00000000-0000-0000-0000-000000000000';
		const resp = await page.request.post(
			`${API_BASE}/api/exceptions/${fakeId}/assign`,
			{
				headers: { ...headers, 'Content-Type': 'application/json' },
				data: { user_id: null }
			}
		);
		expect(resp.status()).toBe(403);
	});

	test('bulk resolve: handles partial success with reasons', async ({ page }) => {
		const headers = await apiHeaders(page);

		const seeded = seedOpenExceptions(2);
		try {
			const fakeId = '00000000-0000-0000-0000-000000000000';
			const ids = [seeded[0], seeded[1], fakeId];

			const resp = await page.request.post(
				`${API_BASE}/api/exceptions/bulk/resolve`,
				{
					headers: { ...headers, 'Content-Type': 'application/json' },
					data: { ids, action: 'dismiss', resolution: 'e2e bulk dismiss' }
				}
			);
			expect(resp.status()).toBe(200);
			const body = (await resp.json()) as {
				updated: number;
				skipped: Array<{ id: string; reason: string }>;
			};
			expect(body.updated).toBe(2);
			expect(body.skipped).toHaveLength(1);
			expect(body.skipped[0].id).toBe(fakeId);
			expect(body.skipped[0].reason).toBe('not_found');

			// time_to_resolution_hours populated on the dismissed rows.
			const after = await fetchExceptions(page);
			for (const id of [seeded[0], seeded[1]]) {
				const row = after.find((e) => e.id === id);
				expect(row?.status).toBe('dismissed');
				expect(row?.time_to_resolution_hours).not.toBeNull();
			}
		} finally {
			deleteExceptions(seeded);
		}
	});

	test('bulk resolve: rejects unknown action with 400', async ({ page }) => {
		const headers = await apiHeaders(page);
		const open = await fetchExceptions(page, '?status=dismissed');
		const ids = open.slice(0, 1).map((e) => e.id);
		if (ids.length === 0) ids.push('00000000-0000-0000-0000-000000000000');

		const resp = await page.request.post(
			`${API_BASE}/api/exceptions/bulk/resolve`,
			{
				headers: { ...headers, 'Content-Type': 'application/json' },
				data: { ids, action: 'novel_action', resolution: 'x' }
			}
		);
		expect(resp.status()).toBe(400);
	});

	test('list filter ?assigned_to_user_id narrows to that assignee', async ({
		page,
		tenantAdmin
	}) => {
		const headers = await apiHeaders(page);
		const [exceptionId] = seedOpenExceptions(1);
		try {
			const { id: userId } = await fetchAdminUserId(page, tenantAdmin.email);

			// Assign the seeded exception to the admin so the filter has a hit.
			await page.request.post(
				`${API_BASE}/api/exceptions/${exceptionId}/assign`,
				{
					headers: { ...headers, 'Content-Type': 'application/json' },
					data: { user_id: userId }
				}
			);

			const filtered = await fetchExceptions(page, `?assigned_to_user_id=${userId}`);
			expect(filtered.length).toBeGreaterThan(0);
			for (const e of filtered) {
				expect(e.assigned_to_user_id).toBe(userId);
			}
		} finally {
			// Deleting the row removes its assignment too — no unassign needed.
			deleteExceptions([exceptionId]);
		}
	});
});
