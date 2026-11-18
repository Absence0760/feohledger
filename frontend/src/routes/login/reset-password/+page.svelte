<script lang="ts">
	import AuthCard from '#lib/components/auth/AuthCard.svelte';
	import SetPasswordLink from '#lib/components/auth/SetPasswordLink.svelte';
	import { api } from '#lib/api.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { brand } from '#lib/stores/brand.svelte.ts';
	import { signInHref } from '#lib/utils/prefillEmail.ts';

	// Every employee link that redeems a reset token lands here: forgot-password's,
	// and the set-password link sent instead of a temporary password — to a
	// signup admin, an invited user, an admin-reset user and a partner-provisioned
	// child admin (`mode=set` for an invite, decisions §283). The state machine is
	// shared with the supplier portal's page (`SetPasswordLink`).
	let setMode = $state(false);

	// The tab title carries the tenant's product name, not a hardcoded one, so
	// a white-label host never shows the platform's name in the tab.
	let pageTitle = $derived(
		m(setMode ? 'auth.resetPassword.setPageTitle' : 'auth.resetPassword.pageTitle', {
			product: brand.productName
		})
	);
</script>

<svelte:head>
	<title>{pageTitle}</title>
</svelte:head>

<SetPasswordLink
	bind:setMode
	check={(token) => api.post<{ email: string }>('/api/auth/reset-password/check', { token })}
	redeem={(token, new_password) => api.post('/api/auth/reset-password', { token, new_password })}
	{signInHref}
	expiredHint={setMode ? m('auth.resetPassword.setExpiredHint') : undefined}
>
	{#snippet recovery()}
		<a class="link-btn" href="/login/forgot-password">{m('auth.forgotPassword.heading')}</a>
	{/snippet}
	{#snippet chrome({ heading, subtitle, error, body })}
		<AuthCard {heading} {subtitle} {error}>
			{@render body()}
		</AuthCard>
	{/snippet}
</SetPasswordLink>
