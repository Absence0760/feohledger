import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { acceptConsent, expect, test } from '../fixtures/helpers';
import { WEB_ORIGIN, tenantOrigin } from '../fixtures/env';

/**
 * The published legal documents — `/legal` and the five pages under it.
 *
 * These are load-bearing in a way most routes are not. A customer's DPO reads
 * the DPA and the sub-processor register before signing; a supplier whose bank
 * details and tax ID we hold reads the Privacy Policy to find out how to
 * exercise a right; a regulator reads whichever one is being complained about.
 * So the failure this spec guards is not "the page looks wrong" — it is a
 * published legal document that has silently lost a clause, become unreachable,
 * or started asserting a fact nobody filled in.
 *
 * Three properties, in order of how badly they fail:
 *
 *  1. **Reachable anonymously, on both host shapes.** Nobody exercising a data
 *     right has an account, and they arrive on whichever origin the link they
 *     followed used. `routes/+layout.svelte` routes `/legal/*` ahead of both
 *     its auth gate and its tenant probe for this reason — on the apex a
 *     tenant-gated legal page would render the marketing Landing instead, and
 *     on a tenant subdomain it would bounce to a sign-in form.
 *  2. **No unfilled fact reaches the reader as prose.** Pending operator facts
 *     render through `lib/legal/Fact.svelte` as a marked `[… to be confirmed]`
 *     span. An empty string, `null`, or a plausible-looking invented value
 *     would each be worse than the gap itself.
 *  3. **The required clauses are present by name.** Each document has a handful
 *     of disclosures it exists to make. They are asserted individually so a
 *     refactor that drops one fails here rather than at a compliance review.
 *  4. **Every section is reachable from a table of contents.** Ninety-one
 *     sections across the six, and the reason a document gets a TOC rather
 *     than the panels `/organization` gets is that all of it must stay
 *     rendered — so the contents list is the only navigation there is
 *     (`docs/decisions.md` §205).
 *
 * Deliberately NOT asserted: the exact wording. These documents get revised,
 * and a spec that pinned prose would be edited to match on every revision until
 * it asserted nothing. Clause presence is the durable contract.
 */

/**
 * The document set, hardcoded rather than imported.
 *
 * `$lib` value imports do not resolve under Playwright's transform (see
 * `frontend/CLAUDE.md` § `pnpm check` does not cover `tests-e2e/`), but that
 * constraint is a feature here: a hardcoded list means DELETING a page from
 * `pages.ts` fails this spec, where a loop over the imported list would just
 * quietly run one fewer iteration. The drift test below keeps the two honest.
 */
const PAGES = [
	{ path: '/legal/privacy', title: 'Privacy Policy' },
	{ path: '/legal/terms', title: 'Terms of Service' },
	{ path: '/legal/dpa', title: 'Data Processing Addendum' },
	{ path: '/legal/sub-processors', title: 'Sub-processors' },
	{ path: '/legal/accessibility', title: 'Accessibility Statement' },
	{ path: '/legal/cookies', title: 'Cookie Notice' },
] as const;

/** Markers that mean a draft escaped to a published legal page. */
const DRAFT_MARKERS = [
	/TODO/,
	/FIXME/,
	/lorem ipsum/i,
	/\bplaceholder\b/i,
	/\[insert /i,
	/\bXXX\b/,
	/coming soon/i,
	/sample (?:policy|agreement|text)/i,
];

/**
 * Values that mean a fact leaked through unrendered. `Fact.svelte` exists to
 * make this impossible; these assertions are what prove it stayed impossible.
 */
const UNRENDERED = [/\bundefined\b/, /\bnull\b/, /\[object Object\]/, /NaN/];

/**
 * The contents list `lib/legal/LegalPage.svelte` derives for every document.
 *
 * A CSS locator rather than `getByRole`, on purpose: the `<summary>` that
 * discloses the list has no role Playwright's own ARIA mapping computes, so
 * `getByRole('button', { name: 'Contents' })` matches nothing. The `<nav>`'s
 * accessible name is the stable part — it exists to distinguish this landmark
 * from the "Other legal documents" one below it.
 */
const CONTENTS = 'nav[aria-label="Sections of this document"]';

test.describe('legal pages', () => {
	// Every test in this file runs signed OUT. That is the condition the pages
	// have to work under, and the default worker storage state would hide a
	// regression that made them auth-gated.
	test.use({ storageState: { cookies: [], origins: [] } });

	for (const { path, title } of PAGES) {
		test(`${path} renders anonymously with no draft markers`, async ({ page }) => {
			await page.goto(path);

			await expect(page.getByRole('heading', { level: 1, name: title })).toBeVisible();
			await expect(page.getByText(/^Last updated: \d{4}-\d{2}-\d{2}$/)).toBeVisible();

			const body = await page.locator('.legal-page').innerText();
			// A legal document that fits in a tweet is a stub, not a document.
			expect(body.length).toBeGreaterThan(2000);

			for (const marker of DRAFT_MARKERS) {
				expect(body, `${path} contains a draft marker: ${marker}`).not.toMatch(marker);
			}
			for (const leak of UNRENDERED) {
				expect(body, `${path} leaked an unrendered value: ${leak}`).not.toMatch(leak);
			}
		});

		test(`${path} lists every one of its sections in its contents`, async ({ page }) => {
			// The contents list is DERIVED from the rendered `h2[id]` set rather
			// than declared per page, which is what makes this assertion the real
			// contract instead of a restatement: a hand-kept list could match a
			// count and still name the wrong sections, so the labels and the
			// hrefs are compared element by element, in order. A section added,
			// renamed or renumbered in the page file needs no edit here — one
			// dropped out of the list fails.
			await page.goto(path);
			await expect(page.getByRole('heading', { level: 1, name: title })).toBeVisible();

			const headings = page.locator('.legal-page h2[id]');
			await expect(headings.first()).toBeVisible();
			const sectionCount = await headings.count();
			// The shortest of the six (the Cookie Notice) has nine. A document
			// that suddenly reports one or two has lost its body, not its TOC.
			expect(sectionCount, `${path} has no id'd sections to list`).toBeGreaterThanOrEqual(9);

			const entries = page.locator(`${CONTENTS} a`);
			await expect(entries).toHaveCount(sectionCount);

			const flatten = (s: string) => s.replace(/\s+/g, ' ').trim();
			expect(
				(await entries.allTextContents()).map(flatten),
				`${path}: a contents entry does not read like the heading it points at`
			).toEqual((await headings.allTextContents()).map(flatten));
			expect(
				await entries.evaluateAll((els) =>
					(els as HTMLAnchorElement[]).map((a) => a.getAttribute('href'))
				),
				`${path}: a contents entry points somewhere other than its section`
			).toEqual(await headings.evaluateAll((els) => els.map((h) => `#${h.id}`)));
		});
	}

	test('the contents lists document sections, never the page around them', async ({ page }) => {
		// Three `<h2>`s on this page are not sections of the document: the
		// pending-facts notice's "Details still to be confirmed", the
		// cross-document nav's "Other documents", and the consent banner's
		// "Your privacy choices". Scoping the derivation to the `<article>` is
		// the whole of what excludes them — a query widened to the document
		// would put all three in the contents of a privacy policy, and the
		// count assertions above would still pass, because they count the same
		// widened set.
		await page.goto('/legal/privacy');

		const entries = page.locator(`${CONTENTS} a`);
		await expect(entries.first()).toBeVisible();

		const sections = await page.locator('.legal-page h2[id]').count();
		const everyHeading = await page.locator('h2').count();
		expect(
			everyHeading,
			'the chrome headings this guard is about are gone — re-point it at whatever replaced them'
		).toBeGreaterThan(sections);
		await expect(entries).toHaveCount(sections);

		for (const chrome of [
			'Details still to be confirmed',
			'Other documents',
			'Your privacy choices'
		]) {
			await expect(
				entries.filter({ hasText: chrome }),
				`the contents lists page chrome: ${chrome}`
			).toHaveCount(0);
		}
	});

	test('activating a contents entry reaches that section and marks it current', async ({
		page
	}) => {
		// The DPA is the document this exists for: twenty-one sections over
		// 1,552 lines, and §14 is the one a customer's DPO is sent to.
		await page.goto('/legal/dpa');

		const entry = page.locator(`${CONTENTS} a[href="#transfers"]`);
		await expect(entry).toHaveText('14. International transfers');
		await entry.click();

		// The section lands in the URL, so the reader can cite it, share it, and
		// go Back — the three properties a JS scroll handler would have cost,
		// and the reason these are plain anchors with nothing intercepting them.
		await expect(page).toHaveURL(/\/legal\/dpa#transfers$/);
		await expect(page.locator('.legal-page h2#transfers')).toBeInViewport();

		// Scroll-spy: exactly one entry is current, and it is the section the
		// reader is now in.
		await expect(entry).toHaveAttribute('aria-current', 'true');
		await expect(page.locator(`${CONTENTS} a[aria-current="true"]`)).toHaveCount(1);
	});

	test('a deep link to a section still lands on that section', async ({ page }) => {
		// Every cross-reference inside Terms and the DPA is an in-page anchor
		// ("see section 10"), and external links arrive on them too — a
		// procurement email says "/legal/terms#termination". Adding navigation
		// must not have changed what arriving at one does, and the contents list
		// has to agree about where the reader landed.
		await page.goto('/legal/terms#termination');

		const heading = page.locator('.legal-page h2#termination');
		await expect(heading).toBeVisible();
		await expect(heading).toBeInViewport();
		await expect(
			page.locator(`${CONTENTS} a[href="#termination"]`)
		).toHaveAttribute('aria-current', 'true');
	});

	test('the contents is a rail beside the document, or a closed disclosure above it', async ({
		page
	}) => {
		// Two forms, one list. The reading measure is the thing neither form may
		// touch: a contents column carved out of 46rem would make every document
		// worse to read in exchange for navigating it, which is the trade #433
		// already refused once.
		const contents = page.locator(CONTENTS);
		const disclosure = page.locator(`${CONTENTS} details`);
		const column = page.locator('.legal-page');

		await page.setViewportSize({ width: 1400, height: 900 });
		await page.goto('/legal/privacy');
		await expect(contents).toBeVisible();

		// Wide: open, pinned, and entirely to the LEFT of the text column.
		await expect(disclosure).toHaveJSProperty('open', true);
		await expect(contents).toHaveCSS('position', 'sticky');
		const rail = (await contents.boundingBox())!;
		const wide = (await column.boundingBox())!;
		expect(
			rail.x + rail.width,
			'the contents rail overlaps the reading column'
		).toBeLessThanOrEqual(wide.x);
		expect(wide.width, 'the rail was taken out of the 46rem measure').toBeGreaterThan(700);

		// Narrow: back in the flow, above the text, and closed — on a phone an
		// open twenty-one-entry list is a screenful in front of the document.
		await page.setViewportSize({ width: 800, height: 900 });
		await expect(disclosure).toHaveJSProperty('open', false);
		await expect(contents).toHaveCSS('position', 'static');
		const stacked = (await contents.boundingBox())!;
		const narrow = (await column.boundingBox())!;
		expect(stacked.y, 'the contents sits below the text it indexes').toBeLessThan(narrow.y);
	});

	test('the index links every document', async ({ page }) => {
		await page.goto('/legal');
		await expect(page.getByRole('heading', { level: 1, name: 'Legal' })).toBeVisible();

		for (const { path, title } of PAGES) {
			await expect(page.getByRole('link', { name: title }).first()).toHaveAttribute('href', path);
		}
	});

	test('every legal route offers a way back into the product (#434)', async ({ page }) => {
		// The dead end this catches is invisible to a component test: each page
		// rendered correctly, and the trap was a property of the set in its
		// layout. `routes/+layout.svelte` branches `/legal/*` past the tenant
		// probe — right, and necessary — but that branch stripped the sidebar,
		// the header and every route out of the document set, and put nothing
		// back. The index's outbound links were three deeper into `/legal` and
		// three `mailto:`; the six documents had one link to the index. So a
		// signed-in user opening the Cookie Notice from the consent banner, and a
		// procurement reviewer sent the DPA, both had the Back button or the URL
		// bar and nothing else.
		//
		// Asserted as a PROPERTY of every route rather than as "the header is
		// present", so a future redesign that moves the escape hatch somewhere
		// else still passes, and one that drops it cannot.
		for (const { path } of [{ path: '/legal' }, ...PAGES]) {
			await page.goto(path);
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

			const escapes = await page.locator('a[href]').evaluateAll((els) =>
				(els as HTMLAnchorElement[])
					// `mailto:` reaches a person, not the product, and an in-page
					// anchor resolves to the path it is already on — neither is a
					// way out. `a.pathname` resolves a relative href against the
					// document, so both fall out on the prefix test below.
					.filter((a) => a.protocol === 'http:' || a.protocol === 'https:')
					.map((a) => a.pathname)
					.filter((pathname) => !pathname.startsWith('/legal'))
			);

			expect(
				escapes,
				`${path} has no link out of /legal — it is a navigational dead end`
			).not.toHaveLength(0);
		}
	});

	test('the legal header carries the mark and a sign-in link on every route', async ({ page }) => {
		// The specific affordances #434 asks for, asserted once the property
		// above is satisfied: the mark goes to `/`, which already resolves
		// correctly on both host shapes (the marketing Landing on the apex, the
		// app on a tenant subdomain), and sign-in goes to the one sign-in route.
		//
		// This describe block is ANONYMOUS (`test.use` at the top clears
		// storage), which is what makes "Sign in" the right label here — the
		// signed-in half of the same pill is pinned in `the legal set from
		// inside the app` at the bottom of this file.
		for (const { path } of [{ path: '/legal' }, ...PAGES]) {
			await page.goto(path);
			const header = page.getByRole('banner');
			// Exactly one. Both the index and every document open with a
			// `<header>` of their own, which is a `banner` outside a landmark —
			// so the top bar is wrapped around a `<main>` that demotes them. Two
			// banners is not a WCAG A/AA failure (axe files it under
			// best-practice, which this suite does not run), so nothing else
			// here would catch it.
			await expect(header).toHaveCount(1);
			await expect(header.getByRole('link', { name: 'FeohLedger' })).toHaveAttribute('href', '/');
			await expect(header.getByRole('link', { name: 'Sign in' })).toHaveAttribute(
				'href',
				'/login'
			);
		}
	});

	test('the header renders on the apex too, where no tenant resolves', async ({ page }) => {
		// The header must not have reintroduced what the standalone branch in
		// `routes/+layout.svelte` exists to avoid. On the apex `hasTenant` is
		// FALSE, so anything reading tenant state renders its no-tenant branch
		// (the marketing Landing) or nothing at all. A header that still draws
		// here is a header that waits on nothing — which is the property, since
		// no assertion can observe a pre-hydration document in a client-rendered
		// app.
		await page.goto(`${WEB_ORIGIN}/legal/dpa`);
		const header = page.getByRole('banner');
		await expect(header.getByRole('link', { name: 'FeohLedger' })).toBeVisible();
		await expect(header.getByRole('link', { name: 'Sign in' })).toBeVisible();
	});

	test('the reading measure is still 46rem after the backdrop work', async ({ page }) => {
		// 46rem is the right line length for a document someone has to read end
		// to end, and the instinctive way to answer "63% of the screen is empty"
		// is to stretch the text into it — which would make these worse, not
		// better (#433 says so explicitly). Measured on the RENDERED column
		// rather than on the CSS, so a second `max-width` reintroduced anywhere,
		// or a sheet gutter eating into the measure, fails here.
		await page.setViewportSize({ width: 1600, height: 900 });
		await page.goto('/legal/privacy');

		const article = page.locator('.legal-page');
		const width = (await article.boundingBox())?.width ?? 0;
		// 46rem at the 16px root = 736px. The sheet's gutter is outside the
		// measure, so the text column is the measure itself.
		expect(width).toBeGreaterThan(700);
		expect(width, 'the legal measure was widened — see #433').toBeLessThanOrEqual(736);
	});

	test('a pending operator fact renders as a marked gap, never as a bare blank', async ({
		page
	}) => {
		// While any fact in `lib/legal/operator.ts` is null the pages must SAY so
		// — both in the notice at the top and inline where the fact would have
		// appeared. Filling every fact is the intended end state, so this test
		// asserts the two halves agree rather than asserting facts are pending.
		await page.goto('/legal/privacy');

		const notice = page.getByRole('complementary', { name: /Details still to be confirmed/i });
		const inlineGaps = page.locator('.legal-page .fact-pending');

		if (await notice.isVisible()) {
			// Something is pending: it must be marked inline too, and each marker
			// must name what is missing rather than render an empty span.
			expect(await inlineGaps.count()).toBeGreaterThan(0);
			for (const text of await inlineGaps.allInnerTexts()) {
				expect(text.trim()).toMatch(/to be confirmed\]$/);
			}
		} else {
			// Every fact is filled — then no inline gap may remain anywhere.
			await expect(inlineGaps).toHaveCount(0);
		}
	});

	test('each document states the disclosures it exists to make', async ({ page }) => {
		// One assertion per clause, so a failure names the missing disclosure.
		const required: Record<string, RegExp[]> = {
			'/legal/privacy': [
				/controller/i,
				/processor/i,
				/lawful basis/i,
				/erasure|right to erasure|delete/i,
				/portab(?:le|ility)/i,
				/international transfer/i,
				/retention/i,
				/supervisory authority/i,
				/automated decision/i,
				/children/i
			],
			'/legal/terms': [
				/limitation of liability/i,
				/acceptable use/i,
				/indemnif/i,
				/governing law/i,
				/termination/i,
				// The five product-specific disclaimers. These are the heart of the
				// liability posture for an AP platform and the most likely to be
				// lost in a rewrite.
				/not.{0,40}(tax|accounting|legal|financial) advice/i,
				/money transmitter|payment institution|not a bank/i,
				/sanctions/i,
				/human review|reviewed by a human|can be wrong/i
			],
			'/legal/dpa': [
				/documented instructions/i,
				/sub-processor/i,
				/technical and organi[sz]ational/i,
				/personal data breach/i,
				/deletion or return|return or deletion/i,
				/audit/i,
				/standard contractual clauses/i,
				/annex/i
			],
			'/legal/sub-processors': [/change/i, /object/i, /transfer/i],
			'/legal/cookies': [/strictly necessary/i, /withdraw/i, /localStorage/i],
			// The accessibility statement's whole value is that it admits what is
			// not verified. A revision that quietly dropped the limitations section
			// would leave a statement claiming more than the evidence supports —
			// which is the same failure as claiming a certification, one document over.
			'/legal/accessibility': [
				/WCAG 2\.2/,
				/Level AA/,
				/partially conformant/i,
				/self-assessment/i,
				/screen reader|screen-reader/i,
				/not yet verified|have not yet verified/i,
				/feedback|report a barrier|tell us/i
			]
		};

		for (const [path, clauses] of Object.entries(required)) {
			await page.goto(path);
			const body = await page.locator('.legal-page').innerText();
			for (const clause of clauses) {
				expect(body, `${path} is missing a required disclosure: ${clause}`).toMatch(clause);
			}
		}
	});

	test('no page claims a certification the project does not hold', async ({ page }) => {
		// The single most dangerous class of error in this document set: a
		// published certification claim with no audit behind it is a deception
		// problem, not a drafting one. No SOC 2 report and no ISO 27001
		// certificate exists (`docs/soc2-readiness.md` is a plan). Any mention
		// must be forward-looking, so the bare claim shapes are banned outright.
		const BANNED = [
			/\bSOC 2 (?:Type )?(?:I{1,2}|1|2)?\s*[- ]?\s*(?:certified|compliant|attested)/i,
			/\bISO[ -]?27001[ -]?(?:certified|compliant)/i,
			// Negative lookahead so the HONEST sentence — "we hold no SOC 2
			// report" — is not itself flagged. Only a positive claim is banned.
			/\bwe (?:are|hold|have)\s+(?!no\b|not\b|neither\b)[^.]{0,40}\bSOC 2\b/i,
			/\bPCI[ -]DSS[ -]?(?:certified|compliant)/i,
			/\bHIPAA[ -]?compliant/i
		];

		for (const { path } of PAGES) {
			await page.goto(path);
			const body = await page.locator('.legal-page').innerText();
			for (const claim of BANNED) {
				expect(body, `${path} asserts an unheld certification: ${claim}`).not.toMatch(claim);
			}
		}
	});

	test('the documents are reachable on the apex host, where there is no tenant', async ({
		page
	}) => {
		// On the apex the layout renders the marketing Landing for every other
		// path. A legal page that fell through to it would be a link that looks
		// live and silently serves the wrong document.
		await page.goto(`${WEB_ORIGIN}/legal/privacy`);
		await expect(page.getByRole('heading', { level: 1, name: 'Privacy Policy' })).toBeVisible();
	});

	test('the documents do not bounce a signed-out visitor on a tenant host', async ({
		page,
		tenantSlug
	}) => {
		// The regression this catches: on a tenant subdomain `hasTenant` is true,
		// so without the legal bypass the auth effect redirects to /login and a
		// supplier with no account can never read the policy covering their data.
		await page.goto(`${tenantOrigin(tenantSlug)}/legal/privacy`);
		await expect(page.getByRole('heading', { level: 1, name: 'Privacy Policy' })).toBeVisible();
		expect(page.url()).toContain('/legal/privacy');
	});

	test('the marketing footer links the whole document set', async ({ page }) => {
		// Published-but-unlinked is the state this whole change existed to fix.
		await page.goto(WEB_ORIGIN);
		for (const { path, title } of PAGES) {
			await expect(page.getByRole('link', { name: title }).first()).toHaveAttribute('href', path);
		}
	});

	test('the consent banner links the Cookie Notice it is obtaining consent under', async ({
		page
	}) => {
		await page.goto(WEB_ORIGIN);
		const banner = page.getByRole('region', { name: 'Cookie and privacy consent' });
		await expect(banner).toBeVisible();
		await expect(banner.getByRole('link', { name: /Cookie Notice/i })).toHaveAttribute(
			'href',
			'/legal/cookies'
		);
	});

	test('signup presents the terms it forms a contract under', async ({ page }) => {
		// Clicking that button creates a tenant and binds an organisation. The
		// terms have to be presented at the point of assent, not only from a
		// footer on another page.
		await page.goto(`${WEB_ORIGIN}/signup`);
		const consent = page.locator('.legal-consent');
		await expect(consent.getByRole('link', { name: 'Terms of Service' })).toHaveAttribute(
			'href',
			'/legal/terms'
		);
		await expect(consent.getByRole('link', { name: 'Privacy Policy' })).toHaveAttribute(
			'href',
			'/legal/privacy'
		);
		await expect(consent.getByRole('link', { name: 'Data Processing Addendum' })).toHaveAttribute(
			'href',
			'/legal/dpa'
		);

		// The sentence is assembled from segments — the catalogue entry's text
		// either side of each `{token}` marker, with an anchor rendered between
		// them (`lib/i18n/segments.ts`). A splitter that dropped the space before
		// a link, or an `{#each}` whose whitespace handling ate one, would render
		// "agree to theTerms of Service": still three correct links, so the
		// assertions above would pass, and still a mangled consent line.
		const sentence = (await consent.innerText()).replace(/\s+/g, ' ').trim();
		expect(sentence).toBe(
			'By creating a workspace you agree to the Terms of Service and the Privacy Policy, ' +
				'including the Data Processing Addendum that governs supplier and invoice data you ' +
				'load into it.'
		);
	});

	test('the supplier portal gives a vendor a route to the whole document set', async ({
		page,
		tenantSlug
	}) => {
		// A supplier is a data subject whose bank details and tax ID we hold, and
		// the portal is the only surface they ever see. The footer used to render
		// only when the TENANT had configured its own URLs, so an unconfigured
		// tenant's suppliers had no route to us at all.
		//
		// It then linked two of the documents by name, which left a Supplier User
		// — bound by Terms §3.4, and the subject of the DPA — unable to reach
		// either. So the assertion follows the link rather than pinning an href:
		// what matters is that the route ARRIVES at the documents, and a footer
		// naming a subset passed the old shape of this test while failing the
		// reader.
		await page.goto(`${tenantOrigin(tenantSlug)}/portal/login`);
		await page.getByRole('link', { name: 'Privacy & terms' }).click();

		await expect(page.getByRole('heading', { level: 1, name: 'Legal' })).toBeVisible();
		for (const { path, title } of PAGES) {
			await expect(page.getByRole('link', { name: title }).first()).toHaveAttribute('href', path);
		}
	});

	test('no document scrolls the page sideways at 320px (WCAG 1.4.10)', async ({ page }) => {
		// These pages are the most table-heavy in the app — the sub-processor
		// register alone carries twelve. A wide table in normal flow widens the
		// DOCUMENT, so the reader scrolls the whole page horizontally to read one
		// column, which is exactly what 1.4.10 Reflow prohibits. Each is wrapped
		// in `.table-scroll` so the overflow belongs to the table instead.
		await page.setViewportSize({ width: 320, height: 720 });

		for (const { path } of PAGES) {
			await page.goto(path);
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

			const overflow = await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			);
			expect(overflow, `${path} overflows the viewport by ${overflow}px at 320px`).toBeLessThanOrEqual(
				0
			);
		}
	});

	test('the Cookie Notice can withdraw a consent choice, not just describe how', async ({
		page
	}) => {
		// ePrivacy requires withdrawal to be as easy as giving consent. Until the
		// control existed the only route was clearing site data through browser
		// settings — accurate, but not equivalent, and the notice had to say so
		// because it was the truth.
		const CONSENT_KEY = 'feoh_consent_choice';
		const banner = page.getByRole('region', { name: 'Cookie and privacy consent' });

		// Seed the choice BEFORE the document loads, rather than setting it and
		// reloading. Two reasons, and the second is the one that bit: a reload
		// means the assertions below race hydration, and `toBeHidden()` passes
		// vacuously against the pre-hydration document (the banner is rendered by
		// Svelte, so "not there yet" and "correctly hidden" look identical). The
		// click then landed in the window after the button painted but before its
		// handler was attached, and was silently lost.
		await page.addInitScript((key) => localStorage.setItem(key, 'rejected'), CONSENT_KEY);
		await page.goto('/legal/cookies');

		// Wait on the control's OWN readiness, not on the heading: the legal
		// routes render ahead of the tenant probe, so their markup (heading
		// included) exists before hydration and proves nothing about handlers.
		// The button disables itself until the component mounts, which is both
		// honest UX and the only truthful signal here.
		const withdraw = page.getByRole('button', { name: 'Change your privacy choice' });
		await expect(withdraw).toBeEnabled();
		await expect(banner).toBeHidden();

		await withdraw.click();

		// The banner comes back in the same page view — not on the next load —
		// and the stored choice is gone.
		await expect(banner).toBeVisible();
		expect(await page.evaluate((key) => localStorage.getItem(key), CONSENT_KEY)).toBeNull();
	});

	test('every table scroller is reachable by keyboard (WCAG 2.1.1)', async ({ page }) => {
		// Solving reflow by wrapping a wide table in an `overflow-x: auto` div
		// creates a region only a mouse can pan. axe reports it as
		// `scrollable-region-focusable`, but ONLY once the table actually
		// overflows — which depends on how wide the font renders, so it passed
		// locally and failed in CI. Asserting the attribute directly does not
		// depend on rendering at all.
		for (const { path } of PAGES) {
			await page.goto(path);
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

			const scrollers = page.locator('.legal-page .table-scroll');
			const count = await scrollers.count();
			for (let i = 0; i < count; i++) {
				await expect(
					scrollers.nth(i),
					`${path}: table scroller ${i} is not keyboard-reachable`
				).toHaveAttribute('tabindex', '0');
			}
		}
	});

	test('the route set and lib/legal/pages.ts have not drifted apart', async () => {
		// The hardcoded PAGES list above is the guard; this keeps it honest
		// against the source of truth the app actually renders from, so adding a
		// document to `pages.ts` without covering it here fails rather than
		// going untested.
		const source = readFileSync(
			fileURLToPath(new URL('../../src/lib/legal/pages.ts', import.meta.url)),
			'utf8'
		);
		const declared = [...source.matchAll(/path:\s*'([^']+)'/g)].map((m) => m[1]);

		expect(declared.sort()).toEqual(PAGES.map((p) => p.path).sort());
	});
});

test.describe('the legal set from inside the app', () => {
	// Signed IN, unlike everything above: this is the surface the other tests
	// deliberately exclude, and the one the documents themselves promise to give
	// change notices on (`privacy` §19, `terms` §18). The set was reachable from
	// the marketing footer, the supplier portal and the signup form — every
	// surface except the one people work in all day.
	test('a signed-in user can reach it from the app chrome', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('aside.sidebar').first()).toBeVisible();

		await page.getByRole('button', { name: 'Profile and account menu' }).click();
		await page.getByRole('link', { name: 'Legal & privacy' }).click();

		await expect(page.getByRole('heading', { level: 1, name: 'Legal' })).toBeVisible();
		await expect(page.getByRole('link', { name: 'Privacy Policy' }).first()).toHaveAttribute(
			'href',
			'/legal/privacy'
		);
	});

	test('and is sent back to the app, not to a sign-in form they already passed', async ({
		page,
	}) => {
		// The half of the header the anonymous block above cannot see. It read
		// "Sign in" → `/login` for everyone, so the employee who just used the
		// profile menu to get here was offered the form they had already filled
		// in — and `/login` does not bounce an authenticated user onward, so it
		// was a round trip to nowhere.
		//
		// Asserted on a DOCUMENT rather than the index because that is where a
		// reader is stranded longest, and both come from the one layout.
		await page.goto('/legal/privacy');

		const pill = page.getByRole('banner').getByRole('link', { name: 'Back to app' });
		await expect(pill).toHaveAttribute('href', '/');
		// Not merely relabelled: the wrong door must be gone, or the dead end
		// is still one mis-click away.
		await expect(
			page.getByRole('banner').getByRole('link', { name: 'Sign in' })
		).toHaveCount(0);

		// "Back to app" is a wider pill than the "Sign in" it replaced, and both
		// legal a11y specs measure 320px ANONYMOUSLY — so the widest state of
		// this header had no reflow coverage at all (WCAG 1.4.10).
		await page.setViewportSize({ width: 320, height: 720 });
		await expect(pill).toBeVisible();
		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth - document.documentElement.clientWidth
		);
		expect(overflow, 'signed-in legal header overflows at 320px').toBeLessThanOrEqual(1);

		await page.setViewportSize({ width: 1280, height: 720 });
		await pill.click();
		await expect(page.locator('aside.sidebar').first()).toBeVisible();
	});
});

test.describe('the legal set from inside the supplier portal', () => {
	// The third arrival, and the one the hardcoded header served worst. A
	// supplier is a data subject whose bank details and tax ID we hold; the
	// portal footer links these documents for exactly that reason. Sending them
	// to `/login` was not a dead end but the WRONG DOOR — the employee sign-in
	// form, which no password a vendor holds will ever open, on the surface
	// where they went looking for their privacy rights.
	//
	// Anonymous storage state, then a real portal sign-in: the two surfaces
	// keep separate localStorage keys (`auth_token` vs `portal_auth_token`), and
	// it is that separation the header reads.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('sends a signed-in vendor back to the portal, never to the employee login', async ({
		page,
	}) => {
		await acceptConsent(page);
		await page.goto('/portal/login');
		await page.locator('input[type="email"]').fill('supplier@portal.test');
		await page.locator('input[type="password"]').fill('demo');
		await page.locator('button[type="submit"]').click();
		await expect(page).toHaveURL(/\/portal\/?$/, { timeout: 15_000 });

		await page.goto('/legal/privacy');

		const banner = page.getByRole('banner');
		await expect(banner.getByRole('link', { name: 'Back to portal' })).toHaveAttribute(
			'href',
			'/portal'
		);
		await expect(banner.getByRole('link', { name: 'Sign in' })).toHaveCount(0);
	});
});
