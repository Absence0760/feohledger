<script lang="ts">
	import { untrack } from 'svelte';
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
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import SortableHeader from '$lib/components/ui/SortableHeader.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Tabs from '$lib/components/ui/Tabs.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import {
		EXCEPTION_SEVERITIES,
		exceptionSeverityLabelKey,
		exceptionStatusLabelKey,
		exceptionStatusTone,
		exceptionTypeFallback,
		exceptionTypeLabelKey
	} from '$lib/types/exception';
	import type { ExceptionSeverity } from '$lib/types/exception';
	import type { ExceptionSummary } from '$lib/types/exceptionSummary';
	import { toggleSort, type SortOrder } from '$lib/utils/sort';
	import AgentDashboard from '$lib/components/exceptions/AgentDashboard.svelte';
	import { formatMoney } from '$lib/utils/money';
	import { formatDate, timeAgo } from '$lib/utils/time';
	import { getActiveFormatLocale } from '$lib/i18n/formatLocale';
	import { pruneSelection } from '$lib/utils/selection';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import type { MessageKey } from '$lib/i18n/messages';
	import { formatApiDetail } from '$lib/utils/apiError';

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

	/** Every chip tally on the page — see {@link ExceptionSummary}. */
	type Summary = ExceptionSummary;

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
	// Queue filters round-trip through the query string (`?status=`, `?type=`),
	// the convention every other list route follows. They used to live only in
	// `$state`, so reload / back / a pasted link all dropped the operator back
	// on the default Open view — on a triage queue that is the difference
	// between "here is the fraud flag I am asking you to look at" and "here is
	// the exceptions page, go find it". `syncUrl()` below is the single writer.
	//
	// Clamped to the keys the chips render: an unrecognised `?status=` would
	// leave every chip unpressed over an empty table, so the page would show a
	// queue state that no control on it can explain or undo.
	const STATUS_KEYS = ['all', 'open', 'escalated', 'resolved', 'dismissed'];
	let statusFilter = $state(
		STATUS_KEYS.includes($pageStore.url.searchParams.get('status') ?? '')
			? ($pageStore.url.searchParams.get('status') as string)
			: 'open'
	);
	let typeFilter = $state<string | null>($pageStore.url.searchParams.get('type'));
	// `?severity=`, clamped to the chips' own roster for the reason `status` is:
	// an unknown value would narrow the table under a row with no chip pressed.
	let severityFilter = $state<string>(
		(EXCEPTION_SEVERITIES as readonly string[]).includes(
			$pageStore.url.searchParams.get('severity') ?? ''
		)
			? ($pageStore.url.searchParams.get('severity') as string)
			: 'all'
	);
	// Server-side search over invoice number + vendor (`?search=`). Never a
	// client-side `.filter()` over the loaded page — that searches 20 rows and
	// calls it the queue (`frontend/docs/ui-patterns.md` § Search).
	let search = $state($pageStore.url.searchParams.get('search') ?? '');
	// The term the newest ISSUED list load carried. Written by `loadExceptions`,
	// read by the debounce effect (a term already on screen schedules nothing)
	// and by the summary / select-all requests, which must describe the set the
	// TABLE shows rather than whatever is half-typed in the box.
	let appliedSearch = $state(($pageStore.url.searchParams.get('search') ?? '').trim());
	// Column sort (`?sort=&order=`), clamped to the backend allowlist
	// (`api/exceptions.EXCEPTION_SORTABLE_COLUMNS`): an unknown key is a 422
	// there, which would turn a hand-edited link into the error state. `null` is
	// the backend's default order, newest first.
	const SORT_KEYS = ['created_at', 'severity', 'due_at'];
	let sortField = $state<string | null>(
		SORT_KEYS.includes($pageStore.url.searchParams.get('sort') ?? '')
			? $pageStore.url.searchParams.get('sort')
			: null
	);
	let sortOrder = $state<SortOrder>(
		$pageStore.url.searchParams.get('order') === 'asc' ? 'asc' : 'desc'
	);
	let selectedIds = $state<Set<string>>(new Set());
	// True once "Select all N matching" (below) has resolved the WHOLE
	// filtered set of open/escalated exceptions — not just the loaded page —
	// into `selectedIds`. See the identical mechanism on the invoices list
	// page (`routes/invoices/+page.svelte`) for the full rationale. Reset by
	// `reload()` whenever a chip or the search changes the set; a sort does not.
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

	/**
	 * The ONE writer of this route's query string — `view`, `status`, `type`,
	 * `severity`, `search`, `sort` / `order`.
	 *
	 * It must be the only one, for the reason `routes/invoices/+page.svelte`
	 * documents at length: SvelteKit's shallow `replaceState` writes `history`
	 * but never `$page.url`, so a second writer rebuilding from `$page.url`
	 * reads a frozen snapshot and deterministically drops whatever the first
	 * writer added. The previous `syncViewToUrl` mutated a copy of `$page.url`,
	 * which worked only while `view` was the sole param on the route.
	 *
	 * Built from scratch rather than by mutation so "one owner" stays
	 * checkable: a param not listed here does not survive.
	 *
	 * Every read is untracked — this is a writer called from the filter
	 * `$effect`, and a tracked read would re-trigger the effect that calls it.
	 */
	function syncUrl() {
		untrack(() => {
			const params = new URLSearchParams();
			if (view === 'agents') params.set('view', 'agents');
			if (statusFilter !== 'open') params.set('status', statusFilter);
			if (typeFilter) params.set('type', typeFilter);
			if (severityFilter !== 'all') params.set('severity', severityFilter);
			const term = search.trim();
			if (term) params.set('search', term);
			if (sortField) {
				params.set('sort', sortField);
				params.set('order', sortOrder);
			}
			const qs = params.toString();
			replaceState(`${$pageStore.url.pathname}${qs ? `?${qs}` : ''}`, {});
		});
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

	// Total over `ExceptionSeverity`, the same pairing `EXCEPTION_STATUS_TONES`
	// has with its label map: a severity that gains a colour without a label —
	// a tinted cell printing a raw wire value — is a compile error. It stays
	// HERE rather than moving next to the label keys because it is the sibling
	// of `TYPE_COLORS` above, and the contrast reasoning written there governs
	// both; `$lib/types/` carries vocabularies and tone NAMES
	// (`EXCEPTION_STATUS_TONES` is `BadgeTone`), not measured hexes.
	const SEVERITY_COLORS: Record<ExceptionSeverity, string> = {
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
	/**
	 * A severity's label. The cell printed `error` / `warning` / `info` in
	 * lowercase Latin beside a type badge and a status badge that were both
	 * already translated — the last data-driven value on the row still reading
	 * off the wire. An unrecognised severity still prints raw, and takes the
	 * grey the colour map already falls back to.
	 */
	function severityLabel(severity: string): string {
		const key = exceptionSeverityLabelKey(severity);
		return key ? m(key) : severity;
	}

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

	/**
	 * Re-read the table AND its chip tallies for the current filters. The
	 * summary is faceted over the same filter set the list takes, so any filter
	 * change (a chip in any row, a search) moves some tally; sort and load-more
	 * change neither, and call `loadExceptions` alone.
	 *
	 * `loadExceptions` runs first because it stamps `appliedSearch`
	 * synchronously, before its first `await`, and `loadSummary` reads it.
	 */
	function reload() {
		// The "select all N matching" set was resolved against the FILTERS
		// active when it was clicked; once they change it no longer describes
		// anything real, so drop out of matching mode. Must happen before
		// `loadExceptions()` — see the identical note on the invoices list page.
		selectedAllMatching = false;
		syncUrl();
		loadExceptions();
		loadSummary();
	}

	// A chip click is a discrete action, so it reloads immediately.
	$effect(() => {
		statusFilter;
		typeFilter;
		severityFilter;
		reload();
	});

	// A keystroke costs a request, so the term is debounced 300ms (the
	// /invoices, /vendors, /requisitions convention) and the sequencers below
	// discard a slow response for an earlier term. A term that already matches
	// `appliedSearch` schedules nothing — that is what stops this effect's FIRST
	// run (mount, including a bookmarked `?search=`) from firing a duplicate
	// load 300ms behind the chip effect's, and it cancels a pending debounce
	// when a chip click has already loaded with the typed term.
	let searchTimer: ReturnType<typeof setTimeout>;
	$effect(() => {
		const next = search.trim();
		clearTimeout(searchTimer);
		if (next === appliedSearch) return;
		searchTimer = setTimeout(reload, 300);
		// Cancel a pending debounce on teardown, or it fires against a route
		// the user already left.
		return () => clearTimeout(searchTimer);
	});

	/**
	 * The filter half of every queue request — list, chip tallies and the
	 * select-all resolver — built in one place so the three cannot describe
	 * different sets (the backend routes all three through one builder, too).
	 * `status=all` is the backend's "no filter" as well, so it is omitted rather
	 * than spelled. Every read is untracked: this runs inside the chip effect,
	 * and a tracked `search` read would make that effect fire per keystroke,
	 * un-debounced (issue #168).
	 */
	function filterParams(term: string): URLSearchParams {
		const params = new URLSearchParams();
		untrack(() => {
			if (statusFilter !== 'all') params.set('status', statusFilter);
			if (typeFilter) params.set('type', typeFilter);
			if (severityFilter !== 'all') params.set('severity', severityFilter);
		});
		if (term) params.set('search', term);
		return params;
	}

	function handleSort(field: string) {
		const next = toggleSort({ field: sortField, order: sortOrder }, field);
		applySort(next.field, next.order);
	}

	/**
	 * `Age` is `created_at` read backwards — the OLDEST row has the LARGEST age —
	 * so the Age header reports the order of the ages it shows: ascending age is
	 * `created_at` descending. Without the flip its `aria-sort="ascending"`
	 * would announce the opposite of what the column visibly does.
	 */
	const AGE_FIELD = 'created_at';
	function flipOrder(order: SortOrder): SortOrder {
		return order === 'asc' ? 'desc' : 'asc';
	}
	let ageOrder = $derived(flipOrder(sortOrder));
	function handleAgeSort() {
		const next = toggleSort(
			{ field: sortField, order: sortField === AGE_FIELD ? ageOrder : sortOrder },
			AGE_FIELD
		);
		applySort(AGE_FIELD, flipOrder(next.order));
	}

	// Sort reorders the set; it cannot change which rows are in it, so the chip
	// tallies and a "select all N matching" selection both stay valid.
	function applySort(field: string | null, order: SortOrder) {
		sortField = field;
		sortOrder = order;
		syncUrl();
		loadExceptions();
	}

	$effect(() => {
		orgCurrency.ensureLoaded();
	});

	async function loadExceptions(opts: { append?: boolean; nextPage?: number } = {}) {
		const nextPage = opts.nextPage ?? 1;
		const token = fetchSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		errored = false;
		try {
			// A fresh load takes the live term and records it; load-more keeps the
			// term page 1 was fetched with, or a half-typed box would append
			// another search's rows to this one's.
			const term = opts.append
				? untrack(() => appliedSearch)
				: untrack(() => search).trim();
			if (!opts.append) appliedSearch = term;
			const params = filterParams(term);
			const currentSort = untrack(() => sortField);
			if (currentSort) {
				params.set('sort', currentSort);
				params.set('order', untrack(() => sortOrder));
			}
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
			toast(m('exceptions.toast.loadFailed'), 'error');
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
			// The SAME filters the table was just loaded with — search included,
			// via `appliedSearch` — so every chip counts the set on screen. This
			// page once sent the summary nothing at all, and its type chips
			// carried OPEN-only tallies while the operator was looking at
			// Escalated / Resolved / All; with a search box that gap would have
			// been every chip counting the tenant above a one-row table.
			const params = filterParams(untrack(() => appliedSearch));
			const data = await api.get<Summary>(`/api/exceptions/summary?${params}`);
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

	/**
	 * One whole sentence per outcome, keyed by action. These toasts used to
	 * conjugate the verb in a template literal — `Exception ${action}d`,
	 * `${n} ${action}d, ${k} skipped` — which is English morphology no
	 * catalogue key can carry, and which spelled the dismissal "dismissd".
	 */
	const DONE_KEYS: Record<Action, MessageKey> = {
		resolve: 'exceptions.toast.resolved',
		escalate: 'exceptions.toast.escalated',
		dismiss: 'exceptions.toast.dismissed'
	};
	const BULK_DONE_KEYS: Record<Action, MessageKey> = {
		resolve: 'exceptions.toast.bulkResolved',
		escalate: 'exceptions.toast.bulkEscalated',
		dismiss: 'exceptions.toast.bulkDismissed'
	};
	const BULK_DONE_SKIPPED_KEYS: Record<Action, MessageKey> = {
		resolve: 'exceptions.toast.bulkResolvedSkipped',
		escalate: 'exceptions.toast.bulkEscalatedSkipped',
		dismiss: 'exceptions.toast.bulkDismissedSkipped'
	};

	/**
	 * A refused or failed action, in the backend's own words.
	 *
	 * `api.ts` already renders every error body through `formatApiDetail` — a
	 * 422's validation LIST as `field: msg`, a segregation-of-duties 403's
	 * sentence verbatim — and throws that string as the `ApiError`'s message.
	 * The helper this replaced read an `e.detail` no `ApiError` carries (dead:
	 * had one ever been a list it would have printed `[object Object]`) and fell
	 * back to an English literal. Routing the message through the same
	 * `formatApiDetail` keeps a blank one on the translated fallback.
	 */
	function actionError(err: unknown): string {
		return formatApiDetail(
			err instanceof Error ? err.message : undefined,
			m('exceptions.toast.actionFailed')
		);
	}

	async function commitResolve(action: Action) {
		if (!resolveTarget) return;
		const note = resolutionText.trim();
		if (!note && action !== 'dismiss') {
			toast(m('exceptions.toast.noteRequired'), 'error');
			return;
		}
		saving = true;
		try {
			// The note exactly as typed — empty for a dismissal without one. The
			// page used to invent `${action}d by user` ("dismissd by user") and
			// store it as though the operator had written it; the decision itself
			// is already on the append-only `exception.dismissed` row, and the
			// backend records no note when none was given.
			await api.post(`/api/exceptions/${resolveTarget.id}/resolve`, {
				resolution: note,
				action,
			});
			toast(m(DONE_KEYS[action]), 'success');
			resolveTarget = null;
			resolutionText = '';
			await Promise.all([loadExceptions(), loadSummary()]);
		} catch (err) {
			toast(actionError(err), 'error');
		} finally {
			saving = false;
		}
	}

	async function commitBulkResolve(action: Action) {
		const ids = [...selectedIds];
		if (ids.length === 0) return;
		const note = resolutionText.trim();
		if (!note && action !== 'dismiss') {
			toast(m('exceptions.toast.noteRequired'), 'error');
			return;
		}
		saving = true;
		try {
			// As typed, for the reason `commitResolve` gives.
			const body = await api.post<{ updated: number; skipped: { id: string; reason: string }[] }>(
				'/api/exceptions/bulk/resolve',
				{ ids, action, resolution: note }
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
			// Two whole sentences at most: the outcome, then — only when rows
			// were refused — the segregation explanation, which is its own
			// catalogue sentence rather than a clause spliced into this one.
			const outcome =
				skipped === 0
					? m(BULK_DONE_KEYS[action], { n: body.updated })
					: m(BULK_DONE_SKIPPED_KEYS[action], { n: body.updated, skipped });
			toast(
				refused > 0
					? `${outcome} ${m('exceptions.bulk.segregationSkipped', { n: refused })}`
					: outcome,
				skipped === 0 ? 'success' : 'info'
			);
			bulkResolveOpen = false;
			resolutionText = '';
			selectedIds = new Set();
			selectedAllMatching = false;
			await Promise.all([loadExceptions(), loadSummary()]);
		} catch (err) {
			toast(actionError(err), 'error');
		} finally {
			saving = false;
		}
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
			// The table's own filters, search and severity included — a selection
			// wider than the rows on screen is a bulk action on rows nobody saw.
			const params = filterParams(appliedSearch);
			// Bulk resolve only ever acts on open/escalated rows — mirrors
			// `selectableIds`. `statusFilter === 'all'` has no narrower value to
			// reuse, so it's spelled out explicitly.
			params.set('status', statusFilter === 'all' ? 'open,escalated' : statusFilter);
			const res = await api.get<MatchingIdsResponse>(`/api/exceptions/ids?${params}`);
			selectedIds = new Set(res.ids);
			selectedAllMatching = true;
			if (res.truncated) {
				toast(
					m('exceptions.toast.selectAllTruncated', { shown: res.ids.length, total: res.total }),
					'error'
				);
			} else {
				toast(m('exceptions.toast.selectedAllMatching', { n: res.ids.length }), 'success');
			}
		} catch {
			toast(m('exceptions.toast.selectAllFailed'), 'error');
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

	/**
	 * The due cell, in the reader's own language.
	 *
	 * It used to compose English by hand — `3h overdue`, `in 2d` — inside a page
	 * whose every other data-driven cell is translated, so five of the six
	 * shipped locales read a German type badge beside an English deadline. The
	 * buckets are unchanged (hours under a day, then days); only the rendering
	 * moved to `Intl.RelativeTimeFormat` on the active in-app locale.
	 *
	 * `Intl` rather than catalogue keys for the same reason `utils/time.ts`
	 * gives: it brings every locale's plural rules with it and needs no new
	 * message keys to stay correct. A past deadline formats as the negative of
	 * the same unit ("3 hr. ago"), which is what overdue means — the `.overdue`
	 * colour and weight carry the urgency on top.
	 */
	/** Date + time parts for the exact-instant tooltips on Age / Due. */
	const DATETIME_OPTS: Intl.DateTimeFormatOptions = {
		month: 'short',
		day: 'numeric',
		year: 'numeric',
		hour: 'numeric',
		minute: '2-digit'
	};

	function dueLabel(exc: ExceptionItem): string {
		if (!exc.due_at) return '—';
		const due = new Date(exc.due_at).getTime();
		if (Number.isNaN(due)) return '—';
		const hours = Math.round((due - Date.now()) / 3600000);
		const fmt = new Intl.RelativeTimeFormat(getActiveFormatLocale(), {
			numeric: 'always',
			style: 'short'
		});
		return Math.abs(hours) < 24
			? fmt.format(hours, 'hour')
			: fmt.format(Math.round(hours / 24), 'day');
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

	/**
	 * The severity row — worst-first, the order the backend's rank map declares
	 * and the Sev column sorts by. All three always render, zeros included, the
	 * way the status row does: a severity chip that vanished at 0 would take the
	 * pressed state with it and leave the table narrowed by a chip nobody can
	 * see. A severity the server returns that this build predates still gets a
	 * chip, with its raw value.
	 */
	let severityChips = $derived.by(() => {
		if (!summary) return [];
		const counts = summary.by_severity;
		const known = EXCEPTION_SEVERITIES as readonly string[];
		const keys = [...known, ...Object.keys(counts).filter((k) => !known.includes(k))];
		return [
			{
				key: 'all',
				label: m('exceptions.filter.allSeverities'),
				count: Object.values(counts).reduce((sum, n) => sum + n, 0)
			},
			...keys.map((sev) => ({ key: sev, label: severityLabel(sev), count: counts[sev] ?? 0 }))
		];
	});

	/**
	 * The type row: every type the tallies found, plus the ACTIVE type even when
	 * it counts 0. The tallies are faceted over the search and the other rows, so
	 * a search can empty `by_type` — and a pressed type chip that dropped out of
	 * the row would leave the table narrowed by a filter nothing on screen shows
	 * or can undo (the `chipStatuses` rule on `/invoices`).
	 */
	let typeChipEntries = $derived.by((): [string, number][] => {
		if (!summary) return [];
		const entries = Object.entries(summary.by_type);
		if (typeFilter && !(typeFilter in summary.by_type)) entries.push([typeFilter, 0]);
		return entries;
	});

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
	//
	// And a FILTER matching nothing is not an empty queue. "No open exceptions.
	// Everything looks good!" is a statement about every open duplicate, fraud
	// flag and compliance hold in the tenant; with a type chip active it was
	// being printed over a set of one type, so narrowing to `Fraud Flag` and
	// finding none read as an all-clear on the whole queue. Only the
	// unfiltered Open view has earned that sentence — a type or severity chip,
	// or a search term, falls back to the neutral "No exceptions found."
	let emptyMessage = $derived(
		loading
			? m('common.loading')
			: errored
				? m('exceptions.empty.errored')
				: statusFilter === 'open' && !typeFilter && severityFilter === 'all' && !appliedSearch
					? m('exceptions.empty.open')
					: m('exceptions.empty.other')
	);
</script>

<PageHeader title={m('exceptions.title')}>
	<Tabs
		tabs={[
			// Open + escalated within the current filters and search — the same
			// faceted tallies as the status chips, so the tab and the chips below
			// it never disagree about how big the queue on screen is.
			{ key: 'queue', label: m('exceptions.tab.queue'), count: summary ? summary.open + summary.escalated : undefined },
			{ key: 'agents', label: m('exceptions.tab.agents') }
		]}
		bind:active={view}
		onchange={() => syncUrl()}
		ariaLabel={m('exceptions.tab.aria')}
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

		{#if typeChipEntries.length > 0}
			<!-- `aria-pressed` mirrors `ui/FilterChips`: the status row announces
			     which chip is on, and this row — the same control, one line
			     below — announced nothing, so a screen-reader user could not tell
			     a narrowed queue from the whole one. -->
			<nav class="type-filters">
				<button
					class="type-chip"
					class:active={typeFilter === null}
					type="button"
					aria-pressed={typeFilter === null}
					onclick={() => (typeFilter = null)}
				>
					{m('exceptions.filter.allTypes')}
				</button>
				{#each typeChipEntries as [type, count]}
					<button
						class="type-chip"
						class:active={typeFilter === type}
						type="button"
						aria-pressed={typeFilter === type}
						style="--type-color:{TYPE_COLORS[type] ?? '#888'}"
						onclick={() => (typeFilter = typeFilter === type ? null : type)}
					>
						<span class="type-dot"></span>
						{typeLabel(type)} <span class="count">{count}</span>
					</button>
				{/each}
			</nav>
		{/if}

		<!-- The same `exceptions.severity.*` keys the row's Sev cell reads, so a
		     chip and the rows it filters cannot name one severity two ways. -->
		<FilterChips chips={severityChips} bind:active={severityFilter} />
	{/if}

	<SearchBox
		bind:value={search}
		placeholder={m('exceptions.search.placeholder')}
		ariaLabel={m('exceptions.search.aria')}
	/>

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
				<!-- This page passes its own `header` snippet, so it owns `scope`
				     on every `<th>` (DataTable adds it only to the headers it
				     generates itself) — WCAG 1.3.1. -->
				<th class="checkbox-col" scope="col">
					<input
						type="checkbox"
						checked={allSelected}
						onchange={toggleSelectAll}
						aria-label={m('exceptions.selectAllAria')}
					/>
				</th>
				<th scope="col">{m('exceptions.col.type')}</th>
				<SortableHeader
					field="severity"
					label={m('exceptions.col.severity')}
					active={sortField === 'severity'}
					order={sortOrder}
					onsort={handleSort}
				/>
				<th scope="col">{m('exceptions.col.invoice')}</th>
				<th scope="col">{m('exceptions.col.vendor')}</th>
				<th class="right" scope="col">{m('exceptions.col.amount')}</th>
				<th scope="col">{m('exceptions.col.assignee')}</th>
				<!-- Sorts `created_at`, reported in AGE order — see `handleAgeSort`. -->
				<SortableHeader
					field={AGE_FIELD}
					label={m('exceptions.col.age')}
					active={sortField === AGE_FIELD}
					order={ageOrder}
					onsort={handleAgeSort}
				/>
				<SortableHeader
					field="due_at"
					label={m('exceptions.col.due')}
					active={sortField === 'due_at'}
					order={sortOrder}
					onsort={handleSort}
				/>
				<th scope="col">{m('exceptions.col.status')}</th>
				<th class="actions-col" scope="col"></th>
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
					<td class="type-cell">
						<span
							class="type-badge"
							style="background:{TYPE_COLORS[exc.exception_type] ?? '#888'}1f;color:{TYPE_COLORS[exc.exception_type] ?? '#888'}"
						>
							{typeLabel(exc.exception_type, exc.type_label)}
						</span>
						<!-- The description IS the triage datum — which invoice
						     it duplicates, which PO line the price missed. It was
						     reachable only by hovering the badge, so it existed
						     for neither a keyboard nor a touch operator, and a
						     queue could not be scanned without pointing at every
						     row in turn. Clamped to two lines with the full text
						     still in `title`. -->
						{#if exc.description}
							<span class="type-detail" title={exc.description}>{exc.description}</span>
						{/if}
					</td>
					<td>
						<span
							class="severity"
							style="color:{SEVERITY_COLORS[exc.severity as ExceptionSeverity] ?? '#888'}"
						>
							{severityLabel(exc.severity)}
						</span>
					</td>
					<td class="mono">{exc.invoice_number ?? '—'}</td>
					<td class="muted-cell">{exc.vendor_name ?? '—'}</td>
					<td class="mono right">{formatCurrency(exc.amount, exc.currency)}</td>
					<td class="muted-cell">{exc.assigned_to ?? '—'}</td>
					<!-- The precise instant belongs in `title`, formatted — the raw
					     ISO string was leaking into the tooltip of every row. -->
					<td class="muted-cell" title={formatDate(exc.created_at, '', DATETIME_OPTS)}>
						{timeAgo(exc.created_at)}
					</td>
					<td
						class="muted-cell"
						class:overdue={exc.is_overdue}
						title={formatDate(exc.due_at, '', DATETIME_OPTS)}
					>
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
	ariaLabel={m('exceptions.resolveModal.title')}
	width="sm"
	onclose={() => (resolveTarget = null)}
>
	{#if resolveTarget}
		<h2>{m('exceptions.resolveModal.title')}</h2>
		<p class="modal-hint">
			<!-- `typeLabel`, not the server's `type_label`: the row badge one
			     click away is translated, and naming the same type two ways
			     across a confirm step is §149's defect in its last hiding place. -->
			<strong>{typeLabel(resolveTarget.exception_type, resolveTarget.type_label)}</strong>
			{#if resolveTarget.invoice_number}— {resolveTarget.invoice_number}{/if}
			{#if resolveTarget.vendor_name}· {resolveTarget.vendor_name}{/if}
		</p>
		{#if resolveTarget.description}
			<p class="modal-description">{resolveTarget.description}</p>
		{/if}
		<!-- `data-testid` is the e2e suite's handle on this dialog, so a spec
		     never has to name it by its accessible name — which is translated. -->
		<form
			data-testid="exception-resolve-form"
			onsubmit={(e) => { e.preventDefault(); commitResolve('resolve'); }}
		>
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
	ariaLabel={m('exceptions.bulkModal.title', { n: selectedIds.size })}
	width="sm"
	onclose={() => (bulkResolveOpen = false)}
>
	<h2>{m('exceptions.bulkModal.title', { n: selectedIds.size })}</h2>
	<p class="modal-hint">
		{m('exceptions.bulkModal.hint')}
	</p>
	<form
		data-testid="exception-bulk-resolve-form"
		onsubmit={(e) => { e.preventDefault(); commitBulkResolve('resolve'); }}
	>
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

	/* `--danger`, not a literal: a hand-measured hex here was a second source
	   of truth for the one colour the palette already names for this job. */
	.muted-cell.overdue {
		color: var(--danger);
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

	/* The type cell carries the badge plus the finding itself, so it is the
	   one column allowed to wrap. The width cap keeps the rest of the row on
	   its existing grid — a free-running description would push Amount and
	   Status off the scan line the queue is read down. */
	.type-cell {
		max-width: 320px;
	}

	.type-detail {
		display: -webkit-box;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		overflow: hidden;
		margin-top: 3px;
		color: var(--text-muted);
		font-size: 0.78rem;
		line-height: 1.35;
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
