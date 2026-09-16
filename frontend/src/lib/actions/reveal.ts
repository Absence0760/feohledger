import type { Action } from 'svelte/action';

export interface RevealParams {
	/**
	 * Milliseconds to stagger this element behind its neighbours. Applied as a
	 * `transition-delay`, so it costs nothing when the element is already in
	 * view on load and nothing at all under reduced motion.
	 */
	delay?: number;
	/**
	 * How far into the viewport the element must come before it reveals, as a
	 * fraction of its own height. The default fires as soon as any of it is on
	 * screen, which is what a section header wants; a tall card wants more so it
	 * does not animate while it is still a sliver at the bottom edge.
	 */
	amount?: number;
}

/**
 * Reveals `node` when it first scrolls into view.
 *
 * **The hidden state is set by this action, never by a stylesheet.** That
 * ordering is the whole contract: the markup renders visible, and only an
 * element this action has actually attached to is ever hidden. If the script
 * fails to load, if `IntersectionObserver` is missing, or if the element is
 * inside a `<noscript>`-equivalent path, the page is a normal page with
 * everything on it — the failure mode of a CSS-first implementation is a blank
 * marketing site, which is the one outcome a marketing site cannot have.
 *
 * **Reduced motion is honoured by not animating at all**, rather than by
 * animating quickly. `app.css` already collapses every duration under
 * `prefers-reduced-motion: reduce`, but a collapsed duration still means the
 * element starts at `opacity: 0` and depends on an observer firing to leave it.
 * Here the preference is read before anything is hidden, so a user who asked
 * for less motion gets a page with no hidden state in it at all — nothing to
 * fail open from. The query is re-read per element rather than cached at module
 * scope so a preference changed mid-session applies to the next page.
 *
 * Reveal is one-way: the observer disconnects on the first intersection. An
 * element that re-hides on scroll-up is a distraction on the way back up a
 * page, and it makes `Ctrl+F` / find-in-page unreliable.
 *
 * ```svelte
 * <div use:reveal>…</div>
 * <div use:reveal={{ delay: 80, amount: 0.25 }}>…</div>
 * ```
 */
export const reveal: Action<HTMLElement, RevealParams | undefined> = (node, params) => {
	const prefersReducedMotion =
		typeof window !== 'undefined' &&
		typeof window.matchMedia === 'function' &&
		window.matchMedia('(prefers-reduced-motion: reduce)').matches;

	if (prefersReducedMotion || typeof IntersectionObserver === 'undefined') {
		return {};
	}

	node.classList.add('reveal');
	if (params?.delay) node.style.transitionDelay = `${params.delay}ms`;

	const observer = new IntersectionObserver(
		(entries) => {
			for (const entry of entries) {
				if (!entry.isIntersecting) continue;
				node.classList.add('reveal-in');
				// One-way: stop observing the moment it lands, so scrolling a long
				// page does not keep a few dozen observers alive for elements that
				// can never change state again.
				observer.disconnect();
			}
		},
		{ threshold: Math.min(Math.max(params?.amount ?? 0.01, 0), 1), rootMargin: '0px 0px -8% 0px' }
	);
	observer.observe(node);

	return {
		destroy() {
			observer.disconnect();
		}
	};
};

/**
 * Counts a number up to `value` when the element first scrolls into view, then
 * leaves it at `value`.
 *
 * It writes `textContent`, so the element must have no other children — the
 * caller renders the final value as the element's text and this replaces it
 * while the animation runs. That is deliberate: the **final** value is in the
 * markup, so a reader with reduced motion, a crawler, and a page whose script
 * never ran all see the real figure rather than a zero.
 *
 * `format` exists because these are not all plain integers — the marketing
 * stats include `1–2%`, and a count-up that ignored the suffix would animate to
 * a number that never appears on the page. A stat whose value is not a leading
 * number is left alone entirely.
 */
export const countUp: Action<HTMLElement, { value: string; duration?: number } | undefined> = (
	node,
	params
) => {
	const raw = params?.value ?? node.textContent ?? '';
	const match = /^(\d[\d,]*)(.*)$/.exec(raw.trim());
	const prefersReducedMotion =
		typeof window !== 'undefined' &&
		typeof window.matchMedia === 'function' &&
		window.matchMedia('(prefers-reduced-motion: reduce)').matches;

	// Nothing numeric to count, no observer, or the user asked for less motion:
	// leave the markup's own text exactly as it is.
	if (!match || prefersReducedMotion || typeof IntersectionObserver === 'undefined') {
		return {};
	}

	const target = Number(match[1].replace(/,/g, ''));
	const suffix = match[2];
	const duration = params?.duration ?? 900;
	let frame = 0;

	const observer = new IntersectionObserver((entries) => {
		for (const entry of entries) {
			if (!entry.isIntersecting) continue;
			observer.disconnect();
			const started = performance.now();
			const step = (now: number) => {
				const t = Math.min((now - started) / duration, 1);
				// Ease-out cubic: fast first, settling into the final value, which
				// is what makes the last digit readable rather than a blur.
				const eased = 1 - Math.pow(1 - t, 3);
				node.textContent = `${Math.round(target * eased).toLocaleString()}${suffix}`;
				if (t < 1) frame = requestAnimationFrame(step);
			};
			frame = requestAnimationFrame(step);
		}
	});
	observer.observe(node);

	return {
		destroy() {
			observer.disconnect();
			if (frame) cancelAnimationFrame(frame);
		}
	};
};
