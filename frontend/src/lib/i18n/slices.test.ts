import { describe, expect, it } from 'vitest';
import { en } from './locales/en';
import { EN_FALLBACK, SLICE_LOADERS, SLICE_NAMES, SLICE_PREFIXES, sliceOf } from './catalogues';
import { EN_SLICES } from './allMessages';
import type { SliceName } from './messages';

/**
 * The lazy catalogue slices (catalogues.ts, decisions §261 + §300) hold keys
 * that only some routes render. A key of a slice rendered anywhere its slice is
 * not loaded would show English in every other locale — silently, since `m()`
 * falls back rather than failing. This test makes that a failure instead.
 *
 * It reads the source tree, builds the import graph, and for every file that
 * names a key of slice S (a literal `'org.title'`, a `{ui:org.title}` reference
 * in help prose, or a template prefix like `` `org.section.${id}` ``) proves
 * that every way the app reaches that file loads S first:
 *
 *   - a route whose directory, or an ancestor's, has a `+layout.ts` calling
 *     `ensureSlices(…'S'…)` — SvelteKit awaits that load before rendering the
 *     route's layouts and page; or
 *   - a component that itself calls `ensureSlices(…'S'…)` as it initialises
 *     (the ⓘ HelpTip, which appears on every page and owns the help slice).
 *
 * Both lists are read off the code, so there is nothing here to keep in step
 * with it. A failure means one of three things: load the slice on that route,
 * move the key back to the main catalogue (shared code renders it), or — only
 * for an import that renders none of the imported module's keys — declare the
 * edge in NOT_RENDERED below.
 */

const SOURCES = import.meta.glob(['/src/**/*.ts', '/src/**/*.svelte', '!/src/**/*.test.ts', '!/src/lib/i18n/locales/**'], {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/**
 * Imports that reach a module without rendering any of its catalogue keys,
 * each with the reason. Every entry must still be a real import, so one cannot
 * outlive the code it excuses.
 */
const NOT_RENDERED: { from: string; to: string; why: string }[] = [
	{
		from: '/src/lib/components/help/HelpTip.svelte',
		to: '/src/lib/help/content.ts',
		why: 'A tip reads only guidesForTerm() — guide ids and their plain-English titles — never the guide bodies whose {ui:…} references name settings keys.'
	}
];

/**
 * Keys the BACKEND names: the ERP and commerce connection forms are built from
 * a provider catalogue the API serves, each field carrying its own `label_key`
 * / `help_key`, so the frontend file that renders them never spells the key.
 * The scan reads the backend adapter packages as if they were that file. The backend's
 * own tests (`test_erp_catalog.py`, `test_commerce_adapters.py`) prove each
 * named key exists in the English catalogue; this proves it is loaded where it
 * renders.
 */
const rawFiles = (files: Record<string, string>) => Object.values(files).join('\n');
const SERVER_NAMED: { source: string; text: string; renderedBy: string }[] = [
	{
		source: 'backend/app/services/erp_adapters/',
		text: rawFiles(
			import.meta.glob('../../../../backend/app/services/erp_adapters/*.py', { query: '?raw', import: 'default', eager: true })
		),
		renderedBy: '/src/lib/components/organization/ErpConnectionPanel.svelte'
	},
	{
		source: 'backend/app/services/commerce_adapters/',
		text: rawFiles(
			import.meta.glob('../../../../backend/app/services/commerce_adapters/*.py', { query: '?raw', import: 'default', eager: true })
		),
		renderedBy: '/src/lib/components/organization/CommerceConnectionPanel.svelte'
	}
];

const KEY_CHARS =/^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)+$/;

function resolveImport(from: string, spec: string): string | null {
	let base: string;
	if (spec.startsWith('#lib/')) base = `/src/lib/${spec.slice(5)}`;
	else if (spec.startsWith('./') || spec.startsWith('../')) {
		const parts = from.split('/').slice(0, -1);
		for (const seg of spec.split('/')) {
			if (seg === '..') parts.pop();
			else if (seg !== '.') parts.push(seg);
		}
		base = parts.join('/');
	} else return null;
	for (const c of [base, `${base}.ts`, `${base}.svelte`, `${base}/index.ts`]) if (c in SOURCES) return c;
	return null;
}

const IMPORTERS = new Map<string, Set<string>>();
const EDGES = new Set<string>();
for (const [file, text] of Object.entries(SOURCES)) {
	for (const m of text.matchAll(/(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)['"]([^'"]+)['"]/g)) {
		const target = resolveImport(file, m[1]);
		if (!target) continue;
		EDGES.add(`${file} -> ${target}`);
		if (NOT_RENDERED.some((e) => e.from === file && e.to === target)) continue;
		if (!IMPORTERS.has(target)) IMPORTERS.set(target, new Set());
		IMPORTERS.get(target)!.add(file);
	}
}

/** The slices an `ensureSlices(…)` call in this file asks for. */
function ensuredIn(text: string): SliceName[] {
	const out: SliceName[] = [];
	for (const call of text.matchAll(/\bensureSlices\(([^)]*)\)/g))
		for (const arg of call[1].matchAll(/['"]([A-Za-z]+)['"]/g)) out.push(arg[1] as SliceName);
	return out;
}

const ROUTES = '/src/routes';
const isRouteFile = (f: string) => f.startsWith(`${ROUTES}/`) && /\/\+(page|layout)\.(svelte|ts)$/.test(f);

/** Route directories whose `+layout.ts` loads each slice. */
const LOADING_DIRS = new Map<SliceName, string[]>();
/** Non-route files (components) that load a slice themselves. */
const OWNERS = new Map<SliceName, string[]>();
for (const [file, text] of Object.entries(SOURCES)) {
	if (file === '/src/lib/i18n/store.svelte.ts') continue;
	for (const s of ensuredIn(text)) {
		if (file.startsWith(`${ROUTES}/`) && file.endsWith('/+layout.ts')) {
			const dir = file.slice(0, -'/+layout.ts'.length);
			LOADING_DIRS.set(s, [...(LOADING_DIRS.get(s) ?? []), dir]);
		} else {
			OWNERS.set(s, [...(OWNERS.get(s) ?? []), file]);
		}
	}
}

/**
 * Why `file` is not covered for `slice` — the chain from an uncovered entry
 * point down to it — or null when every path that reaches it loads the slice.
 */
function uncoveredPath(file: string, slice: SliceName, seen = new Set<string>()): string[] | null {
	if ((OWNERS.get(slice) ?? []).includes(file)) return null;
	if (isRouteFile(file)) {
		const dir = file.slice(0, file.lastIndexOf('/'));
		const loaded = (LOADING_DIRS.get(slice) ?? []).some((d) => dir === d || dir.startsWith(`${d}/`));
		return loaded ? null : [file];
	}
	if (seen.has(file)) return null; // a cycle: judged on its other importers
	seen.add(file);
	const importers = IMPORTERS.get(file);
	if (!importers || importers.size === 0) return [`${file} (imported by nothing this test can see)`];
	for (const imp of importers) {
		const chain = uncoveredPath(imp, slice, seen);
		if (chain) return [...chain, file];
	}
	return null;
}

/** Exact key → slice, from the English slices (the runtime places keys by prefix). */
const SLICE_OF_KEY = new Map<string, SliceName>(
	SLICE_NAMES.flatMap((s) => Object.keys(EN_SLICES[s]).map((k) => [k, s] as [string, SliceName]))
);

/** Every slice key a source file names, by slice. */
function sliceKeysIn(text: string): Map<SliceName, string[]> {
	const out = new Map<SliceName, string[]>();
	const add = (key: string) => {
		const s = SLICE_OF_KEY.get(key);
		if (s) out.set(s, [...(out.get(s) ?? []), key]);
	};
	for (const m of text.matchAll(/[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)+/g)) add(m[0]);
	// A key assembled at runtime — `org.section.${id}` or 'org.section.' + id —
	// is taken to name every slice key under that prefix.
	// The Python f-string shape (`f"org.erp.field.{name}"`) is the same thing in
	// the backend catalogues SERVER_NAMED reads.
	const prefixes = [
		...[...text.matchAll(/([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*\.)\$\{/g)].map((m) => m[1]),
		...[...text.matchAll(/['"]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*\.)['"]\s*\+/g)].map((m) => m[1]),
		...[...text.matchAll(/\bf['"]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*\.)\{/g)].map((m) => m[1])
	];
	for (const p of prefixes)
		for (const s of SLICE_NAMES)
			for (const key of Object.keys(EN_SLICES[s])) if (key.startsWith(p)) add(key);
	return out;
}

const USES = [
	...Object.entries(SOURCES).map(([file, text]) => ({ file, keys: sliceKeysIn(text) })),
	...SERVER_NAMED.map(({ text, renderedBy }) => ({ file: renderedBy, keys: sliceKeysIn(text) }))
];

describe('catalogue slices', () => {
	it('are loaded somewhere, and every loader names a real slice', () => {
		for (const s of SLICE_NAMES) {
			expect((LOADING_DIRS.get(s) ?? []).length + (OWNERS.get(s) ?? []).length, `${s} is never loaded`).toBeGreaterThan(0);
		}
		for (const s of [...LOADING_DIRS.keys(), ...OWNERS.keys()]) expect(SLICE_NAMES, `ensureSlices('${s}')`).toContain(s);
	});

	it('hold only keys this test can find by name', () => {
		for (const s of SLICE_NAMES)
			for (const key of Object.keys(EN_SLICES[s])) expect(key, `${s}: ${key}`).toMatch(KEY_CHARS);
	});

	it('are placed by prefix: every key carries a prefix of its own slice, and no prefix is claimed twice', () => {
		// `m()` reads the main catalogue first and only then asks `sliceOf`, so a
		// main key under a slice prefix is fine; a slice key under the wrong
		// prefix would be looked up in the wrong slice and render raw.
		const prefixes = SLICE_NAMES.flatMap((s) => SLICE_PREFIXES[s]);
		for (const a of prefixes) for (const b of prefixes) if (a !== b) expect(a.startsWith(b), `${a} / ${b}`).toBe(false);
		for (const s of SLICE_NAMES)
			for (const key of Object.keys(EN_SLICES[s])) expect(sliceOf(key), `${key} is in ${s}`).toBe(s);
	});

	it('keep English out of the entry chunk except where a static fallback is needed', async () => {
		// The route-scoped slices are awaited by their route before it renders, so
		// their English is a lazy chunk like every other locale's; help's English
		// is the static fallback a HelpTip shows while its chunk loads.
		expect(Object.keys(EN_FALLBACK)).toEqual(['help']);
		for (const s of SLICE_NAMES) expect(await SLICE_LOADERS[s].en(), s).toEqual(EN_SLICES[s]);
	});

	it('are never imported statically by the app', () => {
		// One value import of a route-scoped slice (or of allMessages.ts, which
		// holds them all) would pull it back into a shared chunk, undoing the split
		// without failing anything else.
		const lazy = SLICE_NAMES.filter((s) => !(s in EN_FALLBACK));
		const offenders = Object.entries(SOURCES)
			.filter(([file]) => file !== '/src/lib/i18n/allMessages.ts')
			.flatMap(([file, text]) =>
				[...text.matchAll(/^\s*import\s+(?!type\b)[^;]*?from\s*['"]([^'"]+)['"]/gm)]
					.map((m) => m[1])
					.filter((spec) => /allMessages/.test(spec) || lazy.some((s) => spec.includes(`locales/${s}/`)))
					.map((spec) => `${file} imports ${spec}`)
			);
		expect(offenders).toEqual([]);
	});

	it('never share a key with each other or the main catalogue', () => {
		const seen = new Map<string, string>(Object.keys(en).map((k) => [k, 'main']));
		for (const s of SLICE_NAMES)
			for (const key of Object.keys(EN_SLICES[s])) {
				expect(seen.get(key), `${key} is in ${s} and ${seen.get(key)}`).toBeUndefined();
				seen.set(key, s);
			}
	});

	it('have every key referenced somewhere, so the route check below sees all of them', () => {
		// A key nothing names cannot be placed: either it is dead or something
		// builds it in a way the scan cannot follow (a computed namespace).
		const named = new Set(USES.flatMap((u) => [...u.keys.values()].flat()));
		for (const s of SLICE_NAMES) {
			const missing = Object.keys(EN_SLICES[s]).filter((k) => !named.has(k));
			expect(missing, `${s}: keys nothing in src/ names`).toEqual([]);
		}
	});

	it('declare NOT_RENDERED edges that still exist', () => {
		for (const e of NOT_RENDERED) expect(EDGES.has(`${e.from} -> ${e.to}`), `${e.from} -> ${e.to}`).toBe(true);
	});

	it('declare SERVER_NAMED sources that still name keys, read by a file that renders them', () => {
		for (const s of SERVER_NAMED) {
			expect(s.text.length, s.source).toBeGreaterThan(0);
			expect(sliceKeysIn(s.text).size, `${s.source} names no slice key any more`).toBeGreaterThan(0);
			expect(SOURCES[s.renderedBy], s.renderedBy).toMatch(/m\(f\.label_key\)/);
		}
	});

	it('are rendered only where they are loaded', () => {
		const failures: string[] = [];
		for (const { file, keys } of USES) {
			for (const [slice, list] of keys) {
				const chain = uncoveredPath(file, slice);
				if (chain)
					failures.push(
						`${file} names ${slice}-slice key ${list[0]}${list.length > 1 ? ` (+${list.length - 1} more)` : ''}, ` +
							`but is reached without loading it: ${chain.join(' → ')}`
					);
			}
		}
		expect(failures).toEqual([]);
	});
});

describe('catalogue slice scan', () => {
	// The scan is the whole guarantee, so its own moving parts are pinned.
	it('resolves the import shapes the app uses', () => {
		expect(resolveImport('/src/routes/admin/+layout.ts', '#lib/i18n/store.svelte.ts')).toBe('/src/lib/i18n/store.svelte.ts');
		expect(resolveImport('/src/lib/i18n/catalogues.ts', './messages')).toBe('/src/lib/i18n/messages.ts');
		expect(resolveImport('/src/lib/help/guides/setup.ts', '../types.ts')).toBe('/src/lib/help/types.ts');
	});

	it('finds literal keys, {ui:} references and runtime-built prefixes', () => {
		const [aSettingsKey] = Object.keys(EN_SLICES.settings);
		const prefix = aSettingsKey.slice(0, aSettingsKey.lastIndexOf('.') + 1);
		expect(sliceKeysIn(`m('${aSettingsKey}')`).get('settings')).toEqual([aSettingsKey]);
		expect(sliceKeysIn(`'Open {ui:${aSettingsKey}}.'`).get('settings')).toEqual([aSettingsKey]);
		expect(sliceKeysIn('m(`' + prefix + '${id}`)').get('settings')).toContain(aSettingsKey);
		expect(sliceKeysIn(`m('${prefix}' + id)`).get('settings')).toContain(aSettingsKey);
		expect(sliceKeysIn(`m('nav.dashboard')`).size).toBe(0);
	});

	it('reads the loaders off the code', () => {
		expect(LOADING_DIRS.get('settings')).toEqual(
			expect.arrayContaining(['/src/routes/organization', '/src/routes/admin', '/src/routes/billing', '/src/routes/help'])
		);
		expect(LOADING_DIRS.get('portal')).toEqual(['/src/routes/portal']);
		expect(OWNERS.get('help')).toEqual(['/src/lib/components/help/HelpTip.svelte']);
	});

	it('follows a key through shared code to an uncovered route', () => {
		// lib/types/admin.ts is imported all over the app; a settings key named
		// there would be reached from /invoices, which never loads the slice.
		expect(uncoveredPath('/src/lib/types/admin.ts', 'settings')).not.toBeNull();
		expect(uncoveredPath('/src/routes/admin/+page.svelte', 'settings')).toBeNull();
		expect(uncoveredPath('/src/routes/invoices/+page.svelte', 'settings')).toEqual(['/src/routes/invoices/+page.svelte']);
	});
});
