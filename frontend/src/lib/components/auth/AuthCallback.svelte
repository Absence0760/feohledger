<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import AuthCard from '#lib/components/auth/AuthCard.svelte';
	import { setToken } from '#lib/api.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { postLoginTarget, takeNextForSso, withNext } from '#lib/utils/postLoginRedirect.ts';
	import { idpErrorCodeForDisplay, idpErrorKey } from '#lib/utils/idpCallbackError.ts';

	/**
	 * The page an identity provider sends the browser back to — the OIDC
	 * `/login/sso-callback` and the SAML bridge `/login/saml-callback` — which
	 * trades what the URL carries for a session and moves on.
	 *
	 * The two pages were copies of each other with their own card markup; they
	 * now differ only in what they read from the URL and which endpoint they
	 * call, and render inside `AuthShell` (through `AuthCard`) like every other
	 * sign-in screen (issue #635 finding 36).
	 *
	 * An IdP refusal (`?error=`) is stated as a sentence, never as the protocol
	 * code (`#lib/utils/idpCallbackError.ts`); the code itself stays underneath
	 * as support detail when it looks like one. A refusal from OUR backend
	 * arrives already localized from its code (`api/codedRefusals.ts`).
	 */
	interface Props {
		/** Query parameters the callback cannot proceed without. */
		required: readonly string[];
		/** What to say when one of them is missing. */
		missingKey: MessageKey;
		/** Trade the callback's parameters for a session. */
		exchange: (
			params: URLSearchParams
		) => Promise<{ access_token: string; must_change_password: boolean }>;
	}

	let { required, missingKey, exchange }: Props = $props();

	let phase = $state<'working' | 'error'>('working');
	// Our own sentence is kept as its KEY and rendered through `m()`, so it
	// follows the catalogue: a non-English one loads lazily and can land after
	// this page has already decided what went wrong. A refusal from the
	// backend arrives as text, localized by the transport.
	let messageKey = $state<MessageKey | null>(null);
	let serverMessage = $state('');
	const message = $derived(messageKey ? m(messageKey) : serverMessage);
	// The IdP's own error code, shown small under the sentence for support.
	let idpCode = $state<string | null>(null);
	// The page the user set out for, stashed by /login before the IdP round
	// trip. Taken (and cleared) up front, success or not, so it can never
	// steer a later sign-in in this tab.
	let next = $state<string | null>(null);

	onMount(async () => {
		next = takeNextForSso();
		const params = new URLSearchParams(page.url.search);
		const err = params.get('error');

		if (err) {
			phase = 'error';
			messageKey = idpErrorKey(err);
			idpCode = idpErrorCodeForDisplay(err);
			return;
		}
		if (required.some((name) => !params.get(name))) {
			phase = 'error';
			messageKey = missingKey;
			return;
		}

		try {
			const resp = await exchange(params);
			setToken(resp.access_token);
			await auth.fetchUser();
			goto(
				resp.must_change_password ? withNext('/change-password', next) : postLoginTarget(next)
			);
		} catch (e) {
			phase = 'error';
			if (e instanceof Error && e.message) serverMessage = e.message;
			else messageKey = 'auth.callback.failed';
		}
	});
</script>

<svelte:head>
	<title>{m('auth.callback.pageTitle')}</title>
</svelte:head>

<AuthCard
	heading={phase === 'working' ? m('auth.callback.signingIn') : m('auth.callback.failedHeading')}
	error={phase === 'error' ? message : undefined}
>
	{#if phase === 'working'}
		<!-- The heading is not announced when it is the only thing on screen
		     that says what is happening; this is. -->
		<div class="working" role="status">
			<span class="spinner" aria-hidden="true"></span>
			<span class="visually-hidden">{m('auth.callback.signingIn')}</span>
		</div>
	{:else}
		{#if idpCode}
			<p class="hint">{m('auth.callback.idpCode', { code: idpCode })}</p>
		{/if}
		<a class="link-btn" href={withNext('/login', next)}>{m('auth.callback.backToSignIn')}</a>
	{/if}
</AuthCard>

<style>
	.spinner {
		display: block;
		width: 26px;
		height: 26px;
		border: 3px solid var(--border);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
</style>
