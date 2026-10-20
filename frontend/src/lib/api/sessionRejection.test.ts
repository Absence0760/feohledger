import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
	loginNoticeKey,
	loginPathFor,
	ownWorkspaceSignInUrl,
	sessionRejection,
	TOKEN_TENANT_MISMATCH
} from './sessionRejection.ts';

describe('sessionRejection', () => {
	it('pins the code the backend emits', () => {
		// Read the backend source rather than re-typing it: a rename on either
		// side must fail here, not leave the SPA hanging on a refusal it no
		// longer recognises.
		const refusals = new URL('../../../../backend/app/api/refusals.py', import.meta.url).pathname;
		expect(readFileSync(refusals, 'utf8')).toContain(
			`TOKEN_TENANT_MISMATCH = "${TOKEN_TENANT_MISMATCH}"`
		);
	});

	it('ends the session on a 401, whatever the code', () => {
		expect(sessionRejection(401, null)).toBe('expired');
		expect(sessionRejection(401, 'anything')).toBe('expired');
	});

	it('ends the session on a token for another workspace', () => {
		expect(sessionRejection(403, TOKEN_TENANT_MISMATCH)).toBe('other_workspace');
	});

	it('leaves every other refusal to the caller', () => {
		// A role refusal, a sign-in on the wrong page (no session yet), an
		// uncoded 403 and a non-auth status must not sign anyone out.
		expect(sessionRejection(403, null)).toBeNull();
		expect(sessionRejection(403, 'wrong_workspace')).toBeNull();
		expect(sessionRejection(403, 'sensitive_step_up_required')).toBeNull();
		expect(sessionRejection(404, TOKEN_TENANT_MISMATCH)).toBeNull();
		expect(sessionRejection(409, null)).toBeNull();
	});
});

describe('the /login round trip', () => {
	it('sends an expired session to the plain sign-in page', () => {
		expect(loginPathFor('expired')).toBe('/login');
		expect(loginNoticeKey(new URL('http://x/login').searchParams)).toBeNull();
	});

	it('says why when the token belonged to another workspace', () => {
		const path = loginPathFor('other_workspace');
		expect(loginNoticeKey(new URL(`http://x${path}`).searchParams)).toBe(
			'auth.login.signedOutOtherWorkspace'
		);
	});

	it('honours only a reason it set', () => {
		expect(loginNoticeKey(new URLSearchParams('reason=something_else'))).toBeNull();
	});
});

describe('ownWorkspaceSignInUrl', () => {
	it('returns an http(s) sign-in link', () => {
		const url = 'https://signuptest.feohledger.com/login?email=a%40b.c';
		expect(ownWorkspaceSignInUrl({ sign_in_url: url })).toBe(url);
		expect(ownWorkspaceSignInUrl({ sign_in_url: 'http://techflow.localhost:7777/login' })).toBe(
			'http://techflow.localhost:7777/login'
		);
	});

	it('refuses anything that is not an http(s) URL', () => {
		expect(ownWorkspaceSignInUrl({ sign_in_url: 'javascript:alert(1)' })).toBeNull();
		expect(ownWorkspaceSignInUrl({ sign_in_url: '/login' })).toBeNull();
		expect(ownWorkspaceSignInUrl({ sign_in_url: null })).toBeNull();
		expect(ownWorkspaceSignInUrl({})).toBeNull();
	});
});
