import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * Credit-memo money-path controls (API-level).
 *
 * Locks the invariants a credit memo touches: applying one reduces a
 * vendor/invoice payable, so it MUST
 *   - write an append-only audit row on create / apply / void,
 *   - refuse to over-apply (credit > the invoice's remaining balance),
 *   - be RBAC-gated (mutate = admin / ap_manager only),
 *   - carry exact Decimal money (no float drift in the balance math).
 *
 * Each test provisions its own vendor invoice + memos via the API and
 * tears them down with psql in finally (the product has no memo-delete
 * endpoint by design — memos are kept for audit).
 */

interface Vendor {
	id: string;
	name: string;
}

/** A coded refusal `detail` (`backend/app/api/refusals.py::coded_refusal`). */
interface CodedDetail {
	code: string;
	message: string;
	params: Record<string, unknown>;
}

async function firstVendor(page: import('@playwright/test').Page): Promise<Vendor> {
	const resp = await page.request.get(`${API_BASE}/api/vendors`, {
		headers: await authedTenantHeaders(page)
	});
	const body = (await resp.json()) as { items: Vendor[] };
	return body.items[0];
}

/** Create an approved invoice firmly bound to `vendorId` (psql forces the FK
 *  so the vendor-match guard is deterministic regardless of name fuzzing). */
async function makeInvoice(
	page: import('@playwright/test').Page,
	vendor: Vendor,
	amount: number
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			vendor: vendor.name,
			invoice_number: `CM-MC-${Date.now()}-${Math.floor(Math.random() * 1e6)}`,
			amount
		}
	});
	const inv = (await resp.json()) as { id: string };
	// POST /api/invoices intentionally ignores a client-supplied status (the
	// status-injection fix). Force `approved` and bind vendor_id via SQL so
	// the credit-memo vendor-match guard is deterministic.
	tenantPsql(`UPDATE invoices SET status='approved', vendor_id='${vendor.id}' WHERE id='${inv.id}'`);
	return inv.id;
}

// NOTE: audit_log is append-only at the DB level (immutability trigger), so
// cleanup never deletes audit rows — leftover rows pointing at deleted test
// entities are harmless residue. We only remove the entity rows themselves.
function cleanupInvoice(id: string): void {
	tenantPsql(
		`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id='${id}')`
	);
	tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${id}'`);
	// The matched vendor may be seeded `unverified` — refresh_warnings (now run
	// at manual-entry creation time) raises an `unverified_vendor` exception
	// against it, which FKs to this invoice and must clear before the delete.
	tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${id}'`);
	deleteInvoicesWhere(`id='${id}'`);
}

function deleteMemo(id: string): void {
	tenantPsql(`DELETE FROM credit_memos WHERE id='${id}'`);
}

function memoAuditActions(id: string): string[] {
	const out = tenantPsql(
		`SELECT action FROM audit_log WHERE entity_id='${id}' AND entity_type='credit_memo' ORDER BY created_at`
	);
	return out
		.split('\n')
		.map((s) => s.trim())
		.filter(Boolean);
}

test.describe('credit-memo money controls (API)', () => {
	test('create + apply each write an append-only audit row', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const vendor = await firstVendor(page);
		const invoiceId = await makeInvoice(page, vendor, 500);
		let memoId: string | null = null;

		try {
			const createResp = await page.request.post(`${API_BASE}/api/credit-memos`, {
				headers,
				data: { memo_number: `CM-AUD-${Date.now()}`, vendor_id: vendor.id, amount: 120 }
			});
			expect(createResp.status()).toBe(201);
			memoId = ((await createResp.json()) as { id: string }).id;

			// One audit row after create.
			expect(memoAuditActions(memoId)).toEqual(['credit_memo.created']);

			const applyResp = await page.request.post(
				`${API_BASE}/api/credit-memos/${memoId}/apply`,
				{ headers, data: { invoice_id: invoiceId } }
			);
			expect(applyResp.status()).toBe(200);
			expect(((await applyResp.json()) as { status: string }).status).toBe('applied');

			// Apply appended a second row — the trail is append-only.
			expect(memoAuditActions(memoId)).toEqual([
				'credit_memo.created',
				'credit_memo.applied'
			]);

			// Money never leaks into the audit details (PII/exactness): amount is a
			// string-Decimal, not a float, and there's no bank/tax field.
			const detail = tenantPsql(
				`SELECT details->>'amount' FROM audit_log WHERE entity_id='${memoId}' AND action='credit_memo.applied'`
			).trim();
			expect(detail).toBe('120.00');
		} finally {
			if (memoId) deleteMemo(memoId);
			cleanupInvoice(invoiceId);
		}
	});

	test('void writes an audit row; an applied memo cannot be voided', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const vendor = await firstVendor(page);
		let openMemoId: string | null = null;
		let appliedMemoId: string | null = null;
		const invoiceId = await makeInvoice(page, vendor, 300);

		try {
			// Open memo → void OK + audit row.
			const openResp = await page.request.post(`${API_BASE}/api/credit-memos`, {
				headers,
				data: { memo_number: `CM-V-${Date.now()}`, vendor_id: vendor.id, amount: 40 }
			});
			openMemoId = ((await openResp.json()) as { id: string }).id;
			const voidResp = await page.request.post(
				`${API_BASE}/api/credit-memos/${openMemoId}/void`,
				{ headers }
			);
			expect(voidResp.status()).toBe(200);
			expect(((await voidResp.json()) as { status: string }).status).toBe('void');
			expect(memoAuditActions(openMemoId)).toEqual([
				'credit_memo.created',
				'credit_memo.voided'
			]);

			// Applied-at-creation memo → void refused (immutable for audit).
			const appliedResp = await page.request.post(`${API_BASE}/api/credit-memos`, {
				headers,
				data: {
					memo_number: `CM-VA-${Date.now()}`,
					vendor_id: vendor.id,
					amount: 25,
					invoice_id: invoiceId
				}
			});
			const applied = (await appliedResp.json()) as { id: string; status: string };
			appliedMemoId = applied.id;
			expect(applied.status).toBe('applied');
			const refused = await page.request.post(
				`${API_BASE}/api/credit-memos/${appliedMemoId}/void`,
				{ headers }
			);
			expect(refused.status()).toBe(409);
			// No void row was written — the refusal is pre-mutation.
			expect(memoAuditActions(appliedMemoId)).toEqual(['credit_memo.created']);
		} finally {
			if (openMemoId) deleteMemo(openMemoId);
			if (appliedMemoId) deleteMemo(appliedMemoId);
			cleanupInvoice(invoiceId);
		}
	});

	test('cannot over-apply — total credit may not exceed the invoice balance', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);
		const vendor = await firstVendor(page);
		// Invoice worth 100.00 exactly.
		const invoiceId = await makeInvoice(page, vendor, 100);
		const memoIds: string[] = [];

		try {
			// First memo of 60.00 applies fine (remaining 100 → 40).
			const m1 = await page.request.post(`${API_BASE}/api/credit-memos`, {
				headers,
				data: { memo_number: `CM-OA1-${Date.now()}`, vendor_id: vendor.id, amount: 60 }
			});
			const memo1 = ((await m1.json()) as { id: string }).id;
			memoIds.push(memo1);
			const apply1 = await page.request.post(
				`${API_BASE}/api/credit-memos/${memo1}/apply`,
				{ headers, data: { invoice_id: invoiceId } }
			);
			expect(apply1.status()).toBe(200);

			// Second memo of 50.00 would push total credit to 110 > 100 → blocked.
			const m2 = await page.request.post(`${API_BASE}/api/credit-memos`, {
				headers,
				data: { memo_number: `CM-OA2-${Date.now()}`, vendor_id: vendor.id, amount: 50 }
			});
			const memo2 = ((await m2.json()) as { id: string }).id;
			memoIds.push(memo2);
			const apply2 = await page.request.post(
				`${API_BASE}/api/credit-memos/${memo2}/apply`,
				{ headers, data: { invoice_id: invoiceId } }
			);
			expect(apply2.status()).toBe(409);
			// Coded (`api/refusals.coded_refusal`): the client localizes on `code`.
			const refusal2 = ((await apply2.json()) as { detail: CodedDetail }).detail;
			expect(refusal2.code).toBe('credit_memo_exceeds_balance');
			expect(refusal2.message).toContain('remaining creditable balance');
			// memo2 stays open — the blocked apply did not mutate it.
			const memo2Get = await page.request.get(`${API_BASE}/api/credit-memos?status=open`, {
				headers
			});
			const openItems = (
				(await memo2Get.json()) as { items: Array<{ id: string; status: string }> }
			).items;
			expect(openItems.find((m) => m.id === memo2)?.status).toBe('open');

			// A memo of exactly the remaining 40.00 still fits the boundary.
			const m3 = await page.request.post(`${API_BASE}/api/credit-memos`, {
				headers,
				data: { memo_number: `CM-OA3-${Date.now()}`, vendor_id: vendor.id, amount: 40 }
			});
			const memo3 = ((await m3.json()) as { id: string }).id;
			memoIds.push(memo3);
			const apply3 = await page.request.post(
				`${API_BASE}/api/credit-memos/${memo3}/apply`,
				{ headers, data: { invoice_id: invoiceId } }
			);
			expect(apply3.status()).toBe(200);
		} finally {
			for (const id of memoIds) deleteMemo(id);
			cleanupInvoice(invoiceId);
		}
	});

	test('over-application is also blocked when applying at creation time', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const vendor = await firstVendor(page);
		const invoiceId = await makeInvoice(page, vendor, 80);

		try {
			// Creating a memo of 81.00 directly linked to an 80.00 invoice → 409.
			const resp = await page.request.post(`${API_BASE}/api/credit-memos`, {
				headers,
				data: {
					memo_number: `CM-CC-${Date.now()}`,
					vendor_id: vendor.id,
					amount: 81,
					invoice_id: invoiceId
				}
			});
			expect(resp.status()).toBe(409);
			const refusal = ((await resp.json()) as { detail: CodedDetail }).detail;
			expect(refusal.code).toBe('credit_memo_exceeds_balance');
			expect(refusal.message).toContain('remaining creditable balance');
			// The balance travels as an exact string beside its currency.
			expect(refusal.params.remaining).toBe('80.00');
			expect(refusal.params.currency).toMatch(/^[A-Z]{3}$/);
		} finally {
			cleanupInvoice(invoiceId);
		}
	});

	test('RBAC: ap_clerk cannot create or apply a credit memo', async ({ page, tenantClerk }) => {
		const adminHeaders = await authedTenantHeaders(page);
		const vendor = await firstVendor(page);

		// Sign the clerk in via the API to get a clerk JWT for the same tenant.
		const slug = adminHeaders['X-Tenant-Slug'];
		const loginResp = await page.request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': slug },
			data: { email: tenantClerk.email, password: tenantClerk.password }
		});
		expect(loginResp.status()).toBe(200);
		const clerkToken = ((await loginResp.json()) as { access_token: string }).access_token;
		const clerkHeaders = { Authorization: `Bearer ${clerkToken}`, 'X-Tenant-Slug': slug };

		// Clerk read is allowed (admin/ap_manager/ap_clerk/cfo).
		const listResp = await page.request.get(`${API_BASE}/api/credit-memos`, {
			headers: clerkHeaders
		});
		expect(listResp.status()).toBe(200);

		// Clerk create is forbidden (mutate = admin/ap_manager).
		const createResp = await page.request.post(`${API_BASE}/api/credit-memos`, {
			headers: clerkHeaders,
			data: { memo_number: `CM-RBAC-${Date.now()}`, vendor_id: vendor.id, amount: 10 }
		});
		expect(createResp.status()).toBe(403);

		// Admin makes a memo the clerk then can't apply or void.
		const adminCreate = await page.request.post(`${API_BASE}/api/credit-memos`, {
			headers: adminHeaders,
			data: { memo_number: `CM-RBAC2-${Date.now()}`, vendor_id: vendor.id, amount: 10 }
		});
		const memoId = ((await adminCreate.json()) as { id: string }).id;
		try {
			const clerkApply = await page.request.post(
				`${API_BASE}/api/credit-memos/${memoId}/apply`,
				{ headers: clerkHeaders, data: { invoice_id: vendor.id } }
			);
			expect(clerkApply.status()).toBe(403);
			const clerkVoid = await page.request.post(
				`${API_BASE}/api/credit-memos/${memoId}/void`,
				{ headers: clerkHeaders }
			);
			expect(clerkVoid.status()).toBe(403);
		} finally {
			deleteMemo(memoId);
		}
	});

	test('concurrent applies cannot over-credit the same invoice (row lock)', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);
		const vendor = await firstVendor(page);
		// Invoice worth 100.00; two open memos of 60.00 — each fits alone (≤100),
		// but together they'd be 120 > 100. Firing both /apply at once must NOT
		// let both through: the invoice row lock (`SELECT … FOR UPDATE`) serializes
		// the over-application guard, so exactly one wins (200) and the other is
		// refused (409). Without the lock both reads see an already-applied sum of
		// 0 and both pass — over-crediting the invoice.
		const invoiceId = await makeInvoice(page, vendor, 100);
		const memoIds: string[] = [];

		try {
			const mkMemo = async (n: number): Promise<string> => {
				const r = await page.request.post(`${API_BASE}/api/credit-memos`, {
					headers,
					data: {
						memo_number: `CM-RACE${n}-${Date.now()}-${Math.floor(Math.random() * 1e6)}`,
						vendor_id: vendor.id,
						amount: 60
					}
				});
				const id = ((await r.json()) as { id: string }).id;
				memoIds.push(id);
				return id;
			};
			const memoA = await mkMemo(1);
			const memoB = await mkMemo(2);

			const apply = (id: string) =>
				page.request.post(`${API_BASE}/api/credit-memos/${id}/apply`, {
					headers,
					data: { invoice_id: invoiceId }
				});
			// Fire both at the HTTP layer simultaneously.
			const [ra, rb] = await Promise.all([apply(memoA), apply(memoB)]);
			const statuses = [ra.status(), rb.status()].sort();
			// Exactly one 200, one 409 — never two 200s (that's the over-credit bug).
			expect(statuses).toEqual([200, 409]);

			// Ground truth in the DB: exactly one memo applied to the invoice, so
			// the applied total (60.00) never exceeds the invoice's 100.00.
			const appliedCount = tenantPsql(
				`SELECT count(*) FROM credit_memos WHERE invoice_id='${invoiceId}' AND status='applied'`
			).trim();
			expect(appliedCount).toBe('1');
		} finally {
			for (const id of memoIds) deleteMemo(id);
			cleanupInvoice(invoiceId);
		}
	});
});
