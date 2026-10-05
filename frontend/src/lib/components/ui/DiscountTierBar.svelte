<script lang="ts">
	import type { DiscountTier } from '#lib/types/discounts.ts';

	type Props = {
		/** Sliding-scale tiers (e.g. 2% by day 10, 1% by day 20). */
		tiers: DiscountTier[];
		/** Optionally highlight the accepted tier (matched on `days`). */
		acceptedDays?: number | null;
	};

	let { tiers, acceptedDays = null }: Props = $props();

	// Show soonest-deadline (usually richest) tiers first.
	let sorted = $derived([...tiers].sort((a, b) => a.days - b.days));
</script>

{#if sorted.length === 0}
	<span class="tier-none">—</span>
{:else}
	<div class="tier-bar">
		{#each sorted as tier (tier.days)}
			<span class="tier-chip" class:accepted={acceptedDays === tier.days} title="{tier.percent}% if paid within {tier.days} days">
				{tier.percent}% / {tier.days}d
			</span>
		{/each}
	</div>
{/if}

<style>
	.tier-bar {
		display: inline-flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	/* Not `<Badge>`: the accepted tier is a FILLED chip (`--success-strong`
	   behind white text), and the palette has no solid tone — `Badge` exposes
	   five tints, `neutral` and `erp`, none of which can say "this is the tier
	   the supplier actually took". Rendering the unaccepted tiers through the
	   primitive would leave that emphasis as a colour rule on a `variant` in
	   this component, which is exactly what `Badge` forbids, and §52's ring
	   trick doesn't transfer: the distinction here IS the fill. The chip also
	   carries `tabular-nums` so the `2% / 10d` figures line up down a column.
	   Only the colour literals are retired to the palette pair. */
	.tier-chip {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		border-radius: 10px;
		background: var(--success-tint);
		color: var(--success-on-tint);
		font-size: 0.74rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.tier-chip.accepted {
		background: var(--success-strong);
		color: #fff;
	}
	.tier-none {
		color: var(--text-muted);
	}
</style>
