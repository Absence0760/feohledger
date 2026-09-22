import { describe, expect, it } from 'vitest';
import { vendorOptionLabel, type VendorPickerOption } from './vendorPicker';

const opt = (id: string, name: string, code?: string | null): VendorPickerOption => ({
	id,
	name,
	code
});

describe('vendorOptionLabel', () => {
	it('appends the vendor code when there is one', () => {
		expect(vendorOptionLabel(opt('1', 'Acme Corp', 'V-001'))).toBe('Acme Corp (V-001)');
	});

	it('omits an absent, null or blank code rather than rendering empty parens', () => {
		expect(vendorOptionLabel(opt('1', 'Acme Corp'))).toBe('Acme Corp');
		expect(vendorOptionLabel(opt('1', 'Acme Corp', null))).toBe('Acme Corp');
		expect(vendorOptionLabel(opt('1', 'Acme Corp', '   '))).toBe('Acme Corp');
	});
});
