<script lang="ts">
	/**
	 * Dual control on vendor bank details: a change is staged as a request
	 * (the current details stay in force), a second person approves or rejects
	 * it, and the requester can never approve their own. On approval the new
	 * details apply, the vendor is re-screened, and every unpaid invoice for
	 * that vendor gets a payment-blocking Fraud Flag (vendors guide § What
	 * happens after approval; services/vendor_change_requests).
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import DCard from './DCard.svelte';
	import DArrow from './DArrow.svelte';
	import DPill from './DPill.svelte';
	import DStop from './DStop.svelte';

	const label = $derived(m('help.diagram.bank-change-dual-control.label'));
	const after = $derived([
		m('help.diagram.bank-change-dual-control.rescreened'),
		m('help.diagram.bank-change-dual-control.flagged', { flag: m('exceptions.type.fraudFlag') })
	]);
</script>

<svg class="d-wide" viewBox="0 0 760 350" width="100%" role="img" aria-label={label}>
	<DCard
		x={0}
		y={50}
		w={160}
		h={124}
		layout="col"
		icon="person"
		label={m('help.diagram.bank-change-dual-control.requester')}
		sub={m('help.diagram.bank-change-dual-control.requesterSub')}
	/>
	<DArrow points={[[160, 112], [198, 112]]} />
	<DCard
		x={200}
		y={50}
		w={160}
		h={124}
		layout="col"
		tone="accent"
		icon="swap"
		label={m('help.term.bank-change-request')}
		sub={m('help.diagram.bank-change-dual-control.stagedSub')}
	/>
	<DArrow points={[[360, 112], [398, 112]]} />
	<DCard
		x={400}
		y={50}
		w={160}
		h={124}
		layout="col"
		icon="shieldCheck"
		label={m('help.diagram.bank-change-dual-control.approver')}
		sub={m('help.diagram.bank-change-dual-control.approverSub')}
	/>
	<DArrow points={[[560, 112], [598, 112]]} tone="success" />
	<DCard
		x={600}
		y={50}
		w={160}
		h={124}
		layout="col"
		tone="success"
		icon="bank"
		label={m('help.diagram.bank-change-dual-control.applied')}
	/>

	<DArrow points={[[80, 50], [80, 20], [480, 20], [480, 26]]} tone="danger" dashed head={false} />
	<DStop x={480} y={36} r={10} />
	<DPill x={280} y={20} w={230} tone="danger" icon="cross" text={m('help.diagram.bank-change-dual-control.notOwn')} />

	<DArrow points={[[480, 174], [480, 208]]} dashed />
	<DCard
		x={400}
		y={210}
		w={160}
		h={64}
		icon="crossCircle"
		label={m('vendors.changeRequests.status.rejected')}
		sub={m('help.diagram.bank-change-dual-control.rejectedSub')}
	/>
	<DArrow points={[[660, 174], [660, 208]]} tone="success" />
	<DCard
		x={560}
		y={210}
		w={200}
		h={124}
		tone="warning"
		badge="shield"
		label={m('help.diagram.bank-change-dual-control.then')}
		items={after}
	/>
</svg>

<svg class="d-tall" viewBox="0 0 320 544" width="100%" role="img" aria-label={label}>
	<DCard
		x={0}
		y={0}
		w={280}
		h={60}
		icon="person"
		label={m('help.diagram.bank-change-dual-control.requester')}
		sub={m('help.diagram.bank-change-dual-control.requesterSub')}
	/>
	<DArrow points={[[110, 60], [110, 94]]} />
	<DCard
		x={0}
		y={96}
		w={280}
		h={60}
		tone="accent"
		icon="swap"
		label={m('help.term.bank-change-request')}
		sub={m('help.diagram.bank-change-dual-control.stagedSub')}
	/>
	<DArrow points={[[110, 156], [110, 190]]} />
	<DCard
		x={0}
		y={192}
		w={280}
		h={60}
		icon="shieldCheck"
		label={m('help.diagram.bank-change-dual-control.approver')}
		sub={m('help.diagram.bank-change-dual-control.approverSub')}
	/>
	<DArrow points={[[280, 30], [304, 30], [304, 222], [294, 222]]} tone="danger" dashed head={false} />
	<DStop x={292} y={222} r={10} />
	<DPill x={205} y={77} w={200} tone="danger" text={m('help.diagram.bank-change-dual-control.notOwn')} />

	<DArrow points={[[110, 252], [110, 268], [65, 268], [65, 290]]} dashed />
	<DArrow points={[[110, 268], [225, 268], [225, 290]]} tone="success" />
	<DCard
		x={0}
		y={292}
		w={130}
		h={108}
		layout="col"
		icon="crossCircle"
		label={m('vendors.changeRequests.status.rejected')}
		sub={m('help.diagram.bank-change-dual-control.rejectedSub')}
	/>
	<DCard
		x={150}
		y={292}
		w={150}
		h={108}
		layout="col"
		tone="success"
		icon="bank"
		label={m('help.diagram.bank-change-dual-control.applied')}
	/>
	<DArrow points={[[225, 400], [225, 428]]} tone="success" />
	<DCard
		x={0}
		y={430}
		w={300}
		h={110}
		tone="warning"
		icon="shield"
		label={m('help.diagram.bank-change-dual-control.then')}
		items={after}
	/>
</svg>
