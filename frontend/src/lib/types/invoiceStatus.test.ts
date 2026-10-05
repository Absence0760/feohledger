import { describe, it, expect } from 'vitest';
import {
	INVOICE_STATUSES,
	INVOICE_STATUS_LABEL_KEYS,
	IMMUTABLE_STATUSES,
	SYSTEM_MANAGED_STATUSES,
	VALID_TRANSITIONS,
	invoiceStatusLabelKey,
	type InvoiceStatus
} from './invoice';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Drift guard for the invoice-status LABELS, the sibling of
 * `paymentStatus.test.ts`.
 *
 * `Record<InvoiceStatus, MessageKey>` already fails `pnpm check` when a status
 * is added with no key — but a key naming nothing in the catalogue typechecks
 * fine and renders the raw key string (`m()` falls back key → raw), which is
 * how `invoices.status.readyForReview` would reach a badge as literal text.
 * This is the runtime half.
 */
describe('INVOICE_STATUS_LABEL_KEYS', () => {
	it('names a real, non-empty catalogue key for every status', () => {
		for (const status of INVOICE_STATUSES) {
			const key = INVOICE_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key].trim().length).toBeGreaterThan(0);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of INVOICE_STATUSES) {
			expect(en[INVOICE_STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});

	it('labels every status the other status sets name', () => {
		// The three sets are read one after another on the same row: a status
		// that can be selected, transitioned to, or refused as immutable is a
		// status the UI shows the user by name. One with a set membership but
		// no label renders a blank chip / option.
		const named = new Set<string>([
			...SYSTEM_MANAGED_STATUSES,
			...IMMUTABLE_STATUSES,
			...Object.keys(VALID_TRANSITIONS),
			...Object.values(VALID_TRANSITIONS).flat()
		]);
		for (const status of named) {
			expect(invoiceStatusLabelKey(status), `${status} has no label`).toBeTruthy();
		}
	});

	it('resolves a known status and returns null otherwise', () => {
		for (const status of INVOICE_STATUSES) {
			expect(invoiceStatusLabelKey(status)).toBe(INVOICE_STATUS_LABEL_KEYS[status]);
		}
		// `StatusBadge` renders the raw value — visible and searchable —
		// rather than a blank pill for a status this build doesn't know. The
		// assistant's tool results carry a status straight off the wire, so
		// this is a real runtime path, not a theoretical one.
		expect(invoiceStatusLabelKey('archived')).toBeNull();
	});

	it('keeps `pending` labelled as the extraction step, not "Pending"', () => {
		// Called out on its own: `pending` is the one status whose label is
		// deliberately NOT its enum value — the invoice is mid-extraction, and
		// "Pending" would read as "waiting for a human" beside
		// `ready_for_review`, which is the status that actually means that.
		const status: InvoiceStatus = 'pending';
		expect(en[INVOICE_STATUS_LABEL_KEYS[status]]).toBe('Extracting');
	});
});
