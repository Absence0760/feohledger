import { describe, expect, it } from 'vitest';
import { en } from './locales/en';

/**
 * Source-scan guard: a surface declared translated must not carry English in
 * its template.
 *
 * `listJoinAudit.test.ts` and `pagedListFooter.test.ts` established the shape —
 * read the tree through Vite's `import.meta.glob` (the frontend deliberately
 * carries no `@types/node`) and fail on a pattern rather than on a review.
 * Both of those already glob `/src/**​/*.svelte`, so components were never
 * outside their reach; what was missing was a guard that asks the question at
 * all for a component. `lib/components/ConsentBanner.svelte` shipped with **no
 * `m()` call in it** — title, both category names, both button labels, the
 * `aria-label` and the Cookie Notice link text all literals — while
 * `docs/i18n.md` listed the surfaces around it as extracted. It mounts from the
 * root layout, so it was the first thing a `de` / `es` / `fr` / `ja` / `pt-BR`
 * visitor saw and the one surface they had to interact with to dismiss; it is
 * also the ePrivacy Art 5(3) consent gate, where consent has to be *informed*.
 * A per-route audit never reached it because it is not a route.
 *
 * So the roster below is per-COMPONENT and opt-in: adding a file to
 * `TRANSLATED` is how a slice locks its work in, and from then on any bare
 * Latin-letter text node or human-readable attribute literal in that file's
 * template fails here. It is deliberately not "every component" — most of the
 * tree is still English by the documented incremental path, and a guard that
 * failed on all of it would have to be disabled to be useful.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/**
 * Components whose template is fully extracted, and must stay that way.
 *
 * Routes are welcome here too — the split is not "components only", it is
 * "surfaces someone has finished".
 */
const TRANSLATED = ['/src/lib/components/ConsentBanner.svelte'];

/** Attributes a human reads. `class` / `role` / `type` are not copy. */
const COPY_ATTRS = /\s(?:aria-label|aria-description|title|placeholder|alt)="([^"]*)"/g;

/** Strip `<script>`, `<style>` and HTML comments — only the template is copy. */
function template(source: string): string {
	return source
		.replace(/<script[\s\S]*?<\/script>/g, '')
		.replace(/<style[\s\S]*?<\/style>/g, '')
		.replace(/<!--[\s\S]*?-->/g, '');
}

/**
 * Every literal text node in a Svelte template — what a reader sees that did
 * not come from `m()`.
 *
 * Hand-rolled rather than regexed because the two things being skipped nest:
 * a tag can hold `{…}` expressions containing `>` and quotes, and a `{…}`
 * expression can hold `<` inside a string. A brace/quote-aware scan is the only
 * way to tell a tag from a comparison operator.
 */
function textNodes(source: string): string[] {
	const s = template(source);
	const out: string[] = [];
	let buf = '';
	let i = 0;

	while (i < s.length) {
		if (s[i] === '<') {
			if (buf.trim()) out.push(buf.trim());
			buf = '';
			let depth = 0;
			let quote = '';
			while (i < s.length) {
				const c = s[i];
				if (quote) {
					if (c === quote) quote = '';
				} else if (c === '"' || c === "'") {
					quote = c;
				} else if (c === '{') {
					depth++;
				} else if (c === '}') {
					depth--;
				} else if (c === '>' && depth === 0) {
					i++;
					break;
				}
				i++;
			}
			continue;
		}

		if (s[i] === '{') {
			if (buf.trim()) out.push(buf.trim());
			buf = '';
			let depth = 0;
			while (i < s.length) {
				if (s[i] === '{') depth++;
				else if (s[i] === '}') {
					depth--;
					if (depth === 0) {
						i++;
						break;
					}
				}
				i++;
			}
			continue;
		}

		buf += s[i];
		i++;
	}
	if (buf.trim()) out.push(buf.trim());

	// Two or more Latin letters in a row is the test for "a word". It lets
	// through punctuation, `&nbsp;`-free whitespace, dashes and digits, which
	// are not copy a translator would touch.
	return out.filter((t) => /[A-Za-z]{2,}/.test(t));
}

/** Human-readable attribute values that are literals rather than `{m(…)}`. */
function literalCopyAttrs(source: string): string[] {
	const out: string[] = [];
	for (const match of template(source).matchAll(COPY_ATTRS)) {
		if (/[A-Za-z]{2,}/.test(match[1])) out.push(match[0].trim());
	}
	return out;
}

describe('translated surfaces carry no literal copy', () => {
	it('reads the component tree', () => {
		expect(Object.keys(RAW).length).toBeGreaterThan(50);
	});

	it('detects the literals it claims to', () => {
		// The detector has to be shown to fire, or a roster of green files says
		// nothing. This is the shape the banner shipped in: a bare heading, a
		// bare button label, a hardcoded aria-label — beside the `{…}` blocks,
		// `=>` arrows and quoted attributes that must NOT be mistaken for copy.
		const sample = [
			'<script lang="ts">const label = \'not copy, this is script\';</script>',
			'{#if visible}',
			'<section role="region" aria-label="Cookie and privacy consent">',
			'<h2>Your privacy choices</h2>',
			'<p>{m(\'consent.bodyNecessary\')}</p>',
			'<button onclick={() => (open = !open)}>Accept all</button>',
			'{/if}',
			'<style>.consent { color: red; }</style>'
		].join('\n');
		expect(textNodes(sample)).toEqual(['Your privacy choices', 'Accept all']);
		expect(literalCopyAttrs(sample)).toEqual(['aria-label="Cookie and privacy consent"']);
	});

	for (const path of TRANSLATED) {
		it(`${path} renders every word through m()`, () => {
			const source = RAW[path];
			expect(source, `${path} not found through import.meta.glob`).toBeTypeOf('string');
			expect(
				textNodes(source),
				`${path} renders literal text. Add a key to locales/en.ts (and all five other catalogues) and render it with m().`
			).toEqual([]);
			expect(
				literalCopyAttrs(source),
				`${path} has a hardcoded human-readable attribute. An aria-label is read aloud; key it like any other string.`
			).toEqual([]);
			expect(source, `${path} should call m()`).toMatch(/\bm\(/);
		});
	}
});

describe('consent banner key roster', () => {
	// The banner is the ePrivacy Art 5(3) gate: an unrendered key is copy a
	// reader was supposed to see, and an orphaned one is copy that quietly left
	// the dialog while five translators still maintain it.
	const BANNER = '/src/lib/components/ConsentBanner.svelte';
	const consentKeys = Object.keys(en).filter((k) => k.startsWith('consent.'));

	it('has a consent namespace at all', () => {
		expect(consentKeys.length).toBeGreaterThan(8);
	});

	it('renders every consent.* key in the banner', () => {
		const source = RAW[BANNER];
		const unused = consentKeys.filter((key) => !source.includes(`'${key}'`));
		expect(unused, 'these consent keys have no call site — render them or prune them').toEqual([]);
	});

	it('resolves every consent.* key the banner names', () => {
		const used = [...RAW[BANNER].matchAll(/m\(\s*'(consent\.[A-Za-z0-9.]+)'/g)].map((x) => x[1]);
		expect(used.length, 'the banner stopped calling m() with consent keys').toBeGreaterThan(8);
		for (const key of used) expect(en, `${key} is missing from en.ts`).toHaveProperty(key);
	});
});
