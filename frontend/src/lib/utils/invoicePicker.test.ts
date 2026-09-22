import { describe, expect, it } from 'vitest';
import {
	creditAmountParam,
	invoiceOptionLabel,
	remainingToCredit,
	type InvoicePickerOption
} from './invoicePicker';

const inv = (over: Partial<InvoicePickerOption> = {}): InvoicePickerOption => ({
	id: 'i1',
	invoice_number: 'INV-001',
	amount: 500,
	currency: 'USD',
	...over
});

describe('invoiceOptionLabel', () => {
	it('is the invoice number — what an operator searches by and reads back', () => {
		expect(invoiceOptionLabel(inv())).toBe('INV-001');
	});
});

describe('remainingToCredit', () => {
	it('stays quiet for an invoice no credit has touched', () => {
		// Its balance IS its amount; printing both says the same figure twice.
		expect(remainingToCredit(inv({ creditable_balance: 500 }))).toBeNull();
		expect(remainingToCredit(inv({ amount: '500.00', creditable_balance: 500 }))).toBeNull();
	});

	it('calls out what is left once earlier credits have eaten into it', () => {
		expect(remainingToCredit(inv({ creditable_balance: 50 }))).toBe(50);
		// The server's own figure is what gets shown, never a recomputed one.
		expect(remainingToCredit(inv({ creditable_balance: '0.01' }))).toBe('0.01');
	});

	it('shows nothing for a source that carries no balance at all', () => {
		expect(remainingToCredit(inv())).toBeNull();
		expect(remainingToCredit(inv({ creditable_balance: null }))).toBeNull();
	});
});

describe('creditAmountParam', () => {
	it('passes an amount the backend accepts, as the text the form holds', () => {
		expect(creditAmountParam(250.5)).toBe('250.5');
		expect(creditAmountParam('100.00')).toBe('100.00');
		expect(creditAmountParam(' 7 ')).toBe('7');
	});

	it('withholds anything the create would refuse, so the list never 422s', () => {
		// A 422 there would read as "invoices couldn't be loaded" while the
		// operator is still typing; the create's own validation is the place
		// to say the amount is wrong.
		for (const bad of ['', 0, '0', '0.00', -5, '1.005', 'abc', '1e3', null, undefined]) {
			expect(creditAmountParam(bad as number | string | null | undefined), String(bad)).toBeNull();
		}
	});
});
