import { test, expect } from 'vitest';
import { interpolate } from './interpolate';

test('returns the template unchanged when there are no params', () => {
	expect(interpolate('Hello world')).toBe('Hello world');
	expect(interpolate('No {placeholder} filled')).toBe('No {placeholder} filled');
});

test('substitutes a single placeholder', () => {
	expect(interpolate('Sections: {group}', { group: 'Billing' })).toBe('Sections: Billing');
});

test('substitutes multiple distinct placeholders', () => {
	expect(interpolate('{a} of {b}', { a: 3, b: 10 })).toBe('3 of 10');
});

test('replaces every occurrence of a repeated placeholder', () => {
	expect(interpolate('{x}+{x}', { x: 2 })).toBe('2+2');
});

test('coerces numbers to strings', () => {
	expect(interpolate('{n} EUR', { n: 5 })).toBe('5 EUR');
});

test('leaves an unreferenced placeholder intact rather than blanking it', () => {
	expect(interpolate('{known} {unknown}', { known: 'a' })).toBe('a {unknown}');
});

test('substitutes regex-special values verbatim (literal, not regex, replace)', () => {
	expect(interpolate('cost: {price}', { price: '$1.99' })).toBe('cost: $1.99');
});

const INVOICES = '{n, plural, one {# invoice} other {# invoices}}';

test('plural: selects the one branch for count 1 and other for N (en)', () => {
	expect(interpolate(INVOICES, { n: 1 }, 'en')).toBe('1 invoice');
	expect(interpolate(INVOICES, { n: 3 }, 'en')).toBe('3 invoices');
	expect(interpolate(INVOICES, { n: 0 }, 'en')).toBe('0 invoices');
});

test('plural: an exact =N branch wins over the category branch', () => {
	const tpl = '{n, plural, =0 {No invoices} one {# invoice} other {# invoices}}';
	expect(interpolate(tpl, { n: 0 }, 'en')).toBe('No invoices');
	expect(interpolate(tpl, { n: 1 }, 'en')).toBe('1 invoice');
});

test('plural: German uses one/other categories like English here', () => {
	const tpl = '{n, plural, one {# Rechnung} other {# Rechnungen}}';
	expect(interpolate(tpl, { n: 1 }, 'de')).toBe('1 Rechnung');
	expect(interpolate(tpl, { n: 5 }, 'de')).toBe('5 Rechnungen');
});

test('plural: a branch message may itself contain a placeholder', () => {
	const tpl = '{n, plural, one {# item for {who}} other {# items for {who}}}';
	expect(interpolate(tpl, { n: 1, who: 'Acme' }, 'en')).toBe('1 item for Acme');
	expect(interpolate(tpl, { n: 4, who: 'Acme' }, 'en')).toBe('4 items for Acme');
});

test('plural: missing selected category falls back to other', () => {
	const tpl = '{n, plural, other {# things}}';
	expect(interpolate(tpl, { n: 1 }, 'en')).toBe('1 things');
});

test('plural: # is the count grouped for the message locale, not raw digits', () => {
	expect(interpolate(INVOICES, { n: 12345 }, 'en')).toBe('12,345 invoices');
	const de = '{n, plural, one {# Rechnung} other {# Rechnungen}}';
	expect(interpolate(de, { n: 12345 }, 'de')).toBe('12.345 Rechnungen');
	// A string count (some params arrive as strings) is grouped the same way.
	expect(interpolate(INVOICES, { n: '2500' }, 'en')).toBe('2,500 invoices');
});

test('plural: a fractional plural value keeps its digits rather than rounding', () => {
	expect(interpolate('{n, plural, other {# days}}', { n: 1.5 }, 'en')).toBe('1.5 days');
});
