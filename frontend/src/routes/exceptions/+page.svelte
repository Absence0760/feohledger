<script lang="ts">
	import { untrack } from 'svelte';
	import { page as pageStore } from '$app/state';
	import { afterNavigate, replaceState } from '$app/navigation';
	import { api } from '#lib/api.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import type { MatchingIdsResponse } from '#lib/utils/pagination.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import BulkBar from '#lib/components/ui/BulkBar.svelte';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';
	import SearchBox from '#lib/components/ui/SearchBox.svelte';
	import SortableHeader from '#lib/components/ui/SortableHeader.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Tabs from '#lib/components/ui/Tabs.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import {
		EXCEPTION_SEVERITIES,
		exceptionSeverityLabelKey,
		exceptionStatusLabelKey,
		exceptionStatusTone,
		exceptionTypeFallback,
		exceptionTypeLabelKey
	} from '#lib/types/exception.ts';
	import type { ExceptionSeverity } from '#lib/types/exception.ts';
	import type { ExceptionSummary } from '#lib/types/exceptionSummary.ts';
	import { clearSort, toggleSort, type SortOrder } from '#lib/utils/sort.ts';
	import AgentDashboard from '#lib/components/exceptions/AgentDashboard.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { formatMoney, type MoneyAmount } from '#lib/utils/money.ts';
	import { formatDate, timeAgo } from '#lib/utils/time.ts';
	import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
	import { pruneSelection } from '#lib/utils/selection.ts';
	import { orgCurrency } from '#lib/stores/orgSettings.svelte.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { formatApiDetail } from '#lib/utils/apiError.ts';
	import {
		isAllowed,
		isOffered,
		refusalText,
		type ActionRefusal
	} from '#lib/api/actionVerdicts.ts';
	import {
		exceptionDescriptionText,
		exceptionDescriptionTitle,
		type ExceptionDescriptionText
	} from '#lib/api/exceptionDescription.ts';
	import {
		erpPrefill,
		isErpPaidReport,
		poRematchNote,
		type ErpReport
	} from '#lib/types/erpReport.ts';
	import type { PaymentMethod } from '#lib/types/payment.ts';
	import { localToday } from '#lib/types/paymentExecution.ts';
	import RecordPaymentFields from '#lib/components/payments/RecordPaymentFields.svelte';

	interface ExceptionItem {
		id: string;
		invoice_id: string | null;
		invoice_number: string | null;
		vendor_name: string | null;
		// The joined invoice's amount: an exact decimal string (`"1234.50"`), or a
		// JSON number from a backend predating that wire change — `MoneyAmount`
		// takes both, and it is display-only: never added or compared here.
		amount: MoneyAmount;
		// What `amount` is denominated in — the joined invoice's own code, not the
		// org's reporting currency (a GBP-reporting tenant holds USD invoices, so
		// labelling the row with the rollup code is a different wrong answer).
		// Null exactly when `amount` is: both are gated on the invoice having been
		// joined. See `docs/decisions.md` §160.
		currency: string | null;
		exception_type: string;
		type_label: string;
		severity: string;
		// The English sentence — the FALLBACK since migration 0103. The code +
		// params name it in the invoice-warning catalogue; both null for a
		// human-written reason and for rows raised before 0103.
		description: string | null;
		description_code?: string | null;
		description_params?: Record<string, unknown> | null;
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
		/** The CALLER's verdict on each decision for this row — `null` when
		 *  `POST /{id}/resolve` would accept that action from them, else the
		 *  coded refusal it would answer with (`api/exceptions.py::
		 *  decision_refusals`: the status gate, then the segregation predicate
		 *  `record_decision` raises from). The resolve dialog offers each button
		 *  from this; the page used to check only the status, so the uploader of
		 *  a duplicate invoice was offered a Resolve that 403'd (issue #575).
		 *  Absent on a row from any other payload = unknown = nothing offered. */
		decision_refusals?: Record<Action, ActionRefusal>;
		/** What the ERP reported, as data (decisions §319) — null on every row
		 *  but the ERP reconciliation ones. */
		erp_report?: ErpReport | null;
		/** The ERP's product name for that report, or null ("your ERP"). */
		erp_name?: string | null;
		/** Only on a row carrying an ERP-reported payment: the caller's verdict
		 *  on `POST /{id}/record-erp-payment`. Absent = unknown = not offered. */
		record_erp_payment_refusal?: ActionRefusal;
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
	// Column sort (`?sort=&order=`), clamped to the backend allowlist
	// (`api/exceptions.EXCEPTION_SORTABLE_COLUMNS`): an unknown key is a 422
	// there, which would turn a hand-edited link into the error state. `null` is
	// the backend's default order, newest first.
	const SORT_KEYS = ['created_at', 'severity', 'due_at'];

	/**
	 * The one READER of this route's query string — the counterpart of the one
	 * writer, `syncUrl()`. Used at mount and again by the `afterNavigate` hook
	 * for a navigation that reuses this page, so the two cannot clamp a param
	 * differently.
	 */
	function readUrlState(params: Pick<URLSearchParams, 'get'>) {
		const status = params.get('status') ?? '';
		const severity = params.get('severity') ?? '';
		const sort = params.get('sort') ?? '';
		return {
			view: (params.get('view') === 'agents' ? 'agents' : 'queue') as 'queue' | 'agents',
			status: STATUS_KEYS.includes(status) ? status : 'open',
			type: params.get('type'),
			// `?severity=`, clamped to the chips' own roster for the reason
			// `status` is: an unknown value would narrow the table under a row
			// with no chip pressed.
			severity: (EXCEPTION_SEVERITIES as readonly string[]).includes(severity) ? severity : 'all',
			search: params.get('search') ?? '',
			sortField: SORT_KEYS.includes(sort) ? sort : null,
			sortOrder: (params.get('order') === 'asc' ? 'asc' : 'desc') as SortOrder
		};
	}
	const initialUrlState = readUrlState(pageStore.url.searchParams);

	let statusFilter = $state(initialUrlState.status);
	let typeFilter = $state<string | null>(initialUrlState.type);
	let severityFilter = $state<string>(initialUrlState.severity);
	// Server-side search over invoice number + vendor (`?search=`). Never a
	// client-side `.filter()` over the loaded page — that searches 20 rows and
	// calls it the queue (`frontend/docs/ui-patterns.md` § Search).
	let search = $state(initialUrlState.search);
	// The term the newest ISSUED list load carried. Written by `loadExceptions`,
	// read by the debounce effect (a term already on screen schedules nothing)
	// and by the summary / select-all requests, which must describe the set the
	// TABLE shows rather than whatever is half-typed in the box.
	let appliedSearch = $state(initialUrlState.search.trim());
	let sortField = $state<string | null>(initialUrlState.sortField);
	let sortOrder = $state<SortOrder>(initialUrlState.sortOrder);
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
	let view = $state<'queue' | 'agents'>(initialUrlState.view);

	/**
	 * The ONE writer of this route's query string — `view`, `status`, `type`,
	 * `severity`, `search`, `sort` / `order`.
	 *
	 * It must be the only one, for the reason `routes/invoices/+page.svelte`
	 * documents at length: SvelteKit's shallow `replaceState` writes `history`
	 * but never `page.url`, so a second writer rebuilding from `page.url`
	 * reads a frozen snapshot and deterministically drops whatever the first
	 * writer added. The previous `syncViewToUrl` mutated a copy of `page.url`,
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
			// The open dialog is part of the view, so a link can open it
			// (`/exceptions?id=` — the payment queue links a blocked row here).
			const openId = resolveTarget?.id ?? pendingOpenId;
			if (openId) params.set('id', openId);
			const qs = params.toString();
			replaceState(`${pageStore.url.pathname}${qs ? `?${qs}` : ''}`, {});
		});
	}

	let resolveTarget = $state<ExceptionItem | null>(null); // single-row resolve modal
	// `?id=` names an exception whose dialog the page opens once it has loaded
	// that row — the payment queue's "Open exception" link lands here.
	let pendingOpenId = $state<string | null>(pageStore.url.searchParams.get('id'));

	// "Record the ERP's payment" — the one-click path for a row holding a
	// payment the ERP reported and FeohLedger could not record (decisions
	// §319). Pre-filled from the row's `erp_report` when the dialog opens.
	let erpMethod = $state<PaymentMethod | ''>('');
	let erpReference = $state('');
	let erpPaidOn = $state('');
	let erpMaxDate = $state(localToday());
	let recordingErp = $state(false);
	let erpError = $state('');
	let bulkResolveOpen = $state(false);                    // bulk-resolve modal
	let resolutionText = $state('');
	let saving = $state(false);

	// Each type names a palette TONE, never a hex: the badge renders the
	// tone's calibrated `-tint` / `-on-tint` pair, which `tokenPairing.test.ts`
	// asserts in BOTH themes. These were literal hexes (`#f06464`, …) with a
	// `1f` alpha suffix concatenated on for the background — a pairing measured
	// on navy, and 2.5:1 once the page is light (docs/decisions.md §289).
	// Must cover the backend roster (`services/exception_lifecycle.EXCEPTION_TYPES`);
	// a missing key falls back to `muted`, which reads as "unclassified" next to
	// every colour-coded sibling — worst on the types that BLOCK a payment run.
	// Reuse a tone already in this map rather than introducing one.
	type TypeTone = 'danger' | 'warning' | 'violet' | 'muted';
	const TYPE_TONES: Record<string, TypeTone> = {
		duplicate: 'violet',
		po_mismatch: 'warning',
		fraud_flag: 'danger',
		extraction_failed: 'danger',
		unverified_vendor: 'warning',
		review_rejected: 'danger',
		amount_exceeded: 'danger',
		missing_data: 'warning',
		quality_hold: 'danger',
		price_variance: 'warning',
		erp_reconciliation: 'danger',
		contract_noncompliant: 'danger',
		line_total_mismatch: 'danger',
		payment_compliance_hold: 'danger',
	};

	// Total over `ExceptionSeverity`, the same pairing `EXCEPTION_STATUS_TONES`
	// has with its label map: a severity that gains a colour without a label —
	// a tinted cell printing a raw wire value — is a compile error. It stays
	// HERE rather than moving next to the label keys because it is the sibling
	// of `TYPE_TONES` above, and the contrast reasoning written there governs
	// both; `#lib/types/` carries vocabularies and tone NAMES
	// (`EXCEPTION_STATUS_TONES` is `BadgeTone`).
	const SEVERITY_TONES: Record<ExceptionSeverity, 'danger' | 'warning' | 'accent'> = {
		error: 'danger',
		warning: 'warning',
		info: 'accent',
	};

	/**
	 * A status' label. The badge printed the raw wire value (`open`,
	 * `escalated`) in a row whose every other translated cell sat beside a
	 * filter chip already reading `Offen` — the same status, named twice, a
	 * few pixels apart. It reads the chips' OWN keys, so the two cannot drift.
	 * An unrecognised status still prints raw rather than blank.
	 *
	 * The tone map moved to `#lib/types/exception` alongside those keys, so a
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

	/**
	 * The description in the reader's language — localized off the row's
	 * `description_code` / `description_params` (the invoice's own warning
	 * code, so the queue and the invoice agree in wording), with the server's
	 * English `description` as the fallback. A composite (several
	 * price-variance lines) comes back as a sentence plus its findings.
	 */
	function describe(exc: ExceptionItem): ExceptionDescriptionText | null {
		return exceptionDescriptionText(exc, m);
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

	// The × on the sorted column: back to the backend's default order.
	function handleClearSort() {
		const next = clearSort({ field: sortField, order: sortOrder });
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

	/**
	 * A navigation that REUSES this mounted page re-reads the URL — the sidebar's
	 * Exceptions row clicked on a filtered queue, or Back/Forward between two
	 * real entries of this route. The state above is seeded once at mount, so
	 * each of those used to change the address bar and nothing else (the
	 * `/invoices` hook documents the same defect). Shallow `replaceState`
	 * writes never fire this, so `syncUrl()` cannot loop back through it.
	 *
	 * Only what changed is assigned: a chip change re-runs the chip effect
	 * (which carries the new search and sort with it), a search change alone
	 * schedules the debounced reload, and a sort change alone reloads through
	 * `applySort`, because no effect tracks the sort.
	 */
	afterNavigate(({ from, to, type }) => {
		if (type === 'enter' || !from || !to || from.route.id !== to.route.id) return;
		const next = readUrlState(to.url.searchParams);
		if (next.view !== view) view = next.view;
		const chipsChanged =
			next.status !== statusFilter || next.type !== typeFilter || next.severity !== severityFilter;
		const searchChanged = next.search !== search;
		const sortChanged = next.sortField !== sortField || next.sortOrder !== sortOrder;
		if (searchChanged) search = next.search;
		if (chipsChanged) {
			sortField = next.sortField;
			sortOrder = next.sortOrder;
			statusFilter = next.status;
			typeFilter = next.type;
			severityFilter = next.severity;
		} else if (sortChanged) {
			applySort(next.sortField, next.sortOrder);
		}
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
		erpError = '';
		erpMaxDate = localToday();
		if (isErpPaidReport(exc.erp_report)) {
			const prefill = erpPrefill(exc.erp_report, erpMaxDate);
			erpMethod = prefill.method;
			erpReference = prefill.reference;
			erpPaidOn = prefill.paidOn;
		}
	}

	// The open dialog rides the URL (`?id=`), like every other piece of view
	// state here — so opening or closing it rewrites the query string.
	$effect(() => {
		void resolveTarget?.id;
		void pendingOpenId;
		syncUrl();
	});

	// Open the dialog a link asked for (`?id=`), once. The row comes from the
	// detail read, which carries the same verdicts as a list row, so the
	// dialog offers exactly what the server would accept.
	$effect(() => {
		const id = pendingOpenId;
		if (!id) return;
		untrack(async () => {
			try {
				openResolve(await api.get<ExceptionItem>(`/api/exceptions/${encodeURIComponent(id)}`));
			} catch {
				toast(m('exceptions.toast.notFound'), 'error');
			} finally {
				pendingOpenId = null;
			}
		});
	});

	/** The ERP's name for the record panel, in the reader's words when the
	 *  adapter has no product name. */
	function erpName(exc: ExceptionItem): string {
		return exc.erp_name ?? m('payments.recordOutside.yourErp');
	}

	/** A hold an ERP PO sync's re-check no longer finds stays open for a person
	 *  (decisions §323, amended); this is the line that says why they may now
	 *  clear it. Null on every other row. */
	function rematchNote(exc: ExceptionItem): string | null {
		return poRematchNote(exc.erp_report, erpName(exc), m, (day) => formatDate(day));
	}

	async function commitRecordErpPayment() {
		const target = resolveTarget;
		const reference = erpReference.trim();
		const method = erpMethod;
		if (!target || !method || !reference || !erpPaidOn) return;
		recordingErp = true;
		erpError = '';
		try {
			// No `amount`: the server records what the invoice owes on that date.
			await api.post(`/api/exceptions/${target.id}/record-erp-payment`, {
				method,
				reference,
				paid_on: erpPaidOn
			});
			toast(
				m('exceptions.erpPayment.recorded', { invoice: target.invoice_number ?? '—' }),
				'success'
			);
			resolveTarget = null;
			await Promise.all([loadExceptions(), loadSummary()]);
		} catch (err) {
			// In the dialog, beside the form (role="alert"): the coded refusal
			// names what is still in the way, already in the reader's language.
			erpError = err instanceof Error ? err.message : m('exceptions.erpPayment.failed');
		} finally {
			recordingErp = false;
		}
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
			// Selectable = some decision is open to the reader on the row (escalate
			// is refused only by the status and role gates). Bulk resolve still
			// reports a per-row segregation skip by name.
			exceptions
				.filter((e) => e.decision_refusals && Object.values(e.decision_refusals).some(isAllowed))
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
	function formatCurrency(n: MoneyAmount, currency: string | null): string {
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
		// Without a summary (still loading, or the tally request failed) the
		// chips still render, uncounted: the summary is "non-critical" to the
		// queue, but the chips are the only control that can change — or undo —
		// a severity the URL already applied to the table.
		const counts = summary?.by_severity;
		const known = EXCEPTION_SEVERITIES as readonly string[];
		const keys = counts
			? [...known, ...Object.keys(counts).filter((k) => !known.includes(k))]
			: [...known];
		return [
			{
				key: 'all',
				label: m('exceptions.filter.allSeverities'),
				count: counts ? Object.values(counts).reduce((sum, n) => sum + n, 0) : undefined
			},
			...keys.map((sev) => ({
				key: sev,
				label: severityLabel(sev),
				count: counts ? (counts[sev] ?? 0) : undefined
			}))
		];
	});

	/**
	 * The type row: every type the tallies found, plus the ACTIVE type even when
	 * it counts 0. The tallies are faceted over the search and the other rows, so
	 * a search can empty `by_type` — and a pressed type chip that dropped out of
	 * the row would leave the table narrowed by a filter nothing on screen shows
	 * or can undo (the `chipStatuses` rule on `/invoices`).
	 */
	let typeChipEntries = $derived.by((): [string, number | undefined][] => {
		// No summary → no type roster to list, but an ACTIVE type still gets its
		// chip (uncounted), so the filter stays visible and undoable.
		if (!summary) return typeFilter ? [[typeFilter, undefined]] : [];
		const entries: [string, number | undefined][] = Object.entries(summary.by_type);
		if (typeFilter && !(typeFilter in summary.by_type)) entries.push([typeFilter, 0]);
		return entries;
	});

	// The type row's "All types" key. `typeFilter` is `null` for "no type", and a
	// chip key is a string; the empty string cannot collide with a wire type.
	const ALL_TYPES = '';
	// "All types" is label-only (no tally), as it always was. Once the summary
	// has landed the row renders only when there is at least one type to choose
	// between — `typeChips.length > 1`; before then it holds its place (template).
	let typeChips = $derived([
		{ key: ALL_TYPES, label: m('exceptions.filter.allTypes') },
		...typeChipEntries.map(([type, count]) => ({ key: type, label: typeLabel(type), count }))
	]);

	/**
	 * The type row's writer. Pressing the already-pressed type clears it — the
	 * row's behaviour before it moved onto `<FilterChips>` (which only ever
	 * selects), kept so a click on a pressed chip still undoes it.
	 */
	function selectType(key: string) {
		typeFilter = key === ALL_TYPES || key === typeFilter ? null : key;
	}

	// Rendered with or without a summary, for the reason `severityChips` gives:
	// when the tally request failed, hiding the row stranded the operator on
	// whatever status the URL had applied (`?status=resolved`) with no control
	// on screen to leave it. Uncounted until the summary lands.
	let statusChips = $derived([
		{
			key: 'all',
			label: m('common.all'),
			count: summary
				? summary.open + summary.escalated + summary.resolved + summary.dismissed
				: undefined
		},
		{ key: 'open', label: m('exceptions.filter.open'), count: summary?.open },
		{ key: 'escalated', label: m('exceptions.filter.escalated'), count: summary?.escalated },
		{ key: 'resolved', label: m('exceptions.filter.resolved'), count: summary?.resolved },
		{ key: 'dismissed', label: m('exceptions.filter.dismissed'), count: summary?.dismissed }
	]);

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
	<!-- `queue-panel` gives the panel the shell's own vertical rhythm: the panel
	     is ONE flex item of `.workspace`, so the workspace gap stopped at its
	     edge and the chips, search box and table inside it sat flush against
	     each other. -->
	<div id="exc-panel-queue" class="queue-panel" role="tabpanel" aria-labelledby="exc-tab-queue">
		<!-- Three faceted chip rows over one table (ui-patterns § Status filter
		     chips), all three the same `<FilterChips>` control. The type row was
		     a hand-rolled copy with its own smaller, square, dot-marked chips and
		     an "All types" chip that pressed as an outline rather than a fill —
		     the same control drawn three ways, one line apart. -->
		<div class="queue-filters">
			<div class="help-row">
				<FilterChips chips={statusChips} bind:active={statusFilter} />
				<HelpTip term="exception" />
			</div>

			<!-- Drawn from first paint, holding only "All types" until the summary
			     says which types exist: gating it on the roster alone inserted the
			     row ~40px above the search box and the table once the summary
			     landed, on every visit (tests-e2e/reactivity/layout-stability.spec.ts).
			     It still goes when a LOADED summary leaves nothing to choose between. -->
			{#if summary === null || typeChips.length > 1}
				<!-- `data-testid`: the e2e suite's handle on THIS row, since every
				     row now renders the same `.filter-chip`. -->
				<div class="help-row" data-testid="exception-type-filters">
					<FilterChips chips={typeChips} bind:active={() => typeFilter ?? ALL_TYPES, selectType} />
					<!-- Which of these types stop a payment run is the question the
					     row of types raises; the tip answers it. -->
					<HelpTip term="payment-blocking-exception" />
				</div>
			{/if}

			<!-- The same `exceptions.severity.*` keys the row's Sev cell reads, so a
			     chip and the rows it filters cannot name one severity two ways. -->
			<FilterChips chips={severityChips} bind:active={severityFilter} />
		</div>

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

	<!-- `fixed` + per-column widths (ui-patterns § Data tables). Under the
	     default auto layout the table's minimum width was the sum of every
	     column's longest unbreakable run — an email in Assignee, an ERP
	     invoice number, a nowrap badge, the 180px actions column — and the
	     Type cell's `max-width` was a no-op (auto layout ignores it on a
	     cell). So one long value pushed the table past the content area and
	     the region scrolled Status and the Resolve / Invoice actions off its
	     right edge. Fixed layout sizes the columns from the header row; the
	     long single-line values clip with an ellipsis and keep the full text
	     in `title`. -->
	<div class="exc-table">
	<DataTable columns={COLUMNS} isEmpty={exceptions.length === 0} empty={emptyMessage} colspan={11} fixed>
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
					class="col-sev"
					field="severity"
					label={m('exceptions.col.severity')}
					active={sortField === 'severity'}
					order={sortOrder}
					onsort={handleSort} onclear={handleClearSort}
				/>
				<th class="col-invoice" scope="col">{m('exceptions.col.invoice')}</th>
				<th class="col-vendor" scope="col">{m('exceptions.col.vendor')}</th>
				<th class="right col-amount" scope="col">{m('exceptions.col.amount')}</th>
				<th class="col-assignee" scope="col">{m('exceptions.col.assignee')}</th>
				<!-- Sorts `created_at`, reported in AGE order — see `handleAgeSort`. -->
				<SortableHeader
					class="col-age"
					field={AGE_FIELD}
					label={m('exceptions.col.age')}
					active={sortField === AGE_FIELD}
					order={ageOrder}
					onsort={handleAgeSort} onclear={handleClearSort}
				/>
				<SortableHeader
					class="col-due"
					field="due_at"
					label={m('exceptions.col.due')}
					active={sortField === 'due_at'}
					order={sortOrder}
					onsort={handleSort} onclear={handleClearSort}
				/>
				<th class="col-status" scope="col">{m('exceptions.col.status')}</th>
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
							style="background:var(--{TYPE_TONES[exc.exception_type] ?? 'muted'}-tint);color:var(--{TYPE_TONES[exc.exception_type] ?? 'muted'}-on-tint)"
						>
							{typeLabel(exc.exception_type, exc.type_label)}
						</span>
						<!-- The description IS the triage datum — which invoice
						     it duplicates, which PO line the price missed. It was
						     reachable only by hovering the badge, so it existed
						     for neither a keyboard nor a touch operator, and a
						     queue could not be scanned without pointing at every
						     row in turn. Clamped to two lines with the full text
						     still in `title` — a composite's findings each on
						     their own line there, the sentence alone in the cell. -->
						{#if exc.description || exc.description_code}
							{@const desc = describe(exc)}
							{#if desc}
								<span class="type-detail" title={exceptionDescriptionTitle(desc)}>{desc.summary}</span>
							{/if}
						{/if}
						{#if rematchNote(exc)}
							<span class="type-detail rematch-note" title={rematchNote(exc)} data-testid="exception-rematch-note">{rematchNote(exc)}</span>
						{/if}
					</td>
					<td>
						<span
							class="severity"
							style="color:var(--{SEVERITY_TONES[exc.severity as ExceptionSeverity] ?? 'text-muted'})"
						>
							{severityLabel(exc.severity)}
						</span>
					</td>
					<td class="mono cell-clip" title={exc.invoice_number ?? undefined}>{exc.invoice_number ?? '—'}</td>
					<td class="muted-cell cell-clip" title={exc.vendor_name ?? undefined}>{exc.vendor_name ?? '—'}</td>
					<td class="mono right">{formatCurrency(exc.amount, exc.currency)}</td>
					<td class="muted-cell cell-clip" title={exc.assigned_to ?? undefined}>{exc.assigned_to ?? '—'}</td>
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
					<td class="status-cell">
						<Badge
							tone={exceptionStatusTone(exc.status)}
							variant="status-badge badge-{exc.status}"
						>
							{statusLabel(exc.status)}
						</Badge>
					</td>
					<td class="actions">
						<!-- Open the dialog when ANY decision is open to this reader —
						     escalate stays open to a refused actor, and is their exit. -->
						{#if exc.decision_refusals && Object.values(exc.decision_refusals).some(isAllowed)}
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
	</div>

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
		<div class="help-row title-row">
			<h2>{m('exceptions.resolveModal.title')}</h2>
			<HelpTip term="segregation-of-duties" />
		</div>
		<p class="modal-hint">
			<!-- `typeLabel`, not the server's `type_label`: the row badge one
			     click away is translated, and naming the same type two ways
			     across a confirm step is §149's defect in its last hiding place. -->
			<strong>{typeLabel(resolveTarget.exception_type, resolveTarget.type_label)}</strong>
			{#if resolveTarget.invoice_number}— {resolveTarget.invoice_number}{/if}
			{#if resolveTarget.vendor_name}· {resolveTarget.vendor_name}{/if}
		</p>
		{@const desc = describe(resolveTarget)}
		{#if desc}
			<div class="modal-description" data-testid="exception-description">
				<p>{desc.summary}</p>
				{#if desc.findings.length > 0}
					<ul>
						{#each desc.findings as finding, i (i)}
							<li>{finding}</li>
						{/each}
					</ul>
				{/if}
			</div>
		{/if}
		{#if rematchNote(resolveTarget)}
			<p class="modal-hint rematch-note" data-testid="exception-rematch-note-detail">
				{rematchNote(resolveTarget)}
			</p>
		{/if}
		{#if isErpPaidReport(resolveTarget.erp_report) && isOffered(resolveTarget.record_erp_payment_refusal)}
			<!-- The payment the ERP reported, pre-filled, recorded in one step.
			     Resolving this exception and recording the payment happen in one
			     transaction server-side, so neither can land without the other. -->
			<section class="erp-payment" data-testid="erp-payment-panel" aria-labelledby="erp-payment-title">
				<h3 id="erp-payment-title">
					{m('exceptions.erpPayment.title', { erp: erpName(resolveTarget) })}
				</h3>
				<p class="modal-hint">{m('exceptions.erpPayment.intro')}</p>
				<form
					data-testid="erp-payment-form"
					onsubmit={(e) => { e.preventDefault(); commitRecordErpPayment(); }}
				>
					<RecordPaymentFields
						bind:method={erpMethod}
						bind:reference={erpReference}
						bind:paidOn={erpPaidOn}
						maxDate={erpMaxDate}
						idPrefix="erp-payment"
						testid="erp-payment"
					/>
					{#if erpError}
						<p class="modal-error" role="alert" data-testid="erp-payment-error">{erpError}</p>
					{/if}
					<div class="modal-footer">
						<button
							type="submit"
							class="btn-primary"
							data-testid="erp-payment-submit"
							aria-describedby={resolveTarget.record_erp_payment_refusal
								? 'erp-payment-refusal'
								: undefined}
							disabled={recordingErp ||
								!isAllowed(resolveTarget.record_erp_payment_refusal) ||
								!erpMethod ||
								!erpReference.trim() ||
								!erpPaidOn ||
								erpPaidOn > erpMaxDate}
						>
							{recordingErp ? m('exceptions.erpPayment.recording') : m('exceptions.erpPayment.record')}
						</button>
					</div>
					{#if resolveTarget.record_erp_payment_refusal}
						<p class="modal-hint" id="erp-payment-refusal" data-testid="erp-payment-refusal">
							{refusalText(resolveTarget.record_erp_payment_refusal, m)}
						</p>
					{/if}
				</form>
			</section>
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
					disabled={saving || !isAllowed(resolveTarget.decision_refusals?.dismiss)}
					onclick={() => commitResolve('dismiss')}
				>
					{m('exceptions.resolveModal.dismiss')}
				</button>
				<button
					type="button"
					class="btn-warning"
					disabled={saving ||
						!resolutionText.trim() ||
						!isAllowed(resolveTarget.decision_refusals?.escalate)}
					onclick={() => commitResolve('escalate')}
				>
					{m('exceptions.resolveModal.escalate')}
				</button>
				<button
					type="submit"
					class="btn-primary"
					disabled={saving ||
						!resolutionText.trim() ||
						!isAllowed(resolveTarget.decision_refusals?.resolve)}
				>
					{saving ? m('common.saving') : m('exceptions.resolveModal.resolve')}
				</button>
			</div>
			{#if resolveTarget.decision_refusals?.resolve && isOffered(resolveTarget.decision_refusals.resolve)}
				<!-- Why Resolve / Dismiss are disabled, in the server's words: a
				     colleague must clear this one; escalating it is still open. -->
				<p class="modal-hint" data-testid="exception-decision-refusal">
					{refusalText(resolveTarget.decision_refusals.resolve, m)}
				</p>
			{/if}
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

	/* --- Queue panel --- */

	/* The workspace's own column rhythm, continued inside the tab panel. */
	.queue-panel {
		display: flex;
		flex-direction: column;
		gap: 16px;
		min-width: 0;
	}

	/* The three chip rows read as one filter block: tighter to each other than
	   to the search box and table below them. */
	.queue-filters {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	/* The resolve dialog's heading row (global `.help-row`) keeps the
	   heading's spacing; the heading's own modal margin is zeroed below. */
	.title-row {
		margin-bottom: 4px;
	}

	.title-row h2 {
		margin: 0;
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
		background: color-mix(in srgb, var(--text-muted) 5%, transparent);
	}

	/* --- Queue table column widths ---
	   `DataTable fixed`: the columns are sized from these header widths, never
	   from the longest value in some row. The predictable columns are sized to
	   their widest content in the six locales (fr "AVERTISSEMENT", a
	   seven-figure amount, "Résoudre" + "Facture"). Invoice and Vendor take a
	   share of the width, so they gain room on a wide window; Type carries no
	   width and takes the rest — it is the column with the most to say.

	   Eleven columns is one more than any other list here, so this table runs
	   10px of horizontal cell padding instead of the global 14px: that 88px is
	   what lets the whole row, actions included, sit inside the content area
	   at a 1666px window with the sidebar open (a 1390px region). Below the
	   min-width the table scrolls inside its focusable region (WCAG 1.4.10)
	   rather than crushing Type and Vendor. Widths are under `.exc-table` and
	   `:global` because `SortableHeader` renders its `<th>` from another
	   component (the `/vendors` arrangement). */
	.exc-table :global(table) {
		min-width: 1380px;
	}
	.exc-table :global(th),
	.exc-table :global(td) {
		padding-left: 10px;
		padding-right: 10px;
	}
	/* A header that outgrows its column (fr "ANCIENNETÉ" with the sort arrow
	   and its ×) wraps inside it instead of running into the next one. */
	.exc-table :global(th) {
		white-space: normal;
	}
	.exc-table :global(th.checkbox-col) { width: 40px; }
	.exc-table :global(th.col-sev) { width: 116px; }
	.exc-table :global(th.col-invoice) { width: 10%; }
	.exc-table :global(th.col-vendor) { width: 11%; }
	.exc-table :global(th.col-amount) { width: 132px; }
	.exc-table :global(th.col-assignee) { width: 116px; }
	.exc-table :global(th.col-age) { width: 104px; }
	.exc-table :global(th.col-due) { width: 100px; }
	.exc-table :global(th.col-status) { width: 120px; }
	.exc-table :global(th.actions-col) { width: 184px; }

	/* A status longer than its column (ja "エスカレーション済み") wraps inside
	   the pill rather than running under the row actions. */
	.status-cell :global(.badge) {
		white-space: normal;
	}

	/* The checkbox column hugs its box; the 24px target floor is untouched. */
	.exc-table :global(.checkbox-col) {
		padding-right: 0;
	}

	/* One line, clipped to the column: an ERP invoice number, a long vendor
	   name or an email assignee ends in "…" (full value in `title`) instead of
	   widening the table or running under the next column. */
	.cell-clip {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
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
		/* A fixed column never grows to fit, so a long translated type name
		   clips inside the pill rather than spilling into Sev. */
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		vertical-align: top;
	}

	/* The type cell carries the badge plus the finding itself, so it is the
	   one column allowed to wrap — within the width the fixed layout gives
	   it. (It used to carry `max-width: 320px`, which auto table layout
	   ignores on a cell.) `overflow-wrap` breaks an unbroken run in a
	   finding — a URL, a PO reference — inside the column. */
	.type-detail {
		overflow-wrap: anywhere;
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

	/* --- Modal extras --- */

	.modal-description {
		font-size: 0.82rem;
		color: var(--text);
		margin: 0 0 14px;
		padding: 8px 10px;
		background: var(--bg);
		border-radius: 4px;
	}

	.modal-description p {
		margin: 0;
	}

	/* The ERP-reported payment, recorded from the dialog (decisions §319). */
	.erp-payment {
		margin: 0 0 16px;
		padding: 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
	}

	.erp-payment h3 {
		margin: 0 0 6px;
		font-size: 0.9rem;
	}

	.erp-payment form {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.modal-error {
		margin: 0;
		padding: 8px 10px;
		border: 1px solid var(--danger);
		border-radius: 6px;
		background: var(--danger-tint);
		color: var(--text);
		font-size: 0.82rem;
	}

	.modal-description ul {
		margin: 6px 0 0;
		padding-left: 18px;
	}

	.modal input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
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
		border: 1px solid var(--warning);
		background: var(--surface);
		color: var(--warning);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-warning:hover:not(:disabled) {
		background: color-mix(in srgb, var(--warning) 10%, transparent);
	}

	.btn-secondary:disabled,
	.btn-warning:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
</style>
