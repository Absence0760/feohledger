<script lang="ts">
	import IconShow from '~icons/material-symbols/visibility-outline';
	import IconHide from '~icons/material-symbols/visibility-off-outline';
	import { m } from '#lib/i18n/store.svelte.ts';

	/**
	 * A labelled password field with a show-password toggle — the one every
	 * employee auth form uses (login, change-password, reset/set-password).
	 *
	 * The toggle is a real `<button>` with `aria-pressed` and a CONSTANT
	 * accessible name ("Show password"): the pressed state says whether the
	 * text is showing, so the name never flips under a screen reader mid-task.
	 * It sits outside the `<label>`, so it is never part of the field's name.
	 *
	 * Password managers keep working: `autocomplete` and `name` stay on the
	 * one input whatever its type, and the field goes back to `type=password`
	 * when its form submits — the moment a manager inspects the form to offer a
	 * save.
	 */
	type Props = {
		/** Unique per page; the label points at it. */
		id: string;
		label: string;
		value: string;
		/** `current-password` for a sign-in / the password being replaced, `new-password` for a new one. */
		autocomplete: 'current-password' | 'new-password';
		name?: string;
		required?: boolean;
		/** A hint for password generators only; the form validates the length itself. */
		minlength?: number;
		/**
		 * What the form found wrong with this field, shown beside it and
		 * read with it; non-empty also sets `aria-invalid` (WCAG 3.3.1).
		 */
		error?: string;
		/** Ids of further help text (a rules checklist), read after the error. */
		describedBy?: string;
		/**
		 * Announce the error as it appears (a polite live region). Only for an
		 * error that shows while focus is ELSEWHERE — the confirmation's
		 * mismatch, shown on blur. An error found on submit is read by moving
		 * focus to the field (its `aria-describedby`); announcing it as well
		 * reads it twice and queues every other field's behind it.
		 */
		announce?: boolean;
		/** The input element, for a form that focuses its first bad field. */
		input?: HTMLInputElement | null;
		onblur?: () => void;
		oninput?: () => void;
	};

	let {
		id,
		label,
		value = $bindable(''),
		autocomplete,
		name,
		required = false,
		minlength,
		error = '',
		describedBy,
		announce = false,
		input = $bindable(null),
		onblur,
		oninput
	}: Props = $props();

	let shown = $state(false);
	const errorId = $derived(`${id}-error`);

	// Hide again on submit, so a password manager sees a password field when
	// it reads the form, and the text is not left on screen behind whatever
	// the page shows next.
	$effect(() => {
		const form = input?.form;
		if (!form) return;
		const hide = () => {
			shown = false;
		};
		form.addEventListener('submit', hide, { capture: true });
		return () => form.removeEventListener('submit', hide, { capture: true });
	});
</script>

<div class="password-field">
	<label for={id}><span>{label}</span></label>
	<div class="control">
		<input
			{id}
			{name}
			type={shown ? 'text' : 'password'}
			bind:value
			bind:this={input}
			{autocomplete}
			{required}
			{minlength}
			autocapitalize="off"
			spellcheck="false"
			aria-invalid={error ? 'true' : undefined}
			aria-describedby={describedBy ? `${errorId} ${describedBy}` : errorId}
			{onblur}
			{oninput}
		/>
		<button
			type="button"
			class="reveal"
			aria-label={m('auth.password.show')}
			aria-pressed={shown}
			aria-controls={id}
			onclick={() => (shown = !shown)}
		>
			{#if shown}
				<IconHide aria-hidden="true" />
			{:else}
				<IconShow aria-hidden="true" />
			{/if}
		</button>
	</div>
	<!-- Always in the tree, so that when it is a live region it is one before
	     it has anything to say (one inserted with its text is not announced).
	     Empty, it has no height and no margin. -->
	<p class="field-error" id={errorId} aria-live={announce ? 'polite' : undefined}>{error}</p>
</div>

<style>
	/* Field chrome (the input's border, focus ring, invalid border and the
	   label's type) comes from AuthShell's `:where()` rules; this adds the
	   toggle's room inside the field and the error line under it. */
	.password-field {
		display: flex;
		flex-direction: column;
	}
	.field-error {
		margin: 0;
		font-size: 0.82rem;
		line-height: 1.4;
		color: var(--danger);
	}
	.field-error:not(:empty) {
		margin-top: 6px;
	}
	.control {
		position: relative;
		margin-top: 6px;
	}
	.control input {
		padding-right: 52px;
	}
	/* Edge draws its own reveal eye inside a password field; this toggle
	   replaces it, and two eyes stacked in one field is one too many. */
	.control input::-ms-reveal,
	.control input::-ms-clear {
		display: none;
	}
	/* 40x40, comfortably over the 24px minimum (WCAG 2.5.8), centred in the
	   46px field. */
	.reveal {
		position: absolute;
		top: 50%;
		right: 3px;
		transform: translateY(-50%);
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		padding: 0;
		border: none;
		border-radius: 8px;
		background: transparent;
		color: var(--text-muted);
		font-size: 1.25rem;
		cursor: pointer;
	}
	.reveal:hover,
	.reveal[aria-pressed='true'] {
		color: var(--text);
	}
	.reveal:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
	}
</style>
