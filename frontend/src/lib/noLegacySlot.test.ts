import { describe, expect, it } from 'vitest';

// Svelte 5 runes components render passed content with `{@render children()}`
// from `let { children } = $props()`. The legacy `<slot />` still compiles in a
// runes file but is deprecated, and mixing the two in one tree is how a layout
// ends up with branches that behave differently. The three layouts that still
// used it were converted; this keeps `<slot` from coming back anywhere,
// comments included (a quoted `<slot>` in prose reads as current practice).

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

describe('no legacy <slot> in .svelte files', () => {
	it('every component renders children through a snippet', () => {
		const offenders = Object.entries(RAW)
			.filter(([, src]) => /<slot[\s/>]/.test(src))
			.map(([path]) => path.replace(/^\/src\//, ''))
			.sort();
		expect(offenders).toEqual([]);
	});

	it('the glob reaches the root layout (the scan is live)', () => {
		expect(RAW['/src/routes/+layout.svelte']).toContain('{@render children()}');
	});
});
