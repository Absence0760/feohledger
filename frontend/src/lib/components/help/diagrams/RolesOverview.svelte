<script lang="ts">
	/**
	 * The four system roles and what each mainly does, per the verified role
	 * facts in lib/help/guides/start.ts and setup.ts (manage-users-roles):
	 * the clerk looks up and prepares, the AP manager runs AP day to day, the
	 * CFO signs off and oversees, the admin configures. A person may hold
	 * several roles and gets the union of what they allow.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { IconName } from './icons.ts';
	import DCard from './DCard.svelte';
	import DPill from './DPill.svelte';

	const label = $derived(m('help.diagram.roles-overview.label'));
	type Role = { icon: IconName; name: string; focus: string; items: string[] };
	const roles: Role[] = $derived([
		{
			icon: 'search',
			name: m('admin.roles.name.apClerk'),
			focus: m('help.diagram.roles-overview.clerkFocus'),
			items: [
				m('help.diagram.roles-overview.clerk1'),
				m('help.diagram.roles-overview.clerk2'),
				m('help.diagram.roles-overview.clerk3')
			]
		},
		{
			icon: 'invoice',
			name: m('admin.roles.name.apManager'),
			focus: m('help.diagram.roles-overview.managerFocus'),
			items: [
				m('help.diagram.roles-overview.manager1'),
				m('help.diagram.roles-overview.manager2'),
				m('help.diagram.roles-overview.manager3')
			]
		},
		{
			icon: 'chart',
			name: m('admin.roles.name.cfo'),
			focus: m('help.diagram.roles-overview.cfoFocus'),
			items: [
				m('help.diagram.roles-overview.cfo1'),
				m('help.diagram.roles-overview.cfo2'),
				m('help.diagram.roles-overview.cfo3')
			]
		},
		{
			icon: 'gear',
			name: m('admin.roles.name.admin'),
			focus: m('help.diagram.roles-overview.adminFocus'),
			items: [
				m('help.diagram.roles-overview.admin1'),
				m('help.diagram.roles-overview.admin2'),
				m('help.diagram.roles-overview.admin3')
			]
		}
	]);
	const several = $derived(m('help.diagram.roles-overview.several'));
</script>

<svg class="d-wide" viewBox="0 0 760 272" width="100%" role="img" aria-label={label}>
	{#each roles as r, i (i)}
		<DCard
			x={i * 195}
			y={0}
			w={175}
			h={232}
			layout="col"
			tone={i === 1 ? 'accent' : 'neutral'}
			icon={r.icon}
			label={r.name}
			sub={r.focus}
			items={r.items}
		/>
	{/each}
	<DPill x={380} y={258} w={420} icon="person" text={several} />
</svg>

<svg class="d-tall" viewBox="0 0 320 548" width="100%" role="img" aria-label={label}>
	{#each roles as r, i (i)}
		<DCard
			x={0}
			y={i * 130}
			w={320}
			h={120}
			tone={i === 1 ? 'accent' : 'neutral'}
			icon={r.icon}
			label={r.name}
			sub={r.focus}
			items={r.items}
		/>
	{/each}
	<DPill x={160} y={534} w={320} icon="person" text={several} />
</svg>
