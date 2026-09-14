<script lang="ts">
	/**
	 * `/gl-accounts` — the chart of accounts.
	 *
	 * The third ERP-synced master data set behind 3-way matching, and until now
	 * the only one with no page: `/purchase-orders` and `/vendors` each carry
	 * their own Sync-from-ERP button gated on `auth.isManager`, while
	 * `POST /api/gl-accounts/sync-erp` — deliberately widened to `ap_manager`
	 * on the backend — was reachable only from the `/organization` Data Sync
	 * panel, a route the nav shows to `admin` alone, inside the blanket
	 * read-only `<fieldset>`. So the one role the write was widened to had no
	 * way to perform it, and `POST /api/gl-accounts` (create) had no caller at
	 * all. See `docs/decisions.md` §163.
	 *
	 * Shape copied from `/purchase-orders` + `/budgets` rather than invented:
	 * PageHeader shell, SearchBox + FilterChips, DataTable, URL-backed filter
	 * state, one request sequencer per independent request stream.
	 *
	 * The list read is deliberately UNPAGINATED (see `api/glAccounts.ts`), so
	 * the rows on screen are the whole answer for the active filters — which is
	 * why the footer states a plain count and there is no Load-more control to
	 * pair with a "Showing all N" claim.
	 */
	import { listGlAccounts, syncGlAccountsFromErp } from '$lib/api/glAccounts';
	import type { GlAccount } from '$lib/types/glAccount';
	import {
		GL_ACCOUNT_TYPES,
		GL_ACCOUNT_TYPE_LABEL_KEYS,
		glAccountTypeLabelKey
	} from '$lib/types/glAccount';
	import { auth } from '$lib/stores/auth.svelte';
	import { entityStore } from '$lib/stores/entity.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import GlAccountModal from '$lib/components/modals/GlAccountModal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { m } from '$lib/i18n/store.svelte';
	import { page } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import { untrack } from 'svelte';

	// Both writes on this router are require_roles(ADMIN, AP_MANAGER); the read
	// is role-open. A CFO reaches this page for the read (nav.ts) but holds
	// neither role, so a button shown to them could only 403 — the same gate
	// `/purchase-orders` and `/vendors` put on their own sync buttons.
	const canManage = $derived(auth.isManager);

	// --- Filter state (URL-backed) ---
	let search = $state($page.url.searchParams.get('search') ?? '');
	let typeFilter = $state<string>($page.url.searchParams.get('type') ?? 'all');
	// An inactive account is why a code "disappeared" from the pickers, so it
	// has to be reachable — but it is not what you want to read by default,
	// which is also the backend's default (`active_only=True`).
	let includeInactive = $state($page.url.searchParams.get('inactive') === '1');

	// --- Data ---
	let accounts = $state<GlAccount[]>([]);
	let loading = $state(true);
	/** A load has completed (either way) — gates the onboarding state. */
	let loaded = $state(false);
	/** The last load FAILED. Keeps the table from claiming an empty chart. */
	let errored = $state(false);

	let syncing = $state(false);
	let showCreate = $state(false);

	const TYPE_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		...GL_ACCOUNT_TYPES.map((t) => ({ key: t, label: m(GL_ACCOUNT_TYPE_LABEL_KEYS[t]) }))
	]);

	/**
	 * Scope is shown only on a multi-entity tenant — the same condition that
	 * gates the sidebar entity switcher. With one entity every row is either
	 * shared or the default entity's and the distinction has no consequence,
	 * so the column would be a constant.
	 */
	const showScope = $derived(entityStore.multiEntity);
	/** Status only earns a column once an inactive row can actually appear. */
	const showStatus = $derived(includeInactive);

	const COLUMNS = $derived([
		{ label: m('glAccounts.col.code') },
		{ label: m('glAccounts.col.name') },
		{ label: m('glAccounts.col.type') },
		{ label: m('glAccounts.col.parent') },
		...(showScope ? [{ label: m('glAccounts.col.scope') }] : []),
		{ label: m('glAccounts.col.erpId') },
		...(showStatus ? [{ label: m('glAccounts.col.status') }] : [])
	]);

	const filtersActive = $derived(search.trim() !== '' || typeFilter !== 'all');

	// Four distinct empty states (frontend/docs/ui-patterns.md § Data tables):
	// loading, errored, a filter that matched nothing, and a genuinely empty
	// chart. The last one gets the EmptyState block below instead of a cell.
	const emptyMessage = $derived(
		loading
			? m('common.loading')
			: errored
				? m('glAccounts.empty.errored')
				: filtersActive
					? m('glAccounts.empty.filtered')
					: m('glAccounts.empty')
	);

	const showOnboarding = $derived(
		loaded && !errored && accounts.length === 0 && !filtersActive
	);

	/**
	 * EVERY read here is untracked, and that is load-bearing rather than
	 * defensive. Svelte tracks reads transitively through the functions an
	 * effect calls, and **two** of this page's effects call `load()` — and so
	 * `buildParams()` — synchronously: the type chip's and the inactive
	 * toggle's. A tracked read of either filter therefore lands in BOTH
	 * effects' dependency sets, so one chip click re-runs both and issues two
	 * identical requests; the sequencer keeps the later one from clobbering the
	 * earlier, which is exactly what makes the duplicate invisible. A tracked
	 * `search` read is the same defect in its louder form — an immediate,
	 * un-debounced request per keystroke racing the 300ms timer (issue #168).
	 *
	 * Each effect declares the one filter it actually depends on by reading it
	 * directly, so nothing in here needs to be a dependency. `untrack` still
	 * reads the live value — the request always carries current state.
	 *
	 * (`/budgets` leaves `dimensionFilter` tracked here and gets away with it
	 * because only one of its effects calls `load()` synchronously. Don't copy
	 * that half of the pattern onto a page with two.)
	 */
	function buildParams() {
		const term = untrack(() => search).trim();
		const type = untrack(() => typeFilter);
		return {
			...(term ? { search: term } : {}),
			...(type !== 'all' ? { account_type: type } : {}),
			// Always sent: omitting it is not the same as `false` — the backend
			// defaults `active_only` to true.
			active_only: !untrack(() => includeInactive)
		};
	}

	/**
	 * Reflect the live filter state into the URL. EVERY read in here is
	 * untracked, `$page.url` included: syncUrl() is a WRITER called from the
	 * filter effects, not a source of dependencies — the URL read would
	 * self-trigger the effect that writes it via replaceState
	 * (`effect_update_depth_exceeded`), and a tracked `search` read is issue
	 * #168 again. Same shape as `/budgets` and `/recurring`.
	 */
	function syncUrl() {
		untrack(() => {
			const url = new URL($page.url);
			if (typeFilter !== 'all') url.searchParams.set('type', typeFilter);
			else url.searchParams.delete('type');
			if (search.trim()) url.searchParams.set('search', search.trim());
			else url.searchParams.delete('search');
			if (includeInactive) url.searchParams.set('inactive', '1');
			else url.searchParams.delete('inactive');
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	// One sequencer for the list stream (latest-issued wins), so a slow
	// response for an earlier filter can't land after a faster later one.
	// `syncFromErp` re-fetches rather than editing rows in place, so nothing
	// here needs `supersedeInFlight()`. See frontend/docs/ui-patterns.md
	// § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	async function load() {
		const token = fetchSequence.start();
		loading = true;
		try {
			const rows = await listGlAccounts(buildParams());
			if (!fetchSequence.canCommit(token)) return;
			accounts = rows;
			errored = false;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: a superseded request's failure
			// is not this table's news — the newer one owns the error state, and
			// blanking the rows here would discard what it is about to publish.
			if (!fetchSequence.isCurrentRequest(token)) return;
			errored = true;
			// **Clear the rows.** `errored` only reaches the reader through
			// `emptyMessage`, which `DataTable` renders on `isEmpty` alone — so
			// a SECOND failed load (a chip click or a search after one good
			// fetch) would otherwise leave the previous filter's rows on screen
			// with nothing but a toast that fades, and the count footer below
			// would keep reporting that stale number as the answer to filters
			// it never ran. `/exceptions` sets the same precedent for the same
			// reason.
			accounts = [];
			toast(err instanceof Error ? err.message : m('glAccounts.toast.loadFailed'), 'error');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loaded = true;
			}
		}
	}

	async function syncFromErp() {
		syncing = true;
		try {
			const result = await syncGlAccountsFromErp();
			toast(result.message, 'success');
			await load();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('glAccounts.toast.syncFailed'), 'error');
		} finally {
			syncing = false;
		}
	}

	$effect(() => {
		// The entity names behind `entity_id`, and the switcher's own gate.
		entityStore.ensureLoaded();
	});

	// Deliberately NO probe of `settings.erp` to choose between "sync it from
	// your ERP" and "add them by hand" in the onboarding copy. `GET
	// /api/organization` projects `settings` BY ROLE
	// (`services/org_settings_view.NON_ADMIN_SETTINGS`), so the answer a
	// non-admin gets for that key is a partial projection rather than the
	// tenant's real configuration — and a page that asks a question whose
	// answer depends on who is reading, only to pick a sentence, is how
	// `/organization` came to present platform defaults as tenant settings
	// (decisions §153). One description covers both routes truthfully.

	// A chip / toggle is a discrete action: fetch immediately. Both skip their
	// own mount-time run — a Svelte `$effect` always fires once regardless of
	// whether its tracked value changed — except the first one, which IS the
	// mount load.
	let typeEffectRan = false;
	$effect(() => {
		typeFilter;
		if (!typeEffectRan) {
			typeEffectRan = true;
			// Mount: honour a bookmarked ?search=/?type=/?inactive=1 and load once.
			void load();
			return;
		}
		syncUrl();
		void load();
	});

	let inactiveEffectRan = false;
	$effect(() => {
		includeInactive;
		if (!inactiveEffectRan) {
			inactiveEffectRan = true;
			return;
		}
		syncUrl();
		void load();
	});

	let searchTimer: ReturnType<typeof setTimeout>;
	let searchEffectRan = false;
	$effect(() => {
		search;
		if (!searchEffectRan) {
			searchEffectRan = true;
			return;
		}
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => {
			syncUrl();
			void load();
		}, 300);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone, running syncUrl() / a fetch against a route
		// the user already left.
		return () => clearTimeout(searchTimer);
	});

	function typeLabel(type: string | null): string {
		if (!type) return '—';
		const key = glAccountTypeLabelKey(type);
		// Tolerant by design: `account_type` is a free-form column and an ERP
		// sync writes whatever its chart says, so an unrecognised value renders
		// raw rather than as an empty cell.
		return key ? m(key) : type;
	}

	/** The chart a row belongs to: the shared chart, or an entity by name. */
	function scopeLabel(entityId: string | null): string {
		if (!entityId) return m('glAccounts.scope.shared');
		return (
			entityStore.entities.find((e) => e.id === entityId)?.name ??
			m('glAccounts.scope.unknownEntity')
		);
	}
</script>

<PageHeader title={m('glAccounts.title')}>
	{#snippet actions()}
		{#if canManage}
			<button class="btn-primary" onclick={() => (showCreate = true)}>
				{m('glAccounts.action.new')}
			</button>
			<!-- `POST /api/gl-accounts/sync-erp` is require_roles(ADMIN, AP_MANAGER),
			     matching the buttons on /purchase-orders and /vendors. -->
			<button class="btn-outline" disabled={syncing} onclick={syncFromErp}>
				{syncing ? m('glAccounts.action.syncing') : m('glAccounts.action.syncErp')}
			</button>
		{/if}
	{/snippet}

	<div class="filter-row">
		<SearchBox
			bind:value={search}
			placeholder={m('glAccounts.search.placeholder')}
			ariaLabel={m('glAccounts.search.aria')}
		/>
		<FilterChips chips={TYPE_CHIPS} bind:active={typeFilter} />
		<label class="inactive-toggle">
			<input type="checkbox" bind:checked={includeInactive} />
			<span>{m('glAccounts.filter.includeInactive')}</span>
		</label>
	</div>

	{#if showOnboarding}
		<EmptyState
			icon="📒"
			testId="gl-accounts-empty-state"
			heading={m('glAccounts.onboarding.heading')}
			description={m('glAccounts.onboarding.description')}
			actionLabel={canManage ? m('glAccounts.action.new') : undefined}
			onaction={canManage ? () => (showCreate = true) : undefined}
		/>
	{:else}
		<DataTable columns={COLUMNS} isEmpty={accounts.length === 0} empty={emptyMessage}>
			{#snippet body()}
				{#each accounts as acct (acct.id)}
					<!-- `.row-muted` (not opacity) de-emphasises a retired account —
					     the shared idiom for a deactivated row. -->
					<tr class:row-muted={!acct.is_active}>
						<td class="mono">{acct.code}</td>
						<td>{acct.name}</td>
						<td>{typeLabel(acct.account_type)}</td>
						<td class="mono muted">{acct.parent_code ?? '—'}</td>
						{#if showScope}
							<td>
								{#if acct.entity_id}
									<Badge tone="neutral" variant="entity-scoped">
										{scopeLabel(acct.entity_id)}
									</Badge>
								{:else}
									<Badge tone="accent" variant="shared">{m('glAccounts.scope.shared')}</Badge>
								{/if}
							</td>
						{/if}
						<td class="mono muted">{acct.erp_account_id ?? '—'}</td>
						{#if showStatus}
							<td>
								{#if acct.is_active}
									<Badge tone="success" variant="active">{m('glAccounts.status.active')}</Badge>
								{:else}
									<Badge tone="muted" variant="inactive">{m('glAccounts.status.inactive')}</Badge>
								{/if}
							</td>
						{/if}
					</tr>
				{/each}
			{/snippet}
		</DataTable>

		{#if accounts.length > 0}
			<!-- A plain count, never "Showing all N": the read is unpaginated, so
			     this IS every row matching the active filters. -->
			<div class="count-row">
				<span class="count-line">
					{m('glAccounts.count', { n: accounts.length })}
				</span>
			</div>
		{/if}
	{/if}
</PageHeader>

{#if showCreate}
	<GlAccountModal onclose={() => (showCreate = false)} onsaved={() => void load()} />
{/if}

<style>
	.filter-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}
	.inactive-toggle {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.85rem;
		color: var(--text-muted);
		white-space: nowrap;
	}
	.count-row {
		display: flex;
		justify-content: center;
		padding: 4px 0 8px;
	}
	.count-line {
		font-size: 0.8rem;
		color: var(--text-muted);
	}
	.btn-outline {
		padding: 8px 16px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}
	.btn-outline:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.btn-outline:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
