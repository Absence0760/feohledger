<script lang="ts">
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import { api } from '$lib/api';
	import { auth } from '$lib/stores/auth.svelte';
	import { goto } from '$app/navigation';
	import { m } from '$lib/i18n/store.svelte';

	interface UserResponse {
		id: string;
		email: string;
		full_name: string;
		must_change_password: boolean;
		roles: string[];
	}

	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let error = $state('');
	let submitting = $state(false);

	let strengthHints = $derived.by(() => {
		const hints: Array<{ label: string; ok: boolean }> = [];
		hints.push({ label: m('auth.changePassword.strength.length'), ok: newPassword.length >= 12 });
		hints.push({ label: m('auth.changePassword.strength.upper'), ok: /[A-Z]/.test(newPassword) });
		hints.push({ label: m('auth.changePassword.strength.lower'), ok: /[a-z]/.test(newPassword) });
		hints.push({ label: m('auth.changePassword.strength.digit'), ok: /[0-9]/.test(newPassword) });
		return hints;
	});

	let allStrong = $derived(strengthHints.every((h) => h.ok));

	async function onSubmit(e: Event) {
		e.preventDefault();
		error = '';
		if (newPassword !== confirmPassword) {
			error = m('auth.changePassword.mismatch');
			return;
		}
		if (!allStrong) {
			error = m('auth.changePassword.tooWeak');
			return;
		}
		submitting = true;
		try {
			await api.post<UserResponse>('/api/auth/change-password', {
				current_password: currentPassword,
				new_password: newPassword,
			});
			await auth.fetchUser();
			goto('/');
		} catch (err) {
			error = err instanceof Error ? err.message : m('auth.changePassword.failed');
		} finally {
			submitting = false;
		}
	}

	async function onLogout() {
		await auth.logout();
	}
</script>

<svelte:head>
	<title>{m('auth.changePassword.pageTitle')}</title>
</svelte:head>

<div class="page">
	<form class="card" onsubmit={onSubmit}>
		<BrandMark size={32} />
		<h1>{m('auth.changePassword.heading')}</h1>
		<p class="sub">
			{#if auth.user?.must_change_password}
				{m('auth.changePassword.subForced')}
			{:else}
				{m('auth.changePassword.subVoluntary')}
			{/if}
		</p>

		<div role="alert" aria-live="assertive">
			{#if error}
				<div class="error">{error}</div>
			{/if}
		</div>

		<label>
			<span>{m('auth.changePassword.currentPassword')}</span>
			<input
				type="password"
				bind:value={currentPassword}
				required
				autocomplete="current-password"
			/>
		</label>

		<label>
			<span>{m('auth.changePassword.newPassword')}</span>
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
			<span>{m('auth.changePassword.confirmPassword')}</span>
			<input
				type="password"
				bind:value={confirmPassword}
				required
				autocomplete="new-password"
				minlength="12"
			/>
		</label>

		<button type="submit" disabled={submitting || !allStrong || newPassword !== confirmPassword}>
			{submitting ? m('auth.changePassword.submitting') : m('auth.changePassword.submit')}
		</button>

		<button type="button" class="secondary" onclick={onLogout}>{m('auth.changePassword.signOut')}</button>
	</form>
</div>

<style>
	.page {
		min-height: 100vh;
		display: grid;
		place-items: center;
		background: var(--bg);
		padding: 40px 20px;
	}
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 40px 36px;
		width: min(440px, 92vw);
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	h1 {
		margin: 0;
		font-size: 1.25rem;
		font-weight: 700;
		color: var(--text);
	}
	.sub {
		margin: -4px 0 4px;
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
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	label span {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	input {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		font-size: 0.9rem;
		color: var(--text);
		font-family: inherit;
	}
	input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}
	.strength {
		margin: -4px 0 4px;
		padding: 0 0 0 4px;
		list-style: none;
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.strength li.ok {
		color: #2e9960;
	}
	button {
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
	button:hover:not(:disabled) {
		opacity: 0.9;
	}
	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	button.secondary {
		background: transparent;
		border: 1px solid var(--border);
		color: var(--text-muted);
	}
</style>
