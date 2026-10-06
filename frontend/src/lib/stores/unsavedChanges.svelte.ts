/**
 * The app-wide "you have unsaved changes" guard.
 *
 * A page with an editable form calls `guardUnsavedChanges(() => dirty)` once,
 * during component init. Every in-app navigation away from it while dirty —
 * a sidebar link, a tab, a programmatic `goto`, the browser Back button — is
 * held, and the ONE `UnsavedChangesDialog` mounted in the root layout asks
 * whether to stay or leave. Leaving replays the held navigation exactly.
 *
 * A reload, a tab close or a typed URL is different: the browser allows no
 * custom dialog there, only its own. SvelteKit raises that native prompt when
 * a `leave` navigation is cancelled, so the guard cancels and stops.
 *
 * One dialog, one copy, one behaviour — instead of a native `confirm()` on
 * one page and nothing on the next.
 */
import { beforeNavigate, goto } from '$app/navigation';
import { replayFor, type Replay } from '#lib/utils/unsavedChanges.ts';

let held = $state<Replay | null>(null);
// One-shot pass for the navigation the reader chose to continue: replaying it
// runs `beforeNavigate` again, and the guard must not stop it a second time.
let passNext = false;

function replay(r: Replay): void {
	passNext = true;
	if (r.kind === 'history') history.go(r.delta);
	else if (r.kind === 'document') location.href = r.href;
	else
		void goto(r.href).finally(() => {
			passNext = false;
		});
}

/** State + answers for the dialog. */
export const unsavedChanges = {
	get pending(): boolean {
		return held !== null;
	},
	stay(): void {
		held = null;
	},
	leave(): void {
		const r = held;
		held = null;
		if (r) replay(r);
	},
};

/**
 * Hold navigations away from this page while `isDirty()` is true. Call once
 * during component init (it registers a `beforeNavigate` hook, which SvelteKit
 * scopes to the component's lifetime). Exclude an in-flight save from
 * `isDirty`, so the save's own redirect is never held.
 */
export function guardUnsavedChanges(isDirty: () => boolean): void {
	beforeNavigate((nav) => {
		if (passNext) {
			passNext = false;
			return;
		}
		if (!isDirty()) return;
		nav.cancel();
		held = replayFor(
			{
				type: nav.type,
				to: nav.to,
				willUnload: nav.willUnload,
				delta: 'delta' in nav ? nav.delta : undefined,
			},
			location.origin
		);
	});
}
