<script lang="ts">
	import Atmosphere from '$lib/components/marketing/Atmosphere.svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import { OPERATOR } from '$lib/legal/operator';

	/**
	 * The frame every published legal route renders inside — the index and the
	 * six documents.
	 *
	 * It exists for two things that are properties of the *surround*, not of any
	 * one document (#434, #433):
	 *
	 * **1. A way back into the product.** `routes/+layout.svelte` branches
	 * `/legal/*` to a bare slot ahead of its tenant probe — correctly, because
	 * these documents are identical on the apex and on every tenant subdomain
	 * and gating them on tenant resolution would blank them for a beat and, on
	 * the apex, serve the marketing Landing instead of the document asked for.
	 * What that branch never put back was navigation: the index's only outbound
	 * links were three deeper into `/legal` and three `mailto:`, so a signed-in
	 * user who opened the Cookie Notice, or a procurement reviewer sent the DPA,
	 * had the Back button and nothing else.
	 *
	 * This header is deliberately the smallest thing that fixes that, and
	 * **entirely static**: two hardcoded hrefs, a decorative `<img>` from
	 * `static/`, and one constant from `lib/legal/operator.ts`. No store read,
	 * no `hasTenantContext()`, no fetch, nothing that resolves after mount — so
	 * it cannot blank, flash, or reintroduce the wait that branch avoids. `/`
	 * already resolves correctly on both host shapes (Landing on the apex, the
	 * app on a tenant subdomain) and `/login` is the sign-in surface on a tenant
	 * host; on the apex, where there is no tenant to sign into, it lands on the
	 * marketing page, which is the honest answer to "sign in to what?".
	 *
	 * **2. A backdrop, applied once.** The documents were a 46rem column on flat
	 * `var(--bg)`, which on a wide screen is about two-thirds undifferentiated
	 * fill — on the surface a prospect reads before buying and a customer's DPO
	 * reads during diligence. `Atmosphere` (from #429) is reused rather than a
	 * second backdrop invented: `fixed` because these documents are long and a
	 * parent-sized aurora smears over a 5,000px page, `still` because ambient
	 * motion running for more than five seconds would owe a pause control under
	 * WCAG 2.2.2 and a pause button on a privacy policy is clutter. Nothing here
	 * animates at all, so there is nothing for the `prefers-reduced-motion`
	 * block in `app.css` to collapse.
	 *
	 * It is brand-NEUTRAL by design, unlike the pre-auth surfaces of #422: these
	 * are the *operator's* own documents, published identically to every tenant,
	 * so deriving the backdrop from a tenant accent would dress the platform's
	 * legal terms in a customer's colours. `Atmosphere` is fixed rgba literals
	 * and reads no `--accent`, which is exactly the property needed.
	 *
	 * **The reading column keeps its own opaque `--bg`** (`.legal-sheet`). That
	 * is not decoration — it is what makes the contrast of every document
	 * provably unchanged: text still composites against `#0f1117` and nothing
	 * else, including `LegalPage`'s `--warning-tint` callout, which has 0.11
	 * headroom over the 4.5:1 floor and would lose it on a tinted ground. The
	 * aurora's own peak (`rgba(99, 140, 255, 0.30)` over `--bg`) lifts the
	 * ground to `rgb(40, 54, 93)`, where `--text-muted` is 3.67:1 — so muted
	 * text may never sit directly on the atmosphere. The header obeys that: its
	 * links are `--text` (9.30:1 on that same worst case) and the sign-in pill
	 * has its own opaque `--surface`.
	 *
	 * English strings, like the rest of the chrome in this directory
	 * (`LegalPage`'s "← All legal documents" and "Other documents") — see
	 * `frontend/CLAUDE.md` § i18n, whose exception is scoped to
	 * `src/routes/legal/` and `src/lib/legal/`. A pointer *at* the document set
	 * from anywhere else is ordinary UI and is translated; this is the chrome
	 * *inside* an English-only document set.
	 */
</script>

<div class="legal-root">
	<Atmosphere still fixed />

	<!--
		Not `position: sticky`. Every document here is navigated by in-page
		anchor — `/legal/terms#fees`, `#termination`, `/legal/privacy#roles` —
		and a sticky bar covers the heading the reader just jumped to unless
		every heading carries a matching `scroll-margin-top`. A header that is
		present on each route already answers the dead end; staying put costs
		nothing and cannot hide a clause.
	-->
	<header class="legal-topbar">
		<div class="topbar-inner">
			<a class="brand" href="/">
				<!-- Decorative: the wordmark beside it is the link's accessible name. -->
				<BrandMark size={26} />
				<span class="brand-name">{OPERATOR.serviceName}</span>
			</a>
			<a class="sign-in" href="/login">Sign in</a>
		</div>
	</header>

	<!--
		`<main>`, and not only for the landmark it adds. `<header>` maps to the
		`banner` role unless it descends from `article`/`aside`/`main`/`nav`/
		`section` — and both the index and every document open with a `<header>`
		of their own. Outside a landmark those were banners, so a top bar added
		beside them would announce a SECOND banner to a screen reader, on a page
		that until now had no `main` at all (the standalone branch skips the app
		shell, which is where `<main id="main-content">` lives). Wrapping the
		document here fixes both: one banner, one main, and every document's
		own header demoted to the section heading it always was.
	-->
	<main class="legal-sheet">
		<slot />
	</main>
</div>

<style>
	.legal-root {
		/* One gutter for the whole frame: the header aligns its mark to the
		   document's own text edge, and the sheet turns it into padding. */
		--legal-gutter: 16px;
		/* Geometry of the contents rail `lib/legal/LegalPage.svelte` pins beside
		   a document on a wide screen. Declared here because BOTH halves of that
		   layout read it — the rail's own grid track, in that file, and the
		   sheet's max-width below, in this one. */
		--legal-contents-rail: 15rem;
		--legal-contents-gap: 40px;
		position: relative;
		min-height: 100vh;
		background: var(--bg);
		color: var(--text);
		/* `clip`, not `hidden`: it contains the atmosphere's blurred blobs
		   without making this element a scroll container, which would break
		   `position: sticky` for anything a document renders inside it. */
		overflow-x: clip;
	}

	/* Everything except the Atmosphere sits above it — one rule rather than a
	   z-index per element, the same shape `Landing` uses. */
	.legal-topbar,
	.legal-sheet {
		position: relative;
		z-index: 1;
	}

	/* ------------------------------- header ------------------------------- */
	.legal-topbar {
		padding: 18px 0 0;
	}

	.topbar-inner {
		/* Held to the sheet's own width and gutter, so the mark sits over the
		   document's left text edge instead of floating at the window edge. */
		max-width: calc(46rem + 2 * var(--legal-gutter));
		margin: 0 auto;
		padding-inline: var(--legal-gutter);
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
	}

	.brand {
		display: inline-flex;
		align-items: center;
		gap: 10px;
		/* --text, not --text-muted: this sits directly on the atmosphere, where
		   the aurora's peak takes muted text to 3.67:1. */
		color: var(--text);
		text-decoration: none;
	}

	.brand-name {
		font-weight: 700;
		font-size: 1rem;
		letter-spacing: -0.01em;
	}

	.brand:hover .brand-name,
	.brand:focus-visible .brand-name {
		text-decoration: underline;
	}

	.sign-in {
		/* Its own opaque fill for the same reason: a pill on --surface is a
		   surface the palette contract already asserts --text against. */
		padding: 8px 16px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--surface);
		color: var(--text);
		font-size: 0.9rem;
		font-weight: 600;
		text-decoration: none;
		transition: border-color 0.15s, background 0.15s;
	}

	.sign-in:hover,
	.sign-in:focus-visible {
		border-color: var(--accent);
		/* --surface-2 is the raised companion, and --text is the ONLY token the
		   palette allows on it (11.0:1; --text-muted is 4.34:1 there and is why
		   the token carries that warning). This pill is already --text. */
		background: var(--surface-2);
	}

	/* -------------------------------- sheet ------------------------------- */

	/* The document's ground. Opaque --bg on purpose: the measure sits on the
	   exact colour it sat on before this layout existed, so no contrast pair in
	   any document changed — see the component comment. The treatment is
	   entirely in the surround. */
	.legal-sheet {
		/* The 46rem measure, hoisted here from `routes/legal/+page.svelte` and
		   `lib/legal/LegalPage.svelte`, which each carried their own copy. It is
		   the TEXT measure — ~66–75 characters, the right line length for
		   documents people actually have to read — so the card is the measure
		   PLUS its own gutter, never the measure squeezed to fit a card. */
		max-width: calc(46rem + 2 * var(--legal-gutter));
		margin: 8px auto 64px;
		padding-inline: var(--legal-gutter);
		position: relative;
		border: 1px solid var(--border);
		border-radius: 16px;
		background: var(--bg);
		box-shadow: 0 30px 60px -32px rgba(0, 0, 0, 0.85);
	}

	/* A hairline of the brand blue along the sheet's top edge — the one thing
	   that stops the card reading as a plain rectangle. Decorative and
	   text-free, so it pairs with nothing. */
	.legal-sheet::before {
		content: '';
		position: absolute;
		inset: 0 12% auto;
		height: 1px;
		background: linear-gradient(
			90deg,
			transparent,
			rgba(99, 140, 255, 0.45),
			rgba(231, 185, 94, 0.28),
			transparent
		);
	}

	/* Below the sheet's own width the card fills the viewport, so its rounded
	   corners and side borders would draw a box around the whole screen. Drop
	   them and let the document run edge to edge; the gutter is unchanged, which
	   is what keeps the 320px reflow behaviour identical to before. */
	/* 52rem (832px) is where the sheet at its widest — the 46rem measure plus a
	   28px gutter each side, 792px — first fits with room around it. Both
	   queries turn at the same number on purpose: a card that is rounded and
	   bordered while still touching both window edges is the in-between state
	   that looks like a mistake. */
	@media (max-width: 52rem) {
		.legal-sheet {
			margin-top: 0;
			border-inline: 0;
			border-radius: 0;
			box-shadow: none;
		}
		.legal-sheet::before {
			inset: 0 0 auto;
		}
	}

	@media (min-width: 52rem) {
		.legal-root {
			--legal-gutter: 28px;
		}
	}

	/* A DOCUMENT — never the index — grows by exactly the contents rail at the
	   width where `lib/legal/LegalPage.svelte` pins that rail beside the text.
	   72rem has to match the `min-width` query and `RAIL_QUERY` in that file;
	   all three turn together or the rail lands in a column that is not there.

	   `:has()` rather than a prop or a class the page sets, because the sheet is
	   the PARENT: a child cannot widen its container, and the index page
	   (`routes/legal/+page.svelte`) has no rail and must keep the bare measure —
	   a wider card with 46rem of centred text in it is the one outcome worth
	   avoiding here.

	   The measure itself does not move. 46rem stays 46rem and the rail plus its
	   gap are added beside it, so every line length, every contrast pair and the
	   e2e assertion that pins the reading column are all unchanged (#433). */
	@media (min-width: 72rem) {
		.legal-sheet:has(:global(.legal-shell)) {
			max-width: calc(
				46rem + var(--legal-contents-rail) + var(--legal-contents-gap) + 2 * var(--legal-gutter)
			);
		}
	}
</style>
