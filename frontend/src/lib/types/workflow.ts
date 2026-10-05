import type { MoneyAmount, MoneyString } from '#lib/utils/money.ts';

export type WorkflowStepType =
	| 'extraction'
	| 'approval'
	| 'erp_export'
	| 'condition'
	| 'parallel'
	| 'webhook'
	| 'email'
	| 'delay';

export const STEP_TYPE_LABELS: Record<WorkflowStepType, string> = {
	extraction: 'Data Extraction',
	approval: 'Approval',
	erp_export: 'ERP Export',
	condition: 'Condition',
	parallel: 'Parallel Approval',
	webhook: 'Webhook',
	email: 'Send Email',
	delay: 'Delay',
};

export const STEP_TYPE_DESCRIPTIONS: Record<WorkflowStepType, string> = {
	extraction: 'AI-powered data extraction from uploaded invoices',
	approval: 'Human review and approval of invoice data',
	erp_export: 'Export approved invoices to your ERP system',
	condition: 'Branch the workflow based on invoice attributes',
	parallel: 'Fan out to several approver groups and join the results',
	webhook: 'Call an external HTTP endpoint',
	email: 'Send a notification email',
	delay: 'Pause the workflow for a fixed duration',
};

export interface ExtractionStepConfig {
	auto_approve_enabled: boolean;
	auto_approve_threshold: number;
}

export type RoutingField = 'gl_account' | 'cost_center' | 'department' | 'vendor_id';
export type RoutingOperator = 'eq' | 'ne' | 'in' | 'not_in' | 'starts_with';

export interface RoutingRule {
	field: RoutingField;
	operator: RoutingOperator;
	value: string | string[];
}

export const ROUTING_FIELD_LABELS: Record<RoutingField, string> = {
	gl_account: 'GL account',
	cost_center: 'Cost center',
	department: 'Department',
	vendor_id: 'Vendor',
};

export const ROUTING_OPERATOR_LABELS: Record<RoutingOperator, string> = {
	eq: 'equals',
	ne: 'does not equal',
	in: 'is one of',
	not_in: 'is not one of',
	starts_with: 'starts with',
};

export interface ApprovalLevelConfig {
	// The chain's per-level amount band, and the three thresholds on
	// `ApprovalStepConfig` below, are money the user TYPES and we send back.
	// The builder now holds the raw decimal text: `json.loads` decodes the body
	// before any validator runs, so a JSON number has already been through a
	// float by the time `schemas/workflow.py`'s `Decimal | None` sees it, and
	// no annotation can undo that.
	//
	// `MoneyAmount`, not `MoneyString`, because these live in the free-form
	// `steps_config` JSONB: every workflow saved before this change persisted
	// a `parseFloat`ed NUMBER there, and it is still what a load returns. The
	// alias is honest about both shapes, and it is still what makes `>` on a
	// threshold a compile error.
	//
	// The builder validates the shape at save and refuses text it cannot read,
	// rather than repairing it or sending `null` — a silently-dropped
	// `require_cfo_above` removes the CFO gate on every invoice it routes.
	min_amount: MoneyAmount;
	max_amount: MoneyAmount;
	approver_ids: string[];
	required_approvals: number;
	name: string;
	routing_rules: RoutingRule[];
	parallel_mode: 'any' | 'all';
	escalation_hours: number | null;
	escalation_to_user_ids: string[];
}

export interface ApprovalStepConfig {
	required: boolean;
	approver_id: string | null;
	approver_ids: string[];
	approver_strategy: 'manual' | 'specific' | 'auto' | 'chain';
	auto_approve_below: MoneyAmount;
	require_cfo_above: MoneyAmount;
	max_invoice_amount: MoneyAmount;
	approval_chain: ApprovalLevelConfig[];
	require_segregation: boolean;
}

export type ErpExportFormat = 'json' | 'xml' | 'csv' | 'cxml' | 'edi';

export const ERP_FORMAT_LABELS: Record<ErpExportFormat, string> = {
	json: 'JSON',
	xml: 'XML',
	csv: 'CSV',
	cxml: 'cXML',
	edi: 'EDI',
};

export interface ErpExportStepConfig {
	export_format: ErpExportFormat;
	auto_send_on_approval: boolean;
	include_line_items: boolean;
	include_attachments: boolean;
}

// ── Builder step types (stored in steps_config JSONB, no enum migration) ──

export type ConditionField =
	| 'amount'
	| 'currency'
	| 'vendor_id'
	| 'gl_account'
	| 'cost_center'
	| 'department';

export type ConditionOperator =
	| 'gt'
	| 'gte'
	| 'lt'
	| 'lte'
	| 'eq'
	| 'ne'
	| 'in'
	| 'not_in'
	| 'starts_with';

export const CONDITION_FIELD_LABELS: Record<ConditionField, string> = {
	amount: 'Amount',
	currency: 'Currency',
	vendor_id: 'Vendor',
	gl_account: 'GL account',
	cost_center: 'Cost center',
	department: 'Department',
};

export const CONDITION_OPERATOR_LABELS: Record<ConditionOperator, string> = {
	gt: 'greater than',
	gte: 'greater than or equal',
	lt: 'less than',
	lte: 'less than or equal',
	eq: 'equals',
	ne: 'does not equal',
	in: 'is one of',
	not_in: 'is not one of',
	starts_with: 'starts with',
};

export interface ConditionRule {
	field: ConditionField;
	operator: ConditionOperator;
	value: number | string | string[];
}

export interface ConditionStepConfig {
	rules: ConditionRule[];
	match: 'all' | 'any';
	on_true_goto: number | null;
	on_false_goto: number | null;
}

export interface ParallelBranchConfig {
	name: string;
	approver_ids: string[];
}

export interface ParallelStepConfig {
	branches: ParallelBranchConfig[];
	join: 'all' | 'any';
	min_approvals: number | null;
}

export type WebhookMethod = 'POST' | 'GET' | 'PUT';

export interface WebhookStepConfig {
	url: string;
	method: WebhookMethod;
	headers: Record<string, string>;
	body_template: string | null;
	timeout_seconds: number;
}

export type EmailRecipientKind = 'approver' | 'vendor' | 'custom';

export interface EmailStepConfig {
	to: EmailRecipientKind;
	to_addresses: string[];
	subject: string;
	body_template: string;
}

export interface DelayStepConfig {
	duration_seconds: number;
	until_field: string | null;
}

export type StepConfig =
	| ExtractionStepConfig
	| ApprovalStepConfig
	| ErpExportStepConfig
	| ConditionStepConfig
	| ParallelStepConfig
	| WebhookStepConfig
	| EmailStepConfig
	| DelayStepConfig;

export interface WorkflowStep {
	number: number;
	type: WorkflowStepType;
	name: string;
	enabled: boolean;
	config: StepConfig;
}

export interface WorkflowDefinition {
	id: string;
	name: string;
	description: string | null;
	steps_config: { steps: WorkflowStep[] };
	is_active: boolean;
	is_default: boolean;
	created_at: string;
	updated_at: string | null;
}

export const DEFAULT_EXTRACTION_CONFIG: ExtractionStepConfig = {
	auto_approve_enabled: false,
	auto_approve_threshold: 0.95,
};

export const DEFAULT_APPROVAL_CONFIG: ApprovalStepConfig = {
	required: true,
	approver_id: null,
	approver_ids: [],
	approver_strategy: 'manual',
	auto_approve_below: null,
	require_cfo_above: null,
	max_invoice_amount: null,
	approval_chain: [],
	// Segregation of duties (approver ≠ uploader) is the classic AP invariant and
	// the backend's own default (`schemas/workflow.py`, `approval_chain.py` reads
	// `.get("require_segregation", True)`). A UI-created workflow MUST NOT ship a
	// weaker default than a template- or API-created one: an explicit `false` here
	// is a real value that silently enabled self-approval on every workflow built
	// in the builder. Turning it off is now a deliberate, visible choice in the
	// approval-step editor.
	require_segregation: true,
};

export const DEFAULT_ERP_CONFIG: ErpExportStepConfig = {
	export_format: 'json',
	auto_send_on_approval: true,
	include_line_items: true,
	include_attachments: false,
};

export const DEFAULT_CONDITION_CONFIG: ConditionStepConfig = {
	rules: [{ field: 'amount', operator: 'gt', value: 0 }],
	match: 'all',
	on_true_goto: null,
	on_false_goto: null,
};

export const DEFAULT_PARALLEL_CONFIG: ParallelStepConfig = {
	branches: [{ name: 'Branch 1', approver_ids: [] }],
	join: 'all',
	min_approvals: null,
};

export const DEFAULT_WEBHOOK_CONFIG: WebhookStepConfig = {
	url: '',
	method: 'POST',
	headers: {},
	body_template: null,
	timeout_seconds: 30,
};

export const DEFAULT_EMAIL_CONFIG: EmailStepConfig = {
	to: 'approver',
	to_addresses: [],
	subject: '',
	body_template: '',
};

export const DEFAULT_DELAY_CONFIG: DelayStepConfig = {
	duration_seconds: 3600,
	until_field: null,
};

export const DEFAULT_STEP_CONFIGS: Record<WorkflowStepType, () => StepConfig> = {
	extraction: () => ({ ...DEFAULT_EXTRACTION_CONFIG }),
	approval: () => ({ ...DEFAULT_APPROVAL_CONFIG, approver_ids: [], approval_chain: [] }),
	erp_export: () => ({ ...DEFAULT_ERP_CONFIG }),
	condition: () => structuredClone(DEFAULT_CONDITION_CONFIG),
	parallel: () => structuredClone(DEFAULT_PARALLEL_CONFIG),
	webhook: () => structuredClone(DEFAULT_WEBHOOK_CONFIG),
	email: () => structuredClone(DEFAULT_EMAIL_CONFIG),
	delay: () => structuredClone(DEFAULT_DELAY_CONFIG),
};

// ── Management interfaces (templates, versioning, simulation, import/export) ──

export interface WorkflowTemplate {
	key: string;
	name: string;
	description: string;
	category: string;
	steps_config: { steps: WorkflowStep[] };
}

export interface WorkflowVersion {
	id: string;
	version_number: number;
	note: string | null;
	created_at: string;
	created_by: string | null;
	steps_config: { steps: WorkflowStep[] };
}

export interface WorkflowDiffChange {
	kind: 'added' | 'removed' | 'changed';
	step_number: number;
	field: string | null;
	before: unknown;
	after: unknown;
	summary: string;
}

export interface WorkflowDiff {
	from_version: number | string;
	to_version: number | string;
	changes: WorkflowDiffChange[];
}

export interface SimInvoice {
	/**
	 * Request-side money: `schemas/workflow.py::SimInvoice.amount` is a
	 * `Decimal` that accepts a string-decimal precisely so the caller never
	 * has to send a float. The modal already types it as text; the `number`
	 * arm this used to carry was dead and only invited arithmetic.
	 */
	amount: MoneyString;
	currency: string;
	vendor_id: string | null;
	gl_account: string | null;
	cost_center: string | null;
	department: string | null;
}

export interface SimulationPathEntry {
	step_number: number;
	type: string;
	name: string;
	outcome: string;
	detail: string;
}

export interface SimulationResult {
	path: SimulationPathEntry[];
	terminal_state: string;
	warnings: string[];
}

export interface WorkflowExport {
	schema_version: number;
	name: string;
	description: string | null;
	steps_config: { steps: WorkflowStep[] };
}
