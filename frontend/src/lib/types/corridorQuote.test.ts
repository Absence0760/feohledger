import { describe, it, expect } from 'vitest';
import {
	allQuotes,
	formatFeeRate,
	isRankedQuote,
	quoteReasonKey,
	unrankedQuotes,
	type CorridorQuote,
	type CorridorQuoteComparison
} from './corridorQuote';
import { en } from '#lib/i18n/locales/en.ts';

function quote(over: Partial<CorridorQuote> = {}): CorridorQuote {
	return {
		provider: 'mock',
		method: 'ach',
		available: true,
		unavailable_reason: null,
		total_cost: '1.50',
		flat_fee: '0.50',
		pct_fee: '0.002',
		eta_business_days: 2,
		fx_rate: null,
		...over
	};
}

function comparison(over: Partial<CorridorQuoteComparison> = {}): CorridorQuoteComparison {
	return {
		invoice_id: 'i-1',
		mode: 'cheapest',
		currency: 'USD',
		amount: '500.00',
		winner: quote(),
		runners_up: [],
		savings_vs_runner_up: '0',
		advisory: true,
		...over
	};
}

describe('quote partitioning', () => {
	it('keeps the winner and every runner-up, ranked or not', () => {
		const skipped = quote({
			provider: 'modern_treasury',
			available: false,
			unavailable_reason: 'no_quote_endpoint',
			total_cost: null
		});
		const cmp = comparison({ runners_up: [quote({ provider: 'increase' }), skipped] });

		// The list the UI renders is the WHOLE response — a non-quoting adapter
		// dropped here would silently vanish from the screen too, which is the
		// exact failure the modal exists to avoid.
		expect(allQuotes(cmp).map((q) => q.provider)).toEqual([
			'mock',
			'increase',
			'modern_treasury'
		]);
		expect(unrankedQuotes(cmp).map((q) => q.provider)).toEqual(['modern_treasury']);
		expect(isRankedQuote(skipped)).toBe(false);
	});
});

describe('quoteReasonKey', () => {
	it('maps every machine code the backend can emit to a real catalogue key', () => {
		const codes = [
			'no_quote_endpoint',
			'provider_not_supported',
			'disabled_in_config',
			'not_configured',
			'provider_not_configured:KeyError',
			'adapter_error:TimeoutException'
		];
		for (const code of codes) {
			const key = quoteReasonKey(code);
			expect(key, `no message key for reason "${code}"`).toBeTruthy();
			expect(Object.keys(en)).toContain(key as string);
		}
	});

	it('returns null for an adapter-authored sentence so it renders verbatim', () => {
		// Concrete adapters emit their own prose ("method 'sepa' not supported by
		// column"). That is more informative than any bucket we could invent, so
		// the caller renders it as-is rather than mapping it to a generic key.
		expect(quoteReasonKey("method 'sepa' not supported by column")).toBeNull();
		expect(quoteReasonKey(null)).toBeNull();
		expect(quoteReasonKey('')).toBeNull();
	});
});

describe('formatFeeRate', () => {
	it('renders a ratio as a percentage', () => {
		expect(formatFeeRate('0.005', 'en-US')).toBe('0.5%');
		expect(formatFeeRate('0.025', 'en-US')).toBe('2.5%');
		expect(formatFeeRate('0', 'en-US')).toBe('0%');
	});

	it('guards non-finite / missing provider input instead of rendering NaN%', () => {
		expect(formatFeeRate(null)).toBe('—');
		expect(formatFeeRate(undefined)).toBe('—');
		expect(formatFeeRate('   ')).toBe('—');
		expect(formatFeeRate('not-a-number')).toBe('—');
		expect(formatFeeRate('Infinity')).toBe('—');
	});
});
