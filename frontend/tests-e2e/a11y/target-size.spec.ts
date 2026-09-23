import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from './axe-helper';

/**
 * Accessibility regression guard for WCAG 2.2 AA, SC 2.5.8 Target Size
 * (Minimum) — **row-select checkboxes** (the design-system control) and the
 * **branding colour field** (a page-level control that lost the criterion to a
 * CSS specificity tie).
 *
 * **This guard is verified red-to-green, not asserted to be one.** The round
 * that landed the checkbox fix could not run Playwright, so it shipped this
 * file unexecuted; it has since been run against the pre-fix `app.css`
 * (`width/height: 16px`, a 1px opaque border, `margin: 0`, `outline: none` on
 * focus) and **five of the six checkbox cases fail** there — target 16 vs the
 * 24 floor (×3), the painted/margin box measuring 14 rather than 16, and
 * `outline-style: none`. Do not weaken an assertion here without re-running
 * that experiment: the value of this file is entirely in the failures it
 * produces, and the sixth case used to produce none (see its own comment).
 *
 * The criterion asks for a 24×24 CSS-px target, OR — through its *spacing*
 * exception — an undersized target whose 24px-diameter circle reaches no other
 * target. `input[type=checkbox]` is painted 16×16, so for years it cleared
 * 2.5.8 only through that exception, and whether it did was decided by the
 * padding each page gave its checkbox column rather than by anything in the
 * design system. `app.css` now grows the *hit box* to 24×24 (a 4px transparent
 * border, `background-clip: padding-box`, and a matching -4px margin so the
 * margin box — and therefore every row height — is still 16×16), which means
 * the SIZE half of the criterion is met outright and the neighbour geometry
 * stops mattering.
 *
 * **Why this spec exists separately from `axe.spec.ts`.** That suite already
 * scans `/invoices`, and it passed throughout — because whether the first row's
 * checkbox is even *in scope* for the rule depends on tenant state. A disabled
 * input is not focusable, so axe's `widget-not-inline-matches` skips it
 * entirely, and every `SYSTEM_MANAGED_STATUSES` row (`pending`, `paid`,
 * `done`, …) renders its checkbox disabled. A seeded tenant whose visible rows
 * are all system-managed therefore hands axe no selectable checkbox to measure,
 * and the scan reports the page clean without having looked. Playwright runs
 * spec folders alphabetically, so `a11y/` sees the tenant *before* the
 * `invoices/` specs add rows to it — the coverage was an artefact of file
 * ordering. Each test here rewrites the list response so a selectable row is
 * guaranteed on screen, then asserts the geometry directly as well as through
 * axe.
 *
 * The complement is the static scan `src/lib/a11y/targetSizeAudit.test.ts`,
 * which catches the base rule being weakened anywhere in the tree without
 * needing a route to render it. Neither subsumes the other: the scan cannot
 * resolve the cascade, and axe cannot see a state no listed route reaches.
 */

/** The 2.5.8 floor. axe allows a 0.05px rounding margin; we do not need it. */
const MIN_TARGET = 24;
/** What the control is *painted* at — unchanged by the hit-area fix. */
const PAINTED = 16;
/** How far the hit box has to extend past the paint on each side to reach the
 *  floor from the painted size: (24 − 16) / 2. */
const HIT_INSET = (MIN_TARGET - PAINTED) / 2;

/**
 * Retire the response-rewriting route handler before the test does.
 *
 * `makeFirstRowSelectable` intercepts the invoice list and calls
 * `route.fetch()` to get the real payload back. `/invoices` keeps issuing list
 * and count requests while the page settles, so a handler can still be inside
 * that `await` when the last assertion resolves and Playwright tears the test
 * down — surfacing as `route.fetch: Test ended.` / `apiResponse.json: Response
 * has been disposed`, attributed to whichever test owned the handler. It is a
 * harness race, not an app defect, and it took down the two SHORTEST tests here
 * (the select-all case and the sweep, which assert and finish before a
 * page-load request has come back) on a CI shard, one of them hard enough to
 * kill the worker.
 *
 * `unrouteAll({ behavior: 'ignoreErrors' })` is the documented remedy and the
 * idiom this suite already uses (`bank-reconciliation.spec.ts`): it waits for
 * running handlers and discards the teardown-only errors. Deliberately NOT a
 * try/catch inside the handler falling back to `route.continue()` — that would
 * let a genuinely failed fetch through un-rewritten, so the test would pass
 * having measured a checkbox it never made selectable.
 */
test.afterEach(async ({ page }) => {
	await page.unrouteAll({ behavior: 'ignoreErrors' });
});

/**
 * Force the first invoice row into a selectable status so its checkbox is
 * enabled, and therefore in scope for the `target-size` rule.
 *
 * Rewrites the real payload rather than replacing it (the pattern
 * `deemphasised-rows.spec.ts` uses for the credit-memo and payment-queue
 * cases): the row shape is wide, and what matters here is the row *state*, not
 * a fixture. The `**\/api/invoices**` glob also matches `/api/invoices/counts`
 * and `/api/invoices/{id}`, so the handler re-checks the pathname rather than
 * trusting the glob.
 */
async function makeFirstRowSelectable(page: import('@playwright/test').Page): Promise<void> {
	await page.route('**/api/invoices**', async (route) => {
		const request = route.request();
		if (request.method() !== 'GET' || new URL(request.url()).pathname !== '/api/invoices') {
			await route.continue();
			return;
		}
		const response = await route.fetch();
		const body = (await response.json()) as { items?: { status?: string }[] };
		if (body.items?.length) body.items[0].status = 'ready_for_review';
		await route.fulfill({ response, json: body });
	});
}

/** Box metrics of one element, in CSS px, as the browser lays it out. */
async function boxes(locator: import('@playwright/test').Locator) {
	return locator.evaluate((el) => {
		const rect = el.getBoundingClientRect();
		const cs = getComputedStyle(el);
		const num = (v: string) => parseFloat(v) || 0;
		const bl = num(cs.borderLeftWidth);
		const bt = num(cs.borderTopWidth);
		const br = num(cs.borderRightWidth);
		const bb = num(cs.borderBottomWidth);
		const ml = num(cs.marginLeft);
		const mt = num(cs.marginTop);
		const mr = num(cs.marginRight);
		const mb = num(cs.marginBottom);
		return {
			// What a pointer hit-tests, and what axe measures.
			target: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
			// The padding box: with a transparent border and
			// `background-clip: padding-box`, this is exactly what is painted.
			painted: {
				x: rect.x + bl,
				y: rect.y + bt,
				width: rect.width - bl - br,
				height: rect.height - bt - bb
			},
			// What the surrounding layout reserves — the number that decides row
			// height, so it must not have grown.
			margin: { width: rect.width + ml + mr, height: rect.height + mt + mb },
			outline: { style: cs.outlineStyle, width: cs.outlineWidth, offset: cs.outlineOffset }
		};
	});
}

test.describe('accessibility — target size on row checkboxes (WCAG 2.2 AA, 2.5.8)', () => {
	test('a selectable invoice row checkbox meets the 24×24 target with no axe violations', async ({
		page
	}) => {
		await makeFirstRowSelectable(page);
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const checkbox = page.locator('table tbody tr').first().locator('input[type="checkbox"]');
		// The precondition the route scan never established: a DISABLED checkbox
		// is not focusable, so axe skips it and reports the page clean.
		await expect(checkbox).toBeEnabled();

		const m = await boxes(checkbox);
		expect(m.target.width).toBeGreaterThanOrEqual(MIN_TARGET);
		expect(m.target.height).toBeGreaterThanOrEqual(MIN_TARGET);

		await expectNoA11yViolations(page);
	});

	test('the select-all header checkbox meets the 24×24 target', async ({ page }) => {
		await makeFirstRowSelectable(page);
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const selectAll = page.locator('table thead input[type="checkbox"]');
		await expect(selectAll).toBeEnabled();

		const m = await boxes(selectAll);
		expect(m.target.width).toBeGreaterThanOrEqual(MIN_TARGET);
		expect(m.target.height).toBeGreaterThanOrEqual(MIN_TARGET);
	});

	test('growing the hit box changes neither the painted control nor the row height', async ({
		page
	}) => {
		// The fix would be a regression of its own if it pushed every table row
		// 8px taller, so the layout half is asserted, not assumed: the MARGIN box
		// is what a line box / flex row / table cell reserves, and it is still
		// 16×16 because the 4px transparent border is cancelled by a -4px margin.
		await makeFirstRowSelectable(page);
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const row = page.locator('table tbody tr').first();
		const checkbox = row.locator('input[type="checkbox"]');
		const m = await boxes(checkbox);

		expect(m.painted.width).toBeCloseTo(PAINTED, 1);
		expect(m.painted.height).toBeCloseTo(PAINTED, 1);
		expect(m.margin.width).toBeCloseTo(PAINTED, 1);
		expect(m.margin.height).toBeCloseTo(PAINTED, 1);

		// Concentric: the extra hit area is shared equally on all four sides, so
		// the control is painted exactly where a 16×16 checkbox always was.
		expect(m.painted.x - m.target.x).toBeCloseTo(m.target.x + m.target.width - (m.painted.x + m.painted.width), 1);
		expect(m.painted.y - m.target.y).toBeCloseTo(m.target.y + m.target.height - (m.painted.y + m.painted.height), 1);

		// The row is taller than the checkbox in every table (a status badge and
		// the row-action button both out-measure it), so the cell — and with it
		// the row — must be unaffected by the hit box at all.
		const rowBox = await row.boundingBox();
		expect(rowBox!.height).toBeGreaterThan(MIN_TARGET);
	});

	test('the enlarged hit area is real: a click outside the painted box toggles the row', async ({
		page
	}) => {
		// Measuring the rect only proves what axe will read. This proves the
		// browser hit-tests it too — click 2px left of the painted control, which
		// is inside the 24×24 target and outside the 16×16 paint.
		//
		// The inset is asserted against HIT_INSET, not merely `> 0`. This was the
		// ONE case that still passed against the pre-fix CSS, and it passed
		// vacuously: a 1px opaque border also satisfies `insetX > 0`, and the
		// resulting click at x + 0.5 lands on the painted box, so it only ever
		// re-proved that clicking a checkbox toggles it.
		await makeFirstRowSelectable(page);
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const checkbox = page.locator('table tbody tr').first().locator('input[type="checkbox"]');
		await expect(checkbox).toBeEnabled();
		await expect(checkbox).not.toBeChecked();

		const m = await boxes(checkbox);
		const insetX = m.painted.x - m.target.x;
		expect(insetX).toBeGreaterThanOrEqual(HIT_INSET);

		const clickX = m.target.x + insetX / 2;
		// The point is outside the paint — otherwise this asserts nothing new.
		expect(clickX).toBeLessThan(m.painted.x);
		await page.mouse.click(clickX, m.target.y + m.target.height / 2);

		await expect(checkbox).toBeChecked();
	});

	test('the keyboard focus ring is drawn on the painted control, not the hit box', async ({
		page
	}) => {
		// A ring around the 24×24 hit box would float 4px off the control it is
		// meant to identify. A negative `outline-offset` pulls it back onto the
		// painted edge (2.4.7 / 2.4.13 — the indicator has to point at the thing
		// that has focus).
		await makeFirstRowSelectable(page);
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const checkbox = page.locator('table tbody tr').first().locator('input[type="checkbox"]');
		await expect(checkbox).toBeEnabled();
		await checkbox.focus();

		const m = await boxes(checkbox);
		expect(m.outline.style).not.toBe('none');
		const width = parseFloat(m.outline.width);
		const offset = parseFloat(m.outline.offset);
		expect(width).toBeGreaterThan(0);
		// The ring's outer edge sits at `|offset|` inside the hit box; it must
		// land within one stroke of the painted edge (which is 4px in), i.e. it
		// traces the control rather than the invisible target around it.
		const inset = m.painted.x - m.target.x;
		expect(Math.abs(-offset - inset)).toBeLessThanOrEqual(width);
	});

	test('every enabled checkbox on a list page meets the 24×24 target', async ({ page }) => {
		// A sweep rather than a named control: the fix is global, so the guard
		// should fail if ANY page-scoped rule shrinks a checkbox back below the
		// floor — which is exactly what `min-width`/`min-height` (rather than
		// `width`/`height`) in the base rule exist to prevent.
		await makeFirstRowSelectable(page);
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const sizes = await page
			.locator('input[type="checkbox"]:not([disabled])')
			.evaluateAll((els) =>
				els
					.filter((el) => (el as HTMLElement).offsetParent !== null)
					.map((el) => {
						const r = el.getBoundingClientRect();
						return { width: r.width, height: r.height };
					})
			);

		expect(sizes.length).toBeGreaterThan(0);
		for (const size of sizes) {
			expect(size.width).toBeGreaterThanOrEqual(MIN_TARGET);
			expect(size.height).toBeGreaterThanOrEqual(MIN_TARGET);
		}
	});
});

test.describe('accessibility — target size on the branding colour field (WCAG 2.2 AA, 2.5.8)', () => {
	/**
	 * `/organization`'s Branding panel pairs a native colour swatch with a hex
	 * text field. Both are page-level controls, so neither is covered by the
	 * design-system checkbox rule above — and the panel is where SC 2.5.8 was
	 * actually being failed on a shipping page.
	 *
	 * The cause was a specificity TIE, not a size mistake. The page's text-entry
	 * recipe (`width: 100%`) went from 0-0-1 to 0-2-1 when it gained
	 * `:not([type='checkbox']):not([type='radio'])`, which ties
	 * `.color-field input[type='color']` (`width: 40px`) and sits later in the
	 * file, so the swatch grew to the full 478px column and squeezed the hex
	 * field beside it to **22px** — under the floor, and unusable well before
	 * that. `axe.spec.ts`'s `/organization` scan catches it as a `target-size`
	 * violation; this names the geometry, so the next failure says *what* broke
	 * rather than only *that* something did.
	 */
	test('the swatch keeps its own width and the hex field clears the 24px floor', async ({
		page
	}) => {
		// The Branding panel by name: the page shows one section at a time now
		// (`docs/decisions.md` §205), so a bare `/organization` renders the
		// Getting-started lead-in and this locator would find nothing.
		await page.goto('/organization?section=branding');

		const field = page.locator('#org-branding .color-field').first();
		await expect(field).toBeVisible();
		const swatch = field.locator('input[type="color"]');
		const hex = field.locator('input[type="text"]');

		const fieldBox = (await field.boundingBox())!;
		const swatchBox = (await swatch.boundingBox())!;
		const hexBox = (await hex.boundingBox())!;

		// Both controls meet the floor outright.
		expect(swatchBox.width).toBeGreaterThanOrEqual(MIN_TARGET);
		expect(swatchBox.height).toBeGreaterThanOrEqual(MIN_TARGET);
		expect(hexBox.width).toBeGreaterThanOrEqual(MIN_TARGET);
		expect(hexBox.height).toBeGreaterThanOrEqual(MIN_TARGET);

		// The regression signature, asserted as a proportion rather than a pixel
		// count so a column-width change doesn't make this brittle: the swatch is
		// a fixed-size chip and the hex field takes the rest of the row.
		expect(swatchBox.width).toBeLessThan(fieldBox.width / 2);
		expect(hexBox.width).toBeGreaterThan(fieldBox.width / 2);
	});
});
