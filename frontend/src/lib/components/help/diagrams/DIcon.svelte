<svelte:options namespace="svg" />

<script lang="ts">
	/**
	 * One diagram line icon (./icons.ts), placed with its top-left corner at
	 * (x, y) and scaled to `size`. Strokes in `currentColor`, so the caller's
	 * tone class decides the colour.
	 */
	import { ICONS, type IconName } from './icons.ts';

	let { name, x, y, size = 20 }: { name: IconName; x: number; y: number; size?: number } = $props();
</script>

<path class="d-icon" d={ICONS[name]} stroke-width={(1.75 * 24) / size} transform="translate({x} {y}) scale({size / 24})" />

<style>
	.d-icon {
		fill: none;
		stroke: currentColor;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
</style>
