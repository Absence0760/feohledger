"""Recurring / subscription invoice generation — scheduler + sweep + variance.

Predictable, fixed-cadence spend (rent, SaaS seats, utilities, insurance)
shouldn't need a fresh upload + extraction every period. A
:class:`~app.models.recurring_invoice.RecurringInvoiceTemplate` captures the
vendor, amount, GL coding, entity and a simple cadence (monthly / quarterly /
annual + day-of-period); this module turns that into a calendar and generates
the next ``Invoice`` on schedule — pre-coded from the template and dropped
straight into the approval queue (``ready_for_review``), bypassing extraction.

Two surfaces share these helpers:

  * the ``/api/recurring`` router (manual ``generate-now``, the projected
    upcoming schedule, ``next_run_on`` recompute on create/edit/resume), and
  * the :func:`run_recurring_invoices_loop` background sweep, which mirrors
    ``discount_auto_trigger`` exactly — control-plane enumerates orgs, a fresh
    per-tenant engine is disposed in ``finally``, and one tenant's failure is
    logged but never halts the sweep. Within a tenant it mirrors
    ``vendor_rescreen``: each template is re-read by id, guarded, and committed
    **on its own**, so one template that raises can't abort the tick and
    discard the invoices already generated on it.

A skipped period is never silent
--------------------------------
A template missing ``amount`` or ``vendor_name`` can't generate, and the
sweep's defensive cursor advance rolls it past the period anyway so a stuck
cursor can't spin forever. That half was always right; the missing half is
that nothing surfaced the miss — the template stayed ``active``,
``generated_count`` never moved, and the only trace was a log line, so a
subscription invoice a tenant believed was being raised every month simply
wasn't. Now every skip stamps a PII-free marker on ``template.meta``
(:func:`record_generation_skip` — reason code, period, consecutive count,
timestamp; no migration), writes a ``recurring_template.generation_skipped``
audit row, and rides the ``last_skip`` field on the API response. Past
:data:`MAX_CONSECUTIVE_SKIPS` consecutive misses the sweep pauses the template
and audits that too, so an unfixable schedule stops claiming to be live.
:func:`clear_generation_skip` resets the count the moment it generates again.

Money-path boundary
-------------------
This module ONLY creates an invoice in the queue. It never schedules a payment
or moves money — a human still approves + funds each generated invoice through
the normal CFO-gated path. The worst a bug here can do is drop an extra draft
invoice into the review queue.

Idempotency
-----------
Lives at the DB layer, not here: every generated invoice carries
``Invoice.recurring_template_id`` + ``Invoice.recurring_period_key`` and a
partial unique index ``uq_invoice_recurring_period`` on that pair. A
concurrent / retried double-fire of the same period raises ``IntegrityError``
on the second INSERT, which :func:`generate_one` catches (inside a savepoint so
the surrounding sweep keeps its other work) and turns into a no-op that returns
the already-existing invoice.

All money is ``Decimal`` (never float). Only stdlib date math — no
``python-dateutil`` dependency. See ``backend/docs/recurring-invoices.md``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.recurring_invoice import (
    CADENCE_ANNUAL,
    CADENCE_QUARTERLY,
    MATERIAL_EDIT_FIELDS,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    RecurringInvoiceTemplate,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.invoice_warning_catalog import warning
from app.services.sweep_health import SWEEP_RECURRING_INVOICES, run_sweep_loop
from app.services.workflow_engine import create_workflow_instance
from app.utils.dates import utc_today

logger = logging.getLogger(__name__)

# Platform-default variance tolerance (percent) for flagging an arrived invoice
# whose amount drifts from its recurring template. Mirrors the price-variance
# tolerance shape in vendor_enrichment (PRICE_TOLERANCE_PCT). Per-template
# override on `RecurringInvoiceTemplate.variance_tolerance_pct`.
DEFAULT_VARIANCE_TOLERANCE_PCT = Decimal("10.0")

_HUNDRED = Decimal("100")

# --------------------------------------------------------------------------- #
# Non-generatable templates — the skip is persisted, counted and bounded
# --------------------------------------------------------------------------- #

#: PII-free reason codes for a template that cannot produce an invoice. Stable
#: strings: they ride the audit trail and the API response, so they are read by
#: humans and by the frontend, never re-worded per call site.
SKIP_MISSING_AMOUNT = "missing_amount"
SKIP_MISSING_VENDOR = "missing_vendor"
SKIP_MISSING_AMOUNT_AND_VENDOR = "missing_amount_and_vendor"

#: Key under which the skip marker lives on ``RecurringInvoiceTemplate.meta``.
#: Settings-JSON, so recording a skip needs no migration.
SKIP_META_KEY = "generation_skip"

#: Consecutive due periods a template may fail to generate before the sweep
#: pauses it. Mirrors ``services/scheduled_reports``' auto-disable-after-N
#: shape: an unfixable schedule shouldn't keep claiming to be live. Small
#: because each miss is a whole billing period, not a tick.
MAX_CONSECUTIVE_SKIPS = 3


def not_generatable_reason(template: RecurringInvoiceTemplate) -> str | None:
    """Why ``template`` cannot generate an invoice, or ``None`` if it can.

    Pure. The single condition :func:`generate_one`, the sweep, and the
    router's ``generate-now`` 422 all read, so the three can't drift into
    disagreeing about what "generatable" means.
    """
    missing_amount = template.amount is None
    missing_vendor = not template.vendor_name
    if missing_amount and missing_vendor:
        return SKIP_MISSING_AMOUNT_AND_VENDOR
    if missing_amount:
        return SKIP_MISSING_AMOUNT
    if missing_vendor:
        return SKIP_MISSING_VENDOR
    return None


def _read_skip_marker(template: RecurringInvoiceTemplate) -> dict:
    """The persisted skip marker, or ``{}``. Tolerates hand-edited JSON."""
    marker = (template.meta or {}).get(SKIP_META_KEY)
    return marker if isinstance(marker, dict) else {}


def record_generation_skip(
    template: RecurringInvoiceTemplate, *, period_key: str, reason: str, at: datetime
) -> dict:
    """Stamp a non-generatable period onto the template and return the marker.

    Persisting is the point. ``generate_one`` returning ``None`` used to log a
    WARNING and nothing else, while the sweep's (correct) defensive cursor
    advance rolled ``next_run_on`` forward anyway — so the template stayed
    ``active``, ``generated_count`` never moved, and a subscription invoice a
    tenant believed was being raised every month simply wasn't. The only trace
    was a log line, and ``GET /api/recurring/{id}/history`` showed an empty run
    history indistinguishable from "nothing due yet".

    ``consecutive`` counts CONSECUTIVE misses — :func:`clear_generation_skip`
    resets it the moment the template generates again — and is what bounds the
    silence via :data:`MAX_CONSECUTIVE_SKIPS`. The marker is PII-free: a reason
    code, the period, a count and a timestamp; never the vendor or the amount.
    """
    prior = _read_skip_marker(template)
    try:
        consecutive = int(prior.get("consecutive") or 0) + 1
    except (TypeError, ValueError):  # pragma: no cover — hand-edited JSON
        consecutive = 1
    marker = {
        "reason": reason,
        "period_key": period_key,
        "consecutive": consecutive,
        "last_skipped_at": at.isoformat(),
    }
    # Reassign rather than mutate: SQLAlchemy doesn't track in-place changes to
    # a JSONB dict, so an in-place write would never reach the UPDATE.
    template.meta = {**(template.meta or {}), SKIP_META_KEY: marker}
    return marker


def clear_generation_skip(template: RecurringInvoiceTemplate) -> None:
    """Drop the skip marker once the template can generate again. No-op if absent.

    Call this from **every** path that has just established the reason no
    longer holds, not only the background sweep. The marker isn't cosmetic:
    ``record_generation_skip`` counts up from whatever it finds, so a stale
    count left behind after a manual fix makes the NEXT single miss trip the
    :data:`MAX_CONSECUTIVE_SKIPS` auto-pause — and a healthy template goes on
    reporting "Not generating" to the operator who just fixed it, which is the
    original bug inverted.
    """
    meta = template.meta or {}
    if SKIP_META_KEY not in meta:
        return
    remaining = {k: v for k, v in meta.items() if k != SKIP_META_KEY}
    template.meta = remaining or None


def clear_generation_skip_if_resolved(template: RecurringInvoiceTemplate) -> None:
    """Clear the skip marker iff the template is generatable again.

    For the callers that change a template *without* generating from it — the
    ``PATCH`` that fills in the missing amount, the ``resume`` that puts it back
    in service. A template still missing its vendor keeps the marker, because
    the reason it names is still true.
    """
    if not_generatable_reason(template) is None:
        clear_generation_skip(template)


# --------------------------------------------------------------------------- #
# Who is implicated in a payable this template raises
# --------------------------------------------------------------------------- #
#
# Segregation of duties keys on a SET of actors, not one column
# (``approval_chain.violates_segregation``). For a recurring payable that set is
# the template's author plus everyone who later changed a term of it: an
# ap_manager who repoints someone else's template at a different vendor and
# amount has shaped the payable as completely as whoever wrote it, and before
# ``material_editor_ids`` existed they were recorded nowhere on it and could
# approve what it generated. Recording the editor in ``created_by_user_id``
# instead would merely have moved the exemption to the author.


def material_edit_fields(changed: list[str] | set[str]) -> set[str]:
    """The subset of ``changed`` that alters a term of the generated payable.

    One definition, in ``models/recurring_invoice.MATERIAL_EDIT_FIELDS``, so the
    router cannot drift from the model's documented classification.
    """
    return MATERIAL_EDIT_FIELDS & set(changed)


def record_material_editor(template: RecurringInvoiceTemplate, user_id: uuid.UUID) -> None:
    """Add ``user_id`` to the template's material-editor set. Idempotent.

    A set, not an append-only log: "who is implicated" is a membership question,
    and a list that grew a duplicate on every edit would make the column's size
    a function of how often one person edited. Sorted so the stored JSON is
    stable and diffable.

    Reassigns a **new list** rather than mutating in place. The column is plain
    ``JSONB`` (no ``MutableList``), so SQLAlchemy's dirty check compares object
    identity against the load-time baseline — an ``append`` on the existing list
    is invisible to the flush and the edit would silently not persist. Same trap
    as the ``copy.deepcopy`` in ``approval_chain.advance_approval_chain``.
    """
    existing = {str(x) for x in (template.material_editor_ids or [])}
    if str(user_id) in existing:
        return
    template.material_editor_ids = sorted(existing | {str(user_id)})


def implicated_actor_ids(
    template: RecurringInvoiceTemplate,
    *,
    uploader_id: uuid.UUID | None,
) -> list[str] | None:
    """The actors to stamp on ``Invoice.segregation_actor_ids`` — author ∪ editors.

    ``uploader_id`` is whoever is about to land in ``Invoice.uploaded_by_id``, and
    is removed: the predicate already refuses them via that column, and naming a
    person twice tells a later reader the two columns disagree about something.

    Returns ``None`` rather than ``[]`` when nobody is left, so the stored shape
    for "no additional implicated actor" is the same NULL every other creation
    path writes, and "empty set" never has to be distinguished from "no set".
    """
    ids = {str(x) for x in (template.material_editor_ids or [])}
    if template.created_by_user_id is not None:
        ids.add(str(template.created_by_user_id))
    if uploader_id is not None:
        ids.discard(str(uploader_id))
    return sorted(ids) or None


# --------------------------------------------------------------------------- #
# Pure date math — no dateutil
# --------------------------------------------------------------------------- #


def _add_months(d: date, months: int) -> date:
    """Return ``d`` advanced by ``months`` calendar months.

    Pure stdlib. The day is clamped to the last valid day of the target month
    so e.g. Jan-31 + 1 month → Feb-28/29. Callers here only ever pass
    ``day_of_period`` clamped to 1..28 by the schema, so the clamp is belt-and-
    suspenders, but it keeps the helper correct for any input.
    """
    total = (d.year * 12 + (d.month - 1)) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    # Last day of the target month: day-0 of the following month.
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - date(year, month, 1)).days
    return date(year, month, min(d.day, last_day))


def _months_per_period(cadence: str) -> int:
    if cadence == CADENCE_QUARTERLY:
        return 3
    if cadence == CADENCE_ANNUAL:
        return 12
    return 1  # monthly (default)


def period_key_for(cadence: str, run_on: date) -> str:
    """Canonical period key for a generation date.

    - monthly   → ``"YYYY-MM"``
    - quarterly → ``"YYYY-Qn"`` (n = 1..4)
    - annual    → ``"YYYY"``
    """
    if cadence == CADENCE_ANNUAL:
        return f"{run_on.year:04d}"
    if cadence == CADENCE_QUARTERLY:
        quarter = (run_on.month - 1) // 3 + 1
        return f"{run_on.year:04d}-Q{quarter}"
    return f"{run_on.year:04d}-{run_on.month:02d}"


def compute_next_run_on(
    cadence: str,
    day_of_period: int,
    *,
    after: date,
    start_date: date,
    end_date: date | None = None,
) -> date | None:
    """First occurrence on/after ``max(after, start_date)`` for the cadence.

    The schedule is anchored at ``start_date``'s month/quarter/year, landing on
    ``day_of_period`` of each period (already clamped 1..28 by the schema).
    Returns the first such date that is ``>= max(after, start_date)``, or
    ``None`` if that date would fall past ``end_date``.

    Used both to seed ``next_run_on`` at create time (``after == start_date``)
    and to re-anchor it on resume so a paused template never back-fires every
    historic period it slept through (``after == today``).
    """
    floor = max(after, start_date)
    months = _months_per_period(cadence)

    # Anchor on the start period's day_of_period, then walk forward in whole
    # periods until we land on/after the floor.
    candidate = start_date.replace(day=1)
    candidate = _add_months(candidate, 0)
    candidate = date(candidate.year, candidate.month, day_of_period)
    # Guard the loop: bounded by the gap between floor and start in periods.
    while candidate < floor:
        candidate = _add_months(candidate, months)
        candidate = date(candidate.year, candidate.month, day_of_period)

    if end_date is not None and candidate > end_date:
        return None
    return candidate


def current_due_run_on(
    cadence: str,
    day_of_period: int,
    *,
    today: date,
    start_date: date,
) -> date:
    """The latest scheduled occurrence on/before ``max(today, start_date)``.

    This is the period a manual ``generate-now`` targets — the "current due"
    period. It is a pure function of (schedule, today), NOT of the mutable
    ``next_run_on`` cursor, so re-calling on the same day always resolves to the
    SAME period — which is what makes ``generate-now`` idempotent regardless of
    how far the cursor has since advanced.

    Before ``start_date`` the schedule hasn't begun, so the first occurrence
    (``start_date``'s ``day_of_period``, or the FOLLOWING period's if
    ``day_of_period`` falls earlier in the month than ``start_date`` itself)
    is returned as the floor.
    """
    months = _months_per_period(cadence)
    first = date(start_date.year, start_date.month, day_of_period)
    if first < start_date:
        # day_of_period lands earlier in the month than start_date, so this
        # period's occurrence is actually BEFORE the schedule begins — advance
        # to the next period, matching compute_next_run_on's walk-forward
        # anchor (issue #179). Without this, generate-now could target a
        # pre-start-dated period the background sweep would never reach.
        first = _add_months(first, months)
        first = date(first.year, first.month, day_of_period)
    ceiling = max(today, start_date)
    candidate = first
    if candidate > ceiling:
        return first
    nxt = candidate
    while True:
        following = _add_months(nxt, months)
        following = date(following.year, following.month, day_of_period)
        if following > ceiling:
            break
        nxt = following
    return nxt


def project_schedule(
    template: RecurringInvoiceTemplate, *, count: int, from_date: date | None = None
) -> list[tuple[str, date]]:
    """Project the next ``count`` (period_key, run_on) occurrences forward.

    Read-only — creates nothing. Starts at ``template.next_run_on`` (or
    ``from_date`` when given) and walks the cadence, stopping early at
    ``end_date``.
    """
    if count <= 0:
        return []
    start = from_date or template.next_run_on
    if start is None:
        return []
    months = _months_per_period(template.cadence)
    out: list[tuple[str, date]] = []
    run_on = date(start.year, start.month, template.day_of_period)
    if run_on < start:
        run_on = _add_months(run_on, months)
        run_on = date(run_on.year, run_on.month, template.day_of_period)
    for _ in range(count):
        if template.end_date is not None and run_on > template.end_date:
            break
        out.append((period_key_for(template.cadence, run_on), run_on))
        run_on = _add_months(run_on, months)
        run_on = date(run_on.year, run_on.month, template.day_of_period)
    return out


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


def _generated_invoice_number(period_key: str) -> str:
    """Deterministic-prefix, collision-resistant generated invoice number.

    ``REC-<period_key>-<short>`` where ``<short>`` is a random hex tail. The
    DB idempotency key is (template_id, period_key) — NOT this number — so a
    short random tail is fine and keeps the human-facing number unique even if
    a template is reactivated for a re-numbered run.
    """
    return f"REC-{period_key}-{uuid.uuid4().hex[:6]}"


async def generate_one(
    db: AsyncSession,
    template: RecurringInvoiceTemplate,
    *,
    run_on: date,
    actor_id: uuid.UUID | None = None,
) -> Invoice | None:
    """Generate one pre-coded invoice for ``template`` for the ``run_on`` period.

    Builds the Invoice from the template's denormalised coding, stamps the
    ``(recurring_template_id, recurring_period_key)`` idempotency pair, creates
    the workflow instance, and drops the invoice into the approval queue at
    ``ready_for_review`` (NOT ``pending`` — that is the extraction state the
    reaper sweeps; a recurring invoice is already coded and needs only
    approval). Advances the template's scheduling cursor.

    ``actor_id`` is the person who caused this generation — ``user.id`` from the
    manual ``POST /{id}/generate-now``, ``None`` from the background sweep. It
    lands on the audit row AND on ``Invoice.uploaded_by_id``, which is what
    ``approval_chain.violates_segregation`` keys on, so the AP manager who
    triggered a generation cannot also approve what it produced.

    When the sweep runs it there is no live actor, so ``uploaded_by_id`` falls
    back to ``template.created_by_user_id`` — the employee who authored the
    standing instruction. The audit row keeps the honest ``actor_id`` (``None``
    for the sweep: nobody ran it), because the two answer different questions.
    *Who performed this action* is nobody; *whose instruction created this
    payable* is the template's author, and it is the second that segregation of
    duties must key on.

    One column names one person, and a template can be shaped by several, so
    ``Invoice.segregation_actor_ids`` carries the rest — author ∪ material
    editors, minus whoever already landed in ``uploaded_by_id``
    (:func:`implicated_actor_ids`). ``violates_segregation`` refuses an approval
    by anyone in that set, which is what stops an ap_manager who repointed
    someone else's template at a different vendor and amount from approving the
    invoice it then generated.

    Idempotent: a concurrent / retried call for an already-generated period
    hits the partial unique index, the INSERT raises ``IntegrityError`` inside
    a savepoint, and we return the already-existing invoice (no duplicate, no
    audit, no cursor advance).

    Returns the created (or pre-existing) :class:`Invoice`. ``None`` only when
    the template lacks the minimum data to generate (no vendor / no amount) —
    the condition is :func:`not_generatable_reason`, shared with the sweep and
    the router so a "can't generate" verdict is decided in one place. A caller
    that receives ``None`` owes the user a persisted trace; the sweep records
    one via :func:`record_generation_skip`.
    """
    reason = not_generatable_reason(template)
    if reason is not None:
        logger.warning("[recurring] template %s not generatable (%s)", template.id, reason)
        return None

    # The guard above just established the template CAN generate, so any skip
    # marker is stale. Clearing here — in the shared primitive — is what makes
    # `POST /{id}/generate-now` reset the count too; leaving it to the sweep
    # alone meant a manually-fixed template kept a stale `consecutive`, so its
    # next single miss tripped the auto-pause meant for three.
    clear_generation_skip(template)

    period_key = period_key_for(template.cadence, run_on)
    correlation_id = uuid.uuid4()

    # Resolved once, so the two segregation columns below cannot disagree about
    # who the uploader is — `implicated_actor_ids` subtracts exactly the id that
    # lands in `uploaded_by_id`.
    uploader_id = actor_id or template.created_by_user_id

    invoice = Invoice(
        organization_id=template.organization_id,
        entity_id=template.entity_id,
        correlation_id=correlation_id,
        invoice_number=_generated_invoice_number(period_key),
        vendor_name=template.vendor_name,
        vendor_id=template.vendor_id,
        description=template.description,
        amount=template.amount,
        currency=template.currency,
        invoice_date=run_on,
        payment_terms=template.payment_terms,
        po_number=template.po_number,
        gl_account=template.gl_account,
        cost_center=template.cost_center,
        department=template.department,
        project=template.project,
        # Already coded — straight into the approval queue, never extraction.
        status=InvoiceStatus.ready_for_review,
        recurring_template_id=template.id,
        recurring_period_key=period_key,
        # Whoever caused this payable to exist. Two answers, in order: the
        # person who clicked `POST /{id}/generate-now`, and — when the
        # background sweep raised it, so there is no live actor — the employee
        # who authored the template, whose standing instruction this invoice
        # is. Segregation of duties keys on this column, so without either the
        # AP manager who clicked generate-now, or the one whose template fires
        # every month, could also approve what it produced.
        #
        # `template.created_by_user_id` is NULL only for a template that
        # predates the column (migration 0096); it is deliberately not
        # backfilled, since there is no honest author to recover and guessing
        # one manufactures either a refusal or an absolution. Such an invoice
        # keeps the legacy exemption, exactly as a pre-`uploaded_by_id` row
        # does — a shrinking set, not a standing hole.
        uploaded_by_id=uploader_id,
        # …and everyone ELSE the payable's terms are attributable to: the author
        # when a live actor displaced them above, plus every material editor of
        # the template (`material_editor_ids`). One column can only name one
        # person; `violates_segregation` refuses anyone in this set exactly as it
        # refuses the uploader, so repointing someone else's template at a new
        # vendor and amount no longer leaves the editor free to approve the
        # result. `None` when the set is empty — the same NULL every other
        # creation path writes.
        segregation_actor_ids=implicated_actor_ids(template, uploader_id=uploader_id),
    )
    try:
        # Savepoint so a unique-violation rolls back ONLY this generation — the
        # surrounding sweep keeps every sibling template it already generated.
        # `db.add` MUST be INSIDE the block: `SessionTransaction._take_snapshot`
        # flushes the session when a `begin_nested()` boundary opens, so a row
        # added first is INSERTed before the SAVEPOINT exists — the
        # IntegrityError then escapes the block and leaves the transaction
        # needing a rollback, so the recovery SELECT below raises
        # PendingRollbackError and takes the whole sweep tick down with it.
        # Same trap as `card_issuance.persist_card`; see that docstring.
        async with db.begin_nested():
            db.add(invoice)
            await db.flush()
    except IntegrityError:
        # The (template, period) slot was already claimed (concurrent sweep or a
        # manual generate-now). Return the existing invoice — no duplicate.
        existing = (
            await db.execute(
                select(Invoice).where(
                    Invoice.recurring_template_id == template.id,
                    Invoice.recurring_period_key == period_key,
                )
            )
        ).scalar_one_or_none()
        return existing

    # Snapshot the active workflow definition onto the invoice (frozen routing).
    await create_workflow_instance(db, invoice)

    await dispatch_audit(
        db,
        correlation_id=correlation_id,
        organization_id=template.organization_id,
        actor_id=actor_id,
        action="invoice.created",
        entity_type="invoice",
        entity_id=invoice.id,
        details={
            "source": "recurring_template",
            "recurring_template_id": str(template.id),
            "period_key": period_key,
            "status": InvoiceStatus.ready_for_review.value,
        },
    )
    await dispatch_audit(
        db,
        correlation_id=correlation_id,
        organization_id=template.organization_id,
        actor_id=actor_id,
        action="recurring_template.generated",
        entity_type="recurring_invoice_template",
        entity_id=template.id,
        details={"invoice_id": str(invoice.id), "period_key": period_key},
    )

    # Advance the template's scheduling cursor to the NEXT period — strictly
    # one whole period after this run_on, so the next tick doesn't re-pick it.
    template.generated_count = (template.generated_count or 0) + 1
    template.last_period_key = period_key
    template.last_generated_at = datetime.now(UTC)
    next_anchor = _add_months(run_on, _months_per_period(template.cadence))
    template.next_run_on = compute_next_run_on(
        template.cadence,
        template.day_of_period,
        after=next_anchor,
        start_date=template.start_date,
        end_date=template.end_date,
    )

    return invoice


# --------------------------------------------------------------------------- #
# Variance — flag an ARRIVED invoice that drifts from its template
# --------------------------------------------------------------------------- #


def flag_template_variance(
    invoice: Invoice,
    template: RecurringInvoiceTemplate,
    *,
    default_tolerance_pct: Decimal = DEFAULT_VARIANCE_TOLERANCE_PCT,
) -> dict | None:
    """Compare an arrived invoice's amount to its template's expected amount.

    Returns a warning dict when ``|delta%| >= tolerance`` (the template's
    ``variance_tolerance_pct`` override, else ``default_tolerance_pct``), else
    ``None``. Pure + Decimal — mirrors ``vendor_enrichment.detect_price_variance``
    tolerance approach. No PII in the message (amounts only).
    """
    if invoice.amount is None or template.amount is None or template.amount == 0:
        return None
    tolerance = (
        template.variance_tolerance_pct
        if template.variance_tolerance_pct is not None
        else default_tolerance_pct
    )
    expected = Decimal(str(template.amount))
    actual = Decimal(str(invoice.amount))
    delta = actual - expected
    delta_pct = (delta / expected * _HUNDRED).quantize(Decimal("0.1"))
    if abs(delta_pct) < tolerance:
        return None
    return warning(
        # The direction is a word in the sentence, so it selects the code
        # rather than riding as a param — see `invoice_warning_catalog`.
        "recurring_variance_over" if delta > 0 else "recurring_variance_under",
        "warning",
        amount=actual,
        deltaPct=f"{delta_pct:+}",
        templateName=template.name,
        expectedAmount=expected,
        currency=invoice.currency,
    )


# --------------------------------------------------------------------------- #
# Background sweep — mirrors discount_auto_trigger exactly
# --------------------------------------------------------------------------- #


@dataclass
class SweepResult:
    """Per-sweep outcome for logging + tests.

    ``sweep_health.failure_count`` sums every field named ``failures`` or
    ending in ``_failures``, so ``template_failures`` deliberately joins the
    health signal (a template whose generation *raised* is a real failure) while
    ``templates_skipped`` deliberately does not: a template missing a vendor or
    an amount is a tenant configuration problem, not a broken sweep, and
    counting it would leave the sweep permanently `degraded` for something no
    operator of this platform can fix. That skip is surfaced per-template
    instead — persisted marker, audit row, and an auto-pause that bounds it.
    """

    tenants_scanned: int = 0
    invoices_generated: int = 0
    #: Due periods a template couldn't generate (missing amount / vendor).
    templates_skipped: int = 0
    #: Templates the sweep paused after `MAX_CONSECUTIVE_SKIPS` misses.
    templates_paused: int = 0
    #: Tenants whose sweep aborted outright (engine / connect / query failure).
    failures: int = 0
    #: Individual templates whose generation raised. Counted apart from
    #: ``failures`` because one template no longer takes its tenant down.
    template_failures: int = 0


@dataclass
class TenantSweepOutcome:
    """One tenant's slice of a sweep. Mirrors the fields on :class:`SweepResult`."""

    generated: int = 0
    templates_skipped: int = 0
    templates_paused: int = 0
    template_failures: int = 0


async def generate_recurring_invoices_once(*, today: date | None = None) -> SweepResult:
    """One generation sweep across every tenant. Safe to call directly."""
    result = SweepResult()
    ref_today = today or utc_today()

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(select(Organization.id, Organization.db_name))
        tenants = list(rows.all())

    for _org_id, db_name in tenants:
        result.tenants_scanned += 1
        try:
            outcome = await _sweep_tenant(db_name, ref_today)
            result.invoices_generated += outcome.generated
            result.templates_skipped += outcome.templates_skipped
            result.templates_paused += outcome.templates_paused
            result.template_failures += outcome.template_failures
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            logger.warning("[recurring] failed sweeping %s: %s", db_name, exc.__class__.__name__)
            result.failures += 1

    if (
        result.invoices_generated
        or result.templates_skipped
        or result.failures
        or result.template_failures
    ):
        logger.info(
            "[recurring] swept %d tenant(s); generated=%d skipped=%d paused=%d "
            "failed_sweeps=%d failed_templates=%d",
            result.tenants_scanned,
            result.invoices_generated,
            result.templates_skipped,
            result.templates_paused,
            result.failures,
            result.template_failures,
        )
    return result


def _still_due(template: RecurringInvoiceTemplate, today: date) -> bool:
    """Re-test, under the row lock, the predicate the id query selected on.

    Step 2 of the two-phase shape in ``../docs/background-sweeps.md`` § Locking,
    and it is **correctness, not an optimisation**.

    ``uq_invoice_recurring_period`` does NOT cover this. That index makes a
    *duplicate* invoice for one ``(template, period_key)`` impossible; the
    failure mode here produces an invoice for a period that is **not due yet**,
    which the index is happy to accept. Two replicas read the same template id
    unlocked; replica A generates period P, advances ``next_run_on`` to P+1 and
    commits; replica B then locks the row, reads the *fresh* P+1 cursor and — on
    the old code, which took whatever ``next_run_on`` said with no re-check —
    generates P+1 early, on a distinct period key the index cannot object to. A
    subscription invoice appears in the approval queue for a month that has not
    started, and the cursor jumps to P+2 so the real P+1 tick raises nothing.

    A ``status`` that is no longer ``active`` is the other half: the sweep's own
    auto-pause, or an admin's ``pause``/``end``, can land between the id read
    and the lock, and a paused schedule must not raise one more invoice.

    Deliberately the same three clauses as the id query.
    """
    if template.status != STATUS_ACTIVE:
        return False
    return template.next_run_on is not None and template.next_run_on <= today


def _advance_cursor(template: RecurringInvoiceTemplate, run_on: date) -> None:
    """Force ``next_run_on`` forward one period if generation didn't move it.

    Defensive: the already-generated no-op and the non-generatable skip both
    leave the cursor where it was, and a stuck ``next_run_on <= today`` would
    re-select the same template on every tick forever.
    """
    if template.next_run_on is None or template.next_run_on > run_on:
        return
    months = _months_per_period(template.cadence)
    template.next_run_on = compute_next_run_on(
        template.cadence,
        template.day_of_period,
        after=_add_months(run_on, months),
        start_date=template.start_date,
        end_date=template.end_date,
    )


async def _handle_non_generatable(
    db: AsyncSession,
    template: RecurringInvoiceTemplate,
    *,
    period_key: str,
    reason: str,
    outcome: TenantSweepOutcome,
) -> None:
    """Persist, audit and (past the cap) pause a template that can't generate."""
    marker = record_generation_skip(
        template, period_key=period_key, reason=reason, at=datetime.now(UTC)
    )
    outcome.templates_skipped += 1
    # Correlate the skip — and the pause it may trigger — on the template's own
    # id, so a reader following one template's generation problem gets the
    # whole thread rather than one row per unrelated correlation.
    await dispatch_audit(
        db,
        correlation_id=template.id,
        organization_id=template.organization_id,
        actor_id=None,
        action="recurring_template.generation_skipped",
        entity_type="recurring_invoice_template",
        entity_id=template.id,
        details={
            "reason": reason,
            "period_key": period_key,
            "consecutive": marker["consecutive"],
        },
    )
    if marker["consecutive"] >= MAX_CONSECUTIVE_SKIPS and template.status == STATUS_ACTIVE:
        # Stop pretending to be a live schedule. `resume` re-anchors the cursor
        # from today, so nothing back-fires the periods slept through.
        template.status = STATUS_PAUSED
        outcome.templates_paused += 1
        await dispatch_audit(
            db,
            correlation_id=template.id,
            organization_id=template.organization_id,
            actor_id=None,
            action="recurring_template.paused",
            entity_type="recurring_invoice_template",
            entity_id=template.id,
            details={
                "status": STATUS_PAUSED,
                "source": "sweep",
                "reason": reason,
                "consecutive_skips": marker["consecutive"],
            },
        )


async def _sweep_tenant(db_name: str, today: date) -> TenantSweepOutcome:
    """Generate due invoices for one tenant.

    Finds ``active`` templates with ``next_run_on <= today`` and generates the
    due period for each, capped at ``recurring_invoices_max_per_sweep`` per
    tick. Never moves money.

    **Per template, not per tenant.** Each template is re-read by id, guarded,
    and committed on its own. The loop used to run every template in one
    transaction with a single commit at the end, so one template that raised
    aborted the tenant's whole tick *and discarded the invoices already
    generated on it* — the identical shape fixed in ``vendor_rescreen`` and
    ``payment_erp_sync``. Re-reading by id matters because a rollback expires
    the ORM objects a pre-loaded list would still hold, and touching one of
    those from async SQLAlchemy is a ``MissingGreenlet``, not a clean failure.

    **Two-phase, one row locked at a time**, the shape every mutating sweep uses
    (``../docs/background-sweeps.md`` § Locking): candidate ids are read
    UNLOCKED in a total order, then each is re-read ``FOR UPDATE``, re-checked
    against the predicate the id query used (:func:`_still_due`), generated, and
    committed on its own — which releases the lock before the next template is
    touched. Capping rather than paginating is legitimate here because a
    generated period advances ``next_run_on`` and so leaves the candidate set;
    the next tick resumes past it.

    The re-check is the half this sweep was missing, and
    ``uq_invoice_recurring_period`` does not substitute for it — see
    :func:`_still_due` for the not-yet-due period the index cannot catch.
    """
    cap = int(settings.recurring_invoices_max_per_sweep)
    outcome = TenantSweepOutcome()
    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            template_ids = (
                (
                    await db.execute(
                        select(RecurringInvoiceTemplate.id)
                        .where(
                            RecurringInvoiceTemplate.status == STATUS_ACTIVE,
                            RecurringInvoiceTemplate.next_run_on.isnot(None),
                            RecurringInvoiceTemplate.next_run_on <= today,
                        )
                        # Oldest-due first, `id` as the tiebreak so the order is
                        # TOTAL, not merely mostly-sorted: every replica then
                        # locks the same rows in the same sequence and two
                        # sweeps queue instead of deadlocking.
                        .order_by(
                            RecurringInvoiceTemplate.next_run_on.asc(),
                            RecurringInvoiceTemplate.id.asc(),
                        )
                        .limit(cap)
                    )
                )
                .scalars()
                .all()
            )

            for template_id in template_ids:
                try:
                    # `with_for_update` bypasses the identity map, so this is a
                    # real `SELECT ... FOR UPDATE` on exactly one row.
                    template = await db.get(
                        RecurringInvoiceTemplate, template_id, with_for_update=True
                    )
                    if template is None or not _still_due(template, today):
                        # Deleted, paused/ended, or already generated-and-advanced
                        # by another replica between the id read and the lock.
                        # End the transaction so the lock is released now rather
                        # than at the end of the tick.
                        await db.rollback()
                        continue
                    run_on = template.next_run_on
                    period_key = period_key_for(template.cadence, run_on)

                    reason = not_generatable_reason(template)
                    if reason is not None:
                        await _handle_non_generatable(
                            db,
                            template,
                            period_key=period_key,
                            reason=reason,
                            outcome=outcome,
                        )
                        _advance_cursor(template, run_on)
                        await db.commit()
                        continue

                    # Generatable — so any marker from an earlier period is
                    # stale, including on the already-generated no-op path
                    # below, which never reaches `generate_one`'s own clear.
                    clear_generation_skip(template)

                    # generate_one() returns the SAME non-None Invoice whether
                    # it just created one or hit the (template, period_key)
                    # idempotency guard and returned the pre-existing row — so
                    # "invoice is not None" alone can't tell a real create from
                    # a no-op. Pre-check the period ourselves and only call
                    # generate_one (and count it) when the period genuinely has
                    # no invoice yet; an already-generated period is a no-op
                    # that must NOT inflate the `generated` metric/log.
                    already_generated = (
                        await db.execute(
                            select(Invoice.id).where(
                                Invoice.recurring_template_id == template.id,
                                Invoice.recurring_period_key == period_key,
                            )
                        )
                    ).scalar_one_or_none() is not None
                    if not already_generated:
                        invoice = await generate_one(db, template, run_on=run_on, actor_id=None)
                        if invoice is not None:
                            outcome.generated += 1
                    _advance_cursor(template, run_on)
                    await db.commit()
                except Exception as exc:  # noqa: BLE001 — one template must not halt the tenant
                    # Class only — a DB/asyncpg error message can echo a vendor
                    # name or a denormalised amount (PII-out-of-logs).
                    logger.warning(
                        "[recurring] template=%s generation failed in %s: %s",
                        template_id,
                        db_name,
                        exc.__class__.__name__,
                    )
                    await db.rollback()
                    outcome.template_failures += 1

            return outcome
    finally:
        await engine.dispose()


async def run_recurring_invoices_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; cancelled on shutdown.
    Body is the shared ``sweep_health.run_sweep_loop``."""
    await run_sweep_loop(
        SWEEP_RECURRING_INVOICES,
        lambda: generate_recurring_invoices_once(),
        interval_seconds=settings.recurring_invoices_interval_seconds,
        log=logger,
        log_prefix="[recurring]",
    )
