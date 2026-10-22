import { describe, expect, it } from 'vitest';
import {
	nextFromParams,
	postLoginTarget,
	rememberNextForSso,
	safeNextPath,
	SSO_NEXT_KEY,
	takeNextForSso,
	withNext,
	type NextStorage
} from './postLoginRedirect.ts';

function memoryStorage(seed: Record<string, string> = {}): NextStorage & { data: Map<string, string> } {
	const data = new Map(Object.entries(seed));
	return {
		data,
		getItem: (k) => data.get(k) ?? null,
		setItem: (k, v) => void data.set(k, v),
		removeItem: (k) => void data.delete(k)
	};
}

describe('safeNextPath', () => {
	it.each([
		'/invoices/3f6c2a10-1b2c-4d5e-8f90-123456789abc',
		'/invoices?status=pending&sort=-amount',
		'/payments#queue',
		'/profile?section=mfa',
		'/?tab=aging',
		'/vendors/%2F%2Fnot-a-host'
	])('accepts the same-origin path %s', (path) => {
		expect(safeNextPath(path)).toBe(path);
	});

	it.each([
		['protocol-relative', '//evil.example/x'],
		['backslash host', '/\\evil.example'],
		['backslash anywhere', '/invoices\\..\\..\\evil'],
		['absolute http', 'https://evil.example/invoices'],
		['javascript scheme', 'javascript:alert(1)'],
		['scheme without slashes', 'http:evil.example'],
		['relative without slash', 'invoices'],
		['tab-smuggled host', '/\t/evil.example'],
		['newline-smuggled host', '/\n/evil.example'],
		['dot segments collapsing to //', '/.//evil.example'],
		['dot-dot collapsing to //', '/a/..//evil.example'],
		['the login page', '/login'],
		['a login sub-page', '/login/mfa'],
		['login with a query', '/login?next=/invoices'],
		['percent-encoded login', '/%6Cogin'],
		['upper-case login', '/LOGIN/mfa'],
		['the dashboard itself', '/'],
		['empty', ''],
		['a bare slash-slash', '//'],
		['too long', '/' + 'a'.repeat(2048)],
		['malformed percent-escape in a login check', '/%E0%A4%A']
	])('refuses %s', (_label, path) => {
		expect(safeNextPath(path)).toBeNull();
	});

	it.each([null, undefined, 42, {}, ['/invoices']])('refuses a non-string (%s)', (v) => {
		expect(safeNextPath(v)).toBeNull();
	});
});

describe('nextFromParams / postLoginTarget', () => {
	it('reads ?next= through the same gate', () => {
		expect(nextFromParams(new URLSearchParams('next=%2Finvoices%2F42'))).toBe('/invoices/42');
		expect(nextFromParams(new URLSearchParams('next=https%3A%2F%2Fevil.example'))).toBeNull();
		expect(nextFromParams(new URLSearchParams(''))).toBeNull();
	});

	it('falls back to the dashboard', () => {
		expect(postLoginTarget('/invoices/42')).toBe('/invoices/42');
		expect(postLoginTarget('//evil.example')).toBe('/');
		expect(postLoginTarget(null)).toBe('/');
	});
});

describe('withNext', () => {
	it('appends an encoded next', () => {
		expect(withNext('/login', '/invoices?status=pending')).toBe(
			'/login?next=%2Finvoices%3Fstatus%3Dpending'
		);
	});

	it('joins onto an existing query', () => {
		expect(withNext('/login?reason=other_workspace', '/invoices')).toBe(
			'/login?reason=other_workspace&next=%2Finvoices'
		);
	});

	it('adds nothing for an unsafe, absent or default destination', () => {
		expect(withNext('/login', '//evil.example')).toBe('/login');
		expect(withNext('/login', null)).toBe('/login');
		expect(withNext('/login', '/')).toBe('/login');
		expect(withNext('/login', '/login/mfa')).toBe('/login');
	});

	it('round-trips through the reader', () => {
		const href = withNext('/login/mfa', '/invoices/42?tab=activity');
		const params = new URL(href, 'https://acme.example').searchParams;
		expect(nextFromParams(params)).toBe('/invoices/42?tab=activity');
	});
});

describe('the SSO stash', () => {
	it('stores, then takes and clears', () => {
		const s = memoryStorage();
		rememberNextForSso('/invoices/42', s);
		expect(s.data.get(SSO_NEXT_KEY)).toBe('/invoices/42');
		expect(takeNextForSso(s)).toBe('/invoices/42');
		expect(s.data.has(SSO_NEXT_KEY)).toBe(false);
		expect(takeNextForSso(s)).toBeNull();
	});

	it('clears a stale value when the new attempt has no destination', () => {
		const s = memoryStorage({ [SSO_NEXT_KEY]: '/payments' });
		rememberNextForSso(null, s);
		expect(s.data.has(SSO_NEXT_KEY)).toBe(false);
	});

	it('never stores an unsafe destination', () => {
		const s = memoryStorage();
		rememberNextForSso('https://evil.example', s);
		expect(s.data.has(SSO_NEXT_KEY)).toBe(false);
	});

	it('re-validates what it reads back (storage is writable by any script)', () => {
		const s = memoryStorage({ [SSO_NEXT_KEY]: '//evil.example' });
		expect(takeNextForSso(s)).toBeNull();
		expect(s.data.has(SSO_NEXT_KEY)).toBe(false);
	});

	it('survives storage that throws or is absent', () => {
		const throwing: NextStorage = {
			getItem: () => {
				throw new Error('blocked');
			},
			setItem: () => {
				throw new Error('blocked');
			},
			removeItem: () => {
				throw new Error('blocked');
			}
		};
		expect(() => rememberNextForSso('/invoices', throwing)).not.toThrow();
		expect(takeNextForSso(throwing)).toBeNull();
		expect(() => rememberNextForSso('/invoices', null)).not.toThrow();
		expect(takeNextForSso(null)).toBeNull();
	});
});
