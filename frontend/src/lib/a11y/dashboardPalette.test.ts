import { describe, expect, it } from 'vitest';
import dashboard from '../../routes/+page.svelte?raw';
import appCss from '../../app.css?raw';

/**
 * The dashboard's charts used to colour their bars from hex literals in the
 * SCRIPT (`PIPELINE_COLORS`, `AGING_COLORS`, a `'#888'` fallback), passed in
 * through `style=`. The stylesheet audit (`tokenPairing.test.ts`) reads `<style>`
 * blocks, so it never saw them, and they were tuned for the dark ground only —
 * the light theme (`docs/decisions.md` §289) drew the same values on white
 * (#635). The colours are now CSS keyed by `data-status` / `data-band`, from
 * palette tokens; this keeps a literal from coming back anywhere in the file.
 */
describe('dashboard palette', () => {
	const COLOUR_LITERAL = /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/gi;

	it('types no colour literal, in script, markup or style', () => {
		// One pass is not enough: removing a comment can join the text around it
		// into a new opener, so removal repeats until nothing changes.
		let withoutComments = dashboard;
		let previous: string;
		do {
			previous = withoutComments;
			withoutComments = withoutComments
				.replace(/<!--[\s\S]*?-->/g, '')
				.replace(/\/\*[\s\S]*?\*\//g, '')
				.replace(/^\s*\/\/.*$/gm, '');
		} while (withoutComments !== previous);
		expect([...withoutComments.matchAll(COLOUR_LITERAL)].map((m) => m[0])).toEqual([]);
	});

	it('draws no colour from a marketing-only token', () => {
		// The dark `:root` block ends with the public pages' own hues (gradient
		// stops and the ornament's cast shadow), after the comment that names
		// them. They are tuned for the landing page, not for the app's charts —
		// the review bar once filled with `--brand-violet` (#635).
		const start = appCss.indexOf("The brand's two accent hues");
		expect(start, 'app.css marketing-token block moved').toBeGreaterThan(-1);
		const block = appCss.slice(start, appCss.indexOf('}', start));
		const marketing = [...block.matchAll(/^\s*(--[a-z-]+)\s*:/gm)].map((m) => m[1]);
		expect(marketing).toContain('--brand-violet');
		expect(marketing.filter((t) => dashboard.includes(`var(${t}`))).toEqual([]);
	});

	it('names no English word in the trend tooltip', () => {
		// The bar title is a catalogue sentence with a plural, not "(n invoices)".
		expect(dashboard).not.toMatch(/\binvoices\)"/);
		expect(dashboard).toContain("m('dashboard.trend.barTitle'");
	});
});
