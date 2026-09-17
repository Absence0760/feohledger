<script lang="ts">
	import { portalAuth } from '$lib/stores/portalAuth.svelte';
	import { portalBrand } from '$lib/stores/portalBrand.svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import { goto } from '$app/navigation';
	import { m } from '$lib/i18n/store.svelte';

	let email = $state('');
	let password = $state('');
	let error = $state('');
	let loading = $state(false);

	// MFA second-factor step. When login returns a challenge, we swap the
	// password form for the code form (stashing the challenge token). The vendor
	// can clear it with their authenticator (`totp`) or, if they've lost it, with
	// an emailed one-time backup code (`email`).
	let mfaChallenge = $state<string | null>(null);
	let mfaCode = $state('');
	let mfaMethod = $state<'totp' | 'email'>('totp');
	let emailSent = $state(false);
	let emailSending = $state(false);

	function afterAuth() {
		if (portalAuth.user?.must_change_password) {
			goto('/portal/change-password');
		} else {
			goto('/portal');
		}
	}

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		loading = true;
		try {
			const res = await portalAuth.login(email, password);
			if (res.kind === 'mfa') {
				mfaChallenge = res.challenge;
				mfaCode = '';
				mfaMethod = 'totp';
				emailSent = false;
			} else {
				afterAuth();
			}
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.login.failed');
		} finally {
			loading = false;
		}
	}

	async function handleMfaSubmit(e: Event) {
		e.preventDefault();
		if (!mfaChallenge) return;
		error = '';
		loading = true;
		try {
			await portalAuth.completeMfa(mfaChallenge, mfaCode, mfaMethod);
			afterAuth();
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.login.mfa.failed');
		} finally {
			loading = false;
		}
	}

	async function sendEmailCode() {
		if (!mfaChallenge) return;
		error = '';
		emailSending = true;
		try {
			await portalAuth.requestEmailMfa(mfaChallenge);
			mfaMethod = 'email';
			mfaCode = '';
			emailSent = true;
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.login.mfa.sendFailed');
		} finally {
			emailSending = false;
		}
	}

	function useAuthenticator() {
		mfaMethod = 'totp';
		mfaCode = '';
		error = '';
	}

	function backToPassword() {
		mfaChallenge = null;
		mfaCode = '';
		mfaMethod = 'totp';
		emailSent = false;
		error = '';
	}
</script>

<div class="login-page">
	<!-- Decorative, brand-derived backdrop (issue #422). aria-hidden, carries
	     no text, and sits behind the card via z-index, so it cannot move
	     focus, tab order or the form fields. Deliberately NOT `AuthShell` +
	     `Atmosphere` (the employee pre-auth shell): that pair's aurora is
	     literal platform rgba() colours and a hardcoded "FeohLedger" mark +
	     tagline — correct for the employee pages, which are platform-branded
	     by policy before any tenant is known, but exactly the platform
	     branding a white-labeled supplier portal must not show
	     (`docs/white-label.md` § Supplier-portal theming; `AuthShell.svelte`'s
	     own doc comment says as much). These two blobs instead use
	     `--accent-glow` / `--accent-wash`, both `color-mix()` off `--accent` in
	     `app.css` specifically so a decorative layer re-themes with a tenant's
	     own accent rather than hardcoding the default indigo. -->
	<div class="backdrop" aria-hidden="true">
		<div class="blob blob-a"></div>
		<div class="blob blob-b"></div>
	</div>

	{#if mfaChallenge}
		<form class="login-card" onsubmit={handleMfaSubmit}>
			{#if portalBrand.mark.kind === 'logo'}
				<img
					class="brand-logo"
					src={portalBrand.logoUrl}
					alt={portalBrand.productName}
					draggable="false"
				/>
			{:else if portalBrand.mark.kind === 'platform'}
				<BrandMark size={36} />
			{/if}
			<h1>{m('portal.login.mfa.title')}</h1>
			<p class="subtitle">
				{#if mfaMethod === 'email'}
					{m('portal.login.mfa.subtitleEmail')}
				{:else}
					{m('portal.login.mfa.subtitleTotp')}
				{/if}
			</p>

			<div role="alert" aria-live="assertive">
				{#if error}
					<div class="error">{error}</div>
				{/if}
			</div>

			<label>
				<span>{mfaMethod === 'email' ? m('portal.login.mfa.emailCodeLabel') : m('portal.login.mfa.codeLabel')}</span>
				<input
					type="text"
					inputmode="numeric"
					autocomplete="one-time-code"
					bind:value={mfaCode}
					maxlength="8"
					required
				/>
			</label>

			<button type="submit" disabled={loading || mfaCode.length < 6}>
				{loading ? m('portal.login.mfa.verifying') : m('portal.login.mfa.verify')}
			</button>

			<div class="divider"><span>{m('portal.login.mfa.or')}</span></div>

			{#if mfaMethod === 'totp'}
				<button type="button" class="secondary" onclick={sendEmailCode} disabled={emailSending}>
					{emailSending ? m('portal.login.mfa.sending') : m('portal.login.mfa.useEmail')}
				</button>
			{:else}
				{#if emailSent}
					<p class="hint">{m('portal.login.mfa.emailSent')}</p>
				{/if}
				<button type="button" class="secondary" onclick={useAuthenticator}>
					{m('portal.login.mfa.useAuthenticator')}
				</button>
			{/if}

			<button type="button" class="link-btn" onclick={backToPassword}>{m('portal.login.mfa.back')}</button>
		</form>
	{:else}
		<form class="login-card" onsubmit={handleSubmit}>
			{#if portalBrand.mark.kind === 'logo'}
				<img
					class="brand-logo"
					src={portalBrand.logoUrl}
					alt={portalBrand.productName}
					draggable="false"
				/>
			{:else if portalBrand.mark.kind === 'platform'}
				<!-- Unbranded tenant: the platform's mark beside the platform's name. A
				     tenant that renamed the product without a logo gets neither (§173). -->
				<BrandMark size={36} />
			{/if}
			<h1>{portalBrand.productName}</h1>
			<p class="subtitle">{m('portal.login.subtitle')}</p>

			<div role="alert" aria-live="assertive">
				{#if error}
					<div class="error">{error}</div>
				{/if}
			</div>

			<label>
				<span>{m('portal.login.email')}</span>
				<input type="email" bind:value={email} required autocomplete="email" />
			</label>
			<label>
				<span>{m('portal.login.password')}</span>
				<input type="password" bind:value={password} required autocomplete="current-password" />
			</label>

			<button type="submit" disabled={loading}>
				{loading ? m('portal.login.signingIn') : m('portal.login.signIn')}
			</button>
		</form>
	{/if}
</div>

<style>
	.login-page {
		position: relative;
		min-height: 100vh;
		display: grid;
		place-items: center;
		overflow: hidden;
		background: var(--bg);
	}

	/* Decorative backdrop — see the template comment above for why this is a
	   local pair of blobs rather than the employee `Atmosphere` component. */
	.backdrop {
		position: absolute;
		inset: 0;
		overflow: hidden;
		pointer-events: none;
		z-index: 0;
	}
	.blob {
		position: absolute;
		border-radius: 50%;
		filter: blur(90px);
	}
	.blob-a {
		top: -20%;
		right: -12%;
		width: 58%;
		height: 62%;
		background: radial-gradient(circle, var(--accent-glow), transparent 70%);
		transform: translate3d(-5%, 6%, 0) scale(1.1);
	}
	.blob-b {
		bottom: -22%;
		left: -14%;
		width: 52%;
		height: 56%;
		background: radial-gradient(circle, var(--accent-wash), transparent 72%);
		transform: translate3d(6%, -5%, 0) scale(0.95);
	}
	/* Held still, for the same reason `AuthShell` passes `still` to
	   `Atmosphere` on every employee pre-auth page: continuous ambient motion
	   that runs past five seconds owes a pause control under WCAG 2.2.2, and a
	   pause button on a sign-in form is clutter. A sign-in page is also where a
	   visitor is idle and typing, so a permanently animating backdrop spends
	   battery for nothing. The resting transforms above are the frame the
	   drift was designed to settle on. */

	.login-card {
		position: relative;
		z-index: 1;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		box-shadow: var(--shadow-card);
		padding: 40px 36px;
		width: min(400px, 90vw);
		display: flex;
		flex-direction: column;
		gap: 16px;
	}
	.brand-logo {
		height: 40px;
		width: auto;
		max-width: 200px;
		object-fit: contain;
		align-self: flex-start;
		/* #435: stop the browser's default drag-to-ghost-image on chrome/brand
		   art. draggable="false" (on the element, above) handles the drag
		   itself; these two cover WebKit/Blink's own image-drag and the
		   selection highlight it leaves behind. */
		-webkit-user-drag: none;
		user-select: none;
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
	button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.link-btn {
		margin-top: 0;
		background: transparent;
		color: var(--text-muted);
		font-weight: 400;
		font-size: 0.82rem;
	}
	.secondary {
		margin-top: 0;
		background: transparent;
		border: 1px solid var(--border);
		color: var(--text);
	}
	.secondary:hover:not(:disabled) {
		border-color: var(--text-muted);
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
	.hint {
		margin: 0;
		font-size: 0.8rem;
		color: var(--text-muted);
	}
</style>
