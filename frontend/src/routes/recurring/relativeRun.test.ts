import { describe, expect, it } from 'vitest';
import { daysUntil } from './relativeRun.ts';

describe('daysUntil', () => {
	it('counts calendar days, not elapsed hours, late in the evening', () => {
		// 23:30 local on the 19th: the 20th is tomorrow, not "today".
		const now = new Date(2026, 10, 19, 23, 30);
		expect(daysUntil('2026-11-20', now)).toBe(1);
		expect(daysUntil('2026-11-19', now)).toBe(0);
		expect(daysUntil('2026-11-18', now)).toBe(-1);
	});

	it('is the same answer just after midnight', () => {
		const now = new Date(2026, 10, 19, 0, 5);
		expect(daysUntil('2026-11-19', now)).toBe(0);
		expect(daysUntil('2026-11-26', now)).toBe(7);
	});

	it('spans a DST change as whole days', () => {
		const now = new Date(2026, 2, 1, 12, 0);
		expect(daysUntil('2026-04-01', now)).toBe(31);
	});

	it('returns null for a non-date', () => {
		expect(daysUntil('', new Date())).toBeNull();
		expect(daysUntil('soon', new Date())).toBeNull();
	});
});
