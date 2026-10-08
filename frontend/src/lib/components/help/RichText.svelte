<script lang="ts">
	/**
	 * Help prose with its small inline markup (`#lib/help/types.ts`) rendered as
	 * elements: bold, emphasis, a UI label from the catalogue, and links to a
	 * glossary term, a guide or an app page. Never `{@html}` — every part binds
	 * as text inside an element this component wrote.
	 *
	 * A `{ui:key}` label and a term's link text come from m(), so under a
	 * non-English locale they read in the reader's language even though the
	 * prose around them is English; each is marked with the UI's own `lang`
	 * so a screen reader switches voice at the boundary (WCAG 3.1.2).
	 */
	import type { AnyMessageKey } from '#lib/i18n/messages.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { inline } from '#lib/help/inline.ts';
	import { navPageFor, termHref, termKey } from '#lib/help/content.ts';

	let { text }: { text: string } = $props();

	const parts = $derived(inline(text));
	// The prose is English; the labels m() fills in are the UI's language.
	const uiLang = $derived(currentLocale() === 'en' ? undefined : currentLocale());
</script>

{#each parts as part, i (i)}{#if part.kind === 'strong'}<strong>{part.text}</strong
		>{:else if part.kind === 'em'}<em>{part.text}</em
		>{:else if part.kind === 'ui'}<strong class="ui-label" lang={uiLang}>{m(part.key as AnyMessageKey)}</strong
		>{:else if part.kind === 'term'}<a href={termHref(part.id)} lang={part.label ? undefined : uiLang}
			>{part.label ?? m(termKey(part.id))}</a
		>{:else if part.kind === 'guide'}<a href="/help/guides/{part.id}">{part.label}</a
		>{:else if part.kind === 'page'}{@const nav = navPageFor(part.href)}<a
			href={part.href}
			lang={part.label ? undefined : uiLang}>{part.label ?? (nav ? m(nav.labelKey) : part.href)}</a
		>{:else}{part.text}{/if}{/each}
