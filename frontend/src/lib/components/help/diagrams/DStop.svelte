<svelte:options namespace="svg" />

<script lang="ts">
	/** The end of a refused path: a cross in a danger disc, centred on (x, y). */
	import DIcon from './DIcon.svelte';

	let { x, y, r = 12 }: { x: number; y: number; r?: number } = $props();
</script>

<g class="d-stop">
	<circle cx={x} cy={y} {r} />
	<DIcon name="cross" x={x - r * 0.62} y={y - r * 0.62} size={r * 1.24} />
</g>

<style>
	.d-stop {
		color: var(--danger);
	}
	circle {
		fill: color-mix(in srgb, var(--danger) 18%, var(--bg));
		stroke: var(--danger);
		stroke-width: 1.5;
	}
</style>
