import { describe, expect, it } from 'vitest';
import {
	DSAR_COUNTED_SECTIONS,
	DSAR_COUNT_LABEL_KEYS,
	SUBJECT_IDENTIFIER_HINT_KEYS,
	SUBJECT_TYPES,
	SUBJECT_TYPE_LABEL_KEYS,
	dsarCountLines,
	subjectTypeLabelKey
} from './privacy';
import { EN_ALL, loadAllMessages } from '#lib/i18n/allMessages.ts';
import { interpolate } from '#lib/i18n/interpolate.ts';
import { SUPPORTED_LOCALES } from '#lib/i18n/locale.ts';

/**
 * `/admin/privacy` summarises a DSAR export from the response's typed
 * `record_counts`, never from server prose (decisions §360). The section names
 * are the backend's; this file holds the frontend's labels to them.
 */
const BACKEND = import.meta.glob('../../../../backend/app/services/privacy_export.py', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

function backendSections(): string[] {
	const source = Object.values(BACKEND)[0] ?? '';
	const tuple = source.match(/DSAR_COUNTED_SECTIONS:[^=]*=\s*\(([\s\S]*?)\)/);
	return tuple ? [...tuple[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]) : [];
}

describe('DSAR record-count sections', () => {
	it('reads the backend tuple', () => {
		expect(backendSections().length).toBeGreaterThan(10);
	});

	it('mirrors privacy_export.DSAR_COUNTED_SECTIONS exactly, in order', () => {
		expect([...DSAR_COUNTED_SECTIONS]).toEqual(backendSections());
	});

	it('labels every section with a pluralised {n} message in all six locales', async () => {
		expect(Object.keys(DSAR_COUNT_LABEL_KEYS).sort()).toEqual([...DSAR_COUNTED_SECTIONS].sort());
		for (const loc of SUPPORTED_LOCALES) {
			const dict = await loadAllMessages(loc);
			for (const key of Object.values(DSAR_COUNT_LABEL_KEYS)) {
				expect(dict[key], `${loc} lacks ${key}`).toMatch(/\{n, plural,/);
				for (const n of [0, 1, 2, 5]) {
					const out = interpolate(dict[key], { n }, loc);
					expect(out, `${loc} ${key} n=${n}`).toContain(String(n));
					expect(out).not.toMatch(/[{}#]/);
				}
			}
		}
	});
});

describe('dsarCountLines', () => {
	it('lists only the sections the export carries, in catalogue order', () => {
		const lines = dsarCountLines({ documents: 2, roles: 1, related_invoices: 0 });
		expect(lines.map((l) => [l.section, l.n])).toEqual([
			['roles', 1],
			['related_invoices', 0],
			['documents', 2]
		]);
		expect(lines[0].key).toBe('admin.privacy.count.roles');
	});

	it('skips a section this build has no label for, and a missing payload', () => {
		expect(dsarCountLines({ some_future_section: 4 })).toEqual([]);
		expect(dsarCountLines(undefined)).toEqual([]);
	});
});

describe('subject-type labels', () => {
	it('covers every subject type, with the English the page always showed', () => {
		expect(Object.keys(SUBJECT_TYPE_LABEL_KEYS).sort()).toEqual([...SUBJECT_TYPES].sort());
		expect(Object.keys(SUBJECT_IDENTIFIER_HINT_KEYS).sort()).toEqual([...SUBJECT_TYPES].sort());
		expect(EN_ALL[SUBJECT_TYPE_LABEL_KEYS.user]).toBe('AP-team user');
		expect(EN_ALL[SUBJECT_TYPE_LABEL_KEYS.vendor_contact]).toBe('Vendor contact details');
	});

	it('has no key for a stored type this build predates', () => {
		expect(subjectTypeLabelKey('vendor_user')).toBe('admin.privacy.subject.vendorUser');
		expect(subjectTypeLabelKey('something_new')).toBeNull();
	});
});

describe('the erasure summary', () => {
	// Built from the response's typed counts, with ICU plurals in place of the
	// old hand-built "field(s)".
	it('renders both counts in every locale with no placeholder left', async () => {
		for (const loc of SUPPORTED_LOCALES) {
			const dict = await loadAllMessages(loc);
			for (const [fields, documents] of [
				[1, 0],
				[3, 1],
				[7, 2]
			]) {
				const out = interpolate(dict['admin.privacy.toast.erased'], { fields, documents }, loc);
				expect(out, `${loc} ${fields}/${documents}`).not.toMatch(/[{}#]/);
				expect(out).toContain(String(fields));
			}
			const incomplete = interpolate(dict['admin.privacy.toast.erasureIncomplete'], { failed: 2 }, loc);
			expect(incomplete).toContain('2');
			expect(incomplete).not.toMatch(/[{}#]/);
		}
	});

	it('reads one field, three fields in English', () => {
		expect(interpolate(EN_ALL['admin.privacy.toast.erased'], { fields: 1, documents: 2 }, 'en')).toBe(
			'Erased: 1 field redacted, 2 documents deleted.'
		);
	});
});
