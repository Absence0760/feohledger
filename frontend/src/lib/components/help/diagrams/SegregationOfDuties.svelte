<script lang="ts">
	/**
	 * Segregation of duties on an invoice: whoever brought it in or shaped it
	 * (the implicated set — services/approval_chain.violates_segregation) is
	 * refused at approval; a colleague outside that set approves.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import DCard from './DCard.svelte';
	import DArrow from './DArrow.svelte';
	import DPill from './DPill.svelte';
	import DStop from './DStop.svelte';

	const label = $derived(m('help.diagram.segregation-of-duties.label'));
	const maker = $derived(m('help.diagram.segregation-of-duties.maker'));
	const makerSub = $derived(m('help.diagram.segregation-of-duties.makerSub'));
	const checker = $derived(m('help.diagram.segregation-of-duties.checker'));
	const checkerSub = $derived(m('help.diagram.segregation-of-duties.checkerSub'));
	const refused = $derived(m('help.diagram.segregation-of-duties.refused'));
	const approves = $derived(m('help.diagram.segregation-of-duties.approves'));
</script>

<svg class="d-wide" viewBox="0 0 760 270" width="100%" role="img" aria-label={label}>
	<DCard x={0} y={44} w={200} h={80} icon="person" label={maker} sub={makerSub} />
	<DArrow points={[[200, 84], [258, 84]]} />
	<DCard
		x={260}
		y={44}
		w={190}
		h={80}
		tone="accent"
		icon="invoice"
		label={m('help.term.invoice')}
		sub={m('invoices.status.readyForReview')}
	/>
	<DArrow points={[[450, 84], [568, 84]]} />
	<DCard x={570} y={44} w={190} h={80} tone="success" icon="checkCircle" label={m('invoices.status.approved')} />

	<DArrow points={[[100, 44], [100, 16], [509, 16], [509, 58]]} tone="danger" dashed head={false} />
	<DStop x={509} y={66} />
	<DPill x={300} y={16} w={200} tone="danger" icon="cross" text={refused} />

	<DCard x={0} y={180} w={200} h={80} icon="person" label={checker} sub={checkerSub} />
	<DArrow points={[[200, 220], [665, 220], [665, 126]]} tone="success" />
	<DPill x={420} y={220} w={200} tone="success" icon="check" text={approves} />
</svg>

<svg class="d-tall" viewBox="0 0 320 356" width="100%" role="img" aria-label={label}>
	<DCard x={0} y={0} w={140} h={116} layout="col" icon="person" label={maker} sub={makerSub} />
	<DArrow points={[[140, 58], [178, 58]]} />
	<DCard
		x={180}
		y={0}
		w={140}
		h={116}
		layout="col"
		tone="accent"
		icon="invoice"
		label={m('help.term.invoice')}
		sub={m('invoices.status.readyForReview')}
	/>
	<DArrow points={[[250, 116], [250, 238]]} />

	<DArrow points={[[70, 116], [70, 178], [222, 178]]} tone="danger" dashed head={false} />
	<DStop x={234} y={178} />
	<DPill x={124} y={178} w={170} tone="danger" text={refused} />

	<DCard x={0} y={240} w={140} h={116} layout="col" icon="person" label={checker} sub={checkerSub} />
	<DArrow points={[[140, 298], [178, 298]]} tone="success" />
	<DCard
		x={180}
		y={240}
		w={140}
		h={116}
		layout="col"
		tone="success"
		icon="checkCircle"
		label={m('invoices.status.approved')}
	/>
</svg>
