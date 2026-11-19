/**
 * The shared PO-status labels and the portal flip-refusal sentences.
 *
 * The status set every dead order is refused under is read from the backend
 * (`models/procurement.DEAD_PO_STATUSES`), so a new dead status cannot reach
 * a status column as a raw database token.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { EN_ALL, loadAllMessages } from '#lib/i18n/allMessages.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';
import { interpolate } from '#lib/i18n/interpolate.ts';
import {
	PO_ALREADY_INVOICED,
	PO_NOT_INVOICEABLE,
	PO_STATUS_LABEL_KEYS,
	PO_TOTAL_UNVERIFIABLE,
	poFlipRefusalText,
	poStatusLabel,
	poStatusLabelKey
} from './purchaseOrder.ts';

const PROCUREMENT = readFileSync(
	new URL('../../../../backend/app/models/procurement.py', import.meta.url),
	'utf8'
);

function backendDeadStatuses(): string[] {
	const line = PROCUREMENT.match(/^DEAD_PO_STATUSES[^=]*= \(([^)]*)\)/m)?.[1] ?? '';
	return [...line.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

const en = (key: MessageKey, params?: Record<string, string | number>) =>
	interpolate(EN_ALL[key] ?? key, params);

describe('PO status labels', () => {
	it('labels every status the backend counts as dead, plus open', () => {
		const dead = backendDeadStatuses();
		expect(dead.length).toBeGreaterThan(0);
		for (const status of [...dead, 'open']) {
			expect(poStatusLabelKey(status), status).not.toBeNull();
		}
	});

	it('has an English string for every key, translated in every locale', async () => {
		for (const key of Object.values(PO_STATUS_LABEL_KEYS)) {
			expect(EN_ALL[key], key).toBeTruthy();
		}
		for (const locale of ['de', 'es', 'fr', 'ja', 'pt-BR'] as const) {
			const all = await loadAllMessages(locale);
			for (const key of Object.values(PO_STATUS_LABEL_KEYS)) {
				expect(all[key], `${locale} ${key}`).toBeTruthy();
			}
			expect(all['portal.po.refusal.notInvoiceable'], locale).toBeTruthy();
			expect(all['portal.po.refusal.alreadyInvoiced'], locale).toBeTruthy();
		}
	});

	it('shows an unknown (free-form) status as-is', () => {
		expect(poStatusLabel('closed', en)).toBe('Closed');
		expect(poStatusLabel('on_hold_in_erp', en)).toBe('on_hold_in_erp');
		expect(poStatusLabelKey(null)).toBeNull();
	});
});

describe('poFlipRefusalText', () => {
	it('localizes the codes it knows and falls back to the server English', () => {
		expect(
			poFlipRefusalText(
				{ code: PO_NOT_INVOICEABLE, message: 'server', params: { status: 'closed' } },
				en
			)
		).toBe('No longer open for invoicing');
		expect(poFlipRefusalText({ code: PO_ALREADY_INVOICED, message: 'server' }, en)).toBe(
			'Already invoiced'
		);
		expect(poFlipRefusalText({ code: PO_TOTAL_UNVERIFIABLE, message: 'server' }, en)).toBe(
			EN_ALL['portal.po.totalUnverifiableNote']
		);
		expect(poFlipRefusalText({ code: 'something_newer', message: 'Server says no.' }, en)).toBe(
			'Server says no.'
		);
		expect(poFlipRefusalText(null, en)).toBe('');
		expect(poFlipRefusalText(undefined, en)).toBe('');
	});
});
