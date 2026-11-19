<script lang="ts">
	import { m } from '#lib/i18n/store.svelte.ts';
	import HelpTip from '#lib/components/help/HelpTip.svelte';

	// Clicking a prompt fills + sends it (the page wires `onpick`). The defaults
	// are the AP assistant's; the cash-flow copilot passes its own prompts and
	// copy so both chat surfaces share one empty state.
	let {
		onpick,
		prompts,
		heading,
		sub
	}: {
		onpick: (prompt: string) => void;
		/** Starter prompts, one button each, already localized. Defaults to the AP
		 *  assistant's `assistant.example.*` set. */
		prompts?: readonly string[];
		/** Already-localized heading; defaults to `assistant.empty.heading`. */
		heading?: string;
		/** Already-localized sub-line; defaults to `assistant.empty.sub`. */
		sub?: string;
	} = $props();

	// The default lives here, not as a constant beside the types: each prompt is
	// both a button label and the message it sends, so it follows the active
	// locale (an English-only default once showed in all six). `$derived`, so a
	// locale switch relabels the buttons in place.
	let list = $derived(
		prompts ?? [
			m('assistant.example.approvals'),
			m('assistant.example.topVendors'),
			m('assistant.example.poMismatch')
		]
	);
</script>

<div class="empty-state">
	<div class="empty-icon" aria-hidden="true">
		<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
			<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
		</svg>
	</div>
	<div class="help-row">
		<h2>{heading ?? m('assistant.empty.heading')}</h2>
		<HelpTip term="ai-assistant" />
	</div>
	<p class="empty-sub">
		{sub ?? m('assistant.empty.sub')}
	</p>
	<div class="prompt-list">
		{#each list as prompt (prompt)}
			<button type="button" class="prompt-btn" onclick={() => onpick(prompt)}>
				{prompt}
			</button>
		{/each}
	</div>
</div>

<style>
	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		text-align: center;
		gap: 10px;
		padding: 48px 20px;
		max-width: 560px;
		margin: 0 auto;
	}

	.empty-icon {
		color: var(--accent);
	}

	.empty-state h2 {
		margin: 0;
		font-size: 1.3rem;
	}
	.empty-sub {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.9rem;
		line-height: 1.5;
	}

	.prompt-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
		width: 100%;
		margin-top: 12px;
	}

	.prompt-btn {
		width: 100%;
		text-align: left;
		padding: 12px 16px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.9rem;
		cursor: pointer;
		transition: border-color 0.12s;
	}

	.prompt-btn:hover {
		border-color: var(--accent);
		color: var(--accent);
		background: color-mix(in srgb, var(--accent) 6%, transparent);
	}
</style>
