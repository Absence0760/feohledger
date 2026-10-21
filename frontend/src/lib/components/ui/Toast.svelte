<script lang="ts" module>
	import {
		createToastQueue,
		type ToastMessage,
		type ToastType
	} from '#lib/utils/toastQueue.ts';

	export type { ToastMessage, ToastType };

	let toasts = $state<ToastMessage[]>([]);

	// Timing lives in `utils/toastQueue.ts` (decisions §307): an error stays
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
	import { m } from '#lib/i18n/store.svelte.ts';
	import { placeToasts, TOAST_MARGIN, type Box } from '#lib/utils/toastPlacement.ts';

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
	 * Keep persistent errors clear of what the reader needs next (WCAG 2.4.11).
	 * The region sits bottom-right, which is where the shared bulk-action bar
	 * sits too (centred, `bottom: 24px`) — while one is on screen the region
	 * rises above it — and, on a phone, where an open dialog's footer sits.
	 * `utils/toastPlacement.ts` decides where the stack goes around that footer
	 * (#635 G); this measures the page for it. Observed only while toasts are
	 * showing.
	 */
	$effect(() => {
		if (toasts.length === 0) {
			placedBottom = null;
			maxHeight = null;
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
			const dialog = openDialog();
			// A bulk bar behind an open dialog's backdrop is not what the
			// reader is looking at; only the dialog's actions are.
			const bar = dialog ? null : document.querySelector('.bulk-bar');
			watch([bar, dialog, region]);
			const vh = window.innerHeight;
			const consent =
				parseFloat(
					getComputedStyle(document.documentElement).getPropertyValue('--consent-banner-height')
				) || 0;
			const barLift = bar ? Math.max(0, vh - bar.getBoundingClientRect().top) + 8 : 0;
			const placed = placeToasts({
				viewport: { width: window.innerWidth, height: vh },
				stack: { width: region.offsetWidth, height: region.scrollHeight },
				bottomInset: Math.max(TOAST_MARGIN + consent, barLift),
				avoid: dialog ? dialogActions(dialog) : null
			});
			placedBottom = placed.bottom;
			maxHeight = placed.maxHeight;
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
		// A long dialog scrolls inside itself, carrying its footer with it.
		document.addEventListener('scroll', schedule, { capture: true, passive: true });
		measure();
		return () => {
			cancelAnimationFrame(frame);
			mutations.disconnect();
			rootStyle.disconnect();
			resize.disconnect();
			window.removeEventListener('resize', schedule);
			document.removeEventListener('scroll', schedule, true);
		};
	});

	/**
	 * The open modal dialog, topmost last: `ui/Modal` and the feature dialogs
	 * that hand-roll its shell (`InvoiceModal`, `RunDetailModal`, …) all render
	 * `role="dialog"` inside a `.backdrop`; the portal's dialog sets
	 * `aria-modal`. A popover (the bell's `role="dialog"`) is neither.
	 */
	function openDialog(): HTMLElement | null {
		const all = document.querySelectorAll<HTMLElement>(
			'.backdrop [role="dialog"], [role="dialog"][aria-modal="true"]'
		);
		return all.length ? all[all.length - 1] : null;
	}

	/**
	 * The dialog's action row as it shows on screen: its last visible
	 * `.modal-footer` (the shared footer class), else the row holding its last
	 * visible button. Clipped to the dialog's own box, since a long dialog
	 * scrolls and a footer scrolled out of it is not on screen to cover; null
	 * when none of it is.
	 */
	function dialogActions(dialog: HTMLElement): Box | null {
		const visible = (el: Element) => {
			const r = el.getBoundingClientRect();
			return r.width > 0 && r.height > 0;
		};
		const footers = [...dialog.querySelectorAll('.modal-footer')].filter(visible);
		let row: Element | null = footers.at(-1) ?? null;
		if (!row) row = [...dialog.querySelectorAll('button')].filter(visible).at(-1)?.parentElement ?? null;
		if (!row) return null;
		const r = row.getBoundingClientRect();
		const d = dialog.getBoundingClientRect();
		const box = {
			top: Math.max(r.top, d.top, 0),
			bottom: Math.min(r.bottom, d.bottom, window.innerHeight),
			left: Math.max(r.left, d.left),
			right: Math.min(r.right, d.right)
		};
		return box.top < box.bottom && box.left < box.right ? box : null;
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
		const target = returnTo?.isConnected ? returnTo : document.getElementById('main-content');
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
		{#each toasts.filter((t) => t.type === 'error') as t (t.id)}
			{@render toastItem(t)}
		{/each}
	</div>
	<div class="toast-container" aria-live="polite">
		{#each toasts.filter((t) => t.type !== 'error') as t (t.id)}
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
		/* `bottom` is set inline: above the consent banner, any bulk bar, and
		   an open dialog's actions (`utils/toastPlacement.ts`). */
		bottom: 16px;
		right: 16px;
		z-index: 9999;
		display: flex;
		flex-direction: column;
		gap: 8px;
		pointer-events: none;
	}

	/* Too tall to fit beside a dialog's actions: the stack scrolls instead of
	   covering them, by pointer (the wheel works over the toasts themselves).
	   The dialog's focus trap keeps Tab inside the dialog, so a keyboard user
	   reaches these toasts once the dialog closes. */
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
