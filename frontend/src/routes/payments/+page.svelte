<script lang="ts">
	import type { Payment, PaymentStatus } from '$lib/types/payment';
	import {
		PAYMENT_STATUSES,
		paymentStatusLabelKey,
		paymentMethodLabelKey,
		PAYMENT_STATUS_TONES,
		runStatusLabelKey,
		runStatusTone,
		voidCardOutcomeLabelKey
	} from '$lib/types/payment';
	import { paymentStore } from '$lib/stores/payments.svelte';
	import { appendUnique } from '$lib/utils/pagination';
	import { api } from '$lib/api';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import RunDetailModal from '$lib/components/modals/RunDetailModal.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { pruneSelection } from '$lib/utils/selection';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import type { BadgeTone } from '$lib/components/ui/badgeTone';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import { formatMoney, isPositiveAmount, type MoneyAmount } from '$lib/utils/money';
	import type { MessageKey } from '$lib/i18n/messages';
	import { compareCorridorQuotes } from '$lib/api/corridorQuotes';
	import {
		allQuotes,
		formatFeeRate,
		isRankedQuote,
		quoteReasonKey,
		unrankedQuotes,
		type CorridorQuote,
		type CorridorQuoteComparison,
		type QuoteMode
	} from '$lib/types/corridorQuote';
	import {
		formatCurrencyTotals,
		groupAmountsByCurrency,
		spansMultipleCurrencies,
		type CurrencyGroup
	} from '$lib/utils/currencyGroups';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { formatDate } from '$lib/utils/time';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID } from '$lib/types/admin';
	import {
		acceptPaymentSettlement,
		retryRunErpSync,
		retryVoidCardCancel,
		voidPayment,
		type RunErpSyncResult
	} from '$lib/api/payments';
	import {
		confirmCardRebate,
		listCardRebates,
		markCardRebatePaid
	} from '$lib/api/cards';
	import {
		formatRebateRate,
		nextRebateTransition,
		rebateTone,
		type CardRebate,
		type RebateListResponse,
		type RebateTransition
	} from '$lib/types/cardRebate';
	import { m } from '$lib/i18n/store.svelte';
	import { untrack } from 'svelte';
	import { page } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import SortableHeader from '$lib/components/ui/SortableHeader.svelte';
	import { toggleSort, type SortOrder } from '$lib/utils/sort';

	let RUNS_COLUMNS = $derived([
		{ label: m('payments.col.run') },
		{ label: m('payments.col.status') },
		{ label: m('payments.col.total'), class: 'right' },
		{ label: m('payments.summary.payments') },
		{ label: m('payments.col.executed') },
		{ label: m('payments.col.created') },
		// Actions column: the run-level ERP sync-back retry lives here. Always
		// rendered so the table keeps its shape for a role that can't use it.
		{ class: 'actions-col' }
	]);

	let CARDS_COLUMNS = $derived([
		{ label: m('payments.col.card') },
		{ label: m('payments.col.vendor') },
		{ label: m('payments.col.invoice') },
		{ label: m('payments.col.limit'), class: 'right' },
		{ label: m('payments.col.charged'), class: 'right' },
		{ label: m('payments.col.status') },
		{ label: m('payments.col.expires') },
		{ class: 'actions-col' }
	]);

	let REBATES_COLUMNS = $derived([
		{ label: m('payments.rebates.col.period') },
		{ label: m('payments.rebates.col.card') },
		{ label: m('payments.rebates.col.rate'), class: 'right' },
		{ label: m('payments.rebates.col.amount'), class: 'right' },
		{ label: m('payments.rebates.col.status') },
		// Actions column: the one available lifecycle step. Always rendered so
		// the table keeps its shape for a terminal (`paid_out`) row.
		{ class: 'actions-col' }
	]);

	type Tab = 'queue' | 'history' | 'runs' | 'cards';
	const TABS: Tab[] = ['queue', 'history', 'runs', 'cards'];

	// Tab + History-tab search / status filter / column sort are all URL-backed
	// (`?tab=&search=&status=&sort=&order=`) so a reload / back / shared link
	// reproduces the same view — mirrors `/contracts` + `/expenses`. `tab` is
	// included because the History search/status controls are meaningless
	// without knowing which tab is showing. See `syncUrl()`.
	let activeTab = $state<Tab>(
		TABS.includes($page.url.searchParams.get('tab') as Tab)
			? ($page.url.searchParams.get('tab') as Tab)
			: 'queue'
	);
	let search = $state($page.url.searchParams.get('search') ?? '');
	let activeStatus = $state<PaymentStatus | 'all'>(
		PAYMENT_STATUSES.includes($page.url.searchParams.get('status') as PaymentStatus)
			? ($page.url.searchParams.get('status') as PaymentStatus)
			: 'all'
	);

	let sortField = $state<string | null>($page.url.searchParams.get('sort'));
	let sortOrder = $state<SortOrder>(($page.url.searchParams.get('order') as SortOrder) ?? 'desc');

	// WRITER — every read untracked, `$page.url` included (issue #168 / the
	// self-trigger on replaceState). Called from the tab/status effect, the
	// search debounce, the sort handler, and each tab button.
	function syncUrl() {
		untrack(() => {
			const url = new URL($page.url);
			if (activeTab !== 'queue') url.searchParams.set('tab', activeTab);
			else url.searchParams.delete('tab');
			const s = search.trim();
			if (s && activeTab === 'history') url.searchParams.set('search', s);
			else url.searchParams.delete('search');
			if (activeStatus !== 'all' && activeTab === 'history')
				url.searchParams.set('status', activeStatus);
			else url.searchParams.delete('status');
			if (sortField && activeTab === 'history') {
				url.searchParams.set('sort', sortField);
				url.searchParams.set('order', sortOrder);
			} else {
				url.searchParams.delete('sort');
				url.searchParams.delete('order');
			}
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	function selectTab(tab: Tab) {
		activeTab = tab;
		syncUrl();
	}

	function handleHistorySort(field: string) {
		const next = toggleSort({ field: sortField, order: sortOrder }, field);
		sortField = next.field;
		sortOrder = next.order;
		syncUrl();
		paymentStore.fetch(buildParams()).catch(() => {}); // noqa: raw-fetch-in-component — store method; routes through api.get
	}


	// Summary
	// Money fields arrive as exact Decimal STRINGS from the backend (money
	// invariant — never float). formatMoney/Number() coerce at each use site.
	interface Summary {
		total_paid: string;
		total_pending: string;
		payment_count: number;
		total_rebates: string;
		queue_count: number;
		// `Payment.amount` is INVOICE currency (the home-currency debit lives on
		// `source_amount`), so these totals used to be face-value sums across
		// currencies. The backend converts into the org's reporting currency now
		// and says which currency that is — render it, or the figure is a bare
		// number whose denomination the reader has to assume.
		currency?: string;
		// Payments it could not convert, excluded from the totals above.
		unconverted_payment_count?: number;
		// Rebates left out of `total_rebates` for being denominated in another
		// currency. A DIFFERENT question from the count above: that one is a
		// payment whose reporting figure could not be established, this one is a
		// rebate whose currency is known and simply is not this one.
		excluded_rebate_count?: number;
	}
	let summary = $state<Summary | null>(null);

	// Queue
	interface QueueItem {
		id: string;
		invoice_number: string;
		vendor_name: string;
		// Exact Decimal STRING money (never float); coerce with Number() to sum.
		amount: string;
		currency: string;
		due_date: string | null;
		payment_terms: string | null;
		status: string;
		is_overdue: boolean;
		discount_eligible: boolean;
		discount_date: string | null;
		// discount_percent is a rate, not money — stays a JSON number.
		discount_percent: number | null;
		discount_amount: string | null;
		// --- What a payment run would refuse ---------------------------------
		// `services/payment_runs.run_refusal_reasons` is the ONE predicate set
		// `create_payment_run_for_invoices` enforces, and the queue reports its
		// verdict per row so the page can never offer something the run builder
		// then 409s (which refuses the WHOLE batch, with no way to bisect it).
		//
		// `blocked` — refused on EVERY rail (an unresolved payment-blocking
		// exception, or an invoice fully covered by applied credit memos). The
		// checkbox is disabled.
		//
		// `required_method` — refused on every rail BUT this one: a live virtual
		// card already claims the invoice, and `virtual_card` converges onto
		// that card instead of opening a second outflow. NOT blocked — the row
		// stays selectable with its rail pinned, because refusing it outright
		// would break the documented mint-a-card-then-run-it flow.
		//
		// All three stay OPTIONAL so an older backend leaves this page behaving
		// exactly as it does today: every read goes through `isBlocked()` /
		// `requiredMethod()` below, which treat "absent" as "no refusal".
		//
		// `blocked_reason` is a CODE from the backend's fixed, PII-free
		// vocabulary (an exception TYPE, or one of `payment_runs`' own reason
		// constants) — never prose — so it renders in all six shipped locales.
		blocked?: boolean;
		blocked_reason?: string | null;
		required_method?: string | null;
	}

	// Per-currency slice of the WHOLE queue (or of the whole selectable set, on
	// `/queue/ids`). Money is exact decimal STRING — the backend sums it in SQL
	// so the frontend never reduces a paginated page into a total that would
	// then contradict the "N of M" footer. `total_amount` on `/queue` covers
	// every payable row; on `/queue/ids` only the selectable (unblocked) ones.
	interface QueueCurrencySlice {
		currency: string;
		count: number;
		total_amount: string;
		total_savings: string;
	}
	interface QueueResponse {
		items: QueueItem[];
		total: number;
		page: number;
		page_size: number;
		selectable_total: number;
		blocked_total: number;
		currency?: string;
		unconverted_count?: number;
		by_currency?: QueueCurrencySlice[];
	}
	interface QueueIdsResponse {
		ids: string[];
		total: number;
		truncated: boolean;
		currency?: string;
		by_currency?: QueueCurrencySlice[];
	}

	const QUEUE_PAGE_SIZE = 20;
	let queue = $state<QueueItem[]>([]);
	let queuePage = $state(1);
	let queueTotal = $state(0);
	let queueLoadingMore = $state(false);
	// Whole-queue counts + per-currency money, straight off the response — a
	// KPI/banner derived from the loaded page would undercount past page 1.
	let queueBlockedTotal = $state(0);
	let queueSelectableTotal = $state(0);
	let queueByCurrency = $state<QueueCurrencySlice[]>([]);
	let hasMoreQueue = $derived(queue.length < queueTotal);
	// Loading / errored are tracked separately from `queue.length === 0` because
	// the empty copy here is a claim about MONEY — "no invoices are ready for
	// payment". Rendered while the fetch is in flight, or left standing forever
	// after a failed one, that reads as "nothing is owed" when the truth is "we
	// have not looked". Same three-state rule the /exceptions and /notifications
	// lists follow; see `frontend/CLAUDE.md` § Data tables.
	let queueLoading = $state(true);
	let queueErrored = $state(false);
	// Queue rows the backend could not express in the reporting currency, so
	// its own roll-ups exclude them. Rows still render in their OWN currency.
	let queueUnconvertedCount = $state(0);

	// Queue selection and payment run creation
	let selectedQueue = $state<Set<string>>(new Set());
	// True once "Select all N matching" resolved the WHOLE selectable queue —
	// not just the loaded page — into `selectedQueue` via `GET
	// /api/payments/queue/ids`. While true the prune effect must NOT narrow the
	// selection down to the loaded rows (that would drop every id past the
	// page), and the pay-bar money totals come from the backend's own
	// per-currency breakdown of that set rather than the loaded rows. Mirrors
	// `/invoices`' `selectedAllMatching`. Reset by clear / deselect-all / a
	// fresh queue load.
	let selectedAllQueueMatching = $state(false);
	let selectingAllQueue = $state(false);
	let selectAllQueueGroups = $state<QueueCurrencySlice[]>([]);
	let paymentMethods = $state<Record<string, string>>({});
	let creatingRun = $state(false);
	let showReview = $state(false);

	// Run detail modal — opened both after creating a draft from the queue
	// and when clicking a row in the Runs tab.
	let activeRunId = $state<string | null>(null);

	/** Would a payment run refuse this row on EVERY rail?
	 *
	 *  The single read of `blocked`, so "an older backend doesn't send the
	 *  field" is answered in exactly one place: an absent / non-`true` value is
	 *  NOT blocked, which is byte-for-byte today's behaviour. */
	function isBlocked(item: QueueItem): boolean {
		return item.blocked === true;
	}

	/** The one rail this row can still be paid on, or `null` for "any".
	 *
	 *  Non-null means a run would refuse every OTHER rail (today: a live
	 *  virtual card already claims the invoice). The row stays selectable; its
	 *  method is pinned to this value everywhere a method is chosen or sent. */
	function requiredMethod(item: QueueItem): string | null {
		return item.required_method || null;
	}

	/** The method to send for a queue row: the operator's pick, unless the
	 *  backend pinned the rail — a pinned row is the only rail the run builder
	 *  accepts, so an operator's stale pick must never win over it. */
	function methodFor(item: QueueItem): string {
		return requiredMethod(item) ?? paymentMethods[item.id] ?? 'ach';
	}

	/** Localised sentence for why a run would refuse — or constrain — a row.
	 *
	 *  Maps the backend's fixed reason vocabulary; an unrecognised or missing
	 *  code falls back to the generic reason rather than rendering a raw
	 *  identifier at the operator. */
	function blockedReason(item: QueueItem): string {
		switch (item.blocked_reason) {
			case 'duplicate':
				return m('payments.queue.blocked.duplicate');
			case 'fraud_flag':
				return m('payments.queue.blocked.fraudFlag');
			case 'line_total_mismatch':
				return m('payments.queue.blocked.lineTotalMismatch');
			case 'payment_reconciliation':
				// The fourth member of PAYMENT_BLOCKING_EXCEPTION_TYPES, and the
				// one this switch was missing — so a row held because an earlier
				// payment's fate at the rail is unknown fell through to the
				// generic string and told the operator nothing actionable. The
				// default arm stays for codes this build genuinely doesn't know.
				return m('payments.queue.blocked.paymentReconciliation');
			case 'fully_credited':
				return m('payments.queue.blocked.fullyCredited');
			case 'live_virtual_card':
				// Not a block — the row is pinned to the card rail. Its own
				// sentence says so, rather than the "can't be paid" generic.
				return m('payments.queue.blocked.liveVirtualCard');
			default:
				return m('payments.queue.blocked.generic');
		}
	}

	// The rows a payment run could actually be built from. Everything that
	// counts, sums, selects-all or prunes reads THIS, never `queue` — a blocked
	// row that slipped into the selection would take the whole draft down with
	// a 409. Page-scoped: it covers the LOADED rows, which is why the blocked
	// BANNER below reads `queueBlockedTotal` (whole set) instead.
	let selectableQueue = $derived(queue.filter((q) => !isBlocked(q)));

	// Prune the queue selection to ids still SELECTABLE whenever the loaded rows
	// change (after an execute/void via onRunChanged/commitVoid drops the
	// just-handled invoice — or after a refresh reveals that a selected row has
	// since picked up a blocking exception). Otherwise the pay-bar count
	// (`selectedQueue.size`) outruns the rows the money totals actually sum
	// over. No-op (same Set) when clean.
	//
	// SKIPPED in "select all N matching" mode: that selection deliberately
	// holds ids past the loaded page (resolved via `/queue/ids`), and pruning
	// to the loaded rows would silently discard them — same rule as
	// `/invoices`' `selectedAllMatching`.
	$effect(() => {
		if (selectedAllQueueMatching) return;
		const pruned = pruneSelection(
			selectedQueue,
			selectableQueue.map((q) => q.id)
		);
		if (pruned !== selectedQueue) selectedQueue = pruned;
	});

	let allQueueSelected = $derived(
		selectableQueue.length > 0 && selectableQueue.every(q => selectedQueue.has(q.id))
	);

	let selectedRows = $derived(queue.filter((q) => selectedQueue.has(q.id)));

	/** One currency's subtotal, ready for {@link formatGroups} — `total` is an
	 *  exact decimal STRING when it comes from the backend, a BigInt-scaled
	 *  number when `groupAmountsByCurrency` summed the loaded rows. Never a
	 *  cross-currency sum either way. */
	type DisplayGroup = { currency: string; total: MoneyAmount };

	function sliceToGroups(slices: QueueCurrencySlice[], key: 'total_amount' | 'total_savings'): DisplayGroup[] {
		return slices
			.filter((s) => Number(s[key]) > 0)
			.map((s) => ({ currency: s.currency, total: s[key] }));
	}

	// Money arrives as string-Decimal, and each row carries its OWN currency.
	// Reducing them into one figure was doubly wrong: it coerced exact decimals
	// through binary floats, and — worse — it added EUR to USD and rendered the
	// result in the org default, so a EUR 100 + USD 100 selection read as one
	// meaningless "200". `groupAmountsByCurrency` sums exactly *within* each
	// currency (BigInt-scaled, never a float reduce) and never across them; the
	// UI shows each currency's subtotal side by side. No FX conversion happens
	// on a read — see `backend/docs/multi-currency.md`.
	//
	// In "select all N matching" mode the selection reaches past the loaded
	// page, so the per-currency totals come from the backend's own breakdown of
	// the whole selectable set (`selectAllQueueGroups`) rather than the loaded
	// rows — otherwise the pay-bar would understate a 50-invoice selection to
	// whatever 20 rows happen to be on screen.
	let selectedGroups = $derived<DisplayGroup[]>(
		selectedAllQueueMatching
			? sliceToGroups(selectAllQueueGroups, 'total_amount')
			: groupAmountsByCurrency(selectedRows, orgCurrency.currency)
	);

	// `.filter(total > 0)` preserves the old `selectedSavings > 0` guard: a
	// currency whose discounts sum to nothing must not add a "· save $0.00".
	// A comparison against zero is a predicate, not money arithmetic.
	let selectedSavingsGroups = $derived<DisplayGroup[]>(
		selectedAllQueueMatching
			? sliceToGroups(selectAllQueueGroups, 'total_savings')
			: groupAmountsByCurrency(
					selectedRows.filter((q) => q.discount_eligible && q.discount_amount)
						.map((q) => ({ amount: q.discount_amount, currency: q.currency })),
					orgCurrency.currency
				).filter((g) => g.total > 0)
	);

	// The whole queue's early-pay savings. Read off the response's own
	// per-currency breakdown (`queueByCurrency`) — the queue is paginated now,
	// so the loaded rows are no longer the whole queue. Still never a
	// cross-currency sum: the backend groups by currency in SQL.
	let queueSavingsGroups = $derived<DisplayGroup[]>(
		sliceToGroups(queueByCurrency, 'total_savings')
	);

	// `create_payment_run_for_invoices` 422s a run spanning more than one
	// currency ("All invoices in a payment run must share the same currency"),
	// because `PaymentRun.total_amount` is one bare Numeric with no currency of
	// its own. So a mixed selection isn't just unreadable — it can't be
	// submitted. Say so up front instead of letting the draft fail.
	let mixedCurrencySelection = $derived(spansMultipleCurrencies(selectedGroups as CurrencyGroup[]));

	/** Render a set of per-currency subtotals as one honest label.
	 *
	 *  Single currency → exactly what the pay-bar always showed, but in the
	 *  row's OWN currency rather than the org default. Several → each subtotal
	 *  side by side, separated (never added). Empty → a zero in the org
	 *  currency, which is what "nothing selected" costs. */
	function formatGroups(groups: DisplayGroup[]): string {
		// A deliberate display choice, not a fallback: nothing selected costs
		// nothing, and a zero reads the same in any currency.
		if (groups.length === 0) return formatMoney(0, { currency: orgCurrency.currency });
		// The per-currency rendering itself lives in `currencyGroups` now, so
		// /expenses' KPI rollup and this pay bar can't drift on it; only the
		// "nothing selected" reading stays a per-caller display choice.
		return formatCurrencyTotals(groups, orgCurrency.currency).join(' · ');
	}

	// The server's refusal, kept on screen. A 409 from
	// `create_payment_run_for_invoices` NAMES the offending invoice numbers
	// ("Invoice(s) have an unresolved duplicate/fraud/line-total exception and
	// can't be paid until it's cleared: INV-1001, INV-1002"), and `ApiError`
	// already carries that text — but a 5-second toast is the wrong home for
	// the one thing that says which of twenty selected rows is at fault. This
	// renders it in the review panel until the operator changes the selection.
	let createRunError = $state('');

	function toggleQueueSelect(item: QueueItem) {
		// Belt-and-braces: the checkbox is disabled for a blocked row, but the
		// guard lives with the state change so no future caller can bypass it.
		if (isBlocked(item)) return;
		createRunError = '';
		const next = new Set(selectedQueue);
		if (next.has(item.id)) next.delete(item.id);
		else next.add(item.id);
		selectedQueue = next;
		// Seed the method from the row's own pin when it has one — a
		// card-claimed invoice is only payable on that rail, so defaulting it
		// to ACH would stage a run the backend refuses.
		if (!paymentMethods[item.id]) paymentMethods[item.id] = methodFor(item);
	}

	function toggleQueueSelectAll() {
		createRunError = '';
		if (allQueueSelected) {
			selectedQueue = new Set();
			selectedAllQueueMatching = false;
		} else {
			selectedQueue = new Set(selectableQueue.map(q => q.id));
			for (const q of selectableQueue) {
				if (!paymentMethods[q.id]) paymentMethods[q.id] = methodFor(q);
			}
		}
	}

	// Resolve and select EVERY selectable (unblocked) invoice in the queue —
	// not just the loaded page — via `GET /api/payments/queue/ids`. The header
	// checkbox only ever covers the loaded rows; without this, "create draft
	// run" silently acted on the loaded page and skipped the rest. The response
	// carries the whole set's per-currency money breakdown, so the pay-bar
	// stays honest without holding every row. Mirrors `/invoices`'
	// `selectAllMatching()`.
	async function selectAllQueueMatching() {
		selectingAllQueue = true;
		try {
			const res = await api.get<QueueIdsResponse>('/api/payments/queue/ids');
			selectedQueue = new Set(res.ids);
			for (const id of res.ids) {
				if (!paymentMethods[id]) paymentMethods[id] = 'ach';
			}
			selectAllQueueGroups = res.by_currency ?? [];
			selectedAllQueueMatching = true;
			createRunError = '';
			if (res.truncated) {
				toast(
					m('payments.queue.selectAllTruncated', { shown: res.ids.length, total: res.total }),
					'error'
				);
			} else {
				toast(m('payments.queue.selectedAllMatching', { n: res.ids.length }), 'success');
			}
		} catch (err) {
			toast(err instanceof Error ? err.message : m('payments.queue.selectAllFailed'), 'error');
		} finally {
			selectingAllQueue = false;
		}
	}

	function clearQueueSelection() {
		selectedQueue = new Set();
		selectedAllQueueMatching = false;
		showReview = false;
		createRunError = '';
	}

	async function createDraftRun() {
		if (selectedQueue.size === 0) return;
		if (mixedCurrencySelection) {
			// Advance signal for the backend's own 422 — never send a request we
			// already know is refused.
			createRunError = m('payments.queue.mixedCurrencyBlocked');
			toast(createRunError, 'error');
			return;
		}
		createRunError = '';
		creatingRun = true;
		try {
			// A pinned row's rail wins over the stored pick. `/queue/ids`
			// already excludes pinned rows from "select all N matching", so an
			// id we never loaded can't be one — but a row selected by hand and
			// then re-fetched with a fresh pin must still go out on it.
			const byId = new Map(queue.map((q) => [q.id, q]));
			const items = [...selectedQueue].map((id) => {
				const row = byId.get(id);
				return {
					invoice_id: id,
					method: row ? methodFor(row) : paymentMethods[id] || 'ach',
				};
			});

			// Create as a draft. The backend always creates with status='draft';
			// a separate /execute call moves money. The modal lets the user
			// review what's about to be paid before triggering it.
			const run = await api.post<{ id: string; message: string }>(
				'/api/payments/runs',
				{ items }
			);

			toast('Draft payment run created — review and execute', 'success');
			selectedQueue = new Set();
			showReview = false;
			activeRunId = run.id;

			// Refresh runs list so the new draft shows up immediately.
			await loadRuns();
		} catch (err) {
			// `ApiError.message` is `formatApiDetail(body.detail, …)`, so the
			// backend's 409 detail — which names the offending invoice numbers —
			// arrives intact. Surface it in BOTH places: the toast announces it
			// (assertive live region), the panel keeps it readable.
			const detail = err instanceof Error ? err.message : m('payments.queue.createFailed');
			createRunError = detail;
			toast(detail, 'error');
		} finally {
			creatingRun = false;
		}
	}

	async function onRunChanged() {
		// Called by the modal after a successful execute or cancel.
		// Refresh everything the user might be looking at.
		await loadSummary();
		await loadQueue();
		await loadRuns();
		if (activeTab === 'history') {
			await paymentStore.fetch(buildParams()); // noqa: raw-fetch-in-component — store method; routes through api.get
			await fetchPaymentCounts();
		}
	}

	// Void modal — cfo/admin can void a completed or in-flight payment.
	let voidTarget = $state<Payment | null>(null);
	let voidReason = $state('');
	let voiding = $state(false);
	// The voided payment's card verdict, once the void has run. The dialog stays
	// open on it: both legs of a void are best-effort, so a bare success toast
	// would let the operator read "voided" as "the card is shut" — and a card
	// the provider did not close is still live and bearer-spendable. `null` is
	// the pre-void state and every case with nothing to report (`closed`,
	// `no_card`, a non-card payment), which closes the dialog as before.
	let voidCardResult = $state<Payment | null>(null);
	let retryingCardCancel = $state(false);
	let cardRetryFailed = $state(false);

	const voidCardDisposition = $derived(voidCardResult?.void_card_disposition ?? null);
	// Only the retryable verdict gets a button. `not_closed_final` (the card was
	// already charged) is shown, because the card was NOT closed and the
	// operator must know — but a retry there can only fail forever, so offering
	// one would be a control that cannot work.
	const canRetryCardCancel = $derived(voidCardDisposition === 'not_closed_retryable');

	function openVoid(p: Payment) {
		voidTarget = p;
		voidReason = '';
		voidCardResult = null;
		cardRetryFailed = false;
	}

	function closeVoid() {
		voidTarget = null;
		voidCardResult = null;
		cardRetryFailed = false;
	}

	/** Refresh every surface a void (or a later card close) can change. */
	async function reloadAfterVoid() {
		await Promise.all([
			loadSummary(),
			loadQueue(),
			paymentStore.fetch(buildParams()), // noqa: raw-fetch-in-component — store method; routes through api.get
			fetchPaymentCounts(),
		]);
	}

	async function commitVoid() {
		if (!voidTarget) return;
		const reason = voidReason.trim();
		if (!reason) {
			toast('Reason is required for a void', 'error');
			return;
		}
		voiding = true;
		try {
			const voided = await voidPayment(voidTarget.id, reason);
			toast('Payment voided', 'success');
			// Branch on the SERVER's verdict, never on the raw outcome tag: a tag
			// this build doesn't know must not read as "closed" (decisions §132).
			const disposition = voided.void_card_disposition ?? null;
			if (disposition === 'not_closed_retryable' || disposition === 'not_closed_final') {
				voidCardResult = voided;
				voidReason = '';
			} else {
				closeVoid();
				voidReason = '';
			}
			await reloadAfterVoid();
		} catch (err) {
			const e = err as { detail?: string; message?: string } | null;
			toast(e?.detail ?? e?.message ?? 'Void failed', 'error');
		} finally {
			voiding = false;
		}
	}

	/**
	 * Finish the void's card leg. Server-side this is `payment.void`-gated and
	 * 409s on anything but an already-voided card payment, so it can only ever
	 * complete a reversal — never close a card while a payment still claims the
	 * money is in flight (decisions §96).
	 */
	async function commitCardCancelRetry() {
		if (!voidCardResult) return;
		retryingCardCancel = true;
		cardRetryFailed = false;
		try {
			const retried = await retryVoidCardCancel(voidCardResult.id);
			voidCardResult = retried;
			if ((retried.void_card_disposition ?? null) === 'closed') {
				toast(m('payments.void.card.closed'), 'success');
			} else {
				cardRetryFailed = true;
			}
			await reloadAfterVoid();
		} catch (err) {
			const e = err as { detail?: string; message?: string } | null;
			cardRetryFailed = true;
			toast(e?.detail ?? e?.message ?? m('payments.void.card.retryFailed'), 'error');
		} finally {
			retryingCardCancel = false;
		}
	}

	function canVoid(p: Payment): boolean {
		// Server-side gate: require_permission(payment.void) — defaults to
		// admin/cfo, but a custom role can be granted it. Status: anything that
		// isn't already terminal-by-failure.
		//
		// `pending_compliance` is deliberately NOT offered here: nothing ever
		// reached the processor, so there is no rail to reverse. Its purpose-built
		// exits are Release / Dismiss below, which also close the
		// `payment_compliance_hold` exception the hold opened — a void would not.
		if (auth.user && auth.can(PERM_PAYMENT_VOID)) {
			return p.status === 'completed' || p.status === 'submitted' || p.status === 'processing';
		}
		return false;
	}

	// --- Compliance hold (pending_compliance) --------------------------------
	// A payment the sanctions/KYC gate parked at `pending_compliance` has two
	// server-side exits, each behind the permission that matches what it does:
	//   * release  → re-runs the SAME compliance-then-adapter path and can move
	//                money, so `payment.execute` (mirrors POST .../execute).
	//   * dismiss  → gives up, flips to `failed`, moves nothing, so
	//                `payment.void` (mirrors POST .../void).
	// Both 409 outside `pending_compliance`; hiding them elsewhere keeps the row
	// honest, and the backend enforces regardless.
	type ComplianceMode = 'release' | 'dismiss';
	let complianceTarget = $state<Payment | null>(null);
	let complianceMode = $state<ComplianceMode>('release');
	let complianceReason = $state('');
	let complianceBusy = $state(false);

	function canReleaseHold(p: Payment): boolean {
		return (
			p.status === 'pending_compliance' && !!auth.user && auth.can(PERM_PAYMENT_EXECUTE)
		);
	}

	function canDismissHold(p: Payment): boolean {
		return p.status === 'pending_compliance' && !!auth.user && auth.can(PERM_PAYMENT_VOID);
	}

	function openCompliance(p: Payment, mode: ComplianceMode) {
		complianceTarget = p;
		complianceMode = mode;
		complianceReason = '';
	}

	function closeCompliance() {
		complianceTarget = null;
		complianceReason = '';
	}

	async function refreshAfterCompliance() {
		await Promise.all([
			loadSummary(),
			loadQueue(),
			paymentStore.fetch(buildParams()), // noqa: raw-fetch-in-component — store method; routes through api.get
			fetchPaymentCounts()
		]);
	}

	async function commitCompliance() {
		if (!complianceTarget) return;
		const id = complianceTarget.id;
		const mode = complianceMode;
		const reason = complianceReason.trim();
		if (mode === 'dismiss' && !reason) {
			toast(m('payments.compliance.reasonRequired'), 'error');
			return;
		}
		complianceBusy = true;
		try {
			if (mode === 'release') {
				// The response carries the REAL post-release status: the backend
				// re-runs the gate, so a payment whose hold hasn't actually cleared
				// comes back still `pending_compliance`. Report what happened
				// instead of claiming success — this is never a bypass.
				const updated = await api.post<Payment>(`/api/payments/${id}/compliance/release`, {});
				toast(
					updated.status === 'pending_compliance'
						? m('payments.compliance.release.stillHeld')
						: m('payments.compliance.release.released'),
					updated.status === 'pending_compliance' ? 'error' : 'success'
				);
			} else {
				await api.post<Payment>(`/api/payments/${id}/compliance/dismiss`, { reason });
				toast(m('payments.compliance.dismiss.done'), 'success');
			}
			closeCompliance();
			await refreshAfterCompliance();
		} catch (err) {
			const fallback =
				mode === 'release'
					? m('payments.compliance.release.failed')
					: m('payments.compliance.dismiss.failed');
			toast(err instanceof Error ? err.message : fallback, 'error');
		} finally {
			complianceBusy = false;
		}
	}

	// --- Settlement acceptance (the under-settlement hold's other exit) -------
	// A `completed` payment whose rail settled LESS than AP authorized (or in a
	// currency we never authorized) does not discharge the invoice:
	// `payment_settlement.settlement_coverage` holds it at `payment_scheduled`
	// rather than reporting it settled in full. `backend/docs/payments.md`
	// documents exactly two exits — accept the shortfall as final, or void and
	// re-pay — and only void was wired here, which is the wrong action: the
	// money already left, and a void invites a second payment for it.
	//
	// Whether a reported figure actually covers the invoice is the SERVER's
	// call (it needs both authorized legs and the invoice currency, neither of
	// which is on this wire) — and comparing two amounts client-side is barred
	// outright by frontend/CLAUDE.md § Money formatting. So the row offers the
	// action wherever a figure was reported at all, and the dialog asks the
	// backend the one question it can answer cheaply and without money math:
	// is the invoice still HELD? An invoice past `payment_scheduled` has
	// nothing to accept, and the confirm stays out of reach rather than
	// becoming a button that can only 409.
	let settlementTarget = $state<Payment | null>(null);
	let settlementReason = $state('');
	let settlementBusy = $state(false);
	// Invoice status behind the target payment: `null` while in flight,
	// `'?'` when we could not read it (fail OPEN — the backend re-checks).
	let settlementInvoiceStatus = $state<string | null>(null);
	let settlementError = $state<string | null>(null);

	/** The invoice status the backend's own release guard requires. */
	const SETTLEMENT_HELD_STATUS = 'payment_scheduled';

	function canAcceptSettlement(p: Payment): boolean {
		// Presence, never a comparison: `settled_amount` is null when no rail
		// ever reported a figure, which the backend treats as "nothing
		// contradicts this invoice" — there is no shortfall to accept.
		return (
			p.status === 'completed' &&
			p.settled_amount !== null &&
			p.settled_amount !== undefined &&
			!!auth.user &&
			auth.can(PERM_PAYMENT_EXECUTE)
		);
	}

	async function openSettlement(p: Payment) {
		settlementTarget = p;
		settlementReason = '';
		settlementError = null;
		settlementInvoiceStatus = null;
		const invoiceId = p.invoice_id;
		try {
			const inv = await api.get<{ status: string }>(`/api/invoices/${invoiceId}`);
			// Guard against a slow response landing after the dialog moved on.
			if (settlementTarget?.invoice_id === invoiceId) settlementInvoiceStatus = inv.status;
		} catch {
			// Could not read the invoice — say so rather than guessing either
			// way. `'?'` renders the unknown state, which still offers the
			// confirm because the backend re-checks and 409s if it isn't held.
			if (settlementTarget?.invoice_id === invoiceId) settlementInvoiceStatus = '?';
		}
	}

	function closeSettlement() {
		settlementTarget = null;
		settlementReason = '';
		settlementError = null;
		settlementInvoiceStatus = null;
	}

	async function commitSettlement() {
		if (!settlementTarget) return;
		const id = settlementTarget.id;
		const reason = settlementReason.trim();
		if (!reason) {
			toast(m('payments.settlement.reasonRequired'), 'error');
			return;
		}
		settlementBusy = true;
		settlementError = null;
		try {
			await acceptPaymentSettlement(id, reason);
			toast(m('payments.settlement.done'), 'success');
			closeSettlement();
			await refreshAfterCompliance();
		} catch (err) {
			// Persistent, not a toast that fades: the backend's 409 detail
			// ("already covers", "no held invoice") is the actionable half of
			// the refusal and the operator needs it while the dialog is open.
			settlementError = err instanceof Error ? err.message : m('payments.settlement.failed');
		} finally {
			settlementBusy = false;
		}
	}

	// --- Run-level ERP sync-back retry ---------------------------------------
	// `services/payment_erp_sync` is the ONLY path that flips
	// `payment_scheduled → paid`, and it is dispatched one-shot per terminal
	// event. A leg that raised leaves the money moved and the invoice stranded
	// forever behind an `erp_reconciliation` exception whose description names
	// this endpoint. Voiding is explicitly NOT the exit for that state.
	//
	// Idempotent by construction and moves no money, so this is a confirm — not
	// an armed two-click destructive action — followed by the REAL counts.
	let erpSyncTarget = $state<RunItem | null>(null);
	let erpSyncBusy = $state(false);
	let erpSyncResult = $state<RunErpSyncResult | null>(null);
	let erpSyncError = $state<string | null>(null);

	// Statuses that can't hold a settled payment: `draft` has never been
	// dispatched and `cancelled` has no payments left, so the endpoint would
	// 409 on both. Everything else may carry one — the backend counts.
	const ERP_SYNC_INELIGIBLE_RUN_STATUSES = new Set(['draft', 'cancelled']);

	function canSyncRunErp(run: RunItem): boolean {
		return (
			!ERP_SYNC_INELIGIBLE_RUN_STATUSES.has(run.status) &&
			!!auth.user &&
			auth.can(PERM_PAYMENT_EXECUTE)
		);
	}

	function openErpSync(run: RunItem) {
		erpSyncTarget = run;
		erpSyncResult = null;
		erpSyncError = null;
	}

	function closeErpSync() {
		erpSyncTarget = null;
		erpSyncResult = null;
		erpSyncError = null;
	}

	async function commitErpSync() {
		if (!erpSyncTarget) return;
		erpSyncBusy = true;
		erpSyncError = null;
		try {
			erpSyncResult = await retryRunErpSync(erpSyncTarget.id);
			// `transitioned` is the only count that answers "did this recover
			// anything" — `synced` counts legs that RAN and stays true on a
			// repeat. Reporting `synced` as success would claim a recovery on
			// every retry of an already-clean run.
			await onRunChanged();
		} catch (err) {
			erpSyncError = err instanceof Error ? err.message : m('payments.erpSync.failed');
		} finally {
			erpSyncBusy = false;
		}
	}

	async function downloadRemittance(p: Payment) {
		try {
			const blob = await api.downloadBlob(`/api/payments/${p.id}/remittance`);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `remittance-${p.reference ?? p.id.slice(0, 8)}.pdf`;
			document.body.appendChild(a);
			a.click();
			a.remove();
			URL.revokeObjectURL(url);
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Could not download remittance', 'error');
		}
	}

	// Runs
	interface RunItem {
		id: string;
		status: string;
		total_amount: number | null;
		/** What `total_amount` is denominated in, off `PaymentRunResponse`.
		 *  `null` when the server could not prove one — see
		 *  `$lib/types/payment.ts::PaymentRun.currency`. */
		currency: string | null;
		executed_at: string | null;
		created_at: string;
		payment_count: number;
	}
	let runs = $state<RunItem[]>([]);
	// Three states, not two — the same rule the Queue and History tabs in this
	// same file already follow. "No payment runs yet." is a claim about whether
	// this tenant has ever moved money; rendered during the first fetch and left
	// standing forever after a failed one, it read as "you have none" when the
	// truth was "we could not look". The Runs tab was the last list here with no
	// loading flag at all.
	let runsLoading = $state(true);
	let runsErrored = $state(false);

	function buildParams(): Record<string, string> {
		// Paging (page / page_size) is owned by the store's Load-More; only
		// the filter params belong here.
		const params: Record<string, string> = {};
		if (activeStatus !== 'all') params.status = activeStatus;
		// `untrack`: `buildParams()` is also called from the tab-change `$effect`
		// below. A plain read of `search` here would make THAT effect depend on
		// `search` too (Svelte tracks reads transitively through called
		// functions), so every keystroke would re-fire it — an immediate,
		// un-debounced fetch racing the dedicated debounce timer further down.
		// `untrack` still reads the current value (the request still carries the
		// live search term); it just stops that read from registering as a
		// dependency of whichever effect happens to be calling this.
		const currentSearch = untrack(() => search);
		if (currentSearch.trim()) params.search = currentSearch.trim();
		// Same `untrack` reasoning as `search` above — this function is also
		// called from the tab-change effect and the debounced search fetch.
		const currentSort = untrack(() => sortField);
		if (currentSort) {
			params.sort = currentSort;
			params.order = untrack(() => sortOrder);
		}
		return params;
	}

	// Three states, not two: a failed load must not read as "nothing matched".
	let historyEmptyMessage = $derived(
		paymentStore.errored ? m('payments.history.empty.errored') : m('payments.history.empty')
	);

	let searchTimer: ReturnType<typeof setTimeout>;
	function debouncedFetch() {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => {
			// `.catch`: the store re-throws so an awaiting caller keeps its own
			// handling, but this call is fire-and-forget — `paymentStore.errored`
			// is what renders the failure state, so swallow the rejection rather
			// than log an unhandled one.
			if (activeTab === 'history') {
				syncUrl();
				paymentStore.fetch(buildParams()).catch(() => {}); // noqa: raw-fetch-in-component — store method; routes through api.get
				// The chips share the list's population, so they re-tally with it.
				fetchPaymentCounts();
			}
		}, 300);
	}

	$effect(() => {
		orgCurrency.ensureLoaded();
		loadSummary();
		loadQueue();
	});

	$effect(() => {
		syncUrl();
		if (activeTab === 'history') {
			activeStatus;
			// `.catch`: fire-and-forget from an $effect — `paymentStore.errored`
			// renders the failure, so the re-thrown rejection has no other home.
			paymentStore.fetch(buildParams()).catch(() => {}); // noqa: raw-fetch-in-component — store method; routes through api.get
			fetchPaymentCounts();
		} else if (activeTab === 'queue') {
			loadQueue();
		} else if (activeTab === 'runs') {
			loadRuns();
		} else if (activeTab === 'cards') {
			loadCards();
			loadRebates();
		}
	});

	$effect(() => {
		search;
		debouncedFetch();
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone and lands a stale list into the shared store.
		return () => clearTimeout(searchTimer);
	});

	// One sequencer PER independent list, never one shared counter — this page
	// holds four surfaces that reload on different triggers (a tab switch, an
	// execute, a void, a compliance release), and `onRunChanged` fires three of
	// them at once. A single counter would let the runs response retire the
	// queue's in-flight request and blank the list it was about to fill.
	// See `frontend/CLAUDE.md` § Sequencing list fetches.
	const summarySequence = createRequestSequencer();
	const queueSequence = createRequestSequencer();
	const runsSequence = createRequestSequencer();
	const cardsSequence = createRequestSequencer();
	const rebatesSequence = createRequestSequencer();

	async function loadSummary() {
		const token = summarySequence.start();
		try {
			const data = await api.get<Summary>('/api/payments/summary');
			if (!summarySequence.canCommit(token)) return; // superseded by a newer load
			summary = data;
		} catch (err) {
			if (summarySequence.isCurrentRequest(token)) {
				toast(err instanceof Error ? err.message : 'Failed to load summary', 'error');
			}
		}
	}

	async function loadQueue() {
		const token = queueSequence.start();
		queueLoading = true;
		// A fresh load re-bases the queue: any "select all N matching" set
		// resolved against the previous contents no longer describes it (an
		// execute/void via onRunChanged drops rows). Drop matching mode so the
		// prune effect resumes narrowing the selection to what's on screen.
		selectedAllQueueMatching = false;
		try {
			const data = await api.get<QueueResponse>(
				`/api/payments/queue?page=1&page_size=${QUEUE_PAGE_SIZE}`
			);
			if (!queueSequence.canCommit(token)) return; // superseded by a newer load
			queue = data.items;
			queuePage = 1;
			queueTotal = data.total;
			queueByCurrency = data.by_currency ?? [];
			queueBlockedTotal = data.blocked_total ?? 0;
			queueSelectableTotal = data.selectable_total ?? 0;
			queueUnconvertedCount = data.unconverted_count ?? 0;
			queueErrored = false;
		} catch (err) {
			if (queueSequence.isCurrentRequest(token)) {
				queueErrored = true;
				toast(err instanceof Error ? err.message : 'Failed to load payment queue', 'error');
			}
		} finally {
			// `isCurrentRequest`, not `canCommit`: a local edit that supersedes
			// this request must still clear the spinner, or the table sits on
			// "Loading…" forever with no request left to clear it. Clear BOTH
			// flags — loadQueue and loadMoreQueue share `queueSequence`, so a
			// Load-More that superseded this reload owns clearing whichever
			// spinner is showing (mirrors `loadCards`).
			if (queueSequence.isCurrentRequest(token)) {
				queueLoading = false;
				queueLoadingMore = false;
			}
		}
	}

	// Append the next page. `appendUnique` (never a raw spread) drops a row that
	// re-surfaces when the underlying set shifts between fetches — a duplicated
	// id crashes the keyed `{#each}`. Shares `queueSequence` with `loadQueue`,
	// so a full reload landing mid-append retires this response.
	async function loadMoreQueue() {
		if (queueLoadingMore || !hasMoreQueue) return;
		const token = queueSequence.start();
		queueLoadingMore = true;
		try {
			const next = queuePage + 1;
			const data = await api.get<QueueResponse>(
				`/api/payments/queue?page=${next}&page_size=${QUEUE_PAGE_SIZE}`
			);
			if (!queueSequence.canCommit(token)) return;
			queue = appendUnique(queue, data.items);
			queuePage = next;
			queueTotal = data.total;
			queueByCurrency = data.by_currency ?? queueByCurrency;
			queueBlockedTotal = data.blocked_total ?? queueBlockedTotal;
			queueSelectableTotal = data.selectable_total ?? queueSelectableTotal;
		} catch (err) {
			if (queueSequence.isCurrentRequest(token)) {
				toast(err instanceof Error ? err.message : 'Failed to load more of the payment queue', 'error');
			}
		} finally {
			if (queueSequence.isCurrentRequest(token)) {
				queueLoading = false;
				queueLoadingMore = false;
			}
		}
	}

	// --- Virtual cards tab ---
	interface CardItem {
		id: string;
		invoice_id: string;
		card_provider: string;
		last_four: string | null;
		amount_limit: number;
		amount_charged: number | null;
		currency: string;
		status: string;
		expires_at: string | null;
		created_at: string;
		vendor_name: string | null;
		invoice_number: string | null;
	}

	/**
	 * Badge tone per virtual-card status (`card_adapters/base.CardStatus`).
	 *
	 * Six of the eight had no CSS rule and rendered untinted — only
	 * `completed` and `cancelled` happened to collide with a payment-status
	 * class name. Same bare-string caveat as the runs map above.
	 *
	 * `sent` / `active` share `accent` and `charged` / `completed` share
	 * `success`: neither pair was visually distinguished before, so this
	 * collapses nothing that existed.
	 */
	const CARD_STATUS_TONES: Record<string, BadgeTone> = {
		created: 'neutral',
		sent: 'accent',
		active: 'accent',
		charged: 'success',
		completed: 'success',
		expired: 'muted',
		cancelled: 'muted',
		declined: 'danger'
	};

	function cardTone(status: string): BadgeTone {
		return CARD_STATUS_TONES[status] ?? 'neutral';
	}

	const CARDS_PAGE_SIZE = 20;
	let cards = $state<CardItem[]>([]);
	let cardsTotal = $state(0);
	let cardsPage = $state(1);
	let loadingCards = $state(false);
	let loadingMoreCards = $state(false);
	// Three states, not two — the rule the Queue, History, Runs and Rebates
	// lists on this same page already follow. "No virtual cards issued yet." is
	// a claim about whether this tenant has ever issued a card; rendered over a
	// failed read it answers "we could not look" with "there is nothing", and
	// the toast that reported the failure has already faded. The Cards table
	// was the last list here with no error flag at all.
	let cardsErrored = $state(false);
	let hasMoreCards = $derived(cards.length < cardsTotal);

	interface RebateStatusBreakdown {
		pending_total: number;
		confirmed_total: number;
		paid_out_total: number;
	}
	interface CardDashboard {
		active_cards: number;
		active_cards_value: number;
		spend_this_month: number;
		// REALIZED (confirmed + paid_out) only — see `rebates_*_by_status` for
		// the full breakdown, incl. still-`pending` (unconfirmed) money.
		rebates_this_month: number;
		rebates_ytd: number;
		projected_annual_rebates: number;
		rebates_this_month_by_status: RebateStatusBreakdown;
		rebates_ytd_by_status: RebateStatusBreakdown;
		// The single currency every figure above is denominated in — the org's
		// REPORTING currency, resolved server-side. Read it off the response
		// rather than falling back to the `orgCurrency` store: the store
		// resolves the same order client-side, but the response is the one that
		// actually denominates these numbers.
		currency: string;
	}
	let cardDashboard = $state<CardDashboard | null>(null);

	async function loadCards(opts: { append?: boolean; nextPage?: number } = {}) {
		const nextPage = opts.nextPage ?? 1;
		// Fetch and Load-More share one counter (latest-issued wins), exactly as
		// the payments store documents: a Load-More that resolves after a fresh
		// reload must not append a stale page onto the new list.
		const token = cardsSequence.start();
		if (opts.append) loadingMoreCards = true;
		else loadingCards = true;
		try {
			const listReq = api.get<{ items: CardItem[]; total: number }>(
				`/api/cards?page=${nextPage}&page_size=${CARDS_PAGE_SIZE}`
			);
			// The dashboard is page-independent — only (re)fetch it on a fresh load.
			const dashReq = opts.append
				? Promise.resolve(cardDashboard)
				: api.get<CardDashboard>('/api/cards/dashboard').catch(() => null);
			const [list, dash] = await Promise.all([listReq, dashReq]);
			if (!cardsSequence.canCommit(token)) return; // superseded by a newer load
			cards = opts.append ? appendUnique(cards, list.items) : list.items;
			cardsTotal = list.total;
			cardsPage = nextPage;
			cardDashboard = dash;
			cardsErrored = false;
		} catch (err) {
			// Mirrors `loadRebates` (the sibling list on this same tab): flag the
			// failure, clear the rows on a fresh load so a stale page can't be
			// read as current, and let the table render the errored copy.
			if (cardsSequence.isCurrentRequest(token)) {
				cardsErrored = true;
				if (!opts.append) cards = [];
				toast(err instanceof Error ? err.message : m('payments.cards.errored'), 'error');
			}
		} finally {
			// NOT canCommit — reading it here would leave the spinner stuck on
			// forever once a newer request superseded this one.
			if (cardsSequence.isCurrentRequest(token)) {
				loadingCards = false;
				loadingMoreCards = false;
			}
		}
	}

	async function loadMoreCards() {
		await loadCards({ append: true, nextPage: cardsPage + 1 });
	}

	// The AP-side reveal renders exactly what `CardDetailsResponse` declares
	// (`backend/app/schemas/virtual_card.py`): `{card_number, exp_month, exp_year,
	// cvv}`. FastAPI strips anything the response_model doesn't declare, so the
	// supplier-portal shape (`{pan, expires_at, last_four}`) that used to be typed
	// here arrived `undefined` — a blank card number and no expiry. Don't widen
	// the backend schema to suit the client; `backend/app/api/portal.py` records
	// that reading `details.pan` was a prior break.
	let revealedCard = $state<{ cardNumber: string; cvv: string; expires: string } | null>(null);

	/** `exp_month` / `exp_year` → the `MM/YYYY` printed on the card. Kept dumb on
	 *  purpose: these are card-face digits, not a date to localize. */
	function formatCardExpiry(month: number, year: number): string {
		if (!Number.isFinite(month) || !Number.isFinite(year)) return '—';
		return `${String(month).padStart(2, '0')}/${year}`;
	}

	async function revealCard(cardId: string) {
		try {
			const resp = await api.get<{
				card_number: string;
				exp_month: number;
				exp_year: number;
				cvv: string;
			}>(`/api/cards/${cardId}/details`);
			revealedCard = {
				cardNumber: resp.card_number,
				cvv: resp.cvv,
				expires: formatCardExpiry(resp.exp_month, resp.exp_year)
			};
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Card not viewable', 'error');
		}
	}

	// --- Card-rebate lifecycle (`pending` → `confirmed` → `paid_out`) --------
	// The Cards tab rendered a muted "+{amount} pending confirmation" hint for a
	// status the product gave no way to advance: `GET /api/cards/rebates` and
	// its two transitions shipped API-only, so the pending bucket could only
	// ever grow and the realized headline beside it could never catch up.
	//
	// A transition here is a HUMAN-DRIVEN RECORD of something the processor did
	// out-of-band — Lithic/Nium confirm and pay rebates on a periodic statement,
	// not on a webhook we ingest. Neither call requests a payout and neither
	// moves money; the dialog copy says so, because a "Mark paid out" button is
	// otherwise very easy to read as one that pays someone.
	//
	// Read and write share ONE role gate server-side (admin/ap_manager/cfo), so
	// there is no role that can see this table without being able to act on it;
	// the check is still made, because `/payments` is reachable by a custom role
	// holding only `payment.execute` / `payment.void` (see `lib/nav.ts`).
	//
	// The list is PAGED (`page` / `page_size`, like every other list here) — it
	// grows by one row per settled card, forever. Two totals, named apart on the
	// wire: `total` is the row COUNT the Load-more control counts against, and
	// `total_amount` is the summed money over the WHOLE filtered set, never over
	// the loaded page. Each ROW carries its own `currency`, resolved server-side
	// from the card that earned it, so a mixed-currency programme renders every
	// amount under its own code instead of bare.
	const REBATES_PAGE_SIZE = 20;
	let rebates = $state<CardRebate[]>([]);
	let rebateTotalAmount = $state<RebateListResponse['total_amount']>(null);
	let rebateCount = $state(0);
	let rebatePage = $state(1);
	// `null` until a list has loaded — what `rebateTotalAmount` is denominated in.
	let rebateListCurrency = $state<string | null>(null);
	let rebateExcludedCount = $state(0);
	let loadingRebates = $state(false);
	let loadingMoreRebates = $state(false);
	let rebatesErrored = $state(false);
	let hasMoreRebates = $derived(rebates.length < rebateCount);

	// `RebateResponse.status` is a bare string, so the label is looked up rather
	// than interpolated into a message key: an unrecognised status renders
	// verbatim instead of the raw key (or a blank cell), the same way
	// `rebateTone` falls back to a flat pill.
	const REBATE_STATUS_LABEL_KEYS = {
		pending: 'payments.rebates.status.pending',
		confirmed: 'payments.rebates.status.confirmed',
		paid_out: 'payments.rebates.status.paidOut'
	} as const;

	function rebateStatusLabel(status: string): string {
		const key = REBATE_STATUS_LABEL_KEYS[status as keyof typeof REBATE_STATUS_LABEL_KEYS];
		return key ? m(key) : status;
	}

	function canManageRebates(): boolean {
		return !!auth.user && auth.hasAnyRole('admin', 'ap_manager', 'cfo');
	}

	// --- Corridor quote comparison (advisory) --------------------------------
	// `POST /api/payments/corridor-quotes` prices ONE payable invoice across
	// every processor the org has configured and ranks them. It had no caller in
	// the app at all, so the optimizer was a module nothing reached.
	//
	// It is ADVISORY: it books no Payment, claims no run, touches no invoice and
	// does not decide which rail pays — `payment_corridor.pick_corridor` + the
	// org's configured provider still do that at execute time. Nothing in this
	// UI may imply that opening or comparing a quote selected a route, which is
	// why the dialog leads with the standing advisory notice rather than
	// closing with a disclaimer.
	//
	// Role gate mirrors the endpoint's own `require_roles(admin, ap_manager,
	// cfo)`, so a clerk never sees a control that can only 403.
	let quoteTarget = $state<QueueItem | null>(null);
	let quoteMode = $state<QuoteMode>('cheapest');
	let quoteMethod = $state('');
	let quoteBusy = $state(false);
	let quoteError = $state<string | null>(null);
	let quoteResult = $state<CorridorQuoteComparison | null>(null);

	/** Rails worth pricing. `''` = let the server default (`ach`). Kept to the
	 *  corridor-relevant set — the point of the comparison is cross-border
	 *  routing, where the fee spread between processors is real. */
	const QUOTE_METHODS = [
		{ value: '', key: 'payments.quotes.method.default' },
		{ value: 'ach', key: 'payments.quotes.method.ach' },
		{ value: 'wire', key: 'payments.quotes.method.wire' },
		{ value: 'international_wire', key: 'payments.quotes.method.internationalWire' },
		{ value: 'sepa', key: 'payments.quotes.method.sepa' },
		{ value: 'international_ach', key: 'payments.quotes.method.internationalAch' },
		{ value: 'check', key: 'payments.quotes.method.check' }
	] as const;

	function canCompareRoutes(): boolean {
		return !!auth.user && auth.hasAnyRole('admin', 'ap_manager', 'cfo');
	}

	function openQuotes(item: QueueItem) {
		quoteTarget = item;
		quoteMode = 'cheapest';
		quoteMethod = '';
		quoteError = null;
		quoteResult = null;
		void runQuotes();
	}

	function closeQuotes() {
		quoteTarget = null;
		quoteError = null;
		quoteResult = null;
		quoteBusy = false;
	}

	async function runQuotes() {
		if (!quoteTarget) return;
		const invoiceId = quoteTarget.id;
		quoteBusy = true;
		quoteError = null;
		try {
			const cmp = await compareCorridorQuotes({
				invoiceId,
				method: quoteMethod || undefined,
				mode: quoteMode
			});
			// Guard against a slow response landing after the dialog moved on.
			if (quoteTarget?.id !== invoiceId) return;
			quoteResult = cmp;
		} catch (err) {
			if (quoteTarget?.id !== invoiceId) return;
			// Persistent, not a toast: the 409 body names each provider's own
			// machine reason ("no provider can quote method=sepa for …"), which is
			// the actionable half of the refusal.
			quoteResult = null;
			quoteError = err instanceof Error ? err.message : m('payments.quotes.failed');
		} finally {
			if (quoteTarget?.id === invoiceId) quoteBusy = false;
		}
	}

	function setQuoteMode(mode: QuoteMode) {
		if (quoteMode === mode) return;
		quoteMode = mode;
		void runQuotes();
	}

	/** The reason a processor is not in the ranking, in the user's language when
	 *  we have copy for the machine code, and VERBATIM otherwise — an adapter's
	 *  own sentence ("method 'sepa' not supported by column") says more than any
	 *  bucket we could invent for it. */
	function quoteReasonText(q: CorridorQuote): string {
		const key = quoteReasonKey(q.unavailable_reason);
		if (key) return m(key as MessageKey);
		return q.unavailable_reason ?? m('payments.quotes.reason.unknown');
	}

	async function loadRebates(opts: { append?: boolean; nextPage?: number } = {}) {
		const nextPage = opts.nextPage ?? 1;
		// Fetch and Load-more share one counter (latest-issued wins), exactly as
		// the cards list beside them does: a Load-more resolving after a fresh
		// reload must not append a stale page onto the new list.
		const token = rebatesSequence.start();
		if (opts.append) loadingMoreRebates = true;
		else loadingRebates = true;
		try {
			const list = await listCardRebates({ page: nextPage, pageSize: REBATES_PAGE_SIZE });
			if (!rebatesSequence.canCommit(token)) return; // superseded
			rebates = opts.append ? appendUnique(rebates, list.items) : list.items;
			rebateCount = list.total;
			rebatePage = nextPage;
			// Both figures below describe the WHOLE filtered set, not the rows
			// just loaded — they are read off the envelope rather than reduced
			// over `rebates`, which is the whole point of `decisions.md` §79/§82.
			rebateTotalAmount = list.total_amount;
			rebateListCurrency = list.currency;
			rebateExcludedCount = list.excluded_rebate_count ?? 0;
			rebatesErrored = false;
		} catch (err) {
			// Three distinct states, never one: "no rebates yet" is a claim about
			// the card programme's earnings and must not be rendered over a
			// failed read (mirrors `/exceptions`, `frontend/CLAUDE.md`).
			if (rebatesSequence.isCurrentRequest(token)) {
				rebatesErrored = true;
				if (!opts.append) rebates = [];
				toast(
					err instanceof Error ? err.message : m('payments.rebates.errored'),
					'error'
				);
			}
		} finally {
			// NOT canCommit — reading it here would leave the spinner stuck on
			// once a newer request superseded this one.
			if (rebatesSequence.isCurrentRequest(token)) {
				loadingRebates = false;
				loadingMoreRebates = false;
			}
		}
	}

	async function loadMoreRebates() {
		await loadRebates({ append: true, nextPage: rebatePage + 1 });
	}

	let rebateTarget = $state<CardRebate | null>(null);
	let rebateMode = $state<RebateTransition>('confirm');
	let rebateBusy = $state(false);
	let rebateError = $state<string | null>(null);

	function openRebate(r: CardRebate, mode: RebateTransition) {
		rebateTarget = r;
		rebateMode = mode;
		rebateError = null;
	}

	function closeRebate() {
		rebateTarget = null;
		rebateError = null;
	}

	async function commitRebate() {
		if (!rebateTarget) return;
		const id = rebateTarget.id;
		const mode = rebateMode;
		rebateBusy = true;
		rebateError = null;
		try {
			if (mode === 'confirm') await confirmCardRebate(id);
			else await markCardRebatePaid(id);
			toast(
				mode === 'confirm'
					? m('payments.rebates.confirmed')
					: m('payments.rebates.markedPaid'),
				'success'
			);
			closeRebate();
			// The dashboard's realized headline is `confirmed + paid_out`, so it
			// moves on a confirm — reload both rather than patching the row.
			// The rebate list re-bases to page 1: the row's status changed, so
			// the whole-set `total_amount` and the ordering the loaded pages were
			// cut from have both moved, and a re-read is what proves the write
			// landed (the same posture `loadQueue` takes after an execute).
			await loadCards();
			await loadRebates();
		} catch (err) {
			// Persistent, not a toast that fades: a 409 ("Cannot confirm a rebate
			// in 'confirmed' status") is what an operator whose row went stale
			// under a concurrent update needs to read while the dialog is open.
			rebateError = err instanceof Error ? err.message : m('payments.rebates.failed');
		} finally {
			rebateBusy = false;
		}
	}

	async function loadRuns() {
		const token = runsSequence.start();
		runsLoading = true;
		try {
			const data = await api.get<{ items: RunItem[] }>('/api/payments/runs/?page_size=100');
			if (!runsSequence.canCommit(token)) return; // superseded by a newer load
			runs = data.items;
			runsErrored = false;
		} catch (err) {
			if (runsSequence.isCurrentRequest(token)) {
				runsErrored = true;
				toast(err instanceof Error ? err.message : 'Failed to load payment runs', 'error');
			}
		} finally {
			// `isCurrentRequest`, not `canCommit`: a request superseded by a local
			// edit must still clear the spinner, or the tab sits on "Loading…"
			// with no request left to clear it (mirrors `loadQueue`).
			if (runsSequence.isCurrentRequest(token)) runsLoading = false;
		}
	}

	// Per-status tallies over the WHOLE payment set, from GET /api/payments/counts
	// — so the History chip counts (and "All") reflect every page, not just the
	// loaded one. Falls back to the loaded-page tally if the fetch fails.
	let paymentCounts = $state<Record<string, number>>({});
	let paymentCountsTotal = $state(0);
	// Own sequencer, same reason as the invoice store's: this fires alongside
	// every list fetch, so without one a slow response for an older filter can
	// land after a faster newer one and leave the chips lying.
	const paymentCountsSequence = createRequestSequencer();

	async function fetchPaymentCounts() {
		const token = paymentCountsSequence.start();
		try {
			// Carries the list's own params so the chips describe the same rows
			// the table shows — a search for one vendor used to leave them
			// reading the tenant's whole total over a one-row table.
			// `status`/`sort`/`order` ride along and are ignored by the endpoint,
			// which must NOT filter by status: that's the dimension it tallies.
			const qs = new URLSearchParams(buildParams()).toString();
			const query = qs ? `?${qs}` : '';
			const data = await api.get<{ total: number; by_status: Record<string, number> }>(
				`/api/payments/counts${query}`
			);
			if (!paymentCountsSequence.canCommit(token)) return;
			paymentCounts = data.by_status ?? {};
			paymentCountsTotal = data.total ?? 0;
		} catch {
			if (!paymentCountsSequence.isCurrentRequest(token)) return;
			paymentCounts = {};
			paymentCountsTotal = 0;
		}
	}

	function statusCount(s: PaymentStatus): number {
		return paymentCounts[s] ?? paymentStore.all.filter((p) => p.status === s).length;
	}

	/** Render money under the currency the SERVER stated, honestly.
	 *
	 *  Every `/payments` money cell goes through this, with the code its own
	 *  payload names: the row's for a payment, a queue row or a run; the
	 *  response's for the summary, the card dashboard, a rebate list or a
	 *  corridor quote.
	 *
	 *  It used to fall back to `orgCurrency` for a missing code, and a second
	 *  helper (`formatRowMoney`) existed only to opt the per-row cells out of
	 *  that. `PaymentResponse.currency` / `PaymentRunResponse.currency` are
	 *  `null` exactly when the backend refused to guess — a run whose legs
	 *  disagree, a row with nothing recorded — and borrowing the org's code
	 *  there reinstated the fabrication the field exists to end
	 *  (`docs/decisions.md` §79/§82). `formatMoney` now renders an unprovable
	 *  code BARE itself (§198), so there is one path and no fallback to forget.
	 *  Nothing here adds, compares or converts — the value passes through
	 *  untouched.
	 */
	function formatCurrency(
		amount: number | string | null | undefined,
		currency: string | null | undefined
	): string {
		return formatMoney(amount, { currency });
	}


	// The rail name is a message key, not an English literal — a rail this
	// build doesn't know renders raw rather than blank.
	function methodLabel(method: string | null): string {
		if (!method) return '—';
		const key = paymentMethodLabelKey(method);
		return key ? m(key) : method;
	}

	// The status name is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = (s: string) => {
		const key = paymentStatusLabelKey(s);
		return key ? m(key) : s;
	};

	// Same rule for the RUN status in the Runs table. It had no label map at
	// all, so the pill printed the raw enum (`executing`, `partial`) in a table
	// whose every other cell is translated — and the RunDetailModal one click
	// away did the same.
	const runLabel = (s: string) => {
		const key = runStatusLabelKey(s);
		return key ? m(key) : s;
	};

	let historyChips = $derived([
		{ key: 'all', label: m('common.all'), count: paymentCountsTotal || paymentStore.all.length },
		...PAYMENT_STATUSES.map((s) => ({
			key: s,
			label: statusLabel(s),
			count: statusCount(s)
		}))
	]);
</script>

<PageHeader title={m('payments.title')}>
	{#if summary}
		<div class="summary-cards">
			<div class="scard">
				<span class="scard-value">
					{formatCurrency(summary.total_paid, summary.currency)}
				</span>
				<span class="scard-label">{m('payments.summary.totalPaid')}</span>
			</div>
			<div class="scard">
				<span class="scard-value">
					{formatCurrency(summary.total_pending, summary.currency)}
				</span>
				<span class="scard-label">{m('payments.summary.pending')}</span>
			</div>
			<div class="scard">
				<span class="scard-value">{summary.queue_count}</span>
				<span class="scard-label">{m('payments.summary.readyToPay')}</span>
			</div>
			<div class="scard">
				<span class="scard-value">{summary.payment_count}</span>
				<span class="scard-label">{m('payments.summary.payments')}</span>
			</div>
			{#if Number(summary.total_rebates) > 0}
				<div class="scard rebate">
					<!-- `summary.currency` is what the API says EVERY money figure in
					     this block is denominated in, `total_rebates` included — the
					     three cards above pass it and this one didn't, falling through
					     to the store instead of the response's own declaration. -->
					<span class="scard-value">{formatCurrency(summary.total_rebates, summary.currency)}</span>
					<span class="scard-label">{m('payments.summary.rebatesEarned')}</span>
				</div>
			{/if}
		</div>
		<!-- A payment with no rate into the reporting currency is left out of the
		     totals above, so the headline understates what actually moved. Same
		     notice pattern as /discounts' excluded foreign offers and the /cfo
		     cash-position card's unconverted outflows — all three read alike on
		     purpose. -->
		{#if (summary.unconverted_payment_count ?? 0) > 0}
			<p class="fx-skipped" role="alert" data-testid="unconverted-payments">
				{m('payments.summary.unconvertedPayments', {
					n: summary.unconverted_payment_count ?? 0,
					currency: summary.currency ?? ''
				})}
			</p>
		{/if}
		<!-- Its own notice, not folded into the one above: a rebate excluded for
		     being in another currency is a different fact from a payment whose
		     reporting figure could not be established, and the rebate KPI is a
		     different figure from the two money totals. Without this the rebate
		     card went from wrong-but-complete to right-but-silently-partial. -->
		{#if (summary.excluded_rebate_count ?? 0) > 0}
			<p class="fx-skipped" role="alert" data-testid="excluded-rebates">
				{m('payments.summary.excludedRebates', {
					n: summary.excluded_rebate_count ?? 0,
					currency: summary.currency ?? ''
				})}
			</p>
		{/if}
	{/if}

	<nav class="tabs">
		<button class="tab" class:active={activeTab === 'queue'} onclick={() => selectTab('queue')}>
			{m('payments.tab.queue')} {#if summary}<span class="tab-count">{summary.queue_count}</span>{/if}
		</button>
		<button class="tab" class:active={activeTab === 'history'} onclick={() => selectTab('history')}>
			{m('payments.tab.history')}
		</button>
		<button class="tab" class:active={activeTab === 'cards'} onclick={() => selectTab('cards')}>
			{m('payments.tab.cards')}
		</button>
		<button class="tab" class:active={activeTab === 'runs'} onclick={() => selectTab('runs')}>
			{m('payments.tab.runs')}
		</button>
	</nav>

	{#if activeTab === 'history'}
		<div class="filter-row">
			<SearchBox
				bind:value={search}
				placeholder={m('payments.search.placeholder')}
				ariaLabel={m('payments.search.aria')}
			/>
			<FilterChips chips={historyChips} bind:active={activeStatus} />
		</div>
	{/if}

	{#if activeTab === 'queue'}
		<!-- Rows the backend could not express in the reporting currency are
		     excluded from its own queue roll-ups. The rows themselves are still
		     listed and selectable — each renders in its own currency — so this
		     says the TOTALS are short, not that anything is missing. -->
		{#if queueUnconvertedCount > 0}
			<p class="fx-skipped" role="alert" data-testid="unconverted-queue">
				{m('payments.queue.unconvertedRows', { n: queueUnconvertedCount })}
			</p>
		{/if}
		{#if selectedQueue.size > 0}
			<div class="pay-bar">
				{#if mixedCurrencySelection}
					<!-- Inside the fixed bar, above the controls: the bar floats, so
					     a sibling in normal flow would render nowhere near it. -->
					<p class="pay-bar-warn" role="alert" data-testid="mixed-currency-warning">
						{m('payments.queue.mixedCurrency', { n: selectedGroups.length })}
					</p>
				{/if}
				<div class="pay-bar-row">
					<span class="pay-bar-count" data-testid="pay-bar-count">
						<!-- `formatGroups` renders ONE subtotal per currency, side by
						     side — it never adds EUR to USD into a single figure. -->
						{m('payments.queue.selected', { n: selectedQueue.size, total: formatGroups(selectedGroups) })}
						{#if selectedSavingsGroups.length > 0}
							<span class="pay-bar-savings">{m('payments.queue.save', { amount: formatGroups(selectedSavingsGroups) })}</span>
						{/if}
					</span>
					{#if allQueueSelected && !selectedAllQueueMatching && queueSelectableTotal > selectableQueue.length}
						<button
							class="btn-select-all-matching"
							data-testid="queue-select-all-matching"
							disabled={selectingAllQueue}
							onclick={selectAllQueueMatching}
						>
							{selectingAllQueue
								? m('common.loading')
								: m('common.selectAllMatching', { total: queueSelectableTotal })}
						</button>
					{:else if selectedAllQueueMatching}
						<span class="pay-bar-all-matching" data-testid="queue-all-matching-note"
							>{m('common.allMatchingSelected')}</span
						>
					{/if}
					{#if !showReview}
						<button
							class="btn-pay"
							disabled={mixedCurrencySelection}
							title={mixedCurrencySelection ? m('payments.queue.mixedCurrencyBlocked') : ''}
							onclick={() => (showReview = true)}
						>
							{m('payments.queue.reviewAndPay')}
						</button>
					{/if}
					<button class="btn-clear" onclick={clearQueueSelection}>{m('common.clear')}</button>
				</div>
			</div>
		{/if}

		{#if showReview && selectedQueue.size > 0}
			<div class="review-panel">
				<div class="review-title">{m('payments.queue.reviewTitle')}</div>
				<table class="review-table">
					<thead>
						<tr>
							<th>{m('payments.col.invoice')}</th>
							<th>{m('payments.col.vendor')}</th>
							<th class="right">{m('payments.col.amount')}</th>
							<th>{m('payments.col.method')}</th>
						</tr>
					</thead>
					<tbody>
						{#each queue.filter(q => selectedQueue.has(q.id)) as item (item.id)}
							<tr>
								<td class="mono">{item.invoice_number}</td>
								<td>{item.vendor_name}</td>
								<!-- The queue row above this panel already renders `item.currency`;
								     dropping it here made the review step — the last screen before a
								     run is staged — the one place the figure lost its code. -->
								<td class="right mono">{formatCurrency(item.amount, item.currency)}</td>
								<td>
									<!-- A rail-pinned row (a live virtual card already claims the
									     invoice) offers only that rail: every other one is a 409
									     from the run builder, so a select that can only produce a
									     refusal is worse than no choice. The reason renders
									     beside it. -->
									{#if requiredMethod(item)}
										<span class="pinned-method" data-testid="queue-pinned-method">
											{m(paymentMethodLabelKey(requiredMethod(item)!) ?? 'payments.quotes.method.default')}
										</span>
										<span class="pinned-method-why">{blockedReason(item)}</span>
									{:else}
										<select class="method-select" aria-label={`Payment method for ${item.invoice_number}`} value={paymentMethods[item.id] || 'ach'} onchange={(e) => (paymentMethods[item.id] = e.currentTarget.value)}>
											<option value="ach">ACH</option>
											<option value="wire">Wire</option>
											<option value="check">Check</option>
											<option value="virtual_card">Virtual Card</option>
										</select>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
				{#if selectedAllQueueMatching && selectedQueue.size > queue.filter((q) => selectedQueue.has(q.id)).length}
					<!-- "Select all N matching" reaches past the loaded page, so the
					     table above lists only the rows on screen. The run still
					     covers all {selectedQueue.size}; the footer total is the
					     backend's figure for the whole set. -->
					<p class="review-partial" data-testid="review-partial-note">
						{m('payments.queue.reviewPartial', {
							shown: queue.filter((q) => selectedQueue.has(q.id)).length,
							n: selectedQueue.size
						})}
					</p>
				{/if}
				{#if createRunError}
					<!-- The server's own refusal, kept on screen. On the
					     financial-integrity 409 this NAMES the invoice numbers
					     that blocked the run, which is the only thing that makes
					     a 20-row selection actionable. -->
					<p class="review-error" role="alert" data-testid="create-run-error">
						{createRunError}
					</p>
				{/if}
				<div class="review-footer">
					<span class="review-total">{m('payments.queue.total', { amount: formatGroups(selectedGroups) })}</span>
					<button
						class="btn-execute"
						disabled={creatingRun || mixedCurrencySelection}
						title={mixedCurrencySelection ? m('payments.queue.mixedCurrencyBlocked') : ''}
						onclick={createDraftRun}
					>
						{creatingRun
							? m('payments.queue.creatingDraft')
							: m('payments.queue.createDraftRun', { n: selectedQueue.size })}
					</button>
				</div>
			</div>
		{/if}

		{#if queueBlockedTotal > 0}
			<p class="blocked-banner" role="status" data-testid="queue-blocked-banner">
				{m('payments.queue.blockedCount', { n: queueBlockedTotal })}
			</p>
		{/if}

		{#if queueSavingsGroups.length > 0}
			<div class="savings-banner">
				<span class="savings-icon">💸</span>
				<span>
					{m('payments.queue.savingsBanner', { amount: formatGroups(queueSavingsGroups) })}
				</span>
			</div>
		{/if}

		<DataTable
			isEmpty={queue.length === 0}
			empty={queueLoading
				? m('common.loading')
				: queueErrored
					? m('payments.queue.empty.errored')
					: m('payments.queue.empty')}
			colspan={canCompareRoutes() ? 9 : 8}
		>
			{#snippet header()}
				<tr>
					<th class="checkbox-col"><input type="checkbox" aria-label={m('payments.selectAllPayableAria')} checked={allQueueSelected} disabled={selectableQueue.length === 0} onchange={toggleQueueSelectAll} /></th>
					<th>{m('payments.col.invoiceNumber')}</th>
					<th>{m('payments.col.vendor')}</th>
					<th class="right">{m('payments.col.amount')}</th>
					<th>{m('payments.col.dueDate')}</th>
					<th>{m('payments.col.discount')}</th>
					<th>{m('payments.col.terms')}</th>
					<th>{m('payments.col.status')}</th>
					{#if canCompareRoutes()}
						<th class="right">{m('payments.col.routes')}</th>
					{/if}
				</tr>
			{/snippet}
			{#snippet body()}
				{#each queue as item (item.id)}
					<tr
						class:overdue={item.is_overdue}
						class:row-selected={selectedQueue.has(item.id)}
						class:discount-eligible={item.discount_eligible}
						class:row-blocked={isBlocked(item)}
						class:row-muted={isBlocked(item)}
					>
						<td class="checkbox-col">
							{#if isBlocked(item)}
								<!-- Disabled, not omitted: the column keeps its shape, and
								     the accessible name carries the REASON so a screen
								     reader learns why this row can't be paid. -->
								<input
									type="checkbox"
									disabled
									checked={false}
									data-testid="queue-blocked-checkbox"
									aria-label={m('payments.queue.blockedCheckboxAria', {
										invoice: item.invoice_number,
										reason: blockedReason(item)
									})}
								/>
							{:else}
								<input type="checkbox" aria-label={`Select invoice ${item.invoice_number}`} checked={selectedQueue.has(item.id)} onchange={() => toggleQueueSelect(item)} />
							{/if}
						</td>
						<td class="mono">{item.invoice_number}</td>
						<td>{item.vendor_name}</td>
						<td class="right mono">{formatCurrency(item.amount, item.currency)}</td>
						<td class:overdue-text={item.is_overdue}>
							<!-- The 6px that used to be the pill's own margin lives on this
							     row: `<Badge>` owns colour and metrics, the caller owns
							     placement. -->
							<span class="due-cell">
								{formatDate(item.due_date)}
								{#if item.is_overdue}
									<Badge tone="danger" variant="overdue-badge">{m('payments.overdue')}</Badge>
								{/if}
							</span>
						</td>
						<td>
							{#if item.discount_eligible && item.discount_amount && item.discount_percent}
								<span
									class="discount-chip"
									title="{item.discount_percent}% discount expires {formatDate(item.discount_date)}"
								>
									{m('payments.queue.discountSave', { amount: formatCurrency(item.discount_amount, item.currency) })}
									<span class="discount-pct">{m('payments.queue.discountBy', { percent: item.discount_percent, date: formatDate(item.discount_date) })}</span>
								</span>
							{:else}
								<span class="muted">—</span>
							{/if}
						</td>
						<td class="muted">{item.payment_terms ?? '—'}</td>
						<td>
							<StatusBadge status={item.status as import('$lib/types/invoice').InvoiceStatus} />
							{#if isBlocked(item)}
								<span class="blocked-chip" data-testid="queue-blocked-chip">
									{blockedReason(item)}
								</span>
							{:else if requiredMethod(item)}
								<!-- Not blocked — payable, but only on one rail. Its own chip,
								     because reusing the blocked one would read as "can't be
								     paid" for a row whose checkbox works. -->
								<span class="pinned-chip" data-testid="queue-pinned-chip">
									{blockedReason(item)}
								</span>
							{/if}
						</td>
						{#if canCompareRoutes()}
							<!-- Advisory: opening this prices the invoice across the
							     org's configured processors. It selects nothing and
							     moves nothing — offered on a BLOCKED row too, because
							     seeing what a payment would cost is not paying it. -->
							<td class="right">
								<RowAction
									ariaLabel={m('payments.quotes.openAria', {
										invoice: item.invoice_number
									})}
									onclick={() => openQuotes(item)}
								>
									{m('payments.quotes.open')}
								</RowAction>
							</td>
						{/if}
					</tr>
				{/each}
			{/snippet}
		</DataTable>

		{#if hasMoreQueue}
			<div class="load-more-row">
				<button class="btn-load-more" onclick={loadMoreQueue} disabled={queueLoadingMore}>
					{queueLoadingMore
						? m('common.loading')
						: m('payments.queue.loadMore', { shown: queue.length, total: queueTotal })}
				</button>
			</div>
		{:else if queueTotal > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('payments.queue.showingAll', { total: queueTotal })}</span>
			</div>
		{/if}

	{:else if activeTab === 'history'}
		<DataTable
			isEmpty={paymentStore.all.length === 0}
			empty={historyEmptyMessage}
			colspan={8}
		>
			{#snippet header()}
				<tr>
					<th scope="col">{m('payments.col.invoiceNumber')}</th>
					<th scope="col">{m('payments.col.vendor')}</th>
					<SortableHeader field="method" label={m('payments.col.method')} active={sortField === 'method'} order={sortOrder} onsort={handleHistorySort} />
					<SortableHeader field="amount" label={m('payments.col.amount')} class="right" active={sortField === 'amount'} order={sortOrder} onsort={handleHistorySort} />
					<SortableHeader field="status" label={m('payments.col.status')} active={sortField === 'status'} order={sortOrder} onsort={handleHistorySort} />
					<th scope="col">{m('payments.col.reference')}</th>
					<SortableHeader field="created_at" label={m('payments.col.date')} active={sortField === 'created_at'} order={sortOrder} onsort={handleHistorySort} />
					<th class="actions-col"></th>
				</tr>
			{/snippet}
			{#snippet body()}
				{#each paymentStore.all as p (p.id)}
					<tr>
						<td class="mono">{p.invoice_number ?? '—'}</td>
						<td>{p.vendor_name ?? '—'}</td>
						<td>
							<Badge
								tone={p.method === 'virtual_card' ? 'accent' : 'neutral'}
								variant="method-badge"
							>
								{methodLabel(p.method)}
								{#if p.method === 'virtual_card' && p.card_last_four}
									<span class="card-meta">•••• {p.card_last_four}</span>
								{/if}
							</Badge>
						</td>
						<td class="right mono">
							{formatCurrency(p.amount, p.currency)}
							{#if p.settled_amount !== null && p.settled_amount !== undefined}
								<!-- What the RAIL says it settled, beside what AP authorized.
								     Both figures are rendered; no delta is computed here — the
								     backend owns whether this covers the invoice. Without this
								     line an invoice held for an under-settlement was invisible. -->
								<span class="settled-line" data-testid="payment-settled-amount">
									{m('payments.history.settled', {
										// `settled_currency` is NULL for a rail that reported an
										// amount but no code — the same unprovable case as an
										// unstated authorized currency, and it takes the same
										// honest bare rendering rather than the org default.
										amount: formatCurrency(p.settled_amount, p.settled_currency)
									})}
								</span>
							{/if}
						</td>
						<td>
							<span
								class="status-pill-wrap"
								class:compliance-ring={p.status === 'pending_compliance'}
							>
								<Badge tone={PAYMENT_STATUS_TONES[p.status]} variant={p.status}>
									{statusLabel(p.status)}
								</Badge>
							</span>
						</td>
						<td class="mono muted">{p.reference ?? '—'}</td>
						<td class="muted">{formatDate(p.created_at)}</td>
						<td class="actions">
							{#if p.status === 'completed'}
								<RowAction onclick={() => downloadRemittance(p)}>{m('payments.history.remittance')}</RowAction>
							{/if}
							{#if canReleaseHold(p)}
								<RowAction variant="accent" onclick={() => openCompliance(p, 'release')}>
									{m('payments.history.complianceRelease')}
								</RowAction>
							{/if}
							{#if canDismissHold(p)}
								<RowAction variant="danger" onclick={() => openCompliance(p, 'dismiss')}>
									{m('payments.history.complianceDismiss')}
								</RowAction>
							{/if}
							{#if canAcceptSettlement(p)}
								<RowAction
									variant="accent"
									onclick={() => openSettlement(p)}
									ariaLabel={m('payments.history.settlementAcceptAria', {
										invoice: p.invoice_number ?? p.id.slice(0, 8)
									})}
								>
									{m('payments.history.settlementAccept')}
								</RowAction>
							{/if}
							{#if canVoid(p)}
								<RowAction variant="danger" onclick={() => openVoid(p)}>{m('payments.history.void')}</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>

		{#if paymentStore.hasMore}
			<div class="load-more-row">
				<button class="btn-load-more" onclick={() => paymentStore.loadMore()} disabled={paymentStore.loading}>
					{paymentStore.loading
						? m('common.loading')
						: m('payments.history.loadMore', { shown: paymentStore.all.length, total: paymentStore.total })}
				</button>
			</div>
		{:else if paymentStore.total > 0}
			<div class="load-more-row">
				<span class="load-more-end"
					>{m('payments.history.showingAll', { total: paymentStore.total })}</span
				>
			</div>
		{/if}

	{:else if activeTab === 'runs'}
		{#if runsErrored}
			<!-- A failed load is a dead end without a way back: the toast that
			     reported it has already faded. Same error-with-retry block
			     `/admin/api-keys` uses. -->
			<div class="state error" data-testid="runs-error" role="alert">
				<p>{m('payments.runs.empty.errored')}</p>
				<button type="button" class="btn-cancel" onclick={loadRuns}>{m('payments.runs.retry')}</button>
			</div>
		{:else}
		<DataTable
			columns={RUNS_COLUMNS}
			isEmpty={runs.length === 0}
			empty={runsLoading ? m('common.loading') : m('payments.runs.empty')}
			colspan={7}
		>
			{#snippet body()}
				{#each runs as run (run.id)}
					<tr
						class="clickable"
						onclick={(e) => {
							if (isRowOpenClick(e)) activeRunId = run.id;
						}}
					>
						<td class="mono">
							<RowLink
								onclick={() => (activeRunId = run.id)}
								ariaLabel={`View payment run ${run.id.slice(0, 8)}`}
							>
								{run.id.slice(0, 8)}
							</RowLink>
						</td>
						<td><Badge tone={runStatusTone(run.status)} variant={run.status}>{runLabel(run.status)}</Badge></td>
						<td class="right mono">
							{run.total_amount ? formatCurrency(run.total_amount, run.currency) : '—'}
						</td>
						<td>{run.payment_count}</td>
						<td class="muted">{formatDate(run.executed_at)}</td>
						<td class="muted">{formatDate(run.created_at)}</td>
						<td class="actions">
							{#if canSyncRunErp(run)}
								<RowAction
									onclick={() => openErpSync(run)}
									ariaLabel={m('payments.runs.erpSyncAria', { run: run.id.slice(0, 8) })}
								>
									{m('payments.runs.erpSync')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
		{/if}
	{:else if activeTab === 'cards'}
		{#if cardDashboard}
			<div class="rebate-grid">
				<div class="rebate-card">
					<span class="rebate-label">{m('payments.cards.rebatesThisMonth')}</span>
					<span class="rebate-value">{formatCurrency(cardDashboard.rebates_this_month, cardDashboard.currency)}</span>
					<span class="rebate-hint">{m('payments.cards.rebatesRealizedHint')}</span>
					{#if cardDashboard.rebates_this_month_by_status.pending_total > 0}
						<span class="rebate-hint">
							{m('payments.cards.rebatesPendingHint', {
								amount: formatCurrency(cardDashboard.rebates_this_month_by_status.pending_total, cardDashboard.currency)
							})}
						</span>
					{/if}
				</div>
				<div class="rebate-card">
					<span class="rebate-label">{m('payments.cards.rebatesYtd')}</span>
					<span class="rebate-value">{formatCurrency(cardDashboard.rebates_ytd, cardDashboard.currency)}</span>
					<span class="rebate-hint">{m('payments.cards.rebatesRealizedHint')}</span>
					{#if cardDashboard.rebates_ytd_by_status.pending_total > 0}
						<span class="rebate-hint">
							{m('payments.cards.rebatesPendingHint', {
								amount: formatCurrency(cardDashboard.rebates_ytd_by_status.pending_total, cardDashboard.currency)
							})}
						</span>
					{/if}
				</div>
				<div class="rebate-card highlight">
					<span class="rebate-label">{m('payments.cards.projectedAnnual')}</span>
					<span class="rebate-value">{formatCurrency(cardDashboard.projected_annual_rebates, cardDashboard.currency)}</span>
					<span class="rebate-hint">{m('payments.cards.atRunRate')}</span>
				</div>
				<div class="rebate-card">
					<span class="rebate-label">{m('payments.cards.activeCards')}</span>
					<span class="rebate-value">
						{cardDashboard.active_cards}
						<span class="rebate-sub">{formatCurrency(cardDashboard.active_cards_value, cardDashboard.currency)}</span>
					</span>
				</div>
			</div>
		{/if}

		<!-- Card-rebate lifecycle. The KPI cards above render a muted
		     "+{amount} pending confirmation" hint; until now nothing on this
		     page could advance that status, so the pending bucket only ever
		     grew. Advancing a rebate RECORDS what the processor already did on
		     its own statement — it never moves money, which is why the dialog
		     says so before either action can be taken. -->
		<h2 class="section-heading">{m('payments.rebates.title')}</h2>
		<p class="section-note">{m('payments.rebates.subtitle')}</p>

		{#if rebateExcludedCount > 0}
			<!-- Every ROW states its own card's currency, so a mixed list renders
			     fine. What this says is narrower and still true: the total below
			     the table is a single-currency figure, and these rows are not in
			     it. Without it, a total beside a mixed list reads as complete. -->
			<p class="fx-skipped" role="alert" data-testid="rebate-mixed-currency">
				{m('payments.rebates.mixedCurrency', { n: rebateExcludedCount })}
			</p>
		{/if}

		<div data-testid="rebates-table">
		<DataTable
			columns={REBATES_COLUMNS}
			isEmpty={rebates.length === 0}
			empty={loadingRebates
				? m('payments.rebates.loading')
				: rebatesErrored
					? m('payments.rebates.errored')
					: m('payments.rebates.empty')}
			colspan={6}
		>
			{#snippet body()}
				{#each rebates as rebate (rebate.id)}
					<tr>
						<td class="mono">{rebate.period ?? '\u2014'}</td>
						<td class="mono muted">{rebate.virtual_card_id.slice(0, 8)}</td>
						<td class="right mono">{formatRebateRate(rebate.rate)}</td>
						<!-- The row's OWN currency, resolved server-side from the card
						     that earned the rebate. `card_rebates` has no currency
						     column, so this used to be unavailable and a
						     mixed-currency programme's amounts had to render bare. -->
						<td class="right mono" data-testid="rebate-amount">
							{formatCurrency(rebate.amount, rebate.currency)}
						</td>
						<td>
							<Badge tone={rebateTone(rebate.status)}>
								{rebateStatusLabel(rebate.status)}
							</Badge>
						</td>
						<td class="actions">
							{#if canManageRebates()}
								{@const step = nextRebateTransition(rebate.status)}
								{#if step === 'confirm'}
									<RowAction
										onclick={() => openRebate(rebate, 'confirm')}
										ariaLabel={m('payments.rebates.confirmAria', {
											rebate: rebate.id.slice(0, 8)
										})}
									>
										{m('payments.rebates.confirm')}
									</RowAction>
								{:else if step === 'mark-paid'}
									<RowAction
										onclick={() => openRebate(rebate, 'mark-paid')}
										ariaLabel={m('payments.rebates.markPaidAria', {
											rebate: rebate.id.slice(0, 8)
										})}
									>
										{m('payments.rebates.markPaid')}
									</RowAction>
								{/if}
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
		</div>

		{#if hasMoreRebates}
			<div class="load-more-row">
				<button
					class="btn-load-more"
					onclick={loadMoreRebates}
					disabled={loadingMoreRebates}
					data-testid="load-more-rebates"
				>
					{loadingMoreRebates ? m('common.loading') : m('payments.rebates.loadMore', { shown: rebates.length, total: rebateCount })}
				</button>
			</div>
		{:else if rebateCount > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('payments.rebates.showingAll', { total: rebateCount })}</span>
			</div>
		{/if}

		{#if rebateCount > 0 && rebateListCurrency}
			<!-- The WHOLE filtered set's total, off the envelope — never a sum of
			     the loaded page. The Load-more control above can leave most of the
			     rows unfetched, and a total that moved as you paged would be the
			     exact defect `decisions.md` §79/§82 record. -->
			<p class="rebate-total" data-testid="rebate-total">
				{m('payments.rebates.total')}
				<strong class="mono">{formatCurrency(rebateTotalAmount, rebateListCurrency)}</strong>
			</p>
		{/if}

		<h2 class="section-heading">{m('payments.rebates.cardsTitle')}</h2>

		<!-- The Cards tab stacks TWO tables (rebates above, cards here), so a
		     page-wide `table tbody tr` count reads both. This wrapper is the
		     scope the cards-pagination spec measures its page size against —
		     a real API, not scaffolding: without it the assertion silently
		     starts counting a sibling table's rows. -->
		<div data-testid="cards-table">
		<DataTable
			columns={CARDS_COLUMNS}
			isEmpty={cards.length === 0}
			empty={loadingCards
				? m('payments.cards.loading')
				: cardsErrored
					? m('payments.cards.errored')
					: m('payments.cards.empty')}
			colspan={8}
		>
			{#snippet body()}
				{#each cards as card (card.id)}
					<tr>
						<td>
							<Badge tone="accent" variant="card-badge">
								{card.card_provider}
								{#if card.last_four}
									<span class="card-meta">•••• {card.last_four}</span>
								{/if}
							</Badge>
						</td>
						<td>{card.vendor_name ?? '—'}</td>
						<td class="mono">{card.invoice_number ?? '—'}</td>
						<td class="right mono">{formatCurrency(card.amount_limit, card.currency)}</td>
						<td class="right mono muted">
							{card.amount_charged
								? formatCurrency(card.amount_charged, card.currency)
								: '—'}
						</td>
						<td><Badge tone={cardTone(card.status)} variant={card.status}>{card.status}</Badge></td>
						<td class="muted">{formatDate(card.expires_at)}</td>
						<td class="actions">
							{#if card.status === 'created' || card.status === 'sent' || card.status === 'active'}
								<RowAction onclick={() => revealCard(card.id)}>{m('payments.cards.reveal')}</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
		</div>

		{#if hasMoreCards}
			<div class="load-more-row">
				<button class="btn-load-more" onclick={loadMoreCards} disabled={loadingMoreCards}>
					{loadingMoreCards ? m('common.loading') : m('payments.cards.loadMore', { shown: cards.length, total: cardsTotal })}
				</button>
			</div>
		{:else if cardsTotal > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('payments.cards.showingAll', { total: cardsTotal })}</span>
			</div>
		{/if}
	{/if}
</PageHeader>

{#if activeRunId}
	<RunDetailModal
		runId={activeRunId}
		onclose={() => (activeRunId = null)}
		onchange={onRunChanged}
	/>
{/if}

<Modal
	open={revealedCard !== null}
	ariaLabel="Card details"
	title={m('payments.cardDetails.title')}
	onclose={() => (revealedCard = null)}
>
	{#if revealedCard}
		<p class="modal-hint">
			{m('payments.cardDetails.hint')}
		</p>
		<div class="card-details">
			<div class="card-row">
				<span class="card-label">{m('payments.cardDetails.cardNumber')}</span>
				<span class="card-value mono" data-testid="card-details-number">{revealedCard.cardNumber}</span>
			</div>
			<div class="card-row">
				<span class="card-label">{m('payments.cardDetails.cvv')}</span>
				<span class="card-value mono" data-testid="card-details-cvv">{revealedCard.cvv}</span>
			</div>
			<div class="card-row">
				<span class="card-label">{m('payments.cardDetails.expires')}</span>
				<span class="card-value mono" data-testid="card-details-expires">{revealedCard.expires}</span>
			</div>
		</div>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (revealedCard = null)}>
				{m('payments.cardDetails.close')}
			</button>
		</div>
	{/if}
</Modal>

<!-- Advance one card rebate along `pending` → `confirmed` → `paid_out`.
     Confirm-then-act, like the compliance-hold and settlement dialogs beside
     it — but deliberately NOT armed/destructive-tinted: this records what the
     processor already did on its own statement. It requests no payout and pays
     no one, and the copy leads with that so "Mark paid out" cannot be read as a
     button that moves money. -->
<Modal
	open={rebateTarget !== null}
	ariaLabel="Advance card rebate"
	title={rebateMode === 'confirm'
		? m('payments.rebates.dialog.confirmTitle')
		: m('payments.rebates.dialog.markPaidTitle')}
	onclose={closeRebate}
>
	{#if rebateTarget}
		<p class="modal-hint">
			<strong class="mono">{rebateTarget.id.slice(0, 8)}</strong>
			{#if rebateTarget.period}· {rebateTarget.period}{/if}
			· {formatRebateRate(rebateTarget.rate)}
		</p>
		<!-- The figure being recorded, rendered the same way the table renders
		     it — under the row's own currency, off the row. -->
		<p class="rebate-dialog-amount mono" data-testid="rebate-dialog-amount">
			{formatCurrency(rebateTarget.amount, rebateTarget.currency)}
		</p>

		<p class="modal-note" data-testid="rebate-warning">
			{rebateMode === 'confirm'
				? m('payments.rebates.dialog.confirmWarning')
				: m('payments.rebates.dialog.markPaidWarning')}
		</p>

		{#if rebateError}
			<p class="state error" role="alert" data-testid="rebate-error">{rebateError}</p>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={closeRebate}>
				{m('common.cancel')}
			</button>
			<button
				type="button"
				class="btn-primary"
				data-testid="rebate-confirm"
				disabled={rebateBusy}
				onclick={commitRebate}
			>
				{rebateBusy
					? m('payments.rebates.dialog.busy')
					: rebateMode === 'confirm'
						? m('payments.rebates.dialog.confirmCta')
						: m('payments.rebates.dialog.markPaidCta')}
			</button>
		</div>
	{/if}
</Modal>

<Modal
	open={voidTarget !== null}
	ariaLabel="Void payment"
	title={voidCardResult && voidCardDisposition !== 'closed'
		? m('payments.void.card.title')
		: m('payments.void.title')}
	onclose={closeVoid}
>
	{#if voidTarget}
		<p class="modal-hint">
			<strong>{voidTarget.invoice_number ?? voidTarget.id.slice(0, 8)}</strong>
			{#if voidTarget.vendor_name}· {voidTarget.vendor_name}{/if}
			· {formatCurrency(voidTarget.amount, voidTarget.currency)}
		</p>
		{#if voidCardResult}
			<!-- The void landed; its card leg did not. Both legs are best-effort so
			     the accounting void is never blocked by a provider outage — which
			     is exactly why the outcome has to be SAID here rather than left on
			     the audit row. Retry is offered only where it can work. -->
			{#if voidCardDisposition === 'closed'}
				<!-- A retry landed. Confirm it here rather than closing on a toast:
				     this panel is the record of the card being chased, so the
				     operator sees the outcome that ends the chase. -->
				<p class="modal-hint" data-testid="void-card-closed">
					{m('payments.void.card.closed')}
				</p>
			{:else}
				<p class="modal-warn" role="alert" data-testid="void-card-warning">
					{voidCardDisposition === 'not_closed_final'
						? m('payments.void.card.final')
						: m('payments.void.card.retryable')}
				</p>
			{/if}
			{#if voidCardResult.void_card_outcome}
				{@const labelKey = voidCardOutcomeLabelKey(voidCardResult.void_card_outcome)}
				<p class="modal-hint" data-testid="void-card-outcome">
					<!-- An unknown tag renders raw rather than being dropped or dressed
					     up as something friendlier: it is PII-free by construction
					     (`card_cancel_error:*` carries an exception TYPE, never the
					     provider's body) and the operator needs the literal value to
					     quote at support. -->
					{labelKey ? m(labelKey) : voidCardResult.void_card_outcome}
				</p>
			{/if}
			{#if cardRetryFailed}
				<p class="state error" role="alert" data-testid="void-card-retry-failed">
					{m('payments.void.card.retryFailed')}
				</p>
			{/if}
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={closeVoid} data-testid="void-card-close">
					{m('payments.close')}
				</button>
				{#if canRetryCardCancel}
					<button
						type="button"
						class="btn-primary"
						data-testid="void-card-retry"
						disabled={retryingCardCancel}
						onclick={commitCardCancelRetry}
					>
						{retryingCardCancel
							? m('payments.void.card.retrying')
							: m('payments.void.card.retry')}
					</button>
				{/if}
			</div>
		{:else}
			<p class="modal-warn">
				{m('payments.void.warning')}
			</p>
			<form onsubmit={(e) => { e.preventDefault(); commitVoid(); }}>
				<label>
					<span>{m('payments.void.reason')}</span>
					<input
						type="text"
						bind:value={voidReason}
						placeholder={m('payments.void.reasonPlaceholder')}
						maxlength="500"
						data-testid="void-reason"
						autofocus
					/>
				</label>
				<div class="modal-footer">
					<button type="button" class="btn-cancel" onclick={closeVoid}>
						{m('common.cancel')}
					</button>
					<button
						type="submit"
						class="btn-danger"
						data-testid="void-confirm"
						disabled={voiding || !voidReason.trim()}
					>
						{voiding ? m('payments.void.voiding') : m('payments.void.confirm')}
					</button>
				</div>
			</form>
		{/if}
	{/if}
</Modal>

<!-- Compliance-hold resolution. Confirm-then-act like the void dialog above:
     release re-runs the gate (and can dispatch money), dismiss gives up on the
     payment — neither should be a single stray click on a table row. -->
<Modal
	open={complianceTarget !== null}
	ariaLabel="Resolve compliance hold"
	title={complianceMode === 'release'
		? m('payments.compliance.release.title')
		: m('payments.compliance.dismiss.title')}
	onclose={closeCompliance}
>
	{#if complianceTarget}
		<p class="modal-hint">
			<strong>{complianceTarget.invoice_number ?? complianceTarget.id.slice(0, 8)}</strong>
			{#if complianceTarget.vendor_name}· {complianceTarget.vendor_name}{/if}
			· {formatCurrency(complianceTarget.amount, complianceTarget.currency)}
		</p>
		<p class="modal-warn">
			{complianceMode === 'release'
				? m('payments.compliance.release.warning')
				: m('payments.compliance.dismiss.warning')}
		</p>
		<form onsubmit={(e) => { e.preventDefault(); commitCompliance(); }}>
			{#if complianceMode === 'dismiss'}
				<label>
					<span>{m('payments.compliance.reason')}</span>
					<input
						type="text"
						bind:value={complianceReason}
						placeholder={m('payments.compliance.reasonPlaceholder')}
						maxlength="1000"
						autofocus
					/>
				</label>
			{/if}
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={closeCompliance}>
					{m('common.cancel')}
				</button>
				{#if complianceMode === 'release'}
					<button type="submit" class="btn-pay" disabled={complianceBusy}>
						{complianceBusy
							? m('payments.compliance.release.busy')
							: m('payments.compliance.release.confirm')}
					</button>
				{:else}
					<button
						type="submit"
						class="btn-danger"
						disabled={complianceBusy || !complianceReason.trim()}
					>
						{complianceBusy
							? m('payments.compliance.dismiss.busy')
							: m('payments.compliance.dismiss.confirm')}
					</button>
				{/if}
			</div>
		</form>
	{/if}
</Modal>

<!-- Accept a short / unverifiable settlement as final. Confirm-then-act like
     the void + compliance dialogs above, and for the same reason: the money has
     already moved, so this closes the payable out at the figure the rail
     actually settled. Irreversible — it is not a void. -->
<Modal
	open={settlementTarget !== null}
	ariaLabel="Accept settlement as final"
	title={m('payments.settlement.title')}
	onclose={closeSettlement}
>
	{#if settlementTarget}
		<p class="modal-hint">
			<strong>{settlementTarget.invoice_number ?? settlementTarget.id.slice(0, 8)}</strong>
			{#if settlementTarget.vendor_name}· {settlementTarget.vendor_name}{/if}
		</p>
		<!-- Both figures side by side, neither derived from the other — and each
		     under the currency the SERVER stated for it. The authorized half used
		     to fall back to the org default while the settled half rendered
		     `settled_currency`, so a EUR payment read "Authorized $1,200.00"
		     above "Settled €1,150.00": a fabricated code, on the one screen whose
		     entire job is to let a human compare these two figures, and which
		     exists because a rail can settle in a currency we never authorized. -->
		<dl class="settlement-figures" data-testid="settlement-figures">
			<div>
				<dt>{m('payments.settlement.authorized')}</dt>
				<dd class="mono" data-testid="settlement-authorized-figure">
					{formatCurrency(settlementTarget.amount, settlementTarget.currency)}
				</dd>
			</div>
			<div>
				<dt>{m('payments.settlement.settled')}</dt>
				<dd class="mono" data-testid="settlement-settled-figure">
					{formatCurrency(settlementTarget.settled_amount, settlementTarget.settled_currency)}
				</dd>
			</div>
		</dl>

		{#if settlementInvoiceStatus === null}
			<p class="modal-hint" data-testid="settlement-checking">
				{m('payments.settlement.checking')}
			</p>
		{:else if settlementInvoiceStatus !== SETTLEMENT_HELD_STATUS && settlementInvoiceStatus !== '?'}
			<!-- Nothing to accept: the invoice is no longer held. Say so instead
			     of offering a confirm the backend would 409. -->
			<p class="modal-warn" role="alert" data-testid="settlement-not-held">
				{m('payments.settlement.notHeld')}
				<StatusBadge
					status={settlementInvoiceStatus as import('$lib/types/invoice').InvoiceStatus}
				/>
			</p>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={closeSettlement}>
					{m('payments.close')}
				</button>
			</div>
		{:else}
			<p class="modal-warn" data-testid="settlement-warning">
				{m('payments.settlement.warning')}
			</p>
			{#if settlementError}
				<p class="state error" role="alert" data-testid="settlement-error">{settlementError}</p>
			{/if}
			<form onsubmit={(e) => { e.preventDefault(); commitSettlement(); }}>
				<label>
					<span>{m('payments.settlement.reason')}</span>
					<input
						type="text"
						bind:value={settlementReason}
						placeholder={m('payments.settlement.reasonPlaceholder')}
						maxlength="1000"
						data-testid="settlement-reason"
						autofocus
					/>
				</label>
				<div class="modal-footer">
					<button type="button" class="btn-cancel" onclick={closeSettlement}>
						{m('common.cancel')}
					</button>
					<button
						type="submit"
						class="btn-danger"
						data-testid="settlement-confirm"
						disabled={settlementBusy || !settlementReason.trim()}
					>
						{settlementBusy
							? m('payments.settlement.busy')
							: m('payments.settlement.confirm')}
					</button>
				</div>
			</form>
		{/if}
	{/if}
</Modal>

<!-- Re-run the ERP sync-back for one run. Moves no money and is idempotent by
     construction, so this confirms rather than arming — and then reports the
     REAL per-leg counts, leading with `transitioned` (the only one that answers
     "did this recover anything"). A run with nothing to recover says so. -->
<Modal
	open={erpSyncTarget !== null}
	ariaLabel="Retry ERP sync for payment run"
	title={m('payments.erpSync.title')}
	onclose={closeErpSync}
>
	{#if erpSyncTarget}
		<p class="modal-hint">
			<strong class="mono">{erpSyncTarget.id.slice(0, 8)}</strong>
			· {erpSyncTarget.payment_count}
			{m('payments.summary.payments')}
			{#if erpSyncTarget.total_amount}·
				{formatCurrency(erpSyncTarget.total_amount, erpSyncTarget.currency)}{/if}
		</p>

		{#if erpSyncResult}
			<p
				class="modal-hint"
				role="status"
				data-testid="erp-sync-outcome"
				class:sync-nothing={erpSyncResult.transitioned === 0}
			>
				{erpSyncResult.transitioned === 0
					? m('payments.erpSync.nothing')
					: m('payments.erpSync.recovered', { count: erpSyncResult.transitioned })}
			</p>
			<dl class="sync-counts" data-testid="erp-sync-counts">
				<div>
					<dt>{m('payments.erpSync.counts.transitioned')}</dt>
					<dd data-testid="erp-sync-transitioned">{erpSyncResult.transitioned}</dd>
				</div>
				<div>
					<dt>{m('payments.erpSync.counts.synced')}</dt>
					<dd data-testid="erp-sync-synced">{erpSyncResult.synced}</dd>
				</div>
				<div>
					<dt>{m('payments.erpSync.counts.skipped')}</dt>
					<dd data-testid="erp-sync-skipped">{erpSyncResult.skipped}</dd>
				</div>
				<div>
					<dt>{m('payments.erpSync.counts.held')}</dt>
					<dd data-testid="erp-sync-held">{erpSyncResult.held}</dd>
				</div>
				<div>
					<dt>{m('payments.erpSync.counts.failed')}</dt>
					<dd data-testid="erp-sync-failed">{erpSyncResult.failed}</dd>
				</div>
			</dl>
			<p class="modal-hint">{m('payments.erpSync.countsHint')}</p>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={closeErpSync}>
					{m('payments.close')}
				</button>
			</div>
		{:else}
			<p class="modal-warn" data-testid="erp-sync-warning">{m('payments.erpSync.warning')}</p>
			{#if erpSyncError}
				<p class="state error" role="alert" data-testid="erp-sync-error">{erpSyncError}</p>
			{/if}
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={closeErpSync}>
					{m('common.cancel')}
				</button>
				<button
					type="button"
					class="btn-pay"
					data-testid="erp-sync-confirm"
					disabled={erpSyncBusy}
					onclick={commitErpSync}
				>
					{erpSyncBusy ? m('payments.erpSync.busy') : m('payments.erpSync.confirm')}
				</button>
			</div>
		{/if}
	{/if}
</Modal>

<!-- Compare what each configured processor would charge to pay ONE invoice.
     Read-only and ADVISORY — `POST /api/payments/corridor-quotes` books no
     payment, claims no run and picks no rail; `payment_corridor.pick_corridor`
     plus the org's configured provider still decide that at execute time. The
     notice therefore leads, before any figure: a comparison a user reads as a
     choice is worse than no comparison. -->
<Modal
	open={quoteTarget !== null}
	ariaLabel="Compare payment routes"
	title={m('payments.quotes.title')}
	width="lg"
	onclose={closeQuotes}
>
	{#if quoteTarget}
		<p class="modal-note" data-testid="quotes-advisory">
			{m('payments.quotes.advisory')}
		</p>
		<p class="modal-hint">
			<strong class="mono">{quoteTarget.invoice_number}</strong>
			{#if quoteTarget.vendor_name}· {quoteTarget.vendor_name}{/if}
			· {formatCurrency(quoteTarget.amount, quoteTarget.currency)}
		</p>

		<div class="quote-controls">
			<div class="quote-modes" role="group" aria-label={m('payments.quotes.modeAria')}>
				<button
					type="button"
					class="filter-chip"
					class:active={quoteMode === 'cheapest'}
					aria-pressed={quoteMode === 'cheapest'}
					data-testid="quotes-mode-cheapest"
					onclick={() => setQuoteMode('cheapest')}
				>
					{m('payments.quotes.mode.cheapest')}
				</button>
				<button
					type="button"
					class="filter-chip"
					class:active={quoteMode === 'fastest'}
					aria-pressed={quoteMode === 'fastest'}
					data-testid="quotes-mode-fastest"
					onclick={() => setQuoteMode('fastest')}
				>
					{m('payments.quotes.mode.fastest')}
				</button>
			</div>
			<label class="quote-method">
				<span>{m('payments.quotes.methodLabel')}</span>
				<select bind:value={quoteMethod} data-testid="quotes-method" onchange={() => runQuotes()}>
					{#each QUOTE_METHODS as opt (opt.value)}
						<option value={opt.value}>{m(opt.key)}</option>
					{/each}
				</select>
			</label>
		</div>

		{#if quoteBusy}
			<p class="state" data-testid="quotes-loading">{m('common.loading')}</p>
		{:else if quoteError}
			<!-- Persistent, not a toast. The 409 body names each provider's own
			     machine reason, which is what tells an operator whether the corridor
			     is unsupported or the processors are misconfigured. -->
			<p class="state error" role="alert" data-testid="quotes-error">{quoteError}</p>
		{:else if quoteResult}
			<table
				class="quote-table"
				data-testid="quotes-ranking"
				aria-label={m('payments.quotes.rankingCaption')}
			>
				<thead>
					<tr>
						<th scope="col">{m('payments.quotes.col.processor')}</th>
						<th scope="col">{m('payments.quotes.col.rail')}</th>
						<th scope="col" class="right">{m('payments.quotes.col.eta')}</th>
						<th scope="col" class="right">{m('payments.quotes.col.fee')}</th>
						<th scope="col" class="right">{m('payments.quotes.col.total')}</th>
					</tr>
				</thead>
				<tbody>
					{#each allQuotes(quoteResult).filter(isRankedQuote) as q, i (q.provider)}
						<tr class:quote-winner={i === 0} data-testid="quote-row">
							<td>
								<span class="mono">{q.provider}</span>
								{#if i === 0}
									<Badge tone="success" variant="quote-best">
										{m('payments.quotes.best')}
									</Badge>
								{/if}
							</td>
							<td class="muted">{q.method}</td>
							<td class="right">{m('payments.quotes.etaDays', { n: q.eta_business_days })}</td>
							<td class="right mono">
								{formatCurrency(q.flat_fee, quoteResult.currency)}
								+ {formatFeeRate(q.pct_fee)}
							</td>
							<td class="right mono" data-testid="quote-total">
								{formatCurrency(q.total_cost, quoteResult.currency)}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>

			{#if isPositiveAmount(quoteResult.savings_vs_runner_up)}
				<!-- The server computed this delta; the client never subtracts two
				     amounts (frontend/CLAUDE.md § Money formatting). -->
				<p class="quote-savings" data-testid="quotes-savings">
					{m('payments.quotes.savings', {
						amount: formatCurrency(quoteResult.savings_vs_runner_up, quoteResult.currency)
					})}
				</p>
			{/if}

			{#if unrankedQuotes(quoteResult).length > 0}
				<!-- Rendered EXPLICITLY, never omitted. An adapter with no published
				     fee schedule fails closed (`no_quote_endpoint`) and drops out of
				     the ranking — `modern_treasury` is exactly that case today. A
				     tenant on such a rail would otherwise watch an auction its own
				     rail never entered and read the winner as the best route open to
				     it. A ranking that hides its own gaps is worse than no ranking. -->
				<section class="quote-unranked" data-testid="quotes-unranked">
					<h3>{m('payments.quotes.unranked.title')}</h3>
					<p class="quote-unranked-note">{m('payments.quotes.unranked.note')}</p>
					<ul>
						{#each unrankedQuotes(quoteResult) as q (q.provider)}
							<li data-testid="quote-unranked-row">
								<span class="mono">{q.provider}</span>
								<span class="quote-unranked-reason">{quoteReasonText(q)}</span>
							</li>
						{/each}
					</ul>
				</section>
			{/if}
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={closeQuotes}>
				{m('payments.close')}
			</button>
		</div>
	{/if}
</Modal>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */

	/* Error-with-retry block — same shape as `/admin/api-keys`. */
	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}
	.state.error {
		color: var(--danger);
	}
	.state.error p {
		margin: 0 0 8px;
	}

	/* --- Summary --- */

	.summary-cards {
		display: flex;
		gap: 12px;
		flex-wrap: wrap;
	}

	.scard {
		flex: 1;
		min-width: 140px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 14px 16px;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.scard.rebate {
		border-color: rgba(31, 168, 106, 0.3);
		background: rgba(31, 168, 106, 0.04);
	}

	.scard-value {
		font-size: 1.2rem;
		font-weight: 700;
		color: var(--text);
	}

	.scard.rebate .scard-value {
		color: #1fa86a;
	}

	.scard-label {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	/* --- Tabs --- */

	.tabs {
		display: flex;
		gap: 0;
		border-bottom: 1px solid var(--border);
		/* WCAG 1.4.10: let the tab row wrap rather than push the page wider
		   than a narrow viewport. */
		flex-wrap: wrap;
	}

	.tab {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 10px 20px;
		border: none;
		background: none;
		color: var(--text-muted);
		font-size: 0.88rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		border-bottom: 2px solid transparent;
		margin-bottom: -1px;
		transition: all 0.12s;
	}

	.tab:hover {
		color: var(--text);
	}

	.tab.active {
		color: var(--accent);
		border-bottom-color: var(--accent);
	}

	.tab-count {
		font-size: 0.72rem;
		padding: 1px 6px;
		border-radius: 8px;
		background: rgba(99, 140, 255, 0.12);
		color: var(--accent);
		font-weight: 600;
	}

	/* --- Queue --- */

	.overdue {
		background: rgba(224, 64, 64, 0.04);
	}

	.overdue-text {
		color: #f06464;
	}

	/* Date + its overdue pill. The gap used to be the pill's own
	   `margin-left`; `<Badge>` owns colour and metrics, the caller owns
	   placement. */
	.due-cell {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		/* The date and the pill were separate inline runs before and could
		   break between them; a flex row must be told to keep doing that or a
		   narrow viewport gets a horizontal scrollbar (SC 1.4.10). */
		flex-wrap: wrap;
	}

	/* --- Status badges --- */

	/* Every pill on this page is `<Badge>`; the tone per status lives in
	   `PAYMENT_STATUS_TONES` / `RUN_STATUS_TONES` in `$lib/types/payment`
	   (shared with `RunDetailModal`, which badges the same two vocabularies)
	   and in the page-local `CARD_STATUS_TONES`, which has no second caller.
	   What stays here is placement and the one emphasis the palette has no
	   tone for.

	   Held by the sanctions/KYC gate is an attention state, not a failure —
	   amber, so it reads as "needs a human" and not as `failed`. It shares the
	   warning tone with `pending`, which is correct (both are waiting), so the
	   RING, not the fill, is what separates a payment a human must clear from
	   one merely waiting its turn. Before the tokens the two were told apart by
	   two hand-picked oranges; that read as a distinction but was never a
	   stated one, and is exactly what a later palette nudge collapses silently.
	   `Compliance Hold` vs `Pending` already carries it in text (SC 1.4.1) —
	   this is scannability, which is why the status is first-class at all.

	   The ring is drawn by this wrapper rather than by the badge: a
	   caller-specific emphasis is not a sixth tone, and `Badge` deliberately
	   exposes no styling props. A non-inset shadow takes no layout space at
	   all, so the pill stays aligned with every sibling badge — which is what
	   the previous `inset` shadow was working around. */
	.status-pill-wrap {
		display: inline-flex;
		border-radius: 12px;
	}

	.compliance-ring {
		box-shadow: 0 0 0 1px var(--warning-on-tint);
	}

	/* --- Settlement (what the rail says it moved) --- */

	/* Sub-line under the authorized amount. Muted and smaller so the AUTHORIZED
	   figure stays the row's headline — this is provenance, not a correction. */
	.settled-line {
		display: block;
		font-size: 0.75rem;
		color: var(--text-muted);
	}

	.settlement-figures,
	.sync-counts {
		display: flex;
		flex-wrap: wrap;
		gap: 8px 20px;
		margin: 0 0 12px;
	}

	.settlement-figures dt,
	.sync-counts dt {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.settlement-figures dd,
	.sync-counts dd {
		margin: 2px 0 0;
		font-size: 0.95rem;
	}

	.sync-counts dd {
		font-variant-numeric: tabular-nums;
	}

	/* "Nothing to recover" must not read like a success. */
	.sync-nothing {
		color: var(--text-muted);
	}

	/* --- Queue selection & payment --- */

	.checkbox-col {
		width: 36px;
		text-align: center;
		padding-left: 10px;
		padding-right: 4px;
	}

	.checkbox-col input[type='checkbox'] {
		cursor: pointer;
		accent-color: var(--accent);
	}

	/* Floats above the page so selecting rows doesn't shove the queue down.
	   Same treatment as the invoices bulk-bar. */
	.pay-bar {
		position: fixed;
		left: 50%;
		bottom: 24px;
		transform: translateX(-50%);
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 10px 16px;
		background: var(--surface);
		border: 1px solid var(--accent);
		border-radius: 8px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
		z-index: 50;
		max-width: calc(100vw - 48px);
	}

	.pay-bar-row {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	/* Amber, not red: nothing has failed — the selection simply can't be
	   submitted as one run until it is narrowed to a single currency. */
	/* Same look as /discounts' `.disc-skipped` and the /cfo unconverted-outflows
	   caveat: an FX exclusion notice reads alike everywhere it appears. */
	.fx-skipped {
		font-size: 0.85rem;
		margin: 0 0 12px;
		color: #d4940a;
		font-weight: 600;
	}

	.pay-bar-warn {
		margin: 0;
		max-width: 56ch;
		font-size: 0.8rem;
		line-height: 1.35;
		color: #d4940a;
	}

	.pay-bar-count {
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--accent);
		flex: 1;
	}

	.pay-bar-savings {
		color: #1fa86a;
		font-weight: 600;
	}

	.btn-select-all-matching {
		padding: 4px 10px;
		border-radius: 4px;
		border: 1px solid var(--accent);
		background: transparent;
		color: var(--accent);
		font-size: 0.8rem;
		font-weight: 600;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-select-all-matching:disabled {
		opacity: 0.6;
		cursor: default;
	}

	.btn-select-all-matching:not(:disabled):hover {
		background: var(--accent-strong);
		color: #fff;
	}

	.pay-bar-all-matching {
		font-size: 0.8rem;
		color: var(--text-muted);
		white-space: nowrap;
	}

	.review-partial {
		margin: 8px 0 0;
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.savings-banner {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 14px;
		background: rgba(31, 168, 106, 0.08);
		border: 1px solid rgba(31, 168, 106, 0.3);
		border-radius: 6px;
		font-size: 0.85rem;
		color: var(--text);
	}

	.savings-icon {
		font-size: 1.1rem;
	}

	tbody tr.discount-eligible {
		background: rgba(31, 168, 106, 0.04);
	}

	tbody tr.discount-eligible:hover {
		background: rgba(31, 168, 106, 0.08);
	}

	/* NOT a `<Badge>`: two stacked lines, so it needs the metrics a shared
	   one-size pill deliberately doesn't have (decisions.md §47 — a pill with
	   genuinely different metrics is a different component, not a prop). It
	   takes the palette tokens instead, which is the same concession
	   `ScreeningBadge` got. */
	.discount-chip {
		display: inline-flex;
		flex-direction: column;
		gap: 2px;
		padding: 4px 8px;
		border-radius: 6px;
		background: var(--success-tint);
		color: var(--success-on-tint);
		font-size: 0.78rem;
		font-weight: 600;
		line-height: 1.2;
	}

	/* `inherit`, not `--text-muted`: the sub-line sits ON the tint, and muted
	   grey over a 15% tint is 4.11:1 on `--surface` — a WCAG 1.4.3 failure the
	   static audit can't see (it resolves no cascade) and axe only catches on a
	   rendered tab. A tint pairs with its own `-on-tint` text, sub-labels
	   included; size and weight already carry the hierarchy. */
	.discount-pct {
		font-size: 0.7rem;
		font-weight: 400;
		color: inherit;
	}

	.btn-pay {
		padding: 6px 16px;
		border-radius: 4px;
		border: none;
		background: var(--success-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-pay:hover:not(:disabled) {
		opacity: 0.9;
	}

	.btn-pay:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* A queue row the backend would refuse: readable, visibly inert, never
	   hidden — an operator has to be able to see WHAT is blocked to go clear it.
	   `.row-blocked` is now the semantic marker only; the de-emphasis is the
	   shared `.row-muted` recipe in app.css (a muted colour token).

	   It used to be `opacity: 0.72` here, and this row is the case where that
	   was worst. Group opacity fades the whole subtree, so it hit hardest the
	   one element the row exists to show: on --surface the invoice number
	   (--text) still rendered at 7.56:1, while the `.blocked-chip` naming the
	   REASON — a duplicate, a fraud flag, a line-total mismatch — composited to
	   3.45:1, the muted payment-terms cell to 3.42:1, and the status pill beside
	   it to 3.41–3.59:1. A financial-integrity signal was rendered less legible
	   than the ordinary rows around it, which inverts the point of flagging it.
	   The token leaves every descendant that sets its own colour — the chip, the
	   StatusBadge — at its own calibrated strength.

	   Keep the `tbody`-prefix warning: these rows render inside a `{#snippet}`
	   handed to `DataTable`, so a descendant selector is pruned as unused at
	   compile time (which is why the sibling `tbody tr.discount-eligible` rules
	   never applied). The shared rule in app.css is written accordingly. */

	/* Also not a `<Badge>`: it carries a whole localised sentence ("Possible
	   duplicate — unresolved"), so `white-space: normal` is load-bearing and
	   the primitive's `nowrap` would push the status column past a 320px
	   viewport (SC 1.4.10). It already spells the recipe with the tokens. */
	.blocked-chip {
		display: inline-block;
		margin-left: 6px;
		padding: 2px 8px;
		border-radius: 10px;
		background: var(--danger-tint);
		color: var(--danger-on-tint);
		font-size: 0.72rem;
		font-weight: 600;
		white-space: normal;
	}

	/* The rail-pinned sibling of `.blocked-chip` — same shape, ACCENT tone, not
	   danger: this row is payable, just only on one rail, and a red chip on a
	   selectable row would read as a refusal. Same `-tint` / `-on-tint` pair
	   rule, and the same `white-space: normal` for the same 1.4.10 reason. */
	.pinned-chip {
		display: inline-block;
		margin-left: 6px;
		padding: 2px 8px;
		border-radius: 10px;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		font-size: 0.72rem;
		font-weight: 600;
		white-space: normal;
	}

	/* The review panel's method cell for a pinned row: the rail as plain text
	   (there is no choice to offer) plus the reason beneath it, so the operator
	   isn't left guessing why this row's select vanished. */
	.pinned-method {
		display: block;
		font-size: 0.85rem;
		color: var(--text);
	}

	.pinned-method-why {
		display: block;
		font-size: 0.72rem;
		color: var(--text-muted);
	}

	.blocked-banner {
		margin: 0;
		padding: 10px 14px;
		border: 1px solid var(--danger);
		border-radius: 6px;
		background: var(--danger-tint);
		color: var(--text);
		font-size: 0.85rem;
	}

	.review-error {
		margin: 0 0 10px;
		padding: 10px 12px;
		border: 1px solid var(--danger);
		border-radius: 6px;
		background: var(--danger-tint);
		color: var(--text);
		font-size: 0.85rem;
		line-height: 1.4;
	}

	.btn-clear {
		padding: 6px 12px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.82rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-clear:hover {
		color: var(--text);
	}

	/* --- Review panel --- */

	.review-panel {
		padding: 14px;
		background: var(--bg);
		border-bottom: 1px solid var(--border);
	}

	.review-title {
		font-size: 0.78rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-bottom: 10px;
	}

	.review-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
		margin-bottom: 12px;
	}

	.review-table th {
		padding: 6px 10px;
		text-align: left;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
	}

	.review-table td {
		padding: 6px 10px;
		border-bottom: 1px solid var(--border);
	}

	.method-select {
		/* base look (border/radius/colour/font/chevron) from the global recipe */
		padding: 4px 30px 4px 8px;
		background-color: var(--surface);
		font-size: 0.82rem;
	}

	.review-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.review-total {
		font-size: 0.9rem;
		font-weight: 700;
		color: var(--text);
	}

	.btn-execute {
		padding: 8px 20px;
		border-radius: 6px;
		border: none;
		background: var(--success-strong);
		color: #fff;
		font-size: 0.88rem;
		font-weight: 600;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-execute:hover:not(:disabled) {
		opacity: 0.9;
	}

	.btn-execute:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* --- Void modal --- */

	.modal-warn {
		font-size: 0.82rem;
		color: var(--text);
		margin: 0 0 14px;
		padding: 10px 12px;
		background: rgba(224, 64, 64, 0.08);
		border: 1px solid rgba(224, 64, 64, 0.3);
		border-radius: 4px;
	}

	.btn-danger {
		padding: 8px 18px;
		border-radius: 4px;
		border: 1px solid var(--danger-strong);
		background: var(--danger-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-danger:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn-danger:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.rebate-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 12px;
		margin-bottom: 16px;
	}

	.rebate-card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 14px 16px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.rebate-card.highlight {
		border-color: rgba(31, 168, 106, 0.4);
		background: rgba(31, 168, 106, 0.05);
	}

	.rebate-label {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.rebate-value {
		font-size: 1.4rem;
		font-weight: 700;
		color: var(--text);
		line-height: 1.1;
	}

	.rebate-card.highlight .rebate-value {
		color: #1fa86a;
	}

	.rebate-sub {
		display: block;
		font-size: 0.78rem;
		font-weight: 400;
		color: var(--text-muted);
		margin-top: 2px;
	}

	.rebate-hint {
		font-size: 0.7rem;
		color: var(--text-muted);
	}

	/* Cards-tab section structure. The tab stacks two tables (rebates, then
	   the cards themselves) under one `<h1>`, so each gets a real `<h2>` —
	   heading structure, not bold text (WCAG 1.3.1). */
	.section-heading {
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--text);
		margin: 24px 0 4px;
	}

	.section-note {
		font-size: 0.8rem;
		color: var(--text-muted);
		margin: 0 0 12px;
		max-width: 78ch;
	}

	.rebate-total {
		font-size: 0.85rem;
		color: var(--text-muted);
		margin: 8px 0 0;
		text-align: right;
	}

	.rebate-dialog-amount {
		font-size: 1.35rem;
		font-weight: 600;
		color: var(--text);
		margin: 0 0 14px;
	}

	/* Neutral sibling of `.modal-warn`. Deliberately NOT the red tint: this
	   dialog records a processor statement, it does not move money, and a
	   danger-coloured box would say the opposite of the sentence inside it. */
	.modal-note {
		font-size: 0.82rem;
		color: var(--text);
		margin: 0 0 14px;
		padding: 10px 12px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: 4px;
	}

	/* Same reasoning as `.discount-pct`: this sub-label renders INSIDE a
	   `<Badge>` (the method pill and the card pill both), and `--text-muted`
	   over the 15% accent tint is 4.34:1 on `--surface`. `inherit` takes the
	   badge's own `-on-tint` colour — and is a no-op on the flat `neutral`
	   method pill, whose colour already is `--text-muted`. */
	.card-meta {
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.72rem;
		font-weight: 400;
		color: inherit;
	}

	.card-details {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 14px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
	}

	.card-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 12px;
	}

	.card-label {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.card-value {
		font-size: 1rem;
		font-weight: 600;
		color: var(--text);
		letter-spacing: 0.06em;
	}

	@media (max-width: 768px) {
		.summary-cards {
			grid-template-columns: repeat(2, 1fr);
		}

		.filter-row {
			flex-direction: column;
			align-items: stretch;
		}
	}

	/* --- Corridor quote comparison (advisory) --- */

	.quote-controls {
		display: flex;
		align-items: flex-end;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: 12px;
		margin: 0 0 14px;
	}

	.quote-modes {
		display: flex;
		gap: 6px;
	}

	.quote-method {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	.quote-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.86rem;
	}

	.quote-table th,
	.quote-table td {
		padding: 8px 10px;
		border-bottom: 1px solid var(--border);
		text-align: left;
	}

	.quote-table th {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		font-weight: 600;
	}

	.quote-table .right {
		text-align: right;
	}

	.quote-table .mono {
		font-family: var(--font-mono);
	}

	.quote-table .muted {
		color: var(--text-muted);
	}

	.quote-table tr.quote-winner td {
		font-weight: 600;
	}

	.quote-savings {
		margin: 10px 0 0;
		font-size: 0.85rem;
		color: var(--text);
	}

	.quote-unranked {
		margin-top: 18px;
		padding: 12px 14px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface-2);
	}

	.quote-unranked h3 {
		margin: 0 0 6px;
		font-size: 0.9rem;
	}

	.quote-unranked-note {
		margin: 0 0 10px;
		font-size: 0.8rem;
		color: var(--text-muted);
		max-width: 78ch;
	}

	.quote-unranked ul {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.quote-unranked li {
		display: flex;
		gap: 10px;
		flex-wrap: wrap;
		align-items: baseline;
		font-size: 0.84rem;
	}

	.quote-unranked-reason {
		color: var(--text-muted);
	}
</style>
