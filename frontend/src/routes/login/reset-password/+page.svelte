<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { page } from '$app/state';
	import AuthCard from '#lib/components/auth/AuthCard.svelte';
	import NewPasswordFields from '#lib/components/auth/NewPasswordFields.svelte';
	import { api, ApiError } from '#lib/api.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { signInHref } from '#lib/utils/prefillEmail.ts';

	// Two links land here, both redeeming the same single-use reset token:
	// forgot-password's, and the set-password link a self-service signup admin
	// gets instead of a temporary password (`mode=set`, decisions §283). The
	// mode changes copy only.
	let token = $state('');
	let setMode = $state(false);
	// `checking` until the token is read; `invalid` when it is unknown, used or
	// expired; `ready` shows the form; `done` the confirmation.
	// `unavailable` when the check itself failed (rate limit, outage) — the
	// link may well be fine.
	let phase = $state<'checking' | 'invalid' | 'unavailable' | 'ready' | 'done'>('checking');
	let accountEmail = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let error = $state('');
	let submitting = $state(false);
	let newFields = $state<ReturnType<typeof NewPasswordFields> | null>(null);
	// A new password the server refused (422), shown beside its field.
	let newServerError = $state('');
	let successEl = $state<HTMLParagraphElement | null>(null);
	let invalidEl = $state<HTMLDivElement | null>(null);
	// The link died on submit rather than on load: the explanation is then
	// focused (the submit button went with the form), and focus is what
	// announces it — a `role="alert"` as well would read it twice.
	let invalidOnSubmit = $state(false);

	onMount(async () => {
		token = page.url.searchParams.get('token') ?? '';
		setMode = page.url.searchParams.get('mode') === 'set';
		if (!token) {
			phase = 'invalid';
			return;
		}
		// A read, never a redemption: a mail scanner that opens the link, or a
		// reload, leaves the token usable. Only submitting a password spends it.
		await checkToken();
	});

	async function checkToken() {
		phase = 'checking';
		error = '';
		try {
			const res = await api.post<{ email: string }>('/api/auth/reset-password/check', { token });
			accountEmail = res.email;
			phase = 'ready';
		} catch (err) {
			// Only a 400 means the link itself is dead. A rate limit or an outage
			// says nothing about the link, so telling the visitor to request a
			// new one would be wrong: say what happened and let them retry.
			if (err instanceof ApiError && err.status === 400) {
				invalidOnSubmit = true;
				phase = 'invalid';
			} else {
				error = err instanceof Error ? err.message : m('auth.resetPassword.checkFailed');
				phase = 'unavailable';
			}
		}
	}

	let heading = $derived(
		setMode ? m('auth.resetPassword.setHeading') : m('auth.resetPassword.heading')
	);

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		// Marks each bad field beside itself and focuses the first one — the
		// same rules and messages as /change-password (NewPasswordFields).
		if (!newFields?.validate()) return;
		submitting = true;
		try {
			await api.post('/api/auth/reset-password', { token, new_password: newPassword });
			phase = 'done';
			// The submit button had focus, and it has just been removed from the
			// DOM along with the form — which drops focus to <body> and leaves a
			// screen reader with nothing to say. Land it on the confirmation
			// instead, the way /signup does: that is announced, and it puts
			// keyboard users at the top of the new content (WCAG 2.4.3, 4.1.3).
			await tick();
			successEl?.focus();
		} catch (err) {
			// A 400 is the link refusing (spent, expired or its account since
			// deactivated, folded together by design — services/password_reset.py),
			// the same status the load-time check answers. A token that died after
			// the page loaded is the same dead link, so it gets the same screen and
			// its way forward, a fresh link — not a banner over a form that can
			// never succeed. A 422 is the new password refused, and goes beside
			// that field; anything else (a 429) shows the error's message — the
			// coded `rate_limited` refusal, localized by `api/codedRefusals.ts` —
			// in the banner and leaves the form to retry.
			if (err instanceof ApiError && err.status === 400) {
				invalidOnSubmit = true;
				phase = 'invalid';
				// The focused submit button went with the form; land focus on the
				// explanation so it is announced (as the success branch does).
				await tick();
				invalidEl?.focus();
			} else if (err instanceof ApiError && err.status === 422) {
				newServerError = err.message;
				await tick();
				newFields?.focusNew();
			} else {
				error = err instanceof Error ? err.message : m('auth.resetPassword.invalidToken');
			}
		} finally {
			submitting = false;
		}
	}
</script>

<svelte:head>
	<title>{heading} — FeohLedger</title>
</svelte:head>

<AuthCard
	{heading}
	subtitle={phase === 'ready'
		? setMode
			? m('auth.resetPassword.setSubtitle')
			: m('auth.resetPassword.subtitle')
		: undefined}
	{error}
>
	{#if phase === 'done'}
		<p class="success" tabindex="-1" bind:this={successEl}>
			{setMode ? m('auth.resetPassword.setSuccess') : m('auth.resetPassword.success')}
		</p>
		<a class="link-btn" href={signInHref(accountEmail)}>{m('auth.resetPassword.goToLogin')}</a>
	{:else if phase === 'ready'}
		<form onsubmit={handleSubmit} novalidate>
			<!-- Password managers file a new password under the username field in
			     the same form; without one they save it against no account, or not
			     at all. Hidden, read-only, out of the tab order and the
			     accessibility tree — same as routes/change-password. -->
			<input
				type="email"
				name="username"
				autocomplete="username"
				value={accountEmail}
				readonly
				hidden
				tabindex="-1"
				aria-hidden="true"
			/>

			<NewPasswordFields
				bind:this={newFields}
				idPrefix="reset"
				newLabel={m('auth.resetPassword.newPassword')}
				confirmLabel={m('auth.resetPassword.confirmPassword')}
				bind:newPassword
				bind:confirmPassword
				bind:serverError={newServerError}
			/>

			<!-- Enabled whatever is typed: a submit says what is wrong, beside
			     the field (issue #635 #14). -->
			<button type="submit" disabled={submitting}>
				{submitting
					? m('auth.resetPassword.submitting')
					: setMode
						? m('auth.resetPassword.setSubmit')
						: m('auth.resetPassword.submit')}
			</button>
		</form>
	{:else if phase === 'invalid'}
		<!-- One focus target for the explanation AND the set-mode hint, so a
		     screen reader hears what to do next, not only what went wrong. -->
		<div
			class="invalid"
			role={invalidOnSubmit ? undefined : 'alert'}
			tabindex="-1"
			bind:this={invalidEl}
		>
			<p class="explain">
				{token ? m('auth.resetPassword.invalidToken') : m('auth.resetPassword.missingToken')}
			</p>
			{#if setMode}
				<p class="explain">{m('auth.resetPassword.setExpiredHint')}</p>
			{/if}
		</div>
		<a class="link-btn" href="/login/forgot-password">{m('auth.forgotPassword.heading')}</a>
	{:else if phase === 'unavailable'}
		<!-- The failure itself is in AuthCard's error banner. -->
		<button type="button" onclick={checkToken}>{m('auth.resetPassword.retry')}</button>
	{:else}
		<p class="explain" role="status">{m('auth.resetPassword.checking')}</p>
	{/if}
</AuthCard>

<style>
	.invalid {
		display: flex;
		flex-direction: column;
		gap: inherit;
	}
	.explain {
		margin: 0;
		font-size: 0.9rem;
		line-height: 1.55;
		color: var(--text-muted);
	}
</style>
