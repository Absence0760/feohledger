/**
 * The plan change a card-refused upgrade is waiting on, kept across a Stripe
 * REDIRECT confirmation of the card.
 *
 * The billing page remembers the refused plan in component state and retries
 * it once the card is saved. A card confirms in place, so that is the whole
 * story — except for a redirect-based method, where Stripe navigates away and
 * back to `/billing?setup_intent=…&redirect_status=…` and every piece of state
 * is gone. The card panel promised "once your card is saved, we will move you
 * to the X plan", so the plan code is stashed in `sessionStorage` right before
 * `confirmSetup` and taken back on the return.
 *
 * The stash names the SetupIntent it belongs to and is honoured only for that
 * one: a stale entry (an abandoned attempt in this tab) can never retry a plan
 * change on some later, unrelated return. It holds a plan code and an intent
 * id — neither is a secret. Pure apart from the injected storage, so vitest's
 * node environment reaches all of it; blocked storage only costs the retry.
 */

export const PENDING_PLAN_KEY = 'billing_pending_plan';

export type PendingPlanStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function defaultStorage(): PendingPlanStorage | null {
	try {
		return typeof sessionStorage === 'undefined' ? null : sessionStorage;
	} catch {
		return null;
	}
}

/**
 * Stash `planCode` for the SetupIntent about to be confirmed, or clear any
 * stash when there is no pending plan — always one or the other, so an
 * earlier attempt can never steer this one.
 */
export function rememberPendingPlan(
	planCode: string | null | undefined,
	setupIntentId: string | null | undefined,
	storage: PendingPlanStorage | null = defaultStorage()
): void {
	if (!storage) return;
	try {
		if (planCode && setupIntentId) {
			storage.setItem(PENDING_PLAN_KEY, JSON.stringify({ planCode, setupIntentId }));
		} else {
			storage.removeItem(PENDING_PLAN_KEY);
		}
	} catch {
		// Quota / blocked storage: the card still saves, the retry is lost.
	}
}

/** Read and clear the stash; the plan code only when it was stashed for
 *  `setupIntentId`, else `null`. Always clears. */
export function takePendingPlan(
	setupIntentId: string | null | undefined,
	storage: PendingPlanStorage | null = defaultStorage()
): string | null {
	if (!storage) return null;
	try {
		const raw = storage.getItem(PENDING_PLAN_KEY);
		storage.removeItem(PENDING_PLAN_KEY);
		if (!raw || !setupIntentId) return null;
		const parsed: unknown = JSON.parse(raw);
		if (!parsed || typeof parsed !== 'object') return null;
		const { planCode, setupIntentId: stashedFor } = parsed as Record<string, unknown>;
		if (typeof planCode !== 'string' || !planCode || stashedFor !== setupIntentId) return null;
		return planCode;
	} catch {
		return null;
	}
}
