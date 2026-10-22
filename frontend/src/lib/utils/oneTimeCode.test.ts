import { describe, expect, it } from 'vitest';
import { normalizeOneTimeCode } from './oneTimeCode.ts';

describe('normalizeOneTimeCode', () => {
	it.each([
		['123456', '123456'],
		['123 456', '123456'],
		['123-456', '123456'],
		[' 123456\n', '123456'],
		['Your code: 482 193', '482193'],
		['１２３４５６', '123456'], // full-width digits from a Japanese IME
		['12345678901', '12345678'],
		['', '']
	])('%j → %j', (raw, expected) => {
		expect(normalizeOneTimeCode(raw)).toBe(expected);
	});
});
