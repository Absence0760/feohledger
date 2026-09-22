<script lang="ts">
	import { api } from '$lib/api';
	import { appendUnique } from '$lib/utils/pagination';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import type { BadgeTone } from '$lib/components/ui/badgeTone';
	import { formatMoney } from '$lib/utils/money';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { formatDate } from '$lib/utils/time';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { untrack } from 'svelte';

	const COLUMNS = $derived([
		{ label: m('purchaseOrders.col.poNumber') },
		{ label: m('purchaseOrders.col.vendor') },
		{ label: m('purchaseOrders.col.total'), class: 'right' },
		{ label: m('purchaseOrders.col.status') },
		{ label: m('purchaseOrders.col.lines') },
		{ label: m('purchaseOrders.col.created') }
	]);

	interface POLineItem {
		id: string;
		description: string | null;
		quantity: number | null;
		unit_price: number | null;
		total: number | null;
	}

	interface POListItem {
		id: string;
		po_number: string;
		vendor_id: string | null;
		vendor_name: string | null;
		total: number;
		/**
		 * The PO's OWN currency — `total` and every line are in it. `null` when
		 * no source recorded one (a PO older than migration 0099 with no
		 * requisition behind it), and the figures then render bare: the org's
		 * currency would be a claim nobody made (decisions §197).
		 */
		currency: string | null;
		status: string;
		line_items: POLineItem[];
		created_at: string;
	}

	interface POLinkedInvoice {
		id: string;
		invoice_number: string;
		vendor_name: string | null;
		amount: number;
		/** The INVOICE's currency, which need not be the PO's. */
		currency: string | null;
		status: string;
	}

	interface PODetail extends POListItem {
		linked_invoices: POLinkedInvoice[];
	}

	const PAGE_SIZE = 20;

	let pos = $state<POListItem[]>([]);
	let total = $state(0);
	let page = $state(1);
	let loading = $state(true);
	let loadingMore = $state(false);
	let search = $state('');
	let statusFilter = $state<'all' | 'open' | 'closed' | 'cancelled'>('all');
	/**
	 * Badge tone per PO status.
	 *
	 * `cancelled` keeps the tinted grey (`muted`) it had, distinct from the
	 * flat `neutral` an unrecognised status falls back to — the old CSS drew
	 * exactly that line (a `rgba(150,150,150,.15)` tint for cancelled, a bare
	 * `--bg` fill for everything else), and it is worth keeping: a cancelled PO
	 * is a decision somebody made, an unknown status is one nobody has told us
	 * about. `PurchaseOrder.status` is a free-form column, hence the fallback.
	 */
	const STATUS_TONES: Record<string, BadgeTone> = {
		open: 'accent',
		closed: 'success',
		cancelled: 'muted'
	};

	let syncing = $state(false);

	let detailId = $state<string | null>(null);
	let detail = $state<PODetail | null>(null);
	let detailLoading = $state(false);

	let searchTimer: ReturnType<typeof setTimeout> | null = null;

	$effect(() => {
		void loadPos();
		void fetchCounts();
	});

	// A status chip is a discrete action, so it fetches immediately — it used
	// to share the text box's 250ms timer, which made every chip click wait a
	// quarter-second for no reason. `search` is read through `untrack` so this
	// effect depends on `statusFilter` alone (the request still carries the
	// live search term).
	//
	// Both effects skip their own mount-time run: a Svelte `$effect` always
	// fires once immediately regardless of whether its tracked value actually
	// changed, so without the guard each would queue a redundant `loadPos()`
	// on top of the mount load above.
	let statusEffectRan = false;
	$effect(() => {
		const s = statusFilter;
		if (!statusEffectRan) {
			statusEffectRan = true;
			return;
		}
		void loadPos({ search: untrack(() => search), status: s });
	});

	let searchEffectRan = false;
	$effect(() => {
		const q = search;
		if (!searchEffectRan) {
			searchEffectRan = true;
			return;
		}
		if (searchTimer) clearTimeout(searchTimer);
		searchTimer = setTimeout(() => {
			void loadPos({ search: q, status: untrack(() => statusFilter) });
			// The tallies are search-scoped too — a chip count that ignored the
			// active search would contradict the list under it.
			void fetchCounts({ search: q });
		}, 250);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone and lands a stale list into the shared store.
		return () => {
			if (searchTimer) clearTimeout(searchTimer);
		};
	});

	$effect(() => {
		if (!detailId) {
			detail = null;
			return;
		}
		void loadDetail(detailId);
	});

	// Sequences every `loadPos` call (mount, chip, debounced search, load-more —
	// one shared counter, latest-issued wins) so a slow response for an earlier
	// search/filter can't land after a faster later one and clobber the list,
	// and so a load-more can't append the previous filter's page onto the new
	// one. This page mutates nothing in place — `syncFromErp` re-fetches — so it
	// needs no `supersedeInFlight()` call. See `frontend/CLAUDE.md` § Sequencing
	// list fetches.
	const fetchSequence = createRequestSequencer();

	async function loadPos(
		opts: { search?: string; status?: string; append?: boolean; nextPage?: number } = {}
	) {
		const token = fetchSequence.start();
		loading = !opts.append;
		try {
			const nextPage = opts.nextPage ?? 1;
			const params = new URLSearchParams({
				page: String(nextPage),
				page_size: String(PAGE_SIZE)
			});
			if (opts.search) params.set('search', opts.search);
			if (opts.status && opts.status !== 'all') params.set('status', opts.status);
			const data = await api.get<{ items: POListItem[]; total: number }>(
				`/api/purchase-orders?${params}`
			);
			// Superseded by a newer load — discard rather than clobber.
			if (!fetchSequence.canCommit(token)) return;
			pos = opts.append ? appendUnique(pos, data.items) : data.items;
			total = data.total;
			page = nextPage;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: only the newest request reports.
			if (!fetchSequence.isCurrentRequest(token)) return;
			toast(err instanceof Error ? err.message : m('purchaseOrders.toast.loadListFailed'), 'error');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) loading = false;
		}
	}

	async function loadMore() {
		loadingMore = true;
		try {
			await loadPos({ search, status: statusFilter, append: true, nextPage: page + 1 });
		} finally {
			loadingMore = false;
		}
	}

	// Whole-set status tallies from GET /api/purchase-orders/counts (search-aware,
	// entity-scoped) — the same contract `/api/vendors/counts` and
	// `/api/payments/counts` already serve. Without them the only number this
	// page has is `total`, which counts the ACTIVE filter's result set: rendering
	// it on the All chip while another chip was active labelled the filtered
	// count "All", so it could only be shown while All was itself active, and the
	// other chips could carry no count at all.
	//
	// It degrades to exactly that older behaviour when the endpoint isn't
	// reachable: `countsUnavailable` latches the first failure so a search
	// keystroke can't re-issue a request already known to fail, nothing is
	// toasted, and the chips simply lose their badges rather than showing a wrong
	// or blank number.
	let statusCounts = $state<Record<string, number>>({});
	let countsTotal = $state<number | null>(null);
	let countsUnavailable = false;

	// Its own sequencer: the counts are an independent request stream from the
	// list (a shared counter would let a counts response retire the list's
	// in-flight one), and two debounced searches can otherwise land out of order
	// and leave the chips describing a search the table isn't showing.
	const countsSequence = createRequestSequencer();

	async function fetchCounts(opts: { search?: string } = {}) {
		if (countsUnavailable) return;
		const token = countsSequence.start();
		try {
			const params = new URLSearchParams();
			const q = (opts.search ?? '').trim();
			if (q) params.set('search', q);
			const qs = params.toString();
			const data = await api.get<{ total: number; by_status: Record<string, number> }>(
				`/api/purchase-orders/counts${qs ? `?${qs}` : ''}`
			);
			if (!countsSequence.canCommit(token)) return;
			statusCounts = data.by_status ?? {};
			countsTotal = data.total ?? 0;
		} catch {
			if (!countsSequence.isCurrentRequest(token)) return;
			countsUnavailable = true;
			statusCounts = {};
			countsTotal = null;
		}
	}

	// Prefer the whole-set tallies; fall back to the filtered `total`, and only
	// while All is the active filter — the one case where that number IS the
	// whole-set count.
	let allCount = $derived(
		countsTotal !== null ? countsTotal : statusFilter === 'all' ? total : undefined
	);

	let statusChips = $derived([
		{ key: 'all', label: m('common.all'), count: allCount },
		{ key: 'open', label: m('purchaseOrders.filter.open'), count: statusCounts.open },
		{ key: 'closed', label: m('purchaseOrders.filter.closed'), count: statusCounts.closed },
		{
			key: 'cancelled',
			label: m('purchaseOrders.filter.cancelled'),
			count: statusCounts.cancelled
		}
	]);

	// The detail modal is a THIRD independent request stream, so it gets its own
	// sequencer: open a PO, close it, open another, and the first response can
	// land in the dialog now showing the second — the wrong line items under the
	// right PO number. Same shape as `/goods-receipts`.
	const detailSequence = createRequestSequencer();

	async function loadDetail(id: string) {
		const token = detailSequence.start();
		detailLoading = true;
		try {
			const data = await api.get<PODetail>(`/api/purchase-orders/${id}`);
			if (!detailSequence.canCommit(token)) return;
			detail = data;
		} catch (err) {
			if (!detailSequence.isCurrentRequest(token)) return;
			toast(err instanceof Error ? err.message : m('purchaseOrders.toast.loadFailed'), 'error');
			detailId = null;
		} finally {
			if (detailSequence.isCurrentRequest(token)) detailLoading = false;
		}
	}

	async function syncFromErp() {
		syncing = true;
		try {
			const result = await api.post<{ message: string }>('/api/purchase-orders/sync-erp', {});
			toast(result.message, 'success');
			// A sync imports POs, so the tallies moved too.
			await Promise.all([loadPos({ search, status: statusFilter }), fetchCounts({ search })]);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('purchaseOrders.toast.syncFailed'), 'error');
		} finally {
			syncing = false;
		}
	}

	// Every figure on this page is labelled by the currency its own row names —
	// the PO's for its total and lines, the invoice's for a linked invoice —
	// and renders bare when that is `null`. It used to stamp the org's currency
	// on every PO, so a EUR order read as dollars (decisions §196, §197).

	let hasMore = $derived(pos.length < total);
</script>

<PageHeader title={m('purchaseOrders.title')}>
	{#snippet actions()}
		{#if auth.isManager}
			<!-- `POST /api/purchase-orders/sync-erp` is
			     require_roles(ADMIN, AP_MANAGER). A CFO reaches this page for the
			     read (nav.ts) but holds neither role, so the button only 403'd. -->
			<button class="btn-outline" disabled={syncing} onclick={syncFromErp}>
				{syncing ? m('purchaseOrders.action.syncing') : m('purchaseOrders.action.syncErp')}
			</button>
		{/if}
	{/snippet}

	<div class="filter-row">
		<SearchBox bind:value={search} placeholder={m('purchaseOrders.search.placeholder')} ariaLabel={m('purchaseOrders.search.aria')} />
		<FilterChips chips={statusChips} bind:active={statusFilter} />
	</div>

	<DataTable
		columns={COLUMNS}
		isEmpty={pos.length === 0}
		empty={loading ? m('common.loading') : m('purchaseOrders.empty')}
	>
		{#snippet body()}
			{#each pos as po (po.id)}
				<tr
					class="clickable"
					onclick={(e) => {
						if (isRowOpenClick(e)) detailId = po.id;
					}}
				>
					<td class="mono">
						<RowLink
							onclick={() => (detailId = po.id)}
							ariaLabel={m('purchaseOrders.row.view', { number: po.po_number })}
						>
							{po.po_number}
						</RowLink>
					</td>
					<td>{po.vendor_name ?? '—'}</td>
					<td class="right mono" data-testid="po-total">{formatMoney(po.total, { currency: po.currency })}</td>
					<td><Badge tone={STATUS_TONES[po.status] ?? 'neutral'} variant={po.status}>{po.status}</Badge></td>
					<td class="muted">{po.line_items.length}</td>
					<td class="muted">{formatDate(po.created_at)}</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>

	{#if hasMore}
		<div class="load-more-row">
			<button class="btn-load-more" onclick={loadMore} disabled={loadingMore}>
				{loadingMore ? m('common.loading') : m('purchaseOrders.loadMore', { shown: pos.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('purchaseOrders.showingAll', { total })}</span>
		</div>
	{/if}
</PageHeader>

<Modal
	open={detailId !== null}
	ariaLabel={m('purchaseOrders.modal.aria')}
	width="lg"
	onclose={() => (detailId = null)}
>
	{#snippet header()}
		<header class="modal-header">
			<div class="title-block">
				<h2>{m('purchaseOrders.modal.title')}</h2>
				{#if detail}
					<span class="po-number-badge">{detail.po_number}</span>
					<Badge tone={STATUS_TONES[detail.status] ?? 'neutral'} variant={detail.status}>{detail.status}</Badge>
				{/if}
			</div>
			<button class="close-btn" onclick={() => (detailId = null)} aria-label={m('purchaseOrders.modal.close')}>&times;</button>
		</header>
	{/snippet}

	<div class="modal-body">
		{#if detailLoading}
			<div class="loading">{m('common.loading')}</div>
		{:else if detail}
			<dl class="meta">
				<dt>{m('purchaseOrders.modal.vendor')}</dt><dd>{detail.vendor_name ?? '—'}</dd>
				<dt>{m('purchaseOrders.modal.total')}</dt><dd class="mono" data-testid="po-detail-total">{formatMoney(detail.total, { currency: detail.currency })}</dd>
				<dt>{m('purchaseOrders.modal.created')}</dt><dd>{formatDate(detail.created_at)}</dd>
			</dl>

			<h3>{m('purchaseOrders.modal.lineItems')}</h3>
			<table class="line-table">
				<thead>
					<tr>
						<th>{m('purchaseOrders.modal.description')}</th>
						<th class="right">{m('purchaseOrders.modal.qty')}</th>
						<th class="right">{m('purchaseOrders.modal.unitPrice')}</th>
						<th class="right">{m('purchaseOrders.modal.lineTotal')}</th>
					</tr>
				</thead>
				<tbody>
					{#each detail.line_items as li (li.id)}
						<tr>
							<td>{li.description ?? '—'}</td>
							<td class="right mono">{li.quantity ?? '—'}</td>
							<td class="right mono">{formatMoney(li.unit_price, { currency: detail.currency })}</td>
							<td class="right mono">{formatMoney(li.total, { currency: detail.currency })}</td>
						</tr>
					{:else}
						<tr><td colspan="4" class="empty">{m('purchaseOrders.modal.noLineItems')}</td></tr>
					{/each}
				</tbody>
			</table>

			<h3>{m('purchaseOrders.modal.linkedInvoices', { count: detail.linked_invoices.length })}</h3>
			<table class="line-table">
				<thead>
					<tr>
						<th>{m('purchaseOrders.modal.invoiceNumber')}</th>
						<th>{m('purchaseOrders.modal.invoiceVendor')}</th>
						<th class="right">{m('purchaseOrders.modal.amount')}</th>
						<th>{m('purchaseOrders.modal.invoiceStatus')}</th>
					</tr>
				</thead>
				<tbody>
					{#each detail.linked_invoices as inv (inv.id)}
						<tr>
							<td class="mono">{inv.invoice_number}</td>
							<td>{inv.vendor_name ?? '—'}</td>
							<td class="right mono">{formatMoney(inv.amount, { currency: inv.currency })}</td>
							<td>{inv.status}</td>
						</tr>
					{:else}
						<tr><td colspan="4" class="empty">{m('purchaseOrders.modal.noLinkedInvoices')}</td></tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>
</Modal>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */
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

	/* Detail modal — custom header + body + nested line tables. */
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
	.po-number-badge {
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
		flex: 1;
	}
	.modal-body h3 {
		margin: 18px 0 8px;
		font-size: 0.85rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
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
	dd.mono {
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.82rem;
	}
	.line-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}
	.line-table th {
		background: var(--bg);
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
	.line-table .right {
		text-align: right;
	}
	.line-table .mono {
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.82rem;
	}
	.line-table .empty {
		text-align: center;
		padding: 40px;
		color: var(--text-muted);
	}
	.loading {
		padding: 40px;
		text-align: center;
		color: var(--text-muted);
	}
</style>
