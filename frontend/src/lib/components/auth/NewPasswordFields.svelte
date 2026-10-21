<script lang="ts">
	import PasswordInput from '#lib/components/auth/PasswordInput.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import {
		PASSWORD_MIN_LENGTH,
		confirmationProblem,
		meetsPasswordRules,
		passwordRuleStatus,
		type PasswordRule
	} from '#lib/utils/passwordRules.ts';

	/**
	 * "New password" + its rules checklist + "Confirm new password", for the
	 * change-password and reset/set-password forms.
	 *
	 * The form's Submit stays enabled; the form calls `validate()` on submit,
	 * which marks each bad field (`aria-invalid`, its message beside it via
	 * `aria-describedby`) and focuses the first one. A disabled Submit gave no
	 * reason at all, and meant the mismatch message could never appear.
	 *
	 * The mismatch message also shows before a submit, once the user has
	 * finished typing the confirmation (it has lost focus non-empty), and from
	 * then on follows each keystroke so it clears the moment the two agree.
	 */
	type Props = {
		/** Prefix for the element ids; unique per page. */
		idPrefix: string;
		newLabel: string;
		confirmLabel: string;
		newPassword: string;
		confirmPassword: string;
		/**
		 * The server's refusal of the new password (a 422), shown on the new
		 * field until it is edited. The page sets it and calls `focusNew()`.
		 */
		serverError?: string;
	};

	let {
		idPrefix,
		newLabel,
		confirmLabel,
		newPassword = $bindable(''),
		confirmPassword = $bindable(''),
		serverError = $bindable('')
	}: Props = $props();

	const RULE_LABEL: Record<PasswordRule, MessageKey> = {
		length: 'auth.changePassword.strength.length',
		upper: 'auth.changePassword.strength.upper',
		lower: 'auth.changePassword.strength.lower',
		digit: 'auth.changePassword.strength.digit'
	};

	let newInput = $state<HTMLInputElement | null>(null);
	let confirmInput = $state<HTMLInputElement | null>(null);
	let submitted = $state(false);
	let confirmTouched = $state(false);

	const rules = $derived(passwordRuleStatus(newPassword));
	const newError = $derived(
		submitted && !meetsPasswordRules(newPassword) ? m('auth.changePassword.tooWeak') : serverError
	);
	const confirmError = $derived.by(() => {
		const problem = confirmationProblem(newPassword, confirmPassword);
		if (problem === 'mismatch' && (submitted || confirmTouched)) {
			return m('auth.changePassword.mismatch');
		}
		if (problem === 'empty' && submitted) return m('auth.password.confirmRequired');
		return '';
	});

	const ids = $derived({
		newField: `${idPrefix}-new`,
		rulesLead: `${idPrefix}-rules-lead`,
		rules: `${idPrefix}-rules`,
		confirmField: `${idPrefix}-confirm`
	});

	/**
	 * Check both fields and show what is wrong. Focuses the first bad field
	 * unless `focus` is false (the page already focused an earlier one).
	 * Returns whether the form may submit.
	 */
	export function validate(focus = true): boolean {
		submitted = true;
		if (!meetsPasswordRules(newPassword)) {
			if (focus) newInput?.focus();
			return false;
		}
		if (confirmationProblem(newPassword, confirmPassword) !== null) {
			if (focus) confirmInput?.focus();
			return false;
		}
		return true;
	}

	/** Put focus on the new-password field — after a server refusal of it. */
	export function focusNew(): void {
		newInput?.focus();
	}
</script>

<div class="new-password-fields">
	<div class="field">
		<PasswordInput
			id={ids.newField}
			label={newLabel}
			bind:value={newPassword}
			bind:input={newInput}
			autocomplete="new-password"
			required
			minlength={PASSWORD_MIN_LENGTH}
			error={newError}
			describedBy={`${ids.rulesLead} ${ids.rules}`}
			oninput={() => (serverError = '')}
		/>
		<p class="visually-hidden" id={ids.rulesLead}>{m('auth.password.rulesLead')}</p>
		<ul class="strength" id={ids.rules}>
			{#each rules as { rule, ok } (rule)}
				<li class:ok>
					<span aria-hidden="true">{ok ? '✓' : '·'}</span>
					{m(RULE_LABEL[rule])}<span class="visually-hidden">
						({ok ? m('auth.password.ruleMet') : m('auth.password.ruleUnmet')})</span
					>
				</li>
			{/each}
		</ul>
	</div>

	<div class="field">
		<PasswordInput
			id={ids.confirmField}
			label={confirmLabel}
			bind:value={confirmPassword}
			bind:input={confirmInput}
			autocomplete="new-password"
			required
			minlength={PASSWORD_MIN_LENGTH}
			error={confirmError}
			announce
			onblur={() => {
				if (confirmPassword !== '') confirmTouched = true;
			}}
		/>
	</div>
</div>

<style>
	.new-password-fields {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	.field {
		display: flex;
		flex-direction: column;
	}
	.strength {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 6px 12px;
		margin: 8px 0 0;
		padding: 0;
		list-style: none;
		font-size: 0.8rem;
		color: var(--text-muted);
	}
	.strength li {
		transition: color 0.2s;
	}
	/* --success is calibrated against --bg (6.17:1); a hint turning green is
	   the state change here. */
	.strength li.ok {
		color: var(--success);
	}
	@media (max-width: 420px) {
		.strength {
			grid-template-columns: 1fr;
		}
	}
</style>
