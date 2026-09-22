/**
 * The invoice-specific half of `ui/InvoicePicker.svelte`. Everything generic —
 * paging, the count line, keyboard navigation, what Enter commits — lives in
 * `./searchPicker.ts`, shared with `ui/VendorPicker` (`docs/decisions.md`
 * §202).
 */
import type { MoneyAmount } from './money';

/** Invoice picker option — the `id` is what the consuming form submits. */
export interface InvoicePickerOption {
	id: string;
	invoice_number: string;
	amount: MoneyAmount;
	currency: string | null;
	/**
	 * What the invoice can still absorb, when the source is a credit's
	 * eligible set (`api/creditMemos.ts`). Absent for any other source.
	 */
	creditable_balance?: MoneyAmount;
}

/** The text shown for an option, and the input's text once it is chosen. */
export function invoiceOptionLabel(option: InvoicePickerOption): string {
	return option.invoice_number;
}

/**
 * The balance to call out under an option, or `null` to show none.
 *
 * Only when earlier credits have already eaten into the invoice: then the
 * figure a credit can still take differs from the invoice's own amount, and
 * that difference is exactly what decides whether this credit fits. An
 * untouched invoice's balance IS its amount, and saying so twice is noise.
 * Compared as numbers only to decide WHETHER to show it; what is shown is the
 * server's own exact figure.
 */
export function remainingToCredit(option: InvoicePickerOption): MoneyAmount | null {
	const balance = option.creditable_balance;
	if (balance === null || balance === undefined || balance === '') return null;
	return Number(balance) !== Number(option.amount) ? balance : null;
}

/**
 * The memo amount as the eligible-set filter (`?amount=`), or `null` while the
 * form does not hold one the backend would accept.
 *
 * The create form's amount field is a number input, so it holds `''`, a
 * number, or (on an edit) a decimal string. The backend validates the filter
 * exactly as it validates a memo's amount — positive, at most two decimal
 * places — and a 422 there would read as "invoices couldn't be loaded". So a
 * value create would refuse anyway is not sent at all: the picker lists every
 * invoice with any balance left, and the create's own validation speaks.
 */
export function creditAmountParam(value: number | string | null | undefined): string | null {
	if (value === null || value === undefined) return null;
	const text = String(value).trim();
	if (!/^\d{1,13}(\.\d{1,2})?$/.test(text)) return null;
	return Number(text) > 0 ? text : null;
}
