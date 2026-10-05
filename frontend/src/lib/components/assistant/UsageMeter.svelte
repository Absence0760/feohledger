<script lang="ts">
	import type { UsageResponse } from '#lib/types/assistant.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let { usage }: { usage: UsageResponse | null } = $props();

	// Budget 0 = unlimited — show the running total but no progress bar.
	let unlimited = $derived(!usage || usage.budget <= 0);
	let pct = $derived(
		usage && usage.budget > 0 ? Math.min(100, (usage.total_tokens / usage.budget) * 100) : 0
	);
	let near = $derived(pct >= 80 && pct < 100);
	let over = $derived(pct >= 100);

	const nf = new Intl.NumberFormat();
	function fmt(n: number): string {
		return nf.format(n);
	}
</script>

{#if usage}
	<div class="usage-meter" data-testid="usage-meter" class:over class:near>
		<div class="usage-head">
			<span class="usage-label">{m('assistant.usage.label', { period: usage.period })}</span>
			<span class="usage-figs">
				{#if unlimited}
					{m('assistant.usage.tokens', { total: fmt(usage.total_tokens) })}
				{:else}
					{m('assistant.usage.tokensOf', { total: fmt(usage.total_tokens), budget: fmt(usage.budget) })}
				{/if}
			</span>
		</div>
		{#if !unlimited}
			<div class="usage-track" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin="0" aria-valuemax="100">
				<div class="usage-fill" style="width:{pct}%"></div>
			</div>
			{#if over}
				<span class="usage-note">{m('assistant.usage.budgetReached')}</span>
			{:else if near}
				<span class="usage-note">{m('assistant.usage.remaining', { remaining: fmt(usage.remaining) })}</span>
			{/if}
		{:else}
			<span class="usage-note muted">{m('assistant.usage.unlimited')}</span>
		{/if}
	</div>
{/if}

<style>
	.usage-meter {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.78rem;
	}
	.usage-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 8px;
	}
	.usage-label {
		font-weight: 600;
		color: var(--text-muted);
	}
	.usage-figs {
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
	}
	.usage-track {
		height: 6px;
		background: rgba(128, 128, 128, 0.15);
		border-radius: 3px;
		overflow: hidden;
	}
	.usage-fill {
		height: 100%;
		background: var(--accent);
		transition: width 0.3s ease;
	}
	.usage-meter.near .usage-fill {
		background: #d4940a;
	}
	.usage-meter.over .usage-fill {
		background: #e04040;
	}
	.usage-note {
		font-size: 0.72rem;
		color: #d4940a;
	}
	.usage-meter.over .usage-note {
		color: var(--danger);
	}
	.usage-note.muted {
		color: var(--text-muted);
	}
</style>
