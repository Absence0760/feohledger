import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';

/**
 * Source guard: nothing in the e2e tree derives a worker's tenant from
 * `workerIndex`.
 *
 * Each worker is pinned to one `e2e<N>` tenant (`fixtures/helpers.ts`), and
 * the pin has to be distinct among the workers ALIVE AT ONCE. `workerIndex` is
 * not: Playwright replaces a worker after a failed test and the replacement
 * gets a fresh index (4, 5, …), so `workerIndex % E2E_TENANT_COUNT` put it on a
 * tenant another running worker already owned. After the first failure in a
 * local 4-worker run, two workers were writing to one tenant and counting specs
 * failed on the other one's rows — a cascade that read as a batch of new
 * failures. `parallelIndex` is the value Playwright guarantees is unique among
 * live workers and inherits across a replacement.
 *
 * CI runs one tenant per shard, where every index resolves to `e2e1`, so it
 * cannot catch a regression here; this guard is what does.
 */

const E2E_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

function specTree(dir: string): Array<{ path: string; source: string }> {
	const out: Array<{ path: string; source: string }> = [];
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const full = join(dir, entry.name);
		if (entry.isDirectory()) {
			if (entry.name === 'node_modules' || entry.name === '.auth') continue;
			out.push(...specTree(full));
		} else if (entry.name.endsWith('.ts')) {
			out.push({ path: relative(E2E_ROOT, full), source: readFileSync(full, 'utf8') });
		}
	}
	return out;
}

/** Block and line comments out, so prose explaining why `workerIndex` is wrong
 *  is not itself flagged. A spec may not import another spec, so this is a local
 *  copy of `meta/origin-guard.spec.ts`'s helper, as `teardown-guard` keeps too. */
function stripComments(source: string): string {
	return source.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/.*$/gm, '$1');
}

const WORKER_INDEX = /\bworkerIndex\b/;

function readsWorkerIndex(source: string): boolean {
	return WORKER_INDEX.test(stripComments(source));
}

test.describe('e2e tenant pinning', () => {
	test('no file keys anything on workerIndex', () => {
		const offenders = specTree(E2E_ROOT)
			.filter((f) => f.path !== 'meta/tenant-pinning-guard.spec.ts')
			.filter((f) => readsWorkerIndex(f.source))
			.map((f) => f.path);
		expect(
			offenders,
			'Key per-worker state on testInfo.parallelIndex — a replacement worker gets a new workerIndex and collides with a live one.'
		).toEqual([]);
	});

	test('the detector flags a known-bad file and clears a known-good one', () => {
		expect(readsWorkerIndex('use(_tenantSlugFor(workerInfo.workerIndex));')).toBe(true);
		expect(readsWorkerIndex('use(_tenantSlugFor(workerInfo.parallelIndex));')).toBe(false);
		// A comment explaining why workerIndex is wrong is not a use of it.
		expect(readsWorkerIndex('// never `workerIndex`\nconst pi = info.parallelIndex;')).toBe(false);
	});

	test('the guard is actually looking at the fixture that pins tenants', () => {
		const helpers = specTree(E2E_ROOT).find((f) => f.path === join('fixtures', 'helpers.ts'));
		expect(helpers, 'fixtures/helpers.ts not found — the guard walked the wrong tree').toBeDefined();
		expect(stripComments(helpers!.source)).toMatch(/\bparallelIndex\b/);
	});
});
