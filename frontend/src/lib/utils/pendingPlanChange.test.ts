import { describe, expect, it } from 'vitest';
import {
	PENDING_PLAN_KEY,
	rememberPendingPlan,
	takePendingPlan,
	type PendingPlanStorage
} from './pendingPlanChange.ts';

function memoryStorage(seed: Record<string, string> = {}): PendingPlanStorage & {
	data: Map<string, string>;
} {
	const data = new Map(Object.entries(seed));
	return {
		data,
		getItem: (k) => data.get(k) ?? null,
		setItem: (k, v) => void data.set(k, v),
		removeItem: (k) => void data.delete(k)
	};
}

describe('pending plan change across a Stripe redirect', () => {
	it('round-trips for the SetupIntent it was stashed for, then is gone', () => {
		const storage = memoryStorage();
		rememberPendingPlan('growth', 'seti_1', storage);
		expect(takePendingPlan('seti_1', storage)).toBe('growth');
		expect(storage.data.has(PENDING_PLAN_KEY)).toBe(false);
		expect(takePendingPlan('seti_1', storage)).toBeNull();
	});

	it('is not honoured for a different SetupIntent, and is cleared anyway', () => {
		const storage = memoryStorage();
		rememberPendingPlan('growth', 'seti_old', storage);
		expect(takePendingPlan('seti_new', storage)).toBeNull();
		expect(storage.data.has(PENDING_PLAN_KEY)).toBe(false);
	});

	it('clears an earlier stash when this confirmation has no pending plan', () => {
		const storage = memoryStorage();
		rememberPendingPlan('growth', 'seti_1', storage);
		rememberPendingPlan(null, 'seti_2', storage);
		expect(storage.data.has(PENDING_PLAN_KEY)).toBe(false);
	});

	it('reads a malformed or tampered stash as nothing', () => {
		for (const raw of ['not json', '"growth"', '{"planCode":7,"setupIntentId":"seti_1"}', 'null']) {
			expect(takePendingPlan('seti_1', memoryStorage({ [PENDING_PLAN_KEY]: raw }))).toBeNull();
		}
	});

	it('degrades to no retry when storage is missing or throws', () => {
		const throwing: PendingPlanStorage = {
			getItem: () => {
				throw new Error('blocked');
			},
			setItem: () => {
				throw new Error('blocked');
			},
			removeItem: () => {
				throw new Error('blocked');
			}
		};
		expect(() => rememberPendingPlan('growth', 'seti_1', throwing)).not.toThrow();
		expect(takePendingPlan('seti_1', throwing)).toBeNull();
		expect(takePendingPlan('seti_1', null)).toBeNull();
	});
});
