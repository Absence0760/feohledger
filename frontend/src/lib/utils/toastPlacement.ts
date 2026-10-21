/**
 * Where the toast stack sits so it never covers an open dialog's actions
 * (issue #635 finding G; WCAG 2.4.11 Focus Not Obscured).
 *
 * The stack lives in the bottom-right corner, above the consent banner and any
 * bulk-action bar (`bottomInset`). On a phone an open dialog is nearly as wide
 * as the viewport and up to 90vh tall, so its footer — Save, Create, Confirm —
 * lands exactly where the corner stack is. Since error toasts stopped timing out
 * (decisions §307), one there would sit over the button the reader needs to
 * retry with until they dismissed it.
 *
 * The rule, in order:
 * 1. The corner, when it does not touch the actions (always, with no dialog).
 * 2. Otherwise lifted to sit just above the actions, still bottom-anchored, so
 *    an error stays next to the button whose press caused it.
 * 3. When the whole stack does not fit above them, it is capped to the larger
 *    of the space above and the space below and scrolls (`maxHeight`). It never
 *    falls back to covering the actions: the stack scrolls, the actions don't.
 *
 * The focused control inside the dialog is a second thing to keep clear
 * (`focused`), for the same criterion: lifted above the footer, the stack sits
 * exactly over the last fields above it. Scrolling the dialog cannot fix that —
 * the footer scrolls with the form in every dialog here, and the stack follows
 * the footer — so the stack moves instead: to just above whichever of the two
 * is lowest with room above it, else capped beside both.
 *
 * Pure: the component measures the DOM and applies the result; this owns only
 * the geometry, so it unit-tests without a browser.
 */

export interface Box {
	top: number;
	right: number;
	bottom: number;
	left: number;
}

export interface ToastPlacementInput {
	viewport: { width: number; height: number };
	/** The stack's natural size — its full content height, not a capped one. */
	stack: { width: number; height: number };
	/** Distance (px) from the viewport bottom the stack's bottom edge sits at
	 *  when nothing is in the way: the 16px margin, the consent banner, a
	 *  bulk-action bar. */
	bottomInset: number;
	/** The open dialog's action row, in viewport coordinates; null = none. */
	avoid: Box | null;
	/** The focused control inside the open dialog (not a toast's own button),
	 *  in viewport coordinates; null/omitted = none to keep clear. */
	focused?: Box | null;
}

export interface ToastPlacement {
	/** Distance (px) from the viewport bottom to the stack's bottom edge. */
	bottom: number;
	/** Cap on the stack's height (it scrolls past it); null = uncapped. */
	maxHeight: number | null;
}

/** Gutter between the stack and the viewport edge (matches the CSS). */
export const TOAST_MARGIN = 16;
/** Clearance kept between the stack and the actions it avoids. */
export const TOAST_CLEARANCE = 8;

function intersects(a: Box, b: Box): boolean {
	return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

function keepClearOf(b: Box): Box {
	return {
		top: b.top - TOAST_CLEARANCE,
		bottom: b.bottom + TOAST_CLEARANCE,
		left: b.left - TOAST_CLEARANCE,
		right: b.right + TOAST_CLEARANCE
	};
}

export function placeToasts(input: ToastPlacementInput): ToastPlacement {
	const { viewport, stack, bottomInset, avoid, focused = null } = input;
	const left = viewport.width - TOAST_MARGIN - stack.width;
	const right = viewport.width - TOAST_MARGIN;
	const at = (bottom: number): Box => ({
		top: viewport.height - bottom - stack.height,
		bottom: viewport.height - bottom,
		left,
		right
	});

	const obstacles = [avoid, focused].filter((b): b is Box => b !== null).map(keepClearOf);
	const clear = (b: Box) => obstacles.every((o) => !intersects(b, o));
	if (clear(at(bottomInset))) return { bottom: bottomInset, maxHeight: null };

	// Just above an obstacle (never lower than the corner would put it), the
	// lowest one that fits whole and covers nothing.
	const lifts = obstacles
		.map((o) => Math.max(bottomInset, viewport.height - o.top))
		.sort((a, b) => a - b);
	for (const bottom of lifts) {
		const box = at(bottom);
		if (box.top >= TOAST_MARGIN && clear(box)) return { bottom, maxHeight: null };
	}

	// Too tall for any band whole: cap it to the larger of the space above and
	// below everything it must keep clear.
	const top = Math.min(...obstacles.map((o) => o.top));
	const lowest = Math.max(...obstacles.map((o) => o.bottom));
	const lifted = Math.max(bottomInset, viewport.height - top);
	const roomAbove = viewport.height - lifted - TOAST_MARGIN;
	const roomBelow = viewport.height - bottomInset - lowest;
	return roomBelow > roomAbove
		? { bottom: bottomInset, maxHeight: Math.max(0, Math.floor(roomBelow)) }
		: { bottom: lifted, maxHeight: Math.max(0, Math.floor(roomAbove)) };
}
