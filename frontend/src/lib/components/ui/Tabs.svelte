<script lang="ts">
	type Tab = {
		key: string;
		label: string;
		/** Optional count badge rendered in `.tab-count`. Omit for label-only tabs. */
		count?: number | string;
	};

	type Props = {
		tabs: Tab[];
		/** The active tab key. Two-way bindable. */
		active: string;
		/** Fired in addition to updating `active`. */
		onchange?: (key: string) => void;
		/** Accessible name for the tablist. */
		ariaLabel?: string;
		/** Prefix for the generated tab `id` + `aria-controls`. The caller gives
		    the visible panel `id="<idPrefix>-panel-<activeKey>"` + `role="tabpanel"`
		    + `aria-labelledby="<idPrefix>-tab-<activeKey>"` to complete the pairing. */
		idPrefix?: string;
	};

	let { tabs, active = $bindable(), onchange, ariaLabel, idPrefix = 'tabs' }: Props = $props();

	let tablistEl = $state<HTMLDivElement | null>(null);

	function select(key: string) {
		if (key === active) return;
		active = key;
		onchange?.(key);
	}

	// WAI-ARIA tabs pattern (WCAG 2.4.3 / 4.1.2): roving tabindex — only the
	// active tab is in the Tab order; Arrow keys move between tabs and activate.
	function onKey(e: KeyboardEvent) {
		const idx = tabs.findIndex((t) => t.key === active);
		if (idx === -1) return;
		let next = idx;
		if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % tabs.length;
		else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + tabs.length) % tabs.length;
		else if (e.key === 'Home') next = 0;
		else if (e.key === 'End') next = tabs.length - 1;
		else return;
		e.preventDefault();
		select(tabs[next].key);
		tablistEl
			?.querySelector<HTMLElement>(`#${CSS.escape(`${idPrefix}-tab-${tabs[next].key}`)}`)
			?.focus();
	}
</script>

<!-- svelte-ignore a11y_interactive_supports_focus -->
<!-- The tablist itself is not in the Tab order by design (WAI-ARIA APG tabs
     pattern): focus lives on the active tab via roving tabindex below, and the
     tablist's onkeydown only re-dispatches Arrow/Home/End to those tabs. -->
<div
	bind:this={tablistEl}
	class="tab-row"
	role="tablist"
	aria-label={ariaLabel}
	onkeydown={onKey}
>
	{#each tabs as tab (tab.key)}
		<button
			class="tab"
			class:active={active === tab.key}
			type="button"
			role="tab"
			id={`${idPrefix}-tab-${tab.key}`}
			aria-controls={`${idPrefix}-panel-${tab.key}`}
			aria-selected={active === tab.key}
			tabindex={active === tab.key ? 0 : -1}
			onclick={() => select(tab.key)}
		>
			{tab.label}{#if tab.count !== undefined}{' '}<span class="tab-count">{tab.count}</span>{/if}
		</button>
	{/each}
</div>

<style>
	/* Underline tab bar. Lifted from the per-route copies in
	   /expenses, /payments, /audit so new pages share one source. */
	.tab-row {
		display: flex;
		/* Tabs drop onto a second line rather than pushing the document sideways
		   at a narrow viewport (WCAG 1.4.10 Reflow). Wrapping, not
		   `overflow-x: auto`: 1.4.10 only excuses two-dimensional content from
		   reflowing, and a row of navigation controls is not that — a hidden
		   tab behind a sideways scroll is a destination the reader never finds. */
		flex-wrap: wrap;
		gap: 4px;
		border-bottom: 1px solid var(--border);
	}
	.tab {
		padding: 8px 16px;
		border: none;
		background: none;
		color: var(--text-muted);
		font-size: 0.9rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		border-bottom: 2px solid transparent;
		margin-bottom: -1px;
	}
	.tab:hover {
		color: var(--text);
	}
	.tab.active {
		color: var(--accent);
		border-bottom-color: var(--accent);
	}
	.tab-count {
		font-size: 0.78rem;
		color: var(--text-muted);
	}
</style>
