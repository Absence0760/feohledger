<script lang="ts">
	import { page } from '$app/state';
	import { auth } from '$lib/stores/auth.svelte';
	import { groupForPath, sectionTabActive, visibleChildren } from '$lib/nav';
	import { m } from '$lib/i18n/store.svelte';

	// The grouped section (Procurement / Billing / Insights / Settings) that owns
	// the current route, if any. Top-level links (Invoices, Payments, …) return
	// null → no sub-tab bar. Children the current role can't see are dropped.
	let group = $derived(groupForPath(page.url.pathname));
	let tabs = $derived(
		group ? visibleChildren(group, auth.hasAnyRole.bind(auth), auth.can.bind(auth)) : []
	);
</script>

<!-- Only show the bar when the section actually offers a choice. A lone
     accessible tab (e.g. a CFO's Settings = just Audit Trail) would just
     duplicate the page title, so we suppress it. -->
{#if group && tabs.length > 1}
	<nav class="section-tabs" aria-label={m('shell.sectionNav', { group: m(group.labelKey) })}>
		<div class="section-tabs-inner">
			{#each tabs as tab (tab.href)}
				{@const active = sectionTabActive(tab, tabs, page.url)}
				<a
					href={tab.href}
					class="section-tab"
					class:active
					aria-current={active ? 'page' : undefined}
				>
					{m(tab.labelKey)}
				</a>
			{/each}
		</div>
	</nav>
{/if}

<style>
	/* Secondary nav strip above the page content. Full-width border, inner
	   content aligned to the same 1800px / 20px gutter as `.workspace` so the
	   tabs line up with the page title below. */
	/* Not sticky, on purpose. A pinned strip covers whatever the browser (or
	   Playwright) scrolls into view just beneath it — an intercepted click in
	   every grouped page's specs, and a focused control hidden under it
	   (WCAG 2.4.11), the defect the marketing header needed scroll padding for. */
	.section-tabs {
		border-bottom: 1px solid var(--border);
		/* Opaque --surface for the same reason as the sidebar: the tab labels'
		   contrast is calibrated on it, and a translucent strip would hand that
		   decision to whatever scrolls underneath. */
		background: var(--surface);
		box-shadow: 0 10px 24px -20px rgba(0, 0, 0, 0.9);
	}

	.section-tabs-inner {
		max-width: 1800px;
		margin: 0 auto;
		padding: 0 28px;
		display: flex;
		gap: 2px;
		overflow-x: auto;
	}

	.section-tab {
		position: relative;
		padding: 13px 14px;
		color: var(--text-muted);
		font-size: 0.88rem;
		font-weight: 500;
		text-decoration: none;
		white-space: nowrap;
		transition: color 0.15s;
	}
	/* The indicator is a pseudo-element that grows from the centre, so the
	   active tab reads as selected at a glance and the change of tab has a
	   moment of motion; it never moves layout. */
	.section-tab::after {
		content: '';
		position: absolute;
		left: 14px;
		right: 14px;
		bottom: -1px;
		height: 2px;
		border-radius: 2px 2px 0 0;
		background: var(--accent);
		transform: scaleX(0);
		transition: transform 0.25s var(--ease-out);
	}

	.section-tab:hover {
		color: var(--text);
	}

	.section-tab.active {
		color: var(--text);
	}
	.section-tab.active::after {
		transform: scaleX(1);
		box-shadow: 0 0 10px var(--accent-glow);
	}

	@media (max-width: 700px) {
		.section-tabs-inner {
			padding: 0 14px;
		}
	}
</style>
