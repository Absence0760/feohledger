// TYPE-ONLY on purpose. `$lib` is a SvelteKit alias resolved from
// `.svelte-kit/tsconfig.json`'s `paths`; `tsc` honours it, but Playwright's
// own esbuild transform does not read that file. A `import type` is erased
// before the runtime ever sees it, so this costs nothing at test time — a
// VALUE import from `$lib` here would fail to resolve when Playwright loads
// the spec. Keep app *values* out of the e2e tree; app *types* are what make
// these fixtures honest.
import type { DashboardData, DashboardDiscountCapture } from '$lib/types/analytics';

/**
 * The one `GET /api/dashboard` stub the dashboard specs build on.
 *
 * **Why it is shared, and why it is typed.** Both dashboard specs used to
 * carry their own hand-written copy of this payload, and nothing compared
 * either against the shape the page reads — `pnpm check` extends
 * `.svelte-kit/tsconfig.json`, whose `include` covers `../src/**` and not
 * `tests-e2e`. The drift that cost: the stub omitted `unconverted_count` on
 * `aging_reporting`, `undefined > 0` is false, and the partial-conversion
 * disclosure the spec existed to exercise could only ever render its
 * no-notice branch. Two files, one omission, no signal.
 *
 * `satisfies DashboardData` is what makes that a compile error now — but ONLY
 * because `tsconfig.e2e.json` puts this tree in a real TypeScript program and
 * `pnpm check:e2e` runs it in CI. A `satisfies` in an unchecked tree is
 * decoration; the two land together on purpose.
 */

/**
 * Response fields no frontend surface reads, so `DashboardData` does not
 * declare them.
 *
 * They are still sent, and a stub that omits them is a payload the backend
 * never produces — which is exactly how a page comes to depend on a field the
 * fixture happens to leave out. Kept OUTSIDE the `satisfies` block rather than
 * added to `DashboardData`: `satisfies` excess-property-checks an object
 * literal, and widening the app type to silence that would type fields the app
 * has no consumer for, inviting the next reader to render one.
 */
const NON_RENDERED_FIELDS = {
	upcoming_total_amount: 0,
	upcoming_total_amount_reporting: 0,
	upcoming_unconverted_count: 0,
	processing_time: {},
	approval_bottleneck: []
} as const;

/** The reporting currency every fixture below is denominated in. */
export const REPORTING_CURRENCY = 'USD';

/** One `vendor_spend` row. */
export function vendorSpendRow(
	vendor: string,
	amount: number,
	unconverted_count: number
): DashboardData['vendor_spend'][number] {
	return { vendor, amount, unconverted_count };
}

/** One `upcoming_payments` row — a FACE amount in the invoice's own currency. */
export function upcomingPaymentRow(
	invoiceNumber: string,
	amount: number,
	currency: string | null
): DashboardData['upcoming_payments'][number] {
	return {
		id: `00000000-0000-4000-8000-${invoiceNumber.replace(/\D/g, '').padStart(12, '0')}`,
		invoice_number: invoiceNumber,
		vendor_name: `Vendor ${invoiceNumber}`,
		amount,
		currency,
		due_date: '2026-09-30',
		is_overdue: false
	};
}

/** One `monthly_trend` row. */
export function monthlyTrendRow(
	month: string,
	amount: number,
	unconverted_count: number
): DashboardData['monthly_trend'][number] {
	return { month, count: 2, amount, reporting_amount: amount, unconverted_count };
}

/**
 * Everything a spec is allowed to vary.
 *
 * Deliberately narrow: every KPI-row count defaults to zero, so the KPI row's
 * two partial-conversion lines (`unconverted-rollup-face-value`,
 * `unconverted-rollup-excluded`) never fire unless a spec asks for them — and a
 * chart notice can never be one of them misread. A spec that needs a line says
 * so through the three named counts below, not by spreading a raw object over
 * the payload.
 */
export interface DashboardPatch {
	/** `vendor_spend`, in rank order. Pass `[]` to render no vendor bars. */
	vendorSpend?: DashboardData['vendor_spend'];
	/** `monthly_trend`, oldest first. Pass `[]` to hide the trend card. */
	monthlyTrend?: DashboardData['monthly_trend'];
	/** The ONE count the API returns for the whole aging band set. */
	agingUnconverted?: number;
	/** Overrides folded onto the base `discount_capture` block. */
	discount?: Partial<DashboardDiscountCapture>;
	/**
	 * `reporting.unconverted_count` — invoices the Total Amount KPI counted at
	 * FACE value (`invoice_reporting_amount_sql` falls back rather than drops).
	 */
	totalFaceValue?: number;
	/**
	 * `total_paid_unconverted_count` / `total_pending_unconverted_count` —
	 * payments the Paid / Pending KPIs EXCLUDED (`payment_reporting_amount_sql`
	 * refuses a face-value fallback). The opposite rule from the count above.
	 */
	paidExcluded?: number;
	pendingExcluded?: number;
	/**
	 * `reporting.reporting_currency` — the code every KPI and chart figure is
	 * denominated in, and the one the page labels them with.
	 */
	reportingCurrency?: string;
	/** `upcoming_payments` — per-row FACE amounts, each in its own currency. */
	upcomingPayments?: DashboardData['upcoming_payments'];
}

/** The base payload — every count zero, both chart series populated. */
function base(patch: DashboardPatch): DashboardData {
	const reportingCurrency = patch.reportingCurrency ?? REPORTING_CURRENCY;
	return {
		total_invoices: 6,
		total_amount: 6000,
		reporting: {
			reporting_currency: reportingCurrency,
			total_amount: 6000,
			total_count: 6,
			unconverted_count: patch.totalFaceValue ?? 0
		},
		total_paid: 2000,
		total_pending: 4000,
		total_paid_reporting: 2000,
		total_pending_reporting: 4000,
		total_paid_unconverted_count: patch.paidExcluded ?? 0,
		total_pending_unconverted_count: patch.pendingExcluded ?? 0,
		total_rebates: 0,
		excluded_rebate_count: 0,
		open_exceptions: 0,
		touchless_rate: 0,
		stale_approvals: 0,
		pipeline: { new: 6 },
		vendor_spend: [],
		// `aging` is a face-value cross-currency sum in its entirety and carries
		// no count; `aging_reporting` is the five bands PLUS the one count. The
		// two are NOT the same shape, so a fixture must not serve one object for
		// both — that is the omission this module exists to have caught.
		aging: { current: 4000, days_30: 1000, days_60: 500, days_90: 300, days_90_plus: 200 },
		aging_reporting: {
			current: 4000,
			days_30: 1000,
			days_60: 500,
			days_90: 300,
			days_90_plus: 200,
			unconverted_count: patch.agingUnconverted ?? 0
		},
		monthly_trend: [],
		upcoming_payments: patch.upcomingPayments ?? [],
		discount_capture: {
			eligible_count: 0,
			captured_count: 0,
			missed_count: 0,
			pending_count: 0,
			captured_amount: 0,
			missed_amount: 0,
			pending_amount: 0,
			reporting_currency: reportingCurrency,
			captured_amount_reporting: 0,
			missed_amount_reporting: 0,
			pending_amount_reporting: 0,
			unconverted_count: 0,
			capture_rate_pct: null,
			insufficient_data: true
		}
	} satisfies DashboardData;
}

/**
 * A brand-new tenant's `GET /api/dashboard` — every figure a genuine zero,
 * every series empty.
 *
 * Deliberately NOT a `DashboardPatch` knob. `total_invoices` is not a free
 * field: a tenant with no invoices has no amounts, no aging bands, no pipeline
 * and no trend either, and a payload claiming zero invoices worth $6,000 is one
 * the backend cannot produce — exactly the class of fixture this module exists
 * to stop. The state it represents is the only one the dashboard's onboarding
 * EmptyState answers (`docs/decisions.md` §154), so it ships whole.
 */
export function emptyTenantDashboardResponse() {
	const payload = base({});
	const zeroBands = { current: 0, days_30: 0, days_60: 0, days_90: 0, days_90_plus: 0 };
	return {
		...payload,
		...NON_RENDERED_FIELDS,
		total_invoices: 0,
		total_amount: 0,
		reporting: { ...payload.reporting, total_amount: 0, total_count: 0 },
		total_paid: 0,
		total_pending: 0,
		total_paid_reporting: 0,
		total_pending_reporting: 0,
		pipeline: {},
		aging: zeroBands,
		aging_reporting: { ...zeroBands, unconverted_count: 0 }
	};
}

/** A full `GET /api/dashboard` body, ready to `JSON.stringify`. */
export function dashboardResponse(patch: DashboardPatch = {}) {
	const payload = base(patch);
	return {
		...payload,
		...NON_RENDERED_FIELDS,
		vendor_spend: patch.vendorSpend ?? payload.vendor_spend,
		monthly_trend: patch.monthlyTrend ?? payload.monthly_trend,
		discount_capture: { ...payload.discount_capture, ...patch.discount }
	};
}
