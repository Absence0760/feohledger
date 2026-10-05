// Typed helpers for the two *history-derived* enrichment reads — the vendor
// performance score and the per-invoice auto-fill suggestions. Both are
// advisory, compute-on-read, and mutate nothing; the backend derives them
// deterministically from the tenant's own approved invoice history (no external
// call, no cloud key). Backend: `backend/app/api/enrichment.py`, documented in
// `backend/docs/data-enrichment.md`.
//
// The *external* firmographics enrichment (D&B / Clearbit) deliberately stays
// in `api/vendors.ts` next to the rest of the vendor stewardship writes — it
// calls a third party and has an apply endpoint. These two have neither, which
// is why they live apart rather than being appended there.
//
// Routes through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID
// + 401-bounce) — never raw fetch. Every numeric field arrives as a STRING
// (the backend's exact-Decimal wire convention); render them, never do money or
// score arithmetic on them client-side.
import { api } from '#lib/api.ts';

// ---------------------------------------------------------------------------
// Vendor performance score — GET /api/enrichment/vendors/{id}/score
// ---------------------------------------------------------------------------

/** The three sub-scores the backend computes. `on_time` is N/A unless the
 *  vendor has POs carrying an expected delivery date AND a goods receipt. */
export type SubScoreName = 'accuracy' | 'dispute' | 'on_time';

export interface VendorSubScore {
	name: SubScoreName | string;
	/** 0–100, one decimal, as an exact string. `null` = N/A (excluded from the
	 *  composite entirely rather than counted as zero). */
	score: string | null;
	/** How many records the sub-score was computed over — 0 when N/A. */
	sample_size: number;
	/** The backend's own one-line explanation of what produced this sub-score
	 *  (e.g. "3 of 5 approved invoices needed no corrections"). Rendered
	 *  verbatim: it is the evidence, and it carries counts the response does
	 *  not otherwise break out. Backend-generated English — see
	 *  `backend/docs/data-enrichment.md` § Vendor scoring. */
	detail: string;
}

export interface VendorScoreResponse {
	vendor_id: string;
	vendor_name: string;
	/** Weight-renormalized mean over the available sub-scores, as an exact
	 *  string. `null` when every sub-score is N/A (no history at all). */
	composite: string | null;
	sub_scores: VendorSubScore[];
	computed_at: string;
}

/** This vendor's performance score. Advisory + compute-on-read — nothing is
 *  persisted, and calling it changes nothing. Gated admin / ap_manager / cfo
 *  (`_SCORE_ROLES`); an ap_clerk gets a 403. */
export function getVendorScore(vendorId: string): Promise<VendorScoreResponse> {
	return api.get<VendorScoreResponse>(`/api/enrichment/vendors/${vendorId}/score`);
}

// ---------------------------------------------------------------------------
// Invoice auto-fill suggestions — GET /api/enrichment/invoices/{id}/suggestions
// ---------------------------------------------------------------------------

/** Coding fields the auto-fill surface can suggest. Mirrors the backend's
 *  `AUTOFILL_FIELDS`. */
export type AutofillField = 'gl_account' | 'cost_center' | 'payment_terms';

export interface FieldSuggestion {
	field: AutofillField | string;
	value: string;
	/** Share of the sampled history carrying `value`, 0–100 as an exact string. */
	confidence: string;
	/** Historical invoices that carried ANY value for this field. */
	sample_size: number;
	/** How many of those carried `value` — with `sample_size`, the provenance. */
	occurrences: number;
	/** The backend's own evidence sentence. The UI builds its own localized
	 *  line from `occurrences` / `sample_size` / `confidence` instead, so this
	 *  is here for parity with the API rather than for display. */
	evidence: string;
	/** Second-most-common historical value, when there was one. */
	runner_up: string | null;
}

export interface PriceVariance {
	line_index: number;
	item_key: string;
	description: string | null;
	current_unit_price: string;
	baseline_unit_price: string;
	delta: string;
	delta_pct: string;
	sample_size: number;
	direction: 'over' | 'under' | string;
	severity: 'warning' | 'info' | string;
}

export interface EnrichmentSuggestionsResponse {
	invoice_id: string;
	vendor_id: string | null;
	/** At most one per field, and ONLY for fields the invoice leaves empty —
	 *  the backend suppresses a suggestion over a populated value, so applying
	 *  one can never overwrite a human's coding. */
	field_suggestions: FieldSuggestion[];
	price_variances: PriceVariance[];
	generated_at: string;
}

/** Advisory coding suggestions for one invoice, derived from this vendor's
 *  approved history. Read-only: it never writes to the invoice — the user
 *  applies a suggestion into the form and still has to save. Gated
 *  admin / ap_manager / ap_clerk / cfo (`_SUGGEST_ROLES`). */
export function getInvoiceSuggestions(invoiceId: string): Promise<EnrichmentSuggestionsResponse> {
	return api.get<EnrichmentSuggestionsResponse>(
		`/api/enrichment/invoices/${invoiceId}/suggestions`
	);
}
