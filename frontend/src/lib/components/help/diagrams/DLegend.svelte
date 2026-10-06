<svelte:options namespace="svg" />

<script lang="ts">
	/** One legend row: an icon in a tone, then its meaning, from (x, y) across `w`. */
	import DIcon from './DIcon.svelte';
	import type { IconName } from './icons.ts';
	import type { Tone } from './tone.ts';

	let {
		x,
		y,
		w,
		icon,
		tone = 'neutral',
		text
	}: { x: number; y: number; w: number; icon: IconName; tone?: Tone; text: string } = $props();
</script>

<g class="tone-{tone}"><DIcon name={icon} {x} y={y - 8} size={16} /></g>
<foreignObject x={x + 22} y={y - 16} width={Math.max(w - 22, 1)} height="32">
	<div class="text">{text}</div>
</foreignObject>

<style>
	g {
		color: var(--text-muted);
	}
	.tone-success {
		color: var(--success);
	}
	.tone-danger {
		color: var(--danger);
	}
	.tone-warning {
		color: var(--warning-on-tint);
	}
	.tone-accent {
		color: var(--accent);
	}
	.text {
		display: flex;
		align-items: center;
		height: 100%;
		color: var(--text-muted);
		font-size: 11.5px;
		line-height: 1.2;
	}
</style>
