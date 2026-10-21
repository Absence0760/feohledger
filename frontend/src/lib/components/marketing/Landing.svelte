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
	import IconMenu from '~icons/material-symbols/menu';
	import IconClose from '~icons/material-symbols/close';
	import Pricing from '#lib/components/marketing/Pricing.svelte';
	import AdapterRail from '#lib/components/marketing/AdapterRail.svelte';
	import Atmosphere from '#lib/components/marketing/Atmosphere.svelte';
	import HeroPipeline from '#lib/components/marketing/HeroPipeline.svelte';
	import { LEGAL_PAGES } from '#lib/legal/pages.ts';
	import BrandMark from '#lib/components/ui/BrandMark.svelte';
	import ThemePicker from '#lib/components/ui/ThemePicker.svelte';
	import { countUp, reveal } from '#lib/actions/reveal.ts';
	import { ensureSlices, m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { TALLY_ORNAMENT, TALLY_ORNAMENT_SIZE } from '#lib/marketing/ornament.ts';
	import { DEFAULT_PRODUCT_NAME } from '#lib/stores/brandTheme.ts';
	import { theme } from '#lib/stores/theme.svelte.ts';
	import { formatMoney } from '#lib/utils/money.ts';

	// The page's copy is the `marketing` catalogue slice, in the visitor's
	// language (decisions §261, §300). The root layout renders this component
	// in place of a route on the apex, so no `+layout.ts` load can fetch the
	// slice first; the page waits for it instead of drawing its keys raw or in
	// English under a `<html lang>` that says otherwise (#635 finding 10).
	// The layout already renders nothing until the host is classified, so the
	// wait is one small chunk added to a blank frame the visitor was already
	// looking at.
	let copyReady = $state(false);
	void ensureSlices('marketing').then(() => (copyReady = true));

	// The platform's own name: the apex has no tenant, so no brand to apply.
	const product = DEFAULT_PRODUCT_NAME;

	type Card = { icon: typeof IconExtract; title: MessageKey; body: MessageKey };

	const features: Card[] = [
		{ icon: IconExtract, title: 'marketing.features.extract.title', body: 'marketing.features.extract.body' },
		{ icon: IconApprove, title: 'marketing.features.approve.title', body: 'marketing.features.approve.body' },
		{ icon: IconMatch, title: 'marketing.features.match.title', body: 'marketing.features.match.body' },
		{ icon: IconErp, title: 'marketing.features.erp.title', body: 'marketing.features.erp.body' },
		{ icon: IconCard, title: 'marketing.features.card.title', body: 'marketing.features.card.body' },
		{ icon: IconAlert, title: 'marketing.features.exceptions.title', body: 'marketing.features.exceptions.body' }
	];

	const steps: Card[] = [
		{ icon: IconUpload, title: 'marketing.how.capture.title', body: 'marketing.how.capture.body' },
		{ icon: IconBolt, title: 'marketing.how.route.title', body: 'marketing.how.route.body' },
		{ icon: IconCheck, title: 'marketing.how.pay.title', body: 'marketing.how.pay.body' }
	];

	// Every figure on this page must be checkable against the product by anyone
	// who bothers: a specific number is an objectively verifiable factual claim,
	// not puffery. How each one is derived — and what has been withdrawn for
	// want of a derivation (the "3.2s" / "97%" statistics, "12 workflow step
	// types", "Provision in 30 seconds") — is recorded per figure in
	// docs/marketing-substantiation.md. Change a number here or in the
	// `marketing` catalogue slice and that file in the same commit; if the
	// source count moves, change the number. Prices and allowances are not
	// typed here or in Pricing.svelte at all: they are generated from the plan
	// catalogue (`pnpm gen:pricing`).
	//
	// `countUp` animates the leading integer and leaves anything else alone, so
	// `1–2%` is rendered, not counted to. The final value is what is written in
	// the markup — the animation replaces it and puts it back — so a visitor with
	// reduced motion, a crawler and a failed bundle all read the real figure.
	const stats = $derived([
		{ value: '12', label: m('marketing.stats.rails') },
		{ value: '9', label: m('marketing.stats.stepTypes') },
		{ value: '6', label: m('marketing.stats.languages') },
		{ value: m('marketing.stats.rebateValue'), label: m('marketing.stats.rebate') }
	]);

	// The rebate worked example (substantiation: $500,000 × 12 × 1–2%), in the
	// visitor's number format. Whole dollars: cents on an illustration would
	// claim a precision it does not have.
	const usd = (amount: string) => formatMoney(amount, { currency: 'USD', whole: true });
	const cardsBody = $derived(
		m('marketing.diff.cards.body', { monthly: usd('500000'), low: usd('60000'), high: usd('120000') })
	);

	// Whether the page has scrolled past the hero's top edge, which is the only
	// thing the sticky header changes about itself. Kept as a boolean rather
	// than a scroll offset so the class flips once instead of on every frame.
	let scrolled = $state(false);

	// The narrow header's disclosure menu (#635 finding 25). Below 1024px the
	// in-page links and the theme switch move into it; the signup call to
	// action stays in the bar, and so does Sign in down to 360px.
	let menuOpen = $state(false);
	let menuToggle = $state<HTMLButtonElement | null>(null);

	let header = $state<HTMLElement | null>(null);

	function closeMenu(returnFocus = false) {
		if (!menuOpen) return;
		menuOpen = false;
		if (returnFocus) menuToggle?.focus();
	}

	// The open panel is part of the sticky header, so it would cover whatever
	// focus or a tap moves to below it (WCAG 2.4.11). It closes as soon as
	// either leaves the header — without taking focus back.
	function onHeaderFocusOut(event: FocusEvent) {
		const next = event.relatedTarget as Node | null;
		if (next && !header?.contains(next)) closeMenu();
	}
	function onPointerDown(event: PointerEvent) {
		if (menuOpen && !header?.contains(event.target as Node)) closeMenu();
	}
	// Escape closes it from anywhere, and returns focus to the toggle only
	// when focus was in the menu — never pulling it up from the page.
	function onKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape' || !menuOpen) return;
		const active = document.activeElement;
		const inMenu = !!active && (active === menuToggle || !!document.getElementById('landing-menu')?.contains(active));
		closeMenu(inMenu);
	}
</script>

<svelte:window
	onscroll={() => (scrolled = window.scrollY > 12)}
	onkeydown={onKeydown}
	onpointerdown={onPointerDown}
/>

<!-- Follows the visitor's theme like every other surface (docs/decisions.md
     §306): every colour below is a palette token, and the one raster — the
     tally ornament — has a render lit for each ground. -->
{#if copyReady}
<div class="landing">
	<Atmosphere fixed />

	<header class="nav" class:scrolled bind:this={header} onfocusout={onHeaderFocusOut}>
		<a href="/" class="brand">
			<BrandMark size={28} />
			<span class="brand-name">{product}</span>
		</a>
		<div class="nav-end">
			<!-- The wide header's site links. Below 1024px this is `display: none`,
			     which takes it out of the accessibility tree too, and the same
			     links are in the menu panel below instead — so there is only ever
			     one "Main" navigation landmark to land on. -->
			<nav class="nav-links nav-wide" aria-label={m('marketing.nav.label')}>
				<a href="#features">{m('marketing.nav.features')}</a>
				<a href="#how">{m('marketing.nav.how')}</a>
				<a href="#pricing">{m('marketing.nav.pricing')}</a>
			</nav>
			<!-- The apex has no sign-in form of its own: each company signs in at
			     its own workspace host, so this asks which one (#635 finding 4). -->
			<a href="/find-workspace" class="nav-signin">{m('marketing.nav.signIn')}</a>
			<a href="/signup" class="nav-cta">
				<!-- Two labels, one shown per breakpoint. `display: none` on the
				     hidden one takes it out of the accessibility tree too, so the
				     link's name is always exactly the label in view. -->
				<span class="cta-long">{m('marketing.cta.create')}</span><span class="cta-short"
					>{m('marketing.cta.short')}</span
				>
				<IconArrow aria-hidden="true" />
			</a>
			<!-- An anonymous visitor has no /profile, so the theme is chosen here:
			     the auth pages' picker, drawn as three icons. Beside the <nav>, not
			     in it — it is a setting, not a destination. -->
			<div class="theme-wide">
				<ThemePicker name="landing-theme" compact />
			</div>
			<!-- A disclosure, not a menu widget: a button that shows and hides a
			     panel of ordinary links (WAI-ARIA APG "Disclosure Navigation
			     Menu"), so Tab walks it and no arrow-key model is owed. The panel
			     follows the button in the DOM, so opening it puts its links next
			     in the focus order. -->
			<button
				type="button"
				class="menu-toggle"
				bind:this={menuToggle}
				aria-expanded={menuOpen}
				aria-controls="landing-menu"
				onclick={() => (menuOpen = !menuOpen)}
			>
				{#if menuOpen}<IconClose aria-hidden="true" />{:else}<IconMenu aria-hidden="true" />{/if}
				<span class="visually-hidden">{m('marketing.nav.menu')}</span>
			</button>
		</div>
		<div id="landing-menu" class="menu-panel" hidden={!menuOpen}>
			<nav aria-label={m('marketing.nav.label')}>
				<a href="#features" onclick={() => closeMenu()}>{m('marketing.nav.features')}</a>
				<a href="#how" onclick={() => closeMenu()}>{m('marketing.nav.how')}</a>
				<a href="#pricing" onclick={() => closeMenu()}>{m('marketing.nav.pricing')}</a>
				<!-- Shown only below 360px, where the bar's own Sign in is hidden:
				     one of the two is ever on screen (or in the accessibility tree). -->
				<a href="/find-workspace" class="menu-signin">{m('marketing.nav.signIn')}</a>
			</nav>
			<ThemePicker name="landing-theme-menu" compact />
		</div>
	</header>

	<main class="content">
		<section class="hero">
			<div class="hero-text">
				<span class="eyebrow">{m('marketing.hero.eyebrow')}</span>
				<!-- No forced line breaks: where a headline may break is a property
				     of the language, so each locale wraps its own (`text-wrap:
				     pretty`, and hyphenation for long compounds). -->
				<h1>
					<span class="title-main">{m('marketing.hero.title')}</span>
					<span class="accent">{m('marketing.hero.titleAccent')}</span>
				</h1>
				<p class="lede">{m('marketing.hero.lede')}</p>
				<div class="cta-row">
					<a href="/signup" class="primary">{m('marketing.cta.create')}<IconArrow aria-hidden="true" /></a>
					<a href="#how" class="secondary">{m('marketing.hero.secondary')}</a>
				</div>
				<p class="sub-note">{m('marketing.hero.subNote')}</p>
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
				<span class="eyebrow">{m('marketing.features.eyebrow')}</span>
				<h2>{m('marketing.features.heading')}</h2>
				<p>{m('marketing.features.lede')}</p>
			</div>

			<div class="feature-grid">
				{#each features as feature, i}
					<div class="feature" use:reveal={{ delay: (i % 3) * 80, amount: 0.15 }}>
						<div class="feature-icon">
							<feature.icon />
						</div>
						<h3>{m(feature.title)}</h3>
						<p>{m(feature.body)}</p>
					</div>
				{/each}
			</div>
		</section>

		<section id="how" class="how">
			<div class="section-head" use:reveal>
				<span class="eyebrow">{m('marketing.how.eyebrow')}</span>
				<h2>{m('marketing.how.heading')}</h2>
			</div>

			<div class="steps">
				{#each steps as step, i}
					<div class="step" use:reveal={{ delay: i * 90, amount: 0.15 }}>
						<!-- A watermark ordinal behind the card. `aria-hidden` because
						     that is only defensible if this really is decoration: the
						     ordinal is already carried by the cards' reading order, so
						     nothing is lost by hiding it. The fade is in the COLOUR
						     (a `color-mix`), not an `opacity` on the element — group opacity
						     would composite the card's real text down with it, which
						     is the idiom src/lib/a11y/opacityAudit.test.ts exists to
						     keep out of this tree. -->
						<div class="step-num" aria-hidden="true">{i + 1}</div>
						<div class="step-icon"><step.icon /></div>
						<h3>{m(step.title)}</h3>
						<p>{m(step.body)}</p>
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
					<span class="eyebrow">{m('marketing.tally.eyebrow')}</span>
					<h2>{m('marketing.tally.heading')}</h2>
					<p class="tally-lede">{m('marketing.tally.lede')}</p>
				</div>

				<div class="tally-art" use:reveal={{ delay: 120 }}>
					<img
						src={asset(TALLY_ORNAMENT[theme.resolved])}
						width={TALLY_ORNAMENT_SIZE.width}
						height={TALLY_ORNAMENT_SIZE.height}
						loading="lazy"
						decoding="async"
						draggable="false"
						alt={m('marketing.tally.alt')}
					/>
				</div>
			</div>

			<div class="diff-grid">
				<div class="diff" use:reveal={{ delay: 60 }}>
					<h3>{m('marketing.diff.ai.title')}</h3>
					<p>{m('marketing.diff.ai.body')}</p>
				</div>
				<div class="diff" use:reveal={{ delay: 120 }}>
					<h3>{m('marketing.diff.connect.title')}</h3>
					<p>{m('marketing.diff.connect.body')}</p>
				</div>
				<div class="diff" use:reveal={{ delay: 180 }}>
					<h3>{m('marketing.diff.cards.title')}</h3>
					<p>{cardsBody}</p>
					<!-- A large, vivid dollar figure reads as a projection unless the
					     variability sits next to it. Derivation:
					     docs/marketing-substantiation.md. -->
					<p class="diff-note">{m('marketing.diff.cards.note')}</p>
				</div>
				<div class="diff" use:reveal={{ delay: 240 }}>
					<h3>{m('marketing.diff.mobile.title')}</h3>
					<p>{m('marketing.diff.mobile.body')}</p>
				</div>
			</div>
		</section>

		<Pricing />

		<section class="cta-section" use:reveal>
			<div class="cta-inner">
				<h2>{m('marketing.final.heading')}</h2>
				<!-- The steps signup really takes: the address, the confirmation
				     email, then the set-password link (decisions §283). -->
				<p>{m('marketing.final.body')}</p>
				<a href="/signup" class="primary large">{m('marketing.cta.create')}<IconArrow aria-hidden="true" /></a>
			</div>
		</section>
	</main>

	<footer class="footer">
		<div class="footer-inner">
			<div class="footer-brand">
				<BrandMark size={22} />
				<span class="brand-name">{product}</span>
			</div>
			<nav class="footer-links" aria-label={m('marketing.footer.label')}>
				<a href="/find-workspace">{m('marketing.nav.signIn')}</a>
				<a href="/signup">{m('marketing.cta.create')}</a>
				<a href="#features">{m('marketing.nav.features')}</a>
				<a href="#how">{m('marketing.nav.how')}</a>
				<!--
					The legal set, reached from the marketing page because that is
					where an evaluating buyer, a supplier chasing their own data and a
					procurement reviewer all land first — and on the apex domain this
					footer is the only navigation that exists. Rendered from
					`LEGAL_PAGES` rather than hand-listed so a new document appears
					here without anyone remembering to add it. The titles stay
					English in every locale, as the documents do (decisions §174).
				-->
				{#each LEGAL_PAGES as page (page.path)}
					<a href={page.path}>{page.title}</a>
				{/each}
			</nav>
			<div class="footer-copy">
				{m('marketing.footer.copyright', { year: String(new Date().getFullYear()), product })}
			</div>
		</div>
	</footer>
</div>
{/if}

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
	/* Everything except the Atmosphere sits above it. One rule rather than a
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
		flex-wrap: wrap;
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
		background: color-mix(in srgb, var(--bg) 72%, transparent);
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
	.nav-end,
	.nav-links {
		display: flex;
		align-items: center;
		gap: 22px;
	}
	.nav-end {
		gap: 16px;
	}
	/* Sign in is the returning customer's one control on this page: padded to
	   a 36px target rather than leaning on 2.5.8's spacing exception. */
	.nav-end .nav-signin {
		display: inline-flex;
		align-items: center;
		min-height: 36px;
		padding: 0 4px;
	}
	.nav-links a,
	.nav-end .nav-signin {
		color: var(--text-muted);
		text-decoration: none;
		font-size: 0.9rem;
		white-space: nowrap;
		transition: color 0.15s;
	}
	.nav-links a:hover,
	.nav-end .nav-signin:hover {
		color: var(--text);
	}
	/* The CTA is no longer inside `.nav-links` (it is an action, not one of the
	   page's sections), so nothing out-specifies it any more. It once lost to
	   `.nav-links a` (0,1,1) and rendered --text-muted on --accent-strong —
	   about 1.5:1; the two-class selector keeps it ahead of any future link
	   rule in this header without reaching for `!important`. */
	.nav-end .nav-cta {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		text-decoration: none;
		white-space: nowrap;
		font-size: 0.9rem;
		background: var(--accent-strong);
		color: #fff;
		padding: 9px 17px;
		border-radius: 8px;
		font-weight: 500;
		box-shadow: 0 8px 22px -12px var(--accent-glow);
		transition: transform 0.15s, box-shadow 0.15s;
	}
	/* Restates the fill beside the white, so the pair is decided in one rule
	   the token audit can read. */
	.nav-end .nav-cta:hover {
		background: var(--accent-strong);
		color: #fff;
		transform: translateY(-1px);
		box-shadow: 0 12px 26px -12px color-mix(in srgb, var(--accent) 70%, transparent);
	}
	.cta-short {
		display: none;
	}

	/* The phone menu: a toggle in the bar and a panel under it. Neither exists
	   above 1024px, where the same links sit in the bar. */
	.menu-toggle {
		display: none;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		flex: none;
		padding: 0;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: color-mix(in srgb, var(--surface) 60%, transparent);
		color: var(--text);
		font-size: 22px;
		cursor: pointer;
	}
	.menu-toggle:hover {
		border-color: var(--border-strong);
	}
	.menu-toggle:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.menu-panel {
		display: none;
	}

	/* The menu takes over at 1024px, not at the phone breakpoint: with Sign
	   in beside them, the section links, the CTA and the theme switch need
	   about 970px in French, and a bar that wraps onto a second row is a
	   taller sticky header than `scroll-padding-top` allows for (WCAG
	   2.4.11). landing.spec.ts measures the bar from 320px to 1025px in every
	   locale. */
	@media (max-width: 1024px) {
		.nav {
			row-gap: 0;
		}
		/* The panel is open: the bar takes the solid ground the scrolled header
		   has, so the links are not read over the hero. */
		.nav:has(.menu-panel:not([hidden])) {
			background: var(--bg);
			border-bottom-color: var(--border);
		}
		.nav-end {
			gap: 12px;
		}
		.nav-wide,
		.theme-wide {
			display: none;
		}
		.menu-toggle {
			display: inline-flex;
		}
		.menu-panel:not([hidden]) {
			display: flex;
			flex-direction: column;
			align-items: flex-start;
			gap: 14px;
			flex-basis: 100%;
			padding: 14px 0 6px;
		}
		.menu-panel nav {
			display: flex;
			flex-direction: column;
			align-self: stretch;
		}
		/* Each row a full-width, 44px target (WCAG 2.5.8 asks 24px; a thumb
		   asks more). */
		.menu-panel nav a {
			display: flex;
			align-items: center;
			min-height: 44px;
			color: var(--text);
			text-decoration: none;
			font-size: 1rem;
			border-bottom: 1px solid var(--hairline);
		}
	}
	@media (max-width: 760px) {
		.nav {
			padding: 14px 20px;
		}
		.nav-end .nav-cta {
			padding: 8px 14px;
			white-space: nowrap;
		}
		.cta-long { display: none; }
		.cta-short { display: inline; }
	}
	.menu-panel nav a.menu-signin {
		display: none;
	}
	/* WCAG 1.4.10 Reflow, at the criterion's own 320px. With the wordmark
	   beside them, the mark, Sign in, the short CTA and the menu toggle run
	   past that viewport in the longer locales, and `.landing` clips
	   horizontal overflow — so the last control's edge would be cut off rather
	   than scrollable (landing.spec.ts measures every locale). The wordmark goes, and
	   the mark alone carries the brand; its TEXT stays in the accessibility tree
	   (clipped, not `display: none`), because the mark image is decorative and
	   that text is the home link's only accessible name. */
	@media (max-width: 400px) {
		.nav {
			padding: 12px 14px;
			column-gap: 8px;
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
		.nav-end {
			gap: 8px;
		}
		.nav-end .nav-cta {
			padding: 8px 12px;
		}
		/* The arrow is decoration; at this width its 22px are worth more to a
		   long translated label. */
		.nav-end .nav-cta :global(svg) {
			display: none;
		}
		.menu-toggle {
			width: 36px;
			height: 36px;
		}
	}
	/* Below 360px the bar holds the mark, the signup call to action and the
	   menu toggle; Sign in moves into the menu. "Iniciar sesión" beside
	   "Empezar gratis" does not fit 320px, and a label that did would be a
	   worse label. Measured in every locale by landing.spec.ts. */
	@media (max-width: 359px) {
		.nav-end .nav-signin {
			display: none;
		}
		.menu-panel nav a.menu-signin {
			display: flex;
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
		border: 1px solid color-mix(in srgb, var(--accent) 28%, transparent);
		background: var(--accent-tint);
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
	/* A German compound ("AP-Automatisierung") at the headline's size is wider
	   than a phone; hyphenate in the page's language, and break a word only
	   when even that is not enough. */
	h1,
	h2,
	h3 {
		hyphens: auto;
		overflow-wrap: break-word;
	}
	h1 {
		margin: 0 0 24px;
		font-size: clamp(2.5rem, 5.4vw, 3.9rem);
		line-height: 1.04;
		letter-spacing: -0.03em;
		font-weight: 800;
	}
	h1 .title-main,
	h1 .accent {
		display: block;
		/* `pretty`, not `balance`: balancing a three-line phone headline left
		   one short word alone on the middle line. */
		text-wrap: pretty;
	}
	h1 .accent {
		background: linear-gradient(100deg, var(--accent-on-tint) 0%, var(--brand-violet) 42%, var(--gilt) 100%);
		background-size: 220% 100%;
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
		/* One pass of the gradient across the words and back, then rest. It
		   ends where it starts, so the resting frame — and the only frame under
		   reduced motion — is the intended one. 4s: nothing on this page moves
		   past five seconds (decisions §279). */
		animation: sheen 4s ease-in-out 1 forwards;
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
		box-shadow: 0 14px 34px -14px var(--accent-glow);
	}
	.primary:hover {
		transform: translateY(-2px);
		box-shadow: 0 20px 42px -16px color-mix(in srgb, var(--accent) 70%, transparent);
	}
	.primary.large { padding: 15px 30px; font-size: 1rem; }
	.secondary {
		background: color-mix(in srgb, var(--surface) 60%, transparent);
		color: var(--text);
		border: 1px solid var(--border);
	}
	.secondary:hover { border-color: var(--accent); transform: translateY(-2px); }
	.sub-note {
		margin: 18px 0 0;
		color: var(--text-muted);
		font-size: 0.82rem;
	}

	/* `min-width: 0` on both grid items: a grid item will not shrink below its
	   longest unbreakable word, and at the headline's size a French or German
	   word ("L’automatisation") is wider than a phone, which pushed the whole
	   column past the viewport instead of letting the headline hyphenate. */
	.hero-text {
		min-width: 0;
	}
	.hero-visual {
		position: relative;
		min-width: 0;
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
		background: color-mix(in srgb, var(--surface) 40%, transparent);
		box-shadow: var(--shadow-card);
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
		background: linear-gradient(135deg, var(--accent-on-tint), var(--brand-violet));
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
		background: color-mix(in srgb, var(--surface) 62%, transparent);
		box-shadow: var(--shadow-card);
		border: 1px solid var(--border);
	}
	/* A hairline of accent along the top edge. A pseudo-element rather than a
	   border so it can be inset from the corners and not fight the card's own
	   radius. It does not react to the pointer: nothing here is clickable, and
	   a card that lifts or lights up under the cursor promises a link it does
	   not have (#635 finding 30). */
	.feature::before {
		content: '';
		position: absolute;
		top: -1px;
		left: 18%;
		right: 18%;
		height: 1px;
		background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent) 55%, transparent), transparent);
	}
	.feature-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		border-radius: 12px;
		background: linear-gradient(140deg, color-mix(in srgb, var(--accent) 22%, transparent), color-mix(in srgb, var(--brand-violet) 14%, transparent));
		border: 1px solid color-mix(in srgb, var(--accent) 22%, transparent);
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
		background: color-mix(in srgb, var(--surface) 62%, transparent);
		box-shadow: var(--shadow-card);
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
		color: color-mix(in srgb, var(--text) 7%, transparent);
	}
	.step-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 46px;
		height: 46px;
		border-radius: 12px;
		background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 22%, transparent), color-mix(in srgb, var(--brand-violet) 18%, transparent));
		border: 1px solid color-mix(in srgb, var(--accent) 22%, transparent);
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
		/* Same reasoning as `AuthShell`'s `.panel-art`: illustration, not a file
		   the reader wants. The attribute stops the drag; these two stop the
		   selection highlight and WebKit's own image-drag, which it does not. */
		-webkit-user-drag: none;
		user-select: none;
		display: block;
		width: 100%;
		height: auto;
		/* The render is lit from above-left on a transparent film, so a warm
		   bloom under it reads as the object's own light on the page rather
		   than as a box around a picture. */
		filter: drop-shadow(0 26px 46px color-mix(in srgb, var(--gilt) 14%, transparent))
			drop-shadow(0 8px 18px var(--drop-shade));
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
		background: color-mix(in srgb, var(--surface) 62%, transparent);
		box-shadow: var(--shadow-card);
		border: 1px solid var(--border);
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
			radial-gradient(640px 320px at 50% 0%, color-mix(in srgb, var(--accent) 22%, transparent), transparent 70%),
			linear-gradient(180deg, color-mix(in srgb, var(--surface-2) 60%, var(--surface)), var(--surface));
		border: 1px solid color-mix(in srgb, var(--accent) 22%, transparent);
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
		background: color-mix(in srgb, var(--bg) 60%, transparent);
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
