"""Approval chain engine — multi-level routing, delegation, segregation.

Handles:
- Multi-level approval chains (amount-based level resolution, sequential advancement)
- Delegation / out-of-office proxy resolution
- Segregation of duties (uploader ≠ approver)
"""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.models.user import User
from app.models.workflow import WorkflowInstance

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Segregation of duties
# ------------------------------------------------------------------


def implicated_actors(invoice: Invoice) -> set[str]:
    """Every control-plane user id segregation of duties refuses on this payable.

    ``Invoice.uploaded_by_id`` ∪ ``Invoice.segregation_actor_ids``, stringified —
    the single definition of "who is implicated in this payable".
    :func:`violates_segregation` refuses exactly this set, and the inter-company
    mirror inherits exactly this set from its source
    (``services/intercompany.route_intercompany_invoice``), so the approval gate
    and the mirror can never disagree about who shaped a payable. A third input
    added here reaches both at once.

    Stringified because ``segregation_actor_ids`` is JSONB (strings once
    round-tripped through Postgres) while an in-memory row may still hold UUID
    objects; the answer must not depend on which it is looking at. Read with
    ``getattr`` for the reason :func:`violates_segregation` gives.
    """
    ids = {str(x) for x in (getattr(invoice, "segregation_actor_ids", None) or [])}
    if invoice.uploaded_by_id is not None:
        ids.add(str(invoice.uploaded_by_id))
    return ids


def violates_segregation(
    invoice: Invoice,
    actor_id: uuid.UUID,
    approval_config: dict,
) -> bool:
    """Return True if approving as ``actor_id`` would breach segregation of duties.

    SoD is the classic AP invariant and a SOC 2 baseline — default-on. Orgs
    that need to disable it (e.g. single-operator accounts) must set
    ``require_segregation: false`` explicitly on the approval step config.

    The rule is **approver ∉ the set of actors implicated in this payable**, not
    "approver ≠ one column". Two inputs make up that set:

    * ``Invoice.uploaded_by_id`` — the one actor who caused this row to exist.
    * ``Invoice.segregation_actor_ids`` — every *other* control-plane user whose
      act shaped the payable's terms. Two creation paths have such people to
      name. A recurring template has two roles: the employee who authored the
      standing instruction, and anyone who later made a **material** edit to it
      (vendor, amount, currency, GL coding, schedule —
      ``recurring_invoice.MATERIAL_EDIT_FIELDS``);
      ``recurring_invoices.implicated_actor_ids`` computes it and
      ``generate_one`` stamps it. And an inter-company mirror copies its terms
      verbatim from the source payable, so it inherits the source's whole
      implicated set (:func:`implicated_actors`) —
      ``intercompany.route_intercompany_invoice`` stamps it. Every other
      creation path has no second actor to name.

    A single column could only ever hold one of those. Stamping the editor
    *instead* of the author would have moved the exemption rather than closed
    it — which is why the set, and every path feeding it, changed together.

    Returns False (no breach) when:

    * ``require_segregation`` is explicitly ``False`` in the approval config;
    * the actor is neither the uploader nor in the implicated set — including
      the case where ``uploaded_by_id`` is NULL **and** the set is empty, which
      means the invoice has **no employee creator at all**.

    That last branch reads fail-open, and is only sound because of an invariant
    enforced elsewhere: every path under ``app/`` that constructs an ``Invoice``
    on behalf of a signed-in employee stamps ``uploaded_by_id`` with that user
    (manual create, file upload, CSV import, the inter-company mirror). The
    paths that leave it NULL have no control-plane user to record at all —
    email intake and inbound PEPPOL (system ingestion), and supplier-portal
    submit / PO flip (the actor is a tenant-scoped ``VendorUser``, who holds no
    employee JWT and can never reach an approval endpoint). So NULL provably
    means "not created by anyone who could approve it", and self-approval is
    impossible by construction rather than by this check. None of those three
    writes ``segregation_actor_ids`` either, so widening the rule to a set left
    their reading untouched.

    The recurring sweep used to be on that list and no longer is, because it
    never belonged there: nobody *ran* it, but an employee *authored* the
    template, and that is the person segregation has to exclude. Migration 0096
    added ``recurring_invoice_templates.created_by_user_id`` and
    ``recurring_invoices.generate_one`` falls back to it, so a sweep-generated
    invoice now names its author. Templates predating that column still
    generate NULL — deliberately not backfilled, since there is no honest
    author to recover — but a *material editor* of such a template still lands
    in the implicated set, so the legacy residue is an un-named author, not an
    un-named editor.

    Failing CLOSED on NULL instead was considered and rejected: it would make
    every email-intake, PEPPOL and portal-submitted invoice permanently
    unapprovable — an outage across three ingestion channels, not a
    control. The real defect was that the invariant was assumed rather than
    enforced (``services/csv_import`` quietly violated it and let an importer
    approve what they had just imported), so the fix is the enforcement:
    ``tests/test_invoice_uploader_stamping.py`` fails if a new ``Invoice(...)``
    site neither stamps the column nor is declared to have no employee actor.
    See ``docs/decisions.md``.

    ``segregation_actor_ids`` is read with ``getattr``, because the subject is
    not always an ``Invoice``: expense reports, requisitions and expense
    pre-approvals reuse this rule through ``check_segregation`` with a
    ``SimpleNamespace`` attribute shim, so the 403 wording and the opt-out stay
    shared. Those shims pass the attribute explicitly as ``None`` — none of
    those three tables records who edited a row, so there is no second actor to
    name — and the ``getattr`` default is what keeps a *future* subject that
    forgets from raising ``AttributeError`` on the approval path, where the cost
    of a 500 is an outage on the control rather than a bypass of it.

    The pure predicate is shared by ``check_segregation`` (which raises) and by
    the amount-floor auto-approve path (which degrades to human review rather
    than 403 a legitimate submission) so both honour one definition of the rule.
    """
    if approval_config.get("require_segregation", True) is False:
        return False
    # Not redundant: a ``None`` actor is nobody, and nobody is implicated in
    # anything — it must never read as a breach against a NULL uploader.
    if actor_id is None:
        return False
    return str(actor_id) in implicated_actors(invoice)


def check_segregation(
    invoice: Invoice,
    actor_id: uuid.UUID,
    approval_config: dict,
) -> None:
    """Raise 403 if the approver is implicated in the invoice they are approving.

    The raising half of :func:`violates_segregation`. ``invoice`` need not be an
    ``Invoice``: expense reports, requisitions and expense pre-approvals pass a
    ``SimpleNamespace`` shim carrying the same two attributes, so one rule and
    one 403 string serve every "decider ≠ requester" surface.
    """
    if violates_segregation(invoice, actor_id, approval_config):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Segregation of duties: a user involved in creating this invoice "
                "cannot also approve it."
            ),
        )


# ------------------------------------------------------------------
# Delegation / out-of-office
# ------------------------------------------------------------------


async def resolve_assignee(
    user_id: uuid.UUID,
    control_db: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Check if the target user is OOO and resolve to their delegate.

    Returns (effective_assignee_id, original_id_or_none).
    If no delegation is active, returns (user_id, None).
    """
    result = await control_db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return user_id, None

    if user.delegate_to_id and user.delegate_until and user.delegate_until > datetime.now(UTC):
        return user.delegate_to_id, user_id

    return user_id, None


# ------------------------------------------------------------------
# Multi-level chain helpers
# ------------------------------------------------------------------


def _evaluate_routing_rule(rule: dict, attrs: dict) -> bool:
    """Evaluate a single RoutingRule against an attrs dict (extracted from
    the invoice). Unknown fields and unknown operators short-circuit to
    True so a stale UI config cannot harden into a 403 / approval block."""
    field = rule.get("field")
    op = rule.get("operator")
    value = rule.get("value")
    actual = attrs.get(field) if field else None

    if op == "eq":
        return actual == value
    if op == "ne":
        return actual != value
    if op == "in":
        return actual in (value or []) if isinstance(value, list) else False
    if op == "not_in":
        return actual not in (value or []) if isinstance(value, list) else True
    if op == "starts_with":
        return bool(actual) and isinstance(value, str) and str(actual).startswith(value)
    # Unknown operator — fail open so the level still applies.
    return True


def _level_routing_matches(level: dict, attrs: dict) -> bool:
    """All routing_rules on a level AND-compose. An empty rules list always
    matches."""
    rules = level.get("routing_rules") or []
    return all(_evaluate_routing_rule(rule, attrs) for rule in rules)


def _to_decimal(value) -> Decimal | None:
    """Coerce a money-ish value (Decimal / int / numeric str / float) to Decimal
    for exact comparison. Returns None for None / unparseable. Floats go through
    str() so a config literal like 5000.0 lands as Decimal('5000.0'), not the
    binary-float artefact Decimal(float) would produce."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# What a money threshold is measured against
# ------------------------------------------------------------------


@dataclass(frozen=True)
class GateAmount:
    """An amount as a money GATE must see it — in the currency the thresholds
    are denominated in, or an explicit declaration that it could not be.

    Every approval money control in this codebase (``max_invoice_amount``,
    ``require_cfo_above``, ``auto_approve_below``, and the approval chain's
    per-level ``min_amount`` / ``max_amount`` bands) is a **bare number** on a
    JSONB config with no currency of its own. It is denominated in the org's
    **reporting currency** — the same convention
    ``payments.cfo_approval_above`` follows (see
    ``services/payment_controls.cfo_approval_decision``) and
    ``settings.expense_approval.cfo_threshold`` follows. Comparing one against
    a raw ``Invoice.amount`` billed in another currency is not a comparison at
    all: a GBP 9,000 invoice reads as under a 10,000 gate it is USD 11,400
    above.

    ``expressible=False`` says the amount could NOT be put into that currency
    (no FX rate locked on the invoice row, or a lock that provably describes a
    different currency pair). Every consumer treats it as **fail closed** — the
    gate fires, the chain level applies, the auto-approve floor does not — never
    as a licence to compare bare numbers. It exists as a value rather than a
    convention because there are five of these comparisons and a convention is
    what the sixth one forgets.

    Build it with ``reporting_gate_amount``; a plain ``Decimal`` is still
    accepted everywhere and means "already in the gate currency", which is what
    a single-currency tenant and a pre-converted total (the expense-report
    path) both are.
    """

    amount: Decimal
    currency: str = ""
    expressible: bool = True


def _coerce_gate_amount(value) -> GateAmount:
    """Normalise a gate operand to ``GateAmount``.

    A bare number is taken at face value in the gate currency — the correct and
    unchanged reading for a single-currency tenant, and for a caller that has
    already converted. ``None`` / unparseable reads as 0, matching
    ``resolve_applicable_levels``'s long-standing behaviour."""
    if isinstance(value, GateAmount):
        return value
    return GateAmount(amount=_to_decimal(value) or Decimal(0))


def reporting_gate_amount(invoice, *, amount=None, org_settings: dict | None) -> GateAmount:
    """Express ``amount`` (default: ``invoice.amount``) in the org's reporting
    currency, at the rate already **locked on the invoice row**.

    ``amount`` is whatever figure the gate is really about — the bare invoice
    amount, the structuring aggregate (``structuring.vendor_recent_spend`` is
    scoped to the invoice's own currency, so the sum is single-currency and one
    rate prices all of it), or a reconciled PO total. It must be denominated in
    the INVOICE's currency; this converts it once.

    No FX call is made. The rate comes from
    ``invoice_warnings._refresh_reporting_amount``, which every write path runs
    through ``refresh_warnings``. Fetching a rate here instead would make the
    same invoice pass or fail a control depending on the minute it was
    evaluated, and would let a market move retroactively change a decision
    already recorded on the audit trail.

    Never raises: an invoice with no usable lock comes back
    ``expressible=False``, which every gate reads as fail-closed.
    """
    from app.services.currency_conversion import (
        reporting_amount_at_locked_rate,
        resolve_reporting_currency,
    )

    reporting_currency = resolve_reporting_currency(org_settings)
    raw = _to_decimal(getattr(invoice, "amount", None) if amount is None else amount) or Decimal(0)
    converted, unconverted = reporting_amount_at_locked_rate(
        amount=raw,
        currency=getattr(invoice, "currency", None),
        reporting_currency=reporting_currency,
        persisted_reporting_currency=getattr(invoice, "reporting_currency", None),
        persisted_reporting_source_currency=getattr(invoice, "reporting_source_currency", None),
        persisted_fx_rate=getattr(invoice, "reporting_fx_rate", None),
    )
    return GateAmount(amount=converted, currency=reporting_currency, expressible=not unconverted)


def finite_money_threshold(value) -> Decimal | None:
    """The usable Decimal a configured money threshold denotes, or ``None``.

    ``None`` means "there is no threshold here to compare against" — the value
    was unset, unparseable (a settings typo, a hand-edited / imported
    ``steps_config``), or non-finite. A caller that gates on the result must
    decide what "no usable threshold" means for ITS control and fail in the safe
    direction; this helper never raises and never guesses.

    `NaN` and `Infinity` are lumped in with unparseable deliberately: `NaN`
    raises on an ordering comparison (a 500 out of whatever gate touched it) and
    `Infinity` makes ``amount > threshold`` silently False — a control that
    quietly stops existing. Neither is a threshold anyone meant to configure.
    """
    threshold = _to_decimal(value)
    if threshold is None or not threshold.is_finite():
        return None
    return threshold


def _money_gate_applies(threshold_raw, amount, *, gate: str, consequence: str) -> bool:
    """Shared fail-CLOSED body behind ``cfo_gate_applies`` / ``max_amount_gate_applies``.

    - threshold explicitly unset (``None``) → ``False`` (no gate configured).
    - threshold present but unusable (see ``finite_money_threshold``) → ``True``.
      A configured-but-malformed money control must fire, never silently skip
      itself — the only safe direction. The malformed value is logged PII-free
      (it is a money threshold, not a secret) so an admin can fix it, and we
      never raise, so one bad settings write can't brick the approval queue with
      a 500.
    - amount not expressible in the gate's currency (see ``GateAmount``) →
      ``True``. There is no comparison to make, and the two ways of having no
      comparison — a broken threshold and an unpriceable amount — must fail the
      same direction. Comparing the raw figures instead is how a JPY 1,000,000
      invoice clears a 10,000 gate.
    - otherwise ``amount > threshold``.

    ``amount`` is a ``GateAmount`` or a bare number meaning "already in the gate
    currency". Comparison is exact-Decimal (money is never float).
    """
    if threshold_raw is None:
        return False
    threshold = finite_money_threshold(threshold_raw)
    if threshold is None:
        logger.error(
            "%s money threshold is unparseable (%r); %s (fail-closed)",
            gate,
            threshold_raw,
            consequence,
        )
        return True
    gate_amount = _coerce_gate_amount(amount)
    if not gate_amount.expressible:
        logger.warning(
            "%s money threshold cannot be compared — the amount is not expressible in %s "
            "(no locked FX rate for this row); %s (fail-closed)",
            gate,
            gate_amount.currency or "the reporting currency",
            consequence,
        )
        return True
    return gate_amount.amount > threshold


def cfo_gate_applies(threshold_raw, amount: Decimal) -> bool:
    """Does the CFO-approval money gate apply to an invoice/report of ``amount``?

    Returns ``True`` when the amount must carry CFO sign-off. The single
    fail-CLOSED decision shared by every CFO-threshold site (human approval, the
    expense-report approval gate, the auto-approve revoke check, and the
    exception-agent resolvers). See ``_money_gate_applies`` for the contract.
    """
    return _money_gate_applies(
        threshold_raw,
        amount,
        gate="auto-approval",
        consequence="requiring human (CFO) approval",
    )


def max_amount_gate_applies(threshold_raw, amount: Decimal) -> bool:
    """Does the hard ``max_invoice_amount`` cap reject an amount of ``amount``?

    The cap's sibling of ``cfo_gate_applies``, with the identical fail-CLOSED
    contract — and it exists because the two call sites that enforce this cap
    (``review._enforce_approval_thresholds`` and
    ``extraction.decide_auto_approve``) coerced the raw config value with a bare
    ``Decimal(str(...))``. A non-numeric ``max_invoice_amount`` — which
    ``POST /api/workflows/import`` accepts, since it takes ``steps_config`` as a
    free-form dict that no Pydantic ``Decimal`` field constrains — therefore
    raised ``InvalidOperation`` out of the gate: a 500 on EVERY approval for that
    workflow (the queue bricked), and invoices dropped to ``failed`` on the
    extraction path. A misconfigured control must refuse loudly, not crash.
    """
    return _money_gate_applies(
        threshold_raw,
        amount,
        gate="max-invoice-amount",
        consequence="refusing the approval",
    )


def resolve_applicable_levels(
    chain: list[dict],
    amount: GateAmount | Decimal | int | float | str,
    *,
    invoice_attrs: dict | None = None,
) -> list[dict]:
    """Filter chain levels to those that apply to this invoice.

    A level applies when:
    - min_amount is None OR amount >= min_amount
    - max_amount is None OR amount <= max_amount
    - every routing_rule evaluates True against `invoice_attrs`

    Amount comparison is exact-Decimal (invariant: money is never float). The
    invoice amount and the configured min/max thresholds are both coerced to
    Decimal, so a boundary invoice never mis-routes to the wrong approver tier
    on binary-float drift.

    The band bounds are bare numbers denominated in the org's **reporting
    currency**, like every other approval money threshold — so ``amount`` should
    be a ``GateAmount`` from ``reporting_gate_amount``. When it reports
    ``expressible=False`` the bands are **not applied at all** and every
    routing-rule-matching level is returned: fail closed for a chain means MORE
    approvers, since an empty result means no chain requirement. Filtering on
    unpriceable figures could instead drop the senior level a large foreign
    invoice was supposed to route to — the silent version of skipping the CFO.

    `invoice_attrs` keys map onto RoutingField (gl_account, cost_center,
    department, vendor_id). Callers should populate it from the Invoice
    row; missing keys behave like None and only match `ne` / `not_in`
    rules.
    """
    attrs = invoice_attrs or {}
    gate_amount = _coerce_gate_amount(amount)
    amt = gate_amount.amount
    if not gate_amount.expressible:
        logger.warning(
            "approval-chain amount bands cannot be applied — the amount is not expressible "
            "in %s (no locked FX rate for this row); every routing-matched level applies "
            "(fail-closed)",
            gate_amount.currency or "the reporting currency",
        )
    applicable = []
    for level in chain:
        if gate_amount.expressible:
            min_amt = _to_decimal(level.get("min_amount"))
            max_amt = _to_decimal(level.get("max_amount"))
            if min_amt is not None and amt < min_amt:
                continue
            if max_amt is not None and amt > max_amt:
                continue
        if not _level_routing_matches(level, attrs):
            continue
        applicable.append(level)
    return applicable


def invoice_routing_attrs(invoice) -> dict:
    """Extract routing-relevant attributes off an Invoice row. `department`
    falls back to the GL-account prefix if no explicit column exists yet."""
    return {
        "gl_account": getattr(invoice, "gl_account", None),
        "cost_center": getattr(invoice, "cost_center", None),
        "department": getattr(invoice, "department", None),
        "vendor_id": str(invoice.vendor_id) if getattr(invoice, "vendor_id", None) else None,
    }


#: The one JSONB key the multi-level approval chain lives under on
#: ``WorkflowInstance.state_data``. Nothing else under ``app/`` spells the
#: string: every read, write, clear and SQL predicate goes through this module,
#: and ``tests/test_approval_chain_state_owner.py`` is the drift guard that
#: keeps it that way.
CHAIN_STATE_KEY = "approval_levels"


def chain_state_of(state_data: dict | None) -> dict:
    """The approval-chain state inside a raw ``state_data`` mapping.

    ``{}`` means "this instance has no approval chain", and a missing key, a
    stored JSON ``null`` and an empty object all mean exactly that. **A stored
    ``null`` is not a third state.** Nothing in the app writes one —
    ``init_chain_state`` writes an object and ``clear_chain_state`` removes the
    key outright — so a ``null`` can only arrive from a hand-edited row, a
    restore, or an importer that serialises absence as JSON ``null``. Every one
    of those means "no chain here"; the next approval re-initialises one.

    That is why the coercion is ``or {}`` and **not** ``.get(key, {})``. The
    two differ only when the key is present holding ``null``, and there the
    second hands back ``None`` — which every caller immediately calls
    ``.get("levels", ...)`` on. So the difference between the two spellings is
    not a routing decision, it is an ``AttributeError`` raised on the approval
    path. Both spellings were live in this codebase; the ``.get(key, {})`` one
    survived only because its callers happened to test the result for
    truthiness before subscripting it.

    A *truthy* value of the wrong shape (a list, a string) is deliberately
    passed through untouched rather than coerced to ``{}``. That is corrupt
    state, not absent state: the sweeper that meets it is built to count the
    instance as a failure and carry on
    (``approval_escalation._escalate_tenant``), whereas reading it as "no
    chain" would silently drop a real chain's requirement out of an invoice's
    approval path.

    When a chain IS present the returned dict is the same object nested inside
    ``state_data``, so ``advance_approval_chain`` / ``apply_escalation`` can
    mutate it in place and then reassign the outer mapping. Mutating the empty
    dict returned for a chainless instance persists nothing — every caller
    bails out on a falsy result instead.
    """
    return (state_data or {}).get(CHAIN_STATE_KEY) or {}


def get_chain_progress(instance: WorkflowInstance) -> dict:
    """The approval-chain state on ``instance``, or ``{}`` when it has none.

    The instance-shaped front door to :func:`chain_state_of` — see there for
    what ``{}`` covers and why the coercion is spelled the way it is.
    """
    return chain_state_of(instance.state_data)


def clear_chain_state(state_data: dict) -> None:
    """Drop the chain state from a ``state_data`` mapping, in place.

    Removes the key rather than nulling it, so no row is left carrying the
    ambiguous ``null`` :func:`chain_state_of` has to absorb. Used by
    ``review.reject_invoice``: a reworked invoice must re-run the whole chain
    from level 0, never resume at the level it was rejected on.
    """
    state_data.pop(CHAIN_STATE_KEY, None)


def init_chain_state(
    instance: WorkflowInstance,
    applicable_levels: list[dict],
) -> None:
    """Initialize the approval chain state on a workflow instance.

    Called when an invoice enters the approval phase with strategy="chain".
    """
    now_iso = datetime.now(UTC).isoformat()
    levels_state = []
    for i, level in enumerate(applicable_levels):
        levels_state.append(
            {
                "level": i,
                "name": level.get("name", f"Level {i + 1}"),
                "required": level.get("required_approvals", 1),
                "approver_ids": list(level.get("approver_ids", [])),
                "approvals": [],
                "parallel_mode": level.get("parallel_mode", "any"),
                "escalation_hours": level.get("escalation_hours"),
                "escalation_to_user_ids": list(level.get("escalation_to_user_ids", [])),
                # Stamped on level entry; the escalation sweeper compares
                # this to wall-clock now to decide if the level is stale.
                # Only the level whose `level == current_level` carries an
                # `entered_at` worth acting on.
                "entered_at": now_iso if i == 0 else None,
                "escalations": [],
            }
        )

    state = dict(instance.state_data or {})
    state[CHAIN_STATE_KEY] = {
        "levels": levels_state,
        "current_level": 0,
    }
    instance.state_data = state


async def _resolve_authorized_approvers(approver_ids: list[str]) -> set[str]:
    """Expand a level's named `approver_ids` to include each id's active
    delegate, so a delegate can act in the delegator's place. Returns the
    original ids plus every resolved delegate, all as strings."""
    from app.database import control_session_factory

    authorized = set(approver_ids)
    async with control_session_factory() as ctrl_db:
        for approver_id in approver_ids:
            try:
                approver_uuid = uuid.UUID(approver_id)
            except (ValueError, TypeError, AttributeError):
                continue
            effective_id, original_id = await resolve_assignee(approver_uuid, ctrl_db)
            if original_id is not None:
                authorized.add(str(effective_id))
    return authorized


async def check_level_approver(approver_ids: list[str], actor_id: uuid.UUID) -> None:
    """Raise 403 unless `actor_id` is a named approver (or their active
    delegate) for a step/level carrying a non-empty `approver_ids` allow-list.

    A named-approver chain exists specifically to restrict a level to
    particular people — holding a coarse role (ap_manager/cfo/admin) that
    passes the endpoint's RBAC gate is not sufficient on its own. An empty
    `approver_ids` list means unrestricted: any actor who cleared the RBAC gate
    may approve, matching legacy behaviour.
    """
    if not approver_ids:
        return
    actor_str = str(actor_id)
    if actor_str in approver_ids:
        return
    authorized = await _resolve_authorized_approvers(approver_ids)
    if actor_str not in authorized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an authorized approver for this step.",
        )


def _level_satisfied(level: dict) -> bool:
    """`any` mode: distinct approver count >= required. `all` mode: every
    listed approver has approved at least once."""
    distinct = {a["user_id"] for a in level.get("approvals", [])}
    if level.get("parallel_mode") == "all":
        return all(uid in distinct for uid in level.get("approver_ids", []))
    return len(distinct) >= level.get("required", 1)


def advance_approval_chain(
    instance: WorkflowInstance,
    actor_id: uuid.UUID,
) -> bool:
    """Record an approval and advance the chain.

    Returns True if the chain is fully complete (all levels satisfied).
    Returns False if more approvals are needed.
    """
    # Deep-copy so the nested approval/level mutations below reassign a value
    # that genuinely differs from SQLAlchemy's dirty-check baseline — see the
    # note in apply_escalation. Recording an approval mutates nested JSONB, and
    # this must persist even when nothing else on the instance changed.
    state = copy.deepcopy(instance.state_data or {})
    chain_state = chain_state_of(state)
    if not chain_state:
        return True  # no chain configured, treat as complete

    levels = chain_state.get("levels", [])
    current_idx = chain_state.get("current_level", 0)

    if current_idx >= len(levels):
        return True  # already past all levels

    current_level = levels[current_idx]
    now_iso = datetime.now(UTC).isoformat()

    # Segregation across levels. A multi-level chain exists precisely to require
    # DISTINCT sign-offs at each tier (e.g. manager -> director -> CFO). Without
    # this guard, one approver who satisfied an earlier level could keep
    # approving and single-handedly clear every remaining level — collapsing the
    # whole chain to one person and defeating the control. (`check_segregation`
    # only blocks the uploader; it says nothing about reuse across levels.) An
    # approver who already acted on a different level is refused here.
    actor_str = str(actor_id)
    for idx, lvl in enumerate(levels):
        if idx == current_idx:
            continue
        if any(a.get("user_id") == actor_str for a in lvl.get("approvals", [])):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You already approved an earlier level of this chain; "
                "a different approver is required.",
            )

    # Record this approval
    current_level["approvals"].append(
        {
            "user_id": str(actor_id),
            "at": now_iso,
        }
    )

    # Check if current level is satisfied
    if _level_satisfied(current_level):
        chain_state["current_level"] = current_idx + 1

        if chain_state["current_level"] >= len(levels):
            instance.state_data = state
            return True

        # Stamp entry time on the newly-active level so the sweeper has a
        # clock to read.
        next_level = levels[chain_state["current_level"]]
        next_level["entered_at"] = now_iso

    instance.state_data = state
    return False


# ------------------------------------------------------------------
# Escalation
# ------------------------------------------------------------------


def apply_escalation(instance: WorkflowInstance, *, now: datetime | None = None) -> bool:
    """If the current level has been stale longer than its escalation_hours,
    unblock it with `escalation_to_user_ids` and record an escalation event.
    Returns True if the instance was mutated.

    `any` mode: the escalation targets are ADDED to `approver_ids` — a new
    eligible approver who can independently clear the level's `required`
    count, unaffected by whether the original approvers ever act.

    `all` mode: every id in `approver_ids` must approve, so simply appending
    would make an already-stuck level need MORE sign-offs than before —
    the opposite of "unblock" (issue #128). Instead, every approver who
    hasn't yet approved is SUBSTITUTED with the escalation targets (approvers
    who already signed off are kept — their approval still counts); the
    level's requirement shrinks to "already-approved + escalation targets"
    rather than growing to "everyone, plus the escalation targets too".

    An **unrestricted** level (empty `approver_ids`) is a no-op in BOTH modes.
    `check_level_approver` treats an empty allow-list as "any actor who cleared
    the endpoint's RBAC gate may approve", so the escalation targets are
    already eligible and there is nothing to add. Writing them in would turn
    `[]` into `[target…]` — *narrowing* an open level to those users alone and
    403-ing the entire AP team that could approve it a moment earlier. That is
    the same inversion issue #128 fixed for `all` mode, arriving through the
    empty-list case instead; an escalation must never shrink who may approve.
    (Such a level is never eligibility-blocked in the first place: `any` mode
    counts distinct approvals without consulting `approver_ids`, and `all` mode
    over an empty list is satisfied by the first approval.)

    Idempotent — once a level is escalated to a given user set, re-running
    is a no-op."""
    now = now or datetime.now(UTC)
    # Deep-copy, not shallow: the escalation mutates objects *nested* inside
    # the JSONB (a level's approver_ids / escalations). A shallow copy shares
    # those nested objects with the value SQLAlchemy loaded, so the in-place
    # mutation also changes its dirty-check baseline — the reassignment then
    # looks value-equal and the UPDATE is skipped. The sweeper changes nothing
    # else on the instance to drag the column along, so the escalation would
    # silently never persist. A deep copy keeps the baseline pristine so the
    # commit actually writes the change.
    state = copy.deepcopy(instance.state_data or {})
    chain_state = chain_state_of(state)
    if not chain_state:
        return False
    levels = chain_state.get("levels", [])
    current_idx = chain_state.get("current_level", 0)
    if current_idx >= len(levels):
        return False
    level = levels[current_idx]
    hours = level.get("escalation_hours")
    targets = level.get("escalation_to_user_ids") or []
    entered_at = level.get("entered_at")
    if not hours or not targets or not entered_at:
        return False
    try:
        entered_dt = datetime.fromisoformat(entered_at)
    except ValueError:
        return False
    age = now - entered_dt
    if age < timedelta(hours=hours):
        return False

    approver_ids = level.get("approver_ids") or []
    if not approver_ids:
        # Unrestricted level — see the docstring. Everyone the RBAC gate admits
        # can already approve it, so escalating can only take eligibility away.
        return False

    existing = set(approver_ids)
    new_targets = [uid for uid in targets if uid not in existing]
    if not new_targets:
        return False  # already escalated to these users — idempotent

    if level.get("parallel_mode") == "all":
        # Substitute every NOT-YET-APPROVED approver with the escalation
        # targets, rather than appending on top of the existing requirement.
        # An 'all' level needing {A, B} must not become {A, B, C} (harder to
        # clear if B is genuinely unavailable) — it becomes {A, C} once A has
        # already approved, so C alone can now clear the level.
        approved_ids = {a["user_id"] for a in level.get("approvals", [])}
        kept = [uid for uid in level.get("approver_ids", []) if uid in approved_ids]
        level["approver_ids"] = kept + [uid for uid in new_targets if uid not in kept]
    else:
        # Append, preserving the configured order. `list(set | set)` produced a
        # PYTHONHASHSEED-dependent ordering, so the same escalation rewrote the
        # JSONB differently run to run — noise in an audit-visible column.
        level["approver_ids"] = approver_ids + new_targets
    level.setdefault("escalations", []).append(
        {
            "at": now.isoformat(),
            "added_user_ids": new_targets,
            "after_hours": hours,
        }
    )
    instance.state_data = state
    return True
