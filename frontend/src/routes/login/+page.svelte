<script lang="ts">
	import AuthShell from '#lib/components/auth/AuthShell.svelte';
	import PasswordInput from '#lib/components/auth/PasswordInput.svelte';
	import AuthBrandMark from '#lib/components/auth/AuthBrandMark.svelte';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { emailFromQuery } from '#lib/utils/prefillEmail.ts';
	import { api, ApiError } from '#lib/api.ts';
	import { loginNoticeKey, ownWorkspaceSignInUrl } from '#lib/api/sessionRejection.ts';
	import { getApiBase, getTenantSlug, hasTenantContext } from '#lib/tenant.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { SSO_PROVIDER_LABELS } from '#lib/types/ssoSettings.ts';
	import {
		nextFromParams,
		postLoginTarget,
		rememberNextForSso,
		withNext
	} from '#lib/utils/postLoginRedirect.ts';

	interface SSOConfigPublic {
		enabled: boolean;
		provider: string | null;
		sso_only?: boolean;
	}

	let email = $state('');
	let password = $state('');
	let error = $state('');
	// Why the user is on this page, when `api.ts` sent them here (a session for
	// another workspace). Information, not a failure: it renders as a neutral
	// notice in a polite live region, and a submit clears it.
	let notice = $state('');
	// The account's own sign-in page, when the server refused a sign-in made
	// on another workspace's page (`wrong_workspace`, decisions §288).
	let ownWorkspaceUrl = $state<string | null>(null);
	let loading = $state(false);
	let ssoEnabled = $state(false);
	let ssoProviderLabel = $state<string>('');
	let samlEnabled = $state(false);
	let samlProviderLabel = $state<string>('');
	// When the tenant requires SSO, hide the password form entirely. Only ever
	// true alongside an enabled config, so a broken IdP can't lock everyone out.
	let ssoOnly = $state(false);
	// Both SSO config answers are in (either way). Until then the page shows a
	// short loading state instead of the password form: an SSO-only workspace
	// used to draw the form and then pull it away (issue #635 finding 33).
	let configLoaded = $state(false);
	// The workspace this page signs in to, by its address — the host the
	// visitor is on, so it says nothing a public endpoint would have to.
	// `null` on the platform apex, which has no workspace. Read at init (the
	// app is static, there is no server render), so it is there on first paint.
	const workspaceHost = hasTenantContext() ? window.location.host : null;

	// The page the user was sent here from (the auth wall, an ended session),
	// already validated as a same-origin path; `null` means the dashboard.
	const next = $derived(nextFromParams(page.url.searchParams));

	// Shared with the `/organization` SSO panel's picker, so an admin can only
	// pick a provider this page knows how to label.
	const PROVIDER_LABELS = SSO_PROVIDER_LABELS;

	// `?slug=` is now OPTIONAL on every SSO/SAML entry point: on a customer's
	// vanity host there is no slug in the hostname (`#lib/hostRouting.ts`
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
		// A sign-in link we built (the signup welcome email, the reset page's
		// "go to sign in") names the account; the field is still editable.
		email = emailFromQuery(page.url.searchParams);
		// `api.ts` sends a session it had to end here with the reason in the
		// URL — a token for another workspace than this page's.
		const noticeKey = loginNoticeKey(page.url.searchParams);
		if (noticeKey) notice = m(noticeKey);
		// A tenant is configured for at most one protocol; query both — at once,
		// not one after the other — and render whichever is enabled. Both are
		// non-fatal: a failed call leaves password sign-in, as before. On the
		// platform apex (marketing / signup host) there is neither a slug nor a
		// matching custom domain, so both calls 404 and are ignored. Each is
		// bounded, so a config endpoint that hangs (a stalled proxy) falls back
		// to the password form rather than holding the page on the spinner.
		try {
			const [oidc, saml] = await Promise.allSettled([
				withinConfigWait(api.get<SSOConfigPublic>(`/api/auth/sso/config${ssoQuery()}`)),
				withinConfigWait(api.get<SSOConfigPublic>(`/api/auth/saml/config${ssoQuery()}`))
			]);
			if (oidc.status === 'fulfilled') {
				ssoEnabled = oidc.value.enabled;
				ssoProviderLabel = PROVIDER_LABELS[oidc.value.provider ?? 'oidc'] ?? 'SSO';
				if (oidc.value.enabled && oidc.value.sso_only) ssoOnly = true;
			}
			if (saml.status === 'fulfilled') {
				samlEnabled = saml.value.enabled;
				samlProviderLabel = PROVIDER_LABELS[saml.value.provider ?? 'saml'] ?? 'SSO';
				if (saml.value.enabled && saml.value.sso_only) ssoOnly = true;
			}
		} finally {
			configLoaded = true;
		}
	});

	/** How long the page waits for one SSO config answer before treating it
	 *  as unavailable — the same outcome as a failed call: password sign-in. */
	const CONFIG_WAIT_MS = 5000;

	function withinConfigWait<T>(call: Promise<T>): Promise<T> {
		return new Promise<T>((resolve, reject) => {
			const timer = setTimeout(() => reject(new Error('sso config timed out')), CONFIG_WAIT_MS);
			call.then(resolve, reject).finally(() => clearTimeout(timer));
		});
	}

	function signInWithSSO() {
		// 302 directly to the backend authorize endpoint — it builds the IdP
		// URL and redirects the browser onward. Full page nav, not fetch,
		// because we need the browser to follow the IdP's redirects.
		//
		// `getApiBase()`, not the build-time `PUBLIC_API_URL`: on a vanity host
		// it resolves to same-origin, which is the ONLY way the vanity hostname
		// reaches the backend in the `Host` header it resolves the tenant from.
		//
		// The IdP round trip cannot carry `next`, so it waits in sessionStorage
		// for the callback page (`#lib/utils/postLoginRedirect.ts`).
		rememberNextForSso(next);
		window.location.href = `${getApiBase()}/api/auth/sso/authorize${ssoQuery()}`;
	}

	function signInWithSAML() {
		// Full page nav to the backend SAML login endpoint — it builds the
		// AuthnRequest and 302s onward to the IdP (same reason as OIDC).
		rememberNextForSso(next);
		window.location.href = `${getApiBase()}/api/auth/saml/login${ssoQuery()}`;
	}

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		notice = '';
		ownWorkspaceUrl = null;
		loading = true;
		try {
			const result = await auth.login(email, password);
			if (result.kind === 'mfa') {
				// Stash the challenge in sessionStorage so the verify page can
				// pick it up. sessionStorage clears on tab close — won't outlive
				// the login attempt.
				sessionStorage.setItem('mfa_challenge', JSON.stringify(result.challenge));
				goto(withNext('/login/mfa', next));
				return;
			}
			goto(postLoginTarget(next));
		} catch (err) {
			error = err instanceof Error ? err.message : m('auth.login.failed');
			ownWorkspaceUrl =
				err instanceof ApiError && err.code === 'wrong_workspace'
					? ownWorkspaceSignInUrl(err.params)
					: null;
		} finally {
			loading = false;
		}
	}
</script>

<AuthShell>
	<form class="login-form" onsubmit={handleSubmit}>
		<div class="head">
			<AuthBrandMark />
			<h1>{m('auth.login.heading')}</h1>
			{#if workspaceHost}
				<p class="subtitle workspace">{m('auth.login.workspace', { workspace: workspaceHost })}</p>
			{:else}
				<p class="subtitle">{m('auth.login.subtitle')}</p>
			{/if}
		</div>

		<div role="status" aria-live="polite">
			{#if notice}
				<div class="info">{notice}</div>
			{/if}
		</div>

		<div role="alert" aria-live="assertive">
			{#if error}
				<!-- AuthShell stacks the banner: the sentence, then the link on its
				     own full-width line. The sentence names the workspace by host
				     only; the URL lives in the link (codedRefusals `wrong_workspace`). -->
				<div class="error">
					<span>{error}</span>
					{#if ownWorkspaceUrl}
						<a class="own-workspace-link" href={ownWorkspaceUrl}>
							{m('auth.login.goToOwnWorkspace')}
						</a>
					{/if}
				</div>
			{/if}
		</div>

		{#if !configLoaded}
			<!-- Held until both SSO config answers are in, so an SSO-only
			     workspace never shows the password form first. The text fades
			     in only if the wait is noticeable. -->
			<div class="config-loading" role="status">
				<span class="spinner" aria-hidden="true"></span>
				<span>{m('auth.login.loadingOptions')}</span>
			</div>
		{:else if !ssoOnly}
			<label>
				<span>{m('auth.login.email')}</span>
				<input type="email" bind:value={email} required autocomplete="username" />
			</label>
			<PasswordInput
				id="login-password"
				label={m('auth.login.password')}
				bind:value={password}
				required
				autocomplete="current-password"
			/>

			<button type="submit" disabled={loading}>
				{loading ? m('auth.login.signingIn') : m('auth.login.signIn')}
			</button>
			<a class="forgot-link" href="/login/forgot-password">{m('auth.login.forgotPassword')}</a>
		{:else}
			<p class="sso-only-note">{m('auth.login.ssoOnly')}</p>
		{/if}

		{#if configLoaded && !ssoOnly && (ssoEnabled || samlEnabled)}
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

	<!-- Local development only: `import.meta.env.DEV` is a compile-time
	     constant, so `pnpm build` drops this branch AND the dynamic import with
	     it — no seeded address reaches the deployed bundle
	     (`scripts/check_dev_accounts_stripped.mjs` checks the build). -->
	{#if import.meta.env.DEV && configLoaded && !ssoOnly}
		{#await import('#lib/dev/DevAccountsPanel.svelte') then { default: DevAccountsPanel }}
			<DevAccountsPanel
				slug={getTenantSlug()}
				onpick={(account) => {
					email = account.email;
					password = account.password;
				}}
			/>
		{/await}
	{/if}

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
	/* A host can be long and has no spaces to break at. */
	.subtitle.workspace {
		overflow-wrap: anywhere;
	}

	/* Roughly the height of the password form it stands in for, so the page
	   does not collapse and regrow while the config loads. Scoped under the
	   form so its own delayed fade wins over AuthShell's field entrance. */
	.login-form .config-loading {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 10px;
		min-height: 200px;
		font-size: 0.88rem;
		color: var(--text-muted);
		animation: config-wait-in 0.2s ease 0.4s backwards;
	}
	@keyframes config-wait-in {
		from {
			opacity: 0;
		}
	}
	.spinner {
		width: 18px;
		height: 18px;
		border: 2px solid var(--border);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
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

	.own-workspace-link {
		color: inherit;
		font-weight: 600;
		text-decoration: underline;
		text-underline-offset: 2px;
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
