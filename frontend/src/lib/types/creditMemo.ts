import type { MoneyAmount } from '$lib/utils/money';

/**
 * One invoice a credit can be applied to — `EligibleInvoiceResponse` in
 * `backend/app/schemas/credit_memo.py`, served by both credit-memo
 * `eligible-invoices` reads (`$lib/api/creditMemos.ts`). Deliberately not
 * `Invoice`: the picker names the invoice and says what it can still absorb,
 * it does not render it.
 *
 * Lives here rather than beside the API helpers so an e2e fixture can
 * `satisfies` it: `$lib/api/*` reaches `$env` through `$lib/tenant`, which the
 * `tests-e2e/` typecheck cannot resolve (frontend `CLAUDE.md` § check:e2e).
 */
export interface EligibleInvoice {
	id: string;
	invoice_number: string;
	vendor_name: string;
	status: string;
	due_date: string | null;
	amount: MoneyAmount;
	currency: string;
	/** `amount` minus every credit already applied — what the over-application
	 *  guard on both application paths compares against. */
	creditable_balance: MoneyAmount;
}
