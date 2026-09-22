<script lang="ts">
	import Money from './Money.svelte';
	import type { CurrencyFigure } from './moneyByCurrency';

	// Several figures that must NOT be added together, one per currency, one
	// per line — the display half of a server rollup that groups by currency
	// (the `/cfo` accruals, by-entity's Open POs). Each figure is labelled by
	// its own code through `<Money>`, so the `null` entry (nobody recorded a
	// currency) renders bare rather than borrowing a neighbour's symbol
	// (`docs/decisions.md` §196, §197). Order is the caller's: the server's
	// rollups sort by code with the unknown entry last.

	interface Props {
		figures: CurrencyFigure[];
		/** Passed through to every `<Money>`. */
		whole?: boolean;
		mono?: boolean;
	}

	let { figures, whole = false, mono = false }: Props = $props();
</script>

{#if figures.length === 0}
	<!-- Nothing in any currency is a real zero — the rollup ran and found no
	     rows — so it renders as one, bare: there is no currency to name. -->
	<Money amount={0} currency={null} {whole} {mono} />
{:else}
	<span class="mbc">
		{#each figures as f (f.currency ?? '')}
			<span class="mbc-line" data-currency={f.currency ?? 'none'}>
				<Money amount={f.amount} currency={f.currency} {whole} {mono} />
			</span>
		{/each}
	</span>
{/if}

<style>
	/* One figure per line, right-aligned among themselves so the digits of
	   several currencies line up the way a column of figures does. */
	.mbc {
		display: inline-flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 2px;
	}
</style>
