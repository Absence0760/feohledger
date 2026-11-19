"""The inbound-webhook contract, held for every route at once.

Project invariant: a webhook handler verifies its provider's signature, dedupes
by event id, and answers every rejection the same way so the response is never
an oracle (a silent bodyless 204; Xero's protocol forces 401 / bodyless 200,
decisions §392; the Slack / Teams callbacks and email intake answer an opaque
ack). Each provider has its own deep test file; this one holds the contract
ACROSS them, through the real ASGI app:

* ``WEBHOOKS`` is the inventory. A new inbound webhook route that is not in it
  fails :func:`test_every_inbound_webhook_route_is_in_the_contract_table`, so it
  has to be added here together with its signature scheme, its rejection
  answer and the tests that pin its dedup.
* For each route, every rejection probe (unsigned, wrong signature, a signed
  timestamp outside the window, an unknown tenant / target, a replay) gets the
  SAME status and the SAME body, and none of them reaches the route's effect:
  no dedup claim is written and the route's apply seam is never called.
* Where a verified delivery can be built cheaply, the same delivery posted
  twice has exactly one effect.

Depth that needs a seeded tenant per provider (a payment rail's adapter
signature, a connected ERP, a PEPPOL Access Point) lives in the per-provider
files; each row cites them by name and :func:`test_every_cited_test_exists`
keeps those citations honest.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select

from app.config import settings
from app.services import webhook_security
from app.services.webhook_security import DEDUP_PREFIX

_TESTS = Path(__file__).parent


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Webhook:
    scheme: str
    #: (status, body) of every pre-verification rejection.
    reject: tuple[int, bytes]
    #: (status, body) of every verified decision (unknown target, duplicate).
    #: Equal to ``reject`` wherever the key is per-tenant, so "verified" itself
    #: never distinguishes a tenant; it may differ only for a platform-wide key.
    decision: tuple[int, bytes]
    #: Whether the provider signs a timestamp (and so a replay window applies).
    signs_timestamp: bool
    dedup: str
    #: ``file::test`` names pinning the depth this file does not repeat.
    depth: tuple[str, ...]


_SILENT = (204, b"")
_SLACK_ACK = (
    200,
    json.dumps(
        {"response_type": "ephemeral", "text": "Thanks — your response was recorded."},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode(),
)
_TEAMS_ACK = (
    200,
    json.dumps(
        {"type": "message", "text": "Thanks — your response was recorded."},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode(),
)
_EMAIL_ACK = (200, b'{"status":"received"}')

WEBHOOKS: dict[str, Webhook] = {
    "/api/cards/webhook/{provider}": Webhook(
        scheme="hex HMAC-SHA256 of the raw body, the owning tenant's sealed card key",
        reject=_SILENT,
        decision=_SILENT,
        signs_timestamp=False,
        dedup="cards:<provider event id>",
        depth=(
            "test_webhook_security.py::test_card_webhook_dedups_replayed_event",
            "test_card_money_path_regression.py::"
            "test_a_replayed_settlement_event_does_not_re_rebate_or_re_stamp",
        ),
    ),
    "/api/payments/webhook/{tenant_slug}/{provider}": Webhook(
        scheme="the rail adapter's own scheme (parse_webhook), the URL tenant's sealed key",
        reject=_SILENT,
        decision=_SILENT,
        signs_timestamp=True,  # stripe_treasury does; the other rails sign the body only
        dedup="<provider>:<event id>; an empty id falls to the terminal-state allowlist",
        depth=(
            "test_payments_webhook_dedup.py::test_populated_event_id_still_deduped",
            "test_payment_webhook_security.py::test_webhook_returns_silently_for_unknown_tenant",
            "test_payment_webhook_security.py::"
            "test_webhook_returns_silently_when_adapter_rejects_signature",
            "test_payment_adapters_new.py::test_stripe_treasury_webhook_rejects_stale_timestamp",
        ),
    ),
    "/api/erp/webhook/quickbooks": Webhook(
        scheme="base64 HMAC-SHA256 of the raw body, the platform's Intuit verifier token",
        reject=_SILENT,
        decision=_SILENT,
        signs_timestamp=False,
        dedup="per CloudEvent id",
        depth=("test_qbo_webhook.py::test_claiming_dedupes_by_event_id",),
    ),
    "/api/erp/webhook/xero": Webhook(
        scheme="base64 HMAC-SHA256 of the raw body, the platform's Xero webhook key",
        reject=(401, b""),
        decision=(200, b""),
        signs_timestamp=False,
        dedup="per event identity (tenant, resource, type, date)",
        depth=("test_xero_webhook.py::test_claiming_dedupes_by_the_events_identity",),
    ),
    "/api/erp/webhook/merge_dev/{tenant_slug}": Webhook(
        scheme="base64url HMAC-SHA256 of the raw body, the URL tenant's sealed Merge key",
        reject=_SILENT,
        decision=_SILENT,
        signs_timestamp=False,
        dedup="tenant + bill + modified_at",
        depth=(
            "test_merge_webhook.py::test_a_replayed_delivery_is_dropped_by_its_claim",
            "test_merge_webhook.py::test_an_unknown_slug_answers_exactly_as_a_bad_signature_does",
        ),
    ),
    "/api/erp/webhook/{erp_type}": Webhook(
        scheme="hex HMAC-SHA256 of the raw body, the body-named tenant's sealed ERP key",
        reject=_SILENT,
        decision=_SILENT,
        signs_timestamp=False,
        dedup="erp:<erp_type>:<event_id>; an absent id is applied by the guarded transition",
        depth=(
            "test_erp_webhook_dedup_regression.py::"
            "test_distinct_status_events_without_event_id_both_apply",
        ),
    ),
    "/api/billing/webhook/{provider}": Webhook(
        scheme="Stripe-Signature t=<ts>,v1=<HMAC-SHA256 of 't.body'>, the platform's key",
        reject=_SILENT,
        decision=_SILENT,
        signs_timestamp=True,
        dedup="billing:<event id>",
        depth=(
            "test_billing_webhook.py::test_webhook_dedupes_by_event_id",
            "test_billing_webhook.py::test_stripe_signature_rejects_an_old_but_correctly_signed_event",
        ),
    ),
    "/api/email-intake/inbound/{provider}": Webhook(
        scheme="hex HMAC-SHA256 of the raw body, the platform's intake signing secret",
        reject=_SILENT,
        # The secret is platform-wide, so "verified" names no tenant; every
        # decision after it (unknown token, duplicate, success) is one ack.
        decision=_EMAIL_ACK,
        signs_timestamp=False,
        dedup="org-scoped message id",
        depth=(
            "test_email_intake_processing.py::"
            "test_process_dedupes_identical_message_id_across_deliveries",
            "test_email_intake.py::test_inbound_webhook_decisions_all_share_the_same_200_ack",
        ),
    ),
    "/api/peppol/inbound/{organization_id}": Webhook(
        scheme="hex HMAC-SHA256 of the raw body, the platform's Access Point secret",
        reject=_SILENT,
        decision=_SILENT,
        signs_timestamp=False,
        dedup="AS4 message id, per tenant",
        depth=("test_peppol_inbound.py::test_redelivery_is_deduped_no_second_invoice",),
    ),
    "/api/approvals/slack/interactivity": Webhook(
        scheme="Slack v0 HMAC-SHA256 of 'v0:ts:body', the platform's signing secret",
        reject=_SLACK_ACK,
        decision=_SLACK_ACK,
        signs_timestamp=True,
        dedup="single-use action-token pair",
        depth=("test_slack_approvals.py::test_approve_is_single_use",),
    ),
    "/api/approvals/teams/interactivity": Webhook(
        scheme="Teams base64 HMAC-SHA256 of the body (Authorization: HMAC), platform token",
        reject=_TEAMS_ACK,
        decision=_TEAMS_ACK,
        signs_timestamp=True,  # when the timestamp header is present
        dedup="single-use action-token pair",
        depth=("test_teams_approvals.py::test_approve_is_single_use",),
    ),
}

#: Routes whose path looks like an inbound webhook but are not one: each is
#: behind employee auth, and says why here so the exclusion is a decision.
NOT_INBOUND = {
    "/api/webhooks": "outbound subscription management (admin JWT)",
    "/api/organization/": "tenant settings for a chat webhook / intake address (admin JWT)",
}

_INBOUND_SHAPE = re.compile(r"/(webhook|webhooks|inbound|interactivity)(/|$)")


def _flat_routes() -> list[tuple[str, set[str]]]:
    from fastapi.routing import iter_route_contexts

    from app.main import app

    return [(c.path, set(c.methods or ())) for c in iter_route_contexts(app.routes)]


def test_every_inbound_webhook_route_is_in_the_contract_table():
    routes = _flat_routes()
    # Guard the guard: a flattener that sees nothing would pass vacuously.
    assert len(routes) >= 400
    found = {
        path
        for path, methods in routes
        if "POST" in methods
        and _INBOUND_SHAPE.search(path)
        and not any(path.startswith(prefix) for prefix in NOT_INBOUND)
    }
    missing = found - WEBHOOKS.keys()
    stale = WEBHOOKS.keys() - found
    assert not missing, (
        f"inbound webhook route(s) {sorted(missing)} are not in WEBHOOKS: add each with its "
        "signature scheme, rejection answer, dedup key and the tests that pin them"
    )
    assert not stale, f"WEBHOOKS lists route(s) the app no longer serves: {sorted(stale)}"


def test_every_cited_test_exists():
    for path, row in WEBHOOKS.items():
        assert row.depth, f"{path} cites no test for its dedup"
        for ref in row.depth:
            file, name = ref.split("::")
            tree = ast.parse((_TESTS / file).read_text())
            names = {
                n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            assert name in names, f"{path} cites {ref}, which does not exist"


def test_a_per_tenant_key_never_answers_a_verified_decision_differently():
    """Where the key is the tenant's own, verifying it would prove the tenant
    exists: only a platform-wide key may answer a decision differently."""
    for path, row in WEBHOOKS.items():
        if "{tenant_slug}" in path or "cards" in path or path.endswith("{erp_type}"):
            assert row.decision == row.reject, path


# ---------------------------------------------------------------------------
# Helpers for the through-the-app matrix
# ---------------------------------------------------------------------------


async def _dedup_claims() -> set[str]:
    r = await webhook_security.get_redis()
    return {k for k in r._kv if k.startswith(DEDUP_PREFIX)}


class _Recorder:
    """Stands in for a route's effect seam and counts what reached it."""

    def __init__(self, result=None, *, wraps=None):
        self.calls: list = []
        self._result = result
        self._wraps = wraps

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._wraps is not None:
            return self._wraps(*args, **kwargs)
        return self._result


class _AsyncRecorder(_Recorder):
    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._wraps is not None:
            return await self._wraps(*args, **kwargs)
        return self._result


def _answer(resp) -> tuple[int, bytes]:
    return resp.status_code, resp.content


async def _post(c, url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    return _answer(await c.post(url, content=body, headers=headers))


def _client(realdb):
    c = realdb.client(key="a", role=None)
    c.headers.pop("X-Tenant-Slug", None)  # no provider sends our tenant header
    return c


def _hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _b64(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


async def _assert_all(probes: dict[str, tuple[int, bytes]], expected: tuple[int, bytes]):
    wrong = {label: got for label, got in probes.items() if got != expected}
    assert not wrong, f"answers differ from {expected!r}: {wrong!r}"


# ---------------------------------------------------------------------------
# Cards — per-tenant key, looked up by the card the event names
# ---------------------------------------------------------------------------


async def test_cards_rejections_are_one_answer_and_a_replay_settles_once(realdb):
    from app.models.virtual_card import CardRebate, VirtualCard
    from tests.test_card_webhook_provider_cross_check import (
        _seed_charged_card,
        _set_webhook_secret,
    )
    from tests.test_card_webhook_provider_cross_check import _sign as card_sign

    row = WEBHOOKS["/api/cards/webhook/{provider}"]
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(mk, org_id, provider="nium", token=token)
    await _set_webhook_secret(realdb, org_id=org_id)

    def event(card_token: str, event_id: str) -> bytes:
        return json.dumps(
            {
                "eventType": "transaction.settled",
                "webhookId": event_id,
                "cardHashId": card_token,
                "amount": "250.00",
                "merchantName": "Contract Merchant",
            }
        ).encode()

    url = "/api/cards/webhook/nium"
    body = event(token, str(uuid.uuid4()))
    unknown = event(f"tok_{uuid.uuid4().hex[:12]}", str(uuid.uuid4()))
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(c, url, body, {"Content-Type": "application/json"}),
            "bad signature": await _post(c, url, body, {"Webhook-Signature": "0" * 64}),
            "signed for another body": await _post(
                c, url, body, {"Webhook-Signature": card_sign(b"{}")}
            ),
            "unknown card, correctly signed": await _post(
                c, url, unknown, {"Webhook-Signature": card_sign(unknown)}
            ),
            "unknown provider": await _post(
                c, "/api/cards/webhook/nope", body, {"Webhook-Signature": card_sign(body)}
            ),
        }
        await _assert_all(rejected, row.reject)
        assert await _dedup_claims() == set()
        async with mk() as s:
            card = await s.get(VirtualCard, card_id)
            assert card.status == "charged"

        signed = {"Webhook-Signature": card_sign(body)}
        first = await _post(c, url, body, signed)
        replay = await _post(c, url, body, signed)
    assert first == replay == row.decision

    async with mk() as s:
        card = await s.get(VirtualCard, card_id)
        rebates = (
            await s.execute(
                select(func.count())
                .select_from(CardRebate)
                .where(CardRebate.virtual_card_id == card_id)
            )
        ).scalar_one()
    assert card.status == "completed"
    assert rebates == 1, "a replayed settlement must not rebate twice"


# ---------------------------------------------------------------------------
# Payment rails and the per-tenant ERP routes — every pre-verification door
# ---------------------------------------------------------------------------


async def test_payment_rejections_are_one_answer_and_claim_nothing(realdb):
    row = WEBHOOKS["/api/payments/webhook/{tenant_slug}/{provider}"]
    slug = realdb.info("a").slug
    body = json.dumps({"id": "evt_contract", "type": "payment_order.completed"}).encode()
    sig = {"X-Signature": _hex("guess", body)}
    async with _client(realdb) as c:
        rejected = {
            "unknown tenant": await _post(
                c,
                f"/api/payments/webhook/no-such-{uuid.uuid4().hex[:8]}/modern_treasury",
                body,
                sig,
            ),
            "provider the tenant is not on": await _post(
                c, f"/api/payments/webhook/{slug}/modern_treasury", body, sig
            ),
            "unsigned": await _post(c, f"/api/payments/webhook/{slug}/modern_treasury", body, {}),
            "the mock rail": await _post(c, f"/api/payments/webhook/{slug}/mock", body, sig),
            "unknown rail": await _post(c, f"/api/payments/webhook/{slug}/nope", body, sig),
        }
    await _assert_all(rejected, row.reject)
    assert await _dedup_claims() == set()


async def test_merge_rejections_are_one_answer_and_apply_nothing(realdb, monkeypatch):
    from app.services import erp_merge_webhook

    row = WEBHOOKS["/api/erp/webhook/merge_dev/{tenant_slug}"]
    apply = _AsyncRecorder()
    monkeypatch.setattr(erp_merge_webhook, "apply_merge_event", apply)
    slug = realdb.info("a").slug
    body = json.dumps({"hook": {"event": "Invoice.changed"}, "data": {"id": "b1"}}).encode()
    well_formed = base64.urlsafe_b64encode(hashlib.sha256(b"guess").digest()).decode()
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(c, f"/api/erp/webhook/merge_dev/{slug}", body, {}),
            "malformed signature": await _post(
                c, f"/api/erp/webhook/merge_dev/{slug}", body, {"X-Merge-Webhook-Signature": "x"}
            ),
            "tenant not on Merge": await _post(
                c,
                f"/api/erp/webhook/merge_dev/{slug}",
                body,
                {"X-Merge-Webhook-Signature": well_formed},
            ),
            "unknown tenant": await _post(
                c,
                f"/api/erp/webhook/merge_dev/no-such-{uuid.uuid4().hex[:8]}",
                body,
                {"X-Merge-Webhook-Signature": well_formed},
            ),
        }
    await _assert_all(rejected, row.reject)
    assert apply.calls == []
    assert await _dedup_claims() == set()


async def test_generic_erp_rejections_are_one_answer_and_claim_nothing(realdb):
    row = WEBHOOKS["/api/erp/webhook/{erp_type}"]
    slug = realdb.info("a").slug

    def body(tenant: str) -> bytes:
        return json.dumps(
            {
                "tenant_slug": tenant,
                "correlation_id": str(uuid.uuid4()),
                "event_id": "evt_contract",
                "status": "posted",
            }
        ).encode()

    known, unknown = body(slug), body(f"no-such-{uuid.uuid4().hex[:8]}")
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(c, "/api/erp/webhook/netsuite", known, {}),
            "malformed signature": await _post(
                c, "/api/erp/webhook/netsuite", known, {"X-Webhook-Signature": "nothex"}
            ),
            "tenant not on this ERP": await _post(
                c, "/api/erp/webhook/netsuite", known, {"X-Webhook-Signature": _hex("g", known)}
            ),
            "unknown tenant": await _post(
                c, "/api/erp/webhook/netsuite", unknown, {"X-Webhook-Signature": _hex("g", unknown)}
            ),
            "Merge on the generic route": await _post(
                c, "/api/erp/webhook/merge_dev", known, {"X-Webhook-Signature": _hex("g", known)}
            ),
        }
    await _assert_all(rejected, row.reject)
    assert await _dedup_claims() == set()


# ---------------------------------------------------------------------------
# Platform-key routes — a verified delivery is cheap, so replay is held here
# ---------------------------------------------------------------------------


async def test_quickbooks_rejections_are_one_answer_and_a_replay_dispatches_once(
    realdb, monkeypatch
):
    from app.services import erp_status_sync

    row = WEBHOOKS["/api/erp/webhook/quickbooks"]
    monkeypatch.setattr(settings, "erp_qbo_webhook_verifier_token", "contract-qbo-token")
    dispatch = _Recorder()
    monkeypatch.setattr(erp_status_sync, "dispatch_qbo_events", dispatch)
    url = "/api/erp/webhook/quickbooks"
    event_id = f"evt-{uuid.uuid4().hex[:8]}"
    body = json.dumps(
        [
            {
                "specversion": "1.0",
                "id": event_id,
                "source": "intuit.test",
                "type": "qbo.bill.updated.v1",
                "time": "2026-10-20T10:00:00Z",
                "intuitentityid": "501",
                "intuitaccountid": "no-such-company",
                "data": {},
            }
        ]
    ).encode()
    signed = {"intuit-signature": _b64("contract-qbo-token", body)}
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(c, url, body, {}),
            "bad signature": await _post(c, url, body, {"intuit-signature": "AAAA"}),
            "another token": await _post(c, url, body, {"intuit-signature": _b64("other", body)}),
        }
        assert dispatch.calls == []
        assert await _dedup_claims() == set()
        first = await _post(c, url, body, signed)  # unknown company: a verified decision
        replay = await _post(c, url, body, signed)
    await _assert_all(rejected, row.reject)
    assert first == replay == row.decision
    dispatched = [e for (args, _kw) in dispatch.calls for e in args[0]]
    assert [e.event_id for e in dispatched] == [event_id], "a replay must dispatch nothing"


async def test_xero_rejections_are_one_answer_and_a_replay_dispatches_once(realdb, monkeypatch):
    from app.services import erp_xero_webhook

    row = WEBHOOKS["/api/erp/webhook/xero"]
    monkeypatch.setattr(settings, "erp_xero_webhook_key", "contract-xero-key")
    dispatch = _Recorder()
    monkeypatch.setattr(erp_xero_webhook, "dispatch_xero_events", dispatch)
    url = "/api/erp/webhook/xero"
    bill = str(uuid.uuid4())
    body = json.dumps(
        {
            "events": [
                {
                    "resourceUrl": f"https://api.xero.com/api.xro/2.0/Invoices/{bill}",
                    "resourceId": bill,
                    "eventDateUtc": "2026-11-13T10:00:00.000",
                    "eventType": "UPDATE",
                    "eventCategory": "INVOICE",
                    "tenantId": str(uuid.uuid4()),  # an organisation nobody connected
                    "tenantType": "ORGANISATION",
                }
            ],
            "firstEventSequence": 1,
            "lastEventSequence": 1,
            "entropy": "CONTRACT",
        }
    ).encode()
    signed = {"x-xero-signature": _b64("contract-xero-key", body)}
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(c, url, body, {}),
            "bad signature": await _post(c, url, body, {"x-xero-signature": "AAAA"}),
            "another key": await _post(c, url, body, {"x-xero-signature": _b64("other", body)}),
        }
        assert dispatch.calls == []
        assert await _dedup_claims() == set()
        first = await _post(c, url, body, signed)
        replay = await _post(c, url, body, signed)
    await _assert_all(rejected, row.reject)
    assert first == replay == row.decision
    dispatched = [e for (args, _kw) in dispatch.calls for e in args[0]]
    assert len(dispatched) == 1, "a replay must dispatch nothing"


async def test_billing_rejections_include_a_stale_timestamp_and_a_replay_applies_once(
    realdb, monkeypatch
):
    from app.api import billing_webhook
    from tests.test_billing_webhook import _WEBHOOK_SECRET
    from tests.test_billing_webhook import _sign as stripe_sign

    row = WEBHOOKS["/api/billing/webhook/{provider}"]
    monkeypatch.setattr(settings, "billing_webhook_enabled", True)
    monkeypatch.setattr(settings, "billing_provider", "stripe_billing")
    monkeypatch.setattr(settings, "billing_stripe_webhook_secret", _WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "billing_stripe_api_key", "sk_test")
    apply = _AsyncRecorder()
    monkeypatch.setattr(billing_webhook, "apply_billing_event", apply)
    url = "/api/billing/webhook/stripe_billing"
    body = json.dumps(
        {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_contract", "status": "past_due"}},
        }
    ).encode()
    stale = str(int(time.time()) - 3600)
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(c, url, body, {}),
            "bad signature": await _post(c, url, body, {"Stripe-Signature": "t=1,v1=deadbeef"}),
            "another secret": await _post(
                c, url, body, {"Stripe-Signature": stripe_sign(body, "whsec_other")}
            ),
            "stale timestamp, correctly signed": await _post(
                c, url, body, {"Stripe-Signature": stripe_sign(body, timestamp=stale)}
            ),
            "another provider": await _post(
                c, "/api/billing/webhook/nope", body, {"Stripe-Signature": stripe_sign(body)}
            ),
        }
        assert apply.calls == []
        assert await _dedup_claims() == set()
        signed = {"Stripe-Signature": stripe_sign(body)}
        first = await _post(c, url, body, signed)
        replay = await _post(c, url, body, signed)
    await _assert_all(rejected, row.reject)
    assert first == replay == row.decision
    assert len(apply.calls) == 1, "a replayed event must be applied once"


async def test_email_intake_rejections_are_one_answer_and_decisions_another(realdb, monkeypatch):
    from app.api import email_intake as intake_api

    row = WEBHOOKS["/api/email-intake/inbound/{provider}"]
    monkeypatch.setattr(settings, "email_intake_signing_secret", "contract-intake-secret")
    process = _AsyncRecorder(
        SimpleNamespace(tenant_slug=None, invoices_created=[], error="unknown token")
    )
    monkeypatch.setattr(intake_api, "process_inbound_email", process)
    url = "/api/email-intake/inbound/generic"
    body = json.dumps(
        {
            "to": "ap+no-such-token@intake.example.test",
            "from": "vendor@example.test",
            "message_id": f"<{uuid.uuid4().hex}@example.test>",
            "attachments": [],
        }
    ).encode()
    signed = {"X-Signature": _hex("contract-intake-secret", body)}
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(c, url, body, {}),
            "bad signature": await _post(c, url, body, {"X-Signature": "0" * 64}),
            "another secret": await _post(c, url, body, {"X-Signature": _hex("other", body)}),
            "unknown provider": await _post(c, "/api/email-intake/inbound/nope", body, signed),
        }
        assert process.calls == []
        decided = {
            "unknown tenant": await _post(c, url, body, signed),
            "replay": await _post(c, url, body, signed),
        }
    await _assert_all(rejected, row.reject)
    await _assert_all(decided, row.decision)


async def test_peppol_rejections_are_one_answer_and_receive_nothing(realdb, monkeypatch):
    from app.api import peppol_inbound

    row = WEBHOOKS["/api/peppol/inbound/{organization_id}"]
    monkeypatch.setattr(settings, "peppol_inbound_enabled", True)
    monkeypatch.setattr(settings, "peppol_inbound_signing_secret", "contract-peppol-secret")
    receive = _AsyncRecorder()
    monkeypatch.setattr(peppol_inbound, "receive_peppol_message", receive)
    org_id = realdb.info("a").org_id
    body = b"<Invoice/>"
    signed = {"X-Peppol-Signature": _hex("contract-peppol-secret", body)}
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(c, f"/api/peppol/inbound/{org_id}", body, {}),
            "bad signature": await _post(
                c, f"/api/peppol/inbound/{org_id}", body, {"X-Peppol-Signature": "0" * 64}
            ),
            "unknown tenant, correctly signed": await _post(
                c, f"/api/peppol/inbound/{uuid.uuid4()}", body, signed
            ),
            "not a tenant id, correctly signed": await _post(
                c, "/api/peppol/inbound/not-a-uuid", body, signed
            ),
        }
    await _assert_all(rejected, row.reject)
    assert receive.calls == []


# ---------------------------------------------------------------------------
# Chat approval callbacks — timestamped signatures, opaque ack
# ---------------------------------------------------------------------------


async def test_slack_rejections_include_a_stale_timestamp_and_never_open_a_token(
    realdb, monkeypatch
):
    from app.api import slack_approvals
    from tests.test_slack_approvals import (
        _ACTION_KEY,
        _SLACK_SECRET,
        _payload_body,
        _signed_headers,
    )

    row = WEBHOOKS["/api/approvals/slack/interactivity"]
    monkeypatch.setattr(settings, "email_action_signing_key", _ACTION_KEY)
    monkeypatch.setattr(settings, "slack_signing_secret", _SLACK_SECRET)
    monkeypatch.setattr(settings, "slack_request_max_age_seconds", 300)
    opened = _Recorder(wraps=slack_approvals.verify_action_token)
    monkeypatch.setattr(slack_approvals, "verify_action_token", opened)
    url = "/api/approvals/slack/interactivity"
    body = _payload_body("not-a-real-token")
    stale = str(int(time.time()) - 3600)
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(
                c, url, body, {"Content-Type": "application/x-www-form-urlencoded"}
            ),
            "another secret": await _post(c, url, body, _signed_headers(body, secret="other")),
            "stale timestamp, correctly signed": await _post(
                c, url, body, _signed_headers(body, timestamp=stale)
            ),
        }
        assert opened.calls == [], "nothing unverified may reach the action token"
        decided = {"unknown token": await _post(c, url, body, _signed_headers(body))}
    await _assert_all(rejected, row.reject)
    await _assert_all(decided, row.decision)


async def test_teams_rejections_include_a_stale_timestamp_and_never_open_a_token(
    realdb, monkeypatch
):
    from app.api import teams_approvals
    from tests.test_teams_approvals import (
        _ACTION_KEY,
        _TEAMS_SECRET,
        _payload_body,
        _signed_headers,
    )

    row = WEBHOOKS["/api/approvals/teams/interactivity"]
    monkeypatch.setattr(settings, "email_action_signing_key", _ACTION_KEY)
    monkeypatch.setattr(settings, "teams_security_token", _TEAMS_SECRET)
    monkeypatch.setattr(settings, "teams_request_max_age_seconds", 300)
    opened = _Recorder(wraps=teams_approvals.verify_action_token)
    monkeypatch.setattr(teams_approvals, "verify_action_token", opened)
    url = "/api/approvals/teams/interactivity"
    body = _payload_body("not-a-real-token")
    other = base64.b64encode(b"another-token").decode()
    stale = str(int(time.time()) - 3600)
    async with _client(realdb) as c:
        rejected = {
            "unsigned": await _post(c, url, body, {"Content-Type": "application/json"}),
            "another secret": await _post(c, url, body, _signed_headers(body, secret=other)),
            "stale timestamp, correctly signed": await _post(
                c, url, body, _signed_headers(body, timestamp=stale)
            ),
        }
        assert opened.calls == [], "nothing unverified may reach the action token"
        decided = {"unknown token": await _post(c, url, body, _signed_headers(body))}
    await _assert_all(rejected, row.reject)
    await _assert_all(decided, row.decision)
