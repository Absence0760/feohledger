/**
 * The dashboard's time-of-day greeting, as pure functions so the boundaries
 * are testable without a component or a clock.
 *
 * The hour is the reader's own wall clock (`Date#getHours()`, the browser's
 * timezone): there is no per-user timezone setting, and "good morning" is
 * about where the reader is, not where the tenant is. Presentation only — no
 * figure on the page may gate or reorder on it.
 */

export type GreetingKey =
	| 'dashboard.greeting.morning'
	| 'dashboard.greeting.afternoon'
	| 'dashboard.greeting.evening';

/** 05:00–11:59 morning, 12:00–17:59 afternoon, 18:00–04:59 evening. */
export function greetingKey(hour: number): GreetingKey {
	if (hour >= 5 && hour < 12) return 'dashboard.greeting.morning';
	if (hour >= 12 && hour < 18) return 'dashboard.greeting.afternoon';
	return 'dashboard.greeting.evening';
}

/**
 * The first word of a display name, or `null` when there is none (a SCIM- or
 * SSO-provisioned account can arrive with an empty `full_name`). A `null`
 * means "greet nobody by name": the caller shows the date alone rather than
 * "Good morning, ".
 */
export function firstName(fullName: string | null | undefined): string | null {
	const first = (fullName ?? '').trim().split(/\s+/)[0];
	return first ? first : null;
}

/** Today's local calendar date as `YYYY-MM-DD`, the key `formatDate` takes. */
export function localDateKey(now: Date): string {
	const y = now.getFullYear();
	const mo = String(now.getMonth() + 1).padStart(2, '0');
	const d = String(now.getDate()).padStart(2, '0');
	return `${y}-${mo}-${d}`;
}
