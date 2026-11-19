/**
 * Whole calendar days from today to a template's `next_run_on`, in the
 * reader's own timezone — positive ahead, negative behind, `null` when the
 * value is not a date.
 *
 * `next_run_on` is a bare `YYYY-MM-DD`. `new Date('2026-11-20')` parses that
 * as UTC midnight, and the page used to subtract `Date.now()` and round: west
 * of UTC that rolled the day back, and late in any evening it rounded
 * "tomorrow" down to "today". Both ends are local midnights here, so the
 * answer is a calendar-day count, not an elapsed-time estimate.
 */
export function daysUntil(value: string, now: Date = new Date()): number | null {
	const dateOnly = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
	if (!dateOnly) return null;
	const target = new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]));
	if (Number.isNaN(target.getTime())) return null;
	const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
	// Math.round absorbs the 23h / 25h day across a DST change.
	return Math.round((target.getTime() - today.getTime()) / 86_400_000);
}
