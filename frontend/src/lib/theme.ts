/**
 * Colour theme — System / Light / Dark (docs/decisions.md §289).
 *
 * Pure and framework-free (no `$app/*`, no runes) so vitest can reach every
 * branch; `stores/theme.svelte.ts` is the thin reactive wrapper the app uses.
 *
 * How a theme reaches the page:
 *
 *  1. The inline script in `src/app.html` reads the stored choice and sets
 *     `<html data-theme="system|light|dark">` BEFORE first paint, so a visitor
 *     who chose Light never sees a dark frame. It cannot import this module (it
 *     runs before any bundle), so it repeats three facts from here — the
 *     storage key, the accepted values and the two `theme-color` values — and
 *     `theme.test.ts` executes that script to hold it to them.
 *  2. `src/app.css` does the rest in CSS: the bare `:root` palette is dark,
 *     `:root[data-theme='light']` is light, and System is the media query
 *     `prefers-color-scheme: light` on `:root:not([data-theme='dark'])`. So the
 *     OS switching theme while the page is open needs no script at all.
 *  3. This module only resolves which of the two palettes is SHOWING, for the
 *     few consumers outside CSS — the `<meta name="theme-color">` and a
 *     third-party widget that takes a theme argument (hCaptcha).
 *
 * The choice is per browser, in `localStorage`, like the display language: a
 * preference about this screen, not about the account. Every storage access is
 * wrapped, because `localStorage` throws in some private modes and when site
 * data is blocked — and the page must render correctly when it does.
 */

export type ThemeChoice = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

/** In the order the picker shows them. */
export const THEME_CHOICES: readonly ThemeChoice[] = ['system', 'light', 'dark'];

/** Repeated in `src/app.html`'s inline script — `theme.test.ts` holds them equal. */
export const THEME_STORAGE_KEY = 'feoh_theme';

/**
 * `<meta name="theme-color">` per palette — the mobile browser's address bar.
 * Equal to each palette's `--bg` in `app.css`; `theme.test.ts` holds them so.
 */
export const THEME_COLORS: Record<ResolvedTheme, string> = {
	dark: '#0f1117',
	light: '#f4f5f8'
};

/** The query System follows. The same one `app.css` uses. */
export const PREFERS_LIGHT_QUERY = '(prefers-color-scheme: light)';

export function isThemeChoice(value: unknown): value is ThemeChoice {
	return value === 'system' || value === 'light' || value === 'dark';
}

/**
 * `window.localStorage`, or `null` where touching it throws (some private
 * modes, blocked site data, a sandboxed frame). Reading the property is itself
 * what throws, so it cannot be done outside a `try`.
 */
export function browserStorage(): Storage | null {
	try {
		return typeof window === 'undefined' ? null : window.localStorage;
	} catch {
		return null;
	}
}

/** The stored choice, or System when there is none, it is garbage, or storage throws. */
export function readThemeChoice(storage: Pick<Storage, 'getItem'> | null): ThemeChoice {
	if (!storage) return 'system';
	try {
		const stored = storage.getItem(THEME_STORAGE_KEY);
		return isThemeChoice(stored) ? stored : 'system';
	} catch {
		return 'system';
	}
}

/**
 * Persist a choice. System is stored as the ABSENCE of a choice, so a browser
 * that never touched the picker and one that went back to System behave
 * identically. Returns whether it was saved; a failure is not an error — the
 * theme still applies for this page view, it just won't survive a reload.
 */
export function writeThemeChoice(
	storage: Pick<Storage, 'setItem' | 'removeItem'> | null,
	choice: ThemeChoice
): boolean {
	if (!storage) return false;
	try {
		if (choice === 'system') storage.removeItem(THEME_STORAGE_KEY);
		else storage.setItem(THEME_STORAGE_KEY, choice);
		return true;
	} catch {
		return false;
	}
}

/**
 * Which palette is showing. System follows the OS, and — like `app.css` — an
 * OS that states no preference gets dark, the palette the app was designed in.
 */
export function resolveTheme(choice: ThemeChoice, prefersLight: boolean): ResolvedTheme {
	if (choice === 'system') return prefersLight ? 'light' : 'dark';
	return choice;
}

/** Whether the OS currently asks for light. `false` where `matchMedia` is absent. */
export function systemPrefersLight(win: Pick<Window, 'matchMedia'> | undefined): boolean {
	try {
		return !!win?.matchMedia?.(PREFERS_LIGHT_QUERY).matches;
	} catch {
		return false;
	}
}

/**
 * Put a choice on the document: the `data-theme` attribute the stylesheet
 * keys on, and the browser-chrome colour for the palette that resolves.
 */
export function applyTheme(doc: Document, choice: ThemeChoice, resolved: ResolvedTheme): void {
	doc.documentElement.setAttribute('data-theme', choice);
	doc.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME_COLORS[resolved]);
}
