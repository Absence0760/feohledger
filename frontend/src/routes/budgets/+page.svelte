<script lang="ts">
	import type { Budget } from '$lib/types/budget';
	import {
		BUDGET_DIMENSIONS,
		BUDGET_DIMENSION_LABELS,
		type BudgetDimension
	} from '$lib/types/budget';
	import { auth } from '$lib/stores/auth.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import {
		listBudgets,
		getBudgetSummary,
		deleteBudget as apiDeleteBudget
	} from '$lib/api/budgets';
	import type { BudgetSummary } from '$lib/types/budget';
	import { formatCurrencyTotals } from '$lib/utils/currencyGroups';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import BudgetModal from '$lib/components/modals/BudgetModal.svelte';
	import { formatMoney } from '$lib/utils/money';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { m } from '$lib/i18n/store.svelte';
	import { page } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import { untrack } from 'svelte';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { appendUnique } from '$lib/utils/pagination';

	const PAGE_SIZE = 50;

	// Mutations are admin / cfo only (financial config).
	const canManage = $derived(auth.hasAnyRole('admin', 'cfo'));

	// --- Filter state (URL-backed) ---
	let search = $state($page.url.searchParams.get('search') ?? '');
	let dimensionFilter = $state<string>($page.url.searchParams.get('dimension') ?? 'all');

	// --- Data ---
	let budgets = $state<Budget[]>([]);
	let total = $state(0);
	let loading = $state(false);
	let pageNum = $state(1);
	let loadingMore = $state(false);

	// The list is paged: `total` is the whole filtered set, so a footer that
	// says "Showing all {total}" is only true once every row is loaded.
	let hasMore = $derived(budgets.length < total);

	// --- Modal state ---
	let showCreate = $state(false);
	let editing = $state<Budget | null>(null);
	let confirmDeleteId = $state<string | null>(null);

	const DIMENSION_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		...BUDGET_DIMENSIONS.map((d) => ({ key: d, label: BUDGET_DIMENSION_LABELS[d] }))
	]);

	const COLUMNS = $derived([
		{ label: m('budgets.col.name') },
		{ label: m('budgets.col.dimension') },
		{ label: m('budgets.col.value') },
		{ label: m('budgets.col.period') },
		{ label: m('budgets.col.allocation'), class: 'right' },
		{ label: '', class: 'actions-col' }
	]);

	// Client-side text search over the loaded page (the list endpoint already
	// filters by name/value server-side via ?search=; this keeps it responsive).
	const visibleBudgets = $derived.by(() => {
		const q = search.trim().toLowerCase();
		if (!q) return budgets;
		return budgets.filter(
			(b) =>
				b.name.toLowerCase().includes(q) || b.dimension_value.toLowerCase().includes(q)
		);
	});

	// KPI: whole-set allocation from `GET /api/budgets/summary`, over the SAME
	// dimension/period/search filters the list ran with — NOT a reduce over the
	// loaded page. The old `budgets.reduce(...)` was wrong twice: it summed one
	// page while the "Budgets" count beside it was the server's whole-set
	// `total`, and it added every row's bare `amount` across currencies into the
	// org default, so EUR 400 + USD 3 500 read as one meaningless figure. The
	// rollup groups by currency and never adds across them (`$lib/utils/currencyGroups`).
	let budgetSummary = $state<BudgetSummary | null>(null);

	// Independent sequencer: this request can be in flight alongside the list's,
	// and one shared counter would let either blank the other.
	const summarySequence = createRequestSequencer();

	async function loadBudgetSummary() {
		const token = summarySequence.start();
		try {
			const res = await getBudgetSummary(buildParams());
			if (!summarySequence.canCommit(token)) return;
			budgetSummary = res;
		} catch {
			// Non-fatal — the KPI card shows its no-figure dash rather than
			// taking the page down. The table has its own error state.
			if (summarySequence.isCurrentRequest(token)) budgetSummary = null;
		}
	}

	// One subtotal per currency, side by side — never added together; a total
	// with no established currency renders bare rather than joining the org's
	// (decisions §200).
	const allocatedGroups = $derived(formatCurrencyTotals(budgetSummary?.by_currency ?? []));
	// The org's code labels ONE figure here: the zero of a summary that landed
	// and allocated nothing. A summary that has not landed (or failed) is no
	// figure at all — `null`, which `KpiCard` draws as its dash — never a zero
	// wearing a currency nobody computed.
	const allocatedTotal = $derived(
		budgetSummary
			? (allocatedGroups[0] ?? formatMoney(0, { currency: orgCurrency.currency }))
			: null
	);
	// Currencies past the first ride the card's muted sub-line, so the headline
	// stays one readable figure while nothing is silently dropped.
	const allocatedRest = $derived(
		allocatedGroups.length > 1 ? allocatedGroups.slice(1).join(' · ') : null
	);

	// `untrack` on the `search` read: buildParams() is called from `load()`,
	// which the filter `$effect` below calls directly — and Svelte tracks reads
	// transitively through called functions, so a plain read here would make
	// that effect depend on `search` and re-fire it on every keystroke,
	// un-debounced, racing the dedicated 300ms timer (issue #168). `untrack`
	// still reads the CURRENT value, so the request carries the live search
	// term; it just stops the read registering as the caller's dependency.
	function buildParams() {
		const params: { dimension?: string; search?: string } = {};
		if (dimensionFilter !== 'all') params.dimension = dimensionFilter;
		const currentSearch = untrack(() => search);
		if (currentSearch.trim()) params.search = currentSearch.trim();
		return params;
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
			if (dimensionFilter !== 'all') url.searchParams.set('dimension', dimensionFilter);
			else url.searchParams.delete('dimension');
			if (search.trim()) url.searchParams.set('search', search.trim());
			else url.searchParams.delete('search');
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	// Sequences `load` (latest-issued wins) so a slow response for an earlier
	// filter can't land after a faster later one. `onSaved` / `deleteBudget`
	// edit the list in place with no fetch of their own, so they retire whatever
	// is in flight first — a brand-new budget needs no pre-existing row, so it
	// races even the first load. See `frontend/CLAUDE.md` § Sequencing list
	// fetches.
	const fetchSequence = createRequestSequencer();

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		try {
			const res = await listBudgets({
				...buildParams(),
				page: nextPage,
				page_size: PAGE_SIZE
			});
			// Superseded by a newer load, or by a local create/edit/delete.
			if (!fetchSequence.canCommit(token)) return;
			budgets = opts.append ? appendUnique(budgets, res.items) : res.items;
			total = res.total;
			pageNum = nextPage;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// edit still failed, and no newer load is coming to report it.
			if (!fetchSequence.isCurrentRequest(token)) return;
			toast(err instanceof Error ? err.message : m('budgets.toast.loadFailed'), 'error');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	// Dimension filter → server refetch. Debounced search re-syncs the URL +
	// refetch (server-side ILIKE keeps cross-page matches in scope).
	//
	// `searchEffectRan` skips this effect's own mount-time run: a Svelte
	// `$effect` always fires once immediately regardless of whether its
	// tracked value actually changed, so without the guard this queued a
	// SECOND, redundant `load()` ~300ms after the dimensionFilter effect
	// below already loaded the page once. `load()` replaces `budgets`
	// wholesale, so if a create/edit lands in that window, the delayed
	// duplicate can resolve afterward and silently clobber it with a stale
	// snapshot — the same class of bug fixed in UsersPanel.svelte.
	let searchTimer: ReturnType<typeof setTimeout>;
	let searchEffectRan = false;
	$effect(() => {
		search;
		if (!searchEffectRan) {
			searchEffectRan = true;
			return;
		}
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => {
			syncUrl();
			load();
			loadBudgetSummary();
		}, 300);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone, running syncUrl()/a list fetch against a route
		// the user already left.
		return () => clearTimeout(searchTimer);
	});

	$effect(() => {
		dimensionFilter;
		syncUrl();
		load();
		loadBudgetSummary();
	});

	$effect(() => {
		orgCurrency.ensureLoaded();
	});

	function onSaved(b: Budget) {
		fetchSequence.supersedeInFlight();
		const idx = budgets.findIndex((x) => x.id === b.id);
		if (idx >= 0) budgets = budgets.map((x) => (x.id === b.id ? b : x));
		else {
			budgets = [b, ...budgets];
			total += 1;
		}
		if (editing && editing.id === b.id) editing = b;
		// The allocation KPI is a server rollup, not a reduce over `budgets` —
		// re-fetch it so a create / amount edit is reflected.
		loadBudgetSummary();
	}

	async function deleteBudget(id: string) {
		try {
			await apiDeleteBudget(id);
			fetchSequence.supersedeInFlight();
			budgets = budgets.filter((b) => b.id !== id);
			total = Math.max(0, total - 1);
			loadBudgetSummary();
			toast(m('budgets.toast.deleted'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('budgets.toast.deleteFailed'), 'error');
		} finally {
			confirmDeleteId = null;
		}
	}

	function onWindowClick(e: MouseEvent) {
		const target = e.target as Element | null;
		if (!target?.closest('.row-action') && confirmDeleteId) confirmDeleteId = null;
	}

	function dimLabel(d: string): string {
		return BUDGET_DIMENSION_LABELS[d as BudgetDimension] ?? d;
	}
</script>

<svelte:window onclick={onWindowClick} />

<PageHeader title={m('budgets.title')}>
	{#snippet actions()}
		{#if canManage}
			<button class="btn-primary" onclick={() => (showCreate = true)}>{m('budgets.action.new')}</button>
		{/if}
	{/snippet}

	<div class="kpi-row">
		<KpiCard
			value={allocatedTotal}
			sub={allocatedRest}
			label={m('budgets.kpi.totalAllocated')}
		/>
		<KpiCard value={total} label={m('budgets.kpi.budgets')} />
		<KpiCard value={DIMENSION_CHIPS.length - 1} label={m('budgets.kpi.dimensions')} />
	</div>

	<div class="filter-row">
		<SearchBox bind:value={search} placeholder={m('budgets.search.placeholder')} ariaLabel={m('budgets.search.aria')} />
		<FilterChips chips={DIMENSION_CHIPS} bind:active={dimensionFilter} />
	</div>

	<DataTable
		columns={COLUMNS}
		isEmpty={visibleBudgets.length === 0}
		empty={loading ? m('common.loading') : m('budgets.empty')}
	>
		{#snippet body()}
			{#each visibleBudgets as b (b.id)}
				<tr class="clickable" onclick={(e) => { if (isRowOpenClick(e)) editing = b; }}>
					<td>
						<RowLink onclick={() => (editing = b)} ariaLabel={m('budgets.row.open', { name: b.name })}>
							{b.name}
						</RowLink>
					</td>
					<td>{dimLabel(b.dimension)}</td>
					<td>{b.dimension_value}</td>
					<td class="muted">{b.period ?? '—'}</td>
					<td class="right mono"><Money amount={b.amount} currency={b.currency} /></td>
					<td class="actions">
						{#if canManage}
							<RowAction
								variant="danger"
								armed={confirmDeleteId === b.id}
								onclick={(e) => {
									e.stopPropagation();
									if (confirmDeleteId === b.id) deleteBudget(b.id);
									else confirmDeleteId = b.id;
								}}
							>
								{confirmDeleteId === b.id ? m('budgets.row.confirm') : m('budgets.row.delete')}
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
				class="btn-load-more"
				onclick={() => load({ append: true })}
				disabled={loadingMore}
			>
				{loadingMore
					? m('common.loading')
					: m('budgets.loadMore', { shown: budgets.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('budgets.showingAll', { total })}</span>
		</div>
	{/if}
</PageHeader>

{#if showCreate}
	<BudgetModal budget={null} onclose={() => (showCreate = false)} onsaved={onSaved} />
{/if}

{#if editing}
	<BudgetModal budget={editing} onclose={() => (editing = null)} onsaved={onSaved} />
{/if}

<style>
	.filter-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}
</style>
