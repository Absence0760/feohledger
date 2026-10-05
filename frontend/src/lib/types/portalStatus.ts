import type { MessageKey } from '#lib/i18n/messages.ts';
import type { InvoiceStatus } from './invoice';
import type { PaymentStatus } from './payment';

/**
 * Vendor-facing status PHASES for the supplier portal.
 *
 * The supplier portal is not the internal AP console: a vendor doesn't need
 * to distinguish `sending_to_erp` from `posted_in_erp`, and the raw
 * workflow-engine / payment-rail enum values (see
 * `backend/app/models/invoice.py::InvoiceStatus` and
 * `backend/app/schemas/payment.py::PaymentStatus`) are internal jargon that
 * must never render verbatim in front of a supplier (persona-supplier audit
 * finding, issue #328).
 *
 * These maps collapse the internal state machines into the handful of phases
 * a vendor actually cares about — Submitted / Processing / Approved / Paid /
 * Rejected — while staying keyed on the SAME `InvoiceStatus` /
 * `PaymentStatus` types the internal app uses (`#lib/types/invoice`,
 * `#lib/types/payment`): a `Record<InvoiceStatus, …>` fails typechecking here
 * until a new backend status is classified, instead of falling through to the
 * raw string.
 *
 * **A phase is an ID, not a label.** It used to be the English label itself:
 * the chip set was derived by `Object.entries(LABELS)` grouping statuses whose
 * label STRING matched, so the phase identity WAS the display text. That makes
 * i18n a data change rather than a presentation one — translate "Processing"
 * and the four statuses behind it stop grouping (each lands in its own chip),
 * or two phases whose translations collide silently merge into one. The
 * identity is now a stable snake_case id ({@link PortalInvoicePhase} /
 * {@link PortalPaymentPhase}), the display text is a message key, and the
 * status→phase assignment is written out rather than inferred — so a label
 * edit in any of the six catalogues cannot move a status between chips.
 *
 * The phase id is also what `?phase=` carries in the portal list URLs. The
 * WIRE values are unchanged: each chip still sends the raw internal `status=`
 * values behind it, exactly as before.
 */

// --- Invoice phases -------------------------------------------------------

/** Chip order (roughly lifecycle order). Total over {@link PortalInvoicePhase}. */
export const PORTAL_INVOICE_PHASE_ORDER = [
	'submitted',
	'processing',
	'under_review',
	'approved',
	'payment_scheduled',
	'paid',
	'completed',
	'rejected'
] as const;

export type PortalInvoicePhase = (typeof PORTAL_INVOICE_PHASE_ORDER)[number];

/**
 * The i18n key carrying each phase's vendor-facing label — never the English
 * string itself. `Record<PortalInvoicePhase, MessageKey>`, so a phase with no
 * wording is a compile error; `portalStatus.test.ts` proves each key resolves
 * in the catalogue.
 */
export const PORTAL_INVOICE_PHASE_LABEL_KEYS: Record<PortalInvoicePhase, MessageKey> = {
	submitted: 'portal.invoices.phase.submitted',
	processing: 'portal.invoices.phase.processing',
	under_review: 'portal.invoices.phase.underReview',
	approved: 'portal.invoices.phase.approved',
	payment_scheduled: 'portal.invoices.phase.paymentScheduled',
	paid: 'portal.invoices.phase.paid',
	completed: 'portal.invoices.phase.completed',
	rejected: 'portal.invoices.phase.rejected'
};

/**
 * Which vendor-facing phase each internal invoice status collapses into.
 *
 * `done` is reachable straight from `approved` (a workflow with no ERP step
 * and no scheduled payment) as well as after `paid` — it doesn't always mean
 * "paid", so it gets the vendor-neutral `completed` phase rather than
 * asserting something that may not be true. `failed` is a system-managed retry
 * state (`failed → pending | sending_to_erp`), not a rejection and not
 * something a vendor can act on, so it reads as `processing`.
 */
export const PORTAL_INVOICE_STATUS_PHASES: Record<InvoiceStatus, PortalInvoicePhase> = {
	new: 'submitted',
	pending: 'processing',
	ready_for_review: 'under_review',
	approved: 'approved',
	rejected: 'rejected',
	sending_to_erp: 'processing',
	sent_to_erp: 'processing',
	posted_in_erp: 'processing',
	payment_scheduled: 'payment_scheduled',
	paid: 'paid',
	done: 'completed',
	failed: 'processing'
};

/**
 * The neutral phase an unclassified status falls back to.
 *
 * Deliberately NOT the `null` that `runStatusLabelKey` and its siblings return
 * (whose callers then render the raw wire value): this module's whole reason
 * to exist is that the raw value must never reach a supplier. A not-yet-
 * classified backend status, a stale portal build or a test fixture reads as
 * "Processing" — true of anything mid-pipeline — rather than leaking
 * `posted_in_erp` into the vendor's browser.
 */
const FALLBACK_INVOICE_PHASE: PortalInvoicePhase = 'processing';

/** The vendor-facing phase for an internal status; fail-soft, never raw. */
export function portalInvoicePhase(status: string): PortalInvoicePhase {
	return PORTAL_INVOICE_STATUS_PHASES[status as InvoiceStatus] ?? FALLBACK_INVOICE_PHASE;
}

/** The message key for an internal status' vendor-facing label. Always a key. */
export function portalInvoiceStatusLabelKey(status: string): MessageKey {
	return PORTAL_INVOICE_PHASE_LABEL_KEYS[portalInvoicePhase(status)];
}

export interface PortalInvoicePhaseChip {
	phase: PortalInvoicePhase;
	labelKey: MessageKey;
	statuses: InvoiceStatus[];
}

function invoiceStatusesFor(phase: PortalInvoicePhase): InvoiceStatus[] {
	return (Object.entries(PORTAL_INVOICE_STATUS_PHASES) as [InvoiceStatus, PortalInvoicePhase][])
		.filter(([, p]) => p === phase)
		.map(([status]) => status);
}

/** Ordered phase → internal-status mapping the portal invoice-filter chips
 *  render from. Phases with no backing status are dropped. */
export const PORTAL_INVOICE_PHASES: PortalInvoicePhaseChip[] = PORTAL_INVOICE_PHASE_ORDER.map(
	(phase) => ({
		phase,
		labelKey: PORTAL_INVOICE_PHASE_LABEL_KEYS[phase],
		statuses: invoiceStatusesFor(phase)
	})
).filter((chip) => chip.statuses.length > 0);

// --- Payment phases -------------------------------------------------------

/** Chip order for the portal payment-history list. */
export const PORTAL_PAYMENT_PHASE_ORDER = [
	'scheduled',
	'processing',
	'completed',
	'failed',
	'cancelled'
] as const;

export type PortalPaymentPhase = (typeof PORTAL_PAYMENT_PHASE_ORDER)[number];

export const PORTAL_PAYMENT_PHASE_LABEL_KEYS: Record<PortalPaymentPhase, MessageKey> = {
	scheduled: 'portal.payments.phase.scheduled',
	processing: 'portal.payments.phase.processing',
	completed: 'portal.payments.phase.completed',
	failed: 'portal.payments.phase.failed',
	cancelled: 'portal.payments.phase.cancelled'
};

/**
 * Which vendor-facing phase each internal payment status collapses into.
 *
 * `pending_compliance` is a sanctions/KYC hold the AP team resolves — from the
 * supplier's side it is indistinguishable from any other in-flight state, and
 * naming it would disclose a screening decision, so it reads as `processing`.
 * `voided` (a reversal) joins `cancelled`: both mean the money is not coming
 * through this payment.
 */
export const PORTAL_PAYMENT_STATUS_PHASES: Record<PaymentStatus, PortalPaymentPhase> = {
	pending: 'scheduled',
	pending_compliance: 'processing',
	submitted: 'processing',
	processing: 'processing',
	completed: 'completed',
	failed: 'failed',
	cancelled: 'cancelled',
	voided: 'cancelled'
};

/** Same fail-soft rule as {@link FALLBACK_INVOICE_PHASE}. */
const FALLBACK_PAYMENT_PHASE: PortalPaymentPhase = 'processing';

export function portalPaymentPhase(status: string): PortalPaymentPhase {
	return PORTAL_PAYMENT_STATUS_PHASES[status as PaymentStatus] ?? FALLBACK_PAYMENT_PHASE;
}

export function portalPaymentStatusLabelKey(status: string): MessageKey {
	return PORTAL_PAYMENT_PHASE_LABEL_KEYS[portalPaymentPhase(status)];
}

export interface PortalPaymentPhaseChip {
	phase: PortalPaymentPhase;
	labelKey: MessageKey;
	statuses: PaymentStatus[];
}

function paymentStatusesFor(phase: PortalPaymentPhase): PaymentStatus[] {
	return (Object.entries(PORTAL_PAYMENT_STATUS_PHASES) as [PaymentStatus, PortalPaymentPhase][])
		.filter(([, p]) => p === phase)
		.map(([status]) => status);
}

export const PORTAL_PAYMENT_PHASES: PortalPaymentPhaseChip[] = PORTAL_PAYMENT_PHASE_ORDER.map(
	(phase) => ({
		phase,
		labelKey: PORTAL_PAYMENT_PHASE_LABEL_KEYS[phase],
		statuses: paymentStatusesFor(phase)
	})
).filter((chip) => chip.statuses.length > 0);
