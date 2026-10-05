<script lang="ts">
	import { formatMoney } from '#lib/utils/money.ts';
	import { formatPeriod } from '#lib/utils/time.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { CashPositionResult } from '#lib/types/cashFlow.ts';

	let { result }: { result: CashPositionResult } = $props();

	// Money is exact-string on the wire; coerce ONLY to drive chart geometry —
	// every displayed figure goes back through `formatMoney` on the string.
	function num(v: string | null | undefined): number {
		if (v === null || v === undefined || v === '') return 0;
		const n = Number(v);
		return Number.isFinite(n) ? n : 0;
	}

	function fmt(v: string | null | undefined): string {
		return formatMoney(v, { currency: result.currency, whole: true });
	}

	// One point per period, plotted at its CLOSING balance (the running position
	// carried into the next period).
	let points = $derived(
		result.periods.map((p) => ({
			period: p.period,
			label: formatPeriod(p.period),
			closing: num(p.closing),
			below: p.below_threshold,
			raw: p
		}))
	);

	let threshold = $derived(
		result.min_balance_threshold !== null ? num(result.min_balance_threshold) : null
	);
	let hasBreach = $derived(points.some((p) => p.below));
	let breachCount = $derived(points.filter((p) => p.below).length);

	// --- SVG geometry (viewBox space; the <svg> scales to its container) ---
	const W = 720;
	const H = 240;
	const PAD_L = 8;
	const PAD_R = 8;
	const PAD_T = 16;
	const PAD_B = 28;

	// Y domain spans every plotted value plus the opening balance, the threshold,
	// and zero — so a negative closing (overdraft) and the min-balance line are
	// both always in frame.
	let domain = $derived.by(() => {
		const vals = [num(result.opening_balance), 0, ...points.map((p) => p.closing)];
		if (threshold !== null) vals.push(threshold);
		let lo = Math.min(...vals);
		let hi = Math.max(...vals);
		if (lo === hi) {
			// Degenerate flat series — pad so the line sits mid-frame.
			lo -= 1;
			hi += 1;
		}
		const span = hi - lo;
		return { lo: lo - span * 0.08, hi: hi + span * 0.08 };
	});

	function x(i: number): number {
		const n = points.length;
		if (n <= 1) return PAD_L + (W - PAD_L - PAD_R) / 2;
		return PAD_L + (i / (n - 1)) * (W - PAD_L - PAD_R);
	}
	function y(v: number): number {
		const { lo, hi } = domain;
		const t = (v - lo) / (hi - lo);
		return PAD_T + (1 - t) * (H - PAD_T - PAD_B);
	}

	let linePath = $derived(points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(i)} ${y(p.closing)}`).join(' '));
	let areaPath = $derived.by(() => {
		if (points.length === 0) return '';
		const base = y(threshold ?? 0);
		const top = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(i)} ${y(p.closing)}`).join(' ');
		return `${top} L ${x(points.length - 1)} ${base} L ${x(0)} ${base} Z`;
	});

	let thresholdY = $derived(threshold !== null ? y(threshold) : null);

	// Accessible one-line summary of the trajectory + any shortfall.
	let summary = $derived(
		m('cashFlow.chart.aria', {
			start: fmt(result.opening_balance),
			end: points.length ? fmt(points[points.length - 1].raw.closing) : fmt(result.opening_balance),
			shortfall: result.first_shortfall_period
				? m('cashFlow.chart.ariaShortfall', { period: formatPeriod(result.first_shortfall_period) })
				: m('cashFlow.chart.ariaHealthy')
		})
	);
</script>

<figure class="cash-chart" data-testid="cash-position-chart">
	<figcaption class="chart-cap">
		<span class="chart-title">{m('cashFlow.chart.title')}</span>
		<span class="chart-meta">
			{m('cashFlow.chart.opening', {
				amount: fmt(result.opening_balance),
				source: m(`cashFlow.chart.source.${result.opening_balance_source}` as never)
			})}
		</span>
	</figcaption>

	{#if hasBreach}
		<p class="chart-breach" role="alert">
			{m('cashFlow.chart.breach', { n: breachCount })}
			{#if result.first_shortfall_period}
				<span class="breach-when">{m('cashFlow.chart.firstShortfall', { period: formatPeriod(result.first_shortfall_period) })}</span>
			{/if}
		</p>
	{/if}

	{#if points.length === 0}
		<p class="chart-empty">{m('cashFlow.chart.empty')}</p>
	{:else}
		<svg
			class="chart-svg"
			viewBox="0 0 {W} {H}"
			preserveAspectRatio="none"
			role="img"
			aria-label={summary}
		>
			<!-- Zero baseline -->
			<line class="axis-zero" x1={PAD_L} x2={W - PAD_R} y1={y(0)} y2={y(0)} />

			<!-- Minimum-balance threshold -->
			{#if thresholdY !== null}
				<line class="threshold-line" x1={PAD_L} x2={W - PAD_R} y1={thresholdY} y2={thresholdY} />
			{/if}

			<!-- Filled area + curve -->
			<path class="area" d={areaPath} />
			<path class="line" d={linePath} />

			<!-- Points — red when below the minimum-balance threshold. -->
			{#each points as p, i (p.period)}
				<circle
					class="pt"
					class:below={p.below}
					cx={x(i)}
					cy={y(p.closing)}
					r="4"
				>
					<title>{p.label}: {fmt(p.raw.closing)}</title>
				</circle>
			{/each}
		</svg>

		<!-- Period axis labels (kept in normal flow so they don't skew with the
		     non-uniform SVG scaling). -->
		<div class="chart-axis" aria-hidden="true">
			{#each points as p (p.period)}
				<span class="axis-label" class:below={p.below}>{p.label}</span>
			{/each}
		</div>

		<div class="chart-legend">
			<span class="legend-item"><span class="swatch line-swatch"></span>{m('cashFlow.chart.legend.closing')}</span>
			{#if threshold !== null}
				<span class="legend-item"><span class="swatch thr-swatch"></span>{m('cashFlow.chart.legend.threshold', { amount: fmt(result.min_balance_threshold) })}</span>
			{/if}
			{#if hasBreach}
				<span class="legend-item"><span class="swatch breach-swatch"></span>{m('cashFlow.chart.legend.breach')}</span>
			{/if}
		</div>
	{/if}
</figure>

<style>
	.cash-chart {
		margin: 10px 0 0;
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 14px 16px 12px;
		background: var(--surface);
	}
	.chart-cap {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
		flex-wrap: wrap;
		margin-bottom: 8px;
	}
	.chart-title {
		font-size: 0.82rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text);
	}
	.chart-meta {
		font-size: 0.76rem;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}
	.chart-breach {
		margin: 0 0 10px;
		padding: 8px 12px;
		border-radius: 8px;
		background: rgba(240, 70, 70, 0.1);
		border: 1px solid rgba(240, 70, 70, 0.3);
		color: var(--danger);
		font-size: 0.82rem;
	}
	.breach-when {
		color: var(--text-muted);
	}
	.chart-empty {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 4px 0;
	}
	.chart-svg {
		width: 100%;
		height: 240px;
		display: block;
		overflow: visible;
	}
	.axis-zero {
		stroke: var(--border);
		stroke-width: 1;
		vector-effect: non-scaling-stroke;
	}
	.threshold-line {
		stroke: #e0a040;
		stroke-width: 1.5;
		stroke-dasharray: 5 4;
		vector-effect: non-scaling-stroke;
	}
	.area {
		fill: rgba(99, 140, 255, 0.12);
	}
	.line {
		fill: none;
		stroke: #638cff;
		stroke-width: 2;
		vector-effect: non-scaling-stroke;
	}
	.pt {
		fill: #638cff;
		stroke: var(--surface);
		stroke-width: 1.5;
	}
	.pt.below {
		fill: #e04040;
	}
	.chart-axis {
		display: flex;
		justify-content: space-between;
		gap: 6px;
		margin-top: 4px;
	}
	.axis-label {
		font-size: 0.68rem;
		color: var(--text-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		flex: 1 1 0;
		text-align: center;
	}
	.axis-label.below {
		color: var(--danger);
		font-weight: 600;
	}
	.chart-legend {
		display: flex;
		flex-wrap: wrap;
		gap: 14px;
		margin-top: 10px;
		font-size: 0.74rem;
		color: var(--text-muted);
	}
	.legend-item {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.swatch {
		width: 14px;
		height: 3px;
		border-radius: 2px;
		display: inline-block;
	}
	.line-swatch {
		background: #638cff;
	}
	.thr-swatch {
		background: #e0a040;
	}
	.breach-swatch {
		background: #e04040;
		width: 8px;
		height: 8px;
		border-radius: 50%;
	}
</style>
