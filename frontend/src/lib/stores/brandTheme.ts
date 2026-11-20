/**
 * Pure white-label theming helpers, kept in a plain `.ts` module (no Svelte
 * runes) so they're unit-testable under the runtime-free vitest config — the
 * rune store in `brand.svelte.ts` re-exports them. Mirrors the backend
 * `BrandConfig` validators.
 */

import { contrastRatio, parseColor, WCAG_AA_NORMAL, type Rgb } from '#lib/a11y/contrast.ts';

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

/** The two palettes in `src/app.css`. */
export type ThemeName = 'dark' | 'light';
export const THEME_NAMES: readonly ThemeName[] = ['dark', 'light'];

/**
 * The surfaces `--accent` renders on as small text (links, the active tab,
 * armed row actions), per palette — the same two the platform's own accent is
 * held to by `lib/a11y/tokenPairing.test.ts` (`--surface-2` is a fill, not a
 * text surface). Copied from `src/app.css`, and `brandTheme.test.ts` holds the
 * two equal, so a palette change cannot leave the derivation measuring an old
 * colour.
 */
export const ACCENT_TEXT_SURFACES: Record<ThemeName, Record<'--bg' | '--surface', string>> = {
	dark: { '--bg': '#0f1117', '--surface': '#181a23' },
	light: { '--bg': '#f4f5f8', '--surface': '#ffffff' }
};

/**
 * The inline custom properties a brand writes on `<html>`. `--accent` itself
 * is never written inline: an inline value beats both palettes, which is how a
 * colour picked for one theme ended up as text on the other. Instead each
 * palette in `src/app.css` reads its own one —
 * `--accent: var(--brand-accent-dark, …)` in the dark block,
 * `var(--brand-accent-light, …)` in the light ones (docs/decisions.md §477).
 * `--accent-strong` keeps one value: its contract (white text on it) does not
 * depend on the theme.
 */
export const BRAND_ACCENT_PROPS = [
	'--brand-accent-dark',
	'--brand-accent-light',
	'--accent-strong'
] as const;
export type BrandAccentProp = (typeof BRAND_ACCENT_PROPS)[number];

const THEME_PROP: Record<ThemeName, BrandAccentProp> = {
	dark: '--brand-accent-dark',
	light: '--brand-accent-light'
};

function toHex({ r, g, b }: Rgb): string {
	return '#' + [r, g, b].map((n) => n.toString(16).padStart(2, '0')).join('');
}

export type TextSurface = '--bg' | '--surface';

/** The lower of a colour's two text ratios on a palette — the binding one. */
function worstTextContrast(color: Rgb, theme: ThemeName): { ratio: number; surface: TextSurface } {
	let worst: { ratio: number; surface: TextSurface } | null = null;
	for (const surface of ['--bg', '--surface'] as const) {
		const ratio = contrastRatio(color, ACCENT_TEXT_SURFACES[theme][surface]) as number;
		if (worst === null || ratio < worst.ratio) worst = { ratio, surface };
	}
	return worst as { ratio: number; surface: TextSurface };
}

/**
 * Mix a colour toward black (`light` theme: darken) or white (`dark` theme:
 * lighten) by `t` ∈ [0, 1]. A linear mix with black or white scales every
 * channel's distance from the target by the same factor, so the order and
 * proportions of the channels — the hue — are kept; only lightness (and the
 * HSL saturation that rides on it) moves.
 */
function mixToward(color: Rgb, theme: ThemeName, t: number): Rgb {
	const target = theme === 'light' ? 0 : 255;
	const ch = (c: number) => Math.round(c + (target - c) * t);
	return { r: ch(color.r), g: ch(color.g), b: ch(color.b) };
}

/**
 * The colour a brand accent is shown as in one theme: the tenant's own hex if
 * it already reads as text there (≥ 4.5:1 on `--bg` and `--surface`),
 * otherwise the least-changed shade of it that does — darkened for Light,
 * lightened for Dark. `null` for a value that is not a usable hex.
 *
 * No new tenant setting (docs/decisions.md §477): the tenant picks one brand
 * colour, and the app keeps it legible in whichever theme the reader chose.
 * Darkening on a light page (or lightening on a dark one) only ever raises the
 * ratio once the colour is past the surface's own lightness, so the smallest
 * passing mix is found by bisection, measured after rounding to bytes so the
 * returned hex is the one that passes. The end points (pure black on Light,
 * white on Dark) clear 4.5:1 on every surface, so there is always an answer.
 */
export function themeAccent(color: string | null | undefined, theme: ThemeName): string | null {
	if (!isValidHexColor(color)) return null;
	const trimmed = (color as string).trim();
	const rgb = parseColor(trimmed) as Rgb;
	const passes = (c: Rgb) => worstTextContrast(c, theme).ratio >= WCAG_AA_NORMAL;
	if (passes(rgb)) return trimmed;
	let lo = 0;
	let hi = 1;
	for (let i = 0; i < 24; i++) {
		const mid = (lo + hi) / 2;
		if (passes(mixToward(rgb, theme, mid))) hi = mid;
		else lo = mid;
	}
	return toHex(mixToward(rgb, theme, hi));
}

/**
 * The CSS custom-property overrides to apply for a brand — only the keys the
 * org configured with a *valid* color. An unset or malformed color is omitted
 * so the `src/app.css` default token stands. `accent_color` becomes one value
 * per theme ({@link themeAccent}); see {@link BRAND_ACCENT_PROPS}.
 */
export function brandThemeVars(brand: Brand): Partial<Record<BrandAccentProp, string>> {
	const vars: Partial<Record<BrandAccentProp, string>> = {};
	for (const theme of THEME_NAMES) {
		const value = themeAccent(brand.accent_color, theme);
		if (value) vars[THEME_PROP[theme]] = value;
	}
	if (isValidHexColor(brand.accent_strong_color))
		vars['--accent-strong'] = brand.accent_strong_color.trim();
	return vars;
}

/** One theme's verdict on a candidate `accent_color` as text. */
export interface AccentTextVerdict {
	/** The tenant's own colour on that palette's binding surface. */
	ratio: number;
	surface: TextSurface;
	meetsAA: boolean;
	/** What the app shows instead: the colour itself when it passes. */
	applied: string;
	appliedRatio: number;
}

/**
 * The advisory for `accent_color` as TEXT, against both themes' surfaces, or
 * `null` while the field holds no usable hex. A colour below 4.5:1 in a theme
 * is not refused — {@link themeAccent} shows an adjusted shade there — but the
 * tenant is told, with the shade, so the change to their brand is not silent.
 */
export function accentTextContrast(
	color: string | null | undefined
): Record<ThemeName, AccentTextVerdict> | null {
	if (!isValidHexColor(color)) return null;
	const rgb = parseColor((color as string).trim()) as Rgb;
	const out = {} as Record<ThemeName, AccentTextVerdict>;
	for (const theme of THEME_NAMES) {
		const { ratio, surface } = worstTextContrast(rgb, theme);
		const applied = themeAccent(color, theme) as string;
		out[theme] = {
			ratio,
			surface,
			meetsAA: ratio >= WCAG_AA_NORMAL,
			applied,
			appliedRatio: worstTextContrast(parseColor(applied) as Rgb, theme).ratio
		};
	}
	return out;
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
