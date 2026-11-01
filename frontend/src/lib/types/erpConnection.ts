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
	 * Decided per provider by the backend catalogue, which applies the same
	 * flag itself (`catalog.destination_keys`): Business Central's
	 * `environment` is one, QuickBooks' (two Intuit hosts) is not.
	 */
	destination?: boolean;
	/**
	 * The field applies only while another field of this provider holds one
	 * of `values` (NetSuite's OAuth 2.0 vs TBA credentials): hidden, and not
	 * required, otherwise. See `fieldApplies`.
	 */
	show_when?: { field: string; values: string[] };
	/**
	 * What a SAVED configuration without this key means — the adapter's own
	 * reading of the absent key. Shown instead of the first option, so a form
	 * opened on an older connection does not silently switch it on save.
	 */
	stored_default?: string;
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

/**
 * Today's (UTC) API calls on an ERP whose quota is shared across tenants and
 * budgeted per org — Blackbaud SKY (`services/erp_call_budget`, decisions
 * §322 / §324). Absent for any other ERP, and when the counts can't be read.
 */
export interface ErpCallUsage {
	/** `org_share`: this org's calls against its share of the platform key.
	 *  `subscription`: every call on the org's own key, against its quota. */
	scope: 'org_share' | 'subscription' | string;
	used: number;
	/** Null when no limit applies (a share or quota configured as 0). */
	limit: number | null;
	/** The next 00:00 UTC, when the counts reset. */
	resets_at: string;
	/** `rate_limited` | `subscription_quota` | `tenant_budget`, while refused. */
	paused_reason: string | null;
	paused_until: string | null;
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
	/** Inside the first expiry-notice lead time: offer Reconnect now (§295). */
	expires_soon?: boolean;
	/** Why `needs_reconnect`: `refresh_token_expired` | `refresh_token_revoked`. */
	reconnect_reason?: string | null;
	connected_at?: string | null;
	/** Today's call usage on a shared-quota ERP (Blackbaud); see `callUsageLines`. */
	call_usage?: ErpCallUsage | null;
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
	other: 'org.erp.option.other',
	oauth2_m2m: 'org.erp.option.oauth2_m2m',
	tba: 'org.erp.option.tba'
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
		if (!f.secret && !values[f.name] && sameProvider && f.stored_default) {
			values[f.name] = f.stored_default;
		}
		if (!f.secret && !values[f.name] && f.options?.length) values[f.name] = f.options[0];
	}
	return values;
}

/** Does `f` apply to what the form currently says (its `show_when`)? */
export function fieldApplies(f: ErpProviderField, values: Record<string, string>): boolean {
	return !f.show_when || f.show_when.values.includes(values[f.show_when.field] ?? '');
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
 * pair counts as required: there is no other app to connect through. A field
 * that does not apply to the current choice (`fieldApplies`) is never missing.
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
			fieldApplies(f, values) &&
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

/**
 * The sentence for a connection that needs the admin's attention, or null:
 * why it must be reconnected (`reconnect_reason`, decisions §295), or that it
 * expires soon. A reason this build does not know reads as the generic
 * "expired" sentence it always showed.
 */
export function oauthAttentionKey(
	providerKey: string,
	status: ErpOAuthStatus | null
): MessageKey | null {
	const state = oauthConnectionState(providerKey, status);
	if (state === 'needs_reconnect') {
		if (status?.reconnect_reason === 'refresh_token_revoked') {
			return 'org.erp.oauth.needsReconnectRevoked';
		}
		if (status?.reconnect_reason === 'refresh_token_expired') {
			return 'org.erp.oauth.needsReconnectExpired';
		}
		return 'org.erp.oauth.needsReconnect';
	}
	if (state === 'connected' && status?.expires_soon && status.refresh_token_expires_at) {
		return 'org.erp.oauth.expiresSoon';
	}
	return null;
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
	// The callback came from a browser without the nonce cookie the authorize
	// call set (login-CSRF binding, `backend/app/api/erp_oauth.py`).
	browser_mismatch: 'org.erp.oauth.error.browserMismatch',
} as const satisfies Record<string, MessageKey>;

/** The 409 `GET …/oauth/{provider}/authorize` answers when this host cannot
 *  carry the browser-binding cookie — a white-label custom domain, whose `/api`
 *  is a same-origin proxy, never the API host the callback is on. */
export const CONNECT_FROM_PLATFORM_HOST = 'erp_connect_platform_host';

/**
 * Where that refusal says to connect from instead: `params.url`, only when it
 * is an absolute http(s) URL, else null (the server offered none, or sent
 * something this page will not turn into a link).
 */
export function platformConnectUrl(params: Record<string, unknown>): string | null {
	const raw = params.url;
	if (typeof raw !== 'string') return null;
	try {
		const url = new URL(raw);
		return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
	} catch {
		return null;
	}
}

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

/**
 * What `POST /api/organization/test-erp` answers. `code` says what happened
 * (`connected`, `connection_failed`, `credentials_unavailable`,
 * `unknown_adapter`); `provider` is the ERP's catalogue key (the Merge.dev
 * linked ERP for a Merge.dev connection), `adapter` the unsupported key.
 * `message` is the server's English, for a code this build does not know.
 */
export interface ErpTestResult {
	success: boolean;
	message: string;
	code?: string;
	provider?: string;
	adapter?: string;
}

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/**
 * The test result in the reader's language, naming the ERP by its display
 * name (`label`, the panel's `providerLabel`) rather than the backend's key.
 * `tested` is the provider the form tested. Anything this build cannot state
 * (an unknown code, a result for a different ERP than the form shows) is the
 * server's own sentence.
 */
export function erpTestResultText(
	result: ErpTestResult,
	tested: ErpProvider,
	label: (p: ErpProvider) => string,
	t: Translate
): string {
	const sameErp =
		tested.key === MERGE_DEV_PROVIDER ? !!result.provider : result.provider === tested.key;
	switch (result.code) {
		case 'connected':
			return sameErp ? t('org.erp.test.connected', { erp: label(tested) }) : result.message;
		case 'connection_failed':
			if (!sameErp) return result.message;
			return t(tested.auth === 'oauth' ? 'org.erp.test.failedOAuth' : 'org.erp.test.failed', {
				erp: label(tested)
			});
		case 'credentials_unavailable':
			return t('org.erp.test.credentialsUnavailable');
		case 'unknown_adapter':
			return result.adapter
				? t('org.erp.test.unknownAdapter', { adapter: result.adapter })
				: result.message;
		default:
			return result.message;
	}
}

const CALL_PAUSED_KEYS = {
	rate_limited: 'org.erp.usage.paused.rateLimited',
	tenant_budget: 'org.erp.usage.paused.tenantBudget',
	subscription_quota: 'org.erp.usage.paused.subscriptionQuota'
} as const satisfies Record<string, MessageKey>;

/** Locale formatters `callUsageLines` renders figures and times with. */
export interface CallUsageFormat {
	/** A count in the reader's locale (1,234 / 1.234 / 1 234). */
	count: (n: number) => string;
	/** An instant in the reader's locale and time zone. */
	time: (iso: string) => string;
}

/**
 * The sentences the ERP panel shows for `usage`: today's count against the
 * limit it is measured by, then — while calls are refused — why and until
 * when. Empty when there is no usage to show (another ERP, or the counts were
 * unreadable: the panel then says nothing rather than an error). A pause
 * reason this build does not know reads as the generic "paused until".
 */
export function callUsageLines(
	usage: ErpCallUsage | null | undefined,
	erp: string,
	t: Translate,
	fmt: CallUsageFormat
): string[] {
	if (!usage) return [];
	const used = fmt.count(usage.used);
	const reset = fmt.time(usage.resets_at);
	const lines = [
		usage.limit == null
			? t('org.erp.usage.noLimit', { erp, used, reset })
			: t(usage.scope === 'org_share' ? 'org.erp.usage.ofShare' : 'org.erp.usage.ofQuota', {
					erp,
					used,
					limit: fmt.count(usage.limit),
					reset
				})
	];
	if (usage.paused_reason && usage.paused_until) {
		const reason = usage.paused_reason;
		const key = Object.hasOwn(CALL_PAUSED_KEYS, reason)
			? CALL_PAUSED_KEYS[reason as keyof typeof CALL_PAUSED_KEYS]
			: 'org.erp.usage.paused';
		lines.push(t(key, { erp, until: fmt.time(usage.paused_until) }));
	}
	return lines;
}
