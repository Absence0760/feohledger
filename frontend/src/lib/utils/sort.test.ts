import { describe, it, expect } from 'vitest';
import { clearSort, toggleSort } from './sort';

describe('toggleSort', () => {
	it('starts a newly-clicked column ascending', () => {
		const next = toggleSort({ field: null, order: 'desc' }, 'amount');
		expect(next).toEqual({ field: 'amount', order: 'asc' });
	});

	it('switching to a different column always starts ascending, regardless of the old order', () => {
		const next = toggleSort({ field: 'amount', order: 'desc' }, 'vendor_name');
		expect(next).toEqual({ field: 'vendor_name', order: 'asc' });
	});

	it('clicking the already-active ascending column flips it to descending', () => {
		const next = toggleSort({ field: 'amount', order: 'asc' }, 'amount');
		expect(next).toEqual({ field: 'amount', order: 'desc' });
	});

	it('clicking the already-active descending column flips it to ascending', () => {
		const next = toggleSort({ field: 'amount', order: 'desc' }, 'amount');
		expect(next).toEqual({ field: 'amount', order: 'asc' });
	});
});

describe('clearSort', () => {
	it('drops the field, returning the list to its default order', () => {
		expect(clearSort({ field: 'vendor_name', order: 'desc' })).toEqual({
			field: null,
			order: 'desc',
		});
	});

	it('leaves the next header click starting ascending', () => {
		const cleared = clearSort({ field: 'amount', order: 'desc' });
		expect(toggleSort(cleared, 'amount')).toEqual({ field: 'amount', order: 'asc' });
	});
});
