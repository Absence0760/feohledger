/**
 * While a modal dialog is open, everything outside the topmost one is `inert`
 * (decisions §384; WCAG 1.3.2, 2.4.3, 2.5.8).
 *
 * `aria-modal="true"` only ASKS assistive tech to stay inside the dialog, and
 * the focus trap only keeps Tab there. Neither stops a screen reader's virtual
 * cursor, a pointer, or a touch reaching the page behind, and axe's WCAG 2.2
 * `target-size` rule rightly reported the `/invoices` sort buttons behind the
 * invoice dialog as "partially obscured" targets. `inert` takes the background
 * out of focus, hit-testing and the accessibility tree at once, which is what
 * the sidebar overlay already does for its own page (`routes/+layout.svelte`).
 *
 * Driven by the open-modal registry in `actions/focusTrap.ts`, so every dialog
 * that registers there (a `role="dialog"` with `aria-modal="true"` and
 * `use:focusTrap` — `ui/Modal` and the hand-rolled shells alike) gets it with
 * no change of its own. The rules:
 *
 * - **What is marked.** Every element that neither is a keeper nor contains
 *   one, walked down from `<body>` along the keepers' ancestor paths only. The
 *   keepers are the topmost dialog and the app-wide live regions
 *   (`INERT_EXEMPT_SELECTOR`): an inert subtree is pruned from the
 *   accessibility tree, so a live region inside one announces nothing. The
 *   toast region normally hosts itself inside the topmost dialog
 *   (`ui/Toast.svelte`) and is covered by the dialog; it is kept explicitly for
 *   the moments it is elsewhere (at its home before the dialog's entrance
 *   animation ends, or still inside the dialog below a newly stacked one), and
 *   so that an `inert` attribute is never written onto the region itself — it
 *   would travel with it into the dialog. Toast calls `syncModalInert` again
 *   after each move, so the marks follow it.
 * - **Stacking.** Each sync first takes off every mark IT made, then marks
 *   afresh for the current topmost dialog. A second dialog opened over the
 *   first therefore makes the first inert, and closing it — or closing them
 *   in any order — restores exactly what the remaining stack needs.
 * - **Never clobber.** An element already `inert` when a sync marks (the
 *   layout's `<main>` and skip link behind the phone sidebar overlay) is left
 *   alone and is not recorded, so a later sync never removes it.
 * - **Content added later is not marked.** A node inserted at `<body>` level
 *   while a dialog is open (a download link, any future portal) stays usable;
 *   nothing in the app portals a picker or menu out of a dialog today, and
 *   one that did would keep working. Content added INSIDE a marked subtree
 *   inherits its inertness, as it should.
 *
 * The walk is written against `InertNode`, the slice of `Element` it uses, so
 * it unit-tests under vitest's node environment (`modalInert.test.ts`).
 */

/**
 * App-wide live regions that must stay live while a dialog is open: the toast
 * region (`data-dialog-satellite`, `ui/Toast.svelte`) and SvelteKit's route
 * announcer. A new app-level live region outside the routed page adds itself
 * here.
 */
export const INERT_EXEMPT_SELECTOR = '[data-dialog-satellite], #svelte-announcer';

/** Elements that render nothing; marking them would only churn attributes. */
const NON_RENDERED = new Set(['SCRIPT', 'STYLE', 'TEMPLATE', 'LINK', 'META', 'NOSCRIPT']);

/** The slice of `Element` the walk needs. */
export interface InertNode {
	readonly tagName: string;
	readonly children: ArrayLike<InertNode>;
	readonly parentElement: InertNode | null;
	hasAttribute(name: string): boolean;
	setAttribute(name: string, value: string): void;
	removeAttribute(name: string): void;
}

/** The ancestors of `node` strictly between it and `root`, or null if `root` is not one. */
function pathTo(root: InertNode, node: InertNode): InertNode[] | null {
	const chain: InertNode[] = [];
	for (let p = node.parentElement; p; p = p.parentElement) {
		if (p === root) return chain;
		chain.push(p);
	}
	return null;
}

/**
 * Marks every element under `root` that is neither `dialog` nor one of the
 * `exempt` live regions and contains none of them, skipping any already
 * `inert`. Returns the elements it marked — the only ones its caller may later
 * unmark. A dialog that is not under `root` (detached) marks nothing; an exempt
 * region that is not under it is ignored.
 */
export function markOutside<T extends InertNode>(root: T, dialog: T, exempt: readonly T[] = []): T[] {
	const dialogPath = pathTo(root, dialog);
	if (!dialogPath) return [];
	const keep = new Set<InertNode>([dialog]);
	const onPath = new Set<InertNode>(dialogPath);
	for (const region of exempt) {
		const path = pathTo(root, region);
		if (!path) continue;
		keep.add(region);
		for (const a of path) onPath.add(a);
	}
	const marked: T[] = [];
	const walk = (parent: InertNode) => {
		for (const child of Array.from(parent.children)) {
			if (keep.has(child)) continue;
			if (onPath.has(child)) {
				walk(child);
				continue;
			}
			if (NON_RENDERED.has(child.tagName.toUpperCase()) || child.hasAttribute('inert')) continue;
			child.setAttribute('inert', '');
			marked.push(child as T);
		}
	};
	walk(root);
	return marked;
}

/**
 * One owner of the marks for a document. `sync(top, exempt)` takes off its own
 * previous marks, then — when `top` is a dialog — marks everything outside it
 * and outside the `exempt` live regions.
 */
export function createInertStack<T extends InertNode>(root: T) {
	let marked: T[] = [];
	return {
		sync(top: T | null, exempt: readonly T[] = []): void {
			for (const el of marked) el.removeAttribute('inert');
			marked = top ? markOutside(root, top, exempt) : [];
		},
		/** The elements currently marked by this stack (for tests). */
		marked(): readonly T[] {
			return marked;
		}
	};
}

let documentStack: ReturnType<typeof createInertStack<HTMLElement>> | null = null;

/**
 * Brings the document's marks in line with `top`, the topmost open modal
 * dialog (or null when none is open). Called by `actions/focusTrap.ts` whenever
 * the registry changes and by `ui/Toast.svelte` after it moves its region.
 */
export function syncModalInert(top: HTMLElement | null): void {
	if (typeof document === 'undefined' || !document.body) return;
	documentStack ??= createInertStack<HTMLElement>(document.body);
	const exempt = top ? Array.from(document.querySelectorAll<HTMLElement>(INERT_EXEMPT_SELECTOR)) : [];
	documentStack.sync(top, exempt);
}
