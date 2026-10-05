// Typed helpers for the platform billing surface. Routes through the shared
// `api` client (Bearer + X-Tenant-Slug + 401-bounce). See
// `#lib/types/billing` for the response contracts and `backend/docs/billing.md`
// for the backend slice this consumes.
import { api } from '#lib/api.ts';
import type {
	BillingInvoicesResponse,
	BillingPaymentMethodsResponse,
	BillingPlanChangeResponse,
	BillingPlansResponse,
	BillingSetupIntentResponse,
	BillingSubscriptionResponse
} from '#lib/types/billing.ts';

/** Current plan + subscription status + usage-to-date for the active period.
 *  `plan`/`subscription` are null when the org has no live subscription. */
export function getBillingSubscription(): Promise<BillingSubscriptionResponse> {
	return api.get<BillingSubscriptionResponse>('/api/billing/subscription');
}

/** The org's past platform-billing invoices / receipts (newest first). admin/cfo
 *  only (the backend 403s everyone else). An org never provisioned with the
 *  provider — or an unconfigured / unavailable one — yields an empty list. */
export function getBillingInvoices(): Promise<BillingInvoicesResponse> {
	return api.get<BillingInvoicesResponse>('/api/billing/invoices');
}

/** The org's saved cards as PII-safe metadata only (brand / last4 / expiry —
 *  never a PAN). admin/cfo only. An org never provisioned with the provider — or
 *  an unconfigured / unavailable one — yields an empty list. */
export function getBillingPaymentMethods(): Promise<BillingPaymentMethodsResponse> {
	return api.get<BillingPaymentMethodsResponse>('/api/billing/payment-methods');
}

/** Start a SetupIntent so the org can add or replace a saved card. admin/cfo
 *  only. Returns the provider's single-use `client_secret` (the frontend confirms
 *  the card against it with the provider's JS SDK — no charge, no PAN). An org
 *  never provisioned — or an unconfigured / unavailable provider — yields
 *  `configured=false` + a null secret, never an error. */
export function startBillingSetupIntent(): Promise<BillingSetupIntentResponse> {
	return api.post<BillingSetupIntentResponse>('/api/billing/payment-method/setup-intent', {});
}

/** The sellable plan catalog (active plans, cheapest first) — the plan-change
 *  picker's data source. admin/cfo only. */
export function getBillingPlans(): Promise<BillingPlansResponse> {
	return api.get<BillingPlansResponse>('/api/billing/plans');
}

/** Move the org's live subscription to `planCode`, prorated mid-period.
 *  admin/cfo only. This APPLIES the change immediately — there is no preview —
 *  and is idempotent: changing to the plan the org is already on succeeds with
 *  `changed: false` and a zero proration rather than an error. */
export function changeBillingPlan(planCode: string): Promise<BillingPlanChangeResponse> {
	return api.post<BillingPlanChangeResponse>('/api/billing/change-plan', { plan_code: planCode });
}
