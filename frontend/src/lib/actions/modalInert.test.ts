import { describe, expect, it } from 'vitest';

import { createInertStack, INERT_EXEMPT_SELECTOR, markOutside, type InertNode } from './modalInert';

/**
 * The inert stack (decisions §384): while a modal dialog is open, everything
 * outside the topmost one is `inert`, the app-wide live regions excepted, and
 * only the marks the stack made are ever taken off. vitest runs in node here,
 * so the tree is a minimal fake `Element`; the browser wiring is pinned by
 * `tests-e2e/a11y/recall-import-verify.spec.ts` (axe over two stacked
 * dialogs) and `tests-e2e/a11y/modal-inert.spec.ts`.
 */
class FakeEl implements InertNode {
	readonly children: FakeEl[] = [];
	parentElement: FakeEl | null = null;
	private attrs = new Map<string, string>();

	constructor(
		readonly name: string,
		readonly tagName = 'DIV'
	) {}

	append(...kids: FakeEl[]): this {
		for (const k of kids) {
			k.parentElement = this;
			this.children.push(k);
		}
		return this;
	}
	hasAttribute(n: string) {
		return this.attrs.has(n);
	}
	setAttribute(n: string, v: string) {
		this.attrs.set(n, v);
	}
	removeAttribute(n: string) {
		this.attrs.delete(n);
	}
	get inert() {
		return this.hasAttribute('inert');
	}
}

/**
 * The shape of the authenticated shell with the invoice dialog open and the
 * recall dialog stacked beside it (`InvoiceModal` renders the recall dialog as
 * a sibling of its own backdrop):
 *
 *   body
 *   ├─ app (display: contents)
 *   │  ├─ shell
 *   │  │  ├─ skip
 *   │  │  ├─ sidebar
 *   │  │  └─ main
 *   │  │     ├─ tabs
 *   │  │     └─ page
 *   │  │        ├─ table
 *   │  │        ├─ invBackdrop ─ invDialog ─ (invHeader, invForm)
 *   │  │        └─ recBackdrop ─ recDialog ─ (recForm)
 *   │  ├─ toasts   (the toast region, at home)
 *   │  ├─ consent
 *   │  └─ announcer
 *   └─ script
 */
function shell() {
	const n = (name: string, tag?: string) => new FakeEl(name, tag);
	const t = {
		body: n('body', 'BODY'),
		app: n('app'),
		shell: n('shell'),
		skip: n('skip', 'A'),
		sidebar: n('sidebar', 'NAV'),
		main: n('main', 'MAIN'),
		tabs: n('tabs'),
		page: n('page'),
		table: n('table'),
		invBackdrop: n('invBackdrop'),
		invDialog: n('invDialog'),
		invHeader: n('invHeader'),
		invForm: n('invForm', 'FORM'),
		recBackdrop: n('recBackdrop'),
		recDialog: n('recDialog'),
		recForm: n('recForm', 'FORM'),
		toasts: n('toasts'),
		consent: n('consent'),
		announcer: n('announcer'),
		script: n('script', 'SCRIPT')
	};
	t.invDialog.append(t.invHeader, t.invForm);
	t.invBackdrop.append(t.invDialog);
	t.recDialog.append(t.recForm);
	t.recBackdrop.append(t.recDialog);
	t.page.append(t.table, t.invBackdrop, t.recBackdrop);
	t.main.append(t.tabs, t.page);
	t.shell.append(t.skip, t.sidebar, t.main);
	t.app.append(t.shell, t.toasts, t.consent, t.announcer);
	t.body.append(t.app, t.script);
	return t;
}

const names = (els: readonly FakeEl[]) => els.map((e) => e.name).sort();

/** Moves `el` to the end of `parent`, as the toast region's relocation does. */
function moveTo(el: FakeEl, parent: FakeEl) {
	const old = el.parentElement;
	if (old) old.children.splice(old.children.indexOf(el), 1);
	parent.append(el);
}

describe('markOutside', () => {
	it('marks every sibling along the dialog’s ancestor path, and nothing on it or in it', () => {
		const t = shell();
		// Only the invoice dialog is open: take the recall backdrop out.
		t.page.children.splice(t.page.children.indexOf(t.recBackdrop), 1);
		const marked = markOutside(t.body, t.invDialog);
		expect(names(marked)).toEqual(['announcer', 'consent', 'sidebar', 'skip', 'table', 'tabs', 'toasts']);
		for (const el of [t.app, t.shell, t.main, t.page, t.invBackdrop, t.invDialog, t.invHeader, t.invForm]) {
			expect(el.inert, el.name).toBe(false);
		}
		// Non-rendered nodes are never touched.
		expect(t.script.inert).toBe(false);
	});

	it('keeps the exempt live regions, and writes no attribute onto them', () => {
		const t = shell();
		const marked = markOutside(t.body, t.invDialog, [t.toasts, t.announcer]);
		expect(t.toasts.inert).toBe(false);
		expect(t.announcer.inert).toBe(false);
		expect(names(marked)).not.toContain('toasts');
		expect(t.consent.inert).toBe(true);
	});

	it('keeps a live region hosted inside a background dialog, marking that dialog’s other content', () => {
		const t = shell();
		moveTo(t.toasts, t.invDialog); // hosted in the invoice dialog, recall opened over it
		const marked = markOutside(t.body, t.recDialog, [t.toasts]);
		expect(t.toasts.inert).toBe(false);
		expect(t.invBackdrop.inert).toBe(false); // on the region's path
		expect(names(marked)).toEqual(expect.arrayContaining(['invHeader', 'invForm', 'table']));
	});

	it('skips — and does not claim — an element that is already inert', () => {
		const t = shell();
		t.skip.setAttribute('inert', ''); // the layout's skip link behind the sidebar overlay
		const marked = markOutside(t.body, t.invDialog);
		expect(names(marked)).not.toContain('skip');
		expect(t.skip.inert).toBe(true);
	});

	it('marks nothing for a dialog that is not under the root', () => {
		const t = shell();
		const detached = new FakeEl('detached');
		expect(markOutside(t.body, detached)).toEqual([]);
		expect(t.app.inert).toBe(false);
	});

	it('ignores an exempt region that is not under the root', () => {
		const t = shell();
		const stray = new FakeEl('stray');
		expect(names(markOutside(t.body, t.invDialog, [stray]))).toContain('toasts');
	});
});

describe('createInertStack', () => {
	it('open: the page behind the dialog goes inert', () => {
		const t = shell();
		const stack = createInertStack(t.body);
		stack.sync(t.invDialog, [t.toasts]);
		expect(t.table.inert).toBe(true);
		expect(t.sidebar.inert).toBe(true);
		expect(t.recBackdrop.inert).toBe(true); // not open yet in this sense: not the top
		expect(t.invForm.inert).toBe(false);
	});

	it('nested open: a dialog stacked over the first makes the first inert', () => {
		const t = shell();
		const stack = createInertStack(t.body);
		stack.sync(t.invDialog, [t.toasts]);
		stack.sync(t.recDialog, [t.toasts]);
		expect(t.invBackdrop.inert).toBe(true);
		expect(t.recBackdrop.inert).toBe(false);
		expect(t.recForm.inert).toBe(false);
		expect(t.table.inert).toBe(true);
	});

	it('closing the top dialog restores exactly the state under the first', () => {
		const t = shell();
		const stack = createInertStack(t.body);
		stack.sync(t.invDialog, [t.toasts]);
		const underFirst = names(stack.marked());
		stack.sync(t.recDialog, [t.toasts]);
		stack.sync(t.invDialog, [t.toasts]); // the recall dialog closed
		expect(names(stack.marked())).toEqual(underFirst);
		expect(t.invBackdrop.inert).toBe(false);
		expect(t.invForm.inert).toBe(false);
	});

	it('closing out of order (the first dialog before the one over it) leaves the remaining top live', () => {
		const t = shell();
		const stack = createInertStack(t.body);
		stack.sync(t.invDialog, [t.toasts]);
		stack.sync(t.recDialog, [t.toasts]);
		// The invoice dialog closes first; the recall dialog is still the top.
		t.page.children.splice(t.page.children.indexOf(t.invBackdrop), 1);
		t.invBackdrop.parentElement = null;
		stack.sync(t.recDialog, [t.toasts]);
		expect(t.recForm.inert).toBe(false);
		expect(t.table.inert).toBe(true);
		stack.sync(null);
		expect(stack.marked()).toEqual([]);
		for (const el of [t.table, t.tabs, t.sidebar, t.skip, t.consent, t.announcer, t.toasts]) {
			expect(el.inert, el.name).toBe(false);
		}
	});

	it('closing the last dialog takes off every mark it made', () => {
		const t = shell();
		const stack = createInertStack(t.body);
		stack.sync(t.invDialog, [t.toasts]);
		stack.sync(t.recDialog, [t.toasts]);
		stack.sync(t.invDialog, [t.toasts]);
		stack.sync(null);
		const all = Object.values(t);
		expect(all.filter((el) => el.inert).map((el) => el.name)).toEqual([]);
	});

	it('preserves an inert the layout set, through open, nested open and close', () => {
		const t = shell();
		t.skip.setAttribute('inert', '');
		t.main.setAttribute('inert', ''); // e.g. the sidebar overlay: the dialog under it is NOT ours to fix
		const stack = createInertStack(t.body);
		stack.sync(t.invDialog, [t.toasts]);
		stack.sync(t.recDialog, [t.toasts]);
		stack.sync(t.invDialog, [t.toasts]);
		stack.sync(null);
		expect(t.skip.inert).toBe(true);
		expect(t.main.inert).toBe(true);
	});

	it('follows the toast region when it moves into the top dialog', () => {
		const t = shell();
		const stack = createInertStack(t.body);
		moveTo(t.toasts, t.invDialog);
		stack.sync(t.invDialog, [t.toasts]);
		stack.sync(t.recDialog, [t.toasts]); // recall opens; region still in the invoice dialog
		expect(t.invBackdrop.inert).toBe(false);
		expect(t.invForm.inert).toBe(true);
		moveTo(t.toasts, t.recDialog); // Toast relocates and re-syncs
		stack.sync(t.recDialog, [t.toasts]);
		expect(t.invBackdrop.inert).toBe(true);
		expect(t.invForm.inert).toBe(false); // un-marked: covered by the backdrop now
		expect(t.toasts.inert).toBe(false);
	});
});

describe('INERT_EXEMPT_SELECTOR', () => {
	it('names the toast region and SvelteKit’s route announcer', () => {
		expect(INERT_EXEMPT_SELECTOR).toContain('[data-dialog-satellite]');
		expect(INERT_EXEMPT_SELECTOR).toContain('#svelte-announcer');
	});
});
