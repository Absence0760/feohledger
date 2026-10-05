<script lang="ts">
	import type { Vendor } from '#lib/types/vendor.ts';
	import { createVendor } from '#lib/api/vendors.ts';
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let { onclose, onsaved }: { onclose: () => void; onsaved: (vendor: Vendor) => void } = $props();

	let name = $state('');
	let code = $state('');
	let email = $state('');
	let phone = $state('');
	let address = $state('');
	let tax_id = $state('');
	let saving = $state(false);

	const canSubmit = $derived(name.trim() !== '');

	async function handleSubmit() {
		if (!canSubmit || saving) return;
		saving = true;
		try {
			const vendor = await createVendor({
				name: name.trim(),
				code: code.trim() || null,
				email: email.trim() || null,
				phone: phone.trim() || null,
				address: address.trim() || null,
				tax_id: tax_id.trim() || null
			});
			toast(m('vendors.createModal.toast.created', { name: vendor.name }), 'success');
			onsaved(vendor);
			onclose();
		} catch (err) {
			toast(
				err instanceof Error ? err.message : m('vendors.createModal.toast.createFailed'),
				'error'
			);
		} finally {
			saving = false;
		}
	}
</script>

<Modal
	open
	ariaLabel={m('vendors.createModal.aria')}
	title={m('vendors.createModal.title')}
	width="md"
	{onclose}
>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			handleSubmit();
		}}
	>
		<div class="form-grid">
			<label class="full-width">
				<span>{m('vendors.createModal.field.name')} <em class="required">*</em></span>
				<input type="text" bind:value={name} maxlength="255" required />
			</label>
			<label>
				<span>{m('vendors.createModal.field.code')}</span>
				<input type="text" bind:value={code} maxlength="50" />
			</label>
			<label>
				<span>{m('vendors.createModal.field.taxId')}</span>
				<input type="text" bind:value={tax_id} maxlength="50" />
			</label>
			<label>
				<span>{m('vendors.createModal.field.email')}</span>
				<input type="email" bind:value={email} maxlength="320" />
			</label>
			<label>
				<span>{m('vendors.createModal.field.phone')}</span>
				<input type="text" bind:value={phone} maxlength="50" />
			</label>
			<label class="full-width">
				<span>{m('vendors.createModal.field.address')}</span>
				<textarea bind:value={address} maxlength="500" rows="2"></textarea>
			</label>
		</div>

		<p class="hint">{m('vendors.createModal.bankHint')}</p>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving || !canSubmit}>
				{saving ? m('vendors.createModal.saving') : m('vendors.createModal.create')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px 16px;
	}
	.full-width {
		grid-column: 1 / -1;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 13px;
	}
	.hint {
		margin: 12px 0 0;
		font-size: 12px;
		color: var(--text-muted);
	}
</style>
