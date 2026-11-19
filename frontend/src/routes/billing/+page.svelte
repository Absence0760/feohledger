<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import SubscriptionBadge from '#lib/components/ui/SubscriptionBadge.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import AiUsagePanel from '#lib/components/billing/AiUsagePanel.svelte';
	import type { BadgeTone } from '#lib/components/ui/badgeTone.ts';
	import {
		changeBillingPlan,
		getBillingInvoices,
		getBillingPaymentMethods,
		getBillingPlans,
		getBillingSubscription,
		startBillingSetupIntent
	} from '#lib/api/billing.ts';
	import { formatMoney } from '#lib/utils/money.ts';
	import { formatCount } from '#lib/utils/count.ts';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	// The published-address registry, not a literal typed here. All three
	// `mailto:`s below read `billing@example.com` — the IANA-reserved TLD, which
	// can never deliver — so every "contact us" on this page was a dead button.
	// That is the same defect the `sales` entry in `operator.ts` records for the
	// pricing page's Enterprise call-to-action, and `Pricing.svelte` already
	// sources its address this way. There is no `billing@` alias: routing to one
	// would re-create the bug in a subtler form, since an unprovisioned address
	// bounces exactly like a reserved one. Each button goes to whichever of the
	// five live aliases matches what the button's own label promises.
	import { CONTACT } from '#lib/legal/operator.ts';
	import type {
		BillingInvoice,
		BillingInvoiceStatus,
		BillingPaymentMethod,
		BillingPlan,
		BillingPlanChangeResponse,
		BillingSubscriptionResponse
	} from '#lib/types/billing.ts';
	import { rebateMeterGroups } from '#lib/types/billing.ts';
	import { formatDate, formatMonth } from '#lib/utils/time.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';
	import { PLAN_FEATURES, isPlanFeature, planFeatureLabelKey } from '#lib/types/planFeatures.ts';

	// RBAC: the backend gates `GET /api/billing/subscription` to admin / cfo and
	// 403s everyone else. `isAdmin` covers admin; `isCfo` = admin|cfo, so the
	// union is exactly {admin, cfo}. Wait for `auth.user` to resolve before
	// redirecting — bouncing before /me lands would race first paint (the
	// discounts + audit pages document the same gotcha).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin || auth.isCfo);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	let data = $state<BillingSubscriptionResponse | null>(null);
	// Rebate meters arrive one key per currency (`card_rebate_total.USD`), so the
	// figures are rendered per currency rather than added into one number that
	// would be denominated in nothing. Empty when the org accrued none.
	let rebateGroups = $derived(rebateMeterGroups(data?.usage));
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Invoices / receipts list — loaded independently so a slow / failed invoices
	// fetch never blocks the plan + usage surface (and vice-versa).
	let invoices = $state<BillingInvoice[]>([]);
	let invoicesLoading = $state(true);
	let invoicesError = $state<string | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			data = await getBillingSubscription();
		} catch (e) {
			error = errorMessage(e, m('billing.error.load'));
		} finally {
			loading = false;
		}
	}

	async function loadInvoices() {
		invoicesLoading = true;
		invoicesError = null;
		try {
			invoices = (await getBillingInvoices()).invoices;
		} catch (e) {
			invoicesError = errorMessage(e, m('billing.error.loadInvoices'));
		} finally {
			invoicesLoading = false;
		}
	}

	// Saved cards — loaded independently of the plan/usage/invoices blocks (mirrors
	// the invoices section), so a slow / failed payment-methods fetch never blocks
	// the rest of the surface. PII-safe metadata only (brand / ****last4 / exp) —
	// the backend never returns a PAN, and we never hold one in state.
	let paymentMethods = $state<BillingPaymentMethod[]>([]);
	let pmLoading = $state(true);
	let pmError = $state<string | null>(null);

	// Add / replace card flow. The deployed flow confirms `client_secret` against
	// the provider's JS SDK (Stripe Elements); the local-first stack has no real
	// Stripe and the frontend must not call secret-bearing services directly, so
	// the secret is surfaced as a clearly-marked "next step" seam, never used to
	// charge or to mount real Elements here.
	let addingCard = $state(false);
	let cardSetup = $state<{
		state: 'idle' | 'starting' | 'ready' | 'not_configured' | 'error';
		message: string | null;
	}>({ state: 'idle', message: null });

	async function loadPaymentMethods() {
		pmLoading = true;
		pmError = null;
		try {
			paymentMethods = (await getBillingPaymentMethods()).payment_methods;
		} catch (e) {
			pmError = errorMessage(e, m('billing.error.loadPaymentMethods'));
		} finally {
			pmLoading = false;
		}
	}

	async function startAddCard() {
		addingCard = true;
		cardSetup = { state: 'starting', message: null };
		try {
			const res = await startBillingSetupIntent();
			if (!res.configured || !res.client_secret) {
				// Org never provisioned with the provider, or the provider isn't
				// configured (the live adapter fails closed without a key). Not an
				// error — a clear "billing not configured" affordance.
				cardSetup = {
					state: 'not_configured',
					message: m('billing.card.notConfigured')
				};
				return;
			}
			// A real SetupIntent secret came back. Mounting the provider's card form
			// (Stripe Elements) is a deployed-only piece — see the placeholder below.
			// Re-list cards so a card attached out-of-band shows up.
			cardSetup = {
				state: 'ready',
				message: m('billing.card.ready')
			};
			await loadPaymentMethods();
		} catch (e) {
			cardSetup = {
				state: 'error',
				message: errorMessage(e, m('billing.card.startError'))
			};
		} finally {
			addingCard = false;
		}
	}

	function closeAddCard() {
		cardSetup = { state: 'idle', message: null };
	}

	// Plan-change flow. `POST /api/billing/change-plan` APPLIES the change on
	// the spot — there is no preview-only mode on the backend — so the modal
	// says so before the confirm click, then shows the real proration the
	// response returns. Idempotent: re-"changing" to the current plan comes
	// back `changed: false` with a zero proration, rendered as a clean no-op
	// rather than an error.
	let showChangePlan = $state(false);
	let availablePlans = $state<BillingPlan[]>([]);
	let plansLoading = $state(false);
	let plansError = $state<string | null>(null);
	let selectedPlanCode = $state<string | null>(null);
	let changingPlan = $state(false);
	let changeError = $state<string | null>(null);
	let changeResult = $state<BillingPlanChangeResponse | null>(null);

	async function loadAvailablePlans() {
		plansLoading = true;
		plansError = null;
		try {
			availablePlans = (await getBillingPlans()).plans;
		} catch (e) {
			plansError = errorMessage(e, m('billing.plan.changeModal.loadError'));
		} finally {
			plansLoading = false;
		}
	}

	function openChangePlan() {
		showChangePlan = true;
		selectedPlanCode = null;
		changeError = null;
		changeResult = null;
		loadAvailablePlans();
	}

	function closeChangePlan() {
		showChangePlan = false;
	}

	async function confirmPlanChange() {
		if (!selectedPlanCode) return;
		changingPlan = true;
		changeError = null;
		try {
			changeResult = await changeBillingPlan(selectedPlanCode);
			// Refresh the plan/usage surface behind the modal so it reflects the
			// new plan by the time the user closes the dialog.
			await load();
		} catch (e) {
			changeError = errorMessage(e, m('billing.plan.changeModal.changeFailed'));
		} finally {
			changingPlan = false;
		}
	}

	/** Display name for a plan code the picker already fetched, falling back to
	 *  the bare code if the list has since moved on (defensive, not expected). */
	function planName(code: string): string {
		return availablePlans.find((p) => p.code === code)?.name ?? code;
	}

	$effect(() => {
		// Only fetch once we know the role is allowed (avoids a guaranteed 403
		// for the clerk/manager before the redirect fires).
		if (userLoaded && allowed) {
			load();
			loadInvoices();
			loadPaymentMethods();
		}
	});

	/** Pretty "Visa ····4242" label for a saved card (brand title-cased). */
	function cardLabel(pm: BillingPaymentMethod): string {
		const brand = pm.brand ? pm.brand.charAt(0).toUpperCase() + pm.brand.slice(1) : m('billing.pm.card');
		return pm.last4 ? `${brand} ····${pm.last4}` : brand;
	}

	/** "Expires 12/2030" or "—" when the provider omits expiry. */
	function cardExpiry(pm: BillingPaymentMethod): string {
		if (pm.exp_month == null || pm.exp_year == null) return '—';
		return m('billing.pm.expires', {
			date: `${String(pm.exp_month).padStart(2, '0')}/${pm.exp_year}`
		});
	}

	// No actions column: nothing acts on a single saved card (Add / replace
	// works on the set), and an empty, unlabelled column was dead width.
	const PAYMENT_METHOD_COLUMNS = $derived([
		{ label: m('billing.pm.colCard') },
		{ label: m('billing.pm.colExpiry') }
	]);

	const plan = $derived(data?.plan ?? null);
	const subscription = $derived(data?.subscription ?? null);
	const hasSubscription = $derived(plan !== null && subscription !== null);

	/** Currency for the proration figure — the target plan's own currency,
	 *  falling back to the (now-current) plan's currency defensively. */
	const changedPlanCurrency = $derived(
		changeResult
			? (availablePlans.find((p) => p.code === changeResult!.new_plan_code)?.currency ??
					plan?.currency)
			: undefined
	);

	/** Whole-number usage counter rendered defensively (the API sends strings). */
	function asCount(n: string | undefined): string {
		if (n === undefined || n === '') return '0';
		return formatCount(n, { fallback: n });
	}

	/**
	 * Period boundaries are UTC midnights (the provider bills on UTC days), so
	 * they render in UTC: in local time a reader west of UTC saw "Oct 31 –
	 * Nov 30" for a period that starts on Nov 1.
	 */
	const UTC_DATE_OPTS: Intl.DateTimeFormatOptions = {
		month: 'short',
		day: 'numeric',
		year: 'numeric',
		timeZone: 'UTC'
	};

	/** Period window label, e.g. "Jun 1, 2026 – Jul 1, 2026". */
	const periodWindow = $derived(
		subscription
			? `${formatDate(subscription.current_period_start, '—', UTC_DATE_OPTS)} – ${formatDate(subscription.current_period_end, '—', UTC_DATE_OPTS)}`
			: '—'
	);

	/** The usage month as a reader says it ("November 2026"), not the `YYYY-MM` key. */
	const usagePeriod = $derived(data?.period ? formatMonth(data.period, 'long') : null);

	// Who bills the subscription. The adapter code (`stripe_billing`) is a
	// config value, not a name; a provider brand is a proper noun and stays
	// untranslated. An adapter not listed here falls back to the generic word.
	const PROVIDER_NAMES: Record<string, string> = { stripe_billing: 'Stripe' };
	const managedBy = $derived(
		subscription?.externally_managed
			? (PROVIDER_NAMES[data?.provider ?? ''] ?? m('billing.plan.provider'))
			: m('billing.plan.selfServe')
	);

	/** Human label for an invoice settlement state. */
	const INVOICE_STATUS_LABELS = $derived<Record<BillingInvoiceStatus, string>>({
		paid: m('billing.invoiceStatus.paid'),
		open: m('billing.invoiceStatus.open'),
		void: m('billing.invoiceStatus.void')
	});

	/**
	 * Badge tone per settlement state. Not `$derived` — a tone is not
	 * translatable, and the map is the semantic call the pill's colour used to
	 * hard-code three ways.
	 */
	const INVOICE_STATUS_TONES: Record<BillingInvoiceStatus, BadgeTone> = {
		paid: 'success',
		// Open is money still owed us, not a failure — amber, never red.
		open: 'warning',
		void: 'muted'
	};

	const INVOICE_COLUMNS = $derived([
		{ label: m('billing.invoiceCol.invoice') },
		{ label: m('billing.invoiceCol.period') },
		{ label: m('billing.invoiceCol.amount'), class: 'num' },
		{ label: m('billing.invoiceCol.status') },
		{ label: m('billing.invoiceCol.date') },
		{ label: '', class: 'actions-col' }
	]);

	/**
	 * Granted features, named as the pricing page names them (`planFeature.*`)
	 * and in catalogue order. The raw key (`public_api`) used to be the label.
	 * A key the client does not know yet (an operator-negotiated grant) still
	 * shows, de-snaked, after the known ones rather than vanishing.
	 */
	const entitlementLabels = $derived.by(() => {
		if (!plan) return [] as { key: string; label: string }[];
		const granted = Object.entries(plan.entitlements)
			.filter(([, v]) => v === true)
			.map(([k]) => k);
		const known = PLAN_FEATURES.filter((f) => granted.includes(f)).map((f) => ({
			key: f,
			label: m(planFeatureLabelKey(f))
		}));
		const unknown = granted
			.filter((k) => !isPlanFeature(k))
			.map((k) => ({ key: k, label: k.replace(/_/g, ' ') }));
		return [...known, ...unknown];
	});
</script>

<PageHeader title={m('billing.title')}>
	<!-- One reading column, top to bottom in the order the reader asks the
	     questions: what am I on and is it healthy (plan), how much have I used
	     against it (usage), how do I pay (payment method), what have I paid
	     (invoices). Every section shares the column's width, so the edges line
	     up instead of the plan card stopping at 720px under a full-width meter. -->
	<div class="billing">
		{#if error}
			<LoadError message={error} onretry={load} testId="billing-error" />
		{:else}
			{#if loading}
				<!-- The plan card's slot, held from first paint so the sections
				     below (which load on their own) do not jump when it lands. -->
				<section class="panel plan-card plan-pending" aria-busy="true" data-testid="billing-loading">
					<p class="state">{m('billing.loading')}</p>
				</section>
			{:else if !hasSubscription}
				<!-- The org has no live subscription yet. Not `<EmptyState>`: its
				     title is a paragraph, and this one is the page's section heading. -->
				<section class="panel empty" data-testid="billing-empty">
					<h2>{m('billing.empty.heading')}</h2>
					<p>{m('billing.empty.body')}</p>
					<!-- Labelled "Contact sales": the org has no subscription yet. -->
					<a class="btn-primary btn-link" href="mailto:{CONTACT.sales}"
						>{m('billing.empty.contactSales')}</a
					>
				</section>
			{:else if plan && subscription}
				{#if subscription.status === 'past_due'}
					<!-- The one state that needs the reader to act before the dunning
					     sweep cancels them; it says what to do and takes them to where
					     they do it. (`canceled` never reaches this page: the endpoint
					     reports only a live subscription, so that is the empty state.) -->
					<div class="callout tone-danger" role="status" data-testid="billing-status-callout">
						<strong>{m('billing.status.pastDue.title')}</strong>
						<p>{m('billing.status.pastDue.body')}</p>
						<a href="#billing-payment-methods">{m('billing.status.pastDue.action')}</a>
					</div>
				{/if}

				<section class="panel plan-card" aria-label={m('billing.plan.aria')} data-testid="billing-plan">
					<div class="plan-head">
						<div class="plan-title">
							<span class="eyebrow">{m('billing.plan.current')}</span>
							<div class="plan-name-row">
								<h2 class="plan-name">{plan.name}</h2>
								<SubscriptionBadge status={subscription.status} />
							</div>
						</div>
						<p class="plan-price">
							<span class="price-figure"><Money amount={plan.monthly_price} currency={plan.currency} /></span>
							<span class="per">{m('billing.plan.perMonth')}</span>
						</p>
					</div>

					<dl class="plan-meta">
						<div class="meta-item">
							<dt>{m('billing.plan.period')}</dt>
							<dd>{periodWindow}</dd>
						</div>
						{#if subscription.status === 'trialing'}
							<div class="meta-item">
								<dt>{m('billing.plan.trialEnds')}</dt>
								<dd>{formatDate(subscription.trial_end, '—', UTC_DATE_OPTS)}</dd>
							</div>
						{/if}
						<div class="meta-item">
							<dt>{m('billing.plan.managedBy')}</dt>
							<dd>{managedBy}</dd>
						</div>
					</dl>

					{#if entitlementLabels.length > 0}
						<div class="entitlements">
							<h3 class="meta-label">{m('billing.plan.included')}</h3>
							<ul>
								{#each entitlementLabels as f (f.key)}
									<li>
										<svg class="check" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
											<path d="M3.5 8.5l3 3 6-7" />
										</svg>
										<span>{f.label}</span>
									</li>
								{/each}
							</ul>
						</div>
					{/if}

					<div class="plan-actions">
						<button
							type="button"
							class="btn-primary"
							onclick={openChangePlan}
							data-testid="billing-change-plan"
						>
							{m('billing.plan.changePlan')}
						</button>
						<!-- "Need a plan change?" is a commercial ask, so it goes to sales. -->
						<a class="link" href="mailto:{CONTACT.sales}">{m('billing.plan.changeContact')}</a>
					</div>
				</section>
			{/if}

			<!-- Usage — ONE section, rendered on every non-error state rather than
			     a copy inside each branch of the chain above (decisions §125, §143: a KPI
			     row is never gated on its own response, and which branch would own
			     it is the question still in flight). `asCount` coerces a missing
			     count to `'0'`, so the guard is on `data`, not on the field.

			     The priced meter leads (decisions §253): AI-read invoices against
			     the allowance, overage, cap and pause state. It renders for an org
			     with no subscription too — that org is on the Free allowance. Only
			     an admin edits the cap (the endpoint is admin-only). The raw
			     extraction counters follow as activity, not as the bill. -->
			<section class="usage" aria-label={m('billing.usage.aria')}>
				<h2 class="section-title">
					{m('billing.usage.heading')}
					{#if usagePeriod}<span class="period">· {usagePeriod}</span>{/if}
				</h2>

				<div class="panel">
					<AiUsagePanel
						usage={data?.ai_usage ?? null}
						pending={loading}
						canEditCap={auth.isAdmin}
						onUsageChange={(ai) => {
							if (data) data = { ...data, ai_usage: ai };
						}}
					/>
				</div>

				<div class="usage-counts" data-testid="billing-usage-counts">
					<div class="help-row usage-title">
						<h3>{m('billing.usage.activity')}</h3>
						<HelpTip term="extraction" />
					</div>
					<div class="kpi-row">
						<KpiCard
							value={data ? asCount(data.usage.extractions) : null}
							label={m('billing.usage.extractions')}
							pending={loading}
						/>
						<KpiCard
							value={data ? asCount(data.usage.extractions_platform) : null}
							label={m('billing.usage.billableExtractions')}
							pending={loading}
						/>
						<!-- One card PER CURRENCY, never a cross-currency sum. No
						     rebates → no card, rather than a $0.00 in a currency the
						     page picked. `rebateGroups` is empty while `data` is null,
						     so these appear with the response and never as a dash.
						     An org with no subscription gets them IN the row; one
						     with a plan gets the note below it. -->
						{#if !hasSubscription}
							{#each rebateGroups as group (group.currency)}
								<KpiCard
									value={formatMoney(group.total, { currency: group.currency })}
									label={rebateGroups.length > 1
										? `${m('billing.usage.cardRebates')} (${group.currency})`
										: m('billing.usage.cardRebates')}
									highlight="green"
								/>
							{/each}
						{/if}
					</div>
					{#if hasSubscription && rebateGroups.length > 0}
						<p class="note">
							{m('billing.usage.cardRebatesNote')}
							{#each rebateGroups as group, i (group.currency)}{i > 0
									? ', '
									: ''}<Money amount={group.total} currency={group.currency} />{/each}
							{m('billing.usage.informational')}
							<HelpTip term="card-rebate" />
						</p>
					{/if}
				</div>
			</section>
		{/if}

		<!-- Payment methods — the org's saved cards (PII-safe metadata only).
		     Loaded independently of the plan/usage block, with its own loading
		     and error states, so it renders while the plan is still loading, for
		     an org with no live subscription, and even when the plan failed. -->
		<section
			id="billing-payment-methods"
			class="list-section"
			aria-label={m('billing.pm.aria')}
			data-testid="billing-payment-methods"
		>
			<div class="section-head">
				<h2 class="section-title">{m('billing.pm.heading')}</h2>
				<button
					type="button"
					class="btn-cancel"
					onclick={startAddCard}
					disabled={addingCard}
					data-testid="billing-add-card"
				>
					{addingCard ? m('billing.pm.starting') : m('billing.pm.addReplace')}
				</button>
			</div>

			{#if cardSetup.state !== 'idle'}
				<!-- Add-card flow affordance. The real card-collection form (the
				     provider's Stripe Elements) is a deployed-only piece — it can't run
				     in the local-first stack and the frontend must never call a
				     secret-bearing service directly. So we surface the right next-step
				     state here and leave a clearly-marked seam for Elements. -->
				<div
					class="card-setup state-{cardSetup.state}"
					data-testid="billing-card-setup"
					role={cardSetup.state === 'error' ? 'alert' : 'status'}
				>
					{#if cardSetup.state === 'starting'}
						<p>{m('billing.card.starting')}</p>
					{:else if cardSetup.state === 'not_configured'}
						<p>{cardSetup.message}</p>
						<!-- The payment provider is not configured — an operational
						     fault on our side, not a purchase question, so: support. -->
						<a class="link" href="mailto:{CONTACT.support}">{m('billing.card.contactUs')}</a>
					{:else if cardSetup.state === 'error'}
						<p>{cardSetup.message}</p>
						<button type="button" class="btn-cancel" onclick={startAddCard}>{m('common.retry')}</button>
					{:else if cardSetup.state === 'ready'}
						<p>{cardSetup.message}</p>
						<!-- DEPLOYED-ONLY SEAM: mount the provider's card form (Stripe
						     Elements) here, confirm the SetupIntent client_secret against
						     it, then re-list cards. No Stripe keys are hardcoded in the
						     static frontend; the secret never leaves this boundary. -->
						<p class="card-setup-placeholder" data-testid="billing-card-elements-placeholder">
							{m('billing.card.elementsPlaceholder')}
						</p>
					{/if}
					<button type="button" class="link card-setup-dismiss" onclick={closeAddCard}>
						{m('billing.card.dismiss')}
					</button>
				</div>
			{/if}

			{#if pmLoading}
				<p class="state" data-testid="billing-payment-methods-loading">{m('billing.pm.loading')}</p>
			{:else if pmError}
				<LoadError
					message={pmError}
					onretry={loadPaymentMethods}
					testId="billing-payment-methods-error"
				/>
			{:else}
				<DataTable
					columns={PAYMENT_METHOD_COLUMNS}
					isEmpty={paymentMethods.length === 0}
					empty={m('billing.pm.empty')}
				>
					{#snippet body()}
						{#each paymentMethods as pm (pm.id)}
							<tr>
								<td>
									<span class="card-label">{cardLabel(pm)}</span>
									{#if pm.is_default}
										<span class="default-pill">{m('billing.pm.default')}</span>
									{/if}
								</td>
								<td class="muted">{cardExpiry(pm)}</td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>
			{/if}
		</section>

		<!-- Invoices / receipts — the org's past platform-billing invoices, loaded
		     independently of everything above for the same reason. -->
		<section class="list-section" aria-label={m('billing.invoices.aria')} data-testid="billing-invoices">
			<h2 class="section-title">{m('billing.invoices.heading')}</h2>
			{#if invoicesLoading}
				<p class="state" data-testid="billing-invoices-loading">{m('billing.invoices.loading')}</p>
			{:else if invoicesError}
				<LoadError
					message={invoicesError}
					onretry={loadInvoices}
					testId="billing-invoices-error"
				/>
			{:else}
				<DataTable
					columns={INVOICE_COLUMNS}
					isEmpty={invoices.length === 0}
					empty={m('billing.invoices.empty')}
				>
					{#snippet body()}
						{#each invoices as inv (inv.id)}
							<tr>
								<td class="mono nowrap">{inv.number ?? '—'}</td>
								<td class="nowrap">{inv.period ? formatMonth(inv.period) : '—'}</td>
								<td class="num"><Money amount={inv.amount} currency={inv.currency} mono /></td>
								<td>
									<Badge tone={INVOICE_STATUS_TONES[inv.status] ?? 'muted'} variant={inv.status}>
										{INVOICE_STATUS_LABELS[inv.status] ?? inv.status}
									</Badge>
								</td>
								<td>{formatDate(inv.created_at)}</td>
								<td class="actions">
									{#if inv.hosted_url}
										<a
											class="link"
											href={inv.hosted_url}
											target="_blank"
											rel="noopener noreferrer"
											aria-label={m('billing.invoices.viewAria', { number: inv.number ?? inv.id })}
										>
											{m('billing.invoices.view')}
										</a>
									{:else}
										<span class="muted">—</span>
									{/if}
								</td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>
			{/if}
		</section>
	</div>
</PageHeader>

<!-- Plan-change dialog. `POST /api/billing/change-plan` applies the move on the
     spot (no preview mode on the backend), so the confirm step says so plainly;
     the result state then renders the REAL proration the response returned
     (or a clean no-op message when `changed` is false). -->
<Modal
	open={showChangePlan}
	ariaLabel={m('billing.plan.changeModal.aria')}
	title={changeResult
		? m('billing.plan.changeModal.successHeading')
		: m('billing.plan.changeModal.selectHeading')}
	onclose={closeChangePlan}
>
	{#if changeResult}
		<div class="change-result" data-testid="billing-plan-change-result">
			{#if changeResult.changed}
				<p>
					{m('billing.plan.changeModal.successChanged', {
						plan: planName(changeResult.new_plan_code)
					})}
				</p>
				<dl class="proration">
					<dt>{m('billing.plan.changeModal.prorationLabel')}</dt>
					<dd>
						<Money
							amount={changeResult.proration.amount}
							currency={changedPlanCurrency}
							accounting
						/>
					</dd>
				</dl>
				<p class="hint">{m('billing.plan.changeModal.prorationHint')}</p>
			{:else}
				<p data-testid="billing-plan-change-noop">
					{m('billing.plan.changeModal.successNoop', {
						plan: planName(changeResult.new_plan_code)
					})}
				</p>
			{/if}
			<div class="modal-footer">
				<button type="button" class="btn-primary" onclick={closeChangePlan}>
					{m('billing.plan.changeModal.done')}
				</button>
			</div>
		</div>
	{:else}
		<form
			onsubmit={(e) => {
				e.preventDefault();
				confirmPlanChange();
			}}
		>
			{#if plansLoading}
				<p class="state">{m('billing.plan.changeModal.loading')}</p>
			{:else if plansError}
				<LoadError
					message={plansError}
					onretry={loadAvailablePlans}
					testId="billing-plans-error"
				/>
			{:else if availablePlans.length === 0}
				<p class="state">{m('billing.plan.changeModal.empty')}</p>
			{:else}
				<fieldset class="plan-options">
					<legend>{m('billing.plan.changeModal.legend')}</legend>
					{#each availablePlans as p (p.code)}
						{@const isCurrent = plan?.code === p.code}
						<label class="plan-option" class:selected={selectedPlanCode === p.code}>
							<input
								type="radio"
								name="plan-code"
								value={p.code}
								checked={selectedPlanCode === p.code}
								disabled={isCurrent}
								onchange={() => (selectedPlanCode = p.code)}
								aria-label={m('billing.plan.changeModal.selectAria', { name: p.name })}
							/>
							<span class="plan-option-name">
								{p.name}
								{#if isCurrent}
									<span class="current-pill">{m('billing.plan.changeModal.currentBadge')}</span>
								{/if}
							</span>
							<span class="plan-option-price">
								<Money amount={p.monthly_price} currency={p.currency} mono />
								<span class="per">{m('billing.plan.perMonth')}</span>
							</span>
						</label>
					{/each}
				</fieldset>

				{#if changeError}
					<div class="state error" role="alert">{changeError}</div>
				{/if}

				<p class="apply-notice">{m('billing.plan.changeModal.applyNotice')}</p>

				<div class="modal-footer">
					<button type="button" class="btn-cancel" onclick={closeChangePlan}>
						{m('common.cancel')}
					</button>
					<button
						type="submit"
						class="btn-primary"
						disabled={!selectedPlanCode || changingPlan}
						data-testid="billing-change-plan-confirm"
					>
						{changingPlan
							? m('billing.plan.changeModal.changing')
							: m('billing.plan.changeModal.changeButton')}
					</button>
				</div>
			{/if}
		</form>
	{/if}
</Modal>

<style>
	/* One reading column. A local cap, not a breakpoint: below 64rem the
	   column is simply the workspace width, so 390px gets no new rule. */
	.billing {
		display: flex;
		flex-direction: column;
		gap: 2rem;
		width: 100%;
		max-width: 64rem;
		min-width: 0;
	}

	.billing > :global(*) {
		min-width: 0;
	}

	/* The card surface every non-table section sits on — the same radius and
	   shadow `.grid-container` and `.kpi` use, so the page reads as one set. */
	.panel {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		box-shadow: var(--shadow-card);
		padding: 1.5rem;
	}

	.section-title {
		margin: 0;
		font-size: 1.1rem;
		font-weight: 600;
	}

	.section-head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
	}

	.list-section,
	.usage {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.state {
		color: var(--text-muted);
		margin: 0;
		padding: 0.5rem 0;
	}

	.state.error {
		color: var(--danger);
	}

	/* --- Status callout (past due) --- */
	.callout {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-left-width: 4px;
		border-radius: var(--radius);
		padding: 1rem 1.25rem;
	}

	.callout.tone-danger {
		border-left-color: var(--danger);
	}

	.callout p {
		margin: 0;
		color: var(--text-muted);
	}

	.callout a {
		color: var(--accent);
		font-weight: 600;
		align-self: flex-start;
		margin-top: 0.25rem;
	}

	/* --- Empty (no subscription) --- */
	.empty h2 {
		margin: 0 0 0.5rem;
		font-size: 1.25rem;
	}

	.empty p {
		color: var(--text-muted);
		margin: 0 0 1.25rem;
		max-width: 42rem;
	}

	.btn-link {
		display: inline-block;
		text-decoration: none;
	}

	/* --- Current plan --- */
	.plan-pending {
		/* Roughly the settled card's height, so the sections below do not jump
		   when the plan lands. */
		min-height: 14rem;
	}

	.plan-head {
		display: flex;
		flex-wrap: wrap;
		align-items: flex-start;
		justify-content: space-between;
		gap: 1rem 2rem;
	}

	.plan-title {
		min-width: 0;
	}

	.eyebrow,
	.meta-label,
	.plan-meta dt {
		display: block;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}

	.plan-name-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem 0.75rem;
		margin-top: 0.35rem;
	}

	.plan-name {
		margin: 0;
		font-size: 1.75rem;
		line-height: 1.2;
		overflow-wrap: anywhere;
	}

	.plan-price {
		margin: 0;
		display: flex;
		align-items: baseline;
		gap: 0.35rem;
		white-space: nowrap;
	}

	.price-figure {
		font-size: 1.75rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
	}

	.per {
		font-weight: 400;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.plan-meta {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(100%, 12rem), 1fr));
		gap: 1rem 1.5rem;
		margin: 1.5rem 0 0;
		padding-top: 1.25rem;
		border-top: 1px solid var(--hairline);
	}

	.meta-item {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		min-width: 0;
	}

	.plan-meta dd {
		margin: 0;
		font-weight: 600;
		overflow-wrap: anywhere;
	}

	.entitlements {
		margin-top: 1.5rem;
	}

	.entitlements h3 {
		margin: 0;
	}

	/* A checklist, not tag pills: the features are what the plan buys, and a
	   list of sentences reads as one where a cloud of chips does not. */
	.entitlements ul {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr));
		gap: 0.5rem 1.5rem;
		list-style: none;
		padding: 0;
		margin: 0.6rem 0 0;
	}

	.entitlements li {
		display: flex;
		align-items: flex-start;
		gap: 0.5rem;
		font-size: 0.9rem;
	}

	.check {
		flex: none;
		width: 1rem;
		height: 1rem;
		margin-top: 0.15rem;
		fill: none;
		stroke: var(--success);
		stroke-width: 2;
		stroke-linecap: round;
		stroke-linejoin: round;
	}

	.plan-actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem 1.25rem;
		margin-top: 1.5rem;
		padding-top: 1.25rem;
		border-top: 1px solid var(--hairline);
	}

	.link {
		color: var(--accent);
		font-size: 0.9rem;
	}

	/* --- Usage --- */
	.period {
		font-weight: 400;
		color: var(--text-muted);
		font-size: 0.95rem;
	}

	/* `AiUsagePanel` draws its own top rule and offset for when it followed
	   other content; as the first thing inside its card those are a stray
	   line. Neutralised here rather than in the shared component. */
	.usage .panel > :global(.ai-usage) {
		margin-top: 0;
		padding-top: 0;
		border-top: none;
	}

	.usage-counts {
		margin-top: 0.5rem;
	}

	.usage-title {
		margin: 0 0 0.75rem;
	}

	.usage-title > h3 {
		margin: 0;
		font-size: 0.95rem;
	}

	.kpi-row {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(100%, 12rem), 1fr));
		gap: 1rem;
	}

	.note {
		margin: 0.75rem 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	/* --- Payment methods --- */
	.card-setup {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 0.75rem 1rem;
		font-size: 0.9rem;
	}

	.card-setup p {
		margin: 0;
		color: var(--text);
	}

	.card-setup.state-error {
		border-color: var(--danger);
	}

	.card-setup.state-error p {
		color: var(--danger);
	}

	.card-setup-placeholder {
		color: var(--text-muted) !important;
		font-style: italic;
		flex-basis: 100%;
	}

	.card-setup-dismiss {
		margin-left: auto;
		background: none;
		border: none;
		cursor: pointer;
		padding: 0;
		min-height: 24px;
	}

	.card-label {
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}

	/* Not `<Badge>`: the "default card" marker inside a table cell — a
	   register smaller than a status pill, metric-matched to `.current-pill`
	   below, and it owns its own inline offset. Colour is the palette pair. */
	.default-pill {
		display: inline-block;
		margin-left: 0.5rem;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		padding: 2px 8px;
		border-radius: 10px;
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		font-weight: 600;
	}

	/* --- Invoices --- */
	.num {
		text-align: right;
	}

	/* An invoice number or month broken across lines ("FL-/2026-/10") is
	   unreadable; at phone width the table scrolls inside `DataTable` instead. */
	.nowrap {
		white-space: nowrap;
	}

	.muted {
		color: var(--text-muted);
	}

	/* --- Plan-change modal: picker fieldset + result panel --- */
	.plan-options {
		border: none;
		padding: 0;
		margin: 0 0 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.plan-options legend {
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
		padding: 0 0 0.5rem;
	}

	.plan-option {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 0.75rem 0.9rem;
		cursor: pointer;
		transition: border-color 0.15s var(--ease-out);
	}

	.plan-option:hover:not(:has(input:disabled)) {
		border-color: var(--border-strong);
	}

	/* The plan you already have: de-emphasised with the muted colour token,
	   never `opacity`, which takes the text below its contrast floor. */
	.plan-option:has(input:disabled) {
		cursor: default;
		color: var(--text-muted);
	}

	.plan-option.selected {
		border-color: var(--accent);
		box-shadow: 0 0 0 1px var(--accent);
	}

	.plan-option-name {
		flex: 1;
		min-width: 0;
		font-weight: 600;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
	}

	.plan-option-price {
		white-space: nowrap;
		font-weight: 600;
	}

	/* Not `<Badge>`: a "this is the one you already have" marker inside a
	   radio label, deliberately smaller than a status pill and metric-matched
	   to `.default-pill` above. Colour comes from the palette pair. */
	.current-pill {
		background: var(--muted-tint);
		color: var(--muted-on-tint);
		padding: 2px 8px;
		border-radius: 10px;
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		font-weight: 600;
	}

	.apply-notice {
		font-size: 0.85rem;
		color: var(--text-muted);
		margin: 0 0 1rem;
	}

	.change-result .proration {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 1rem;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 0.75rem 1rem;
		margin: 0.75rem 0;
	}

	.change-result .proration dt {
		/* Muted text on --surface-2 is 4.34:1; only --text clears 4.5:1 there.
		   /billing was not in the axe route list, so this one was latent. */
		color: var(--text);
		font-size: 0.85rem;
	}

	.change-result .proration dd {
		margin: 0;
		font-weight: 600;
		font-size: 1.1rem;
	}

	.change-result .hint {
		font-size: 0.85rem;
		color: var(--text-muted);
	}
</style>
