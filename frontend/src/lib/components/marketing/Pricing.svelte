<script lang="ts">
	import IconArrow from '~icons/material-symbols/arrow-forward';
	import IconCheck from '~icons/material-symbols/check-small';
	import { CONTACT } from '$lib/legal/operator';
	import { reveal } from '$lib/actions/reveal';

	type Billing = 'monthly' | 'annual';
	let billing = $state<Billing>('annual');

	interface Plan {
		name: string;
		tagline: string;
		priceMonthly: number | null; // null = custom / contact
		priceAnnual: number | null;
		unit: string;
		ctaLabel: string;
		ctaHref: string;
		featured?: boolean;
		features: string[];
		footnote?: string;
	}

	const plans: Plan[] = [
		{
			name: 'Self-hosted',
			tagline: 'Run it yourself. Source is open.',
			priceMonthly: 0,
			priceAnnual: 0,
			unit: 'on your infrastructure',
			ctaLabel: 'View on GitHub',
			ctaHref: 'https://github.com/Absence0760/feohledger',
			features: [
				'Full source — backend, frontend, mobile',
				'Unlimited invoices + seats',
				'All extraction + ERP + card adapters',
				'Your database, your cloud, your keys',
				'Community support via GitHub issues',
				'Docker Compose for local, Terraform for AWS',
			],
			footnote: 'You bring the infrastructure and operate it yourself.',
		},
		{
			name: 'Free',
			tagline: 'Hosted. For solo ops and trials.',
			priceMonthly: 0,
			priceAnnual: 0,
			unit: 'forever',
			ctaLabel: 'Start free',
			ctaHref: '/signup',
			features: [
				'Up to 50 invoices / month',
				'2 seats',
				'AI extraction on platform keys',
				'Standard approval workflow',
				'CSV export',
				'Community support',
			],
		},
		{
			name: 'Pro',
			tagline: 'For growing teams with real AP volume.',
			priceMonthly: 29,
			priceAnnual: 24,
			unit: 'per seat / month',
			// Was 'Start 14-day trial'. It routes to the same `/signup` the free
			// plan uses, and `tenant_provisioning._provision_into` binds EVERY new
			// org to the `free` plan regardless — there is no plan selection in
			// signup and no trial-flagged Subscription. The button could not do
			// what it said, which is a present-tense false statement rather than a
			// price that might change. Restore the trial wording when signup can
			// actually provision one.
			ctaLabel: 'Start free, upgrade any time',
			ctaHref: '/signup',
			featured: true,
			features: [
				'Unlimited invoices',
				'Unlimited seats (5-seat minimum)',
				'All extraction providers + BYOK',
				'Custom approval workflows + RBAC',
				'ERP sync (NetSuite, Dynamics, Merge.dev)',
				'2/3-way PO matching + exception queue',
				'Virtual card payments with rebates',
				'Mobile app (iOS + Android)',
				'Priority email support',
			],
			footnote: 'Virtual-card rebates vary by issuer, card spend and vendor acceptance.',
		},
		{
			name: 'Enterprise',
			tagline: 'For finance orgs at scale.',
			priceMonthly: null,
			priceAnnual: null,
			unit: 'contact sales',
			ctaLabel: 'Talk to us',
			ctaHref: `mailto:${CONTACT.sales}`,
			features: [
				'Everything in Pro',
				'SSO (SAML + OIDC) with SCIM provisioning',
				'BYOK for all providers — your data, your keys',
				'Dedicated tenant cluster option',
				'Uptime commitment by agreement',
				'Named customer success manager',
				'Support for your security review',
				'Custom data retention policy',
			],
		},
	];

	function priceFor(p: Plan): string {
		const v = billing === 'annual' ? p.priceAnnual : p.priceMonthly;
		if (v === null) return 'Custom';
		if (v === 0) return '$0';
		return `$${v}`;
	}
</script>

<section id="pricing" class="pricing">
	<div class="section-head" use:reveal>
		<span class="eyebrow">Pricing</span>
		<h2>Simple plans. No sales call required.</h2>
		<p>
			Start free and upgrade when the volume justifies it. Every plan
			includes the full AI extraction, approval workflow, and mobile app —
			you're paying for scale and integrations, not basic features.
		</p>

		<div class="toggle" role="tablist" aria-label="Billing period">
			<button
				class:active={billing === 'monthly'}
				onclick={() => (billing = 'monthly')}
				role="tab"
				aria-selected={billing === 'monthly'}
			>
				Monthly
			</button>
			<button
				class:active={billing === 'annual'}
				onclick={() => (billing = 'annual')}
				role="tab"
				aria-selected={billing === 'annual'}
			>
				Annual <span class="save">save 17%</span>
			</button>
		</div>
	</div>

	<div class="grid">
		{#each plans as plan, i}
			<div class="plan" class:featured={plan.featured} use:reveal={{ delay: i * 70, amount: 0.1 }}>
				{#if plan.featured}
					<div class="badge">Most popular</div>
				{/if}
				<div class="plan-name">{plan.name}</div>
				<div class="plan-tagline">{plan.tagline}</div>

				<div class="plan-price">
					<span class="amount">{priceFor(plan)}</span>
					<span class="unit">{plan.unit}</span>
				</div>

				<a class="plan-cta" class:primary={plan.featured} href={plan.ctaHref}>
					{plan.ctaLabel}
					<IconArrow />
				</a>

				<ul class="plan-features">
					{#each plan.features as feature}
						<li>
							<span class="check"><IconCheck /></span>
							<span>{feature}</span>
						</li>
					{/each}
				</ul>

				{#if plan.footnote}
					<p class="plan-foot">{plan.footnote}</p>
				{/if}
			</div>
		{/each}
	</div>

	<div class="compare-note">
		Need usage-based? Hitting 10k+ invoices a month? <a href="mailto:{CONTACT.sales}"
			>Ask about volume pricing</a
		>.
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
		margin: 0 0 30px;
	}

	/* ------------------------------ toggle ------------------------------ */
	.toggle {
		display: inline-flex;
		padding: 4px;
		background: rgba(24, 26, 35, 0.7);
		border: 1px solid var(--border);
		border-radius: 999px;
	}
	.toggle button {
		background: transparent;
		border: none;
		padding: 8px 18px;
		border-radius: 999px;
		color: var(--text-muted);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		transition: color 0.2s, background 0.2s, box-shadow 0.2s;
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.toggle button:hover:not(.active) {
		color: var(--text);
	}
	.toggle button.active {
		background: var(--accent-strong);
		color: #fff;
		box-shadow: 0 6px 18px -8px rgba(99, 140, 255, 0.9);
	}
	/* A DARKENING tint on the selected button, not a lightening one. The chip
	   was `rgba(255, 255, 255, 0.2)`, which lifts --accent-strong to #657fde and
	   puts its white label at 3.72:1 — the first axe scan this page ever had
	   caught it. Black at 0.24 takes the fill to ~#3048a3, where white is ~8.1:1. */
	.save {
		font-size: 0.7rem;
		padding: 2px 6px;
		border-radius: 4px;
		background: rgba(0, 0, 0, 0.24);
	}
	.toggle button:not(.active) .save {
		background: var(--accent-tint);
		color: var(--accent-on-tint);
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
	.badge {
		position: absolute;
		top: -12px;
		left: 50%;
		transform: translateX(-50%);
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.72rem;
		font-weight: 600;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		padding: 4px 14px;
		border-radius: 999px;
		white-space: nowrap;
		box-shadow: 0 6px 18px -6px rgba(99, 140, 255, 0.8);
	}

	.plan-name {
		font-size: 0.86rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
		margin-bottom: 8px;
	}
	.plan.featured .plan-name { color: var(--accent-on-tint); }
	.plan-tagline {
		color: var(--text);
		font-size: 0.92rem;
		margin-bottom: 24px;
		line-height: 1.45;
	}

	.plan-price {
		display: flex;
		align-items: baseline;
		gap: 8px;
		margin-bottom: 24px;
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
</style>
