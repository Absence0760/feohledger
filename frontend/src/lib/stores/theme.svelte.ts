import { browser } from '$app/env';
import { sharedCookieDomain } from '#lib/tenant.ts';
import {
	applyTheme,
	browserCookies,
	browserStorage,
	PREFERS_LIGHT_QUERY,
	readThemeChoice,
	resolveTheme,
	systemPrefersLight,
	writeThemeChoice,
	type ResolvedTheme,
	type ThemeChoice
} from '#lib/theme.ts';

/**
 * The reactive face of `#lib/theme.ts` (docs/decisions.md §289, §290): the
 * visitor's System / Light / Dark choice and the palette it resolves to.
 *
 * The first paint is not this store's job — `src/app.html`'s inline script has
 * already put the stored choice on `<html>` before any bundle loads. `init()`
 * only picks that state up so the picker shows it, and tracks the OS so
 * `resolved` stays true while System is selected. The palette itself switches
 * in CSS; nothing here repaints anything. `resolved` is what a widget that
 * takes a theme argument (hCaptcha on signup) follows.
 */
class ThemeStore {
	choice = $state<ThemeChoice>('system');
	#prefersLight = $state(false);
	#initialised = false;

	/** The palette actually showing. */
	resolved: ResolvedTheme = $derived(resolveTheme(this.choice, this.#prefersLight));

	constructor() {
		// Read the stored choice and the OS NOW, not in `init()`: the root
		// layout calls that from an `$effect`, which runs after its children
		// have mounted, so until then `resolved` said dark whatever `app.html`
		// had painted. The landing's ornament `<img>` mounted with the dark
		// render under a stored Light choice, the browser started fetching it,
		// and `init()` swapped it a moment later.
		if (browser) this.#read();
	}

	#read(): void {
		this.choice = readThemeChoice(browserStorage(), browserCookies());
		this.#prefersLight = systemPrefersLight(window);
	}

	init(): void {
		if (!browser || this.#initialised) return;
		this.#initialised = true;
		this.#read();
		try {
			window.matchMedia?.(PREFERS_LIGHT_QUERY).addEventListener('change', (e) => {
				this.#prefersLight = e.matches;
				applyTheme(document, this.choice, this.resolved);
			});
		} catch {
			/* no matchMedia — System stays on its initial resolution */
		}
		applyTheme(document, this.choice, this.resolved);
	}

	/**
	 * Switch theme now, and remember it: across every workspace of the platform
	 * where the host allows a shared cookie, in this origin's storage otherwise.
	 */
	set(choice: ThemeChoice): void {
		this.choice = choice;
		if (!browser) return;
		writeThemeChoice(browserStorage(), choice, {
			jar: document,
			domain: sharedCookieDomain(),
			secure: window.location.protocol === 'https:'
		});
		applyTheme(document, choice, this.resolved);
	}
}

export const theme = new ThemeStore();
