<script lang="ts">
	/**
	 * The ways an invoice arrives, and what reads it. Uploads, email intake and
	 * the supplier portal are read by AI extraction; a PEPPOL e-invoice is read
	 * from its structured data; a CSV import is read by nothing — it lands with
	 * the fields the file gave it, and runs no automatic checks
	 * (services/csv_import.py), so its line skips both columns.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import DCard from './DCard.svelte';
	import DArrow from './DArrow.svelte';
	import DPill from './DPill.svelte';

	const label = $derived(m('help.diagram.capture-channels.label'));
	const channels = $derived([
		{ icon: 'upload', label: m('help.diagram.capture-channels.upload'), sub: m('help.diagram.capture-channels.uploadSub') },
		{ icon: 'mail', label: m('help.term.email-intake'), sub: m('help.diagram.capture-channels.emailSub') },
		{ icon: 'store', label: m('help.term.supplier-portal'), sub: m('help.diagram.capture-channels.portalSub') },
		{ icon: 'network', label: m('help.term.peppol'), sub: m('help.term.e-invoice') },
		{ icon: 'table', label: m('help.diagram.capture-channels.csv'), sub: m('help.diagram.capture-channels.csvSub') }
	] as const);
	const checks = $derived([
		m('help.diagram.capture-channels.checkVendor'),
		m('invoices.modal.poMatch.title'),
		m('help.diagram.capture-channels.checkDuplicates')
	]);
</script>

<svg class="d-wide" viewBox="0 0 760 324" width="100%" role="img" aria-label={label}>
	{#each channels as c, i (i)}
		<DCard x={0} y={i * 66} w={196} h={56} icon={c.icon} label={c.label} sub={c.sub} />
	{/each}
	{#each [28, 94, 160] as y (y)}<DArrow points={[[196, y], [232, y]]} />{/each}
	<DArrow points={[[196, 292], [690, 292], [690, 204]]} dashed />
	<DPill x={312} y={292} w={200} text={m('help.diagram.capture-channels.noReading')} />

	<DCard
		x={234}
		y={6}
		w={164}
		h={176}
		layout="col"
		tone="accent"
		icon="sparkle"
		label={m('help.term.extraction')}
		sub={m('help.diagram.capture-channels.extractionSub')}
	/>
	<DArrow points={[[196, 226], [232, 226]]} />
	<DCard
		x={234}
		y={198}
		w={164}
		h={56}
		label={m('help.diagram.capture-channels.structured')}
		sub={m('help.diagram.capture-channels.structuredSub')}
	/>

	<DArrow points={[[398, 94], [432, 94]]} />
	<DArrow points={[[398, 226], [432, 226]]} />
	<DCard
		x={434}
		y={40}
		w={160}
		h={226}
		layout="col"
		icon="search"
		label={m('help.diagram.capture-channels.checks')}
		items={checks}
	/>
	<DArrow points={[[594, 153], [618, 153]]} />
	<DCard
		x={620}
		y={102}
		w={140}
		h={102}
		layout="col"
		tone="success"
		icon="inbox"
		label={m('help.diagram.capture-channels.review')}
	/>
</svg>

<svg class="d-tall" viewBox="0 0 320 500" width="100%" role="img" aria-label={label}>
	{#each channels as c, i (i)}
		<DCard
			x={0}
			y={i * 60}
			w={150}
			h={50}
			icon={c.icon}
			label={c.label}
			sub={i === 4 ? m('help.diagram.capture-channels.noReading') : undefined}
		/>
	{/each}
	{#each [25, 85, 145] as y (y)}<DArrow points={[[150, y], [178, y]]} />{/each}
	<DCard
		x={180}
		y={0}
		w={120}
		h={170}
		layout="col"
		tone="accent"
		icon="sparkle"
		label={m('help.term.extraction')}
		sub={m('help.diagram.capture-channels.extractionSub')}
	/>
	<DArrow points={[[150, 205], [178, 205]]} />
	<DCard
		x={180}
		y={180}
		w={120}
		h={78}
		label={m('help.diagram.capture-channels.structured')}
		sub={m('help.diagram.capture-channels.structuredSub')}
	/>
	<DArrow points={[[300, 85], [312, 85], [312, 298]]} />
	<DArrow points={[[300, 205], [312, 205]]} head={false} />
	<DCard
		x={40}
		y={300}
		w={280}
		h={96}
		icon="search"
		label={m('help.diagram.capture-channels.checks')}
		items={checks}
	/>
	<DArrow points={[[20, 290], [20, 438]]} dashed />
	<DArrow points={[[180, 396], [180, 438]]} />
	<DCard
		x={0}
		y={440}
		w={320}
		h={58}
		tone="success"
		icon="inbox"
		label={m('help.diagram.capture-channels.review')}
	/>
</svg>
