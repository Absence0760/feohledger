<script lang="ts">
	import type { Snippet } from 'svelte';

	type Props = {
		/** When set, renders as an `<a>` for client-side navigation. Otherwise a
		 *  `<button>` that calls `onclick` (typically opening a detail/edit modal). */
		href?: string;
		/** Click handler for the button form. */
		onclick?: (e: MouseEvent) => void;
		/** Accessible name — what a screen reader announces and what e2e specs
		 *  select on. Always pass a row-specific label, e.g. "Edit invoice INV-42". */
		ariaLabel: string;
		children: Snippet;
	};

	let { href, onclick, ariaLabel, children }: Props = $props();

	// Stop the click from also reaching a parent `tr` row-open handler, so the
	// primary-cell affordance fires exactly once. Row-level fallback click is for
	// the rest of the row (see utils/rowNav.ts::isRowOpenClick).
	function handleClick(e: MouseEvent) {
		e.stopPropagation();
		onclick?.(e);
	}
</script>

{#if href}
	<!-- No stopPropagation here, unlike the button: SvelteKit's router catches
	     link clicks by delegation higher up the document, so stopping the click
	     here made every row link a FULL page reload. The parent row's handler
	     already ignores clicks that start on an `<a>` (`isRowOpenClick`). -->
	<a class="row-link" {href} aria-label={ariaLabel}>
		{@render children()}
	</a>
{:else}
	<button class="row-link" type="button" aria-label={ariaLabel} onclick={handleClick}>
		{@render children()}
	</button>
{/if}

<style>
	/* Looks like plain cell text, behaves like a link: the focusable, keyboard-
	 * and screen-reader-accessible "open this row" control that lives in a row's
	 * primary cell. Pairs with a whole-row mouse click for sighted pointer users. */
	.row-link {
		appearance: none;
		background: none;
		border: none;
		padding: 0;
		margin: 0;
		font: inherit;
		color: inherit;
		text-align: left;
		cursor: pointer;
		text-decoration: none;
		border-radius: 3px;
		transition: color 0.15s;
	}

	.row-link:hover {
		color: var(--accent);
		text-decoration: underline;
	}

	.row-link:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
		color: var(--accent);
	}
</style>
