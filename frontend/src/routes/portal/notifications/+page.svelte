<script lang="ts">
	import { onMount } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import {
		getPortalNotificationPreferences,
		updatePortalNotificationPreferences,
		type PortalNotificationPreferences,
	} from '#lib/portalApi.ts';

	let emailOnPayment = $state(true);
	let emailOnRejection = $state(true);
	let loading = $state(true);
	let saving = $state(false);
	let msg = $state('');
	let err = $state('');

	function apply(p: PortalNotificationPreferences) {
		emailOnPayment = p.email_on_payment;
		emailOnRejection = p.email_on_rejection;
	}

	async function load() {
		loading = true;
		err = '';
		try {
			apply(await getPortalNotificationPreferences());
		} catch (e) {
			err = e instanceof Error ? e.message : m('portal.notifications.loadFailed');
		} finally {
			loading = false;
		}
	}

	async function save(e: Event) {
		e.preventDefault();
		saving = true;
		msg = '';
		err = '';
		try {
			apply(
				await updatePortalNotificationPreferences({
					email_on_payment: emailOnPayment,
					email_on_rejection: emailOnRejection,
				})
			);
			msg = m('portal.notifications.saved');
		} catch (e) {
			err = e instanceof Error ? e.message : m('portal.notifications.saveFailed');
		} finally {
			saving = false;
		}
	}

	onMount(load);
</script>

<div class="page">
	<header>
		<h1>{m('portal.notifications.title')}</h1>
		<p class="sub">{m('portal.notifications.subtitle')}</p>
	</header>

	{#if err}<div class="error" role="alert">{err}</div>{/if}
	<div role="status">{#if msg}<div class="message">{msg}</div>{/if}</div>

	<section class="card">
		<h2>{m('portal.notifications.cardTitle')}</h2>
		<p class="note">{m('portal.notifications.cardNote')}</p>
		{#if loading}
			<p class="note">{m('portal.notifications.loading')}</p>
		{:else}
			<form onsubmit={save}>
				<label class="toggle">
					<input type="checkbox" bind:checked={emailOnPayment} />
					<span>{m('portal.notifications.onPaid')}</span>
				</label>
				<label class="toggle">
					<input type="checkbox" bind:checked={emailOnRejection} />
					<span>{m('portal.notifications.onRejected')}</span>
				</label>
				<button type="submit" class="btn-primary" disabled={saving}>
					{saving ? m('portal.notifications.saving') : m('portal.notifications.save')}
				</button>
			</form>
		{/if}
	</section>
</div>

<style>
	.page {
		max-width: 700px;
		margin: 0 auto;
	}
	header {
		margin-bottom: 20px;
	}
	h1 {
		margin: 0;
		font-size: 1.25rem;
	}
	.sub {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 18px 20px;
		margin-bottom: 18px;
	}
	h2 {
		margin: 0 0 4px;
		font-size: 1rem;
	}
	.note {
		margin: 0 0 14px;
		color: var(--text-muted);
		font-size: 0.82rem;
	}
	form {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	.toggle {
		display: flex;
		align-items: center;
		gap: 10px;
		font-size: 0.9rem;
		color: var(--text);
	}
	.toggle input {
		width: 16px;
		height: 16px;
	}
	.btn-primary {
		align-self: flex-start;
		padding: 8px 16px;
		border: none;
		border-radius: 4px;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		cursor: pointer;
	}
	.btn-primary:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.error {
		background: rgba(224, 64, 64, 0.1);
		border: 1px solid rgba(224, 64, 64, 0.3);
		color: var(--danger);
		padding: 8px 12px;
		border-radius: 4px;
		margin-bottom: 10px;
		font-size: 0.85rem;
	}
	.message {
		background: rgba(40, 160, 80, 0.12);
		border: 1px solid rgba(40, 160, 80, 0.35);
		color: var(--success);
		padding: 8px 12px;
		border-radius: 4px;
		margin-bottom: 10px;
		font-size: 0.85rem;
	}
</style>
