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
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import { formatDate } from '#lib/utils/time.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import {
		portalPaymentStatusLabelKey,
		portalPaymentStatusTone,
		PORTAL_PAYMENT_PHASES
	} from '#lib/types/portalStatus.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import PortalListFilters from '#lib/components/portal/PortalListFilters.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { paymentMethodLabelKey } from '#lib/types/payment.ts';
	import { formatCount } from '#lib/utils/count.ts';

	type PortalPayment = PortalPaymentListItem;

	let items = $state<PortalPayment[]>([]);
	// Server-side count of the vendor's whole payment history, not the loaded
	// page — older remittances live behind the Load-more control below.
	let total = $state(0);
	let pageNum = $state(1);
	let loading = $state(false);
	let loadingMore = $state(false);
	let error = $state('');
	// A failed first-page read — kept apart from `error` so it is never
	// rendered as "no payments yet" (see the invoice list).
	let loadError = $state('');
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
		if (!opts.append) loadError = '';
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
			const msg = err instanceof Error ? err.message : m('portal.payments.loadFailed');
			if (opts.append) {
				error = msg;
			} else {
				items = [];
				total = 0;
				loadError = msg;
			}
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

	// The rail is a backend enum (`virtual_card`, `faster_payments`); render its
	// label, and the raw value only for a rail this build does not know.
	function methodLabel(method: string | null): string {
		if (!method) return m('portal.common.dash');
		const key = paymentMethodLabelKey(method);
		return key ? m(key) : method;
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
	{:else if loadError}
		<LoadError message={loadError} onretry={refresh} testId="portal-payments-load-error" />
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
		<!-- DataTable: eight columns scroll inside a named, focusable region
		     at narrow widths instead of overflowing the page (WCAG 1.4.10). -->
		<DataTable
			ariaLabel={m('portal.payments.tableAria')}
			columns={[
				{ label: m('portal.payments.col.invoiceNumber') },
				{ label: m('portal.payments.col.submitted') },
				{ label: m('portal.payments.col.completed') },
				{ label: m('portal.payments.col.method') },
				{ label: m('portal.payments.col.amount'), class: 'right' },
				{ label: m('portal.payments.col.status') },
				{ label: m('portal.payments.col.reference') },
				{}
			]}
		>
			{#snippet body()}
				{#each items as p (p.id)}
					<tr>
						<td>{p.invoice_number}</td>
						<td>{formatDate(p.submitted_at, m('portal.common.dash'))}</td>
						<td>{formatDate(p.completed_at, m('portal.common.dash'))}</td>
						<td>{methodLabel(p.method)}</td>
						<td class="right"><Money amount={p.amount} currency={p.currency} mono /></td>
						<td><Badge tone={portalPaymentStatusTone(p.status)} variant="status-badge {p.status}"
								>{m(portalPaymentStatusLabelKey(p.status))}</Badge
							></td>
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
			{/snippet}
		</DataTable>

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
						: m('portal.payments.loadMore', { shown: formatCount(items.length), total: formatCount(total) })}
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
	/* Table chrome (padding, borders, header, `.right`, `td.actions`) is
	   app.css's `.grid-container`, owned by DataTable. */
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
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
		color: var(--danger);
		padding: 10px 14px;
		border-radius: 4px;
		margin-bottom: 12px;
	}
</style>
