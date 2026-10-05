<script lang="ts">
	/**
	 * Supplier-portal home.
	 *
	 * `/portal` used to redirect straight to `/portal/invoices`, so a supplier
	 * had no answer to the two questions they open the portal with — *is
	 * anything waiting on me?* and *where is my money?* (`docs/followups.md`).
	 *
	 * Every figure comes from the whole-set, vendor-scoped `GET
	 * /api/portal/summary`; nothing is re-derived from a loaded page, so the
	 * headline can't contradict the list it links into. Money arrives as an
	 * exact decimal string per currency and is rendered per currency — never
	 * summed across them.
	 */
	import { onMount } from 'svelte';
	import { getPortalSummary, type PortalSummary } from '#lib/portalApi.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import EmptyState from '#lib/components/ui/EmptyState.svelte';
	import Money from '#lib/components/ui/Money.svelte';

	// `change_type` is a backend vocabulary string; it must never render raw in
	// front of a supplier. Typed map so `m()` stays statically checked
	// (frontend/CLAUDE.md — dynamic keys go through a map), with a neutral
	// fallback for a type this build doesn't know.
	const CHANGE_TYPE_KEY = {
		bank_details: 'portal.home.changeType.bankDetails',
		tax_id: 'portal.home.changeType.taxId',
	} as const satisfies Record<string, MessageKey>;

	let summary = $state<PortalSummary | null>(null);
	let loading = $state(true);
	let error = $state('');

	async function load() {
		loading = true;
		error = '';
		try {
			summary = await getPortalSummary();
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.home.loadFailed');
		} finally {
			loading = false;
		}
	}

	onMount(load);

	const hasInvoices = $derived((summary?.invoices_total ?? 0) > 0);
	const needsNothing = $derived(
		!!summary &&
			summary.invoices_action_required === 0 &&
			summary.open_discount_offers === 0 &&
			summary.pending_change === null
	);

	function changeTypeLabel(changeType: string): string {
		const key = (CHANGE_TYPE_KEY as Record<string, MessageKey>)[changeType];
		return m(key ?? 'portal.home.changeType.other');
	}
</script>

<section class="home" aria-busy={loading}>
	<header class="home-header">
		<h1>{m('portal.home.heading')}</h1>
		<p class="subtitle">{m('portal.home.subtitle')}</p>
	</header>

	{#if loading}
		<p class="state">{m('portal.common.loading')}</p>
	{:else if error}
		<!-- Distinct from the empty state: something failed, so offer a retry
		     rather than a first-run call to action. -->
		<div class="state error" role="alert">
			<p>{error}</p>
			<button type="button" class="btn-secondary" onclick={load}>
				{m('portal.home.retry')}
			</button>
		</div>
	{:else if summary && !hasInvoices}
		<EmptyState
			icon="📄"
			heading={m('portal.home.empty.heading')}
			description={m('portal.home.empty.body')}
			actionLabel={m('portal.home.empty.action')}
			actionHref="/portal/invoices"
			testId="portal-home-empty"
		/>
	{:else if summary}
		<div class="kpis" data-testid="portal-home-kpis">
			<KpiCard
				value={summary.invoices_action_required}
				label={m('portal.home.kpi.actionRequired')}
				highlight={summary.invoices_action_required > 0 ? 'red' : null}
			/>
			<KpiCard value={summary.invoices_in_progress} label={m('portal.home.kpi.inProgress')} />
			<KpiCard
				value={summary.invoices_paid}
				label={m('portal.home.kpi.paid')}
				highlight={summary.invoices_paid > 0 ? 'green' : null}
			/>
			<KpiCard value={summary.invoices_completed} label={m('portal.home.kpi.completed')} />
		</div>

		{#if summary.outstanding_by_currency.length > 0}
			<section class="panel" aria-labelledby="portal-home-outstanding">
				<h2 id="portal-home-outstanding">{m('portal.home.outstanding')}</h2>
				<p class="hint">{m('portal.home.outstandingHint')}</p>
				<ul class="totals" data-testid="portal-home-outstanding">
					{#each summary.outstanding_by_currency as row (row.currency)}
						<li>
							<Money amount={row.total} currency={row.currency} mono />
							<span class="muted">{m('portal.home.outstandingCount', { count: row.count })}</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		<div class="actions">
			{#if summary.invoices_action_required > 0}
				<article class="panel attention" data-testid="portal-home-action-required">
					<h2>{m('portal.home.actionRequired.title')}</h2>
					<p>
						{m('portal.home.actionRequired.body', { count: summary.invoices_action_required })}
					</p>
					<!-- `phase` carries the stable phase ID from
					     `#lib/types/portalStatus`, never a display label — the
					     chips used to be keyed by their English text, which a
					     translated portal would have stopped matching. -->
					<a class="btn-primary" href="/portal/invoices?phase=rejected">
						{m('portal.home.actionRequired.cta')}
					</a>
				</article>
			{/if}

			{#if summary.open_discount_offers > 0}
				<article class="panel" data-testid="portal-home-offers">
					<h2>{m('portal.home.offers.title')}</h2>
					<p>{m('portal.home.offers.body', { count: summary.open_discount_offers })}</p>
					<a class="btn-secondary" href="/portal/discount-offers">
						{m('portal.home.offers.cta')}
					</a>
				</article>
			{/if}

			{#if summary.pending_change}
				<article class="panel" data-testid="portal-home-pending-change">
					<h2>{m('portal.home.pendingChange.title')}</h2>
					<p>
						{m('portal.home.pendingChange.body', {
							type: changeTypeLabel(summary.pending_change.change_type),
						})}
					</p>
					<a class="btn-secondary" href="/portal/company">
						{m('portal.home.pendingChange.cta')}
					</a>
				</article>
			{/if}

			{#if needsNothing}
				<article class="panel" data-testid="portal-home-all-clear">
					<h2>{m('portal.home.clear.title')}</h2>
					<p>{m('portal.home.clear.body')}</p>
				</article>
			{/if}
		</div>

		<nav class="links" aria-label={m('portal.home.heading')}>
			<a href="/portal/invoices">{m('portal.home.viewInvoices')}</a>
			<a href="/portal/payments">{m('portal.home.viewPayments')}</a>
		</nav>
	{/if}
</section>

<style>
	.home {
		display: flex;
		flex-direction: column;
		gap: 20px;
		max-width: 1000px;
	}

	.home-header h1 {
		margin: 0;
		font-size: 1.3rem;
	}

	.subtitle {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.state {
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.state.error {
		display: flex;
		align-items: center;
		gap: 12px;
		color: var(--danger);
	}

	.kpis {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 12px;
	}

	.panel {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 16px;
	}

	.panel h2 {
		margin: 0 0 6px;
		font-size: 0.95rem;
	}

	.panel p {
		margin: 0 0 12px;
		font-size: 0.85rem;
		color: var(--text-muted);
		line-height: 1.5;
	}

	.panel.attention {
		border-color: var(--danger);
	}

	.hint {
		margin: 0 0 10px;
	}

	.totals {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-wrap: wrap;
		gap: 20px;
	}

	.totals li {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: 1.05rem;
	}

	.muted {
		font-size: 0.75rem;
		color: var(--text-muted);
	}

	.actions {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
		gap: 12px;
	}

	.actions a {
		text-decoration: none;
		display: inline-block;
	}

	.links {
		display: flex;
		gap: 16px;
		font-size: 0.85rem;
	}

	.links a {
		color: var(--text-muted);
	}

	.links a:hover {
		color: var(--text);
	}
</style>
