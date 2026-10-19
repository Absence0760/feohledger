import { describe, expect, it } from 'vitest';
import { en } from '../i18n/locales/en';
import { interpolate } from '../i18n/interpolate';
import type { MessageKey } from '../i18n/messages';
import { glSyncReportLines, vendorSyncReportLines } from './erpSyncReport';

const t = (key: MessageKey, params?: Record<string, string | number>) =>
	interpolate(en[key], params, 'en');

describe('glSyncReportLines', () => {
	it('is one success line when nothing was refused', () => {
		expect(glSyncReportLines({ created: 89, updated: 0, conflicts: [], unlinked: [] }, t)).toEqual([
			{ text: 'Synced 89 new and 0 updated accounts.', tone: 'success' }
		]);
	});

	it('warns, apart, about refused codes and unlinked rows', () => {
		const lines = glSyncReportLines(
			{ created: 1, updated: 0, conflicts: [{ code: '6100' }], unlinked: ['Job Materials'] },
			t
		);
		expect(lines.map((l) => l.tone)).toEqual(['success', 'warning', 'warning']);
		expect(lines[1].text).toContain(': 6100.');
		expect(lines[2].text).toContain('recode their invoices: Job Materials.');
	});
});

describe('vendorSyncReportLines', () => {
	it('names the vendors left unlinked by an ambiguous name', () => {
		const lines = vendorSyncReportLines(
			{ created: 0, updated: 0, unchanged: 2, conflicts: [{ name: 'Acme' }] },
			t
		);
		expect(lines[0]).toEqual({
			text: 'Synced 0 new, 0 updated and 2 unchanged vendors.',
			tone: 'success'
		});
		expect(lines[1]).toEqual({
			text: 'Not linked, because the name matches more than one vendor: Acme.',
			tone: 'warning'
		});
	});
});
