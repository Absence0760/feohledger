<script lang="ts">
	import { LAST_UPDATED, OPERATOR, OPERATOR_FACTS_COMPLETE, PENDING, PENDING_FACT_LABELS } from './operator';
	import { LEGAL_PAGES } from './pages';

	/**
	 * Shared chrome for every page under `routes/legal/`.
	 *
	 * Owns the parts that must be identical across the document set — the
	 * heading, the last-updated stamp, the pending-facts notice, the table of
	 * contents, the cross-document nav, and the typography — so a page file
	 * contains only its own legal text. The `.legal-page` wrapper is the hook
	 * the e2e spec reads the body text from.
	 *
	 * These pages render standalone: no sidebar, no tenant chrome, readable on
	 * the apex domain to a visitor who has never signed in. `routes/+layout.svelte`
	 * routes `/legal/*` past its auth gate for exactly that reason.
	 * `routes/legal/+layout.svelte` is what puts a minimal, static way back into
	 * the product inside that branch, and owns the backdrop and the 46rem
	 * measure this component no longer declares.
	 */
	let { title, intro, children } = $props<{
		title: string;
		/** Optional one-paragraph orientation shown above the body text. */
		intro?: string;
		children: import('svelte').Snippet;
	}>();

	/* ---------------------------- table of contents ----------------------------
	   Six documents and 91 top-level sections between them — the DPA alone has
	   21, running to 1,552 lines — and until now the only way to find a clause
	   was the browser's find-in-page. A customer's DPO cites §8 Sub-processors,
	   a supplier is told to read §12 Your rights: both arrive needing one
	   section out of twenty and get a wall of text.

	   Derived from the RENDERED document rather than declared per page. The
	   alternative — a `sections` prop each page passes — is 91 hand-kept
	   entries duplicating headings that already exist, in six files, and it
	   drifts the first time a section is renumbered in one place and not the
	   other. A DOM read is exact by construction and cannot go stale. It is
	   also sound here specifically: the app is `adapter-static` with no SSR
	   (root `CLAUDE.md` § Static frontend), so `routes/+layout.svelte` renders
	   nothing at all until its browser-guarded effect resolves and every
	   document is client-rendered regardless.

	   Scoped to the `<article>`, which is what makes it correct rather than
	   merely convenient: the page carries two `<h2>`s that are NOT document
	   sections — `.pending h2#pending-heading` ("Details still to be
	   confirmed") and `.legal-nav h2` ("Other documents") — and both sit
	   outside the article. Do not widen the query to the document.
	   -------------------------------------------------------------------------- */

	/** One entry per top-level section of the rendered document. */
	interface Section {
		/** The heading's own `id` — every `<h2>` in all six documents has one. */
		id: string;
		/** The heading's text, verbatim, including its section number. */
		label: string;
	}

	/**
	 * The width at which the contents list stops being a disclosure above the
	 * text and becomes a sticky rail beside it.
	 *
	 * Written three times, and all three have to agree: here (so the disclosure
	 * starts open in the rail), in the `min-width` query at the bottom of this
	 * file (the grid), and in `routes/legal/+layout.svelte` (which widens the
	 * sheet by exactly the rail at the same width, because a child cannot widen
	 * its container). 72rem = 1152px: the widened sheet is 67rem, which leaves
	 * a 2.5rem page margin each side — the narrowest width at which the rail is
	 * new space rather than space taken from the 46rem measure.
	 */
	const RAIL_QUERY = '(min-width: 72rem)';

	/**
	 * How far down the viewport the scroll-spy's reading line sits, in CSS px.
	 *
	 * The active section is the last one whose heading has passed this line —
	 * i.e. the section the reader is inside, not the next one coming up. A line
	 * at 0 would hand "current" to a heading the instant its bottom pixel left
	 * the screen; 120px is roughly where a reader's eye is.
	 */
	const SPY_LINE_PX = 120;

	let article = $state<HTMLElement | null>(null);
	let sections = $state<Section[]>([]);
	let activeId = $state<string | null>(null);
	let contentsOpen = $state(false);

	// Derive the contents, then spy on it. One effect, because the spy observes
	// the very elements the derivation just found — splitting them would mean
	// querying the DOM twice and keeping two lists in step.
	//
	// A document's section set is fixed for the life of the page: the six page
	// files render their `<h2>`s unconditionally (only inline `Fact` spans and
	// the Cookie Notice's withdrawal confirmation are reactive), and a route
	// change instantiates a different `+page.svelte`, so this component is
	// rebuilt rather than reused. So one pass on mount is the whole job.
	$effect(() => {
		const root = article;
		if (!root) return;

		const headings = [...root.querySelectorAll<HTMLElement>('h2[id]')];
		sections = headings.map((h) => ({ id: h.id, label: (h.textContent ?? '').trim() }));

		// Same guard as `$lib/actions/reveal.ts`: without the observer the list
		// is still a complete, working table of contents — it just marks nothing
		// as current. Degrading to "no highlight" beats throwing on a legal page.
		if (headings.length === 0 || typeof IntersectionObserver === 'undefined') return;

		const mark = () => {
			let current = headings[0].id;
			for (const heading of headings) {
				if (heading.getBoundingClientRect().top > SPY_LINE_PX) break;
				current = heading.id;
			}
			activeId = current;
		};

		// The observer is the *trigger*, not the measurement. Asking it which
		// heading is intersecting answers the wrong question — inside a long
		// section none of them is, and the highlight would blank out. Pulling
		// the root's top edge down to the reading line instead means the
		// callback fires exactly when a heading crosses that line in either
		// direction, which is exactly when the answer changes; `mark()` then
		// recomputes from all of them. It also fires once on observe, so the
		// initial state (including after a deep link's scroll) is correct
		// without a scroll listener.
		const observer = new IntersectionObserver(mark, {
			rootMargin: `-${SPY_LINE_PX}px 0px 0px 0px`
		});
		for (const heading of headings) observer.observe(heading);
		return () => observer.disconnect();
	});

	// The disclosure is open in the rail and closed above the text, and follows
	// the viewport across the breakpoint. CSS cannot set `open`, and rendering
	// two different navs would put the same 21 links in the document twice.
	$effect(() => {
		const rail = window.matchMedia(RAIL_QUERY);
		const sync = () => {
			contentsOpen = rail.matches;
		};
		sync();
		rail.addEventListener('change', sync);
		return () => rail.removeEventListener('change', sync);
	});
</script>

<svelte:head>
	<title>{title} · {OPERATOR.serviceName}</title>
</svelte:head>

<div class="legal-shell">
	<header class="legal-header">
		<a href="/legal" class="back">← All legal documents</a>
		<h1>{title}</h1>
		<p class="updated">Last updated: {LAST_UPDATED}</p>
		{#if intro}
			<p class="intro">{intro}</p>
		{/if}
	</header>

	{#if !OPERATOR_FACTS_COMPLETE}
		<!--
			An honest gap marker, not a draft banner. The legal text below is
			complete and operative; what is outstanding is the short list of
			operator facts in `lib/legal/operator.ts`. Saying so plainly beats
			both alternatives: inventing a registered entity would make the page
			false, and hiding the gap would leave a reader unable to tell which
			statements are load-bearing.
		-->
		<aside class="pending" aria-labelledby="pending-heading">
			<h2 id="pending-heading">Details still to be confirmed</h2>
			<p>
				The terms below are complete and apply as written. These specific facts
				are not yet settled, and are marked <span class="fact-pending">like this</span>
				wherever they appear:
			</p>
			<ul>
				{#each PENDING as key (key)}
					<li>{PENDING_FACT_LABELS[key]}</li>
				{/each}
			</ul>
		</aside>
	{/if}

	{#if sections.length > 0}
		<!--
			A `<nav>` with its own name, because the page already has one
			("Other legal documents") and an unnamed second landmark is
			indistinguishable in a screen reader's landmark list.

			Every entry is a plain `href="#id"` and nothing intercepts the
			click. That is deliberate: the browser's own anchor navigation moves
			the sequential-focus starting point, puts the section in the URL so
			the reader can cite or share it, and adds it to history so Back
			returns. A JS `scrollIntoView` would move the pixels and none of
			the rest.
		-->
		<nav class="contents" aria-label="Sections of this document">
			<details class="contents-disclosure" bind:open={contentsOpen}>
				<summary>Contents</summary>
				<ol class="contents-list">
					<!--
						Unkeyed on purpose. The list is static for the life of the
						page, and a keyed `{#each}` would make a duplicate `id`
						anywhere in a document a hard runtime error on a published
						legal page rather than the two-entries-one-target
						annoyance it actually is.
					-->
					{#each sections as section}
						<li>
							<a
								class="contents-link"
								href="#{section.id}"
								aria-current={activeId === section.id ? 'true' : undefined}
								>{section.label}</a
							>
						</li>
					{/each}
				</ol>
			</details>
		</nav>
	{/if}

	<article class="legal-page" bind:this={article}>
		{@render children()}
	</article>

	<nav class="legal-nav" aria-label="Other legal documents">
		<h2>Other documents</h2>
		<ul>
			{#each LEGAL_PAGES as page (page.path)}
				<li><a href={page.path}>{page.title}</a></li>
			{/each}
		</ul>
	</nav>
</div>

<style>
	/* Smooth scrolling for the contents list's in-page links, scoped to while a
	   document is mounted so it never leaks into the app shell — the same
	   `:global(html:has(…))` shape `marketing/Landing.svelte` uses for its nav.
	   The global reduced-motion rule at the end of `app.css` sets
	   `scroll-behavior: auto !important` on every element, so anyone who asked
	   for less motion gets an instant jump and there is nothing to honour here
	   by hand. */
	:global(html:has(.legal-shell)) {
		scroll-behavior: smooth;
		/* Breathing room above a section the browser has just scrolled to —
		   NOT a header offset. `routes/legal/+layout.svelte` keeps the top bar
		   static precisely so nothing can cover a clause, and the contents rail
		   is in a side column, so no heading is ever obscured. Declared on the
		   scroll container rather than as `scroll-margin-top` on the headings
		   so it also governs the scroll that keyboard focus causes. */
		scroll-padding-top: 24px;
	}

	/* No measure and no side gutter of its own: `routes/legal/+layout.svelte`
	   owns both, once, for the index and the six documents together (#433).
	   This file and the index each used to declare `max-width: 46rem`, which is
	   two places for one typographic decision. Only the vertical rhythm is the
	   document's own. */
	.legal-shell {
		padding: 32px 0 64px;
		color: var(--text);
	}

	.back {
		display: inline-block;
		margin-bottom: 16px;
		color: var(--text-muted);
		font-size: 0.875rem;
		text-decoration: none;
	}

	.back:hover,
	.back:focus-visible {
		color: var(--accent-on-tint);
		text-decoration: underline;
	}

	h1 {
		margin: 0 0 8px;
		font-size: 2rem;
		line-height: 1.2;
	}

	.updated {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.875rem;
	}

	.intro {
		margin: 16px 0 0;
		color: var(--text-muted);
		font-size: 1.0625rem;
		line-height: 1.6;
	}

	.pending {
		margin: 32px 0 0;
		padding: 16px 20px;
		border: 1px solid var(--warning-on-tint);
		border-radius: 8px;
		background: var(--warning-tint);
	}

	.pending h2 {
		margin: 0 0 8px;
		font-size: 1rem;
		color: var(--warning-on-tint);
	}

	.pending p,
	.pending li {
		color: var(--text);
		font-size: 0.9375rem;
		line-height: 1.6;
	}

	.pending p {
		margin: 0 0 8px;
	}

	.pending ul {
		margin: 0;
		padding-left: 20px;
	}

	/* The inline marker a page renders where a pending fact would go. Defined
	   here (and applied via :global, since the markup lives in the page files)
	   so all five documents mark a gap identically.

	   Deliberately NOT `white-space: nowrap`, which is the instinctive choice
	   for keeping a marker visually intact. These labels are whole phrases —
	   "[a postal address for the controller to be confirmed]" is 46 characters —
	   so refusing to wrap pushed the DOCUMENT 163px wider than a 320px viewport,
	   making a reader scroll the page sideways to read a privacy policy
	   (WCAG 1.4.10 Reflow). The brackets already delimit the marker; it does not
	   also need to sit on one line. */
	.pending .fact-pending,
	.legal-page :global(.fact-pending) {
		padding: 0 4px;
		border-radius: 3px;
		color: var(--warning-on-tint);
		font-style: italic;
	}

	/* The tint goes on ONLY in the document body, where the marker sits on the
	   plain page background. `--warning-tint` is translucent, so the example
	   marker inside the pending notice — which already has that tint as its own
	   background — would composite it twice: 6.14:1 becomes 4.39:1, under the
	   1.4.3 floor of 4.5. The notice's own tint is contrast enough to mark the
	   example, so the second layer buys nothing and costs conformance. */
	.legal-page :global(.fact-pending) {
		background: var(--warning-tint);
	}

	.legal-page {
		margin-top: 32px;
		line-height: 1.7;
	}

	.legal-page :global(h2) {
		margin: 40px 0 12px;
		padding-top: 8px;
		font-size: 1.375rem;
		line-height: 1.3;
	}

	.legal-page :global(h3) {
		margin: 28px 0 8px;
		font-size: 1.0625rem;
		line-height: 1.4;
	}

	.legal-page :global(p),
	.legal-page :global(li) {
		font-size: 1rem;
	}

	.legal-page :global(p) {
		margin: 0 0 16px;
	}

	.legal-page :global(ul),
	.legal-page :global(ol) {
		margin: 0 0 16px;
		padding-left: 24px;
	}

	.legal-page :global(li) {
		margin-bottom: 8px;
	}

	.legal-page :global(a) {
		color: var(--accent-on-tint);
	}

	.legal-page :global(table) {
		width: 100%;
		margin: 0 0 16px;
		border-collapse: collapse;
		font-size: 0.9375rem;
	}

	.legal-page :global(th),
	.legal-page :global(td) {
		padding: 8px 12px;
		border: 1px solid var(--border);
		text-align: left;
		vertical-align: top;
	}

	.legal-page :global(th) {
		background: var(--surface);
		font-weight: 600;
	}

	/* WCAG 1.4.10 Reflow: a wide table must not push the page wider than the
	   viewport. Wrapping it in a scroller keeps the document itself at 320px
	   without horizontal page scroll.

	   Every `.table-scroll` in a page file carries `tabindex="0"`, and must.
	   Solving reflow this way creates a region only a mouse can pan, which is
	   WCAG 2.1.1 Keyboard — axe reports it as `scrollable-region-focusable`.
	   It surfaced in CI rather than locally because the rule only fires once the
	   table actually overflows its container, which depends on how wide the
	   font renders. Making the region a tab stop lets arrow keys scroll it. */
	.legal-page :global(.table-scroll) {
		overflow-x: auto;
		margin: 0 0 16px;
	}

	.legal-page :global(.table-scroll table) {
		margin: 0;
		min-width: 32rem;
	}

	/* ---------------------------- table of contents ----------------------------
	   Narrow first: a closed disclosure between the document's header and its
	   text. Closed by DEFAULT because on a phone an open 21-entry list is a
	   screenful of links in front of the document the reader came for — and
	   because the whole point of this page type is that the text is all there,
	   so skipping the contents costs nothing. */
	.contents {
		margin: 32px 0 0;
	}

	.contents-disclosure {
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface);
	}

	/* Styled like `.legal-nav h2` rather than like a section heading: this is
	   chrome around the document, not part of it. Kept as `<summary>`'s own
	   text with no heading inside it — a heading nested in what maps to a
	   button is filtered out of the accessibility tree by some engines, so it
	   would buy an outline entry in exchange for a less predictable control. */
	.contents-disclosure summary {
		/* 40px tall with the line box, comfortably over the 24px floor WCAG 2.2
		   SC 2.5.8 asks of a target. */
		padding: 10px 14px;
		color: var(--text-muted);
		font-size: 0.875rem;
		font-weight: 600;
		cursor: pointer;
	}

	.contents-list {
		margin: 0;
		padding: 0 8px 10px;
		/* The section numbers are part of the heading text ("1. Who we are"),
		   and the DPA's three annexes have no number at all, so a generated
		   marker would either double up or invent an order the document does
		   not have. `<ol>` stays for the semantics; the numbering is the
		   document's. */
		list-style: none;
	}

	.contents-list li {
		margin: 0;
	}

	.contents-link {
		display: block;
		/* 31px with the line box — over the SC 2.5.8 floor without the spacing
		   exception, which a tightly stacked list would not qualify for. */
		padding: 6px 10px;
		border-left: 2px solid transparent;
		border-radius: 4px;
		color: var(--text-muted);
		font-size: 0.8125rem;
		line-height: 1.45;
		text-decoration: none;
		/* Section titles are whole clauses — "4. Strictly necessary vs. optional
		   storage — and today, nothing is optional" is 74 characters — so they
		   WRAP. An ellipsis would hide the words a reader is scanning for, and
		   `white-space: nowrap` is the exact mistake that once pushed these
		   documents 163px past a 320px viewport (see `.fact-pending` above). */
		overflow-wrap: break-word;
	}

	.contents-link:hover,
	.contents-link:focus-visible {
		/* Same value as `Sidebar.nav-item:hover` and
		   `SectionTabs.section-more-item:hover` — this is a nav row, so it
		   matches the chrome it belongs to rather than inventing a token.
		   `--surface-hover` does not exist. */
		background: rgba(99, 140, 255, 0.08);
		color: var(--text);
	}

	/* After the hover rule, which has the same specificity: where the reader
	   actually is outranks where the pointer happens to be. The tint/on-tint
	   pair is calibrated together — never `--accent` as the text colour on
	   `--accent-tint`, which measures 4.48:1. */
	.contents-link[aria-current='true'] {
		border-left-color: var(--accent);
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		font-weight: 600;
	}

	.legal-nav {
		margin-top: 56px;
		padding-top: 24px;
		border-top: 1px solid var(--border);
	}

	.legal-nav h2 {
		margin: 0 0 12px;
		font-size: 1rem;
		color: var(--text-muted);
	}

	.legal-nav ul {
		display: flex;
		flex-wrap: wrap;
		gap: 8px 20px;
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.legal-nav a {
		color: var(--accent-on-tint);
		font-size: 0.9375rem;
	}

	/* Wide: the list leaves the flow for a sticky rail beside the document.
	   `routes/legal/+layout.svelte` widens the sheet by
	   `--legal-contents-rail + --legal-contents-gap` at this SAME width and
	   declares both tokens, so the 46rem measure is untouched and the rail is
	   new space — change 72rem here and you must change it there and in
	   `RAIL_QUERY` above.

	   A rail rather than anything pinned over the text: the layout's top bar is
	   deliberately static because a sticky bar covers the clause a reader just
	   jumped to, and that reasoning applies to a contents bar just as much.
	   `sticky` works here because that file uses `overflow-x: clip` rather than
	   `hidden` on `.legal-root`, which keeps it from becoming a scroll
	   container — it says so. */
	@media (min-width: 72rem) {
		.legal-shell {
			display: grid;
			/* The document column takes whatever is left, which the sheet's own
			   max-width has already sized to exactly 46rem. Spelling 46rem here
			   instead would be the second copy of the measure that #433 removed. */
			grid-template-columns: var(--legal-contents-rail) minmax(0, 1fr);
			grid-template-areas:
				'contents head'
				'contents note'
				'contents doc'
				'contents after';
			column-gap: var(--legal-contents-gap);
		}

		.legal-header {
			grid-area: head;
		}

		/* Empty when every operator fact is filled, which costs an `auto` row of
		   zero height. */
		.pending {
			grid-area: note;
		}

		.contents {
			grid-area: contents;
			/* `start` so the item is its content's height rather than the full
			   four-row span — sticky needs somewhere to travel inside its grid
			   area. */
			align-self: start;
			position: sticky;
			top: 32px;
			margin-top: 0;
		}

		.legal-page {
			grid-area: doc;
		}

		.legal-nav {
			grid-area: after;
		}

		.contents-list {
			/* 21 entries do not fit a laptop viewport and the rail is pinned, so
			   the LIST scrolls and "Contents" stays put. Unlike `.table-scroll`
			   this needs no `tabindex="0"`: the region is full of links, so it
			   is already keyboard-operable and axe's
			   `scrollable-region-focusable` does not fire. */
			max-height: calc(100vh - 6rem);
			overflow-y: auto;
			overscroll-behavior: contain;
		}
	}
</style>
