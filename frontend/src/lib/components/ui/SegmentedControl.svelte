<script lang="ts" generics="T extends string">
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
	 * Labels arrive already localized, like `SettingsRail` — the component is
	 * i18n-agnostic. First use: the colour theme on `/profile`
	 * (docs/decisions.md §289).
	 */
	interface Option {
		value: T;
		label: string;
	}

	let {
		legend,
		name,
		options,
		value,
		onchange,
		describedBy
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
	} = $props();
</script>

<fieldset class="segmented" aria-describedby={describedBy}>
	<legend>{legend}</legend>
	<div class="segments">
		{#each options as option (option.value)}
			<label class="segment" class:checked={value === option.value} data-value={option.value}>
				<input
					type="radio"
					{name}
					value={option.value}
					checked={value === option.value}
					onchange={() => onchange(option.value)}
				/>
				<span>{option.label}</span>
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
</style>
