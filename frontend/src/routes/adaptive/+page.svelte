<script lang="ts">
	/**
	 * `/adaptive` — the UI for the adaptive-workflow read models and their two
	 * act surfaces. Shaped after `/experiments` (the sibling that already had a
	 * page): `PageHeader` + `Tabs` + shared `DataTable` / `Badge` / `KpiCard`,
	 * a role gate that mirrors the backend's, and per-panel loading / error /
	 * empty states rather than one page-wide spinner.
	 *
	 * Three things this page has to get right, all of them backend contracts:
	 *
	 * 1. **Everything here is ADVISORY and deterministic** — no LLM, computed on
	 *    read from the tenant's own approval history. A suggestion is a
	 *    recommendation a human accepts; it is never a change that already
	 *    happened. The standing note says so, and every act is a separate,
	 *    explicit, role-gated button.
	 *
	 * 2. **The threshold apply carries the stale-value guard.** The number on
	 *    screen is sent back as `expected_recommended_threshold`; a 409 means the
	 *    deterministic stats moved while the page was open. That is not a generic
	 *    failure — the guard exists FOR this UI — so it renders as its own
	 *    "the recommendation changed, here is the new one" state, with the
	 *    refreshed recommendation re-read underneath it and nothing applied.
	 *
	 * 3. **The feedback read is audited.** `GET /api/adaptive/feedback` writes an
	 *    `adaptive_feedback.viewed` access-audit row, so it is NEVER polled and
	 *    never fetched speculatively: it loads when the reader opens the Feedback
	 *    tab (once), and again only when they ask for it. Its metrics carry an
	 *    honest insufficient-data state — below the minimum sample there is no
	 *    rate, and this page renders that as its own thing rather than a
	 *    computed-looking number nothing backs.
	 */
	import { goto } from '$app/navigation';
	import { ApiError, api } from '$lib/api';
	import { auth } from '$lib/stores/auth.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import Tabs from '$lib/components/ui/Tabs.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import type { BadgeTone } from '$lib/components/ui/badgeTone';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { formatMoney } from '$lib/utils/money';
	import {
		getApprovalPatterns,
		getAnomalies,
		getSuggestions,
		dismissSuggestion,
		getRoutingSuggestion,
		applyRoutingSuggestion,
		getThresholdRecommendation,
		applyThresholdRecommendation,
		getFeedback,
		type AdaptiveFeedback,
		type AnomalyBatch,
		type ApprovalPatterns,
		type RoutingSuggestion,
		type SuggestionStatus,
		type ThresholdRecommendation,
		type WorkflowSuggestion
	} from '$lib/api/adaptive';

	// RBAC mirrors the backend exactly:
	//   read   → _READ_ROLES            = admin | ap_manager | cfo
	//   dismiss/route-apply → _WRITE_ROLES = admin | ap_manager
	//   threshold apply → _THRESHOLD_APPLY_ROLES = admin (it edits a workflow
	//                                              definition, like PATCH /workflows)
	// Wait for `auth.user` before redirecting so we don't bounce ahead of /me
	// (same shape as /admin/access-review, /admin/api-keys).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.hasAnyRole('admin', 'ap_manager', 'cfo'));
	const canAct = $derived(auth.isManager); // admin | ap_manager
	const canApplyThreshold = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	type TabKey = 'suggestions' | 'threshold' | 'routing' | 'patterns' | 'anomalies' | 'feedback';
	let activeTab = $state<TabKey>('suggestions');

	const TABS = $derived([
		{ key: 'suggestions', label: m('adaptive.tab.suggestions') },
		{ key: 'threshold', label: m('adaptive.tab.threshold') },
		{ key: 'routing', label: m('adaptive.tab.routing') },
		{ key: 'patterns', label: m('adaptive.tab.patterns') },
		{ key: 'anomalies', label: m('adaptive.tab.anomalies') },
		{ key: 'feedback', label: m('adaptive.tab.feedback') }
	]);

	$effect(() => {
		if (userLoaded && allowed) orgCurrency.ensureLoaded();
	});

	// -----------------------------------------------------------------------
	// Suggestions (advisory)
	// -----------------------------------------------------------------------
	let suggestions = $state<WorkflowSuggestion[]>([]);
	let suggestionsLoading = $state(true);
	let suggestionsError = $state<string | null>(null);
	let suggestionStatus = $state<'open' | 'all'>('open');
	let armedDismissId = $state<string | null>(null);

	const SUGGESTION_TONES: Record<SuggestionStatus, 'accent' | 'muted' | 'success' | 'neutral'> = {
		open: 'accent',
		dismissed: 'muted',
		applied: 'success',
		stale: 'neutral'
	};

	const SUGGESTION_STATUS_LABELS = $derived<Record<SuggestionStatus, string>>({
		open: m('adaptive.suggestions.status.open'),
		dismissed: m('adaptive.suggestions.status.dismissed'),
		applied: m('adaptive.suggestions.status.applied'),
		stale: m('adaptive.suggestions.status.stale')
	});

	async function loadSuggestions() {
		suggestionsLoading = true;
		suggestionsError = null;
		try {
			const res = await getSuggestions(suggestionStatus);
			suggestions = res.suggestions;
		} catch {
			suggestionsError = m('adaptive.suggestions.error');
		} finally {
			suggestionsLoading = false;
		}
	}

	async function doDismiss(s: WorkflowSuggestion) {
		if (armedDismissId !== s.id) {
			armedDismissId = s.id;
			return;
		}
		armedDismissId = null;
		try {
			await dismissSuggestion(s.id);
			toast(m('adaptive.suggestions.dismissed'), 'success');
			await loadSuggestions();
		} catch {
			toast(m('adaptive.suggestions.dismissFailed'), 'error');
		}
	}

	// -----------------------------------------------------------------------
	// Auto-approve threshold recommendation + apply (the stale-guard surface)
	// -----------------------------------------------------------------------
	let threshold = $state<ThresholdRecommendation | null>(null);
	let thresholdLoading = $state(true);
	let thresholdError = $state<string | null>(null);
	let applying = $state(false);
	/**
	 * The stale-value guard's own state — deliberately NOT `thresholdError`.
	 * A 409 here is the guard doing its job: the recommendation moved, nothing
	 * was applied, and the refreshed figure is already on screen. Rendering that
	 * as "apply failed" would tell the reader the wrong thing about both what
	 * happened and what to do next.
	 */
	let staleFrom = $state<string | null>(null);

	async function loadThreshold() {
		thresholdLoading = true;
		thresholdError = null;
		try {
			threshold = await getThresholdRecommendation();
		} catch {
			thresholdError = m('adaptive.threshold.error');
		} finally {
			thresholdLoading = false;
		}
	}

	async function doApplyThreshold() {
		if (!threshold) return;
		const sent = threshold.recommended_threshold;
		applying = true;
		staleFrom = null;
		try {
			const res = await applyThresholdRecommendation(sent, threshold.workflow_id);
			if (res.applied) {
				toast(m('adaptive.threshold.applied'), 'success');
			} else {
				toast(m('adaptive.threshold.noop'), 'info');
			}
			await loadThreshold();
		} catch (e) {
			if (e instanceof ApiError && e.status === 409) {
				// The recommendation changed underneath the rendered number.
				// Re-read it so the reader is looking at the current one, and say
				// plainly that nothing was applied.
				staleFrom = sent;
				await loadThreshold();
			} else {
				toast(e instanceof Error ? e.message : m('adaptive.threshold.applyFailed'), 'error');
			}
		} finally {
			applying = false;
		}
	}

	// -----------------------------------------------------------------------
	// Smart routing (advisory ranking of PEOPLE — the "why" is not optional)
	// -----------------------------------------------------------------------
	type PickableInvoice = { id: string; invoice_number: string; vendor_name: string | null };
	let reviewInvoices = $state<PickableInvoice[]>([]);
	let routingInvoiceId = $state<string>('');
	let routing = $state<RoutingSuggestion | null>(null);
	let routingLoading = $state(false);
	let routingError = $state<string | null>(null);
	let routingApplying = $state(false);
	let invoicesLoaded = $state(false);

	async function loadReviewInvoices() {
		try {
			const res = await api.get<{ items: PickableInvoice[] }>(
				'/api/invoices?status=ready_for_review&page=1&page_size=50'
			);
			reviewInvoices = res.items ?? [];
		} catch {
			reviewInvoices = [];
		} finally {
			invoicesLoaded = true;
		}
	}

	async function loadRouting() {
		if (!routingInvoiceId) {
			routing = null;
			return;
		}
		routingLoading = true;
		routingError = null;
		try {
			routing = await getRoutingSuggestion(routingInvoiceId);
		} catch {
			routing = null;
			routingError = m('adaptive.routing.error');
		} finally {
			routingLoading = false;
		}
	}

	async function doApplyRouting() {
		if (!routingInvoiceId) return;
		routingApplying = true;
		try {
			const res = await applyRoutingSuggestion(routingInvoiceId);
			toast(
				res.assigned
					? m('adaptive.routing.applied', { name: res.assigned_to_name ?? res.assigned_to_id })
					: m('adaptive.routing.alreadyAssigned'),
				'success'
			);
			await loadRouting();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('adaptive.routing.applyFailed'), 'error');
		} finally {
			routingApplying = false;
		}
	}

	// -----------------------------------------------------------------------
	// Approval patterns
	// -----------------------------------------------------------------------
	let patterns = $state<ApprovalPatterns | null>(null);
	let patternsLoading = $state(false);
	let patternsError = $state<string | null>(null);
	let patternsLoaded = $state(false);

	async function loadPatterns() {
		patternsLoading = true;
		patternsError = null;
		try {
			patterns = await getApprovalPatterns();
		} catch {
			patternsError = m('adaptive.patterns.error');
		} finally {
			patternsLoading = false;
			patternsLoaded = true;
		}
	}

	// -----------------------------------------------------------------------
	// Anomalies
	// -----------------------------------------------------------------------
	let anomalies = $state<AnomalyBatch | null>(null);
	let anomaliesLoading = $state(false);
	let anomaliesError = $state<string | null>(null);
	let anomaliesLoaded = $state(false);

	async function loadAnomalies() {
		anomaliesLoading = true;
		anomaliesError = null;
		try {
			anomalies = await getAnomalies();
		} catch {
			anomaliesError = m('adaptive.anomalies.error');
		} finally {
			anomaliesLoading = false;
			anomaliesLoaded = true;
		}
	}

	// -----------------------------------------------------------------------
	// Feedback loop — AUDITED READ. Loaded on the explicit act of opening the
	// tab (once), or on the explicit Refresh. Never on a timer, never eagerly.
	// -----------------------------------------------------------------------
	let feedback = $state<AdaptiveFeedback | null>(null);
	let feedbackLoading = $state(false);
	let feedbackError = $state<string | null>(null);
	let feedbackRequested = $state(false);

	async function loadFeedback() {
		feedbackRequested = true;
		feedbackLoading = true;
		feedbackError = null;
		try {
			feedback = await getFeedback();
		} catch {
			feedbackError = m('adaptive.feedback.error');
		} finally {
			feedbackLoading = false;
		}
	}

	// Per-panel lazy loading: the reader pays for the panel they opened. The
	// suggestions + threshold panels back the default tab, so they load with the
	// page; the rest load the first time their tab is opened. `feedback` is the
	// one that must NOT auto-load — see the header note.
	// The suggestions list re-reads when the status filter moves; the threshold
	// does not. Two effects rather than one so a filter change can't drag an
	// unrelated fetch along with it, and so neither dependency set is implicit.
	$effect(() => {
		if (!userLoaded || !allowed) return;
		void suggestionStatus;
		loadSuggestions();
	});

	$effect(() => {
		if (!userLoaded || !allowed) return;
		loadThreshold();
	});

	$effect(() => {
		if (!userLoaded || !allowed) return;
		if (activeTab === 'routing' && !invoicesLoaded) loadReviewInvoices();
		if (activeTab === 'patterns' && !patternsLoaded) loadPatterns();
		if (activeTab === 'anomalies' && !anomaliesLoaded) loadAnomalies();
		if (activeTab === 'feedback' && !feedbackRequested) loadFeedback();
	});

	const SUGGESTION_COLUMNS = $derived([
		{ label: m('adaptive.suggestions.col.suggestion') },
		{ label: m('adaptive.suggestions.col.vendor') },
		{ label: m('adaptive.suggestions.col.confidence'), class: 'right' },
		{ label: m('adaptive.suggestions.col.status') },
		{ class: 'actions-col' }
	]);

	const APPROVER_COLUMNS = $derived([
		{ label: m('adaptive.patterns.col.approver') },
		{ label: m('adaptive.patterns.col.approved'), class: 'right' },
		{ label: m('adaptive.patterns.col.rejected'), class: 'right' },
		{ label: m('adaptive.patterns.col.approvalRate'), class: 'right' },
		{ label: m('adaptive.patterns.col.medianDays'), class: 'right' },
		{ label: m('adaptive.patterns.col.sample'), class: 'right' }
	]);

	const VENDOR_COLUMNS = $derived([
		{ label: m('adaptive.patterns.col.vendor') },
		{ label: m('adaptive.patterns.col.approvalRate'), class: 'right' },
		{ label: m('adaptive.patterns.col.consistency'), class: 'right' },
		{ label: m('adaptive.patterns.col.avgAmount'), class: 'right' },
		{ label: m('adaptive.patterns.col.sample'), class: 'right' }
	]);

	/**
	 * Anomaly-flag severity → `Badge` tone.
	 *
	 * The backend emits `info` and `warning` only, and the old
	 * `severity === 'error' ? 'danger' : 'warning'` ternary therefore painted
	 * EVERY flag as a warning — including the four `info` ones
	 * (`amount_low`, `unusual_approver`, `off_pattern_timing`,
	 * `amount_comparison_unavailable`), the last of which reports the ABSENCE
	 * of a verdict. A tinted badge reads as a signal, so the wrong tint is a
	 * wrong claim: `info` is the accent tint the page already uses for "open,
	 * for your attention", not the amber one that says "this looks wrong".
	 * `error` is mapped defensively so a future severity gets louder rather
	 * than quieter, and an unrecognised one falls to the untinted `neutral`
	 * chip instead of borrowing a signal it hasn't earned (decisions §47).
	 */
	const SEVERITY_TONES: Record<string, BadgeTone> = {
		info: 'accent',
		warning: 'warning',
		error: 'danger'
	};
	const severityTone = (severity: string): BadgeTone => SEVERITY_TONES[severity] ?? 'neutral';

	/**
	 * How many approvals the vendor money columns leave out.
	 *
	 * `avg_approved_amount` is computed over approvals that could be expressed
	 * in the reporting currency; `sample_size` counts every decision. When the
	 * two disagree the average is over fewer rows than the count beside it
	 * claims, so the table says so rather than moving the denominator
	 * (decisions §79/§82 — the same `role="alert"` treatment `/cfo` gives its
	 * unconverted outflows).
	 */
	const vendorUnconvertedTotal = $derived(
		(patterns?.vendors ?? []).reduce((n, v) => n + (v.unconverted_count ?? 0), 0)
	);

	const ANOMALY_COLUMNS = $derived([
		{ label: m('adaptive.anomalies.col.invoice') },
		{ label: m('adaptive.anomalies.col.vendor') },
		{ label: m('adaptive.anomalies.col.amount'), class: 'right' },
		{ label: m('adaptive.anomalies.col.flags') }
	]);
</script>

<svelte:window
	onclick={(e) => {
		if (armedDismissId && !(e.target as HTMLElement)?.closest('.row-action')) armedDismissId = null;
	}}
/>

<PageHeader title={m('adaptive.title')}>
	<p class="lede">{m('adaptive.lede')}</p>
	<p class="advisory" data-testid="adaptive-advisory">{m('adaptive.advisoryNote')}</p>

	<Tabs tabs={TABS} bind:active={activeTab} ariaLabel={m('adaptive.title')} idPrefix="adaptive" />

	<!-- ------------------------------------------------------------------ -->
	{#if activeTab === 'suggestions'}
		<div
			class="panel"
			id="adaptive-panel-suggestions"
			role="tabpanel"
			aria-labelledby="adaptive-tab-suggestions"
		>
			<h2>{m('adaptive.suggestions.heading')}</h2>
			<p class="hint">{m('adaptive.suggestions.intro')}</p>

			<div class="panel-controls">
				<label for="sugg-status">{m('adaptive.suggestions.showLabel')}</label>
				<!-- No `onchange`: the effect above already tracks `suggestionStatus`,
				     and adding one here would fire the same GET twice. -->
				<select id="sugg-status" bind:value={suggestionStatus}>
					<option value="open">{m('adaptive.suggestions.status.open')}</option>
					<option value="all">{m('common.all')}</option>
				</select>
			</div>

			{#if suggestionsError}
				<div class="state error" role="alert" data-testid="adaptive-suggestions-error">
					<p>{suggestionsError}</p>
					<button type="button" class="btn-cancel" onclick={loadSuggestions}
						>{m('adaptive.retry')}</button
					>
				</div>
			{:else}
				<DataTable
					columns={SUGGESTION_COLUMNS}
					isEmpty={suggestions.length === 0}
					empty={suggestionsLoading ? m('common.loading') : m('adaptive.suggestions.empty')}
				>
					{#snippet body()}
						{#each suggestions as s (s.id)}
							<tr data-testid="adaptive-suggestion-row">
								<td>
									<strong>{s.title}</strong>
									{#if s.rationale}<span class="sub">{s.rationale}</span>{/if}
								</td>
								<td>{s.vendor_name}</td>
								<td class="right mono">{s.confidence_pct}%</td>
								<td>
									<Badge tone={SUGGESTION_TONES[s.status]} variant={s.status}>
										{SUGGESTION_STATUS_LABELS[s.status]}
									</Badge>
								</td>
								<td class="actions">
									{#if canAct && s.status === 'open'}
										<RowAction
											variant="danger"
											armed={armedDismissId === s.id}
											onclick={() => doDismiss(s)}
										>
											{armedDismissId === s.id
												? m('adaptive.suggestions.confirm')
												: m('adaptive.suggestions.dismiss')}
										</RowAction>
									{/if}
								</td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>
			{/if}
		</div>

		<!-- ------------------------------------------------------------------ -->
	{:else if activeTab === 'threshold'}
		<div
			class="panel"
			id="adaptive-panel-threshold"
			role="tabpanel"
			aria-labelledby="adaptive-tab-threshold"
		>
			<h2>{m('adaptive.threshold.heading')}</h2>
			<p class="hint">{m('adaptive.threshold.intro')}</p>

			<!-- The stale-value guard's own state. Persistent (not a toast) —
			     the reader has to be able to compare what they sent against what
			     the recommendation became before deciding to apply again. -->
			{#if staleFrom}
				<div class="state changed" role="alert" data-testid="adaptive-threshold-stale">
					<strong>{m('adaptive.threshold.staleHeading')}</strong>
					<p>
						{m('adaptive.threshold.stale', {
							was: formatMoney(staleFrom, { currency: orgCurrency.currency }),
							now: formatMoney(threshold?.recommended_threshold ?? null, {
								currency: orgCurrency.currency
							})
						})}
					</p>
				</div>
			{/if}

			<!-- KPI row — rendered on EVERY state, never gated on the response.
			     `docs/decisions.md` §125: an absent figure is a dash, and whether it
			     is absent because it is still arriving is ANNOUNCED, not drawn. This
			     row used to sit inside `{:else if threshold}` and collapse to nothing
			     while the recommendation was in flight — the same
			     §34-satisfying-but-different mechanism `/cfo` and `/tax` carried at
			     page level, which is why the one convention could not be learned once.

			     `pending` is only consulted when the value is MISSING, so a re-fetch
			     keeps the recommendation already on screen rather than blanking a row
			     somebody is reading; a FAILED load leaves the cards
			     absent-but-not-loading, which is why the error moved UNDER the row
			     rather than replacing it. The green "raise it" verdict drops with the
			     figure — `KpiCard` does that itself, and the `?.` here only keeps the
			     expression from dereferencing a null. -->
			<div class="kpi-row" data-testid="adaptive-threshold-card">
				<KpiCard
					label={m('adaptive.threshold.current')}
					value={threshold
						? formatMoney(threshold.current_threshold, { currency: orgCurrency.currency })
						: null}
					pending={thresholdLoading}
				/>
				<KpiCard
					label={m('adaptive.threshold.recommended')}
					value={threshold
						? formatMoney(threshold.recommended_threshold, {
								currency: orgCurrency.currency
							})
						: null}
					highlight={threshold?.should_raise ? 'green' : null}
					pending={thresholdLoading}
				/>
				<KpiCard
					label={m('adaptive.threshold.cap')}
					value={threshold
						? formatMoney(threshold.cap_threshold, { currency: orgCurrency.currency })
						: null}
					pending={thresholdLoading}
				/>
				<KpiCard
					label={m('adaptive.threshold.qualifyingVendors')}
					value={threshold ? threshold.qualifying_vendor_count : null}
					sub={threshold
						? m('adaptive.threshold.cleanInvoices', { n: threshold.total_clean_invoices })
						: null}
					pending={thresholdLoading}
				/>
			</div>

			{#if thresholdError}
				<div class="state error" role="alert" data-testid="adaptive-threshold-error">
					<p>{thresholdError}</p>
					<button type="button" class="btn-cancel" onclick={loadThreshold}
						>{m('adaptive.retry')}</button
					>
				</div>
			{:else if thresholdLoading && !threshold}
				<p class="state">{m('common.loading')}</p>
			{:else if threshold}
				<p class="rationale" data-testid="adaptive-threshold-rationale">{threshold.rationale}</p>

				{#if threshold.evidence.length}
					<h3>{m('adaptive.threshold.evidence')}</h3>
					<ul class="evidence">
						{#each threshold.evidence as ev (ev.vendor_name)}
							<li>
								<strong>{ev.vendor_name}</strong>
								<span class="sub">
									{m('adaptive.threshold.evidenceBasedOn', { n: ev.based_on_n })} ·
									{m('adaptive.threshold.evidenceMax')}
									<Money amount={ev.max_approved_amount} currency={orgCurrency.currency} /> ·
									{m('adaptive.threshold.evidenceMedian')}
									<Money amount={ev.median_approved_amount} currency={orgCurrency.currency} />
								</span>
							</li>
						{/each}
					</ul>
				{/if}

				<div class="panel-actions">
					{#if !canApplyThreshold}
						<p class="hint" data-testid="adaptive-threshold-admin-only">
							{m('adaptive.threshold.adminOnly')}
						</p>
					{:else if !threshold.workflow_id}
						<p class="hint">{m('adaptive.threshold.noWorkflow')}</p>
					{:else if !threshold.should_raise}
						<p class="hint" data-testid="adaptive-threshold-no-raise">
							{m('adaptive.threshold.noRaise')}
						</p>
					{:else}
						<button
							class="btn-primary"
							data-testid="adaptive-threshold-apply"
							disabled={applying}
							onclick={doApplyThreshold}
						>
							{applying
								? m('adaptive.threshold.applying')
								: m('adaptive.threshold.apply', {
										amount: formatMoney(threshold.recommended_threshold, {
											currency: orgCurrency.currency
										})
									})}
						</button>
						<p class="hint">{m('adaptive.threshold.applyNote')}</p>
					{/if}
				</div>
			{/if}
		</div>

		<!-- ------------------------------------------------------------------ -->
	{:else if activeTab === 'routing'}
		<div class="panel" id="adaptive-panel-routing" role="tabpanel" aria-labelledby="adaptive-tab-routing">
			<h2>{m('adaptive.routing.heading')}</h2>
			<p class="hint">{m('adaptive.routing.intro')}</p>

			<div class="panel-controls">
				<label for="routing-invoice">{m('adaptive.routing.pickInvoice')}</label>
				<select
					id="routing-invoice"
					data-testid="adaptive-routing-invoice"
					bind:value={routingInvoiceId}
					onchange={loadRouting}
				>
					<option value="">{m('adaptive.routing.pickPlaceholder')}</option>
					{#each reviewInvoices as inv (inv.id)}
						<option value={inv.id}>{inv.invoice_number} — {inv.vendor_name ?? '—'}</option>
					{/each}
				</select>
			</div>

			{#if invoicesLoaded && reviewInvoices.length === 0}
				<p class="state">{m('adaptive.routing.noInvoices')}</p>
			{/if}

			{#if routingError}
				<div class="state error" role="alert" data-testid="adaptive-routing-error">
					<p>{routingError}</p>
				</div>
			{:else if routingLoading}
				<p class="state">{m('common.loading')}</p>
			{:else if routing}
				{#if routing.insufficient_history || routing.candidates.length === 0}
					<p class="state" data-testid="adaptive-routing-insufficient">
						{m('adaptive.routing.insufficient')}
					</p>
				{:else}
					<!-- Ranking PEOPLE without saying why is worse than not ranking
					     them, so every candidate carries its forward score, the
					     overturn down-weight, and the sample both are read over. -->
					<ol class="candidates">
						{#each routing.candidates as c (c.approver_id)}
							<li data-testid="adaptive-routing-candidate">
								<div class="cand-head">
									<span class="rank">#{c.rank}</span>
									<strong>{c.approver_name ?? c.approver_id}</strong>
									<span class="score">{c.score}</span>
								</div>
								<p class="sub">
									{m('adaptive.routing.baseScore', { score: c.base_score })} ·
									{#if Number(c.outcome_penalty) > 0}
										{m('adaptive.routing.penalty', {
											points: c.outcome_penalty,
											pct: c.overturn_rate_pct,
											n: c.outcome_sample_size
										})}
									{:else}
										{m('adaptive.routing.noPenalty', { n: c.outcome_sample_size })}
									{/if}
								</p>
								{#if c.reasons.length}
									<p class="sub">{c.reasons.join(' · ')}</p>
								{/if}
							</li>
						{/each}
					</ol>
					<p class="hint">{m('adaptive.routing.explainNote')}</p>
					{#if canAct}
						<div class="panel-actions">
							<button
								class="btn-primary"
								data-testid="adaptive-routing-apply"
								disabled={routingApplying}
								onclick={doApplyRouting}
							>
								{routingApplying ? m('common.saving') : m('adaptive.routing.apply')}
							</button>
						</div>
					{/if}
				{/if}
			{/if}
		</div>

		<!-- ------------------------------------------------------------------ -->
	{:else if activeTab === 'patterns'}
		<div class="panel" id="adaptive-panel-patterns" role="tabpanel" aria-labelledby="adaptive-tab-patterns">
			<h2>{m('adaptive.patterns.heading')}</h2>
			<p class="hint">{m('adaptive.patterns.intro')}</p>

			{#if patternsError}
				<div class="state error" role="alert" data-testid="adaptive-patterns-error">
					<p>{patternsError}</p>
					<button type="button" class="btn-cancel" onclick={loadPatterns}
						>{m('adaptive.retry')}</button
					>
				</div>
			{:else}
				<h3>{m('adaptive.patterns.approvers')}</h3>
				<DataTable
					columns={APPROVER_COLUMNS}
					isEmpty={(patterns?.approvers.length ?? 0) === 0}
					empty={patternsLoading ? m('common.loading') : m('adaptive.patterns.emptyApprovers')}
				>
					{#snippet body()}
						{#each patterns?.approvers ?? [] as a (a.approver_id)}
							<tr>
								<td>{a.approver_name ?? a.approver_id}</td>
								<td class="right mono">{a.approved_count}</td>
								<td class="right mono">{a.rejected_count}</td>
								<td class="right mono">{a.approval_rate_pct}%</td>
								<td class="right mono">{a.median_time_to_approve_days}</td>
								<td class="right mono">{a.sample_size}</td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>

				<h3>{m('adaptive.patterns.vendors')}</h3>
				<!-- "Average approved" is computed over approvals expressible in the
				     reporting currency; "Sample" counts every decision. When some
				     could not be converted the two disagree, and an average over
				     N-minus-k beside a sample count of N is a wrong number. Disclose
				     it — do not quietly move the denominator (decisions §79/§82).
				     Sits ABOVE the figures it qualifies, as on /cfo. The wording
				     is this panel's own key, not the dashboard's borrowed one:
				     that string names the dashboard's OWN excluded figures, while
				     what is excluded here is the average / largest / median, and
				     it carries the COUNT so the reader knows how much of the
				     sample the qualifier covers. -->
				{#if vendorUnconvertedTotal > 0}
					<p class="unconverted" role="alert" data-testid="adaptive-vendor-unconverted">
						{m('adaptive.patterns.unconverted', {
							n: vendorUnconvertedTotal,
							currency: orgCurrency.currency
						})}
					</p>
				{/if}
				<DataTable
					columns={VENDOR_COLUMNS}
					isEmpty={(patterns?.vendors.length ?? 0) === 0}
					empty={patternsLoading ? m('common.loading') : m('adaptive.patterns.emptyVendors')}
				>
					{#snippet body()}
						{#each patterns?.vendors ?? [] as v (v.vendor_name)}
							<tr data-unconverted={v.unconverted_count}>
								<td>{v.vendor_name}</td>
								<td class="right mono">{v.approval_rate_pct}%</td>
								<td class="right mono">{v.consistency_pct}%</td>
								<td class="right mono">
									<Money amount={v.avg_approved_amount} currency={orgCurrency.currency} mono />
								</td>
								<td class="right mono">{v.sample_size}</td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>
			{/if}
		</div>

		<!-- ------------------------------------------------------------------ -->
	{:else if activeTab === 'anomalies'}
		<div
			class="panel"
			id="adaptive-panel-anomalies"
			role="tabpanel"
			aria-labelledby="adaptive-tab-anomalies"
		>
			<h2>{m('adaptive.anomalies.heading')}</h2>
			<p class="hint">{m('adaptive.anomalies.intro')}</p>

			{#if anomaliesError}
				<div class="state error" role="alert" data-testid="adaptive-anomalies-error">
					<p>{anomaliesError}</p>
					<button type="button" class="btn-cancel" onclick={loadAnomalies}
						>{m('adaptive.retry')}</button
					>
				</div>
			{:else}
				{#if anomalies}
					<p class="hint">{m('adaptive.anomalies.scanned', { n: anomalies.total_scanned })}</p>
				{/if}
				<DataTable
					columns={ANOMALY_COLUMNS}
					isEmpty={(anomalies?.flagged.length ?? 0) === 0}
					empty={anomaliesLoading ? m('common.loading') : m('adaptive.anomalies.empty')}
				>
					{#snippet body()}
						{#each anomalies?.flagged ?? [] as a (a.invoice_id)}
							<tr>
								<!-- The id used to be a bare 8-character stub: not openable, not
								     selectable, and not enough to paste anywhere. It is now the
								     same `/invoices?id=` deep link the exceptions queue uses, so
								     the row leads somewhere; the full uuid rides on the title +
								     accessible name (and in the href) for copying. -->
								<td class="mono" title={a.invoice_id}>
									<RowLink
										href="/invoices?id={a.invoice_id}"
										ariaLabel="{m('adaptive.anomalies.col.invoice')} {a.invoice_id}"
									>
										{a.invoice_id.slice(0, 8)}
									</RowLink>
								</td>
								<td>{a.vendor_name}</td>
								<td class="right">
									<!-- `amount_currency`, never the org's reporting currency: when
									     the invoice carries no usable rate lock the backend falls
									     back to the BILLED figure for display, and labelling that
									     with the reporting currency is a wrong number rather than
									     a missing one. The `amount_comparison_unavailable` flag
									     beside it says the amount rules abstained. If the backend
									     can't name a currency at all, the bare exact figure is the
									     honest render — `formatMoney` would otherwise fall back to
									     the platform default and invent a symbol. -->
									{#if a.amount_currency}
										<Money amount={a.amount} currency={a.amount_currency} mono />
									{:else}
										<span class="money mono">{a.amount}</span>
									{/if}
								</td>
								<td>
									{#each a.flags as f (f.code)}
										<div class="flag">
											<Badge tone={severityTone(f.severity)} variant={f.code}>
												{f.code}
											</Badge>
											<span class="sub">{f.message}</span>
										</div>
									{/each}
								</td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>
			{/if}
		</div>

		<!-- ------------------------------------------------------------------ -->
	{:else}
		<div class="panel" id="adaptive-panel-feedback" role="tabpanel" aria-labelledby="adaptive-tab-feedback">
			<h2>{m('adaptive.feedback.heading')}</h2>
			<p class="hint">{m('adaptive.feedback.intro')}</p>
			<p class="hint">{m('adaptive.feedback.auditNote')}</p>

			<!-- Rendered on EVERY state, like the threshold row above — see the
			     comment there for why (`docs/decisions.md` §125). -->
			<div class="kpi-row" data-testid="adaptive-feedback-card">
				<KpiCard
					label={m('adaptive.feedback.autoApproved')}
					value={feedback ? feedback.outcomes.auto_approved_count : null}
					pending={feedbackLoading}
				/>
				<KpiCard
					label={m('adaptive.feedback.overturned')}
					value={feedback ? feedback.outcomes.overturned_count : null}
					pending={feedbackLoading}
				/>
				<!-- The honest insufficient-data state. Below the minimum sample
				     the backend reports NO rate, so neither does this — a dash
				     plus the reason, never a computed-looking 0%.

				     `null`, never a literal em dash: `KpiCard` owns the glyph
				     (`$lib/utils/kpiValue.ts::KPI_NO_FIGURE`, pinned equal to
				     `formatMoney(null)`) and a hand-written one renders
				     `data-kpi-state="value"` — the card CLAIMING a figure
				     exists, which is the opposite of what this branch means
				     and is invisible to the state-based a11y guard
				     (`tests-e2e/a11y/kpi-pending.spec.ts`).

				     One card rather than the two-branch `{#if}` this used to be:
				     with the row now rendering while the response is in flight
				     there are THREE outcomes here (a rate, an
				     insufficient-sample dash, a still-loading dash), and the
				     branch only ever expressed two. `sub` carries the reason, so
				     "no rate because too few decisions" stays distinguishable
				     from "no rate yet" without a second glyph. -->
				<KpiCard
					label={m('adaptive.feedback.overturnRate')}
					value={feedback && !feedback.outcomes.insufficient_data
						? `${feedback.outcomes.overturn_rate_pct}%`
						: null}
					sub={feedback?.outcomes.insufficient_data
						? m('adaptive.feedback.insufficientShort')
						: null}
					pending={feedbackLoading}
				/>
			</div>

			{#if feedbackError}
				<div class="state error" role="alert" data-testid="adaptive-feedback-error">
					<p>{feedbackError}</p>
					<button type="button" class="btn-cancel" onclick={loadFeedback}
						>{m('adaptive.retry')}</button
					>
				</div>
			{:else if feedbackLoading}
				<p class="state">{m('common.loading')}</p>
			{:else if feedback}
				<h3>{m('adaptive.feedback.metrics')}</h3>
				<ul class="metrics">
					{#each feedback.metrics as metric (metric.name)}
						<li data-testid={`adaptive-metric-${metric.name}`}>
							{#if metric.insufficient_data}
								<span class="metric-value muted" data-testid="adaptive-metric-insufficient">
									{m('adaptive.feedback.notMeasurable')}
								</span>
							{:else}
								<span class="metric-value">{metric.value_pct}%</span>
							{/if}
							<span class="sub">{metric.label}</span>
						</li>
					{/each}
				</ul>

				<h3>{m('adaptive.feedback.recommendations')}</h3>
				<div class="rec-pair">
					<div>
						<h4>{m('adaptive.feedback.baseRec')}</h4>
						<p class="mono">
							<Money
								amount={feedback.base_recommendation.recommended_threshold}
								currency={orgCurrency.currency}
								mono
							/>
						</p>
						<p class="sub">{feedback.base_recommendation.rationale}</p>
					</div>
					<div>
						<h4>{m('adaptive.feedback.adjustedRec')}</h4>
						<p class="mono">
							<Money
								amount={feedback.adjusted_recommendation.recommended_threshold}
								currency={orgCurrency.currency}
								mono
							/>
						</p>
						<p class="sub">{feedback.adjusted_recommendation.rationale}</p>
					</div>
				</div>
				{#if feedback.base_recommendation.should_raise && !feedback.adjusted_recommendation.should_raise}
					<p class="hint" data-testid="adaptive-feedback-held-back">
						{m('adaptive.feedback.heldBack')}
					</p>
				{/if}

				<div class="panel-actions">
					<button type="button" class="btn-cancel" onclick={loadFeedback}
						>{m('adaptive.feedback.refresh')}</button
					>
				</div>
			{:else}
				<div class="panel-actions">
					<button type="button" class="btn-primary" onclick={loadFeedback}
						>{m('adaptive.feedback.load')}</button
					>
				</div>
			{/if}
		</div>
	{/if}
</PageHeader>

<style>
	.lede {
		color: var(--text-muted);
		max-width: 70ch;
		margin: 0;
	}
	.advisory {
		color: var(--text-muted);
		max-width: 70ch;
		margin: 6px 0 0;
		font-size: 0.85rem;
		border-left: 3px solid var(--accent);
		padding-left: 10px;
	}
	.panel {
		margin-top: 16px;
	}
	h2 {
		font-size: 1.05rem;
		margin: 0 0 4px;
	}
	h3 {
		font-size: 0.95rem;
		margin: 18px 0 6px;
	}
	h4 {
		font-size: 0.85rem;
		margin: 0 0 4px;
		color: var(--text-muted);
	}
	.hint {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 0 0 8px;
		max-width: 80ch;
	}
	.sub {
		display: block;
		color: var(--text-muted);
		font-size: 0.8rem;
	}
	/* The unconverted-approvals disclosure. Amber and emphatic — this is not a
	   hint, it is the caveat that makes the average beside it readable — and the
	   same token `/cfo` and the by-entity breakdown use for the identical
	   "these figures are a floor" line. */
	.unconverted {
		color: var(--warning-on-tint);
		font-weight: 600;
		font-size: 0.85rem;
		margin: 0 0 8px;
		max-width: 80ch;
	}
	/* The currency-less fallback render — matches `<Money mono>` so a table
	   column doesn't change metrics when one row can't be labelled. */
	.money.mono {
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
		font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
	}
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
	/* The stale-value guard reads as a *changed* state, not a failure — same
	   amber "a human needs to look" register the rest of the app uses. */
	.state.changed {
		background: color-mix(in srgb, var(--warning) 14%, transparent);
		border-left: 3px solid var(--warning);
		padding: 10px 12px;
		border-radius: 6px;
		color: var(--text);
	}
	.state.changed p {
		margin: 4px 0 0;
		font-size: 0.9rem;
	}
	.panel-controls {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 10px 0;
		flex-wrap: wrap;
	}
	.panel-controls label {
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.panel-actions {
		margin-top: 12px;
	}
	.kpi-row {
		display: flex;
		gap: 12px;
		flex-wrap: wrap;
		margin: 10px 0;
	}
	.rationale {
		color: var(--text-muted);
		max-width: 80ch;
		margin: 4px 0 0;
	}
	.evidence,
	.metrics,
	.candidates {
		margin: 0;
		padding-left: 18px;
	}
	.evidence li,
	.metrics li,
	.candidates li {
		margin-bottom: 8px;
	}
	.metrics {
		list-style: none;
		padding-left: 0;
	}
	.metric-value {
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.metric-value.muted {
		font-weight: 500;
		color: var(--text-muted);
	}
	.candidates {
		list-style: none;
		padding-left: 0;
	}
	.cand-head {
		display: flex;
		align-items: baseline;
		gap: 8px;
	}
	.cand-head .rank {
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}
	.cand-head .score {
		margin-left: auto;
		font-variant-numeric: tabular-nums;
		font-weight: 600;
	}
	.rec-pair {
		display: flex;
		gap: 24px;
		flex-wrap: wrap;
	}
	.rec-pair > div {
		flex: 1 1 260px;
	}
	.flag {
		margin-bottom: 4px;
	}
	.mono {
		font-variant-numeric: tabular-nums;
	}
</style>
