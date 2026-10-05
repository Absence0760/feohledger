<script lang="ts">
	import { portalAuth } from '#lib/stores/portalAuth.svelte.ts';
	import { goto } from '$app/navigation';
	import { m } from '#lib/i18n/store.svelte.ts';

	let currentPassword = $state('');
	let newPassword = $state('');
	let confirm = $state('');
	let error = $state('');
	let loading = $state(false);

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		if (newPassword !== confirm) {
			error = m('portal.changePassword.mismatch');
			return;
		}
		loading = true;
		try {
			await portalAuth.changePassword(currentPassword, newPassword);
			goto('/portal/invoices');
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.changePassword.failed');
		} finally {
			loading = false;
		}
	}
</script>

<div class="page">
	<form class="card" onsubmit={handleSubmit}>
		<h1>{m('portal.changePassword.title')}</h1>
		<p class="hint">{m('portal.changePassword.hint')}</p>

		<div role="alert" aria-live="assertive">
			{#if error}<div class="error">{error}</div>{/if}
		</div>

		<label>
			<span>{m('portal.changePassword.current')}</span>
			<input type="password" bind:value={currentPassword} required />
		</label>
		<label>
			<span>{m('portal.changePassword.new')}</span>
			<input type="password" bind:value={newPassword} required minlength="12" />
		</label>
		<label>
			<span>{m('portal.changePassword.confirm')}</span>
			<input type="password" bind:value={confirm} required />
		</label>
		<button type="submit" disabled={loading}>
			{loading ? m('portal.changePassword.saving') : m('portal.changePassword.save')}
		</button>
	</form>
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
		background: rgba(224, 64, 64, 0.1);
		border: 1px solid rgba(224, 64, 64, 0.3);
		color: var(--danger);
		padding: 10px;
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
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	input {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		font-size: 0.9rem;
		color: var(--text);
	}
	button {
		margin-top: 8px;
		padding: 10px;
		border-radius: 4px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-weight: 500;
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
</style>
