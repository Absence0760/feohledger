<script lang="ts">
	import { adminStore } from '$lib/stores/admin.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { roleLabelKey } from '$lib/types/admin';
	import type { Role } from '$lib/types/admin';

	// $derived so the column headers re-render when the locale changes.
	let SYSTEM_COLUMNS = $derived([{ label: m('admin.roles.col.name') }, { label: m('admin.roles.col.description') }]);
	let CUSTOM_COLUMNS = $derived([
		{ label: m('admin.roles.col.name') },
		{ label: m('admin.roles.col.description') },
		{ label: m('admin.roles.col.permissions') },
		{ class: 'actions-col' }
	]);

	let creating = $state(false);
	let newName = $state('');
	let newDescription = $state('');
	let newPermissions = $state<Set<string>>(new Set());

	let editing = $state<Role | null>(null);
	let editDescription = $state('');
	let editPermissions = $state<Set<string>>(new Set());
	let confirmDeleteId = $state<string | null>(null);
	let saving = $state(false);

	$effect(() => {
		// Fire-and-forget: the store loaders re-throw so an awaiting caller keeps
		// its own handling, but nothing awaits here — the store's `errored` flag is
		// what the UI renders. Swallow so a failed load isn't an unhandled rejection.
		adminStore.fetchRoles().catch(() => {});
		adminStore.fetchPermissionCatalog().catch(() => {});
	});

	/**
	 * A role's display name — the same resolver `UsersPanel` and `/profile` use
	 * (`types/admin.ts::roleLabelKey`).
	 *
	 * Both tables read it, and that is deliberate rather than only the system
	 * one: a custom role is never in the key map (the backend reserves the four
	 * built-in names), so the call states the rule — built-in roles are copy,
	 * a tenant's own role name is data — instead of encoding it in which table
	 * calls which. The system table printed `ap_manager` while the user rows one
	 * tab away printed `AP Manager`.
	 */
	function roleLabel(name: string): string {
		const key = roleLabelKey(name);
		return key ? m(key) : name;
	}

	function togglePermission(set: Set<string>, key: string): Set<string> {
		const next = new Set(set);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		return next;
	}

	/** Open the Create-role modal. Exposed so the tabbed host's PageHeader
	    action button can trigger it (the button lives outside this panel). */
	export function openCreate() {
		newName = '';
		newDescription = '';
		newPermissions = new Set();
		creating = true;
	}

	async function handleCreate() {
		const name = newName.trim();
		if (!name) return;
		saving = true;
		try {
			await adminStore.createRole({
				name,
				description: newDescription.trim() || undefined,
				permissions: [...newPermissions],
			});
			toast(m('admin.roles.toast.created', { name }), 'success');
			creating = false;
			newName = '';
			newDescription = '';
			newPermissions = new Set();
		} catch (err) {
			toast(extractError(err), 'error');
		} finally {
			saving = false;
		}
	}

	function openEdit(role: Role) {
		editing = role;
		editDescription = role.description ?? '';
		editPermissions = new Set(role.permissions ?? []);
	}

	async function handleEdit() {
		if (!editing) return;
		saving = true;
		try {
			await adminStore.updateRole(editing.id, {
				description: editDescription.trim() || undefined,
				permissions: [...editPermissions],
			});
			toast(m('admin.roles.toast.updated'), 'success');
			editing = null;
		} catch (err) {
			toast(extractError(err), 'error');
		} finally {
			saving = false;
		}
	}

	function permLabels(role: Role): string[] {
		const labels = adminStore.permissionCatalog;
		return (role.permissions ?? []).map(
			(k) => labels.find((p) => p.key === k)?.label ?? k
		);
	}

	async function handleDelete(id: string) {
		try {
			await adminStore.deleteRole(id);
			toast(m('admin.roles.toast.deleted'), 'success');
		} catch (err) {
			toast(extractError(err), 'error');
		} finally {
			confirmDeleteId = null;
		}
	}

	function handleWindowClick(e: MouseEvent) {
		if (
			confirmDeleteId &&
			!(e.target as HTMLElement).closest('.row-action')
		) {
			confirmDeleteId = null;
		}
	}

	function extractError(err: unknown): string {
		const e = err as { detail?: string; message?: string } | null;
		return e?.detail ?? e?.message ?? m('admin.roles.toast.requestFailed');
	}

	let systemRoles = $derived(adminStore.roles.filter((r) => r.is_system));
	let customRoles = $derived(adminStore.roles.filter((r) => !r.is_system));
</script>

<svelte:window onclick={handleWindowClick} />

<section class="role-section">
	<div class="section-header">
		<h2>{m('admin.roles.system.heading')}</h2>
		<p class="section-hint">
			{m('admin.roles.system.hint')}
		</p>
	</div>
	<DataTable
	columns={SYSTEM_COLUMNS}
	isEmpty={systemRoles.length === 0}
	empty={adminStore.rolesErrored ? m('common.loadFailed') : m('admin.roles.system.empty')}
>
		{#snippet body()}
			{#each systemRoles as role (role.id)}
				<tr>
					<td>
						<span class="role-badge system">{roleLabel(role.name)}</span>
					</td>
					<td class="muted-cell">{role.description ?? '—'}</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>
</section>

<section class="role-section">
	<div class="section-header">
		<h2>{m('admin.roles.custom.heading')}</h2>
		<p class="section-hint">
			{m('admin.roles.custom.hintPre')}
			<strong>{m('admin.roles.custom.hintPermissions')}</strong>
			{m('admin.roles.custom.hintPost')}
		</p>
	</div>
	<DataTable
	columns={CUSTOM_COLUMNS}
	isEmpty={customRoles.length === 0}
	empty={adminStore.rolesErrored ? m('common.loadFailed') : m('admin.roles.custom.empty')}
>
		{#snippet body()}
			{#each customRoles as role (role.id)}
				<tr>
					<td>
						<span class="role-badge">{roleLabel(role.name)}</span>
					</td>
					<td class="muted-cell">{role.description ?? '—'}</td>
					<td>
						{#if permLabels(role).length}
							<div class="perm-chips">
								{#each permLabels(role) as label (label)}
									<span class="perm-chip">{label}</span>
								{/each}
							</div>
						{:else}
							<span class="muted-cell">{m('admin.roles.noPermissions')}</span>
						{/if}
					</td>
					<td class="actions">
						<RowAction onclick={() => openEdit(role)}>{m('admin.roles.row.edit')}</RowAction>
						<RowAction
							variant="danger"
							armed={confirmDeleteId === role.id}
							onclick={(e) => {
								e.stopPropagation();
								if (confirmDeleteId === role.id) {
									handleDelete(role.id);
								} else {
									confirmDeleteId = role.id;
								}
							}}
						>
							{confirmDeleteId === role.id ? m('admin.roles.row.confirm') : m('admin.roles.row.delete')}
						</RowAction>
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>
</section>

<Modal
	open={creating}
	ariaLabel={m('admin.roles.modal.create.aria')}
	width="sm"
	onclose={() => (creating = false)}
>
	<h2>{m('admin.roles.modal.create.heading')}</h2>
	<p class="modal-hint">
		{m('admin.roles.modal.create.hint')}
	</p>
	<form onsubmit={(e) => { e.preventDefault(); handleCreate(); }}>
		<label>
			<span>{m('admin.roles.field.name')}</span>
			<input type="text" bind:value={newName} required maxlength="50" placeholder={m('admin.roles.field.namePlaceholder')} />
		</label>
		<label>
			<span>{m('admin.roles.field.description')}</span>
			<input type="text" bind:value={newDescription} maxlength="255" placeholder={m('admin.roles.field.descriptionPlaceholder')} />
		</label>
		<fieldset class="perm-fieldset">
			<legend>{m('admin.roles.field.permissions')}</legend>
			{#each adminStore.permissionCatalog as perm (perm.key)}
				<label class="perm-option">
					<input
						type="checkbox"
						checked={newPermissions.has(perm.key)}
						onchange={() => (newPermissions = togglePermission(newPermissions, perm.key))}
					/>
					<span>{perm.label}</span>
				</label>
			{/each}
		</fieldset>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (creating = false)}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={!newName.trim() || saving}>
				{saving ? m('admin.roles.modal.create.creating') : m('admin.roles.modal.create.create')}
			</button>
		</div>
	</form>
</Modal>

<Modal
	open={editing !== null}
	ariaLabel={m('admin.roles.modal.edit.aria')}
	width="sm"
	onclose={() => (editing = null)}
>
	{#if editing}
		<h2>{m('admin.roles.modal.edit.heading', { name: editing.name })}</h2>
		<p class="modal-hint">
			{m('admin.roles.modal.edit.hint')}
		</p>
		<form onsubmit={(e) => { e.preventDefault(); handleEdit(); }}>
			<label>
				<span>{m('admin.roles.field.description')}</span>
				<input type="text" bind:value={editDescription} maxlength="255" />
			</label>
			<fieldset class="perm-fieldset">
				<legend>{m('admin.roles.field.permissions')}</legend>
				{#each adminStore.permissionCatalog as perm (perm.key)}
					<label class="perm-option">
						<input
							type="checkbox"
							checked={editPermissions.has(perm.key)}
							onchange={() => (editPermissions = togglePermission(editPermissions, perm.key))}
						/>
						<span>{perm.label}</span>
					</label>
				{/each}
			</fieldset>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (editing = null)}>{m('common.cancel')}</button>
				<button type="submit" class="btn-primary" disabled={saving}>
					{saving ? m('admin.roles.modal.edit.saving') : m('admin.roles.modal.edit.save')}
				</button>
			</div>
		</form>
	{/if}
</Modal>

<style>
	/* Panel-specific styling; shared design-system CSS lives in app.css. */
	.role-section {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.section-header h2 {
		margin: 0 0 2px;
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--text);
	}

	.section-hint {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.82rem;
	}

	.muted-cell {
		color: var(--text-muted);
	}

	/* Not `<Badge>`: this is a role NAME, not a status — sentence-cased at the
	   user's own capitalisation, and uppercasing it would misrender the name.
	   Only the colour literals are retired to the palette pairs; a system role
	   stays grey and a custom one accent, so the two tables stay tellable
	   apart. */
	.role-badge {
		display: inline-block;
		padding: 2px 10px;
		border-radius: 10px;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		font-size: 0.78rem;
		font-weight: 500;
		white-space: nowrap;
	}

	.role-badge.system {
		background: var(--muted-tint);
		color: var(--muted-on-tint);
	}

	.perm-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	/* Not `<Badge>`: a permission TAG nested under a role, several to a cell.
	   It stays smaller and lighter-weight than `.role-badge` above so the
	   hierarchy reads; that difference now lives in the metrics rather than in
	   a two-hundredths-of-an-alpha step nobody could see. */
	.perm-chip {
		display: inline-block;
		padding: 1px 8px;
		border-radius: 8px;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		font-size: 0.72rem;
		white-space: nowrap;
	}

	.perm-fieldset {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 10px 12px;
		margin: 4px 0 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.perm-fieldset legend {
		padding: 0 6px;
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--text-muted);
	}

	.perm-option {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 8px;
		font-size: 0.85rem;
		cursor: pointer;
	}

	.perm-option input {
		width: auto;
		margin: 0;
	}
</style>
