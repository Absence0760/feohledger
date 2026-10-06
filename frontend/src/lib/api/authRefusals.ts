/**
 * Localizing the backend's coded auth refusals — a refused factor-change
 * step-up (employee or supplier portal), and a passkey ceremony with no usable
 * credential.
 *
 * The backend sends `detail = {code, message, params}` for these
 * (`backend/app/api/refusals.py::coded_refusal`), and `api.ts` /
 * `portalApi.ts` lift `code` / `params` onto the thrown `ApiError`. This module turns a known code into the
 * reader's language; an unknown code — one this build predates — or no code at
 * all falls back to the server's English `message`, never to a blank toast.
 * Same arrangement as `einvoiceIssues.ts`: it imports nothing that pulls
 * `$app/*`, so the node vitest config can reach it, and `m` is passed in by the
 * caller rather than imported from the rune store.
 *
 * The table is hand-written rather than generated because it is a handful of
 * entries that change only alongside a deliberate auth decision; `authRefusals.test.ts`
 * pins each code against the backend's literal so a rename on either side
 * fails there.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';
import { formatList } from '#lib/utils/list.ts';

/** The structural slice of `ApiError` this needs — kept structural so the
 *  module never imports `#lib/api.ts` (which reaches `$app/env/public`). */
export interface CodedRefusal {
	code: string | null;
	params: Record<string, unknown>;
	message: string;
}

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

const KEYS: Record<string, MessageKey> = {
	step_up_failed: 'authRefusal.stepUpFailed',
	step_up_sso_only: 'authRefusal.stepUpSsoOnly',
	passkey_wrong_host: 'authRefusal.passkeyWrongHost',
	passkey_not_registered: 'authRefusal.passkeyNotRegistered',
	portal_step_up_failed: 'authRefusal.portalStepUpFailed',
	// The second-factor gate on a sensitive action (`api/auth.require_sensitive_step_up`).
	sensitive_step_up_required: 'authRefusal.sensitiveStepUpRequired',
	sensitive_step_up_failed: 'authRefusal.sensitiveStepUpFailed',
	sensitive_step_up_no_factor: 'authRefusal.sensitiveStepUpNoFactor',
	sensitive_step_up_unavailable: 'authRefusal.sensitiveStepUpUnavailable'
};

/** The codes this build can state in the reader's language. */
export const AUTH_REFUSAL_CODES = Object.keys(KEYS);

function stringList(value: unknown): string[] | null {
	if (!Array.isArray(value) || value.length === 0) return null;
	if (!value.every((v) => typeof v === 'string' && v)) return null;
	return value as string[];
}

/**
 * The localized sentence for a coded refusal, or `null` when this build cannot
 * state it — then the caller renders `refusal.message`. A wrong-host refusal
 * whose params are missing or malformed is `null` too: a sentence with a blank
 * where the host belongs is worse than the server's complete English one.
 */
export function authRefusalText(refusal: CodedRefusal, t: Translate): string | null {
	if (!refusal.code) return null;
	const key = KEYS[refusal.code];
	if (!key) return null;
	if (refusal.code === 'passkey_wrong_host') {
		const hosts = stringList(refusal.params.registered_hosts);
		const host = refusal.params.host;
		if (!hosts || typeof host !== 'string' || !host) return null;
		return t(key, { hosts: formatList(hosts), host });
	}
	return t(key);
}

/**
 * What a page shows for an error from a step-up / passkey call: the localized
 * refusal when the error is a coded one this build knows, else the error's own
 * message, else the caller's localized fallback (a non-`Error` throw).
 */
export function authErrorMessage(err: unknown, t: Translate, fallback: MessageKey): string {
	if (!(err instanceof Error)) return t(fallback);
	// Structural rather than `instanceof ApiError` (see `CodedRefusal`). A
	// `DOMException` from a cancelled browser passkey prompt also has a `code`,
	// but a numeric one and no `params`, so it falls through to its message.
	const { code, params } = err as Error & { code?: unknown; params?: unknown };
	if (typeof code === 'string' && params && typeof params === 'object') {
		const localized = authRefusalText(
			{ code, params: params as Record<string, unknown>, message: err.message },
			t
		);
		if (localized) return localized;
	}
	return err.message;
}
