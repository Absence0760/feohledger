<script lang="ts">
	import Money from '#lib/components/ui/Money.svelte';
	import { formatMoney } from '#lib/utils/money.ts';
	import { formatDate, formatPeriod } from '#lib/utils/time.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { ApiError } from '#lib/api.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import {
		captureDiscountsFromPlan,
		createDraftRunFromPlan,
		saveCashFlowPlan,
		type CaptureDiscountsResult,
		type DraftRunResult
	} from '#lib/api/cashFlow.ts';
	import type { PaymentPlanResult, SaveCashPlanResult } from '#lib/types/cashFlow.ts';

	let {
		result,
		onsaved
	}: {
		result: PaymentPlanResult;
		/** Fired after a successful save so the page can refresh its saved-plan
		 *  list. Optional — the card works standalone. */
		onsaved?: () => void;
	} = $props();

	function fmt(v: string | null | undefined): string {
		return formatMoney(v, { currency: result.currency, whole: true });
	}

	let selected = $derived(result.discount_recommendations.filter((r) => r.selected));
	let hasBreach = $derived(result.periods.some((p) => p.below_threshold));
	let breachCount = $derived(result.periods.filter((p) => p.below_threshold).length);

	// Phase 3 — draft-only enactment (docs/cash-flow-copilot.md §6/§7). Gated
	// to the same finance-leader roles as the copilot backend (`_COPILOT_ROLES`
	// — admin/ap_manager/cfo, not ap_clerk); defense-in-depth alongside the
	// backend's own RBAC, mirroring how /discounts gates its accept button.
	let canEnact = $derived(auth.isManager || auth.isCfo);

	let draftBusy = $state(false);
	let draftMessage = $state<string | null>(null);
	let draftError = $state<string | null>(null);

	let captureBusy = $state(false);
	let captureArmed = $state(false);
	let captureMessage = $state<string | null>(null);
	let captureError = $state<string | null>(null);

	let saveBusy = $state(false);
	let saveMessage = $state<string | null>(null);
	let saveError = $state<string | null>(null);

	/** A 409 here means the plan's parameters no longer match what the URL's
	 *  plan_id was computed from (edited underlying data, or "today" moved
	 *  on) — the stale-plan guard. Surface a friendly nudge to re-ask instead
	 *  of a raw error. */
	function describeFailure(err: unknown): string {
		if (err instanceof ApiError && err.status === 409) {
			return m('cashFlow.plan.actions.stale');
		}
		const detail = err instanceof Error ? err.message : String(err);
		return m('cashFlow.plan.actions.error', { detail });
	}

	async function handleCreateDraftRun() {
		if (draftBusy) return;
		draftBusy = true;
		draftError = null;
		draftMessage = null;
		try {
			const res: DraftRunResult = await createDraftRunFromPlan(result);
			draftMessage = m(
				res.created
					? 'cashFlow.plan.actions.draftRunCreated'
					: 'cashFlow.plan.actions.draftRunExisting',
				{ n: res.payment_count, amount: fmt(res.total_amount) }
			);
			if (res.requires_cfo_approval) {
				draftMessage = `${draftMessage} ${m('cashFlow.plan.actions.cfoApprovalNote')}`;
			}
		} catch (err) {
			draftError = describeFailure(err);
		} finally {
			draftBusy = false;
		}
	}

	async function handleCaptureDiscounts() {
		if (!captureArmed) {
			captureArmed = true;
			return;
		}
		captureArmed = false;
		if (captureBusy) return;
		captureBusy = true;
		captureError = null;
		captureMessage = null;
		try {
			const res: CaptureDiscountsResult = await captureDiscountsFromPlan(result);
			captureMessage =
				res.accepted_count > 0
					? m('cashFlow.plan.actions.captureResult', { n: res.accepted_count })
					: m('cashFlow.plan.actions.captureNoneNew');
		} catch (err) {
			captureError = describeFailure(err);
		} finally {
			captureBusy = false;
		}
	}

	/** Freeze this proposal so it can be measured against what actually gets
	 *  paid. Read-only over the money path, and no confirm step: saving a
	 *  snapshot is not a decision that can be wrong, and re-saving an
	 *  already-saved plan returns the original rather than overwriting it. */
	async function handleSavePlan() {
		if (saveBusy) return;
		saveBusy = true;
		saveError = null;
		saveMessage = null;
		try {
			const res: SaveCashPlanResult = await saveCashFlowPlan(result);
			saveMessage = m(
				res.created ? 'cashFlow.plan.actions.planSaved' : 'cashFlow.plan.actions.planAlreadySaved'
			);
			onsaved?.();
		} catch (err) {
			saveError = describeFailure(err);
		} finally {
			saveBusy = false;
		}
	}

	function unarmCapture(e: MouseEvent) {
		if (captureArmed && !(e.target as HTMLElement)?.closest?.('.plan-capture-btn')) {
			captureArmed = false;
		}
	}
</script>

<svelte:window onclick={unarmCapture} />

<figure class="plan-card" data-testid="payment-plan-card">
	<figcaption class="plan-cap">
		<span class="plan-title">{m('cashFlow.plan.title')}</span>
		<span class="plan-meta">
			{m('cashFlow.chart.opening', {
				amount: fmt(result.opening_balance),
				source: m(`cashFlow.chart.source.${result.opening_balance_source}` as never)
			})}
		</span>
	</figcaption>

	{#if hasBreach}
		<p class="plan-breach" role="alert">
			{m('cashFlow.chart.breach', { n: breachCount })}
			{#if result.first_shortfall_period}
				<span class="breach-when"
					>{m('cashFlow.chart.firstShortfall', {
						period: formatPeriod(result.first_shortfall_period)
					})}</span
				>
			{/if}
		</p>
	{:else}
		<p class="plan-healthy">{m('cashFlow.chart.ariaHealthy')}</p>
	{/if}

	<p class="plan-savings">
		{#if selected.length > 0}
			{m('cashFlow.plan.savingsSummary', {
				n: selected.length,
				amount: fmt(result.total_savings_selected)
			})}
		{:else}
			{m('cashFlow.plan.noSavings')}
		{/if}
	</p>

	<div class="plan-section">
		<h4 class="plan-subhead">{m('cashFlow.plan.scheduleHeading')}</h4>
		{#if result.periods.length === 0}
			<p class="plan-empty">{m('cashFlow.chart.empty')}</p>
		{:else}
			<table class="mini-table">
				<thead>
					<tr>
						<th>{m('cashFlow.plan.col.period')}</th>
						<th class="num">{m('cashFlow.plan.col.outflow')}</th>
						<th class="num">{m('cashFlow.plan.col.closing')}</th>
					</tr>
				</thead>
				<tbody>
					{#each result.periods as p (p.period)}
						<tr class:below={p.below_threshold}>
							<td>{formatPeriod(p.period)}</td>
							<td class="num"><Money amount={p.outflow} currency={result.currency} mono /></td>
							<td class="num"><Money amount={p.closing} currency={result.currency} mono /></td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>

	{#if selected.length > 0}
		<div class="plan-section">
			<h4 class="plan-subhead">{m('cashFlow.plan.discountsHeading')}</h4>
			<table class="mini-table">
				<thead>
					<tr>
						<th>{m('cashFlow.plan.col.vendor')}</th>
						<th class="num">{m('cashFlow.plan.col.savings')}</th>
						<th>{m('cashFlow.plan.col.payBy')}</th>
					</tr>
				</thead>
				<tbody>
					{#each selected as r (r.offer_id)}
						<tr>
							<td>{r.vendor_name ?? r.invoice_number ?? m('assistant.tool.unknownVendor')}</td>
							<td class="num"><Money amount={r.savings} currency={result.currency} mono /></td>
							<td>{formatDate(r.pay_by)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	{#if result.unretimed_offer_ids.length > 0}
		<p class="plan-note">
			{m('cashFlow.plan.unretimedNote', { n: result.unretimed_offer_ids.length })}
		</p>
	{/if}

	{#if canEnact}
		<div class="plan-actions">
			<button
				type="button"
				class="plan-action-btn"
				disabled={draftBusy}
				onclick={handleCreateDraftRun}
			>
				{draftBusy
					? m('cashFlow.plan.actions.creatingRun')
					: m('cashFlow.plan.actions.createDraftRun')}
			</button>
			{#if selected.length > 0}
				<button
					type="button"
					class="plan-action-btn plan-capture-btn"
					class:armed={captureArmed}
					disabled={captureBusy}
					onclick={handleCaptureDiscounts}
				>
					{captureBusy
						? m('cashFlow.plan.actions.capturingDiscounts')
						: captureArmed
							? m('cashFlow.plan.actions.captureDiscountsConfirm')
							: m('cashFlow.plan.actions.captureDiscounts', { n: selected.length })}
				</button>
			{/if}
			<button type="button" class="plan-action-btn" disabled={saveBusy} onclick={handleSavePlan}>
				{saveBusy ? m('cashFlow.plan.actions.savingPlan') : m('cashFlow.plan.actions.savePlan')}
			</button>
		</div>
		{#if saveMessage}
			<p class="plan-action-result" role="status">{saveMessage}</p>
		{/if}
		{#if saveError}
			<p class="plan-action-error" role="alert">{saveError}</p>
		{/if}
		{#if draftMessage}
			<p class="plan-action-result" role="status">{draftMessage}</p>
		{/if}
		{#if draftError}
			<p class="plan-action-error" role="alert">{draftError}</p>
		{/if}
		{#if captureMessage}
			<p class="plan-action-result" role="status">{captureMessage}</p>
		{/if}
		{#if captureError}
			<p class="plan-action-error" role="alert">{captureError}</p>
		{/if}
	{/if}

	<p class="plan-disclaimer">{m('cashFlow.plan.disclaimer')}</p>
</figure>

<style>
	.plan-card {
		margin: 10px 0 0;
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 14px 16px 12px;
		background: var(--surface);
	}
	.plan-cap {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
		flex-wrap: wrap;
		margin-bottom: 8px;
	}
	.plan-title {
		font-size: 0.82rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text);
	}
	.plan-meta {
		font-size: 0.76rem;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}
	.plan-breach {
		margin: 0 0 10px;
		padding: 8px 12px;
		border-radius: 8px;
		background: rgba(240, 70, 70, 0.1);
		border: 1px solid rgba(240, 70, 70, 0.3);
		color: var(--danger);
		font-size: 0.82rem;
	}
	.breach-when {
		color: var(--text-muted);
	}
	.plan-healthy {
		margin: 0 0 10px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.plan-savings {
		margin: 0 0 12px;
		font-size: 0.88rem;
		font-weight: 600;
		color: var(--text);
	}
	.plan-section {
		margin-bottom: 12px;
	}
	.plan-section:last-of-type {
		margin-bottom: 8px;
	}
	.plan-subhead {
		margin: 0 0 6px;
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	.plan-empty {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 0;
	}
	.mini-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
	}
	.mini-table th {
		text-align: left;
		font-weight: 600;
		color: var(--text-muted);
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		padding: 4px 8px;
		border-bottom: 1px solid var(--border);
	}
	.mini-table td {
		padding: 6px 8px;
		border-bottom: 1px solid rgba(128, 128, 128, 0.12);
	}
	.mini-table tr:last-child td {
		border-bottom: none;
	}
	.mini-table .num {
		text-align: right;
	}
	.mini-table tr.below td {
		color: var(--danger);
	}
	.plan-note {
		margin: 0 0 8px;
		font-size: 0.76rem;
		color: var(--text-muted);
	}
	.plan-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin: 0 0 8px;
	}
	.plan-action-btn {
		padding: 7px 14px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
	}
	.plan-action-btn:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.plan-action-btn:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.plan-capture-btn.armed {
		border-color: var(--danger);
		background: rgba(240, 70, 70, 0.1);
		color: var(--danger);
	}
	.plan-action-result {
		margin: 0 0 6px;
		font-size: 0.78rem;
		color: var(--text);
	}
	.plan-action-error {
		margin: 0 0 6px;
		font-size: 0.78rem;
		color: var(--danger);
	}
	.plan-disclaimer {
		margin: 0;
		font-size: 0.74rem;
		color: var(--text-muted);
		font-style: italic;
	}
</style>
