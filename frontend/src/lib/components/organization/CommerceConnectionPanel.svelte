<script lang="ts">
	/**
	 * Organization → Commerce: connect the business's commerce / inventory
	 * platform, map where received stock lands, and override SKU lookups
	 * (decisions §273). Recording a goods receipt then pushes each received
	 * line's quantity to the platform.
	 *
	 * The catalogue (`GET /api/commerce/providers`) drives everything the form
	 * offers; this component hardcodes no platform. Secrets are write-only and
	 * sealed server-side (decisions §266): the configuration saves through
	 * `PUT /api/commerce/settings`, the typed secrets through
	 * `PUT /api/organization/credentials/commerce` (`#lib/api/providerCredentials.ts`),
	 * and the settings read says which secret NAMES are stored. An input starts
	 * blank with a "saved" hint; a blank secret keeps the stored value on save
	 * or test; "Remove" clears it. Changing a destination field (a shop domain)
	 * stops showing it as saved — the save drops it server-side.
	 *
	 * An OAuth platform shows "Connect" instead of a credentials save — the hook
	 * for platforms that connect by consent. None does in this build; the
	 * backend answers with a coded refusal the status line shows.
	 *
	 * Admin-only end to end: `readOnly` renders the notice, and every endpoint
	 * here is admin-gated server-side.
	 */
	import { tick } from 'svelte';
	import {
		deleteCommerceSkuMapping,
		disconnectCommerce,
		getCommerceCatalog,
		getCommerceLocations,
		getCommerceSettings,
		listCommerceSkuMappings,
		saveCommerceLocations,
		saveCommerceSettings,
		saveCommerceSkuMapping,
		startCommerceOAuth,
		testCommerceConnection
	} from '#lib/api/commerce.ts';
	import { listEntities } from '#lib/api/entities.ts';
	import { updateProviderCredentials } from '#lib/api/providerCredentials.ts';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import SecretField from '#lib/components/ui/SecretField.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { Entity } from '#lib/stores/entity.svelte.ts';
	import {
		buildCommercePayload,
		buildCommerceSecretUpdate,
		buildCommerceTestPayload,
		commerceOptionLabelKey,
		commerceSecretIsSaved,
		initialCommerceValues,
		missingCommerceFields,
		syncReasonKey,
		type CommerceCatalog,
		type CommerceLocationsView,
		type CommerceProvider,
		type CommerceProviderField,
		type CommerceSkuMapping,
		type StoredCommerceConfig
	} from '#lib/types/commerce.ts';
	import { formatList } from '#lib/utils/list.ts';

	let {
		readOnly,
		userLoaded
	}: {
		/** Signed-in user is not an admin: every endpoint here would 403. */
		readOnly: boolean;
		/** `/auth/me` has resolved. */
		userLoaded: boolean;
	} = $props();

	const uid = $props.id();

	let catalog = $state<CommerceCatalog | null>(null);
	let stored = $state<StoredCommerceConfig | null>(null);
	let loadFailed = $state(false);
	let selectedKey = $state('');
	let values = $state<Record<string, string>>({});
	/** Each secret field's remove toggle. Every key a `SecretField` binds is
	 *  present from the start: Svelte 5 refuses `bind:` of `undefined` to a
	 *  prop that declares a fallback. */
	let clear = $state<Record<string, boolean>>({});
	let invalid = $state<readonly string[]>([]);
	let saving = $state(false);
	let testing = $state(false);
	let connecting = $state(false);
	let disconnectArmed = $state(false);
	let statusMessage = $state('');
	let statusTone = $state<'success' | 'failure' | null>(null);

	let locations = $state<CommerceLocationsView | null>(null);
	let entities = $state<Entity[]>([]);
	let defaultLocation = $state('');
	let entityLocations = $state<Record<string, string>>({});
	let savingLocations = $state(false);

	let mappings = $state<CommerceSkuMapping[]>([]);
	let newSku = $state('');
	let newItemId = $state('');
	let savingMapping = $state(false);

	/** The sealed secret NAMES for the platform on file (`secrets_set`). */
	let storedNames = $state<string[]>([]);
	const provider = $derived<CommerceProvider | null>(
		catalog?.providers.find((p) => p.key === selectedKey) ?? null
	);
	const connected = $derived(!!stored?.provider);
	const connectedProvider = $derived(
		catalog?.providers.find((p) => p.key === stored?.provider) ?? null
	);

	function setStatus(text: string, tone: 'success' | 'failure' | null) {
		statusMessage = text;
		statusTone = tone;
	}

	function fieldLabel(f: CommerceProviderField): string {
		const label = m(f.label_key);
		return f.required ? label : m('org.erp.optionalLabel', { label });
	}

	function optionLabel(option: string): string {
		const key = commerceOptionLabelKey(option);
		return key ? m(key) : option;
	}

	function select(key: string) {
		selectedKey = key;
		const p = catalog?.providers.find((x) => x.key === key);
		values = p ? initialCommerceValues(p, stored) : {};
		clear = p
			? Object.fromEntries(p.fields.filter((f) => f.secret).map((f) => [f.name, false]))
			: {};
		invalid = [];
	}

	async function load() {
		loadFailed = false;
		try {
			const [cat, view] = await Promise.all([getCommerceCatalog(), getCommerceSettings()]);
			catalog = cat;
			stored = view.settings;
			storedNames = view.secrets_set;
			select(stored?.provider ?? '');
			if (stored?.provider) await loadConnected();
		} catch {
			loadFailed = true;
		}
	}

	/** The location picker and SKU overrides, for a connected platform. */
	async function loadConnected() {
		try {
			const [locs, ents, maps] = await Promise.all([
				getCommerceLocations(),
				listEntities(true),
				listCommerceSkuMappings()
			]);
			locations = locs;
			entities = ents;
			mappings = maps.items;
			defaultLocation = locs.location_id ?? '';
			entityLocations = { ...locs.entity_locations };
		} catch (err) {
			setStatus(err instanceof Error ? err.message : m('org.commerce.loadFailed'), 'failure');
		}
	}

	let requested = false;
	$effect(() => {
		if (requested || !userLoaded || readOnly) return;
		requested = true;
		void load();
	});

	function validate(p: CommerceProvider): boolean {
		const missing = missingCommerceFields(p, values, stored, storedNames, clear);
		invalid = missing.map((f) => f.name);
		if (!missing.length) return true;
		setStatus(
			m('org.erp.missingRequired', { fields: formatList(missing.map((f) => m(f.label_key))) }),
			'failure'
		);
		void tick().then(() => document.getElementById(`${uid}-${missing[0].name}`)?.focus());
		return false;
	}

	async function save() {
		if (!provider || !validate(provider)) return;
		const p = provider;
		saving = true;
		setStatus('', null);
		try {
			// Configuration first: a new platform or destination drops the old
			// secrets server-side, and the typed ones then belong to it.
			const view = await saveCommerceSettings(buildCommercePayload(p, values));
			stored = view.settings;
			storedNames = view.secrets_set;
			const update = buildCommerceSecretUpdate(p, values, clear);
			if (update) storedNames = (await updateProviderCredentials('commerce', update)).commerce;
			select(p.key);
			toast(m('org.commerce.saved'), 'success');
			await loadConnected();
		} catch (err) {
			setStatus(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'failure');
		} finally {
			saving = false;
		}
	}

	async function test() {
		if (!provider) return;
		testing = true;
		setStatus('', null);
		try {
			const result = await testCommerceConnection(buildCommerceTestPayload(provider, values));
			if (result.success) {
				setStatus(m('org.commerce.testOk', { count: result.locations.length }), 'success');
			} else {
				const key = syncReasonKey(result.code);
				const reason = key ? m(key) : m('org.toast.testFailed');
				setStatus(result.message ? `${reason} ${result.message}` : reason, 'failure');
			}
		} catch (err) {
			setStatus(err instanceof Error ? err.message : m('org.toast.testFailed'), 'failure');
		} finally {
			testing = false;
		}
	}

	async function connectOAuth() {
		if (!provider) return;
		connecting = true;
		setStatus('', null);
		try {
			const { authorize_url } = await startCommerceOAuth(provider.key);
			window.location.href = authorize_url;
		} catch (err) {
			connecting = false;
			setStatus(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'failure');
		}
	}

	async function onDisconnect(e: MouseEvent) {
		e.stopPropagation();
		if (!disconnectArmed) {
			disconnectArmed = true;
			return;
		}
		disconnectArmed = false;
		try {
			await disconnectCommerce();
			stored = null;
			storedNames = [];
			locations = null;
			mappings = [];
			select('');
			setStatus(m('org.commerce.disconnected'), 'success');
		} catch (err) {
			setStatus(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'failure');
		}
	}

	async function saveLocations() {
		savingLocations = true;
		try {
			const mapped = Object.fromEntries(
				Object.entries(entityLocations).filter(([, v]) => v && v.trim())
			);
			const saved = await saveCommerceLocations({
				location_id: defaultLocation || null,
				entity_locations: mapped
			});
			if (stored) {
				stored = {
					...stored,
					location_id: saved.location_id ?? undefined,
					entity_locations: saved.entity_locations
				};
			}
			toast(m('org.commerce.locationsSaved'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingLocations = false;
		}
	}

	async function addMapping(e: SubmitEvent) {
		e.preventDefault();
		if (!newSku.trim() || !newItemId.trim()) return;
		savingMapping = true;
		try {
			const saved = await saveCommerceSkuMapping({
				sku: newSku.trim(),
				provider_item_id: newItemId.trim()
			});
			mappings = [...mappings.filter((x) => x.id !== saved.id), saved].sort((a, b) =>
				a.sku.localeCompare(b.sku)
			);
			newSku = '';
			newItemId = '';
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingMapping = false;
		}
	}

	async function removeMapping(id: string) {
		try {
			await deleteCommerceSkuMapping(id);
			mappings = mappings.filter((x) => x.id !== id);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		}
	}

	function onWindowClick(e: MouseEvent) {
		if (disconnectArmed && !(e.target as HTMLElement).closest('.disconnect-action')) {
			disconnectArmed = false;
		}
	}
</script>

<svelte:window onclick={onWindowClick} />

<section class="card" data-testid="commerce-panel">
	<h2>{m('org.section.commerce')}</h2>
	<p class="card-hint">{m('org.commerce.hint')}</p>

	{#if readOnly}
		<p class="card-hint" data-testid="commerce-admin-only">{m('org.readOnly.sectionAdminOnly')}</p>
	{:else if loadFailed}
		<div class="load-failed" role="alert">
			<p class="card-hint failure-text">{m('org.commerce.loadFailed')}</p>
			<button type="button" class="btn-test" onclick={load}>{m('common.tryAgain')}</button>
		</div>
	{:else if !catalog}
		<p class="card-hint" aria-busy="true">{m('common.loading')}</p>
	{:else}
		<div class="form-grid">
			<label class="full-width">
				<span>{m('org.commerce.platform')}</span>
				<select
					data-testid="commerce-provider-select"
					value={selectedKey}
					onchange={(e) => {
						select(e.currentTarget.value);
						setStatus('', null);
					}}
				>
					<option value="" disabled>{m('org.commerce.choose')}</option>
					{#each catalog.providers as p (p.key)}
						<option value={p.key} disabled={!p.available}>{p.label}</option>
					{/each}
				</select>
			</label>
		</div>

		{#if provider}
			{#if provider.fields.length}
				<div class="form-grid">
					{#each provider.fields as f (f.name)}
						{@const inputId = `${uid}-${f.name}`}
						{@const helpId = f.help_key ? `${inputId}-help` : undefined}
						<div class="field">
							{#if f.secret}
								<SecretField
									id={inputId}
									label={fieldLabel(f)}
									bind:value={values[f.name]}
									bind:clear={clear[f.name]}
									configured={commerceSecretIsSaved(provider, f, stored, storedNames, values)}
									placeholder={f.placeholder ?? ''}
									required={f.required}
									invalid={invalid.includes(f.name)}
									describedBy={helpId}
									oninput={() => (invalid = invalid.filter((n) => n !== f.name))}
									testId="commerce-secret-{f.name}"
								/>
							{:else}
								<label>
									<span>{fieldLabel(f)}</span>
									{#if f.options}
										<select
											id={inputId}
											name={f.name}
											bind:value={values[f.name]}
											aria-describedby={helpId}
										>
											{#each f.options as option (option)}
												<option value={option}>{optionLabel(option)}</option>
											{/each}
										</select>
									{:else}
										<input
											id={inputId}
											name={f.name}
											type="text"
											autocomplete="off"
											bind:value={values[f.name]}
											oninput={() => (invalid = invalid.filter((n) => n !== f.name))}
											aria-invalid={invalid.includes(f.name) ? 'true' : undefined}
											aria-describedby={helpId}
											placeholder={f.placeholder ?? ''}
										/>
									{/if}
								</label>
							{/if}
							{#if helpId}
								<p class="field-hint" id={helpId}>{m(f.help_key!)}</p>
							{/if}
						</div>
					{/each}
				</div>
			{:else}
				<p class="card-hint field-gap">{m('org.commerce.noFields')}</p>
			{/if}

			<p class="card-hint field-gap">
				<a href={provider.docs_url} target="_blank" rel="noopener noreferrer">
					{m('org.commerce.docsLink', { platform: provider.label })}
				</a>
			</p>

			<div class="action-row">
				<div class="action-buttons">
					{#if provider.auth === 'oauth' && stored?.provider !== provider.key}
						<button
							class="btn-save-section"
							disabled={connecting}
							onclick={connectOAuth}
							data-testid="commerce-connect"
						>
							{m('org.commerce.connect', { platform: provider.label })}
						</button>
					{:else}
						<button
							class="btn-save-section"
							disabled={saving}
							onclick={save}
							data-testid="commerce-save"
						>
							{saving ? m('org.common.saving') : m('org.commerce.save')}
						</button>
						<button class="btn-test" disabled={testing} onclick={test} data-testid="commerce-test">
							{testing ? m('org.common.testing') : m('org.common.testConnection')}
						</button>
					{/if}
					{#if connected}
						<span class="disconnect-action">
							<RowAction variant="danger" armed={disconnectArmed} onclick={onDisconnect}>
								{disconnectArmed ? m('org.commerce.disconnectConfirm') : m('org.commerce.disconnect')}
							</RowAction>
						</span>
					{/if}
				</div>
			</div>
		{/if}

		{#if connected && connectedProvider && locations}
			<div class="subsection" data-testid="commerce-locations">
				<h3>{m('org.commerce.locationsTitle')}</h3>
				<p class="card-hint">{m('org.commerce.locationsHint')}</p>
				{#if !locations.available}
					<p class="card-hint failure-text">
						{m('org.commerce.locationsUnavailable', {
							reason: m(syncReasonKey(locations.code) ?? 'commerceSync.reason.other')
						})}
					</p>
				{/if}
				<div class="form-grid">
					<label class="full-width">
						<span>{m('org.commerce.defaultLocation')}</span>
						<select bind:value={defaultLocation} data-testid="commerce-default-location">
							<option value="">{m('org.commerce.noLocation')}</option>
							{#each locations.locations as loc (loc.id)}
								<option value={loc.id}>{loc.name}</option>
							{/each}
							{#if defaultLocation && !locations.locations.some((l) => l.id === defaultLocation)}
								<option value={defaultLocation}>{defaultLocation}</option>
							{/if}
						</select>
					</label>
					{#if entities.length > 1}
						{#each entities as entity (entity.id)}
							<label>
								<span>{m('org.commerce.entityLocation', { entity: entity.name })}</span>
								<select bind:value={entityLocations[entity.id]}>
									<option value="">{m('org.commerce.useDefaultLocation')}</option>
									{#each locations.locations as loc (loc.id)}
										<option value={loc.id}>{loc.name}</option>
									{/each}
								</select>
							</label>
						{/each}
					{/if}
				</div>
				<div class="action-row">
					<button
						class="btn-save-section"
						disabled={savingLocations}
						onclick={saveLocations}
						data-testid="commerce-save-locations"
					>
						{savingLocations ? m('org.common.saving') : m('org.commerce.saveLocations')}
					</button>
				</div>
			</div>

			<div class="subsection" data-testid="commerce-sku-mappings">
				<h3>{m('org.commerce.skuTitle')}</h3>
				<p class="card-hint">{m('org.commerce.skuHint', { platform: connectedProvider.label })}</p>
				{#if mappings.length}
					<table class="mapping-table">
						<thead>
							<tr>
								<th scope="col">{m('org.commerce.skuColumn')}</th>
								<th scope="col">{m('org.commerce.itemIdColumn')}</th>
								<th scope="col"><span class="visually-hidden">{m('org.commerce.actionsColumn')}</span></th>
							</tr>
						</thead>
						<tbody>
							{#each mappings as mapping (mapping.id)}
								<tr>
									<td class="mono">{mapping.sku}</td>
									<td class="mono">{mapping.provider_item_id}</td>
									<td class="right">
										<button
											type="button"
											class="link-btn"
											aria-label={m('org.commerce.removeMappingAria', { sku: mapping.sku })}
											onclick={() => removeMapping(mapping.id)}
										>
											{m('org.commerce.removeMapping')}
										</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				{/if}
				<form class="mapping-form" onsubmit={addMapping}>
					<label>
						<span>{m('org.commerce.skuColumn')}</span>
						<input bind:value={newSku} maxlength="100" data-testid="commerce-mapping-sku" />
					</label>
					<label>
						<span>{m('org.commerce.itemIdColumn')}</span>
						<input bind:value={newItemId} maxlength="255" data-testid="commerce-mapping-item" />
					</label>
					<button
						type="submit"
						class="btn-test"
						disabled={savingMapping || !newSku.trim() || !newItemId.trim()}
					>
						{m('org.commerce.addMapping')}
					</button>
				</form>
			</div>
		{/if}
	{/if}

	<!-- Always mounted so a result is announced when it arrives (WCAG 4.1.3). -->
	<p
		class="test-result"
		role="status"
		data-testid="commerce-status"
		class:success={statusTone === 'success'}
		class:failure={statusTone === 'failure' && !!statusMessage}
	>
		{statusMessage}
	</p>
</section>

<style>
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 20px 24px;
	}

	.card h2 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0 0 4px;
	}

	h3 {
		font-size: 0.9rem;
		font-weight: 600;
		margin: 0 0 4px;
	}

	.card-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	.field-gap {
		margin-top: 12px;
	}

	.failure-text {
		color: var(--danger);
	}

	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
		margin-top: 14px;
	}

	.full-width {
		grid-column: 1 / -1;
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	label > span:first-child {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	input,
	select {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		font-family: inherit;
		width: 100%;
		box-sizing: border-box;
	}

	.field-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 6px 0 0;
	}

	.link-btn {
		background: none;
		border: none;
		padding: 0;
		margin-top: 6px;
		font: inherit;
		font-size: 0.78rem;
		color: var(--accent);
		text-decoration: underline;
		cursor: pointer;
		min-height: 24px;
	}

	a {
		color: var(--accent);
	}

	.subsection {
		margin-top: 20px;
		padding-top: 16px;
		border-top: 1px solid var(--border);
	}

	.action-row {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 12px;
		margin-top: 16px;
	}

	.action-buttons {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 12px;
	}

	.load-failed {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 12px;
	}

	.mapping-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
		margin-bottom: 12px;
	}

	.mapping-table th,
	.mapping-table td {
		text-align: left;
		padding: 6px 8px;
		border-bottom: 1px solid var(--border);
		overflow-wrap: anywhere;
	}

	.mapping-table .right {
		text-align: right;
	}

	.mapping-form {
		display: flex;
		flex-wrap: wrap;
		align-items: flex-end;
		gap: 12px;
	}

	.mapping-form label {
		flex: 1 1 160px;
	}

	.btn-save-section,
	.btn-test {
		padding: 8px 18px;
		border-radius: 6px;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-save-section {
		border: none;
		background: var(--accent-strong);
		color: #fff;
	}

	.btn-test {
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
	}

	.btn-save-section:disabled,
	.btn-test:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.test-result {
		font-size: 0.85rem;
		font-weight: 500;
		margin: 12px 0 0;
	}

	.test-result:empty {
		margin: 0;
	}

	.test-result.success {
		color: var(--success);
	}

	.test-result.failure {
		color: var(--danger);
	}

	@media (max-width: 600px) {
		.form-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
