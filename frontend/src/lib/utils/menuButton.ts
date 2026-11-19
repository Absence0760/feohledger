/**
 * Pure rules behind `ui/MenuButton.svelte` — the WAI-ARIA APG menu button
 * (https://www.w3.org/WAI/ARIA/apg/patterns/menu-button/).
 *
 * Kept out of the component so the keyboard model and the viewport placement
 * are unit-tested without a DOM (the frontend's vitest runs in Node).
 */

/** What a key pressed on the CLOSED trigger does: open, focusing which item. */
export function triggerKeyAction(key: string): 'first' | 'last' | null {
	switch (key) {
		case 'ArrowDown':
		case 'Enter':
		case ' ':
			return 'first';
		case 'ArrowUp':
			return 'last';
		default:
			return null;
	}
}

/**
 * The item index a key moves focus to inside an OPEN menu of `count` items,
 * or `null` when the key is not a movement key. Arrows wrap, as the APG
 * pattern recommends.
 */
export function menuKeyTarget(key: string, current: number, count: number): number | null {
	if (count <= 0) return null;
	switch (key) {
		case 'ArrowDown':
			return current < 0 ? 0 : (current + 1) % count;
		case 'ArrowUp':
			return current < 0 ? count - 1 : (current - 1 + count) % count;
		case 'Home':
		case 'PageUp':
			return 0;
		case 'End':
		case 'PageDown':
			return count - 1;
		default:
			return null;
	}
}

/**
 * Type-ahead: the next item after `current` whose label starts with the typed
 * character (case-insensitive), wrapping; `null` when none does or the key is
 * not a single printable character.
 */
export function typeaheadTarget(key: string, current: number, labels: readonly string[]): number | null {
	if (key.length !== 1 || key === ' ') return null;
	const needle = key.toLocaleLowerCase();
	for (let step = 1; step <= labels.length; step++) {
		const i = (current + step + labels.length) % labels.length;
		if (labels[i].trim().toLocaleLowerCase().startsWith(needle)) return i;
	}
	return null;
}

export interface Box {
	left: number;
	right: number;
	top: number;
	bottom: number;
}

export interface MenuPlacement {
	/** Open below the trigger, or above it when there is not room below. */
	side: 'below' | 'above';
	/** Horizontal shift (px) from the trigger-start alignment that keeps the menu on screen. */
	shiftX: number;
}

/** A viewport size alone, read as a box whose top-left corner is (0, 0). */
export interface Size {
	width: number;
	height: number;
}

function toBox(b: Box | Size): Box {
	return 'left' in b ? b : { left: 0, top: 0, right: b.width, bottom: b.height };
}

/**
 * Where to draw a menu of `size` next to `trigger` so it stays inside
 * `bounds` (keeping `margin` px clear of each edge). `bounds` is the visible
 * area the menu can be drawn in: the viewport, or — inside a scrolling pane —
 * the part of it the nearest clipping ancestor shows (`clipBounds`). A bare
 * `{ width, height }` is the viewport.
 *
 * Vertical: below by default; above only when it does not fit below AND
 * there is more room above (the bulk bar is pinned to the bottom of the
 * viewport, so its menus flip). Horizontal: aligned to the trigger's start
 * edge, then shifted left or right by exactly the overflow — and, when the
 * menu is wider than the bounds allow, pinned to the left margin (the
 * menu's own `max-width` keeps that case from arising at 320px).
 */
export function placeMenu(
	trigger: Box,
	size: { width: number; height: number },
	bounds: Box | Size,
	margin = 8,
	gap = 4
): MenuPlacement {
	const area = toBox(bounds);
	const below = area.bottom - trigger.bottom - gap - margin;
	const above = trigger.top - area.top - gap - margin;
	const side: MenuPlacement['side'] = size.height <= below || below >= above ? 'below' : 'above';

	let shiftX = 0;
	const right = trigger.left + size.width;
	if (right > area.right - margin) shiftX = area.right - margin - right;
	if (trigger.left + shiftX < area.left + margin) shiftX = area.left + margin - trigger.left;
	return { side, shiftX };
}

/** The slice of the DOM `clipBounds` reads — an `Element` satisfies it. */
export interface ClipNode {
	parentElement: ClipNode | null;
	getBoundingClientRect(): Box;
}

/**
 * The visible area a menu anchored inside `start` can be drawn in: the
 * viewport, cut down to the nearest ancestor that clips its overflow.
 *
 * The menu is `position: absolute` inside its trigger's wrapper, so an
 * ancestor with `overflow` other than `visible` (InvoiceModal's
 * `.form-scroll`, a scrolling table) hides whatever of it falls outside that
 * ancestor's box — measuring against `window.innerHeight` alone left the
 * e-invoice menu un-flipped and cut off at the bottom of the form pane. Only
 * the first such ancestor is used: it is the one the menu's position is
 * judged against, and the pane is normally the tighter box.
 *
 * `overflowOf` is `getComputedStyle` in the component, injected so this
 * stays testable in Node.
 */
export function clipBounds(
	start: ClipNode,
	viewport: Size,
	overflowOf: (node: ClipNode) => { overflowX: string; overflowY: string }
): Box {
	const view: Box = { left: 0, top: 0, right: viewport.width, bottom: viewport.height };
	for (let node = start.parentElement; node; node = node.parentElement) {
		const { overflowX, overflowY } = overflowOf(node);
		if (overflowX === 'visible' && overflowY === 'visible') continue;
		const r = node.getBoundingClientRect();
		return {
			left: Math.max(view.left, r.left),
			top: Math.max(view.top, r.top),
			right: Math.min(view.right, r.right),
			bottom: Math.min(view.bottom, r.bottom)
		};
	}
	return view;
}
