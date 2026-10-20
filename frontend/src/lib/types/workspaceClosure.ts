/**
 * Closing a workspace (Organization → Close workspace, `docs/decisions.md` §294).
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
	card_transactions: 'org.close.blocker.cardTransactions',
	bank_statements: 'org.close.blocker.bankStatements',
	tax_filings: 'org.close.blocker.taxFilings',
	tax_records: 'org.close.blocker.taxRecords',
	positive_pay_files: 'org.close.blocker.positivePayFiles',
	einvoice_transmissions: 'org.close.blocker.einvoiceTransmissions',
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

/** `GET /api/organization/close`. */
export interface ClosurePreflight {
	closable: boolean;
	blockers: string[];
	/** What the typed confirmation must match. */
	slug: string;
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
