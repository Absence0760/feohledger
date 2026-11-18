<script lang="ts">
	import SetPasswordLink from '#lib/components/auth/SetPasswordLink.svelte';
	import PortalAuthCard from '#lib/components/portal/PortalAuthCard.svelte';
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
		<a class="link-btn" href="/portal/forgot-password">{m('auth.forgotPassword.heading')}</a>
		<a class="link-btn" href="/portal/login">{m('portal.resetPassword.backToSignIn')}</a>
	{/snippet}
	{#snippet chrome({ heading, subtitle, error, body })}
		<PortalAuthCard {heading} {subtitle} {error}>
			{@render body()}
		</PortalAuthCard>
	{/snippet}
</SetPasswordLink>
