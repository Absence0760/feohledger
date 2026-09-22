<script lang="ts">
	/**
	 * /goods-receipts — what arrived, and what quality said about it.
	 *
	 * Two tabs, because the page answers two questions that share a subject:
	 *
	 *   - **Receipts** — the deliveries themselves (the 3-way quantity leg).
	 *   - **Inspections** — the `QualityInspection` rows that drive the 4-way
	 *     quality leg of `services/po_matching`. This is the entry surface for
	 *     them: the invoice warnings panel has always RENDERED
	 *     `inspection_result`, `inspection_accepted_quantity` and the
	 *     `quality_hold` exception, so the app showed the consequences of an
	 *     inspection while offering no way to record one. It also shows what the
	 *     QMS sync brought in — a synced row often resolves to neither a receipt
	 *     nor a PO, so it exists nowhere else in the UI.
	 *
	 * An inspection is tied to a goods receipt, which is why it lives here and
	 * not on its own route.
	 *
	 * RBAC mirrors `api/inspections.py`: reading the list is open to any
	 * authenticated user, while recording one and running the QMS sync are
	 * admin / ap_manager (`auth.isManager`). A clerk sees every inspection and
	 * no button; `require_roles` refuses the write regardless.
	 */
	import { api } from '$lib/api';
	import { MAX_PAGE_SIZE, appendUnique } from '$lib/utils/pagination';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import type { BadgeTone } from '$lib/components/ui/badgeTone';
	import Tabs from '$lib/components/ui/Tabs.svelte';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import type { MessageKey } from '$lib/i18n/messages';
	import { formatDate } from '$lib/utils/time';
	import { goodsReceiptTone } from '$lib/types/goodsReceipt';
	import { auth } from '$lib/stores/auth.svelte';
	import type { Inspection } from '$lib/api/inspections';
	import { isInspectionResult, listInspections, syncInspections } from '$lib/api/inspections';
	import RecordInspectionModal from './RecordInspectionModal.svelte';
	import type { InspectableReceipt } from './RecordInspectionModal.svelte';
	import { page as pageStore } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import { untrack } from 'svelte';

	const canMutate = $derived(auth.isManager);

	const COLUMNS = $derived([
		{ label: m('goodsReceipts.col.grNumber') },
		{ label: m('goodsReceipts.col.po') },
		{ label: m('goodsReceipts.col.received') },
		{ label: m('goodsReceipts.col.status') },
		{ label: m('goodsReceipts.col.lines') },
		{ label: m('goodsReceipts.col.created') }
	]);

	const INSPECTION_COLUMNS = $derived([
		{ label: m('goodsReceipts.inspections.col.number') },
		{ label: m('goodsReceipts.inspections.col.result') },
		{ label: m('goodsReceipts.inspections.col.receipt') },
		{ label: m('goodsReceipts.inspections.col.inspected') },
		{ label: m('goodsReceipts.inspections.col.inspector') },
		{ label: m('goodsReceipts.inspections.col.quantities') },
		{ label: m('goodsReceipts.inspections.col.notes') }
	]);

	interface GRLine {
		id: string;
		description: string | null;
		quantity_received: number | null;
	}

	interface GRListItem {
		id: string;
		gr_number: string;
		po_id: string | null;
		po_number: string | null;
		received_date: string | null;
		status: string;
		line_count: number;
		created_at: string;
	}

	interface GRDetail extends GRListItem {
		line_items: GRLine[];
	}

	const PAGE_SIZE = 20;
	/**
	 * Page size of the receipts fetch that populates the record-inspection
	 * form's receipt PICKER — the only thing that still needs a list of
	 * receipts the page has not paged to.
	 *
	 * It used to also back the Inspections tab's "Goods receipt" column, because
	 * `GET /api/inspections` returned `gr_id` and no number: every inspection
	 * load dragged a 100-row receipts fetch along with it purely to build an
	 * id→number map, and any receipt outside that window still rendered
	 * unlabelled — a defect growing the page size could only move, not fix. The
	 * row now carries `gr_number` from a server-side join, so this fetch is
	 * loaded once for the picker and skipped entirely for a reader who cannot
	 * record an inspection.
	 */
	const RECEIPT_PICKER_SIZE = 100;

	type TabKey = 'receipts' | 'inspections';

	function initialTab(): TabKey {
		return $pageStore.url.searchParams.get('tab') === 'inspections' ? 'inspections' : 'receipts';
	}

	let tab = $state<TabKey>(initialTab());

	const TABS = $derived([
		{ key: 'receipts', label: m('goodsReceipts.tabs.receipts') },
		{ key: 'inspections', label: m('goodsReceipts.tabs.inspections') }
	]);

	let grs = $state<GRListItem[]>([]);
	let total = $state(0);
	let page = $state(1);
	let loading = $state(true);
	let loadingMore = $state(false);

	let detailId = $state<string | null>(null);
	let detail = $state<GRDetail | null>(null);
	let detailLoading = $state(false);

	let inspections = $state<Inspection[]>([]);
	let inspectionsLoaded = $state(false);
	let inspectionsLoading = $state(false);
	let loadingMoreInspections = $state(false);
	let inspectionsTotal = $state(0);
	let inspectionsPage = $state(1);
	/** Inspections for the receipt whose detail modal is open, fetched with
	 *  `?gr_id=` rather than filtered out of the tab's list — the tab now shows
	 *  a PAGE, so filtering it would silently miss this receipt's inspections
	 *  whenever they sit past the loaded rows. */
	let detailInspections = $state<Inspection[]>([]);
	let detailInspectionsLoading = $state(false);
	/** Receipts the record form may pick from; kept apart from `grs` so the
	 *  Load-more pagination of the Receipts tab is untouched. Loaded only for a
	 *  user who can actually record one. */
	let pickerReceipts = $state<GRListItem[]>([]);
	let syncing = $state(false);
	let recordOpen = $state(false);
	/** Set when the record form is opened from a receipt's detail — the subject
	 *  is already decided and the form renders it read-only. */
	let recordFor = $state<InspectableReceipt | null>(null);

	// Two INDEPENDENT request streams, so two sequencers — a shared counter
	// would let a detail open mark the list's in-flight response un-committable
	// and blank the table. Neither loader edits a row in place, so neither needs
	// `supersedeInFlight()`. The inspections loader gets its own for the same
	// reason. See `frontend/CLAUDE.md` § Sequencing list fetches.
	const listSequence = createRequestSequencer();
	const detailSequence = createRequestSequencer();
	const inspectionSequence = createRequestSequencer();
	const detailInspectionSequence = createRequestSequencer();

	$effect(() => {
		void loadGRs();
	});

	// Reflect the active tab in the URL so a reload / back-button lands on the
	// same one, and an inspection can be linked to directly.
	$effect(() => {
		const active = tab;
		untrack(() => {
			const url = new URL($pageStore.url);
			if (active === 'inspections') url.searchParams.set('tab', active);
			else url.searchParams.delete('tab');
			replaceState(`${url.pathname}${url.search}`, {});
		});
	});

	$effect(() => {
		if (tab !== 'inspections') return;
		untrack(() => {
			void ensureInspections();
			void ensurePickerReceipts();
		});
	});

	$effect(() => {
		if (!detailId) {
			detail = null;
			detailInspections = [];
			return;
		}
		// The modal asks the SERVER for this receipt's inspections (`?gr_id=`)
		// rather than filtering the tab's list: the tab holds one page now, so a
		// client-side filter would show an empty "no inspections" panel for a
		// receipt whose rows simply had not been paged to yet.
		const id = detailId;
		untrack(() => void loadDetailInspections(id));
		void loadDetail(id);
	});

	async function loadGRs(opts: { append?: boolean; nextPage?: number } = {}) {
		const token = listSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		try {
			const nextPage = opts.nextPage ?? 1;
			const params = new URLSearchParams({
				page: String(nextPage),
				page_size: String(PAGE_SIZE)
			});
			const data = await api.get<{ items: GRListItem[]; total: number }>(
				`/api/goods-receipts?${params}`
			);
			// Superseded by a newer load — discard rather than clobber. A page-2
			// append landing after a page-1 reload used to push the second page
			// onto the fresh list and overwrite `total`/`page` with it.
			if (!listSequence.canCommit(token)) return;
			grs = opts.append ? appendUnique(grs, data.items) : data.items;
			total = data.total;
			page = nextPage;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: only the newest request reports.
			if (!listSequence.isCurrentRequest(token)) return;
			toast(err instanceof Error ? err.message : m('goodsReceipts.toast.loadListFailed'), 'error');
		} finally {
			// Both flags belong to the newest request: an append used to clear
			// `loading` for a page-1 reload that was still out, dropping the
			// spinner while the table still held the previous page.
			if (listSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	async function loadMore() {
		await loadGRs({ append: true, nextPage: page + 1 });
	}

	async function loadDetail(id: string) {
		const token = detailSequence.start();
		detailLoading = true;
		try {
			const data = await api.get<GRDetail>(`/api/goods-receipts/${id}`);
			// Open one receipt, close it, open another: the first response must
			// not land in the modal now showing the second.
			if (!detailSequence.canCommit(token)) return;
			detail = data;
		} catch (err) {
			if (!detailSequence.isCurrentRequest(token)) return;
			toast(err instanceof Error ? err.message : m('goodsReceipts.toast.loadFailed'), 'error');
			detailId = null;
		} finally {
			if (detailSequence.isCurrentRequest(token)) detailLoading = false;
		}
	}

	/** Load the first page of inspections once per tab activation. */
	async function ensureInspections() {
		if (inspectionsLoaded || inspectionsLoading) return;
		await refreshInspections();
	}

	async function refreshInspections(opts: { append?: boolean; nextPage?: number } = {}) {
		const nextPage = opts.nextPage ?? 1;
		const token = inspectionSequence.start();
		if (opts.append) loadingMoreInspections = true;
		else inspectionsLoading = true;
		try {
			const data = await listInspections({ page: nextPage, pageSize: PAGE_SIZE });
			if (!inspectionSequence.canCommit(token)) return;
			inspections = opts.append ? appendUnique(inspections, data.items) : data.items;
			inspectionsTotal = data.total;
			inspectionsPage = nextPage;
			inspectionsLoaded = true;
		} catch (err) {
			if (!inspectionSequence.isCurrentRequest(token)) return;
			toast(
				err instanceof Error ? err.message : m('goodsReceipts.inspections.toast.loadFailed'),
				'error'
			);
		} finally {
			if (inspectionSequence.isCurrentRequest(token)) {
				inspectionsLoading = false;
				loadingMoreInspections = false;
			}
		}
	}

	async function loadMoreInspections() {
		await refreshInspections({ append: true, nextPage: inspectionsPage + 1 });
	}

	/** This receipt's inspections, asked for by `gr_id`. `MAX_PAGE_SIZE` is the
	 *  ceiling; a single delivery inspected more than 100 times is not a case
	 *  this panel needs to page, and the count is visible in the tab regardless. */
	async function loadDetailInspections(grId: string) {
		const token = detailInspectionSequence.start();
		detailInspectionsLoading = true;
		try {
			const data = await listInspections({ grId, pageSize: MAX_PAGE_SIZE });
			// Open one receipt, close it, open another: the first response must
			// not land in the modal now showing the second.
			if (!detailInspectionSequence.canCommit(token)) return;
			detailInspections = data.items;
		} catch (err) {
			if (!detailInspectionSequence.isCurrentRequest(token)) return;
			toast(
				err instanceof Error ? err.message : m('goodsReceipts.inspections.toast.loadFailed'),
				'error'
			);
		} finally {
			if (detailInspectionSequence.isCurrentRequest(token)) detailInspectionsLoading = false;
		}
	}

	/** The record form's receipt picker. Loaded once, and only for a user who
	 *  can record — a reader never pays for a list they cannot use. */
	async function ensurePickerReceipts() {
		if (!canMutate || pickerReceipts.length > 0) return;
		try {
			const data = await api.get<{ items: GRListItem[] }>(
				`/api/goods-receipts?page=1&page_size=${RECEIPT_PICKER_SIZE}`
			);
			pickerReceipts = data.items;
		} catch {
			// Non-fatal: the picker falls back to whatever receipts the page has
			// already loaded, and recording from a receipt's own detail modal
			// (which passes a fixed receipt) is unaffected.
		}
	}

	let hasMore = $derived(grs.length < total);
	let hasMoreInspections = $derived(inspections.length < inspectionsTotal);

	/** Every receipt the record form can offer, keyed by id — the paged Receipts
	 *  tab plus the picker window plus whichever detail is open. Labelling an
	 *  inspection's receipt no longer goes through here: the row carries
	 *  `gr_number` from the server. */
	const receiptIndex = $derived.by(() => {
		const index = new Map<string, GRListItem>();
		for (const gr of pickerReceipts) index.set(gr.id, gr);
		for (const gr of grs) index.set(gr.id, gr);
		if (detail) index.set(detail.id, detail);
		return index;
	});

	/** Receipts the record form may link an inspection to. Deduped and sorted
	 *  by receipt number so the picker is stable. */
	const recordableReceipts = $derived.by(() =>
		[...receiptIndex.values()]
			.map((gr) => ({
				id: gr.id,
				gr_number: gr.gr_number,
				po_id: gr.po_id,
				po_number: gr.po_number
			}))
			.sort((a, b) => a.gr_number.localeCompare(b.gr_number))
	);

	const RESULT_LABELS: Record<string, MessageKey> = {
		pass: 'goodsReceipts.inspections.result.pass',
		fail: 'goodsReceipts.inspections.result.fail',
		partial: 'goodsReceipts.inspections.result.partial'
	};

	function resultLabel(result: string): string {
		// An unrecognised value is shown verbatim rather than mapped to one of
		// the three — the column is free-form on the backend and guessing which
		// outcome an unknown string means is exactly the mistake
		// `qms_sync.normalize_disposition` refuses to make.
		return isInspectionResult(result) ? m(RESULT_LABELS[result]) : result;
	}

	function resultTone(result: string): BadgeTone {
		if (result === 'pass') return 'success';
		if (result === 'fail') return 'danger';
		if (result === 'partial') return 'warning';
		return 'neutral';
	}

	/** The accepted / rejected pair as one cell. `—` for a `pass` that recorded
	 *  neither, which is the normal shape of a clean inspection. */
	function quantitiesCell(ins: Inspection): string {
		const accepted = ins.accepted_quantity;
		const rejected = ins.rejected_quantity;
		if (accepted === null && rejected === null) return '—';
		return `${accepted ?? '—'} / ${rejected ?? '—'}`;
	}

	function openRecordFor(receipt: InspectableReceipt | null) {
		recordFor = receipt;
		recordOpen = true;
	}

	function onRecorded(created: Inspection) {
		recordOpen = false;
		recordFor = null;
		toast(
			m('goodsReceipts.inspections.toast.recorded', { number: created.inspection_number }),
			'success'
		);
		// Re-read rather than splicing the returned row in: the tab is a plain
		// list of what the tenant holds, and a re-read is what proves the write
		// landed where the matcher will look for it. Back to page 1 — the new row
		// is the newest, so it belongs at the top of the ordering the pages were
		// cut from.
		inspectionsLoaded = false;
		void refreshInspections();
		if (detailId) void loadDetailInspections(detailId);
	}

	async function runSync() {
		if (syncing) return;
		syncing = true;
		try {
			const res = await syncInspections();
			// Report what the sync actually DID. A pull that found nothing has to
			// say so — "0 created" from a provider that returned three records is
			// a different fact from a provider that returned none, and an
			// all-quiet success toast would read the same for both.
			if (res.fetched === 0) {
				toast(m('goodsReceipts.inspections.toast.syncNone'), 'info');
			} else if (res.created === 0 && res.updated === 0) {
				toast(
					m('goodsReceipts.inspections.toast.syncUpToDate', {
						fetched: res.fetched,
						skipped: res.skipped
					}),
					'info'
				);
			} else {
				toast(
					m('goodsReceipts.inspections.toast.synced', {
						fetched: res.fetched,
						created: res.created,
						updated: res.updated,
						unchanged: res.unchanged,
						skipped: res.skipped
					}),
					'success'
				);
			}
			inspectionsLoaded = false;
			await refreshInspections();
		} catch (err) {
			// The 409s ("no QMS configured", "provider has no adapter") carry the
			// backend's own explanation, and that explanation IS the outcome —
			// surface it verbatim rather than a generic failure.
			toast(
				err instanceof Error ? err.message : m('goodsReceipts.inspections.toast.syncFailed'),
				'error'
			);
		} finally {
			syncing = false;
		}
	}
</script>

<PageHeader title={m('goodsReceipts.title')}>
	{#snippet actions()}
		{#if tab === 'inspections' && canMutate}
			<button
				class="btn-outline"
				onclick={runSync}
				disabled={syncing}
				data-testid="sync-inspections"
			>
				{syncing
					? m('goodsReceipts.inspections.action.syncing')
					: m('goodsReceipts.inspections.action.sync')}
			</button>
			<button
				class="btn-primary"
				onclick={() => openRecordFor(null)}
				disabled={recordableReceipts.length === 0}
				title={recordableReceipts.length === 0
					? m('goodsReceipts.inspections.action.recordUnavailable')
					: undefined}
				data-testid="record-inspection"
			>
				{m('goodsReceipts.inspections.action.record')}
			</button>
		{/if}
	{/snippet}

	<Tabs tabs={TABS} bind:active={tab} ariaLabel={m('goodsReceipts.title')} idPrefix="goods-receipts" />

	{#if tab === 'receipts'}
		<div
			id="goods-receipts-panel-receipts"
			role="tabpanel"
			aria-labelledby="goods-receipts-tab-receipts"
		>
			<DataTable
				columns={COLUMNS}
				isEmpty={grs.length === 0}
				empty={loading ? m('common.loading') : m('goodsReceipts.empty')}
				colspan={6}
			>
				{#snippet body()}
					{#each grs as gr (gr.id)}
						<tr
							class="clickable"
							onclick={(e) => {
								if (isRowOpenClick(e)) detailId = gr.id;
							}}
						>
							<td class="mono">
								<RowLink
									onclick={() => (detailId = gr.id)}
									ariaLabel={m('goodsReceipts.row.view', { number: gr.gr_number })}
								>
									{gr.gr_number}
								</RowLink>
							</td>
							<td class="mono">{gr.po_number ?? '—'}</td>
							<td>{formatDate(gr.received_date)}</td>
							<td><Badge tone={goodsReceiptTone(gr.status)} variant={gr.status}>{gr.status}</Badge></td>
							<td class="muted">{gr.line_count}</td>
							<td class="muted">{formatDate(gr.created_at)}</td>
						</tr>
					{/each}
				{/snippet}
			</DataTable>

			{#if hasMore}
				<div class="load-more-row">
					<button class="btn-load-more" onclick={loadMore} disabled={loadingMore}>
						{loadingMore
							? m('common.loading')
							: m('goodsReceipts.loadMore', { shown: grs.length, total })}
					</button>
				</div>
			{:else if total > 0}
				<div class="load-more-row">
					<span class="load-more-end">{m('goodsReceipts.showingAll', { total })}</span>
				</div>
			{/if}
		</div>
	{:else}
		<div
			id="goods-receipts-panel-inspections"
			role="tabpanel"
			aria-labelledby="goods-receipts-tab-inspections"
		>
			<!-- Whether an inspection is REQUIRED before payment is resolved per
			     invoice by `services/matching_rules` (vendor rule → commodity/GL
			     rule → org default), so this panel must not imply it always is.
			     Recording one always feeds the 4-way leg either way. -->
			<p class="panel-hint muted" data-testid="inspections-hint">
				{m('goodsReceipts.inspections.hint')}
			</p>

			<DataTable
				columns={INSPECTION_COLUMNS}
				isEmpty={inspections.length === 0}
				empty={inspectionsLoading ? m('common.loading') : m('goodsReceipts.inspections.empty')}
				colspan={7}
			>
				{#snippet body()}
					{#each inspections as ins (ins.id)}
						<tr data-testid="inspection-row" data-inspection-number={ins.inspection_number}>
							<td class="mono">{ins.inspection_number}</td>
							<td>
								<Badge tone={resultTone(ins.result)} variant={ins.result}>
									{resultLabel(ins.result)}
								</Badge>
							</td>
							<td class="mono">
								{#if ins.gr_id}
									{@const grId = ins.gr_id}
									<!-- `gr_number` rides the row, resolved by the server's own
									     outer join — every linked receipt is named, including one
									     the page has not paged to. -->
									<RowLink
										onclick={() => (detailId = grId)}
										ariaLabel={ins.gr_number
											? m('goodsReceipts.row.view', { number: ins.gr_number })
											: m('goodsReceipts.inspections.viewReceipt')}
									>
										{ins.gr_number ?? m('goodsReceipts.inspections.viewReceipt')}
									</RowLink>
								{:else}
									<span class="muted" title={m('goodsReceipts.inspections.unlinkedHint')}>
										{m('goodsReceipts.inspections.unlinked')}
									</span>
								{/if}
							</td>
							<td>{formatDate(ins.inspected_date)}</td>
							<td class="muted">{ins.inspector ?? '—'}</td>
							<td class="mono">{quantitiesCell(ins)}</td>
							<td class="notes muted">{ins.deviation_notes ?? '—'}</td>
						</tr>
					{/each}
				{/snippet}
			</DataTable>

			{#if hasMoreInspections}
				<div class="load-more-row">
					<button
						class="btn-load-more"
						onclick={loadMoreInspections}
						disabled={loadingMoreInspections}
						data-testid="load-more-inspections"
					>
						{loadingMoreInspections
							? m('common.loading')
							: m('goodsReceipts.inspections.loadMore', {
									shown: inspections.length,
									total: inspectionsTotal
								})}
					</button>
				</div>
			{:else if inspectionsTotal > 0}
				<div class="load-more-row">
					<span class="load-more-end">
						{m('goodsReceipts.inspections.showingAll', { total: inspectionsTotal })}
					</span>
				</div>
			{/if}
		</div>
	{/if}
</PageHeader>

<Modal open={detailId !== null} ariaLabel={m('goodsReceipts.modal.aria')} onclose={() => (detailId = null)}>
	{#snippet header()}
		<header class="modal-header">
			<div class="title-block">
				<h2>{m('goodsReceipts.modal.title')}</h2>
				{#if detail}
					<span class="num-badge">{detail.gr_number}</span>
					<Badge tone={goodsReceiptTone(detail.status)} variant={detail.status}>{detail.status}</Badge>
				{/if}
			</div>
			<button class="close-btn" onclick={() => (detailId = null)} aria-label={m('goodsReceipts.modal.close')}>&times;</button>
		</header>
	{/snippet}

	<div class="modal-body">
		{#if detailLoading}
			<div class="loading">{m('common.loading')}</div>
		{:else if detail}
			<dl class="meta">
				<dt>{m('goodsReceipts.modal.po')}</dt><dd class="mono">{detail.po_number ?? '—'}</dd>
				<dt>{m('goodsReceipts.modal.received')}</dt><dd>{formatDate(detail.received_date)}</dd>
			</dl>

			<h3>{m('goodsReceipts.modal.lineItemsReceived')}</h3>
			<table class="line-table">
				<thead>
					<tr>
						<th>{m('goodsReceipts.modal.description')}</th>
						<th class="right">{m('goodsReceipts.modal.quantityReceived')}</th>
					</tr>
				</thead>
				<tbody>
					{#each detail.line_items as li (li.id)}
						<tr>
							<td>{li.description ?? '—'}</td>
							<td class="right mono">{li.quantity_received ?? '—'}</td>
						</tr>
					{:else}
						<tr><td colspan="2" class="empty">{m('goodsReceipts.modal.noLineItems')}</td></tr>
					{/each}
				</tbody>
			</table>

			<!-- The 4-way quality leg for THIS delivery. `po_matching` reads the
			     most recent of these rows (by `created_at`), so the list is
			     ordered newest-first to match what the matcher would pick. -->
			<div class="section-head">
				<h3>{m('goodsReceipts.modal.inspections')}</h3>
				{#if canMutate}
					<button
						class="btn-inline"
						onclick={() =>
							openRecordFor({
								id: detail!.id,
								gr_number: detail!.gr_number,
								po_id: detail!.po_id,
								po_number: detail!.po_number
							})}
						data-testid="record-inspection-for-receipt"
					>
						{m('goodsReceipts.modal.recordInspection')}
					</button>
				{/if}
			</div>
			<table class="line-table">
				<thead>
					<tr>
						<th>{m('goodsReceipts.inspections.col.number')}</th>
						<th>{m('goodsReceipts.inspections.col.result')}</th>
						<th>{m('goodsReceipts.inspections.col.inspected')}</th>
						<th class="right">{m('goodsReceipts.inspections.col.quantities')}</th>
					</tr>
				</thead>
				<tbody>
					{#each detailInspections as ins (ins.id)}
						<tr data-testid="receipt-inspection-row">
							<td class="mono">{ins.inspection_number}</td>
							<td>
								<Badge tone={resultTone(ins.result)} variant={ins.result}>
									{resultLabel(ins.result)}
								</Badge>
							</td>
							<td>{formatDate(ins.inspected_date)}</td>
							<td class="right mono">{quantitiesCell(ins)}</td>
						</tr>
					{:else}
						<tr>
							<td colspan="4" class="empty">
								{detailInspectionsLoading
									? m('common.loading')
									: m('goodsReceipts.modal.noInspections')}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>
</Modal>

{#if recordOpen}
	<RecordInspectionModal
		receipts={recordableReceipts}
		fixedReceipt={recordFor}
		onclose={() => {
			recordOpen = false;
			recordFor = null;
		}}
		onrecorded={onRecorded}
	/>
{/if}

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */
	/* The status pill is `<Badge>`, toned by `types/goodsReceipt.goodsReceiptTone`.
	   `GoodsReceipt.status` is a free-form `String(30)` with no normalisation on
	   write, so the tone is resolved from the one set the backend itself acts on
	   — `po_matching.CANCELLED_GR_STATUSES`, the receipts it excludes from the
	   3-way quantity leg. Those badge `muted`; everything else `success`.
	   `types/goodsReceipt.test.ts` fails if the two sets drift. */
	/* Detail modal — bespoke header / body layout not covered by the shared modal CSS. */
	.modal-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 16px 20px;
		border-bottom: 1px solid var(--border);
		margin: -24px -24px 0;
	}
	.title-block {
		display: flex;
		align-items: center;
		gap: 12px;
	}
	.modal-header h2 {
		margin: 0;
		font-size: 1.1rem;
		font-weight: 600;
	}
	.num-badge {
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.close-btn {
		background: none;
		border: none;
		font-size: 1.5rem;
		cursor: pointer;
		color: var(--text-muted);
		line-height: 1;
		padding: 0 4px;
	}
	.close-btn:hover {
		color: var(--text);
	}
	.modal-body {
		padding: 20px 0 0;
		overflow-y: auto;
	}
	.modal-body h3 {
		margin: 18px 0 8px;
		font-size: 0.85rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
	.section-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
	}
	.btn-inline {
		background: none;
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 4px 10px;
		font-size: 0.78rem;
		font-family: inherit;
		color: var(--text);
		cursor: pointer;
	}
	.btn-inline:hover {
		border-color: var(--accent);
		color: var(--accent);
	}
	/* Secondary header action (Sync from QMS), matching /vendors' ERP-sync
	   button — the primary action beside it is recording an inspection. */
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
	}
	.btn-outline:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.btn-outline:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.panel-hint {
		margin: 12px 0;
		font-size: 0.8rem;
		line-height: 1.5;
		max-width: 78ch;
	}
	dl.meta {
		display: grid;
		grid-template-columns: 90px 1fr 90px 1fr;
		gap: 8px 14px;
		margin: 0 0 18px;
		padding-bottom: 14px;
		border-bottom: 1px solid var(--border);
	}
	dt {
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
		align-self: center;
	}
	dd {
		margin: 0;
		font-size: 0.9rem;
	}
	.line-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}
	.line-table th {
		text-align: left;
		padding: 6px 10px;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
	}
	.line-table td {
		padding: 6px 10px;
		border-bottom: 1px solid var(--border);
	}
	.line-table .mono {
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.82rem;
	}
	.line-table .right {
		text-align: right;
	}
	.line-table .empty {
		text-align: center;
		padding: 40px;
		color: var(--text-muted);
	}
	.notes {
		max-width: 32ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.loading {
		padding: 40px;
		text-align: center;
		color: var(--text-muted);
	}
</style>
