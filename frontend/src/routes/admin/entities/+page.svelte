<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { entityStore } from '#lib/stores/entity.svelte.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import {
		listEntities,
		createEntity,
		updateEntity,
		setDefaultEntity,
		type Entity
	} from '#lib/api/entities.ts';

	// RBAC: `GET /api/entities` is open to any authenticated user (the sidebar
	// switcher reads it), but POST / PATCH / set-default are
	// `require_roles(ROLE_ADMIN)`. The whole page is a mutation surface, so it
	// gates on admin like every sibling under /admin — a non-admin who lands
	// here would see a read-only table whose every control 403s. Wait for
	// `auth.user` to resolve before redirecting so we don't bounce before /me
	// lands (the api-keys / billing / audit pages document the same race).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	// $derived so the column headers re-render when the locale changes.
	let COLUMNS = $derived([
		{ label: m('admin.entities.col.name') },
		{ label: m('admin.entities.col.slug') },
		{ label: m('admin.entities.col.currency') },
		{ label: m('admin.entities.col.status') },
		{ class: 'actions-col' }
	]);

	let entities = $state<Entity[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Create flow. `createError` renders the backend's own refusal inline in the
	// modal — a duplicate slug (409) and a malformed slug (400) both arrive as
	// readable `detail` text, and re-stating them here would let our copy drift
	// from the rule the server actually enforces.
	let creating = $state(false);
	let newName = $state('');
	let newSlug = $state('');
	let newCurrency = $state('');
	let createError = $state<string | null>(null);
	let saving = $state(false);

	// Edit flow — same inline-error contract. Deactivating the default entity is
	// the refusal that matters here (400, "The default entity cannot be
	// deactivated."): the default is the home for un-scoped and new rows.
	let editing = $state<Entity | null>(null);
	let editName = $state('');
	let editCurrency = $state('');
	let editActive = $state(true);
	let editError = $state<string | null>(null);
	let updating = $state(false);

	// Make-default row action (armed two-click, mirroring the api-keys revoke).
	let confirmDefaultId = $state<string | null>(null);

	/** Auto-suggest a slug while the admin types a name, until they edit the
	 *  slug themselves — then leave their value alone. */
	let slugTouched = $state(false);
	function slugify(value: string): string {
		return value
			.toLowerCase()
			.replace(/[^a-z0-9]+/g, '-')
			.replace(/^-+|-+$/g, '');
	}

	async function load() {
		loading = true;
		error = null;
		try {
			entities = await listEntities();
		} catch (e) {
			error = e instanceof Error ? e.message : m('admin.entities.loadFailed');
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		// Only fetch once we know the role is allowed (avoids a wasted round trip
		// for a non-admin before the redirect fires).
		if (userLoaded && allowed) load();
	});

	/**
	 * Re-sync the sidebar switcher after any mutation. The store caches its list
	 * behind a `#loaded` flag, so without this the second entity an admin creates
	 * here wouldn't surface the switcher until a full page reload — which is
	 * exactly the end-to-end path this page exists to unblock.
	 */
	async function refreshSwitcher() {
		entityStore.reset();
		await entityStore.ensureLoaded();
	}

	function openCreate() {
		newName = '';
		newSlug = '';
		newCurrency = '';
		createError = null;
		slugTouched = false;
		creating = true;
	}

	async function handleCreate() {
		const name = newName.trim();
		const slug = newSlug.trim();
		if (!name || !slug) return;
		saving = true;
		createError = null;
		try {
			await createEntity({
				name,
				slug,
				currency: newCurrency.trim() ? newCurrency.trim().toUpperCase() : null
			});
			creating = false;
			toast(m('admin.entities.toast.created'), 'success');
			await load();
			await refreshSwitcher();
		} catch (e) {
			createError = e instanceof Error ? e.message : m('admin.entities.toast.createFailed');
		} finally {
			saving = false;
		}
	}

	function openEdit(entity: Entity) {
		editing = entity;
		editName = entity.name;
		editCurrency = entity.currency ?? '';
		editActive = entity.is_active;
		editError = null;
	}

	async function handleUpdate() {
		if (!editing) return;
		const name = editName.trim();
		if (!name) return;
		updating = true;
		editError = null;
		try {
			await updateEntity(editing.id, {
				name,
				currency: editCurrency.trim() ? editCurrency.trim().toUpperCase() : null,
				is_active: editActive
			});
			editing = null;
			toast(m('admin.entities.toast.updated'), 'success');
			await load();
			await refreshSwitcher();
		} catch (e) {
			editError = e instanceof Error ? e.message : m('admin.entities.toast.updateFailed');
		} finally {
			updating = false;
		}
	}

	async function handleSetDefault(id: string) {
		try {
			await setDefaultEntity(id);
			toast(m('admin.entities.toast.defaultChanged'), 'success');
			await load();
			await refreshSwitcher();
		} catch (e) {
			toast(
				e instanceof Error ? e.message : m('admin.entities.toast.defaultFailed'),
				'error'
			);
		} finally {
			confirmDefaultId = null;
		}
	}

	function handleWindowClick(e: MouseEvent) {
		if (confirmDefaultId && !(e.target as HTMLElement).closest('.row-action')) {
			confirmDefaultId = null;
		}
	}
</script>

<svelte:window onclick={handleWindowClick} />

<PageHeader title={m('admin.entities.title')}>
	{#snippet actions()}
		<button class="btn-primary" onclick={openCreate}>{m('admin.entities.createEntity')}</button>
	{/snippet}

	<p class="page-hint">{m('admin.entities.hint')}</p>

	{#if loading}
		<p class="state" data-testid="entities-loading">{m('admin.entities.loading')}</p>
	{:else if error}
		<div class="state error" data-testid="entities-error" role="alert">
			<p>{error}</p>
			<button type="button" class="btn-cancel" onclick={load}>{m('admin.entities.retry')}</button>
		</div>
	{:else}
		<DataTable columns={COLUMNS} isEmpty={entities.length === 0} empty={m('admin.entities.empty')}>
			{#snippet body()}
				{#each entities as entity (entity.id)}
					<tr
						class="clickable"
						class:row-muted={!entity.is_active}
						data-testid="entity-row"
						data-slug={entity.slug}
						onclick={(e) => {
							if (isRowOpenClick(e)) openEdit(entity);
						}}
					>
						<td>
							<RowLink
								onclick={() => openEdit(entity)}
								ariaLabel={m('admin.entities.editAria', { name: entity.name })}
							>
								{entity.name}
							</RowLink>
						</td>
						<td class="mono">{entity.slug}</td>
						<td>{entity.currency ?? m('admin.entities.currencyInherited')}</td>
						<td class="status-col">
							<span class="status-pills">
								{#if entity.is_default}
									<Badge tone="accent" variant="default">{m('admin.entities.statusDefault')}</Badge>
								{/if}
								{#if entity.is_active}
									<Badge tone="success" variant="active">{m('admin.entities.statusActive')}</Badge>
								{:else}
									<Badge tone="muted" variant="inactive">{m('admin.entities.statusInactive')}</Badge>
								{/if}
							</span>
						</td>
						<td class="actions">
							{#if !entity.is_default}
								<RowAction
									armed={confirmDefaultId === entity.id}
									onclick={(e) => {
										e.stopPropagation();
										if (confirmDefaultId === entity.id) {
											handleSetDefault(entity.id);
										} else {
											confirmDefaultId = entity.id;
										}
									}}
								>
									{confirmDefaultId === entity.id
										? m('admin.entities.row.confirm')
										: m('admin.entities.row.makeDefault')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{/if}
</PageHeader>

<!-- Create entity modal -->
<Modal
	open={creating}
	ariaLabel={m('admin.entities.create.aria')}
	width="sm"
	onclose={() => (creating = false)}
>
	<h2>{m('admin.entities.create.heading')}</h2>
	<p class="modal-hint">{m('admin.entities.create.hint')}</p>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			handleCreate();
		}}
	>
		<label>
			<span>{m('admin.entities.field.name')} <em class="required">*</em></span>
			<input
				type="text"
				bind:value={newName}
				oninput={() => {
					if (!slugTouched) newSlug = slugify(newName);
				}}
				required
				maxlength="255"
				data-testid="entity-name-input"
				placeholder={m('admin.entities.field.namePlaceholder')}
			/>
		</label>
		<label>
			<span>{m('admin.entities.field.slug')} <em class="required">*</em></span>
			<input
				type="text"
				bind:value={newSlug}
				oninput={() => (slugTouched = true)}
				required
				maxlength="100"
				data-testid="entity-slug-input"
				placeholder={m('admin.entities.field.slugPlaceholder')}
			/>
			<small class="field-hint">{m('admin.entities.field.slugHint')}</small>
		</label>
		<label>
			<span>{m('admin.entities.field.currency')}</span>
			<input
				type="text"
				bind:value={newCurrency}
				maxlength="3"
				data-testid="entity-currency-input"
				placeholder={m('admin.entities.field.currencyPlaceholder')}
			/>
			<small class="field-hint">{m('admin.entities.field.currencyHint')}</small>
		</label>

		{#if createError}
			<div class="state error" role="alert" data-testid="entity-create-error">{createError}</div>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (creating = false)}>
				{m('common.cancel')}
			</button>
			<button
				type="submit"
				class="btn-primary"
				disabled={!newName.trim() || !newSlug.trim() || saving}
			>
				{saving ? m('admin.entities.create.creating') : m('admin.entities.create.create')}
			</button>
		</div>
	</form>
</Modal>

<!-- Edit entity modal -->
<Modal
	open={editing !== null}
	ariaLabel={m('admin.entities.edit.aria')}
	width="sm"
	onclose={() => (editing = null)}
>
	{#if editing}
		<h2>{m('admin.entities.edit.heading', { name: editing.name })}</h2>
		<p class="modal-hint">{m('admin.entities.edit.hint')}</p>
		<form
			onsubmit={(e) => {
				e.preventDefault();
				handleUpdate();
			}}
		>
			<label>
				<span>{m('admin.entities.field.name')} <em class="required">*</em></span>
				<input
					type="text"
					bind:value={editName}
					required
					maxlength="255"
					data-testid="entity-edit-name-input"
				/>
			</label>
			<label>
				<span>{m('admin.entities.field.currency')}</span>
				<input
					type="text"
					bind:value={editCurrency}
					maxlength="3"
					data-testid="entity-edit-currency-input"
				/>
				<small class="field-hint">{m('admin.entities.field.currencyHint')}</small>
			</label>
			<label class="check">
				<input type="checkbox" bind:checked={editActive} data-testid="entity-edit-active" />
				<span>{m('admin.entities.field.active')}</span>
			</label>
			<!--
				The checkbox stays enabled on the default entity. The server is the
				authority on that rule (400, "The default entity cannot be
				deactivated.") and restating it as a disabled control would hide the
				reason behind a dead input — the admin would see something they can't
				click and no explanation of why. Attempting it renders the server's
				own sentence below instead.
			-->
			{#if editing.is_default}
				<p class="field-hint default-note">{m('admin.entities.edit.defaultNote')}</p>
			{/if}

			{#if editError}
				<div class="state error" role="alert" data-testid="entity-edit-error">{editError}</div>
			{/if}

			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (editing = null)}>
					{m('common.cancel')}
				</button>
				<button type="submit" class="btn-primary" disabled={!editName.trim() || updating}>
					{updating ? m('admin.entities.edit.saving') : m('admin.entities.edit.save')}
				</button>
			</div>
		</form>
	{/if}
</Modal>

<style>
	.page-hint {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.85rem;
		max-width: 720px;
	}

	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}

	.state.error {
		color: #f06464;
	}

	.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
	}

	/* Default + Active are two independent facts about a row, so the cell carries
	   both pills rather than collapsing them into one status word. The flex box
	   is an inner span, not the `<td>` itself — a table cell taken out of the
	   table layout algorithm loses its row's vertical alignment. */
	.status-pills {
		display: inline-flex;
		gap: 0.35rem;
		flex-wrap: wrap;
		align-items: center;
	}

	.field-hint {
		display: block;
		margin-top: 0.25rem;
		color: var(--text-muted);
		font-size: 0.75rem;
	}

	.default-note {
		margin: 0.25rem 0 0;
	}

	.check {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 0.5rem;
	}

	.check input {
		width: auto;
	}
</style>
