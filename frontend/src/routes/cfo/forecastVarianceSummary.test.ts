import { describe, expect, it } from 'vitest';

import {
	collectForecastEntries,
	unconvertedTotal,
	variancePctLabel,
	varianceTone
} from './forecastVarianceSummary';
import type { ForecastVariance, ForecastVarianceRow } from '#lib/types/analytics.ts';

function row(over: Partial<ForecastVarianceRow> = {}): ForecastVarianceRow {
	return {
		month: '2026-05',
		forecast: '100000.00',
		actual: '120000.00',
		variance: '20000.00',
		variance_pct: 20,
		unconverted_count: 0,
		...over
	};
}

function result(rows: ForecastVarianceRow[]): ForecastVariance {
	return { reporting_currency: 'USD', rows };
}

describe('collectForecastEntries', () => {
	it('sends the typed decimal text verbatim', () => {
		// The string that goes out is the string that came in — no `Number`
		// anywhere on the path, so no rounding can have happened before the
		// backend parses it as `Decimal`.
		const out = collectForecastEntries([{ month: '2026-05', forecast: '100000.55' }]);
		expect(out).toEqual({ ok: true, rows: [{ month: '2026-05', forecast: '100000.55' }] });
	});

	it('trims but never repairs', () => {
		const out = collectForecastEntries([{ month: ' 2026-05 ', forecast: '  250000  ' }]);
		expect(out).toEqual({ ok: true, rows: [{ month: '2026-05', forecast: '250000' }] });
	});

	it('drops a row left entirely blank', () => {
		const out = collectForecastEntries([
			{ month: '2026-05', forecast: '1000' },
			{ month: '', forecast: '' }
		]);
		expect(out).toEqual({ ok: true, rows: [{ month: '2026-05', forecast: '1000' }] });
	});

	it('refuses unreadable money rather than sending a repaired figure', () => {
		// `$1,200.50`, `1 200`, `1.2e5`, a trailing dot and a leading `+` are all
		// refused — coercing any of them would make the variance measure against
		// a number the CFO never typed.
		for (const forecast of ['$1,200.50', '1 200', '1.2e5', '1200.', '+1200', 'abc']) {
			expect(collectForecastEntries([{ month: '2026-05', forecast }])).toEqual({
				ok: false,
				reason: 'amount'
			});
		}
	});

	it('refuses a month with a forecast but no amount — never a silent 0', () => {
		expect(collectForecastEntries([{ month: '2026-05', forecast: '' }])).toEqual({
			ok: false,
			reason: 'amount'
		});
	});

	it('refuses an amount with no month rather than dropping the row', () => {
		// Dropping it would report a variance for fewer months than were typed.
		expect(collectForecastEntries([{ month: '', forecast: '1000' }])).toEqual({
			ok: false,
			reason: 'month'
		});
	});

	it('refuses a month outside 01–12, which the API answers 422 for', () => {
		for (const month of ['2026-13', '2026-00', '2026/05', '26-05', '2026-5']) {
			expect(collectForecastEntries([{ month, forecast: '1000' }])).toEqual({
				ok: false,
				reason: 'month'
			});
		}
	});

	it('refuses an empty editor', () => {
		expect(collectForecastEntries([])).toEqual({ ok: false, reason: 'empty' });
		expect(collectForecastEntries([{ month: '', forecast: '' }])).toEqual({
			ok: false,
			reason: 'empty'
		});
	});
});

describe('unconvertedTotal', () => {
	it('is 0 before anything is submitted', () => {
		expect(unconvertedTotal(null)).toBe(0);
	});

	it('is 0 when every month converted', () => {
		expect(unconvertedTotal(result([row(), row({ month: '2026-06' })]))).toBe(0);
	});

	it('folds the per-month counts', () => {
		// A COUNT may cross months; the amounts beside it may not.
		expect(
			unconvertedTotal(
				result([
					row({ unconverted_count: 2 }),
					row({ month: '2026-06', unconverted_count: 1 })
				])
			)
		).toBe(3);
	});
});

describe('variancePctLabel', () => {
	it('renders a signed percentage', () => {
		expect(variancePctLabel(row({ variance_pct: 20 }))).toBe('+20%');
		expect(variancePctLabel(row({ variance_pct: -12.5 }))).toBe('-12.5%');
	});

	it('keeps a genuine zero when a forecast was actually given', () => {
		expect(variancePctLabel(row({ variance_pct: 0 }))).toBe('0%');
	});

	it('returns null for a zero forecast — never "0%"', () => {
		// The backend emits `variance_pct: 0` whenever the forecast is not
		// positive, because a percentage of zero is not computable. `0%` reads as
		// "exactly on plan", the opposite of "there was no plan".
		expect(variancePctLabel(row({ forecast: '0.00', variance_pct: 0 }))).toBeNull();
		expect(variancePctLabel(row({ forecast: '', variance_pct: 0 }))).toBeNull();
	});

	it('refuses a non-finite percentage rather than rendering NaN%', () => {
		expect(variancePctLabel(row({ variance_pct: Number.NaN }))).toBeNull();
	});
});

describe('varianceTone', () => {
	it('reads the backend subtraction, never performing one', () => {
		expect(varianceTone(row({ variance: '20000.00' }))).toBe('over');
		expect(varianceTone(row({ variance: '-20000.00' }))).toBe('under');
		expect(varianceTone(row({ variance: '0.00' }))).toBe('level');
	});

	it('is level for an unreadable figure rather than guessing a direction', () => {
		expect(varianceTone(row({ variance: '' }))).toBe('level');
	});
});
