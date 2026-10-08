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
	 * blank with a "saved, leave blank to keep" placeholder, and a blank secret
	 * on save or test keeps the stored value server-side. The OAuth token block
	 * is never sent from here: the provider's callback is its only writer.
	 *
	 * The plan gate and the admin-only read-only state are decided by the
	 * page (the same props every panel there uses) and rendered here.
	 */
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
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { FEATURE_ERP_INTEGRATIONS } from '#lib/types/planFeatures.ts';
	import {
		MERGE_DEV_PROVIDER,
		buildErpPayload,
		groupProviders,
		initialValues,
		missingRequired,
		readOAuthReturn,
		secretIsSaved,
		selectedProviderKey,
		type ErpCatalog,
		type ErpOAuthStatus,
		type ErpProvider,
		type ErpProviderField,
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
	let saving = $state(false);
	let testing = $state(false);
	let connecting = $state(false);
	let disconnecting = $state(false);
	let oauthStatus = $state<ErpOAuthStatus | null>(null);
	/** The one live region's text: test results, OAuth returns, validation. */
	let statusMessage = $state('');
	let statusTone = $state<'success' | 'failure' | null>(null);

	const mask = $derived(catalog?.secret_mask ?? '');
	const groups = $derived(catalog ? groupProviders(catalog.providers) : []);
	const provider = $derived<ErpProvider | null>(
		catalog?.providers.find((p) => p.key === selectedKey) ?? null
	);
	/** The selection already on file, so OAuth can connect against it. */
	const storedKey = $derived(selectedProviderKey(stored));
	const isStoredSelection = $derived(
		!!provider &&
			storedKey === provider.key &&
			(provider.key !== MERGE_DEV_PROVIDER || stored?.type === mergeErpType)
	);

	function setStatus(text: string, tone: 'success' | 'failure' | null) {
		statusMessage = text;
		statusTone = tone;
	}

	function providerLabel(p: ErpProvider): string {
		return p.key === MERGE_DEV_PROVIDER ? m('org.erp.provider.mergeDev') : p.label;
	}

	function fieldLabel(f: ErpProviderField): string {
		const label = m(f.label_key);
		return f.required ? label : m('org.erp.optionalLabel', { label });
	}

	/** A choice field's option value (the ERP's own code) → its label. An
	 *  unknown value renders as itself, which is what the ERP calls it. */
	const OPTION_LABEL_KEYS: Record<string, MessageKey> = {
		production: 'org.erp.env.production',
		sandbox: 'org.erp.env.sandbox',
		AUTHORISED: 'org.erp.option.AUTHORISED',
		DRAFT: 'org.erp.option.DRAFT'
	};

	function optionLabel(option: string): string {
		const key = OPTION_LABEL_KEYS[option];
		return key ? m(key) : option;
	}

	/** Reset the form to `key`'s values on file (blank for a new choice). */
	function choose(key: string) {
		selectedKey = key;
		const p = catalog?.providers.find((x) => x.key === key);
		values = p ? initialValues(p, stored, mask) : {};
		if (key === MERGE_DEV_PROVIDER) {
			const onFile = storedKey === MERGE_DEV_PROVIDER ? (stored?.type ?? '') : '';
			const known = catalog?.merge_dev_long_tail.some((e) => e.value === onFile);
			mergeErpType = known ? onFile : (catalog?.merge_dev_long_tail[0]?.value ?? '');
		}
		setStatus('', null);
		void loadOAuthStatus();
	}

	async function loadCatalog() {
		try {
			catalog = await getErpCatalog();
			catalogError = false;
			choose(storedKey && catalog.providers.some((p) => p.key === storedKey) ? storedKey : '');
		} catch {
			catalogError = true;
		}
	}

	async function loadOAuthStatus() {
		oauthStatus = null;
		if (!provider || provider.auth !== 'oauth' || storedKey !== provider.key) return;
		try {
			oauthStatus = await getErpOAuthStatus();
		} catch {
			setStatus(m('org.erp.oauth.statusFailed'), 'failure');
		}
	}

	// The catalogue is admin-only and plan-gated in use, so it is fetched only
	// once the panel will actually render the form.
	let requested = false;
	$effect(() => {
		if (requested || !userLoaded || readOnly || !entitled) return;
		requested = true;
		void loadCatalog();
	});

	// The OAuth callback lands back here with `?erp_connected=` / `?erp_error=`.
	$effect(() => {
		const ret = readOAuthReturn(page.url.searchParams);
		if (!ret) return;
		if (ret.kind === 'connected') {
			const p = catalog?.providers.find((x) => x.key === ret.provider);
			setStatus(m('org.erp.oauth.returnConnected', { erp: p ? providerLabel(p) : ret.provider }), 'success');
		} else {
			setStatus(m('org.erp.oauth.returnError', { code: ret.code }), 'failure');
		}
	});

	function validate(p: ErpProvider): boolean {
		const missing = missingRequired(p, values, stored, mask);
		if (!missing.length) return true;
		setStatus(m('org.erp.missingRequired', { fields: formatList(missing.map((f) => m(f.label_key))) }), 'failure');
		return false;
	}

	async function save() {
		if (!provider || !validate(provider)) return;
		saving = true;
		setStatus('', null);
		try {
			const data = await api.patch<{ settings: Record<string, unknown> }>('/api/organization', {
				settings: { erp: buildErpPayload(provider, values, mergeErpType) }
			});
			onsaved?.(data);
			// What was typed into a secret is now stored; the form drops it and
			// shows the saved placeholder, the same as after a reload.
			const next = (data.settings.erp ?? undefined) as StoredErpConfig | undefined;
			values = initialValues(provider, next, mask);
			toast(m('org.toast.sectionSaved', { section: m('org.section.erpSaved') }), 'success');
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
			const result = await testErpConnection(buildErpPayload(provider, values, mergeErpType));
			setStatus(result.message, result.success ? 'success' : 'failure');
		} catch (err) {
			setStatus(err instanceof Error ? err.message : m('org.toast.testFailed'), 'failure');
		} finally {
			testing = false;
		}
	}

	async function connect() {
		if (!provider) return;
		connecting = true;
		setStatus(m('org.erp.oauth.redirecting', { erp: providerLabel(provider) }), null);
		try {
			const { authorize_url } = await startErpOAuth(provider.key);
			window.location.href = authorize_url;
		} catch (err) {
			connecting = false;
			setStatus(err instanceof Error ? err.message : m('org.toast.testFailed'), 'failure');
		}
	}

	async function disconnect() {
		if (!provider) return;
		disconnecting = true;
		try {
			await disconnectErpOAuth();
			oauthStatus = { provider: provider.key, connected: false };
			setStatus(m('org.erp.oauth.disconnected', { erp: providerLabel(provider) }), 'success');
		} catch (err) {
			setStatus(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'failure');
		} finally {
			disconnecting = false;
		}
	}

	const oauthConnected = $derived(
		!!provider && !!oauthStatus?.connected && oauthStatus.provider === provider.key
	);
</script>

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
		<p class="card-hint failure-text" role="alert">{m('org.erp.loadFailed')}</p>
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
								<option value={erp.value}>{erp.label}</option>
							{/each}
						</select>
					</label>
				</div>
				<p class="card-hint field-gap">{m('org.erp.mergeHint')}</p>
			{/if}

			{#if provider.auth === 'oauth'}
				<p class="card-hint field-gap">{m('org.erp.oauth.byoHint', { erp: providerLabel(provider) })}</p>
			{/if}

			<div class="form-grid">
				{#each provider.fields as f (f.name)}
					{@const saved = secretIsSaved(provider, f, stored, mask)}
					{@const helpId = f.help_key ? `${uid}-${f.name}-help` : undefined}
					<div class="field">
						<label>
							<span>{fieldLabel(f)}</span>
							{#if f.options}
								<select bind:value={values[f.name]} name={f.name} aria-describedby={helpId}>
									{#each f.options as option (option)}
										<option value={option}>{optionLabel(option)}</option>
									{/each}
								</select>
							{:else}
								<input
									type={f.secret ? 'password' : 'text'}
									name={f.name}
									autocomplete={f.secret ? 'new-password' : 'off'}
									bind:value={values[f.name]}
									required={f.required && !saved}
									placeholder={f.secret && saved ? m('org.erp.secretSaved') : (f.placeholder ?? '')}
									aria-describedby={helpId}
									data-secret-saved={f.secret ? String(saved) : undefined}
								/>
							{/if}
						</label>
						{#if f.help_key && helpId}
							<p class="field-hint" id={helpId}>{m(f.help_key)}</p>
						{/if}
					</div>
				{/each}
			</div>

			<p class="card-hint field-gap">
				<a href={provider.docs_url} target="_blank" rel="noopener noreferrer">
					{m('org.erp.docsLink', { erp: providerLabel(provider) })}
				</a>
			</p>

			{#if provider.auth === 'oauth'}
				<div class="oauth-row" data-testid="erp-oauth">
					{#if oauthConnected}
						<p class="oauth-state connected">
							{oauthStatus?.external_tenant_id
								? m('org.erp.oauth.connectedCompany', { erp: providerLabel(provider), id: oauthStatus.external_tenant_id })
								: m('org.erp.oauth.connected', { erp: providerLabel(provider) })}
						</p>
						<button class="btn-test" disabled={disconnecting} onclick={disconnect}>
							{disconnecting ? m('org.erp.oauth.disconnecting') : m('org.erp.oauth.disconnect')}
						</button>
					{:else}
						<p class="oauth-state" id="{uid}-oauth-state">
							{isStoredSelection
								? m('org.erp.oauth.notConnected', { erp: providerLabel(provider) })
								: m('org.erp.oauth.saveFirst')}
						</p>
						<button
							class="btn-save-section"
							disabled={connecting || !isStoredSelection || !provider.available}
							aria-describedby="{uid}-oauth-state"
							onclick={connect}
						>
							{m('org.erp.oauth.connect', { erp: providerLabel(provider) })}
						</button>
					{/if}
				</div>
			{/if}

			<div class="erp-test-row">
				<button class="btn-save-section" disabled={saving || !provider.available} onclick={save}>
					{saving ? m('org.common.saving') : m('org.erp.save')}
				</button>
				{#if provider.auth === 'credentials'}
					<button class="btn-test" disabled={testing || !provider.available} onclick={test}>
						{testing ? m('org.common.testing') : m('org.common.testConnection')}
					</button>
				{/if}
			</div>
		{/if}
	{/if}

	<!-- Always mounted, so a result is announced when it arrives (WCAG 4.1.3):
	     a live region inserted together with its text is not read by every
	     screen reader. -->
	<p
		class="test-result"
		role="status"
		data-testid="erp-status"
		class:success={statusTone === 'success'}
		class:failure={statusTone === 'failure'}
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

	.oauth-row,
	.erp-test-row {
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
