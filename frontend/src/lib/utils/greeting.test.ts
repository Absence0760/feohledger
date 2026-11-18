import { describe, expect, it } from 'vitest';
import { firstName, greetingKey, localDateKey } from './greeting.ts';

describe('greetingKey', () => {
	it('switches at 05:00, 12:00 and 18:00', () => {
		expect(greetingKey(4)).toBe('dashboard.greeting.evening');
		expect(greetingKey(5)).toBe('dashboard.greeting.morning');
		expect(greetingKey(11)).toBe('dashboard.greeting.morning');
		expect(greetingKey(12)).toBe('dashboard.greeting.afternoon');
		expect(greetingKey(17)).toBe('dashboard.greeting.afternoon');
		expect(greetingKey(18)).toBe('dashboard.greeting.evening');
		expect(greetingKey(23)).toBe('dashboard.greeting.evening');
		expect(greetingKey(0)).toBe('dashboard.greeting.evening');
	});
});

describe('firstName', () => {
	it('takes the first word of the full name', () => {
		expect(firstName('Alice Admin')).toBe('Alice');
		expect(firstName('  Marcus   Manager ')).toBe('Marcus');
	});

	it('is null for a user with no name', () => {
		expect(firstName('')).toBeNull();
		expect(firstName('   ')).toBeNull();
		expect(firstName(null)).toBeNull();
		expect(firstName(undefined)).toBeNull();
	});
});

describe('localDateKey', () => {
	it('uses the local calendar day, zero-padded', () => {
		expect(localDateKey(new Date(2026, 0, 5, 23, 59))).toBe('2026-01-05');
	});
});
