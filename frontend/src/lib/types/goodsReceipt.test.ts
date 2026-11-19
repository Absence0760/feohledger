import { describe, it, expect } from 'vitest';
import {
	CANCELLED_GR_STATUSES,
	isCancelledGoodsReceipt,
	goodsReceiptTone,
	goodsReceiptStatusLabelKey
} from './goodsReceipt';

// Mirrors `backend/app/services/po_matching.py::CANCELLED_GR_STATUSES`. If that
// frozenset gains a spelling and this doesn't, a reversed receipt goes back to
// rendering as a successful delivery while the matcher silently ignores it —
// the two disagreeing is the defect, so the sets are pinned together.
const BACKEND_CANCELLED_GR_STATUSES = ['cancelled', 'canceled', 'void', 'voided', 'reversed'];

describe('goods-receipt status semantics', () => {
	it('mirrors the backend cancelled-status set exactly', () => {
		expect([...CANCELLED_GR_STATUSES].sort()).toEqual([...BACKEND_CANCELLED_GR_STATUSES].sort());
	});

	it('matches case-insensitively and tolerates surrounding whitespace', () => {
		// The column is free-form String(30) with no normalisation on write.
		expect(isCancelledGoodsReceipt('Cancelled')).toBe(true);
		expect(isCancelledGoodsReceipt('  REVERSED ')).toBe(true);
		expect(isCancelledGoodsReceipt('voided')).toBe(true);
	});

	it('treats a received or unknown status as a real delivery', () => {
		expect(isCancelledGoodsReceipt('received')).toBe(false);
		expect(isCancelledGoodsReceipt('partial')).toBe(false);
		expect(isCancelledGoodsReceipt(null)).toBe(false);
		expect(isCancelledGoodsReceipt('')).toBe(false);
	});

	it('badges a cancelled receipt muted and everything else success', () => {
		for (const s of BACKEND_CANCELLED_GR_STATUSES) {
			expect(goodsReceiptTone(s), s).toBe('muted');
		}
		expect(goodsReceiptTone('received')).toBe('success');
	});
});

describe('goodsReceiptStatusLabelKey', () => {
	it('labels the statuses the backend writes', () => {
		expect(goodsReceiptStatusLabelKey('received')).toBe('goodsReceipts.status.received');
		expect(goodsReceiptStatusLabelKey('cancelled')).toBe('goodsReceipts.status.cancelled');
	});

	it('labels every cancelled spelling, synonyms sharing one label', () => {
		for (const s of BACKEND_CANCELLED_GR_STATUSES) {
			expect(goodsReceiptStatusLabelKey(s)).not.toBeNull();
		}
		expect(goodsReceiptStatusLabelKey('canceled')).toBe('goodsReceipts.status.cancelled');
		expect(goodsReceiptStatusLabelKey('voided')).toBe('goodsReceipts.status.void');
	});

	it('matches like isCancelledGoodsReceipt: trimmed and case-folded', () => {
		expect(goodsReceiptStatusLabelKey('  Received ')).toBe('goodsReceipts.status.received');
	});

	it('returns null for an unknown or missing status, so the caller shows it raw', () => {
		expect(goodsReceiptStatusLabelKey('partially_received')).toBeNull();
		expect(goodsReceiptStatusLabelKey(null)).toBeNull();
		expect(goodsReceiptStatusLabelKey('')).toBeNull();
	});
});
