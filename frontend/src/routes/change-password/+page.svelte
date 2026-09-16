<script lang="ts">
	import AuthShell from '$lib/components/auth/AuthShell.svelte';
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

<AuthShell>
	<form class="change-form" onsubmit={onSubmit}>
		<div class="head">
			<BrandMark size={40} />
			<h1>{m('auth.changePassword.heading')}</h1>
			<p class="sub">
				{#if auth.user?.must_change_password}
					{m('auth.changePassword.subForced')}
				{:else}
					{m('auth.changePassword.subVoluntary')}
				{/if}
			</p>
		</div>

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
</AuthShell>

<style>
	/* Field chrome, the submit button, the error banner and the entrance come
	   from AuthShell; this is only the strength checklist and the sign-out
	   escape hatch. */
	.change-form {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	.head {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 10px;
		margin-bottom: 4px;
	}
	.sub {
		margin: 0;
		font-size: 0.93rem;
		line-height: 1.55;
		color: var(--text-muted);
	}
	.strength {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 6px 12px;
		margin: -6px 0 0;
		padding: 0;
		list-style: none;
		font-size: 0.8rem;
		color: var(--text-muted);
	}
	.strength li {
		transition: color 0.2s;
	}
	/* --success, not the old literal #2e9960: a hint turning green is the one
	   state change on this page, and the token is what is calibrated against
	   --bg (6.17:1). */
	.strength li.ok {
		color: var(--success);
	}
	@media (max-width: 420px) {
		.strength {
			grid-template-columns: 1fr;
		}
	}
	.secondary {
		min-height: 46px;
		padding: 11px 16px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--text-muted);
		font-family: inherit;
		font-size: 0.93rem;
		cursor: pointer;
		transition: border-color 0.15s, color 0.15s;
	}
	.secondary:hover {
		border-color: var(--text-muted);
		color: var(--text);
	}
</style>
