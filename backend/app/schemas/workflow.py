"""Pydantic schemas for workflow action endpoints."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.api.pagination import PageMeta

# ---------- workflow definition schemas ----------


class ExtractionStepConfig(BaseModel):
    auto_approve_enabled: bool = False
    auto_approve_threshold: float = Field(default=0.95, ge=0.0, le=1.0)


# Fields the routing engine knows how to read off an Invoice. Anything not
# in this set is silently ignored by `_evaluate_routing_rules` so a stale
# UI config can't hard-fail the approval flow.
RoutingField = Literal["gl_account", "cost_center", "department", "vendor_id"]
RoutingOperator = Literal["eq", "ne", "in", "not_in", "starts_with"]


class RoutingRule(BaseModel):
    """One conditional clause that decides whether a chain level applies.

    Multiple rules on the same level AND together. `value` accepts a string
    for scalar operators (eq, ne, starts_with) or a list of strings for set
    operators (in, not_in)."""

    field: RoutingField
    operator: RoutingOperator
    value: str | list[str]


class ApprovalLevelConfig(BaseModel):
    """One level in a multi-level approval chain."""

    # Money is exact — never float (project invariant). The routing engine reads
    # these off the JSONB snapshot via `_to_decimal`, which already coerces
    # str/number/Decimal; typing them Decimal keeps the API boundary exact too.
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    approver_ids: list[str] = []
    required_approvals: int = Field(default=1, ge=1)
    name: str = ""
    # Optional dept/GL/vendor filter — level only applies when every rule
    # evaluates True against the invoice. Empty list = no filter.
    routing_rules: list[RoutingRule] = []
    # "any": `required_approvals` distinct users from approver_ids satisfies
    # the level (default; matches legacy behaviour). "all": every listed
    # approver_id must have approved.
    parallel_mode: Literal["any", "all"] = "any"
    # Escalation knobs. After this many hours sitting at the current level,
    # the sweeper appends `escalation_to_user_ids` to `approver_ids` so
    # those users become eligible to approve. None disables escalation.
    escalation_hours: int | None = Field(default=None, ge=1)
    escalation_to_user_ids: list[str] = []


class ApprovalStepConfig(BaseModel):
    required: bool = True
    approver_id: str | None = None  # deprecated, use approver_ids
    approver_ids: list[str] = []
    approver_strategy: str = "manual"  # "manual", "specific", "auto", "chain"
    # Money is exact — never float (project invariant). Consumed off the JSONB
    # snapshot by `decide_auto_approve` / `_to_decimal`, which coerce safely.
    auto_approve_below: Decimal | None = None  # auto-approve invoices below this amount
    require_cfo_above: Decimal | None = None  # require CFO approval above this amount
    max_invoice_amount: Decimal | None = None  # reject invoices above this amount
    approval_chain: list[ApprovalLevelConfig] = []  # used when strategy="chain"
    require_segregation: bool = True  # approver ≠ uploader (classic AP invariant; SOC 2 baseline)


class ErpExportStepConfig(BaseModel):
    export_format: str = "json"  # "json", "xml", "csv", "cxml", "edi"
    auto_send_on_approval: bool = True  # send to ERP immediately after approval
    include_line_items: bool = True  # include line item details in the payload
    include_attachments: bool = False  # include PDF file URL in the payload


# ---------- no-code builder step config shapes ----------
#
# These mirror the canonical config keys in reviews/workflow-builder-spec.md
# (§ "config shapes per NEW type"). Worker A's workflow_builder consumes the
# exact same keys; do not rename. Stored inside the `steps_config` JSONB — no
# enum migration: the new types live alongside the canonical
# extraction/approval/erp_export/done types.

ConditionField = Literal[
    "amount", "currency", "vendor_id", "gl_account", "cost_center", "department"
]
ConditionOperator = Literal["gt", "gte", "lt", "lte", "eq", "ne", "in", "not_in", "starts_with"]


class ConditionRule(BaseModel):
    field: ConditionField
    operator: ConditionOperator
    # number / string for scalar operators; list for in / not_in.
    value: float | str | list[str | float]


class ConditionStepConfig(BaseModel):
    rules: list[ConditionRule] = []
    match: Literal["all", "any"] = "all"
    on_true_goto: int | None = None
    on_false_goto: int | None = None


class ParallelBranch(BaseModel):
    name: str = ""
    approver_ids: list[str] = []


class ParallelStepConfig(BaseModel):
    branches: list[ParallelBranch] = []
    join: Literal["all", "any"] = "all"
    min_approvals: int | None = None


class WebhookStepConfig(BaseModel):
    url: str = ""
    method: Literal["POST", "GET", "PUT"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body_template: str | None = None
    timeout_seconds: int = 10


class EmailStepConfig(BaseModel):
    to: Literal["approver", "vendor", "custom"] = "approver"
    to_addresses: list[str] = []
    subject: str = ""
    body_template: str = ""


class DelayStepConfig(BaseModel):
    duration_seconds: int = 0
    until_field: str | None = None


# Maps a step's `type` to the config model that shape actually owns. Pydantic
# can't natively discriminate `config`'s Union by this — the tag (`type`)
# lives on the SIBLING field, not inside `config` itself — so a bare Union
# resolves by trying each member in declaration order and scoring the best
# "fit", which is ambiguous when every member's fields are optional and the
# last member is a catch-all `dict`. That let a full, valid `ApprovalStepConfig`
# payload (including the Decimal money fields) silently resolve to the untyped
# `dict` fallback instead — the Union's own typing was never actually enforced
# on that write path. `"done"` has no config shape of its own and is
# deliberately absent (falls through to the Union's own resolution → `dict`).
_STEP_CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "extraction": ExtractionStepConfig,
    "approval": ApprovalStepConfig,
    "erp_export": ErpExportStepConfig,
    "condition": ConditionStepConfig,
    "parallel": ParallelStepConfig,
    "webhook": WebhookStepConfig,
    "email": EmailStepConfig,
    "delay": DelayStepConfig,
}


class WorkflowStepConfig(BaseModel):
    number: int
    # extraction | approval | erp_export | done (canonical) +
    # condition | parallel | webhook | email | delay (builder)
    type: Literal[
        "extraction",
        "approval",
        "erp_export",
        "done",
        "condition",
        "parallel",
        "webhook",
        "email",
        "delay",
    ]
    name: str
    enabled: bool = True
    config: (
        ExtractionStepConfig
        | ApprovalStepConfig
        | ErpExportStepConfig
        | ConditionStepConfig
        | ParallelStepConfig
        | WebhookStepConfig
        | EmailStepConfig
        | DelayStepConfig
        | dict
    ) = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _dispatch_config_by_type(cls, data):
        """Resolve `config` against the model `type` actually names, instead
        of leaving the Union to guess. Only intercepts plain-dict input (a
        JSON request body); an already-constructed instance or config object
        passes through untouched. An unrecognized `type` (e.g. "done") falls
        through to the Union's own resolution unchanged."""
        if not isinstance(data, dict):
            return data
        model_cls = _STEP_CONFIG_MODELS.get(data.get("type"))
        raw_config = data.get("config")
        if model_cls is not None and isinstance(raw_config, dict):
            data = {**data, "config": model_cls.model_validate(raw_config)}
        return data


class WorkflowDefinitionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    steps: list[WorkflowStepConfig]


class WorkflowDefinitionUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    steps: list[WorkflowStepConfig] | None = None


class WorkflowDefinitionResponse(BaseModel):
    id: str
    name: str
    description: str | None
    steps_config: dict
    is_active: bool
    is_default: bool
    created_at: str
    updated_at: str | None

    @classmethod
    def from_db(cls, defn) -> "WorkflowDefinitionResponse":
        return cls(
            id=str(defn.id),
            name=defn.name,
            description=defn.description,
            steps_config=defn.steps_config,
            is_active=defn.is_active,
            is_default=defn.is_default,
            created_at=defn.created_at.isoformat() if defn.created_at else "",
            updated_at=defn.updated_at.isoformat() if defn.updated_at else None,
        )


class WorkflowDefinitionListResponse(PageMeta):
    items: list[WorkflowDefinitionResponse]
    total: int


# ---------- no-code builder: templates / versions / diff / simulation / I-O ----------


class WorkflowTemplate(BaseModel):
    key: str
    name: str
    description: str
    category: str
    steps_config: dict


class WorkflowTemplateListResponse(BaseModel):
    items: list[WorkflowTemplate]


class WorkflowVersion(BaseModel):
    id: str
    version_number: int
    note: str | None
    created_at: str
    created_by: str | None
    steps_config: dict

    @classmethod
    def from_db(cls, ver) -> "WorkflowVersion":
        return cls(
            id=str(ver.id),
            version_number=ver.version_number,
            note=ver.note,
            created_at=ver.created_at.isoformat() if ver.created_at else "",
            created_by=str(ver.created_by) if ver.created_by else None,
            steps_config=ver.steps_config,
        )


class WorkflowVersionListResponse(BaseModel):
    items: list[WorkflowVersion]


class WorkflowDiffChange(BaseModel):
    kind: Literal["added", "removed", "changed"]
    step_number: int
    field: str | None = None
    before: object | None = None
    after: object | None = None
    summary: str


class WorkflowDiff(BaseModel):
    from_version: int | str
    to_version: int | str
    changes: list[WorkflowDiffChange]


class SimInvoice(BaseModel):
    # amount accepts a string-decimal so the caller never has to send a float;
    # money stays Decimal end-to-end.
    amount: Decimal = Field(default=Decimal("0"))
    currency: str = "USD"
    vendor_id: str | None = None
    gl_account: str | None = None
    cost_center: str | None = None
    department: str | None = None


class SimulateRequest(BaseModel):
    invoice: SimInvoice | None = None
    invoice_id: str | None = None


class SimulationStep(BaseModel):
    step_number: int
    type: str
    name: str
    outcome: str
    detail: str


class SimulationResult(BaseModel):
    path: list[SimulationStep]
    terminal_state: str
    warnings: list[str] = []


class WorkflowExport(BaseModel):
    schema_version: int = 1
    name: str
    description: str | None = None
    steps_config: dict


class ImportWorkflowRequest(BaseModel):
    name: str | None = None
    definition: WorkflowExport


class CreateFromTemplateRequest(BaseModel):
    template_key: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=255)


class CreateVersionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


# ---------- request schemas ----------


class AssignReviewerRequest(BaseModel):
    user_id: str


class ApproveRequest(BaseModel):
    """Optional field corrections applied at approval time."""

    vendor: str | None = Field(default=None, max_length=255)
    invoice_number: str | None = Field(default=None, max_length=100)
    # A correction written straight onto `invoices.amount` Numeric(15, 2).
    amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    description: str | None = None
    po_number: str | None = Field(default=None, max_length=100)
    due_date: date | None = None
    gl_account: str | None = Field(default=None, max_length=100)
    cost_center: str | None = Field(default=None, max_length=100)
    # The `updated_at` of the invoice the approver loaded (`InvoiceResponse.
    # updated_at`). Supplied and stale → 409 `invoice_stale_approval` instead of
    # approving a version they never saw. Not a correction: the endpoint pops
    # it before the corrections reach the review service. `api/invoice_version`.
    expected_updated_at: datetime | None = None


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


# ---------- response schemas ----------


class WorkflowStepResponse(BaseModel):
    id: str
    step_number: int
    step_type: str
    assigned_to: str | None
    action: str | None
    completed_at: str | None
    created_at: str

    @classmethod
    def from_db(cls, step) -> "WorkflowStepResponse":
        return cls(
            id=str(step.id),
            step_number=step.step_number,
            step_type=step.step_type,
            assigned_to=str(step.assigned_to) if step.assigned_to else None,
            action=step.action,
            completed_at=step.completed_at.isoformat() if step.completed_at else None,
            created_at=step.created_at.isoformat() if step.created_at else "",
        )


class WorkflowInstanceResponse(BaseModel):
    id: str
    correlation_id: str
    definition_id: str
    invoice_id: str
    current_step: int
    state: str
    state_data: dict | None
    steps: list[WorkflowStepResponse]
    created_at: str

    @classmethod
    def from_db(cls, instance, steps: list) -> "WorkflowInstanceResponse":
        return cls(
            id=str(instance.id),
            correlation_id=str(instance.correlation_id),
            definition_id=str(instance.definition_id),
            invoice_id=str(instance.invoice_id),
            current_step=instance.current_step,
            state=instance.state,
            state_data=instance.state_data,
            steps=[WorkflowStepResponse.from_db(s) for s in steps],
            created_at=instance.created_at.isoformat() if instance.created_at else "",
        )


class AuditLogEntryResponse(BaseModel):
    id: str
    correlation_id: str
    actor_id: str | None
    actor_name: str | None = None
    action: str
    entity_type: str
    entity_id: str | None
    details: dict | None
    created_at: str

    @classmethod
    def from_db(cls, entry, actor_names: dict[str, str] | None = None) -> "AuditLogEntryResponse":
        actor_id_str = str(entry.actor_id) if entry.actor_id else None
        return cls(
            id=str(entry.id),
            correlation_id=str(entry.correlation_id) if entry.correlation_id else "",
            actor_id=actor_id_str,
            actor_name=(actor_names or {}).get(actor_id_str, None) if actor_id_str else None,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=str(entry.entity_id) if entry.entity_id else None,
            details=entry.details,
            created_at=entry.created_at.isoformat() if entry.created_at else "",
        )
