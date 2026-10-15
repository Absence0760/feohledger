"""Commerce / inventory platform connection and per-receipt sync status (decisions §273).

``settings.commerce`` is CONFIGURATION, with one writer: this router
(``PATCH /api/organization`` refuses the key). Its secrets are sealed in
``provider_credentials`` (block ``commerce``, decisions §266) and written only
through ``PUT /api/organization/credentials/commerce``; a configuration save
here refuses a non-blank secret, and one that changes the platform or its
destination drops the sealed secrets through the same audited writer (the ERP
rule, §271). Every write is admin-only and audited with key names only.

Sync status reads follow the receipt they belong to: any signed-in user who can
see the receipt (entity-scoped). A retry re-sends what the receipt already
authorised under the same idempotency key, so it is open to the roles that may
record a receipt.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, get_current_user, get_org_id, require_roles
from app.api.goods_receipts import RECEIPT_ENTRY_ROLES
from app.api.refusals import coded_refusal
from app.database import get_control_db
from app.models.commerce import CommerceInventorySync, CommerceSkuMapping
from app.models.entity import Entity
from app.models.organization import Organization
from app.models.procurement import GoodsReceipt
from app.models.user import User
from app.schemas.commerce import (
    CommerceLocationsUpdate,
    CommerceSettingsUpdate,
    CommerceSkuMappingUpsert,
)
from app.services import commerce_sync
from app.services.audit_dispatch import dispatch_audit, record_auth_audit_or_raise
from app.services.commerce_adapters import (
    CommerceError,
    UnknownCommerceAdapterError,
    get_commerce_adapter,
)
from app.services.commerce_adapters import catalog as commerce_catalog
from app.services.commerce_adapters.registry import adapter_class
from app.services.credential_crypto import CredentialCryptoError
from app.services.provider_credentials import (
    CREDENTIALS_UNAVAILABLE_DETAIL,
    config_for_connection_test,
    credential_status,
    extract_secrets,
    provider_config,
    update_secrets,
)
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    lock_organization,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/commerce", tags=["commerce"])

SETTINGS_KEY = commerce_sync.SETTINGS_KEY

# Refusal codes — the web client localizes on these (`api/codedRefusals.ts`).
COMMERCE_UNKNOWN_PROVIDER = "commerce_unknown_provider"
COMMERCE_NOT_CONFIGURED = "commerce_not_configured"
COMMERCE_OAUTH_NOT_AVAILABLE = "commerce_oauth_not_available"
COMMERCE_UNKNOWN_ENTITY = "commerce_unknown_entity"
COMMERCE_SECRET_IN_SETTINGS = "commerce_secret_in_settings"

_AUDIT_FAILED = "The change could not be recorded in the audit trail, so it was not saved."
_STORE_UNAVAILABLE = "The credential store is unavailable, so nothing was saved."


def _stored(org: Organization) -> dict | None:
    block = (org.settings or {}).get(SETTINGS_KEY)
    return block if isinstance(block, dict) else None


async def _view(db: AsyncSession, org: Organization, block: dict | None) -> dict[str, Any]:
    return {
        "configured": commerce_sync.commerce_config({SETTINGS_KEY: block}) is not None,
        "settings": commerce_catalog.public_commerce_config(block) if block else None,
        # Which secret fields are sealed — names only (`provider_credentials`).
        "secrets_set": (await credential_status(db, org.id)).get(SETTINGS_KEY, []),
    }


async def _write_block(
    db: AsyncSession,
    org: Organization,
    user: User,
    new_block: dict | None,
    *,
    action: str,
    extra: dict[str, Any] | None = None,
    drop_secrets: frozenset[str] = frozenset(),
    drop_reason: str | None = None,
) -> dict | None:
    """Replace ``settings.commerce`` under the org row lock, audited before commit.

    A change that cannot be audited is not made (the ``PUT /organization/sso``
    shape, decisions §239). ``drop_secrets`` names sealed secrets the save
    invalidates; the stored ones among them are cleared through the credential
    store's writer, audited the way the credentials endpoint audits.
    """
    org = await lock_organization(db, org)
    before = _stored(org)
    changed = commerce_catalog.changed_keys(before, new_block or {})
    settings_blob = dict(org.settings or {})
    if new_block:
        settings_blob[SETTINGS_KEY] = new_block
    else:
        settings_blob.pop(SETTINGS_KEY, None)
    org.settings = settings_blob
    try:
        await record_auth_audit_or_raise(
            organization_id=org.id,
            actor_id=user.id,
            action=action,
            entity_type="organization",
            entity_id=org.id,
            details={
                "changed": changed,
                "provider": str((new_block or {}).get("provider") or "")[:50] or None,
                **(extra or {}),
            },
        )
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=503, detail=_AUDIT_FAILED) from None

    stored_names = set((await credential_status(db, org.id)).get(SETTINGS_KEY, []))
    to_clear = sorted(stored_names & drop_secrets)
    if to_clear:

        async def _audit_drop(cleared: list[str]) -> None:
            try:
                await record_auth_audit_or_raise(
                    organization_id=org.id,
                    actor_id=user.id,
                    action="organization.credentials_updated",
                    entity_type="organization",
                    entity_id=org.id,
                    details={
                        "block": SETTINGS_KEY,
                        "changed": cleared,
                        "cleared": cleared,
                        "reason": drop_reason,
                    },
                )
            except Exception:
                raise HTTPException(status_code=503, detail=_AUDIT_FAILED) from None

        try:
            await update_secrets(db, org.id, SETTINGS_KEY, {}, to_clear, before_write=_audit_drop)
        except CredentialCryptoError:
            await db.rollback()
            raise HTTPException(status_code=503, detail=_STORE_UNAVAILABLE) from None
        except HTTPException:
            await db.rollback()
            raise
    await db.commit()
    return new_block


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


@router.get("/providers")
async def list_providers(user: User = Depends(require_roles(ROLE_ADMIN))):
    """The setup form's catalogue — one entry per registered provider."""
    return {"providers": commerce_catalog.all_providers()}


@router.get("/settings")
async def get_settings(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """``settings.commerce`` (configuration only) and which secrets are sealed."""
    return await _view(db, org, _stored(org))


@router.put("/settings")
async def put_settings(
    body: CommerceSettingsUpdate,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Save the connection's configuration.

    A non-blank secret is refused, naming the fields (never the values): secrets
    go to ``PUT /api/organization/credentials/commerce``. A save naming another
    platform, or the same one at a new destination, drops the sealed secrets.
    """
    incoming = dict(body.settings)
    try:
        adapter_class(incoming.get("provider"))
    except UnknownCommerceAdapterError as exc:
        raise HTTPException(
            status_code=422,
            detail=coded_refusal(
                COMMERCE_UNKNOWN_PROVIDER,
                f"'{exc.provider}' is not a supported commerce platform.",
                provider=exc.provider,
            ),
        ) from None
    sent = sorted(extract_secrets(SETTINGS_KEY, incoming))
    if sent:
        raise HTTPException(
            status_code=422,
            detail=coded_refusal(
                COMMERCE_SECRET_IN_SETTINGS,
                f"commerce credentials ({', '.join(sent)}) are managed by "
                "PUT /api/organization/credentials/commerce.",
                fields=", ".join(sent),
            ),
        )
    stored = _stored(org)
    merged = commerce_catalog.merge_commerce_update(stored, incoming)
    block = await _write_block(
        db,
        org,
        user,
        merged,
        action="organization.commerce_updated",
        drop_secrets=commerce_catalog.secrets_to_drop(stored, merged),
        drop_reason="commerce_destination_changed",
    )
    return await _view(db, org, block)


@router.delete("/settings", status_code=204)
async def delete_settings(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Disconnect: drop the block (configuration and location mapping) and its
    sealed secrets. Receipts recorded afterwards plan no push; already-planned
    rows fail ``not_configured``."""
    stored_names = (await credential_status(db, org.id)).get(SETTINGS_KEY, [])
    if _stored(org) is not None or stored_names:
        await _write_block(
            db,
            org,
            user,
            None,
            action="organization.commerce_disconnected",
            drop_secrets=frozenset(stored_names),
            drop_reason="commerce_disconnected",
        )
    return Response(status_code=204)


@router.post("/test")
async def test_connection(
    body: dict | None = None,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Test the posted form (with any secrets typed into it) or, with no body,
    the saved connection. Stored secrets join a posted form only while it names
    the saved platform at the saved destination
    (``provider_credentials.config_for_connection_test``). Returns the locations
    the credentials see."""
    request_cfg = body if body and body.get("provider") else None
    try:
        config = await config_for_connection_test(org, SETTINGS_KEY, request_cfg, db=db)
    except CredentialCryptoError:
        return {
            "success": False,
            "code": "credentials_unreadable",
            "message": CREDENTIALS_UNAVAILABLE_DETAIL,
            "locations": [],
        }
    if not config or not config.get("provider"):
        raise HTTPException(
            status_code=400,
            detail=coded_refusal(COMMERCE_NOT_CONFIGURED, "No commerce platform is configured."),
        )
    try:
        adapter = get_commerce_adapter(config)
    except UnknownCommerceAdapterError as exc:
        return {
            "success": False,
            "code": "not_configured",
            "message": f"'{exc.provider}' is not a supported commerce platform.",
            "locations": [],
        }
    try:
        await adapter.test_connection()
        locations = await adapter.list_locations()
    except CommerceError as exc:
        # The code is the answer — the panel localizes it. The adapter's text
        # stays in the log: echoing it would trust every provider module to keep
        # its message clean, which nothing enforces.
        logger.info("commerce test_connection refused: %s", exc.code)
        return {"success": False, "code": exc.code, "message": "", "locations": []}
    except Exception as exc:  # noqa: BLE001 — class name only; no provider text
        logger.warning("commerce test_connection raised %s", type(exc).__name__)
        return {"success": False, "code": "unexpected", "message": "", "locations": []}
    return {
        "success": True,
        "code": None,
        "message": "",
        "locations": [{"id": loc.id, "name": loc.name, "active": loc.active} for loc in locations],
    }


@router.get("/oauth/{provider}/authorize")
async def oauth_authorize(provider: str, user: User = Depends(require_roles(ROLE_ADMIN))):
    """The Connect button's hook for an OAuth provider.

    No commerce provider in this build connects by OAuth consent — each offers a
    key-based connection — so this refuses with a stable code. The route and the
    form's Connect button exist so an OAuth provider plugs in here (the flow in
    ``services/erp_oauth`` is ERP-bound today; ``docs/followups.md``).
    """
    try:
        cls = adapter_class(provider)
    except UnknownCommerceAdapterError:
        raise HTTPException(status_code=404, detail="Unknown commerce platform") from None
    raise HTTPException(
        status_code=501,
        detail=coded_refusal(
            COMMERCE_OAUTH_NOT_AVAILABLE,
            f"{cls.spec.label} cannot be connected by OAuth in this build; use its key-based "
            "connection.",
            provider=cls.spec.key,
        ),
    )


# --------------------------------------------------------------------------- #
# Location mapping
# --------------------------------------------------------------------------- #


@router.get("/locations")
async def get_locations(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """The provider's locations (live) plus the stored mapping."""
    stored = _stored(org)
    if not stored:
        raise HTTPException(
            status_code=409,
            detail=coded_refusal(COMMERCE_NOT_CONFIGURED, "No commerce platform is configured."),
        )
    mapping = {
        "location_id": stored.get(commerce_catalog.LOCATION_KEY),
        "entity_locations": stored.get(commerce_catalog.ENTITY_LOCATIONS_KEY) or {},
    }
    try:
        adapter = get_commerce_adapter(await provider_config(org, SETTINGS_KEY, db=db) or {})
        locations = await adapter.list_locations()
    except (CommerceError, UnknownCommerceAdapterError, CredentialCryptoError) as exc:
        code = getattr(exc, "code", None) or (
            "credentials_unreadable" if isinstance(exc, CredentialCryptoError) else "not_configured"
        )
        return {"available": False, "code": code, "locations": [], **mapping}
    return {
        "available": True,
        "code": None,
        "locations": [{"id": loc.id, "name": loc.name, "active": loc.active} for loc in locations],
        **mapping,
    }


@router.put("/locations")
async def put_locations(
    body: CommerceLocationsUpdate,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
    tenant_db: AsyncSession = Depends(get_tenant_db),
):
    """Map the org (and optionally each entity) to a provider location."""
    stored = _stored(org)
    if not stored:
        raise HTTPException(
            status_code=409,
            detail=coded_refusal(COMMERCE_NOT_CONFIGURED, "No commerce platform is configured."),
        )
    entity_locations = {str(k): v.strip() for k, v in body.entity_locations.items() if v.strip()}
    if entity_locations:
        known = {
            str(e)
            for e in (
                await tenant_db.execute(
                    select(Entity.id).where(Entity.id.in_([uuid.UUID(k) for k in entity_locations]))
                )
            ).scalars()
        }
        unknown = sorted(set(entity_locations) - known)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=coded_refusal(
                    COMMERCE_UNKNOWN_ENTITY, "A mapped entity does not exist.", entityId=unknown[0]
                ),
            )
    new_block = dict(stored)
    location_id = (body.location_id or "").strip()
    if location_id:
        new_block[commerce_catalog.LOCATION_KEY] = location_id
    else:
        new_block.pop(commerce_catalog.LOCATION_KEY, None)
    if entity_locations:
        new_block[commerce_catalog.ENTITY_LOCATIONS_KEY] = entity_locations
    else:
        new_block.pop(commerce_catalog.ENTITY_LOCATIONS_KEY, None)
    block = await _write_block(
        db,
        org,
        user,
        new_block,
        action="organization.commerce_locations_updated",
        extra={"entity_count": len(entity_locations)},
    )
    return {
        "location_id": (block or {}).get(commerce_catalog.LOCATION_KEY),
        "entity_locations": (block or {}).get(commerce_catalog.ENTITY_LOCATIONS_KEY) or {},
    }


# --------------------------------------------------------------------------- #
# SKU overrides
# --------------------------------------------------------------------------- #


def _mapping_dict(m: CommerceSkuMapping) -> dict:
    return {
        "id": str(m.id),
        "provider": m.provider,
        "sku": m.sku,
        "provider_item_id": m.provider_item_id,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


@router.get("/sku-mappings")
async def list_sku_mappings(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_tenant_db),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """The overrides for the CONFIGURED provider (another provider's are inert)."""
    provider = (_stored(org) or {}).get("provider")
    rows = (
        (
            await db.execute(
                select(CommerceSkuMapping)
                .where(
                    CommerceSkuMapping.organization_id == org_id,
                    CommerceSkuMapping.provider == (provider or ""),
                )
                .order_by(CommerceSkuMapping.sku)
            )
        )
        .scalars()
        .all()
    )
    return {"provider": provider, "items": [_mapping_dict(m) for m in rows]}


@router.put("/sku-mappings")
async def upsert_sku_mapping(
    body: CommerceSkuMappingUpsert,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_tenant_db),
    org_id: uuid.UUID = Depends(get_org_id),
):
    provider = (_stored(org) or {}).get("provider")
    if not provider:
        raise HTTPException(
            status_code=409,
            detail=coded_refusal(COMMERCE_NOT_CONFIGURED, "No commerce platform is configured."),
        )
    sku = body.sku.strip()
    existing = (
        await db.execute(
            select(CommerceSkuMapping)
            .where(
                CommerceSkuMapping.organization_id == org_id,
                CommerceSkuMapping.provider == provider,
                CommerceSkuMapping.sku == sku,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    before = existing.provider_item_id if existing else None
    if existing is None:
        existing = CommerceSkuMapping(
            organization_id=org_id,
            provider=provider,
            sku=sku,
            provider_item_id=body.provider_item_id.strip(),
            created_by_user_id=user.id,
        )
        db.add(existing)
    else:
        existing.provider_item_id = body.provider_item_id.strip()
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="This SKU was mapped concurrently.") from None
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="commerce_sku_mapping.saved",
        entity_type="commerce_sku_mapping",
        entity_id=existing.id,
        details={
            "provider": provider,
            "sku": sku,
            "provider_item_id": existing.provider_item_id,
            "previous_provider_item_id": before,
        },
    )
    await db.refresh(existing)
    return _mapping_dict(existing)


@router.delete("/sku-mappings/{mapping_id}", status_code=204)
async def delete_sku_mapping(
    mapping_id: uuid.UUID,
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_tenant_db),
    org_id: uuid.UUID = Depends(get_org_id),
):
    row = (
        await db.execute(
            select(CommerceSkuMapping).where(
                CommerceSkuMapping.id == mapping_id, CommerceSkuMapping.organization_id == org_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="SKU mapping not found")
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="commerce_sku_mapping.deleted",
        entity_type="commerce_sku_mapping",
        entity_id=row.id,
        details={"provider": row.provider, "sku": row.sku},
    )
    await db.execute(delete(CommerceSkuMapping).where(CommerceSkuMapping.id == row.id))
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Per-receipt sync status
# --------------------------------------------------------------------------- #


def sync_dict(row: CommerceInventorySync) -> dict:
    return {
        "id": str(row.id),
        "goods_receipt_id": str(row.goods_receipt_id),
        "gr_line_item_id": str(row.gr_line_item_id),
        "direction": row.direction,
        "provider": row.provider,
        "sku": row.sku,
        # A count of goods, sent as an exact decimal string.
        "quantity_delta": format(row.quantity_delta.normalize(), "f"),
        "status": row.status,
        "reason": row.reason,
        "error_message": row.error_message,
        "retryable": row.retryable,
        "provider_reference": row.provider_reference,
        "provider_location_id": row.provider_location_id,
        "attempts": row.attempts,
        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else None,
        "synced_at": row.synced_at.isoformat() if row.synced_at else None,
        "can_retry": row.status == "failed"
        or (row.status == "skipped" and row.reason in commerce_sync.RETRYABLE_SKIPS),
    }


@router.get("/syncs")
async def list_syncs(
    goods_receipt_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Every sync row for one receipt, resolved within the caller's entity like
    ``GET /api/goods-receipts/{id}`` (an out-of-scope receipt is the same 404)."""
    receipt = (
        await db.execute(
            apply_entity_scope(
                select(GoodsReceipt.id).where(GoodsReceipt.id == goods_receipt_id),
                GoodsReceipt,
                entity_id,
            )
        )
    ).scalar_one_or_none()
    if receipt is None:
        raise HTTPException(status_code=404, detail="Goods receipt not found")
    rows = (
        (
            await db.execute(
                select(CommerceInventorySync)
                .where(CommerceInventorySync.goods_receipt_id == goods_receipt_id)
                .order_by(CommerceInventorySync.created_at, CommerceInventorySync.direction)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [sync_dict(r) for r in rows]}


@router.post("/syncs/{sync_id}/retry")
async def retry_sync(
    sync_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*RECEIPT_ENTRY_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Retry a failed push (or a skip an admin has since fixed) — runs after commit."""
    row = await commerce_sync.request_retry(
        db,
        org_id=org_id,
        sync_id=sync_id,
        entity_id=entity_id,
        actor_id=user.id,
        org_settings=org.settings or {},
        tenant_db_name=org.db_name,
    )
    return sync_dict(row)
