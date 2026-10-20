import { describe, expect, it } from 'vitest';
import { parsePalette } from '#lib/a11y/cssAudit.ts';
import {
	applyTheme,
	isThemeChoice,
	readThemeChoice,
	resolveTheme,
	systemPrefersLight,
	THEME_CHOICES,
	THEME_COLORS,
	THEME_STORAGE_KEY,
	writeThemeChoice,
	type ThemeChoice
} from './theme';

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

/** Just enough of a Document for `applyTheme` and the inline script. */
function fakeDocument() {
	const attrs = new Map<string, string>();
	const meta = { content: '#000000', setAttribute: (_: string, v: string) => (meta.content = v) };
	return {
		attrs,
		meta,
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
 * The inline script runs before any bundle, so it cannot import the module
 * above; it repeats its facts instead. Execute it against fakes for every
 * branch, and compare what it does with what `theme.ts` would do — that is
 * what keeps the repetition honest.
 */
describe('app.html pre-paint script', () => {
	const scripts = [...appHtml.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
	const script = scripts.find((s) => s.includes('data-theme'));

	it('exists, and sits after the theme-color meta it writes to', () => {
		expect(script, 'src/app.html must carry the inline theme script').toBeDefined();
		expect(appHtml.indexOf('name="theme-color"')).toBeLessThan(appHtml.indexOf(script!));
		// Before %sveltekit.head%, so it runs ahead of every stylesheet and module.
		expect(appHtml.indexOf(script!)).toBeLessThan(appHtml.indexOf('%sveltekit.head%'));
	});

	function run(stored: string | null, prefersLight: boolean, opts: { storageThrows?: boolean; noMatchMedia?: boolean } = {}) {
		const doc = fakeDocument();
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
