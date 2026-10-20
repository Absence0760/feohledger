import { describe, expect, it } from 'vitest';
import { parsePalette } from '#lib/a11y/cssAudit.ts';
import {
	applyTheme,
	isThemeChoice,
	readThemeChoice,
	readThemeCookie,
	resolveTheme,
	systemPrefersLight,
	THEME_CHOICES,
	THEME_COLORS,
	THEME_COOKIE_MAX_AGE,
	THEME_STORAGE_KEY,
	themeCookie,
	writeThemeChoice,
	type ThemeChoice
} from './theme';
import { sharedCookieDomainForHost } from './hostRouting';

/**
 * The theme helper (docs/decisions.md §289) and the pre-paint script in
 * `src/app.html` that has to agree with it without being able to import it.
 */

const RAW = import.meta.glob(['/src/app.html', '/src/app.css'], {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;
const appHtml = RAW['/src/app.html'];
const appCss = RAW['/src/app.css'];

/** A Map-backed Storage, optionally one whose every call throws. */
function memoryStorage(initial: Record<string, string> = {}, throws = false) {
	const data = new Map(Object.entries(initial));
	const guard = () => {
		if (throws) throw new DOMException('denied', 'SecurityError');
	};
	return {
		data,
		getItem: (k: string) => (guard(), data.get(k) ?? null),
		setItem: (k: string, v: string) => (guard(), void data.set(k, v)),
		removeItem: (k: string) => (guard(), void data.delete(k))
	};
}

/**
 * A `document.cookie` with a browser's shape: assigning one `name=value; attrs`
 * string sets (or, at `Max-Age=0`, deletes) that one cookie, and reading
 * returns `name=value; name=value`. `blocked` drops writes silently, as a
 * browser refusing cookies does; `throws` throws on every access.
 */
function cookieJar(initial = '', opts: { blocked?: boolean; throws?: boolean } = {}) {
	const jar = new Map<string, string>();
	const written: string[] = [];
	for (const part of initial.split(';')) {
		const eq = part.indexOf('=');
		if (eq > 0) jar.set(part.slice(0, eq).trim(), part.slice(eq + 1).trim());
	}
	return {
		written,
		get cookie() {
			if (opts.throws) throw new DOMException('denied', 'SecurityError');
			return [...jar].map(([k, v]) => `${k}=${v}`).join('; ');
		},
		set cookie(raw: string) {
			if (opts.throws) throw new DOMException('denied', 'SecurityError');
			written.push(raw);
			if (opts.blocked) return;
			const [pair, ...attrs] = raw.split(';');
			const eq = pair.indexOf('=');
			const name = pair.slice(0, eq).trim();
			if (attrs.some((a) => a.trim().toLowerCase() === 'max-age=0')) jar.delete(name);
			else jar.set(name, pair.slice(eq + 1).trim());
		}
	};
}

/** Just enough of a Document for `applyTheme` and the inline script. */
function fakeDocument(cookies = '', cookiesThrow = false) {
	const attrs = new Map<string, string>();
	const meta = { content: '#000000', setAttribute: (_: string, v: string) => (meta.content = v) };
	const jar = cookieJar(cookies, { throws: cookiesThrow });
	return {
		attrs,
		meta,
		get cookie() {
			return jar.cookie;
		},
		documentElement: { setAttribute: (k: string, v: string) => void attrs.set(k, v) },
		querySelector: (sel: string) => (sel === 'meta[name="theme-color"]' ? meta : null)
	};
}

describe('theme choice', () => {
	it('accepts exactly the three choices', () => {
		expect(THEME_CHOICES).toEqual(['system', 'light', 'dark']);
		for (const c of THEME_CHOICES) expect(isThemeChoice(c)).toBe(true);
		for (const bad of ['', 'Light', 'auto', null, undefined, 1]) expect(isThemeChoice(bad)).toBe(false);
	});

	it('resolves System from the OS, and an OS with no light preference to dark', () => {
		expect(resolveTheme('system', true)).toBe('light');
		expect(resolveTheme('system', false)).toBe('dark');
		// An explicit choice ignores the OS either way.
		for (const prefersLight of [true, false]) {
			expect(resolveTheme('light', prefersLight)).toBe('light');
			expect(resolveTheme('dark', prefersLight)).toBe('dark');
		}
	});

	it('reads a stored choice, and falls back to System on nothing, garbage, or a throwing store', () => {
		expect(readThemeChoice(memoryStorage({ [THEME_STORAGE_KEY]: 'light' }))).toBe('light');
		expect(readThemeChoice(memoryStorage({ [THEME_STORAGE_KEY]: 'dark' }))).toBe('dark');
		expect(readThemeChoice(memoryStorage())).toBe('system');
		expect(readThemeChoice(memoryStorage({ [THEME_STORAGE_KEY]: 'purple' }))).toBe('system');
		expect(readThemeChoice(memoryStorage({ [THEME_STORAGE_KEY]: 'light' }, true))).toBe('system');
		expect(readThemeChoice(null)).toBe('system');
	});

	it('stores Light/Dark, stores System as no entry, and reports a throwing store as unsaved', () => {
		const store = memoryStorage();
		expect(writeThemeChoice(store, 'light')).toBe(true);
		expect(store.data.get(THEME_STORAGE_KEY)).toBe('light');
		expect(writeThemeChoice(store, 'system')).toBe(true);
		expect(store.data.has(THEME_STORAGE_KEY)).toBe(false);

		expect(writeThemeChoice(memoryStorage({}, true), 'dark')).toBe(false);
		expect(writeThemeChoice(null, 'dark')).toBe(false);
	});

	it('reads the OS preference defensively', () => {
		const win = (matches: boolean) => ({ matchMedia: () => ({ matches }) }) as unknown as Window;
		expect(systemPrefersLight(win(true))).toBe(true);
		expect(systemPrefersLight(win(false))).toBe(false);
		expect(systemPrefersLight(undefined)).toBe(false);
		expect(
			systemPrefersLight({
				matchMedia: () => {
					throw new Error('nope');
				}
			} as unknown as Window)
		).toBe(false);
	});

	it('applies the choice as data-theme and the resolved palette as the theme-color', () => {
		const doc = fakeDocument();
		applyTheme(doc as unknown as Document, 'system', 'light');
		expect(doc.attrs.get('data-theme')).toBe('system');
		expect(doc.meta.content).toBe(THEME_COLORS.light);
		applyTheme(doc as unknown as Document, 'dark', 'dark');
		expect(doc.attrs.get('data-theme')).toBe('dark');
		expect(doc.meta.content).toBe(THEME_COLORS.dark);
	});

	it('keeps each theme-color equal to that palette’s --bg in app.css', () => {
		for (const t of ['dark', 'light'] as const) {
			expect(THEME_COLORS[t]).toBe(parsePalette(appCss, t)['--bg']);
		}
	});
});

/**
 * The shared cookie (docs/decisions.md §290): every workspace is its own
 * origin, so the choice made on one reaches the others — and the apex signup
 * page reaches the workspace it creates — only through a cookie on the
 * platform's registrable domain.
 */
describe('theme across workspaces — the shared cookie', () => {
	const PLATFORM = ['feohledger.com'];

	it('shares at the platform domain from the apex and from every workspace', () => {
		expect(sharedCookieDomainForHost('feohledger.com', PLATFORM)).toBe('feohledger.com');
		expect(sharedCookieDomainForHost('acme.feohledger.com', PLATFORM)).toBe('feohledger.com');
		expect(sharedCookieDomainForHost('ACME.feohledger.com.', PLATFORM)).toBe('feohledger.com');
		// The longest matching platform domain, as host classification uses.
		expect(sharedCookieDomainForHost('acme.app.example.com', ['example.com', 'app.example.com'])).toBe(
			'app.example.com'
		);
	});

	it('never shares on localhost, its subdomains, or an IP literal — those stay per origin', () => {
		for (const host of ['localhost', 'acme.localhost', 'acme.localhost:7777']) {
			expect(sharedCookieDomainForHost(host, ['localhost'])).toBeNull();
			expect(sharedCookieDomainForHost(host, [])).toBeNull();
		}
		expect(sharedCookieDomainForHost('127.0.0.1', ['127.0.0.1'])).toBeNull();
		expect(sharedCookieDomainForHost('[::1]', [])).toBeNull();
		expect(sharedCookieDomainForHost(null, PLATFORM)).toBeNull();
		expect(sharedCookieDomainForHost('', PLATFORM)).toBeNull();
	});

	it('never shares on a customer vanity domain — a platform cookie does not belong on their site', () => {
		expect(sharedCookieDomainForHost('ap.acmecorp.com', PLATFORM)).toBeNull();
		expect(sharedCookieDomainForHost('acmecorp.com', PLATFORM)).toBeNull();
		// A look-alike suffix is not a subdomain.
		expect(sharedCookieDomainForHost('evilfeohledger.com', PLATFORM)).toBeNull();
	});

	it('with no PUBLIC_PLATFORM_DOMAINS, shares only under the PUBLIC_SITE_URL host', () => {
		// Today's production shape: the platform domain list is unset, and the
		// site URL (from APP_URL) is the apex.
		const SITE = 'https://feohledger.com';
		expect(sharedCookieDomainForHost('feohledger.com', [], SITE)).toBe('feohledger.com');
		expect(sharedCookieDomainForHost('acme.feohledger.com', [], SITE)).toBe('feohledger.com');
		expect(sharedCookieDomainForHost('acme.feohledger.com', [], `${SITE}/`)).toBe('feohledger.com');
		// Both declared: either names our domain.
		expect(sharedCookieDomainForHost('acme.feohledger.com', ['localhost'], SITE)).toBe(
			'feohledger.com'
		);
	});

	it('never puts a Domain cookie on a custom domain’s foreign parent when only the legacy host rule is in play', () => {
		// The legacy rule reads `ap.customer.example` as workspace `ap` of
		// `customer.example`. That must never become our cookie's domain.
		for (const site of [null, undefined, '', 'https://feohledger.com', 'not a url', 'ftp://customer.example']) {
			for (const host of [
				'ap.customer.example',
				'customer.example',
				'ap.billing.customer.example',
				'ap.customer.co.uk',
				'feohledger.com.customer.example'
			]) {
				expect(sharedCookieDomainForHost(host, [], site), `${host} with site ${site}`).toBeNull();
			}
		}
		// A build that declares nothing shares nothing, even on our own hosts.
		expect(sharedCookieDomainForHost('acme.feohledger.com', [], null)).toBeNull();
		expect(sharedCookieDomainForHost('feohledger.com', [], '')).toBeNull();
	});

	it('the dev site URL (localhost) shares nothing', () => {
		expect(sharedCookieDomainForHost('acme.localhost', [], 'http://localhost:7777')).toBeNull();
		expect(sharedCookieDomainForHost('localhost', ['localhost'], 'http://localhost:7777')).toBeNull();
	});

	it('builds a Domain-scoped, year-long, SameSite=Lax cookie, Secure on https only', () => {
		expect(themeCookie('light', 'feohledger.com', true)).toBe(
			`feoh_theme=light; Domain=feohledger.com; Path=/; Max-Age=${THEME_COOKIE_MAX_AGE}; SameSite=Lax; Secure`
		);
		expect(themeCookie('dark', 'feohledger.com', false)).not.toContain('Secure');
		expect(THEME_COOKIE_MAX_AGE).toBe(31_536_000);
	});

	it('parses the first valid feoh_theme out of a cookie string', () => {
		expect(readThemeCookie('feoh_theme=light')).toBe('light');
		expect(readThemeCookie('a=1; feoh_theme=dark; b=2')).toBe('dark');
		expect(readThemeCookie('feoh_theme=system')).toBe('system');
		expect(readThemeCookie('feoh_theme=purple; feoh_theme=light')).toBe('light');
		expect(readThemeCookie('xfeoh_theme=light')).toBeNull();
		expect(readThemeCookie('')).toBeNull();
		expect(readThemeCookie(null)).toBeNull();
	});

	it('reads the cookie ahead of this origin’s storage — it is the newest choice made anywhere', () => {
		const store = memoryStorage({ [THEME_STORAGE_KEY]: 'light' });
		expect(readThemeChoice(store, 'feoh_theme=dark')).toBe('dark');
		// System written to the cookie on another workspace beats a stale Light here.
		expect(readThemeChoice(store, 'feoh_theme=system')).toBe('system');
		expect(readThemeChoice(store, 'feoh_theme=purple')).toBe('light');
		expect(readThemeChoice(store, '')).toBe('light');
	});

	it('writes a platform-domain cookie — System included — and drops the per-origin copy', () => {
		const store = memoryStorage({ [THEME_STORAGE_KEY]: 'dark' });
		const jar = cookieJar();
		const target = { jar, domain: 'feohledger.com', secure: true };

		expect(writeThemeChoice(store, 'light', target)).toBe(true);
		expect(jar.written.at(-1)).toBe(themeCookie('light', 'feohledger.com', true));
		expect(readThemeCookie(jar.cookie)).toBe('light');
		expect(store.data.has(THEME_STORAGE_KEY)).toBe(false);

		expect(writeThemeChoice(store, 'system', target)).toBe(true);
		expect(readThemeCookie(jar.cookie)).toBe('system');
		expect(store.data.has(THEME_STORAGE_KEY)).toBe(false);
	});

	it('the choice made on one workspace is what the next one reads', () => {
		const jar = cookieJar(); // one browser's cookies for *.feohledger.com
		const acme = memoryStorage({ [THEME_STORAGE_KEY]: 'dark' }); // a #607-era per-origin value
		const signup = memoryStorage();
		writeThemeChoice(signup, 'light', { jar, domain: 'feohledger.com', secure: true });
		expect(readThemeChoice(acme, jar.cookie)).toBe('light');
	});

	it('falls back to this origin’s storage where there is no platform domain', () => {
		const store = memoryStorage();
		const jar = cookieJar();
		expect(writeThemeChoice(store, 'dark', { jar, domain: null, secure: false })).toBe(true);
		expect(jar.written).toEqual([]);
		expect(store.data.get(THEME_STORAGE_KEY)).toBe('dark');
	});

	it('falls back to storage when the browser drops the cookie silently, or throws', () => {
		for (const opts of [{ blocked: true }, { throws: true }]) {
			const store = memoryStorage();
			const jar = cookieJar('', opts);
			expect(writeThemeChoice(store, 'light', { jar, domain: 'feohledger.com', secure: true })).toBe(
				true
			);
			expect(store.data.get(THEME_STORAGE_KEY)).toBe('light');
		}
	});
});

/**
 * The inline script runs before any bundle, so it cannot import the module
 * above; it repeats its facts instead. Execute it against fakes for every
 * branch, and compare what it does with what `theme.ts` would do — that is
 * what keeps the repetition honest.
 */
describe('app.html pre-paint script', () => {
	// Located by its exact, stable opening tag rather than by matching tags with
	// a regex: the script is ours and carries an id for exactly this.
	const OPEN = '<script id="theme-boot">';
	const start = appHtml.indexOf(OPEN);
	const end = start < 0 ? -1 : appHtml.indexOf('</script>', start + OPEN.length);
	const script = start < 0 || end < 0 ? undefined : appHtml.slice(start + OPEN.length, end);

	it('exists, and sits after the theme-color meta it writes to', () => {
		expect(script, 'src/app.html must carry <script id="theme-boot">').toBeDefined();
		expect(script).toContain('data-theme');
		expect(appHtml.indexOf('name="theme-color"')).toBeLessThan(appHtml.indexOf(script!));
		// Before %sveltekit.head%, so it runs ahead of every stylesheet and module.
		expect(appHtml.indexOf(script!)).toBeLessThan(appHtml.indexOf('%sveltekit.head%'));
	});

	function run(
		stored: string | null,
		prefersLight: boolean,
		opts: { storageThrows?: boolean; noMatchMedia?: boolean; cookies?: string; cookiesThrow?: boolean } = {}
	) {
		const doc = fakeDocument(opts.cookies ?? '', opts.cookiesThrow);
		const storage = memoryStorage(stored === null ? {} : { [THEME_STORAGE_KEY]: stored }, opts.storageThrows);
		const win: Record<string, unknown> = {};
		Object.defineProperty(win, 'localStorage', {
			get() {
				// Some browsers throw on the property access itself.
				if (opts.storageThrows) throw new DOMException('denied', 'SecurityError');
				return storage;
			}
		});
		if (!opts.noMatchMedia) win.matchMedia = (q: string) => ({ matches: q === '(prefers-color-scheme: light)' && prefersLight });
		new Function('window', 'document', script!)(win, doc);
		return doc;
	}

	const cases: [string | null, boolean][] = [];
	for (const stored of [null, 'system', 'light', 'dark', 'garbage']) {
		for (const prefersLight of [true, false]) cases.push([stored, prefersLight]);
	}

	for (const [stored, prefersLight] of cases) {
		it(`stored=${stored} prefersLight=${prefersLight} matches theme.ts`, () => {
			const doc = run(stored, prefersLight);
			const choice: ThemeChoice = readThemeChoice(memoryStorage(stored === null ? {} : { [THEME_STORAGE_KEY]: stored }));
			expect(doc.attrs.get('data-theme')).toBe(choice);
			expect(doc.meta.content).toBe(THEME_COLORS[resolveTheme(choice, prefersLight)]);
		});
	}

	// The shared cookie against every stored value: the cookie wins when it is
	// valid, and a missing or garbage cookie defers to this origin's storage.
	for (const cookies of ['', 'feoh_theme=light', 'other=1; feoh_theme=dark', 'feoh_theme=system', 'feoh_theme=purple; feoh_theme=light', 'feoh_theme=purple', 'xfeoh_theme=light']) {
		for (const stored of [null, 'light', 'dark']) {
			it(`cookie="${cookies}" stored=${stored} matches theme.ts`, () => {
				const doc = run(stored, true, { cookies });
				const choice = readThemeChoice(
					memoryStorage(stored === null ? {} : { [THEME_STORAGE_KEY]: stored }),
					cookies
				);
				expect(doc.attrs.get('data-theme')).toBe(choice);
				expect(doc.meta.content).toBe(THEME_COLORS[resolveTheme(choice, true)]);
			});
		}
	}

	it('still reads storage when reading the cookie throws', () => {
		const doc = run('light', false, { cookiesThrow: true });
		expect(doc.attrs.get('data-theme')).toBe('light');
	});

	it('follows the OS when storage throws — the page still gets a data-theme', () => {
		const light = run('dark', true, { storageThrows: true });
		expect(light.attrs.get('data-theme')).toBe('system');
		expect(light.meta.content).toBe(THEME_COLORS.light);
		const dark = run('light', false, { storageThrows: true });
		expect(dark.attrs.get('data-theme')).toBe('system');
		expect(dark.meta.content).toBe(THEME_COLORS.dark);
	});

	it('falls back to dark for System when matchMedia is missing, as app.css does', () => {
		const doc = run(null, true, { noMatchMedia: true });
		expect(doc.attrs.get('data-theme')).toBe('system');
		expect(doc.meta.content).toBe(THEME_COLORS.dark);
	});
});
