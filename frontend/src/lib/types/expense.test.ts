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
	reconciliationStatusLabelKey
} from './expense';
import { en } from '#lib/i18n/locales/en.ts';
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
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of values) {
			expect(en[keys[status]]).not.toBe(status);
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
			expect(Object.keys(en), `${method} → "${key}" is not in the catalogue`).toContain(key);
		}
	});
});
