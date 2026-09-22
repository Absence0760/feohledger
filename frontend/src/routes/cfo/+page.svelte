<script lang="ts">
	import { api } from '$lib/api';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import ByEntityBreakdown from '$lib/components/analytics/ByEntityBreakdown.svelte';
	import CfoMetrics from '$lib/components/analytics/CfoMetrics.svelte';
	import ScheduledReportsPanel from '$lib/components/analytics/ScheduledReportsPanel.svelte';
	import ForecastVariancePanel from './ForecastVariancePanel.svelte';
	import { formatMoney, isNegativeAmount, isPositiveAmount, parseMoneyForLayout } from '$lib/utils/money';
	import type { MoneyAmount } from '$lib/utils/money';
	import { formatPeriod } from '$lib/utils/time';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { untrack } from 'svelte';
	import { openingBalanceSkipKey } from './openingBalanceNotice';
	import { formatUtilization, overBudgetCount } from './budgetRollupSummary';
	import { getBudgetRollup } from '$lib/api/budgets';
	import type { BudgetRollup } from '$lib/types/budget';
	import type {
		CashflowForecast,
		CashflowGranularity,
		CashPosition,
		WhatIfScenarios
	} from '$lib/types/analytics';

	let granularity = $state<CashflowGranularity>('week');
	let horizonDays = $state(90);
	let openingBalance = $state('');
	let threshold = $state('');

	let forecast = $state<CashflowForecast | null>(null);
	let whatif = $state<WhatIfScenarios | null>(null);
	let position = $state<CashPosition | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Org-wide budget-vs-actual. Fetched INDEPENDENTLY of the cash-flow panels
	// and rendered outside their `{#if}` — same reasoning as
	// `ScheduledReportsPanel`: it takes no control input from this page, and a
	// failed forecast must not take the only consolidated budget view down with
	// it. `null` = not loaded yet; `insufficient_data` = loaded and there are no
	// budgets, which is a different statement and gets different copy.
	let budgetRollup = $state<BudgetRollup | null>(null);
	let budgetRollupError = $state(false);

	const budgetsOverCount = $derived(overBudgetCount(budgetRollup));

	// The forecast, what-if and cash-position endpoints each denominate their
	// figures through `resolve_reporting_currency`, and land together in one
	// `load()` — but only the cash position NAMES the answer
	// (`opening_balance_currency`). So the forecast and what-if figures are
	// labelled from that sibling payload; the `orgCurrency` store, which sees
	// three of the backend's four rungs and answers `null` past them, is only
	// the fallback while nothing has landed (decisions §119, §200).
	const reportingCurrency = $derived(position?.opening_balance_currency || orgCurrency.currency);

	function fmt(amount: MoneyAmount): string {
		return formatMoney(amount, { currency: reportingCurrency, whole: true });
	}

	/** Format a figure in the currency the RESPONSE says it is denominated in.
	 *
	 *  The cash-position curve carries its own `opening_balance_currency` — "the
	 *  reporting currency the whole curve is denominated in" — and the two
	 *  warning banners above the table already print it. The table itself went
	 *  through `fmt`, i.e. the separately-fetched org-settings currency, so the
	 *  page could render "3 outflows could not be converted to GBP" directly
	 *  above a column of `$`. A currency label has to come from the same payload
	 *  as the number it labels. Mirrors `CfoMetrics.svelte`'s own `fmtIn`. */
	function fmtIn(amount: MoneyAmount, currency: string | undefined): string {
		return formatMoney(amount, { currency: currency || orgCurrency.currency, whole: true });
	}

	// `opening_balance_provider_skipped` means a live bank balance EXISTED and the
	// resolution chain refused it, so the curve starts from the next link down.
	// The mapping (and its deliberate speak-anyway fallback) is the pure
	// `openingBalanceNotice` helper, unit-tested beside this page.
	let openingSkipReasonKey = $derived(
		openingBalanceSkipKey(position?.opening_balance_provider_skipped)
	);

	// Sequences `load` (latest-issued wins) so a response for an earlier control
	// setting can't land after a faster later one. This is the money-decision
	// surface: without it, typing `50000` into Opening balance issued five
	// overlapping rounds of three requests each, and whichever resolved LAST won
	// — so the curve, the running balance and the "below minimum balance" breach
	// banner could settle on the figures for `500` while the input read `50000`,
	// with nothing on screen saying which number produced them.
	// See `frontend/CLAUDE.md` § Sequencing list fetches.
	const loadSequence = createRequestSequencer();

	// The two free-text values the newest issued request carried. Written by
	// `load()`, read by the debounce effect below — the same `appliedSearch`
	// discipline `/expenses` and `/vendors` use for their search boxes.
	let appliedOpening = $state('');
	let appliedThreshold = $state('');

	// Read WITHOUT registering a dependency. `load()` is called synchronously
	// from the discrete-control `$effect` below and Svelte tracks reads
	// transitively through called functions, so a plain read here would make
	// that effect depend on these two inputs and fire an immediate,
	// un-debounced request on every keystroke — racing the 300ms timer that
	// exists to prevent exactly that (issue #168 / `docs/decisions.md` §53).
	function currentOpening(): string {
		return untrack(() => openingBalance).trim();
	}
	function currentThreshold(): string {
		return untrack(() => threshold).trim();
	}

	async function load() {
		const token = loadSequence.start();
		loading = true;
		error = null;
		const opening = currentOpening();
		const minBalance = currentThreshold();
		appliedOpening = opening;
		appliedThreshold = minBalance;
		try {
			const base = `granularity=${granularity}&horizon_days=${horizonDays}`;
			const posQs =
				`${base}` +
				(opening ? `&opening_balance=${encodeURIComponent(opening)}` : '') +
				(minBalance ? `&min_balance_threshold=${encodeURIComponent(minBalance)}` : '');
			const [f, w, p] = await Promise.all([
				api.get<CashflowForecast>(`/api/analytics/cashflow_forecast?${base}`),
				api.get<WhatIfScenarios>(`/api/analytics/cashflow_whatif?${base}`),
				api.get<CashPosition>(`/api/analytics/cash_position?${posQs}`)
			]);
			// Superseded by a newer request — discard rather than publish figures
			// the controls no longer describe.
			if (!loadSequence.canCommit(token)) return;
			forecast = f;
			whatif = w;
			position = p;
		} catch (e) {
			if (loadSequence.isCurrentRequest(token)) {
				error = e instanceof Error ? e.message : m('cfo.error.loadFailed');
			}
		} finally {
			// `isCurrentRequest`, never `canCommit` — a superseded response must
			// not clear the spinner the newest request owns.
			if (loadSequence.isCurrentRequest(token)) loading = false;
		}
	}

	// The two segmented controls are discrete actions: load immediately.
	$effect(() => {
		orgCurrency.ensureLoaded();
		void granularity;
		void horizonDays;
		load();
	});

	// One-shot: the rollup takes none of this page's controls, so it neither
	// re-fires nor needs the request sequencer.
	$effect(() => {
		getBudgetRollup()
			.then((r) => {
				budgetRollup = r;
				budgetRollupError = false;
			})
			.catch(() => {
				budgetRollupError = true;
			});
	});

	// The two free-text money inputs are debounced — one request per pause, not
	// per keystroke. Declares its own dependencies by reading them directly.
	let inputTimer: ReturnType<typeof setTimeout>;
	$effect(() => {
		const nextOpening = openingBalance.trim();
		const nextThreshold = threshold.trim();
		clearTimeout(inputTimer);
		if (nextOpening === appliedOpening && nextThreshold === appliedThreshold) return;
		inputTimer = setTimeout(load, 300);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone, running a fetch against a route the user left.
		return () => clearTimeout(inputTimer);
	});

	// Chart geometry only. `parseMoneyForLayout` is the one sanctioned
	// money-string → number conversion; the figures beside each bar are
	// rendered from the exact strings via `fmt`, never from these numbers.
	let maxScheduled = $derived(
		Math.max(1, ...(forecast?.periods ?? []).map((p) => parseMoneyForLayout(p.scheduled_amount)))
	);

	async function exportCsv() {
		try {
			const blob = await api.downloadBlob(
				`/api/analytics/export/cashflow_forecast?granularity=${granularity}&horizon_days=${horizonDays}`
			);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `cashflow_forecast_${new Date().toISOString().slice(0, 10)}.csv`;
			document.body.appendChild(a);
			a.click();
			a.remove();
			URL.revokeObjectURL(url);
		} catch (e) {
			error = e instanceof Error ? e.message : m('cfo.error.exportFailed');
		}
	}

	function granLabel(g: string): string {
		return g === 'day' ? m('cfo.gran.day') : g === 'month' ? m('cfo.gran.month') : m('cfo.gran.week');
	}
</script>

<PageHeader title={m('cfo.title')}>
	{#snippet actions()}
		<button class="btn-primary" onclick={exportCsv} data-testid="export-csv">{m('cfo.exportCsv')}</button>
	{/snippet}

	<div class="cf-controls">
		<div class="control">
			<span class="control-label">{m('cfo.control.granularity')}</span>
			<div class="seg">
				{#each ['day', 'week', 'month'] as g (g)}
					<button
						class="seg-btn"
						class:active={granularity === g}
						onclick={() => (granularity = g as CashflowGranularity)}
					>{granLabel(g)}</button>
				{/each}
			</div>
		</div>
		<div class="control">
			<span class="control-label">{m('cfo.control.horizon')}</span>
			<div class="seg">
				{#each [30, 90, 180, 365] as h (h)}
					<button class="seg-btn" class:active={horizonDays === h} onclick={() => (horizonDays = h)}>
						{m('cfo.control.horizonDays', { days: h })}
					</button>
				{/each}
			</div>
		</div>
		<label class="control">
			<span class="control-label">{m('cfo.control.openingBalance')}</span>
			<input
				class="cf-input"
				type="text"
				inputmode="decimal"
				placeholder={m('cfo.control.openingPlaceholder')}
				bind:value={openingBalance}
				aria-label={m('cfo.control.openingBalance')}
			/>
		</label>
		<label class="control">
			<span class="control-label">{m('cfo.control.minBalance')}</span>
			<input
				class="cf-input"
				type="text"
				inputmode="decimal"
				placeholder={m('cfo.control.minBalancePlaceholder')}
				bind:value={threshold}
				aria-label={m('cfo.control.minBalance')}
			/>
		</label>
	</div>

	<!-- KPI row — rendered on EVERY state, never gated on the response.
	     `docs/decisions.md` §125: an absent figure is a dash, and whether it is
	     absent because it is still arriving is ANNOUNCED, not drawn. This row
	     used to sit inside `{:else if forecast}` and collapse to nothing while
	     the three analytics requests were in flight — which satisfied §34 (no
	     figure nobody computed was shown) but by a DIFFERENT mechanism from
	     `/discounts` and `/bank-reconciliation`, so the one convention had two
	     shapes and a reader could not learn it once.

	     `pending` is only consulted when the value is MISSING, so a re-fetch
	     (granularity, horizon, an applied opening balance) keeps the figures
	     already on screen rather than blanking a row the CFO is reading; the
	     `.loading` line below says a newer answer is on its way. A FAILED load
	     leaves them absent-but-not-loading — an `unavailable` row — and the
	     error banner below it says why, which is why the banner moved under the
	     row rather than replacing it. -->
	<div class="kpi-row" data-testid="forecast-kpi-row">
		<KpiCard
			value={forecast ? fmt(forecast.totals.scheduled_amount) : null}
			label={m('cfo.kpi.projectedOutflow')}
			pending={loading}
		/>
		<KpiCard
			value={forecast ? fmt(forecast.totals.committed_amount) : null}
			label={m('cfo.kpi.committed')}
			pending={loading}
		/>
		<KpiCard
			value={forecast ? fmt(forecast.totals.pending_amount) : null}
			label={m('cfo.kpi.pipeline')}
			pending={loading}
		/>
		<!-- The three cards above read the forecast; this one reads the SEPARATE
		     what-if response. `?? '0'` was actively defeating the honest answer
		     — `fmt` is `formatMoney`, which already returns the no-figure dash
		     for a missing amount, and the coercion turned it into a green "$0
		     available if you pay early", a figure nobody computed on the one
		     card a CFO acts on (`docs/decisions.md` §34).

		     All three requests still commit together out of one `Promise.all`,
		     so `whatif` is never null while `forecast` is set. It is a trap
		     rather than a live bug — splitting that `Promise.all` to let the
		     forecast paint before the slower what-if is an obvious future
		     change, and it would have silently armed the zero. `null` hands the
		     case to `KpiCard`, which also drops the green tint when there is no
		     figure to call good news. -->
		<KpiCard
			value={whatif ? fmt(whatif.scenarios.early.total_discount_captured) : null}
			label={m('cfo.kpi.discountIfEarly')}
			highlight="green"
			pending={loading}
		/>
	</div>

	{#if error}
		<p class="cf-error" role="alert">{error}</p>
	{:else if loading}
		<p class="loading">{m('cfo.loading')}</p>
	{:else if forecast}

		<!-- Forecast bar chart -->
		<div class="chart-card">
			<h2>{m('cfo.chart.projectedOutflows', { granularity: granLabel(granularity) })}</h2>
			{#if forecast.periods.length > 0}
				<div class="cf-bars">
					{#each forecast.periods as p (p.period)}
						<div class="cf-bar-row">
							<span class="cf-bar-label">{formatPeriod(p.period)}</span>
							<div
								class="cf-bar-bg"
								role="img"
								aria-label={m('cfo.chart.barAria', { committed: fmt(p.committed_amount), pending: fmt(p.pending_amount) })}
							>
								<div
									class="cf-bar committed"
									style="width:{(parseMoneyForLayout(p.committed_amount) / maxScheduled) * 100}%"
									title={m('cfo.chart.committedTitle', { amount: fmt(p.committed_amount) })}
								></div>
								<div
									class="cf-bar pending"
									style="width:{(parseMoneyForLayout(p.pending_amount) / maxScheduled) * 100}%"
									title={m('cfo.chart.pendingTitle', { amount: fmt(p.pending_amount) })}
								></div>
							</div>
							<span class="cf-bar-amount">{fmt(p.scheduled_amount)}</span>
						</div>
					{/each}
				</div>
				<div class="cf-legend">
					<span class="cf-dot committed"></span> {m('cfo.legend.committed')}
					<span class="cf-dot pending"></span> {m('cfo.legend.pending')}
				</div>
			{:else}
				<p class="empty">{m('cfo.empty.outflows')}</p>
			{/if}
		</div>

		<!-- What-if scenarios -->
		{#if whatif}
			<div class="chart-card">
				<h2>{m('cfo.whatif.title')}</h2>
				<div class="scenario-grid">
					{#each [['early', m('cfo.whatif.early')], ['on_time', m('cfo.whatif.onTime')], ['late', m('cfo.whatif.late')]] as [key, label] (key)}
						{@const s = whatif.scenarios[key as 'early' | 'on_time' | 'late']}
						<div class="scenario-card" class:best={key === 'early'}>
							<span class="scenario-title">{label}</span>
							<span class="scenario-outflow">{fmt(s.total_outflow)}</span>
							<span class="scenario-sub">{m('cfo.whatif.netOutflow')}</span>
							{#if isPositiveAmount(s.total_discount_captured)}
								<span class="scenario-discount">{m('cfo.whatif.discountCaptured', { amount: fmt(s.total_discount_captured) })}</span>
							{/if}
							<span class="scenario-days">{m('cfo.whatif.daysToPay', { days: s.weighted_avg_pay_date_days })}</span>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<!-- Cash position -->
		{#if position}
			<div class="chart-card">
				<h2>{m('cfo.position.title')}</h2>
				<!-- "We have a bank balance and declined to use it" and "no bank is
				     connected" are different facts with different remedies, and the
				     page used to render both as the same enter-an-opening-balance
				     hint. The API already carries the reason; this is display only. -->
				{#if openingSkipReasonKey}
					<p class="cf-skipped" role="alert" data-testid="opening-balance-skipped">
						{m(openingSkipReasonKey, { currency: position.opening_balance_currency })}
					</p>
				{/if}
				<!-- The OUTFLOW half of the same currency guard. A foreign invoice
				     with no rate lock is subtracted at face value (dropping it would
				     understate the outflow), so a non-zero count means every closing
				     balance below mixes currencies — and the balance carries forward,
				     so one such row poisons the tail. -->
				{#if position.unconverted_count > 0}
					<p class="cf-skipped" role="alert" data-testid="unconverted-outflows">
						{m('cfo.position.unconvertedOutflows', {
							n: position.unconverted_count,
							currency: position.opening_balance_currency
						})}
					</p>
				{/if}
				{#if position.opening_balance_source === 'none'}
					<p class="cf-hint">{m('cfo.position.enterOpening')}</p>
				{/if}
				{#if position.breaches.length > 0}
					<p class="cf-breach" role="alert">
						{m('cfo.position.breach', { n: position.breaches.length })}
					</p>
				{/if}
				<!-- A data table legitimately scrolls inside its own container when it
				     cannot reflow (WCAG 1.4.10); without this wrapper the table pushed
				     the whole document sideways at 320px. Same role `.grid-container`
				     plays for the shared <DataTable>. -->
				<div class="cf-table-scroll" tabindex="0">
					<table class="cf-table">
						<thead>
							<tr>
								<th>{m('cfo.position.colPeriod')}</th>
								<th class="num">{m('cfo.position.colOpening')}</th>
								<th class="num">{m('cfo.position.colOutflow')}</th>
								<th class="num">{m('cfo.position.colClosing')}</th>
							</tr>
						</thead>
						<tbody>
							{#each position.periods as p (p.period)}
								{@const ccy = position.opening_balance_currency}
								<tr class:breach={p.below_threshold} data-breach={p.below_threshold}>
									<td>{formatPeriod(p.period)}</td>
									<td class="num">{fmtIn(p.opening, ccy)}</td>
									<td class="num">-{fmtIn(p.outflow, ccy)}</td>
									<td class="num closing">{fmtIn(p.closing, ccy)}</td>
								</tr>
							{/each}
							{#if position.periods.length === 0}
								<tr><td colspan="4" class="empty">{m('cfo.position.empty')}</td></tr>
							{/if}
						</tbody>
					</table>
				</div>
			</div>
		{/if}

		<!-- CFO metrics: DPO, cash conversion cycle, accruals, supplier
		     concentration, fraud-rate trend, rebate yield -->
		<CfoMetrics periodDays={horizonDays} />

		<!-- Consolidated reporting across entities (self-hides for single-entity tenants) -->
		<ByEntityBreakdown />
	{/if}

	<!-- Org-wide budget vs actual. Deliberately OUTSIDE the forecast `{#if}`
	     above, like the scheduled-reports panel below: it is an independent
	     fetch taking none of this page's controls, and a failed cash-flow load
	     must not hide the only consolidated allocated-vs-spent view.

	     Figures are grouped BY CURRENCY and never added across one — the
	     backend refuses to convert on a read, so a blended row would be
	     denominated in nothing real. Anything the spend legs had to leave out
	     is disclosed above the table, in the same `.cf-skipped` idiom the
	     cash-position card uses for its unconverted outflows. -->
	{#if budgetRollupError}
		<div class="chart-card" data-testid="budget-rollup">
			<h2>{m('cfo.budgets.title')}</h2>
			<p class="cf-error" role="alert">{m('cfo.budgets.loadFailed')}</p>
		</div>
	{:else if budgetRollup}
		<div class="chart-card" data-testid="budget-rollup">
			<h2>{m('cfo.budgets.title')}</h2>
			<!-- The disclosure sits ABOVE the numbers it qualifies, not in a
			     tooltip: a requisition / PO / invoice denominated in another
			     currency than its budget is REFUSED by the spend legs (they
			     never convert), so a non-zero count means committed and actual
			     below are a floor, not the whole picture. -->
			{#if budgetRollup.excluded_row_count > 0}
				<p class="cf-skipped" role="alert" data-testid="budget-rollup-excluded">
					{m('cfo.budgets.excluded', { n: budgetRollup.excluded_row_count })}
				</p>
			{/if}
			{#if budgetRollup.insufficient_data}
				<p class="empty">{m('cfo.budgets.empty')}</p>
			{:else}
				{#if budgetsOverCount > 0}
					<p class="cf-breach" role="alert" data-testid="budget-rollup-over">
						{m('cfo.budgets.overBudget', { n: budgetsOverCount })}
					</p>
				{/if}
				<!-- A data table legitimately scrolls inside its own container when it
				     cannot reflow (WCAG 1.4.10); without this wrapper the table pushed
				     the whole document sideways at 320px. Same role `.grid-container`
				     plays for the shared <DataTable>. -->
				<div class="cf-table-scroll" tabindex="0">
					<table class="cf-table">
						<thead>
							<tr>
								<th>{m('cfo.budgets.colCurrency')}</th>
								<th class="num">{m('cfo.budgets.colAllocated')}</th>
								<th class="num">{m('cfo.budgets.colCommitted')}</th>
								<th class="num">{m('cfo.budgets.colActual')}</th>
								<th class="num">{m('cfo.budgets.colRemaining')}</th>
								<th class="num">{m('cfo.budgets.colUtilization')}</th>
							</tr>
						</thead>
						<tbody>
							{#each budgetRollup.by_currency as row (row.currency)}
								{@const utilization = formatUtilization(row.utilization_pct)}
								<tr>
									<td>
										{row.currency}
										<span class="cf-row-sub"
											>{m('cfo.budgets.budgetCount', { n: row.budget_count })}</span
										>
									</td>
									<td class="num">{fmtIn(row.allocated, row.currency)}</td>
									<td class="num">{fmtIn(row.committed, row.currency)}</td>
									<td class="num">{fmtIn(row.actual, row.currency)}</td>
									<!-- `remaining` is the BACKEND's subtraction, in Decimal; the
									     predicate only decides whether to tint it. -->
									<td class="num" class:over={isNegativeAmount(row.remaining)}
										>{fmtIn(row.remaining, row.currency)}</td
									>
									<td class="num">{utilization ?? m('cfo.budgets.noUtilization')}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>
	{/if}

	<!-- Forecast vs actual. Like the budget rollup above and the scheduled
	     reports below, it sits OUTSIDE the forecast `{#if}`: it takes none of
	     this page's controls, issues its own request (a POST carrying the
	     CFO's typed forecast), and a failed cash-flow load must not hide the
	     only surface that renders `forecast_variance`'s partial-figure
	     disclosure. Role-gated inside the panel to admin + cfo, matching the
	     endpoint's own `_CFO_ROLES`. -->
	<ForecastVariancePanel />

	<!-- Scheduled reports — the CRUD surface for the report runner that emails
	     these same analytics. Deliberately OUTSIDE the forecast `{#if}` above: it
	     is an independent fetch, and a failed cash-flow load must not take the
	     only way to see (or re-enable) a broken schedule down with it. Read is
	     admin + cfo — exactly this route's own gate; mutation is admin-only and
	     gated inside the panel. No entity selector: `X-Entity-ID` is not honoured
	     by the scheduled-report routes, a schedule being whole-tenant. -->
	<ScheduledReportsPanel />
</PageHeader>

<style>
	.cf-controls {
		display: flex;
		flex-wrap: wrap;
		gap: 18px;
		align-items: flex-end;
	}
	.control {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.control-label {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}
	.seg {
		display: inline-flex;
		border: 1px solid var(--border);
		border-radius: 6px;
		overflow: hidden;
	}
	.seg-btn {
		padding: 7px 14px;
		border: none;
		background: var(--surface);
		color: var(--text-muted);
		cursor: pointer;
		font-family: inherit;
		font-size: 0.85rem;
		text-transform: capitalize;
	}
	.seg-btn.active {
		background: var(--accent-strong);
		color: #fff;
	}
	.cf-input {
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.9rem;
		width: 180px;
	}
	.cf-error {
		color: var(--danger);
		font-weight: 500;
	}
	.chart-card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 20px;
	}
	.chart-card h2 {
		font-size: 1rem;
		margin: 0 0 16px;
	}
	.cf-bars {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.cf-bar-row {
		display: grid;
		grid-template-columns: 90px 1fr 110px;
		align-items: center;
		gap: 12px;
	}
	.cf-bar-label {
		font-size: 0.8rem;
		color: var(--text-muted);
	}
	.cf-bar-bg {
		display: flex;
		height: 18px;
		background: rgba(128, 128, 128, 0.12);
		border-radius: 4px;
		overflow: hidden;
	}
	.cf-bar {
		height: 100%;
	}
	.cf-bar.committed {
		background: #638cff;
	}
	.cf-bar.pending {
		background: #b7c5f5;
	}
	.cf-bar-amount {
		font-size: 0.82rem;
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.cf-legend {
		margin-top: 14px;
		font-size: 0.78rem;
		color: var(--text-muted);
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.cf-dot {
		display: inline-block;
		width: 10px;
		height: 10px;
		border-radius: 2px;
		margin-left: 12px;
	}
	.cf-dot.committed {
		background: #638cff;
	}
	.cf-dot.pending {
		background: #b7c5f5;
	}
	.scenario-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
		gap: 14px;
	}
	.scenario-card {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 16px;
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.scenario-card.best {
		border-color: #2faa6a;
		background: rgba(47, 170, 106, 0.06);
	}
	.scenario-title {
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--text-muted);
	}
	.scenario-outflow {
		font-size: 1.4rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
	}
	.scenario-sub {
		font-size: 0.72rem;
		color: var(--text-muted);
	}
	.scenario-discount {
		font-size: 0.82rem;
		color: #2faa6a;
		font-weight: 600;
	}
	.scenario-days {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin-top: 4px;
	}
	.cf-hint,
	.cf-breach,
	.cf-skipped {
		font-size: 0.85rem;
		margin: 0 0 12px;
	}
	/* Amber, not red: the projection is still usable — it just isn't seeded from
	   the bank. Distinct from `.cf-hint` so "we declined your balance" can't be
	   read as the ordinary "enter an opening balance" prompt. */
	.cf-skipped {
		color: #d4940a;
		font-weight: 600;
	}
	.cf-breach {
		color: var(--danger);
		font-weight: 600;
	}
	/* Secondary detail inside a table cell (the per-currency budget count) —
	   muted on `--surface`, which clears 4.5:1 (see frontend/CLAUDE.md §
	   Colour tokens). */
	.cf-row-sub {
		color: var(--text-muted);
		font-size: 0.78rem;
		margin-left: 6px;
	}
	/* An overspent allocation. `--danger` is the on-a-dark-surface text token;
	   never its `-strong` companion, which is a FILL. */
	.cf-table td.num.over {
		color: var(--danger);
		font-weight: 600;
	}
	/* The table's own scroll container. `.chart-card` is a plain block with
	   `overflow: visible`, so a four/six-column money table wider than the card
	   used to push the DOCUMENT sideways rather than scroll inside the card —
	   WCAG 1.4.10 Reflow. A table that cannot reflow is allowed to scroll in
	   one direction; the page is not. */
	.cf-table-scroll {
		overflow-x: auto;
	}
	.cf-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.88rem;
	}
	.cf-table th,
	.cf-table td {
		padding: 8px 12px;
		text-align: left;
		border-bottom: 1px solid var(--border);
	}
	.cf-table th.num,
	.cf-table td.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.cf-table td.closing {
		font-weight: 600;
	}
	.cf-table tr.breach td {
		color: var(--danger);
	}
	.empty {
		color: var(--text-muted);
		text-align: center;
		padding: 20px;
	}
</style>
