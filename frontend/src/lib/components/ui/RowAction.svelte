<script lang="ts">
	import type { Snippet } from 'svelte';
	import RefusalNote from './RefusalNote.svelte';

	type Variant = 'default' | 'accent' | 'success' | 'danger';

	type Props = {
		/** Color treatment. `default` = neutral border, accent on hover.
		 *  `accent` / `success` / `danger` = colored border + matching text. */
		variant?: Variant;
		/** When true, applies a red "armed" tint — used for two-step destructive
		 *  confirmation (one click arms, second click commits). */
		armed?: boolean;
		/** When true, renders a tighter padded square — for icon-only buttons. */
		icon?: boolean;
		disabled?: boolean;
		/** The action's own request is in flight. The button refuses further
		 *  clicks and announces `aria-busy`, so an armed confirm cannot send its
		 *  request twice. Spelled `aria-disabled` rather than `disabled` so a
		 *  keyboard user who just pressed it keeps focus (a natively disabled
		 *  button drops focus to `<body>`). Ignored for the `href` form. */
		busy?: boolean;
		title?: string;
		ariaLabel?: string;
		/** Id of an element describing the action — e.g. why a disabled action
		 *  is disabled, so a screen reader hears the reason, not just "dimmed". */
		describedby?: string;
		/** Why the action is refused, when the reader can act on it (a
		 *  verdict's `refusalText`). Non-empty renders a `RefusalNote` on its
		 *  own line above the button and adds it to `aria-describedby`; pair it
		 *  with `disabled` (frontend/docs/ui-patterns.md § Whether to offer an
		 *  action). Ignored for the `href` form. */
		reason?: string;
		/** Id for the reason note (defaults to a generated one) — set it when a
		 *  test or another control needs to find the note. */
		reasonId?: string;
		/** When set, renders as an `<a>` for client-side navigation. */
		href?: string;
		/** Click handler. */
		onclick?: (e: MouseEvent) => void;
		/** Button type for non-link form contexts. */
		type?: 'button' | 'submit';
		children: Snippet;
	};

	let {
		variant = 'default',
		armed = false,
		icon = false,
		disabled = false,
		busy = false,
		title,
		ariaLabel,
		describedby,
		reason,
		reasonId,
		href,
		onclick,
		type = 'button',
		children,
	}: Props = $props();

	const uid = $props.id();
	const noteId = $derived(reasonId ?? `${uid}-reason`);
	const describedbyIds = $derived(
		[reason ? noteId : null, describedby ?? null].filter(Boolean).join(' ') || null
	);

	function handleClick(e: MouseEvent) {
		if (busy) {
			// Swallow it whole: a row action's click usually also opens or
			// un-arms something further up the tree.
			e.preventDefault();
			e.stopPropagation();
			return;
		}
		onclick?.(e);
	}
</script>

{#if href}
	<a
		class="row-action"
		class:variant-accent={variant === 'accent'}
		class:variant-success={variant === 'success'}
		class:variant-danger={variant === 'danger'}
		class:armed
		class:icon
		{href}
		title={title ?? null}
		aria-label={ariaLabel ?? null}
	>
		{@render children()}
	</a>
{:else}
	{#if reason}<RefusalNote id={noteId} {reason} />{/if}
	<button
		class="row-action"
		class:variant-accent={variant === 'accent'}
		class:variant-success={variant === 'success'}
		class:variant-danger={variant === 'danger'}
		class:armed
		class:icon
		class:busy
		{type}
		{disabled}
		onclick={handleClick}
		title={title ?? null}
		aria-label={ariaLabel ?? null}
		aria-describedby={describedbyIds}
		aria-disabled={busy || null}
		aria-busy={busy || null}
	>
		{@render children()}
	</button>
{/if}

<style>
	.row-action {
		padding: 4px 12px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
		text-decoration: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 4px;
		line-height: 1.2;
		white-space: nowrap;
		transition:
			border-color 0.15s,
			color 0.15s,
			background 0.15s;
	}

	.row-action.icon {
		padding: 0;
		width: 30px;
		height: 28px;
	}

	.row-action:hover:not(:disabled):not(.busy) {
		border-color: var(--accent);
		color: var(--accent);
	}

	/* --- accent: persistent accent border, accent-tinted hover */
	.row-action.variant-accent {
		border-color: var(--accent);
		color: var(--accent);
	}

	.row-action.variant-accent:hover:not(:disabled) {
		background: color-mix(in srgb, var(--accent) 8%, transparent);
	}

	/* --- success: green border + text */
	.row-action.variant-success {
		border-color: var(--success);
		color: var(--success);
	}

	.row-action.variant-success:hover:not(:disabled) {
		background: color-mix(in srgb, var(--success) 10%, transparent);
	}

	/* --- danger: neutral by default, red on hover; armed = red filled */
	.row-action.variant-danger:hover:not(:disabled) {
		border-color: var(--danger);
		color: var(--danger);
	}

	.row-action.variant-danger.armed {
		border-color: var(--danger);
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		color: var(--danger);
	}

	.row-action:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.row-action.busy {
		opacity: 0.6;
		cursor: progress;
	}
</style>
