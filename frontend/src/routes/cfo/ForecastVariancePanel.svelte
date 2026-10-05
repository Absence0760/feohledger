<script lang="ts">
	import { auth } from '#lib/stores/auth.svelte.ts';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatPeriod } from '#lib/utils/time.ts';
	import { postForecastVariance } from '#lib/api/analytics.ts';
	import type { ForecastVariance } from '#lib/types/analytics.ts';
	import {
		collectForecastEntries,
		unconvertedTotal,
		variancePctLabel,
		varianceTone,
		type ForecastEntry
	} from './forecastVarianceSummary';

	/**
	 * Forecast vs actual — the entry + read surface for
	 * `POST /api/analytics/forecast_variance`.
	 *
	 * That endpoint shipped complete and disclosure-correct with **no UI at
	 * all**, so its `unconverted_count` — the fact that makes a variance
	 * readable as the floor it is — had no consumer. This is that surface, and
	 * the disclosure is why it is shaped the way it is rather than as a bare
	 * table of numbers.
	 *
	 * **A POST with a body, so it needs a form.** The forecast is the CFO's own
	 * figure set, pasted from their FP&A tool, and the backend deliberately
	 * persists nothing — there is no saved forecast to GET. Every visit starts
	 * from an empty editor, which is honest about what the API stores.
	 *
	 * **Self-contained, like `ScheduledReportsPanel` / `ByEntityBreakdown`.**
	 * The host page passes nothing and this owns its own request, so the
	 * cash-flow panels' controls neither drive it nor can take it down: it
	 * renders OUTSIDE their `{#if}`, the same reasoning as the budget rollup.
	 *
	 * **RBAC.** admin + CFO — `_CFO_ROLES` on the endpoint, which is also
	 * exactly this route's own gate. `auth.isCfo` is `admin | cfo`.
	 */

	let entries = $state<ForecastEntry[]>([{ month: '', forecast: '' }]);
	let open = $state(false);
	let submitting = $state(false);
	let result = $state<ForecastVariance | null>(null);
	let loadError = $state<string | null>(null);

	// A count folded across months, which is safe because it is a COUNT — the
	// amounts beside it are not added across anything.
	const unconverted = $derived(unconvertedTotal(result));

	function addRow() {
		entries = [...entries, { month: '', forecast: '' }];
	}

	function removeRow(idx: number) {
		entries = entries.filter((_, i) => i !== idx);
		if (entries.length === 0) addRow();
	}

	async function submit() {
		// Validated ONCE, here, at submit — never on every keystroke, and never
		// repaired. The typed text is the exact decimal string that goes on the
		// wire; an amount we cannot read is refused with a toast rather than
		// coerced to `0`, which would make the variance equal the whole actual
		// outflow and report a fabricated 0%.
		const collected = collectForecastEntries(entries);
		if (!collected.ok) {
			toast(
				collected.reason === 'amount'
					? m('common.amountInvalid')
					: collected.reason === 'month'
						? m('cfo.forecastVariance.errorMonth')
						: m('cfo.forecastVariance.errorEmpty'),
				'error'
			);
			return;
		}
		submitting = true;
		loadError = null;
		try {
			result = await postForecastVariance(collected.rows);
			open = false;
		} catch (e) {
			// The backend's own explanation (a 422 naming the bad month, a 403)
			// is the actionable half of the refusal, so it is surfaced verbatim.
			loadError = e instanceof Error ? e.message : m('cfo.forecastVariance.loadFailed');
			toast(loadError, 'error');
		} finally {
			submitting = false;
		}
	}
</script>

{#if auth.isCfo}
	<div class="chart-card" data-testid="forecast-variance">
		<div class="fv-head">
			<h2>{m('cfo.forecastVariance.title')}</h2>
			<button class="btn-primary" onclick={() => (open = true)} data-testid="forecast-variance-open">
				{result ? m('cfo.forecastVariance.edit') : m('cfo.forecastVariance.enter')}
			</button>
		</div>
		<p class="fv-hint">{m('cfo.forecastVariance.hint')}</p>

		{#if result || submitting}
			<!-- The disclosure sits ABOVE the amounts it qualifies, not in a
			     tooltip: a completed payment whose outflow cannot be expressed in
			     the reporting currency is EXCLUDED from `actual` rather than added
			     at face value, so a non-zero count means every actual — and every
			     variance derived from one — is a FLOOR (decisions §35). Same
			     `role="alert"` treatment as the cash-position card's unconverted
			     outflows and the budget rollup's excluded rows. -->
			{#if result && unconverted > 0}
				<p class="cf-skipped" role="alert" data-testid="forecast-variance-unconverted">
					{m('cfo.forecastVariance.unconverted', {
						n: unconverted,
						currency: result.reporting_currency
					})}
				</p>
			{/if}
			<!-- Rendered from the moment a comparison is SUBMITTED, not from the
			     moment it answers — `docs/decisions.md` §125, with the same
			     adjustment `/audit`'s verification counts needed. The backend
			     persists no forecast, so there is nothing to fetch on mount and
			     nothing to report until the CFO submits one; the row is gated on
			     `result || submitting` for that reason rather than rendered
			     unconditionally, and the `{:else}` branch below it stays the
			     genuine "you have not entered a forecast" state.

			     `submitting` is what carries it through the wait: the modal closes
			     only once the POST lands, so a slow variance used to leave the
			     panel on its empty state — "no forecast entered" asserted while
			     one was in flight — and then pop a row in. `pending` is consulted
			     only for a missing figure, so re-submitting an edited forecast
			     keeps the previous answer on screen. -->
			<div class="kpi-row" data-testid="forecast-variance-kpis">
				<KpiCard
					value={result ? String(result.rows.length) : null}
					label={m('cfo.forecastVariance.kpiMonths')}
					pending={submitting}
				/>
				<KpiCard
					value={result ? result.reporting_currency : null}
					label={m('cfo.forecastVariance.kpiCurrency')}
					pending={submitting}
				/>
			</div>
			{#if result}
				<DataTable
					columns={[
						{ label: m('cfo.forecastVariance.colMonth') },
						{ label: m('cfo.forecastVariance.colForecast'), class: 'right' },
						{ label: m('cfo.forecastVariance.colActual'), class: 'right' },
						{ label: m('cfo.forecastVariance.colVariance'), class: 'right' },
						{ label: m('cfo.forecastVariance.colVariancePct'), class: 'right' }
					]}
					isEmpty={result.rows.length === 0}
					empty={m('cfo.forecastVariance.empty')}
				>
					{#snippet body()}
						{#each result?.rows ?? [] as row (row.month)}
							{@const pct = variancePctLabel(row)}
							{@const tone = varianceTone(row)}
							<tr data-unconverted={row.unconverted_count}>
								<td>
									{formatPeriod(row.month)}
									{#if row.unconverted_count > 0}
										<span class="cf-row-sub"
											>{m('cfo.forecastVariance.rowUnconverted', {
												n: row.unconverted_count
											})}</span
										>
									{/if}
								</td>
								<td class="right num"
									><Money amount={row.forecast} currency={result?.reporting_currency} whole /></td
								>
								<td class="right num"
									><Money amount={row.actual} currency={result?.reporting_currency} whole /></td
								>
								<!-- The backend's own subtraction, in Decimal. The tone
								     predicate only decides whether to tint it. -->
								<td class="right num" class:over={tone === 'over'} class:under={tone === 'under'}>
									<Money amount={row.variance} currency={result?.reporting_currency} whole accounting />
								</td>
								<!-- `null`, never 0%: a percentage of a zero (or absent)
								     forecast is not computable, and 0% reads as "exactly on
								     plan" — the most reassuring statement available over the
								     one row carrying no information. -->
								<td class="right num">{pct ?? m('cfo.forecastVariance.noVariancePct')}</td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>
			{/if}
		{:else}
			<p class="empty">{m('cfo.forecastVariance.empty')}</p>
		{/if}
	</div>

	<Modal
		{open}
		ariaLabel={m('cfo.forecastVariance.modalTitle')}
		title={m('cfo.forecastVariance.modalTitle')}
		width="lg"
		onclose={() => (open = false)}
	>
		<form
			onsubmit={(e) => {
				e.preventDefault();
				submit();
			}}
		>
			<p class="fv-hint">{m('cfo.forecastVariance.modalHint')}</p>
			<!-- The backend fails closed with a specific, PII-free explanation (a
			     422 naming the month it could not parse), and that explanation is
			     the actionable half of the refusal — so it lands in a persistent
			     region beside the form, not only in a toast that fades. Mirrors
			     `VendorStatementReconModal`'s `statement-intake-error`. -->
			{#if loadError}
				<p class="cf-error" role="alert" data-testid="forecast-variance-error">{loadError}</p>
			{/if}
			<table class="fv-entry">
				<thead>
					<tr>
						<th scope="col">{m('cfo.forecastVariance.fieldMonth')}</th>
						<th scope="col">{m('cfo.forecastVariance.fieldForecast')}</th>
						<th scope="col" class="fv-remove-col"></th>
					</tr>
				</thead>
				<tbody>
					{#each entries as _entry, i (i)}
						<tr>
							<td>
								<!-- `type="month"` yields exactly the `YYYY-MM` the API
								     parses, so the month half needs no shape repair. -->
								<input
									class="cf-input"
									type="month"
									bind:value={entries[i].month}
									aria-label={m('cfo.forecastVariance.ariaMonth', { n: i + 1 })}
								/>
							</td>
							<td>
								<!-- RAW decimal text, held as typed. Validated once at
								     submit; never `parseFloat`ed here. -->
								<input
									class="cf-input"
									type="text"
									inputmode="decimal"
									placeholder={m('cfo.forecastVariance.forecastPlaceholder')}
									bind:value={entries[i].forecast}
									aria-label={m('cfo.forecastVariance.ariaForecast', { n: i + 1 })}
								/>
							</td>
							<td>
								<button
									type="button"
									class="btn-cancel fv-remove"
									onclick={() => removeRow(i)}
									aria-label={m('cfo.forecastVariance.removeRow', { n: i + 1 })}
								>
									&times;
								</button>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
			<button type="button" class="btn-cancel" onclick={addRow} data-testid="forecast-variance-add">
				{m('cfo.forecastVariance.addRow')}
			</button>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (open = false)}>
					{m('common.cancel')}
				</button>
				<button type="submit" class="btn-primary" disabled={submitting} data-testid="forecast-variance-submit">
					{submitting ? m('cfo.forecastVariance.submitting') : m('cfo.forecastVariance.submit')}
				</button>
			</div>
		</form>
	</Modal>
{/if}

<style>
	.chart-card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 20px;
	}
	.chart-card h2 {
		font-size: 1rem;
		margin: 0;
	}
	.fv-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 8px;
	}
	.fv-hint {
		font-size: 0.85rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}
	.cf-error {
		color: var(--danger);
		font-weight: 500;
		margin: 0 0 12px;
	}
	/* Amber, not red: the comparison is usable — it just isn't complete.
	   Matches the cash-position card's own partial-figure notice. */
	.cf-skipped {
		color: #d4940a;
		font-weight: 600;
		font-size: 0.85rem;
		margin: 0 0 12px;
	}
	.cf-row-sub {
		color: var(--text-muted);
		font-size: 0.78rem;
		margin-left: 6px;
	}
	/* Paid out MORE than forecast. `--danger` is the on-a-dark-surface text
	   token; never its `-strong` companion, which is a FILL. */
	.num {
		font-variant-numeric: tabular-nums;
	}
	.num.over {
		color: var(--danger);
		font-weight: 600;
	}
	.num.under {
		color: var(--success);
		font-weight: 600;
	}
	.empty {
		color: var(--text-muted);
		text-align: center;
		padding: 20px;
	}
	.fv-entry {
		width: 100%;
		border-collapse: collapse;
		margin-bottom: 12px;
	}
	.fv-entry th {
		text-align: left;
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
		padding: 0 8px 6px 0;
	}
	.fv-entry td {
		padding: 4px 8px 4px 0;
	}
	.cf-input {
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.9rem;
		width: 100%;
	}
	.fv-remove {
		padding: 6px 12px;
	}
	.fv-remove-col {
		width: 1%;
	}
</style>
