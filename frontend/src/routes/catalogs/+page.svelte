<script lang="ts">
	import type { Catalog, GuidedBuyingSuggestion } from '#lib/types/catalog.ts';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { catalogTypeLabelKey, guidedBuyingReasonLabelKey } from '#lib/types/catalog.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import {
		listCatalogs,
		deleteCatalog as apiDeleteCatalog,
		guidedBuying,
		listGlAccounts,
		type GlAccountOption
	} from '#lib/api/catalogs.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import SearchBox from '#lib/components/ui/SearchBox.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import CatalogModal from '#lib/components/modals/CatalogModal.svelte';
	import PunchoutModal from '#lib/components/modals/PunchoutModal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { goto } from '$app/navigation';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	const canCreate = $derived(auth.isManager); // admin | ap_manager
	// Buyers (admin / ap_manager / ap_clerk) may start a punch-out session.
	const canPunchout = $derived(auth.hasAnyRole('admin', 'ap_manager', 'ap_clerk'));

	// Catalog type and guided-buying reason are message keys, not English
	// literals — a value this build doesn't know renders raw rather than
	// disappearing (a reason nobody can read is still better evidence than
	// none).
	const typeLabel = (t: string) => {
		const key = catalogTypeLabelKey(t);
		return key ? m(key) : t;
	};

	const reasonLabel = (r: string) => {
		const key = guidedBuyingReasonLabelKey(r);
		return key ? m(key) : r;
	};

	// --- List state ---
	const PAGE_SIZE = 100;

	let catalogs = $state<Catalog[]>([]);
	let total = $state(0);
	let loading = $state(true);
	// Three states, not two. `isEmpty` used to be gated on `!loading`, so during
	// the first fetch the table rendered a header with NOTHING under it — no
	// rows, no spinner, no message — and a failed load (reported only by a toast
	// that then faded) left "No catalogs yet." standing forever. Same rule
	// `/exceptions` and the payments queue follow.
	let errored = $state(false);
	let pageNum = $state(1);
	let loadingMore = $state(false);

	// The list is paged: `total` is the whole filtered set, so a footer that
	// says "Showing all {total}" is only true once every row is loaded.
	let hasMore = $derived(catalogs.length < total);
	let search = $state('');
	let typeFilter = $state<string>('all'); // all | internal | punchout | preferred

	// --- Modal + selection state ---
	let showCreate = $state(false);
	let editing = $state<Catalog | null>(null);
	let confirmDeleteId = $state<string | null>(null);
	let punchoutCatalog = $state<Catalog | null>(null);

	// --- Lookups (shared with the modal) ---
	// Vendors are NOT fetched here any more: `ui/VendorPicker` inside the modal
	// searches them server-side, so the page no longer pays for a vendor list
	// on every load — nor caps one at the first 100 rows.
	let glAccounts = $state<GlAccountOption[]>([]);

	// --- Guided buying panel ---
	let showGuided = $state(false);
	let guidedCategory = $state('');
	let guidedQuery = $state('');
	let guided = $state<GuidedBuyingSuggestion | null>(null);
	let guidedLoading = $state(false);

	const TYPE_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		{ key: 'internal', label: m('catalogs.filter.internal') },
		{ key: 'punchout', label: m('catalogs.filter.punchout') },
		{ key: 'preferred', label: m('catalogs.filter.preferred') }
	]);

	const COLUMNS = $derived([
		{ label: m('catalogs.col.name') },
		{ label: m('catalogs.col.type') },
		{ label: m('catalogs.col.items'), class: 'right' },
		{ label: m('catalogs.col.status') },
		{ label: '', class: 'actions-col' }
	]);

	let searchTimer: ReturnType<typeof setTimeout> | undefined;

	// Sequences `load` (latest-issued wins) so a slow response for an earlier
	// search term can't land after a faster later one. `handleDelete` drops a row
	// in place with no fetch of its own, so it retires whatever is in flight
	// first — otherwise the deleted catalog reappears when the load already out
	// resolves. (`onSaved` refetches, so it needs no supersede.) See
	// `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		try {
			const params: {
				search?: string;
				catalog_type?: string;
				is_preferred?: boolean;
				page?: number;
				page_size: number;
			} = { page: nextPage, page_size: PAGE_SIZE };
			if (search.trim()) params.search = search.trim();
			if (typeFilter === 'preferred') params.is_preferred = true;
			else if (typeFilter !== 'all') params.catalog_type = typeFilter;
			const res = await listCatalogs(params);
			// Superseded by a newer load, or by a local delete.
			if (!fetchSequence.canCommit(token)) return;
			catalogs = opts.append ? appendUnique(catalogs, res.items) : res.items;
			total = res.total;
			pageNum = nextPage;
			errored = false;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// delete still failed, and no newer load is coming to report it.
			if (!fetchSequence.isCurrentRequest(token)) return;
			errored = true;
			toast(err instanceof Error ? err.message : m('catalogs.toast.loadFailed'), 'error');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	// Debounce text search: re-fetch 280ms after the last keystroke. Reading
	// `search` registers the dependency so edits re-run this effect.
	$effect(() => {
		const q = search;
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => load(), q === '' ? 0 : 280);
		return () => clearTimeout(searchTimer);
	});

	// Initial lookup (GL accounts) — once.
	$effect(() => {
		(async () => {
			try {
				glAccounts = await listGlAccounts();
			} catch {
				glAccounts = [];
			}
		})();
	});

	function onSaved(_c: Catalog) {
		load();
	}

	async function handleDelete(id: string) {
		if (confirmDeleteId !== id) {
			confirmDeleteId = id;
			return;
		}
		try {
			await apiDeleteCatalog(id);
			fetchSequence.supersedeInFlight();
			catalogs = catalogs.filter((c) => c.id !== id);
			total = Math.max(0, total - 1);
			toast(m('catalogs.toast.deleted'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('catalogs.toast.deleteFailed'), 'error');
		} finally {
			confirmDeleteId = null;
		}
	}

	async function runGuided() {
		guidedLoading = true;
		try {
			guided = await guidedBuying({
				category: guidedCategory.trim() || undefined,
				q: guidedQuery.trim() || undefined
			});
		} catch (err) {
			toast(err instanceof Error ? err.message : m('catalogs.toast.guidedFailed'), 'error');
		} finally {
			guidedLoading = false;
		}
	}

	function toggleGuided() {
		showGuided = !showGuided;
		if (showGuided && guided === null) runGuided();
	}
</script>

<PageHeader title={m('catalogs.title')}>
	{#snippet actions()}
		<button class="btn-secondary" onclick={toggleGuided}>
			{showGuided ? m('catalogs.action.hideGuided') : m('catalogs.action.showGuided')}
		</button>
		{#if canCreate}
			<button class="btn-primary" onclick={() => (showCreate = true)}>{m('catalogs.action.new')}</button>
		{/if}
	{/snippet}

	{#if showGuided}
		<section class="guided-panel">
			<div class="guided-controls">
				<input
					type="text"
					placeholder={m('catalogs.guided.categoryPlaceholder')}
					aria-label={m('catalogs.guided.categoryAria')}
					bind:value={guidedCategory}
				/>
				<input
					type="text"
					placeholder={m('catalogs.guided.searchPlaceholder')}
					aria-label={m('catalogs.guided.searchAria')}
					bind:value={guidedQuery}
				/>
				<button class="btn-primary" onclick={runGuided} disabled={guidedLoading}>
					{guidedLoading ? m('catalogs.guided.finding') : m('catalogs.guided.find')}
				</button>
			</div>

			{#if guided}
				<div class="guided-grid">
					<div class="guided-col">
						<h4>{m('catalogs.guided.preferredVendors')}</h4>
						{#if guided.preferred_vendors.length === 0}
							<p class="muted">{m('catalogs.guided.noPreferredVendors')}</p>
						{:else}
							{#each guided.preferred_vendors as v (v.vendor_id + (v.catalog_id ?? ''))}
								<div class="guided-card">
									<strong>{v.vendor_name}</strong>
									<div class="reasons">
										{#each v.reasons as r}
											<span class="reason">{reasonLabel(r)}</span>
										{/each}
									</div>
									{#if v.catalog_name}<span class="sub">{m('catalogs.guided.catalogLabel', { name: v.catalog_name })}</span>{/if}
									{#if v.contract_number}<span class="sub">{m('catalogs.guided.contractLabel', { number: v.contract_number })}</span>{/if}
								</div>
							{/each}
						{/if}
					</div>

					<div class="guided-col">
						<div class="help-row">
							<h4>{m('catalogs.guided.inContractVendors')}</h4>
							<HelpTip term="contract" />
						</div>
						{#if guided.in_contract_vendors.length === 0}
							<p class="muted">{m('catalogs.guided.noContractVendors')}</p>
						{:else}
							{#each guided.in_contract_vendors as v (v.vendor_id)}
								<div class="guided-card">
									<strong>{v.vendor_name}</strong>
									{#if v.contract_number}<span class="sub">{m('catalogs.guided.contractLabel', { number: v.contract_number })}</span>{/if}
								</div>
							{/each}
						{/if}
					</div>

					<div class="guided-col">
						<h4>{m('catalogs.guided.matchingItems')}</h4>
						{#if guided.items.length === 0}
							<p class="muted">{m('catalogs.guided.noMatchingItems')}</p>
						{:else}
							{#each guided.items as it (it.catalog_item_id)}
								<div class="guided-card">
									<div class="item-head">
										<strong>{it.name}</strong>
										{#if it.is_preferred}<span class="reason">{m('catalogs.guided.preferredTag')}</span>{/if}
									</div>
									<span class="sub">
										{it.catalog_name}{#if it.unit_price != null} ·
											<Money amount={it.unit_price} currency={it.currency} />{/if}
									</span>
								</div>
							{/each}
						{/if}
					</div>
				</div>
			{/if}
		</section>
	{/if}

	<div class="toolbar-row">
		<SearchBox bind:value={search} placeholder={m('catalogs.search.placeholder')} ariaLabel={m('catalogs.search.aria')} />
	</div>

	<FilterChips chips={TYPE_CHIPS} bind:active={typeFilter} onchange={() => load()} />

	{#if errored}
		<!-- A failed load is a dead end without a way back: the toast that
		     reported it has already faded. Same error-with-retry block
		     `/admin/api-keys` uses. -->
		<div class="state error" data-testid="catalogs-error" role="alert">
			<p>{m('catalogs.empty.errored')}</p>
			<button type="button" class="btn-cancel" onclick={() => load()}>{m('catalogs.retry')}</button>
		</div>
	{:else}
	<DataTable
		columns={COLUMNS}
		isEmpty={catalogs.length === 0}
		empty={loading ? m('common.loading') : m('catalogs.empty')}
	>
		{#snippet body()}
			{#each catalogs as c (c.id)}
				<tr class="clickable" onclick={(e) => { if (isRowOpenClick(e)) editing = c; }}>
					<td>
						<RowLink onclick={() => (editing = c)} ariaLabel={m('catalogs.row.open', { name: c.name })}>
							{c.name}
							{#if c.is_preferred}<span class="pref-dot" title={m('catalogs.preferredTitle')}>★</span>{/if}
						</RowLink>
					</td>
					<td>{typeLabel(c.catalog_type)}</td>
					<td class="right">{c.item_count}</td>
					<td>{c.is_active ? m('catalogs.status.active') : m('catalogs.status.inactive')}</td>
					<td class="actions">
						{#if canPunchout && c.catalog_type === 'punchout'}
							<RowAction onclick={() => (punchoutCatalog = c)}>{m('catalogs.row.punchout')}</RowAction>
						{/if}
						{#if canCreate}
							<RowAction
								variant="danger"
								armed={confirmDeleteId === c.id}
								onclick={() => handleDelete(c.id)}
							>
								{confirmDeleteId === c.id ? m('catalogs.row.confirm') : m('catalogs.row.delete')}
							</RowAction>
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>
	{/if}

	{#if hasMore}
		<div class="load-more-row">
			<button
				class="btn-load-more"
				onclick={() => load({ append: true })}
				disabled={loadingMore}
			>
				{loadingMore
					? m('common.loading')
					: m('catalogs.loadMore', { shown: catalogs.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('catalogs.showingAll', { total })}</span>
		</div>
	{/if}
</PageHeader>

{#if showCreate}
	<CatalogModal
		catalog={null}
		{glAccounts}
		onclose={() => (showCreate = false)}
		onsaved={onSaved}
	/>
{/if}

{#if editing}
	<CatalogModal
		catalog={editing}
		{glAccounts}
		onclose={() => (editing = null)}
		onsaved={onSaved}
	/>
{/if}

{#if punchoutCatalog}
	<PunchoutModal
		catalog={punchoutCatalog}
		onclose={() => (punchoutCatalog = null)}
		onconverted={(requisitionId) => {
			punchoutCatalog = null;
			// Deep-link straight to the new draft requisition's detail modal.
			goto(`/requisitions?id=${requisitionId}`);
		}}
	/>
{/if}

<style>
	/* Error-with-retry block — same shape as `/admin/api-keys`. */
	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}

	.state.error {
		color: var(--danger);
	}

	.state.error p {
		margin: 0 0 8px;
	}

	.toolbar-row {
		display: flex;
		gap: 10px;
		align-items: center;
	}

	.pref-dot {
		color: #d4940a;
		margin-left: 4px;
	}

	.guided-panel {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 14px 16px;
		background: var(--surface);
	}

	.guided-controls {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
		align-items: center;
		margin-bottom: 12px;
	}

	.guided-controls input {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.86rem;
		min-width: 180px;
	}
	.guided-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
		gap: 14px;
	}

	.guided-col h4 {
		margin: 0 0 8px;
		font-size: 0.85rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	.guided-card {
		display: flex;
		flex-direction: column;
		gap: 3px;
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 6px;
		margin-bottom: 8px;
		font-size: 0.85rem;
	}

	.item-head {
		display: flex;
		align-items: center;
		gap: 8px;
		justify-content: space-between;
	}

	.reasons {
		display: flex;
		gap: 4px;
		flex-wrap: wrap;
	}

	.reason {
		display: inline-block;
		padding: 1px 7px;
		border-radius: 10px;
		font-size: 0.7rem;
		font-weight: 600;
		background: rgba(31, 168, 106, 0.15);
		color: #1fa86a;
	}

	.sub {
		color: var(--text-muted);
		font-size: 0.78rem;
	}

	.muted {
		color: var(--text-muted);
		font-size: 0.82rem;
	}

</style>
