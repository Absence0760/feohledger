import { describe, expect, it } from 'vitest';
import { placeToasts, TOAST_CLEARANCE, TOAST_MARGIN, type Box } from './toastPlacement';

// A 375×667 phone; the stack is one wrapped error toast.
const viewport = { width: 375, height: 667 };
const stack = { width: 343, height: 70 };
const corner = TOAST_MARGIN;

/** The stack's box for a placement, as the browser would lay it out. */
function stackBox(bottom: number, height: number): Box {
	return {
		top: viewport.height - bottom - height,
		bottom: viewport.height - bottom,
		left: viewport.width - TOAST_MARGIN - stack.width,
		right: viewport.width - TOAST_MARGIN
	};
}

function overlaps(a: Box, b: Box) {
	return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

describe('placeToasts', () => {
	it('stays in the corner when there is no dialog', () => {
		expect(placeToasts({ viewport, stack, bottomInset: corner, avoid: null })).toEqual({
			bottom: corner,
			maxHeight: null
		});
	});

	it('stays in the corner when the dialog’s actions are clear of it', () => {
		// A short dialog centred on the screen: its footer ends well above.
		const avoid = { top: 380, bottom: 420, left: 30, right: 345 };
		expect(placeToasts({ viewport, stack, bottomInset: corner, avoid })).toEqual({
			bottom: corner,
			maxHeight: null
		});
	});

	it('lifts above the actions when the corner would cover them', () => {
		// A tall dialog's footer at the bottom of the screen (90vh modal).
		const avoid = { top: 590, bottom: 636, left: 30, right: 345 };
		const placed = placeToasts({ viewport, stack, bottomInset: corner, avoid });
		expect(placed.maxHeight).toBeNull();
		expect(placed.bottom).toBe(viewport.height - (avoid.top - TOAST_CLEARANCE));
		expect(overlaps(stackBox(placed.bottom, stack.height), avoid)).toBe(false);
	});

	it('never sits lower than the corner inset (bulk bar, consent banner)', () => {
		const avoid = { top: 640, bottom: 660, left: 30, right: 345 };
		const placed = placeToasts({ viewport, stack, bottomInset: 120, avoid });
		expect(placed).toEqual({ bottom: 120, maxHeight: null });
	});

	it('caps and scrolls a stack too tall to fit above, rather than covering', () => {
		const tall = { width: 343, height: 400 };
		const avoid = { top: 300, bottom: 346, left: 30, right: 345 };
		const placed = placeToasts({ viewport, stack: tall, bottomInset: corner, avoid });
		// Below the footer is 667 - 16 - 354 = 297px; above is 292 - 16 = 276.
		expect(placed).toEqual({ bottom: corner, maxHeight: 297 });
		expect(overlaps(stackBox(placed.bottom, placed.maxHeight!), avoid)).toBe(false);
	});

	it('takes the band above when it is the larger one', () => {
		const tall = { width: 343, height: 600 };
		const avoid = { top: 560, bottom: 606, left: 30, right: 345 };
		const placed = placeToasts({ viewport, stack: tall, bottomInset: corner, avoid });
		expect(placed.bottom).toBe(viewport.height - (avoid.top - TOAST_CLEARANCE));
		expect(placed.maxHeight).toBe(avoid.top - TOAST_CLEARANCE - TOAST_MARGIN);
		expect(overlaps(stackBox(placed.bottom, placed.maxHeight!), avoid)).toBe(false);
	});

	it('at 320px wide still clears a full-width footer', () => {
		const narrow = { width: 320, height: 568 };
		const s = { width: 288, height: 90 };
		const avoid = { top: 495, bottom: 541, left: 13, right: 307 };
		const placed = placeToasts({ viewport: narrow, stack: s, bottomInset: corner, avoid });
		const box = {
			top: narrow.height - placed.bottom - s.height,
			bottom: narrow.height - placed.bottom,
			left: narrow.width - TOAST_MARGIN - s.width,
			right: narrow.width - TOAST_MARGIN
		};
		expect(overlaps(box, avoid)).toBe(false);
		expect(box.top).toBeGreaterThanOrEqual(TOAST_MARGIN);
	});
});
