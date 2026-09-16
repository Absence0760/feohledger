<script lang="ts">
	import IconPause from '~icons/material-symbols/pause';
	import IconPlay from '~icons/material-symbols/play-arrow';

	/**
	 * Stops and restarts the decorative animation on the public surfaces —
	 * WCAG 2.2.2 Pause, Stop, Hide.
	 *
	 * The criterion applies to anything that moves automatically for more than
	 * five seconds alongside other content, which is exactly what the hero
	 * sequence, the drifting aurora and the adapter rail are. `prefers-reduced-
	 * motion` does not discharge it: that is an OS-level setting a visitor may
	 * not have, may not know about, and cannot change for one page. The
	 * criterion asks for a control **in the content**.
	 *
	 * It sets `data-motion` on the shell it is given, and `app.css` turns that
	 * into `animation-play-state: paused` for the whole subtree — so a new
	 * animation anywhere under the shell is covered the day it is written,
	 * rather than on the day someone remembers this exists.
	 *
	 * The state is a `$bindable` rather than component-local because the page
	 * root is what carries the attribute, not this button.
	 *
	 * Labels arrive as props because this component's only caller is the
	 * marketing page, whose copy is deliberately English-only — the same call
	 * the legal pages make. Passing them in means the day a translated surface
	 * wants a pause control, it hands over `m('…')` instead of this file growing
	 * a catalogue dependency the marketing page would then also carry.
	 */
	interface Props {
		paused: boolean;
		pauseLabel?: string;
		playLabel?: string;
	}

	let {
		paused = $bindable(false),
		pauseLabel = 'Pause animation',
		playLabel = 'Play animation'
	}: Props = $props();
</script>

<button
	type="button"
	class="motion-toggle"
	aria-pressed={paused}
	onclick={() => (paused = !paused)}
>
	{#if paused}
		<IconPlay aria-hidden="true" />
	{:else}
		<IconPause aria-hidden="true" />
	{/if}
	<span class="label">{paused ? playLabel : pauseLabel}</span>
</button>

<style>
	.motion-toggle {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		/* 36px, not the 24px SC 2.5.8 floor: this sits in a nav row beside other
		   controls, and the extra height is what keeps the icon optically level
		   with the text links next to it. */
		min-width: 36px;
		min-height: 36px;
		padding: 0 9px;
		border-radius: 999px;
		border: 1px solid var(--border);
		background: rgba(24, 26, 35, 0.6);
		color: var(--text-muted);
		font-family: inherit;
		font-size: 1rem;
		line-height: 1;
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s, background 0.15s;
	}
	.motion-toggle:hover {
		color: var(--text);
		border-color: var(--text-muted);
	}
	.motion-toggle:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	/* The label names the action for a screen reader and is the button's
	   accessible name; sighted users get the glyph. Clipped rather than
	   `display: none` so it stays in the accessibility tree. */
	.label {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		overflow: hidden;
		clip-path: inset(50%);
		white-space: nowrap;
		border: 0;
	}
</style>
