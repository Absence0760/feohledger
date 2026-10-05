import { describe, it, expect } from 'vitest';
import { INVOICE_STATUSES } from './invoice';
import { PAYMENT_STATUSES } from './payment';
import {
	PORTAL_INVOICE_PHASE_ORDER,
	PORTAL_INVOICE_PHASE_LABEL_KEYS,
	PORTAL_INVOICE_STATUS_PHASES,
	PORTAL_INVOICE_PHASES,
	PORTAL_PAYMENT_PHASE_ORDER,
	PORTAL_PAYMENT_PHASE_LABEL_KEYS,
	PORTAL_PAYMENT_STATUS_PHASES,
	PORTAL_PAYMENT_PHASES,
	portalInvoicePhase,
	portalPaymentPhase,
	portalInvoiceStatusLabelKey,
	portalPaymentStatusLabelKey,
	type PortalInvoicePhase,
	type PortalPaymentPhase
} from './portalStatus';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Drift guard + regression test for the persona-supplier audit finding
 * (issue #328): the supplier portal used to render `inv.status` / `p.status`
 * verbatim — a vendor would see raw internal-jargon values like
 * `sending_to_erp`, `posted_in_erp`, `ready_for_review` straight from
 * `backend/app/models/invoice.py::InvoiceStatus`.
 *
 * Since the i18n slice the phase is a stable ID with a message KEY, not the
 * English label — so this file additionally pins the phase MEMBERSHIP. Which
 * statuses sit behind a chip is what the list actually sends as `?status=`;
 * a status quietly changing chips is a user-visible filter regression that no
 * type can catch, because every phase id is assignable to every status.
 *
 * The status vocabulary below is enumerated from the BACKEND, not from the
 * maps under test — a map that forgot a status would otherwise agree with
 * itself. Sources:
 *   - `backend/app/models/invoice.py::InvoiceStatus` (12 values).
 *   - `backend/app/services/payment_adapters/base.py::PaymentStatus` (pending,
 *     submitted, processing, completed, failed, cancelled) plus the two
 *     `api/payments.py` sets directly: `voided` (POST .../void) and
 *     `pending_compliance` (the compliance hold).
 */

const BACKEND_INVOICE_STATUSES = [
	'new',
	'pending',
	'ready_for_review',
	'approved',
	'rejected',
	'sending_to_erp',
	'sent_to_erp',
	'posted_in_erp',
	'payment_scheduled',
	'paid',
	'done',
	'failed'
] as const;

const BACKEND_PAYMENT_STATUSES = [
	'pending',
	'pending_compliance',
	'submitted',
	'processing',
	'completed',
	'failed',
	'cancelled',
	'voided'
] as const;

// Internal enum values that must NEVER be the literal vendor-facing label —
// a portal reader with no AP context has no use for the ERP pipeline's
// internal step names.
const INTERNAL_ONLY_INVOICE_STATUSES = [
	'sending_to_erp',
	'sent_to_erp',
	'posted_in_erp',
	'ready_for_review',
	'failed'
] as const;

const INTERNAL_ONLY_PAYMENT_STATUSES = ['pending_compliance', 'submitted', 'processing'] as const;

/**
 * The exact phase → internal-status membership, written out.
 *
 * This is the byte-for-byte grouping the label-string derivation produced
 * before the phase became an id: "Submitted" → new; "Processing" → pending,
 * sending_to_erp, sent_to_erp, posted_in_erp, failed; "Under Review" →
 * ready_for_review; "Approved" → approved; "Payment Scheduled" →
 * payment_scheduled; "Paid" → paid; "Completed" → done; "Rejected" →
 * rejected. Changing a line here is changing what a supplier's filter chip
 * asks the API for, so it must be a deliberate edit.
 */
const EXPECTED_INVOICE_MEMBERSHIP: Record<PortalInvoicePhase, string[]> = {
	submitted: ['new'],
	processing: ['pending', 'sending_to_erp', 'sent_to_erp', 'posted_in_erp', 'failed'],
	under_review: ['ready_for_review'],
	approved: ['approved'],
	payment_scheduled: ['payment_scheduled'],
	paid: ['paid'],
	completed: ['done'],
	rejected: ['rejected']
};

/** Same, for the payment-history chips ("Scheduled" → pending; "Processing" →
 *  pending_compliance, submitted, processing; …; "Cancelled" → cancelled,
 *  voided). */
const EXPECTED_PAYMENT_MEMBERSHIP: Record<PortalPaymentPhase, string[]> = {
	scheduled: ['pending'],
	processing: ['pending_compliance', 'submitted', 'processing'],
	completed: ['completed'],
	failed: ['failed'],
	cancelled: ['cancelled', 'voided']
};

describe('portal invoice phases', () => {
	it('classifies every status the backend can persist', () => {
		for (const status of BACKEND_INVOICE_STATUSES) {
			expect(
				(PORTAL_INVOICE_STATUS_PHASES as Record<string, string>)[status],
				`${status} has no portal-facing phase`
			).toBeTruthy();
		}
		// And the frontend union it is keyed on covers exactly that vocabulary.
		expect([...INVOICE_STATUSES].sort()).toEqual([...BACKEND_INVOICE_STATUSES].sort());
	});

	it('groups statuses exactly as the label-derived chips did', () => {
		const actual = Object.fromEntries(
			PORTAL_INVOICE_PHASES.map((c) => [c.phase, [...c.statuses].sort()])
		);
		const expected = Object.fromEntries(
			Object.entries(EXPECTED_INVOICE_MEMBERSHIP).map(([phase, statuses]) => [
				phase,
				[...statuses].sort()
			])
		);
		expect(actual).toEqual(expected);
	});

	it('reaches every backend status through exactly one chip', () => {
		for (const status of BACKEND_INVOICE_STATUSES) {
			const owning = PORTAL_INVOICE_PHASES.filter((c) =>
				(c.statuses as readonly string[]).includes(status)
			);
			expect(owning.length, `${status} is in ${owning.length} phase chips, expected 1`).toBe(1);
		}
	});

	it('orders the chips by the declared phase order, dropping none', () => {
		expect(PORTAL_INVOICE_PHASES.map((c) => c.phase)).toEqual([...PORTAL_INVOICE_PHASE_ORDER]);
	});

	it('names a real, non-empty catalogue key for every phase', () => {
		for (const phase of PORTAL_INVOICE_PHASE_ORDER) {
			const key = PORTAL_INVOICE_PHASE_LABEL_KEYS[phase];
			expect(key, `${phase} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${phase} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key].trim().length).toBeGreaterThan(0);
		}
	});

	it('never renders an internal-only status as its own raw value', () => {
		for (const status of INTERNAL_ONLY_INVOICE_STATUSES) {
			expect(
				en[portalInvoiceStatusLabelKey(status)],
				`${status} rendered verbatim to the supplier portal`
			).not.toBe(status);
		}
	});

	it('fails soft to the neutral phase, never to a raw value', () => {
		// Unlike `runStatusLabelKey` and friends, this accessor never returns
		// null: a null would send the caller back to the raw wire value, which
		// is precisely what must not reach a supplier.
		expect(portalInvoicePhase('some_future_internal_status')).toBe('processing');
		expect(en[portalInvoiceStatusLabelKey('some_future_internal_status')]).not.toBe(
			'some_future_internal_status'
		);
		for (const status of INVOICE_STATUSES) {
			expect(en[portalInvoiceStatusLabelKey(status)]).not.toBe(status);
		}
	});
});

describe('portal payment phases', () => {
	it('classifies every status the backend can persist', () => {
		for (const status of BACKEND_PAYMENT_STATUSES) {
			expect(
				(PORTAL_PAYMENT_STATUS_PHASES as Record<string, string>)[status],
				`${status} has no portal-facing phase`
			).toBeTruthy();
		}
		expect([...PAYMENT_STATUSES].sort()).toEqual([...BACKEND_PAYMENT_STATUSES].sort());
	});

	it('groups statuses exactly as the label-derived chips did', () => {
		const actual = Object.fromEntries(
			PORTAL_PAYMENT_PHASES.map((c) => [c.phase, [...c.statuses].sort()])
		);
		const expected = Object.fromEntries(
			Object.entries(EXPECTED_PAYMENT_MEMBERSHIP).map(([phase, statuses]) => [
				phase,
				[...statuses].sort()
			])
		);
		expect(actual).toEqual(expected);
	});

	it('reaches every backend status through exactly one chip', () => {
		for (const status of BACKEND_PAYMENT_STATUSES) {
			const owning = PORTAL_PAYMENT_PHASES.filter((c) =>
				(c.statuses as readonly string[]).includes(status)
			);
			expect(owning.length, `${status} is in ${owning.length} phase chips, expected 1`).toBe(1);
		}
	});

	it('orders the chips by the declared phase order, dropping none', () => {
		expect(PORTAL_PAYMENT_PHASES.map((c) => c.phase)).toEqual([...PORTAL_PAYMENT_PHASE_ORDER]);
	});

	it('names a real, non-empty catalogue key for every phase', () => {
		for (const phase of PORTAL_PAYMENT_PHASE_ORDER) {
			const key = PORTAL_PAYMENT_PHASE_LABEL_KEYS[phase];
			expect(key, `${phase} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${phase} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key].trim().length).toBeGreaterThan(0);
		}
	});

	it('never renders an internal-only status as its own raw value', () => {
		for (const status of INTERNAL_ONLY_PAYMENT_STATUSES) {
			expect(
				en[portalPaymentStatusLabelKey(status)],
				`${status} rendered verbatim to the supplier portal`
			).not.toBe(status);
		}
	});

	it('fails soft to the neutral phase, never to a raw value', () => {
		expect(portalPaymentPhase('some_future_internal_status')).toBe('processing');
		for (const status of PAYMENT_STATUSES) {
			expect(en[portalPaymentStatusLabelKey(status)]).not.toBe(status);
		}
	});
});

describe('phase ids are locale-independent', () => {
	it('uses no phase id that reads as a display label', () => {
		// The regression this redesign closes: the id used to BE the English
		// label ("Payment Scheduled"), so grouping and URL matching both broke
		// the moment a catalogue translated it. Snake_case lower-case ids can't
		// be mistaken for one.
		for (const phase of [...PORTAL_INVOICE_PHASE_ORDER, ...PORTAL_PAYMENT_PHASE_ORDER]) {
			expect(phase, `${phase} is not a stable id`).toMatch(/^[a-z][a-z_]*$/);
		}
	});

	it('never uses the phase id as its own English label', () => {
		for (const phase of PORTAL_INVOICE_PHASE_ORDER) {
			expect(en[PORTAL_INVOICE_PHASE_LABEL_KEYS[phase]]).not.toBe(phase);
		}
		for (const phase of PORTAL_PAYMENT_PHASE_ORDER) {
			expect(en[PORTAL_PAYMENT_PHASE_LABEL_KEYS[phase]]).not.toBe(phase);
		}
	});
});
