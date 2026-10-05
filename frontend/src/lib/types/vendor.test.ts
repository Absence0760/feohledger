import { describe, it, expect } from 'vitest';
import {
	ENRICHABLE_FIELD_LABEL_KEYS,
	RISK_LEVEL_LABEL_KEYS,
	SANCTIONS_RESULTS,
	SANCTIONS_RESULT_LABEL_KEYS,
	SCREENING_CATEGORIES,
	SCREENING_CATEGORY_LABEL_KEYS,
	SCREENING_STATUS_LABEL_KEYS,
	VENDOR_SOURCE_LABEL_KEYS,
	VENDOR_STATUSES,
	VENDOR_STATUS_LABEL_KEYS,
	VENDOR_STATUS_TONES,
	riskLevelLabelKey,
	sanctionsResultLabelKey,
	screeningCategoryLabelKey,
	screeningCategoryLabels,
	screeningStatusLabelKey,
	vendorSourceLabelKey,
	vendorStatusLabelKey,
	type RiskLevel,
	type ScreeningStatus
} from './vendor';
import { interpolate } from '#lib/i18n/interpolate.ts';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Map-completeness guard for the sanctions-screening label map.
 *
 * `Record<ScreeningStatus, MessageKey>` already fails `pnpm check` when a
 * status joins the union with no key — but a key naming nothing in the
 * catalogue typechecks fine and renders the raw key string in the pill
 * (`m()` falls back key → raw). This is the runtime half.
 */
const SCREENING_STATUSES: ScreeningStatus[] = ['unscreened', 'clear', 'review', 'match'];

describe('SCREENING_STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every screening status', () => {
		for (const status of SCREENING_STATUSES) {
			const key = SCREENING_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of SCREENING_STATUSES) {
			expect(en[SCREENING_STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});

	it('screeningStatusLabelKey resolves a known status and returns null otherwise', () => {
		for (const status of SCREENING_STATUSES) {
			expect(screeningStatusLabelKey(status)).toBe(SCREENING_STATUS_LABEL_KEYS[status]);
		}
		// The pill renders the raw value — visible and searchable — rather than
		// blank, so a verdict a new provider introduces degrades gracefully.
		expect(screeningStatusLabelKey('probable_match')).toBeNull();
	});

	it('carries the pill labels ScreeningBadge renders beside the verdict', () => {
		// Both live in the same pill row as the verdict above; an English
		// literal there is the hybrid this conversion exists to remove.
		expect(Object.keys(en)).toContain('vendors.screening.blocked');
		expect(Object.keys(en)).toContain('vendors.screening.adverseMedia');
		expect(Object.keys(en)).toContain('vendors.screening.adverseMediaTitle');
	});
});

describe('VENDOR_STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every lifecycle status', () => {
		for (const status of VENDOR_STATUSES) {
			const key = VENDOR_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key]).not.toBe(status);
		}
	});

	it('labels every status the tone map tints', () => {
		// The two are read one after the other on the same badge — a status
		// with a tone but no label renders a coloured pill showing a raw enum
		// value. They live in this module together for exactly that reason.
		for (const status of Object.keys(VENDOR_STATUS_TONES)) {
			expect(vendorStatusLabelKey(status), `${status} has a tone but no label`).toBeTruthy();
		}
	});

	it('resolves a known status and returns null otherwise', () => {
		for (const status of VENDOR_STATUSES) {
			expect(vendorStatusLabelKey(status)).toBe(VENDOR_STATUS_LABEL_KEYS[status]);
		}
		// `Vendor.status` is untyped on the wire, so the row renders the raw
		// value rather than a blank badge for one this build does not know.
		expect(vendorStatusLabelKey('archived')).toBeNull();
	});
});

describe('VENDOR_SOURCE_LABEL_KEYS', () => {
	it('names a real catalogue key for every vendor source', () => {
		for (const key of Object.values(VENDOR_SOURCE_LABEL_KEYS)) {
			expect(Object.keys(en), `"${key}" is not in the catalogue`).toContain(key);
		}
		expect(vendorSourceLabelKey('erp_sync')).toBe('vendors.source.erpSync');
		expect(vendorSourceLabelKey('portal_self_service')).toBeNull();
	});
});

const RISK_LEVELS: RiskLevel[] = ['low', 'medium', 'high', 'critical', 'unknown'];

describe('RISK_LEVEL_LABEL_KEYS', () => {
	it('names a real catalogue key for every risk level', () => {
		for (const level of RISK_LEVELS) {
			const key = RISK_LEVEL_LABEL_KEYS[level];
			expect(key, `${level} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${level} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key]).not.toBe(level);
		}
	});

	it('resolves a known level and returns null otherwise', () => {
		for (const level of RISK_LEVELS) {
			expect(riskLevelLabelKey(level)).toBe(RISK_LEVEL_LABEL_KEYS[level]);
		}
		expect(riskLevelLabelKey('severe')).toBeNull();
	});

	it('composes the pill and its tooltip through their own keys', () => {
		// The badge must never concatenate a translated level onto an English
		// word — the whole reason the risk pill read `High risk` between two
		// translated pills. Both composed strings carry `{level}`, so a locale
		// can put the noun wherever its grammar wants it.
		for (const key of ['vendors.risk.pill', 'vendors.risk.title'] as const) {
			expect(Object.keys(en)).toContain(key);
			expect(en[key], `${key} must interpolate the level`).toContain('{level}');
			expect(
				interpolate(en[key], { level: en[RISK_LEVEL_LABEL_KEYS.high] })
			).not.toContain('{level}');
		}
	});
});

/**
 * Drift guard for the screening-hit taxonomy.
 *
 * The vocabulary is fixed on the backend
 * (`app/services/sanctions_categories.py` — CATEGORY_SANCTIONS / _PEP /
 * _ADVERSE_MEDIA / _HIGH_RISK_COUNTRY), so it is enumerated from THERE rather
 * than from the map under test: a map that forgot a kind would otherwise agree
 * with itself, and a dropped hit is the one outcome a compliance reviewer must
 * never get.
 */
const BACKEND_SCREENING_CATEGORIES = [
	'sanctions',
	'pep',
	'adverse_media',
	'high_risk_country'
] as const;

describe('SCREENING_CATEGORY_LABEL_KEYS', () => {
	it('covers exactly the taxonomy the backend can persist', () => {
		expect([...SCREENING_CATEGORIES].sort()).toEqual([...BACKEND_SCREENING_CATEGORIES].sort());
	});

	it('names a real, non-empty catalogue key for every category', () => {
		for (const category of SCREENING_CATEGORIES) {
			const key = SCREENING_CATEGORY_LABEL_KEYS[category];
			expect(key, `${category} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${category} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key].trim().length).toBeGreaterThan(0);
			expect(en[key]).not.toBe(category);
		}
	});

	it('resolves a known category and returns null otherwise', () => {
		for (const category of SCREENING_CATEGORIES) {
			expect(screeningCategoryLabelKey(category)).toBe(SCREENING_CATEGORY_LABEL_KEYS[category]);
		}
		expect(screeningCategoryLabelKey('crypto_exposure')).toBeNull();
	});

	it('resolves a hit list in order, degrading an unknown kind to readable text', () => {
		const parts = screeningCategoryLabels(['pep', 'crypto_exposure']);
		expect(parts.map((p) => p.category)).toEqual(['pep', 'crypto_exposure']);
		expect(parts[0].key).toBe('vendors.screening.category.pep');
		// A provider adapter can widen the taxonomy before this build catches
		// up (`categories_from_raw_response` accepts any list of strings), so an
		// unrecognised kind renders de-underscored — never dropped, never raw
		// snake_case.
		expect(parts[1].key).toBeNull();
		expect(parts[1].fallback).toBe('crypto exposure');
	});

	it('resolves a missing or empty list to no entries', () => {
		expect(screeningCategoryLabels(null)).toEqual([]);
		expect(screeningCategoryLabels(undefined)).toEqual([]);
		expect(screeningCategoryLabels([])).toEqual([]);
	});
});

/**
 * The verdict vocabulary `backend/app/services/vendor_screening.py` writes onto
 * `sanctions_checks.result` (`ScreeningResult.result`, and the literal the
 * NULL-vendor path raises). Restated here, as the category list above is, so a
 * backend widening shows up as a failure rather than a raw `snake_case` badge
 * in a compliance reviewer's timeline.
 */
const BACKEND_SANCTIONS_RESULTS = ['clear', 'review_required', 'match'] as const;

describe('SANCTIONS_RESULT_LABEL_KEYS', () => {
	it('covers exactly the verdicts the backend can persist', () => {
		expect([...SANCTIONS_RESULTS].sort()).toEqual([...BACKEND_SANCTIONS_RESULTS].sort());
	});

	it('names a real, non-empty catalogue key for every verdict', () => {
		for (const result of SANCTIONS_RESULTS) {
			const key = SANCTIONS_RESULT_LABEL_KEYS[result];
			expect(key, `${result} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${result} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key].trim().length).toBeGreaterThan(0);
			expect(en[key]).not.toBe(result);
		}
	});

	it('is a distinct vocabulary from the vendor-level screening status', () => {
		// The backend collapses `review_required` to `review` when it stamps
		// `Vendor.screening_status`, so the status map has no member that could
		// label a `review_required` history row. Reusing it there would render a
		// blank badge on the one verdict a reviewer most needs to read.
		expect(screeningStatusLabelKey('review_required')).toBeNull();
		expect(sanctionsResultLabelKey('review_required')).toBe(
			'vendors.screening.result.reviewRequired'
		);
	});

	it('resolves a known verdict and returns null otherwise', () => {
		for (const result of SANCTIONS_RESULTS) {
			expect(sanctionsResultLabelKey(result)).toBe(SANCTIONS_RESULT_LABEL_KEYS[result]);
		}
		expect(sanctionsResultLabelKey('pending_manual_review')).toBeNull();
	});
});

describe('ENRICHABLE_FIELD_LABEL_KEYS', () => {
	it('names a real catalogue key for every applyable field', () => {
		// The field name is also the `{field}` interpolated into the translated
		// apply-checkbox aria-label, so an English literal here landed
		// mid-sentence in a translated label.
		for (const key of Object.values(ENRICHABLE_FIELD_LABEL_KEYS)) {
			expect(Object.keys(en), `"${key}" is not in the catalogue`).toContain(key);
		}
	});
});
