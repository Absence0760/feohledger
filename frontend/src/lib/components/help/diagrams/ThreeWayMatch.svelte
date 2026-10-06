<script lang="ts">
	/**
	 * PO matching: which documents each variant compares (two-way: PO and
	 * invoice; three-way adds the goods receipt; four-way the quality
	 * inspection), the comparison on quantity and price within the match
	 * tolerance, and its two outcomes — matched, or a mismatch that warns on
	 * the invoice and opens an exception (PO Mismatch, or Quality Hold for a
	 * failed inspection). Facts per services/po_matching.py and
	 * backend/docs/po-matching.md.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { IconName } from './icons.ts';
	import DCard from './DCard.svelte';
	import DArrow from './DArrow.svelte';
	import DPill from './DPill.svelte';
	import DIcon from './DIcon.svelte';

	const label = $derived(m('help.diagram.three-way-match.label'));
	const docs = $derived([
		{ icon: 'order', label: m('help.term.purchase-order'), sub: m('help.diagram.three-way-match.poSub') },
		{ icon: 'invoice', label: m('help.term.invoice'), sub: m('help.diagram.three-way-match.invoiceSub') },
		{ icon: 'box', label: m('help.term.goods-receipt'), sub: m('help.diagram.three-way-match.receiptSub') },
		{ icon: 'inspect', label: m('help.term.quality-inspection'), sub: m('help.diagram.three-way-match.inspectionSub') }
	] as const);
	const variants = $derived([
		{ label: m('help.term.two-way-match'), n: 2 },
		{ label: m('help.term.three-way-match'), n: 3 },
		{ label: m('help.term.four-way-match'), n: 4 }
	]);
	const exceptionSub = $derived(`${m('exceptions.type.poMismatch')} · ${m('exceptions.type.qualityHold')}`);
	const ICON_ORDER: IconName[] = ['order', 'invoice', 'box', 'inspect'];
</script>

<svg class="d-wide" viewBox="0 0 760 452" width="100%" role="img" aria-label={label}>
	<!-- What each variant compares: nested brackets over the documents. -->
	{#each variants as v, i (v.n)}
		{@const y = 18 + (2 - i) * 30}
		{@const x2 = v.n * 200 - 40}
		<DArrow points={[[0, y + 10], [0, y], [x2, y], [x2, y + 10]]} head={false} radius={4} dashed={v.n === 4} />
		<DPill x={x2 / 2} y={y} text={v.label} tone={v.n === 3 ? 'accent' : 'neutral'} />
	{/each}

	{#each docs as d, i (i)}
		<DCard
			x={i * 200}
			y={108}
			w={160}
			h={108}
			layout="col"
			icon={d.icon}
			label={d.label}
			sub={d.sub}
			tone={i === 1 ? 'accent' : 'neutral'}
			dashed={i === 3}
		/>
		<DArrow points={[[i * 200 + 80, 216], [i * 200 + 80, 240]]} head={false} dashed={i === 3} />
	{/each}
	<DArrow points={[[80, 240], [680, 240]]} head={false} radius={0} />
	<DArrow points={[[380, 240], [380, 268]]} />

	<DCard
		x={230}
		y={270}
		w={300}
		h={62}
		icon="scale"
		label={m('help.diagram.three-way-match.compare')}
		sub={m('help.diagram.three-way-match.compareSub')}
	/>
	<DArrow points={[[380, 332], [380, 350], [150, 350], [150, 372]]} tone="success" />
	<DArrow points={[[380, 350], [430, 350], [430, 372]]} tone="danger" />

	<DCard
		x={40}
		y={374}
		w={220}
		h={74}
		tone="success"
		icon="checkCircle"
		label={m('invoices.modal.poMatch.matched')}
		sub={m('help.diagram.three-way-match.matchedSub')}
	/>
	<DCard
		x={330}
		y={374}
		w={200}
		h={74}
		tone="danger"
		icon="crossCircle"
		label={m('invoices.modal.poMatch.mismatch')}
		sub={m('help.diagram.three-way-match.mismatchSub')}
	/>
	<DArrow points={[[530, 411], [568, 411]]} tone="danger" />
	<DCard x={570} y={374} w={190} h={74} tone="warning" icon="alert" label={m('help.term.exception')} sub={exceptionSub} />
</svg>

<svg class="d-tall" viewBox="0 0 320 660" width="100%" role="img" aria-label={label}>
	{#each docs as d, i (i)}
		<DCard
			x={0}
			y={i * 58}
			w={320}
			h={50}
			icon={d.icon}
			label={d.label}
			sub={d.sub}
			tone={i === 1 ? 'accent' : 'neutral'}
			dashed={i === 3}
		/>
	{/each}

	<!-- Which documents each variant compares, as rows of document icons. -->
	<rect class="panel" x="0" y="240" width="320" height="112" rx="12" />
	{#each variants as v, i (v.n)}
		{@const cy = 262 + i * 34}
		<foreignObject x="12" y={cy - 14} width="160" height="28">
			<div class="row-label">{v.label}</div>
		</foreignObject>
		{#each ICON_ORDER.slice(0, v.n) as icon, j (icon)}
			<g class="mini" class:accent={j === 1}><DIcon name={icon} x={186 + j * 32} y={cy - 10} size={20} /></g>
		{/each}
	{/each}
	<DArrow points={[[160, 352], [160, 384]]} />

	<DCard
		x={10}
		y={386}
		w={300}
		h={72}
		icon="scale"
		label={m('help.diagram.three-way-match.compare')}
		sub={m('help.diagram.three-way-match.compareSub')}
	/>
	<DArrow points={[[160, 458], [160, 472], [75, 472], [75, 492]]} tone="success" />
	<DArrow points={[[160, 472], [245, 472], [245, 492]]} tone="danger" />
	<DCard
		x={0}
		y={494}
		w={150}
		h={74}
		layout="col"
		tone="success"
		icon="checkCircle"
		label={m('invoices.modal.poMatch.matched')}
	/>
	<DCard
		x={170}
		y={494}
		w={150}
		h={74}
		layout="col"
		tone="danger"
		icon="crossCircle"
		label={m('invoices.modal.poMatch.mismatch')}
	/>
	<DArrow points={[[245, 568], [245, 590]]} tone="danger" />
	<DCard x={110} y={592} w={210} h={62} tone="warning" icon="alert" label={m('help.term.exception')} sub={exceptionSub} />
</svg>

<style>
	.panel {
		fill: var(--bg);
		stroke: color-mix(in srgb, var(--text-muted) 50%, var(--surface));
		stroke-width: 1.5;
	}
	.row-label {
		display: flex;
		align-items: center;
		height: 100%;
		color: var(--text);
		font-size: 12px;
		font-weight: 600;
		line-height: 1.2;
	}
	.mini {
		color: var(--text-muted);
	}
	.mini.accent {
		color: var(--accent);
	}
</style>
