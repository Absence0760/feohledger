<script lang="ts">
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { ImportRowError } from '#lib/types/csvImport.ts';
	import { localizeApiDetail } from '#lib/api/codedRefusals.ts';

	// The per-row reasons a skip-and-report CSV import refused rows for, each
	// stated in the reader's language from its `{code, params}` and falling back
	// to the server's English (decisions §372, §377). Shared by the vendor /
	// invoice import modal and the expenses page's corporate-card import, so a
	// refused row never vanishes into a "skipped" count.
	let { errors }: { errors: ImportRowError[] } = $props();
</script>

<!-- The list can scroll, so its region is focusable for keyboard users (axe
     `scrollable-region-focusable`). -->
{#if errors.length > 0}
	<div class="error-scroll" role="region" aria-label={m('csvImport.result.errorsTitle')} tabindex="0">
		<ul class="error-list" data-testid="csv-import-errors">
			{#each errors as e (e.row)}
				<li>{m('csvImport.result.rowError', { row: e.row, message: localizeApiDetail(e, m) ?? e.message })}</li>
			{/each}
		</ul>
	</div>
{/if}

<style>
	.error-scroll {
		max-height: 260px;
		overflow-y: auto;
	}

	.error-list {
		margin: 0;
		padding-left: 18px;
		font-size: 0.8rem;
		color: var(--danger);
	}

	.error-list li {
		margin-bottom: 4px;
	}
</style>
