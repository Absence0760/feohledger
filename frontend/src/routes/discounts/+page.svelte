<script lang="ts">
	import { goto } from '$app/navigation';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { hasPartialRealisedSet, partialRealisedCount } from '#lib/utils/discountPartialSet.ts';
	import {
		formatAmountWithoutCurrency,
		recommendationCurrency
	} from '#lib/utils/discountRecommendation.ts';
	import { normalizeMoneyInput } from '#lib/utils/moneyInput.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { orgCurrency } from '#lib/stores/orgSettings.svelte.ts';
	import { formatMoney, scaleMoney } from '#lib/utils/money.ts';
	import type { MoneyAmount, MoneyString } from '#lib/utils/money.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import DiscountTierBar from '#lib/components/ui/DiscountTierBar.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import BulkNegotiationModal from '#lib/components/modals/BulkNegotiationModal.svelte';
	import {
		getDiscountDashboard,
		listDiscountOffers,
		acceptDiscountOffer,
		declineDiscountOffer,
		optimizeDiscounts
	} from '#lib/api/discounts.ts';
	import { DISCOUNT_STATUS_TONES } from '#lib/types/discounts.ts';
	import type {
		DiscountDashboard,
		DiscountOffer,
		DiscountTier,
		DiscountStatus,
		DiscountStatusFilter,
		DiscountOptimization
	} from '#lib/types/discounts.ts';

	// RBAC mirrors `backend/app/api/discounts.py`, which is the layer that
	// actually enforces:
	//   READ   (`_READ_ROLES`)   = admin / ap_manager / ap_clerk / cfo — the
	//          dashboard, the offer list, per-invoice ROI, AND `POST /optimize`
	//          (a POST only because it takes a body; it computes and mutates
	//          nothing, so it is read-gated).
	//   DECIDE (`_ACCEPT_ROLES`) = admin / ap_manager / cfo — accept + decline.
	//          `isManager` = admin|ap_manager, `isCfo` = admin|cfo, so their
	//          union is exactly that trio.
	// This page used to redirect a clerk on a comment claiming "the backend
	// 403s everyone else" — it does not, and the clerk got a dead end on a
	// surface they are entitled to read. Wait for `auth.user` to load before
	// deciding — a redirect before /me resolves would race first paint (the
	// audit page documents the same gotcha).
	const userLoaded = $derived(auth.user !== null);
	const canRead = $derived(auth.hasAnyRole('admin', 'ap_manager', 'ap_clerk', 'cfo'));
	const canDecide = $derived(auth.isManager || auth.isCfo);
	// PROPOSE (`_WRITE_ROLES`) = admin / ap_manager — narrower than `canDecide`.
	// A CFO may accept or decline an offer a supplier put on the table, but
	// putting one TO a supplier is an AP operation, so `POST /bulk-negotiate`
	// 403s them. Showing the trigger to a CFO would be the same dead end this
	// page already removed for clerks, one role over.
	const canNegotiate = $derived(auth.isManager);

	$effect(() => {
		if (userLoaded && !canRead) goto('/');
	});

	const PAGE_SIZE = 20;

	const OFFER_COLUMNS = $derived([
		{ label: m('discounts.col.vendorInvoice') },
		{ label: m('discounts.col.scope') },
		{ label: m('discounts.col.tiers') },
		{ label: m('discounts.col.base'), class: 'right' },
		{ label: m('discounts.col.bestDiscount'), class: 'right' },
		{ label: m('discounts.col.status') },
		{ label: m('discounts.col.validUntil') },
		{ class: 'actions-col' }
	]);

	const STATUS_LABELS = $derived<Record<DiscountStatus, string>>({
		offered: m('discounts.status.offered'),
		accepted: m('discounts.status.accepted'),
		captured: m('discounts.status.captured'),
		declined: m('discounts.status.declined'),
		expired: m('discounts.status.expired')
	});

	let dashboard = $state<DiscountDashboard | null>(null);
	let offers = $state<DiscountOffer[]>([]);
	let total = $state(0);
	let page = $state(1);
	let statusFilter = $state<DiscountStatusFilter>('all');

	let loading = $state(true);
	let loadingMore = $state(false);
	let error = $state<string | null>(null);

	// The KPI row's own load state, separate from `loading` (which tracks the
	// OFFERS list — a different request that resolves on its own schedule).
	// Only ever cleared, never re-raised: a refresh after accept/decline keeps
	// the figures already on screen rather than blanking a row the reader is
	// looking at, and a dashboard whose request FAILED is unavailable, not
	// pending — the error banner below says why, and a card that went on
	// claiming to load would never settle.
	let dashboardPending = $state(true);

	let hasMore = $derived(offers.length < total);

	// Aggregate (tenant-wide) money is labelled with the currency the RESPONSE
	// says it is denominated in — `dashboard.currency` / `optimization.currency`
	// — never the org-default store. Both responses state it precisely because
	// the totals are sums across offers that carry their own currencies, and the
	// page already renders that same field in the guard text two lines away; a
	// tenant whose reporting currency had moved read one figure under two codes.
	// `orgCurrency` is the fallback only for a response that omits the field.
	//
	// The absent amount is NOT coerced to 0. `n ?? 0` used to make every money
	// KPI render a formatted zero for the whole load window — a figure nobody
	// had computed, shown as a computed one (`docs/decisions.md` §34), and the
	// most actionable wrong answer available ("we captured nothing"). Every
	// caller now holds a landed response, and `formatMoney` falls back to the
	// same em dash `KpiCard` shows if one ever doesn't.
	function aggMoney(
		n: MoneyAmount,
		currency: string | null | undefined
	): string {
		return formatMoney(n, { currency: currency || orgCurrency.currency, whole: true });
	}

	/** Friendly relative "in 3 days" / "5 days ago" for a deadline. */
	function relativeDeadline(dateStr: string | null): string {
		if (!dateStr) return '';
		const d = new Date(dateStr);
		if (Number.isNaN(d.getTime())) return '';
		const days = Math.round((d.getTime() - Date.now()) / 86_400_000);
		if (days === 0) return m('discounts.deadline.today');
		if (days > 0) return m('discounts.deadline.inDays', { n: days });
		return m('discounts.deadline.daysAgo', { n: -days });
	}

	function bestTier(o: DiscountOffer): DiscountTier | null {
		if (o.tiers.length === 0) return null;
		return o.tiers.reduce((best, t) => (t.percent > best.percent ? t : best));
	}

	function bestDiscountAmount(o: DiscountOffer): MoneyString | null {
		const t = bestTier(o);
		if (!t) return null;
		// Exact: one HALF_UP rounding of base * percent / 100, never a float
		// multiply followed by a float divide.
		return scaleMoney(o.base_amount, t.percent, { divideBy: 100 });
	}

	const STATUS_CHIPS = $derived<{ key: DiscountStatusFilter; label: string }[]>([
		{ key: 'all', label: m('common.all') },
		{ key: 'offered', label: m('discounts.status.offered') },
		{ key: 'accepted', label: m('discounts.status.accepted') },
		{ key: 'captured', label: m('discounts.status.captured') },
		{ key: 'missed', label: m('discounts.chip.missed') }
	]);

	// Two sequencers: the offers list and the KPI dashboard are separate
	// requests, and each accept/decline re-fires the dashboard, so two of those
	// can resolve out of order. Sharing one counter would let a dashboard
	// refresh mark an in-flight offers load un-committable and blank the table.
	// `confirmAccept`/`decline` edit a row in place with no fetch, so they
	// supersede the offers sequencer first. See `frontend/CLAUDE.md`
	// § Sequencing list fetches.
	const offersSequence = createRequestSequencer();
	const dashboardSequence = createRequestSequencer();

	async function loadDashboard() {
		const token = dashboardSequence.start();
		try {
			const data = await getDiscountDashboard();
			if (!dashboardSequence.canCommit(token)) return;
			dashboard = data;
			dashboardPending = false;
		} catch (e) {
			// The KPI row is best-effort — a failure here shouldn't blank the table.
			if (!dashboardSequence.isCurrentRequest(token)) return;
			dashboard = null;
			dashboardPending = false;
			if (!error) error = e instanceof Error ? e.message : m('discounts.error.dashboard');
		}
	}

	async function loadOffers(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? page + 1 : 1;
		const token = offersSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		try {
			const data = await listDiscountOffers({
				status: statusFilter,
				page: nextPage,
				pageSize: PAGE_SIZE
			});
			// Superseded by a newer load, or by a local accept/decline.
			if (!offersSequence.canCommit(token)) return;
			offers = opts.append ? appendUnique(offers, data.items) : data.items;
			total = data.total;
			page = nextPage;
			error = null;
		} catch (e) {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// edit still failed, and no newer load is coming to report it.
			if (!offersSequence.isCurrentRequest(token)) return;
			error = e instanceof Error ? e.message : m('discounts.error.offers');
			if (!opts.append) offers = [];
		} finally {
			if (offersSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	$effect(() => {
		if (!canRead) return;
		orgCurrency.ensureLoaded();
		loadDashboard();
	});

	$effect(() => {
		if (!canRead) return;
		// Re-fetch the offers whenever the status filter changes.
		statusFilter;
		loadOffers();
	});

	// --- Accept modal ---
	let acceptTarget = $state<DiscountOffer | null>(null);
	let selectedTierDays = $state<number | null>(null);
	let accepting = $state(false);

	function openAccept(o: DiscountOffer) {
		acceptTarget = o;
		// Default to the richest tier.
		selectedTierDays = bestTier(o)?.days ?? null;
	}

	async function confirmAccept() {
		if (!acceptTarget) return;
		accepting = true;
		try {
			const updated = await acceptDiscountOffer(
				acceptTarget.id,
				selectedTierDays ?? undefined
			);
			// A load already in flight read this offer BEFORE the accept landed,
			// so its response would revert the row to `offered`. Retire it.
			offersSequence.supersedeInFlight();
			offers = offers.map((o) => (o.id === updated.id ? updated : o));
			toast(m('discounts.toast.accepted'), 'success');
			acceptTarget = null;
			await loadDashboard();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('discounts.toast.acceptFailed'), 'error');
		} finally {
			accepting = false;
		}
	}

	// --- Decline (armed two-click) ---
	let confirmDeclineId = $state<string | null>(null);
	let declining = $state(false);

	async function decline(o: DiscountOffer) {
		if (confirmDeclineId !== o.id) {
			confirmDeclineId = o.id;
			return;
		}
		declining = true;
		try {
			const updated = await declineDiscountOffer(o.id);
			offersSequence.supersedeInFlight();
			offers = offers.map((x) => (x.id === updated.id ? updated : x));
			toast(m('discounts.toast.declined'), 'success');
			confirmDeclineId = null;
			await loadDashboard();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('discounts.toast.declineFailed'), 'error');
		} finally {
			declining = false;
		}
	}

	// --- Optimize panel ---
	let cashBudget = $state('');
	let optimizing = $state(false);
	let optimization = $state<DiscountOptimization | null>(null);
	// `?? []` because the field is optional: a backend predating it renders no
	// unrankable section rather than throwing on `undefined.length`.
	const unrankableRecs = $derived(optimization?.unrankable ?? []);

	async function runOptimize() {
		// The budget goes out as the EXACT STRING the user typed. It used to go
		// through `Number()`, and the backend's `json.loads` then handed the
		// optimizer a float — so the budget it selected against was the rounded
		// double, not what was typed. This budget decides which invoices get
		// paid early: a spend decision, not a display value (root `CLAUDE.md`
		// § Project invariants). `POST /optimize` now refuses a JSON number.
		const typed = cashBudget.trim();
		const budget = normalizeMoneyInput(typed);
		// A blank field legitimately means "no budget"; text we could not read
		// does NOT — running unconstrained on a typo would commit more cash than
		// was asked for, which is exactly what the server's `extra="forbid"`
		// closes on its side.
		if (typed && budget === null) {
			toast(m('discounts.opt.budgetInvalid'), 'error');
			return;
		}
		optimizing = true;
		try {
			optimization = await optimizeDiscounts(budget ?? undefined);
		} catch (e) {
			toast(e instanceof Error ? e.message : m('discounts.toast.optimizeFailed'), 'error');
		} finally {
			optimizing = false;
		}
	}

	// --- Vendor-wide negotiation (`POST /api/discounts/bulk-negotiate`) ---
	// No vendor list is fetched here. `ui/VendorPicker` inside the modal asks
	// the server for one page at a time and filters with `search=`, so the
	// proposal form reaches every supplier instead of the first 100 — and a
	// reader who never opens it pays for nothing.
	let showNegotiate = $state(false);

	function openNegotiate() {
		showNegotiate = true;
	}

	/** A proposal landed. Re-read both surfaces from the server rather than
	 *  splicing the row in locally: the new offer is `offered`, which the
	 *  current status filter may or may not include, and its `base_amount` is
	 *  server-computed — the list and the KPI row should agree with it, not with
	 *  a client-side guess at where it belongs. */
	async function onNegotiated() {
		await Promise.all([loadOffers(), loadDashboard()]);
	}
</script>

<svelte:window
	onclick={(e) => {
		// Un-arm the decline confirm when the click lands outside a row action.
		if (confirmDeclineId && !(e.target as HTMLElement)?.closest?.('.row-action')) {
			confirmDeclineId = null;
		}
	}}
/>

<PageHeader title={m('discounts.title')}>
	{#snippet actions()}
		<!-- Gated on `_WRITE_ROLES` (admin / ap_manager), the roles the backend
		     actually allows to propose. A clerk and a CFO both read this page;
		     neither may create a vendor-wide offer. -->
		{#if userLoaded && canRead && canNegotiate}
			<button class="btn-primary" type="button" onclick={openNegotiate}>
				{m('discounts.bulk.open')}
			</button>
		{/if}
	{/snippet}

	{#if !userLoaded}
		<p class="loading">{m('common.loading')}</p>
	{:else if !canRead}
		<p class="loading">{m('discounts.redirecting')}</p>
	{:else}
		<!-- KPI row.

		     Every card passes `null` rather than a figure until the dashboard
		     response lands, so all five render the shared "no figure" dash
		     (`#lib/utils/kpiValue`) instead of a formatted zero. Round 26 moved
		     the capture-rate card off the zero-while-loading pattern on its own,
		     leaving the row saying two contradictory things at once; this is
		     that decision applied to the whole row and pushed down into
		     `KpiCard`, so the choice is made once and every page inherits it
		     (`docs/decisions.md` §34 — a figure nobody computed must not be
		     displayed as a computed one).

		     `pending` is what separates "still arriving" from "did not arrive":
		     it draws nothing extra, it only makes the card announce itself as
		     busy so the dash is never read out as a value.

		     The two counted labels drop their parenthetical while the figure is
		     missing — "Captured (0)" is the same unearned zero as the amount
		     above it. The count-free wording is the status/chip label for the
		     same word, translated in all six locales, so this needs no key of
		     its own and cannot fall out of sync with the KPI it sits under. -->
		<div class="kpi-row">
			<KpiCard
				value={dashboard ? aggMoney(dashboard.captured_amount, dashboard.currency) : null}
				label={dashboard
					? m('discounts.kpi.captured', { n: dashboard.captured_count })
					: m('discounts.status.captured')}
				highlight="green"
				pending={dashboardPending}
			/>
			<KpiCard
				value={dashboard ? aggMoney(dashboard.missed_amount, dashboard.currency) : null}
				label={dashboard
					? m('discounts.kpi.missed', { n: dashboard.missed_count })
					: m('discounts.chip.missed')}
				highlight="red"
				pending={dashboardPending}
			/>
			<!-- `—`, never `0%`, until an offer has actually been captured or
			     missed. A capture rate is a ratio over the DECIDED population,
			     and a tenant whose offers are all still open has decided
			     nothing — `0%` reported that as "we captured none of them", the
			     opposite fact and the one that reads as a failing programme.
			     Same "—" + reason treatment the CFO cash-conversion-cycle and
			     fraud-rate cards already use, and the same empty state the
			     sibling figure on `/` shows (`docs/decisions.md` §34).

			     The dash is no longer written here: `null` means "no figure" and
			     `KpiCard` owns the glyph, so this card and the four beside it
			     cannot drift apart. The reason line is still withheld during the
			     load window — "no offer has been captured or missed yet" is a
			     claim we cannot make before the response lands. -->
			<KpiCard
				value={dashboard && !dashboard.insufficient_data
					? `${(dashboard.capture_rate_pct ?? 0).toFixed(0)}%`
					: null}
				label={m('discounts.kpi.captureRate')}
				sub={dashboard?.insufficient_data ? m('discounts.kpi.captureRateUnknown') : null}
				pending={dashboardPending}
			/>
			<KpiCard
				value={dashboard ? aggMoney(dashboard.projected_savings, dashboard.currency) : null}
				label={m('discounts.kpi.projectedSavings')}
				highlight="green"
				pending={dashboardPending}
			/>
			<KpiCard
				value={dashboard?.open_offer_count ?? null}
				label={m('discounts.kpi.openOffers')}
				pending={dashboardPending}
			/>
		</div>

		<!-- The dashboard half of the currency guard. `projected_savings` sums
		     ONLY offers denominated in the reporting currency, so a non-zero
		     count means the headline figure is lower than the offers listed
		     below imply — honest, but silently so. Mirrors the /cfo cash-position
		     card's unconverted-outflows notice. -->
		{#if dashboard && dashboard.unconvertible_offer_count > 0}
			<p class="disc-skipped" role="alert" data-testid="unconvertible-offers">
				{m('discounts.unconvertibleOffers', {
					n: dashboard.unconvertible_offer_count,
					currency: dashboard.currency
				})}
			</p>
		{/if}

		<!-- The same honesty for the two REALISED figures. `captured_amount` and
		     `missed_amount` count only offers denominated in the reporting
		     currency — amounts in different currencies are not added together —
		     so a non-zero count means the green and red KPIs above describe part
		     of the set. Without this the page shows a partial figure as if it
		     were the whole one, which is the defect the backend fix removed.
		     The rule lives in `utils/discountPartialSet.ts` so it has one owner
		     and can be unit-tested without the test restating its own sum. -->
		{#if dashboard && hasPartialRealisedSet(dashboard)}
			<p class="disc-skipped" role="alert" data-testid="excluded-realised">
				{m('discounts.excludedRealised', {
					n: partialRealisedCount(dashboard),
					currency: dashboard.currency
				})}
			</p>
		{/if}

		<!-- Optimize panel -->
		<div class="opt-panel">
			<div class="opt-head">
				<div>
					<h2>{m('discounts.opt.heading')}</h2>
					<p class="opt-sub">{m('discounts.opt.sub')}</p>
				</div>
				<form class="opt-form" onsubmit={(e) => { e.preventDefault(); runOptimize(); }}>
					<label class="opt-field">
						<span class="opt-label">{m('discounts.opt.budgetLabel')}</span>
						<input
							class="opt-input"
							type="text"
							inputmode="decimal"
							placeholder={m('discounts.opt.budgetPlaceholder')}
							bind:value={cashBudget}
							aria-label={m('discounts.opt.budgetAria')}
						/>
					</label>
					<button class="btn-primary" type="submit" disabled={optimizing}>
						{optimizing ? m('discounts.opt.optimizing') : m('discounts.opt.optimize')}
					</button>
				</form>
			</div>

			{#if optimization}
				<div class="opt-summary">
					<span>{m('discounts.opt.availableSavings')} <strong>{aggMoney(optimization.total_savings_available, optimization.currency)}</strong></span>
					<span>{m('discounts.opt.selectedSavings')} <strong class="pos">{aggMoney(optimization.total_savings_selected, optimization.currency)}</strong></span>
					<span>{m('discounts.opt.selectedOutlay')} <strong>{aggMoney(optimization.total_outlay_selected, optimization.currency)}</strong></span>
					<span>{m('discounts.opt.costOfCapital')} <strong>{optimization.cost_of_capital_pct.toFixed(1)}%</strong></span>
				</div>
				<!-- The optimizer half of the same guard: an offer in another
				     currency is excluded from all three totals above (and from
				     selection once a cash budget binds), so it ranks below as a
				     recommendation whose money is nowhere in the summary. -->
				{#if optimization.unconvertible_count > 0}
					<p class="disc-skipped" role="alert" data-testid="unconvertible-optimizer">
						{m('discounts.unconvertibleOffers', {
							n: optimization.unconvertible_count,
							currency: optimization.currency
						})}
					</p>
				{/if}
				{#if optimization.recommendations.length > 0}
					<div class="scenario-grid">
						{#each optimization.recommendations as rec (rec.offer_id)}
							<!-- `roi.savings` is computed from the OFFER's own base amount, so
							     it is in the OFFER's currency — which the row now states.
							     `unconvertible` keeps its separate job: this row's money is not
							     in the totals' currency and so is not counted in them. A payload
							     predating the per-row code still degrades to a bare figure. See
							     `utils/discountRecommendation.ts`. -->
							{@const recCurrency = recommendationCurrency(rec, optimization.currency)}
							<div class="scenario-card" class:best={rec.selected}>
								<span class="scenario-title">
									{rec.vendor_name ?? rec.invoice_number ?? rec.offer_id.slice(0, 8)}
								</span>
								<!-- `worthwhile` is TRI-state: `null` means the optimizer could
								     not rank this offer, which is not the same answer as
								     "ranked, and it loses". Compare to `true` explicitly so a
								     null can never tint the row as a decided verdict. -->
								<span class="scenario-roi" class:pos={rec.roi.worthwhile === true}>
									{rec.roi.annualized_return_pct === null
										? m('discounts.opt.aprUnknown')
										: `${rec.roi.annualized_return_pct.toFixed(1)}% APR`}
								</span>
								<span class="scenario-sub">
									{m('discounts.opt.save')}
									{#if recCurrency}
										<Money amount={rec.roi.savings} currency={recCurrency} />
									{:else}
										<span class="money" data-testid="rec-savings-unlabelled"
											>{formatAmountWithoutCurrency(rec.roi.savings)}</span
										>
									{/if}
									· {rec.discount_percent}% / {rec.tier_days}d
								</span>
								<!-- The banner above reports how MANY rows are excluded; this
								     says which one you are looking at. Without it the card
								     showed a figure the totals do not contain, in a currency
								     the page had guessed. -->
								{#if rec.unconvertible}
									<span class="scenario-warn" data-testid="rec-unconvertible">
										{m('discounts.opt.recUnconvertible', { currency: optimization.currency })}
									</span>
								{/if}
								<span class="scenario-sub">{m('discounts.opt.payBy', { date: formatDate(rec.pay_by) })}</span>
								<span class="scenario-flag" class:selected={rec.selected}>
									{rec.selected ? m('discounts.opt.selected') : m('discounts.opt.notSelected')}
								</span>
							</div>
						{/each}
					</div>
				{:else}
					<p class="empty">{m('discounts.opt.noOffers')}</p>
				{/if}
				<!-- Offers the optimizer could not RANK. The server withholds the APR
				     for an offer with no net due date rather than reporting a
				     fabricated 0.00 %, and carries them here rather than sorting them
				     to the bottom of a ranking BY APR. Rendering the array is the
				     whole point: before this they were simply ABSENT from the page —
				     a real, capturable saving that no longer appeared anywhere, which
				     is a worse failure than the wrong number it replaced. -->
				{#if unrankableRecs.length > 0}
					<div class="unrankable-block" data-testid="optimizer-unrankable">
						<h4 class="unrankable-heading">{m('discounts.opt.unrankableHeading')}</h4>
						<p class="disc-skipped">
							{m('discounts.opt.unrankableNote', { n: unrankableRecs.length })}
						</p>
						<div class="scenario-grid">
							{#each unrankableRecs as rec (rec.offer_id)}
								{@const recCurrency = recommendationCurrency(rec, optimization.currency)}
								<div class="scenario-card">
									<span class="scenario-title">
										{rec.vendor_name ?? rec.invoice_number ?? rec.offer_id.slice(0, 8)}
									</span>
									<!-- No `pos` tint: there is no verdict to be positive about. -->
									<span class="scenario-roi">{m('discounts.opt.aprUnknown')}</span>
									<span class="scenario-sub">
										{m('discounts.opt.save')}
										{#if recCurrency}
											<Money amount={rec.roi.savings} currency={recCurrency} />
										{:else}
											<span class="money">{formatAmountWithoutCurrency(rec.roi.savings)}</span>
										{/if}
										· {rec.discount_percent}% / {rec.tier_days}d
									</span>
									<span class="scenario-flag">{m('discounts.opt.notRanked')}</span>
								</div>
							{/each}
						</div>
					</div>
				{/if}
			{/if}
		</div>

		<!-- Status filter -->
		<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} />

		{#if error}
			<p class="dash-error" role="alert">{error}</p>
		{/if}

		<DataTable
			columns={OFFER_COLUMNS}
			isEmpty={!loading && offers.length === 0}
			empty={loading ? m('discounts.table.loading') : m('discounts.table.empty')}
			colspan={8}
		>
			{#snippet body()}
				{#each offers as offer (offer.id)}
					{@const best = bestTier(offer)}
					{@const bestAmt = bestDiscountAmount(offer)}
					<tr>
						<td>
							<span class="primary">{offer.vendor_name ?? '—'}</span>
							{#if offer.invoice_number}
								<span class="secondary mono">{offer.invoice_number}</span>
							{/if}
						</td>
						<td><span class="scope-pill">{offer.scope}</span></td>
						<td>
							<DiscountTierBar
								tiers={offer.tiers}
								acceptedDays={offer.accepted_tier?.days ?? null}
							/>
						</td>
						<td class="right"><Money amount={offer.base_amount} currency={offer.currency} mono /></td>
						<td class="right">
							{#if bestAmt !== null && best}
								<span class="best-discount">
									<Money amount={bestAmt} currency={offer.currency} mono />
									<span class="best-pct">{best.percent}%</span>
								</span>
							{:else}
								<span class="muted">—</span>
							{/if}
						</td>
						<td>
							<Badge tone={DISCOUNT_STATUS_TONES[offer.status]} variant="status-badge {offer.status}">
								{STATUS_LABELS[offer.status]}
							</Badge>
						</td>
						<td class="muted">
							{formatDate(offer.valid_until)}
							{#if offer.valid_until && offer.status === 'offered'}
								<span class="rel">{relativeDeadline(offer.valid_until)}</span>
							{/if}
						</td>
						<td class="actions">
							<!-- Deciding an offer is `_ACCEPT_ROLES` (admin / ap_manager /
							     cfo). A clerk reads this page; showing them buttons the
							     API would 403 is the dead end in the other direction. -->
							{#if offer.status === 'offered' && canDecide}
								<RowAction variant="success" onclick={() => openAccept(offer)} ariaLabel={m('discounts.row.acceptAria', { vendor: offer.vendor_name ?? offer.id.slice(0, 8) })}>
									{m('discounts.row.accept')}
								</RowAction>
								<RowAction
									variant="danger"
									armed={confirmDeclineId === offer.id}
									disabled={declining}
									onclick={() => decline(offer)}
									ariaLabel={m('discounts.row.declineAria', { vendor: offer.vendor_name ?? offer.id.slice(0, 8) })}
								>
									{confirmDeclineId === offer.id ? m('discounts.row.confirm') : m('discounts.row.decline')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>

		{#if hasMore}
			<div class="load-more-row">
				<button class="btn-load-more" onclick={() => loadOffers({ append: true })} disabled={loadingMore}>
					{loadingMore ? m('common.loading') : m('discounts.loadMore', { shown: offers.length, total })}
				</button>
			</div>
		{:else if total > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('discounts.showingAll', { total })}</span>
			</div>
		{/if}
	{/if}
</PageHeader>

<!-- Accept-tier modal -->
<Modal
	open={acceptTarget !== null}
	ariaLabel={m('discounts.modal.aria')}
	title={m('discounts.modal.title')}
	width="sm"
	onclose={() => (acceptTarget = null)}
>
	{#if acceptTarget}
		<form onsubmit={(e) => { e.preventDefault(); confirmAccept(); }}>
			<p class="modal-hint">
				<strong>{acceptTarget.vendor_name ?? acceptTarget.id.slice(0, 8)}</strong>
				{#if acceptTarget.invoice_number}· {acceptTarget.invoice_number}{/if}
				· {m('discounts.modal.base')} <Money amount={acceptTarget.base_amount} currency={acceptTarget.currency} />
			</p>

			{#if acceptTarget.tiers.length > 0}
				<fieldset class="tier-picker">
					<legend>{m('discounts.modal.chooseTier')}</legend>
					{#each [...acceptTarget.tiers].sort((a, b) => a.days - b.days) as tier (tier.days)}
						<label class="tier-option">
							<input
								type="radio"
								name="tier"
								value={tier.days}
								checked={selectedTierDays === tier.days}
								onchange={() => (selectedTierDays = tier.days)}
							/>
							<span class="tier-option-label">
								{m('discounts.modal.tierOption', { percent: tier.percent, days: tier.days })}
								<span class="tier-option-amt">
									{m('discounts.modal.save')} <Money
										amount={scaleMoney(acceptTarget.base_amount, tier.percent, {
											divideBy: 100
										})}
										currency={acceptTarget.currency}
									/>
								</span>
							</span>
						</label>
					{/each}
				</fieldset>
			{:else}
				<p class="modal-hint">{m('discounts.modal.noTiers')}</p>
			{/if}

			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (acceptTarget = null)}>{m('common.cancel')}</button>
				<button type="submit" class="btn-primary" disabled={accepting}>
					{accepting ? m('discounts.modal.accepting') : m('discounts.modal.acceptOffer')}
				</button>
			</div>
		</form>
	{/if}
</Modal>

<!-- Vendor-wide negotiation. Mounted only while open so the form starts clean
     on every proposal — a stale vendor + tier set carried over from a previous
     one is the wrong default for an offer against a different supplier. -->
{#if showNegotiate}
	<BulkNegotiationModal onclose={() => (showNegotiate = false)} oncreated={onNegotiated} />
{/if}

<style>
	.loading,
	.empty {
		color: var(--text-muted);
		text-align: center;
		padding: 20px;
	}
	.dash-error {
		color: var(--danger);
		font-weight: 500;
	}
	/* Amber, not red: the totals are still usable — they just don't cover every
	   offer on screen. Same tone + weight as the /cfo cash-position card's
	   `.cf-skipped` notice, which reports the mirror-image currency gap. */
	.disc-skipped {
		font-size: 0.85rem;
		margin: 0;
		color: #d4940a;
		font-weight: 600;
	}

	/* --- Optimize panel --- */
	.opt-panel {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 20px;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}
	.opt-head {
		display: flex;
		justify-content: space-between;
		align-items: flex-end;
		gap: 18px;
		flex-wrap: wrap;
	}
	.opt-head h2 {
		font-size: 1rem;
		margin: 0 0 4px;
	}
	.opt-sub {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0;
		max-width: 52ch;
	}
	.opt-form {
		display: flex;
		align-items: flex-end;
		gap: 10px;
	}
	.opt-field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.opt-label {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}
	.opt-input {
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.9rem;
		width: 180px;
	}
	.opt-summary {
		display: flex;
		flex-wrap: wrap;
		gap: 18px;
		font-size: 0.85rem;
		color: var(--text-muted);
		padding-top: 4px;
		border-top: 1px solid var(--border);
	}
	.opt-summary strong {
		color: var(--text);
	}
	.opt-summary strong.pos {
		color: #1fa86a;
	}

	/* --- Optimizer recommendation cards (cfo scenario-grid style) --- */
	.scenario-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
		gap: 14px;
	}
	.scenario-card {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 16px;
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.scenario-card.best {
		border-color: #2faa6a;
		background: rgba(47, 170, 106, 0.06);
	}
	.scenario-title {
		font-size: 0.85rem;
		font-weight: 600;
	}
	.scenario-roi {
		font-size: 1.3rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
	}
	.scenario-roi.pos {
		color: #1fa86a;
	}
	.scenario-sub {
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.scenario-flag {
		margin-top: 4px;
		font-size: 0.72rem;
		font-weight: 600;
		color: var(--text-muted);
	}
	.scenario-flag.selected {
		color: #1fa86a;
	}

	/* The unrankable block sits BELOW the ranked grid and is visibly its own
	   section: these offers are not the tail of the ranking, they are outside
	   it. Colour comes from tokens only — no tint here, so nothing to pair. */
	.unrankable-block {
		margin-top: 18px;
		padding-top: 14px;
		border-top: 1px solid var(--border);
	}
	.unrankable-heading {
		margin: 0 0 6px;
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--text);
	}
	/* Amber, same tone as `.disc-skipped`: this row's money is real, it just
	   is not in the currency the totals above are stated in. */
	.scenario-warn {
		font-size: 0.72rem;
		font-weight: 600;
		color: #d4940a;
	}

	/* --- Offers table cells --- */
	.primary {
		display: block;
		font-weight: 500;
	}
	.secondary {
		display: block;
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.mono {
		font-variant-numeric: tabular-nums;
		font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
	}
	.muted {
		color: var(--text-muted);
	}
	.scope-pill {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 10px;
		background: var(--bg);
		font-size: 0.74rem;
		font-weight: 600;
		text-transform: capitalize;
		color: var(--text-muted);
	}
	.best-discount {
		display: inline-flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 1px;
	}
	.best-pct {
		font-size: 0.7rem;
		color: #1fa86a;
		font-weight: 600;
	}
	.rel {
		display: block;
		font-size: 0.72rem;
		color: var(--text-muted);
	}

	/* --- Accept modal tier picker --- */
	.tier-picker {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 12px;
		margin: 0 0 14px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.tier-picker legend {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		padding: 0 4px;
	}
	.tier-option {
		display: flex;
		align-items: center;
		gap: 8px;
		cursor: pointer;
		font-size: 0.88rem;
	}
	.tier-option input {
		accent-color: var(--accent);
	}
	.tier-option-label {
		display: flex;
		flex-direction: column;
	}
	.tier-option-amt {
		font-size: 0.78rem;
		color: #1fa86a;
		font-weight: 600;
	}
	.modal-hint {
		font-size: 0.85rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	@media (max-width: 768px) {
		.opt-head {
			flex-direction: column;
			align-items: stretch;
		}
		.opt-form {
			flex-direction: column;
			align-items: stretch;
		}
		.opt-input {
			width: 100%;
		}
	}
</style>
