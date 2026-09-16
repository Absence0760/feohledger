<script lang="ts">
	import type { Snippet } from 'svelte';
	import AuthShell from '$lib/components/auth/AuthShell.svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';

	/**
	 * Heading + subtitle + error chrome for the secondary auth pages
	 * (forgot-password, reset-password), rendered inside `AuthShell`.
	 *
	 * The field, button and error styling used to live here as ~80 lines of
	 * `:global()` rules — a second copy of what `routes/login` defined for
	 * itself. Both now come from `AuthShell`, which sign-in and signup render
	 * inside too, so the page one click from "Forgot password?" cannot drift
	 * back to an older look. What stays here is only what these two pages have
	 * that sign-in does not: the text-style `.link-btn` back-link, the `.success`
	 * confirmation and the `.hint` line.
	 *
	 * `.auth-card` is kept as the wrapper's class: it is the stable hook
	 * `tests-e2e/organization/branding-mark.spec.ts` scopes the mark check to,
	 * and it still names what this component is.
	 */
	interface Props {
		heading: string;
		subtitle?: string;
		error?: string;
		children: Snippet;
	}

	let { heading, subtitle, error, children }: Props = $props();
</script>

<AuthShell>
	<div class="auth-card auth-stack">
		<div class="head">
			<BrandMark size={40} />
			<h1>{heading}</h1>
			{#if subtitle}
				<p class="subtitle">{subtitle}</p>
			{/if}
		</div>

		<div role="alert" aria-live="assertive">
			{#if error}
				<div class="error">{error}</div>
			{/if}
		</div>

		{@render children()}
	</div>
</AuthShell>

<style>
	.auth-card {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	.head {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 10px;
		margin-bottom: 6px;
	}
	.subtitle {
		margin: 0;
		font-size: 0.93rem;
		line-height: 1.55;
		color: var(--text-muted);
	}

	.auth-card :global(form) {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}

	.auth-card :global(.link-btn) {
		align-self: center;
		padding: 0;
		border: none;
		background: transparent;
		color: var(--accent-on-tint);
		font-family: inherit;
		font-size: 0.86rem;
		text-align: center;
		text-decoration: underline;
		text-underline-offset: 2px;
		cursor: pointer;
	}
	.auth-card :global(.link-btn:hover) {
		color: var(--text);
	}

	.auth-card :global(.success) {
		margin: 0;
		padding: 12px 14px;
		border-radius: 10px;
		border: 1px solid rgba(38, 185, 119, 0.35);
		background: var(--success-tint);
		color: var(--success-on-tint);
		font-size: 0.88rem;
		line-height: 1.5;
	}

	.auth-card :global(.hint) {
		margin: 4px 0 0;
		font-size: 0.82rem;
		color: var(--text-muted);
		text-align: center;
	}
</style>
