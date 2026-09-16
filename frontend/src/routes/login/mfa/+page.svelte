<script lang="ts">
	import AuthShell from '$lib/components/auth/AuthShell.svelte';
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

<AuthShell>
	<form class="mfa-form" onsubmit={handleSubmit}>
		<div class="head">
			<BrandMark size={40} />
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
		</div>

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
			<button type="button" class="primary" onclick={verifyPasskey} disabled={loading}>
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
</AuthShell>

<style>
	/* Field chrome, the submit button, the error banner and the entrance come
	   from AuthShell; this is the one-time-code field and the method switchers. */
	.mfa-form {
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
	.subtitle {
		margin: 0;
		font-size: 0.93rem;
		line-height: 1.55;
		color: var(--text-muted);
	}

	.info {
		padding: 11px 14px;
		border-radius: 10px;
		border: 1px solid rgba(99, 140, 255, 0.28);
		background: var(--accent-tint);
		color: var(--text);
		font-size: 0.85rem;
		line-height: 1.5;
	}

	/* A code, not prose: centred, spaced digits, tabular so they do not shift
	   as they are typed. Overrides the shell's field default on specificity,
	   which the shell leaves open by writing its rules under `:where()`. */
	.mfa-form input[autocomplete='one-time-code'] {
		font-size: 1.3rem;
		font-weight: 600;
		letter-spacing: 0.35em;
		text-align: center;
		font-variant-numeric: tabular-nums;
	}

	.primary,
	.secondary {
		min-height: 46px;
		padding: 11px 16px;
		border-radius: 10px;
		font-family: inherit;
		font-size: 0.93rem;
		font-weight: 600;
		cursor: pointer;
		transition: transform 0.15s, border-color 0.15s, box-shadow 0.15s;
	}
	.primary {
		border: none;
		background: var(--accent-strong);
		color: #fff;
		box-shadow: 0 14px 30px -14px rgba(99, 140, 255, 0.95);
	}
	.primary:hover:not(:disabled) {
		transform: translateY(-1px);
	}
	.primary:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}
	.secondary {
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-weight: 500;
	}
	.secondary:hover:not(:disabled) {
		border-color: var(--accent);
	}

	.divider {
		display: flex;
		align-items: center;
		gap: 12px;
		color: var(--text-muted);
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.1em;
		margin: 2px 0;
	}
	.divider::before,
	.divider::after {
		content: '';
		flex: 1;
		height: 1px;
		background: var(--border);
	}
</style>
