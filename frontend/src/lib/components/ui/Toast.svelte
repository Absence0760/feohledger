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

	let region = $state<HTMLElement | null>(null);
	let hovered = $state(false);
	let focusInside = $state(false);

	$effect(() => {
		queue.setPaused(hovered || focusInside);
	});

	// The list emptied under the pointer or the focus: neither `mouseleave`
	// nor `focusout` is guaranteed to fire for an element that is removed.
	$effect(() => {
		if (toasts.length === 0) {
			hovered = false;
			focusInside = false;
		}
	});

	function onFocusOut(e: FocusEvent) {
		const next = e.relatedTarget as Node | null;
		focusInside = !!(next && region?.contains(next));
	}

	/**
	 * Dismissed from its own button: the focused button is about to vanish, so
	 * hand focus to the next toast's dismiss button, or — when this was the
	 * last one — to the page's `<main>` (the skip-link target), never `<body>`
	 * (WCAG 2.4.3).
	 */
	async function dismissFromButton(id: number) {
		dismiss(id);
		await tick();
		// The card under the pointer was just removed, so no `mouseleave` will
		// come for it: re-read whether the pointer is over a remaining toast.
		hovered = !!region?.querySelector('.toast:hover');
		const next = region?.querySelector<HTMLButtonElement>('.toast-dismiss');
		if (next) {
			next.focus();
		} else {
			focusInside = false;
			document.getElementById('main-content')?.focus();
		}
	}
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
	onfocusin={() => (focusInside = true)}
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
			onclick={() => dismissFromButton(t.id)}
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
		bottom: 16px;
		right: 16px;
		z-index: 9999;
		display: flex;
		flex-direction: column;
		gap: 8px;
		pointer-events: none;
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
		max-width: 420px;
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
