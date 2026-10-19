<script lang="ts">
	let {
		value = $bindable(''),
		placeholder = 'Search...',
		ariaLabel = 'Search',
		testid
	}: {
		value?: string;
		placeholder?: string;
		ariaLabel?: string;
		/** `data-testid` for the input, for a page with more than one search. */
		testid?: string;
	} = $props();
</script>

<div class="search-box">
	<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
		<circle cx="11" cy="11" r="8" />
		<path d="m21 21-4.35-4.35" />
	</svg>
	<input type="text" {placeholder} aria-label={ariaLabel} bind:value data-testid={testid} />
</div>

<style>
	.search-box {
		display: flex;
		align-items: center;
		gap: 8px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 20px;
		padding: 8px 14px;
		max-width: 320px;
		color: var(--text-muted);
	}

	/* WCAG 2.4.7: the input drops its own outline (it sits borderless inside
	   the pill), so the pill carries the focus ring — the same 2px accent ring
	   app.css gives every other control. Without this a keyboard user tabbing
	   off the filter chips landed in the search box with nothing on screen
	   saying so. */
	.search-box:focus-within {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.search-box input {
		border: none;
		background: none;
		outline: none;
		font-size: 0.9rem;
		width: 100%;
		color: var(--text);
		font-family: inherit;
	}

	.search-box input::placeholder {
		color: var(--text-muted);
	}
</style>
