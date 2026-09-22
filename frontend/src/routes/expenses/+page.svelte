<script lang="ts">
	import type {
		Expense,
		ExpenseStatus,
		ExpenseReportStatus,
		ExpensePreapprovalStatus,
		ReconciliationStatus,
		ExpenseReport,
		ExpenseReportSummary,
		ExpensePolicy,
		ExpensePreapproval,
		PolicyViolation,
		CorporateCardTransaction,
		CardMatchSuggestion,
		ExpenseSummary
	} from '$lib/types/expense';
	import {
		EXPENSE_FILTER_STATUSES,
		EXPENSE_STATUS_LABEL_KEYS,
		expenseStatusLabelKey,
		EXPENSE_STATUS_TONES,
		expenseReportStatusLabelKey,
		EXPENSE_REPORT_STATUS_TONES,
		EXPENSE_PREAPPROVAL_STATUSES,
		expensePreapprovalStatusLabelKey,
		EXPENSE_PREAPPROVAL_STATUS_TONES,
		RECONCILIATION_STATUSES,
		reconciliationStatusLabelKey,
		RECONCILIATION_STATUS_TONES
	} from '$lib/types/expense';
	import { expenseStore } from '$lib/stores/expenses.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import {
		listGlAccounts,
		exportExpensesCsv,
		bulkGlCode,
		listExpenseReports,
		createExpenseReport,
		getExpenseReport,
		attachExpenses,
		expenseReportSummary,
		deleteExpense as apiDeleteExpense,
		listPolicies,
		deletePolicy as apiDeletePolicy,
		listPreapprovals,
		createPreapproval,
		approvePreapproval,
		rejectPreapproval,
		submitReport as apiSubmitReport,
		approveReport,
		rejectReport,
		listCardTransactions,
		importCardCsv,
		syncVirtualCards,
		cardMatchSuggestions,
		matchCardTxn,
		unmatchCardTxn,
		ignoreCardTxn,
		createExpenseFromCard,
		getExpenseSummary,
		getExpenseIds,
		type ExpenseListParams,
		type GlAccountOption
	} from '$lib/api/expenses';
	import Badge from '$lib/components/ui/Badge.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import PolicyModal from '$lib/components/modals/PolicyModal.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import BulkBar from '$lib/components/ui/BulkBar.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import { DEFAULT_CURRENCY, formatMoney } from '$lib/utils/money';
	import { formatCurrencyTotals } from '$lib/utils/currencyGroups';
	import { formatDate } from '$lib/utils/time';
	import ExpenseModal from '$lib/components/modals/ExpenseModal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { pruneSelection } from '$lib/utils/selection';
	import { page } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import { untrack } from 'svelte';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { appendUnique } from '$lib/utils/pagination';
	import { m } from '$lib/i18n/store.svelte';
	import { normalizeMoneyInput } from '$lib/utils/moneyInput';
	import SortableHeader from '$lib/components/ui/SortableHeader.svelte';
	import { toggleSort, type SortOrder } from '$lib/utils/sort';

	const canCreate = $derived(auth.hasAnyRole('admin', 'ap_manager', 'ap_clerk'));
	// Policy CRUD + report/pre-approval REJECT = admin | ap_manager.
	const canManagePolicies = $derived(auth.isManager);
	// Report APPROVE is admin | ap_manager | cfo — and above the org's
	// `expense_approval.cfo_threshold` the backend demands cfo/admin, so the CFO
	// must be able to see the button. `auth.isCfo` = admin|cfo, so the union with
	// `isManager` is exactly those three roles.
	const canApproveReports = $derived(auth.isManager || auth.isCfo);

	// --- Tabs ---
	type Tab = 'expenses' | 'reports' | 'policies' | 'preapprovals' | 'cards';
	let tab = $state<Tab>(($page.url.searchParams.get('tab') as Tab) ?? 'expenses');

	// --- Expenses tab filter state (URL-backed) ---
	let search = $state($page.url.searchParams.get('search') ?? '');
	// The search term the newest issued list request carried. Written by
	// `loadExpenses()`, read by the debounce effect — see the comment there.
	let appliedSearch = $state(($page.url.searchParams.get('search') ?? '').trim());
	let statusFilter = $state<string>($page.url.searchParams.get('status') ?? 'all');
	// Column sort (expenses tab only) — URL-backed via the same `syncUrl()`
	// below. `null` field = the backend's own default order (most-recent first).
	let sortField = $state<string | null>($page.url.searchParams.get('sort'));
	let sortOrder = $state<SortOrder>(($page.url.searchParams.get('order') as SortOrder) ?? 'desc');

	// --- Modal + selection state ---
	let showCreate = $state(false);
	let editing = $state<Expense | null>(null);
	let selected = $state<Set<string>>(new Set());
	let confirmDeleteId = $state<string | null>(null);

	let glAccounts = $state<GlAccountOption[]>([]);
	let bulkGl = $state('');
	let bulkBusy = $state(false);
	// True once "Select all N matching" has resolved the whole filtered set
	// (not just the loaded page) into `selected`. See the identical
	// mechanism (and full rationale) on the invoices list page's
	// `selectAllMatching`. Reset by the status-filter and search effects
	// whenever the filters actually change.
	let selectedAllMatching = $state(false);
	let selectingAllMatching = $state(false);

	// Chip statuses = the reachable subset ∪ whatever is ACTIVE. The subset omits
	// `rejected` and `reimbursed` because nothing in the backend ever assigns
	// them (see EXPENSE_FILTER_STATUSES for the enumerated writers — report
	// rejection returns children to `draft`, and there is no reimbursement
	// transition at all), so those chips were filters that returned an empty
	// list forever. The union with the active status is the same
	// `quick subset ∪ active` rule /invoices uses (frontend/CLAUDE.md § Status
	// filter chips): a bookmarked `?status=reimbursed` — or a demo tenant seeded
	// with such rows — still renders its chip, so an active filter is never
	// invisible and the user can always click back to All.
	const chipStatuses = $derived.by(() => {
		const active = statusFilter as ExpenseStatus;
		if (EXPENSE_FILTER_STATUSES.includes(active) || !(active in EXPENSE_STATUS_LABEL_KEYS))
			return EXPENSE_FILTER_STATUSES;
		return [...EXPENSE_FILTER_STATUSES, active];
	});

	// Every status name below is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = (s: string) => {
		const key = expenseStatusLabelKey(s);
		return key ? m(key) : s;
	};
	const reportStatusLabel = (s: string) => {
		const key = expenseReportStatusLabelKey(s);
		return key ? m(key) : s;
	};
	const preapprovalStatusLabel = (s: string) => {
		const key = expensePreapprovalStatusLabelKey(s);
		return key ? m(key) : s;
	};
	const reconStatusLabel = (s: string) => {
		const key = reconciliationStatusLabelKey(s);
		return key ? m(key) : s;
	};

	const STATUS_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		...chipStatuses.map((s) => ({ key: s, label: statusLabel(s) }))
	]);

	const COLUMNS = $derived([
		{ label: '', class: 'checkbox-col' },
		{ label: m('expenses.col.date') },
		{ label: m('expenses.col.merchant') },
		{ label: m('expenses.col.category') },
		{ label: m('expenses.col.gl') },
		{ label: m('expenses.col.amount'), class: 'right' },
		{ label: m('expenses.col.status') },
		{ label: '', class: 'actions-col' }
	]);

	// KPIs, from `GET /api/expenses/summary` — the WHOLE filtered set, not the
	// loaded page.
	//
	// They used to reduce over `expenseStore.all`, which holds 20 rows (plus
	// whatever Load-more appended). So "Period total" summed a page while the
	// "Expenses" card beside it showed the server's whole-set `total`, and the
	// two contradicted each other the moment a tenant had 21 expenses. The
	// rollup runs the SAME status/search filters server-side, so all three cards
	// and the table describe one set.
	//
	// The old sum was wrong a second way: it added every row's bare `amount`
	// across currencies and rendered the result in the org default, so EUR 100 +
	// USD 100 read as one meaningless "200". The rollup groups by currency and
	// never adds across them — the rule `/payments` already applies via
	// `groupAmountsByCurrency` (`$lib/utils/currencyGroups`).
	let expenseSummary = $state<ExpenseSummary | null>(null);

	// Its own sequencer, not the store's: this is an independent request that
	// can be in flight while the list's is, and one shared counter would let
	// either blank the other (frontend/CLAUDE.md § Sequencing list fetches).
	const summarySequence = createRequestSequencer();

	async function loadExpenseSummary() {
		const token = summarySequence.start();
		try {
			const res = await getExpenseSummary(buildParams());
			if (!summarySequence.canCommit(token)) return;
			expenseSummary = res;
		} catch {
			// Non-fatal: the KPI cards fall back to a placeholder rather than
			// taking the page down. The table has its own error state.
			if (summarySequence.isCurrentRequest(token)) expenseSummary = null;
		}
	}

	// One subtotal per currency, side by side — never added together; an
	// unestablished currency renders bare rather than joining the org's
	// (decisions §200). Mirrors `formatGroups` on /payments.
	const periodTotalGroups = $derived(formatCurrencyTotals(expenseSummary?.by_currency ?? []));
	// The org's code labels only the zero of a summary that landed empty. One
	// that has not landed is no figure — `null`, which `KpiCard` draws as its
	// dash — never a zero wearing a currency nobody computed.
	const periodTotal = $derived(
		expenseSummary
			? (periodTotalGroups[0] ?? formatMoney(0, { currency: orgCurrency.currency }))
			: null
	);
	// Currencies past the first ride the card's muted sub-line, so the headline
	// stays one readable figure while nothing is silently dropped.
	const periodTotalRest = $derived(
		periodTotalGroups.length > 1 ? periodTotalGroups.slice(1).join(' · ') : null
	);
	const pendingCount = $derived(
		(expenseSummary?.by_status.draft ?? 0) + (expenseSummary?.by_status.submitted ?? 0)
	);

	// The live search term, read WITHOUT registering a dependency.
	//
	// `buildParams()` / `loadExpenses()` are called synchronously from the
	// statusFilter `$effect` below, and Svelte tracks reads transitively through
	// called functions — so a plain `search` read would make that effect depend
	// on `search` too, firing an immediate un-debounced load on every keystroke
	// (issue #168, the very thing `syncUrl`'s comment says was fixed on
	// /invoices, /payments and /vendors; `routes/vendors/+page.svelte` untracks
	// the same read for the same reason). Worse here than there: `loadExpenses()`
	// stamps `appliedSearch` first, so the debounce timer would then
	// short-circuit and the keystroke fetch would be the ONLY one.
	function currentSearchTerm(): string {
		return untrack(() => search).trim();
	}

	// The filters a user sets on this page: status + the free-text term. Both
	// are SERVER filters — `GET /api/expenses` ILIKEs merchant / description /
	// category, the columns this table renders — so the whole filtered set is
	// searched instead of the page already loaded, and `/export` runs the same
	// three through the same backend filter builder. One object feeds the list,
	// the KPI rollup, the select-all resolver AND the CSV, so the file can only
	// ever be the rows on screen.
	function buildFilterParams(): ExpenseListParams {
		const params: ExpenseListParams = {};
		if (statusFilter !== 'all') params.status = statusFilter;
		const term = currentSearchTerm();
		if (term) params.search = term;
		return params;
	}

	// …plus the sort, which only the list declares (`/export`, `/summary` and
	// `/ids` would silently drop it).
	function buildParams(): ExpenseListParams {
		const params: ExpenseListParams = buildFilterParams();
		// Same `untrack` reasoning as `currentSearchTerm()` — this is called
		// synchronously from effects that must not depend on the sort state.
		const currentSort = untrack(() => sortField);
		if (currentSort) {
			params.sort = currentSort;
			params.order = untrack(() => sortOrder);
		}
		return params;
	}

	// Issues a fresh page-1 list load and records the term it carried, so the
	// debounce effect below can tell a term that is already on screen from one
	// that still needs a fetch.
	function loadExpenses() {
		appliedSearch = currentSearchTerm();
		// Fire-and-forget: the store loaders re-throw so an awaiting caller keeps
		// its own handling, but nothing awaits here — the store's `errored` flag is
		// what the UI renders. Swallow so a failed load isn't an unhandled rejection.
		expenseStore.fetch(buildParams()).catch(() => {}); // noqa: raw-fetch-in-component — store method, routes through api client
		// The KPI rollup answers a different query, so it is issued alongside
		// rather than derived from the page that just came back.
		void loadExpenseSummary();
	}

	// Reflect the live filter state into the URL. EVERY read in here is
	// untracked, `$page.url` included, because syncUrl() is a WRITER called
	// from the filter `$effect`s below — not a source of dependencies:
	//   - the URL read would self-trigger the effect that writes it via
	//     replaceState (Svelte effect_update_depth_exceeded);
	//   - a tracked `search` read would make every filter effect depend on
	//     `search`, so each keystroke re-fired it: an immediate, un-debounced
	//     load racing the dedicated 300ms debounce timer. That is issue #168,
	//     fixed on /invoices, /payments and /vendors but never carried to this
	//     page. Each effect declares the filters it actually depends on by
	//     reading them directly, so nothing here needs to be tracked.
	function syncUrl() {
		untrack(() => {
			const url = new URL($page.url);
			if (tab !== 'expenses') url.searchParams.set('tab', tab);
			else url.searchParams.delete('tab');
			if (statusFilter !== 'all') url.searchParams.set('status', statusFilter);
			else url.searchParams.delete('status');
			if (search.trim()) url.searchParams.set('search', search.trim());
			else url.searchParams.delete('search');
			// Pre-approval status filter only belongs in the URL while on that tab.
			if (tab === 'preapprovals' && preapprovalStatus !== 'all')
				url.searchParams.set('pa_status', preapprovalStatus);
			else url.searchParams.delete('pa_status');
			// Reconciliation status filter only belongs in the URL while on the cards tab.
			if (tab === 'cards' && reconFilter !== 'all') url.searchParams.set('recon', reconFilter);
			else url.searchParams.delete('recon');
			if (sortField) {
				url.searchParams.set('sort', sortField);
				url.searchParams.set('order', sortOrder);
			} else {
				url.searchParams.delete('sort');
				url.searchParams.delete('order');
			}
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	function handleSort(field: string) {
		const next = toggleSort({ field: sortField, order: sortOrder }, field);
		sortField = next.field;
		sortOrder = next.order;
		syncUrl();
		loadExpenses();
	}

	// A keystroke now costs a request, so the term is debounced 300ms (the
	// /invoices, /payments, /vendors convention) and the store's own sequencer
	// discards a slow response for an earlier term. `appliedSearch` is the term
	// the newest ISSUED load used: re-running with a term that already matches
	// it schedules nothing, which is what keeps the effect's first run (mount,
	// including a bookmarked `?search=`) from firing a duplicate load 300ms
	// behind the status effect's — and cancels a pending debounce when a chip
	// click has already loaded with the typed term.
	let searchTimer: ReturnType<typeof setTimeout>;
	$effect(() => {
		const next = search.trim();
		clearTimeout(searchTimer);
		if (next === appliedSearch) return;
		// The "select all N matching" set was resolved against the OLD search
		// term; once it changes the set no longer describes anything real.
		// See the identical mechanism on the invoices list page.
		selectedAllMatching = false;
		searchTimer = setTimeout(() => {
			syncUrl();
			loadExpenses();
		}, 300);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone, running syncUrl()/a list fetch against a route
		// the user already left.
		return () => clearTimeout(searchTimer);
	});

	// A chip click is a discrete action — it loads immediately, no debounce.
	$effect(() => {
		statusFilter;
		selectedAllMatching = false;
		syncUrl();
		loadExpenses();
	});

	$effect(() => {
		orgCurrency.ensureLoaded();
		loadGlAccounts();
	});

	async function loadGlAccounts() {
		try {
			glAccounts = await listGlAccounts();
		} catch {
			/* non-critical for the list view */
		}
	}

	function glLabel(id: string | null): string {
		if (!id) return '—';
		const g = glAccounts.find((a) => a.id === id);
		return g ? g.code : '—';
	}

	// --- Selection ---
	// Keep the selection ⊆ the rows actually loaded (a status or search refetch
	// replaces them). Otherwise stale ids inflate the bulk-bar count, break the
	// select-all `size === length` comparison, and feed invisible ids into the
	// bulk GL re-code.
	$effect(() => {
		if (selectedAllMatching) return;
		const pruned = pruneSelection(
			selected,
			expenseStore.all.map((e) => e.id)
		);
		if (pruned !== selected) selected = pruned;
	});

	function toggleSelect(id: string) {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	let allSelected = $derived(
		expenseStore.all.length > 0 && selected.size === expenseStore.all.length
	);

	function toggleSelectAll() {
		if (allSelected) {
			selected = new Set();
			selectedAllMatching = false;
		} else {
			selected = new Set(expenseStore.all.map((e) => e.id));
		}
	}

	// Resolve and select EVERY expense matching the current list filters (not
	// just the loaded page) via `GET /api/expenses/ids`. The header checkbox
	// above only ever covers `expenseStore.all` — see the identical mechanism
	// (and full rationale) on the invoices list page's `selectAllMatching`.
	async function selectAllMatching() {
		selectingAllMatching = true;
		try {
			const res = await getExpenseIds(buildParams());
			selected = new Set(res.ids);
			selectedAllMatching = true;
			if (res.truncated) {
				toast(
					`Selected the first ${res.ids.length} of ${res.total} matching — narrow your filters to select the rest.`,
					'error'
				);
			} else {
				toast(`Selected all ${res.ids.length} matching expense(s)`, 'success');
			}
		} catch {
			toast('Failed to select all matching', 'error');
		} finally {
			selectingAllMatching = false;
		}
	}

	async function applyBulkGl() {
		if (selected.size === 0) return;
		bulkBusy = true;
		try {
			const res = await bulkGlCode([...selected], bulkGl || null);
			const msg = res.skipped?.length
				? `${m('expenses.toast.glCoded', { n: res.updated })} (skipped ${res.skipped.length})`
				: m('expenses.toast.glCoded', { n: res.updated });
			toast(msg, 'success');
			selected = new Set();
			selectedAllMatching = false;
			bulkGl = '';
			// Records the term like `loadExpenses()` does — this refetch IS the
			// newest issued load, so a debounce still pending for the same term
			// must not fire a second one behind it. Awaited (not fire-and-forget)
			// so a failed refresh reaches the catch below.
			appliedSearch = currentSearchTerm();
			await expenseStore.fetch(buildParams()); // noqa: raw-fetch-in-component — store method, routes through api client
			void loadExpenseSummary();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.toast.bulkGlFailed'), 'error');
		} finally {
			bulkBusy = false;
		}
	}

	// --- Per-row delete ---
	async function deleteExpense(id: string) {
		try {
			await apiDeleteExpense(id);
			expenseStore.remove(id);
			// The row left the set the KPI row describes — re-ask rather than
			// leave a stale total on screen.
			void loadExpenseSummary();
			toast(m('expenses.toast.deleted'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.toast.deleteFailed'), 'error');
		} finally {
			confirmDeleteId = null;
		}
	}

	function onSaved(e: Expense) {
		expenseStore.upsert(e);
		// A create adds to the set and an edit can change its amount, currency or
		// status — all three move the KPI rollup, which is server-side.
		void loadExpenseSummary();
		if (editing && editing.id === e.id) editing = e;
	}

	// Outside-click un-arms any pending armed-confirm (expense delete, policy
	// delete, pre-approval reject) when the click lands outside a row action.
	function onWindowClick(e: MouseEvent) {
		const target = e.target as Element | null;
		if (!target?.closest('.row-action')) {
			if (confirmDeleteId) confirmDeleteId = null;
			if (confirmDeletePolicyId) confirmDeletePolicyId = null;
			if (paRejectArmedId) paRejectArmedId = null;
		}
	}

	// ============================ Reports tab ============================
	const REPORTS_PAGE_SIZE = 50;
	let reports = $state<ExpenseReport[]>([]);
	let reportsLoading = $state(false);
	let reportsTotal = $state(0);
	let reportsPage = $state(1);
	let reportsLoadingMore = $state(false);

	// `reportsTotal` is the whole set, so the footer may only claim to be
	// showing all of it once every row is actually loaded.
	let reportsHasMore = $derived(reports.length < reportsTotal);
	let activeReport = $state<ExpenseReport | null>(null);
	let activeSummary = $state<ExpenseReportSummary | null>(null);
	// The summary is its OWN fetch, separate from the report body, so the KPI
	// row it feeds needs its own in-flight flag — `reportBusy` tracks the
	// lifecycle mutations, not this read. See the row's comment for why.
	let summaryLoading = $state(false);
	let showNewReport = $state(false);
	let newReportNumber = $state('');
	let newReportTitle = $state('');
	let reportBusy = $state(false);
	let attachId = $state('');

	// One sequencer per sub-list on this page (reports / policies / pre-approvals
	// / card transactions), never one shared counter: they are four independent
	// requests, and a shared counter would let a tab switch mark another list's
	// in-flight response un-committable and blank it. The expenses tab itself is
	// sequenced inside `expenseStore`. See `frontend/CLAUDE.md` § Sequencing list
	// fetches.
	//
	// Reports has no local-mutation helper — every submit/approve/reject/attach
	// re-fetches through `loadReports()` — so it needs no `supersedeInFlight()`;
	// the sequencer here only stops two of those refreshes landing out of order.
	// Three states, not one: a failed load must not read as "nothing matched".
	// The search term reaches the server, so once the fetch settles the plain
	// empty message is a true statement about the whole filtered set.
	let expensesEmptyMessage = $derived(
		expenseStore.loading
			? m('expenses.loading')
			: expenseStore.errored
				? m('expenses.empty.errored')
				: m('expenses.empty')
	);

	const reportsSequence = createRequestSequencer();

	async function loadReports(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? reportsPage + 1 : 1;
		const token = reportsSequence.start();
		if (opts.append) reportsLoadingMore = true;
		else reportsLoading = true;
		try {
			const res = await listExpenseReports({
				page: nextPage,
				page_size: REPORTS_PAGE_SIZE
			});
			if (!reportsSequence.canCommit(token)) return;
			reports = opts.append ? appendUnique(reports, res.items) : res.items;
			reportsTotal = res.total;
			reportsPage = nextPage;
		} finally {
			if (reportsSequence.isCurrentRequest(token)) {
				reportsLoading = false;
				reportsLoadingMore = false;
			}
		}
	}

	// Load reports whenever the Reports tab is active.
	$effect(() => {
		if (tab === 'reports') loadReports();
	});

	async function openReport(r: ExpenseReport) {
		// Set BEFORE the first await, so the summary row renders `pending` from
		// the frame the detail view opens rather than spending one frame in the
		// `unavailable` state — the report id is known here, so the fetch is
		// certain to happen.
		summaryLoading = true;
		try {
			activeReport = await getExpenseReport(r.id);
		} catch {
			activeReport = r;
		}
		await refreshSummary();
	}

	async function refreshSummary() {
		if (!activeReport) {
			summaryLoading = false;
			return;
		}
		summaryLoading = true;
		try {
			activeSummary = await expenseReportSummary(activeReport.id);
		} catch {
			activeSummary = null;
		} finally {
			summaryLoading = false;
		}
	}

	function closeReport() {
		activeReport = null;
		activeSummary = null;
		summaryLoading = false;
	}

	async function handleNewReport() {
		if (!newReportNumber.trim()) return;
		reportBusy = true;
		try {
			const created = await createExpenseReport({
				report_number: newReportNumber.trim(),
				title: newReportTitle.trim() || null,
				// A new record's initial value, not a label: the org's code when
				// it resolved, else the platform default the backend would pick.
				currency: orgCurrency.currency ?? DEFAULT_CURRENCY,
				notes: null
			});
			toast(m('expenses.reports.toast.created'), 'success');
			showNewReport = false;
			newReportNumber = '';
			newReportTitle = '';
			await loadReports();
			await openReport(created);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.reports.toast.createFailed'), 'error');
		} finally {
			reportBusy = false;
		}
	}

	async function attachToReport() {
		if (!activeReport || !attachId.trim()) return;
		reportBusy = true;
		try {
			const updated = await attachExpenses(activeReport.id, [attachId.trim()], false);
			activeReport = updated;
			attachId = '';
			toast(m('expenses.reports.toast.attached'), 'success');
			await refreshSummary();
			await loadReports();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.reports.toast.attachFailed'), 'error');
		} finally {
			reportBusy = false;
		}
	}

	async function detachFromReport(expenseId: string) {
		if (!activeReport) return;
		reportBusy = true;
		try {
			const updated = await attachExpenses(activeReport.id, [expenseId], true);
			activeReport = updated;
			toast(m('expenses.reports.toast.detached'), 'success');
			await refreshSummary();
			await loadReports();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.reports.toast.detachFailed'), 'error');
		} finally {
			reportBusy = false;
		}
	}

	// Blocking policy violations returned by the WF3 submit route (422). Rendered
	// as an inline panel above the report detail; cleared on a clean submit.
	let submitViolations = $state<PolicyViolation[]>([]);

	async function submitReport() {
		if (!activeReport) return;
		reportBusy = true;
		submitViolations = [];
		try {
			// draft → submitted via the real WF3 transition. A 422 returns the
			// blocking policy-violation list (missing required receipt, absent
			// pre-approval) without transitioning — surfaced inline + as a toast.
			const result = await apiSubmitReport(activeReport.id);
			if (result.ok) {
				activeReport = result.report;
				toast(m('expenses.reports.toast.submitted'), 'success');
				await refreshSummary();
				await loadReports();
			} else {
				submitViolations = result.violations;
				toast(result.message || m('expenses.reports.toast.submitBlocked'), 'warning');
			}
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.reports.toast.submitFailed'), 'error');
		} finally {
			reportBusy = false;
		}
	}

	async function approveActiveReport() {
		if (!activeReport) return;
		reportBusy = true;
		try {
			const updated = await approveReport(activeReport.id);
			activeReport = updated;
			toast(m('expenses.reports.toast.approved'), 'success');
			await refreshSummary();
			await loadReports();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.reports.toast.approveFailed'), 'error');
		} finally {
			reportBusy = false;
		}
	}

	let rejectReason = $state('');
	let showReject = $state(false);

	async function rejectActiveReport() {
		if (!activeReport) return;
		reportBusy = true;
		try {
			const updated = await rejectReport(activeReport.id, rejectReason.trim());
			activeReport = updated;
			showReject = false;
			rejectReason = '';
			toast(m('expenses.reports.toast.rejected'), 'success');
			await refreshSummary();
			await loadReports();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.reports.toast.rejectFailed'), 'error');
		} finally {
			reportBusy = false;
		}
	}

	// Reject is admin | ap_manager (`POST /expense-reports/{id}/reject`), and never
	// by the report's own owner (segregation of duties — decider != submitter).
	function canDecideReport(r: ExpenseReport): boolean {
		return canManagePolicies && r.status === 'submitted' && auth.user?.id !== r.employee_user_id;
	}

	// Approve is a WIDER role set than reject: admin | ap_manager | cfo
	// (`POST /expense-reports/{id}/approve`). Above
	// `settings.expense_approval.cfo_threshold` the backend REQUIRES cfo/admin —
	// so gating Approve on the manager-only predicate hid the button from the
	// exact person the CFO gate escalates to, leaving a large report with no
	// approver at all. Same SoD self-check.
	function canApproveReport(r: ExpenseReport): boolean {
		return canApproveReports && r.status === 'submitted' && auth.user?.id !== r.employee_user_id;
	}

	function exportReportCsv() {
		if (!activeReport) return;
		exportExpensesCsv({ report_id: activeReport.id });
	}

	// --- Violation badge helpers (shared by expenses + report-detail rows) ---
	function violationTitle(list: PolicyViolation[]): string {
		return list.map((v) => v.message).join('; ');
	}

	function switchTab(next: Tab) {
		tab = next;
		closeReport();
		closeMatchPicker();
		showReject = false;
		submitViolations = [];
		syncUrl();
	}

	// ========================== Policies tab ==========================
	// A policy's thresholds are denominated in its own threshold_currency; an
	// unset one means the org's reporting currency (what the backend engine
	// falls back to), so the table must never render them in a different unit.
	// `null` when that is unresolved too — the figures then render bare and the
	// column names the concept, never a guessed code (decisions §200).
	function policyCurrency(p: ExpensePolicy): string | null {
		return p.threshold_currency ?? orgCurrency.currency;
	}

	let policies = $state<ExpensePolicy[]>([]);
	let policiesLoading = $state(false);
	let showPolicyCreate = $state(false);
	let editingPolicy = $state<ExpensePolicy | null>(null);
	let confirmDeletePolicyId = $state<string | null>(null);

	// `onPolicySaved` / `deletePolicy` edit the list in place with no fetch of
	// their own — and a newly created policy needs no pre-existing row, so it
	// races even the tab's first load.
	const policiesSequence = createRequestSequencer();

	async function loadPolicies() {
		const token = policiesSequence.start();
		policiesLoading = true;
		try {
			const loaded = await listPolicies();
			// Superseded by a newer load, or by a local create/edit/delete.
			if (!policiesSequence.canCommit(token)) return;
			policies = loaded;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// edit still failed, and no newer load is coming to report it.
			if (!policiesSequence.isCurrentRequest(token)) return;
			toast(err instanceof Error ? err.message : m('expenses.policies.toast.loadFailed'), 'error');
		} finally {
			if (policiesSequence.isCurrentRequest(token)) policiesLoading = false;
		}
	}

	$effect(() => {
		if (tab === 'policies') loadPolicies();
	});

	function onPolicySaved(p: ExpensePolicy) {
		policiesSequence.supersedeInFlight();
		const idx = policies.findIndex((x) => x.id === p.id);
		if (idx >= 0) policies = policies.map((x) => (x.id === p.id ? p : x));
		else policies = [p, ...policies];
	}

	async function deletePolicy(id: string) {
		try {
			await apiDeletePolicy(id);
			policiesSequence.supersedeInFlight();
			policies = policies.filter((p) => p.id !== id);
			toast(m('expenses.policies.toast.deleted'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.toast.deleteFailed'), 'error');
		} finally {
			confirmDeletePolicyId = null;
		}
	}

	// ======================== Pre-approvals tab ========================
	let preapprovals = $state<ExpensePreapproval[]>([]);
	let preapprovalsLoading = $state(false);
	let preapprovalStatus = $state<string>($page.url.searchParams.get('pa_status') ?? 'all');
	let showNewPreapproval = $state(false);
	let paBusy = $state(false);
	let paTitle = $state('');
	// Raw decimal text to the wire: `schemas/expense.py::ExpensePreapprovalBase`
	// declares `estimated_amount` a `Decimal`, and `json.loads` collapses a
	// fractional JSON number to a float before pydantic can intervene.
	let paAmount = $state<string | null>(null);
	let paCategory = $state('');
	let paJustification = $state('');
	let paRejectArmedId = $state<string | null>(null);

	const PREAPPROVAL_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		...EXPENSE_PREAPPROVAL_STATUSES.map((s) => ({
			key: s,
			label: preapprovalStatusLabel(s)
		}))
	]);

	// `approvePa` / `rejectPa` rewrite a row in place with no fetch of their own.
	const preapprovalsSequence = createRequestSequencer();

	async function loadPreapprovals() {
		const token = preapprovalsSequence.start();
		preapprovalsLoading = true;
		try {
			const params = preapprovalStatus !== 'all' ? { status: preapprovalStatus } : {};
			const loaded = await listPreapprovals(params);
			// Superseded by a newer load, or by a local approve/reject.
			if (!preapprovalsSequence.canCommit(token)) return;
			preapprovals = loaded;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// edit still failed, and no newer load is coming to report it.
			if (!preapprovalsSequence.isCurrentRequest(token)) return;
			toast(err instanceof Error ? err.message : m('expenses.preapprovals.toast.loadFailed'), 'error');
		} finally {
			if (preapprovalsSequence.isCurrentRequest(token)) preapprovalsLoading = false;
		}
	}

	$effect(() => {
		if (tab === 'preapprovals') {
			preapprovalStatus;
			loadPreapprovals();
			syncUrl();
		}
	});

	async function handleNewPreapproval() {
		if (!paTitle.trim() || paAmount == null) return;
		const exactAmount = normalizeMoneyInput(paAmount);
		if (exactAmount === null) {
			toast(m('common.amountInvalid'), 'error');
			return;
		}
		paBusy = true;
		try {
			await createPreapproval({
				title: paTitle.trim(),
				estimated_amount: exactAmount,
				currency: orgCurrency.currency ?? DEFAULT_CURRENCY,
				category: paCategory.trim() || null,
				justification: paJustification.trim() || null
			});
			toast(m('expenses.preapprovals.toast.created'), 'success');
			showNewPreapproval = false;
			paTitle = '';
			paAmount = null;
			paCategory = '';
			paJustification = '';
			await loadPreapprovals();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.reports.toast.createFailed'), 'error');
		} finally {
			paBusy = false;
		}
	}

	function canDecidePreapproval(pa: ExpensePreapproval): boolean {
		return (
			canManagePolicies &&
			pa.status === 'pending' &&
			auth.user?.id !== pa.requester_user_id
		);
	}

	async function approvePa(pa: ExpensePreapproval) {
		try {
			const updated = await approvePreapproval(pa.id);
			preapprovalsSequence.supersedeInFlight();
			preapprovals = preapprovals.map((p) => (p.id === pa.id ? updated : p));
			toast(m('expenses.preapprovals.toast.approved'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.reports.toast.approveFailed'), 'error');
		}
	}

	async function rejectPa(pa: ExpensePreapproval) {
		try {
			const updated = await rejectPreapproval(pa.id);
			preapprovalsSequence.supersedeInFlight();
			preapprovals = preapprovals.map((p) => (p.id === pa.id ? updated : p));
			toast(m('expenses.preapprovals.toast.rejected'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.reports.toast.rejectFailed'), 'error');
		} finally {
			paRejectArmedId = null;
		}
	}

	// ============================ Cards tab (WF4) ============================
	const CARDS_PAGE_SIZE = 50;
	let cardTxns = $state<CorporateCardTransaction[]>([]);
	let cardsLoading = $state(false);
	let cardsTotal = $state(0);
	let cardsPage = $state(1);
	let cardsLoadingMore = $state(false);

	// Same rule as the reports list: `cardsTotal` is the whole set.
	let cardsHasMore = $derived(cardTxns.length < cardsTotal);
	let reconFilter = $state<string>($page.url.searchParams.get('recon') ?? 'all');
	let cardBusy = $state(false);
	let cardFileInput = $state<HTMLInputElement>();

	// Match-suggestion picker modal state.
	let matchTxn = $state<CorporateCardTransaction | null>(null);
	let matchSuggestions = $state<CardMatchSuggestion[]>([]);
	let matchLoading = $state(false);

	const RECON_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		...RECONCILIATION_STATUSES.map((s) => ({ key: s, label: reconStatusLabel(s) }))
	]);

	const unmatchedCount = $derived(
		cardTxns.filter((t) => t.reconciliation_status === 'unmatched').length
	);
	const matchedCount = $derived(
		cardTxns.filter((t) => t.reconciliation_status === 'matched').length
	);

	// Like reports, the cards tab has no local-mutation helper — import / sync /
	// match / unmatch / ignore all re-fetch through `loadCardTxns()` — so it
	// needs no `supersedeInFlight()`; the sequencer stops a filter flip and a
	// post-mutation refresh landing out of order.
	const cardsSequence = createRequestSequencer();

	async function loadCardTxns(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? cardsPage + 1 : 1;
		const token = cardsSequence.start();
		if (opts.append) cardsLoadingMore = true;
		else cardsLoading = true;
		try {
			const params = reconFilter !== 'all' ? { reconciliation_status: reconFilter } : {};
			const res = await listCardTransactions({
				...params,
				page: nextPage,
				page_size: CARDS_PAGE_SIZE
			});
			if (!cardsSequence.canCommit(token)) return;
			cardTxns = opts.append ? appendUnique(cardTxns, res.items) : res.items;
			cardsTotal = res.total;
			cardsPage = nextPage;
		} catch (err) {
			if (!cardsSequence.isCurrentRequest(token)) return;
			toast(err instanceof Error ? err.message : m('expenses.cards.toast.loadFailed'), 'error');
		} finally {
			if (cardsSequence.isCurrentRequest(token)) {
				cardsLoading = false;
				cardsLoadingMore = false;
			}
		}
	}

	// Load whenever the Cards tab is active or the recon filter changes (mirrors
	// the Reports / Pre-approvals tab effects).
	$effect(() => {
		if (tab === 'cards') {
			reconFilter;
			loadCardTxns();
			syncUrl();
		}
	});

	async function handleCardCsv(e: Event) {
		const input = e.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;
		cardBusy = true;
		try {
			const result = await importCardCsv(file);
			toast(
				m('expenses.cards.toast.imported', { n: result.imported }) +
					(result.skipped
						? m('expenses.cards.toast.importedSkipped', { n: result.skipped })
						: ''),
				result.imported > 0 ? 'success' : 'info'
			);
			await loadCardTxns();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.cards.toast.importFailed'), 'error');
		} finally {
			cardBusy = false;
			input.value = ''; // allow re-pick of the same file
		}
	}

	async function handleSyncVirtualCards() {
		cardBusy = true;
		try {
			const res = await syncVirtualCards();
			toast(
				m('expenses.cards.toast.synced', { n: res.created }) +
					(res.skipped ? m('expenses.cards.toast.syncedSkipped', { n: res.skipped }) : ''),
				'success'
			);
			await loadCardTxns();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.cards.toast.syncFailed'), 'error');
		} finally {
			cardBusy = false;
		}
	}

	async function openMatchPicker(txn: CorporateCardTransaction) {
		matchTxn = txn;
		matchSuggestions = [];
		matchLoading = true;
		try {
			matchSuggestions = await cardMatchSuggestions(txn.id);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.cards.toast.suggestionsFailed'), 'error');
		} finally {
			matchLoading = false;
		}
	}

	function closeMatchPicker() {
		matchTxn = null;
		matchSuggestions = [];
	}

	async function confirmMatch(expenseId: string) {
		if (!matchTxn) return;
		cardBusy = true;
		try {
			await matchCardTxn(matchTxn.id, expenseId);
			toast(m('expenses.cards.toast.matched'), 'success');
			closeMatchPicker();
			await loadCardTxns();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.cards.toast.matchFailed'), 'error');
		} finally {
			cardBusy = false;
		}
	}

	async function createExpenseForCard(txn: CorporateCardTransaction) {
		cardBusy = true;
		try {
			await createExpenseFromCard(txn.id);
			toast(m('expenses.cards.toast.expenseCreated'), 'success');
			await loadCardTxns();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.cards.toast.createExpenseFailed'), 'error');
		} finally {
			cardBusy = false;
		}
	}

	async function ignoreCard(txn: CorporateCardTransaction) {
		cardBusy = true;
		try {
			await ignoreCardTxn(txn.id);
			toast(m('expenses.cards.toast.ignored'), 'success');
			await loadCardTxns();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.cards.toast.ignoreFailed'), 'error');
		} finally {
			cardBusy = false;
		}
	}

	async function unmatchCard(txn: CorporateCardTransaction) {
		cardBusy = true;
		try {
			await unmatchCardTxn(txn.id);
			toast(m('expenses.cards.toast.unmatched'), 'success');
			await loadCardTxns();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('expenses.cards.toast.unmatchFailed'), 'error');
		} finally {
			cardBusy = false;
		}
	}
</script>

<svelte:window onclick={onWindowClick} />

<PageHeader title={m('expenses.title')}>
	{#snippet actions()}
		{#if tab === 'expenses'}
			<button class="btn-secondary" onclick={() => exportExpensesCsv(buildFilterParams())}>{m('expenses.action.exportCsv')}</button>
			{#if canCreate}
				<button class="btn-primary" onclick={() => (showCreate = true)}>{m('expenses.action.newExpense')}</button>
			{/if}
		{:else if tab === 'reports'}
			{#if canCreate}
				<button class="btn-primary" onclick={() => (showNewReport = true)}>{m('expenses.action.newReport')}</button>
			{/if}
		{:else if tab === 'policies'}
			{#if canManagePolicies}
				<button class="btn-primary" onclick={() => (showPolicyCreate = true)}>{m('expenses.action.newPolicy')}</button>
			{/if}
		{:else if tab === 'preapprovals'}
			{#if canCreate}
				<button class="btn-primary" onclick={() => (showNewPreapproval = true)}>{m('expenses.action.newRequest')}</button>
			{/if}
		{:else if tab === 'cards'}
			{#if canManagePolicies}
				<input type="file" accept=".csv" bind:this={cardFileInput} onchange={handleCardCsv} hidden />
				<button class="btn-secondary" disabled={cardBusy} onclick={() => cardFileInput?.click()}>{m('expenses.action.importCsv')}</button>
				<button class="btn-secondary" disabled={cardBusy} onclick={handleSyncVirtualCards}>{m('expenses.action.syncCards')}</button>
			{/if}
		{/if}
	{/snippet}

	<div class="tab-row">
		<button class="tab" class:active={tab === 'expenses'} onclick={() => switchTab('expenses')}>{m('expenses.tab.expenses')}</button>
		<button class="tab" class:active={tab === 'reports'} onclick={() => switchTab('reports')}>{m('expenses.tab.reports')}</button>
		{#if canManagePolicies}
			<button class="tab" class:active={tab === 'policies'} onclick={() => switchTab('policies')}>{m('expenses.tab.policies')}</button>
		{/if}
		<button class="tab" class:active={tab === 'preapprovals'} onclick={() => switchTab('preapprovals')}>{m('expenses.tab.preapprovals')}</button>
		<button class="tab" class:active={tab === 'cards'} onclick={() => switchTab('cards')}>{m('expenses.tab.cards')}</button>
	</div>

	{#if tab === 'expenses'}
		<div class="kpi-row">
			<KpiCard value={periodTotal} sub={periodTotalRest} label={m('expenses.kpi.periodTotal')} />
			<KpiCard value={expenseStore.total} label={m('expenses.kpi.expenses')} />
			<KpiCard value={pendingCount} label={m('expenses.kpi.pending')} highlight={pendingCount ? 'red' : null} />
		</div>

		<div class="filter-row">
			<SearchBox bind:value={search} placeholder={m('expenses.search.placeholder')} ariaLabel={m('expenses.search.aria')} />
			<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} />
		</div>

		<DataTable
			columns={COLUMNS}
			isEmpty={expenseStore.all.length === 0}
			empty={expensesEmptyMessage}
		>
			{#snippet header()}
				<tr>
					<th class="checkbox-col">
						<input
							type="checkbox"
							aria-label={m('expenses.selectAllAria')}
							checked={allSelected}
							onchange={toggleSelectAll}
						/>
					</th>
					<SortableHeader field="expense_date" label={m('expenses.col.date')} active={sortField === 'expense_date'} order={sortOrder} onsort={handleSort} />
					<SortableHeader field="merchant" label={m('expenses.col.merchant')} active={sortField === 'merchant'} order={sortOrder} onsort={handleSort} />
					<SortableHeader field="category" label={m('expenses.col.category')} active={sortField === 'category'} order={sortOrder} onsort={handleSort} />
					<th>{m('expenses.col.method')}</th>
					<SortableHeader field="amount" label={m('expenses.col.amount')} class="right" active={sortField === 'amount'} order={sortOrder} onsort={handleSort} />
					<SortableHeader field="status" label={m('expenses.col.status')} active={sortField === 'status'} order={sortOrder} onsort={handleSort} />
					<th class="actions-col"></th>
				</tr>
			{/snippet}
			{#snippet body()}
				{#each expenseStore.all as exp (exp.id)}
					<tr
						class="clickable"
						class:row-selected={selected.has(exp.id)}
						onclick={(e) => {
							if (isRowOpenClick(e)) editing = exp;
						}}
					>
						<td class="checkbox-col">
							<input
								type="checkbox"
								aria-label={m('expenses.selectAria', { name: exp.merchant ?? exp.id })}
								checked={selected.has(exp.id)}
								onchange={() => toggleSelect(exp.id)}
							/>
						</td>
						<td class="muted">{formatDate(exp.expense_date)}</td>
						<td>
							<RowLink onclick={() => (editing = exp)} ariaLabel={m('expenses.row.editAria', { name: exp.merchant ?? exp.id })}>
								{exp.merchant ?? '—'}
							</RowLink>
						</td>
						<td>{exp.category ?? '—'}</td>
						<td class="muted">{glLabel(exp.gl_account_id)}</td>
						<td class="right mono"><Money amount={exp.amount} currency={exp.currency} /></td>
						<td>
							<!-- The 6px that used to be the violation pill's own
							     `margin-left` lives on this wrapper: `<Badge>` owns colour
							     and metrics, the caller owns placement. `cursor: help` is
							     inherited down onto the pill from `.violation-chip`, which
							     is also the caller's to own — the tooltip is the point of
							     that chip. -->
							<span class="status-cell">
								<Badge tone={EXPENSE_STATUS_TONES[exp.status as ExpenseStatus]} variant={exp.status}>{statusLabel(exp.status)}</Badge>
								{#if exp.policy_violations && exp.policy_violations.length}
									<span class="violation-chip">
										<Badge tone="danger" variant="violation" title={violationTitle(exp.policy_violations)}>⚠ {exp.policy_violations.length}</Badge>
									</span>
								{/if}
							</span>
						</td>
						<td class="actions">
							{#if canCreate}
								<RowAction
									variant="danger"
									armed={confirmDeleteId === exp.id}
									onclick={(e) => {
										e.stopPropagation();
										if (confirmDeleteId === exp.id) deleteExpense(exp.id);
										else confirmDeleteId = exp.id;
									}}
								>
									{confirmDeleteId === exp.id ? m('expenses.row.confirm') : m('expenses.row.delete')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>

		{#if expenseStore.hasMore}
			<div class="load-more-row">
				<button class="btn-load-more" onclick={() => expenseStore.loadMore()} disabled={expenseStore.loading}>
					{expenseStore.loading ? m('expenses.loading') : m('expenses.loadMore', { shown: expenseStore.all.length, total: expenseStore.total })}
				</button>
			</div>
		{:else if expenseStore.total > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('expenses.showingAll', { total: expenseStore.total })}</span>
			</div>
		{/if}
	{:else if tab === 'reports'}
		<!-- ===================== Reports tab ===================== -->
		{#if activeReport}
			<div class="report-detail">
				<div class="report-detail-head">
					<button class="btn-back" onclick={closeReport}>{m('expenses.reports.back')}</button>
					<div class="report-title-block">
						<h2>{activeReport.report_number}</h2>
						<Badge tone={EXPENSE_REPORT_STATUS_TONES[activeReport.status as ExpenseReportStatus]} variant={activeReport.status}>{reportStatusLabel(activeReport.status)}</Badge>
					</div>
					<div class="report-detail-actions">
						<button class="btn-secondary" onclick={exportReportCsv}>{m('expenses.reports.exportCsv')}</button>
						{#if canCreate && activeReport.status === 'draft'}
							<button class="btn-primary" disabled={reportBusy} onclick={submitReport}>{m('expenses.reports.submit')}</button>
						{/if}
						{#if canApproveReport(activeReport)}
							<button class="btn-primary" disabled={reportBusy} onclick={approveActiveReport}>{m('expenses.reports.approve')}</button>
						{/if}
						{#if canDecideReport(activeReport)}
							<button class="btn-secondary danger" disabled={reportBusy} onclick={() => (showReject = true)}>{m('expenses.reports.reject')}</button>
						{/if}
					</div>
				</div>

				{#if submitViolations.length}
					<div class="violation-panel">
						<strong>{m('expenses.reports.submitBlocked')}</strong>
						<ul>
							{#each submitViolations as v (v.code + (v.policy_id ?? ''))}
								<li>{v.message}</li>
							{/each}
						</ul>
					</div>
				{/if}

				{#if showReject && activeReport}
					<div class="reject-row">
						<input
							type="text"
							placeholder={m('expenses.reports.rejectPlaceholder')}
							bind:value={rejectReason}
							aria-label={m('expenses.reports.rejectAria')}
						/>
						<button class="btn-secondary danger" disabled={reportBusy} onclick={rejectActiveReport}>{m('expenses.reports.confirmReject')}</button>
						<button class="btn-secondary" disabled={reportBusy} onclick={() => { showReject = false; rejectReason = ''; }}>{m('expenses.reports.cancel')}</button>
					</div>
				{/if}

				<!-- Rendered on every state of the summary fetch, never gated on it
				     (`docs/decisions.md` §125). It used to sit inside
				     `{#if activeSummary}` and collapse to nothing while the summary
				     was in flight — and again, permanently, whenever that fetch
				     FAILED, which `refreshSummary`'s `catch` turns into a silent
				     `null`. An `unavailable` row of dashes is the honest rendering
				     of that second case; a row that simply isn't there says the
				     report has no total.

				     Every lifecycle action on this report (submit / approve /
				     reject / attach / detach) re-runs `refreshSummary`, and
				     `pending` is only consulted when the value is MISSING, so those
				     keep the figures on screen instead of blanking them. -->
				<div class="kpi-row">
					<KpiCard
						value={activeSummary
							? formatMoney(activeSummary.total, { currency: activeSummary.currency })
							: null}
						label={m('expenses.reports.total')}
						pending={summaryLoading}
					/>
					<KpiCard
						value={activeSummary ? activeSummary.count : null}
						label={m('expenses.reports.expenses')}
						pending={summaryLoading}
					/>
					<KpiCard
						value={activeSummary ? activeSummary.by_category.length : null}
						label={m('expenses.reports.categories')}
						pending={summaryLoading}
					/>
				</div>
				<!--
					The total sums each line's rate-locked conversion into the report
					currency. Lines with no usable rate are EXCLUDED, so the figure
					above would silently understate without this notice (issue #157).
					One condition rather than the nested pair this used to be: the
					outer `{#if activeSummary}` was the row's gate, and with the row
					hoisted out it wrapped nothing else.
				-->
				{#if activeSummary && activeSummary.unconverted_count > 0}
					<div class="unconverted-panel" role="alert">
						{m('expenses.reports.unconverted', {
							count: activeSummary.unconverted_count,
							currency: activeSummary.currency
						})}
					</div>
				{/if}

				{#if canCreate && activeReport.status === 'draft'}
					<div class="attach-row">
						<input
							type="text"
							placeholder={m('expenses.reports.attachPlaceholder')}
							bind:value={attachId}
							aria-label={m('expenses.reports.attachAria')}
						/>
						<button class="btn-secondary" disabled={reportBusy || !attachId.trim()} onclick={attachToReport}>{m('expenses.reports.attach')}</button>
					</div>
				{/if}

				<DataTable
					columns={[
						{ label: m('expenses.reports.col.date') },
						{ label: m('expenses.reports.col.merchant') },
						{ label: m('expenses.reports.col.category') },
						{ label: m('expenses.reports.col.amount'), class: 'right' },
						{ label: m('expenses.reports.col.status') },
						{ label: '', class: 'actions-col' }
					]}
					isEmpty={activeReport.expenses.length === 0}
					empty={m('expenses.reports.emptyExpenses')}
				>
					{#snippet body()}
						{#each activeReport?.expenses ?? [] as exp (exp.id)}
							<tr>
								<td class="muted">{formatDate(exp.expense_date)}</td>
								<td>{exp.merchant ?? '—'}</td>
								<td>{exp.category ?? '—'}</td>
								<td class="right mono"><Money amount={exp.amount} currency={exp.currency} /></td>
								<td>
									<span class="status-cell">
										<Badge tone={EXPENSE_STATUS_TONES[exp.status as ExpenseStatus]} variant={exp.status}>{statusLabel(exp.status)}</Badge>
										{#if exp.policy_violations && exp.policy_violations.length}
											<span class="violation-chip">
												<Badge tone="danger" variant="violation" title={violationTitle(exp.policy_violations)}>⚠ {exp.policy_violations.length}</Badge>
											</span>
										{/if}
									</span>
								</td>
								<td class="actions">
									{#if canCreate && activeReport?.status === 'draft'}
										<RowAction variant="default" onclick={() => detachFromReport(exp.id)}>{m('expenses.reports.detach')}</RowAction>
									{/if}
								</td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>
			</div>
		{:else}
			<DataTable
				columns={[
					{ label: m('expenses.reports.col.reportNumber') },
					{ label: m('expenses.reports.col.title') },
					{ label: m('expenses.reports.col.status') },
					{ label: m('expenses.reports.col.total'), class: 'right' }
				]}
				isEmpty={reports.length === 0}
				empty={reportsLoading ? m('expenses.loading') : m('expenses.reports.empty')}
			>
				{#snippet body()}
					{#each reports as r (r.id)}
						<tr class="clickable" onclick={(e) => { if (isRowOpenClick(e)) openReport(r); }}>
							<td class="mono">
								<RowLink onclick={() => openReport(r)} ariaLabel={m('expenses.reports.openAria', { number: r.report_number })}>
									{r.report_number}
								</RowLink>
							</td>
							<td>{r.title ?? '—'}</td>
							<td><Badge tone={EXPENSE_REPORT_STATUS_TONES[r.status as ExpenseReportStatus]} variant={r.status}>{reportStatusLabel(r.status)}</Badge></td>
							<td class="right mono"><Money amount={r.total_amount} currency={r.currency} /></td>
						</tr>
					{/each}
				{/snippet}
			</DataTable>
			{#if reportsHasMore}
				<div class="load-more-row">
					<button
						class="btn-load-more"
						onclick={() => loadReports({ append: true })}
						disabled={reportsLoadingMore}
					>
						{reportsLoadingMore
							? m('common.loading')
							: m('expenses.reports.loadMore', {
									shown: reports.length,
									total: reportsTotal
								})}
					</button>
				</div>
			{:else if reportsTotal > 0}
				<div class="load-more-row">
					<span class="load-more-end">{m('expenses.reports.showingAll', { total: reportsTotal })}</span>
				</div>
			{/if}
		{/if}
	{:else if tab === 'policies'}
		<!-- ===================== Policies tab ===================== -->
		<DataTable
			columns={[
				{ label: m('expenses.policies.col.name') },
				{ label: m('expenses.policies.col.category') },
				{ label: m('expenses.policies.col.currency') },
				{ label: m('expenses.policies.col.limit'), class: 'right' },
				{ label: m('expenses.policies.col.receiptAbove'), class: 'right' },
				{ label: m('expenses.policies.col.preapprAbove'), class: 'right' },
				{ label: m('expenses.policies.col.active') },
				{ label: '', class: 'actions-col' }
			]}
			isEmpty={policies.length === 0}
			empty={policiesLoading ? m('expenses.loading') : m('expenses.policies.empty')}
		>
			{#snippet body()}
				{#each policies as p (p.id)}
					<tr class="clickable" onclick={(e) => { if (isRowOpenClick(e)) editingPolicy = p; }}>
						<td>
							<RowLink onclick={() => (editingPolicy = p)} ariaLabel={m('expenses.policies.editAria', { name: p.name })}>
								{p.name}
							</RowLink>
						</td>
						<td>{p.category ?? m('expenses.policies.categoryAll')}</td>
						<td>{policyCurrency(p) ?? orgCurrency.label}</td>
						<td class="right mono">{p.category_limit != null ? formatMoney(p.category_limit, { currency: policyCurrency(p) }) : '—'}</td>
						<td class="right mono">{p.requires_receipt_above != null ? formatMoney(p.requires_receipt_above, { currency: policyCurrency(p) }) : '—'}</td>
						<td class="right mono">{p.requires_preapproval_above != null ? formatMoney(p.requires_preapproval_above, { currency: policyCurrency(p) }) : '—'}</td>
						<!-- The variant was `approved` / `cancelled` because those were the
						     rules that happened to carry the right colours. A policy is
						     neither approved nor cancelled — it is on or off — and now that
						     the tone carries the colour, the class is free to say so
						     (decisions.md §47: a variant is a selector hook, never a
						     colour). No spec selects on either name. -->
						<td><Badge tone={p.active ? 'success' : 'neutral'} variant={p.active ? 'active' : 'inactive'}>{p.active ? m('expenses.policies.active') : m('expenses.policies.inactive')}</Badge></td>
						<td class="actions">
							{#if canManagePolicies}
								<RowAction
									variant="danger"
									armed={confirmDeletePolicyId === p.id}
									onclick={(e) => {
										e.stopPropagation();
										if (confirmDeletePolicyId === p.id) deletePolicy(p.id);
										else confirmDeletePolicyId = p.id;
									}}
								>
									{confirmDeletePolicyId === p.id ? m('expenses.row.confirm') : m('expenses.row.delete')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{:else if tab === 'preapprovals'}
		<!-- ==================== Pre-approvals tab ==================== -->
		<div class="filter-row">
			<FilterChips chips={PREAPPROVAL_CHIPS} bind:active={preapprovalStatus} />
		</div>

		<DataTable
			columns={[
				{ label: m('expenses.preapprovals.col.title') },
				{ label: m('expenses.preapprovals.col.category') },
				{ label: m('expenses.preapprovals.col.estimated'), class: 'right' },
				{ label: m('expenses.preapprovals.col.status') },
				{ label: '', class: 'actions-col' }
			]}
			isEmpty={preapprovals.length === 0}
			empty={preapprovalsLoading ? m('expenses.loading') : m('expenses.preapprovals.empty')}
		>
			{#snippet body()}
				{#each preapprovals as pa (pa.id)}
					<tr>
						<td>{pa.title}</td>
						<td>{pa.category ?? '—'}</td>
						<td class="right mono"><Money amount={pa.estimated_amount} currency={pa.currency} /></td>
						<td><Badge tone={EXPENSE_PREAPPROVAL_STATUS_TONES[pa.status as ExpensePreapprovalStatus]} variant={pa.status}>{preapprovalStatusLabel(pa.status)}</Badge></td>
						<td class="actions">
							{#if canDecidePreapproval(pa)}
								<RowAction variant="success" onclick={() => approvePa(pa)}>{m('expenses.preapprovals.approve')}</RowAction>
								<RowAction
									variant="danger"
									armed={paRejectArmedId === pa.id}
									onclick={() => {
										if (paRejectArmedId === pa.id) rejectPa(pa);
										else paRejectArmedId = pa.id;
									}}
								>
									{paRejectArmedId === pa.id ? m('expenses.preapprovals.confirmReject') : m('expenses.preapprovals.reject')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{:else if tab === 'cards'}
		<!-- ===================== Cards tab (WF4) ===================== -->
		<div class="kpi-row">
			<KpiCard value={unmatchedCount} label={m('expenses.cards.kpi.unmatched')} highlight={unmatchedCount ? 'red' : null} />
			<KpiCard value={matchedCount} label={m('expenses.cards.kpi.matched')} highlight={matchedCount ? 'green' : null} />
			<KpiCard value={cardsTotal} label={m('expenses.cards.kpi.transactions')} />
		</div>

		<div class="filter-row">
			<FilterChips chips={RECON_CHIPS} bind:active={reconFilter} />
		</div>

		<DataTable
			columns={[
				{ label: m('expenses.cards.col.date') },
				{ label: m('expenses.cards.col.merchant') },
				{ label: m('expenses.cards.col.card') },
				{ label: m('expenses.cards.col.amount'), class: 'right' },
				{ label: m('expenses.cards.col.status') },
				{ label: '', class: 'actions-col' }
			]}
			isEmpty={cardTxns.length === 0}
			empty={cardsLoading ? m('expenses.loading') : m('expenses.cards.empty')}
		>
			{#snippet body()}
				{#each cardTxns as txn (txn.id)}
					<tr>
						<td class="muted">{formatDate(txn.txn_date)}</td>
						<td>{txn.merchant ?? '—'}</td>
						<td class="muted">
							{#if txn.virtual_card_id}
								<!-- A marker ("this charge came off a virtual card"), not a
								     status: `accent`, the same tone `/payments` gives the
								     virtual-card method pill. It read `approved` green only
								     because that was the rule with the colour someone wanted. -->
								<Badge tone="accent" variant="virtual-card">{m('expenses.cards.virtual')}</Badge>
							{/if}
							{txn.card_last_four ? `•••• ${txn.card_last_four}` : '—'}
						</td>
						<td class="right mono"><Money amount={txn.amount} currency={txn.currency} /></td>
						<td>
							<Badge
								tone={RECONCILIATION_STATUS_TONES[
									txn.reconciliation_status as ReconciliationStatus
								]}
								variant={txn.reconciliation_status}
							>
								{reconStatusLabel(txn.reconciliation_status)}
							</Badge>
						</td>
						<!-- All four bind `cardBusy`. The three mutating handlers already
						     set it, but nothing read it here, and the row's own `{#if}`
						     only changes once the refetch lands — so a second click inside
						     the round trip fired a second request. On Create expense that
						     minted a SECOND Expense (and a second `expense.created` audit
						     row) for one card charge, which then flowed into a report
						     total: the backend's already-matched 409 is a read-then-write
						     check, so two overlapping requests both read `unmatched`.
						     (The txn row is locked FOR UPDATE server-side now too — this
						     half is what stops the second request being issued at all.)
						     Match opens a picker rather than mutating, but is disabled on
						     the same flag: choosing a target while another mutation is
						     retiring the row is the same stale-row problem. -->
						<td class="actions">
							{#if canManagePolicies && txn.reconciliation_status === 'unmatched'}
								<RowAction variant="default" disabled={cardBusy} onclick={() => openMatchPicker(txn)}>{m('expenses.cards.match')}</RowAction>
							{/if}
							{#if canCreate && txn.reconciliation_status === 'unmatched'}
								<RowAction variant="default" disabled={cardBusy} onclick={() => createExpenseForCard(txn)}>{m('expenses.cards.createExpense')}</RowAction>
							{/if}
							{#if canManagePolicies && txn.reconciliation_status === 'matched'}
								<RowAction variant="default" disabled={cardBusy} onclick={() => unmatchCard(txn)}>{m('expenses.cards.unmatch')}</RowAction>
							{/if}
							{#if canManagePolicies && txn.reconciliation_status === 'unmatched'}
								<RowAction variant="default" disabled={cardBusy} onclick={() => ignoreCard(txn)}>{m('expenses.cards.ignore')}</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
		{#if cardsHasMore}
			<div class="load-more-row">
				<button
					class="btn-load-more"
					onclick={() => loadCardTxns({ append: true })}
					disabled={cardsLoadingMore}
				>
					{cardsLoadingMore
						? m('common.loading')
						: m('expenses.cards.loadMore', { shown: cardTxns.length, total: cardsTotal })}
				</button>
			</div>
		{:else if cardsTotal > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('expenses.cards.showingAll', { total: cardsTotal })}</span>
			</div>
		{/if}
	{/if}
</PageHeader>

<!-- Bulk GL code bar (expenses tab only) -->
{#if tab === 'expenses'}
	<BulkBar
		count={selected.size}
		onclear={() => {
			selected = new Set();
			selectedAllMatching = false;
		}}
	>
		{#snippet actions()}
			{#if allSelected && !selectedAllMatching && expenseStore.total > expenseStore.all.length}
				<button class="bulk-action-btn secondary" disabled={selectingAllMatching} onclick={selectAllMatching}>
					{selectingAllMatching
						? m('common.loading')
						: m('common.selectAllMatching', { total: expenseStore.total })}
				</button>
			{:else if selectedAllMatching}
				<span class="bulk-all-matching-note">{m('common.allMatchingSelected')}</span>
			{/if}
			<select class="bulk-gl-select" bind:value={bulkGl} aria-label={m('expenses.bulk.glAria')} disabled={bulkBusy}>
				<option value="">{m('expenses.bulk.clearGl')}</option>
				{#each glAccounts as g (g.id)}
					<option value={g.id}>{g.code} — {g.name}</option>
				{/each}
			</select>
			<button class="bulk-action-btn" disabled={bulkBusy} onclick={applyBulkGl}>
				{bulkBusy ? m('expenses.bulk.applying') : m('expenses.bulk.glCode', { n: selected.size })}
			</button>
		{/snippet}
	</BulkBar>
{/if}

{#if showCreate}
	<ExpenseModal expense={null} {glAccounts} onclose={() => (showCreate = false)} onsaved={onSaved} />
{/if}

{#if editing}
	<ExpenseModal expense={editing} {glAccounts} onclose={() => (editing = null)} onsaved={onSaved} />
{/if}

{#if showNewReport}
	<Modal open ariaLabel={m('expenses.newReport.aria')} title={m('expenses.newReport.title')} width="sm" onclose={() => (showNewReport = false)}>
		<form onsubmit={(e) => { e.preventDefault(); handleNewReport(); }}>
			<div class="report-form">
				<label>
					<span>{m('expenses.newReport.number')} <em class="required">*</em></span>
					<input type="text" bind:value={newReportNumber} required />
				</label>
				<label>
					<span>{m('expenses.newReport.titleField')}</span>
					<input type="text" bind:value={newReportTitle} />
				</label>
			</div>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (showNewReport = false)}>{m('expenses.newReport.cancel')}</button>
				<button type="submit" class="btn-primary" disabled={reportBusy || !newReportNumber.trim()}>
					{reportBusy ? m('expenses.newReport.creating') : m('expenses.newReport.create')}
				</button>
			</div>
		</form>
	</Modal>
{/if}

{#if showPolicyCreate}
	<PolicyModal policy={null} onclose={() => (showPolicyCreate = false)} onsaved={onPolicySaved} />
{/if}

{#if editingPolicy}
	<PolicyModal policy={editingPolicy} onclose={() => (editingPolicy = null)} onsaved={onPolicySaved} />
{/if}

{#if showNewPreapproval}
	<Modal open ariaLabel={m('expenses.newPreapproval.aria')} title={m('expenses.newPreapproval.title')} width="sm" onclose={() => (showNewPreapproval = false)}>
		<form onsubmit={(e) => { e.preventDefault(); handleNewPreapproval(); }}>
			<div class="report-form">
				<label>
					<span>{m('expenses.newPreapproval.titleField')} <em class="required">*</em></span>
					<input type="text" bind:value={paTitle} required />
				</label>
				<label>
					<span>{m('expenses.newPreapproval.estimated')} <em class="required">*</em></span>
					<input
						type="number"
						step="0.01"
						min="0"
						value={paAmount ?? ''}
						oninput={(e) => (paAmount = e.currentTarget.value.trim() || null)}
					/>
				</label>
				<label>
					<span>{m('expenses.newPreapproval.category')}</span>
					<input type="text" bind:value={paCategory} placeholder={m('expenses.newPreapproval.categoryPlaceholder')} />
				</label>
				<label>
					<span>{m('expenses.newPreapproval.justification')}</span>
					<textarea bind:value={paJustification} rows="2"></textarea>
				</label>
			</div>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (showNewPreapproval = false)}>{m('expenses.newReport.cancel')}</button>
				<button type="submit" class="btn-primary" disabled={paBusy || !paTitle.trim() || paAmount == null}>
					{paBusy ? m('expenses.newReport.creating') : m('expenses.newReport.create')}
				</button>
			</div>
		</form>
	</Modal>
{/if}

{#if matchTxn}
	<Modal open ariaLabel={m('expenses.match.aria')} title={m('expenses.match.title')} width="sm" onclose={closeMatchPicker}>
		<div class="match-picker">
			{#if matchLoading}
				<p class="muted">{m('expenses.match.loading')}</p>
			{:else if matchSuggestions.length === 0}
				<p class="muted">{m('expenses.match.empty')}</p>
			{:else}
				<ul class="match-list">
					{#each matchSuggestions as suggestion (suggestion.expense.id)}
						<li>
							<button
								type="button"
								class="match-row"
								disabled={cardBusy}
								onclick={() => confirmMatch(suggestion.expense.id)}
							>
								<span>{suggestion.expense.merchant ?? '—'} · {formatDate(suggestion.expense.expense_date)}</span>
								<span class="mono"><Money amount={suggestion.expense.amount} currency={suggestion.expense.currency} /></span>
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={closeMatchPicker}>{m('expenses.match.cancel')}</button>
		</div>
	</Modal>
{/if}

<style>
	.tab-row {
		display: flex;
		/* Wraps for the same reason as the shared `ui/Tabs.svelte` tab bar this
		   is a local copy of — a nowrap row of tabs pushes the whole document
		   sideways at 320px (WCAG 1.4.10). */
		flex-wrap: wrap;
		gap: 4px;
		border-bottom: 1px solid var(--border);
	}
	.tab {
		padding: 8px 16px;
		border: none;
		background: none;
		color: var(--text-muted);
		font-size: 0.9rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		border-bottom: 2px solid transparent;
		margin-bottom: -1px;
	}
	.tab:hover {
		color: var(--text);
	}
	.tab.active {
		color: var(--accent);
		border-bottom-color: var(--accent);
	}

	.filter-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	.btn-secondary {
		padding: 8px 14px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.88rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}
	.btn-secondary:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	/* Every pill on this page is `<Badge>`. The tone per status lives beside the
	   label map in `$lib/types/expense` — four of them, one per vocabulary this
	   page badges (expense, report, pre-approval, card reconciliation) — so the
	   list page, the report-detail tables and `ExpenseModal` can't tint the same
	   status two different shades, which is what they were doing (.12 alpha
	   here, .15 in the modal). What stays here is placement and the tooltip
	   affordance. */

	/* Status pill + the optional policy-violation pill beside it. The 6px was
	   the violation pill's own `margin-left`; `flex-wrap` keeps the pair able to
	   break at a narrow viewport, which two inline runs did for free and a flex
	   row must be told to do (SC 1.4.10). */
	.status-cell {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}

	/* `cursor` is inherited, so the caller can own the affordance without
	   `Badge` growing a prop for it. The pill's whole job is its `title`
	   tooltip, which is what the help cursor advertises. */
	.violation-chip {
		display: inline-flex;
		cursor: help;
	}

	.bulk-gl-select {
		/* base look (border/colour/font/chevron) from the global select recipe */
		padding: 6px 30px 6px 10px;
		border-radius: 6px;
		background-color: var(--surface);
		font-size: 0.82rem;
	}

	.bulk-action-btn {
		padding: 6px 14px;
		border-radius: 6px;
		border: 1px solid var(--accent-strong);
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.bulk-action-btn:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.bulk-action-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}

	.bulk-action-btn.secondary {
		background: transparent;
		color: var(--accent-strong);
	}

	.bulk-all-matching-note {
		font-size: 0.82rem;
		color: var(--text-muted);
		white-space: nowrap;
	}

	/* --- Reports --- */
	.report-detail {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}
	.report-detail-head {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}
	.report-title-block {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.report-title-block h2 {
		margin: 0;
		font-size: 1.1rem;
	}
	.report-detail-actions {
		margin-left: auto;
		display: flex;
		gap: 8px;
	}
	.btn-back {
		border: none;
		background: none;
		color: var(--text-muted);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
		padding: 4px 0;
	}
	.btn-back:hover {
		color: var(--accent);
	}
	.attach-row,
	.reject-row {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}
	/* Text-entry recipe. No checkbox sits under this selector today, but it
	   outranks the global control base in `app.css`, so the carve-out keeps a
	   later one from losing its tick (`background:` resets the drawn mark). */
	.attach-row input:not([type='checkbox']):not([type='radio']),
	.reject-row input:not([type='checkbox']):not([type='radio']) {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.85rem;
		min-width: 280px;
	}

	/* Danger variant of the secondary button (report reject). */
	.btn-secondary.danger:hover {
		border-color: var(--danger);
		color: var(--danger);
	}

	/* Inline blocking-violation panel above the report detail. */
	.violation-panel {
		border: 1px solid var(--danger);
		background: rgba(224, 64, 64, 0.06);
		border-radius: 8px;
		padding: 12px 14px;
		color: var(--text);
		font-size: 0.85rem;
	}
	.violation-panel ul {
		margin: 8px 0 0;
		padding-left: 18px;
	}
	.violation-panel li {
		margin: 2px 0;
		color: var(--danger);
	}

	/* Partial-total notice: some lines lack an FX lock and were excluded. */
	.unconverted-panel {
		margin-top: 10px;
		border: 1px solid #c98a00;
		background: rgba(201, 138, 0, 0.08);
		border-radius: 8px;
		padding: 10px 14px;
		color: var(--text);
		font-size: 0.85rem;
	}

	/* --- New-report mini modal --- */
	.report-form {
		display: flex;
		flex-direction: column;
		gap: 12px;
		margin: 12px 0;
	}
	.report-form label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	/* Text-entry recipe. No checkbox sits under this selector today, but it
	   outranks the global control base in `app.css`, so the carve-out keeps a
	   later one from losing its tick (`background:` resets the drawn mark). */
	.report-form input:not([type='checkbox']):not([type='radio']),
	.report-form textarea {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}

	/* --- Card match-suggestion picker (WF4) --- */
	.match-picker {
		margin: 12px 0;
	}
	.match-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.match-row {
		width: 100%;
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 12px;
		padding: 8px 10px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.85rem;
		cursor: pointer;
	}
	.match-row:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.match-row:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
</style>
