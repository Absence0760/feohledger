"""Approver ∉ the implicated set, at EVERY approval door, end to end.

The rule lives in one place (`review.approve_invoice` and the predicates in
`services/approval_chain`), and each door's own suite pins it for the one case
that suite was written for: the email / Slack / Teams suites only ever make the
approver the *uploader*, and the receiver / inspector suite drives the single
and bulk doors over HTTP but the chat and email doors only through a direct
call to the shared service. A door that grew its own pre-check, swallowed the
service's refusal, or forgot to forward the receipt / inspection recorders
would pass every one of those.

This module is the cross product, over real HTTP and a real database:

    doors  = single approve · bulk approve · email link · Slack button · Teams card
    kinds  = uploader · segregation_actor_ids · goods receiver · quality inspector

Every refused cell must leave the invoice in `ready_for_review`, unstamped
(`approved_by_user_id` NULL) and with no `invoice.approved` audit row, and the
two coded doors (single, bulk) must name the rule that fired. A `clean` row per
door proves the harness can actually approve through that door, so a refusal
is never vacuous. Requires the dev Postgres (`realdb`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.approval_chain import (
    APPROVAL_SEGREGATION,
    APPROVAL_SEGREGATION_INSPECTOR,
    APPROVAL_SEGREGATION_RECEIVER,
)
from app.services.email_action_token import (
    ACTION_APPROVE,
    CHANNEL_EMAIL,
    CHANNEL_SLACK,
    CHANNEL_TEAMS,
    build_action_token,
    digest_of_invoice,
)
from tests.test_approval_receiver_segregation import _inspect, _po, _receive

TENANT = "a"
APPROVER_ROLE = "ap_manager"
pytestmark = pytest.mark.asyncio

_ACTION_KEY = "matrix-action-key"
_SLACK_SECRET = "matrix-slack-signing-secret"
_TEAMS_SECRET = base64.b64encode(b"matrix-teams-security-token").decode("ascii")

DOORS = ("single", "bulk", "email", "slack", "teams")

#: kind -> the code the coded doors must name when it refuses.
KINDS = {
    "uploader": APPROVAL_SEGREGATION,
    "segregation_actor_ids": APPROVAL_SEGREGATION,
    "receiver": APPROVAL_SEGREGATION_RECEIVER,
    "inspector": APPROVAL_SEGREGATION_INSPECTOR,
}


@pytest.fixture
def door_keys(monkeypatch):
    monkeypatch.setattr(settings, "email_action_signing_key", _ACTION_KEY)
    monkeypatch.setattr(settings, "email_action_ttl_hours", 168)
    monkeypatch.setattr(settings, "slack_signing_secret", _SLACK_SECRET)
    monkeypatch.setattr(settings, "slack_request_max_age_seconds", 300)
    monkeypatch.setattr(settings, "teams_security_token", _TEAMS_SECRET)
    monkeypatch.setattr(settings, "teams_request_max_age_seconds", 300)


# --------------------------------------------------------------------------- #
# Arrange: one ready-for-review invoice that implicates the approver by `kind`
# --------------------------------------------------------------------------- #


async def _arrange(realdb, kind: str) -> uuid.UUID:
    approver = realdb.info(TENANT).users[APPROVER_ROLE]
    po_number = None
    if kind in ("receiver", "inspector"):
        po_id, po_number, line_id = await _po(realdb)
        if kind == "receiver":
            await _receive(realdb, APPROVER_ROLE, po_id, line_id)
        else:
            await _inspect(realdb, APPROVER_ROLE, po_id=po_id)

    async with realdb.sessionmaker(TENANT)() as s:
        entity_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        inv = Invoice(
            invoice_number=f"SODM-{uuid.uuid4().hex[:8]}",
            vendor_name="Matrix Supplies Ltd",
            amount=Decimal("1037.00"),
            currency="USD",
            po_number=po_number,
            status=InvoiceStatus.ready_for_review,
            # Somebody who never approves here, unless the uploader IS the case.
            uploaded_by_id=approver if kind == "uploader" else uuid.uuid4(),
            segregation_actor_ids=(
                [str(uuid.uuid4()), str(approver)] if kind == "segregation_actor_ids" else None
            ),
            organization_id=realdb.info(TENANT).org_id,
            entity_id=entity_id,
        )
        s.add(inv)
        await s.commit()
        return inv.id


# --------------------------------------------------------------------------- #
# Act: approve as the approver through one door
# --------------------------------------------------------------------------- #


async def _token(realdb, invoice_id, channel: str) -> str:
    async with realdb.sessionmaker(TENANT)() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        facts = digest_of_invoice(inv, review_round=0)
    info = realdb.info(TENANT)
    return build_action_token(
        tenant_slug=info.slug,
        invoice_id=invoice_id,
        facts=facts,
        actor_id=info.users[APPROVER_ROLE],
        action=ACTION_APPROVE,
        signing_key=_ACTION_KEY,
        ttl_hours=168,
        channel=channel,
    )


def _slack_request(token: str) -> tuple[bytes, dict]:
    payload = {"type": "block_actions", "actions": [{"action_id": "ap_approve", "value": token}]}
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")
    ts = str(int(time.time()))
    base = b"v0:" + ts.encode("utf-8") + b":" + body
    sig = "v0=" + hmac.new(_SLACK_SECRET.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return body, {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
    }


def _teams_request(token: str) -> tuple[bytes, dict]:
    body = json.dumps({"type": "message", "text": "approve", "value": {"token": token}}).encode()
    digest = hmac.new(base64.b64decode(_TEAMS_SECRET), body, hashlib.sha256).digest()
    return body, {
        "Content-Type": "application/json",
        "Authorization": f"HMAC {base64.b64encode(digest).decode('ascii')}",
    }


async def _approve_via(realdb, door: str, invoice_id: uuid.UUID):
    """Returns the coded refusal (or None on success) for the coded doors; the
    link / chat doors answer a friendly 200 either way, so they return None and
    the database is the verdict."""
    if door == "single":
        async with realdb.client(key=TENANT, role=APPROVER_ROLE) as c:
            resp = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
        if resp.status_code == 200:
            return None
        assert resp.status_code == 403, resp.text
        return resp.json()["detail"]["code"]
    if door == "bulk":
        async with realdb.client(key=TENANT, role=APPROVER_ROLE) as c:
            resp = await c.post(
                "/api/invoices/bulk/status",
                json={"ids": [str(invoice_id)], "status": "approved"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["updated"] == 1:
            assert body["skipped"] == []
            return None
        (skip,) = body["skipped"]
        assert skip["id"] == str(invoice_id)
        return skip["code"]
    if door == "email":
        token = await _token(realdb, invoice_id, CHANNEL_EMAIL)
        async with realdb.client(key=TENANT, role=None) as c:
            resp = await c.post(f"/api/invoices/email-action/{token}/confirm")
        assert resp.status_code == 200, resp.text
        return None
    if door == "slack":
        body, headers = _slack_request(await _token(realdb, invoice_id, CHANNEL_SLACK))
        async with realdb.client(key=TENANT, role=None) as c:
            resp = await c.post("/api/approvals/slack/interactivity", content=body, headers=headers)
        assert resp.status_code == 200, resp.text
        return None
    if door == "teams":
        body, headers = _teams_request(await _token(realdb, invoice_id, CHANNEL_TEAMS))
        async with realdb.client(key=TENANT, role=None) as c:
            resp = await c.post("/api/approvals/teams/interactivity", content=body, headers=headers)
        assert resp.status_code == 200, resp.text
        return None
    raise AssertionError(door)


async def _outcome(realdb, invoice_id):
    async with realdb.sessionmaker(TENANT)() as s:
        status, stamped = (
            await s.execute(
                select(Invoice.status, Invoice.approved_by_user_id).where(Invoice.id == invoice_id)
            )
        ).one()
        audits = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_id == invoice_id, AuditLog.action == "invoice.approved")
            )
        ).scalar_one()
    return status, stamped, audits


# --------------------------------------------------------------------------- #
# The matrix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("door", DOORS)
async def test_an_implicated_approver_is_refused_at_every_door(realdb, door_keys, door, kind):
    inv_id = await _arrange(realdb, kind)

    code = await _approve_via(realdb, door, inv_id)

    if door in ("single", "bulk"):
        assert code == KINDS[kind], (door, kind, code)
    status, stamped, audits = await _outcome(realdb, inv_id)
    assert status == InvoiceStatus.ready_for_review, (door, kind)
    assert stamped is None, (door, kind)
    assert audits == 0, (door, kind)


@pytest.mark.parametrize("door", DOORS)
async def test_an_unimplicated_approver_gets_through_every_door(realdb, door_keys, door):
    """The control row: same approver, same door, nobody implicated. Without
    it a door that refused everything would pass the matrix above."""
    inv_id = await _arrange(realdb, "clean")

    assert await _approve_via(realdb, door, inv_id) is None

    status, stamped, audits = await _outcome(realdb, inv_id)
    assert status == InvoiceStatus.approved, door
    assert stamped == realdb.info(TENANT).users[APPROVER_ROLE], door
    assert audits == 1, door
