<script lang="ts">
	import IconArrow from '~icons/material-symbols/arrow-forward';
	import IconCheck from '~icons/material-symbols/check-small';
	import LinkedMessage from '#lib/components/ui/LinkedMessage.svelte';
	import { CONTACT } from '#lib/legal/operator.ts';
	import { reveal } from '#lib/actions/reveal.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { PLANS } from '#lib/marketing/plans.generated.ts';
	import { CORE_FEATURE_KEYS, pricingTiers, topPlanName } from '#lib/marketing/pricing.ts';

	// Every price, allowance, overage rate and feature below comes from the
	// plan catalogue, via `lib/marketing/pricing.ts` over the GENERATED
	// `plans.generated.ts` (`pnpm gen:pricing`; `pnpm check:pricing` fails CI
	// when they drift — docs/decisions.md §253, issue #426). No figure is typed
	// into this file or its catalogue strings; `pricing.test.ts` enforces both.
	//
	// What is deliberately NOT here, each because it was false:
	//  - per-seat pricing, an annual toggle and a seat minimum — the plans are
	//    flat monthly per workspace with unlimited users;
	//  - a trial button — signup binds every new workspace to the first catalog
	//    plan (`tenant_provisioning`), and nothing grants `trial_days` yet, so
	//    every call to action is signup and the upgrade happens in /billing;
	//  - a "Most popular" badge — there is no customer data behind it.
	// Re-derived with the active locale (`m()` and the format locale are both
	// reactive), so switching language re-renders every figure.
	const tiers = $derived(pricingTiers());
	const firstPlan = PLANS[0].name;
	const topPlan = topPlanName();
	const salesHref = `mailto:${CONTACT.sales}`;
</script>

<section id="pricing" class="pricing" aria-labelledby="pricing-heading">
	<div class="section-head" use:reveal>
		<span class="eyebrow">{m('marketing.pricing.eyebrow')}</span>
		<h2 id="pricing-heading">{m('marketing.pricing.heading')}</h2>
		<p>{m('marketing.pricing.lede')}</p>
	</div>

	<div class="grid">
		{#each tiers as tier, i (tier.code)}
			<article
				class="plan"
				class:featured={tier.featured}
				data-plan={tier.code}
				aria-labelledby="plan-{tier.code}-name"
				use:reveal={{ delay: i * 70, amount: 0.1 }}
			>
				<h3 class="plan-name" id="plan-{tier.code}-name">{tier.name}</h3>
				{#if tier.tagline}
					<p class="plan-tagline">{m(tier.tagline)}</p>
				{/if}

				<p class="plan-price">
					<span class="amount" data-testid="plan-price">{tier.price}</span>
					<span class="unit">{m('marketing.pricing.perMonth')}</span>
				</p>

				<ul class="plan-allowance">
					<li class="strong">{m('marketing.pricing.unlimitedUsers')}</li>
					<li class="strong" data-testid="plan-included">
						{m('marketing.pricing.included', { n: tier.included, count: tier.includedLabel })}
					</li>
					<li class="muted" data-testid="plan-past-limit">
						{#if tier.overage === null}
							{m('marketing.pricing.pastLimit.pause')}
						{:else}
							{m('marketing.pricing.pastLimit.overage', { price: tier.overage })}
						{/if}
					</li>
				</ul>

				<a class="plan-cta" class:primary={tier.featured} href="/signup">
					{i === 0
						? m('marketing.pricing.cta.free')
						: m('marketing.pricing.cta.upgrade', { plan: tier.name })}
					<IconArrow aria-hidden="true" />
				</a>

				<p class="features-lead">
					{tier.buildsOn
						? m('marketing.pricing.buildsOn', { plan: tier.buildsOn })
						: m('marketing.pricing.coreHeading')}
				</p>
				<ul class="plan-features">
					{#each tier.buildsOn ? tier.addedFeatures : CORE_FEATURE_KEYS as key (key)}
						<li>
							<span class="check" aria-hidden="true"><IconCheck /></span>
							<span>{m(key)}</span>
						</li>
					{/each}
				</ul>
			</article>
		{/each}

		<article
			class="plan"
			data-plan="enterprise"
			aria-labelledby="plan-enterprise-name"
			use:reveal={{ delay: tiers.length * 70, amount: 0.1 }}
		>
			<h3 class="plan-name" id="plan-enterprise-name">{m('marketing.pricing.enterprise.name')}</h3>
			<p class="plan-tagline">{m('marketing.pricing.tagline.enterprise')}</p>

			<p class="plan-price">
				<span class="amount">{m('marketing.pricing.enterprise.price')}</span>
				<span class="unit">{m('marketing.pricing.enterprise.unit')}</span>
			</p>

			<ul class="plan-allowance">
				<li class="strong">{m('marketing.pricing.unlimitedUsers')}</li>
				<li class="strong">{m('marketing.pricing.enterprise.allowance')}</li>
			</ul>

			<a class="plan-cta" href={salesHref}>
				{m('marketing.pricing.cta.enterprise')}
				<IconArrow aria-hidden="true" />
			</a>

			<p class="features-lead">{m('marketing.pricing.buildsOn', { plan: topPlan })}</p>
			<ul class="plan-features">
				{#each ['marketing.pricing.enterprise.terms', 'marketing.pricing.enterprise.security'] as const as key (key)}
					<li>
						<span class="check" aria-hidden="true"><IconCheck /></span>
						<span>{m(key)}</span>
					</li>
				{/each}
			</ul>
		</article>
	</div>

	<div class="ai-read" use:reveal>
		<h3>{m('marketing.pricing.aiRead.heading')}</h3>
		<p>{m('marketing.pricing.aiRead.body')}</p>
	</div>

	<div class="compare-note">
		<p>{m('marketing.pricing.startsOnFree', { plan: firstPlan })}</p>
		<p>
			<LinkedMessage
				text={m('marketing.pricing.more', { plan: topPlan })}
				links={{ sales: { href: salesHref, label: m('marketing.pricing.moreLink') } }}
			/>
		</p>
		<p>
			<LinkedMessage
				text={m('marketing.pricing.selfHost')}
				links={{
					source: {
						href: 'https://github.com/Absence0760/feohledger',
						label: m('marketing.pricing.selfHostLink')
					}
				}}
			/>
		</p>
	</div>
</section>

<style>
	.pricing {
		max-width: 1180px;
		margin: 0 auto 110px;
		padding: 20px 32px;
	}

	/* The section head and eyebrow deliberately repeat Landing's recipe rather
	   than importing it: Svelte scopes styles per component, and this section
	   is the one place on the page they would otherwise diverge. */
	.section-head {
		max-width: 720px;
		margin: 0 auto 52px;
		text-align: center;
	}
	.eyebrow {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		padding: 5px 13px 5px 11px;
		margin-bottom: 20px;
		border-radius: 999px;
		border: 1px solid rgba(99, 140, 255, 0.28);
		background: rgba(99, 140, 255, 0.10);
		font-size: 0.75rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--accent-on-tint);
	}
	.eyebrow::before {
		content: '';
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--accent);
	}
	.section-head h2 {
		font-size: clamp(1.7rem, 3.6vw, 2.4rem);
		font-weight: 700;
		letter-spacing: -0.025em;
		margin: 0 0 14px;
	}
	.section-head p {
		color: var(--text-muted);
		line-height: 1.62;
		margin: 0;
	}

	/* ------------------------------ grid -------------------------------- */
	.grid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 16px;
		align-items: start;
	}
	@media (max-width: 1100px) {
		.grid { grid-template-columns: repeat(2, 1fr); gap: 20px; }
	}
	@media (max-width: 640px) {
		.grid { grid-template-columns: 1fr; max-width: 480px; margin-inline: auto; }
	}

	.plan {
		position: relative;
		border-radius: 16px;
		padding: 28px 22px;
		display: flex;
		flex-direction: column;
		background: rgba(24, 26, 35, 0.62);
		border: 1px solid var(--border);
		transition: border-color 0.2s, transform 0.2s, background 0.2s;
	}
	.plan:hover {
		border-color: rgba(99, 140, 255, 0.35);
		background: rgba(30, 33, 45, 0.75);
		transform: translateY(-3px);
	}
	/* The featured plan gets a gradient BORDER rather than a flat accent one: a
	   masked pseudo-element painting only the 1px ring, so the card's own
	   translucent fill still shows the page behind it. */
	.plan.featured {
		border-color: transparent;
		background:
			radial-gradient(420px 220px at 50% 0%, rgba(99, 140, 255, 0.16), transparent 70%),
			rgba(28, 31, 44, 0.8);
		box-shadow: 0 28px 70px -28px rgba(99, 140, 255, 0.45);
	}
	.plan.featured::before {
		content: '';
		position: absolute;
		inset: -1px;
		border-radius: inherit;
		padding: 1px;
		background: linear-gradient(160deg, #7d9bff, #a37dff 50%, rgba(231, 185, 94, 0.7));
		mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
		mask-composite: exclude;
		pointer-events: none;
	}

	.plan-name {
		font-size: 0.86rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
		margin: 0 0 8px;
	}
	.plan.featured .plan-name { color: var(--accent-on-tint); }
	.plan-tagline {
		color: var(--text);
		font-size: 0.92rem;
		margin: 0 0 24px;
		line-height: 1.45;
	}

	.plan-price {
		display: flex;
		align-items: baseline;
		flex-wrap: wrap;
		gap: 8px;
		margin: 0 0 20px;
	}
	.amount {
		font-size: 2.4rem;
		font-weight: 800;
		letter-spacing: -0.035em;
		font-variant-numeric: tabular-nums;
	}
	.plan.featured .amount {
		background: linear-gradient(135deg, #7d9bff, #a37dff);
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
	}
	.unit {
		color: var(--text-muted);
		font-size: 0.85rem;
	}

	.plan-cta {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 11px 16px;
		border-radius: 10px;
		text-decoration: none;
		font-weight: 600;
		font-size: 0.9rem;
		background: rgba(15, 17, 23, 0.5);
		color: var(--text);
		border: 1px solid var(--border);
		margin-bottom: 24px;
		transition: border-color 0.15s, transform 0.15s, box-shadow 0.15s;
	}
	.plan-cta:hover {
		border-color: var(--accent);
	}
	.plan-cta.primary {
		background: var(--accent-strong);
		color: #fff;
		border-color: var(--accent-strong);
		box-shadow: 0 12px 30px -12px rgba(99, 140, 255, 0.9);
	}
	.plan-cta.primary:hover {
		transform: translateY(-1px);
		box-shadow: 0 16px 34px -12px rgba(99, 140, 255, 1);
	}

	.plan-features {
		list-style: none;
		padding: 0;
		margin: 0 0 12px;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.plan-features li {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		color: var(--text);
		font-size: 0.84rem;
		line-height: 1.45;
	}
	.check {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		border-radius: 999px;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		flex-shrink: 0;
		margin-top: 2px;
	}

	.plan-foot {
		color: var(--text-muted);
		font-size: 0.78rem;
		line-height: 1.5;
		margin: 16px 0 0;
		padding-top: 14px;
		border-top: 1px solid var(--border);
	}

	.compare-note {
		margin-top: 40px;
		text-align: center;
		color: var(--text-muted);
		font-size: 0.88rem;
	}
	/* Underlined at rest (WCAG 1.4.1 Use of Color). It sits inside a sentence
	   of --text-muted, and --accent against that is 1.03:1 — colour alone did
	   not mark it as a link at all, so the underline is the only cue that can. */
	.compare-note a {
		color: var(--accent-on-tint);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.compare-note a:hover { color: var(--text); }

	/* ---------------------------- allowance ----------------------------- */
	.plan-allowance {
		list-style: none;
		padding: 0 0 18px;
		margin: 0 0 18px;
		border-bottom: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		gap: 6px;
		font-size: 0.86rem;
		line-height: 1.45;
	}
	.plan-allowance .strong {
		color: var(--text);
		font-weight: 600;
	}
	.plan-allowance .muted {
		color: var(--text-muted);
	}
	.features-lead {
		color: var(--text-muted);
		font-size: 0.8rem;
		font-weight: 600;
		margin: 0 0 10px;
	}

	/* ----------------------------- ai-read ------------------------------ */
	.ai-read {
		max-width: 760px;
		margin: 40px auto 0;
		padding: 20px 24px;
		border-radius: 14px;
		border: 1px solid var(--border);
		background: rgba(24, 26, 35, 0.62);
	}
	.ai-read h3 {
		font-size: 1rem;
		margin: 0 0 8px;
	}
	.ai-read p {
		color: var(--text-muted);
		font-size: 0.88rem;
		line-height: 1.6;
		margin: 0;
	}
	.compare-note p {
		margin: 0 0 8px;
	}
</style>
