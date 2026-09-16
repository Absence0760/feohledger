import { api } from '$lib/api';
import {
	EMPTY_BRAND,
	DEFAULT_PRODUCT_NAME,
	brandThemeVars,
	brandMark,
	type Brand,
	type BrandMarkChoice
} from '$lib/stores/brandTheme';

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
 */

export type { Brand, BrandMarkChoice };
export { isValidHexColor, brandThemeVars, brandMark } from '$lib/stores/brandTheme';

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
	 * server. Only touches the two accent tokens — never re-declares the rest of
	 * the theme.
	 */
	applyTheme(): void {
		if (typeof document === 'undefined') return;
		const root = document.documentElement;
		const vars = brandThemeVars(this.brand);
		for (const prop of ['--accent', '--accent-strong']) {
			if (vars[prop]) root.style.setProperty(prop, vars[prop]);
			else root.style.removeProperty(prop);
		}
	}

	/** Convenience: load (if needed) then apply the theme. */
	async ensureLoadedAndApply(): Promise<void> {
		await this.ensureLoaded();
		this.applyTheme();
	}

	reset(): void {
		this.brand = { ...EMPTY_BRAND };
		this.#loaded = false;
		this.#inflight = null;
		if (typeof document !== 'undefined') {
			document.documentElement.style.removeProperty('--accent');
			document.documentElement.style.removeProperty('--accent-strong');
		}
	}
}

export const brand = new BrandStore();
