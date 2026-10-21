/**
 * The toast queue's timing rules, kept out of `ui/Toast.svelte` so they can be
 * unit-tested with fake timers (decisions §313, WCAG 2.2.1 Timing Adjustable).
 *
 * - An ERROR toast never times out. It stays until the reader dismisses it,
 *   because it is often the only account of what went wrong and may carry an
 *   instruction. An explicit `duration` does not change that.
 * - Every other toast times out — `DEFAULT_TOAST_MS`, or the caller's explicit
 *   `duration` — but the clock stops while the reader is hovering over the
 *   toasts or has focus inside them (`setPaused(true)`), and runs on with the
 *   time that was left when they move away. A toast that arrives while paused
 *   starts its clock on resume.
 * - Persistent errors are capped at `MAX_PERSISTENT_ERRORS`; past it the
 *   OLDEST error goes, so a page that fails repeatedly cannot fill the screen.
 *   The same error text arriving again replaces its earlier copy rather than
 *   stacking a duplicate — and, being a fresh insertion, is announced again.
 *
 * Pure: no Svelte, no DOM. The component owns the reactive list and the
 * hover/focus tracking; this owns ids, timers and the list's contents.
 */

export type ToastType = 'success' | 'error' | 'info' | 'warning';

export interface ToastMessage {
	id: number;
	type: ToastType;
	text: string;
}

export const DEFAULT_TOAST_MS = 5000;
export const MAX_PERSISTENT_ERRORS = 3;

/** The two timer calls the queue makes — injectable for tests. */
export interface ToastClock {
	setTimeout(fn: () => void, ms: number): unknown;
	clearTimeout(handle: unknown): void;
	now(): number;
}

const realClock: ToastClock = {
	setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
	clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
	now: () => Date.now()
};

interface Timer {
	/** Milliseconds left on this toast's clock. */
	remaining: number;
	/** When the clock last started running; null while stopped. */
	startedAt: number | null;
	handle: unknown;
}

export interface ToastQueue {
	push(text: string, type?: ToastType, duration?: number): number;
	dismiss(id: number): void;
	setPaused(paused: boolean): void;
	readonly paused: boolean;
	readonly items: readonly ToastMessage[];
}

export function createToastQueue(opts: {
	onChange: (items: ToastMessage[]) => void;
	clock?: ToastClock;
	maxPersistentErrors?: number;
}): ToastQueue {
	const clock = opts.clock ?? realClock;
	const maxErrors = opts.maxPersistentErrors ?? MAX_PERSISTENT_ERRORS;
	let items: ToastMessage[] = [];
	const timers = new Map<number, Timer>();
	let nextId = 0;
	let paused = false;

	function publish() {
		opts.onChange(items);
	}

	function start(id: number, t: Timer) {
		t.startedAt = clock.now();
		t.handle = clock.setTimeout(() => dismiss(id), t.remaining);
	}

	function stop(t: Timer) {
		if (t.startedAt === null) return;
		clock.clearTimeout(t.handle);
		t.remaining = Math.max(0, t.remaining - (clock.now() - t.startedAt));
		t.startedAt = null;
		t.handle = undefined;
	}

	function remove(ids: Set<number>) {
		for (const id of ids) {
			const t = timers.get(id);
			if (t) {
				stop(t);
				timers.delete(id);
			}
		}
		items = items.filter((t) => !ids.has(t.id));
	}

	function dismiss(id: number) {
		if (!items.some((t) => t.id === id)) return;
		remove(new Set([id]));
		// Nothing left to hover or focus: a pause can't be released by a
		// pointer or focus leaving an element that no longer exists.
		if (items.length === 0) paused = false;
		publish();
	}

	function push(text: string, type: ToastType = 'info', duration?: number): number {
		const id = nextId++;
		if (type === 'error') {
			const evict = new Set(
				items.filter((t) => t.type === 'error' && t.text === text).map((t) => t.id)
			);
			const errors = items.filter((t) => t.type === 'error' && !evict.has(t.id));
			// Room for the new one: drop the oldest past the cap.
			for (const t of errors.slice(0, Math.max(0, errors.length - (maxErrors - 1)))) {
				evict.add(t.id);
			}
			if (evict.size) remove(evict);
		} else {
			const timer: Timer = {
				remaining: duration ?? DEFAULT_TOAST_MS,
				startedAt: null,
				handle: undefined
			};
			timers.set(id, timer);
			if (!paused) start(id, timer);
		}
		items = [...items, { id, type, text }];
		publish();
		return id;
	}

	function setPaused(next: boolean) {
		if (next === paused) return;
		paused = next;
		for (const [id, t] of timers) {
			if (paused) stop(t);
			else start(id, t);
		}
	}

	return {
		push,
		dismiss,
		setPaused,
		get paused() {
			return paused;
		},
		get items() {
			return items;
		}
	};
}
