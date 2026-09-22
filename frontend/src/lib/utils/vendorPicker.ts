/**
 * The vendor-specific half of `ui/VendorPicker.svelte`. Everything generic —
 * paging, the count line, keyboard navigation, what Enter commits — lives in
 * `./searchPicker.ts`, shared with `ui/InvoicePicker` (`docs/decisions.md`
 * §145, §202).
 */

/** Vendor picker option — the `id` is what the consuming form submits. */
export interface VendorPickerOption {
	id: string;
	name: string;
	code?: string | null;
}

/** The text shown for an option, and the input's text once it is chosen. */
export function vendorOptionLabel(option: VendorPickerOption): string {
	const code = option.code?.trim();
	return code ? `${option.name} (${code})` : option.name;
}
