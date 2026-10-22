/**
 * What an identity provider's `?error=` on the SSO / SAML callback means, in
 * words a person signing in can act on.
 *
 * An OIDC provider that refuses or abandons a sign-in redirects back with an
 * RFC 6749 §4.1.2.1 / OpenID Connect Core §3.1.2.6 error code (`access_denied`,
 * `login_required`, …). The callback pages used to print that code as the whole
 * message ("Identity provider error: access_denied"), which names a protocol
 * detail to someone who only pressed Cancel (issue #635 finding 36). Each known
 * code maps to a plain sentence; anything else gets one generic sentence.
 *
 * The raw code is kept as secondary detail for a support conversation — but
 * only when it LOOKS like a protocol code. The value comes from the URL, so
 * anyone can write a callback link whose `error` is a sentence of their own
 * ("call this number to restore access"); {@link idpErrorCodeForDisplay}
 * refuses to echo anything that is not a short identifier.
 *
 * Pure (no Svelte, no `$app`), so the node vitest config reaches it.
 */
import type { MainMessageKey } from '#lib/i18n/messages.ts';

/** The codes with a sentence of their own. */
export const IDP_ERROR_KEYS: Readonly<Record<string, MainMessageKey>> = {
	access_denied: 'auth.callback.idpAccessDenied',
	login_required: 'auth.callback.idpLoginRequired',
	consent_required: 'auth.callback.idpConsentRequired',
	interaction_required: 'auth.callback.idpInteractionRequired',
	temporarily_unavailable: 'auth.callback.idpTemporarilyUnavailable',
	server_error: 'auth.callback.idpServerError'
};

/** The sentence for an IdP error code; a code this build does not know gets the generic one. */
export function idpErrorKey(code: string): MainMessageKey {
	return Object.hasOwn(IDP_ERROR_KEYS, code) ? IDP_ERROR_KEYS[code] : 'auth.callback.idpUnknown';
}

/** A protocol error code is a short identifier (RFC 6749 restricts it to
 *  printable ASCII; every registered one is lowercase words and underscores). */
const DISPLAYABLE_CODE = /^[A-Za-z0-9_.-]{1,64}$/;

/** The raw code, when it is safe to show as support detail; otherwise `null`. */
export function idpErrorCodeForDisplay(code: string): string | null {
	return DISPLAYABLE_CODE.test(code) ? code : null;
}
