import { describe, expect, it } from 'vitest';

import { emailFromQuery, signInHref } from './prefillEmail.ts';

const q = (s: string) => new URLSearchParams(s);

describe('emailFromQuery', () => {
	it('returns a decoded, trimmed address', () => {
		expect(emailFromQuery(q('email=admin%40acme.example'))).toBe('admin@acme.example');
		expect(emailFromQuery(q('email=%20a%2Bb%40acme.example%20'))).toBe('a+b@acme.example');
	});

	it('is empty when absent or blank', () => {
		expect(emailFromQuery(q(''))).toBe('');
		expect(emailFromQuery(q('email='))).toBe('');
		expect(emailFromQuery(q('email=%20%20'))).toBe('');
	});

	it('drops anything that is not one address', () => {
		expect(emailFromQuery(q('email=not-an-email'))).toBe('');
		expect(emailFromQuery(q('email=a%40b'))).toBe('');
		expect(emailFromQuery(q('email=your account is locked%40evil.example'))).toBe('');
		expect(emailFromQuery(q('email=a%40b.example c%40d.example'))).toBe('');
		expect(emailFromQuery(q(`email=${'a'.repeat(320)}%40b.example`))).toBe('');
	});
});

describe('signInHref', () => {
	it('carries the address encoded, or is bare /login', () => {
		expect(signInHref('a+b@acme.example')).toBe('/login?email=a%2Bb%40acme.example');
		expect(signInHref('')).toBe('/login');
		expect(signInHref(null)).toBe('/login');
	});

	it('round-trips through emailFromQuery', () => {
		const href = signInHref('first.last+ap@acme.example');
		expect(emailFromQuery(new URL(href, 'http://x').searchParams)).toBe('first.last+ap@acme.example');
	});
});
