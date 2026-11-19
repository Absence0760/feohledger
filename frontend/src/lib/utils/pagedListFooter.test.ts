import { describe, expect, it } from 'vitest';

/**
 * Source-scan guard: a list that can say "Showing all N" must also be able to
 * load the rest of the N.
 *
 * Six lists (budgets, intake, catalogs, requisitions, and the /expenses
 * Reports + Cards sub-lists) asked the API for one capped page and then
 * rendered `m('…​.showingAll', { total })` unconditionally, where `total` is the
 * server's count of the *whole* filtered set. With 87 budgets the page showed
 * 50 rows and asserted "Showing all 87 budgets" — the remaining 37 were
 * unreachable, and the footer said they did not exist.
 *
 * The rule: any file referencing a `<list>.showingAll` message must also
 * reference the matching `<list>.loadMore`, which is what renders the button
 * that appends the next page. Per-key, not per-file, because `/expenses` holds
 * four independent sub-lists in one component.
 *
 * Since the footer became one component (`ui/LoadMoreFooter.svelte`), the
 * pairing is structural there: it renders the button while `hasMore` and the
 * end-of-list line only in the else-branch. So the guard also refuses the
 * hand-rolled copy that used to be pasted into 26 files (the
 * `.load-more-row` / `.btn-load-more` markup) anywhere but that component,
 * and checks that each use names one list's two messages.
 *
 * Reads the tree through Vite's `import.meta.glob` for the same reason
 * `a11y/tokenPairing.test.ts` does — the frontend deliberately carries no
 * `@types/node`.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const SHOWING_ALL = /m\(\s*'([A-Za-z0-9.]+)\.showingAll'/g;

const FOOTER = '/src/lib/components/ui/LoadMoreFooter.svelte';

/** The footer markup's own hooks: only the component may render them. */
const HAND_ROLLED = /(?<![\w-])(load-more-row|btn-load-more)(?![\w-])/;

/** Strip comments, so a sentence that names a class is not a copy of it. */
function code(source: string): string {
	return source.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '');
}

/** Files outside the component that render the footer markup by hand. */
function handRolled(files: Record<string, string>): string[] {
	return Object.entries(files)
		.filter(([path, source]) => path !== FOOTER && HAND_ROLLED.test(code(source)))
		.map(([path]) => path);
}

/** Each `<LoadMoreFooter …/>` whose two keys name different lists. */
function mismatchedKeys(files: Record<string, string>): string[] {
	const out: string[] = [];
	for (const [path, source] of Object.entries(files)) {
		for (const tag of source.matchAll(/<LoadMoreFooter\b[\s\S]*?\/>/g)) {
			const more = /moreKey="([A-Za-z0-9.]+)\.loadMore"/.exec(tag[0]);
			const all = /allKey="([A-Za-z0-9.]+)\.showingAll"/.exec(tag[0]);
			if (!more || !all || more[1] !== all[1]) out.push(`${path}: ${tag[0].replace(/\s+/g, ' ')}`);
		}
	}
	return out;
}

describe('paged list footers', () => {
	it('reads the component tree', () => {
		expect(Object.keys(RAW).length).toBeGreaterThan(50);
		expect(RAW[FOOTER], 'LoadMoreFooter.svelte moved?').toBeTypeOf('string');
	});

	it('every "Showing all N" footer is paired with a Load more control', () => {
		const offenders: string[] = [];
		for (const [path, source] of Object.entries(RAW)) {
			SHOWING_ALL.lastIndex = 0;
			for (const match of source.matchAll(SHOWING_ALL)) {
				const list = match[1];
				if (!source.includes(`'${list}.loadMore'`)) offenders.push(`${path} → ${list}`);
			}
		}
		expect(
			offenders,
			'these lists claim to show everything but offer no way to load the rest'
		).toEqual([]);
	});

	it('only LoadMoreFooter renders the Load more / Showing all markup', () => {
		expect(
			handRolled(RAW),
			'render <LoadMoreFooter> instead of copying its markup'
		).toEqual([]);
		// The component itself still carries the hooks Playwright selects on.
		expect(RAW[FOOTER]).toContain('class="load-more-row"');
		expect(RAW[FOOTER]).toContain('class="btn-load-more"');
		expect(RAW[FOOTER]).toContain('class="load-more-end"');
	});

	it("every LoadMoreFooter names one list's loadMore and showingAll", () => {
		const uses = Object.values(RAW).join('\n').match(/<LoadMoreFooter\b/g) ?? [];
		expect(uses.length, 'the paged lists render LoadMoreFooter').toBeGreaterThan(30);
		expect(mismatchedKeys(RAW)).toEqual([]);
	});
});

describe('the footer scanners catch what they are for', () => {
	it('flags a hand-rolled footer, but not the component or a comment', () => {
		const files = {
			'/src/routes/a/+page.svelte': '<div class="load-more-row"><span class="load-more-end">x</span></div>',
			'/src/routes/b/+page.svelte': '<button class="btn-load-more" onclick={more}>More</button>',
			'/src/routes/c/+page.svelte': '<!-- the old .load-more-row markup lives in LoadMoreFooter -->',
			'/src/routes/d/+page.svelte': '<div class="load-more-rows btn-load-more-x">fine</div>',
			[FOOTER]: '<div class="load-more-row"><button class="btn-load-more"></button></div>'
		};
		expect(handRolled(files)).toEqual(['/src/routes/a/+page.svelte', '/src/routes/b/+page.svelte']);
	});

	it("flags a footer whose keys name two lists, or omit one", () => {
		const files = {
			'/src/ok.svelte':
				'<LoadMoreFooter onloadmore={() => load({ append: true })} moreKey="budgets.loadMore" allKey="budgets.showingAll" />',
			'/src/mixed.svelte': '<LoadMoreFooter\n\tmoreKey="budgets.loadMore"\n\tallKey="intake.showingAll"\n/>',
			'/src/missing.svelte': '<LoadMoreFooter moreKey="budgets.loadMore" />'
		};
		const found = mismatchedKeys(files);
		expect(found).toHaveLength(2);
		expect(found[0]).toMatch(/^\/src\/mixed\.svelte/);
		expect(found[1]).toMatch(/^\/src\/missing\.svelte/);
	});
});
