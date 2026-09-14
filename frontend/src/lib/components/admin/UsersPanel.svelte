<script lang="ts">
	import type { AdminUser } from '$lib/types/admin';
	import { roleLabelKey } from '$lib/types/admin';
	import { adminStore } from '$lib/stores/admin.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import BulkBar from '$lib/components/ui/BulkBar.svelte';
	import BulkDeleteButton from '$lib/components/ui/BulkDeleteButton.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { formatDate } from '$lib/utils/time';
	import { formatList } from '$lib/utils/list';
	import { pruneSelection } from '$lib/utils/selection';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';

	let showCreateModal = $state(false);
	let editingUser = $state<AdminUser | null>(null);
	let createdCredentials = $state<{ email: string; password: string } | null>(null);
	let confirmDeleteId = $state<string | null>(null);

	// Bulk-select state
	let selectedIds = $state<Set<string>>(new Set());
	let bulkDeleting = $state(false);

	// Create form
	let newName = $state('');
	let newEmail = $state('');
	let newRoles = $state<string[]>([]);

	// Edit form
	let editName = $state('');
	let editEmail = $state('');
	let editRoles = $state<string[]>([]);
	let editPassword = $state('');

	let saving = $state(false);

	let search = $state('');
	let searchTimer: ReturnType<typeof setTimeout> | null = null;
	// Guards the search-effect below against firing its own redundant
	// fetch on mount — see the comment there.
	let searchEffectRan = false;

	$effect(() => {
		// Fire-and-forget: the store loaders re-throw so an awaiting caller keeps
		// its own handling, but nothing awaits here — the store's `errored` flag is
		// what the UI renders. Swallow so a failed load isn't an unhandled rejection.
		adminStore.fetchUsers().catch(() => {});
		adminStore.fetchRoles().catch(() => {});
	});

	$effect(() => {
		// React to search changes; debounce to avoid hammering the API.
		const q = search;
		// A Svelte `$effect` runs once on mount regardless of whether its
		// tracked value actually changed, so without this guard this effect
		// ALSO schedules a fetch (with search='') ~250ms after mount — on
		// top of the unconditional, immediate fetch the effect above just
		// issued. That redundant fetch is a real race, not just a wasted
		// request: `adminStore.fetchUsers()` always *replaces* the list
		// (never merges), so if a user creates/deletes a row in that
		// ~250ms window, the delayed duplicate GET can resolve afterward
		// and silently overwrite the just-mutated list with a stale,
		// page-1-only snapshot — the "table shows N rows instead of M"
		// flake. The mount effect above already covers the initial
		// (unfiltered) load, so skip this effect's very first run and only
		// debounce-fetch on a genuine search-value change thereafter.
		if (!searchEffectRan) {
			searchEffectRan = true;
			return;
		}
		if (searchTimer) clearTimeout(searchTimer);
		searchTimer = setTimeout(() => {
			// See the mount effect: `adminStore.errored` is what the table reads.
			adminStore.fetchUsers({ search: q }).catch(() => {});
		}, 250);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone and lands a stale list into the shared store.
		return () => {
			if (searchTimer) clearTimeout(searchTimer);
		};
	});

	async function loadMore() {
		await adminStore.loadMoreUsers({ search });
	}

	// Drop selected ids that fell off the list when a search refetch narrows it,
	// so the bulk-bar count and the bulk-delete id set never include rows the
	// user can no longer see. `pruneSelection` is a no-op (same Set) when clean.
	$effect(() => {
		const pruned = pruneSelection(
			selectedIds,
			adminStore.users.map((u) => u.id)
		);
		if (pruned !== selectedIds) selectedIds = pruned;
	});

	let selectableIds = $derived(
		adminStore.users.filter((u) => u.id !== auth.user?.id).map((u) => u.id)
	);
	let allSelected = $derived(
		selectableIds.length > 0 && selectableIds.every((id) => selectedIds.has(id))
	);

	function toggleSelect(id: string) {
		const next = new Set(selectedIds);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selectedIds = next;
	}

	function toggleSelectAll() {
		if (allSelected) {
			selectedIds = new Set();
		} else {
			selectedIds = new Set(selectableIds);
		}
	}

	async function handleBulkDelete() {
		if (selectedIds.size === 0) return;
		bulkDeleting = true;
		try {
			const result = await adminStore.bulkDeleteUsers([...selectedIds]);
			selectedIds = new Set();
			if (result.failed.length === 0) {
				toast(m('admin.users.toast.deleted', { n: result.deleted.length }), 'success');
			} else if (result.deleted.length === 0) {
				const reason = describeBulkFailure(result.failed[0]);
				toast(m('admin.users.toast.noneDeleted', { reason }), 'error');
			} else {
				toast(
					m('admin.users.toast.partialDeleted', {
						deleted: result.deleted.length,
						blocked: result.failed.length,
					}),
					'success',
				);
			}
		} catch (err) {
			toast(err instanceof Error ? err.message : m('admin.users.toast.bulkFailed'), 'error');
		} finally {
			bulkDeleting = false;
		}
	}

	function describeBulkFailure(f: { reason: string; references: { open_invoice_assignments: number; pending_approval_steps: number; active_workflow_approver_in: number } | null }): string {
		if (f.reason === 'self') return m('admin.users.fail.self');
		if (f.reason === 'not_found') return m('admin.users.fail.notFound');
		if (f.reason === 'blocked' && f.references) {
			const parts: string[] = [];
			if (f.references.open_invoice_assignments) parts.push(m('admin.users.fail.openInvoices', { n: f.references.open_invoice_assignments }));
			if (f.references.pending_approval_steps) parts.push(m('admin.users.fail.pendingApprovals', { n: f.references.pending_approval_steps }));
			if (f.references.active_workflow_approver_in) parts.push(m('admin.users.fail.activeWorkflows', { n: f.references.active_workflow_approver_in }));
			return m('admin.users.fail.referenced', { parts: formatList(parts) });
		}
		return m('admin.users.fail.blocked');
	}

	/** Open the Invite-User modal. Exposed so the tabbed host's PageHeader
	    action button can trigger it (the button lives outside this panel). */
	export function openCreate() {
		newName = '';
		newEmail = '';
		newRoles = [];
		showCreateModal = true;
	}

	function openEdit(user: AdminUser) {
		editingUser = user;
		editName = user.full_name;
		editEmail = user.email;
		editRoles = user.roles.map((r) => r.name);
		editPassword = '';
	}

	async function handleCreate() {
		if (!newName.trim() || !newEmail.trim()) return;
		saving = true;
		try {
			const result = await adminStore.createUser({
				full_name: newName.trim(),
				email: newEmail.trim(),
				role_names: newRoles,
			});
			showCreateModal = false;
			createdCredentials = {
				email: result.email,
				password: (result as unknown as { temporary_password: string }).temporary_password,
			};
		} catch (err) {
			toast(err instanceof Error ? err.message : m('admin.users.toast.createFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	async function handleUpdate() {
		if (!editingUser) return;
		saving = true;
		try {
			const changes: Record<string, unknown> = {
				full_name: editName.trim(),
				email: editEmail.trim(),
				role_names: editRoles,
			};
			if (editPassword.trim()) {
				changes.password = editPassword;
			}
			await adminStore.updateUser(editingUser.id, changes);
			toast(m('admin.users.toast.updated'), 'success');
			editingUser = null;
		} catch (err) {
			toast(err instanceof Error ? err.message : m('admin.users.toast.updateFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	async function toggleActive(user: AdminUser) {
		try {
			await adminStore.updateUser(user.id, { is_active: !user.is_active });
			toast(user.is_active ? m('admin.users.toast.deactivated') : m('admin.users.toast.activated'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('admin.users.toast.updateFailed'), 'error');
		}
	}

	async function handleDelete(id: string) {
		try {
			await adminStore.deleteUser(id);
			toast(m('admin.users.toast.singleDeleted'), 'success');
			confirmDeleteId = null;
		} catch (err) {
			toast(err instanceof Error ? err.message : m('admin.users.toast.deleteFailed'), 'error');
		}
	}

	function toggleRole(role: string, list: string[]): string[] {
		return list.includes(role) ? list.filter((r) => r !== role) : [...list, role];
	}

	/**
	 * A role's display name: the translated label for one of the four built-in
	 * roles, the stored name verbatim for a tenant's own custom role.
	 *
	 * The resolution rule lives in `types/admin.ts::roleLabelKey` so this panel,
	 * `RolesPanel` and `/profile` cannot each invent their own — printing the
	 * raw `ap_manager` on one of them is how this started.
	 */
	function roleLabel(name: string): string {
		const key = roleLabelKey(name);
		return key ? m(key) : name;
	}

	function handleWindowClick(e: MouseEvent) {
		if (confirmDeleteId && !(e.target as HTMLElement).closest('.row-action')) {
			confirmDeleteId = null;
		}
	}

	let isSelf = (id: string) => id === auth.user?.id;
</script>

<svelte:window onclick={handleWindowClick} />

<div class="filter-row">
	<SearchBox
		bind:value={search}
		placeholder={m('admin.users.search.placeholder')}
		ariaLabel={m('admin.users.search.aria')}
	/>
</div>

<BulkBar count={selectedIds.size} onclear={() => (selectedIds = new Set())}>
	{#snippet actions()}
		<BulkDeleteButton
			onconfirm={handleBulkDelete}
			disabled={bulkDeleting}
			label={m('admin.users.bulk.delete', { n: selectedIds.size })}
		/>
	{/snippet}
</BulkBar>

<!-- Three states, not one. The mount effect swallows the load rejection (the
     store's `errored` flag is what the UI renders — a flag the store did NOT
     have until now), so a 500 or an offline backend used to leave the user
     directory asserting "No users found." with no toast, no banner and no
     retry, on the page an admin opens precisely when access is misbehaving. -->
<DataTable
	isEmpty={adminStore.users.length === 0}
	empty={adminStore.loading
		? m('common.loading')
		: adminStore.errored
			? m('common.loadFailed')
			: m('admin.users.empty')}
	colspan={7}
>
	{#snippet header()}
		<tr>
			<th class="checkbox-col">
				<input
					type="checkbox"
					checked={allSelected}
					onchange={toggleSelectAll}
					aria-label={m('admin.users.selectAllAria')}
				/>
			</th>
			<th>{m('admin.users.col.name')}</th>
			<th>{m('admin.users.col.email')}</th>
			<th>{m('admin.users.col.roles')}</th>
			<th>{m('admin.users.col.status')}</th>
			<th>{m('admin.users.col.created')}</th>
			<th></th>
		</tr>
	{/snippet}
	{#snippet body()}
		{#each adminStore.users as user (user.id)}
			<tr
				class="clickable"
				class:row-muted={!user.is_active}
				class:inactive={!user.is_active}
				class:row-selected={selectedIds.has(user.id)}
				onclick={(e) => {
					if (isRowOpenClick(e)) openEdit(user);
				}}
			>
				<td class="checkbox-col">
					{#if !isSelf(user.id)}
						<input
							type="checkbox"
							checked={selectedIds.has(user.id)}
							onchange={() => toggleSelect(user.id)}
							aria-label={m('admin.users.selectAria', { name: user.full_name })}
						/>
					{/if}
				</td>
				<td class="name-cell">
					<RowLink onclick={() => openEdit(user)} ariaLabel={m('admin.users.editAria', { name: user.full_name })}>
						{user.full_name}
					</RowLink>
					{#if isSelf(user.id)}
						<span class="you-badge">{m('admin.users.you')}</span>
					{/if}
				</td>
				<td class="email-cell">{user.email}</td>
				<td>
					<div class="role-badges">
						{#each user.roles as role}
							<span class="role-badge">{roleLabel(role.name)}</span>
						{:else}
							<span class="no-roles">{m('admin.users.noRoles')}</span>
						{/each}
					</div>
				</td>
				<td>
					<span class="status-dot" class:active={user.is_active} class:deactivated={!user.is_active}>
						{user.is_active ? m('admin.users.statusActive') : m('admin.users.statusInactive')}
					</span>
				</td>
				<td class="date-cell">{formatDate(user.created_at)}</td>
				<td class="actions">
					<RowAction onclick={() => toggleActive(user)}>
						{user.is_active ? m('admin.users.row.deactivate') : m('admin.users.row.activate')}
					</RowAction>
					{#if !isSelf(user.id)}
						<RowAction
							variant="danger"
							armed={confirmDeleteId === user.id}
							onclick={(e) => {
								e.stopPropagation();
								if (confirmDeleteId === user.id) {
									handleDelete(user.id);
								} else {
									confirmDeleteId = user.id;
								}
							}}
						>
							{confirmDeleteId === user.id ? m('admin.users.row.confirm') : m('admin.users.row.delete')}
						</RowAction>
					{/if}
				</td>
			</tr>
		{/each}
	{/snippet}
</DataTable>

{#if adminStore.hasMore}
	<div class="load-more-row">
		<button class="btn-load-more" onclick={loadMore} disabled={adminStore.loading}>
			{adminStore.loading ? m('common.loading') : m('admin.users.loadMore', { shown: adminStore.users.length, total: adminStore.total })}
		</button>
	</div>
{:else if adminStore.total > 0}
	<div class="load-more-row">
		<span class="load-more-end">{m('admin.users.showingAll', { total: adminStore.total })}</span>
	</div>
{/if}

<!-- Invite User Modal -->
<Modal open={showCreateModal} ariaLabel={m('admin.users.modal.invite.aria')} width="sm" onclose={() => (showCreateModal = false)}>
	<h2>{m('admin.users.modal.invite.heading')}</h2>
	<p class="modal-hint">{m('admin.users.modal.invite.hint')}</p>
	<form onsubmit={(e) => { e.preventDefault(); handleCreate(); }}>
		<label>
			<span>{m('admin.users.field.fullName')} <em class="required">*</em></span>
			<input type="text" bind:value={newName} required />
		</label>
		<label>
			<span>{m('admin.users.field.email')} <em class="required">*</em></span>
			<input type="email" bind:value={newEmail} required />
		</label>
		<fieldset>
			<legend>{m('admin.users.roles.legend')}</legend>
			<p class="modal-hint">{m('admin.users.roles.hint')}</p>
			<div class="role-checks">
				{#each adminStore.roles as role}
					<label class="check-label">
						<input
							type="checkbox"
							checked={newRoles.includes(role.name)}
							onchange={() => (newRoles = toggleRole(role.name, newRoles))}
						/>
						{roleLabel(role.name)}
					</label>
				{/each}
			</div>
		</fieldset>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (showCreateModal = false)}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving}>
				{saving ? m('admin.users.modal.invite.creating') : m('admin.users.modal.invite.create')}
			</button>
		</div>
	</form>
</Modal>

<!-- Created Credentials Modal -->
<Modal open={createdCredentials !== null} ariaLabel={m('admin.users.modal.created.aria')} width="sm" onclose={() => (createdCredentials = null)}>
	{#if createdCredentials}
		<h2>{m('admin.users.modal.created.heading')}</h2>
		<p class="modal-hint">{m('admin.users.modal.created.hint')}</p>
		<div class="credentials-box">
			<div class="credential-row">
				<span class="credential-label">{m('admin.users.modal.created.email')}</span>
				<code class="credential-value">{createdCredentials.email}</code>
			</div>
			<div class="credential-row">
				<span class="credential-label">{m('admin.users.modal.created.tempPassword')}</span>
				<code class="credential-value password">{createdCredentials.password}</code>
			</div>
		</div>
		<div class="modal-footer">
			<button class="btn-primary" onclick={() => {
				navigator.clipboard.writeText(`Email: ${createdCredentials?.email}\nPassword: ${createdCredentials?.password}`);
				toast(m('admin.users.toast.copied'), 'success');
			}}>{m('admin.users.modal.created.copy')}</button>
			<button class="btn-cancel" onclick={() => (createdCredentials = null)}>{m('admin.users.modal.created.done')}</button>
		</div>
	{/if}
</Modal>

<!-- Edit User Modal -->
<Modal open={editingUser !== null} ariaLabel={m('admin.users.modal.edit.aria')} width="sm" onclose={() => (editingUser = null)}>
	{#if editingUser}
		<h2>{m('admin.users.modal.edit.heading')}</h2>
		<form onsubmit={(e) => { e.preventDefault(); handleUpdate(); }}>
			<label>
				<span>{m('admin.users.field.fullName')}</span>
				<input type="text" bind:value={editName} required />
			</label>
			<label>
				<span>{m('admin.users.field.email')}</span>
				<input type="email" bind:value={editEmail} required />
			</label>
			<label>
				<span>{m('admin.users.field.resetPassword')} <em class="hint">{m('admin.users.field.resetPasswordHint')}</em></span>
				<input type="password" bind:value={editPassword} minlength="6" />
			</label>
			<fieldset>
				<legend>{m('admin.users.roles.legend')}</legend>
				<p class="modal-hint">{m('admin.users.roles.hint')}</p>
				<div class="role-checks">
					{#each adminStore.roles as role}
						<label class="check-label">
							<input
								type="checkbox"
								checked={editRoles.includes(role.name)}
								onchange={() => (editRoles = toggleRole(role.name, editRoles))}
							/>
							{roleLabel(role.name)}
						</label>
					{/each}
				</div>
			</fieldset>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (editingUser = null)}>{m('common.cancel')}</button>
				<button type="submit" class="btn-primary" disabled={saving}>
					{saving ? m('admin.users.modal.edit.saving') : m('admin.users.modal.edit.save')}
				</button>
			</div>
		</form>
	{/if}
</Modal>

<style>
	/* Panel-specific styling; shared design-system CSS lives in app.css. */

	/* --- Bulk-row checkbox column (BulkBar / BulkDeleteButton come from $lib/components) --- */

	.checkbox-col {
		width: 36px;
		text-align: center;
		padding-left: 10px;
		padding-right: 4px;
	}

	.checkbox-col input[type='checkbox'] {
		cursor: pointer;
		accent-color: var(--accent);
	}

	/* --- Table cells --- */

	/* A deactivated user's row is de-emphasised by the shared `.row-muted`
	 * recipe in app.css (a muted colour token), not by an `opacity` fade —
	 * the fade put this row's email cell at 2.33:1 and its role badges at
	 * 2.32–2.43:1 against --surface.
	 *
	 * `.inactive` rides alongside it as a SELECTOR HOOK ONLY — it sets no
	 * colour here and no rule matches it. Two e2e specs assert on it
	 * (`admin/users.spec.ts`, `scim/provisioning.spec.ts`), which is exactly
	 * the "keep the semantic class if something reads it" carve-out app.css
	 * describes. Same split decisions §47 made for Badge: the semantic class
	 * is an identity for tests to select on, never the thing that paints. */

	.name-cell {
		font-weight: 500;
		display: flex;
		align-items: center;
		gap: 8px;
	}

	/* Not `<Badge>`: a tiny "this row is you" marker tucked beside the name in
	   a tight cell — a status pill's metrics would push the name around. (Also
	   the class itself is an e2e hook: three specs locate the current user's
	   row by it.) Only the colour literals are retired to the palette pair. */
	.you-badge {
		font-size: 0.68rem;
		font-weight: 600;
		padding: 1px 6px;
		border-radius: 8px;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
	}

	.email-cell {
		color: var(--text-muted);
	}

	.date-cell {
		color: var(--text-muted);
		font-size: 0.82rem;
		white-space: nowrap;
	}

	/* --- Roles --- */

	.role-badges {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	/* Not `<Badge>`: a role NAME, not a status — sentence-cased at whatever
	   capitalisation the role carries, several to a cell. Same call as the
	   matching chip in `RolesPanel`; only the colour literals are retired. */
	.role-badge {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 10px;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		font-size: 0.75rem;
		font-weight: 500;
		white-space: nowrap;
	}

	.no-roles {
		font-size: 0.78rem;
		color: var(--text-muted);
		font-style: italic;
	}

	/* --- Status --- */

	.status-dot {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 0.82rem;
		font-weight: 500;
	}

	.status-dot::before {
		content: '';
		width: 7px;
		height: 7px;
		border-radius: 50%;
	}

	.status-dot.active::before {
		background: #1fa86a;
	}

	.status-dot.deactivated::before {
		background: #e04040;
	}

	/* --- Modal extras --- */

	.modal input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	fieldset {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 10px 12px;
		margin: 0;
	}

	legend {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		padding: 0 4px;
	}

	.role-checks {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
	}

	.check-label {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 6px;
		font-size: 0.85rem;
		color: var(--text);
		cursor: pointer;
	}

	.check-label input[type='checkbox'] {
		accent-color: var(--accent);
		cursor: pointer;
	}

	.hint {
		font-style: italic;
		text-transform: none;
		letter-spacing: normal;
		font-weight: 400;
	}

	/* --- Credentials display --- */

	.credentials-box {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 14px;
		margin-bottom: 16px;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.credential-row {
		display: flex;
		flex-direction: column;
		gap: 3px;
	}

	.credential-label {
		font-size: 0.72rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.credential-value {
		font-size: 0.9rem;
		color: var(--text);
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
		background: var(--surface);
		padding: 4px 8px;
		border-radius: 4px;
		border: 1px solid var(--border);
		user-select: all;
	}

	.credential-value.password {
		color: var(--accent);
		font-weight: 600;
	}
</style>
