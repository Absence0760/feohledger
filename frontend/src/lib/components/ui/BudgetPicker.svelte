<script lang="ts">
	/**
	 * The shared budget picker — a searchable, SERVER-PAGED combobox over the
	 * budgets denominated in one currency.
	 *
	 * The control itself — reach, the honest count line, the WAI-ARIA combobox
	 * contract, the Escape capture — is `ui/SearchPicker`, shared with
	 * `ui/VendorPicker` and `ui/InvoicePicker`. This wrapper supplies the source
	 * (`GET /api/budgets?currency=&search=`, which matches name and dimension
	 * value), the label, the option row and the copy.
	 *
	 * **The set is the budgets in `currency`, filtered by the server.** A
	 * requisition can only link a budget in its own currency
	 * (`api/requisitions._resolve_links` 422s the rest, because the budget legs
	 * never convert), so the picker never offers one it would refuse. The source
	 * is derived from `currency`: a changed currency is a new function, and the
	 * picker drops the options it held for the old one.
	 *
	 * `/api/budgets` is admin / ap_manager / cfo. An `ap_clerk` gets a 403,
	 * which the count line reports as a load failure, never as "no budgets" —
	 * though the requisition form does not render this picker for a clerk.
	 */
	import SearchPicker from './SearchPicker.svelte';
	import Money from './Money.svelte';
	import { listBudgets } from '#lib/api/budgets.ts';
	import type { Budget } from '#lib/types/budget.ts';
	import type { SearchPickerLoad } from '#lib/utils/searchPicker.ts';
	import { budgetOptionLabel } from '#lib/utils/budgetPicker.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		value = $bindable(''),
		currency,
		label,
		hint,
		selectedLabel = null,
		disabled = false,
		fullWidth = false,
		testid,
		onselect
	}: {
		/** The chosen budget's uuid — `''` for none. This is what the form submits. */
		value?: string;
		/** Only budgets denominated in this ISO 4217 code are offered. */
		currency: string;
		label?: string;
		hint?: string;
		/** The linked budget's label when the form opened on an existing link —
		 *  it may sit on any page of the set. */
		selectedLabel?: string | null;
		disabled?: boolean;
		fullWidth?: boolean;
		testid?: string;
		/** Fired on every commit — the chosen budget, or null when cleared. */
		onselect?: (option: Budget | null) => void;
	} = $props();

	const code = $derived(currency.trim().toUpperCase());
	const load: SearchPickerLoad<Budget> = $derived.by(() => {
		const c = code;
		return async ({ search, page, page_size }) => {
			const res = await listBudgets({ currency: c, search, page, page_size });
			return { items: res.items, total: res.total };
		};
	});
</script>

<SearchPicker
	bind:value
	class="budget-picker"
	{load}
	optionLabel={budgetOptionLabel}
	text={{
		loading: m('budgets.picker.loading'),
		loadFailed: m('budgets.picker.loadFailed'),
		empty: m('budgets.picker.empty', { currency: code }),
		noMatches: (query) => m('budgets.picker.noMatches', { query }),
		showingAll: (total) => m('budgets.picker.showingAll', { total }),
		showingPartial: (shown, total) => m('budgets.picker.showingPartial', { shown, total }),
		refineHint: m('budgets.picker.refineHint'),
		loadMore: m('budgets.picker.loadMore'),
		loadMoreFailed: m('budgets.picker.loadMoreFailed'),
		clearAria: m('budgets.picker.clearAria'),
		listAria: m('budgets.picker.listAria'),
		unresolvedSelection: m('budgets.picker.unresolvedSelection')
	}}
	{label}
	{hint}
	{selectedLabel}
	placeholder={m('budgets.picker.placeholder')}
	{disabled}
	{fullWidth}
	{testid}
	{onselect}
>
	{#snippet option(budget)}
		<span class="bp-name">{budgetOptionLabel(budget)}</span>
		<span class="bp-amount"><Money amount={budget.amount} currency={budget.currency} /></span>
	{/snippet}
</SearchPicker>

<style>
	.bp-name {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* `--text-muted` on the popup's `--surface` clears 4.5:1 (the vendor
	   code's colour in the same popup); no `opacity`. */
	.bp-amount {
		flex-shrink: 0;
		font-size: 0.75rem;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}
</style>
