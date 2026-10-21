<script lang="ts">
	import AuthShell from '#lib/components/auth/AuthShell.svelte';
	import BrandMark from '#lib/components/ui/BrandMark.svelte';
	import { auth, type MFAChallenge } from '#lib/stores/auth.svelte.ts';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount, tick } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { authErrorMessage } from '#lib/api/authRefusals.ts';
	import { nextFromParams, postLoginTarget, withNext } from '#lib/utils/postLoginRedirect.ts';
	import { normalizeOneTimeCode } from '#lib/utils/oneTimeCode.ts';

	type Method = 'totp' | 'passkey' | 'email';

	const CHALLENGE_KEY = 'mfa_challenge';

	let challenge = $state<MFAChallenge | null>(null);
	let method = $state<Method>('totp');
	let code = $state('');
	let error = $state('');
	let notice = $state('');
	let loading = $state(false);
	let emailSent = $state(false);
	// A request for an email code is in flight: the button is disabled so a
	// double-click cannot send two codes (the second would void the first).
	let sendingEmail = $state(false);

	// The page the user was opening before sign-in, carried from /login.
	const next = $derived(nextFromParams(page.url.searchParams));
	const signInHref = $derived(withNext('/login', next));

	// The control each method starts on. Every method switch and the email
	// send REMOVE the button that was clicked, so focus would otherwise fall
	// to <body>; it is moved here instead (ui-patterns § Inline form feedback).
	let codeInput = $state<HTMLInputElement | null>(null);
	let passkeyButton = $state<HTMLButtonElement | null>(null);
	let emailButton = $state<HTMLButtonElement | null>(null);

	async function focusCurrentControl() {
		await tick();
		if (method === 'passkey') passkeyButton?.focus();
		else if (method === 'email' && !emailSent) emailButton?.focus();
		else codeInput?.focus();
	}

	function readChallenge(): string | null {
		try {
			return sessionStorage.getItem(CHALLENGE_KEY);
		} catch {
			return null;
		}
	}

	function clearChallenge() {
		try {
			sessionStorage.removeItem(CHALLENGE_KEY);
		} catch {
			// Storage blocked — nothing was stored to clear.
		}
	}

	onMount(() => {
		const raw = readChallenge();
		if (!raw) {
			goto(signInHref);
			return;
		}
		try {
			challenge = JSON.parse(raw) as MFAChallenge;
			// Prefer the strongest available factor: passkey > totp > email.
			if (challenge.methods.includes('passkey')) method = 'passkey';
			else if (challenge.methods.includes('totp')) method = 'totp';
			else method = 'email';
		} catch {
			goto(signInHref);
			return;
		}
		void focusCurrentControl();
	});

	function finish() {
		if (!challenge) return;
		clearChallenge();
		// If the org enforces MFA but the user wasn't enrolled, send them
		// straight to the enrollment screen on the profile.
		goto(challenge.must_enroll ? '/profile' : postLoginTarget(next));
	}

	async function verifyPasskey() {
		if (!challenge || loading) return;
		error = '';
		loading = true;
		try {
			await auth.completePasskey(challenge.mfa_challenge_token);
			finish();
		} catch (err) {
			error = authErrorMessage(err, m, 'auth.mfa.error.passkey');
		} finally {
			loading = false;
		}
	}

	async function sendEmailCode() {
		// `aria-disabled` while sending, not `disabled`: a focused button that
		// becomes disabled throws keyboard focus to <body>, and on a failed
		// send the user would be left nowhere.
		if (!challenge || sendingEmail) return;
		error = '';
		notice = '';
		sendingEmail = true;
		try {
			await auth.requestEmailMfa(challenge.mfa_challenge_token);
			emailSent = true;
			notice = m('auth.mfa.emailSent');
			await focusCurrentControl();
		} catch (err) {
			error = err instanceof Error ? err.message : m('auth.mfa.error.emailSend');
		} finally {
			sendingEmail = false;
		}
	}

	function onCodeInput(e: Event & { currentTarget: HTMLInputElement }) {
		// Leave an input-method composition alone until it ends (the
		// `compositionend` call normalises it), or the rewrite breaks it.
		if (e instanceof InputEvent && e.isComposing) return;
		// Digits only, whatever the paste carried (`123 456`, a trailing
		// newline, full-width digits): what the user sees is exactly what is sent.
		code = normalizeOneTimeCode(e.currentTarget.value);
		e.currentTarget.value = code;
	}

	async function handleSubmit(e: Event) {
		e.preventDefault();
		if (!challenge) return;
		// The passkey factor has its own button (verifyPasskey); this code form
		// only submits the totp / email code methods.
		if (method === 'passkey') return;
		const submitted = normalizeOneTimeCode(code);
		if (submitted.length < 6) return;
		error = '';
		loading = true;
		try {
			await auth.completeMfa(challenge.mfa_challenge_token, submitted, method);
			finish();
		} catch (err) {
			error = err instanceof Error ? err.message : m('auth.mfa.error.verify');
		} finally {
			loading = false;
		}
	}

	function switchMethod(to: Method) {
		method = to;
		code = '';
		error = '';
		notice = '';
		emailSent = false;
		void focusCurrentControl();
	}

	/** Abandon this challenge (expired, lost device) and start over. The
	 *  challenge is cleared so /login does not bounce straight back here. */
	function backToSignIn(e: MouseEvent) {
		// A modified click (new tab / window) stays an ordinary link and keeps
		// this tab's challenge.
		if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
		e.preventDefault();
		clearChallenge();
		goto(signInHref);
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

		<div role="status" aria-live="polite">
			{#if notice}
				<div class="info" id="mfa-notice">{notice}</div>
			{/if}
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
			<button
				type="button"
				class="primary"
				onclick={verifyPasskey}
				aria-disabled={loading}
				bind:this={passkeyButton}
			>
				{loading ? m('auth.mfa.waitingForPasskey') : m('auth.mfa.verifyWithPasskey')}
			</button>
		{/if}

		{#if method === 'email' && !emailSent}
			<button
				type="button"
				class="secondary"
				onclick={sendEmailCode}
				aria-disabled={sendingEmail}
				bind:this={emailButton}
			>
				{sendingEmail ? m('auth.mfa.sendingEmail') : m('auth.mfa.emailMeCode')}
			</button>
		{/if}

		{#if method !== 'passkey' && (method === 'totp' || emailSent)}
			<label>
				<span>{m('auth.mfa.codeLabel')}</span>
				<!-- No maxlength: it would truncate a paste BEFORE the non-digits
				     are stripped (`Code: 123 456` → `Code: 12`). onCodeInput caps
				     the normalised value at 8 digits instead. -->
				<input
					type="text"
					inputmode="numeric"
					pattern="[0-9]*"
					autocomplete="one-time-code"
					value={code}
					oninput={onCodeInput}
					oncompositionend={onCodeInput}
					aria-describedby={notice ? 'mfa-notice' : undefined}
					bind:this={codeInput}
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

		<a class="back-link" href={signInHref} onclick={backToSignIn}>
			{m('auth.mfa.backToSignIn')}
		</a>
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
		box-shadow: 0 14px 30px -14px color-mix(in srgb, var(--accent) 95%, transparent);
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
	.primary[aria-disabled='true'],
	.secondary[aria-disabled='true'] {
		cursor: progress;
	}

	/* Same treatment as /login's "Forgot password?" link. */
	.back-link {
		align-self: center;
		font-size: 0.85rem;
		color: var(--accent-on-tint);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.back-link:hover {
		color: var(--text);
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
