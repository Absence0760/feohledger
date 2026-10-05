<script lang="ts">
	import { m } from '#lib/i18n/store.svelte.ts';

	type Bar = {
		/** Row label (vendor name, period). */
		label: string;
		/** Numeric value driving the bar width — coerced from the Decimal string. */
		value: number;
		/** The currency-formatted amount shown at the row's right edge. */
		amountLabel: string;
		/** Optional secondary annotation (e.g. share % or invoice count). */
		sub?: string;
	};

	type Props = {
		bars: Bar[];
	};

	let { bars }: Props = $props();

	// Scale every bar against the largest value so the widest fills the track.
	// Guard a zero/negative max so a degenerate dataset can't divide-by-zero.
	let max = $derived(Math.max(1, ...bars.map((b) => (b.value > 0 ? b.value : 0))));
</script>

{#if bars.length === 0}
	<p class="chart-empty">{m('assistant.chart.noData')}</p>
{:else}
	<div class="chart-bars" role="list">
		{#each bars as bar (bar.label)}
			<div class="bar-row" role="listitem">
				<span class="bar-label" title={bar.label}>{bar.label}</span>
				<div class="bar-track">
					<div
						class="bar-fill"
						style="width:{Math.max(2, (Math.max(bar.value, 0) / max) * 100)}%"
						title={bar.amountLabel}
					></div>
				</div>
				<span class="bar-amount">
					{bar.amountLabel}
					{#if bar.sub}<span class="bar-sub">{bar.sub}</span>{/if}
				</span>
			</div>
		{/each}
	</div>
{/if}

<style>
	/* Mirrors the CFO dashboard's `.cf-bar*` recipe (frontend/src/routes/cfo)
	 * — kept local so this is a drop-in for any structured bar dataset. */
	.chart-bars {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.bar-row {
		display: grid;
		grid-template-columns: 130px 1fr 140px;
		align-items: center;
		gap: 12px;
	}
	.bar-label {
		font-size: 0.8rem;
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.bar-track {
		height: 18px;
		background: rgba(128, 128, 128, 0.12);
		border-radius: 4px;
		overflow: hidden;
	}
	.bar-fill {
		height: 100%;
		background: #638cff;
		border-radius: 4px;
		transition: width 0.25s ease;
	}
	.bar-amount {
		font-size: 0.82rem;
		text-align: right;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.bar-sub {
		display: block;
		font-size: 0.72rem;
		color: var(--text-muted);
	}
	.chart-empty {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 4px 0;
	}
</style>
