import { describe, expect, it } from 'vitest';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import { messages as de } from '#lib/i18n/locales/de.ts';
import { interpolate } from '../i18n/interpolate';
import type { MessageKey } from '../i18n/messages';
import { consolidationReasonLabel } from './vendorConsolidation';

const t = (key: MessageKey, params?: Record<string, string | number>) =>
	interpolate(EN_ALL[key], params, 'en');
const tDe = (key: MessageKey, params?: Record<string, string | number>) =>
	interpolate((de as Record<string, string>)[key], params, 'de');

describe('consolidationReasonLabel', () => {
	// The three shapes `backend/app/services/vendor_consolidation.py` emits.
	it('keys the two exact-match reasons', () => {
		expect(consolidationReasonLabel('same tax id', t)).toBe('Same tax ID');
		expect(consolidationReasonLabel('same vendor code', t)).toBe('Same vendor code');
		expect(consolidationReasonLabel('same tax id', tDe)).toBe('Gleiche Steuernummer');
	});

	it('states name similarity as a percentage in the reader’s format', () => {
		expect(consolidationReasonLabel('names 0.93 similar', t, 'en')).toBe('Names 93% similar');
		expect(consolidationReasonLabel('names 1.00 similar', t, 'en')).toBe('Names 100% similar');
		// German writes the percent sign after a no-break space.
		expect(consolidationReasonLabel('names 0.93 similar', tDe, 'de')).toBe(
			'Namen zu 93\u00a0% ähnlich'
		);
	});

	it('shows a reason it does not know as sent, rather than hiding the evidence', () => {
		expect(consolidationReasonLabel('same bank account', t)).toBe('same bank account');
	});
});

describe('consolidation merge toast', () => {
	// Was two hand-built `s` suffixes in one template literal (#635).
	it('pluralises both counts independently', () => {
		expect(t('vendors.consolidation.toast.merged', { n: 1, records: 1 })).toBe(
			'Merged 1 duplicate (1 record re-homed)'
		);
		expect(t('vendors.consolidation.toast.merged', { n: 2, records: 5 })).toBe(
			'Merged 2 duplicates (5 records re-homed)'
		);
		expect(tDe('vendors.consolidation.toast.merged', { n: 1, records: 3 })).toBe(
			'1 Duplikat zusammengeführt (3 Datensätze übertragen)'
		);
	});
});
