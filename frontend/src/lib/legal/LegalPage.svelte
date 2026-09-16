<script lang="ts">
	import { LAST_UPDATED, OPERATOR, OPERATOR_FACTS_COMPLETE, PENDING, PENDING_FACT_LABELS } from './operator';
	import { LEGAL_PAGES } from './pages';

	/**
	 * Shared chrome for every page under `routes/legal/`.
	 *
	 * Owns the parts that must be identical across the document set — the
	 * heading, the last-updated stamp, the pending-facts notice, the
	 * cross-document nav, and the typography — so a page file contains only its
	 * own legal text. The `.legal-page` wrapper is the hook the e2e spec reads
	 * the body text from.
	 *
	 * These pages render standalone: no sidebar, no tenant chrome, readable on
	 * the apex domain to a visitor who has never signed in. `routes/+layout.svelte`
	 * routes `/legal/*` past its auth gate for exactly that reason.
	 */
	let { title, intro, children } = $props<{
		title: string;
		/** Optional one-paragraph orientation shown above the body text. */
		intro?: string;
		children: import('svelte').Snippet;
	}>();
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

	<article class="legal-page">
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
	.legal-shell {
		max-width: 46rem;
		/* 16px side gutter at phone width; centred with room to breathe above. */
		margin: 0 auto;
		padding: 32px 16px 64px;
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
	   without horizontal page scroll. */
	.legal-page :global(.table-scroll) {
		overflow-x: auto;
		margin: 0 0 16px;
	}

	.legal-page :global(.table-scroll table) {
		margin: 0;
		min-width: 32rem;
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
</style>
