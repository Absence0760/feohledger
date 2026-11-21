/**
 * Response contracts for the platform billing surface — how the AP platform
 * bills its OWN customers (the orgs/tenants): plans, subscription state, and
 * usage-to-date. This is the control-plane billing read surface, distinct from
 * the accounts-payable money path the app manages for customers.
 *
 * Mirrors `GET /api/billing/subscription` (`backend/app/api/billing.py`).
 * Money values arrive as exact decimal **strings** (this is a billing surface —
 * exactness is the point) and are rendered through `<Money>` / `formatMoney`,
 * never re-computed client-side.
 */

/** Lifecycle of an org's subscription. Mirrors the backend's four states. */
export type SubscriptionStatus = 'trialing' | 'active' | 'past_due' | 'canceled';

export interface BillingPlan {
	/** Stable machine id (e.g. `growth`). */
	code: string;
	/** Display name (e.g. `Growth`). */
	name: string;
	/** Monthly price as an exact decimal string (e.g. `"49.00"`). */
	monthly_price: string;
	/** ISO 4217 currency code. */
	currency: string;
	/** Feature flags, e.g. `{ "public_api": true, "max_seats": 25 }`. */
	entitlements: Record<string, unknown>;
	/** Free-trial length in days. */
	trial_days: number;
}

export interface BillingSubscription {
	status: SubscriptionStatus;
	/** ISO timestamps bounding the current billing period. */
	current_period_start: string | null;
	current_period_end: string | null;
	/** ISO timestamp the trial ends (null once the trial is over / never trialed). */
	trial_end: string | null;
	/** True when a live provider (Stripe) owns the subscription. */
	externally_managed: boolean;
}

/** Usage-to-date for the current period. All values are exact strings.
 *
 * The rebate meter arrives as one key PER CURRENCY
 * (`card_rebate_total.USD`), never a bare `card_rebate_total` — it used to be
 * a single cross-currency sum, which is a quantity in no currency at all, and
 * this is a meter a later billing slice prices. Read it through
 * {@link rebateMeterGroups} rather than indexing a key by hand.
 */
export interface BillingUsage {
	/** Total extraction events in the period. */
	extractions: string;
	/** The billable (platform-program) subset of extractions. */
	extractions_platform: string;
	/** Per-currency rebate meters + any meter a later slice adds. */
	[meter: string]: string;
}

/** One currency's rebate total, as it arrives on the meter map.
 *
 * `{currency, total}` deliberately matches the house per-currency shape every
 * backend `*CurrencyTotal` schema emits and `utils/currencyGroups.ts` renders,
 * so this can feed the shared display primitive rather than needing its own.
 */
export interface RebateMeterGroup {
	/** ISO 4217 code, from the meter key. */
	currency: string;
	/** Exact decimal string — never parsed to a number for display. */
	total: string;
}

/** The prefix every per-currency rebate meter carries. */
const REBATE_METER_PREFIX = 'card_rebate_total.';

/** Per-currency rebate totals from a usage meter map, sorted by currency code.
 *
 * Returns `[]` when the org accrued no rebates — the backend emits no rebate
 * key at all in that case, deliberately, because zero rebates in an unstated
 * currency is not a fact. A caller renders nothing rather than a `$0.00` whose
 * currency it invented.
 *
 * Pure, so it is unit-tested; a malformed key (no 3-letter code) is skipped
 * rather than rendered under a fabricated code.
 */
export function rebateMeterGroups(usage: BillingUsage | null | undefined): RebateMeterGroup[] {
	if (!usage) return [];
	const groups: RebateMeterGroup[] = [];
	for (const [key, amount] of Object.entries(usage)) {
		if (!key.startsWith(REBATE_METER_PREFIX)) continue;
		const currency = key.slice(REBATE_METER_PREFIX.length).trim().toUpperCase();
		if (currency.length !== 3) continue;
		groups.push({ currency, total: String(amount ?? '0') });
	}
	groups.sort((a, b) => a.currency.localeCompare(b.currency));
	return groups;
}

/** Why AI reading is paused. Mirrors `ai_invoice_meter.PAUSE_*`. */
export type AiPauseReason = 'allowance_reached' | 'spend_cap_reached';

/**
 * This month's AI-read invoices against the plan allowance (decisions §253).
 * Mirrors `AiUsageView` in `backend/app/api/billing.py`. Money arrives as exact
 * decimal STRINGS in `currency`; counts are numbers.
 */
export interface BillingAiUsage {
	/** UTC calendar month, `YYYY-MM`. */
	period: string;
	/** Distinct invoices AI-read on the platform's model key this month. */
	used: number;
	/** Included in the plan; null = the plan does not meter AI reads. */
	included: number | null;
	/** Per-invoice overage price; null = the plan pauses instead of billing. */
	overage_unit_price: string | null;
	currency: string;
	/** Overage billed so far (clamped to the cap). */
	overage_units: number;
	overage_amount: string;
	/** Overage at this month's daily pace, clamped to the cap. */
	projected_overage_amount: string;
	/** Monthly overage spending cap; null = no cap. */
	spend_cap: string | null;
	/** Whether the next new AI read would be refused. */
	paused: boolean;
	pause_reason: AiPauseReason | null;
}

export interface BillingSubscriptionResponse {
	/** Active billing adapter (e.g. `mock`, `stripe_billing`). */
	provider: string;
	/** Null when the org has no live subscription. */
	plan: BillingPlan | null;
	/** Null when the org has no live subscription. */
	subscription: BillingSubscription | null;
	/** The period the usage is rolled up for, `YYYY-MM`. */
	period: string;
	usage: BillingUsage;
	ai_usage: BillingAiUsage;
}

/** Result of `PUT /api/billing/spending-cap`. */
export interface BillingSpendCapResponse {
	monthly_spend_cap: string | null;
	ai_usage: BillingAiUsage;
}

/** How full this month's allowance is, 0–100; null when the plan is unmetered. */
export function aiUsagePercent(usage: BillingAiUsage | null | undefined): number | null {
	if (!usage || usage.included === null) return null;
	if (usage.included <= 0) return usage.used > 0 ? 100 : 0;
	return Math.min(100, Math.max(0, Math.round((usage.used / usage.included) * 100)));
}

/** The meter's colour: past 80% amber, at the limit (or paused) red. */
export function aiUsageTone(usage: BillingAiUsage | null | undefined): 'ok' | 'warning' | 'danger' {
	if (!usage) return 'ok';
	if (usage.paused) return 'danger';
	const pct = aiUsagePercent(usage);
	if (pct === null) return 'ok';
	if (pct >= 100) return 'danger';
	return pct >= 80 ? 'warning' : 'ok';
}

/** Largest cap the backend accepts (`api/billing.MAX_SPEND_CAP`). */
const MAX_SPEND_CAP_CENTS = 100_000_000n;

/**
 * Validate a typed spending cap WITHOUT turning it into a float: the value goes
 * to the server as the exact decimal string the user typed (trimmed). Blank
 * means "no cap". Whole cents only, 0.00 – 1,000,000.00 — the same rule the
 * backend enforces, checked here so the form can say so before the round trip.
 */
export function parseSpendCapInput(
	raw: string
): { ok: true; value: string | null } | { ok: false } {
	const text = raw.trim();
	if (text === '') return { ok: true, value: null };
	const match = /^(\d{1,7})(?:\.(\d{1,2}))?$/.exec(text);
	if (!match) return { ok: false };
	const cents = BigInt(match[1]) * 100n + BigInt((match[2] ?? '').padEnd(2, '0'));
	if (cents > MAX_SPEND_CAP_CENTS) return { ok: false };
	return { ok: true, value: text };
}

/** Settlement state of a past billing invoice / receipt. */
export type BillingInvoiceStatus = 'paid' | 'open' | 'void';

/** One past platform-billing invoice / receipt. Mirrors `GET /api/billing/invoices`
 *  (`backend/app/api/billing.py`). Money is an exact decimal string. */
export interface BillingInvoice {
	/** Provider-side invoice id (stable row key). */
	id: string;
	/** Human invoice number (e.g. `MOCK-2026-06`), null when the provider omits it. */
	number: string | null;
	/** Billing period the invoice covers, `YYYY-MM` (null when unknown). */
	period: string | null;
	/** Total as an exact decimal string — rendered via `<Money>`, never re-computed. */
	amount: string;
	/** ISO 4217 currency code. */
	currency: string;
	status: BillingInvoiceStatus;
	/** Hosted invoice / receipt URL (e.g. Stripe), null when the provider has none. */
	hosted_url: string | null;
	/** ISO timestamp the invoice was created (null when unknown). */
	created_at: string | null;
}

export interface BillingInvoicesResponse {
	/** Active billing adapter (e.g. `mock`, `stripe_billing`). */
	provider: string;
	/** Past invoices / receipts, newest first. Empty when the org has none. */
	invoices: BillingInvoice[];
}

/**
 * One saved payment method — PII-safe metadata ONLY (brand / last4 / expiry).
 * NEVER a full PAN. Mirrors `GET /api/billing/payment-methods`
 * (`backend/app/api/billing.py`).
 */
export interface BillingPaymentMethod {
	/** Provider-side payment-method id (stable row key). */
	id: string;
	/** Card brand (e.g. `visa`), null when the provider omits it. */
	brand: string | null;
	/** Last four digits of the card — never the full PAN. */
	last4: string | null;
	/** Expiry month (1–12), null when unknown. */
	exp_month: number | null;
	/** Expiry year (four-digit), null when unknown. */
	exp_year: number | null;
	/** Whether this is the org's default card. */
	is_default: boolean;
}

export interface BillingPaymentMethodsResponse {
	/** Active billing adapter (e.g. `mock`, `stripe_billing`). */
	provider: string;
	/** Saved cards as PII-safe metadata. Empty when the org has none on file. */
	payment_methods: BillingPaymentMethod[];
}

/**
 * Result of starting a SetupIntent to add / replace a card. Mirrors
 * `POST /api/billing/payment-method/setup-intent` (`backend/app/api/billing.py`).
 * `client_secret` is the single-use secret the provider's JS SDK (Stripe
 * Elements, deployed-only) confirms the card against — never a long-lived secret
 * or a PAN. `configured=false` (no customer / unconfigured provider) → null
 * secret + the UI shows a "billing not configured" state, not an error.
 */
export interface BillingSetupIntentResponse {
	/** Active billing adapter (e.g. `mock`, `stripe_billing`). */
	provider: string;
	/** True once a SetupIntent could be started (org provisioned + provider configured). */
	configured: boolean;
	/** Single-use secret the frontend confirms the card with. Null when not configured. */
	client_secret: string | null;
	/** Provider-side SetupIntent id. Null when not configured. */
	setup_intent_id: string | null;
}

/** The sellable plan catalog (active plans only). Mirrors `GET /api/billing/plans`
 *  (`backend/app/api/billing.py`) — the data source for the plan-change picker. */
export interface BillingPlansResponse {
	/** Active plans, cheapest first. */
	plans: BillingPlan[];
}

/**
 * Mid-period proration for a plan change — an exact decimal STRING. Positive =
 * extra charge (upgrade), negative = credit (downgrade), `"0.00"` = no change /
 * same plan. Mirrors `POST /api/billing/change-plan`'s `ProrationView`.
 */
export interface BillingPlanChangeProration {
	amount: string;
	unused_days: number;
	period_days: number;
}

/**
 * Result of `POST /api/billing/change-plan`. This call APPLIES the change —
 * there is no preview-only mode on the backend — so `changed` distinguishes a
 * real mutation from the idempotent no-op of "changing" to the plan the org is
 * already on (no mutation, zero proration).
 */
export interface BillingPlanChangeResponse {
	changed: boolean;
	old_plan_code: string;
	new_plan_code: string;
	proration: BillingPlanChangeProration;
}

/** Body of `POST /api/billing/payment-method/default`: the SetupIntent whose
 *  confirmed card becomes the one the subscription charges. The response is the
 *  refreshed {@link BillingPaymentMethodsResponse}. */
export interface BillingSetDefaultPaymentMethodRequest {
	setup_intent_id: string;
}

/** The billing adapter code that means "real Stripe" (`billing_adapters/stripe_billing.py`). */
export const STRIPE_BILLING_PROVIDER = 'stripe_billing';

/** The local-first billing adapter (`billing_adapters/mock_adapter.py`). */
export const MOCK_BILLING_PROVIDER = 'mock';

/**
 * Which card form the add-card flow shows for a started SetupIntent:
 *
 * - `not_configured` — no SetupIntent (org never provisioned, provider off), or
 *   an adapter this build has no card form for. Not an error: the page offers
 *   a contact link.
 * - `stripe` — real Stripe with a publishable key: mount the Payment Element.
 * - `stripe_key_missing` — the backend runs Stripe but this build was made
 *   without `PUBLIC_STRIPE_PUBLISHABLE_KEY`, an operator config gap. Stripe.js
 *   must not load without a key, so the form cannot render.
 * - `mock` — the local test provider: no card form, a "save test card" button.
 */
export type CardFormMode = 'not_configured' | 'stripe' | 'stripe_key_missing' | 'mock';

export function cardFormMode(
	setup: Pick<BillingSetupIntentResponse, 'provider' | 'configured' | 'client_secret' | 'setup_intent_id'>,
	publishableKey: string | null | undefined
): CardFormMode {
	if (!setup.configured || !setup.setup_intent_id) return 'not_configured';
	if (setup.provider === MOCK_BILLING_PROVIDER) return 'mock';
	if (setup.provider === STRIPE_BILLING_PROVIDER) {
		// Elements confirms against the client secret; without one there is
		// nothing to mount, whatever the key.
		if (!setup.client_secret) return 'not_configured';
		return publishableKey && publishableKey.trim() ? 'stripe' : 'stripe_key_missing';
	}
	return 'not_configured';
}

/** `POST /api/billing/change-plan` refused because the org has no saved card. */
export const PAYMENT_METHOD_REQUIRED = 'payment_method_required';
/** `POST /api/billing/change-plan` refused because the provider declined the upgrade charge. */
export const BILLING_PAYMENT_DECLINED = 'billing_payment_declined';

/**
 * What the plan-change dialog offers beside a refusal: `add_card` when the org
 * has no card yet, `update_card` when the card on file was declined, `null`
 * for every other refusal (its localized message is the whole answer). Keyed
 * on the refusal CODE, never on the (localized) message.
 */
export function planChangeCardAction(code: string | null | undefined): 'add_card' | 'update_card' | null {
	if (code === PAYMENT_METHOD_REQUIRED) return 'add_card';
	if (code === BILLING_PAYMENT_DECLINED) return 'update_card';
	return null;
}
