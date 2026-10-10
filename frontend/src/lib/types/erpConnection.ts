/**
 * The ERP setup form's catalogue and the pure logic the panel runs on it.
 *
 * The catalogue is served by `GET /api/organization/erp/providers` from the
 * backend's `erp_adapters/catalog.py`, which is the single source of truth for
 * which ERPs the form offers and which fields each needs. Nothing here
 * hardcodes a provider; adding one is a backend catalogue edit.
 *
 * Secrets are write-only and live apart from the configuration: they are sealed
 * server-side (`provider_credentials`, decisions §266), set only through
 * `PUT /api/organization/credentials/erp`, and never returned — the form learns
 * which ones are stored by NAME (`GET /api/organization/credentials`). A save
 * PATCHes the configuration (`buildErpPayload`, no secret in it) and then PUTs
 * only what was typed or removed (`buildErpSecretUpdate`). So the form never
 * holds a real secret it did not just have typed into it.
 *
 * Pure: no `$app`, no fetch, so vitest can reach it.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';
import { credentialUpdate, type ProviderCredentialUpdate } from '#lib/types/providerCredentials.ts';

export type ErpAuthKind = 'credentials' | 'oauth';

export interface ErpProviderField {
	name: string;
	label_key: MessageKey;
	secret: boolean;
	required: boolean;
	placeholder?: string;
	help_key?: MessageKey;
	/** A fixed choice list (e.g. QuickBooks `environment`). */
	options?: string[];
	/**
	 * Says where the stored secrets are sent (a host, account, tenant,
	 * company or environment). Changing one starts a new connection: the
	 * backend keeps no stored secret across it, so neither does the form.
	 */
	destination?: boolean;
}

export interface ErpProvider {
	key: string;
	label: string;
	regions: string[];
	auth: ErpAuthKind;
	fields: ErpProviderField[];
	docs_url: string;
	/** Set on the Merge.dev choice: the plan tier that includes it. */
	plan?: string;
	/** Whether this build has the adapter registered. */
	available: boolean;
}

export interface ErpCatalog {
	providers: ErpProvider[];
	merge_dev_long_tail: { value: string; label: string }[];
}

/** One OAuth ERP as `GET /api/organization/erp/oauth/status` reports it. */
export interface ErpOAuthProviderStatus {
	key: string;
	display_name: string;
	/** An app is configured to connect with: the tenant's own, or the
	 *  platform's. Not the catalogue's `available` (adapter registered). */
	available: boolean;
	client_source: 'tenant' | 'platform' | null;
}

/** `GET /api/organization/erp/oauth/status` (`api/erp_oauth.py`). No tokens. */
export interface ErpOAuthStatus {
	provider: string | null;
	connected: boolean;
	/** The refresh token was refused: the consent has to be given again. */
	needs_reconnect: boolean;
	external_tenant_id?: string | null;
	expires_at?: string | null;
	refresh_token_expires_at?: string | null;
	connected_at?: string | null;
	/** What a bring-your-own app must register as its redirect URI. */
	redirect_uri: string;
	providers: ErpOAuthProviderStatus[];
}

/** `settings.erp` as `GET /api/organization` returns it: configuration only. */
export type StoredErpConfig = Record<string, unknown> & {
	type?: string;
	integration_method?: string;
	oauth?: { connected: boolean };
};

export const MERGE_DEV_PROVIDER = 'merge_dev';

/** The bring-your-own-app pair every OAuth provider accepts (`catalog.py`
 *  `_byo_app_fields`). Optional while the platform has its own app. */
export const BYO_APP_FIELDS: readonly string[] = ['client_id', 'client_secret'];

/**
 * A choice value (the ERP's own code, or a Merge.dev long-tail value) whose
 * display label is catalogued. Anything else renders as itself, which is
 * what the ERP calls it.
 */
const OPTION_LABEL_KEYS: Record<string, MessageKey> = {
	production: 'org.erp.env.production',
	sandbox: 'org.erp.env.sandbox',
	AUTHORISED: 'org.erp.option.AUTHORISED',
	DRAFT: 'org.erp.option.DRAFT',
	Pending: 'org.erp.option.Pending',
	Approved: 'org.erp.option.Approved',
	other: 'org.erp.option.other'
};

export function optionLabelKey(option: string): MessageKey | null {
	return Object.hasOwn(OPTION_LABEL_KEYS, option) ? OPTION_LABEL_KEYS[option] : null;
}

/** The two region groups the dropdown leads with, in display order. */
export const ERP_REGION_GROUPS: { region: string; labelKey: MessageKey }[] = [
	{ region: 'US', labelKey: 'org.erp.group.us' },
	{ region: 'ZA', labelKey: 'org.erp.group.za' }
];

export interface ErpOptionGroup {
	labelKey: MessageKey;
	providers: ErpProvider[];
}

/**
 * Dropdown groups: one per region (an ERP popular in both appears in both,
 * which is what someone scanning "their" region expects), then the rest —
 * Merge.dev last.
 */
export function groupProviders(providers: ErpProvider[]): ErpOptionGroup[] {
	const groups: ErpOptionGroup[] = [];
	const placed = new Set<string>();
	for (const { region, labelKey } of ERP_REGION_GROUPS) {
		const inRegion = providers.filter((p) => p.regions.includes(region));
		inRegion.forEach((p) => placed.add(p.key));
		if (inRegion.length) groups.push({ labelKey, providers: inRegion });
	}
	const rest = providers.filter((p) => !placed.has(p.key));
	if (rest.length) groups.push({ labelKey: 'org.erp.group.other', providers: rest });
	return groups;
}

/** The catalogue entry a stored config selects (Merge.dev by its method). */
export function selectedProviderKey(erp: StoredErpConfig | undefined | null): string | null {
	if (!erp || (!erp.type && !erp.integration_method)) return null;
	if ((erp.integration_method ?? MERGE_DEV_PROVIDER) === MERGE_DEV_PROVIDER) return MERGE_DEV_PROVIDER;
	return erp.type || null;
}

/**
 * The form's initial values for `provider` from what is stored. A secret
 * always starts blank: blank means "keep", and the field says whether one is
 * stored instead (see `secretIsSaved`).
 */
export function initialValues(
	provider: ErpProvider,
	stored: StoredErpConfig | undefined | null
): Record<string, string> {
	const sameProvider = stored ? selectedProviderKey(stored) === provider.key : false;
	const values: Record<string, string> = {};
	for (const f of provider.fields) {
		const raw = sameProvider ? stored?.[f.name] : undefined;
		const value = typeof raw === 'string' ? raw : '';
		values[f.name] = f.secret ? '' : value;
		if (!f.secret && !values[f.name] && f.options?.length) values[f.name] = f.options[0];
	}
	return values;
}

/**
 * Has any destination field in `values` moved away from what is stored? Blank
 * and absent are the same, as on the backend (`catalog.same_connection`).
 */
export function destinationChanged(
	provider: ErpProvider,
	values: Record<string, string>,
	stored: StoredErpConfig | undefined | null
): boolean {
	if (!stored) return false;
	return provider.fields.some((f) => {
		if (!f.destination) return false;
		const was = typeof stored[f.name] === 'string' ? (stored[f.name] as string).trim() : '';
		return (values[f.name] ?? '').trim() !== was;
	});
}

/**
 * Is a secret stored for this field, for the provider on file? `storedNames`
 * is the `erp` list from `GET /api/organization/credentials` (names only).
 * Not for another ERP than the one on file (a save switching ERP drops its
 * secrets), and not once `values` changes a destination field: the backend
 * drops a stored secret rather than carry it to a new host or account
 * (`catalog.secrets_to_drop`), so the form asks for it again.
 */
export function secretIsSaved(
	provider: ErpProvider,
	field: ErpProviderField,
	stored: StoredErpConfig | undefined | null,
	storedNames: readonly string[],
	values?: Record<string, string>
): boolean {
	if (!field.secret || !stored || selectedProviderKey(stored) !== provider.key) return false;
	if (values && destinationChanged(provider, values, stored)) return false;
	return storedNames.includes(field.name);
}

/** Keys a save never sends: the OAuth block's only writer is the callback. */
const NEVER_SENT_KEYS: ReadonlySet<string> = new Set(['oauth']);

/**
 * The `settings.erp` CONFIGURATION a save sends: no secret field at all —
 * `PATCH /api/organization` refuses one, and they go to the credentials
 * endpoint (`buildErpSecretUpdate`). The OAuth block is never sent: only the
 * OAuth callback writes it.
 */
export function buildErpPayload(
	provider: ErpProvider,
	values: Record<string, string>,
	mergeErpType: string,
	stored?: StoredErpConfig | null
): Record<string, unknown> {
	const body: Record<string, unknown> =
		provider.key === MERGE_DEV_PROVIDER
			? { type: mergeErpType || 'other', integration_method: MERGE_DEV_PROVIDER }
			: { type: provider.key, integration_method: 'direct' };
	// A save replaces the stored block, so a setting this form does not render
	// (Blackbaud's `transaction_code_values`, set through the API) would be
	// lost on every save. Send those back unchanged, for the same ERP only.
	if (stored && selectedProviderKey(stored) === provider.key && stored.type === body.type) {
		const rendered = new Set(provider.fields.map((f) => f.name));
		for (const [key, value] of Object.entries(stored)) {
			if (!(key in body) && !rendered.has(key) && !NEVER_SENT_KEYS.has(key)) body[key] = value;
		}
	}
	for (const f of provider.fields) {
		if (!f.secret) body[f.name] = (values[f.name] ?? '').trim();
	}
	return body;
}

/**
 * The `PUT /api/organization/credentials/erp` body for `provider`'s secret
 * fields, or `null` when the save changes none (no request is made). A typed
 * value is set; a blank one keeps what is stored, unless its remove toggle is
 * on (`clear[name]`); a typed value wins over a stale toggle.
 */
export function buildErpSecretUpdate(
	provider: ErpProvider,
	values: Record<string, string>,
	clear: Readonly<Record<string, boolean>>
): ProviderCredentialUpdate | null {
	return credentialUpdate(
		Object.fromEntries(
			provider.fields
				.filter((f) => f.secret)
				.map((f) => [f.name, { value: values[f.name] ?? '', clear: clear[f.name] === true }])
		)
	);
}

/**
 * The body of a connection TEST: the configuration plus each secret the admin
 * has typed. A blank secret is left out, and the backend fills it from the
 * sealed store only while the form names the saved ERP at the saved
 * destination (`provider_credentials.config_for_connection_test`).
 */
export function buildErpTestPayload(
	provider: ErpProvider,
	values: Record<string, string>,
	mergeErpType: string,
	stored?: StoredErpConfig | null
): Record<string, unknown> {
	const body = buildErpPayload(provider, values, mergeErpType, stored);
	for (const f of provider.fields) {
		const typed = (values[f.name] ?? '').trim();
		if (f.secret && typed) body[f.name] = typed;
	}
	return body;
}

/**
 * Must the admin bring their own app for this OAuth provider? Only when the
 * status says no app at all is configured: the platform has none and none is
 * saved. Unknown (status not loaded, or not an OAuth ERP) is "no", so the
 * form never demands credentials on a guess.
 */
export function byoAppRequired(provider: ErpProvider, status: ErpOAuthStatus | null): boolean {
	if (provider.auth !== 'oauth' || !status) return false;
	const entry = status.providers.find((p) => p.key === provider.key);
	return entry ? !entry.available : false;
}

/**
 * Required fields still empty. A required secret that is already stored counts
 * as filled, since leaving it blank keeps it, unless the admin chose to remove
 * it (`clear[name]`). With `requireByoApp` the optional client id + secret
 * pair counts as required: there is no other app to connect through.
 */
export function missingRequired(
	provider: ErpProvider,
	values: Record<string, string>,
	stored: StoredErpConfig | undefined | null,
	storedNames: readonly string[],
	opts: { clear?: Readonly<Record<string, boolean>>; requireByoApp?: boolean } = {}
): ErpProviderField[] {
	const clear = opts.clear ?? {};
	return provider.fields.filter(
		(f) =>
			(f.required || (opts.requireByoApp === true && BYO_APP_FIELDS.includes(f.name))) &&
			!(values[f.name] ?? '').trim() &&
			!(secretIsSaved(provider, f, stored, storedNames, values) && clear[f.name] !== true)
	);
}

/** Where an OAuth ERP's connection stands, for the provider being shown. */
export type OAuthConnectionState = 'connected' | 'needs_reconnect' | 'not_connected';

export function oauthConnectionState(
	providerKey: string,
	status: ErpOAuthStatus | null
): OAuthConnectionState {
	if (!status || status.provider !== providerKey) return 'not_connected';
	if (status.needs_reconnect) return 'needs_reconnect';
	return status.connected ? 'connected' : 'not_connected';
}

/** The `?erp_connected=` / `?erp_error=` return from the OAuth callback. */
export type OAuthReturn =
	| { kind: 'connected'; provider: string }
	| { kind: 'error'; code: string }
	| null;

/** The OAuth callback's `erp_error` codes an admin can act on, each with its
 * own explanation. Any other code falls back to the generic message. */
export const OAUTH_ERROR_KEYS = {
	state_expired: 'org.erp.oauth.error.stateExpired',
	access_denied: 'org.erp.oauth.error.accessDenied',
	not_authorized: 'org.erp.oauth.error.notAuthorized',
	plan_required: 'org.erp.oauth.error.planRequired',
	provider_unavailable: 'org.erp.oauth.error.providerUnavailable',
	token_exchange_failed: 'org.erp.oauth.error.tokenExchangeFailed',
	no_external_tenant: 'org.erp.oauth.error.noExternalTenant',
	already_linked: 'org.erp.oauth.error.alreadyLinked',
	credentials_unavailable: 'org.erp.oauth.error.credentialsUnavailable',
	audit_unavailable: 'org.erp.oauth.error.auditUnavailable',
} as const satisfies Record<string, MessageKey>;

/** Bounded so a crafted URL can't put a paragraph into the status line. */
const RETURN_PARAM_LIMIT = 64;

/** The query parameters the OAuth callback appends on its way back. */
export const OAUTH_RETURN_PARAMS: readonly string[] = ['erp_connected', 'erp_error'];

/**
 * `url` without the OAuth return parameters (path + query, the shape
 * SvelteKit's `replaceState` takes), or null when it carries none. Shown once,
 * the message must not come back on a reload or a shared link.
 */
export function withoutOAuthReturn(url: Pick<URL, 'href'>): string | null {
	const next = new URL(url.href);
	if (!OAUTH_RETURN_PARAMS.some((p) => next.searchParams.has(p))) return null;
	for (const p of OAUTH_RETURN_PARAMS) next.searchParams.delete(p);
	return `${next.pathname}${next.search}${next.hash}`;
}

export function readOAuthReturn(params: Pick<URLSearchParams, 'get'>): OAuthReturn {
	const error = params.get('erp_error');
	if (error) return { kind: 'error', code: error.slice(0, RETURN_PARAM_LIMIT) };
	const connected = params.get('erp_connected');
	if (connected) return { kind: 'connected', provider: connected.slice(0, RETURN_PARAM_LIMIT) };
	return null;
}
