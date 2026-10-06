<script lang="ts">
	/**
	 * The invoice lifecycle as a walkthrough: a numbered rail of stages (a tab
	 * list — arrow keys, Home and End move along it), and under it the selected
	 * stage: the statuses it covers, as the same badges /invoices shows, the
	 * statuses it can branch off to, what happens, who acts, and its guide.
	 *
	 * Stages and statuses come from `#lib/help/lifecycle.ts`, which is pinned to
	 * the workflow engine's status set; the badge words are catalogue labels.
	 */
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { LIFECYCLE } from '#lib/help/lifecycle.ts';
	import { INVOICE_STATUS_LABEL_KEYS } from '#lib/types/invoice.ts';
	import { guideFor } from '#lib/help/content.ts';
	import StatusBadge from '#lib/components/ui/StatusBadge.svelte';
	import RichText from './RichText.svelte';

	let selected = $state(0);
	const tabs: HTMLButtonElement[] = $state([]);
	const stage = $derived(LIFECYCLE[selected]);
	const guide = $derived(guideFor(stage.guide));
	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');
	const uid = `lifecycle-${Math.random().toString(36).slice(2, 8)}`;

	function select(i: number) {
		selected = (i + LIFECYCLE.length) % LIFECYCLE.length;
		tabs[selected]?.focus();
	}

	function onKeydown(e: KeyboardEvent) {
		const moves: Record<string, number> = {
			ArrowRight: selected + 1,
			ArrowDown: selected + 1,
			ArrowLeft: selected - 1,
			ArrowUp: selected - 1,
			Home: 0,
			End: LIFECYCLE.length - 1
		};
		if (e.key in moves) {
			e.preventDefault();
			select(moves[e.key]);
		}
	}
</script>

<div class="lifecycle" data-testid="invoice-lifecycle">
	<div class="rail" role="tablist" aria-label={m('help.lifecycle.label')} tabindex="-1" onkeydown={onKeydown}>
		{#each LIFECYCLE as s, i (s.id)}
			<button
				type="button"
				role="tab"
				id="{uid}-tab-{i}"
				aria-selected={i === selected}
				aria-controls="{uid}-panel"
				tabindex={i === selected ? 0 : -1}
				bind:this={tabs[i]}
				onclick={() => (selected = i)}
			>
				<span class="n" aria-hidden="true">{i + 1}</span>
				<span class="name">{m(INVOICE_STATUS_LABEL_KEYS[s.statuses[0]])}</span>
			</button>
		{/each}
	</div>

	<div class="panel" role="tabpanel" id="{uid}-panel" aria-labelledby="{uid}-tab-{selected}">
		<p class="step">{m('help.lifecycle.stage', { n: selected + 1, total: LIFECYCLE.length })}</p>
		<div class="badges">
			{#each stage.statuses as status (status)}<StatusBadge {status} />{/each}
			{#if stage.branches}
				<span class="branch">{m('help.lifecycle.branches')}</span>
				{#each stage.branches as status (status)}<StatusBadge {status} />{/each}
			{/if}
		</div>
		<p class="text" lang={englishLang}><RichText text={stage.text} /></p>
		<p class="who" lang={englishLang}>
			<span class="who-label" lang={englishLang ? currentLocale() : undefined}>{m('help.lifecycle.whoActs')}</span>
			{stage.who}
		</p>
		{#if guide}
			<a class="guide" href="/help/guides/{guide.id}">
				{m('help.lifecycle.readGuide')}: <span lang={englishLang}>{guide.title}</span>
			</a>
		{/if}
		<div class="pager">
			<button type="button" class="btn-cancel" disabled={selected === 0} onclick={() => select(selected - 1)}
				>{m('help.lifecycle.prev')}</button
			>
			<button
				type="button"
				class="btn-cancel"
				disabled={selected === LIFECYCLE.length - 1}
				onclick={() => select(selected + 1)}>{m('help.lifecycle.next')}</button
			>
		</div>
	</div>
</div>

<style>
	.lifecycle {
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--surface);
		box-shadow: var(--shadow-card);
		overflow: hidden;
	}
	/* The stages as a route: numbered stops joined by a line. Scrolls sideways
	   rather than wrapping on a narrow screen, so the order stays a line. */
	.rail {
		display: grid;
		grid-auto-flow: column;
		grid-auto-columns: minmax(7.5rem, 1fr);
		overflow-x: auto;
		padding: 18px 12px 12px;
		border-bottom: 1px solid var(--border);
		scrollbar-width: thin;
	}
	.rail button {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		min-height: 44px;
		padding: 0 6px 6px;
		border: 0;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		font-size: 0.8rem;
		text-align: center;
		cursor: pointer;
	}
	/* The line to the next stop. */
	.rail button:not(:last-child)::after {
		content: '';
		position: absolute;
		top: 14px;
		left: calc(50% + 18px);
		right: calc(-50% + 18px);
		height: 2px;
		background: var(--border);
	}
	.n {
		display: grid;
		place-items: center;
		width: 28px;
		height: 28px;
		border-radius: 50%;
		border: 2px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-weight: 700;
	}
	.rail button:hover .n {
		border-color: var(--accent);
	}
	.rail button[aria-selected='true'] {
		color: var(--text);
		font-weight: 600;
	}
	.rail button[aria-selected='true'] .n {
		border-color: var(--accent-strong);
		background: var(--accent-strong);
		color: #fff;
	}
	.panel {
		padding: 18px 20px 20px;
	}
	.step {
		margin: 0 0 8px;
		color: var(--text-muted);
		font-size: 0.75rem;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}
	.badges {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	.branch {
		margin-left: 8px;
		color: var(--text-muted);
		font-size: 0.8rem;
	}
	.text {
		margin: 12px 0 8px;
		line-height: 1.6;
		max-width: 46rem;
	}
	.text :global(a) {
		color: var(--accent);
	}
	.who {
		margin: 0 0 12px;
		color: var(--text-muted);
		font-size: 0.875rem;
	}
	.who-label {
		margin-right: 4px;
		color: var(--text);
		font-weight: 600;
	}
	.guide {
		color: var(--accent);
		font-weight: 600;
		font-size: 0.875rem;
	}
	.pager {
		display: flex;
		justify-content: space-between;
		gap: 8px;
		margin-top: 16px;
	}
	.pager button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
