<script lang="ts">
	import { asset } from '$app/paths';
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
	import AdapterRail from '$lib/components/marketing/AdapterRail.svelte';
	import Backdrop from '$lib/components/marketing/Backdrop.svelte';
	import HeroPipeline from '$lib/components/marketing/HeroPipeline.svelte';
	import MotionToggle from '$lib/components/marketing/MotionToggle.svelte';
	import { LEGAL_PAGES } from '$lib/legal/pages';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import { countUp, reveal } from '$lib/actions/reveal';

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

	// Every figure here must be checkable against the product by anyone who
	// bothers. A specific performance number is an objectively verifiable factual
	// claim, not puffery, so it needs substantiation we can produce on request.
	//
	// Two of these used to be inventions: "3.2s avg. extraction time" and "97%
	// field accuracy on typed invoices" had no benchmark, eval or fixture behind
	// them anywhere in the repo — and the only 97% in the tree is Basware's
	// published *touchless processing rate* quoted in docs/competitive-analysis.md,
	// a different metric belonging to a competitor. "12 workflow step types" was
	// 9 counted honestly (4 canonical + 5 builder; reaching 12 needed the three
	// backwards-compatible aliases counted as types of their own).
	//
	// These four are countable from the source: PAYMENT_METHODS, the locale
	// catalogues, CANONICAL_STEP_TYPES + BUILDER_STEP_TYPES, and the rebate rate
	// in api/cards.py (1% default, org-negotiated). If a number here stops
	// matching, change the number.
	//
	// `countUp` animates the leading integer and leaves anything else alone, so
	// `1–2%` is rendered, not counted to. The final value is what is written in
	// the markup — the animation replaces it and puts it back — so a visitor with
	// reduced motion, a crawler and a failed bundle all read the real figure.
	const stats = [
		{ value: '7', label: 'payment rails, ACH to CHAPS' },
		{ value: '9', label: 'workflow step types' },
		{ value: '6', label: 'languages, fully localized' },
		{ value: '1–2%', label: 'typical rebate on card payments' },
	];

	// Set by the in-page pause control (WCAG 2.2.2). `app.css` turns the
	// attribute into `animation-play-state: paused` for this whole subtree, so
	// the hero loop, the aurora and the adapter rail stop together.
	let motionPaused = $state(false);

	// Whether the page has scrolled past the hero's top edge, which is the only
	// thing the sticky header changes about itself. Kept as a boolean rather
	// than a scroll offset so the class flips once instead of on every frame.
	let scrolled = $state(false);
</script>

<svelte:window onscroll={() => (scrolled = window.scrollY > 12)} />

<div class="landing" data-motion={motionPaused ? 'paused' : 'running'}>
	<Backdrop fixed />

	<header class="nav" class:scrolled>
		<a href="/" class="brand">
			<BrandMark size={28} />
			<span class="brand-name">FeohLedger</span>
		</a>
		<nav class="nav-links">
			<a href="#features">Features</a>
			<a href="#how">How it works</a>
			<a href="#pricing">Pricing</a>
			<MotionToggle bind:paused={motionPaused} />
			<a href="/signup" class="nav-cta">
				<!-- Two labels, one shown per breakpoint. `display: none` on the
				     hidden one takes it out of the accessibility tree too, so the
				     link's name is always exactly the label in view. -->
				<span class="cta-long">Create workspace</span><span class="cta-short">Start free</span>
				<IconArrow aria-hidden="true" />
			</a>
		</nav>
	</header>

	<main class="content">
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

			<div class="hero-visual">
				<HeroPipeline />
			</div>
		</section>

		<section class="rail-section" use:reveal>
			<AdapterRail />
		</section>

		<section class="stats" use:reveal>
			{#each stats as stat, i}
				<div class="stat" use:reveal={{ delay: i * 70 }}>
					<div class="stat-value" use:countUp={{ value: stat.value }}>{stat.value}</div>
					<div class="stat-label">{stat.label}</div>
				</div>
			{/each}
		</section>

		<section id="features" class="features">
			<div class="section-head" use:reveal>
				<span class="eyebrow">Everything you need</span>
				<h2>One platform, from capture to payment.</h2>
				<p>
					Most teams stitch together extraction, approvals, matching, and ERP
					sync across four tools. FeohLedger is one tool that does all of it —
					with the pluggable pieces you'd expect.
				</p>
			</div>

			<div class="feature-grid">
				{#each features as feature, i}
					<div class="feature" use:reveal={{ delay: (i % 3) * 80, amount: 0.15 }}>
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
			<div class="section-head" use:reveal>
				<span class="eyebrow">How it works</span>
				<h2>Invoice to ledger, in three steps.</h2>
			</div>

			<div class="steps">
				{#each steps as step, i}
					<div class="step" use:reveal={{ delay: i * 90, amount: 0.15 }}>
						<!-- A watermark ordinal behind the card. `aria-hidden` because
						     that is only defensible if this really is decoration: the
						     ordinal is already carried by the cards' reading order, so
						     nothing is lost by hiding it. The fade is in the COLOUR
						     (an rgba), not an `opacity` on the element — group opacity
						     would composite the card's real text down with it, which
						     is the idiom src/lib/a11y/opacityAudit.test.ts exists to
						     keep out of this tree. -->
						<div class="step-num" aria-hidden="true">{i + 1}</div>
						<div class="step-icon"><step.icon /></div>
						<h3>{step.title}</h3>
						<p>{step.body}</p>
					</div>
				{/each}
			</div>
		</section>

		<!--
			The brand's own argument, made once and with the object in the room.
			The mark is feoh written as a split Exchequer tally (docs/decisions.md
			§173); this is a render of the thing it abstracts, generated by
			assets/marketing/gen-marketing.sh. It earns its place because the
			section's claim IS the tally's mechanism — two records that have to
			agree — rather than being a picture next to unrelated copy.
		-->
		<section class="tally">
			<div class="tally-intro">
				<div class="tally-copy" use:reveal>
					<span class="eyebrow">Why teams switch</span>
					<h2>Built for finance, not marketed at them.</h2>
					<p class="tally-lede">
						The Exchequer settled a debt by notching a stick, splitting it, and
						giving each party half — the debt was paid when the halves matched.
						That is still the job: an invoice against a purchase order, a payment
						against a statement. Everything here exists to make the halves meet.
					</p>
				</div>

				<div class="tally-art" use:reveal={{ delay: 120 }}>
					<img
						src={asset('/marketing/tally-split.webp')}
						width="1100"
						height="460"
						loading="lazy"
						decoding="async"
						alt="A notched gold tally stick split lengthwise into two halves, the notches on each half lining up with the other."
					/>
				</div>
			</div>

			<div class="diff-grid">
				<div class="diff" use:reveal={{ delay: 60 }}>
					<h3>AI you control</h3>
					<p>
						Use our platform key or bring your own — Claude, OpenAI, or Textract.
						Your prompt, your model, your data-retention policy. Self-host with
						Ollama if you need to.
					</p>
				</div>
				<div class="diff" use:reveal={{ delay: 120 }}>
					<h3>Pluggable everything</h3>
					<p>
						Adapter pattern for extraction, ERP, cards, and email. Swap providers
						without touching business logic. Ship a new ERP integration in a
						day, not a quarter.
					</p>
				</div>
				<div class="diff" use:reveal={{ delay: 180 }}>
					<h3>Cards that pay you back</h3>
					<p>
						Virtual card payments via Lithic or Nium earn 1–2% rebate. On a
						shop doing $500k/month in invoices, that's $60–120k/yr straight
						back to your budget.
					</p>
					<!-- A large, vivid dollar figure reads as a projection unless the
					     variability sits next to it. Pricing.svelte carries the same
					     qualifier, but it is a different component and a reader may
					     never scroll that far. -->
					<p class="diff-note">
						Illustrative. Rebates depend on your negotiated rate, how much spend
						moves to card, and which vendors accept it.
					</p>
				</div>
				<div class="diff" use:reveal={{ delay: 240 }}>
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

		<section class="cta-section" use:reveal>
			<div class="cta-inner">
				<h2>Spin up your workspace in 30 seconds.</h2>
				<p>
					Pick a slug, verify your email, and you're in. Free plan — no card,
					no contract, no sales call.
				</p>
				<a href="/signup" class="primary large">Create your workspace<IconArrow /></a>
			</div>
		</section>
	</main>

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
				<!--
					The legal set, reached from the marketing page because that is
					where an evaluating buyer, a supplier chasing their own data and a
					procurement reviewer all land first — and on the apex domain this
					footer is the only navigation that exists. Rendered from
					`LEGAL_PAGES` rather than hand-listed so a new document appears
					here without anyone remembering to add it.
				-->
				{#each LEGAL_PAGES as page (page.path)}
					<a href={page.path}>{page.title}</a>
				{/each}
			</div>
			<div class="footer-copy">
				© {new Date().getFullYear()} FeohLedger.
			</div>
		</div>
	</footer>
</div>

<style>
	/* Smooth anchor scrolling for the nav's in-page links — scoped to while this
	   page is mounted, so it never leaks into the app shell, where a smooth
	   scroll on every route change would feel sluggish. The global
	   reduced-motion rule in app.css sets `scroll-behavior: auto !important` on
	   every element, which overrides this for anyone who asked for less motion. */
	:global(html:has(.landing)) {
		scroll-behavior: smooth;
		/* The sticky header's height plus breathing room, declared on the SCROLL
		   CONTAINER rather than as a margin on chosen sections. It governs every
		   scroll the browser makes to reveal something — anchor jumps from the nav
		   AND keyboard focus moving onto a control. Without it, Shift+Tab onto a
		   pricing button parked it at y=20, entirely under a 74px header: WCAG
		   2.4.11 Focus Not Obscured. Per-section `scroll-margin-top` fixed only
		   the two anchors it was written on. */
		scroll-padding-top: 88px;
	}

	.landing {
		position: relative;
		min-height: 100vh;
		background: var(--bg);
		color: var(--text);
		overflow-x: clip;
	}
	/* Everything except the Backdrop sits above it. One rule rather than a
	   z-index on each section. */
	.content,
	.nav,
	.footer {
		position: relative;
		z-index: 1;
	}

	/* -------------------------------- nav -------------------------------- */
	.nav {
		position: sticky;
		top: 0;
		z-index: 40;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 18px 32px;
		/* The header spans the viewport; only its contents are held to the
		   page's measure, so the blur reaches the window edges on wide screens
		   instead of ending mid-air. */
		max-width: none;
		border-bottom: 1px solid transparent;
		transition: background 0.25s, border-color 0.25s, backdrop-filter 0.25s;
	}
	.nav.scrolled {
		background: rgba(15, 17, 23, 0.72);
		backdrop-filter: blur(14px) saturate(140%);
		border-bottom-color: var(--border);
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
		gap: 22px;
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
	/* Scoped under `.nav-links` on purpose. As a bare `.nav-cta` (0,1,0) this lost
	   to `.nav-links a` (0,1,1) above and rendered --text-muted on --accent-strong
	   — about 1.5:1. The previous version papered over the same tie with
	   `color: #fff !important`; matching the specificity is the fix, and it keeps
	   `!important` free for the reduced-motion and motion-toggle overrides that
	   genuinely need it. */
	.nav-links .nav-cta {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		background: var(--accent-strong);
		color: #fff;
		padding: 9px 17px;
		border-radius: 8px;
		font-weight: 500;
		box-shadow: 0 8px 22px -12px rgba(99, 140, 255, 0.9);
		transition: transform 0.15s, box-shadow 0.15s;
	}
	.nav-links .nav-cta:hover {
		color: #fff;
		transform: translateY(-1px);
		box-shadow: 0 12px 26px -12px rgba(99, 140, 255, 1);
	}
	.cta-short {
		display: none;
	}
	@media (max-width: 760px) {
		.nav {
			padding: 14px 20px;
		}
		.nav-links {
			gap: 12px;
		}
		.nav-links a:not(.nav-cta) { display: none; }
		.nav-links .nav-cta {
			padding: 8px 14px;
			white-space: nowrap;
		}
		.cta-long { display: none; }
		.cta-short { display: inline; }
	}
	/* WCAG 1.4.10 Reflow, at the criterion's own 320px. The mark, the motion
	   toggle and the short CTA are 13px wider than that viewport with the
	   wordmark beside them, and `.landing` clips horizontal overflow — so the
	   button's edge was cut off rather than scrollable. The wordmark goes, and
	   the mark alone carries the brand; its TEXT stays in the accessibility tree
	   (clipped, not `display: none`), because the mark image is decorative and
	   that text is the home link's only accessible name. */
	@media (max-width: 400px) {
		.nav {
			padding: 12px 14px;
		}
		.nav .brand-name {
			position: absolute;
			width: 1px;
			height: 1px;
			margin: -1px;
			overflow: hidden;
			clip-path: inset(50%);
			white-space: nowrap;
		}
	}

	/* ------------------------------- hero -------------------------------- */
	.hero {
		max-width: 1180px;
		margin: 26px auto 88px;
		padding: 36px 32px;
		display: grid;
		grid-template-columns: 1.05fr 0.95fr;
		gap: 64px;
		align-items: center;
	}
	@media (max-width: 960px) {
		.hero { grid-template-columns: 1fr; gap: 48px; margin-bottom: 64px; }
	}
	@media (max-width: 600px) {
		.hero { padding: 24px 20px; }
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
	h1 {
		margin: 0 0 24px;
		font-size: clamp(2.5rem, 5.4vw, 3.9rem);
		line-height: 1.04;
		letter-spacing: -0.03em;
		font-weight: 800;
	}
	h1 .accent {
		background: linear-gradient(100deg, #7d9bff 0%, #a37dff 42%, #e7b95e 100%);
		background-size: 220% 100%;
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
		/* A slow pass of the gradient across the words. It ends where it starts,
		   so the resting frame under reduced motion is the intended one. */
		animation: sheen 9s ease-in-out infinite;
	}
	@keyframes sheen {
		0%, 100% { background-position: 0% 50%; }
		50% { background-position: 100% 50%; }
	}
	.lede {
		font-size: 1.08rem;
		line-height: 1.62;
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
		padding: 13px 24px;
		border-radius: 10px;
		font-weight: 600;
		font-size: 0.95rem;
		text-decoration: none;
		transition: transform 0.15s, box-shadow 0.15s, border-color 0.15s;
	}
	.primary {
		background: var(--accent-strong);
		color: #fff;
		box-shadow: 0 14px 34px -14px rgba(99, 140, 255, 0.95);
	}
	.primary:hover {
		transform: translateY(-2px);
		box-shadow: 0 20px 42px -16px rgba(99, 140, 255, 1);
	}
	.primary.large { padding: 15px 30px; font-size: 1rem; }
	.secondary {
		background: rgba(24, 26, 35, 0.6);
		color: var(--text);
		border: 1px solid var(--border);
	}
	.secondary:hover { border-color: var(--accent); transform: translateY(-2px); }
	.sub-note {
		margin: 18px 0 0;
		color: var(--text-muted);
		font-size: 0.82rem;
	}

	.hero-visual {
		position: relative;
	}

	/* ------------------------------ sections ----------------------------- */
	.rail-section {
		margin: 0 auto 88px;
	}

	.stats {
		/* `width: min(…)` rather than `max-width` + `margin: auto`: the panel is
		   bordered, so unlike the unbordered sections it needs a gutter OUTSIDE
		   itself, and without one it ran edge to edge on a phone with its border
		   cut off by the viewport. */
		width: min(1116px, calc(100% - 40px));
		margin: 0 auto 110px;
		padding: 30px 32px;
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 20px;
		border-radius: 18px;
		border: 1px solid var(--border);
		background: rgba(24, 26, 35, 0.4);
	}
	@media (max-width: 760px) {
		.stats { grid-template-columns: repeat(2, 1fr); margin-bottom: 80px; }
	}
	.stat {
		text-align: center;
		padding: 14px 8px;
	}
	.stat-value {
		font-size: clamp(2rem, 3.4vw, 2.5rem);
		font-weight: 800;
		letter-spacing: -0.03em;
		font-variant-numeric: tabular-nums;
		background: linear-gradient(135deg, #7d9bff, #a37dff);
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
	}
	.stat-label {
		font-size: 0.8rem;
		color: var(--text-muted);
		margin-top: 6px;
	}

	.section-head {
		max-width: 720px;
		margin: 0 auto 52px;
		text-align: center;
		padding: 0 24px;
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

	/* ------------------------------ features ----------------------------- */
	.features {
		max-width: 1180px;
		margin: 0 auto 110px;
		padding: 20px 32px;
	}
	.feature-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 18px;
	}
	@media (max-width: 960px) {
		.feature-grid { grid-template-columns: repeat(2, 1fr); }
	}
	@media (max-width: 620px) {
		.feature-grid { grid-template-columns: 1fr; }
	}
	.feature {
		position: relative;
		border-radius: 14px;
		padding: 24px;
		background: rgba(24, 26, 35, 0.62);
		border: 1px solid var(--border);
		transition: border-color 0.2s, transform 0.2s, background 0.2s;
	}
	/* A hairline of accent along the top edge, brightening on hover. A
	   pseudo-element rather than a border so it can be inset from the corners
	   and not fight the card's own radius. */
	.feature::before {
		content: '';
		position: absolute;
		top: -1px;
		left: 18%;
		right: 18%;
		height: 1px;
		background: linear-gradient(90deg, transparent, rgba(99, 140, 255, 0.55), transparent);
		transition: left 0.3s, right 0.3s;
	}
	.feature:hover {
		border-color: rgba(99, 140, 255, 0.4);
		background: rgba(30, 33, 45, 0.75);
		transform: translateY(-3px);
	}
	.feature:hover::before {
		left: 4%;
		right: 4%;
	}
	.feature-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		border-radius: 12px;
		background: linear-gradient(140deg, rgba(99, 140, 255, 0.22), rgba(163, 125, 255, 0.14));
		border: 1px solid rgba(99, 140, 255, 0.22);
		color: var(--accent-on-tint);
		font-size: 22px;
		margin-bottom: 16px;
	}
	.feature h3 {
		font-size: 1.02rem;
		font-weight: 700;
		margin: 0 0 8px;
	}
	.feature p {
		color: var(--text-muted);
		font-size: 0.88rem;
		line-height: 1.6;
		margin: 0;
	}

	/* ------------------------------- how --------------------------------- */
	.how {
		max-width: 1180px;
		margin: 0 auto 110px;
		padding: 20px 32px;
	}
	.steps {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 20px;
	}
	@media (max-width: 800px) {
		.steps { grid-template-columns: 1fr; }
	}
	.step {
		position: relative;
		overflow: hidden;
		border-radius: 14px;
		padding: 28px 24px;
		background: rgba(24, 26, 35, 0.62);
		border: 1px solid var(--border);
	}
	.step-num {
		position: absolute;
		top: 12px;
		right: 20px;
		font-size: 4.2rem;
		font-weight: 800;
		line-height: 1;
		/* The fade lives in the colour, not in `opacity` — see the markup. */
		color: rgba(226, 228, 234, 0.07);
	}
	.step-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 46px;
		height: 46px;
		border-radius: 12px;
		background: linear-gradient(135deg, rgba(99, 140, 255, 0.22), rgba(163, 125, 255, 0.18));
		border: 1px solid rgba(99, 140, 255, 0.22);
		color: var(--accent-on-tint);
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
		line-height: 1.6;
		margin: 0;
	}

	/* ------------------------------- tally ------------------------------- */
	.tally {
		max-width: 1180px;
		margin: 0 auto 110px;
		padding: 20px 32px;
	}
	.tally-intro {
		display: grid;
		grid-template-columns: 0.9fr 1.1fr;
		gap: 48px;
		align-items: center;
		margin-bottom: 48px;
	}
	@media (max-width: 900px) {
		.tally-intro { grid-template-columns: 1fr; gap: 28px; }
	}
	.tally-copy h2 {
		font-size: clamp(1.7rem, 3.6vw, 2.4rem);
		font-weight: 700;
		letter-spacing: -0.025em;
		margin: 0 0 16px;
	}
	.tally-lede {
		color: var(--text-muted);
		line-height: 1.7;
		font-size: 1rem;
		margin: 0;
	}
	.tally-art {
		position: relative;
	}
	.tally-art img {
		display: block;
		width: 100%;
		height: auto;
		/* The render is lit from above-left on a transparent film, so a warm
		   bloom under it reads as the object's own light on the page rather
		   than as a box around a picture. */
		filter: drop-shadow(0 26px 46px rgba(231, 185, 94, 0.14))
			drop-shadow(0 8px 18px rgba(0, 0, 0, 0.55));
	}
	.diff-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 18px;
	}
	@media (max-width: 760px) {
		.diff-grid { grid-template-columns: 1fr; }
	}
	.diff {
		border-radius: 14px;
		padding: 26px;
		background: rgba(24, 26, 35, 0.62);
		border: 1px solid var(--border);
		transition: border-color 0.2s;
	}
	.diff:hover {
		border-color: rgba(99, 140, 255, 0.32);
	}
	.diff h3 {
		margin: 0 0 10px;
		font-size: 1.05rem;
		font-weight: 700;
	}
	.diff p {
		color: var(--text-muted);
		line-height: 1.6;
		margin: 0;
		font-size: 0.9rem;
	}
	.diff .diff-note {
		margin-top: 12px;
		font-size: 0.8rem;
	}

	/* ---------------------------- cta-section ---------------------------- */
	.cta-section {
		max-width: 1180px;
		margin: 0 auto 100px;
		padding: 0 32px;
	}
	.cta-inner {
		position: relative;
		overflow: hidden;
		border-radius: 22px;
		padding: 64px 40px;
		text-align: center;
		background:
			radial-gradient(640px 320px at 50% 0%, rgba(99, 140, 255, 0.26), transparent 70%),
			linear-gradient(180deg, rgba(31, 35, 48, 0.9), rgba(20, 22, 31, 0.9));
		border: 1px solid rgba(99, 140, 255, 0.22);
	}
	.cta-inner h2 {
		font-size: clamp(1.7rem, 3.6vw, 2.4rem);
		font-weight: 700;
		letter-spacing: -0.025em;
		margin: 0 0 14px;
	}
	.cta-inner p {
		color: var(--text-muted);
		line-height: 1.62;
		margin: 0 auto 26px;
		max-width: 520px;
	}

	/* ------------------------------ footer ------------------------------- */
	.footer {
		border-top: 1px solid var(--border);
		background: rgba(15, 17, 23, 0.6);
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
		flex-wrap: wrap;
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
