<script lang="ts">
	// Measure (metric + aggregation) picker for the report builder. Driven by the
	// catalog source's `measures` — each measure exposes the aggregations it
	// allows. The same field may be added under more than one aggregation.
	import type { AggFn, CatalogMeasure, SpecMeasure } from '#lib/types/reports.ts';
	import { AGG_LABEL_KEYS, fieldLabel, measureColumnKey } from '#lib/types/reports.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import RowAction from '#lib/components/ui/RowAction.svelte';

	interface Props {
		available: CatalogMeasure[];
		selected: SpecMeasure[];
	}
	let { available, selected = $bindable() }: Props = $props();

	function meta(key: string): CatalogMeasure | undefined {
		return available.find((x) => x.key === key);
	}

	function label(key: string): string {
		return fieldLabel(m, key, meta(key)?.label);
	}

	function add(key: string) {
		const field = meta(key);
		if (!field) return;
		// Default to the first allowed aggregation; skip if that exact
		// (field, agg) pair is already chosen.
		const agg = field.aggs[0];
		if (!agg || selected.some((s) => s.key === key && s.agg === agg)) return;
		selected = [...selected, { key, agg }];
	}

	function remove(row: SpecMeasure) {
		selected = selected.filter((s) => !(s.key === row.key && s.agg === row.agg));
	}

	function aggsFor(key: string): AggFn[] {
		return meta(key)?.aggs ?? [];
	}
</script>

<div class="editor" data-testid="measure-editor">
	<div class="editor-head">
		<h3>{m('reports.measures.title')}</h3>
		{#if available.length}
			<select
				aria-label={m('reports.measures.add')}
				value=""
				onchange={(e) => {
					add(e.currentTarget.value);
					e.currentTarget.value = '';
				}}
			>
				<option value="" disabled selected>{m('reports.measures.addOption')}</option>
				{#each available as field (field.key)}
					<option value={field.key}>{fieldLabel(m, field.key, field.label)}</option>
				{/each}
			</select>
		{/if}
	</div>

	{#if selected.length === 0}
		<p class="hint">{m('reports.measures.empty')}</p>
	{:else}
		<ul class="rows">
			{#each selected as row (measureColumnKey(row))}
				<li class="row">
					<span class="field-label">{label(row.key)}</span>
					<select aria-label={m('reports.measures.aggFor', { field: label(row.key) })} bind:value={row.agg}>
						{#each aggsFor(row.key) as agg (agg)}
							<option value={agg}>{m(AGG_LABEL_KEYS[agg])}</option>
						{/each}
					</select>
					<RowAction
						variant="danger"
						onclick={() => remove(row)}
						ariaLabel={m('reports.measures.removeAria', { field: label(row.key) })}
						>{m('reports.remove')}</RowAction
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
