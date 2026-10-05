<script lang="ts">
	// Custom (ad-hoc) Report Builder. Pick a data source from the catalog, then
	// choose dimensions / measures / filters — the whole builder is driven by
	// `GET /api/reports/catalog`, never a hardcoded field list. Run renders a
	// `ReportResult`; a spec can be saved as a named definition, re-loaded, and
	// exported (CSV / PDF). See `#lib/api/reports.ts` + the API contract.
	import { page } from '$app/state';
	import { replaceState } from '$app/navigation';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import {
		createReport,
		deleteReport,
		downloadReportExport,
		getReport,
		getReportCatalog,
		listReports,
		runReport,
		runSavedReport,
		updateReport
	} from '#lib/api/reports.ts';
	import type {
		ReportCatalog,
		ReportDefinition,
		ReportResult,
		ReportSpec,
		SortDir,
		SpecDimension,
		SpecFilter,
		SpecMeasure
	} from '#lib/types/reports.ts';
	import { AGG_LABELS, measureColumnKey } from '#lib/types/reports.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { timeAgo } from '#lib/utils/time.ts';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import DimensionEditor from '#lib/components/reports/DimensionEditor.svelte';
	import MeasureEditor from '#lib/components/reports/MeasureEditor.svelte';
	import FilterEditor from '#lib/components/reports/FilterEditor.svelte';
	import ResultTable from '#lib/components/reports/ResultTable.svelte';
	import SaveReportModal from '#lib/components/reports/SaveReportModal.svelte';

	// Saving / patching / deleting a definition is admin/ap_manager/cfo; running
	// + reading is all four roles (the backend enforces regardless).
	const canMutate = $derived(auth.hasAnyRole('admin', 'ap_manager', 'cfo'));

	const PAGE_SIZE = 100;

	// --- catalog ---
	let catalog = $state<ReportCatalog | null>(null);
	let catalogLoading = $state(true);
	let catalogError = $state<string | null>(null);

	// --- spec state ---
	let sourceKey = $state('');
	let dims = $state<SpecDimension[]>([]);
	let measures = $state<SpecMeasure[]>([]);
	let filters = $state<SpecFilter[]>([]);
	let sortKey = $state('');
	let sortDir = $state<SortDir>('desc');

	const source = $derived(catalog?.sources.find((s) => s.key === sourceKey) ?? null);

	// Sort options = every chosen dimension + measure column.
	const sortOptions = $derived([
		...dims.map((d) => ({
			key: d.key,
			label: source?.dimensions.find((c) => c.key === d.key)?.label ?? d.key
		})),
		...measures.map((m) => ({
			key: measureColumnKey(m),
			label: `${AGG_LABELS[m.agg]} of ${source?.measures.find((c) => c.key === m.key)?.label ?? m.key}`
		}))
	]);

	const spec = $derived<ReportSpec>({
		data_source: sourceKey,
		dimensions: dims,
		measures,
		filters,
		sort: sortKey && sortOptions.some((o) => o.key === sortKey) ? [{ key: sortKey, dir: sortDir }] : [],
		limit: null
	});

	const canRun = $derived(!!sourceKey && measures.length > 0);

	// --- run state ---
	let result = $state<ReportResult | null>(null);
	let running = $state(false);
	let runError = $state<string | null>(null);
	let currentPage = $state(1);

	// --- saved definitions ---
	let saved = $state<ReportDefinition[]>([]);
	let savedLoading = $state(true);
	let savedError = $state<string | null>(null);

	// The saved definition currently loaded into the builder (enables Update +
	// Export, which need a persisted id).
	let loadedId = $state<string | null>(null);
	let loadedName = $state('');
	let loadedDescription = $state('');

	// --- save modal ---
	let showSave = $state(false);
	let savingReport = $state(false);
	let saveError = $state<string | null>(null);

	async function loadCatalog() {
		catalogLoading = true;
		catalogError = null;
		try {
			catalog = await getReportCatalog();
			if (!sourceKey && catalog.sources[0]) selectSource(catalog.sources[0].key);
		} catch (e) {
			catalogError = e instanceof Error ? e.message : 'Failed to load the report catalog.';
		} finally {
			catalogLoading = false;
		}
	}

	async function loadSaved() {
		savedLoading = true;
		savedError = null;
		try {
			const res = await listReports();
			saved = res.reports;
		} catch (e) {
			savedError = e instanceof Error ? e.message : 'Failed to load saved reports.';
		} finally {
			savedLoading = false;
		}
	}

	// Track whether the loaded spec has been edited since load (so Run uses the
	// ad-hoc endpoint reflecting the live edits rather than the stale saved one).
	//
	// The snapshot is taken IMPERATIVELY, at the one moment a definition is
	// loaded — never in an `$effect`. An effect that calls `specForCompare()`
	// reads the very state it snapshots, so every edit re-ran it and re-pinned
	// the snapshot to the current spec; `dirtySinceLoad` was then permanently
	// `false` and `run()` always executed the PERSISTED spec server-side while
	// the builder showed the edited one.
	let loadedSnapshot = $state('');
	function specForCompare() {
		return { dims, measures, filters, sortKey, sortDir, sourceKey };
	}
	const dirtySinceLoad = $derived(
		loadedId ? JSON.stringify(specForCompare()) !== loadedSnapshot : false
	);

	// Reset the whole builder to a fresh spec on the given source.
	function selectSource(key: string) {
		sourceKey = key;
		dims = [];
		measures = [];
		filters = [];
		sortKey = '';
		sortDir = 'desc';
		result = null;
		runError = null;
		currentPage = 1;
		loadedId = null;
		loadedName = '';
		loadedDescription = '';
		loadedSnapshot = '';
		clearIdParam();
	}

	function applyDefinition(def: ReportDefinition) {
		sourceKey = def.data_source;
		dims = def.dimensions.map((d) => ({ ...d }));
		measures = def.measures.map((m) => ({ ...m }));
		filters = def.filters.map((f) => ({ ...f }));
		const s = def.sort[0];
		sortKey = s?.key ?? '';
		sortDir = s?.dir ?? 'desc';
		loadedId = def.id;
		loadedName = def.name;
		loadedDescription = def.description ?? '';
		result = null;
		runError = null;
		currentPage = 1;
		// Baseline for `dirtySinceLoad` — taken here, after the spec fields above
		// are assigned, so it captures exactly what was persisted.
		loadedSnapshot = JSON.stringify(specForCompare());
	}

	async function loadDefinition(id: string, { updateUrl = true } = {}) {
		try {
			const def = await getReport(id);
			applyDefinition(def);
			if (updateUrl) setIdParam(id);
		} catch (e) {
			toast(e instanceof Error ? e.message : 'Failed to load that report.', 'error');
		}
	}

	function setIdParam(id: string) {
		const url = new URL(page.url.href);
		url.searchParams.set('id', id);
		replaceState(`${url.pathname}${url.search}`, {});
	}
	function clearIdParam() {
		const url = new URL(page.url.href);
		if (url.searchParams.has('id')) {
			url.searchParams.delete('id');
			replaceState(`${url.pathname}${url.search}`, {});
		}
	}

	async function run(atPage = 1) {
		if (!canRun) return;
		running = true;
		runError = null;
		currentPage = atPage;
		try {
			// A loaded saved report runs its persisted spec; otherwise the ad-hoc one.
			result =
				loadedId && !dirtySinceLoad
					? await runSavedReport(loadedId, { page: atPage, page_size: PAGE_SIZE })
					: await runReport(spec, { page: atPage, page_size: PAGE_SIZE });
		} catch (e) {
			runError = e instanceof Error ? e.message : 'Failed to run the report.';
			result = null;
		} finally {
			running = false;
		}
	}

	function openSave() {
		saveError = null;
		showSave = true;
	}

	async function onSave(data: { name: string; description: string }) {
		savingReport = true;
		saveError = null;
		try {
			const def = await createReport({
				...spec,
				name: data.name,
				description: data.description || null
			});
			showSave = false;
			toast('Report saved.', 'success');
			applyDefinition(def);
			setIdParam(def.id);
			await loadSaved();
		} catch (e) {
			saveError = e instanceof Error ? e.message : 'Failed to save the report.';
		} finally {
			savingReport = false;
		}
	}

	async function updateLoaded() {
		if (!loadedId) return;
		try {
			const def = await updateReport(loadedId, {
				...spec,
				name: loadedName,
				description: loadedDescription || null
			});
			applyDefinition(def);
			toast('Report updated.', 'success');
			await loadSaved();
		} catch (e) {
			toast(e instanceof Error ? e.message : 'Failed to update the report.', 'error');
		}
	}

	let confirmDeleteId = $state<string | null>(null);
	async function doDelete(def: ReportDefinition) {
		if (confirmDeleteId !== def.id) {
			confirmDeleteId = def.id;
			return;
		}
		confirmDeleteId = null;
		try {
			await deleteReport(def.id);
			toast('Report deleted.', 'success');
			if (loadedId === def.id) selectSource(sourceKey);
			await loadSaved();
		} catch (e) {
			toast(e instanceof Error ? e.message : 'Failed to delete the report.', 'error');
		}
	}

	// Run a saved report inline (loads it into the builder first, then runs).
	async function runSaved(def: ReportDefinition) {
		await loadDefinition(def.id);
		await run(1);
	}

	async function exportSaved(id: string, format: 'csv' | 'pdf') {
		try {
			const blob = await downloadReportExport(id, format);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `report_${id}.${format}`;
			document.body.appendChild(a);
			a.click();
			a.remove();
			URL.revokeObjectURL(url);
		} catch (e) {
			toast(e instanceof Error ? e.message : `Failed to export ${format.toUpperCase()}.`, 'error');
		}
	}

	const SAVED_COLUMNS = [
		{ label: 'Name' },
		{ label: 'Source' },
		{ label: 'Updated' },
		{ class: 'actions-col' }
	];

	$effect(() => {
		void (async () => {
			await Promise.all([loadCatalog(), loadSaved()]);
			// Deep-link: /reports?id=<uuid> loads that saved definition.
			const id = page.url.searchParams.get('id');
			if (id && !loadedId) await loadDefinition(id, { updateUrl: false });
		})();
	});
</script>

<svelte:window
	onclick={(e) => {
		if (confirmDeleteId && !(e.target as HTMLElement)?.closest('.row-action')) confirmDeleteId = null;
	}}
/>

<PageHeader title="Report Builder">
	{#snippet actions()}
		{#if canMutate}
			<button class="btn-primary" onclick={openSave} disabled={!canRun}>Save report</button>
		{/if}
	{/snippet}

	<p class="lede">
		Build an ad-hoc report: pick a data source, group by dimensions, add measures, and filter —
		then run, save, and export. Read is open to your whole team.
	</p>

	{#if catalogLoading}
		<p class="muted">Loading catalog…</p>
	{:else if catalogError}
		<!-- The catalog drives the WHOLE builder, so a failure has to replace it —
		     but it was replaced by a bare paragraph fired from a single-shot,
		     dependency-free `$effect`, leaving nothing on screen that could
		     re-run the load. Same error-with-retry block `/admin/api-keys` uses. -->
		<div class="state error" data-testid="catalog-error" role="alert">
			<p>{catalogError}</p>
			<button type="button" class="btn-cancel" onclick={loadCatalog}>Retry</button>
		</div>
	{:else if catalog}
		<div class="builder">
			<div class="field source-field">
				<label for="report-source">Data source</label>
				<select
					id="report-source"
					value={sourceKey}
					onchange={(e) => selectSource(e.currentTarget.value)}
				>
					{#each catalog.sources as s (s.key)}
						<option value={s.key}>{s.label}</option>
					{/each}
				</select>
				{#if loadedId}
					<span class="loaded-tag" data-testid="loaded-tag">Editing “{loadedName}”</span>
				{/if}
			</div>

			{#if source}
				<DimensionEditor available={source.dimensions} bind:selected={dims} />
				<MeasureEditor available={source.measures} bind:selected={measures} />
				<FilterEditor available={source.filters} bind:selected={filters} />

				{#if sortOptions.length}
					<div class="sort-row">
						<label for="report-sort">Sort by</label>
						<select id="report-sort" bind:value={sortKey}>
							<option value="">— None —</option>
							{#each sortOptions as o (o.key)}
								<option value={o.key}>{o.label}</option>
							{/each}
						</select>
						<select aria-label="Sort direction" bind:value={sortDir} disabled={!sortKey}>
							<option value="desc">Descending</option>
							<option value="asc">Ascending</option>
						</select>
					</div>
				{/if}

				<div class="builder-actions">
					<button
						class="btn-primary"
						data-testid="run-report"
						onclick={() => run(1)}
						disabled={!canRun || running}
					>
						{running ? 'Running…' : 'Run report'}
					</button>
					{#if canMutate && loadedId}
						<button class="btn-secondary" onclick={updateLoaded} disabled={running}
							>Save changes</button
						>
					{/if}
					{#if loadedId}
						<button class="btn-secondary" onclick={() => exportSaved(loadedId!, 'csv')}
							>Export CSV</button
						>
						<button class="btn-secondary" onclick={() => exportSaved(loadedId!, 'pdf')}
							>Export PDF</button
						>
					{/if}
					{#if !canRun}
						<span class="muted small">Add at least one measure to run.</span>
					{/if}
				</div>
			{/if}
		</div>

		{#if runError}
			<p class="error-banner" role="alert">{runError}</p>
		{/if}

		{#if result}
			<ResultTable {result} onpage={(p) => run(p)} />
		{/if}

		<!-- Saved reports -->
		<section class="saved" data-testid="saved-reports">
			<h2>Saved reports</h2>
			{#if savedError}
				<!-- Not rendered ALONGSIDE the table: an error banner over "No saved
				     reports yet." tells the reader two different things at once. -->
				<div class="state error" data-testid="saved-error" role="alert">
					<p>{savedError}</p>
					<button type="button" class="btn-cancel" onclick={loadSaved}>Retry</button>
				</div>
			{:else}
			<DataTable
				columns={SAVED_COLUMNS}
				isEmpty={saved.length === 0}
				empty={savedLoading ? 'Loading…' : 'No saved reports yet.'}
			>
				{#snippet body()}
					{#each saved as def (def.id)}
						<tr
							class="clickable"
							onclick={(e) => {
								if (isRowOpenClick(e)) loadDefinition(def.id);
							}}
						>
							<td>
								<RowLink onclick={() => loadDefinition(def.id)} ariaLabel="Load report {def.name}">
									{def.name}
								</RowLink>
								{#if def.description}
									<div class="row-desc">{def.description}</div>
								{/if}
							</td>
							<td>{catalog?.sources.find((s) => s.key === def.data_source)?.label ?? def.data_source}</td>
							<td class="muted">{timeAgo(def.updated_at)}</td>
							<td class="actions">
								<RowAction onclick={() => runSaved(def)} ariaLabel="Run report {def.name}">Run</RowAction>
								<RowAction onclick={() => exportSaved(def.id, 'csv')} ariaLabel="Export {def.name} as CSV">CSV</RowAction>
								<RowAction onclick={() => exportSaved(def.id, 'pdf')} ariaLabel="Export {def.name} as PDF">PDF</RowAction>
								{#if canMutate}
									<RowAction
										variant="danger"
										armed={confirmDeleteId === def.id}
										onclick={() => doDelete(def)}
										ariaLabel="Delete report {def.name}"
									>
										{confirmDeleteId === def.id ? 'Confirm' : 'Delete'}
									</RowAction>
								{/if}
							</td>
						</tr>
					{/each}
				{/snippet}
			</DataTable>
			{/if}
		</section>
	{/if}
</PageHeader>

<SaveReportModal
	open={showSave}
	saving={savingReport}
	error={saveError}
	onsave={onSave}
	onclose={() => (showSave = false)}
/>

<style>
	/* Error-with-retry block — same shape as `/admin/api-keys`. */
	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}
	.state.error {
		color: var(--danger);
	}
	.state.error p {
		margin: 0 0 8px;
	}

	.lede {
		color: var(--text-muted);
		max-width: 80ch;
		margin: 0;
	}
	.builder {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.source-field {
		flex-direction: row;
		align-items: center;
		gap: 12px;
	}
	.source-field label {
		font-weight: 600;
	}
	.loaded-tag {
		color: var(--accent);
		font-size: 0.85rem;
	}
	.sort-row {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.sort-row label {
		font-weight: 500;
	}
	.builder-actions {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
	}
	.btn-secondary {
		padding: 8px 16px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
		flex-shrink: 0;
	}
	.btn-secondary:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.btn-secondary:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	select {
		padding: 7px 10px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		font-size: 0.9rem;
	}
	.saved {
		margin-top: 20px;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.saved h2 {
		font-size: 1.05rem;
		margin: 0;
	}
	.row-desc {
		color: var(--text-muted);
		font-size: 0.8rem;
		margin-top: 2px;
	}
	.error-banner {
		color: var(--danger);
		margin: 4px 0;
	}
	.muted {
		color: var(--text-muted);
	}
	.small {
		font-size: 0.85rem;
	}
</style>
