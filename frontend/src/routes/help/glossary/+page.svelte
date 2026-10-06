<script lang="ts">
	/**
	 * The glossary: every term, grouped by topic, each with its one-line
	 * definition (the HelpTip text, in the reader's language) and its fuller
	 * English explanation. Each entry is an anchor (`/help/glossary#<id>`) —
	 * the target of every HelpTip's "More in the glossary" and every
	 * `[[term]]` link — and the linked entry is highlighted when you land on it.
	 *
	 * The topic filter is URL-backed (`?topic=`), like every filter in the app.
	 */
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { tick } from 'svelte';
	import { brand } from '#lib/stores/brand.svelte.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import {
		CATEGORY_KEYS,
		GLOSSARY,
		GLOSSARY_CATEGORIES,
		termFor,
		termKey,
		termShortKey,
		type GlossaryCategory
	} from '#lib/help/content.ts';
	import RichText from '#lib/components/help/RichText.svelte';
	import HelpCrumbs from '#lib/components/help/HelpCrumbs.svelte';
	import EnglishNotice from '#lib/components/help/EnglishNotice.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';

	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');
	const topic = $derived.by((): GlossaryCategory | null => {
		const t = page.url.searchParams.get('topic');
		return (GLOSSARY_CATEGORIES as string[]).includes(t ?? '') ? (t as GlossaryCategory) : null;
	});
	const shown = $derived(GLOSSARY_CATEGORIES.filter((c) => !topic || c === topic));
	const target = $derived(page.url.hash.slice(1));

	// Under a topic filter a related term from another topic isn't rendered, so
	// a bare `#id` would go nowhere: lift the filter for that link.
	function relatedHref(id: string): string {
		const target = termFor(id);
		return !topic || target?.category === topic ? `#${id}` : `/help/glossary#${id}`;
	}

	function pick(t: GlossaryCategory | null) {
		const url = new URL(page.url.href);
		url.hash = '';
		if (t) url.searchParams.set('topic', t);
		else url.searchParams.delete('topic');
		goto(`${url.pathname}${url.search}${url.hash}`, { replace: true, reset: false });
	}

	// The static build serves this page client-side, so the browser's own
	// jump-to-anchor runs before the entries exist. Do it once they render.
	$effect(() => {
		const id = target;
		if (!id) return;
		tick().then(() => document.getElementById(id)?.scrollIntoView({ block: 'start' }));
	});
</script>

<svelte:head>
	<title>{m('help.glossary.title')} · {m('help.title')} · {brand.productName}</title>
</svelte:head>

<HelpCrumbs trail={[{ href: '/help#ref-h', label: m('help.nav.reference') }]} current={m('help.glossary.title')} />
<h1 class="title">{m('help.glossary.title')}</h1>
<p class="intro">{m('help.glossary.intro', { product: brand.productName })}</p>
<EnglishNotice />

<div class="topics">
	<FilterChips
		chips={[
			{ key: 'all', label: m('help.glossary.allTopics') },
			...GLOSSARY_CATEGORIES.map((c) => ({ key: c, label: m(CATEGORY_KEYS[c]) }))
		]}
		active={topic ?? 'all'}
		onchange={(key) => pick(key === 'all' ? null : (key as GlossaryCategory))}
	/>
</div>

{#each shown as c (c)}
	<section class="topic" aria-labelledby="topic-{c}">
		<h2 id="topic-{c}">{m(CATEGORY_KEYS[c])}</h2>
		<dl>
			{#each GLOSSARY.filter((t) => t.category === c) as t (t.id)}
				<div class="entry" id={t.id} class:target={target === t.id} data-testid="glossary-entry">
					<dt>{m(termKey(t.id))}</dt>
					<dd>
						<p class="short">{m(termShortKey(t.id))}</p>
						<div class="long" lang={englishLang}>
							{#each t.long.split(/\n\s*\n/) as para, i (i)}<p><RichText text={para} /></p>{/each}
						</div>
						{#if t.aliases?.length}
							<div class="refs">
								<span class="label">{m('help.glossary.aliases')}</span>
								<ul>
									{#each t.aliases as a (a)}<li lang={englishLang}>{a}</li>{/each}
								</ul>
							</div>
						{/if}
						{#if t.related?.length}
							<div class="refs">
								<span class="label">{m('help.glossary.related')}</span>
								<ul>
									{#each t.related as r (r)}<li><a href={relatedHref(r)}>{m(termKey(r))}</a></li>{/each}
								</ul>
							</div>
						{/if}
					</dd>
				</div>
			{/each}
		</dl>
	</section>
{/each}

<style>
	.title {
		margin: 8px 0 8px;
		font-size: clamp(1.5rem, 1.2rem + 1vw, 2rem);
		font-weight: 800;
		letter-spacing: -0.02em;
	}
	.intro {
		margin: 0;
		color: var(--text-muted);
		max-width: 46rem;
	}
	.topics {
		margin: 20px 0 8px;
	}
	.topic {
		margin-top: 32px;
	}
	.topic h2 {
		margin: 0 0 8px;
		font-size: 1.15rem;
		font-weight: 700;
	}
	dl {
		margin: 0;
	}
	.entry {
		padding: 16px 0;
		border-bottom: 1px solid var(--border);
		scroll-margin-top: 20px;
	}
	.entry.target {
		margin: 0 -14px;
		padding: 16px 14px;
		border-radius: var(--radius-sm);
		background: var(--accent-wash);
	}
	dt {
		font-weight: 700;
		font-size: 1.02rem;
	}
	dd {
		margin: 4px 0 0;
		max-width: 48rem;
	}
	.short {
		margin: 0 0 8px;
		font-weight: 500;
	}
	.long p {
		margin: 0 0 8px;
		color: var(--text-muted);
		line-height: 1.6;
	}
	.long :global(a),
	.refs a {
		color: var(--accent);
	}
	/* Separate items, not a ", "-joined string: the aliases are English and the
	   related names are the reader's language, so no one separator fits both. */
	.refs {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 2px 8px;
		margin-top: 4px;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.refs ul {
		display: contents;
		list-style: none;
	}
	/* A 24px target per link (WCAG 2.5.8): a long "See also" wraps, and
	   wrapped inline links otherwise sit closer than that. */
	.refs a {
		display: inline-flex;
		align-items: center;
		min-height: 24px;
	}
	.refs li + li::before {
		content: '·';
		margin-right: 8px;
		color: var(--text-muted);
	}
	.label {
		font-weight: 600;
		color: var(--text);
	}
</style>
