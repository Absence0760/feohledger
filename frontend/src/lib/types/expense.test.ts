import { describe, it, expect } from 'vitest';
import {
	EXPENSE_STATUSES,
	EXPENSE_STATUS_LABEL_KEYS,
	EXPENSE_PAYMENT_METHODS,
	EXPENSE_PAYMENT_METHOD_LABEL_KEYS,
	EXPENSE_REPORT_STATUSES,
	EXPENSE_REPORT_STATUS_LABEL_KEYS,
	EXPENSE_PREAPPROVAL_STATUSES,
	EXPENSE_PREAPPROVAL_STATUS_LABEL_KEYS,
	RECONCILIATION_STATUSES,
	RECONCILIATION_STATUS_LABEL_KEYS,
	expenseStatusLabelKey,
	expenseReportStatusLabelKey,
	expensePreapprovalStatusLabelKey,
	reconciliationStatusLabelKey,
	isLineCreator,
	canAuthorLooseLine
} from './expense';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

/**
 * Map-completeness guard for the four expense label maps.
 *
 * `Record<Status, MessageKey>` already fails `pnpm check` when a status joins
 * a union with no key — but a key naming nothing in the catalogue typechecks
 * fine and renders the raw key string in the badge (`m()` falls back key →
 * raw). So the compile-time half is the union and this is the runtime half.
 *
 * All four were hardcoded English until this test existed, which is how the
 * `/expenses` tabs rendered translated filter chips beside untranslated
 * `Reimbursed` / `Unmatched` badges.
 */
const CASES: {
	name: string;
	values: readonly string[];
	keys: Record<string, MessageKey>;
	accessor: (s: string) => MessageKey | null;
}[] = [
	{
		name: 'EXPENSE_STATUS_LABEL_KEYS',
		values: EXPENSE_STATUSES,
		keys: EXPENSE_STATUS_LABEL_KEYS,
		accessor: expenseStatusLabelKey
	},
	{
		name: 'EXPENSE_REPORT_STATUS_LABEL_KEYS',
		values: EXPENSE_REPORT_STATUSES,
		keys: EXPENSE_REPORT_STATUS_LABEL_KEYS,
		accessor: expenseReportStatusLabelKey
	},
	{
		name: 'EXPENSE_PREAPPROVAL_STATUS_LABEL_KEYS',
		values: EXPENSE_PREAPPROVAL_STATUSES,
		keys: EXPENSE_PREAPPROVAL_STATUS_LABEL_KEYS,
		accessor: expensePreapprovalStatusLabelKey
	},
	{
		name: 'RECONCILIATION_STATUS_LABEL_KEYS',
		values: RECONCILIATION_STATUSES,
		keys: RECONCILIATION_STATUS_LABEL_KEYS,
		accessor: reconciliationStatusLabelKey
	}
];

describe.each(CASES)('$name', ({ values, keys, accessor }) => {
	it('names a real catalogue key for every status', () => {
		for (const status of values) {
			const key = keys[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(EN_ALL), `${status} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of values) {
			expect(EN_ALL[keys[status]]).not.toBe(status);
		}
	});

	it('resolves a known status and returns null otherwise', () => {
		for (const status of values) {
			expect(accessor(status)).toBe(keys[status]);
		}
		// The caller renders the raw value — visible and searchable — rather
		// than a blank badge, so a status the backend adds first degrades
		// gracefully until this map catches up.
		expect(accessor('some_future_status')).toBeNull();
	});
});

describe('EXPENSE_PAYMENT_METHOD_LABEL_KEYS', () => {
	it('names a real catalogue key for every payment method', () => {
		for (const method of EXPENSE_PAYMENT_METHODS) {
			const key = EXPENSE_PAYMENT_METHOD_LABEL_KEYS[method];
			expect(key, `${method} has no label key`).toBeTruthy();
			expect(Object.keys(EN_ALL), `${method} → "${key}" is not in the catalogue`).toContain(key);
		}
	});
});

// The §434 mirror (`services/expense_controls.expense_creator_refusal` +
// `.report_line_creator_refusal`): a NULL creator passes, the creator passes,
// anyone else is refused — and a line on a report is its owner's, not judged.
describe('isLineCreator / canAuthorLooseLine', () => {
	const me = 'user-me';
	const other = 'user-other';

	it('passes the creator and a NULL (pre-0134) creator, refuses anyone else', () => {
		expect(isLineCreator({ created_by_user_id: me }, me)).toBe(true);
		expect(isLineCreator({ created_by_user_id: null }, me)).toBe(true);
		expect(isLineCreator({ created_by_user_id: other }, me)).toBe(false);
	});

	it('refuses a recorded creator when the caller is unknown', () => {
		expect(isLineCreator({ created_by_user_id: other }, undefined)).toBe(false);
		expect(isLineCreator({ created_by_user_id: other }, null)).toBe(false);
		expect(isLineCreator({ created_by_user_id: null }, undefined)).toBe(true);
	});

	it('judges only a loose line: one on a report is left to its report gate', () => {
		expect(canAuthorLooseLine({ report_id: null, created_by_user_id: other }, me)).toBe(false);
		expect(canAuthorLooseLine({ report_id: null, created_by_user_id: me }, me)).toBe(true);
		expect(canAuthorLooseLine({ report_id: 'r1', created_by_user_id: other }, me)).toBe(true);
	});
});
