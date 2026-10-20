/**
 * Closing a workspace (admin only) — `backend/app/api/organization_close.py`.
 *
 * `getClosurePreflight` reads which blockers, if any, stop a self-service
 * closure. `closeWorkspace` deletes the workspace; it needs the slug typed back
 * and, where MFA is on, a second-factor proof (the backend answers
 * `sensitive_step_up_required` until one is sent — the page then opens
 * `ui/StepUpPrompt` and resends with it). See `docs/decisions.md` §294.
 */
import { api } from '#lib/api.ts';
import type { StepUpProof } from '#lib/stores/auth.svelte.ts';
import type { ClosurePreflight } from '#lib/types/workspaceClosure.ts';

const BASE = '/api/organization/close';

export function getClosurePreflight(): Promise<ClosurePreflight> {
	return api.get<ClosurePreflight>(BASE);
}

export function closeWorkspace(
	confirmSlug: string,
	stepUp?: StepUpProof
): Promise<{ closed: boolean }> {
	return api.post<{ closed: boolean }>(BASE, {
		confirm_slug: confirmSlug.trim(),
		...(stepUp ? { step_up: stepUp } : {})
	});
}
