<script lang="ts">
	import { api } from '$lib/api';
	import { m } from '$lib/i18n/store.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import { entityStore } from '$lib/stores/entity.svelte';
	import { isPositiveAmount } from '$lib/utils/money';
	import type { AnalyticsByEntity } from '$lib/types/analytics';

	// Consolidated reporting ACROSS entities — a side-by-side per-entity AP
	// rollup plus a consolidated total (the cross-check). Renders only when the
	// tenant has more than one entity, mirroring the entity switcher's
	// single-entity hide rule. `GET /api/analytics/by-entity` ignores the
	// X-Entity-ID selection by design, so the table is the same regardless of
	// which entity is currently selected in the switcher.

	interface Props {
		/** Trailing window in days (matches the rest of the CFO surface). */
		periodDays?: number;
	}

	let { periodDays = 365 }: Props = $props();

	let data = $state<AnalyticsByEntity | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);

	// Every figure is labelled by the currency the PAYLOAD names for it, never
	// a client-side guess (`docs/decisions.md` §196):
	//
	// - Spend and Outstanding render the `reporting_*` fields in
	//   `reporting_currency` on every row. The rows used to render the naive
	//   `total_spend` / `outstanding_amount` — sums across whatever currencies
	//   an entity's invoices are in — under the entity's configured `currency`,
	//   or `orgCurrency` when it had none: a mixed-currency figure wearing one
	//   currency's symbol. One currency down the column is also what makes the
	//   consolidated row the cross-check it claims to be.
	// - Open POs render BARE. `PurchaseOrder` records no currency, so no code
	//   can be proven for a sum of PO totals, and the server sends none.

	$effect(() => {
		// Register deps so a period change re-fetches.
		void periodDays;
		entityStore.ensureLoaded();
		// Only fetch once we know the tenant is multi-entity — single-entity
		// tenants don't render this section at all.
		if (!entityStore.multiEntity) {
			data = null;
			return;
		}
		load();
	});

	async function load() {
		loading = true;
		error = null;
		try {
			data = await api.get<AnalyticsByEntity>(
				`/api/analytics/by-entity?period_days=${periodDays}`
			);
		} catch (e) {
			error = e instanceof Error ? e.message : m('byEntity.loadFailed');
		} finally {
			loading = false;
		}
	}
</script>

{#if entityStore.multiEntity}
	<div class="chart-card" data-testid="by-entity-section">
		<h2>{m('byEntity.heading')}</h2>
		{#if error}
			<p class="be-error" role="alert">{error}</p>
		{:else if loading && !data}
			<p class="empty">{m('byEntity.loading')}</p>
		{:else if data}
			<DataTable
				columns={[
					{ label: m('byEntity.col.entity') },
					{ label: m('byEntity.col.spend'), class: 'num' },
					{ label: m('byEntity.col.outstanding'), class: 'num' },
					{ label: m('byEntity.col.invoices'), class: 'num' },
					{ label: m('byEntity.col.openExceptions'), class: 'num' },
					{ label: m('byEntity.col.openPos'), class: 'num' }
				]}
			>
				{#snippet body()}
					{#each data?.entities ?? [] as e (e.entity_id)}
						<tr>
							<td>
								{e.entity_name}
								{#if e.is_default}<span class="be-tag">{m('byEntity.tag.default')}</span>{/if}
							</td>
							<td class="num">
								<Money amount={e.reporting_total_spend} currency={e.reporting_currency} mono />
							</td>
							<td class="num">
								<Money
									amount={e.reporting_outstanding_amount}
									currency={e.reporting_currency}
									mono
								/>
							</td>
							<td class="num">{e.invoice_count}</td>
							<td class="num" class:be-alert={e.open_exceptions > 0}>{e.open_exceptions}</td>
							<td class="num"><Money amount={e.open_po_amount} currency={null} mono /></td>
						</tr>
					{/each}
					{#if data?.consolidated}
						{@const c = data.consolidated}
						<tr class="be-total">
							<td>{m('byEntity.consolidated')}</td>
							<td class="num">
								<Money amount={c.reporting_total_spend} currency={c.reporting_currency} mono />
							</td>
							<td class="num">
								<Money
									amount={c.reporting_outstanding_amount}
									currency={c.reporting_currency}
									mono
								/>
							</td>
							<td class="num">{c.invoice_count}</td>
							<td class="num" class:be-alert={c.open_exceptions > 0}>{c.open_exceptions}</td>
							<td class="num"><Money amount={c.open_po_amount} currency={null} mono /></td>
						</tr>
					{/if}
				{/snippet}
			</DataTable>
			<!-- Rows with no locked rate are counted at FACE value in the rollup
			     (`invoice_reporting_amount_sql`), not excluded — so each line says
			     what the column's totals actually hold. -->
			{#if data.consolidated.reporting_total_spend_unconverted_count > 0}
				<p class="be-skipped" role="alert" data-testid="unconverted-spend">
					{m('byEntity.unconvertedSpend', {
						n: data.consolidated.reporting_total_spend_unconverted_count,
						currency: data.consolidated.reporting_currency
					})}
				</p>
			{/if}
			{#if data.consolidated.reporting_outstanding_unconverted_count > 0}
				<p class="be-skipped" role="alert" data-testid="unconverted-outstanding">
					{m('byEntity.unconverted', {
						n: data.consolidated.reporting_outstanding_unconverted_count,
						currency: data.consolidated.reporting_currency
					})}
				</p>
			{/if}
			{#if isPositiveAmount(data.consolidated.open_po_amount)}
				<p class="be-note" data-testid="open-po-no-currency">{m('byEntity.openPoNoCurrency')}</p>
			{/if}
		{/if}
	</div>
{/if}

<style>
	.be-error {
		color: var(--danger);
	}

	.be-skipped {
		color: var(--warning-on-tint);
		font-size: 0.85rem;
		font-weight: 600;
		margin: 10px 0 0;
	}

	.be-note {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 10px 0 0;
	}

	.be-tag {
		margin-left: 6px;
		font-size: 0.72rem;
		color: var(--text-muted);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 0 5px;
		vertical-align: middle;
	}

	.be-alert {
		color: var(--danger);
		font-weight: 600;
	}

	/* The consolidated cross-check row — visually separated as the total. */
	.be-total td {
		border-top: 2px solid var(--border);
		font-weight: 700;
	}
</style>
