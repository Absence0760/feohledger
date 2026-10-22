import { beforeEach, describe, expect, it } from 'vitest';
import { sidebar } from './sidebar.svelte.ts';

/**
 * The shell sidebar's three states and the one derived from them: `overlay` is
 * the expanded panel floating over the page, which only happens on a narrow
 * viewport (#635 finding 39). The store is a module singleton, so each test
 * puts it back to the wide, expanded default first.
 */
describe('sidebar store', () => {
	beforeEach(() => {
		sidebar.setNarrow(false);
		if (sidebar.collapsed) sidebar.toggle();
	});

	it('starts wide and expanded, which is not an overlay', () => {
		expect(sidebar.collapsed).toBe(false);
		expect(sidebar.narrow).toBe(false);
		expect(sidebar.overlay).toBe(false);
	});

	it('is an overlay only when narrow AND expanded', () => {
		sidebar.setNarrow(true);
		expect(sidebar.overlay).toBe(true);

		sidebar.collapse();
		expect(sidebar.overlay).toBe(false);

		sidebar.toggle();
		expect(sidebar.overlay).toBe(true);

		sidebar.setNarrow(false);
		expect(sidebar.collapsed).toBe(false);
		expect(sidebar.overlay).toBe(false);
	});

	it('collapse is idempotent, unlike toggle', () => {
		sidebar.collapse();
		sidebar.collapse();
		expect(sidebar.collapsed).toBe(true);
		sidebar.toggle();
		expect(sidebar.collapsed).toBe(false);
	});

	it('setNarrow leaves the collapsed state alone', () => {
		sidebar.collapse();
		sidebar.setNarrow(true);
		expect(sidebar.collapsed).toBe(true);
		sidebar.setNarrow(false);
		expect(sidebar.collapsed).toBe(true);
	});
});
