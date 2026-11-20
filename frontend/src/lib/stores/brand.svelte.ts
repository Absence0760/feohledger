import { api } from '#lib/api.ts';
import {
	EMPTY_BRAND,
	DEFAULT_PRODUCT_NAME,
	brandThemeVars,
	brandMark,
	type Brand,
	type BrandMarkChoice
} from '#lib/stores/brandTheme.ts';
import {
	ACCENT_PROPS,
	browserStorage,
	clearAccentCache,
	readAccentCache,
	writeAccentCache
} from '#lib/stores/brandAccentCache.ts';
import { getTenantStorageKey } from '#lib/tenant.ts';

/**
 * Per-tenant white-label branding (logo, product name, accent theme colors,
 * support / legal links). Lazy-loaded once per session from
 * `GET /api/organization/branding` — mirrors the {@link orgCurrency} store's
 * shape (cache + single in-flight request + resilient fallback).
 *
 * Every field is optional. An empty field means "use the platform default":
 * the FeohLedger mark + "FeohLedger" product name, and the AA-passing
 * accent tokens already in `src/app.css`. The custom accent colors are applied
 * by writing CSS custom properties on `document.documentElement` ONLY when the
 * org actually configured them (see {@link brandThemeVars}) — so an org that
 * sets nothing is byte-for-byte the default theme.
 *
 * Resilient by design: the read endpoint is open to any authed role, but any
 * failure (network, transient) degrades to defaults rather than breaking a
 * render. `reset()` clears it (logout / tenant switch).
 *
 * The pure theming helpers (`isValidHexColor`, `brandThemeVars`) live in the
 * runtime-free `brandTheme.ts` so they're unit-testable; re-exported here.
 *
 * The applied accent is cached per workspace (`brandAccentCache.ts`) so the
 * next cold load paints it before first paint (`app.html`'s accent-boot
 * script) and an in-app sign-in paints it before the read lands
 * ({@link BrandStore.ensureLoaded}); the fetched branding always corrects it.
 */

export type { Brand, BrandMarkChoice };
export { isValidHexColor, brandThemeVars, brandMark } from '#lib/stores/brandTheme.ts';

class BrandStore {
	brand = $state<Brand>({ ...EMPTY_BRAND });
	#loaded = false;
	#inflight: Promise<void> | null = null;

	/** Product name to render, with the platform default as the fallback. */
	get productName(): string {
		return this.brand.product_name?.trim() || DEFAULT_PRODUCT_NAME;
	}

	/** Configured logo URL, or '' when the org uses the bundled mark. */
	get logoUrl(): string {
		return this.brand.logo_url?.trim() || '';
	}

	/** Which mark the chrome shows: the logo, the platform mark, or a monogram. */
	get mark(): BrandMarkChoice {
		return brandMark(this.brand);
	}

	get supportUrl(): string {
		return this.brand.support_url?.trim() || '';
	}

	get legalUrl(): string {
		return this.brand.legal_url?.trim() || '';
	}

	/**
	 * Lazy-load the tenant branding once per session. Safe to call from any
	 * page/layout `$effect`; concurrent callers share one in-flight request.
	 */
	async ensureLoaded(): Promise<void> {
		if (this.#loaded) return;
		if (this.#inflight) return this.#inflight;
		this.#applyCachedAccent();
		this.#inflight = (async () => {
			try {
				const data = await api.get<Partial<Brand>>('/api/organization/branding');
				this.brand = { ...EMPTY_BRAND, ...(data ?? {}) };
				this.#loaded = true;
			} catch {
				// Keep defaults; don't mark loaded so a later call can retry.
			} finally {
				this.#inflight = null;
			}
		})();
		return this.#inflight;
	}

	/**
	 * Apply the configured accent colors to `document.documentElement` (and
	 * clear any previously-applied override that's no longer set). No-op on the
	 * server. Only touches the brand accent props (`BRAND_ACCENT_PROPS`: one
	 * accent per theme, plus the strong accent) — never `--accent` itself, which
	 * each palette in `app.css` reads from its own one (decisions §478).
	 */
	applyTheme(): void {
		if (typeof document === 'undefined') return;
		const root = document.documentElement;
		const vars = brandThemeVars(this.brand);
		for (const prop of ACCENT_PROPS) {
			if (vars[prop]) root.style.setProperty(prop, vars[prop]);
			else root.style.removeProperty(prop);
		}
		// Only fetched branding is authoritative enough to cache: before the
		// first successful read `this.brand` is the empty default, and writing
		// that would erase a good entry on a transient failure.
		if (this.#loaded) writeAccentCache(browserStorage(), getTenantStorageKey(), vars);
	}

	/**
	 * Paint the cached accent while the read is in flight — the in-app sign-in
	 * path, where no reload runs `app.html`'s pre-paint script. Validated on
	 * read; `applyTheme()` replaces it with the fetched values.
	 */
	#applyCachedAccent(): void {
		if (typeof document === 'undefined') return;
		const cached = readAccentCache(browserStorage(), getTenantStorageKey());
		for (const prop of ACCENT_PROPS) {
			const value = cached[prop];
			if (value) document.documentElement.style.setProperty(prop, value);
		}
	}

	/** Convenience: load (if needed) then apply the theme. */
	async ensureLoadedAndApply(): Promise<void> {
		await this.ensureLoaded();
		// A failed read leaves whatever the cache painted standing (the
		// tenant's last-known colour) rather than wiping it to the default.
		if (this.#loaded) this.applyTheme();
	}

	reset(): void {
		this.brand = { ...EMPTY_BRAND };
		this.#loaded = false;
		this.#inflight = null;
		clearAccentCache(browserStorage(), getTenantStorageKey());
		if (typeof document !== 'undefined') {
			for (const prop of ACCENT_PROPS) document.documentElement.style.removeProperty(prop);
		}
	}
}

export const brand = new BrandStore();
