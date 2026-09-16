import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, test } from 'vitest';

/**
 * The Cookie Notice publishes an exhaustive inventory of everything this app
 * stores in a visitor's browser. That is a promise to a reader, and an
 * ePrivacy/GDPR transparency claim — "these are the only things we store" is
 * only true until someone adds a key and forgets the page exists.
 *
 * Nothing else would catch that. The notice is prose, the storage calls are
 * spread across `api.ts`, `portalApi.ts`, `entity.ts`, the i18n store, the
 * consent banner and the login flow, and a new `localStorage.setItem` is a
 * perfectly ordinary line of code to write.
 *
 * So: scan the source for storage keys, and require each one to appear in the
 * published notice. The test names the offending key, so the fix is obvious —
 * document it, or don't store it.
 */

const SRC = new URL('../../', import.meta.url).pathname;
const COOKIE_PAGE = join(SRC, 'routes/legal/cookies/+page.svelte');

/** Every `.ts`/`.svelte` file under `src/`, excluding tests. */
function sourceFiles(dir: string, acc: string[] = []): string[] {
	for (const name of readdirSync(dir)) {
		const full = join(dir, name);
		if (statSync(full).isDirectory()) {
			sourceFiles(full, acc);
		} else if (/\.(ts|svelte)$/.test(name) && !/\.test\.ts$/.test(name)) {
			acc.push(full);
		}
	}
	return acc;
}

/**
 * Keys written as string literals directly in a storage call, plus the
 * `const X_KEY = '...'` declarations the modules hoist them into.
 *
 * A key built from a template literal (the entity switcher's
 * `selected_entity_id:${tenant}`) cannot be recovered statically, which is why
 * the prefix below is asserted separately rather than pretended to be covered.
 */
function declaredKeys(): Set<string> {
	const keys = new Set<string>();
	const direct = /(?:localStorage|sessionStorage)\.(?:get|set|remove)Item\(\s*'([^']+)'/g;
	const hoisted = /const\s+(?:[A-Z_]*KEY)\s*=\s*'([^']+)'/g;

	for (const file of sourceFiles(SRC)) {
		const text = readFileSync(file, 'utf8');
		for (const m of text.matchAll(direct)) keys.add(m[1]);
		for (const m of text.matchAll(hoisted)) keys.add(m[1]);
	}
	return keys;
}

describe('the Cookie Notice lists every key the app actually stores', () => {
	const page = readFileSync(COOKIE_PAGE, 'utf8');

	test('every statically-declared storage key appears in the notice', () => {
		const missing = [...declaredKeys()].filter((key) => !page.includes(key));

		expect(
			missing,
			`These browser-storage keys are set by the app but are not named in ` +
				`/legal/cookies. Add them to the inventory table (or stop storing them): ` +
				missing.join(', ')
		).toEqual([]);
	});

	test('the dynamically-keyed entity selection is covered by its prefix', () => {
		// `entity.ts` builds `selected_entity_id:<tenant>` at runtime, so the scan
		// above cannot see it. The prefix is the stable part and is what the
		// notice documents.
		expect(page).toContain('selected_entity_id');
	});

	test('the scan actually found the keys we know exist', () => {
		// A regex that silently matched nothing would make the first test pass
		// vacuously — the failure mode this guard is least able to notice about
		// itself.
		const found = declaredKeys();
		for (const known of ['auth_token', 'portal_auth_token', 'feoh_locale', 'feoh_consent_choice']) {
			expect(found, `scan missed a known key: ${known}`).toContain(known);
		}
	});
});
