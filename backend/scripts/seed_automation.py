"""Seed the Automation section (`/experiments`, `/adaptive`) with a believable
history, so those pages show computed results rather than empty and
"not enough data" states.

Neither page has a table of its own to fill. Adaptive Workflows is computed on
read from the tenant's `audit_log`: `invoice.approved` / `invoice.rejected`
(approval patterns, suggestions, the threshold recommendation),
`invoice.auto_approved` and `invoice.voided_return_to_approved` (the feedback
loop and the routing down-weight), and each invoice's `ready_for_review`
transition (the time-to-approve clock). An experiment's results are computed
the same way, over the invoices recorded in its `assignments`. The main seed
writes none of those rows, so on a fresh tenant every panel sits below its
minimum sample. This module writes the history that would have produced them:

* **About 100 back-dated invoices**, spread over the last ~170 days, each carrying
  the audit trail the app writes for that path. The trail is uploaded →
  extraction completed → approved → payment scheduled → paid → completed,
  with rejections, resubmissions and voids where the plan calls for them. Each
  invoice also gets a workflow instance holding the config it actually ran
  under, and a `Payment` for every scheduling.
* **Three experiments on the default workflow**: one concluded with a winner,
  one running, and one draft. Every invoice created while an experiment was
  running is assigned to it with the real `assign_variant` hash, and its
  history follows that variant's config. An invoice that landed in the
  concluded test's "auto-approve under $500" arm really was auto-approved,
  which is where the feedback loop's auto-approval population comes from.

The plan is shaped to clear every documented minimum (see
`backend/docs/adaptive-workflows.md`). Three vendors carry 12+ spotless approvals
each, which feeds the suggestions and the threshold recommendation. Two more
carry corrections and rejections, so they show what *doesn't* qualify. About
two dozen invoices are auto-approved and one of them is voided, keeping the
overturn rate under the 5% brake. The three approvers differ in speed, and the
CFO has one overturned approval, so routing has something to rank on.
`tests/test_seed_automation.py` pins those minimums, so a change to a gate or to
this plan cannot silently put a panel back into its empty state.

Invoice, experiment and payment ids are uuid5s over the org id. That keeps
`assign_variant`'s split (a hash of the two ids) identical on every run, so the
seeded results are reproducible.

**Demo tenants only.** `seed.py` calls this for acme, never for the e2e worker
tenants: those specs count invoices, and a running experiment re-routes every
new invoice on its workflow. It needs one user in each of the four system roles, since approvers
differ from the uploader (segregation of duties). It is additive and idempotent:
it does nothing if its first invoice already exists.

Usage (from `backend/`):

    python scripts/seed_automation.py                       # default: feoh_acme
    python scripts/seed_automation.py --tenant feoh_acme

`seed_automation(session, org_id)` never commits; the caller owns the
transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import math
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported — same
# reasoning (and the same E402 constraint) as `seed_extras.py`.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.user import Role, User, UserRole
from app.models.vendor import Vendor
from app.models.workflow import AuditLog, WorkflowDefinition, WorkflowInstance, WorkflowStep
from app.models.workflow_experiment import WorkflowExperiment
from app.schemas.workflow_experiments import ExperimentCreate
from app.services.approval_signature import build_signature_detail
from app.services.workflow_experiments import VARIANT_B, assign_variant

# Fixed namespace for every id this module mints (see the module docstring).
_NS = uuid.UUID("6f1d0c8e-5a8b-4c55-9e57-3a2f8b0d7c41")

# The four system roles the plan casts. The ap_clerk uploads; the other three
# approve — never the uploader (segregation of duties). On acme these are Clara
# Clerk, Alice Admin, Marcus Manager and Frank CFO.
_ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")

# Experiment windows, in days before the seed runs. Every invoice created inside
# a window is assigned to that experiment; the gap between them assigns nothing.
_EXP_AUTO_START, _EXP_AUTO_END = 171, 75
_EXP_CFO_START = 62

# The concluded test's variant B auto-approves at or under this amount.
_AUTO_BELOW = Decimal("500.00")
# The running test's variant B routes anything over this to the CFO.
_CFO_ABOVE = Decimal("10000.00")


def _id(org_id: uuid.UUID, kind: str, key: object) -> uuid.UUID:
    return uuid.uuid5(_NS, f"{org_id}:{kind}:{key}")


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"))


@dataclass(frozen=True)
class _VendorPlan:
    """One vendor's slice of the history.

    ``approvers`` rotate across the vendor's invoices. ``hours`` bounds the
    time-to-approve. The index sets name which invoices (0-based) were approved
    with a field correction, rejected once before approval, or had their payment
    voided after approval.
    """

    name: str
    code: str
    gl: str
    count: int
    low: Decimal
    high: Decimal
    approvers: tuple[str, ...]
    hours: tuple[int, int]
    first_day: int = 170
    last_day: int = 4
    corrected: frozenset[int] = field(default_factory=frozenset)
    rejected_first: frozenset[int] = field(default_factory=frozenset)
    voided: frozenset[int] = field(default_factory=frozenset)


# Three spotless vendors. 12+ human approvals each, no rejections and no
# corrections, which is what the suggestion and threshold gates require.
# Amounts stay above `_AUTO_BELOW`, so the auto-approve arm never takes one of
# their invoices away from a human.
_CLEAN = (
    _VendorPlan("Cloud Services Inc", "CSI", "6300", 14, Decimal("2150"), Decimal("2980"),
                ("ap_manager",), (3, 20)),
    _VendorPlan("Office Supplies Co", "OSC", "6100", 13, Decimal("520"), Decimal("1460"),
                ("ap_manager", "admin"), (6, 30)),
    _VendorPlan("Facility Services Ltd", "FSL", "6400", 12, Decimal("1840"), Decimal("3720"),
                ("admin",), (12, 48)),
)  # fmt: skip

# Two vendors that do NOT qualify, so the page shows the contrast: corrections,
# rejections, a slow CFO, and one approval overturned by a void.
_NOISY = (
    _VendorPlan("Marketing Agency Pro", "MAP", "6500", 10, Decimal("3200"), Decimal("14800"),
                ("ap_manager", "cfo"), (30, 110),
                corrected=frozenset({1, 4, 7}), rejected_first=frozenset({2, 8})),
    _VendorPlan("Legal Partners LLP", "LPL", "6600", 8, Decimal("6400"), Decimal("13900"),
                ("cfo",), (70, 200),
                rejected_first=frozenset({3}), voided=frozenset({5})),
)  # fmt: skip

# Small-ticket vendors, all inside the concluded experiment's window and all
# under `_AUTO_BELOW`, so each one in its auto-approve arm is auto-approved.
# `_sized_small_plans` replaces each `count` per org (see there). One
# auto-approved Catering payment is voided: the single overturn the feedback
# loop measures.
_SMALL = (
    _VendorPlan("Transport Logistics", "TRL", "6700", 24, Decimal("150"), Decimal("480"),
                ("ap_manager",), (2, 14), first_day=169, last_day=77),
    _VendorPlan("Catering Solutions", "CAT", "6800", 24, Decimal("160"), Decimal("470"),
                ("ap_manager",), (2, 14), first_day=168, last_day=76),
)  # fmt: skip

# Auto-approvals per small-ticket vendor. Two vendors → 24, with one voided:
# a ~4.2% overturn rate, under the feedback loop's 5% brake, so the threshold
# raise still stands. 20 with one void is exactly 5.0%, and the brake engages.
_AUTO_PER_SMALL_VENDOR = 12


def _sized_small_plans(org_id: uuid.UUID, auto_test: WorkflowExperiment) -> list[_VendorPlan]:
    """`_SMALL`, each vendor sized so EXACTLY `_AUTO_PER_SMALL_VENDOR` of its
    invoices land in the concluded test's auto-approve arm.

    Which arm an invoice lands in is `assign_variant`'s hash of the invoice and
    experiment ids, both uuid5s over the org id. A fixed count therefore gave
    each org a different number of auto-approvals: 24 in one tenant, 20 on acme,
    which put acme's overturn rate exactly on the brake. The hash needs no
    database, so each vendor walks its candidate ids and stops at the one that
    fills the arm; the human-reviewed arm gets whatever fell there first (about
    as many, on average).

    The human-reviewed invoices carry the GL corrections, chosen from that arm
    by construction. Spotless, a vendor with 12+ of them would join the clean
    set the threshold is argued from, and every 4th one from the second on
    guarantees a correction in exactly that case. A fixed index list could land
    wholly in the auto arm for some org.
    """
    sized = []
    for plan in _SMALL:
        auto: list[int] = []
        human: list[int] = []
        while len(auto) < _AUTO_PER_SMALL_VENDOR:
            i = len(auto) + len(human)
            assert i < 10 * plan.count, "assign_variant stopped splitting"
            inv_id = _id(org_id, "invoice", f"{plan.code}-{i}")
            variant = assign_variant(
                str(inv_id), str(auto_test.id), split_a_pct=auto_test.split_a_pct
            )
            (auto if variant == VARIANT_B else human).append(i)
        sized.append(replace(plan, count=len(auto) + len(human), corrected=frozenset(human[1::4])))
    return sized


def _scatter(plan: _VendorPlan, i: int) -> Decimal:
    """Invoice ``i``'s position in ``[0, 1]`` — a permutation of the plan's
    indices, so values are spread out of date order and none repeats. The stride
    must be coprime with the count, or the walk revisits a few values only."""
    stride = next(k for k in (7, 5, 3, 11, 13) if math.gcd(k, plan.count) == 1)
    return Decimal((i * stride) % plan.count) / Decimal(max(plan.count - 1, 1))


def _amount(plan: _VendorPlan, i: int) -> Decimal:
    return _q(plan.low + (plan.high - plan.low) * _scatter(plan, i))


def _day(plan: _VendorPlan, i: int) -> float:
    """Days-ago for invoice ``i``, evenly spaced from first_day to last_day."""
    span = plan.first_day - plan.last_day
    return plan.first_day - span * i / max(plan.count - 1, 1)


def _with_approval(steps_config: dict, **approval_cfg) -> dict:
    """A copy of ``steps_config`` with keys merged into the approval step's config."""
    cfg = copy.deepcopy(steps_config)
    for step in cfg.get("steps", []):
        if step.get("type") == "approval":
            step["config"] = {**(step.get("config") or {}), **approval_cfg}
    return cfg


class _Writer:
    """Writes each invoice's rows with the audit trail the app would write for them."""

    def __init__(self, session, *, org_id, entity_id, users, now):
        self.session = session
        self.org_id = org_id
        self.entity_id = entity_id
        self.users = users
        self.now = now
        self.counts = {"invoices": 0, "audit_rows": 0, "payments": 0, "auto_approved": 0}

    def audit(self, inv: Invoice, at: datetime, action: str, actor: str | None, details: dict):
        row = AuditLog(
            correlation_id=inv.correlation_id,
            organization_id=self.org_id,
            actor_id=self.users[actor] if actor else None,
            action=action,
            entity_type="invoice",
            entity_id=inv.id,
            details=details,
        )
        row.created_at = at
        self.session.add(row)
        self.counts["audit_rows"] += 1

    def move(self, inv: Invoice, at: datetime, action: str, actor: str | None, new: str, **extra):
        """A status transition, written as `workflow_engine.transition_invoice` does."""
        self.audit(inv, at, action, actor, {**extra, "old_status": inv.status, "new_status": new})
        inv.status = new

    def approve(self, inv: Invoice, at: datetime, actor: str, changes: dict | None):
        """`review.approve_invoice`'s finalising write, signature included.

        Signed exactly as the app signs, so the SOX verifier reads these rows
        as `valid` under the same key. With no key configured (the seed's
        default environment) the block is omitted and they read as `unsigned`.
        """
        details: dict = {}
        if changes:
            details["changes"] = changes
        signature = build_signature_detail(
            invoice_id=inv.id,
            amount=inv.amount,
            actor_id=self.users[actor],
            decision="approved",
            timestamp=at,
            signing_key=settings.approval_signing_key,
        )
        if signature:
            details["signature"] = signature
        self.move(inv, at, "invoice.approved", actor, "approved", **details)
        inv.approval_date = at.date()
        inv.approved_by = self.users.names[actor]

    def pay(self, inv: Invoice, scheduled_at: datetime, *, ref: str, void: bool) -> None:
        """Schedule → (optionally void and reschedule) → paid → done.

        Stops at the last step that has already happened. A recently approved
        invoice is left `approved` or `payment_scheduled`, as it would be.
        """
        cutoff = self.now - timedelta(hours=1)
        if scheduled_at > cutoff:
            return
        attempt = 0
        if void:
            voided_at = scheduled_at + timedelta(days=1)
            self._schedule(inv, scheduled_at, ref=f"{ref}-{attempt}")
            self._payment.status = "voided"
            self._payment.failure_reason = (
                f"Voided by {self.users.names['ap_manager']}: remittance details queried"
            )
            self.move(inv, voided_at, "invoice.voided_return_to_approved", "ap_manager", "approved")
            attempt += 1
            scheduled_at = voided_at + timedelta(days=1)
            if scheduled_at > cutoff:
                return
        self._schedule(inv, scheduled_at, ref=f"{ref}-{attempt}")
        paid_at = scheduled_at + timedelta(days=2)
        if paid_at > cutoff:
            return
        self._payment.status = "completed"
        self._payment.completed_at = paid_at
        self.move(inv, paid_at, "invoice.paid_via_erp_sync", None, "paid")
        done_at = paid_at + timedelta(days=1)
        if done_at > cutoff:
            return
        self.move(inv, done_at, "invoice.completed", None, "done")

    def _schedule(self, inv: Invoice, at: datetime, *, ref: str) -> None:
        self.move(inv, at, "invoice.payment_scheduled", "ap_manager", "payment_scheduled")
        self._payment = Payment(
            id=_id(self.org_id, "payment", ref),
            correlation_id=uuid.uuid4(),
            invoice_id=inv.id,
            amount=inv.amount,
            method="ach",
            status="submitted",
            reference=f"PAY-{ref}",
            provider="mock",
            submitted_at=at,
            entity_id=self.entity_id,
        )
        self._payment.created_at = at
        self.session.add(self._payment)
        self.counts["payments"] += 1


class _Users(dict):
    """`{role: user_id}` plus `.names` — the display name `approved_by` stores."""

    names: dict[str, str]


async def _load_users(org_id: uuid.UUID, control_factory) -> _Users | None:
    """One control-plane user per role in ``_ROLES``, or None if any is missing.

    The earliest-created holder wins when a role has several, so the cast is
    stable across runs.
    """
    async with control_factory() as ctrl:
        rows = (
            await ctrl.execute(
                select(Role.name, User.id, User.full_name)
                .join(UserRole, UserRole.role_id == Role.id)
                .join(User, User.id == UserRole.user_id)
                .where(User.organization_id == org_id, Role.name.in_(_ROLES))
                .order_by(User.created_at, User.id)
            )
        ).all()
    users = _Users()
    users.names = {}
    for role, uid, name in rows:
        if role not in users:
            users[role] = uid
            users.names[role] = name or role
    return users if all(r in users for r in _ROLES) else None


async def seed_automation(
    session,
    org_id: uuid.UUID,
    *,
    now: datetime | None = None,
    control_factory=control_session_factory,
) -> dict:
    """Write the Automation-section history into one tenant.

    Returns a tally, or ``{}`` when it did nothing (already seeded, or the
    tenant lacks what the plan needs). Never commits. ``control_factory`` is
    the control-plane session factory the role users are read through; the test
    harness passes its own per-slot one.
    """
    now = now or datetime.now(UTC)
    first_id = _id(org_id, "invoice", f"{_CLEAN[0].code}-0")
    if (await session.execute(select(Invoice.id).where(Invoice.id == first_id))).first():
        print("  Automation history already seeded — skipping")
        return {}

    users = await _load_users(org_id, control_factory)
    if users is None:
        print(
            "  Automation seed needs a user in each of "
            "admin / ap_manager / ap_clerk / cfo — skipping"
        )
        return {}

    plans = (*_CLEAN, *_NOISY, *_SMALL)
    vendors = dict(
        (
            await session.execute(
                select(Vendor.name, Vendor.id).where(Vendor.name.in_([p.name for p in plans]))
            )
        ).all()
    )
    default_def = (
        await session.execute(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.organization_id == org_id)
            .order_by(WorkflowDefinition.is_default.desc(), WorkflowDefinition.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if default_def is None or len(vendors) < len(plans):
        print("  Automation seed needs the full demo seed's vendors and workflow — skipping")
        return {}
    entity_id = (
        await session.execute(text("SELECT id FROM entities WHERE is_default LIMIT 1"))
    ).scalar()

    base = default_def.steps_config
    exp_auto = _experiment(
        org_id,
        entity_id,
        default_def,
        key="auto-below-500",
        name="Auto-approve invoices under $500",
        description=(
            "Does letting low-value invoices skip manual review raise the touchless "
            "rate without letting bad invoices through?"
        ),
        config_b=_with_approval(base, auto_approve_below=str(_AUTO_BELOW)),
        primary_metric="touchless_rate_pct",
        status="concluded",
        started_at=now - timedelta(days=_EXP_AUTO_START),
        ended_at=now - timedelta(days=_EXP_AUTO_END),
    )
    exp_cfo = _experiment(
        org_id,
        entity_id,
        default_def,
        key="cfo-above-10k",
        name="CFO sign-off above $10,000",
        description=(
            "What does routing large invoices to the CFO cost in approval time? "
            "Variant B adds a CFO gate over $10,000."
        ),
        config_b=_with_approval(base, require_cfo_above=str(_CFO_ABOVE)),
        primary_metric="time_to_approval_days",
        status="running",
        started_at=now - timedelta(days=_EXP_CFO_START),
    )
    exp_draft = _experiment(
        org_id,
        entity_id,
        default_def,
        key="auto-below-1000",
        name="Raise auto-approve to $1,000",
        description="Follow-up to the $500 test: is twice the ceiling still safe?",
        config_b=_with_approval(base, auto_approve_below="1000.00"),
        primary_metric="touchless_rate_pct",
        status="draft",
    )
    experiments = (exp_auto, exp_cfo, exp_draft)
    session.add_all(experiments)
    await session.flush()
    plans = (*_CLEAN, *_NOISY, *_sized_small_plans(org_id, exp_auto))

    w = _Writer(session, org_id=org_id, entity_id=entity_id, users=users, now=now)
    voided_auto = False
    for plan in plans:
        for i in range(plan.count):
            days_ago = _day(plan, i)
            created = (now - timedelta(days=days_ago)).replace(minute=(i * 13) % 60, second=0)
            created = created.replace(hour=9 + (i % 7))
            amount = _amount(plan, i)
            inv = Invoice(
                id=_id(org_id, "invoice", f"{plan.code}-{i}"),
                organization_id=org_id,
                entity_id=entity_id,
                invoice_number=f"{plan.code}-{created:%y%m}-{i + 1:03d}",
                vendor_id=vendors[plan.name],
                vendor_name=plan.name,
                description=f"{plan.name} — {created:%B %Y}",
                amount=amount,
                subtotal=amount,
                tax_amount=Decimal("0.00"),
                currency="USD",
                invoice_date=(created - timedelta(days=2)).date(),
                received_date=created.date(),
                due_date=(created + timedelta(days=30)).date(),
                payment_terms="Net 30",
                gl_account=plan.gl,
                status="new",
                uploaded_by_id=users["ap_clerk"],
            )
            inv.created_at = created
            session.add(inv)
            w.counts["invoices"] += 1

            # Which experiment (if any) this invoice was created under, and the
            # config it therefore ran with — frozen onto its instance.
            exp, variant, config = None, None, base
            for candidate in (exp_auto, exp_cfo):
                if candidate.started_at <= created and (
                    candidate.ended_at is None or created < candidate.ended_at
                ):
                    exp = candidate
                    variant = assign_variant(str(inv.id), str(exp.id), split_a_pct=exp.split_a_pct)
                    config = exp.config_b if variant == VARIANT_B else exp.config_a
                    break

            w.move(
                inv,
                created,
                "invoice.uploaded",
                "ap_clerk",
                "pending",
                filename=f"{inv.invoice_number}.pdf",
                content_type="application/pdf",
            )
            if exp is not None:
                exp.assignments = {**exp.assignments, str(inv.id): variant}
                w.audit(
                    inv,
                    created,
                    "invoice.experiment_assigned",
                    None,
                    {
                        "experiment_id": str(exp.id),
                        "experiment_name": exp.name,
                        "variant": variant,
                        "workflow_definition_id": str(default_def.id),
                    },
                )

            extracted = created + timedelta(minutes=4 + i % 5)
            auto = exp is exp_auto and variant == VARIANT_B and amount <= _AUTO_BELOW
            extraction = {
                "method": "mock",
                "confidence": 0.97,
                "vendor_action": "matched",
                "vendor_id": str(inv.vendor_id),
                "gl_suggested": plan.gl,
            }
            if auto:
                w.move(
                    inv,
                    extracted,
                    "invoice.auto_approved",
                    "ap_clerk",
                    "approved",
                    auto_approved=True,
                    **extraction,
                )
                inv.approval_date = extracted.date()
                inv.approved_by = "system (auto-approve)"
                w.counts["auto_approved"] += 1
                approved_at, approver = extracted, None
                void = plan.code == "CAT" and not voided_auto
                voided_auto = voided_auto or void
            else:
                w.move(
                    inv,
                    extracted,
                    "invoice.extraction_completed",
                    "ap_clerk",
                    "ready_for_review",
                    auto_approved=False,
                    **extraction,
                )
                lo, hi = plan.hours
                # Offset from the amount's index so wait time doesn't track amount.
                share = float(_scatter(plan, (i + 3) % plan.count))
                wait = timedelta(hours=lo + (hi - lo) * share)
                approver = plan.approvers[i % len(plan.approvers)]
                if exp is exp_cfo and variant == VARIANT_B and amount > _CFO_ABOVE:
                    approver = "cfo"
                decided = extracted + wait
                if i in plan.rejected_first:
                    w.move(
                        inv,
                        decided,
                        "invoice.rejected",
                        approver,
                        "rejected",
                        reason="PO reference missing — please resubmit with the PO number",
                    )
                    resubmitted = decided + timedelta(days=1)
                    w.move(inv, resubmitted, "invoice.resubmitted", "ap_clerk", "ready_for_review")
                    decided = resubmitted + wait
                changes = None
                if i in plan.corrected:
                    changes = {"gl_account": {"old": "6000", "new": plan.gl}}
                w.approve(inv, decided, approver, changes)
                approved_at, void = decided, i in plan.voided

            w.pay(inv, approved_at + timedelta(days=1), ref=f"AUT-{plan.code}-{i}", void=void)
            _instance(
                session,
                inv,
                default_def,
                config,
                approver and users[approver],
                extracted,
                approved_at,
            )

    await session.flush()
    tally = {**w.counts, "experiments": len(experiments)}
    print(
        f"  Seeded automation history: {tally['invoices']} invoices "
        f"({tally['auto_approved']} auto-approved), {tally['payments']} payments, "
        f"{tally['audit_rows']} audit rows, {tally['experiments']} experiments "
        f"(concluded / running / draft)"
    )
    return tally


def _experiment(
    org_id, entity_id, definition, *, key, name, description, config_b, primary_metric, status,
    started_at=None, ended_at=None,
) -> WorkflowExperiment:  # fmt: skip
    """An experiment whose configs went through the API's own create schema."""
    body = ExperimentCreate(
        name=name,
        description=description,
        workflow_definition_id=definition.id,
        config_a=copy.deepcopy(definition.steps_config),
        config_b=config_b,
        primary_metric=primary_metric,
    )
    exp = WorkflowExperiment(
        id=_id(org_id, "experiment", key),
        organization_id=org_id,
        entity_id=entity_id,
        name=body.name,
        description=body.description,
        workflow_definition_id=body.workflow_definition_id,
        config_a=body.config_a,
        config_b=body.config_b,
        split_a_pct=body.split_a_pct,
        primary_metric=body.primary_metric,
        min_sample_per_variant=body.min_sample_per_variant,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        assignments={},
    )
    exp.created_at = (started_at or datetime.now(UTC)) - timedelta(days=1)
    return exp


def _instance(session, inv, definition, config, approver_id, extracted_at, approved_at) -> None:
    """The invoice's workflow instance, holding the config it actually ran under.

    The approval step is assigned to whoever decided it. An auto-approved
    invoice's approval step has no assignee, as the engine leaves it.
    """
    instance = WorkflowInstance(
        correlation_id=inv.correlation_id,
        definition_id=definition.id,
        invoice_id=inv.id,
        current_step=2,
        state="completed" if inv.status == "done" else "active",
        steps_config_snapshot=config,
    )
    session.add(instance)
    for number, step_type, action, at, assignee in (
        (1, "extraction", "extract", extracted_at, None),
        (2, "approval", "approve", approved_at, approver_id),
    ):
        step = WorkflowStep(
            correlation_id=inv.correlation_id,
            instance=instance,
            step_number=number,
            step_type=step_type,
            assigned_to=assignee,
            original_assigned_to=assignee,
            action=action,
            completed_at=at,
        )
        step.created_at = extracted_at
        session.add(step)


async def seed_automation_tenant(db_name: str, org_id: uuid.UUID) -> dict:
    """Open the tenant, seed it, commit. What `seed.py` and the CLI call."""
    engine = create_async_engine(_make_tenant_url(db_name))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            tally = await seed_automation(session, org_id)
            await session.commit()
    finally:
        await engine.dispose()
    return tally


async def _run(db_name: str) -> None:
    async with control_session_factory() as ctrl:
        org = (
            await ctrl.execute(select(Organization).where(Organization.db_name == db_name))
        ).scalar_one_or_none()
    if org is None:
        print(f"FAIL: no organization with db_name={db_name!r}. Run scripts/seed.py first.")
        return
    await seed_automation_tenant(db_name, org.id)
    print(f"Done. Visit http://{org.slug}.localhost:7777/adaptive and /experiments.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="feoh_acme", help="Tenant DB name (default: feoh_acme)")
    args = parser.parse_args()
    asyncio.run(_run(args.tenant))


if __name__ == "__main__":
    main()
