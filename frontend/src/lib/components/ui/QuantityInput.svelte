<script lang="ts">
	/**
	 * A goods-quantity field: raw text in the reader's own decimal notation,
	 * checked by `utils/quantity.parseQuantity`, with the reason it is wrong
	 * said in words and tied to the input (WCAG 3.3.1 / 3.3.3) — not just an
	 * `aria-invalid` and a submit button that silently turns off.
	 *
	 * The value stays the text the user typed; callers read the dot-normalised
	 * figure through `parseQuantity` when they submit. `describedBy` lets a
	 * caller tie its own note (an over-receipt warning) to the same input.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import { decimalSeparator, parseQuantity } from '#lib/utils/quantity.ts';

	let {
		value = $bindable(''),
		ariaLabel,
		describedBy,
		required = false,
		testid,
		size = 'md'
	}: {
		value?: string;
		/** Omit when the field sits inside a `<label>` that names it. */
		ariaLabel?: string;
		describedBy?: string;
		required?: boolean;
		testid?: string;
		/** `sm` for a table cell. */
		size?: 'sm' | 'md';
	} = $props();

	const uid = $props.id();
	const errorId = `${uid}-error`;
	const invalid = $derived(parseQuantity(value).kind === 'invalid');
	const ids = $derived([invalid ? errorId : null, describedBy ?? null].filter(Boolean).join(' '));
</script>

<input
	type="text"
	inputmode="decimal"
	class:sm={size === 'sm'}
	bind:value
	aria-label={ariaLabel}
	aria-invalid={invalid ? 'true' : undefined}
	aria-describedby={ids || undefined}
	{required}
	data-testid={testid}
/>
{#if invalid}
	<small id={errorId} class="quantity-error" data-testid={testid ? `${testid}-error` : undefined}>
		{m('common.quantityInvalid', { separator: decimalSeparator() })}
	</small>
{/if}

<style>
	input.sm {
		width: 7rem;
		text-align: right;
	}
	.quantity-error {
		display: block;
		margin-top: 4px;
		font-size: 0.72rem;
		line-height: 1.35;
		color: var(--danger-on-tint);
		text-align: left;
	}
</style>
