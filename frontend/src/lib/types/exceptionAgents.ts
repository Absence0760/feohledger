// Types for the autonomous exception-agent surfaces (decision log + stats).
// Mirrors the backend Pydantic shapes in
// `backend/app/schemas/exception_agent.py`. Confidence is a display-only float
// (stored exact as Numeric(5,4) server-side).

import type { MessageKey } from '#lib/i18n/messages.ts';

export interface AgentDecision {
	id: string;
	exception_id: string;
	invoice_id: string;
	exception_type: string;
	action_taken: 'auto_resolved' | 'escalated' | 'no_action';
	confidence: number;
	rationale: string | null;
	changes: Record<string, { old: string; new: string }> | null;
	autonomy_level: string;
	agent_type: string;
	created_at: string;
}

export interface AgentDecisionList {
	items: AgentDecision[];
	total: number;
	page: number;
	page_size: number;
}

export interface AgentStats {
	total_decisions: number;
	auto_resolved: number;
	escalated: number;
	no_action: number;
	resolution_rate: number;
	escalation_rate: number;
	// Accuracy is a placeholder pending a human-overturn signal — `null` until
	// the platform tracks whether an auto-resolution was later reversed.
	accuracy: number | null;
}

/**
 * The i18n key carrying each action's label — never the English string itself.
 *
 * The dashboard renders this badge in its decision log AND as the outcome of a
 * run it just performed, beside a confidence figure and an autonomy level, so a
 * hardcoded map read as a hole in an otherwise-translated panel. The same three
 * labels are the action filter chips, which is why the chips reuse these keys
 * rather than minting their own — a chip that says one thing and the rows it
 * filters another is the drift a shared map removes.
 *
 * Vocabulary owner: `backend/app/services/exception_agents/base.py`'s
 * `ACTION_*` constants. `exceptionAgents.test.ts` reads them.
 */
export const ACTION_LABEL_KEYS: Record<AgentDecision['action_taken'], MessageKey> = {
	auto_resolved: 'exceptions.agents.action.autoResolved',
	escalated: 'exceptions.agents.action.escalated',
	no_action: 'exceptions.agents.action.noAction'
};

/**
 * The message key for an action, or `null` for one this build doesn't know.
 * `agent_decisions.action_taken` is a plain `String(20)`, so a row written by a
 * newer coordinator can carry an action this frontend predates — the caller
 * then renders the raw value rather than an empty badge.
 */
export function agentActionLabelKey(action: string): MessageKey | null {
	return ACTION_LABEL_KEYS[action as AgentDecision['action_taken']] ?? null;
}

/**
 * The org's autonomy setting, lowest authority first —
 * `backend/app/services/exception_agents/autonomy.py::_THRESHOLDS`.
 * `conservative` is "off": its threshold is unreachable, so every exception
 * escalates.
 */
export const AUTONOMY_LEVELS = ['conservative', 'balanced', 'aggressive'] as const;

export type AutonomyLevel = (typeof AUTONOMY_LEVELS)[number];

/**
 * Autonomy-level labels. Rendered in the decision-log row and in the run
 * dialog's facts list; both printed the raw lowercase wire value before.
 */
export const AUTONOMY_LEVEL_LABEL_KEYS: Record<AutonomyLevel, MessageKey> = {
	conservative: 'exceptions.agents.autonomy.conservative',
	balanced: 'exceptions.agents.autonomy.balanced',
	aggressive: 'exceptions.agents.autonomy.aggressive'
};

/**
 * The message key for an autonomy level, or `null` for an unknown one.
 * `resolve_autonomy_level` already falls back to `conservative` when it STAMPS
 * a decision, but the column is a plain `String(20)` and historical rows are
 * read back as written, so the accessor stays tolerant.
 */
export function autonomyLevelLabelKey(level: string): MessageKey | null {
	return AUTONOMY_LEVEL_LABEL_KEYS[level as AutonomyLevel] ?? null;
}

/**
 * What `POST /api/exceptions/{id}/agent-resolve` returns — the exception's new
 * status plus the single append-only `AgentDecision` the coordinator recorded.
 *
 * There is always a decision, whatever the outcome. `escalated` is a NORMAL
 * result, not a failure: the coordinator resolves the org's autonomy threshold,
 * and a recommendation whose confidence doesn't clear it is handed to a human
 * instead of applied. `no_action` likewise means the resolver found nothing it
 * could safely change. Rendering either as an error would teach operators that
 * the safe path is the broken one.
 * See `backend/docs/exception-agents.md`.
 */
export interface AgentResolveResult {
	exception: { id: string; status: string };
	decision: AgentDecision;
}

/**
 * One exception an agent can be run on — the `GET /api/exceptions` row, narrowed
 * to what the runner panel shows and to what the endpoint will actually accept.
 *
 * `invoice_id` is nullable on the wire and the backend 422s an invoice-less
 * exception (a Positive Pay `not_on_file` fraud return has no invoice for an
 * agent to act on — human triage only), so the panel filters those out rather
 * than offering a Run that can only fail.
 */
export interface AgentCandidateException {
	id: string;
	invoice_id: string | null;
	invoice_number: string | null;
	vendor_name: string | null;
	exception_type: string;
	type_label: string;
	severity: string;
	status: string;
	created_at: string;
}

/** The statuses `POST .../agent-resolve` accepts; anything else 409s. */
export const AGENT_RUNNABLE_STATUSES = ['open', 'escalated'] as const;
