<script lang="ts">
	import type { Snippet } from 'svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { ImportResult } from '#lib/types/csvImport.ts';
	import { localizeApiDetail } from '#lib/api/glChartRefusal.ts';

	// Generic Day-0 CSV import flow shared by `/vendors` and `/invoices`:
	// pick a file, upload it, show the skip-and-report result (every bad row
	// is counted + explained, never an all-or-nothing failure — see
	// backend/docs/csv-import.md). The two callers differ only in which
	// endpoint they hit and what column guide to show, so both live here
	// instead of two near-identical modals.
	let {
		title,
		ariaLabel,
		columnsHint,
		onimport,
		onclose,
		onimported
	}: {
		title: string;
		ariaLabel: string;
		/** Renders the column guide for this importer (required/optional
		 *  columns, an example row) — differs between vendors and invoices. */
		columnsHint: Snippet;
		/** Performs the upload; throws on a hard failure (bad file, size cap,
		 *  non-UTF-8). Row-level problems come back inside the resolved
		 *  `ImportResult`, not as a rejection. */
		onimport: (file: File) => Promise<ImportResult>;
		onclose: () => void;
		/** Fired after a successful upload (even one with skipped rows) so the
		 *  caller can refresh its list. */
		onimported?: (result: ImportResult) => void;
	} = $props();

	let fileInput = $state<HTMLInputElement | undefined>(undefined);
	let fileName = $state<string | null>(null);
	let importing = $state(false);
	let result = $state<ImportResult | null>(null);

	function onFileChosen(e: Event) {
		fileName = (e.target as HTMLInputElement).files?.[0]?.name ?? null;
		result = null;
	}

	async function runImport() {
		const file = fileInput?.files?.[0];
		if (!file) return;
		importing = true;
		try {
			const res = await onimport(file);
			result = res;
			onimported?.(res);
			toast(
				res.skipped > 0
					? m('csvImport.toast.partial', { imported: res.imported, skipped: res.skipped })
					: m('csvImport.toast.success', { imported: res.imported }),
				res.skipped > 0 ? 'info' : 'success'
			);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('csvImport.toast.failed'), 'error');
		} finally {
			importing = false;
		}
	}

	function reset() {
		fileName = null;
		result = null;
		if (fileInput) fileInput.value = '';
	}
</script>

<Modal open={true} {ariaLabel} {title} width="md" {onclose}>
	{#if result}
		<div class="result">
			<p class="result-summary">
				{m('csvImport.result.summary', { imported: result.imported, skipped: result.skipped })}
			</p>
			{#if result.errors.length > 0}
				<ul class="error-list">
					{#each result.errors as e (e.row)}
						<li>{m('csvImport.result.rowError', { row: e.row, message: localizeApiDetail(e, m) ?? e.message })}</li>
					{/each}
				</ul>
			{/if}
		</div>
		<div class="modal-footer">
			<button type="button" class="btn-outline" onclick={reset}>
				{m('csvImport.importAnother')}
			</button>
			<button type="button" class="btn-primary" onclick={onclose}>{m('csvImport.close')}</button>
		</div>
	{:else}
		<div class="hint">
			{@render columnsHint()}
		</div>

		<input type="file" accept=".csv" bind:this={fileInput} onchange={onFileChosen} hidden />
		<button type="button" class="btn-outline file-pick" onclick={() => fileInput?.click()}>
			{fileName ?? m('csvImport.chooseFile')}
		</button>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="button" class="btn-primary" disabled={!fileName || importing} onclick={runImport}>
				{importing ? m('csvImport.importing') : m('csvImport.import')}
			</button>
		</div>
	{/if}
</Modal>

<style>
	.btn-outline {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 8px 18px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-outline:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-outline:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 10px 12px;
		margin-bottom: 14px;
	}

	.hint :global(code) {
		background: var(--surface);
		padding: 1px 4px;
		border-radius: 3px;
		font-size: 0.8em;
	}

	.hint :global(ul) {
		margin: 6px 0 0;
		padding-left: 18px;
	}

	.hint :global(p:first-child) {
		margin-top: 0;
	}

	.hint :global(p:last-child) {
		margin-bottom: 0;
	}

	.file-pick {
		width: 100%;
		text-align: left;
	}

	.result-summary {
		font-size: 0.88rem;
		margin: 0 0 10px;
	}

	.error-list {
		max-height: 260px;
		overflow-y: auto;
		margin: 0;
		padding-left: 18px;
		font-size: 0.8rem;
		color: var(--danger);
	}

	.error-list li {
		margin-bottom: 4px;
	}
</style>
