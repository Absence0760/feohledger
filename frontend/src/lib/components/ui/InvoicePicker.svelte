<script lang="ts" generics="T extends InvoicePickerOption">
	/**
	 * The shared invoice picker — a searchable, SERVER-PAGED combobox over
	 * whatever set of invoices the parent's `load` defines.
	 *
	 * Replaces the two native `<select>`s on `/credit-memos` (the Apply dialog,
	 * and the create dialog's optional "Apply to invoice" link), which read one
	 * array the page filled on MOUNT by walking every page of `GET /api/invoices`
	 * and then filtered by vendor in the browser. That cost one request per 100
	 * invoices on every visit, for a list most visits never opened, and it still
	 * offered invoices the apply then refused — another currency, too little
	 * balance left, another entity (`docs/decisions.md` §202).
	 *
	 * The control — reach, the honest count line, the WAI-ARIA combobox contract,
	 * the Escape capture — is `ui/SearchPicker`, shared with `ui/VendorPicker`.
	 * This wrapper supplies the label, the option row and the copy, and it
	 * always PRELOADS: it fetches the first page when it mounts (i.e. when its
	 * dialog opens) and whenever `load` changes, so the closed field can already
	 * say "nothing here can take this" or "couldn't load" — for a dialog whose
	 * whole purpose is picking an invoice, that is the first thing to know.
	 *
	 * `load` is the SOURCE, and its identity is the set: bind the scope (a memo
	 * id, a chosen vendor) into it with `$derived`, so a new scope is a new
	 * function and the picker drops what it held for the old one.
	 */
	import SearchPicker from './SearchPicker.svelte';
	import Money from './Money.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { formatMoney } from '$lib/utils/money';
	import type { SearchPickerLoad } from '$lib/utils/searchPicker';
	import {
		invoiceOptionLabel,
		remainingToCredit,
		type InvoicePickerOption
	} from '$lib/utils/invoicePicker';

	let {
		value = $bindable(''),
		load,
		emptyText,
		label,
		ariaLabel,
		hint,
		selectedLabel = null,
		placeholder,
		required = false,
		disabled = false,
		fullWidth = false,
		testid,
		onselect
	}: {
		/** The chosen invoice's uuid — `''` for none. This is what the form submits. */
		value?: string;
		/** Fetches one page of the set this field chooses from. */
		load: SearchPickerLoad<T>;
		/** What an EMPTY set means here, already localized. The caller's set
		 *  has a rule ("invoices this credit can go on"), and only the caller
		 *  can say what its emptiness means — never a generic "no invoices". */
		emptyText: string;
		/** Visible field label. Omit for an inline control and pass `ariaLabel`. */
		label?: string;
		/** Accessible name when there is no visible label. */
		ariaLabel?: string;
		/** A standing explanation under the field, in its description. */
		hint?: string;
		/** The committed invoice's number when the form opened on one. */
		selectedLabel?: string | null;
		placeholder?: string;
		required?: boolean;
		disabled?: boolean;
		/** Span both columns of a `.form-grid`. */
		fullWidth?: boolean;
		testid?: string;
		/** Fired on every commit — the chosen option, or null when cleared. */
		onselect?: (option: T | null) => void;
	} = $props();
</script>

<SearchPicker
	bind:value
	class="invoice-picker"
	preload
	{load}
	optionLabel={invoiceOptionLabel}
	text={{
		loading: m('invoices.picker.loading'),
		loadFailed: m('invoices.picker.loadFailed'),
		empty: emptyText,
		noMatches: (query) => m('invoices.picker.noMatches', { query }),
		showingAll: (total) => m('invoices.picker.showingAll', { total }),
		showingPartial: (shown, total) => m('invoices.picker.showingPartial', { shown, total }),
		refineHint: m('invoices.picker.refineHint'),
		loadMore: m('invoices.picker.loadMore'),
		loadMoreFailed: m('invoices.picker.loadMoreFailed'),
		clearAria: m('invoices.picker.clearAria'),
		listAria: m('invoices.picker.listAria'),
		unresolvedSelection: m('invoices.picker.unresolvedSelection')
	}}
	{label}
	{ariaLabel}
	{hint}
	{selectedLabel}
	{placeholder}
	{required}
	{disabled}
	{fullWidth}
	{testid}
	{onselect}
>
	{#snippet option(invoice)}
		{@const remaining = remainingToCredit(invoice)}
		<span class="ip-number">{invoice.invoice_number}</span>
		<span class="ip-figures">
			<Money amount={invoice.amount} currency={invoice.currency} />
			{#if remaining !== null}
				<span class="ip-remaining">
					{m('invoices.picker.leftToCredit', {
						amount: formatMoney(remaining, { currency: invoice.currency })
					})}
				</span>
			{/if}
		</span>
	{/snippet}
</SearchPicker>

<style>
	.ip-number {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}

	.ip-figures {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		flex-shrink: 0;
		font-variant-numeric: tabular-nums;
	}

	/* `--text-muted` on the option's `--surface` clears 4.5:1 (it is the
	   vendor code's colour in the same popup); no `opacity`. */
	.ip-remaining {
		font-size: 0.72rem;
		color: var(--text-muted);
		white-space: nowrap;
	}
</style>
