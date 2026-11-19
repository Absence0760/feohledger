import { describe, expect, it } from 'vitest';

import {
	clipBounds,
	menuKeyTarget,
	placeMenu,
	triggerKeyAction,
	typeaheadTarget,
	type Box,
	type ClipNode
} from './menuButton';

/**
 * The keyboard model and viewport placement of `ui/MenuButton.svelte` (the
 * WAI-ARIA APG menu button). The rendered markup is pinned in
 * `components/ui/MenuButton.test.ts`; the real keyboard round trip in a
 * browser, in both themes, by `tests-e2e/a11y/menu-button.spec.ts`.
 */

describe('triggerKeyAction', () => {
	it('opens on the first item for Enter, Space and ArrowDown', () => {
		expect(triggerKeyAction('Enter')).toBe('first');
		expect(triggerKeyAction(' ')).toBe('first');
		expect(triggerKeyAction('ArrowDown')).toBe('first');
	});

	it('opens on the last item for ArrowUp', () => {
		expect(triggerKeyAction('ArrowUp')).toBe('last');
	});

	it('ignores every other key (Tab must still leave the trigger)', () => {
		expect(triggerKeyAction('Tab')).toBeNull();
		expect(triggerKeyAction('a')).toBeNull();
		expect(triggerKeyAction('Escape')).toBeNull();
	});
});

describe('menuKeyTarget', () => {
	it('moves down and wraps from the last item to the first', () => {
		expect(menuKeyTarget('ArrowDown', 0, 3)).toBe(1);
		expect(menuKeyTarget('ArrowDown', 2, 3)).toBe(0);
	});

	it('moves up and wraps from the first item to the last', () => {
		expect(menuKeyTarget('ArrowUp', 2, 3)).toBe(1);
		expect(menuKeyTarget('ArrowUp', 0, 3)).toBe(2);
	});

	it('jumps to the ends on Home and End', () => {
		expect(menuKeyTarget('Home', 2, 3)).toBe(0);
		expect(menuKeyTarget('End', 0, 3)).toBe(2);
	});

	it('enters the list from no current item', () => {
		expect(menuKeyTarget('ArrowDown', -1, 3)).toBe(0);
		expect(menuKeyTarget('ArrowUp', -1, 3)).toBe(2);
	});

	it('leaves non-movement keys and empty menus alone', () => {
		expect(menuKeyTarget('Enter', 0, 3)).toBeNull();
		expect(menuKeyTarget('Escape', 0, 3)).toBeNull();
		expect(menuKeyTarget('ArrowDown', -1, 0)).toBeNull();
	});
});

describe('typeaheadTarget', () => {
	const labels = ['CSV', 'JSON', 'XML'];

	it('jumps to the next item starting with the typed letter, any case', () => {
		expect(typeaheadTarget('x', 0, labels)).toBe(2);
		expect(typeaheadTarget('J', 2, labels)).toBe(1);
	});

	it('cycles through items sharing a first letter', () => {
		const two = ['Copy', 'Cut', 'Paste'];
		expect(typeaheadTarget('c', 0, two)).toBe(1);
		expect(typeaheadTarget('c', 1, two)).toBe(0);
	});

	it('ignores Space, named keys and letters nothing starts with', () => {
		expect(typeaheadTarget(' ', 0, labels)).toBeNull();
		expect(typeaheadTarget('ArrowDown', 0, labels)).toBeNull();
		expect(typeaheadTarget('q', 0, labels)).toBeNull();
	});
});

describe('placeMenu', () => {
	const viewport = { width: 320, height: 640 };
	const size = { width: 140, height: 120 };

	it('opens below a trigger with room under it, aligned to its start', () => {
		const trigger = { left: 20, right: 100, top: 100, bottom: 136 };
		expect(placeMenu(trigger, size, viewport)).toEqual({ side: 'below', shiftX: 0 });
	});

	it('flips above a trigger pinned to the bottom of the viewport (the bulk bar)', () => {
		const trigger = { left: 20, right: 100, top: 580, bottom: 616 };
		expect(placeMenu(trigger, size, viewport).side).toBe('above');
	});

	it('stays below when neither side fits but below has more room', () => {
		const trigger = { left: 20, right: 100, top: 60, bottom: 96 };
		const tall = { width: 140, height: 900 };
		expect(placeMenu(trigger, tall, viewport).side).toBe('below');
	});

	it('shifts left by exactly the overflow at 320px', () => {
		const trigger = { left: 250, right: 310, top: 100, bottom: 136 };
		// 250 + 140 = 390; it must end at 320 - 8 = 312.
		expect(placeMenu(trigger, size, viewport).shiftX).toBe(-78);
	});

	it('never pushes the menu past the left margin', () => {
		const trigger = { left: 2, right: 60, top: 100, bottom: 136 };
		expect(placeMenu(trigger, size, viewport).shiftX).toBe(6);
		const wide = { width: 400, height: 120 };
		expect(placeMenu({ ...trigger, left: 100 }, wide, viewport).shiftX).toBe(-92);
	});
});

describe('placeMenu inside a clipping box', () => {
	// InvoiceModal's `.form-scroll`: a pane from y=120 to y=520 in a 900px-tall
	// window, 400px wide starting at x=200.
	const pane: Box = { left: 200, right: 600, top: 120, bottom: 520 };
	const size = { width: 140, height: 160 };

	it('flips above a trigger near the bottom of the pane even with room left in the window', () => {
		const trigger = { left: 220, right: 300, top: 440, bottom: 476 };
		expect(placeMenu(trigger, size, { width: 1280, height: 900 }).side).toBe('below');
		expect(placeMenu(trigger, size, pane).side).toBe('above');
	});

	it('measures the room above from the pane top, not the window top', () => {
		// Neither side fits a 300px menu. In a 560px window there is more room
		// above (288 vs 212); inside the pane there is more below (172 vs 168).
		const trigger = { left: 220, right: 300, top: 300, bottom: 336 };
		const tall = { width: 140, height: 300 };
		expect(placeMenu(trigger, tall, { width: 1280, height: 560 }).side).toBe('above');
		expect(placeMenu(trigger, tall, pane).side).toBe('below');
	});

	it('shifts against the pane edges, not the window edges', () => {
		const trigger = { left: 520, right: 580, top: 200, bottom: 236 };
		// 520 + 140 = 660; it must end at 600 - 8 = 592.
		expect(placeMenu(trigger, size, pane).shiftX).toBe(-68);
		const nearLeft = { left: 202, right: 260, top: 200, bottom: 236 };
		expect(placeMenu(nearLeft, size, pane).shiftX).toBe(6);
	});
});

describe('clipBounds', () => {
	type Fake = ClipNode & { overflow: string };
	function node(overflow: string, rect: Box, parent: Fake | null = null): Fake {
		return { overflow, parentElement: parent, getBoundingClientRect: () => rect };
	}
	const style = (n: ClipNode) => {
		const o = (n as Fake).overflow;
		const [x, y = x] = o.split(' ');
		return { overflowX: x, overflowY: y };
	};
	const viewport = { width: 1000, height: 800 };
	const full: Box = { left: 0, top: 0, right: 1000, bottom: 800 };

	it('is the viewport when no ancestor clips', () => {
		const root = node('visible', full);
		const wrap = node('visible', { left: 10, top: 10, right: 50, bottom: 50 }, root);
		const menu = node('visible', { left: 0, top: 0, right: 1, bottom: 1 }, wrap);
		expect(clipBounds(menu, viewport, style)).toEqual(full);
	});

	it('uses the first ancestor whose overflow is not visible', () => {
		const pane = node('hidden', { left: 0, top: 0, right: 900, bottom: 700 });
		const scroll = node('visible auto', { left: 100, top: 120, right: 500, bottom: 520 }, pane);
		const wrap = node('visible', { left: 150, top: 300, right: 250, bottom: 336 }, scroll);
		const menu = node('visible', { left: 0, top: 0, right: 1, bottom: 1 }, wrap);
		expect(clipBounds(menu, viewport, style)).toEqual({ left: 100, top: 120, right: 500, bottom: 520 });
	});

	it('never extends past the viewport', () => {
		const tall = node('auto', { left: -20, top: 50, right: 1200, bottom: 2000 });
		const menu = node('visible', { left: 0, top: 0, right: 1, bottom: 1 }, tall);
		expect(clipBounds(menu, viewport, style)).toEqual({ left: 0, top: 50, right: 1000, bottom: 800 });
	});

	it('does not count the menu itself', () => {
		const menu = node('auto', { left: 300, top: 300, right: 310, bottom: 310 });
		expect(clipBounds(menu, viewport, style)).toEqual(full);
	});
});
