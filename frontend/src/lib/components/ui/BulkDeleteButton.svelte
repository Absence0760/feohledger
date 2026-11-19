<script lang="ts">
	let {
		onconfirm,
		disabled = false,
		label = 'Delete',
		confirmLabel,
		title = ''
	}: {
		onconfirm: () => void;
		disabled?: boolean;
		/** Visible label in the unarmed state — defaults to "Delete". */
		label?: string;
		/** Label in the armed state. Pass a localized string; the default
		 *  ("Confirm " + label) is English-only. */
		confirmLabel?: string;
		/** Optional native tooltip (e.g. "Cannot delete X — Y"). */
		title?: string;
	} = $props();

	let armed = $state(false);

	function handleClick(e: MouseEvent) {
		e.stopPropagation();
		if (armed) {
			onconfirm();
			armed = false;
		} else {
			armed = true;
		}
	}

	function handleWindowClick(e: MouseEvent) {
		if (armed && !(e.target as HTMLElement).closest('.bulk-delete-btn')) {
			armed = false;
		}
	}
</script>

<svelte:window onclick={handleWindowClick} />

<!-- `.bulk-delete-btn` is the e2e / outside-click hook; the shared
     `.btn-danger` (+ `.armed`) paints it, the same box as every other bar
     button. -->
<button
	type="button"
	class="btn-danger bulk-delete-btn"
	class:armed
	{disabled}
	{title}
	onclick={handleClick}
>
	{#if armed}
		<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
			<polyline points="20 6 9 17 4 12"/>
		</svg>
		{confirmLabel ?? `Confirm ${label}`}
	{:else}
		<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
			<polyline points="3 6 5 6 21 6"/>
			<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
		</svg>
		{label}
	{/if}
</button>

