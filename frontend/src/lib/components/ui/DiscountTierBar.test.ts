import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

// The real store imports `$app/env`; the English catalogue + the real
// interpolator stand in for it (ICU plurals included).
vi.mock('#lib/i18n/store.svelte.ts', async () => {
	const { en } = await import('#lib/i18n/locales/en.ts');
	const { interpolate } = await import('#lib/i18n/interpolate.ts');
	return {
		m: (key: string, params?: Record<string, string | number>) =>
			interpolate((en as Record<string, string>)[key] ?? key, params, 'en')
	};
});

const { default: DiscountTierBar } = await import('./DiscountTierBar.svelte');

/**
 * The tier chip's percent goes through `formatPercent` (the locale owns the
 * sign's spacing and the decimal separator), and its tooltip and day unit come
 * from the catalogue — neither is English pinned into every locale.
 */

afterEach(() => setActiveFormatLocale(undefined));

/** Svelte's server render leaves hydration comments between expressions. */
function text(html: string): string {
	return html.split('<!---->').join('').split('<!--[-->').join('').split('<!--]-->').join('');
}

describe('DiscountTierBar', () => {
	it('formats each tier through the locale-aware percent formatter', () => {
		setActiveFormatLocale('en-US');
		const html = text(
			render(DiscountTierBar, {
				props: {
					tiers: [
						{ days: 20, percent: 1 },
						{ days: 10, percent: 2.5 }
					]
				}
			}).body
		);
		// Soonest deadline first.
		expect(html.indexOf('2.5% / 10d')).toBeGreaterThan(-1);
		expect(html.indexOf('2.5% / 10d')).toBeLessThan(html.indexOf('1% / 20d'));
		expect(html).toContain('title="2.5% off if paid within 10 days"');
	});

	it('pluralises the tooltip’s day count', () => {
		setActiveFormatLocale('en-US');
		const html = text(render(DiscountTierBar, { props: { tiers: [{ days: 1, percent: 2 }] } }).body);
		expect(html).toContain('title="2% off if paid within 1 day"');
	});

	it('follows the active number locale for the figure', () => {
		setActiveFormatLocale('fr-FR');
		const html = text(render(DiscountTierBar, { props: { tiers: [{ days: 10, percent: 2.5 }] } }).body);
		// French: comma decimal, a (no-break) space before the sign — which
		// space is the ICU data's call, so either is accepted.
		expect(html).toMatch(/>2,5[\u00a0\u202f]% \/ 10d</);
	});

	it('renders a dash when there are no tiers', () => {
		const html = render(DiscountTierBar, { props: { tiers: [] } }).body;
		expect(html).toContain('tier-none');
	});
});
