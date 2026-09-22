<script lang="ts">
	import { page } from '$app/state';

	/**
	 * Vertical, grouped, URL-backed section picker for a settings page.
	 *
	 * The third navigation primitive in the app, and the three do not overlap:
	 *  - `layout/SectionTabs.svelte` moves between ROUTES in a nav group, driven
	 *    by `lib/nav.ts`.
	 *  - `ui/Tabs.svelte` is a horizontal `role="tablist"` over LOCAL state, for
	 *    a handful of views of one dataset (`/expenses`, `/payments`, `/audit`).
	 *  - this moves between SECTIONS of one settings page, and the section is in
	 *    the URL so it can be linked to, bookmarked and pointed at from a doc.
	 *
	 * Anchors with `aria-current`, not `role="tab"` buttons — the same call
	 * `SectionTabs` made, and for the same reason: the destination is an address,
	 * so it should be a link, and a link needs no roving-tabindex arrow-key
	 * contract to get right. `Tabs.svelte` owns that pattern for the cases that
	 * genuinely are local view state.
	 *
	 * The caller owns which section is active (it reads `?section=` itself, so it
	 * can validate the slug and fall back). This component only renders.
	 */

	type RailItem = {
		/** `?section=` value. Stable — it appears in bookmarks and docs. */
		slug: string;
		label: string;
	};

	type RailGroup = {
		/** Omit for a flat rail with no group headings. */
		label?: string;
		items: RailItem[];
	};

	type Props = {
		groups: RailGroup[];
		/** Active slug. The caller has already validated it. */
		active: string;
		/**
		 * Accessible name for the nav, and the prefix on the narrow-viewport
		 * disclosure ("Settings section: Branding"). Passed in rather than read
		 * from a message key so the component stays reusable across pages with
		 * their own catalogues — the same choice `SecretReveal` makes.
		 */
		label: string;
	};

	let { groups, active, label }: Props = $props();

	// Per-instance, not a literal: `aria-controls` has to point at a unique id,
	// and a hardcoded one silently breaks the moment a page renders two rails.
	// `$props.id()` has to be a bare declaration initializer, so the readable
	// prefix goes on separately rather than inside the template literal.
	const uid = $props.id();
	const listId = `settings-rail-${uid}`;

	let open = $state(false);

	let items = $derived(groups.flatMap((g) => g.items));
	let activeLabel = $derived(items.find((i) => i.slug === active)?.label ?? '');

	/**
	 * Same path, `section` swapped, every other query parameter kept — a page
	 * carrying its own state in the URL (an entity filter, say) must not lose it
	 * on a section change. Returning only the query string keeps the href
	 * relative, and drops any stale `#hash` along with it.
	 */
	function href(slug: string): string {
		const params = new URLSearchParams(page.url.search);
		params.set('section', slug);
		return `?${params.toString()}`;
	}

	// Choosing a section on a phone should close the picker behind you.
	$effect(() => {
		void page.url.href;
		open = false;
	});
</script>

<nav class="rail" aria-label={label}>
	<!-- Narrow-viewport disclosure. `display: none` above the breakpoint takes it
	     out of the tab order and the a11y tree, so the wide layout is a plain
	     list of links with nothing to expand. -->
	<button
		type="button"
		class="rail-toggle"
		aria-expanded={open}
		aria-controls={listId}
		onclick={() => (open = !open)}
	>
		<span class="rail-toggle-label">{label}</span>
		<span class="rail-toggle-current">{activeLabel}</span>
		<svg
			class="rail-chevron"
			class:flipped={open}
			width="14"
			height="14"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2.5"
			aria-hidden="true"
		>
			<polyline points="6 9 12 15 18 9" />
		</svg>
	</button>

	<div class="rail-groups" class:open id={listId}>
		{#each groups as group, gi (group.label ?? gi)}
			<div class="rail-group">
				{#if group.label}
					<!-- Deliberately not a heading. The panels these link to use `<h2>`
					     for their own titles, so a heading here would interleave with
					     the document outline a screen-reader user navigates by — and
					     the e2e suite selects panels by heading role and name. The
					     label is hidden from assistive tech and re-attached as the
					     list's accessible name instead, so it is announced once, as
					     what it is: the name of a group of links. -->
					<p class="rail-group-label" aria-hidden="true">{group.label}</p>
				{/if}
				<ul aria-label={group.label}>
					{#each group.items as item (item.slug)}
						<li>
							<a
								href={href(item.slug)}
								class="rail-link"
								class:active={item.slug === active}
								aria-current={item.slug === active ? 'page' : undefined}
								data-section-link={item.slug}
							>
								{item.label}
							</a>
						</li>
					{/each}
				</ul>
			</div>
		{/each}
	</div>
</nav>

<style>
	.rail {
		min-width: 0;
	}

	/* ---------------------------- narrow screens --------------------------- */

	.rail-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 12px 14px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.9rem;
		text-align: left;
		cursor: pointer;
	}

	.rail-toggle:hover,
	.rail-toggle:focus-visible {
		border-color: var(--accent);
	}

	.rail-toggle-label {
		color: var(--text-muted);
		font-size: 0.8rem;
	}

	.rail-toggle-current {
		flex: 1;
		font-weight: 600;
	}

	.rail-chevron {
		flex: none;
		transition: transform 0.15s var(--ease-out);
	}

	.rail-chevron.flipped {
		transform: rotate(180deg);
	}

	/* Collapsed by default, so a phone opens on the panel rather than on a
	   20-row menu. Toggled by class rather than `<details>`: a closed
	   `<details>` does not render its children in every engine we support, so
	   the wide layout could not reliably force them visible again with CSS. */
	.rail-groups {
		display: none;
		margin-top: 8px;
	}

	.rail-groups.open {
		display: block;
	}

	/* ----------------------------- wide screens ---------------------------- */

	/* 60rem is where the 46rem-ish panel column plus a 200px rail first fit
	   side by side without squeezing the forms. */
	@media (min-width: 60rem) {
		.rail-toggle {
			display: none;
		}

		.rail-groups {
			display: block;
			margin-top: 0;
			/* Sticky is safe here, unlike `SectionTabs` — that one is a horizontal
			   strip and pinning it covered whatever the browser scrolled into view
			   just beneath it. A column beside the content covers nothing.
			   `overflow-y: auto` needs no `tabindex="0"`: the region is nothing but
			   links, so it is already keyboard-scrollable and axe's
			   `scrollable-region-focusable` is satisfied by its focusable content. */
			position: sticky;
			top: 24px;
			max-height: calc(100vh - 48px);
			overflow-y: auto;
			padding-right: 4px;
		}
	}

	.rail-group + .rail-group {
		margin-top: 18px;
	}

	.rail-group-label {
		margin: 0 0 6px;
		padding: 0 10px;
		color: var(--text-muted);
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.rail-group ul {
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.rail-link {
		display: block;
		padding: 7px 10px;
		border-radius: 7px;
		border-left: 2px solid transparent;
		color: var(--text-muted);
		font-size: 0.9rem;
		font-weight: 500;
		text-decoration: none;
		transition: color 0.15s, background 0.15s;
	}

	.rail-link:hover {
		/* Same literal as `Sidebar.nav-item:hover` and `SectionTabs`: this is a
		   nav row, so it matches the chrome it belongs to. `--surface-hover` does
		   not exist — a dead token with a fallback is what `a11y/tokenPairing`
		   flags. */
		background: rgba(99, 140, 255, 0.08);
		color: var(--text);
	}

	.rail-link.active {
		background: rgba(99, 140, 255, 0.08);
		border-left-color: var(--accent);
		color: var(--text);
		font-weight: 600;
	}
</style>
