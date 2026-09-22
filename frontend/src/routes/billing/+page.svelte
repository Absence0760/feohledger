<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth } from '$lib/stores/auth.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import SubscriptionBadge from '$lib/components/ui/SubscriptionBadge.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import type { BadgeTone } from '$lib/components/ui/badgeTone';
	import {
		changeBillingPlan,
		getBillingInvoices,
		getBillingPaymentMethods,
		getBillingPlans,
		getBillingSubscription,
		startBillingSetupIntent
	} from '$lib/api/billing';
	import { formatMoney } from '$lib/utils/money';
	import { m } from '$lib/i18n/store.svelte';
	import type {
		BillingInvoice,
		BillingInvoiceStatus,
		BillingPaymentMethod,
		BillingPlan,
		BillingPlanChangeResponse,
		BillingSubscriptionResponse
	} from '$lib/types/billing';
	import { rebateMeterGroups } from '$lib/types/billing';
	import { formatDate } from '$lib/utils/time';

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
			error = e instanceof Error ? e.message : m('billing.error.load');
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
			invoicesError = e instanceof Error ? e.message : m('billing.error.loadInvoices');
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
			pmError = e instanceof Error ? e.message : m('billing.error.loadPaymentMethods');
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
				message: e instanceof Error ? e.message : m('billing.card.startError')
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
			plansError = e instanceof Error ? e.message : m('billing.plan.changeModal.loadError');
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
			changeError = e instanceof Error ? e.message : m('billing.plan.changeModal.changeFailed');
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

	const PAYMENT_METHOD_COLUMNS = $derived([
		{ label: m('billing.pm.colCard') },
		{ label: m('billing.pm.colExpiry') },
		{ label: '', class: 'actions-col' }
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
		const v = Number(n);
		return Number.isFinite(v) ? v.toLocaleString() : n;
	}

	/** Period window label, e.g. "Jun 1 – Jul 1, 2026". */
	const periodWindow = $derived(
		subscription
			? `${formatDate(subscription.current_period_start)} → ${formatDate(subscription.current_period_end)}`
			: '—'
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

	/** Pretty list of granted entitlement flags (truthy boolean keys). */
	const entitlementFlags = $derived(
		plan
			? Object.entries(plan.entitlements)
					.filter(([, v]) => v === true)
					.map(([k]) => k)
			: []
	);
</script>

<PageHeader title={m('billing.title')}>
	{#if error}
		<div class="state error" data-testid="billing-error" role="alert">
			<p>{error}</p>
			<button type="button" class="btn" onclick={load}>{m('billing.retry')}</button>
		</div>
	{:else}
		{#if loading}
			<p class="state" data-testid="billing-loading">{m('billing.loading')}</p>
		{:else if !hasSubscription}
			<!-- Friendly empty state — the org has no live subscription yet. -->
			<div class="empty" data-testid="billing-empty">
				<h2>{m('billing.empty.heading')}</h2>
				<p>{m('billing.empty.body')}</p>
				<a class="btn primary" href="mailto:billing@example.com"
					>{m('billing.empty.contactSales')}</a
				>
			</div>
		{:else if plan && subscription}
			<!-- Active subscription detail. -->
			<section class="plan-card" aria-label={m('billing.plan.aria')} data-testid="billing-plan">
				<div class="plan-head">
					<div>
						<span class="eyebrow">{m('billing.plan.current')}</span>
						<h2 class="plan-name">{plan.name}</h2>
					</div>
					<SubscriptionBadge status={subscription.status} />
				</div>

				<div class="plan-meta">
					<div class="meta-item">
						<span class="meta-label">{m('billing.plan.price')}</span>
						<span class="meta-value">
							<Money amount={plan.monthly_price} currency={plan.currency} />
							<span class="per">{m('billing.plan.perMonth')}</span>
						</span>
					</div>
					<div class="meta-item">
						<span class="meta-label">{m('billing.plan.period')}</span>
						<span class="meta-value">{periodWindow}</span>
					</div>
					{#if subscription.status === 'trialing'}
						<div class="meta-item">
							<span class="meta-label">{m('billing.plan.trialEnds')}</span>
							<span class="meta-value">{formatDate(subscription.trial_end)}</span>
						</div>
					{/if}
					<div class="meta-item">
						<span class="meta-label">{m('billing.plan.managedBy')}</span>
						<span class="meta-value">
							{subscription.externally_managed ? data?.provider ?? m('billing.plan.provider') : m('billing.plan.selfServe')}
						</span>
					</div>
				</div>

				{#if entitlementFlags.length > 0}
					<div class="entitlements">
						<span class="meta-label">{m('billing.plan.included')}</span>
						<ul>
							{#each entitlementFlags as flag (flag)}
								<li>{flag.replace(/_/g, ' ')}</li>
							{/each}
						</ul>
					</div>
				{/if}

				<div class="actions">
					<button
						type="button"
						class="btn"
						onclick={openChangePlan}
						data-testid="billing-change-plan"
					>
						{m('billing.plan.changePlan')}
					</button>
					<a class="link" href="mailto:billing@example.com">{m('billing.plan.changeContact')}</a>
				</div>
			</section>
		{/if}

		<!-- Usage meters — ONE section, rendered on every non-error state rather
		     than a copy inside each branch of the chain above.

		     It used to be two, both gated on `{#if loading}` having already
		     resolved, so the meters collapsed to nothing while the subscription
		     was in flight (`docs/decisions.md` §125 — a KPI row is never gated on
		     its own response). They could not simply each take `pending`: which
		     branch owns the row is decided by `hasSubscription`, and while the
		     response is out there is no answer to that — the row has to live
		     outside the question. Two copies also could not BOTH be brought onto
		     the convention without a third for the loading state.

		     `asCount` coerces a missing count to `'0'`, so the guard is on `data`
		     rather than on the field: a `'0'` is a figure as far as `KpiCard` is
		     concerned, and `pending` would have had nothing to do. Every settled
		     figure is unchanged.

		     The two presentations the branches carried are preserved exactly: an
		     org with no subscription gets its per-currency rebate cards IN the
		     row, one with a plan gets the note below it. -->
		<section class="usage-section" aria-label={m('billing.usage.aria')}>
			<h3>{m('billing.usage.heading')} <span class="period">({data?.period ?? '—'})</span></h3>
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
				<!-- One card PER CURRENCY. The meter used to be a single
				     cross-currency sum rendered with no currency at all, so a
				     tenant accruing EUR rebates read them as dollars. No
				     rebates → no card, rather than a $0.00 in a currency the
				     page picked. `rebateGroups` is empty while `data` is null,
				     so these appear with the response and never as a dash. -->
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
				</p>
			{/if}
		</section>
	{/if}

	<!-- Payment methods — the org's saved cards (PII-safe metadata only). Loaded
	     independently of the plan/usage block, so it renders for an org with a
	     card on file but no live subscription too. -->
	{#if !loading && !error}
		<section
			class="payment-methods-section"
			aria-label={m('billing.pm.aria')}
			data-testid="billing-payment-methods"
		>
			<div class="pm-head">
				<h3>{m('billing.pm.heading')}</h3>
				<button
					type="button"
					class="btn"
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
						<a class="link" href="mailto:billing@example.com">{m('billing.card.contactUs')}</a>
					{:else if cardSetup.state === 'error'}
						<p>{cardSetup.message}</p>
						<button type="button" class="btn" onclick={startAddCard}>{m('billing.retry')}</button>
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
				<div class="state error" data-testid="billing-payment-methods-error" role="alert">
					<p>{pmError}</p>
					<button type="button" class="btn" onclick={loadPaymentMethods}>{m('billing.retry')}</button>
				</div>
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
									{cardLabel(pm)}
									{#if pm.is_default}
										<span class="default-pill">{m('billing.pm.default')}</span>
									{/if}
								</td>
								<td>{cardExpiry(pm)}</td>
								<td class="actions"></td>
							</tr>
						{/each}
					{/snippet}
				</DataTable>
			{/if}
		</section>
	{/if}

	<!-- Invoices / receipts — the org's past platform-billing invoices. Loaded
	     independently of the plan/usage block above, so it renders for an org
	     with receipts but no live subscription too. -->
	{#if !loading && !error}
		<section class="invoices-section" aria-label={m('billing.invoices.aria')} data-testid="billing-invoices">
			<h3>{m('billing.invoices.heading')}</h3>
			{#if invoicesLoading}
				<p class="state" data-testid="billing-invoices-loading">{m('billing.invoices.loading')}</p>
			{:else if invoicesError}
				<div class="state error" data-testid="billing-invoices-error" role="alert">
					<p>{invoicesError}</p>
					<button type="button" class="btn" onclick={loadInvoices}>{m('billing.retry')}</button>
				</div>
			{:else}
				<DataTable
					columns={INVOICE_COLUMNS}
					isEmpty={invoices.length === 0}
					empty={m('billing.invoices.empty')}
				>
					{#snippet body()}
						{#each invoices as inv (inv.id)}
							<tr>
								<td class="mono">{inv.number ?? '—'}</td>
								<td>{inv.period ?? '—'}</td>
								<td class="num"><Money amount={inv.amount} currency={inv.currency} /></td>
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
	{/if}
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
				<div class="state error" role="alert">
					<p>{plansError}</p>
					<button type="button" class="btn" onclick={loadAvailablePlans}>
						{m('billing.retry')}
					</button>
				</div>
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
								<Money amount={p.monthly_price} currency={p.currency} />
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
	.state {
		color: var(--text-muted);
		padding: 1rem 0;
	}

	.state.error {
		color: #f06464;
	}

	.empty {
		max-width: 640px;
		padding: 1rem 0;
	}

	.empty h2 {
		margin: 0 0 0.5rem;
	}

	.empty p {
		color: var(--text-muted);
		margin: 0 0 1rem;
	}

	.plan-card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 12px;
		padding: 1.5rem;
		max-width: 720px;
		margin-bottom: 1.5rem;
	}

	.plan-head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 1rem;
	}

	.eyebrow {
		display: block;
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}

	.plan-name {
		margin: 0.25rem 0 0;
		font-size: 1.5rem;
	}

	.plan-meta {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 1rem;
		margin-top: 1.25rem;
	}

	.meta-item {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.meta-label {
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.meta-value {
		font-weight: 600;
	}

	.per {
		font-weight: 400;
		color: var(--text-muted);
		font-size: 0.85rem;
	}

	.entitlements {
		margin-top: 1.25rem;
	}

	.entitlements ul {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		list-style: none;
		padding: 0;
		margin: 0.5rem 0 0;
	}

	/* Not `<Badge>`: an entitlement TAG list, not a status — sentence-cased,
	   regular weight, and read as a group. Colour comes from the palette pair. */
	.entitlements li {
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		padding: 3px 10px;
		border-radius: 12px;
		font-size: 0.8rem;
		text-transform: capitalize;
	}

	/* Plan-change modal: picker fieldset + result panel. */
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
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		padding: 0 0 0.5rem;
	}

	.plan-option {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.65rem 0.85rem;
		cursor: pointer;
	}

	.plan-option:has(input:disabled) {
		cursor: default;
		opacity: 0.6;
	}

	.plan-option.selected {
		border-color: var(--accent);
	}

	.plan-option-name {
		flex: 1;
		font-weight: 600;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.plan-option-price {
		white-space: nowrap;
		font-weight: 600;
	}

	/* Not `<Badge>`: a "this is the one you already have" marker inside a
	   radio label, deliberately smaller than a status pill (0.7rem) and metric-
	   matched to `.default-pill` below. Colour comes from the palette pair. */
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
		border-radius: 8px;
		padding: 0.75rem 1rem;
		margin: 0.75rem 0;
	}

	.change-result .proration dt {
		/* Same 4.34:1 failure as the admin code chips — muted text on
		   --surface-2. /billing is not in the axe route list, so this one was
		   latent rather than caught. */
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

	.actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem;
		margin-top: 1.5rem;
		padding-top: 1.25rem;
		border-top: 1px solid var(--border);
	}

	.usage-section h3 {
		margin: 0 0 0.75rem;
	}

	.period {
		font-weight: 400;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.kpi-row {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 1rem;
	}

	.note {
		margin-top: 0.75rem;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.btn {
		background: var(--surface-2);
		color: var(--text);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.5rem 1rem;
		font-size: 0.9rem;
		cursor: pointer;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn.primary {
		background: var(--accent-strong);
		color: #fff;
		border-color: transparent;
		text-decoration: none;
		display: inline-block;
	}

	.link {
		color: var(--accent);
		font-size: 0.9rem;
	}

	.payment-methods-section {
		margin-top: 1.5rem;
		max-width: 720px;
	}

	.pm-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		margin: 0 0 0.75rem;
	}

	.pm-head h3 {
		margin: 0;
	}

	.card-setup {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.75rem 1rem;
		margin-bottom: 0.75rem;
		font-size: 0.9rem;
	}

	.card-setup p {
		margin: 0;
		color: var(--text);
	}

	.card-setup.state-error {
		border-color: #f06464;
	}

	.card-setup.state-error p {
		color: #f06464;
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
	}

	/* Not `<Badge>`: the "default card" marker inside a table cell — same
	   smaller register as `.current-pill` above, and it owns its own inline
	   offset. Colour comes from the palette pair. */
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

	.invoices-section {
		margin-top: 1.5rem;
		max-width: 720px;
	}

	.invoices-section h3 {
		margin: 0 0 0.75rem;
	}

	.num {
		text-align: right;
	}

	.muted {
		color: var(--text-muted);
	}

	/* The settlement-state pill is `<Badge>` now. It already spelled out the
	   shared recipe by hand ("mirrors the StatusBadge .badge recipe") in three
	   private tones — exactly the duplication the primitive exists to end. */
</style>
