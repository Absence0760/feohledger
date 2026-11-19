/**
 * Per-workspace cache of the two applied white-label accent values, so a
 * branded tenant's first frame after a reload paints in its own colour rather
 * than the platform blue for a round trip (docs/white-label.md § Accent before
 * first paint, docs/decisions.md §463).
 *
 * Runtime-free on purpose: `brand.svelte.ts` reads and writes it, and
 * `src/app.html`'s `<script id="accent-boot">` repeats the read (it runs before
 * any bundle, so it cannot import this). `brandAccentCache.test.ts` executes
 * that script against the same entries to hold the two equal.
 *
 * - Keyed by `getTenantStorageKey()` (`<prefix><slug>` on a platform host,
 *   `<prefix><hostname>` on a vanity host), in this origin's `localStorage` —
 *   never the shared theme cookie, which every workspace of the platform sees.
 * - Every value is re-validated as a hex colour on READ, not only on write:
 *   storage is writable by anything running on the origin, and the value goes
 *   straight into a CSS custom property.
 * - Every storage access is wrapped: a private mode or blocked site data
 *   degrades to "no cache", which is the old behaviour.
 */
import { isValidHexColor } from '#lib/stores/brandTheme.ts';

export const ACCENT_CACHE_PREFIX = 'feoh_brand_accent:';

/** The two custom properties the cache may set — nothing else, ever. */
export const ACCENT_PROPS = ['--accent', '--accent-strong'] as const;

type AccentVars = Partial<Record<(typeof ACCENT_PROPS)[number], string>>;

/** The minimal storage surface used, so tests can pass a fake. */
export interface AccentStorage {
	getItem(key: string): string | null;
	setItem(key: string, value: string): void;
	removeItem(key: string): void;
}

export function accentCacheKey(tenantKey: string): string {
	return ACCENT_CACHE_PREFIX + tenantKey;
}

/** Keep only the two accent props, and only when each is a valid hex colour. */
export function sanitizeAccentVars(raw: unknown): AccentVars {
	const vars: AccentVars = {};
	if (!raw || typeof raw !== 'object') return vars;
	for (const prop of ACCENT_PROPS) {
		const value = (raw as Record<string, unknown>)[prop];
		if (typeof value === 'string' && isValidHexColor(value)) vars[prop] = value.trim();
	}
	return vars;
}

/** The cached, validated accent vars for a workspace; `{}` when none or unreadable. */
export function readAccentCache(
	storage: AccentStorage | null | undefined,
	tenantKey: string | null
): AccentVars {
	if (!storage || !tenantKey) return {};
	try {
		const raw = storage.getItem(accentCacheKey(tenantKey));
		return raw ? sanitizeAccentVars(JSON.parse(raw)) : {};
	} catch {
		return {};
	}
}

/**
 * Record what was just applied. An org with no valid accent removes the entry,
 * so an unbranded workspace stores nothing at all.
 */
export function writeAccentCache(
	storage: AccentStorage | null | undefined,
	tenantKey: string | null,
	vars: Record<string, string>
): void {
	if (!storage || !tenantKey) return;
	const clean = sanitizeAccentVars(vars);
	try {
		if (Object.keys(clean).length === 0) storage.removeItem(accentCacheKey(tenantKey));
		else storage.setItem(accentCacheKey(tenantKey), JSON.stringify(clean));
	} catch {
		// Quota / blocked storage: the next cold load falls back to the default.
	}
}

export function clearAccentCache(
	storage: AccentStorage | null | undefined,
	tenantKey: string | null
): void {
	if (!storage || !tenantKey) return;
	try {
		storage.removeItem(accentCacheKey(tenantKey));
	} catch {
		// Nothing to undo.
	}
}

/** `window.localStorage`, or `null` where it is absent or the access throws. */
export function browserStorage(): AccentStorage | null {
	try {
		return typeof window === 'undefined' ? null : window.localStorage;
	} catch {
		return null;
	}
}
