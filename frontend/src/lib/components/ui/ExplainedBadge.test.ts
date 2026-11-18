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
	// Svelte's hydration markers are comments; drop them so the markup reads plainly.
	return render(ExplainedBadge, { props }).body.replace(/<!--.*?-->/g, '');
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
			/<span class="badge warning skipped[^"]*"[^>]*>Not generating<span class="visually-hidden[^"]*"> 2 periods skipped — last 2026-11: no amount set<\/span><\/span>/
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
		const text = renderBadge(props).replace(/<[^>]+>/g, '');
		expect(text).toBe('Not generating 2 periods skipped — last 2026-11: no amount set');
	});
});
