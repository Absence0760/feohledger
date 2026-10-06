import { describe, expect, it } from 'vitest';
import { en } from '#lib/i18n/locales/en.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';
import { interpolate } from '#lib/i18n/interpolate.ts';
import { AUTH_REFUSAL_CODES, authErrorMessage, authRefusalText } from './authRefusals.ts';

/**
 * The coded step-up / passkey refusals (`backend/app/api/auth.py::coded_refusal`)
 * render in the reader's language on `/profile` and the MFA login page, and
 * fall back to the server's English for anything this build cannot state.
 *
 * `t` here is a stand-in for `m()` that resolves against the English catalogue
 * and records the key, so the test proves WHICH key a code maps to as well as
 * that the params reach it.
 */
function recorder() {
	const calls: { key: MessageKey; params?: Record<string, string | number> }[] = [];
	const t = (key: MessageKey, params?: Record<string, string | number>) => {
		calls.push({ key, params });
		return interpolate(en[key], params, 'en');
	};
	return { t, calls };
}

/** Shaped like the `ApiError` `api.ts` throws for a coded `detail`. */
function apiError(code: unknown, params: unknown, message: string): Error {
	return Object.assign(new Error(message), { status: 400, code, params });
}

describe('authRefusalText', () => {
	it('pins every code the backend emits — a rename on either side fails here', () => {
		// The literals `api/auth.py` and `api/portal_auth.py` pass to `coded_refusal`.
		expect([...AUTH_REFUSAL_CODES].sort()).toEqual([
			'passkey_not_registered',
			'passkey_wrong_host',
			'portal_step_up_failed',
			'sensitive_step_up_failed',
			'sensitive_step_up_no_factor',
			'sensitive_step_up_required',
			'sensitive_step_up_unavailable',
			'step_up_failed',
			'step_up_sso_only'
		]);
	});

	it.each([
		['step_up_failed', 'authRefusal.stepUpFailed'],
		['step_up_sso_only', 'authRefusal.stepUpSsoOnly'],
		['passkey_not_registered', 'authRefusal.passkeyNotRegistered'],
		// The portal's own sentence: it must not offer a passkey, which a
		// supplier account cannot have.
		['portal_step_up_failed', 'authRefusal.portalStepUpFailed'],
		['sensitive_step_up_required', 'authRefusal.sensitiveStepUpRequired'],
		['sensitive_step_up_failed', 'authRefusal.sensitiveStepUpFailed'],
		['sensitive_step_up_no_factor', 'authRefusal.sensitiveStepUpNoFactor'],
		['sensitive_step_up_unavailable', 'authRefusal.sensitiveStepUpUnavailable']
	] as const)('maps %s to %s', (code, key) => {
		const { t, calls } = recorder();
		const out = authRefusalText({ code, params: {}, message: 'server English' }, t);
		expect(calls.map((c) => c.key)).toEqual([key]);
		expect(out).toBe(en[key]);
	});

	it('never offers the password as a second factor for a sensitive action', () => {
		// `require_sensitive_step_up` ignores an offered password in every tenant;
		// a sentence that asked for one would send the reader to a dead end.
		for (const key of [
			'authRefusal.sensitiveStepUpRequired',
			'authRefusal.sensitiveStepUpFailed'
		] as const) {
			expect(en[key].toLowerCase()).not.toContain('password');
		}
	});

	it('never offers a supplier the passkey their portal account cannot hold', () => {
		expect(en['authRefusal.portalStepUpFailed'].toLowerCase()).not.toContain('passkey');
	});

	it('fills the wrong-host sentence from params, joining the hosts itself', () => {
		const { t, calls } = recorder();
		const out = authRefusalText(
			{
				code: 'passkey_wrong_host',
				params: { registered_hosts: ['acme.example.com', 'ap.acme.test'], host: 'pay.acme.io' },
				message: 'server English'
			},
			t
		);
		expect(calls[0].key).toBe('authRefusal.passkeyWrongHost');
		expect(calls[0].params?.host).toBe('pay.acme.io');
		expect(out).toContain('acme.example.com');
		expect(out).toContain('ap.acme.test');
		expect(out).toContain('pay.acme.io');
		expect(out).not.toContain('{');
	});

	it.each([
		['no hosts', { host: 'pay.acme.io' }],
		['empty hosts', { registered_hosts: [], host: 'pay.acme.io' }],
		['non-string host entry', { registered_hosts: [42], host: 'pay.acme.io' }],
		['no current host', { registered_hosts: ['acme.example.com'] }]
	])('declines a wrong-host refusal with %s rather than render a blank', (_label, params) => {
		const { t } = recorder();
		expect(
			authRefusalText({ code: 'passkey_wrong_host', params, message: 'server English' }, t)
		).toBeNull();
	});

	it('declines an unknown code and a missing one', () => {
		const { t, calls } = recorder();
		expect(authRefusalText({ code: 'from_a_newer_backend', params: {}, message: 'x' }, t)).toBeNull();
		expect(authRefusalText({ code: null, params: {}, message: 'x' }, t)).toBeNull();
		expect(calls).toEqual([]);
	});
});

describe('authErrorMessage', () => {
	it('localizes a coded ApiError', () => {
		const { t } = recorder();
		const err = apiError('step_up_sso_only', {}, 'server English');
		expect(authErrorMessage(err, t, 'profile.mfa.disableFailed')).toBe(
			en['authRefusal.stepUpSsoOnly']
		);
	});

	it("falls back to the server's English for a code this build predates", () => {
		const { t } = recorder();
		const err = apiError('step_up_newer_reason', {}, 'Some newer English sentence.');
		expect(authErrorMessage(err, t, 'profile.mfa.disableFailed')).toBe(
			'Some newer English sentence.'
		);
	});

	it('renders an uncoded error by its message', () => {
		const { t } = recorder();
		expect(authErrorMessage(apiError(null, {}, 'MFA is disabled'), t, 'profile.mfa.disableFailed')).toBe(
			'MFA is disabled'
		);
	});

	it('leaves a cancelled browser prompt (a numeric DOMException code) to its own message', () => {
		const { t, calls } = recorder();
		const cancelled = Object.assign(new Error('The operation was aborted.'), { code: 20 });
		expect(authErrorMessage(cancelled, t, 'profile.passkeys.addFailed')).toBe(
			'The operation was aborted.'
		);
		expect(calls).toEqual([]);
	});

	it('uses the localized fallback for a non-Error throw', () => {
		const { t } = recorder();
		expect(authErrorMessage('boom', t, 'profile.passkeys.addFailed')).toBe(
			en['profile.passkeys.addFailed']
		);
	});
});
