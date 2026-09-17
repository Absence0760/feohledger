<script lang="ts">
	import type { Expense, ExpenseStatus } from '$lib/types/expense';
	import {
		EXPENSE_PAYMENT_METHODS,
		EXPENSE_PAYMENT_METHOD_LABEL_KEYS,
		expenseStatusLabelKey,
		EXPENSE_STATUS_TONES
	} from '$lib/types/expense';
	import { auth } from '$lib/stores/auth.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import type { MoneyAmount } from '$lib/utils/money';
	import { normalizeMoneyInput } from '$lib/utils/moneyInput';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import {
		createExpense,
		updateExpense,
		uploadReceipt,
		receiptUrl,
		type GlAccountOption
	} from '$lib/api/expenses';
	import { glAccountOptionLabel } from '$lib/types/glAccount';
	import { entityStore } from '$lib/stores/entity.svelte';

	let {
		expense,
		glAccounts,
		onclose,
		onsaved
	}: {
		// null → create mode; an Expense → detail/edit mode.
		expense: Expense | null;
		glAccounts: GlAccountOption[];
		onclose: () => void;
		onsaved: (e: Expense) => void;
	} = $props();

	const isCreate = $derived(expense === null);
	// create / update / receipt upload = admin / ap_manager / ap_clerk.
	const canEdit = $derived(auth.hasAnyRole('admin', 'ap_manager', 'ap_clerk'));

	/** Today as `YYYY-MM-DD` in the user's own timezone — `toISOString()` would
	 *  hand back the UTC day, which is the previous date for anyone west of
	 *  Greenwich after 00:00 local. */
	function todayLocalIso(): string {
		const now = new Date();
		const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
		return local.toISOString().slice(0, 10);
	}

	/** Seed a text money field from whatever shape the response carried. */
	function moneyText(value: MoneyAmount): string | null {
		return value === null || value === undefined || value === '' ? null : String(value);
	}

	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot */
	// `expense_date` is REQUIRED (`ExpenseCreate.expense_date: date`, and the
	// column is NOT NULL). Create mode seeds today so the field is never blank —
	// an empty one used to POST `expense_date: null` and come back as a 422 whose
	// `detail` is a list, rendering as the notorious "[object Object]" toast.
	let expense_date = $state(expense?.expense_date ?? todayLocalIso());
	let merchant = $state(expense?.merchant ?? '');
	let category = $state(expense?.category ?? '');
	// The typed amount stays RAW TEXT to the wire: `schemas/expense.py`
	// declares `ExpenseCreate.amount` a `Decimal`, and `json.loads` has already
	// collapsed a fractional JSON number to a float by the time pydantic sees
	// it. `normalizeMoneyInput` decides shape with a regex, never `Number`.
	let amount = $state<string | null>(moneyText(expense?.amount));
	let currency = $state(expense?.currency ?? 'USD');
	let payment_method = $state(expense?.payment_method ?? 'out_of_pocket');
	let gl_account_id = $state(expense?.gl_account_id ?? '');
	let description = $state(expense?.description ?? '');
	let reimbursable = $state(expense?.reimbursable ?? true);
	let mileage_miles = $state<number | null>(expense?.mileage_miles ?? null);
	/* eslint-enable svelte/state-referenced-locally */

	let saving = $state(false);

	// Receipt: on create we stash the File and upload after the create returns
	// an id; on edit we upload immediately (the id already exists).
	let fileInput = $state<HTMLInputElement>();
	let pendingFile = $state<File | null>(null);
	let uploading = $state(false);

	const status = $derived(expense?.status ?? 'draft');
	// `Expense.status` is a bare string on the wire, so both lookups below are
	// `?? fallback` — a status this build doesn't know renders its raw value in
	// a flat chip rather than blank.
	const statusKey = $derived(status as ExpenseStatus);
	// The status name is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = $derived.by(() => {
		const key = expenseStatusLabelKey(status);
		return key ? m(key) : status;
	});

	function numOrNull(v: unknown): number | null {
		if (v === '' || v === null || v === undefined) return null;
		const n = parseFloat(String(v));
		return Number.isFinite(n) ? n : null;
	}

	function handleError(err: unknown, fallback: string) {
		toast(err instanceof Error ? err.message : fallback, 'error');
	}

	function onFilePick(e: Event) {
		const input = e.target as HTMLInputElement;
		const file = input.files?.[0] ?? null;
		if (!file) return;
		if (isCreate || !expense) {
			// Defer — upload after the create call returns an id.
			pendingFile = file;
		} else {
			uploadNow(expense.id, file);
		}
	}

	async function uploadNow(id: string, file: File) {
		uploading = true;
		try {
			const updated = await uploadReceipt(id, file);
			toast(m('expenseModal.toast.receiptUploaded'), 'success');
			onsaved(updated);
		} catch (err) {
			handleError(err, m('expenseModal.toast.uploadFailed'));
		} finally {
			uploading = false;
			if (fileInput) fileInput.value = '';
		}
	}

	async function handleSave() {
		// `expense_date` joins the guard: it is required by the API, and clearing
		// it on an EDIT would PATCH an explicit null at a NOT NULL column.
		if (!expense_date || !merchant.trim() || amount == null) return;
		const exactAmount = normalizeMoneyInput(amount);
		if (exactAmount === null) {
			// Refused, never repaired: an expense whose amount we could not read
			// must not be created amount-less (the column is NOT NULL and the
			// policy engine judges the figure).
			toast(m('common.amountInvalid'), 'error');
			return;
		}
		saving = true;
		try {
			const payload = {
				expense_date,
				merchant: merchant.trim(),
				category: category.trim() || null,
				amount: exactAmount,
				currency: currency.trim() || 'USD',
				payment_method,
				gl_account_id: gl_account_id || null,
				description: description.trim() || null,
				reimbursable,
				// null (not 0) when blank — the backend reads a missing value as
				// "this line is not a trip" and skips the mileage rule entirely.
				mileage_miles
			};
			let saved = isCreate
				? await createExpense(payload)
				: await updateExpense(expense!.id, payload);
			// A receipt staged during create is uploaded once the id exists.
			if (pendingFile) {
				saved = await uploadReceipt(saved.id, pendingFile);
				pendingFile = null;
			}
			toast(m(isCreate ? 'expenseModal.toast.created' : 'expenseModal.toast.saved'), 'success');
			onsaved(saved);
			onclose();
		} catch (err) {
			handleError(err, m(isCreate ? 'expenseModal.toast.createFailed' : 'expenseModal.toast.saveFailed'));
		} finally {
			saving = false;
		}
	}

	async function viewReceipt() {
		if (!expense?.receipt_file_key) return;
		try {
			const url = await receiptUrl(expense.receipt_file_key);
			window.open(url, '_blank');
			setTimeout(() => URL.revokeObjectURL(url), 60_000);
		} catch (err) {
			handleError(err, m('expenseModal.toast.receiptLoadFailed'));
		}
	}

	const modalTitle = $derived(
		isCreate
			? m('expenseModal.title.new')
			: canEdit
				? m('expenseModal.title.edit', {
						name: expense!.merchant ?? expense!.id.slice(0, 8)
					})
				: m('expenseModal.title.view', {
						name: expense!.merchant ?? expense!.id.slice(0, 8)
					})
	);
	const ariaLabel = $derived(isCreate ? m('expenseModal.aria.new') : m('expenseModal.aria.detail'));
</script>

<Modal open {ariaLabel} title={modalTitle} width="lg" {onclose}>
	<form onsubmit={(e) => { e.preventDefault(); handleSave(); }}>
		{#if !isCreate}
			<div class="status-row">
				<Badge tone={EXPENSE_STATUS_TONES[statusKey] ?? 'neutral'} variant={status}>
					{statusLabel}
				</Badge>
			</div>
		{/if}

		<div class="form-grid">
			<label>
				<span>{m('expenseModal.field.date')} <em class="required">*</em></span>
				<input type="date" bind:value={expense_date} required disabled={!canEdit} />
			</label>
			<label>
				<span>{m('expenseModal.field.merchant')} <em class="required">*</em></span>
				<input type="text" bind:value={merchant} required disabled={!canEdit} />
			</label>
			<label>
				<span>{m('expenseModal.field.amount')} <em class="required">*</em></span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={amount ?? ''}
					oninput={(e) => (amount = e.currentTarget.value.trim() || null)}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('expenseModal.field.currency')}</span>
				<input type="text" bind:value={currency} maxlength="3" disabled={!canEdit} />
			</label>
			<label>
				<span>{m('expenseModal.field.category')}</span>
				<input
					type="text"
					bind:value={category}
					placeholder={m('expenseModal.field.categoryPlaceholder')}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('expenseModal.field.paymentMethod')}</span>
				<select bind:value={payment_method} disabled={!canEdit}>
					{#each EXPENSE_PAYMENT_METHODS as method}
						<option value={method}>{m(EXPENSE_PAYMENT_METHOD_LABEL_KEYS[method])}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>{m('expenseModal.field.glAccount')}</span>
				<select bind:value={gl_account_id} disabled={!canEdit}>
					<option value="">{m('expenseModal.field.glSelect')}</option>
					{#each glAccounts as g (g.id)}
						<option value={g.id}>
							{glAccountOptionLabel(g, entityStore, {
								withName: true,
								unknownEntity: m('glAccounts.scope.unknownEntity')
							})}
						</option>
					{/each}
				</select>
			</label>
			<label>
				<span>{m('expenseModal.field.mileageMiles')}</span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={mileage_miles ?? ''}
					oninput={(e) => (mileage_miles = numOrNull(e.currentTarget.value))}
					placeholder={m('expenseModal.field.mileageMilesPlaceholder')}
					disabled={!canEdit}
				/>
			</label>
			<label class="checkbox-label">
				<input type="checkbox" bind:checked={reimbursable} disabled={!canEdit} />
				<span>{m('expenseModal.field.reimbursable')}</span>
			</label>
			<label class="full-width">
				<span>{m('expenseModal.field.description')}</span>
				<textarea bind:value={description} rows="2" disabled={!canEdit}></textarea>
			</label>
		</div>

		<!-- Receipt -->
		<div class="receipt-section">
			<span class="receipt-title">{m('expenseModal.receipt.title')}</span>
			{#if expense?.receipt_file_key}
				<button type="button" class="btn-doc" onclick={viewReceipt}
					>{m('expenseModal.receipt.view')}</button
				>
			{:else if pendingFile}
				<span class="receipt-empty"
					>{m('expenseModal.receipt.pending', { name: pendingFile.name })}</span
				>
			{:else}
				<span class="receipt-empty">{m('expenseModal.receipt.empty')}</span>
			{/if}
			{#if canEdit}
				<input
					type="file"
					accept=".pdf,.png,.jpg,.jpeg,.tiff,.heic,.webp"
					bind:this={fileInput}
					onchange={onFilePick}
					hidden
				/>
				<button
					type="button"
					class="btn-doc-upload"
					disabled={uploading}
					onclick={() => fileInput?.click()}
				>
					{uploading
						? m('expenseModal.receipt.uploading')
						: expense?.receipt_file_key
							? m('expenseModal.receipt.replace')
							: m('expenseModal.receipt.attach')}
				</button>
			{/if}
		</div>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('expenseModal.close')}</button>
			{#if canEdit}
				<button type="submit" class="btn-primary" disabled={saving}>
					{saving
						? m('expenseModal.saving')
						: isCreate
							? m('expenseModal.create')
							: m('expenseModal.save')}
				</button>
			{/if}
		</div>
	</form>
</Modal>

<style>
	.status-row {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 12px;
	}

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

	.form-grid label.checkbox-label {
		flex-direction: row;
		align-items: center;
		gap: 8px;
	}

	/* The TEXT-entry recipe, carved away from the checkbox/radio it also
	   reached. Svelte scopes this to `.form-grid.svelte-x input:where(.svelte-x)`,
	   which outranks the global control base in `app.css`, and `background:`
	   is a SHORTHAND — it reset `background-image`, the drawn tick, so the
	   Reimbursable toggle rendered identically checked and unchecked. */
	.form-grid input:not([type='checkbox']):not([type='radio']),
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

	.receipt-section {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-top: 16px;
		flex-wrap: wrap;
	}
	.receipt-title {
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--text);
	}
	.receipt-empty {
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.btn-doc,
	.btn-doc-upload {
		padding: 5px 12px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
	}
	.btn-doc:hover,
	.btn-doc-upload:hover {
		border-color: var(--accent);
		color: var(--accent);
	}
	.btn-doc-upload:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
</style>
