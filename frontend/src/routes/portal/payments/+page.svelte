<script lang="ts">
	import {
		portalApi,
		listPortalPayments,
		PORTAL_PAGE_SIZE,
		type PortalPaymentListItem,
	} from '#lib/portalApi.ts';
	import { onMount, untrack } from 'svelte';
	import { page } from '$app/state';
	import { replaceState } from '$app/navigation';
	import Money from '#lib/components/ui/Money.svelte';
	import { formatDate } from '#lib/utils/time.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { portalPaymentStatusLabelKey, PORTAL_PAYMENT_PHASES } from '#lib/types/portalStatus.ts';
	import PortalListFilters from '#lib/components/portal/PortalListFilters.svelte';

	type PortalPayment = PortalPaymentListItem;

	let items = $state<PortalPayment[]>([]);
	// Server-side count of the vendor's whole payment history, not the loaded
	// page — older remittances live behind the Load-more control below.
	let total = $state(0);
	let pageNum = $state(1);
	let loading = $state(false);
	let loadingMore = $state(false);
	let error = $state('');
	let downloading = $state<string | null>(null);

	const hasMore = $derived(items.length < total);

	/* URL-backed filter state, mirroring the invoice list: a supplier who
	 * bookmarks or reloads a filtered history keeps it. `phase` is validated
	 * against the known chips so a hand-edited URL cannot select a bucket the
	 * backend would not recognise, and it carries the phase ID (`completed`),
	 * never its label — so the link survives a locale switch. */
	const seededPhase = (() => {
		const raw = page.url.searchParams.get('phase');
		if (!raw) return null;
		return PORTAL_PAYMENT_PHASES.some((c) => c.phase === raw) ? raw : null;
	})();
	const initialFilters = {
		phase: seededPhase,
		search: page.url.searchParams.get('search') ?? '',
		dateFrom: page.url.searchParams.get('date_from') ?? '',
		dateTo: page.url.searchParams.get('date_to') ?? '',
	};

	// --- Filters. PortalListFilters owns the phase chips + debounced search
	// (see the invoice list); a phase expands to the raw `payments.status`
	// values behind it, sent as repeated `?status=`.
	let activePhase = $state<string | null>(initialFilters.phase);
	let activeSearch = $state(initialFilters.search);
	let activeDateFrom = $state(initialFilters.dateFrom);
	let activeDateTo = $state(initialFilters.dateTo);
	let filtersEl = $state<{ reset: () => void } | undefined>();
	const filtered = $derived(
		activePhase !== null || activeSearch.trim() !== '' || activeDateFrom !== '' || activeDateTo !== ''
	);

	function phaseStatuses(p: string | null): string[] | undefined {
		if (p === null) return undefined;
		return PORTAL_PAYMENT_PHASES.find((c) => c.phase === p)?.statuses;
	}

	function syncUrl() {
		untrack(() => {
			const url = new URL(page.url.href);
			const set = (k: string, v: string) =>
				v ? url.searchParams.set(k, v) : url.searchParams.delete(k);
			set('phase', activePhase ?? '');
			set('search', activeSearch.trim());
			set('date_from', activeDateFrom);
			set('date_to', activeDateTo);
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	function applyFilters(f: {
		phase: string | null;
		search: string;
		dateFrom: string;
		dateTo: string;
	}) {
		activePhase = f.phase;
		activeSearch = f.search;
		activeDateFrom = f.dateFrom;
		activeDateTo = f.dateTo;
		syncUrl();
		load();
	}

	// Sequences `load` so a slow first page can't land after a Load-more and
	// drop the appended rows. Nothing edits the list in place (the remittance
	// download is read-only), so there is no `supersedeInFlight()` call.
	// See `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		error = '';
		try {
			const res = await listPortalPayments({
				page: nextPage,
				page_size: PORTAL_PAGE_SIZE,
				status: phaseStatuses(activePhase),
				search: activeSearch.trim() || undefined,
				date_from: activeDateFrom || undefined,
				date_to: activeDateTo || undefined,
			});
			if (!fetchSequence.canCommit(token)) return;
			items = opts.append ? appendUnique(items, res.items) : res.items;
			total = res.total;
			pageNum = nextPage;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit` — a superseded load still failed.
			if (!fetchSequence.isCurrentRequest(token)) return;
			error = err instanceof Error ? err.message : m('portal.payments.loadFailed');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	/** Reload from page 1. */
	function refresh() {
		return load();
	}

	async function downloadRemittance(p: PortalPayment) {
		downloading = p.id;
		error = '';
		try {
			const blob = await portalApi.download(`/api/portal/payments/${p.id}/remittance`);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `remittance-${p.reference || p.id.slice(0, 8)}.pdf`;
			document.body.appendChild(a);
			a.click();
			a.remove();
			URL.revokeObjectURL(url);
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.payments.downloadFailed');
		} finally {
			downloading = null;
		}
	}

	onMount(refresh);
</script>

<div class="page">
	<header>
		<h1>{m('portal.payments.title')}</h1>
	</header>

	{#if error}<div class="error" role="alert">{error}</div>{/if}

	<PortalListFilters
		bind:this={filtersEl}
		chips={PORTAL_PAYMENT_PHASES.map((c) => ({ key: c.phase, label: m(c.labelKey) }))}
		allLabel={m('portal.payments.filterAll')}
		groupLabel={m('portal.payments.col.status')}
		searchLabel={m('portal.payments.searchLabel')}
		searchPlaceholder={m('portal.payments.searchPlaceholder')}
		dateFromLabel={m('portal.payments.dateFromLabel')}
		dateToLabel={m('portal.payments.dateToLabel')}
		initial={initialFilters}
		onchange={applyFilters}
	/>

	{#if loading && !items.length}
		<div class="loading">{m('portal.common.loading')}</div>
	{:else if !items.length}
		<div class="empty">
			{#if filtered}
				<p>{m('portal.payments.emptyFiltered')}</p>
				<button type="button" class="link-btn" onclick={() => filtersEl?.reset()}
					>{m('portal.payments.clearFilters')}</button
				>
			{:else}
				<p>{m('portal.payments.empty')}</p>
				<p class="hint">{m('portal.payments.emptyHint')}</p>
			{/if}
		</div>
	{:else}
		<table>
			<thead>
				<tr>
					<th>{m('portal.payments.col.invoiceNumber')}</th>
					<th>{m('portal.payments.col.submitted')}</th>
					<th>{m('portal.payments.col.completed')}</th>
					<th>{m('portal.payments.col.method')}</th>
					<th class="num">{m('portal.payments.col.amount')}</th>
					<th>{m('portal.payments.col.status')}</th>
					<th>{m('portal.payments.col.reference')}</th>
					<th class="actions-col"></th>
				</tr>
			</thead>
			<tbody>
				{#each items as p (p.id)}
					<tr>
						<td>{p.invoice_number}</td>
						<td>{formatDate(p.submitted_at, m('portal.common.dash'))}</td>
						<td>{formatDate(p.completed_at, m('portal.common.dash'))}</td>
						<td>{p.method || m('portal.common.dash')}</td>
						<td class="num"><Money amount={p.amount} currency={p.currency} /></td>
						<td><span class="status s-{p.status}">{m(portalPaymentStatusLabelKey(p.status))}</span></td>
						<td>{p.reference || m('portal.common.dash')}</td>
						<td class="actions">
							{#if p.status === 'completed'}
								<button
									type="button"
									class="remit-btn"
									disabled={downloading === p.id}
									onclick={() => downloadRemittance(p)}
								>
									{downloading === p.id ? m('portal.payments.preparing') : m('portal.payments.downloadRemittance')}
								</button>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>

		{#if hasMore}
			<div class="load-more-row">
				<button
					type="button"
					class="btn-load-more"
					onclick={() => load({ append: true })}
					disabled={loadingMore}
				>
					{loadingMore
						? m('portal.common.loading')
						: m('portal.payments.loadMore', { shown: items.length, total })}
				</button>
			</div>
		{:else if total > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('portal.payments.showingAll', { total })}</span>
			</div>
		{/if}
	{/if}
</div>

<style>
	.page {
		max-width: 1100px;
		margin: 0 auto;
	}
	header {
		margin-bottom: 20px;
	}
	h1 {
		margin: 0;
		font-size: 1.25rem;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 4px;
		overflow: hidden;
	}
	th,
	td {
		text-align: left;
		padding: 10px 12px;
		font-size: 0.88rem;
		border-bottom: 1px solid var(--border);
	}
	th {
		background: var(--bg);
		color: var(--text-muted);
		font-weight: 500;
		text-transform: uppercase;
		font-size: 0.72rem;
		letter-spacing: 0.04em;
	}
	tbody tr:last-child td {
		border-bottom: none;
	}
	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 6px;
		white-space: nowrap;
	}
	.remit-btn {
		padding: 4px 12px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: transparent;
		color: var(--text);
		font-size: 0.8rem;
		cursor: pointer;
	}
	.remit-btn:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.remit-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.status {
		padding: 2px 8px;
		border-radius: 3px;
		font-size: 0.75rem;
		background: var(--bg);
		border: 1px solid var(--border);
	}
	.s-completed {
		background: rgba(40, 160, 80, 0.15);
		border-color: rgba(40, 160, 80, 0.4);
	}
	.s-failed {
		background: rgba(224, 64, 64, 0.12);
		border-color: rgba(224, 64, 64, 0.35);
	}
	.empty,
	.loading {
		padding: 40px;
		text-align: center;
		background: var(--surface);
		border: 1px dashed var(--border);
		border-radius: 4px;
		color: var(--text-muted);
	}
	.empty .hint {
		font-size: 0.82rem;
	}
	.link-btn {
		background: none;
		border: none;
		padding: 0;
		margin-top: 6px;
		font: inherit;
		font-size: 0.82rem;
		color: var(--accent);
		cursor: pointer;
		text-decoration: underline;
	}
	.error {
		background: rgba(224, 64, 64, 0.1);
		border: 1px solid rgba(224, 64, 64, 0.3);
		color: var(--danger);
		padding: 10px 14px;
		border-radius: 4px;
		margin-bottom: 12px;
	}
</style>
