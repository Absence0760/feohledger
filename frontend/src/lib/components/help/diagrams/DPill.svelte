<svelte:options namespace="svg" />

<script lang="ts">
	/**
	 * A short label riding on a connector, centred on (x, y). The pill sizes to
	 * its text inside a `w`-wide slot, so a longer translation grows the pill
	 * rather than spilling out of it.
	 */
	import DIcon from './DIcon.svelte';
	import type { IconName } from './icons.ts';
	import type { Tone } from './tone.ts';

	let {
		x,
		y,
		text,
		tone = 'neutral',
		icon,
		w = 160
	}: { x: number; y: number; text: string; tone?: Tone; icon?: IconName; w?: number } = $props();
</script>

<foreignObject x={x - w / 2} y={y - 13} width={w} height="26">
	<div class="slot">
		<span class="pill tone-{tone}">
			{#if icon}<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"
					><DIcon name={icon} x={0} y={0} size={16} /></svg
				>{/if}{text}
		</span>
	</div>
</foreignObject>

<style>
	.slot {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 100%;
	}
	.pill {
		--tone: var(--text-muted);
		display: inline-flex;
		align-items: center;
		gap: 4px;
		max-width: 100%;
		padding: 2px 9px;
		border: 1px solid color-mix(in srgb, var(--tone) 60%, var(--surface));
		border-radius: 999px;
		background: var(--surface);
		color: var(--tone);
		font-size: 11px;
		font-weight: 600;
		line-height: 1.3;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.tone-neutral {
		color: var(--text-muted);
	}
	.tone-accent {
		--tone: var(--accent);
	}
	.tone-success {
		--tone: var(--success);
	}
	.tone-danger {
		--tone: var(--danger);
	}
	.tone-warning {
		--tone: var(--warning-on-tint);
	}
	svg {
		flex: none;
	}
</style>
