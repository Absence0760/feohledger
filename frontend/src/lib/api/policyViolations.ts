/**
 * Stating an expense policy violation in the reader's language.
 *
 * The policy engine (`backend/app/services/expense_policy.py`) builds every
 * violation as `{code, message, params, …}`: a stable `code`, the `params` its
 * sentence needs (`VIOLATION_PARAMS` is the catalogue — money as exact decimal
 * strings beside their ISO code, never a float), and the server's English
 * `message`. The same shape reaches the page from two places: the submit 422's
 * `violations` list and each expense's stored `policy_violations`
 * (`docs/decisions.md` §378).
 *
 * {@link policyViolationText} always returns a sentence: the localized one when
 * this build knows the code and every param it needs is well-formed, otherwise
 * the server's `message` — never a sentence with a blank where a figure
 * belongs. A row stored before the params existed carries none, so it states
 * its English until its next write refreshes it.
 *
 * Imports nothing from the Svelte runtime (the caller passes `m`), so it
 * unit-tests under vitest's node environment.
 */
import type { MainMessageKey, MessageKey } from '#lib/i18n/messages.ts';
import type { PolicyViolation } from '#lib/types/expense.ts';
import { formatMoney } from '#lib/utils/money.ts';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;
type Params = Record<string, unknown>;

/** The violation codes this build states (`expense_policy.VIOLATION_*`). */
export const POLICY_VIOLATION_CODES = [
	'category_limit',
	'receipt_required',
	'preapproval_required',
	'per_diem_exceeded',
	'mileage_amount_mismatch'
] as const;

const DECIMAL = /^-?\d+(\.\d+)?$/;

function decimal(params: Params, key: string): string | null {
	const v = params[key];
	return typeof v === 'string' && DECIMAL.test(v) ? v : null;
}

function currency(params: Params, key: string): string | null {
	const v = params[key];
	return typeof v === 'string' && /^[A-Za-z]{3}$/.test(v) ? v.toUpperCase() : null;
}

function money(params: Params, amountKey: string, currencyKey: string): string | null {
	const amount = decimal(params, amountKey);
	const code = currency(params, currencyKey);
	return amount === null || code === null ? null : formatMoney(amount, { currency: code });
}

/** The figures every violation carries, or `null` if any is malformed. */
function common(params: Params) {
	const amount = money(params, 'amount', 'amount_currency');
	const limit = money(params, 'limit', 'currency');
	const unit = currency(params, 'currency');
	const comparison = params.comparison;
	if (amount === null || limit === null || unit === null) return null;
	if (comparison !== 'resolved' && comparison !== 'unresolved') return null;
	return { amount, limit, currency: unit, unresolved: comparison === 'unresolved' };
}

function pick(unresolved: boolean, resolved: MainMessageKey, fellClosed: MainMessageKey) {
	return unresolved ? fellClosed : resolved;
}

type Builder = (params: Params, t: Translate) => string | null;

const BUILDERS: Record<(typeof POLICY_VIOLATION_CODES)[number], Builder> = {
	category_limit: (p, t) => {
		const c = common(p);
		if (c === null) return null;
		const { unresolved, ...figures } = c;
		const category = p.category;
		if (category === null || category === undefined) {
			return t(
				pick(
					unresolved,
					'expenses.violation.categoryLimitUncategorised',
					'expenses.violation.categoryLimitUncategorisedUnresolved'
				),
				figures
			);
		}
		// The expense's own category, as the user typed it — free text, so it is
		// named verbatim rather than looked up.
		if (typeof category !== 'string' || !category.trim()) return null;
		return t(
			pick(unresolved, 'expenses.violation.categoryLimit', 'expenses.violation.categoryLimitUnresolved'),
			{ ...figures, category }
		);
	},
	receipt_required: (p, t) => {
		const c = common(p);
		if (c === null) return null;
		const { unresolved, ...figures } = c;
		return t(
			pick(unresolved, 'expenses.violation.receiptRequired', 'expenses.violation.receiptRequiredUnresolved'),
			figures
		);
	},
	preapproval_required: (p, t) => {
		const c = common(p);
		if (c === null) return null;
		const { unresolved, ...figures } = c;
		return t(
			pick(
				unresolved,
				'expenses.violation.preapprovalRequired',
				'expenses.violation.preapprovalRequiredUnresolved'
			),
			figures
		);
	},
	per_diem_exceeded: (p, t) => {
		const c = common(p);
		if (c === null) return null;
		const { unresolved, ...figures } = c;
		return t(
			pick(unresolved, 'expenses.violation.perDiemExceeded', 'expenses.violation.perDiemExceededUnresolved'),
			figures
		);
	},
	mileage_amount_mismatch: (p, t) => {
		const c = common(p);
		// `miles` is a distance and `rate` a per-mile figure at four places:
		// both are named exactly as sent, never rounded to a currency's places.
		const miles = decimal(p, 'miles');
		const rate = decimal(p, 'rate');
		if (c === null || miles === null || rate === null) return null;
		const { unresolved, ...figures } = c;
		const params = { ...figures, miles, rate };
		if (unresolved) return t('expenses.violation.mileageUnresolved', params);
		if (p.direction === 'over') return t('expenses.violation.mileageOver', params);
		if (p.direction === 'under') return t('expenses.violation.mileageUnder', params);
		return null;
	}
};

/**
 * One violation's sentence: localized when this build can state it, the
 * server's English `message` otherwise. Never throws.
 */
export function policyViolationText(violation: PolicyViolation, t: Translate): string {
	const build = (BUILDERS as Record<string, Builder | undefined>)[violation.code];
	const params = violation.params;
	if (build && params && typeof params === 'object') {
		try {
			const text = build(params as Params, t);
			if (text) return text;
		} catch {
			// A formatter that throws on a value this build did not expect
			// degrades to the server's sentence, never a blank.
		}
	}
	return violation.message;
}
