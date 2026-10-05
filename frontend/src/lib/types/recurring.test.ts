import { describe, it, expect } from 'vitest';
import {
	CADENCE_LABEL_KEYS,
	GENERATION_SKIP_REASONS,
	RECURRING_CADENCES,
	RECURRING_STATUSES,
	STATUS_LABEL_KEYS,
	cadenceLabelKey,
	recurringStatusLabelKey,
	skipReasonKey
} from './recurring';
import { en } from '#lib/i18n/locales/en.ts';

describe('skipReasonKey', () => {
	it('names every reason code the backend sweep can emit', () => {
		for (const code of GENERATION_SKIP_REASONS) {
			const key = skipReasonKey(code);
			expect(key, `no message key for reason "${code}"`).toBeTruthy();
			// The key must actually exist in the catalogue, or `m()` would fall
			// back to rendering the raw key string in the badge tooltip.
			expect(Object.keys(en)).toContain(key as string);
		}
	});

	it('returns null for a code this frontend does not know', () => {
		// The caller renders the raw code — visible and searchable — rather than
		// a blank tooltip, so a newly-added backend reason degrades gracefully
		// until this map catches up (the test above is what makes it catch up).
		expect(skipReasonKey('missing_moon_phase')).toBeNull();
	});
});

/**
 * Map-completeness guard for the template status + cadence labels.
 *
 * `Record<RecurringStatus, MessageKey>` already fails `pnpm check` when a
 * status joins the union with no key — but a key naming nothing in the
 * catalogue typechecks fine and renders the raw key string in the badge
 * (`m()` falls back key → raw). This is the runtime half; it matters here
 * because the amber "Not generating" badge beside the status pill has been
 * translated since round 23, so an English status read as the odd one out.
 */
describe('STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every template status', () => {
		for (const status of RECURRING_STATUSES) {
			const key = STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key]).not.toBe(status);
		}
	});

	it('resolves a known status and returns null otherwise', () => {
		for (const status of RECURRING_STATUSES) {
			expect(recurringStatusLabelKey(status)).toBe(STATUS_LABEL_KEYS[status]);
		}
		expect(recurringStatusLabelKey('archived')).toBeNull();
	});
});

describe('CADENCE_LABEL_KEYS', () => {
	it('names a real catalogue key for every cadence', () => {
		for (const cadence of RECURRING_CADENCES) {
			const key = CADENCE_LABEL_KEYS[cadence];
			expect(key, `${cadence} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${cadence} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('resolves a known cadence and returns null otherwise', () => {
		expect(cadenceLabelKey('quarterly')).toBe('recurring.cadence.quarterly');
		expect(cadenceLabelKey('weekly')).toBeNull();
	});
});
