"""Which chart an invoice's GL code resolves against — and refusing one that
belongs to a different entity's chart.

``Invoice.gl_account`` and ``InvoiceLineItem.gl_account`` store a GL **code**,
not a foreign key (``docs/decisions.md`` §186). A code is only meaningful
relative to a chart, and an invoice's chart is the one its OWN entity sees:
the shared accounts (``gl_accounts.entity_id IS NULL``) plus that entity's own
— the same ``shared ∪ own`` union ``list_gl_accounts``, the extraction catalog
and ``gl_recode._ActiveChart`` read.

Two subsidiaries may each define their own ``6000`` (``docs/multi-entity.md``
§ A code is unique within the chart it belongs to). So in the consolidated view
the pickers used to offer subsidiary B's ``6000`` for a subsidiary-A invoice,
and every manual write path accepted it: the stored string ``"6000"`` then
resolved against A's chart — a different account, or none — while every reader
of the column (budgets, matching rules, 1099 box maps, approval routing,
reports) silently treated it as A's.

This module is the one place that decision is made. It refuses a code that
exists ONLY in some other entity's chart. It deliberately does not refuse a
code that is in no chart at all (hand-typed, or on a retired account): that is
a separate question with its own trade-offs — historical CSV imports carry
codes whose accounts are long gone — tracked in ``docs/followups.md``. What is
refused here is never correct for the invoice it is written to.

Ownership is read over every row, active or retired: a code the invoice's own
chart holds only as a RETIRED account still resolves to that account, so it is
not "another entity's", and a code retired in B is still B's.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gl_account import GLAccount


@dataclass(frozen=True)
class ChartOwnership:
    """Which chart(s) each code belongs to, for one tenant.

    ``shared`` holds codes on shared rows; ``by_entity`` maps an entity to the
    codes it owns. Built for a bounded set of codes (the ones being written),
    so it never holds the whole chart unless asked to.
    """

    shared: frozenset[str]
    by_entity: dict[uuid.UUID, frozenset[str]]

    def in_own_chart(self, code: str, entity_id: uuid.UUID | None) -> bool:
        """Is ``code`` in ``shared ∪ entity_id's own`` chart?

        ``entity_id`` None (an invoice no entity was ever stamped on) sees the
        shared chart alone — the rule ``gl_recode._ActiveChart.is_valid_for``
        and the extraction catalog already apply to such an invoice.
        """
        if code in self.shared:
            return True
        return entity_id is not None and code in self.by_entity.get(entity_id, frozenset())

    def belongs_elsewhere(self, code: str, entity_id: uuid.UUID | None) -> bool:
        """True when ``code`` resolves in some OTHER entity's chart and not in
        this invoice's own — the write this module exists to refuse."""
        if self.in_own_chart(code, entity_id):
            return False
        return any(code in codes for owner, codes in self.by_entity.items() if owner != entity_id)


async def load_chart_ownership(
    db: AsyncSession,
    organization_id: uuid.UUID,
    codes: Iterable[str],
) -> ChartOwnership:
    """Ownership of ``codes`` across every chart in the tenant (one query)."""
    wanted = sorted({c for c in codes if c})
    if not wanted:
        return ChartOwnership(shared=frozenset(), by_entity={})
    rows = (
        await db.execute(
            select(GLAccount.code, GLAccount.entity_id).where(
                GLAccount.organization_id == organization_id,
                GLAccount.code.in_(wanted),
            )
        )
    ).all()
    shared: set[str] = set()
    by_entity: dict[uuid.UUID, set[str]] = {}
    for code, owner in rows:
        if owner is None:
            shared.add(code)
        else:
            by_entity.setdefault(owner, set()).add(code)
    return ChartOwnership(
        shared=frozenset(shared),
        by_entity={owner: frozenset(c) for owner, c in by_entity.items()},
    )


def foreign_codes_detail(codes: list[str], *, where: str = "") -> str:
    """The refusal a caller sees. Names the codes — chart-of-accounts
    configuration, not PII — because they are exactly what has to change."""
    listed = ", ".join(f"'{c}'" for c in codes)
    noun = "GL account" if len(codes) == 1 else "GL accounts"
    verb = "belongs" if len(codes) == 1 else "belong"
    prefix = f"{where}: " if where else ""
    return (
        f"{prefix}{noun} {listed} {verb} to another entity's chart of accounts, not "
        "this invoice's. Choose a code from the invoice's own chart — the shared "
        "accounts plus its entity's own."
    )


async def refuse_foreign_gl_codes(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    codes: Iterable[str | None],
    where: str = "",
) -> None:
    """Raise 422 if any of ``codes`` belongs only to another entity's chart.

    Call it with the codes a write is about to STORE that are new to the row —
    re-saving a code the row already carries is not a coding decision, and
    refusing it would make an invoice uneditable the day its account moved.
    Blank / ``None`` codes (clearing the field) always pass.
    """
    wanted = [c for c in codes if c]
    if not wanted:
        return
    ownership = await load_chart_ownership(db, organization_id, wanted)
    foreign = sorted({c for c in wanted if ownership.belongs_elsewhere(c, entity_id)})
    if foreign:
        raise HTTPException(status_code=422, detail=foreign_codes_detail(foreign, where=where))
