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

export function placeToasts(input: ToastPlacementInput): ToastPlacement {
	const { viewport, stack, bottomInset, avoid } = input;
	const left = viewport.width - TOAST_MARGIN - stack.width;
	const right = viewport.width - TOAST_MARGIN;
	const at = (bottom: number): Box => ({
		top: viewport.height - bottom - stack.height,
		bottom: viewport.height - bottom,
		left,
		right
	});

	if (!avoid) return { bottom: bottomInset, maxHeight: null };
	const keepClear: Box = {
		top: avoid.top - TOAST_CLEARANCE,
		bottom: avoid.bottom + TOAST_CLEARANCE,
		left: avoid.left - TOAST_CLEARANCE,
		right: avoid.right + TOAST_CLEARANCE
	};
	if (!intersects(at(bottomInset), keepClear)) return { bottom: bottomInset, maxHeight: null };

	// Just above the actions (never lower than the corner would put it).
	const lifted = Math.max(bottomInset, viewport.height - keepClear.top);
	const roomAbove = viewport.height - lifted - TOAST_MARGIN;
	if (stack.height <= roomAbove) return { bottom: lifted, maxHeight: null };

	// Too tall for either band whole: cap it to the larger one.
	const roomBelow = viewport.height - bottomInset - keepClear.bottom;
	return roomBelow > roomAbove
		? { bottom: bottomInset, maxHeight: Math.max(0, Math.floor(roomBelow)) }
		: { bottom: lifted, maxHeight: Math.max(0, Math.floor(roomAbove)) };
}
