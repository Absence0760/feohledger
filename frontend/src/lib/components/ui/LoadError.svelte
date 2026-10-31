<script lang="ts">
	/**
	 * A failed load, stated in place, with a way back: the message and a Retry
	 * button. THE error-and-retry primitive — a page's failed fetch and the
	 * whole-shell account load (`layout/AccountLoadState`) both render this one,
	 * so the two cannot drift into different answers to the same situation.
	 *
	 * Render it where the content would have been, in the `{:else if}` after the
	 * success branch and before the loading line. A toast is not a substitute:
	 * it fades, and a page that only toasts its failure is left on its loading
	 * line forever — `/organization` read "Loading…" indefinitely after a failed
	 * `GET /api/organization` (#635).
	 *
	 * Every string arrives already localized, so each caller keeps its own key
	 * namespace; the button defaults to `common.retry`.
	 *
	 * Two shapes:
	 *
	 * - **In a page** (no `heading`): `role="alert"`, because it replaces content
	 *   the reader was waiting for. Retry unmounts this block — the caller clears
	 *   its error flag to show its loading line — so the focused button vanishes
	 *   and focus would fall to `<body>`, putting a keyboard user back at the top
	 *   of the document. Focus moves to the enclosing `<main>` first (the
	 *   layout's skip-link target, already `tabindex="-1"`), so the next Tab
	 *   lands in the page content that replaces this (WCAG 2.4.3). Inside a
	 *   dialog (`ui/Modal`, also `tabindex="-1"`) it is the dialog that takes
	 *   focus instead: `<main>` is outside the focus trap, and focusing it would
	 *   strand a keyboard user behind the open modal.
	 * - **Replacing the whole screen** (`heading` given): a titled region whose
	 *   heading the caller focuses through {@link focusHeading}, which is what a
	 *   screen reader announces — so no `role="alert"` on top of it, which would
	 *   read the same words twice. The caller keeps the block mounted through a
	 *   retry and says so with `retrying`: the button turns `aria-disabled` (not
	 *   `disabled`, which would throw focus to `<body>`) and reads
	 *   `common.retrying`, and keeps the focus the user pressed it with.
	 *
	 * `actions` renders after Retry, for a second way out (the account panel's
	 * Log out).
	 */
	import type { Snippet } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		message,
		onretry,
		heading,
		headingLevel = 2,
		retryLabel,
		retrying = false,
		actions,
		testId
	}: {
		message: string;
		onretry: () => void;
		/** A title for the failure. Omit inside a page; pass when this replaces the screen. */
		heading?: string;
		headingLevel?: 1 | 2 | 3;
		/** Defaults to `common.retry`. */
		retryLabel?: string;
		/** A retry is in flight and this block stays mounted through it. */
		retrying?: boolean;
		/** Further buttons after Retry. */
		actions?: Snippet;
		/** `data-testid` for an e2e selector; the class is Svelte-scoped. */
		testId?: string;
	} = $props();

	let headingEl = $state<HTMLElement | null>(null);
	const headingId = $props.id();

	/** Move focus onto the heading — call it when the failure (re)appears. */
	export function focusHeading() {
		headingEl?.focus();
	}

	function retry(e: MouseEvent) {
		if (retrying) return;
		if (!heading) (e.currentTarget as HTMLElement).closest<HTMLElement>('[role="dialog"], main')?.focus();
		onretry();
	}
</script>

{#snippet body()}
	<p class="message">{message}</p>
	<div class="actions">
		<button type="button" class="btn-primary" onclick={retry} aria-disabled={retrying || undefined}>
			{retrying ? m('common.retrying') : (retryLabel ?? m('common.retry'))}
		</button>
		{@render actions?.()}
	</div>
{/snippet}

{#if heading}
	<section class="load-error titled" aria-labelledby={headingId} data-testid={testId}>
		<svelte:element
			this={`h${headingLevel}`}
			id={headingId}
			tabindex="-1"
			bind:this={headingEl}
		>
			{heading}
		</svelte:element>
		{@render body()}
	</section>
{:else}
	<div class="load-error" role="alert" data-testid={testId}>
		{@render body()}
	</div>
{/if}

<style>
	.load-error {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 12px;
		color: var(--text-muted);
		padding: 24px 0;
	}

	.load-error.titled {
		padding: 0;
	}

	.titled > :global(:is(h1, h2, h3)) {
		margin: 4px 0 0;
		font-size: 1.25rem;
		font-weight: 700;
		color: var(--text);
	}

	.message {
		margin: 0;
	}

	.titled .message {
		font-size: 0.93rem;
		line-height: 1.55;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
	}

	.titled .actions {
		margin-top: 8px;
	}

	.actions button[aria-disabled='true'] {
		cursor: progress;
	}
</style>
