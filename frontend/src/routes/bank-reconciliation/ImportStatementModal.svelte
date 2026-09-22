<script lang="ts">
	/**
	 * Import a bank statement CSV.
	 *
	 * Mutate-only (admin / ap_manager) — the page never renders this for a
	 * clerk, and `require_roles` refuses the POST regardless.
	 *
	 * The upload is idempotent at the backend on
	 * `(org, account_identifier, sha256(body))`, so a double-click or a retry
	 * returns the FIRST statement rather than creating a second one that would
	 * match nothing and read as "this file didn't reconcile".
	 */
	import type { BankStatement } from '$lib/types/bankReconciliation';
	import { uploadBankStatement } from '$lib/api/bankReconciliation';
	import Modal from '$lib/components/ui/Modal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { DEFAULT_CURRENCY, currencyOptions } from '$lib/utils/money';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';

	let {
		onclose,
		onimported
	}: {
		onclose: () => void;
		onimported: (statement: BankStatement) => void;
	} = $props();

	let file = $state<File | null>(null);
	let accountIdentifier = $state('');
	let periodStart = $state('');
	let periodEnd = $state('');
	/* eslint-disable svelte/state-referenced-locally -- seeded once from the store */
	// A form's initial value, so it needs A code: the org's when it resolved,
	// else the platform default — explicitly, since the store itself no longer
	// substitutes one (decisions §200).
	let currency = $state(orgCurrency.currency ?? DEFAULT_CURRENCY);
	/* eslint-enable svelte/state-referenced-locally */
	let saving = $state(false);

	/**
	 * Mirrors `csv_import.MAX_CSV_IMPORT_SIZE` (10 MB). The backend is
	 * authoritative and 413s regardless — this only spares the user a 10 MB
	 * upload that cannot land, and lets the refusal read as a size problem
	 * rather than a network failure.
	 */
	const MAX_UPLOAD_MB = 10;

	/**
	 * The import's own refusal (a header with no date column, an empty file)
	 * comes back as a specific 422 explanation, and that explanation is the
	 * whole point of the refusal — so it lands in a persistent inline region.
	 * A toast that fades leaves the user with a form that just didn't work.
	 */
	let importError = $state<string | null>(null);

	const currencies = $derived(currencyOptions(orgCurrency.currency ?? DEFAULT_CURRENCY));

	const canSubmit = $derived(
		file !== null && accountIdentifier.trim() !== '' && periodStart !== '' && periodEnd !== ''
	);

	function onFile(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const picked = input.files?.[0] ?? null;
		importError = null;
		if (picked && picked.size > MAX_UPLOAD_MB * 1024 * 1024) {
			file = null;
			input.value = '';
			importError = m('bankRecon.import.tooLarge', { max: MAX_UPLOAD_MB });
			return;
		}
		file = picked;
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (!canSubmit || !file || saving) return;
		saving = true;
		importError = null;
		try {
			const statement = await uploadBankStatement(file, {
				account_identifier: accountIdentifier.trim(),
				period_start: periodStart,
				period_end: periodEnd,
				currency
			});
			toast(
				m('bankRecon.toast.imported', {
					matched: statement.matched_count,
					count: statement.transaction_count
				}),
				'success'
			);
			onimported(statement);
		} catch (err) {
			importError = err instanceof Error ? err.message : m('bankRecon.toast.importFailed');
		} finally {
			saving = false;
		}
	}
</script>

<Modal ariaLabel={m('bankRecon.import.aria')} title={m('bankRecon.import.aria')} {onclose}>
	<form onsubmit={submit}>
		<label class="field">
			<span>{m('bankRecon.import.file')}</span>
			<input type="file" accept=".csv,text/csv" onchange={onFile} required />
		</label>

		<label class="field">
			<span>{m('bankRecon.import.account')}</span>
			<input type="text" bind:value={accountIdentifier} maxlength="120" required />
			<!-- The identifier is a LABEL, not a credential: it is stored in the
			     clear on the statement row and rendered in the list. Say so, so a
			     full account number never gets typed into it. -->
			<small class="hint">{m('bankRecon.import.accountHint')}</small>
		</label>

		<div class="field-row">
			<label class="field">
				<span>{m('bankRecon.import.periodStart')}</span>
				<input type="date" bind:value={periodStart} required />
			</label>
			<label class="field">
				<span>{m('bankRecon.import.periodEnd')}</span>
				<input type="date" bind:value={periodEnd} required />
			</label>
			<label class="field">
				<span>{m('bankRecon.import.currency')}</span>
				<select bind:value={currency}>
					{#each currencies as code (code)}
						<option value={code}>{code}</option>
					{/each}
				</select>
			</label>
		</div>

		{#if importError}
			<!-- `role="alert"` so the refusal reaches a screen reader without a
			     focus move (WCAG 4.1.3). -->
			<p class="import-error" role="alert" data-testid="import-error">{importError}</p>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={!canSubmit || saving}>
				{saving ? m('bankRecon.import.importing') : m('bankRecon.import.submit')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 14px;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.field-row {
		display: flex;
		gap: 12px;
		flex-wrap: wrap;
	}
	.field-row .field {
		flex: 1 1 140px;
	}
	.hint {
		font-size: 0.75rem;
		color: var(--text-muted);
		line-height: 1.4;
	}
	.import-error {
		margin: 0 0 14px;
		padding: 10px 12px;
		border: 1px solid var(--danger-strong);
		border-radius: 6px;
		color: var(--danger-on-tint);
		font-size: 0.85rem;
		line-height: 1.45;
	}
</style>
