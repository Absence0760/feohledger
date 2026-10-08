<script lang="ts">
	/**
	 * The help centre's shell, shared by every /help page: one search box over
	 * the text, and the contents list (HelpNav) as a column beside it that stays
	 * in view while you read. Below 900px the contents fold behind a "Help
	 * contents" button under the search, and close again when you pick a page.
	 *
	 * Searching is live: typing on any help page goes to /help/search?q=…,
	 * replacing the history entry while you stay on the results, so Back leaves
	 * search rather than stepping through every keystroke.
	 */
	import type { Snippet } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { m, ensureHelpCatalogue } from '#lib/i18n/store.svelte.ts';
	import { MediaQuery } from 'svelte/reactivity';
	import HelpNav from '#lib/components/help/HelpNav.svelte';

	let { children }: { children: Snippet } = $props();

	// Every /help route reads the help-centre catalogue slice, which is not
	// part of the main catalogue (decisions §261); English shows until it lands.
	$effect(() => {
		void ensureHelpCatalogue();
	});

	const onSearch = $derived(page.url.pathname.replace(/\/$/, '') === '/help/search');
	let q = $state(page.url.searchParams.get('q') ?? '');
	let input: HTMLInputElement | undefined = $state();

	// Follow the URL — Back / Forward, a pasted search link — but never while
	// the reader is typing. Each keystroke navigates, and the URL lands a beat
	// behind the box (and trimmed): copying it back mid-typing ate the space
	// in "three way" and dropped characters typed faster than the navigation.
	$effect(() => {
		const urlQ = page.url.searchParams.get('q') ?? '';
		if (onSearch && document.activeElement !== input) q = urlQ;
	});

	// One history entry per search, not per keystroke. A keystroke's
	// navigation must land before the next is decided: replacing while the
	// first is still in flight replaced the page the search started from, so
	// Back skipped it. Keystrokes that arrive meanwhile collapse into one
	// follow-up navigation carrying the latest text.
	let inflight = false;
	let queued = false;

	async function search() {
		if (inflight) {
			queued = true;
			return;
		}
		const v = q.trim();
		if (!v && !onSearch) return;
		inflight = true;
		try {
			await goto(v ? `/help/search?q=${encodeURIComponent(v)}` : '/help/search', {
				replace: onSearch,
				reset: false
			});
		} finally {
			inflight = false;
		}
		if (queued) {
			queued = false;
			search();
		}
	}

	// One contents list in the document at a time, so its ids stay unique.
	const wide = new MediaQuery('min-width: 900px');

	let contentsOpen = $state(false);
	$effect(() => {
		void page.url.pathname;
		contentsOpen = false;
	});
</script>

<div class="help-shell">
	{#if wide.current}
		<!-- A plain box: HelpNav is the landmark (a named <nav>); an aside around
		     it gave screen readers two "Help contents" landmarks, one inside the other. -->
		<div class="help-side">
			<HelpNav />
		</div>
	{/if}
	<div class="help-col">
		<form role="search" class="help-search" onsubmit={(e) => (e.preventDefault(), search())}>
			<label for="help-q" class="visually-hidden">{m('help.search.label')}</label>
			<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
				><circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></svg
			>
			<input
				id="help-q"
				type="search"
				placeholder={m('help.search.placeholder')}
				bind:value={q}
				bind:this={input}
				oninput={search}
				autocomplete="off"
			/>
		</form>
		{#if !wide.current}
		<button
			type="button"
			class="btn-cancel contents-toggle"
			aria-expanded={contentsOpen}
			aria-controls="help-contents"
			onclick={() => (contentsOpen = !contentsOpen)}>{m('help.contents')}</button
		>
		<div id="help-contents" class="contents" class:open={contentsOpen}>
			<HelpNav />
		</div>
		{/if}
		<div class="help-main">
			{@render children()}
		</div>
	</div>
</div>

<style>
	/* The app's own page width (`.workspace` in app.css), anchored to the
	   sidebar: centring a narrower shell parked the contents column in mid-air,
	   and capping it short left dead space on the right. Prose keeps its own
	   reading measure inside this (a guide's text column caps at 50rem). */
	.help-shell {
		max-width: 1800px;
		margin: 0;
		padding: 28px 28px 64px;
	}
	.help-search {
		position: relative;
		max-width: 38rem;
	}
	.help-search svg {
		position: absolute;
		top: 50%;
		left: 12px;
		transform: translateY(-50%);
		color: var(--text-muted);
		pointer-events: none;
	}
	.help-search input {
		width: 100%;
		min-height: 42px;
		padding: 10px 12px 10px 40px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--surface);
		color: var(--text);
		font: inherit;
	}
	.contents-toggle {
		width: 100%;
		margin-top: 10px;
	}
	.contents {
		display: none;
		margin-top: 10px;
		padding-bottom: 12px;
		border-bottom: 1px solid var(--border);
	}
	.contents.open {
		display: block;
	}
	.help-main {
		min-width: 0;
		margin-top: 28px;
		container: help-main / inline-size;
	}
	@media (max-width: 700px) {
		.help-shell {
			padding: 20px 14px 48px;
		}
	}
	@media (min-width: 900px) {
		.help-shell {
			display: grid;
			grid-template-columns: 15rem minmax(0, 1fr);
			gap: 40px;
			align-items: start;
		}
		/* Placed, not auto-flowed: `wide` lands a moment after a resize. */
		.help-side {
			grid-column: 1;
			position: sticky;
			top: 20px;
			max-height: calc(100vh - 40px);
			overflow-y: auto;
			overscroll-behavior: contain;
			scrollbar-width: thin;
		}
		.help-col {
			grid-column: 2;
			min-width: 0;
		}
	}
</style>
