import { describe, expect, it } from 'vitest';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Source-scan guard: `/vendors/screening` carries no hardcoded user-facing
 * English.
 *
 * Round 28 keyed this route's shared label MAPS (screening status, risk level,
 * hit category) and left the page's own copy in place, so a German or Japanese
 * reviewer got translated pills sitting inside an English page — and the
 * strings that matter most here are the ones on the two controls that stop a
 * payment (Block / Re-screen) and the three history states, where "we could
 * not look" must not read as "there is nothing to see".
 *
 * Two halves, because each catches what the other cannot:
 *
 *  1. The exact literals the extraction removed. Cheap, exhaustive for what
 *     shipped, and a revert fails loudly rather than silently.
 *  2. A forward rule — every user-facing ATTRIBUTE must be an expression, not a
 *     quoted literal — which catches copy nobody has written yet.
 *
 * It reads the file through Vite's `import.meta.glob` for the same reason
 * `lib/a11y/tokenPairing.test.ts` does: the frontend deliberately carries no
 * `@types/node`, so there is no `fs`.
 */

const ROUTE = '/src/routes/vendors/screening/+page.svelte';

const RAW = import.meta.glob('/src/routes/vendors/screening/+page.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const RAW_SOURCE = RAW[ROUTE];

/**
 * Comments are prose ABOUT the code, and this route's are unusually rich —
 * several quote the very labels they explain ("the `Payments blocked` KPI…",
 * the note recording why the `capitalize` transform is gone). Scanning them
 * would make the guard fire on its own documentation, and the pressure that
 * puts on an author is to delete the explanation. Strips `/* … *\/`, `<!-- …
 * -->`, and whole-line `//`; a trailing `//` after code is left alone so a URL
 * inside a string can't be truncated.
 */
/**
 * Apply `pattern` until the text stops changing.
 *
 * One pass over a PAIRED multi-character delimiter can re-form the delimiter it
 * just removed: `<!<!-- x -->-- y -->` has its inner comment stripped and the
 * surviving halves close up into `<!-- y -->`. Re-running to a fixed point is
 * the documented remedy for that class, and it is kept to a single `replace`
 * per call because that is the shape the check recognises.
 */
function replaceUntilStable(input: string, pattern: RegExp): string {
	let previous: string;
	let out = input;
	do {
		previous = out;
		out = out.replace(pattern, '');
	} while (out !== previous);
	return out;
}

function stripComments(source: string): string {
	let out = replaceUntilStable(source, /\/\*[\s\S]*?\*\//g);
	out = replaceUntilStable(out, /<!--[\s\S]*?-->/g);
	// Line-anchored and unpaired, so it cannot re-form its own opener.
	return out.replace(/^[ \t]*\/\/.*$/gm, '');
}

const SOURCE = stripComments(RAW_SOURCE);

/** The template — everything after the closing `</script>`, minus `<style>`. */
function template(source: string): string {
	const afterScript = source.slice(source.indexOf('</script>') + '</script>'.length);
	const styleAt = afterScript.lastIndexOf('<style>');
	return styleAt === -1 ? afterScript : afterScript.slice(0, styleAt);
}

/**
 * Every literal this route used to render. Each is now a catalogue key, so its
 * presence anywhere in the source means the extraction was undone — or that a
 * new instance of the same copy was pasted back in beside the key.
 */
const REMOVED_LITERALS = [
	'"Screening Review Queue"',
	"'Refreshing…'",
	'"Sanctions matches"',
	'"Needs review"',
	'"Payments blocked"',
	'"Count unavailable"',
	'"All vendors, not just this queue"',
	'"Search vendor or matched list…"',
	'"Search screening review queue"',
	"'Could not load the review queue.'",
	"'No vendors match your search.'",
	"'No vendors are awaiting screening review. 🎉'",
	'`Review screening for ',
	'"Vendor screening review"',
	'<dt>Screening status</dt>',
	'<dt>Risk level</dt>',
	'<dt>Hit categories</dt>',
	'<dt>Payments</dt>',
	"'Re-screening…'",
	"'Re-screen now'",
	'"Reason (optional)"',
	'"Block reason"',
	"'Unblock payments'",
	"'Block payments'",
	"You don't have permission to block or re-screen this vendor.</p>",
	'<h3>Screening history</h3>',
	'No screening history yet.</p>',
	// A bare text node, not a quoted literal — so neither the attribute rule nor
	// the rest of this list would catch a revert of just this one interpolation.
	'· score ',
	"'Failed to load the screening review queue'",
	"'Failed to load screening history'",
	"'Vendor re-screened'",
	"'Re-screen failed'",
	"'Action failed'",
	// The English-only derivation the verdict badge used, and the CSS that
	// dressed it up — `capitalize` would title-case a translated phrase.
	"h.result.replace(/_/g, ' ')",
	'text-transform: capitalize'
];

/** Attributes whose value a person reads or a screen reader announces. */
const USER_FACING_ATTRS = ['placeholder', 'aria-label', 'ariaLabel', 'label', 'title', 'empty'];

describe('/vendors/screening copy is extracted', () => {
	it('is readable as a source file', () => {
		expect(RAW_SOURCE, `${ROUTE} not found in the glob`).toBeTypeOf('string');
		expect(SOURCE.length).toBeGreaterThan(0);
		// The comment stripper must not eat the code: a bad regex that swallowed
		// the file would make every other assertion here pass vacuously.
		expect(SOURCE).toContain('<PageHeader');
		expect(SOURCE).toContain('data-testid="screening-history-empty"');
	});

	it('carries none of the literals the extraction replaced', () => {
		const survivors = REMOVED_LITERALS.filter((literal) => SOURCE.includes(literal));
		expect(
			survivors,
			'these strings are catalogue keys now — render them through m(), not inline'
		).toEqual([]);
	});

	it('passes every user-facing attribute as an expression, not a literal', () => {
		const tpl = template(SOURCE);
		const offenders: string[] = [];
		for (const attr of USER_FACING_ATTRS) {
			// `attr="literal"` / `attr='literal'`. An extracted value is
			// `attr={m('…')}`, which has no quote directly after the `=`.
			const re = new RegExp(`(?<![\\w-])${attr}=["']([^"']*)["']`, 'g');
			for (const match of tpl.matchAll(re)) {
				offenders.push(`${attr}="${match[1]}"`);
			}
		}
		expect(offenders, 'extract these into keys and render them with m()').toEqual([]);
	});

	it('names catalogue keys that all exist', () => {
		// `m()` is typed on `MessageKey`, so `pnpm check` already catches a typo.
		// This is the runtime half, and it also proves the route actually reads
		// the block this slice added rather than leaving it orphaned.
		const keys = [...SOURCE.matchAll(/m\('([^']+)'/g)].map((match) => match[1]);
		expect(keys.length, 'the route should read the catalogue').toBeGreaterThan(30);
		const missing = keys.filter((key) => !(key in en));
		expect(missing, 'keys referenced by the route but absent from en').toEqual([]);
		// The route's own namespace, distinct from the shared label maps above it.
		expect(keys.some((key) => key.startsWith('vendors.screening.queue.'))).toBe(true);
	});
});
