import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import {
	RETRY_SKIP_REASON_KEYS,
	RUN_NOT_RETRYABLE,
	isRunRetryOffered,
	retrySkipReasonKey
} from './runRetry';

/**
 * The run dialog's Retry failed payments control reads the server's verdict
 * (`actions.retry_failed`) and states the endpoint's skip reasons in the
 * reader's language.
 */

const PAYMENTS_PY = readFileSync(
	join(__dirname, '../../../../backend/app/api/payments.py'),
	'utf8'
);
const CONTROLS_PY = readFileSync(
	join(__dirname, '../../../../backend/app/services/payment_controls.py'),
	'utf8'
);

describe('isRunRetryOffered', () => {
	it('fails closed until the verdict arrives', () => {
		expect(isRunRetryOffered(undefined)).toBe(false);
	});

	it('is offered (and pressable) when the endpoint would accept', () => {
		expect(isRunRetryOffered(null)).toBe(true);
	});

	it('is hidden for a refusal about the reader’s role', () => {
		expect(isRunRetryOffered({ code: 'role_not_permitted', message: 'x' })).toBe(false);
	});

	it('is hidden on a run that did not finish with failures', () => {
		expect(isRunRetryOffered({ code: RUN_NOT_RETRYABLE, message: 'x' })).toBe(false);
	});

	it.each(['payment_run_segregation', 'payment_run_awaiting_cfo', 'payment_run_nothing_to_retry'])(
		'is shown (disabled, with the reason) for %s — the reader can act on it',
		(code) => {
			expect(isRunRetryOffered({ code, message: 'x' })).toBe(true);
		}
	);

	it('names a code the backend gate actually emits', () => {
		expect(CONTROLS_PY).toContain(`"${RUN_NOT_RETRYABLE}"`);
	});
});

describe('retry skip reasons', () => {
	const emitted = [...PAYMENTS_PY.matchAll(/skipped\.append\("([a-z_]+)"\)/g)].map((m) => m[1]);

	it('the scan finds the endpoint’s reasons at all', () => {
		expect(emitted.length).toBeGreaterThan(5);
	});

	it('every reason the endpoint emits has a message key, and no key is stale', () => {
		expect(new Set(Object.keys(RETRY_SKIP_REASON_KEYS))).toEqual(new Set(emitted));
	});

	it('every key exists in the English catalogue', () => {
		for (const key of Object.values(RETRY_SKIP_REASON_KEYS)) {
			expect(EN_ALL[key], key).toBeTruthy();
		}
	});

	it('an unknown reason has no key, so the caller renders the raw code', () => {
		expect(retrySkipReasonKey('a_reason_from_the_future')).toBeNull();
		expect(retrySkipReasonKey('needs_reconciliation')).toBe(
			'paymentRuns.runDetail.retrySkip.needsReconciliation'
		);
	});
});
