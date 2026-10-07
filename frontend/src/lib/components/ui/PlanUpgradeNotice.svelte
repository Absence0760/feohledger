<script lang="ts">
	/**
	 * "Available on Growth / Scale — upgrade": rendered IN PLACE of a setting
	 * the org's plan does not include, so an admin learns why and where to go
	 * instead of filling a form the server refuses with a 402
	 * (`plan_feature_required`, docs/decisions.md §254).
	 *
	 * Read the gate from `auth.hasFeature(feature)` at the call site and render
	 * this when it is false. The server stays the authority — this only spares
	 * the dead end.
	 *
	 * The upgrade link goes to `/billing`, which is admin/CFO-only (matching
	 * `require_roles(admin, cfo)` on the billing API); anyone else is told to
	 * ask an administrator instead of being sent to a page that would bounce
	 * them.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import {
		planFeatureLabelKey,
		planTierLabelKey,
		type PlanFeature
	} from '#lib/types/planFeatures.ts';

	let {
		feature,
		testId
	}: {
		feature: PlanFeature;
		/** `data-testid` for an e2e selector; the class is Svelte-scoped. */
		testId?: string;
	} = $props();

	const plan = $derived(m(planTierLabelKey(feature)));
	const featureName = $derived(m(planFeatureLabelKey(feature)));
</script>

<section class="plan-upgrade" data-testid={testId} data-feature={feature}>
	<p class="plan-upgrade-title">{m('planUpgrade.title', { plan })}</p>
	<p class="plan-upgrade-body">{m('planUpgrade.body', { feature: featureName, plan })}</p>
	{#if auth.isCfo}
		<a class="plan-upgrade-link" href="/billing">{m('planUpgrade.cta', { plan })}</a>
	{:else}
		<p class="plan-upgrade-body">{m('planUpgrade.askAdmin')}</p>
	{/if}
</section>

<style>
	.plan-upgrade {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 6px;
		margin: 12px 0 0;
		padding: 14px 16px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--accent-tint);
	}

	.plan-upgrade-title {
		margin: 0;
		font-weight: 600;
		/* The calibrated text-on-tint token; `--accent` on `--accent-tint`
		   fails 1.4.3 (see app.css). */
		color: var(--accent-on-tint);
	}

	.plan-upgrade-body {
		margin: 0;
		font-size: 0.875rem;
		line-height: 1.45;
		color: var(--text);
	}

	.plan-upgrade-link {
		display: inline-flex;
		align-items: center;
		/* WCAG 2.5.8 target size. */
		min-height: 24px;
		margin-top: 2px;
		font-weight: 600;
		color: var(--accent-on-tint);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
</style>
