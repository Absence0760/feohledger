<script lang="ts">
	/**
	 * The help centre's front page, in the order a newcomer needs it:
	 *
	 *  1. Start here, for your role — the signed-in user's own role is chosen
	 *     for them (`?role=` picks another, so a link can point a colleague at
	 *     theirs), with that role's first-week guide and its everyday tasks.
	 *  2. Follow an invoice — the lifecycle walkthrough.
	 *  3. How it works — the concept guides.
	 *  4. Reference — the page directory and the glossary.
	 *
	 * Guide titles and summaries are English help prose (`lang="en"` under
	 * another UI locale, with the notice saying so); everything else is UI copy.
	 */
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { brand } from '#lib/stores/brand.svelte.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { ROLE_LABEL_KEYS } from '#lib/types/admin.ts';
	import {
		CONCEPT_GUIDES_LIST,
		GLOSSARY,
		GUIDES,
		HELP_ROLES,
		PAGE_HELP,
		ROLE_START,
		guideFor,
		type HelpRole
	} from '#lib/help/content.ts';
	import InvoiceLifecycle from '#lib/components/help/InvoiceLifecycle.svelte';
	import EnglishNotice from '#lib/components/help/EnglishNotice.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';

	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');

	/** The role a reader most likely came for: the most senior they hold. */
	const ownRole = $derived(
		(['admin', 'cfo', 'ap_manager', 'ap_clerk'] as HelpRole[]).find((r) => auth.hasRole(r)) ?? 'ap_clerk'
	);
	const role = $derived.by((): HelpRole => {
		const r = page.url.searchParams.get('role');
		return (HELP_ROLES as string[]).includes(r ?? '') ? (r as HelpRole) : ownRole;
	});
	const start = $derived(guideFor(ROLE_START[role])!);
	const tasks = $derived(GUIDES.filter((g) => g.kind === 'howto' && g.roles?.includes(role)));
	// Eight is a screenful beside the start card; an admin's list runs to twenty.
	const TASKS_SHOWN = 8;
	let allTasks = $state(false);
	const shownTasks = $derived(allTasks ? tasks : tasks.slice(0, TASKS_SHOWN));

	function pick(r: HelpRole) {
		const url = new URL(page.url.href);
		if (r === ownRole) url.searchParams.delete('role');
		else url.searchParams.set('role', r);
		goto(`${url.pathname}${url.search}${url.hash}`, { replace: true, reset: false });
	}
</script>

<svelte:head>
	<title>{m('help.title')} · {brand.productName}</title>
</svelte:head>

<header class="hero">
	<p class="eyebrow">{m('help.title')}</p>
	<h1>{m('help.hero.title', { product: brand.productName })}</h1>
	<p class="lede">{m('help.hero.lede', { product: brand.productName })}</p>
	<EnglishNotice />
</header>

<section class="block" aria-labelledby="start-h">
	<div class="block-head">
		<h2 id="start-h">{m('help.start.heading')}</h2>
	</div>
	<div class="roles">
		<p class="pick-label">{m('help.start.pickRole')}</p>
		<FilterChips
			chips={HELP_ROLES.map((r) => ({
				key: r,
				label: m(ROLE_LABEL_KEYS[r]),
				count: r === ownRole ? m('help.start.you') : undefined
			}))}
			active={role}
			onchange={(key) => pick(key as HelpRole)}
		/>
	</div>

	<div class="start-grid">
		<a class="start-card" href="/help/guides/{start.id}" data-testid="help-start-card">
			<span class="start-kicker">{m('help.start.firstWeek')}</span>
			<span class="start-title" lang={englishLang}>{start.title}</span>
			<span class="start-summary" lang={englishLang}>{start.summary}</span>
			<span class="start-go" aria-hidden="true">→</span>
		</a>
		<div class="tasks">
			<h3>{m('help.start.tasks')}</h3>
			<ul>
				{#each shownTasks as g (g.id)}
					<li><a href="/help/guides/{g.id}" lang={englishLang}>{g.title}</a></li>
				{/each}
			</ul>
			{#if tasks.length > TASKS_SHOWN}
				<button type="button" class="more-tasks" aria-expanded={allTasks} onclick={() => (allTasks = !allTasks)}>
					{allTasks ? m('help.start.fewerTasks') : m('help.start.allTasks', { n: tasks.length })}
				</button>
			{/if}
		</div>
	</div>
</section>

<section class="block" aria-labelledby="life-h">
	<div class="block-head">
		<h2 id="life-h">{m('help.landing.lifecycle')}</h2>
		<a href="/help/guides/invoice-lifecycle">{m('help.landing.lifecycleGuide')} →</a>
	</div>
	<p class="intro">{m('help.landing.lifecycleIntro')}</p>
	<InvoiceLifecycle />
</section>

<section class="block" aria-labelledby="how-h">
	<div class="block-head">
		<h2 id="how-h">{m('help.kind.concept')}</h2>
	</div>
	<ul class="concepts">
		{#each CONCEPT_GUIDES_LIST as g (g.id)}
			<li>
				<a href="/help/guides/{g.id}" lang={englishLang}>{g.title}</a>
				<p lang={englishLang}>{g.summary}</p>
			</li>
		{/each}
	</ul>
</section>

<section class="block" aria-labelledby="ref-h">
	<div class="block-head">
		<h2 id="ref-h">{m('help.nav.reference')}</h2>
	</div>
	<div class="ref-grid">
		<a class="ref-card" href="/help/pages">
			<span class="ref-title">{m('help.pages.title')}</span>
			<span class="ref-text">{m('help.landing.pagesBlurb', { count: Object.keys(PAGE_HELP).length })}</span>
		</a>
		<a class="ref-card" href="/help/glossary">
			<span class="ref-title">{m('help.glossary.title')}</span>
			<span class="ref-text">{m('help.landing.glossaryBlurb', { count: GLOSSARY.length })}</span>
		</a>
	</div>
</section>

<style>
	.hero {
		max-width: 48rem;
	}
	.eyebrow {
		margin: 0;
		color: var(--accent);
		font-size: 0.78rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}
	h1 {
		margin: 6px 0 10px;
		font-size: clamp(1.6rem, 1.2rem + 1.4vw, 2.2rem);
		font-weight: 800;
		letter-spacing: -0.025em;
		line-height: 1.15;
	}
	.lede {
		margin: 0;
		color: var(--text-muted);
		font-size: 1.02rem;
		line-height: 1.6;
	}
	.block {
		margin-top: 40px;
	}
	.block + .block {
		padding-top: 32px;
		border-top: 1px solid var(--border);
	}
	.block-head {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		justify-content: space-between;
		gap: 4px 16px;
	}
	.block-head h2 {
		margin: 0;
		font-size: 1.25rem;
		font-weight: 700;
	}
	.block-head a {
		color: var(--accent);
		font-size: 0.9rem;
		font-weight: 600;
	}
	.intro {
		margin: 6px 0 16px;
		color: var(--text-muted);
		max-width: 48rem;
	}
	.roles {
		margin: 12px 0 16px;
	}
	.pick-label {
		margin: 0 0 8px;
		color: var(--text-muted);
		font-size: 0.875rem;
	}
	.start-grid {
		display: grid;
		gap: 16px;
		align-items: start;
	}
	.more-tasks {
		margin-top: 8px;
		min-height: 32px;
		padding: 4px 0;
		border: 0;
		background: none;
		color: var(--accent);
		font: inherit;
		font-size: 0.875rem;
		font-weight: 600;
		cursor: pointer;
	}
	@container help-main (min-width: 44rem) {
		.start-grid {
			grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
		}
	}
	.start-card {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 22px 48px 22px 22px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: radial-gradient(600px 200px at 0% 0%, var(--accent-wash), transparent 70%), var(--surface);
		box-shadow: var(--shadow-card);
		color: var(--text);
		text-decoration: none;
		transition: transform 0.15s var(--ease-out), border-color 0.15s var(--ease-out);
	}
	.start-card:hover {
		transform: translateY(-1px);
		border-color: var(--accent);
	}
	.start-kicker {
		color: var(--accent);
		font-size: 0.75rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}
	.start-title {
		font-size: 1.2rem;
		font-weight: 700;
	}
	.start-summary {
		color: var(--text-muted);
		line-height: 1.55;
	}
	.start-go {
		position: absolute;
		right: 20px;
		top: 50%;
		transform: translateY(-50%);
		color: var(--accent);
		font-size: 1.4rem;
	}
	.tasks h3 {
		margin: 0 0 8px;
		font-size: 0.8rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.tasks ul,
	.concepts {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.tasks li a {
		display: block;
		padding: 8px 0;
		border-bottom: 1px solid var(--border);
		color: var(--text);
		text-decoration: none;
	}
	.tasks li a:hover {
		color: var(--accent);
	}
	.concepts {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(17rem, 100%), 1fr));
		gap: 0 32px;
	}
	.concepts li {
		padding: 14px 0;
		border-bottom: 1px solid var(--border);
	}
	.concepts a {
		color: var(--text);
		font-weight: 600;
		text-decoration: none;
	}
	.concepts a:hover {
		color: var(--accent);
	}
	.concepts p {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
		line-height: 1.5;
	}
	.ref-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(16rem, 100%), 1fr));
		gap: 16px;
		margin-top: 14px;
	}
	.ref-card {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 18px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--surface);
		color: var(--text);
		text-decoration: none;
	}
	.ref-card:hover {
		border-color: var(--accent);
	}
	.ref-title {
		font-weight: 700;
	}
	.ref-text {
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	@media (prefers-reduced-motion: reduce) {
		.start-card {
			transition: none;
		}
		.start-card:hover {
			transform: none;
		}
	}
</style>
