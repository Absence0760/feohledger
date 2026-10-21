<script lang="ts" module>
	export interface SetupChecklistStep {
		id: string;
		label: string;
		href: string;
		/** `true` done, `false` still to do, `null` — no fact to say either. */
		done: boolean | null;
	}
</script>

<script lang="ts">
	/**
	 * An ordered list of setup steps, each a link to where it is done and,
	 * when the server knows, whether it has been (`docs/decisions.md` §309).
	 *
	 * A step's `done` is one of three things, and they are drawn differently on
	 * purpose:
	 *
	 * - `true` — a check mark and a visible "Done" badge (text, not colour
	 *   alone: WCAG 1.4.1);
	 * - `false` — its number, plus screen-reader text saying it is still to do;
	 * - `null` — its number and nothing else. A step with no fact behind it is
	 *   never drawn as finished or unfinished: a tick nobody can verify is the
	 *   thing this list must not show.
	 *
	 * Labels arrive already localized (the dashboard's keys live in the main
	 * catalogue, the settings page's in its slice); the two status words are
	 * this component's own, under `setupChecklist.*`.
	 */
	import IconCheck from '~icons/material-symbols/check-rounded';
	import Badge from '#lib/components/ui/Badge.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		steps,
		label,
		labelledby,
		testId
	}: {
		steps: SetupChecklistStep[];
		/** Accessible name for the list, when no visible heading names it. */
		label?: string;
		/** Id of the visible heading that names the list — preferred over `label`. */
		labelledby?: string;
		/** `data-testid` for an e2e selector; the class is Svelte-scoped. */
		testId?: string;
	} = $props();
</script>

<ol
	class="setup-checklist"
	aria-labelledby={labelledby}
	aria-label={labelledby ? undefined : label}
	data-testid={testId}
>
	{#each steps as step, i (step.id)}
		<li
			class="step"
			class:done={step.done === true}
			data-step={step.id}
			data-done={step.done === null ? 'untracked' : String(step.done)}
		>
			<span class="marker" aria-hidden="true">
				{#if step.done === true}<IconCheck />{:else}{i + 1}{/if}
			</span>
			<a href={step.href}>{step.label}</a>
			{#if step.done === true}
				<Badge tone="success">{m('setupChecklist.done')}</Badge>
			{:else if step.done === false}
				<span class="visually-hidden">{m('setupChecklist.todo')}</span>
			{/if}
		</li>
	{/each}
</ol>

<style>
	.setup-checklist {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
	}

	.step {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 10px 0;
		border-bottom: 1px solid var(--border);
		min-width: 0;
	}

	.step:last-child {
		border-bottom: none;
	}

	.marker {
		flex: none;
		display: inline-grid;
		place-items: center;
		width: 26px;
		height: 26px;
		border-radius: 50%;
		border: 1px solid var(--border-strong);
		color: var(--text-muted);
		font-size: 0.78rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}

	.step.done .marker {
		border-color: transparent;
		background: var(--success-tint);
		color: var(--success-on-tint);
		font-size: 1rem;
	}

	/* A link, and the row's whole target: padded to clear 24px (WCAG 2.5.8). */
	.step a {
		flex: 1;
		min-width: 0;
		padding: 4px 0;
		color: var(--text);
		font-weight: 500;
		overflow-wrap: anywhere;
	}

	.step.done a {
		color: var(--text-muted);
	}
</style>
