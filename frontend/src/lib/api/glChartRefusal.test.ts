/**
 * The client half of the GL-chart refusal (`backend/app/services/gl_chart.py`
 * `ChartRefusal.body`). Pins that a recognised body is stated from its code and
 * params in the reader's language, that every reason names its own codes, and
 * that anything this build does not recognise parses to `null` so the caller
 * renders the server's own sentence.
 */
import { afterEach, describe, expect, it } from 'vitest';
import {
	GL_CODES_OUTSIDE_CHART,
	localizeApiDetail,
	localizeGlChartRefusal,
	parseGlChartRefusal
} from './glChartRefusal';
import { en } from '../i18n/locales/en';
import { messages as de } from '../i18n/locales/de';
import { interpolate } from '../i18n/interpolate';
import { setActiveFormatLocale } from '../i18n/formatLocale';

const translator =
	(messages: Record<string, string>, locale: string) =>
	(key: string, params?: Record<string, string | number>) =>
		interpolate(messages[key], params, locale);

const enT = translator(en, 'en');
const deT = translator(de, 'de');

/** Shaped exactly as the backend's `ChartRefusal.body()` serializes. */
function body(overrides: Record<string, unknown> = {}) {
	return {
		code: GL_CODES_OUTSIDE_CHART,
		on_lines: false,
		foreign: [],
		retired: [],
		unknown: [],
		message: 'server sentence',
		...overrides
	};
}

afterEach(() => setActiveFormatLocale(undefined));

describe('parseGlChartRefusal', () => {
	it('reads the backend body', () => {
		expect(parseGlChartRefusal(body({ foreign: ['6000'], on_lines: true }))).toEqual({
			code: GL_CODES_OUTSIDE_CHART,
			on_lines: true,
			foreign: ['6000'],
			retired: [],
			unknown: [],
			message: 'server sentence'
		});
	});

	it('reads a CSV row error, which is the body plus `row`', () => {
		expect(parseGlChartRefusal({ ...body({ unknown: ['9999'] }), row: 4 })?.unknown).toEqual([
			'9999'
		]);
	});

	it.each([
		['a string detail', 'GL account refused'],
		['a validation list', [{ loc: ['body'], msg: 'bad' }]],
		['another code', body({ code: 'something_else', foreign: ['6000'] })],
		['a missing bucket', { code: GL_CODES_OUTSIDE_CHART, foreign: ['6000'], message: 'x' }],
		['a non-string code in a bucket', body({ retired: [6800] })],
		['every bucket empty', body()],
		['nothing', undefined]
	])('is null for %s', (_label, detail) => {
		expect(parseGlChartRefusal(detail)).toBeNull();
		expect(localizeApiDetail(detail, enT)).toBeNull();
	});
});

describe('localizeGlChartRefusal', () => {
	it('names each code under its own reason and asks for a code', () => {
		setActiveFormatLocale('en');
		const out = localizeApiDetail(body({ foreign: ['6000'] }), enT);
		expect(out).toBe(
			"GL account '6000' belongs to another entity's chart of accounts, not this invoice's. " +
				"Choose a code from the invoice's own chart — the shared accounts plus its entity's own."
		);
	});

	it('pluralizes, lists every code, and asks for an ACTIVE code', () => {
		setActiveFormatLocale('en');
		const out = localizeApiDetail(body({ retired: ['6800', '6900'], unknown: ['9999'] }), enT);
		expect(out).toBe(
			"GL accounts '6800', '6900' are retired in this invoice's chart. " +
				"GL account '9999' is not in this invoice's chart of accounts. " +
				"Choose an active code from the invoice's own chart — the shared accounts plus its entity's own."
		);
	});

	it('marks line-item codes', () => {
		const out = localizeApiDetail(body({ unknown: ['9998'], on_lines: true }), enT);
		expect(out?.startsWith('Line items: ')).toBe(true);
	});

	it("states the refusal in the reader's language, not the server's English", () => {
		setActiveFormatLocale('de');
		const refusal = parseGlChartRefusal(body({ retired: ['6800', '6900'], on_lines: true }))!;
		const out = localizeGlChartRefusal(refusal, deT);
		expect(out).toBe(
			"Positionen: Die Sachkonten '6800', '6900' sind im Kontenplan dieser Rechnung stillgelegt. " +
				'Wählen Sie ein aktives Konto aus dem eigenen Kontenplan der Rechnung – den gemeinsamen ' +
				'Konten plus denen ihrer Einheit.'
		);
		expect(out).not.toContain('server sentence');
	});
});
