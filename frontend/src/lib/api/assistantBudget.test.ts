import { describe, expect, it } from 'vitest';
import { AssistantBudgetError, assistantBudgetError } from './assistantBudget.ts';

describe('assistantBudgetError — the AI budget 429 vs every other 429', () => {
	it('reads the budget refusal the backend actually sends (nested under `detail`)', () => {
		// backend/app/api/assistant.py::_budget_exceeded_http — HTTPException(detail={...})
		const err = assistantBudgetError({
			detail: 'Monthly AI assistant token budget exceeded.',
			code: 'assistant_budget_exceeded',
			used: 1234,
			budget: 1000,
			period: '2026-10'
		});
		expect(err).toBeInstanceOf(AssistantBudgetError);
		expect(err?.used).toBe(1234);
		expect(err?.budget).toBe(1000);
		expect(err?.period).toBe('2026-10');
		expect(err?.message).toBe('Monthly AI assistant token budget exceeded.');
	});

	it('is NOT a budget error for the shared rate limiter’s coded refusal', () => {
		expect(
			assistantBudgetError({
				code: 'rate_limited',
				message: 'Too many requests. Please wait 90 seconds before trying again.',
				params: { retry_after_seconds: 90 }
			})
		).toBeNull();
	});

	it('is NOT a budget error for a bare string, a list, or no body', () => {
		for (const detail of [undefined, null, '', 'Too Many Requests', [], [{ budget: 1 }], 42]) {
			expect(assistantBudgetError(detail), JSON.stringify(detail)).toBeNull();
		}
	});

	it('requires a numeric budget — a stringly or missing one is some other refusal', () => {
		expect(assistantBudgetError({ code: 'assistant_budget_exceeded' })).toBeNull();
		expect(assistantBudgetError({ budget: '1000', used: 5 })).toBeNull();
		expect(assistantBudgetError({ budget: Number.NaN })).toBeNull();
	});

	it('a zero budget still counts, and a missing `used` reads as 0', () => {
		const err = assistantBudgetError({ budget: 0 });
		expect(err?.budget).toBe(0);
		expect(err?.used).toBe(0);
		expect(err?.period).toBe('');
	});
});
