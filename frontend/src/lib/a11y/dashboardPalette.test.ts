import { describe, expect, it } from 'vitest';
import dashboard from '../../routes/+page.svelte?raw';

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
		const withoutComments = dashboard
			.replace(/<!--[\s\S]*?-->/g, '')
			.replace(/\/\*[\s\S]*?\*\//g, '')
			.replace(/^\s*\/\/.*$/gm, '');
		expect([...withoutComments.matchAll(COLOUR_LITERAL)].map((m) => m[0])).toEqual([]);
	});

	it('names no English word in the trend tooltip', () => {
		// The bar title is a catalogue sentence with a plural, not "(n invoices)".
		expect(dashboard).not.toMatch(/\binvoices\)"/);
		expect(dashboard).toContain("m('dashboard.trend.barTitle'");
	});
});
