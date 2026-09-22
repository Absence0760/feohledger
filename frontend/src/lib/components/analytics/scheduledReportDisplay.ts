/**
 * Pure display helpers for the scheduled-reports panel.
 *
 * Kept out of the component (and unit-tested beside it, the same way
 * `routes/cfo/openingBalanceNotice.ts` is) because two of them encode a
 * CONTRACT, not styling:
 *
 *  1. **The auto-disabled state.** The runner disables a schedule after 5
 *     consecutive failures and records the streak as a `[retry N]` prefix on
 *     `last_run_error`. A schedule that silently stopped emailing is the exact
 *     failure this panel exists to make visible, so it must be distinguishable
 *     from one an admin paused on purpose — same `enabled: false`, completely
 *     different remedy. Deriving it in one tested function is what stops the
 *     two collapsing into a single "Disabled" pill again.
 *  2. **The label fallback.** `report_type` / `cadence` vocabularies come off
 *     the runner's own registries and ride the list response. A key we have no
 *     translation for must still RENDER — humanised — so that adding a report
 *     type on the backend makes it selectable here for free instead of
 *     invisible. Hardcoding either list is the bug this guards against.
 */

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';
import type { ScheduledReport } from '$lib/types/scheduledReport';

/** Consecutive failures after which the runner disables a schedule itself.
 *  Mirrors `backend/app/services/scheduled_reports.py`. */
export const AUTO_DISABLE_FAILURE_COUNT = 5;

/** Translations for the report types the runner ships today. A key absent here
 *  is NOT an error — see {@link reportTypeLabel}. */
export const REPORT_TYPE_LABEL_KEYS: Record<string, MessageKey> = {
	aging_snapshot: 'scheduledReports.type.agingSnapshot',
	cashflow_forecast: 'scheduledReports.type.cashflowForecast',
	expense_register: 'scheduledReports.type.expenseRegister',
	invoice_register: 'scheduledReports.type.invoiceRegister',
	payment_register: 'scheduledReports.type.paymentRegister',
	vendor_spend: 'scheduledReports.type.vendorSpend'
};

/** Translations for the cadences the runner ships today. */
export const CADENCE_LABEL_KEYS: Record<string, MessageKey> = {
	daily: 'scheduledReports.cadence.daily',
	weekly: 'scheduledReports.cadence.weekly',
	monthly: 'scheduledReports.cadence.monthly'
};

/**
 * `aging_snapshot` → `Aging snapshot`. The last-resort rendering for a
 * vocabulary key the backend has and this build does not — readable English
 * rather than a raw identifier, and never a hidden option. Stays English by the
 * established data-value convention (currency codes, ERP product names, …).
 */
export function humaniseKey(key: string): string {
	const words = key.replace(/[_-]+/g, ' ').trim();
	if (!words) return key;
	return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The i18n key for a report type, or `null` when only the humanised fallback
 *  applies. Split from the label so the component can call `m()` itself (a
 *  message read inside the template stays reactive to a locale switch). */
export function reportTypeLabelKey(type: string): MessageKey | null {
	return REPORT_TYPE_LABEL_KEYS[type] ?? null;
}

/** The i18n key for a cadence, or `null` for the humanised fallback. */
export function cadenceLabelKey(cadence: string): MessageKey | null {
	return CADENCE_LABEL_KEYS[cadence] ?? null;
}

/** The consecutive-failure count the runner stamped on `last_run_error`, or
 *  `null` when the error carries no `[retry N]` prefix. */
export function retryCountFromError(error: string | null): number | null {
	if (!error) return null;
	const match = /^\[retry (\d+)\]/.exec(error.trim());
	if (!match) return null;
	const n = Number(match[1]);
	return Number.isFinite(n) ? n : null;
}

/**
 * The six states a row can be in, in the order they are checked.
 *
 * `auto_disabled` outranks `disabled` deliberately: both have `enabled: false`,
 * but only one of them means "this stopped emailing without anyone deciding
 * that".
 */
export type ScheduleHealth =
	| 'auto_disabled'
	| 'disabled'
	| 'failure'
	| 'partial'
	| 'success'
	| 'never_run';

/**
 * True when the runner disabled this schedule itself after
 * {@link AUTO_DISABLE_FAILURE_COUNT} consecutive failures.
 *
 * All three signals are required. `enabled: false` alone is a paused schedule;
 * a `[retry N]` prefix alone is a schedule still retrying. Without the streak
 * marker we do not claim the runner did it — an admin who paused a schedule
 * that had previously failed must not be told the system turned it off.
 */
export function isAutoDisabled(s: ScheduledReport): boolean {
	if (s.enabled) return false;
	if (s.last_run_status !== 'failure') return false;
	const retries = retryCountFromError(s.last_run_error);
	return retries !== null && retries >= AUTO_DISABLE_FAILURE_COUNT;
}

export function scheduleHealth(s: ScheduledReport): ScheduleHealth {
	if (isAutoDisabled(s)) return 'auto_disabled';
	if (!s.enabled) return 'disabled';
	if (s.last_run_status === 'failure') return 'failure';
	if (s.last_run_status === 'partial') return 'partial';
	if (s.last_run_status === 'success') return 'success';
	return 'never_run';
}

/** The `Badge` tones this panel uses — every tone but the ERP pipeline's. */
export type ScheduleBadgeTone = Exclude<BadgeTone, 'erp'>;

/**
 * `auto_disabled` is `danger` while a hand-paused `disabled` is the flat,
 * signal-free `neutral` chip — that colour gap, plus a different word, is what
 * keeps "it broke and gave up" from reading as "someone turned it off".
 */
export function healthTone(health: ScheduleHealth): ScheduleBadgeTone {
	switch (health) {
		case 'auto_disabled':
		case 'failure':
			return 'danger';
		case 'partial':
			return 'warning';
		case 'success':
			return 'success';
		case 'disabled':
			return 'neutral';
		case 'never_run':
			return 'muted';
	}
}

export const HEALTH_LABEL_KEYS: Record<ScheduleHealth, MessageKey> = {
	auto_disabled: 'scheduledReports.health.autoDisabled',
	disabled: 'scheduledReports.health.disabled',
	failure: 'scheduledReports.health.failure',
	partial: 'scheduledReports.health.partial',
	success: 'scheduledReports.health.success',
	never_run: 'scheduledReports.health.neverRun'
};

/**
 * Whether the runner's own message about the last attempt should be shown.
 *
 * `partial` is the case that MUST show it: some recipients received the report
 * and some did not, and only this string (counts + an exception class, never an
 * address) says how many. `failure` / `auto_disabled` show it for the same
 * reason. A `success` row has nothing to explain.
 */
export function showsRunError(health: ScheduleHealth): boolean {
	return health === 'partial' || health === 'failure' || health === 'auto_disabled';
}

/**
 * How many of the submitted recipients the backend dropped as duplicates.
 *
 * The backend de-dupes case-insensitively instead of rejecting, so the saved
 * list can be shorter than the typed one. The panel re-renders from the
 * response either way; this only decides whether to SAY so, because a silently
 * shortened list looks like data loss.
 */
export function droppedRecipientCount(submitted: string[], saved: string[]): number {
	return Math.max(0, submitted.length - saved.length);
}
