<script lang="ts">
	import { onMount } from 'svelte';
	import { focusTrap } from '#lib/actions/focusTrap.ts';
	import Money from '#lib/components/ui/Money.svelte';
	import { formatDate } from '#lib/utils/time.ts';
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

	let items = $state<PortalDiscountOffer[]>([]);
	// Server-side count of the whole FILTERED offer set, not the loaded page —
	// an offer past the first page still expires, so it has to be reachable.
	let total = $state(0);
	let pageNum = $state(1);
	let loading = $state(false);
	let loadingMore = $state(false);
	let error = $state('');
	let activeFilter = $state('');
	let busy = $state<string | null>(null);
	// The offer whose tier the vendor is choosing in the accept dialog.
	let accepting = $state<PortalDiscountOffer | null>(null);
	let chosenTierDays = $state<number | null>(null);

	// Freeze background page scroll while the accept dialog is open (restored on
	// close), so a wheel event over the backdrop can't scroll the offers behind it.
	$effect(() => {
		if (!accepting) return;
		const prev = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		return () => {
			document.body.style.overflow = prev;
		};
	});

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
			error = err instanceof Error ? err.message : m('portal.discounts.loadFailed');
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
		refresh();
	}

	function openAccept(offer: PortalDiscountOffer) {
		accepting = offer;
		// Default to the best capturable tier today; fall back to the first tier.
		chosenTierDays = offer.best_tier?.days ?? offer.tiers[0]?.days ?? null;
	}

	function closeAccept() {
		accepting = null;
		chosenTierDays = null;
	}

	async function confirmAccept() {
		if (!accepting) return;
		const offer = accepting;
		busy = offer.id;
		error = '';
		try {
			await acceptPortalDiscountOffer(offer.id, chosenTierDays ?? undefined);
			closeAccept();
			await refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.discounts.acceptFailed');
		} finally {
			busy = null;
		}
	}

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
		}
	}

	function fmtPct(n: number): string {
		return `${n}%`;
	}

	// The chosen tier object for the accept dialog (for the live savings preview).
	const chosenTier = $derived(
		accepting && chosenTierDays !== null
			? (accepting.tiers.find((t) => t.days === chosenTierDays) ?? null)
			: null,
	);

	onMount(refresh);
</script>

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
	{:else if !items.length}
		<div class="empty">
			<p>{m('portal.discounts.empty')}</p>
			<p class="hint">{m('portal.discounts.emptyHint')}</p>
		</div>
	{:else}
		<table>
			<thead>
				<tr>
					<th>{m('portal.discounts.col.appliesTo')}</th>
					<th class="num">{m('portal.discounts.col.amount')}</th>
					<th>{m('portal.discounts.col.tiers')}</th>
					<th class="num">{m('portal.discounts.col.bestDiscount')}</th>
					<th class="num">{m('portal.discounts.col.youSave')}</th>
					<th>{m('portal.discounts.col.window')}</th>
					<th>{m('portal.discounts.col.status')}</th>
					<th class="actions-col"></th>
				</tr>
			</thead>
			<tbody>
				{#each items as o (o.id)}
					<tr>
						<td>
							{#if o.scope === 'invoice'}
								{m('portal.discounts.scopeInvoice', { number: o.invoice_number || m('portal.common.dash') })}
							{:else}
								{m('portal.discounts.scopeAll')}
							{/if}
						</td>
						<td class="num"><Money amount={o.base_amount} currency={o.currency} /></td>
						<td class="tiers">
							{#each o.tiers as t (t.days)}
								<span class="tier">{m('portal.discounts.tier', { days: t.days, percent: fmtPct(t.percent) })}</span>
							{/each}
						</td>
						<td class="num">
							{o.best_tier ? fmtPct(o.best_tier.percent) : m('portal.common.dash')}
						</td>
						<td class="num">
							{#if o.status === 'captured' && o.captured_amount !== null}
								<Money amount={o.captured_amount} currency={o.currency} />
							{:else if o.accepted_tier}
								<Money amount={o.accepted_tier.savings} currency={o.currency} />
							{:else if o.best_tier}
								<Money amount={o.best_tier.savings} currency={o.currency} />
							{:else}
								—
							{/if}
						</td>
						<td class="window">{formatDate(o.valid_from, m('portal.common.dash'))} – {formatDate(o.valid_until, m('portal.common.dash'))}</td>
						<td><span class="status s-{o.status}">{o.status}</span></td>
						<td class="actions">
							{#if o.status === 'offered'}
								<button
									type="button"
									class="act accept"
									disabled={busy === o.id || !o.best_tier}
									onclick={() => openAccept(o)}
								>
									{m('portal.discounts.accept')}
								</button>
								<button
									type="button"
									class="act"
									disabled={busy === o.id}
									onclick={() => decline(o)}
								>
									{m('portal.discounts.decline')}
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

{#if accepting}
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div
		class="backdrop"
		role="presentation"
		onclick={(e) => {
			if (e.target === e.currentTarget) closeAccept();
		}}
	>
		<!-- focusTrap handles Esc + focus trap/restore. -->
		<div
			use:focusTrap={{ onEscape: closeAccept }}
			class="dialog"
			role="dialog"
			tabindex="-1"
			aria-modal="true"
			aria-label={m('portal.discounts.dialog.title')}
		>
			<h2>{m('portal.discounts.dialog.title')}</h2>
			<p class="dlg-sub">{m('portal.discounts.dialog.subtitle')}</p>

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

			<div class="dlg-footer">
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
		</div>
	</div>
{/if}

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
		vertical-align: top;
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
	.tiers {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.tier {
		font-size: 0.78rem;
		color: var(--text-muted);
		white-space: nowrap;
	}
	.window {
		white-space: nowrap;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 6px;
		white-space: nowrap;
	}
	.act {
		padding: 4px 12px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: transparent;
		color: var(--text);
		font-size: 0.8rem;
		cursor: pointer;
	}
	.act:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.act.accept:hover:not(:disabled) {
		border-color: rgba(40, 160, 80, 0.6);
		color: #2aa050;
	}
	.act:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.status {
		padding: 2px 8px;
		border-radius: 3px;
		font-size: 0.75rem;
		background: var(--bg);
		border: 1px solid var(--border);
		text-transform: capitalize;
	}
	.s-accepted,
	.s-captured {
		background: rgba(40, 160, 80, 0.15);
		border-color: rgba(40, 160, 80, 0.4);
	}
	.s-declined,
	.s-expired {
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
	.error {
		background: rgba(224, 64, 64, 0.1);
		border: 1px solid rgba(224, 64, 64, 0.3);
		color: var(--danger);
		padding: 10px 14px;
		border-radius: 4px;
		margin-bottom: 12px;
	}

	/* Accept dialog — the portal has no AP shared Modal, so it carries its own
	   small backdrop+dialog (matching the portal's self-contained styling). */
	.backdrop {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.5);
		display: grid;
		place-items: center;
		z-index: 50;
	}
	.dialog {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 20px 22px;
		width: 440px;
		max-width: calc(100vw - 32px);
	}
	.dialog h2 {
		margin: 0 0 6px;
		font-size: 1.05rem;
	}
	.dlg-sub {
		margin: 0 0 14px;
		color: var(--text-muted);
		font-size: 0.83rem;
	}
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
	.dlg-footer {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}
	.btn-cancel {
		padding: 7px 14px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: transparent;
		color: var(--text);
		cursor: pointer;
		font-size: 0.85rem;
	}
	.btn-primary {
		padding: 7px 14px;
		border: 1px solid var(--accent-strong);
		border-radius: 4px;
		background: var(--accent-strong);
		color: #fff;
		cursor: pointer;
		font-size: 0.85rem;
	}
	.btn-primary:disabled {
		opacity: 0.6;
		cursor: default;
	}
</style>
