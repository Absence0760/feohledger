<script lang="ts">
	/**
	 * One guide: breadcrumb, title and summary, who it is for, a button into
	 * the page the work happens on (only when the reader's role can open it —
	 * the same `canSee` gate the sidebar uses, so help never offers a page that
	 * would 403), an "On this page" list, the sections, then the terms it uses
	 * and the guides to read next.
	 *
	 * The prose is English (`lang="en"` under another locale, behind
	 * EnglishNotice); the chrome around it and every `{ui:…}` label inside it
	 * are in the reader's language.
	 */
	import { page } from '$app/state';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { brand } from '#lib/stores/brand.svelte.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { canSee } from '#lib/nav.ts';
	import { ROLE_LABEL_KEYS } from '#lib/types/admin.ts';
	import {
		GUIDE_KIND_KEYS,
		guideFor,
		navPageFor,
		termFor,
		termHref,
		termKey,
		termShortKey
	} from '#lib/help/content.ts';
	import { headingId } from '#lib/help/anchor.ts';
	import RichText from '#lib/components/help/RichText.svelte';
	import HelpCrumbs from '#lib/components/help/HelpCrumbs.svelte';
	import EnglishNotice from '#lib/components/help/EnglishNotice.svelte';
	import InvoiceLifecycle from '#lib/components/help/InvoiceLifecycle.svelte';
	import Diagram from '#lib/components/help/diagrams/Diagram.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';

	const guide = $derived(guideFor(page.params.id ?? ''));
	// A breadcrumb lands where its words say: the landing page's section for
	// that kind of guide (role starts and their task lists share "Start here").
	const KIND_ANCHORS = { start: '/help#start-h', howto: '/help#start-h', concept: '/help#how-h' } as const;
	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');

	const has = (...roles: string[]) => auth.hasAnyRole(...roles);
	const can = (perm: string) => auth.can(perm);
	const target = $derived.by(() => {
		if (!guide?.route) return undefined;
		const nav = navPageFor(guide.route);
		if (nav && !canSee(nav.roles, has, nav.permissions, can)) return undefined;
		return { href: guide.route, label: nav ? m(nav.labelKey) : undefined };
	});
	const terms = $derived((guide?.terms ?? []).filter((id) => termFor(id)));
	const related = $derived((guide?.related ?? []).flatMap((id) => guideFor(id) ?? []));
</script>

<svelte:head>
	<title>{guide?.title ?? m('help.guide.notFoundTitle')} · {m('help.title')} · {brand.productName}</title>
</svelte:head>

{#if !guide}
	<HelpCrumbs trail={[]} current={m('help.guide.notFoundTitle')} />
	<h1 class="title">{m('help.guide.notFoundTitle')}</h1>
	<p>{m('help.guide.notFound')}</p>
	<p><a href="/help">{m('help.guide.backToHelp')}</a></p>
{:else}
	{#key guide.id}
		<article class="guide" data-testid="help-guide">
			<HelpCrumbs trail={[{ href: KIND_ANCHORS[guide.kind], label: m(GUIDE_KIND_KEYS[guide.kind]) }]} current={guide.title} currentLang={englishLang} />
			<header>
				<h1 class="title" lang={englishLang}>{guide.title}</h1>
				<p class="summary" lang={englishLang}>{guide.summary}</p>
				<div class="meta">
					<span class="for">
						<span class="meta-label">{m('help.guide.for')}</span>
						{#if guide.roles?.length}
							{#each guide.roles as r (r)}<Badge tone="muted">{m(ROLE_LABEL_KEYS[r])}</Badge>{/each}
						{:else}
							<Badge tone="muted">{m('help.guide.everyone')}</Badge>
						{/if}
					</span>
					{#if target}
						<a class="btn-primary open" href={target.href} data-testid="help-open-page">
							{target.label ? m('help.guide.open', { page: target.label }) : m('help.guide.openPage')}
						</a>
					{/if}
				</div>
				<EnglishNotice />
			</header>

			<div class="guide-grid" class:has-toc={guide.sections.length > 1}>
			{#if guide.sections.length > 1}
				<nav class="toc" aria-label={m('help.guide.onThisPage')}>
					<h2>{m('help.guide.onThisPage')}</h2>
					<ol>
						{#each guide.sections as s (s.heading)}
							<li><a href="#{headingId(s.heading)}" lang={englishLang}>{s.heading}</a></li>
						{/each}
					</ol>
				</nav>
			{/if}

			<div class="body" lang={englishLang}>
				{#each guide.sections as s (s.heading)}
					<section>
						<h2 id={headingId(s.heading)}>{s.heading}</h2>
						{#each s.blocks as b, i (i)}
							{#if b.type === 'p'}
								<p><RichText text={b.text} /></p>
							{:else if b.type === 'steps'}
								<ol class="steps">
									{#each b.items as item, j (j)}<li><RichText text={item} /></li>{/each}
								</ol>
							{:else if b.type === 'list'}
								<ul class="list">
									{#each b.items as item, j (j)}<li><RichText text={item} /></li>{/each}
								</ul>
							{:else if b.type === 'note'}
								<aside class="note {b.tone}">
									<span class="note-label" lang={englishLang ? currentLocale() : undefined}
										>{m(b.tone === 'tip' ? 'help.note.tip' : b.tone === 'caution' ? 'help.note.caution' : 'help.note.role')}</span
									>
									<span><RichText text={b.text} /></span>
								</aside>
							{:else if b.type === 'diagram'}
								<figure class="figure diagram-figure">
									<div lang={englishLang ? currentLocale() : undefined}><Diagram id={b.id} /></div>
									<figcaption><RichText text={b.caption} /></figcaption>
								</figure>
							{:else if b.type === 'lifecycle'}
								<div class="figure" lang={englishLang ? currentLocale() : undefined}><InvoiceLifecycle /></div>
							{/if}
						{/each}
					</section>
				{/each}
			</div>
			</div>

			{#if terms.length}
				<section class="aside-block" aria-labelledby="terms-h">
					<h2 id="terms-h">{m('help.guide.terms')}</h2>
					<dl class="terms">
						{#each terms as id (id)}
							<div>
								<dt><a href={termHref(id)}>{m(termKey(id))}</a></dt>
								<dd>{m(termShortKey(id))}</dd>
							</div>
						{/each}
					</dl>
				</section>
			{/if}

			{#if related.length}
				<section class="aside-block" aria-labelledby="related-h">
					<h2 id="related-h">{m('help.guide.related')}</h2>
					<ul class="related">
						{#each related as g (g.id)}
							<li>
								<a href="/help/guides/{g.id}" lang={englishLang}>{g.title}</a>
								<p lang={englishLang}>{g.summary}</p>
							</li>
						{/each}
					</ul>
				</section>
			{/if}
		</article>
	{/key}
{/if}

<style>
	/* Header and closing sections share the text column's measure; on a
	   wide help column the "On this page" list becomes a sticky rail to its
	   right instead of a box above the text, so the width is used for
	   navigation rather than left empty. */
	.guide > :global(*) {
		max-width: 50rem;
	}
	.guide > .guide-grid {
		max-width: none;
	}
	.guide-grid {
		display: grid;
		grid-template-columns: minmax(0, 50rem);
	}
	@container help-main (min-width: 68rem) {
		.guide-grid.has-toc {
			grid-template-columns: minmax(0, 50rem) 15rem;
			column-gap: 48px;
			align-items: start;
		}
		.guide-grid.has-toc .toc {
			grid-column: 2;
			grid-row: 1;
			position: sticky;
			top: 20px;
			margin-top: 32px;
		}
		.guide-grid.has-toc .body {
			grid-column: 1;
			grid-row: 1;
		}
	}
	.title {
		margin: 8px 0 8px;
		font-size: clamp(1.5rem, 1.2rem + 1vw, 2rem);
		font-weight: 800;
		letter-spacing: -0.02em;
		line-height: 1.2;
	}
	.summary {
		margin: 0;
		color: var(--text-muted);
		font-size: 1.05rem;
		line-height: 1.55;
	}
	.meta {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-top: 16px;
	}
	.for {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	.meta-label {
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.open {
		text-decoration: none;
	}
	.toc {
		margin-top: 24px;
		padding: 14px 18px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--surface);
	}
	.toc h2 {
		margin: 0 0 6px;
		font-size: 0.78rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.toc ol {
		margin: 0;
		padding-left: 1.2rem;
	}
	.toc li {
		padding: 3px 0;
	}
	/* `.body`'s links are rendered by RichText, a child component: `:global`
	   reaches them, scoped to this page's `.body`. */
	.toc a,
	.body :global(a),
	.terms a,
	.related a {
		color: var(--accent);
	}
	.body {
		line-height: 1.65;
	}
	.body section {
		margin-top: 32px;
	}
	.body h2 {
		margin: 0 0 10px;
		font-size: 1.2rem;
		font-weight: 700;
		scroll-margin-top: 20px;
	}
	.body p {
		margin: 0 0 12px;
	}
	.steps {
		margin: 0 0 14px;
		padding: 0;
		list-style: none;
		counter-reset: step;
	}
	.steps li {
		position: relative;
		padding: 2px 0 12px 42px;
		counter-increment: step;
	}
	.steps li::before {
		content: counter(step);
		position: absolute;
		left: 0;
		top: 0;
		display: grid;
		place-items: center;
		width: 28px;
		height: 28px;
		border-radius: 50%;
		background: color-mix(in srgb, var(--accent) 18%, transparent);
		color: var(--text);
		font-size: 0.85rem;
		font-weight: 700;
	}
	.list {
		margin: 0 0 14px;
		padding-left: 1.3rem;
	}
	.list li {
		padding: 2px 0;
	}
	.note {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin: 4px 0 16px;
		padding: 12px 16px;
		border-left: 3px solid var(--border);
		border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
		background: var(--surface);
	}
	.note.tip {
		border-left-color: var(--success);
	}
	.note.caution {
		border-left-color: var(--warning-on-tint);
	}
	.note.role {
		border-left-color: var(--accent);
	}
	.note-label {
		font-size: 0.75rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.figure {
		margin: 8px 0 18px;
	}
	.diagram-figure {
		padding: 18px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--surface);
	}
	.diagram-figure figcaption {
		margin-top: 12px;
		color: var(--text-muted);
		font-size: 0.875rem;
		line-height: 1.5;
	}
	.aside-block {
		margin-top: 40px;
		padding-top: 24px;
		border-top: 1px solid var(--border);
	}
	.aside-block h2 {
		margin: 0 0 12px;
		font-size: 1.05rem;
		font-weight: 700;
	}
	.terms {
		display: grid;
		gap: 12px;
		margin: 0;
	}
	.terms dt {
		font-weight: 600;
	}
	.terms dd {
		margin: 2px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	.related {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.related li {
		padding: 10px 0;
		border-bottom: 1px solid var(--border);
	}
	.related a {
		font-weight: 600;
	}
	.related p {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
</style>
