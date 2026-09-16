import { portalApi } from '$lib/portalApi';
import {
	EMPTY_BRAND,
	DEFAULT_PRODUCT_NAME,
	brandThemeVars,
	brandMark,
	type Brand,
	type BrandMarkChoice
} from '$lib/stores/brandTheme';

/**
 * Per-tenant white-label branding for the **supplier portal**
 * (`/portal/*`) — the portal counterpart of the employee-app
 * {@link import('$lib/stores/brand.svelte').brand} store.
 *
 * Why a separate store: the employee store reads `GET /api/organization/branding`
 * over the authenticated `api` client (JWT-gated, employee `User`). The portal
 * login page is **unauthenticated** and portal users are `VendorUser` — a
 * different identity — so it reads the **public-by-design** `GET
 * /api/portal/branding` over `portalApi` instead. That endpoint resolves the
 * tenant by the same `X-Tenant-Slug`/Host resolver and returns ONLY the
 * non-sensitive `BrandConfig` fields. The pure theming helpers
 * (`brandThemeVars`, etc.) are shared with the employee store via `brandTheme.ts`.
 *
 * Every field is optional; an empty field means "use the platform default" (the
 * bundled mark + "FeohLedger" + the AA-passing `app.css` accent tokens).
 * Resilient by design: any fetch failure degrades to defaults rather than
 * breaking a render, so the portal always themes.
 */

class PortalBrandStore {
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

	/** Which mark the portal shows: the logo, the platform mark, or a monogram. */
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
	 * Lazy-load the tenant branding once per portal session from the public
	 * `GET /api/portal/branding`. Safe to call from a layout `$effect`;
	 * concurrent callers share one in-flight request. Never throws.
	 */
	async ensureLoaded(): Promise<void> {
		if (this.#loaded) return;
		if (this.#inflight) return this.#inflight;
		this.#inflight = (async () => {
			try {
				const data = await portalApi.get<Partial<Brand>>('/api/portal/branding');
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
	 * Apply the configured accent colors to `document.documentElement` (and clear
	 * any previously-applied override that's no longer set). No-op on the server.
	 * Only touches the two accent tokens — never re-declares the rest of the theme.
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

export const portalBrand = new PortalBrandStore();
