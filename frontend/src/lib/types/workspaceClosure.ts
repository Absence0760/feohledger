/**
 * Closing a workspace (Organization → Close workspace, `docs/decisions.md` §306).
 *
 * The backend refuses a self-service closure for any workspace that holds
 * something that must be kept, and names each reason by a stable code
 * (`backend/app/services/workspace_closure.py::BLOCKERS`). This table is the
 * sentence for each; `backend/tests/test_workspace_closure.py` reads this file
 * and fails when a backend code has no entry here, so a new guard can never
 * render as a bare code.
 *
 * Pure — no `$app/*`, no `api.ts` — so vitest can import it. Its keys are in
 * the settings slice (decisions §300), so nothing reachable from a route that
 * does not load that slice may import this module: `api/codedRefusals.ts`
 * reads the refusal through `types/workspaceClosureRefusal.ts` instead.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';

export const WORKSPACE_CLOSE_BLOCKER_KEYS = {
	second_factor_unavailable: 'org.close.blocker.secondFactorUnavailable',
	audit_archive_unavailable: 'org.close.blocker.auditArchiveUnavailable',
	audit_queue_unverifiable: 'org.close.blocker.auditQueueUnverifiable',
	other_users: 'org.close.blocker.otherUsers',
	paid_subscription: 'org.close.blocker.paidSubscription',
	partner_children: 'org.close.blocker.partnerChildren',
	partner_managed: 'org.close.blocker.partnerManaged',
	custom_domain: 'org.close.blocker.customDomain',
	approved_invoices: 'org.close.blocker.approvedInvoices',
	payments: 'org.close.blocker.payments',
	payment_runs: 'org.close.blocker.paymentRuns',
	virtual_cards: 'org.close.blocker.virtualCards',
	applied_credit_memos: 'org.close.blocker.appliedCreditMemos',
	approved_expense_reports: 'org.close.blocker.approvedExpenseReports',
	approved_preapprovals: 'org.close.blocker.approvedPreapprovals',
	card_transactions: 'org.close.blocker.cardTransactions',
	bank_statements: 'org.close.blocker.bankStatements',
	statement_reconciliations: 'org.close.blocker.statementReconciliations',
	tax_filings: 'org.close.blocker.taxFilings',
	tax_records: 'org.close.blocker.taxRecords',
	positive_pay_files: 'org.close.blocker.positivePayFiles',
	einvoice_transmissions: 'org.close.blocker.einvoiceTransmissions',
	progressed_purchase_orders: 'org.close.blocker.progressedPurchaseOrders',
	approved_requisitions: 'org.close.blocker.approvedRequisitions',
	approved_intake_requests: 'org.close.blocker.approvedIntakeRequests',
	goods_receipts: 'org.close.blocker.goodsReceipts',
	inventory_pushes: 'org.close.blocker.inventoryPushes',
	active_contracts: 'org.close.blocker.activeContracts',
	accepted_discounts: 'org.close.blocker.acceptedDiscounts',
	approved_supplier_changes: 'org.close.blocker.approvedSupplierChanges',
	privacy_requests: 'org.close.blocker.privacyRequests',
	supplier_portal_users: 'org.close.blocker.supplierPortalUsers',
	records_unverifiable: 'org.close.blocker.recordsUnverifiable'
} as const satisfies Record<string, MessageKey>;

export type WorkspaceCloseBlocker = keyof typeof WORKSPACE_CLOSE_BLOCKER_KEYS;

/**
 * What a closure deletes that no guard stopped, by the preflight's
 * `will_delete` codes (`backend/app/services/workspace_closure.py::
 * INVENTORY_COUNTS` + `inactive_users`). Each is an ICU plural on `{n}`.
 */
export const WORKSPACE_CLOSE_INVENTORY_KEYS = {
	invoices: 'org.close.inventory.invoices',
	vendors: 'org.close.inventory.vendors',
	purchase_orders: 'org.close.inventory.purchaseOrders',
	credit_memos: 'org.close.inventory.creditMemos',
	expense_reports: 'org.close.inventory.expenseReports',
	contracts: 'org.close.inventory.contracts',
	exceptions: 'org.close.inventory.exceptions',
	inactive_users: 'org.close.inventory.inactiveUsers'
} as const satisfies Record<string, MessageKey>;

/** The inventory as `[labelKey, count]` pairs in a stable order, skipping a
 *  zero or a code this build has no label for. */
export function inventoryLines(willDelete: Record<string, number>): [MessageKey, number][] {
	const lines: [MessageKey, number][] = [];
	for (const [code, key] of Object.entries(WORKSPACE_CLOSE_INVENTORY_KEYS)) {
		const n = willDelete[code];
		if (typeof n === 'number' && n > 0) lines.push([key, n]);
	}
	return lines;
}

/** `GET /api/organization/close`. */
export interface ClosurePreflight {
	closable: boolean;
	blockers: string[];
	/** What the typed confirmation must match. */
	slug: string;
	/** Non-zero counts of what a closure would delete, by inventory code. */
	will_delete: Record<string, number>;
}

/** The sentence key for a blocker code, or `null` for one this build predates. */
export function blockerLabelKey(code: string): MessageKey | null {
	return Object.hasOwn(WORKSPACE_CLOSE_BLOCKER_KEYS, code)
		? WORKSPACE_CLOSE_BLOCKER_KEYS[code as WorkspaceCloseBlocker]
		: null;
}

export { refusalBlockers } from '#lib/types/workspaceClosureRefusal.ts';

/** The same comparison the server makes: exact, surrounding spaces ignored. */
export function confirmationMatches(typed: string, slug: string): boolean {
	return slug.length > 0 && typed.trim() === slug;
}
