<script lang="ts">
	/**
	 * The "there is genuinely nothing here yet, and here is the one thing to
	 * do about it" block — a first-run / zero-data affordance, not a filtered
	 * "no matches" line.
	 *
	 * Callers MUST keep the four empty states distinct (frontend/CLAUDE.md
	 * § Data tables): render this ONLY for the genuinely-empty-and-unfiltered
	 * case. `loading`, `errored`, and "a filter matched nothing" keep their own
	 * copy (a plain message / retry block) — this component is not for them.
	 *
	 * i18n-agnostic like `FieldWarning` / `SecretReveal`: every string is
	 * passed in already-localized. The optional action renders as a `<button>`
	 * (pass `onaction`) or an `<a>` (pass `actionHref`); omit both for a
	 * message-only state.
	 *
	 * `art` picks one of the generated illustrations
	 * (`assets/illustrations/gen_empty_states.py` → `emptyStateArt.generated.ts`)
	 * and wins over the older emoji `icon`. Inline SVG rather than an `<img>`
	 * because the strokes follow the palette and the ACCENT follows the
	 * tenant's brand, which an image file cannot see; built element by element
	 * from data because nothing in this tree uses `{@html}`. It is decorative
	 * (`aria-hidden`): the heading is what says the page is empty.
	 */
	import {
		ART_VIEWBOX,
		EMPTY_STATE_ART,
		type ArtShape,
		type EmptyStateArt
	} from './emptyStateArt.generated';

	let {
		art,
		icon,
		heading,
		description,
		actionLabel,
		onaction,
		actionHref,
		testId
	}: {
		/** A generated illustration; preferred over `icon`. */
		art?: EmptyStateArt;
		/** A single emoji, shown large and decorative. Superseded by `art`. */
		icon?: string;
		heading: string;
		description?: string;
		actionLabel?: string;
		onaction?: () => void;
		actionHref?: string;
		/** `data-testid` for an e2e selector; the class is Svelte-scoped. */
		testId?: string;
	} = $props();

	const showAction = $derived(!!actionLabel && (!!onaction || !!actionHref));
	const shapes = $derived<ArtShape[]>(art ? EMPTY_STATE_ART[art] : []);
</script>

<div class="empty-state" data-testid={testId}>
	{#if art}
		<svg class="empty-state-art" viewBox={ART_VIEWBOX} aria-hidden="true" focusable="false">
			{#each shapes as shape, i (i)}
				{#if shape.el === 'rect'}
					<rect
						{...shape.attrs}
						class:tone-line={shape.tone === 'line'}
						class:tone-paper={shape.tone === 'paper'}
						class:tone-soft={shape.tone === 'soft'}
						class:tone-accent={shape.tone === 'accent'}
						class:tone-accent-soft={shape.tone === 'accent-soft'}
					/>
				{:else if shape.el === 'circle'}
					<circle
						{...shape.attrs}
						class:tone-line={shape.tone === 'line'}
						class:tone-paper={shape.tone === 'paper'}
						class:tone-soft={shape.tone === 'soft'}
						class:tone-accent={shape.tone === 'accent'}
						class:tone-accent-soft={shape.tone === 'accent-soft'}
					/>
				{:else}
					<path
						{...shape.attrs}
						class:tone-line={shape.tone === 'line'}
						class:tone-paper={shape.tone === 'paper'}
						class:tone-soft={shape.tone === 'soft'}
						class:tone-accent={shape.tone === 'accent'}
						class:tone-accent-soft={shape.tone === 'accent-soft'}
					/>
				{/if}
			{/each}
		</svg>
	{:else if icon}
		<span class="empty-state-icon" aria-hidden="true">{icon}</span>
	{/if}
	<p class="empty-state-heading">{heading}</p>
	{#if description}
		<p class="empty-state-description">{description}</p>
	{/if}
	{#if showAction}
		{#if actionHref}
			<a class="btn-primary empty-state-action" href={actionHref}>{actionLabel}</a>
		{:else}
			<button type="button" class="btn-primary empty-state-action" onclick={onaction}>
				{actionLabel}
			</button>
		{/if}
	{/if}
</div>

<style>
	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		text-align: center;
		gap: 8px;
		padding: 48px 20px;
	}
	.empty-state-art {
		width: 176px;
		height: auto;
		margin-bottom: 6px;
		overflow: visible;
		/* One-shot and short: it settles once when the empty state appears. The
		   reduced-motion rule in app.css lands it on its last frame. */
		animation: art-settle 0.6s var(--ease-out);
	}
	@keyframes art-settle {
		from { opacity: 0; transform: translate3d(0, 8px, 0) scale(0.97); }
		to { opacity: 1; transform: none; }
	}
	/* Colour is decided here, never in the generator: the palette's muted
	   stroke, a card-like fill, and the tenant accent. Decorative and
	   aria-hidden, so none of it is a WCAG 1.4.11 graphic. */
	.empty-state-art :global(*) {
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.tone-line {
		fill: none;
		stroke: #6b7082;
		stroke-width: 2;
	}
	.tone-paper {
		fill: #1f2230;
		stroke: #6b7082;
		stroke-width: 2;
	}
	.tone-soft {
		fill: rgba(226, 228, 234, 0.05);
		stroke: none;
	}
	.tone-accent {
		fill: none;
		stroke: var(--accent);
		stroke-width: 2.4;
	}
	.tone-accent-soft {
		fill: color-mix(in srgb, var(--accent) 22%, transparent);
		stroke: none;
	}
	.empty-state-icon {
		font-size: 2rem;
		line-height: 1;
		margin-bottom: 4px;
	}
	.empty-state-heading {
		margin: 0;
		font-size: 1.02rem;
		font-weight: 700;
		color: var(--text);
	}
	.empty-state-description {
		margin: 0;
		max-width: 30rem;
		font-size: 0.85rem;
		line-height: 1.5;
		color: var(--text-muted);
	}
	.empty-state-action {
		margin-top: 8px;
		text-decoration: none;
	}
</style>
