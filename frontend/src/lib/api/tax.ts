// Typed helpers for the 1099 reporting + admin-workflow endpoints. All
// requests route through the shared `api` client (Bearer + X-Tenant-Slug +
// 401-bounce). See backend/docs/tax-1099.md for the full contract.
import { api } from '#lib/api.ts';
import type {
	Filing1099Response,
	Report1099,
	TinVerifyResponse,
	VendorTaxProfile
} from '#lib/types/tax.ts';

// 1099 report for a calendar year. Aggregates completed payments per
// vendor and flags W-9 / TIN / >$600-threshold status.
export function get1099Report(year: number): Promise<Report1099> {
	return api.get<Report1099>(`/api/tax/1099-report?year=${encodeURIComponent(year)}`);
}

/** Update W-9 / tax fields on a vendor without uploading a new file — e.g.
 *  marking a vendor 1099-ineligible after a legal review, or correcting a
 *  mistyped TIN. `admin` / `ap_manager` only. */
export function updateVendorTaxFields(
	vendorId: string,
	fields: {
		tax_classification?: string | null;
		is_1099_eligible?: boolean;
		w9_received_date?: string | null;
		tax_id?: string | null;
	}
): Promise<VendorTaxProfile> {
	return api.patch<VendorTaxProfile>(`/api/tax/vendors/${vendorId}/w9`, fields);
}

/** Upload the vendor's signed W-9 PDF and mark them 1099-tracked. `file`
 *  must be one of the shared `ALLOWED_CONTENT_TYPES` (PDF/PNG/JPEG/TIFF).
 *  `admin` / `ap_manager` only. */
export function uploadVendorW9(
	vendorId: string,
	file: File,
	fields: { tax_classification?: string; is_1099_eligible?: boolean }
): Promise<VendorTaxProfile> {
	const form: Record<string, string> = {};
	if (fields.tax_classification) form.tax_classification = fields.tax_classification;
	if (fields.is_1099_eligible !== undefined) {
		form.is_1099_eligible = String(fields.is_1099_eligible);
	}
	return api.upload<VendorTaxProfile>(`/api/tax/vendors/${vendorId}/w9`, file, form);
}

/** Validate a vendor's TIN through the configured TIN-validation adapter.
 *  Pass `taxId` to update + validate a new value in one call; omit it to
 *  re-check whatever is already on the vendor row. The response carries
 *  only the verdict + a redacted last-4 — never the TIN. `admin` /
 *  `ap_manager` only. */
export function verifyVendorTin(vendorId: string, taxId?: string): Promise<TinVerifyResponse> {
	return api.post<TinVerifyResponse>(`/api/tax/vendors/${vendorId}/tin-verify`, {
		tax_id: taxId ?? null
	});
}

/** Download a vendor's 1099-NEC / 1099-MISC working-copy PDF for a year.
 *  The form carries only the boxes belonging to `formType` (the vendor's
 *  reportable total is split across boxes by GL account — see
 *  `Vendor1099Row.box_allocations`), so it 400s both when the vendor has no
 *  reportable payments at all AND when none of them land on the requested
 *  form. Callers should only offer a form the row has a box on. Read by all
 *  three roles (admin/ap_manager/cfo). */
export function downloadVendor1099Pdf(
	vendorId: string,
	year: number,
	formType: string = '1099-NEC'
): Promise<Blob> {
	const qs = new URLSearchParams({ year: String(year), form_type: formType });
	return api.downloadBlob(`/api/tax/vendors/${vendorId}/1099?${qs.toString()}`);
}

/** Submit a year's 1099s for e-filing via the configured adapter.
 *  Each vendor is filed for the part of its reportable total that belongs on
 *  `formType` — a vendor with both rent and contractor spend is filed twice,
 *  once per form, never once for the combined figure.
 *  Idempotent on `(org, idempotencyKey)` — a retried submit with the same
 *  key returns the stored confirmation (`already_filed: true`) instead of
 *  re-filing. This is genuinely irreversible once a real filing adapter is
 *  configured — the caller must confirm-then-act before calling this.
 *  `admin` / `ap_manager` only. */
export function file1099Batch(
	year: number,
	formType: string = '1099-NEC',
	idempotencyKey?: string
): Promise<Filing1099Response> {
	return api.post<Filing1099Response>('/api/tax/1099/file', {
		year,
		form_type: formType,
		idempotency_key: idempotencyKey ?? null
	});
}
