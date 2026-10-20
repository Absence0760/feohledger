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
 * Where the choice lives (docs/decisions.md §290). It is a preference about
 * this screen, not about the account, so it stays in the browser — but every
 * workspace is its own origin (`<slug>.feohledger.com`, and the apex for
 * signup), and `localStorage` is per origin. So on a platform host the choice
 * is a cookie on the platform's registrable domain (`Domain=feohledger.com`,
 * derived from the host by `#lib/hostRouting.ts::sharedCookieDomainForHost`),
 * which every workspace and the signup page read alike. Where no such domain
 * exists — localhost, an IP literal, a customer's vanity host — or the browser
 * refuses the cookie, it falls back to `localStorage`, per origin. A cookie,
 * when present, wins: it is the newest choice made anywhere on the platform.
 *
 * Every cookie and storage access is wrapped, because both throw in some
 * private modes and when site data is blocked — and the page must render
 * correctly when they do.
 */

export type ThemeChoice = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

/** In the order the picker shows them. */
export const THEME_CHOICES: readonly ThemeChoice[] = ['system', 'light', 'dark'];

/**
 * The `localStorage` key AND the cookie name. Repeated in `src/app.html`'s
 * inline script — `theme.test.ts` holds them equal.
 */
export const THEME_STORAGE_KEY = 'feoh_theme';

/** How long the shared cookie lives: a year, renewed by every choice. */
export const THEME_COOKIE_MAX_AGE = 60 * 60 * 24 * 365;

/** The slice of `Document` the cookie helpers need, so tests can fake it. */
export interface CookieJar {
	cookie: string;
}

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

/** `document.cookie`, or `''` where reading it throws (sandboxed frames, blocked site data). */
export function browserCookies(): string {
	try {
		return typeof document === 'undefined' ? '' : document.cookie;
	} catch {
		return '';
	}
}

/**
 * The theme carried by a `document.cookie` string, or `null` when it carries
 * none. The first VALID `feoh_theme` wins, so a malformed one never hides a
 * good one. `src/app.html` repeats this parse.
 */
export function readThemeCookie(cookies: string | null | undefined): ThemeChoice | null {
	if (!cookies) return null;
	for (const part of cookies.split(';')) {
		const eq = part.indexOf('=');
		if (eq < 0 || part.slice(0, eq).trim() !== THEME_STORAGE_KEY) continue;
		const value = part.slice(eq + 1).trim();
		if (isThemeChoice(value)) return value;
	}
	return null;
}

/**
 * The choice to show: the shared cookie when there is one, else the per-origin
 * stored choice, else System — also when the value is garbage or storage throws.
 */
export function readThemeChoice(
	storage: Pick<Storage, 'getItem'> | null,
	cookies: string | null = null
): ThemeChoice {
	const shared = readThemeCookie(cookies);
	if (shared) return shared;
	if (!storage) return 'system';
	try {
		const stored = storage.getItem(THEME_STORAGE_KEY);
		return isThemeChoice(stored) ? stored : 'system';
	} catch {
		return 'system';
	}
}

/** The `document.cookie` assignment that shares `choice` across `domain`. */
export function themeCookie(choice: ThemeChoice, domain: string, secure: boolean): string {
	return (
		`${THEME_STORAGE_KEY}=${choice}; Domain=${domain}; Path=/; ` +
		`Max-Age=${THEME_COOKIE_MAX_AGE}; SameSite=Lax${secure ? '; Secure' : ''}`
	);
}

/** Where a shared choice goes: the cookie jar, the platform domain (or `null`
 *  where there is none) and whether the page is on https. */
export interface SharedThemeTarget {
	jar: CookieJar;
	domain: string | null;
	secure: boolean;
}

/**
 * Persist a choice. Returns whether it was saved; a failure is not an error —
 * the theme still applies for this page view, it just won't survive a reload.
 *
 * With a platform domain, the choice goes into the shared cookie, System
 * included: System is a choice there, written as `system`, so it overrides a
 * stale Light another workspace remembers per origin. The write is read back,
 * because a browser blocking cookies drops it silently; only once it has
 * landed is the per-origin copy removed, so it can never resurface over a
 * newer choice made on another workspace.
 *
 * Without one (or when the cookie did not land), `localStorage` holds it, and
 * System is stored as the ABSENCE of a choice, so a browser that never touched
 * the picker and one that went back to System behave identically.
 */
export function writeThemeChoice(
	storage: Pick<Storage, 'setItem' | 'removeItem'> | null,
	choice: ThemeChoice,
	shared: SharedThemeTarget | null = null
): boolean {
	if (shared?.domain) {
		try {
			shared.jar.cookie = themeCookie(choice, shared.domain, shared.secure);
			if (readThemeCookie(shared.jar.cookie) === choice) {
				try {
					storage?.removeItem(THEME_STORAGE_KEY);
				} catch {
					/* the cookie wins on read anyway */
				}
				return true;
			}
		} catch {
			/* cookies refused — fall back to this origin's storage */
		}
	}
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
