<script lang="ts">
	import { api } from '$lib/api';
	import { appendUnique } from '$lib/utils/pagination';
	import type { MatchingIdsResponse } from '$lib/utils/pagination';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { pruneSelection } from '$lib/utils/selection';
	import { toggleSort, type SortOrder } from '$lib/utils/sort';
	import { untrack } from 'svelte';
	// Aliased: this page already has a local `page` variable for the loaded
	// vendor-list page number (below) — `$app/stores`'s page is the URL/route
	// store, unrelated, and the two names would otherwise collide.
	import { page as urlStore } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import SortableHeader from '$lib/components/ui/SortableHeader.svelte';
	import BulkBar from '$lib/components/ui/BulkBar.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import ScreeningBadge from '$lib/components/ui/ScreeningBadge.svelte';
	import VendorModal from '$lib/components/modals/VendorModal.svelte';
	import VendorConsolidationModal from '$lib/components/modals/VendorConsolidationModal.svelte';
	import ImportCsvModal from '$lib/components/modals/ImportCsvModal.svelte';
	import CreateVendorModal from '$lib/components/modals/CreateVendorModal.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import InviteVendorPortalUserModal from '$lib/components/modals/InviteVendorPortalUserModal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { auth } from '$lib/stores/auth.svelte';
	import { PERM_VENDOR_MANAGE } from '$lib/types/admin';
	import { m } from '$lib/i18n/store.svelte';
	import { importVendorsCsv, type PortalInviteResult } from '$lib/api/vendors';
	import {
		VENDOR_STATUS_TONES,
		vendorSourceLabelKey,
		vendorStatusLabelKey
	} from '$lib/types/vendor';
	import type { Vendor, VendorBankDetails } from '$lib/types/vendor';
	import type { ImportResult } from '$lib/types/csvImport';
	import { getVendorIds, bulkVendorStatus, bulkScreenVendors, exportVendorsCsv } from '$lib/api/vendors';

	type BankDetails = VendorBankDetails;

	let vendors = $state<Vendor[]>([]);
	let detailVendor = $state<Vendor | null>(null);
	// Vendor consolidation ("Merge into canonical") — admin / ap_manager hold
	// vendor.manage by default; the action surfaces only for them. Gated on the
	// granular permission, not a role check (mirrors the backend gate).
	let showConsolidation = $state(false);
	// Day-0 CSV import — `POST /api/vendors/import-csv` is
	// `require_roles(ADMIN, AP_MANAGER)`, the same plain-role gate as the ERP
	// sync button above, so this reuses `auth.isManager` rather than the
	// granular `vendor.manage` permission (which could diverge under a
	// custom role split).
	let showImportCsv = $state(false);
	const canManageVendors = $derived(auth.can(PERM_VENDOR_MANAGE));
	// "+ New Vendor" — `POST /api/vendors` is `require_permission(vendor.manage)`,
	// so it surfaces on the same granular gate as verify/reject/bank.
	let showCreateVendor = $state(false);
	// Supplier-portal invite — `POST /api/vendors/{id}/portal-users` is
	// `require_roles(ADMIN, AP_MANAGER)`, the plain-role gate (like ERP sync /
	// CSV import), hence `auth.isManager` not the granular permission.
	let inviteVendor = $state<Vendor | null>(null);
	// Confirmation of a sent invite. Deliberately NOT a credential: the backend
	// no longer returns the temp password (it is emailed to the supplier), so
	// there is nothing here to reveal-once, copy, or leave in the DOM.
	let inviteResult = $state<PortalInviteResult | null>(null);
	let bankEditing = $state<Vendor | null>(null);
	let bankForm = $state<BankDetails>({
		counterparty_id: '',
		account_last4: '',
		routing_last4: '',
		wire_routing_last4: '',
		bank_name: '',
		country: '',
		mailing_address: { street: '', city: '', state: '', postal: '', country: '' }
	});
	let savingBank = $state(false);
	// GB uses a 6-digit sort code, not a 9-digit US ABA routing number — the
	// destination-bank `country` field (distinct from the check-mailing
	// `mailing_address.country`) drives which label/shape the routing/sort
	// field below shows. Backend validation mirrors this: `schemas.vendor
	// .VendorBankChangeRequest` checks `sort_code` via `validate_uk_sort_code`
	// only when present, same "only when present" posture as `routing_number`.
	const bankIsUK = $derived((bankForm.country || '').trim().toUpperCase() === 'GB');

	function openBankEditor(v: Vendor) {
		bankEditing = v;
		bankForm = {
			counterparty_id: v.bank_details?.counterparty_id ?? '',
			account_last4: v.bank_details?.account_last4 ?? '',
			routing_last4: v.bank_details?.routing_last4 ?? '',
			wire_routing_last4: v.bank_details?.wire_routing_last4 ?? '',
			bank_name: v.bank_details?.bank_name ?? '',
			country: v.bank_details?.country ?? '',
			mailing_address: {
				street: v.bank_details?.mailing_address?.street ?? '',
				city: v.bank_details?.mailing_address?.city ?? '',
				state: v.bank_details?.mailing_address?.state ?? '',
				postal: v.bank_details?.mailing_address?.postal ?? '',
				country: v.bank_details?.mailing_address?.country ?? ''
			}
		};
	}

	async function saveBankDetails() {
		if (!bankEditing) return;
		savingBank = true;
		try {
			// Bank-detail changes are dual-control (BEC / bank-redirect gate): this
			// does NOT apply the change — it stages a request a second approver must
			// sign off on via the change-request queue. So we don't optimistically
			// update the row, and the toast says "submitted", not "saved".
			await api.post(`/api/vendors/${bankEditing.id}/bank-change`, {
				bank_details: bankForm
			});
			// Name the queue the request landed in — "submitted for approval" left
			// the reviewer with nowhere to go, which is how the whole dual-control
			// gate ended up unreachable from the app.
			toast(m('vendors.bank.toast.submitted'), 'success');
			bankEditing = null;
		} catch (err) {
			const e = err as { detail?: string; message?: string } | null;
			toast(e?.detail ?? e?.message ?? m('vendors.bank.toast.submitFailed'), 'error');
		} finally {
			savingBank = false;
		}
	}

	// Replace a vendor in the list (and keep the open detail modal in sync) after
	// a mutation the detail modal reports — re-screen, risk recompute,
	// block/unblock, enrichment apply. NOT the bank-detail editor below:
	// `saveBankDetails` stages a dual-control change request and applies
	// nothing locally, so there is no row to sync until a second approver
	// signs it off.
	function applyVendorUpdate(updated: Vendor) {
		// A fetch already in flight read this vendor BEFORE the mutation landed,
		// so its response would revert the change (a lifted payment block
		// reappearing, a fresh screening verdict going back to stale). Retire
		// every pre-edit request before applying.
		fetchSequence.supersedeInFlight();
		vendors = vendors.map((v) => (v.id === updated.id ? updated : v));
		if (detailVendor && detailVendor.id === updated.id) detailVendor = updated;
	}
	// Search + status filter are URL-backed (`?search=&status=`) alongside sort
	// (below) so a reload / back-button / shared link reproduces the same view —
	// mirrors `/contracts` + `/expenses`. See `syncUrl()`.
	const VENDOR_STATUSES = ['active', 'unverified', 'inactive', 'rejected'];
	let search = $state($urlStore.url.searchParams.get('search') ?? '');
	let statusFilter = $state(
		VENDOR_STATUSES.includes($urlStore.url.searchParams.get('status') ?? '')
			? ($urlStore.url.searchParams.get('status') as string)
			: 'all'
	);
	let syncing = $state(false);

	const PAGE_SIZE = 20;
	let total = $state(0);
	let page = $state(1);
	let loadingMore = $state(false);
	// First fetch landed (success or error) — gates the onboarding empty state
	// so "Add your first vendor" doesn't flash during the initial load.
	let loaded = $state(false);
	let loadErrored = $state(false);
	let hasMore = $derived(vendors.length < total);

	// Column sort — URL-backed (`?sort=&order=`) so it survives a reload/share,
	// mirroring the /expenses `syncUrl()` pattern (see `syncSortUrl` below).
	// `null` field = the backend's own default order (name ascending).
	let sortField = $state<string | null>($urlStore.url.searchParams.get('sort'));
	let sortOrder = $state<SortOrder>(($urlStore.url.searchParams.get('order') as SortOrder) ?? 'asc');

	// --- Bulk selection ---
	let selected = $state<Set<string>>(new Set());
	// True once "Select all N matching" has resolved the whole filtered set
	// (not just the loaded page) into `selected` — mirrors the identical
	// mechanism on /invoices and /expenses (`selectAllMatching` below).
	let selectedAllMatching = $state(false);
	let selectingAllMatching = $state(false);
	let bulkBusy = $state(false);

	// Prune the selection to ids still visible whenever the list refetches, so
	// `selected` can't retain ids that fell off the list (inflating the
	// bulk-bar count and feeding invisible ids into a bulk mutation).
	$effect(() => {
		if (selectedAllMatching) return;
		const pruned = pruneSelection(
			selected,
			vendors.map((v) => v.id)
		);
		if (pruned !== selected) selected = pruned;
	});

	let allSelected = $derived(vendors.length > 0 && vendors.every((v) => selected.has(v.id)));

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
			selected = new Set(vendors.map((v) => v.id));
		}
	}

	// Status tallies over the WHOLE (search-scoped) vendor set, from
	// GET /api/vendors/counts — so the chip counts (and the red Unverified
	// attention badge) reflect every page, not just the loaded one.
	let statusCounts = $state<Record<string, number>>({});
	let countsTotal = $state(0);

	// Status and source are message keys, not English literals — an
	// unrecognised value from the API renders raw rather than blank. Both maps
	// live in `$lib/types/vendor.ts` beside `VENDOR_STATUS_TONES`, so the badge
	// can't name a status the tone map tints differently.
	const statusLabel = (s: string) => {
		const key = vendorStatusLabelKey(s);
		return key ? m(key) : s;
	};

	const sourceLabel = (s: string) => {
		const key = vendorSourceLabelKey(s);
		return key ? m(key) : s;
	};

	// Debounce timer for search input — mirrors the /invoices and /payments
	// convention (300ms).
	let searchTimer: ReturnType<typeof setTimeout>;
	function debouncedFetch() {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => {
			syncUrl();
			fetchVendors();
		}, 300);
	}

	// Fetch on mount and whenever the status filter chip changes (a chip click
	// is a discrete action, so it fetches immediately — no debounce). This is
	// the ONLY effect that unconditionally calls fetchVendors(); a second
	// effect doing the same on mount used to double-fetch on load.
	$effect(() => {
		statusFilter;
		syncUrl();
		fetchVendors();
	});

	// Re-fetch on search input (debounced).
	$effect(() => {
		search;
		debouncedFetch();
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone and lands a stale list into the shared store.
		return () => clearTimeout(searchTimer);
	});

	// Sequences fetchVendors calls (fetch and load-more alike — one shared
	// counter, latest-issued wins) so a slow response for an earlier
	// search/filter can't land after a faster later one and clobber the list.
	// `applyVendorUpdate` marks in-flight fetches stale the same way, so a
	// response issued before a local edit can't overwrite it either.
	const fetchSequence = createRequestSequencer();
	// The chip counts fire alongside every list fetch, so they need their own
	// sequencer for the same reason — see `fetchCounts`.
	const countsSequence = createRequestSequencer();

	async function fetchVendors(opts: { append?: boolean; nextPage?: number } = {}) {
		const token = fetchSequence.start();
		try {
			const nextPage = opts.nextPage ?? 1;
			const params = new URLSearchParams({
				page: String(nextPage),
				page_size: String(PAGE_SIZE)
			});
			// `untrack`: this function is also called from the statusFilter
			// `$effect` above. A plain read of `search` here would make THAT
			// effect depend on `search` too (Svelte tracks reads transitively
			// through called functions), so every keystroke would re-fire it —
			// an immediate, un-debounced fetch racing the dedicated debounce
			// timer. `untrack` still reads the current value (the request still
			// carries the live search term); it just stops that read from
			// registering as a dependency of whichever effect calls this.
			const currentSearch = untrack(() => search);
			if (currentSearch.trim()) params.set('search', currentSearch.trim());
			if (statusFilter !== 'all') params.set('status', statusFilter);
			// Same `untrack` reasoning as `search` above: this function is also
			// called from the status/search effects, and a plain read here would
			// make THOSE effects depend on the sort state too.
			const currentSort = untrack(() => sortField);
			if (currentSort) {
				params.set('sort', currentSort);
				params.set('order', untrack(() => sortOrder));
			}
			const data = await api.get<{ items: Vendor[]; total: number }>(
				`/api/vendors?${params}`
			);
			// Superseded by a newer fetch, or by a local edit.
			if (!fetchSequence.canCommit(token)) return;
			vendors = opts.append ? appendUnique(vendors, data.items) : data.items;
			total = data.total;
			page = nextPage;
			loadErrored = false;
			if (!opts.append) fetchCounts();
		} catch {
			// `isCurrentRequest`, not `canCommit`: a request superseded by a
			// local edit still failed, and no newer request is coming to report
			// it — only a newer *fetch* makes this one's outcome irrelevant.
			if (fetchSequence.isCurrentRequest(token)) {
				toast('Failed to load vendors', 'error');
				loadErrored = true;
			}
		} finally {
			if (fetchSequence.isCurrentRequest(token)) loaded = true;
		}
	}

	async function loadMoreVendors() {
		loadingMore = true;
		try {
			await fetchVendors({ append: true, nextPage: page + 1 });
		} finally {
			loadingMore = false;
		}
	}

	// Reflect the live filter state (search + status + sort) into the URL —
	// mirrors `/contracts` + `/expenses`. EVERY read here is untracked,
	// `$urlStore.url` included: this is a WRITER called from the filter
	// `$effect`s and the debounce timer, never a dependency source — a tracked
	// `$urlStore` read would self-trigger the effect that calls `replaceState`,
	// and a tracked `search` read would make every filter effect re-fire on
	// each keystroke (issue #168).
	function syncUrl() {
		untrack(() => {
			const url = new URL($urlStore.url);
			const s = search.trim();
			if (s) url.searchParams.set('search', s);
			else url.searchParams.delete('search');
			if (statusFilter !== 'all') url.searchParams.set('status', statusFilter);
			else url.searchParams.delete('status');
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
		fetchVendors();
	}

	// Resolve and select EVERY vendor matching the current filters (not just
	// the loaded page) via `GET /api/vendors/ids` — mirrors the identical
	// "select all N matching" affordance on /invoices and /expenses.
	async function selectAllMatching() {
		selectingAllMatching = true;
		try {
			const params: { search?: string; status?: string } = {};
			const currentSearch = search.trim();
			if (currentSearch) params.search = currentSearch;
			if (statusFilter !== 'all') params.status = statusFilter;
			const res: MatchingIdsResponse = await getVendorIds(params);
			selected = new Set(res.ids);
			selectedAllMatching = true;
			if (res.truncated) {
				toast(
					`Selected the first ${res.ids.length} of ${res.total} matching — narrow your filters to select the rest.`,
					'error'
				);
			} else {
				toast(`Selected all ${res.ids.length} matching vendor(s)`, 'success');
			}
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Failed to select all matching', 'error');
		} finally {
			selectingAllMatching = false;
		}
	}

	function clearSelection() {
		selected = new Set();
		selectedAllMatching = false;
	}

	async function bulkVerify() {
		bulkBusy = true;
		try {
			const res = await bulkVendorStatus([...selected], 'active');
			await fetchVendors();
			clearSelection();
			const msg = res.skipped.length
				? m('vendors.bulk.verified', { n: res.updated }) + m('vendors.bulk.skipped', { n: res.skipped.length })
				: m('vendors.bulk.verified', { n: res.updated });
			toast(msg, res.updated > 0 ? 'success' : 'error');
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Bulk verify failed', 'error');
		} finally {
			bulkBusy = false;
		}
	}

	async function bulkReject() {
		bulkBusy = true;
		try {
			const res = await bulkVendorStatus([...selected], 'rejected');
			await fetchVendors();
			clearSelection();
			const msg = res.skipped.length
				? m('vendors.bulk.rejected', { n: res.updated }) + m('vendors.bulk.skipped', { n: res.skipped.length })
				: m('vendors.bulk.rejected', { n: res.updated });
			toast(msg, res.updated > 0 ? 'success' : 'error');
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Bulk reject failed', 'error');
		} finally {
			bulkBusy = false;
		}
	}

	async function bulkScreen() {
		bulkBusy = true;
		try {
			const res = await bulkScreenVendors([...selected]);
			await fetchVendors();
			clearSelection();
			const msg = res.skipped.length
				? m('vendors.bulk.screened', { n: res.screened }) + m('vendors.bulk.skipped', { n: res.skipped.length })
				: m('vendors.bulk.screened', { n: res.screened });
			toast(msg, res.screened > 0 ? 'success' : 'error');
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Bulk screen failed', 'error');
		} finally {
			bulkBusy = false;
		}
	}

	async function bulkExport() {
		bulkBusy = true;
		try {
			const ids = [...selected];
			await exportVendorsCsv(ids);
			toast(m('vendors.bulk.exported', { n: ids.length }), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Bulk export failed', 'error');
		} finally {
			bulkBusy = false;
		}
	}

	async function fetchCounts() {
		// Sequenced for the same reason the payments History chips are: this
		// fires from the debounced-search path, so a slow response for an older
		// term can otherwise land after a faster newer one and leave the chips
		// describing a search the table is no longer showing.
		const token = countsSequence.start();
		try {
			const params = new URLSearchParams();
			if (search.trim()) params.set('search', search.trim());
			const qs = params.toString();
			const data = await api.get<{ total: number; by_status: Record<string, number> }>(
				`/api/vendors/counts${qs ? `?${qs}` : ''}`
			);
			if (!countsSequence.canCommit(token)) return;
			statusCounts = data.by_status ?? {};
			countsTotal = data.total ?? 0;
		} catch {
			// Non-fatal: chips fall back to the loaded-page tallies below.
			if (!countsSequence.isCurrentRequest(token)) return;
			statusCounts = {};
			countsTotal = 0;
		}
	}

	async function verifyVendor(id: string) {
		try {
			await api.post(`/api/vendors/${id}/verify`, {});
			await fetchVendors();
			toast('Vendor verified', 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Verify failed', 'error');
		}
	}

	async function rejectVendor(id: string) {
		try {
			await api.post(`/api/vendors/${id}/reject`, {});
			await fetchVendors();
			toast('Vendor rejected', 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Reject failed', 'error');
		}
	}

	async function syncFromErp() {
		syncing = true;
		try {
			const result = await api.post<{ message: string }>('/api/vendors/sync-erp', {});
			await fetchVendors();
			toast(result.message, 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Sync failed', 'error');
		} finally {
			syncing = false;
		}
	}

	// Prefer the server-side tallies (whole set); fall back to the loaded page
	// only if the counts endpoint failed (countsTotal === 0 with rows present).
	let allCount = $derived(countsTotal || vendors.length);
	let unverifiedCount = $derived(
		statusCounts.unverified ?? vendors.filter((v) => v.status === 'unverified').length
	);

	let statusChips = $derived([
		{ key: 'all', label: m('common.all'), count: allCount },
		{
			key: 'unverified',
			label: m('vendors.filter.unverified'),
			count: unverifiedCount > 0 ? unverifiedCount : undefined,
			alert: true
		},
		{ key: 'active', label: m('vendors.filter.active') },
		{ key: 'rejected', label: m('vendors.filter.rejected') }
	]);
	// Only the true "this tenant has zero vendors" state points at the CSV
	// import CTA — a search/status filter that matches nothing gets the plain
	// message, since "import a CSV" would be a non-sequitur when the tenant
	// already has vendors and the filter is just narrow.
	let emptyMessage = $derived(
		vendors.length === 0 && !search.trim() && statusFilter === 'all'
			? m('vendors.empty.fresh')
			: m('vendors.empty')
	);
	// A brand-new tenant with zero vendors and no active filter gets the rich
	// onboarding CTA (mirrors the dashboard / `/invoices` EmptyState) instead of
	// the bare "no vendors" cell — gated on `loaded` (not mid-load) and no error.
	let showOnboarding = $derived(
		loaded &&
			!loadErrored &&
			vendors.length === 0 &&
			total === 0 &&
			!search.trim() &&
			statusFilter === 'all'
	);
</script>

<PageHeader title={m('vendors.title')}>
	{#snippet actions()}
		{#if canManageVendors}
			<button class="btn-primary" onclick={() => (showCreateVendor = true)}>
				{m('vendors.action.newVendor')}
			</button>
		{/if}
		{#if auth.isManager}
			<!-- The dual-control queue a staged bank/tax change waits in. Role-gated
			     to match `GET /api/vendors/change-requests` (admin | ap_manager). -->
			<a class="btn-outline" href="/vendors/change-requests">
				{m('vendors.action.changeApprovals')}
			</a>
		{/if}
		{#if canManageVendors}
			<button class="btn-outline" onclick={() => (showConsolidation = true)}>
				Merge duplicates
			</button>
		{/if}
		{#if auth.isManager}
			<!-- `POST /api/vendors/sync-erp` is require_roles(ADMIN, AP_MANAGER).
			     A CFO reaches this page (nav.ts admits them for the read) but
			     holds neither role, so the button only ever 403'd. -->
			<button class="btn-outline" disabled={syncing} onclick={syncFromErp}>
				{syncing ? m('vendors.action.syncing') : m('vendors.action.syncErp')}
			</button>
		{/if}
		{#if auth.isManager}
			<!-- Day-0 bulk load — `POST /api/vendors/import-csv`, same gate as
			     the sync button above. See backend/docs/csv-import.md. -->
			<button class="btn-outline" onclick={() => (showImportCsv = true)}>
				{m('vendors.action.importCsv')}
			</button>
		{/if}
	{/snippet}

	<div class="filter-row">
		<SearchBox bind:value={search} placeholder={m('vendors.search.placeholder')} ariaLabel={m('vendors.search.aria')} />
		<FilterChips chips={statusChips} bind:active={statusFilter} />
	</div>

	{#if showOnboarding}
		<EmptyState
			icon="🏢"
			testId="vendors-empty-state"
			heading={m('vendors.onboarding.heading')}
			description={m('vendors.onboarding.description')}
			actionLabel={canManageVendors ? m('vendors.action.newVendor') : undefined}
			onaction={canManageVendors ? () => (showCreateVendor = true) : undefined}
		/>
	{:else}
	<DataTable isEmpty={vendors.length === 0} empty={emptyMessage} colspan={10} fixed>
		{#snippet header()}
			<tr>
				<th class="checkbox-col">
					<input type="checkbox" aria-label={m('vendors.selectAllAria')} checked={allSelected} onchange={toggleSelectAll} />
				</th>
				<SortableHeader field="name" label={m('vendors.col.vendor')} active={sortField === 'name'} order={sortOrder} onsort={handleSort} />
				<SortableHeader field="code" label={m('vendors.col.code')} active={sortField === 'code'} order={sortOrder} onsort={handleSort} />
				<th scope="col">{m('vendors.col.email')}</th>
				<SortableHeader field="status" label={m('vendors.col.status')} active={sortField === 'status'} order={sortOrder} onsort={handleSort} />
				<th scope="col">{m('vendors.col.screening')}</th>
				<th scope="col">{m('vendors.col.source')}</th>
				<th scope="col">{m('vendors.col.invoices')}</th>
				<th scope="col">{m('vendors.col.erp')}</th>
				<th class="actions-col"></th>
			</tr>
		{/snippet}
		{#snippet body()}
			{#each vendors as v (v.id)}
				<tr
						class="clickable"
						class:row-selected={selected.has(v.id)}
						class:unverified={v.status === 'unverified'}
						class:rejected={v.status === 'rejected'}
						onclick={(e) => {
							if (isRowOpenClick(e)) detailVendor = v;
						}}
					>
						<td class="checkbox-col">
							<input
								type="checkbox"
								checked={selected.has(v.id)}
								onclick={(e) => e.stopPropagation()}
								onchange={() => toggleSelect(v.id)}
								aria-label={`Select ${v.name}`}
							/>
						</td>
						<td class="vendor-name">
							<RowLink onclick={() => (detailVendor = v)} ariaLabel={`Open vendor ${v.name}`}>
								{v.name}
							</RowLink>
						</td>
					<td class="mono muted">{v.code ?? '—'}</td>
					<td class="muted">{v.email ?? '—'}</td>
					<td>
						<Badge
							tone={VENDOR_STATUS_TONES[v.status] ?? 'neutral'}
							variant="status-badge {v.status}"
						>
							{statusLabel(v.status)}
						</Badge>
					</td>
					<td>
						<ScreeningBadge
							screening={v.screening_status}
							risk={v.risk_level}
							blocked={v.payments_blocked}
						/>
					</td>
					<td>
						<span class="source-badge">{sourceLabel(v.source)}</span>
					</td>
					<td class="mono">{v.invoice_count}</td>
					<td>
						{#if v.erp_vendor_id}
							<span
								class="erp-linked"
								role="img"
								aria-label={`Linked to ERP: ${v.erp_vendor_id}`}
								title="Linked to ERP: {v.erp_vendor_id}"
							>
								<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
							</span>
						{:else}
							<span class="muted">—</span>
						{/if}
					</td>
					<!-- Verify / reject / bank-change all sit behind
					     `require_permission(vendor.manage)` on the backend, which a
					     CFO does not hold (`ROLE_DEFAULT_PERMISSIONS`) — yet a CFO
					     reaches this page for the read. Ungated, they filled in the
					     whole bank-details dialog before the save 403'd, on the
					     BEC-sensitive path. `canManageVendors` is the same granular
					     permission the backend checks, so a custom role that grants
					     it works too. -->
					<td class="actions">
						{#if canManageVendors}
							{#if v.status === 'unverified'}
								<RowAction variant="success" onclick={() => verifyVendor(v.id)}>{m('vendors.row.verify')}</RowAction>
								<RowAction variant="danger" onclick={() => rejectVendor(v.id)}>{m('vendors.row.reject')}</RowAction>
							{/if}
							<RowAction onclick={() => openBankEditor(v)}>
								{v.bank_details?.counterparty_id ? m('vendors.row.bankSet') : m('vendors.row.bank')}
							</RowAction>
						{/if}
						{#if auth.isManager}
							<RowAction onclick={() => (inviteVendor = v)}>{m('vendors.row.invite')}</RowAction>
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>

	{#if hasMore}
		<div class="load-more-row">
			<button class="btn-load-more" onclick={loadMoreVendors} disabled={loadingMore}>
				{loadingMore ? m('common.loading') : m('vendors.loadMore', { shown: vendors.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('vendors.showingAll', { total })}</span>
		</div>
	{/if}
	{/if}

	{#if canManageVendors}
		<BulkBar count={selected.size} onclear={clearSelection}>
			{#snippet actions()}
				{#if allSelected && !selectedAllMatching && total > vendors.length}
					<button class="bulk-action-btn secondary" disabled={selectingAllMatching} onclick={selectAllMatching}>
						{selectingAllMatching
							? m('common.loading')
							: m('common.selectAllMatching', { total })}
					</button>
				{:else if selectedAllMatching}
					<span class="bulk-all-matching-note">{m('common.allMatchingSelected')}</span>
				{/if}
				<RowAction variant="success" disabled={bulkBusy} onclick={bulkVerify}>{m('vendors.row.verify')}</RowAction>
				<RowAction variant="danger" disabled={bulkBusy} onclick={bulkReject}>{m('vendors.row.reject')}</RowAction>
				<div class="bulk-divider"></div>
				<RowAction disabled={bulkBusy} onclick={bulkScreen}>{m('vendors.bulk.screen')}</RowAction>
				<RowAction disabled={bulkBusy} onclick={bulkExport}>{m('vendors.bulk.exportCsv')}</RowAction>
			{/snippet}
		</BulkBar>
	{/if}
</PageHeader>

{#if detailVendor}
	<VendorModal
		vendor={detailVendor}
		onclose={() => (detailVendor = null)}
		onupdated={applyVendorUpdate}
	/>
{/if}

{#if showConsolidation}
	<VendorConsolidationModal
		onclose={() => (showConsolidation = false)}
		onmerged={() => fetchVendors()}
	/>
{/if}

{#if showCreateVendor}
	<CreateVendorModal
		onclose={() => (showCreateVendor = false)}
		onsaved={() => fetchVendors()}
	/>
{/if}

{#if inviteVendor}
	<InviteVendorPortalUserModal
		vendorId={inviteVendor.id}
		vendorName={inviteVendor.name}
		onclose={() => (inviteVendor = null)}
		oninvited={(result) => (inviteResult = result)}
	/>
{/if}

{#if inviteResult}
	<Modal
		ariaLabel={m('vendors.invite.sent.aria')}
		title={m('vendors.invite.sent.heading')}
		width="md"
		onclose={() => (inviteResult = null)}
	>
		<p class="invite-sent-body" data-testid="vendor-invite-sent">
			{m('vendors.invite.sent.body', { email: inviteResult.user.email })}
		</p>
		<dl class="invite-sent-meta">
			<div>
				<dt>{m('vendors.invite.sent.email')}</dt>
				<dd>{inviteResult.user.email}</dd>
			</div>
			{#if inviteResult.portal_url}
				<div>
					<dt>{m('vendors.invite.sent.url')}</dt>
					<dd class="mono">{inviteResult.portal_url}</dd>
				</div>
			{/if}
		</dl>
		<div class="modal-footer">
			<button type="button" class="btn-primary" onclick={() => (inviteResult = null)}>
				{m('vendors.invite.sent.done')}
			</button>
		</div>
	</Modal>
{/if}

{#if showImportCsv}
	<ImportCsvModal
		title={m('vendors.action.importCsv')}
		ariaLabel={m('vendors.action.importCsv')}
		onimport={(file: File): Promise<ImportResult> => importVendorsCsv(file)}
		onclose={() => (showImportCsv = false)}
		onimported={() => fetchVendors()}
	>
		{#snippet columnsHint()}
			<p>{m('csvImport.vendors.hint.intro')}</p>
			<ul>
				<li>{m('csvImport.vendors.hint.name')}</li>
				<li>{m('csvImport.vendors.hint.code')}</li>
				<li>{m('csvImport.vendors.hint.dedup')}</li>
			</ul>
			<p>{m('csvImport.vendors.hint.status')}</p>
		{/snippet}
	</ImportCsvModal>
{/if}

<Modal
	open={bankEditing !== null}
	ariaLabel="Vendor bank counterparty"
	onclose={() => (bankEditing = null)}
>
	{#if bankEditing}
		<h2>{m('vendors.bank.title', { vendor: bankEditing.name })}</h2>
		<p class="modal-hint">
			These values bridge to your payment processor (e.g. Modern Treasury). The
			<code>counterparty_id</code> is the processor's identifier; the last4s are stored
			here for display only — full account / routing numbers belong with the processor.
		</p>
		<p class="dual-control-hint">
			{m('vendors.bank.dualControlHint')}
			{#if auth.isManager}
				<a href="/vendors/change-requests">{m('vendors.bank.reviewQueueLink')}</a>
			{/if}
		</p>
		<form onsubmit={(e) => { e.preventDefault(); saveBankDetails(); }}>
			<label>
				<span>{m('vendors.bank.counterpartyId')}</span>
				<input type="text" maxlength="255" bind:value={bankForm.counterparty_id} />
			</label>
			<label>
				<span>{m('vendors.bank.bankName')}</span>
				<input type="text" maxlength="255" bind:value={bankForm.bank_name} />
			</label>
			<label>
				<span>{m('vendors.bank.destinationCountry')}</span>
				<input type="text" maxlength="2" bind:value={bankForm.country} />
			</label>
			<div class="form-row">
				<label>
					<span>{m('vendors.bank.accountLast4')}</span>
					<input type="text" maxlength="4" bind:value={bankForm.account_last4} />
				</label>
				<label>
					<span>{bankIsUK ? m('vendors.bank.sortCodeLast2') : m('vendors.bank.achRoutingLast4')}</span>
					<input
						type="text"
						maxlength={bankIsUK ? 2 : 4}
						bind:value={bankForm.routing_last4}
					/>
				</label>
			</div>
			<!-- Larger US banks publish a SEPARATE Fedwire ABA from their ACH one,
			     so a wire and an ACH to the same vendor are addressed differently.
			     UK vendors use a sort code and have neither, so this is hidden for
			     them. Leave it blank when the bank publishes one number: the
			     payment path falls back to the ACH routing number for wires. -->
			{#if !bankIsUK}
				<label>
					<span>{m('vendors.bank.wireRoutingLast4')}</span>
					<input type="text" maxlength="4" bind:value={bankForm.wire_routing_last4} />
				</label>
				<p class="modal-hint">{m('vendors.bank.wireRoutingHint')}</p>
			{/if}
			<h3>{m('vendors.bank.mailingAddressSection')}</h3>
			<p class="modal-hint">{m('vendors.bank.mailingHint')}</p>
			<label>
				<span>{m('vendors.bank.mailingStreet')}</span>
				<input
					type="text"
					maxlength="255"
					bind:value={bankForm.mailing_address!.street}
				/>
			</label>
			<div class="form-row">
				<label>
					<span>{m('vendors.bank.mailingCity')}</span>
					<input type="text" maxlength="100" bind:value={bankForm.mailing_address!.city} />
				</label>
				<label>
					<span>{m('vendors.bank.mailingState')}</span>
					<input type="text" maxlength="100" bind:value={bankForm.mailing_address!.state} />
				</label>
			</div>
			<div class="form-row">
				<label>
					<span>{m('vendors.bank.mailingPostal')}</span>
					<input
						type="text"
						maxlength="20"
						bind:value={bankForm.mailing_address!.postal}
					/>
				</label>
				<label>
					<span>{m('vendors.bank.mailingCountry')}</span>
					<input
						type="text"
						maxlength="2"
						bind:value={bankForm.mailing_address!.country}
					/>
				</label>
			</div>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (bankEditing = null)}>
					{m('common.cancel')}
				</button>
				<button type="submit" class="btn-primary" disabled={savingBank}>
					{savingBank ? m('common.saving') : m('common.save')}
				</button>
			</div>
		</form>
	{/if}
</Modal>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */
	/* Shared by the Merge-duplicates <button> and the change-approvals <a>, so
	   the two toolbar controls read as one row. */
	.btn-outline {
		display: inline-flex;
		align-items: center;
		padding: 8px 18px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
		text-decoration: none;
	}

	/* The dual-control explainer in the bank-details dialog: saving here stages
	   a request, it does not change where money goes. */
	.dual-control-hint {
		margin: -4px 0 14px;
		font-size: 0.8rem;
		color: var(--text-muted);
	}
	.dual-control-hint a {
		color: var(--accent);
	}
	.btn-outline:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.btn-outline:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Row emphasis + bespoke cell badges */
	tr:last-child td {
		border-bottom: none;
	}
	.unverified {
		background: rgba(212, 148, 10, 0.04);
	}
	/* De-emphasize rejected rows with a subtle tint rather than a blanket
	   opacity (which would composite every cell's text below the WCAG
	   1.4.3 4.5:1 contrast floor). The red "Rejected" status badge carries
	   the state signal. */
	.rejected td {
		background: rgba(224, 64, 64, 0.04);
	}
	.vendor-name {
		font-weight: 500;
	}
	.source-badge {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 10px;
		font-size: 0.72rem;
		font-weight: 500;
		background: var(--bg);
		color: var(--text-muted);
	}
	.erp-linked {
		color: #1fa86a;
		display: inline-flex;
	}

	/* Bank-counterparty modal extras */
	.modal-hint code {
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.78rem;
		color: var(--text);
	}
	.modal input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}
	.form-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}
	.modal h3 {
		margin: 4px 0 0;
		font-size: 0.95rem;
		font-weight: 600;
	}

	/* Bulk-bar "select all N matching" affordance — mirrors /expenses. */
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
	.invite-sent-body {
		margin: 0 0 1rem;
		font-size: 0.9rem;
		color: var(--text-muted);
	}
	.invite-sent-meta {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 0.75rem;
		margin: 0 0 0.5rem;
	}
	.invite-sent-meta dt {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
	.invite-sent-meta dd {
		margin: 0.15rem 0 0;
		font-weight: 600;
		word-break: break-all;
	}
	.invite-sent-meta dd.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
	}
</style>
