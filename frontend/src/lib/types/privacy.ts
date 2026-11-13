// Types for the GDPR/CCPA privacy surface (`/admin/privacy`). Mirrors
// `backend/app/schemas/privacy.py` + `backend/app/models/data_subject_request.py`.
import type { StepUpProof } from '#lib/stores/auth.svelte.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

export const SUBJECT_TYPES = ['user', 'vendor_user', 'vendor_contact'] as const;
export type SubjectType = (typeof SUBJECT_TYPES)[number];

/** The catalogue key naming each subject type (settings slice, `admin.privacy.*`). */
export const SUBJECT_TYPE_LABEL_KEYS: Record<SubjectType, MessageKey> = {
	user: 'admin.privacy.subject.user',
	vendor_user: 'admin.privacy.subject.vendorUser',
	vendor_contact: 'admin.privacy.subject.vendorContact'
};

/** The key for a subject type's label, or `null` for one this build predates
 *  (the caller then shows the stored value verbatim). */
export function subjectTypeLabelKey(t: string): MessageKey | null {
	return SUBJECT_TYPE_LABEL_KEYS[t as SubjectType] ?? null;
}

/** What the identifier field means for each subject type — shown as the
 *  field hint so an admin doesn't have to guess between an email and a UUID. */
export const SUBJECT_IDENTIFIER_HINT_KEYS: Record<SubjectType, MessageKey> = {
	user: 'admin.privacy.identifierHint.user',
	vendor_user: 'admin.privacy.identifierHint.vendorUser',
	vendor_contact: 'admin.privacy.identifierHint.vendorContact'
};

/**
 * The bundle sections the server counts into `DSARResponse.record_counts`, in
 * the order the export summary lists them. Mirrors
 * `backend/app/services/privacy_export.py::DSAR_COUNTED_SECTIONS`;
 * `privacy.test.ts` reads that tuple and fails on drift (decisions §360).
 */
export const DSAR_COUNTED_SECTIONS = [
	'roles',
	'push_devices',
	'passkeys',
	'audit_events',
	'notifications',
	'expense_reports',
	'expenses',
	'related_invoices',
	'related_payments',
	'portal_users',
	'contracts',
	'virtual_cards',
	'chat_messages',
	'documents'
] as const;
export type DsarCountedSection = (typeof DSAR_COUNTED_SECTIONS)[number];

/** Each counted section's `{n}`-plural label. */
export const DSAR_COUNT_LABEL_KEYS: Record<DsarCountedSection, MessageKey> = {
	roles: 'admin.privacy.count.roles',
	push_devices: 'admin.privacy.count.pushDevices',
	passkeys: 'admin.privacy.count.passkeys',
	audit_events: 'admin.privacy.count.auditEvents',
	notifications: 'admin.privacy.count.notifications',
	expense_reports: 'admin.privacy.count.expenseReports',
	expenses: 'admin.privacy.count.expenses',
	related_invoices: 'admin.privacy.count.invoices',
	related_payments: 'admin.privacy.count.payments',
	portal_users: 'admin.privacy.count.portalUsers',
	contracts: 'admin.privacy.count.contracts',
	virtual_cards: 'admin.privacy.count.virtualCards',
	chat_messages: 'admin.privacy.count.chatMessages',
	documents: 'admin.privacy.count.documents'
};

/** One summary line per counted section the export carries, in catalogue
 *  order. A section this build has no label for is skipped rather than shown
 *  as a raw identifier — `privacy.test.ts` keeps that set empty. */
export function dsarCountLines(
	counts: Record<string, number> | undefined
): { section: DsarCountedSection; key: MessageKey; n: number }[] {
	if (!counts) return [];
	return DSAR_COUNTED_SECTIONS.filter((s) => typeof counts[s] === 'number').map((section) => ({
		section,
		key: DSAR_COUNT_LABEL_KEYS[section],
		n: counts[section]
	}));
}

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
	/** Typed per-section record counts of `data` (see `DSAR_COUNTED_SECTIONS`).
	 *  Optional only for a server that predates it. */
	record_counts?: Record<string, number>;
	/** Whether `data` is everything held. Each high-volume section is capped in
	 *  this response; `false` means at least one was cut (named in
	 *  `truncated_sections`) and the whole set is the download at
	 *  `full_bundle_url` (decisions §369). Optional only for an older server. */
	complete?: boolean;
	truncated_sections?: string[];
	/** API-relative path of the complete, uncapped bundle for this request
	 *  (`GET /api/privacy/dsar/{request_id}/bundle`). */
	full_bundle_url?: string | null;
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
	/** Storage leg: `documents_failed > 0` means the erasure is INCOMPLETE and
	 *  should be re-run (`backend/app/schemas/privacy.py::ErasureResponse`). */
	documents_deleted: number;
	documents_retained: number;
	documents_failed: number;
	passkeys_deleted: number;
	sessions_revoked: number;
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
