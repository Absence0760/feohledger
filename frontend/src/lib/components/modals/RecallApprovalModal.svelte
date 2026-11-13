<script lang="ts">
	import { recallApproval } from '#lib/api/invoices.ts';
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';
	import type { Invoice } from '#lib/types/invoice.ts';

	/**
	 * Recall an invoice's approval, with the reason the next approver will
	 * read (`POST /api/invoices/{id}/recall-approval`, decisions §374). Opened
	 * by the invoice modal only when the server's `recall_approval` verdict is
	 * `null`; the server re-checks everything under the row lock, so a refusal
	 * that landed in between (a run staged meanwhile) is stated here, in the
	 * reader's language, and nothing moves.
	 */
	let {
		invoiceId,
		onclose,
		onrecalled
	}: {
		invoiceId: string;
		onclose: () => void;
		onrecalled: (invoice: Invoice) => void;
	} = $props();

	let reason = $state('');
	let recalling = $state(false);
	let recallError = $state<string | null>(null);

	async function handleSubmit() {
		const text = reason.trim();
		if (!text || recalling) return;
		recalling = true;
		recallError = null;
		try {
			const invoice = await recallApproval(invoiceId, text);
			toast(m('invoices.modal.toast.recalled'), 'success');
			onrecalled(invoice);
		} catch (err) {
			recallError = errorMessage(err, m('invoices.modal.toast.recallFailed'));
		} finally {
			recalling = false;
		}
	}
</script>

<Modal
	open
	ariaLabel={m('invoices.modal.recall.title')}
	title={m('invoices.modal.recall.title')}
	width="sm"
	{onclose}
>
	<form
		data-testid="recall-approval-form"
		onsubmit={(e) => {
			e.preventDefault();
			handleSubmit();
		}}
	>
		<p class="intro">{m('invoices.modal.recall.body')}</p>
		<label>
			<span>{m('invoices.modal.recall.reasonLabel')}</span>
			<textarea
				data-testid="recall-approval-reason"
				placeholder={m('invoices.modal.recall.reasonPlaceholder')}
				bind:value={reason}
				rows="3"
				maxlength="2000"
				required
			></textarea>
		</label>
		{#if recallError}
			<p class="error" role="alert" data-testid="recall-approval-error">{recallError}</p>
		{/if}
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button
				type="submit"
				class="btn-primary"
				data-testid="recall-approval-confirm"
				disabled={recalling || !reason.trim()}
			>
				{recalling ? m('invoices.modal.recall.recalling') : m('invoices.modal.recall.confirm')}
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
	.error {
		margin: 12px 0 0;
		font-size: 13px;
		color: var(--danger);
	}
</style>
