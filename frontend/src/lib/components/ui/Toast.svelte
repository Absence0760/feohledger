<script lang="ts" module>
	import {
		createToastQueue,
		type ToastMessage,
		type ToastType
	} from '#lib/utils/toastQueue.ts';

	export type { ToastMessage, ToastType };

	let toasts = $state<ToastMessage[]>([]);

	// Timing lives in `utils/toastQueue.ts` (decisions §313): an error stays
	// until dismissed; anything else times out, but its clock stops while the
	// toasts are hovered or hold focus (WCAG 2.2.1).
	const queue = createToastQueue({ onChange: (items) => (toasts = items) });

	/**
	 * Show a toast. `duration` (ms) overrides the default for a non-error toast;
	 * an error ignores it and stays until the reader dismisses it.
	 */
	export function toast(text: string, type: ToastType = 'info', duration?: number) {
		queue.push(text, type, duration);
	}

	export function dismiss(id: number) {
		queue.dismiss(id);
	}
</script>

<script lang="ts">
	import { tick } from 'svelte';
	import {
		onModalDialogsChange,
		topModalDialog
	} from '#lib/actions/focusTrap.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { placeToasts, TOAST_MARGIN } from '#lib/utils/toastPlacement.ts';

	let region = $state<HTMLElement | null>(null);
	let hovered = $state(false);
	let focusInside = $state(false);
	// Where keyboard focus came from when it entered the region, so the last
	// dismissal can hand it back there rather than to the top of the page.
	let returnTo: HTMLElement | null = null;
	// Distance (px) from the viewport bottom to the region's bottom edge, and a
	// cap on its height (it scrolls past it). Measured while toasts show; null
	// = the CSS default corner (above the consent banner), uncapped.
	let placedBottom = $state<number | null>(null);
	let maxHeight = $state<number | null>(null);

	$effect(() => {
		queue.setPaused(hovered || focusInside);
	});

	/**
	 * While a modal dialog is open the region lives INSIDE it (appended last),
	 * and goes back to its place in the layout when the dialog closes.
	 *
	 * `aria-modal="true"` tells assistive tech that everything outside the
	 * dialog is inert: WebKit and Chromium prune it from the accessibility
	 * tree, so a region left outside would not be announced and its dismiss
	 * buttons could not be reached by a screen reader's cursor (the Shopify
	 * Polaris toast hit exactly this). And the dialog's focus trap kept Tab
	 * inside it, so a persistent error could not be dismissed by keyboard at
	 * all (WCAG 2.1.1). Inside the dialog both are true by construction: the
	 * live regions are in the modal subtree, and `actions/focusTrap` puts the
	 * dismiss buttons (marked `data-dialog-satellite`) after the dialog's own
	 * controls in its Tab cycle.
	 *
	 * The region stays `position: fixed`, so it still sits in the viewport
	 * corner, not in the dialog's flow. Its containing block is then the
	 * nearest ancestor with a transform / filter / backdrop-filter / contain:
	 * every modal backdrop has `backdrop-filter` and is itself
	 * `position: fixed; inset: 0`, so that box IS the viewport — the rule a
	 * modal backdrop must keep (`docs/ui-patterns.md` § Toast). Its z-index now
	 * stacks inside the backdrop's layer, above the dialog. The dialog's own
	 * entrance animation transforms it for 0.22s, which would clip and shift
	 * the region, so the move waits for that animation to finish.
	 *
	 * A toast that was inserted where a screen reader would not hear it — into
	 * a region that was detached (raised in the same update that closed the
	 * dialog: "Saved" + close) or outside the open modal — is re-inserted once
	 * the region has moved (`rekey`): live regions announce what is added to
	 * them, not the content of a subtree that is moved or re-attached.
	 */
	let host = $state<HTMLElement | null>(null);
	// Marks the region's own place in the layout while it is hosted elsewhere.
	let home: Comment | null = null;
	$effect(() => {
		host = topModalDialog();
		return onModalDialogsChange(() => (host = topModalDialog()));
	});
	$effect(() => {
		if (!region) return;
		const el = region;
		const marker = document.createComment('toast-region');
		el.before(marker);
		home = marker;
		return () => {
			if (el.previousSibling !== marker) relocate(el, marker.parentNode, marker.nextSibling);
			marker.remove();
			home = null;
		};
	});
	$effect(() => {
		const target = host;
		if (!region || !home) return;
		const el = region;
		if (!target) {
			if (el.previousSibling !== home) relocate(el, home.parentNode, home.nextSibling);
			return;
		}
		let cancelled = false;
		Promise.all(target.getAnimations().map((a) => a.finished.catch(() => undefined))).then(() => {
			if (!cancelled && target.isConnected) relocate(el, target, null);
		});
		return () => {
			cancelled = true;
		};
	});

	/**
	 * Move the region. Moving a focused node drops its focus; the focus
	 * bookkeeping is reset first so the hand-off logic below does not then
	 * "rescue" focus to a toast — the dialog's own focus handling (focus in on
	 * open, restore to the trigger on close) owns where focus goes.
	 */
	function relocate(el: HTMLElement, parent: Node | null, before: Node | null) {
		if (!parent) return;
		focusInside = false;
		returnTo = null;
		parent.insertBefore(el, before);
		for (const t of toasts) {
			if (!unheard.has(t.id)) continue;
			unheard.delete(t.id);
			rekey[t.id] = (rekey[t.id] ?? 0) + 1;
		}
	}

	// Toast ids inserted where no screen reader would hear them (see above),
	// and a per-id counter that re-keys (so re-inserts) them after a move.
	const unheard = new Set<number>();
	let rekey = $state<Record<number, number>>({});

	/** Records whether a toast element arrived somewhere it is announced. */
	function arrive(node: HTMLElement, id: number) {
		const modal = topModalDialog();
		const heard = node.isConnected && (!modal || modal.contains(node));
		if (heard) unheard.delete(id);
		else unheard.add(id);
	}

	/**
	 * Escape on a focused toast dismisses that toast — and only that: it is a
	 * native listener on the region so it runs before the dialog's own Escape
	 * (on the dialog node, an ancestor while hosted), which would close the
	 * dialog and could discard its edits.
	 */
	$effect(() => {
		if (!region) return;
		const el = region;
		function onKey(e: KeyboardEvent) {
			if (e.key !== 'Escape') return;
			const item = (e.target as Element | null)?.closest<HTMLElement>('[data-toast-id]');
			if (!item) return;
			e.stopPropagation();
			e.preventDefault();
			dismiss(Number(item.dataset.toastId));
		}
		el.addEventListener('keydown', onKey);
		return () => el.removeEventListener('keydown', onKey);
	});

	/**
	 * Re-read hover and focus from the page after the list changes. A toast can
	 * leave without its own `mouseleave` / `focusout` — dismissed, evicted by
	 * the error cap, replaced by a repeat — and a stale `hovered` would freeze
	 * every remaining timer. A focused toast that left hands focus on.
	 */
	$effect(() => {
		void toasts;
		tick().then(() => {
			if (!region) return;
			hovered = !!region.querySelector('.toast:hover');
			if (focusInside) handOffFocus();
		});
	});

	/**
	 * Keep the toasts clear of what the reader needs next (WCAG 2.4.11, 2.5.8).
	 * The region sits bottom-right, which is where the shared bulk-action bar
	 * sits too (centred, `bottom: 24px`) — while one is on screen the region
	 * rises above it — and where an open dialog reaches on a phone, or on a
	 * desktop when it is wide. `utils/toastPlacement.ts` decides; when the
	 * dialog is in the way, the dialog yields a band at the bottom of the
	 * viewport and the stack sits in it (`dock`), so a toast never covers any
	 * of the dialog's controls. Observed only while toasts are showing.
	 */
	$effect(() => {
		if (toasts.length === 0) {
			placedBottom = null;
			maxHeight = null;
			// The band is NOT given back here: the dialog moving back down as the
			// last toast goes would slide its footer under the pointer that just
			// dismissed it (on a phone, an armed "Confirm cancel"). It is given
			// back when the dialog closes or another takes its place.
			return;
		}
		const observed = new Set<Element>();
		let frame = 0;
		const resize = new ResizeObserver(() => schedule());
		function watch(els: (Element | null)[]) {
			const next = new Set(els.filter((el): el is Element => el !== null));
			for (const el of observed) if (!next.has(el)) resize.unobserve(el);
			for (const el of next) if (!observed.has(el)) resize.observe(el);
			observed.clear();
			for (const el of next) observed.add(el);
		}
		function measure() {
			frame = 0;
			if (!region) return;
			const dialog = topModalDialog();
			// The dialog a band was yielded by has closed or is no longer on top.
			if (dock && dock.dialog !== dialog) undock();
			// A bulk bar behind an open dialog's backdrop is not what the
			// reader is looking at; only the dialog is.
			const bar = dialog ? null : document.querySelector('.bulk-bar');
			watch([bar, dialog, region]);
			const vh = window.innerHeight;
			const consent =
				parseFloat(
					getComputedStyle(document.documentElement).getPropertyValue('--consent-banner-height')
				) || 0;
			const barLift = bar ? Math.max(0, vh - bar.getBoundingClientRect().top) + 8 : 0;
			const r = dialog?.getBoundingClientRect();
			const placed = placeToasts({
				viewport: { width: window.innerWidth, height: vh },
				stack: { width: region.offsetWidth, height: region.scrollHeight },
				bottomInset: Math.max(TOAST_MARGIN + consent, barLift),
				dialog: r ? { top: r.top, bottom: r.bottom, left: r.left, right: r.right } : null,
				docked: !!dock
			});
			placedBottom = placed.bottom;
			maxHeight = placed.maxHeight;
			if (dialog && placed.band !== null) yieldBand(dialog, placed.band);
		}
		function schedule() {
			if (!frame) frame = requestAnimationFrame(measure);
		}
		const mutations = new MutationObserver(schedule);
		mutations.observe(document.body, { childList: true, subtree: true });
		// The consent banner publishes its height on <html>'s style.
		const rootStyle = new MutationObserver(schedule);
		rootStyle.observe(document.documentElement, { attributes: true, attributeFilter: ['style'] });
		window.addEventListener('resize', schedule);
		// The topmost modal dialog (what the stack keeps clear of) changed.
		const offModals = onModalDialogsChange(schedule);
		measure();
		return () => {
			cancelAnimationFrame(frame);
			mutations.disconnect();
			rootStyle.disconnect();
			resize.disconnect();
			window.removeEventListener('resize', schedule);
			offModals();
		};
	});

	// The band is given back when its dialog closes or another dialog takes the
	// top — whether or not toasts are still showing — and when this component
	// goes.
	$effect(() => {
		const off = onModalDialogsChange(() => {
			if (dock && dock.dialog !== topModalDialog()) undock();
		});
		return () => {
			off();
			undock();
		};
	});

	/**
	 * The open dialog yielding the band at the bottom of the viewport that the
	 * stack sits in. Once yielded it is kept, and only ever grows, for as long
	 * as that dialog is open (`placeToasts`' `docked`): the dialog moving back
	 * as a toast leaves would slide its footer under the pointer that just
	 * dismissed the toast. Given back by `undock` when the dialog closes or
	 * another takes the top.
	 *
	 * Every dialog shell here is a `role="dialog"` box centred in a
	 * `position: fixed; inset: 0` backdrop (its parent) — the rule § Toast
	 * already sets for hosting the region. The backdrop's bottom padding grows
	 * to the band, so the dialog is centred in what is left; the dialog's
	 * height is capped to that space, since its own `max-height` is in `vh` and
	 * would ignore the padding. A dialog that is shorter keeps its height and
	 * only moves up. Its own scrolling (the dialog's, or its body's) takes the
	 * rest (a shell that never scrolled is made to) — so the focused control
	 * is scrolled back into view if the shortening hid it. The inline styles
	 * are saved and restored exactly (scroll positions are not: they stay
	 * where the reader left them). While the band is held a click in it does
	 * not close the dialog, and a new viewport recomputes the cap.
	 */
	let dock: {
		dialog: HTMLElement;
		backdrop: HTMLElement;
		saved: { maxHeight: string; overflowY: string; paddingBottom: string };
		band: number;
		/** The viewport the dialog's cap was computed for. */
		viewport: string;
		/** Swallows a click in the band (see `yieldBand`). */
		guard: (e: MouseEvent) => void;
		/** Recomputes the cap for a new viewport, toasts showing or not. */
		onResize: () => void;
	} | null = null;

	function yieldBand(dialog: HTMLElement, band: number) {
		const backdrop = dialog.parentElement;
		// Only a dialog centred in a full-viewport fixed backdrop (every shell
		// here, and the rule § Toast sets): padding anything else would push
		// page layout around.
		if (!backdrop || !isFullViewportBackdrop(backdrop)) return;
		const viewport = `${window.innerWidth}x${window.innerHeight}`;
		// Grow-only while the dialog is open: a toast leaving (a timed-out
		// success beside a persistent error) must not move the dialog either.
		// A new viewport (rotation, zoom, a resized window) always recomputes
		// the cap, or the dialog would keep a height that no longer fits.
		if (dock && band <= dock.band && dock.viewport === viewport) return;
		if (!dock) {
			// A click in the band is on the backdrop, which every shell reads
			// as "close": a tap aimed at a control where it was a moment ago
			// would close the dialog instead. The band is not the backdrop's
			// to give away, so its clicks are swallowed here.
			const guard = (e: MouseEvent) => {
				if (e.target === backdrop && dock && e.clientY >= window.innerHeight - dock.band) {
					e.stopPropagation();
				}
			};
			backdrop.addEventListener('click', guard, true);
			const onResize = () => {
				if (dock) yieldBand(dock.dialog, dock.band);
			};
			window.addEventListener('resize', onResize);
			dock = {
				dialog,
				backdrop,
				saved: {
					maxHeight: dialog.style.maxHeight,
					overflowY: dialog.style.overflowY,
					paddingBottom: backdrop.style.paddingBottom
				},
				band,
				viewport,
				guard,
				onResize
			};
		}
		dock.band = Math.max(dock.band, band);
		dock.viewport = viewport;
		band = dock.band;
		// Shortening the dialog must not scroll away what the reader was just
		// looking at — on a failed Save, the footer they retry from. Each
		// scroller in the dialog keeps its distance from its own bottom.
		const fromBottom = new Map(
			scrollersIn(dialog).map((el) => [el, el.scrollHeight - el.clientHeight - el.scrollTop])
		);
		// The dialog's own cap, read with ours lifted.
		dialog.style.maxHeight = dock.saved.maxHeight;
		const own = parseFloat(getComputedStyle(dialog).maxHeight);
		backdrop.style.paddingBottom = `${band}px`;
		const room = backdrop.clientHeight - parseFloat(getComputedStyle(backdrop).paddingTop) - band;
		dialog.style.maxHeight = `${Math.max(0, Number.isFinite(own) ? Math.min(own, room) : room)}px`;
		// A shell that never needed to scroll must now, or its content would
		// spill out of the shortened box into the band.
		if (getComputedStyle(dialog).overflowY === 'visible') dialog.style.overflowY = 'auto';
		for (const el of scrollersIn(dialog)) {
			el.scrollTop = el.scrollHeight - el.clientHeight - (fromBottom.get(el) ?? 0);
		}
		// And the focused control, wherever it is, stays in view (2.4.11).
		const focused = document.activeElement;
		if (
			focused instanceof HTMLElement &&
			focused !== dialog &&
			dialog.contains(focused) &&
			!region?.contains(focused)
		) {
			focused.scrollIntoView({ block: 'nearest' });
		}
	}

	function isFullViewportBackdrop(el: HTMLElement): boolean {
		if (getComputedStyle(el).position !== 'fixed') return false;
		const r = el.getBoundingClientRect();
		return r.top <= 0 && r.left <= 0 && r.bottom >= window.innerHeight && r.right >= window.innerWidth;
	}

	/** The dialog and every element inside it that scrolls vertically (not the
	 *  hosted toast region). */
	function scrollersIn(dialog: HTMLElement): HTMLElement[] {
		const found: HTMLElement[] = [];
		const consider = (el: HTMLElement) => {
			if (/(auto|scroll)/.test(getComputedStyle(el).overflowY)) found.push(el);
		};
		consider(dialog);
		for (const el of dialog.querySelectorAll<HTMLElement>('*')) {
			if (!region?.contains(el)) consider(el);
		}
		return found;
	}

	function undock() {
		if (!dock) return;
		const { dialog, backdrop, saved, guard, onResize } = dock;
		dock = null;
		backdrop.removeEventListener('click', guard, true);
		window.removeEventListener('resize', onResize);
		dialog.style.maxHeight = saved.maxHeight;
		dialog.style.overflowY = saved.overflowY;
		backdrop.style.paddingBottom = saved.paddingBottom;
	}

	function onFocusIn(e: FocusEvent) {
		const from = e.relatedTarget as HTMLElement | null;
		if (!focusInside && from && !region?.contains(from)) returnTo = from;
		focusInside = true;
	}

	function onFocusOut(e: FocusEvent) {
		const next = e.relatedTarget as Node | null;
		if (next) {
			focusInside = !!region?.contains(next);
			return;
		}
		// No destination: the focused toast is being removed (Chromium fires
		// `focusout` on removal), or the window lost focus. Decide once the DOM
		// has settled.
		tick().then(() => {
			if (focusInside) handOffFocus();
		});
	}

	/**
	 * The focused toast is gone: move focus to the next toast's dismiss button,
	 * or — when none is left — back where it came from, falling back to the
	 * page's `<main>` (the skip-link target), never `<body>` (WCAG 2.4.3).
	 */
	function handOffFocus() {
		// Idempotent: only acts when focus has actually been dropped. Both the
		// list-change effect and `focusout` may call it for one removal.
		const active = document.activeElement;
		if (active && active !== document.body) {
			focusInside = !!region?.contains(active);
			return;
		}
		const next = region?.querySelector<HTMLButtonElement>('.toast-dismiss');
		if (next) {
			next.focus();
			focusInside = true;
			return;
		}
		focusInside = false;
		// Hosted in a dialog, the fallback is the dialog, never the page behind
		// it (the dialog node takes focus: it carries tabindex="-1").
		const fallback = region?.closest<HTMLElement>('[role="dialog"]') ?? document.getElementById('main-content');
		const target = returnTo?.isConnected ? returnTo : fallback;
		returnTo = null;
		target?.focus();
	}

	// Focus hand-off and the hover re-read happen in the list-change effect
	// above, which covers this and every other way a toast leaves.
</script>

<!--
	WCAG 4.1.3 Status Messages: toasts are announced without moving focus.
	Errors go in an assertive region (interrupt); everything else in a polite
	region. Two stacked containers keep the live-region semantics clean — a
	single region toggling aria-live is unreliable across screen readers.
	Pausing changes no DOM, so it announces nothing; a repeated error is a new
	insertion (its old copy is removed), so it is announced again.
	Each toast carries a real <button> dismiss (keyboard-operable), not a div.
	The region is named "Status messages", not "Notifications": that name
	belongs to the sidebar bell, and two landmarks sharing a name are
	indistinguishable in a screen reader's landmark list.
-->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
	bind:this={region}
	class="toast-region"
	data-dialog-satellite
	role="region"
	aria-label={m('toast.region')}
	class:capped={maxHeight !== null}
	style:bottom={placedBottom === null
		? 'calc(16px + var(--consent-banner-height, 0px))'
		: `${placedBottom}px`}
	style:max-height={maxHeight === null ? null : `${maxHeight}px`}
	onfocusin={onFocusIn}
	onfocusout={onFocusOut}
>
	<div class="toast-container" aria-live="assertive">
		{#each toasts.filter((t) => t.type === 'error') as t (`${t.id}:${rekey[t.id] ?? 0}`)}
			{@render toastItem(t)}
		{/each}
	</div>
	<div class="toast-container" aria-live="polite">
		{#each toasts.filter((t) => t.type !== 'error') as t (`${t.id}:${rekey[t.id] ?? 0}`)}
			{@render toastItem(t)}
		{/each}
	</div>
</div>

{#snippet toastItem(t: ToastMessage)}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="toast {t.type}"
		data-testid="toast"
		data-toast-type={t.type}
		data-toast-id={t.id}
		use:arrive={t.id}
		onmouseenter={() => (hovered = true)}
		onmouseleave={() => (hovered = false)}
	>
		<span class="toast-icon" aria-hidden="true">
			{#if t.type === 'success'}
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
			{:else if t.type === 'error'}
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
			{:else if t.type === 'warning'}
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
			{:else}
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
			{/if}
		</span>
		<span class="toast-text">{t.text}</span>
		<button
			type="button"
			class="toast-dismiss"
			aria-label={m('toast.dismiss')}
			onclick={() => dismiss(t.id)}
		>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
		</button>
	</div>
{/snippet}

<style>
	/* Both live containers stack in the same fixed corner; the region wrapper
	   just groups them for the landmark. */
	.toast-region {
		position: fixed;
		/* `bottom` is set inline: above the consent banner and any bulk bar
		   (`utils/toastPlacement.ts`). An open dialog makes room for it rather
		   than the other way round. */
		bottom: 16px;
		right: 16px;
		z-index: 9999;
		display: flex;
		flex-direction: column;
		gap: 8px;
		pointer-events: none;
	}

	/* Taller than its share of the viewport (`TOAST_MAX_SHARE`), with or
	   without a dialog open: the stack scrolls — by pointer (the wheel works over the toasts themselves)
	   and by keyboard (Tab reaches each dismiss button and the browser scrolls
	   it into view: the region is in the dialog's Tab cycle). */
	.toast-region.capped {
		overflow-y: auto;
		overscroll-behavior: contain;
	}

	.toast-container {
		display: flex;
		flex-direction: column-reverse;
		gap: 8px;
	}

	.toast {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 12px 18px;
		border-radius: 8px;
		font-size: 0.88rem;
		font-family: inherit;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
		pointer-events: auto;
		animation: slide-in 0.25s ease;
		/* Never wider than the viewport's gutters (reflow at 320px, 1.4.10). */
		max-width: min(420px, calc(100vw - 32px));
	}

	@keyframes slide-in {
		from {
			transform: translateY(20px);
			opacity: 0;
		}
		to {
			transform: translateY(0);
			opacity: 1;
		}
	}

	/* An OPAQUE tone-tinted card (the toast floats over arbitrary content, so
	   a translucent tint would take on whatever is under it), with the tone's
	   `-on-tint` text. Mixed from the palette rather than typed, so both themes
	   get a card in their own register — these were four dark literals
	   (#132a1e, …) and a toast was a navy slab on a light page. */
	.toast.success {
		background: color-mix(in srgb, var(--success) 14%, var(--surface));
		border: 1px solid var(--success);
		color: var(--success-on-tint);
	}

	.toast.error {
		background: color-mix(in srgb, var(--danger) 14%, var(--surface));
		border: 1px solid var(--danger);
		color: var(--danger-on-tint);
	}

	.toast.warning {
		background: color-mix(in srgb, var(--warning) 14%, var(--surface));
		border: 1px solid var(--warning);
		color: var(--warning-on-tint);
	}

	.toast.info {
		background: color-mix(in srgb, var(--accent) 14%, var(--surface));
		border: 1px solid var(--accent);
		color: var(--accent-on-tint);
	}

	.toast-icon {
		flex-shrink: 0;
		display: grid;
		place-items: center;
	}

	.toast-text {
		line-height: 1.4;
		flex: 1;
	}

	.toast-dismiss {
		flex-shrink: 0;
		display: grid;
		place-items: center;
		/* WCAG 2.5.8 target size: 24px minimum. */
		width: 24px;
		height: 24px;
		padding: 0;
		border: none;
		border-radius: 5px;
		background: transparent;
		color: inherit;
		opacity: 0.75;
		cursor: pointer;
		transition: opacity 0.12s, background 0.12s;
	}

	.toast-dismiss:hover,
	.toast-dismiss:focus-visible {
		opacity: 1;
		background: color-mix(in srgb, currentColor 12%, transparent);
	}

	/* WCAG 2.3.3 Animation from Interactions — kill the slide-in when the user
	   asks for reduced motion (also covered globally in app.css, belt + braces). */
	@media (prefers-reduced-motion: reduce) {
		.toast {
			animation: none;
		}
	}
</style>
