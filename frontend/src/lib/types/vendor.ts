// Vendor + sanctions-screening / risk types. These mirror the backend
// `VendorResponse`, `SanctionsCheckResponse`, `ScreeningReviewItem`, and the
// `/risk` payloads. The vendor list/detail UI and the typed helpers in
// `$lib/api/vendors.ts` share them.

export type ScreeningStatus = 'unscreened' | 'clear' | 'review' | 'match';
export type RiskLevel = 'low' | 'medium' | 'high' | 'critical' | 'unknown';

export interface VendorMailingAddress {
	street: string | null;
	city: string | null;
	state: string | null;
	postal: string | null;
	country: string | null;
}

export interface VendorBankDetails {
	counterparty_id: string | null;
	account_last4: string | null;
	// Last 4 of the ACH / domestic routing number (`bank_details.routing_number`
	// — the original generic key, which every stored row already means ACH by).
	routing_last4: string | null;
	// Last 4 of the SEPARATE Fedwire ABA larger US banks publish for incoming
	// wires (`bank_details.wire_routing_number`). Optional: when it is absent
	// the wire rail falls back to the ACH number, which is what a bank with a
	// single ABA means. Backend: `resolve_routing_number` in
	// `services/payment_adapters/base.py`.
	wire_routing_last4?: string | null;
	bank_name: string | null;
	// ISO 3166-1 alpha-2 destination-bank country. Drives the routing-number
	// vs. sort-code label/validation switch on the AP bank-change modal (a
	// UK vendor uses a sort code, not a US ABA routing number) — mirrors the
	// backend `schemas.vendor.VendorBankDetails.country` field.
	country?: string | null;
	// Where a printed check to this vendor gets mailed — required by the
	// `checkeeper` check-printing payment rail (see `services/payment_adapters/
	// checkeeper.py`); without it that rail refuses with
	// `checkeeper_missing_mailing_address`.
	mailing_address?: VendorMailingAddress | null;
}

export interface Vendor {
	id: string;
	name: string;
	code: string | null;
	email: string | null;
	phone: string | null;
	address: string | null;
	tax_id: string | null;
	payment_terms: string | null;
	accepts_virtual_cards: boolean;
	status: string;
	source: string;
	verified_by: string | null;
	erp_vendor_id: string | null;
	erp_synced_at: string | null;
	invoice_count: number;
	created_at: string;
	bank_details: VendorBankDetails | null;
	// Sanctions screening + vendor-risk fields (Sanctions & Vendor Risk Screening).
	screening_status: ScreeningStatus;
	last_screened_at: string | null;
	payments_blocked: boolean;
	payments_blocked_reason: string | null;
	risk_score: string | null;
	risk_level: RiskLevel;
}

// One row of a vendor's sanctions-screening history (newest first).
export interface SanctionsCheck {
	id: string;
	vendor_id: string;
	provider: string;
	check_type: string;
	result: string;
	risk_score: string | null;
	matched_list: string | null;
	// PII-free taxonomy of WHAT was hit: 'sanctions' | 'pep' | 'adverse_media'
	// | 'high_risk_country'. Empty for a clear screen and for rows written
	// before the taxonomy shipped.
	categories: string[];
	adverse_media: boolean;
	checked_at: string;
}

// A vendor surfaced on the screening review queue.
export interface ScreeningReviewItem {
	vendor_id: string;
	vendor_name: string;
	screening_status: ScreeningStatus;
	last_screened_at: string | null;
	payments_blocked: boolean;
	risk_level: RiskLevel;
	risk_score: string | null;
	latest_matched_list: string | null;
	latest_provider: string | null;
	latest_categories: string[];
	adverse_media: boolean;
}

/**
 * `GET /api/vendors/counts` — whole-set tallies over the entity-scoped,
 * search/source-filtered vendor population. PII-free (counts only).
 *
 * `payments_blocked` is a SECOND tally over the same population, not a slice
 * of `by_status`: a payment block is an orthogonal axis
 * (`POST /api/vendors/{id}/block` sets `payments_blocked` and never touches
 * `status` or `screening_status`), so it may overlap any bucket and must never
 * be added to `total`.
 *
 * It exists because `/vendors/screening`'s "Payments blocked" KPI used to be
 * counted off the screening REVIEW QUEUE — `screening_status IN
 * ('match','review')` — which structurally cannot see a vendor AP blocked
 * while screening-clear. A tally has to come from a query that asks the
 * tally's own question.
 */
export interface VendorStatusCounts {
	total: number;
	by_status: Record<string, number>;
	payments_blocked: number;
	// Whole-set tally of `Vendor.screening_status`, on the same aggregate pass
	// as `by_status` / `payments_blocked`. Drives the `/vendors/screening` KPI
	// row: those figures used to be derived by filtering the LOADED review
	// queue, which stopped being whole-set the moment that queue paginated.
	by_screening_status: Record<string, number>;
}

// `GET /api/vendors/screening/review-queue` — the canonical paginated envelope.
export interface ScreeningReviewQueueResponse {
	items: ScreeningReviewItem[];
	total: number;
	page: number;
	page_size: number;
}

// Vendor risk detail (GET /risk, POST /risk/recompute).
export interface VendorRisk {
	vendor_id: string;
	risk_score: string | null;
	risk_level: RiskLevel;
	risk_factors: Record<string, unknown> | null;
	risk_scored_at: string | null;
}

// One bucket of GET /risk/summary.
export interface RiskSummaryBucket {
	risk_level: RiskLevel;
	count: number;
}

// ---------------------------------------------------------------------------
// External vendor enrichment (firmographics from D&B / Clearbit / mock).
// Mirrors the backend `VendorEnrichmentResponse` / `VendorEnrichmentApply*`.
// Advisory / suggestion-only: the enrich call NEVER writes back — a steward
// reviews the per-field diff and explicitly applies a chosen subset. `tax_id`
// is intentionally NOT applyable here (only `name` / `address` / `website`);
// a tax-id change goes through the bank/tax change-request gate.
// ---------------------------------------------------------------------------

// Vendor columns the enrichment apply endpoint can write. Kept in lock-step
// with the backend `APPLYABLE_FIELDS` tuple (name / address / website).
export type EnrichableField = 'name' | 'address' | 'website';

// Normalised firmographics from the provider (raw tax_id is never echoed —
// only `tax_id_masked` of the form `***<last4>`).
export interface VendorFirmographics {
	provider: string;
	matched: boolean;
	legal_name: string | null;
	address: string | null;
	country: string | null;
	industry: string | null;
	sic_code: string | null;
	naics_code: string | null;
	employee_count: number | null;
	annual_revenue: string | null;
	website: string | null;
	duns_number: string | null;
	year_founded: number | null;
	tax_id_masked: string | null;
	confidence: number | null;
	extra: Record<string, unknown>;
}

// One advisory change a steward may choose to apply (current → suggested).
export interface EnrichmentFieldSuggestion {
	field: EnrichableField;
	current_value: string | null;
	suggested_value: string | null;
}

export interface VendorEnrichmentResponse {
	vendor_id: string;
	vendor_name: string;
	firmographics: VendorFirmographics;
	suggestions: EnrichmentFieldSuggestion[];
	generated_at: string;
}

// The steward's selection of fields to write onto the vendor.
export interface EnrichmentApplyField {
	field: EnrichableField;
	value: string | null;
}

export interface VendorEnrichmentApplyResponse {
	vendor_id: string;
	// Field-level before/after diff actually written (empty when a no-op).
	applied: Record<string, { old: string | null; new: string | null }>;
	vendor: Vendor;
	applied_at: string;
}

// Message keys for the applyable fields (used in the enrich diff UI). The
// field name is a table cell AND the `{field}` interpolated into the
// translated `vendors.modal.applyFieldAria` checkbox label, so an English
// literal here landed mid-sentence in a translated aria-label.
export const ENRICHABLE_FIELD_LABEL_KEYS: Record<EnrichableField, MessageKey> = {
	name: 'vendors.enrich.field.name',
	address: 'vendors.enrich.field.address',
	website: 'vendors.enrich.field.website'
};

// ---------------------------------------------------------------------------
// Vendor consolidation (duplicate / similar vendor clusters + merge).
// Mirrors the backend `VendorConsolidationResponse` / `VendorMerge*`. The
// suggestions endpoint is advisory (clusters by tax_id / code / fuzzy name,
// deterministic canonical pick, tax_id masked); the merge endpoint executes
// the fold of duplicates into the canonical vendor (gated `vendor.manage`).
// ---------------------------------------------------------------------------

// One member of a consolidation cluster (the canonical pick has is_canonical).
export interface VendorClusterMember {
	vendor_id: string;
	name: string;
	code: string | null;
	tax_id_masked: string | null; // ***6789 — never the full tax id
	status: string | null;
	invoice_count: number;
	is_canonical: boolean;
}

export interface VendorCluster {
	cluster_id: number;
	members: VendorClusterMember[];
	canonical_vendor_id: string;
	score: string; // 0..1 strongest pairwise evidence, string-Decimal
	reasons: string[];
}

export interface VendorConsolidationResponse {
	clusters: VendorCluster[];
	vendor_count: number;
	cluster_count: number;
	truncated: boolean; // tenant exceeded the bound, or clusters were capped
	generated_at: string;
}

// The steward's explicit merge: fold `duplicate_vendor_ids` into the canonical.
export interface VendorMergeRequest {
	canonical_vendor_id: string;
	duplicate_vendor_ids: string[];
}

export interface VendorMergeResponse {
	canonical_vendor_id: string;
	duplicate_vendor_ids: string[];
	// Per-table reassigned row counts (PII-free — table name → rows moved).
	reassigned: Record<string, number>;
	total_reassigned: number;
	// Duplicate ids THIS call flipped active → inactive (empty on idempotent re-run).
	deactivated_vendor_ids: string[];
	merged_at: string;
}

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';

/**
 * Badge tone per vendor lifecycle status (`Vendor.status`).
 *
 * Keyed on `string` because the column is untyped on the wire — an unknown
 * value falls back to `neutral` at the call site rather than rendering a BLANK
 * badge, which is how an unlisted status disappears (`types/payment.ts` records
 * the same trap for `pending_compliance`).
 *
 * Lives here rather than on `/vendors` so the list page and `VendorModal` — the
 * detail view of the same row — cannot end up tinting one vendor two ways. This
 * is the ONLY thing that says a `rejected` vendor is red: `.rejected td` fades
 * the row by 4% and nothing else in the table repeats the state.
 */
export const VENDOR_STATUS_TONES: Record<string, BadgeTone> = {
	active: 'success',
	// Unverified is work owed before this payee can be trusted with money, not
	// a failure — amber, the same register the row tint uses.
	unverified: 'warning',
	// Inactive is a vendor deliberately retired: the absence of a signal, so a
	// flat chip rather than a tint (it was already `var(--bg)`).
	inactive: 'neutral',
	rejected: 'danger'
};

// Every lifecycle value `Vendor.status` can hold. The column is untyped on the
// wire (hence the `string`-keyed tone map above), so this union is the
// frontend's own vocabulary: it exists to make the label map below TOTAL, so a
// status added here without a label is a compile error rather than a badge
// that quietly falls back to its raw enum value.
export type VendorStatus = 'active' | 'unverified' | 'inactive' | 'rejected';

export const VENDOR_STATUSES: VendorStatus[] = ['active', 'unverified', 'inactive', 'rejected'];

/**
 * The i18n key carrying each lifecycle-status label — never the English string
 * itself. It lived as a page-local English map on `/vendors`, whose STATUS
 * FILTER CHIPS are translated, so the row badge read `Unverified` in English
 * directly beside a chip that read `Nicht verifiziert`. It now sits beside the
 * tone map for the same reason that one was hoisted: the label and the colour
 * of one status belong together.
 */
export const VENDOR_STATUS_LABEL_KEYS: Record<VendorStatus, MessageKey> = {
	active: 'vendors.status.active',
	unverified: 'vendors.status.unverified',
	inactive: 'vendors.status.inactive',
	rejected: 'vendors.status.rejected'
};

/** The message key for a lifecycle status, or `null` for an unrecognised one. */
export function vendorStatusLabelKey(status: string): MessageKey | null {
	return VENDOR_STATUS_LABEL_KEYS[status as VendorStatus] ?? null;
}

// Where the vendor record came from (`Vendor.source`) — rendered as a muted
// pill in the row beside the status badge, so it is keyed for the same reason.
export type VendorSource = 'manual' | 'erp_sync' | 'ai_extracted';

export const VENDOR_SOURCE_LABEL_KEYS: Record<VendorSource, MessageKey> = {
	manual: 'vendors.source.manual',
	erp_sync: 'vendors.source.erpSync',
	ai_extracted: 'vendors.source.aiExtracted'
};

/** The message key for a vendor source, or `null` for an unrecognised one. */
export function vendorSourceLabelKey(source: string): MessageKey | null {
	return VENDOR_SOURCE_LABEL_KEYS[source as VendorSource] ?? null;
}

/**
 * The i18n key carrying each screening-status label — never the English string
 * itself. `ui/ScreeningBadge.svelte` renders this pill on the (extracted)
 * `/vendors` list and inside `VendorModal`, so a hardcoded English map here
 * put an untranslated `Clear` beside translated columns. `Record<
 * ScreeningStatus, MessageKey>` makes a new status a compile error rather
 * than a blank pill, and `vendor.test.ts` proves every key exists in the
 * catalogue.
 *
 */
export const SCREENING_STATUS_LABEL_KEYS: Record<ScreeningStatus, MessageKey> = {
	unscreened: 'vendors.screening.status.unscreened',
	clear: 'vendors.screening.status.clear',
	review: 'vendors.screening.status.review',
	match: 'vendors.screening.status.match'
};

/**
 * The message key for a screening status, or `null` for one this frontend
 * doesn't know (the caller renders the raw value — visible and searchable —
 * rather than a blank pill).
 */
export function screeningStatusLabelKey(status: string): MessageKey | null {
	return SCREENING_STATUS_LABEL_KEYS[status as ScreeningStatus] ?? null;
}

/**
 * The i18n key carrying each risk level — never the English string itself.
 *
 * It is rendered in three places, and the pill in `ui/ScreeningBadge.svelte`
 * is why this had to land: that badge shows the screening verdict and the
 * negative-news flag (both localized) directly beside the risk level, so an
 * English `High risk` sat between two translated pills.
 *
 * The pill and its tooltip are COMPOSED strings, not the bare level, so they
 * get their own keys (`vendors.risk.pill` / `.title`) rather than an English
 * `{label} risk` template: German puts the noun first, French wants a space
 * before its colon, and Japanese neither. Never concatenate a translated
 * level onto an English word.
 */
export const RISK_LEVEL_LABEL_KEYS: Record<RiskLevel, MessageKey> = {
	low: 'vendors.risk.low',
	medium: 'vendors.risk.medium',
	high: 'vendors.risk.high',
	critical: 'vendors.risk.critical',
	unknown: 'vendors.risk.unknown'
};

/**
 * The message key for a risk level, or `null` for one this frontend doesn't
 * know (the caller renders the raw value — visible and searchable — rather
 * than a blank cell). `Vendor.risk_level` is typed against the union, but the
 * scorer can widen it before this map catches up.
 */
export function riskLevelLabelKey(level: string): MessageKey | null {
	return RISK_LEVEL_LABEL_KEYS[level as RiskLevel] ?? null;
}

// The screening-hit taxonomy (`SanctionsCheck.categories`). The vocabulary is
// FIXED and PII-free by construction — the adapters normalise every provider's
// own wording into these four labels (`backend/app/services/
// sanctions_categories.py`), so the union is total and a category with no
// label is a compile error, the way `PaymentRunStatus` works.
export const SCREENING_CATEGORIES = [
	'sanctions',
	'pep',
	'adverse_media',
	'high_risk_country'
] as const;

export type ScreeningCategory = (typeof SCREENING_CATEGORIES)[number];

/**
 * The i18n key carrying each hit kind's label — never the English string
 * itself. These render in the screening-review modal's "Hit categories" row
 * and inline in every history line, both beside the already-translated
 * screening verdict and risk level, so a hardcoded map read as a gap in the
 * sentence. `vendor.test.ts` proves each key resolves in the catalogue.
 */
export const SCREENING_CATEGORY_LABEL_KEYS: Record<ScreeningCategory, MessageKey> = {
	sanctions: 'vendors.screening.category.sanctions',
	pep: 'vendors.screening.category.pep',
	adverse_media: 'vendors.screening.category.adverseMedia',
	high_risk_country: 'vendors.screening.category.highRiskCountry'
};

/**
 * The message key for a hit kind, or `null` for one this frontend doesn't
 * know. `sanctions_checks.raw_response` is JSONB read back by a deliberately
 * tolerant parser (`categories_from_raw_response` accepts any list of
 * strings), so a provider adapter can widen the taxonomy before this map
 * catches up — the caller then renders {@link screeningCategoryFallback}
 * rather than dropping the hit, which is the one outcome a compliance reviewer
 * must never get.
 */
export function screeningCategoryLabelKey(category: string): MessageKey | null {
	return SCREENING_CATEGORY_LABEL_KEYS[category as ScreeningCategory] ?? null;
}

/** Readable stand-in for an unrecognised category: the raw label, de-underscored. */
export function screeningCategoryFallback(category: string): string {
	return category.replace(/_/g, ' ');
}

export interface ScreeningCategoryLabel {
	category: string;
	/** The message key, or `null` when this build has no wording for the hit. */
	key: MessageKey | null;
	/** What to render when `key` is `null`. */
	fallback: string;
}

/**
 * Resolve a screening result's categories for display — one entry per hit, in
 * the order the provider reported them.
 *
 * This replaced a `formatScreeningCategories` that returned a finished English
 * string: joining is the caller's job now, because only the caller can reach
 * `m()`. The resolution rule (known → key, unknown → de-underscored raw) stays
 * here so the two call sites can't drift.
 */
export function screeningCategoryLabels(
	categories: string[] | null | undefined
): ScreeningCategoryLabel[] {
	return (categories ?? []).map((category) => ({
		category,
		key: screeningCategoryLabelKey(category),
		fallback: screeningCategoryFallback(category)
	}));
}

// The per-screen VERDICT vocabulary (`SanctionsCheck.result`), which is a THIRD
// vocabulary — not `ScreeningStatus`, and not `ScreeningCategory`. The backend's
// `vendor_screening._STATUS_MAP` collapses `review_required` to `review` when it
// stamps `Vendor.screening_status`, so the vendor-level status map above cannot
// label a history row: it has no `review_required` member.
export const SANCTIONS_RESULTS = ['clear', 'review_required', 'match'] as const;

export type SanctionsResult = (typeof SANCTIONS_RESULTS)[number];

/**
 * The i18n key carrying each verdict's label — never the English string itself.
 *
 * The screening-review modal's history timeline rendered these as
 * `result.replace(/_/g, ' ')` under a `text-transform: capitalize`, which is an
 * English-only derivation: it printed `Review Required` in an otherwise
 * translated modal, and `capitalize` would have title-cased a German or French
 * label mid-phrase. `vendor.test.ts` proves each key resolves in the catalogue.
 */
export const SANCTIONS_RESULT_LABEL_KEYS: Record<SanctionsResult, MessageKey> = {
	clear: 'vendors.screening.result.clear',
	review_required: 'vendors.screening.result.reviewRequired',
	match: 'vendors.screening.result.match'
};

/**
 * The message key for a screening verdict, or `null` for one this frontend
 * doesn't know — the caller then renders the de-underscored raw value, visible
 * and searchable, rather than a blank badge. `SanctionsCheck.result` is typed
 * `string` off the wire precisely because an adapter can widen it before this
 * map catches up, and a dropped verdict is the one outcome a compliance
 * reviewer must never get (the rule `screeningCategoryLabelKey` states).
 */
export function sanctionsResultLabelKey(result: string): MessageKey | null {
	return SANCTIONS_RESULT_LABEL_KEYS[result as SanctionsResult] ?? null;
}

// ---------------------------------------------------------------------------
// Vendor change requests — the dual-control (BEC / bank-redirect) gate.
//
// A bank-detail or tax-ID change NEVER applies inline: `POST /api/vendors`,
// `PATCH /api/vendors/{id}`, `POST /api/vendors/{id}/bank-change` and the
// supplier portal all STAGE a `VendorChangeRequest`, and a SECOND user holding
// `vendor.bank_change.approve` applies it. Mirrors the backend
// `VendorChangeRequestResponse`.
//
// `proposed_value` is MASKED on the queue list (last-4 only) and revealed on
// the per-vendor detail endpoint — so the field is deliberately loose here:
// the same key carries `{account_last4, bank_name}` in one view and the full
// `{bank_details: {...}}` in the other.
// ---------------------------------------------------------------------------

export type VendorChangeRequestStatus = 'pending' | 'approved' | 'rejected';

/** The two staged change kinds the backend knows how to apply. */
export type VendorChangeType = 'bank_details' | 'tax_id';

export interface VendorChangeRequest {
	id: string;
	vendor_id: string;
	vendor_name: string | null;
	change_type: string;
	status: string;
	/** Masked on the queue list; full value on `GET /api/vendors/{id}/change-requests`. */
	proposed_value: Record<string, unknown> | null;
	// Exactly one requester is set: the portal VendorUser, or the AP User.
	requested_by_vendor_user_id: string | null;
	requested_by_user_id: string | null;
	reviewed_by_user_id: string | null;
	reviewed_at: string | null;
	review_note: string | null;
	created_at: string;
}

/** `GET /api/vendors/change-requests` — the paginated queue envelope. */
export interface VendorChangeRequestPage {
	items: VendorChangeRequest[];
	total: number;
	page: number;
	page_size: number;
}

/**
 * `GET /api/vendors/change-requests/counts` — whole-set, search-independent
 * tallies for the status chips. PII-free (counts only).
 *
 * Separate from the queue's own `total`, which counts only the ACTIVE filter's
 * result set: the Pending badge has to be the whole set or a queue with 25
 * unreviewed bank changes reads as 20 the moment the first page caps. Same
 * reason `GET /api/vendors/counts` exists for the vendor status chips.
 *
 * `pending` is a convenience duplicate of `by_status.pending`; read whichever,
 * they are the same number.
 */
export interface VendorChangeRequestCounts {
	total: number;
	pending: number;
	by_status: Partial<Record<VendorChangeRequestStatus, number>>;
}

/**
 * One-line summary of a MASKED proposed value, for the queue list.
 *
 * Renders nothing but the backend's own mask: the list payload carries
 * `account_last4` / `tax_id_last4` and never a full account or tax number, so
 * this helper must not invent one. Returns `null` when the change type is
 * unknown or the payload is empty, so the caller renders its own em-dash
 * rather than a blank cell.
 */
export function maskedProposalSummary(
	changeType: string,
	proposed: Record<string, unknown> | null | undefined
): string | null {
	if (!proposed) return null;
	if (changeType === 'bank_details') {
		const bankName = typeof proposed.bank_name === 'string' ? proposed.bank_name : null;
		const last4 = typeof proposed.account_last4 === 'string' ? proposed.account_last4 : null;
		return [bankName, last4 ? `••••${last4}` : null].filter(Boolean).join(' · ') || null;
	}
	if (changeType === 'tax_id') {
		const last4 = typeof proposed.tax_id_last4 === 'string' ? proposed.tax_id_last4 : null;
		return last4 ? `••••${last4}` : null;
	}
	return null;
}

/**
 * The REVEALED proposed value flattened to `{field, value}` rows for the
 * review dialog — what an approver reads back to the supplier on a callback
 * before signing off.
 *
 * Returns `null` whenever it cannot represent the payload **without losing
 * anything** (an unknown change type, a nested object, a non-primitive). The
 * caller then falls back to rendering the raw JSON: on a control whose whole
 * job is "look at the new account number", a prettier view that silently drops
 * a field would be worse than an ugly one that shows all of them.
 */
export function revealedProposalFields(
	changeType: string,
	proposed: Record<string, unknown> | null | undefined
): { field: string; value: string }[] | null {
	if (!proposed) return null;
	const inner =
		changeType === 'bank_details'
			? proposed.bank_details
			: changeType === 'tax_id'
				? proposed
				: null;
	if (!inner || typeof inner !== 'object' || Array.isArray(inner)) return null;
	const rows: { field: string; value: string }[] = [];
	for (const [key, value] of Object.entries(inner as Record<string, unknown>)) {
		if (value === null || value === undefined) continue;
		if (typeof value === 'object') return null; // nested — fall back to JSON
		rows.push({ field: key.replace(/_/g, ' '), value: String(value) });
	}
	return rows.length ? rows : null;
}
