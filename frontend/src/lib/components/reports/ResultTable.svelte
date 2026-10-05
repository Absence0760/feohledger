<script lang="ts">
	// Renders a `ReportResult` — dimension + measure columns straight from
	// `result.columns`, with money-typed measures rendered through <Money> (the
	// values are exact decimal strings, never re-computed here). Paginates over
	// `total_rows`.
	import { cellCurrency, type ReportResult } from '#lib/types/reports.ts';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Money from '#lib/components/ui/Money.svelte';

	interface Props {
		result: ReportResult;
		/** Fired when the user pages; the parent re-runs at the new page. */
		onpage?: (page: number) => void;
	}
	let { result, onpage }: Props = $props();

	const columns = $derived(
		result.columns.map((c) => ({
			label: c.label,
			class: c.kind === 'measure' ? 'right' : undefined
		}))
	);

	const pageCount = $derived(
		result.page_size > 0 ? Math.max(1, Math.ceil(result.total_rows / result.page_size)) : 1
	);
	const canPrev = $derived(result.page > 1);
	const canNext = $derived(result.page < pageCount);

	// Row start/end for the "showing X–Y of N" caption.
	const rangeStart = $derived(
		result.total_rows === 0 ? 0 : (result.page - 1) * result.page_size + 1
	);
	const rangeEnd = $derived(Math.min(result.page * result.page_size, result.total_rows));

	function isMoney(key: string): boolean {
		const col = result.columns.find((c) => c.key === key);
		return col?.kind === 'measure' && col?.type === 'money';
	}
</script>

<div class="result" data-testid="report-result">
	<DataTable {columns} isEmpty={result.rows.length === 0} empty="No rows for this report.">
		{#snippet body()}
			{#each result.rows as row, i (i)}
				<tr>
					{#each result.columns as col (col.key)}
						<td class:right={col.kind === 'measure'} class:mono={col.kind === 'measure'}>
							{#if isMoney(col.key)}
								<Money
									amount={row[col.key] as string | number | null}
									currency={cellCurrency(col, row)}
									mono
								/>
							{:else}
								{row[col.key] ?? '—'}
							{/if}
						</td>
					{/each}
				</tr>
			{/each}
		{/snippet}
	</DataTable>

	<div class="pager">
		<span class="caption" data-testid="report-result-caption">
			{#if result.total_rows === 0}
				No results
			{:else}
				Showing {rangeStart}–{rangeEnd} of {result.total_rows}
			{/if}
		</span>
		<div class="pager-btns">
			<button
				type="button"
				class="btn-secondary"
				disabled={!canPrev}
				onclick={() => onpage?.(result.page - 1)}>Previous</button
			>
			<span class="page-of">Page {result.page} of {pageCount}</span>
			<button
				type="button"
				class="btn-secondary"
				disabled={!canNext}
				onclick={() => onpage?.(result.page + 1)}>Next</button
			>
		</div>
	</div>
</div>

<style>
	.result {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.pager {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		flex-wrap: wrap;
	}
	.caption {
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.pager-btns {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.page-of {
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.btn-secondary {
		padding: 6px 14px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}
	.btn-secondary:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.btn-secondary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
