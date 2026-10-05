"""A SCIM PATCH of `active` reads the value, not its truthiness.

Microsoft Entra ID deprovisions with
`{"op": "Replace", "path": "active", "value": "False"}` — the STRING — unless the
app is registered with the `aadOptscim062020` compliance flag. The handler read
it with `bool(value)`, and `bool("False")` is `True`, so Entra's deprovision
left the account active (or re-activated a disabled one) while answering 200:
the IdP recorded an offboarding that never happened, and the person kept every
session and role they had.

Driven against real Postgres through the handler, like
`test_scim_user_uniqueness.py`, so the assertion is on the stored row.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.scim import patch_user
from app.models.organization import Organization
from app.models.user import User
from app.schemas.scim import SCIMPatchOp, SCIMPatchRequest

pytestmark = pytest.mark.asyncio

_REQUEST = SimpleNamespace(base_url="http://testserver/")


async def _org_a(realdb) -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()


@pytest_asyncio.fixture
async def scim_user(realdb):
    """A throwaway control-plane user in tenant A, removed afterwards."""
    mk = realdb.control_sessionmaker()
    created: list[uuid.UUID] = []

    async def _make(*, active: bool) -> uuid.UUID:
        user_id = uuid.uuid4()
        async with mk() as s:
            s.add(
                User(
                    id=user_id,
                    email=f"scim-active-{user_id}@example.test",
                    full_name="SCIM Active Probe",
                    hashed_password=None,
                    organization_id=realdb.info("a").org_id,
                    is_active=active,
                    must_change_password=False,
                )
            )
            await s.commit()
        created.append(user_id)
        return user_id

    yield _make

    async with mk() as s:
        for user_id in created:
            await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def _patch(realdb, user_id, *ops: SCIMPatchOp) -> bool:
    org = await _org_a(realdb)
    async with realdb.control_sessionmaker()() as s:
        await patch_user(user_id, SCIMPatchRequest(Operations=list(ops)), _REQUEST, org, s)
        await s.commit()
    async with realdb.control_sessionmaker()() as s:
        return (await s.get(User, user_id)).is_active


@pytest.mark.parametrize("value", ["False", "false", " FALSE ", False])
async def test_entra_style_deprovision_deactivates(realdb, scim_user, value):
    user_id = await scim_user(active=True)

    active = await _patch(realdb, user_id, SCIMPatchOp(op="Replace", path="active", value=value))

    assert active is False


async def test_a_string_false_in_a_root_replace_deactivates(realdb, scim_user):
    """The other shape: a path-less replace carrying `{"active": "False"}`."""
    user_id = await scim_user(active=True)

    active = await _patch(realdb, user_id, SCIMPatchOp(op="replace", value={"active": "False"}))

    assert active is False


@pytest.mark.parametrize("value", ["True", "true", True])
async def test_reactivation_still_works(realdb, scim_user, value):
    user_id = await scim_user(active=False)

    active = await _patch(realdb, user_id, SCIMPatchOp(op="replace", path="active", value=value))

    assert active is True


@pytest.mark.parametrize("value", ["no", "0", 0, None, "", ["false"]])
async def test_an_unreadable_active_value_is_a_400_and_changes_nothing(realdb, scim_user, value):
    """Never a guess in either direction: the IdP must see the failure."""
    user_id = await scim_user(active=True)

    with pytest.raises(HTTPException) as exc:
        await _patch(realdb, user_id, SCIMPatchOp(op="replace", path="active", value=value))

    assert exc.value.status_code == 400
    assert exc.value.detail["scimType"] == "invalidValue"
    async with realdb.control_sessionmaker()() as s:
        assert (await s.get(User, user_id)).is_active is True
