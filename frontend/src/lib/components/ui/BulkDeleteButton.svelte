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

<button
	type="button"
	class="bulk-delete-btn"
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

<style>
	.bulk-delete-btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 5px 12px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
		transition: border-color 0.15s;
	}

	.bulk-delete-btn:hover {
		border-color: var(--danger);
		color: var(--danger);
	}

	.bulk-delete-btn.armed {
		border-color: var(--danger);
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		color: var(--danger);
	}

	.bulk-delete-btn:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
</style>
