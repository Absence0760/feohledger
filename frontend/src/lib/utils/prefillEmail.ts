/**
 * The address a sign-in link asks the login page to prefill (`?email=`).
 *
 * Server-built links carry it — the signup welcome email's sign-in link and
 * the reset page's "go to sign in" (`backend/app/utils/tenant_urls.sign_in_url`,
 * `docs/decisions.md` §282). The value only ever becomes the email field's
 * text: Svelte binds it as a value, never markup, and nothing else on the page
 * reads it. Anything that is not plausibly a single address is dropped rather
 * than shown, so a crafted link cannot put a sentence in the field.
 */
const MAX_EMAIL_LENGTH = 320;
const ONE_ADDRESS = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function emailFromQuery(params: Pick<URLSearchParams, 'get'>): string {
	const raw = (params.get('email') ?? '').trim();
	if (!raw || raw.length > MAX_EMAIL_LENGTH || !ONE_ADDRESS.test(raw)) return '';
	return raw;
}

/** `/login`, with `email` prefilled when there is one. */
export function signInHref(email: string | null | undefined): string {
	return email ? `/login?${new URLSearchParams({ email }).toString()}` : '/login';
}
