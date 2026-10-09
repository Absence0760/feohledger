<script lang="ts">
	/**
	 * A procurement line's description with its SKU beneath it, when it has one
	 * (migration 0111, decisions §268). Shared by the PO detail, the receipt
	 * detail and the record-receipt form, so the three show item identity the
	 * same way — a second line rather than a column, which keeps each table
	 * inside 320px (WCAG 1.4.10).
	 */
	import { m } from '#lib/i18n/store.svelte.ts';

	let { description, sku }: { description: string | null; sku?: string | null } = $props();
</script>

<span class="line-description">{description ?? '—'}</span>
{#if sku}
	<span class="line-sku mono" data-testid="line-sku">{m('procurement.sku', { sku })}</span>
{/if}

<style>
	.line-sku {
		display: block;
		font-size: 0.75rem;
		color: var(--text-muted);
		overflow-wrap: anywhere;
	}
</style>
