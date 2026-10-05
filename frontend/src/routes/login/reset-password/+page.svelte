<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { page } from '$app/stores';
	import AuthCard from '$lib/components/auth/AuthCard.svelte';
	import { api } from '$lib/api';
	import { m } from '$lib/i18n/store.svelte';

	let token = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let error = $state('');
	let submitting = $state(false);
	let done = $state(false);
	let successEl = $state<HTMLParagraphElement | null>(null);

	onMount(() => {
		token = $page.url.searchParams.get('token') ?? '';
		if (!token) {
			error = m('auth.resetPassword.missingToken');
		}
	});

	// Same complexity checklist `routes/change-password/+page.svelte` shows —
	// reused message keys so the two forms don't drift on wording.
	let strengthHints = $derived.by(() => {
		const hints: Array<{ label: string; ok: boolean }> = [];
		hints.push({ label: m('auth.changePassword.strength.length'), ok: newPassword.length >= 12 });
		hints.push({ label: m('auth.changePassword.strength.upper'), ok: /[A-Z]/.test(newPassword) });
		hints.push({ label: m('auth.changePassword.strength.lower'), ok: /[a-z]/.test(newPassword) });
		hints.push({ label: m('auth.changePassword.strength.digit'), ok: /[0-9]/.test(newPassword) });
		return hints;
	});

	let allStrong = $derived(strengthHints.every((h) => h.ok));

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		if (newPassword !== confirmPassword) {
			error = m('auth.resetPassword.mismatch');
			return;
		}
		if (!allStrong) {
			return;
		}
		submitting = true;
		try {
			await api.post('/api/auth/reset-password', { token, new_password: newPassword });
			done = true;
			// The submit button had focus, and it has just been removed from the
			// DOM along with the form — which drops focus to <body> and leaves a
			// screen reader with nothing to say. Land it on the confirmation
			// instead, the way /signup does: that is announced, and it puts
			// keyboard users at the top of the new content (WCAG 2.4.3, 4.1.3).
			await tick();
			successEl?.focus();
		} catch (err) {
			// A reused/expired/bogus token surfaces as the backend's own opaque
			// "Invalid or expired reset link." (they're indistinguishable by
			// design — services/password_reset.py); anything else (weak
			// password, rate limit) rides the backend's own detail text.
			error = err instanceof Error ? err.message : m('auth.resetPassword.invalidToken');
		} finally {
			submitting = false;
		}
	}
</script>

<svelte:head>
	<title>{m('auth.resetPassword.heading')} — FeohLedger</title>
</svelte:head>

<AuthCard
	heading={m('auth.resetPassword.heading')}
	subtitle={done || !token ? undefined : m('auth.resetPassword.subtitle')}
	{error}
>
	{#if done}
		<p class="success" tabindex="-1" bind:this={successEl}>{m('auth.resetPassword.success')}</p>
		<a class="link-btn" href="/login">{m('auth.resetPassword.goToLogin')}</a>
	{:else if token}
		<form onsubmit={handleSubmit}>
			<label>
				<span>{m('auth.resetPassword.newPassword')}</span>
				<input
					type="password"
					bind:value={newPassword}
					required
					autocomplete="new-password"
					minlength="12"
				/>
			</label>

			<ul class="strength">
				{#each strengthHints as hint}
					<li class:ok={hint.ok}>{hint.ok ? '✓' : '·'} {hint.label}</li>
				{/each}
			</ul>

			<label>
				<span>{m('auth.resetPassword.confirmPassword')}</span>
				<input
					type="password"
					bind:value={confirmPassword}
					required
					autocomplete="new-password"
					minlength="12"
				/>
			</label>

			<button type="submit" disabled={submitting || !allStrong || newPassword !== confirmPassword}>
				{submitting ? m('auth.resetPassword.submitting') : m('auth.resetPassword.submit')}
			</button>
		</form>
	{:else}
		<a class="link-btn" href="/login/forgot-password">{m('auth.forgotPassword.heading')}</a>
	{/if}
</AuthCard>

<style>
	.strength {
		margin: -8px 0 0;
		padding: 0 0 0 4px;
		list-style: none;
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.strength li.ok {
		color: var(--success);
	}
</style>
