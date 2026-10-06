<script lang="ts">
	import { m } from '#lib/i18n/store.svelte.ts';
	import { kpiDisplayValue, kpiFigureState } from '#lib/utils/kpiValue.ts';
	import HelpTip from '#lib/components/help/HelpTip.svelte';

	type Props = {
		/**
		 * The figure, already formatted by the caller (this never re-formats).
		 * `null` / `undefined` means there is no figure to show — the response
		 * has not landed, or it failed — and the card renders the shared
		 * "no figure" dash instead of a number nobody computed. A genuine `0`
		 * is a figure and renders as one. See `#lib/utils/kpiValue`.
		 */
		value: string | number | null | undefined;
		label: string;
		/** Tints the card + value: green for positive, red for attention. */
		highlight?: 'green' | 'red' | null;
		/**
		 * Optional secondary line under the label — a qualifier on the headline
		 * figure (e.g. money the headline deliberately excludes). Rendered
		 * smaller + muted so the primary value stays the thing you read first;
		 * omit it entirely when there is nothing to qualify.
		 */
		sub?: string | null;
		/**
		 * The missing figure is still being fetched, as opposed to unavailable.
		 * Only meaningful alongside a nullish `value`: it changes nothing that
		 * is drawn (both render the same dash) and only what is *announced* —
		 * `aria-busy`, plus a screen-reader-only "Loading…" in place of the
		 * dash, so a placeholder is never read out as a value.
		 */
		pending?: boolean;
		/**
		 * A glossary id (`#lib/help/glossary.ts`): puts an ⓘ HelpTip beside the
		 * label explaining what the figure measures. Beside, not inside, the
		 * label span — the label's text stays exactly what specs select on.
		 */
		helpTerm?: string;
	};

	let { value, label, highlight = null, sub = null, pending = false, helpTerm }: Props = $props();

	const figure = $derived(kpiFigureState(value, pending));
	const shown = $derived(kpiDisplayValue(value, pending));

	// The tint is a verdict on the figure — green "this is money we saved", red
	// "this needs attention". With no figure there is no verdict, so a card
	// waiting on its response stays neutral rather than colouring a placeholder
	// as good or bad news. It also keeps a five-card row uniform while it loads.
	const tinted = $derived(figure === 'value');
</script>

<div
	class="kpi"
	class:highlight-green={tinted && highlight === 'green'}
	class:highlight-red={tinted && highlight === 'red'}
	aria-busy={figure === 'pending' ? 'true' : undefined}
	data-kpi-state={figure}
>
	<!-- The dash stays readable for `unavailable` (its `sub` line usually says
	     why); for `pending` it is hidden and replaced by the loading text below,
	     so assistive tech hears "Loading…" rather than "em dash".

	     Not a live region: a KPI row settles all its cards at once, so
	     `aria-live` here would fire a burst of announcements over whatever the
	     user was reading. `aria-busy` plus text present at first render is the
	     quiet form — a user who arrives on the card mid-load hears the state,
	     and one who is elsewhere is not interrupted by a page merely finishing
	     its own load. -->
	<span class="kpi-value" aria-hidden={figure === 'pending' ? 'true' : undefined}>{shown}</span>
	{#if figure === 'pending'}
		<span class="visually-hidden">{m('common.loading')}</span>
	{/if}
	{#if helpTerm}
		<span class="kpi-label-row"><span class="kpi-label">{label}</span><HelpTip term={helpTerm} /></span>
	{:else}
		<span class="kpi-label">{label}</span>
	{/if}
	{#if sub}
		<span class="kpi-sub">{sub}</span>
	{/if}
</div>

<style>
	/* `.kpi` itself is styled globally in app.css; this only anchors the
	   `.visually-hidden` loading text (which is `position: absolute`) so it
	   can't escape the card. The recipe itself is the shared one in app.css —
	   this component used to carry a private copy of it. */
	.kpi {
		position: relative;
	}
	.kpi-label-row {
		display: inline-flex;
		align-items: center;
		gap: 2px;
	}
</style>
