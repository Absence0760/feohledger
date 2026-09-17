<script lang="ts">
	// Group-by dimension picker for the report builder. Entirely driven by the
	// catalog source's `dimensions` — no hardcoded field list. Date dimensions
	// expose a grain selector (day/month/quarter/year).
	import type { CatalogDimension, DateGrain, SpecDimension } from '$lib/types/reports';
	import { GRAIN_LABELS } from '$lib/types/reports';
	import RowAction from '$lib/components/ui/RowAction.svelte';

	interface Props {
		/** All group-by fields the chosen data source offers. */
		available: CatalogDimension[];
		/** The chosen dimensions (two-way bound). */
		selected: SpecDimension[];
	}
	let { available, selected = $bindable() }: Props = $props();

	const GRAINS: DateGrain[] = ['day', 'month', 'quarter', 'year'];

	const unused = $derived(available.filter((d) => !selected.some((s) => s.key === d.key)));

	function meta(key: string): CatalogDimension | undefined {
		return available.find((d) => d.key === key);
	}

	function add(key: string) {
		const d = meta(key);
		if (!d) return;
		selected = [...selected, { key: d.key, grain: d.type === 'date' ? 'month' : null }];
	}

	function remove(key: string) {
		selected = selected.filter((s) => s.key !== key);
	}
</script>

<div class="editor" data-testid="dimension-editor">
	<div class="editor-head">
		<h3>Group by</h3>
		{#if unused.length}
			<select
				aria-label="Add dimension"
				value=""
				onchange={(e) => {
					add(e.currentTarget.value);
					e.currentTarget.value = '';
				}}
			>
				<option value="" disabled selected>+ Add dimension…</option>
				{#each unused as d (d.key)}
					<option value={d.key}>{d.label}</option>
				{/each}
			</select>
		{/if}
	</div>

	{#if selected.length === 0}
		<p class="hint">No dimensions — the report aggregates across the whole set.</p>
	{:else}
		<ul class="rows">
			{#each selected as dim (dim.key)}
				<li class="row">
					<span class="field-label">{meta(dim.key)?.label ?? dim.key}</span>
					{#if meta(dim.key)?.type === 'date'}
						<select aria-label="Grain for {meta(dim.key)?.label}" bind:value={dim.grain}>
							{#each GRAINS as g (g)}
								<option value={g}>{GRAIN_LABELS[g]}</option>
							{/each}
						</select>
					{/if}
					<RowAction
						variant="danger"
						onclick={() => remove(dim.key)}
						ariaLabel="Remove dimension {meta(dim.key)?.label ?? dim.key}">Remove</RowAction
					>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.editor {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 14px 16px;
		background: var(--surface);
	}
	.editor-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		/* The heading and its <select> drop onto separate lines rather than
		   pushing the document sideways at a narrow viewport (WCAG 1.4.10). */
		flex-wrap: wrap;
		gap: 12px;
		margin-bottom: 10px;
	}
	h3 {
		margin: 0;
		font-size: 0.95rem;
	}
	.hint {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 0;
	}
	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.field-label {
		font-weight: 500;
		flex: 1;
	}
	select {
		/* A <select> sizes to its widest option and `min-width: auto` keeps it
		   there, which is how a long option label pushed the page sideways at
		   320px. */
		min-width: 0;
		max-width: 100%;
		padding: 6px 10px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		font-size: 0.9rem;
	}
</style>
