import { describe, expect, it } from 'vitest';

import { menuKeyTarget, placeMenu, triggerKeyAction, typeaheadTarget } from './menuButton';

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
