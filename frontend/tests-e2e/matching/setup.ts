/**
 * Shared setup helpers for the invoice↔PO matching specs.
 *
 * PO matching is computed by `services/po_matching.match_invoice_to_po`, run
 * from `invoice_warnings.refresh_warnings` on every invoice mutation. The
 * result lands on `Invoice.po_match` (JSONB) and is returned verbatim in the
 * `InvoiceResponse`. These specs exercise the real matcher end-to-end:
 *
 *   1. seed a PurchaseOrder (+ optional line items / GR / QualityInspection)
 *      directly in the tenant DB — there is no create-PO / create-GR HTTP API,
 *      so `tenantPsql` is the documented escape hatch for state the API can't
 *      build (inspections DO have a POST route and use it).
 *   2. create an Invoice via the API with the PO number, status `pending`
 *      (NOT `new` — `_refresh_po_match` short-circuits draft `new` invoices).
 *      `POST /api/invoices` does NOT run the matcher, so po_match is null here.
 *   3. PATCH the invoice (any field) — that is the chokepoint that runs
 *      `refresh_warnings` → the matcher — and read `po_match` off the response.
 *
 * Everything is uniquely suffixed per call so parallel/serial specs in the
 * same tenant never collide, and `cleanup()` removes the rows: invoices go
 * through `deleteInvoicesWhere`, which owns their whole child graph; the
 * rest follow FK order (inspections + gr_lines → gr; po_lines → po).
 */

import {
	API_BASE,
	authToken,
	deleteInvoicesWhere,
	tenantHeaders,
	tenantPsql
} from '../fixtures/helpers';
import type { Page } from '@playwright/test';

/** A single SQL statement against the worker's tenant DB, trimmed. */
function sql(query: string): string {
	return tenantPsql(query).trim();
}

/** Resolve `(organization_id, default entity_id)` for the worker's tenant
 *  straight from the tenant DB's `entities` table (no control-plane access). */
export function tenantScope(): { orgId: string; entityId: string } {
	const row = sql("select organization_id || '|' || id from entities where is_default = true limit 1;");
	const [orgId, entityId] = row.split('|');
	if (!orgId || !entityId) throw new Error(`could not resolve tenant scope: ${row}`);
	return { orgId, entityId };
}

let _seq = 0;
/** Monotonic, per-process-unique token so concurrent runs don't collide on
 *  po_number / invoice_number / gr_number / inspection_number. */
export function uniq(prefix: string): string {
	_seq += 1;
	return `${prefix}-${Date.now().toString(36)}-${_seq}-${Math.floor(Math.random() * 1e6)}`;
}

export type PoLine = { description: string; quantity: number; unitPrice: number; total: number };

/** Insert a PurchaseOrder (+ optional line items). Returns its id + number. */
export function createPo(opts: {
	total: number;
	lines?: PoLine[];
	vendorId?: string | null;
	/** The PO's own currency (`purchase_orders.currency`, migration 0099).
	 *  Omitted = NULL, a PO that records none — what every seeded row was
	 *  before the column existed (decisions §197). */
	currency?: string | null;
}): { poId: string; poNumber: string } {
	const { orgId, entityId } = tenantScope();
	const poNumber = uniq('PO-M');
	const vendorCol = opts.vendorId ? `'${opts.vendorId}'` : 'NULL';
	const currencyCol = opts.currency ? `'${opts.currency}'` : 'NULL';
	sql(
		`insert into purchase_orders (id, po_number, vendor_id, total, currency, status, organization_id, entity_id, created_at, updated_at)
		 values (gen_random_uuid(), '${poNumber}', ${vendorCol}, ${opts.total}, ${currencyCol}, 'open', '${orgId}', '${entityId}', now(), now());`
	);
	const poId = sql(`select id from purchase_orders where po_number = '${poNumber}';`);
	for (const l of opts.lines ?? []) {
		sql(
			`insert into po_line_items (id, po_id, description, quantity, unit_price, total, created_at, updated_at)
			 values (gen_random_uuid(), '${poId}', '${l.description}', ${l.quantity}, ${l.unitPrice}, ${l.total}, now(), now());`
		);
	}
	return { poId, poNumber };
}

/** Insert a GoodsReceipt against a PO (+ optional received-quantity lines). */
export function createGr(opts: {
	poId: string;
	lines?: { description: string; quantityReceived: number }[];
}): { grId: string; grNumber: string } {
	const { orgId, entityId } = tenantScope();
	const grNumber = uniq('GR-M');
	sql(
		`insert into goods_receipts (id, gr_number, po_id, received_date, status, organization_id, entity_id, created_at, updated_at)
		 values (gen_random_uuid(), '${grNumber}', '${opts.poId}', now(), 'received', '${orgId}', '${entityId}', now(), now());`
	);
	const grId = sql(`select id from goods_receipts where gr_number = '${grNumber}';`);
	for (const l of opts.lines ?? []) {
		sql(
			`insert into gr_line_items (id, gr_id, description, quantity_received, created_at, updated_at)
			 values (gen_random_uuid(), '${grId}', '${l.description}', ${l.quantityReceived}, now(), now());`
		);
	}
	return { grId, grNumber };
}

/**
 * Seed a `QualityInspection` straight into the tenant DB.
 *
 * `POST /api/inspections` is the normal path and the specs use it — this exists
 * for the cases that need a row to EXIST for a caller who cannot create one
 * (the clerk / reader specs), where going through the API would test the RBAC
 * gate instead of the thing under test.
 */
export function createInspectionRow(opts: {
	grId: string;
	number: string;
	result?: 'pass' | 'fail' | 'partial';
}): void {
	const { orgId, entityId } = tenantScope();
	sql(
		`insert into quality_inspections (id, inspection_number, gr_id, result, status, organization_id, entity_id, created_at, updated_at)
		 values (gen_random_uuid(), '${opts.number}', '${opts.grId}', '${opts.result ?? 'pass'}', 'completed', '${orgId}', '${entityId}', now(), now());`
	);
}

/** Create an invoice via the API, then PATCH it once so the matcher runs and
 *  `po_match` is materialized. Returns the id and the computed po_match. */
export async function createMatchedInvoice(
	page: Page,
	opts: {
		poNumber?: string;
		amount: number;
		vendorId?: string;
		glAccount?: string;
		currency?: string;
	}
): Promise<{ invoiceId: string; poMatch: PoMatch | null }> {
	const headers = { ...tenantHeaders(await authToken(page)), 'Content-Type': 'application/json' };
	const createBody: Record<string, unknown> = {
		vendor: 'Matching Test Vendor',
		invoice_number: uniq('INV-M'),
		amount: opts.amount,
		status: 'pending',
		po_number: opts.poNumber
	};
	if (opts.glAccount) createBody.gl_account = opts.glAccount;
	if (opts.currency) createBody.currency = opts.currency;
	const created = await page.request.post(`${API_BASE}/api/invoices`, { headers, data: createBody });
	if (!created.ok()) throw new Error(`create invoice failed: ${created.status()} ${await created.text()}`);
	const invoiceId = ((await created.json()) as { id: string }).id;

	// POST /api/invoices intentionally ignores a client-supplied status (the
	// status-injection fix — InvoiceCreate has no `status` field), so the row is
	// always created as the draft `new`, and `_refresh_po_match` short-circuits
	// `new` invoices. Move it to `pending` so the matcher actually runs. Setting
	// vendor_id isn't exposed on the API either (the per-vendor rule + vendor-
	// scoped PO lookup need it). Both are the legitimate tenantPsql use case —
	// state the API can't build, same as the PO/GR seeds above.
	//
	// vendor_id is now pinned EXPLICITLY (to the caller's vendor, or NULL). The
	// create endpoint auto-links a vendor from the typed name
	// (`match_and_link_vendor`, source="manual"), so a manual invoice no longer
	// lands vendor-less: it would carry an auto-minted vendor whose id the
	// vendor-scoped matcher (`po_matching` filters `PurchaseOrder.vendor_id ==
	// invoice.vendor_id`) then uses to reject a PO seeded without a vendor —
	// turning every match into `no_po`. These specs isolate the amount / qty /
	// inspection matching, so NULL matches a vendor-agnostic PO; the vendor
	// scoping itself is covered by rules-and-isolation (explicit vendorId) and
	// backend test_po_matching_algorithm.py.
	const vendorSet = opts.vendorId ? `vendor_id = '${opts.vendorId}'` : 'vendor_id = NULL';
	sql(`update invoices set status = 'pending', ${vendorSet} where id = '${invoiceId}';`);

	const poMatch = await recompute(page, invoiceId);
	return { invoiceId, poMatch };
}

/** PATCH the invoice with a no-op note edit to re-run the matcher, and return
 *  the freshly-computed po_match. The note value is unique so the PATCH always
 *  produces a real field diff (the audit row), guaranteeing refresh runs. */
export async function recompute(page: Page, invoiceId: string): Promise<PoMatch | null> {
	const headers = { ...tenantHeaders(await authToken(page)), 'Content-Type': 'application/json' };
	const patched = await page.request.patch(`${API_BASE}/api/invoices/${invoiceId}`, {
		headers,
		data: { notes: uniq('touch') }
	});
	if (!patched.ok()) throw new Error(`patch invoice failed: ${patched.status()} ${await patched.text()}`);
	return ((await patched.json()) as { po_match: PoMatch | null }).po_match;
}

/** The match record as returned by InvoiceResponse.po_match (asdict of the
 *  service's MatchResult dataclass). */
export type PoMatch = {
	match_type: string;
	status: string;
	po_id: string | null;
	po_number: string | null;
	po_total: number | null;
	/** The PO's own code; `null` when it records none. */
	po_currency: string | null;
	/** `same` / `different` / `unknown` — the currency leg (decisions §197). */
	currency_check: 'same' | 'different' | 'unknown' | null;
	gr_id: string | null;
	/** `null` on a currency mismatch — there is no variance between two units. */
	amount_variance: number | null;
	amount_variance_pct: number | null;
	within_tolerance: boolean;
	inspection_id: string | null;
	inspection_result: string | null;
	inspection_accepted_quantity: number | null;
	inspection_required: boolean;
	issues: string[];
	details: Record<string, unknown>;
};

/** Fetch the open exception types for an invoice straight from the DB. */
export function exceptionsFor(invoiceId: string): string[] {
	const out = sql(
		`select string_agg(exception_type || ':' || severity, ',') from exceptions
		 where invoice_id = '${invoiceId}' and status in ('open', 'escalated');`
	);
	return out ? out.split(',') : [];
}

/** Remove every row a test created (FK-safe order). Accepts the ids it knows. */
export function cleanup(ids: {
	invoiceIds?: string[];
	grIds?: string[];
	poIds?: string[];
}): void {
	for (const id of ids.invoiceIds ?? []) {
		// `deleteInvoicesWhere` owns the whole child graph (16 non-cascading FKs
		// reference `invoices`); hand-listing a subset here only worked while a
		// matching spec's invoice happened not to acquire the rest.
		deleteInvoicesWhere(`id = '${id}'`);
	}
	for (const id of ids.grIds ?? []) {
		sql(`delete from quality_inspections where gr_id = '${id}';`);
		sql(`delete from gr_line_items where gr_id = '${id}';`);
		sql(`delete from goods_receipts where id = '${id}';`);
	}
	for (const id of ids.poIds ?? []) {
		sql(`delete from quality_inspections where po_id = '${id}';`);
		sql(`delete from po_line_items where po_id = '${id}';`);
		sql(`delete from purchase_orders where id = '${id}';`);
	}
}
