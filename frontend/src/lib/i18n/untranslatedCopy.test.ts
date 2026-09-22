import { parse } from 'svelte/compiler';
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
const TRANSLATED = [
	'/src/lib/components/ConsentBanner.svelte',
	'/src/lib/components/ui/BulkBar.svelte',
	'/src/routes/credit-memos/+page.svelte',
	'/src/routes/exceptions/+page.svelte'
];

/** Attributes a human reads. `class` / `role` / `type` are not copy. */
const COPY_ATTRS = new Set(['aria-label', 'aria-description', 'title', 'placeholder', 'alt']);

/**
 * Two or more Latin letters in a row is the test for "a word". It lets through
 * punctuation, whitespace, dashes and digits, which are not copy a translator
 * would touch.
 */
const IS_COPY = /[A-Za-z]{2,}/;

/**
 * Parse the component and walk its TEMPLATE only.
 *
 * This used to strip `<script>`, `<style>` and comments with regexes and then
 * hand-scan the remainder. CodeQL was right to reject that
 * (`js/bad-tag-filter`, `js/incomplete-multi-character-sanitization`): an
 * anchor on `</script>` does not match `</script >`, and a scanner that can be
 * confused about what is markup is exactly the wrong thing to build a guard on.
 * Not exploitable here — this reads our own sources at test time, it is not a
 * sanitizer over untrusted input — but the correctness complaint behind the
 * alert is real, and `a11y/imageDragging.test.ts` had already settled the
 * answer for this repo: parse it.
 *
 * Parsing removes the class of bug rather than patching the pattern. Script and
 * style bodies live on `ast.instance` / `ast.module` / `ast.css`, so walking
 * `ast.fragment` puts them structurally out of reach — a `<script>` holding the
 * string `'</script >'`, or a comment that merely quotes markup, cannot leak
 * into the text this returns. The walk recurses over every own property rather
 * than enumerating block types, so copy inside `{#if}`, `{#each}`, `{#await}`
 * or `{#snippet}` is found without the guard knowing those blocks exist.
 */
function walkFragment(source: string, visit: (node: Record<string, unknown>) => void): void {
	const ast = parse(source, { modern: true }) as unknown as { fragment: unknown };
	const seen = new Set<unknown>();
	(function walk(node: unknown): void {
		if (node === null || typeof node !== 'object' || seen.has(node)) return;
		seen.add(node);
		if (Array.isArray(node)) {
			for (const child of node) walk(child);
			return;
		}
		const record = node as Record<string, unknown>;
		visit(record);
		for (const [key, value] of Object.entries(record)) {
			// `attributes` is NOT descended into. An attribute's value is itself
			// a list of `Text` nodes (`class="bulk-bar"` parses to exactly the
			// same node type as visible copy), so a generic recursion reports
			// every class name and role as untranslated text. Attributes are a
			// different question with a different answer — `literalCopyAttrs`
			// reads this key directly, and only for the five attributes a human
			// actually hears or reads.
			if (key === 'attributes') continue;
			walk(value);
		}
	})(ast.fragment);
}

/**
 * Every literal text node in the template — what a reader sees that did not
 * come from `m()`.
 *
 * An `{expression}` is an `ExpressionTag` node, never `Text`, so a keyed string
 * is skipped by the node type alone rather than by brace counting.
 */
function textNodes(source: string): string[] {
	const out: string[] = [];
	walkFragment(source, (node) => {
		if (node.type !== 'Text') return;
		const raw = typeof node.data === 'string' ? node.data : '';
		const text = raw.trim();
		if (text && IS_COPY.test(text)) out.push(text);
	});
	return out;
}

/**
 * Human-readable attribute values that are literals rather than `{m(…)}`.
 *
 * A literal attribute parses as a single `Text` value; `title={m('k')}` gives an
 * `ExpressionTag` and `title="a {b}"` gives a mixed array — neither is a bare
 * literal, and both are correctly ignored.
 */
function literalCopyAttrs(source: string): string[] {
	const out: string[] = [];
	walkFragment(source, (node) => {
		const attrs = node.attributes;
		if (!Array.isArray(attrs)) return;
		for (const raw of attrs) {
			const attr = raw as Record<string, unknown>;
			if (attr.type !== 'Attribute') continue;
			const name = typeof attr.name === 'string' ? attr.name : '';
			if (!COPY_ATTRS.has(name)) continue;
			const value = attr.value;
			if (!Array.isArray(value) || value.length !== 1) continue;
			const only = value[0] as Record<string, unknown> | undefined;
			if (!only || only.type !== 'Text') continue;
			const text = typeof only.data === 'string' ? only.data : '';
			if (IS_COPY.test(text)) out.push(`${name}="${text}"`);
		}
	});
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
		//
		// Three of these lines exist to pin what the old regex scanner got
		// wrong, and each one fails against it:
		//   - `</script >` (trailing space) — the strip anchored on `</script>`
		//     and missed it, leaking a script body into the scanned template.
		//   - a script string containing markup and English words — reachable
		//     only if script bodies are not structurally excluded.
		//   - copy nested in `{#if}` — the flat scan never knew blocks existed.
		const sample = [
			'<script lang="ts">const leak = \'</div> Not copy, this is script\';</script >',
			'{#if visible}',
			'<section role="region" aria-label="Cookie and privacy consent">',
			'<h2>Your privacy choices</h2>',
			'<p>{m(\'consent.bodyNecessary\')}</p>',
			'<button onclick={() => (open = !open)} title={m(\'consent.manage\')}>Accept all</button>',
			'</section>',
			'{/if}',
			'<style>.consent { color: red; }</style>'
		].join('\n');
		// `Not copy, this is script` is absent: the script body is a different
		// branch of the AST, not a region this scanner had to strip.
		expect(textNodes(sample)).toEqual(['Your privacy choices', 'Accept all']);
		// `title={m(…)}` is an ExpressionTag, so only the bare literal is named.
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

describe('the "Select all N matching" bulk affordance', () => {
	/**
	 * One string, six call sites, and five of them were literals.
	 *
	 * `/exceptions`, `/invoices`, `/vendors`, `/contracts` and `/expenses` each
	 * inlined `` `Select all ${total} matching` `` and `All matching selected`,
	 * on pages `frontend/docs/i18n.md` listed as fully extracted, while
	 * `/payments` kept the only keyed copy under a PRIVATE `payments.queue.*`
	 * pair the other five could not borrow — `pagedListFooter.test.ts`'
	 * per-namespace pairing is the precedent against reaching into a sibling's
	 * keys (`docs/decisions.md` §155).
	 *
	 * The owner is `common.*`, not `ui/BulkBar.svelte`: two of the six
	 * (`/invoices`, `/payments`) render their own bar and never import that
	 * component, so moving the copy into it would have reached four of six and
	 * left the split that started this. The wording is identical everywhere and
	 * the only variable is `{total}` — the `common.all` / `common.loading` case.
	 *
	 * So the guard is on the STRING, not on the component: a seventh caller that
	 * inlines the English, or re-privatises the key under its own namespace,
	 * fails here wherever it lives.
	 */

	/** The English literal, in the two shapes it shipped in. */
	const LITERALS = [/`Select all \$\{[^`]*\} matching`/, /All matching selected/];

	/** A `…​.selectAllMatching` / `…​.allMatchingSelected` key that is not common.*. */
	const PRIVATE_KEY = /'(?!common\.)[A-Za-z0-9.]+\.(?:selectAllMatching|allMatchingSelected)'/;

	/** Every surface that offers the affordance. All six migrated together. */
	const CALL_SITES = [
		'/src/routes/contracts/+page.svelte',
		'/src/routes/exceptions/+page.svelte',
		'/src/routes/expenses/+page.svelte',
		'/src/routes/invoices/+page.svelte',
		'/src/routes/payments/+page.svelte',
		'/src/routes/vendors/+page.svelte'
	];

	it('has the shared pair in the English catalogue', () => {
		expect(en).toHaveProperty('common.selectAllMatching');
		expect(en).toHaveProperty('common.allMatchingSelected');
		expect((en as Record<string, string>)['common.selectAllMatching']).toContain('{total}');
	});

	it('is nowhere written as an English literal', () => {
		const offenders = Object.entries(RAW)
			.filter(([, source]) => LITERALS.some((re) => re.test(source)))
			.map(([path]) => path)
			.sort();
		expect(
			offenders,
			'render m(\'common.selectAllMatching\', { total }) / m(\'common.allMatchingSelected\') instead'
		).toEqual([]);
	});

	it('is nowhere re-privatised under a route namespace', () => {
		const offenders = Object.entries(RAW)
			.filter(([, source]) => PRIVATE_KEY.test(source))
			.map(([path]) => path)
			.sort();
		expect(
			offenders,
			'the affordance is shared copy — it belongs to common.*, not to one route'
		).toEqual([]);
	});

	it('pairs the two halves in every file that offers it', () => {
		// The pagedListFooter rule, applied to this pair: a bar that can resolve
		// the whole filtered set must also be able to say it did, or the button
		// stays on screen over a selection that already reaches past the page.
		const unpaired = Object.entries(RAW)
			.filter(([, s]) => s.includes("'common.selectAllMatching'"))
			.filter(([, s]) => !s.includes("'common.allMatchingSelected'"))
			.map(([path]) => path)
			.sort();
		expect(unpaired, 'these offer "Select all N matching" with no "All matching selected"').toEqual(
			[]
		);
	});

	it('keeps all six call sites on the shared pair', () => {
		// Named explicitly so a revert on any ONE of them is a failure. Five
		// drifting from a sixth is how this started.
		for (const path of CALL_SITES) {
			expect(RAW[path], `${path} not found through import.meta.glob`).toBeTypeOf('string');
			expect(RAW[path], `${path} lost common.selectAllMatching`).toContain(
				"'common.selectAllMatching'"
			);
			expect(RAW[path], `${path} lost common.allMatchingSelected`).toContain(
				"'common.allMatchingSelected'"
			);
		}
	});
});
