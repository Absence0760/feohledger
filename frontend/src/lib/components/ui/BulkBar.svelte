<script lang="ts">
	import type { Snippet } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		count,
		onclear,
		countLabel,
		lead,
		actions
	}: {
		count: number;
		onclear: () => void;
		/** The count text, when the page words it its own way (a catalogue
		 *  whose noun takes a gender, as `invoices.bulk.selected` does).
		 *  Defaults to `common.nSelected`. */
		countLabel?: string;
		/** Rendered between the count and Clear: the "select all N matching"
		 *  affordance, or the note that says it is in force. */
		lead?: Snippet;
		/**
		 * Render the per-page action buttons (BulkDeleteButton, `.btn-secondary`
		 * actions). Place them after the divider; the component handles the
		 * Clear button + count display itself.
		 */
		actions: Snippet;
	} = $props();

	// The bar is fixed over the bottom of the viewport, so while it shows the
	// page reserves its height (plus its 24px offset and a gap) below the
	// content: the last rows and the pagination scroll clear of it instead
	// of sitting under it. Measured, because it wraps to more rows on a
	// narrow screen. One bar per page, so one root class is enough.
	let barHeight = $state(0);
	$effect(() => {
		if (count <= 0 || barHeight <= 0) return;
		const root = document.documentElement;
		root.style.setProperty('--bulk-bar-reserve', `${barHeight + 40}px`);
		root.classList.add('has-bulk-bar');
		return () => {
			root.classList.remove('has-bulk-bar');
			root.style.removeProperty('--bulk-bar-reserve');
		};
	});
</script>

{#if count > 0}
	<div
		class="bulk-bar"
		role="toolbar"
		aria-label={m('common.bulkActions')}
		bind:offsetHeight={barHeight}
	>
		<span class="bulk-count">{countLabel ?? m('common.nSelected', { n: count })}</span>
		{@render lead?.()}
		<button class="btn-cancel bulk-clear" type="button" onclick={onclear}>{m('common.clear')}</button>
		<div class="bulk-divider"></div>
		{@render actions()}
	</div>
{/if}

<style>
	.bulk-bar {
		position: fixed;
		left: 50%;
		bottom: 24px;
		transform: translateX(-50%);
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 16px;
		background: var(--surface);
		border: 1px solid var(--accent);
		border-radius: 8px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
		flex-wrap: wrap;
		z-index: 50;
		max-width: calc(100vw - 48px);
		box-sizing: border-box;
	}

	.bulk-count {
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--accent);
		white-space: nowrap;
	}

	.bulk-divider {
		width: 1px;
		height: 20px;
		background: var(--border);
	}
</style>
