<script lang="ts">
	type Chip = {
		key: string;
		label: string;
		/** Optional count badge rendered in `.count`. Omit for label-only chips. */
		count?: number | string;
		/** Renders the count as a red attention badge (`.count.alert`). */
		alert?: boolean;
	};

	type Props = {
		chips: Chip[];
		/** The active chip key. Two-way bindable. */
		active: string;
		/** Fired in addition to updating `active`. */
		onchange?: (key: string) => void;
		/**
		 * Accessible name for the chip `<nav>`. Pass one wherever the page has
		 * more than one navigation landmark, so a screen-reader user can tell
		 * this row of filters from the site nav.
		 */
		ariaLabel?: string;
	};

	let { chips, active = $bindable(), onchange, ariaLabel }: Props = $props();

	function select(key: string) {
		active = key;
		onchange?.(key);
	}
</script>

<nav class="filters" aria-label={ariaLabel}>
	{#each chips as chip (chip.key)}
		<button
			class="filter-chip"
			class:active={active === chip.key}
			type="button"
			aria-pressed={active === chip.key}
			onclick={() => select(chip.key)}
		>
			{chip.label}{#if chip.count !== undefined}{' '}<span class="count" class:alert={chip.alert}>{chip.count}</span>{/if}
		</button>
	{/each}
</nav>
