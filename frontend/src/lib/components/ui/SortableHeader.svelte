<script lang="ts">
	/**
	 * A clickable `<th>` for a sortable list column. Renders inside a
	 * `DataTable`'s `header` snippet alongside the page's other (non-sortable)
	 * `<th>`s. A click sorts ascending on a column that isn't the active one,
	 * or flips the direction on the one that already is — the page owns the
	 * actual state + URL persistence (see `#lib/utils/sort.ts::toggleSort`)
	 * and passes it down as `active` / `order`.
	 *
	 * `aria-sort` on the `<th>` (not the button) is what a screen reader
	 * announces as the column's current sort state (WCAG 1.3.1 — the same
	 * reason `DataTable`'s plain headers carry `scope="col"`).
	 */
	let {
		field,
		label,
		active,
		order,
		onsort,
		class: className = ''
	}: {
		field: string;
		label: string;
		active: boolean;
		order: 'asc' | 'desc';
		onsort: (field: string) => void;
		class?: string;
	} = $props();
</script>

<th scope="col" class={className} aria-sort={active ? (order === 'asc' ? 'ascending' : 'descending') : 'none'}>
	<button type="button" class="sort-btn" class:active onclick={() => onsort(field)}>
		{label}
		<span class="sort-icon" aria-hidden="true">
			{#if active}
				{order === 'asc' ? '▲' : '▼'}
			{:else}
				<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M7 10l5-5 5 5M7 14l5 5 5-5"/></svg>
			{/if}
		</span>
	</button>
</th>

<style>
	.sort-btn {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		border: none;
		background: none;
		padding: 0;
		margin: 0;
		font: inherit;
		text-transform: inherit;
		letter-spacing: inherit;
		color: inherit;
		cursor: pointer;
	}
	.sort-btn:hover,
	.sort-btn.active {
		color: var(--accent);
	}
	.sort-btn:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
		border-radius: 2px;
	}
	.sort-icon {
		display: inline-flex;
		align-items: center;
		opacity: 0.7;
		font-size: 0.7em;
	}
	.sort-btn.active .sort-icon {
		opacity: 1;
	}
</style>
