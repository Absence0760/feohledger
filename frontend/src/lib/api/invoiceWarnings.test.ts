/**
 * The client half of the invoice-warning contract.
 *
 * `decisions.md` §157 replaced server-English findings with a `{code, params}`
 * pair, and these tests pin the four things that make it hold: the generated
 * catalogue names only keys that exist, a parameter renders in the reader's
 * locale rather than en-US, an unrecognised code degrades to the server's own
 * sentence, and so does a KNOWN code whose params are incomplete — the skew
 * case that would otherwise print `{amount}` at a reviewer.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { invoiceWarningText, localizeInvoiceWarning } from './invoiceWarnings';
import {
	INVOICE_WARNING_MESSAGE_KEYS,
	INVOICE_WARNING_PARAM_KINDS
} from './invoiceWarningMessages.generated';
import { en } from '../i18n/locales/en';
import { interpolate } from '../i18n/interpolate';
import { setActiveFormatLocale } from '../i18n/formatLocale';

const messages: Record<string, string> = en;

/** Stand-in for `m` — the real one needs the Svelte runtime. */
const translate = (key: string, params?: Record<string, string | number>) =>
	interpolate(messages[key], params, 'en');

afterEach(() => setActiveFormatLocale(undefined));

describe('the generated catalogue', () => {
	const catalogue: Record<string, string> = INVOICE_WARNING_MESSAGE_KEYS;
	const kinds: Record<string, Record<string, string>> = INVOICE_WARNING_PARAM_KINDS;

	it('is non-empty and names only keys the English catalogue defines', () => {
		// The `satisfies Record<string, MessageKey>` makes this a compile error
		// too; asserted at runtime so weakening the type is still loud.
		const codes = Object.keys(catalogue);
		expect(codes.length).toBeGreaterThan(0);
		for (const code of codes) {
			expect(messages[catalogue[code]], `no message for ${code}`).toBeTruthy();
		}
	});

	it('declares a kind for every parameter each localized sentence reads', () => {
		// The English VALUE is what `m()` interpolates, so its placeholder set —
		// not the backend template's — is what the params have to cover. A key
		// translated with an extra `{token}` would otherwise render braces.
		for (const [code, key] of Object.entries(catalogue)) {
			const used = (messages[key].match(/\{[a-zA-Z0-9_]+\}/g) ?? []).map((t) => t.slice(1, -1));
			const pluralVars = [...messages[key].matchAll(/\{(\w+),\s*plural,/g)].map((m) => m[1]);
			const declared = new Set(Object.keys(kinds[code] ?? {}));
			for (const name of [...used, ...pluralVars]) {
				expect(declared.has(name), `${code}: ${key} reads undeclared {${name}}`).toBe(true);
			}
		}
	});

	it('covers every code with a kind map', () => {
		expect(Object.keys(kinds).sort()).toEqual(Object.keys(catalogue).sort());
	});
});

describe('localizeInvoiceWarning', () => {
	it('formats money with the warning’s own currency, not a hardcoded symbol', () => {
		const localized = localizeInvoiceWarning({
			message: 'Round amount: 5000.00 ZAR',
			code: 'round_amount',
			params: { amount: '5000.00', currency: 'ZAR' }
		});
		expect(localized).not.toBeNull();
		// The symbol comes from the ISO code the row carries — the pre-catalogue
		// message hardcoded `$` on some of these sentences.
		expect(String(localized!.params.amount)).toContain('5,000.00');
		expect(String(localized!.params.amount)).not.toContain('$');
	});

	it('formats money and percentages for the ACTIVE locale', () => {
		setActiveFormatLocale('de-DE');
		const localized = localizeInvoiceWarning({
			message: 'Amount variance +20.0% vs PO PO-1 (invoice 120.00 EUR vs PO 100.00 EUR)',
			code: 'po_amount_variance',
			params: {
				variancePct: '+20.0',
				poNumber: 'PO-1',
				invoiceAmount: '120.00',
				poTotal: '100.00',
				currency: 'EUR'
			}
		});
		// German decimal comma, and the sign preserved because the backend's own
		// digits carried one.
		expect(String(localized!.params.invoiceAmount)).toContain('120,00');
		// `\u00a0` — German puts a NON-BREAKING space before the `%`, which is
		// exactly the kind of detail a hand-built sentence gets wrong.
		expect(String(localized!.params.variancePct)).toBe('+20,0\u00a0%');
	});

	it('keeps the precision the backend measured, in both directions', () => {
		// `98` has no decimals and no sign; `+20.0` has one of each. Re-deciding
		// that here would either lose a digit or invent one.
		const similarity = localizeInvoiceWarning({
			message: '',
			code: 'duplicate_similar_cross_entity',
			params: { similarity: '98' }
		});
		expect(String(similarity!.params.similarity)).toBe('98%');
	});

	it('passes a count through as a number so ICU plurals can select on it', () => {
		const one = localizeInvoiceWarning({
			message: '',
			code: 'rush_payment',
			params: { days: 1 }
		});
		expect(one!.params.days).toBe(1);
		expect(translate(one!.key, one!.params)).toBe(
			'Rush payment: due within 1 day of the invoice date'
		);
		const many = localizeInvoiceWarning({ message: '', code: 'rush_payment', params: { days: 4 } });
		expect(translate(many!.key, many!.params)).toBe(
			'Rush payment: due within 4 days of the invoice date'
		);
	});

	it('renders the cross-entity tail only when there is one', () => {
		const params = {
			similarity: '97',
			invoiceNumber: 'INV-1',
			vendorName: 'Northwind'
		};
		const none = localizeInvoiceWarning({
			message: '',
			code: 'duplicate_similar',
			params: { ...params, crossEntityCount: 0 }
		});
		expect(translate(none!.key, none!.params)).toBe(
			'Potential duplicate: 97% match to INV-1 from Northwind'
		);
		const two = localizeInvoiceWarning({
			message: '',
			code: 'duplicate_similar',
			params: { ...params, crossEntityCount: 2 }
		});
		expect(translate(two!.key, two!.params)).toBe(
			'Potential duplicate: 97% match to INV-1 from Northwind ' +
				'(plus 2 near-identical invoices under another entity)'
		);
	});

	it('degrades to null — never a blank row — for a code this build predates', () => {
		expect(localizeInvoiceWarning({ message: 'x', code: 'nothing_like_this' })).toBeNull();
		// Every warning persisted before the catalogue shipped looks like this.
		expect(localizeInvoiceWarning({ message: 'x' })).toBeNull();
		expect(localizeInvoiceWarning({ message: 'x', code: null })).toBeNull();
	});

	it('degrades to null when a KNOWN code arrives with a missing param', () => {
		// `interpolate` leaves an unfilled `{placeholder}` intact, so rendering
		// this would show braces. The server's complete sentence is better.
		expect(
			localizeInvoiceWarning({
				message: 'Round amount: 5000.00 USD',
				code: 'round_amount',
				params: { currency: 'USD' }
			})
		).toBeNull();
	});
});

describe('invoiceWarningText', () => {
	it('localizes a known code', () => {
		expect(
			invoiceWarningText(
				{ message: 'PO PO-9 not found', code: 'po_not_found', params: { poNumber: 'PO-9' } },
				translate
			)
		).toBe('PO PO-9 not found');
	});

	it('falls back to the backend’s own sentence for an unknown one', () => {
		expect(
			invoiceWarningText({ message: 'Something the server knows and we do not' }, translate)
		).toBe('Something the server knows and we do not');
	});
});
