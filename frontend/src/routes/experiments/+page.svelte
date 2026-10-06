<script lang="ts">
	import type {
		Experiment,
		ExperimentResults,
		PrimaryMetric
	} from '#lib/types/experiments.ts';
	import {
		EXPERIMENT_STATUSES,
		experimentStatusLabelKey,
		STATUS_TONES,
		PRIMARY_METRIC_LABEL_KEYS
	} from '#lib/types/experiments.ts';
	import type { WorkflowDefinition } from '#lib/types/workflow.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { api } from '#lib/api.ts';
	import {
		listExperiments,
		createExperiment,
		startExperiment,
		stopExperiment,
		concludeExperiment,
		deleteExperiment,
		getExperimentResults
	} from '#lib/api/experiments.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';

	const canMutate = $derived(auth.isAdmin);

	const METRICS: PrimaryMetric[] = [
		'time_to_approval_days',
		'touchless_rate_pct',
		'exception_rate_pct',
		'rejection_rate_pct'
	];

	const COLUMNS = $derived([
		{ label: m('experiments.col.name') },
		{ label: m('experiments.col.workflow') },
		{ label: m('experiments.col.primaryMetric') },
		{ label: m('experiments.col.split') },
		{ label: m('experiments.col.assigned'), class: 'right' },
		{ label: m('experiments.col.status') },
		{ class: 'actions-col' }
	]);

	let experiments = $state<Experiment[]>([]);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let statusFilter = $state<string>('all');

	let definitions = $state<WorkflowDefinition[]>([]);

	// Create modal state.
	let showCreate = $state(false);
	let saving = $state(false);
	let createError = $state<string | null>(null);
	let form = $state({
		name: '',
		description: '',
		workflow_definition_id: '',
		split_a_pct: 50,
		primary_metric: 'time_to_approval_days' as PrimaryMetric,
		min_sample_per_variant: 10,
		config_a: '{\n  "steps": []\n}',
		config_b: '{\n  "steps": []\n}'
	});

	// Results modal state.
	let resultsFor = $state<Experiment | null>(null);
	let results = $state<ExperimentResults | null>(null);
	let resultsLoading = $state(false);
	let resultsError = $state<string | null>(null);

	// The results readout is a re-issuable fetch keyed on which experiment was
	// clicked, so it takes a request-identity guard (`frontend/CLAUDE.md`
	// § Sequencing list fetches). Its own sequencer — the experiment LIST above
	// is an independent request stream, and every mutation re-fetches it.
	const resultsSequence = createRequestSequencer();

	const filtered = $derived(
		statusFilter === 'all'
			? experiments
			: experiments.filter((e) => e.status === statusFilter)
	);

	// The status name is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank. The chips
	// and the row badge read the SAME map, so a status can't be named two ways
	// one column apart (the chips used to carry their own `experiments.chip.*`
	// copies, translated, beside an untranslated badge).
	const statusLabel = (s: string) => {
		const key = experimentStatusLabelKey(s);
		return key ? m(key) : s;
	};

	const STATUS_CHIPS = $derived([
		{ key: 'all', label: m('common.all'), count: experiments.length },
		...EXPERIMENT_STATUSES.map((s) => ({
			key: s,
			label: statusLabel(s),
			count: experiments.filter((e) => e.status === s).length
		}))
	]);

	async function load() {
		loading = true;
		loadError = null;
		try {
			const res = await listExperiments();
			experiments = res.experiments;
		} catch {
			loadError = m('experiments.error.load');
		} finally {
			loading = false;
		}
	}

	async function loadDefinitions() {
		try {
			const res = await api.get<{ items: WorkflowDefinition[] }>(
				'/api/workflows?page=1&page_size=100'
			);
			definitions = res.items ?? [];
		} catch {
			definitions = [];
		}
	}

	$effect(() => {
		load();
		if (canMutate) loadDefinitions();
	});

	function openCreate() {
		createError = null;
		form = {
			name: '',
			description: '',
			workflow_definition_id: definitions[0]?.id ?? '',
			split_a_pct: 50,
			primary_metric: 'time_to_approval_days',
			min_sample_per_variant: 10,
			config_a: JSON.stringify(
				definitions[0]?.steps_config ?? { steps: [] },
				null,
				2
			),
			config_b: JSON.stringify(
				definitions[0]?.steps_config ?? { steps: [] },
				null,
				2
			)
		};
		showCreate = true;
	}

	// When the chosen definition changes, seed both configs from its live steps.
	function onDefinitionChange() {
		const defn = definitions.find((d) => d.id === form.workflow_definition_id);
		if (defn) {
			const json = JSON.stringify(defn.steps_config, null, 2);
			form.config_a = json;
			form.config_b = json;
		}
	}

	async function submitCreate() {
		createError = null;
		if (!form.name.trim()) {
			createError = m('experiments.validate.nameRequired');
			return;
		}
		if (!form.workflow_definition_id) {
			createError = m('experiments.validate.defnRequired');
			return;
		}
		let configA: Record<string, unknown>;
		let configB: Record<string, unknown>;
		try {
			configA = JSON.parse(form.config_a);
		} catch {
			createError = m('experiments.validate.configAInvalid');
			return;
		}
		try {
			configB = JSON.parse(form.config_b);
		} catch {
			createError = m('experiments.validate.configBInvalid');
			return;
		}
		saving = true;
		try {
			await createExperiment({
				name: form.name.trim(),
				description: form.description.trim() || null,
				workflow_definition_id: form.workflow_definition_id,
				config_a: configA,
				config_b: configB,
				split_a_pct: form.split_a_pct,
				primary_metric: form.primary_metric,
				min_sample_per_variant: form.min_sample_per_variant
			});
			showCreate = false;
			toast(m('experiments.toast.created'), 'success');
			await load();
		} catch (e) {
			createError = e instanceof Error ? e.message : m('experiments.toast.createFailed');
		} finally {
			saving = false;
		}
	}

	async function doStart(exp: Experiment) {
		try {
			await startExperiment(exp.id);
			toast(m('experiments.toast.started'), 'success');
			await load();
		} catch {
			toast(m('experiments.toast.startFailed'), 'error');
		}
	}

	async function doStop(exp: Experiment) {
		try {
			await stopExperiment(exp.id);
			toast(m('experiments.toast.stopped'), 'success');
			await load();
		} catch {
			toast(m('experiments.toast.stopFailed'), 'error');
		}
	}

	async function doConclude(exp: Experiment) {
		try {
			await concludeExperiment(exp.id);
			toast(m('experiments.toast.concluded'), 'success');
			await load();
		} catch {
			toast(m('experiments.toast.concludeFailed'), 'error');
		}
	}

	let confirmDeleteId = $state<string | null>(null);
	async function doDelete(exp: Experiment) {
		if (confirmDeleteId !== exp.id) {
			confirmDeleteId = exp.id;
			return;
		}
		confirmDeleteId = null;
		try {
			await deleteExperiment(exp.id);
			toast(m('experiments.toast.deleted'), 'success');
			await load();
		} catch {
			toast(m('experiments.toast.deleteFailed'), 'error');
		}
	}

	async function openResults(exp: Experiment) {
		const token = resultsSequence.start();
		resultsFor = exp;
		results = null;
		resultsError = null;
		resultsLoading = true;
		try {
			const res = await getExperimentResults(exp.id);
			// Open one experiment's results, close it, open another: the first
			// response must not land under the second's name — a winner call
			// attributed to the wrong experiment is worse than no readout.
			if (!resultsSequence.canCommit(token)) return;
			results = res;
		} catch {
			// `isCurrentRequest`, not `canCommit`: only the newest request reports.
			if (!resultsSequence.isCurrentRequest(token)) return;
			resultsError = m('experiments.error.results');
		} finally {
			if (resultsSequence.isCurrentRequest(token)) resultsLoading = false;
		}
	}

</script>

<svelte:window onclick={(e) => {
	if (confirmDeleteId && !(e.target as HTMLElement)?.closest('.row-action')) confirmDeleteId = null;
}} />

<PageHeader title={m('experiments.title')}>
	{#snippet actions()}
		{#if canMutate}
			<button class="btn-primary" onclick={openCreate}>{m('experiments.new')}</button>
		{/if}
	{/snippet}

	<p class="lede">{m('experiments.lede')}</p>

	<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} />

	{#if loadError}
		<!-- Not rendered ALONGSIDE the table: the banner and "No experiments yet."
		     used to appear together, telling the reader two different things at
		     once — and neither offered a way to try again, since the toast-less
		     banner came from a dependency-free `$effect`. Same error-with-retry
		     block `/admin/api-keys` uses. -->
		<div class="state error" data-testid="experiments-error" role="alert">
			<p>{loadError}</p>
			<button type="button" class="btn-cancel" onclick={load}>{m('experiments.retry')}</button>
		</div>
	{:else}
	<DataTable
		columns={COLUMNS}
		isEmpty={filtered.length === 0}
		empty={loading ? m('common.loading') : m('experiments.table.empty')}
	>
		{#snippet body()}
			{#each filtered as exp (exp.id)}
				<tr class="clickable" onclick={(e) => { if (isRowOpenClick(e)) openResults(exp); }}>
					<td>
						<RowLink onclick={() => openResults(exp)} ariaLabel={m('experiments.row.viewAria', { name: exp.name })}>
							{exp.name}
						</RowLink>
					</td>
					<td>{exp.workflow_definition_name ?? '—'}</td>
					<td>{m(PRIMARY_METRIC_LABEL_KEYS[exp.primary_metric])}</td>
					<td class="mono">{exp.split_a_pct}% / {100 - exp.split_a_pct}%</td>
					<td class="right mono">{exp.assigned_count}</td>
					<td>
						<Badge tone={STATUS_TONES[exp.status]} variant={exp.status}>
							{statusLabel(exp.status)}
						</Badge>
					</td>
					<td class="actions">
						{#if canMutate && exp.status === 'draft'}
							<RowAction variant="success" onclick={() => doStart(exp)}>{m('experiments.row.start')}</RowAction>
							<RowAction
								variant="danger"
								armed={confirmDeleteId === exp.id}
								onclick={() => doDelete(exp)}
							>
								{confirmDeleteId === exp.id ? m('experiments.row.confirm') : m('experiments.row.delete')}
							</RowAction>
						{:else if canMutate && exp.status === 'running'}
							<RowAction onclick={() => doStop(exp)}>{m('experiments.row.stop')}</RowAction>
							<RowAction variant="danger" onclick={() => doConclude(exp)}>{m('experiments.row.conclude')}</RowAction>
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>
	{/if}
</PageHeader>

<!-- Create experiment -->
<Modal
	open={showCreate}
	ariaLabel={m('experiments.modal.aria')}
	title={m('experiments.modal.title')}
	width="lg"
	onclose={() => (showCreate = false)}
>
	<form onsubmit={(e) => { e.preventDefault(); submitCreate(); }}>
		<div class="field">
			<label for="exp-name">{m('experiments.modal.name')} <em class="required">*</em></label>
			<input id="exp-name" type="text" bind:value={form.name} placeholder={m('experiments.modal.namePlaceholder')} />
		</div>
		<div class="field">
			<label for="exp-desc">{m('experiments.modal.description')}</label>
			<input id="exp-desc" type="text" bind:value={form.description} placeholder={m('experiments.modal.descriptionPlaceholder')} />
		</div>
		<div class="field">
			<div class="help-row label-row">
				<label for="exp-defn">{m('experiments.modal.workflowDefinition')} <em class="required">*</em></label>
				<HelpTip term="workflow-experiment" />
			</div>
			<select id="exp-defn" bind:value={form.workflow_definition_id} onchange={onDefinitionChange}>
				{#each definitions as d (d.id)}
					<option value={d.id}>{d.name}</option>
				{/each}
			</select>
		</div>
		<div class="field-row">
			<div class="field">
				<label for="exp-split">{m('experiments.modal.splitLabel')}</label>
				<input id="exp-split" type="number" min="0" max="100" bind:value={form.split_a_pct} />
			</div>
			<div class="field">
				<label for="exp-metric">{m('experiments.modal.primaryMetric')}</label>
				<select id="exp-metric" bind:value={form.primary_metric}>
					{#each METRICS as mtr (mtr)}
						<option value={mtr}>{m(PRIMARY_METRIC_LABEL_KEYS[mtr])}</option>
					{/each}
				</select>
			</div>
			<div class="field">
				<label for="exp-min">{m('experiments.modal.minSample')}</label>
				<input id="exp-min" type="number" min="1" bind:value={form.min_sample_per_variant} />
			</div>
		</div>
		<div class="field-row">
			<div class="field">
				<label for="exp-cfg-a">{m('experiments.modal.configA')}</label>
				<textarea id="exp-cfg-a" rows="8" class="mono" bind:value={form.config_a}></textarea>
			</div>
			<div class="field">
				<label for="exp-cfg-b">{m('experiments.modal.configB')}</label>
				<textarea id="exp-cfg-b" rows="8" class="mono" bind:value={form.config_b}></textarea>
			</div>
		</div>
		{#if createError}
			<p class="error-banner">{createError}</p>
		{/if}
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (showCreate = false)}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving}>
				{saving ? m('common.saving') : m('experiments.modal.create')}
			</button>
		</div>
	</form>
</Modal>

<!-- Results readout -->
<Modal
	open={resultsFor !== null}
	ariaLabel={m('experiments.results.aria')}
	title={resultsFor ? m('experiments.results.titleNamed', { name: resultsFor.name }) : m('experiments.results.title')}
	width="lg"
	onclose={() => (resultsFor = null)}
>
	{#if resultsLoading}
		<p>{m('experiments.results.loading')}</p>
	{:else if resultsError}
		<p class="error-banner">{resultsError}</p>
	{:else if results}
		<div
			class="readout"
			data-testid="experiment-results"
			data-experiment-id={resultsFor?.id}
			data-results-for={results.experiment_id}
		>
			{#if results.enough_data}
				<div class="winner" class:tie={results.winner === 'tie'}>
					{#if results.winner === 'tie'}
						<strong>{m('experiments.results.noWinner')}</strong>
					{:else}
						<strong>{m('experiments.results.winner', { variant: results.winner ?? '' })}</strong>
					{/if}
				</div>
			{:else}
				<div class="winner pending"><strong>{m('experiments.results.notEnough')}</strong></div>
			{/if}
			<p class="rationale">{results.rationale}</p>
			{#if results.notes.length}
				<ul class="notes">
					{#each results.notes as note (note)}<li>{note}</li>{/each}
				</ul>
			{/if}

			<table class="variant-table">
				<thead>
					<tr>
						<th scope="col">{m('experiments.results.metric')}</th>
						<th scope="col" class="right">{m('experiments.results.variantA')}</th>
						<th scope="col" class="right">{m('experiments.results.variantB')}</th>
					</tr>
				</thead>
				<tbody>
					<tr><td>{m('experiments.results.assigned')}</td><td class="right mono">{results.variant_a.assigned_count}</td><td class="right mono">{results.variant_b.assigned_count}</td></tr>
					<tr><td>{m('experiments.results.completed')}</td><td class="right mono">{results.variant_a.completed_count}</td><td class="right mono">{results.variant_b.completed_count}</td></tr>
					<tr class:primary={results.primary_metric === 'time_to_approval_days'}>
						<td>{m('experiments.results.medianTime')}</td>
						<td class="right mono">{results.variant_a.median_time_to_approval_days}</td>
						<td class="right mono">{results.variant_b.median_time_to_approval_days}</td>
					</tr>
					<tr class:primary={results.primary_metric === 'touchless_rate_pct'}>
						<td>{m('experiments.results.touchlessRate')}</td>
						<td class="right mono">{results.variant_a.touchless_rate_pct}%</td>
						<td class="right mono">{results.variant_b.touchless_rate_pct}%</td>
					</tr>
					<tr class:primary={results.primary_metric === 'exception_rate_pct'}>
						<td>{m('experiments.results.exceptionRate')}</td>
						<td class="right mono">{results.variant_a.exception_rate_pct}%</td>
						<td class="right mono">{results.variant_b.exception_rate_pct}%</td>
					</tr>
					<tr class:primary={results.primary_metric === 'rejection_rate_pct'}>
						<td>{m('experiments.results.rejectionRate')}</td>
						<td class="right mono">{results.variant_a.rejection_rate_pct}%</td>
						<td class="right mono">{results.variant_b.rejection_rate_pct}%</td>
					</tr>
				</tbody>
			</table>
			<p class="hint">{m('experiments.results.hint', { n: results.min_sample_per_variant })}</p>
		</div>
	{/if}
</Modal>

<style>
	.lede {
		color: var(--text-muted);
		max-width: 70ch;
		margin: 0;
	}

	/* The status pill is `<Badge>` now — it had re-typed the shared recipe by
	   hand, under classes that named the paint (green/amber/grey) rather than
	   the status. The tone per status lives in `types/experiments`. */
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

	.error-banner {
		color: var(--danger);
		margin: 8px 0;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 12px;
		flex: 1;
	}

	.field-row {
		display: flex;
		gap: 12px;
	}

	.field label {
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	/* A field label's ⓘ row (global `.help-row`) sits tighter than a heading's. */
	.label-row {
		gap: 4px;
	}
	textarea.mono {
		font-family: ui-monospace, monospace;
		font-size: 0.8rem;
	}

	.readout .winner {
		padding: 10px 14px;
		border-radius: 8px;
		background: color-mix(in srgb, var(--accent) 18%, transparent);
		margin-bottom: 10px;
	}

	.readout .winner.tie,
	.readout .winner.pending {
		background: color-mix(in srgb, var(--text-muted) 18%, transparent);
	}

	.rationale {
		color: var(--text-muted);
		margin: 0 0 8px;
	}

	.notes {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 0 0 12px;
	}

	.variant-table {
		width: 100%;
		border-collapse: collapse;
		margin-top: 8px;
	}

	.variant-table th,
	.variant-table td {
		padding: 6px 10px;
		border-bottom: 1px solid var(--border);
		text-align: left;
	}
	.variant-table .right {
		text-align: right;
	}

	.variant-table tr.primary {
		background: color-mix(in srgb, var(--accent) 12%, transparent);
		font-weight: 600;
	}

	.hint {
		color: var(--text-muted);
		font-size: 0.8rem;
		margin-top: 10px;
	}

</style>
