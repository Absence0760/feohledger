// The exception-queue `exception_type` taxonomy, with the i18n key carrying
// each label rather than the English string itself.
//
// `Exception.exception_type` is a plain `String(50)` server-side (no DB enum),
// so the roster the backend declares is the contract: `EXCEPTION_TYPES` in
// `backend/app/services/exception_lifecycle.py`, labelled for the queue by
// `backend/app/api/exceptions.py::EXCEPTION_TYPE_LABELS`. `exception.test.ts`
// reads that Python source and fails if either drifts.
//
// Why this module exists: the agent decision log rendered its type cell as
// `exception_type.replace(/_/g, ' ')`, an English-only derivation that printed
// `po mismatch` inside an otherwise-translated table — and printed it in a
// DIFFERENT wording from the queue one tab away, which labels the same row
// `PO Mismatch` from the server's map. A de-underscored raw key cannot be
// translated at all (`decisions.md` §149 is the same defect on the screening
// verdict), so the taxonomy needed a real label map.
//
// The lifecycle `status` vocabulary lives here too, and landed in ONE change
// with every surface that renders it — the queue's badge, the AI-agents
// runnable-queue cell and that panel's run dialog. Keying one without the
// others is how two surfaces come to name a single status differently, which
// is the defect above with a different column name.

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';

/**
 * Every `Exception.exception_type` the platform raises, in the backend's
 * declaration order.
 *
 * `amount_exceeded` is on the roster but nothing raises it any more — the
 * backend keeps it, and its label, because historical rows still carry it and
 * must not regress to rendering a raw key. The same reasoning keeps it here.
 */
export const EXCEPTION_TYPES = [
	'duplicate',
	'po_mismatch',
	'fraud_flag',
	'extraction_failed',
	'unverified_vendor',
	'review_rejected',
	'amount_exceeded',
	'missing_data',
	'quality_hold',
	'price_variance',
	'contract_noncompliant',
	'erp_reconciliation',
	'line_total_mismatch',
	'payment_compliance_hold',
	'payment_reconciliation'
] as const;

export type ExceptionType = (typeof EXCEPTION_TYPES)[number];

/**
 * The i18n key carrying each type's label — never the English string itself.
 *
 * Each ENGLISH value is byte-identical to the matching entry in
 * `EXCEPTION_TYPE_LABELS`, which is what the queue still renders through the
 * wire's `type_label`. `exception.test.ts` asserts that equality, so the two
 * surfaces cannot disagree in English while only one of them is translated.
 */
export const EXCEPTION_TYPE_LABEL_KEYS: Record<ExceptionType, MessageKey> = {
	duplicate: 'exceptions.type.duplicate',
	po_mismatch: 'exceptions.type.poMismatch',
	fraud_flag: 'exceptions.type.fraudFlag',
	extraction_failed: 'exceptions.type.extractionFailed',
	unverified_vendor: 'exceptions.type.unverifiedVendor',
	review_rejected: 'exceptions.type.reviewRejected',
	amount_exceeded: 'exceptions.type.amountExceeded',
	missing_data: 'exceptions.type.missingData',
	quality_hold: 'exceptions.type.qualityHold',
	price_variance: 'exceptions.type.priceVariance',
	contract_noncompliant: 'exceptions.type.contractNoncompliant',
	erp_reconciliation: 'exceptions.type.erpReconciliation',
	line_total_mismatch: 'exceptions.type.lineTotalMismatch',
	payment_compliance_hold: 'exceptions.type.paymentComplianceHold',
	payment_reconciliation: 'exceptions.type.paymentReconciliation'
};

/**
 * The message key for an exception type, or `null` for one this build has no
 * wording for.
 *
 * Tolerant on purpose — the same rule `screeningCategoryLabelKey` states. The
 * column is a plain `String(50)` and a historical row can carry a type this
 * frontend predates, so the caller renders the server's own label, or
 * {@link exceptionTypeFallback}, rather than dropping the cell.
 */
export function exceptionTypeLabelKey(type: string): MessageKey | null {
	return EXCEPTION_TYPE_LABEL_KEYS[type as ExceptionType] ?? null;
}

/** Readable stand-in for an unrecognised type: the raw key, de-underscored. */
export function exceptionTypeFallback(type: string): string {
	return type.replace(/_/g, ' ');
}

/**
 * Every `Exception.status` the lifecycle produces.
 *
 * `open` is the column default (`models/exception.py`); the other three are the
 * whole image of `exception_lifecycle.RESOLUTION_STATUSES` (the queue verbs
 * `resolve` / `escalate` / `dismiss`), and `GET /api/exceptions/summary`
 * enumerates exactly these four as the chip counts. The order is the chips'
 * order, which is also the lifecycle's: actionable first, terminal after.
 *
 * `escalated` is in BOTH halves — `ACTIONABLE_STATUSES` and the resolution
 * map — because escalating is not a resolution: it records why a human is
 * needed and leaves the row open with its SLA clock running.
 */
export const EXCEPTION_STATUSES = ['open', 'escalated', 'resolved', 'dismissed'] as const;

export type ExceptionStatus = (typeof EXCEPTION_STATUSES)[number];

/**
 * The i18n key carrying each status' label — never the English string itself.
 *
 * These are the SAME four keys the queue's own status `FilterChips` already
 * read, deliberately rather than a second `exceptions.status.*` set: the chip
 * and the badges it filters name one status, and two key sets is how they come
 * to name it two ways once a translator revises one of them. A chip reading
 * `Gelöst` above rows badged `resolved` was the shipped state.
 */
export const EXCEPTION_STATUS_LABEL_KEYS: Record<ExceptionStatus, MessageKey> = {
	open: 'exceptions.filter.open',
	escalated: 'exceptions.filter.escalated',
	resolved: 'exceptions.filter.resolved',
	dismissed: 'exceptions.filter.dismissed'
};

/**
 * Badge tone per status. Total over {@link ExceptionStatus}, so a status with a
 * tone but no label — a coloured pill printing a raw wire value — is a compile
 * error. Read it through {@link exceptionStatusTone}.
 *
 * `open` (amber) and `escalated` (red) keep separate tones on purpose —
 * escalation is what says a human deadline has already passed, and folding
 * both onto `warning` would erase the only scannable difference between an
 * exception in the queue and one that has run out of time. `dismissed` keeps
 * the flat `neutral` chip: a dismissal is the absence of a finding, not a
 * state to hunt for.
 */
export const EXCEPTION_STATUS_TONES: Record<ExceptionStatus, BadgeTone> = {
	open: 'warning',
	escalated: 'danger',
	resolved: 'success',
	dismissed: 'neutral'
};

/** An unknown status gets the flat chip rather than none. */
export function exceptionStatusTone(status: string): BadgeTone {
	return EXCEPTION_STATUS_TONES[status as ExceptionStatus] ?? 'neutral';
}

/**
 * The message key for a lifecycle status, or `null` for one this build has no
 * wording for.
 *
 * Tolerant for the same reason {@link exceptionTypeLabelKey} is: `status` is a
 * plain `String(30)` with no DB enum behind it, so a row written by a future
 * build can carry a status this one predates. The caller then prints the raw
 * value — which is what every surface printed before this map existed, and is
 * honest — rather than an empty badge.
 */
export function exceptionStatusLabelKey(status: string): MessageKey | null {
	return EXCEPTION_STATUS_LABEL_KEYS[status as ExceptionStatus] ?? null;
}

/**
 * Every `Exception.severity` the platform raises.
 *
 * The third vocabulary on the row, and the last one that printed raw. The
 * backend declares it as `exception_lifecycle.EXCEPTION_SEVERITY_RANK` — the
 * rank the queue's severity sort uses — as well as in a comment on the column
 * (`models/exception.py`: `# error, warning, info`). `exception.test.ts` pins
 * this against the rank map (members and order), that comment, and every
 * `severity="…"` literal a raising site actually writes, so a fourth severity
 * fails there whichever way it arrives.
 *
 * Order is worst-first, which is the order a triager scans and the order the
 * rank map declares — the severity chips render in it.
 */
export const EXCEPTION_SEVERITIES = ['error', 'warning', 'info'] as const;

export type ExceptionSeverity = (typeof EXCEPTION_SEVERITIES)[number];

/**
 * The i18n key carrying each severity's label — never the English string
 * itself.
 *
 * A fresh `exceptions.severity.*` namespace, minted when the Sev cell was the
 * only surface naming a severity. The queue's severity FILTER CHIPS now read
 * these same keys — the {@link EXCEPTION_STATUS_LABEL_KEYS} arrangement, one
 * key set for a chip and the badges it filters — so the two cannot come to
 * name one severity two ways.
 */
export const EXCEPTION_SEVERITY_LABEL_KEYS: Record<ExceptionSeverity, MessageKey> = {
	error: 'exceptions.severity.error',
	warning: 'exceptions.severity.warning',
	info: 'exceptions.severity.info'
};

/**
 * The message key for a severity, or `null` for one this build has no wording
 * for.
 *
 * Tolerant for the reason {@link exceptionStatusLabelKey} is: `severity` is a
 * plain `String(20)` with no DB enum, so a row written by a later build can
 * carry a severity this one predates. The caller then prints the raw value —
 * what every row printed before this map existed.
 */
export function exceptionSeverityLabelKey(severity: string): MessageKey | null {
	return EXCEPTION_SEVERITY_LABEL_KEYS[severity as ExceptionSeverity] ?? null;
}
