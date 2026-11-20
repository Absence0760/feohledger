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
	import { isAllowed } from '#lib/api/actionVerdicts.ts';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import { errorMessage } from '#lib/utils/apiError.ts';
	import { formatPercent } from '#lib/utils/percent.ts';
	import { formatCount } from '#lib/utils/count.ts';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { page } from '$app/state';
	import { afterNavigate, replaceState } from '$app/navigation';

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
		{ label: m('experiments.col.primaryMetric'), class: 'col-metric' },
		{ label: m('experiments.col.split'), class: 'col-split' },
		{ label: m('experiments.col.assigned'), class: 'right col-count' },
		{ label: m('experiments.col.status'), class: 'col-status' },
		{ class: 'actions-col' }
	]);

	let experiments = $state<Experiment[]>([]);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	// URL-backed (`?status=`), like every other list's chips: Back / reload /
	// a shared link restore the view. An unknown value falls back to All rather
	// than an empty table no chip is lit for.
	function statusFromUrl(params: Pick<URLSearchParams, 'get'>): string {
		const s = params.get('status');
		return s && (EXPERIMENT_STATUSES as string[]).includes(s) ? s : 'all';
	}
	let statusFilter = $state<string>(statusFromUrl(page.url.searchParams));

	// The one reader of URL state. SvelteKit reuses this component for a
	// navigation within the route (the sidebar link while filtered, Back /
	// Forward between two entries), so the mount-time seed above never sees it
	// (`docs/ui-patterns.md` § "Reading the URL once at mount is only half of
	// the round trip"). The filter is client-side, so no refetch is needed.
	function readUrlState(params: Pick<URLSearchParams, 'get'>) {
		const next = statusFromUrl(params);
		if (next !== statusFilter) statusFilter = next;
	}

	afterNavigate(({ from, to }) => {
		if (from?.route.id && from.route.id === to?.route.id && to?.url) {
			readUrlState(to.url.searchParams);
		}
	});

	// A writer of URL state, never a dependency of it (`docs/ui-patterns.md`):
	// called from the chip's `onchange`, so it cannot re-trigger itself.
	function syncUrl(key: string) {
		const url = new URL(page.url.href);
		if (key !== 'all') url.searchParams.set('status', key);
		else url.searchParams.delete('status');
		replaceState(`${url.pathname}${url.search}`, {});
	}

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

	// Every lifecycle action and a Retry re-issue this, so two can be in flight
	// at once: the newest wins, or a slow pre-action response re-lists a
	// concluded experiment as running (`frontend/CLAUDE.md` § Sequencing).
	const listSequence = createRequestSequencer();

	async function load() {
		const token = listSequence.start();
		loading = true;
		loadError = null;
		try {
			const res = await listExperiments();
			if (!listSequence.canCommit(token)) return;
			experiments = res.experiments;
		} catch {
			if (listSequence.isCurrentRequest(token)) loadError = m('experiments.error.load');
		} finally {
			if (listSequence.isCurrentRequest(token)) loading = false;
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
			createError = errorMessage(e, m('experiments.toast.createFailed'));
		} finally {
			saving = false;
		}
	}

	type RowActionKind = 'start' | 'stop' | 'conclude' | 'delete';

	// One lifecycle call at a time, tracked per action: the button that sent
	// it is `busy` (aria-disabled, so the keyboard user who pressed it keeps
	// focus) and every other row action is `disabled` until it settles, so a
	// double-click cannot start, stop or conclude twice.
	let inFlight = $state<{ action: RowActionKind; id: string } | null>(null);
	const isBusy = (action: RowActionKind, id: string) =>
		inFlight?.action === action && inFlight.id === id;
	const isHeld = (action: RowActionKind, id: string) =>
		inFlight !== null && !isBusy(action, id);

	// Two destructive actions, each armed two-click (ui-patterns § RowAction):
	// Delete (a draft) and Conclude, which is TERMINAL — a concluded experiment
	// takes no new assignments and cannot be restarted — and used to fire on a
	// single click beside the reversible Stop. One armed slot for the page, so
	// arming one row's action disarms every other — two separate slots let a
	// Delete on one row and a Conclude on another sit armed at once.
	let armed = $state<{ action: 'delete' | 'conclude'; id: string } | null>(null);
	const isArmed = (action: 'delete' | 'conclude', id: string) =>
		armed?.action === action && armed.id === id;

	async function runAction(
		exp: Experiment,
		action: RowActionKind,
		call: (id: string) => Promise<unknown>,
		done: string,
		failed: string
	) {
		if (inFlight) return;
		inFlight = { action, id: exp.id };
		try {
			await call(exp.id);
			toast(done, 'success');
			await load();
		} catch (e) {
			// The server's reason (a 409 "already running", a refusal) is the
			// useful part; the fixed sentence is only the fallback.
			toast(errorMessage(e, failed), 'error');
		} finally {
			inFlight = null;
			// Disarmed only once the request settles, so the busy button keeps
			// reading "Confirm" rather than reverting mid-request.
			armed = null;
		}
	}

	function doStart(exp: Experiment) {
		return runAction(exp, 'start', startExperiment, m('experiments.toast.started'), m('experiments.toast.startFailed'));
	}

	function doStop(exp: Experiment) {
		return runAction(exp, 'stop', stopExperiment, m('experiments.toast.stopped'), m('experiments.toast.stopFailed'));
	}

	function doConclude(exp: Experiment) {
		if (inFlight) return;
		if (!isArmed('conclude', exp.id)) {
			armed = { action: 'conclude', id: exp.id };
			return;
		}
		return runAction(exp, 'conclude', concludeExperiment, m('experiments.toast.concluded'), m('experiments.toast.concludeFailed'));
	}

	function doDelete(exp: Experiment) {
		if (inFlight) return;
		if (!isArmed('delete', exp.id)) {
			armed = { action: 'delete', id: exp.id };
			return;
		}
		return runAction(exp, 'delete', deleteExperiment, m('experiments.toast.deleted'), m('experiments.toast.deleteFailed'));
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
	if (armed && !inFlight && !(e.target as HTMLElement)?.closest('.row-action')) {
		armed = null;
	}
}} />

<PageHeader title={m('experiments.title')}>
	{#snippet actions()}
		{#if canMutate}
			<button class="btn-primary" onclick={openCreate}>{m('experiments.new')}</button>
		{/if}
	{/snippet}

	<p class="lede">{m('experiments.lede')}</p>

	<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} onchange={syncUrl} />

	{#if loadError}
		<!-- Not rendered ALONGSIDE the table: the banner and "No experiments yet."
		     used to appear together, telling the reader two different things at
		     once — and neither offered a way to try again, since the toast-less
		     banner came from a dependency-free `$effect`. -->
		<LoadError message={loadError} onretry={load} testId="experiments-error" />
	{:else}
	<div class="experiment-table">
	<DataTable
		fixed
		columns={COLUMNS}
		isEmpty={filtered.length === 0}
		{loading}
		filtered={experiments.length > 0}
		emptyFiltered={m('experiments.table.emptyFiltered')}
		empty={m('experiments.table.empty')}
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
					<td class="mono">{formatPercent(exp.split_a_pct)} / {formatPercent(100 - exp.split_a_pct)}</td>
					<td class="right mono">{formatCount(exp.assigned_count)}</td>
					<td>
						<Badge tone={STATUS_TONES[exp.status]} variant={exp.status}>
							{statusLabel(exp.status)}
						</Badge>
					</td>
					<td class="actions">
						<!-- Start reads the row's verdict (`actions.start`, from the
						     endpoint's own `start_refusal`); Delete, Stop and Conclude
						     still restate theirs (docs/followups.md). -->
						{#if isAllowed(exp.actions?.start)}
							<RowAction
								variant="success"
								busy={isBusy('start', exp.id)}
								disabled={isHeld('start', exp.id)}
								ariaLabel={m('common.rowActionAria', { action: m('experiments.row.start'), name: exp.name })}
								onclick={() => doStart(exp)}
							>{m('experiments.row.start')}</RowAction>
						{/if}
						{#if canMutate && exp.status === 'draft'}
							<RowAction
								variant="danger"
								armed={isArmed('delete', exp.id)}
								busy={isBusy('delete', exp.id)}
								disabled={isHeld('delete', exp.id)}
								ariaLabel={m('common.rowActionAria', {
									action: isArmed('delete', exp.id) ? m('experiments.row.confirm') : m('experiments.row.delete'),
									name: exp.name
								})}
								onclick={() => doDelete(exp)}
							>
								{isArmed('delete', exp.id) ? m('experiments.row.confirm') : m('experiments.row.delete')}
							</RowAction>
						{:else if canMutate && exp.status === 'running'}
							<RowAction
								busy={isBusy('stop', exp.id)}
								disabled={isHeld('stop', exp.id)}
								ariaLabel={m('common.rowActionAria', { action: m('experiments.row.stop'), name: exp.name })}
								onclick={() => doStop(exp)}
							>{m('experiments.row.stop')}</RowAction>
							<RowAction
								variant="danger"
								armed={isArmed('conclude', exp.id)}
								busy={isBusy('conclude', exp.id)}
								disabled={isHeld('conclude', exp.id)}
								ariaLabel={m('common.rowActionAria', {
									action: isArmed('conclude', exp.id) ? m('experiments.row.confirm') : m('experiments.row.conclude'),
									name: exp.name
								})}
								onclick={() => doConclude(exp)}
							>
								{isArmed('conclude', exp.id) ? m('experiments.row.confirm') : m('experiments.row.conclude')}
							</RowAction>
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>
	</div>
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
		<LoadingState label={m('experiments.results.loading')} />
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
					<tr><td>{m('experiments.results.assigned')}</td><td class="right mono">{formatCount(results.variant_a.assigned_count)}</td><td class="right mono">{formatCount(results.variant_b.assigned_count)}</td></tr>
					<tr><td>{m('experiments.results.completed')}</td><td class="right mono">{formatCount(results.variant_a.completed_count)}</td><td class="right mono">{formatCount(results.variant_b.completed_count)}</td></tr>
					<tr class:primary={results.primary_metric === 'time_to_approval_days'}>
						<td>{m('experiments.results.medianTime')}</td>
						<td class="right mono">{results.variant_a.median_time_to_approval_days}</td>
						<td class="right mono">{results.variant_b.median_time_to_approval_days}</td>
					</tr>
					<tr class:primary={results.primary_metric === 'touchless_rate_pct'}>
						<td>{m('experiments.results.touchlessRate')}</td>
						<td class="right mono">{formatPercent(results.variant_a.touchless_rate_pct)}</td>
						<td class="right mono">{formatPercent(results.variant_b.touchless_rate_pct)}</td>
					</tr>
					<tr class:primary={results.primary_metric === 'exception_rate_pct'}>
						<td>{m('experiments.results.exceptionRate')}</td>
						<td class="right mono">{formatPercent(results.variant_a.exception_rate_pct)}</td>
						<td class="right mono">{formatPercent(results.variant_b.exception_rate_pct)}</td>
					</tr>
					<tr class:primary={results.primary_metric === 'rejection_rate_pct'}>
						<td>{m('experiments.results.rejectionRate')}</td>
						<td class="right mono">{formatPercent(results.variant_a.rejection_rate_pct)}</td>
						<td class="right mono">{formatPercent(results.variant_b.rejection_rate_pct)}</td>
					</tr>
				</tbody>
			</table>
			<p class="hint">{m('experiments.results.hint', { n: results.min_sample_per_variant })}</p>
		</div>
	{/if}
</Modal>

<style>
	/* Column widths. The table is `table-layout: fixed` (DataTable's
	   `fixed`): under `auto` the columns were sized against the one
	   "Loading…" row, then re-sized against the real rows, so every column
	   slid sideways as the data landed (tests-e2e/reactivity/
	   layout-stability.spec.ts). The predictable columns get widths sized to
	   their widest content; the text column(s) carry none and share the rest.
	   Past the `min-width` the table scrolls inside its region
	   (frontend/docs/ui-patterns.md § Data tables). DataTable renders these
	   `<th>` from `columns`, so the selectors go through `:global` under a
	   page-owned wrapper (the /gl-accounts pattern). */
	.experiment-table { min-width: 0; }
	.experiment-table :global(table) { min-width: 1150px; }
	.experiment-table :global(th.col-metric) { width: 190px; }
	.experiment-table :global(th.col-split) { width: 120px; }
	.experiment-table :global(th.col-count) { width: 110px; }
	.experiment-table :global(th.col-status) { width: 130px; }
	.experiment-table :global(th.actions-col) { width: 220px; }

	.lede {
		color: var(--text-muted);
		max-width: 70ch;
		margin: 0;
	}

	/* The status pill is `<Badge>` now — it had re-typed the shared recipe by
	   hand, under classes that named the paint (green/amber/grey) rather than
	   the status. The tone per status lives in `types/experiments`. */
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
