import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
	RECEIPT_ENTRY_ROLES,
	isCancelledPO,
	remainingQuantity
} from './goodsReceipt.ts';

const backend = (rel: string) =>
	readFileSync(fileURLToPath(new URL(`../../../../backend/${rel}`, import.meta.url)), 'utf8');

describe('remainingQuantity', () => {
	const line = (quantity: number | null, received: number) => ({
		id: 'l',
		description: null,
		quantity,
		quantity_received: received
	});

	it('is exact at the column scale', () => {
		expect(remainingQuantity(line(10, 9.3))).toBe(0.7);
		expect(remainingQuantity(line(1.0001, 0.0001))).toBe(1);
	});

	it('never goes negative on an over-receipt', () => {
		expect(remainingQuantity(line(10, 12))).toBe(0);
	});

	it('is zero for a line with no ordered quantity', () => {
		expect(remainingQuantity(line(null, 0))).toBe(0);
	});
});

describe('backend mirrors', () => {
	it('cancelled PO statuses match services/goods_receipts.CANCELLED_PO_STATUSES', () => {
		const src = backend('app/services/goods_receipts.py');
		const set = /CANCELLED_PO_STATUSES = frozenset\(\{([^}]*)\}\)/.exec(src)?.[1] ?? '';
		const statuses = [...set.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
		expect(statuses.length).toBeGreaterThan(0);
		for (const s of statuses) expect(isCancelledPO(s.toUpperCase())).toBe(true);
		expect(isCancelledPO('open')).toBe(false);
	});

	it('entry roles match api/goods_receipts.RECEIPT_ENTRY_ROLES', () => {
		const src = backend('app/api/goods_receipts.py');
		const tuple = /RECEIPT_ENTRY_ROLES = \(([^)]*)\)/.exec(src)?.[1] ?? '';
		const names = tuple.split(',').map((s) => s.trim().replace(/^ROLE_/, '').toLowerCase());
		expect([...RECEIPT_ENTRY_ROLES].sort()).toEqual(names.filter(Boolean).sort());
	});
});
