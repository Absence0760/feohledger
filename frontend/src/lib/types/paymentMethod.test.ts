import { describe, it, expect } from 'vitest';
import { PAYMENT_METHODS, PAYMENT_METHOD_LABEL_KEYS, paymentMethodLabelKey } from './payment';
import { en } from '#lib/i18n/locales/en.ts';
import { SUPPORTED_LOCALES } from '#lib/i18n/locale.ts';
import { CATALOGUE_LOADERS } from '#lib/i18n/catalogues.ts';

/**
 * Drift guard: the frontend payment-method vocabulary must cover every rail the
 * backend can stamp onto `payments.method`, and every listed method must have a
 * non-empty display label (a missing one renders a blank cell in the payments
 * method dropdowns / History column).
 *
 * Sources (backend):
 *   - `app/schemas/payment.py::PaymentMethod` — ach, wire, check, virtual_card,
 *     and the UK domestic rails bacs / faster_payments / chaps (issue #328).
 *   - `services/payment_corridor.CORRIDOR_OVERRIDE_FEES` selects the same rails.
 *
 * The UK rails were added so a same-currency GBP→GB payment routes onto Faster
 * Payments instead of falling through to `international_wire` (SWIFT + a 2.5 %
 * fee anchor).
 */
const BACKEND_PAYMENT_METHODS = [
	'ach',
	'wire',
	'check',
	'virtual_card',
	'bacs',
	'faster_payments',
	'chaps'
] as const;

describe('PAYMENT_METHODS', () => {
	it('covers every rail the backend can persist', () => {
		for (const method of BACKEND_PAYMENT_METHODS) {
			expect(
				PAYMENT_METHODS as readonly string[],
				`${method} is a backend rail but missing from PAYMENT_METHODS`
			).toContain(method);
		}
	});

	it('offers no method the backend never persists', () => {
		const backend = new Set<string>(BACKEND_PAYMENT_METHODS);
		for (const method of PAYMENT_METHODS) {
			expect(backend.has(method), `${method} is not a backend payment rail`).toBe(true);
		}
	});

	it('gives every method a label KEY that exists in the catalogue', () => {
		// `Record<PaymentMethod, MessageKey>` is the compile-time half; a key
		// naming nothing in the catalogue typechecks fine and renders the raw
		// key string in the History column (`m()` falls back key → raw).
		for (const method of PAYMENT_METHODS) {
			const key = PAYMENT_METHOD_LABEL_KEYS[method];
			expect(key, `${method} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${method} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key].trim().length).toBeGreaterThan(0);
		}
	});

	it('resolves a known rail and returns null otherwise', () => {
		for (const method of PAYMENT_METHODS) {
			expect(paymentMethodLabelKey(method)).toBe(PAYMENT_METHOD_LABEL_KEYS[method]);
		}
		// The cell renders the raw value — visible and searchable — rather than
		// blank, so a rail the backend adds first degrades gracefully.
		expect(paymentMethodLabelKey('sepa')).toBeNull();
	});

	it('labels the UK domestic rails with their industry names', () => {
		expect(en[PAYMENT_METHOD_LABEL_KEYS.bacs]).toBe('BACS');
		expect(en[PAYMENT_METHOD_LABEL_KEYS.faster_payments]).toBe('Faster Payments');
		expect(en[PAYMENT_METHOD_LABEL_KEYS.chaps]).toBe('CHAPS');
	});

	it('keeps the scheme NAMES verbatim in every locale', async () => {
		// ACH / BACS / Faster Payments / CHAPS are the names of clearing
		// schemes, not words — a locale that "translated" one would be naming
		// a rail that does not exist. The three that ARE words (wire, check,
		// virtual card) are deliberately not asserted here.
		const schemes = ['ach', 'bacs', 'faster_payments', 'chaps'] as const;
		for (const loc of SUPPORTED_LOCALES) {
			const dict = (await CATALOGUE_LOADERS[loc]()) as Record<string, string>;
			for (const scheme of schemes) {
				const key = PAYMENT_METHOD_LABEL_KEYS[scheme];
				expect(dict[key], `${loc} renamed the ${scheme} scheme`).toBe(en[key]);
			}
		}
	});
});
