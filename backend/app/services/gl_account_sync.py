"""Link an ERP's chart of accounts into a tenant's chart — by the ERP's key.

``POST /api/gl-accounts/sync-erp`` hands :func:`sync_chart_from_erp` what the
adapter's ``list_gl_accounts`` returned. Every account the ERP sends is matched
to at most one of our rows, and a row is linked (``erp_account_id``) to at most
one ERP account, because that link is what a bill posts against
(``gl_chart.resolve_erp_account_ids``): a row linked to the wrong ERP account
books the expense somewhere the bookkeeper never chose.

The rules, in the order they apply (docs/decisions.md §284):

0. **A link counts only in the company it was made in.** An ERP id is scoped
   to the company that issued it — QuickBooks numbers accounts per company, so
   sandbox "7" and production "7" are different accounts. Each link records
   its connection realm (``erp_link_realm``, ``services/erp_link_realm``). A
   link from another realm, or from before realms were recorded, is kept only
   when this pull shows the same id holding the row's own code; otherwise it
   is dead — dropped, re-matched by code, and recorded in ``link_changes``.
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

Rules 0, 3 and 4 run over the organization's WHOLE chart, whichever entity the
sync was run under: they are about which ERP account a code or id names, not
about which subsidiary holds the row. Matching and creating (rules 1, 2) run
over the chart being synced into.

**A person settles what the rules will not guess.** A row left unlinked by rule
3 or 4 keeps the ERP accounts it could mean (``erp_link_candidates``); an admin
or AP manager picks one through ``POST /api/gl-accounts/{id}/erp-link``, and
rules 3 and 4 never re-judge that confirmed link (it still dies with its realm,
or when its account leaves the ERP). Nothing here ever picks a candidate.

**Inactive accounts** arrive with ``active=False``: they still exist (a row
linked to one is not dead), a matched row is marked inactive in our chart, and
no row is created for one.

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

#: Why an ERP account was not linked (a stable code; the browser localizes it).
DUPLICATE_CODE = "duplicate_code"  # two accounts in this pull resolve to the code
DUPLICATE_ERP_ID = "duplicate_erp_id"  # one ERP id arrived with two codes
CODE_TAKEN = "code_taken"  # our row with the code is linked to another live account
AMBIGUOUS_LEGACY = "ambiguous_legacy"  # our row's code is a legacy key two accounts share
CODE_TOO_LONG = "code_too_long"  # longer than GL_CODE_MAX_LENGTH; never truncated
MISSING_ERP_ID = "missing_erp_id"  # the ERP sent no id: nothing could post to it

#: Cap on the entries a sync RESPONSE lists; the counts are always exact, and
#: the audit row lists everything.
MAX_LISTED = 50


@dataclass
class ChartSyncResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    #: An existing row with no live link, now linked to the account that holds
    #: its code (a manually created row, or a link from another ERP company).
    linked: int = 0
    #: ``{"code", "reason", "erp_account_ids"}`` per refused code, one entry per
    #: code (every ERP account that resolved to it), sorted by code.
    conflicts: list[dict] = field(default_factory=list)
    #: Codes of existing rows whose ERP link this run REMOVED (rules 0, 3, 4).
    unlinked: list[str] = field(default_factory=list)
    #: ``{"code", "erp_code"}`` for rows linked by id whose code is not the
    #: ERP's current one (kept: see rule 1).
    code_drift: list[dict] = field(default_factory=list)
    #: ``{"code", "from", "to"}`` for every row whose ``erp_account_id`` this run
    #: changed (``to`` None = unlinked) — where its bills posted before and
    #: where they post now — plus ``from_realm`` / ``to_realm`` when a link kept
    #: its id but moved to another ERP company. The audit row records it in full.
    link_changes: list[dict] = field(default_factory=list)
    #: The connection realm the pull came from (``services/erp_link_realm``).
    realm: str | None = None
    #: Links made before realms were recorded that this pull confirmed (the
    #: same id still holds the row's code) and stamped with ``realm``.
    realm_adopted: int = 0
    #: Codes of rows marked inactive because the ERP made their account inactive.
    deactivated: list[str] = field(default_factory=list)

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
            "realm": self.realm,
            "realm_adopted": self.realm_adopted,
            "deactivated_count": len(self.deactivated),
            "deactivated": listed(self.deactivated),
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
    realm: str | None = None,
) -> ChartSyncResult:
    """Apply one pull of the ERP's chart, made under connection ``realm``
    (``erp_link_realm.connection_realm``). Matching and creating use the chart
    being synced INTO — shared ∪ ``entity_id``'s own (every row, consolidated)
    — and new rows land in ``entity_id``'s chart (shared when None); the repair
    passes (rules 0, 3, 4) cover the whole organization. See the module
    docstring."""
    result = ChartSyncResult(realm=realm)

    # -- the pull, reduced to one candidate per code ------------------------
    by_code: dict[str, list[GLAccountPayload]] = defaultdict(list)
    codes_by_id: dict[str, set[str]] = defaultdict(set)
    too_long: dict[str, list[GLAccountPayload]] = defaultdict(list)
    #: Every account in the pull that has an id, whatever else is wrong with it.
    by_id: dict[str, GLAccountPayload] = {}
    missing_id: set[str] = set()
    for acct in accounts:
        code = (acct.code or "").strip()
        if not code:
            continue
        if not acct.erp_account_id:
            # Nothing could post to it, and two id-less accounts would collapse
            # into one in the dedupe below. Reported; never linked or created.
            missing_id.add(code)
            continue
        by_id.setdefault(acct.erp_account_id, acct)
        if len(code) > GL_CODE_MAX_LENGTH:
            too_long[code].append(acct)
            continue
        codes_by_id[acct.erp_account_id].add(code)
        # The same account listed twice (a provider paging overlap) is one.
        if not any(seen.erp_account_id == acct.erp_account_id for seen in by_code[code]):
            by_code[code].append(acct)

    refused: dict[str, str] = {}  # code -> reason
    for code, group in by_code.items():
        if len(group) > 1:
            refused[code] = DUPLICATE_CODE
    for codes in codes_by_id.values():
        if len(codes) > 1:
            for code in codes:
                refused.setdefault(code, DUPLICATE_ERP_ID)
    candidates = {code: group[0] for code, group in by_code.items() if code not in refused}
    # Every id in the pull — refused, over-long and inactive ones included: a
    # row linked to any of them points at an account that still exists, so its
    # link is not "dead" and must not be re-pointed at whatever account holds
    # its code.
    live_ids = set(by_id)
    code_of_id = {a.erp_account_id: code for code, a in candidates.items()}

    # The ERP accounts each LEGACY code (an older adapter's key) named. Only
    # legacy codes: a row keyed by one that two accounts share may have merged
    # them (rule 4). A current code another account holds is rule 2's business,
    # not a sign of a merge — a row renamed in the ERP keeps its link.
    legacy_claims: dict[str, set[str]] = defaultdict(set)
    for group in by_code.values():
        for acct in group:
            for legacy in acct.legacy_codes:
                if legacy:
                    legacy_claims[legacy].add(acct.erp_account_id)

    # -- the organization's whole chart, read once --------------------------
    all_rows = (
        (
            await db.execute(
                select(GLAccount)
                .where(GLAccount.organization_id == organization_id)
                .order_by(*_preference(entity_id))
            )
        )
        .scalars()
        .all()
    )
    rows = [
        row
        for row in all_rows
        if entity_id is None or row.entity_id is None or row.entity_id == entity_id
    ]

    def candidates_for(reason: str, erp_ids: set[str]) -> dict | None:
        listed = [
            {"erp_account_id": i, "code": (by_id[i].code or "").strip(), "name": by_id[i].name}
            for i in sorted(erp_ids)
            if i in by_id
        ]
        return {"realm": realm, "reason": reason, "accounts": listed} if listed else None

    def unlink(row: GLAccount, reason: str, erp_ids: set[str]) -> None:
        if row.erp_account_id is not None:
            result.link_changes.append({"code": row.code, "from": row.erp_account_id, "to": None})
            row.erp_account_id = None
            row.erp_link_realm = None
            _clear_confirmation(row)
            result.unlinked.append(row.code)
        row.erp_link_candidates = candidates_for(reason, erp_ids)

    def legacy_ambiguous(row: GLAccount, erp_id: str | None) -> bool:
        """Rule 4: did an older adapter key some OTHER account by ``row``'s code?"""
        return bool(legacy_claims.get(row.code, set()) - {erp_id})

    def confirmed(row: GLAccount) -> bool:
        """A person chose this row's account, and that account still exists."""
        return row.erp_link_confirmed_at is not None and row.erp_account_id in live_ids

    # -- rule 0: a link made under another connection is not trusted --------
    # Kept only when this pull shows the SAME id holding the row's own code
    # (its current one, or the legacy key an older adapter gave it): then the
    # id names the account the row's invoices meant in this company too.
    # Anything else is dead — dropped here, re-matched by code below, and
    # every change recorded in `link_changes` (the audit row lists them all).
    dead: dict[uuid.UUID, str] = {}
    for row in all_rows:
        if row.erp_account_id is None or row.erp_link_realm == realm:
            continue
        acct = by_id.get(row.erp_account_id)
        if acct is not None and _same_account(row.code, acct):
            if row.erp_link_realm is None:
                result.realm_adopted += 1
            else:
                result.link_changes.append(
                    {
                        "code": row.code,
                        "from": row.erp_account_id,
                        "to": row.erp_account_id,
                        "from_realm": row.erp_link_realm,
                        "to_realm": realm,
                    }
                )
                # A confirmation chose an account of the OTHER company.
                _clear_confirmation(row)
            row.erp_link_realm = realm
            continue
        dead[row.id] = row.erp_account_id
        row.erp_account_id = None
        row.erp_link_realm = None
        _clear_confirmation(row)

    # -- rules 3 and 4, over the whole organization -------------------------
    # A row a person confirmed is the answer to exactly the question these
    # rules ask, so neither re-judges it.
    for row in all_rows:
        linked_to = row.erp_account_id
        if linked_to is None or confirmed(row):
            continue
        if row.code in refused:
            # Rule 3: two ERP accounts answer to the code this row shows.
            unlink(row, refused[row.code], {a.erp_account_id for a in by_code[row.code]})
        elif (
            linked_to in code_of_id
            and row.code != code_of_id[linked_to]
            and legacy_ambiguous(row, linked_to)
        ):
            # Rule 4: a pre-§284 row that merged this account with another.
            # Its invoices are refused and recoded, or a person confirms which
            # account the row meant; nothing posts on a guess. The ERP account
            # gets its own row under its real code below.
            unlink(row, AMBIGUOUS_LEGACY, legacy_claims[row.code] | {linked_to})

    row_by_id: dict[str, GLAccount] = {}
    row_by_code: dict[str, GLAccount] = {}
    for row in rows:  # first one wins: the order IS the preference...
        erp_id = row.erp_account_id
        if erp_id:
            held = row_by_id.get(erp_id)
            # ...except that a confirmed row may share its account with the row
            # holding that account's own code, and rule 1 updates the latter.
            if held is None or (held.code != code_of_id.get(erp_id) == row.code):
                row_by_id[erp_id] = row
        row_by_code.setdefault(row.code, row)

    claimed_rows: set[uuid.UUID] = set()
    relinked: set[uuid.UUID] = set()

    for code in sorted(candidates):
        acct = candidates[code]
        erp_id = acct.erp_account_id

        # -- rule 1: by the ERP's id --------------------------------------
        row = row_by_id.get(erp_id)
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
                # it to this one would be a guess. A person decides
                # (`erp_link_candidates`, POST /api/gl-accounts/{id}/erp-link).
                unlink(row, AMBIGUOUS_LEGACY, legacy_claims[code] | {erp_id})
                refused.setdefault(code, AMBIGUOUS_LEGACY)
                continue
            claimed_rows.add(row.id)
            if row.erp_account_id != erp_id:
                before = dead.get(row.id, row.erp_account_id)
                result.link_changes.append({"code": code, "from": before, "to": erp_id})
                _clear_confirmation(row)
                row.erp_account_id = erp_id
                row.erp_link_realm = realm
                row.erp_link_candidates = None
                relinked.add(row.id)
                result.linked += 1
                _apply(row, acct, result, count=False)
            else:
                _apply(row, acct, result)
            continue

        # -- new -----------------------------------------------------------
        if not acct.active:
            # Nothing should be newly coded to an account the ERP retired.
            continue
        db.add(
            GLAccount(
                code=code,
                name=acct.name,
                account_type=acct.account_type,
                parent_code=acct.parent_code,
                organization_id=organization_id,
                entity_id=entity_id,
                erp_account_id=erp_id,
                erp_link_realm=realm,
            )
        )
        result.created += 1

    # A link from another connection that nothing re-matched: record where its
    # bills used to post. The push already refused it (its realm is not the
    # current one); the row now says so too.
    for row in all_rows:
        if row.id in dead and row.id not in relinked:
            result.link_changes.append({"code": row.code, "from": dead[row.id], "to": None})
            result.unlinked.append(row.code)

    for code, group in too_long.items():
        refused.setdefault(code, CODE_TOO_LONG)
        by_code.setdefault(code, []).extend(group)
    for code in missing_id:
        refused.setdefault(code, MISSING_ERP_ID)

    # "Needs a decision": an unlinked row whose code two ERP accounts share
    # keeps them as candidates too. Then every pending decision is refreshed
    # against this pull — a candidate the ERP no longer has is dropped, and a
    # row that got linked, or whose candidates came from another connection,
    # stops asking.
    for code, reason in refused.items():
        row = row_by_code.get(code)
        if reason == DUPLICATE_CODE and row is not None and row.erp_account_id is None:
            row.erp_link_candidates = candidates_for(
                reason, {a.erp_account_id for a in by_code[code]}
            )
    for row in all_rows:
        pending = row.erp_link_candidates
        if not pending:
            continue
        if row.erp_account_id is not None or pending.get("realm") != realm:
            row.erp_link_candidates = None
            continue
        ids = {a.get("erp_account_id") for a in pending.get("accounts") or []}
        row.erp_link_candidates = candidates_for(pending.get("reason") or "", ids & live_ids)

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
    result.deactivated.sort()
    return result


def _same_account(code: str, acct: GLAccountPayload) -> bool:
    """Does ``acct`` answer to ``code`` — its current code, or a key an older
    adapter gave it? The evidence a link from another realm must show."""
    return code == (acct.code or "").strip() or code in acct.legacy_codes


def _clear_confirmation(row: GLAccount) -> None:
    row.erp_link_confirmed_at = None
    row.erp_link_confirmed_by = None


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
    if not acct.active and row.is_active:
        # Retired in the ERP, so retired here: nothing new is coded to it
        # (gl_chart §199). A sync never reactivates a row — a retirement in our
        # chart may be our own decision, so reactivating is a person's call.
        row.is_active = False
        result.deactivated.append(row.code)
        changed = True
    if not count:
        return
    if changed:
        result.updated += 1
    else:
        result.unchanged += 1
