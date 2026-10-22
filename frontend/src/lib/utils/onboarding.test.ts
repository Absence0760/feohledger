import { describe, expect, it } from 'vitest';
import type { OnboardingProgress } from '#lib/types/analytics.ts';
import {
	DASHBOARD_SETUP_STEPS,
	setupProgressCount,
	setupStepDone,
	setupStepHref,
	type SetupStepId
} from './onboarding.ts';

const NONE: OnboardingProgress = {
	company: false,
	users: false,
	security: false,
	erp: false,
	vendors: false,
	workflow: false,
	email_intake: false
};

describe('setup steps (docs/decisions.md §315)', () => {
	it('reads each tracked step from its own server flag, and nothing else', () => {
		for (const flag of Object.keys(NONE) as (keyof OnboardingProgress)[]) {
			const progress = { ...NONE, [flag]: true };
			for (const other of Object.keys(NONE) as (keyof OnboardingProgress)[]) {
				expect(setupStepDone(other, progress), `${other} with only ${flag} set`).toBe(other === flag);
			}
		}
	});

	it('never claims a done state it has no fact for', () => {
		// Settings with no reliable "done" fact are links only.
		for (const id of ['defaults', 'payments', 'branding'] as SetupStepId[]) {
			expect(setupStepDone(id, { ...NONE, company: true })).toBeNull();
		}
		// Progress not loaded, or not readable by this user: unknown, not "to do".
		expect(setupStepDone('company', null)).toBeNull();
	});

	it('the first invoice is the last dashboard step and is never done while it shows', () => {
		expect(DASHBOARD_SETUP_STEPS.at(-1)).toBe('first_invoice');
		const all = Object.fromEntries(Object.keys(NONE).map((k) => [k, true])) as unknown as OnboardingProgress;
		expect(setupStepDone('first_invoice', all)).toBe(false);
		expect(setupStepDone('first_invoice', null)).toBe(false);
	});

	it('counts done over the steps that carry a state', () => {
		expect(setupProgressCount(DASHBOARD_SETUP_STEPS, { ...NONE, company: true, vendors: true })).toEqual({
			done: 2,
			total: DASHBOARD_SETUP_STEPS.length
		});
		// Untracked steps are left out of the total rather than counted as undone.
		expect(setupProgressCount(['company', 'defaults', 'branding'], NONE)).toEqual({ done: 0, total: 1 });
	});

	it('links each step to the panel that owns it, relative on the page that owns it', () => {
		expect(setupStepHref('company')).toBe('/organization?section=company');
		expect(setupStepHref('company', '/organization')).toBe('?section=company');
		expect(setupStepHref('email_intake', '/organization')).toBe('?section=email-intake');
		// A step that lives on another page stays absolute wherever it is linked from.
		expect(setupStepHref('users', '/organization')).toBe('/admin');
		expect(setupStepHref('workflow', '/organization')).toBe('/workflows');
		expect(setupStepHref('vendors')).toBe('/vendors');
		expect(setupStepHref('first_invoice')).toBe('/invoices');
	});
});
