<script lang="ts">
	import AuthShell from '$lib/components/auth/AuthShell.svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import IconCheck from '~icons/material-symbols/check-circle-outline';
	import { api } from '$lib/api';
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { m } from '$lib/i18n/store.svelte';

	interface CompleteResponse {
		status: string;
		slug: string;
		tenant_url: string;
		admin_email: string;
	}

	let phase = $state<'pending' | 'success' | 'error'>('pending');
	let result = $state<CompleteResponse | null>(null);
	let errorMessage = $state<string>('');

	onMount(async () => {
		const token = $page.url.searchParams.get('token');
		if (!token) {
			phase = 'error';
			errorMessage = m('auth.verify.noToken');
			return;
		}
		try {
			result = await api.post<CompleteResponse>('/api/signup/complete', { token });
			phase = 'success';
		} catch (err) {
			phase = 'error';
			errorMessage = err instanceof Error ? err.message : m('auth.verify.failed');
		}
	});
</script>

<svelte:head>
	<title>{m('auth.verify.pageTitle')}</title>
</svelte:head>

<AuthShell>
	<!-- The live region is the persistent wrapper, not a node that appears with
	     its content: it exists from first paint in the `pending` phase, so the
	     switch to success or error is a CHANGE inside it and is announced. -->
	<div class="auth-stack verify" aria-live="polite">
		<div class="head">
			{#if phase === 'success' && result}
				<div class="done-icon" aria-hidden="true"><IconCheck /></div>
			{:else}
				<BrandMark size={40} />
			{/if}
			{#if phase === 'pending'}
				<h1>{m('auth.verify.pendingHeading')}</h1>
				<p class="sub">{m('auth.verify.pendingSub')}</p>
			{:else if phase === 'success' && result}
				<h1>{m('auth.verify.successHeading')}</h1>
				<p class="sub">
					{m('auth.verify.successSubPre')}<strong>{result.admin_email}</strong>{m('auth.verify.successSubPost')}
				</p>
			{:else}
				<h1>{m('auth.verify.errorHeading')}</h1>
			{/if}
		</div>

		{#if phase === 'pending'}
			<div class="spinner" aria-hidden="true"></div>
		{:else if phase === 'success' && result}
			<ol class="steps">
				<li>{m('auth.verify.step1')}</li>
				<li>{m('auth.verify.step2')}</li>
				<li>{m('auth.verify.step3')}</li>
			</ol>
			<a class="primary" href={result.tenant_url}>{m('auth.verify.continueTo', { slug: result.slug })}</a>
		{:else}
			<p class="error">{errorMessage}</p>
			<a class="start-over" href="/signup">{m('auth.verify.startOver')}</a>
		{/if}
	</div>
</AuthShell>

<style>
	/* Heading, error banner and entrance come from AuthShell. */
	.verify {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	.head {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 10px;
	}
	.sub {
		margin: 0;
		font-size: 0.93rem;
		line-height: 1.55;
		color: var(--text-muted);
	}
	.sub strong {
		color: var(--text);
	}
	.done-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 56px;
		height: 56px;
		border-radius: 16px;
		border: 1px solid rgba(38, 185, 119, 0.4);
		background: var(--success-tint);
		color: var(--success-on-tint);
		font-size: 30px;
		animation: pop 0.6s cubic-bezier(0.34, 1.56, 0.64, 1) backwards;
	}
	@keyframes pop {
		from { transform: scale(0.6); opacity: 0; }
		to { transform: scale(1); opacity: 1; }
	}
	.steps {
		margin: 0;
		padding: 14px 16px 14px 34px;
		border-radius: 12px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.88rem;
		line-height: 1.7;
	}
	.primary {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-height: 48px;
		padding: 12px 18px;
		border-radius: 10px;
		background: var(--accent-strong);
		color: #fff;
		font-weight: 600;
		text-decoration: none;
		box-shadow: 0 14px 30px -14px rgba(99, 140, 255, 0.95);
		transition: transform 0.15s, box-shadow 0.15s;
	}
	.primary:hover {
		transform: translateY(-1px);
	}
	.start-over {
		align-self: flex-start;
		color: var(--accent-on-tint);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.spinner {
		width: 26px;
		height: 26px;
		border: 3px solid var(--border);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin {
		to { transform: rotate(360deg); }
	}
</style>
