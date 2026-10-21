<script lang="ts">
	import AuthShell from '#lib/components/auth/AuthShell.svelte';
	import BrandMark from '#lib/components/ui/BrandMark.svelte';
	import IconCheck from '~icons/material-symbols/check-circle-outline';
	import { api, ApiError } from '#lib/api.ts';
	import { SIGNUP_EMAIL_HAS_WORKSPACE } from '#lib/api/codedRefusals.ts';
	import { ownWorkspaceSignInUrl } from '#lib/api/sessionRejection.ts';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { onMount, untrack } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	interface CompleteResponse {
		status: string;
		slug: string;
		tenant_url: string;
		admin_email: string;
		// Single-use link to choose the admin's first password (decisions §283).
		// Null when the server could not build one; Forgot password is the way in.
		set_password_url: string | null;
		// The workspace's sign-in page, address prefilled.
		sign_in_url?: string | null;
		// The link had already been used (a reload, a second tab, a mail scanner
		// that ran it first): the workspace exists, nothing was re-issued, and
		// `set_password_url` is null — the welcome email holds the one link
		// (decisions §292).
		already_verified?: boolean;
	}

	// `expired` is the backend's uniform 410: a link that is unknown, past its
	// 24 hours, or spent without having created a workspace. The three are
	// indistinguishable by design (`api/signup.py::_INVALID_LINK`), so the page
	// keys on the status alone and says the one thing true of all of them.
	const TOKEN_STATE_KEY = 'verifyToken';

	let phase = $state<'pending' | 'success' | 'expired' | 'error'>('pending');
	let result = $state<CompleteResponse | null>(null);
	let errorMessage = $state<string>('');
	// The address already has an account (`signup_email_has_workspace`): the
	// existing workspace's sign-in page, offered instead of starting over.
	let existingWorkspaceUrl = $state<string | null>(null);
	// Only an http(s) URL becomes an href (the same reader the login page's
	// wrong-workspace link uses).
	const signInUrl = $derived(
		result ? ownWorkspaceSignInUrl({ sign_in_url: result.sign_in_url }) : null
	);

	/**
	 * Drop `?token=` from the address bar once the request carrying it has
	 * been answered, so the link is not left in the browser's history list, a
	 * copied URL or a screenshot. The token moves into this tab's own history
	 * entry instead (`persistState`), which no copy or share carries but a
	 * reload restores — so a reload still re-sends it and gets the backend's
	 * re-answer for a spent link (decisions §292) rather than "no token".
	 *
	 * A shallow, replacing `goto` (SvelteKit's successor to `replaceState`):
	 * no navigation, no new history entry. Run after the round trip rather
	 * than before it, because the router refuses until it has started, which
	 * on a first load is after the components mount. The one queued retry is
	 * the `ErpConnectionPanel` arrangement, for a router that is still not up.
	 */
	function stripToken(token: string, retry = true) {
		const url = new URL(untrack(() => page.url.href));
		if (!url.searchParams.has('token')) return;
		url.searchParams.delete('token');
		const state: Record<string, unknown> = { ...untrack(() => page.state), [TOKEN_STATE_KEY]: token };
		goto(`${url.pathname}${url.search}${url.hash}`, {
			state,
			shallow: true,
			replace: true,
			persistState: true
		}).catch(() => {
			if (retry) setTimeout(() => stripToken(token, false), 0);
		});
	}

	/** The token: from the link, or from this tab's history entry after a reload. */
	function readToken(): string | null {
		const fromUrl = page.url.searchParams.get('token');
		if (fromUrl) return fromUrl;
		const kept = (page.state as Record<string, unknown>)[TOKEN_STATE_KEY];
		return typeof kept === 'string' && kept ? kept : null;
	}

	onMount(async () => {
		const token = readToken();
		if (!token) {
			phase = 'error';
			errorMessage = m('auth.verify.noToken');
			return;
		}
		try {
			result = await api.post<CompleteResponse>('/api/signup/complete', { token });
			phase = 'success';
		} catch (err) {
			if (err instanceof ApiError && err.status === 410) {
				phase = 'expired';
			} else {
				phase = 'error';
				errorMessage = err instanceof Error ? err.message : m('auth.verify.failed');
				existingWorkspaceUrl =
					err instanceof ApiError && err.code === SIGNUP_EMAIL_HAS_WORKSPACE
						? ownWorkspaceSignInUrl(err.params)
						: null;
			}
		} finally {
			stripToken(token);
		}
	});
</script>

<svelte:head>
	<title>{m('auth.verify.pageTitle')}</title>
</svelte:head>

<AuthShell>
	<!-- The live region is the persistent wrapper, not a node that appears with
	     its content: it exists from first paint in the `pending` phase, so the
	     switch to success or error is a CHANGE inside it and is announced. -->
	<div class="auth-stack verify" aria-live="polite">
		<div class="head">
			{#if phase === 'success' && result}
				<div class="done-icon" aria-hidden="true"><IconCheck /></div>
			{:else}
				<BrandMark size={40} />
			{/if}
			{#if phase === 'pending'}
				<h1>{m('auth.verify.pendingHeading')}</h1>
				<p class="sub">{m('auth.verify.pendingSub')}</p>
			{:else if phase === 'success' && result}
				<h1>{m('auth.verify.successHeading')}</h1>
				{#if result.already_verified}
					<p class="sub">{m('auth.verify.alreadyVerified')}</p>
				{:else}
					<p class="sub">
						{m('auth.verify.successSubPre')}<strong>{result.admin_email}</strong>{m('auth.verify.successSubPost')}
					</p>
				{/if}
			{:else if phase === 'expired'}
				<h1>{m('auth.verify.expiredHeading')}</h1>
				<p class="sub">{m('auth.verify.expiredSub')}</p>
			{:else if existingWorkspaceUrl}
				<h1>{m('auth.verify.existingHeading')}</h1>
			{:else}
				<h1>{m('auth.verify.errorHeading')}</h1>
			{/if}
		</div>

		{#if phase === 'pending'}
			<div class="spinner" aria-hidden="true"></div>
		{:else if phase === 'success' && result?.already_verified}
			<!-- A re-opened link: the workspace exists and nothing was re-issued,
			     so the way on is its sign-in page. -->
			{#if signInUrl}
				<a class="primary" href={signInUrl}>{m('auth.verify.goToSignIn')}</a>
			{/if}
		{:else if phase === 'success' && result}
			<ol class="steps">
				<li>{m('auth.verify.step1')}</li>
				<li>{m('auth.verify.step2')}</li>
				<li>{m('auth.verify.step3')}</li>
			</ol>
			{#if result.set_password_url}
				<!-- A full navigation to the tenant's own host; the link is built by
				     the server, never assembled here. -->
				<a class="primary" href={result.set_password_url}>{m('auth.verify.setPassword')}</a>
				<p class="note">{m('auth.verify.linkNote')}</p>
			{:else}
				<p class="note">{m('auth.verify.noLink')}</p>
			{/if}
		{:else if phase === 'expired'}
			<!-- The 410 names no workspace (that would make it an oracle for which
			     links once existed), so the way in for someone who did finish is
			     their workspace's own address, which their welcome email holds. -->
			<a class="primary" href="/signup">{m('auth.verify.startOver')}</a>
			<p class="note">{m('auth.verify.expiredSignInHint')}</p>
		{:else}
			<p class="error">{errorMessage}</p>
			{#if existingWorkspaceUrl}
				<a class="primary" href={existingWorkspaceUrl}>{m('auth.login.goToOwnWorkspace')}</a>
			{/if}
			<a class="start-over" href="/signup">{m('auth.verify.startOver')}</a>
		{/if}
	</div>
</AuthShell>

<style>
	/* Heading, error banner and entrance come from AuthShell. */
	.verify {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	.head {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 10px;
	}
	.sub {
		margin: 0;
		font-size: 0.93rem;
		line-height: 1.55;
		color: var(--text-muted);
	}
	.sub strong {
		color: var(--text);
	}
	.done-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 56px;
		height: 56px;
		border-radius: 16px;
		border: 1px solid color-mix(in srgb, var(--success) 40%, transparent);
		background: var(--success-tint);
		color: var(--success-on-tint);
		font-size: 30px;
		animation: pop 0.6s cubic-bezier(0.34, 1.56, 0.64, 1) backwards;
	}
	@keyframes pop {
		from { transform: scale(0.6); opacity: 0; }
		to { transform: scale(1); opacity: 1; }
	}
	.steps {
		margin: 0;
		padding: 14px 16px 14px 34px;
		border-radius: 12px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.88rem;
		line-height: 1.7;
	}
	.primary {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-height: 48px;
		padding: 12px 18px;
		border-radius: 10px;
		background: var(--accent-strong);
		color: #fff;
		font-weight: 600;
		text-decoration: none;
		box-shadow: 0 14px 30px -14px color-mix(in srgb, var(--accent) 95%, transparent);
		transition: transform 0.15s, box-shadow 0.15s;
	}
	.primary:hover {
		transform: translateY(-1px);
	}
	.note {
		margin: 0;
		font-size: 0.85rem;
		line-height: 1.55;
		color: var(--text-muted);
	}
	.start-over {
		align-self: flex-start;
		color: var(--accent-on-tint);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.spinner {
		width: 26px;
		height: 26px;
		border: 3px solid var(--border);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin {
		to { transform: rotate(360deg); }
	}
</style>
