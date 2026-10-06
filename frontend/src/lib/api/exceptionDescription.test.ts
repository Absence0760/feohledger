/**
 * The queue's half of the exception-description contract (migration 0103).
 *
 * Pins the fallback order `exceptionDescription.ts` documents: no code → the
 * server's sentence; an unrenderable code → the WHOLE server sentence with no
 * duplicate findings list; a renderable composite → a localized frame plus each
 * finding localized (or its own English when this build predates its code).
 */
import { describe, expect, it } from 'vitest';
import { exceptionDescriptionText, exceptionDescriptionTitle } from './exceptionDescription';
import { en } from '../i18n/locales/en';
import { messages as de } from '../i18n/locales/de';
import { interpolate } from '../i18n/interpolate';

function translator(catalogue: Record<string, string>, locale: string) {
	return (key: string, params?: Record<string, string | number>) =>
		interpolate(catalogue[key], params, locale);
}

const translateEn = translator(en, 'en');
const translateDe = translator(de, 'de');

const LINE_OVER = {
	code: 'price_variance_over',
	params: {
		deltaPct: '+20.0',
		item: 'Widget',
		unitPrice: '12.00',
		baselineUnitPrice: '10.00',
		currency: 'ZAR'
	},
	message: "Unit price +20.0% over this vendor's baseline for Widget (12.00 ZAR vs 10.00 ZAR)"
};

describe('exceptionDescriptionText', () => {
	it('renders a human-authored description (no code) as written', () => {
		expect(
			exceptionDescriptionText({ description: 'Wrong PO, please resubmit' }, translateDe)
		).toEqual({ summary: 'Wrong PO, please resubmit', findings: [] });
	});

	it('returns null when there is nothing to show', () => {
		expect(exceptionDescriptionText({ description: null }, translateEn)).toBeNull();
	});

	it('localizes a single finding in the warning’s own wording, no hardcoded $', () => {
		const out = exceptionDescriptionText(
			{
				description: 'Round amount: 5000.00 ZAR',
				description_code: 'round_amount',
				description_params: { amount: '5000.00', currency: 'ZAR' }
			},
			translateDe
		);
		expect(out?.findings).toEqual([]);
		expect(out?.summary).toMatch(/^Runder Betrag: /);
		expect(out?.summary).toContain('ZAR');
		expect(out?.summary).not.toContain('$');
	});

	it('decomposes a composite into a localized frame plus each finding', () => {
		const out = exceptionDescriptionText(
			{
				description: 'joined English fallback',
				description_code: 'exception.price_variance_findings',
				description_params: {
					count: 2,
					findings: [
						LINE_OVER,
						// A finding whose code this build predates keeps its own English.
						{ code: 'price_variance_sideways', params: {}, message: 'Server-only finding' }
					]
				}
			},
			translateDe
		);
		expect(out?.summary).toContain('2 Positionen');
		expect(out?.findings).toHaveLength(2);
		expect(out?.findings[0]).toMatch(/^Einzelpreis .* für Widget /);
		expect(out?.findings[0]).not.toContain('$');
		expect(out?.findings[1]).toBe('Server-only finding');
	});

	it('falls back to the whole description — without a duplicate list — for an unknown frame', () => {
		const out = exceptionDescriptionText(
			{
				description: 'Frame from the future: A; B',
				description_code: 'exception.from_the_future',
				description_params: { findings: [LINE_OVER] }
			},
			translateEn
		);
		expect(out).toEqual({ summary: 'Frame from the future: A; B', findings: [] });
	});

	it('falls back when a known code arrives with a missing parameter', () => {
		const out = exceptionDescriptionText(
			{
				description: 'Round amount: 5000.00 ZAR',
				description_code: 'round_amount',
				description_params: { currency: 'ZAR' }
			},
			translateEn
		);
		expect(out).toEqual({ summary: 'Round amount: 5000.00 ZAR', findings: [] });
	});
});

describe('exceptionDescriptionTitle', () => {
	it('puts the summary and each finding on its own line', () => {
		expect(exceptionDescriptionTitle({ summary: 'S', findings: ['A', 'B'] })).toBe('S\nA\nB');
	});
});
