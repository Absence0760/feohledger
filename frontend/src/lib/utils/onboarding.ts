import type { OnboardingProgress } from '#lib/types/analytics.ts';

/**
 * The first-run setup steps — where each one is done, and whether the server
 * says it has been (`docs/decisions.md` §315).
 *
 * Shared by the dashboard's setup checklist and `/organization`'s Getting
 * started panel, so the two cannot send the same step to two different places
 * or read its progress from two different facts. Labels stay with the caller:
 * the dashboard's live in the main catalogue, the settings page's in the
 * `settings` slice.
 */

/** A step the server tracks — one `OnboardingProgress` flag each. */
export type TrackedSetupStep = keyof OnboardingProgress;

/**
 * Every step either surface lists. `first_invoice` is the dashboard's last
 * step and is, by construction, never done while the checklist is showing;
 * `defaults` / `payments` / `branding` are settings with no reliable "done" fact
 * — a blank invoice-defaults form is a valid choice — so they are links only.
 */
export type SetupStepId = TrackedSetupStep | 'first_invoice' | 'defaults' | 'payments' | 'branding';

const SETUP_STEP_HREF: Record<SetupStepId, string> = {
	company: '/organization?section=company',
	defaults: '/organization?section=defaults',
	users: '/admin',
	security: '/organization?section=security',
	erp: '/organization?section=erp',
	vendors: '/vendors',
	workflow: '/workflows',
	email_intake: '/organization?section=email-intake',
	payments: '/organization?section=payments',
	branding: '/organization?section=branding',
	first_invoice: '/invoices'
};

/**
 * Where `id` is done. From the page that owns it (`fromPath`), a panel link is
 * the bare `?section=…`, so it keeps any other query parameter the page carries.
 */
export function setupStepHref(id: SetupStepId, fromPath?: string): string {
	const href = SETUP_STEP_HREF[id];
	return fromPath && href.startsWith(`${fromPath}?`) ? href.slice(fromPath.length) : href;
}

const TRACKED: readonly TrackedSetupStep[] = [
	'company',
	'users',
	'security',
	'erp',
	'vendors',
	'workflow',
	'email_intake'
];

function isTracked(id: SetupStepId): id is TrackedSetupStep {
	return (TRACKED as readonly string[]).includes(id);
}

/**
 * `true` / `false` from the server's facts, or `null` for a step with no fact
 * behind it — rendered without a done state rather than with an invented one.
 * `null` progress (not loaded, or not readable by this user) is `null` too.
 */
export function setupStepDone(id: SetupStepId, progress: OnboardingProgress | null): boolean | null {
	if (id === 'first_invoice') return false;
	if (!progress || !isTracked(id)) return null;
	return progress[id];
}

/** The dashboard checklist, in the order the `start-admin` guide recommends. */
export const DASHBOARD_SETUP_STEPS = [
	'company',
	'users',
	'security',
	'erp',
	'vendors',
	'workflow',
	'first_invoice'
] as const satisfies readonly SetupStepId[];

/** How many of `steps` are done, over how many carry a done state at all. */
export function setupProgressCount(
	steps: readonly SetupStepId[],
	progress: OnboardingProgress | null
): { done: number; total: number } {
	const states = steps.map((id) => setupStepDone(id, progress)).filter((d) => d !== null);
	return { done: states.filter(Boolean).length, total: states.length };
}
