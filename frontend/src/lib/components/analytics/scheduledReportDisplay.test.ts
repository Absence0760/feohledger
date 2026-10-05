import { describe, expect, it } from 'vitest';

import {
	AUTO_DISABLE_FAILURE_COUNT,
	cadenceLabelKey,
	droppedRecipientCount,
	healthTone,
	humaniseKey,
	isAutoDisabled,
	reportTypeLabelKey,
	retryCountFromError,
	scheduleHealth,
	showsRunError
} from './scheduledReportDisplay';
import type { ScheduledReport } from '#lib/types/scheduledReport.ts';

function row(over: Partial<ScheduledReport> = {}): ScheduledReport {
	return {
		id: 'sr-1',
		name: 'Weekly aging',
		report_type: 'aging_snapshot',
		cadence: 'weekly',
		recipients: ['cfo@acme.test'],
		period_days: 30,
		enabled: true,
		next_run_at: '2026-09-01T08:00:00Z',
		last_run_at: null,
		last_run_status: null,
		last_run_error: null,
		...over
	};
}

describe('unknown vocabulary keys still render', () => {
	it('maps the shipped report types + cadences to message keys', () => {
		expect(reportTypeLabelKey('aging_snapshot')).toBe('scheduledReports.type.agingSnapshot');
		expect(cadenceLabelKey('monthly')).toBe('scheduledReports.cadence.monthly');
	});

	it('returns no key for a type the backend added and this build has not seen', () => {
		// The point of the null: the component falls back to `humaniseKey`
		// instead of hiding the option, so a new backend report type is
		// selectable here for free.
		expect(reportTypeLabelKey('carbon_footprint')).toBeNull();
		expect(cadenceLabelKey('fortnightly')).toBeNull();
	});

	it('humanises a raw key into readable English', () => {
		expect(humaniseKey('aging_snapshot')).toBe('Aging snapshot');
		expect(humaniseKey('cash-flow_forecast')).toBe('Cash flow forecast');
		expect(humaniseKey('')).toBe('');
	});
});

describe('the [retry N] streak marker', () => {
	it('reads the count off the prefix', () => {
		expect(retryCountFromError('[retry 5] SMTPException: connection refused')).toBe(5);
		expect(retryCountFromError('  [retry 12] boom')).toBe(12);
	});

	it('is null when there is no marker', () => {
		expect(retryCountFromError(null)).toBeNull();
		expect(retryCountFromError('SMTPException: connection refused')).toBeNull();
		expect(retryCountFromError('failed [retry 5]')).toBeNull();
	});
});

describe('auto-disabled is distinguishable from hand-paused', () => {
	const autoDisabled = row({
		enabled: false,
		last_run_status: 'failure',
		last_run_error: `[retry ${AUTO_DISABLE_FAILURE_COUNT}] SMTPException: connection refused`
	});

	it('needs all three signals', () => {
		expect(isAutoDisabled(autoDisabled)).toBe(true);
		// Still enabled → still retrying, not given up.
		expect(isAutoDisabled({ ...autoDisabled, enabled: true })).toBe(false);
		// Paused by an admin, no streak marker → their decision, not the runner's.
		expect(isAutoDisabled({ ...autoDisabled, last_run_error: 'SMTPException' })).toBe(false);
		// Paused after a partial run → not a failure streak.
		expect(
			isAutoDisabled({ ...autoDisabled, last_run_status: 'partial', last_run_error: null })
		).toBe(false);
		// Below the threshold → the runner has not given up yet.
		expect(
			isAutoDisabled({
				...autoDisabled,
				last_run_error: `[retry ${AUTO_DISABLE_FAILURE_COUNT - 1}] SMTPException`
			})
		).toBe(false);
	});

	it('gives the two states different health, different tone', () => {
		const paused = row({ enabled: false });
		expect(scheduleHealth(autoDisabled)).toBe('auto_disabled');
		expect(scheduleHealth(paused)).toBe('disabled');
		// The colour gap is half of what makes "it broke" unmistakable.
		expect(healthTone('auto_disabled')).toBe('danger');
		expect(healthTone('disabled')).toBe('neutral');
		expect(healthTone('auto_disabled')).not.toBe(healthTone('disabled'));
	});
});

describe('health of a live schedule', () => {
	it('reflects the last run', () => {
		expect(scheduleHealth(row())).toBe('never_run');
		expect(scheduleHealth(row({ last_run_status: 'success' }))).toBe('success');
		expect(scheduleHealth(row({ last_run_status: 'partial' }))).toBe('partial');
		expect(scheduleHealth(row({ last_run_status: 'failure' }))).toBe('failure');
	});

	it('shows the runner message for partial + failing rows only', () => {
		// `partial` is the case that must not be silent: some recipients got it.
		expect(showsRunError('partial')).toBe(true);
		expect(showsRunError('failure')).toBe(true);
		expect(showsRunError('auto_disabled')).toBe(true);
		expect(showsRunError('success')).toBe(false);
		expect(showsRunError('never_run')).toBe(false);
		expect(showsRunError('disabled')).toBe(false);
	});
});

describe('server-side recipient de-duplication', () => {
	it('counts what the backend dropped', () => {
		expect(droppedRecipientCount(['a@x.test', 'A@X.test', 'b@x.test'], ['a@x.test', 'b@x.test'])).toBe(1);
		expect(droppedRecipientCount(['a@x.test'], ['a@x.test'])).toBe(0);
		// Never negative — a backend that somehow returned more is not a "drop".
		expect(droppedRecipientCount(['a@x.test'], ['a@x.test', 'b@x.test'])).toBe(0);
	});
});
