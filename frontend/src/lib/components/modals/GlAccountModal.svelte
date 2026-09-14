<script lang="ts">
	import { createGlAccount } from '$lib/api/glAccounts';
	import { GL_ACCOUNT_TYPES, GL_ACCOUNT_TYPE_LABEL_KEYS } from '$lib/types/glAccount';
	import Modal from '$lib/components/ui/Modal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { entityStore } from '$lib/stores/entity.svelte';
	import { m } from '$lib/i18n/store.svelte';

	let { onclose, onsaved }: { onclose: () => void; onsaved: () => void } = $props();

	let code = $state('');
	let name = $state('');
	let accountType = $state<string>('expense');
	let parentCode = $state('');
	let saving = $state(false);

	const canSubmit = $derived(code.trim() !== '' && name.trim() !== '');

	/**
	 * Which chart this account will land in, said out loud before it is created.
	 *
	 * The backend decides that from the live entity selection, not from the
	 * body (`api/gl_accounts.py::create_gl_account`): consolidated creates a
	 * SHARED account visible to every subsidiary, an entity selected creates
	 * one only that subsidiary sees. The difference is invisible in the form
	 * and unfixable afterwards — there is no PATCH on this router — so the
	 * form has to state it rather than let the sidebar switcher decide
	 * silently. Only shown on a multi-entity tenant, the same condition that
	 * gates the switcher itself: with one entity there is no second answer.
	 */
	const scopeHint = $derived(
		!entityStore.multiEntity
			? null
			: entityStore.selected
				? m('glAccounts.createModal.scopeEntity', { entity: entityStore.selected.name })
				: m('glAccounts.createModal.scopeShared')
	);

	async function handleSubmit() {
		if (!canSubmit || saving) return;
		saving = true;
		try {
			const created = await createGlAccount({
				code: code.trim(),
				name: name.trim(),
				account_type: accountType || null,
				parent_code: parentCode.trim() || null
			});
			toast(m('glAccounts.createModal.toast.created', { code: created.code }), 'success');
			onsaved();
			onclose();
		} catch (err) {
			// The backend's own message is the useful one here: a 409 names the
			// code AND the chart it already exists in, which is exactly what the
			// user has to change. Don't flatten it to a generic failure.
			toast(
				err instanceof Error ? err.message : m('glAccounts.createModal.toast.createFailed'),
				'error'
			);
		} finally {
			saving = false;
		}
	}
</script>

<Modal
	open
	ariaLabel={m('glAccounts.createModal.aria')}
	title={m('glAccounts.createModal.title')}
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
			<label>
				<span>{m('glAccounts.createModal.field.code')} <em class="required">*</em></span>
				<input type="text" bind:value={code} maxlength="50" required />
			</label>
			<label>
				<span>{m('glAccounts.createModal.field.type')}</span>
				<select bind:value={accountType}>
					{#each GL_ACCOUNT_TYPES as t (t)}
						<option value={t}>{m(GL_ACCOUNT_TYPE_LABEL_KEYS[t])}</option>
					{/each}
				</select>
			</label>
			<label class="full-width">
				<span>{m('glAccounts.createModal.field.name')} <em class="required">*</em></span>
				<input type="text" bind:value={name} maxlength="255" required />
			</label>
			<label>
				<span>{m('glAccounts.createModal.field.parentCode')}</span>
				<input type="text" bind:value={parentCode} maxlength="50" />
			</label>
		</div>

		{#if scopeHint}
			<p class="hint">{scopeHint}</p>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving || !canSubmit}>
				{saving ? m('common.saving') : m('glAccounts.createModal.create')}
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
