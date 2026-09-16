<script lang="ts">
	import IconExtract from '~icons/material-symbols/document-scanner-outline';
	import IconApprove from '~icons/material-symbols/fact-check-outline';
	import IconMatch from '~icons/material-symbols/join-inner';
	import IconErp from '~icons/material-symbols/sync-alt';
	import IconCard from '~icons/material-symbols/credit-card-outline';
	import IconAlert from '~icons/material-symbols/error-outline';
	import IconUpload from '~icons/material-symbols/upload-file-outline';
	import IconBolt from '~icons/material-symbols/bolt';
	import IconCheck from '~icons/material-symbols/check-circle-outline';
	import IconArrow from '~icons/material-symbols/arrow-forward';
	import Pricing from '$lib/components/marketing/Pricing.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';

	const features = [
		{
			icon: IconExtract,
			title: 'AI extraction',
			body: 'Drop in a PDF or photo. Claude, GPT-4V, or Textract pulls every field and line item with per-field confidence scores. Platform keys or bring your own.',
		},
		{
			icon: IconApprove,
			title: 'Approval workflows',
			body: 'Drag-and-drop step builder. Route by amount, vendor, or GL code. RBAC, audit trail, and Slack-style mobile approvals out of the box.',
		},
		{
			icon: IconMatch,
			title: '2-way + 3-way PO matching',
			body: 'Automatic match against POs and goods receipts with configurable tolerance. Mismatches land in the exception queue, not your inbox.',
		},
		{
			icon: IconErp,
			title: 'Real ERP sync',
			body: 'Push approved invoices to NetSuite, Dynamics 365, or anything Merge.dev supports. Status syncs back — ERP stays the source of truth.',
		},
		{
			icon: IconCard,
			title: 'Virtual card payments',
			body: 'Issue single-use cards per invoice via Lithic or Nium. Earn 1–2% rebate on every payment. Cards fund themselves.',
		},
		{
			icon: IconAlert,
			title: 'Exception queue',
			body: 'Duplicates, fraud flags, amount breaches, unverified vendors — all surfaced, assigned, and resolvable without digging through email.',
		},
	];

	const steps = [
		{
			icon: IconUpload,
			title: 'Capture',
			body: 'Email, upload, drag-drop, or snap a photo on mobile. Works with PDF, PNG, JPG, and scanned receipts.',
		},
		{
			icon: IconBolt,
			title: 'Extract + route',
			body: 'AI pulls vendor, amount, GL code, and line items. Your workflow routes it to the right approver automatically.',
		},
		{
			icon: IconCheck,
			title: 'Pay + sync',
			body: 'Approved invoices go to ERP, schedule for payment, and pay via ACH, wire, check, or rebate-earning virtual card.',
		},
	];

	const stats = [
		{ value: '3.2s', label: 'avg. extraction time' },
		{ value: '97%', label: 'field accuracy on typed invoices' },
		{ value: '1–2%', label: 'rebate on card payments' },
		{ value: '12', label: 'workflow step types' },
	];
</script>

<div class="landing">
	<header class="nav">
		<a href="/" class="brand">
			<BrandMark size={28} />
			<span class="brand-name">FeohLedger</span>
		</a>
		<nav class="nav-links">
			<a href="#features">Features</a>
			<a href="#how">How it works</a>
			<a href="#pricing">Pricing</a>
			<a href="/signup" class="nav-cta">Create workspace <IconArrow /></a>
		</nav>
	</header>

	<section class="hero">
		<div class="hero-text">
			<span class="eyebrow">Accounts payable, automated</span>
			<h1>
				AP automation<br />
				your finance team<br />
				<span class="accent">will actually use.</span>
			</h1>
			<p class="lede">
				From invoice to payment in minutes, not days. AI-powered extraction,
				configurable approval workflows, and real ERP sync — in one place,
				with rebates on every card payment.
			</p>
			<div class="cta-row">
				<a href="/signup" class="primary">Start free workspace<IconArrow /></a>
				<a href="#how" class="secondary">See how it works</a>
			</div>
			<p class="sub-note">
				Free to start · No credit card · Provision in 30 seconds
			</p>
		</div>

		<div class="hero-visual" aria-hidden="true">
			<div class="mock-card">
				<div class="mock-header">
					<Badge tone="success">Ready for review</Badge>
					<div class="mock-conf">Confidence <strong>96%</strong></div>
				</div>
				<div class="mock-title">INV-2026-00418</div>
				<div class="mock-vendor">Northwind Suppliers Ltd</div>
				<div class="mock-rows">
					<div class="mock-row"><span>Amount</span><strong>$12,480.00</strong></div>
					<div class="mock-row"><span>Due</span><strong>Apr 29, 2026</strong></div>
					<div class="mock-row"><span>PO match</span><strong class="ok">3-way · 100%</strong></div>
					<div class="mock-row"><span>GL suggestion</span><strong>5200 · Cost of goods</strong></div>
				</div>
				<div class="mock-actions">
					<button class="mock-approve">Approve</button>
					<button class="mock-reject">Reject</button>
				</div>
			</div>
			<div class="mock-bg-1"></div>
			<div class="mock-bg-2"></div>
		</div>
	</section>

	<section class="stats">
		{#each stats as stat}
			<div class="stat">
				<div class="stat-value">{stat.value}</div>
				<div class="stat-label">{stat.label}</div>
			</div>
		{/each}
	</section>

	<section id="features" class="features">
		<div class="section-head">
			<span class="eyebrow">Everything you need</span>
			<h2>One platform, from capture to payment.</h2>
			<p>
				Most teams stitch together extraction, approvals, matching, and ERP
				sync across four tools. FeohLedger is one tool that does all of it —
				with the pluggable pieces you'd expect.
			</p>
		</div>

		<div class="feature-grid">
			{#each features as feature}
				<div class="feature">
					<div class="feature-icon">
						<feature.icon />
					</div>
					<h3>{feature.title}</h3>
					<p>{feature.body}</p>
				</div>
			{/each}
		</div>
	</section>

	<section id="how" class="how">
		<div class="section-head">
			<span class="eyebrow">How it works</span>
			<h2>Invoice to ledger, in three steps.</h2>
		</div>

		<div class="steps">
			{#each steps as step, i}
				<div class="step">
					<!-- A watermark ordinal behind the card, faded to 12%.
					     `aria-hidden` because that fade is only defensible if this
					     really is decoration: the ordinal is already carried by the
					     cards' reading order, so nothing is lost by hiding it — and
					     hiding it turns the WCAG 1.4.3 decoration exemption into a
					     statement rather than an assumption. Guard:
					     src/lib/a11y/opacityAudit.test.ts -->
					<div class="step-num" aria-hidden="true">{i + 1}</div>
					<div class="step-icon"><step.icon /></div>
					<h3>{step.title}</h3>
					<p>{step.body}</p>
				</div>
			{/each}
		</div>
	</section>

	<section class="differentiators">
		<div class="section-head">
			<span class="eyebrow">Why teams switch</span>
			<h2>Built for finance, not marketed at them.</h2>
		</div>

		<div class="diff-grid">
			<div class="diff">
				<h3>AI you control</h3>
				<p>
					Use our platform key or bring your own — Claude, OpenAI, or Textract.
					Your prompt, your model, your data-retention policy. Self-host with
					Ollama if you need to.
				</p>
			</div>
			<div class="diff">
				<h3>Pluggable everything</h3>
				<p>
					Adapter pattern for extraction, ERP, cards, and email. Swap providers
					without touching business logic. Ship a new ERP integration in a
					day, not a quarter.
				</p>
			</div>
			<div class="diff">
				<h3>Cards that pay you back</h3>
				<p>
					Virtual card payments via Lithic or Nium earn 1–2% rebate. On a
					shop doing $500k/month in invoices, that's $60–120k/yr straight
					back to your budget.
				</p>
			</div>
			<div class="diff">
				<h3>Mobile without compromise</h3>
				<p>
					Native iOS and Android with camera OCR, biometric login,
					swipe-to-approve, and offline mode. Approvers unblock AP from
					an airport Wi-Fi.
				</p>
			</div>
		</div>
	</section>

	<Pricing />

	<section class="cta-section">
		<div class="cta-inner">
			<h2>Spin up your workspace in 30 seconds.</h2>
			<p>
				Pick a slug, verify your email, and you're in. Free plan — no card,
				no contract, no sales call.
			</p>
			<a href="/signup" class="primary large">Create your workspace<IconArrow /></a>
		</div>
	</section>

	<footer class="footer">
		<div class="footer-inner">
			<div class="footer-brand">
				<BrandMark size={22} />
				<span class="brand-name">FeohLedger</span>
			</div>
			<div class="footer-links">
				<a href="/signup">Sign up</a>
				<a href="#features">Features</a>
				<a href="#how">How it works</a>
			</div>
			<div class="footer-copy">
				© {new Date().getFullYear()} FeohLedger.
			</div>
		</div>
	</footer>
</div>

<style>
	.landing {
		min-height: 100vh;
		background:
			radial-gradient(1200px 600px at 80% -10%, rgba(99, 140, 255, 0.18), transparent 60%),
			radial-gradient(900px 500px at -10% 30%, rgba(129, 99, 255, 0.12), transparent 60%),
			var(--bg);
		color: var(--text);
	}

	/* -------------------------------- nav -------------------------------- */
	.nav {
		display: flex;
		align-items: center;
		justify-content: space-between;
		max-width: 1180px;
		margin: 0 auto;
		padding: 24px 32px;
	}
	.brand {
		display: inline-flex;
		align-items: center;
		gap: 10px;
		text-decoration: none;
		color: var(--text);
	}
	.brand-name {
		font-weight: 700;
		font-size: 1rem;
		letter-spacing: -0.01em;
	}
	.nav-links {
		display: flex;
		align-items: center;
		gap: 28px;
	}
	.nav-links a {
		color: var(--text-muted);
		text-decoration: none;
		font-size: 0.9rem;
		transition: color 0.15s;
	}
	.nav-links a:hover {
		color: var(--text);
	}
	.nav-cta {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		background: var(--accent-strong);
		color: #fff !important;
		padding: 8px 16px;
		border-radius: 6px;
		font-weight: 500;
	}
	.nav-cta:hover {
		opacity: 0.92;
	}
	@media (max-width: 720px) {
		.nav-links a:not(.nav-cta) { display: none; }
	}

	/* ------------------------------- hero -------------------------------- */
	.hero {
		max-width: 1180px;
		margin: 40px auto 80px;
		padding: 40px 32px;
		display: grid;
		grid-template-columns: 1.1fr 1fr;
		gap: 60px;
		align-items: center;
	}
	@media (max-width: 960px) {
		.hero { grid-template-columns: 1fr; gap: 40px; }
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
	h1 {
		margin: 0 0 24px;
		font-size: clamp(2.4rem, 5vw, 3.6rem);
		line-height: 1.05;
		letter-spacing: -0.02em;
		font-weight: 800;
	}
	h1 .accent {
		background: linear-gradient(135deg, var(--accent), #a37dff);
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
	}
	.lede {
		font-size: 1.05rem;
		line-height: 1.6;
		color: var(--text-muted);
		max-width: 540px;
		margin: 0 0 32px;
	}
	.cta-row {
		display: flex;
		gap: 12px;
		flex-wrap: wrap;
	}
	.primary,
	.secondary {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		padding: 12px 22px;
		border-radius: 8px;
		font-weight: 500;
		font-size: 0.95rem;
		text-decoration: none;
		transition: transform 0.1s, opacity 0.15s;
	}
	.primary {
		background: var(--accent-strong);
		color: #fff;
		box-shadow: 0 8px 28px -8px rgba(99, 140, 255, 0.6);
	}
	.primary:hover { opacity: 0.92; transform: translateY(-1px); }
	.primary.large { padding: 14px 28px; font-size: 1rem; }
	.secondary {
		background: transparent;
		color: var(--text);
		border: 1px solid var(--border);
	}
	.secondary:hover { border-color: var(--text-muted); }
	.sub-note {
		margin: 16px 0 0;
		color: var(--text-muted);
		font-size: 0.82rem;
	}

	/* --------------------------- hero visual ----------------------------- */
	.hero-visual {
		position: relative;
		min-height: 380px;
	}
	.mock-card {
		position: relative;
		z-index: 2;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 12px;
		padding: 20px;
		box-shadow: 0 40px 80px -20px rgba(0, 0, 0, 0.5);
	}
	.mock-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 14px;
	}
	.mock-conf {
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.mock-conf strong {
		color: var(--text);
	}
	.mock-title {
		font-size: 1.05rem;
		font-weight: 700;
		margin-bottom: 4px;
	}
	.mock-vendor {
		color: var(--text-muted);
		font-size: 0.88rem;
		margin-bottom: 16px;
	}
	.mock-rows {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 12px 0;
		border-top: 1px solid var(--border);
		border-bottom: 1px solid var(--border);
		margin-bottom: 14px;
	}
	.mock-row {
		display: flex;
		justify-content: space-between;
		font-size: 0.85rem;
	}
	.mock-row span { color: var(--text-muted); }
	.mock-row strong.ok { color: #5bd798; }
	.mock-actions {
		display: flex;
		gap: 8px;
	}
	.mock-approve,
	.mock-reject {
		flex: 1;
		padding: 8px;
		border-radius: 6px;
		border: none;
		font-weight: 500;
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}
	.mock-approve {
		background: var(--accent-strong);
		color: #fff;
	}
	.mock-reject {
		background: transparent;
		color: var(--text-muted);
		border: 1px solid var(--border);
	}
	.mock-bg-1,
	.mock-bg-2 {
		position: absolute;
		inset: 0;
		border-radius: 12px;
		background: var(--surface);
		border: 1px solid var(--border);
		opacity: 0.5;
	}
	.mock-bg-1 {
		z-index: 1;
		transform: translate(16px, 20px) rotate(3deg);
	}
	.mock-bg-2 {
		z-index: 0;
		transform: translate(32px, 40px) rotate(6deg);
		opacity: 0.3;
	}

	/* ------------------------------- stats ------------------------------- */
	.stats {
		max-width: 1180px;
		margin: 0 auto 100px;
		padding: 32px;
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 24px;
		border-top: 1px solid var(--border);
		border-bottom: 1px solid var(--border);
	}
	@media (max-width: 720px) {
		.stats { grid-template-columns: repeat(2, 1fr); }
	}
	.stat {
		text-align: center;
		padding: 16px 8px;
	}
	.stat-value {
		font-size: 2rem;
		font-weight: 800;
		letter-spacing: -0.02em;
		background: linear-gradient(135deg, var(--accent), #a37dff);
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
	}
	.stat-label {
		font-size: 0.8rem;
		color: var(--text-muted);
		margin-top: 4px;
	}

	/* --------------------------- sections head --------------------------- */
	.section-head {
		max-width: 720px;
		margin: 0 auto 48px;
		text-align: center;
		padding: 0 24px;
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
		margin: 0;
	}

	/* ------------------------------ features ----------------------------- */
	.features {
		max-width: 1180px;
		margin: 0 auto 100px;
		padding: 40px 32px;
	}
	.feature-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 20px;
	}
	@media (max-width: 960px) {
		.feature-grid { grid-template-columns: repeat(2, 1fr); }
	}
	@media (max-width: 600px) {
		.feature-grid { grid-template-columns: 1fr; }
	}
	.feature {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 24px;
		transition: border-color 0.15s, transform 0.15s;
	}
	.feature:hover {
		border-color: var(--accent);
		transform: translateY(-2px);
	}
	.feature-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		border-radius: 8px;
		background: rgba(99, 140, 255, 0.12);
		color: var(--accent);
		font-size: 22px;
		margin-bottom: 14px;
	}
	.feature h3 {
		font-size: 1rem;
		font-weight: 700;
		margin: 0 0 8px;
	}
	.feature p {
		color: var(--text-muted);
		font-size: 0.88rem;
		line-height: 1.55;
		margin: 0;
	}

	/* ------------------------------- how --------------------------------- */
	.how {
		max-width: 1180px;
		margin: 0 auto 100px;
		padding: 40px 32px;
	}
	.steps {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 24px;
	}
	@media (max-width: 760px) {
		.steps { grid-template-columns: 1fr; }
	}
	.step {
		position: relative;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 28px 24px;
	}
	.step-num {
		position: absolute;
		top: 20px;
		right: 20px;
		font-size: 2.4rem;
		font-weight: 800;
		opacity: 0.12;
		line-height: 1;
	}
	.step-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		border-radius: 10px;
		background: linear-gradient(135deg, rgba(99, 140, 255, 0.2), rgba(163, 125, 255, 0.2));
		color: var(--accent);
		font-size: 24px;
		margin-bottom: 16px;
	}
	.step h3 {
		margin: 0 0 8px;
		font-size: 1.05rem;
		font-weight: 700;
	}
	.step p {
		color: var(--text-muted);
		font-size: 0.88rem;
		line-height: 1.55;
		margin: 0;
	}

	/* -------------------------- differentiators -------------------------- */
	.differentiators {
		max-width: 1180px;
		margin: 0 auto 100px;
		padding: 40px 32px;
	}
	.diff-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 20px;
	}
	@media (max-width: 720px) {
		.diff-grid { grid-template-columns: 1fr; }
	}
	.diff {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 28px;
	}
	.diff h3 {
		margin: 0 0 10px;
		font-size: 1.05rem;
		font-weight: 700;
	}
	.diff p {
		color: var(--text-muted);
		line-height: 1.55;
		margin: 0;
		font-size: 0.9rem;
	}

	/* ---------------------------- cta-section ---------------------------- */
	.cta-section {
		max-width: 1180px;
		margin: 0 auto 100px;
		padding: 0 32px;
	}
	.cta-inner {
		background:
			radial-gradient(600px 300px at 50% 0%, rgba(99, 140, 255, 0.22), transparent 70%),
			var(--surface);
		border: 1px solid var(--border);
		border-radius: 16px;
		padding: 60px 40px;
		text-align: center;
	}
	.cta-inner h2 {
		font-size: clamp(1.6rem, 3.5vw, 2.2rem);
		font-weight: 700;
		letter-spacing: -0.02em;
		margin: 0 0 14px;
	}
	.cta-inner p {
		color: var(--text-muted);
		line-height: 1.6;
		margin: 0 0 24px;
		max-width: 520px;
		margin-left: auto;
		margin-right: auto;
	}

	/* ------------------------------ footer ------------------------------- */
	.footer {
		border-top: 1px solid var(--border);
	}
	.footer-inner {
		max-width: 1180px;
		margin: 0 auto;
		padding: 28px 32px;
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: 16px;
	}
	.footer-brand {
		display: inline-flex;
		align-items: center;
		gap: 10px;
		font-weight: 700;
	}
	.footer-links {
		display: flex;
		gap: 20px;
	}
	.footer-links a {
		color: var(--text-muted);
		text-decoration: none;
		font-size: 0.88rem;
	}
	.footer-links a:hover {
		color: var(--text);
	}
	.footer-copy {
		color: var(--text-muted);
		font-size: 0.82rem;
	}
</style>
