"""Review service — approve, reject, and resubmit invoices."""

import logging
import uuid
from collections.abc import Collection
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.refusals import coded_refusal
from app.config import settings
from app.models.invoice import Invoice, InvoiceStatus
from app.schemas.money import json_money_string
from app.services.rag import store_embedding
from app.services.vendor_priors import record_corrections
from app.services.workflow_engine import (
    advance_workflow,
    complete_current_step,
    create_workflow_step,
    get_step_config,
    get_workflow_instance,
    transition_invoice,
)

_log = logging.getLogger(__name__)

# The approval money gates' refusal codes (`api/refusals.coded_refusal`). The
# two "exceeded" refusals carry every figure their sentence names as typed
# params (`_enforce_approval_thresholds._gate_params`); the misconfigured one
# names none. Siblings of `approval_chain.APPROVAL_*`.
APPROVAL_MAX_AMOUNT_EXCEEDED = "approval_max_amount_exceeded"
APPROVAL_MAX_AMOUNT_MISCONFIGURED = "approval_max_amount_misconfigured"
APPROVAL_CFO_REQUIRED = "approval_cfo_required"

#: Approve or reject asked of an invoice that is not awaiting review. The
#: single in-app doors (`POST /api/invoices/{id}/approve|reject`) refuse it with
#: this code — the email / Slack / Teams doors already refused it — and the
#: review verdict (:func:`review_refusals`) states it, so the invoice modal can
#: never offer a decision on an invoice that is not in review.
INVOICE_NOT_IN_REVIEW = "invoice_not_in_review"

# ── Recalling an approval (decisions §374) ─────────────────────────────────

#: `POST /api/invoices/{id}/recall-approval` on an invoice holding no approval
#: it can take back: anything but `approved`, or a `failed` ERP push after an
#: approval. ``params.status``. The clients hide the action on this code.
INVOICE_NOT_RECALLABLE = "invoice_not_recallable"
#: Refused because the ERP may already hold the invoice's bill: a confirmed
#: push (``erp_document_id``, or an approved invoice a void walked back from
#: ``posted_in_erp``), or a failed push that may have created one
#: (``services/erp.possibly_posted``). A bill there can be paid there, and
#: nothing in FeohLedger recalls it yet (``docs/followups.md``, void-erp).
INVOICE_RECALL_IN_ERP = "invoice_recall_in_erp"
#: Refused because one of our rails is already paying the invoice: a live
#: payment (a staged, approved, exported or executed run) or a live virtual
#: card, read by ``erp_export_gate.live_claims`` (decisions §309 / §357).
#: ``params.claim`` is ``payment`` / ``virtual_card``.
INVOICE_RECALL_CLAIMED = "invoice_recall_claimed"

_RECALL_CLAIM_MESSAGES = {
    "payment": (
        "FeohLedger is already paying this invoice: it has a live payment. Cancel its "
        "payment run or void the payment before recalling the approval."
    ),
    "virtual_card": (
        "A virtual card has been issued to pay this invoice. Cancel the card before "
        "recalling the approval."
    ),
}


async def _fetch_invoice_bytes(invoice: Invoice) -> bytes | None:
    """Best-effort S3 fetch of the invoice file for RAG embedding.

    Returns None on any failure — RAG storage is a learning-side-effect, not
    a correctness requirement, so we never block the approval on it.
    """
    file_key = invoice.file_key
    if not file_key:
        return None
    try:
        # boto3 is blocking; `_get_object` runs it in a worker thread so an
        # approval never parks the event loop on an S3 round trip.
        from app.services.storage import _get_object

        content, _content_type = await _get_object(file_key)
        return content
    except Exception as exc:
        _log.warning("Failed to fetch bytes for RAG embedding of %s: %s", invoice.id, exc)
        return None


async def resolve_approval_config(
    db: AsyncSession,
    invoice: Invoice,
    instance=None,
) -> dict:
    """The approval-step config governing THIS invoice's approval.

    The frozen per-invoice snapshot first (the invariant: an in-flight invoice
    is governed by the config it entered under, not the live definition), and —
    only when there is no snapshot to read — the org's currently-active
    definition as a **fail-closed** fallback.

    That fallback is the whole point. Not every invoice has a
    ``WorkflowInstance``: the email-intake and PEPPOL-inbound ingest paths
    create the row without one, and so does any legacy / directly-inserted
    invoice. Returning ``{}`` there did not mean "no rules apply" — it meant the
    max-amount cap, the CFO gate, the structuring guard and the named-approver
    check were ALL skipped, so a $50,000 invoice that arrived by email cleared a
    $1,000 ``require_cfo_above`` on a lone ap_manager's approval. A money
    control must not be contingent on a bookkeeping row existing.

    Resolution is read-only (``resolve_active_workflow_definition``, never the
    get-or-CREATE variant) — a definition must never appear as a side effect of
    an approval. ``{}`` now means only "this org has no active definition, or
    its definition has no approval step", which is genuinely nothing to enforce.

    Pass ``instance`` when the caller already loaded it, to save a query.
    """
    from app.services.workflow_engine import resolve_active_workflow_definition

    if instance is None:
        instance = await get_workflow_instance(db, invoice.id)
    snapshot = getattr(instance, "steps_config_snapshot", None) if instance else None
    if not snapshot:
        defn = await resolve_active_workflow_definition(
            db, invoice.organization_id, getattr(invoice, "entity_id", None)
        )
        snapshot = defn.steps_config if defn else None
    if not snapshot:
        return {}
    return get_step_config(snapshot, "approval") or {}


async def _enforce_approval_thresholds(
    db: AsyncSession,
    invoice: Invoice,
    actor_roles: set[str],
    *,
    org_settings: dict | None = None,
    approval_config: dict | None = None,
) -> None:
    """Check approval thresholds from the workflow snapshot. Raises on violation."""
    from fastapi import HTTPException, status

    config = (
        approval_config
        if approval_config is not None
        else await resolve_approval_config(db, invoice)
    )
    if not config:
        return

    # Compare on the money path with Decimal, never float — a float cast of the
    # invoice amount can misjudge a boundary amount against the CFO/max gate.
    amount = Decimal(str(invoice.amount or 0))

    # Structuring guard: a same-vendor rolling-window aggregate that can push
    # the effective amount over the max/CFO gate even though THIS invoice
    # alone doesn't cross it — closes the "split one big payable into several
    # small ones, each under threshold, with distinct invoice numbers so the
    # exact-match duplicate check never fires" bypass. See services/structuring.py.
    aggregate_amount = amount
    recent_spend = Decimal(0)
    invoice_currency = (getattr(invoice, "currency", None) or "").strip().upper() or "USD"
    # The code the refusal's typed params name: the invoice's OWN currency, or
    # None when it carries none. `invoice_currency`'s "USD" stand-in stays out
    # of them — a typed field is formatted with a symbol by both clients, so a
    # guessed code would render a code-less invoice as dollars (decisions
    # §160 / §196); a None figure renders bare.
    params_currency = (getattr(invoice, "currency", None) or "").strip().upper() or None
    # The same rule for the English `message`, which several surfaces show as-is
    # (the email-action page, bulk-status skip reasons, agent rationales): a
    # code-less invoice's figure is stated bare, never as dollars.
    message_currency = f" {params_currency}" if params_currency else ""
    vendor_id = getattr(invoice, "vendor_id", None)
    structuring_window_days = 0
    if vendor_id is not None:
        from app.services.structuring import get_structuring_config, vendor_recent_spend

        s_cfg = get_structuring_config(org_settings)
        if s_cfg["enabled"]:
            structuring_window_days = s_cfg["window_days"]
            recent_spend = await vendor_recent_spend(
                db,
                vendor_id=vendor_id,
                exclude_invoice_id=invoice.id,
                window_days=structuring_window_days,
                currency=getattr(invoice, "currency", None) or "USD",
                entity_id=getattr(invoice, "entity_id", None),
            )
            aggregate_amount = amount + recent_spend

    # Both money gates below are BARE NUMBERS denominated in the org's REPORTING
    # currency — the same convention `payments.cfo_approval_above` follows (see
    # `services/payment_controls.cfo_approval_decision`) and
    # `settings.expense_approval.cfo_threshold` follows. So the figure compared
    # is the aggregate expressed in that currency, at the rate already LOCKED on
    # this invoice row, not the amount as billed. Without it a GBP 9,000 invoice
    # (USD 11,400) sailed under a USD 10,000 `require_cfo_above` and was approved
    # with no CFO signature at all. `vendor_recent_spend` is scoped to the
    # invoice's own currency, so one rate prices the whole aggregate.
    #
    # An invoice we cannot express in that currency reports `expressible=False`,
    # which `approval_chain`'s shared gate body reads as fail-CLOSED: the gate
    # fires and a human (a CFO, for the CFO gate) has to decide.
    from app.services.approval_chain import reporting_gate_amount

    gate_amount = reporting_gate_amount(invoice, amount=aggregate_amount, org_settings=org_settings)
    gate_currency = gate_amount.currency

    def _structuring_applies(threshold_dec: Decimal) -> bool:
        return recent_spend > 0 and amount <= threshold_dec

    def _structuring_note(threshold_dec: Decimal) -> str:
        if not _structuring_applies(threshold_dec):
            return ""
        return (
            f" This invoice alone is under the threshold, but combined with "
            f"{recent_spend:,.2f}{message_currency} in other recent invoices from this "
            f"vendor (last {structuring_window_days} days) it totals "
            f"{aggregate_amount:,.2f}{message_currency}."
        )

    def _measured_differs() -> bool:
        return gate_amount.expressible and not (
            gate_amount.amount == aggregate_amount and invoice_currency == gate_currency
        )

    def _compared_note() -> str:
        """What was actually measured, when it isn't the billed figure."""
        if not gate_amount.expressible:
            return (
                f" This invoice could not be expressed in {gate_currency}, the currency the "
                f"limit is set in, so it cannot be cleared against it."
            )
        if not _measured_differs():
            return ""
        return (
            f" Measured as {gate_amount.amount:,.2f} {gate_currency} — the limit is set in "
            f"{gate_currency}."
        )

    def _gate_params(threshold_dec: Decimal | None) -> dict:
        """The typed half of a money-gate refusal — every figure its sentence
        names, as an exact string beside its currency, so a client states the
        sentence (and its two optional notes) in the reader's language and
        number format. A `None` is a note that does not apply; `limit` is
        `None` only when the threshold itself is malformed."""
        structuring = threshold_dec is not None and _structuring_applies(threshold_dec)
        return {
            "amount": json_money_string(amount),
            "currency": params_currency,
            "limit": json_money_string(threshold_dec) if threshold_dec is not None else None,
            "limit_currency": gate_currency,
            "recent_spend": json_money_string(recent_spend) if structuring else None,
            "aggregate_amount": json_money_string(aggregate_amount) if structuring else None,
            "window_days": structuring_window_days if structuring else None,
            "expressible": gate_amount.expressible,
            "measured_amount": (
                json_money_string(gate_amount.amount) if _measured_differs() else None
            ),
        }

    # Both money gates below read the config through the shared fail-CLOSED
    # parsers in `approval_chain`. `steps_config` is a JSONB blob and
    # `POST /api/workflows/import` accepts it free-form (no Pydantic `Decimal`
    # field constrains it there), so a threshold can genuinely arrive as a
    # non-numeric string — and a bare `Decimal(str(...))` on it raises
    # InvalidOperation, which is a 500 on EVERY approval under that workflow,
    # legitimate ones included. A misconfigured money control must refuse
    # loudly, never crash and never silently skip itself.
    from app.services.approval_chain import _to_decimal, cfo_gate_applies, max_amount_gate_applies

    # Hard reject if over max. The parsed Decimal is used for BOTH the
    # comparison and the message; when it is unusable the gate still fires
    # (fail-closed) and the message names the misconfiguration instead of
    # formatting a value that would raise.
    max_amount = config.get("max_invoice_amount")
    if max_amount_gate_applies(max_amount, gate_amount):
        max_amount_dec = _to_decimal(max_amount)
        if max_amount_dec is None or not max_amount_dec.is_finite():
            detail = coded_refusal(
                APPROVAL_MAX_AMOUNT_MISCONFIGURED,
                "This workflow's approval step has an unusable 'max_invoice_amount'. "
                "Approval is blocked until an admin corrects the workflow definition.",
            )
        else:
            detail = coded_refusal(
                APPROVAL_MAX_AMOUNT_EXCEEDED,
                f"Invoice amount {amount:,.2f}{message_currency} exceeds maximum allowed "
                f"{max_amount_dec:,.2f} {gate_currency}."
                + _structuring_note(max_amount_dec)
                + _compared_note(),
                **_gate_params(max_amount_dec),
            )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)

    # CFO role gate for high-value invoices. `cfo_gate_applies` is the shared
    # fail-CLOSED parse: a configured-but-malformed `require_cfo_above` demands
    # CFO sign-off (never silently skips the gate) instead of raising an
    # InvalidOperation that would 500 every approval — even a legitimate CFO's —
    # and brick the queue on a single settings typo.
    cfo_threshold = config.get("require_cfo_above")
    # The gate reads `aggregate_amount`, not `amount`, so the structuring bypass
    # (split one payable into several under-threshold invoices from the same
    # vendor) can't walk under it either.
    if cfo_gate_applies(cfo_threshold, gate_amount) and "cfo" not in actor_roles:
        threshold_dec = _to_decimal(cfo_threshold)
        if threshold_dec is not None and not threshold_dec.is_finite():
            threshold_dec = None
        limit = (
            f"{threshold_dec:,.2f} {gate_currency}"
            if threshold_dec is not None
            else "the configured limit"
        )
        # A malformed threshold has no Decimal to measure the aggregate against,
        # so the note is omitted — the gate itself still fires (fail-closed).
        note = _structuring_note(threshold_dec) if threshold_dec is not None else ""
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=coded_refusal(
                APPROVAL_CFO_REQUIRED,
                f"Invoice amount {amount:,.2f}{message_currency} exceeds {limit}. "
                f"CFO approval required." + note + _compared_note(),
                **_gate_params(threshold_dec),
            ),
        )


# ── The approval gates ─────────────────────────────────────────────────────
#
# `approve_invoice` and `review_refusals` (the verdict the invoice modal reads
# before it offers Approve) run THESE functions, so the page and the endpoint
# cannot disagree on who may approve what (issue #575 § One rule, one place).
# Each raises the coded HTTPException the endpoint answers with.


def awaiting_review(invoice: Invoice) -> bool:
    """Whether ``invoice`` awaits a review decision — the ONE predicate every
    review-decision door reads: the in-app approve / reject
    (:func:`refuse_unless_in_review`), the email, Slack and Teams action links,
    and the review verdict (:func:`review_refusals`)."""
    return invoice.status == InvoiceStatus.ready_for_review


async def review_round_of(db: AsyncSession, invoice_id: uuid.UUID) -> int:
    """The invoice's review round (``email_action_token.round_from_state_data``:
    rounds ended by a rejection or a recall), read from its workflow instance.
    No instance is round 0. The one reading both sides of an out-of-app
    approval use: :func:`approval_message_facts` when a message is minted, and
    the email, Slack and Teams doors against the locked row (decisions §379)."""
    from app.services.email_action_token import round_from_state_data

    instance = await get_workflow_instance(db, invoice_id)
    return round_from_state_data(instance.state_data if instance is not None else None)


async def approval_message_facts(db: AsyncSession, invoice: Invoice) -> str:
    """``email_action_token.digest_of_invoice`` of ``invoice`` in its current
    review round: what every `invoice_assigned` notification binds its Approve /
    Reject actions to. A link minted in one round fails the facts check in any
    later one, so a recall or an in-app reject retires the earlier round's
    messages without anyone having to click them."""
    from app.services.email_action_token import digest_of_invoice

    return digest_of_invoice(invoice, review_round=await review_round_of(db, invoice.id))


def refuse_unless_in_review(invoice: Invoice) -> None:
    """409 ``invoice_not_in_review`` unless :func:`awaiting_review`.

    The single in-app approve / reject doors call it first; the email, Slack
    and Teams doors answer the same predicate in their own response shapes.
    ``POST /api/invoices/bulk/status`` does not reach here: setting a batch's
    status is a separate, documented manager capability (``new → approved`` is
    a legal transition for it), and whether it should also skip drafts is
    tracked in ``docs/followups.md``.
    """
    from fastapi import HTTPException, status

    if not awaiting_review(invoice):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=coded_refusal(
                INVOICE_NOT_IN_REVIEW,
                "This invoice is not awaiting review, so it cannot be approved or rejected.",
            ),
        )


async def _identity_gates(invoice: Invoice, actor_id: uuid.UUID, approval_config: dict) -> None:
    """The gates read BEFORE any correction: segregation of duties (the
    invoice's implicated actors may not approve it) and the single-level
    named-approver gate."""
    from app.services.approval_chain import check_level_approver, check_segregation

    check_segregation(invoice, actor_id, approval_config)
    # "specific" restricts this step to the listed approver_ids (or their active
    # delegate) — the coarse require_permission(PERM_INVOICE_APPROVE) RBAC gate
    # on the endpoint only confirms the actor holds an approving role, not that
    # they are one of the named approvers.
    if approval_config.get("approver_strategy") == "specific":
        specific_ids = approval_config.get("approver_ids") or (
            [approval_config["approver_id"]] if approval_config.get("approver_id") else []
        )
        await check_level_approver(specific_ids, actor_id)


async def _figure_gates(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
    actor_roles: set[str],
    approval_config: dict,
    org_settings: dict | None,
    receipt_recorders: Collection[str] | None,
) -> None:
    """The gates read AFTER any correction: receiving ≠ approving, then the
    max-amount and CFO money gates.

    Receiving: whoever hand-recorded a live goods receipt on the invoice's PO
    may not approve it (decisions §267) — after the corrections, so a
    ``po_number`` correction cannot re-point the invoice at a PO the approver
    received and slip past a set read for the old one. Not looked up at all when
    the step opted out of segregation. ``receipt_recorders`` is a set resolved
    up front (bulk approve), or ``None`` to look it up here.
    """
    from app.services.approval_chain import check_receiving_segregation
    from app.services.approval_chain import receipt_recorders as lookup_receipt_recorders

    if approval_config.get("require_segregation", True) is not False:
        if receipt_recorders is None:
            receipt_recorders = await lookup_receipt_recorders(db, invoice)
        check_receiving_segregation(actor_id, receipt_recorders, approval_config)
    await _enforce_approval_thresholds(
        db,
        invoice,
        actor_roles,
        org_settings=org_settings,
        approval_config=approval_config,
    )


async def _chain_level_gate(
    instance,
    invoice: Invoice,
    actor_id: uuid.UUID,
    approval_config: dict,
    *,
    org_settings: dict | None,
) -> int:
    """Route ``instance``'s chain against the invoice as it now stands and
    refuse an actor who is not the current level's named approver. Returns the
    current level's index.

    Initialises the chain on first approval — or, when the escalation sweep
    initialised it and nobody has approved yet, re-routes it against the
    CORRECTED invoice (``approval_chain.ensure_chain_routed``). No applicable
    level → single-level. Without the named-approver check, any actor holding
    the coarse RBAC permission could clear a level meant for a specific person
    (e.g. the named CFO), collapsing the SoD control the chain exists for.
    """
    from app.services.approval_chain import (
        check_level_approver,
        ensure_chain_routed,
        get_chain_progress,
    )

    ensure_chain_routed(instance, invoice, approval_config, org_settings=org_settings)
    chain_state = get_chain_progress(instance)
    levels = chain_state.get("levels", [])
    current = chain_state.get("current_level", 0)
    if current < len(levels):
        await check_level_approver(levels[current].get("approver_ids", []), actor_id)
    return current


async def review_refusals(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor,
    org_settings: dict | None,
) -> dict[str, dict | None]:
    """What ``POST /approve`` and ``POST /reject`` would answer ``actor`` for
    ``invoice`` right now: ``{"approve": refusal | None, "reject": refusal |
    None}``, each refusal the coded ``{code, message, params}`` detail the
    endpoint would refuse with.

    The invoice modal reads this to decide whether to offer — and, when not,
    why not — the two review decisions, instead of restating any of their rules
    (issue #575). It is computed by running the gates the endpoint runs, in the
    endpoint's order, on an approval with no corrections (the modal's own
    corrections are submitted with the approval and re-gated there):

    * the permission ``require_permission(invoice.approve)`` checks;
    * :func:`refuse_unless_in_review`;
    * :func:`_identity_gates`, :func:`_figure_gates`;
    * for a chain, :func:`_chain_level_gate` and
      ``approval_chain.advance_approval_chain`` (level reuse) on a SCRATCH copy
      of the workflow instance's state, so reading the verdict writes nothing.

    Deliberately NOT covered: the optimistic-concurrency check (it compares the
    version the page loaded, which this read IS), and a correction's own
    refusals (GL chart, applied-credit stranding), which depend on what the
    reviewer types.

    Reject has no segregation or threshold gate on the server — a reviewer may
    always send an invoice back — so its verdict is the permission and the
    review stage only.
    """
    import copy
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.deps import holds_any_permission
    from app.api.permissions import PERM_INVOICE_APPROVE
    from app.api.refusals import refusal_of, role_not_permitted
    from app.services.approval_chain import advance_approval_chain

    if not holds_any_permission(actor, {PERM_INVOICE_APPROVE}):
        return {"approve": role_not_permitted(), "reject": role_not_permitted()}
    try:
        refuse_unless_in_review(invoice)
    except HTTPException as exc:
        refused = refusal_of(exc, fallback_code=INVOICE_NOT_IN_REVIEW)
        return {"approve": refused, "reject": refused}

    instance = await get_workflow_instance(db, invoice.id)
    approval_config = await resolve_approval_config(db, invoice, instance)
    actor_roles = {r.name for r in actor.roles} if actor.roles else set()
    try:
        await _identity_gates(invoice, actor.id, approval_config)
        await _figure_gates(
            db,
            invoice,
            actor_id=actor.id,
            actor_roles=actor_roles,
            approval_config=approval_config,
            org_settings=org_settings,
            receipt_recorders=None,
        )
        if approval_config.get("approver_strategy") == "chain" and instance:
            scratch = SimpleNamespace(state_data=copy.deepcopy(instance.state_data or {}))
            await _chain_level_gate(
                scratch, invoice, actor.id, approval_config, org_settings=org_settings
            )
            advance_approval_chain(scratch, actor.id)
    except HTTPException as exc:
        return {"approve": refusal_of(exc, fallback_code="approval_refused"), "reject": None}
    return {"approve": None, "reject": None}


async def approve_invoice(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
    actor_name: str,
    actor_roles: set[str] | None = None,
    corrections: dict | None = None,
    org_settings: dict | None = None,
    receipt_recorders: Collection[str] | None = None,
) -> Invoice:
    """Approve ``invoice`` as ``actor_id`` — the one door every approval path
    (in-app single and bulk, email / Slack / Teams links, mobile, the exception
    agents) goes through, so every control below holds on all of them.

    ``receipt_recorders`` lets bulk approve hand in the batch's
    ``approval_chain.receipt_recorders_by_invoice`` resolved up front; ``None``
    looks it up here. Either way it is re-read after a correction, which can
    re-point the invoice at another PO.
    """
    from app.services.approval_chain import advance_approval_chain

    # Read the approval config: the invoice's frozen snapshot, falling back —
    # fail-CLOSED — to the org's active definition when it has none. See
    # `resolve_approval_config`. Resolved ONCE and threaded into
    # `_enforce_approval_thresholds` below, so the segregation / named-approver
    # gates and the money gates can never read different configs.
    instance = await get_workflow_instance(db, invoice.id)
    approval_config: dict = await resolve_approval_config(db, invoice, instance)

    # Segregation of duties + the single-level named-approver gate, before any
    # correction lands (`_identity_gates`, shared with `review_refusals`).
    await _identity_gates(invoice, actor_id, approval_config)

    # Apply any field corrections FIRST, capturing a per-field before/after diff
    # for the audit trail (SOX change-history requirement). Money fields
    # serialise as string-Decimal inside the diff (build_field_diff handles the
    # typing).
    #
    # Order matters: corrections (which can include `amount`) must be applied
    # BEFORE threshold enforcement so the max-amount cap and the CFO gate are
    # evaluated against the POST-correction amount. Enforcing first would let a
    # reviewer approve a $100 invoice with corrections={"amount": 5000} and slip
    # past a $1000 cap / $500 CFO gate — the gate would read the stale $100.
    field_diff: dict = {}
    if corrections:
        from app.services.audit_access import build_field_diff
        from app.services.gl_chart import refuse_gl_codes_outside_chart

        # A GL correction is a coding decision like any other write, so it must
        # resolve in the invoice's own chart (shared ∪ its entity) — never
        # another entity's, and an active account of it whenever it has any
        # (decisions §194/§199) — refused BEFORE any correction is applied. This
        # is also the door the GL-coding exception agent approves through; its
        # coordinator turns the 422 into an escalation with this text as the
        # rationale, so a vendor's dominant historical code whose account has
        # since been retired escalates instead of being approved in.
        new_gl = corrections.get("gl_account")
        if new_gl and new_gl != invoice.gl_account:
            await refuse_gl_codes_outside_chart(
                db,
                organization_id=invoice.organization_id,
                entity_id=invoice.entity_id,
                codes=[new_gl],
            )

        field_map = {"vendor": "vendor_name"}
        before: dict = {}
        after: dict = {}
        for field, value in corrections.items():
            if value is not None:
                attr = field_map.get(field, field)
                before[attr] = getattr(invoice, attr, None)
                setattr(invoice, attr, value)
                after[attr] = getattr(invoice, attr, None)
        diff_fields = list(after.keys())

        # Re-resolve the vendor LINK whenever the correction (re)saved the
        # vendor name and the link is stale or absent — the same rule
        # `PATCH /api/invoices/{id}` applies (`api/invoices.py`). `vendor_id`,
        # not `vendor_name`, is what the payment run reads the payee's bank
        # details from and what the credit-memo vendor guard compares against,
        # so approving a corrected vendor NAME without re-linking left
        # `Invoice.vendor_id` pointing at the PRE-correction vendor — a
        # display/payee mismatch of the same shape the PATCH-path fix already
        # closed, just missing on this one entry point into `approved`.
        # Blank name → no provable vendor → NULL (un-creditable / unpayable
        # until someone names the vendor again), mirroring the PATCH path.
        if "vendor_name" in after and (
            before.get("vendor_name") != invoice.vendor_name or invoice.vendor_id is None
        ):
            from app.services.vendor_matching import match_and_link_vendor

            before["vendor_id"] = invoice.vendor_id
            if (invoice.vendor_name or "").strip():
                await match_and_link_vendor(db, invoice, invoice.organization_id, source="manual")
            else:
                invoice.vendor_id = None
            after["vendor_id"] = invoice.vendor_id
            diff_fields.append("vendor_id")

        # The PATCH path's applied-credit guard, for the same reason: a
        # corrected vendor, currency or amount must not strand a credit memo
        # already applied to this invoice (decisions §214). Unfiltered by field:
        # `corrections` is a bare dict internal callers also pass, and the guard
        # is a no-op query for an invoice with no applied memo.
        if after:
            from app.services.applied_credit_integrity import (
                refuse_edit_stranding_applied_credits,
            )

            await refuse_edit_stranding_applied_credits(
                db, invoice, currency_edited="currency" in after
            )

        field_diff = build_field_diff(before, after, diff_fields)

        # Store vendor-consistent corrections in the correction cache so
        # future extractions from the same vendor pick up the right values.
        await record_corrections(db, invoice, corrections)

        # Recompute warnings + po_match against the CORRECTED fields. Without
        # this the approved invoice keeps the pre-correction artefacts: a
        # po_number correction leaves a stale `po_mismatch` warning and a failed
        # match against the old PO on the row, and a vendor_name correction skips
        # the duplicate check on the new value. The PATCH path already refreshes
        # after setattr; the approve path must too. Best-effort — a warnings
        # recompute must never break an otherwise-valid approval.
        from app.services.invoice_warnings import refresh_warnings

        try:
            await refresh_warnings(db, invoice, org_settings=org_settings)
        except Exception as exc:  # noqa: BLE001
            _log.warning("refresh_warnings after corrections failed for %s: %s", invoice.id, exc)

    # Receiving segregation + the money gates, against the now-corrected invoice
    # (`_figure_gates`, shared with `review_refusals`). A correction can re-point
    # the invoice at another PO, so a batch-resolved recorder set is re-read.
    await _figure_gates(
        db,
        invoice,
        actor_id=actor_id,
        actor_roles=actor_roles or set(),
        approval_config=approval_config,
        org_settings=org_settings,
        receipt_recorders=None if corrections else receipt_recorders,
    )

    # Upsert the RAG embedding using the invoice's NOW-correct fields.
    # Best-effort: failures (S3 unavailable, no text layer, embedding API
    # down) log and move on — the approval itself still commits.
    try:
        file_bytes = await _fetch_invoice_bytes(invoice)
        if file_bytes:
            await store_embedding(db, invoice, file_bytes=file_bytes)
    except Exception as exc:  # noqa: BLE001
        _log.warning("RAG embedding storage failed for %s: %s", invoice.id, exc)

    # Multi-level chain: check if this approval satisfies the current level
    # or if more levels remain.
    if approval_config.get("approver_strategy") == "chain" and instance:
        # Lock the workflow instance row to prevent concurrent approval races
        from app.models.workflow import WorkflowInstance

        locked_result = await db.execute(
            select(WorkflowInstance).where(WorkflowInstance.id == instance.id).with_for_update()
        )
        instance = locked_result.scalar_one()

        # Route the chain against the corrected invoice and refuse an actor who
        # is not this level's named approver (`_chain_level_gate`, shared with
        # `review_refusals`). The level index is read BEFORE advancing —
        # advance_approval_chain bumps current_level once the level is
        # satisfied — for the partial-approval audit row below.
        approved_level = await _chain_level_gate(
            instance, invoice, actor_id, approval_config, org_settings=org_settings
        )

        # Records the approval; refuses an approver who already signed another
        # level (`approval_level_reuse`).
        chain_complete = advance_approval_chain(instance, actor_id)
        if not chain_complete:
            # More levels needed — stay in ready_for_review, record partial.
            #
            # Each intermediate approver's decision must land in the immutable
            # audit trail, not only in the mutable state_data JSONB. Without
            # this, a 3-level chain produced exactly ONE audit row (the final
            # approval), so an auditor could never reconstruct who approved at
            # levels 1..N-1. Write an append-only `invoice.approval_step` row
            # capturing the actor + the level they approved at.
            from app.services.audit_dispatch import dispatch_audit

            await dispatch_audit(
                db,
                correlation_id=invoice.correlation_id,
                organization_id=invoice.organization_id,
                actor_id=actor_id,
                action="invoice.approval_step",
                entity_type="invoice",
                entity_id=invoice.id,
                details={
                    "decision": "approved",
                    "level": approved_level,
                    **({"changes": field_diff} if field_diff else {}),
                },
            )
            await db.flush()
            return invoice

    # All approvals satisfied (or single-level) — finalize
    from app.services.approval_chain import record_final_approval

    # By id, beside the display name: a receipt this approver hand-records
    # afterwards must not lift the invoice's payment hold (decisions §389).
    record_final_approval(invoice, approved_by=actor_name, actor_id=actor_id)

    # Digital signature on the approval (SOX non-repudiation): an HMAC-SHA256
    # "timestamp + user hash" over the canonical approval facts (invoice id +
    # exact Decimal amount + actor + decision + timestamp), stamped into THIS
    # approval audit row's immutable `details`. Re-verifiable later via
    # GET /api/audit/invoice/{id}/verify-signatures. No-op (None) when no signing
    # key is configured. The same `signed_at` timestamp feeds both the digest and
    # the stored block so a re-derivation reproduces identical canonical bytes.
    from datetime import UTC, datetime

    from app.services.approval_signature import build_signature_detail

    signed_at = datetime.now(UTC)
    audit_details: dict = {}
    if field_diff:
        audit_details["changes"] = field_diff
    signature = build_signature_detail(
        invoice_id=invoice.id,
        amount=Decimal(str(invoice.amount or 0)),
        actor_id=actor_id,
        decision="approved",
        timestamp=signed_at,
        signing_key=settings.approval_signing_key,
    )
    if signature:
        audit_details["signature"] = signature

    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.approved,
        actor_id=actor_id,
        action_name="invoice.approved",
        details=audit_details or None,
    )

    if instance:
        await advance_workflow(db, instance, "erp_push", action="approved")

    # Every control above has passed and the invoice is `approved`. When its
    # frozen workflow says "send to the ERP on approval", the push is queued
    # for AFTER the caller commits, in a transaction of its own, so a plan or
    # duplicate refusal there can never undo this approval (decisions §304).
    from app.services.erp_send import schedule_auto_send

    schedule_auto_send(db, invoice, instance, actor_id=actor_id)

    return invoice


async def reject_invoice(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
    actor_name: str,
    reason: str,
) -> Invoice:
    invoice.rejected_by = actor_name
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.rejected,
        actor_id=actor_id,
        action_name="invoice.rejected",
        details={"reason": reason},
    )

    # Create an exception record (shared chokepoint → emits `exception.raised`)
    from app.services.exception_service import create_exception

    await create_exception(
        db,
        exception_type="review_rejected",
        description=reason,
        status="open",
        organization_id=invoice.organization_id,
        invoice=invoice,  # exception follows its invoice (P2)
        # `actor_id` (the rejecter) IS in scope here and is deliberately not
        # stamped. This row notifies AP of a decision the reviewer already made
        # and already audited (`invoice.rejected`); it is not a second look at
        # their act, so there is nothing for a different person to check.
        # Recording them would make finishing your own rework queue item a
        # segregation breach.
        raised_by_user_id=None,
    )

    instance = await get_workflow_instance(db, invoice.id)
    if instance:
        await complete_current_step(db, instance, "rejected")
        # Track rejection count + reset any multi-level approval chain state.
        #
        # `approve_invoice` only initialises chain state when
        # `approval_levels` is absent. If a rejected-and-reworked invoice kept
        # its old chain state, the next approval would RESUME at whatever level
        # it was rejected at — silently skipping every already-satisfied level
        # (a manager→CFO chain rejected at L0 would then need only the CFO). A
        # reworked invoice must re-run the whole chain, so clear it here (the
        # single reject chokepoint) and let the next approval re-initialise.
        from app.services.approval_chain import clear_chain_state

        state_data = dict(instance.state_data or {})
        state_data["rejection_count"] = state_data.get("rejection_count", 0) + 1
        clear_chain_state(state_data)
        instance.state_data = state_data

    return invoice


def holds_recallable_approval(invoice: Invoice) -> bool:
    """Whether ``invoice``'s status holds an approval a recall can take back:
    `approved`, or `failed` once it was ever approved (an approved invoice's
    failed ERP push — `models/invoice_status.financially_locked`'s second case).
    A `failed` extraction was never approved; there is nothing to recall."""
    from app.models.invoice_status import was_ever_approved

    if invoice.status == InvoiceStatus.approved:
        return True
    return invoice.status == InvoiceStatus.failed and was_ever_approved(invoice)


async def recall_refusal(db: AsyncSession, invoice: Invoice, *, actor) -> dict | None:
    """What `POST /{id}/recall-approval` would answer ``actor`` for ``invoice``
    right now — the coded refusal, or None. The ONE rule: the door raises from
    it under the row lock, and ``GET /review-actions`` serves it as
    ``recall_approval``, so neither client restates it (decisions §374).

    In order:

    1. the permission the approve and reject doors require (`invoice.approve`)
       — taking an approval back is the approver's duty, not entry's;
    2. :func:`holds_recallable_approval` (``invoice_not_recallable``);
    3. the ERP may hold the bill (``invoice_recall_in_erp``): a confirmed push
       left ``erp_document_id``, or the push's own record says a bill may exist
       (``services/erp.possibly_posted`` — fail-closed: an attempt whose outcome
       is unknown counts). Named before a claim because nothing in FeohLedger
       clears it, so nobody cancels a run only to meet it next;
    4. a live payment or virtual card (``invoice_recall_claimed``), read by
       ``erp_export_gate.live_claims`` — the rails' own predicates, so "live"
       reads here as it does for the run builder, the card mint, the ERP push
       and closing. A payment run holds its invoices through exactly these
       payment rows, so this is also "the invoice is in a payment run".

    Call it on a row locked for update, as the door does: the run builder, the
    card mint and the ERP push each lock the invoice too, so no claim or push
    lands between this read and the transition.
    """
    from app.api.deps import holds_any_permission
    from app.api.permissions import PERM_INVOICE_APPROVE
    from app.api.refusals import role_not_permitted
    from app.services.erp import possibly_posted
    from app.services.erp_export_gate import live_claims

    if not holds_any_permission(actor, {PERM_INVOICE_APPROVE}):
        return role_not_permitted()
    if not holds_recallable_approval(invoice):
        return coded_refusal(
            INVOICE_NOT_RECALLABLE,
            "Only an approved invoice, or one whose ERP push failed after approval, "
            "can have its approval recalled.",
            status=invoice.status.value,
        )
    instance = await get_workflow_instance(db, invoice.id)
    state = getattr(instance, "state_data", None) if instance is not None else None
    if invoice.erp_document_id or possibly_posted(state):
        return coded_refusal(
            INVOICE_RECALL_IN_ERP,
            "The ERP may already hold a bill for this invoice, and a bill there can be "
            "paid there. Correct or void it in the ERP; its approval can't be recalled "
            "from FeohLedger.",
        )
    claim = (await live_claims(db, [invoice.id])).get(invoice.id)
    if claim is not None:
        return coded_refusal(INVOICE_RECALL_CLAIMED, _RECALL_CLAIM_MESSAGES[claim], claim=claim)
    return None


async def recall_approval(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
    reason: str,
) -> Invoice:
    """Take back ``invoice``'s approval and return it to `ready_for_review`
    (decisions §374). The caller has locked the row and asked
    :func:`recall_refusal`.

    * **Nothing is erased.** The `invoice.approved` audit row and its signature
      stay where they are; this writes its own append-only
      `invoice.approval_recalled` row naming the approval it recalls
      (``approved_by`` / ``approval_date``) and the reason. The row's
      ``approved_by`` / ``approval_date`` keep naming that approval until a new
      one overwrites them, so ``was_ever_approved`` stays true: an AP clerk's
      entry window stays shut and the document is not re-read.
    * **The recaller joins the segregation set.** Whoever reopens signed
      figures disputes them, and is usually the one who corrects them, so a
      second person signs the corrected version
      (``approval_chain.add_implicated_actor``).
    * **The chain re-runs from level 0.** The chain state is cleared, as a
      rejection clears it, so the next approval re-routes the chain against the
      invoice as it then stands and the max-amount and CFO gates are read
      again — none of the recalled approval's levels carry over.
    """
    from app.services.approval_chain import add_implicated_actor, clear_chain_state
    from app.services.workflow_engine import RECALL_ACTION, advance_workflow

    from_status = invoice.status.value
    recalled = {
        "approved_by": invoice.approved_by,
        "approval_date": invoice.approval_date.isoformat() if invoice.approval_date else None,
    }
    add_implicated_actor(invoice, actor_id)
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.ready_for_review,
        actor_id=actor_id,
        action_name=RECALL_ACTION,
        details={"reason": reason, "recalled_from": from_status, "recalled_approval": recalled},
    )

    instance = await get_workflow_instance(db, invoice.id)
    if instance is not None:
        state_data = dict(instance.state_data or {})
        state_data["recall_count"] = state_data.get("recall_count", 0) + 1
        clear_chain_state(state_data)
        # A recall from a failed push starts the next push with a full budget,
        # as `/retry-erp` does: an exhausted counter would leave the
        # re-approved invoice at `sending_to_erp` with no attempt made. The
        # push's other keys (a queued job, an orphan document, attempt dates)
        # are kept — the recall gate has already refused one that names a bill.
        if "erp_retries" in state_data:
            state_data["erp_retries"] = 0
        instance.state_data = state_data
        # Back in review: an instance a failed push marked `failed` is active
        # again (the escalation sweep reads only active ones), the step the
        # approval opened (the ERP push) is closed, and a fresh approval step is
        # opened for the queue.
        instance.state = "active"
        await advance_workflow(db, instance, "review", action="approval_recalled")
    return invoice


async def resubmit_invoice(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
) -> Invoice:
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.ready_for_review,
        actor_id=actor_id,
        action_name="invoice.resubmitted",
    )

    instance = await get_workflow_instance(db, invoice.id)
    if instance:
        # Create a new review step
        await create_workflow_step(db, instance, "review")
        instance.current_step = 1  # review step index

    return invoice


async def assign_reviewer(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    reviewer_name: str,
    control_db: AsyncSession | None = None,
) -> None:
    from app.services.audit_dispatch import dispatch_audit

    # Check delegation — if reviewer is OOO, reassign to their delegate
    original_id = None
    if control_db:
        from app.services.approval_chain import resolve_assignee

        effective_id, original_id = await resolve_assignee(reviewer_id, control_db)
        if original_id:
            # Delegation active — look up delegate's name
            from app.models.user import User as UserModel

            delegate_result = await control_db.execute(
                select(UserModel).where(UserModel.id == effective_id)
            )
            delegate = delegate_result.scalar_one_or_none()
            if delegate:
                reviewer_id = effective_id
                reviewer_name = delegate.full_name

    invoice.assigned_to_id = reviewer_id
    invoice.assigned_to = reviewer_name

    # Mirror the assignment onto the open approval step, when there is one.
    #
    # A missing WorkflowInstance must NOT short-circuit the rest of this
    # function. It used to `return` here, so an invoice with no instance — the
    # email-intake and PEPPOL-inbound ingest paths create one without, and so
    # does any legacy row — had its assignee written with **no
    # `invoice.assigned_for_review` audit row and no notification**. The
    # reviewer was never told, and no email / Slack / Teams approval token was
    # ever minted (that happens inside `notify_event`), so the invoice sat
    # assigned-but-silent. The step row is a nice-to-have; the audit trail and
    # telling the human are not.
    instance = await get_workflow_instance(db, invoice.id)
    if instance is not None:
        from app.models.workflow import WorkflowStep

        # Steps are now persisted under the canonical "approval" type, but rows
        # written before that normalisation may still carry the legacy "review"
        # alias — match both so reassignment finds either.
        result = await db.execute(
            select(WorkflowStep)
            .where(
                WorkflowStep.instance_id == instance.id,
                WorkflowStep.step_type.in_(("approval", "review")),
                WorkflowStep.completed_at.is_(None),
            )
            .order_by(WorkflowStep.created_at.desc())
            .limit(1)
        )
        step = result.scalar_one_or_none()
        if step:
            step.assigned_to = reviewer_id
            if original_id:
                step.original_assigned_to = original_id

    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=actor_id,
        action="invoice.assigned_for_review",
        entity_type="invoice",
        entity_id=invoice.id,
        details={
            "reviewer_id": str(reviewer_id),
            **({"delegated_from": str(original_id)} if original_id else {}),
        },
    )

    # Best-effort notification to the (possibly delegated) reviewer. Assignment
    # is the one notifiable event that does not flow through transition_invoice,
    # so it's dispatched explicitly here. Never breaks the assignment.
    from app.models.notification import EVENT_INVOICE_ASSIGNED
    from app.services.notification_dispatch import notify_event
    from app.services.notification_templates import InvoiceContext

    # `notify_event` swallows its own template/recipient/email failures, but its
    # per-recipient `db.add(...)` is unguarded; this outer guard is the final
    # backstop so a session error there can't abort an otherwise-valid
    # assignment — mirrors the guard in workflow_engine.transition_invoice.
    try:
        await notify_event(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=invoice.organization_id,
            event_type=EVENT_INVOICE_ASSIGNED,
            entity_id=invoice.id,
            recipient_user_ids=[reviewer_id],
            invoice_ctx=InvoiceContext(
                invoice_number=invoice.invoice_number,
                vendor_name=invoice.vendor_name,
                amount=invoice.amount,
                currency=invoice.currency or "USD",
            ),
            # The round too (decisions §379): a recall or an in-app reject
            # later retires this message's Approve link.
            action_facts=await approval_message_facts(db, invoice),
            actor_id=actor_id,
        )
    except Exception:  # noqa: BLE001 — never let a notification bug break assignment
        _log.exception("assign_reviewer: notification dispatch failed for invoice=%s", invoice.id)
