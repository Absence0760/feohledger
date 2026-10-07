"""ERP webhook transition-guard tests.

Regression coverage for the divergent-transition-map defect: the handler used
to keep its own local `valid_transitions` dict that permitted edges the
authoritative `workflow_engine.VALID_TRANSITIONS` forbids (e.g.
`posted_in_erp → paid`, `posted_in_erp → failed`). When the ERP reported such a
status, `transition_invoice` raised `HTTPException(409)` and the handler
re-raised it — breaking the documented "every webhook rejection path returns 204
silently" invariant AND corrupting nothing (no transition applied).

These tests assert that:
  * an ERP status whose transition the state machine FORBIDS for the invoice's
    current state returns a silent 204 (not a 409/500) and does NOT mutate state
    or write an audit row, and
  * a legitimately-permitted ERP-driven transition still applies AND goes through
    `transition_invoice` (the audit chokepoint).

The `posted_in_erp` + `voided` case additionally opens an `erp_reconciliation`
Exception for human review (money may already be in flight) — a side effect that
is NOT a transition, is idempotent per open exception, and is PII-free — while a
NON-void forbidden transition (`posted_in_erp` + `paid`) stays a pure silent
no-op with no exception.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.invoice import InvoiceStatus


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _fake_request(body: bytes, headers: dict | None = None):
    req = MagicMock()
    req.body = AsyncMock(return_value=body)

    async def _json():
        return json.loads(body.decode("utf-8"))

    req.json = _json
    req.headers = headers or {}
    return req


def _ctrl_session_for_org(org):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _fake_tenant_session_factory(invoice, *, existing_recon_count: int = 0, settling_payment=None):
    """Fake tenant session. The handler's first TWO ``execute`` calls resolve
    the invoice — an id lookup by ``correlation_id``, then the row-locked
    re-fetch via ``get_invoice_for_update`` (issue #141) — both returning the
    same invoice.

    On the ``payment_scheduled → paid`` edge the handler then issues one more
    ``execute`` — the settling-payment lookup that feeds ``settlement_coverage``
    — which returns ``settling_payment`` (``None`` = no completed payment, so
    the coverage check is skipped and the transition applies). If the coverage
    verdict is non-covering it issues a FURTHER ``execute`` — the
    open-``erp_reconciliation``-exception dedup count — returning
    ``existing_recon_count``.

    The ``side_effect`` list is exact, so a stand-in invoice that omits a field
    a real ``Invoice`` always carries can silently add an extra ``execute`` and
    exhaust it — the handler swallows the resulting error and the test fails on
    a missing commit rather than on the real cause. Every invoice stand-in in
    this file therefore carries ``correlation_id``: it is what the exception's
    ``exception.raised`` audit row files under (services/exception_lifecycle),
    and the fallback lookup only exists for the invoice-less / id-only callers.
    """
    invoice_result = MagicMock()
    invoice_result.scalar_one_or_none = MagicMock(return_value=invoice)
    # One flexible result for BOTH follow-on queries: the settling-payment
    # lookup (`.scalar_one_or_none()`, only on the `payment_scheduled → paid`
    # edge) and the open-`erp_reconciliation`-exception dedup count
    # (`.scalar()`). Serving both off one object keeps the exact `side_effect`
    # sequence path-independent — the void and the short-settlement paths make
    # a different number of queries.
    flex_result = MagicMock()
    flex_result.scalar_one_or_none = MagicMock(return_value=settling_payment)
    flex_result.scalar = MagicMock(return_value=existing_recon_count)

    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[invoice_result, invoice_result, flex_result, flex_result])
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, db


def _org_with_erp_secret(secret: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"erp": {"webhook_signing_secret": secret}},
    )


class _FakeRedis:
    """Minimal SET NX EX / get store for the dedup helper."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def get(self, key):
        return self._store.get(key)

    async def delete(self, key):
        self._store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    return fake


async def _post(
    *,
    org,
    invoice,
    status_value,
    event_id,
    secret="erp-secret",
    existing_recon_count=0,
    settling_payment=None,
):
    """Drive `erp_webhook` with a correctly-signed body for `status_value`.

    `existing_recon_count` seeds the open-`erp_reconciliation`-exception dedup
    count the reconciliation path reads; `settling_payment` is the completed
    `Payment` the `payment_scheduled → paid` settlement-coverage check finds
    (default `None` = no completed payment, check skipped).

    Returns (result, tenant_db, transition_calls) where transition_calls records
    the args every `transition_invoice` invocation received (empty if the guard
    short-circuited before it).
    """
    from app.api.erp_webhook import erp_webhook

    body = {
        "tenant_slug": "acme",
        "correlation_id": str(uuid.uuid4()),
        "erp_document_id": "doc_1",
        "status": status_value,
        "event_id": event_id,
    }
    body_bytes = json.dumps(body).encode("utf-8")
    sig = _sign(secret, body_bytes)

    tenant_factory, db = _fake_tenant_session_factory(
        invoice,
        existing_recon_count=existing_recon_count,
        settling_payment=settling_payment,
    )
    transition_calls: list[dict] = []

    async def _capture(_db, inv, target, *, action_name, details):
        transition_calls.append(
            {"invoice": inv, "target": target, "action_name": action_name, "details": details}
        )
        inv.status = target
        return inv

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine", return_value=MagicMock()),
        patch("app.api.erp_webhook.async_sessionmaker", return_value=tenant_factory),
        patch("app.api.erp_webhook.transition_invoice", _capture),
    ):
        result = await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": sig}),
        )
    return result, db, transition_calls


@pytest.mark.asyncio
async def test_forbidden_void_while_advanced_opens_reconciliation_exception(fake_redis):
    """The reconciliation case: ERP reports `voided` (→ failed) for an invoice
    already `posted_in_erp`. VALID_TRANSITIONS forbids `posted_in_erp → failed`,
    so the webhook must silently 204 — never raise the 409 the old divergent map
    provoked — and must NOT transition the invoice. But a void/cancel of an
    already-advanced invoice is a real reconciliation signal (money may be in
    flight), so it opens exactly ONE open `erp_reconciliation` Exception for
    human review, with a PII-free description."""
    from app.api.erp_webhook import ERP_RECONCILIATION_EXCEPTION_TYPE
    from app.models.exception import Exception as APException

    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.posted_in_erp
    )

    result, db, transition_calls = await _post(
        org=org, invoice=invoice, status_value="voided", event_id="ev_void"
    )

    assert result is None  # silent 204, not a raised HTTPException
    assert invoice.status is InvoiceStatus.posted_in_erp  # NOT auto-transitioned
    assert transition_calls == []  # never reached the transition/audit chokepoint

    # Exactly one Exception row created, of the reconciliation type, open.
    added = [a.args[0] for a in db.add.call_args_list if isinstance(a.args[0], APException)]
    assert len(added) == 1
    exc = added[0]
    assert exc.exception_type == ERP_RECONCILIATION_EXCEPTION_TYPE
    assert exc.status == "open"
    assert exc.severity == "error"
    assert exc.invoice_id == invoice.id
    assert exc.organization_id == org.id
    # PII-free description: whitelisted ERP routing identifiers only, no raw
    # ERP `details` payload (no bank/tax/address). Sanity-check the shape.
    assert "voided" in exc.description
    assert "posted_in_erp" in exc.description
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_forbidden_void_skips_when_open_reconciliation_exists(fake_redis):
    """A DISTINCT later ERP void event (different event id, so the webhook
    event-id dedup doesn't fire) for an invoice that ALREADY has an open
    `erp_reconciliation` exception must NOT pile up a second one — the
    reconciliation path is idempotent on the open exception."""
    from app.models.exception import Exception as APException

    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.payment_scheduled
    )

    result, db, transition_calls = await _post(
        org=org,
        invoice=invoice,
        status_value="cancelled",
        event_id="ev_void_2",
        existing_recon_count=1,  # an open erp_reconciliation already exists
    )

    assert result is None
    assert invoice.status is InvoiceStatus.payment_scheduled
    assert transition_calls == []
    added = [a.args[0] for a in db.add.call_args_list if isinstance(a.args[0], APException)]
    assert added == []  # no duplicate exception


@pytest.mark.asyncio
async def test_void_redelivery_is_event_deduped_no_second_exception(fake_redis):
    """A redelivery (SAME event id) is caught by the webhook event-id dedup
    BEFORE the invoice lookup — so the second delivery opens no exception at
    all. Guards against a redelivered void double-flagging."""
    from app.models.exception import Exception as APException

    org = _org_with_erp_secret("erp-secret")

    first_inv = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.paid
    )
    r1, db1, _ = await _post(org=org, invoice=first_inv, status_value="voided", event_id="ev_dup")
    assert r1 is None
    added1 = [a.args[0] for a in db1.add.call_args_list if isinstance(a.args[0], APException)]
    assert len(added1) == 1  # first delivery flags it

    # Redelivery with the same event id → deduped before the tenant DB is opened.
    second_inv = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.paid
    )
    r2, db2, _ = await _post(org=org, invoice=second_inv, status_value="voided", event_id="ev_dup")
    assert r2 is None
    db2.add.assert_not_called()  # never reached exception creation
    db2.commit.assert_not_called()


@pytest.mark.asyncio
async def test_forbidden_nonvoid_transition_is_pure_silent_204(fake_redis):
    """A stale `Open` (→ posted_in_erp) for an invoice already `paid` is NOT in
    the canonical machine. Since it is a NON-void forbidden transition it must
    stay a PURE silent no-op — no transition AND no reconciliation exception
    (that would be noise).

    This used to be pinned with `posted_in_erp` + `Paid`, which is no longer a
    no-op: the ERP paying an invoice FeohLedger never paid is now recorded as a
    payment made outside FeohLedger (issue #517) — see the next test."""
    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.paid
    )

    result, db, transition_calls = await _post(
        org=org, invoice=invoice, status_value="Open", event_id="ev_open_stale"
    )

    assert result is None
    assert invoice.status is InvoiceStatus.paid
    assert transition_calls == []
    db.add.assert_not_called()  # no exception created for a non-void forbidden edge
    db.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current", [InvoiceStatus.posted_in_erp, InvoiceStatus.sent_to_erp, InvoiceStatus.approved]
)
async def test_erp_paid_for_an_invoice_feohledger_never_paid_is_recorded(fake_redis, current):
    """The ERP-led half of the no-rail pilot: `posted_in_erp → paid` is not an
    edge (a `paid` invoice needs a payment behind it), so the handler records the
    ERP's payment through `_record_erp_reported_payment` — which supplies one —
    and commits. It used to drop the report silently, leaving a no-rail tenant
    with no automatic path to `paid`."""
    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=current)

    recorded = AsyncMock()
    with patch("app.api.erp_webhook._record_erp_reported_payment", recorded):
        result, db, transition_calls = await _post(
            org=org, invoice=invoice, status_value="Paid", event_id=f"ev_paid_{current.value}"
        )

    assert result is None
    recorded.assert_awaited_once()
    kwargs = recorded.await_args.kwargs
    assert kwargs["org"] is org
    assert kwargs["erp_document_id"] == "doc_1"
    assert transition_calls == []  # the record path owns the transitions
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_permitted_transition_sent_to_posted_applies_and_audits(fake_redis):
    """A legitimately-permitted ERP-driven transition still applies. ERP reports
    `Open` (→ posted_in_erp) for an invoice at `sent_to_erp` — a legal edge —
    so it must go through transition_invoice (the audit chokepoint) and commit."""
    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(status=InvoiceStatus.sent_to_erp)

    result, db, transition_calls = await _post(
        org=org, invoice=invoice, status_value="Open", event_id="ev_open"
    )

    assert result is None
    assert len(transition_calls) == 1
    call = transition_calls[0]
    assert call["target"] is InvoiceStatus.posted_in_erp
    assert call["action_name"] == "invoice.erp_status_posted_in_erp"
    # PII-free whitelisted audit details only.
    assert set(call["details"]) == {"erp_type", "erp_status", "erp_document_id", "raw_event_id"}
    assert invoice.status is InvoiceStatus.posted_in_erp
    db.commit.assert_awaited()


def _payment(*, settled_amount, amount="1000.00", settled_currency="USD"):
    """A completed `Payment` stand-in for the settlement-coverage check."""
    from decimal import Decimal

    return SimpleNamespace(
        id=uuid.uuid4(),
        status="completed",
        amount=Decimal(amount),
        settled_amount=None if settled_amount is None else Decimal(settled_amount),
        settled_currency=settled_currency,
        settled_amount_unstorable=False,
        source_amount=None,
        source_currency=None,
    )


@pytest.mark.asyncio
async def test_permitted_transition_scheduled_to_paid_applies(fake_redis):
    """`payment_scheduled → paid` is a canonical edge (ERP reports paid). With no
    completed payment on file the settlement-coverage check is skipped and the
    transition applies + commits as before."""
    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        currency="USD",
        status=InvoiceStatus.payment_scheduled,
    )

    result, db, transition_calls = await _post(
        org=org, invoice=invoice, status_value="paid", event_id="ev_paid2"
    )

    assert result is None
    assert len(transition_calls) == 1
    assert transition_calls[0]["target"] is InvoiceStatus.paid
    assert invoice.status is InvoiceStatus.paid


@pytest.mark.asyncio
async def test_scheduled_to_paid_applies_when_settlement_covers(fake_redis):
    """A completed payment whose rail settled in full → the ERP's `paid` still
    applies. The coverage check runs but does not hold."""
    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        currency="USD",
        status=InvoiceStatus.payment_scheduled,
    )

    result, db, transition_calls = await _post(
        org=org,
        invoice=invoice,
        status_value="paid",
        event_id="ev_paid_covered",
        settling_payment=_payment(settled_amount="1000.00", amount="1000.00"),
    )

    assert result is None
    assert len(transition_calls) == 1
    assert invoice.status is InvoiceStatus.paid


@pytest.mark.asyncio
async def test_scheduled_to_paid_is_held_when_the_rail_settled_short(fake_redis):
    """The bug: a validly-signed ERP `{"status":"Paid"}` for an invoice whose
    payment settled SHORT of what AP authorized must NOT flip it to `paid` —
    `payment_erp_sync` holds it at `payment_scheduled`, and this must too. It
    opens exactly one `erp_reconciliation` exception and does not transition."""
    from app.api.erp_webhook import ERP_RECONCILIATION_EXCEPTION_TYPE
    from app.models.exception import Exception as APException

    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        currency="USD",
        status=InvoiceStatus.payment_scheduled,
    )

    result, db, transition_calls = await _post(
        org=org,
        invoice=invoice,
        status_value="paid",
        event_id="ev_paid_short",
        # Authorized $1000, rail moved $250 — a clear shortfall.
        settling_payment=_payment(settled_amount="250.00", amount="1000.00"),
    )

    assert result is None
    assert transition_calls == []  # NOT transitioned
    assert invoice.status is InvoiceStatus.payment_scheduled
    # Exactly one Exception row, of the reconciliation type, open, PII-free.
    added = [a.args[0] for a in db.add.call_args_list if isinstance(a.args[0], APException)]
    assert len(added) == 1
    exc = added[0]
    assert exc.exception_type == ERP_RECONCILIATION_EXCEPTION_TYPE
    assert exc.status == "open"
    assert exc.invoice_id == invoice.id
    assert "settled" in exc.description and "payment_scheduled" in exc.description
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_scheduled_to_paid_short_does_not_pile_up_a_second_exception(fake_redis):
    """If an open `erp_reconciliation` exception already exists for the invoice,
    a redelivered short-settlement `paid` must not open a second one."""
    from app.models.exception import Exception as APException

    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        currency="USD",
        status=InvoiceStatus.payment_scheduled,
    )

    result, db, transition_calls = await _post(
        org=org,
        invoice=invoice,
        status_value="paid",
        event_id="ev_paid_short_dup",
        settling_payment=_payment(settled_amount="250.00", amount="1000.00"),
        existing_recon_count=1,
    )

    assert result is None
    assert transition_calls == []
    assert invoice.status is InvoiceStatus.payment_scheduled
    # dedup — no second Exception row
    added = [a.args[0] for a in db.add.call_args_list if isinstance(a.args[0], APException)]
    assert added == []
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# Body size cap (memory-exhaustion DoS guard, GitHub issue #142)
#
# `erp_webhook` used to `await request.body()` before any signature/auth
# check, with no size cap — an unauthenticated attacker could POST a
# multi-gigabyte body and have it buffered fully into memory before the HMAC
# check ever ran. The guard bounds the body in two phases, mirroring
# `peppol_inbound_webhook`: reject on a declared Content-Length over the cap
# BEFORE reading the body at all, then re-check the actual read length in
# case the header lied or was absent (e.g. chunked transfer).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_length_over_cap_rejects_before_body_read(monkeypatch):
    """A declared Content-Length over the cap must reject WITHOUT ever
    awaiting `request.body()` — the whole point is bounding memory before
    anything is buffered."""
    from app.api.erp_webhook import erp_webhook
    from app.config import settings

    monkeypatch.setattr(settings, "erp_webhook_max_bytes", 1024)
    request = _fake_request(b"", {"content-length": "999999"})

    result = await erp_webhook(erp_type="generic", request=request)

    assert result is None  # silent 204, not a raised exception
    request.body.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_length_malformed_rejects_before_body_read(monkeypatch):
    """A non-integer Content-Length header must also reject before reading —
    a malformed header shouldn't fall through to an unbounded read."""
    from app.api.erp_webhook import erp_webhook
    from app.config import settings

    monkeypatch.setattr(settings, "erp_webhook_max_bytes", 1024)
    request = _fake_request(b"", {"content-length": "not-a-number"})

    result = await erp_webhook(erp_type="generic", request=request)

    assert result is None
    request.body.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_body_without_content_length_rejects_after_read(monkeypatch):
    """Simulates chunked transfer (no Content-Length header): the body is read
    once, then rejected by the post-read length check."""
    from app.api.erp_webhook import erp_webhook
    from app.config import settings

    monkeypatch.setattr(settings, "erp_webhook_max_bytes", 1024)
    big_body = b"x" * 2048
    request = _fake_request(big_body, {})

    result = await erp_webhook(erp_type="generic", request=request)

    assert result is None
    request.body.assert_awaited_once()


@pytest.mark.asyncio
async def test_content_length_understates_actual_size_still_rejects(monkeypatch):
    """A Content-Length header that lies (understates the real body) must
    still be caught by the post-read re-check, not trusted blindly."""
    from app.api.erp_webhook import erp_webhook
    from app.config import settings

    monkeypatch.setattr(settings, "erp_webhook_max_bytes", 1024)
    big_body = b"x" * 2048
    request = _fake_request(big_body, {"content-length": "10"})

    result = await erp_webhook(erp_type="generic", request=request)

    assert result is None
    request.body.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_signed_request_under_cap_still_succeeds(fake_redis):
    """Regression guard: the new size-cap check must not break a legitimate,
    normal-sized signed request (default cap is a few MB; this body is tiny)."""
    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(status=InvoiceStatus.sent_to_erp)

    result, db, transition_calls = await _post(
        org=org, invoice=invoice, status_value="Open", event_id="ev_size_ok"
    )

    assert result is None
    assert len(transition_calls) == 1
    assert transition_calls[0]["target"] is InvoiceStatus.posted_in_erp
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# Dedup claim release on rollback + row-locked invoice fetch (GitHub issue #141)
#
# Bug A: the dedup claim from `is_event_already_processed` was never released
# on either exception path, unlike cards.py / billing_webhook.py /
# email_intake.py — a transient failure (bad correlation_id, DB hiccup)
# permanently dropped the ERP's retry for the full dedup TTL.
# Bug B: the invoice was fetched via a plain `select(Invoice)` instead of
# `get_invoice_for_update()`, so a concurrent human approval racing the
# webhook could silently overwrite the other.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_claim_released_when_transition_raises(fake_redis):
    """A failure after the dedup claim (e.g. transition_invoice raising) must
    release the claim so the ERP's retry with the SAME event_id can
    reprocess — otherwise the status update is silently dropped for the
    full TTL window.

    Releasing the claim is only half of it: the response must also ASK for that
    retry. A silent 204 tells the ERP the event was delivered, so the retry the
    release prepares for never arrives. Our own failure therefore returns a
    bodyless 503 (see `tests/test_erp_webhook_retry_on_our_failure.py` for the
    decision-vs-failure split this is one half of)."""
    from app.services.webhook_security import is_event_already_processed

    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.sent_to_erp
    )

    tenant_factory, db = _fake_tenant_session_factory(invoice)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db hiccup mid-transition")

    body = {
        "tenant_slug": "acme",
        "correlation_id": str(uuid.uuid4()),
        "erp_document_id": "doc_1",
        "status": "Open",
        "event_id": "ev_claim_release",
    }
    body_bytes = json.dumps(body).encode("utf-8")
    sig = _sign("erp-secret", body_bytes)

    from app.api.erp_webhook import erp_webhook

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine", return_value=MagicMock()),
        patch("app.api.erp_webhook.async_sessionmaker", return_value=tenant_factory),
        patch("app.api.erp_webhook.transition_invoice", _boom),
    ):
        result = await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": sig}),
        )

    # Bodyless 503, not a raised 500 and not the silent 204 a *decision* gets.
    assert result is not None
    assert result.status_code == 503
    assert result.body == b""
    db.rollback.assert_awaited()

    # The claim must be gone — a retry with the SAME event_id must NOT be
    # deduped away.
    still_claimed = await is_event_already_processed("erp:generic", "ev_claim_release")
    assert still_claimed is False


@pytest.mark.asyncio
async def test_invoice_fetch_uses_row_lock(fake_redis):
    """The invoice must be resolved via `get_invoice_for_update` (row lock),
    not a bare unlocked `select(Invoice)` — the documented convention for
    any status transition, preventing a concurrent human approval racing
    the webhook from silently overwriting the other."""
    from app.api.erp_webhook import erp_webhook

    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.sent_to_erp
    )
    tenant_factory, db = _fake_tenant_session_factory(invoice)

    async def _capture(_db, inv, target, *, action_name, details):
        inv.status = target
        return inv

    body = {
        "tenant_slug": "acme",
        "correlation_id": str(uuid.uuid4()),
        "erp_document_id": "doc_1",
        "status": "Open",
        "event_id": "ev_row_lock",
    }
    body_bytes = json.dumps(body).encode("utf-8")
    sig = _sign("erp-secret", body_bytes)

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine", return_value=MagicMock()),
        patch("app.api.erp_webhook.async_sessionmaker", return_value=tenant_factory),
        patch("app.api.erp_webhook.transition_invoice", _capture),
        patch(
            "app.api.erp_webhook.get_invoice_for_update", AsyncMock(return_value=invoice)
        ) as locked_fetch,
    ):
        result = await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": sig}),
        )

    assert result is None
    # The handler must route the invoice fetch through the shared row-locked
    # helper — not build its own unlocked `select(Invoice)` — regardless of
    # exactly what id value this flat mock's id-lookup query resolves to.
    locked_fetch.assert_awaited_once()
    assert locked_fetch.await_args.args[0] is db
