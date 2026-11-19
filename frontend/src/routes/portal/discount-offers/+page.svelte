<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { page } from '$app/state';
	import { replaceState } from '$app/navigation';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { DISCOUNT_STATUS_TONES, type DiscountStatus } from '#lib/types/discounts.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { formatPercent } from '#lib/utils/percent.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import {
		listPortalDiscountOffers,
		acceptPortalDiscountOffer,
		declinePortalDiscountOffer,
		PORTAL_PAGE_SIZE,
		type PortalDiscountOffer,
	} from '#lib/portalApi.ts';

	const FILTERS = [
		{ key: '', labelKey: 'portal.discounts.filter.all' },
		{ key: 'offered', labelKey: 'portal.discounts.filter.open' },
		{ key: 'accepted', labelKey: 'portal.discounts.filter.accepted' },
		{ key: 'captured', labelKey: 'portal.discounts.filter.captured' },
		{ key: 'declined,expired', labelKey: 'portal.discounts.filter.closed' },
	] as const;

	// `status` is a backend enum; it renders through the same keys and tones
	// the AP-side /discounts list uses, never as the raw value.
	const STATUS_KEY: Record<DiscountStatus, MessageKey> = {
		offered: 'discounts.status.offered',
		accepted: 'discounts.status.accepted',
		captured: 'discounts.status.captured',
		declined: 'discounts.status.declined',
		expired: 'discounts.status.expired',
	};
	function statusLabel(status: string): string {
		const key = STATUS_KEY[status as DiscountStatus];
		return key ? m(key) : status;
	}

	let items = $state<PortalDiscountOffer[]>([]);
	// Server-side count of the whole FILTERED offer set, not the loaded page —
	// an offer past the first page still expires, so it has to be reachable.
	let total = $state(0);
	let pageNum = $state(1);
	let loading = $state(false);
	let loadingMore = $state(false);
	let error = $state('');
	// A failed first-page read — never rendered as "no offers right now".
	let loadError = $state('');
	// An accept that failed while the dialog is open is reported IN the
	// dialog; the page banner sits behind the modal where nobody can see it.
	let acceptError = $state('');
	// URL-backed like the invoice and payment lists: reload, back and a
	// bookmark keep the chip. Validated against the known chips.
	let activeFilter = $state(
		(() => {
			const raw = page.url.searchParams.get('status') ?? '';
			return FILTERS.some((f) => f.key === raw) ? raw : '';
		})()
	);
	let busy = $state<string | null>(null);
	// Declining is not reversible from the portal, so it takes two clicks:
	// the first arms the row's button, the second commits.
	let armedDeclineId = $state<string | null>(null);

	// What the row's "Applies to" cell shows; it also names the row's Accept /
	// Decline buttons so a screen reader hears which offer each one acts on.
	function offerLabel(o: PortalDiscountOffer): string {
		return o.scope === 'invoice'
			? m('portal.discounts.scopeInvoice', { number: o.invoice_number || m('portal.common.dash') })
			: m('portal.discounts.scopeAll');
	}
	// The offer whose tier the vendor is choosing in the accept dialog.
	let accepting = $state<PortalDiscountOffer | null>(null);
	let chosenTierDays = $state<number | null>(null);

	const hasMore = $derived(items.length < total);

	// Sequences `load` so a slow response for an earlier status filter can't
	// land after a faster later one (or after a Load-more) and blank the list.
	// Accept / decline re-read through this same loader rather than editing a
	// row in place, so there is no `supersedeInFlight()` call. See
	// `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		error = '';
		if (!opts.append) loadError = '';
		try {
			const res = await listPortalDiscountOffers(activeFilter || undefined, {
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
			const msg = err instanceof Error ? err.message : m('portal.discounts.loadFailed');
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

	/** Reload from page 1 (mount, filter change, and after accept / decline). */
	function refresh() {
		return load();
	}

	function setFilter(key: string) {
		activeFilter = key;
		armedDeclineId = null;
		untrack(() => {
			const url = new URL(page.url.href);
			if (key) url.searchParams.set('status', key);
			else url.searchParams.delete('status');
			replaceState(`${url.pathname}${url.search}`, {});
		});
		refresh();
	}

	function openAccept(offer: PortalDiscountOffer) {
		armedDeclineId = null;
		acceptError = '';
		accepting = offer;
		// Default to the best capturable tier today; fall back to the first tier.
		chosenTierDays = offer.best_tier?.days ?? offer.tiers[0]?.days ?? null;
	}

	function closeAccept() {
		accepting = null;
		chosenTierDays = null;
		acceptError = '';
	}

	async function confirmAccept() {
		if (!accepting) return;
		const offer = accepting;
		busy = offer.id;
		error = '';
		acceptError = '';
		try {
			await acceptPortalDiscountOffer(offer.id, chosenTierDays ?? undefined);
			closeAccept();
			await refresh();
		} catch (err) {
			acceptError = err instanceof Error ? err.message : m('portal.discounts.acceptFailed');
		} finally {
			busy = null;
		}
	}

	function onDeclineClick(offer: PortalDiscountOffer) {
		if (busy) return;
		if (armedDeclineId !== offer.id) {
			armedDeclineId = offer.id;
			return;
		}
		decline(offer);
	}

	// The armed Decline whose request is in flight: it stays armed (reading
	// "Confirm decline") and goes `busy` — aria-disabled, so the keyboard user
	// who pressed it keeps focus — until the request settles.
	const isDeclining = (id: string) => busy === id && armedDeclineId === id;

	async function decline(offer: PortalDiscountOffer) {
		busy = offer.id;
		error = '';
		try {
			await declinePortalDiscountOffer(offer.id);
			await refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.discounts.declineFailed');
		} finally {
			busy = null;
			armedDeclineId = null;
		}
	}

	// Tier percents arrive as 0–100 numbers; the shared formatter puts the
	// sign where the vendor's locale does ("2 %" in French), keeping up to two
	// places so a 1.25 % tier is not rounded into its neighbour.
	function fmtPct(n: number): string {
		return formatPercent(n, { maximumFractionDigits: 2 });
	}

	// The chosen tier object for the accept dialog (for the live savings preview).
	const chosenTier = $derived(
		accepting && chosenTierDays !== null
			? (accepting.tiers.find((t) => t.days === chosenTierDays) ?? null)
			: null,
	);

	onMount(refresh);
</script>

<!-- A click anywhere but a row action disarms a pending Decline
     (ui-patterns.md § Per-row actions). -->
<svelte:window
	onclick={(e) => {
		if (armedDeclineId && !busy && !(e.target as HTMLElement).closest('.row-action')) armedDeclineId = null;
	}}
/>

<div class="page">
	<header>
		<h1>{m('portal.discounts.title')}</h1>
		<p class="sub">{m('portal.discounts.subtitle')}</p>
	</header>

	<nav class="filters" aria-label={m('portal.discounts.filterAria')}>
		{#each FILTERS as f (f.key)}
			<button
				type="button"
				class="filter-chip"
				class:active={activeFilter === f.key}
				aria-pressed={activeFilter === f.key}
				onclick={() => setFilter(f.key)}>{m(f.labelKey)}</button
			>
		{/each}
	</nav>

	{#if error}<div class="error" role="alert">{error}</div>{/if}

	{#if loading && !items.length}
		<div class="loading">{m('portal.common.loading')}</div>
	{:else if loadError}
		<LoadError message={loadError} onretry={refresh} testId="portal-discounts-load-error" />
	{:else if !items.length}
		<div class="empty">
			{#if activeFilter}
				<!-- Filtered to zero is not "no offers": say so, and offer the way back. -->
				<p>{m('portal.discounts.emptyFiltered')}</p>
				<button type="button" class="link-btn" onclick={() => setFilter('')}
					>{m('portal.discounts.clearFilter')}</button
				>
			{:else}
				<p>{m('portal.discounts.empty')}</p>
				<p class="hint">{m('portal.discounts.emptyHint')}</p>
			{/if}
		</div>
	{:else}
		<!-- DataTable: eight columns scroll inside a named, focusable region
		     at narrow widths instead of overflowing the page (WCAG 1.4.10). -->
		<DataTable
			ariaLabel={m('portal.discounts.tableAria')}
			columns={[
				{ label: m('portal.discounts.col.appliesTo') },
				{ label: m('portal.discounts.col.amount'), class: 'right' },
				{ label: m('portal.discounts.col.tiers') },
				{ label: m('portal.discounts.col.bestDiscount'), class: 'right' },
				{ label: m('portal.discounts.col.youSave'), class: 'right' },
				{ label: m('portal.discounts.col.window') },
				{ label: m('portal.discounts.col.status') },
				{}
			]}
		>
			{#snippet body()}
				{#each items as o (o.id)}
					<tr>
						<td>
							{offerLabel(o)}
						</td>
						<td class="right"><Money amount={o.base_amount} currency={o.currency} mono /></td>
						<td class="tiers">
							{#each o.tiers as t (t.days)}
								<span class="tier">{m('portal.discounts.tier', { days: t.days, percent: fmtPct(t.percent) })}</span>
							{/each}
						</td>
						<td class="right">
							{o.best_tier ? fmtPct(o.best_tier.percent) : m('portal.common.dash')}
						</td>
						<td class="right">
							{#if o.status === 'captured' && o.captured_amount !== null}
								<Money amount={o.captured_amount} currency={o.currency} mono />
							{:else if o.accepted_tier}
								<Money amount={o.accepted_tier.savings} currency={o.currency} mono />
							{:else if o.best_tier}
								<Money amount={o.best_tier.savings} currency={o.currency} mono />
							{:else}
								{m('portal.common.dash')}
							{/if}
						</td>
						<td class="window">{formatDate(o.valid_from, m('portal.common.dash'))} – {formatDate(o.valid_until, m('portal.common.dash'))}</td>
						<td>
							<Badge tone={DISCOUNT_STATUS_TONES[o.status as DiscountStatus] ?? 'neutral'} variant="status-badge {o.status}"
								>{statusLabel(o.status)}</Badge
							>
						</td>
						<td class="actions">
							{#if o.status === 'offered'}
								<RowAction
									variant="success"
									disabled={busy !== null || !o.best_tier}
									ariaLabel={m('common.rowActionAria', { action: m('portal.discounts.accept'), name: offerLabel(o) })}
									onclick={() => openAccept(o)}
								>
									{m('portal.discounts.accept')}
								</RowAction>
								<RowAction
									variant="danger"
									armed={armedDeclineId === o.id}
									busy={isDeclining(o.id)}
									disabled={busy !== null && !isDeclining(o.id)}
									ariaLabel={m('common.rowActionAria', {
										action:
											armedDeclineId === o.id
												? m('portal.discounts.declineConfirm')
												: m('portal.discounts.decline'),
										name: offerLabel(o)
									})}
									onclick={() => onDeclineClick(o)}
								>
									{armedDeclineId === o.id
										? m('portal.discounts.declineConfirm')
										: m('portal.discounts.decline')}
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
					type="button"
					class="btn-load-more"
					onclick={() => load({ append: true })}
					disabled={loadingMore}
				>
					{loadingMore
						? m('portal.common.loading')
						: m('portal.discounts.loadMore', { shown: items.length, total })}
				</button>
			</div>
		{:else if total > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('portal.discounts.showingAll', { total })}</span>
			</div>
		{/if}
	{/if}
</div>

<!-- The shared Modal owns the backdrop, Esc, focus trap / restore and the
     background scroll lock; the body (tier picker, savings preview, footer)
     is this page's own. -->
<Modal
	open={accepting !== null}
	ariaLabel={m('portal.discounts.dialog.title')}
	title={m('portal.discounts.dialog.title')}
	width="sm"
	onclose={closeAccept}
>
	{#if accepting}
		<p class="modal-hint">{m('portal.discounts.dialog.subtitle')}</p>
		<!-- A failed accept is reported here, not in the page banner hidden
		     behind the backdrop. -->
		{#if acceptError}<div class="error" role="alert">{acceptError}</div>{/if}

		{#if accepting.tiers.length > 1}
			<label class="field">
				<span>{m('portal.discounts.dialog.chooseTier')}</span>
				<select bind:value={chosenTierDays}>
					{#each accepting.tiers as t (t.days)}
						<option value={t.days}>{m('portal.discounts.dialog.tierOption', { days: t.days, percent: fmtPct(t.percent) })}</option>
					{/each}
				</select>
			</label>
		{/if}

		{#if chosenTier}
			<div class="preview">
				<span>{m('portal.discounts.dialog.youSave')}</span>
				<strong><Money amount={chosenTier.savings} currency={accepting.currency} /></strong>
				<span class="muted">{m('portal.discounts.dialog.savingsDetail', { percent: fmtPct(chosenTier.percent), days: chosenTier.days })}</span>
			</div>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={closeAccept}>{m('portal.discounts.dialog.cancel')}</button>
			<button
				type="button"
				class="btn-primary"
				disabled={busy === accepting.id || chosenTierDays === null}
				onclick={confirmAccept}
			>
				{busy === accepting.id ? m('portal.discounts.dialog.accepting') : m('portal.discounts.dialog.accept')}
			</button>
		</div>
	{/if}
</Modal>

<style>
	.page {
		max-width: 1100px;
		margin: 0 auto;
	}
	header {
		margin-bottom: 16px;
	}
	h1 {
		margin: 0;
		font-size: 1.25rem;
	}
	.sub {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.filters {
		display: flex;
		gap: 8px;
		margin-bottom: 16px;
		flex-wrap: wrap;
	}
	.filter-chip {
		padding: 5px 12px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.82rem;
		cursor: pointer;
	}
	.filter-chip:hover {
		color: var(--text);
	}
	.filter-chip.active {
		background: var(--accent-strong);
		border-color: var(--accent-strong);
		color: #fff;
	}
	/* Table chrome (padding, borders, header, `.right`, `td.actions`) is
	   app.css's `.grid-container`, owned by DataTable. Cells top-align: the
	   tier list makes a row several lines tall. */
	td {
		vertical-align: top;
	}
	/* Stacked with `display: block`, not a flex <td>: a flex cell stops being
	   a table cell, so its border drifts out of line with the rest of the row. */
	.tier {
		display: block;
		font-size: 0.78rem;
		color: var(--text-muted);
		white-space: nowrap;
	}
	.window {
		white-space: nowrap;
		font-size: 0.82rem;
		color: var(--text-muted);
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
	.empty .hint {
		font-size: 0.82rem;
	}
	.error {
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
		color: var(--danger);
		padding: 10px 14px;
		border-radius: 4px;
		margin-bottom: 12px;
	}

	/* Accept dialog body. The shell (.backdrop / .modal / h2 / .modal-hint /
	   .modal-footer) and the .btn-primary / .btn-cancel buttons are app.css's. */
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 14px;
		font-size: 0.85rem;
	}
	.field span {
		color: var(--text-muted);
	}
	.preview {
		display: flex;
		align-items: baseline;
		gap: 8px;
		padding: 12px;
		background: var(--bg);
		border-radius: 4px;
		margin-bottom: 16px;
		flex-wrap: wrap;
	}
	.preview strong {
		font-size: 1.1rem;
	}
	.preview .muted {
		color: var(--text-muted);
		font-size: 0.8rem;
	}
</style>
