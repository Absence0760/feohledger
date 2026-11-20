<script lang="ts">
	/**
	 * The footer under every paged list: a "Load more (20 of 87)" button while
	 * rows remain unfetched, then "Showing all 87 invoices" once they are all
	 * on screen, and nothing at all for an empty list or a failed load.
	 *
	 * "Showing all N" is only ever the else-branch of `hasMore`: `total` is the
	 * server's count of the WHOLE filtered set, so a footer that states it while
	 * rows are still unfetched asserts that rows nobody loaded do not exist (six
	 * lists once shipped exactly that). Under a failed load (`errored`) neither
	 * branch renders, since both counts would describe rows not on screen.
	 *
	 * `moreKey` takes `{shown}` and `{total}`, both formatted here with
	 * `formatCount`; `allKey` takes the raw `{total}`, because every such
	 * message selects its plural on it and its `#` does the grouping
	 * (`formattedCountPlural.test.ts`).
	 *
	 * The `.load-more-row`, `.btn-load-more` and `.load-more-end` classes are
	 * styled in app.css and are what Playwright specs select on; keep them.
	 * `src/lib/utils/pagedListFooter.test.ts` refuses a hand-rolled copy of
	 * this markup anywhere else.
	 */
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatCount } from '#lib/utils/count.ts';

	let {
		shown,
		total,
		hasMore,
		loading,
		errored = false,
		disabled = false,
		onloadmore,
		moreKey,
		allKey,
		loadingKey = 'common.loading',
		testId
	}: {
		/** Rows currently on screen. */
		shown: number;
		/** The server's count of the whole filtered set. */
		total: number;
		hasMore: boolean;
		/** A load-more request is in flight: the button says so and is disabled. */
		loading: boolean;
		/** The last load failed: render no footer at all. */
		errored?: boolean;
		/** Disable the button for a reason other than its own request (a reload). */
		disabled?: boolean;
		onloadmore: () => void;
		/** "Load more ({shown} of {total})". */
		moreKey: MessageKey;
		/** "{total, plural, … Showing all # … }". */
		allKey: MessageKey;
		loadingKey?: MessageKey;
		/** `data-testid` on the button. */
		testId?: string;
	} = $props();
</script>

{#if hasMore && !errored}
	<div class="load-more-row">
		<button
			type="button"
			class="btn-load-more"
			onclick={onloadmore}
			disabled={loading || disabled}
			data-testid={testId}
		>
			{loading
				? m(loadingKey)
				: m(moreKey, { shown: formatCount(shown), total: formatCount(total) })}
		</button>
	</div>
{:else if total > 0 && !errored}
	<div class="load-more-row">
		<span class="load-more-end">{m(allKey, { total })}</span>
	</div>
{/if}
