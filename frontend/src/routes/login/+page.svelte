<script lang="ts">
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import { api } from '$lib/api';
	import { getApiBase, getTenantSlug } from '$lib/tenant';
	import { m } from '$lib/i18n/store.svelte';

	interface SSOConfigPublic {
		enabled: boolean;
		provider: string | null;
		sso_only?: boolean;
	}

	let email = $state('');
	let password = $state('');
	let error = $state('');
	let loading = $state(false);
	let ssoEnabled = $state(false);
	let ssoProviderLabel = $state<string>('');
	let samlEnabled = $state(false);
	let samlProviderLabel = $state<string>('');
	// When the tenant requires SSO, hide the password form entirely. Only ever
	// true alongside an enabled config, so a broken IdP can't lock everyone out.
	let ssoOnly = $state(false);

	const PROVIDER_LABELS: Record<string, string> = {
		okta: 'Okta',
		entra: 'Microsoft',
		oidc: 'SSO',
		saml: 'SSO',
		adfs: 'ADFS',
		onelogin: 'OneLogin',
	};

	// `?slug=` is now OPTIONAL on every SSO/SAML entry point: on a customer's
	// vanity host there is no slug in the hostname (`$lib/hostRouting.ts`
	// classifies it and `getTenantSlug()` returns null so no `X-Tenant-Slug`
	// header suppresses the backend's lookup), and the backend resolves the
	// tenant from the request `Host` against its registered custom domains
	// instead. Omitting the param is therefore the correct call there — a
	// guessed slug would be wrong, and returning early hid the buttons entirely.
	function ssoQuery(): string {
		const slug = getTenantSlug();
		return slug ? `?slug=${encodeURIComponent(slug)}` : '';
	}

	onMount(async () => {
		// A tenant is configured for at most one protocol; query both and render
		// whichever is enabled. Both are non-fatal — password login still works.
		// On the platform apex (marketing / signup host) there is neither a slug
		// nor a matching custom domain, so both calls 404 and are swallowed.
		try {
			const cfg = await api.get<SSOConfigPublic>(`/api/auth/sso/config${ssoQuery()}`);
			ssoEnabled = cfg.enabled;
			ssoProviderLabel = PROVIDER_LABELS[cfg.provider ?? 'oidc'] ?? 'SSO';
			if (cfg.enabled && cfg.sso_only) ssoOnly = true;
		} catch {
			// Non-fatal
		}
		try {
			const cfg = await api.get<SSOConfigPublic>(`/api/auth/saml/config${ssoQuery()}`);
			samlEnabled = cfg.enabled;
			samlProviderLabel = PROVIDER_LABELS[cfg.provider ?? 'saml'] ?? 'SSO';
			if (cfg.enabled && cfg.sso_only) ssoOnly = true;
		} catch {
			// Non-fatal
		}
	});

	function signInWithSSO() {
		// 302 directly to the backend authorize endpoint — it builds the IdP
		// URL and redirects the browser onward. Full page nav, not fetch,
		// because we need the browser to follow the IdP's redirects.
		//
		// `getApiBase()`, not the build-time `PUBLIC_API_URL`: on a vanity host
		// it resolves to same-origin, which is the ONLY way the vanity hostname
		// reaches the backend in the `Host` header it resolves the tenant from.
		window.location.href = `${getApiBase()}/api/auth/sso/authorize${ssoQuery()}`;
	}

	function signInWithSAML() {
		// Full page nav to the backend SAML login endpoint — it builds the
		// AuthnRequest and 302s onward to the IdP (same reason as OIDC).
		window.location.href = `${getApiBase()}/api/auth/saml/login${ssoQuery()}`;
	}

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		loading = true;
		try {
			const result = await auth.login(email, password);
			if (result.kind === 'mfa') {
				// Stash the challenge in sessionStorage so the verify page can
				// pick it up. sessionStorage clears on tab close — won't outlive
				// the login attempt.
				sessionStorage.setItem('mfa_challenge', JSON.stringify(result.challenge));
				goto('/login/mfa');
				return;
			}
			goto('/');
		} catch (err) {
			error = err instanceof Error ? err.message : m('auth.login.failed');
		} finally {
			loading = false;
		}
	}
</script>

<div class="login-page">
	<form class="login-card" onsubmit={handleSubmit}>
		<div class="brand-head">
			<BrandMark size={36} />
			<h1>{m('auth.login.heading')}</h1>
		</div>
		<p class="subtitle">{m('auth.login.subtitle')}</p>

		<div role="alert" aria-live="assertive">
			{#if error}
				<div class="error">{error}</div>
			{/if}
		</div>

		{#if !ssoOnly}
			<label>
				<span>{m('auth.login.email')}</span>
				<input type="email" bind:value={email} required autocomplete="email" />
			</label>
			<label>
				<span>{m('auth.login.password')}</span>
				<input type="password" bind:value={password} required autocomplete="current-password" />
			</label>

			<button type="submit" disabled={loading}>
				{loading ? m('auth.login.signingIn') : m('auth.login.signIn')}
			</button>
			<a class="forgot-link" href="/login/forgot-password">{m('auth.login.forgotPassword')}</a>
		{:else}
			<p class="sso-only-note">{m('auth.login.ssoOnly')}</p>
		{/if}

		{#if !ssoOnly && (ssoEnabled || samlEnabled)}
			<div class="divider"><span>{m('auth.login.or')}</span></div>
		{/if}
		{#if ssoEnabled}
			<button type="button" class="sso-btn" onclick={signInWithSSO}>
				{m('auth.login.signInWith', { provider: ssoProviderLabel })}
			</button>
		{/if}
		{#if samlEnabled}
			<button type="button" class="sso-btn" onclick={signInWithSAML}>
				{m('auth.login.signInWith', { provider: samlProviderLabel })}
			</button>
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

	.brand-head {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.subtitle {
		margin: -8px 0 8px;
		font-size: 0.88rem;
		color: var(--text-muted);
	}

	.sso-only-note {
		margin: 4px 0;
		font-size: 0.9rem;
		color: var(--text-muted);
		text-align: center;
	}

	.forgot-link {
		margin-top: -4px;
		font-size: 0.82rem;
		color: var(--accent);
		text-align: center;
		text-decoration: underline;
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

	button:hover:not(:disabled) {
		opacity: 0.9;
	}

	button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
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

	.sso-btn {
		background: transparent;
		border: 1px solid var(--border);
		color: var(--text);
	}
	.sso-btn:hover:not(:disabled) {
		border-color: var(--text-muted);
		opacity: 1;
	}
</style>
