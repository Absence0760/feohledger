<script lang="ts">
	/**
	 * The way out of a credential block whose stored value can no longer be
	 * opened (`docs/decisions.md` §464).
	 *
	 * Every save of such a block answers 503 `credential_block_unopenable`: the
	 * writer has to open the stored value to merge into it, and its KMS key is
	 * disabled (or, in dev, `FEOH_SECRET_KEY` changed). The panel that got that
	 * refusal renders this prompt, which says so and offers "Clear stored
	 * credentials" — after a confirm that says every stored secret of the block
	 * goes and must be typed again — calling `DELETE
	 * /api/organization/credentials/{block}`, which removes the row without
	 * opening it. Admin-only: the panels render it only for an admin, and the
	 * endpoint refuses anyone else.
	 *
	 * The caller decides when to show it (an `ApiError` whose `code` is
	 * {@link CREDENTIAL_BLOCK_UNOPENABLE}) and gets the new stored-name status
	 * through `oncleared`, then hides it.
	 */
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { resetProviderCredentials } from '#lib/api/providerCredentials.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type {
		CredentialBlock,
		ProviderCredentialStatus
	} from '#lib/types/providerCredentials.ts';

	let {
		block,
		oncleared
	}: {
		block: CredentialBlock;
		oncleared: (status: ProviderCredentialStatus) => void;
	} = $props();

	let confirming = $state(false);
	let clearing = $state(false);

	async function clear() {
		clearing = true;
		try {
			const status = await resetProviderCredentials(block);
			confirming = false;
			toast(m('org.credentialReset.cleared'), 'success');
			oncleared(status);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			clearing = false;
		}
	}
</script>

<div class="credential-reset" role="alert" data-testid="credential-reset-{block}">
	<p>{m('org.credentialReset.notice')}</p>
	<button
		type="button"
		class="btn-danger"
		data-testid="credential-reset-open-{block}"
		onclick={() => (confirming = true)}
	>
		{m('org.credentialReset.action')}
	</button>
</div>

<Modal
	open={confirming}
	ariaLabel={m('org.credentialReset.confirmTitle')}
	title={m('org.credentialReset.confirmTitle')}
	width="sm"
	onclose={() => (confirming = false)}
>
	<p class="confirm-body">{m('org.credentialReset.confirmBody')}</p>
	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={() => (confirming = false)}>
			{m('common.cancel')}
		</button>
		<button
			type="button"
			class="btn-danger armed"
			data-testid="credential-reset-confirm-{block}"
			disabled={clearing}
			onclick={clear}
		>
			{clearing ? m('org.common.saving') : m('org.credentialReset.confirm')}
		</button>
	</div>
</Modal>

<style>
	.credential-reset {
		margin: 4px 0 12px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.credential-reset p {
		margin: 0 0 8px;
	}

	.confirm-body {
		margin: 0 0 16px;
		font-size: 0.9rem;
	}
</style>
