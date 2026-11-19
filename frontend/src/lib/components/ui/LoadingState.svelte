<script lang="ts">
	/**
	 * A load in flight, stated in place: the busy ring and one line of text.
	 * THE standalone loading block — the page, panel, modal or card whose
	 * content has not arrived yet renders this where that content will go, in
	 * the `{#if loading}` / `{:else if …}` branch before it.
	 *
	 * It is a `role="status"` polite live region, so a screen-reader user hears
	 * that something is on its way rather than a silence until the content
	 * lands (WCAG 2.2 SC 4.1.3). Before it existed the same state was written
	 * 67 ways — `<p class="loading">`, `<p class="state">`, `.muted`, `.hint` —
	 * each with its own scoped CSS, and only three were announced.
	 *
	 * Mount it inside an `{#if}` on the request's state: the ring is the motion
	 * budget's one looping animation and must unmount when the answer lands
	 * (`a11y/marketingMotion.test.ts` checks every caller of this component
	 * as it does `<Spinner>`'s). A bare `{m('…loading')}` paragraph instead of
	 * this fails `a11y/liveRegionAudit.test.ts`.
	 *
	 * Not for: a `DataTable`'s loading row (its `empty` prop), a "Load more"
	 * footer (`LoadMoreFooter`), a busy button (`RowAction busy`), or the
	 * screen-reader-only placeholder inside a `KpiCard`.
	 *
	 * `label` arrives already localized and defaults to `common.loading`; pass
	 * a specific line ("Loading the ERP list…") when the wait is for something
	 * the reader should be told about. `testId` is the e2e hook — the class is
	 * Svelte-scoped.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import Spinner from './Spinner.svelte';

	let {
		label,
		testId
	}: {
		/** What is loading; defaults to `common.loading`. */
		label?: string;
		/** `data-testid` for an e2e selector. */
		testId?: string;
	} = $props();
</script>

<p class="loading-state" role="status" aria-live="polite" data-testid={testId}>
	<Spinner size={16} />
	<span>{label ?? m('common.loading')}</span>
</p>

<style>
	.loading-state {
		display: flex;
		align-items: center;
		gap: 10px;
		margin: 0;
		padding: 12px 0;
		color: var(--text-muted);
	}
</style>
