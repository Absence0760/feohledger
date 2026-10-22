import { describe, expect, it } from 'vitest';
import { en } from '#lib/i18n/locales/en.ts';
import { IDP_ERROR_KEYS, idpErrorCodeForDisplay, idpErrorKey } from '#lib/utils/idpCallbackError.ts';

describe('idpErrorKey', () => {
	it.each([
		'access_denied',
		'login_required',
		'consent_required',
		'interaction_required',
		'temporarily_unavailable',
		'server_error'
	])('%s has a sentence of its own, which never names the code', (code) => {
		const key = idpErrorKey(code);
		expect(key).not.toBe('auth.callback.idpUnknown');
		expect(en[key]).toBeTruthy();
		expect(en[key]).not.toContain(code);
	});

	it.each(['invalid_scope', 'unauthorized_client', '', 'toString', '__proto__'])(
		'%j falls back to the generic sentence',
		(code) => {
			expect(idpErrorKey(code)).toBe('auth.callback.idpUnknown');
		}
	);

	it('maps every code to a distinct sentence', () => {
		const keys = Object.values(IDP_ERROR_KEYS);
		expect(new Set(keys).size).toBe(keys.length);
	});
});

describe('idpErrorCodeForDisplay', () => {
	it('shows a protocol-shaped code as support detail', () => {
		expect(idpErrorCodeForDisplay('access_denied')).toBe('access_denied');
		expect(idpErrorCodeForDisplay('AADSTS50105')).toBe('AADSTS50105');
	});

	it.each([
		'Your account is locked. Call 1-800-555-0100',
		'<b>x</b>',
		'a'.repeat(65),
		''
	])('never echoes %j, which is text someone wrote into the link', (value) => {
		expect(idpErrorCodeForDisplay(value)).toBeNull();
	});
});
