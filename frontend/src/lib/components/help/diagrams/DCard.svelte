<svelte:options namespace="svg" />

<script lang="ts">
	/**
	 * A diagram box: a rounded rectangle in a tone, an optional icon in a
	 * tinted disc, and catalogue text (already translated by the caller).
	 *
	 * The text sits in a <foreignObject> so it WRAPS: SVG <text> cannot, and a
	 * German or Japanese label is a different length from the English one. It
	 * still scales with the viewBox like every other shape, and the enclosing
	 * <svg role="img"> keeps it out of the accessibility tree (its aria-label
	 * says what the picture shows).
	 *
	 * `layout="row"` puts the icon at the left; `"col"` stacks icon over centred
	 * text. `items` renders a short bullet list under the label. `badge` puts a
	 * small status icon (a lock, a tick) in the top-right corner.
	 */
	import DIcon from './DIcon.svelte';
	import type { IconName } from './icons.ts';
	import type { Tone } from './tone.ts';

	let {
		x,
		y,
		w,
		h,
		tone = 'neutral',
		icon,
		label,
		sub,
		items,
		layout = 'row',
		dashed = false,
		badge,
		badgeTone = tone
	}: {
		x: number;
		y: number;
		w: number;
		h: number;
		tone?: Tone;
		icon?: IconName;
		label: string;
		sub?: string;
		items?: string[];
		layout?: 'row' | 'col';
		dashed?: boolean;
		badge?: IconName;
		badgeTone?: Tone;
	} = $props();

	const PAD = 12;
	const DISC = 16;
	const col = $derived(layout === 'col');
	const disc = $derived(
		icon ? (col ? { cx: x + w / 2, cy: y + PAD + DISC } : { cx: x + PAD + DISC, cy: y + h / 2 }) : undefined
	);
	const text = $derived.by(() => {
		if (!icon) return { x: x + PAD, y: y + 4, w: w - PAD * 2 - (badge ? 18 : 0), h: h - 8 };
		if (col) {
			const top = PAD + DISC * 2 + 6;
			return { x: x + 8, y: y + top, w: w - 16, h: h - top - 6 };
		}
		const left = PAD + DISC * 2 + 10;
		return { x: x + left, y: y + 4, w: w - left - PAD + (badge ? -10 : 0), h: h - 8 };
	});
</script>

<g class="d-card tone-{tone}">
	<rect class="box" class:dashed {x} {y} width={w} height={h} rx="12" />
	{#if disc && icon}
		<circle class="disc" cx={disc.cx} cy={disc.cy} r={DISC} />
		<g class="glyph"><DIcon name={icon} x={disc.cx - 10} y={disc.cy - 10} size={20} /></g>
	{/if}
	{#if badge}
		<g class="badge tone-{badgeTone}"><DIcon name={badge} x={x + w - 24} y={y + 7} size={16} /></g>
	{/if}
	<foreignObject x={text.x} y={text.y} width={Math.max(text.w, 1)} height={Math.max(text.h, 1)}>
		<div class="fo" class:col>
			<span class="label">{label}</span>
			{#if sub}<span class="sub">{sub}</span>{/if}
			{#if items?.length}
				<ul>
					{#each items as item, i (i)}<li>{item}</li>{/each}
				</ul>
			{/if}
		</div>
	</foreignObject>
</g>

<style>
	.d-card {
		--tone: var(--text-muted);
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
	.box {
		fill: color-mix(in srgb, var(--tone) 12%, var(--bg));
		stroke: var(--tone);
		stroke-width: 1.5;
	}
	.tone-neutral .box {
		fill: var(--bg);
		stroke: color-mix(in srgb, var(--text-muted) 50%, var(--surface));
	}
	.dashed {
		stroke-dasharray: 5 4;
	}
	.disc {
		fill: color-mix(in srgb, var(--tone) 22%, var(--bg));
	}
	.glyph {
		color: var(--tone);
	}
	.tone-neutral .glyph {
		color: var(--text);
	}
	.badge {
		color: var(--tone);
	}
	.fo {
		display: flex;
		flex-direction: column;
		justify-content: center;
		gap: 2px;
		height: 100%;
		color: var(--text);
		font-size: 13px;
		line-height: 1.25;
		overflow-wrap: anywhere;
	}
	/* German compounds outgrow a narrow box; elsewhere a hyphen mid-word only hurts. */
	.fo:lang(de) {
		hyphens: auto;
	}
	.fo.col {
		justify-content: flex-start;
		align-items: center;
		text-align: center;
	}
	.label {
		font-weight: 650;
	}
	.sub,
	ul {
		color: var(--text-muted);
		font-size: 11.5px;
		line-height: 1.3;
	}
	ul {
		margin: 4px 0 0;
		padding-left: 14px;
		text-align: left;
	}
	li + li {
		margin-top: 2px;
	}
</style>
