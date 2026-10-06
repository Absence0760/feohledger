/**
 * The server's own shape for an authenticator code typed into a step-up
 * (`MFAStepUpRequest.code`: 6-8 digits). A shorter entry is a 422, so a
 * half-typed code must not count as an offered proof — every step-up prompt
 * (`/profile`'s factor changes, `ui/StepUpPrompt.svelte`) gates its submit on
 * this one predicate.
 */
export function isCompleteStepUpCode(value: string): boolean {
	return /^[0-9]{6,8}$/.test(value);
}
