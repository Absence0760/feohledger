import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { expect, test } from '../fixtures/helpers';
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
	}

	test('the index links every document', async ({ page }) => {
		await page.goto('/legal');
		await expect(page.getByRole('heading', { level: 1, name: 'Legal' })).toBeVisible();

		for (const { path, title } of PAGES) {
			await expect(page.getByRole('link', { name: title }).first()).toHaveAttribute('href', path);
		}
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
			'/legal/cookies': [/strictly necessary/i, /withdraw/i, /localStorage/i]
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
	});

	test('the supplier portal gives a vendor a route to our privacy terms', async ({
		page,
		tenantSlug
	}) => {
		// A supplier is a data subject whose bank details and tax ID we hold, and
		// the portal is the only surface they ever see. The footer used to render
		// only when the TENANT had configured its own URLs, so an unconfigured
		// tenant's suppliers had no route to us at all.
		await page.goto(`${tenantOrigin(tenantSlug)}/portal/login`);
		await expect(page.getByRole('link', { name: 'Privacy Policy' })).toHaveAttribute(
			'href',
			'/legal/privacy'
		);
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
