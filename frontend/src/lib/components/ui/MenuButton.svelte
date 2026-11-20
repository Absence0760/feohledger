<script lang="ts" generics="T extends string">
	import { tick, type Snippet } from 'svelte';
	import {
		clipBounds,
		menuKeyTarget,
		placeMenu,
		triggerKeyAction,
		typeaheadTarget,
		type MenuPlacement
	} from '#lib/utils/menuButton.ts';

	/**
	 * A button that opens a menu of actions — the WAI-ARIA APG menu button
	 * (`frontend/docs/component-library.md` § ui, `ui-patterns.md` § Menu
	 * button). Use it to group a handful of related ACTIONS behind one
	 * trigger (Export → CSV / JSON / XML). A choice of value is a `<select>`
	 * or `SegmentedControl`, and a list of destinations is links, not this.
	 *
	 * Keyboard: Enter / Space / ArrowDown on the trigger open it on the first
	 * item, ArrowUp on the last; inside, the arrows move (wrapping), Home / End
	 * jump, a letter jumps to the next item starting with it, Enter / Space
	 * choose, Escape closes and returns focus to the trigger, and Tab closes
	 * and lets focus move on. A click outside closes it.
	 *
	 * Labels arrive already localized (the component is i18n-agnostic, like
	 * `SegmentedControl`).
	 */
	let {
		label,
		items,
		onselect,
		disabled = false,
		busy = false,
		variant = 'secondary',
		icon,
		menuLabel,
		testId,
		menuTestId
	}: {
		/** The trigger's text, which is also its accessible name. */
		label: string;
		/** `testId` becomes the item's `data-testid`. */
		items: readonly { value: T; label: string; testId?: string }[];
		onselect: (value: T) => void;
		disabled?: boolean;
		/** The action it ran is in flight. The trigger refuses to open and
		 *  announces `aria-busy`, spelled `aria-disabled` rather than `disabled`:
		 *  choosing an item returns focus to the trigger, and a natively disabled
		 *  button would drop that focus to `<body>` (2.4.3), as `RowAction busy`
		 *  avoids for the same reason. */
		busy?: boolean;
		/** Which shared button class the trigger wears (`btn-<variant>`). */
		variant?: 'secondary' | 'cancel' | 'ghost';
		/** An icon drawn before the label (mark it `aria-hidden`). */
		icon?: Snippet;
		/** The menu's accessible name; defaults to the trigger naming it. */
		menuLabel?: string;
		/** `data-testid` on the trigger; the menu gets `<testId>-menu`. */
		testId?: string;
		/** Overrides the menu's `data-testid` (a caller keeping an older selector). */
		menuTestId?: string;
	} = $props();

	const uid = $props.id();
	const triggerId = `${uid}-trigger`;
	const menuId = `${uid}-menu`;

	let open = $state(false);
	let active = $state(-1);
	let placement = $state<MenuPlacement>({ side: 'below', shiftX: 0 });
	let root: HTMLDivElement | undefined = $state();
	let trigger: HTMLButtonElement | undefined = $state();
	let menu: HTMLDivElement | undefined = $state();
	const itemEls: HTMLButtonElement[] = $state([]);

	// Disabled or busy: either way it does not open.
	const unavailable = $derived(disabled || busy);

	async function openMenu(focus: 'first' | 'last') {
		if (unavailable || items.length === 0) return;
		placement = { side: 'below', shiftX: 0 };
		open = true;
		await tick();
		place();
		focusItem(focus === 'first' ? 0 : items.length - 1);
	}

	function closeMenu(returnFocus: boolean) {
		if (!open) return;
		open = false;
		active = -1;
		if (returnFocus) trigger?.focus();
	}

	function focusItem(i: number) {
		active = i;
		itemEls[i]?.focus();
	}

	/**
	 * Keep the open menu inside what is visible of it (flip above, shift
	 * sideways): the viewport, cut down to the nearest scrolling / clipping
	 * ancestor — inside InvoiceModal's `.form-scroll` the window is not what
	 * clips it (`clipBounds`).
	 */
	function place() {
		if (!trigger || !menu) return;
		const t = trigger.getBoundingClientRect();
		const bounds = clipBounds(
			menu,
			{ width: document.documentElement.clientWidth, height: window.innerHeight },
			(node) => getComputedStyle(node as Element)
		);
		placement = placeMenu(t, { width: menu.offsetWidth, height: menu.offsetHeight }, bounds);
	}

	function choose(value: T) {
		closeMenu(true);
		onselect(value);
	}

	function onTriggerKeydown(e: KeyboardEvent) {
		if (open) return;
		const where = triggerKeyAction(e.key);
		if (!where) return;
		e.preventDefault();
		void openMenu(where);
	}

	function onMenuKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			// Stop here so an enclosing Modal does not close on the same press.
			e.preventDefault();
			e.stopPropagation();
			closeMenu(true);
			return;
		}
		if (e.key === 'Tab') {
			// Close, and let the browser move focus on from the trigger's place.
			closeMenu(false);
			return;
		}
		const next =
			menuKeyTarget(e.key, active, items.length) ??
			typeaheadTarget(
				e.key,
				active,
				items.map((i) => i.label)
			);
		if (next === null) return;
		e.preventDefault();
		focusItem(next);
	}

	// The menu's keys are heard on the menu itself, natively, not through
	// Svelte's delegated `onkeydown` (which runs at the app root): an enclosing
	// dialog's focus trap listens on the dialog node, BETWEEN the menu and the
	// root, so a delegated handler's `stopPropagation()` on Escape came after the
	// trap had already closed the dialog.
	$effect(() => {
		const el = menu;
		if (!el) return;
		el.addEventListener('keydown', onMenuKeydown);
		return () => el.removeEventListener('keydown', onMenuKeydown);
	});

	$effect(() => {
		if (!open) return;
		const onPointerDown = (e: PointerEvent) => {
			if (root && !root.contains(e.target as Node)) closeMenu(false);
		};
		const onResize = () => place();
		document.addEventListener('pointerdown', onPointerDown, true);
		window.addEventListener('resize', onResize);
		return () => {
			document.removeEventListener('pointerdown', onPointerDown, true);
			window.removeEventListener('resize', onResize);
		};
	});

	// A trigger disabled or busy while open takes the menu with it.
	$effect(() => {
		if (unavailable && open) closeMenu(false);
	});
</script>

<div class="menu-button" bind:this={root}>
	<button
		bind:this={trigger}
		id={triggerId}
		type="button"
		class="btn-{variant} menu-trigger"
		aria-haspopup="menu"
		aria-expanded={open}
		aria-controls={open ? menuId : undefined}
		data-testid={testId}
		{disabled}
		aria-disabled={busy || undefined}
		aria-busy={busy || undefined}
		class:busy
		onclick={() => (open ? closeMenu(true) : void openMenu('first'))}
		onkeydown={onTriggerKeydown}
	>
		{@render icon?.()}
		{label}
		<svg
			class="menu-caret"
			class:flipped={open}
			width="12"
			height="12"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2.5"
			aria-hidden="true"><polyline points="6 9 12 15 18 9" /></svg
		>
	</button>
	{#if open}
		<div
			bind:this={menu}
			id={menuId}
			class="menu"
			class:above={placement.side === 'above'}
			style:translate="{placement.shiftX}px 0"
			role="menu"
			tabindex="-1"
			aria-label={menuLabel}
			aria-labelledby={menuLabel ? undefined : triggerId}
			data-testid={menuTestId ?? (testId ? `${testId}-menu` : undefined)}
		>
			{#each items as item, i (item.value)}
				<button
					bind:this={itemEls[i]}
					type="button"
					class="menu-item"
					role="menuitem"
					tabindex="-1"
					data-testid={item.testId}
					onclick={() => choose(item.value)}
					onfocus={() => (active = i)}
				>
					{item.label}
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	.menu-button {
		position: relative;
		display: inline-flex;
	}

	.menu-caret {
		transition: transform 0.15s ease;
	}

	/* Busy reads as the shared buttons' disabled look (app.css), with the
	   progress cursor: it is waiting, not refused. */
	.menu-trigger.busy {
		border-color: var(--border);
		background: var(--surface);
		color: var(--text-muted);
		cursor: progress;
	}

	.menu-caret.flipped {
		transform: rotate(180deg);
	}

	/* Colours (dark / light, computed with `a11y/contrast.ts`):
	   item --text on --surface 13.64 / 17.36:1; the focused item --text on
	   --surface-2 11.01 / 14.68:1; the --accent focus ring against --surface-2
	   4.48 / 5.87:1 (1.4.11 needs 3:1). The --border-strong edge against
	   --surface (1.64 / 1.90:1) is decorative: the shadow and the item text
	   carry the popover's boundary, as for every other popover. */
	.menu {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		z-index: 60;
		min-width: max(100%, 140px);
		max-width: calc(100vw - 16px);
		padding: 4px;
		display: flex;
		flex-direction: column;
		background: var(--surface);
		border: 1px solid var(--border-strong);
		border-radius: var(--radius-sm);
		box-shadow: var(--shadow-float);
		box-sizing: border-box;
	}

	.menu.above {
		top: auto;
		bottom: calc(100% + 4px);
	}

	.menu:focus {
		outline: none;
	}

	.menu-item {
		display: flex;
		align-items: center;
		min-height: 36px;
		padding: 6px 12px;
		border: none;
		border-radius: 6px;
		background: transparent;
		color: var(--text);
		font: inherit;
		font-size: 0.875rem;
		text-align: start;
		white-space: nowrap;
		cursor: pointer;
	}

	/* The focused item is the menu's current item whether focus came from a key
	   or a click on the trigger, so it is painted on :focus, not only
	   :focus-visible; the global ring still draws on top for keyboard users. */
	.menu-item:hover,
	.menu-item:focus {
		background: var(--surface-2);
	}

	@media (prefers-reduced-motion: reduce) {
		.menu-caret {
			transition: none;
		}
	}
</style>
