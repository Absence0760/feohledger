<script lang="ts">
	import type { Snippet } from 'svelte';

	/**
	 * The two-column chat workspace shared by `/assistant` and `/cash-flow`: a
	 * side rail (usage, history, options) beside a chat pane whose message log
	 * scrolls on its own and whose composer sits under it.
	 *
	 * Below 720px the rail does not stack above the chat: it would push the
	 * composer off a phone's first screen. It collapses behind a one-line
	 * disclosure button instead, so the chat pane and its composer follow the
	 * page header directly. The button precedes the rail in the DOM, so focus
	 * order follows the visual order whether the rail is open or not, and the
	 * rail is never re-ordered with CSS. Above 720px the button is `display: none`
	 * and the rail is always shown.
	 */
	type Props = {
		/** The narrow-width disclosure button's text, naming what the rail holds. */
		railLabel: string;
		/** `id` for the rail, which the disclosure button controls. */
		railId: string;
		rail: Snippet;
		/** The message log: rendered inside the scrolling `role="log"` region. */
		log: Snippet;
		/** Under the log: notices and the composer. */
		footer: Snippet;
		busy?: boolean;
		/** The log's scroll container, for scroll-to-bottom. */
		scrollEl?: HTMLDivElement | null;
	};

	let {
		railLabel,
		railId,
		rail,
		log,
		footer,
		busy = false,
		scrollEl = $bindable(null)
	}: Props = $props();

	let railOpen = $state(false);
</script>

<div class="chat-layout">
	<button
		type="button"
		class="rail-disclosure"
		aria-expanded={railOpen}
		aria-controls={railId}
		onclick={() => (railOpen = !railOpen)}
	>
		<svg
			aria-hidden="true"
			class="chevron"
			class:open={railOpen}
			width="14"
			height="14"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"><polyline points="9 6 15 12 9 18" /></svg
		>
		<span>{railLabel}</span>
	</button>

	<aside id={railId} class="chat-rail" class:collapsed={!railOpen}>
		{@render rail()}
	</aside>

	<section class="chat-pane">
		<div class="chat-scroll" bind:this={scrollEl} role="log" aria-live="polite" aria-busy={busy}>
			{@render log()}
		</div>
		{@render footer()}
	</section>
</div>

<style>
	.chat-layout {
		display: grid;
		grid-template-columns: 240px minmax(0, 1fr);
		gap: 16px;
		flex: 1 1 auto;
		min-height: 0;
	}
	.rail-disclosure {
		display: none;
	}
	.chat-rail {
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 14px;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--surface);
		height: fit-content;
		max-height: calc(100vh - 140px);
		overflow-y: auto;
	}
	.chat-pane {
		display: flex;
		flex-direction: column;
		gap: 12px;
		min-height: 0;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--bg);
		overflow: hidden;
	}
	.chat-scroll {
		flex: 1 1 auto;
		min-height: 420px;
		max-height: calc(100vh - 220px);
		overflow-y: auto;
		padding: 20px;
	}

	@media (max-width: 720px) {
		.chat-layout {
			grid-template-columns: minmax(0, 1fr);
			gap: 10px;
		}
		.rail-disclosure {
			display: flex;
			align-items: center;
			gap: 8px;
			width: 100%;
			min-height: 40px;
			padding: 8px 12px;
			border: 1px solid var(--border);
			border-radius: 10px;
			background: var(--surface);
			color: var(--text);
			font-family: inherit;
			font-size: 0.85rem;
			font-weight: 500;
			text-align: left;
			cursor: pointer;
		}
		.rail-disclosure:hover {
			border-color: var(--accent);
		}
		.chevron {
			flex-shrink: 0;
			color: var(--text-muted);
		}
		.chevron.open {
			transform: rotate(90deg);
		}
		.chat-rail {
			max-height: none;
		}
		.chat-rail.collapsed {
			display: none;
		}
		/* The log takes what the viewport leaves after the header, the
		   disclosure row and the composer, so the composer is on the first
		   screen; a fixed 420px floor put it at ~790px on a 844px phone. The
		   floor that remains keeps a few messages readable on a short window. */
		.chat-scroll {
			min-height: 10rem;
			max-height: calc(100dvh - 340px);
			padding: 14px;
		}
	}
</style>
