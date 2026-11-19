import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import { QUEUE_BLOCKED_REASON_KEYS, queueBlockedReasonKey } from './paymentQueue';

/**
 * The payment queue states every refusal code the backend can put in a row's
 * `blocked_reason`, read off the backend's own lists so a new refusal cannot
 * reach the page as the generic sentence.
 */

const BACKEND = join(__dirname, '../../../../backend/app');
const PAYMENT_RUNS_PY = readFileSync(join(BACKEND, 'services/payment_runs.py'), 'utf8');
const PAYMENTS_PY = readFileSync(join(BACKEND, 'api/payments.py'), 'utf8');

/** `REFUSAL_X = "code"` constants in `services/payment_runs.py`. */
const refusalCodes = [...PAYMENT_RUNS_PY.matchAll(/^REFUSAL_[A-Z_]+ = "([a-z_]+)"$/gm)].map(
	(m) => m[1]
);

/** The members of `PAYMENT_BLOCKING_EXCEPTION_TYPES` in `api/payments.py`. */
const blockingTypes = (() => {
	const tuple = PAYMENTS_PY.match(/^PAYMENT_BLOCKING_EXCEPTION_TYPES = \(([^)]*)\)/m);
	return tuple ? [...tuple[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]) : [];
})();

/** Never a queue row: an invoice a live payment holds is filtered out of the
 *  queue before any verdict is given (`_queue_base_where`). */
const NOT_A_QUEUE_ROW = new Set(['live_payment']);

describe('queue blocked reasons', () => {
	it('the scans find the backend vocabulary at all', () => {
		expect(refusalCodes).toContain('currency_unknown');
		expect(blockingTypes).toContain('fraud_flag');
	});

	it('every code the queue can send has a key, and no key is stale', () => {
		const sendable = [...blockingTypes, ...refusalCodes].filter((c) => !NOT_A_QUEUE_ROW.has(c));
		expect(new Set(Object.keys(QUEUE_BLOCKED_REASON_KEYS))).toEqual(new Set(sendable));
	});

	it('every key exists in the English catalogue', () => {
		for (const key of Object.values(QUEUE_BLOCKED_REASON_KEYS)) {
			expect(EN_ALL[key], key).toBeTruthy();
		}
	});

	it('states a missing currency as its own reason, not the generic one', () => {
		expect(queueBlockedReasonKey('currency_unknown')).toBe(
			'payments.queue.blocked.currencyUnknown'
		);
	});

	it('an unknown or absent code falls back to the generic reason', () => {
		expect(queueBlockedReasonKey('a_reason_from_the_future')).toBe(
			'payments.queue.blocked.generic'
		);
		expect(queueBlockedReasonKey(null)).toBe('payments.queue.blocked.generic');
	});
});
