<script lang="ts">
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { goto } from '$app/navigation';
	import { formatDate } from '#lib/utils/time.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import PlanUpgradeNotice from '#lib/components/ui/PlanUpgradeNotice.svelte';
	import { FEATURE_PUBLIC_API } from '#lib/types/planFeatures.ts';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import SecretReveal from '#lib/components/ui/SecretReveal.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import {
		listApiKeys,
		createApiKey,
		revokeApiKey,
		getApiKeyUsage
	} from '#lib/api/apiKeys.ts';
	import type { ApiKey, ApiKeyCreated, ApiKeyUsage } from '#lib/types/apiKeys.ts';

	// RBAC: the backend gates every /api/api-keys endpoint to admin only and 403s
	// the rest. Wait for `auth.user` to resolve before redirecting so we don't
	// bounce before /me lands (the billing/discounts/audit pages document the
	// same race). `isAdmin` is exactly the admin role.
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	// Plan gate (decisions §258): the server refuses this with a 402 when the
	// plan lacks it, so the page shows the upgrade prompt instead of the
	// create control (the notice waits for `auth.user`, so nothing flashes
	// before /me loads).
	const canMintKey = $derived(auth.hasFeature(FEATURE_PUBLIC_API));

	// $derived so the column headers re-render when the locale changes.
	let COLUMNS = $derived([
		{ label: m('admin.apiKeys.col.name') },
		{ label: m('admin.apiKeys.col.keyPrefix') },
		{ label: m('admin.apiKeys.col.scopes') },
		{ label: m('admin.apiKeys.col.created') },
		{ label: m('admin.apiKeys.col.lastUsed') },
		{ label: m('admin.apiKeys.col.status') },
		{ class: 'actions-col' }
	]);

	let keys = $state<ApiKey[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Create flow.
	let creating = $state(false);
	let newName = $state('');
	let saving = $state(false);

	// One-time plaintext reveal (after a successful mint). `SecretReveal` owns the
	// copy affordance + the shown-once warning; this page only holds the value
	// long enough to render it and drops it on close.
	let minted = $state<ApiKeyCreated | null>(null);

	// Revoke confirm (armed two-click on the row action).
	let confirmRevokeId = $state<string | null>(null);

	// Per-key usage view.
	let usageKey = $state<ApiKey | null>(null);
	let usage = $state<ApiKeyUsage | null>(null);
	let usageLoading = $state(false);
	let usageError = $state<string | null>(null);
	// Most-recent 30 days, narrowed away from null so the table snippet (a
	// closure, which loses the `{:else if usage}` narrowing) can read it.
	const usageDays = $derived(usage ? usage.daily.slice(0, 30) : []);

	// The usage panel is a re-issuable fetch keyed on which key was clicked, so
	// it takes a request-identity guard (`frontend/CLAUDE.md` § Sequencing list
	// fetches). Its own sequencer — the key LIST above is an independent stream.
	const usageSequence = createRequestSequencer();

	async function load() {
		loading = true;
		error = null;
		try {
			keys = await listApiKeys();
		} catch (e) {
			error = e instanceof Error ? e.message : m('admin.apiKeys.loadFailed');
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		// Only fetch once we know the role is allowed (avoids a guaranteed 403 for
		// a non-admin before the redirect fires).
		if (userLoaded && allowed) load();
	});

	function openCreate() {
		newName = '';
		creating = true;
	}

	async function handleCreate() {
		const name = newName.trim();
		if (!name) return;
		saving = true;
		try {
			const created = await createApiKey(name);
			creating = false;
			newName = '';
			// Show the plaintext exactly once. Keep the list fresh too.
			minted = created;
			await load();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.apiKeys.toast.createFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	function dismissMinted() {
		// Drop the plaintext from memory the moment the reveal closes.
		minted = null;
	}

	async function handleRevoke(id: string) {
		try {
			await revokeApiKey(id);
			toast(m('admin.apiKeys.toast.revoked'), 'success');
			await load();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.apiKeys.toast.revokeFailed'), 'error');
		} finally {
			confirmRevokeId = null;
		}
	}

	async function openUsage(key: ApiKey) {
		const token = usageSequence.start();
		usageKey = key;
		usage = null;
		usageError = null;
		usageLoading = true;
		try {
			const res = await getApiKeyUsage(key.id, 30);
			// Open one key's usage, close it, open another: the first response must
			// not land in the panel now showing the second.
			if (!usageSequence.canCommit(token)) return;
			usage = res;
		} catch (e) {
			// `isCurrentRequest`, not `canCommit`: only the newest request reports.
			if (!usageSequence.isCurrentRequest(token)) return;
			usageError = e instanceof Error ? e.message : m('admin.apiKeys.toast.usageFailed');
		} finally {
			if (usageSequence.isCurrentRequest(token)) usageLoading = false;
		}
	}

	function handleWindowClick(e: MouseEvent) {
		if (confirmRevokeId && !(e.target as HTMLElement).closest('.row-action')) {
			confirmRevokeId = null;
		}
	}


	function isRevoked(k: ApiKey): boolean {
		return k.revoked_at !== null;
	}
</script>

<svelte:window onclick={handleWindowClick} />

<PageHeader title={m('admin.apiKeys.title')}>
	{#snippet actions()}
		{#if canMintKey}
			<button class="btn-primary" onclick={openCreate}>{m('admin.apiKeys.createKey')}</button>
		{/if}
	{/snippet}

	<p class="page-hint">
		{m('admin.apiKeys.hintPre')}<code>X-API-Key</code>{m('admin.apiKeys.hintPost')}
		<HelpTip term="api-key" />
	</p>

	{#if auth.user && !canMintKey}
		<PlanUpgradeNotice feature={FEATURE_PUBLIC_API} testId="api-keys-plan-upgrade" />
	{/if}

	{#if loading}
		<p class="state" data-testid="api-keys-loading">{m('admin.apiKeys.loading')}</p>
	{:else if error}
		<div class="state error" data-testid="api-keys-error" role="alert">
			<p>{error}</p>
			<button type="button" class="btn-cancel" onclick={load}>{m('admin.apiKeys.retry')}</button>
		</div>
	{:else}
		<DataTable
			columns={COLUMNS}
			isEmpty={keys.length === 0}
			empty={m('admin.apiKeys.empty')}
		>
			{#snippet body()}
				{#each keys as key (key.id)}
					<tr
						class="clickable"
						class:row-muted={isRevoked(key)}
						onclick={(e) => {
							if (isRowOpenClick(e)) openUsage(key);
						}}
					>
						<td>
							<RowLink onclick={() => openUsage(key)} ariaLabel={m('admin.apiKeys.viewUsageAria', { name: key.name })}>
								{key.name}
							</RowLink>
						</td>
						<td class="mono">{key.key_prefix}…</td>
						<td>{key.scopes.join(', ')}</td>
						<td>{formatDate(key.created_at)}</td>
						<td>{formatDate(key.last_used_at)}</td>
						<td class="status-col">
							{#if isRevoked(key)}
								<Badge tone="muted" variant="revoked">{m('admin.apiKeys.statusRevoked')}</Badge>
							{:else}
								<Badge tone="success" variant="active">{m('admin.apiKeys.statusActive')}</Badge>
							{/if}
						</td>
						<td class="actions">
							{#if !isRevoked(key)}
								<RowAction
									variant="danger"
									armed={confirmRevokeId === key.id}
									onclick={(e) => {
										e.stopPropagation();
										if (confirmRevokeId === key.id) {
											handleRevoke(key.id);
										} else {
											confirmRevokeId = key.id;
										}
									}}
								>
									{confirmRevokeId === key.id ? m('admin.apiKeys.row.confirm') : m('admin.apiKeys.row.revoke')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{/if}
</PageHeader>

<!-- Create key modal -->
<Modal open={creating} ariaLabel={m('admin.apiKeys.create.aria')} width="sm" onclose={() => (creating = false)}>
	<h2>{m('admin.apiKeys.create.heading')}</h2>
	<p class="modal-hint">
		{m('admin.apiKeys.create.hintPre')} <strong>{m('admin.apiKeys.create.hintScope')}</strong> {m('admin.apiKeys.create.hintPost')}
	</p>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			handleCreate();
		}}
	>
		<label>
			<span>{m('admin.apiKeys.field.name')} <em class="required">*</em></span>
			<input type="text" bind:value={newName} required maxlength="120" placeholder={m('admin.apiKeys.field.namePlaceholder')} />
		</label>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (creating = false)}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={!newName.trim() || saving}>
				{saving ? m('admin.apiKeys.create.creating') : m('admin.apiKeys.create.create')}
			</button>
		</div>
	</form>
</Modal>

<!-- One-time plaintext reveal -->
<SecretReveal
	open={minted !== null}
	ariaLabel={m('admin.apiKeys.reveal.aria')}
	heading={m('admin.apiKeys.reveal.heading')}
	warningStrong={m('admin.apiKeys.reveal.warningStrong')}
	warning={m('admin.apiKeys.reveal.warning')}
	secret={minted?.key ?? ''}
	testId="minted-key"
	copyLabel={m('admin.apiKeys.reveal.copy')}
	copiedLabel={m('admin.apiKeys.reveal.copied')}
	copiedToast={m('admin.apiKeys.toast.copied')}
	copyFailedToast={m('admin.apiKeys.toast.copyFailed')}
	doneLabel={m('admin.apiKeys.reveal.done')}
	meta={minted
		? [
				{ label: m('admin.apiKeys.reveal.name'), value: minted.api_key.name },
				{
					label: m('admin.apiKeys.reveal.prefix'),
					value: `${minted.api_key.key_prefix}…`,
					mono: true
				}
			]
		: []}
	onclose={dismissMinted}
/>

<!-- Per-key usage view -->
<Modal open={usageKey !== null} ariaLabel={m('admin.apiKeys.usage.aria')} width="md" onclose={() => (usageKey = null)}>
	{#if usageKey}
		<!--
			Both ids: `data-key-id` is the key the admin CLICKED (whose name the
			heading shows), `data-usage-for` is the key the RESPONSE claims.
			`usageSequence` is what keeps them equal, but a sequencer alone leaves
			that unassertable — heading and figures come from different sources, so
			one key's totals under another's name look like ordinary data. Same
			pairing as `/audit`'s verify-drill and `/experiments`' results readout.
		-->
		<div
			data-testid="api-key-usage"
			data-key-id={usageKey.id}
			data-usage-for={usage?.api_key_id ?? ''}
		>
			<h2>{m('admin.apiKeys.usage.heading', { name: usageKey.name })}</h2>
			{#if usageLoading}
				<p class="state" data-testid="usage-loading">{m('admin.apiKeys.usage.loading')}</p>
			{:else if usageError}
				<div class="state error" role="alert">
					<p>{usageError}</p>
					<button type="button" class="btn-cancel" onclick={() => openUsage(usageKey!)}>{m('admin.apiKeys.retry')}</button>
				</div>
			{:else if usage}
				<div class="usage-totals" data-testid="usage-totals">
					<div class="usage-stat">
						<span class="usage-num">{usage.total_requests.toLocaleString()}</span>
						<span class="usage-lbl">{m('admin.apiKeys.usage.totalRequests')}</span>
					</div>
					<div class="usage-stat">
						<span class="usage-num">{usage.window_requests.toLocaleString()}</span>
						<span class="usage-lbl">{m('admin.apiKeys.usage.windowDays', { days: usage.window_days })}</span>
					</div>
					<div class="usage-stat">
						<span class="usage-num">{formatDate(usage.last_used_at)}</span>
						<span class="usage-lbl">{m('admin.apiKeys.usage.lastUsed')}</span>
					</div>
				</div>

				<h3 class="usage-heading">{m('admin.apiKeys.usage.recentActivity')}</h3>
				{#if usage.daily.length === 0}
					<p class="state">{m('admin.apiKeys.usage.noRequests')}</p>
				{:else}
					<DataTable
						columns={[{ label: m('admin.apiKeys.usage.col.date') }, { label: m('admin.apiKeys.usage.col.requests'), class: 'num-col' }]}
						isEmpty={usageDays.length === 0}
						empty={m('admin.apiKeys.usage.noRequests')}
					>
						{#snippet body()}
							{#each usageDays as day (day.usage_date)}
								<tr>
									<td>{formatDate(day.usage_date)}</td>
									<td class="num-col">{day.request_count.toLocaleString()}</td>
								</tr>
							{/each}
						{/snippet}
					</DataTable>
				{/if}
			{/if}
		</div>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (usageKey = null)}>{m('admin.apiKeys.usage.close')}</button>
		</div>
	{/if}
</Modal>

<style>
	.page-hint {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.85rem;
		max-width: 720px;
	}

	.page-hint code {
		background: var(--surface-2);
		/* Not inherited from .page-hint: --text-muted on --surface-2 is 4.34:1,
		   below the 4.5:1 bar. A code literal is the emphasized token in the
		   sentence anyway, so muted was also backwards. */
		color: var(--text);
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.8em;
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

	/*
	 * The status pill is `<Badge>` now. The revoked variant used to need a
	 * hand-tuned 0.12 tint because it paired the grey tint with --text-muted,
	 * which lands at 4.32:1 over --surface at 0.15. `--muted-on-tint` is the
	 * companion calibrated for exactly that background, so the standard 0.15
	 * tone clears the bar without the local exception — which is what the pair
	 * exists for. See `frontend/CLAUDE.md` § Colour tokens and contrast.
	 */

	/*
	 * A revoked key's DATA is de-emphasised by the shared `.row-muted` recipe
	 * in app.css (a muted colour token), not by an `opacity` fade. The fade
	 * composited the row's whole subtree toward the backdrop, which is why it
	 * needed a `:not(.status-col)` carve-out — the muted status pill dropped to
	 * 2.44:1 under it, making the one cell that explains the fade the least
	 * readable thing in the row. A colour token needs no carve-out: a
	 * descendant setting its own colour (the `<Badge>` pair) keeps its own
	 * calibrated contrast, so the exclusion is gone with the fade.
	 */

	/* Usage view */
	.usage-totals {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 1rem;
		margin-bottom: 1.25rem;
	}

	.usage-stat {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}

	.usage-num {
		font-size: 1.25rem;
		font-weight: 700;
	}

	.usage-lbl {
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.usage-heading {
		margin: 0 0 0.5rem;
		font-size: 0.95rem;
	}

	.num-col {
		text-align: right;
	}
</style>
