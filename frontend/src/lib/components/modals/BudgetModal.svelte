<script lang="ts">
	import type { Budget, BudgetDimension, BudgetSpend } from '#lib/types/budget.ts';
	import { BUDGET_DIMENSIONS, BUDGET_DIMENSION_LABELS } from '#lib/types/budget.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { formatMoney, isNegativeAmount } from '#lib/utils/money.ts';
	import { normalizeMoneyInput } from '#lib/utils/moneyInput.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { createBudget, updateBudget, getBudgetSpend } from '#lib/api/budgets.ts';

	let {
		budget,
		onclose,
		onsaved
	}: {
		// null → create mode; a Budget → detail/edit mode.
		budget: Budget | null;
		onclose: () => void;
		onsaved: (b: Budget) => void;
	} = $props();

	const isCreate = $derived(budget === null);
	// Budgets are financial config — only admin / cfo may mutate.
	const canEdit = $derived(auth.hasAnyRole('admin', 'cfo'));

	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot */
	let name = $state(budget?.name ?? '');
	let dimension = $state<BudgetDimension>((budget?.dimension as BudgetDimension) ?? 'department');
	let dimension_value = $state(budget?.dimension_value ?? '');
	let period = $state(budget?.period ?? '');
	let period_start = $state(budget?.period_start ?? '');
	let period_end = $state(budget?.period_end ?? '');
	// The RAW text the user typed, not a parsed number: the allocation reaches
	// the backend as the exact decimal string it was typed as (see
	// `types/budget.ts` → request shapes). Seeded from the response amount,
	// which may itself arrive as a JSON number.
	let amount = $state(String(budget?.amount ?? ''));
	let currency = $state(budget?.currency ?? 'USD');
	let notes = $state(budget?.notes ?? '');
	/* eslint-enable svelte/state-referenced-locally */

	let saving = $state(false);

	// Spend rollup loaded lazily in detail mode. Seeded from `budget` rather
	// than `false`: the `$effect` below fires the fetch, and an effect runs
	// AFTER the first render, so a `false` seed spent one frame rendering the
	// rollup row as `unavailable` (a dash that means "did not arrive") before
	// flipping to pending. In detail mode the fetch is certain, so say so from
	// the first paint. Create mode has no budget and no fetch.
	let spend = $state<BudgetSpend | null>(null);
	/* eslint-disable-next-line svelte/state-referenced-locally -- modal receives a snapshot */
	let spendLoading = $state(budget !== null);

	$effect(() => {
		if (budget) loadSpend(budget.id);
	});

	async function loadSpend(id: string) {
		spendLoading = true;
		try {
			spend = await getBudgetSpend(id);
		} catch {
			spend = null;
		} finally {
			spendLoading = false;
		}
	}

	function handleError(err: unknown, fallback: string) {
		toast(err instanceof Error ? err.message : fallback, 'error');
	}

	const utilClass = $derived(
		spend == null
			? ''
			: spend.utilization_pct >= 100
				? 'over'
				: spend.utilization_pct >= 80
					? 'warn'
					: 'ok'
	);

	async function handleSave() {
		// `normalizeMoneyInput` decides shape with a regex, never `Number` — the
		// string that goes out is the string that came in. Text we could not
		// read is REFUSED, never repaired and never quietly dropped: silently
		// posting no allocation for an amount the user typed is the same class
		// of failure as posting a rounded one.
		const allocation = normalizeMoneyInput(amount);
		if (!name.trim() || !dimension_value.trim()) return;
		if (allocation === null) {
			toast(m('common.amountInvalid'), 'error');
			return;
		}
		saving = true;
		try {
			const payload = {
				name: name.trim(),
				dimension,
				dimension_value: dimension_value.trim(),
				period: period.trim() || null,
				period_start: period_start || null,
				period_end: period_end || null,
				amount: allocation,
				currency: currency.trim() || 'USD',
				notes: notes.trim() || null
			};
			const saved = isCreate ? await createBudget(payload) : await updateBudget(budget!.id, payload);
			toast(isCreate ? m('budgets.modal.toast.created') : m('budgets.modal.toast.saved'), 'success');
			onsaved(saved);
			onclose();
		} catch (err) {
			handleError(err, isCreate ? m('budgets.modal.toast.createFailed') : m('budgets.modal.toast.saveFailed'));
		} finally {
			saving = false;
		}
	}

	const modalTitle = $derived(
		isCreate
			? m('budgets.modal.title.new')
			: canEdit
				? m('budgets.modal.title.edit', { name: budget!.name })
				: m('budgets.modal.title.view', { name: budget!.name })
	);
	const ariaLabel = $derived(isCreate ? m('budgets.modal.aria.new') : m('budgets.modal.aria.detail'));
</script>

<Modal open {ariaLabel} title={modalTitle} width="lg" {onclose}>
	<form onsubmit={(e) => { e.preventDefault(); handleSave(); }}>
		{#if !isCreate}
			<!-- Spend rollup (computed on read) — the KPI row renders on EVERY
			     state, never gated on the response (`docs/decisions.md` §125). It
			     used to sit inside `{#if spend}` and collapse while the rollup was
			     in flight, which in a modal is the worst version of the problem:
			     the dialog's own height changes under the reader as the row pops
			     in, moving the fields below it.

			     The Remaining card's tint is a verdict — GREEN unless the balance
			     is negative — so a collapsed row was the only thing stopping "in
			     budget" being painted before anyone had totalled the spend.
			     `KpiCard` withholds the tint while there is no figure, so the
			     `spend &&` guards here are only keeping the expressions from
			     dereferencing a null. The bar + detail lines below stay gated:
			     they render a percentage-width geometry and a set of amounts,
			     neither of which has a "no figure yet" form. -->
			<div class="kpi-row">
				<KpiCard
					value={spend ? `${spend.utilization_pct.toFixed(1)}%` : null}
					label={m('budgets.modal.kpi.utilization')}
					highlight={spend && spend.utilization_pct >= 100 ? 'red' : null}
					pending={spendLoading}
				/>
				<KpiCard
					value={spend ? formatMoney(spend.committed, { currency: spend.currency }) : null}
					label={m('budgets.modal.kpi.committed')}
					pending={spendLoading}
				/>
				<KpiCard
					value={spend ? formatMoney(spend.remaining, { currency: spend.currency }) : null}
					label={m('budgets.modal.kpi.remaining')}
					highlight={spend ? (isNegativeAmount(spend.remaining) ? 'red' : 'green') : null}
					pending={spendLoading}
				/>
			</div>
			{#if spend}
				<div class="util-bar" aria-label={m('budgets.modal.utilizationAria', { percent: spend.utilization_pct.toFixed(1) })}>
					<div class="util-fill {utilClass}" style={`width: ${Math.min(spend.utilization_pct, 100)}%`}></div>
				</div>
				<div class="spend-detail">
					<span>{m('budgets.modal.detail.allocated')} <Money amount={spend.allocated} currency={spend.currency} /></span>
					<span>{m('budgets.modal.detail.committed')} <Money amount={spend.committed} currency={spend.currency} /></span>
					<span>{m('budgets.modal.detail.actual')} <Money amount={spend.actual} currency={spend.currency} /></span>
					<span class:over={isNegativeAmount(spend.remaining)}>
						{m('budgets.modal.detail.remaining')} <Money amount={spend.remaining} currency={spend.currency} accounting />
					</span>
				</div>
			{:else if spendLoading}
				<p class="muted">{m('budgets.modal.loadingSpend')}</p>
			{/if}
		{/if}

		<div class="form-grid">
			<label class="full-width">
				<span>{m('budgets.modal.field.name')} <em class="required">*</em></span>
				<input type="text" bind:value={name} required disabled={!canEdit} />
			</label>
			<label>
				<span>{m('budgets.modal.field.dimension')}</span>
				<select bind:value={dimension} disabled={!canEdit}>
					{#each BUDGET_DIMENSIONS as d}
						<option value={d}>{BUDGET_DIMENSION_LABELS[d]}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>{m('budgets.modal.field.dimensionValue')} <em class="required">*</em></span>
				<input
					type="text"
					bind:value={dimension_value}
					placeholder={m('budgets.modal.field.dimensionValuePlaceholder')}
					required
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('budgets.modal.field.amount')} <em class="required">*</em></span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={amount}
					oninput={(e) => (amount = e.currentTarget.value)}
					required
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('budgets.modal.field.currency')}</span>
				<input type="text" bind:value={currency} maxlength="3" disabled={!canEdit} />
			</label>
			<label>
				<span>{m('budgets.modal.field.period')}</span>
				<input type="text" bind:value={period} placeholder={m('budgets.modal.field.periodPlaceholder')} disabled={!canEdit} />
			</label>
			<label>
				<span>{m('budgets.modal.field.periodStart')}</span>
				<input type="date" bind:value={period_start} disabled={!canEdit} />
			</label>
			<label>
				<span>{m('budgets.modal.field.periodEnd')}</span>
				<input type="date" bind:value={period_end} disabled={!canEdit} />
			</label>
			<label class="full-width">
				<span>{m('budgets.modal.field.notes')}</span>
				<textarea bind:value={notes} rows="2" disabled={!canEdit}></textarea>
			</label>
		</div>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('budgets.modal.close')}</button>
			{#if canEdit}
				<button type="submit" class="btn-primary" disabled={saving}>
					{saving ? m('budgets.modal.saving') : isCreate ? m('budgets.modal.create') : m('budgets.modal.save')}
				</button>
			{/if}
		</div>
	</form>
</Modal>

<style>
	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}
	.form-grid label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.form-grid label.full-width {
		grid-column: 1 / -1;
	}
	.form-grid input,
	.form-grid select,
	.form-grid textarea {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}
	.form-grid input:disabled,
	.form-grid select:disabled,
	.form-grid textarea:disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.util-bar {
		height: 8px;
		border-radius: 4px;
		background: var(--bg);
		border: 1px solid var(--border);
		overflow: hidden;
		margin: 4px 0 12px;
	}
	.util-fill {
		height: 100%;
		border-radius: 4px;
		transition: width 0.2s ease;
	}
	.util-fill.ok { background: #1fa86a; }
	.util-fill.warn { background: #d4940a; }
	.util-fill.over { background: #e04040; }

	.spend-detail {
		display: flex;
		flex-wrap: wrap;
		gap: 18px;
		margin-bottom: 16px;
		font-size: 0.8rem;
		color: var(--text-muted);
	}
	.spend-detail span.over {
		color: var(--danger);
		font-weight: 600;
	}

	.muted {
		color: var(--text-muted);
		font-size: 0.85rem;
	}
</style>
