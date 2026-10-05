// Pure helpers for the outbound-webhook signing-secret rotation UI
// (`/admin/webhooks`). No Svelte, no `api` — so the rules can be unit-tested
// (`webhookRotation.test.ts`) without a browser or a backend.
//
// Backend contract: `POST /api/webhooks/{id}/rotate-secret` with an optional
// `{overlap_minutes}`. During the overlap window the retiring secret keeps
// signing a second `X-Webhook-Signature-Previous` header, so a receiver that
// accepts either header rotates with zero dropped deliveries. The bounds below
// mirror `backend/app/services/webhooks/rotation.py` — the backend refuses an
// out-of-range value with a 422 rather than clamping it (silently shortening a
// window drops deliveries the caller relied on; silently lengthening one keeps
// a key they wanted dead alive), so the picker must only ever offer values the
// backend accepts. See `backend/docs/public-api.md` § Rotating a signing secret.

import type { MessageKey } from '#lib/i18n/messages.ts';

/** `0` = hard cutover: the retiring secret stops verifying immediately. */
export const OVERLAP_MIN_MINUTES = 0;
/** 24h. Longer would defeat the point of rotating; the backend 422s past it. */
export const OVERLAP_MAX_MINUTES = 1440;
/** The backend's own default when the caller doesn't choose. */
export const OVERLAP_DEFAULT_MINUTES = 60;

export interface OverlapChoice {
	minutes: number;
	labelKey: MessageKey;
}

/**
 * The overlap windows the picker offers, shortest first.
 *
 * `0` leads deliberately: the reason an admin is on this screen unannounced is
 * usually a leak, and "the old secret must stop working now" is the answer to
 * that. Every other option is a planned rotation, where the window is there to
 * give a human time to paste the new secret into the receiving system.
 */
export const OVERLAP_CHOICES: readonly OverlapChoice[] = [
	{ minutes: 0, labelKey: 'admin.webhooks.rotate.overlap.cutover' },
	{ minutes: 15, labelKey: 'admin.webhooks.rotate.overlap.15m' },
	{ minutes: 60, labelKey: 'admin.webhooks.rotate.overlap.1h' },
	{ minutes: 240, labelKey: 'admin.webhooks.rotate.overlap.4h' },
	{ minutes: 1440, labelKey: 'admin.webhooks.rotate.overlap.24h' }
];

/** Whether the backend would accept this overlap (whole minutes, in range). */
export function isValidOverlapMinutes(minutes: number): boolean {
	return (
		Number.isInteger(minutes) &&
		minutes >= OVERLAP_MIN_MINUTES &&
		minutes <= OVERLAP_MAX_MINUTES
	);
}

/**
 * Is a rotation's overlap window still open — i.e. is the retiring secret still
 * signing the secondary header?
 *
 * Mirrors the backend's single expiry rule
 * (`webhooks/rotation.previous_secret_if_live`): live only while the expiry is
 * strictly in the future. Everything else — no window (`null`, the hard-cutover
 * case), an empty string, an unparseable timestamp — reads as **not live**.
 * That direction is the safe one: claiming an expired key still verifies would
 * send an admin away believing they have time they don't.
 */
export function isOverlapLive(expiresAt: string | null | undefined, now = Date.now()): boolean {
	if (!expiresAt) return false;
	const t = new Date(expiresAt).getTime();
	return Number.isFinite(t) && t > now;
}
