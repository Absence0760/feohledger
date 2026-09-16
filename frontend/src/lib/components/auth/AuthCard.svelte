<script lang="ts">
	import type { Snippet } from 'svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';

	/**
	 * Shared unauthenticated-page chrome for the auth flows that live outside
	 * the app shell (forgot-password, reset-password — and login itself could
	 * migrate to this later without a visual change; it isn't touched here to
	 * avoid a cosmetic-only refactor of an already-shipped, heavily-used page).
	 *
	 * Renders the centered card + heading/subtitle/error and exposes the
	 * form-control CSS (label/input/button/link-button/success/hint) to
	 * whatever the caller puts in the default slot, via `:global()` — so a new
	 * auth page gets the same look without copy-pasting the ~80 lines of card
	 * chrome `routes/login/+page.svelte` originally defined for itself.
	 */
	interface Props {
		heading: string;
		subtitle?: string;
		error?: string;
		children: Snippet;
	}

	let { heading, subtitle, error, children }: Props = $props();
</script>

<div class="auth-page">
	<div class="auth-card">
		<BrandMark size={32} />
		<h1>{heading}</h1>
		{#if subtitle}
			<p class="subtitle">{subtitle}</p>
		{/if}

		<div role="alert" aria-live="assertive">
			{#if error}
				<div class="error">{error}</div>
			{/if}
		</div>

		{@render children()}
	</div>
</div>

<style>
	.auth-page {
		min-height: 100vh;
		display: grid;
		place-items: center;
		background: var(--bg);
	}

	.auth-card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 40px 36px;
		width: min(400px, 90vw);
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	h1 {
		margin: 0;
		font-size: 1.3rem;
		font-weight: 700;
		color: var(--text);
	}

	.subtitle {
		margin: -8px 0 8px;
		font-size: 0.88rem;
		color: var(--text-muted);
	}

	.error {
		background: rgba(224, 64, 64, 0.1);
		border: 1px solid rgba(224, 64, 64, 0.3);
		color: var(--danger);
		padding: 10px 14px;
		border-radius: 4px;
		font-size: 0.85rem;
	}

	/* Form-control chrome for whatever the caller renders via the slot —
	   shared so each auth page's own <script>/markup stays free of styling. */
	.auth-card :global(form) {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.auth-card :global(label) {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.auth-card :global(label span) {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.auth-card :global(input) {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		font-size: 0.9rem;
		color: var(--text);
		font-family: inherit;
	}

	.auth-card :global(input:focus) {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.auth-card :global(button[type='submit']) {
		margin-top: 8px;
		padding: 10px;
		border-radius: 4px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.9rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.auth-card :global(button:hover:not(:disabled)) {
		opacity: 0.9;
	}

	.auth-card :global(button:disabled) {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.auth-card :global(.link-btn) {
		background: transparent;
		border: none;
		color: var(--accent);
		font-size: 0.85rem;
		cursor: pointer;
		padding: 0;
		text-align: center;
		font-family: inherit;
		text-decoration: underline;
	}

	.auth-card :global(.success) {
		background: rgba(31, 168, 106, 0.1);
		border: 1px solid rgba(31, 168, 106, 0.3);
		color: var(--success);
		padding: 10px 14px;
		border-radius: 4px;
		font-size: 0.85rem;
	}

	.auth-card :global(.hint) {
		font-size: 0.82rem;
		color: var(--text-muted);
		text-align: center;
		margin: 4px 0 0;
	}
</style>
