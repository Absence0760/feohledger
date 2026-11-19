import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import MenuButton from './MenuButton.svelte';

/**
 * The closed menu button's markup: the APG trigger contract (`aria-haspopup`
 * = menu, `aria-expanded` false, the shared button class) and no menu in the
 * DOM until it opens. The keyboard rules are pure and tested in
 * `utils/menuButton.test.ts`; opening, moving, choosing and closing in a real
 * browser are `tests-e2e/a11y/menu-button.spec.ts`.
 */

const items = [
	{ value: 'csv', label: 'CSV' },
	{ value: 'json', label: 'JSON' }
] as const;

function html(props: Record<string, unknown> = {}): string {
	return render(MenuButton, {
		props: { label: 'Export', items, onselect: () => {}, ...props }
	}).body;
}

function trigger(props: Record<string, unknown> = {}): string {
	const tag = html(props).match(/<button\b[^>]*>/);
	if (!tag) throw new Error('no trigger rendered');
	return tag[0];
}

describe('MenuButton (closed)', () => {
	it('renders an APG menu-button trigger', () => {
		const tag = trigger();
		expect(tag).toMatch(/aria-haspopup="menu"/);
		expect(tag).toMatch(/aria-expanded="false"/);
		expect(tag).toMatch(/type="button"/);
		expect(tag).not.toMatch(/aria-controls/);
	});

	it('wears the shared button class for its variant', () => {
		expect(trigger()).toMatch(/class="btn-secondary menu-trigger/);
		expect(trigger({ variant: 'ghost' })).toMatch(/class="btn-ghost menu-trigger/);
	});

	it('names the trigger by its label', () => {
		expect(html()).toMatch(/>\s*(<!--[^>]*-->)*\s*Export/);
	});

	it('renders no menu or items until it is opened', () => {
		const body = html();
		expect(body).not.toMatch(/role="menu"/);
		expect(body).not.toMatch(/role="menuitem"/);
	});

	it('passes disabled and the test id through to the trigger', () => {
		const tag = trigger({ disabled: true, testId: 'export' });
		expect(tag).toMatch(/\sdisabled(=""|\s|>)/);
		expect(tag).toMatch(/data-testid="export"/);
	});
});
