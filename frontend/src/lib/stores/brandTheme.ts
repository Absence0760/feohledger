/**
 * Pure white-label theming helpers, kept in a plain `.ts` module (no Svelte
 * runes) so they're unit-testable under the runtime-free vitest config — the
 * rune store in `brand.svelte.ts` re-exports them. Mirrors the backend
 * `BrandConfig` validators.
 */

import { contrastRatio, WCAG_AA_NORMAL } from '$lib/a11y/contrast';

export interface Brand {
	product_name: string;
	logo_url: string;
	accent_color: string;
	accent_strong_color: string;
	support_url: string;
	legal_url: string;
}

export const EMPTY_BRAND: Brand = {
	product_name: '',
	logo_url: '',
	accent_color: '',
	accent_strong_color: '',
	support_url: '',
	legal_url: ''
};

export const DEFAULT_PRODUCT_NAME = 'FeohLedger';

/**
 * Which mark the app chrome shows for a brand.
 *
 * - `logo`: the tenant configured a logo URL.
 * - `platform`: no logo, and the product still carries the platform's name, so
 *   the FeohLedger mark (`static/logo-mark.svg`) is the honest choice.
 * - `monogram`: the tenant renamed the product but set no logo. The platform
 *   mark would put FeohLedger's identity beside someone else's product name,
 *   so show the first character of their name instead, the neutral role the
 *   old "AP" placeholder played (docs/decisions.md §173).
 */
export type BrandMarkChoice =
	| { kind: 'logo'; src: string }
	| { kind: 'platform' }
	| { kind: 'monogram'; letter: string };

export function brandMark(brand: Brand): BrandMarkChoice {
	const src = brand.logo_url?.trim();
	if (src) return { kind: 'logo', src };
	const name = brand.product_name?.trim();
	if (!name || name === DEFAULT_PRODUCT_NAME) return { kind: 'platform' };
	return { kind: 'monogram', letter: firstGrapheme(name).toLocaleUpperCase() };
}

/** First user-perceived character, so a combining accent or an emoji isn't split. */
function firstGrapheme(text: string): string {
	const first = new Intl.Segmenter(undefined, { granularity: 'grapheme' })
		.segment(text)
		[Symbol.iterator]()
		.next();
	return first.done ? '' : first.value.segment;
}

/**
 * Strict guard mirroring the backend — a 3- or 6-digit hex literal. The value
 * is written into a CSS custom property, so anything that isn't a plain color
 * is rejected to keep the cascade clean.
 */
export function isValidHexColor(value: string | null | undefined): boolean {
	return typeof value === 'string' && /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(value.trim());
}

/**
 * The CSS custom-property overrides to apply for a brand — only the keys the
 * org configured with a *valid* color. An unset or malformed color is omitted
 * so the `src/app.css` default token stands.
 */
export function brandThemeVars(brand: Brand): Record<string, string> {
	const vars: Record<string, string> = {};
	if (isValidHexColor(brand.accent_color)) vars['--accent'] = brand.accent_color.trim();
	if (isValidHexColor(brand.accent_strong_color))
		vars['--accent-strong'] = brand.accent_strong_color.trim();
	return vars;
}

/**
 * The white-on-colour contrast ratio a candidate `accent_strong_color` would
 * produce, or `null` when the value isn't a usable hex yet (empty field,
 * half-typed).
 *
 * `--accent-strong` has exactly one contract — white text sits on it — and
 * `brandThemeVars` writes whatever hex the tenant types straight into that
 * custom property. So a tenant picking `#ffe066` because it matches their
 * logo turns every primary button, active filter chip and skip link in the
 * app into near-invisible white-on-yellow. The static token-pairing guard
 * (`lib/a11y/tokenPairing.test.ts`) can't see that: it scans the stylesheets,
 * and this override happens at runtime.
 *
 * Advisory, not a block. A brand colour is the tenant's call and the backend
 * accepts any valid hex; what was missing is anyone telling them the cost
 * before they save.
 */
export function accentStrongContrast(color: string | null | undefined): number | null {
	if (!isValidHexColor(color)) return null;
	return contrastRatio('#ffffff', (color as string).trim());
}

/**
 * `true` / `false` for a usable hex, `null` when there's nothing to judge yet.
 * Never collapses "not a colour" into "fails" — a half-typed field must not
 * flash a warning.
 */
export function accentStrongMeetsAA(color: string | null | undefined): boolean | null {
	const ratio = accentStrongContrast(color);
	return ratio === null ? null : ratio >= WCAG_AA_NORMAL;
}
