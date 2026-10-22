/**
 * The new-password rules the employee forms check before they submit —
 * change-password and reset/set-password, through
 * `components/auth/NewPasswordFields.svelte`.
 *
 * They mirror the backend's complexity check (12+ characters, an upper- and a
 * lower-case letter, a digit), which stays the authority: a password that
 * passes here can still be refused with a 422, and the form shows that reason.
 * Pure, so the rules and the error ordering are unit-tested without a browser.
 */

export type PasswordRule = 'length' | 'upper' | 'lower' | 'digit';

export const PASSWORD_MIN_LENGTH = 12;

const RULES: ReadonlyArray<[PasswordRule, (pw: string) => boolean]> = [
	['length', (pw) => pw.length >= PASSWORD_MIN_LENGTH],
	['upper', (pw) => /[A-Z]/.test(pw)],
	['lower', (pw) => /[a-z]/.test(pw)],
	['digit', (pw) => /[0-9]/.test(pw)]
];

/** Every rule, in display order, with whether `password` meets it. */
export function passwordRuleStatus(password: string): Array<{ rule: PasswordRule; ok: boolean }> {
	return RULES.map(([rule, test]) => ({ rule, ok: test(password) }));
}

export function meetsPasswordRules(password: string): boolean {
	return RULES.every(([, test]) => test(password));
}

/**
 * What is wrong with the confirmation, if anything. `empty` and `mismatch`
 * are separate because they need different words: "enter it again" is not
 * "these don't match".
 */
export function confirmationProblem(
	password: string,
	confirmation: string
): 'empty' | 'mismatch' | null {
	if (confirmation === '') return 'empty';
	return confirmation === password ? null : 'mismatch';
}
