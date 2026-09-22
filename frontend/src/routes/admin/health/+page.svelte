<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth } from '$lib/stores/auth.svelte';
	import { formatDate } from '$lib/utils/time';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import type { BadgeTone } from '$lib/components/ui/badgeTone';
	import { getSweepHealth, type SweepHealth, type SweepHealthReport } from '$lib/api/sweepHealth';
	import type { MessageKey } from '$lib/i18n/messages';
	import { m } from '$lib/i18n/store.svelte';

	// RBAC parity with the backend: `GET /api/health/sweeps` is
	// `require_roles(ROLE_ADMIN)`. Wait for `auth.user` to resolve before
	// redirecting so we don't bounce before /me lands (mirrors
	// /admin/api-keys, /admin/webhooks, /admin/access-review).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	let report = $state<SweepHealthReport | null>(null);
	let loading = $state(true);
	let error = $state('');

	// Narrowed out of the possibly-null state so the DataTable `body()` snippet
	// (a closure, which loses the `{:else if report}` narrowing) can read it.
	const sweeps = $derived(report?.sweeps ?? []);

	let COLUMNS = $derived([
		{ label: m('sweepHealth.col.sweep') },
		{ label: m('sweepHealth.col.state') },
		{ label: m('sweepHealth.col.lastRun') },
		{ label: m('sweepHealth.col.outcome') },
		{ label: m('sweepHealth.col.failures') },
		{ label: m('sweepHealth.col.runs') }
	]);

	/** A sweep an operator has to do something about: it died, it was enabled
	 *  but never registered, a tick has hung, or it has failed enough
	 *  consecutive runs to trip the alert streak. `disabled` is not one of
	 *  these — an off flag is an expected state, not a fault. */
	function needsAttention(s: SweepHealth, streak: number): boolean {
		if (s.state === 'died' || s.state === 'not_started' || s.state === 'stalled') return true;
		return streak > 0 && s.consecutive_failures >= streak;
	}

	const alertStreak = $derived(report?.failure_alert_streak ?? 0);
	const attentionCount = $derived(sweeps.filter((s) => needsAttention(s, alertStreak)).length);

	const STATE_TONE: Record<SweepHealth['state'], BadgeTone> = {
		not_started: 'danger',
		died: 'danger',
		stalled: 'warning',
		stopped: 'muted',
		disabled: 'neutral',
		starting: 'accent',
		running: 'accent',
		idle: 'success'
	};

	const STATE_LABEL: Record<SweepHealth['state'], MessageKey> = {
		not_started: 'sweepHealth.state.notStarted',
		died: 'sweepHealth.state.died',
		stalled: 'sweepHealth.state.stalled',
		stopped: 'sweepHealth.state.stopped',
		disabled: 'sweepHealth.state.disabled',
		starting: 'sweepHealth.state.starting',
		running: 'sweepHealth.state.running',
		idle: 'sweepHealth.state.idle'
	};

	const OUTCOME_TONE: Record<string, BadgeTone> = {
		ok: 'success',
		partial: 'warning',
		error: 'danger'
	};

	const OUTCOME_LABEL: Record<string, MessageKey> = {
		ok: 'sweepHealth.outcome.ok',
		partial: 'sweepHealth.outcome.partial',
		error: 'sweepHealth.outcome.error'
	};

	// The backend's `state` / `last_outcome` strings are a small closed
	// vocabulary, but they arrive over JSON — fall back to the raw value rather
	// than rendering a blank cell if the backend ever adds one.
	function stateLabel(state: SweepHealth['state']): string {
		const key = STATE_LABEL[state];
		return key ? m(key) : state;
	}

	function outcomeLabel(outcome: string): string {
		const key = OUTCOME_LABEL[outcome];
		return key ? m(key) : outcome;
	}

	const OVERALL_LABEL: Record<SweepHealthReport['state'], MessageKey> = {
		ok: 'sweepHealth.state.ok',
		degraded: 'sweepHealth.state.degraded',
		failing: 'sweepHealth.state.failing'
	};

	function lastRunLabel(s: SweepHealth): string {
		const at = s.last_run_finished_at ?? s.last_run_started_at;
		if (!at) return m('sweepHealth.never');
		return formatDate(at, m('sweepHealth.never'), { hour: 'numeric', minute: '2-digit' });
	}

	async function load() {
		loading = true;
		error = '';
		try {
			report = await getSweepHealth();
		} catch (e) {
			error = e instanceof Error ? e.message : m('sweepHealth.error');
			report = null;
		} finally {
			loading = false;
		}
	}

	let started = false;
	$effect(() => {
		// Load once, after /me resolves and the role check passes. This read is
		// admin-gated but writes no audit row and touches no tenant DB, so an
		// on-mount load is safe; it is deliberately NOT polled — an operator
		// refreshes when they want a newer sample.
		if (userLoaded && allowed && !started) {
			started = true;
			load();
		}
	});
</script>

<PageHeader title={m('sweepHealth.title')}>
	{#snippet actions()}
		{#if userLoaded && allowed}
			<button class="btn-primary" onclick={load} disabled={loading}>
				{loading ? m('common.loading') : m('sweepHealth.refresh')}
			</button>
		{/if}
	{/snippet}

	<p class="page-hint">{m('sweepHealth.intro')}</p>

	<!-- KPI row — rendered on EVERY state, never gated on the response.
	     `docs/decisions.md` §125: an absent figure is a dash, and whether it is
	     absent because it is still arriving is ANNOUNCED, not drawn. It used to
	     sit inside `{:else if report}` and collapse while the snapshot was in
	     flight.

	     Both tints here are verdicts on an operational control — a green
	     "Overall: OK" and an untinted "0 needing attention" drawn before the
	     snapshot lands are exactly the reassuring-answer-asserted-while-still-
	     asking failure §125 documents. `KpiCard` withholds both while there is
	     no figure, and the derived `attentionCount` / `alertStreak` are
	     `?? 0`-safe, so the guard here is on `report`, not on the derivation. -->
	<div class="kpi-row" data-testid="sweep-health-summary">
		<KpiCard
			value={report ? m(OVERALL_LABEL[report.state]) : null}
			label={m('sweepHealth.kpi.overall')}
			highlight={report ? (report.state === 'ok' ? 'green' : 'red') : null}
			pending={loading}
		/>
		<KpiCard
			value={report ? String(sweeps.length) : null}
			label={m('sweepHealth.kpi.reporting')}
			pending={loading}
		/>
		<KpiCard
			value={report ? String(attentionCount) : null}
			label={m('sweepHealth.kpi.attention')}
			highlight={report && attentionCount > 0 ? 'red' : null}
			pending={loading}
		/>
		<KpiCard
			value={report ? String(report.failure_alert_streak) : null}
			label={m('sweepHealth.kpi.streak')}
			pending={loading}
		/>
	</div>

	{#if loading && report === null}
		<p class="state" data-testid="sweep-health-loading">{m('common.loading')}</p>
	{:else if error}
		<div class="state error" data-testid="sweep-health-error" role="alert">
			<p>{error}</p>
			<button type="button" class="btn-cancel" onclick={load}>{m('sweepHealth.retry')}</button>
		</div>
	{:else if report}
		<DataTable columns={COLUMNS} isEmpty={sweeps.length === 0} empty={m('sweepHealth.empty')}>
			{#snippet body()}
				{#each sweeps as s (s.name)}
					<tr data-testid="sweep-row" data-sweep={s.name} data-state={s.state}>
						<td class="mono">{s.name}</td>
						<td>
							<Badge tone={STATE_TONE[s.state] ?? 'neutral'} variant={`state-${s.state}`}>
								{stateLabel(s.state)}
							</Badge>
						</td>
						<td>{lastRunLabel(s)}</td>
						<td>
							{#if s.last_outcome}
								<Badge
									tone={OUTCOME_TONE[s.last_outcome] ?? 'neutral'}
									variant={`outcome-${s.last_outcome}`}
								>
									{outcomeLabel(s.last_outcome)}
								</Badge>
								{#if s.last_failure_count > 0}
									<span class="sub">{m('sweepHealth.lastFailures', { n: String(s.last_failure_count) })}</span>
								{/if}
								{#if s.last_error_class}
									<!-- Exception CLASS only — never `str(exc)`, which would carry
									     vendor names / account fragments into an operator view. -->
									<span class="sub">{m('sweepHealth.errorClass', { cls: s.last_error_class })}</span>
								{/if}
							{:else}
								<span class="sub">—</span>
							{/if}
							{#if s.exit_error_class}
								<span class="sub danger-text">
									{m('sweepHealth.exitError', { cls: s.exit_error_class })}
								</span>
							{/if}
						</td>
						<td class:danger-text={alertStreak > 0 && s.consecutive_failures >= alertStreak}>
							{s.consecutive_failures}
						</td>
						<td>{m('sweepHealth.runsValue', {
							failed: String(s.total_failed_runs),
							total: String(s.total_runs)
						})}</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{/if}
</PageHeader>

<style>
	.page-hint {
		margin: 0 0 8px;
		color: var(--text-muted);
		font-size: 0.85rem;
		max-width: 900px;
	}

	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}

	.state.error {
		color: var(--danger);
	}

	.kpi-row {
		margin-bottom: 8px;
	}

	.sub {
		display: block;
		color: var(--text-muted);
		font-size: 0.78rem;
		margin-top: 2px;
	}

	.danger-text {
		color: var(--danger);
		font-weight: 600;
	}
</style>
