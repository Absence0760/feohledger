<script lang="ts">
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import { auth, type MFAChallenge } from '$lib/stores/auth.svelte';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import { m } from '$lib/i18n/store.svelte';

	type Method = 'totp' | 'passkey' | 'email';

	let challenge = $state<MFAChallenge | null>(null);
	let method = $state<Method>('totp');
	let code = $state('');
	let error = $state('');
	let loading = $state(false);
	let emailSent = $state(false);

	onMount(() => {
		const raw = sessionStorage.getItem('mfa_challenge');
		if (!raw) {
			goto('/login');
			return;
		}
		try {
			challenge = JSON.parse(raw) as MFAChallenge;
			// Prefer the strongest available factor: passkey > totp > email.
			if (challenge.methods.includes('passkey')) method = 'passkey';
			else if (challenge.methods.includes('totp')) method = 'totp';
			else method = 'email';
		} catch {
			goto('/login');
		}
	});

	async function verifyPasskey() {
		if (!challenge) return;
		error = '';
		loading = true;
		try {
			await auth.completePasskey(challenge.mfa_challenge_token);
			sessionStorage.removeItem('mfa_challenge');
			goto(challenge.must_enroll ? '/profile' : '/');
		} catch (err) {
			error = err instanceof Error ? err.message : m('auth.mfa.error.passkey');
		} finally {
			loading = false;
		}
	}

	async function sendEmailCode() {
		if (!challenge) return;
		error = '';
		try {
			await auth.requestEmailMfa(challenge.mfa_challenge_token);
			emailSent = true;
		} catch (err) {
			error = err instanceof Error ? err.message : m('auth.mfa.error.emailSend');
		}
	}

	async function handleSubmit(e: Event) {
		e.preventDefault();
		if (!challenge) return;
		// The passkey factor has its own button (verifyPasskey); this code form
		// only submits the totp / email code methods.
		if (method === 'passkey') return;
		error = '';
		loading = true;
		try {
			await auth.completeMfa(challenge.mfa_challenge_token, code, method);
			sessionStorage.removeItem('mfa_challenge');
			// If the org enforces MFA but the user wasn't enrolled, send them
			// straight to the enrollment screen on the profile.
			if (challenge.must_enroll) {
				goto('/profile');
			} else {
				goto('/');
			}
		} catch (err) {
			error = err instanceof Error ? err.message : m('auth.mfa.error.verify');
		} finally {
			loading = false;
		}
	}

	function switchMethod(next: Method) {
		method = next;
		code = '';
		error = '';
		emailSent = false;
	}
</script>

<div class="login-page">
	<form class="login-card" onsubmit={handleSubmit}>
		<BrandMark size={32} />
		<h1>{m('auth.mfa.heading')}</h1>
		<p class="subtitle">
			{#if method === 'passkey'}
				{m('auth.mfa.subtitle.passkey')}
			{:else if method === 'totp'}
				{m('auth.mfa.subtitle.totp')}
			{:else}
				{m('auth.mfa.subtitle.email')}
			{/if}
		</p>

		<div role="alert" aria-live="assertive">
			{#if error}
				<div class="error">{error}</div>
			{/if}
		</div>

		{#if challenge && challenge.must_enroll && method === 'email'}
			<div class="info">
				{m('auth.mfa.enrollNotice')}
			</div>
		{/if}

		{#if method === 'passkey'}
			<button type="button" onclick={verifyPasskey} disabled={loading}>
				{loading ? m('auth.mfa.waitingForPasskey') : m('auth.mfa.verifyWithPasskey')}
			</button>
		{/if}

		{#if method === 'email' && !emailSent}
			<button type="button" class="secondary" onclick={sendEmailCode}>
				{m('auth.mfa.emailMeCode')}
			</button>
		{/if}

		{#if method !== 'passkey' && (method === 'totp' || emailSent)}
			<label>
				<span>{m('auth.mfa.codeLabel')}</span>
				<input
					type="text"
					inputmode="numeric"
					pattern="[0-9]*"
					autocomplete="one-time-code"
					bind:value={code}
					maxlength="8"
					required
				/>
			</label>
			<button type="submit" disabled={loading || code.length < 6}>
				{loading ? m('auth.mfa.verifying') : m('auth.mfa.verify')}
			</button>
		{/if}

		{#if challenge && challenge.methods.length > 1}
			<div class="divider"><span>{m('auth.mfa.or')}</span></div>
			{#if method !== 'passkey' && challenge.methods.includes('passkey')}
				<button type="button" class="secondary" onclick={() => switchMethod('passkey')}>
					{m('auth.mfa.usePasskey')}
				</button>
			{/if}
			{#if method !== 'totp' && challenge.methods.includes('totp')}
				<button type="button" class="secondary" onclick={() => switchMethod('totp')}>
					{m('auth.mfa.useTotp')}
				</button>
			{/if}
			{#if method !== 'email' && challenge.methods.includes('email')}
				<button type="button" class="secondary" onclick={() => switchMethod('email')}>
					{m('auth.mfa.useEmail')}
				</button>
			{/if}
		{/if}
	</form>
</div>

<style>
	.login-page {
		min-height: 100vh;
		display: grid;
		place-items: center;
		background: var(--bg);
	}

	.login-card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 40px 36px;
		width: min(400px, 90vw);
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	h1 {
		margin: 0;
		font-size: 1.3rem;
		font-weight: 700;
		color: var(--text);
	}

	.subtitle {
		margin: -8px 0 8px;
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

	.info {
		background: rgba(99, 140, 255, 0.08);
		border: 1px solid rgba(99, 140, 255, 0.25);
		color: var(--text);
		padding: 10px 14px;
		border-radius: 4px;
		font-size: 0.82rem;
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
		font-size: 1.05rem;
		letter-spacing: 0.15em;
		text-align: center;
		color: var(--text);
		font-family: inherit;
	}

	input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	button {
		margin-top: 8px;
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
		opacity: 0.6;
		cursor: not-allowed;
	}

	.secondary {
		background: transparent;
		border: 1px solid var(--border);
		color: var(--text);
	}
	.secondary:hover:not(:disabled) {
		border-color: var(--text-muted);
		opacity: 1;
	}

	.divider {
		display: flex;
		align-items: center;
		gap: 10px;
		color: var(--text-muted);
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		margin: 4px 0;
	}
	.divider::before,
	.divider::after {
		content: '';
		flex: 1;
		height: 1px;
		background: var(--border);
	}
</style>
