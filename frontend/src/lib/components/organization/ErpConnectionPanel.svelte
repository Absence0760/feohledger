<script lang="ts">
	/**
	 * Organization → ERP. One dropdown of ERPs, then the fields that ERP needs.
	 *
	 * Everything the form offers comes from the backend catalogue
	 * (`GET /api/organization/erp/providers`, `erp_adapters/catalog.py`); this
	 * component hardcodes no provider. Before it, the page carried its own
	 * `ERP_TYPES` list of mostly enterprise ERPs, most with no direct adapter,
	 * behind a separate "integration method" select.
	 *
	 * Secrets are write-only. A saved secret arrives masked, its input starts
	 * blank with a visible "saved" hint, and a blank secret on save or test
	 * keeps the stored value server-side. "Remove" sends it as `null`, which
	 * clears it. The OAuth token block is never sent from here: the provider's
	 * callback is its only writer.
	 *
	 * An OAuth ERP (QuickBooks Online, Xero, Sage, Blackbaud) has one primary
	 * action, "Connect to X", which saves the form and then opens the ERP's
	 * consent page. Its own-app client id and secret, and the redirect URI such
	 * an app must register, sit in a collapsed "Use your own app" section that
	 * opens by itself when the platform has no app for that ERP
	 * (`GET /api/organization/erp/oauth/status` → `providers[].available`).
	 *
	 * The plan gate and the admin-only read-only state are decided by the
	 * page (the same props every panel there uses) and rendered here.
	 */
	import { onMount, tick, untrack } from 'svelte';
	import { replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import { api } from '#lib/api.ts';
	import {
		disconnectErpOAuth,
		getErpCatalog,
		getErpOAuthStatus,
		startErpOAuth,
		testErpConnection
	} from '#lib/api/erpConnection.ts';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import PlanUpgradeNotice from '#lib/components/ui/PlanUpgradeNotice.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { FEATURE_ERP_INTEGRATIONS } from '#lib/types/planFeatures.ts';
	import {
		BYO_APP_FIELDS,
		MERGE_DEV_PROVIDER,
		buildErpPayload,
		byoAppRequired,
		groupProviders,
		initialValues,
		missingRequired,
		OAUTH_ERROR_KEYS,
		oauthConnectionState,
		optionLabelKey,
		readOAuthReturn,
		secretIsSaved,
		selectedProviderKey,
		withoutOAuthReturn,
		type ErpCatalog,
		type ErpOAuthStatus,
		type ErpProvider,
		type ErpProviderField,
		type OAuthReturn,
		type StoredErpConfig
	} from '#lib/types/erpConnection.ts';
	import { formatList } from '#lib/utils/list.ts';

	let {
		stored,
		readOnly,
		userLoaded,
		entitled,
		onsaved
	}: {
		/** `settings.erp` from `GET /api/organization` (secrets masked). */
		stored: StoredErpConfig | undefined;
		/** Signed-in user is not an admin: every ERP endpoint would 403. */
		readOnly: boolean;
		/** `/auth/me` has resolved (gates below are meaningless before it). */
		userLoaded: boolean;
		/** The plan includes live ERP integrations (decisions §258). */
		entitled: boolean;
		/** The PATCH response, so the page keeps its `org` in step. */
		onsaved?: (org: { settings: Record<string, unknown> }) => void;
	} = $props();

	const uid = $props.id();

	let catalog = $state<ErpCatalog | null>(null);
	let catalogError = $state(false);
	let selectedKey = $state('');
	let mergeErpType = $state('');
	let values = $state<Record<string, string>>({});
	/** Saved secrets the admin chose to remove: sent as `null` on save. */
	let cleared = $state<ReadonlySet<string>>(new Set());
	/** Required fields a save or connect found empty (`aria-invalid`). */
	let invalid = $state<readonly string[]>([]);
	let saving = $state(false);
	let testing = $state(false);
	let connecting = $state(false);
	let disconnecting = $state(false);
	let confirmDisconnect = $state(false);
	let oauthStatus = $state<ErpOAuthStatus | null>(null);
	let oauthStatusFailed = $state(false);
	/** "Use your own app (advanced)" is expanded. */
	let byoOpen = $state(false);
	/** The one live region's text: test results, validation, connect errors. */
	let statusMessage = $state('');
	let statusTone = $state<'success' | 'failure' | null>(null);
	/** The OAuth callback's `?erp_connected=` / `?erp_error=`, read ONCE on
	 *  mount and shown until the admin does something else. */
	let oauthReturn = $state<OAuthReturn>(null);

	const mask = $derived(catalog?.secret_mask ?? '');
	const groups = $derived(catalog ? groupProviders(catalog.providers) : []);
	const provider = $derived<ErpProvider | null>(
		catalog?.providers.find((p) => p.key === selectedKey) ?? null
	);
	const isOAuth = $derived(provider?.auth === 'oauth');
	/** The ERP's own fields, and (OAuth only) the bring-your-own-app pair. */
	const mainFields = $derived(
		provider ? provider.fields.filter((f) => !isOAuth || !BYO_APP_FIELDS.includes(f.name)) : []
	);
	const byoFields = $derived(
		provider && isOAuth ? provider.fields.filter((f) => BYO_APP_FIELDS.includes(f.name)) : []
	);
	const byoRequired = $derived(!!provider && byoAppRequired(provider, oauthStatus));
	const connection = $derived(
		provider ? oauthConnectionState(provider.key, oauthStatus) : 'not_connected'
	);

	function setStatus(text: string, tone: 'success' | 'failure' | null) {
		statusMessage = text;
		statusTone = tone;
		// Anything the admin does next replaces the OAuth return message.
		oauthReturn = null;
	}

	function providerLabel(p: ErpProvider): string {
		return p.key === MERGE_DEV_PROVIDER ? m('org.erp.provider.mergeDev') : p.label;
	}

	function isRequired(f: ErpProviderField): boolean {
		return f.required || (byoRequired && BYO_APP_FIELDS.includes(f.name));
	}

	function fieldLabel(f: ErpProviderField): string {
		const label = m(f.label_key);
		return isRequired(f) ? label : m('org.erp.optionalLabel', { label });
	}

	/** A choice value (the ERP's own code) → its label, or itself. */
	function optionLabel(option: string, fallback = option): string {
		const key = optionLabelKey(option);
		return key ? m(key) : fallback;
	}

	/** A secret is on file and the admin has not chosen to remove it. */
	function secretKept(p: ErpProvider, f: ErpProviderField): boolean {
		return secretIsSaved(p, f, stored, mask) && !cleared.has(f.name);
	}

	/** Show `key`'s values on file (blank for a new choice). */
	function select(key: string) {
		selectedKey = key;
		const p = catalog?.providers.find((x) => x.key === key);
		values = p ? initialValues(p, stored, mask) : {};
		cleared = new Set();
		invalid = [];
		confirmDisconnect = false;
		if (key === MERGE_DEV_PROVIDER) {
			const onFile = storedKey() === MERGE_DEV_PROVIDER ? (stored?.type ?? '') : '';
			const known = catalog?.merge_dev_long_tail.some((e) => e.value === onFile);
			mergeErpType = known ? onFile : (catalog?.merge_dev_long_tail[0]?.value ?? '');
		}
		syncByoOpen();
	}

	/** The admin picked an ERP from the dropdown. */
	function choose(key: string) {
		select(key);
		setStatus('', null);
	}

	function storedKey(): string | null {
		return selectedProviderKey(stored);
	}

	/** Expand the own-app section when it must be filled, or already is. */
	function syncByoOpen() {
		byoOpen = byoRequired || !!values.client_id;
	}

	async function loadCatalog() {
		catalogError = false;
		try {
			catalog = await getErpCatalog();
			const onFile = storedKey();
			select(onFile && catalog.providers.some((p) => p.key === onFile) ? onFile : '');
			if (catalog.providers.some((p) => p.auth === 'oauth')) await loadOAuthStatus();
		} catch {
			catalogError = true;
		}
	}

	async function loadOAuthStatus() {
		try {
			oauthStatus = await getErpOAuthStatus();
			oauthStatusFailed = false;
		} catch {
			oauthStatusFailed = true;
		}
		syncByoOpen();
	}

	// The catalogue is admin-only and plan-gated in use, so it is fetched only
	// once the panel will actually render the form.
	let requested = false;
	$effect(() => {
		if (requested || !userLoaded || readOnly || !entitled) return;
		requested = true;
		void loadCatalog();
	});

	/**
	 * Drop the return parameters from the address bar, so a reload or a copied
	 * link does not repeat the message. SvelteKit's `replaceState` throws until
	 * its router has started, which on a first load is after the components
	 * mount; a task queued now runs after that start (it awaits only a
	 * microtask), so the one retry lands.
	 */
	function stripOAuthReturn(next: string, retry = true) {
		try {
			replaceState(next, untrack(() => page.state));
		} catch {
			if (retry) setTimeout(() => stripOAuthReturn(next, false), 0);
		}
	}

	// The OAuth callback lands back here with `?erp_connected=` / `?erp_error=`.
	// Read once, untracked: a tracked read re-ran on every dropdown change and
	// put the message back after the admin had moved on.
	onMount(() => {
		const url = untrack(() => page.url);
		oauthReturn = readOAuthReturn(url.searchParams);
		const next = withoutOAuthReturn(url);
		if (next) stripOAuthReturn(next);
	});

	const returnMessage = $derived.by(() => {
		const ret = oauthReturn;
		if (!ret) return '';
		if (ret.kind === 'connected') {
			const p = catalog?.providers.find((x) => x.key === ret.provider);
			const name =
				(p && providerLabel(p)) ??
				oauthStatus?.providers.find((x) => x.key === ret.provider)?.display_name ??
				ret.provider;
			return m('org.erp.oauth.returnConnected', { erp: name });
		}
		const key = OAUTH_ERROR_KEYS[ret.code as keyof typeof OAUTH_ERROR_KEYS];
		const erp = provider ? providerLabel(provider) : m('org.erp.oauth.yourErp');
		return key ? m(key, { erp }) : m('org.erp.oauth.returnError', { code: ret.code });
	});
	const shownMessage = $derived(statusMessage || returnMessage);
	const shownTone = $derived(
		statusMessage ? statusTone : oauthReturn?.kind === 'connected' ? 'success' : 'failure'
	);

	function clearInvalid(name: string) {
		if (invalid.includes(name)) invalid = invalid.filter((n) => n !== name);
	}

	function removeSecret(name: string) {
		cleared = new Set([...cleared, name]);
		values[name] = '';
	}

	function keepSecret(name: string) {
		cleared = new Set([...cleared].filter((n) => n !== name));
	}

	/** Required fields all filled? If not, mark them, say which, and move
	 *  focus to the first (WCAG 3.3.1). */
	function validate(p: ErpProvider): boolean {
		const missing = missingRequired(p, values, stored, mask, {
			cleared,
			requireByoApp: byoRequired
		});
		invalid = missing.map((f) => f.name);
		if (!missing.length) return true;
		setStatus(
			m('org.erp.missingRequired', { fields: formatList(missing.map((f) => m(f.label_key))) }),
			'failure'
		);
		if (p.auth === 'oauth' && missing.some((f) => BYO_APP_FIELDS.includes(f.name))) byoOpen = true;
		void tick().then(() => document.getElementById(`${uid}-${missing[0].name}`)?.focus());
		return false;
	}

	/** PATCH the form. The typed secrets are stored, so the form drops them
	 *  and shows the saved hint, the same as after a reload. */
	async function persist(p: ErpProvider) {
		const data = await api.patch<{ settings: Record<string, unknown> }>('/api/organization', {
			settings: { erp: buildErpPayload(p, values, mergeErpType, cleared, stored) }
		});
		onsaved?.(data);
		const next = (data.settings.erp ?? undefined) as StoredErpConfig | undefined;
		values = initialValues(p, next, mask);
		cleared = new Set();
		invalid = [];
	}

	async function save() {
		if (!provider || !validate(provider)) return;
		const p = provider;
		saving = true;
		setStatus('', null);
		try {
			await persist(p);
			toast(m('org.toast.sectionSaved', { section: m('org.section.erpSaved') }), 'success');
			// Saving or removing an own app changes which app a connect uses.
			if (p.auth === 'oauth') await loadOAuthStatus();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	async function test() {
		if (!provider) return;
		testing = true;
		setStatus('', null);
		try {
			const result = await testErpConnection(
				buildErpPayload(provider, values, mergeErpType, cleared, stored)
			);
			setStatus(result.message, result.success ? 'success' : 'failure');
		} catch (err) {
			setStatus(err instanceof Error ? err.message : m('org.toast.testFailed'), 'failure');
		} finally {
			testing = false;
		}
	}

	/** Save the choice (and any own-app credentials), then open the ERP's
	 *  consent page. The authorize call reads the app from what is saved. */
	async function connect() {
		if (!provider || !validate(provider)) return;
		const p = provider;
		connecting = true;
		setStatus(m('org.erp.oauth.redirecting', { erp: providerLabel(p) }), null);
		try {
			await persist(p);
			const { authorize_url } = await startErpOAuth(p.key);
			window.location.href = authorize_url;
		} catch (err) {
			connecting = false;
			setStatus(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'failure');
		}
	}

	/** Two clicks: the first arms, the second disconnects. */
	async function onDisconnectClick(e: MouseEvent) {
		e.stopPropagation();
		if (!provider) return;
		if (!confirmDisconnect) {
			confirmDisconnect = true;
			return;
		}
		const p = provider;
		confirmDisconnect = false;
		disconnecting = true;
		try {
			await disconnectErpOAuth();
			await loadOAuthStatus();
			setStatus(m('org.erp.oauth.disconnected', { erp: providerLabel(p) }), 'success');
		} catch (err) {
			setStatus(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'failure');
		} finally {
			disconnecting = false;
		}
	}

	function onWindowClick(e: MouseEvent) {
		if (confirmDisconnect && !(e.target as HTMLElement).closest('.disconnect-action')) {
			confirmDisconnect = false;
		}
	}

	async function copyRedirectUri() {
		if (!oauthStatus) return;
		try {
			await navigator.clipboard.writeText(oauthStatus.redirect_uri);
			toast(m('org.erp.oauth.redirectUriCopied'), 'success');
		} catch {
			// Denied permission or an insecure context: the URI stays
			// selectable on the page, so say so rather than fail silently.
			toast(m('org.erp.oauth.redirectUriCopyFailed'), 'error');
		}
	}
</script>

<svelte:window onclick={onWindowClick} />

{#snippet field(p: ErpProvider, f: ErpProviderField)}
	{@const kept = secretKept(p, f)}
	{@const isCleared = f.secret && cleared.has(f.name)}
	{@const inputId = `${uid}-${f.name}`}
	{@const helpId = f.help_key ? `${inputId}-help` : undefined}
	{@const secretId = kept || isCleared ? `${inputId}-saved` : undefined}
	{@const describedBy = [secretId, helpId].filter(Boolean).join(' ') || undefined}
	<div class="field">
		<label>
			<span>{fieldLabel(f)}</span>
			{#if f.options}
				<select
					id={inputId}
					bind:value={values[f.name]}
					name={f.name}
					aria-describedby={describedBy}
				>
					{#each f.options as option (option)}
						<option value={option}>{optionLabel(option)}</option>
					{/each}
				</select>
			{:else}
				<input
					id={inputId}
					type={f.secret ? 'password' : 'text'}
					name={f.name}
					autocomplete={f.secret ? 'new-password' : 'off'}
					bind:value={values[f.name]}
					oninput={() => clearInvalid(f.name)}
					required={isRequired(f) && !kept}
					aria-invalid={invalid.includes(f.name) ? 'true' : undefined}
					placeholder={kept ? m('org.erp.secretSaved') : (f.placeholder ?? '')}
					aria-describedby={describedBy}
					data-secret-saved={f.secret ? String(kept) : undefined}
				/>
			{/if}
		</label>
		{#if secretId}
			<div class="secret-row">
				<p class="field-hint" id={secretId}>
					{isCleared ? m('org.erp.secretCleared') : m('org.erp.secretSavedHint')}
				</p>
				{#if isCleared}
					<button type="button" class="link-btn" onclick={() => keepSecret(f.name)}>
						{m('org.erp.secretKeep')}
					</button>
				{:else}
					<button
						type="button"
						class="link-btn"
						aria-label={m('org.erp.secretRemoveAria', { field: m(f.label_key) })}
						onclick={() => removeSecret(f.name)}
					>
						{m('org.erp.secretRemove')}
					</button>
				{/if}
			</div>
		{/if}
		{#if helpId}
			<p class="field-hint" id={helpId}>{m(f.help_key!)}</p>
		{/if}
	</div>
{/snippet}

<section class="card" data-testid="erp-panel">
	<div class="help-row">
		<h2>{m('org.section.erp')}</h2>
		<HelpTip term="erp-sync" />
	</div>
	<p class="card-hint">{m('org.erp.hint')}</p>

	{#if readOnly}
		<p class="card-hint" data-testid="erp-admin-only">{m('org.readOnly.sectionAdminOnly')}</p>
	{:else if userLoaded && !entitled}
		<!-- Every catalogue entry is a live ERP; the local-first `mock` ERP
		     needs no configuration. -->
		<PlanUpgradeNotice feature={FEATURE_ERP_INTEGRATIONS} testId="erp-plan-upgrade" />
	{:else if catalogError}
		<div class="load-failed" role="alert" data-testid="erp-load-failed">
			<p class="card-hint failure-text">{m('org.erp.loadFailed')}</p>
			<button type="button" class="btn-test" onclick={loadCatalog}>{m('common.tryAgain')}</button>
		</div>
	{:else if !catalog}
		<p class="card-hint" aria-busy="true">{m('org.erp.loading')}</p>
	{:else}
		<div class="form-grid">
			<label class="full-width">
				<span>{m('org.erp.system')}</span>
				<select
					data-testid="erp-provider-select"
					value={selectedKey}
					onchange={(e) => choose(e.currentTarget.value)}
				>
					<option value="" disabled>{m('org.erp.choose')}</option>
					{#each groups as group (group.labelKey)}
						<optgroup label={m(group.labelKey)}>
							{#each group.providers as p (p.key)}
								<option value={p.key}>
									{p.available ? providerLabel(p) : m('org.erp.comingSoon', { erp: providerLabel(p) })}
								</option>
							{/each}
						</optgroup>
					{/each}
				</select>
			</label>
		</div>

		{#if provider}
			{#if !provider.available}
				<p class="card-hint notice" data-testid="erp-coming-soon">
					{m('org.erp.comingSoonHint', { erp: providerLabel(provider) })}
				</p>
			{/if}

			{#if provider.key === MERGE_DEV_PROVIDER}
				<p class="card-hint notice">{m('org.erp.mergePlan')}</p>
				<div class="form-grid">
					<label class="full-width">
						<span>{m('org.erp.mergeErp')}</span>
						<select bind:value={mergeErpType} data-testid="erp-merge-type">
							{#each catalog.merge_dev_long_tail as erp (erp.value)}
								<option value={erp.value}>{optionLabel(erp.value, erp.label)}</option>
							{/each}
						</select>
					</label>
				</div>
				<p class="card-hint field-gap">{m('org.erp.mergeHint')}</p>
			{/if}

			{#if mainFields.length}
				<div class="form-grid">
					{#each mainFields as f (f.name)}
						{@render field(provider, f)}
					{/each}
				</div>
			{/if}

			{#if byoFields.length}
				<details class="byo" bind:open={byoOpen} data-testid="erp-byo-app">
					<summary>{m('org.erp.oauth.byoSummary')}</summary>
					<p class="card-hint field-gap" class:notice={byoRequired} data-testid="erp-byo-hint">
						{byoRequired
							? m('org.erp.oauth.byoRequiredHint', { erp: providerLabel(provider) })
							: m('org.erp.oauth.byoHint', { erp: providerLabel(provider) })}
					</p>
					<div class="form-grid">
						{#each byoFields as f (f.name)}
							{@render field(provider, f)}
						{/each}
					</div>
					{#if oauthStatus}
						<div class="redirect-uri" data-testid="erp-redirect-uri">
							<span class="redirect-label" id="{uid}-redirect-label">
								{m('org.erp.oauth.redirectUri')}
							</span>
							<div class="redirect-value">
								<code id="{uid}-redirect-uri">{oauthStatus.redirect_uri}</code>
								<button
									type="button"
									class="btn-test"
									aria-describedby="{uid}-redirect-label"
									onclick={copyRedirectUri}
								>
									{m('org.erp.oauth.copy')}
								</button>
							</div>
							<p class="field-hint">
								{m('org.erp.oauth.redirectUriHint', { erp: providerLabel(provider) })}
							</p>
						</div>
					{/if}
				</details>
			{/if}

			<p class="card-hint field-gap">
				<a href={provider.docs_url} target="_blank" rel="noopener noreferrer">
					{m('org.erp.docsLink', { erp: providerLabel(provider) })}
				</a>
			</p>

			{#if isOAuth}
				<div class="action-row" data-testid="erp-oauth">
					<p
						class="oauth-state"
						class:connected={connection === 'connected'}
						class:failure-text={connection === 'needs_reconnect'}
						id="{uid}-oauth-state"
						data-testid="erp-oauth-state"
					>
						{#if connection === 'connected'}
							{oauthStatus?.external_tenant_id
								? m('org.erp.oauth.connectedCompany', {
										erp: providerLabel(provider),
										id: oauthStatus.external_tenant_id
									})
								: m('org.erp.oauth.connected', { erp: providerLabel(provider) })}
						{:else if connection === 'needs_reconnect'}
							{m('org.erp.oauth.needsReconnect', { erp: providerLabel(provider) })}
						{:else}
							{m('org.erp.oauth.notConnected', { erp: providerLabel(provider) })}
						{/if}
					</p>
					{#if oauthStatusFailed}
						<p class="oauth-state failure-text">{m('org.erp.oauth.statusFailed')}</p>
					{/if}
					<div class="action-buttons">
						{#if connection === 'connected'}
							<button
								class="btn-save-section"
								disabled={saving || !provider.available}
								onclick={save}
							>
								{saving ? m('org.common.saving') : m('org.erp.save')}
							</button>
						{:else}
							<button
								class="btn-save-section"
								disabled={connecting || !provider.available}
								aria-describedby="{uid}-oauth-state"
								onclick={connect}
							>
								{connection === 'needs_reconnect'
									? m('org.erp.oauth.reconnect', { erp: providerLabel(provider) })
									: m('org.erp.oauth.connect', { erp: providerLabel(provider) })}
							</button>
						{/if}
						{#if connection !== 'not_connected'}
							<span class="disconnect-action">
								<RowAction
									variant="danger"
									armed={confirmDisconnect}
									disabled={disconnecting}
									onclick={onDisconnectClick}
								>
									{disconnecting
										? m('org.erp.oauth.disconnecting')
										: confirmDisconnect
											? m('org.erp.oauth.disconnectConfirm')
											: m('org.erp.oauth.disconnect')}
								</RowAction>
							</span>
						{/if}
					</div>
				</div>
			{:else}
				<div class="action-row">
					<div class="action-buttons">
						<button class="btn-save-section" disabled={saving || !provider.available} onclick={save}>
							{saving ? m('org.common.saving') : m('org.erp.save')}
						</button>
						<button class="btn-test" disabled={testing || !provider.available} onclick={test}>
							{testing ? m('org.common.testing') : m('org.common.testConnection')}
						</button>
					</div>
				</div>
			{/if}
		{/if}
	{/if}

	<!-- Always mounted, so a result is announced when it arrives (WCAG 4.1.3):
	     a live region inserted together with its text is not read by every
	     screen reader. -->
	<p
		class="test-result"
		role="status"
		data-testid="erp-status"
		class:success={shownTone === 'success'}
		class:failure={shownTone === 'failure' && !!shownMessage}
	>
		{shownMessage}
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

	.card-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	.field-gap {
		margin-top: 12px;
	}

	.notice {
		margin-top: 12px;
		color: var(--text);
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

	.field-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 6px 0 0;
		line-height: 1.5;
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

	select {
		padding-right: 30px;
	}

	a {
		color: var(--accent);
	}

	.action-row {
		display: flex;
		align-items: center;
		/* Wraps so a long ERP name or result never forces a horizontal scroll
		   at 320px (WCAG 1.4.10). */
		flex-wrap: wrap;
		gap: 12px;
		margin-top: 16px;
		padding-top: 14px;
		border-top: 1px solid var(--border);
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

	.load-failed .card-hint {
		margin: 0;
	}

	.secret-row {
		display: flex;
		align-items: baseline;
		flex-wrap: wrap;
		gap: 4px 10px;
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
	}

	.byo {
		margin-top: 16px;
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 10px 14px;
	}

	.byo summary {
		cursor: pointer;
		font-size: 0.85rem;
		font-weight: 500;
	}

	.byo[open] summary {
		margin-bottom: 4px;
	}

	.redirect-uri {
		margin-top: 14px;
	}

	.redirect-label {
		display: block;
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		margin-bottom: 4px;
	}

	.redirect-value {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 8px;
	}

	.redirect-value code {
		/* A long URI breaks rather than scroll the page at 320px. */
		overflow-wrap: anywhere;
		font-size: 0.82rem;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 4px 8px;
		user-select: all;
	}

	.oauth-state {
		margin: 0;
		font-size: 0.85rem;
		color: var(--text-muted);
	}

	.oauth-state.connected {
		color: var(--text);
		font-weight: 500;
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

	.btn-save-section:hover:not(:disabled) {
		opacity: 0.85;
	}

	.btn-test:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
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
