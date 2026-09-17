<script lang="ts">
	import type { Snippet } from 'svelte';
	import { m } from '$lib/i18n/store.svelte';

	let {
		count,
		onclear,
		actions
	}: {
		count: number;
		onclear: () => void;
		/**
		 * Render the per-page action buttons (BulkDeleteButton, BulkActionButton).
		 * Place them after the divider; the component handles the Clear button +
		 * count display itself.
		 */
		actions: Snippet;
	} = $props();
</script>

{#if count > 0}
	<div class="bulk-bar" role="toolbar" aria-label={m('common.bulkActions')}>
		<span class="bulk-count">{m('common.nSelected', { n: count })}</span>
		<button class="bulk-clear" type="button" onclick={onclear}>{m('common.clear')}</button>
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
	}

	.bulk-count {
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--accent);
		white-space: nowrap;
	}

	.bulk-clear {
		padding: 4px 10px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
	}

	.bulk-clear:hover {
		color: var(--text);
		background: var(--bg);
	}

	.bulk-divider {
		width: 1px;
		height: 20px;
		background: var(--border);
	}
</style>
