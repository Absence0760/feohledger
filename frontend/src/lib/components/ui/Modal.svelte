<script lang="ts">
	import type { Snippet } from 'svelte';
	import { focusTrap } from '#lib/actions/focusTrap.ts';

	type Props = {
		/** Controls visibility. Defaults to true so `{#if open}<Modal>` reads naturally. */
		open?: boolean;
		/** Maps to the dialog's `aria-label` — the e2e suite selects modals by this. */
		ariaLabel: string;
		/** Convenience title rendered as `<h2>`. Omit and use the `header` snippet for
		 *  custom heading markup (e.g. the invoice modal's `.line-items-title`). */
		title?: string;
		/** Width preset: sm = 440px, md = 480px (default), lg = 820px. */
		width?: 'sm' | 'md' | 'lg';
		onclose: () => void;
		/** Replaces the default `<h2>{title}</h2>` heading. */
		header?: Snippet;
		/** Body content. Pages keep their own `<form>` + `.modal-footer` here so
		 *  form-submit semantics stay intact. */
		children: Snippet;
	};

	let { open = true, ariaLabel, title, width = 'md', onclose, header, children }: Props = $props();

	// Freeze the background page from scrolling while the dialog is open, so a
	// wheel event over the backdrop (or a non-scrolling area of the dialog) can't
	// bleed through and scroll the list behind it. Restored when closed/unmounted.
	$effect(() => {
		if (!open) return;
		const prev = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		return () => {
			document.body.style.overflow = prev;
		};
	});

	function onBackdrop(e: MouseEvent) {
		if (e.target === e.currentTarget) onclose();
	}
</script>

{#if open}
	<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
	<div class="backdrop" onclick={onBackdrop}>
		<!-- focusTrap: focus into the dialog on open, Tab/Shift+Tab wrap (WCAG
		     2.1.2), Esc to close, focus restored to the trigger on close (2.4.3). -->
		<div
			use:focusTrap={{ onEscape: onclose }}
			class="modal"
			class:modal-sm={width === 'sm'}
			class:modal-lg={width === 'lg'}
			role="dialog"
			aria-modal="true"
			aria-label={ariaLabel}
			tabindex="-1"
		>
			{#if header}
				{@render header()}
			{:else if title}
				<h2>{title}</h2>
			{/if}
			{@render children()}
		</div>
	</div>
{/if}
