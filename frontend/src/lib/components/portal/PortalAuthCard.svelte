<script lang="ts">
	import type { Snippet } from 'svelte';
	import BrandMark from '#lib/components/ui/BrandMark.svelte';

	/**
	 * The supplier portal's frame for its signed-out account pages —
	 * `/portal/reset-password` (where a set-password link lands) and
	 * `/portal/forgot-password` (where a supplier asks for one). The portal's
	 * own card rather than the employee `AuthCard`, so a white-label tenant's
	 * supplier never sees platform branding (`docs/white-label.md`).
	 *
	 * The body is the page's markup (or `SetPasswordLink`'s), so its form,
	 * fields and buttons are styled through `:global()`, wrapped in `:where()`
	 * so the field components' own rules still win on specificity.
	 */
	interface Props {
		heading: string;
		subtitle?: string;
		error?: string;
		children: Snippet;
	}

	let { heading, subtitle, error = '', children }: Props = $props();
</script>

<div class="page">
	<div class="card">
		<BrandMark size={36} />
		<h1>{heading}</h1>
		{#if subtitle}<p class="hint">{subtitle}</p>{/if}
		<div role="alert" aria-live="assertive">
			{#if error}<div class="error">{error}</div>{/if}
		</div>
		{@render children()}
	</div>
</div>

<style>
	.page {
		min-height: 100vh;
		display: grid;
		place-items: center;
		background: var(--bg);
	}
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 36px;
		width: min(420px, 92vw);
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	h1 {
		margin: 0;
		font-size: 1.2rem;
	}
	.hint {
		margin: -6px 0 4px;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.error {
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
		color: var(--danger);
		padding: 10px;
		border-radius: 4px;
		font-size: 0.85rem;
	}
	/* The body is the page's markup, reached through `:global()` (see the
	   doc comment above; the pattern `portal/change-password` uses). */
	.card :global(:where(form)) {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	.card :global(:where(.password-field label > span)),
	.card :global(:where(form > label > span)) {
		font-size: 0.78rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	.card :global(:where(form > label)) {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.card :global(:where(.password-field input:not([type='checkbox']):not([type='radio']))),
	.card :global(:where(form > label > input)) {
		width: 100%;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		font-size: 0.9rem;
		color: var(--text);
		font-family: inherit;
	}
	.card :global(:where(.password-field input:not([type='checkbox']):not([type='radio']):focus)),
	.card :global(:where(form > label > input:focus)) {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
	}
	.card :global(:where(.password-field input[aria-invalid='true'])) {
		border-color: var(--danger);
	}
	/* The submit, and Retry (a direct child of the card); never the
	   show-password toggles inside the fields. */
	.card :global(:where(form > button[type='submit'])),
	.card > :global(button) {
		margin-top: 8px;
		padding: 10px;
		border-radius: 4px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-weight: 500;
		cursor: pointer;
	}
	.card :global(:where(form > button[type='submit']:disabled)) {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.card :global(:where(.success)) {
		margin: 0;
		color: var(--text);
		line-height: 1.55;
	}
	.card :global(:where(.link-btn)) {
		color: var(--accent-strong);
		font-size: 0.9rem;
		text-decoration: underline;
	}
</style>
