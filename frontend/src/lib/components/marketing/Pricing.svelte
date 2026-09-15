<script lang="ts">
	import IconArrow from '~icons/material-symbols/arrow-forward';
	import IconCheck from '~icons/material-symbols/check-small';

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
			ctaLabel: 'Start 14-day trial',
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
				'Priority email + chat support',
			],
			footnote: 'Rebates on card payments typically offset the subscription.',
		},
		{
			name: 'Enterprise',
			tagline: 'For finance orgs at scale.',
			priceMonthly: null,
			priceAnnual: null,
			unit: 'contact sales',
			ctaLabel: 'Talk to us',
			ctaHref: 'mailto:sales@feohledger.example',
			features: [
				'Everything in Pro',
				'SSO (SAML + OIDC) with SCIM provisioning',
				'BYOK for all providers — your data, your keys',
				'Dedicated tenant cluster option',
				'99.9% uptime SLA',
				'Named customer success manager',
				'Security review + SOC 2 attestation',
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
	<div class="section-head">
		<span class="eyebrow">Pricing</span>
		<h2>Simple plans. No sales call required.</h2>
		<p>
			Start free, upgrade when the rebates start covering the bill. Every plan
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
		{#each plans as plan}
			<div class="plan" class:featured={plan.featured}>
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
		Need usage-based? Hitting 10k+ invoices a month? <a href="mailto:sales@feohledger.example"
			>Ask about volume pricing</a
		>.
	</div>
</section>

<style>
	.pricing {
		max-width: 1180px;
		margin: 0 auto 100px;
		padding: 40px 32px;
	}

	.section-head {
		max-width: 720px;
		margin: 0 auto 48px;
		text-align: center;
	}
	.eyebrow {
		display: inline-block;
		font-size: 0.78rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--accent);
		margin-bottom: 16px;
	}
	.section-head h2 {
		font-size: clamp(1.6rem, 3.5vw, 2.2rem);
		font-weight: 700;
		letter-spacing: -0.02em;
		margin: 0 0 14px;
	}
	.section-head p {
		color: var(--text-muted);
		line-height: 1.6;
		margin: 0 0 28px;
	}

	/* ------------------------------ toggle ------------------------------ */
	.toggle {
		display: inline-flex;
		padding: 4px;
		background: var(--surface);
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
		transition: color 0.15s, background 0.15s;
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.toggle button.active {
		background: var(--accent-strong);
		color: #fff;
	}
	.save {
		font-size: 0.7rem;
		padding: 2px 6px;
		border-radius: 4px;
		background: rgba(255, 255, 255, 0.2);
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
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 12px;
		padding: 28px 22px;
		display: flex;
		flex-direction: column;
		transition: border-color 0.2s, transform 0.2s;
	}
	.plan:hover { border-color: var(--text-muted); }
	.plan.featured {
		border-color: var(--accent);
		background:
			radial-gradient(400px 200px at 50% 0%, rgba(99, 140, 255, 0.12), transparent 70%),
			var(--surface);
		box-shadow: 0 20px 60px -20px rgba(99, 140, 255, 0.25);
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
	}

	.plan-name {
		font-size: 1rem;
		font-weight: 700;
		letter-spacing: 0.02em;
		text-transform: uppercase;
		color: var(--text-muted);
		margin-bottom: 6px;
	}
	.plan.featured .plan-name { color: var(--accent); }
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
		font-size: 2.2rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}
	.plan.featured .amount {
		background: linear-gradient(135deg, var(--accent), #a37dff);
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
		border-radius: 8px;
		text-decoration: none;
		font-weight: 500;
		font-size: 0.9rem;
		background: transparent;
		color: var(--text);
		border: 1px solid var(--border);
		margin-bottom: 24px;
		transition: background 0.15s, border-color 0.15s;
	}
	.plan-cta:hover {
		border-color: var(--text-muted);
	}
	.plan-cta.primary {
		background: var(--accent-strong);
		color: #fff;
		border-color: var(--accent-strong);
		box-shadow: 0 8px 28px -8px rgba(99, 140, 255, 0.6);
	}
	.plan-cta.primary:hover { opacity: 0.92; }

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
	.compare-note a {
		color: var(--accent);
		text-decoration: none;
	}
	.compare-note a:hover { text-decoration: underline; }
</style>
