<script lang="ts">
	/**
	 * Help search results for `?q=` (the help layout's search box drives the
	 * URL). Guides, pages and glossary terms in one ranked list, each marked
	 * with what it is. Matching runs over the reader's language (term names,
	 * page labels, every `{ui:…}` label in a guide) and English (guide prose,
	 * aliases), so either vocabulary finds a result.
	 */
	import { page } from '$app/state';
	import { brand } from '#lib/stores/brand.svelte.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { PAGE_HELP, navPageFor, searchHelp, termHref, termKey, termShortKey } from '#lib/help/content.ts';
	import HelpCrumbs from '#lib/components/help/HelpCrumbs.svelte';
	import RichText from '#lib/components/help/RichText.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';

	const q = $derived((page.url.searchParams.get('q') ?? '').trim());
	const hits = $derived(searchHelp(q, m));
	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');
</script>

<svelte:head>
	<title>{m('help.search.title')} · {m('help.title')} · {brand.productName}</title>
</svelte:head>

<HelpCrumbs trail={[]} current={m('help.search.title')} />
<h1 class="title">{m('help.search.title')}</h1>

<p class="status" role="status" data-testid="help-search-status">
	{#if !q}
		{m('help.search.empty')}
	{:else if hits.length}
		{m('help.search.results', { n: hits.length, q })}
	{:else}
		{m('help.search.none', { q })}
	{/if}
</p>

{#if hits.length}
	<ul class="hits">
		{#each hits as hit (hit.kind + (hit.kind === 'guide' ? hit.guide.id : hit.kind === 'term' ? hit.term.id : hit.href))}
			<li data-testid="help-search-hit">
				<span class="kind"><Badge tone="muted">{m(hit.kind === 'guide' ? 'help.search.kindGuide' : hit.kind === 'term' ? 'help.search.kindTerm' : 'help.search.kindPage')}</Badge></span>
				{#if hit.kind === 'guide'}
					<a href="/help/guides/{hit.guide.id}" lang={englishLang}>{hit.guide.title}</a>
					<p lang={englishLang}>{hit.guide.summary}</p>
				{:else if hit.kind === 'term'}
					<a href={termHref(hit.term.id)}>{m(termKey(hit.term.id))}</a>
					<p>{m(termShortKey(hit.term.id))}</p>
				{:else}
					{@const nav = navPageFor(hit.href)}
					<a href="/help/pages#{encodeURIComponent(hit.href)}">{nav ? m(nav.labelKey) : hit.href}</a>
					<p lang={englishLang}><RichText text={PAGE_HELP[hit.href].summary} /></p>
				{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.title {
		margin: 8px 0 8px;
		font-size: clamp(1.5rem, 1.2rem + 1vw, 2rem);
		font-weight: 800;
		letter-spacing: -0.02em;
	}
	.status {
		color: var(--text-muted);
	}
	.hits {
		margin: 16px 0 0;
		padding: 0;
		list-style: none;
		max-width: 48rem;
	}
	.hits li {
		padding: 14px 0;
		border-bottom: 1px solid var(--border);
	}
	.kind {
		margin-right: 8px;
	}
	.hits a {
		color: var(--accent);
		font-weight: 600;
	}
	.hits p {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	.hits p :global(a) {
		font-weight: 400;
	}
</style>
