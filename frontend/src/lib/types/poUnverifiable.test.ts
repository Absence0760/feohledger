/**
 * Every reason the ERP PO sync can stamp on `total_unverifiable_reason` has a
 * catalogued sentence, read from the backend enum itself so a new
 * `PoSkipReason` member cannot reach the purchase-orders page as raw code.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import { PO_UNVERIFIABLE_REASON_KEYS, poUnverifiableReasonKey } from './poUnverifiable.ts';

const BASE = readFileSync(
	new URL('../../../../backend/app/services/erp_adapters/base.py', import.meta.url),
	'utf8'
);

function backendReasons(): string[] {
	const body = BASE.split('class PoSkipReason')[1]?.split('\n@dataclass')[0] ?? '';
	return [...body.matchAll(/^ {4}[a-z_]+ = "([a-z_]+)"/gm)].map((m) => m[1]).sort();
}

describe('PO unverifiable reasons', () => {
	it('maps exactly the backend PoSkipReason codes', () => {
		const codes = backendReasons();
		expect(codes.length).toBeGreaterThan(0);
		expect(Object.keys(PO_UNVERIFIABLE_REASON_KEYS).sort()).toEqual(codes);
	});

	it('resolves every code, and an unknown or missing one, to an English sentence', () => {
		for (const code of [...backendReasons(), 'something_newer', null]) {
			const key = poUnverifiableReasonKey(code);
			expect(EN_ALL[key], key).toBeTruthy();
		}
		expect(poUnverifiableReasonKey('something_newer')).toBe(
			'purchaseOrders.unverifiableReason.unknown'
		);
	});
});
