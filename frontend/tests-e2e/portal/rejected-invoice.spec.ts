import type { Page } from '@playwright/test';

import {
	acceptConsent,
	currentTenantSlug,
	deleteInvoicesWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * Supplier portal — a rejected invoice is no longer a dead end (issue #328).
 *
 * 1. `GET /portal/invoices` carries `rejection_reason` (the latest
 *    `review_rejected` exception's description) for a `rejected` invoice; the
 *    list renders it under the status pill, never the rejecting employee's name.
 * 2. A "Revise & resubmit" file control on the rejected row swaps the document
 *    on the SAME invoice (`POST /portal/invoices/{id}/resubmit`) and sends it
 *    back into the pipeline — which now RE-EXTRACTS the corrected document
 *    (pinned to the same vendor), so the invoice leaves `rejected` and its
 *    extracted fields may legitimately change.
 * 3. A processing-phase invoice shows a `waiting_on` line ("Awaiting your
 *    customer's review …") beyond the phase chip.
 *
 * Auth + seed shape mirror pagination.spec.ts. Rows are seeded from SQL and
 * removed in a `finally`.
 */

const PORTAL_EMAIL = 'supplier@portal.test';
const PORTAL_PASSWORD = 'demo';
const NUM = `E2E-REJ-${Date.now()}`;
const REASON = 'PO number is missing from the invoice header';

test.use({ storageState: { cookies: [], origins: [] } });

async function portalSignIn(page: Page) {
	await acceptConsent(page);
	await page.goto('/portal/login');
	await page.locator('input[type="email"]').fill(PORTAL_EMAIL);
	await page.locator('input[type="password"]').fill(PORTAL_PASSWORD);
	await page.locator('button[type="submit"]').click();
	// Sign-in lands on the portal HOME (it exists to answer "what needs my
	// attention"); this spec exercises the invoice list, so navigate on
	// explicitly rather than depending on where login happens to land.
	await expect(page).toHaveURL(/\/portal\/?$/, { timeout: 15_000 });
	await page.goto('/portal/invoices');
}

function portalVendor(): { vendorId: string; orgId: string } {
	const vendorId = tenantPsql(
		`SELECT vendor_id FROM vendor_users WHERE email='${PORTAL_EMAIL}'`
	).trim();
	expect(vendorId, `no VendorUser seeded for ${currentTenantSlug()}`).not.toEqual('');
	const orgId = tenantPsql(`SELECT organization_id FROM vendors WHERE id='${vendorId}'`).trim();
	return { vendorId, orgId };
}

// Ids this file seeded. Cleanup is keyed on THEM, not on the invoice number: a
// resubmit now re-extracts the corrected document, so `invoice_number` is no
// longer stable across the test and a `LIKE 'E2E-REJ-%'` scope would strand the
// very row it was meant to remove. The LIKE sweep is kept as a second pass, for
// strays a previously-crashed run left behind.
const seeded: string[] = [];

function cleanup() {
	const predicates = [`invoice_number LIKE 'E2E-REJ-%'`];
	if (seeded.length > 0) {
		predicates.unshift(`id IN (${seeded.map((id) => `'${id}'`).join(',')})`);
	}
	for (const predicate of predicates) {
		// Resubmit creates a workflow instance + step FK-referencing the invoice,
		// so clear those before the invoices themselves — and clear them in ONE
		// transaction. As two psql calls the backend could insert a step for an
		// in-flight resubmit in the gap between them, and the instance delete
		// then failed the FK on a row that had not existed when the step delete
		// ran.
		const scope = `SELECT id FROM invoices WHERE ${predicate}`;
		tenantPsql(
			`BEGIN; ` +
				`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id IN (${scope})); ` +
				`DELETE FROM workflow_instances WHERE invoice_id IN (${scope}); ` +
				`COMMIT;`
		);
		tenantPsql(`DELETE FROM exceptions WHERE invoice_id IN (${scope})`);
		deleteInvoicesWhere(predicate);
	}
	seeded.length = 0;
}

test.afterEach(cleanup);

test('a rejected invoice tells the vendor why', async ({ page }) => {
	const { vendorId, orgId } = portalVendor();
	cleanup();

	try {
		const invId = crypto.randomUUID();
		seeded.push(invId);
		tenantPsql(
			`INSERT INTO invoices
			   (id, correlation_id, invoice_number, vendor_name, amount, currency, status,
			    rejected_by, organization_id, vendor_id, created_at, updated_at)
			 VALUES ('${invId}', gen_random_uuid(), '${NUM}', 'E2E Rej Vendor',
			         100, 'USD', 'rejected', 'Alice Approver', '${orgId}', '${vendorId}',
			         now() + interval '1 hour', now())`
		);
		tenantPsql(
			`INSERT INTO exceptions
			   (id, invoice_id, exception_type, severity, description, status,
			    organization_id, created_at, updated_at)
			 VALUES (gen_random_uuid(), '${invId}', 'review_rejected', 'warning',
			         '${REASON}', 'open', '${orgId}', now(), now())`
		);

		await portalSignIn(page);

		const row = page.locator('tr.clickable', { hasText: NUM });
		await expect(row).toHaveCount(1, { timeout: 10_000 });
		await expect(row.getByText('Why it was rejected')).toBeVisible();
		await expect(row.getByText(REASON)).toBeVisible();
		// The internal approver name is never surfaced to the vendor.
		await expect(page.getByText('Alice Approver')).toHaveCount(0);
	} finally {
		cleanup();
	}
});

test('the vendor can revise & resubmit a rejected invoice', async ({ page }) => {
	const { vendorId, orgId } = portalVendor();
	cleanup();

	try {
		const invId = crypto.randomUUID();
		seeded.push(invId);
		tenantPsql(
			`INSERT INTO invoices
			   (id, correlation_id, invoice_number, vendor_name, amount, currency, status,
			    file_key, organization_id, vendor_id, created_at, updated_at)
			 VALUES ('${invId}', gen_random_uuid(), '${NUM}', 'E2E Rej Vendor',
			         100, 'USD', 'rejected', '${orgId}/x/original.pdf', '${orgId}', '${vendorId}',
			         now() + interval '1 hour', now())`
		);
		tenantPsql(
			`INSERT INTO exceptions
			   (id, invoice_id, exception_type, severity, description, status,
			    organization_id, created_at, updated_at)
			 VALUES (gen_random_uuid(), '${invId}', 'review_rejected', 'warning',
			         '${REASON}', 'open', '${orgId}', now(), now())`
		);

		await portalSignIn(page);

		const row = page.locator('tr.clickable', { hasText: NUM });
		await expect(row).toHaveCount(1, { timeout: 10_000 });

		// "Revise & resubmit" is a real button (keyboard-reachable) that opens
		// the page's one hidden picker — drive it the way a supplier does.
		const resubmit = row.locator('button.resubmit-btn');
		await resubmit.focus();
		const [chooser] = await Promise.all([
			page.waitForEvent('filechooser'),
			page.keyboard.press('Enter'),
		]);
		await chooser.setFiles({
			name: 'corrected.pdf',
			mimeType: 'application/pdf',
			buffer: Buffer.from('%PDF-1.4\ncorrected\n%%EOF'),
		});

		await expect(page.locator('.msg')).toContainText('resubmitted', { timeout: 15_000 });

		// Backend: the SAME invoice row moved off `rejected`, and its rejection
		// exception is resolved.
		await expect
			.poll(() => tenantPsql(`SELECT status FROM invoices WHERE id='${invId}'`).trim(), {
				timeout: 10_000,
			})
			.not.toEqual('rejected');
		expect(
			tenantPsql(
				`SELECT status FROM exceptions WHERE invoice_id='${invId}' AND exception_type='review_rejected'`
			).trim()
		).toEqual('resolved');
	} finally {
		cleanup();
	}
});

test('a processing-phase invoice shows a "waiting on" line', async ({ page }) => {
	const { vendorId, orgId } = portalVendor();
	cleanup();
	const num = `E2E-REJ-WAIT-${Date.now()}`;
	try {
		const invId = crypto.randomUUID();
		seeded.push(invId);
		tenantPsql(
			`INSERT INTO invoices
			   (id, correlation_id, invoice_number, vendor_name, amount, currency, status,
			    organization_id, vendor_id, created_at, updated_at)
			 VALUES ('${invId}', gen_random_uuid(), '${num}', 'E2E Wait Vendor',
			         100, 'USD', 'ready_for_review', '${orgId}', '${vendorId}',
			         now() - interval '4 days', now() - interval '4 days')`
		);
		await portalSignIn(page);
		const row = page.locator('tr.clickable', { hasText: num });
		await expect(row).toHaveCount(1, { timeout: 10_000 });
		await expect(row.getByText("Awaiting your customer's review")).toBeVisible();
		// The age is shown alongside.
		await expect(row.locator('.waiting-on')).toContainText(/\d+ day/);
	} finally {
		cleanup();
	}
});
