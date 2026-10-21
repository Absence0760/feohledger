<script lang="ts" generics="T extends string">
	import type { Component } from 'svelte';

	/**
	 * A small, single-choice picker drawn as a row of segments — a `<fieldset>`
	 * of real radio buttons, because that is what it is.
	 *
	 * Native radios rather than buttons with `aria-pressed` or a hand-rolled
	 * `role="radiogroup"`: the browser already gives a radio group the keyboard
	 * model WCAG expects (Tab enters the group at the checked option, arrow
	 * keys move AND select, Space selects), a group name from the `<legend>`,
	 * and "1 of 3, selected" from every screen reader — none of which needs a
	 * line of script here. The radio dot stays visible (the global radio style
	 * in `app.css`); the whole segment is its label, so the hit area is the
	 * segment, not the 16px dot.
	 *
	 * `compact` draws the same fieldset as a row of icons, for a header with no
	 * room for words (the landing page's theme switch, docs/decisions.md §306).
	 * Nothing about the semantics changes: the legend and every label are still
	 * in the accessibility tree, only visually hidden, so the group keeps its
	 * name and each radio its label; the radio itself is visually hidden too and
	 * the segment carries its focus ring. Each segment also gets a `title`, so a
	 * pointer user can find out what an icon means. Every option needs an
	 * `icon` in this mode — an icon-less compact segment would be an empty box.
	 *
	 * Labels arrive already localized, like `SettingsRail` — the component is
	 * i18n-agnostic. First use: the colour theme on `/profile`
	 * (docs/decisions.md §289).
	 */
	interface Option {
		value: T;
		label: string;
		/** Drawn before the label; required when `compact`. Decorative. */
		icon?: Component;
	}

	let {
		legend,
		name,
		options,
		value,
		onchange,
		describedBy,
		compact = false
	}: {
		/** The group's accessible name, rendered as a visible `<legend>`. */
		legend: string;
		/** The radio group's `name` — unique on the page. */
		name: string;
		options: Option[];
		value: T;
		onchange: (value: T) => void;
		/** id of a hint element describing the whole group. */
		describedBy?: string;
		/** Icons only; legend and labels kept for assistive tech. */
		compact?: boolean;
	} = $props();
</script>

<fieldset class="segmented" class:compact aria-describedby={describedBy}>
	<legend class:visually-hidden={compact}>{legend}</legend>
	<div class="segments">
		{#each options as option (option.value)}
			<label
				class="segment"
				class:checked={value === option.value}
				data-value={option.value}
				title={compact ? option.label : undefined}
			>
				<input
					type="radio"
					{name}
					value={option.value}
					checked={value === option.value}
					onchange={() => onchange(option.value)}
				/>
				{#if option.icon}<option.icon aria-hidden="true" class="segment-icon" />{/if}
				<span class:visually-hidden={compact}>{option.label}</span>
			</label>
		{/each}
	</div>
</fieldset>

<style>
	.segmented {
		border: none;
		margin: 0;
		padding: 0;
		min-width: 0;
	}
	legend {
		padding: 0;
		margin-bottom: 6px;
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	.segments {
		display: inline-flex;
		flex-wrap: wrap;
		gap: 4px;
		padding: 4px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg);
		max-width: 100%;
	}
	/* The segment is the label, so it is the target: 36px tall clears the
	   2.5.8 floor on its own without leaning on spacing. */
	.segment {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		min-height: 36px;
		padding: 6px 14px;
		border: 1px solid transparent;
		border-radius: 6px;
		color: var(--text-muted);
		font-size: 0.88rem;
		cursor: pointer;
		transition: border-color 0.12s, color 0.12s;
	}
	/* Border and text on hover, never a fill (the badge recipe is <Badge>'s). */
	.segment:hover {
		color: var(--text);
		border-color: var(--border);
	}
	/* The selected segment is lifted onto --surface with --text: it reads as
	   chosen by weight, edge and the filled radio dot together — never by
	   colour alone (1.4.1). */
	.segment.checked {
		background: var(--surface);
		color: var(--text);
		border-color: var(--border);
		font-weight: 600;
		box-shadow: var(--shadow-card);
	}
	/* The radio keeps its own global focus ring; ring the whole segment too so
	   the focused option is obvious at a glance (2.4.7). */
	.segment:has(input:focus-visible) {
		outline: 2px solid var(--accent);
		outline-offset: 1px;
	}

	/* Compact: icons in a pill. 30px squares clear the 24px 2.5.8 floor with
	   room to spare. The track is a translucent --surface so it sits on
	   whatever the header is over; the checked ring is below. */
	.compact .segments {
		flex-wrap: nowrap;
		gap: 2px;
		padding: 2px;
		border-radius: 999px;
		background: color-mix(in srgb, var(--surface) 55%, transparent);
	}
	.compact .segment {
		position: relative;
		justify-content: center;
		min-height: 30px;
		min-width: 30px;
		padding: 0;
		border-radius: 999px;
		font-size: 1rem;
	}
	/* The radio, visually hidden but still the focusable, checkable control.
	   A scoped rule rather than the global `.visually-hidden`, which at (0,1,0)
	   loses to app.css's `input[type='radio']` width and height. Clipped, not
	   faded: an at-rest `opacity` is what opacityAudit.test.ts keeps out. */
	.compact .segment input {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		border: 0;
		overflow: hidden;
		clip-path: inset(50%);
	}
	/* With the dot hidden, the raised fill is no cue at all (1.04:1 against the
	   track in both palettes) and --border is ~1.3:1. The chosen segment gets
	   an --accent ring (≥5.7:1 against the track in both palettes, 1.4.11) on
	   the calibrated accent tint pair: the ring is an edge the others lack,
	   which a hovered option's --border edge does not imitate, so the state
	   never rests on hue alone (1.4.1). */
	.compact .segment.checked {
		border-color: var(--accent);
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		box-shadow: none;
	}
	/* The segment, not the hidden radio, is what the eye finds. */
	.compact .segment:has(input:focus-visible) {
		outline-offset: 0;
	}
	.segment :global(.segment-icon) {
		flex: none;
	}
</style>
