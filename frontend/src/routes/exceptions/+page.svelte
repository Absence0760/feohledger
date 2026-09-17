<script lang="ts">
	import { page as pageStore } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import { api } from '$lib/api';
	import { appendUnique } from '$lib/utils/pagination';
	import type { MatchingIdsResponse } from '$lib/utils/pagination';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import BulkBar from '$lib/components/ui/BulkBar.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Tabs from '$lib/components/ui/Tabs.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import {
		exceptionStatusLabelKey,
		exceptionStatusTone,
		exceptionTypeFallback,
		exceptionTypeLabelKey
	} from '$lib/types/exception';
	import AgentDashboard from '$lib/components/exceptions/AgentDashboard.svelte';
	import { formatMoney } from '$lib/utils/money';
	import { timeAgo } from '$lib/utils/time';
	import { pruneSelection } from '$lib/utils/selection';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import { m } from '$lib/i18n/store.svelte';

	interface ExceptionItem {
		id: string;
		invoice_id: string | null;
		invoice_number: string | null;
		vendor_name: string | null;
		amount: number | null;
		// What `amount` is denominated in — the joined invoice's own code, not the
		// org's reporting currency (a GBP-reporting tenant holds USD invoices, so
		// labelling the row with the rollup code is a different wrong answer).
		// Null exactly when `amount` is: both are gated on the invoice having been
		// joined. See `docs/decisions.md` §160.
		currency: string | null;
		exception_type: string;
		type_label: string;
		severity: string;
		description: string | null;
		status: string;
		resolution: string | null;
		resolved_by: string | null;
		resolved_at: string | null;
		assigned_to: string | null;
		assigned_to_user_id: string | null;
		due_at: string | null;
		is_overdue: boolean;
		time_to_resolution_hours: number | null;
		created_at: string;
	}

	interface Summary {
		open: number;
		escalated: number;
		resolved: number;
		dismissed: number;
		by_type: Record<string, number>;
	}

	type Action = 'resolve' | 'escalate' | 'dismiss';

	const PAGE_SIZE = 20;
	let exceptions = $state<ExceptionItem[]>([]);
	let total = $state(0);
	let page = $state(1);
	let loadingMore = $state(false);
	// The initial / filter-change fetch has its own state. Without it the table
	// rendered the "No open exceptions. Everything looks good!" empty message
	// while the request was still in flight — and PERMANENTLY after a failed one,
	// asserting there were no open duplicates, fraud flags, compliance holds or
	// line-total mismatches when we simply hadn't been able to look.
	let loading = $state(true);
	let errored = $state(false);
	let summary = $state<Summary | null>(null);
	let statusFilter = $state('open');
	let typeFilter = $state<string | null>(null);
	let selectedIds = $state<Set<string>>(new Set());
	// True once "Select all N matching" (below) has resolved the WHOLE
	// filtered set of open/escalated exceptions — not just the loaded page —
	// into `selectedIds`. See the identical mechanism on the invoices list
	// page (`routes/invoices/+page.svelte`) for the full rationale. Reset by
	// `loadExceptions` whenever `statusFilter`/`typeFilter` actually change.
	let selectedAllMatching = $state(false);
	let selectingAllMatching = $state(false);

	let hasMore = $derived(exceptions.length < total);

	// Top-level view: the operational queue vs the AI-agent dashboard. Persisted
	// in the URL (?view=agents) so a refresh / shared link keeps the tab — this
	// used to be only a comment with no actual sync, so the tab reset to Queue
	// on every reload.
	let view = $state<'queue' | 'agents'>(
		$pageStore.url.searchParams.get('view') === 'agents' ? 'agents' : 'queue'
	);

	function syncViewToUrl(key: string) {
		const url = new URL($pageStore.url);
		if (key === 'agents') url.searchParams.set('view', 'agents');
		else url.searchParams.delete('view');
		replaceState(`${url.pathname}${url.search}`, {});
	}

	let resolveTarget = $state<ExceptionItem | null>(null); // single-row resolve modal
	let bulkResolveOpen = $state(false);                    // bulk-resolve modal
	let resolutionText = $state('');
	let saving = $state(false);

	// Badge text sits on a 12%-tint-of-itself background, so the tone must
	// clear WCAG 1.4.3 (4.5:1) on that tint — #e04040 (3.68) and #8b5cf6
	// (3.60) fail; their lighter siblings #f06464 (4.76) / #a78bfa (5.27)
	// pass. Amber #d4940a (5.51) already passes.
	// Must cover the backend roster (`services/exception_lifecycle.EXCEPTION_TYPES`);
	// a missing key falls back to grey, which reads as "unclassified" next to
	// every colour-coded sibling — worst on the types that BLOCK a payment run.
	// Reuse a tone already in this map rather than introducing one: each was
	// picked against the contrast rule above.
	const TYPE_COLORS: Record<string, string> = {
		duplicate: '#a78bfa',
		po_mismatch: '#d4940a',
		fraud_flag: '#f06464',
		extraction_failed: '#f06464',
		unverified_vendor: '#d4940a',
		review_rejected: '#f06464',
		amount_exceeded: '#f06464',
		missing_data: '#d4940a',
		quality_hold: '#f06464',
		price_variance: '#d4940a',
		erp_reconciliation: '#f06464',
		contract_noncompliant: '#f06464',
		line_total_mismatch: '#f06464',
		payment_compliance_hold: '#f06464',
	};

	const SEVERITY_COLORS: Record<string, string> = {
		error: '#f06464',
		warning: '#d4940a',
		info: '#638cff',
	};

	/**
	 * A status' label. The badge printed the raw wire value (`open`,
	 * `escalated`) in a row whose every other translated cell sat beside a
	 * filter chip already reading `Offen` — the same status, named twice, a
	 * few pixels apart. It reads the chips' OWN keys, so the two cannot drift.
	 * An unrecognised status still prints raw rather than blank.
	 *
	 * The tone map moved to `$lib/types/exception` alongside those keys, so a
	 * status that gains a colour without a label is a compile error.
	 */
	function statusLabel(status: string): string {
		const key = exceptionStatusLabelKey(status);
		return key ? m(key) : status;
	}

	/**
	 * An exception type's label, for the row badge AND the type-filter chips.
	 *
	 * Those two disagreed in English on this page: the chip derived its text as
	 * `exception_type.replace(/_/g, ' ')` (`po mismatch`) while the rows it
	 * filters carried the server's `type_label` (`PO Mismatch`) — §149's defect,
	 * and the same de-underscored derivation `EXCEPTION_TYPE_LABEL_KEYS` was
	 * added to remove from the agent decision log. Both read that map now, so
	 * the chip cannot name a type differently from the rows behind it.
	 *
	 * The chip has no `type_label` to fall back on (`summary.by_type` is keyed by
	 * the raw type), which is why the fallback is the module's own rather than a
	 * required argument.
	 */
	function typeLabel(type: string, serverLabel?: string | null): string {
		const key = exceptionTypeLabelKey(type);
		if (key) return m(key);
		return serverLabel || exceptionTypeFallback(type);
	}

	// Two INDEPENDENT request streams — the queue itself and the chip-count
	// summary — so each gets its own sequencer. One shared counter would let a
	// summary refresh mark the queue's in-flight response un-committable and
	// leave the table on the previous filter's rows. Every mutation here
	// (resolve, bulk resolve) re-fetches through these loaders instead of
	// editing a row in place, so neither needs `supersedeInFlight()`. See
	// `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();
	const summarySequence = createRequestSequencer();

	$effect(() => {
		statusFilter;
		typeFilter;
		// The "select all N matching" set was resolved against the FILTERS
		// active when it was clicked; once they change it no longer describes
		// anything real, so drop out of matching mode. Must happen here,
		// synchronously, before `loadExceptions()` — see the identical note on
		// the invoices list page.
		selectedAllMatching = false;
		loadExceptions();
	});

	$effect(() => {
		orgCurrency.ensureLoaded();
		loadSummary();
	});

	async function loadExceptions(opts: { append?: boolean; nextPage?: number } = {}) {
		const nextPage = opts.nextPage ?? 1;
		const token = fetchSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		errored = false;
		try {
			const params = new URLSearchParams();
			if (statusFilter !== 'all') params.set('status', statusFilter);
			if (typeFilter) params.set('type', typeFilter);
			params.set('page', String(nextPage));
			params.set('page_size', String(PAGE_SIZE));
			const data = await api.get<{ items: ExceptionItem[]; total: number }>(
				`/api/exceptions?${params}`
			);
			// Superseded by a newer load — discard rather than clobber. Load
			// more, then switch the status chip: the page-1 replace landed
			// first, then this append pushed the OLD filter's page-2 rows onto
			// the new list and overwrote `total`/`page` with them.
			if (!fetchSequence.canCommit(token)) return;
			exceptions = opts.append ? appendUnique(exceptions, data.items) : data.items;
			total = data.total;
			page = nextPage;
		} catch {
			// `isCurrentRequest`, not `canCommit`: a superseded request's failure
			// is not this table's news — the newer one owns the error state, and
			// blanking the rows here would discard what it is about to publish.
			if (!fetchSequence.isCurrentRequest(token)) return;
			errored = true;
			if (!opts.append) exceptions = [];
			toast('Failed to load exceptions', 'error');
		} finally {
			// Flags and selection belong to the newest request only: a stale
			// response used to clear the spinner while the live fetch was still
			// out.
			if (fetchSequence.isCurrentRequest(token)) {
				loadingMore = false;
				loading = false;
			}
			// Prune on BOTH paths, against the rows the bulk action can actually
			// act on. The catch empties the table, and it used to leave the
			// selection behind it — the bulk bar counted ids over zero visible
			// rows and Resolve would still POST them. Scoping to `selectableIds`
			// (open / escalated) rather than every loaded row also drops a
			// selection whose exception someone else resolved between loads.
			// Unconditional: it reads the rows currently on screen, so it is
			// correct for a discarded response too.
			//
			// EXCEPT while `selectedAllMatching` is true: that selection
			// deliberately spans ids beyond the loaded page (or beyond the
			// loaded page at load-more time), and pruning to `selectableIds`
			// (derived from the loaded rows only) would silently narrow it back
			// down to the exact bug "select all N matching" exists to fix.
			if (!selectedAllMatching) {
				selectedIds = pruneSelection(selectedIds, selectableIds);
			}
		}
	}

	async function loadMoreExceptions() {
		await loadExceptions({ append: true, nextPage: page + 1 });
	}

	async function loadSummary() {
		const token = summarySequence.start();
		try {
			const data = await api.get<Summary>('/api/exceptions/summary');
			// The chip counts drive the filter UI — an older summary landing
			// last would relabel the chips with pre-resolve tallies.
			if (!summarySequence.canCommit(token)) return;
			summary = data;
		} catch {
			/* non-critical */
		}
	}

	function openResolve(exc: ExceptionItem) {
		resolveTarget = exc;
		resolutionText = '';
	}

	function openBulkResolve() {
		bulkResolveOpen = true;
		resolutionText = '';
	}

	async function commitResolve(action: Action) {
		if (!resolveTarget) return;
		const note = resolutionText.trim();
		if (!note && action !== 'dismiss') {
			toast('Resolution note is required', 'error');
			return;
		}
		saving = true;
		try {
			await api.post(`/api/exceptions/${resolveTarget.id}/resolve`, {
				resolution: note || `${action}d by user`,
				action,
			});
			toast(`Exception ${action}d`, 'success');
			resolveTarget = null;
			resolutionText = '';
			await Promise.all([loadExceptions(), loadSummary()]);
		} catch (err) {
			toast(extractError(err), 'error');
		} finally {
			saving = false;
		}
	}

	async function commitBulkResolve(action: Action) {
		const ids = [...selectedIds];
		if (ids.length === 0) return;
		const note = resolutionText.trim();
		if (!note && action !== 'dismiss') {
			toast('Resolution note is required', 'error');
			return;
		}
		saving = true;
		try {
			const body = await api.post<{ updated: number; skipped: { id: string; reason: string }[] }>(
				'/api/exceptions/bulk/resolve',
				{ ids, action, resolution: note || `bulk ${action}` }
			);
			const skipped = body.skipped.length;
			// A segregation refusal is a per-row `skipped` reason, exactly like
			// `already_resolved` and `not_found` (the endpoint must never 409 a
			// whole batch over one refused row). But folding it into a bare
			// "N skipped" count leaves the operator unable to tell a row that was
			// already closed from one they are personally barred from clearing —
			// and only the second has something to do about it.
			const refused = body.skipped.filter((row) =>
				row.reason.startsWith('segregation_')
			).length;
			toast(
				[
					skipped === 0
						? `${body.updated} ${action}d`
						: `${body.updated} ${action}d, ${skipped} skipped`,
					refused > 0 ? m('exceptions.bulk.segregationSkipped', { n: refused }) : ''
				]
					.filter(Boolean)
					.join(' '),
				skipped === 0 ? 'success' : 'info'
			);
			bulkResolveOpen = false;
			resolutionText = '';
			selectedIds = new Set();
			selectedAllMatching = false;
			await Promise.all([loadExceptions(), loadSummary()]);
		} catch (err) {
			toast(extractError(err), 'error');
		} finally {
			saving = false;
		}
	}

	function extractError(err: unknown): string {
		const e = err as { detail?: string; message?: string } | null;
		return e?.detail ?? e?.message ?? 'Action failed';
	}

	function toggleSelect(id: string) {
		const next = new Set(selectedIds);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selectedIds = next;
	}

	let selectableIds = $derived(
		new Set(
			exceptions
				.filter((e) => e.status === 'open' || e.status === 'escalated')
				.map((e) => e.id)
		)
	);

	let allSelected = $derived(
		selectableIds.size > 0 && [...selectableIds].every((id) => selectedIds.has(id))
	);

	function toggleSelectAll() {
		if (allSelected) {
			selectedIds = new Set();
			selectedAllMatching = false;
		} else {
			selectedIds = new Set(selectableIds);
		}
	}

	// Resolve and select EVERY open/escalated exception matching the current
	// queue filters (not just the loaded page) via `GET /api/exceptions/ids`.
	// The header checkbox above only ever covers the exceptions fetched so
	// far — see the identical mechanism (and full rationale) on the invoices
	// list page's `selectAllMatching`.
	async function selectAllMatching() {
		selectingAllMatching = true;
		try {
			const params = new URLSearchParams();
			// Bulk resolve only ever acts on open/escalated rows — mirrors
			// `selectableIds`. `statusFilter === 'all'` has no narrower value to
			// reuse, so it's spelled out explicitly.
			params.set('status', statusFilter === 'all' ? 'open,escalated' : statusFilter);
			if (typeFilter) params.set('type', typeFilter);
			const res = await api.get<MatchingIdsResponse>(`/api/exceptions/ids?${params}`);
			selectedIds = new Set(res.ids);
			selectedAllMatching = true;
			if (res.truncated) {
				toast(
					`Selected the first ${res.ids.length} of ${res.total} matching — narrow your filters to select the rest.`,
					'error'
				);
			} else {
				toast(`Selected all ${res.ids.length} matching exception(s)`, 'success');
			}
		} catch {
			toast('Failed to select all matching', 'error');
		} finally {
			selectingAllMatching = false;
		}
	}

	// The row's own currency, never the org's. `formatMoney` returns its `—`
	// placeholder for a null amount before it looks at the code, and the payload
	// nulls both together, so the unprovable-currency case this cannot label is
	// exactly the case with no figure to label.
	function formatCurrency(n: number | null, currency: string | null): string {
		return formatMoney(n, { currency: currency ?? undefined });
	}

	function dueLabel(exc: ExceptionItem): string {
		if (!exc.due_at) return '—';
		const diff = new Date(exc.due_at).getTime() - Date.now();
		const hours = Math.round(diff / 3600000);
		if (hours <= 0) return `${Math.abs(hours)}h overdue`;
		if (hours < 24) return `in ${hours}h`;
		return `in ${Math.round(hours / 24)}d`;
	}

	let COLUMNS = $derived([
		{ class: 'checkbox-col' },
		{ label: m('exceptions.col.type') },
		{ label: m('exceptions.col.severity') },
		{ label: m('exceptions.col.invoice') },
		{ label: m('exceptions.col.vendor') },
		{ label: m('exceptions.col.amount'), class: 'right' },
		{ label: m('exceptions.col.assignee') },
		{ label: m('exceptions.col.age') },
		{ label: m('exceptions.col.due') },
		{ label: m('exceptions.col.status') },
		{ class: 'actions-col' }
	]);

	let statusChips = $derived(
		summary
			? [
					{
						key: 'all',
						label: m('common.all'),
						count: summary.open + summary.escalated + summary.resolved + summary.dismissed
					},
					{ key: 'open', label: m('exceptions.filter.open'), count: summary.open },
					{ key: 'escalated', label: m('exceptions.filter.escalated'), count: summary.escalated },
					{ key: 'resolved', label: m('exceptions.filter.resolved'), count: summary.resolved },
					{ key: 'dismissed', label: m('exceptions.filter.dismissed'), count: summary.dismissed }
				]
			: []
	);

	// Order matters: "still loading" and "we failed to look" both outrank any
	// claim about what the queue contains.
	let emptyMessage = $derived(
		loading
			? m('common.loading')
			: errored
				? m('exceptions.empty.errored')
				: statusFilter === 'open'
					? m('exceptions.empty.open')
					: m('exceptions.empty.other')
	);
</script>

<PageHeader title={m('exceptions.title')}>
	<Tabs
		tabs={[
			{ key: 'queue', label: m('exceptions.tab.queue'), count: summary ? summary.open + summary.escalated : undefined },
			{ key: 'agents', label: m('exceptions.tab.agents') }
		]}
		bind:active={view}
		onchange={syncViewToUrl}
		ariaLabel="Exceptions views"
		idPrefix="exc"
	/>

	{#if view === 'agents'}
		<div id="exc-panel-agents" role="tabpanel" aria-labelledby="exc-tab-agents">
			<AgentDashboard />
		</div>
	{:else}
	<div id="exc-panel-queue" role="tabpanel" aria-labelledby="exc-tab-queue">
	{#if summary}
		<FilterChips chips={statusChips} bind:active={statusFilter} />

		{#if Object.keys(summary.by_type).length > 0}
			<nav class="type-filters">
				<button
					class="type-chip"
					class:active={typeFilter === null}
					onclick={() => (typeFilter = null)}
				>
					{m('exceptions.filter.allTypes')}
				</button>
				{#each Object.entries(summary.by_type) as [type, count]}
					<button
						class="type-chip"
						class:active={typeFilter === type}
						style="--type-color:{TYPE_COLORS[type] ?? '#888'}"
						onclick={() => (typeFilter = typeFilter === type ? null : type)}
					>
						<span class="type-dot"></span>
						{typeLabel(type)} <span class="count">{count}</span>
					</button>
				{/each}
			</nav>
		{/if}
	{/if}

	<BulkBar
		count={selectedIds.size}
		onclear={() => {
			selectedIds = new Set();
			selectedAllMatching = false;
		}}
	>
		{#snippet actions()}
			{#if allSelected && !selectedAllMatching && total > selectableIds.size}
				<button class="bulk-action-btn" disabled={selectingAllMatching} onclick={selectAllMatching}>
					{selectingAllMatching
						? m('common.loading')
						: m('common.selectAllMatching', { total })}
				</button>
			{:else if selectedAllMatching}
				<span class="bulk-all-matching-note">{m('common.allMatchingSelected')}</span>
			{/if}
			<button class="bulk-action-btn" onclick={openBulkResolve}>
				{m('exceptions.bulk.resolve', { n: selectedIds.size })}
			</button>
		{/snippet}
	</BulkBar>

	<DataTable columns={COLUMNS} isEmpty={exceptions.length === 0} empty={emptyMessage} colspan={11}>
		{#snippet header()}
			<tr>
				<th class="checkbox-col">
					<input
						type="checkbox"
						checked={allSelected}
						onchange={toggleSelectAll}
						aria-label={m('exceptions.selectAllAria')}
					/>
				</th>
				<th>{m('exceptions.col.type')}</th>
				<th>{m('exceptions.col.severity')}</th>
				<th>{m('exceptions.col.invoice')}</th>
				<th>{m('exceptions.col.vendor')}</th>
				<th class="right">{m('exceptions.col.amount')}</th>
				<th>{m('exceptions.col.assignee')}</th>
				<th>{m('exceptions.col.age')}</th>
				<th>{m('exceptions.col.due')}</th>
				<th>{m('exceptions.col.status')}</th>
				<th class="actions-col"></th>
			</tr>
		{/snippet}
		{#snippet body()}
			{#each exceptions as exc (exc.id)}
				<tr
					class:row-selected={selectedIds.has(exc.id)}
					class:resolved={exc.status === 'resolved' || exc.status === 'dismissed'}
				>
					<td class="checkbox-col">
						{#if selectableIds.has(exc.id)}
							<input
								type="checkbox"
								checked={selectedIds.has(exc.id)}
								onchange={() => toggleSelect(exc.id)}
								aria-label={m('exceptions.selectAria')}
							/>
						{/if}
					</td>
					<td>
						<span
							class="type-badge"
							style="background:{TYPE_COLORS[exc.exception_type] ?? '#888'}1f;color:{TYPE_COLORS[exc.exception_type] ?? '#888'}"
							title={exc.description ?? ''}
						>
							{typeLabel(exc.exception_type, exc.type_label)}
						</span>
					</td>
					<td>
						<span
							class="severity"
							style="color:{SEVERITY_COLORS[exc.severity] ?? '#888'}"
						>
							{exc.severity}
						</span>
					</td>
					<td class="mono">{exc.invoice_number ?? '—'}</td>
					<td class="muted-cell">{exc.vendor_name ?? '—'}</td>
					<td class="mono right">{formatCurrency(exc.amount, exc.currency)}</td>
					<td class="muted-cell">{exc.assigned_to ?? '—'}</td>
					<td class="muted-cell" title={exc.created_at}>{timeAgo(exc.created_at)}</td>
					<td class="muted-cell" class:overdue={exc.is_overdue}>
						{dueLabel(exc)}
					</td>
					<td>
						<Badge
							tone={exceptionStatusTone(exc.status)}
							variant="status-badge badge-{exc.status}"
						>
							{statusLabel(exc.status)}
						</Badge>
					</td>
					<td class="actions">
						{#if exc.status === 'open' || exc.status === 'escalated'}
							<RowAction onclick={() => openResolve(exc)}>{m('exceptions.row.resolve')}</RowAction>
						{/if}
						{#if exc.invoice_id}
							<RowAction href="/invoices?id={exc.invoice_id}">{m('exceptions.row.invoice')}</RowAction>
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>

	{#if hasMore}
		<div class="load-more-row">
			<button class="btn-load-more" onclick={loadMoreExceptions} disabled={loadingMore}>
				{loadingMore ? m('common.loading') : m('exceptions.loadMore', { shown: exceptions.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('exceptions.showingAll', { total })}</span>
		</div>
	{/if}
	</div>
	{/if}
</PageHeader>

<!-- Single-row resolve modal -->
<Modal
	open={resolveTarget !== null}
	ariaLabel="Resolve exception"
	width="sm"
	onclose={() => (resolveTarget = null)}
>
	{#if resolveTarget}
		<h2>{m('exceptions.resolveModal.title')}</h2>
		<p class="modal-hint">
			<strong>{resolveTarget.type_label}</strong>
			{#if resolveTarget.invoice_number}— {resolveTarget.invoice_number}{/if}
			{#if resolveTarget.vendor_name}· {resolveTarget.vendor_name}{/if}
		</p>
		{#if resolveTarget.description}
			<p class="modal-description">{resolveTarget.description}</p>
		{/if}
		<form onsubmit={(e) => { e.preventDefault(); commitResolve('resolve'); }}>
			<label>
				<span>{m('exceptions.resolveModal.note')}</span>
				<input
					type="text"
					bind:value={resolutionText}
					placeholder={m('exceptions.resolveModal.notePlaceholder')}
					maxlength="500"
					autofocus
				/>
			</label>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (resolveTarget = null)}>
					{m('common.cancel')}
				</button>
				<button
					type="button"
					class="btn-secondary"
					disabled={saving}
					onclick={() => commitResolve('dismiss')}
				>
					{m('exceptions.resolveModal.dismiss')}
				</button>
				<button
					type="button"
					class="btn-warning"
					disabled={saving || !resolutionText.trim()}
					onclick={() => commitResolve('escalate')}
				>
					{m('exceptions.resolveModal.escalate')}
				</button>
				<button type="submit" class="btn-primary" disabled={saving || !resolutionText.trim()}>
					{saving ? m('common.saving') : m('exceptions.resolveModal.resolve')}
				</button>
			</div>
		</form>
	{/if}
</Modal>

<!-- Bulk-resolve modal -->
<Modal
	open={bulkResolveOpen}
	ariaLabel="Resolve selected exceptions"
	width="sm"
	onclose={() => (bulkResolveOpen = false)}
>
	<h2>{m('exceptions.bulkModal.title', { n: selectedIds.size })}</h2>
	<p class="modal-hint">
		{m('exceptions.bulkModal.hint')}
	</p>
	<form onsubmit={(e) => { e.preventDefault(); commitBulkResolve('resolve'); }}>
		<label>
			<span>{m('exceptions.resolveModal.note')}</span>
			<input
				type="text"
				bind:value={resolutionText}
				placeholder={m('exceptions.bulkModal.notePlaceholder')}
				maxlength="500"
				autofocus
			/>
		</label>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (bulkResolveOpen = false)}>
				{m('common.cancel')}
			</button>
			<button
				type="button"
				class="btn-secondary"
				disabled={saving}
				onclick={() => commitBulkResolve('dismiss')}
			>
				{m('exceptions.resolveModal.dismiss')}
			</button>
			<button
				type="button"
				class="btn-warning"
				disabled={saving || !resolutionText.trim()}
				onclick={() => commitBulkResolve('escalate')}
			>
				{m('exceptions.resolveModal.escalate')}
			</button>
			<button type="submit" class="btn-primary" disabled={saving || !resolutionText.trim()}>
				{saving ? m('common.saving') : m('exceptions.resolveModal.resolve')}
			</button>
		</div>
	</form>
</Modal>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */

	/* --- Type filter chips --- */

	.type-filters {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}

	.type-chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 4px 10px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.78rem;
		text-transform: capitalize;
		cursor: pointer;
		font-family: inherit;
	}

	.type-chip:hover {
		color: var(--text);
	}

	.type-chip.active {
		border-color: var(--type-color, var(--accent));
		color: var(--text);
	}

	.type-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--type-color, var(--accent));
	}

	.type-chip .count {
		font-size: 0.72rem;
		color: var(--text-muted);
	}

	/* --- Bulk-bar action --- */

	.bulk-action-btn {
		padding: 6px 14px;
		border-radius: 4px;
		border: 1px solid var(--accent-strong);
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.bulk-action-btn:hover {
		filter: brightness(1.1);
	}

	.bulk-action-btn:disabled {
		opacity: 0.6;
		cursor: default;
		filter: none;
	}

	.bulk-all-matching-note {
		font-size: 0.85rem;
		color: var(--text-muted);
		white-space: nowrap;
	}

	/* --- Bespoke cells / rows --- */

	/* De-emphasize resolved/dismissed rows with a tint, not a blanket
	   opacity — opacity composites every cell's text below the WCAG 1.4.3
	   4.5:1 contrast floor. The resolved/dismissed status badge carries the
	   state signal. */
	tbody tr.resolved td {
		background: rgba(138, 143, 160, 0.05);
	}

	.checkbox-col {
		width: 32px;
		padding-right: 0;
	}

	.muted-cell {
		color: var(--text-muted);
	}

	.muted-cell.overdue {
		color: #f06464;
		font-weight: 600;
	}

	/* --- Type / severity / status badges --- */

	.type-badge {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 10px;
		font-size: 0.75rem;
		font-weight: 600;
		white-space: nowrap;
	}

	.severity {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
	}

	.actions-col {
		width: 180px;
	}

	/* --- Modal extras --- */

	.modal-description {
		font-size: 0.82rem;
		color: var(--text);
		margin: 0 0 14px;
		padding: 8px 10px;
		background: var(--bg);
		border-radius: 4px;
	}

	.modal input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.btn-secondary {
		padding: 8px 14px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-secondary:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-warning {
		padding: 8px 14px;
		border-radius: 4px;
		border: 1px solid #d4940a;
		background: var(--surface);
		color: #d4940a;
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-warning:hover:not(:disabled) {
		background: rgba(212, 148, 10, 0.1);
	}

	.btn-secondary:disabled,
	.btn-warning:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
</style>
