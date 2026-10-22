/**
 * The app-shell sidebar's state.
 *
 * `collapsed` is the 60px icon rail vs the 220px panel. `narrow` is the
 * viewport being at or below the phone breakpoint, set by the root layout's
 * `matchMedia` listener. On a narrow viewport the expanded panel is an
 * OVERLAY — it floats over the page behind a backdrop instead of pushing the
 * content aside, because at 320px a pushed page is left ~100px wide (#635
 * finding 39). `overlay` is that state, and the one place the shell reads it.
 */
function createSidebarStore() {
	let collapsed = $state(false);
	let narrow = $state(false);

	return {
		get collapsed() { return collapsed; },
		get narrow() { return narrow; },
		/** The expanded panel is floating over the page (narrow viewport only). */
		get overlay() { return narrow && !collapsed; },
		toggle() { collapsed = !collapsed; },
		collapse() { collapsed = true; },
		setNarrow(value: boolean) { narrow = value; },
	};
}

export const sidebar = createSidebarStore();
