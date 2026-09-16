<script lang="ts">
	import { page } from '$app/state';
	import { auth } from '$lib/stores/auth.svelte';
	import { groupForPath, sectionTabActive, visibleChildren, type NavChild } from '$lib/nav';
	import { m } from '$lib/i18n/store.svelte';

	// The grouped section (Procurement / Billing / Automation / Governance /
	// Settings / Insights) that owns the current route, if any. Top-level links
	// (Invoices, Payments, …) return null → no sub-tab bar. Children the current
	// role can't see are dropped.
	let group = $derived(groupForPath(page.url.pathname));
	let tabs = $derived(
		group ? visibleChildren(group, auth.hasAnyRole.bind(auth), auth.can.bind(auth)) : []
	);
	let activeIndex = $derived(tabs.findIndex((t) => sectionTabActive(t, tabs, page.url)));

	/** Must match the `gap` on `.section-tabs-row` / `.section-tabs-measure`. */
	const GAP = 4;

	let rowEl = $state<HTMLElement | undefined>(undefined);
	let measureEl = $state<HTMLElement | undefined>(undefined);
	let moreEl = $state<HTMLElement | undefined>(undefined);
	let moreBtn = $state<HTMLButtonElement | undefined>(undefined);
	let availW = $state(0);
	let widths = $state<number[]>([]);
	let moreW = $state(0);
	let open = $state(false);

	// Re-measure when a label's TEXT changes, not just when the tab list does —
	// switching locale keeps the same hrefs but can change every width.
	let labelSig = $derived(tabs.map((t) => m(t.labelKey)).join('\u0000'));

	/**
	 * Which tabs fit on one row, and which fall into the More menu.
	 *
	 * Before the first measurement (`widths` empty, or stale after the tab list
	 * changed) everything renders inline; `.section-tabs-row` is `overflow:
	 * hidden`, so that pre-measure frame clips instead of widening the document.
	 */
	/**
	 * True once the off-screen row has been measured for THIS tab set, so the
	 * split below is real rather than the pre-measure "render everything" pass.
	 * Exposed as `data-tabs-ready` because it is the only honest signal that the
	 * bar has settled: until it flips, a tab may be in the row one frame and in
	 * the More menu the next.
	 */
	let measured = $derived(widths.length === tabs.length && availW > 0);

	let split = $derived.by((): { visible: NavChild[]; overflow: NavChild[] } => {
		if (!measured) {
			return { visible: tabs, overflow: [] };
		}
		const total = widths.reduce((a, b) => a + b, 0) + GAP * Math.max(0, tabs.length - 1);
		if (total <= availW) return { visible: tabs, overflow: [] };

		// Room for the More button has to come out of the budget first.
		const budget = availW - moreW - GAP;
		let used = 0;
		let n = 0;
		for (let i = 0; i < tabs.length; i++) {
			const w = widths[i] + (n > 0 ? GAP : 0);
			if (used + w > budget) break;
			used += w;
			n++;
		}
		if (n < 1) n = 1; // never render an empty row

		let visible = tabs.slice(0, n);
		// The active tab always stays in the row: a section whose current page is
		// only reachable by opening a menu reads as "nothing is selected".
		if (activeIndex >= n) {
			visible = [...tabs.slice(0, Math.max(0, n - 1)), tabs[activeIndex]];
		}
		const shown = new Set(visible);
		return { visible, overflow: tabs.filter((t) => !shown.has(t)) };
	});

	$effect(() => {
		const el = rowEl;
		if (!el) return;
		const ro = new ResizeObserver(() => (availW = el.clientWidth));
		ro.observe(el);
		availW = el.clientWidth;
		return () => ro.disconnect();
	});

	$effect(() => {
		// `labelSig` is read so a locale switch re-measures (see above).
		void labelSig;
		const el = measureEl;
		if (!el) return;
		widths = Array.from(el.querySelectorAll<HTMLElement>('[data-measure-tab]')).map(
			(n) => n.getBoundingClientRect().width
		);
		moreW = el.querySelector<HTMLElement>('[data-measure-more]')?.getBoundingClientRect().width ?? 0;
	});

	// Navigating from inside the menu should leave it closed.
	$effect(() => {
		void page.url.href;
		open = false;
	});

	$effect(() => {
		if (!open) return;
		const onPointerDown = (e: PointerEvent) => {
			if (!moreEl?.contains(e.target as Node)) open = false;
		};
		const onKeyDown = (e: KeyboardEvent) => {
			if (e.key === 'Escape') {
				open = false;
				moreBtn?.focus();
			}
		};
		document.addEventListener('pointerdown', onPointerDown);
		document.addEventListener('keydown', onKeyDown);
		return () => {
			document.removeEventListener('pointerdown', onPointerDown);
			document.removeEventListener('keydown', onKeyDown);
		};
	});
</script>

<!-- Only show the bar when the section actually offers a choice. A lone
     accessible tab (e.g. a CFO's Settings = just Organization) would just
     duplicate the page title, so we suppress it. -->
{#if group && tabs.length > 1}
	<nav
		class="section-tabs"
		aria-label={m('shell.sectionNav', { group: m(group.labelKey) })}
		data-tabs-ready={measured ? 'true' : 'false'}
	>
		<div class="section-tabs-inner">
			<div class="section-tabs-row" bind:this={rowEl}>
				{#each split.visible as tab (tab.href)}
					{@const active = sectionTabActive(tab, tabs, page.url)}
					<a
						href={tab.href}
						class="section-tab"
						class:active
						aria-current={active ? 'page' : undefined}
					>
						{m(tab.labelKey)}
					</a>
				{/each}
			</div>

			{#if split.overflow.length > 0}
				<div class="section-more" bind:this={moreEl}>
					<button
						type="button"
						class="section-tab section-more-btn"
						bind:this={moreBtn}
						aria-expanded={open}
						aria-haspopup="true"
						aria-controls="section-more-menu"
						aria-label={m('shell.sectionMoreAria', { count: String(split.overflow.length) })}
						onclick={() => (open = !open)}
					>
						{m('shell.sectionMore')}
						<svg
							width="12"
							height="12"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2.5"
							aria-hidden="true"
						>
							<polyline points="6 9 12 15 18 9" />
						</svg>
					</button>
					{#if open}
						<div class="section-more-menu" id="section-more-menu">
							{#each split.overflow as tab (tab.href)}
								{@const active = sectionTabActive(tab, tabs, page.url)}
								<a
									href={tab.href}
									class="section-more-item"
									class:active
									aria-current={active ? 'page' : undefined}
								>
									{m(tab.labelKey)}
								</a>
							{/each}
						</div>
					{/if}
				</div>
			{/if}
		</div>

		<!--
			Off-screen copy used only to read each tab's intrinsic width, so the
			split can be computed without ever rendering an over-wide row. Spans,
			not anchors: focusable content inside `aria-hidden` is reachable by
			keyboard while hidden from assistive tech, which is its own defect.
		-->
		<div class="section-tabs-measure-clip">
			<div class="section-tabs-measure" bind:this={measureEl} aria-hidden="true">
				{#each tabs as tab (tab.href)}
					<span class="section-tab" data-measure-tab>{m(tab.labelKey)}</span>
				{/each}
				<span class="section-tab section-more-btn" data-measure-more>
					{m('shell.sectionMore')}
					<svg width="12" height="12" viewBox="0 0 24 24" aria-hidden="true"></svg>
				</span>
			</div>
		</div>
	</nav>
{/if}

<style>
	/* Secondary nav strip above the page content. Full-width border, inner
	   content aligned to the same 1800px / 20px gutter as `.workspace` so the
	   tabs line up with the page title below. */
	.section-tabs {
		border-bottom: 1px solid var(--border);
		background: var(--surface);
		position: relative;
	}

	.section-tabs-inner {
		max-width: 1800px;
		margin: 0 auto;
		padding: 0 20px;
		display: flex;
		align-items: stretch;
	}

	/* `min-width: 0` lets this flex child shrink below its content width, and
	   `overflow: hidden` is the hard backstop: whatever the measurement says,
	   the row can never widen the document into a horizontal scrollbar
	   (WCAG 1.4.10 — the whole page used to scroll sideways here). */
	.section-tabs-row {
		display: flex;
		gap: 4px;
		min-width: 0;
		flex: 1;
		overflow: hidden;
	}

	.section-tab {
		padding: 12px 16px;
		color: var(--text-muted);
		font-size: 0.9rem;
		font-weight: 500;
		text-decoration: none;
		border-bottom: 2px solid transparent;
		margin-bottom: -1px;
		white-space: nowrap;
	}

	.section-tab:hover {
		color: var(--text);
	}

	.section-tab.active {
		color: var(--accent);
		border-bottom-color: var(--accent);
	}

	.section-more {
		position: relative;
		flex: none;
	}

	.section-more-btn {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		font-family: inherit;
		cursor: pointer;
	}

	.section-more-menu {
		position: absolute;
		top: 100%;
		right: 0;
		z-index: 40;
		min-width: 200px;
		padding: 6px;
		display: flex;
		flex-direction: column;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		box-shadow: 0 8px 24px rgb(0 0 0 / 0.25);
	}

	.section-more-item {
		padding: 8px 12px;
		border-radius: 6px;
		color: var(--text-muted);
		font-size: 0.9rem;
		font-weight: 500;
		text-decoration: none;
		white-space: nowrap;
	}

	.section-more-item:hover {
		/* Same value as `Sidebar.nav-item:hover` / `NotificationBell.bell-item:hover`
		   — this is a nav row in a popover, so it matches the chrome it belongs
		   to rather than inventing a token. `--surface-hover` does not exist;
		   using it with a fallback is a dead token and `a11y/tokenPairing` says
		   so. */
		background: rgba(99, 140, 255, 0.08);
		color: var(--text);
	}

	.section-more-item.active {
		color: var(--accent);
	}

	/* Zero-height, clipping wrapper. The measurement row is deliberately WIDER
	   than the viewport, and `position: absolute` + `visibility: hidden` does
	   NOT take an element out of the document's scrollable area — so without
	   this the measurer reintroduced the very horizontal scrollbar the
	   component exists to remove (372px of it at a 1000px viewport). Clipping
	   affects painting, not layout, so the children still report real widths. */
	.section-tabs-measure-clip {
		position: absolute;
		top: 0;
		left: 0;
		width: 100%;
		height: 0;
		overflow: hidden;
	}

	/* Measured, never seen. `visibility: hidden` (not `display: none`) so the
	   browser still lays it out and reports real widths. */
	.section-tabs-measure {
		position: absolute;
		top: 0;
		left: 0;
		display: flex;
		gap: 4px;
		visibility: hidden;
		pointer-events: none;
		white-space: nowrap;
	}
</style>
