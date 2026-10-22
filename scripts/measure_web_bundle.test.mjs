#!/usr/bin/env node
// Tests for the web bundle measurement behind the budget workflow.
// `node --test scripts/measure_web_bundle.test.mjs`
//
// Built on a hand-made Vite manifest shaped like SvelteKit's, so each rule of
// the gated metric (what is shell, what a route adds, which lazy chunks count)
// is pinned on numbers small enough to check by eye.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { measure, parseDictionary, readCeilings } from './measure_web_bundle.mjs';

const KIT = 'node_modules/@sveltejs/kit/src/runtime/client';
const NODE = (n) => `.svelte-kit/generated/build/client-optimized/nodes/${n}.js`;
const APP = '.svelte-kit/generated/build/client-optimized/app.js';

/** A small build: shell, a layout, two leaves, catalogues, one lazy prose chunk. */
function fixture() {
	const manifest = {
		[APP]: { file: 'app.js', isEntry: true, imports: ['_runtime.js'], dynamicImports: [NODE(0), NODE(1), NODE(2), NODE(3), NODE(4)] },
		[`${KIT}/entry.js`]: { file: 'start.js', isEntry: true, imports: [`${KIT}/payload.js`, '_runtime.js'], dynamicImports: [`${KIT}/client-entry.js`] },
		[`${KIT}/payload.js`]: { file: 'payload.js', isEntry: true },
		[`${KIT}/client-entry.js`]: { file: 'client-entry.js', isDynamicEntry: true, imports: ['_runtime.js'] },
		'_runtime.js': { file: 'runtime.js', imports: ['_i18n.js'] },
		'_i18n.js': { file: 'i18n.js', dynamicImports: ['src/lib/i18n/locales/de.ts', 'src/lib/i18n/locales/ja.ts', 'src/lib/i18n/locales/help/de.ts', 'src/lib/i18n/locales/help/ja.ts'] },
		[NODE(0)]: { file: 'n0.js', isEntry: true, imports: ['_runtime.js', '_tip.js'], css: ['n0.css'] },
		[NODE(1)]: { file: 'n1.js', isEntry: true, imports: ['_runtime.js'] },
		[NODE(2)]: { file: 'n2.js', isEntry: true, imports: ['_runtime.js', '_admin.js'] },
		[NODE(3)]: { file: 'n3.js', isEntry: true, imports: ['_runtime.js', '_admin.js'] },
		[NODE(4)]: { file: 'n4.js', isEntry: true, imports: ['_runtime.js', '_prose.js'], css: ['n4.css'] },
		'_admin.js': { file: 'admin.js', css: ['admin.css'] },
		// The ⓘ tip, in the shell, loads the prose lazily; /help imports it statically.
		'_tip.js': { file: 'tip.js', dynamicImports: ['_prose.js'] },
		'_prose.js': { file: 'prose.js' },
		'src/lib/i18n/locales/de.ts': { file: 'de.js', src: 'src/lib/i18n/locales/de.ts', isDynamicEntry: true },
		'src/lib/i18n/locales/ja.ts': { file: 'ja.js', src: 'src/lib/i18n/locales/ja.ts', isDynamicEntry: true },
		'src/lib/i18n/locales/help/de.ts': { file: 'help-de.js', src: 'src/lib/i18n/locales/help/de.ts', isDynamicEntry: true },
		'src/lib/i18n/locales/help/ja.ts': { file: 'help-ja.js', src: 'src/lib/i18n/locales/help/ja.ts', isDynamicEntry: true }
	};
	const sizes = new Map(
		Object.entries({
			'app.js': 10, 'start.js': 20, 'payload.js': 1, 'client-entry.js': 4, 'runtime.js': 100, 'i18n.js': 50,
			'n0.js': 30, 'n0.css': 5, 'tip.js': 2,
			'n1.js': 7,
			'n2.js': 11, 'n3.js': 13, 'admin.js': 40, 'admin.css': 6,
			'n4.js': 3, 'n4.css': 1, 'prose.js': 70,
			'de.js': 60, 'ja.js': 80, 'help-de.js': 9, 'help-ja.js': 12
		})
	);
	const routes = { '/admin/users': [3, 2], '/help': [4] };
	return { manifest, sizes, routes };
}

const SHELL = 10 + 20 + 1 + 4 + 100 + 50 + 30 + 5 + 2; // entries, client-entry, runtime, i18n, root layout + css, tip

test('the shell is the boot entries, client-entry and the root layout, statically closed', () => {
	const { manifest, sizes, routes } = fixture();
	const r = measure(manifest, routes, sizes, ['main', 'help']);
	assert.deepEqual(r.errors, []);
	assert.equal(r.shellBytes, SHELL);
	assert.equal(r.shellFiles, 9);
});

test('a route counts its leaf and layouts plus their CSS, beyond the shell', () => {
	const { manifest, sizes, routes } = fixture();
	const r = measure(manifest, routes, sizes, ['main', 'help']);
	const admin = r.routeCosts.find((c) => c.id === '/admin/users');
	// leaf 13 + layout 11 + shared admin.js 40 + admin.css 6; runtime is shell.
	assert.equal(admin.bytes, 13 + 11 + 40 + 6);
});

test('a lazy chunk counts on the route that imports it statically, not where it is merely reachable', () => {
	const { manifest, sizes, routes } = fixture();
	const r = measure(manifest, routes, sizes, ['main', 'help']);
	// The tip's dynamic import of prose does not put prose in the shell...
	assert.equal(r.shellBytes, SHELL);
	// ...but /help, which imports it statically, pays for it and is the heaviest route.
	assert.equal(r.heaviest.id, '/help');
	assert.equal(r.heaviest.bytes, 3 + 1 + 70);
});

test('the gated total is shell + heaviest route + the largest catalogue of each kind', () => {
	const { manifest, sizes, routes } = fixture();
	const r = measure(manifest, routes, sizes, ['main', 'help']);
	assert.equal(r.kinds.main.file, 'ja.js');
	assert.equal(r.kinds.help.file, 'help-ja.js');
	assert.equal(r.gatedBytes, SHELL + 74 + 80 + 12);
	// Reported, not gated: every chunk with one catalogue per kind, and everything.
	const all = [...sizes.values()].reduce((a, b) => a + b, 0);
	assert.equal(r.shippedBytes, all);
	assert.equal(r.everyChunkBytes, all - (60 + 80 + 9 + 12) + 80 + 12);
	assert.deepEqual(r.largest, { bytes: 100, file: 'runtime.js' });
});

test('splitting a route into more chunks does not change its weight', () => {
	const { manifest, sizes, routes } = fixture();
	manifest['_admin.js'].imports = ['_admin2.js'];
	manifest['_admin2.js'] = { file: 'admin2.js' };
	sizes.set('admin.js', 25);
	sizes.set('admin2.js', 15);
	const r = measure(manifest, routes, sizes, ['main', 'help']);
	assert.equal(r.routeCosts.find((c) => c.id === '/admin/users').bytes, 13 + 11 + 40 + 6);
});

test('a catalogue kind the source tree has but the build does not lazy-load fails', () => {
	const { manifest, sizes, routes } = fixture();
	const r = measure(manifest, routes, sizes, ['main', 'help', 'portal']);
	assert.equal(r.errors.length, 1);
	assert.match(r.errors[0], /no lazy 'portal' locale catalogue/);
});

test('an emitted file the manifest does not name fails, so no weight escapes the route figures', () => {
	const { manifest, sizes, routes } = fixture();
	sizes.set('stray.js', 5);
	const r = measure(manifest, routes, sizes, ['main', 'help']);
	assert.match(r.errors.join('\n'), /stray\.js is in the build but not in the Vite manifest/);
});

test('a manifest file the build did not emit throws', () => {
	const { manifest, sizes, routes } = fixture();
	sizes.delete('prose.js');
	assert.throws(() => measure(manifest, routes, sizes, ['main', 'help']), /prose\.js, which the build did not emit/);
});

test('parseDictionary reads leaves with a server load, layout holes, and leaves the root layout to the shell', () => {
	const appJs = [
		"export { matchers } from './matchers.js';",
		'export const dictionary = {',
		'\t\t"/": [9],',
		'\t\t"/admin": [~11,[2]],',
		'\t\t"/a/b": [12,[2,,5],[1]]',
		'\t};',
		'export const hooks = {};'
	].join('\n');
	assert.deepEqual(parseDictionary(appJs), { '/': [9], '/admin': [11, 2], '/a/b': [12, 2, 5] });
	assert.throws(() => parseDictionary('export const nodes = [];'), /no `export const dictionary/);
});

test('ceilings come from the environment first, else from the workflow', () => {
	const yaml = 'env:\n  MAX_TOTAL_KB: 500\n  MAX_LARGEST_CHUNK_KB: 100\n';
	assert.deepEqual(readCeilings({}, yaml), { maxTotalKb: 500, maxLargestKb: 100 });
	assert.deepEqual(readCeilings({ MAX_TOTAL_KB: '9' }, yaml), { maxTotalKb: 9, maxLargestKb: 100 });
	assert.throws(() => readCeilings({}, ''), /MAX_TOTAL_KB is neither/);
});
