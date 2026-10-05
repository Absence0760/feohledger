<script lang="ts">
	import type { Reconciliation, ReconStatus } from '#lib/types/vendorStatementRecon.ts';
	import {
		RECON_STATUSES,
		reconStatusLabelKey,
		RECON_STATUS_TONES
	} from '#lib/types/vendorStatementRecon.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import { orgCurrency } from '#lib/stores/orgSettings.svelte.ts';
	import {
		listReconciliations,
		getReconciliationSummary,
		getReconciliation,
		getCloseReadiness,
		deleteReconciliation
	} from '#lib/api/vendorStatementRecon.ts';
	import type { ReconciliationSummary } from '#lib/types/vendorStatementRecon.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import SearchBox from '#lib/components/ui/SearchBox.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import VendorStatementReconModal from '#lib/components/modals/VendorStatementReconModal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { page } from '$app/state';
	import { replaceState } from '$app/navigation';
	import { untrack } from 'svelte';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';

	const canCreate = $derived(auth.isManager);

	// The status name is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = (s: string) => {
		const key = reconStatusLabelKey(s);
		return key ? m(key) : s;
	};

	const STATUS_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		...RECON_STATUSES.map((s) => ({ key: s, label: statusLabel(s) }))
	]);

	const COLUMNS = $derived([
		{ label: m('vendorStatements.col.vendor') },
		{ label: m('vendorStatements.col.statementDate') },
		{ label: m('vendorStatements.col.reference') },
		{ label: m('vendorStatements.col.discrepancies'), class: 'right' },
		{ label: m('vendorStatements.col.statementTotal'), class: 'right' },
		{ label: m('vendorStatements.col.ledgerTotal'), class: 'right' },
		{ label: m('vendorStatements.col.status') },
		{ class: 'actions-col' }
	]);

	const PAGE_SIZE = 20;

	// URL-backed filter state (mirrors the recurring/contracts page convention).
	let search = $state(page.url.searchParams.get('search') ?? '');
	let statusFilter = $state<string>(page.url.searchParams.get('status') ?? 'all');

	let recons = $state<Reconciliation[]>([]);
	let total = $state(0);
	let pageNum = $state(1);
	let loading = $state(true);
	let loadingMore = $state(false);

	let closeReady = $state<boolean | null>(null);
	let blockingCount = $state(0);

	let hasMore = $derived(recons.length < total);

	// Modal state: showCreate = create; a Reconciliation = detail/diff.
	let showCreate = $state(false);
	let detail = $state<Reconciliation | null>(null);


	// `search` is a SERVER filter — `GET /api/vendor-statements` ILIKEs supplier
	// + statement reference, the two free-text columns this table renders, and
	// `/summary` shares the same backend filter builder. It used to narrow the
	// LOADED rows in the browser, so a statement matching on page 2 read as
	// "nothing matched" and the footer's "Showing all N" (the server's whole-set
	// total) sat above a client-narrowed table.
	//
	// `untrack` on the read: buildParams() is called synchronously from load(),
	// which the status `$effect` calls — and Svelte tracks reads transitively —
	// so a plain read would make that effect depend on `search` and fire an
	// immediate, un-debounced request per keystroke (issue #168).
	function buildParams() {
		const params: { status?: string; search?: string } = {};
		if (statusFilter !== 'all') params.status = statusFilter;
		const term = untrack(() => search).trim();
		if (term) params.search = term;
		return params;
	}

	// Reflect the live filter state into the URL. EVERY read in here is
	// untracked, `page.url` included, because syncUrl() is a WRITER called
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
			const url = new URL(page.url.href);
			if (statusFilter !== 'all') url.searchParams.set('status', statusFilter);
			else url.searchParams.delete('status');
			if (search.trim()) url.searchParams.set('search', search.trim());
			else url.searchParams.delete('search');
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	// Sequences `load` (filter change and load-more alike — one shared counter,
	// latest-issued wins). `upsert` / `deleteRecon` edit the list in place with
	// no fetch of their own, so they retire whatever is in flight first: a run
	// created from an uploaded statement needs no pre-existing row, so it races
	// even the first load. See `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	// The term the newest issued list request carried — see the debounce effect.
	// Seeded from the URL the same way `search` is (rather than from `search`
	// itself, which reads as capturing a reactive value at init), so a
	// bookmarked `?search=` doesn't fire a second load behind the first.
	let appliedSearch = $state((page.url.searchParams.get('search') ?? '').trim());

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		// Record the term this request carries, so the debounce below can tell a
		// term already on screen from one that still needs a fetch — which is
		// what stops its first run (mount, including a bookmarked ?search=)
		// firing a duplicate load behind the status effect's.
		if (!opts.append) appliedSearch = untrack(() => search).trim();
		// KPI rollup tracks the same filter state — refresh it on a fresh load.
		if (!opts.append) void loadSummary();
		if (opts.append) loadingMore = true;
		else loading = true;
		try {
			const data = await listReconciliations({
				...buildParams(),
				page: nextPage,
				page_size: PAGE_SIZE
			});
			// Superseded by a newer load, or by a local create/resolve/delete.
			if (!fetchSequence.canCommit(token)) return;
			recons = opts.append ? appendUnique(recons, data.items) : data.items;
			total = data.total;
			pageNum = nextPage;
		} catch (e) {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// edit still failed, and no newer load is coming to report it.
			if (!fetchSequence.isCurrentRequest(token)) return;
			if (!opts.append) recons = [];
			toast(e instanceof Error ? e.message : m('vendorStatements.toast.loadFailed'), 'error');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	let searchTimer: ReturnType<typeof setTimeout>;
	$effect(() => {
		const term = search.trim();
		clearTimeout(searchTimer);
		if (term === appliedSearch) return;
		searchTimer = setTimeout(() => {
			syncUrl();
			void load();
		}, 300);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone, running syncUrl()/a list fetch against a route
		// the user already left.
		return () => clearTimeout(searchTimer);
	});

	$effect(() => {
		statusFilter;
		syncUrl();
		load();
	});

	$effect(() => {
		orgCurrency.ensureLoaded();
		loadCloseReadiness();
	});

	// No vendor fetch here any more. These options ARE the set of valid choices,
	// so a truncated list is not a shorter list — it is a supplier the operator
	// cannot pick. Walking every page on mount was the previous answer, and it
	// cost one request per 100 suppliers on a page that may never open the
	// modal. `ui/VendorPicker` inside `VendorStatementReconModal` searches them
	// server-side instead, so a vendor on page 40 is one query away and nothing
	// is fetched until the form is opened.

	async function loadCloseReadiness() {
		try {
			const data = await getCloseReadiness();
			closeReady = data.is_close_ready;
			blockingCount = data.blocking_vendors.length;
		} catch {
			closeReady = null;
			blockingCount = 0;
		}
	}

	// Deep-link: `/vendor-statements?id=<uuid>` opens that run's detail modal.
	let deepLinkLoaded = $state<string | null>(null);
	$effect(() => {
		const id = page.url.searchParams.get('id');
		if (!id || deepLinkLoaded === id) return;
		deepLinkLoaded = id;
		getReconciliation(id)
			.then((r) => (detail = r))
			.catch(() => toast(m('vendorStatements.toast.notFound'), 'error'));
	});

	async function openDetail(r: Reconciliation) {
		try {
			detail = await getReconciliation(r.id);
		} catch {
			detail = r;
		}
	}

	function closeModal() {
		detail = null;
		showCreate = false;
		const url = new URL(page.url.href);
		if (url.searchParams.has('id')) {
			url.searchParams.delete('id');
			replaceState(`${url.pathname}${url.search}`, {});
			deepLinkLoaded = null;
		}
	}

	function upsert(r: Reconciliation) {
		fetchSequence.supersedeInFlight();
		const idx = recons.findIndex((x) => x.id === r.id);
		if (idx === -1) {
			recons = [r, ...recons];
			total += 1;
		} else {
			recons = recons.map((x) => (x.id === r.id ? r : x));
		}
		loadCloseReadiness();
		void loadSummary();
	}

	function onSaved(r: Reconciliation) {
		upsert(r);
		if (detail && detail.id === r.id) detail = r;
	}

	// --- Delete (armed two-click confirm) ---
	let busyId = $state<string | null>(null);
	let confirmDeleteId = $state<string | null>(null);

	async function deleteRecon(r: Reconciliation) {
		if (confirmDeleteId !== r.id) {
			confirmDeleteId = r.id;
			return;
		}
		confirmDeleteId = null;
		busyId = r.id;
		try {
			await deleteReconciliation(r.id);
			fetchSequence.supersedeInFlight();
			recons = recons.filter((x) => x.id !== r.id);
			total = Math.max(0, total - 1);
			toast(m('vendorStatements.toast.deleted'), 'success');
			loadCloseReadiness();
			void loadSummary();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('vendorStatements.toast.deleteFailed'), 'error');
		} finally {
			busyId = null;
		}
	}

	function discrepancyCount(r: Reconciliation): number {
		return (
			r.summary.amount_mismatch_count +
			r.summary.missing_our_side_count +
			r.summary.missing_their_side_count
		);
	}


	// --- KPI rollup: `GET /api/vendor-statements/summary` — the WHOLE filtered
	// set, over the SAME vendor/status filters. `openCount` filtered the loaded
	// page and `totalDiscrepancies` reduced the per-run discrepancy counts over
	// it — so both contradicted the "showing all N" footer. `discrepancyCount`
	// stays for the per-row cell. ---
	let reconSummary = $state<ReconciliationSummary | null>(null);
	const summarySequence = createRequestSequencer();

	async function loadSummary() {
		const token = summarySequence.start();
		try {
			const res = await getReconciliationSummary(buildParams());
			if (!summarySequence.canCommit(token)) return;
			reconSummary = res;
		} catch {
			if (summarySequence.isCurrentRequest(token)) reconSummary = null;
		}
	}

	const openCount = $derived(reconSummary?.by_status.open ?? 0);
	const totalDiscrepancies = $derived(reconSummary?.open_discrepancies ?? 0);
</script>

<svelte:window
	onclick={(e) => {
		if (confirmDeleteId && !(e.target as HTMLElement)?.closest?.('.row-action')) {
			confirmDeleteId = null;
		}
	}}
/>

<PageHeader title={m('vendorStatements.title')}>
	{#snippet actions()}
		{#if canCreate}
			<button class="btn-primary" onclick={() => (showCreate = true)}>{m('vendorStatements.new')}</button>
		{/if}
	{/snippet}

	<!-- KPI row -->
	<div class="kpi-row">
		<KpiCard value={openCount} label={m('vendorStatements.kpi.openRecons')} />
		<KpiCard value={totalDiscrepancies} label={m('vendorStatements.kpi.openDiscrepancies')} highlight={totalDiscrepancies > 0 ? 'red' : null} />
		<KpiCard
			value={closeReady === null ? '—' : closeReady ? m('vendorStatements.kpi.ready') : m('vendorStatements.kpi.blocking', { n: blockingCount })}
			label={m('vendorStatements.kpi.closeReadiness')}
			highlight={closeReady === false ? 'red' : closeReady === true ? 'green' : null}
		/>
	</div>

	<div class="filter-row">
		<SearchBox bind:value={search} placeholder={m('vendorStatements.searchPlaceholder')} ariaLabel={m('vendorStatements.searchAria')} />
		<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} />
	</div>

	<DataTable
		columns={COLUMNS}
		isEmpty={!loading && recons.length === 0}
		empty={loading ? m('common.loading') : m('vendorStatements.empty')}
		colspan={8}
	>
		{#snippet body()}
			{#each recons as recon (recon.id)}
				<tr
					class="clickable"
					onclick={(e) => {
						if (isRowOpenClick(e)) openDetail(recon);
					}}
				>
					<td>
						<RowLink
							onclick={() => openDetail(recon)}
							ariaLabel={m('vendorStatements.row.openAria', { vendor: recon.vendor_name ?? m('vendorStatements.vendorFallback'), date: recon.statement_date })}
						>
							{recon.vendor_name ?? '—'}
						</RowLink>
					</td>
					<td class="muted">{formatDate(recon.statement_date)}</td>
					<td class="muted">{recon.statement_reference ?? '—'}</td>
					<td class="right mono">
						{#if discrepancyCount(recon) > 0}
							<Badge tone="danger" variant="disc-badge">{discrepancyCount(recon)}</Badge>
						{:else}
							<span class="disc-clean">0</span>
						{/if}
					</td>
					<td class="right mono"><Money amount={recon.summary.statement_total} currency={recon.currency} /></td>
					<td class="right mono"><Money amount={recon.summary.ledger_total} currency={recon.currency} /></td>
					<td>
						<Badge tone={RECON_STATUS_TONES[recon.status]} variant={recon.status}>
							{statusLabel(recon.status)}
						</Badge>
					</td>
					<td class="actions">
						{#if canCreate}
							<RowAction
								variant="danger"
								armed={confirmDeleteId === recon.id}
								disabled={busyId === recon.id}
								onclick={() => deleteRecon(recon)}
								ariaLabel={m('vendorStatements.row.deleteAria', { vendor: recon.vendor_name ?? m('vendorStatements.vendorFallback'), date: recon.statement_date })}
							>
								{confirmDeleteId === recon.id ? m('vendorStatements.row.confirm') : m('vendorStatements.row.delete')}
							</RowAction>
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>

	{#if hasMore}
		<div class="load-more-row">
			<button class="btn-load-more" onclick={() => load({ append: true })} disabled={loadingMore}>
				{loadingMore ? m('common.loading') : m('vendorStatements.loadMore', { shown: recons.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('vendorStatements.showingAll', { total })}</span>
		</div>
	{/if}
</PageHeader>

{#if showCreate}
	<VendorStatementReconModal recon={null} onclose={closeModal} onsaved={onSaved} />
{/if}

{#if detail}
	<VendorStatementReconModal recon={detail} onclose={closeModal} onsaved={onSaved} />
{/if}

<style>
	.filter-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	.mono {
		font-variant-numeric: tabular-nums;
		font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
	}
	.muted {
		color: var(--text-muted);
	}

	/* The discrepancy COUNT is `<Badge tone="danger">` — it sits in its own
	   right-aligned numeric column, not beside a name, so the primitive's
	   metrics cost the row nothing and the tint can no longer drift from the
	   modal's. `.disc-clean` is the zero case: deliberately NOT a badge, because
	   "nothing to reconcile" is the absence of a signal, and a tinted pill
	   reading `0` would advertise itself exactly as loudly as a real
	   discrepancy. */
	.disc-clean {
		color: var(--text-muted);
	}

	/* The status pill is `<Badge>` now — this file and `VendorStatementReconModal`
	   used to tint the same two statuses at two different alphas. The tone per
	   status lives beside the labels in `types/vendorStatementRecon`. */
</style>
