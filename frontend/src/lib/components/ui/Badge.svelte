<script lang="ts">
	import type { Snippet } from 'svelte';

	// Deliberately NOT re-exported from here: the type lives in a `.ts` module
	// so plain `tsc` (the `tests-e2e/` typecheck) can resolve it, and a
	// re-export would invite the next importer back onto the path that cannot.
	import type { BadgeTone } from './badgeTone';

	/**
	 * The shared tinted-badge primitive — ONE owner of the
	 * `background: var(--<tone>-tint); color: var(--<tone>-on-tint)` recipe.
	 *
	 * Why it exists: 202 CSS rules across the app hand-rolled that recipe as an
	 * `rgba()` plus a literal hex — 44 distinct spellings of these five tones.
	 * Every one of them passed the contrast guard, so this is design-system
	 * debt rather than a defect; but the same tone written four ways is exactly
	 * how the 29 sub-4.5:1 badges accumulated unnoticed before decisions.md §30
	 * fixed them. A caller that names a *tone* can't spell it wrong, and a tone
	 * that is later recalibrated moves in one place.
	 *
	 * `variant` is passed straight through as an extra class so a call site
	 * keeps its own semantic hook (`.badge.approved`, `.badge.violation`) — the
	 * e2e suite selects on those, and they stay meaningful as selectors even
	 * though their *colour* rules are retired when a site converts. Do not
	 * reintroduce colour rules for a variant in the calling component: pick the
	 * tone here instead.
	 *
	 * Sizing is fixed on purpose. Call sites varied padding by a pixel or two
	 * with no intent behind the difference; one size is the point of a shared
	 * primitive. A badge that genuinely needs different metrics (a dense table
	 * chip, an oversized hero pill) is a different component, not a prop.
	 */
	let {
		tone = 'neutral',
		variant = '',
		title,
		children
	}: {
		tone?: BadgeTone;
		/** Extra class for e2e / semantic hooks. Never for colour. */
		variant?: string;
		title?: string;
		children: Snippet;
	} = $props();
</script>

<span class="badge {tone} {variant}" {title}>{@render children()}</span>

<style>
	/* WCAG 1.4.3 — small but bold uppercase text, so held to the normal-text
	   bar (≥4.5:1), not the large-text one. Every pair below is asserted
	   directly by `src/lib/a11y/tokenPairing.test.ts` over both backdrops a
	   badge sits on (`--bg` and `--surface`). */
	.badge {
		display: inline-block;
		padding: 3px 10px;
		border-radius: 12px;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		white-space: nowrap;
	}

	.accent {
		background: var(--accent-tint);
		color: var(--accent-on-tint);
	}

	.success {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.warning {
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.danger {
		background: var(--danger-tint);
		color: var(--danger-on-tint);
	}

	.muted {
		background: var(--muted-tint);
		color: var(--muted-on-tint);
	}

	/* Flat, untinted — the "no signal" chip. */
	.neutral {
		background: var(--bg);
		color: var(--text-muted);
	}

	/* The measured literal. #a585f5 on the 15% tint composites to 5.59:1. */
	.erp {
		background: rgba(140, 100, 240, 0.15);
		color: #a585f5;
	}
</style>
