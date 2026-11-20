import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';
import {
	markSeededWorkflowVersions,
	purgeSeededWorkflowVersions,
	type SeededVersionMark
} from '../workflows/seededWorkflowVersions';

async function createUser(
	page: import('@playwright/test').Page,
	email: string
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/admin/users`, {
		headers: await authedTenantHeaders(page),
		data: { full_name: 'Delete Safety Test', email, role_names: ['ap_clerk'] }
	});
	return ((await resp.json()) as { id: string }).id;
}

async function deleteUser(page: import('@playwright/test').Page, id: string) {
	return page.request.delete(`${API_BASE}/api/admin/users/${id}`, {
		headers: await authedTenantHeaders(page)
	});
}

/**
 * /admin user-delete cascade safety. Deleting a user who is still
 * referenced by in-flight work would silently orphan the references —
 * an open invoice's `assigned_to_id` pointing at a deleted user, or a
 * pending workflow_step waiting on a deleted approver — leaving the
 * UI to render `null` and the workflow stuck. The DELETE endpoint
 * refuses with a 409 + structured reference counts; the admin must
 * reassign first.
 *
 * Audit-log / payment-initiator / invoice-uploader references are
 * INTENTIONALLY excluded from the safety check — those must survive
 * the user (audit data is append-only by design).
 */

test.describe('/admin user-delete safety', () => {
	// The approver test below PATCHes the SEEDED default workflow's steps to
	// put its throwaway user into `approver_ids`, then PATCHes them back — and
	// each of those writes an auto-saved `workflow_versions` row that no name
	// sweep can reach, because `deleteWorkflowsWhere` refuses `is_default` rows
	// on purpose (decisions §127). Same leak as the two `workflows/` specs;
	// this one was found by their guard rather than by the report that named
	// them. See `workflows/seededWorkflowVersions.ts`.
	let seededVersions: SeededVersionMark;
	test.beforeEach(() => {
		seededVersions = markSeededWorkflowVersions();
	});
	test.afterEach(() => purgeSeededWorkflowVersions(seededVersions));

	test('user with no references can be deleted', async ({ page }) => {
		const id = await createUser(page, `e2e-safe-${Date.now()}@test.local`);
		const resp = await deleteUser(page, id);
		expect(resp.status()).toBe(204);
	});

	test('refuses delete when user is assigned to an open invoice', async ({ page }) => {
		const id = await createUser(page, `e2e-inv-${Date.now()}@test.local`);

		// Pick a non-terminal invoice and stash its current assigned_to_id
		// so we can revert. Using a `new`/`ready_for_review` row keeps the
		// safety check tripped (status is in the "open" set).
		const before = JSON.parse(
			tenantPsql(
				"SELECT json_build_object('id', id::text, 'orig', assigned_to_id::text) "
					+ "FROM invoices WHERE status='new' LIMIT 1"
			)
		) as { id: string; orig: string | null };

		try {
			tenantPsql(`UPDATE invoices SET assigned_to_id='${id}' WHERE id='${before.id}'`);
			const resp = await deleteUser(page, id);
			expect(resp.status()).toBe(409);
			const body = (await resp.json()) as {
				detail: {
					message: string;
					references: {
						open_invoice_assignments: number;
						pending_approval_steps: number;
						active_workflow_approver_in: number;
					};
				};
			};
			expect(body.detail.references.open_invoice_assignments).toBeGreaterThanOrEqual(1);
		} finally {
			// Revert assignment, then delete the throwaway user.
			const restore = before.orig
				? `UPDATE invoices SET assigned_to_id='${before.orig}' WHERE id='${before.id}'`
				: `UPDATE invoices SET assigned_to_id=NULL WHERE id='${before.id}'`;
			tenantPsql(restore);
			await deleteUser(page, id);
		}
	});

	test('refuses delete when user has a pending workflow step assigned', async ({ page }) => {
		const id = await createUser(page, `e2e-step-${Date.now()}@test.local`);
		const headers = await authedTenantHeaders(page);

		// POST /api/invoices creates a workflow_instance per
		// services/workflow_engine.create_workflow_instance, which is the
		// FK target we need to insert a synthetic step against.
		const invResp = await page.request.post(`${API_BASE}/api/invoices`, {
			headers,
			data: {
				vendor: 'Delete Safety Step Vendor',
				invoice_number: `DSS-${Date.now()}`,
				amount: 100,
				currency: 'USD',
				status: 'new'
			}
		});
		const invoice = (await invResp.json()) as { id: string };

		const instanceId = tenantPsql(
			`SELECT id FROM workflow_instances WHERE invoice_id='${invoice.id}'`
		).trim();

		// Insert a synthetic pending step pointing at our user. Generate the
		// id client-side so we don't have to parse psql's "INSERT 0 1" tail.
		const stepRow = crypto.randomUUID();
		tenantPsql(
			`INSERT INTO workflow_steps (id, correlation_id, instance_id, step_number, step_type, assigned_to) `
				+ `VALUES ('${stepRow}', gen_random_uuid(), '${instanceId}', 99, 'approval', '${id}')`
		);

		try {
			const resp = await deleteUser(page, id);
			expect(resp.status()).toBe(409);
			const body = (await resp.json()) as {
				detail: { references: { pending_approval_steps: number } };
			};
			expect(body.detail.references.pending_approval_steps).toBeGreaterThanOrEqual(1);
		} finally {
			tenantPsql(`DELETE FROM workflow_steps WHERE id='${stepRow}'`);
			tenantPsql(`DELETE FROM workflow_instances WHERE id='${instanceId}'`);
			// The vendor name above is fresh, so create_invoice's vendor matcher
			// auto-created it `unverified` — refresh_warnings (now run at manual-
			// entry creation time) raises an `unverified_vendor` exception against
			// it, which FKs to this invoice and must clear before the invoices
			// delete below.
			tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${invoice.id}'`);
			// audit_log is append-only (DB trigger, migration 0022 + seed) — never DELETE;
			// orphan rows for the removed invoice are harmless (no FK back to invoices).
			deleteInvoicesWhere(`id='${invoice.id}'`);
			await deleteUser(page, id);
		}
	});

	test('refuses delete when user is in an active workflow def approver_ids', async ({
		page
	}) => {
		const id = await createUser(page, `e2e-defn-${Date.now()}@test.local`);
		const headers = await authedTenantHeaders(page);

		// Get the active workflow + put the user into approval.approver_ids.
		const wfsResp = await page.request.get(`${API_BASE}/api/workflows`, { headers });
		const wfs = (
			(await wfsResp.json()) as {
				items: Array<{
					id: string;
					is_active: boolean;
					steps_config: { steps: Array<{ type: string; config: Record<string, unknown> }> };
				}>;
			}
		).items;
		const active = wfs.find((w) => w.is_active);
		expect(active).toBeTruthy();
		const before = active!.steps_config;

		const withApprover = before.steps.map((s) =>
			s.type === 'approval'
				? { ...s, config: { ...s.config, approver_ids: [id] } }
				: s
		);

		try {
			await page.request.patch(`${API_BASE}/api/workflows/${active!.id}`, {
				headers,
				data: { steps: withApprover }
			});

			const resp = await deleteUser(page, id);
			expect(resp.status()).toBe(409);
			const body = (await resp.json()) as {
				detail: { references: { active_workflow_approver_in: number } };
			};
			expect(body.detail.references.active_workflow_approver_in).toBeGreaterThanOrEqual(1);
		} finally {
			await page.request.patch(`${API_BASE}/api/workflows/${active!.id}`, {
				headers,
				data: { steps: before.steps }
			});
			await deleteUser(page, id);
		}
	});
});
