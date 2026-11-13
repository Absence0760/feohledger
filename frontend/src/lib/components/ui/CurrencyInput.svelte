<script lang="ts">
	/**
	 * A required ISO 4217 currency field with free entry.
	 *
	 * Any three-letter code is accepted; the `<datalist>` only suggests (the
	 * caller's `preferred` code first, the common shortlist, then the rest of
	 * ISO 4217). A `<select>` over the shortlist used to be the picker, and it
	 * could not express a PLN or HKD statement at all (decisions §377
	 * follow-up). Blank or malformed is refused before submit through the
	 * input's own validity, with the reason in words tied to the field
	 * (WCAG 3.3.1), and the value is uppercased as it is typed.
	 *
	 * The field follows `preferred` until the user types in it: the org's
	 * currency usually lands after the modal mounts, and seeding only once at
	 * mount left a GBP organization's statement on the platform default.
	 *
	 * `touched` is bindable so a caller that reopens the field on a record
	 * that already holds a currency (an edit) can say so: a touched field is
	 * never re-seeded, and the caller can reset it for each new record. A
	 * caller that remounts the field (a branch that swaps it out and back)
	 * keeps the user's choice the same way.
	 *
	 * Sits inside the caller's `<label>`, which names it. `describedby` names
	 * a hint of the caller's own; the error, when shown, is added after it.
	 */
	import { untrack } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import {
		currencyCodeProblem,
		currencySuggestions,
		seededCurrency,
		upperAsciiLetters
	} from '#lib/utils/currencyCode.ts';

	let {
		value = $bindable(''),
		preferred = null,
		touched = $bindable(false),
		disabled = false,
		describedby,
		testid
	}: {
		value?: string;
		/** The organization's currency (`orgCurrency.currency`), or `null` while unresolved. */
		preferred?: string | null;
		/** True once the field holds a user's (or a loaded record's) choice; never re-seeded then. */
		touched?: boolean;
		disabled?: boolean;
		/** Id of a caller-owned hint describing the field. */
		describedby?: string;
		testid?: string;
	} = $props();

	const uid = $props.id();
	const listId = `${uid}-list`;
	const errorId = `${uid}-error`;

	let input = $state<HTMLInputElement | null>(null);

	// Depends on `preferred` and `touched` only: re-running on its own write
	// to `value` would be a loop with nothing new to say.
	$effect(() => {
		const p = preferred;
		const t = touched;
		untrack(() => {
			const next = seededCurrency(value, p, t);
			if (next !== value) value = next;
		});
	});

	const suggestions = $derived(currencySuggestions(preferred));
	const problem = $derived(currencyCodeProblem(value));
	const message = $derived(
		problem === 'required'
			? m('common.currencyCode.required')
			: problem === 'invalid'
				? m('common.currencyCode.invalid', { value: value.trim() })
				: ''
	);
	// Shown once the user has typed: a field seeded with a valid code never
	// starts out red, and native validation still blocks a submit regardless.
	const showError = $derived(touched && problem !== null);
	const describedBy = $derived(
		[describedby, showError ? errorId : null].filter(Boolean).join(' ') || undefined
	);

	$effect(() => {
		input?.setCustomValidity(message);
	});

	function onInput(e: Event) {
		touched = true;
		const el = e.currentTarget as HTMLInputElement;
		const upper = upperAsciiLetters(el.value);
		if (upper !== el.value) el.value = upper;
		value = upper;
	}
</script>

<input
	bind:this={input}
	type="text"
	{value}
	oninput={onInput}
	list={listId}
	maxlength="3"
	autocomplete="off"
	autocapitalize="characters"
	spellcheck="false"
	required
	{disabled}
	aria-invalid={showError ? 'true' : undefined}
	aria-describedby={describedBy}
	data-testid={testid}
/>
<datalist id={listId}>
	{#each suggestions as code (code)}
		<option value={code}></option>
	{/each}
</datalist>
{#if showError}
	<small id={errorId} class="currency-error" data-testid={testid ? `${testid}-error` : undefined}>
		{message}
	</small>
{/if}

<style>
	input {
		text-transform: uppercase;
		max-width: 8rem;
	}
	.currency-error {
		display: block;
		margin-top: 4px;
		font-size: 0.72rem;
		line-height: 1.35;
		color: var(--danger-on-tint);
	}
</style>
