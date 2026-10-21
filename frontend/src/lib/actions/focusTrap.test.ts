import { describe, expect, it } from 'vitest';
import { tabBoundaryTarget } from './focusTrap';

/**
 * The dialog's Tab cycle with satellites (the toast region hosted inside an
 * open dialog — `ui/Toast.svelte`): own controls, then the toasts' dismiss
 * buttons, then back to the dialog's first control (WCAG 2.1.1 / 2.1.2). The
 * browser wiring is pinned by `tests-e2e/a11y/toast-timing.spec.ts`.
 */
const own = ['name', 'amount', 'cancel', 'save'];
const toasts = ['dismiss-1', 'dismiss-2'];

describe('tabBoundaryTarget', () => {
	it('moves natively between two of the dialog’s own controls', () => {
		expect(tabBoundaryTarget(own, toasts, 'name', false)).toBeNull();
		expect(tabBoundaryTarget(own, toasts, 'save', true)).toBeNull();
	});

	it('Tab from the dialog’s last control goes to the first toast', () => {
		expect(tabBoundaryTarget(own, toasts, 'save', false)).toBe('dismiss-1');
	});

	it('Tab between toasts is native; Tab from the last toast wraps to the dialog', () => {
		expect(tabBoundaryTarget(own, toasts, 'dismiss-1', false)).toBeNull();
		expect(tabBoundaryTarget(own, toasts, 'dismiss-2', false)).toBe('name');
	});

	it('Shift+Tab reverses it: first control → last toast, first toast → last control', () => {
		expect(tabBoundaryTarget(own, toasts, 'name', true)).toBe('dismiss-2');
		expect(tabBoundaryTarget(own, toasts, 'dismiss-1', true)).toBe('save');
		expect(tabBoundaryTarget(own, toasts, 'dismiss-2', true)).toBeNull();
	});

	it('with no toasts it is the plain wrap it always was', () => {
		expect(tabBoundaryTarget(own, [], 'save', false)).toBe('name');
		expect(tabBoundaryTarget(own, [], 'name', true)).toBe('save');
		expect(tabBoundaryTarget(own, [], 'amount', false)).toBeNull();
	});

	it('a dialog whose only tabbable controls are toasts cycles through them', () => {
		expect(tabBoundaryTarget([], toasts, 'dismiss-2', false)).toBe('dismiss-1');
		expect(tabBoundaryTarget([], toasts, 'dismiss-1', true)).toBe('dismiss-2');
		expect(tabBoundaryTarget([], ['only'], 'only', false)).toBe('only');
	});

	it('focus on the dialog itself (null) re-enters at the start, or the end going back', () => {
		expect(tabBoundaryTarget(own, toasts, null, false)).toBe('name');
		expect(tabBoundaryTarget(own, toasts, null, true)).toBe('dismiss-2');
	});

	it('a focused element in neither list (a tabindex=-1 heading) moves natively', () => {
		expect(tabBoundaryTarget(own, toasts, 'heading', false)).toBeNull();
		expect(tabBoundaryTarget(own, toasts, 'heading', true)).toBeNull();
	});

	it('nothing to cycle through returns null', () => {
		expect(tabBoundaryTarget([], [], null, false)).toBeNull();
	});
});
