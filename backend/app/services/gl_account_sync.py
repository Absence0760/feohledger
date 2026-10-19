"""Link an ERP's chart of accounts into a tenant's chart — by the ERP's key.

``POST /api/gl-accounts/sync-erp`` hands :func:`sync_chart_from_erp` what the
adapter's ``list_gl_accounts`` returned. Every account the ERP sends is matched
to at most one of our rows, and a row is linked (``erp_account_id``) to at most
one ERP account, because that link is what a bill posts against
(``gl_chart.resolve_erp_account_ids``): a row linked to the wrong ERP account
books the expense somewhere the bookkeeper never chose.

The rules, in the order they apply (docs/decisions.md §284):

1. **By the ERP's id first.** An account whose ``erp_account_id`` a row already
   holds updates that row — its name, type and parent — so a rename in the ERP
   renames our row instead of creating a second one. The row's **code is never
   rewritten**: an invoice records its GL as that string
   (``schemas/gl_account.GLAccountUpdate``), and changing it would detach every
   line already coded to it. A row whose code no longer matches the ERP's is
   reported (``code_drift``) and stays correctly linked by id.
2. **Then by code**, which must be a key the ERP keeps unique
   (``GLAccountPayload``). A row with that code and no live link is linked; a
   row with that code already linked to ANOTHER account in this pull is a
   collision, refused.
3. **A code two accounts in one pull resolve to is refused for both** — neither
   is created nor linked, and an existing row with that code is unlinked. Never
   "the later one wins": that is how 89 QuickBooks accounts became 81 rows and a
   bill coded to one "Job Materials" posted to the other.
4. **A row keyed by a legacy code two accounts claim is unlinked.** Before §284
   QuickBooks sub-accounts were keyed by their bare ``Name``, so a row "Job
   Materials" may have merged two accounts and be linked to whichever the sync
   read last. Its id says which ERP account it posts to, not which one the
   invoices coded to it meant, so the link is dropped: those invoices are then
   refused ``account_not_linked`` before any push and get recoded. The row and
   its invoices are left in place — nothing is deleted or recoded for them.

Nothing here commits; the caller audits and commits.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gl_account import GL_CODE_MAX_LENGTH, GLAccount
from app.services.erp_adapters.base import GLAccountPayload
from app.tenant import apply_entity_scope

#: Why an ERP account was not linked (a stable code; the browser localizes it).
DUPLICATE_CODE = "duplicate_code"  # two accounts in this pull resolve to the code
DUPLICATE_ERP_ID = "duplicate_erp_id"  # one ERP id arrived with two codes
CODE_TAKEN = "code_taken"  # our row with the code is linked to another live account
AMBIGUOUS_LEGACY = "ambiguous_legacy"  # our row's code is a legacy key two accounts share
CODE_TOO_LONG = "code_too_long"  # longer than GL_CODE_MAX_LENGTH; never truncated

#: Cap on the entries a sync RESPONSE lists; the counts are always exact, and
#: the audit row lists everything.
MAX_LISTED = 50


@dataclass
class ChartSyncResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    #: An existing row with no link, or one to an account absent from the pull,
    #: now linked to the account that holds its code (a manually created row,
    #: or a chart re-pointed at a different ERP company).
    linked: int = 0
    #: ``{"code", "reason", "erp_account_ids"}`` per refused code, one entry per
    #: code (every ERP account that resolved to it), sorted by code.
    conflicts: list[dict] = field(default_factory=list)
    #: Codes of existing rows whose ERP link this run REMOVED (rules 3 and 4).
    unlinked: list[str] = field(default_factory=list)
    #: ``{"code", "erp_code"}`` for rows linked by id whose code is not the
    #: ERP's current one (kept: see rule 1).
    code_drift: list[dict] = field(default_factory=list)
    #: ``{"code", "from", "to"}`` for every row whose ``erp_account_id`` this run
    #: changed (``to`` None = unlinked) — where its bills posted before and
    #: where they post now. The audit row records it in full.
    link_changes: list[dict] = field(default_factory=list)

    def summary(self, *, capped: bool = True) -> dict:
        """The response projection (lists capped at :data:`MAX_LISTED`; counts
        always exact), or with ``capped=False`` the audit row's, which keeps
        every entry — an audit of where money now posts that stops at 50 is
        not one."""

        def listed(items: list) -> list:
            return items[:MAX_LISTED] if capped else list(items)

        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "linked": self.linked,
            "conflict_count": len(self.conflicts),
            "conflicts": listed(self.conflicts),
            "unlinked_count": len(self.unlinked),
            "unlinked": listed(self.unlinked),
            "code_drift_count": len(self.code_drift),
            "code_drift": listed(self.code_drift),
            "link_changes": listed(self.link_changes),
        }


def _preference(entity_id: uuid.UUID | None):
    """The row that answers when a key exists in more than one scope.

    Candidates are ``shared (NULL) ∪ the selected entity`` — the invoice-side
    rule ``services/gl_recode._ActiveChart.is_valid_for`` already applies, so
    the sync resolves a code exactly the way validation does (matching on the
    organization alone once made a sync under subsidiary B update A's row).

    * an entity is selected → **its own** row outranks the shared one (an
      entity-specific account overrides the shared chart for that entity);
    * consolidated → the **shared** row wins, because that is the chart a
      consolidated sync creates into. Ties break oldest-first so the pick is
      deterministic rather than whatever Postgres returns first.
    """
    is_shared = GLAccount.entity_id.is_(None)
    return (
        is_shared.desc() if entity_id is None else is_shared.asc(),
        GLAccount.created_at.asc(),
        GLAccount.id.asc(),
    )


async def sync_chart_from_erp(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    accounts: list[GLAccountPayload],
) -> ChartSyncResult:
    """Apply one pull of the ERP's chart to the chart being synced INTO —
    shared ∪ ``entity_id``'s own (every row, consolidated) — creating new rows
    in ``entity_id``'s chart (shared when None). See the module docstring."""
    result = ChartSyncResult()

    # -- the pull, reduced to one candidate per code ------------------------
    by_code: dict[str, list[GLAccountPayload]] = defaultdict(list)
    codes_by_id: dict[str, set[str]] = defaultdict(set)
    too_long: dict[str, list[GLAccountPayload]] = defaultdict(list)
    for acct in accounts:
        code = (acct.code or "").strip()
        if not code:
            continue
        if len(code) > GL_CODE_MAX_LENGTH:
            too_long[code].append(acct)
            continue
        if acct.erp_account_id:
            codes_by_id[acct.erp_account_id].add(code)
        # The same account listed twice (a provider paging overlap) is one.
        if not any(seen.erp_account_id == acct.erp_account_id for seen in by_code[code]):
            by_code[code].append(acct)

    refused: dict[str, str] = {}  # code -> reason
    for code, group in by_code.items():
        if len(group) > 1:
            refused[code] = DUPLICATE_CODE
    for erp_id, codes in codes_by_id.items():
        if len(codes) > 1:
            for code in codes:
                refused.setdefault(code, DUPLICATE_ERP_ID)
    candidates = {code: group[0] for code, group in by_code.items() if code not in refused}
    # Every id in the pull, refused and over-long ones included: a row linked to
    # any of them points at an account that still exists, so its link is not
    # "dead" and must not be re-pointed at whatever account holds its code.
    live_ids = {a.erp_account_id for g in by_code.values() for a in g if a.erp_account_id}
    live_ids |= {a.erp_account_id for g in too_long.values() for a in g if a.erp_account_id}
    code_of_id = {a.erp_account_id: code for code, a in candidates.items() if a.erp_account_id}

    # The ERP accounts each LEGACY code (an older adapter's key) named. Only
    # legacy codes: a row keyed by one that two accounts share may have merged
    # them (rule 4). A current code another account holds is rule 2's business,
    # not a sign of a merge — a row renamed in the ERP keeps its link.
    legacy_claims: dict[str, set[str]] = defaultdict(set)
    for group in by_code.values():
        for acct in group:
            for legacy in acct.legacy_codes:
                if legacy and acct.erp_account_id:
                    legacy_claims[legacy].add(acct.erp_account_id)

    # -- the chart being synced into, read once -----------------------------
    rows = (
        (
            await db.execute(
                apply_entity_scope(
                    select(GLAccount).where(GLAccount.organization_id == organization_id),
                    GLAccount,
                    entity_id,
                    include_shared=True,
                ).order_by(*_preference(entity_id))
            )
        )
        .scalars()
        .all()
    )

    def unlink(row: GLAccount) -> None:
        if row.erp_account_id is not None:
            result.link_changes.append({"code": row.code, "from": row.erp_account_id, "to": None})
            row.erp_account_id = None
            result.unlinked.append(row.code)

    def legacy_ambiguous(row: GLAccount, erp_id: str | None) -> bool:
        """Rule 4: did an older adapter key some OTHER account by ``row``'s code?"""
        return bool(legacy_claims.get(row.code, set()) - {erp_id})

    # The unlinks run over EVERY row in scope, not just the preferred one per
    # key: a shared row and an entity's override can both hold a bad link, and
    # whichever one this pass skipped would keep posting to it for the invoices
    # that resolve to it.
    for row in rows:
        linked_to = row.erp_account_id
        if linked_to is None:
            continue
        if row.code in refused:
            # Rule 3: two ERP accounts answer to the code this row shows.
            unlink(row)
        elif (
            linked_to in code_of_id
            and row.code != code_of_id[linked_to]
            and legacy_ambiguous(row, linked_to)
        ):
            # Rule 4: a pre-§284 row that merged this account with another.
            # Its invoices are refused and recoded; nothing posts on a guess.
            # The ERP account gets its own row under its real code below.
            unlink(row)

    row_by_id: dict[str, GLAccount] = {}
    row_by_code: dict[str, GLAccount] = {}
    for row in rows:  # first one wins: the order IS the preference
        if row.erp_account_id:
            row_by_id.setdefault(row.erp_account_id, row)
        row_by_code.setdefault(row.code, row)

    claimed_rows: set[uuid.UUID] = set()

    for code in sorted(candidates):
        acct = candidates[code]
        erp_id = acct.erp_account_id

        # -- rule 1: by the ERP's id --------------------------------------
        row = row_by_id.get(erp_id) if erp_id else None
        if row is not None and row.id not in claimed_rows:
            claimed_rows.add(row.id)
            if row.code != code:
                result.code_drift.append({"code": row.code, "erp_code": code})
            _apply(row, acct, result)
            continue

        # -- rule 2: by code ----------------------------------------------
        row = row_by_code.get(code)
        if row is not None:
            if row.id in claimed_rows:
                refused.setdefault(code, CODE_TAKEN)
                continue
            linked_to = row.erp_account_id
            if linked_to and linked_to != erp_id and linked_to in live_ids:
                # Linked to another account that is in this very pull.
                refused.setdefault(code, CODE_TAKEN)
                continue
            if legacy_ambiguous(row, erp_id):
                # Our row shows a code several ERP accounts answer to; linking
                # it to this one would be a guess.
                unlink(row)
                refused.setdefault(code, AMBIGUOUS_LEGACY)
                continue
            claimed_rows.add(row.id)
            if row.erp_account_id != erp_id:
                result.link_changes.append({"code": code, "from": row.erp_account_id, "to": erp_id})
                row.erp_account_id = erp_id
                result.linked += 1
                _apply(row, acct, result, count=False)
            else:
                _apply(row, acct, result)
            continue

        # -- new -----------------------------------------------------------
        db.add(
            GLAccount(
                code=code,
                name=acct.name,
                account_type=acct.account_type,
                parent_code=acct.parent_code,
                organization_id=organization_id,
                entity_id=entity_id,
                erp_account_id=erp_id,
            )
        )
        result.created += 1

    for code, group in too_long.items():
        refused.setdefault(code, CODE_TOO_LONG)
        by_code.setdefault(code, []).extend(group)

    result.conflicts = [
        {
            "code": code,
            "reason": reason,
            "erp_account_ids": sorted(
                {a.erp_account_id for a in by_code.get(code, []) if a.erp_account_id}
                | legacy_claims.get(code, set())
            ),
        }
        for code, reason in sorted(refused.items())
    ]
    result.unlinked.sort()
    return result


def _apply(row: GLAccount, acct: GLAccountPayload, result: ChartSyncResult, *, count=True):
    """Refresh a matched row's descriptive fields. Counts as updated only when
    something actually changed, so a re-run of an unchanged chart reports 0."""
    changed = False
    if acct.name and row.name != acct.name:
        row.name = acct.name
        changed = True
    if acct.account_type and row.account_type != acct.account_type:
        row.account_type = acct.account_type
        changed = True
    if acct.parent_code is not None and row.parent_code != acct.parent_code:
        row.parent_code = acct.parent_code
        changed = True
    if not count:
        return
    if changed:
        result.updated += 1
    else:
        result.unchanged += 1
