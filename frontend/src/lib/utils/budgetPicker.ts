/**
 * The budget half of the requisition form: what the picker shows for an
 * option, and what the overspend pre-check under it says.
 *
 * Everything generic about the picker — paging, the count line, the keyboard
 * contract — is `./searchPicker.ts` under `ui/SearchPicker`. This module owns
 * only the budget rules, kept DOM-free so vitest can pin them.
 */
import type { BudgetCheck, BudgetSpend } from '#lib/types/budget.ts';
import { isNegativeAmount, type MoneyString } from '#lib/utils/money.ts';

/** A picker option — the fields of `Budget` the picker reads. */
export interface BudgetPickerOption {
	id: string;
	name: string;
	dimension_value: string;
	period: string | null;
	currency: string;
}

/** The input's text once a budget is chosen. The name alone is ambiguous
 *  across periods ("Marketing" in 2025 and 2026), so the period rides along. */
export function budgetOptionLabel(option: BudgetPickerOption): string {
	const period = option.period?.trim();
	return period ? `${option.name} (${period})` : option.name;
}

/** Codes compare case-insensitively — the backend upper-cases both sides. */
export function sameCurrency(a: string | null | undefined, b: string | null | undefined): boolean {
	return !!a && !!b && a.trim().toUpperCase() === b.trim().toUpperCase();
}

/** What one `GET /budgets/check` answered, stamped with the question it
 *  answers so a stale answer (an earlier total, a different budget) is never
 *  read as the current one. */
export interface BudgetCheckAnswer {
	budgetId: string;
	amount: MoneyString;
	status: 'loading' | 'failed' | 'ready';
	result: BudgetCheck | null;
}

/**
 * What the form says under the picker. Exactly one of:
 *
 * - `none` — no budget chosen; nothing to say.
 * - `mismatch` — the budget is in another currency than the requisition.
 *   Its headroom is in a different unit, so it is never compared at face
 *   value, and the backend refuses the link (422) — the form blocks save.
 * - `checking` / `failed` — the pre-check is in flight, or did not answer. A
 *   failure is advisory: the link itself is still valid, only the headroom is
 *   unknown, so it never blocks save and never reads as "within budget".
 * - `overspend` / `within` — the backend's `would_overspend` verdict, read as
 *   given (never re-derived from the figures beside it).
 */
export type BudgetCheckView =
	| { kind: 'none' }
	| { kind: 'mismatch'; budgetCurrency: string }
	| { kind: 'checking' }
	| { kind: 'failed' }
	| { kind: 'overspend'; check: BudgetCheck }
	| { kind: 'within'; check: BudgetCheck };

export function budgetCheckView(input: {
	budgetId: string;
	/** The chosen budget's currency when the form knows it, else `null`. */
	budgetCurrency: string | null;
	/** The requisition's currency. */
	currency: string;
	/** The exact total the requisition would commit. */
	amount: MoneyString;
	answer: BudgetCheckAnswer | null;
}): BudgetCheckView {
	const { budgetId, budgetCurrency, currency, amount, answer } = input;
	if (!budgetId) return { kind: 'none' };
	if (budgetCurrency && !sameCurrency(budgetCurrency, currency)) {
		return { kind: 'mismatch', budgetCurrency: budgetCurrency.trim().toUpperCase() };
	}
	if (!answer || answer.budgetId !== budgetId || answer.amount !== amount) {
		return { kind: 'checking' };
	}
	if (answer.status === 'loading') return { kind: 'checking' };
	if (answer.status === 'failed' || !answer.result) return { kind: 'failed' };
	// The check reports its own currency. A budget whose currency the form did
	// not know yet (an existing link still resolving) is held to the same rule
	// here: another currency's headroom is no answer for this requisition.
	if (!sameCurrency(answer.result.currency, currency)) {
		return { kind: 'mismatch', budgetCurrency: answer.result.currency.trim().toUpperCase() };
	}
	return answer.result.would_overspend
		? { kind: 'overspend', check: answer.result }
		: { kind: 'within', check: answer.result };
}

/**
 * What the live regions under the picker show, and whether they are busy.
 *
 * Each typing pause starts a fresh check. Flipping the `role="status"` region
 * to "Checking the budget…" and back made a screen reader announce twice per
 * pause; instead the last settled verdict FOR THIS BUDGET stays in place,
 * marked `busy` (`aria-busy="true"`), until the new one lands — so only a
 * settled result is ever announced. With nothing settled to hold (a budget
 * just chosen) the view is `checking` and the form shows that text outside
 * any live region.
 *
 * Holding re-reads the stale answer against the current budget and currency,
 * so a held verdict can never be another budget's, or another currency's.
 */
export function budgetCheckDisplay(
	input: Parameters<typeof budgetCheckView>[0]
): { view: BudgetCheckView; busy: boolean } {
	const view = budgetCheckView(input);
	if (view.kind !== 'checking') return { view, busy: false };
	const a = input.answer;
	if (a && a.budgetId === input.budgetId && a.status !== 'loading') {
		const held = budgetCheckView({ ...input, amount: a.amount });
		if (held.kind === 'overspend' || held.kind === 'within' || held.kind === 'failed') {
			return { view: held, busy: true };
		}
	}
	return { view, busy: true };
}

/**
 * The read-only headroom line an approver sees on a submitted requisition.
 *
 * Read from `GET /budgets/{id}/spend`, whose `committed` already includes this
 * requisition (it is open: submitted / pending approval / approved). Never
 * from `/budgets/check` with the requisition's total — that would count it a
 * second time. `remaining` is the backend's Decimal subtraction, read as
 * given; only its sign is inspected.
 *
 * - `none` — nothing loaded, or the budget is in another currency (the spend
 *   legs then refuse this requisition, so the figure would not include it).
 * - `over` / `within` — the sign of `remaining`.
 */
export type BudgetHeadroomView =
	| { kind: 'none' }
	| { kind: 'over' | 'within'; spend: BudgetSpend };

export function budgetHeadroomView(spend: BudgetSpend | null, currency: string): BudgetHeadroomView {
	if (!spend || !sameCurrency(spend.currency, currency)) return { kind: 'none' };
	return { kind: isNegativeAmount(spend.remaining) ? 'over' : 'within', spend };
}
