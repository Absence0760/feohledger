<script lang="ts">
	// Saved cash plans + plan-vs-actual, in the `/cash-flow` side rail.
	//
	// A saved plan is a FROZEN snapshot of one proposal (see
	// docs/cash-flow-copilot.md §5). This panel lists them and, on demand,
	// fetches the comparison against what actually got paid. Everything here is
	// read-only except the delete, which discards the baseline and nothing else.
	import Money from '#lib/components/ui/Money.svelte';
	import { formatDate, formatPeriod } from '#lib/utils/time.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { deleteSavedPlan, getSavedPlanVariance, listSavedPlans } from '#lib/api/cashFlow.ts';
	import type { PlanVarianceResult, SavedPlanSummary } from '#lib/types/cashFlow.ts';

	let {
		consolidated = false
	}: {
		/** Mirrors the page's own consolidated toggle: list every plan in the
		 *  tenant rather than only the selected entity's. */
		consolidated?: boolean;
	} = $props();

	let plans = $state<SavedPlanSummary[]>([]);
	let loading = $state(false);
	let loadError = $state<string | null>(null);

	let openPlanId = $state<string | null>(null);
	let variance = $state<PlanVarianceResult | null>(null);
	let varianceBusy = $state(false);
	let varianceError = $state<string | null>(null);

	let armedDeleteId = $state<string | null>(null);

	/** `scope` is passed in rather than read off the prop so the effect below
	 *  tracks exactly one dependency and can't re-trigger itself. */
	async function load(scope: boolean) {
		loading = true;
		loadError = null;
		try {
			plans = await listSavedPlans({ consolidated: scope });
		} catch {
			loadError = m('cashFlow.saved.loadError');
			plans = [];
		} finally {
			loading = false;
		}
	}

	// Loads on mount, and re-lists whenever the consolidated scope changes —
	// the two scopes answer different questions, so keeping the previous one's
	// rows on screen would be wrong, not merely stale.
	$effect(() => {
		const scope = consolidated;
		void load(scope);
	});

	async function toggle(planId: string) {
		if (openPlanId === planId) {
			openPlanId = null;
			variance = null;
			return;
		}
		openPlanId = planId;
		variance = null;
		varianceError = null;
		varianceBusy = true;
		try {
			variance = await getSavedPlanVariance(planId);
		} catch {
			varianceError = m('cashFlow.saved.varianceError');
		} finally {
			varianceBusy = false;
		}
	}

	async function remove(planId: string) {
		if (armedDeleteId !== planId) {
			armedDeleteId = planId;
			return;
		}
		armedDeleteId = null;
		try {
			await deleteSavedPlan(planId);
			if (openPlanId === planId) {
				openPlanId = null;
				variance = null;
			}
			await load(consolidated);
		} catch {
			loadError = m('cashFlow.saved.deleteError');
		}
	}

	function planTitle(plan: SavedPlanSummary): string {
		return plan.label?.trim() || formatDate(plan.plan_date);
	}
</script>

<section class="saved-panel" data-testid="saved-plans-panel">
	<div class="saved-head">
		<h2 class="saved-title">{m('cashFlow.saved.heading')}</h2>
		<button
			type="button"
			class="saved-refresh"
			onclick={() => void load(consolidated)}
			disabled={loading}
			aria-label={m('cashFlow.saved.refresh')}
		>
			{m('cashFlow.saved.refresh')}
		</button>
	</div>

	{#if loadError}
		<p class="saved-error" role="alert">{loadError}</p>
	{/if}

	{#if loading && plans.length === 0}
		<p class="saved-empty">{m('cashFlow.saved.loading')}</p>
	{:else if plans.length === 0 && !loadError}
		<p class="saved-empty">{m('cashFlow.saved.empty')}</p>
	{:else}
		<ul class="saved-list">
			{#each plans as plan (plan.plan_id)}
				<li class="saved-item" class:open={openPlanId === plan.plan_id}>
					<button
						type="button"
						class="saved-row"
						aria-expanded={openPlanId === plan.plan_id}
						onclick={() => void toggle(plan.plan_id)}
					>
						<span class="saved-name">{planTitle(plan)}</span>
						<span class="saved-meta">
							{formatDate(plan.plan_date)}
							{#if plan.consolidated}
								<span class="saved-tag">{m('cashFlow.saved.consolidatedTag')}</span>
							{/if}
						</span>
					</button>

					{#if openPlanId === plan.plan_id}
						<div class="saved-detail">
							{#if varianceBusy}
								<p class="saved-empty">{m('cashFlow.saved.loading')}</p>
							{:else if varianceError}
								<p class="saved-error" role="alert">{varianceError}</p>
							{:else if variance}
								<p class="saved-totals">
									{#if variance.elapsed_period_count > 0}
										{m('cashFlow.saved.totals', { n: variance.elapsed_period_count })}
										<span class="saved-figures">
											<Money amount={variance.planned_total} currency={variance.currency} mono />
											<span aria-hidden="true">→</span>
											<Money amount={variance.actual_total} currency={variance.currency} mono />
										</span>
									{:else}
										{m('cashFlow.saved.noClosedPeriods')}
									{/if}
								</p>

								{#if variance.periods.length > 0}
									<table class="saved-table">
										<thead>
											<tr>
												<th>{m('cashFlow.saved.col.period')}</th>
												<th class="num">{m('cashFlow.saved.col.planned')}</th>
												<th class="num">{m('cashFlow.saved.col.actual')}</th>
											</tr>
										</thead>
										<tbody>
											{#each variance.periods as p (p.period)}
												<tr class:pending={p.status !== 'elapsed'}>
													<td>
														{formatPeriod(p.period)}
														{#if p.status !== 'elapsed'}
															<span class="saved-status"
																>{m(`cashFlow.saved.status.${p.status}` as never)}</span
															>
														{/if}
													</td>
													<td class="num">
														<Money
															amount={p.planned_outflow}
															currency={variance.currency}
															mono
														/>
													</td>
													<td class="num">
														<Money amount={p.actual_outflow} currency={variance.currency} mono />
													</td>
												</tr>
											{/each}
										</tbody>
									</table>
								{/if}

								{#if variance.undated_payment_count + variance.unconvertible_payment_count > 0}
									<p class="saved-note">
										{m('cashFlow.saved.unplaced', {
											n: variance.undated_payment_count + variance.unconvertible_payment_count,
											currency: variance.currency
										})}
									</p>
								{/if}
								{#if variance.selected_offer_count > 0}
									<p class="saved-note">
										{m('cashFlow.saved.discounts', {
											captured: variance.captured_offer_count,
											selected: variance.selected_offer_count
										})}
									</p>
								{/if}
							{/if}

							<button
								type="button"
								class="saved-delete"
								class:armed={armedDeleteId === plan.plan_id}
								onclick={() => void remove(plan.plan_id)}
							>
								{armedDeleteId === plan.plan_id
									? m('cashFlow.saved.deleteConfirm')
									: m('cashFlow.saved.delete')}
							</button>
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</section>

<style>
	.saved-panel {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding-top: 6px;
		border-top: 1px solid var(--border);
	}
	.saved-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
	}
	.saved-title {
		/* A real heading, so the rail is navigable and the panel's title is
		   addressable by role rather than by a substring that also matches the
		   empty state. Reset the UA margin so the visual header is unchanged. */
		margin: 0;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}
	.saved-refresh {
		border: none;
		background: none;
		padding: 0;
		font-family: inherit;
		font-size: 0.72rem;
		color: var(--text-muted);
		cursor: pointer;
		text-decoration: underline;
	}
	.saved-refresh:hover:not(:disabled) {
		color: var(--accent);
	}
	.saved-refresh:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.saved-empty,
	.saved-error,
	.saved-note {
		margin: 0;
		font-size: 0.78rem;
		line-height: 1.45;
		color: var(--text-muted);
	}
	.saved-error {
		color: var(--danger);
	}
	.saved-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.saved-row {
		width: 100%;
		display: flex;
		flex-direction: column;
		gap: 2px;
		text-align: left;
		padding: 6px 8px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		cursor: pointer;
	}
	.saved-row:hover {
		border-color: var(--accent);
	}
	.saved-item.open .saved-row {
		border-color: var(--accent);
	}
	.saved-name {
		font-size: 0.82rem;
		font-weight: 600;
	}
	.saved-meta {
		font-size: 0.72rem;
		color: var(--text-muted);
		display: flex;
		gap: 6px;
		align-items: center;
	}
	.saved-tag {
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 0 6px;
		font-size: 0.66rem;
	}
	.saved-detail {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 8px 8px 4px;
	}
	.saved-totals {
		margin: 0;
		font-size: 0.78rem;
		line-height: 1.45;
		color: var(--text);
	}
	.saved-figures {
		display: inline-flex;
		gap: 6px;
		align-items: baseline;
	}
	.saved-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.74rem;
	}
	.saved-table th {
		text-align: left;
		font-weight: 600;
		color: var(--text-muted);
		font-size: 0.66rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		padding: 3px 4px;
		border-bottom: 1px solid var(--border);
	}
	.saved-table td {
		padding: 4px;
		border-bottom: 1px solid rgba(128, 128, 128, 0.12);
	}
	.saved-table tr:last-child td {
		border-bottom: none;
	}
	.saved-table .num {
		text-align: right;
	}
	.saved-table tr.pending td {
		color: var(--text-muted);
	}
	.saved-status {
		font-size: 0.66rem;
		color: var(--text-muted);
	}
	.saved-delete {
		align-self: flex-start;
		padding: 4px 10px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-family: inherit;
		font-size: 0.74rem;
		cursor: pointer;
	}
	.saved-delete:hover {
		border-color: var(--danger);
		color: var(--danger);
	}
	.saved-delete.armed {
		border-color: var(--danger);
		background: rgba(240, 70, 70, 0.1);
		color: var(--danger);
	}
</style>
