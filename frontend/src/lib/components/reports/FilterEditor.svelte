<script lang="ts">
	// Filter builder for the report. Entirely driven by the catalog source's
	// `filters`: each field advertises the operators the server accepts and,
	// for enum fields, the allowed values. The value input adapts to the field
	// type + operator (single scalar / two-value `between` / multi-value `in`).
	import type { CatalogFilter, FilterOp, SpecFilter } from '$lib/types/reports';
	import { OP_LABELS } from '$lib/types/reports';
	import RowAction from '$lib/components/ui/RowAction.svelte';

	interface Props {
		available: CatalogFilter[];
		selected: SpecFilter[];
	}
	let { available, selected = $bindable() }: Props = $props();

	function meta(key: string): CatalogFilter | undefined {
		return available.find((f) => f.key === key);
	}

	function isNumeric(key: string): boolean {
		const t = meta(key)?.type;
		return t === 'number' || t === 'money';
	}

	/** A sensible empty value for the given operator. */
	function emptyValue(op: FilterOp): SpecFilter['value'] {
		if (op === 'in') return [];
		if (op === 'between') return ['', ''];
		return '';
	}

	function add(key: string) {
		const f = meta(key);
		if (!f) return;
		const op = f.ops[0];
		if (!op) return;
		selected = [...selected, { key, op, value: emptyValue(op) }];
	}

	function remove(idx: number) {
		selected = selected.filter((_, i) => i !== idx);
	}

	function changeOp(filter: SpecFilter, op: FilterOp) {
		filter.op = op;
		filter.value = emptyValue(op);
	}

	// --- value read/write helpers (keep the union out of `bind:value`) ---

	function coerce(key: string, raw: string): string | number {
		if (raw === '') return '';
		return isNumeric(key) ? Number(raw) : raw;
	}

	function scalarValue(filter: SpecFilter): string {
		return filter.value == null || Array.isArray(filter.value) ? '' : String(filter.value);
	}

	function betweenAt(filter: SpecFilter, idx: number): string {
		return Array.isArray(filter.value) ? String(filter.value[idx] ?? '') : '';
	}
	function setBetween(filter: SpecFilter, idx: number, raw: string) {
		const pair = Array.isArray(filter.value) ? [...filter.value] : ['', ''];
		pair[idx] = coerce(filter.key, raw);
		filter.value = pair as (string | number)[];
	}

	function inValues(filter: SpecFilter): (string | number)[] {
		return Array.isArray(filter.value) ? filter.value : [];
	}
	function inText(filter: SpecFilter): string {
		return inValues(filter).join(', ');
	}
	function setInText(filter: SpecFilter, raw: string) {
		filter.value = raw
			.split(',')
			.map((s) => s.trim())
			.filter((s) => s.length > 0)
			.map((s) => coerce(filter.key, s));
	}
	function toggleEnum(filter: SpecFilter, v: string, on: boolean) {
		const cur = inValues(filter);
		filter.value = on ? [...cur, v] : cur.filter((x) => x !== v);
	}

	function inputType(key: string): 'text' | 'number' | 'date' {
		const t = meta(key)?.type;
		if (t === 'date') return 'date';
		if (t === 'number' || t === 'money') return 'number';
		return 'text';
	}
</script>

<div class="editor" data-testid="filter-editor">
	<div class="editor-head">
		<h3>Filters</h3>
		{#if available.length}
			<select
				aria-label="Add filter"
				value=""
				onchange={(e) => {
					add(e.currentTarget.value);
					e.currentTarget.value = '';
				}}
			>
				<option value="" disabled selected>+ Add filter…</option>
				{#each available as f (f.key)}
					<option value={f.key}>{f.label}</option>
				{/each}
			</select>
		{/if}
	</div>

	{#if selected.length === 0}
		<p class="hint">No filters — the report includes the whole set.</p>
	{:else}
		<ul class="rows">
			{#each selected as filter, idx (idx)}
				{@const field = meta(filter.key)}
				<li class="row">
					<span class="field-label">{field?.label ?? filter.key}</span>

					<select
						aria-label="Operator for {field?.label ?? filter.key}"
						value={filter.op}
						onchange={(e) => changeOp(filter, e.currentTarget.value as FilterOp)}
					>
						{#each field?.ops ?? [] as op (op)}
							<option value={op}>{OP_LABELS[op]}</option>
						{/each}
					</select>

					<span class="value-wrap">
						{#if filter.op === 'between'}
							<input
								type={inputType(filter.key)}
								aria-label="{field?.label} from"
								value={betweenAt(filter, 0)}
								oninput={(e) => setBetween(filter, 0, e.currentTarget.value)}
							/>
							<span class="and">and</span>
							<input
								type={inputType(filter.key)}
								aria-label="{field?.label} to"
								value={betweenAt(filter, 1)}
								oninput={(e) => setBetween(filter, 1, e.currentTarget.value)}
							/>
						{:else if filter.op === 'in'}
							{#if field?.enumValues?.length}
								<span class="enum-list">
									{#each field.enumValues as ev (ev)}
										<label class="enum-opt">
											<input
												type="checkbox"
												checked={inValues(filter).includes(ev)}
												onchange={(e) => toggleEnum(filter, ev, e.currentTarget.checked)}
											/>
											{ev}
										</label>
									{/each}
								</span>
							{:else}
								<input
									type="text"
									aria-label="{field?.label} values (comma-separated)"
									placeholder="value1, value2"
									value={inText(filter)}
									oninput={(e) => setInText(filter, e.currentTarget.value)}
								/>
							{/if}
						{:else if field?.enumValues?.length && (filter.op === 'eq' || filter.op === 'ne')}
							<select
								aria-label="Value for {field?.label}"
								value={scalarValue(filter)}
								onchange={(e) => (filter.value = e.currentTarget.value)}
							>
								<option value="">—</option>
								{#each field.enumValues as ev (ev)}
									<option value={ev}>{ev}</option>
								{/each}
							</select>
						{:else}
							<input
								type={inputType(filter.key)}
								aria-label="Value for {field?.label}"
								value={scalarValue(filter)}
								oninput={(e) => (filter.value = coerce(filter.key, e.currentTarget.value))}
							/>
						{/if}
					</span>

					<RowAction
						variant="danger"
						onclick={() => remove(idx)}
						ariaLabel="Remove filter {field?.label ?? filter.key}">Remove</RowAction
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
		gap: 10px;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
	}
	.field-label {
		font-weight: 500;
		min-width: 120px;
	}
	.value-wrap {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
		flex: 1;
	}
	.and {
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.enum-list {
		display: inline-flex;
		gap: 12px;
		flex-wrap: wrap;
	}
	.enum-opt {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 0.9rem;
	}
	select,
	input[type='text'],
	input[type='number'],
	input[type='date'] {
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
	input[type='text'] {
		min-width: 180px;
	}
</style>
