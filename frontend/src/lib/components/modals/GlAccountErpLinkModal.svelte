<script lang="ts">
	import { confirmGlAccountErpLink } from '#lib/api/glAccounts.ts';
	import { erpCandidateLabel, type GlAccount } from '#lib/types/glAccount.ts';
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { untrack } from 'svelte';

	/**
	 * Say which ERP account an unlinked chart row means, among the candidates
	 * the last sync named (`account.erp_link_candidates`, docs/decisions.md
	 * §284). The sync never guesses: a pre-§284 QuickBooks row "Depreciation"
	 * may be the top-level account or "Truck:Depreciation", and only a person
	 * who knows what the invoices coded to it meant can say. Nothing is
	 * preselected, so confirming is always a choice.
	 */
	let {
		account,
		onclose,
		onsaved
	}: { account: GlAccount; onclose: () => void; onsaved: () => void } = $props();

	// Fixed for the life of the dialog: the page mounts one per row.
	const row = untrack(() => account);
	const candidates = row.erp_link_candidates?.accounts ?? [];
	let chosen = $state<string | null>(null);
	let saving = $state(false);
	/** The backend's refusal, verbatim (a 409 when the candidates came from
	 *  another ERP connection names the fix: sync again). */
	let saveError = $state<string | null>(null);

	async function handleSubmit() {
		if (!chosen || saving) return;
		saving = true;
		saveError = null;
		try {
			const linked = await confirmGlAccountErpLink(row.id, chosen);
			const pick = candidates.find((c) => c.erp_account_id === chosen);
			toast(
				m('glAccounts.erpLink.toast.linked', {
					code: linked.code,
					erpCode: pick ? erpCandidateLabel(pick) : chosen
				}),
				'success'
			);
			onsaved();
			onclose();
		} catch (err) {
			saveError = err instanceof Error ? err.message : m('glAccounts.erpLink.toast.failed');
		} finally {
			saving = false;
		}
	}
</script>

<Modal
	open
	ariaLabel={m('glAccounts.erpLink.title', { code: row.code })}
	title={m('glAccounts.erpLink.title', { code: row.code })}
	width="md"
	{onclose}
>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			handleSubmit();
		}}
	>
		<p class="intro">{m('glAccounts.erpLink.intro', { code: row.code })}</p>
		<fieldset>
			<legend class="visually-hidden">{m('glAccounts.erpLink.title', { code: row.code })}</legend>
			{#each candidates as candidate (candidate.erp_account_id)}
				<label class="option" data-testid="gl-erp-candidate">
					<input
						type="radio"
						name="erp-candidate"
						value={candidate.erp_account_id}
						bind:group={chosen}
					/>
					<span class="label">{erpCandidateLabel(candidate)}</span>
					<span class="id">{m('glAccounts.erpLink.optionId', { id: candidate.erp_account_id })}</span>
				</label>
			{/each}
		</fieldset>
		{#if saveError}
			<p class="error" role="alert" data-testid="gl-erp-link-error">{saveError}</p>
		{/if}
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving || !chosen}>
				{saving ? m('common.saving') : m('glAccounts.erpLink.confirm')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.intro {
		margin: 0 0 12px;
		font-size: 13px;
		color: var(--text-muted);
	}
	fieldset {
		border: 0;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.option {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 6px;
		font-size: 13px;
		cursor: pointer;
	}
	.option:has(input:checked) {
		border-color: var(--accent);
	}
	.label {
		flex: 1;
		font-family: var(--font-mono);
		overflow-wrap: anywhere;
	}
	.id {
		color: var(--text-muted);
		font-size: 12px;
		white-space: nowrap;
	}
	.error {
		margin: 12px 0 0;
		font-size: 13px;
		color: var(--danger);
	}
</style>
