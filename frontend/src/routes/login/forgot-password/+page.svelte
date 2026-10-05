<script lang="ts">
	import { tick } from 'svelte';
	import AuthCard from '#lib/components/auth/AuthCard.svelte';
	import { api } from '#lib/api.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let email = $state('');
	let error = $state('');
	let loading = $state(false);
	// The backend always answers with the same generic success shape whether
	// or not `email` matched an account (enumeration resistance —
	// docs/authentication.md § brute-force protection) — so this flips to
	// `true` on any 2xx, never conditioned on what the account lookup found.
	let submitted = $state(false);
	let successEl = $state<HTMLParagraphElement | null>(null);

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		loading = true;
		try {
			await api.post('/api/auth/forgot-password', { email });
			submitted = true;
			// The submit button had focus, and it has just been removed from the
			// DOM along with the form — which drops focus to <body> and leaves a
			// screen reader with nothing to say. Land it on the confirmation
			// instead, the way /signup does: that is announced, and it puts
			// keyboard users at the top of the new content (WCAG 2.4.3, 4.1.3).
			await tick();
			successEl?.focus();
		} catch (err) {
			// A non-2xx here is a real client-side problem (rate limited,
			// malformed request) — never "email not found", which the backend
			// deliberately never reports.
			error = err instanceof Error ? err.message : m('auth.login.failed');
		} finally {
			loading = false;
		}
	}
</script>

<svelte:head>
	<title>{m('auth.forgotPassword.heading')} — FeohLedger</title>
</svelte:head>

<AuthCard
	heading={m('auth.forgotPassword.heading')}
	subtitle={submitted ? undefined : m('auth.forgotPassword.subtitle')}
	{error}
>
	{#if submitted}
		<p class="success" tabindex="-1" bind:this={successEl}>{m('auth.forgotPassword.success')}</p>
		<a class="link-btn" href="/login">{m('auth.forgotPassword.backToLogin')}</a>
	{:else}
		<form onsubmit={handleSubmit}>
			<label>
				<span>{m('auth.forgotPassword.email')}</span>
				<input type="email" bind:value={email} required autocomplete="email" />
			</label>
			<button type="submit" disabled={loading}>
				{loading ? m('auth.forgotPassword.sending') : m('auth.forgotPassword.submit')}
			</button>
		</form>
		<a class="link-btn" href="/login">{m('auth.forgotPassword.backToLogin')}</a>
	{/if}
</AuthCard>
