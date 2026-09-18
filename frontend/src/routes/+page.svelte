<script lang="ts">
	import { api } from '$lib/api';
	import { INVOICE_STATUS_LABEL_KEYS } from '$lib/types/invoice';
	import type { InvoiceStatus } from '$lib/types/invoice';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import {
		formatMoney,
		isPositiveAmount,
		parseMoneyForLayout,
		type MoneyAmount
	} from '$lib/utils/money';
	import { formatDate } from '$lib/utils/time';
	import { formatList } from '$lib/utils/list';
	import { partialLabels, totalUnconverted } from '$lib/utils/dashboardPartials';
	import type { DashboardData } from '$lib/types/analytics';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { currentLocale, m } from '$lib/i18n/store.svelte';
	import type { MessageKey } from '$lib/i18n/messages';
	// Decorative card-header glyphs (aria-hidden at the call site): the card's
	// <h2> is what names it.
	import IconPipeline from '~icons/material-symbols/account-tree-outline';
	import IconVendors from '~icons/material-symbols/storefront-outline';
	import IconAging from '~icons/material-symbols/hourglass-top-outline';
	import IconUpcoming from '~icons/material-symbols/event-upcoming-outline';
	import IconDiscount from '~icons/material-symbols/savings-outline';
	import IconTrend from '~icons/material-symbols/bar-chart';
	import IconArrow from '~icons/material-symbols/arrow-forward';

	// The response shape lives in `$lib/types/analytics.ts` (`DashboardData`,
	// `ReportingAgingBuckets`, `AgingBuckets`) rather than here. Declared
	// inline and un-exported, it was a shape only this file could compare a
	// payload against — so every e2e stub of `/api/dashboard` was a
	// hand-maintained literal that drifted silently the moment the API gained
	// a non-optional field. Exported, the fixtures `satisfies` it and
	// `pnpm check:e2e` turns that drift into a compile error.

	let data = $state<DashboardData | null>(null);
	let loading = $state(true);
	let error = $state(false);

	function load() {
		loading = true;
		error = false;
		api.get<DashboardData>('/api/dashboard').then((res) => {
			data = res;
			loading = false;
		}).catch(() => {
			// Surface a recoverable error instead of a blank page: without an
			// {:else} branch a failed fetch left data=null and rendered nothing.
			error = true;
			loading = false;
		});
	}

	$effect(() => {
		orgCurrency.ensureLoaded();
		load();
	});

	// The response has LANDED and says this tenant has no invoices at all — the
	// one state where the onboarding EmptyState replaces the KPI row rather than
	// sitting beside it. Keyed on `data`, never on `total_invoices` alone, so it
	// is false while the answer is still out: a row that vanished the moment the
	// page started asking would be the collapse `docs/decisions.md` §125 exists
	// to remove. One owner, because the markup tests it twice (the row's branch
	// and the charts').
	const isEmptyTenant = $derived(!!data && data.total_invoices === 0);

	// Dashboard figures are tenant-wide roll-ups with no per-row currency,
	// so they render in the org's configured default currency.
	function fmt(amount: MoneyAmount): string {
		return formatMoney(amount, { currency: orgCurrency.currency, whole: true });
	}

	function fmtFull(amount: MoneyAmount): string {
		return formatMoney(amount, { currency: orgCurrency.currency });
	}

	/** Format a figure in the currency the RESPONSE says it is denominated in.
	 *
	 *  The KPIs above are tenant-wide rollups with no per-row currency, so they
	 *  render in the separately-fetched org default. `discount_capture` is not
	 *  one of those: it carries its OWN `reporting_currency`, and labelling its
	 *  amounts with the org-settings code would let the page print "3 rows with
	 *  no exchange rate into GBP" directly above a column of `$`. A currency
	 *  label has to come from the same payload as the number it labels — the
	 *  same rule `/cfo`'s `fmtIn` follows. */
	function fmtIn(amount: MoneyAmount, currency: string | undefined): string {
		return formatMoney(amount, { currency: currency || orgCurrency.currency, whole: true });
	}

	// Due-date cell: locale-aware short date, no year (the shared helper drives
	// its locale off the active i18n picker).
	function fmtDue(iso: string | null): string {
		return formatDate(iso, '—', { month: 'short', day: 'numeric' });
	}

	// Pipeline order
	const PIPELINE_ORDER: InvoiceStatus[] = [
		'new', 'pending', 'ready_for_review', 'approved',
		'sending_to_erp', 'sent_to_erp', 'posted_in_erp',
		'payment_scheduled', 'paid', 'done', 'failed', 'rejected',
	];

	const PIPELINE_COLORS: Record<string, string> = {
		new: '#638cff',
		pending: '#d4940a',
		ready_for_review: '#8b5cf6',
		approved: '#1fa86a',
		rejected: '#e04040',
		sending_to_erp: '#638cff',
		sent_to_erp: '#3b82f6',
		posted_in_erp: '#1fa86a',
		payment_scheduled: '#059669',
		paid: '#047857',
		done: '#6b7280',
		failed: '#e04040',
	};

	const AGING_COLORS = ['#1fa86a', '#d4940a', '#f59e0b', '#ea580c', '#e04040'];

	// Each band carries BOTH its money figure (`value`, rendered through
	// `fmt` / `fmtFull`) and its chart geometry (`layout`). The five bands are
	// money — `backend/app/schemas/dashboard.py::AgingBuckets` annotates them
	// `MoneyAmount` — so `sum + b.value` and `b.value / agingTotal` were raw
	// arithmetic on currency, which the frontend type only started refusing
	// once these shapes moved into `$lib/types/analytics.ts` and the money-type
	// ratchet could see them. `parseMoneyForLayout` is the one sanctioned hop
	// and is named so the call site refuses the wrong use: `layout` drives a
	// width and must never be rendered or read as a business fact.
	const agingBuckets = $derived(
		data
			? [
				{ label: m('dashboard.aging.current'), value: data.aging_reporting.current, color: AGING_COLORS[0] },
				{ label: m('dashboard.aging.days30'), value: data.aging_reporting.days_30, color: AGING_COLORS[1] },
				{ label: m('dashboard.aging.days60'), value: data.aging_reporting.days_60, color: AGING_COLORS[2] },
				{ label: m('dashboard.aging.days90'), value: data.aging_reporting.days_90, color: AGING_COLORS[3] },
				{ label: m('dashboard.aging.days90plus'), value: data.aging_reporting.days_90_plus, color: AGING_COLORS[4] },
			].map((b) => ({ ...b, layout: parseMoneyForLayout(b.value) }))
			: []
	);

	// A chart SCALE, not a figure — never rendered, never reported.
	let agingTotal = $derived(agingBuckets.reduce((sum, b) => sum + b.layout, 0));

	// Chart SCALES, not figures. `parseMoneyForLayout` is the one sanctioned
	// money -> number hop and is named so the call site refuses the wrong use:
	// the result drives a bar width and must never be rendered, summed, or
	// compared as a business fact. `Math.max` over raw money is exactly the
	// float arithmetic on currency `frontend/CLAUDE.md` forbids.
	let maxVendorSpend = $derived(
		data && data.vendor_spend.length > 0
			? Math.max(...data.vendor_spend.map((v) => parseMoneyForLayout(v.amount)))
			: 1
	);

	let maxTrendAmount = $derived(
		data && data.monthly_trend.length > 0
			? Math.max(...data.monthly_trend.map((t) => parseMoneyForLayout(t.reporting_amount)))
			: 1
	);

	// True whenever any reporting-currency rollup left rows out for lack of a
	// locked exchange rate — the number above is then a floor, not the exact
	// total, and that has to be visible right beside it (decisions §35).
	let hasUnconvertedRows = $derived(
		data
			? data.reporting.unconverted_count > 0 ||
				data.total_paid_unconverted_count > 0 ||
				data.total_pending_unconverted_count > 0
			: false
	);

	// The three CHART disclosures. `hasUnconvertedRows` above covers the KPI
	// row; each chart carries its own because each answers a different
	// question about the same fallback — which vendor's rank is unreliable,
	// how much of the aging picture is part-converted, which month's bar not
	// to trust. A single page-level banner would say "something here mixes
	// currencies" and leave the reader to guess where (decisions §35).
	// The rules live in `utils/dashboardPartials` so they have one owner and
	// their test asserts behaviour rather than restating its own `reduce`.
	let vendorSpendUnconverted = $derived(totalUnconverted(data?.vendor_spend));
	let vendorSpendPartialNames = $derived(partialLabels(data?.vendor_spend, (v) => v.vendor));
	let trendUnconverted = $derived(totalUnconverted(data?.monthly_trend));
	let trendPartialMonths = $derived(partialLabels(data?.monthly_trend, (t) => t.month));

	// The page intro: a greeting keyed on the reader's own clock and today's
	// date in the active locale. Presentation only — it reads nothing the
	// figures below depend on, and with no name on the profile it is just the
	// date rather than a greeting addressed to nobody.
	const now = new Date();
	const greetingKey: MessageKey =
		now.getHours() < 12
			? 'dashboard.greeting.morning'
			: now.getHours() < 18
				? 'dashboard.greeting.afternoon'
				: 'dashboard.greeting.evening';
	const userName = $derived(auth.user?.full_name?.trim() ?? '');
	// Re-read on a locale switch: `formatDate` takes its locale from the i18n
	// store, which only the `currentLocale()` read makes this derivation track.
	const todayLabel = $derived.by(() => {
		void currentLocale();
		return formatDate(now.toISOString(), '', { weekday: 'long', month: 'long', day: 'numeric' });
	});
</script>

<PageHeader title={m('dashboard.title')}>
	<!-- The zero-invoice EmptyState is the ONE thing that REPLACES the KPI row,
	     and it does so only once the response has LANDED — `isEmptyTenant` is
	     false while `data` is null. So an empty tenant sees a pending row that
	     then gives way to the empty state. That hand-off was chosen deliberately:
	     the alternative is gating the row on `total_invoices > 0`, which is
	     gating it on the answer, and it collapses the app's most-visited KPI row
	     on every load of every tenant to spare the empty one a transition.
	     `docs/decisions.md` §154. -->
	{#if isEmptyTenant}
		<EmptyState
			icon="📄"
			heading={m('emptyState.dashboard.heading')}
			description={m('emptyState.dashboard.description')}
			actionLabel={m('emptyState.dashboard.action')}
			actionHref="/invoices"
			testId="dashboard-empty-state"
		/>
	{:else}
		<!-- KPI row — rendered on every other state, never gated on the response
		     (`docs/decisions.md` §125 / §143). It used to sit inside
		     `{:else if data}`, which made the highest-traffic KPI row in the app
		     the last one still collapsing to nothing while its own answer was in
		     flight.

		     A FAILED load leaves the cards absent-but-not-loading — an
		     `unavailable` row — with the error banner and its Retry directly
		     below saying why, which is the placement /cfo uses for the same
		     reason: five dashes are what the page knows, and the banner is why. -->
		<div class="kpi-row">
			<KpiCard
				value={data?.total_invoices ?? null}
				label={m('dashboard.kpi.invoices')}
				pending={loading}
			/>
			<KpiCard
				value={data ? fmt(data.reporting.total_amount) : null}
				label={m('dashboard.kpi.totalAmount')}
				pending={loading}
			/>
			<KpiCard
				value={data ? fmt(data.total_paid_reporting) : null}
				label={m('dashboard.kpi.paid')}
				pending={loading}
			/>
			<KpiCard
				value={data ? fmt(data.total_pending_reporting) : null}
				label={m('dashboard.kpi.pending')}
				pending={loading}
			/>
			<KpiCard
				value={data ? `${data.touchless_rate}%` : null}
				label={m('dashboard.kpi.touchlessRate')}
				highlight={data && data.touchless_rate >= 80 ? 'green' : null}
				pending={loading}
			/>
			<!-- The five above are the row's spine: every tenant has those
			     figures, so a dash on each is an honest "not counted yet". The
			     cards below exist only when there is something to report, and the
			     response is what decides that — so they stay gated on `data`
			     instead of taking `pending`. A pending placeholder for a card that
			     may never render announces that a figure is coming when none is,
			     which is the failure §143 gates the user-triggered rows on. -->
			{#if data}
				{#if data.open_exceptions > 0}
					<a href="/exceptions" class="kpi highlight-red kpi-link">
						<span class="kpi-value">{data.open_exceptions}</span>
						<span class="kpi-label">{m('dashboard.kpi.exceptions')}</span>
					</a>
				{/if}
				{#if data.stale_approvals > 0}
					<KpiCard value={data.stale_approvals} label={m('dashboard.kpi.staleApprovals')} highlight="red" />
				{/if}
				{#if isPositiveAmount(data.total_rebates)}
					<KpiCard
						value={fmt(data.total_rebates)}
						label={m('dashboard.kpi.rebatesEarned')}
						highlight="green"
						sub={(data.excluded_rebate_count ?? 0) > 0
							? m('dashboard.kpi.rebatesExcluded', {
									n: data.excluded_rebate_count ?? 0,
									currency: data.reporting.reporting_currency
								})
							: null}
					/>
				{/if}
				{#if data.discount_capture.eligible_count > 0}
					<!-- The `sub` line is the qualifier on this headline figure, and
					     the unconverted disclosure OUTRANKS the capture rate for it:
					     a rate is context, an unconverted count means the number
					     above it mixes currencies. The full fold + a `role="alert"`
					     banner live in the card below. -->
					<KpiCard
						value={fmtIn(
							data.discount_capture.captured_amount_reporting,
							data.discount_capture.reporting_currency
						)}
						label={m('dashboard.kpi.discountsCaptured')}
						highlight="green"
						sub={data.discount_capture.unconverted_count > 0
							? m('dashboard.discount.unconvertedShort', {
									n: data.discount_capture.unconverted_count,
									currency: data.discount_capture.reporting_currency
								})
							: data.discount_capture.insufficient_data
								? m('dashboard.discount.rateUnknown')
								: m('dashboard.discount.rate', {
										pct: data.discount_capture.capture_rate_pct ?? 0
									})}
					/>
				{/if}
			{/if}
		</div>
	{/if}

	{#if error}
		<div class="dashboard-error" role="alert">
			<p>{m('dashboard.error.loadFailed')}</p>
			<button class="btn-primary" onclick={load}>{m('dashboard.error.retry')}</button>
		</div>
	{:else if loading}
		<p class="loading">{m('common.loading')}</p>
	{:else if data && !isEmptyTenant}

		{#if hasUnconvertedRows}
			<p class="dashboard-skipped" role="alert" data-testid="unconverted-rollup">
				{m('dashboard.reporting.unconverted', { currency: data.reporting.reporting_currency })}
			</p>
		{/if}

		<div class="charts-grid">
			<!-- Invoice Pipeline -->
			<div class="chart-card">
				<h2>{m('dashboard.chart.pipeline')}</h2>
				<div class="pipeline">
					{#each PIPELINE_ORDER.filter(s => (data?.pipeline[s] ?? 0) > 0) as status}
						{@const count = data?.pipeline[status] ?? 0}
						{@const pct = data ? Math.max(count / data.total_invoices * 100, 4) : 0}
						<div class="pipeline-row">
							<span class="pipeline-label">{m(INVOICE_STATUS_LABEL_KEYS[status])}</span>
							<div class="pipeline-bar-bg">
								<div class="pipeline-bar" style="width:{pct}%;background:{PIPELINE_COLORS[status] ?? '#888'}"></div>
							</div>
							<span class="pipeline-count">{count}</span>
						</div>
					{/each}
				</div>
			</div>

			<!-- Spend by Vendor -->
			<div class="chart-card">
				<h2>{m('dashboard.chart.topVendors')}</h2>
				<!-- A vendor whose invoices could not all be converted is ranked
				     on a total that is not in the same currency as the ones
				     above and below it. The card-level notice names those
				     vendors; the row itself is marked so the eye finds them in
				     the bars. -->
				{#if vendorSpendUnconverted > 0}
					<p class="dashboard-skipped" role="alert" data-testid="unconverted-vendor-spend">
						{m('dashboard.vendorSpend.unconverted', {
							n: vendorSpendUnconverted,
							currency: data.reporting.reporting_currency,
							vendors: formatList(vendorSpendPartialNames)
						})}
					</p>
				{/if}
				{#if data.vendor_spend.length > 0}
					<div class="vendor-bars">
						{#each data.vendor_spend as v}
							<div class="vendor-row">
								<span class="vendor-name" title={v.vendor}>{v.vendor}</span>
								<div class="vendor-bar-bg">
									<div
										class="vendor-bar"
										class:partial={v.unconverted_count > 0}
										style="width:{(parseMoneyForLayout(v.amount) / maxVendorSpend) * 100}%"
									></div>
								</div>
								<span
									class="vendor-amount"
									class:partial-amount={v.unconverted_count > 0}
								>{fmt(v.amount)}</span>
							</div>
						{/each}
					</div>
				{:else}
					<p class="empty">{m('dashboard.empty.vendors')}</p>
				{/if}
			</div>

			<!-- Aging -->
			<div class="chart-card">
				<h2>{m('dashboard.chart.aging')}</h2>
				<!-- ONE notice for the band set, matching the single
				     `aging_reporting.unconverted_count` the API returns: the
				     actionable fact ("some of this is unconverted") is the same
				     whichever band the row fell into, and the invoices behind it
				     are listable from /invoices. -->
				{#if data.aging_reporting.unconverted_count > 0}
					<p class="dashboard-skipped" role="alert" data-testid="unconverted-aging">
						{m('dashboard.aging.unconverted', {
							n: data.aging_reporting.unconverted_count,
							currency: data.reporting.reporting_currency
						})}
					</p>
				{/if}
					{#if agingTotal > 0}
					<div class="aging-bar">
						{#each agingBuckets as bucket}
							{#if bucket.layout > 0}
								<div
									class="aging-segment"
									style="width:{(bucket.layout / agingTotal * 100)}%;background:{bucket.color}"
									title="{bucket.label}: {fmtFull(bucket.value)}"
								></div>
							{/if}
						{/each}
					</div>
					<div class="aging-legend">
						{#each agingBuckets as bucket}
							<div class="aging-item">
								<span class="aging-dot" style="background:{bucket.color}"></span>
								<span class="aging-label">{bucket.label}</span>
								<span class="aging-value">{fmt(bucket.value)}</span>
							</div>
						{/each}
					</div>
				{:else}
					<p class="empty">{m('dashboard.empty.aging')}</p>
				{/if}
			</div>

			<!-- Upcoming Payments -->
			<div class="chart-card">
				<h2>{m('dashboard.chart.upcoming')}</h2>
				{#if data.upcoming_payments.length > 0}
					<div class="upcoming-list">
						{#each data.upcoming_payments as inv}
							<div class="upcoming-row" class:overdue={inv.is_overdue}>
								<div class="upcoming-info">
									<span class="upcoming-vendor">{inv.vendor_name}</span>
									<span class="upcoming-inv">{inv.invoice_number}</span>
								</div>
								<span class="upcoming-amount">{fmtFull(inv.amount)}</span>
								<span class="upcoming-date" class:overdue-text={inv.is_overdue}>
									{fmtDue(inv.due_date)}
									{#if inv.is_overdue}
										<Badge tone="danger" variant="overdue-badge">{m('dashboard.overdue')}</Badge>
									{/if}
								</span>
							</div>
						{/each}
					</div>
				{:else}
					<p class="empty">{m('dashboard.empty.upcoming')}</p>
				{/if}
			</div>

			<!-- Early-payment discounts. Three buckets, not two: a window that
			     has not closed yet is PENDING — still capturable — and folding
			     it into "missed" reports live opportunity as forgone savings.
			     Amounts are the reporting-currency figures, labelled with the
			     currency THIS payload names. -->
			<div class="chart-card" data-testid="discount-capture">
				<h2>{m('dashboard.chart.discountCapture')}</h2>
				<!-- The disclosure sits with the figure, not in a tooltip: a
				     non-zero count means some eligible rows entered the totals
				     below at face value because no rate into the reporting
				     currency could be established, so the amounts mix
				     currencies (decisions §35). -->
				{#if data.discount_capture.unconverted_count > 0}
					<p class="dashboard-skipped" role="alert" data-testid="discount-capture-unconverted">
						{m('dashboard.discount.unconverted', {
							n: data.discount_capture.unconverted_count,
							currency: data.discount_capture.reporting_currency
						})}
					</p>
				{/if}
				{#if data.discount_capture.eligible_count === 0}
					<p class="empty">{m('dashboard.empty.discountCapture')}</p>
				{:else}
					{@const dc = data.discount_capture}
					<div class="discount-fold">
						<div class="discount-row">
							<span class="discount-label">{m('dashboard.discount.captured')}</span>
							<span class="discount-amount captured"
								>{fmtIn(dc.captured_amount_reporting, dc.reporting_currency)}</span
							>
							<span class="discount-count">{m('dashboard.discount.count', { n: dc.captured_count })}</span>
						</div>
						<div class="discount-row">
							<span class="discount-label">{m('dashboard.discount.missed')}</span>
							<span class="discount-amount missed"
								>{fmtIn(dc.missed_amount_reporting, dc.reporting_currency)}</span
							>
							<span class="discount-count">{m('dashboard.discount.count', { n: dc.missed_count })}</span>
						</div>
						<div class="discount-row">
							<span class="discount-label">{m('dashboard.discount.pending')}</span>
							<span class="discount-amount pending"
								>{fmtIn(dc.pending_amount_reporting, dc.reporting_currency)}</span
							>
							<span class="discount-count">{m('dashboard.discount.count', { n: dc.pending_count })}</span>
						</div>
					</div>
					<!-- `null`, never 0%, until something has actually been
					     decided — 0% reads as "we captured none of them", the
					     opposite of "nothing has come due yet". -->
					<p class="discount-rate" data-testid="discount-capture-rate">
						{#if dc.insufficient_data}
							{m('dashboard.discount.rateUnknown')}
						{:else}
							{m('dashboard.discount.rate', { pct: dc.capture_rate_pct ?? 0 })}
						{/if}
					</p>
				{/if}
			</div>

			<!-- Monthly Trend -->
			{#if data.monthly_trend.length > 0}
				<div class="chart-card wide">
					<h2>{m('dashboard.chart.monthlyVolume')}</h2>
					<!-- Named months, not a series total. A single foreign
					     vendor onboarding in March contaminates March, and the
					     chart is read bar against bar — a whole-series count
					     would not say which step not to trust. -->
					{#if trendUnconverted > 0}
						<p class="dashboard-skipped" role="alert" data-testid="unconverted-trend">
							{m('dashboard.trend.unconverted', {
								n: trendUnconverted,
								currency: data.reporting.reporting_currency,
								months: formatList(trendPartialMonths)
							})}
						</p>
					{/if}
					<div class="trend-chart">
						{#each data.monthly_trend as month}
							<div class="trend-bar-group">
								<div class="trend-bar-wrap">
									<div
										class="trend-bar"
										class:partial={month.unconverted_count > 0}
										style="height:{maxTrendAmount > 0
											? (parseMoneyForLayout(month.reporting_amount) / maxTrendAmount) * 100
											: 0}%"
										title="{fmt(month.reporting_amount)} ({month.count} invoices)"
									></div>
								</div>
								<span class="trend-label">{month.month.slice(5)}</span>
								<span
									class="trend-value"
									class:partial-amount={month.unconverted_count > 0}
								>{fmt(month.reporting_amount)}</span>
							</div>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</PageHeader>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */
	h2 {
		margin: 0 0 14px;
		font-size: 0.88rem;
		font-weight: 600;
		color: var(--text);
	}

	.loading {
		color: var(--text-muted);
	}

	.dashboard-error {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 12px;
		color: var(--text-muted);
		padding: 24px 0;
	}

	.empty {
		color: var(--text-muted);
		font-size: 0.85rem;
		text-align: center;
		padding: 20px;
	}

	.dashboard-skipped {
		color: var(--warning-on-tint);
		font-size: 0.85rem;
		font-weight: 600;
		margin: -8px 0 16px;
	}

	/* Early-payment discount fold. The `.dashboard-skipped` disclosure inside
	   this card re-uses the rule above, so its top margin is reset there. */
	.chart-card .dashboard-skipped {
		margin: 0 0 12px;
	}

	.discount-fold {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.discount-row {
		display: grid;
		grid-template-columns: 1fr auto auto;
		align-items: baseline;
		gap: 10px;
	}

	.discount-label {
		color: var(--text-muted);
		font-size: 0.85rem;
	}

	.discount-amount {
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}

	/* Text on the card surface, so these are the base tokens — never their
	   `-strong` companions, which are fills for white text. */
	.discount-amount.captured {
		color: var(--success);
	}

	.discount-amount.missed {
		color: var(--danger);
	}

	.discount-amount.pending {
		color: var(--text);
	}

	.discount-count {
		color: var(--text-muted);
		font-size: 0.78rem;
		min-width: 5.5ch;
		text-align: right;
	}

	.discount-rate {
		color: var(--text-muted);
		font-size: 0.82rem;
		margin: 14px 0 0;
	}

	/* Exceptions KPI renders as a link, so it stays inline markup using the
	   global .kpi classes plus this bespoke hover affordance. */
	.kpi-link {
		text-decoration: none;
		cursor: pointer;
		transition: transform 0.1s;
	}

	.kpi-link:hover {
		transform: translateY(-2px);
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
	}

	/* Charts Grid */
	.charts-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 16px;
	}

	.chart-card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 18px 20px;
		/* WCAG 1.4.10: grid items default to min-width:auto, which keeps the
		   chart at its intrinsic width and scrolls the whole page on a narrow
		   viewport. min-width:0 lets the track (and chart) shrink to reflow. */
		min-width: 0;
	}

	/* Single column on narrow viewports so the two-up charts reflow. */
	@media (max-width: 700px) {
		.charts-grid {
			grid-template-columns: 1fr;
		}
	}

	.chart-card.wide {
		grid-column: 1 / -1;
	}

	/* Pipeline */
	.pipeline {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.pipeline-row {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.pipeline-label {
		width: 110px;
		font-size: 0.78rem;
		color: var(--text-muted);
		text-align: right;
		flex-shrink: 0;
	}

	.pipeline-bar-bg {
		flex: 1;
		height: 20px;
		background: var(--bg);
		border-radius: 4px;
		overflow: hidden;
	}

	.pipeline-bar {
		height: 100%;
		border-radius: 4px;
		transition: width 0.3s ease;
	}

	.pipeline-count {
		width: 30px;
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--text);
		text-align: right;
	}

	/* Vendor Spend */
	.vendor-bars {
		display: flex;
		flex-direction: column;
		gap: 7px;
	}

	.vendor-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.vendor-name {
		width: 120px;
		font-size: 0.78rem;
		color: var(--text);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex-shrink: 0;
	}

	.vendor-bar-bg {
		flex: 1;
		height: 16px;
		background: var(--bg);
		border-radius: 3px;
		overflow: hidden;
	}

	.vendor-bar {
		height: 100%;
		background: var(--accent);
		border-radius: 3px;
		opacity: 0.7;
	}

	.vendor-amount {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		width: 70px;
		text-align: right;
		flex-shrink: 0;
	}

	/* A bar or figure whose reporting-currency total was built partly from
	   face values, because no locked rate bridged some of its rows. The
	   authoritative statement is the `role="alert"` notice at the top of the
	   card — this only points at WHICH row or bar it means, which is why it is
	   a texture and an underline rather than a colour: colour alone would fail
	   WCAG 1.4.1 and carry no meaning to a screen reader either way. */
	.vendor-bar.partial,
	.trend-bar.partial {
		background-image: repeating-linear-gradient(
			135deg,
			transparent 0 4px,
			var(--surface) 4px 7px
		);
	}

	.partial-amount {
		text-decoration: underline dotted;
		text-underline-offset: 3px;
	}

	/* Aging */
	.aging-bar {
		display: flex;
		height: 24px;
		border-radius: 6px;
		overflow: hidden;
		margin-bottom: 12px;
	}

	.aging-segment {
		transition: width 0.3s ease;
		cursor: help;
	}

	.aging-legend {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
	}

	.aging-item {
		display: flex;
		align-items: center;
		gap: 5px;
	}

	.aging-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.aging-label {
		font-size: 0.75rem;
		color: var(--text-muted);
	}

	.aging-value {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--text);
	}

	/* Upcoming */
	.upcoming-list {
		display: flex;
		flex-direction: column;
		gap: 0;
	}

	.upcoming-row {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 0;
		border-bottom: 1px solid var(--border);
	}

	.upcoming-row:last-child {
		border-bottom: none;
	}

	.upcoming-row.overdue {
		background: rgba(224, 64, 64, 0.04);
		margin: 0 -20px;
		padding: 8px 20px;
	}

	.upcoming-info {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}

	.upcoming-vendor {
		font-size: 0.82rem;
		font-weight: 500;
		color: var(--text);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.upcoming-inv {
		font-size: 0.72rem;
		color: var(--text-muted);
		font-family: 'SF Mono', 'Cascadia Code', monospace;
	}

	.upcoming-amount {
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--text);
		white-space: nowrap;
	}

	.upcoming-date {
		font-size: 0.78rem;
		color: var(--text-muted);
		white-space: nowrap;
		min-width: 80px;
		text-align: right;
	}

	.overdue-text {
		color: #f06464;
	}

	/* Colour + metrics come from `ui/Badge.svelte`; this keeps only the spacing
	   that is the CALLER's business — the gap between the due date and the flag.
	   `/payments` already renders this same flag through the primitive, so the
	   dashboard's own copy (0.62rem, alpha .12, a literal #f06464 where the
	   palette says --danger-on-tint) was the same badge at a second size. */
	:global(.overdue-badge) {
		margin-left: 4px;
	}

	/* Trend */
	.trend-chart {
		display: flex;
		align-items: flex-end;
		gap: 8px;
		height: 160px;
		padding-top: 10px;
	}

	.trend-bar-group {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 4px;
		height: 100%;
	}

	.trend-bar-wrap {
		flex: 1;
		width: 100%;
		display: flex;
		align-items: flex-end;
	}

	.trend-bar {
		width: 100%;
		background: var(--accent);
		border-radius: 4px 4px 0 0;
		opacity: 0.7;
		min-height: 4px;
		transition: height 0.3s ease;
		cursor: help;
	}

	.trend-bar:hover {
		opacity: 1;
	}

	.trend-label {
		font-size: 0.7rem;
		color: var(--text-muted);
	}

	.trend-value {
		font-size: 0.68rem;
		color: var(--text-muted);
		font-weight: 500;
	}

	@media (max-width: 768px) {
		.charts-grid {
			grid-template-columns: 1fr;
		}

		.kpi-row {
			flex-direction: column;
		}
	}
</style>
