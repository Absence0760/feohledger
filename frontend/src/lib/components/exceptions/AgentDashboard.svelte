<script lang="ts">
	// Autonomous-exception-agent dashboard: resolution rate, escalation rate,
	// accuracy (placeholder), plus the recent decision log. Read-only surface over
	// GET /api/exceptions/agent-stats + /agent-decisions (admin/ap_manager-gated).
	import { onMount } from 'svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import {
		getAgentStats,
		getAgentDecisions,
		getAgentCandidates,
		runExceptionAgent
	} from '$lib/api/exceptionAgents';
	import {
		agentActionLabelKey,
		autonomyLevelLabelKey,
		type AgentStats,
		type AgentDecision,
		type AgentCandidateException,
		type AgentResolveResult
	} from '$lib/types/exceptionAgents';
	import {
		exceptionStatusLabelKey,
		exceptionTypeFallback,
		exceptionTypeLabelKey
	} from '$lib/types/exception';
	import { appendUnique } from '$lib/utils/pagination';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { formatDate } from '$lib/utils/time';
	import { m } from '$lib/i18n/store.svelte';

	const PAGE_SIZE = 20;

	let stats = $state<AgentStats | null>(null);
	let decisions = $state<AgentDecision[]>([]);
	let total = $state(0);
	let page = $state(1);
	let loading = $state(true);
	let loadingMore = $state(false);
	let actionFilter = $state<string | null>(null);

	// --- The run action -------------------------------------------------------
	// `POST /api/exceptions/{id}/agent-resolve` had no caller anywhere in the
	// app: this dashboard reported on agent activity that could only be
	// triggered outside the product. Reporting on a capability nobody can invoke
	// from here is what made the whole surface read-only theatre.
	let candidates = $state<AgentCandidateException[]>([]);
	let candidatesLoading = $state(true);
	let candidatesErrored = $state(false);
	let runTarget = $state<AgentCandidateException | null>(null);
	let runBusy = $state(false);
	let runError = $state<string | null>(null);
	let runOutcome = $state<AgentResolveResult | null>(null);

	let hasMore = $derived(decisions.length < total);

	// Action-badge tones reuse the exceptions palette (WCAG-passing siblings).
	const ACTION_COLORS: Record<string, string> = {
		auto_resolved: '#1fa86a',
		escalated: '#f06464',
		no_action: '#8a8fa0'
	};

	function pct(rate: number): string {
		return `${(rate * 100).toFixed(1)}%`;
	}

	// Sequences every `loadDecisions` call — mount, action chip, load-more; one
	// counter, latest-issued wins. Without it two fast chip clicks (or a chip
	// click while a load-more is out) let the earlier response land last and
	// publish the previous filter's rows, `total` and `page`. The KPI stats are
	// a one-shot mount read of DIFFERENT state, so they stay unsequenced. This
	// surface never edits a row in place, so no `supersedeInFlight()` call is
	// needed. See `frontend/CLAUDE.md` § Sequencing list fetches.
	const decisionsSequence = createRequestSequencer();

	onMount(load);

	async function load() {
		loading = true;
		try {
			const [s] = await Promise.all([getAgentStats(), loadDecisions(), loadCandidates()]);
			stats = s;
		} catch {
			toast(m('exceptions.agents.loadFailed'), 'error');
		} finally {
			loading = false;
		}
	}

	// The runnable queue is fetched separately from the decision log and keeps
	// its own three states. A failed read must never render as "nothing to run
	// an agent on" — that is a claim about the open exception queue, and the
	// same rule the /exceptions queue itself follows.
	async function loadCandidates() {
		candidatesLoading = true;
		candidatesErrored = false;
		try {
			const data = await getAgentCandidates();
			candidates = data.items;
		} catch {
			candidatesErrored = true;
			candidates = [];
		} finally {
			candidatesLoading = false;
		}
	}

	function openRun(exc: AgentCandidateException) {
		runTarget = exc;
		runError = null;
		runOutcome = null;
	}

	function closeRun() {
		const ran = runOutcome !== null;
		runTarget = null;
		runError = null;
		runOutcome = null;
		// Refresh only after a run actually happened — the decision log, the
		// rates above it and the runnable queue all move together.
		if (ran) void refreshAfterRun();
	}

	async function refreshAfterRun() {
		try {
			stats = await getAgentStats();
		} catch {
			// The rates are a read-only summary; a failed refresh must not
			// overwrite what is on screen with an error state for a run that
			// succeeded. The next mount re-reads them.
		}
		await Promise.all([loadDecisions(), loadCandidates()]);
	}

	async function commitRun() {
		if (!runTarget || runBusy) return;
		runBusy = true;
		runError = null;
		try {
			runOutcome = await runExceptionAgent(runTarget.id);
		} catch (err) {
			// Persistent, not a toast that fades: 409 (already resolved, or lost
			// a race with a concurrent run) and 422 (invoice-less exception) each
			// carry the actionable half of the refusal, and the operator needs it
			// while the dialog is still open.
			runError = err instanceof Error ? err.message : m('exceptions.agents.run.failed');
		} finally {
			runBusy = false;
		}
	}

	async function loadDecisions(opts: { append?: boolean; nextPage?: number } = {}) {
		const nextPage = opts.nextPage ?? 1;
		const token = decisionsSequence.start();
		if (opts.append) loadingMore = true;
		try {
			const data = await getAgentDecisions({
				actionTaken: actionFilter ?? undefined,
				page: nextPage,
				pageSize: PAGE_SIZE
			});
			// Superseded by a newer load — discard rather than clobber.
			if (!decisionsSequence.canCommit(token)) return;
			decisions = opts.append ? appendUnique(decisions, data.items) : data.items;
			total = data.total;
			page = nextPage;
		} catch {
			// `isCurrentRequest`, not `canCommit`: only the newest request reports.
			if (!decisionsSequence.isCurrentRequest(token)) return;
			if (!opts.append) toast(m('exceptions.agents.logLoadFailed'), 'error');
		} finally {
			if (decisionsSequence.isCurrentRequest(token)) loadingMore = false;
		}
	}

	async function loadMore() {
		await loadDecisions({ append: true, nextPage: page + 1 });
	}

	function setActionFilter(action: string | null) {
		if (actionFilter === action) return;
		actionFilter = action;
		loadDecisions();
	}

	/** An exception with no invoice has nothing for an agent to act on — the
	 *  backend 422s it (a Positive Pay `not_on_file` fraud return is the case).
	 *  Shown with a disabled Run and the reason rather than hidden, so the queue
	 *  the panel lists stays the queue the queue page shows. */
	function isRunnable(exc: AgentCandidateException): boolean {
		return exc.invoice_id !== null;
	}

	function changeSummary(d: AgentDecision): string {
		if (!d.changes) return '—';
		return Object.entries(d.changes)
			.map(([field, v]) => `${field}: ${v.old || '∅'} → ${v.new}`)
			.join(', ');
	}

	/** An agent action's label, falling back to the raw value for an action this
	 *  build has no wording for (`action_taken` is a plain `String(20)`). */
	function actionLabel(action: string): string {
		const key = agentActionLabelKey(action);
		return key ? m(key) : action;
	}

	/** An autonomy level's label. The log row and the run dialog both printed the
	 *  raw lowercase wire value; an unknown level still does, rather than blank. */
	function autonomyLabel(level: string): string {
		const key = autonomyLevelLabelKey(level);
		return key ? m(key) : level;
	}

	/**
	 * An exception type's label.
	 *
	 * The decision log has only `exception_type` on the wire and rendered it as
	 * `replace(/_/g, ' ')` — `po mismatch`: an English-only derivation, and a
	 * DIFFERENT wording from the queue's own `PO Mismatch` one tab away. The
	 * runnable queue does carry the server's `type_label`, so that is the fallback
	 * where it exists; otherwise the de-underscored raw key, never an empty cell
	 * (`types/exception.ts` states the rule).
	 */
	function typeLabel(type: string, serverLabel?: string | null): string {
		const key = exceptionTypeLabelKey(type);
		if (key) return m(key);
		return serverLabel || exceptionTypeFallback(type);
	}

	/**
	 * A lifecycle status' label — the runnable-queue cell and the run dialog's
	 * outcome line, which both printed the raw wire value.
	 *
	 * Reads the SAME keys the /exceptions queue's status chips and badge read
	 * (`types/exception.ts::EXCEPTION_STATUS_LABEL_KEYS`), which is the point:
	 * this panel is a tab of that page, so a second wording here would name one
	 * status two ways in one page. Unknown status prints raw, not blank.
	 */
	function statusLabel(status: string): string {
		const key = exceptionStatusLabelKey(status);
		return key ? m(key) : status;
	}

	// `$derived`, not `const`: these read `m()`, so a locale switch has to rebuild
	// them or the headers stay in the language the panel happened to mount in.
	const COLUMNS = $derived([
		{ label: m('exceptions.agents.col.when') },
		{ label: m('exceptions.agents.col.resolver') },
		{ label: m('exceptions.agents.col.exception') },
		{ label: m('exceptions.agents.col.action') },
		{ label: m('exceptions.agents.col.confidence'), class: 'right' },
		{ label: m('exceptions.agents.col.autonomy') },
		{ label: m('exceptions.agents.col.change') }
	]);

	const RUN_COLUMNS = $derived([
		{ label: m('exceptions.agents.col.raised') },
		{ label: m('exceptions.agents.col.exception') },
		{ label: m('exceptions.col.invoice') },
		{ label: m('exceptions.col.vendor') },
		{ label: m('exceptions.col.status') },
		{ label: '' }
	]);

	// The three action chips ARE the three action labels, so they read the same
	// keys the badge does — a chip saying one thing and the rows it filters
	// another is the drift one shared map removes.
	const ACTION_CHIPS = $derived([
		{ key: null, label: m('common.all') },
		{ key: 'auto_resolved', label: m('exceptions.agents.action.autoResolved') },
		{ key: 'escalated', label: m('exceptions.agents.action.escalated') },
		{ key: 'no_action', label: m('exceptions.agents.action.noAction') }
	]);
</script>

<div class="agent-dash" data-testid="agent-dashboard">
	<!-- KPI row — rendered on EVERY state, never gated on the response
	     (`docs/decisions.md` §125). It used to sit inside `{#if stats}` and
	     collapse while the stats request was in flight; the green "Resolution
	     rate" tint made that worse than a shape change, since `highlight` was
	     unconditional and the card would have painted a verdict on a figure
	     nobody had computed the moment it appeared. `KpiCard` withholds the tint
	     while there is no figure.

	     `load()` toasts on failure and leaves `stats` null, so a failed read is
	     an `unavailable` row — dashes, not busy — beside the toast that says
	     why, rather than a section that silently isn't there. -->
	<div class="kpi-row">
		<KpiCard
			value={stats ? stats.total_decisions : null}
			label={m('exceptions.agents.kpi.decisions')}
			pending={loading}
		/>
		<KpiCard
			value={stats ? pct(stats.resolution_rate) : null}
			label={m('exceptions.agents.kpi.resolutionRate')}
			highlight="green"
			pending={loading}
		/>
		<KpiCard
			value={stats ? pct(stats.escalation_rate) : null}
			label={m('exceptions.agents.kpi.escalationRate')}
			pending={loading}
		/>
		<!-- The two count cards carry the ACTION labels rather than labels of their
		     own: the card, the filter chip and the row badge name one action. -->
		<KpiCard
			value={stats ? stats.auto_resolved : null}
			label={m('exceptions.agents.action.autoResolved')}
			pending={loading}
		/>
		<KpiCard
			value={stats ? stats.escalated : null}
			label={m('exceptions.agents.action.escalated')}
			pending={loading}
		/>
	</div>

	{#if stats}
		<!-- Accuracy is a placeholder pending a human-overturn signal — never
		     fabricate a number; show the explicit deferred state. -->
		<div class="accuracy-card" data-testid="agent-accuracy">
			<div class="accuracy-head">
				<span class="accuracy-label">{m('exceptions.agents.accuracy.label')}</span>
				<span class="accuracy-value">
					{stats.accuracy === null
						? m('exceptions.agents.accuracy.notMeasured')
						: pct(stats.accuracy)}
				</span>
			</div>
			{#if stats.accuracy === null}
				<p class="accuracy-note">{m('exceptions.agents.accuracy.note')}</p>
			{/if}
		</div>
	{:else if loading}
		<p class="dash-loading">{m('exceptions.agents.loading')}</p>
	{/if}

	<section class="log-section" data-testid="agent-run-panel">
		<header class="log-head">
			<h2>{m('exceptions.agents.run.heading')}</h2>
			<button class="filter-chip" onclick={loadCandidates} disabled={candidatesLoading}>
				{candidatesLoading
					? m('exceptions.agents.run.refreshing')
					: m('exceptions.agents.run.refresh')}
			</button>
		</header>
		<!-- What a run actually does, said before the button rather than after
		     it. The coordinator applies a fix only when the resolver's confidence
		     clears the org's autonomy threshold; otherwise it hands the exception
		     to a human. Both paths record one append-only decision. -->
		<p class="run-note">{m('exceptions.agents.run.note')}</p>

		<DataTable
			columns={RUN_COLUMNS}
			isEmpty={candidates.length === 0}
			empty={candidatesLoading
				? m('exceptions.agents.run.empty.loading')
				: candidatesErrored
					? m('exceptions.agents.run.empty.errored')
					: m('exceptions.agents.run.empty.none')}
			colspan={6}
		>
			{#snippet body()}
				{#each candidates as exc (exc.id)}
					<tr>
						<td class="muted-cell" title={exc.created_at}>{formatDate(exc.created_at)}</td>
						<td>{typeLabel(exc.exception_type, exc.type_label)}</td>
						<td class="mono">{exc.invoice_number ?? '—'}</td>
						<td class="muted-cell">{exc.vendor_name ?? '—'}</td>
						<td class="muted-cell">{statusLabel(exc.status)}</td>
						<td class="right">
							{#if isRunnable(exc)}
								<RowAction
									variant="accent"
									ariaLabel={exc.invoice_number
										? m('exceptions.agents.run.ariaWithInvoice', {
												type: typeLabel(exc.exception_type, exc.type_label),
												invoice: exc.invoice_number
											})
										: m('exceptions.agents.run.aria', {
												type: typeLabel(exc.exception_type, exc.type_label)
											})}
									onclick={() => openRun(exc)}
								>
									{m('exceptions.agents.run.action')}
								</RowAction>
							{:else}
								<!-- Disabled with the reason attached, not omitted: an
								     invoice-less exception is human triage only and the
								     backend 422s it. A missing button explains nothing. -->
								<RowAction
									disabled
									title={m('exceptions.agents.run.blockedTitle')}
									ariaLabel={m('exceptions.agents.run.blockedAria', {
										type: typeLabel(exc.exception_type, exc.type_label)
									})}
								>
									{m('exceptions.agents.run.action')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	</section>

	<section class="log-section" data-testid="agent-decision-log">
		<header class="log-head">
			<h2>{m('exceptions.agents.log.heading')}</h2>
			<nav class="filters" aria-label={m('exceptions.agents.log.filterAria')}>
				{#each ACTION_CHIPS as chip (chip.key ?? 'all')}
					<button
						class="filter-chip"
						class:active={actionFilter === chip.key}
						aria-pressed={actionFilter === chip.key}
						onclick={() => setActionFilter(chip.key)}
					>
						{chip.label}
					</button>
				{/each}
			</nav>
		</header>

		<DataTable
			columns={COLUMNS}
			isEmpty={decisions.length === 0 && !loading}
			empty={m('exceptions.agents.log.empty')}
			colspan={7}
		>
			{#snippet body()}
				{#each decisions as d (d.id)}
					<tr>
						<td class="muted-cell" title={d.created_at}>{formatDate(d.created_at)}</td>
						<td class="mono">{d.agent_type}</td>
						<td class="muted-cell">{typeLabel(d.exception_type)}</td>
						<td>
							<span
								class="action-badge"
								style="background:{ACTION_COLORS[d.action_taken] ?? '#888'}1f;color:{ACTION_COLORS[d.action_taken] ?? '#888'}"
							>
								{actionLabel(d.action_taken)}
							</span>
						</td>
						<td class="mono right">{(d.confidence * 100).toFixed(0)}%</td>
						<td class="muted-cell">{autonomyLabel(d.autonomy_level)}</td>
						<td class="muted-cell change-cell" title={d.rationale ?? ''}>{changeSummary(d)}</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>

		{#if hasMore}
			<div class="load-more-row">
				<button class="btn-load-more" onclick={loadMore} disabled={loadingMore}>
					{loadingMore
						? m('common.loading')
						: m('exceptions.agents.loadMore', { shown: decisions.length, total })}
				</button>
			</div>
		{:else if total > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('exceptions.agents.showingAll', { total })}</span>
			</div>
		{/if}
	</section>
</div>

<!-- Confirm-then-act: a run can MUTATE the invoice (the resolver applies its fix
     through the same audited path a human would), so it is never a bare click.
     The outcome is then rendered from the RESPONSE — `escalated` and
     `no_action` are outcomes of a successful run, not failures. -->
<Modal
	open={runTarget !== null}
	ariaLabel={m('exceptions.agents.modal.aria')}
	title={runOutcome
		? m('exceptions.agents.modal.titleDecision')
		: m('exceptions.agents.modal.titleRun')}
	onclose={closeRun}
>
	{#if runTarget}
		<p class="modal-hint">
			<strong>{typeLabel(runTarget.exception_type, runTarget.type_label)}</strong>
			{#if runTarget.invoice_number}· <span class="mono">{runTarget.invoice_number}</span>{/if}
			{#if runTarget.vendor_name}· {runTarget.vendor_name}{/if}
		</p>

		{#if runOutcome}
			<!-- Every outcome renders the same way. Escalation is what the autonomy
			     threshold is FOR: presenting it as a failure would teach operators
			     that the safe path is the broken one. -->
			<div class="run-outcome" data-testid="agent-run-outcome">
				<span
					class="action-badge"
					data-testid="agent-run-action"
					style="background:{ACTION_COLORS[runOutcome.decision.action_taken] ?? '#888'}1f;color:{ACTION_COLORS[runOutcome.decision.action_taken] ?? '#888'}"
				>
					{actionLabel(runOutcome.decision.action_taken)}
				</span>
				<!-- The status reads the queue's own status keys, so the outcome line
				     and the badge the run just changed name it identically. -->
				<span class="run-outcome-status" data-testid="agent-run-status">
					{m('exceptions.agents.modal.nowStatus')}
					<strong>{statusLabel(runOutcome.exception.status)}</strong>
				</span>
			</div>

			{#if runOutcome.decision.action_taken === 'escalated'}
				<p class="run-note" data-testid="agent-run-escalated-note">
					{m('exceptions.agents.modal.escalatedNote')}
				</p>
			{:else if runOutcome.decision.action_taken === 'no_action'}
				<p class="run-note" data-testid="agent-run-no-action-note">
					{m('exceptions.agents.modal.noActionNote')}
				</p>
			{/if}

			<dl class="run-facts" data-testid="agent-run-facts">
				<div>
					<dt>{m('exceptions.agents.col.resolver')}</dt>
					<dd class="mono">{runOutcome.decision.agent_type}</dd>
				</div>
				<div>
					<dt>{m('exceptions.agents.col.confidence')}</dt>
					<dd class="mono">{(runOutcome.decision.confidence * 100).toFixed(0)}%</dd>
				</div>
				<div>
					<dt>{m('exceptions.agents.col.autonomy')}</dt>
					<dd>{autonomyLabel(runOutcome.decision.autonomy_level)}</dd>
				</div>
			</dl>

			{#if runOutcome.decision.rationale}
				<p class="run-rationale" data-testid="agent-run-rationale">
					{runOutcome.decision.rationale}
				</p>
			{/if}
			{#if runOutcome.decision.changes}
				<p class="run-changes" data-testid="agent-run-changes">
					{changeSummary(runOutcome.decision)}
				</p>
			{/if}

			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={closeRun}>
					{m('exceptions.agents.modal.close')}
				</button>
			</div>
		{:else}
			<p class="modal-warn" data-testid="agent-run-warning">
				{m('exceptions.agents.modal.warning')}
			</p>
			{#if runError}
				<p class="state error" role="alert" data-testid="agent-run-error">{runError}</p>
			{/if}
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={closeRun}>
					{m('common.cancel')}
				</button>
				<button
					type="button"
					class="btn-primary"
					data-testid="agent-run-confirm"
					disabled={runBusy}
					onclick={commitRun}
				>
					{runBusy
						? m('exceptions.agents.run.running')
						: m('exceptions.agents.run.action')}
				</button>
			</div>
		{/if}
	{/if}
</Modal>

<style>
	.agent-dash {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.dash-loading {
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.accuracy-card {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 14px 16px;
		background: var(--surface);
	}

	.accuracy-head {
		display: flex;
		align-items: baseline;
		gap: 12px;
	}

	.accuracy-label {
		font-size: 0.8rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.accuracy-value {
		font-size: 1.1rem;
		font-weight: 600;
		color: var(--text);
	}

	.accuracy-note {
		margin: 8px 0 0;
		font-size: 0.8rem;
		color: var(--text-muted);
		max-width: 60ch;
	}

	.log-section {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.log-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: 8px;
	}

	.log-head h2 {
		margin: 0;
		font-size: 1rem;
	}

	.action-badge {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 10px;
		font-size: 0.72rem;
		font-weight: 600;
		white-space: nowrap;
	}

	.muted-cell {
		color: var(--text-muted);
	}

	.change-cell {
		max-width: 280px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.run-note {
		margin: 0;
		font-size: 0.82rem;
		color: var(--text-muted);
		max-width: 78ch;
	}

	.run-outcome {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
		margin-bottom: 10px;
	}

	.run-outcome-status {
		font-size: 0.85rem;
		color: var(--text-muted);
	}

	.run-facts {
		display: flex;
		gap: 20px;
		flex-wrap: wrap;
		margin: 12px 0 0;
	}

	.run-facts dt {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.run-facts dd {
		margin: 2px 0 0;
		font-size: 0.9rem;
		color: var(--text);
	}

	.run-rationale,
	.run-changes {
		margin: 12px 0 0;
		font-size: 0.85rem;
		color: var(--text);
	}

	.run-changes {
		color: var(--text-muted);
		font-family: var(--font-mono);
	}

	/* Amber-neutral, not danger-red: a run is a normal operation that may change
	   the invoice — the box states a consequence, it does not report an error. */
	.modal-warn {
		font-size: 0.82rem;
		color: var(--text);
		margin: 0 0 14px;
		padding: 10px 12px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: 4px;
	}

	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}

	.state.error {
		color: var(--danger);
	}
</style>
