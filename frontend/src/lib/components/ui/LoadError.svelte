<script lang="ts">
	/**
	 * A failed load, stated in place, with a way back: the message and a Retry
	 * button. Render it where the content would have been, in the `{:else if}`
	 * after the success branch and before the loading line.
	 *
	 * A toast is not a substitute. It fades, and a page that only toasts its
	 * failure is left on its loading line forever — `/organization` read
	 * "Loading…" indefinitely after a failed `GET /api/organization` (#635).
	 *
	 * `message` arrives already localized, so each caller keeps its own key
	 * namespace; the button is `common.retry`. `role="alert"` because it
	 * replaces content the reader was waiting for.
	 *
	 * Retry unmounts this block — the caller clears its error flag to show its
	 * loading line — so the focused button vanishes and focus would fall to
	 * `<body>`, putting a keyboard user back at the top of the document. Focus
	 * moves to the enclosing `<main>` first (the layout's skip-link target,
	 * already `tabindex="-1"`), so the next Tab lands in the page content that
	 * replaces this (WCAG 2.4.3).
	 */
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		message,
		onretry,
		testId
	}: {
		message: string;
		onretry: () => void;
		/** `data-testid` for an e2e selector; the class is Svelte-scoped. */
		testId?: string;
	} = $props();

	function retry(e: MouseEvent) {
		(e.currentTarget as HTMLElement).closest('main')?.focus();
		onretry();
	}
</script>

<div class="load-error" role="alert" data-testid={testId}>
	<p>{message}</p>
	<button type="button" class="btn-primary" onclick={retry}>{m('common.retry')}</button>
</div>

<style>
	.load-error {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 12px;
		color: var(--text-muted);
		padding: 24px 0;
	}

	.load-error p {
		margin: 0;
	}
</style>
