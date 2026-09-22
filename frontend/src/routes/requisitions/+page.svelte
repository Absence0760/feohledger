<script lang="ts">
	import type { Requisition, RequisitionStatus } from '$lib/types/requisition';
	import {
		REQUISITION_FILTER_STATUSES,
		REQUISITION_STATUS_LABEL_KEYS,
		requisitionStatusLabelKey,
		REQUISITION_STATUS_TONES
	} from '$lib/types/requisition';
	import { auth } from '$lib/stores/auth.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import {
		listRequisitions,
		getRequisitionSummary,
		getRequisition,
		deleteRequisition as apiDelete,
		submitRequisition,
		approveRequisition,
		rejectRequisition,
		cancelRequisition,
		reopenRequisition,
		convertRequisitionToPo,
		type RequisitionListParams
	} from '$lib/api/requisitions';
	import type { RequisitionSummary } from '$lib/types/requisition';
	import { formatCurrencyTotals } from '$lib/utils/currencyGroups';
	import { listGlAccounts, type GlAccountOption } from '$lib/api/expenses';
	import Badge from '$lib/components/ui/Badge.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import { formatMoney } from '$lib/utils/money';
	import RequisitionModal from '$lib/components/modals/RequisitionModal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { m } from '$lib/i18n/store.svelte';
	import { page } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import { onMount, untrack } from 'svelte';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { appendUnique } from '$lib/utils/pagination';
	import { formatDate } from '$lib/utils/time';

	const canCreate = $derived(auth.hasAnyRole('admin', 'ap_manager', 'ap_clerk'));
	// approve / reject = admin | ap_manager | CFO. One predicate used to cover
	// approve, reject AND convert, which silently dropped the CFO: `/requisitions`
	// approve and reject are `require_roles(ADMIN, AP_MANAGER, CFO)`, so a CFO
	// could open the queue and had no decision control on it — exactly the split
	// `/expenses` already had to make between its report approve (allows cfo) and
	// reject (does not).
	const canDecideRole = $derived(auth.isManager || auth.isCfo);
	// convert-to-PO stays admin | ap_manager — it is the money step, and
	// `requisitions.py::convert_to_po` gates it that way.
	const canConvert = $derived(auth.isManager);
	// `POST /api/requisitions/{id}/reopen` is gated
	// `require_roles(ADMIN, AP_MANAGER, AP_CLERK)` — the CREATE set, not the
	// decide set. Reopening is the buyer redoing their own ask, so it reuses
	// `canCreate`; a CFO can reject but cannot reopen, and `canDecideRole` would
	// have offered them a button that can only 403.

	const PAGE_SIZE = 100;

	let requisitions = $state<Requisition[]>([]);
	let total = $state(0);
	let loading = $state(false);
	let pageNum = $state(1);
	let loadingMore = $state(false);

	// The list is paged: `total` is the whole filtered set, so a footer that
	// says "Showing all {total}" is only true once every row is loaded.
	let hasMore = $derived(requisitions.length < total);

	let search = $state($page.url.searchParams.get('search') ?? '');
	// The search term the newest issued list request carried. Written by
	// `load()`, read by the debounce effect — see the comment there.
	let appliedSearch = $state(($page.url.searchParams.get('search') ?? '').trim());
	let statusFilter = $state<string>($page.url.searchParams.get('status') ?? 'all');

	let showCreate = $state(false);
	let editing = $state<Requisition | null>(null);
	let confirmDeleteId = $state<string | null>(null);
	let reopenArmedId = $state<string | null>(null);
	let busyId = $state<string | null>(null);
	let glAccounts = $state<GlAccountOption[]>([]);

	// Chip statuses = the reachable subset ∪ whatever is ACTIVE. The subset omits
	// `submitted` because nothing in the backend ever assigns it (see
	// REQUISITION_FILTER_STATUSES for the enumerated writers) — the chip was a
	// filter that returned an empty list forever. The union with the active
	// status is the same `quick subset ∪ active` rule /invoices uses
	// (frontend/CLAUDE.md § Status filter chips): a bookmarked
	// `?status=submitted` still renders its chip, so an active filter is never
	// invisible and the user can always click back to All.
	const chipStatuses = $derived.by(() => {
		const active = statusFilter as RequisitionStatus;
		if (REQUISITION_FILTER_STATUSES.includes(active) || !(active in REQUISITION_STATUS_LABEL_KEYS))
			return REQUISITION_FILTER_STATUSES;
		return [...REQUISITION_FILTER_STATUSES, active];
	});

	// The status name is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = (s: string) => {
		const key = requisitionStatusLabelKey(s);
		return key ? m(key) : s;
	};

	const STATUS_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		...chipStatuses.map((s) => ({ key: s, label: statusLabel(s) }))
	]);

	const COLUMNS = $derived([
		{ label: m('requisitions.col.requisitionNumber') },
		{ label: m('requisitions.col.title') },
		{ label: m('requisitions.col.department') },
		{ label: m('requisitions.col.neededBy') },
		{ label: m('requisitions.col.total'), class: 'right' },
		{ label: m('requisitions.col.status') },
		{ label: '', class: 'actions-col' }
	]);

	// Search is a SERVER filter: the term rides `?search=` on
	// `GET /api/requisitions`, which ILIKEs `requisition_number` + `title` +
	// `department` — the three columns this table renders. It used to run
	// client-side over the loaded rows only, which meant a term matching a
	// requisition on page 2 read as "nothing matched" until the user paged to
	// it; the empty state had to say so. Now the server searches the whole
	// filtered set, so the plain empty message is true again and `total` /
	// Load more describe the MATCHES rather than the unfiltered list.
	const emptyMessage = $derived(loading ? m('common.loading') : m('requisitions.empty'));

	// KPIs from `GET /api/requisitions/summary` — the WHOLE filtered set, over
	// the SAME status/search filters. They used to reduce over `requisitions`,
	// which holds one page: `pendingCount` counted pending rows on that page
	// while the "Requisitions" card beside it showed the server's whole-set
	// `total`, and `periodTotal` summed one page AND added values across
	// currencies into the org default. The rollup groups by currency and never
	// adds across them (`$lib/utils/currencyGroups`).
	let reqSummary = $state<RequisitionSummary | null>(null);
	const summarySequence = createRequestSequencer();

	async function loadSummary() {
		const token = summarySequence.start();
		try {
			const params: Pick<RequisitionListParams, 'status' | 'search'> = {};
			if (statusFilter !== 'all') params.status = statusFilter;
			const term = untrack(() => search).trim();
			if (term) params.search = term;
			const res = await getRequisitionSummary(params);
			if (!summarySequence.canCommit(token)) return;
			reqSummary = res;
		} catch {
			if (summarySequence.isCurrentRequest(token)) reqSummary = null;
		}
	}

	const pendingCount = $derived(reqSummary?.by_status.pending_approval ?? 0);
	// One subtotal per currency, never added; an unestablished currency renders
	// bare rather than joining the org's (decisions §200). The org's code labels
	// only the zero of a summary that landed empty — an unanswered one is no
	// figure, which `KpiCard` draws as its dash.
	const periodTotalGroups = $derived(formatCurrencyTotals(reqSummary?.by_currency ?? []));
	const periodTotal = $derived(
		reqSummary
			? (periodTotalGroups[0] ?? formatMoney(0, { currency: orgCurrency.currency }))
			: null
	);
	const periodTotalRest = $derived(
		periodTotalGroups.length > 1 ? periodTotalGroups.slice(1).join(' · ') : null
	);

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
			// `id` is a transient deep-link param (see deepLinkId below) — it is
			// consumed once at load and never persisted, so the filter-state sync
			// always drops it rather than resurrecting it from a stale URL read.
			url.searchParams.delete('id');
			if (statusFilter !== 'all') url.searchParams.set('status', statusFilter);
			else url.searchParams.delete('status');
			if (search.trim()) url.searchParams.set('search', search.trim());
			else url.searchParams.delete('search');
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	// Sequences `load` (latest-issued wins) so a slow response for an earlier
	// search term or status filter can't land after a faster later one — the
	// classic "acm resolves after acme" race, now reachable on this page
	// because the term is a server filter. `onSaved` / `replaceRow`
	// / `doDelete` edit the list in place with no fetch of their own, so they
	// retire whatever is in flight first — a new requisition needs no
	// pre-existing row, so it races even the first load. See
	// `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		// The KPI rollup tracks the same filter state — refresh it alongside a
		// fresh (non-append) list load. Its own sequencer keeps a slow response
		// from landing over a newer one.
		if (!opts.append) void loadSummary();
		if (opts.append) loadingMore = true;
		else loading = true;
		try {
			const params: RequisitionListParams = {
				page: nextPage,
				page_size: PAGE_SIZE
			};
			if (statusFilter !== 'all') params.status = statusFilter;
			// `untrack`: `load()` is ALSO called synchronously from the
			// statusFilter `$effect` above, and Svelte tracks reads transitively
			// through called functions — so a plain `search` read here would make
			// that effect depend on `search` too, firing an immediate
			// un-debounced load on every keystroke (issue #168, the very thing
			// `syncUrl`'s comment says was fixed on /invoices, /payments and
			// /vendors; `frontend/src/routes/vendors/+page.svelte` untracks the
			// same read for the same reason). Worse here than there: `load()`
			// stamps `appliedSearch` first, so the debounce timer would then
			// short-circuit and the keystroke fetch would be the ONLY one. The
			// value read is still the live one — untrack only stops the read
			// registering as a dependency.
			//
			// Read at issue time, and recorded, so the debounce below can tell a
			// term that is already on screen from one that still needs a fetch.
			const term = untrack(() => search).trim();
			if (term) params.search = term;
			appliedSearch = term;
			const res = await listRequisitions(params);
			// Superseded by a newer load, or by a local create/lifecycle edit.
			if (!fetchSequence.canCommit(token)) return;
			requisitions = opts.append ? appendUnique(requisitions, res.items) : res.items;
			total = res.total;
			pageNum = nextPage;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// edit still failed, and no newer load is coming to report it.
			if (!fetchSequence.isCurrentRequest(token)) return;
			toast(err instanceof Error ? err.message : m('requisitions.toast.loadFailed'), 'error');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	$effect(() => {
		statusFilter;
		syncUrl();
		load();
	});

	// A keystroke now costs a request, so the term is debounced 300ms (the
	// /invoices, /payments, /vendors convention) and the fetch sequencer above
	// discards a slow response for an earlier term. `appliedSearch` is the term
	// the newest ISSUED load used: re-running with a term that already matches
	// it schedules nothing, which is what keeps the effect's first run (mount,
	// including a bookmarked `?search=`) from firing a duplicate load 300ms
	// behind the status effect's — and cancels a pending debounce when a chip
	// click has already loaded with the typed term.
	let searchTimer: ReturnType<typeof setTimeout>;
	$effect(() => {
		const next = search.trim();
		clearTimeout(searchTimer);
		if (next === appliedSearch) return;
		searchTimer = setTimeout(() => {
			syncUrl();
			load();
		}, 300);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone, running syncUrl()/a list fetch against a route
		// the user already left.
		return () => clearTimeout(searchTimer);
	});

	$effect(() => {
		orgCurrency.ensureLoaded();
		loadGlAccounts();
	});

	// Deep-link: `?id=<uuid>` opens that requisition's detail modal (e.g. after a
	// punch-out cart is converted into a draft). Captured once at init — before
	// syncUrl() normalizes the URL and strips the transient param — then resolved
	// straight from the API (the target may live past the 100 rows we fetch).
	const deepLinkId = $page.url.searchParams.get('id');
	onMount(() => {
		if (!deepLinkId) return;
		getRequisition(deepLinkId)
			.then((r) => (editing = r))
			.catch(() => toast(m('requisitions.notFound'), 'error'));
	});

	async function loadGlAccounts() {
		try {
			glAccounts = await listGlAccounts();
		} catch {
			/* non-critical for the list view */
		}
	}

	function onSaved(r: Requisition) {
		fetchSequence.supersedeInFlight();
		const idx = requisitions.findIndex((x) => x.id === r.id);
		if (idx >= 0) requisitions = requisitions.map((x) => (x.id === r.id ? r : x));
		else {
			requisitions = [r, ...requisitions];
			total += 1;
		}
		if (editing && editing.id === r.id) editing = r;
		void loadSummary();
	}

	function replaceRow(r: Requisition) {
		fetchSequence.supersedeInFlight();
		requisitions = requisitions.map((x) => (x.id === r.id ? r : x));
		if (editing && editing.id === r.id) editing = r;
		// A lifecycle transition (submit / approve / reject / cancel) moved a
		// status — refresh the by-status KPI counts.
		void loadSummary();
	}

	async function runAction(
		id: string,
		fn: () => Promise<Requisition>,
		successMsg: string,
		failMsg: string
	) {
		busyId = id;
		try {
			replaceRow(await fn());
			toast(successMsg, 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : failMsg, 'error');
		} finally {
			busyId = null;
		}
	}

	function doSubmit(r: Requisition) {
		runAction(r.id, () => submitRequisition(r.id), m('requisitions.toast.submitted'), m('requisitions.toast.submitFailed'));
	}
	function doApprove(r: Requisition) {
		runAction(r.id, () => approveRequisition(r.id), m('requisitions.toast.approved'), m('requisitions.toast.approveFailed'));
	}
	function doReject(r: Requisition) {
		runAction(r.id, () => rejectRequisition(r.id), m('requisitions.toast.rejected'), m('requisitions.toast.rejectFailed'));
	}
	function doCancel(r: Requisition) {
		runAction(r.id, () => cancelRequisition(r.id), m('requisitions.toast.cancelled'), m('requisitions.toast.cancelFailed'));
	}

	// `rejected -> draft`. The armed two-click is this page's confirm pattern,
	// and the armed label names the DESTINATION (`Draft`) rather than a bare
	// "Confirm": a reopened requisition is back in `draft` — editable and NOT
	// resubmitted — while the sibling /intake reopen lands in `open`. Which side
	// of the review line the row is back on decides whether the user still owes
	// a Submit click.
	async function doReopen(r: Requisition) {
		busyId = r.id;
		try {
			replaceRow(await reopenRequisition(r.id));
			toast(m('requisitions.toast.reopened'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('requisitions.toast.reopenFailed'), 'error');
		} finally {
			busyId = null;
			reopenArmedId = null;
		}
	}

	async function doConvert(r: Requisition) {
		busyId = r.id;
		try {
			const res = await convertRequisitionToPo(r.id);
			toast(
				res.created
					? m('requisitions.toast.converted', { poNumber: res.po_number })
					: m('requisitions.toast.alreadyConverted', { poNumber: res.po_number }),
				'success'
			);
			await load();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('requisitions.toast.convertFailed'), 'error');
		} finally {
			busyId = null;
		}
	}

	async function doDelete(id: string) {
		try {
			await apiDelete(id);
			fetchSequence.supersedeInFlight();
			requisitions = requisitions.filter((r) => r.id !== id);
			total = Math.max(0, total - 1);
			void loadSummary();
			toast(m('requisitions.toast.deleted'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('requisitions.toast.deleteFailed'), 'error');
		} finally {
			confirmDeleteId = null;
		}
	}

	// SoD: a manager can approve/reject a pending requisition only if they aren't
	// the requester (the backend also enforces this — 403).
	function canDecide(r: Requisition): boolean {
		return (
			canDecideRole &&
			r.status === 'pending_approval' &&
			auth.user?.id !== r.requester_user_id
		);
	}

	// Outside-click un-arms any pending armed-confirm (delete, reopen).
	function onWindowClick(e: MouseEvent) {
		const target = e.target as Element | null;
		if (target?.closest('.row-action')) return;
		if (confirmDeleteId) confirmDeleteId = null;
		if (reopenArmedId) reopenArmedId = null;
	}
</script>

<svelte:window onclick={onWindowClick} />

<PageHeader title={m('requisitions.title')}>
	{#snippet actions()}
		{#if canCreate}
			<button class="btn-primary" onclick={() => (showCreate = true)}>{m('requisitions.action.new')}</button>
		{/if}
	{/snippet}

	<div class="kpi-row">
		<KpiCard value={periodTotal} sub={periodTotalRest} label={m('requisitions.kpi.openTotal')} />
		<KpiCard value={total} label={m('requisitions.kpi.requisitions')} />
		<KpiCard value={pendingCount} label={m('requisitions.kpi.pendingApproval')} highlight={pendingCount ? 'red' : null} />
	</div>

	<div class="filter-row">
		<SearchBox bind:value={search} placeholder={m('requisitions.search.placeholder')} ariaLabel={m('requisitions.search.aria')} />
		<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} />
	</div>

	<DataTable
		columns={COLUMNS}
		isEmpty={requisitions.length === 0}
		empty={emptyMessage}
	>
		{#snippet body()}
			{#each requisitions as r (r.id)}
				<tr class="clickable" onclick={(e) => { if (isRowOpenClick(e)) editing = r; }}>
					<td class="mono">
						<RowLink onclick={() => (editing = r)} ariaLabel={m('requisitions.row.open', { number: r.requisition_number })}>
							{r.requisition_number}
						</RowLink>
					</td>
					<td>{r.title ?? '—'}</td>
					<td>{r.department ?? '—'}</td>
					<td class="muted">{formatDate(r.needed_by)}</td>
					<td class="right mono"><Money amount={r.total} currency={r.currency} /></td>
					<td>
						<Badge tone={REQUISITION_STATUS_TONES[r.status as RequisitionStatus]} variant={r.status}>{statusLabel(r.status)}</Badge>
					</td>
					<td class="actions">
						{#if canCreate && r.status === 'draft'}
							<RowAction variant="default" onclick={() => doSubmit(r)} disabled={busyId === r.id}>{m('requisitions.row.submit')}</RowAction>
						{/if}
						{#if canDecide(r)}
							<RowAction variant="success" onclick={() => doApprove(r)} disabled={busyId === r.id}>{m('requisitions.row.approve')}</RowAction>
							<RowAction variant="danger" onclick={() => doReject(r)} disabled={busyId === r.id}>{m('requisitions.row.reject')}</RowAction>
						{/if}
						{#if canCreate && r.status === 'rejected'}
							<RowAction
								variant="default"
								armed={reopenArmedId === r.id}
								disabled={busyId === r.id}
								title={m('requisitions.row.reopenHint')}
								onclick={(e) => {
									e.stopPropagation();
									if (reopenArmedId === r.id) doReopen(r);
									else reopenArmedId = r.id;
								}}
							>
								{reopenArmedId === r.id
									? m('requisitions.row.reopenConfirm')
									: m('requisitions.row.reopen')}
							</RowAction>
						{/if}
						{#if canConvert && r.status === 'approved'}
							<RowAction variant="default" onclick={() => doConvert(r)} disabled={busyId === r.id}>{m('requisitions.row.convertToPo')}</RowAction>
						{/if}
						{#if canCreate && (r.status === 'draft' || r.status === 'submitted' || r.status === 'pending_approval' || r.status === 'approved')}
							<RowAction variant="default" onclick={() => doCancel(r)} disabled={busyId === r.id}>{m('requisitions.row.cancel')}</RowAction>
						{/if}
						{#if canCreate && r.status === 'draft'}
							<RowAction
								variant="danger"
								armed={confirmDeleteId === r.id}
								onclick={(e) => {
									e.stopPropagation();
									if (confirmDeleteId === r.id) doDelete(r.id);
									else confirmDeleteId = r.id;
								}}
							>
								{confirmDeleteId === r.id ? m('requisitions.row.confirm') : m('requisitions.row.delete')}
							</RowAction>
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>

	{#if hasMore}
		<div class="load-more-row">
			<button
				class="btn-load-more"
				onclick={() => load({ append: true })}
				disabled={loadingMore}
			>
				{loadingMore
					? m('common.loading')
					: m('requisitions.loadMore', { shown: requisitions.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('requisitions.showingAll', { total })}</span>
		</div>
	{/if}
</PageHeader>

{#if showCreate}
	<RequisitionModal
		requisition={null}
		{glAccounts}
		onclose={() => (showCreate = false)}
		onsaved={onSaved}
	/>
{/if}

{#if editing}
	<RequisitionModal
		requisition={editing}
		{glAccounts}
		onclose={() => (editing = null)}
		onsaved={onSaved}
	/>
{/if}

<style>
	.filter-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	/* No status-badge rules here on purpose. The one pill on this page is
	   `<Badge>`, and its tone comes from `REQUISITION_STATUS_TONES` beside the
	   label map in `$lib/types/requisition` — shared with `RequisitionModal`,
	   which badges the same union one click away and used to hold a second copy
	   at a different alpha. The status class survives as `variant`: a selector
	   hook, never colour (decisions.md §47). */
</style>
