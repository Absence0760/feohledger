import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
	WORKSPACE_ADDRESS_MAX,
	WORKSPACE_ADDRESS_MIN,
	cleanWorkspaceAddressTyping,
	normalizeWorkspaceInput,
	ownDomainHost,
	tenantHostSuffix,
	workspaceAddressProblem,
	workspaceLoginUrl
} from './workspaceAddress';

const PROD = 'https://{slug}.feohledger.com';
const DEV = 'http://{slug}.localhost:7777';

describe('tenantHostSuffix', () => {
	it('is the host after the slug', () => {
		expect(tenantHostSuffix(PROD)).toBe('.feohledger.com');
		expect(tenantHostSuffix(DEV)).toBe('.localhost:7777');
		expect(tenantHostSuffix('https://{slug}.feohledger.com/')).toBe('.feohledger.com');
	});

	it('refuses a template it cannot build a host from', () => {
		for (const bad of [
			'',
			null,
			undefined,
			'{slug}.feohledger.com', // no scheme
			'https://feohledger.com', // no slug
			'https://{slug}.{slug}.com', // two
			'https://app.feohledger.com/{slug}', // slug in the path
			'https://{slug}.feohledger.com/app', // a path after the host
			'https://{slug}' // nothing after it
		]) {
			expect(tenantHostSuffix(bad), String(bad)).toBeNull();
		}
	});
});

describe('normalizeWorkspaceInput', () => {
	it('takes a pasted address down to its slug', () => {
		const suffix = '.feohledger.com';
		expect(normalizeWorkspaceInput('acme', suffix)).toBe('acme');
		expect(normalizeWorkspaceInput('  Acme ', suffix)).toBe('acme');
		expect(normalizeWorkspaceInput('acme.feohledger.com', suffix)).toBe('acme');
		expect(normalizeWorkspaceInput('https://acme.feohledger.com/invoices?x=1', suffix)).toBe('acme');
		expect(normalizeWorkspaceInput('ACME.FEOHLEDGER.COM/', suffix)).toBe('acme');
	});

	it('never guesses a slug out of a host that is not the platform', () => {
		// A customer's own domain: its first label is not a workspace slug.
		expect(normalizeWorkspaceInput('ap.acmecorp.com', '.feohledger.com')).toBe('ap.acmecorp.com');
		// The bare suffix is not a workspace either.
		expect(normalizeWorkspaceInput('.feohledger.com', '.feohledger.com')).toBe('.feohledger.com');
		expect(normalizeWorkspaceInput('acme.feohledger.com', null)).toBe('acme.feohledger.com');
	});
});

describe('workspaceAddressProblem', () => {
	it('accepts what the backend accepts', () => {
		for (const ok of ['acme', 'abc', 'acme-corp', 'a1b2', 'a'.repeat(30)]) {
			expect(workspaceAddressProblem(ok), ok).toBeNull();
		}
	});

	it('names the first rule a bad address breaks', () => {
		expect(workspaceAddressProblem('')).toBe('empty');
		expect(workspaceAddressProblem('1acme')).toBe('startLetter');
		expect(workspaceAddressProblem('-acme')).toBe('startLetter');
		expect(workspaceAddressProblem('ac_me')).toBe('characters');
		expect(workspaceAddressProblem('ap.acmecorp.com')).toBe('characters');
		expect(workspaceAddressProblem('ab')).toBe('tooShort');
		expect(workspaceAddressProblem('a'.repeat(31))).toBe('tooLong');
		expect(workspaceAddressProblem('acme-')).toBe('endHyphen');
		expect(workspaceAddressProblem('ac--me')).toBe('doubleHyphen');
	});
});

describe('workspaceLoginUrl', () => {
	it("is the workspace's own sign-in page", () => {
		expect(workspaceLoginUrl('acme', PROD)).toBe('https://acme.feohledger.com/login');
		expect(workspaceLoginUrl('acme', DEV)).toBe('http://acme.localhost:7777/login');
		expect(workspaceLoginUrl('acme', 'https://{slug}.feohledger.com/')).toBe('https://acme.feohledger.com/login');
	});

	it('is null for an invalid slug or an unusable template — never a guess', () => {
		expect(workspaceLoginUrl('ab', PROD)).toBeNull();
		expect(workspaceLoginUrl('evil.com/x', PROD)).toBeNull();
		expect(workspaceLoginUrl('acme', '')).toBeNull();
		expect(workspaceLoginUrl('acme', null)).toBeNull();
		expect(workspaceLoginUrl('acme', 'javascript://{slug}.x')).toBeNull();
	});
});

describe('ownDomainHost', () => {
	const suffix = '.feohledger.com';
	it("is a workspace's own custom domain, as typed", () => {
		expect(ownDomainHost('ap.acmecorp.com', suffix)).toBe('ap.acmecorp.com');
		expect(ownDomainHost(normalizeWorkspaceInput('https://AP.acmecorp.com/invoices', suffix), suffix)).toBe(
			'ap.acmecorp.com'
		);
	});

	it('is null for a slug, the platform itself, or something that is not a host', () => {
		for (const value of ['acme', 'feohledger.com', 'x.y.feohledger.com', 'ac me.com', 'acme.', '.com', 'a_b.com']) {
			expect(ownDomainHost(value, suffix), value).toBeNull();
		}
	});
});

describe('cleanWorkspaceAddressTyping', () => {
	it('keeps only what an address can hold, lowercased', () => {
		expect(cleanWorkspaceAddressTyping('Acme Corp_2!')).toBe('acmecorp2');
		expect(cleanWorkspaceAddressTyping('my-co')).toBe('my-co');
		expect(cleanWorkspaceAddressTyping('ÄCME')).toBe('cme');
	});

	it('leaves the format rule to workspaceAddressProblem', () => {
		// A leading digit or hyphen is kept, so the page can SAY why it is wrong.
		expect(cleanWorkspaceAddressTyping('9lives')).toBe('9lives');
		expect(workspaceAddressProblem(cleanWorkspaceAddressTyping('-Acme'))).toBe('startLetter');
	});
});

describe('the bounds mirror backend/app/utils/slug.py', () => {
	it('is SLUG_PATTERN = ^[a-z][a-z0-9-]{2,29}$', () => {
		const slugPy = readFileSync(
			new URL('../../../../backend/app/utils/slug.py', import.meta.url),
			'utf8'
		);
		const m = slugPy.match(/SLUG_PATTERN = re\.compile\(r"\^\[a-z\]\[a-z0-9-\]\{(\d+),(\d+)\}\$"\)/);
		expect(m, 'SLUG_PATTERN changed shape — update workspaceAddress.ts with it').not.toBeNull();
		expect(WORKSPACE_ADDRESS_MIN).toBe(Number(m![1]) + 1);
		expect(WORKSPACE_ADDRESS_MAX).toBe(Number(m![2]) + 1);
	});
});
