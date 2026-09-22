<script lang="ts">
	import type { Snippet } from 'svelte';

	import { m } from '$lib/i18n/store.svelte';

	type Column = {
		/** Header text. Omit for the actions / checkbox column. */
		label?: string;
		/** Optional class on the `<th>` (e.g. `right`, `actions-col`, `checkbox-col`). */
		class?: string;
	};

	type Props = {
		/** Simple header row. Provide this OR the `header` snippet, not both. */
		columns?: Column[];
		/** Custom `<tr>…</tr>` header row for select-all checkboxes / sortable headers. */
		header?: Snippet;
		/** Renders the `<tr>` rows inside `<tbody>`. */
		body: Snippet;
		/** Shown as a single centred row when `isEmpty` is true. */
		empty?: string;
		isEmpty?: boolean;
		/** colspan for the empty row; defaults to the column count. */
		colspan?: number;
		/** `table-layout: fixed` — pair with explicit `<th>` widths in the page. */
		fixed?: boolean;
		/** Sticky header row that pins to the top of the viewport on scroll. */
		stickyHeader?: boolean;
		/**
		 * Accessible name of the scroll region. Name the table when the page
		 * has more than one, or when "which table" is not obvious from the
		 * heading above it; the default is a generic `common.tableRegion`.
		 */
		ariaLabel?: string;
	};

	let {
		columns,
		header,
		body,
		empty = 'No items.',
		isEmpty = false,
		colspan,
		fixed = false,
		stickyHeader = false,
		ariaLabel
	}: Props = $props();

	const emptySpan = $derived(colspan ?? columns?.length ?? 1);
</script>

<!-- The container scrolls sideways once the table is wider than the card
     (WCAG 1.4.10 lets a table that cannot reflow scroll inside itself). A
     region that scrolls must also be reachable by keyboard (2.1.1), and a
     table whose cells hold nothing focusable — a read-only report — gives the
     keyboard no other way in, so the container is the tab stop: focused, the
     arrow keys pan it. axe reports the absence as `scrollable-region-focusable`,
     but only once the table actually overflows, which depends on viewport and
     font width; the attribute is unconditional so correctness does not. The
     `role`/name pair is what a screen reader announces on that stop — a bare
     focusable `div` is announced as nothing. Same rule as the legal pages'
     `.table-scroll` (`lib/legal/LegalPage.svelte`). -->
<div
	class="grid-container"
	role="region"
	aria-label={ariaLabel ?? m('common.tableRegion')}
	tabindex="0"
>
	<table class:fixed class:sticky-header={stickyHeader}>
		<thead>
			{#if header}
				{@render header()}
			{:else if columns}
				<tr>
					{#each columns as col}
						<!-- WCAG 1.3.1: scope ties each header to its column for AT. -->
						<th scope="col" class={col.class ?? null}>{col.label ?? ''}</th>
					{/each}
				</tr>
			{/if}
		</thead>
		<tbody>
			{#if isEmpty}
				<!-- `data-testid` is a real API, not scaffolding: it is the one
				     deterministic handle a test has on "the table is asserting
				     something about an empty result set" (loading / errored /
				     genuinely empty are distinguished by the message text). -->
				<tr><td class="empty" colspan={emptySpan} data-testid="table-empty">{empty}</td></tr>
			{:else}
				{@render body()}
			{/if}
		</tbody>
	</table>
</div>

<style>
	/* Opt-in layout refinements; the base table styling is global (app.css). */
	table.fixed {
		table-layout: fixed;
	}
	table.sticky-header thead {
		position: sticky;
		top: 0;
		z-index: 1;
	}
</style>
