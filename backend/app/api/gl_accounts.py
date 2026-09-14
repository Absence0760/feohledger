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
from app.schemas.gl_account import GLAccountCreate
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
    has to be able to look one up. Only the two writes on this router
    (`POST ""` and `POST /sync-erp`) are admin / ap_manager.

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

    return [
        {
            "id": str(a.id),
            "code": a.code,
            "name": a.name,
            "account_type": a.account_type,
            "parent_code": a.parent_code,
            "is_active": a.is_active,
            "erp_account_id": a.erp_account_id,
            # Which chart the row belongs to: NULL = SHARED across every
            # entity, otherwise the entity that owns it. Unlike every other
            # business table this is not an incidental scoping column, it is
            # the row's meaning (`models/gl_account`), and without it the two
            # views this endpoint serves are both ambiguous: in the
            # CONSOLIDATED view the response is every entity's chart at once,
            # where two subsidiaries legitimately hold their own "6000" and
            # the rows are otherwise indistinguishable; with an entity
            # selected it is `shared ∪ that entity's own`, where a shared row
            # and an entity's override of it read identically while deciding
            # whether an edit reaches one subsidiary or all of them. Additive:
            # the picker callers read `id` / `code` / `name` and ignore it.
            "entity_id": str(a.entity_id) if a.entity_id else None,
        }
        for a in accounts
    ]


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
