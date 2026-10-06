import { describe, expect, it } from 'vitest';
import { en } from '#lib/i18n/locales/en.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';
import { interpolate } from '#lib/i18n/interpolate.ts';
import {
	SSO_FIELD_LABEL_KEYS,
	SSO_ONLY_UNRESOLVED,
	ssoFieldLabels,
	ssoRefusalText
} from './ssoRefusal.ts';

/**
 * The SSO save's coded refusal (`backend/app/api/organization_sso.py`,
 * `SSO_ONLY_UNRESOLVED`) renders in the reader's language and names each
 * missing field by the label the panel shows; anything this build cannot state
 * falls back to the server's English.
 */
const t = (key: MessageKey, params?: Record<string, string | number>) =>
	interpolate(en[key], params, 'en');

describe('ssoRefusalText', () => {
	it('pins the backend code — a rename on either side fails here', () => {
		expect(SSO_ONLY_UNRESOLVED).toBe('sso_only_idp_unresolved');
	});

	it('covers every IdP key services/sso.py can name', () => {
		// OIDC_REQUIRED_FIELDS + SAML_REQUIRED_FIELDS + the optional keys whose
		// malformed value `check_sso_idp_config` refuses by name.
		for (const field of [
			'discovery_url',
			'client_id',
			'client_secret',
			'idp_entity_id',
			'idp_sso_url',
			'idp_x509_cert',
			'idp_x509_cert_multi',
			'allowed_email_domains',
			'provider',
			'sp_entity_id',
			'idp_slo_url'
		]) {
			expect(SSO_FIELD_LABEL_KEYS[field], field).toBeDefined();
		}
	});

	it('names each missing field by its panel label', () => {
		const text = ssoRefusalText(
			{
				code: SSO_ONLY_UNRESOLVED,
				params: { fields: ['discovery_url', 'client_secret'] },
				message: 'server english'
			},
			t
		);
		expect(text).toBe(
			'Requiring SSO closes password sign-in, so the identity-provider settings must be ' +
				'complete first. Missing or invalid: Discovery URL, Client secret.'
		);
	});

	it.each([
		['another code', { code: 'step_up_failed', params: { fields: ['client_id'] } }],
		['no code', { code: null, params: {} }],
		['no fields', { code: SSO_ONLY_UNRESOLVED, params: {} }],
		['an unknown field', { code: SSO_ONLY_UNRESOLVED, params: { fields: ['client_id', 'x'] } }],
		['a non-string field', { code: SSO_ONLY_UNRESOLVED, params: { fields: [7] } }]
	])('returns null for %s, so the caller shows the server English', (_label, refusal) => {
		expect(ssoRefusalText({ ...refusal, message: 'm' }, t)).toBeNull();
	});

	it('ssoFieldLabels refuses an empty list rather than rendering a blank', () => {
		expect(ssoFieldLabels([], t)).toBeNull();
		expect(ssoFieldLabels(['client_id'], t)).toEqual(['Client ID']);
	});
});
