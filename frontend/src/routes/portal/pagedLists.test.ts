import { describe, expect, it } from 'vitest';

/**
 * Source-scan guard: every supplier-portal list is actually paged.
 *
 * All four portal list endpoints (`/api/portal/{invoices,payments,
 * purchase-orders,discount-offers}`) return the canonical
 * `{items, total, page, page_size}` envelope with a server-side default of 20
 * rows. The pages originally fetched the bare URL, read only `res.items`, and
 * rendered no pagination control — so a supplier with 25 invoices saw 20 rows,
 * no count, and had no way to reach invoice 21 or its chat thread. Older
 * remittances and PO flips were unreachable the same way, and a discount offer
 * past the first page expired without ever being shown.
 *
 * Raising `page_size` only moves the cliff to the backend's `MAX_PAGE_SIZE`,
 * so the fix is the AP app's Load-More pattern, and this asserts each half of
 * it stays wired. A Playwright spec (`tests-e2e/portal/pagination.spec.ts`)
 * proves the behaviour end-to-end on the PO + invoice lists; this scan is what
 * keeps the other two — whose seed data is heavier — from silently regressing.
 *
 * Reads the tree through Vite's `import.meta.glob` for the same reason
 * `lib/utils/pagedListFooter.test.ts` does — the frontend deliberately carries
 * no `@types/node`.
 */

const RAW = import.meta.glob('/src/routes/portal/**/+page.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/** route path → the `portal.<ns>` i18n namespace its footer strings live in. */
const PAGED_LISTS: Record<string, string> = {
	'/src/routes/portal/invoices/+page.svelte': 'portal.invoices',
	'/src/routes/portal/payments/+page.svelte': 'portal.payments',
	'/src/routes/portal/purchase-orders/+page.svelte': 'portal.po',
	'/src/routes/portal/discount-offers/+page.svelte': 'portal.discounts'
};

describe('supplier-portal paged lists', () => {
	it('reads the portal route tree', () => {
		expect(Object.keys(RAW).length).toBeGreaterThan(4);
		for (const path of Object.keys(PAGED_LISTS)) {
			expect(RAW[path], `${path} not found — did the route move?`).toBeTypeOf('string');
		}
	});

	for (const [path, ns] of Object.entries(PAGED_LISTS)) {
		describe(path, () => {
			it('requests an explicit page + page_size', () => {
				const src = RAW[path];
				expect(src, 'no PORTAL_PAGE_SIZE — the request never carries a page size').toContain(
					'PORTAL_PAGE_SIZE'
				);
				expect(src, 'no page_size in the request params').toContain('page_size:');
			});

			it('keeps the server-side total in page state', () => {
				expect(RAW[path], 'total is never read off the response').toContain('total = res.total');
			});

			it('appends the next page via appendUnique (never a raw spread)', () => {
				const src = RAW[path];
				expect(src, 'no appendUnique import').toContain(
					"from '#lib/utils/pagination.ts'"
				);
				expect(src).toContain('appendUnique(items, res.items)');
				expect(src, 'raw spread re-surfaces a shifted row and breaks the keyed each').not.toMatch(
					/\[\s*\.\.\.items\s*,\s*\.\.\.res\.items\s*\]/
				);
			});

			it('sequences its fetches', () => {
				const src = RAW[path];
				expect(src, 'no createRequestSequencer — a slow page can clobber a newer one').toContain(
					'createRequestSequencer()'
				);
				expect(src, 'a response is committed without canCommit').toContain('canCommit(token)');
				// The `finally` must clear `loading` on isCurrentRequest, NOT canCommit —
				// otherwise a superseded load leaves the spinner stuck on forever.
				expect(src).toContain('isCurrentRequest(token)');
			});

			it('renders BOTH footer branches, "showing all" only behind total > 0', () => {
				const src = RAW[path];
				expect(src, 'no Load-more control').toContain(`m('${ns}.loadMore'`);
				expect(src, 'no end-of-list footer').toContain(`m('${ns}.showingAll'`);
				expect(src).toContain('class="btn-load-more"');
				// `total` counts the whole filtered set, so claiming "showing all" while
				// rows are still unfetched asserts that rows nobody loaded don't exist.
				expect(src, '"showing all" is not gated on the has-more branch').toMatch(
					/\{:else if total > 0\}/
				);
			});
		});
	}
});
