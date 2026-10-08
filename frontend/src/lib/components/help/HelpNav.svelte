<script lang="ts">
	/**
	 * The help centre's contents list: the overview, then one disclosure group
	 * per kind of guide (the how-tos split by area), then the reference pages.
	 * The group holding the page you're on opens by itself as you move; the
	 * others fold, so the column stays short enough to sit beside the text
	 * without scrolling on its own. `aria-current="page"` marks where you are.
	 *
	 * Guide titles are help prose (English, `lang="en"` under another locale —
	 * `#lib/help/types.ts`); group names and the reference links are UI copy.
	 */
	import { page } from '$app/state';
	import type { AnyMessageKey } from '#lib/i18n/messages.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { CONCEPT_GUIDES } from '#lib/help/guides/concepts.ts';
	import { START_GUIDES } from '#lib/help/guides/start.ts';
	import { HOWTO_GROUPS, type Guide } from '#lib/help/content.ts';

	type Group = { id: string; key: AnyMessageKey; links: { href: string; label: string; english: boolean }[] };

	const guideLinks = (guides: Guide[]) =>
		guides.map((g) => ({ href: `/help/guides/${g.id}`, label: g.title, english: true }));

	const groups: Group[] = $derived([
		{ id: 'start', key: 'help.kind.start', links: guideLinks(START_GUIDES) },
		...HOWTO_GROUPS.map((g, i) => ({ id: `howto-${i}`, key: g.key, links: guideLinks(g.guides) })),
		{ id: 'concept', key: 'help.kind.concept', links: guideLinks(CONCEPT_GUIDES) },
		{
			id: 'reference',
			key: 'help.nav.reference',
			links: [
				{ href: '/help/pages', label: m('help.pages.title'), english: false },
				{ href: '/help/glossary', label: m('help.glossary.title'), english: false }
			]
		}
	]);

	const here = $derived(page.url.pathname.replace(/\/$/, ''));
	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');
	const currentGroup = $derived(groups.find((g) => g.links.some((l) => l.href === here))?.id);

	let opened = $state<Record<string, boolean>>({});
	// Moving to a page opens its group (and leaves any the reader opened alone).
	$effect(() => {
		const id = currentGroup;
		if (id) opened[id] = true;
	});
</script>

<nav class="help-nav" aria-label={m('help.contents')}>
	<a class="overview" href="/help" aria-current={here === '/help' ? 'page' : undefined}>{m('help.nav.overview')}</a>
	{#each groups as group (group.id)}
		<section class="group">
			<h2>
				<button
					type="button"
					aria-expanded={!!opened[group.id]}
					aria-controls="help-nav-{group.id}"
					onclick={() => (opened[group.id] = !opened[group.id])}
				>
					<span>{m(group.key)}</span>
					<svg class="chev" aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
						><polyline points="6 9 12 15 18 9" /></svg
					>
				</button>
			</h2>
			<ul id="help-nav-{group.id}" hidden={!opened[group.id]}>
				{#each group.links as link (link.href)}
					<li>
						<a
							href={link.href}
							lang={link.english ? englishLang : undefined}
							aria-current={here === link.href ? 'page' : undefined}>{link.label}</a
						>
					</li>
				{/each}
			</ul>
		</section>
	{/each}
</nav>

<style>
	.help-nav {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: 0.875rem;
	}
	a {
		display: block;
		padding: 6px 10px;
		border-radius: var(--radius-sm);
		color: var(--text-muted);
		text-decoration: none;
		line-height: 1.35;
	}
	a:hover {
		color: var(--text);
		background: var(--surface);
	}
	/* The sidebar's "you are here": a wash of the tenant's accent, label on --text. */
	a[aria-current='page'] {
		color: var(--text);
		background: color-mix(in srgb, var(--accent) 14%, transparent);
		box-shadow: inset 3px 0 0 var(--accent);
		font-weight: 600;
	}
	.overview {
		font-weight: 600;
		color: var(--text);
	}
	.group h2 {
		margin: 0;
		font-size: inherit;
	}
	.group button {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		min-height: 32px;
		padding: 6px 10px;
		border: 0;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text);
		font: inherit;
		font-weight: 600;
		text-align: left;
		cursor: pointer;
	}
	.group button:hover {
		background: var(--surface);
	}
	.chev {
		flex: none;
		color: var(--text-muted);
		transition: transform 0.15s var(--ease-out);
	}
	button[aria-expanded='true'] .chev {
		transform: rotate(180deg);
	}
	ul {
		margin: 2px 0 8px 10px;
		padding: 0 0 0 8px;
		list-style: none;
		border-left: 1px solid var(--border);
	}
	@media (prefers-reduced-motion: reduce) {
		.chev {
			transition: none;
		}
	}
</style>
