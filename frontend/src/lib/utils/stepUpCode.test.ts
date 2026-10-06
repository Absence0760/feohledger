import { describe, expect, it } from 'vitest';
import { isCompleteStepUpCode } from './stepUpCode.ts';

describe('isCompleteStepUpCode', () => {
	it.each(['123456', '1234567', '12345678'])('accepts %s', (v) => {
		expect(isCompleteStepUpCode(v)).toBe(true);
	});

	it.each(['', '12345', '123456789', '12345a', ' 123456', '123 456'])('rejects %j', (v) => {
		expect(isCompleteStepUpCode(v)).toBe(false);
	});
});
