<script lang="ts">
	import { formatMoney, type MoneyFormatOptions } from '#lib/utils/money.ts';

	interface Props extends MoneyFormatOptions {
		/** The amount — number, string-Decimal from the API, or null. */
		amount: number | string | null | undefined;
		/** Rendered when the amount is null/empty/non-finite. */
		placeholder?: string;
		/** Tabular-number alignment for right-aligned table cells. */
		mono?: boolean;
	}

	let {
		amount,
		currency = null,
		locale = undefined,
		whole = false,
		accounting = false,
		placeholder = '—',
		mono = false
	}: Props = $props();

	let formatted = $derived(
		formatMoney(amount, { currency, locale, whole, accounting }, placeholder)
	);
</script>

<span class="money" class:mono>{formatted}</span>

<style>
	.money {
		white-space: nowrap;
	}

	.money.mono {
		font-variant-numeric: tabular-nums;
		font-family:
			ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
	}
</style>
