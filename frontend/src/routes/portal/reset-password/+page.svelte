<script lang="ts">
	import SetPasswordLink from '#lib/components/auth/SetPasswordLink.svelte';
	import BrandMark from '#lib/components/ui/BrandMark.svelte';
	import { portalApi } from '#lib/portalApi.ts';
	import { portalBrand } from '#lib/stores/portalBrand.svelte.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	// Where the AP team's supplier-portal invite and reset links land
	// (`api/vendors.py` → `POST /api/portal/auth/reset-password`, decisions
	// §283). Public — the token is the credential — and tenant-bound: the
	// backend refuses a link minted for another tenant. The state machine is the
	// employee reset page's (`SetPasswordLink`); the frame is the portal's own,
	// so a white-label tenant's supplier never sees platform branding.
	let setMode = $state(false);

	const signInHref = (email: string) =>
		email ? `/portal/login?email=${encodeURIComponent(email)}` : '/portal/login';
</script>

<svelte:head>
	<title>
		{m(setMode ? 'auth.resetPassword.setPageTitle' : 'auth.resetPassword.pageTitle', {
			product: portalBrand.productName
		})}
	</title>
</svelte:head>

<SetPasswordLink
	bind:setMode
	idPrefix="portal-reset"
	check={(token) =>
		portalApi.post<{ email: string }>('/api/portal/auth/reset-password/check', { token })}
	redeem={(token, new_password) =>
		portalApi.post('/api/portal/auth/reset-password', { token, new_password })}
	{signInHref}
	expiredHint={m('portal.resetPassword.expiredHint')}
>
	{#snippet recovery()}
		<a class="link-btn" href="/portal/login">{m('portal.resetPassword.backToSignIn')}</a>
	{/snippet}
	{#snippet chrome({ heading, subtitle, error, body })}
		<div class="page">
			<div class="card">
				<BrandMark size={36} />
				<h1>{heading}</h1>
				{#if subtitle}<p class="hint">{subtitle}</p>{/if}
				<div role="alert" aria-live="assertive">
					{#if error}<div class="error">{error}</div>{/if}
				</div>
				{@render body()}
			</div>
		</div>
	{/snippet}
</SetPasswordLink>

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
	/* The body is SetPasswordLink's markup, so its elements are reached through
	   `:global()`, wrapped in `:where()` so the field components' own rules
	   still win on specificity (the pattern `portal/change-password` uses). */
	.card :global(:where(form)) {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	.card :global(:where(.password-field label > span)) {
		font-size: 0.78rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	.card :global(:where(.password-field input:not([type='checkbox']):not([type='radio']))) {
		width: 100%;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		font-size: 0.9rem;
		color: var(--text);
		font-family: inherit;
	}
	.card :global(:where(.password-field input:not([type='checkbox']):not([type='radio']):focus)) {
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
