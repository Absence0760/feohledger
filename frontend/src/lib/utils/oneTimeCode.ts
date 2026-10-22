/**
 * A one-time code as the sign-in MFA step sends it: digits only, at most 8
 * (`MFAVerifyRequest.code` is 6-8 digits; TOTP and the emailed code are both
 * six). A pasted code arrives with whatever surrounded it — `123 456`,
 * `123-456`, a trailing newline from the email, full-width digits — and the field used to keep
 * that verbatim, so a paste counted toward the length gate and was then
 * refused by the server. Normalising on input keeps what the user sees equal
 * to what is sent.
 */
export const ONE_TIME_CODE_MAX = 8;

export function normalizeOneTimeCode(raw: string): string {
	// NFKC folds full-width digits (a Japanese IME in full-width mode types
	// `１２３`) to ASCII before the non-digits are dropped.
	return raw.normalize('NFKC').replace(/\D/g, '').slice(0, ONE_TIME_CODE_MAX);
}
