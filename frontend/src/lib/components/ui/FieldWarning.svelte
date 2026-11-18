<script lang="ts">
	/**
	 * An inline, advisory warning attached to a form field — the thing that
	 * says "this is legal, and here is what it will cost you" while the user
	 * is still typing.
	 *
	 * Distinct from a toast (transient, fires on submit) and from a
	 * `role="alert"` refusal panel (assertive, interrupts, used for a request
	 * the server rejected). This is `role="status"` / `aria-live="polite"`:
	 * it appears and updates as the field changes, so an assertive live
	 * region would interrupt the user on every keystroke.
	 *
	 * Render nothing by passing `show={false}` — the element leaves the DOM,
	 * which is what makes the polite region announce on its next appearance.
	 */
	let {
		show = false,
		busy = false,
		message,
		testId
	}: {
		show?: boolean;
		/** The message is stale and a fresh one is on its way: `aria-busy`
		 *  holds the announcement until the caller settles it. */
		busy?: boolean;
		/** Already-localized text — this component is i18n-agnostic. */
		message: string;
		/** `data-testid` for an e2e selector; the class is Svelte-scoped. */
		testId?: string;
	} = $props();
</script>

{#if show}
	<p class="field-warning" role="status" aria-busy={busy || undefined} data-testid={testId}>
		<span aria-hidden="true">⚠</span>
		{message}
	</p>
{/if}

<style>
	.field-warning {
		display: flex;
		align-items: flex-start;
		gap: 6px;
		margin: 6px 0 0;
		font-size: 0.8rem;
		line-height: 1.4;
		/* A long localized amount must wrap inside a narrow field, never push
		   the warning past its container. */
		overflow-wrap: anywhere;
		/* --danger is the text-on-dark token; --danger-strong is the fill
		   behind white text and would be far too dark to read here. */
		color: var(--danger);
	}
</style>
