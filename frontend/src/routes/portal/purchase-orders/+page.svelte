<script lang="ts">
	import {
		portalApi,
		listPortalPurchaseOrders,
		PORTAL_PAGE_SIZE,
		type PortalPOListItem,
	} from '#lib/portalApi.ts';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import { formatDate } from '#lib/utils/time.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	type PortalPO = PortalPOListItem;

	let items = $state<PortalPO[]>([]);
	// Server-side count of every PO the vendor owns, not the loaded page —
	// an older PO waiting to be flipped is behind the Load-more control.
	let total = $state(0);
	let pageNum = $state(1);
	let loading = $state(false);
	let loadingMore = $state(false);
	let error = $state('');
	let message = $state('');
	let flipping = $state<string | null>(null);

	const hasMore = $derived(items.length < total);

	// Sequences `load` so a slow first page can't land after a Load-more and
	// drop the appended rows. A successful flip navigates away rather than
	// editing the list in place, so there is no `supersedeInFlight()` call.
	// See `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		error = '';
		try {
			const res = await listPortalPurchaseOrders({
				page: nextPage,
				page_size: PORTAL_PAGE_SIZE,
			});
			if (!fetchSequence.canCommit(token)) return;
			items = opts.append ? appendUnique(items, res.items) : res.items;
			total = res.total;
			pageNum = nextPage;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit` — a superseded load still failed.
			if (!fetchSequence.isCurrentRequest(token)) return;
			error = err instanceof Error ? err.message : m('portal.po.loadFailed');
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

	async function flip(po: PortalPO) {
		flipping = po.id;
		error = '';
		message = '';
		try {
			await portalApi.post<{ message: string }>(
				`/api/portal/purchase-orders/${po.id}/flip`,
				{}
			);
			message = m('portal.po.created', { po: po.po_number });
			// Land the supplier on their invoices so they can see it in the queue.
			await goto('/portal/invoices');
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.po.createFailed');
		} finally {
			flipping = null;
		}
	}

	onMount(refresh);
</script>

<div class="page">
	<header>
		<h1>{m('portal.po.title')}</h1>
	</header>

	{#if error}<div class="error" role="alert">{error}</div>{/if}
	<div role="status">{#if message}<div class="message">{message}</div>{/if}</div>

	{#if loading && !items.length}
		<div class="loading">{m('portal.common.loading')}</div>
	{:else if !items.length}
		<div class="empty">
			<p>{m('portal.po.empty')}</p>
			<p class="hint">{m('portal.po.emptyHint')}</p>
		</div>
	{:else}
		<table>
			<thead>
				<tr>
					<th>{m('portal.po.col.poNumber')}</th>
					<th>{m('portal.po.col.created')}</th>
					<th>{m('portal.po.col.lines')}</th>
					<th class="num">{m('portal.po.col.total')}</th>
					<th>{m('portal.po.col.status')}</th>
					<th class="actions-col"></th>
				</tr>
			</thead>
			<tbody>
				{#each items as po (po.id)}
					<tr>
						<td>{po.po_number}</td>
						<td>{formatDate(po.created_at, m('portal.common.dash'))}</td>
						<td>{po.line_item_count}</td>
						<td class="num"><Money amount={po.total} currency={po.currency} /></td>
						<td><span class="status s-{po.status}">{po.status}</span></td>
						<td class="actions">
							<button
								type="button"
								class="flip-btn"
								disabled={flipping === po.id}
								onclick={() => flip(po)}
							>
								{flipping === po.id ? m('portal.po.creating') : m('portal.po.createInvoice')}
							</button>
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
						: m('portal.po.loadMore', { shown: items.length, total })}
				</button>
			</div>
		{:else if total > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('portal.po.showingAll', { total })}</span>
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
	.flip-btn {
		padding: 4px 12px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: transparent;
		color: var(--text);
		font-size: 0.82rem;
		cursor: pointer;
	}
	.flip-btn:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.flip-btn:disabled {
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
	.error {
		background: rgba(224, 64, 64, 0.1);
		border: 1px solid rgba(224, 64, 64, 0.3);
		color: var(--danger);
		padding: 10px 14px;
		border-radius: 4px;
		margin-bottom: 12px;
	}
	.message {
		background: rgba(40, 160, 80, 0.12);
		border: 1px solid rgba(40, 160, 80, 0.35);
		color: var(--success);
		padding: 10px 14px;
		border-radius: 4px;
		margin-bottom: 12px;
	}
</style>
