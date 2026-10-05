import { describe, it, expect } from 'vitest';
import {
	CATALOG_TYPES,
	CATALOG_TYPE_LABEL_KEYS,
	GUIDED_BUYING_REASON_LABEL_KEYS,
	catalogTypeLabelKey,
	guidedBuyingReasonLabelKey,
	PUNCHOUT_STATUS_LABEL_KEYS,
	PUNCHOUT_STATUS_TONES,
	punchoutStatusLabelKey,
	type PunchoutSessionStatus
} from './catalog';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Map-completeness guard for the punch-out session-status labels.
 *
 * `Record<PunchoutSessionStatus, MessageKey>` already fails `pnpm check` when
 * a status joins the union with no key — but a key naming nothing in the
 * catalogue typechecks fine and renders the raw key string in the badge
 * (`m()` falls back key → raw). This is the runtime half.
 */
const PUNCHOUT_STATUSES: PunchoutSessionStatus[] = [
	'pending',
	'returned',
	'converted',
	'expired',
	'cancelled'
];

describe('PUNCHOUT_STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every session status', () => {
		for (const status of PUNCHOUT_STATUSES) {
			const key = PUNCHOUT_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key]).not.toBe(status);
		}
	});

	it('resolves a known status and returns null otherwise', () => {
		for (const status of PUNCHOUT_STATUSES) {
			expect(punchoutStatusLabelKey(status)).toBe(PUNCHOUT_STATUS_LABEL_KEYS[status]);
		}
		// `PunchoutSession.status` is a bare string on the wire, so the modal
		// renders the raw value rather than a blank badge for one this build
		// does not know.
		expect(punchoutStatusLabelKey('abandoned')).toBeNull();
	});

	it('labels every status the tone map tints', () => {
		// The two are read one after the other on the same badge — a status
		// with a tone but no label renders a coloured pill with no text.
		for (const status of Object.keys(PUNCHOUT_STATUS_TONES)) {
			expect(punchoutStatusLabelKey(status), `${status} has a tone but no label`).toBeTruthy();
		}
	});
});

describe('CATALOG_TYPE_LABEL_KEYS', () => {
	it('names a real catalogue key for every catalog type', () => {
		for (const type of CATALOG_TYPES) {
			const key = CATALOG_TYPE_LABEL_KEYS[type];
			expect(key, `${type} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${type} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('resolves a known type and returns null otherwise', () => {
		expect(catalogTypeLabelKey('punchout')).toBe('catalogs.type.punchout');
		expect(catalogTypeLabelKey('marketplace')).toBeNull();
	});
});

describe('GUIDED_BUYING_REASON_LABEL_KEYS', () => {
	it('names a real catalogue key for every reason', () => {
		for (const key of Object.values(GUIDED_BUYING_REASON_LABEL_KEYS)) {
			expect(Object.keys(en), `"${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('returns null for a reason the backend adds first', () => {
		// The card renders the raw code in that case — a reason nobody can read
		// is still better evidence for steering a buyer than no reason at all.
		expect(guidedBuyingReasonLabelKey('active_contract')).toBe(
			'catalogs.guided.reason.activeContract'
		);
		expect(guidedBuyingReasonLabelKey('volume_discount')).toBeNull();
	});
});
