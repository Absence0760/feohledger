/**
 * Commerce / inventory platform connection and receipt sync — types and the
 * pure logic the setup panel and the receipt's sync section run on
 * (decisions §268).
 *
 * The provider catalogue comes from `GET /api/commerce/providers`
 * (`commerce_adapters/catalog.py`): nothing here hardcodes a provider, so a new
 * platform needs no frontend change. Secrets are write-only and sealed
 * server-side: the configuration saves through `PUT /api/commerce/settings`,
 * the secrets through `PUT /api/organization/credentials/commerce`, and the
 * settings read reports only which secret NAMES are stored (`secrets_set`). A
 * save naming another platform or destination drops them server-side.
 *
 * Pure: no `$app`, no fetch, so vitest can reach it.
 */
import type { BadgeTone } from '#lib/components/ui/badgeTone.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';
import {
	credentialUpdate,
	type ProviderCredentialUpdate
} from '#lib/types/providerCredentials.ts';

export type CommerceAuthKind = 'credentials' | 'oauth';

export interface CommerceProviderField {
	name: string;
	label_key: MessageKey;
	secret: boolean;
	required: boolean;
	placeholder?: string;
	help_key?: MessageKey;
	options?: string[];
	/** The field says where the credentials go; changing it drops them. */
	destination?: boolean;
}

export interface CommerceProvider {
	key: string;
	label: string;
	auth: CommerceAuthKind;
	regions: string[];
	fields: CommerceProviderField[];
	docs_url: string;
	supports_fractional_quantities: boolean;
	/** Selectable in this build. */
	available: boolean;
}

export interface CommerceCatalog {
	providers: CommerceProvider[];
}

/** `settings.commerce` as `GET /api/commerce/settings` returns it — configuration only. */
export type StoredCommerceConfig = Record<string, unknown> & {
	provider?: string;
	location_id?: string;
	entity_locations?: Record<string, string>;
};

export interface CommerceSettingsView {
	configured: boolean;
	settings: StoredCommerceConfig | null;
	/** Which secret fields are sealed — names only, never a value. */
	secrets_set: string[];
}

export interface CommerceLocation {
	id: string;
	name: string;
	active: boolean;
}

/** `GET /api/commerce/locations` — the platform's locations, live, plus the mapping. */
export interface CommerceLocationsView {
	available: boolean;
	code: string | null;
	locations: CommerceLocation[];
	location_id: string | null;
	entity_locations: Record<string, string>;
}

export interface CommerceTestResult {
	success: boolean;
	code: string | null;
	message: string;
	locations: CommerceLocation[];
}

export interface CommerceSkuMapping {
	id: string;
	provider: string;
	sku: string;
	provider_item_id: string;
	updated_at: string | null;
}

export type CommerceSyncStatus =
	| 'pending'
	| 'in_flight'
	| 'synced'
	| 'skipped'
	| 'failed'
	| 'cancelled';

/** One row of `GET /api/commerce/syncs?goods_receipt_id=`. */
export interface CommerceSync {
	id: string;
	goods_receipt_id: string;
	gr_line_item_id: string;
	direction: 'receive' | 'reverse';
	provider: string;
	sku: string | null;
	/** Signed count of goods, an exact decimal string. */
	quantity_delta: string;
	status: CommerceSyncStatus;
	reason: string | null;
	error_message: string | null;
	retryable: boolean;
	provider_reference: string | null;
	provider_location_id: string | null;
	attempts: number;
	last_attempt_at: string | null;
	synced_at: string | null;
	can_retry: boolean;
}

export const COMMERCE_SYNC_STATUS_KEYS: Record<CommerceSyncStatus, MessageKey> = {
	pending: 'commerceSync.status.pending',
	in_flight: 'commerceSync.status.in_flight',
	synced: 'commerceSync.status.synced',
	skipped: 'commerceSync.status.skipped',
	failed: 'commerceSync.status.failed',
	cancelled: 'commerceSync.status.cancelled'
};

export const COMMERCE_SYNC_STATUS_TONES: Record<CommerceSyncStatus, BadgeTone> = {
	pending: 'accent',
	in_flight: 'accent',
	synced: 'success',
	skipped: 'muted',
	failed: 'danger',
	cancelled: 'neutral'
};

/**
 * Every stable reason the sync or an adapter records (`commerce_sync.py`,
 * `commerce_adapters/base.py`). An unknown one (a newer backend, a provider
 * code) falls back to the generic sentence plus the server's own message.
 */
export const COMMERCE_SYNC_REASON_KEYS = {
	no_sku: 'commerceSync.reason.no_sku',
	zero_quantity: 'commerceSync.reason.zero_quantity',
	unmapped_sku: 'commerceSync.reason.unmapped_sku',
	no_location: 'commerceSync.reason.no_location',
	nothing_to_reverse: 'commerceSync.reason.nothing_to_reverse',
	receipt_cancelled: 'commerceSync.reason.receipt_cancelled',
	not_configured: 'commerceSync.reason.not_configured',
	provider_changed: 'commerceSync.reason.provider_changed',
	credentials_unreadable: 'commerceSync.reason.credentials_unreadable',
	fractional_quantity: 'commerceSync.reason.fractional_quantity',
	auth_failed: 'commerceSync.reason.auth_failed',
	config_invalid: 'commerceSync.reason.config_invalid',
	unknown_location: 'commerceSync.reason.unknown_location',
	rejected: 'commerceSync.reason.rejected',
	transient: 'commerceSync.reason.transient',
	rate_limited: 'commerceSync.reason.rate_limited',
	unexpected: 'commerceSync.reason.unexpected'
} as const satisfies Record<string, MessageKey>;

export function syncReasonKey(reason: string | null): MessageKey | null {
	if (!reason) return null;
	return Object.hasOwn(COMMERCE_SYNC_REASON_KEYS, reason)
		? COMMERCE_SYNC_REASON_KEYS[reason as keyof typeof COMMERCE_SYNC_REASON_KEYS]
		: 'commerceSync.reason.other';
}

/** A choice value the form has a translated label for; anything else renders as itself. */
const OPTION_LABEL_KEYS: Record<string, MessageKey> = {
	production: 'org.commerce.option.production',
	sandbox: 'org.commerce.option.sandbox'
};

export function commerceOptionLabelKey(option: string): MessageKey | null {
	return Object.hasOwn(OPTION_LABEL_KEYS, option) ? OPTION_LABEL_KEYS[option] : null;
}

/**
 * The form's initial values for `provider`. A secret always starts blank: it
 * is sealed server-side and never read back; blank means "keep", and the field
 * says whether one is stored instead (`commerceSecretIsSaved`).
 */
export function initialCommerceValues(
	provider: CommerceProvider,
	stored: StoredCommerceConfig | null | undefined
): Record<string, string> {
	const same = stored?.provider === provider.key;
	const values: Record<string, string> = {};
	for (const f of provider.fields) {
		const raw = same ? stored?.[f.name] : undefined;
		values[f.name] = f.secret || typeof raw !== 'string' ? '' : raw;
		if (!f.secret && !values[f.name] && f.options?.length) values[f.name] = f.options[0];
	}
	return values;
}

/**
 * Has a destination field in `values` moved away from what is stored? Blank
 * and absent are the same, as on the backend (`catalog.same_connection`).
 */
export function commerceDestinationChanged(
	provider: CommerceProvider,
	values: Record<string, string>,
	stored: StoredCommerceConfig | null | undefined
): boolean {
	if (!stored) return false;
	return provider.fields.some((f) => {
		if (!f.destination) return false;
		const was = typeof stored[f.name] === 'string' ? (stored[f.name] as string).trim() : '';
		return (values[f.name] ?? '').trim() !== was;
	});
}

/**
 * Is a secret sealed for this field, for the platform on file? `storedNames`
 * is the settings read's `secrets_set` (names only). Not for another platform,
 * and not once `values` changes a destination field: the backend drops a
 * stored secret rather than carry it to a new host (`catalog.secrets_to_drop`),
 * so the form asks for it again.
 */
export function commerceSecretIsSaved(
	provider: CommerceProvider,
	field: CommerceProviderField,
	stored: StoredCommerceConfig | null | undefined,
	storedNames: readonly string[],
	values?: Record<string, string>
): boolean {
	if (!field.secret || stored?.provider !== provider.key) return false;
	if (values && commerceDestinationChanged(provider, values, stored)) return false;
	return storedNames.includes(field.name);
}

/**
 * The `PUT /api/commerce/settings` body: CONFIGURATION only. No secret field
 * is ever sent there (the backend refuses one); they go to the credentials
 * endpoint (`buildCommerceSecretUpdate`). The location mapping is not sent
 * either — the backend keeps it for the same platform.
 */
export function buildCommercePayload(
	provider: CommerceProvider,
	values: Record<string, string>
): Record<string, unknown> {
	const body: Record<string, unknown> = { provider: provider.key };
	for (const f of provider.fields) {
		if (!f.secret) body[f.name] = (values[f.name] ?? '').trim();
	}
	return body;
}

/**
 * The `PUT /api/organization/credentials/commerce` body for `provider`'s secret
 * fields, or `null` when the save changes none. A typed value is set; a blank
 * one keeps what is stored, unless the admin removed it (its `clear` toggle).
 */
export function buildCommerceSecretUpdate(
	provider: CommerceProvider,
	values: Record<string, string>,
	clear: Readonly<Record<string, boolean>> = {}
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
 * has typed. A blank secret is left out; the backend fills it from the sealed
 * store only while the form names the saved platform at the saved destination
 * (`provider_credentials.config_for_connection_test`).
 */
export function buildCommerceTestPayload(
	provider: CommerceProvider,
	values: Record<string, string>
): Record<string, unknown> {
	const body = buildCommercePayload(provider, values);
	for (const f of provider.fields) {
		const typed = (values[f.name] ?? '').trim();
		if (f.secret && typed) body[f.name] = typed;
	}
	return body;
}

/** Required fields still empty (a kept sealed secret counts as filled). */
export function missingCommerceFields(
	provider: CommerceProvider,
	values: Record<string, string>,
	stored: StoredCommerceConfig | null | undefined,
	storedNames: readonly string[],
	clear: Readonly<Record<string, boolean>> = {}
): CommerceProviderField[] {
	return provider.fields.filter(
		(f) =>
			f.required &&
			!(values[f.name] ?? '').trim() &&
			!(commerceSecretIsSaved(provider, f, stored, storedNames, values) && clear[f.name] !== true)
	);
}
