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

	it('warns about accounts retired because the ERP made them inactive', () => {
		const lines = glSyncReportLines(
			{ created: 0, updated: 1, conflicts: [], unlinked: [], deactivated: ['Bank Charges'] },
			t
		);
		expect(lines[1]).toEqual({
			text: 'Retired, because the ERP made them inactive: Bank Charges.',
			tone: 'warning'
		});
	});
});

describe('glSyncReportLines — parents', () => {
	it('names accounts synced without a parent that did not fit', () => {
		const lines = glSyncReportLines({ created: 1, updated: 0, parent_code_dropped: ['6100'] }, t);
		expect(lines[1]).toEqual({
			text: "Synced without their parent account, because the parent's code is too long to store: 6100.",
			tone: 'warning'
		});
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

	it('names the vendors whose link this pull could not confirm', () => {
		const lines = vendorSyncReportLines(
			{ created: 1, updated: 1, unchanged: 0, conflicts: [], unlinked: ['Acme'] },
			t
		);
		expect(lines.map((l) => l.tone)).toEqual(['success', 'warning']);
		expect(lines[1].text).toContain('no longer matches them by name or tax ID');
		expect(lines[1].text).toContain(': Acme.');
	});

	it('counts retired vendors and names those with a value too long to store', () => {
		const lines = vendorSyncReportLines(
			{
				created: 0,
				updated: 1,
				unchanged: 0,
				deactivated: 1,
				values_too_long: [{ name: 'Northwind' }]
			},
			t
		);
		expect(lines.slice(1)).toEqual([
			{ text: '1 vendor retired, because the ERP made them inactive.', tone: 'warning' },
			{
				text: 'Synced, but some details were too long to store and kept their old values: Northwind.',
				tone: 'warning'
			}
		]);
	});
});
