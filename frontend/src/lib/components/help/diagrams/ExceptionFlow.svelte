<script lang="ts">
	/**
	 * An exception's life: raised (by a check, a match, an agent or a person),
	 * open in the queue, then resolved, dismissed or escalated — and an
	 * escalated one is decided later. The four payment-blocking types
	 * (api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES) keep the invoice out of a
	 * payment run while open OR escalated; resolving or dismissing releases it.
	 * The lock badges carry that, explained by the legend.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import DCard from './DCard.svelte';
	import DArrow from './DArrow.svelte';
	import DLegend from './DLegend.svelte';

	const label = $derived(m('help.diagram.exception-flow.label'));
	const types = $derived([
		m('exceptions.type.duplicate'),
		m('exceptions.type.fraudFlag'),
		m('exceptions.type.lineTotalMismatch'),
		m('exceptions.type.paymentReconciliation')
	]);
	const outcomes = $derived([
		{
			icon: 'checkCircle',
			tone: 'success',
			label: m('exceptions.filter.resolved'),
			sub: m('help.diagram.exception-flow.resolvedSub'),
			badge: 'unlock',
			badgeTone: 'success'
		},
		{
			icon: 'crossCircle',
			tone: 'neutral',
			label: m('exceptions.filter.dismissed'),
			sub: m('help.diagram.exception-flow.dismissedSub'),
			badge: 'unlock',
			badgeTone: 'success'
		},
		{
			icon: 'escalate',
			tone: 'warning',
			label: m('exceptions.filter.escalated'),
			sub: m('help.diagram.exception-flow.escalatedSub'),
			badge: 'lock',
			badgeTone: 'danger'
		}
	] as const);
</script>

<svg class="d-wide" viewBox="0 0 760 300" width="100%" role="img" aria-label={label}>
	<DCard
		x={0}
		y={90}
		w={150}
		h={120}
		layout="col"
		icon="alert"
		label={m('help.diagram.exception-flow.raised')}
		sub={m('help.diagram.exception-flow.raisedSub')}
	/>
	<DArrow points={[[150, 150], [188, 150]]} />
	<DCard
		x={190}
		y={90}
		w={150}
		h={120}
		layout="col"
		tone="danger"
		icon="clock"
		label={m('exceptions.filter.open')}
		sub={m('help.diagram.exception-flow.openSub')}
		badge="lock"
		badgeTone="danger"
	/>
	<DArrow points={[[340, 150], [368, 150], [368, 42], [398, 42]]} />
	<DArrow points={[[340, 150], [398, 150]]} />
	<DArrow points={[[368, 150], [368, 258], [398, 258]]} />
	{#each outcomes as o, i (i)}
		<DCard
			x={400}
			y={6 + i * 108}
			w={170}
			h={72}
			icon={o.icon}
			tone={o.tone}
			label={o.label}
			sub={o.sub}
			badge={o.badge}
			badgeTone={o.badgeTone}
		/>
	{/each}
	<DArrow points={[[570, 258], [592, 258], [592, 42], [574, 42]]} tone="warning" dashed />
	<DArrow points={[[592, 150], [574, 150]]} tone="warning" dashed />

	<DCard
		x={618}
		y={10}
		w={142}
		h={178}
		tone="danger"
		label={m('help.term.payment-blocking-exception')}
		items={types}
	/>
	<DLegend x={620} y={216} w={140} icon="lock" tone="danger" text={m('help.diagram.exception-flow.blocks')} />
	<DLegend x={620} y={256} w={140} icon="unlock" tone="success" text={m('help.diagram.exception-flow.releases')} />
</svg>

<svg class="d-tall" viewBox="0 0 320 640" width="100%" role="img" aria-label={label}>
	<DCard
		x={0}
		y={0}
		w={320}
		h={60}
		icon="alert"
		label={m('help.diagram.exception-flow.raised')}
		sub={m('help.diagram.exception-flow.raisedSub')}
	/>
	<DArrow points={[[160, 60], [160, 88]]} />
	<DCard
		x={0}
		y={90}
		w={320}
		h={60}
		tone="danger"
		icon="clock"
		label={m('exceptions.filter.open')}
		sub={m('help.diagram.exception-flow.openSub')}
		badge="lock"
		badgeTone="danger"
	/>
	<DArrow points={[[20, 150], [20, 220], [38, 220]]} />
	<DArrow points={[[20, 220], [20, 294], [38, 294]]} />
	<DArrow points={[[20, 294], [20, 368], [38, 368]]} />
	{#each outcomes as o, i (i)}
		<DCard
			x={40}
			y={190 + i * 74}
			w={250}
			h={60}
			icon={o.icon}
			tone={o.tone}
			label={o.label}
			sub={o.sub}
			badge={o.badge}
			badgeTone={o.badgeTone}
		/>
	{/each}
	<DArrow points={[[290, 368], [308, 368], [308, 220], [294, 220]]} tone="warning" dashed />
	<DArrow points={[[308, 294], [294, 294]]} tone="warning" dashed />

	<DCard x={0} y={430} w={320} h={124} tone="danger" label={m('help.term.payment-blocking-exception')} items={types} />
	<DLegend x={4} y={580} w={316} icon="lock" tone="danger" text={m('help.diagram.exception-flow.blocks')} />
	<DLegend x={4} y={614} w={316} icon="unlock" tone="success" text={m('help.diagram.exception-flow.releases')} />
</svg>
