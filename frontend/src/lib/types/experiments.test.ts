import { describe, it, expect } from 'vitest';
import {
	EXPERIMENT_STATUSES,
	PRIMARY_METRIC_LABEL_KEYS,
	STATUS_LABEL_KEYS,
	experimentStatusLabelKey,
	type PrimaryMetric
} from './experiments';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Map-completeness guard for the experiment label maps.
 *
 * `Record<ExperimentStatus, MessageKey>` already fails `pnpm check` when a
 * status joins the union with no key — but a key naming nothing in the
 * catalogue typechecks fine and renders the raw key string in the badge
 * (`m()` falls back key → raw). This is the runtime half.
 */
describe('STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every status', () => {
		for (const status of EXPERIMENT_STATUSES) {
			const key = STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('covers every status the filter chips are built from', () => {
		// The chips derive from EXPERIMENT_STATUSES, so a status missing from
		// the label map would render a chip with a raw key for a label.
		for (const status of EXPERIMENT_STATUSES) {
			expect(experimentStatusLabelKey(status)).toBe(STATUS_LABEL_KEYS[status]);
		}
		expect(experimentStatusLabelKey('abandoned')).toBeNull();
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of EXPERIMENT_STATUSES) {
			expect(en[STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});
});

describe('PRIMARY_METRIC_LABEL_KEYS', () => {
	const METRICS: PrimaryMetric[] = [
		'time_to_approval_days',
		'touchless_rate_pct',
		'exception_rate_pct',
		'rejection_rate_pct'
	];

	it('names a real catalogue key for every primary metric', () => {
		for (const metric of METRICS) {
			const key = PRIMARY_METRIC_LABEL_KEYS[metric];
			expect(key, `${metric} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${metric} → "${key}" is not in the catalogue`).toContain(key);
		}
	});
});
