<script lang="ts">
	import type { Contract, ContractStatus, ContractType } from '$lib/types/contract';
	import {
		CONTRACT_STATUSES,
		contractStatusLabelKey,
		STATUS_TONES,
		contractTypeLabelKey
	} from '$lib/types/contract';
	import Badge from '$lib/components/ui/Badge.svelte';
	import { contractStore } from '$lib/stores/contracts.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import {
		getContract,
		getContractIds,
		bulkContractStatus,
		exportContractsCsv,
		type ContractBulkAction
	} from '$lib/api/contracts';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import SortableHeader from '$lib/components/ui/SortableHeader.svelte';
	import BulkBar from '$lib/components/ui/BulkBar.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import ContractModal from '$lib/components/modals/ContractModal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { pruneSelection } from '$lib/utils/selection';
	import { toggleSort, type SortOrder } from '$lib/utils/sort';
	import { type MatchingIdsResponse } from '$lib/utils/pagination';
	import { m } from '$lib/i18n/store.svelte';
	import { page } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import { untrack } from 'svelte';
	import { formatDate } from '$lib/utils/time';

	const canCreate = $derived(auth.isManager);
	const canManage = $derived(auth.isManager);

	// The status name is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = (s: string) => {
		const key = contractStatusLabelKey(s);
		return key ? m(key) : s;
	};

	const STATUS_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		...CONTRACT_STATUSES.map((s) => ({ key: s, label: statusLabel(s) }))
	]);

	// URL-backed filter state (mirrors the invoices page convention).
	let search = $state($page.url.searchParams.get('search') ?? '');
	let statusFilter = $state<string>($page.url.searchParams.get('status') ?? 'all');

	// Modal state: null = create; a Contract = detail/edit.
	// Three states, not two: a failed load must not read as "nothing matched".
	let emptyMessage = $derived(
		contractStore.loading
			? m('common.loading')
			: contractStore.errored
				? m('contracts.empty.errored')
				: m('contracts.empty')
	);

	let showCreate = $state(false);
	let editing = $state<Contract | null>(null);

	// Column sort — URL-backed (`?sort=&order=`), folded into the same
	// syncUrl() the search/status filters already use. `null` field = the
	// backend's own default order (most-recent first).
	let sortField = $state<string | null>($page.url.searchParams.get('sort'));
	let sortOrder = $state<SortOrder>(($page.url.searchParams.get('order') as SortOrder) ?? 'desc');

	// --- Bulk selection ---
	let selected = $state<Set<string>>(new Set());
	let selectedAllMatching = $state(false);
	let selectingAllMatching = $state(false);
	let bulkBusy = $state(false);
	let bulkAction = $state<ContractBulkAction>('activate');

	$effect(() => {
		if (selectedAllMatching) return;
		const pruned = pruneSelection(
			selected,
			contractStore.all.map((c) => c.id)
		);
		if (pruned !== selected) selected = pruned;
	});

	let allSelected = $derived(
		contractStore.all.length > 0 && contractStore.all.every((c) => selected.has(c.id))
	);

	function toggleSelect(id: string) {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	function toggleSelectAll() {
		if (allSelected) {
			selected = new Set();
			selectedAllMatching = false;
		} else {
			selected = new Set(contractStore.all.map((c) => c.id));
		}
	}

	function clearSelection() {
		selected = new Set();
		selectedAllMatching = false;
	}

	// `untrack` on the `search` read: buildParams() is called from `load()`,
	// which the filter `$effect` below calls directly — and Svelte tracks reads
	// transitively through called functions, so a plain read here would make
	// that effect depend on `search` and re-fire it on every keystroke,
	// un-debounced, racing the dedicated 300ms timer (issue #168). `untrack`
	// still reads the CURRENT value, so the request carries the live search
	// term; it just stops the read registering as the caller's dependency.
	function buildParams() {
		const params: { status?: string; search?: string; sort?: string; order?: SortOrder } = {};
		if (statusFilter !== 'all') params.status = statusFilter as ContractStatus;
		const currentSearch = untrack(() => search);
		if (currentSearch.trim()) params.search = currentSearch.trim();
		const currentSort = untrack(() => sortField);
		if (currentSort) {
			params.sort = currentSort;
			params.order = untrack(() => sortOrder);
		}
		return params;
	}

	// Reflect the live filter state into the URL. EVERY read in here is
	// untracked, `$page.url` included, because syncUrl() is a WRITER called
	// from the filter `$effect`s below — not a source of dependencies:
	//   - the URL read would self-trigger the effect that writes it via
	//     replaceState (Svelte effect_update_depth_exceeded);
	//   - a tracked `search` read would make every filter effect depend on
	//     `search`, so each keystroke re-fired it: an immediate, un-debounced
	//     load racing the dedicated 300ms debounce timer. That is issue #168,
	//     fixed on /invoices, /payments and /vendors but never carried to this
	//     page. Each effect declares the filters it actually depends on by
	//     reading them directly, so nothing here needs to be tracked.
	function syncUrl() {
		untrack(() => {
			const url = new URL($page.url);
			if (statusFilter !== 'all') url.searchParams.set('status', statusFilter);
			else url.searchParams.delete('status');
			if (search.trim()) url.searchParams.set('search', search.trim());
			else url.searchParams.delete('search');
			if (sortField) {
				url.searchParams.set('sort', sortField);
				url.searchParams.set('order', sortOrder);
			} else {
				url.searchParams.delete('sort');
				url.searchParams.delete('order');
			}
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	function handleSort(field: string) {
		const next = toggleSort({ field: sortField, order: sortOrder }, field);
		sortField = next.field;
		sortOrder = next.order;
		syncUrl();
		contractStore.fetch(buildParams()).catch(() => {}); // noqa: raw-fetch-in-component — store method, routes through api client
	}

	// Resolve and select EVERY contract matching the current filters (not
	// just the loaded page) via `GET /api/contracts/ids` — mirrors the
	// identical "select all N matching" affordance on /invoices, /expenses,
	// and /vendors.
	async function selectAllMatching() {
		selectingAllMatching = true;
		try {
			const params: { status?: string; search?: string } = {};
			if (statusFilter !== 'all') params.status = statusFilter;
			if (search.trim()) params.search = search.trim();
			const res: MatchingIdsResponse = await getContractIds(params);
			selected = new Set(res.ids);
			selectedAllMatching = true;
			if (res.truncated) {
				toast(
					`Selected the first ${res.ids.length} of ${res.total} matching — narrow your filters to select the rest.`,
					'error'
				);
			} else {
				toast(`Selected all ${res.ids.length} matching contract(s)`, 'success');
			}
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Failed to select all matching', 'error');
		} finally {
			selectingAllMatching = false;
		}
	}

	async function bulkStatusChange() {
		bulkBusy = true;
		try {
			const res = await bulkContractStatus([...selected], bulkAction);
			await contractStore.fetch(buildParams()); // noqa: raw-fetch-in-component — store method, routes through api client
			clearSelection();
			const msg = res.skipped.length
				? m('contracts.bulk.updated', { n: res.updated }) + m('contracts.bulk.skipped', { n: res.skipped.length })
				: m('contracts.bulk.updated', { n: res.updated });
			toast(msg, res.updated > 0 ? 'success' : 'error');
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Bulk status change failed', 'error');
		} finally {
			bulkBusy = false;
		}
	}

	async function bulkExport() {
		bulkBusy = true;
		try {
			const ids = [...selected];
			await exportContractsCsv(ids);
			toast(m('contracts.bulk.exported', { n: ids.length }), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Bulk export failed', 'error');
		} finally {
			bulkBusy = false;
		}
	}

	// `searchEffectRan` skips this effect's own mount-time run: a Svelte
	// `$effect` always fires once immediately regardless of whether its
	// tracked value actually changed, so without the guard this queued a
	// SECOND, redundant fetch ~300ms after the statusFilter effect below
	// already loaded the page once. `contractStore.fetch()` replaces the // noqa: raw-fetch-in-component — comment reference, not a call
	// list wholesale, so if a create/edit lands in that window, the
	// delayed duplicate can resolve afterward and silently clobber it
	// with a stale snapshot — same class of bug fixed in UsersPanel.svelte.
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
			// Fire-and-forget: the store re-throws so an awaiting caller keeps its
			// own handling, but nothing awaits here — `contractStore.errored` is what
			// the table's empty state reads. Swallow so a failed load isn't an
			// unhandled rejection in the console.
			contractStore.fetch(buildParams()).catch(() => {}); // noqa: raw-fetch-in-component — store method, routes through api client
		}, 300);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone, running syncUrl()/a list fetch against a route
		// the user already left.
		return () => clearTimeout(searchTimer);
	});

	$effect(() => {
		statusFilter;
		syncUrl();
		// See the debounce effect above: swallow the rejection, the store's
		// `errored` flag is what the UI reads.
		contractStore.fetch(buildParams()).catch(() => {}); // noqa: raw-fetch-in-component — store method, routes through api client
	});

	// No vendor fetch here any more. These options ARE the set of valid choices,
	// so a truncated list is not a shorter list — it is a supplier the operator
	// cannot pick. Walking every page on mount was the previous answer, and it
	// cost one request per 100 suppliers on a page that may never open the
	// modal. `ui/VendorPicker` inside `ContractModal` searches them server-side
	// instead, so a vendor on page 40 is one query away and nothing is fetched
	// until the form is opened.

	// Deep-link: `/contracts?id=<uuid>` opens that contract's detail modal.
	let deepLinkLoaded = $state<string | null>(null);
	$effect(() => {
		const id = $page.url.searchParams.get('id');
		if (!id || deepLinkLoaded === id) return;
		deepLinkLoaded = id;
		getContract(id)
			.then((c) => (editing = c))
			.catch(() => toast(m('contracts.notFound'), 'error'));
	});

	async function openDetail(c: Contract) {
		// Fetch the full record so the spend summary + line items are present
		// (the list payload omits the heavier `spend` block on some backends).
		try {
			editing = await getContract(c.id);
		} catch {
			editing = c;
		}
	}

	function closeModal() {
		editing = null;
		showCreate = false;
		const url = new URL($page.url);
		if (url.searchParams.has('id')) {
			url.searchParams.delete('id');
			replaceState(`${url.pathname}${url.search}`, {});
			deepLinkLoaded = null;
		}
	}

	function onSaved(c: Contract) {
		contractStore.upsert(c);
		// Keep the open detail modal in sync after a lifecycle action.
		if (editing && editing.id === c.id) editing = c;
	}

	function typeLabel(t: ContractType): string {
		const key = contractTypeLabelKey(t);
		return key ? m(key) : t;
	}
</script>

<PageHeader title={m('contracts.title')}>
	{#snippet actions()}
		{#if canCreate}
			<button class="btn-primary" onclick={() => (showCreate = true)}>{m('contracts.action.new')}</button>
		{/if}
	{/snippet}

	<div class="filter-row">
		<SearchBox bind:value={search} placeholder={m('contracts.search.placeholder')} ariaLabel={m('contracts.search.aria')} />
		<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} />
	</div>

	<DataTable
		isEmpty={contractStore.all.length === 0}
		empty={emptyMessage}
		colspan={8}
	>
		{#snippet header()}
			<tr>
				{#if canManage}
					<th class="checkbox-col">
						<input type="checkbox" aria-label={m('contracts.selectAllAria')} checked={allSelected} onchange={toggleSelectAll} />
					</th>
				{/if}
				<SortableHeader field="contract_number" label={m('contracts.col.contractNumber')} active={sortField === 'contract_number'} order={sortOrder} onsort={handleSort} />
				<th scope="col">{m('contracts.col.vendor')}</th>
				<th scope="col">{m('contracts.col.type')}</th>
				<SortableHeader field="status" label={m('contracts.col.status')} active={sortField === 'status'} order={sortOrder} onsort={handleSort} />
				<SortableHeader field="end_date" label={m('contracts.col.endDate')} active={sortField === 'end_date'} order={sortOrder} onsort={handleSort} />
				<SortableHeader field="total_value" label={m('contracts.col.value')} class="right" active={sortField === 'total_value'} order={sortOrder} onsort={handleSort} />
				<th scope="col" class="right">{m('contracts.col.spend')}</th>
			</tr>
		{/snippet}
		{#snippet body()}
			{#each contractStore.all as contract (contract.id)}
				<tr
					class="clickable"
					class:row-selected={selected.has(contract.id)}
					onclick={(e) => {
						if (isRowOpenClick(e)) openDetail(contract);
					}}
				>
					{#if canManage}
						<td class="checkbox-col">
							<input
								type="checkbox"
								checked={selected.has(contract.id)}
								onclick={(e) => e.stopPropagation()}
								onchange={() => toggleSelect(contract.id)}
								aria-label={`Select ${contract.contract_number}`}
							/>
						</td>
					{/if}
					<td class="mono">
						<RowLink
							onclick={() => openDetail(contract)}
							ariaLabel={m('contracts.row.open', { number: contract.contract_number })}
						>
							{contract.contract_number}
						</RowLink>
					</td>
					<td>{contract.vendor_name ?? '—'}</td>
					<td>{typeLabel(contract.contract_type)}</td>
					<td>
						<Badge tone={STATUS_TONES[contract.status]} variant={contract.status}>
							{statusLabel(contract.status)}
						</Badge>
					</td>
					<td class="muted">{formatDate(contract.end_date)}</td>
					<td class="right mono"><Money amount={contract.total_value} currency={contract.currency} /></td>
					<td class="right mono">
						{#if contract.spend}
							<span class:over-limit={contract.spend.over_limit}>
								<Money amount={contract.spend.invoiced_total} currency={contract.currency} />
							</span>
							{#if contract.spend.over_limit}
								<span class="over-tag" title={m('contracts.overSpendLimit')}>!</span>
							{/if}
						{:else}
							—
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>

	{#if contractStore.hasMore}
		<div class="load-more-row">
			<button class="btn-load-more" onclick={() => contractStore.loadMore()} disabled={contractStore.loading}>
				{contractStore.loading
					? m('common.loading')
					: m('contracts.loadMore', { shown: contractStore.all.length, total: contractStore.total })}
			</button>
		</div>
	{:else if contractStore.total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('contracts.showingAll', { total: contractStore.total })}</span>
		</div>
	{/if}

	{#if canManage}
		<BulkBar count={selected.size} onclear={clearSelection}>
			{#snippet actions()}
				{#if allSelected && !selectedAllMatching && contractStore.total > contractStore.all.length}
					<button class="bulk-action-btn secondary" disabled={selectingAllMatching} onclick={selectAllMatching}>
						{selectingAllMatching
							? m('common.loading')
							: m('common.selectAllMatching', { total: contractStore.total })}
					</button>
				{:else if selectedAllMatching}
					<span class="bulk-all-matching-note">{m('common.allMatchingSelected')}</span>
				{/if}
				<select class="bulk-status-select" bind:value={bulkAction} aria-label={m('contracts.bulk.newStatusAria')} disabled={bulkBusy}>
					<option value="activate">{m('contracts.modal.lifecycle.activate')}</option>
					<option value="terminate">{m('contracts.modal.lifecycle.terminate')}</option>
					<option value="cancel">{m('contracts.modal.lifecycle.cancel')}</option>
				</select>
				<button class="bulk-action-btn" disabled={bulkBusy} onclick={bulkStatusChange}>{m('contracts.bulk.changeStatus')}</button>
				<div class="bulk-divider"></div>
				<button class="bulk-action-btn" disabled={bulkBusy} onclick={bulkExport}>{m('contracts.bulk.exportCsv')}</button>
			{/snippet}
		</BulkBar>
	{/if}
</PageHeader>

{#if showCreate}
	<ContractModal contract={null} onclose={closeModal} onsaved={onSaved} />
{/if}

{#if editing}
	<ContractModal contract={editing} onclose={closeModal} onsaved={onSaved} />
{/if}

<style>
	/* Page-specific bits; shared design-system CSS lives in app.css. */
	.filter-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	/* The status pill is `<Badge>` now. This file and `ContractModal` used to
	   tint the same five statuses at two different alphas (.12 here, .15
	   there) — one owner, one answer. */

	.over-limit {
		color: var(--danger);
		font-weight: 600;
	}

	.over-tag {
		display: inline-block;
		margin-left: 4px;
		width: 16px;
		height: 16px;
		line-height: 16px;
		text-align: center;
		border-radius: 50%;
		background: var(--danger-strong);
		color: #fff;
		font-size: 0.7rem;
		font-weight: 700;
	}

	/* Bulk-bar — mirrors /vendors and /expenses. */
	.bulk-status-select {
		padding: 6px 30px 6px 10px;
		border-radius: 6px;
		background-color: var(--surface);
		font-size: 0.82rem;
	}
	.bulk-action-btn {
		padding: 6px 14px;
		border-radius: 6px;
		border: 1px solid var(--accent-strong);
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}
	.bulk-action-btn:hover:not(:disabled) {
		filter: brightness(1.1);
	}
	.bulk-action-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.bulk-action-btn.secondary {
		background: transparent;
		color: var(--accent-strong);
	}
	.bulk-all-matching-note {
		font-size: 0.82rem;
		color: var(--text-muted);
		white-space: nowrap;
	}
	.bulk-divider {
		width: 1px;
		height: 20px;
		background: var(--border);
	}
</style>
