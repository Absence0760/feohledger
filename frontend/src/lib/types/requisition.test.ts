import { describe, it, expect } from 'vitest';
import {
	REQUISITION_STATUSES,
	REQUISITION_FILTER_STATUSES,
	REQUISITION_STATUS_LABEL_KEYS,
	requisitionStatusLabelKey
} from './requisition';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Map-completeness guard for the requisition status labels — the sibling of
 * `intake.test.ts`, and for the same reason: the map was hardcoded English
 * inside a fully-translated page, so a German user saw "Zurück auf „Entwurf“"
 * next to an untranslated `Draft` badge.
 *
 * The union → `Record<RequisitionStatus, MessageKey>` typing is the
 * compile-time half (a new status is a `pnpm check` failure); this is the
 * runtime half — a key that names nothing in the catalogue typechecks fine and
 * renders the raw key string in the badge.
 */
describe('REQUISITION_STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every status', () => {
		for (const status of REQUISITION_STATUSES) {
			const key = REQUISITION_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of REQUISITION_STATUSES) {
			expect(en[REQUISITION_STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});

	it('labels the unreachable-but-renderable statuses too', () => {
		// `submitted` is filtered out of the chip row because no backend
		// transition stamps it — but a legacy row carrying it must still render
		// a badge, so it keeps its label. See UNREACHABLE_REQUISITION_STATUSES.
		for (const status of REQUISITION_STATUSES) {
			if (REQUISITION_FILTER_STATUSES.includes(status)) continue;
			expect(REQUISITION_STATUS_LABEL_KEYS[status], `${status} lost its label`).toBeTruthy();
		}
	});

	it('requisitionStatusLabelKey resolves a known status and returns null otherwise', () => {
		for (const status of REQUISITION_STATUSES) {
			expect(requisitionStatusLabelKey(status)).toBe(REQUISITION_STATUS_LABEL_KEYS[status]);
		}
		expect(requisitionStatusLabelKey('some_future_status')).toBeNull();
	});
});
