import { describe, expect, it } from 'vitest';

import { confirmationProblem, meetsPasswordRules, passwordRuleStatus } from './passwordRules.ts';

describe('passwordRuleStatus', () => {
	it('lists every rule in display order', () => {
		expect(passwordRuleStatus('').map((r) => r.rule)).toEqual(['length', 'upper', 'lower', 'digit']);
	});

	it('marks each rule on its own', () => {
		const status = Object.fromEntries(passwordRuleStatus('abcdefghijkl').map((r) => [r.rule, r.ok]));
		expect(status).toEqual({ length: true, upper: false, lower: true, digit: false });
	});

	it('counts 12 characters as long enough and 11 as not', () => {
		expect(meetsPasswordRules('Abcdefghij1')).toBe(false);
		expect(meetsPasswordRules('Abcdefghij12')).toBe(true);
	});
});

describe('confirmationProblem', () => {
	it('says empty before it says mismatch', () => {
		expect(confirmationProblem('Abcdefghij12', '')).toBe('empty');
	});

	it('flags a different confirmation', () => {
		expect(confirmationProblem('Abcdefghij12', 'Abcdefghij13')).toBe('mismatch');
	});

	it('accepts an identical one', () => {
		expect(confirmationProblem('Abcdefghij12', 'Abcdefghij12')).toBeNull();
	});
});
