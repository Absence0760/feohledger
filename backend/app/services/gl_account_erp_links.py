"""A GL account's ERP link state in each company it has been synced under
(docs/decisions.md §376; the vendor counterpart is ``services/vendor_erp_links``,
§356).

An ERP account id names an account only inside the company that issued it
(§284, ``services/erp_link_realm``). A ``gl_accounts`` row holds its state in
ONE company: the link (``erp_account_id`` + ``erp_link_realm``), a person's
confirmation of it (``erp_link_confirmed_*``), or — unlinked — the decision it
is waiting for (``erp_link_candidates``, whose ``realm`` names the company).
``gl_account_erp_links`` (:class:`~app.models.gl_account.GLAccountErpLink`)
keeps that state for every OTHER named company, so a tenant that switches ERP
company and back gets back the links a person confirmed and the decisions
still open, instead of re-linking by code and asking again.

``services/gl_account_sync`` is the only caller, and calls both helpers before
rule 0, in this order, on a pull with a named realm:

* :func:`keep_states` files each row's state from another named company under
  that company. A NULL-realm link (pre-0115, or made by a no-realm pull) names
  no company, so filing it under one would be the manufactured evidence §141
  refuses; it is never kept.
* :func:`restore_states` puts back the state kept for the pull's own company,
  and deletes it from the table. A restored link is then judged by the rules
  exactly as a link that never left the row: rules 3 and 4 re-judge it unless
  it is confirmed, and a restored decision is refreshed against the pull.

A pull with no realm (``mock``) neither keeps nor restores: it speaks for no
company. Nothing here is posted to; the push reads the row's own live link.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gl_account import GLAccount, GLAccountErpLink

__all__ = ["Restore", "keep_states", "restore_states"]

#: Rows per INSERT: 8 binds each, well under asyncpg's 32767-parameter limit.
_CHUNK = 1000


def _states_to_keep(row: GLAccount, realm: str) -> dict[str, dict]:
    """``row``'s state in each named company other than ``realm``, by company."""
    states: dict[str, dict] = {}
    if row.erp_account_id and row.erp_link_realm is not None and row.erp_link_realm != realm:
        states[row.erp_link_realm] = {
            "erp_account_id": row.erp_account_id,
            "erp_link_confirmed_at": row.erp_link_confirmed_at,
            "erp_link_confirmed_by": row.erp_link_confirmed_by,
            "erp_link_candidates": None,
        }
    pending = row.erp_link_candidates or None
    pending_realm = pending.get("realm") if pending else None
    if pending_realm is not None and pending_realm != realm:
        state = states.setdefault(
            pending_realm,
            {
                "erp_account_id": None,
                "erp_link_confirmed_at": None,
                "erp_link_confirmed_by": None,
            },
        )
        state["erp_link_candidates"] = pending
    return states


async def keep_states(db: AsyncSession, rows: Iterable[GLAccount], *, realm: str) -> int:
    """File every row's state from a named company other than ``realm`` under
    that company, before the sync moves or drops it. Returns how many were
    filed. One kept state per ``(gl_account_id, realm)``: a later one for the
    same company replaces the earlier, since the row only held one at a time.
    """
    values = [
        {
            "id": uuid.uuid4(),
            "organization_id": row.organization_id,
            "gl_account_id": row.id,
            "realm": kept_realm,
            **state,
        }
        for row in rows
        for kept_realm, state in _states_to_keep(row, realm).items()
    ]
    for start in range(0, len(values), _CHUNK):
        stmt = pg_insert(GLAccountErpLink).values(values[start : start + _CHUNK])
        await db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_gl_account_erp_links_account_realm",
                set_={
                    "erp_account_id": stmt.excluded.erp_account_id,
                    "erp_link_confirmed_at": stmt.excluded.erp_link_confirmed_at,
                    "erp_link_confirmed_by": stmt.excluded.erp_link_confirmed_by,
                    "erp_link_candidates": stmt.excluded.erp_link_candidates,
                    "updated_at": func.now(),
                },
            )
        )
    return len(values)


@dataclass
class Restore:
    """What :func:`restore_states` did to one row."""

    row: GLAccount
    #: The row's link before the restore (its other-company state is kept).
    from_id: str | None
    from_realm: str | None
    #: False: the kept link was dropped, not restored (``erp_id_taken``).
    restored: bool


async def restore_states(
    db: AsyncSession,
    organization_id: uuid.UUID,
    rows: Iterable[GLAccount],
    *,
    realm: str,
) -> list[Restore]:
    """Put back every state kept for ``realm`` onto its row, and remove it
    from the table. Call after :func:`keep_states`, so the state each row
    holds now is already kept under its own company.

    A kept state was made in this company and left the row only because a
    sync ran under another one, so it comes back exactly as it was, confirmed
    link and open decision included, and is re-judged by the rules that follow
    as any same-company state is. One exception, dropped and reported: an
    UNCONFIRMED kept link whose id another unconfirmed row already holds here,
    or that two unconfirmed kept links claim — the rules never leave two rows
    holding one account on their own judgement, and which row is the account's
    would be a guess. The row then keeps its current state for rule 0 to
    judge. A confirmed link always comes back: a person chose it, and it may
    share its account with the row holding that account's own code (rule 1's
    one sanctioned share). A row that already holds this company's state wins
    over the kept one.
    """
    # Read as plain tuples, not ORM rows: the rows are deleted below by a bulk
    # statement, and a stale instance left in the session would be a hazard.
    states = (
        await db.execute(
            select(
                GLAccountErpLink.gl_account_id,
                GLAccountErpLink.erp_account_id,
                GLAccountErpLink.erp_link_confirmed_at,
                GLAccountErpLink.erp_link_confirmed_by,
                GLAccountErpLink.erp_link_candidates,
            )
            .where(
                GLAccountErpLink.organization_id == organization_id,
                GLAccountErpLink.realm == realm,
            )
            .order_by(GLAccountErpLink.id)
        )
    ).all()
    if not states:
        return []
    await db.execute(
        delete(GLAccountErpLink).where(
            GLAccountErpLink.organization_id == organization_id,
            GLAccountErpLink.realm == realm,
        )
    )
    by_id = {row.id: row for row in rows}
    held = {
        row.erp_account_id
        for row in by_id.values()
        if row.erp_account_id and row.erp_link_realm == realm and row.erp_link_confirmed_at is None
    }
    claims = Counter(
        erp_id for _, erp_id, confirmed_at, *_ in states if erp_id and not confirmed_at
    )

    done: list[Restore] = []
    for gl_account_id, erp_id, confirmed_at, confirmed_by, candidates in states:
        row = by_id.get(gl_account_id)
        if row is None:
            continue
        own_link = row.erp_account_id is not None and row.erp_link_realm == realm
        own_pending = (row.erp_link_candidates or {}).get("realm") == realm
        if own_link or own_pending:
            continue
        from_id, from_realm = row.erp_account_id, row.erp_link_realm
        if erp_id and confirmed_at is None and (erp_id in held or claims[erp_id] > 1):
            done.append(Restore(row, from_id, from_realm, restored=False))
            continue
        row.erp_account_id = erp_id
        row.erp_link_realm = realm if erp_id else None
        row.erp_link_confirmed_at = confirmed_at
        row.erp_link_confirmed_by = confirmed_by
        row.erp_link_candidates = candidates
        if erp_id and confirmed_at is None:
            held.add(erp_id)
        done.append(Restore(row, from_id, from_realm, restored=True))
    return done
