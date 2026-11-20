<script lang="ts">
	/**
	 * Why a control is refused, in the server's words — the one rendering of a
	 * verdict's reason beside a disabled action (frontend/docs/ui-patterns.md
	 * § Whether to offer an action). It sits on its own line ABOVE the
	 * buttons and wraps, so a long reason in any locale never pushes a row's
	 * other actions off a fixed-width actions column.
	 *
	 * The disabled control must name this note in `aria-describedby`, so a
	 * screen reader hears the reason, not just "dimmed". `RowAction`'s
	 * `reason` prop renders one of these and wires that for you; use this
	 * directly only beside a control that is not a `RowAction` (a header or
	 * dialog button), passing the same `id` to the button.
	 */
	type Props = {
		/** The localized reason — `refusalText(verdict, m)`. */
		reason: string;
		/** Element id; the refused control's `aria-describedby` points here. */
		id: string;
		testid?: string;
	};

	let { reason, id, testid }: Props = $props();
</script>

<span class="refusal-note" {id} data-testid={testid ?? null}>{reason}</span>

<style>
	.refusal-note {
		display: block;
		/* In a flex row of header / dialog buttons it is one item; cap it so it
		   wraps rather than squeezing the buttons. */
		max-width: 40ch;
		margin-bottom: 4px;
		font-size: 0.74rem;
		line-height: 1.35;
		color: var(--text-muted);
		white-space: normal;
		overflow-wrap: anywhere;
		text-align: start;
	}
	/* The control under the note starts its own line, so it drops the global
	   `td.actions > * + *` gap that spaces inline siblings. */
	:global(td.actions) > .refusal-note + :global(*) {
		margin-left: 0;
	}
</style>
