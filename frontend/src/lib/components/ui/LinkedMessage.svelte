<script lang="ts">
	import { splitTokens } from '$lib/i18n/segments';

	/**
	 * A translated sentence with links inside it.
	 *
	 * The message carries `{token}` markers and `links` maps each token to the
	 * anchor it renders as, so the translation controls where in the sentence
	 * each link falls. See `$lib/i18n/segments.ts` for why the alternative —
	 * `…Pre` / `…Post` catalogue fragments — cannot be translated correctly once
	 * there is more than one link.
	 *
	 * Renders real anchor elements, never `{@html}`: the only markup here is
	 * markup this component wrote, and the message text binds as text.
	 */
	let { text, links } = $props<{
		text: string;
		links: Record<string, { href: string; label: string }>;
	}>();

	let segments = $derived(splitTokens(text, Object.keys(links)));
</script>

{#each segments as segment, i (i)}{#if segment.token}<a href={links[segment.token].href}
			>{links[segment.token].label}</a
		>{:else}{segment.text}{/if}{/each}
