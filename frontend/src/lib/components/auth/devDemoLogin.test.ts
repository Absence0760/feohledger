import { describe, expect, it } from 'vitest';

/**
 * `DevDemoLogin.svelte` carries the seeded demo accounts and their shared
 * password. It must never reach the static bundle shipped to S3, and the only
 * thing that keeps it out is HOW it is loaded: a dynamic import inside an
 * `if (import.meta.env.DEV)` branch, which `vite build` folds to `if (false)`
 * so the chunk is never emitted.
 *
 * A static `import DevDemoLogin from …` rendered under `{#if dev}` looks
 * equivalent and is not — `dev` from `$app/env` is not folded across the module
 * boundary, and that version was measured shipping the component in the
 * production build. This guard pins the loading shape so a tidy-up cannot
 * reintroduce it.
 */

const SOURCES = import.meta.glob(['/src/**/*.svelte', '/src/**/*.ts', '!/src/**/*.test.ts'], {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const COMPONENT = 'DevDemoLogin.svelte';

describe('DevDemoLogin stays out of production builds', () => {
	it('is never statically imported', () => {
		const offenders = Object.entries(SOURCES)
			.filter(([, src]) => new RegExp(`^\\s*import\\s[^(]*['"][^'"]*${COMPONENT}['"]`, 'm').test(src))
			.map(([path]) => path);
		expect(offenders).toEqual([]);
	});

	it('is only dynamically imported inside an import.meta.env.DEV branch', () => {
		const loaders = Object.entries(SOURCES).filter(([, src]) =>
			src.includes(`import('#lib/components/auth/${COMPONENT}')`)
		);
		expect(loaders.map(([path]) => path)).toEqual(['/src/routes/login/+page.svelte']);
		for (const [, src] of loaders) {
			expect(src).toMatch(
				new RegExp(
					`if \\(import\\.meta\\.env\\.DEV\\) \\{\\s*void import\\('#lib/components/auth/${COMPONENT}'\\)`
				)
			);
		}
	});
});
