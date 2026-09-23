"""Which chart an invoice's GL code resolves against — and refusing a code that
does not resolve to an account the invoice may be coded to.

``Invoice.gl_account`` and ``InvoiceLineItem.gl_account`` store a GL **code**,
not a foreign key (``docs/decisions.md`` §186). A code is only meaningful
relative to a chart, and an invoice's chart is the one its OWN entity sees:
the shared accounts (``gl_accounts.entity_id IS NULL``) plus that entity's own
— the same ``shared ∪ own`` union ``list_gl_accounts``, the extraction catalog
and ``gl_recode._ActiveChart`` read.

This module is the one place the rule for a WRITTEN code is decided, in two
parts:

* **Never another entity's** (§194). Two subsidiaries may each define their
  own ``6000`` (``docs/multi-entity.md`` § A code is unique within the chart it
  belongs to), so a code that exists ONLY in some other entity's chart would
  resolve against this invoice's chart as a different account, or none, while
  every reader of the column (budgets, matching rules, 1099 box maps, approval
  routing, reports) silently treated it as this invoice's.
* **An active account of its own chart, whenever that chart has any** (§199).
  A code in no chart at all — mistyped, or hand-typed through the API — and a
  RETIRED account's code are refused on a new write once the invoice's
  effective ACTIVE chart is non-empty, the rule extraction and ``gl_recode``
  already applied to automated codes. With no active account to choose from
  (a tenant that has not synced its chart yet) there is nothing to hold a code
  to, so only the first part applies.

Ownership is read over every row, active or retired: a code the invoice's own
chart holds only as a RETIRED account still resolves to that account, so it is
"retired", not "another entity's", and a code retired in B is still B's.

Only codes a write is about to STORE that are new to the row are judged — the
callers pass exactly those. Re-saving a code the row already carries is not a
coding decision, and refusing it would freeze an invoice the day its account
was retired or moved.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gl_account import GLAccount


@dataclass(frozen=True)
class ChartOwnership:
    """Which chart(s) each code belongs to, for one tenant.

    ``shared`` holds codes on shared rows; ``by_entity`` maps an entity to the
    codes it owns — both over every row, active or retired. ``active_shared`` /
    ``active_by_entity`` are the same restricted to active rows. Built for a
    bounded set of codes (the ones being written) unless the whole tenant chart
    is asked for.
    """

    shared: frozenset[str]
    by_entity: dict[uuid.UUID, frozenset[str]]
    active_shared: frozenset[str] = frozenset()
    active_by_entity: dict[uuid.UUID, frozenset[str]] = field(default_factory=dict)

    def in_own_chart(self, code: str, entity_id: uuid.UUID | None) -> bool:
        """Is ``code`` in ``shared ∪ entity_id's own`` chart, active or retired?

        ``entity_id`` None (an invoice no entity was ever stamped on) sees the
        shared chart alone — the rule ``gl_recode._ActiveChart.is_valid_for``
        and the extraction catalog already apply to such an invoice.
        """
        if code in self.shared:
            return True
        return entity_id is not None and code in self.by_entity.get(entity_id, frozenset())

    def in_own_active_chart(self, code: str, entity_id: uuid.UUID | None) -> bool:
        """Is ``code`` an ACTIVE account in ``shared ∪ entity_id's own``?"""
        if code in self.active_shared:
            return True
        return entity_id is not None and code in self.active_by_entity.get(entity_id, frozenset())

    def belongs_elsewhere(self, code: str, entity_id: uuid.UUID | None) -> bool:
        """True when ``code`` resolves in some OTHER entity's chart and not in
        this invoice's own — the write §194 refuses."""
        if self.in_own_chart(code, entity_id):
            return False
        return any(code in codes for owner, codes in self.by_entity.items() if owner != entity_id)


async def load_chart_ownership(
    db: AsyncSession,
    organization_id: uuid.UUID,
    codes: Iterable[str] | None,
) -> ChartOwnership:
    """Ownership of ``codes`` across every chart in the tenant (one query).

    ``codes=None`` reads the tenant's WHOLE chart — for a caller that has to
    judge codes it cannot list up front (extraction, whose vendor-prior overlay
    lands a code only after the document's own codes were judged). An empty
    iterable is nothing to judge and issues no query.
    """
    stmt = select(GLAccount.code, GLAccount.entity_id, GLAccount.is_active).where(
        GLAccount.organization_id == organization_id
    )
    if codes is not None:
        wanted = sorted({c for c in codes if c})
        if not wanted:
            return ChartOwnership(shared=frozenset(), by_entity={})
        stmt = stmt.where(GLAccount.code.in_(wanted))
    rows = (await db.execute(stmt)).all()
    shared: set[str] = set()
    by_entity: dict[uuid.UUID, set[str]] = {}
    active_shared: set[str] = set()
    active_by_entity: dict[uuid.UUID, set[str]] = {}
    for code, owner, is_active in rows:
        if owner is None:
            shared.add(code)
            if is_active:
                active_shared.add(code)
        else:
            by_entity.setdefault(owner, set()).add(code)
            if is_active:
                active_by_entity.setdefault(owner, set()).add(code)
    return ChartOwnership(
        shared=frozenset(shared),
        by_entity={owner: frozenset(c) for owner, c in by_entity.items()},
        active_shared=frozenset(active_shared),
        active_by_entity={owner: frozenset(c) for owner, c in active_by_entity.items()},
    )


def chart_is_empty(*, has_shared: bool, has_own_entity: bool) -> bool:
    """The one rule behind "this invoice's effective ACTIVE chart — active
    shared accounts ∪ its own entity's — holds nothing at all."

    Two callers answer the same question from different data shapes and both
    compose through this predicate so the union can't drift between them:
    ``chart_has_active_accounts`` below asks it per invoice with one live
    query; ``gl_recode._ActiveChart.is_empty_for`` asks it per invoice too,
    but from the org-wide active set a bulk re-code pass already loaded once
    for every entity it will touch.
    """
    return not has_shared and not has_own_entity


async def chart_has_active_accounts(
    db: AsyncSession, organization_id: uuid.UUID, entity_id: uuid.UUID | None
) -> bool:
    """Does the effective ACTIVE chart of an invoice under ``entity_id`` —
    active shared accounts ∪ that entity's own — hold any account at all?

    The same set ``GET /api/gl-accounts?chart_entity_id=`` serves the invoice
    pickers, which is why they render a ``<select>`` exactly when this is true.
    """
    in_chart = (
        GLAccount.entity_id.is_(None)
        if entity_id is None
        else or_(GLAccount.entity_id.is_(None), GLAccount.entity_id == entity_id)
    )
    found = (
        await db.execute(
            select(GLAccount.id)
            .where(
                GLAccount.organization_id == organization_id,
                GLAccount.is_active == True,  # noqa: E712
                in_chart,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    # `in_chart` already ORs shared-or-own-entity into one query, so there is
    # no separate has_shared / has_own_entity to hand `chart_is_empty` here —
    # `found is not None` IS "not empty" for this combined read.
    return found is not None


def _codes_clause(codes: tuple[str, ...], singular: str, plural: str) -> str:
    listed = ", ".join(f"'{c}'" for c in codes)
    noun = "GL account" if len(codes) == 1 else "GL accounts"
    return f"{noun} {listed} {singular if len(codes) == 1 else plural}"


@dataclass(frozen=True)
class ChartRefusal:
    """The codes a write may not store, by reason. Falsy when there are none."""

    foreign: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.foreign or self.retired or self.unknown)

    def detail(self, where: str = "") -> str:
        """The refusal a caller sees. Names the codes — chart-of-accounts
        configuration, not PII — because they are exactly what has to change,
        and says WHY each is refused, since the fix differs (pick this entity's
        account; reactivate or re-code; correct a typo)."""
        clauses = []
        if self.foreign:
            clauses.append(
                _codes_clause(self.foreign, "belongs", "belong")
                + " to another entity's chart of accounts, not this invoice's"
            )
        if self.retired:
            clauses.append(
                _codes_clause(self.retired, "is", "are") + " retired in this invoice's chart"
            )
        if self.unknown:
            clauses.append(
                _codes_clause(self.unknown, "is", "are")
                + " not in this invoice's chart of accounts"
            )
        # Only a non-empty active chart can refuse a retired or unknown code, so
        # "an active code" is always something the caller can actually pick.
        pick = "an active code" if (self.retired or self.unknown) else "a code"
        prefix = f"{where}: " if where else ""
        return (
            f"{prefix}{'; '.join(clauses)}. Choose {pick} from the invoice's own chart — the "
            "shared accounts plus its entity's own."
        )


@dataclass(frozen=True)
class InvoiceChart:
    """What one invoice's chart says about the codes a write is storing.

    ``has_active_accounts`` is whether the invoice's effective ACTIVE chart is
    non-empty — the switch between the two halves of the rule.
    """

    ownership: ChartOwnership
    entity_id: uuid.UUID | None
    has_active_accounts: bool

    def judge(self, codes: Iterable[str | None], *, require_active: bool = True) -> ChartRefusal:
        """Classify ``codes`` (blank ones always pass).

        ``require_active=False`` applies only §194's refusal — for the one
        caller whose rows may legitimately carry an account that is gone: a CSV
        row imported as history (``services/csv_import``).
        """
        foreign: set[str] = set()
        retired: set[str] = set()
        unknown: set[str] = set()
        for code in {c for c in codes if c}:
            if self.ownership.in_own_active_chart(code, self.entity_id):
                continue
            if self.ownership.belongs_elsewhere(code, self.entity_id):
                foreign.add(code)
            elif require_active and self.has_active_accounts:
                if self.ownership.in_own_chart(code, self.entity_id):
                    retired.add(code)
                else:
                    unknown.add(code)
        return ChartRefusal(
            foreign=tuple(sorted(foreign)),
            retired=tuple(sorted(retired)),
            unknown=tuple(sorted(unknown)),
        )


async def load_invoice_chart(
    db: AsyncSession,
    organization_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    codes: Iterable[str | None],
) -> InvoiceChart:
    """Read what :meth:`InvoiceChart.judge` needs to rule on ``codes``.

    One query for the codes' ownership, plus — only when none of them is an
    active account of the invoice's own chart, i.e. only when something may be
    refused — one ``LIMIT 1`` read of whether that chart has any active account
    at all. A write naming a valid code therefore costs what §194's check did.
    """
    wanted = sorted({c for c in codes if c})
    ownership = await load_chart_ownership(db, organization_id, wanted)
    if any(ownership.in_own_active_chart(c, entity_id) for c in wanted):
        has_active = True
    elif wanted:
        has_active = await chart_has_active_accounts(db, organization_id, entity_id)
    else:
        has_active = False
    return InvoiceChart(ownership=ownership, entity_id=entity_id, has_active_accounts=has_active)


async def refuse_gl_codes_outside_chart(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    codes: Iterable[str | None],
    where: str = "",
) -> None:
    """Raise 422 unless every one of ``codes`` may be stored on an invoice filed
    under ``entity_id``: never another entity's (§194), and an active account
    of the invoice's own chart whenever that chart has any (§199).

    Call it with the codes a write is about to STORE that are new to the row —
    re-saving a code the row already carries is not a coding decision, and
    refusing it would make an invoice uneditable the day its account moved or
    was retired. Blank / ``None`` codes (clearing the field) always pass.
    """
    wanted = [c for c in codes if c]
    if not wanted:
        return
    chart = await load_invoice_chart(db, organization_id, entity_id, wanted)
    refusal = chart.judge(wanted)
    if refusal:
        raise HTTPException(status_code=422, detail=refusal.detail(where))
