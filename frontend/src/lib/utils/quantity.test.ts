import { describe, expect, it } from 'vitest';
import { decimalSeparator, formatQuantityInput, isPositiveQuantity, parseQuantity } from './quantity';

describe('parseQuantity', () => {
	it("accepts the reader's own decimal separator and normalises to a dot", () => {
		expect(parseQuantity('9,5', 'de')).toEqual({ kind: 'valid', value: '9.5' });
		expect(parseQuantity('9.5', 'en')).toEqual({ kind: 'valid', value: '9.5' });
		expect(parseQuantity(' 12 ', 'fr')).toEqual({ kind: 'valid', value: '12' });
	});

	it("refuses the other locale's separator rather than guess", () => {
		// `1.000` is a thousand to a German reader; `1,000` to an English one.
		expect(parseQuantity('1.000', 'de').kind).toBe('invalid');
		expect(parseQuantity('1,000', 'en').kind).toBe('invalid');
	});

	it('fits Numeric(12, 4) and refuses negatives', () => {
		expect(parseQuantity('12345678.1234', 'en').kind).toBe('valid');
		expect(parseQuantity('123456789', 'en').kind).toBe('invalid');
		expect(parseQuantity('1.12345', 'en').kind).toBe('invalid');
		expect(parseQuantity('-1', 'en').kind).toBe('invalid');
	});

	it('reads blank as empty, not invalid', () => {
		expect(parseQuantity('   ', 'en')).toEqual({ kind: 'empty' });
	});
});

describe('isPositiveQuantity', () => {
	it('is false for zero in any spelling', () => {
		expect(isPositiveQuantity(parseQuantity('0', 'en'))).toBe(false);
		expect(isPositiveQuantity(parseQuantity('0.0000', 'en'))).toBe(false);
		expect(isPositiveQuantity(parseQuantity('0,5', 'de'))).toBe(true);
	});
});

describe('formatQuantityInput', () => {
	it("writes the reader's separator, exact at the column scale", () => {
		expect(formatQuantityInput(0.7, 'de')).toBe('0,7');
		expect(formatQuantityInput(10, 'en')).toBe('10');
		expect(decimalSeparator('ja')).toBe('.');
	});
});
