/**
 * Pure half of the unsaved-changes guard (`#lib/stores/unsavedChanges.svelte.ts`):
 * how a navigation the guard held back is replayed once the reader chooses to
 * leave. No SvelteKit runtime here, so it is unit-testable.
 */

/** The parts of a SvelteKit navigation the guard decides on. */
export interface HeldNavigation {
	type: string;
	to: { url: URL } | null;
	willUnload: boolean;
	/** Set on a back/forward (`popstate`) navigation only. */
	delta?: number;
}

/** How a held navigation is re-issued once the reader chooses to leave. */
export type Replay =
	| { kind: 'history'; delta: number }
	| { kind: 'document'; href: string }
	| { kind: 'goto'; href: string };

/**
 * Pure: the way to replay `nav`, or null when there is nothing to replay
 * (a `leave` navigation is the browser's to prompt for, and has no target).
 *
 * Back/forward is replayed as the same history step — SvelteKit already moved
 * the history pointer back when the navigation was cancelled — so the reader
 * lands where the button would have taken them, without a duplicate entry.
 * A navigation that leaves the app (another origin, a full document load) is a
 * document load again; anything else is a client-side `goto`.
 */
export function replayFor(
	nav: HeldNavigation,
	origin: string
): Replay | null {
	if (nav.type === 'leave') return null;
	if (nav.type === 'popstate' && nav.delta) return { kind: 'history', delta: nav.delta };
	const to = nav.to?.url;
	if (!to) return null;
	if (nav.willUnload || to.origin !== origin) return { kind: 'document', href: to.href };
	return { kind: 'goto', href: to.href };
}
