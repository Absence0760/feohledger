import { describe, expect, it } from 'vitest';
import {
	placeToasts,
	TOAST_CLEARANCE,
	TOAST_MARGIN,
	TOAST_MAX_SHARE,
	type Box
} from './toastPlacement';

// A 375×667 phone; the stack is one wrapped error toast.
const viewport = { width: 375, height: 667 };
const stack = { width: 343, height: 70 };
const corner = TOAST_MARGIN;

/** The stack's box for a placement, as the browser would lay it out. */
function stackBox(vp: { width: number; height: number }, bottom: number, height: number, width: number): Box {
	return {
		top: vp.height - bottom - height,
		bottom: vp.height - bottom,
		left: vp.width - TOAST_MARGIN - width,
		right: vp.width - TOAST_MARGIN
	};
}

function overlaps(a: Box, b: Box) {
	return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

/** Where a dialog that yields `band` ends: nothing of it below this line. */
const dialogFloor = (vp: { height: number }, band: number) => vp.height - band;

describe('placeToasts', () => {
	it('stays in the corner, and asks nothing of the page, with no dialog', () => {
		expect(placeToasts({ viewport, stack, bottomInset: corner, dialog: null })).toEqual({
			bottom: corner,
			maxHeight: null,
			band: null
		});
	});

	it('leaves a dialog the corner stack does not touch exactly where it is', () => {
		// A short dialog centred on the phone: it ends well above the stack.
		const dialog = { top: 200, bottom: 460, left: 15, right: 360 };
		expect(placeToasts({ viewport, stack, bottomInset: corner, dialog })).toEqual({
			bottom: corner,
			maxHeight: null,
			band: null
		});
	});

	it('a dialog in the corner yields a band the stack sits in, beside it and never over it', () => {
		// A 90vh phone dialog: the corner stack would sit on its lower controls.
		const dialog = { top: 33, bottom: 634, left: 15, right: 360 };
		const placed = placeToasts({ viewport, stack, bottomInset: corner, dialog });
		expect(placed.bottom).toBe(corner);
		expect(placed.maxHeight).toBeNull();
		expect(placed.band).toBe(corner + stack.height + TOAST_CLEARANCE);
		// Laid out above the band, the dialog and the stack do not meet.
		const yielded = { ...dialog, bottom: dialogFloor(viewport, placed.band!) };
		expect(overlaps(stackBox(viewport, placed.bottom, stack.height, stack.width), yielded)).toBe(false);
	});

	it('on a desktop, a wide dialog reaching into the corner yields too', () => {
		const desk = { width: 1280, height: 800 };
		const s = { width: 420, height: 48 };
		const dialog = { top: 40, bottom: 760, left: 230, right: 1050 };
		const placed = placeToasts({ viewport: desk, stack: s, bottomInset: corner, dialog });
		expect(placed.band).toBe(corner + s.height + TOAST_CLEARANCE);
	});

	it('a narrow dialog the stack is beside, not under, is left alone', () => {
		const desk = { width: 1280, height: 800 };
		const s = { width: 420, height: 48 };
		const dialog = { top: 40, bottom: 760, left: 400, right: 820 };
		expect(placeToasts({ viewport: desk, stack: s, bottomInset: corner, dialog }).band).toBeNull();
	});

	it('keeps the band once yielded, so the dialog does not jump back and forth', () => {
		// Laid out above the band, the dialog no longer meets the corner — the
		// band stays while toasts show.
		const dialog = { top: 33, bottom: 570, left: 15, right: 360 };
		const placed = placeToasts({ viewport, stack, bottomInset: corner, dialog, docked: true });
		expect(placed.band).toBe(corner + stack.height + TOAST_CLEARANCE);
	});

	it('a tall stack is capped and scrolls; the band never exceeds that share', () => {
		const tall = { width: 343, height: 600 };
		const dialog = { top: 33, bottom: 634, left: 15, right: 360 };
		const placed = placeToasts({ viewport, stack: tall, bottomInset: corner, dialog });
		const cap = Math.floor(viewport.height * TOAST_MAX_SHARE);
		expect(placed.maxHeight).toBe(cap);
		expect(placed.band).toBe(corner + cap + TOAST_CLEARANCE);
		// The dialog keeps the rest of the screen.
		expect(dialogFloor(viewport, placed.band!)).toBeGreaterThan(viewport.height / 2);
	});

	it('the band sits above the corner inset (bulk bar, consent banner)', () => {
		const dialog = { top: 33, bottom: 634, left: 15, right: 360 };
		const placed = placeToasts({ viewport, stack, bottomInset: 120, dialog });
		expect(placed.bottom).toBe(120);
		expect(placed.band).toBe(120 + stack.height + TOAST_CLEARANCE);
	});

	it('at 320px wide the stack clears a full-width dialog', () => {
		const narrow = { width: 320, height: 568 };
		const s = { width: 288, height: 90 };
		const dialog = { top: 28, bottom: 540, left: 13, right: 307 };
		const placed = placeToasts({ viewport: narrow, stack: s, bottomInset: corner, dialog });
		const yielded = { ...dialog, bottom: dialogFloor(narrow, placed.band!) };
		const box = stackBox(narrow, placed.bottom, s.height, s.width);
		expect(overlaps(box, yielded)).toBe(false);
		expect(box.top).toBeGreaterThanOrEqual(TOAST_MARGIN);
	});
});
