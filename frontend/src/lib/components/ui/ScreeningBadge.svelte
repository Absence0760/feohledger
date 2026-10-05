<script lang="ts">
	// Small pill for a vendor's sanctions-screening status and/or risk level.
	// Pass `screening` (clear=green, review=amber, match=red, unscreened=grey)
	// and/or `risk` (low=grey, medium=amber, high/critical=red). `blocked`
	// forces the screening pill red with a "Blocked" label. `adverseMedia` adds
	// an amber "Negative news" pill — a different instruction to a reviewer
	// than a watchlist match, so it reads alongside the verdict rather than
	// replacing it. Shared by the vendor list cell, the screening review queue
	// and the detail modal — don't hand-roll these pills (the tone classes
	// carry calibrated WCAG-checked text colours; see `docs/accessibility.md`).
	import {
		riskLevelLabelKey,
		screeningStatusLabelKey,
		type ScreeningStatus,
		type RiskLevel
	} from '#lib/types/vendor.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		screening,
		risk,
		blocked = false,
		adverseMedia = false
	}: {
		screening?: ScreeningStatus;
		risk?: RiskLevel;
		blocked?: boolean;
		adverseMedia?: boolean;
	} = $props();

	// Map status/level to a tone class shared across both pills.
	function screeningTone(s: ScreeningStatus, isBlocked: boolean): string {
		if (isBlocked) return 'red';
		if (s === 'clear') return 'green';
		if (s === 'review') return 'amber';
		if (s === 'match') return 'red';
		return 'grey';
	}

	// Verdict and risk level are message keys, not English literals — an
	// unrecognised value renders raw rather than blank.
	function screeningLabel(s: ScreeningStatus): string {
		const key = screeningStatusLabelKey(s);
		return key ? m(key) : s;
	}

	// The pill and its tooltip are COMPOSED strings — `{level} risk` in
	// English, `Risiko: {level}` in German — so each is its own key with the
	// level interpolated. Concatenating a translated level onto an English
	// word here is exactly the mixed-language pill this badge had.
	function riskLabel(r: RiskLevel): string {
		const key = riskLevelLabelKey(r);
		return key ? m(key) : r;
	}

	function riskTone(r: RiskLevel): string {
		if (r === 'low') return 'grey';
		if (r === 'medium') return 'amber';
		if (r === 'high' || r === 'critical') return 'red';
		return 'grey';
	}
</script>

{#if screening}
	<span class="screen-badge {screeningTone(screening, blocked)}">
		{blocked ? m('vendors.screening.blocked') : screeningLabel(screening)}
	</span>
{/if}
{#if risk && risk !== 'unknown'}
	<span
		class="screen-badge risk {riskTone(risk)}"
		title={m('vendors.risk.title', { level: riskLabel(risk) })}
	>
		{m('vendors.risk.pill', { level: riskLabel(risk) })}
	</span>
{/if}
{#if adverseMedia}
	<span class="screen-badge amber" title={m('vendors.screening.adverseMediaTitle')}>
		{m('vendors.screening.adverseMedia')}
	</span>
{/if}

<style>
	.screen-badge {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 10px;
		font-size: 0.72rem;
		font-weight: 500;
		white-space: nowrap;
	}
	.screen-badge.risk {
		font-size: 0.68rem;
	}
	/* Tones come from the palette pairs, not from private literals — the same
	   values `ui/Badge.svelte` renders. This pill keeps its own *metrics*
	   (smaller, sentence-case, weight 500) on purpose: it sits inline in a
	   dense table cell beside a vendor name, where the uppercase status pill
	   would shout. Metrics are the reason it isn't a `<Badge>`; colour never
	   was, and three private spellings of green/amber/red are gone. */
	.screen-badge.green {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}
	.screen-badge.amber {
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}
	.screen-badge.red {
		background: var(--danger-tint);
		color: var(--danger-on-tint);
	}
	.screen-badge.grey {
		background: var(--bg);
		color: var(--text-muted);
	}
</style>
