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

/**
 * Where to draw a menu of `size` next to `trigger` so it stays inside a
 * `viewport` (keeping `margin` px clear of each edge).
 *
 * Vertical: below by default; above only when it does not fit below AND
 * there is more room above (the bulk bar is pinned to the bottom of the
 * viewport, so its menus flip). Horizontal: aligned to the trigger's start
 * edge, then shifted left or right by exactly the overflow — and, when the
 * menu is wider than the viewport allows, pinned to the left margin (the
 * menu's own `max-width` keeps that case from arising at 320px).
 */
export function placeMenu(
	trigger: Box,
	size: { width: number; height: number },
	viewport: { width: number; height: number },
	margin = 8,
	gap = 4
): MenuPlacement {
	const below = viewport.height - trigger.bottom - gap - margin;
	const above = trigger.top - gap - margin;
	const side: MenuPlacement['side'] = size.height <= below || below >= above ? 'below' : 'above';

	let shiftX = 0;
	const right = trigger.left + size.width;
	if (right > viewport.width - margin) shiftX = viewport.width - margin - right;
	if (trigger.left + shiftX < margin) shiftX = margin - trigger.left;
	return { side, shiftX };
}
