<script lang="ts">
	import AuthShell from '#lib/components/auth/AuthShell.svelte';
	import NewPasswordFields from '#lib/components/auth/NewPasswordFields.svelte';
	import PasswordInput from '#lib/components/auth/PasswordInput.svelte';
	import BrandMark from '#lib/components/ui/BrandMark.svelte';
	import { api, ApiError } from '#lib/api.ts';
	import { tick } from 'svelte';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { brand } from '#lib/stores/brand.svelte.ts';
	import { nextFromParams, postLoginTarget } from '#lib/utils/postLoginRedirect.ts';

	interface UserResponse {
		id: string;
		email: string;
		full_name: string;
		must_change_password: boolean;
		roles: string[];
	}

	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let error = $state('');
	let submitting = $state(false);
	let submitted = $state(false);
	let currentInput = $state<HTMLInputElement | null>(null);
	let newFields = $state<ReturnType<typeof NewPasswordFields> | null>(null);
	// The server's refusals of one field, shown beside it until it is edited:
	// a wrong current/temporary password (400), a new password it would not
	// take (422). Anything else is not about a field and goes in the banner.
	let currentServerError = $state('');
	let newServerError = $state('');

	// A forced change (an admin-created account, or an admin reset) is signed
	// in with the temporary password from the email — the user has never had
	// a "current" password, so the field says which one it wants.
	let forced = $derived(auth.user?.must_change_password ?? false);
	let currentError = $derived(
		submitted && currentPassword === ''
			? forced
				? m('auth.changePassword.temporaryRequired')
				: m('auth.changePassword.currentRequired')
			: currentServerError
	);

	async function onSubmit(e: Event) {
		e.preventDefault();
		error = '';
		submitted = true;
		// Every field is marked at once; focus goes to the first bad one.
		const currentOk = currentPassword !== '';
		const newOk = newFields?.validate(currentOk) ?? false;
		if (!currentOk) currentInput?.focus();
		if (!currentOk || !newOk) return;
		submitting = true;
		try {
			await api.post<UserResponse>('/api/auth/change-password', {
				current_password: currentPassword,
				new_password: newPassword,
			});
			await auth.fetchUser();
			// The forced change after sign-in carries the page the user was
			// opening (the root layout passes it as `?next=`).
			goto(postLoginTarget(nextFromParams(page.url.searchParams)));
		} catch (err) {
			// The one 400 this endpoint raises is a wrong current password; a 422
			// is the new password refused (`api/auth.py::change_password`).
			if (err instanceof ApiError && err.status === 400) {
				currentServerError = forced
					? m('auth.changePassword.temporaryIncorrect')
					: m('auth.changePassword.currentIncorrect');
				await tick();
				currentInput?.focus();
			} else if (err instanceof ApiError && err.status === 422) {
				newServerError = err.message;
				await tick();
				newFields?.focusNew();
			} else {
				error = err instanceof Error ? err.message : m('auth.changePassword.failed');
			}
		} finally {
			submitting = false;
		}
	}

	async function onLogout() {
		await auth.logout();
	}
</script>

<svelte:head>
	<title>{m('auth.changePassword.pageTitle', { product: brand.productName })}</title>
</svelte:head>

<AuthShell>
	<form class="change-form" onsubmit={onSubmit} novalidate>
		<div class="head">
			<BrandMark size={40} />
			<h1>{m('auth.changePassword.heading')}</h1>
			<p class="sub">
				{#if auth.user?.must_change_password}
					{m('auth.changePassword.subForced')}
				{:else}
					{m('auth.changePassword.subVoluntary')}
				{/if}
			</p>
		</div>

		<div role="alert" aria-live="assertive">
			{#if error}
				<div class="error">{error}</div>
			{/if}
		</div>

		<!-- Password managers file a new password under the username field that
		     sits in the same form; without one they offer to save it against no
		     account, or not at all. A first sign-in lands here with a temporary
		     password, so this is the save that matters. Hidden, read-only, and
		     out of the tab order and the accessibility tree. -->
		<input
			type="email"
			name="username"
			autocomplete="username"
			value={auth.user?.email ?? ''}
			readonly
			hidden
			tabindex="-1"
			aria-hidden="true"
		/>

		<PasswordInput
			id="change-current"
			label={forced
				? m('auth.changePassword.temporaryPassword')
				: m('auth.changePassword.currentPassword')}
			bind:value={currentPassword}
			bind:input={currentInput}
			autocomplete="current-password"
			required
			error={currentError}
			oninput={() => (currentServerError = '')}
		/>

		<NewPasswordFields
			bind:this={newFields}
			idPrefix="change"
			newLabel={m('auth.changePassword.newPassword')}
			confirmLabel={m('auth.changePassword.confirmPassword')}
			bind:newPassword
			bind:confirmPassword
			bind:serverError={newServerError}
		/>

		<!-- Enabled whatever is typed: a submit says what is wrong, beside the
		     field. A disabled button said nothing at all (issue #635 #14). -->
		<button type="submit" disabled={submitting}>
			{submitting ? m('auth.changePassword.submitting') : m('auth.changePassword.submit')}
		</button>

		<button type="button" class="secondary" onclick={onLogout}>{m('auth.changePassword.signOut')}</button>
	</form>
</AuthShell>

<style>
	/* Field chrome, the submit button, the error banner and the entrance come
	   from AuthShell; the new-password pair and its checklist from
	   NewPasswordFields. This is only the sign-out escape hatch. */
	.change-form {
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
	.sub {
		margin: 0;
		font-size: 0.93rem;
		line-height: 1.55;
		color: var(--text-muted);
	}
	.secondary {
		min-height: 46px;
		padding: 11px 16px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--text-muted);
		font-family: inherit;
		font-size: 0.93rem;
		cursor: pointer;
		transition: border-color 0.15s, color 0.15s;
	}
	.secondary:hover {
		border-color: var(--text-muted);
		color: var(--text);
	}
</style>
