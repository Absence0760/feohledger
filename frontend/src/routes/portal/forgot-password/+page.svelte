<script lang="ts">
	import { tick } from 'svelte';
	import PortalAuthCard from '#lib/components/portal/PortalAuthCard.svelte';
	import { portalApi } from '#lib/portalApi.ts';
	import { portalBrand } from '#lib/stores/portalBrand.svelte.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	// A supplier asks for their own reset link (`POST
	// /api/portal/auth/forgot-password`, tenant-scoped by the header
	// `portalApi` attaches). The link it mails lands on `/portal/reset-password`.
	// The backend answers the same for every address, so the confirmation shows
	// on any 2xx, never conditioned on whether the account exists. The copy is
	// the employee page's (`auth.forgotPassword.*`, main catalogue).
	let email = $state('');
	let error = $state('');
	let loading = $state(false);
	let submitted = $state(false);
	let successEl = $state<HTMLParagraphElement | null>(null);

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		loading = true;
		try {
			await portalApi.post('/api/portal/auth/forgot-password', { email });
			submitted = true;
			// The submit button goes with the form; land focus on the
			// confirmation so it is announced (WCAG 2.4.3, 4.1.3).
			await tick();
			successEl?.focus();
		} catch (err) {
			// Never "address not found" — a non-2xx is a rate limit or a
			// malformed request.
			error = err instanceof Error ? err.message : m('portal.login.failed');
		} finally {
			loading = false;
		}
	}
</script>

<svelte:head>
	<title>{m('auth.forgotPassword.pageTitle', { product: portalBrand.productName })}</title>
</svelte:head>

<PortalAuthCard
	heading={m('auth.forgotPassword.heading')}
	subtitle={submitted ? undefined : m('auth.forgotPassword.subtitle')}
	{error}
>
	{#if submitted}
		<p class="success" tabindex="-1" bind:this={successEl}>{m('auth.forgotPassword.success')}</p>
	{:else}
		<form onsubmit={handleSubmit}>
			<label>
				<span>{m('auth.forgotPassword.email')}</span>
				<input type="email" bind:value={email} required autocomplete="username" />
			</label>
			<button type="submit" disabled={loading}>
				{loading ? m('auth.forgotPassword.sending') : m('auth.forgotPassword.submit')}
			</button>
		</form>
	{/if}
	<a class="link-btn" href="/portal/login">{m('portal.resetPassword.backToSignIn')}</a>
</PortalAuthCard>
