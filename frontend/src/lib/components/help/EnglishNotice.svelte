<script lang="ts">
	/**
	 * Says, in the reader's language, that the help prose around it is English
	 * — shown only under another UI locale. The guide and glossary text is
	 * authored in English and not yet translated (docs/decisions.md §244); the
	 * buttons, statuses and terms it names are rendered from the catalogue, so
	 * they still read in the reader's language.
	 */
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
</script>

{#if currentLocale() !== 'en'}
	<p class="english-notice" data-testid="help-english-notice">{m('help.englishOnly')}</p>
{/if}

<style>
	.english-notice {
		margin: 14px 0 0;
		padding: 10px 14px;
		border-radius: var(--radius-sm);
		background: var(--muted-tint);
		color: var(--muted-on-tint);
		font-size: 0.875rem;
	}
</style>
