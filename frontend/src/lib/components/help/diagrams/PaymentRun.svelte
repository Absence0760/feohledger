<script lang="ts">
	/**
	 * A payment run from approved invoice to Paid, and what can stop it.
	 *
	 * Steps (services/payment_runs.py, payment_controls.py, payment_settlement):
	 * approved invoices → a draft run (one currency, nothing moved) → CFO
	 * sign-off only when the run total is above the threshold → execute on a
	 * rail → the processor confirms settlement → the invoice is Paid. Signing
	 * off and executing are both closed to the run's creator (maker-checker).
	 *
	 * Gates: a payment-blocking exception keeps an invoice out of a run (and is
	 * checked again at execution); at execution a sanctions review holds the
	 * payment at Compliance Hold and a blocked vendor's payment is refused.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { IconName } from './icons.ts';
	import type { Tone } from './tone.ts';
	import DCard from './DCard.svelte';
	import DArrow from './DArrow.svelte';
	import DPill from './DPill.svelte';
	import DStop from './DStop.svelte';

	const label = $derived(m('help.diagram.payment-run.label'));
	const rails = $derived(
		[m('payments.method.ach'), m('payments.method.wire'), m('payments.method.check'), m('payments.method.virtualCard')].join(' · ')
	);
	type Step = { icon: IconName; label: string; sub?: string; tone?: Tone; dashed?: boolean };
	const steps: Step[] = $derived([
		{ icon: 'invoice', label: m('help.diagram.payment-run.approvedInvoices'), sub: m('help.diagram.payment-run.queueSub') },
		{ icon: 'stack', label: m('help.diagram.payment-run.draftRun'), sub: m('help.diagram.payment-run.draftSub') },
		{ icon: 'shieldCheck', label: m('help.diagram.payment-run.cfoSignOff'), sub: m('help.diagram.payment-run.cfoSub'), dashed: true },
		{ icon: 'send', label: m('help.diagram.payment-run.execute'), sub: rails, tone: 'accent' },
		{ icon: 'coins', label: m('help.term.settlement'), sub: m('help.diagram.payment-run.settleSub') },
		{ icon: 'checkCircle', label: m('invoices.status.paid'), tone: 'success' }
	]);
	const gates = $derived([
		{ icon: 'lock' as const, label: m('help.term.payment-blocking-exception'), sub: m('help.diagram.payment-run.blockingSub') },
		{ icon: 'clock' as const, label: m('payments.status.pendingCompliance'), sub: m('help.diagram.payment-run.complianceSub') },
		{ icon: 'ban' as const, label: m('help.diagram.payment-run.blockedVendor'), sub: m('help.diagram.payment-run.blockedSub') }
	]);
	const notCreator = $derived(m('help.diagram.payment-run.notCreator'));
	const TALL_Y = [0, 100, 224, 324, 448, 548];
	const TALL_H = 72;
</script>

<svg class="d-wide" viewBox="0 0 760 376" width="100%" role="img" aria-label={label}>
	<rect class="frame" x="252" y="40" width="246" height="176" rx="16" />
	<DPill x={375} y={40} w={240} icon="person" tone="accent" text={notCreator} />
	{#each steps as s, i (i)}
		<DCard
			x={i * 130}
			y={60}
			w={110}
			h={144}
			layout="col"
			icon={s.icon}
			label={s.label}
			sub={s.sub}
			tone={s.tone}
			dashed={s.dashed}
		/>
		{#if i < steps.length - 1}<DArrow points={[[i * 130 + 110, 132], [i * 130 + 128, 132]]} />{/if}
	{/each}

	<DArrow points={[[120, 262], [120, 144]]} tone="danger" dashed head={false} />
	<DStop x={120} y={132} r={9} />
	<DArrow points={[[400, 262], [400, 238], [515, 238], [515, 144]]} tone="danger" dashed head={false} />
	<DArrow points={[[640, 262], [640, 238], [515, 238]]} tone="danger" dashed head={false} />
	<DStop x={515} y={132} r={9} />

	<DCard x={0} y={264} w={250} h={100} tone="danger" icon={gates[0].icon} label={gates[0].label} sub={gates[0].sub} />
	<DCard x={290} y={264} w={220} h={100} tone="danger" icon={gates[1].icon} label={gates[1].label} sub={gates[1].sub} />
	<DCard x={540} y={264} w={220} h={100} tone="danger" icon={gates[2].icon} label={gates[2].label} sub={gates[2].sub} />
</svg>

<svg class="d-tall" viewBox="0 0 320 624" width="100%" role="img" aria-label={label}>
	<rect class="frame" x="-6" y="202" width="202" height="206" rx="16" />
	<DPill x={95} y={202} w={200} icon="person" tone="accent" text={notCreator} />
	{#each steps as s, i (i)}
		<DCard
			x={0}
			y={TALL_Y[i]}
			w={190}
			h={TALL_H}
			icon={s.icon}
			label={s.label}
			sub={s.sub}
			tone={s.tone}
			dashed={s.dashed}
		/>
		{#if i < steps.length - 1}<DArrow points={[[95, TALL_Y[i] + TALL_H], [95, TALL_Y[i + 1] - 2]]} />{/if}
	{/each}

	<DArrow points={[[212, 86], [107, 86]]} tone="danger" dashed head={false} />
	<DStop x={95} y={86} r={9} />
	<DArrow points={[[212, 325], [204, 325], [204, 427], [107, 427]]} tone="danger" dashed head={false} />
	<DArrow points={[[212, 465], [204, 465], [204, 427]]} tone="danger" dashed head={false} />
	<DStop x={95} y={427} r={9} />

	<DCard x={212} y={20} w={108} h={150} layout="col" tone="danger" icon={gates[0].icon} label={gates[0].label} />
	<DCard x={212} y={270} w={108} h={110} layout="col" tone="danger" icon={gates[1].icon} label={gates[1].label} />
	<DCard x={212} y={410} w={108} h={110} layout="col" tone="danger" icon={gates[2].icon} label={gates[2].label} />
</svg>

<style>
	.frame {
		fill: color-mix(in srgb, var(--accent) 5%, transparent);
		stroke: color-mix(in srgb, var(--accent) 70%, var(--surface));
		stroke-width: 1.25;
		stroke-dasharray: 6 4;
	}
</style>
