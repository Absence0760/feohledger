<script lang="ts">
	import { api } from '$lib/api';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import { formatMoney, isNegativeAmount } from '$lib/utils/money';
	import type { MoneyAmount } from '$lib/utils/money';
	import { formatPeriod } from '$lib/utils/time';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import type { CfoAnalytics } from '$lib/types/analytics';

	// DPO, cash conversion cycle, accruals, supplier concentration, fraud-rate
	// trend, and rebate yield — `GET /api/analytics/cfo` computes all of these
	// correctly (Decimal throughout on the backend) but had no frontend
	// surface at all until this component. Found by exploratory persona-driven
	// testing (CFO persona). Filed as #236.

	interface Props {
		/** Trailing window in days (matches the rest of the CFO surface). */
		periodDays?: number;
	}

	let { periodDays = 365 }: Props = $props();

	let data = $state<CfoAnalytics | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);

	/** Format a figure the API has already expressed in the REPORTING currency. */
	function fmt(amount: MoneyAmount): string {
		return formatMoney(amount, { currency: orgCurrency.currency, whole: true });
	}

	/** Format a figure denominated in its OWN currency — the unrealized-FX
	 *  table's open-exposure column is the one such figure on this component,
	 *  and `fmt` would mislabel it with the reporting code. */
	function fmtIn(amount: MoneyAmount, currency: string): string {
		return formatMoney(amount, { currency, whole: true });
	}

	let maxDpo = $derived(Math.max(1, ...(data?.dpo_trend ?? []).map((r) => r.dpo)));
	// `rate_pct` is null for a month with no invoices — the rate is not
	// computable, not zero. Those months contribute nothing to the scale (and
	// draw no bar) rather than being coerced to 0.
	let maxFraudRate = $derived(
		Math.max(
			1,
			...(data?.fraud_rate_trend ?? [])
				.map((r) => r.rate_pct)
				.filter((v): v is number => v !== null)
		)
	);

	// Sequences `load` (latest-issued wins). `periodDays` comes from the 30/90/
	// 180/365 button group on `/cfo`, so two clicks in quick succession left two
	// requests in flight with nothing deciding which one may write: the DPO,
	// cash-conversion-cycle and fraud-rate tables could settle on the 30-day
	// figures under an active "365 days" button.
	// See `frontend/CLAUDE.md` § Sequencing list fetches.
	const loadSequence = createRequestSequencer();

	$effect(() => {
		void periodDays;
		orgCurrency.ensureLoaded();
		load();
	});

	async function load() {
		const token = loadSequence.start();
		loading = true;
		error = null;
		try {
			const res = await api.get<CfoAnalytics>(`/api/analytics/cfo?period_days=${periodDays}`);
			if (!loadSequence.canCommit(token)) return;
			data = res;
		} catch (e) {
			if (loadSequence.isCurrentRequest(token)) {
				error = e instanceof Error ? e.message : m('cfoMetrics.loadFailed');
			}
		} finally {
			// `isCurrentRequest`, never `canCommit` — a superseded response must
			// not clear the spinner the newest request owns.
			if (loadSequence.isCurrentRequest(token)) loading = false;
		}
	}
</script>

<div class="chart-card" data-testid="cfo-metrics-section">
	<h2>{m('cfoMetrics.heading')}</h2>
	<!-- KPI row — rendered on EVERY state, never gated on the response
	     (`docs/decisions.md` §125). It used to sit inside `{:else if data}` and
	     collapse while the metrics request was in flight; the horizon control
	     (30/90/180/365) re-fires that request, so the row also blanked on every
	     horizon change instead of holding the figures until the newer answer
	     landed. `pending` is only consulted for a MISSING value, which is what
	     fixes both.

	     The cash-conversion-cycle card already had two reasons for a dash — the
	     backend reports `null` when the cycle is not computable — so it now has
	     three, and only the `sub` line distinguishes them: `KpiCard` deliberately
	     draws pending and unavailable identically. -->
	<div class="kpi-row">
		<KpiCard
			value={data ? `${data.dpo_current.toFixed(1)}d` : null}
			label={m('cfoMetrics.kpi.dpo')}
			pending={loading}
		/>
		<!-- `null`, not a hand-written dash: `KpiCard` owns the glyph, so there is
		     one spelling of "no figure" and `kpiValue.test.ts` pins it. Passing the
		     dash as a value also rendered `data-kpi-state="value"`, which is a claim
		     that a figure exists. -->
		<KpiCard
			value={data && data.cash_conversion_cycle !== null
				? `${data.cash_conversion_cycle.toFixed(1)}d`
				: null}
			label={m('cfoMetrics.kpi.ccc')}
			sub={data && data.cash_conversion_cycle === null
				? m('cfoMetrics.kpi.cccUnavailable')
				: null}
			pending={loading}
		/>
		<KpiCard
			value={data
				? fmtIn(
						data.reporting_accounts_payable_balance.total_amount,
						data.reporting_accounts_payable_balance.reporting_currency
					)
				: null}
			label={m('cfoMetrics.kpi.apBalance')}
			pending={loading}
		/>
		<KpiCard
			value={data ? `${data.rebate_yield.yield_pct.toFixed(2)}%` : null}
			label={m('cfoMetrics.kpi.rebateYield')}
			highlight={data && data.rebate_yield.yield_pct > 0 ? 'green' : null}
			pending={loading}
		/>
	</div>

	{#if error}
		<p class="cfm-error" role="alert">{error}</p>
	{:else if loading && !data}
		<p class="empty">{m('cfoMetrics.loading')}</p>
	{:else if data}
		{#if data.reporting_accounts_payable_balance.unconverted_count > 0}
			<p class="cfm-skipped" role="alert" data-testid="unconverted-ap-balance">
				{m('cfoMetrics.apBalance.unconverted', {
					n: data.reporting_accounts_payable_balance.unconverted_count,
					currency: data.reporting_accounts_payable_balance.reporting_currency
				})}
			</p>
		{/if}

		{#if data.dpo_trend.length > 0}
			<div class="cfm-subsection">
				<h3>{m('cfoMetrics.dpoTrend.title')}</h3>
				<p class="cfm-trend-hint">{m('cfoMetrics.dpoTrend.hint')}</p>
				<div class="cf-bars">
					{#each data.dpo_trend as r (r.month)}
						<div class="cf-bar-row">
							<span class="cf-bar-label">{formatPeriod(r.month)}</span>
							<div class="cf-bar-bg" role="img" aria-label={`${r.dpo.toFixed(1)} days`}>
								<div class="cf-bar committed" style="width:{(r.dpo / maxDpo) * 100}%"></div>
							</div>
							<span class="cf-bar-amount">{r.dpo.toFixed(1)}d</span>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<div class="cfm-subsection">
			<h3>{m('cfoMetrics.accruals.title')}</h3>
			<div class="cfm-stat-grid">
				<div class="cfm-stat">
					<span class="cfm-stat-label">{m('cfoMetrics.accruals.openPo')}</span>
					<span class="cfm-stat-value">{fmt(data.accruals.open_po_amount)}</span>
				</div>
				<div class="cfm-stat">
					<span class="cfm-stat-label">{m('cfoMetrics.accruals.received')}</span>
					<span class="cfm-stat-value">{fmt(data.accruals.received_amount)}</span>
				</div>
				<div class="cfm-stat">
					<span class="cfm-stat-label">{m('cfoMetrics.accruals.unposted')}</span>
					<span class="cfm-stat-value">{fmt(data.accruals.unposted_invoice_amount)}</span>
				</div>
				<div class="cfm-stat cfm-stat-total">
					<span class="cfm-stat-label">{m('cfoMetrics.accruals.total')}</span>
					<span class="cfm-stat-value">{fmt(data.accruals.total_accrual)}</span>
				</div>
			</div>
		</div>

		<div class="cfm-subsection">
			<h3>{m('cfoMetrics.concentration.title')}</h3>
			{#if data.supplier_concentration.flagged}
				<p class="cfm-flag" role="alert">
					{m('cfoMetrics.concentration.flagged', {
						vendor: data.supplier_concentration.largest_vendor ?? '—',
						pct: data.supplier_concentration.largest_vendor_share_pct.toFixed(1)
					})}
				</p>
			{/if}
			<div class="cfm-stat-grid">
				<div class="cfm-stat">
					<span class="cfm-stat-label">{m('cfoMetrics.concentration.top10')}</span>
					<span class="cfm-stat-value">{data.supplier_concentration.top_10_share_pct.toFixed(1)}%</span>
				</div>
				<div class="cfm-stat">
					<span class="cfm-stat-label">{m('cfoMetrics.concentration.top50')}</span>
					<span class="cfm-stat-value">{data.supplier_concentration.top_50_share_pct.toFixed(1)}%</span>
				</div>
				<div class="cfm-stat">
					<span class="cfm-stat-label">{m('cfoMetrics.concentration.largest')}</span>
					<span class="cfm-stat-value">{data.supplier_concentration.largest_vendor ?? '—'}</span>
				</div>
			</div>
			<!-- Every share above — and the `flagged` risk warning — divides by
			     `total_spend`, which counts an invoice with no locked rate at its
			     FACE value. Same role="alert" skipped-rows treatment as the AP
			     balance above, but a DIFFERENT message: that one says "excluded ...
			     a floor" because its rows are left out, these are counted in. -->
			{#if data.supplier_concentration.unconverted_count > 0}
				<p class="cfm-skipped" role="alert" data-testid="unconverted-concentration">
					{m('cfoMetrics.concentration.unconverted', {
						n: data.supplier_concentration.unconverted_count,
						currency: data.reporting_spend.reporting_currency
					})}
				</p>
			{/if}
		</div>

		{#if data.fraud_rate_trend.length > 0}
			<div class="cfm-subsection">
				<h3>{m('cfoMetrics.fraudTrend.title')}</h3>
				<div class="cf-bars">
					{#each data.fraud_rate_trend as r (r.month)}
						<!-- A month that booked no invoices has NO exception rate. It used
						     to arrive as 0 and draw the flattest, most reassuring bar on
						     the chart — worst of all for the "no invoices but exceptions
						     raised anyway" month. Same "—" + reason treatment the cash
						     conversion cycle above already gets. -->
						{@const unknown = r.rate_pct === null}
						<div class="cf-bar-row">
							<span class="cf-bar-label">{formatPeriod(r.month)}</span>
							<div
								class="cf-bar-bg"
								role="img"
								aria-label={unknown
									? m('cfoMetrics.fraudTrend.noInvoices')
									: `${(r.rate_pct ?? 0).toFixed(1)}%`}
							>
								{#if !unknown}
									<div
										class="cf-bar pending"
										style="width:{((r.rate_pct ?? 0) / maxFraudRate) * 100}%"
									></div>
								{/if}
							</div>
							{#if unknown}
								<span class="cf-bar-amount cfm-unknown" data-testid="fraud-rate-unknown">—</span>
							{:else}
								<span class="cf-bar-amount">{(r.rate_pct ?? 0).toFixed(1)}%</span>
							{/if}
						</div>
					{/each}
				</div>
				{#if data.fraud_rate_trend.some((r) => r.insufficient_data)}
					<p class="cfm-note">{m('cfoMetrics.fraudTrend.noInvoices')}</p>
				{/if}
			</div>
		{/if}

		{#if data.unrealized_fx.available && data.unrealized_fx.by_currency.length > 0}
			<div class="cfm-subsection">
				<h3>{m('cfoMetrics.fx.title')}</h3>
				<!-- Open foreign invoices with no locked rate are EXCLUDED from the
				     exposure rather than booked at face value: the mark-to-market leg
				     converts the same original amount at today's rate, so a face-value
				     booking reports the conversion itself as a gain/loss. Excluding
				     them silently would understate the exposure instead, so the count
				     has to be on screen — the number and its caveat must not live in
				     different places (decisions §35). -->
				{#if data.unrealized_fx.unconverted_count > 0}
					<p class="cfm-skipped" role="alert" data-testid="unconverted-fx">
						{m('cfoMetrics.fx.unconverted', {
							n: data.unrealized_fx.unconverted_count,
							currency: data.unrealized_fx.reporting_currency
						})}
					</p>
				{/if}
				<DataTable
					columns={[
						{ label: m('cfoMetrics.fx.colCurrency') },
						{ label: m('cfoMetrics.fx.colOpen'), class: 'num' },
						{ label: m('cfoMetrics.fx.colBooked'), class: 'num' },
						{ label: m('cfoMetrics.fx.colCurrent'), class: 'num' },
						{ label: m('cfoMetrics.fx.colGainLoss'), class: 'num' }
					]}
				>
					{#snippet body()}
						{#each data?.unrealized_fx.by_currency ?? [] as e (e.currency)}
							<!-- The three reporting-currency columns are labelled from the
							     code THIS block names, not the `orgCurrency` store: the
							     store sees three of the backend's four rungs and answers
							     `null` where the server resolved from the fourth
							     (decisions §119, §200). -->
							{@const reportingCcy = data?.unrealized_fx.reporting_currency}
							<tr>
								<td>{e.currency}</td>
								<!-- The open exposure is in the row's OWN currency; only the
								     three columns after it are in the reporting currency
								     (see CfoUnrealizedFxByCurrency). A EUR 10,000 exposure
								     once read "$10,000" with its real code sitting in the
								     cell immediately to the left. -->
								<td class="num">{fmtIn(e.open_original_amount, e.currency)}</td>
								<td class="num"
									>{formatMoney(e.booked_reporting_amount, { currency: reportingCcy, whole: true })}</td
								>
								<td class="num"
									>{formatMoney(e.current_reporting_amount, { currency: reportingCcy, whole: true })}</td
								>
								<td class="num" class:cfm-alert={isNegativeAmount(e.unrealized_gain_loss)}>
									{formatMoney(e.unrealized_gain_loss, { currency: reportingCcy, whole: true })}
								</td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>
			</div>
		{/if}
	{/if}
</div>

<style>
	/* This component is mounted standalone (not via <slot>), so it needs its
	   own copy of the .chart-card / .cf-bar* / .empty recipe rather than
	   relying on the parent route's scoped <style> — Svelte scoped CSS never
	   reaches into a child component's own template. Kept in sync with
	   routes/cfo/+page.svelte's identical block by inspection. */
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
	.empty {
		color: var(--text-muted);
		text-align: center;
		padding: 20px;
	}

	.cfm-error {
		color: var(--danger);
		font-weight: 500;
	}

	.cfm-subsection {
		margin-top: 20px;
	}

	.cfm-subsection h3 {
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--text-muted);
		margin: 0 0 10px;
	}

	.cfm-trend-hint {
		font-size: 0.75rem;
		color: var(--text-muted);
		margin: -6px 0 10px;
	}

	.cfm-stat-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 12px;
	}

	.cfm-stat {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 12px 14px;
		border: 1px solid var(--border);
		border-radius: 8px;
	}

	.cfm-stat-total {
		border-color: var(--accent);
	}

	.cfm-stat-label {
		font-size: 0.72rem;
		color: var(--text-muted);
	}

	.cfm-stat-value {
		font-size: 1.1rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
	}

	.cfm-flag {
		color: var(--danger);
		font-weight: 600;
		font-size: 0.85rem;
		margin: 0 0 12px;
	}

	.cfm-alert {
		color: var(--danger);
	}
	/* Amber, not red — the exposure below is still usable, it just doesn't
	   cover every open foreign invoice. Same treatment as `/cfo`'s
	   `.cf-skipped`, which carries the sibling outflow-side caveat. */
	.cfm-skipped {
		color: #d4940a;
		font-size: 0.85rem;
		font-weight: 600;
		margin: 0 0 12px;
	}
	/* Muted, not amber: "this month has no rate" is a fact about the data,
	   not a warning about the figure beside it. */
	.cfm-note {
		color: var(--text-muted);
		font-size: 0.78rem;
		margin: 10px 0 0;
	}
	.cfm-unknown {
		color: var(--text-muted);
	}
</style>
