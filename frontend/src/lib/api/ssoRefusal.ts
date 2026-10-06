/**
 * Localizing the SSO save's coded refusal — `sso_only` over an identity-provider
 * configuration that does not resolve (`backend/app/api/organization_sso.py`,
 * `SSO_ONLY_UNRESOLVED`; docs/decisions.md §204).
 *
 * The backend sends `detail = {code, message, params: {fields}}`, where
 * `fields` holds config KEY NAMES (never values). `api.ts` lifts `code` /
 * `params` onto the thrown `ApiError`; this turns them into a sentence that
 * names each field by the label the panel shows. An unknown code, or params
 * this build cannot read, return `null` and the caller renders the server's
 * English `message` instead — never a sentence with a blank in it.
 *
 * Pure (no `$app/*`, `m` passed in), like `authRefusals.ts`, so the node vitest
 * config can reach it.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';
import { formatList } from '#lib/utils/list.ts';

export interface CodedRefusal {
	code: string | null;
	params: Record<string, unknown>;
	message: string;
}

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

export const SSO_ONLY_UNRESOLVED = 'sso_only_idp_unresolved';

/** The panel's label for each IdP key the refusal can name. */
export const SSO_FIELD_LABEL_KEYS: Record<string, MessageKey> = {
	discovery_url: 'orgSso.field.discoveryUrl',
	client_id: 'orgSso.field.clientId',
	client_secret: 'orgSso.field.clientSecret',
	idp_entity_id: 'orgSso.field.idpEntityId',
	idp_sso_url: 'orgSso.field.idpSsoUrl',
	idp_x509_cert: 'orgSso.field.idpCert',
	idp_x509_cert_multi: 'orgSso.field.idpCertMulti',
	allowed_email_domains: 'orgSso.field.allowedDomains',
	provider: 'orgSso.field.provider',
	sp_entity_id: 'orgSso.field.spEntityId',
	idp_slo_url: 'orgSso.field.idpSloUrl'
};

/** The labels for a list of IdP key names, or `null` if any is unknown. */
export function ssoFieldLabels(fields: unknown, t: Translate): string[] | null {
	if (!Array.isArray(fields) || fields.length === 0) return null;
	const labels: string[] = [];
	for (const field of fields) {
		const key = typeof field === 'string' ? SSO_FIELD_LABEL_KEYS[field] : undefined;
		if (!key) return null;
		labels.push(t(key));
	}
	return labels;
}

/** The localized sentence for the SSO save's refusal, or `null`. */
export function ssoRefusalText(refusal: CodedRefusal, t: Translate): string | null {
	if (refusal.code !== SSO_ONLY_UNRESOLVED) return null;
	const labels = ssoFieldLabels(refusal.params?.fields, t);
	if (!labels) return null;
	return t('orgSso.refusal.idpUnresolved', { fields: formatList(labels) });
}
