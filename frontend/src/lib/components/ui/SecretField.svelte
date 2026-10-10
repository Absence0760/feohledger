<script lang="ts">
	import { m } from '#lib/i18n/store.svelte.ts';

	/**
	 * A write-only credential input: "leave blank to keep", never pre-filled.
	 *
	 * For a secret the server stores but never returns — the ERP, payment and
	 * card credentials (`/api/organization/credentials`). The field is ALWAYS
	 * empty on load: `configured` says whether a value is stored, and the hint
	 * says so; typing replaces it, and the remove toggle (shown only when one is
	 * stored, and disabled while something is typed) clears it. The caller turns
	 * `value` + `clear` into the PUT body with `credentialUpdate`
	 * (`#lib/types/providerCredentials.ts`) and resets both after a save.
	 *
	 * The hint sits outside the `<label>` and is wired by `aria-describedby`, so
	 * it describes the control without becoming part of its accessible name.
	 */
	type Props = {
		/** Unique per page — the hint's id derives from it. */
		id: string;
		label: string;
		/** What the admin typed. Bindable; never a stored value. */
		value: string;
		/** Remove the stored value on save. Bindable. */
		clear?: boolean;
		/** Whether the server holds a value for this field. */
		configured: boolean;
		/** Placeholder when nothing is stored (format hint, e.g. `test_...`). */
		placeholder?: string;
		testId?: string;
		/** Must be filled to save (only meaningful while nothing is stored). */
		required?: boolean;
		/** A save found it missing: `aria-invalid` (WCAG 3.3.1). */
		invalid?: boolean;
		/** Ids of further descriptions (a field's help text), after the hint. */
		describedBy?: string;
		/** Fired on every keystroke (e.g. to drop an `invalid` mark). */
		oninput?: () => void;
	};

	let {
		id,
		label,
		value = $bindable(''),
		clear = $bindable(false),
		configured,
		placeholder = '',
		testId,
		required = false,
		invalid = false,
		describedBy,
		oninput
	}: Props = $props();

	const hintId = $derived(`${id}-hint`);
	const typed = $derived(value.trim() !== '');
</script>

<div class="secret-field">
	<label>
		<span>{label}</span>
		<input
			{id}
			type="password"
			bind:value
			placeholder={configured ? m('org.secret.keepPlaceholder') : placeholder}
			autocomplete="new-password"
			spellcheck="false"
			aria-describedby={describedBy ? `${hintId} ${describedBy}` : hintId}
			aria-invalid={invalid ? 'true' : undefined}
			required={required && !configured}
			{oninput}
			data-testid={testId}
			data-secret-saved={String(configured)}
		/>
	</label>
	<p class="secret-hint" id={hintId}>
		{configured ? m('org.secret.configured') : m('org.secret.notConfigured')}
	</p>
	{#if configured}
		<label class="secret-clear">
			<input
				type="checkbox"
				bind:checked={clear}
				disabled={typed}
				data-testid={testId ? `${testId}-clear` : undefined}
			/>
			<span>{m('org.secret.clear')}</span>
		</label>
	{/if}
</div>

<style>
	.secret-field {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	label > span:first-child {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	input[type='password'] {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		font-family: inherit;
		width: 100%;
		box-sizing: border-box;
	}

	input[type='password']:focus {
		outline: none;
		border-color: var(--accent);
	}

	.secret-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 6px 0 0;
		line-height: 1.5;
	}

	label.secret-clear {
		flex-direction: row;
		align-items: center;
		gap: 10px;
		margin-top: 6px;
		cursor: pointer;
	}

	label.secret-clear > span:first-of-type {
		font-size: 0.85rem;
		font-weight: 400;
		color: var(--text);
		text-transform: none;
		letter-spacing: normal;
	}
</style>
