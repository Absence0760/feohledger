<script lang="ts">
	import type { Snippet } from 'svelte';
	import { page } from '$app/state';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { pageHelpHref } from '#lib/help/routeHelp.ts';

	type Props = {
		/** Rendered as `<h1>` — the e2e suite selects pages by this heading. */
		title: string;
		/** Right-aligned toolbar buttons (e.g. `+ Upload`, SearchBox). */
		actions?: Snippet;
		/** Page body — filters, table, pagination, etc. */
		children: Snippet;
	};

	let { title, actions, children }: Props = $props();

	// "How this page works": every page in the help centre's page directory
	// (`#lib/help/pages.ts`, which content.test.ts holds to the sidebar) gets a
	// link to its guide — or to its row in the directory — with no per-route
	// wiring. A page off the directory simply shows none.
	const helpHref = $derived(pageHelpHref(page.url.pathname, page.url.search));
</script>

<div class="workspace">
	<header class="toolbar">
		<div class="toolbar-title">
			<h1>{title}</h1>
			{#if helpHref}
				<a class="page-help" href={helpHref} data-testid="page-help-link">
					<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
						><circle cx="12" cy="12" r="10" /><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg
					>
					<span>{m('help.pageLink')}</span>
				</a>
			{/if}
		</div>
		{#if actions}
			<div class="toolbar-actions">{@render actions()}</div>
		{/if}
	</header>
	{@render children()}
</div>

<style>
	.toolbar-title {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px 14px;
		min-width: 0;
	}
	.page-help {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-height: 24px;
		color: var(--text-muted);
		font-size: 0.82rem;
		text-decoration: none;
	}
	.page-help:hover {
		color: var(--accent);
		text-decoration: underline;
	}
</style>
