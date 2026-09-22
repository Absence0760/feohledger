<script lang="ts">
	import { createGlAccount, updateGlAccount, type GlAccountUpdate } from '$lib/api/glAccounts';
	import { GL_ACCOUNT_TYPES, glAccountTypeLabelKey, type GlAccount } from '$lib/types/glAccount';
	import Modal from '$lib/components/ui/Modal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { entityStore } from '$lib/stores/entity.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { untrack } from 'svelte';

	/**
	 * Create one chart-of-accounts row, or — given `account` — correct one
	 * (`PATCH /api/gl-accounts/{id}`). Retiring is a row action on
	 * `/gl-accounts`, not a field here: it is a status change with its own
	 * audit action, not an edit.
	 */
	let {
		account,
		onclose,
		onsaved
	}: { account?: GlAccount; onclose: () => void; onsaved: () => void } = $props();

	// `account` is fixed for the life of the dialog (the page mounts a fresh
	// one per row), so reading it ONCE to seed the form is the intent —
	// `untrack` says so rather than leaving it to look like a missed `$derived`.
	const initial = untrack(() => account);
	const editing = initial !== undefined;
	let code = $state(initial?.code ?? '');
	let name = $state(initial?.name ?? '');
	let accountType = $state<string>(editing ? (initial?.account_type ?? '') : 'expense');
	let parentCode = $state(initial?.parent_code ?? '');
	let saving = $state(false);
	/** The backend's refusal, verbatim — a parent-cycle 422 or the 403 that
	 *  names the view to edit a shared row from. Inline, not a toast, because
	 *  it is exactly what the user has to change before trying again. */
	let saveError = $state<string | null>(null);

	/**
	 * An ERP sync writes whatever type its chart says, so an account being
	 * edited may carry one outside the documented four, or none. Both stay
	 * selectable: saving an untouched form must never rewrite the type.
	 */
	const typeOptions: string[] =
		initial?.account_type && !(GL_ACCOUNT_TYPES as readonly string[]).includes(initial.account_type)
			? [...GL_ACCOUNT_TYPES, initial.account_type]
			: [...GL_ACCOUNT_TYPES];

	/** A type this build has no wording for renders raw, as the list does. */
	function typeOptionLabel(t: string): string {
		const key = glAccountTypeLabelKey(t);
		return key ? m(key) : t;
	}

	const canSubmit = $derived(code.trim() !== '' && name.trim() !== '');

	/**
	 * Which chart this account will land in, said out loud before it is created.
	 *
	 * The backend decides that from the live entity selection, not from the
	 * body (`api/gl_accounts.py::create_gl_account`): consolidated creates a
	 * SHARED account visible to every subsidiary, an entity selected creates
	 * one only that subsidiary sees. The difference is invisible in the form
	 * and cannot be corrected afterwards — the PATCH deliberately changes
	 * neither the code nor the chart (a move is a create plus a retire) — so
	 * the form has to state it rather than let the sidebar switcher decide
	 * silently. Only shown on a multi-entity tenant, the same condition that
	 * gates the switcher itself: with one entity there is no second answer.
	 */
	const scopeHint = $derived(
		editing || !entityStore.multiEntity
			? null
			: entityStore.selected
				? m('glAccounts.createModal.scopeEntity', { entity: entityStore.selected.name })
				: m('glAccounts.createModal.scopeShared')
	);

	/** Only the fields that changed — the PATCH leaves unset ones alone. */
	function changes(): GlAccountUpdate {
		if (!initial) return {};
		const body: GlAccountUpdate = {};
		if (name.trim() !== initial.name) body.name = name.trim();
		const type = accountType || null;
		if (type !== initial.account_type) body.account_type = type;
		const parent = parentCode.trim() || null;
		if (parent !== initial.parent_code) body.parent_code = parent;
		return body;
	}

	async function handleSubmit() {
		if (!canSubmit || saving) return;
		saving = true;
		saveError = null;
		try {
			if (initial) {
				const body = changes();
				if (Object.keys(body).length > 0) {
					await updateGlAccount(initial.id, body);
					toast(m('glAccounts.editModal.toast.saved', { code: initial.code }), 'success');
					onsaved();
				}
				onclose();
				return;
			}
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
			if (initial) {
				saveError = err instanceof Error ? err.message : m('glAccounts.row.toast.failed');
				return;
			}
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
	ariaLabel={editing ? m('glAccounts.editModal.aria') : m('glAccounts.createModal.aria')}
	title={editing ? m('glAccounts.editModal.title') : m('glAccounts.createModal.title')}
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
				<span>{m('glAccounts.createModal.field.code')} {#if !editing}<em class="required">*</em>{/if}</span>
				<input
					type="text"
					bind:value={code}
					maxlength="50"
					required={!editing}
					readonly={editing}
					aria-describedby={editing ? 'gl-code-fixed' : undefined}
				/>
			</label>
			<label>
				<span>{m('glAccounts.createModal.field.type')}</span>
				<select bind:value={accountType}>
					{#if editing && !initial?.account_type}
						<option value="">—</option>
					{/if}
					{#each typeOptions as t (t)}
						<option value={t}>{typeOptionLabel(t)}</option>
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

		{#if editing}
			<p class="hint" id="gl-code-fixed">{m('glAccounts.editModal.codeFixed')}</p>
		{/if}
		{#if scopeHint}
			<p class="hint">{scopeHint}</p>
		{/if}
		{#if saveError}
			<p class="error" role="alert" data-testid="gl-account-save-error">{saveError}</p>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving || !canSubmit}>
				{saving
					? m('common.saving')
					: editing
						? m('glAccounts.editModal.save')
						: m('glAccounts.createModal.create')}
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
	input[readonly] {
		background: var(--bg);
		color: var(--text-muted);
	}
	.hint {
		margin: 12px 0 0;
		font-size: 12px;
		color: var(--text-muted);
	}
	.error {
		margin: 12px 0 0;
		font-size: 13px;
		color: var(--danger);
	}
</style>
