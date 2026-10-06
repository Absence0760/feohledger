import type { Action } from 'svelte/action';

interface FocusTrapParams {
	/** Invoked on Escape pressed while focus is within the trapped element. */
	onEscape?: () => void;
}

const FOCUSABLE =
	'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

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

	// Visible, focusable descendants in DOM order.
	function focusable(): HTMLElement[] {
		return Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
			(el) => el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement
		);
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onEscape?.();
			return;
		}
		if (e.key !== 'Tab') return;
		const items = focusable();
		if (items.length === 0) {
			// Nothing tabbable but the dialog itself — hold focus on it.
			e.preventDefault();
			node.focus();
			return;
		}
		const first = items[0];
		const last = items[items.length - 1];
		const active = document.activeElement as HTMLElement;
		if (e.shiftKey && (active === first || !node.contains(active))) {
			e.preventDefault();
			last.focus();
		} else if (!e.shiftKey && active === last) {
			e.preventDefault();
			first.focus();
		}
	}

	// Move focus into the dialog once its content is in the DOM (next microtask).
	// A dialog whose first control is NOT the safe default (a confirm laid out
	// "Leave · Stay") marks the one that is with `data-autofocus`.
	queueMicrotask(() => {
		const items = focusable();
		(items.find((el) => el.hasAttribute('data-autofocus')) ?? items[0] ?? node).focus();
	});
	node.addEventListener('keydown', onKey);

	return {
		update(p?: FocusTrapParams) {
			onEscape = p?.onEscape;
		},
		destroy() {
			node.removeEventListener('keydown', onKey);
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
