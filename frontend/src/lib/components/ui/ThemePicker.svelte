<script lang="ts">
	import SegmentedControl from '#lib/components/ui/SegmentedControl.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { theme } from '#lib/stores/theme.svelte.ts';
	import { THEME_CHOICES, type ThemeChoice } from '#lib/theme.ts';

	/**
	 * The System / Light / Dark picker, bound to the theme store
	 * (docs/decisions.md §289, §290): a `SegmentedControl` of native radios,
	 * applied the moment one is picked and remembered across every workspace of
	 * the platform. Rendered on `/profile?section=appearance` and under the
	 * form of every `AuthShell` page — the second is how a visitor on the signup
	 * page brings a light theme into the workspace it creates.
	 *
	 * `name` must be unique on the page; `describedBy` points at a hint, where
	 * the caller shows one.
	 */
	let { name, describedBy }: { name: string; describedBy?: string } = $props();

	const LABEL_KEYS: Record<ThemeChoice, MessageKey> = {
		system: 'profile.appearance.system',
		light: 'profile.appearance.light',
		dark: 'profile.appearance.dark'
	};
	const options = $derived(THEME_CHOICES.map((value) => ({ value, label: m(LABEL_KEYS[value]) })));
</script>

<SegmentedControl
	legend={m('profile.appearance.legend')}
	{name}
	{options}
	value={theme.choice}
	onchange={(choice) => theme.set(choice)}
	{describedBy}
/>
