/**
 * Per-tenant SSO configuration (`/organization` → SSO), over
 * `GET/PUT /api/organization/sso` (`backend/app/api/organization_sso.py`).
 *
 * The OIDC client secret is **write-only**: no endpoint returns it, so this
 * type carries `client_secret_configured` and never the value. A save that
 * omits the secret (or sends it blank) keeps the stored one; removing it is
 * the explicit `clear_client_secret`. See `docs/authentication.md` § SSO
 * configuration.
 */
export type SsoProtocol = 'oidc' | 'saml';

export interface SsoSettingsStatus {
	enabled: boolean;
	sso_only: boolean;
	protocol: SsoProtocol;
	provider: string | null;
	allowed_email_domains: string[];
	discovery_url: string | null;
	client_id: string | null;
	client_secret_configured: boolean;
	idp_entity_id: string | null;
	idp_sso_url: string | null;
	/** The IdP's PUBLIC signing certificate — not a secret. */
	idp_x509_cert: string | null;
	idp_x509_cert_multi: string[];
	sp_entity_id: string | null;
	idp_slo_url: string | null;
	scim_group_role_map: Record<string, string>;
	scim_token_configured: boolean;
	/** The server's verdict (`is_sso_only`), not the request. */
	password_sign_in_closed: boolean;
	/** IdP key names the selected protocol still lacks, as if SSO were on. */
	idp_config_missing: string[];
	/** What the admin registers at the IdP — computed server-side. */
	oidc_redirect_uri: string;
	saml_acs_url: string;
	saml_sp_entity_id: string;
}

/**
 * The whole configuration, PUT back. Keys left out are removed server-side,
 * except the secret (kept unless `client_secret` is non-blank or
 * `clear_client_secret` is set) and the SCIM state (always carried; the role
 * map is carried unless named here).
 */
export interface SsoSettingsUpdate {
	enabled: boolean;
	sso_only: boolean;
	protocol: SsoProtocol;
	provider: string | null;
	allowed_email_domains: string[];
	discovery_url: string | null;
	client_id: string | null;
	client_secret?: string;
	clear_client_secret?: boolean;
	idp_entity_id: string | null;
	idp_sso_url: string | null;
	idp_x509_cert: string | null;
	idp_x509_cert_multi: string[];
	sp_entity_id: string | null;
	idp_slo_url: string | null;
}

/**
 * The button label on `/login` for each `settings.sso.provider` token. Product
 * names — English by convention, like `CHAT_PROVIDER_LABELS`. The generic
 * tokens read "SSO". One table, shared by the login page and the SSO panel's
 * picker, so the panel can only offer a label the login page knows.
 */
export const SSO_PROVIDER_LABELS: Record<string, string> = {
	okta: 'Okta',
	entra: 'Microsoft',
	oidc: 'SSO',
	saml: 'SSO',
	adfs: 'ADFS',
	onelogin: 'OneLogin'
};
