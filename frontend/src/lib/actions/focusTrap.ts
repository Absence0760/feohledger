import type { Action } from 'svelte/action';

interface FocusTrapParams {
	/** Invoked on Escape pressed while focus is within the trapped element. */
	onEscape?: () => void;
}

const FOCUSABLE =
	'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Marks a subtree that is hosted INSIDE a dialog without being part of its
 * content — today only the toast region (`ui/Toast.svelte`), which moves into
 * the open modal dialog so its live regions and dismiss buttons are not hidden
 * by `aria-modal` (WCAG 2.1.1, 4.1.3). Its controls join the Tab cycle AFTER
 * the dialog's own, never take the dialog's initial focus, and never count as
 * the dialog's action row.
 */
export const DIALOG_SATELLITE_ATTR = 'data-dialog-satellite';

/**
 * Where Tab / Shift+Tab goes at a boundary of the cycle
 * `own[0] … own[n-1] → satellite[0] … satellite[m-1] → own[0]`, or null to let
 * the browser move focus natively (anywhere that is not a boundary, so radio
 * groups and composite widgets keep their own Tab behaviour). `active` is null
 * when focus is on the dialog node itself or has strayed outside it: the cycle
 * is re-entered at its start going forward and at its end going back. A
 * focused element inside the dialog that is in neither list (a `tabindex="-1"`
 * heading) moves natively. Pure, so it unit-tests without a DOM
 * (`focusTrap.test.ts`).
 */
export function tabBoundaryTarget<T>(
	own: T[],
	satellites: T[],
	active: T | null,
	shift: boolean
): T | null {
	const cycle = [...own, ...satellites];
	if (cycle.length === 0) return null;
	if (active === null) return shift ? cycle[cycle.length - 1] : cycle[0];
	const at = cycle.indexOf(active);
	if (at === -1) return null;
	const boundary = shift
		? at === 0 || at === own.length // own[0] → end; first satellite → last own
		: at === cycle.length - 1 || at === own.length - 1; // last of all → start; last own → first satellite
	if (!boundary) return null;
	return cycle[(at + (shift ? -1 : 1) + cycle.length) % cycle.length];
}

/**
 * The open modal dialogs (those carrying `aria-modal="true"`), oldest first.
 * The toast region hosts itself in the topmost one (`ui/Toast.svelte`); this is
 * the deterministic signal for it — mounted/unmounted here, not inferred from
 * DOM mutations.
 */
const openModals: HTMLElement[] = [];
const modalListeners = new Set<() => void>();

/** The topmost open modal dialog, or null. */
export function topModalDialog(): HTMLElement | null {
	return openModals.at(-1) ?? null;
}

/** Calls `fn` whenever a modal dialog opens or closes; returns the unsubscribe. */
export function onModalDialogsChange(fn: () => void): () => void {
	modalListeners.add(fn);
	return () => modalListeners.delete(fn);
}

function notifyModals() {
	for (const fn of [...modalListeners]) fn();
}

/**
 * Traps keyboard focus inside `node` while it is mounted, then restores focus to
 * the element that was focused before it mounted. Implements WCAG 2.1.2 (No
 * Keyboard Trap — Tab / Shift+Tab wrap within the dialog instead of escaping to
 * the page behind it) and 2.4.3 (Focus Order — focus returns to the trigger on
 * close). The node should carry `tabindex="-1"` so it can hold focus itself when
 * it has no focusable descendants.
 *
 * This is the single shared implementation used by `ui/Modal.svelte` AND the
 * feature dialogs that pre-date it and still hand-roll their own shell
 * (`InvoiceModal`, `RunDetailModal`, `BulkRecodeGLModal`, the supplier-portal
 * discount-accept dialog) — so every dialog gets identical focus management
 * without a risky structural rewrite of those e2e-load-bearing modals.
 */
/**
 * Where focus goes when a dialog closes (WCAG 2.4.3 Focus Order).
 *
 * 1. Only when focus was actually LOST — sitting on `<body>`, which is where the
 *    browser puts it when the focused dialog node is removed. If something else
 *    already holds focus (a second dialog opened in the same flush, or a caller
 *    that moved focus deliberately), restoring would steal it back.
 * 2. To the trigger, if it is still in the document and accepts focus — a
 *    disabled button (a row whose action is now in flight) silently refuses
 *    `focus()`, which is why the check reads `activeElement` afterwards rather
 *    than trusting the call.
 * 3. Otherwise to the page's `<main id="main-content" tabindex="-1">` — the
 *    landmark both shells (`routes/+layout.svelte`, `routes/portal/+layout.svelte`)
 *    already expose as the skip-link target, so the user resumes inside the
 *    content they were working in instead of at the top of the document.
 *
 * Guard: `tests-e2e/a11y/screen-reader.spec.ts` (both the surviving-trigger and
 * the vanished-trigger cases).
 */
function restoreFocus(prev: HTMLElement | null): void {
	const active = document.activeElement;
	if (active && active !== document.body && active.isConnected) return;
	if (prev?.isConnected) {
		prev.focus?.();
		if (document.activeElement === prev) return;
	}
	document.getElementById('main-content')?.focus();
}

export const focusTrap: Action<HTMLElement, FocusTrapParams | undefined> = (node, params) => {
	let onEscape = params?.onEscape;
	// Where focus was before the dialog opened — restored on destroy (2.4.3).
	const prevFocused = (document.activeElement as HTMLElement) ?? null;

	// Visible, focusable descendants in DOM order, the dialog's own first and
	// any satellite's (the hosted toast region) after them.
	function focusable(): { own: HTMLElement[]; satellites: HTMLElement[] } {
		const own: HTMLElement[] = [];
		const satellites: HTMLElement[] = [];
		for (const el of node.querySelectorAll<HTMLElement>(FOCUSABLE)) {
			if (!(el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement)) continue;
			(el.closest(`[${DIALOG_SATELLITE_ATTR}]`) ? satellites : own).push(el);
		}
		return { own, satellites };
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onEscape?.();
			return;
		}
		if (e.key !== 'Tab') return;
		const { own, satellites } = focusable();
		if (own.length + satellites.length === 0) {
			// Nothing tabbable but the dialog itself — hold focus on it.
			e.preventDefault();
			node.focus();
			return;
		}
		const active = document.activeElement as HTMLElement | null;
		const inside = active && active !== node && node.contains(active) ? active : null;
		const target = tabBoundaryTarget(own, satellites, inside, e.shiftKey);
		if (target) {
			e.preventDefault();
			target.focus();
		}
	}

	// Move focus into the dialog once its content is in the DOM (next microtask).
	// A dialog whose first control is NOT the safe default (a confirm laid out
	// "Leave · Stay") marks the one that is with `data-autofocus`.
	// Never a satellite: a toast already showing is not where the dialog starts.
	queueMicrotask(() => {
		const { own } = focusable();
		(own.find((el) => el.hasAttribute('data-autofocus')) ?? own[0] ?? node).focus();
	});
	node.addEventListener('keydown', onKey);

	/**
	 * Focus dropped out of the open dialog to `<body>` — the focused control was
	 * disabled under it (Save turns into a disabled "Saving…" while the request
	 * runs) or removed. Nothing else would bring it back: the Tab handler above
	 * listens on the dialog, so the next Tab went to the top of the page behind
	 * it, out of a modal (2.1.2, 2.4.3). Focus goes to the dialog node itself
	 * (`tabindex="-1"`), from where Tab resumes at its first control. A
	 * satellite's own loss (a dismissed toast) is left to `ui/Toast.svelte`,
	 * which hands focus back to where it came from.
	 */
	function onFocusOut(e: FocusEvent) {
		if (e.relatedTarget) return;
		if ((e.target as Element | null)?.closest?.(`[${DIALOG_SATELLITE_ATTR}]`)) return;
		queueMicrotask(() => {
			if (node.isConnected && (document.activeElement === document.body || !document.activeElement)) {
				node.focus();
			}
		});
	}
	node.addEventListener('focusout', onFocusOut);

	// The same loss where the browser fires no `focusout` (focus fix-up when
	// the focused control is disabled): a Tab pressed with focus on `<body>`
	// while this is the topmost modal re-enters the dialog's cycle.
	function onDocumentKey(e: KeyboardEvent) {
		if (e.key !== 'Tab' || openModals.at(-1) !== node) return;
		const active = document.activeElement;
		if (active && active !== document.body) return;
		const { own, satellites } = focusable();
		e.preventDefault();
		(tabBoundaryTarget(own, satellites, null, e.shiftKey) ?? node).focus();
	}
	document.addEventListener('keydown', onDocumentKey);

	// Only a real modal registers: the toast region is hosted inside it.
	const modal = node.getAttribute('aria-modal') === 'true';
	if (modal) {
		openModals.push(node);
		notifyModals();
	}

	return {
		update(p?: FocusTrapParams) {
			onEscape = p?.onEscape;
		},
		destroy() {
			node.removeEventListener('keydown', onKey);
			node.removeEventListener('focusout', onFocusOut);
			document.removeEventListener('keydown', onDocumentKey);
			if (modal) {
				const i = openModals.indexOf(node);
				if (i !== -1) openModals.splice(i, 1);
				notifyModals();
			}
			// Deferred to a microtask, not done here. The action is torn down in
			// the SAME flush as whatever the dialog's action changed, so the
			// trigger may be about to disappear: approving a pending change
			// request from its dialog filters the row (and the button that opened
			// the dialog) out of the Pending list. Focusing it synchronously
			// "succeeds" and is then lost to `<body>` when the row unmounts a
			// moment later — a keyboard or screen-reader user is thrown back to
			// the top of the document (WCAG 2.4.3). After the flush the DOM is
			// settled, so the restore can see what really survived.
			queueMicrotask(() => restoreFocus(prevFocused));
		},
	};
};
