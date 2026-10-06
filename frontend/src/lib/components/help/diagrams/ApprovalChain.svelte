<script lang="ts">
	/**
	 * An example approval chain along the amount axis: below the auto-approve
	 * amount an invoice skips approval; each chain level applies to its own
	 * amount band, so a larger invoice collects more levels, in order; above
	 * the CFO approval amount whoever approves must hold the CFO role
	 * (services/review.py — the gate restricts who may approve, it is not an
	 * extra level); above the maximum amount it is refused. Every threshold is
	 * in the reporting currency (services/approval_chain.py::GateAmount).
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import DCard from './DCard.svelte';
	import DArrow from './DArrow.svelte';
	import DPill from './DPill.svelte';

	const label = $derived(m('help.diagram.approval-chain.label'));
	const level1 = $derived(m('help.diagram.approval-chain.level', { n: 1 }));
	const level2 = $derived(m('help.diagram.approval-chain.level', { n: 2 }));
	const ticks = $derived([
		{ x: 120, text: m('help.diagram.approval-chain.autoAmount') },
		{ x: 260, text: m('help.diagram.approval-chain.level2From') },
		{ x: 420, text: m('help.diagram.approval-chain.cfoAmount') },
		{ x: 620, text: m('help.diagram.approval-chain.maxAmount') }
	]);
</script>

<svg class="d-wide" viewBox="0 0 760 300" width="100%" role="img" aria-label={label}>
	{#each ticks as t (t.x)}
		<line class="threshold" x1={t.x} y1="12" x2={t.x} y2="236" />
		<foreignObject x={t.x - 68} y="248" width="136" height="48">
			<div class="tick">{t.text}</div>
		</foreignObject>
	{/each}
	<DArrow points={[[0, 236], [756, 236]]} />
	<DPill x={60} y={236} w={110} text={m('help.diagram.approval-chain.amount')} />

	<DCard
		x={8}
		y={78}
		w={104}
		h={96}
		layout="col"
		tone="success"
		icon="check"
		label={m('help.diagram.approval-chain.autoApproved')}
	/>

	<DCard x={130} y={106} w={120} h={42} icon="person" label={level1} />

	<DCard x={270} y={72} w={140} h={42} icon="person" label={level1} />
	<DArrow points={[[340, 114], [340, 138]]} />
	<DCard x={270} y={140} w={140} h={42} icon="person" label={level2} />

	<rect class="cfo-frame" x="432" y="28" width="176" height="178" rx="14" />
	<DCard x={446} y={44} w={148} h={42} icon="person" label={level1} />
	<DArrow points={[[520, 86], [520, 110]]} />
	<DCard x={446} y={112} w={148} h={42} icon="person" label={level2} />
	<DPill x={520} y={180} w={210} icon="shield" tone="warning" text={m('help.diagram.approval-chain.cfoOnly')} />

	<DCard
		x={634}
		y={78}
		w={112}
		h={96}
		layout="col"
		tone="danger"
		icon="ban"
		label={m('help.diagram.approval-chain.refused')}
	/>
</svg>

<svg class="d-tall" viewBox="0 0 320 450" width="100%" role="img" aria-label={label}>
	<DArrow points={[[14, 8], [14, 160]]} head={false} />
	<DArrow points={[[14, 290], [14, 440]]} />
	<text class="axis-label" transform="translate(14 225) rotate(-90)">{m('help.diagram.approval-chain.amount')}</text>

	<DCard
		x={40}
		y={8}
		w={280}
		h={50}
		tone="success"
		icon="check"
		label={m('help.diagram.approval-chain.autoApproved')}
	/>
	<line class="threshold" x1="40" y1="76" x2="320" y2="76" />
	<DPill x={180} y={76} w={270} text={m('help.diagram.approval-chain.autoAmount')} />

	<DCard x={40} y={94} w={130} h={42} icon="person" label={level1} />
	<line class="threshold" x1="40" y1="154" x2="320" y2="154" />
	<DPill x={180} y={154} w={270} text={m('help.diagram.approval-chain.level2From')} />

	<DCard x={40} y={172} w={124} h={42} icon="person" label={level1} />
	<DArrow points={[[164, 193], [194, 193]]} />
	<DCard x={196} y={172} w={124} h={42} icon="person" label={level2} />
	<line class="threshold" x1="40" y1="232" x2="320" y2="232" />
	<DPill x={180} y={232} w={270} text={m('help.diagram.approval-chain.cfoAmount')} />

	<rect class="cfo-frame" x="40" y="248" width="280" height="96" rx="14" />
	<DCard x={50} y={258} w={118} h={42} icon="person" label={level1} />
	<DArrow points={[[168, 279], [190, 279]]} />
	<DCard x={192} y={258} w={118} h={42} icon="person" label={level2} />
	<DPill x={180} y={322} w={260} icon="shield" tone="warning" text={m('help.diagram.approval-chain.cfoOnly')} />
	<line class="threshold" x1="40" y1="362" x2="320" y2="362" />
	<DPill x={180} y={362} w={270} text={m('help.diagram.approval-chain.maxAmount')} />

	<DCard x={40} y={380} w={280} h={50} tone="danger" icon="ban" label={m('help.diagram.approval-chain.refused')} />
</svg>

<style>
	.threshold {
		stroke: color-mix(in srgb, var(--text-muted) 70%, var(--surface));
		stroke-width: 1.25;
		stroke-dasharray: 3 4;
	}
	.cfo-frame {
		fill: color-mix(in srgb, var(--warning-on-tint) 7%, transparent);
		stroke: var(--warning-on-tint);
		stroke-width: 1.5;
		stroke-dasharray: 6 4;
	}
	.axis-label {
		fill: var(--text-muted);
		font-size: 11px;
		font-weight: 600;
		text-anchor: middle;
		dominant-baseline: middle;
	}
	.tick {
		color: var(--text-muted);
		font-size: 11px;
		line-height: 1.25;
		text-align: center;
	}
</style>
