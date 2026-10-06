import { describe, it, expect } from 'vitest';
import {
	ENTRY_BULK_STATUS_TARGETS,
	VALID_TRANSITIONS,
	commonTransitions,
	inInvoiceEntryWindow,
	type InvoiceStatus
} from './invoice';

// The frontend transition map must mirror the backend workflow_engine
// VALID_TRANSITIONS for the user-selectable manual moves. Offering a target
// the backend rejects produces a guaranteed 409 on every selected row (the
// `new → rejected` / `approved → rejected` bug — `rejected` is only reachable
// from `ready_for_review`).
const BACKEND_TRANSITIONS: Record<string, string[]> = {
	new: ['pending', 'ready_for_review', 'approved', 'done'],
	pending: ['ready_for_review', 'approved', 'failed'],
	ready_for_review: ['approved', 'rejected'],
	approved: ['sending_to_erp', 'payment_scheduled', 'done'],
	rejected: ['ready_for_review', 'new'],
	sending_to_erp: ['sent_to_erp', 'failed'],
	sent_to_erp: ['posted_in_erp', 'done'],
	posted_in_erp: ['payment_scheduled', 'done'],
	payment_scheduled: ['paid', 'approved'],
	paid: ['done', 'approved'],
	done: [],
	failed: ['pending', 'sending_to_erp']
};

describe('VALID_TRANSITIONS', () => {
	it('never offers a target the backend would reject', () => {
		for (const [from, targets] of Object.entries(VALID_TRANSITIONS)) {
			const allowed = new Set(BACKEND_TRANSITIONS[from] ?? []);
			for (const to of targets) {
				expect(allowed.has(to), `${from} → ${to} is not a valid backend transition`).toBe(
					true
				);
			}
		}
	});

	it('does not offer rejected from new or approved (the 409 bug)', () => {
		expect(VALID_TRANSITIONS.new).not.toContain('rejected');
		expect(VALID_TRANSITIONS.approved).not.toContain('rejected');
	});

	it('still allows rejecting from ready_for_review', () => {
		expect(VALID_TRANSITIONS.ready_for_review).toContain('rejected');
	});
});

describe('commonTransitions', () => {
	it('returns the intersection of valid targets across a mixed selection', () => {
		// new and approved share only `done` now that `rejected` was removed.
		const common = commonTransitions(['new', 'approved'] as InvoiceStatus[]);
		expect(common).toEqual(['done']);
	});

	it('returns an empty list for an empty selection', () => {
		expect(commonTransitions([])).toEqual([]);
	});
});

describe('the AP clerk\'s entry reach (backend api/invoice_entry.py)', () => {
	it('narrows bulk targets to submit / resubmit / back-to-draft', () => {
		expect(commonTransitions(['new'], ENTRY_BULK_STATUS_TARGETS)).toEqual(['ready_for_review']);
		expect(commonTransitions(['rejected'], ENTRY_BULK_STATUS_TARGETS).sort()).toEqual([
			'new',
			'ready_for_review'
		]);
		expect(commonTransitions(['ready_for_review'], ENTRY_BULK_STATUS_TARGETS)).toEqual([]);
		expect(commonTransitions(['approved'], ENTRY_BULK_STATUS_TARGETS)).toEqual([]);
	});

	it('never lets a clerk set an approval or closing status in bulk', () => {
		for (const s of ['approved', 'rejected', 'done', 'pending'] as InvoiceStatus[]) {
			expect(ENTRY_BULK_STATUS_TARGETS.has(s)).toBe(false);
		}
	});

	it('closes the entry window at approval, including an approved invoice whose ERP push failed', () => {
		expect(inInvoiceEntryWindow('new', null)).toBe(true);
		expect(inInvoiceEntryWindow('ready_for_review', null)).toBe(true);
		expect(inInvoiceEntryWindow('failed', null)).toBe(true);
		expect(inInvoiceEntryWindow('failed', 'Some Approver')).toBe(false);
		expect(inInvoiceEntryWindow('approved', 'Some Approver')).toBe(false);
		expect(inInvoiceEntryWindow('done', null)).toBe(false);
	});
});
