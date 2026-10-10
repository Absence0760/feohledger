/**
 * Toast the still-live-card warnings for a vendor write's response. Split from
 * `./cardRevocation.ts` so that module stays pure (vitest runs without the
 * Svelte compiler, and `Toast.svelte` needs it).
 */
import { toast } from '#lib/components/ui/Toast.svelte';
import { m } from '#lib/i18n/store.svelte.ts';
import type { VendorCardRevocation } from '#lib/types/vendor.ts';
import { cardRevocationWarnings } from './cardRevocation.ts';

// Longer than the default 5s: this is the one toast on the action that says
// money can still move, and it must outlast the success toast beside it.
const WARNING_MS = 12_000;

export function toastCardRevocations(
	revocations: ReadonlyArray<VendorCardRevocation | null | undefined> | null | undefined
): void {
	for (const text of cardRevocationWarnings(revocations, m)) toast(text, 'warning', WARNING_MS);
}
