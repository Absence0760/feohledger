<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { api, setToken } from '#lib/api.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { goto } from '$app/navigation';
	import { m } from '#lib/i18n/store.svelte.ts';

	interface SSOCallbackResponse {
		access_token: string;
		token_type: string;
		must_change_password: boolean;
		tenant_slug: string;
	}

	let phase = $state<'working' | 'error'>('working');
	let message = $state(m('auth.callback.signingIn'));

	onMount(async () => {
		const code = page.url.searchParams.get('code');
		const state = page.url.searchParams.get('state');
		const err = page.url.searchParams.get('error');

		if (err) {
			phase = 'error';
			message = m('auth.callback.idpError', { error: err });
			return;
		}
		if (!code || !state) {
			phase = 'error';
			message = m('auth.callback.missingCodeState');
			return;
		}

		try {
			const resp = await api.post<SSOCallbackResponse>('/api/auth/sso/callback', {
				code,
				state,
			});
			setToken(resp.access_token);
			await auth.fetchUser();
			goto(resp.must_change_password ? '/change-password' : '/');
		} catch (e) {
			phase = 'error';
			message = e instanceof Error ? e.message : m('auth.callback.failed');
		}
	});
</script>

<svelte:head>
	<title>{m('auth.callback.pageTitle')}</title>
</svelte:head>

<div class="page">
	<div class="card" aria-live="polite">
		{#if phase === 'working'}
			<div class="spinner" aria-hidden="true"></div>
			<p class="status">{message}</p>
		{:else}
			<h1>{m('auth.callback.failedHeading')}</h1>
			<p class="error" role="alert">{message}</p>
			<a href="/login">{m('auth.callback.backToSignIn')}</a>
		{/if}
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
		padding: 40px 36px;
		width: min(400px, 90vw);
		text-align: center;
	}
	.spinner {
		width: 24px;
		height: 24px;
		border: 3px solid var(--border);
		border-top-color: var(--accent);
		border-radius: 50%;
		margin: 0 auto 16px;
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation-duration: 0.01ms;
			animation-iteration-count: 1;
		}
	}
	.status {
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	h1 {
		margin: 0 0 12px;
		font-size: 1.3rem;
		font-weight: 700;
	}
	.error {
		color: var(--danger);
		font-size: 0.9rem;
		margin: 0 0 16px;
	}
	a {
		color: var(--accent);
		text-decoration: none;
	}
</style>
