<script lang="ts">
	// Sanctions-screening review queue — the vendors flagged `match` / `review`
	// by the screening engine, awaiting a human decision. A separate sub-route
	// from the vendors LIST page (/vendors) so the two don't collide.
	//
	// Reviewer actions (block / unblock a vendor's payments) are gated on the
	// GRANULAR permission `vendor.block` via the auth store — NOT a role check —
	// so the control is hidden when the user lacks it (the backend enforces it
	// regardless). Re-screen is admin/ap_manager (auth.isManager). PII-safe: the
	// endpoints return names, categories/list-names, scores + statuses only.
	import {
		getScreeningReviewQueue,
		getScreeningHistory,
		getVendorCounts,
		blockVendor,
		unblockVendor,
		screenVendor
	} from '#lib/api/vendors.ts';
	import type { ScreeningReviewItem, SanctionsCheck } from '#lib/types/vendor.ts';
	import {
		screeningStatusLabelKey,
		riskLevelLabelKey,
		sanctionsResultLabelKey,
		screeningCategoryLabels
	} from '#lib/types/vendor.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import SearchBox from '#lib/components/ui/SearchBox.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import ScreeningBadge from '#lib/components/ui/ScreeningBadge.svelte';
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { formatList } from '#lib/utils/list.ts';
	import { untrack } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { PERM_VENDOR_BLOCK } from '#lib/types/admin.ts';

	// Reviewer capabilities — mirror the backend gates so the UI can't drift.
	// Block/unblock is the splittable granular permission; re-screen stays on
	// the admin/ap_manager role (backend `require_roles`).
	const canBlock = $derived(auth.can(PERM_VENDOR_BLOCK));

	// The screening verdict is a message key, not an English literal — an
	// unrecognised status renders raw rather than blank.
	const screeningLabel = (s: string) => {
		const key = screeningStatusLabelKey(s);
		return key ? m(key) : s;
	};

	const riskLabel = (level: string) => {
		const key = riskLevelLabelKey(level);
		return key ? m(key) : level;
	};

	// The per-screen verdict on the history timeline. Same tolerant shape as the
	// two above: a verdict this build has no wording for renders de-underscored
	// rather than blank — a dropped verdict is the one outcome a compliance
	// reviewer must never get.
	const resultLabel = (result: string) => {
		const key = sanctionsResultLabelKey(result);
		return key ? m(key) : result.replace(/_/g, ' ');
	};

	// Composed through a key rather than concatenated: Japanese parenthesises
	// with full-width （）, and `vendors.risk.pill` already established that a
	// translated level never gets an English wrapper glued onto it.
	const riskWithScore = (level: string, score: string | null) =>
		score
			? m('vendors.screening.queue.modal.riskWithScore', { level: riskLabel(level), score })
			: riskLabel(level);

	// Hit categories read as one phrase beside the (translated) verdict and risk
	// level, so they are resolved the same way: a known kind through its key, an
	// unrecognised one through its de-underscored raw label — never dropped,
	// which is the one outcome a compliance reviewer must not get.
	const formatCategories = (categories: string[] | null | undefined) => {
		const parts = screeningCategoryLabels(categories);
		if (!parts.length) return '—';
		return formatList(parts.map((p) => (p.key ? m(p.key) : p.fallback)));
	};
	const canRescreen = $derived(auth.isManager);

	const PAGE_SIZE = 20;

	let items = $state<ScreeningReviewItem[]>([]);
	let loading = $state(true);
	let loadingMore = $state(false);
	let loadError = $state(false);
	let search = $state('');
	// The term the newest ISSUED request carried, so the debounce effect's first
	// run (mount) doesn't fire a duplicate load behind the initial fetch.
	let appliedSearch = $state('');
	let page = $state(1);
	let total = $state(0);
	let hasMore = $derived(items.length < total);

	// Whole-set tallies from `GET /api/vendors/counts`. `null` means "not
	// available" (the call failed, or the caller is an ap_clerk — the screening
	// queue admits that role, the vendor list and therefore its tally do not).
	// We render an em-dash rather than falling back to a queue-derived number,
	// because that number is the bug this replaced.
	let blockedTotal = $state<number | null>(null);
	let matchCount = $state<number | null>(null);
	let reviewCount = $state<number | null>(null);

	// Detail modal — the selected vendor + its screening history.
	let selected = $state<ScreeningReviewItem | null>(null);
	let history = $state<SanctionsCheck[]>([]);
	// The vendor the loaded `history` actually describes. The timeline is the
	// evidence the Block/Unblock decision rests on, so it is rendered only while
	// this matches the open vendor — the sequencer below already discards a
	// superseded response, and this is the assertion that the two agree.
	let historyVendorId = $state<string | null>(null);
	let historyLoading = $state(false);
	// Distinct from "the timeline is empty": see the render block.
	let historyError = $state(false);
	// Keyed to the vendor the action was fired for, not a bare flag. Opening a
	// second vendor while a block is in flight used to leave ITS buttons
	// disabled, and the spinner label sat on a row the action never touched.
	let busy = $state<{ vendorId: string; action: 'block' | 'rescreen' } | null>(null);
	let blockReason = $state('');

	// What the OPEN vendor is busy with — `null` for an action running against
	// some other vendor, so this modal's controls stay live and honest.
	const selectedBusy = $derived(
		selected && busy && busy.vendorId === selected.vendor_id ? busy.action : null
	);
	const historyIsForSelected = $derived(
		selected !== null && historyVendorId === selected.vendor_id
	);

	// `$derived`, not a module constant: reading `m()` here is what makes the
	// headers re-render when the locale picker moves (the convention the sibling
	// `/vendors/change-requests` route established).
	// The first two REUSE the `/vendors` list's own column keys rather than
	// minting near-duplicates: it is the same column, and a second key would let
	// the two pages drift apart in translation (they already had — the French
	// list says "Contrôle").
	const COLUMNS = $derived([
		{ label: m('vendors.col.vendor') },
		{ label: m('vendors.col.screening') },
		{ label: m('vendors.screening.queue.col.matchedList') },
		{ label: m('vendors.screening.queue.col.provider') },
		{ label: m('vendors.screening.queue.col.riskScore') },
		{ label: m('vendors.screening.queue.col.lastScreened') },
		{ class: 'actions-col' }
	]);

	$effect(() => {
		loadQueue();
		loadCounts();
	});

	// Search is SERVER-side. It used to filter the loaded queue in memory, which
	// was survivable only while the endpoint returned every row; now that it
	// pages, an in-memory filter would silently hide every match on a later page
	// and report "no vendors match your search" (`frontend/CLAUDE.md` § Search).
	// The trade: the backend matches vendor name + any matched list on the
	// vendor's screening trail, not the formatted hit-category labels — those
	// are derived in Python from the provider payload and aren't a SQL column.
	$effect(() => {
		const term = search.trim();
		// Read `appliedSearch` untracked: it is written where a request is
		// ISSUED, so tracking it would re-run this effect on its own write.
		if (term === untrack(() => appliedSearch)) return;
		const timer = setTimeout(() => loadQueue(), 300);
		return () => clearTimeout(timer);
	});

	// Sequences `loadQueue`. The mount `$effect` is not its only trigger — the
	// header Refresh button fires it too, so two can be in flight at once and
	// resolve out of order. `applyVendorUpdate` rewrites a row in place with no
	// fetch of its own, so it retires whatever is in flight first: otherwise a
	// queue response issued before a block/unblock/re-screen lands afterwards
	// and puts the lifted payment block — or the stale screening verdict — back
	// on the row. See `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	// The blocked tally is its own request with its own sequence: it is refetched
	// after every block / unblock / re-screen, so two can be in flight and
	// resolve out of order exactly like the queue fetch can.
	const countsSequence = createRequestSequencer();

	// The modal's screening-history fetch gets a third sequencer — one per
	// independent request stream, never a shared counter
	// (`frontend/CLAUDE.md` § Sequencing list fetches). It is the one on this
	// page whose staleness is not merely cosmetic: open vendor A, close, open
	// vendor B, and A's late response landed in B's modal, so the reviewer read
	// A's sanctions timeline while Block/Unblock — bound to `selected` — acted
	// on B. Nothing edits the timeline in place, so it needs no
	// `supersedeInFlight()`.
	const historySequence = createRequestSequencer();

	/**
	 * The "Payments blocked" KPI, from a query that asks the KPI's own question.
	 *
	 * It used to be `items.filter((it) => it.payments_blocked).length` over the
	 * review queue — and the queue is selected on `screening_status IN
	 * ('match','review')`, while `POST /api/vendors/{id}/block` sets
	 * `payments_blocked` and never touches `screening_status`. A vendor AP
	 * blocked while screening-clear was therefore invisible to a headline
	 * claiming to count blocked payments, at any queue size. See
	 * `docs/decisions.md` §48 and `backend/docs/vendor-risk-screening.md`.
	 */
	async function loadCounts() {
		const token = countsSequence.start();
		try {
			const counts = await getVendorCounts();
			if (!countsSequence.canCommit(token)) return;
			blockedTotal = counts.payments_blocked;
			// `by_screening_status` omits a bucket with no rows, so a missing key
			// is a genuine zero — distinct from the `null` "we could not ask".
			matchCount = counts.by_screening_status?.match ?? 0;
			reviewCount = counts.by_screening_status?.review ?? 0;
		} catch {
			// Non-fatal, and deliberately NOT latched back to a queue-derived
			// tally: an em-dash says "we don't know", the old number said
			// something false — and with the queue paginated it would now be
			// false by construction, not just in the blocked-vendor edge case.
			if (!countsSequence.isCurrentRequest(token)) return;
			blockedTotal = null;
			matchCount = null;
			reviewCount = null;
		}
	}

	async function loadQueue(opts: { append?: boolean; nextPage?: number } = {}) {
		const token = fetchSequence.start();
		const targetPage = opts.nextPage ?? 1;
		// `untrack`: this runs from the mount effect AND the debounce effect, and
		// Svelte tracks reads transitively through called functions — a plain
		// read here would make the mount effect depend on `search` and fire an
		// un-debounced request on every keystroke (issue #168).
		const term = untrack(() => search).trim();
		appliedSearch = term;
		if (opts.append) loadingMore = true;
		else loading = true;
		loadError = false;
		try {
			const queue = await getScreeningReviewQueue({
				page: targetPage,
				page_size: PAGE_SIZE,
				search: term || undefined
			});
			// Superseded by a newer refresh, or by a local block/re-screen.
			if (!fetchSequence.canCommit(token)) return;
			// Same job as `utils/pagination.appendUnique` — offset pagination can
			// re-surface a row when a concurrent re-screen shifts the set, and a
			// duplicated key crashes the keyed `{#each}` — but that helper is
			// keyed on `id` and this row's key is `vendor_id`, so the dedupe is
			// spelled out here rather than widening the shared helper for one
			// caller.
			if (opts.append) {
				const seen = new Set(items.map((it) => it.vendor_id));
				items = [...items, ...queue.items.filter((it) => !seen.has(it.vendor_id))];
			} else {
				items = queue.items;
			}
			total = queue.total;
			page = targetPage;
		} catch {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// edit still failed, and no newer load is coming to report it.
			if (!fetchSequence.isCurrentRequest(token)) return;
			loadError = true;
			toast(m('vendors.screening.queue.toast.loadFailed'), 'error');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	async function loadMore() {
		await loadQueue({ append: true, nextPage: page + 1 });
	}

	function openDetail(item: ScreeningReviewItem) {
		selected = item;
		blockReason = '';
		history = [];
		historyVendorId = null;
		historyError = false;
		void loadHistory(item.vendor_id);
	}

	/** This vendor's screening trail. Always called with an explicit vendor id
	 *  rather than reading `selected`: `rescreen()` calls it after an await, by
	 *  which point `selected` may be a different vendor entirely. */
	async function loadHistory(vendorId: string) {
		const token = historySequence.start();
		historyLoading = true;
		historyError = false;
		try {
			const rows = await getScreeningHistory(vendorId);
			// Superseded by a newer open — discard rather than render one vendor's
			// sanctions trail under another vendor's name.
			if (!historySequence.canCommit(token)) return;
			history = rows;
			historyVendorId = vendorId;
		} catch {
			// `isCurrentRequest`, not `canCommit`: only the newest request reports.
			if (!historySequence.isCurrentRequest(token)) return;
			history = [];
			historyVendorId = vendorId;
			historyError = true;
			toast(m('vendors.screening.queue.toast.historyFailed'), 'error');
		} finally {
			if (historySequence.isCurrentRequest(token)) historyLoading = false;
		}
	}

	function closeDetail() {
		selected = null;
		history = [];
		historyVendorId = null;
		historyError = false;
		blockReason = '';
	}

	// Reflect a mutated vendor's block/screening state back into the row + the
	// open modal. The block/unblock/screen endpoints return the full Vendor.
	function applyVendorUpdate(vendor: {
		id: string;
		screening_status: ScreeningReviewItem['screening_status'];
		payments_blocked: boolean;
		last_screened_at: string | null;
		risk_level: ScreeningReviewItem['risk_level'];
		risk_score: string | null;
	}) {
		fetchSequence.supersedeInFlight();
		items = items.map((it) =>
			it.vendor_id === vendor.id
				? {
						...it,
						screening_status: vendor.screening_status,
						payments_blocked: vendor.payments_blocked,
						last_screened_at: vendor.last_screened_at,
						risk_level: vendor.risk_level,
						risk_score: vendor.risk_score
					}
				: it
		);
		if (selected && selected.vendor_id === vendor.id) {
			selected = {
				...selected,
				screening_status: vendor.screening_status,
				payments_blocked: vendor.payments_blocked,
				last_screened_at: vendor.last_screened_at,
				risk_level: vendor.risk_level,
				risk_score: vendor.risk_score
			};
		}
	}

	async function toggleBlock() {
		if (!selected) return;
		// Bind the action to the vendor that was on screen when it was fired.
		// Everything after the first await must read THIS, never `selected` —
		// the modal can be closed and re-opened on another vendor mid-flight.
		const target = selected;
		busy = { vendorId: target.vendor_id, action: 'block' };
		try {
			const updated = target.payments_blocked
				? await unblockVendor(target.vendor_id)
				: await blockVendor(target.vendor_id, blockReason.trim() || undefined);
			applyVendorUpdate(updated);
			// The tally is whole-set, so it can't be adjusted locally — a vendor
			// outside this queue may have been blocked meanwhile. Re-ask.
			loadCounts();
			toast(
				m(
					updated.payments_blocked
						? 'vendors.screening.queue.toast.blocked'
						: 'vendors.screening.queue.toast.unblocked'
				),
				'success'
			);
			// Only clear the field the user is actually looking at — a reason
			// typed for a vendor opened meanwhile is not this action's to wipe.
			if (selected?.vendor_id === target.vendor_id) blockReason = '';
		} catch (err) {
			const e = err as { detail?: string; message?: string } | null;
			toast(e?.detail ?? e?.message ?? m('vendors.screening.queue.toast.actionFailed'), 'error');
		} finally {
			if (busy?.vendorId === target.vendor_id) busy = null;
		}
	}

	async function rescreen() {
		if (!selected) return;
		const target = selected;
		busy = { vendorId: target.vendor_id, action: 'rescreen' };
		try {
			const updated = await screenVendor(target.vendor_id);
			applyVendorUpdate(updated);
			// A `match` verdict auto-blocks the vendor, so the tally can move.
			loadCounts();
			toast(m('vendors.screening.queue.toast.rescreened'), 'success');
			// Refresh the history so the new screen appears at the top — for the
			// vendor that was re-screened, through the sequencer, so it can't
			// overwrite the timeline of a vendor opened meanwhile.
			await loadHistory(target.vendor_id);
		} catch (err) {
			const e = err as { detail?: string; message?: string } | null;
			toast(e?.detail ?? e?.message ?? m('vendors.screening.queue.toast.rescreenFailed'), 'error');
		} finally {
			if (busy?.vendorId === target.vendor_id) busy = null;
		}
	}
</script>

<PageHeader title={m('vendors.screening.queue.title')}>
	{#snippet actions()}
		<button
			class="btn-outline"
			onclick={() => {
				loadQueue();
				loadCounts();
			}}
			disabled={loading}
		>
			{loading
				? m('vendors.screening.queue.refreshing')
				: m('vendors.screening.queue.refresh')}
		</button>
	{/snippet}

	<div class="kpi-row">
		<!-- All three figures are whole-set, from `GET /api/vendors/counts`. They
		     are NOT derived from the loaded queue: that only ever worked while
		     the queue returned every row, and it pages now. `null` = the call
		     failed, or the caller is an ap_clerk (whom `/counts` 403s) — an
		     em-dash says "we don't know" instead of a page-scoped undercount. -->
		<KpiCard
			value={matchCount ?? '—'}
			label={m('vendors.screening.queue.kpi.matches')}
			highlight={matchCount !== null && matchCount > 0 ? 'red' : null}
			sub={matchCount === null ? m('vendors.screening.queue.kpi.unavailable') : null}
		/>
		<KpiCard
			value={reviewCount ?? '—'}
			label={m('vendors.screening.queue.kpi.needsReview')}
			highlight={reviewCount !== null && reviewCount > 0 ? 'red' : null}
			sub={reviewCount === null ? m('vendors.screening.queue.kpi.unavailable') : null}
		/>
		<KpiCard
			value={blockedTotal ?? '—'}
			label={m('vendors.screening.queue.kpi.blocked')}
			sub={blockedTotal === null
				? m('vendors.screening.queue.kpi.unavailable')
				: m('vendors.screening.queue.kpi.blockedScope')}
		/>
	</div>

	<div class="filter-row">
		<SearchBox
			bind:value={search}
			placeholder={m('vendors.screening.queue.search.placeholder')}
			ariaLabel={m('vendors.screening.queue.search.aria')}
		/>
	</div>

	<DataTable
		columns={COLUMNS}
		isEmpty={items.length === 0}
		empty={loading
			? m('common.loading')
			: loadError
				? m('vendors.screening.queue.empty.errored')
				: search.trim()
					? m('vendors.screening.queue.empty.filtered')
					: m('vendors.screening.queue.empty.clean')}
	>
		{#snippet body()}
			{#each items as it (it.vendor_id)}
				<tr
					class="clickable"
					onclick={(e) => {
						if (isRowOpenClick(e)) openDetail(it);
					}}
				>
					<td class="vendor-name">
						<RowLink
							onclick={() => openDetail(it)}
							ariaLabel={m('vendors.screening.queue.row.open', { vendor: it.vendor_name })}
						>
							{it.vendor_name}
						</RowLink>
					</td>
					<td>
						<ScreeningBadge
							screening={it.screening_status}
							risk={it.risk_level}
							blocked={it.payments_blocked}
							adverseMedia={it.adverse_media}
						/>
					</td>
					<td class="muted">{it.latest_matched_list ?? '—'}</td>
					<td class="muted">{it.latest_provider ?? '—'}</td>
					<td class="mono">{it.risk_score ?? '—'}</td>
					<td class="muted">{formatDate(it.last_screened_at)}</td>
					<td class="actions">
						<RowAction onclick={() => openDetail(it)}>
							{m('vendors.screening.queue.row.review')}
						</RowAction>
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>

	<!-- `showingAll` is rendered ONLY behind `total > 0`: `total` is the server's
	     whole-set count, so stating it while a later page is unfetched would be
	     the same claim the KPIs used to make wrongly. -->
	{#if hasMore}
		<div class="load-more-row">
			<button class="btn-load-more" onclick={loadMore} disabled={loadingMore || loading}>
				{loadingMore
					? m('common.loading')
					: m('vendors.loadMore', { shown: items.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('vendors.showingAll', { total })}</span>
		</div>
	{/if}
</PageHeader>

<Modal
	open={selected !== null}
	ariaLabel={m('vendors.screening.queue.modal.aria')}
	width="md"
	onclose={closeDetail}
>
	{#if selected}
		<h2>{selected.vendor_name}</h2>

		<div class="badge-row">
			<ScreeningBadge
				screening={selected.screening_status}
				risk={selected.risk_level}
				blocked={selected.payments_blocked}
				adverseMedia={selected.adverse_media}
			/>
		</div>

		<dl class="meta">
			<div>
				<dt>{m('vendors.screening.queue.modal.screeningStatus')}</dt>
				<dd>{screeningLabel(selected.screening_status)}</dd>
			</div>
			<div>
				<dt>{m('vendors.screening.queue.modal.riskLevel')}</dt>
				<dd>{riskWithScore(selected.risk_level, selected.risk_score)}</dd>
			</div>
			<!-- Three of these `<dt>`s label the same field as a table column
			     above, so they read the column's key rather than a duplicate. -->
			<div>
				<dt>{m('vendors.screening.queue.col.matchedList')}</dt>
				<dd>{selected.latest_matched_list ?? '—'}</dd>
			</div>
			<div>
				<dt>{m('vendors.screening.queue.modal.categories')}</dt>
				<dd>{formatCategories(selected.latest_categories)}</dd>
			</div>
			<div>
				<dt>{m('vendors.screening.queue.col.provider')}</dt>
				<dd>{selected.latest_provider ?? '—'}</dd>
			</div>
			<div>
				<dt>{m('vendors.screening.queue.col.lastScreened')}</dt>
				<dd>{formatDate(selected.last_screened_at)}</dd>
			</div>
			<div>
				<dt>{m('vendors.screening.queue.modal.payments')}</dt>
				<dd>
					{selected.payments_blocked
						? m('vendors.screening.queue.modal.paymentsBlocked')
						: m('vendors.screening.queue.modal.paymentsAllowed')}
				</dd>
			</div>
		</dl>

		{#if canBlock || canRescreen}
			<!-- `data-vendor-id`: the identity these controls act on, asserted by
			     `tests-e2e/vendors/screening-modal-identity.spec.ts`. -->
			<div class="review-actions" data-testid="screening-actions" data-vendor-id={selected.vendor_id}>
				{#if canRescreen}
					<button class="btn-outline" onclick={rescreen} disabled={selectedBusy !== null}>
						{selectedBusy === 'rescreen'
							? m('vendors.screening.queue.action.rescreening')
							: m('vendors.screening.queue.action.rescreen')}
					</button>
				{/if}
				{#if canBlock}
					{#if !selected.payments_blocked}
						<input
							class="reason-input"
							type="text"
							maxlength="255"
							placeholder={m('vendors.screening.queue.reason.placeholder')}
							aria-label={m('vendors.screening.queue.reason.aria')}
							bind:value={blockReason}
						/>
					{/if}
					<button
						class="btn-block"
						class:unblock={selected.payments_blocked}
						onclick={toggleBlock}
						disabled={selectedBusy !== null}
					>
						{#if selectedBusy === 'block'}
							{m('vendors.screening.queue.action.working')}
						{:else}
							{selected.payments_blocked
								? m('vendors.screening.queue.action.unblock')
								: m('vendors.screening.queue.action.block')}
						{/if}
					</button>
				{/if}
			</div>
		{:else}
			<p class="no-perm-note">{m('vendors.screening.queue.noPermissionNote')}</p>
		{/if}

		<h3>{m('vendors.screening.queue.history.title')}</h3>
		<!-- Three states, not two, and the order is load-bearing. "No screening
		     history yet." is a claim that this vendor has never been screened,
		     sitting directly above the Block/Unblock control whose decision rests
		     on that timeline — rendering it on a FAILED fetch tells the reviewer
		     there is nothing to see when the truth is that we could not look.
		     The same rule `/exceptions` states verbatim. The identity guard
		     (`historyIsForSelected`) resolves to the loading state, never to the
		     empty claim, so a timeline in flight for the open vendor can't be
		     read as an answer about it either. -->
		{#if historyError}
			<p class="history-error" role="alert" data-testid="screening-history-error">
				{m('vendors.screening.queue.history.error')}
				<button
					type="button"
					class="btn-retry"
					onclick={() => selected && loadHistory(selected.vendor_id)}
				>
					{m('vendors.screening.queue.history.retry')}
				</button>
			</p>
		{:else if historyLoading || !historyIsForSelected}
			<p class="muted" data-testid="screening-history-loading">{m('common.loading')}</p>
		{:else if history.length === 0}
			<p class="muted" data-testid="screening-history-empty">
				{m('vendors.screening.queue.history.empty')}
			</p>
		{:else}
			<ul class="history" data-testid="screening-history" data-vendor-id={historyVendorId}>
				{#each history as h (h.id)}
					<li>
						<span class="history-result {h.result}">{resultLabel(h.result)}</span>
						<span class="history-meta">
							{h.check_type} · {h.provider}
							{#if h.matched_list}· {h.matched_list}{/if}
							{#if h.categories.length}· {formatCategories(h.categories)}{/if}
							{#if h.risk_score}· {m('vendors.screening.queue.history.score', { score: h.risk_score })}{/if}
						</span>
						<span class="history-date">{formatDate(h.checked_at, '—', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' })}</span>
					</li>
				{/each}
			</ul>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={closeDetail}>
				{m('vendors.screening.queue.close')}
			</button>
		</div>
	{/if}
</Modal>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */
	.btn-outline {
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
		opacity: 0.5;
		cursor: not-allowed;
	}

	.vendor-name {
		font-weight: 500;
	}

	/* --- Detail modal --- */
	.badge-row {
		display: flex;
		gap: 6px;
		flex-wrap: wrap;
		margin: 4px 0 14px;
	}
	.meta {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 10px 24px;
		margin: 0 0 16px;
	}
	.meta div {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.meta dt {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	.meta dd {
		margin: 0;
		font-size: 0.9rem;
		color: var(--text);
	}

	.review-actions {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
		padding: 12px 0;
		border-top: 1px solid var(--border);
		border-bottom: 1px solid var(--border);
		margin-bottom: 14px;
	}
	.reason-input {
		flex: 1;
		min-width: 160px;
		padding: 7px 10px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.85rem;
	}
	.reason-input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}
	.btn-block {
		padding: 8px 16px;
		border-radius: 6px;
		border: 1px solid #f06464;
		background: rgba(224, 64, 64, 0.12);
		color: #f06464;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}
	.btn-block:hover:not(:disabled) {
		background: rgba(224, 64, 64, 0.2);
	}
	.btn-block.unblock {
		border-color: #1fa86a;
		background: rgba(31, 168, 106, 0.12);
		color: #1fa86a;
	}
	.btn-block.unblock:hover:not(:disabled) {
		background: rgba(31, 168, 106, 0.2);
	}
	.btn-block:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.no-perm-note {
		font-size: 0.82rem;
		color: var(--text-muted);
		font-style: italic;
		padding: 12px 0;
		border-top: 1px solid var(--border);
		border-bottom: 1px solid var(--border);
		margin-bottom: 14px;
	}

	/* --- History timeline --- */
	h3 {
		font-size: 0.95rem;
		margin: 0 0 8px;
	}
	.history-error {
		display: flex;
		align-items: baseline;
		gap: 8px;
		flex-wrap: wrap;
		font-size: 0.85rem;
		color: var(--danger);
		margin: 0 0 8px;
	}
	.btn-retry {
		border: none;
		background: none;
		padding: 0;
		font: inherit;
		color: var(--accent-strong);
		text-decoration: underline;
		cursor: pointer;
	}
	.history {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-height: 260px;
		overflow-y: auto;
	}
	.history li {
		display: flex;
		align-items: baseline;
		gap: 10px;
		flex-wrap: wrap;
		padding-bottom: 8px;
		border-bottom: 1px solid var(--border);
	}
	.history-result {
		font-size: 0.72rem;
		font-weight: 600;
		/* No `text-transform: capitalize` — it existed to dress up the raw
		   `review_required` this badge used to print, and the label is properly
		   cased at the source now. Left in, it would title-case a translated
		   phrase mid-sentence ("Prüfung Erforderlich"). */
		padding: 2px 8px;
		border-radius: 10px;
		background: var(--bg);
		color: var(--text-muted);
		white-space: nowrap;
	}
	.history-result.match {
		background: rgba(224, 64, 64, 0.12);
		color: #f06464;
	}
	.history-result.review_required {
		background: rgba(212, 148, 10, 0.12);
		color: #d4940a;
	}
	.history-result.clear {
		background: rgba(31, 168, 106, 0.12);
		color: #1fa86a;
	}
	.history-meta {
		font-size: 0.8rem;
		color: var(--text-muted);
		flex: 1;
	}
	.history-date {
		font-size: 0.76rem;
		color: var(--text-muted);
		white-space: nowrap;
	}
</style>
