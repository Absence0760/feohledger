// Types for the 1099 reporting surface. Mirrors the JSON returned by
// `GET /api/tax/1099-report?year=` (backend `Report1099.to_dict()` /
// `VendorReportRow.to_dict()`). Money fields arrive as string-Decimals.

export interface Vendor1099Row {
	vendor_id: string;
	vendor_name: string;
	/** Masked/full tax id as the backend chooses to expose it; may be null. */
	tax_id: string | null;
	/** W-9 box-3 classification (e.g. "Individual/sole proprietor"); may be null. */
	tax_classification: string | null;
	is_1099_eligible: boolean;
	/** ISO date (YYYY-MM-DD) the W-9 was received, or null. */
	w9_received_date: string | null;
	w9_on_file: boolean;
	/**
	 * Year-to-date **reportable** completed payments — string-Decimal. Card-rail
	 * payments are excluded: the card settlement entity files those on a 1099-K,
	 * so counting them here would over-report the vendor. This is the figure that
	 * lands in the 1099 box amount.
	 */
	ytd_paid: string;
	over_threshold: boolean;
	/** Count of the reportable (non-card) payments behind `ytd_paid`. */
	payment_count: number;
	/**
	 * Card-rail total deliberately EXCLUDED from `ytd_paid` — string-Decimal.
	 * Surfaced so an operator can reconcile against the processor's 1099-K.
	 */
	card_paid: string;
	card_payment_count: number;
	/**
	 * True once a TIN match has stamped `Vendor.tin_verified_at`. Distinct
	 * from merely *having* a `tax_id` on file — a TIN can be present but
	 * never run through `POST /api/tax/vendors/{id}/tin-verify`.
	 */
	tin_verified: boolean;
	/**
	 * Per-box split of `ytd_paid`, driven by the paying invoice's GL account
	 * through the org's `settings.tax.boxes` mapping. Sums to `ytd_paid`
	 * exactly — every payment lands whole in one box, so there is no rounding
	 * residual. See backend/docs/tax-1099.md § Per-box allocation.
	 */
	box_allocations: Box1099Allocation[];
	/**
	 * The slice of `ytd_paid` that reached the fallback box because no mapping
	 * rule matched it — string-Decimal. It IS filed (in the fallback box); it
	 * is surfaced because nobody has told us which box it really belongs in.
	 */
	unmapped_paid: string;
	unmapped_payment_count: number;
	/**
	 * `ytd_paid` minus the sum of the boxes — string-Decimal, `"0.00"` for
	 * every row the aggregation produced. Published so the reconciliation
	 * guarantee can be checked rather than taken on trust.
	 */
	box_unallocated: string;
	/**
	 * The forms this payee must receive (`'1099-NEC'` / `'1099-MISC'`) — each
	 * box measured against its own per-year threshold (royalties $10, attorney
	 * gross proceeds $600, everything else $600 before 2026 and $2,000 from
	 * 2026). `over_threshold` is exactly "this list is non-empty".
	 */
	required_forms: string[];
}

/** One 1099 box's share of a vendor's reportable total. Money is a
 *  string-Decimal; `box` is the stable code (`NEC-1`, `MISC-6`). */
export interface Box1099Allocation {
	box: string;
	/** "1099-NEC" | "1099-MISC" — which form this box prints on. */
	form_type: string;
	/** IRS box number within that form, e.g. "6". */
	box_number: string;
	/** Human label, e.g. "Medical and health care payments". */
	label: string;
	amount: string;
	payment_count: number;
	/** True when this box absorbed spend no mapping rule matched. */
	fallback: boolean;
}

/**
 * The vendor's full tax-bookkeeping state — the shape returned by
 * `_vendor_tax_response()` and echoed by every mutating W-9/TIN endpoint
 * (`PATCH/POST .../w9`, `POST .../tin-verify`). A superset of the fields a
 * `Vendor1099Row` carries, read after an edit to refresh one row in place
 * without re-fetching the whole report.
 */
export interface VendorTaxProfile {
	vendor_id: string;
	tax_id: string | null;
	tax_classification: string | null;
	is_1099_eligible: boolean;
	w9_received_date: string | null;
	/** True only for an actual W-9 (shared column also holds W-8 uploads). */
	w9_on_file: boolean;
	tin_verified_at: string | null;
}

/** `POST /api/tax/vendors/{id}/tin-verify` — the validation verdict, never
 *  the TIN itself (PII stays server-side; see `backend/docs/tax-1099.md`).
 *  Mirrors `TINValidationResult.to_dict()` — note there is no boolean
 *  `is_valid` on the wire; that's a Python-only computed property
 *  (`verdict == "valid"`) that never made it into the serialized dict. */
export interface TinValidationResult {
	verdict: 'valid' | 'invalid' | 'unknown';
	/** "ein" | "ssn" | null — structural TIN type the format check detected. */
	tin_type: string | null;
	/** Redacted last-4 only, e.g. `6789`. */
	tin_last4: string | null;
	/** Whether the TIN matched the recipient legal name — `null` when the
	 *  provider can only check format (the local `mock` adapter). */
	name_match: boolean | null;
	provider: string;
	reason_code: string | null;
}

/** Response envelope of `POST /api/tax/vendors/{id}/tin-verify`: the
 *  refreshed tax profile plus the verdict that produced it. */
export interface TinVerifyResponse extends VendorTaxProfile {
	tin_validation: TinValidationResult;
}

/** One filed/rejected form inside a `Filing1099Response.forms` list — the
 *  redacted per-form result the partner returned. No TIN. */
export interface Filing1099FormResult {
	vendor_id: string;
	form_type: string;
	accepted: boolean;
	reason_code: string | null;
}

/** `POST /api/tax/1099/file` response — mirrors the backend's
 *  `_filing_response()`. Idempotent: a retried submit with the same
 *  `idempotency_key` returns `already_filed: true` and the stored result. */
export interface Filing1099Response {
	filing_id: string;
	year: number;
	provider: string;
	status: string;
	confirmation_number: string | null;
	submitted_count: number;
	accepted_count: number;
	rejected_count: number;
	already_filed: boolean;
	forms: Filing1099FormResult[];
	/** Per-box split behind each filed form. Empty for a filing made before
	 *  per-box allocation shipped — stored records are never back-filled. */
	box_breakdown: Filing1099BoxBreakdown[];
}

/** The per-box detail behind one filed form. PII-free: vendor id, box codes
 *  and amounts — never a name or TIN. */
export interface Filing1099BoxBreakdown {
	vendor_id: string;
	boxes: Box1099Allocation[];
	/** Sum of `boxes` — the figure actually transmitted for this form. */
	form_total: string;
}

export interface Report1099 {
	year: number;
	/** IRS reporting threshold — string-Decimal (e.g. "600"). */
	threshold_usd: string;
	/**
	 * The currency the totals + per-vendor `ytd_paid` are denominated in — the
	 * org's reporting (home) currency. `Payment.amount` is already home-currency
	 * so this is an honest label, not an FX conversion. Authoritative for the
	 * display currency — prefer it over the org-default store.
	 */
	currency: string;
	vendor_count_total: number;
	vendor_count_eligible_over_threshold: number;
	vendor_count_over_threshold_without_w9: number;
	/** Sum of YTD paid across eligible-over-threshold vendors — string-Decimal. */
	total_reportable: string;
	/** @deprecated Back-compat alias of `total_reportable` (same value). */
	total_reportable_usd: string;
	/**
	 * Card-rail spend for the year across EVERY vendor row (not just the
	 * eligible-over-threshold ones `total_reportable` covers) — the money the
	 * 1099 leaves out because the card processor reports it on a 1099-K.
	 * string-Decimal.
	 */
	total_card_excluded: string;
	/**
	 * Per-box totals across exactly the vendors `total_reportable` covers
	 * (1099-eligible + over threshold). The preparer's pre-filing sanity check.
	 */
	box_allocations: Box1099Allocation[];
	/** Reportable money sitting in the fallback box for want of a mapping rule
	 *  — string-Decimal. Non-zero means "go write a GL → box rule". */
	total_unmapped: string;
	/** `total_reportable` minus the sum of the boxes — string-Decimal, always
	 *  `"0.00"`. Published so the guarantee is checkable. */
	box_unallocated: string;
	/** True when the boxes reconcile to `total_reportable` to the cent. */
	box_allocation_reconciled: boolean;
	/** ISO date the report was generated. */
	generated_at: string;
	rows: Vendor1099Row[];
}
