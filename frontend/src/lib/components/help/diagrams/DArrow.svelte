<svelte:options namespace="svg" />

<script lang="ts">
	/**
	 * A connector through `points`, with its corners rounded and an arrowhead
	 * on the last point (`head = false` for a plain line). The head is drawn,
	 * not a <marker>, so two copies of a diagram on one page never share an id.
	 */
	import type { Tone } from './tone.ts';

	let {
		points,
		tone = 'line',
		dashed = false,
		head = true,
		radius = 10
	}: { points: [number, number][]; tone?: Tone | 'line'; dashed?: boolean; head?: boolean; radius?: number } =
		$props();

	const HEAD = 8;

	const geometry = $derived.by(() => {
		const pts = points.map(([x, y]) => ({ x, y }));
		const n = pts.length;
		const end = pts[n - 1];
		const prev = pts[n - 2];
		const len = Math.hypot(end.x - prev.x, end.y - prev.y) || 1;
		const ux = (end.x - prev.x) / len;
		const uy = (end.y - prev.y) / len;
		// Stop the line at the head's base, so the stroke never pokes past the tip.
		const stop = head ? { x: end.x - ux * (HEAD - 1), y: end.y - uy * (HEAD - 1) } : end;
		let d = `M${pts[0].x} ${pts[0].y}`;
		for (let i = 1; i < n - 1; i++) {
			const a = pts[i - 1];
			const b = pts[i];
			const c = pts[i + 1];
			const l1 = Math.hypot(b.x - a.x, b.y - a.y);
			const l2 = Math.hypot(c.x - b.x, c.y - b.y);
			const r = Math.min(radius, l1 / 2, l2 / 2);
			const p1 = { x: b.x - ((b.x - a.x) / l1) * r, y: b.y - ((b.y - a.y) / l1) * r };
			const p2 = { x: b.x + ((c.x - b.x) / l2) * r, y: b.y + ((c.y - b.y) / l2) * r };
			d += ` L${p1.x} ${p1.y} Q${b.x} ${b.y} ${p2.x} ${p2.y}`;
		}
		d += ` L${stop.x} ${stop.y}`;
		const bx = end.x - ux * HEAD;
		const by = end.y - uy * HEAD;
		const w = HEAD * 0.6;
		const tip = `M${end.x} ${end.y} L${bx - uy * w} ${by + ux * w} L${bx + uy * w} ${by - ux * w} Z`;
		return { d, tip };
	});
</script>

<g class="d-arrow tone-{tone}">
	<path class="line" class:dashed d={geometry.d} />
	{#if head}<path class="head" d={geometry.tip} />{/if}
</g>

<style>
	.d-arrow {
		color: var(--text-muted);
	}
	.tone-accent {
		color: var(--accent);
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
	.line {
		fill: none;
		stroke: currentColor;
		stroke-width: 1.75;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.dashed {
		stroke-dasharray: 5 5;
	}
	.head {
		fill: currentColor;
		stroke: currentColor;
		stroke-width: 1;
		stroke-linejoin: round;
	}
</style>
