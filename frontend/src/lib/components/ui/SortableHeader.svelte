<script lang="ts">
	import { m } from '#lib/i18n/store.svelte.ts';

	/**
	 * A clickable `<th>` for a sortable list column. Renders inside a
	 * `DataTable`'s `header` snippet alongside the page's other (non-sortable)
	 * `<th>`s. A click sorts ascending on a column that isn't the active one,
	 * or flips the direction on the one that already is — the page owns the
	 * actual state + URL persistence (see `#lib/utils/sort.ts::toggleSort`)
	 * and passes it down as `active` / `order`.
	 *
	 * The active column also shows a × (when the page passes `onclear`) that
	 * drops the sort and returns the list to its default order — without it, a
	 * sort could only ever be flipped, never removed.
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
		onclear,
		class: className = ''
	}: {
		field: string;
		label: string;
		active: boolean;
		order: 'asc' | 'desc';
		onsort: (field: string) => void;
		/** Clear the sort entirely (back to the default order). */
		onclear?: () => void;
		class?: string;
	} = $props();
</script>

<!-- `aria-label` pins the header's accessible name to the column label, so the
     × (a button inside the cell) is not read into every cell's header. -->
<th scope="col" class={className} aria-label={label} aria-sort={active ? (order === 'asc' ? 'ascending' : 'descending') : 'none'}>
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
	{#if active && onclear}
		<button
			type="button"
			class="sort-clear"
			aria-label={m('common.sort.clear', { column: label })}
			title={m('common.sort.clear', { column: label })}
			onclick={onclear}
		>
			<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
		</button>
	{/if}
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
	/* The active column's "clear sort" ×: a small, quiet target beside the
	   arrow that turns to the danger tone on hover. 24px is WCAG 2.5.8's
	   minimum target size. */
	.sort-clear {
		display: inline-grid;
		place-items: center;
		width: 24px;
		height: 24px;
		margin-left: 4px;
		vertical-align: middle;
		border: none;
		border-radius: 50%;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		cursor: pointer;
		padding: 0;
	}
	.sort-clear:hover {
		background: var(--danger-tint);
		color: var(--danger-on-tint);
	}
	.sort-clear:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
</style>
