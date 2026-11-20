<script lang="ts">
	import { apiErrorMessage } from '#lib/utils/apiError.ts';
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import AuthCard from '#lib/components/auth/AuthCard.svelte';
	import Spinner from '#lib/components/ui/Spinner.svelte';
	import { ApiError, setToken } from '#lib/api.ts';
	import { localizeApiDetail } from '#lib/api/codedRefusals.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { brand } from '#lib/stores/brand.svelte.ts';
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
	// this page has already decided what went wrong. A coded refusal from the
	// backend is kept as its `detail` and localized HERE, reactively, not
	// frozen as the text the transport rendered: the exchange runs on mount,
	// and its answer can land before the preloaded `refusal` slice has, when
	// the transport could only fall back to the server's sentence
	// (`api/resolvedOnly.ts`). Re-derived, it states the refusal from its code
	// as soon as the slice arrives, and follows a locale switch.
	let messageKey = $state<MessageKey | null>(null);
	let refusalDetail = $state<unknown>(undefined);
	let serverMessage = $state('');
	const message = $derived(
		messageKey
			? m(messageKey)
			: ((refusalDetail === undefined ? null : localizeApiDetail(refusalDetail, m)) ?? serverMessage)
	);
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
			// The server's own refusal when it sent one for the user (a
			// coded one arrives localized); never a network or JS message.
			const text = apiErrorMessage(e, '');
			if (text) {
				serverMessage = text;
				if (e instanceof ApiError) refusalDetail = e.detail;
			} else messageKey = 'auth.callback.failed';
		}
	});
</script>

<svelte:head>
	<title>{m('auth.callback.pageTitle', { product: brand.productName })}</title>
</svelte:head>

<AuthCard
	heading={phase === 'working' ? m('auth.callback.signingIn') : m('auth.callback.failedHeading')}
	error={phase === 'error' ? message : undefined}
>
	{#if phase === 'working'}
		<!-- The heading is not announced when it is the only thing on screen
		     that says what is happening; this is. -->
		<Spinner size={32} label={m('auth.callback.signingIn')} hideLabel />
	{:else}
		{#if idpCode}
			<p class="hint">{m('auth.callback.idpCode', { code: idpCode })}</p>
		{/if}
		<a class="link-btn" href={withNext('/login', next)}>{m('auth.callback.backToSignIn')}</a>
	{/if}
</AuthCard>

