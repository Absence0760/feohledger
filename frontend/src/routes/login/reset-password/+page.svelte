<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { page } from '$app/state';
	import AuthCard from '#lib/components/auth/AuthCard.svelte';
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

	// Same complexity checklist `routes/change-password/+page.svelte` shows —
	// reused message keys so the two forms don't drift on wording.
	let strengthHints = $derived.by(() => {
		const hints: Array<{ label: string; ok: boolean }> = [];
		hints.push({ label: m('auth.changePassword.strength.length'), ok: newPassword.length >= 12 });
		hints.push({ label: m('auth.changePassword.strength.upper'), ok: /[A-Z]/.test(newPassword) });
		hints.push({ label: m('auth.changePassword.strength.lower'), ok: /[a-z]/.test(newPassword) });
		hints.push({ label: m('auth.changePassword.strength.digit'), ok: /[0-9]/.test(newPassword) });
		return hints;
	});

	let allStrong = $derived(strengthHints.every((h) => h.ok));

	let heading = $derived(
		setMode ? m('auth.resetPassword.setHeading') : m('auth.resetPassword.heading')
	);

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		if (newPassword !== confirmPassword) {
			error = m('auth.resetPassword.mismatch');
			return;
		}
		if (!allStrong) {
			return;
		}
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
			// never succeed. Anything else (a 422 weak password, a 429) rides the
			// backend's own detail text and leaves the form to retry.
			if (err instanceof ApiError && err.status === 400) {
				invalidOnSubmit = true;
				phase = 'invalid';
				// The focused submit button went with the form; land focus on the
				// explanation so it is announced (as the success branch does).
				await tick();
				invalidEl?.focus();
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
		<form onsubmit={handleSubmit}>
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

			<label>
				<span>{m('auth.resetPassword.newPassword')}</span>
				<input
					type="password"
					bind:value={newPassword}
					required
					autocomplete="new-password"
					minlength="12"
				/>
			</label>

			<ul class="strength">
				{#each strengthHints as hint}
					<li class:ok={hint.ok}>{hint.ok ? '✓' : '·'} {hint.label}</li>
				{/each}
			</ul>

			<label>
				<span>{m('auth.resetPassword.confirmPassword')}</span>
				<input
					type="password"
					bind:value={confirmPassword}
					required
					autocomplete="new-password"
					minlength="12"
				/>
			</label>

			<button type="submit" disabled={submitting || !allStrong || newPassword !== confirmPassword}>
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
	.strength {
		margin: -8px 0 0;
		padding: 0 0 0 4px;
		list-style: none;
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.strength li.ok {
		color: var(--success);
	}
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
