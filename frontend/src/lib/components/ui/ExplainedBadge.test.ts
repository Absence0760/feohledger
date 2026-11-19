import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import ExplainedBadge from './ExplainedBadge.svelte';

/**
 * `ExplainedBadge` carries its explanation as text, not only as a tooltip.
 *
 * A `title` reaches neither a keyboard, a touch screen nor a screen reader, so
 * the sentence that says why a badge is there must be in the badge's own text
 * (a visually-hidden span). The tooltip stays for the pointer user. The page
 * that uses it, `/recurring`, is covered end to end by
 * `tests-e2e/recurring/gl-off-chart.spec.ts`.
 */

function renderBadge(props: {
	tone?: 'warning';
	variant?: string;
	label: string;
	explanation: string;
}): string {
	return render(ExplainedBadge, { props }).body;
}

/** Svelte's server render puts empty hydration comments (`<!---->`) between
 *  expressions; the patterns below allow them rather than strip them. */
const M = '(?:<!--[^>]*-->)*';

/** The text inside the badge's visually-hidden span, read by position: no
 *  markup is rewritten, so nothing here is a sanitizer. */
function hiddenText(html: string): string {
	const open = '<span class="visually-hidden';
	const start = html.indexOf('>', html.indexOf(open)) + 1;
	return html.slice(start, html.indexOf('</span>', start)).split('<!---->').join('');
}

describe('ExplainedBadge', () => {
	const props = {
		tone: 'warning' as const,
		variant: 'skipped',
		label: 'Not generating',
		explanation: '2 periods skipped — last 2026-11: no amount set'
	};

	it('puts the explanation in a visually-hidden span inside the badge', () => {
		const html = renderBadge(props);
		expect(html).toMatch(
			new RegExp(
				`<span class="badge warning skipped[^"]*"[^>]*>${M}Not generating${M}<span class="visually-hidden[^"]*">${M} 2 periods skipped — last 2026-11: no amount set${M}</span>${M}</span>`
			)
		);
	});

	it('keeps the explanation as the tooltip too', () => {
		expect(renderBadge(props)).toContain(
			'title="2 periods skipped — last 2026-11: no amount set"'
		);
	});

	it('separates the pill text from the explanation with a space', () => {
		// Read as text (a screen reader, a copied row), "Not generating" and
		// the explanation must not run together into one word.
		// Svelte trims whitespace at the edges of an element's children, so a
		// literal leading space in the markup would be dropped here.
		const hidden = hiddenText(renderBadge(props));
		expect(hidden).toBe(' 2 periods skipped — last 2026-11: no amount set');
		expect(`Not generating${hidden}`).toBe(
			'Not generating 2 periods skipped — last 2026-11: no amount set'
		);
	});
});
