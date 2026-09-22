<script lang="ts">
	/**
	 * The shared vendor picker — a searchable, SERVER-PAGED combobox.
	 *
	 * Replaces the native `<select>` that five surfaces each built over a
	 * client-side vendor list: `/catalogs` (CatalogModal, twice), `/contracts`
	 * (ContractModal), `/recurring` (RecurringModal), `/vendor-statements`
	 * (VendorStatementReconModal) and `/discounts` (BulkNegotiationModal). Two
	 * of them asked for `page_size=100` and rendered page 1; the other three
	 * walked every page on mount. A `<select>` has no search, so on the capped
	 * pair a tenant past 100 vendors could not select the rest — anywhere — and
	 * nothing on screen said so (`docs/decisions.md` §145).
	 *
	 * The control itself — reach, the honest count line, the WAI-ARIA combobox
	 * contract, the Escape capture — is `ui/SearchPicker`, shared with
	 * `ui/InvoicePicker` (§202). This wrapper supplies the vendor source
	 * (`GET /api/vendors?search=`, which matches name / code / email), the
	 * label, the option row and the copy. `/api/vendors` is
	 * admin/ap_manager/cfo, so an `ap_clerk` gets a 403 — which the count line
	 * reports as a load failure, never as a tenant with no vendors.
	 */
	import SearchPicker from './SearchPicker.svelte';
	import { searchVendorOptions, type VendorOption } from '$lib/api/vendors';
	import { m } from '$lib/i18n/store.svelte';
	import { vendorOptionLabel } from '$lib/utils/vendorPicker';

	let {
		value = $bindable(''),
		label,
		ariaLabel,
		/** The chosen vendor's name when the form opened on an existing row. The
		 *  vendor may sit on any page of the tenant's set, so the picker cannot
		 *  look it up from the first page — the parent already has it on the row
		 *  it is editing (`contract.vendor_name`, `template.vendor_name`, …). */
		selectedLabel = null,
		placeholder,
		required = false,
		disabled = false,
		fullWidth = false,
		compact = false,
		testid,
		onselect
	}: {
		/** The chosen vendor's uuid — `''` for none. This is what the form submits. */
		value?: string;
		/** Visible field label. Omit for an inline control and pass `ariaLabel`. */
		label?: string;
		/** Accessible name when there is no visible label. */
		ariaLabel?: string;
		selectedLabel?: string | null;
		placeholder?: string;
		required?: boolean;
		disabled?: boolean;
		/** Span both columns of a `.form-grid`. */
		fullWidth?: boolean;
		/** Narrow sizing for an inline row of controls. */
		compact?: boolean;
		testid?: string;
		/** Fired on every commit — the chosen option, or null when cleared. */
		onselect?: (option: VendorOption | null) => void;
	} = $props();
</script>

<!-- `class="vendor-picker"` is the selector hook the vendor-picker e2e spec
     reads its count line through; the styling is SearchPicker's own. -->
<SearchPicker
	bind:value
	class="vendor-picker"
	load={searchVendorOptions}
	optionLabel={vendorOptionLabel}
	text={{
		loading: m('vendors.picker.loading'),
		loadFailed: m('vendors.picker.loadFailed'),
		empty: m('vendors.picker.noVendors'),
		noMatches: (query) => m('vendors.picker.noMatches', { query }),
		showingAll: (total) => m('vendors.picker.showingAll', { total }),
		showingPartial: (shown, total) => m('vendors.picker.showingPartial', { shown, total }),
		refineHint: m('vendors.picker.refineHint'),
		loadMore: m('vendors.picker.loadMore'),
		loadMoreFailed: m('vendors.picker.loadMoreFailed'),
		clearAria: m('vendors.picker.clearAria'),
		listAria: m('vendors.picker.listAria'),
		unresolvedSelection: m('vendors.picker.unresolvedSelection')
	}}
	{label}
	{ariaLabel}
	{selectedLabel}
	{placeholder}
	{required}
	{disabled}
	{fullWidth}
	{compact}
	{testid}
	{onselect}
>
	{#snippet option(vendor)}
		<span class="vp-option-name">{vendor.name}</span>
		{#if vendor.code?.trim()}
			<span class="vp-option-code">{vendor.code}</span>
		{/if}
	{/snippet}
</SearchPicker>

<style>
	.vp-option-name {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.vp-option-code {
		flex-shrink: 0;
		font-size: 0.7rem;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
</style>
