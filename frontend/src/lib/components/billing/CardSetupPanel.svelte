<script lang="ts">
	/**
	 * The add / replace-card flow on /billing: starts a SetupIntent, collects the
	 * card, then makes it the org's default (`POST /api/billing/payment-method/
	 * default`) and hands the refreshed list back through `onsaved`.
	 *
	 * Which form shows is `types/billing.ts::cardFormMode`:
	 *
	 * - real Stripe → the Payment Element, confirmed with `stripe.confirmSetup`.
	 *   Stripe.js is imported lazily from `@stripe/stripe-js/pure` the moment
	 *   this mode is chosen, so js.stripe.com never loads on another page, in
	 *   local mock mode, or when the build has no publishable key. The card
	 *   number goes from Stripe's iframe straight to Stripe; this component only
	 *   ever holds the SetupIntent's single-use `client_secret`, in a plain
	 *   variable (never `$state`, never logged, never stored), dropped with the
	 *   Element on save, retry and unmount.
	 * - the local `mock` provider → no card form; "Save test card" confirms the
	 *   mock SetupIntent id the backend returned.
	 * - otherwise a not-configured message with a contact link.
	 *
	 * Mounted fresh for each attempt (the page keys it), so a retry is a new
	 * SetupIntent and nothing from an abandoned one survives.
	 */
	import { onDestroy, onMount, tick } from 'svelte';
	import { PUBLIC_STRIPE_PUBLISHABLE_KEY } from '$app/env/public';
	import type { Stripe, StripeElements, StripePaymentElement } from '@stripe/stripe-js';
	import { setDefaultBillingPaymentMethod, startBillingSetupIntent } from '#lib/api/billing.ts';
	import { cardFormMode, type BillingPaymentMethodsResponse, type CardFormMode } from '#lib/types/billing.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';
	import { currentLocale, m } from '#lib/i18n/store.svelte.ts';
	import { theme } from '#lib/stores/theme.svelte.ts';
	import { CONTACT } from '#lib/legal/operator.ts';

	interface Props {
		/** Display name of the plan a refused upgrade is waiting on, or null for
		 *  a plain add / replace. Shown so the reader knows saving continues it. */
		pendingPlanName?: string | null;
		/** The card is saved and is now the default; `res` is the refreshed list. */
		onsaved: (res: BillingPaymentMethodsResponse) => void | Promise<void>;
		onclose: () => void;
	}

	let { pendingPlanName = null, onsaved, onclose }: Props = $props();

	type Phase = 'starting' | 'start_error' | CardFormMode;
	let phase = $state<Phase>('starting');
	let startError = $state<string | null>(null);
	let elementState = $state<'loading' | 'ready' | 'error'>('loading');
	let saving = $state(false);
	let saveError = $state<string | null>(null);
	let container = $state<HTMLDivElement | undefined>();
	let heading = $state<HTMLHeadingElement | undefined>();

	// Deliberately NOT reactive: the secret and the SDK handles never need to
	// render, and keeping them out of `$state` keeps them out of devtools'
	// state snapshots.
	let setupIntentId: string | null = null;
	let clientSecret: string | null = null;
	let stripe: Stripe | null = null;
	let elements: StripeElements | null = null;
	let paymentElement: StripePaymentElement | null = null;
	let destroyed = false;

	const headingId = `card-setup-heading-${Math.random().toString(36).slice(2, 8)}`;

	function teardown() {
		paymentElement?.destroy();
		paymentElement = null;
		elements = null;
		stripe = null;
		clientSecret = null;
	}

	async function start() {
		teardown();
		setupIntentId = null;
		phase = 'starting';
		startError = null;
		saveError = null;
		try {
			const res = await startBillingSetupIntent();
			if (destroyed) return;
			const mode = cardFormMode(res, PUBLIC_STRIPE_PUBLISHABLE_KEY);
			setupIntentId = res.setup_intent_id;
			phase = mode;
			if (mode === 'stripe') {
				clientSecret = res.client_secret;
				await tick();
				await mountStripe();
			}
		} catch (e) {
			if (destroyed) return;
			phase = 'start_error';
			startError = errorMessage(e, m('billing.card.startError'));
		}
	}

	async function mountStripe() {
		elementState = 'loading';
		try {
			const { loadStripe } = await import('@stripe/stripe-js/pure');
			const locale = currentLocale();
			const loaded = await loadStripe(PUBLIC_STRIPE_PUBLISHABLE_KEY, { locale });
			if (destroyed) return;
			if (!loaded || !container || !clientSecret) throw new Error('stripe unavailable');
			stripe = loaded;
			elements = loaded.elements({
				clientSecret,
				locale,
				appearance: { theme: theme.resolved === 'dark' ? 'night' : 'stripe' }
			});
			const el = elements.create('payment');
			el.on('ready', () => {
				elementState = 'ready';
			});
			el.on('loaderror', () => {
				elementState = 'error';
			});
			el.mount(container);
			paymentElement = el;
		} catch {
			// The script was blocked or unreachable, or Stripe refused the key.
			// Nothing about it is the reader's to fix but a retry.
			if (!destroyed) elementState = 'error';
		}
	}

	/** Make the confirmed SetupIntent's card the default and hand back the list. */
	async function finish(id: string) {
		const res = await setDefaultBillingPaymentMethod(id);
		teardown();
		await onsaved(res);
	}

	async function saveStripeCard() {
		if (!stripe || !elements || saving) return;
		saving = true;
		saveError = null;
		try {
			const result = await stripe.confirmSetup({
				elements,
				redirect: 'if_required',
				// Only a redirect-based method ever uses it; a card (3-D Secure
				// included) completes in place. The page finishes a redirect on
				// return (`/billing?setup_intent=…&redirect_status=succeeded`).
				confirmParams: { return_url: `${window.location.origin}/billing` }
			});
			if (result.error) {
				// Stripe localizes its card and validation messages (the `locale`
				// above) and documents them as safe to show; anything else gets
				// our generic sentence rather than an API-level internals string.
				const shown =
					result.error.type === 'card_error' || result.error.type === 'validation_error';
				saveError = (shown && result.error.message) || m('billing.card.confirmFailed');
				return;
			}
			await finish(result.setupIntent.id);
		} catch (e) {
			saveError = errorMessage(e, m('billing.card.saveFailed'));
		} finally {
			saving = false;
		}
	}

	async function saveTestCard() {
		if (!setupIntentId || saving) return;
		saving = true;
		saveError = null;
		try {
			await finish(setupIntentId);
		} catch (e) {
			saveError = errorMessage(e, m('billing.card.saveFailed'));
		} finally {
			saving = false;
		}
	}

	onMount(() => {
		// Move focus to the panel the reader just opened (from the section's
		// button or from the plan dialog's "Add a card"), and bring it into view.
		heading?.scrollIntoView({ block: 'nearest' });
		heading?.focus();
		start();
	});

	onDestroy(() => {
		destroyed = true;
		teardown();
	});
</script>

<section
	class="card-setup"
	class:state-error={phase === 'start_error'}
	aria-labelledby={headingId}
	data-testid="billing-card-setup"
	data-mode={phase}
>
	<h3 id={headingId} class="card-setup-heading" tabindex="-1" bind:this={heading}>
		{m('billing.card.heading')}
	</h3>

	{#if pendingPlanName}
		<p class="pending" data-testid="billing-card-pending-plan">
			{m('billing.card.pendingPlan', { plan: pendingPlanName })}
		</p>
	{/if}

	{#if phase === 'starting'}
		<p role="status">{m('billing.card.starting')}</p>
	{:else if phase === 'start_error'}
		<p role="alert">{startError}</p>
		<div class="actions">
			<button type="button" class="btn-cancel" onclick={start}>{m('common.retry')}</button>
			<button type="button" class="btn-cancel" onclick={onclose}>{m('billing.card.dismiss')}</button>
		</div>
	{:else if phase === 'not_configured' || phase === 'stripe_key_missing'}
		<p role="status" data-testid="billing-card-not-configured">
			{phase === 'not_configured' ? m('billing.card.notConfigured') : m('billing.card.keyMissing')}
		</p>
		<div class="actions">
			<!-- An operational gap on our side, not a purchase question: support. -->
			<a class="link" href="mailto:{CONTACT.support}">{m('billing.card.contactUs')}</a>
			<button type="button" class="btn-cancel" onclick={onclose}>{m('billing.card.dismiss')}</button>
		</div>
	{:else if phase === 'mock'}
		<p data-testid="billing-card-mock">{m('billing.card.mockIntro')}</p>
		{#if saveError}
			<p class="error" role="alert">{saveError}</p>
		{/if}
		<div class="actions">
			<button
				type="button"
				class="btn-primary"
				onclick={saveTestCard}
				disabled={saving}
				data-testid="billing-card-save-test"
			>
				{saving ? m('billing.card.saving') : m('billing.card.mockSave')}
			</button>
			<button type="button" class="btn-cancel" onclick={onclose} disabled={saving}>
				{m('common.cancel')}
			</button>
		</div>
	{:else if phase === 'stripe'}
		<form
			onsubmit={(e) => {
				e.preventDefault();
				saveStripeCard();
			}}
		>
			<div class="element-wrap" role="group" aria-label={m('billing.card.formLabel')}>
				{#if elementState === 'loading'}
					<p class="muted" role="status">{m('billing.card.loadingForm')}</p>
				{:else if elementState === 'error'}
					<p class="error" role="alert">{m('billing.card.formLoadError')}</p>
				{/if}
				<!-- Stripe mounts its iframe here; it carries its own labelled fields. -->
				<div
					class="element"
					aria-busy={elementState === 'loading'}
					bind:this={container}
					data-testid="billing-card-element"
				></div>
			</div>
			<p class="muted secure-note">{m('billing.card.secureNote')}</p>
			{#if saveError}
				<p class="error" role="alert" data-testid="billing-card-save-error">{saveError}</p>
			{/if}
			<div class="actions">
				{#if elementState === 'error'}
					<button type="button" class="btn-cancel" onclick={start}>{m('common.retry')}</button>
				{:else}
					<button
						type="submit"
						class="btn-primary"
						disabled={saving || elementState !== 'ready'}
						data-testid="billing-card-save"
					>
						{saving ? m('billing.card.saving') : m('billing.card.save')}
					</button>
				{/if}
				<button type="button" class="btn-cancel" onclick={onclose} disabled={saving}>
					{m('common.cancel')}
				</button>
			</div>
		</form>
	{/if}
</section>

<style>
	.card-setup {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 1rem;
		font-size: 0.9rem;
	}

	.card-setup.state-error {
		border-color: var(--danger);
	}

	.card-setup-heading {
		margin: 0;
		font-size: 1rem;
	}

	.card-setup p {
		margin: 0;
		color: var(--text);
	}

	.card-setup p.muted {
		color: var(--text-muted);
	}

	.card-setup p.error,
	.card-setup.state-error p[role='alert'] {
		color: var(--danger);
	}

	.pending {
		font-weight: 600;
	}

	form {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.element-wrap {
		min-height: 2.5rem;
	}

	.secure-note {
		font-size: 0.8rem;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem;
	}
</style>
