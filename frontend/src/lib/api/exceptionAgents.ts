// Typed helpers for the autonomous exception-agent surfaces. All requests route
// through the shared `api` client (Bearer + X-Tenant-Slug + 401-bounce). Backend
// endpoints are admin/ap_manager-gated (see `app/api/exception_agents.py`).
import { api } from '#lib/api.ts';
import type {
	AgentCandidateException,
	AgentDecisionList,
	AgentResolveResult,
	AgentStats
} from '#lib/types/exceptionAgents.ts';
import { AGENT_RUNNABLE_STATUSES } from '#lib/types/exceptionAgents.ts';
import type { PagedResponse } from '#lib/utils/pagination.ts';

export interface AgentDecisionParams {
	exceptionType?: string;
	actionTaken?: string;
	page?: number;
	pageSize?: number;
}

export function getAgentStats(): Promise<AgentStats> {
	return api.get<AgentStats>('/api/exceptions/agent-stats');
}

export function getAgentDecisions(params: AgentDecisionParams = {}): Promise<AgentDecisionList> {
	const q = new URLSearchParams();
	if (params.exceptionType) q.set('exception_type', params.exceptionType);
	if (params.actionTaken) q.set('action_taken', params.actionTaken);
	q.set('page', String(params.page ?? 1));
	q.set('page_size', String(params.pageSize ?? 20));
	return api.get<AgentDecisionList>(`/api/exceptions/agent-decisions?${q}`);
}

/**
 * Run an autonomous agent on ONE exception.
 *
 * `POST /api/exceptions/{id}/agent-resolve` — admin / ap_manager, the same gate
 * as the decision log and stats beside it. The dashboard reported on agent
 * activity that could only be triggered elsewhere until this had a caller.
 *
 * Always returns a decision. `escalated` / `no_action` are OUTCOMES, not
 * errors: the coordinator only applies a fix whose confidence clears the org's
 * autonomy threshold, and hands the rest to a human. The genuine failures are
 * HTTP: 404 (unknown exception), 409 (already resolved / dismissed, or lost a
 * race with a concurrent run), 422 (invoice-less exception — nothing for an
 * agent to act on).
 */
export function runExceptionAgent(exceptionId: string): Promise<AgentResolveResult> {
	return api.post<AgentResolveResult>(`/api/exceptions/${exceptionId}/agent-resolve`, {});
}

/**
 * The exceptions an agent can currently be run on.
 *
 * Reads `GET /api/exceptions` (same admin / ap_manager gate) filtered to the
 * statuses `agent-resolve` accepts, so the runner never offers a row the
 * endpoint would 409. Invoice-less rows are dropped by the caller, not here —
 * the count of what exists and the count of what is runnable are different
 * facts.
 */
export function getAgentCandidates(pageSize = 25): Promise<PagedResponse<AgentCandidateException>> {
	const q = new URLSearchParams({
		status: AGENT_RUNNABLE_STATUSES.join(','),
		page: '1',
		page_size: String(pageSize)
	});
	return api.get<PagedResponse<AgentCandidateException>>(`/api/exceptions?${q}`);
}
