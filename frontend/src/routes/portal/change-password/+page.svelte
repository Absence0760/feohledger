<script lang="ts">
	import NewPasswordFields from '#lib/components/auth/NewPasswordFields.svelte';
	import PasswordInput from '#lib/components/auth/PasswordInput.svelte';
	import { ApiError } from '#lib/api.ts';
	import { portalAuth } from '#lib/stores/portalAuth.svelte.ts';
	import { goto } from '$app/navigation';
	import { tick } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let error = $state('');
	let loading = $state(false);
	let submitted = $state(false);
	let currentInput = $state<HTMLInputElement | null>(null);
	let newFields = $state<ReturnType<typeof NewPasswordFields> | null>(null);
	// The server's refusals of one field, shown beside it until it is edited:
	// a wrong current password (400), a new password it would not take (422,
	// `validate_password_complexity` — the same rules the employee forms check,
	// `api/portal_auth.py::portal_change_password`). Anything else is not about
	// a field and goes in the banner.
	let currentServerError = $state('');
	let newServerError = $state('');

	let currentError = $derived(
		submitted && currentPassword === ''
			? m('auth.changePassword.currentRequired')
			: currentServerError
	);

	async function handleSubmit(e: Event) {
		e.preventDefault();
		error = '';
		submitted = true;
		// Every field is marked at once; focus goes to the first bad one.
		const currentOk = currentPassword !== '';
		const newOk = newFields?.validate(currentOk) ?? false;
		if (!currentOk) currentInput?.focus();
		if (!currentOk || !newOk) return;
		loading = true;
		try {
			await portalAuth.changePassword(currentPassword, newPassword);
			goto('/portal/invoices');
		} catch (err) {
			if (err instanceof ApiError && err.status === 400) {
				currentServerError = m('auth.changePassword.currentIncorrect');
				await tick();
				currentInput?.focus();
			} else if (err instanceof ApiError && err.status === 422) {
				newServerError = err.message;
				await tick();
				newFields?.focusNew();
			} else {
				error = err instanceof Error ? err.message : m('portal.changePassword.failed');
			}
		} finally {
			loading = false;
		}
	}
</script>

<div class="page">
	<form class="card" onsubmit={handleSubmit} novalidate>
		<h1>{m('portal.changePassword.title')}</h1>
		<p class="hint">{m('portal.changePassword.hint')}</p>

		<div role="alert" aria-live="assertive">
			{#if error}<div class="error">{error}</div>{/if}
		</div>

		<!-- Password managers file a new password under the username field in
		     the same form (the pattern `routes/change-password` uses). A supplier
		     lands here with the temp their AP team issued, so this is the save
		     that matters. Hidden, read-only, out of the tab order and the
		     accessibility tree. -->
		<input
			type="email"
			name="username"
			autocomplete="username"
			value={portalAuth.user?.email ?? ''}
			readonly
			hidden
			tabindex="-1"
			aria-hidden="true"
		/>
		<PasswordInput
			id="portal-change-current"
			label={m('portal.changePassword.current')}
			bind:value={currentPassword}
			bind:input={currentInput}
			autocomplete="current-password"
			required
			error={currentError}
			oninput={() => (currentServerError = '')}
		/>
		<NewPasswordFields
			bind:this={newFields}
			idPrefix="portal-change"
			newLabel={m('portal.changePassword.new')}
			confirmLabel={m('portal.changePassword.confirm')}
			bind:newPassword
			bind:confirmPassword
			bind:serverError={newServerError}
		/>
		<!-- Enabled whatever is typed: a submit says what is wrong, beside the
		     field (issue #635 #14, the same as the employee forms). -->
		<button type="submit" disabled={loading}>
			{loading ? m('portal.changePassword.saving') : m('portal.changePassword.save')}
		</button>
	</form>
</div>

<style>
	.page {
		min-height: 100vh;
		display: grid;
		place-items: center;
		background: var(--bg);
	}
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 36px;
		width: min(420px, 92vw);
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	h1 {
		margin: 0;
		font-size: 1.2rem;
	}
	.hint {
		margin: -6px 0 4px;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.error {
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
		color: var(--danger);
		padding: 10px;
		border-radius: 4px;
		font-size: 0.85rem;
	}
	/* The fields are PasswordInput / NewPasswordFields, so their label and
	   input are those components' elements: reach them through `:global()`,
	   wrapped in `:where()` so the components' own rules (the room the
	   show-password toggle needs) still win on specificity. */
	.card :global(:where(.password-field label > span)) {
		font-size: 0.78rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	.card :global(:where(.password-field input:not([type='checkbox']):not([type='radio']))) {
		width: 100%;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		font-size: 0.9rem;
		color: var(--text);
		font-family: inherit;
	}
	.card :global(:where(.password-field input:not([type='checkbox']):not([type='radio']):focus)) {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
	}
	.card :global(:where(.password-field input[aria-invalid='true'])) {
		border-color: var(--danger);
	}
	button[type='submit'] {
		margin-top: 8px;
		padding: 10px;
		border-radius: 4px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-weight: 500;
		cursor: pointer;
	}
	button[type='submit']:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
</style>
