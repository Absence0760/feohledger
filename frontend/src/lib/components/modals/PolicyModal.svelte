<script lang="ts">
	import type { ExpensePolicy, ExpensePolicyCreate } from '$lib/types/expense';
	import { auth } from '$lib/stores/auth.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { createPolicy, updatePolicy } from '$lib/api/expenses';
	import type { MoneyAmount } from '$lib/utils/money';
	import { isMoneyInput } from '$lib/utils/moneyInput';

	let {
		policy,
		onclose,
		onsaved
	}: {
		// null → create mode; an ExpensePolicy → edit mode.
		policy: ExpensePolicy | null;
		onclose: () => void;
		onsaved: (p: ExpensePolicy) => void;
	} = $props();

	const isCreate = $derived(policy === null);
	// Policy CRUD = admin / ap_manager (mirrors the backend mutate gate).
	const canEdit = $derived(auth.isManager);

	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot */
	let name = $state(policy?.name ?? '');
	let active = $state(policy?.active ?? true);
	let category = $state(policy?.category ?? '');
	// The unit every money threshold below is denominated in. Blank = "the org's
	// reporting currency", which the backend resolves at evaluation time — the
	// meaning a bare threshold number always implicitly had.
	let threshold_currency = $state(policy?.threshold_currency ?? '');
	let category_limit = $state<string | null>(moneyText(policy?.category_limit));
	let requires_receipt_above = $state<string | null>(moneyText(policy?.requires_receipt_above));
	let requires_preapproval_above = $state<string | null>(
		moneyText(policy?.requires_preapproval_above)
	);
	let per_diem_amount = $state<string | null>(moneyText(policy?.per_diem_amount));
	let mileage_rate = $state<number | null>(policy?.mileage_rate ?? null);
	/* eslint-enable svelte/state-referenced-locally */

	let saving = $state(false);

	// Money / rate inputs use value={x ?? ''} + oninput (NOT bind:value) so an
	// empty field stays null — a null limit means "no limit", which the backend
	// engine must not read as 0.
	//
	// The four MONEY thresholds hold the raw decimal text all the way to the
	// wire (`schemas/expense.py` declares each a `Decimal`; a fractional JSON
	// number is already a float by the time pydantic sees it). `mileage_rate`
	// stays numeric — it is a rate the engine multiplies by miles, not an
	// amount rendered as currency.
	function numOrNull(v: unknown): number | null {
		if (v === '' || v === null || v === undefined) return null;
		const n = parseFloat(String(v));
		return Number.isFinite(n) ? n : null;
	}

	/** Seed a text money field from whatever shape the response carried. */
	function moneyText(value: MoneyAmount): string | null {
		return value === null || value === undefined || value === '' ? null : String(value);
	}

	async function handleSave() {
		if (!name.trim()) return;
		// A threshold we cannot read is REFUSED, never sent as `null` — the
		// engine reads a null threshold as "no limit", so a mistyped cap would
		// silently switch the rule off rather than fail.
		const thresholds = [
			category_limit,
			requires_receipt_above,
			requires_preapproval_above,
			per_diem_amount
		];
		if (thresholds.some((t) => t !== null && !isMoneyInput(t))) {
			toast(m('common.amountInvalid'), 'error');
			return;
		}
		saving = true;
		try {
			const payload: ExpensePolicyCreate = {
				name: name.trim(),
				active,
				category: category.trim() || null,
				threshold_currency: threshold_currency.trim().toUpperCase() || null,
				category_limit,
				requires_receipt_above,
				requires_preapproval_above,
				per_diem_amount,
				mileage_rate
			};
			const saved = isCreate
				? await createPolicy(payload)
				: await updatePolicy(policy!.id, payload);
			toast(m(isCreate ? 'policyModal.toast.created' : 'policyModal.toast.saved'), 'success');
			onsaved(saved);
			onclose();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('policyModal.toast.saveFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	const modalTitle = $derived(
		isCreate ? m('policyModal.title.new') : m('policyModal.title.edit', { name: policy!.name })
	);
	const ariaLabel = $derived(isCreate ? m('policyModal.aria.new') : m('policyModal.aria.detail'));
</script>

<Modal open {ariaLabel} title={modalTitle} width="lg" {onclose}>
	<form onsubmit={(e) => { e.preventDefault(); handleSave(); }}>
		<div class="form-grid">
			<label>
				<span>{m('policyModal.field.name')} <em class="required">*</em></span>
				<input type="text" bind:value={name} required disabled={!canEdit} />
			</label>
			<label>
				<span>{m('policyModal.field.category')}</span>
				<input
					type="text"
					bind:value={category}
					placeholder={m('policyModal.field.categoryPlaceholder')}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('policyModal.field.thresholdCurrency')}</span>
				<!-- The placeholder shows what blank resolves to only when the org's
				     code is known; unresolved, it is left empty rather than showing
				     a guessed one — the hint beneath says what blank means. -->
				<input
					type="text"
					maxlength="3"
					bind:value={threshold_currency}
					placeholder={orgCurrency.currency
						? m('policyModal.field.thresholdCurrencyPlaceholder', {
								currency: orgCurrency.currency
							})
						: undefined}
					disabled={!canEdit}
				/>
				<small>{m('policyModal.field.thresholdCurrencyHint')}</small>
			</label>
			<label>
				<span>{m('policyModal.field.categoryLimit')}</span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={category_limit ?? ''}
					oninput={(e) => (category_limit = e.currentTarget.value.trim() || null)}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('policyModal.field.receiptAbove')}</span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={requires_receipt_above ?? ''}
					oninput={(e) => (requires_receipt_above = e.currentTarget.value.trim() || null)}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('policyModal.field.preapprovalAbove')}</span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={requires_preapproval_above ?? ''}
					oninput={(e) => (requires_preapproval_above = e.currentTarget.value.trim() || null)}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('policyModal.field.perDiem')}</span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={per_diem_amount ?? ''}
					oninput={(e) => (per_diem_amount = e.currentTarget.value.trim() || null)}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('policyModal.field.mileageRate')}</span>
				<input
					type="number"
					step="0.001"
					min="0"
					value={mileage_rate ?? ''}
					oninput={(e) => (mileage_rate = numOrNull(e.currentTarget.value))}
					disabled={!canEdit}
				/>
			</label>
			<label class="checkbox-label">
				<input type="checkbox" bind:checked={active} disabled={!canEdit} />
				<span>{m('policyModal.field.active')}</span>
			</label>
		</div>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('policyModal.close')}</button>
			{#if canEdit}
				<button type="submit" class="btn-primary" disabled={saving || !name.trim()}>
					{saving
						? m('policyModal.saving')
						: isCreate
							? m('policyModal.create')
							: m('policyModal.save')}
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

	.form-grid label.checkbox-label {
		flex-direction: row;
		align-items: center;
		gap: 8px;
	}

	/* The TEXT-entry recipe, carved away from the checkbox/radio it also
	   reached. Svelte scopes this to `.form-grid.svelte-x input:where(.svelte-x)`,
	   which outranks the global control base in `app.css`, and `background:`
	   is a SHORTHAND — it reset `background-image`, the drawn tick, so the
	   Active toggle rendered identically checked and unchecked. */
	.form-grid input:not([type='checkbox']):not([type='radio']) {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}

	.form-grid input:disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.form-grid small {
		font-size: 0.72rem;
		color: var(--text-muted);
	}
</style>
