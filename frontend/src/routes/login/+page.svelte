<script lang="ts">
	import AuthShell from '$lib/components/auth/AuthShell.svelte';
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

<AuthShell>
	<form class="login-form" onsubmit={handleSubmit}>
		<div class="head">
			<BrandMark size={40} />
			<h1>{m('auth.login.heading')}</h1>
			<p class="subtitle">{m('auth.login.subtitle')}</p>
		</div>

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

	{#snippet panel()}
		<p class="panel-line">{m('auth.login.panelBody')}</p>
	{/snippet}
</AuthShell>

<style>
	/* Field chrome, the submit button, the error banner and the entrance come
	   from AuthShell; this is only what sign-in has that the others do not. */
	.login-form {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	.head {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 10px;
		margin-bottom: 6px;
	}
	.subtitle {
		margin: 0;
		font-size: 0.93rem;
		color: var(--text-muted);
	}

	.sso-only-note {
		margin: 4px 0;
		font-size: 0.9rem;
		color: var(--text-muted);
		text-align: center;
	}

	.forgot-link {
		align-self: center;
		margin-top: -4px;
		font-size: 0.85rem;
		color: var(--accent-on-tint);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.forgot-link:hover {
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

	.sso-btn {
		min-height: 46px;
		padding: 11px 16px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.93rem;
		font-weight: 500;
		cursor: pointer;
		transition: border-color 0.15s, background 0.15s;
	}
	.sso-btn:hover {
		border-color: var(--accent);
	}

	.panel-line {
		margin: 0;
		max-width: 38ch;
		font-size: 1rem;
		line-height: 1.6;
		color: var(--text-muted);
	}
</style>
