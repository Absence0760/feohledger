import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	createToastQueue,
	DEFAULT_TOAST_MS,
	MAX_PERSISTENT_ERRORS,
	type ToastMessage
} from './toastQueue.ts';

// decisions §313 — WCAG 2.2.1: errors stay until dismissed; everything else
// pauses while hovered / focused and resumes with the time it had left.

function setup() {
	let latest: ToastMessage[] = [];
	const q = createToastQueue({
		onChange: (items) => (latest = items),
		clock: {
			setTimeout: (fn, ms) => setTimeout(fn, ms),
			clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
			now: () => Date.now()
		}
	});
	return { q, texts: () => latest.map((t) => t.text) };
}

beforeEach(() => {
	vi.useFakeTimers();
});
afterEach(() => {
	vi.useRealTimers();
});

describe('toast queue timing', () => {
	it('times out a non-error toast after the default duration', () => {
		const { q, texts } = setup();
		q.push('saved', 'success');
		vi.advanceTimersByTime(DEFAULT_TOAST_MS - 1);
		expect(texts()).toEqual(['saved']);
		vi.advanceTimersByTime(1);
		expect(texts()).toEqual([]);
	});

	it('keeps an explicit duration on a non-error toast', () => {
		const { q, texts } = setup();
		q.push('careful', 'warning', 12_000);
		vi.advanceTimersByTime(11_999);
		expect(texts()).toEqual(['careful']);
		vi.advanceTimersByTime(1);
		expect(texts()).toEqual([]);
	});

	it('never times out an error, explicit duration or not', () => {
		const { q, texts } = setup();
		q.push('failed', 'error');
		q.push('also failed', 'error', 1000);
		vi.advanceTimersByTime(10 * 60_000);
		expect(texts()).toEqual(['failed', 'also failed']);
	});

	it('dismiss removes an error', () => {
		const { q, texts } = setup();
		const id = q.push('failed', 'error');
		q.dismiss(id);
		expect(texts()).toEqual([]);
	});

	it('pauses the clock and resumes with the time that was left', () => {
		const { q, texts } = setup();
		q.push('saved', 'success');
		vi.advanceTimersByTime(3000);
		q.setPaused(true);
		vi.advanceTimersByTime(60_000);
		expect(texts()).toEqual(['saved']);
		q.setPaused(false);
		vi.advanceTimersByTime(1999);
		expect(texts()).toEqual(['saved']);
		vi.advanceTimersByTime(1);
		expect(texts()).toEqual([]);
	});

	it('a toast pushed while paused starts its clock on resume', () => {
		const { q, texts } = setup();
		q.setPaused(true);
		q.push('saved', 'info');
		vi.advanceTimersByTime(60_000);
		expect(texts()).toEqual(['saved']);
		q.setPaused(false);
		vi.advanceTimersByTime(DEFAULT_TOAST_MS);
		expect(texts()).toEqual([]);
	});

	it('releases the pause once the last toast is dismissed', () => {
		const { q } = setup();
		const id = q.push('failed', 'error');
		q.setPaused(true);
		q.dismiss(id);
		expect(q.paused).toBe(false);
	});

	it('caps persistent errors, dropping the oldest', () => {
		const { q, texts } = setup();
		for (let i = 0; i < MAX_PERSISTENT_ERRORS + 2; i++) q.push(`e${i}`, 'error');
		const expected = Array.from(
			{ length: MAX_PERSISTENT_ERRORS },
			(_, i) => `e${i + 2}`
		);
		expect(texts()).toEqual(expected);
	});

	it('a repeated error replaces its earlier copy as a new entry', () => {
		const { q } = setup();
		const first = q.push('same', 'error');
		q.push('other', 'error');
		const again = q.push('same', 'error');
		expect(again).not.toBe(first);
		expect(q.items.map((t) => t.text)).toEqual(['other', 'same']);
	});

	it('the cap does not touch non-error toasts', () => {
		const { q, texts } = setup();
		q.push('ok', 'success');
		for (let i = 0; i < MAX_PERSISTENT_ERRORS + 1; i++) q.push(`e${i}`, 'error');
		expect(texts()[0]).toBe('ok');
		expect(texts().filter((t) => t.startsWith('e'))).toHaveLength(MAX_PERSISTENT_ERRORS);
	});
});
