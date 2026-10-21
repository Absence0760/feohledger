import type { APIRequestContext } from '@playwright/test';

import { API_BASE } from './env';
import { deleteInvoicesWhere, deleteVendorsWhere, tenantPsql } from './helpers';
import { SHARED } from './realisticValues';

/**
 * Production-shaped rows for layout tests (#575 § 2).
 *
 * Seed strings used to be short — "Acme Supplies", `INV-1001`, `demo@acme.com`
 * — and a real tenant's are not. #574 was a queue table that fit every seeded
 * row and overflowed the page the first day a customer used it: an email in
 * the Assignee column, an ERP invoice number, a seven-figure amount. A layout
 * test is only as strict as the strings it is measured with, so every layout
 * spec draws them from here rather than inventing its own. The seed now
 * carries the same values (without the 13-digit amount), from the same file.
 *
 * `REALISTIC` is the value set; `seedRealisticRows` puts one of each into a
 * tenant through the API (vendors, invoices — one tax-exclusive with its lines
 * — contracts, credit memos and the assignee user), falling back to
 * `tenantPsql` only for state the API deliberately does not expose — an
 * invoice's workflow status (no endpoint mints an approved or paid invoice, by
 * design), an unverified vendor, a completed payment, and an exception row
 * (raised by the system, never by a client).
 * `purgeRealisticRows` removes all of it.
 */

/** Marks every row this module creates, so teardown can find exactly them. */
export const REALISTIC_MARKER = 'LYT575';

/**
 * The values themselves come from `realisticValues.ts` — i.e. from
 * `backend/scripts/fixtures/realistic_rows.json`, the ONE place they are
 * spelled, which the seed reads too. This module only adds the marker that
 * makes its own rows removable.
 */
const M = REALISTIC_MARKER;
export const REALISTIC = {
	/** A legal-entity name the way an ERP exports it — ~90 characters, no break. */
	longVendor: `${SHARED.long_vendor_name} ${M}`,
	/** A macron, CJK, accented Latin and a typographic dash in one name. */
	nonAsciiVendor: `${SHARED.non_ascii_vendor_name} ${M}`,
	/** ERP-shaped invoice numbers: long, and with no natural break point. */
	invoiceNumbers: SHARED.invoice_numbers.map((n) => `${M}-${n}`),
	/** Large amounts: the 13-integer-digit ceiling of `Numeric(15, 2)`, and two big ones. */
	amounts: SHARED.amounts,
	contractNumber: `${M}-${SHARED.contract_number}`,
	contractTitle: SHARED.contract_title,
	memoNumber: `${M}-${SHARED.memo_number}`,
	/** A routing alias, not a person: long local part, long subsidiary domain. */
	assigneeEmail: (slug: string) =>
		`${SHARED.assignee_local_part}+${M.toLowerCase()}-${slug}@${SHARED.mailbox_domain}`,
	assigneeName: SHARED.assignee_name,
	/** Net lines summing to `subtotal`, the tax once on the header (#572). */
	taxExclusive: {
		...SHARED.tax_exclusive_invoice,
		invoiceNumber: `${M}-${SHARED.tax_exclusive_invoice.invoice_number}`
	}
} as const;

export interface RealisticRows {
	vendorIds: string[];
	invoiceIds: string[];
	assigneeUserId: string | null;
}

async function login(
	request: APIRequestContext,
	slug: string,
	creds: { email: string; password: string }
): Promise<Record<string, string>> {
	const resp = await request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': slug },
		data: creds
	});
	if (!resp.ok()) throw new Error(`login for realistic-row seeding failed: ${resp.status()}`);
	const body = (await resp.json()) as { access_token?: string };
	if (!body.access_token) throw new Error('login for realistic-row seeding returned no token');
	return { Authorization: `Bearer ${body.access_token}`, 'X-Tenant-Slug': slug };
}

async function postJson<T>(
	request: APIRequestContext,
	path: string,
	headers: Record<string, string>,
	data: unknown
): Promise<T> {
	const resp = await request.post(`${API_BASE}${path}`, { headers, data });
	if (!resp.ok()) {
		throw new Error(`POST ${path} → ${resp.status()}: ${(await resp.text()).slice(0, 300)}`);
	}
	return (await resp.json()) as T;
}

/**
 * Seed one of each realistic value into `slug`'s tenant, signed in as `admin`.
 * Always pair with `purgeRealisticRows(slug, rows)` — it also removes rows a
 * previous, killed run left behind, so call it first as well.
 */
export async function seedRealisticRows(
	request: APIRequestContext,
	slug: string,
	admin: { email: string; password: string }
): Promise<RealisticRows> {
	const headers = await login(request, slug, admin);

	const vendorIds: string[] = [];
	for (const name of [REALISTIC.longVendor, REALISTIC.nonAsciiVendor]) {
		const v = await postJson<{ id: string }>(request, '/api/vendors', headers, {
			name,
			email: `${SHARED.remittance_local_part}+${REALISTIC_MARKER.toLowerCase()}@${SHARED.mailbox_domain}`
		});
		vendorIds.push(v.id);
	}

	const invoiceIds: string[] = [];
	const vendorsByInvoice = [REALISTIC.longVendor, REALISTIC.nonAsciiVendor, REALISTIC.longVendor];
	for (let i = 0; i < REALISTIC.invoiceNumbers.length; i++) {
		const inv = await postJson<{ id: string }>(request, '/api/invoices', headers, {
			vendor: vendorsByInvoice[i],
			invoice_number: REALISTIC.invoiceNumbers[i],
			amount: REALISTIC.amounts[i],
			currency: 'USD',
			invoice_date: '2026-10-01',
			due_date: '2026-10-31'
		});
		invoiceIds.push(inv.id);
	}

	// The first invoice from a new supplier is tax-exclusive and its vendor is
	// unverified (#572, #573). Pushed LAST so the three above keep their
	// positions. The lines go through the real line-items endpoint, which
	// re-derives the warnings — it must report that they reconcile.
	const tax = REALISTIC.taxExclusive;
	const taxInv = await postJson<{ id: string }>(request, '/api/invoices', headers, {
		vendor: REALISTIC.nonAsciiVendor,
		invoice_number: tax.invoiceNumber,
		amount: tax.amount,
		subtotal: tax.subtotal,
		tax_amount: tax.tax_amount,
		tax_rate: tax.tax_rate,
		currency: tax.currency,
		invoice_date: '2026-10-01',
		due_date: '2026-10-31'
	});
	const linesResp = await request.put(`${API_BASE}/api/invoices/${taxInv.id}/line-items`, {
		headers,
		data: tax.lines.map((l, i) => ({ line_number: i + 1, ...l }))
	});
	if (!linesResp.ok()) {
		throw new Error(`PUT line-items → ${linesResp.status()}: ${(await linesResp.text()).slice(0, 300)}`);
	}
	const lines = (await linesResp.json()) as { reconciles_with_header?: boolean };
	if (lines.reconciles_with_header !== true) {
		throw new Error(`tax-exclusive lines did not reconcile: ${JSON.stringify(lines).slice(0, 300)}`);
	}
	invoiceIds.push(taxInv.id);
	// Unverified is what a first invoice from an unknown supplier auto-creates;
	// the API's own vendor create always mints `active`, and letting the invoice
	// auto-create it would fuzzy-match the seed's copy of the same base name.
	tenantPsql(
		`UPDATE vendors SET status = 'unverified', source = 'ai_extracted' WHERE id = '${vendorIds[1]}'`,
		slug
	);

	await postJson(request, '/api/contracts', headers, {
		contract_number: REALISTIC.contractNumber,
		title: REALISTIC.contractTitle,
		contract_type: 'msa',
		vendor_id: vendorIds[0],
		currency: 'USD',
		total_value: REALISTIC.amounts[0],
		spend_limit: REALISTIC.amounts[0],
		end_date: '2027-12-31'
	});
	await postJson(request, '/api/credit-memos', headers, {
		memo_number: REALISTIC.memoNumber,
		vendor_id: vendorIds[1],
		amount: REALISTIC.amounts[2],
		currency: 'USD',
		reason: SHARED.memo_reason
	});

	// The assignee is a real user, so the exception row carries an address the
	// app itself would have put there.
	const user = await postJson<{ id: string }>(request, '/api/admin/users', headers, {
		email: REALISTIC.assigneeEmail(slug),
		full_name: REALISTIC.assigneeName,
		role_names: ['ap_manager']
	});

	// Workflow state the API refuses to mint on purpose (an invoice reaches
	// `approved` / `paid` only through the state machine): one approved invoice
	// for the payment queue, one paid with its completed payment for history.
	const [approvedId, paidId, openId] = invoiceIds;
	tenantPsql(`UPDATE invoices SET status = 'approved' WHERE id = '${approvedId}'`, slug);
	tenantPsql(`UPDATE invoices SET status = 'paid' WHERE id = '${paidId}'`, slug);
	tenantPsql(
		`INSERT INTO payments (id, invoice_id, amount, method, status, reference, completed_at, created_at, updated_at)
		 VALUES (gen_random_uuid(), '${paidId}', ${REALISTIC.amounts[1]}, 'ach', 'completed',
		         '${REALISTIC_MARKER}-${SHARED.payment_reference}', now(), now(), now())`,
		slug
	);
	for (const [invoiceId, type, severity] of [
		[openId, 'po_mismatch', 'error'],
		[approvedId, 'duplicate', 'warning']
	] as const) {
		tenantPsql(
			`INSERT INTO exceptions (id, invoice_id, exception_type, severity, status, organization_id,
			                         description, assigned_to, assigned_to_user_id, created_at, updated_at)
			 SELECT gen_random_uuid(), i.id, '${type}', '${severity}', 'open', i.organization_id,
			        '${REALISTIC_MARKER} PO ${REALISTIC.invoiceNumbers[2]} ${SHARED.exception_description}',
			        '${REALISTIC.assigneeEmail(slug)}', '${user.id}', now(), now()
			 FROM invoices i WHERE i.id = '${invoiceId}'`,
			slug
		);
	}

	return { vendorIds, invoiceIds, assigneeUserId: user.id };
}

/**
 * Remove everything `seedRealisticRows` created in `slug` — by marker, so a
 * killed run's leftovers go too. The vendors' whole graph (invoices, payments,
 * exceptions, contracts, credit memos) is owned by `deleteVendorsWhere`; the
 * assignee user is deleted through the API when the seeding run is known.
 */
export async function purgeRealisticRows(
	request: APIRequestContext,
	slug: string,
	admin: { email: string; password: string },
	rows?: RealisticRows
): Promise<void> {
	deleteVendorsWhere(`name LIKE '%${REALISTIC_MARKER}'`, slug);
	// An invoice whose vendor match missed (vendor_id NULL) is not in that graph.
	deleteInvoicesWhere(`invoice_number LIKE '${REALISTIC_MARKER}%'`, slug);
	const headers = await login(request, slug, admin);
	const userIds = new Set<string>(rows?.assigneeUserId ? [rows.assigneeUserId] : []);
	// A previous run's assignee, found by its address through the API rather
	// than the shared control plane (`controlPsql` is for ids a test created).
	const list = await request.get(`${API_BASE}/api/admin/users`, {
		headers,
		params: { search: REALISTIC_MARKER.toLowerCase() }
	});
	if (list.ok()) {
		const body = (await list.json()) as { items: { email: string; id: string }[] };
		for (const u of body.items) {
			if (u.email === REALISTIC.assigneeEmail(slug)) userIds.add(u.id);
		}
	}
	for (const id of userIds) {
		await request.delete(`${API_BASE}/api/admin/users/${id}`, { headers });
	}
}
