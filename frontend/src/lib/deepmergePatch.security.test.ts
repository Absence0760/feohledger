// Guard: the build's copy of deepmerge carries this repo's patch for
// GHSA-gcqx-w48m-w2h4 (frontend/patches/deepmerge@4.3.1.patch, listed under
// package.json `pnpm.patchedDependencies`). 4.3.1 is the last release and has
// no fix, so the patch is what stops a "__proto__" key from setting the merged
// object's prototype. vite-plugin-svelte merges the Svelte config with it.
//
// The advisory is also ignored in two scanners, and both ignores are only
// honest while the patch applies: `osv-scanner.toml` (supply-chain-scan.yml,
// Scorecard) and package.json `pnpm.auditConfig.ignoreGhsas` (audit.yml's
// `pnpm audit`). docs/decisions.md §352.
//
// If this fails after a bump, either the patch no longer applies (re-make it
// with `pnpm patch deepmerge@<version>`) or a fixed release has landed (drop
// the patch and both ignores).
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { describe, expect, it } from 'vitest';

const ADVISORY = 'GHSA-gcqx-w48m-w2h4';

// deepmerge isn't a frontend dependency; load the copy the Svelte plugin resolves.
const pluginRequire = createRequire(
	createRequire(import.meta.url).resolve('@sveltejs/vite-plugin-svelte'),
);
const deepmerge = pluginRequire('deepmerge') as (
	target: object,
	source: object,
) => Record<string, unknown>;
const resolvedVersion = (pluginRequire('deepmerge/package.json') as { version: string }).version;

const pkg = JSON.parse(readFileSync(new URL('../../package.json', import.meta.url), 'utf8')) as {
	pnpm?: {
		patchedDependencies?: Record<string, string>;
		auditConfig?: { ignoreGhsas?: string[] };
	};
};

describe(`deepmerge prototype poisoning (${ADVISORY})`, () => {
	const poisoned = () => JSON.parse('{"__proto__": {"polluted": "yes"}}') as object;

	it('does not set the prototype from a "__proto__" key when the target has none', () => {
		const out = deepmerge(Object.create(null) as object, poisoned());
		expect(out.polluted).toBeUndefined();
		expect(Object.getPrototypeOf(out)).toBe(Object.prototype);
	});

	it('does not set the prototype for a plain target either', () => {
		const out = deepmerge({}, poisoned());
		expect(out.polluted).toBeUndefined();
	});

	it('still merges ordinary keys deeply (positive control)', () => {
		expect(deepmerge({ a: { b: 1 } }, { a: { c: 2 } })).toEqual({ a: { b: 1, c: 2 } });
	});

	it('ignores the advisory in pnpm audit only while the resolved version is patched', () => {
		const ignored = pkg.pnpm?.auditConfig?.ignoreGhsas ?? [];
		if (!ignored.includes(ADVISORY)) return;
		expect(pkg.pnpm?.patchedDependencies).toHaveProperty(`deepmerge@${resolvedVersion}`);
	});

	it('records the osv-scanner ignore with an expiry', () => {
		const toml = readFileSync(new URL('../../../osv-scanner.toml', import.meta.url), 'utf8');
		const entry = toml.split('[[IgnoredVulns]]').find((block) => block.includes(`"${ADVISORY}"`));
		expect(entry).toBeDefined();
		expect(entry).toMatch(/ignoreUntil = \d{4}-\d{2}-\d{2}/);
	});
});
