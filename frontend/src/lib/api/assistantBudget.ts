/**
 * The AI token-budget refusal, told apart from every other HTTP 429.
 *
 * The assistant and the cash-flow copilot answer an over-budget org with a
 * 429 whose `detail` is `{detail, code: "assistant_budget_exceeded", used,
 * budget, period}` (`backend/app/api/assistant.py::_budget_exceeded_http`,
 * mirrored in `cash_flow.py`). The shared rate limiter answers a 429 too —
 * the coded `rate_limited` refusal, which carries no budget at all. Reading
 * every 429 as "AI budget reached" told a throttled user their monthly
 * allowance was spent (with a `0 / 0` figure, since the limiter sends none).
 *
 * Pure — no `$app`, no `fetch` — so vitest reaches it directly.
 */

/** The budget refusal, with the figures the page's notice renders. */
export class AssistantBudgetError extends Error {
	used: number;
	budget: number;
	period: string;
	constructor(detail: string, used: number, budget: number, period: string) {
		super(detail);
		this.name = 'AssistantBudgetError';
		this.used = used;
		this.budget = budget;
		this.period = period;
	}
}

/**
 * The `AssistantBudgetError` a refused response's `detail` describes, or
 * `null` when it is any other refusal. It is a budget refusal only when the
 * detail is an object carrying a numeric `budget` — the one field no other 429
 * sends — so a limiter refusal, a bare string or a missing body all return
 * `null` and are handled as the ordinary coded error they are.
 */
export function assistantBudgetError(detail: unknown): AssistantBudgetError | null {
	if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null;
	const d = detail as Record<string, unknown>;
	if (typeof d.budget !== 'number' || !Number.isFinite(d.budget)) return null;
	const used = typeof d.used === 'number' && Number.isFinite(d.used) ? d.used : 0;
	const message = typeof d.detail === 'string' && d.detail ? d.detail : 'Monthly AI budget reached';
	return new AssistantBudgetError(
		message,
		used,
		d.budget,
		typeof d.period === 'string' ? d.period : ''
	);
}
