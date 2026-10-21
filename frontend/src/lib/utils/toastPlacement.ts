/**
 * Where the toast stack sits so it never covers anything in an open dialog
 * (issue #635 finding G, #636; WCAG 2.4.11 Focus Not Obscured, 2.5.8).
 *
 * The stack lives in the bottom-right corner, above the consent banner and any
 * bulk-action bar (`bottomInset`). On a phone an open dialog is nearly as wide
 * as the viewport and up to 90vh tall, and on a desktop a wide dialog reaches
 * into the corner too — so a toast there sits over the dialog's fields and
 * buttons. Since error toasts stopped timing out (decisions §313), one stays
 * there until it is dismissed.
 *
 * Steering the stack AROUND the dialog's contents cannot be made to work: a
 * dialog is controls from top to bottom (the payment run's body is a column of
 * buttons), so on a phone there is no gap to put it in, and lifting it above
 * the footer only moved it onto the fields above. So the dialog makes room
 * instead. The rule:
 *
 * 1. The stack stays in the corner, capped at `TOAST_MAX_SHARE` of the
 *    viewport's height (it scrolls past that).
 * 2. While a dialog is open and the corner stack would touch the dialog's box,
 *    the dialog yields a band at the bottom of the viewport (`band`): it is
 *    laid out above it, shorter if need be (its own scrolling takes up the
 *    rest), and the stack sits in the band — beside the dialog, never over it.
 * 3. Once a dialog has yielded the band it keeps it (`docked`), and the band
 *    only grows, until the dialog closes: a dialog moving back as a toast
 *    leaves would slide its footer under the pointer that just dismissed it.
 *    A dialog the corner stack does not touch is left exactly where it is.
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
	/** Distance (px) from the viewport bottom the stack's bottom edge sits at:
	 *  the 16px margin, the consent banner, a bulk-action bar. */
	bottomInset: number;
	/** The open modal dialog's box, in viewport coordinates; null = none. */
	dialog: Box | null;
	/** That dialog already yields the band (see rule 3). */
	docked?: boolean;
}

export interface ToastPlacement {
	/** Distance (px) from the viewport bottom to the stack's bottom edge. */
	bottom: number;
	/** Cap on the stack's height (it scrolls past it); null = uncapped. */
	maxHeight: number | null;
	/** Height (px) of the band at the viewport bottom the open dialog must
	 *  leave free for the stack; null = the dialog is not in the way. */
	band: number | null;
}

/** Gutter between the stack and the viewport edge (matches the CSS). */
export const TOAST_MARGIN = 16;
/** Clearance kept between the stack and the dialog it sits beside. */
export const TOAST_CLEARANCE = 8;
/** The most of the viewport's height the stack may take before it scrolls —
 *  and so the most a dialog ever gives up to it. */
export const TOAST_MAX_SHARE = 0.4;

function intersects(a: Box, b: Box): boolean {
	return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

export function placeToasts(input: ToastPlacementInput): ToastPlacement {
	const { viewport, stack, bottomInset, dialog, docked = false } = input;
	const cap = Math.floor(viewport.height * TOAST_MAX_SHARE);
	const shown = Math.min(stack.height, cap);
	const maxHeight = stack.height > cap ? cap : null;

	const corner: Box = {
		top: viewport.height - bottomInset - shown - TOAST_CLEARANCE,
		bottom: viewport.height - bottomInset,
		left: viewport.width - TOAST_MARGIN - stack.width - TOAST_CLEARANCE,
		right: viewport.width - TOAST_MARGIN
	};
	const inTheWay = !!dialog && (docked || intersects(corner, dialog));
	return {
		bottom: bottomInset,
		maxHeight,
		band: inTheWay ? Math.ceil(bottomInset + shown + TOAST_CLEARANCE) : null
	};
}
