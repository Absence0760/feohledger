import { NAV } from '#lib/nav.ts';

import { expect, test } from '../fixtures/helpers';
import {
	REALISTIC,
	REALISTIC_MARKER,
	purgeRealisticRows,
	seedRealisticRows,
	type RealisticRows
} from '../fixtures/realisticRows';

/**
 * Layout guard at desktop widths, measured with production-shaped rows (#575).
 *
 * `reflow.spec.ts` proves no page scrolls sideways at 320px. It cannot see the
 * failure #574 shipped: at a 1666px window the exceptions queue was 1372px
 * wide in a 1088px region, so Status and the row actions sat 283px past the
 * card's edge — on a page whose every seeded row fit. The seed's strings are
 * short; a customer's are not. So this visits every page the sidebar offers,
 * with `fixtures/realisticRows.ts`'s long vendor names, ERP invoice numbers,
 * email assignee, non-ASCII unverified vendor, tax-exclusive invoice and
 * thirteen-digit amounts in the tenant (values shared with the seed through
 * `backend/scripts/fixtures/realistic_rows.json`),
 * and asserts at 1280px and 1666px that:
 *
 *   - the DOCUMENT never scrolls sideways;
 *   - a TABLE fits the region it sits in. At 1666px (the width #574 was sized
 *     for) it must fit outright. At 1280px a table too wide to fit may scroll
 *     inside its own region — the remedy #574 chose — but only a region the
 *     keyboard can reach (`tabindex="0"`, as `DataTable`'s `.grid-container`
 *     carries; axe's `scrollable-region-focusable` only fires while content
 *     actually overflows, so it is measured here directly);
 *   - a ROW ACTION stays inside its cell, and — whenever its table does not
 *     scroll — inside the table's visible region;
 *   - a KPI CARD stays inside its row, and its figure inside the card;
 *   - nothing paints past a `.card` / `.chart-card` edge outside a scroll
 *     region (the dashboard's Top Vendors figure did, and scrolled the page).
 *
 * The page list is `NAV` itself (`#lib/nav.ts`, the module the sidebar renders
 * from), so a page added to the sidebar is measured the day it exists. An
 * overflow this finds is a layout defect: fix the page through the shared
 * components (`DataTable fixed` with column widths, `cell-clip`), never by
 * widening a tolerance here.
 */

const WIDTHS = [1280, 1666] as const;

/** Every destination the sidebar can show, links and group children alike. */
const PAGES = [
	...new Set(NAV.flatMap((e) => (e.kind === 'link' ? [e.href] : e.children.map((c) => c.href))))
];

/**
 * Views the plain path does not put the realistic rows on screen in: a tab
 * (payment history), or a list whose default sort pages them out (vendors,
 * contracts and credit memos sort by name / number, so a seeded row can sit on
 * page 3). Filtering on the marker guarantees the long values are measured.
 */
const EXTRA_VIEWS = [
	'/payments?tab=history',
	`/vendors?search=${REALISTIC_MARKER}`,
	`/contracts?search=${REALISTIC_MARKER}`,
	`/credit-memos?search=${REALISTIC_MARKER}`
];

interface Finding {
	what: string;
	px: number;
}

test.describe('desktop layout with production-shaped rows (#575)', () => {
	// One seeded data set per worker for the whole file (each worker owns its
	// own tenant, and `fullyParallel: false` keeps a file on one worker); the
	// rows are read-only to every test below. Deliberately not `serial` mode:
	// one page's overflow must not skip the measurement of every page after it.

	let rows: RealisticRows | undefined;

	test.beforeAll(async ({ playwright, tenantSlug, tenantAdmin }) => {
		const request = await playwright.request.newContext();
		try {
			await purgeRealisticRows(request, tenantSlug, tenantAdmin);
			rows = await seedRealisticRows(request, tenantSlug, tenantAdmin);
		} finally {
			await request.dispose();
		}
	});

	test.afterAll(async ({ playwright, tenantSlug, tenantAdmin }) => {
		const request = await playwright.request.newContext();
		try {
			await purgeRealisticRows(request, tenantSlug, tenantAdmin, rows);
		} finally {
			await request.dispose();
		}
	});

	test('the page walk found the sidebar surface', () => {
		// A walk that silently returned nothing would delete every test below.
		expect(PAGES.length).toBeGreaterThan(30);
		for (const path of ['/invoices', '/payments', '/vendors', '/exceptions']) {
			expect(PAGES).toContain(path);
		}
	});

	for (const width of WIDTHS) {
		for (const path of [...PAGES, ...EXTRA_VIEWS]) {
			test(`${path} at ${width}px keeps tables, row actions and KPI cards inside their containers`, async ({
				page
			}) => {
				await page.setViewportSize({ width, height: 900 });
				await page.goto(path);

				// Readiness, as in reflow.spec.ts: the shell hydrated, then the
				// route's own <h1>. No `networkidle`, no sleep.
				await expect(page.locator('aside.sidebar').first()).toBeVisible();
				await expect(page).not.toHaveURL(/\/login/);
				await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
				// A route that lists rows has finished loading them once no table
				// region is still busy; pages without one pass straight through.
				await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);

				const findings = await page.evaluate(measureLayout, { allowScroll: width < 1666 });
				expect(
					findings,
					`${path} at ${width}px:\n` +
						findings.map((f) => `  ${f.what} (${f.px}px)`).join('\n')
				).toEqual([]);
			});
		}
	}

	test('the realistic rows actually render on the pages they target', async ({
		page,
		tenantSlug
	}) => {
		// Without this, a seed that silently landed nowhere (a filter, a
		// status the list hides) would make the sweep above measure the short
		// seed strings again and pass for the wrong reason.
		await page.setViewportSize({ width: 1666, height: 900 });
		await page.goto('/exceptions');
		await expect(page.getByText(REALISTIC.assigneeEmail(tenantSlug)).first()).toBeAttached();
		await page.goto('/invoices');
		await expect(page.getByText(REALISTIC.invoiceNumbers[0]).first()).toBeAttached();
		await expect(page.getByText(REALISTIC.taxExclusive.invoiceNumber).first()).toBeAttached();
		await page.goto(`/vendors?search=${REALISTIC_MARKER}`);
		await expect(page.getByText(REALISTIC.nonAsciiVendor).first()).toBeAttached();
		await page.goto(`/contracts?search=${REALISTIC_MARKER}`);
		await expect(page.getByText(REALISTIC.contractNumber).first()).toBeAttached();
		await page.goto(`/credit-memos?search=${REALISTIC_MARKER}`);
		await expect(page.getByText(REALISTIC.memoNumber).first()).toBeAttached();
	});
});

/**
 * Runs in the page. Returns one finding per element that leaves its container.
 * Kept self-contained: `page.evaluate` serialises the function, so it may not
 * close over anything in this module.
 */
function measureLayout({ allowScroll }: { allowScroll: boolean }): Finding[] {
	const out: Finding[] = [];
	const TOL = 1;

	const label = (el: Element): string => {
		const tag = el.tagName.toLowerCase();
		const testId = el.getAttribute('data-testid');
		const cls =
			typeof el.className === 'string' && el.className.trim()
				? `.${el.className.trim().split(/\s+/).slice(0, 2).join('.')}`
				: '';
		const text = (el.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 40);
		return `${tag}${cls}${testId ? `[data-testid=${testId}]` : ''}${text ? ` "${text}"` : ''}`;
	};
	const shown = (el: Element): boolean => {
		const r = el.getBoundingClientRect();
		return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
	};
	/** The nearest ancestor that clips or scrolls horizontally — the table's region. */
	const regionOf = (el: Element): HTMLElement => {
		for (let p = el.parentElement; p; p = p.parentElement) {
			if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(p).overflowX)) return p;
		}
		return document.documentElement;
	};

	const doc = document.documentElement;
	const docOverflow = doc.scrollWidth - doc.clientWidth;
	if (docOverflow > TOL) out.push({ what: 'the document scrolls sideways', px: docOverflow });

	for (const table of document.querySelectorAll('table')) {
		if (!shown(table)) continue;
		const region = regionOf(table);
		const tableOverflow = Math.round(table.getBoundingClientRect().width - region.clientWidth);
		const scrolls = tableOverflow > TOL;
		if (scrolls) {
			const scrollable = /(auto|scroll)/.test(getComputedStyle(region).overflowX);
			const reachable = region.tabIndex >= 0;
			if (!allowScroll) {
				out.push({ what: `table wider than its region ${label(region)}`, px: tableOverflow });
			} else if (!scrollable || !reachable) {
				out.push({
					what: `table overflows a region the keyboard cannot scroll ${label(region)}`,
					px: tableOverflow
				});
			}
		}
		const regionBox = region.getBoundingClientRect();
		for (const cell of table.querySelectorAll('tbody td')) {
			const cellBox = cell.getBoundingClientRect();
			for (const action of cell.querySelectorAll('button, a, .row-action')) {
				if (!shown(action)) continue;
				const box = action.getBoundingClientRect();
				const spill = Math.round(Math.max(box.right - cellBox.right, cellBox.left - box.left));
				if (spill > TOL) out.push({ what: `row action spills out of its cell: ${label(action)}`, px: spill });
				if (!scrolls) {
					const past = Math.round(box.right - regionBox.right);
					if (past > TOL) {
						out.push({ what: `row action past the table's region: ${label(action)}`, px: past });
					}
				}
			}
		}
	}

	for (const card of document.querySelectorAll('.kpi')) {
		if (!shown(card)) continue;
		const row = card.parentElement;
		if (row) {
			const past = Math.round(card.getBoundingClientRect().right - row.getBoundingClientRect().right);
			if (past > TOL) out.push({ what: `KPI card past its row: ${label(card)}`, px: past });
		}
		const spill = card.scrollWidth - card.clientWidth;
		if (spill > TOL) out.push({ what: `KPI card content overflows the card: ${label(card)}`, px: spill });
	}

	// Content painting past a CARD's edge (`.card`, `.chart-card`). Neither
	// clips, so a figure wider than its slot draws over the card border — and
	// on the rightmost card it scrolls the whole page, which is how the
	// dashboard's Top Vendors amount first showed up (#575). Only leaves with
	// no clipping or scrolling ancestor between them and the card are
	// measured: anything inside a scroll region is that region's business,
	// and the table checks above own it.
	for (const card of document.querySelectorAll('.card, .chart-card')) {
		if (!shown(card)) continue;
		const cardRight = card.getBoundingClientRect().right;
		for (const leaf of card.querySelectorAll('*')) {
			if (leaf.children.length > 0 || !shown(leaf)) continue;
			let clipped = false;
			for (let p = leaf.parentElement; p && p !== card; p = p.parentElement) {
				if (getComputedStyle(p).overflowX !== 'visible') {
					clipped = true;
					break;
				}
			}
			if (clipped) continue;
			// The TEXT's extent, not just the element's box: a fixed-width span
			// keeps its box inside the card while its glyphs overflow it, which is
			// exactly the Top Vendors defect. How far the glyphs reach depends on
			// the font, so measuring the box alone passed locally and only the
			// wider CI font pushed it into a document scroll.
			// A leaf that clips itself (an ellipsized name) paints only its box.
			let right = leaf.getBoundingClientRect().right;
			if (getComputedStyle(leaf).overflowX === 'visible') {
				const range = document.createRange();
				range.selectNodeContents(leaf);
				right = Math.max(right, range.getBoundingClientRect().right);
			}
			const past = Math.round(right - cardRight);
			if (past > TOL) {
				out.push({ what: `content past its card's edge: ${label(leaf)}`, px: past });
				break; // one finding per card names the defect
			}
		}
	}

	return out;
}
