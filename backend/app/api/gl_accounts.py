"""GL Account (Chart of Accounts) endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    get_current_user,
    get_org_id,
    require_roles,
)
from app.models.gl_account import GLAccount
from app.models.organization import Organization
from app.models.user import User
from app.schemas.gl_account import GLAccountCreate, GLAccountUpdate
from app.services.audit_dispatch import dispatch_audit
from app.tenant import apply_entity_scope, get_entity_id, get_tenant, get_tenant_db
from app.utils.search import ilike_contains

router = APIRouter(prefix="/gl-accounts", tags=["gl-accounts"])


def _code_in_effective_chart(code: str, org_id: uuid.UUID, entity_id: uuid.UUID | None):
    """Is ``code`` already visible in the chart this caller is looking at?

    The guard on create, and deliberately BROADER than the two partial unique
    indexes migration ``0088`` installs. Those enforce what the data layer can:
    one row per code in the shared chart, and one per code in each entity's own
    chart. This asks the question the user actually cares about — would a second
    row with this code appear in `GET /api/gl-accounts` for whoever creates it —
    which is the *effective* chart, ``shared ∪ the selected entity`` (the same
    union `list_gl_accounts`, the AI extraction catalog and
    ``gl_recode._ActiveChart`` all read). Two rows answering to one code there
    is unresolvable: nothing implements override semantics, and an invoice
    records the code as a STRING, so which account it was coded to becomes
    unanswerable.

    Two entities may still each hold their own ``6000`` — neither is in the
    other's effective chart, and separate subsidiaries running the same standard
    code is normal. In the consolidated view the union is the whole tenant, so
    creating a SHARED code that some entity already defines is refused: a shared
    row lands in every entity's chart, including that one's.
    """
    return apply_entity_scope(
        select(GLAccount.id).where(
            GLAccount.code == code,
            GLAccount.organization_id == org_id,
        ),
        GLAccount,
        entity_id,
        include_shared=True,
    )


def _serialize(a: GLAccount) -> dict:
    """One chart row, as every consumer of this router reads it.

    Shared by the list and the PATCH so a corrected account comes back in
    exactly the shape the picker that is about to re-render it expects.
    """
    return {
        "id": str(a.id),
        "code": a.code,
        "name": a.name,
        "account_type": a.account_type,
        "parent_code": a.parent_code,
        "is_active": a.is_active,
        "erp_account_id": a.erp_account_id,
        # Which chart the row belongs to: NULL = SHARED across every entity,
        # otherwise the entity that owns it. Unlike every other business table
        # this is not an incidental scoping column, it is the row's meaning
        # (`models/gl_account`), and without it the two views the list serves
        # are both ambiguous: in the CONSOLIDATED view the response is every
        # entity's chart at once, where two subsidiaries legitimately hold
        # their own "6000" and the rows are otherwise indistinguishable; with
        # an entity selected it is `shared ∪ that entity's own`, where a shared
        # row and an entity's override of it read identically while deciding
        # whether an edit reaches one subsidiary or all of them. Additive: the
        # picker callers read `id` / `code` / `name` and ignore it.
        "entity_id": str(a.entity_id) if a.entity_id else None,
    }


def _duplicate_detail(code: str, entity_id: uuid.UUID | None) -> str:
    """409 body. Names the code (org configuration, not PII) and the chart."""
    where = "the selected entity's chart" if entity_id else "this tenant's chart of accounts"
    return f"GL account code '{code}' already exists in {where}."


def _sync_match_query(code: str, org_id: uuid.UUID, entity_id: uuid.UUID | None):
    """Find the account an ERP code should update, in the chart being synced.

    Candidates are ``shared (NULL) ∪ the selected entity`` — the invoice-side
    rule ``services/gl_recode._ActiveChart.is_valid_for`` already applies, so
    the sync resolves a code exactly the way validation does. Matching on
    ``(code, organization_id)`` alone (the pre-fix behaviour) meant a sync run
    while subsidiary B was selected UPDATED subsidiary A's row instead of
    creating B's, contradicting this route's own "same rule as manual create".

    Ordering encodes which row wins when a code exists in both scopes:

    * an entity is selected → **its own** row outranks the shared one (an
      entity-specific account overrides the shared chart for that entity);
    * consolidated → the **shared** row wins, because that is the chart a
      consolidated sync creates into. Ties break oldest-first so the pick is
      deterministic rather than whatever Postgres returns first.
    """
    is_shared = GLAccount.entity_id.is_(None)
    query = apply_entity_scope(
        select(GLAccount).where(
            GLAccount.code == code,
            GLAccount.organization_id == org_id,
        ),
        GLAccount,
        entity_id,
        include_shared=True,
    )
    preference = is_shared.desc() if entity_id is None else is_shared.asc()
    return query.order_by(preference, GLAccount.created_at.asc(), GLAccount.id.asc())


@router.get("")
async def list_gl_accounts(
    search: str | None = None,
    account_type: str | None = None,
    active_only: bool = True,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """The chart of accounts, whole, as a bare array — deliberately unpaginated.

    Auth-gated but role-open, like the `/purchase-orders` and `/goods-receipts`
    reads: every role codes or reads GL codes, and a clerk entering an invoice
    has to be able to look one up. Only the three writes on this router
    (`POST ""`, `PATCH /{id}` and `POST /sync-erp`) are admin / ap_manager.

    It is a bounded reference collection and both of its consumers need every
    row, so it does not take the canonical `{items, total, page, page_size}`
    envelope: the invoice / expense / requisition / catalog GL pickers would
    otherwise be unable to offer a code past the first page — which is a coding
    defect, not a paging nicety — and `/gl-accounts` (the list page) states the
    count it actually holds rather than a server total it only partly fetched.
    `tests/test_pagination.py::test_gl_accounts_stays_unpaginated` pins that.
    Filtering is therefore server-side here (`search` / `account_type` /
    `active_only`) and never re-done in the browser.
    """
    # A scoped chart is the shared accounts (NULL entity_id) ∪ the entity's own
    # (include_shared=True); the consolidated view (None) returns everything.
    query = apply_entity_scope(select(GLAccount), GLAccount, entity_id, include_shared=True)
    if active_only:
        query = query.where(GLAccount.is_active)
    if search:
        query = query.where(
            ilike_contains(GLAccount.code, search) | ilike_contains(GLAccount.name, search)
        )
    if account_type:
        query = query.where(GLAccount.account_type == account_type)

    query = query.order_by(GLAccount.code)
    result = await db.execute(query)
    accounts = result.scalars().all()

    return [_serialize(a) for a in accounts]


@router.post("", status_code=201)
async def create_gl_account(
    body: GLAccountCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    # Unlike other tables, a NULL entity_id is meaningful for GL: it makes the
    # account SHARED across every entity. So we deliberately use get_entity_id
    # (not get_write_entity_id) — creating an account while an entity is
    # selected makes it entity-specific; creating it in the consolidated view
    # leaves it shared (NULL). See docs/multi-entity.md § Chart of accounts.
    #
    # A GL code must resolve to exactly one account in the chart it lands in.
    # Checked here for a clean 409; the partial unique indexes (migration 0088)
    # are the real guard, so a concurrent create can't slip past this read.
    existing = (await db.execute(_code_in_effective_chart(body.code, org_id, entity_id))).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=_duplicate_detail(body.code, entity_id))

    account = GLAccount(
        code=body.code,
        name=body.name,
        account_type=body.account_type,
        parent_code=body.parent_code,
        organization_id=org_id,
        entity_id=entity_id,
    )
    db.add(account)
    try:
        await db.flush()
    except IntegrityError as exc:
        # The read above lost a race with a concurrent create. The index is the
        # real guard; this turns its error into the same 409.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=_duplicate_detail(body.code, entity_id)
        ) from exc

    # A GL account is what invoice lines are coded to, so adding one is a
    # chart-of-accounts change that belongs on the append-only trail. PII-free:
    # code / name / type are org config. `entity_id` records whether the
    # account landed shared (NULL) or entity-specific.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="gl_account.created",
        entity_type="gl_account",
        entity_id=account.id,
        details={
            "code": account.code,
            "name": account.name,
            "account_type": account.account_type,
            "entity_id": str(entity_id) if entity_id else None,
        },
    )
    return {
        "id": str(account.id),
        "code": account.code,
        "name": account.name,
    }


async def _parent_chain_would_cycle(
    db: AsyncSession,
    account: GLAccount,
    parent_code: str,
    org_id: uuid.UUID,
) -> bool:
    """Would setting ``account.parent_code = parent_code`` close a loop?

    Cycles were UNREACHABLE before this router had a PATCH: ``parent_code`` was
    fixed at create time, and a row can only point at an account that already
    existed, so the graph was acyclic by construction. Editing the field is
    what makes ``A → B → A`` expressible, which is why the check lives here and
    not on create.

    It matters because the parent chain is what a hierarchical chart-of-accounts
    rollup walks. A loop is not a wrong number, it is a non-terminating walk.

    Resolution happens within the chart the account itself lives in, because
    that is the set in which its ``parent_code`` string is looked up (the same
    effective-chart rule ``_code_in_effective_chart`` and
    ``gl_recode._ActiveChart`` use) — ``shared ∪ its own entity`` for an
    entity-owned row, and the **shared chart alone** for a shared one.
    """
    query = select(GLAccount.code, GLAccount.parent_code).where(GLAccount.organization_id == org_id)
    if account.entity_id is None:
        # A SHARED account is visible to every entity, so its parent must be too
        # — resolve against the shared chart alone. `apply_entity_scope(None)` is
        # the CONSOLIDATED view (every entity's chart at once), which is wrong
        # here: two subsidiaries may each hold their own "6000", and collapsing
        # them into one `code → parent` mapping would let an arbitrary entity's
        # row decide whether a shared account's parent chain loops.
        query = query.where(GLAccount.entity_id.is_(None))
    else:
        # Shared first, so that when a code exists in BOTH scopes the entity's
        # own row overwrites it in the mapping below — the same override
        # precedence `_sync_match_query` applies.
        query = apply_entity_scope(
            query, GLAccount, account.entity_id, include_shared=True
        ).order_by(GLAccount.entity_id.is_(None).desc())

    rows = (await db.execute(query)).all()
    parent_of = {code: parent for code, parent in rows}
    # Walk up from the PROPOSED parent. Reaching this account's own code means
    # the edit closes a loop. Bounded by the chart size, so a pre-existing cycle
    # in imported data cannot hang the request.
    seen: set[str] = set()
    cursor: str | None = parent_code
    for _ in range(len(parent_of) + 1):
        if cursor is None or cursor in seen:
            return False
        if cursor == account.code:
            return True
        seen.add(cursor)
        cursor = parent_of.get(cursor)
    return False


@router.patch("/{account_id}")
async def update_gl_account(
    account_id: uuid.UUID,
    body: GLAccountUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Correct or retire one GL account.

    **Retire, never delete — and there is deliberately no DELETE on this
    router.** A GL account cannot be hard-deleted without orphaning the money
    trail, in two independent ways:

    * ``Invoice.gl_account`` and ``InvoiceLineItem.gl_account`` record the GL as
      a ``String(100)`` *code*, not a foreign key. Deleting the row removes the
      account from the chart while every posted line keeps the string, so "which
      account was this invoice coded to?" becomes unanswerable — and nothing
      raises, because there is no constraint to violate.
    * ``Expense``, ``RequisitionLine`` and ``CatalogItem`` DO hold a real
      ``ForeignKey("gl_accounts.id")``, with no ``ON DELETE`` clause — so
      Postgres defaults to ``NO ACTION`` and a delete against a referenced
      account raises a ``ForeignKeyViolation``, i.e. a 500 rather than a clean
      refusal.

    So retirement is ``is_active = false``. The column already existed and the
    list endpoint already filters on it (``active_only``, default true) — until
    now nothing under ``app/`` ever wrote it, so an inactive row was reachable
    only by direct SQL or an imported chart. A retired account disappears from
    every picker and from ``gl_recode._ActiveChart``, so nothing new can be
    coded to it, while every historical line still resolves.

    **Which rows an editor may touch follows the create rule, not the read
    rule.** The read is ``shared ∪ the selected entity``, but a shared row
    (``entity_id IS NULL``) belongs to *every* entity: retiring one from inside
    subsidiary B's context would silently pull the account out of subsidiary A's
    chart too. So with an entity selected only that entity's OWN rows are
    editable, and a shared row must be edited from the consolidated view — the
    same view that created it (``get_entity_id``, not ``get_write_entity_id``).
    The consolidated view may edit any row in the tenant, so a typo in a
    subsidiary's chart is fixable without switching entity first.

    **Gate: admin | ap_manager**, matching ``POST ""`` and ``POST /sync-erp`` on
    this router. It is not a `require_permission` case: the granular SoD catalog
    covers duties that can be *split* to stop one person completing a fraud
    (approve vs. execute a payment, request vs. approve a bank change), and the
    chart of accounts is not one of those — it cannot move money. It is also
    strictly less reach than the sync an ap_manager already has, which rewrites
    ``name`` and ``account_type`` across the whole chart in one call; gating the
    single-row edit more tightly than the bulk one would be incoherent.
    """
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=422, detail="No fields to update.")

    account = (
        await db.execute(
            select(GLAccount).where(
                GLAccount.id == account_id,
                GLAccount.organization_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="GL account not found")

    if entity_id is not None and account.entity_id != entity_id:
        # Visible in this chart (shared rows are), but not this entity's to
        # change. 403 rather than 404: the caller can already see the row in
        # `GET ""`, so hiding it would only be confusing, and the message names
        # the fix.
        raise HTTPException(
            status_code=403,
            detail=(
                "This account belongs to the shared chart or another entity. "
                "Switch to the consolidated view to edit it."
            ),
        )

    changed: dict[str, object] = {}

    if "name" in data and data["name"] is not None:
        new_name = data["name"].strip()
        if not new_name:
            raise HTTPException(status_code=422, detail="Name cannot be blank.")
        if new_name != account.name:
            changed["name"] = new_name
            account.name = new_name

    if "account_type" in data and data["account_type"] != account.account_type:
        changed["account_type"] = data["account_type"]
        account.account_type = data["account_type"]

    if "parent_code" in data and data["parent_code"] != account.parent_code:
        new_parent = data["parent_code"]
        if new_parent is not None:
            if new_parent == account.code:
                raise HTTPException(status_code=422, detail="An account cannot be its own parent.")
            if await _parent_chain_would_cycle(db, account, new_parent, org_id):
                raise HTTPException(
                    status_code=422,
                    detail=f"'{new_parent}' is below this account in the chart — that would "
                    "make the parent chain a loop.",
                )
        changed["parent_code"] = new_parent
        account.parent_code = new_parent

    if "is_active" in data and data["is_active"] is not None:
        if data["is_active"] != account.is_active:
            changed["is_active"] = data["is_active"]
            account.is_active = data["is_active"]

    await db.flush()

    if changed:
        # The chart of accounts is what invoice lines are coded to, so an edit
        # to it belongs on the append-only trail — and retiring an account
        # changes what every picker and `_ActiveChart` will accept from here on,
        # which is a status change on a regulated record. PII-free: code, name,
        # type and active flag are org configuration.
        #
        # The action name distinguishes a retirement from a correction so an
        # auditor can find every retirement without parsing `changed`; the full
        # before-less `changed` map rides along either way.
        if changed.get("is_active") is False:
            action = "gl_account.deactivated"
        elif changed.get("is_active") is True:
            action = "gl_account.reactivated"
        else:
            action = "gl_account.updated"
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action=action,
            entity_type="gl_account",
            entity_id=account.id,
            details={
                "code": account.code,
                "changed": changed,
                "entity_id": str(account.entity_id) if account.entity_id else None,
            },
        )

    return _serialize(account)


@router.post("/sync-erp")
async def sync_gl_accounts_from_erp(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Pull chart of accounts from the connected ERP via its adapter.

    New accounts land shared (NULL entity_id) in the consolidated view, or
    entity-specific when an entity is selected — same rule as manual create.

    An ERP code is matched against the chart it is being synced INTO (shared ∪
    the selected entity, via ``_sync_match_query``), not against every account
    in the tenant: a sync run under subsidiary B used to update subsidiary A's
    row rather than create B's, which is the opposite of the rule above.
    """
    erp_config = (org.settings or {}).get("erp")
    if not erp_config:
        raise HTTPException(status_code=400, detail="No ERP configured")

    # Lazy-import adapter modules so the @register_adapter decorator
    # populates the dispatcher registry. Same pattern as vendors.py
    # and purchase_orders.py.
    import app.services.erp_adapters.dynamics_365_bc  # noqa: F401
    import app.services.erp_adapters.merge_dev  # noqa: F401
    import app.services.erp_adapters.mock_adapter  # noqa: F401
    import app.services.erp_adapters.netsuite  # noqa: F401
    from app.services.erp_adapters import UnknownErpAdapterError, get_erp_adapter

    try:
        adapter = get_erp_adapter(erp_config)
    except UnknownErpAdapterError as exc:
        # A config problem, not a gateway failure — 400, not 502. Before the
        # dispatcher failed closed this resolved to `mock` and returned its
        # fixture chart of accounts as if it came from the ERP.
        raise HTTPException(
            status_code=400,
            detail=f"'{exc.adapter_key}' is not a supported ERP adapter.",
        ) from exc

    try:
        erp_accounts = await adapter.list_gl_accounts()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ERP request failed: {type(exc).__name__}",
        ) from exc

    created = 0
    updated = 0
    # Accounts created earlier in THIS run, keyed by code. They are not visible
    # to the match query until flush, so without this an ERP catalogue that
    # lists a code twice would insert two rows and trip the unique index at
    # commit — a 500 after the audit row was already written.
    pending: dict[str, GLAccount] = {}
    for acct in erp_accounts:
        existing = pending.get(acct.code)
        if existing is None:
            existing = (
                (await db.execute(_sync_match_query(acct.code, org_id, entity_id)))
                .scalars()
                .first()
            )

        if existing:
            # Only count as updated when something actually changes —
            # otherwise re-running the sync would inflate the metric.
            changed = False
            if existing.name != acct.name:
                existing.name = acct.name
                changed = True
            if acct.account_type and existing.account_type != acct.account_type:
                existing.account_type = acct.account_type
                changed = True
            if acct.erp_account_id and existing.erp_account_id != acct.erp_account_id:
                existing.erp_account_id = acct.erp_account_id
                changed = True
            if changed:
                updated += 1
        else:
            account = GLAccount(
                code=acct.code,
                name=acct.name,
                account_type=acct.account_type,
                parent_code=acct.parent_code,
                organization_id=org_id,
                entity_id=entity_id,
                erp_account_id=acct.erp_account_id or acct.code,
            )
            db.add(account)
            pending[acct.code] = account
            created += 1

    # One PII-free summary row per sync, not one per account — the trail records
    # that a bulk chart change happened, who ran it and how much it moved.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="gl_account.synced_from_erp",
        entity_type="gl_account",
        entity_id=org_id,
        details={
            "created": created,
            "updated": updated,
            "adapter": adapter.erp_type,
            "entity_id": str(entity_id) if entity_id else None,
        },
    )
    await db.commit()
    return {
        "success": True,
        "message": f"Synced {created} new, {updated} updated GL accounts",
        "created": created,
        "updated": updated,
        "adapter": adapter.erp_type,
    }
