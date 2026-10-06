<script lang="ts">
	/**
	 * Page by page: every sidebar destination in sidebar order, grouped as the
	 * sidebar groups them, plus the account pages, each with what it is for and
	 * its guide. A page the reader's role can't open stays listed — knowing a
	 * page exists and who to ask is help too — but is marked, and isn't a link
	 * (it would only 403). The gate is the sidebar's own `canSee`.
	 *
	 * Each row is an anchor (`#<encoded href>`), the target of search results
	 * and of a page header's "How this page works" link when a page has no
	 * guide of its own.
	 */
	import { page } from '$app/state';
	import { tick } from 'svelte';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { brand } from '#lib/stores/brand.svelte.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { canSee } from '#lib/nav.ts';
	import { NAV_PAGES, PAGE_HELP, guideFor } from '#lib/help/content.ts';
	import RichText from '#lib/components/help/RichText.svelte';
	import HelpCrumbs from '#lib/components/help/HelpCrumbs.svelte';
	import EnglishNotice from '#lib/components/help/EnglishNotice.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';

	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');
	const has = (...roles: string[]) => auth.hasAnyRole(...roles);
	const can = (perm: string) => auth.can(perm);

	type Row = { href: string; labelKey?: MessageKey; label?: string; open: boolean };
	type Group = { key: MessageKey; rows: Row[] };

	const ACCOUNT_PAGES: { href: string; labelKey: MessageKey }[] = [
		{ href: '/profile', labelKey: 'shell.profileAndSecurity' },
		{ href: '/notifications', labelKey: 'help.pages.notifications' }
	];

	const groups = $derived.by((): Group[] => {
		const out: Group[] = [{ key: 'help.pages.main', rows: [] }];
		for (const p of NAV_PAGES) {
			const row = { href: p.href, labelKey: p.labelKey, open: canSee(p.roles, has, p.permissions, can) };
			if (!p.groupKey) out[0].rows.push(row);
			else {
				const g = out.find((x) => x.key === p.groupKey);
				if (g) g.rows.push(row);
				else out.push({ key: p.groupKey, rows: [row] });
			}
		}
		out.push({ key: 'help.pages.account', rows: ACCOUNT_PAGES.map((p) => ({ ...p, open: true })) });
		return out;
	});

	const anchor = (href: string) => encodeURIComponent(href);
	// The hash is the reader's to type: a malformed escape must not crash the page.
	function decodeHash(hash: string): string {
		try {
			return decodeURIComponent(hash);
		} catch {
			return hash;
		}
	}
	const target = $derived(decodeHash(page.url.hash.slice(1)));
	$effect(() => {
		const href = target;
		if (!href) return;
		tick().then(() => document.getElementById(anchor(href))?.scrollIntoView({ block: 'start' }));
	});
</script>

<svelte:head>
	<title>{m('help.pages.title')} · {m('help.title')} · {brand.productName}</title>
</svelte:head>

<HelpCrumbs trail={[{ href: '/help#ref-h', label: m('help.nav.reference') }]} current={m('help.pages.title')} />
<h1 class="title">{m('help.pages.title')}</h1>
<p class="intro">{m('help.pages.intro')}</p>
<EnglishNotice />

{#each groups as g (g.key)}
	<section class="group" aria-labelledby="pg-{g.key}">
		<h2 id="pg-{g.key}">{m(g.key)}</h2>
		<ul>
			{#each g.rows as row (row.href)}
				{@const help = PAGE_HELP[row.href]}
				{@const guide = help?.guide ? guideFor(help.guide) : undefined}
				<li id={anchor(row.href)} class:target={target === row.href} class:closed={!row.open} data-testid="help-page-row">
					<div class="name">
						{#if row.open}
							<a href={row.href}>{row.labelKey ? m(row.labelKey) : row.label}</a>
						{:else}
							<span>{row.labelKey ? m(row.labelKey) : row.label}</span>
							<Badge tone="muted">{m('help.pages.notForYou')}</Badge>
						{/if}
					</div>
					{#if help}
						<p class="what" lang={englishLang}><RichText text={help.summary} /></p>
					{/if}
					{#if guide}
						<a class="guide" href="/help/guides/{guide.id}">
							{m('help.pages.guide')}: <span lang={englishLang}>{guide.title}</span>
						</a>
					{/if}
				</li>
			{/each}
		</ul>
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
	.group {
		margin-top: 32px;
	}
	.group h2 {
		margin: 0 0 6px;
		font-size: 1.1rem;
		font-weight: 700;
	}
	ul {
		margin: 0;
		padding: 0;
		list-style: none;
		max-width: 52rem;
	}
	li {
		padding: 12px 0;
		border-bottom: 1px solid var(--border);
		scroll-margin-top: 20px;
	}
	li.target {
		margin: 0 -14px;
		padding: 12px 14px;
		border-radius: var(--radius-sm);
		background: var(--accent-wash);
	}
	.name {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		font-weight: 600;
	}
	.name a {
		color: var(--text);
	}
	.name a:hover {
		color: var(--accent);
	}
	.closed .name > span:first-child {
		color: var(--text-muted);
	}
	.what {
		margin: 4px 0 4px;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	.what :global(a),
	.guide {
		color: var(--accent);
	}
	.guide {
		font-size: 0.85rem;
	}
</style>
