// Types for the GDPR/CCPA privacy surface (`/admin/privacy`). Mirrors
// `backend/app/schemas/privacy.py` + `backend/app/models/data_subject_request.py`.
import type { StepUpProof } from '#lib/stores/auth.svelte.ts';

export const SUBJECT_TYPES = ['user', 'vendor_user', 'vendor_contact'] as const;
export type SubjectType = (typeof SUBJECT_TYPES)[number];

export const SUBJECT_TYPE_LABELS: Record<SubjectType, string> = {
	user: 'AP-team user',
	vendor_user: 'Supplier-portal login',
	vendor_contact: 'Vendor contact details'
};

/** What the identifier field means for each subject type — shown as the
 *  field hint so an admin doesn't have to guess between an email and a UUID. */
export const SUBJECT_IDENTIFIER_HINTS: Record<SubjectType, string> = {
	user: "The user's email address.",
	vendor_user: "The supplier-portal login's email address.",
	vendor_contact: 'The Vendor UUID (from the vendor detail view or the URL).'
};

/** A second-factor proof for an unmasked export — the server reads `code` or
 *  `assertion` and ignores a password (`require_sensitive_step_up`). */
export type DSARStepUp = StepUpProof;

export interface DSARRequest {
	subject_type: SubjectType;
	identifier: string;
	/** Return bank details unmasked — `vendor_contact` only, needs the
	 *  `vendor.bank_change.approve` permission, a justification and `step_up`. */
	include_banking?: boolean;
	banking_justification?: string;
	step_up?: DSARStepUp;
}

export interface DSARResponse {
	request_id: string;
	subject_type: string;
	subject_id: string;
	generated_at: string;
	banking_disclosure: 'masked' | 'unmasked';
	// Loosely typed by design — a heterogeneous portable PII bundle.
	data: Record<string, unknown>;
}

export interface ErasureRequest {
	subject_type: SubjectType;
	identifier: string;
	confirm: true;
	note?: string;
}

export interface ErasureResponse {
	request_id: string;
	subject_type: string;
	subject_id: string;
	status: 'completed' | 'noop';
	already_erased: boolean;
	fields_redacted: number;
	record_counts: Record<string, unknown>;
	completed_at: string;
}

export type DataSubjectRequestType = 'dsar_export' | 'erasure';
export type DataSubjectRequestStatus = 'completed' | 'failed' | 'noop';

export interface DataSubjectRequestSummary {
	id: string;
	request_type: DataSubjectRequestType;
	subject_type: string;
	subject_id: string | null;
	status: DataSubjectRequestStatus;
	requested_by: string | null;
	fields_redacted: number;
	record_counts: Record<string, unknown> | null;
	note: string | null;
	created_at: string | null;
	completed_at: string | null;
}

export interface DataSubjectRequestList {
	total: number;
	requests: DataSubjectRequestSummary[];
}
