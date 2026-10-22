#!/usr/bin/env node
// Measures the production web bundle and asserts the two ceilings in
// .github/workflows/web-bundle-budget.yml (the workflow runs this; the ceilings
// and their dated change log live there, not here).
//
//   cd frontend && PUBLIC_API_URL=http://localhost:8000 pnpm build && cd ..
//   pnpm check:build:bundle            # or: node scripts/measure_web_bundle.mjs
//
// The gated total is what one visitor downloads to open the app's most
// expensive page, not everything the build emits:
//
//   SHELL   the static import closure of SvelteKit's boot entry points (the
//           `isEntry` chunks that are not route nodes: start, app, payload),
//           plus the runtime's client-entry (start always imports it on init)
//           and route node 0, the root layout every page renders.
//   ROUTE   for each route in the client route dictionary, the static import
//           closure of its leaf node and its layout nodes, minus the shell. The
//           heaviest one is gated. Every chunk a node statically imports is
//           downloaded before the page renders (SvelteKit preloads them).
//   LOCALES the largest file of each lazy locale catalogue kind (main and each
//           slice under locales/<slice>/): a visitor fetches at most one of
//           each kind, their own locale's (decisions §261, §300).
//
// Dynamic imports are NOT followed, except for the two above that SvelteKit
// performs unconditionally (client-entry, root layout) and the catalogues,
// which are counted by kind. The rule is that a lazy chunk is weight a visitor
// pays only if something on the page asks for it, so it is counted where it is
// statically imported and not where it is merely reachable. In this build the
// only other dynamic import is the ⓘ HelpTip's guide prose, fetched when a
// reader opens a tip; the /help routes import that same chunk statically, so
// it is counted in full on the route where everyone who visits downloads it.
// Should a route start awaiting a non-catalogue import in its `load`, it would
// escape this metric — the every-chunk figure below still sees it, and the
// per-chunk ceiling still bounds it.
//
// Also reported, NOT gated: the every-chunk sum with one catalogue per kind
// (the gated metric until 2026-10-21) and the shipped sum (every file).
//
// The per-chunk ceiling is unchanged: the largest single JS/CSS file the build
// emits, whatever reaches it. Its job is to catch one heavyweight dependency
// landing in one chunk.
//
// Sizes are gzip level 6 (gzip's default) of each file, summed in bytes and
// rounded up to whole KB once, so splitting code into more chunks never moves a
// total on its own.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import zlib from 'node:zlib';

const LOCALE_SRC_RE = /^src\/lib\/i18n\/locales\/(?:([^/]+)\/)?[^/]+\.ts$/;
const NODE_KEY_RE = /client-optimized\/nodes\/(\d+)\.js$/;
const CLIENT_ENTRY_RE = /@sveltejs\/kit\/src\/runtime\/client\/client-entry\.js$/;

export const kb = (bytes) => Math.ceil(bytes / 1024);

/**
 * Parse the `dictionary` export of SvelteKit's generated client app.js into
 * `{ routeId: [nodeIndex, ...] }` — the leaf and its layouts, root layout
 * (node 0) excluded since it belongs to the shell. The generated source writes
 * a leaf with a server load as `~n` and may leave holes in the layout list, so
 * it is evaluated as the JS expression it is rather than pattern-matched.
 */
export function parseDictionary(appJs) {
	const m = /export const dictionary = (\{[\s\S]*?\n\s*\});/.exec(appJs);
	if (!m) throw new Error('no `export const dictionary = {...};` in the generated app.js');
	const dict = Function(`"use strict"; return (${m[1]});`)();
	const routes = {};
	for (const [id, entry] of Object.entries(dict)) {
		const [leaf, layouts = []] = entry;
		const nodes = new Set([leaf < 0 ? ~leaf : leaf]);
		for (const l of layouts) if (l !== undefined && l !== 0) nodes.add(l < 0 ? ~l : l);
		routes[id] = [...nodes];
	}
	return routes;
}

/**
 * The measurement, from data alone so it can be tested without a build.
 *
 * @param {object} manifest   Vite's client manifest (.vite/manifest.json)
 * @param {object} routes     parseDictionary() output
 * @param {Map<string, number>} sizes  gzipped bytes of every emitted JS/CSS file, by build-relative path
 * @param {string[]} expectedKinds    catalogue kinds the source tree has ('main' + each slice dir)
 */
export function measure(manifest, routes, sizes, expectedKinds) {
	const errors = [];
	const size = (f) => {
		if (!sizes.has(f)) throw new Error(`the manifest names ${f}, which the build did not emit: the manifest and the build disagree`);
		return sizes.get(f);
	};

	// Every emitted file must be accounted for by the manifest, or the shell and
	// route figures could silently miss weight.
	const manifestFiles = new Set();
	for (const e of Object.values(manifest)) {
		manifestFiles.add(e.file);
		for (const c of e.css ?? []) manifestFiles.add(c);
	}
	for (const f of sizes.keys()) {
		if (!manifestFiles.has(f)) errors.push(`${f} is in the build but not in the Vite manifest, so no route would count it`);
	}

	// Lazy locale catalogues, by kind.
	const kindOf = new Map(); // manifest key -> kind
	for (const [key, e] of Object.entries(manifest)) {
		if (!e.isDynamicEntry) continue;
		const lm = LOCALE_SRC_RE.exec(e.src ?? key);
		if (lm) kindOf.set(key, lm[1] ?? 'main');
	}
	const kinds = {};
	for (const [key, kind] of kindOf) {
		const file = manifest[key].file;
		const bytes = size(file);
		if (!kinds[kind] || bytes > kinds[kind].bytes) kinds[kind] = { bytes, file };
	}
	for (const k of expectedKinds) {
		if (!kinds[k]) {
			errors.push(`no lazy '${k}' locale catalogue in the manifest. If it moved or stopped being lazy, the one-per-kind rule no longer describes a visitor; update scripts/measure_web_bundle.mjs.`);
		}
	}

	// Static closure, in emitted files (JS + its CSS).
	const closure = (roots) => {
		const files = new Set();
		const seen = new Set();
		const stack = [...roots];
		while (stack.length) {
			const key = stack.pop();
			if (seen.has(key)) continue;
			seen.add(key);
			const e = manifest[key];
			if (!e) throw new Error(`the manifest references ${key}, which it does not define`);
			files.add(e.file);
			for (const c of e.css ?? []) files.add(c);
			for (const i of e.imports ?? []) stack.push(i);
		}
		return files;
	};
	const sum = (files) => [...files].reduce((n, f) => n + size(f), 0);

	const nodeKey = new Map();
	for (const key of Object.keys(manifest)) {
		const nm = NODE_KEY_RE.exec(key);
		if (nm) nodeKey.set(Number(nm[1]), key);
	}
	const keyOfNode = (n) => {
		const k = nodeKey.get(n);
		if (!k) throw new Error(`route node ${n} is not in the manifest`);
		return k;
	};

	const shellRoots = Object.entries(manifest)
		.filter(([key, e]) => e.isEntry && !NODE_KEY_RE.test(key))
		.map(([key]) => key);
	const clientEntry = Object.keys(manifest).find((k) => CLIENT_ENTRY_RE.test(k));
	if (!shellRoots.length) errors.push('no non-route entry chunk in the manifest: the shell cannot be found');
	if (!clientEntry) errors.push("no SvelteKit client-entry.js in the manifest: if the runtime's boot changed, update the shell roots here");
	if (errors.length) return { errors };

	shellRoots.push(clientEntry, keyOfNode(0));
	const shellFiles = closure(shellRoots);
	const shellBytes = sum(shellFiles);

	const routeCosts = Object.entries(routes)
		.map(([id, nodes]) => {
			const files = [...closure(nodes.map(keyOfNode))].filter((f) => !shellFiles.has(f));
			return { id, bytes: sum(files), files: files.length };
		})
		.sort((a, b) => b.bytes - a.bytes || a.id.localeCompare(b.id));
	if (!routeCosts.length) errors.push('the route dictionary is empty');
	const heaviest = routeCosts[0] ?? { id: '(none)', bytes: 0, files: 0 };

	const catalogueFiles = new Set([...kindOf.keys()].map((k) => manifest[k].file));
	const localeBytes = Object.values(kinds).reduce((n, k) => n + k.bytes, 0);

	let shippedBytes = 0;
	let largest = { bytes: 0, file: '' };
	for (const [f, b] of sizes) {
		shippedBytes += b;
		if (b > largest.bytes) largest = { bytes: b, file: f };
	}
	const everyChunkBytes =
		[...sizes].filter(([f]) => !catalogueFiles.has(f)).reduce((n, [, b]) => n + b, 0) + localeBytes;

	return {
		errors,
		shellBytes,
		shellFiles: shellFiles.size,
		heaviest,
		routeCosts,
		kinds,
		gatedBytes: shellBytes + heaviest.bytes + localeBytes,
		everyChunkBytes,
		shippedBytes,
		fileCount: sizes.size,
		catalogueCount: catalogueFiles.size,
		largest
	};
}

/** The ceilings: env wins, else read from the workflow, the one place they are written. */
export function readCeilings(env, workflowYaml) {
	const pick = (name) => {
		if (env[name]) return Number(env[name]);
		const m = new RegExp(`^\\s+${name}:\\s*(\\d+)\\s*$`, 'm').exec(workflowYaml ?? '');
		if (!m) throw new Error(`${name} is neither in the environment nor in the workflow`);
		return Number(m[1]);
	};
	return { maxTotalKb: pick('MAX_TOTAL_KB'), maxLargestKb: pick('MAX_LARGEST_CHUNK_KB') };
}

function listFiles(dir) {
	const out = [];
	for (const ent of fs.readdirSync(dir, { withFileTypes: true, recursive: true })) {
		if (!ent.isFile() || !/\.(js|css)$/.test(ent.name)) continue;
		out.push(path.relative(dir, path.join(ent.parentPath, ent.name)));
	}
	return out.sort();
}

function main() {
	const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
	const fe = path.join(root, 'frontend');
	const buildDir = path.join(fe, 'build');
	const manifestPath = path.join(fe, '.svelte-kit/output/client/.vite/manifest.json');
	const appJsCandidates = [
		path.join(fe, '.svelte-kit/generated/build/client-optimized/app.js'),
		path.join(fe, '.svelte-kit/generated/client-optimized/app.js')
	];
	const localesSrc = path.join(fe, 'src/lib/i18n/locales');
	const workflow = path.join(root, '.github/workflows/web-bundle-budget.yml');

	const fail = (msg) => {
		console.log(`::error::${msg}`);
		process.exit(1);
	};
	if (!fs.existsSync(buildDir)) fail(`No build output at ${path.relative(root, buildDir)}. Run \`PUBLIC_API_URL=http://localhost:8000 pnpm build\` in frontend/ first.`);
	if (!fs.existsSync(manifestPath)) fail(`No Vite manifest at ${path.relative(root, manifestPath)}. The measurement needs it for the import graph; if SvelteKit moved it, update this script.`);
	const appJs = appJsCandidates.find((p) => fs.existsSync(p));
	if (!appJs) fail("No generated client app.js (SvelteKit's route dictionary). If SvelteKit moved it, update appJsCandidates in this script.");

	const { maxTotalKb, maxLargestKb } = readCeilings(process.env, fs.existsSync(workflow) ? fs.readFileSync(workflow, 'utf8') : '');
	const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
	let routes;
	try {
		routes = parseDictionary(fs.readFileSync(appJs, 'utf8'));
	} catch (e) {
		fail(`Could not read the route dictionary from ${path.relative(root, appJs)}: ${e.message}`);
	}
	const sizes = new Map(listFiles(buildDir).map((f) => [f, zlib.gzipSync(fs.readFileSync(path.join(buildDir, f)), { level: 6 }).length]));
	const expectedKinds = [
		'main',
		...fs.readdirSync(localesSrc, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name).sort()
	];

	let r;
	try {
		r = measure(manifest, routes, sizes, expectedKinds);
	} catch (e) {
		fail(e.message);
	}
	if (r.errors.length) {
		for (const e of r.errors) console.log(`::error::${e}`);
		process.exit(1);
	}

	const gatedKb = kb(r.gatedBytes);
	const largestKb = kb(r.largest.bytes);
	const top = r.routeCosts.slice(0, 5);
	const lines = [
		`Files measured:        ${r.fileCount}, ${r.catalogueCount} of them lazy locale catalogues; ${Object.keys(routes).length} routes`,
		`Gated total:           ${gatedKb} KB (budget ${maxTotalKb} KB) = shell + heaviest route + one catalogue of each kind`,
		`  App shell:           ${kb(r.shellBytes)} KB (${r.shellFiles} files)`,
		`  Heaviest route:      ${kb(r.heaviest.bytes)} KB beyond the shell — ${r.heaviest.id} (${r.heaviest.files} files)`,
		...expectedKinds.map((k) => `  Largest ${`${k}:`.padEnd(12)} ${kb(r.kinds[k].bytes)} KB — ${r.kinds[k].file}`),
		`Every chunk, not gated: ${kb(r.everyChunkBytes)} KB: every chunk, one catalogue of each kind (the gated metric until 2026-10-21)`,
		`Shipped, not gated:    ${kb(r.shippedBytes)} KB: every chunk, every catalogue`,
		`Largest chunk:         ${largestKb} KB — ${r.largest.file} (budget ${maxLargestKb} KB)`,
		'Heaviest routes (KB beyond the shell):',
		...top.map((t) => `  ${String(kb(t.bytes)).padStart(4)}  ${t.id}`)
	];
	console.log(lines.join('\n'));

	const summary = process.env.GITHUB_STEP_SUMMARY;
	if (summary) {
		const md = [
			'## Web bundle budget',
			'',
			'| Metric | Size | Budget |',
			'|---|---|---|',
			`| **Gated total** (app shell + heaviest route + one locale catalogue of each kind) | ${gatedKb} KB | ${maxTotalKb} KB |`,
			`| App shell | ${kb(r.shellBytes)} KB | — |`,
			`| Heaviest route, beyond the shell: \`${r.heaviest.id}\` | ${kb(r.heaviest.bytes)} KB | — |`,
			...expectedKinds.map((k) => `| Largest \`${k}\` catalogue | ${kb(r.kinds[k].bytes)} KB | — |`),
			`| Largest single chunk | ${largestKb} KB | ${maxLargestKb} KB |`,
			`| Every chunk, one catalogue of each kind | ${kb(r.everyChunkBytes)} KB | not gated |`,
			`| Shipped (every chunk, every catalogue) | ${kb(r.shippedBytes)} KB | not gated |`,
			`| Files measured | ${r.fileCount} (${r.catalogueCount} lazy catalogues) | — |`,
			'',
			`Largest chunk: \`${r.largest.file}\``,
			'',
			'Heaviest routes (KB beyond the shell): ' + top.map((t) => `\`${t.id}\` ${kb(t.bytes)}`).join(', '),
			''
		];
		fs.appendFileSync(summary, md.join('\n'));
	}

	let failed = false;
	if (gatedKb > maxTotalKb) {
		console.log(
			`::error::The gated bundle total is ${gatedKb} KB, over the ${maxTotalKb} KB budget (shell ${kb(r.shellBytes)} + heaviest route ${r.heaviest.id} ${kb(r.heaviest.bytes)} + catalogues). Either trim weight, or raise MAX_TOTAL_KB in .github/workflows/web-bundle-budget.yml and append a dated entry to the ceiling change log explaining the measurement and why the growth is warranted.`
		);
		failed = true;
	}
	if (largestKb > maxLargestKb) {
		console.log(
			`::error::Largest chunk ${r.largest.file} is ${largestKb} KB, over the ${maxLargestKb} KB per-chunk budget. This usually means a single heavyweight dependency landed in one chunk — check whether it is really needed before raising the ceiling. If it is a locale catalogue, move route-scoped keys into a lazy slice (decisions §261, §300) rather than raising it.`
		);
		failed = true;
	}
	process.exit(failed ? 1 : 0);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
