<script lang="ts">
	import { inviteVendorPortalUser, type PortalInviteResult } from '#lib/api/vendors.ts';
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		vendorId,
		vendorName,
		onclose,
		oninvited
	}: {
		vendorId: string;
		vendorName: string;
		onclose: () => void;
		oninvited: (result: PortalInviteResult) => void;
	} = $props();

	let email = $state('');
	let fullName = $state('');
	let saving = $state(false);

	const canSubmit = $derived(email.trim() !== '' && fullName.trim() !== '');

	async function handleSubmit() {
		if (!canSubmit || saving) return;
		saving = true;
		try {
			const result = await inviteVendorPortalUser(vendorId, {
				email: email.trim(),
				full_name: fullName.trim()
			});
			// The parent confirms delivery. There is no credential in `result` —
			// the temp password is emailed to the supplier and never returned.
			oninvited(result);
			onclose();
		} catch (err) {
			toast(
				err instanceof Error ? err.message : m('vendors.invite.toast.failed'),
				'error'
			);
		} finally {
			saving = false;
		}
	}
</script>

<Modal
	open
	ariaLabel={m('vendors.invite.aria')}
	title={m('vendors.invite.title', { vendor: vendorName })}
	width="sm"
	{onclose}
>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			handleSubmit();
		}}
	>
		<p class="hint">{m('vendors.invite.hint')}</p>
		<div class="fields">
			<label>
				<span>{m('vendors.invite.field.fullName')} <em class="required">*</em></span>
				<input type="text" bind:value={fullName} maxlength="255" required />
			</label>
			<label>
				<span>{m('vendors.invite.field.email')} <em class="required">*</em></span>
				<input type="email" bind:value={email} maxlength="320" required />
			</label>
		</div>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving || !canSubmit}>
				{saving ? m('vendors.invite.sending') : m('vendors.invite.send')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.hint {
		margin: 0 0 12px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.fields {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 13px;
	}
</style>
