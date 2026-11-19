<script lang="ts">
	import type { RecurringTemplate } from '#lib/types/recurring.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import {
		RECURRING_STATUSES,
		recurringStatusLabelKey,
		STATUS_TONES,
		cadenceLabelKey,
		skipReasonKey
	} from '#lib/types/recurring.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { ApiError } from '#lib/api.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import {
		listRecurring,
		getRecurringSummary,
		getRecurring,
		deleteRecurring,
		pauseRecurring,
		resumeRecurring,
		endRecurring,
		generateRecurringNow
	} from '#lib/api/recurring.ts';
	import type { RecurringTemplateSummary } from '#lib/types/recurring.ts';
	import { formatCurrencyTotals } from '#lib/utils/currencyGroups.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import ExplainedBadge from '#lib/components/ui/ExplainedBadge.svelte';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import SearchBox from '#lib/components/ui/SearchBox.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import RecurringModal from '#lib/components/modals/RecurringModal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { page } from '$app/state';
	import { replaceState } from '$app/navigation';
	import { untrack } from 'svelte';
	import { formatDate } from '#lib/utils/time.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { daysUntil } from './relativeRun.ts';

	const canCreate = $derived(auth.isManager);

	// Status and cadence are message keys, not English literals — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = (s: string) => {
		const key = recurringStatusLabelKey(s);
		return key ? m(key) : s;
	};
	const cadenceLabel = (c: string) => {
		const key = cadenceLabelKey(c);
		return key ? m(key) : c;
	};

	const STATUS_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		...RECURRING_STATUSES.map((s) => ({ key: s, label: statusLabel(s) }))
	]);

	const COLUMNS = $derived([
		{ label: m('recurring.col.name') },
		{ label: m('recurring.col.vendor') },
		{ label: m('recurring.col.amount'), class: 'right' },
		{ label: m('recurring.col.cadence') },
		{ label: m('recurring.col.nextRun') },
		{ label: m('recurring.col.generated'), class: 'right' },
		{ label: m('recurring.col.status') },
		{ class: 'actions-col' }
	]);

	const PAGE_SIZE = 20;

	// URL-backed filter state (mirrors the contracts page convention).
	let search = $state(page.url.searchParams.get('search') ?? '');
	let statusFilter = $state<string>(page.url.searchParams.get('status') ?? 'all');
	// `gl=off_chart` narrows to templates whose GL code the chart no longer
	// accepts — the ones generating uncoded invoices until someone re-codes them.
	let glFilter = $state<string>(
		page.url.searchParams.get('gl') === 'off_chart' ? 'off_chart' : 'all'
	);

	let templates = $state<RecurringTemplate[]>([]);
	let total = $state(0);
	let pageNum = $state(1);
	let loading = $state(true);
	let loadingMore = $state(false);
	// The latest list load failed. `LoadError` replaces the table: clearing the
	// rows left "No recurring templates match your filters." asserting an empty
	// list the page never actually received.
	let loadErrored = $state(false);

	let hasMore = $derived(templates.length < total);
	// "No templates match your filters" is false with no filter on: a fresh
	// tenant reads that it filtered something out.
	let noActiveFilters = $derived(
		!search.trim() && statusFilter === 'all' && glFilter === 'all'
	);

	// Modal state: null = create; a RecurringTemplate = detail/edit.
	let showCreate = $state(false);
	let editing = $state<RecurringTemplate | null>(null);

	// `untrack` on the `search` read: buildParams() is called from `load()`,
	// which the filter `$effect` below calls directly — and Svelte tracks reads
	// transitively through called functions, so a plain read here would make
	// that effect depend on `search` and re-fire it on every keystroke,
	// un-debounced, racing the dedicated 300ms timer (issue #168). `untrack`
	// still reads the CURRENT value, so the request carries the live search
	// term; it just stops the read registering as the caller's dependency.
	function buildParams() {
		const params: { status?: string; search?: string; gl_off_chart?: boolean } = {};
		if (statusFilter !== 'all') params.status = statusFilter;
		if (glFilter === 'off_chart') params.gl_off_chart = true;
		const currentSearch = untrack(() => search);
		if (currentSearch.trim()) params.search = currentSearch.trim();
		return params;
	}

	// Reflect the live filter state into the URL. EVERY read in here is
	// untracked, `page.url` included, because syncUrl() is a WRITER called
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
			const url = new URL(page.url.href);
			if (statusFilter !== 'all') url.searchParams.set('status', statusFilter);
			else url.searchParams.delete('status');
			if (search.trim()) url.searchParams.set('search', search.trim());
			else url.searchParams.delete('search');
			if (glFilter === 'off_chart') url.searchParams.set('gl', 'off_chart');
			else url.searchParams.delete('gl');
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	// Sequences `load` (filter change and load-more alike — one shared counter,
	// latest-issued wins). `upsert` edits the list in place with no fetch of its
	// own, so it retires whatever is in flight first: otherwise a pause/resume/
	// end — or a brand-new template, which needs no pre-existing row and so
	// races even the first load — is reverted by the load already out.
	// See `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		// The KPI rollup tracks the same filter state — refresh it alongside a
		// fresh (non-append) list load; its own sequencer guards ordering.
		if (!opts.append) void loadSummary();
		if (opts.append) loadingMore = true;
		else {
			loading = true;
			loadErrored = false;
		}
		try {
			const data = await listRecurring({ ...buildParams(), page: nextPage, page_size: PAGE_SIZE });
			// Superseded by a newer load, or by a local edit.
			if (!fetchSequence.canCommit(token)) return;
			templates = opts.append ? appendUnique(templates, data.items) : data.items;
			total = data.total;
			pageNum = nextPage;
		} catch (e) {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// edit still failed, and no newer load is coming to report it.
			if (!fetchSequence.isCurrentRequest(token)) return;
			// A failed Load-more keeps the rows already shown, so a toast is
			// enough; a failed list load is stated in place, with a Retry.
			if (opts.append) {
				toast(e instanceof Error ? e.message : m('recurring.toast.loadFailed'), 'error');
			} else {
				templates = [];
				total = 0;
				loadErrored = true;
			}
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	// `searchEffectRan` skips this effect's own mount-time run: a Svelte
	// `$effect` always fires once immediately regardless of whether its
	// tracked value actually changed, so without the guard this queued a
	// SECOND, redundant `load()` ~300ms after the statusFilter effect
	// below already loaded the page once. `load()` replaces `templates`
	// wholesale, so if a create/edit lands in that window, the delayed
	// duplicate can resolve afterward and silently clobber it with a
	// stale snapshot — same class of bug fixed in UsersPanel.svelte.
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
		}, 300);
		// Cancel a pending debounce on teardown: without it the timer fires
		// after the page is gone, running syncUrl()/a list fetch against a route
		// the user already left.
		return () => clearTimeout(searchTimer);
	});

	$effect(() => {
		statusFilter;
		glFilter;
		syncUrl();
		load();
	});

	// No vendor fetch here any more. These options ARE the set of valid choices,
	// so a truncated list is not a shorter list — it is a supplier the operator
	// cannot pick. Walking every page on mount was the previous answer, and it
	// cost one request per 100 suppliers on a page that may never open the
	// modal. `ui/VendorPicker` inside `RecurringModal` searches them server-side
	// instead, so a vendor on page 40 is one query away and nothing is fetched
	// until the form is opened.

	// Deep-link: `/recurring?id=<uuid>` opens that template's detail modal.
	let deepLinkLoaded = $state<string | null>(null);
	// The deep-linked template that could not be fetched for a reason other
	// than "it does not exist". Only a 404 may say "not found"; a 500 or an
	// offline backend is a failed load, stated in place with a Retry.
	let deepLinkFailedId = $state<string | null>(null);

	function openDeepLink(id: string) {
		deepLinkFailedId = null;
		getRecurring(id)
			.then((t) => {
				// Don't open a modal for a link the user has since left.
				if (page.url.searchParams.get('id') === id) editing = t;
			})
			.catch((err) => {
				if (page.url.searchParams.get('id') !== id) return;
				if (err instanceof ApiError && err.status === 404) {
					toast(m('recurring.notFound'), 'error');
				} else {
					deepLinkFailedId = id;
				}
			});
	}

	$effect(() => {
		const id = page.url.searchParams.get('id');
		if (!id) {
			untrack(() => (deepLinkFailedId = null));
			return;
		}
		if (untrack(() => deepLinkLoaded) === id) return;
		untrack(() => {
			deepLinkLoaded = id;
			openDeepLink(id);
		});
	});

	async function openDetail(t: RecurringTemplate) {
		try {
			editing = await getRecurring(t.id);
		} catch {
			editing = t;
		}
	}

	function closeModal() {
		editing = null;
		showCreate = false;
		const url = new URL(page.url.href);
		if (url.searchParams.has('id')) {
			url.searchParams.delete('id');
			replaceState(`${url.pathname}${url.search}`, {});
			deepLinkLoaded = null;
		}
	}

	function upsert(t: RecurringTemplate) {
		// Retire every load issued before this mutation landed — their responses
		// predate it and would revert the row (or drop a just-created one).
		fetchSequence.supersedeInFlight();
		const idx = templates.findIndex((x) => x.id === t.id);
		if (idx === -1) {
			templates = [t, ...templates];
			total += 1;
		} else {
			templates = templates.map((x) => (x.id === t.id ? t : x));
		}
	}

	function onSaved(t: RecurringTemplate) {
		upsert(t);
		// Keep the open detail modal in sync after a lifecycle action.
		if (editing && editing.id === t.id) editing = t;
		// A create / edit / pause / resume / end moved a status or an amount —
		// the KPI rollup is a server figure, so re-fetch it.
		void loadSummary();
	}

	// --- Row lifecycle actions ---
	let busyId = $state<string | null>(null);

	async function runRowLifecycle(
		t: RecurringTemplate,
		fn: () => Promise<RecurringTemplate>,
		successMsg: string,
		fallback: string
	) {
		busyId = t.id;
		try {
			const updated = await fn();
			toast(successMsg, 'success');
			onSaved(updated);
		} catch (e) {
			toast(e instanceof Error ? e.message : fallback, 'error');
		} finally {
			busyId = null;
		}
	}

	async function generateNow(t: RecurringTemplate) {
		busyId = t.id;
		try {
			await generateRecurringNow(t.id);
			toast(m('recurring.toast.generated'), 'success');
			// Refresh the row so generated_count / next_run_on stay accurate.
			try {
				onSaved(await getRecurring(t.id));
			} catch {
				/* the toast already confirmed success */
			}
		} catch (e) {
			toast(e instanceof Error ? e.message : m('recurring.toast.generateFailed'), 'error');
		} finally {
			busyId = null;
		}
	}

	// --- End (armed two-click confirm, like delete) ---
	let confirmEndId = $state<string | null>(null);

	async function endTemplate(t: RecurringTemplate) {
		if (confirmEndId !== t.id) {
			confirmEndId = t.id;
			return;
		}
		confirmEndId = null;
		await runRowLifecycle(
			t,
			() => endRecurring(t.id),
			m('recurring.toast.ended'),
			m('recurring.toast.endFailed')
		);
	}

	/** Why the last due period produced no invoice, in words. An unrecognised
	 * backend code renders verbatim rather than blank. */
	function skipReason(code: string): string {
		const key = skipReasonKey(code);
		return key ? m(key as MessageKey) : code;
	}

	/** Friendly relative "in 3 days" / "5 days ago" for the next run. */
	function relativeRun(s: string | null): string {
		if (!s) return '';
		const days = daysUntil(s);
		if (days === null) return '';
		if (days === 0) return m('recurring.rel.today');
		if (days > 0) return m('recurring.rel.inDays', { n: days });
		return m('recurring.rel.daysAgo', { n: -days });
	}

	// --- KPI rollup: `GET /api/recurring/summary` — the WHOLE filtered set, over
	// the SAME filters. `activeCount` / `soonestNextRun` / `monthlyRecurringTotal`
	// all used to derive from the loaded page, and the monthly total divided
	// floats (`amount / 3`, `amount / 12`). The server does the cadence
	// normalisation in exact decimal and groups the result by currency —
	// `formatCurrencyTotals` renders one subtotal per currency, never added. ---
	let recurringSummary = $state<RecurringTemplateSummary | null>(null);
	// The first rollup is still out — what `KpiCard pending` announces.
	let summaryPending = $state(true);
	const summarySequence = createRequestSequencer();

	async function loadSummary() {
		const token = summarySequence.start();
		try {
			const res = await getRecurringSummary(buildParams());
			if (!summarySequence.canCommit(token)) return;
			recurringSummary = res;
		} catch {
			if (summarySequence.isCurrentRequest(token)) recurringSummary = null;
		} finally {
			if (summarySequence.isCurrentRequest(token)) summaryPending = false;
		}
	}

	// Nullish until the rollup arrives (or when it failed), so `KpiCard` draws
	// its "no figure" dash: a `0` asserted "no active templates" before, or
	// without, the server ever saying so.
	const activeCount = $derived(recurringSummary ? (recurringSummary.by_status.active ?? 0) : null);
	const offChartCount = $derived(recurringSummary?.gl_off_chart ?? 0);
	// Offered only while there is something to find (or the filter is already
	// on, so it can always be turned back off).
	const showGlFilter = $derived(offChartCount > 0 || glFilter === 'off_chart');
	const GL_CHIPS = $derived([
		{ key: 'all', label: m('recurring.glFilter.all') },
		{
			key: 'off_chart',
			label: m('recurring.glFilter.offChart'),
			count: offChartCount,
			alert: offChartCount > 0
		}
	]);
	const soonestNextRun = $derived(recurringSummary?.soonest_next_run ?? null);
	const monthlyGroups = $derived(
		formatCurrencyTotals(recurringSummary?.monthly_equivalent ?? [])
	);
	const monthlyRecurringValue = $derived(monthlyGroups[0] ?? null);
	const monthlyRecurringRest = $derived(
		monthlyGroups.length > 1 ? monthlyGroups.slice(1).join(' · ') : null
	);
</script>

<svelte:window
	onclick={(e) => {
		// Un-arm the End confirm when the click lands outside a row action.
		if (confirmEndId && !(e.target as HTMLElement)?.closest?.('.row-action')) {
			confirmEndId = null;
		}
	}}
/>

<PageHeader title={m('recurring.title')}>
	{#snippet actions()}
		{#if canCreate}
			<button class="btn-primary" onclick={() => (showCreate = true)}>{m('recurring.action.new')}</button>
		{/if}
	{/snippet}

	<!-- KPI row -->
	<div class="kpi-row">
		<KpiCard
			value={activeCount}
			pending={summaryPending}
			label={m('recurring.kpi.activeTemplates')}
		/>
		<KpiCard
			value={recurringSummary ? (soonestNextRun ? formatDate(soonestNextRun) : '—') : null}
			pending={summaryPending}
			label={m('recurring.kpi.nextRun')}
		/>
		<KpiCard
			value={recurringSummary ? (monthlyRecurringValue ?? '—') : null}
			pending={summaryPending}
			sub={monthlyRecurringRest}
			label={m('recurring.kpi.monthlyRecurring')}
			highlight="green"
		/>
	</div>

	<div class="filter-row">
		<SearchBox bind:value={search} placeholder={m('recurring.search.placeholder')} ariaLabel={m('recurring.search.aria')} />
		<div class="help-row">
			<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} />
			{#if showGlFilter}
				<FilterChips chips={GL_CHIPS} bind:active={glFilter} />
			{/if}
			<HelpTip term="recurring-invoice" />
		</div>
	</div>

	{#if deepLinkFailedId}
		<LoadError
			message={m('recurring.deepLink.loadFailed')}
			onretry={() => deepLinkFailedId && openDeepLink(deepLinkFailedId)}
			testId="recurring-deeplink-error"
		/>
	{/if}

	{#if loadErrored}
		<LoadError
			message={m('recurring.toast.loadFailed')}
			onretry={() => load()}
			testId="recurring-load-error"
		/>
	{:else}
	<DataTable
		columns={COLUMNS}
		isEmpty={!loading && templates.length === 0}
		empty={loading
			? m('common.loading')
			: noActiveFilters
				? m('recurring.empty.fresh')
				: m('recurring.empty')}
		colspan={8}
	>
		{#snippet body()}
			{#each templates as template (template.id)}
				<tr
					class="clickable"
					onclick={(e) => {
						if (isRowOpenClick(e)) openDetail(template);
					}}
				>
					<td>
						<RowLink
							onclick={() => openDetail(template)}
							ariaLabel={m('recurring.row.open', { name: template.name })}
						>
							{template.name}
						</RowLink>
					</td>
					<td>{template.vendor_name ?? '—'}</td>
					<td class="right mono"><Money amount={template.amount} currency={template.currency} /></td>
					<td>
						<span class="cadence">{cadenceLabel(template.cadence)}</span>
						<span class="cadence-day">{m('recurring.dayOfPeriod', { day: template.day_of_period })}</span>
					</td>
					<td class="muted">
						{formatDate(template.next_run_on)}
						{#if template.next_run_on && template.status === 'active'}
							<span class="rel">{relativeRun(template.next_run_on)}</span>
						{/if}
					</td>
					<td class="right mono">{template.generated_count}</td>
					<td>
						<!-- The 6px that used to live on `.badge.skipped`'s own margin now
						     lives on this row: `<Badge>` owns colour and pill metrics, the
						     caller owns placement. -->
						<span class="badge-row">
							<Badge tone={STATUS_TONES[template.status]} variant={template.status}>
								{statusLabel(template.status)}
							</Badge>
							<!-- Each pill names the problem and carries its why as text — a
							     visually-hidden span, with the tooltip kept for the pointer
							     user — because a `title` alone reaches neither a keyboard, a
							     touch screen nor a screen reader (decisions §442, §455). -->
							{#if template.last_skip}
								<ExplainedBadge
									tone="warning"
									variant="skipped"
									label={m('recurring.skip.pill')}
									explanation={m('recurring.skip.title', {
										n: template.last_skip.consecutive,
										period: template.last_skip.period_key ?? '—',
										reason: skipReason(template.last_skip.reason)
									})}
								/>
							{/if}
							{#if template.gl_account_off_chart}
								<ExplainedBadge
									tone="warning"
									variant="gl-off-chart"
									label={m('recurring.glOffChart.pill', { code: template.gl_account ?? '—' })}
									explanation={m('recurring.glOffChart.title', { code: template.gl_account ?? '—' })}
								/>
							{/if}
						</span>
					</td>
					<td class="actions">
						{#if canCreate}
							{#if template.status === 'active'}
								<RowAction
									disabled={busyId === template.id}
									onclick={() => generateNow(template)}
									ariaLabel={m('recurring.row.generateNowAria', {
										action: m('recurring.row.generateNow'),
										name: template.name
									})}
								>
									{m('recurring.row.generateNow')}
								</RowAction>
								<RowAction
									disabled={busyId === template.id}
									onclick={() => runRowLifecycle(template, () => pauseRecurring(template.id), m('recurring.toast.paused'), m('recurring.toast.pauseFailed'))}
									ariaLabel={m('recurring.row.pauseAria', { name: template.name })}
								>
									{m('recurring.row.pause')}
								</RowAction>
							{:else if template.status === 'paused'}
								<RowAction
									variant="success"
									disabled={busyId === template.id}
									onclick={() => runRowLifecycle(template, () => resumeRecurring(template.id), m('recurring.toast.resumed'), m('recurring.toast.resumeFailed'))}
									ariaLabel={m('recurring.row.resumeAria', { name: template.name })}
								>
									{m('recurring.row.resume')}
								</RowAction>
							{/if}
							{#if template.status !== 'ended'}
								<RowAction
									variant="danger"
									armed={confirmEndId === template.id}
									disabled={busyId === template.id}
									onclick={() => endTemplate(template)}
									ariaLabel={m('recurring.row.endAria', { name: template.name })}
								>
									{confirmEndId === template.id ? m('recurring.row.confirm') : m('recurring.row.end')}
								</RowAction>
							{/if}
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>
	{/if}

	{#if hasMore}
		<div class="load-more-row">
			<button class="btn-load-more" onclick={() => load({ append: true })} disabled={loadingMore}>
				{loadingMore ? m('common.loading') : m('recurring.loadMore', { shown: templates.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('recurring.showingAll', { total })}</span>
		</div>
	{/if}
</PageHeader>

{#if showCreate}
	<RecurringModal template={null} onclose={closeModal} onsaved={onSaved} />
{/if}

{#if editing}
	<RecurringModal template={editing} onclose={closeModal} onsaved={onSaved} />
{/if}

<style>
	/* Page-specific bits; shared design-system CSS lives in app.css. */
	.filter-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	.mono {
		font-variant-numeric: tabular-nums;
		font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
	}

	.muted {
		color: var(--text-muted);
	}

	.cadence {
		font-weight: 500;
	}

	.cadence-day {
		display: block;
		font-size: 0.72rem;
		color: var(--text-muted);
	}

	.rel {
		display: block;
		font-size: 0.72rem;
		color: var(--text-muted);
	}

	/* Both pills are `<Badge>` now — this file and `RecurringModal` used to
	   tint the same three statuses at two different alphas. The tone per status
	   lives beside the labels in `types/recurring`; only the placement of the
	   two-pill row stays here. */
	.badge-row {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}

</style>
