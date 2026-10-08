<script module lang="ts">
	import type { Guide } from '#lib/help/types.ts';

	// One fetch of the help centre's guide index, shared by every tip on the
	// page; a failed fetch is forgotten so the next open retries.
	let guidesFor: Promise<(id: string) => Guide[]> | undefined;
	function loadReading() {
		guidesFor ??= import('#lib/help/content.ts').then(
			(mod) => (id: string) => mod.guidesForTerm(id),
			(err: unknown) => {
				guidesFor = undefined;
				throw err;
			}
		);
		return guidesFor;
	}
</script>

<script lang="ts">
	/**
	 * ⓘ toggletip for an AP term, next to the label or column that uses it:
	 * `<HelpTip term="three-way-match" />`. `term` is a glossary id
	 * (`#lib/help/glossary.ts`; `lib/help/content.test.ts` fails on an unknown
	 * one anywhere in the tree). It shows the term's name and one-line
	 * definition, both from the message catalogue — so the tip is in the
	 * reader's language and carries no guide prose into the page — and links
	 * on to the full glossary entry and to up to three guides that explain the
	 * term. Those guides' titles live with the help centre's prose, so they are
	 * fetched the first time any tip opens (one shared dynamic import), never
	 * bundled with the page; until they arrive, or if the fetch fails, the tip
	 * shows the glossary link alone.
	 *
	 * A button, not a hover tooltip, so it works with the keyboard and on touch
	 * (WCAG 1.4.13, 2.1.1): click / Enter / Space toggles, Escape closes and
	 * keeps focus on the button, clicking or tabbing away closes; clicking
	 * inside the bubble (its text, its links) does not, because the bubble
	 * itself takes focus (`tabindex="-1"`). The term and definition are written
	 * into a persistent polite live region only once the popover is showing —
	 * text inserted into a region while it is still hidden is not announced —
	 * and the links stay out of that region, so opening a tip announces the
	 * definition once rather than the whole bubble twice.
	 *
	 * The bubble is a manual popover in the browser's top layer: no table's
	 * scroll container can clip it and no sticky header paints over it. It is
	 * placed from the button's rect — below, or above when there is no room —
	 * and follows it on scroll and resize.
	 */
	import { tick } from 'svelte';
	import { m, currentLocale, ensureHelpCatalogue } from '#lib/i18n/store.svelte.ts';
	import { termHref, termKey, termShortKey } from '#lib/help/terms.ts';

	let { term }: { term: string } = $props();

	// The term's name and definition live in the help-centre catalogue slice,
	// which loads on demand (decisions §261); English shows until it lands.
	$effect(() => {
		void ensureHelpCatalogue();
	});

	type Reading = { id: string; title: string }[];
	let reading = $state<Reading>([]);
	let readingFor = '';

	const name = $derived(m(termKey(term)));
	let open = $state(false);
	let pos = $state({ top: 0, left: 0 });
	let root: HTMLSpanElement | undefined = $state();
	let button: HTMLButtonElement | undefined = $state();
	let bubble: HTMLSpanElement | undefined = $state();
	let announcement = $state('');
	const bubbleId = `helptip-${Math.random().toString(36).slice(2, 9)}`;

	function close(refocus = false) {
		open = false;
		announcement = '';
		if (refocus) button?.focus();
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			// Don't let the same Escape also close an enclosing Modal.
			e.stopPropagation();
			e.preventDefault();
			close(true);
		}
	}

	// A native listener, not `onkeydown=`: Svelte 5 delegates template event
	// handlers to the document root, so a delegated stopPropagation() runs only
	// after the event has already bubbled through — and closed — an enclosing
	// dialog, whose focus trap listens on the dialog element itself.
	$effect(() => {
		const el = root;
		if (!el) return;
		el.addEventListener('keydown', onKeydown);
		return () => el.removeEventListener('keydown', onKeydown);
	});

	function onFocusOut(e: FocusEvent) {
		if (open && root && !root.contains(e.relatedTarget as Node | null)) close();
	}

	$effect(() => {
		if (!open) return;
		const onDoc = (e: PointerEvent) => {
			if (root && !root.contains(e.target as Node)) close();
		};
		document.addEventListener('pointerdown', onDoc);
		return () => document.removeEventListener('pointerdown', onDoc);
	});

	function place() {
		if (!button || !bubble) return;
		const b = button.getBoundingClientRect();
		const w = bubble.offsetWidth;
		const h = bubble.offsetHeight;
		const margin = 8;
		const gap = 6;
		const vw = document.documentElement.clientWidth;
		const vh = document.documentElement.clientHeight;
		const left = Math.min(Math.max(b.left + b.width / 2 - w / 2, margin), Math.max(vw - margin - w, margin));
		const below = b.bottom + gap;
		const top = below + h > vh - margin && b.top - gap - h >= margin ? b.top - gap - h : below;
		pos = { top, left };
	}

	$effect(() => {
		if (!open || readingFor === term) return;
		// A reused tip given a new term must not show the old term's guides.
		reading = [];
		readingFor = term;
		loadReading().then(
			(guidesFor) => {
				reading = guidesFor(term).map((g) => ({ id: g.id, title: g.title }));
				// The bubble just grew: keep it on screen.
				tick().then(place);
			},
			() => (readingFor = '')
		);
	});

	$effect(() => {
		if (!open) return;
		let alive = true;
		tick().then(() => {
			if (!alive || !bubble) return;
			bubble.showPopover();
			place();
			announcement = `${name}. ${m(termShortKey(term))}`;
		});
		window.addEventListener('scroll', place, true);
		window.addEventListener('resize', place);
		return () => {
			alive = false;
			window.removeEventListener('scroll', place, true);
			window.removeEventListener('resize', place);
		};
	});
</script>

<span class="helptip" bind:this={root} onfocusout={onFocusOut}>
	<button
		type="button"
		class="helptip-btn"
		bind:this={button}
		aria-label={m('help.tip.about', { term: name })}
		aria-expanded={open}
		aria-controls={bubbleId}
		onclick={() => (open = !open)}
		data-testid="help-tip"
	>
		<svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16">
			<circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.4" />
			<circle cx="8" cy="4.9" r="0.95" fill="currentColor" />
			<path d="M8 7.2v4.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
		</svg>
	</button>
	<span class="visually-hidden" role="status">{announcement}</span>
	{#if open}
		<span
			id={bubbleId}
			class="bubble"
			popover="manual"
			tabindex="-1"
			bind:this={bubble}
			style:top="{pos.top}px"
			style:left="{pos.left}px"
		>
			<!-- Read out through the live region above; hidden here so a reader
			     doesn't hear it twice. The links below are what Tab reaches. -->
			<strong class="term" aria-hidden="true">{name}</strong>
			<span class="short" aria-hidden="true">{m(termShortKey(term))}</span>
			<a class="more" href={termHref(term)}>{m('help.tip.more')}</a>
			{#if reading.length}
				<span class="reading">
					<span class="reading-label">{m('help.tip.furtherReading')}</span>
					{#each reading as g (g.id)}
						<a class="more" href="/help/guides/{g.id}" lang={currentLocale() === 'en' ? undefined : 'en'}>{g.title}</a>
					{/each}
				</span>
			{/if}
		</span>
	{/if}
</span>

<style>
	.helptip {
		position: relative;
		display: inline-flex;
		vertical-align: middle;
	}
	/* 24×24 target (WCAG 2.5.8) around a 16px glyph. */
	.helptip-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 24px;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
	}
	.helptip-btn:hover,
	.helptip-btn[aria-expanded='true'] {
		color: var(--accent);
		background: color-mix(in srgb, var(--accent) 14%, transparent);
	}
	.bubble {
		position: fixed;
		inset: auto;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		width: max-content;
		max-width: min(20rem, calc(100vw - 16px));
		padding: 0.65rem 0.8rem;
		background: var(--surface);
		color: var(--text);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		box-shadow: var(--shadow-float);
		font-size: 0.85rem;
		font-weight: 400;
		line-height: 1.45;
		text-align: left;
		white-space: normal;
		text-transform: none;
		letter-spacing: normal;
	}
	/* Author styles beat the UA's popover hiding rule, so restate it. */
	.bubble:focus {
		outline: none;
	}
	.bubble:not(:popover-open) {
		display: none;
	}
	.term {
		font-weight: 700;
	}
	.reading {
		display: flex;
		flex-direction: column;
		gap: 0;
		margin-top: 4px;
		padding-top: 6px;
		border-top: 1px solid var(--border);
	}
	.reading-label {
		color: var(--text-muted);
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.05em;
		text-transform: uppercase;
	}
	/* Each link a 24px-tall target (WCAG 2.5.8), however tightly they stack. */
	.more {
		display: flex;
		align-items: center;
		min-height: 24px;
		font-size: 0.8rem;
		color: var(--accent);
		text-decoration: underline;
	}
</style>
