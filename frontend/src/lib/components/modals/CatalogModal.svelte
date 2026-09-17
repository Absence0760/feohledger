<script lang="ts">
	import type { Catalog, CatalogItem, CatalogType } from '$lib/types/catalog';
	import { CATALOG_TYPES, CATALOG_TYPE_LABEL_KEYS } from '$lib/types/catalog';
	import { auth } from '$lib/stores/auth.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import VendorPicker from '$lib/components/ui/VendorPicker.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { normalizeMoneyInput } from '$lib/utils/moneyInput';
	import {
		createCatalog,
		updateCatalog,
		createCatalogItem,
		deleteCatalogItem,
		type GlAccountOption
	} from '$lib/api/catalogs';
	import { glAccountOptionLabel } from '$lib/types/glAccount';
	import { entityStore } from '$lib/stores/entity.svelte';

	let {
		catalog,
		glAccounts,
		onclose,
		onsaved
	}: {
		// null → create mode; a Catalog → detail/edit mode.
		catalog: Catalog | null;
		glAccounts: GlAccountOption[];
		onclose: () => void;
		onsaved: (c: Catalog) => void;
	} = $props();

	const isCreate = $derived(catalog === null);
	// create / update = admin / ap_manager.
	const canEdit = $derived(auth.isManager);

	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot */
	let name = $state(catalog?.name ?? '');
	let catalog_type = $state<CatalogType>((catalog?.catalog_type as CatalogType) ?? 'internal');
	let vendor_id = $state(catalog?.vendor_id ?? '');
	let punchout_url = $state(catalog?.punchout_url ?? '');
	let is_active = $state(catalog?.is_active ?? true);
	let is_preferred = $state(catalog?.is_preferred ?? false);
	let description = $state(catalog?.description ?? '');
	// Live item list (edit mode only) — refreshed from the returned Catalog.
	let items = $state<CatalogItem[]>(catalog?.items ?? []);
	/* eslint-enable svelte/state-referenced-locally */

	let saving = $state(false);
	let confirmDeleteItemId = $state<string | null>(null);

	// New-item form (edit mode only).
	let newName = $state('');
	let newSku = $state('');
	// The typed unit price stays RAW TEXT all the way to the wire —
	// `schemas/catalog.py::CatalogItemCreate.unit_price` is a `Decimal`, and
	// `json.loads` has already collapsed a fractional JSON number to a float by
	// the time pydantic sees it. `normalizeMoneyInput` decides shape with a
	// regex and never touches `Number`; text it cannot read is refused below
	// rather than silently added as a price-less item.
	let newPrice = $state<string | null>(null);
	let newCurrency = $state('USD');
	let newUom = $state('');
	let newCategory = $state('');
	let newVendorId = $state('');
	let newGlId = $state('');
	let addingItem = $state(false);

	function handleError(err: unknown, fallback: string) {
		toast(err instanceof Error ? err.message : fallback, 'error');
	}

	async function handleSave() {
		if (!name.trim()) return;
		saving = true;
		try {
			const payload = {
				name: name.trim(),
				catalog_type,
				vendor_id: vendor_id || null,
				punchout_url: catalog_type === 'punchout' ? punchout_url.trim() || null : null,
				is_active,
				is_preferred,
				description: description.trim() || null
			};
			const saved = isCreate
				? await createCatalog(payload)
				: await updateCatalog(catalog!.id, payload);
			items = saved.items;
			toast(isCreate ? m('catalogs.modal.toast.created') : m('catalogs.modal.toast.saved'), 'success');
			onsaved(saved);
			if (isCreate) onclose();
		} catch (err) {
			handleError(err, isCreate ? m('catalogs.modal.toast.createFailed') : m('catalogs.modal.toast.saveFailed'));
		} finally {
			saving = false;
		}
	}

	async function handleAddItem() {
		if (!catalog || !newName.trim()) return;
		const price = newPrice === null ? null : normalizeMoneyInput(newPrice);
		if (newPrice !== null && price === null) {
			toast(m('common.amountInvalid'), 'error');
			return;
		}
		addingItem = true;
		try {
			const created = await createCatalogItem(catalog.id, {
				name: newName.trim(),
				sku: newSku.trim() || null,
				description: null,
				unit_price: price,
				currency: newCurrency.trim() || 'USD',
				uom: newUom.trim() || null,
				vendor_id: newVendorId || null,
				gl_account_id: newGlId || null,
				category: newCategory.trim() || null,
				is_active: true
			});
			items = [...items, created];
			newName = '';
			newSku = '';
			newPrice = null;
			newUom = '';
			newCategory = '';
			newVendorId = '';
			newGlId = '';
			toast(m('catalogs.modal.toast.itemAdded'), 'success');
		} catch (err) {
			handleError(err, m('catalogs.modal.toast.itemAddFailed'));
		} finally {
			addingItem = false;
		}
	}

	async function handleDeleteItem(id: string) {
		if (confirmDeleteItemId !== id) {
			confirmDeleteItemId = id;
			return;
		}
		try {
			await deleteCatalogItem(id);
			items = items.filter((i) => i.id !== id);
			toast(m('catalogs.modal.toast.itemRemoved'), 'success');
		} catch (err) {
			handleError(err, m('catalogs.modal.toast.itemRemoveFailed'));
		} finally {
			confirmDeleteItemId = null;
		}
	}

	const modalTitle = $derived(
		isCreate
			? m('catalogs.modal.title.new')
			: canEdit
				? m('catalogs.modal.title.edit', { name: catalog!.name })
				: m('catalogs.modal.title.view', { name: catalog!.name })
	);
	const ariaLabel = $derived(isCreate ? m('catalogs.modal.aria.new') : m('catalogs.modal.aria.detail'));
</script>

<Modal open {ariaLabel} title={modalTitle} width="lg" {onclose}>
	<form onsubmit={(e) => { e.preventDefault(); handleSave(); }}>
		<div class="form-grid">
			<label>
				<span>{m('catalogs.modal.field.name')} <em class="required">*</em></span>
				<input type="text" bind:value={name} required disabled={!canEdit} />
			</label>
			<label>
				<span>{m('catalogs.modal.field.type')}</span>
				<select bind:value={catalog_type} disabled={!canEdit}>
					{#each CATALOG_TYPES as t}
						<option value={t}>{m(CATALOG_TYPE_LABEL_KEYS[t])}</option>
					{/each}
				</select>
			</label>
			<VendorPicker
				bind:value={vendor_id}
				label={m('catalogs.modal.field.vendor')}
				placeholder={m('catalogs.modal.field.vendorSelect')}
				selectedLabel={catalog?.vendor_name ?? null}
				disabled={!canEdit}
			/>
			{#if catalog_type === 'punchout'}
				<label>
					<span>{m('catalogs.modal.field.punchoutUrl')}</span>
					<input
						type="url"
						bind:value={punchout_url}
						placeholder={m('catalogs.modal.field.punchoutUrlPlaceholder')}
						disabled={!canEdit}
					/>
				</label>
			{/if}
			<label class="checkbox-label">
				<input type="checkbox" bind:checked={is_active} disabled={!canEdit} />
				<span>{m('catalogs.modal.field.active')}</span>
			</label>
			<label class="checkbox-label">
				<input type="checkbox" bind:checked={is_preferred} disabled={!canEdit} />
				<span>{m('catalogs.modal.field.preferred')}</span>
			</label>
			<label class="full-width">
				<span>{m('catalogs.modal.field.description')}</span>
				<textarea bind:value={description} rows="2" disabled={!canEdit}></textarea>
			</label>
		</div>

		{#if catalog_type === 'punchout'}
			<p class="hint">
				{m('catalogs.modal.punchoutHint')}
			</p>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('catalogs.modal.close')}</button>
			{#if canEdit}
				<button type="submit" class="btn-primary" disabled={saving}>
					{saving ? m('catalogs.modal.saving') : isCreate ? m('catalogs.modal.create') : m('catalogs.modal.save')}
				</button>
			{/if}
		</div>
	</form>

	<!-- Items section (edit mode + internal catalogs only) -->
	{#if !isCreate && catalog_type === 'internal'}
		<section class="items-section">
			<h3>{m('catalogs.modal.items')} <span class="muted">{m('catalogs.modal.itemCount', { count: items.length })}</span></h3>
			{#if items.length === 0}
				<p class="muted">{m('catalogs.modal.noItems')}</p>
			{:else}
				<table class="item-table">
					<thead>
						<tr>
							<th>{m('catalogs.modal.col.sku')}</th>
							<th>{m('catalogs.modal.col.name')}</th>
							<th>{m('catalogs.modal.col.category')}</th>
							<th class="right">{m('catalogs.modal.col.price')}</th>
							<th>{m('catalogs.modal.col.uom')}</th>
							{#if canEdit}<th></th>{/if}
						</tr>
					</thead>
					<tbody>
						{#each items as item (item.id)}
							<tr>
								<td class="mono">{item.sku ?? '—'}</td>
								<td>{item.name}</td>
								<td>{item.category ?? '—'}</td>
								<td class="right">
									{#if item.unit_price != null}
										<Money amount={item.unit_price} currency={item.currency} />
									{:else}
										—
									{/if}
								</td>
								<td>{item.uom ?? '—'}</td>
								{#if canEdit}
									<td class="actions">
										<RowAction
											variant="danger"
											armed={confirmDeleteItemId === item.id}
											onclick={() => handleDeleteItem(item.id)}
										>
											{confirmDeleteItemId === item.id ? m('catalogs.modal.item.confirm') : m('catalogs.modal.item.remove')}
										</RowAction>
									</td>
								{/if}
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}

			{#if canEdit}
				<div class="add-item">
					<input
						class="grow"
						type="text"
						placeholder={m('catalogs.modal.add.namePlaceholder')}
						aria-label={m('catalogs.modal.add.nameAria')}
						bind:value={newName}
					/>
					<input type="text" placeholder={m('catalogs.modal.add.skuPlaceholder')} aria-label={m('catalogs.modal.add.skuAria')} bind:value={newSku} />
					<input
						type="number"
						step="0.01"
						min="0"
						placeholder={m('catalogs.modal.add.pricePlaceholder')}
						aria-label={m('catalogs.modal.add.priceAria')}
						value={newPrice ?? ''}
						oninput={(e) => (newPrice = e.currentTarget.value.trim() || null)}
					/>
					<input
						class="cur"
						type="text"
						maxlength="3"
						placeholder={m('catalogs.modal.add.currencyPlaceholder')}
						aria-label={m('catalogs.modal.add.currencyAria')}
						bind:value={newCurrency}
					/>
					<input
						class="uom"
						type="text"
						placeholder={m('catalogs.modal.add.uomPlaceholder')}
						aria-label={m('catalogs.modal.add.uomAria')}
						bind:value={newUom}
					/>
					<input
						type="text"
						placeholder={m('catalogs.modal.add.categoryPlaceholder')}
						aria-label={m('catalogs.modal.add.categoryAria')}
						bind:value={newCategory}
					/>
					<VendorPicker
						bind:value={newVendorId}
						ariaLabel={m('catalogs.modal.add.vendorAria')}
						placeholder={m('catalogs.modal.add.vendorPlaceholder')}
						compact
					/>
					<select bind:value={newGlId} aria-label={m('catalogs.modal.add.glAria')}>
						<option value="">{m('catalogs.modal.add.glPlaceholder')}</option>
						{#each glAccounts as g (g.id)}
							<option value={g.id}>
								{glAccountOptionLabel(g, entityStore, {
									unknownEntity: m('glAccounts.scope.unknownEntity')
								})}
							</option>
						{/each}
					</select>
					<button
						type="button"
						class="btn-primary"
						disabled={addingItem || !newName.trim()}
						onclick={handleAddItem}
					>
						{addingItem ? m('catalogs.modal.add.adding') : m('catalogs.modal.add.add')}
					</button>
				</div>
			{/if}
		</section>
	{/if}
</Modal>

<style>
	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}
	.form-grid label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.form-grid label.full-width {
		grid-column: 1 / -1;
	}
	.form-grid label.checkbox-label {
		flex-direction: row;
		align-items: center;
		gap: 8px;
	}
	/* The TEXT-entry recipe, carved away from the checkbox/radio it also
	   reached. Svelte scopes this to `.form-grid.svelte-x input:where(.svelte-x)`,
	   which outranks the global control base in `app.css`, and `background:`
	   is a SHORTHAND — it reset `background-image`, the drawn tick, so the
	   Active / Preferred toggle rendered identically checked and unchecked. */
	.form-grid input:not([type='checkbox']):not([type='radio']),
	.form-grid select,
	.form-grid textarea {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}
	.form-grid input:disabled,
	.form-grid select:disabled,
	.form-grid textarea:disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.hint {
		margin: 10px 0 0;
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	.items-section {
		margin-top: 22px;
		border-top: 1px solid var(--border);
		padding-top: 16px;
	}
	.items-section h3 {
		margin: 0 0 10px;
		font-size: 0.95rem;
	}
	.muted {
		color: var(--text-muted);
		font-size: 0.82rem;
		font-weight: 400;
	}

	.item-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.84rem;
	}
	.item-table th,
	.item-table td {
		text-align: left;
		padding: 6px 8px;
		border-bottom: 1px solid var(--border);
	}
	.item-table th.right,
	.item-table td.right {
		text-align: right;
	}
	.item-table td.mono {
		font-family: var(--font-mono);
	}
	.item-table td.actions {
		display: flex;
		gap: 6px;
		white-space: nowrap;
	}

	.add-item {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 12px;
		align-items: center;
	}
	.add-item input,
	.add-item select {
		/* base look (border/colour/font/chevron) from the global select recipe */
		padding: 6px 30px 6px 8px;
		border-radius: 5px;
		font-size: 0.82rem;
		width: 110px;
	}
	.add-item input.grow {
		flex: 1;
		min-width: 150px;
	}
	.add-item input.cur {
		width: 56px;
	}
	.add-item input.uom {
		width: 70px;
	}
</style>
